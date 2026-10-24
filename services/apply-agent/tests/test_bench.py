"""The portal benchmark against local stand-in pages, in headless Chromium. No network.

    ./python/bin/python -m unittest discover -s tests -t .
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bench  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FIX = (Path(__file__).parent / "fixtures").resolve()
GH = (FIX / "boards.greenhouse.io" / "apply.html").as_uri()
ASHBY = (FIX / "ashby_application.html").as_uri()
TRICKY = (FIX / "bench_tricky.html").as_uri()


class Run:
    """Stand-in for runlog's Run."""
    def __init__(self):
        self.events = []

    def event(self, name, **data):
        self.events.append((name, data))

    warn = event


class ClassifyTests(unittest.TestCase):
    def test_host(self):
        self.assertEqual(bench.ats_from_host("https://boards.greenhouse.io/s/jobs/1"), "greenhouse")
        self.assertEqual(bench.ats_from_host("https://td.wd3.myworkdayjobs.com/en-US/TD/job/1"), "workday")
        self.assertEqual(bench.ats_from_host("https://career4.successfactors.com/x"), "successfactors")
        self.assertEqual(bench.ats_from_host("https://eeho.fa.us2.oraclecloud.com/hcmUI/x"), "oracle")
        self.assertIsNone(bench.ats_from_host("https://careers.example.com/jobs/1"))

    def test_markup(self):
        self.assertEqual(bench.classify("https://careers.example.com/x",
                                        '<iframe src="https://boards.greenhouse.io/embed/job_app?for=x">'),
                         "greenhouse (embedded)")
        self.assertEqual(bench.classify("https://careers.example.com/x", "<div>plain</div>"), "generic")

    def test_persona_never_blank(self):
        self.assertEqual(bench.persona_value({"type": "text", "label": "First Name"}), "Bench")
        self.assertEqual(bench.persona_value({"type": "email", "label": "Email"}), "bench.tester@example.com")
        self.assertEqual(bench.persona_value({"type": "select", "label": "x", "options": ["A", "B"]}), "A")
        self.assertIsNone(bench.persona_value({"type": "radio", "label": "x", "options": []}))
        self.assertEqual(bench.persona_value({"type": "textarea", "label": "Why us?"}), bench.LONG_TEXT)


class ScoreTests(unittest.TestCase):
    def base(self, **kw):
        return {"opened": "form", "fields": [], "missed": [], "submit_button": True, **kw}

    def test_clean_page_passes(self):
        f = {"outcome": "verified", "required": True, "appeared_after_fill": False}
        self.assertEqual(bench.score(self.base(fields=[f]))["verdict"], "pass")

    def test_multi_page_fails(self):
        s = bench.score(self.base(submit_button=False, next_button=True))
        self.assertEqual(s["verdict"], "fail")
        self.assertIn("multi-page", " ".join(s["reasons"]))

    def test_optional_miss_is_risky(self):
        s = bench.score(self.base(missed=[{"required": False, "label": "x"}]))
        self.assertEqual(s["verdict"], "risky")

    def test_manual_and_unreachable(self):
        self.assertEqual(bench.score({"opened": "needs_user"})["verdict"], "manual")
        self.assertEqual(bench.score({"opened": "error", "open_error": "x"})["verdict"], "unreachable")


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def bench_one(self, url, **target):
        out = self.tmp / "run"
        results = bench.run_targets([{"id": "1", "company": "Co", "title": "PM", "apply_url": url, **target}],
                                    out, True, Run(), browser=self.browser)
        return results[0]

    def test_greenhouse_style_form(self):
        r = self.bench_one(GH)
        self.assertEqual(r["opened"], "form")
        self.assertTrue(r["submit_button"])
        by = {f["label"]: f for f in r["fields"]}
        self.assertEqual(by["First Name"]["outcome"], "verified")
        self.assertEqual(by["Resume/CV"]["outcome"], "verified")
        self.assertEqual(by["Cover Letter"]["outcome"], "left_blank")
        self.assertEqual(r["missed"], [])
        for name in ("page.html", "filled.png", "trace.zip"):
            self.assertTrue((Path(r["dir"]) / name).exists(), name)

    def test_ashby_style_form(self):
        r = self.bench_one(ASHBY)
        self.assertEqual(r["opened"], "form")
        self.assertGreater(r["counts"]["verified"], 0)

    def test_tricky_controls_are_reported_not_hidden(self):
        r = self.bench_one(TRICKY)
        self.assertEqual(r["verdict"], "fail")
        missed = {m["label"]: m for m in r["missed"]}
        self.assertTrue(missed["Department *"]["required"])        # div dropdown the engine can't see
        self.assertTrue(missed["Pronouns"]["in_shadow"])           # inside shadow DOM
        self.assertNotIn("Search jobs", missed)                    # site chrome outside the form ignored
        by = {f["label"]: f for f in r["fields"]}
        # Keystroke-only input: cleared after the first fill, kept after a keystroke retype.
        self.assertEqual(by["Preferred name"]["outcome"], "verified")
        self.assertGreaterEqual(r["retyped"], 1)
        self.assertTrue(by["When did you work here?"]["appeared_after_fill"])
        self.assertEqual(by["When did you work here?"]["outcome"], "verified")
        self.assertTrue(any("save-draft" in b["url"] for b in r["blocked_requests"]))
        self.assertIn("follow-up", " ".join(r["reasons"]))

    def test_cookie_banner_does_not_block_the_check(self):
        r = self.bench_one((FIX / "cookie_banner.html").as_uri())
        self.assertEqual(r["verdict"], "pass", r["reasons"])

    def test_account_portal_is_manual_without_loading(self):
        r = self.bench_one("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123")
        self.assertEqual(r["verdict"], "manual")
        self.assertEqual(r["ats"], "workday")

    def test_unresolved_target(self):
        out = self.tmp / "run2"
        r = bench.run_targets([{"id": "9", "company": "Co", "title": "PM"}], out, True, Run(), browser=self.browser)
        self.assertEqual(r[0]["verdict"], "unresolved")

    def test_summary_and_report(self):
        results = [self.bench_one(GH), self.bench_one(TRICKY),
                   self.bench_one("https://acme.wd5.myworkdayjobs.com/x")]
        s = bench.summarize(results)
        self.assertEqual(s["postings"], 3)
        ats = {row["ats"]: row for row in s["by_ats"]}
        self.assertEqual(ats["workday"]["manual"], 1)
        md = bench.report_md(results, s)
        self.assertIn("| workday | 1 |", md)
        self.assertIn("not found: Department", md)


class ResolveTests(unittest.TestCase):
    def test_linkedin_urls_in_the_cache_are_dropped(self):
        tmp = Path(tempfile.mkdtemp())
        cache = tmp / "resolved.json"
        cache.write_text(json.dumps({"1": "https://www.linkedin.com/jobs/view/x-1/", "2": "https://jobs.lever.co/x/2"}))
        self.assertEqual(bench.load_cache(cache), {"2": "https://jobs.lever.co/x/2"})

    def test_cached_urls_skip_linkedin(self):
        tmp = Path(tempfile.mkdtemp())
        cache = tmp / "resolved.json"
        cache.write_text(json.dumps({"1": "https://jobs.lever.co/x/1"}))
        targets = [{"id": "1", "url": "https://www.linkedin.com/jobs/view/1"}]
        with mock.patch("playwright.sync_api.sync_playwright", side_effect=AssertionError("no browser")):
            out = bench.resolve_all(targets, tmp / "profile", True, Run(), cache_path=cache)
        self.assertEqual(out[0]["apply_url"], "https://jobs.lever.co/x/1")


if __name__ == "__main__":
    unittest.main()


class MeasurementTests(unittest.TestCase):
    """False negatives the first live benchmark reported on Engine's form."""

    def test_reformatted_phone_counts_as_kept(self):
        f = {"type": "tel", "label": "Phone"}
        self.assertEqual(bench.verify(f, "4165550123", {"value": "(416) 555-0123"}, {"value": "(416) 555-0123"}),
                         (True, None))
        self.assertFalse(bench.verify(f, "4165550123", {"value": "(416) 555-9999"}, {})[0])

    def test_dropdown_showing_a_shortened_choice_counts_as_picked(self):
        f = {"type": "combo_select", "label": "Country", "combobox": True}
        raw = {"value": "", "around": "Country* +1", "shown": "+1"}
        self.assertEqual(bench.verify(f, "Canada +1", {"value": None}, raw), (True, None))
        raw_wrong = {"value": "", "around": "Country* +44", "shown": "+44"}
        self.assertFalse(bench.verify(f, "Canada +1", {"value": None}, raw_wrong)[0])

    def test_preferred_name_isnt_answered_as_a_referral_source(self):
        self.assertEqual(bench.persona_value({"type": "text", "label": "Preferred First Name"}), "Bench")
        self.assertEqual(bench.persona_value({"type": "text", "label": "Referral source"}), "LinkedIn")
