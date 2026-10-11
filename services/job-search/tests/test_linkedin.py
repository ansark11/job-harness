"""Parsing LinkedIn guest pages, pinned to real responses saved in fixtures/.

    python3 -m unittest discover -s tests -t .
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import linkedin  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)


def fixture(name):
    return (FIX / name).read_text()


class SearchPageTests(unittest.TestCase):
    def setUp(self):
        self.cards = linkedin.parse_search(fixture("search_page.html"))

    def test_parses_all_ten_cards(self):
        self.assertEqual(len(self.cards), 10)
        self.assertEqual(len({c["id"] for c in self.cards}), 10)

    def test_card_fields(self):
        c = self.cards[0]
        self.assertEqual(c["id"], "4478345858")
        self.assertEqual(c["title"], "Senior Product Manager")
        self.assertEqual(c["company"], "Shoppers Drug Mart Specialty Health Network")
        self.assertEqual(c["location"], "Mississauga, Ontario, Canada")
        self.assertEqual(c["posted_date"], "2026-10-11")
        self.assertEqual(c["posted_ago"], "1 hour ago")
        self.assertTrue(c["url"].endswith("-4478345858"))
        self.assertNotIn("?", c["url"])                         # tracking params stripped
        self.assertTrue(c["company_logo"].startswith("https://media.licdn.com/"))

    def test_empty_page_is_empty_list(self):
        self.assertEqual(linkedin.parse_search(""), [])


class PostingTests(unittest.TestCase):
    def test_external_posting(self):
        p = linkedin.parse_posting(fixture("posting_external.html"))
        self.assertEqual(p["title"], "Senior Product Manager, Fund Management")
        self.assertEqual(p["company"], "Purpose")
        self.assertEqual(p["location"], "Toronto, Ontario, Canada")
        self.assertEqual(p["posted_ago"], "8 hours ago")
        self.assertEqual(p["applicants"], 102)
        self.assertEqual(p["apply_type"], "external")
        self.assertEqual(p["criteria"], {"seniority_level": "Not Applicable", "employment_type": "Full-time",
                                         "industries": "Financial Services"})
        self.assertIn("Purpose Unlimited", p["description"])
        self.assertIn("$125,000 - $145,000", p["description"])
        self.assertIn("\n", p["description"])                  # paragraphs kept for display

    def test_easy_apply_posting(self):
        p = linkedin.parse_posting(fixture("posting_easy.html"))
        self.assertEqual(p["apply_type"], "easy")
        self.assertTrue(p["title"])
        self.assertTrue(p["description"])

    def test_multi_location_pay_stays_in_description(self):
        p = linkedin.parse_posting(fixture("posting_multi_pay.html"))
        self.assertIn("Mountain View $205,500 - $278,000", p["description"])
        self.assertIn("Toronto, Ontario $162,000- $219,000", p["description"])
        self.assertNotIn("salary_text", p)                      # no structured pay on guest pages

    def test_description_lists_become_bullets(self):
        p = linkedin.parse_posting(fixture("posting_external.html"))
        self.assertIn("• We believe in a flexible work structure", p["description"])

    def test_unknown_apply_type(self):
        self.assertEqual(linkedin.parse_posting("<html></html>")["apply_type"], "unknown")


class MergeAndTimeTests(unittest.TestCase):
    def test_merge_prefers_detail_and_estimates_posted_at(self):
        card = {"id": "1", "title": "Sr PM", "posted_date": "2026-10-11", "posted_ago": "1 hour ago"}
        p = linkedin.merge(card, {"title": "Senior PM, Payments", "posted_ago": "3 hours ago", "criteria": {}}, NOW)
        self.assertEqual(p["title"], "Senior PM, Payments")
        self.assertEqual(p["posted_at"], (NOW - timedelta(hours=3)).isoformat(timespec="minutes"))
        self.assertEqual(p["apply_type"], "unknown")
        self.assertEqual(p["source"], "linkedin")

    def test_merge_falls_back_to_card_date(self):
        p = linkedin.merge({"id": "1", "posted_date": "2026-09-01"}, {"detail_error": "HTTP 429"}, NOW)
        self.assertTrue(p["posted_at"].startswith("2026-09-01"))
        self.assertEqual(p["detail_error"], "HTTP 429")
        self.assertGreater(linkedin.age_days(p, NOW), 30)

    def test_parse_ago_units(self):
        self.assertEqual(linkedin.parse_ago("2 weeks ago", NOW), NOW - timedelta(days=14))
        self.assertEqual(linkedin.parse_ago("1 minute ago", NOW), NOW - timedelta(minutes=1))
        self.assertIsNone(linkedin.parse_ago("Reposted", NOW))

    def test_window_clamped_between_24h_and_30d(self):
        self.assertEqual(linkedin.window_seconds(None, NOW), 86400)
        self.assertEqual(linkedin.window_seconds(NOW - timedelta(hours=3), NOW), 86400)
        self.assertEqual(linkedin.window_seconds(NOW - timedelta(days=3), NOW), 3 * 86400)
        self.assertEqual(linkedin.window_seconds(NOW - timedelta(days=90), NOW), 30 * 86400)

    def test_search_url(self):
        url = linkedin.search_url("Senior Product Manager", "Toronto, Ontario, Canada", 86400,
                                  ["remote", "hybrid"], ["full-time", "contract"], start=10)
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["keywords"], ["Senior Product Manager"])
        self.assertEqual(q["f_TPR"], ["r86400"])
        self.assertEqual(q["f_WT"], ["2,3"])
        self.assertEqual(q["f_JT"], ["F,C"])
        self.assertEqual(q["start"], ["10"])


if __name__ == "__main__":
    unittest.main()
