#!/usr/bin/env python3
"""Daily search: job-search -> matcher -> tracker ingest, then a notification.

    python3 workflows/daily_search/run.py [--resume resume.txt] [--force] [--max-pages N] [--no-notify]

Scheduled by launchd at 19:00 and at login (see launchd/). launchd runs a
missed 19:00 job when the Mac wakes, so the guard below is what makes extra
triggers harmless: if a run already succeeded since the most recent 19:00,
this exits without doing anything (--force overrides).

The search window starts where the last successful run started, so nothing
posted between runs is missed (job-search clamps it to 24h..30d).

Services are called only through their CLIs; their logs join this run's
trace (python3 scripts/trace.py --last).
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import runlog

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SERVICES = ROOT / "services"
CRITERIA = ROOT / "criteria.json"
DATA = HERE / "data"
KIND = "daily_search"
RUN_HOUR = 19
KEEP_RUN_DIRS = 14


class StepError(Exception):
    pass


def boundary(now_local, hour=RUN_HOUR):
    """The most recent scheduled run time at or before now (local)."""
    today = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return today if now_local >= today else today - timedelta(days=1)


def already_ran(last, now_local, hour=RUN_HOUR):
    """True if the last successful run finished after the most recent boundary."""
    if not last or not last.get("finished_at"):
        return False
    return datetime.fromisoformat(last["finished_at"]) >= boundary(now_local, hour)


def python_for(service_dir):
    venv = service_dir / "python" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def make_caller(run):
    """call(service, args) -> stdout. Each call is logged as a service_call event."""
    def call(service, args, stdin=None):
        sdir = SERVICES / service
        script = {"job-search": "search.py", "matcher": "match.py", "tracker": "tracker.py"}[service]
        t0 = time.monotonic()
        proc = subprocess.run([python_for(sdir), script, *args], cwd=sdir, env=run.child_env(),
                              input=stdin, capture_output=True, text=True)
        ms = round((time.monotonic() - t0) * 1000)
        err = proc.stderr.strip()[-2000:] or None
        run.event("service_call", service=service, args=args, exit_code=proc.returncode, duration_ms=ms,
                  stderr=err)
        if proc.returncode != 0:
            last_line = err.splitlines()[-1] if err else "no output"
            raise StepError(f"{service} exited {proc.returncode}: {last_line}")
        return proc.stdout
    return call


def tracker_json(call, *args):
    return json.loads(call("tracker", list(args)))


def notify(title, message):
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def resolve_resume(arg, call):
    """--resume wins; otherwise the tracker's default resume (added with resume management)."""
    if arg:
        return Path(arg).resolve()
    try:
        out = tracker_json(call, "resume", "default")
    except (StepError, json.JSONDecodeError) as e:
        raise StepError("no resume: pass --resume, or add one with tracker.py resume add") from e
    if not out.get("text_path"):
        raise StepError("no default resume: add one with tracker.py resume add")
    return Path(out["text_path"])


def prune(data_dir, keep=KEEP_RUN_DIRS):
    dirs = sorted((d for d in data_dir.glob("*") if d.is_dir()), key=lambda d: d.name)
    for d in dirs[:-keep]:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def run_daily(args, call, now, run, notifier=notify):
    """Returns (exit code, summary dict)."""
    last = tracker_json(call, "run", "last-success", "--kind", KIND)["last_success"]
    if not args.force and already_ran(last, now.astimezone()):
        run.event("skipped", reason="already ran since the last 19:00", last_finished=last["finished_at"])
        return 0, {"skipped": True}

    resume = resolve_resume(args.resume, call)
    rec = tracker_json(call, "run", "start", "--kind", KIND)
    work = DATA / f"{now.strftime('%Y%m%dT%H%M%S')}-{rec['id']}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        search_args = ["--criteria", str(CRITERIA), "--out", str(work / "postings.json")]
        if last:
            search_args += ["--since", last["started_at"]]
        if args.max_pages:
            search_args += ["--max-pages", str(args.max_pages)]
        call("job-search", search_args)

        call("matcher", [str(work / "postings.json"), "--criteria", str(CRITERIA), "--resume", str(resume),
                         "--out", str(work / "matches.json")])
        ingest = tracker_json(call, "ingest", str(work / "matches.json"), "--run-id", rec["id"])

        postings = json.loads((work / "postings.json").read_text())
        matches = json.loads((work / "matches.json").read_text())
        stats = {"postings": len(postings["postings"]), "matches": len(matches["matches"]),
                 "rejected": len(matches["rejected"]), "added": len(ingest["added"]), "merged": len(ingest.get("merged", [])),
                 "expired": len(ingest["expired"]), "window_seconds": postings["window_seconds"]}
    except (StepError, OSError, json.JSONDecodeError, KeyError) as e:
        tracker_json(call, "run", "finish", rec["id"], "--status", "failed", "--stats", json.dumps({"error": str(e)}))
        run.error("daily_search_failed", error=str(e), run_id=rec["id"])
        if not args.no_notify:
            notifier("Job search failed", str(e)[:180])
        return 1, {"error": str(e)}

    tracker_json(call, "run", "finish", rec["id"], "--status", "ok", "--stats", json.dumps(stats))
    prune(DATA)
    if not args.no_notify:
        n = stats["added"]
        notifier("Job search", f"{n} new match{'es' if n != 1 else ''} to review" if n else "No new matches today")
    return 0, stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", help="resume text file (default: the tracker's default resume)")
    ap.add_argument("--force", action="store_true", help="run even if already done since the last 19:00")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)

    with runlog.start(KIND, HERE, argv) as run:
        code, summary = run_daily(args, make_caller(run), datetime.now(timezone.utc), run)
        print(json.dumps(summary))
        run.finish(code, **summary)
        return code


if __name__ == "__main__":
    sys.exit(main())
