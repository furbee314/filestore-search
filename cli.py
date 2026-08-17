"""Command line interface for the offline file store search.

Commands:
  init                create db schema (and sample config if missing)
  index               full reindex of the data directory
  refresh             incremental refresh (add/change/remove)
  serve [--port N]    run the web app
  search "query"      one-shot search on the CLI (good for testing)
  llm-test            check connectivity to the local LLM service
  stats               show index statistics
"""
import argparse
import datetime
import json
import os
import sys

import db
from config import load_config
from llm import LLMClient


def main():
    ap = argparse.ArgumentParser(prog="filestore-search")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("init")
    sub.add_parser("index")
    sub.add_parser("refresh")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--no-index", action="store_true",
                         help="do not auto-index on startup if empty")

    p_search = sub.add_parser("search")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--limit", type=int, default=15)
    p_search.add_argument("--category")
    p_search.add_argument("--platform")
    p_search.add_argument("--sort", choices=["relevance", "newest"],
                          default="relevance",
                          help="order results; 'newest' = most recently modified")
    p_search.add_argument("--since",
                          help="only files modified since this date "
                               "(YYYY-MM-DD, ISO datetime, or epoch)")
    p_search.add_argument("--json", action="store_true")

    p_newest = sub.add_parser("newest",
                              help="list the most recently modified files "
                                   "(no keyword search)")
    p_newest.add_argument("--limit", type=int, default=15)
    p_newest.add_argument("--category")
    p_newest.add_argument("--platform")
    p_newest.add_argument("--since",
                          help="only files modified since this date "
                               "(YYYY-MM-DD, ISO datetime, or epoch)")
    p_newest.add_argument("--json", action="store_true")

    sub.add_parser("llm-test")
    sub.add_parser("stats")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 0

    cfg = load_config()

    if args.cmd == "init":
        con = db.connect(cfg)
        print(f"db ready: {cfg['db_path']}")
        # seed a sample config from the shipped example if none exists yet
        import shutil
        p = os.path.join(cfg["root"], "search.json")
        example = os.path.join(cfg["root"], "search.json.example")
        if not os.path.exists(p) and os.path.exists(example):
            shutil.copyfile(example, p)
            print(f"sample config written: {p} (edit data_dir / llm_* to match your setup)")
        elif not os.path.exists(p):
            print(f"no search.json and no example to copy; create {p}")
        return 0

    if args.cmd in ("index", "refresh"):
        con = db.connect(cfg)
        if args.cmd == "index":
            db.full_reindex(cfg, con)
        else:
            db.incremental_refresh(cfg, con)
        return 0

    if args.cmd == "serve":
        import app
        # host/port come from the CLI args; argv=[] so app.main() does not
        # re-parse the CLI's own command line
        app.main(host=args.host, port=args.port, argv=[])
        return 0

    if args.cmd == "search":
        con = db.connect(cfg)
        q = " ".join(args.query)
        since = db.since_to_ts(args.since)
        res = db.search(con, q, limit=args.limit,
                        category=args.category, platform=args.platform,
                        min_mtime=since,
                        sort="mtime" if args.sort == "newest" else "relevance")
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"{len(res)} results for {q!r}\n")
            for r in res:
                print(f"  [{r['category']:>13}] {r['name']}")
                print(f"      {r['path']}  v={r['version'] or '-'}  "
                      f"arch={r['arch']}  size={r['size']}")
        return 0

    if args.cmd == "newest":
        con = db.connect(cfg)
        since = db.since_to_ts(args.since)
        res = db.newest(con, args.limit, category=args.category,
                        platform=args.platform, min_mtime=since)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"most recent {len(res)} file(s) "
                  f"{'since ' + args.since if args.since else ''}\n")
            for r in res:
                m = datetime.datetime.fromtimestamp(
                    r["mtime"]).strftime("%Y-%m-%d %H:%M")
                print(f"  [{r['category']:>13}] {r['name']}   modified {m}")
                print(f"      {r['path']}  v={r['version'] or '-'}  "
                      f"arch={r['arch']}  size={r['size']}")
        return 0

    if args.cmd == "llm-test":
        c = LLMClient(cfg=cfg)
        if not c.enabled:
            print("LLM disabled (FILESTORE_SEARCH_LLM_DISABLE or llm_disable)")
            return 1
        ok = c.ping()
        print(f"LLM {('reachable' if ok else 'NOT reachable')} at {c.base_url} "
              f"model {c.model}")
        return 0 if ok else 1

    if args.cmd == "stats":
        con = db.connect(cfg)
        total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        cats = con.execute(
            "SELECT category, COUNT(*) FROM files GROUP BY category "
            "ORDER BY 2 DESC").fetchall()
        print(f"total files: {total}")
        for c, n in cats:
            print(f"  {c:>13} {n}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
