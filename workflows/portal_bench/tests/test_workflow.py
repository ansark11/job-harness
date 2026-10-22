"""Target building for the portal benchmark.

    python3 -m unittest discover -s workflows/portal_bench/tests -t workflows/portal_bench
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as wf  # noqa: E402

ITEMS = [
    {"id": "1", "company": "A", "title": "PM", "url": "https://www.linkedin.com/jobs/view/1", "apply_type": "external",
     "status": "new"},
    {"id": "2", "company": "B", "title": "PM", "url": "u2", "apply_type": "easy", "status": "new"},
    {"id": "3", "company": "C", "title": "PM", "url": "u3", "apply_type": "unknown", "status": "kept"},
    {"id": "4", "company": "D", "title": "PM", "url": "u4", "apply_type": "external", "status": "expired"},
]


class TargetTests(unittest.TestCase):
    def test_portal_postings_only(self):
        ids = [t["id"] for t in wf.build_targets(ITEMS)]
        self.assertEqual(ids, ["1", "3"])

    def test_status_filter(self):
        self.assertEqual([t["id"] for t in wf.build_targets(ITEMS, status="kept")], ["3"])

    def test_extra_urls_go_straight_to_the_portal(self):
        t = wf.build_targets([], ["# comment", "", "https://jobs.lever.co/x/1"])
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["apply_url"], "https://jobs.lever.co/x/1")
        self.assertEqual(t[0]["company"], "jobs.lever.co")


if __name__ == "__main__":
    unittest.main()
