"""Applications and their items, on a temp database.

    ./python/bin/python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import applications  # noqa: E402
import bank  # noqa: E402
import resumes  # noqa: E402
import store  # noqa: E402
from tests.test_profile import RESUME_LINES, TempDb, make_docx  # noqa: E402
from tests.test_store import NOW, m  # noqa: E402


class ApplicationTests(TempDb):
    def setUp(self):
        super().setUp()
        make_docx(self.dir / "Jane Doe - Resume.docx", RESUME_LINES)
        self.resume = resumes.add(self.conn, self.dir / "Jane Doe - Resume.docx")
        store.ingest(self.conn, [m("1", description="Own payouts."), m("2")], NOW)
        store.set_status(self.conn, "1", "kept", NOW)

    def test_create_requires_kept_and_is_one_per_opportunity(self):
        with self.assertRaisesRegex(store.TrackerError, "keep it before applying"):
            applications.create(self.conn, "2")
        a = applications.create(self.conn, "1", now=NOW)
        self.assertEqual((a["status"], a["resume"]["id"], a["job"]["apply_type"]), ("queued", self.resume["id"], "easy"))
        self.assertEqual(a["job"]["description"], "Own payouts.")
        again = applications.create(self.conn, "1")
        self.assertEqual((again["id"], again["existing"]), (a["id"], True))
        applications.set_status(self.conn, a["id"], "failed", "boom")
        self.assertNotEqual(applications.create(self.conn, "1")["id"], a["id"])   # retry after failure

    def test_text_resume_cant_be_uploaded(self):
        (self.dir / "plain.txt").write_text("\n".join(RESUME_LINES))
        txt = resumes.add(self.conn, self.dir / "plain.txt")
        with self.assertRaisesRegex(store.TrackerError, "isn't a PDF or DOCX"):
            applications.create(self.conn, "1", txt["id"])

    def test_resume_used_by_an_application_cant_be_removed(self):
        applications.create(self.conn, "1")
        with self.assertRaisesRegex(store.TrackerError, "used by 1 application"):
            resumes.remove(self.conn, self.resume["id"])

    def test_items_packet_and_blocking(self):
        a = applications.create(self.conn, "1")
        applications.upsert_items(self.conn, a["id"], [
            {"key": "mobile phone number", "label": "Mobile phone number", "state": "filled", "value": "416",
             "source": "bank", "bank_key": "phone", "required": True},
            {"key": "why us", "label": "Why us?", "kind": "long_form", "state": "needs_review", "value": "Draft",
             "source": "draft", "meta": {"resume_points": ["x"]}, "required": True},
            {"key": "gender", "label": "Gender", "state": "skipped"},
            {"key": "visa", "label": "Visa?", "state": "needs_answer", "required": True, "inbox_id": 1},
        ])
        p = applications.packet(self.conn, a["id"])
        self.assertEqual(p["answers"], {"mobile phone number": "416"})
        self.assertEqual(p["skip"], ["gender"])
        self.assertEqual(p["blocking"], ["why us", "visa"])
        self.assertEqual(p["resume_upload_name"], "Jane Doe - Resume.docx")
        self.assertEqual(p["job"]["id"], "1")

    def test_approve_by_number_adds_long_form_to_library_and_survives_upserts(self):
        a = applications.create(self.conn, "1")
        applications.upsert_items(self.conn, a["id"], [
            {"key": "phone", "label": "Phone", "state": "filled", "value": "416"},
            {"key": "why us", "label": "Why us?", "kind": "long_form", "state": "needs_review", "value": "Draft"}])
        item = applications.approve(self.conn, a["id"], "2")
        self.assertEqual(item["state"], "approved")
        self.assertEqual(bank.approved_list(self.conn)[0]["question"], "Why us?")
        # A later agent pass reporting the field again must not undo the approval.
        applications.upsert_items(self.conn, a["id"], [{"key": "why us", "label": "Why us?", "kind": "long_form",
                                                       "state": "needs_review", "value": "Other draft"}])
        self.assertEqual(applications.packet(self.conn, a["id"])["answers"]["why us"], "Draft")

    def test_edit_approves_users_value(self):
        a = applications.create(self.conn, "1")
        applications.upsert_items(self.conn, a["id"], [{"key": "visa", "label": "Visa?", "state": "needs_answer"}])
        with self.assertRaisesRegex(store.TrackerError, "no value to approve"):
            applications.approve(self.conn, a["id"], "visa")
        item = applications.edit(self.conn, a["id"], "visa", "No")
        self.assertEqual((item["state"], item["source"], item["value"]), ("approved", "user", "No"))
        self.assertEqual(bank.approved_list(self.conn), [])          # profile answers aren't long-form library

    def test_apply_url_is_kept_and_reaches_the_packet(self):
        a = applications.create(self.conn, "1")
        self.assertIsNone(applications.packet(self.conn, a["id"])["job"]["apply_url"])
        applications.set_status(self.conn, a["id"], "ready", apply_url="https://jobs.lever.co/x/1")
        applications.set_status(self.conn, a["id"], "in_progress")          # later updates don't erase it
        self.assertEqual(applications.packet(self.conn, a["id"])["job"]["apply_url"], "https://jobs.lever.co/x/1")

    def test_status_and_submitted_at(self):
        a = applications.create(self.conn, "1")
        out = applications.set_status(self.conn, a["id"], "submitted", report={"status": "submitted"})
        self.assertIsNotNone(out["submitted_at"])
        self.assertEqual(out["last_report"], {"status": "submitted"})
        self.assertEqual(applications.list_all(self.conn, "submitted")[0]["company"], "Acme")
        with self.assertRaises(store.TrackerError):
            applications.set_status(self.conn, a["id"], "done")


if __name__ == "__main__":
    unittest.main()
