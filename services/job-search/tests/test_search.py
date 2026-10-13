"""The search loop: paging, dedupe, title skip, age cutoff, backoff. No network.

    python3 -m unittest discover -s tests -t .
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import search  # noqa: E402

NOW = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)


def card_html(job_id, title, date="2026-10-11", ago="2 hours ago"):
    return f"""<li><div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">
      <a class="base-card__full-link" href="https://ca.linkedin.com/jobs/view/x-{job_id}?trk=1"></a>
      <div class="base-search-card__info"><h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle"><a href="https://ca.linkedin.com/company/acme?trk=x">Acme</a></h4>
      <span class="job-search-card__location">Toronto, Ontario, Canada</span>
      <time datetime="{date}">{ago}</time></div></div></li>"""


def posting_html(ago="2 hours ago", onsite=True):
    control = "public_jobs_apply-link-onsite" if onsite else "public_jobs_apply-link-offsite_x"
    return f"""<h2 class="top-card-layout__title">Senior Product Manager</h2>
      <span class="posted-time-ago__text">{ago}</span>
      <button data-tracking-control-name="{control}">Apply</button>
      <ul><li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Industries</h3>
      <span class="description__job-criteria-text">Financial Services</span></li></ul>
      <div class="show-more-less-html__markup">Build payments.</div>"""


class FakeWeb:
    """Serves search pages by (keywords, start) and postings by id."""

    def __init__(self, pages, postings, statuses=None):
        self.pages = pages
        self.postings = postings
        self.statuses = list(statuses or [])
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if self.statuses:
            return self.statuses.pop(0), ""
        if "/jobPosting/" in url:
            return 200, self.postings.get(url.rsplit("/", 1)[1], "")
        q = parse_qs(urlsplit(url).query)
        return 200, self.pages.get((q["keywords"][0], int(q["start"][0])), "")


CFG = {"keywords": ["Senior Product Manager"], "locations": ["Toronto, Ontario, Canada"],
       "work_types": ["remote", "hybrid"], "job_types": ["full-time"],
       "title_include": "product manag", "title_exclude": "marketing", "max_pages": 3, "max_age_days": 30}


def fetcher(web):
    return search.Fetcher(get=web.get, sleep=lambda s: None)


class RunSearchTests(unittest.TestCase):
    def test_pages_until_short_page_and_skips_titles_before_detail_fetch(self):
        page0 = "".join(card_html(i, "Senior Product Manager") for i in range(1, 10))
        page0 += card_html(10, "Senior Product Manager, Marketing Tech")
        page1 = card_html(11, "Principal Product Manager")
        web = FakeWeb({("Senior Product Manager", 0): page0, ("Senior Product Manager", 10): page1},
                      {str(i): posting_html() for i in range(1, 12)})
        out = search.run_search(CFG, fetcher(web), NOW)

        self.assertFalse(out["failed"])
        self.assertEqual(len(out["postings"]), 10)
        self.assertEqual(out["queries"][0]["pages"], 2)       # page 1 had < 10 cards: stop
        self.assertEqual([s["id"] for s in out["skipped"]], ["10"])
        self.assertIn("title_exclude", out["skipped"][0]["reason"])
        self.assertFalse(any(u.endswith("/jobPosting/10") for u in web.urls))   # never fetched
        p = out["postings"][0]
        self.assertEqual(p["apply_type"], "easy")
        self.assertEqual(p["criteria"]["industries"], "Financial Services")
        self.assertEqual(p["search_hits"], [{"keywords": "Senior Product Manager",
                                             "location": "Toronto, Ontario, Canada"}])

    def test_dedupes_across_queries_and_records_every_hit(self):
        cfg = {**CFG, "keywords": ["Senior Product Manager", "Principal Product Manager"]}
        same = card_html(1, "Senior Product Manager")
        web = FakeWeb({("Senior Product Manager", 0): same, ("Principal Product Manager", 0): same},
                      {"1": posting_html()})
        out = search.run_search(cfg, fetcher(web), NOW)
        self.assertEqual(len(out["postings"]), 1)
        self.assertEqual(len(out["postings"][0]["search_hits"]), 2)
        self.assertEqual(sum(u.endswith("/jobPosting/1") for u in web.urls), 1)

    def test_drops_postings_older_than_max_age(self):
        web = FakeWeb({("Senior Product Manager", 0): card_html(1, "Senior Product Manager", "2026-09-01", "")},
                      {"1": posting_html(ago="1 month ago")})
        out = search.run_search(CFG, fetcher(web), NOW)
        self.assertEqual(out["postings"], [])
        self.assertIn("days ago", out["skipped"][0]["reason"])

    def test_backs_off_on_rate_limit_then_succeeds(self):
        sleeps = []
        web = FakeWeb({("Senior Product Manager", 0): card_html(1, "Senior Product Manager")},
                      {"1": posting_html()}, statuses=[429])
        f = search.Fetcher(get=web.get, sleep=sleeps.append)
        out = search.run_search(CFG, f, NOW)
        self.assertEqual(len(out["postings"]), 1)
        self.assertIn(search.BACKOFF[0], sleeps)

    def test_network_blip_is_retried(self):
        sleeps, attempts = [], []
        web = FakeWeb({("Senior Product Manager", 0): card_html(1, "Senior Product Manager")}, {"1": posting_html()})

        def get(url):
            attempts.append(url)
            if len(attempts) == 1:
                raise search.FetchError("unreachable: [Errno 8] nodename nor servname provided")
            return web.get(url)
        out = search.run_search(CFG, search.Fetcher(get=get, sleep=sleeps.append), NOW)
        self.assertEqual(len(out["postings"]), 1)
        self.assertIn(search.BACKOFF[0], sleeps)

    def test_every_query_blocked_is_failure(self):
        web = FakeWeb({}, {}, statuses=[999] * 10)
        out = search.run_search(CFG, fetcher(web), NOW)
        self.assertTrue(out["failed"])
        self.assertIn("HTTP 999", out["queries"][0]["error"])

    def test_detail_failure_keeps_posting_with_error(self):
        web = FakeWeb({("Senior Product Manager", 0): card_html(1, "Senior Product Manager")}, {})
        web.get_orig = web.get

        def get(url):
            return (404, "") if "/jobPosting/" in url else web.get_orig(url)
        out = search.run_search(CFG, search.Fetcher(get=get, sleep=lambda s: None), NOW)
        self.assertEqual(out["postings"][0]["detail_error"], "HTTP 404")


class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_exit_code(self):
        web = FakeWeb({("Senior Product Manager", 0): card_html(1, "Senior Product Manager")},
                      {"1": posting_html()})
        with tempfile.TemporaryDirectory() as tmp:
            crit = Path(tmp) / "criteria.json"
            crit.write_text(json.dumps({"search": CFG}))
            out = Path(tmp) / "out.json"
            with mock.patch.object(search.Fetcher, "_urllib_get", staticmethod(web.get)), \
                    mock.patch.object(search.time, "sleep", lambda s: None), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = search.main(["--criteria", str(crit), "--out", str(out)])
            self.assertEqual(code, 0)
            data = json.loads(out.read_text())
            self.assertEqual([p["id"] for p in data["postings"]], ["1"])
            self.assertEqual(data["window_seconds"], 86400)


if __name__ == "__main__":
    unittest.main()
