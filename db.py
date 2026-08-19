"""SQLite storage layer: schema, upsert, incremental refresh, FTS5 search."""
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from indexer import SCHEMA, SCHEMA_VERSION, classify, extract_version, extract_vendor, extract_description


def connect(cfg, check_same_thread=True):
    os.makedirs(os.path.dirname(cfg["db_path"]) or ".", exist_ok=True)
    # check_same_thread=False is required when the web app serves requests
    # from multiple threads (ThreadingHTTPServer); pair with a lock.
    con = sqlite3.connect(cfg["db_path"], check_same_thread=check_same_thread)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(con)
    return con


def ensure_schema(con):
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
    con.commit()


def _row_from_file(root, relpath, st):
    name = os.path.basename(relpath)
    category, platform, arch = classify(name, relpath)
    version = extract_version(name)
    vendor = extract_vendor(name, relpath)
    description = extract_description(name, relpath, category)
    ext = os.path.splitext(name)[1].lower().lstrip(".") or "none"
    # for .tar.gz etc
    if name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".msi.zip")):
        ext = name.lower().rsplit(".", 2)[-2] + "." + name.lower().rsplit(".", 1)[-1]
    return {
        "path": relpath,
        "name": name,
        "category": category,
        "platform": platform,
        "arch": arch,
        "file_type": ext,
        "version": version,
        "vendor": vendor,
        "description": description,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "indexed_at": time.time(),
    }


def iter_files(cfg):
    """Yield (relpath, stat_result) for every file under data_dir that is not ignored."""
    ignored = set(cfg["ignored_names"])
    data_dir = cfg["data_dir"]
    for dirpath, dirnames, filenames in os.walk(data_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if any(fn.startswith(p) for p in ignored):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat_is_regular(st):
                continue
            rel = os.path.relpath(full, data_dir)
            yield rel, st


def stat_is_regular(st):
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


def full_reindex(cfg, con, quiet=False):
    """Rebuild the entire index from scratch. Returns (count, elapsed)."""
    t0 = time.time()
    con.execute("DELETE FROM files")
    # contentless FTS tables cannot be emptied with plain DELETE; drop and
    # recreate instead.
    con.execute("DROP TABLE IF EXISTS files_fts")
    con.execute("""CREATE VIRTUAL TABLE files_fts USING fts5(
        path, name, version, description, vendor,
        content='', tokenize='unicode61')""")
    rows = [_row_from_file(cfg["data_dir"], rel, st) for rel, st in iter_files(cfg)]
    files_rows = []
    fts_rows = []
    for i, r in enumerate(rows, start=1):
        files_rows.append((i, r["path"], r["name"], r["category"], r["platform"],
                           r["arch"], r["file_type"], r["version"], r["vendor"],
                           r["description"], r["size"], r["mtime"], r["indexed_at"]))
        fts_rows.append((i, r["path"], r["name"], r["version"], r["description"],
                         r["vendor"]))
    con.executemany(
        """INSERT INTO files (id, path, name, category, platform, arch, file_type,
             version, vendor, description, size, mtime, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", files_rows)
    con.executemany(
        "INSERT INTO files_fts(rowid, path, name, version, description, vendor)"
        " VALUES (?, ?, ?, ?, ?, ?)", fts_rows)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    dt = time.time() - t0
    if not quiet:
        print(f"indexed {n} files in {dt:.1f}s -> {cfg['db_path']}")
    return n, dt


def incremental_refresh(cfg, con):
    """Add/modify new and changed files, delete gone ones. Returns (added, updated, removed)."""
    t0 = time.time()
    existing = {}
    for p, m in con.execute("SELECT path, mtime FROM files"):
        existing[p] = m

    seen = set()
    added = updated = 0
    for rel, st in iter_files(cfg):
        seen.add(rel)
        row = _row_from_file(cfg["data_dir"], rel, st)
        if rel not in existing:
            con.execute(
                """INSERT INTO files
                   (path, name, category, platform, arch, file_type, version, vendor,
                    description, size, mtime, indexed_at)
                   VALUES (:path, :name, :category, :platform, :arch, :file_type,
                    :version, :vendor, :description, :size, :mtime, :indexed_at)""",
                row)
            fid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.execute(
                "INSERT INTO files_fts(rowid, path, name, version, description, vendor)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (fid, row["path"], row["name"], row["version"], row["description"], row["vendor"]))
            added += 1
        elif existing[rel] != st.st_mtime:
            old = con.execute(
                "SELECT id, path, name, version, description, vendor FROM files WHERE path=?",
                (rel,)).fetchone()
            fid = old[0]
            # contentless FTS5 tables only support removal via the 'delete'
            # command, passing the row's old content values.
            con.execute(
                "INSERT INTO files_fts(files_fts, rowid, path, name, version,"
                " description, vendor) VALUES ('delete', ?, ?, ?, ?, ?, ?)",
                (fid, old[1], old[2], old[3], old[4], old[5]))
            con.execute(
                """UPDATE files SET name=:name, category=:category, platform=:platform,
                   arch=:arch, file_type=:file_type, version=:version, vendor=:vendor,
                   description=:description, size=:size, mtime=:mtime, indexed_at=:indexed_at
                   WHERE path=:path""", row)
            con.execute(
                "INSERT INTO files_fts(rowid, path, name, version, description, vendor)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (fid, row["path"], row["name"], row["version"], row["description"], row["vendor"]))
            updated += 1

    removed = 0
    for p in existing:
        if p not in seen:
            old = con.execute(
                "SELECT id, path, name, version, description, vendor FROM files WHERE path=?",
                (p,)).fetchone()
            if old:
                con.execute(
                    "INSERT INTO files_fts(files_fts, rowid, path, name, version,"
                    " description, vendor) VALUES ('delete', ?, ?, ?, ?, ?, ?)",
                    (old[0], old[1], old[2], old[3], old[4], old[5]))
                con.execute("DELETE FROM files WHERE path=?", (p,))
                removed += 1
    con.commit()
    dt = time.time() - t0
    print(f"refresh: +{added} ~{updated} -{removed} in {dt:.1f}s")
    return added, updated, removed


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

BAD_CHARS = re.compile(r"['\"]")


def fts_query(query):
    """Build an OR-based FTS5 query from raw text.

    Every token is quoted so user input cannot break the FTS5 syntax, and
    tokens are combined with OR so a partial match is found even when some
    words are not present in the index.
    """
    q = BAD_CHARS.sub("", query.strip())
    tokens = [t for t in q.split() if t]
    if not tokens:
        return ""
    return " OR ".join('"%s"' % t.replace('"', '""') for t in tokens)


def since_to_ts(value):
    """Accept an ISO date (YYYY-MM-DD), an ISO datetime, or a numeric epoch.

    Returns a POSIX timestamp (float) or None if unparseable.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if re.match(r"^\d+(\.\d+)?$", v):
        return float(v)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


# Hard ceiling on how many candidate rows the re-ranker will consider in one
# call. Keeps very broad queries (a single common token) bounded while still
# covering every page a UI would realistically flip through.
_MAX_POOL = 5000


def _filters_sql(table, category, platform, min_mtime, extra=""):
    """Append shared category/platform/min_mtime filter clauses.

    ``table`` is the column prefix (``f`` or bare), ``extra`` is any clause
    already built (the MATCH / WHERE ... LIKE part). Returns (sql, params).
    """
    prefix = table + "." if table else ""
    sql = extra
    params = []
    if category:
        sql += f" AND {prefix}category = ?"
        params.append(category)
    if platform:
        sql += f" AND {prefix}platform = ?"
        params.append(platform)
    if min_mtime is not None:
        sql += f" AND {prefix}mtime >= ?"
        params.append(min_mtime)
    return sql, params


def count_matches(con, query, category=None, platform=None, min_mtime=None):
    """Total number of files a keyword search would return.

    Mirrors the FTS-or-LIKE behaviour of :func:`search` so the UI can show an
    accurate "of N" total and build a pagination bar. Returns 0 when the query
    has no tokens (search() returns [] in that case too).
    """
    q = fts_query(query)
    if not q:
        return 0
    base = "SELECT COUNT(*) FROM files_fts JOIN files f ON f.id = files_fts.rowid WHERE files_fts MATCH ?"
    sql, params = _filters_sql("f", category, platform, min_mtime,
                               extra=base + " ")
    params = [q] + params
    n = con.execute(sql, params).fetchone()[0]
    if n:
        return n
    # LIKE fallback count (used when FTS matches nothing).
    like_q = "%" + query.strip().lower()[:40] + "%"
    base2 = ("SELECT COUNT(*) FROM files "
             "WHERE (lower(description) LIKE ? OR lower(name) LIKE ?)")
    sql2, params2 = _filters_sql("", category, platform, min_mtime,
                                 extra=base2)
    params2 = [like_q, like_q] + params2
    return con.execute(sql2, params2).fetchone()[0]


def count_filtered(con, category=None, platform=None, min_mtime=None):
    """Total files matching the filters only (no keyword match).

    This is the 'latest / newest' store-wide fallback path: every file under
    the given category/platform/modified-since filter.
    """
    sql, params = _filters_sql("", category, platform, min_mtime,
                               extra="SELECT COUNT(*) FROM files ")
    return con.execute(sql, params).fetchone()[0]


def search(con, query, limit=25, offset=0, category=None, platform=None,
           min_mtime=None, sort="relevance"):
    """Run a natural-language-ish search. Returns a page of result dicts.

    ``offset`` (zero-based) selects the page within the ranked result set;
    call :func:`count_matches` for the total length of that set to drive
    pagination.

    Strategy: two FTS5 passes.
      1. OR pass (high recall) over all tokens
      2. AND-ish boost: files matching more tokens rank higher (bm25 does not
         do this across separate terms well, so we count matches in Python)
    The LLM layer (app.py) is responsible for turning free text into better
    queries; this function is the deterministic backstop.

    ``sort`` controls final ordering:
      - "relevance" (default): rank by matched-token count then bm25.
      - "mtime": the keyword match set is ordered most-recently-modified
        first (used for 'latest / newest' searches over specific files).
    """
    q = fts_query(query)
    if not q:
        return []
    off = max(0, int(offset))
    lim = max(1, int(limit))
    # Candidate pool large enough to cover the requested page plus re-rank
    # headroom, capped so a very broad query stays bounded.
    pool = min(max(off + lim, lim * (50 if sort == "mtime" else 3)), _MAX_POOL)

    sql = """
      SELECT f.id, f.path, f.name, f.category, f.platform, f.arch, f.file_type,
             f.version, f.vendor, f.description, f.size, f.mtime,
             bm25(files_fts) AS score
      FROM files_fts
      JOIN files f ON f.id = files_fts.rowid
      WHERE files_fts MATCH ?
    """
    sql, fparams = _filters_sql("f", category, platform, min_mtime,
                                extra=sql)
    params: list = [q] + fparams
    if sort == "mtime":
        # Pull a generous candidate pool so the Python re-rank (relevance
        # first, recency as tie-breaker) can pick the right files even when
        # the most relevant match is not among the very newest.
        sql += " ORDER BY f.mtime DESC, f.id DESC LIMIT ?"
        params.append(pool)
    else:
        sql += " ORDER BY score LIMIT ?"
        params.append(pool)

    rows = con.execute(sql, params).fetchall()
    if not rows:
        # fall back to substring match when FTS found nothing (short/odd
        # tokens, punctuation-heavy queries)
        like_q = "%" + query.strip().lower()[:40] + "%"
        sql2 = """SELECT id, path, name, category, platform, arch, file_type,
                         version, vendor, description, size, mtime,
                         1e8 AS score
                  FROM files
                  WHERE (lower(description) LIKE ? OR lower(name) LIKE ?)"""
        sql2, fparams2 = _filters_sql("", category, platform, min_mtime,
                                      extra=sql2)
        p2 = [like_q, like_q] + fparams2
        pool2 = min(off + lim, _MAX_POOL)
        if sort == "mtime":
            sql2 += " ORDER BY mtime DESC, id DESC LIMIT ? OFFSET ?"
            p2 += [pool2, off]
        else:
            # deterministic order so repeated pages are stable
            sql2 += " ORDER BY id LIMIT ? OFFSET ?"
            p2 += [pool2, off]
        rows = con.execute(sql2, p2).fetchall()

    results = []
    for r in rows:
        d = {
            "id": r[0], "path": r[1], "name": r[2], "category": r[3],
            "platform": r[4], "arch": r[5], "file_type": r[6], "version": r[7],
            "vendor": r[8], "description": r[9], "size": r[10], "mtime": r[11],
            "score": r[12],
        }
        results.append(d)
    # re-rank: more matched tokens => better. For relevance, break ties by
    # bm25. For recency ("mtime"), keep relevance as the primary key and use
    # most-recently-modified as the tie-breaker, so "latest Dell R740
    # firmware" surfaces the Dell R740 files (they match more tokens) while a
    # generic "latest firmware" still returns the newest firmware files.
    tokens = [t.lower() for t in query.split() if t]
    for d in results:
        hay = " ".join([d["name"], d["description"], d["vendor"],
                        d["version"], d["category"], d["platform"]]).lower()
        d["matched"] = sum(1 for t in tokens if t in hay)
    if sort == "mtime":
        results.sort(key=lambda d: (-d["matched"], -d["mtime"]))
    else:
        results.sort(key=lambda d: (-d["matched"], d["score"]))
    for d in results:
        d.pop("score", None)
        d.pop("matched", None)
    return results[off:off + lim]


def newest(con, limit, offset=0, category=None, platform=None, min_mtime=None):
    """Return the most recently modified files, optionally filtered.

    This is the 'latest / newest / recent' path: no full-text matching, just
    recency. category/platform/min_mtime are applied as filters. ``offset``
    (zero-based) selects a page; the total matching set is available via
    :func:`count_filtered`.
    """
    off = max(0, int(offset))
    sql = """SELECT id, path, name, category, platform, arch, file_type,
                    version, vendor, description, size, mtime
             FROM files"""
    conds = []
    params = []
    if category:
        conds.append("category = ?")
        params.append(category)
    if platform:
        conds.append("platform = ?")
        params.append(platform)
    if min_mtime is not None:
        conds.append("mtime >= ?")
        params.append(min_mtime)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY mtime DESC, id DESC LIMIT ? OFFSET ?"
    params.append(int(limit))
    params.append(off)
    rows = con.execute(sql, params).fetchall()
    return [{
        "id": r[0], "path": r[1], "name": r[2], "category": r[3],
        "platform": r[4], "arch": r[5], "file_type": r[6], "version": r[7],
        "vendor": r[8], "description": r[9], "size": r[10], "mtime": r[11],
    } for r in rows]


def facets(con):
    """Return category and platform counts for filter dropdowns."""
    cats = [r[0] for r in con.execute(
        "SELECT DISTINCT category FROM files ORDER BY category")]
    plats = [r[0] for r in con.execute(
        "SELECT DISTINCT platform FROM files WHERE platform != 'unknown' "
        "ORDER BY platform")]
    counts = dict(con.execute(
        "SELECT category, COUNT(*) FROM files GROUP BY category").fetchall())
    return {"categories": cats, "platforms": plats, "counts": counts,
            "total": sum(counts.values())}
