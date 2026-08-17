# File Store Search — 100% offline natural-language search for your software library

Natural-language search over a file store of vendor-named software: Red Hat
(RPM), Ubuntu/Debian (deb), Windows (MSI/EXE), firmware and drivers. Runs
fully offline: SQLite FTS5 for the fast deterministic layer, plus an
optional local LLM (any OpenAI-compatible endpoint: Ollama, vLLM,
llama.cpp server, text-generation-webui, LM Studio...) for turning
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
| `reindex.sh` | wrapper for cron/systemd-timer refresh |
| `search.json.example` | example configuration (copy to `search.json`) |
| `nginx.conf.example` | nginx vhost: proxy UI+API, alias `/files/` to your store |
| `make_test_data.py` | generates a synthetic vendor-named store for testing |
| `mock_llm.py` | mock OpenAI-compatible LLM for testing without a real one |
| `test_e2e.py`, `test_download.py`, `test_robust.py` | end-to-end + robustness test scripts |

## Quick start (dev/test on this machine)

```bash
cd /opt/filestore-search            # or wherever these files live
export FILESTORE_SEARCH_DATA=$(pwd)/data \
       FILESTORE_SEARCH_DB=$(pwd)/test-search.db
python3 make_test_data.py data      # 51 synthetic vendor-named files
python3 -m cli init
python3 -m cli index                # full index
python3 mock_llm.py --port 8901 &   # optional: mock LLM
export FILESTORE_SEARCH_LLM_BASE=http://127.0.0.1:8901/v1 \
       FILESTORE_SEARCH_LLM_MODEL=mock
python3 -m app --port 8099          # the test scripts expect port 8099
# open http://localhost:8099  ->  "firmware for my Dell R740"
python3 -m cli llm-test            # verify LLM connectivity
python3 test_e2e.py && python3 test_download.py && python3 test_robust.py
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
   | `llm_base` | `FILESTORE_SEARCH_LLM_BASE` | OpenAI-compatible base of your local LLM, i.e. up to and including `/v1`. Ollama: `http://127.0.0.1:11434/v1`. vLLM / llama.cpp server: `http://<host>:8000/v1`. text-generation-webui: its OpenAI-compatible URL. |
   | `llm_model` | `FILESTORE_SEARCH_LLM_MODEL` | model name the endpoint expects (Ollama tag, vLLM `--served-model-name`, etc.). |
   | `llm_api_key` | `FILESTORE_SEARCH_LLM_API_KEY` | optional. Most local servers need none (leave `""`); text-generation-webui does. |
   | `llm_timeout` | `FILESTORE_SEARCH_LLM_TIMEOUT` | seconds per LLM call. Bump for small models on slow hardware. |
   | `llm_disable` | `FILESTORE_SEARCH_LLM_DISABLE` | `true` = pure FTS mode, no LLM calls at all. |
   | `max_results` | `FILESTORE_SEARCH_MAX_RESULTS` | cap on results returned per search. |
   | `ignored_names` | — | filename prefixes to skip while indexing. |

   Example for this box:

   ```json
   {
     "data_dir": "/mnt",
     "db_path": "/var/lib/filestore-search/search.db",
     "public_url": "https://files.example.com",
     "llm_base": "http://127.0.0.1:8000/v1",
     "llm_model": "qwen2.5-7b-instruct",
     "llm_api_key": "",
     "llm_timeout": 30,
     "llm_disable": false,
     "max_results": 25,
     "ignored_names": [".", "~$", ".tmp", ".swp", "Thumbs.db", ".DS_Store"]
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
   OpenAI-compatible service. With a smaller model, enable the "AI answer"
   checkbox only when you want prose answers; the search itself works with
   or without the LLM (it rewrites the query and adds category/platform
   filters; if that over-filters, the app automatically retries without the
   filters). If the LLM is down or disabled, everything still works via
   plain FTS.

## How search works

1. Your free-text query hits `/api/search`.
2. If the LLM is enabled, it rewrites the query into:
   - a tightened keyword query (filler words dropped, model numbers kept)
   - a `category` filter (firmware, linux-rpm, windows-msi, ...)
   - a `platform` filter (rhel, ubuntu, windows, ...)
   - an optional `version` token
   - an optional `since` date when you ask for "files since <date>"
3. SQLite FTS5 (OR-matched tokens, `bm25` ranking + a "more tokens matched
   = higher rank" re-rank) returns results; LIKE fallback catches very short
   queries.
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
  files modified after that date. The LLM sets this automatically when you
  name a time window.

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
