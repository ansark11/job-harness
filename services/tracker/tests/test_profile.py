"""Resumes, the answer bank, the inbox and approved answers, on a temp database.

    ./python/bin/python -m unittest discover -s tests -t .
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bank  # noqa: E402
import db  # noqa: E402
import resumes  # noqa: E402
import store  # noqa: E402
import tracker  # noqa: E402

RESUME_LINES = ["Jane Doe - Senior Product Manager",
                "Led card-issuing and payments products at a fintech, growing volume 4x.",
                "Shipped checkout optimization for a commerce platform used by 40,000 merchants.",
                "Managed a team of 5 PMs across risk, onboarding and payouts."]


def make_docx(path, lines):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')


def make_pdf(path, lines):
    """A minimal valid one-page PDF with Helvetica text."""
    content = "BT /F1 10 Tf 50 750 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    Path(path).write_bytes(out.encode("latin-1"))


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"TRACKER_DB": str(self.dir / "data" / "jobs.db")})
        self.env.start()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()


class ResumeTests(TempDb):
    def test_add_docx_pdf_txt_and_default(self):
        make_docx(self.dir / "a.docx", RESUME_LINES)
        make_pdf(self.dir / "b.pdf", RESUME_LINES)
        (self.dir / "c.txt").write_text("\n".join(RESUME_LINES))
        a = resumes.add(self.conn, self.dir / "a.docx", "Product Lead")
        b = resumes.add(self.conn, self.dir / "b.pdf")
        resumes.add(self.conn, self.dir / "c.txt")
        self.assertTrue(a["is_default"])                                # first one is the default
        self.assertFalse(b["is_default"])
        self.assertIn("card-issuing", Path(a["text_path"]).read_text())
        self.assertIn("card-issuing", Path(b["text_path"]).read_text())
        self.assertTrue(Path(b["file_path"]).exists())
        self.assertEqual(Path(a["file_path"]).parent, self.dir / "data" / "resumes")
        resumes.set_default(self.conn, b["id"])
        self.assertEqual(resumes.default(self.conn)["id"], b["id"])
        self.assertEqual(len(resumes.list_all(self.conn)), 3)

    def test_pdf_lines_stay_whole_and_refresh_rewrites_text(self):
        make_pdf(self.dir / "b.pdf", RESUME_LINES)
        rec = resumes.add(self.conn, self.dir / "b.pdf")
        text = Path(rec["text_path"]).read_text()
        self.assertIn("Led card-issuing and payments products at a fintech, growing volume 4x.", text)
        Path(rec["text_path"]).write_text("stale")
        resumes.refresh(self.conn, rec["id"])
        self.assertIn("card-issuing", Path(rec["text_path"]).read_text())

    def test_upload_pdf_but_read_text_from_docx(self):
        make_pdf(self.dir / "r.pdf", ["Scanned-looking PDF text " * 10])
        make_docx(self.dir / "r.docx", RESUME_LINES)
        rec = resumes.add(self.conn, self.dir / "r.pdf", "Jane Doe - Resume", text_from=self.dir / "r.docx")
        self.assertTrue(rec["file_path"].endswith(".pdf"))
        self.assertTrue(rec["text_source"].endswith("-text.docx"))
        self.assertIn("card-issuing", Path(rec["text_path"]).read_text())
        Path(rec["text_path"]).write_text("stale")
        resumes.refresh(self.conn, rec["id"])                    # refresh reads the text source
        self.assertIn("card-issuing", Path(rec["text_path"]).read_text())

    def test_set_text_source_and_remove(self):
        make_pdf(self.dir / "r.pdf", RESUME_LINES)
        make_docx(self.dir / "r.docx", RESUME_LINES + ["Docx-only line"])
        rec = resumes.add(self.conn, self.dir / "r.pdf")
        out = resumes.set_text_source(self.conn, rec["id"], self.dir / "r.docx")
        self.assertIn("Docx-only line", Path(out["text_path"]).read_text())
        gone = resumes.remove(self.conn, rec["id"])
        self.assertEqual(gone["removed"], rec["id"])
        self.assertFalse(Path(rec["file_path"]).exists())
        with self.assertRaises(store.TrackerError):
            resumes.get(self.conn, rec["id"])

    def test_linkedin_name(self):
        make_docx(self.dir / "r.docx", RESUME_LINES)
        rec = resumes.add(self.conn, self.dir / "r.docx")
        self.assertIsNone(rec["linkedin_name"])
        self.assertEqual(resumes.set_linkedin_name(self.conn, rec["id"], "Jane Doe - Resume.pdf")["linkedin_name"],
                         "Jane Doe - Resume.pdf")

    def test_resume_text_is_stored_without_contact_details(self):
        bank.set_answer(self.conn, "first_name", "Jane")
        bank.set_answer(self.conn, "last_name", "Doe")
        bank.set_answer(self.conn, "phone", "647-779-7348")
        lines = ["JANE DOE", "Toronto, ON | jane.doe@example.com | (647) 779 7348 | linkedin.com/in/janedoe"] + RESUME_LINES
        make_docx(self.dir / "r.docx", lines)
        text = Path(resumes.add(self.conn, self.dir / "r.docx")["text_path"]).read_text()
        self.assertTrue(text.startswith("[name]\nToronto, ON | [email] | [phone] | [linkedin]"))
        for leak in ("Jane", "Doe", "jane.doe", "779", "janedoe"):
            self.assertNotIn(leak.lower(), text.lower())
        self.assertIn("card-issuing", text)

    def test_same_file_twice_returns_existing(self):
        make_docx(self.dir / "a.docx", RESUME_LINES)
        first = resumes.add(self.conn, self.dir / "a.docx")
        again = resumes.add(self.conn, self.dir / "a.docx")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["already_added"])

    def test_rejects_unsupported_and_textless(self):
        (self.dir / "x.rtf").write_text("hi")
        with self.assertRaises(store.TrackerError):
            resumes.add(self.conn, self.dir / "x.rtf")
        (self.dir / "short.txt").write_text("too short")
        with self.assertRaisesRegex(store.TrackerError, "scanned image"):
            resumes.add(self.conn, self.dir / "short.txt")

    def test_no_default_is_an_error(self):
        with self.assertRaisesRegex(store.TrackerError, "no default resume"):
            resumes.default(self.conn)


class BankTests(TempDb):
    def test_seeded_and_resolve_exact_ignores_case_punctuation_and_required_marker(self):
        bank.set_answer(self.conn, "sponsorship_us", "No")
        out = bank.resolve(self.conn, "WILL you now or in the future require visa sponsorship to work in the "
                                      "United States? *")
        self.assertEqual((out["status"], out["answer"], out["via"]), ("answered", "No", "exact"))

    def test_unknown_unanswered_and_always_ask(self):
        self.assertEqual(bank.resolve(self.conn, "Favourite colour?")["status"], "unknown")
        self.assertEqual(bank.resolve(self.conn, "Veteran status")["status"], "unanswered")
        bank.set_answer(self.conn, "salary_expectation_usd", "Open", always_ask=True)
        self.assertEqual(bank.resolve(self.conn, "What are your salary expectations (USD)?")["status"], "always_ask")

    def test_alternative_wordings(self):
        e = bank.set_answer(self.conn, "gender_ca", "Man", alternatives=["Male", "Cisgender male", "Man"])
        self.assertEqual(e["alternatives"], ["Male", "Cisgender male"])
        self.assertEqual(bank.resolve(self.conn, "Gender identity")["alternatives"], ["Male", "Cisgender male"])
        bank.set_answer(self.conn, "gender_ca", "Man")                        # None leaves them alone
        self.assertEqual(bank.get(self.conn, "gender_ca")["alternatives"], ["Male", "Cisgender male"])

    def test_keys_never_include_answers(self):
        bank.set_answer(self.conn, "race_us", "Asian")
        dumped = json.dumps(bank.keys(self.conn))
        self.assertNotIn("Asian", dumped)
        self.assertIn("race_us", dumped)

    def test_inbox_resolve_creates_alias_reused_next_time(self):
        bank.set_answer(self.conn, "sponsorship_us", "No")
        q = "Do you require H-1B sponsorship now or later?"
        item = bank.inbox_add(self.conn, q, "select", ["Yes", "No"], "US", "app1")
        dup = bank.inbox_add(self.conn, q.upper(), "select")
        self.assertTrue(dup["duplicate"])
        bank.inbox_propose(self.conn, item["id"], "sponsorship_us", 0.9, "same meaning")
        bank.inbox_resolve(self.conn, item["id"], "sponsorship_us")
        out = bank.resolve(self.conn, "Do you require H-1B sponsorship now or later")
        self.assertEqual((out["status"], out["via"], out["answer"]), ("answered", "alias", "No"))
        self.assertEqual(bank.inbox_list(self.conn), [])

    def test_inbox_resolve_needs_answer_when_key_unanswered(self):
        item = bank.inbox_add(self.conn, "Are you a US veteran?")
        with self.assertRaisesRegex(store.TrackerError, "pass --answer"):
            bank.inbox_resolve(self.conn, item["id"], "veteran_us")
        bank.inbox_resolve(self.conn, item["id"], "veteran_us", answer="I am not a protected veteran")
        self.assertEqual(bank.resolve(self.conn, "Are you a US veteran?")["answer"], "I am not a protected veteran")

    def test_inbox_answer_creates_custom_key(self):
        item = bank.inbox_add(self.conn, "Do you have a valid driver's license?", "radio", ["Yes", "No"])
        out = bank.inbox_answer(self.conn, item["id"], "Yes")
        self.assertEqual(out["entry"]["key"], "do_you_have_a_valid_driver")
        self.assertTrue(out["entry"]["custom"])
        self.assertEqual(bank.resolve(self.conn, "Do you have a valid driver's license?")["answer"], "Yes")
        with self.assertRaisesRegex(store.TrackerError, "already answered"):
            bank.inbox_answer(self.conn, item["id"], "No")

    def test_propose_refuses_unknown_key(self):
        item = bank.inbox_add(self.conn, "Something new?")
        with self.assertRaises(store.TrackerError):
            bank.inbox_propose(self.conn, item["id"], "made_up_key", 0.99)
        self.assertIsNone(bank.inbox_propose(self.conn, item["id"], None, 0.1)["proposed_key"])

    def test_approved_similar(self):
        bank.approved_add(self.conn, "Why are you interested in payments?", "Because...", "1", "Stripe")
        bank.approved_add(self.conn, "Describe a product you launched.", "I launched...")
        sim = bank.approved_similar(self.conn, "What makes you passionate about payments?")
        self.assertEqual([s["question"] for s in sim], ["Why are you interested in payments?"])

    def test_onboard_walks_unanswered_with_option_numbers(self):
        answers = iter(["Jane", "", "q"])
        out = tracker.onboard(self.conn, ask=lambda _: next(answers), say=lambda s: None)
        self.assertEqual(out["answered"], ["first_name"])
        self.assertEqual(out["skipped"], ["last_name"])
        replies = iter(["1"] * 50)
        tracker.onboard(self.conn, ask=lambda _: next(replies), say=lambda s: None)
        self.assertEqual(bank.get(self.conn, "authorized_ca")["answer"], "Yes")   # option 1


class CliTests(TempDb):
    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = tracker.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_resume_and_bank_commands(self):
        make_docx(self.dir / "r.docx", RESUME_LINES)
        code, rec = self.cli("resume", "add", str(self.dir / "r.docx"), "--name", "PM 2026")
        self.assertEqual((code, rec["name"]), (0, "PM 2026"))
        self.assertEqual(self.cli("resume", "default")[1]["id"], rec["id"])
        self.assertIn("card-issuing", self.cli("resume", "show", rec["id"], "--text")[1]["text"])
        self.assertEqual(self.cli("bank", "set", "email", "jane@example.com")[1]["answer"], "jane@example.com")
        self.assertEqual(self.cli("bank", "resolve", "Email")[1]["status"], "answered")

    def test_answers_are_masked_in_the_run_log(self):
        self.cli("bank", "set", "race_us", "Asian")
        item = self.cli("inbox", "add", "--question", "Ethnicity?")[1]
        self.cli("inbox", "resolve", str(item["id"]), "--key", "race_us", "--answer", "Asian")
        log = (Path(os.environ["HARNESS_LOG_DIR"]) / "tracker.jsonl").read_text()
        self.assertNotIn("Asian", log)
        self.assertIn("[answer]", log)

    def test_bad_request(self):
        code, out = self.cli("bank", "show", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no answer-bank key", out["error"])


if __name__ == "__main__":
    unittest.main()
