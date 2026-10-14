#!/usr/bin/env python3
"""Show what happened in a run, across every tool, from the structured logs.

    python3 scripts/trace.py --last                  # most recent trace
    python3 scripts/trace.py <trace or session id>   # a prefix is enough
    python3 scripts/trace.py --list                  # recent traces
    python3 scripts/trace.py --errors                # recent warnings and errors
    python3 scripts/trace.py <id> --full             # every record's full data
    python3 scripts/trace.py <id> --json             # raw records, merged and sorted

Reads each component's own log, <component>/logs/<component>.jsonl* (see
runlog.py); other files in logs/ are ignored.
Only reads log files: it imports no service code.
"""

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The fields worth a glance per event; --full shows everything.
BRIEF = {
    "service_call": ("service", "args", "exit_code", "duration_ms", "error"),
    "search_query": ("keywords", "geo", "page", "results", "new"),
    "posting_skipped": ("id", "title", "reason"),
    "match_decision": ("id", "title", "company", "decision", "reasons", "flags", "score"),
    "extract_failed": ("id", "error"),
    "ingest_result": ("added", "updated", "skipped", "expired"),
    "status_change": ("id", "from", "status"),
    "bank_resolve": ("question", "status", "key", "via"),
    "inbox_add": ("id", "question", "duplicate"),
    "inbox_resolved": ("id", "key", "how"),
    "question_mapped": ("question", "key", "confidence"),
    "question_answered": ("question", "answer", "confidence", "basis"),
    "auto_mapped": ("question", "key", "confidence"),
    "apply_step": ("application_id", "step", "status", "note", "source", "error"),
    "field_resolved": ("key", "kind", "state", "source"),
    "application_status": ("id", "status", "note"),
    "run_end": ("exit_code", "duration_ms"),
    "crash": ("exception",),
}


REQUIRED = ("ts", "trace_id", "run_id", "component", "event")


def log_files(root):
    """Each component's own runlog file (and its rotations) only. Other
    .jsonl files can sit in logs/ in their own formats and must not be read
    as run records."""
    for logs in sorted([*root.glob("services/*/logs"), *root.glob("workflows/*/logs")]):
        yield from sorted(logs.glob(f"{logs.parent.name}.jsonl*"))


def records(root=None):
    root = root or ROOT   # resolved at call time, so tests can redirect ROOT
    out = []
    for path in log_files(root):
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and all(k in rec for k in REQUIRED):   # skip anything malformed
                    out.append(rec)
    return sorted(out, key=lambda r: r["ts"])


def brief(rec):
    keys = BRIEF.get(rec["event"])
    data = rec.get("data") or {}
    if keys is None:
        items = list(data.items())[:4]
    else:
        items = [(k, data[k]) for k in keys if data.get(k) not in (None, "", [], {})]
    text = "  ".join(f"{k}={json.dumps(v, default=str) if not isinstance(v, str) else v}" for k, v in items)
    return text[:220]


def depth_map(recs):
    parent = {r["run_id"]: r.get("parent_run_id") for r in recs}
    def depth(run_id, seen=()):
        p = parent.get(run_id)
        return 0 if not p or p not in parent or p in seen else 1 + depth(p, seen + (run_id,))
    return {rid: depth(rid) for rid in parent}


def show(recs, full=False):
    depth = depth_map(recs)
    for r in recs:
        mark = {"error": "!!", "warning": "! "}.get(r.get("level"), "  ")
        indent = "    " * depth.get(r["run_id"], 0)
        print(f"{r['ts'][11:23]} {mark}{indent}{r['component']:<14} {r['event']:<24} {brief(r)}")
        if full:
            print(f"{'':16}{indent}{json.dumps(r.get('data'), indent=2, default=str)}".replace("\n", "\n" + " " * 16 + indent))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace", nargs="?", help="trace or session id (prefix ok)")
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-n", type=int, default=20, help="how many traces/errors to list")
    args = ap.parse_args(argv)
    recs = records()
    if not recs:
        print("No structured logs yet.")
        return 0

    if args.list:
        traces = OrderedDict()
        for r in recs:
            t = traces.setdefault(r["trace_id"], {"start": r["ts"], "components": set(), "errors": 0, "cmd": r["cmd"]})
            t["components"].add(r["component"])
            t["errors"] += r.get("level") == "error"
            t["end"] = r["ts"]
        for tid, t in list(traces.items())[-args.n:]:
            flag = f"  {t['errors']} error(s)" if t["errors"] else ""
            print(f"{tid}  {t['start'][:19]}  {', '.join(sorted(t['components']))}  {' '.join(t['cmd'])[:60]}{flag}")
        return 0

    if args.errors:
        bad = [r for r in recs if r.get("level") in ("warning", "error")][-args.n:]
        for r in bad:
            print(f"{r['ts'][:19]} {r['level']:<7} trace={r['trace_id']} {r['component']:<14} {r['event']:<20} {brief(r)}")
        return 0

    if args.last:
        tid = recs[-1]["trace_id"]
    elif args.trace:
        matches = sorted({r["trace_id"] for r in recs if r["trace_id"].startswith(args.trace)})
        if not matches:
            print(f"No trace starting with {args.trace!r}. Try --list.")
            return 1
        if len(matches) > 1:
            print("Ambiguous prefix; matches: " + ", ".join(matches))
            return 1
        tid = matches[0]
    else:
        ap.error("give a trace id, or --last / --list / --errors")
    selected = [r for r in recs if r["trace_id"] == tid]
    if args.json:
        json.dump(selected, sys.stdout, indent=2, default=str)
        print()
    else:
        print(f"trace {tid}: {len(selected)} records from {', '.join(sorted({r['component'] for r in selected}))}")
        show(selected, args.full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
