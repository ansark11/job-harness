#!/usr/bin/env python3
"""Step 4 (Arm A): browser-use on every posting in targets.json, with the fake packet.

    ./python/bin/python run_agents.py [--only "Engine,Stripe"] [--watch]
    ./python/bin/python run_agents.py --continue data/<timestamp>     # rerun unfinished / interrupted

Per posting, in a fresh browser behind the safety harness (network blocker armed throughout,
action guard on, each agent run its own process, 40 steps / 10 min, hard kill at 12):
  1. the agent gets the Arm A task (reach the form, fill from the packet, never submit);
  2. the moment it stops, the final page is captured for Step 5: every field's value (all
     frames), the census of controls, invalid fields, CAPTCHA state, screenshot, page.html,
     and a Playwright trace of the whole run.
Each posting's result.json is written as soon as it finishes, so a closed lid costs only
the run in progress: runs spanning a sleep are flagged "interrupted" and redone by --continue.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import harness

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import bench  # noqa: E402
import forms  # noqa: E402

TASK = """Open {url}. Get to the job application form (click through job descriptions, "Apply",
"Apply manually", and close cookie banners or pop-ups). Fill every field using only the values in
this packet: {packet}. Upload the resume file for resume/CV fields. Do not submit, sign in, or
create an account. If the portal requires an account or email verification, stop and report
`login_required`. When every field you can fill is filled, stop and report: fields filled, fields
left blank (`unanswered`), and any errors shown on the page."""

FIELDS_JS = f"""
(root) => {{
  const fields = ({forms.EXTRACT_JS})(root);
  const buttons = ({forms.BUTTON_CHOICES_JS})(root);
  return fields.concat(buttons);
}}
"""


def slug(i, company):
    return f"{i:02d}-{re.sub(r'[^A-Za-z0-9]+', '-', company)[:30]}"


async def capture(ctx, out_dir):
    """Everything Step 5 needs from the page the agent ended on."""
    pages = [p for p in ctx.pages if p.url and not p.url.startswith(("about:", "chrome:"))]
    if not pages:
        return {"final_url": None, "frames": []}
    page = pages[-1]
    snap = {"final_url": page.url, "frames": []}
    try:
        await page.screenshot(path=str(out_dir / "final.png"), full_page=True)
        (out_dir / "page.html").write_text(await page.content())
        (out_dir / "aria.txt").write_text(await page.locator("body").aria_snapshot())
    except Exception as e:
        snap["capture_error"] = f"{type(e).__name__}: {e}"[:200]
    for frame in page.frames:
        entry = {"url": frame.url}
        for name, js in (("fields", FIELDS_JS), ("census", bench.CENSUS_JS), ("invalid", bench.INVALID_JS),
                         ("captcha", bench.CAPTCHA_JS)):
            try:
                entry[name] = await frame.evaluate(js, None) if name == "fields" else await frame.evaluate(js)
            except Exception as e:
                entry[name] = None
                entry.setdefault("errors", []).append(f"{name}: {type(e).__name__}")
        try:
            entry["password_fields"] = await frame.locator("input[type=password]").count()
            entry["text"] = (await frame.locator("body").inner_text(timeout=3000))[:4000]
        except Exception:
            pass
        if entry.get("fields") or frame == page.main_frame:
            snap["frames"].append(entry)
    return snap


async def run_target(i, t, packet, resume, out_root, headless):
    out_dir = out_root / slug(i, t["company"])
    out_dir.mkdir(parents=True, exist_ok=True)
    pw, ctx, cdp_url, blocker = await harness.launch(headless)
    await ctx.tracing.start(screenshots=True, snapshots=True)
    try:
        task = TASK.format(url=t["apply_url"], packet=json.dumps(packet))
        result = await harness.run_isolated({"task": task, "cdp_url": cdp_url, "resume": str(resume),
                                             "max_steps": harness.MAX_STEPS})
        snap = await capture(ctx, out_dir)
    finally:
        try:
            await ctx.tracing.stop(path=str(out_dir / "trace.zip"))
        except Exception:
            pass
        await ctx.close()
        await pw.stop()
    record = {"target": t, **result, "blocked": blocker.blocked, "attempted_submit": blocker.attempted_submit,
              "snapshot": snap, "finished_at": datetime.now().isoformat(timespec="seconds")}
    harness.dump(out_dir / "result.json", record)
    return record


def done_already(out_dir):
    f = out_dir / "result.json"
    if not f.exists():
        return False
    r = json.loads(f.read_text())
    return not (r.get("interrupted") or r.get("hard_killed") and not r.get("steps") or r.get("config_error")
                or r.get("error"))


async def main_async(args):
    harness.keep_awake()
    targets = json.loads((HERE / "targets.json").read_text())
    if args.only:
        wanted = {c.strip().lower() for c in args.only.split(",")}
        targets = [t for t in targets if t["company"].lower() in wanted]
    packet = json.loads((HERE / "persona.json").read_text())
    out_root = Path(args.cont) if args.cont else HERE / "data" / datetime.now().strftime("%Y%m%dT%H%M%S")
    out_root.mkdir(parents=True, exist_ok=True)
    resume = bench.placeholder_pdf(HERE / "data" / "Test_Resume.pdf")
    packet["files"]["resume"] = str(resume)
    harness.dump(out_root / "run.json", {"targets": len(targets), "started": datetime.now().isoformat(timespec="seconds"),
                                         "model": harness.MODEL, "max_steps": harness.MAX_STEPS,
                                         "time_limit": harness.TIME_LIMIT})
    print(f"Output: {out_root}", flush=True)
    for i, t in enumerate(targets, 1):
        out_dir = out_root / slug(i, t["company"])
        if args.cont and done_already(out_dir):
            print(f"[{i}/{len(targets)}] {t['company']}: already done, skipping", flush=True)
            continue
        r = await run_target(i, t, packet, resume, out_root, not args.watch)
        flags = [k for k in ("interrupted", "hard_killed", "timed_out", "config_error", "error") if r.get(k)]
        print(f"[{i}/{len(targets)}] {t['company']:<16} steps={r.get('steps')} {r.get('seconds')}s "
              f"${r.get('cost_usd')} blocked={len(r['blocked'])} refused={len(r.get('refusals', []))} "
              f"attempted_submit={r['attempted_submit']} final={str(r['snapshot'].get('final_url'))[:60]} "
              f"{' '.join(flags)}", flush=True)
    print("finished", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated company names")
    ap.add_argument("--continue", dest="cont", help="an earlier output folder: redo only unfinished/interrupted")
    ap.add_argument("--watch", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
