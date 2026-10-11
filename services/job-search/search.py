#!/usr/bin/env python3
"""Search LinkedIn's public job pages and print normalized postings as JSON.

    python3 search.py --criteria ../../criteria.json [--since 2026-10-10T19:00:00-04:00]
                      [--max-pages 5] [--out postings.json]

Reads the "search" section of the criteria file: keywords x locations, work
types, job types, title include/exclude patterns, max age. One query per
keyword and location; pages until a page is empty, every card on it was
already seen, or --max-pages. Cards whose title fails the pattern are skipped
before their detail page is fetched (logged with the reason). Postings older
than max_age_days are dropped.

Output: {"generated_at", "window_seconds", "queries": [...],
         "postings": [...], "skipped": [{"id", "title", "reason"}]}
Exit 0 on success, 2 if every query failed (likely rate-limited or blocked).
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import linkedin
import runlog

HERE = Path(__file__).resolve().parent
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
RETRY_STATUSES = {429, 999, 502, 503}   # 999 is LinkedIn's "slow down / blocked"
BACKOFF = (30, 90)


class FetchError(Exception):
    pass


class Fetcher:
    """Throttled GET with backoff. `get` is injectable so tests never touch the network."""

    def __init__(self, delay=(2.0, 4.0), get=None, sleep=None):
        self.delay = delay
        self.sleep = sleep or time.sleep
        self.get = get or self._urllib_get
        self.requests = 0
        self._last = None

    @staticmethod
    def _urllib_get(url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except urllib.error.URLError as e:
            raise FetchError(f"unreachable: {e.reason}") from e

    def fetch(self, url):
        for attempt in range(len(BACKOFF) + 1):
            if self._last is not None:
                self.sleep(random.uniform(*self.delay))
            self._last = time.monotonic()
            self.requests += 1
            status, body = self.get(url)
            if status == 200:
                return body
            if status in RETRY_STATUSES and attempt < len(BACKOFF):
                self.sleep(BACKOFF[attempt])
                continue
            raise FetchError(f"HTTP {status}")
        raise FetchError("retries exhausted")


def title_filter(cfg):
    inc = re.compile(cfg["title_include"], re.I) if cfg.get("title_include") else None
    exc = re.compile(cfg["title_exclude"], re.I) if cfg.get("title_exclude") else None

    def check(title):
        t = title or ""
        if inc and not inc.search(t):
            return "title does not match title_include"
        if exc and exc.search(t):
            return f"title matches title_exclude ({exc.search(t).group(0)!r})"
        return None
    return check


def run_search(cfg, fetcher, now, since=None, max_pages=None, run=None):
    log = run or _NullRun()
    window = linkedin.window_seconds(since, now)
    max_pages = max_pages or cfg.get("max_pages", 5)
    max_age = cfg.get("max_age_days", 30)
    check_title = title_filter(cfg)

    cards, hits, queries, skipped = {}, {}, [], []
    for keywords in cfg["keywords"]:
        for location in cfg["locations"]:
            q = {"keywords": keywords, "location": location, "pages": 0, "results": 0, "new": 0, "error": None}
            for page in range(max_pages):
                url = linkedin.search_url(keywords, location, window, cfg.get("work_types", ()),
                                          cfg.get("job_types", ()), start=page * linkedin.PAGE_SIZE)
                try:
                    page_cards = linkedin.parse_search(fetcher.fetch(url))
                except FetchError as e:
                    q["error"] = str(e)
                    log.warn("search_failed", keywords=keywords, location=location, page=page, error=str(e))
                    break
                q["pages"] += 1
                q["results"] += len(page_cards)
                new = [c for c in page_cards if c["id"] not in cards]
                q["new"] += len(new)
                log.event("search_query", keywords=keywords, geo=location, page=page,
                          results=len(page_cards), new=len(new))
                for c in page_cards:
                    hits.setdefault(c["id"], []).append({"keywords": keywords, "location": location})
                    cards.setdefault(c["id"], c)
                if len(page_cards) < linkedin.PAGE_SIZE or not new:
                    break
            queries.append(q)

    if queries and all(q["error"] and q["pages"] == 0 for q in queries):
        return {"window_seconds": window, "queries": queries, "postings": [], "skipped": [], "failed": True}

    postings = []
    for job_id, card in cards.items():
        reason = check_title(card.get("title"))
        if reason:
            skipped.append({"id": job_id, "title": card.get("title"), "company": card.get("company"), "reason": reason})
            log.event("posting_skipped", id=job_id, title=card.get("title"), reason=reason)
            continue
        try:
            detail = linkedin.parse_posting(fetcher.fetch(linkedin.posting_url(job_id)))
        except FetchError as e:
            detail = {"detail_error": str(e)}
            log.warn("detail_failed", id=job_id, error=str(e))
        posting = linkedin.merge(card, detail, now)
        posting["search_hits"] = hits[job_id]
        age = linkedin.age_days(posting, now)
        if age is not None and age >= max_age:
            reason = f"posted {age:.0f} days ago (max {max_age})"
            skipped.append({"id": job_id, "title": posting.get("title"), "company": posting.get("company"),
                            "reason": reason})
            log.event("posting_skipped", id=job_id, title=posting.get("title"), reason=reason)
            continue
        postings.append(posting)

    postings.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
    return {"window_seconds": window, "queries": queries, "postings": postings, "skipped": skipped, "failed": False}


class _NullRun:
    def event(self, *a, **k):
        pass

    warn = event


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--criteria", required=True, help="criteria JSON; its 'search' section is used")
    ap.add_argument("--since", help="ISO time of the last successful run (window = since..now, 24h-30d)")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    with runlog.start("job-search", HERE, argv) as run:
        criteria = json.loads(Path(args.criteria).read_text())
        if "search" not in criteria:
            sys.exit(f"{args.criteria} has no 'search' section (see criteria.example.json)")
        cfg = criteria["search"]
        now = datetime.now(timezone.utc)
        since = datetime.fromisoformat(args.since) if args.since else None
        if since and since.tzinfo is None:
            since = since.astimezone()
        fetcher = Fetcher()
        result = run_search(cfg, fetcher, now, since, args.max_pages, run)
        result = {"generated_at": now.isoformat(timespec="seconds"), **result}
        text = json.dumps(result, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n")
        else:
            print(text)
        summary = {"postings": len(result["postings"]), "skipped": len(result["skipped"]),
                   "queries": len(result["queries"]), "requests": fetcher.requests,
                   "window_seconds": result["window_seconds"], "output_digest": runlog.digest(result["postings"])}
        print(f"job-search: {summary['postings']} postings, {summary['skipped']} skipped, "
              f"{summary['requests']} requests", file=sys.stderr)
        code = 2 if result["failed"] else 0
        run.finish(code, **summary)
        return code


if __name__ == "__main__":
    sys.exit(main())
