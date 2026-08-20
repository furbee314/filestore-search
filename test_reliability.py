"""Reliability tests: deterministic rewrites, guardrails, stable ranking.

Self-contained: builds a throwaway index from ./data and exercises the
search layers directly plus app.do_search with a stubbed (flaky) LLM client,
so no Ollama / mock server / running app is needed.

Run:
  python3 test_reliability.py
"""
import os
import sys
import tempfile
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

# 2c. ...but an explicit time phrase in the user request IS honored
d = app.do_search("dell bios since 2099-01-01", limit=5)
assert d["total"] == 0, \
    f"explicit 'since 2099-01-01' should zero the set, got total={d['total']}"
assert "since_ignored" not in (d["llm_meta"] or {})
print("guardrail: explicit time phrase honored (total=0 for since 2099): OK")

# 2d. db-level identifier recovery
assert db.recover_identifiers("dell r740 with 6230 cpu", "dell bios") \
    == ["r740", "6230"]
assert db.recover_identifiers("dell r740", "dell r740 bios") == []
print("recover_identifiers unit cases: OK")

print("\nALL OK")
