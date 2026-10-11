"""draft.py and map_question.py on canned model replies. No network.

    python3 -m unittest discover -s tests -t .
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import draft  # noqa: E402
import llm  # noqa: E402
import map_question  # noqa: E402

JOB = {"id": "9", "title": "Senior PM, Payouts", "company": "Stripe", "description": "Own payouts."}
RESUME = "Led payouts product at Wealthsimple, cutting failed transfers 30%."
KEYS = [{"key": "sponsorship_us", "question": "Will you now or in the future require visa sponsorship to work "
                                              "in the United States?", "group": "work_authorization"},
        {"key": "sponsorship_ca", "question": "Will you now or in the future require sponsorship to work in Canada?",
         "group": "work_authorization"}]


def reply(obj):
    return {"model": "gpt-6-luna", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj)}]}]}


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, payload):
        self.prompts.append(payload["input"])
        return reply(self.replies.pop(0))


class DraftTests(unittest.TestCase):
    def test_draft_within_limit_with_resume_points(self):
        model = Scripted({"answer": "I led payouts at Wealthsimple.", "resume_points": ["Led payouts product"]})
        out = draft.draft("Why Stripe?", JOB, RESUME, 500, post=model)
        self.assertEqual(out["answer"], "I led payouts at Wealthsimple.")
        self.assertTrue(out["within_limit"])
        self.assertIn("Use ONLY facts in the resume", model.prompts[0])
        self.assertIn(RESUME, model.prompts[0])

    def test_too_long_retries_once_then_flags(self):
        model = Scripted({"answer": "x" * 120, "resume_points": []}, {"answer": "y" * 90, "resume_points": []})
        out = draft.draft("Q", JOB, RESUME, 100, post=model)
        self.assertEqual((out["char_count"], out["within_limit"]), (90, True))
        self.assertIn("WAS 120 CHARACTERS", model.prompts[1])
        model = Scripted({"answer": "x" * 120, "resume_points": []}, {"answer": "y" * 110, "resume_points": []})
        self.assertFalse(draft.draft("Q", JOB, RESUME, 100, post=model)["within_limit"])

    def test_revision_and_examples_reach_the_prompt(self):
        model = Scripted({"answer": "Better.", "resume_points": []})
        draft.draft("Q", JOB, RESUME, 500, steer="mention payouts reliability", previous="Old draft.",
                    examples=[{"question": "Why fintech?", "answer": "Because money moves lives."}], post=model)
        p = model.prompts[0]
        self.assertIn("REVISE THIS EARLIER DRAFT:\nOld draft.", p)
        self.assertIn("THE CANDIDATE ASKS: mention payouts reliability", p)
        self.assertIn("Because money moves lives.", p)

    def test_empty_answer_is_error(self):
        with self.assertRaises(llm.ModelError):
            draft.draft("Q", JOB, RESUME, post=Scripted({"answer": " ", "resume_points": []}))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "job.json").write_text(json.dumps(JOB))
            (t / "r.txt").write_text(RESUME)
            out = io.StringIO()
            with redirect_stdout(out):
                code = draft.main(["--question", "Why?", "--job", str(t / "job.json"), "--resume", str(t / "r.txt")],
                                  post=Scripted({"answer": "Because.", "resume_points": ["a"]}))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["answer"], "Because.")


class MapTests(unittest.TestCase):
    def test_known_key_accepted(self):
        model = Scripted({"key": "sponsorship_us", "confidence": 0.93, "reason": "Same fact."})
        out, _ = map_question.propose("Do you need an H-1B now or later?", KEYS, ["Yes", "No"], model)
        self.assertEqual(out, {"key": "sponsorship_us", "confidence": 0.93, "reason": "Same fact."})
        self.assertIn("Options offered: Yes, No", model.prompts[0])

    def test_invented_key_becomes_null(self):
        out, _ = map_question.propose("Q?", KEYS, post=Scripted({"key": "h1b_status", "confidence": 0.99,
                                                                 "reason": "x"}))
        self.assertEqual((out["key"], out["confidence"]), (None, 0.0))

    def test_cli_refuses_keys_with_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "keys.json"
            p.write_text(json.dumps({"keys": [{**KEYS[0], "answer": "No"}]}))
            with self.assertRaises(SystemExit):
                map_question.main(["--question", "Q", "--keys", str(p)], post=Scripted())


if __name__ == "__main__":
    unittest.main()
