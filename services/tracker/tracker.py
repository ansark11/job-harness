#!/usr/bin/env python3
"""Tracker CLI: every command prints JSON.

    python3 tracker.py ingest matches.json [--run-id ID]
    python3 tracker.py list [--status new|kept|discarded|expired] [--group-by fetch-date]
    python3 tracker.py show <id>
    python3 tracker.py keep <id> [<id> ...]
    python3 tracker.py discard <id> [<id> ...]
    python3 tracker.py expire
    python3 tracker.py run start --kind daily_search
    python3 tracker.py run finish <run id> --status ok|failed [--stats JSON]
    python3 tracker.py run last-success --kind daily_search

The database is data/jobs.db (TRACKER_DB overrides it). Exit 1 on a bad
request (unknown id, disallowed status change), with {"error": ...}.
"""

import argparse
import json
import sys
from pathlib import Path

import db
import runlog
import store

HERE = Path(__file__).resolve().parent


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="add matcher output")
    p.add_argument("matches", help="matcher output JSON ('-' for stdin)")
    p.add_argument("--run-id")

    p = sub.add_parser("list")
    p.add_argument("--status", choices=["new", "kept", "discarded", "expired"])
    p.add_argument("--group-by", choices=["fetch-date"])

    p = sub.add_parser("show")
    p.add_argument("id")

    for name in ("keep", "discard"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")

    sub.add_parser("expire", help=f"expire postings unreviewed for {store.EXPIRE_DAYS} days")

    p = sub.add_parser("run", help="record daily runs")
    rsub = p.add_subparsers(dest="run_cmd", required=True)
    r = rsub.add_parser("start")
    r.add_argument("--kind", required=True)
    r = rsub.add_parser("finish")
    r.add_argument("id")
    r.add_argument("--status", required=True, choices=["ok", "failed"])
    r.add_argument("--stats", default="{}")
    r = rsub.add_parser("last-success")
    r.add_argument("--kind", required=True)
    return ap


def dispatch(conn, args, run):
    if args.cmd == "ingest":
        text = sys.stdin.read() if args.matches == "-" else Path(args.matches).read_text()
        data = json.loads(text)
        matches = data["matches"] if isinstance(data, dict) else data
        result = store.ingest(conn, matches, run_id=args.run_id)
        run.event("ingest_result", **{k: len(v) for k, v in result.items()}, input_digest=runlog.digest(matches))
        return result
    if args.cmd == "list":
        items = store.list_opportunities(conn, args.status)
        return {"groups": store.group_by_fetch_date(items)} if args.group_by else {"items": items}
    if args.cmd == "show":
        return store.get(conn, args.id)
    if args.cmd in ("keep", "discard"):
        to = "kept" if args.cmd == "keep" else "discarded"
        results = [store.set_status(conn, i, to) for i in args.ids]
        for r in results:
            run.event("status_change", **r)
        return {"results": results}
    if args.cmd == "expire":
        ids = store.expire(conn)
        run.event("expired", count=len(ids), ids=ids)
        return {"expired": ids}
    if args.cmd == "run":
        if args.run_cmd == "start":
            return store.run_start(conn, args.kind)
        if args.run_cmd == "finish":
            return store.run_finish(conn, args.id, args.status, json.loads(args.stats))
        return {"last_success": store.last_success(conn, args.kind)}
    raise AssertionError(args.cmd)


def main(argv=None):
    args = build_parser().parse_args(argv)
    with runlog.start("tracker", HERE, argv) as run:
        conn = db.connect()
        try:
            out, code = dispatch(conn, args, run), 0
        except store.TrackerError as e:
            out, code = {"error": str(e)}, 1
            run.warn("bad_request", error=str(e))
        finally:
            conn.close()
        print(json.dumps(out, indent=2))
        run.finish(code)
        return code


if __name__ == "__main__":
    sys.exit(main())
