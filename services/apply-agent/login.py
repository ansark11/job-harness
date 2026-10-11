#!/usr/bin/env python3
"""Open the agent's browser so you can sign in to LinkedIn yourself.

    ./python/bin/python login.py [--check]

The session is kept in data/browser-profile/ (gitignored) and reused by
apply.py. Nothing here types or stores your password: you sign in by hand,
including any 2FA. Close the window when the feed shows.

--check: headless; prints whether the saved session is still signed in.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import runlog

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "data" / "browser-profile"


def signed_in(ctx):
    return any(c["name"] == "li_at" and c["value"] for c in ctx.cookies("https://www.linkedin.com"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for you to sign in")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    with runlog.start("apply-agent", HERE, argv) as run, sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=args.check)
        try:
            if args.check:
                ok = signed_in(ctx)
            else:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://www.linkedin.com/login")
                print("Sign in to LinkedIn in the browser window (waiting up to "
                      f"{args.timeout // 60} min)...", file=sys.stderr)
                deadline = time.monotonic() + args.timeout
                ok = False
                while time.monotonic() < deadline and ctx.pages:
                    if signed_in(ctx) and "/feed" in page.url:
                        ok = True
                        break
                    time.sleep(2)
        finally:
            ctx.close()
        print(json.dumps({"signed_in": ok, "profile": str(PROFILE)}))
        run.finish(0 if ok else 1, signed_in=ok)     # never the cookie itself
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
