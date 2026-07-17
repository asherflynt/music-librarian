#!/usr/bin/env python3
"""LAN web UI for the librarian.

stdlib http.server on purpose: this container's only dependencies are `requests` and
`mutagen`, and a web framework would be by far the largest thing in the image for a
handful of endpoints serving one page.

Security posture: LAN-only, no auth, by explicit choice -- consistent with the slskd /
Soulbeet / Navidrome services already on this network. This UI can queue downloads and
(via the upgrade scanner) cause files to be deleted, so it must NOT be exposed through
the Cloudflared tunnel. There is deliberately no static-file handler and no path
routing to disk: the page is a constant below, so there is nothing to traverse to.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Every knob the UI is allowed to write, with its type and bounds. A POST touching
# anything not in here is rejected -- this is the write boundary to a file the
# librarian re-reads and acts on, so it is an allowlist rather than a denylist.
#   name: (kind, lo, hi)
SETTINGS = {
    "TARGET_TB":                ("float", 0.0, 500.0),
    "TARGET_TRACKS":            ("int", 0, 100_000_000),
    "EXPLORE_RATIO":            ("float", 0.0, 1.0),
    "MIN_FREE_GB":              ("float", 0.0, 100_000.0),
    "STAGING_MIN_FREE_GB":      ("float", 0.0, 100_000.0),
    "CONCURRENCY":              ("int", 1, 16),
    "TASTE_REFRESH_MIN":        ("int", 5, 100_000),
    "MEASURE_EVERY_SEC":        ("int", 30, 86_400),
    "PAUSED":                   ("bool", 0, 1),
    "TASTE_HALF_LIFE_DAYS":     ("float", 1.0, 36_500.0),
    "WEIGHT_NAVIDROME":         ("float", 0.0, 100.0),
    "WEIGHT_PLEX":              ("float", 0.0, 100.0),
    "WEIGHT_YTMUSIC":           ("float", 0.0, 100.0),
    "FAVORITES_ENABLED":        ("bool", 0, 1),
    "FAVORITE_SYNC_HOURS":      ("int", 1, 8760),
    "FAVORITE_PRIORITY":        ("int", 0, 10_000),
    "FAVORITE_INCLUDE_EP":      ("bool", 0, 1),
    "FAVORITE_INCLUDE_SINGLES": ("bool", 0, 1),
    "FAVORITE_INCLUDE_LIVE":    ("bool", 0, 1),
    "FAVORITE_INCLUDE_COMPILATIONS": ("bool", 0, 1),
    "FAVORITE_INCLUDE_REMIX":   ("bool", 0, 1),
    "UPGRADE_ENABLED":          ("bool", 0, 1),
    "UPGRADE_HOUR":             ("int", 0, 23),
    "UPGRADE_MAX_PER_RUN":      ("int", 1, 1000),
    "UPGRADE_RECHECK_DAYS":     ("int", 1, 3650),
    "UPGRADE_ONLY_WHEN_IDLE":   ("bool", 0, 1),
}

CTX = {}
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# env-file read/write (comment- and order-preserving)
# ---------------------------------------------------------------------------
_KV = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


def read_env(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if line.strip().startswith("#"):
            continue
        m = _KV.match(line)
        if m:
            out[m.group(2)] = m.group(4).split("#")[0].strip()
    return out


def write_env(path, updates):
    """Rewrite matching KEY= lines in place; append anything new.

    config.env's comments ARE its documentation -- they explain what each knob does
    and are the only such docs on the server. A naive dump of key=value would erase
    them, so existing lines are edited in place and everything else is passed through
    untouched. Written via temp+rename so the librarian, which re-reads this file
    every loop, can never observe a half-written config.
    """
    with _write_lock:
        lines = path.read_text().splitlines() if path.exists() else []
        seen = set()
        out = []
        for line in lines:
            m = _KV.match(line)
            if m and m.group(2) in updates and not line.strip().startswith("#"):
                key = m.group(2)
                trailing = ""
                # keep any inline comment that documents the value
                if "#" in m.group(4):
                    trailing = "   # " + m.group(4).split("#", 1)[1].strip()
                out.append(f"{m.group(1)}{key}={updates[key]}{trailing}")
                seen.add(key)
            else:
                out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(out) + "\n")
        os.replace(tmp, path)


def write_lines(path, names):
    with _write_lock:
        header = ""
        if path.exists():
            keep = []
            for line in path.read_text().splitlines():
                if line.strip().startswith("#") or not line.strip():
                    keep.append(line)
                else:
                    break        # header block only: stop at the first real entry
            header = "\n".join(keep).rstrip() + "\n\n" if keep else ""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(header + "\n".join(sorted(names, key=str.lower)) + "\n")
        os.replace(tmp, path)


def read_lines(path):
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#")]


def coerce(key, raw):
    """Validate + clamp one setting. Raises ValueError on anything unacceptable."""
    if key not in SETTINGS:
        raise ValueError(f"unknown setting {key!r}")
    kind, lo, hi = SETTINGS[key]
    if kind == "bool":
        if isinstance(raw, bool):
            return "1" if raw else "0"
        return "1" if str(raw).strip().lower() in ("1", "true", "yes", "on") else "0"
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: {raw!r} is not a number")
    if v != v:                       # NaN
        raise ValueError(f"{key}: not a number")
    v = max(lo, min(hi, v))          # clamp rather than reject: the UI is a slider,
                                     # not a config parser, and a clamp is predictable
    return str(int(v)) if kind == "int" else str(v)


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "librarian"

    def log_message(self, fmt, *args):
        pass                          # don't spam the librarian's log with hits

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                      # browser navigated away mid-poll

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            return {}

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        st = CTX["state"]
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")

            if u.path == "/api/status":
                # status.json is produced by the main loop's measurement pass; the
                # UI reads that rather than re-walking the library on every poll.
                try:
                    data = json.loads(CTX["status_json"].read_text())
                except (OSError, ValueError):
                    data = {"updated": None, "note": "no measurement yet"}
                data["config"] = {k: read_env(CTX["config_env"]).get(k, "")
                                  for k in SETTINGS}
                return self._json(data)

            if u.path == "/api/config":
                cur = read_env(CTX["config_env"])
                return self._json({k: cur.get(k, "") for k in SETTINGS})

            if u.path == "/api/exclude":
                return self._json({"names": read_lines(CTX["exclude_txt"])})

            if u.path == "/api/favorites":
                return self._json({"names": read_lines(CTX["favorites_txt"])})

            if u.path == "/api/artists":
                limit = min(500, int(q.get("limit", ["60"])[0]))
                excl = {n.lower() for n in read_lines(CTX["exclude_txt"])}
                favs = {n.lower() for n in read_lines(CTX["favorites_txt"])}
                return self._json({"artists": [
                    {"artist": a, "weight": round(w or 0, 2),
                     "expanded": bool(e),
                     "excluded": (a or "").lower() in excl,
                     "favorite": (a or "").lower() in favs}
                    for (a, w, e) in st.top_artists(limit)]})

            if u.path == "/api/log":
                n = min(500, int(q.get("n", ["120"])[0]))
                # Already scrubbed: log() runs scrub_secrets() before anything
                # reaches the ring buffer, so no key can be here to leak.
                return self._json({"lines": list(CTX["log_ring"])[-n:]})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        st = CTX["state"]
        try:
            if u.path == "/api/config":
                body = self._body()
                updates, errors = {}, []
                for k, v in body.items():
                    try:
                        updates[k] = coerce(k, v)
                    except ValueError as e:
                        errors.append(str(e))
                if errors:
                    return self._json({"error": "; ".join(errors)}, 400)
                if updates:
                    write_env(CTX["config_env"], updates)
                return self._json({"ok": True, "written": updates,
                                   "note": "applies on the next loop (~1 min)"})

            if u.path in ("/api/pause", "/api/resume"):
                write_env(CTX["config_env"],
                          {"PAUSED": "1" if u.path.endswith("pause") else "0"})
                return self._json({"ok": True})

            if u.path == "/api/exclude":
                names = [n.strip() for n in self._body().get("names", []) if n.strip()]
                write_lines(CTX["exclude_txt"], set(names))
                return self._json({"ok": True, "count": len(set(names))})

            if u.path == "/api/favorites":
                names = [n.strip() for n in self._body().get("names", []) if n.strip()]
                write_lines(CTX["favorites_txt"], set(names))
                # Make the next loop pick it up now rather than up to
                # FAVORITE_SYNC_HOURS later -- adding a favorite should feel immediate.
                st.set_meta("last_favorites_sync", 0)
                return self._json({"ok": True, "count": len(set(names))})

            if u.path == "/api/refresh-taste":
                st.set_meta("last_taste_refresh", 0)
                return self._json({"ok": True})

            if u.path == "/api/sync-favorites":
                st.set_meta("last_favorites_sync", 0)
                return self._json({"ok": True})

            if u.path == "/api/scan-upgrades":
                if not CTX["cfg"]()["UPGRADE_ENABLED"]:
                    return self._json(
                        {"error": "upgrades are disabled — enable UPGRADE_ENABLED first"}, 400)
                st.set_meta("force_upgrade_pass", 1)
                return self._json({"ok": True, "note": "pass starts within ~5 min"})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def serve(port, ctx):
    CTX.update(ctx)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Web UI on http://0.0.0.0:{port} "
          f"(LAN only — do not expose publicly)", flush=True)
    return srv


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Music Librarian</title>
<style>
:root{
  --bg:#0f1115; --card:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#98a0b3;
  --acc:#5aa9e6; --ok:#4ec9a0; --warn:#e6b45a; --bad:#e66a6a; --fav:#c58af9;
}
@media (prefers-color-scheme:light){
  :root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ec;--fg:#1a1d24;--dim:#5d6577;--acc:#1f6feb}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;
  align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
.wrap{padding:20px;max-width:1180px;margin:0 auto}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(290px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-size:12px;margin:0 0 12px;color:var(--dim);text-transform:uppercase;
  letter-spacing:.9px;font-weight:600}
.big{font-size:26px;font-weight:650;letter-spacing:-.4px}
.sub{color:var(--dim);font-size:12px}
.bar{height:7px;background:var(--line);border-radius:4px;overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;background:var(--acc);transition:width .4s}
.row{display:flex;justify-content:space-between;gap:10px;padding:4px 0}
.row+.row{border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.scroll{max-height:290px;overflow:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
  background:var(--line);color:var(--dim)}
.pill.ok{background:rgba(78,201,160,.16);color:var(--ok)}
.pill.warn{background:rgba(230,180,90,.16);color:var(--warn)}
.pill.bad{background:rgba(230,106,106,.16);color:var(--bad)}
.pill.fav{background:rgba(197,138,249,.16);color:var(--fav)}
button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:7px 13px;
  font-size:13px;font-weight:550;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button:disabled{opacity:.45;cursor:not-allowed}
button:active{transform:translateY(1px)}
input,select{background:var(--bg);border:1px solid var(--line);color:var(--fg);
  border-radius:6px;padding:5px 8px;font-size:13px;width:100%}
label{display:block;font-size:12px;color:var(--dim);margin:9px 0 3px}
.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:0 14px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:10px;
  overflow:auto;max-height:320px;font-size:11.5px;line-height:1.55;margin:0}
.banner{padding:9px 13px;border-radius:8px;margin-bottom:14px;font-size:13px;display:none}
.banner.on{display:block}
.banner.stop{background:rgba(230,180,90,.14);border:1px solid rgba(230,180,90,.4)}
.banner.pause{background:rgba(230,106,106,.14);border:1px solid rgba(230,106,106,.4)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:flex;align-items:center;gap:5px;background:var(--bg);
  border:1px solid var(--line);border-radius:99px;padding:2px 5px 2px 10px;font-size:12px}
.chip b{font-weight:500}
.chip button{background:none;color:var(--dim);padding:0 4px;font-size:14px;line-height:1}
.chip button:hover{color:var(--bad)}
.add{display:flex;gap:6px;margin-top:9px}
.muted{color:var(--dim);font-size:12px;padding:8px 0}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--card);
  border:1px solid var(--line);border-radius:8px;padding:9px 15px;font-size:13px;
  opacity:0;transition:opacity .25s;pointer-events:none;z-index:10;box-shadow:0 6px 22px #0006}
#toast.on{opacity:1}
</style></head><body>
<header>
  <h1>🎵 Music Librarian</h1>
  <span id="hdr" class="sub"></span>
  <span style="flex:1"></span>
  <button id="pause" class="ghost">Pause</button>
  <button id="refresh" class="ghost">Refresh taste</button>
  <button id="syncfav" class="ghost">Sync favorites</button>
  <button id="scan" class="ghost">Scan upgrades</button>
</header>
<div class="wrap">
  <div id="pausebanner" class="banner pause">Paused — no new downloads are being queued.</div>
  <div id="stopbanner" class="banner stop"></div>

  <div class="grid">
    <div class="card">
      <h2>Progress</h2>
      <div class="big" id="tb">–</div>
      <div class="bar"><i id="tbbar" style="width:0"></i></div>
      <div class="sub" id="tbsub">–</div>
      <div class="row" style="margin-top:10px"><span class="sub">Tracks</span><b id="tracks">–</b></div>
      <div class="row"><span class="sub">Cluster free</span><b id="free">–</b></div>
      <div class="row"><span class="sub">Staging free</span><b id="sfree">–</b></div>
      <div class="row"><span class="sub">Updated</span><span class="sub" id="upd">–</span></div>
    </div>

    <div class="card">
      <h2>Library quality (on disk)</h2>
      <div class="big" id="llpct">–</div>
      <div class="sub">lossless</div>
      <div class="bar"><i id="llbar" style="width:0;background:var(--ok)"></i></div>
      <table id="qual"></table>
      <div class="sub" id="upcand" style="margin-top:8px"></div>
    </div>

    <div class="card">
      <h2>This run</h2>
      <table id="counts"></table>
    </div>

    <div class="card">
      <h2>Favorites <span class="sub">— full discography + new releases</span></h2>
      <div id="favlist" class="scroll"></div>
      <div class="add">
        <input id="favin" placeholder="Add artist (exact name)…">
        <button id="favadd">Add</button>
      </div>
      <div class="sub" style="margin-top:7px">Starring an artist in Navidrome works too.</div>
    </div>

    <div class="card">
      <h2>New releases</h2>
      <div class="scroll"><table id="newrel"></table></div>
    </div>

    <div class="card">
      <h2>Lossy fallbacks</h2>
      <div class="scroll"><table id="fb"></table></div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Settings <span class="sub">— saved to config.env, applied next loop</span></h2>
    <div class="settings" id="settings"></div>
    <div style="margin-top:13px;display:flex;gap:8px;align-items:center">
      <button id="save">Save settings</button>
      <span class="sub" id="savenote"></span>
    </div>
  </div>

  <div class="grid" style="margin-top:14px">
    <div class="card">
      <h2>Taste — top artists <span class="sub">(vet the list; exclude what you don't want)</span></h2>
      <div class="scroll"><table id="artists"></table></div>
    </div>
    <div class="card">
      <h2>Excluded artists</h2>
      <div id="exclist" class="scroll"></div>
      <div class="add">
        <input id="excin" placeholder="Add artist to blocklist…">
        <button id="excadd">Add</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Log</h2>
    <pre id="log">…</pre>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gb = n => n == null ? '–' : (n >= 1024 ? (n/1024).toFixed(2)+' TB' : Math.round(n)+' GB');
let toastT;
function toast(m){ const t=$('#toast'); t.textContent=m; t.classList.add('on');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('on'),2600); }
async function api(p, opt){ const r = await fetch(p, opt);
  const j = await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||r.status); return j; }
async function post(p, body){ return api(p, {method:'POST',
  headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):'{}'}); }

// Which settings render as a checkbox vs a number field.
const BOOLS = new Set(['PAUSED','FAVORITES_ENABLED','FAVORITE_INCLUDE_EP',
  'FAVORITE_INCLUDE_SINGLES','FAVORITE_INCLUDE_LIVE','FAVORITE_INCLUDE_COMPILATIONS',
  'FAVORITE_INCLUDE_REMIX','UPGRADE_ENABLED','UPGRADE_ONLY_WHEN_IDLE']);
const HELP = {
  TARGET_TB:'Stop at this library size', TARGET_TRACKS:'…or this many tracks',
  EXPLORE_RATIO:'0 = only what you love, 1 = only new horizons',
  MIN_FREE_GB:'Hard floor: never fill the pool below this',
  STAGING_MIN_FREE_GB:'Pause if the NVMe staging pool gets this low',
  CONCURRENCY:'Parallel downloads', TASTE_REFRESH_MIN:'Re-pull play counts (min)',
  MEASURE_EVERY_SEC:'Library re-scan interval (s)',
  TASTE_HALF_LIFE_DAYS:'A play this old counts half as much',
  WEIGHT_NAVIDROME:'Weight of Navidrome plays', WEIGHT_PLEX:'Weight of Plex/Plexamp plays',
  WEIGHT_YTMUSIC:'Weight of YouTube Music history',
  FAVORITE_SYNC_HOURS:'How often to check favorites for new releases',
  FAVORITE_PRIORITY:'Queue position vs exploration (higher = sooner)',
  FAVORITE_INCLUDE_SINGLES:'Warning: adds a LOT of singles',
  UPGRADE_HOUR:'Hour of day to run the upgrade pass (0–23)',
  UPGRADE_MAX_PER_RUN:'Albums checked per pass', UPGRADE_RECHECK_DAYS:'Don\'t re-check within N days',
  UPGRADE_ONLY_WHEN_IDLE:'Yield to the main fill',
  UPGRADE_ENABLED:'Re-download lossy albums as FLAC when one appears',
};
let settingsBuilt = false;
function buildSettings(cfg){
  if(settingsBuilt) return; settingsBuilt = true;
  $('#settings').innerHTML = Object.keys(cfg).map(k => {
    const help = HELP[k] ? `<span class="sub"> — ${esc(HELP[k])}</span>` : '';
    if(BOOLS.has(k)) return `<label>${k}${help}</label>
      <select data-k="${k}"><option value="0">off</option><option value="1">on</option></select>`;
    return `<label>${k}${help}</label><input data-k="${k}" type="number" step="any">`;
  }).join('');
  for(const [k,v] of Object.entries(cfg)){
    const el = document.querySelector(`[data-k="${k}"]`); if(!el) continue;
    el.value = BOOLS.has(k) ? (['1','true','yes','on'].includes(String(v).toLowerCase())?'1':'0') : v;
  }
}

function renderFavs(favs, names){
  const byName = {}; (favs||[]).forEach(f => byName[f.artist.toLowerCase()] = f);
  if(!names.length && !(favs||[]).length){
    $('#favlist').innerHTML = '<div class="muted">No favorites yet. Add one below, or star an artist in Navidrome — the librarian will fetch their whole discography and keep watching for new releases.</div>';
    return;
  }
  const all = new Set([...names, ...(favs||[]).map(f=>f.artist)]);
  $('#favlist').innerHTML = `<table>${[...all].sort((a,b)=>a.localeCompare(b)).map(n => {
    const f = byName[n.toLowerCase()] || {};
    const manual = names.some(x => x.toLowerCase() === n.toLowerCase());
    let st = '<span class="pill">pending sync</span>';
    if(f.note) st = `<span class="pill warn" title="${esc(f.note)}">${esc(f.note.slice(0,26))}</span>`;
    else if(f.albums_total) st = `<span class="pill ${f.albums_have>=f.albums_total?'ok':''}">${f.albums_have}/${f.albums_total} albums</span>`;
    return `<tr><td><b>${esc(n)}</b><br><span class="sub">${f.source==='navidrome'?'★ starred in Navidrome':'manual'}</span></td>
      <td style="text-align:right">${st}</td>
      <td style="width:26px">${manual?`<button class="chip-x ghost" data-fav="${esc(n)}" style="padding:2px 7px">✕</button>`:''}</td></tr>`;
  }).join('')}</table>`;
  document.querySelectorAll('[data-fav]').forEach(b => b.onclick = async () => {
    const next = names.filter(x => x !== b.dataset.fav);
    await post('/api/favorites', {names: next}); toast('Favorite removed'); load();
  });
}

async function load(){
  try{
    const [s, arts, lg, exc, fav] = await Promise.all([
      api('/api/status'), api('/api/artists?limit=60'), api('/api/log?n=120'),
      api('/api/exclude'), api('/api/favorites')]);

    // progress
    const pct = s.pct_to_target ?? 0;
    $('#tb').textContent = (s.library_tb ?? 0).toFixed(3) + ' TB';
    $('#tbbar').style.width = Math.min(100, pct) + '%';
    $('#tbsub').textContent = `${pct}% of ${s.target_tb ?? '?'} TB target`;
    $('#tracks').textContent = `${(s.tracks ?? 0).toLocaleString()} / ${(s.target_tracks ?? 0).toLocaleString()}`;
    $('#free').textContent = gb(s.cluster_free_gb);
    $('#sfree').textContent = gb(s.staging_free_gb);
    $('#upd').textContent = s.updated ?? '–';
    $('#hdr').textContent = s.paused ? 'paused' : (s.stop_reason ? 'idle' : 'running');

    $('#pause').textContent = s.paused ? 'Resume' : 'Pause';
    $('#pausebanner').classList.toggle('on', !!s.paused);
    $('#stopbanner').classList.toggle('on', !!s.stop_reason && !s.paused);
    if(s.stop_reason) $('#stopbanner').textContent = 'Stopped: ' + s.stop_reason +
      ' — raise a target below to resume.';

    // library quality
    const lp = s.library_lossless_pct;
    $('#llpct').textContent = lp == null ? '–' : lp + '%';
    $('#llbar').style.width = (lp ?? 0) + '%';
    $('#qual').innerHTML = '<tr><th>Codec</th><th>Tracks</th><th>Avg kbps</th><th>Size</th></tr>' +
      (s.library_quality||[]).map(b => `<tr><td>${esc(b.codec)}
        <span class="pill ${b.lossless?'ok':'warn'}">${b.lossless?'lossless':'lossy'}</span></td>
        <td>${b.tracks.toLocaleString()}</td><td>${b.avg_kbps||'–'}</td>
        <td>${(b.bytes/1073741824).toFixed(1)} GB</td></tr>`).join('') ||
      '<tr><td class="muted" colspan="4">No scan yet.</td></tr>';
    const uc = s.upgrade_candidates ?? 0;
    const ups = s.upgrades || {};
    $('#upcand').textContent = uc ? `${uc.toLocaleString()} lossy track(s) — upgrade candidates` +
      (Object.keys(ups).length ? ' · ' + Object.entries(ups).map(([k,v])=>`${k}: ${v}`).join(', ') : '')
      : 'Everything on disk is lossless.';

    // counts
    const C = [['queued_flac','FLAC','ok'],['queued_alac','ALAC','ok'],
      ['queued_lossy','Lossy fallback','warn'],['exists','Already had','' ],
      ['pending','Pending',''],['no_source','No source',''],['no_meta','No metadata',''],
      ['low_quality_only','Too low quality','warn'],['error','Errors','bad']];
    $('#counts').innerHTML = C.map(([k,lbl,cls]) => `<tr><td>${lbl}</td>
      <td style="text-align:right"><span class="pill ${(s[k]&&cls)||''}">${(s[k]??0).toLocaleString()}</span></td></tr>`).join('');

    // favorites + new releases
    renderFavs(s.favorites, fav.names);
    $('#newrel').innerHTML = (s.new_releases||[]).length
      ? (s.new_releases||[]).map(n => `<tr><td><b>${esc(n.artist)}</b><br>
          <span class="sub">${esc(n.album)}</span></td>
          <td style="text-align:right" class="sub">${esc(n.released||'')}</td></tr>`).join('')
      : '<tr><td class="muted">Nothing new yet. Favorites are checked every FAVORITE_SYNC_HOURS.</td></tr>';

    // fallbacks
    $('#fb').innerHTML = (s.recent_fallbacks||[]).length
      ? '<tr><th>Artist</th><th>Album</th><th>Got</th></tr>' + (s.recent_fallbacks||[])
        .map(f => `<tr><td>${esc(f.artist)}</td><td>${esc(f.album)}</td>
          <td><span class="pill ${f.lossless?'ok':'warn'}">${esc(f.format)} ${f.kbps||''}</span></td></tr>`).join('')
      : '<tr><td class="muted">No fallbacks — everything so far was lossless.</td></tr>';

    // artists
    const excSet = new Set(exc.names.map(n=>n.toLowerCase()));
    $('#artists').innerHTML = '<tr><th>Artist</th><th>Weight</th><th></th></tr>' +
      arts.artists.map(a => `<tr><td>${esc(a.artist)}
        ${a.favorite?'<span class="pill fav">favorite</span>':''}
        ${a.excluded?'<span class="pill bad">excluded</span>':''}</td>
        <td>${a.weight}</td>
        <td style="text-align:right">${a.excluded?'':
          `<button class="ghost" data-exc="${esc(a.artist)}" style="padding:2px 7px">exclude</button>
           <button class="ghost" data-favadd="${esc(a.artist)}" style="padding:2px 7px">★</button>`}</td></tr>`).join('');
    document.querySelectorAll('[data-exc]').forEach(b => b.onclick = async () => {
      await post('/api/exclude', {names: [...exc.names, b.dataset.exc]});
      toast('Excluded ' + b.dataset.exc); load();
    });
    document.querySelectorAll('[data-favadd]').forEach(b => b.onclick = async () => {
      if(excSet.has(b.dataset.favadd.toLowerCase())) return toast('That artist is excluded.');
      await post('/api/favorites', {names: [...fav.names, b.dataset.favadd]});
      toast('Favorited ' + b.dataset.favadd); load();
    });

    // excludes
    $('#exclist').innerHTML = '<div class="chips">' + exc.names.map(n =>
      `<span class="chip"><b>${esc(n)}</b><button data-rm="${esc(n)}">×</button></span>`).join('') + '</div>';
    document.querySelectorAll('[data-rm]').forEach(b => b.onclick = async () => {
      await post('/api/exclude', {names: exc.names.filter(x => x !== b.dataset.rm)});
      toast('Removed ' + b.dataset.rm); load();
    });

    buildSettings(s.config || {});
    $('#log').textContent = lg.lines.join('\n');
    $('#log').scrollTop = $('#log').scrollHeight;
  }catch(e){ $('#hdr').textContent = 'error: ' + e.message; }
}

$('#save').onclick = async () => {
  const body = {};
  document.querySelectorAll('[data-k]').forEach(el => body[el.dataset.k] = el.value);
  try{ const r = await post('/api/config', body);
    $('#savenote').textContent = r.note || ''; toast('Saved — applies next loop'); }
  catch(e){ toast('Error: ' + e.message); }
};
$('#pause').onclick = async () => {
  const paused = $('#pause').textContent === 'Resume';
  await post(paused ? '/api/resume' : '/api/pause');
  settingsBuilt = false;                  // PAUSED changed underneath the form
  toast(paused ? 'Resumed' : 'Paused'); load();
};
$('#refresh').onclick = async () => { await post('/api/refresh-taste'); toast('Taste refresh queued'); };
$('#syncfav').onclick = async () => { await post('/api/sync-favorites'); toast('Favorites sync queued'); };
$('#scan').onclick = async () => {
  try{ const r = await post('/api/scan-upgrades'); toast(r.note || 'Upgrade scan queued'); }
  catch(e){ toast(e.message); }
};
$('#favadd').onclick = async () => {
  const v = $('#favin').value.trim(); if(!v) return;
  const cur = (await api('/api/favorites')).names;
  await post('/api/favorites', {names: [...cur, v]});
  $('#favin').value = ''; toast('Favorited ' + v); load();
};
$('#excadd').onclick = async () => {
  const v = $('#excin').value.trim(); if(!v) return;
  const cur = (await api('/api/exclude')).names;
  await post('/api/exclude', {names: [...cur, v]});
  $('#excin').value = ''; toast('Excluded ' + v); load();
};
$('#favin').addEventListener('keydown', e => { if(e.key === 'Enter') $('#favadd').click(); });
$('#excin').addEventListener('keydown', e => { if(e.key === 'Enter') $('#excadd').click(); });

load(); setInterval(load, 5000);
</script></body></html>
"""
