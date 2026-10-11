"""scripts/trace.py over a fake log tree. No real logs touched.

    python3 -m unittest discover -s scripts/tests -t scripts
"""

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trace as trace_cli  # noqa: E402


def rec(ts, comp, event, run_id, trace_id="t1", parent=None, level="info", **data):
    return {"ts": f"2026-10-10T00:00:{ts:02d}.000+00:00", "trace_id": trace_id, "run_id": run_id,
            "parent_run_id": parent, "component": comp, "cmd": ["x.py"], "code_version": "abc",
            "event": event, "level": level, "data": data}


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        wf = root / "workflows" / "daily_search" / "logs"
        st = root / "services" / "job-search" / "logs"
        wf.mkdir(parents=True)
        st.mkdir(parents=True)
        (wf / "daily_search.jsonl").write_text("\n".join(json.dumps(r) for r in [
            rec(1, "daily_search", "run_start", "w1"),
            rec(3, "daily_search", "service_call", "w1", service="job-search", exit_code=0),
            rec(9, "daily_search", "run_start", "w2", trace_id="t2"),
        ]) + "\nnot json\n")
        (st / "job-search.jsonl").write_text("\n".join(json.dumps(r) for r in [
            rec(2, "job-search", "match_decision", "s1", parent="w1", id="4012", decision="match"),
            rec(2, "job-search", "invalid_input", "s1", parent="w1", level="warning", details=["bad"]),
        ]) + "\n")
        # Regression: a different format in the same logs/ dir crashed --last
        # with KeyError: 'trace_id'. It must be ignored, as must malformed lines.
        (st / "link_events.jsonl").write_text(json.dumps({"at": "2026-10-10T09:00:00", "kind": "exit"}) + "\n")
        with open(st / "job-search.jsonl", "a") as f:
            f.write(json.dumps({"ts": "2026-10-10T00:00:05", "event": "no ids"}) + "\n[1, 2]\n")
        self.patch = mock.patch.object(trace_cli, "ROOT", root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = trace_cli.main(list(argv))
        return code, out.getvalue()

    def test_trace_merges_components_in_time_order_with_child_indent(self):
        code, out = self.run_cli("t1")
        lines = [line for line in out.splitlines()[1:]]
        self.assertEqual(code, 0)
        self.assertIn("4 records from daily_search, job-search", out)
        # The component column is the first component name on each line (the
        # data after it can mention others, e.g. service=job-search).
        comps = [re.search(r"(daily_search|job-search)", line).group(1) for line in lines]
        self.assertEqual(comps, ["daily_search", "job-search", "job-search", "daily_search"])
        self.assertIn("    job-search", lines[1])               # child run indented under its parent
        self.assertIn("decision=match", lines[1])

    def test_last_list_errors_json(self):
        self.assertIn("trace t2", self.run_cli("--last")[1])
        listing = self.run_cli("--list")[1]
        self.assertIn("t1", listing)
        self.assertIn("t2", listing)
        self.assertIn("invalid_input", self.run_cli("--errors")[1])
        code, out = self.run_cli("t1", "--json")
        self.assertEqual(len(json.loads(out)), 4)

    def test_foreign_and_malformed_records_are_ignored(self):
        recs = trace_cli.records()
        self.assertEqual(len(recs), 5)                              # only the 5 well-formed runlog records
        self.assertEqual(self.run_cli("--last")[0], 0)

    def test_unknown_and_ambiguous_prefix(self):
        self.assertEqual(self.run_cli("zzz")[0], 1)
        code, out = self.run_cli("t")
        self.assertEqual(code, 1)
        self.assertIn("Ambiguous", out)


if __name__ == "__main__":
    unittest.main()
