#!/usr/bin/env python3
"""Filter job-search postings against the criteria and score fit against a resume.

    python3 match.py postings.json --criteria ../../criteria.json --resume resume.txt [--out matches.json]

For each posting: prefilter (no model call) -> extract facts and fit (model,
cached) -> decide (code). Matches are sorted with postings from the last 24
hours first, then by fit score. Every rejection is kept with its reasons.

Output: {"generated_at", "resume_digest", "criteria_digest",
         "matches": [posting + "match": {score, reasons, concerns, flags, facts, pay, industry, fresh}],
         "rejected": [{id, title, company, stage, reasons}]}
Exit 0 on success (even if some postings couldn't be scored: those are kept
as matches flagged "not scored"), 2 if the model failed for every posting.
"""

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import extract
import llm
import rules
import runlog

HERE = Path(__file__).resolve().parent
CACHE_PATH = HERE / "data" / "extractions.json"
PROMPT_VERSION = 4
FRESH_HOURS = 24
WORKERS = 4


def cache_key(posting, resume_digest):
    return runlog.digest([PROMPT_VERSION, llm.MODEL, posting.get("id"), posting.get("title"),
                          posting.get("description"), resume_digest])


def load_cache(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(path)


def is_fresh(posting, now):
    if not posting.get("posted_at"):
        return False
    return (now - datetime.fromisoformat(posting["posted_at"])).total_seconds() < FRESH_HOURS * 3600


def match_postings(postings, cfg, resume, now, post=None, cache=None, run=None, on_cached=None):
    """(matches, rejected, stats). `cache` is a dict updated in place; `on_cached` is called
    after each new extraction so a crash later never loses calls already paid for."""
    log = run or _NullRun()
    cache = {} if cache is None else cache
    resume_digest = runlog.digest(resume)
    matches, rejected = [], []
    stats = {"postings": len(postings), "prefiltered": 0, "model_calls": 0, "cache_hits": 0, "model_errors": 0}

    todo = []
    for p in postings:
        reasons = rules.prefilter(p, cfg, now)
        if reasons:
            stats["prefiltered"] += 1
            rejected.append(_reject(p, "prefilter", reasons))
            log.event("match_decision", id=p.get("id"), title=p.get("title"), company=p.get("company"),
                      decision="reject", stage="prefilter", reasons=reasons)
        else:
            todo.append(p)

    def facts_for(p):
        key = cache_key(p, resume_digest)
        if key in cache:
            return p, cache[key]["facts"], None, True
        try:
            facts, call = extract.extract(p, resume, post)
        except llm.ModelError as e:
            return p, None, str(e), False
        with lock:
            cache[key] = {"facts": facts, "at": now.isoformat(timespec="seconds"), "id": p.get("id")}
            if on_cached:
                on_cached(cache)
        return p, facts, call, False

    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(facts_for, todo))

    for p, facts, call_or_error, hit in results:
        stats["cache_hits" if hit else "model_calls"] += 1
        fresh = is_fresh(p, now)
        if facts is None:
            stats["model_errors"] += 1
            log.warn("extract_failed", id=p.get("id"), error=call_or_error)
            matches.append({**p, "match": {"score": None, "reasons": [], "concerns": [], "fresh": fresh,
                                           "flags": [f"not scored: {call_or_error}"], "facts": None,
                                           "pay": None, "industry": None}})
            continue
        if call_or_error and not hit:
            log.event("model_call", id=p.get("id"), **call_or_error)
        reasons, flags, derived = rules.decide(p, facts, cfg)
        log.event("match_decision", id=p.get("id"), title=p.get("title"), company=p.get("company"),
                  decision="reject" if reasons else "match", stage="rules", reasons=reasons, flags=flags,
                  score=facts["fit_score"], facts=facts, pay=derived["pay"], cached=hit)
        if reasons:
            rejected.append(_reject(p, "rules", reasons, facts["fit_score"]))
        else:
            matches.append({**p, "match": {"score": facts["fit_score"], "reasons": facts["fit_reasons"],
                                           "concerns": facts["concerns"], "flags": flags, "facts": facts,
                                           "pay": derived["pay"], "industry": derived["industry"],
                                           "role_domain": derived["role_domain"], "fresh": fresh}})

    matches.sort(key=lambda m: (m["match"]["fresh"], m["match"]["score"] if m["match"]["score"] is not None
                                else -1), reverse=True)
    stats["matches"] = len(matches)
    stats["rejected"] = len(rejected)
    return matches, rejected, stats


def _reject(p, stage, reasons, score=None):
    return {"id": p.get("id"), "title": p.get("title"), "company": p.get("company"), "url": p.get("url"),
            "stage": stage, "reasons": reasons, "score": score}


class _NullRun:
    def event(self, *a, **k):
        pass

    warn = event


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("postings", help="job-search output JSON")
    ap.add_argument("--criteria", required=True, help="criteria JSON; its 'match' section is used")
    ap.add_argument("--resume", required=True, help="resume as plain text")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    with runlog.start("matcher", HERE, argv) as run:
        data = json.loads(Path(args.postings).read_text())
        postings = data["postings"] if isinstance(data, dict) else data
        criteria = json.loads(Path(args.criteria).read_text())
        if "match" not in criteria:
            sys.exit(f"{args.criteria} has no 'match' section (see criteria.example.json)")
        cfg = criteria["match"]
        resume = Path(args.resume).read_text()
        now = datetime.now(timezone.utc)
        cache = {} if args.no_cache else load_cache(CACHE_PATH)

        saver = None if args.no_cache else (lambda c: save_cache(CACHE_PATH, c))
        matches, rejected, stats = match_postings(postings, cfg, resume, now, post, cache, run, saver)

        result = {"generated_at": now.isoformat(timespec="seconds"), "resume_digest": runlog.digest(resume),
                  "criteria_digest": runlog.digest(cfg), "matches": matches, "rejected": rejected}
        text = json.dumps(result, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n")
        else:
            print(text)
        print(f"matcher: {stats['matches']} matches, {stats['rejected']} rejected "
              f"({stats['model_calls']} model calls, {stats['cache_hits']} cached, "
              f"{stats['model_errors']} errors)", file=sys.stderr)
        scored = stats["postings"] - stats["prefiltered"]
        code = 2 if scored and stats["model_errors"] == scored else 0
        run.finish(code, **stats, input_digest=runlog.digest(postings), output_digest=runlog.digest(matches))
        return code


if __name__ == "__main__":
    sys.exit(main())
