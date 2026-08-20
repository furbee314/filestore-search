# File Store Search — 100% offline natural-language search for your software library

Natural-language search over a file store of vendor-named software: Red Hat
(RPM), Ubuntu/Debian (deb), Windows (MSI/EXE), firmware and drivers. Runs
fully offline: SQLite FTS5 for the fast deterministic layer, plus an
optional local LLM — by default Ollama on the same machine, **CPU-only,
no GPU needed** (any OpenAI-compatible endpoint also works: vLLM,
llama.cpp server, text-generation-webui, LM Studio...) — for turning
"firmware for my Dell R740" into a precise query and for answering
questions in plain English.

```
                 +-------------------------------------------+
 users  ------->  |  nginx (or the app directly)             |
                 |   /            UI + /api/*  -> python app |
                 |   /files/...   downloads  -> your store   |
                 +-------------------------------------------+
                                                        |
                                              +---------+---------+
                                              | python app (app.py) |
                                              +---------+---------+
                              +----------------+----------------+
                              |                 |
                        +-----v-----+    +------v------+
                        | SQLite FTS5|    | local LLM   |  (optional; OpenAI-compatible
                        | search.db  |    | /v1/chat/...|   /v1/chat/completions)
                        +------------+    +-------------+
```

No network calls ever leave the machine except to your own LLM endpoint.
Zero third-party Python dependencies (stdlib only).

## Files

| file | purpose |
|---|---|
| `config.py` | configuration (env vars + optional `search.json`) |
| `indexer.py` | file classification + name/version/vendor parsing |
| `db.py` | SQLite schema, index/refresh, FTS5 search |
| `llm.py` | local LLM client (rewrite + answer), graceful fallback |
| `app.py` | web UI + JSON API + `/files/` downloads (ThreadingHTTPServer) |
| `cli.py` | `init / index / refresh / search / llm-test / stats / serve` |
| `filestore-search.service` | systemd unit for the web app |
| `ollama.service` | systemd unit for Ollama (local LLM, CPU) |
| `reindex.sh` | wrapper for cron/systemd-timer refresh |
| `search.json.example` | example configuration (copy to `search.json`) |
| `nginx.conf.example` | nginx vhost: proxy UI+API, alias `/files/` to your store |
| `make_test_data.py` | generates a synthetic vendor-named store for testing |
| `mock_llm.py` | mock OpenAI-compatible LLM for testing without a real one |
| `requirements.txt` | documents Python dependencies (none — stdlib only) + host-level LLM deps |
| `prepare-offline-ollama.sh` | build an air-gapped Ollama+model transfer bundle (run on a connected machine) |
| `offline-install-ollama.sh` | install Ollama + model on the offline system from that bundle |
| `test_e2e.py`, `test_download.py`, `test_robust.py` | end-to-end + robustness test scripts |

## Local LLM on CPU (default: Ollama + qwen2.5:3b-instruct)

The target deployment is a CPU-only box (this app is tested on an 8-core
Ryzen 7 / 9.5GB RAM host, no GPU). The default configuration therefore
points at a local Ollama server with a small instruct model:

1. **Install Ollama** (no GPU required; it simply runs on the CPU):
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
   The installer needs the `zstd` system package to unpack its tarball on
   RHEL-family hosts (e.g. `dnf install zstd`; on Debian/Ubuntu
   `apt-get install zstd`). No Python packages are needed — the app itself
   is stdlib-only (see `requirements.txt`).
2. **Pull the model** (Q4_K_M 4-bit; ~2.3GB download, ~2.6GB RAM resident):
   ```bash
   ollama pull qwen2.5:3b-instruct
   ```
3. **Run it** — either `systemctl enable --now ollama` (a ready-made,
   CPU-friendly unit is shipped as `ollama.service`: binds to 127.0.0.1,
   4096-token context, capped at ~6 CPU cores so the search app still gets
   cycles) or `ollama serve` / `systemctl enable ollama` (the installer's
   stock unit).
4. **Point the app at it.** The defaults already do this:
   `llm_base = http://127.0.0.1:11434/v1`,
   `llm_model = qwen2.5:3b-instruct`. Nothing else to configure.

Sizing guide (RAM budget for the model):

| model | RAM (weights + ctx) | feel on an 8-core CPU | use when |
|---|---|---|---|
| qwen2.5:1.5b-instruct | ~2GB | ~10-15 tok/s | RAM < 4GB, rewrite-only duty |
| **qwen2.5:3b-instruct (default)** | **~3GB** | **~5-10 tok/s** | **the sweet spot for this app** |
| qwen2.5:7b-instruct | ~6GB | ~2-4 tok/s | 8GB+ RAM and you want better prose answers |

Practical notes:

- **Context window is the main RAM dial.** The app sends
  `options.num_ctx = llm_max_ctx` (default 4096) to Ollama on every
  request. On a tight box set `llm_max_ctx` to 2048 (env
  `FILESTORE_SEARCH_LLM_MAX_CTX`) and cut `OLLAMA_CONTEXT_LENGTH` too.
  This app's prompts (rewrite ~300 tokens, answer ~900 tokens) fit in
  2048 with headroom.
- **Timeouts.** CPU inference is slow: the app's default LLM timeout is
  60s (rewrite budget 45s, answer budget 90s). Raise
  `llm_timeout` / `FILESTORE_SEARCH_LLM_TIMEOUT` on weaker CPUs; the app
  still degrades gracefully to plain FTS if a call times out, so a slow
  answer just means "no AI answer this time", never a broken search.
- **First request warms up** (model load + JIT): the very first call can
  take 30-60s. Subsequent calls are fast because Ollama keeps the model
  resident (idle eviction after 5 min by default).
- **CPU contention.** One request at a time is the intended use (a
  single UI / a few admins). If you need parallel search+answer, bump
  `OLLAMA_NUM_PARALLEL` and expect per-request latency to climb.
- **Any other OpenAI-compatible server works** — just set `llm_base` /
  `llm_model` (e.g. vLLM or llama.cpp `server` on port 8000). The
  `num_ctx` hint is only sent to Ollama (detected by port 11434); other
  backends are left untouched.

## Offline (air-gapped) deployment

If the destination system has no network access at all, use the two
helper scripts shipped in this repo to transfer everything in one bundle:

1. **On a connected machine of the same architecture** (amd64 here), build
   the transfer bundle:
   ```bash
   ./prepare-offline-ollama.sh /path/to/out
   # -> /path/to/out/filestore-search-offline-llm.tar
   #    (Ollama linux tarball + sha256 + the qwen2.5:3b-instruct model
   #     library in Ollama's ready-to-use on-disk layout, ~3.3GB total)
   ```
   Pass a zstd .rpm as a 4th argument (e.g.
   `dnf download zstd` then
   `./prepare-offline-ollama.sh /out v0.32.14 qwen2.5:3b-instruct zstd-*.rpm`)
   to pack it into the bundle — the install script will `rpm -ivh` it
   automatically if the target lacks zstd.
   The script pins the Ollama version (default `v0.32.14`, change via its
   2nd argument) so the bundle matches `offline-install-ollama.sh`.
2. **Copy to the offline system** (any medium — USB, `scp` over a jump
   host, CD). You need three things:
   - `filestore-search-offline-llm.tar` (the bundle)
   - `offline-install-ollama.sh`
   - the `filestore-search` source tree (this repo — `git archive` works)
   Plus the `zstd` package on the target: `dnf install zstd` works from
   the OS installation media, or `rpm -ivh` the zstd .rpm.
3. **On the offline system** (as root):
   ```bash
   ./offline-install-ollama.sh /path/to/filestore-search-offline-llm.tar
   ```
   The script verifies the tarball checksum, unpacks Ollama to `/usr/local`,
   creates the `ollama` user, installs `ollama.service` (same unit as the
   one in this repo), restores the model library to
   `/usr/share/ollama/.ollama/models` with correct ownership, then starts
   the service and checks `ollama list` shows `qwen2.5:3b-instruct`.
4. **Deploy the app** exactly as in "Production deployment" (index, systemd
   unit for the app, nginx) — nothing else on the app side is network
   dependent: it talks only to SQLite and `127.0.0.1:11434`.
5. **Smoke test**: `python3 -m cli llm-test` and a search through the UI;
   the first request warms up the model (30-60s, expected).

Manual alternative (no helper scripts): download
`https://github.com/ollama/ollama/releases/download/<ver>/ollama-linux-amd64.tar.zst`
on the connected box, `zstd -d` + `tar -x` into `/usr/local` on the target,
and copy `/usr/share/ollama/.ollama/models/` (blobs + manifests) to the
same path on the target with `ollama:ollama` ownership — the install script
is that sequence with checksums and user/systemd setup added.

## Quick start (dev/test on this machine)

```bash
cd /opt/filestore-search            # or wherever these files live
export FILESTORE_SEARCH_DATA=$(pwd)/data \
       FILESTORE_SEARCH_DB=$(pwd)/test-search.db
python3 make_test_data.py data      # 51 synthetic vendor-named files
python3 -m cli init
python3 -m cli index                # full index
ollama pull qwen2.5:3b-instruct     # once, ~2.3GB
ollama serve &                      # or: systemctl start ollama
# defaults already target Ollama on 127.0.0.1:11434 + qwen2.5:3b-instruct
python3 -m app --port 8099          # the test scripts expect port 8099
# open http://localhost:8099  ->  "firmware for my Dell R740"
python3 -m cli llm-test             # verify LLM connectivity
python3 test_e2e.py && python3 test_download.py && python3 test_robust.py
# (no Ollama handy? mock_llm.py stands in for it:
#   python3 mock_llm.py --port 8901 &
#   export FILESTORE_SEARCH_LLM_BASE=http://127.0.0.1:8901/v1 FILESTORE_SEARCH_LLM_MODEL=mock
```

## Production deployment

1. **Copy** this directory to e.g. `/opt/filestore-search`.
2. **Create config** — copy `search.json.example` to `search.json` next to
   the code and edit (or `python3 -m cli init` seeds it for you). Keys:

   | key | env override | meaning |
   |---|---|---|
   | `data_dir` | `FILESTORE_SEARCH_DATA` | root of the file store. Everything under it is indexed and served by relative path. For this box: `/mnt` (with `Software_Library/`, `repos/`, `isos/` inside). |
   | `db_path` | `FILESTORE_SEARCH_DB` | where the SQLite index lives. Needs a writable dir for the user running the app. |
   | `public_url` | `FILESTORE_SEARCH_URL` | base URL the UI puts on download links. If your nginx already serves the store, set this to it (e.g. `https://files.example.com`) and the app's own `/files/` endpoint is unused. Leave `""` to download through the app (`/files/...`). |
   | `llm_base` | `FILESTORE_SEARCH_LLM_BASE` | OpenAI-compatible base of your local LLM, i.e. up to and including `/v1`. Default (CPU box): Ollama on the same host, `http://127.0.0.1:11434/v1`. vLLM / llama.cpp server: `http://<host>:8000/v1`. text-generation-webui: its OpenAI-compatible URL. |
   | `llm_model` | `FILESTORE_SEARCH_LLM_MODEL` | model name the endpoint expects. Default: `qwen2.5:3b-instruct` (small instruct model, runs on CPU; Ollama tag or vLLM `--served-model-name`). |
   | `llm_api_key` | `FILESTORE_SEARCH_LLM_API_KEY` | optional. Most local servers need none (leave `""`); text-generation-webui does. |
   | `llm_timeout` | `FILESTORE_SEARCH_LLM_TIMEOUT` | seconds per LLM call. Default 60 — CPU inference is slow; bump further on weak CPUs. |
   | `llm_max_ctx` | `FILESTORE_SEARCH_LLM_MAX_CTX` | context window (tokens) sent to Ollama as `options.num_ctx`; default 4096. Lower it (2048) on RAM-tight boxes. |
   | `llm_disable` | `FILESTORE_SEARCH_LLM_DISABLE` | `true` = pure FTS mode, no LLM calls at all. |
   | `max_results` | `FILESTORE_SEARCH_MAX_RESULTS` | cap on results returned per search. |
   | `ignored_names` | — | filename prefixes to skip while indexing. |
   | `ignored_suffixes` | — | filename suffixes to skip while indexing (case-insensitive). Defaults cover the common checksum sidecars: `.sha1 .sha128 .sha256 .sha512 .md5`. |

   Example for this box:

   ```json
   {
     "data_dir": "/mnt",
     "db_path": "/var/lib/filestore-search/search.db",
     "public_url": "https://files.example.com",
     "llm_base": "http://127.0.0.1:11434/v1",
     "llm_model": "qwen2.5:3b-instruct",
     "llm_api_key": "",
     "llm_timeout": 60,
     "llm_max_ctx": 4096,
     "llm_disable": false,
     "max_results": 25,
     "ignored_names": [".", "~$", ".tmp", ".swp", "Thumbs.db", ".DS_Store"],
     "ignored_suffixes": [".sha1", ".sha128", ".sha256", ".sha512", ".md5"]
   }
   ```

   The layout is understood from the directory structure:
   `repos/RHEL9/...` and `repos/Ubuntu/...` classify as RHEL/Ubuntu packages
   (vendor repo schemes like `packages/x86_64/...` and `pool/main/l/...` are
   fine), `Software_Library/Drivers/` as drivers, `Software_Library/` firmware
   zips as firmware, and `isos/` (or any `*.iso`) as install media with the
   platform detected from the ISO name (RHEL, Ubuntu, Windows, ESXi, ...).
3. **Index**: `python3 -m cli index` (full), then keep it fresh:
   ```cron
   * * * * *  root  /opt/filestore-search/reindex.sh >> /var/log/filestore-search.log 2>&1
   ```
   `refresh` is incremental (adds changed/new files, removes gone ones) and
   finishes in milliseconds for typical stores.
4. **Run the app** via systemd: `cp filestore-search.service /etc/systemd/system/`,
   adjust `User/Group`/paths, `systemctl enable --now filestore-search`.
   It listens on 127.0.0.1:8080.
5. **nginx**: use `nginx.conf.example` — proxy `/` (UI + API) to the app and
   `alias /files/` to your store directory. If your store is already served
   by nginx, just set `public_url` to its base URL and drop the `/files/`
   location.
6. **LLM** (optional but recommended): point `llm_base` at your local
   OpenAI-compatible service — on this CPU-only box the default is Ollama
   with `qwen2.5:3b-instruct` (see "Local LLM on CPU" above; install Ollama
   + `ollama pull` + `systemctl enable --now ollama`, done). With a smaller
   model, enable the "AI answer" checkbox only when you want prose answers;
   the search itself works with or without the LLM (it rewrites the query
   and adds category/platform filters; if that over-filters, the app
   automatically retries without the filters). If the LLM is down or
   disabled, everything still works via plain FTS.

## How search works

1. Your free-text query hits `/api/search`.
2. If the LLM is enabled, it rewrites the query into:
   - a tightened keyword query (filler words dropped, model numbers kept)
   - a `category` filter (firmware, linux-rpm, windows-msi, windows-patch,
     linux-installer, ...)
   - a `platform` filter (rhel, ubuntu, windows, ...)
   - an optional `version` token
3. SQLite FTS5 (OR-matched tokens, `bm25` ranking + a "more tokens matched
   = higher rank" re-rank) returns results; LIKE fallback catches very short
   queries. The re-rank also folds in a per-file **deliverable priority** so
   installables (`.rpm/.deb/.msi/.msu/.exe/.sh` installers, ISOs, BIOS/firmware
   bundles, drivers) outrank the paperwork that lives next to them: docs
   (`.txt`/`.pdf`/readmes) rank last, repo metadata/manifests
   (`repomd.xml`, `Packages.gz`, GPG keys) below, and archives in between.
   This is why "dell r740 bios" returns the BIOS file rather than a readme
   that merely mentions it. Time windows ("this month", "last 30 days",
   "since March") are **not** part of the LLM rewrite — they are resolved
   deterministically from the system clock (see Recency below), so the model
   can never hallucinate a date.
4. Optional `&answer=1`: the LLM writes a short plain-English answer naming
   the best file(s) and their download path, based only on the top results
   (it cannot invent files).

### Recency ("latest / newest / recent")

Recency is a first-class search dimension, backed by the file `mtime` that
the indexer already stores. Three ways to use it:

- **Natural language** — say "latest", "newest", "recent" (e.g. "latest Dell
  R740 firmware"). The app detects the word, searches the matching files and
  orders them most-recently-modified first. A bare "latest" (no other
  keywords) falls back to the store-wide most-recent files, so it never
  returns an empty page.
- **`&sort=newest`** — order the (keyword-matched) results by recency instead
  of relevance. This is the UI's "Sort: Newest first" dropdown.
- **`&since=YYYY-MM-DD`** (or an ISO datetime / epoch) — restrict results to
  files modified after that date.
- **Time phrases in your query** — "this month", "last 30 days", "last week",
  "since March", "updated in June 2023", "today"/"yesterday", or an explicit
  date you type ("since 2024-05-01"). The server resolves these against the
  **system clock** (never the LLM, which has no reliable current date and
  would hallucinate one) and applies them as an `mtime` floor. Windows use
  start-of-period semantics: "this month" → 1st of this month, "last week"
  → 7 days back, "last year" → Jan 1 of last year.
- **Empty windows widen, strict dates don't** — if a system-resolved window
  ("this month", "last week") matches nothing, the search widens to the most
  recent matching files and says so in the result meta line, instead of
  returning an empty page. A date you typed explicitly ("since 2024-01-01",
  `&since=`) is a real filter and is never widened.

All of these compose with the category/platform filters and with each other.
The CLI mirrors this: `python3 -m cli search ... --sort newest --since 2024-01-01`,
and `python3 -m cli newest` to list the most recently modified files without
any keyword search.

Classification is from filename + directory (extension, rpm/deb layout,
vendor/product patterns), so it works with whatever the vendors named the
files. You can also filter manually in the UI (category / platform
dropdowns) and search a subset.

## Testing without a real LLM

`mock_llm.py` implements `POST /v1/chat/completions` with canned
responses (keyword-based rewrite + answer quoting the top result) so the
whole pipeline is testable offline:

```bash
python3 mock_llm.py --port 8901 &
FILESTORE_SEARCH_LLM_BASE=http://127.0.0.1:8901/v1 python3 -m cli llm-test
```

## Limitations / notes

- Indexing is by *filename* only (no content scanning, no rpm/deb header
  parsing) — that matches your "vendors name the files" setup and keeps the
  indexer fast and dependency-free. If you later want richer metadata
  (`rpm -qp --qf`, `dpkg-deb -f`), that's a natural extension of
  `indexer._row_from_file`.
- The web app is stdlib-only and single-node; put nginx in front for TLS,
  logging and static serving of the files.
- The LLM is used *only* for query-rewriting and answer text; search results
  are always the deterministic FTS5 set (the LLM can't skip or reorder
  files, only answer about them), which keeps it trustworthy and auditable.
- Path traversal on `/files/` is blocked (`realpath` containment check).
