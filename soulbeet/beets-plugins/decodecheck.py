"""Beets decode-gate plugin: verify every audio file decodes cleanly before import.

Implements docs/decode-gate.md:
- Hooks ``import_task_created`` so corrupt, truncated, or zero-duration lossless
  files are stopped before autotagging or moving files into the library.
- Checks every audio item of a task using ``ffmpeg`` full decode to null.
- If ANY file in a task fails, drops the entire task (returning ``[]``), moves
  ALL files belonging to the task (audio and non-audio) into
  ``<state_dir>/quarantine/<YYYY-MM-DD>/<task_id>/<relative path>``, and appends
  one JSONL record per bad file to ``<state_dir>/rejects.jsonl`` under
  ``fcntl.flock``.
- If all files pass, optionally records a summary line in
  ``<state_dir>/checked.jsonl`` and returns ``[task]`` unchanged.
- Fails closed: if ffmpeg cannot run or an unexpected exception occurs, logs
  clearly and rejects the task rather than letting unchecked audio enter the
  library or crashing the beets run.
"""

from __future__ import annotations

import dataclasses
import datetime
import fcntl
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import threading
import time
from typing import Any

from beets.plugins import BeetsPlugin

try:
    from beets.importer.tasks import (
        ArchiveImportTask,
        SentinelImportTask,
        SingletonImportTask,
    )
except ImportError:  # pragma: no cover - fallback for older beets or test stubs
    try:
        from beets.importer import (
            ArchiveImportTask,
            SentinelImportTask,
            SingletonImportTask,
        )
    except ImportError:
        ArchiveImportTask = None  # type: ignore[assignment,misc]
        SentinelImportTask = None  # type: ignore[assignment,misc]
        SingletonImportTask = None  # type: ignore[assignment,misc]


CONTRACT_VERSION = 1
MAX_DETAIL_CHARS = 300
PRUNE_INTERVAL_S = 3600
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OUT_TIME_HMS_RE = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")

# Formats and extensions treated as lossless for the no_duration check.
LOSSLESS_FORMATS = frozenset(
    {
        "FLAC",
        "ALAC",
        "APE",
        "WAVPACK",
        "WV",
        "WAVE",
        "WAV",
        "AIFF",
        "DSD STREAM FILE",
        "DSF",
        "TTA",
    }
)
LOSSLESS_EXTS = frozenset(
    {
        ".flac",
        ".alac",
        ".ape",
        ".wv",
        ".wav",
        ".aif",
        ".aiff",
        ".aifc",
        ".dsf",
        ".dff",
        ".tta",
    }
)

# Extensions considered audio files when inspecting task directories for
# unreadable tracks dropped by mutagen or detecting sibling album subdirs.
AUDIO_EXTS = frozenset(
    {
        ".flac",
        ".mp3",
        ".m4a",
        ".aac",
        ".alac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".aif",
        ".aiff",
        ".aifc",
        ".wv",
        ".ape",
        ".wma",
        ".asf",
        ".mpc",
        ".dsf",
        ".dff",
        ".tta",
    }
)

# FFmpeg decoder context names for non-audio (image / video) codecs whose
# stderr probe warnings on embedded cover art (attached_pic streams) must not
# trigger a decode_error. Even with ``-vn``, ffmpeg probes embedded picture
# streams when opening a file and can log e.g. ``[mjpeg @ 0x...] unable to
# decode APP fields`` on malformed cover art while the audio decodes cleanly.
IGNORED_NON_AUDIO_CODECS = frozenset(
    {
        "mjpeg",
        "mjpegb",
        "jpegls",
        "jpeg2000",
        "libopenjpeg",
        "png",
        "apng",
        "bmp",
        "gif",
        "webp",
        "tiff",
        "ppm",
        "pgm",
        "pbm",
        "pam",
        "pcx",
        "sgi",
        "targa",
        "psd",
        "exr",
        "qoi",
        "xbm",
        "xpm",
        "h264",
        "hevc",
        "vp8",
        "vp9",
        "av1",
    }
)
FFMPEG_CTX_RE = re.compile(r"^\[([A-Za-z0-9_,.-]+)\s*@\s*0x[0-9a-fA-F]+\]\s*")
FFMPEG_REPEAT_RE = re.compile(r"^Last message repeated \d+ times$")

# Exact full-line stderr messages emitted exclusively by FFmpeg's container
# metadata / tag / attached-picture parsers (verified against FFmpeg 8.0
# libavformat/{id3v2,apetag,flac_picture,flacdec,oggparsevorbis,mov}.c).
# These are matched on the full line after stripping an optional single
# ``[<ctx> @ 0x...]`` prefix (id3v2.c calls ``av_log(s, ...)`` with ``s=NULL``
# during input open, so ID3v2 messages often arrive unprefixed).
#
# Conservative by design: generic decoder/container errors ("Invalid data found
# when processing input", "Header missing", "Error while decoding stream",
# "Skipping N bytes of junk", OOM allocation errors, etc.) are intentionally
# excluded. Ignored metadata warnings never override a non-zero exit code,
# truncation, or lossless no_duration verdict.
IGNORED_METADATA_WARNINGS = frozenset(
    {
        # libavformat/id3v2.c
        "Cannot read BOM value, input too short",
        "Incorrect BOM value",
        "Error reading lyrics, skipped",
        "Error reading comment frame, skipped",
        "Error reading GEOB frame, data truncated.",
        "Error decoding attached picture description.",
        "Failed to read tag data",
        "Failed to read compressed tag",
        "invalid frame id, assuming padding",
        # libavformat/apetag.c
        "APE tag size too large.",
        "Error reading cover art.",
        "Tag size is way too big",
        "APE Tag is a header",
        # libavformat/flac_picture.c & libavformat/flacdec.c
        "Attached picture metadata block too short",
        "Could not read mimetype from an attached picture.",
        "Error parsing attached picture.",
        "error parsing VorbisComment metadata",
        # libavformat/oggparsevorbis.c
        "Failed to parse cover art block.",
        # libavformat/mov.c (metadata / cover-art atoms only)
        "Error parsing cover art.",
        "UDTA parsing failed retrying raw",
        "loci too short",
    }
)

# Anchored full-line regexes for parameterized metadata/tag-only warnings from
# the same FFmpeg 8.0 libavformat parsers.
IGNORED_METADATA_WARNING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # libavformat/id3v2.c
    re.compile(r"^Error reading frame(?: .*)?, skipped$"),
    re.compile(r"^Failed to uncompress tag: -?\d+$"),
    re.compile(r"^Unknown attached picture mimetype: .+, skipping\.$"),
    re.compile(r"^Unknown attached picture type -?\d+\.$"),
    re.compile(r"^Skipping .+ ID3v2 frame .+\.$"),
    re.compile(r"^ID3v2\.\d+ tag skipped, cannot handle .+$"),
    # libavformat/apetag.c
    re.compile(r"^Unsupported tag version\. \(>=\d+\)$"),
    re.compile(r"^Invalid tag size \d+\.$"),
    re.compile(r"^Too many tag fields \(\d+\)$"),
    re.compile(r"^Invalid APE tag key '.*'\.$"),
    re.compile(r"^Skipping binary tag '.*'\.$"),
    # libavformat/flac_picture.c
    re.compile(r"^Invalid picture type: -?\d+\.$"),
    re.compile(r"^Attached picture metadata block too big \d+$"),
    re.compile(r"^Unknown attached picture mimetype: .+\.$"),
    # libavformat/mov.c
    re.compile(r"^loci too short \(\d+ bytes left, need at least \d+\)$"),
    re.compile(r"^The 'keys' atom with the invalid key count: \d+$"),
    re.compile(r"^The key# \d+ in meta has invalid size:\d+$"),
    re.compile(r"^The index of 'data' is out of range: -?\d+ < 1 or >= -?\d+\.$"),
    re.compile(r"^Skipping unhandled metadata .+ of type -?\d+$"),
    re.compile(r"^Unknown cover type: 0x[0-9a-fA-F]+\.$"),
)


@dataclasses.dataclass
class CheckResult:
    """Outcome of checking a single audio file."""

    ok: bool
    reason: str | None
    detail: str
    header_s: float | None
    decoded_s: float | None
    magic_mismatch: str | None = None
    ignored_warnings: tuple[str, ...] = ()
    trailing_tag: str | None = None


def _lower_priority() -> None:
    """Lower subprocess scheduling priority on POSIX systems (best-effort)."""
    try:
        if hasattr(os, "nice"):
            os.nice(10)
    except OSError:
        pass


def _round_seconds(val: float | None) -> float | None:
    if val is None:
        return None
    return round(float(val), 2)


def _to_str_path(path: Any) -> str:
    if isinstance(path, (bytes, str, os.PathLike)):
        return os.fsdecode(path)
    return str(path)


def parse_progress_out_time_s(stdout: str | bytes | None) -> float | None:
    """Parse decoded duration in seconds from ffmpeg ``-progress pipe:1`` output.

    Uses the last valid ``out_time_us`` line (microseconds). Falls back to
    ``out_time_ms`` (which ffmpeg also emits in microseconds) and then
    ``out_time=HH:MM:SS.uuuuuu`` if ``out_time_us`` is absent or ``N/A``.
    """
    if not stdout:
        return None
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")

    last_us: int | None = None
    last_ms_field: int | None = None
    last_hms: float | None = None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("out_time_us="):
            val = line.split("=", 1)[1].strip()
            try:
                us = int(val)
            except ValueError:
                continue
            if us >= 0:
                last_us = us
        elif line.startswith("out_time_ms="):
            val = line.split("=", 1)[1].strip()
            try:
                us = int(val)
            except ValueError:
                continue
            if us >= 0:
                last_ms_field = us
        elif line.startswith("out_time="):
            val = line.split("=", 1)[1].strip()
            m = OUT_TIME_HMS_RE.match(val)
            if m:
                hours = int(m.group(1))
                mins = int(m.group(2))
                secs = float(m.group(3))
                last_hms = hours * 3600.0 + mins * 60.0 + secs

    if last_us is not None:
        return last_us / 1_000_000.0
    if last_ms_field is not None:
        return last_ms_field / 1_000_000.0
    if last_hms is not None:
        return last_hms
    return None


def is_lossless(item_format: str | None, path: str) -> bool:
    """Return True if the file is a lossless audio format."""
    if item_format and item_format.strip().upper() in LOSSLESS_FORMATS:
        return True
    ext = os.path.splitext(path)[1].lower()
    return ext in LOSSLESS_EXTS


def detect_magic_mismatch(path: str) -> str | None:
    """Detect cosmetic container/magic mismatches (e.g. ID3v2 prepended to FLAC).

    Returns a short descriptor string on mismatch, or ``None`` if magic matches
    the extension (or the extension is not checked / file cannot be read).
    Never used to reject files — logged only for accepted tasks.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if not head:
        return None

    if ext == ".flac":
        if head.startswith(b"fLaC"):
            return None
        if head.startswith(b"ID3"):
            return "flac_with_id3v2"
        return f"flac_magic_{head[:4].hex()}"

    if ext == ".mp3":
        if head.startswith(b"ID3"):
            return None
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
            return None
        if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WAVE":
            return "mp3_in_wav_riff"
        return f"mp3_magic_{head[:4].hex()}"

    if ext in (".ogg", ".oga", ".opus"):
        return None if head.startswith(b"OggS") else f"ogg_magic_{head[:4].hex()}"

    if ext in (".m4a", ".mp4", ".m4b", ".alac"):
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return None
        return f"mp4_magic_{head[:8].hex()}"

    if ext == ".wav":
        if (
            len(head) >= 12
            and head[:4] in (b"RIFF", b"RF64")
            and head[8:12] == b"WAVE"
        ):
            return None
        return f"wav_magic_{head[:4].hex()}"

    if ext in (".aif", ".aiff", ".aifc"):
        if (
            len(head) >= 12
            and head[:4] == b"FORM"
            and head[8:12] in (b"AIFF", b"AIFC")
        ):
            return None
        return f"aiff_magic_{head[:4].hex()}"

    if ext == ".wv":
        return None if head.startswith(b"wvpk") else f"wv_magic_{head[:4].hex()}"

    if ext == ".ape":
        return None if head.startswith(b"MAC ") else f"ape_magic_{head[:4].hex()}"

    if ext == ".dsf":
        return None if head.startswith(b"DSD ") else f"dsf_magic_{head[:4].hex()}"

    if ext == ".mpc":
        if head.startswith(b"MP+") or head.startswith(b"MPCK"):
            return None
        return f"mpc_magic_{head[:4].hex()}"

    if ext in (".wma", ".asf"):
        asf_guid = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"
        return None if head.startswith(asf_guid) else f"asf_magic_{head[:4].hex()}"

    return None


def compute_timeout(header_s: float | None) -> float:
    """Per-file decode timeout: ``max(120 s, 2 x header length)``."""
    h = float(header_s) if (header_s is not None and header_s > 0) else 0.0
    return max(120.0, 2.0 * h)


def is_ignored_image_warning(line: str) -> bool:
    """Return True if ``line`` comes from an allowed non-audio codec context."""
    m = FFMPEG_CTX_RE.match(line)
    return bool(m and m.group(1).lower() in IGNORED_NON_AUDIO_CODECS)


def is_ignored_metadata_warning(line: str) -> bool:
    """Return True if ``line`` matches an exact known tag/metadata-only warning.

    Strips at most one leading ``[<ctx> @ 0x...]`` prefix before checking full-
    line exact literals (``IGNORED_METADATA_WARNINGS``) and anchored patterns
    (``IGNORED_METADATA_WARNING_PATTERNS``).
    """
    m = FFMPEG_CTX_RE.match(line)
    body = line[m.end() :] if m else line
    if body in IGNORED_METADATA_WARNINGS:
        return True
    return any(pat.match(body) for pat in IGNORED_METADATA_WARNING_PATTERNS)


def filter_ffmpeg_stderr(stderr: str | None) -> tuple[list[str], list[str]]:
    """Split ffmpeg stderr into actionable error lines and ignored warnings.

    Lines are moved to ``ignored_lines`` when they match either:
    - an explicit non-audio (image/video) codec context in
      ``IGNORED_NON_AUDIO_CODECS`` (``[<codec> @ 0x...]``), or
    - an exact full-line metadata/tag-parsing warning in
      ``IGNORED_METADATA_WARNINGS`` / ``IGNORED_METADATA_WARNING_PATTERNS``
      (optionally preceded by a single ``[<ctx> @ 0x...]`` prefix), or
    - ``Last message repeated N times`` immediately following an ignored line.

    Audio decoder errors, generic container/stream errors, and all other
    unprefixed messages remain in ``error_lines``.
    """
    error_lines: list[str] = []
    ignored_lines: list[str] = []
    prev_ignored = False
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if is_ignored_image_warning(line) or is_ignored_metadata_warning(line):
            ignored_lines.append(line)
            prev_ignored = True
        elif prev_ignored and FFMPEG_REPEAT_RE.match(line):
            ignored_lines.append(line)
        else:
            error_lines.append(line)
            prev_ignored = False
    return error_lines, ignored_lines


def evaluate_decode(
    path: str,
    header_s: float | None,
    item_format: str | None,
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    min_check_s: float = 15.0,
    trunc_abs_s: float = 5.0,
    trunc_frac: float = 0.05,
    check_magic: bool = True,
) -> CheckResult:
    """Apply the decode-gate rules to a completed ffmpeg invocation."""
    decoded_raw = parse_progress_out_time_s(stdout)
    error_lines, ignored_lines = filter_ffmpeg_stderr(stderr)
    error_text = "\n".join(error_lines)

    h_val = float(header_s) if header_s is not None else None
    h_rounded = _round_seconds(h_val)

    # Rule 2: any non-ignored stderr output or non-zero exit code -> decode_error.
    if returncode != 0 or error_text:
        detail = (
            error_text[:MAX_DETAIL_CHARS]
            if error_text
            else f"ffmpeg exited with status {returncode}"
        )
        return CheckResult(
            ok=False,
            reason="decode_error",
            detail=detail,
            header_s=h_rounded,
            decoded_s=_round_seconds(decoded_raw),
        )

    decoded_val = decoded_raw if decoded_raw is not None else 0.0
    d_rounded = _round_seconds(decoded_val)

    # Rule 3: truncation check when header duration exceeds min_check_s.
    if h_val is not None and h_val > min_check_s:
        tolerance = max(trunc_abs_s, h_val * trunc_frac)
        if decoded_val < h_val - tolerance:
            detail = (
                f"decoded {decoded_val:.2f}s < expected {h_val:.2f}s "
                f"(tolerance {tolerance:.2f}s)"
            )[:MAX_DETAIL_CHARS]
            return CheckResult(
                ok=False,
                reason="truncated",
                detail=detail,
                header_s=h_rounded,
                decoded_s=d_rounded,
            )

    # Rule 4: lossless files with missing or zero header duration -> no_duration.
    if is_lossless(item_format, path) and (h_val is None or h_val <= 0.0):
        detail = (
            f"lossless file has missing/zero header duration "
            f"(header_s={h_val or 0.0:.2f}, decoded_s={decoded_val:.2f})"
        )[:MAX_DETAIL_CHARS]
        return CheckResult(
            ok=False,
            reason="no_duration",
            detail=detail,
            header_s=h_rounded if h_rounded is not None else 0.0,
            decoded_s=d_rounded,
        )

    # Rule 5: magic mismatch is cosmetic; record on accepted task only.
    magic = detect_magic_mismatch(path) if check_magic else None
    return CheckResult(
        ok=True,
        reason=None,
        detail="",
        header_s=h_rounded,
        decoded_s=d_rounded,
        magic_mismatch=magic,
        ignored_warnings=tuple(ignored_lines),
    )


# Exit codes that mean the checker itself could not run (shell convention:
# 126 = not executable, 127 = not found / missing shared library) and signals
# that come from outside (OOM killer, shutdown), not from the file.
CHECKER_FAILURE_RCS = frozenset({126, 127})
CHECKER_FAILURE_SIGNALS = frozenset({-9, -15})


# Trailing non-FLAC metadata tag constants (ID3v1, ID3v1 enhanced, APEv1/v2).
ID3V1_SIZE = 128
ID3V1_ENHANCED_SIZE = 227
APE_FOOTER_SIZE = 32
APE_PREAMBLE = b"APETAGEX"
APE_FLAG_CONTAINS_HEADER = 0x80000000
APE_FLAG_IS_HEADER = 0x20000000
PIPE_CHUNK_BYTES = 65536


def is_flac(item_format: str | None, path: str) -> bool:
    """Return True if the file is a FLAC audio file."""
    fmt = (item_format or "").strip().upper()
    return fmt == "FLAC" or os.path.splitext(path)[1].lower() == ".flac"


def _detect_id3v1_at(fh: Any, end: int) -> tuple[int, str] | None:
    """Detect an ID3v1 tag (and optional preceding TAG+ enhanced block) ending at ``end``."""
    if end < ID3V1_SIZE:
        return None
    fh.seek(end - ID3V1_SIZE)
    if fh.read(3) != b"TAG":
        return None
    new_end = end - ID3V1_SIZE
    if new_end >= ID3V1_ENHANCED_SIZE:
        fh.seek(new_end - ID3V1_ENHANCED_SIZE)
        if fh.read(4) == b"TAG+":
            return new_end - ID3V1_ENHANCED_SIZE, "id3v1+"
    return new_end, "id3v1"


def _detect_ape_at(fh: Any, end: int) -> tuple[int, str] | None:
    """Detect an APEv1/APEv2 tag footer ending at ``end``."""
    if end < APE_FOOTER_SIZE:
        return None
    fh.seek(end - APE_FOOTER_SIZE)
    footer = fh.read(APE_FOOTER_SIZE)
    if len(footer) != APE_FOOTER_SIZE or footer[:8] != APE_PREAMBLE:
        return None
    version, tag_size, _item_count, flags = struct.unpack("<IIII", footer[8:24])
    reserved = footer[24:32]
    if version not in (1000, 2000):
        return None
    if tag_size < APE_FOOTER_SIZE:
        return None
    if flags & APE_FLAG_IS_HEADER:
        return None
    if reserved != b"\x00" * 8:
        return None
    has_header = version == 2000 and bool(flags & APE_FLAG_CONTAINS_HEADER)
    total_size = tag_size + (APE_FOOTER_SIZE if has_header else 0)
    new_end = end - total_size
    if new_end <= 0:
        return None
    label = "apev2" if version == 2000 else "apev1"
    return new_end, label


def detect_trailing_tag(path: str) -> tuple[int, str] | None:
    """Detect non-FLAC trailing metadata tags (ID3v1 / TAG+ / APEv1 / APEv2).

    Returns ``(audio_end_offset, tag_label)`` where ``audio_end_offset`` is the
    byte length of the file before the trailing tag(s), or ``None`` if no valid
    trailing tag is found (or the file cannot be read). Never modifies the file.
    """
    try:
        with open(path, "rb") as fh:
            file_size = os.fstat(fh.fileno()).st_size
            if file_size <= APE_FOOTER_SIZE:
                return None

            end = file_size
            tags_rev: list[str] = []
            seen_id3v1 = False
            seen_ape = False

            while end > 0:
                if not seen_id3v1:
                    hit = _detect_id3v1_at(fh, end)
                    if hit is not None:
                        end, label = hit
                        tags_rev.append(label)
                        seen_id3v1 = True
                        continue
                if not seen_ape:
                    hit = _detect_ape_at(fh, end)
                    if hit is not None:
                        end, label = hit
                        tags_rev.append(label)
                        seen_ape = True
                        continue
                break
    except OSError:
        return None

    if not tags_rev or end < 4:
        return None
    return end, "+".join(reversed(tags_rev))


def build_ffmpeg_cmd(
    ffmpeg_bin: str,
    path: str,
    item_format: str | None = None,
    *,
    input_format: str | None = None,
) -> list[str]:
    """The decode command for one file (see docs/decode-gate.md)."""
    cmd = [ffmpeg_bin, "-nostdin", "-v", "error", "-progress", "pipe:1"]
    if is_flac(item_format, path):
        cmd += ["-err_detect", "crccheck"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += ["-i", path, "-vn", "-sn", "-dn", "-f", "null", "-"]
    return cmd


def ffmpeg_self_test(ffmpeg_bin: str = "ffmpeg") -> str | None:
    """Decode 1 s of generated silence with the same flags as a real check.

    Returns ``None`` if the checker works, else a short error string. Catches a
    binary that starts but is broken (wrong program, missing libraries, an
    ffmpeg build that rejects an option), which would otherwise make every
    good file look like a ``decode_error`` and get innocent peers blocked.
    """
    cmd = [
        ffmpeg_bin, "-nostdin", "-v", "error", "-progress", "pipe:1",
        "-err_detect", "crccheck",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1",
        "-vn", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cannot execute {ffmpeg_bin}: {exc}"[:MAX_DETAIL_CHARS]
    err = (proc.stderr or "").strip()
    decoded = parse_progress_out_time_s(proc.stdout)
    if proc.returncode != 0 or err or decoded is None or decoded < 0.5:
        return (
            f"ffmpeg self-test failed (rc={proc.returncode}, "
            f"decoded={decoded}): {err or 'no output'}"
        )[:MAX_DETAIL_CHARS]
    return None


def _run_ffmpeg_piped_prefix(
    path: str,
    limit_bytes: int,
    header_s: float | None,
    item_format: str | None,
    *,
    ffmpeg_bin: str,
    timeout: float,
    min_check_s: float,
    trunc_abs_s: float,
    trunc_frac: float,
) -> CheckResult | None:
    """Stream the first ``limit_bytes`` of ``path`` to ffmpeg on stdin.

    Returns ``None`` if the source file could not be read (caller falls back to
    the first decode result so vanished-file detection still triggers). Keeps
    the same timeout and ``checker_unavailable`` semantics as direct decode.
    """
    h_rounded = _round_seconds(header_s)
    cmd = build_ffmpeg_cmd(
        ffmpeg_bin, "pipe:0", item_format or "FLAC", input_format="flac"
    )

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_lower_priority,
        )
    except OSError as exc:
        return CheckResult(
            ok=False,
            reason="checker_unavailable",
            detail=f"cannot execute {ffmpeg_bin}: {exc}"[:MAX_DETAIL_CHARS],
            header_s=h_rounded,
            decoded_s=None,
        )

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    stop_event = threading.Event()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    read_error: list[OSError] = []

    def _feed() -> None:
        try:
            with open(path, "rb") as fh:
                remaining = limit_bytes
                while remaining > 0 and not stop_event.is_set():
                    chunk = fh.read(min(PIPE_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except (BrokenPipeError, OSError):
                        break
                    remaining -= len(chunk)
        except OSError as exc:
            read_error.append(exc)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _drain(stream: Any, buf: list[bytes]) -> None:
        try:
            while True:
                data = stream.read(PIPE_CHUNK_BYTES)
                if not data:
                    break
                buf.append(data)
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    t_feed = threading.Thread(target=_feed, daemon=True)
    t_out = threading.Thread(
        target=_drain, args=(proc.stdout, stdout_chunks), daemon=True
    )
    t_err = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_chunks), daemon=True
    )
    t_feed.start()
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_event.set()
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.SubprocessError:
            pass
        t_feed.join(timeout=5)
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        partial_stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        partial_decoded = parse_progress_out_time_s(partial_stdout)
        return CheckResult(
            ok=False,
            reason="decode_timeout",
            detail=f"ffmpeg timed out after {timeout:.0f}s"[:MAX_DETAIL_CHARS],
            header_s=h_rounded,
            decoded_s=_round_seconds(partial_decoded),
        )

    stop_event.set()
    t_feed.join(timeout=5)
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout_str = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_str = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    rc = proc.returncode
    if rc in CHECKER_FAILURE_RCS or rc in CHECKER_FAILURE_SIGNALS:
        err = stderr_str.strip()
        return CheckResult(
            ok=False,
            reason="checker_unavailable",
            detail=(
                f"{ffmpeg_bin} exited with status {rc}"
                + (f": {err}" if err else "")
            )[:MAX_DETAIL_CHARS],
            header_s=h_rounded,
            decoded_s=None,
        )

    if read_error:
        return None

    return evaluate_decode(
        path=path,
        header_s=header_s,
        item_format=item_format,
        returncode=rc,
        stdout=stdout_str,
        stderr=stderr_str,
        min_check_s=min_check_s,
        trunc_abs_s=trunc_abs_s,
        trunc_frac=trunc_frac,
    )


def run_ffmpeg_decode(
    path: str,
    header_s: float | None,
    item_format: str | None,
    *,
    ffmpeg_bin: str = "ffmpeg",
    min_check_s: float = 15.0,
    trunc_abs_s: float = 5.0,
    trunc_frac: float = 0.05,
) -> CheckResult:
    """Run a full audio decode via ffmpeg and evaluate the result.

    Uses ``-vn -sn -dn`` so embedded album art (attached_pic video streams)
    cannot distort ``out_time_us`` (ffmpeg 8.x reports ~11us for a 1-frame
    attached_pic stream when video isn't disabled).

    FLAC gets ``-err_detect crccheck``: without it ffmpeg ignores FLAC frame
    CRCs, so bit flips inside audio data with intact framing decode "clean".
    It is not used for other formats (MP3 CRCs are optional and unreliable).

    If a FLAC fails with ``decode_error`` and has a trailing non-FLAC metadata
    tag (ID3v1, ID3v1 enhanced ``TAG+``, or APEv1/APEv2 footer), re-runs the
    identical check streaming only the bytes before the trailer to ``pipe:0``.
    """
    timeout = compute_timeout(header_s)
    h_rounded = _round_seconds(header_s)
    cmd = build_ffmpeg_cmd(ffmpeg_bin, path, item_format)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            preexec_fn=_lower_priority,
        )
    except subprocess.TimeoutExpired as exc:
        partial_decoded = parse_progress_out_time_s(exc.stdout)
        return CheckResult(
            ok=False,
            reason="decode_timeout",
            detail=f"ffmpeg timed out after {timeout:.0f}s"[:MAX_DETAIL_CHARS],
            header_s=h_rounded,
            decoded_s=_round_seconds(partial_decoded),
        )
    except OSError as exc:
        return CheckResult(
            ok=False,
            reason="checker_unavailable",
            detail=f"cannot execute {ffmpeg_bin}: {exc}"[:MAX_DETAIL_CHARS],
            header_s=h_rounded,
            decoded_s=None,
        )

    rc = proc.returncode
    if rc in CHECKER_FAILURE_RCS or rc in CHECKER_FAILURE_SIGNALS:
        err = (proc.stderr or "").strip()
        return CheckResult(
            ok=False,
            reason="checker_unavailable",
            detail=(
                f"{ffmpeg_bin} exited with status {rc}"
                + (f": {err}" if err else "")
            )[:MAX_DETAIL_CHARS],
            header_s=h_rounded,
            decoded_s=None,
        )

    res = evaluate_decode(
        path=path,
        header_s=header_s,
        item_format=item_format,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        min_check_s=min_check_s,
        trunc_abs_s=trunc_abs_s,
        trunc_frac=trunc_frac,
    )

    if not res.ok and res.reason == "decode_error" and is_flac(item_format, path):
        trailer = detect_trailing_tag(path)
        if trailer is not None:
            audio_end, tag_kind = trailer
            retry = _run_ffmpeg_piped_prefix(
                path=path,
                limit_bytes=audio_end,
                header_s=header_s,
                item_format=item_format,
                ffmpeg_bin=ffmpeg_bin,
                timeout=timeout,
                min_check_s=min_check_s,
                trunc_abs_s=trunc_abs_s,
                trunc_frac=trunc_frac,
            )
            if retry is not None:
                if retry.ok:
                    retry.trailing_tag = tag_kind
                return retry

    return res


def make_task_id(now: datetime.datetime | None = None) -> str:
    """Generate a unique task identifier like ``20261124T163000-ab12cd``."""
    dt = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{dt.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


def _is_within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def build_quarantine_dest(
    src_path: str,
    state_dir: str,
    date_str: str,
    task_id: str,
    toppath: str | None = None,
) -> str:
    """Compute the quarantine destination path preserving relative structure.

    When ``src_path`` lives under ``dirname(state_dir)`` (e.g. ``/downloads``
    when ``state_dir`` is ``/downloads/.decodecheck``), the relative path is
    relative to that staging root, yielding e.g.::

        /downloads/.decodecheck/quarantine/2026-11-24/<task_id>/Some Dir/04 Track.flac
    """
    src_abs = os.path.abspath(src_path)
    state_abs = os.path.abspath(state_dir)
    staging_root = os.path.dirname(state_abs)

    rel: str | None = None
    if staging_root and src_abs != staging_root and _is_within(src_abs, staging_root):
        rel = os.path.relpath(src_abs, staging_root)
    elif toppath:
        top_abs = os.path.abspath(toppath)
        if os.path.isdir(top_abs) and _is_within(src_abs, top_abs):
            top_name = os.path.basename(top_abs.rstrip(os.sep))
            inner = os.path.relpath(src_abs, top_abs)
            rel = os.path.join(top_name, inner) if top_name else inner

    if not rel:
        parent_name = os.path.basename(os.path.dirname(src_abs).rstrip(os.sep))
        base = os.path.basename(src_abs)
        rel = os.path.join(parent_name, base) if parent_name else base

    parts = [
        part
        for part in rel.replace("\\", "/").split("/")
        if part and part not in (".", "..")
    ]
    if not parts:
        parts = [os.path.basename(src_abs) or "file"]

    return os.path.join(state_abs, "quarantine", date_str, task_id, *parts)


def collect_task_files(
    task_paths: list[str],
    item_paths: list[str],
    state_dir: str,
) -> list[str]:
    """Collect all audio and non-audio files belonging to an import task.

    Never touches anything inside ``state_dir`` and avoids pulling in unrelated
    sibling/nested album directories that contain other audio files.
    """
    state_real = os.path.realpath(state_dir)
    item_abs_set = {os.path.abspath(p) for p in item_paths}
    task_dir_set = {
        os.path.abspath(p) for p in task_paths if os.path.isdir(p)
    }

    collected: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        ap = os.path.abspath(p)
        if ap in seen:
            return
        if _is_within(os.path.realpath(ap), state_real):
            return
        if os.path.lexists(ap) and not os.path.isdir(ap):
            seen.add(ap)
            collected.append(ap)

    for ip in item_paths:
        _add(ip)

    for tp in task_paths:
        tp_abs = os.path.abspath(tp)
        if _is_within(os.path.realpath(tp_abs), state_real):
            continue
        if os.path.isfile(tp_abs) or os.path.islink(tp_abs):
            _add(tp_abs)
            continue
        if not os.path.isdir(tp_abs):
            continue

        try:
            entries = sorted(os.scandir(tp_abs), key=lambda e: e.name)
        except OSError:
            continue

        for entry in entries:
            entry_abs = os.path.abspath(entry.path)
            entry_real = os.path.realpath(entry_abs)
            if _is_within(entry_real, state_real) or _is_within(
                state_real, entry_real
            ):
                # Never touch state_dir or any ancestor of state_dir.
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                _add(entry_abs)
            else:
                if entry_abs in task_dir_set:
                    # Handled directly by outer task_paths loop.
                    continue
                # Include auxiliary subdirectories (e.g. Scans/, Artwork/) only
                # if they contain no audio files outside this task's items.
                sub_files: list[str] = []
                has_foreign_audio = False
                for root, dirs, files in os.walk(entry_abs):
                    # Prune state_dir if somehow nested inside.
                    dirs[:] = [
                        d
                        for d in dirs
                        if not _is_within(
                            os.path.realpath(os.path.join(root, d)), state_real
                        )
                    ]
                    for fn in files:
                        fp = os.path.abspath(os.path.join(root, fn))
                        ext = os.path.splitext(fn)[1].lower()
                        if ext in AUDIO_EXTS and fp not in item_abs_set:
                            has_foreign_audio = True
                            break
                        sub_files.append(fp)
                    if has_foreign_audio:
                        break
                if not has_foreign_audio:
                    for fp in sub_files:
                        _add(fp)

    return collected


def _unique_dest(dest: str) -> str:
    if not os.path.lexists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    idx = 1
    while True:
        cand = f"{base}.{idx}{ext}"
        if not os.path.lexists(cand):
            return cand
        idx += 1


def quarantine_task_files(
    files: list[str],
    task_paths: list[str],
    state_dir: str,
    date_str: str,
    task_id: str,
    toppath: str | None = None,
) -> dict[str, str]:
    """Move all files of a rejected task into quarantine and prune empty dirs.

    Returns a mapping of ``{abspath(src): dest_path}`` for files successfully
    moved.
    """
    state_abs = os.path.abspath(state_dir)
    staging_root = os.path.dirname(state_abs)
    protected = {os.sep, state_abs}
    if staging_root:
        protected.add(staging_root)

    moved: dict[str, str] = {}
    candidate_dirs: set[str] = set()

    for src in files:
        src_abs = os.path.abspath(src)
        if not os.path.lexists(src_abs):
            continue
        dest = _unique_dest(
            build_quarantine_dest(
                src_abs, state_abs, date_str, task_id, toppath=toppath
            )
        )
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src_abs, dest)
        moved[src_abs] = dest
        candidate_dirs.add(os.path.dirname(src_abs))

    for tp in task_paths:
        tp_abs = os.path.abspath(tp)
        if os.path.isdir(tp_abs):
            candidate_dirs.add(tp_abs)

    # Remove empty directories bottom-up, stopping before protected roots.
    for d in sorted(
        candidate_dirs, key=lambda p: len(p.split(os.sep)), reverse=True
    ):
        cur = d
        while cur and cur not in protected:
            try:
                os.rmdir(cur)
            except OSError:
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

    return moved


def extract_item_meta(item: Any) -> dict[str, Any]:
    """Extract tag metadata from a beets Item (or duck-typed stub) for rejects.jsonl."""

    def _get(field: str) -> Any:
        try:
            val = getattr(item, field)
        except Exception:
            val = None
        if val is None and hasattr(item, "get"):
            try:
                val = item.get(field)
            except Exception:
                val = None
        return val

    def _str_or_none(field: str) -> str | None:
        val = _get(field)
        if val is None:
            return None
        s = str(val).strip()
        return s if s else None

    track_raw = _get("track")
    track_val: int | None = None
    if track_raw is not None:
        try:
            t = int(track_raw)
            if t > 0:
                track_val = t
        except (TypeError, ValueError):
            track_val = None

    return {
        "artist": _str_or_none("artist"),
        "album": _str_or_none("album"),
        "title": _str_or_none("title"),
        "albumartist": _str_or_none("albumartist"),
        "track": track_val,
    }


def build_reject_record(
    *,
    ts: int,
    task_id: str,
    result: CheckResult,
    staging_path: str,
    quarantined_to: str | None,
    item_meta: dict[str, Any],
    task_files: int,
    task_bad: int,
    album_mode: bool,
) -> dict[str, Any]:
    """Build one ``rejects.jsonl`` object matching the docs/decode-gate.md contract."""
    return {
        "v": CONTRACT_VERSION,
        "ts": int(ts),
        "task_id": task_id,
        "reason": result.reason,
        "detail": (result.detail or "")[:MAX_DETAIL_CHARS],
        "header_s": result.header_s,
        "decoded_s": result.decoded_s,
        "staging_path": staging_path,
        "basename": os.path.basename(staging_path),
        "quarantined_to": quarantined_to,
        "artist": item_meta.get("artist"),
        "album": item_meta.get("album"),
        "title": item_meta.get("title"),
        "albumartist": item_meta.get("albumartist"),
        "track": item_meta.get("track"),
        "task_files": int(task_files),
        "task_bad": int(task_bad),
        "album_mode": bool(album_mode),
    }


def append_jsonl_locked(path: str, records: list[dict[str, Any]]) -> None:
    """Append JSONL records to ``path`` under an exclusive ``fcntl.flock``."""
    if not records:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = "".join(
        json.dumps(rec, ensure_ascii=False) + "\n" for rec in records
    )
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0, os.SEEK_END)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def prune_quarantine(
    state_dir: str,
    quarantine_days: int,
    *,
    now_ts: float | None = None,
    force: bool = False,
) -> list[str]:
    """Remove quarantine dated folders older than ``quarantine_days``.

    Rate-limited to at most once per ``PRUNE_INTERVAL_S`` (1 hour) via
    ``<state_dir>/.last_prune`` mtime unless ``force=True``.
    Returns the list of removed directory paths.
    """
    now = time.time() if now_ts is None else float(now_ts)
    state_abs = os.path.abspath(state_dir)
    stamp_path = os.path.join(state_abs, ".last_prune")

    if not force:
        try:
            mtime = os.path.getmtime(stamp_path)
            if now - mtime < PRUNE_INTERVAL_S:
                return []
        except OSError:
            pass

    removed: list[str] = []
    quarantine_root = os.path.join(state_abs, "quarantine")

    if quarantine_days > 0 and os.path.isdir(quarantine_root):
        today = datetime.datetime.fromtimestamp(
            now, tz=datetime.timezone.utc
        ).date()
        cutoff = today - datetime.timedelta(days=quarantine_days)
        try:
            entries = sorted(os.scandir(quarantine_root), key=lambda e: e.name)
        except OSError:
            entries = []

        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not DATE_DIR_RE.match(entry.name):
                continue
            try:
                dir_date = datetime.date.fromisoformat(entry.name)
            except ValueError:
                continue
            if dir_date < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                removed.append(entry.path)

    os.makedirs(state_abs, exist_ok=True)
    try:
        with open(stamp_path, "a", encoding="utf-8"):
            pass
        os.utime(stamp_path, (now, now))
    except OSError:
        pass

    return removed


def _is_passthrough_task(task: Any) -> bool:
    """Return True for sentinel or archive tasks that must pass through untouched."""
    if SentinelImportTask is not None and isinstance(task, SentinelImportTask):
        return True
    if ArchiveImportTask is not None and isinstance(task, ArchiveImportTask):
        return True
    # Defensive duck-typing for stubbed tests or future subclasses.
    cls_name = type(task).__name__
    if cls_name in ("SentinelImportTask", "ArchiveImportTask"):
        return True
    # ``skip`` is a property on real beets tasks (True for sentinels); only an
    # explicit True counts, so a bound method or stub attribute never matches.
    if getattr(task, "skip", False) is True:
        return True
    return False


def _extract_task_items(task: Any) -> tuple[list[Any], bool]:
    """Return ``(items, album_mode)`` for an ImportTask or SingletonImportTask."""
    is_singleton = (
        SingletonImportTask is not None
        and isinstance(task, SingletonImportTask)
    ) or (
        hasattr(task, "item")
        and getattr(task, "item", None) is not None
        and not getattr(task, "is_album", True)
    )

    if is_singleton:
        item = getattr(task, "item", None)
        return ([item] if item is not None else []), False

    items = list(getattr(task, "items", None) or [])
    album_mode = bool(getattr(task, "is_album", True))
    return items, album_mode


def _find_unreadable_album_audio(
    task_paths: list[str],
    item_paths: list[str],
    state_dir: str,
) -> list[str]:
    """Find non-hidden audio files directly inside album dirs missing from task.items.

    Beets' ``ImportTaskFactory.read_item`` silently drops files that mutagen
    cannot parse at all (e.g. 0-byte or header-corrupted FLACs). Checking them
    here prevents an incomplete album from slipping past the gate.
    """
    state_real = os.path.realpath(state_dir)
    known = {os.path.abspath(p) for p in item_paths}
    extras: list[str] = []

    for tp in task_paths:
        if not os.path.isdir(tp):
            continue
        try:
            entries = sorted(os.scandir(tp), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            ap = os.path.abspath(entry.path)
            if ap in known:
                continue
            if _is_within(os.path.realpath(ap), state_real):
                continue
            extras.append(ap)

    return extras


class DecodeCheckPlugin(BeetsPlugin):
    """Reject corrupt, truncated, or duration-less audio at import time."""

    def __init__(self) -> None:
        super().__init__()
        self.config.add(
            {
                "state_dir": "/downloads/.decodecheck",
                "quarantine_days": 14,
                "ffmpeg": "ffmpeg",
                "min_check_s": 15,
                "trunc_abs_s": 5,
                "trunc_frac": 0.05,
                "enabled": True,
            }
        )
        self._last_prune_ts: float = 0.0
        # ffmpeg binary that passed ffmpeg_self_test() in this process.
        self._self_tested_bin: str | None = None
        self.register_listener(
            "import_task_created", self.on_import_task_created
        )

    def _cfg_bool(self, key: str) -> bool:
        return bool(self.config[key].get(bool))

    def _cfg_str(self, key: str) -> str:
        return str(self.config[key].as_str())

    def _cfg_int(self, key: str) -> int:
        return int(self.config[key].get(int))

    def _cfg_float(self, key: str) -> float:
        return float(self.config[key].get(float))

    def _maybe_prune(self, state_dir: str, quarantine_days: int) -> None:
        now = time.time()
        if now - self._last_prune_ts < PRUNE_INTERVAL_S:
            return
        try:
            removed = prune_quarantine(state_dir, quarantine_days, now_ts=now)
            self._last_prune_ts = now
            if removed:
                self._log.info(
                    "pruned {} expired quarantine folder(s)", len(removed)
                )
        except Exception as exc:
            self._log.warning("quarantine prune failed: {}", exc)

    def on_import_task_created(self, session: Any, task: Any) -> list[Any]:
        """Validate every audio file in ``task``; drop & quarantine on any failure.

        beets 2.11 ``ImportTask.handle_created`` flattens the lists returned by
        all ``import_task_created`` listeners; returning ``[]`` drops the task,
        while ``None`` (or no listener result at all) keeps it.
        """
        items: list[Any] = []
        album_mode = True
        try:
            if not self._cfg_bool("enabled"):
                return [task]
            if _is_passthrough_task(task):
                return [task]
            items, album_mode = _extract_task_items(task)
            if not items:
                return [task]
            return self._check_task(task, items, album_mode)
        except Exception as exc:  # never let an exception escape the hook
            try:
                self._log.error(
                    "unexpected error checking import task ({}); rejecting task",
                    exc,
                )
                if items:
                    self._fail_closed_reject(task, items, album_mode, exc)
            except Exception:
                pass
            return []

    def _check_task(
        self, task: Any, items: list[Any], album_mode: bool
    ) -> list[Any]:
        state_dir = self._cfg_str("state_dir")
        quarantine_days = self._cfg_int("quarantine_days")
        ffmpeg_bin = self._cfg_str("ffmpeg")
        min_check_s = self._cfg_float("min_check_s")
        trunc_abs_s = self._cfg_float("trunc_abs_s")
        trunc_frac = self._cfg_float("trunc_frac")

        self._maybe_prune(state_dir, quarantine_days)

        now = datetime.datetime.now(datetime.timezone.utc)
        ts = int(now.timestamp())
        date_str = now.strftime("%Y-%m-%d")
        task_id = make_task_id(now)

        toppath = (
            _to_str_path(task.toppath)
            if getattr(task, "toppath", None) is not None
            else None
        )
        task_paths = [
            _to_str_path(p) for p in (getattr(task, "paths", None) or [])
        ]
        item_paths = [_to_str_path(it.path) for it in items]

        # Build the full list of audio targets to check: all beets Items plus
        # any unreadable audio files in album directories that mutagen skipped.
        targets: list[tuple[str, float | None, str | None, dict[str, Any]]] = []
        for item, staging_path in zip(items, item_paths):
            raw_len = getattr(item, "length", None)
            header_s: float | None = None
            if raw_len is not None:
                try:
                    header_s = float(raw_len)
                except (TypeError, ValueError):
                    header_s = None
            item_format = getattr(item, "format", None)
            targets.append(
                (staging_path, header_s, item_format, extract_item_meta(item))
            )

        if album_mode:
            for extra_path in _find_unreadable_album_audio(
                task_paths, item_paths, state_dir
            ):
                targets.append(
                    (
                        extra_path,
                        0.0,
                        None,
                        {
                            "artist": None,
                            "album": None,
                            "title": None,
                            "albumartist": None,
                            "track": None,
                        },
                    )
                )

        bad_entries: list[tuple[str, dict[str, Any], CheckResult]] = []
        magic_mismatches: list[dict[str, str]] = []
        ignored_image_warnings: list[dict[str, str]] = []
        ignored_metadata_warnings: list[dict[str, str]] = []
        trailing_tags: list[dict[str, str]] = []

        targets_to_check = targets
        if self._self_tested_bin != ffmpeg_bin:
            st_err = ffmpeg_self_test(ffmpeg_bin)
            if st_err is None:
                self._self_tested_bin = ffmpeg_bin
            else:
                self._log.error("decode checker self-test failed: {}", st_err)
                staging_path, header_s, _fmt, meta = targets[0]
                bad_entries.append(
                    (
                        staging_path,
                        meta,
                        CheckResult(
                            ok=False,
                            reason="checker_unavailable",
                            detail=st_err,
                            header_s=_round_seconds(header_s),
                            decoded_s=None,
                        ),
                    )
                )
                targets_to_check = []

        n_targets = len(targets_to_check)
        for idx, (staging_path, header_s, item_format, meta) in enumerate(
            targets_to_check, 1
        ):
            if not os.path.lexists(staging_path):
                return self._abort_vanished(task_paths, staging_path)
            # One line per file. Soulbeet kills a beets run that prints nothing
            # for 3 minutes, and a large hi-res album can take longer than that
            # to decode silently. This output keeps the watchdog fed.
            self._log.info(
                "decodecheck: {}/{} {}", idx, n_targets,
                os.path.basename(staging_path),
            )
            res = run_ffmpeg_decode(
                staging_path,
                header_s,
                item_format,
                ffmpeg_bin=ffmpeg_bin,
                min_check_s=min_check_s,
                trunc_abs_s=trunc_abs_s,
                trunc_frac=trunc_frac,
            )
            if not res.ok and not os.path.lexists(staging_path):
                # Moved away while we decoded it (another beets import of the
                # same folder): not a corrupt file.
                return self._abort_vanished(task_paths, staging_path)
            if not res.ok:
                bad_entries.append((staging_path, meta, res))
                if res.reason == "checker_unavailable":
                    self._self_tested_bin = None  # re-test before the next task
                    self._log.error(
                        "decode checker unavailable ({}): {}",
                        ffmpeg_bin,
                        res.detail,
                    )
                    break
            else:
                if res.magic_mismatch:
                    magic_mismatches.append(
                        {
                            "basename": os.path.basename(staging_path),
                            "kind": res.magic_mismatch,
                        }
                    )
                if res.trailing_tag:
                    trailing_tags.append(
                        {
                            "basename": os.path.basename(staging_path),
                            "kind": res.trailing_tag,
                        }
                    )
                if res.ignored_warnings:
                    first_img = next(
                        (
                            w
                            for w in res.ignored_warnings
                            if is_ignored_image_warning(w)
                        ),
                        None,
                    )
                    if first_img is not None:
                        ignored_image_warnings.append(
                            {
                                "basename": os.path.basename(staging_path),
                                "warning": first_img[:MAX_DETAIL_CHARS],
                            }
                        )
                    first_meta = next(
                        (
                            w
                            for w in res.ignored_warnings
                            if is_ignored_metadata_warning(w)
                        ),
                        None,
                    )
                    if first_meta is not None:
                        ignored_metadata_warnings.append(
                            {
                                "basename": os.path.basename(staging_path),
                                "warning": first_meta[:MAX_DETAIL_CHARS],
                            }
                        )

        if bad_entries:
            all_target_paths = [t[0] for t in targets]
            gone = [p for p in all_target_paths if not os.path.lexists(p)]
            if gone:
                return self._abort_vanished(task_paths, gone[0])
            files_to_move = collect_task_files(
                task_paths, all_target_paths, state_dir
            )
            moved_map = quarantine_task_files(
                files_to_move,
                task_paths,
                state_dir,
                date_str,
                task_id,
                toppath=toppath,
            )

            records = [
                build_reject_record(
                    ts=ts,
                    task_id=task_id,
                    result=res,
                    staging_path=staging_path,
                    quarantined_to=moved_map.get(os.path.abspath(staging_path)),
                    item_meta=meta,
                    task_files=len(targets),
                    task_bad=len(bad_entries),
                    album_mode=album_mode,
                )
                for staging_path, meta, res in bad_entries
            ]
            rejects_path = os.path.join(state_dir, "rejects.jsonl")
            append_jsonl_locked(rejects_path, records)

            reasons = ", ".join(
                f"{os.path.basename(sp)} ({res.reason})"
                for sp, _, res in bad_entries
            )
            q_dir = os.path.join(
                os.path.abspath(state_dir), "quarantine", date_str, task_id
            )
            self._log.warning(
                "rejected task {} ({}/{} bad, moved {} file(s) to {}): {}",
                task_id,
                len(bad_entries),
                len(targets),
                len(moved_map),
                q_dir,
                reasons,
            )
            return []

        # All files decoded clean.
        if magic_mismatches:
            self._log.info(
                "accepted task {} with cosmetic magic mismatch(es): {}",
                task_id,
                ", ".join(
                    f"{m['basename']}={m['kind']}" for m in magic_mismatches
                ),
            )
        if ignored_image_warnings:
            self._log.info(
                "accepted task {} ignoring {} non-audio ffmpeg warning(s): {}",
                task_id,
                len(ignored_image_warnings),
                ignored_image_warnings[0]["warning"],
            )
        if ignored_metadata_warnings:
            self._log.info(
                "accepted task {} ignoring {} metadata ffmpeg warning(s): {}",
                task_id,
                len(ignored_metadata_warnings),
                ignored_metadata_warnings[0]["warning"],
            )
        trailing_tag_summary: str | None = None
        if trailing_tags:
            unique_kinds = list(dict.fromkeys(t["kind"] for t in trailing_tags))
            trailing_tag_summary = (
                unique_kinds[0]
                if len(unique_kinds) == 1
                else ",".join(unique_kinds)
            )
            self._log.info(
                "accepted task {} with trailing tag(s) on {} file(s): {}",
                task_id,
                len(trailing_tags),
                trailing_tag_summary,
            )

        checked_rec: dict[str, Any] = {
            "v": CONTRACT_VERSION,
            "ts": ts,
            "task_id": task_id,
            "files": len(targets),
            "album_mode": album_mode,
            "magic_mismatches": len(magic_mismatches),
        }
        if magic_mismatches:
            checked_rec["magic_mismatch_details"] = magic_mismatches
        if ignored_image_warnings:
            checked_rec["ignored_image_warnings"] = len(ignored_image_warnings)
            checked_rec["ignored_image_warning_details"] = ignored_image_warnings
        if ignored_metadata_warnings:
            checked_rec["ignored_metadata_warnings"] = len(
                ignored_metadata_warnings
            )
            checked_rec["ignored_metadata_warning_details"] = (
                ignored_metadata_warnings
            )
        if trailing_tags and trailing_tag_summary is not None:
            checked_rec["trailing_tag"] = trailing_tag_summary
            checked_rec["trailing_tag_files"] = len(trailing_tags)
            checked_rec["trailing_tag_details"] = trailing_tags

        try:
            checked_path = os.path.join(state_dir, "checked.jsonl")
            append_jsonl_locked(checked_path, [checked_rec])
        except Exception as exc:
            self._log.warning("failed to append checked.jsonl: {}", exc)

        return [task]

    def _abort_vanished(self, task_paths: list[str], path: str) -> list[Any]:
        """Drop the task without a reject or quarantine: a file disappeared
        mid-check, so another beets process is importing this folder."""
        self._log.warning(
            "skipping task {}: {} vanished during the decode check "
            "(concurrent import); no reject written",
            ", ".join(task_paths) or "?",
            path,
        )
        return []

    def _fail_closed_reject(
        self,
        task: Any,
        items: list[Any],
        album_mode: bool,
        exc: Exception,
    ) -> None:
        """Best-effort quarantine and reject log when an unexpected error occurs."""
        try:
            state_dir = self._cfg_str("state_dir")
        except Exception:
            state_dir = "/downloads/.decodecheck"

        now = datetime.datetime.now(datetime.timezone.utc)
        ts = int(now.timestamp())
        date_str = now.strftime("%Y-%m-%d")
        task_id = make_task_id(now)

        toppath = (
            _to_str_path(task.toppath)
            if getattr(task, "toppath", None) is not None
            else None
        )
        task_paths = [
            _to_str_path(p) for p in (getattr(task, "paths", None) or [])
        ]
        item_paths = [_to_str_path(it.path) for it in items]

        moved_map: dict[str, str] = {}
        try:
            files_to_move = collect_task_files(
                task_paths, item_paths, state_dir
            )
            moved_map = quarantine_task_files(
                files_to_move,
                task_paths,
                state_dir,
                date_str,
                task_id,
                toppath=toppath,
            )
        except Exception as move_exc:
            self._log.error(
                "failed to quarantine task {} during fail-closed recovery: {}",
                task_id,
                move_exc,
            )

        try:
            first_item = items[0]
            first_path = item_paths[0]
            raw_len = getattr(first_item, "length", None)
            header_s = float(raw_len) if raw_len is not None else None
            res = CheckResult(
                ok=False,
                reason="checker_unavailable",
                detail=f"decodecheck plugin error: {exc}"[:MAX_DETAIL_CHARS],
                header_s=_round_seconds(header_s),
                decoded_s=None,
            )
            rec = build_reject_record(
                ts=ts,
                task_id=task_id,
                result=res,
                staging_path=first_path,
                quarantined_to=moved_map.get(os.path.abspath(first_path)),
                item_meta=extract_item_meta(first_item),
                task_files=len(items),
                task_bad=1,
                album_mode=album_mode,
            )
            append_jsonl_locked(os.path.join(state_dir, "rejects.jsonl"), [rec])
        except Exception as log_exc:
            self._log.error(
                "failed to write rejects.jsonl during fail-closed recovery: {}",
                log_exc,
            )
