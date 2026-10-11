"""scripts/runlog.py, the master copy every component carries.

    python3 -m unittest discover -s scripts/tests -t scripts
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlog  # noqa: E402


class RedactTests(unittest.TestCase):
    def test_session_cookies_and_keys_are_redacted(self):
        out = runlog.redact({"li_at": "x", "Cookie": "a=b", "JSESSIONID": "ajax:1", "csrf_token": "t",
                             "api_key": "k", "company": "Stripe", "usage": {"input_tokens": 10}})
        for k in ("li_at", "Cookie", "JSESSIONID", "csrf_token", "api_key"):
            self.assertEqual(out[k], runlog.REDACTED, k)
        self.assertEqual(out["company"], "Stripe")
        self.assertEqual(out["usage"], {"input_tokens": 10})

    def test_token_shaped_values_are_redacted_anywhere(self):
        out = runlog.redact(["sk-proj-abcdefghij", "AQEDAR" + "x" * 30, "Senior PM"])
        self.assertEqual(out, [runlog.REDACTED, runlog.REDACTED, "Senior PM"])


class RunTests(unittest.TestCase):
    def test_writes_records_into_harness_log_dir(self):
        with runlog.start("demo", Path(__file__).parent, argv=["--x"]) as run:
            run.event("match_decision", id="1", decision="match", li_at="secret")
            run.finish(0)
        path = Path(os.environ["HARNESS_LOG_DIR"]) / "demo.jsonl"
        recs = [json.loads(line) for line in path.read_text().splitlines()]
        events = [r["event"] for r in recs if r["run_id"] == run.run_id]
        self.assertEqual(events, ["run_start", "match_decision", "run_end"])
        self.assertEqual(recs[-2]["data"]["li_at"], runlog.REDACTED)


if __name__ == "__main__":
    unittest.main()
