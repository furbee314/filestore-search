"""End-to-end test against the running web app + mock LLM.

Run the app first:
  python3 make_test_data.py data
  FILESTORE_SEARCH_DATA=$(pwd)/data FILESTORE_SEARCH_DB=$(pwd)/test-search.db \
    python3 -m cli index
  python3 mock_llm.py --port 8901 &
  FILESTORE_SEARCH_DATA=$(pwd)/data FILESTORE_SEARCH_DB=$(pwd)/test-search.db \
    FILESTORE_SEARCH_LLM_BASE=http://127.0.0.1:8901/v1 FILESTORE_SEARCH_LLM_MODEL=mock \
    python3 -m app --port 8099
"""
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("FILESTORE_SEARCH_BASE", "http://127.0.0.1:8099")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


print("== /api/health ==")
h = get("/api/health")
print(json.dumps(h, indent=2))

print("\n== /api/facets ==")
f = get("/api/facets")
print("total:", f["total"], "| categories:", f["categories"],
      "| platforms:", f["platforms"])

print("\n== search: 'firmware for my Dell R740' (deterministic, no LLM) ==")
d = get("/api/search?q=firmware%20for%20my%20Dell%20R740&limit=4")
print("count:", d["count"], "| llm_used:", d["llm_used"])
for r in d["results"]:
    print("  ", r["name"], "|", r["category"], "| v" + (r["version"] or "-"))

print("\n== search: 'nvidia driver for ubuntu 24.04' WITH AI answer ==")
d = get("/api/search?q=nvidia%20driver%20for%20ubuntu%2024.04&limit=6&answer=1")
print("rewritten_query:", d["rewritten_query"])
print("llm_meta:", d["llm_meta"])
print("ANSWER:", d["answer"])

print("\n== search: 'kernel update for RHEL 9' WITH AI answer ==")
d = get("/api/search?q=kernel%20update%20for%20RHEL%209&limit=6&answer=1")
print("rewritten_query:", d["rewritten_query"])
print("ANSWER:", d["answer"])

print("\n== search: 'RHEL 9 install ISO' (iso category + platform) ==")
d = get("/api/search?q=RHEL%209%20install%20ISO&limit=5&answer=1")
print("rewritten_query:", d["rewritten_query"], "| meta:", d["llm_meta"])
isos = [r["name"] for r in d["results"] if r["category"] == "iso"]
assert isos, f"expected an iso result, got {d['results']}"
print("top iso:", isos[0])
print("ANSWER:", (d["answer"] or "")[:160])

print("\n== search: gibberish (should still return something / not crash) ==")
d = get("/api/search?q=blorp%20xyzzy&limit=3")
print("count:", d["count"])

print("\nALL OK")
