#!/usr/bin/env python3
"""Step 1 of docs/agent-fill-experiment.md: the test set.

    ../../python/bin/python build_targets.py [--results <bench results.json>]

Takes every posting the rules engine graded fail or unreachable in the latest full
benchmark, plus a few passes as controls (skips closed and manual ones). Loads each
portal URL once (no LinkedIn, nothing filled) and drops postings that have closed.
Writes targets.json next to this file.

Run with the apply-agent venv: it reuses bench.py read-only.
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parents[1]
sys.path.insert(0, str(AGENT))

import bench  # noqa: E402

LATEST = AGENT.parents[1] / "workflows" / "portal_bench" / "data" / "20261024T175043" / "bench" / "results.json"
CONTROLS = ("Coinflow", "Marqeta", "Affirm")      # stable passes on three portal setups (Ashby x2, Greenhouse)


def pick(results):
    targets = [dict(t, role="target") for t in results if t.get("verdict") in ("fail", "unreachable")]
    controls = [dict(t, role="control") for t in results if t.get("company") in CONTROLS]
    return targets + controls


def still_open(page, url):
    """(open?, note) after loading the portal URL once."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return False, f"didn't load: {type(e).__name__}"
    if resp is not None and resp.status in (404, 410):
        return False, f"HTTP {resp.status}"
    page.wait_for_timeout(2500)
    closed = bench.closed_text(page)
    return (False, f'closed: "{closed}"') if closed else (True, "open")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(LATEST))
    args = ap.parse_args(argv)
    data = json.loads(Path(args.results).read_text())
    results = data["results"] if isinstance(data, dict) and "results" in data else data
    chosen = pick(results)

    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for t in chosen:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            ok, note = still_open(ctx.new_page(), t["apply_url"])
            ctx.close()
            print(f"{t['role']:<8} {t['verdict']:<12} {t.get('ats', ''):<20} {t['company'][:22]:<22} {note}")
            if ok:
                out.append({k: t.get(k) for k in ("id", "company", "title", "ats", "apply_url", "role")} |
                           {"rules_verdict": t["verdict"], "rules_reasons": t.get("reasons", []),
                            "rules_dir": t.get("dir")})
        browser.close()
    (HERE / "targets.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"\n{len(out)} of {len(chosen)} still open -> targets.json")


if __name__ == "__main__":
    main()
