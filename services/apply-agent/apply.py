#!/usr/bin/env python3
"""Fill (and optionally submit) one application in a real browser.

    ./python/bin/python apply.py packet.json --mode fill|submit [--dry-run] [--headless]

packet.json (built by workflows/apply):
  {"application_id", "job": {"id", "url", "apply_type"},
   "resume_file", "resume_upload_name",          # uploaded under the readable name
   "answers": {<field key>: <value>},            # keys are forms.normalize(label)
   "skip": [<field key>, ...]}                   # leave these blank

Uses the persistent browser profile in data/browser-profile/ (sign in once
with login.py). Prints a report: {"status", "fields", "unknown", "errors",
"screenshots", ...}. status is one of submitted, ready, needs_input,
already_applied, not_easy_apply, login_required, cap_reached, error.

Safeguards: --dry-run never clicks Submit; at most --daily-cap submissions
per local day (counted in data/submissions.jsonl); headed by default so you
can watch; short random pauses between actions.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import easyapply
import runlog

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PROFILE = DATA / "browser-profile"
SUBMISSIONS = DATA / "submissions.jsonl"
DEFAULT_CAP = 10


def submitted_today(path=SUBMISSIONS, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone()
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text().splitlines():
        try:
            at = datetime.fromisoformat(json.loads(line)["at"]).astimezone()
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        count += at.date() == now.date()
    return count


def record_submission(packet, path=SUBMISSIONS):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "application_id": packet.get("application_id"), "job_id": packet["job"].get("id")}) + "\n")


def upload_copy(packet):
    """Copy the resume to data/uploads/<readable name> so employers see a sensible file name."""
    src = Path(packet["resume_file"])
    name = packet.get("resume_upload_name") or src.name
    dest = DATA / "uploads" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return {**packet, "resume_file": str(dest), "resume_upload_name": name}


def run_packet(page, packet, mode, dry_run, shots_dir, cap, pause=(0.3, 0.9)):
    apply_type = packet["job"].get("apply_type")
    if apply_type not in ("easy", "unknown", None):
        return {"status": "error", "error": f"apply type {apply_type!r} isn't supported yet", "fields": []}
    walk = easyapply.Walk(page, packet, mode, dry_run, shots_dir, pause)
    report = walk.run(cap_ok=lambda: submitted_today() < cap)
    if report["status"] == "submitted":
        record_submission(packet)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packet")
    ap.add_argument("--mode", choices=["fill", "submit"], required=True)
    ap.add_argument("--dry-run", action="store_true", help="never click Submit")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--daily-cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--profile-dir", default=str(PROFILE))
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    with runlog.start("apply-agent", HERE, argv) as run:
        packet = upload_copy(json.loads(Path(args.packet).read_text()))
        app_id = packet.get("application_id") or "adhoc"
        shots = DATA / "screens" / f"{app_id}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(args.profile_dir, headless=args.headless,
                                                       viewport={"width": 1280, "height": 900})
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                report = run_packet(page, packet, args.mode, args.dry_run, shots, args.daily_cap)
            except Exception as e:      # a browser failure must still produce a report
                report = {"status": "error", "error": f"{type(e).__name__}: {e}", "fields": []}
            finally:
                ctx.close()
        report.update(application_id=packet.get("application_id"), job_id=packet["job"].get("id"),
                      mode=args.mode, dry_run=args.dry_run)
        print(json.dumps(report, indent=2))
        for f in report.get("fields", []):
            # What went into each field and where it came from. Values only for
            # non-sensitive outcomes; the packet's sensitive answers stay out of the log.
            run.event("apply_step", application_id=app_id, step=f.get("page"), status=f.get("outcome"),
                      note=f.get("label"), type=f.get("type"), source=f.get("source"), error=f.get("error"))
        run.finish(0 if report["status"] != "error" else 1, status=report["status"],
                   unknown=len(report.get("unknown", [])), screenshots=len(report.get("screenshots", [])))
        return 0 if report["status"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
