"""External portals against local stand-in pages, in headless Chromium.

    ./python/bin/python -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apply  # noqa: E402
import portals  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FIX = (Path(__file__).parent / "fixtures").resolve()
JOB = (FIX / "linkedin_offsite.html").as_uri()
CAREERS = (FIX / "careers_embed.html").as_uri()
GH = (FIX / "boards.greenhouse.io" / "apply.html").as_uri()
ASHBY = (FIX / "ashby_application.html").as_uri()

SPONSOR = "will you now or in the future require visa sponsorship to work in the united states"
WHY = "why do you want to work at portal co"
GH_FULL = {"first name": "Jane", "last name": "Doe", "email": "jane@example.com", "location city": "Toronto, ON",
           SPONSOR: "No", WHY: "I led payouts.", "gender": "Decline To Self Identify",
           "linkedin profile": "https://linkedin.com/in/jane"}


class UrlTests(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(portals.detect("https://boards.greenhouse.io/stripe/jobs/123"), "greenhouse")
        self.assertEqual(portals.detect("https://job-boards.greenhouse.io/x/jobs/1"), "greenhouse")
        self.assertEqual(portals.detect("https://jobs.lever.co/plaid/abc"), "lever")
        self.assertEqual(portals.detect("https://jobs.ashbyhq.com/ramp/abc"), "ashby")
        self.assertEqual(portals.detect("https://wd5.myworkdayjobs.com/en-US/x/job/1"), "workday")
        self.assertEqual(portals.detect("https://careers.example.com/jobs/1"), "generic")
        self.assertEqual(portals.detect("https://notgreenhouse.io.evil.com/x"), "generic")

    def test_form_url(self):
        self.assertEqual(portals.form_url("https://jobs.lever.co/plaid/abc?lever-source=LinkedIn"),
                         "https://jobs.lever.co/plaid/abc/apply?lever-source=LinkedIn")
        self.assertEqual(portals.form_url("https://jobs.lever.co/plaid/abc/apply"), "https://jobs.lever.co/plaid/abc/apply")
        self.assertEqual(portals.form_url("https://jobs.ashbyhq.com/ramp/abc/"), "https://jobs.ashbyhq.com/ramp/abc/application")
        self.assertEqual(portals.form_url("https://boards.greenhouse.io/s/jobs/1"), "https://boards.greenhouse.io/s/jobs/1")


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.resume = Path(self.tmp.name) / "Jane Doe - Resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 test")
        self.ctx = self.browser.new_context()
        # The embedded form, served locally at a real Greenhouse address; nothing reaches the network.
        gh = (FIX / "boards.greenhouse.io" / "apply.html").read_text()
        self.ctx.route("https://boards.greenhouse.io/**", lambda r: r.fulfill(content_type="text/html", body=gh))
        self.ctx.route("https://content.googleapis.com/**", lambda r: r.fulfill(content_type="text/html", body=""))
        self.page = self.ctx.new_page()

    def tearDown(self):
        self.ctx.close()
        self.tmp.cleanup()

    def packet(self, answers=None, apply_url=None, skip=("cover letter",)):
        return {"application_id": "a1", "job": {"id": "9", "url": JOB, "apply_type": "external", "apply_url": apply_url},
                "resume_file": str(self.resume), "answers": answers or {}, "skip": list(skip)}

    def walk(self, packet, mode="fill", dry_run=False, success_wait=3):
        return portals.PortalWalk(self.page, packet, mode, dry_run, pause=None, success_wait=success_wait).run()

    def test_resolves_apply_url_from_linkedin_popup(self):
        self.assertEqual(portals.resolve_apply_url(self.page, JOB), CAREERS)

    def test_follows_embedded_form_and_reports_unknowns(self):
        r = self.walk(self.packet())                       # apply_url resolved from the LinkedIn page
        self.assertEqual(r["apply_url"], CAREERS)
        self.assertEqual(r["status"], "needs_input")
        by_key = {f["key"]: f for f in r["fields"]}
        self.assertEqual(by_key["resume cv"]["outcome"], "filled")
        self.assertEqual(by_key["cover letter"]["outcome"], "skipped")
        self.assertTrue(by_key["first name"]["required"])
        self.assertFalse(by_key["phone"]["required"])
        self.assertEqual(by_key[SPONSOR]["options"], ["Yes", "No"])
        self.assertEqual(by_key[WHY]["type"], "textarea")
        self.assertIn("location city", r["unknown"])

    def test_fill_mode_stops_for_approval(self):
        r = self.walk(self.packet(GH_FULL, apply_url=GH, skip=("cover letter", "phone")))
        self.assertEqual((r["status"], r["note"]), ("ready", "filled; waiting for your approval"))
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_submit_with_combobox_choice_and_confirmation(self):
        r = self.walk(self.packet(GH_FULL, apply_url=GH, skip=("cover letter", "phone")), mode="submit")
        self.assertEqual(r["status"], "submitted")
        sent = self.page.evaluate("window.submitted")
        self.assertEqual(sent["city"], "Toronto, ON, Canada")       # picked the matching suggestion
        self.assertEqual((sent["sponsor"], sent["gender"], sent["resume"]),
                         ("No", "Decline To Self Identify", "Jane Doe - Resume.pdf"))

    def test_portal_rejects_a_value(self):
        r = self.walk(self.packet({**GH_FULL, "email": "not-an-email"}, apply_url=GH, skip=("cover letter", "phone")),
                      mode="submit")
        self.assertEqual((r["status"], r["errors"]), ("needs_input", ["Please enter a valid email address."]))

    def test_captcha_needs_you(self):
        r = self.walk(self.packet(GH_FULL, apply_url=GH + "?captcha", skip=("cover letter", "phone")), mode="submit")
        self.assertEqual(r["status"], "needs_user")
        self.assertIn("CAPTCHA", r["note"])

    def test_ashby_yes_no_buttons(self):
        answers = {"name": "Jane Doe", "email": "jane@example.com",
                   "are you legally authorized to work in the united states": "Yes"}
        r = self.walk(self.packet({}, apply_url=ASHBY))
        auth = next(f for f in r["fields"] if f["type"] == "button_choice")
        self.assertEqual((auth["options"], auth["required"]), (["Yes", "No"], True))
        r = self.walk(self.packet(answers, apply_url=ASHBY), mode="submit")
        self.assertEqual(r["status"], "submitted")
        self.assertEqual(self.page.evaluate("window.submitted"), {"name": "Jane Doe", "auth": "Yes", "cv": 1})

    def test_manual_portals_are_left_to_you(self):
        r = self.walk(self.packet(apply_url="https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123"))
        self.assertEqual((r["status"], r["ats"]), ("needs_user", "workday"))
        self.assertIn("apply by hand", r["note"])

    def test_dry_run_never_submits(self):
        r = self.walk(self.packet(GH_FULL, apply_url=GH, skip=("cover letter", "phone")), mode="submit", dry_run=True)
        self.assertEqual(r["status"], "ready")
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_run_packet_routes_external_to_portal(self):
        r = apply.run_packet(self.page, self.packet(GH_FULL, apply_url=GH, skip=("cover letter", "phone")),
                             "fill", False, None, cap=10, pause=None)
        self.assertEqual((r["channel"], r["status"]), ("portal", "ready"))


if __name__ == "__main__":
    unittest.main()


class PopupTests(unittest.TestCase):
    """LinkedIn's leaving-LinkedIn prompts and the overlays real portals put over their forms."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.ctx = self.browser.new_context()
        self.page = self.ctx.new_page()

    def tearDown(self):
        self.ctx.close()

    def test_continue_prompt_is_clicked_through(self):
        url = (FIX / "linkedin_interstitial.html").as_uri()
        self.assertEqual(portals.resolve_apply_url(self.page, url), CAREERS)

    def test_never_returns_the_linkedin_page_itself(self):
        url = (FIX / "linkedin_stuck.html").as_uri()
        self.assertIsNone(portals.resolve_apply_url(self.page, url, timeout=2500))

    def test_left_linkedin(self):
        self.assertFalse(portals._left_linkedin("https://www.linkedin.com/jobs/view/1/", "https://www.linkedin.com/jobs/view/1"))
        self.assertFalse(portals._left_linkedin("about:blank", "x"))
        self.assertTrue(portals._left_linkedin("https://jobs.lever.co/x/1", "https://www.linkedin.com/jobs/view/1"))

    def test_cookie_banner_is_accepted_and_form_fills(self):
        self.page.goto((FIX / "cookie_banner.html").as_uri())
        self.assertEqual(portals.dismiss_overlays(self.page), 1)
        self.assertEqual(self.page.locator("#onetrust-banner-sdk").count(), 0)
        self.page.locator("#fn").fill("Jane", timeout=2000)      # clickable now

    def test_form_in_a_modal_is_left_alone(self):
        self.page.goto((FIX / "modal_form.html").as_uri())
        self.assertEqual(portals.dismiss_overlays(self.page), 0)
        self.assertTrue(self.page.locator("#m").is_visible())


class EngineGapTests(unittest.TestCase):
    """The gaps the first live benchmark found on Engine's Greenhouse form and Mastercard's careers page."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.resume = Path(self.tmp.name) / "Jane Doe - Resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 test")
        self.ctx = self.browser.new_context()
        self.page = self.ctx.new_page()

    def tearDown(self):
        self.ctx.close()
        self.tmp.cleanup()

    GH_NEW = (FIX / "greenhouse_new.html").as_uri()

    def test_searchable_dropdown_is_read_with_its_options(self):
        import forms
        self.page.goto(self.GH_NEW)
        by = {f["key"]: f for f in forms.extract_fields(self.page, probe=True)}
        self.assertEqual(by["country"]["type"], "combo_select")
        self.assertEqual(by["country"]["options"], ["Canada", "United States", "United Kingdom"])
        self.assertEqual(by["location city"]["type"], "text")           # a typeahead stays text
        self.assertTrue(by["location city"]["combobox"])
        self.assertEqual(by["resume cv"]["type"], "file")               # named by its role=group

    def test_wrong_dropdown_or_typeahead_value_is_an_error_not_a_guess(self):
        import forms
        self.page.goto(self.GH_NEW)
        by = {f["key"]: f for f in forms.extract_fields(self.page, probe=True)}
        with self.assertRaises(forms.FillError):
            forms.fill_field(self.page, by["country"], "Atlantis")
        with self.assertRaises(forms.FillError):
            forms.fill_field(self.page, by["location city"], "Torino")   # suggestions appear, none match

    def test_fills_through_a_form_that_resets_early_typing(self):
        answers = {"first name": "Jane", "last name": "Doe", "email": "jane@example.com", "country": "Canada",
                   "location city": "Toronto"}
        packet = {"application_id": "a1", "job": {"id": "9", "url": self.GH_NEW, "apply_type": "external",
                                                  "apply_url": self.GH_NEW},
                  "resume_file": str(self.resume), "answers": answers, "skip": []}
        r = portals.PortalWalk(self.page, packet, "submit", False, pause=None, success_wait=3).run()
        self.assertEqual(r["status"], "submitted", r.get("unknown"))
        self.assertEqual(self.page.evaluate("window.submitted"),
                         {"first": "Jane", "last": "Doe", "email": "jane@example.com", "country": "Canada",
                          "city": "Toronto, ON, Canada", "resume": "Jane Doe - Resume.pdf"})

    def test_job_page_inputs_are_not_an_application_and_apply_leads_to_workday(self):
        self.ctx.route("https://acme.wd5.myworkdayjobs.com/**", lambda route: route.fulfill(
            content_type="text/html", body="<h1>Workday</h1><button>Sign In</button>"))
        page_url = (FIX / "careers_jobpage.html").as_uri()
        packet = {"application_id": "a1", "job": {"id": "9", "url": page_url, "apply_type": "external",
                                                  "apply_url": page_url},
                  "resume_file": str(self.resume), "answers": {}, "skip": []}
        r = portals.PortalWalk(self.page, packet, "fill", False, pause=None).run()
        self.assertEqual((r["status"], r["ats"]), ("needs_user", "workday"))
        self.assertTrue(r["apply_url"].startswith("https://acme.wd5.myworkdayjobs.com/"))

    def test_looks_like_application(self):
        f = lambda t, label: {"type": t, "label": label, "key": label.lower()}      # noqa: E731
        self.assertFalse(portals.looks_like_application([f("text", "Search jobs"), f("email", "Email")]))
        self.assertTrue(portals.looks_like_application([f("text", "First Name"), f("text", "Email")]))
        self.assertTrue(portals.looks_like_application([f("file", "Resume/CV")]))
