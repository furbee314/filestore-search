"""Reliability tests: deterministic rewrites, guardrails, stable ranking.

Self-contained: builds a throwaway index from ./data and exercises the
search layers directly plus app.do_search with a stubbed (flaky) LLM client,
so no Ollama / mock server / running app is needed.

Run:
  python3 test_reliability.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DB_PATH = os.path.join(tempfile.gettempdir(), "fss-reliability.db")
for p in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass

os.environ["FILESTORE_SEARCH_DATA"] = os.path.join(HERE, "data")
os.environ["FILESTORE_SEARCH_DB"] = DB_PATH
os.environ["FILESTORE_SEARCH_LLM_DISABLE"] = "1"

import db  # noqa: E402
import app  # noqa: E402
from config import load_config  # noqa: E402

CFG = load_config()
db.full_reindex(CFG, db.connect(CFG), quiet=True)
con = db.connect(CFG, check_same_thread=False)

# ---------------------------------------------------------------------------
# 3. ranking: stopword stripping, column weights, stable ordering
# ---------------------------------------------------------------------------
print("== ranking ==")
# stop words must not distort the FTS query
assert db.fts_query("firmware for my dell r740") == '"firmware" OR "dell" OR "r740"'
assert db.fts_query("the a") == '"the" OR "a"'          # all-stop: still searches

# with/without filler words -> same top result (filler no longer distorts)
top = lambda q: [r["id"] for r in db.search(con, q, limit=5)]
assert top("dell r740 bios") == top("dell r740 bios for me"), \
    "stop words changed the result order"
print("stop words no longer change the order: OK")

# column weights: a distinctive number in the filename must rank that file
# above a file that only matches it in the generated description
rows = db.search(con, "raid 9560 firmware", limit=5)
assert rows and "9560" in rows[0]["name"], \
    f"9560 file should rank first, got {[r['name'] for r in rows]}"
print("column weighting: " + rows[0]["name"] + " ranks first: OK")

# stability: repeated identical queries -> identical result sets
q = "firmware for my Dell R740"
runs = [db.search(con, q, limit=10) for _ in range(3)]
assert all(runs[0] == r for r in runs), "relevance results are not stable"
mruns = [db.search(con, q, limit=10, sort="mtime") for _ in range(3)]
assert all(mruns[0] == r for r in mruns), "mtime results are not stable"
print("repeated searches are stable (relevance + mtime): OK")

# paging stays consistent (no duplicates/holes across pages of a stable set)
seen = []
for off in (0, 5, 10):
    seen += [r["id"] for r in db.search(con, "dell", limit=5, offset=off)]
assert len(seen) == len(set(seen)), f"paged results overlap: {seen}"
print("pagination over a stable set has no duplicates: OK")

# ---------------------------------------------------------------------------
# structured OR: 'X or Y for Z' -> Z AND (X OR Y), not the flat union
# ---------------------------------------------------------------------------
print("\n== structured OR ==")
# expression shape: AND terms required, OR group as alternative
assert db.match_expressions("dell r740", ["bios", "firmware"]) == [
    '"dell" AND "r740" AND ("bios" OR "firmware")',
    '"dell" OR "r740" OR "bios" OR "firmware"'
], db.match_expressions("dell r740", ["bios", "firmware"])
# a term that is already required (AND) must not also sit in the OR group
assert db.match_expressions("dell r740", ["bios", "r740"]) == [
    '"dell" AND "r740" AND ("bios")',
    '"dell" OR "r740" OR "bios"'
], db.match_expressions("dell r740", ["bios", "r740"])
assert db.match_expressions("the a", []) == ['"the" AND "a"', '"the" OR "a"']
assert db.fts_query("firmware for my dell r740") == '"firmware" OR "dell" OR "r740"'
print("expression shapes: OK")

# the production symptom: 'bios or firmware for dell r740'. The flat union
# of every word returns files that mention ANY of them (unrelated results);
# the structured set is exactly the dell-r740 files that have bios or
# firmware — and its total must equal what search() pages over.
flat_ids = set(r["id"] for r in db.search(con, "bios or firmware dell r740", limit=100))
or_ids = [r["id"] for r in db.search(con, "dell r740", limit=100,
                                     or_terms=["bios", "firmware"])]
assert or_ids, "structured OR returned no results"
assert flat_ids - set(or_ids), \
    "flat union returned files the structured OR correctly excludes"
for r in db.search(con, "dell r740", limit=100, or_terms=["bios", "firmware"]):
    low = (r["name"] + " " + (r["description"] or "")).lower()
    assert ("bios" in low or "firmware" in low or "ipmi" in low), \
        f"unrelated file leaked into OR results: {r['name']}"
assert db.count_matches(con, "dell r740", ["bios", "firmware"]) == len(or_ids), \
    "total must describe the same set as the results"
print(f"structured OR: {len(or_ids)} relevant vs {len(flat_ids)} under flat "
      f"union; total consistent: OK")

# structured path is a no-op when no OR group is given (back-compat)
assert [r["id"] for r in db.search(con, "dell r740", limit=10)] == \
       [r["id"] for r in db.search(con, "dell r740", limit=10, or_terms=None)]
assert [r["id"] for r in db.search(con, "dell r740", limit=10,
                                   or_terms=[])] == \
       [r["id"] for r in db.search(con, "dell r740", limit=10)]
print("no-op without OR group: OK")

# structured search that matches nothing falls back to flat OR, not to []
res = db.search(con, "zzz nonexistent", or_terms=["bios"])
assert res, "empty-structured must fall back to flat OR"
assert db.count_matches(con, "zzz nonexistent", ["bios"]) > 0
print("empty-structured fallback: OK")

# count_filtered must accept a category/platform filter without 500'ing on a
# missing WHERE clause (the store-wide 'latest' fallback path)
total_all = db.count_filtered(con)
total_cat = db.count_filtered(con, category="firmware")
total_cat_plat = db.count_filtered(con, category="firmware", platform="linux")
assert total_cat_plat <= total_cat <= total_all
assert total_all == con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
print(f"count_filtered honors filters without error "
      f"(all={total_all}, firmware={total_cat}, firmware+linux={total_cat_plat}): OK")

# re-ranker sees OR terms: an R740 file whose only relevant hit is
# 'firmware' (the alternative) still ranks with the R740 files
top = [r["name"] for r in db.search(con, "dell r740", limit=10,
                                    or_terms=["bios", "firmware"])]
assert any("r740" in n for n in top[:2]), \
    f"R740 files should dominate the OR results, got {top}"
print("re-ranker includes OR terms: OK")

# ---------------------------------------------------------------------------
# checksum sidecars (.sha128/.sha256/...) must never enter the index
# ---------------------------------------------------------------------------
leaked = con.execute(
    "SELECT COUNT(*) FROM files WHERE path LIKE '%.sha1' "
    "OR path LIKE '%.sha256' OR path LIKE '%.sha128' "
    "OR path LIKE '%.sha512' OR path LIKE '%.md5'").fetchone()[0]
assert leaked == 0, f"checksum sidecars leaked into the index: {leaked}"
total_no_sidecars = total_all
on_disk = sum(len(fs) for _, _, fs in os.walk(os.path.join(HERE, "data")))
assert total_no_sidecars < on_disk, \
    "expected sidecars on disk but none indexed (is the exclusion a no-op?)"
print(f"checksum sidecars excluded "
      f"({on_disk} on disk, {total_no_sidecars} indexed): OK")

# ---------------------------------------------------------------------------
# checksum sidecars: their digest values must land in the index + results
# ---------------------------------------------------------------------------
print("\n== sidecar digests in results ==")
import hashlib  # noqa: E402

def digest_of(relpath, algo):
    with open(os.path.join(HERE, "data", relpath), "rb") as f:
        return hashlib.new(algo, f.read()).hexdigest()

iso_rel = "isos/rhel-9.4-x86_64-dvd.iso"
row = con.execute(
    "SELECT sha256, md5 FROM files WHERE path=?", (iso_rel,)).fetchone()
assert row[0] == digest_of(iso_rel, "sha256"), \
    f"stored sha256 {row[0][:12]}... != actual {digest_of(iso_rel, 'sha256')[:12]}..."
assert row[1] == "", f"no .md5 sidecar expected for {iso_rel}, got {row[1]!r}"
print(f"sha256 stored matches the file's real digest ({iso_rel}): OK")

# md5 sidecar (coreutils layout: "DIGEST  filename")
lenovo_rel = ("Software_Library/Admin_Software/lenovo/"
              "lenovo-bios-t14-gen3-mncn25ww.zip")
row = con.execute(
    "SELECT md5 FROM files WHERE path=?", (lenovo_rel,)).fetchone()
assert row[0] == digest_of(lenovo_rel, "md5"), \
    "stored md5 does not match the .md5 sidecar content"
print(f"md5 stored matches the .md5 sidecar ({lenovo_rel}): OK")

# every sidecar on disk must have populated the matching column
for suffix, col in ((".sha256", "sha256"), (".md5", "md5")):
    expect = 0
    for dp, _dn, fns in os.walk(os.path.join(HERE, "data")):
        for fn in fns:
            if fn.endswith(suffix):
                main = fn[:-len(suffix)]
                r = con.execute(
                    "SELECT 1 FROM files WHERE path=? COLLATE NOCASE",
                    (os.path.relpath(os.path.join(dp, main),
                                     os.path.join(HERE, "data")),)).fetchone()
                if r:
                    expect += 1
    got = con.execute(
        f"SELECT COUNT(*) FROM files WHERE {col} != ''").fetchone()[0]
    assert got == expect, f"expected {expect} files with {col}, got {got}"
print(f"all sidecars with an indexed main file are stored "
      f"(sha256 + md5): OK")

# unit: reader handles missing / garbage sidecars without raising
assert db.read_sidecar_digests(os.path.join(HERE, "data"),
                               "isos/no-such-file.iso") == ("", "")
gdir = os.path.join(tempfile.gettempdir(), "fss-sidecar-garbage")
os.makedirs(gdir, exist_ok=True)
g_main = os.path.join(gdir, "g.iso")
g_bad = os.path.join(gdir, "g.iso.sha256")
open(g_main, "wb").close()
open(g_bad, "w").write("not a digest at all\n")
assert db.read_sidecar_digests(gdir, "g.iso") == ("", "")
os.unlink(g_bad)
# valid but wrong-length digest is rejected, not stored
open(g_bad, "w").write("beef\n")
assert db.read_sidecar_digests(gdir, "g.iso") == ("", "")
os.unlink(g_bad)
os.unlink(g_main)
os.rmdir(gdir)
print("sidecar reader: missing/garbage/wrong-length digests -> '': OK")

# search() results carry the values (both FTS and LIKE paths)
by_path = {r["path"]: r for r in db.search(con, "rhel dvd", limit=50)}
r = by_path.get(iso_rel)
assert r and r.get("sha256") == digest_of(iso_rel, "sha256"), \
    f"search result missing sha256 for {iso_rel}: {r and r.get('sha256')}"
assert r.get("md5") == "", "no md5 expected for the iso"
by_path = {r["path"]: r for r in db.search(con, "bios t14 gen3",
                                           limit=50)}
r = by_path.get(lenovo_rel)
assert r and r.get("md5") == digest_of(lenovo_rel, "md5"), \
    f"search result missing md5 for {lenovo_rel}"
print("search() results include sha256/md5 where sidecars exist: OK")

# newest() carries the values too
rows = db.newest(con, 200)
by_path = {r["path"]: r for r in rows}
assert by_path[iso_rel]["sha256"] == digest_of(iso_rel, "sha256")
assert by_path[lenovo_rel]["md5"] == digest_of(lenovo_rel, "md5")
print("newest() results include sha256/md5: OK")

# files without sidecars must carry empty strings, not None
no_side = con.execute(
    "SELECT COUNT(*) FROM files").fetchone()[0]
empty = con.execute(
    "SELECT COUNT(*) FROM files WHERE sha256 = '' AND md5 = ''").fetchone()[0]
assert empty > 0, "expected most files to lack sidecars"
res = db.search(con, "readme", limit=5)
assert all("sha256" in r and "md5" in r for r in res)
print(f"result dicts always carry sha256/md5 keys "
      f"({empty} of {no_side} files have none): OK")

# ---------------------------------------------------------------------------
# deliverable priority: installables rank above their paperwork
# ---------------------------------------------------------------------------
def prio(name):
    r = con.execute("SELECT priority FROM files WHERE name=?", (name,)).fetchone()
    return r[0] if r else None

assert prio("readme-how-to-install-drivers.txt") == 0, \
    f"doc should be priority 0, got {prio('readme-how-to-install-drivers.txt')}"
assert prio("SymantecLinuxInstaller") == 3, \
    f"no-ext installer should be priority 3, got {prio('SymantecLinuxInstaller')}"
assert prio("install-symantec-endpoint.sh") == 3, \
    f".sh installer should be priority 3, got {prio('install-symantec-endpoint.sh')}"
assert prio("dell-om-agent-7.4.0-win-x64.msu") == 3, \
    f".msu patch should be priority 3, got {prio('dell-om-agent-7.4.0-win-x64.msu')}"
assert prio("repomd.xml") == 1, \
    f"repo metadata should be priority 1, got {prio('repomd.xml')}"
assert prio("Packages.gz") == 1, \
    f"repo manifest should be priority 1, got {prio('Packages.gz')}"
print("priority stored per file type (rpm/.msu/.sh/no-ext=3, metadata=1, doc=0): OK")

# an installable that matches the same tokens must outrank the readme of the
# same software (matched-token count is tied; priority breaks it)
rows = db.search(con, "install", limit=50)
names = [r["name"] for r in rows]
need = ("install-symantec-endpoint.sh", "readme-how-to-install-drivers.txt")
assert all(n in names for n in need), \
    f"expected both installable and readme in the pool, got {names[:10]}"
assert names.index(need[0]) < names.index(need[1]), \
    f"installable should rank above its paperwork: {names[:8]}"
print("installable ranks above its paperwork at equal relevance: OK")

# ---------------------------------------------------------------------------
# 1+2. app-level: rewrite cache + guardrails, with a stubbed flaky LLM
# ---------------------------------------------------------------------------
print("\n== rewrite cache + guardrails (stubbed LLM) ==")


class StubLLM:
    """Minimal LLMClient stand-in: enabled, with pluggable rewrite_query."""
    enabled = True
    timeout = 5
    rewrite_query: Optional[Callable[..., Any]]
    cache_key_prefix = ""

    def __init__(self):
        self.rewrite_query = None

    def answer(self, *a, **k):
        return ""


app.init(CFG)
app.STATE["llm"] = StubLLM()
calls = {"n": 0}


class FlakyRewriter:
    """Returns a *different* rewrite on every call (simulates an unpinned
    3B model) — the cache must absorb that."""

    def __init__(self):
        self.variants = [
            {"query": "dell r740 bios", "category": None, "platform": None,
             "version": None, "since": None},
            {"query": "dell firmware", "category": "firmware",
             "platform": None, "version": None, "since": None},
        ]

    def rewrite_query(self, q, timeout=None):
        i = calls["n"] % len(self.variants)
        calls["n"] += 1
        return dict(self.variants[i])


app.STATE["llm"].rewrite_query = FlakyRewriter().rewrite_query

# 1. cache: same question twice -> LLM called once, identical rewrites
app._REWRITE_CACHE.clear()
d1 = app.do_search("dell r740 bios please", limit=5)
d2 = app.do_search("dell r740 bios please", limit=5)
assert calls["n"] == 1, f"LLM called {calls['n']} times, expected 1 (cache miss)"
assert d1["rewritten_query"] == d2["rewritten_query"]
assert [r["id"] for r in d1["results"]] == [r["id"] for r in d2["results"]]
assert d1["llm_used"] and d2["llm_used"]
print(f"cache: same question -> LLM hit once, identical rewrites "
      f"('{d1['rewritten_query']}'): OK")

# 2a. identifier guardrail: LLM drops 'r740' -> merged back into the query
app._REWRITE_CACHE.clear()
calls.update({"n": 0})


class Dropper:
    def rewrite_query(self, q, timeout=None):
        calls["n"] += 1
        return {"query": "dell bios firmware", "category": "firmware",
                "platform": None, "version": None, "since": None}


app.STATE["llm"].rewrite_query = Dropper().rewrite_query
d = app.do_search("dell r740 bios", limit=5)
assert "r740" in d["rewritten_query"].lower(), \
    f"'r740' was dropped by the rewriter and not recovered: {d['rewritten_query']}"
assert "dropped_identifiers" in (d["llm_meta"] or {}), \
    f"expected dropped_identifiers in llm_meta, got {d['llm_meta']}"
r740 = [r["id"] for r in d["results"] if "r740" in r["name"].lower()]
assert r740, f"R740 file not in results after identifier recovery: {d['results']}"
print(f"guardrail: LLM dropped 'r740' -> recovered into "
      f"'{d['rewritten_query']}', R740 file still returned: OK")

# 2b. since guardrail: LLM-invented time window with NO time phrase in the
# user request must be ignored (use 2099 which would zero the set otherwise)
app._REWRITE_CACHE.clear()
calls.update({"n": 0})


class TimeHallucinator:
    def rewrite_query(self, q, timeout=None):
        calls["n"] += 1
        return {"query": "dell bios", "category": None, "platform": None,
                "version": None, "since": "2099-01-01"}


app.STATE["llm"].rewrite_query = TimeHallucinator().rewrite_query
full = db.count_matches(con, "dell bios")
d = app.do_search("find the dell bios", limit=5)
assert "since_ignored" in (d["llm_meta"] or {}), \
    f"hallucinated 'since' was not flagged: {d['llm_meta']}"
assert d["total"] == full, \
    f"ignored-since total {d['total']} != full total {full}"
print(f"guardrail: hallucinated since=2099 ignored, total intact ({full}): OK")

# 2c. ...but an explicit time phrase in the user request IS honored —
# resolved from the system clock, not the LLM (clean rewriter here so no
# LLM-invented since is in the mix)
app._REWRITE_CACHE.clear()


class CleanRewriter:
    def rewrite_query(self, q, timeout=None):
        return {"query": "dell bios", "category": None, "platform": None,
                "version": None, "since": None}


app.STATE["llm"].rewrite_query = CleanRewriter().rewrite_query
d = app.do_search("dell bios since 2099-01-01", limit=5)
assert d["total"] == 0, \
    f"explicit 'since 2099-01-01' should zero the set, got total={d['total']}"
assert d["since"] == "2099-01-01", f"since not surfaced: {d.get('since')}"
print("guardrail: explicit time phrase honored via system clock: OK")

# 2d. db-level identifier recovery
assert db.recover_identifiers("dell r740 with 6230 cpu", "dell bios") \
    == ["r740", "6230"]
assert db.recover_identifiers("dell r740", "dell r740 bios") == []
print("recover_identifiers unit cases: OK")

# ---------------------------------------------------------------------------
# 5. time phrases: resolved from the SYSTEM clock, never the LLM
# ---------------------------------------------------------------------------
print("\n== time phrases (system clock) ==")
N0 = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)  # Wednesday
rp = lambda s: db.resolve_time_phrase(s, now=N0)

assert rp("since 2024-05-01") == "2024-05-01"
assert rp("updated on 2024-05-01") == "2024-05-01"
assert rp("bios 2024.05.01") == "2024-05-01"
assert rp("last 30 days") == "2026-07-20"
assert rp("past 3 months") == "2026-05-19"
assert rp("last 2 years") == "2024-08-19"
assert rp("last week") == "2026-08-12"
assert rp("this week") == "2026-08-17"          # Monday
assert rp("this month") == "2026-08-01"
assert rp("last month") == "2026-07-01"
assert rp("this quarter") == "2026-07-01"
assert rp("last quarter") == "2026-04-01"
assert rp("this year") == "2026-01-01"
assert rp("last year") == "2025-01-01"
assert rp("today") == "2026-08-19"
assert rp("yesterday") == "2026-08-18"
assert rp("since March") == "2026-03-01"
assert rp("updated in June 2023") == "2023-06-01"
assert rp("firmware from january") is None      # bare month needs in/since
assert rp("dell r740 bios") is None             # no time phrase
assert rp("the latest update") is None          # recency word, no window
print("resolve_time_phrase unit cases: OK")

# month/year arithmetic edges
assert db.resolve_time_phrase("last month",
                              now=datetime(2026, 3, 1, tzinfo=timezone.utc)) \
    == "2026-02-01"
assert db.resolve_time_phrase("last 2 months",
                              now=datetime(2026, 1, 31, tzinfo=timezone.utc)) \
    == "2025-11-30"
assert db.resolve_time_phrase("last month",
                              now=datetime(2026, 1, 15, tzinfo=timezone.utc)) \
    == "2025-12-01"
assert db.resolve_time_phrase("since July",
                              now=datetime(2026, 3, 1, tzinfo=timezone.utc)) \
    == "2025-07-01"                            # future month -> previous year
assert db.resolve_time_phrase("last month",
                              now=datetime(2026, 3, 31, tzinfo=timezone.utc)) \
    == "2026-02-01"
print("month/year edges: OK")

# End-to-end: two synthetic files, one this month, one old (400 days back)
now = datetime.now(timezone.utc)
# fresh_ts: the 2nd of this month — always inside "this month" and
# "last 30 days", and always more than 7 days old (so outside "last
# week"), regardless of what day today is.
fresh_ts = now.replace(day=2).timestamp()
old_ts = now.timestamp() - 400 * 86400
tt_dir = os.path.join(CFG["data_dir"], "_timetest")
os.makedirs(tt_dir, exist_ok=True)
tt_fresh = os.path.join(tt_dir, "timetest-fresh-9.9.9-1.x86_64.rpm")
tt_old = os.path.join(tt_dir, "timetest-old-1.0.0-1.x86_64.rpm")
for p, ts in ((tt_fresh, fresh_ts), (tt_old, old_ts)):
    with open(p, "wb") as f:
        f.write(b"x")
    os.utime(p, (ts, ts))
db.full_reindex(CFG, con, quiet=True)
full = db.count_matches(con, "timetest")
assert full == 2, f"expected 2 timetest files, got {full}"
assert db.count_matches(con, "timetest",
                        min_mtime=db.since_to_ts(
                            db.resolve_time_phrase("this month"))) == 1, \
    "test setup: fresh file must fall inside 'this month'"
assert db.count_matches(con, "timetest",
                        min_mtime=db.since_to_ts(
                            db.resolve_time_phrase("last 30 days"))) == 1, \
    "test setup: fresh file must fall inside 'last 30 days'"
assert db.count_matches(con, "timetest",
                        min_mtime=db.since_to_ts(
                            db.resolve_time_phrase("last week"))) == 0, \
    "test setup: no timetest file may fall inside 'last week'"


class LLMDateLiar:
    """Simulates the bug: the model invents a wrong 'since' date."""
    def rewrite_query(self, q, timeout=None):
        return {"query": "timetest", "category": None, "platform": None,
                "version": None, "since": "2099-01-01"}


app.STATE["llm"].rewrite_query = LLMDateLiar().rewrite_query
app._REWRITE_CACHE.clear()

# 5a. "this month" -> system-clock window; the LLM's bogus 2099 date must
#     NOT be what filters the results
d = app.do_search("timetest this month", limit=5)
assert d["total"] == 1, \
    f"'this month' should return only the fresh file, total={d['total']}"
assert d["since"] and d["since"].startswith(now.strftime("%Y-%m") + "-01"), \
    f"since should be start of this month, got {d['since']}"
assert d["results"][0]["name"].startswith("timetest-fresh")
assert "since_ignored" in (d["llm_meta"] or {}), \
    f"LLM-invented date should be flagged: {d['llm_meta']}"
print(f"e2e: 'this month' -> system clock ({d['since']}), "
      f"LLM-invented date ignored: OK")

# 5b. "last 30 days" -> window of 30 days
d = app.do_search("timetest last 30 days", limit=5)
assert d["total"] == 1, f"'last 30 days' total={d['total']}"
assert d["results"][0]["name"].startswith("timetest-fresh")
print("e2e: 'last 30 days' -> fresh file only: OK")

# 5c. no time phrase -> LLM date ignored, full set returned
app._REWRITE_CACHE.clear()
d = app.do_search("find the timetest package", limit=5)
assert d["total"] == full, \
    f"no time phrase: LLM date must not shrink the set, total={d['total']}"
print("e2e: no time phrase -> LLM date ignored, full set: OK")

# 5d. explicit date typed by the user still works (verbatim, no clock math)
app._REWRITE_CACHE.clear()
d = app.do_search("timetest since 2020-01-01", limit=5)
assert d["total"] == full, \
    f"'since 2020-01-01' should keep both files, total={d['total']}"
print("e2e: explicit user date honored: OK")

# 5e. empty window (nothing fresh enough) -> widened to most recent matches,
#     with an explanatory flag instead of a silent empty page
d = app.do_search("timetest last week", limit=5)
assert d["total"] == full, \
    f"'last week' found nothing; window should widen, total={d['total']}"
assert "window_widened" in (d["llm_meta"] or {}), \
    f"widening should be flagged: {d['llm_meta']}"
assert not d.get("since"), \
    f"widened window should not report a since filter: {d.get('since')}"
print("e2e: empty window widened with flag: OK")

# 5f. ...but a strict user-typed future date is NOT widened
app._REWRITE_CACHE.clear()
d = app.do_search("timetest since 2099-01-01", limit=5)
assert d["total"] == 0, \
    f"strict 'since 2099-01-01' must stay zero, got total={d['total']}"
assert "window_widened" not in (d["llm_meta"] or {}), \
    f"strict filter must not be widened: {d['llm_meta']}"
print("e2e: strict user date not widened: OK")

# cleanup: remove the synthetic files and reindex
for p in (tt_fresh, tt_old):
    os.unlink(p)
os.rmdir(tt_dir)
db.full_reindex(CFG, con, quiet=True)

# ---------------------------------------------------------------------------
# schema migration: a v2 database gains sha256/md5 and gets backfilled
# ---------------------------------------------------------------------------
print("\n== v2 -> v3 migration ==")
mig_db = os.path.join(tempfile.gettempdir(), "fss-mig-test.db")
for p in (mig_db, mig_db + "-wal", mig_db + "-shm"):
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
mig_data = os.path.join(tempfile.gettempdir(), "fss-mig-data")
shutil.rmtree(mig_data, ignore_errors=True)
os.makedirs(mig_data)
mig_iso = os.path.join(mig_data, "mig.iso")
mig_iso_data = b"migration-test-payload" * 100
with open(mig_iso, "wb") as f:
    f.write(mig_iso_data)
mig_sha = hashlib.sha256(mig_iso_data).hexdigest()
with open(mig_iso + ".sha256", "w") as f:
    f.write(mig_sha + "  mig.iso\n")

# build a v2-shaped DB: priority column, no sha256/md5, schema_version=2
con2 = sqlite3.connect(mig_db)
con2.executescript(
    """CREATE TABLE files (
         id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
         name TEXT NOT NULL, category TEXT NOT NULL, platform TEXT NOT NULL,
         arch TEXT NOT NULL DEFAULT 'unknown', file_type TEXT NOT NULL,
         version TEXT NOT NULL DEFAULT '', vendor TEXT NOT NULL DEFAULT '',
         description TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL,
         mtime REAL NOT NULL, indexed_at REAL NOT NULL,
         priority INTEGER NOT NULL DEFAULT 3);
       CREATE VIRTUAL TABLE files_fts USING fts5(
         path, name, version, description, vendor,
         content='', tokenize='unicode61');
       CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
con2.execute(
    "INSERT INTO files (path, name, category, platform, file_type, "
    "size, mtime, indexed_at, priority) VALUES "
    "('mig.iso','mig.iso','iso','unknown','iso',?,0,0,3)",
    (len(mig_iso_data),))
con2.execute("INSERT INTO files_fts(rowid, path, name, version, description, "
             "vendor) VALUES (1,'mig.iso','mig.iso','','','')")
con2.execute("INSERT INTO meta(key, value) VALUES ('schema_version','2')")
con2.commit()
con2.close()

mig_cfg = dict(CFG)
mig_cfg["data_dir"] = mig_data
mig_cfg["db_path"] = mig_db
con3 = db.connect(mig_cfg)  # triggers ensure_schema -> _migrate
cols = {r[1] for r in con3.execute("PRAGMA table_info(files)")}
assert {"sha256", "md5"} <= cols, f"migration did not add columns: {cols}"
ver = con3.execute(
    "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
assert str(ver) == "3", f"schema_version not advanced: {ver}"
val = con3.execute(
    "SELECT sha256 FROM files WHERE path='mig.iso'").fetchone()[0]
assert val == mig_sha, \
    f"backfilled sha256 {val[:12]}... != {mig_sha[:12]}..."
# a second connect is a no-op (idempotent)
con3b = db.connect(mig_cfg)
val2 = con3b.execute(
    "SELECT sha256 FROM files WHERE path='mig.iso'").fetchone()[0]
assert val2 == mig_sha
con3b.close()
con3.close()
shutil.rmtree(mig_data, ignore_errors=True)
for p in (mig_db, mig_db + "-wal", mig_db + "-shm"):
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
print("v2 -> v3: columns added, sidecars backfilled, idempotent: OK")

print("\nALL OK")
