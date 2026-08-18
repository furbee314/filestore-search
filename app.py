"""Offline file-store search web app (standard library only, no Flask).

Endpoints:
  GET /                 -> single-page UI
  GET /app.css          -> styles
  GET /app.js           -> front-end logic
  GET /api/health       -> {ok, llm, total, db, ...}
  GET /api/facets       -> category/platform filters + counts
  GET /api/search?q=&category=&platform=&limit=&answer=1
                         -> {query, results:[...], answer:"..."}
                           answer is present only when answer=1 and the LLM
                           is reachable; on failure answer is null and the
                           deterministic results are still returned.
                           &sort=newest sorts by most-recently-modified
                           instead of relevance; &since=YYYY-MM-DD (or epoch)
                           restricts results to files modified after that.

Run:
  python3 -m app            # uses config.py
  FILESTORE_SEARCH_DATA=... python3 -m app --port 8080
"""
import json
import mimetypes
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn

import db
from config import load_config
from llm import LLMClient

# Natural-language recency markers: their presence (in the user's query, the
# LLM-rewritten query, or an explicit ?sort=newest) switches search into
# most-recently-modified ordering instead of relevance.
RECENT_RE = re.compile(
    r"\b(latest|newest|recent|recently)\b", re.IGNORECASE)

STATE = {
    "cfg": None,
    "con": None,
    "llm": None,
    "lock": threading.Lock(),
}


def init(cfg=None):
    cfg = cfg or load_config()
    STATE["cfg"] = cfg
    STATE["con"] = db.connect(cfg, check_same_thread=False)
    STATE["llm"] = LLMClient(cfg=cfg)


def get_state():
    if STATE["con"] is None:
        init()
    return STATE


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def do_search(q, category=None, platform=None, limit=25, want_answer=False,
              sort=None, since=None):
    st = get_state()
    llm = st["llm"]
    used_llm = False
    llm_meta = None
    query = q
    cat = category
    plat = platform
    ver = None
    since_ts = db.since_to_ts(since)
    # recency can also come from the LLM's rewrite (a real LLM may set the
    # 'since' filter when the user names a time window)
    since_ts_from_llm = None

    if llm.enabled:
        try:
            rr = llm.rewrite_query(q, timeout=min(45, llm.timeout))
            if rr:
                query = rr["query"] or q
                cat = rr["category"] or cat
                plat = rr["platform"] or plat
                ver = rr["version"]
                if rr.get("since"):
                    since_ts_from_llm = db.since_to_ts(rr["since"])
                used_llm = True
                llm_meta = rr
        except Exception as e:
            llm_meta = {"error": str(e)}

    if since_ts_from_llm is not None and since_ts is None:
        since_ts = since_ts_from_llm

    # version filter: if the LLM (or the user via ?version=) named one, bias
    # results to it by appending the version token to the query
    if ver and ver not in query:
        query = f"{query} {ver}"

    def run(cat_, plat_, recency_order=False):
        with st["lock"]:
            return db.search(st["con"], query, limit=limit,
                             category=cat_, platform=plat_,
                             min_mtime=since_ts,
                             sort="mtime" if recency_order else "relevance")

    def runr(cat_, plat_):
        with st["lock"]:
            return db.newest(st["con"], limit, category=cat_,
                             platform=plat_, min_mtime=since_ts)

    def loosen(fn, c, p):
        """Run fn, and if empty, retry with progressively looser filters."""
        res = fn(c, p)
        if not res:
            for c2, p2 in ((None, p), (c, None), (None, None)):
                if c2 is c and p2 is p:
                    continue
                res = fn(c2, p2)
                if res:
                    break
        return res

    explicit_newest = sort in ("newest", "mtime")
    # Recency intent: an explicit ?sort=newest, or the word 'latest/newest/
    # recent/new' in the user's original query OR the LLM-rewritten one.
    recency = explicit_newest or bool(
        RECENT_RE.search(q or "")) or bool(RECENT_RE.search(query or ""))

    if recency:
        # "most recent" — order by most-recently-modified. When the user also
        # named keywords (e.g. "latest Dell R740 firmware") we restrict to the
        # matching files; for a bare "latest" the keyword set is the recency
        # words themselves, which match nothing, so we fall back to the
        # store-wide most-recent files (never an empty page).
        order = "mtime"
        results = loosen(lambda c, p: run(c, p, recency_order=True), cat, plat)
        if not results:
            results = loosen(runr, cat, plat)
    else:
        order = "relevance"
        # Robustness: if the LLM (or user) filters were too aggressive and
        # wiped out the results, retry with progressively looser filters
        # rather than returning an empty page.
        results = loosen(lambda c, p: run(c, p), cat, plat)

    answer = None
    if want_answer:
        if not llm.enabled:
            answer = None
        else:
            try:
                answer = llm.answer(q, results, timeout=min(90, llm.timeout))
            except Exception as e:
                answer = f"(LLM unavailable: {e})"

    return {
        "query": q,
        "rewritten_query": query if used_llm else None,
        "llm_used": used_llm,
        "llm_meta": llm_meta,
        "results": results,
        "answer": answer,
        "count": len(results),
        "sort": order,
        "since": since,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FileStoreSearch/1.0"

    def log_message(self, fmt, *args):  # keep stdout quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/":
            return self._send(200, UI_HTML, "text/html; charset=utf-8")
        if path == "/app.css":
            return self._send(200, CSS, "text/css; charset=utf-8")
        if path == "/app.js":
            return self._send(200, JS, "text/javascript; charset=utf-8")
        if path == "/api/health":
            st = get_state()
            llm_ok = False
            if st["llm"].enabled:
                llm_ok = st["llm"].ping()
            with st["lock"]:
                total = st["con"].execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return self._send(200, json.dumps({
                "ok": True, "total": total,
                "llm_enabled": st["llm"].enabled,
                "llm_reachable": llm_ok,
                "llm_model": st["llm"].model,
                "llm_base": st["llm"].base_url,
                "data_dir": st["cfg"]["data_dir"],
            }))
        if path == "/api/facets":
            st = get_state()
            with st["lock"]:
                facets = db.facets(st["con"])
            return self._send(200, json.dumps(facets))
        if path == "/api/search":
            q = (qs.get("q") or "").strip()
            if not q:
                return self._send(400, json.dumps({"error": "missing q"}))
            limit = min(int(qs.get("limit") or 25), 100)
            cat = qs.get("category") or None
            plat = qs.get("platform") or None
            want_answer = qs.get("answer") in ("1", "true", "yes")
            sort = qs.get("sort") or None
            since = qs.get("since") or None
            try:
                out = do_search(q, category=cat, platform=plat, limit=limit,
                                want_answer=want_answer, sort=sort, since=since)
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps(out))
        if path == "/download" or path.startswith("/files/"):
            return self._download(parsed)
        return self._send(404, json.dumps({"error": "not found"}))

    def _download(self, parsed):
        """Serve files straight out of data_dir (fallback when the UI is not
        fronted by the nginx site)."""
        st = get_state()
        rel = parsed.path[len("/files/"):] if parsed.path.startswith("/files/") \
            else parsed.query.split("path=", 1)[1] if "path=" in parsed.query else ""
        if not rel:
            return self._send(400, "missing path")
        rel = urllib.parse.unquote(rel)
        base = os.path.realpath(st["cfg"]["data_dir"])
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep) and full != base:
            return self._send(403, "forbidden")
        if not os.path.isfile(full):
            return self._send(404, "not found")
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        start = end = None
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
        ctype, _ = mimetypes.guess_type(full)
        ctype = ctype or "application/octet-stream"
        self.send_response(206 if start is not None else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        fn = os.path.basename(full)
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" +
                         urllib.parse.quote(fn))
        if start is not None:
            end = end if (end is not None and end < size) else size - 1
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            length = end - start + 1
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                self.wfile.write(f.read(length))
        else:
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    self.wfile.write(chunk)


# ---------------------------------------------------------------------------
# static assets
# ---------------------------------------------------------------------------

UI_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Store Search</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header>
  <h1>File Store Search</h1>
  <div id="status" class="status"></div>
</header>
<main>
  <form id="search-form" autocomplete="off">
    <input id="q" type="search" placeholder="Search in natural language, e.g. &#8220;firmware for my Dell R740 with a 6230 CPU&#8221;" required>
    <button type="submit" id="go">Search</button>
    <label class="chk"><input type="checkbox" id="want-answer"> AI answer</label>
  </form>
  <div id="filters" class="filters"></div>
  <div id="answer" class="answer" hidden></div>
  <div id="meta" class="meta"></div>
  <div id="results"></div>
  <div id="loading" class="loading" hidden>Searching&#8230;</div>
</main>
<footer>100% offline &middot; search index rebuilt periodically &middot; downloads via your nginx site or /files/</footer>
<script src="/app.js"></script>
</body></html>
"""

CSS = r"""
:root { --bg:#0f172a; --panel:#1e293b; --ink:#e2e8f0; --mut:#94a3b8;
        --acc:#38bdf8; --ok:#4ade80; --warn:#fbbf24; --err:#f87171; --line:#334155; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
header { display:flex; justify-content:space-between; align-items:center;
         padding:14px 22px; background:var(--panel); border-bottom:1px solid var(--line); }
h1 { font-size:18px; margin:0; letter-spacing:.3px; }
.status { font-size:12.5px; color:var(--mut); }
.status b { color:var(--ink); }
main { max-width:1050px; margin:0 auto; padding:18px 22px 60px; }
#search-form { display:flex; gap:8px; }
#search-form input[type=search] { flex:1; background:var(--panel); color:var(--ink);
  border:1px solid var(--line); border-radius:8px; padding:10px 12px; font-size:15px; }
#search-form input[type=search]:focus { outline:none; border-color:var(--acc); }
button { background:var(--acc); color:#082032; border:0; border-radius:8px;
  padding:9px 18px; font-weight:600; cursor:pointer; }
button:hover { filter:brightness(1.08); }
.chk { display:flex; align-items:center; gap:6px; color:var(--mut); font-size:13.5px;
  user-select:none; }
.filters { display:flex; gap:8px; margin:12px 0 4px; flex-wrap:wrap; }
.filters select { background:var(--panel); color:var(--ink); border:1px solid var(--line);
  border-radius:8px; padding:7px 9px; font-size:13.5px; }
.meta { color:var(--mut); font-size:13px; margin:10px 2px 6px; }
.meta .rw { color:var(--warn); }
.answer { background:var(--panel); border:1px solid var(--line);
  border-left:3px solid var(--acc); border-radius:8px; padding:12px 14px;
  margin:12px 0; white-space:pre-wrap; }
table { width:100%; border-collapse:collapse; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.4px;
  position:sticky; top:0; background:var(--bg); }
tr:hover td { background:#22304a; }
.name { font-weight:600; word-break:break-all; }
.path { color:var(--mut); font-size:12.5px; font-family:ui-monospace,Menlo,Consolas,monospace;
  word-break:break-all; }
.badge { display:inline-block; padding:2px 7px; border-radius:20px; font-size:11.5px;
  background:#0b1220; border:1px solid var(--line); color:var(--mut); white-space:nowrap; }
.badge.cat { color:var(--acc); border-color:var(--acc); }
.size { color:var(--mut); font-variant-numeric:tabular-nums; white-space:nowrap; }
.mtime { color:var(--mut); font-variant-numeric:tabular-nums; white-space:nowrap; font-size:12.5px; }
a.dl { color:var(--acc); text-decoration:none; }
a.dl:hover { text-decoration:underline; }
.loading { color:var(--mut); margin:18px 0; }
.empty { color:var(--mut); margin:18px 0; }
footer { text-align:center; color:var(--mut); font-size:12px; padding:16px;
  border-top:1px solid var(--line); margin-top:30px; }
@media (max-width:640px){ th,td{padding:6px} .path{display:none} .mtime{display:none} }
"""

JS = r"""
'use strict';
const $ = id => document.getElementById(id);
let facets = null;

function fmtSize(b){ if(!b) return '';
  const u=['B','KB','MB','GB','TB']; let i=0; b=+b;
  while(b>=1024&&i<u.length-1){b/=1024;i++}
  return b.toFixed(b<10&&i>0?1:0)+' '+u[i]; }
function fmtDate(ts){ if(!ts) return '';
  const d=new Date(ts*1000);
  const p=n=>String(n).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes()); }
function esc(s){ return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function loadFacets(){
  try{
    const r = await fetch('/api/facets'); facets = await r.json();
    renderFilters();
  }catch(e){}
}
function renderFilters(){
  if(!facets) return;
  const f = $('filters');
  let h = '<select id="f-cat"><option value="">All categories</option>';
  for(const c of facets.categories)
    h += `<option value="${esc(c)}">${esc(c)}${facets.counts[c]?` (${facets.counts[c]})`:''}</option>`;
  h += '</select><select id="f-plat"><option value="">All platforms</option>';
  for(const p of facets.platforms)
    h += `<option value="${esc(p)}">${esc(p)}</option>`;
  h += '</select><select id="f-sort"><option value="">Sort: Relevance</option>'
     + '<option value="newest">Sort: Newest first</option></select>';
  f.innerHTML = h;
  $('f-cat').onchange = doSearch;
  $('f-plat').onchange = doSearch;
  $('f-sort').onchange = doSearch;
}

async function health(){
  try{
    const r = await fetch('/api/health'); const h = await r.json();
    const llm = h.llm_enabled ? (h.llm_reachable?'LLM online':'LLM offline') : 'LLM disabled';
    $('status').innerHTML = `<b>${h.total}</b> files indexed &middot; ${esc(llm)}`
      + (h.llm_reachable?` &middot; ${esc(h.llm_model)}`:'');
  }catch(e){ $('status').textContent='index unavailable'; }
}

async function doSearch(){
  const q = $('q').value.trim();
  if(!q) return;
  const cat = $('f-cat')?.value || '';
  const plat = $('f-plat')?.value || '';
  const sort = $('f-sort')?.value || '';
  const want = $('want-answer').checked;
  $('loading').hidden=false; $('answer').hidden=true; $('results').innerHTML='';
  const url = `/api/search?q=${encodeURIComponent(q)}&limit=25`
    + (cat?`&category=${encodeURIComponent(cat)}`:'')
    + (plat?`&platform=${encodeURIComponent(plat)}`:'')
    + (sort?`&sort=${encodeURIComponent(sort)}`:'')
    + (want?'&answer=1':'');
  try{
    const r = await fetch(url); const d = await r.json();
    $('loading').hidden=true;
    if(d.error){ $('results').innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
    renderMeta(d);
    if(d.answer){ $('answer').textContent = d.answer; $('answer').hidden=false; }
    else $('answer').hidden=true;
    renderResults(d.results||[]);
  }catch(e){
    $('loading').hidden=true;
    $('results').innerHTML = `<div class="empty">Request failed: ${esc(e.message)}</div>`;
  }
}

function renderMeta(d){
  let h = `${d.count} result${d.count===1?'':'s'} for &#8220;${esc(d.query)}&#8221;`;
  if(d.sort==='mtime') h += ` &middot; <span class="rw">sorted newest first</span>`;
  if(d.since) h += ` &middot; modified since ${esc(d.since)}`;
  if(d.rewritten_query && d.rewritten_query!==d.query)
    h += ` &middot; <span class="rw">searched as &#8220;${esc(d.rewritten_query)}&#8221;</span>`;
  $('meta').innerHTML = h;
}

function renderResults(rows){
  if(!rows.length){ $('results').innerHTML='<div class="empty">No files matched. Try different words, or enable &#8220;AI answer&#8221; to let the LLM interpret it.</div>'; return; }
  const base = document.location.origin;
  let h = `<table><thead><tr><th>File</th><th>Category</th><th>Platform</th><th>Version</th><th>Modified</th><th>Size</th><th></th></tr></thead><tbody>`;
  for(const r of rows){
    const dl = `/files/${encodeURIComponent(r.path)}`;
    h += `<tr>
      <td><div class="name">${esc(r.name)}</div><div class="path">${esc(r.path)}</div></td>
      <td><span class="badge cat">${esc(r.category)}</span></td>
      <td>${esc(r.platform)}${r.arch!=='unknown'?` <span class="badge">${esc(r.arch)}</span>`:''}</td>
      <td>${esc(r.version)||'&ndash;'}</td>
      <td class="size">${fmtDate(r.mtime)}</td>
      <td class="size">${fmtSize(r.size)}</td>
      <td><a class="dl" href="${dl}" download>Download</a></td>
      </tr>`;
  }
  h += '</tbody></table>';
  $('results').innerHTML = h;
}

$('search-form').onsubmit = e => { e.preventDefault(); doSearch(); };
health(); loadFacets();
$('q').focus();
"""


def main(host=None, port=None, argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=host or "0.0.0.0")
    ap.add_argument("--port", type=int,
                    default=int(port or os.environ.get("FILESTORE_SEARCH_PORT", 8080)))
    ap.add_argument("--index", action="store_true", help="run a full reindex first")
    ap.add_argument("--no-index", action="store_true",
                    help="do not auto-index on startup if empty")
    # argv=None -> parse sys.argv[1:] (normal `python3 -m app` use);
    # callers embedding us in another CLI pass their own remaining args.
    args = ap.parse_args(argv)

    cfg = load_config()
    init(cfg)
    st = get_state()
    n = st["con"].execute("SELECT COUNT(*) FROM files").fetchone()[0]
    if args.index:
        db.full_reindex(cfg, st["con"])
    elif n == 0 and not args.no_index:
        print("no index yet; running full reindex")
        db.full_reindex(cfg, st["con"])

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"File Store Search listening on http://{args.host}:{args.port}")
    print(f"  data dir : {cfg['data_dir']}")
    print(f"  db       : {cfg['db_path']}")
    if cfg["llm_disable"]:
        print("  LLM      : disabled")
    else:
        print(f"  LLM      : {cfg['llm_base']} model {cfg['llm_model']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
