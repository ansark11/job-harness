"""The Easy Apply walk against a local stand-in page, in headless Chromium.

    ./python/bin/python -m unittest discover -s tests -t .
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apply  # noqa: E402
import easyapply  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PAGE = (Path(__file__).parent / "fixtures" / "easy_apply.html").resolve().as_uri()

PHONE = "mobile phone number"
SPONSOR = "will you now or in the future require visa sponsorship to work in the united states"
YEARS = "how many years of work experience do you have with payments"
GENDER = "gender"
WHY = "why are you interested in this role"
COVER = "upload cover letter"
FULL = {PHONE: "416-555-0100", SPONSOR: "No", YEARS: "8", GENDER: "I don't wish to answer",
        WHY: "I led payouts at a fintech."}


class EasyApplyTests(unittest.TestCase):
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
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()
        self.tmp.cleanup()

    def packet(self, answers=None, skip=(), url=PAGE, upload_name="Jane Doe - Resume.pdf"):
        return {"application_id": "a1", "job": {"id": "9", "url": url, "apply_type": "easy"},
                "resume_file": str(self.resume), "resume_upload_name": upload_name,
                "answers": answers or {}, "skip": list(skip)}

    def walk(self, packet, mode="fill", dry_run=False, cap_ok=lambda: True):
        return easyapply.Walk(self.page, packet, mode, dry_run, pause=None).run(cap_ok)

    def test_first_run_stops_at_first_unknown_keeping_prefills_and_discards(self):
        r = self.walk(self.packet())
        self.assertEqual(r["status"], "needs_input")
        self.assertEqual(r["unknown"], [PHONE])
        email = next(f for f in r["fields"] if f["key"] == "email address")
        self.assertEqual((email["outcome"], email["value"]), ("prefilled", "jane@example.com"))
        phone = next(f for f in r["fields"] if f["key"] == PHONE)
        self.assertTrue(phone["required"])
        self.assertIn("discarded", self.page.evaluate("window.events"))

    def test_resume_selected_by_name_and_cover_letter_reported(self):
        r = self.walk(self.packet({PHONE: FULL[PHONE]}))
        self.assertEqual(r["status"], "needs_input")
        by_key = {f["key"]: f for f in r["fields"]}
        self.assertEqual(by_key["choose a resume"]["outcome"], "filled")
        self.assertEqual(by_key["upload resume"]["outcome"], "not_needed")
        self.assertEqual(r["unknown"], [COVER])                    # optional: workflow will skip it

    def test_resume_uploaded_when_not_on_linkedin(self):
        r = self.walk(self.packet({PHONE: FULL[PHONE]}, skip=[COVER], upload_name="New Resume.pdf"))
        by_key = {f["key"]: f for f in r["fields"]}
        self.assertEqual(by_key["upload resume"]["outcome"], "filled")
        self.assertEqual(by_key["choose a resume"]["outcome"], "replaced_by_upload")

    def test_page_three_unknowns_with_types_and_options(self):
        r = self.walk(self.packet({PHONE: FULL[PHONE]}, skip=[COVER]))
        unknown = {f["key"]: f for f in r["fields"] if f.get("outcome") == "unknown"}
        self.assertEqual(set(unknown), {SPONSOR, YEARS, GENDER, WHY})
        self.assertEqual(unknown[SPONSOR]["type"], "radio")
        self.assertEqual(unknown[SPONSOR]["options"], ["Yes", "No"])
        self.assertTrue(unknown[SPONSOR]["required"])
        self.assertEqual(unknown[WHY]["type"], "textarea")
        self.assertEqual(unknown[WHY]["maxlength"], 1000)
        self.assertFalse(unknown[GENDER]["required"])
        self.assertEqual(unknown[GENDER]["options"][-1], "I don't wish to answer")

    def test_unmatchable_choice_is_unknown_with_error(self):
        r = self.walk(self.packet({**FULL, SPONSOR: "Maybe"}, skip=[COVER]))
        sponsor = next(f for f in r["fields"] if f["key"] == SPONSOR)
        self.assertEqual(sponsor["outcome"], "unknown")
        self.assertIn("matches none", sponsor["error"])

    def test_validation_error_after_next(self):
        r = self.walk(self.packet({PHONE: "call me"}))
        self.assertEqual(r["status"], "needs_input")
        self.assertEqual(r["errors"], ["Enter a valid phone number"])

    def test_fill_mode_and_dry_run_stop_at_review(self):
        self.assertEqual(self.walk(self.packet(FULL, skip=[COVER]))["status"], "ready")
        r = self.walk(self.packet(FULL, skip=[COVER]), mode="submit", dry_run=True)
        self.assertEqual(r["status"], "ready")
        self.assertNotIn("submitted", self.page.evaluate("window.events"))

    def test_submit_fills_everything_and_confirms(self):
        r = self.walk(self.packet(FULL, skip=[COVER]), mode="submit")
        self.assertEqual(r["status"], "submitted")
        sent = self.page.evaluate("window.submitted")
        self.assertEqual(sent, {"phone": "416-555-0100", "resume": "b", "upload": None, "sponsor": "No",
                                "years": "8", "gender": "I don't wish to answer", "why": "I led payouts at a fintech."})
        self.assertNotIn("discarded", self.page.evaluate("window.events"))

    def test_daily_cap_blocks_submit(self):
        r = self.walk(self.packet(FULL, skip=[COVER]), mode="submit", cap_ok=lambda: False)
        self.assertEqual(r["status"], "cap_reached")
        self.assertNotIn("submitted", self.page.evaluate("window.events"))

    def test_not_easy_apply_and_already_applied(self):
        self.assertEqual(self.walk(self.packet(url=PAGE + "?noeasy"))["status"], "not_easy_apply")
        self.assertEqual(self.walk(self.packet(url=PAGE + "?applied"))["status"], "already_applied")


class CliTests(unittest.TestCase):
    def test_cli_dry_run_with_a_fresh_profile(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(apply, "DATA", Path(tmp) / "data"):
            resume = Path(tmp) / "r.pdf"
            resume.write_bytes(b"%PDF-1.4 test")
            packet = Path(tmp) / "packet.json"
            packet.write_text(json.dumps({"application_id": "a1", "job": {"id": "9", "url": PAGE, "apply_type": "easy"},
                                          "resume_file": str(resume), "resume_upload_name": "Jane Doe - Resume.pdf",
                                          "answers": FULL, "skip": [COVER]}))
            out = io.StringIO()
            with redirect_stdout(out):
                code = apply.main([str(packet), "--mode", "submit", "--dry-run", "--headless",
                                   "--profile-dir", str(Path(tmp) / "profile")])
            report = json.loads(out.getvalue())
            self.assertEqual((code, report["status"], report["dry_run"]), (0, "ready", True))
            self.assertTrue(all(Path(s).exists() for s in report["screenshots"]))
            self.assertEqual(apply.submitted_today(Path(tmp) / "data" / "submissions.jsonl"), 0)


class SubmissionCountTests(unittest.TestCase):
    def test_counts_only_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in [
                {"at": "2020-01-01T12:00:00+00:00"}, {"at": apply.datetime.now(apply.timezone.utc).isoformat()},
                {"bad": 1}]) + "\nnot json\n")
            self.assertEqual(apply.submitted_today(path), 1)

    def test_record_and_upload_copy(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(apply, "DATA", Path(tmp)):
            src = Path(tmp) / "abc123.pdf"
            src.write_bytes(b"x")
            p = apply.upload_copy({"resume_file": str(src), "resume_upload_name": "Jane Doe - Resume.pdf",
                                   "job": {"id": "1"}})
            self.assertEqual(Path(p["resume_file"]).name, "Jane Doe - Resume.pdf")
            self.assertTrue(Path(p["resume_file"]).exists())


if __name__ == "__main__":
    unittest.main()
