"""daily_search orchestration with faked service calls. No network, no real tracker.

    python3 -m unittest discover -s workflows/daily_search/tests -t workflows/daily_search
"""

import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as wf  # noqa: E402

TORONTO = timezone(timedelta(hours=-4))


def args(**over):
    base = {"resume": "/tmp/resume.txt", "force": False, "max_pages": None, "no_notify": False}
    return argparse.Namespace(**{**base, **over})


class NullRun:
    def __init__(self):
        self.events = []

    def event(self, name, **data):
        self.events.append((name, data))

    error = warn = event


class FakeServices:
    """Plays job-search, matcher and tracker; records every call."""

    def __init__(self, last=None, fail=None):
        self.last = last
        self.fail = fail
        self.calls = []
        self.finished = None

    def __call__(self, service, a, stdin=None):
        self.calls.append((service, a))
        if service == self.fail:
            raise wf.StepError(f"{service} exited 2: boom")
        if service == "job-search":
            out = Path(a[a.index("--out") + 1])
            out.write_text(json.dumps({"window_seconds": 86400, "postings": [{"id": "1"}, {"id": "2"}]}))
            return ""
        if service == "matcher":
            Path(a[a.index("--out") + 1]).write_text(json.dumps({"matches": [{"id": "1"}], "rejected": [{"id": "2"}]}))
            return ""
        if a[:2] == ["run", "last-success"]:
            return json.dumps({"last_success": self.last})
        if a[:2] == ["run", "start"]:
            return json.dumps({"id": "r1", "kind": "daily_search"})
        if a[:2] == ["run", "finish"]:
            # ["run", "finish", <id>, "--status", <status>, "--stats", <json>]
            self.finished = (a[4], json.loads(a[6]))
            return json.dumps({"id": a[2], "status": a[4]})
        if a[0] == "ingest":
            return json.dumps({"added": ["1"], "updated": [], "skipped": [], "expired": []})
        raise AssertionError(a)

    def services(self):
        return [s if s != "tracker" else f"tracker {a[0]}" for s, a in self.calls]


class GuardTests(unittest.TestCase):
    def test_boundary(self):
        evening = datetime(2026, 10, 11, 19, 30, tzinfo=TORONTO)
        morning = datetime(2026, 10, 12, 8, 0, tzinfo=TORONTO)
        self.assertEqual(wf.boundary(evening), datetime(2026, 10, 11, 19, 0, tzinfo=TORONTO))
        self.assertEqual(wf.boundary(morning), datetime(2026, 10, 11, 19, 0, tzinfo=TORONTO))

    def test_already_ran(self):
        now = datetime(2026, 10, 12, 8, 0, tzinfo=TORONTO)     # login next morning
        ran_last_night = {"finished_at": "2026-10-11T23:05:00+00:00"}    # 19:05 Toronto
        ran_yesterday_afternoon = {"finished_at": "2026-10-11T19:00:00+00:00"}   # 15:00 Toronto
        self.assertTrue(wf.already_ran(ran_last_night, now))
        self.assertFalse(wf.already_ran(ran_yesterday_afternoon, now))   # missed 19:00 -> catch up
        self.assertFalse(wf.already_ran(None, now))


class RunDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(wf, "DATA", Path(self.tmp.name))
        self.patch.start()
        self.notes = []
        self.now = datetime(2026, 10, 11, 23, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def notifier(self, title, msg):
        self.notes.append((title, msg))

    def test_happy_path_chains_services_and_records_run(self):
        last = {"started_at": "2026-10-10T23:00:00+00:00", "finished_at": "2026-10-10T23:09:00+00:00"}
        svc = FakeServices(last=last)
        code, stats = wf.run_daily(args(), svc, self.now, NullRun(), self.notifier)
        self.assertEqual(code, 0)
        self.assertEqual(svc.services(), ["tracker run", "tracker run", "job-search", "matcher",
                                          "tracker ingest", "tracker run"])
        search_args = svc.calls[2][1]
        self.assertEqual(search_args[search_args.index("--since") + 1], last["started_at"])
        self.assertEqual(svc.finished[0], "ok")
        self.assertEqual(stats, {"postings": 2, "matches": 1, "rejected": 1, "added": 1, "expired": 0,
                                 "window_seconds": 86400})
        self.assertEqual(self.notes, [("Job search", "1 new match to review")])

    def test_skips_when_already_ran_unless_forced(self):
        last = {"started_at": "2026-10-11T23:00:00+00:00", "finished_at": "2026-10-11T23:01:00+00:00"}
        svc = FakeServices(last=last)
        now = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)
        code, summary = wf.run_daily(args(), svc, now.astimezone(TORONTO), NullRun(), self.notifier)
        self.assertEqual(summary, {"skipped": True})
        self.assertEqual(len(svc.calls), 1)
        code, summary = wf.run_daily(args(force=True), svc, now.astimezone(TORONTO), NullRun(), self.notifier)
        self.assertNotIn("skipped", summary)

    def test_failure_marks_run_failed_and_notifies(self):
        svc = FakeServices(fail="matcher")
        code, summary = wf.run_daily(args(), svc, self.now, NullRun(), self.notifier)
        self.assertEqual(code, 1)
        self.assertEqual(svc.finished[0], "failed")
        self.assertIn("matcher exited 2", svc.finished[1]["error"])
        self.assertEqual(self.notes[0][0], "Job search failed")

    def test_first_run_has_no_since(self):
        svc = FakeServices(last=None)
        wf.run_daily(args(no_notify=True), svc, self.now, NullRun(), self.notifier)
        self.assertNotIn("--since", svc.calls[2][1])
        self.assertEqual(self.notes, [])

    def test_missing_resume_fails_before_starting_a_run(self):
        svc = FakeServices()
        orig = svc.__call__

        def call(service, a, stdin=None):
            if a[:2] == ["resume", "default"]:
                raise wf.StepError("tracker exited 2")
            return orig(service, a, stdin)
        with self.assertRaises(wf.StepError) as cm:
            wf.run_daily(args(resume=None), call, self.now, NullRun(), self.notifier)
        self.assertIn("no resume", str(cm.exception))

    def test_prune_keeps_recent_run_dirs(self):
        for i in range(5):
            d = Path(self.tmp.name) / f"2026100{i}T190000-r{i}"
            d.mkdir()
            (d / "postings.json").write_text("{}")
        wf.prune(Path(self.tmp.name), keep=2)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["20261003T190000-r3", "20261004T190000-r4"])


if __name__ == "__main__":
    unittest.main()
