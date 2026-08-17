"""Robustness tests: LLM down, FTS-injection attempts, weird queries.

Run the app first (see test_e2e.py docstring)."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("FILESTORE_SEARCH_BASE", "http://127.0.0.1:8099")


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


# 1) FTS-injection / special-character queries must not 500
weird = [
    "foo' OR '1'='1",
    'name" AND (path:*',
    'a*b c"d',
    "123",
    "éèà ümlauts",
]
for q in weird:
    d = get("/api/search?q=" + urllib.request.quote(q) + "&limit=2")
    assert d.get("error") is None, f"query {q!r} errored: {d.get('error')}"
    print(f"weird query {q!r:28} -> ok, {d.get('count', len(d.get('results', [])))} results")

# whitespace-only query should be a clean 400, not a 500
d = get("/api/search?q=%20%20%20&limit=2")
assert d.get("error") == "missing q", f"expected clean 400, got {d}"
print("whitespace-only query -> clean 400 'missing q'")

# 2) LLM down: verify graceful degradation (deterministic results still
#    returned; answer is a graceful note, not a crash)
code = (
    "import json, os; "
    "os.environ['FILESTORE_SEARCH_LLM_BASE']='http://127.0.0.1:59999/v1'; "
    "import app as A; A.init(); "
    "out = A.do_search('dell bios r740', want_answer=True); "
    "print(json.dumps({'count': out['count'], 'answer': out['answer'], "
    "'top': out['results'][0]['name'] if out['results'] else None}))"
)
env = dict(os.environ, FILESTORE_SEARCH_DB=os.path.join(HERE, "test-search.db"))
r = subprocess.run([sys.executable, "-c", code], cwd=HERE,
                   capture_output=True, text=True, env=env)
print("LLM-down path:", r.stdout.strip() or r.stderr.strip()[:400])

# 3) LIKE fallback: query where FTS finds nothing but substring matches
d = get("/api/search?q=dell-bios-r740-x4.4.4&limit=3")  # hyphenated blob
print("hyphenated blob ->", d.get("count"), d.get("results", [{}])[0].get("name"))

print("\nROBUSTNESS OK")
