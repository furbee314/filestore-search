"""Offline file-store search web app (standard library only, no Flask).

Endpoints:
  GET /                 -> single-page UI
  GET /app.css          -> styles
  GET /app.js           -> front-end logic
  GET /api/health       -> {ok, llm, total, db, ...}
  GET /api/facets       -> category/platform filters + counts
  GET /api/search?q=&category=&platform=&limit=&offset=&answer=1
                         -> {query, results:[...], answer:"...",
                             total, offset, limit, pages}
                           results holds one page (limit rows from offset);
                           total is the full result-set length, pages the
                           number of pages at that limit.
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
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime
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

# Rewrite cache: with sampling pinned (seed=0, temperature 0), a repeated
# search can reuse the LLM's rewrite instead of re-running CPU inference.
# The same question therefore always yields the same result set. Bounded
# so long-running services don't grow unbounded.
_REWRITE_CACHE = OrderedDict()
_REWRITE_CACHE_LOCK = threading.Lock()
_REWRITE_CACHE_MAX = 512


def _rewrite_cache_get(key):
    with _REWRITE_CACHE_LOCK:
        if key in _REWRITE_CACHE:
            _REWRITE_CACHE.move_to_end(key)
            return _REWRITE_CACHE[key]
    return None


def _rewrite_cache_put(key, value):
    with _REWRITE_CACHE_LOCK:
        _REWRITE_CACHE[key] = value
        _REWRITE_CACHE.move_to_end(key)
        while len(_REWRITE_CACHE) > _REWRITE_CACHE_MAX:
            _REWRITE_CACHE.popitem(last=False)


def _append_tokens(query, tokens):
    """Append any tokens not already present (case-insensitive, substring)."""
    base = " ".join(query.split())
    low = base.lower()
    for t in tokens:
        tl = t.lower()
        if tl in low:
            continue
        low += " " + tl
        base += " " + t
    return base


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

def do_search(q, category=None, platform=None, limit=25, offset=0,
              want_answer=False, sort=None, since=None):
    st = get_state()
    llm = st["llm"]
    used_llm = False
    llm_meta = None
    query = q
    cat = category
    plat = platform
    ver = None
    or_terms = None  # LLM 'X or Y' alternatives (any_of)
    since_ts = db.since_to_ts(since)
    off = max(0, int(offset))
    lim = max(1, int(limit))
    # Time phrases are resolved from the SYSTEM clock, never the LLM: the
    # LLM has no reliable current date and hallucinates one for relative
    # phrases ("this month", "last week"). An explicit ?since= parameter
    # always wins; otherwise a time phrase in the user's request is
    # resolved deterministically against the local clock.
    since_resolved = None

    if llm.enabled:
        # Rewrite cache: the same question reuses the LLM's rewrite instead
        # of re-running CPU inference. Combined with pinned sampling
        # (seed=0 / temperature 0) this makes repeated searches of the same
        # question return the same result set every time. The key includes
        # today's local date so a time-phrased query ("this month", "last
        # week") is re-rewritten when the day rolls over and its window
        # slides. A stub LLM can prepend a per-check prefix to its cache key
        # so each test section starts from a cold cache (real clients use
        # the bare question).
        cache_key = " ".join(re.split(r"\s+", q.lower().strip())) + \
            " @ " + datetime.now().strftime("%Y-%m-%d") + \
            getattr(llm, "cache_key_prefix", "")
        rr = _rewrite_cache_get(cache_key)
        if rr is None:
            try:
                rr = llm.rewrite_query(q, timeout=min(45, llm.timeout))
            except Exception as e:
                llm_meta = {"error": str(e)}
            else:
                _rewrite_cache_put(cache_key, rr)
        if rr:
            query = rr["query"] or q
            cat = rr["category"] or cat
            plat = rr["platform"] or plat
            ver = rr["version"]
            or_terms = rr.get("any_of")
            # Never trust an LLM-invented 'since' date: the model has no
            # reliable current date and hallucinates one for relative
            # phrases. Any since value it emits is discarded — time windows
            # come from the user's ?since= param or the system-clock
            # resolution of the phrase below.
            llm_meta = dict(rr)
            if rr.get("since"):
                llm_meta["since_ignored"] = \
                    "LLM-invented date; resolved from system clock instead"
            used_llm = True
            # Guardrail: the rewriter may rephrase, but it must not drop
            # concrete identifiers (model/part/version numbers) — those are
            # what pin a search down to the right file. Anything it
            # discarded is merged back into the keyword query (the version
            # is also checked, since the LLM may move it into "version").
            kept = db.recover_identifiers(q, query + (" " + ver if ver else ""))
            if kept:
                query = _append_tokens(query, kept)
                llm_meta["dropped_identifiers"] = kept

    # Time window, in priority order: an explicit ?since= param (user-typed
    # date) beats a time phrase in the request resolved against the SYSTEM
    # clock (never the LLM, which can't be trusted with the current date).
    # A literal date in the request ("since 2024-05-01") is a strict filter
    # the user asked for; a relative phrase ("this month", "last week") is
    # a soft window that may be widened when it matches nothing.
    q_has_literal_date = bool(re.search(
        r"\b\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?\b", q or "") or
        re.search(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b", q or ""))
    if since_ts is None and q:
        since_resolved = db.resolve_time_phrase(q)
        if since_resolved:
            since_ts = db.since_to_ts(since_resolved)
    since_window_relative = (since_ts is not None and since_resolved
                             and not since and not q_has_literal_date)

    # version filter: if the LLM (or the user via ?version=) named one, bias
    # results to it by appending the version token to the query
    if ver and ver not in query:
        query = f"{query} {ver}"

    def run(cat_, plat_, recency_order=False):
        with st["lock"]:
            return db.search(st["con"], query, limit=lim, offset=off,
                             category=cat_, platform=plat_,
                             min_mtime=since_ts,
                             sort="mtime" if recency_order else "relevance",
                             or_terms=or_terms)

    def runr(cat_, plat_):
        with st["lock"]:
            return db.newest(st["con"], lim, offset=off, category=cat_,
                             platform=plat_, min_mtime=since_ts)

    def loosen(fn, c, p):
        """Run fn, and if empty, retry with progressively looser filters.

        Returns (results, cat_used, plat_used) so the caller can compute the
        total against the same effective filter set that produced the results.
        """
        res = fn(c, p)
        if not res:
            for c2, p2 in ((None, p), (c, None), (None, None)):
                if c2 is c and p2 is p:
                    continue
                res = fn(c2, p2)
                if res:
                    return res, c2, p2
            return res, c, p
        return res, c, p

    explicit_newest = sort in ("newest", "mtime")
    # Recency intent: an explicit ?sort=newest, or the word 'latest/newest/
    # recent/new' in the user's original query OR the LLM-rewritten one.
    recency = explicit_newest or bool(
        RECENT_RE.search(q or "")) or bool(RECENT_RE.search(query or ""))

    # Decide the result path ONCE (page-independent), before fetching the
    # requested page, so the total and the bar stay consistent across pages.
    # For recency we only use the keyword-matched, mtime-ordered set if it has
    # at least one member; a bare "latest" (no real keywords) falls back to the
    # store-wide most-recent files instead.
    cat_eff = plat_eff = None
    # An empty time window is a soft filter: when a relative phrase resolved
    # from the system clock ("this month", "last week") empties the keyword
    # set — the store simply has nothing that fresh — fall back to the most
    # recent matching files overall rather than an empty page, and tell the
    # caller so it can explain the window was widened. Strict filters (an
    # ?since= param, or a literal date typed in the request) are never
    # widened. (q_has_literal_date above is the guard: version numbers like
    # "24.04" don't match the literal-date patterns, so this can't
    # false-positive on them.)
    if since_window_relative:
        with st["lock"]:
            kw_any = db.count_matches(st["con"], query, or_terms,
                                      category=cat, platform=plat,
                                      min_mtime=since_ts)
        if not kw_any:
            since_ts = None
            since_resolved = None
            llm_meta = llm_meta or {}
            llm_meta["window_widened"] = \
                "no files in that window; showing most recent matches"

    if recency:
        order = "mtime"
        kw_count = 0
        with st["lock"]:
            kw_count = db.count_matches(st["con"], query, or_terms,
                                        category=cat, platform=plat,
                                        min_mtime=since_ts)
        if kw_count:
            # keyword set is non-empty: page through it (loosening filters only
            # kicks in if the current page happens to come back empty)
            results, cat_eff, plat_eff = loosen(
                lambda c, p: run(c, p, recency_order=True), cat, plat)
            total = db.count_matches(st["con"], query, or_terms,
                                     category=cat_eff, platform=plat_eff,
                                     min_mtime=since_ts)
        else:
            # no keyword matches: fall back to store-wide most-recent files
            results, cat_eff, plat_eff = loosen(runr, cat, plat)
            total = db.count_filtered(st["con"], category=cat_eff,
                                      platform=plat_eff,
                                      min_mtime=since_ts)
    else:
        order = "relevance"
        # Robustness: if the LLM (or user) filters were too aggressive and
        # wiped out the results, retry with progressively looser filters
        # rather than returning an empty page.
        results, cat_eff, plat_eff = loosen(lambda c, p: run(c, p), cat, plat)
        total = db.count_matches(st["con"], query, or_terms, category=cat_eff,
                                 platform=plat_eff, min_mtime=since_ts)

    answer = None
    if want_answer:
        if not llm.enabled:
            answer = None
        else:
            try:
                answer = llm.answer(q, results, timeout=min(90, llm.timeout))
            except Exception as e:
                answer = f"(LLM unavailable: {e})"

    pages = max(1, -(-total // lim)) if total else (1 if results else 0)
    return {
        "query": q,
        "rewritten_query": query if used_llm else None,
        "llm_used": used_llm,
        "llm_meta": llm_meta,
        "results": results,
        "answer": answer,
        "count": len(results),
        "total": total,
        "offset": off,
        "limit": lim,
        "pages": pages,
        "sort": order,
        "since": since or since_resolved,
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
            limit = min(int(qs.get("limit") or 20), 100)
            try:
                offset = max(int(qs.get("offset") or 0), 0)
            except (TypeError, ValueError):
                offset = 0
            cat = qs.get("category") or None
            plat = qs.get("platform") or None
            want_answer = qs.get("answer") in ("1", "true", "yes")
            sort = qs.get("sort") or None
            since = qs.get("since") or None
            try:
                out = do_search(q, category=cat, platform=plat, limit=limit,
                                offset=offset, want_answer=want_answer,
                                sort=sort, since=since)
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
<title>Search the Vault</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header>
  <h1><a id="home" class="home" href="/">Search the Vault</a></h1>
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
  <div id="pagination" class="pagination"></div>
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
h1 a.home { color:var(--ink); text-decoration:none; }
h1 a.home:hover { color:var(--acc); }
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
a.parent { color:var(--acc); font-size:12px; text-decoration:none; margin-top:3px;
  display:inline-block; }
a.parent:hover { text-decoration:underline; }
.badge { display:inline-block; padding:2px 7px; border-radius:20px; font-size:11.5px;
  background:#0b1220; border:1px solid var(--line); color:var(--mut); white-space:nowrap; }
.badge.cat { color:var(--acc); border-color:var(--acc); }
.size { color:var(--mut); font-variant-numeric:tabular-nums; white-space:nowrap; }
.chksum { display:inline-block; color:var(--mut); font-size:12px;
  font-family:ui-monospace,Menlo,Consolas,monospace; margin:1px 4px 1px 0;
  cursor:help; white-space:nowrap; }
.chksum:hover { color:var(--acc); }
.mtime { color:var(--mut); font-variant-numeric:tabular-nums; white-space:nowrap; font-size:12.5px; }
a.dl { color:var(--acc); text-decoration:none; }
a.dl:hover { text-decoration:underline; }
.loading { color:var(--mut); margin:18px 0; }
.empty { color:var(--mut); margin:18px 0; }
.pagination { display:flex; gap:5px; align-items:center; justify-content:center;
  flex-wrap:wrap; margin:18px 0 4px; }
.pagination button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
  border-radius:6px; padding:5px 11px; font-size:13px; cursor:pointer; min-width:34px; }
.pagination button:hover:not(:disabled) { border-color:var(--acc); color:var(--acc); }
.pagination button.cur { background:var(--acc); color:#082032; border-color:var(--acc);
  font-weight:600; }
.pagination button:disabled { opacity:.4; cursor:default; }
.pagination .pgdots { color:var(--mut); padding:0 2px; }
footer { text-align:center; color:var(--mut); font-size:12px; padding:16px;
  border-top:1px solid var(--line); margin-top:30px; }
@media (max-width:640px){ th,td{padding:6px} .path{display:none} .mtime{display:none} }
"""

JS = r"""
'use strict';
const $ = id => document.getElementById(id);
let facets = null;
let page = 1; // current page (1-based) of the active result set

// Base URL = the origin the page was reached from (the same URL used to
// access the site). Everything else in the UI is derived from it.
const BASE_URL = document.location.origin;

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
     + '<option value="newest">Sort: Newest first</option></select>'
     + '<label class="chk limit-lbl"><span>Results per page</span>'
     + '<select id="f-limit">'
     + [5,10,20,50,100].map(n=>`<option value="${n}">${n}${n===20?' (default)':''}</option>`).join('')
     + '</select></label>';
  f.innerHTML = h;
  const reset = () => { page = 1; doSearch(); };
  $('f-cat').onchange = reset;
  $('f-plat').onchange = reset;
  $('f-sort').onchange = reset;
  $('f-limit').onchange = reset; // changing page size re-starts from page 1
  // default to 20 results per page
  $('f-limit').value = '20';
}

async function health(){
  try{
    const r = await fetch('/api/health'); const h = await r.json();
    const llm = h.llm_enabled ? (h.llm_reachable?'LLM online':'LLM offline') : 'LLM disabled';
    $('status').innerHTML = `<b>${h.total}</b> files indexed &middot; ${esc(llm)}`
      + (h.llm_reachable?` &middot; ${esc(h.llm_model)}`:'');
  }catch(e){ $('status').textContent='index unavailable'; }
}

// Parent-folder link target for a result: the directory the file sits in.
// The base URL is exactly the URL the user used to reach this page
// (document.location origin), so it works whether the UI is served at
// "/", on a port, or behind nginx at a sub-path. A file directly at the
// store root has a parent of just the site root. A trailing slash marks
// it as a directory (it collapses to "<baseurl>/" at the root).
function parentHref(path){
  const p = String(path||'');
  const i = p.lastIndexOf('/');
  const dir = i > 0 ? p.slice(0, i) : ''; // '' when the file is at the root
  return dir ? BASE_URL + '/' + dir + '/' : BASE_URL + '/';
}

async function doSearch(){
  const q = $('q').value.trim();
  if(!q) return;
  const cat = $('f-cat')?.value || '';
  const plat = $('f-plat')?.value || '';
  const sort = $('f-sort')?.value || '';
  const limit = $('f-limit')?.value || '20';
  const want = $('want-answer').checked;
  const offset = (page - 1) * +limit;
  $('loading').hidden=false; $('answer').hidden=true; $('results').innerHTML='';
  $('pagination').innerHTML='';
  const url = `/api/search?q=${encodeURIComponent(q)}&limit=${encodeURIComponent(limit)}&offset=${offset}`
    + (cat?`&category=${encodeURIComponent(cat)}`:'')
    + (plat?`&platform=${encodeURIComponent(plat)}`:'')
    + (sort?`&sort=${encodeURIComponent(sort)}`:'')
    + (want?'&answer=1':'');
  try{
    const r = await fetch(url); const d = await r.json();
    $('loading').hidden=true;
    if(d.error){ $('results').innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
    renderMeta(d);
    if(d.answer && page===1){ $('answer').textContent = d.answer; $('answer').hidden=false; }
    else $('answer').hidden=true;
    renderResults(d.results||[]);
    renderPagination(d);
  }catch(e){
    $('loading').hidden=true;
    $('results').innerHTML = `<div class="empty">Request failed: ${esc(e.message)}</div>`;
  }
}

function gotoPage(p){
  page = Math.max(1, p);
  doSearch();
  window.scrollTo({top:0, behavior:'smooth'});
}

function renderPagination(d){
  const el = $('pagination');
  const total = d.total||0, limit = d.limit||+($('f-limit').value||'20');
  const pages = Math.max(1, Math.ceil(total/limit));
  // No bar when there is nothing to navigate (single page or empty set).
  if(d.error || total<=0 || pages<=1){ el.innerHTML=''; return; }
  const cur = page;
  // Compact window of page numbers around the current page.
  const win = 2; // pages shown on each side of current
  const lo = Math.max(1, cur-win), hi = Math.min(pages, cur+win);
  let h = '';
  h += `<button class="pgnav" ${cur===1?'disabled':''} onclick="gotoPage(${cur-1})">&laquo; Prev</button>`;
  if(lo>1){ h += `<button class="pgnum" onclick="gotoPage(1)">1</button>`; if(lo>2) h += `<span class="pgdots">&hellip;</span>`; }
  for(let p=lo;p<=hi;p++)
    h += `<button class="pgnum ${p===cur?'cur':''}" onclick="gotoPage(${p})">${p}</button>`;
  if(hi<pages){ if(hi<pages-1) h += `<span class="pgdots">&hellip;</span>`; h += `<button class="pgnum" onclick="gotoPage(${pages})">${pages}</button>`; }
  h += `<button class="pgnav" ${cur===pages?'disabled':''} onclick="gotoPage(${cur+1})">Next &raquo;</button>`;
  el.innerHTML = h;
}

function renderMeta(d){
  const total = d.total||d.count||0;
  const limit = d.limit||d.count||0;
  const off = d.offset||0;
  const from = off+1;
  const to = off + d.count;
  let h = total ? `Showing ${from}&ndash;${to} of ${total} result${total===1?'':'s'}`
                : `${d.count||0} result${(d.count||0)===1?'':'s'}`;
  h += ` for &#8220;${esc(d.query)}&#8221;`;
  if(d.sort==='mtime') h += ` &middot; <span class="rw">sorted newest first</span>`;
  if(d.since) h += ` &middot; modified since ${esc(d.since)}`;
  if(d.llm_meta && d.llm_meta.window_widened)
    h += ` &middot; <span class="rw">${esc(d.llm_meta.window_widened)}</span>`;
  if(d.rewritten_query && d.rewritten_query!==d.query)
    h += ` &middot; <span class="rw">searched as &#8220;${esc(d.rewritten_query)}&#8221;</span>`;
  $('meta').innerHTML = h;
}

function renderResults(rows){
  if(!rows.length){ $('results').innerHTML='<div class="empty">No files matched. Try different words, or enable &#8220;AI answer&#8221; to let the LLM interpret it.</div>'; return; }
  const base = document.location.origin;
  let h = `<table><thead><tr><th>File</th><th>Category</th><th>Platform</th><th>Version</th><th>Modified</th><th>Size</th><th>Checksums</th><th></th></tr></thead><tbody>`;
  for(const r of rows){
    const dl = `/files/${encodeURIComponent(r.path)}`;
    const purl = parentHref(r.path);
    // Checksums come from sibling sidecar files (<name>.sha256 / .md5)
    // read at index time. Truncated in the cell; full value in the tooltip.
    let cs = '';
    if(r.sha256){
      const tip = r.sha256;
      cs += `<span class="chksum" title="sha256: ${esc(tip)}">sha256 ${esc(tip.slice(0,8))}&#8230;</span>`;
    }
    if(r.md5){
      const tip = r.md5;
      cs += `<span class="chksum" title="md5: ${esc(tip)}">md5 ${esc(tip.slice(0,8))}&#8230;</span>`;
    }
    if(!cs) cs = '&ndash;';
    h += `<tr>
      <td><div class="name">${esc(r.name)}</div><div class="path">${esc(r.path)}</div>
      <a class="parent" href="${esc(purl)}" title="Browse this file's folder in the store">Parent folder</a></td>
      <td><span class="badge cat">${esc(r.category)}</span></td>
      <td>${esc(r.platform)}${r.arch!=='unknown'?` <span class="badge">${esc(r.arch)}</span>`:''}</td>
      <td>${esc(r.version)||'&ndash;'}</td>
      <td class="size">${fmtDate(r.mtime)}</td>
      <td class="size">${fmtSize(r.size)}</td>
      <td class="size">${cs}</td>
      <td><a class="dl" href="${dl}" download>Download</a></td>
      </tr>`;
  }
  h += '</tbody></table>';
  $('results').innerHTML = h;
}

// A new search always restarts at page 1. (gotoPage sets `page` for the
// active result set; if we didn't reset it here, a fresh query submitted
// while sitting on, say, page 3 would fetch offset (3-1)*limit and show the
// middle of the new result set instead of its top.)
$('search-form').onsubmit = e => { e.preventDefault(); page = 1; doSearch(); };
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
