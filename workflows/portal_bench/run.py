#!/usr/bin/env python3
"""Portal benchmark over your own opportunities: which portals you face, and how the agent does on each.

    python3 workflows/portal_bench/run.py [--status new|kept|discarded] [--urls extra.txt] [--limit N] [--watch]

1. Asks the tracker for every opportunity that applies on a company portal
   (apply_type external or unknown), skipping expired ones.
2. Adds any portal URLs listed one per line in --urls (to cover portal types
   your searches haven't surfaced yet).
3. Runs the apply-agent's benchmark (services/apply-agent/bench.py): resolves
   each portal behind LinkedIn's Apply button once (cached), then opens each
   form in a clean browser, fills it with a FAKE test person, never submits,
   and scores it.

Prints the summary; the full report is report.md in the output folder.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import runlog

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parents[1] / "services"
DATA = HERE / "data"
PORTAL_TYPES = ("external", "unknown")


class StepError(Exception):
    pass


def python_for(service_dir):
    venv = service_dir / "python" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def make_caller(run):
    def call(service, script, args, stream=False):
        sdir = SERVICES / service
        t0 = time.monotonic()
        proc = subprocess.run([python_for(sdir), script, *args], cwd=sdir, env=run.child_env(),
                              stdout=subprocess.PIPE, stderr=None if stream else subprocess.PIPE, text=True)
        err = (proc.stderr or "").strip()[-2000:] or None
        run.event("service_call", service=service, args=args, exit_code=proc.returncode,
                  duration_ms=round((time.monotonic() - t0) * 1000), stderr=err)
        if proc.returncode != 0:
            raise StepError(f"{service}/{script} exited {proc.returncode}: {(err or proc.stdout or '')[-300:]}")
        return json.loads(proc.stdout)
    return call


def build_targets(items, extra_urls=(), status=None):
    """Opportunities that apply on a company portal, plus extra portal URLs, as bench targets."""
    targets = []
    for it in items:
        if it.get("apply_type") not in PORTAL_TYPES or it.get("status") == "expired":
            continue
        if status and it.get("status") != status:
            continue
        targets.append({k: it.get(k) for k in ("id", "company", "title", "url")})
    for i, url in enumerate(u.strip() for u in extra_urls):
        if url and not url.startswith("#"):
            targets.append({"id": f"extra-{i + 1}", "company": url.split("/")[2], "title": "(extra URL)",
                            "url": url, "apply_url": url})
    return targets


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", choices=["new", "kept", "discarded"])
    ap.add_argument("--urls", help="file of extra portal URLs, one per line")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--watch", action="store_true", help="show a browser window for each form check")
    args = ap.parse_args(argv)

    with runlog.start("portal_bench", HERE, argv) as run:
        call = make_caller(run)
        items = call("tracker", "tracker.py", ["list"])["items"]
        extra = Path(args.urls).read_text().splitlines() if args.urls else []
        targets = build_targets(items, extra, args.status)
        work = DATA / datetime.now().strftime("%Y%m%dT%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        (work / "targets.json").write_text(json.dumps({"targets": targets}, indent=2))
        run.event("targets", count=len(targets), extra=len(extra))
        print(f"Benchmarking {len(targets)} portal posting(s). Nothing is submitted. Progress prints below.",
              file=sys.stderr, flush=True)
        bench_args = ["bench.py", "run", str(work / "targets.json"), "--out", str(work / "bench")]
        if args.limit:
            bench_args += ["--limit", str(args.limit)]
        if args.watch:
            bench_args += ["--watch"]
        out = call("apply-agent", bench_args[0], bench_args[1:], stream=True)
        print(json.dumps(out, indent=2))
        run.finish(0, verdicts=out.get("verdicts"), report=out.get("report"))
        return 0


if __name__ == "__main__":
    sys.exit(main())
