"""Verify download endpoint, UI page, and incremental refresh.

Run the app first (see test_e2e.py docstring)."""
import os
import shutil
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("FILESTORE_SEARCH_BASE", "http://127.0.0.1:8099")


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


# UI page
st, hdr, body = get("/")
assert st == 200 and b"File Store Search" in body, "UI page failed"
print("UI page OK,", len(body), "bytes, content-type:", hdr.get("Content-Type"))

# css + js
for p in ("/app.css", "/app.js"):
    st, hdr, body = get(p)
    assert st == 200, f"{p} failed: {st}"
print("static assets OK")

# download a file fully
path = "/files/" + urllib.request.quote(
    "Software_Library/Admin_Software/dell/dell-bios-r740-x4.4.4-a01.zip")
st, hdr, body = get(path)
assert st == 200 and len(body) > 0, f"download failed: {st} {len(body)}"
print("full download OK,", len(body), "bytes, type:", hdr.get("Content-Type"))

# ranged download
st, hdr, body = get(path, {"Range": "bytes=0-99"})
assert st == 206 and len(body) == 100, f"range failed: {st} {len(body)}"
print("range download OK, 100 bytes, range:", hdr.get("Content-Range"))

# path traversal guard
try:
    get("/files/" + urllib.request.quote("../etc/passwd"))
    raise SystemExit("FAIL: traversal was NOT blocked")
except urllib.error.HTTPError as e:
    assert e.code in (400, 403, 404), f"unexpected status: {e.code}"
    print("path traversal blocked OK ->", e.code)

# incremental refresh: add a file, refresh via CLI, search it
data = os.path.join(HERE, "data")
newfile = os.path.join(data, "Software_Library/Admin_Software/dell/new-dell-bios-r750-x1.2.3-a01.zip")
os.makedirs(os.path.dirname(newfile), exist_ok=True)
with open(newfile, "wb") as f:
    f.write(b"z" * 4096)

import subprocess
env = dict(os.environ, FILESTORE_SEARCH_DB=os.path.join(HERE, "test-search.db"))
r = subprocess.run(["python3", "-m", "cli", "refresh"],
                   cwd=HERE, env=env,
                   capture_output=True, text=True)
print("refresh stdout:", r.stdout.strip())

st, hdr, body = get("/api/search?q=dell%20r750%20bios&limit=3")
import json
d = json.loads(body)
names = [x["name"] for x in d["results"]]
assert any("r750" in n for n in names), f"new file not searchable: {names}"
print("incremental refresh + search OK:", names[0])

# remove the file, refresh, confirm gone
os.remove(newfile)
r = subprocess.run(["python3", "-m", "cli", "refresh"],
                   cwd=HERE, env=env,
                   capture_output=True, text=True)
st, hdr, body = get("/api/search?q=dell%20r750%20bios&limit=3")
d = json.loads(body)
names = [x["name"] for x in d["results"]]
assert not any("r750" in n for n in names), f"stale file still present: {names}"
print("delete-on-refresh OK (r750 gone)")

print("\nALL DOWNLOAD/REFRESH CHECKS PASSED")
