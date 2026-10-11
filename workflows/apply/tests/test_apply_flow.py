"""The apply loop end to end: the real tracker (temp database, in-process), a
scripted form for the apply-agent, and canned drafter/mapper replies.

    python3 -m unittest discover -s workflows/apply/tests -t workflows/apply
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(1, str(HERE.parents[2] / "services" / "tracker"))

import run as wf  # noqa: E402
import tracker  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path, lines):
    import zipfile
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')

PHONE = {"key": "mobile phone number", "label": "Mobile phone number", "type": "text", "required": True}
WHY = {"key": "why do you want to work at test co", "label": "Why do you want to work at Test Co?",
       "type": "textarea", "required": True, "maxlength": 1000}
GENDER = {"key": "gender", "label": "Gender", "type": "select", "required": False,
          "options": ["Male", "Female", "I don't wish to answer"]}
SPONSOR = {"key": "will you now or in the future require visa sponsorship to work in the united states",
           "label": "Will you now or in the future require visa sponsorship to work in the United States?",
           "type": "radio", "required": True, "options": ["Yes", "No"]}
LICENSE = {"key": "do you hold a valid driver s license", "label": "Do you hold a valid driver's license?",
           "type": "radio", "required": True, "options": ["Yes", "No"]}
HOBBY = {"key": "favourite hobby", "label": "Favourite hobby", "type": "text", "required": False}
COVER = {"key": "cover letter", "label": "Cover letter", "type": "textarea", "required": False}
FORM = [[PHONE], [WHY, GENDER, SPONSOR, LICENSE, HOBBY, COVER]]


class Services:
    """tracker: the real CLI in-process. apply-agent: walks FORM. drafter/mapper: canned."""

    def __init__(self):
        self.agent_runs = []
        self.mapper_inputs = []
        self.submitted = None

    def __call__(self, tool, args):
        if tool == "tracker":
            out = io.StringIO()
            with redirect_stdout(out):
                code = tracker.main(args)
            result = json.loads(out.getvalue())
            if code:
                raise wf.StepError(result["error"])
            return result
        if tool == "drafter":
            q = args[args.index("--question") + 1]
            steer = args[args.index("--steer") + 1] if "--steer" in args else None
            return {"answer": f"Draft for {q}" + (f" ({steer})" if steer else ""), "resume_points": ["Led payouts"],
                    "char_count": 20, "max_chars": 1000, "within_limit": True}
        if tool == "mapper":
            self.mapper_inputs.append(Path(args[args.index("--keys") + 1]).read_text())
            q = args[args.index("--question") + 1]
            key = "phone" if "phone" in q.lower() else None
            return {"key": key, "confidence": 0.9 if key else 0.0, "reason": "same fact" if key else "new"}
        if tool == "apply-agent":
            return self.agent(json.loads(Path(args[0]).read_text()), args)
        raise AssertionError(tool)

    def agent(self, packet, args):
        self.agent_runs.append(packet)
        answers, skip = packet["answers"], set(packet["skip"])
        seen = []
        for page_no, page in enumerate(FORM, 1):
            unknown = False
            for f in page:
                outcome = "filled" if f["key"] in answers else "skipped" if f["key"] in skip else "unknown"
                unknown |= outcome == "unknown"
                seen.append({**f, "page": page_no, "outcome": outcome, "source": "packet" if outcome == "filled" else None})
            if unknown:
                return {"status": "needs_input", "fields": seen, "unknown": [], "screenshots": []}
        if "--dry-run" in args:
            return {"status": "ready", "fields": seen, "screenshots": []}
        self.submitted = answers
        return {"status": "submitted", "fields": seen, "screenshots": []}


class ApplyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"TRACKER_DB": str(t / "jobs.db")}),
                        mock.patch.object(wf, "DATA", t / "wf")]
        for p in self.patches:
            p.start()
        self.svc = Services()
        resume = t / "Jane Doe - Resume.docx"
        make_docx(resume, ["Jane Doe, Senior PM. Led payouts at a fintech, growing volume 4x."] * 5)
        matches = t / "m.json"
        matches.write_text(json.dumps([{"id": "1", "title": "Senior PM", "company": "Test Co", "apply_type": "easy",
                                        "location": "New York, NY", "url": "https://x/1", "description": "Payouts.",
                                        "match": {"score": 90, "fresh": True}}]))
        for cmd in (["resume", "add", str(resume)], ["ingest", str(matches)], ["keep", "1"],
                    ["bank", "set", "phone", "416-555-0100"], ["bank", "set", "sponsorship_us", "No"],
                    ["bank", "set", "gender_us", "I don't wish to answer"]):
            self.svc("tracker", cmd)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def wf(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = wf.main(list(argv), call=self.svc)
        return code, json.loads(out.getvalue())

    def test_full_flow_from_inbox_to_submitted(self):
        # Round 1: the phone question's wording is new -> inbox, with the mapper's suggested key.
        code, out = self.wf("start", "1")
        app_id = out["application_id"]
        self.assertEqual((code, out["status"]), (0, "needs_input"))
        self.assertEqual([n["label"] for n in out["needs"]], ["Mobile phone number"])
        inbox = self.svc("tracker", ["inbox", "list"])["items"]
        self.assertEqual((inbox[0]["proposed_key"], inbox[0]["application_id"]), ("phone", app_id))
        self.assertNotIn("416-555-0100", self.svc.mapper_inputs[0])          # model never sees answers

        # You confirm the suggestion; page 2 then needs a draft review and one new question.
        self.svc("tracker", ["inbox", "resolve", str(inbox[0]["id"]), "--key", "phone"])
        code, out = self.wf("continue", app_id)
        needs = {n["label"]: n for n in out["needs"]}
        self.assertEqual(set(needs), {WHY["label"], LICENSE["label"]})
        self.assertEqual(needs[WHY["label"]]["state"], "needs_review")
        self.assertEqual(needs[WHY["label"]]["resume_points"], ["Led payouts"])
        items = {i["key"]: i for i in self.svc("tracker", ["application", "show", app_id])["items"]}
        self.assertEqual((items["gender"]["state"], items["gender"]["source"]), ("filled", "bank"))
        self.assertEqual(items[SPONSOR["key"]]["value"], "No")
        self.assertEqual(items[HOBBY["key"]]["state"], "skipped")             # optional unknown: blank
        self.assertEqual(items[COVER["key"]]["state"], "skipped")             # optional long-form: blank

        # Regenerate with a steer, approve it, answer the new question once.
        why_n = needs[WHY["label"]]["n"]
        _, out = self.wf("regenerate", app_id, str(why_n), "--steer", "shorter")
        self.assertIn("(shorter)", next(n for n in out["needs"] if n["label"] == WHY["label"])["draft"])
        self.svc("tracker", ["application", "approve", app_id, str(why_n)])
        lic = next(i for i in self.svc("tracker", ["inbox", "list"])["items"] if i["question"] == LICENSE["label"])
        self.svc("tracker", ["inbox", "answer", str(lic["id"]), "--answer", "Yes"])

        code, out = self.wf("continue", app_id)
        self.assertEqual((code, out["status"], out["needs"]), (0, "submitted", []))
        self.assertEqual(self.svc.submitted[LICENSE["key"]], "Yes")
        self.assertEqual(self.svc.submitted[WHY["key"]], f"Draft for {WHY['label']} (shorter)")
        library = self.svc("tracker", ["answers", "list"])["answers"]
        self.assertEqual(library[0]["question"], WHY["label"])
        # The license question now resolves from the bank for every future application.
        self.assertEqual(self.svc("tracker", ["bank", "resolve", LICENSE["label"]])["answer"], "Yes")

    def test_dry_run_stops_at_ready(self):
        self.svc("tracker", ["bank", "set", "phone", "416"])
        app_id = self.wf("start", "1", "--dry-run")[1]["application_id"]
        inbox = self.svc("tracker", ["inbox", "list"])["items"]
        self.svc("tracker", ["inbox", "resolve", str(inbox[0]["id"]), "--key", "phone"])
        self.wf("continue", app_id, "--dry-run")
        self.svc("tracker", ["application", "approve", app_id, WHY["key"]])
        lic = next(i for i in self.svc("tracker", ["inbox", "list"])["items"] if i["question"] == LICENSE["label"])
        self.svc("tracker", ["inbox", "answer", str(lic["id"]), "--answer", "Yes"])
        code, out = self.wf("continue", app_id, "--dry-run")
        self.assertEqual(out["status"], "ready")
        self.assertIsNone(self.svc.submitted)

    def test_agent_failure_statuses(self):
        def agent(packet, args):
            return {"status": "login_required", "fields": []}
        self.svc.agent = agent
        self.svc("tracker", ["bank", "set", "phone", "1"])
        code, out = self.wf("start", "1")
        self.assertEqual(out["status"], "failed")
        self.assertIn("login.py", out["note"])

    def test_draft_failure_asks_you_to_write_it(self):
        orig = self.svc.__class__.__call__

        def call(tool, args):
            if tool == "drafter":
                raise wf.StepError("draft.py exited 2: OPENAI_API_KEY not set")
            return orig(self.svc, tool, args)
        self.svc("tracker", ["bank", "set", "phone", "1"])
        out = io.StringIO()
        with redirect_stdout(out):
            wf.main(["start", "1"], call=call)
        app_id = json.loads(out.getvalue())["application_id"]
        inbox = self.svc("tracker", ["inbox", "list"])["items"]
        self.svc("tracker", ["inbox", "resolve", str(inbox[0]["id"]), "--key", "phone"])
        out = io.StringIO()
        with redirect_stdout(out):
            wf.main(["continue", app_id], call=call)
        why = next(n for n in json.loads(out.getvalue())["needs"] if n["label"] == WHY["label"])
        self.assertEqual(why["state"], "needs_answer")
        self.assertIn("OPENAI_API_KEY", why["note"])

    def test_bank_answer_that_fits_no_option_asks_you(self):
        def agent(packet, args):
            f = {**SPONSOR, "page": 1, "outcome": "unknown", "error": "'Nope' matches none of ['Yes', 'No']"}
            return {"status": "needs_input", "fields": [f]}
        self.svc.agent = agent
        code, out = self.wf("start", "1")
        self.assertEqual(out["needs"][0]["note"], "'Nope' matches none of ['Yes', 'No']")
        self.assertIn("application edit", out["needs"][0]["do"][0])


if __name__ == "__main__":
    unittest.main()
