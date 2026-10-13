"""Tracker store and CLI on a temp database.

    python3 -m unittest discover -s tests -t .
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import store  # noqa: E402
import tracker  # noqa: E402

NOW = datetime(2026, 10, 11, 23, 0, tzinfo=timezone.utc)      # 19:00 in Toronto
TORONTO = timezone(timedelta(hours=-4))


def m(job_id, score=80, fresh=True, **over):
    return {"id": job_id, "source": "linkedin", "title": f"Senior PM {job_id}", "company": "Acme",
            "location": "Toronto, Ontario, Canada", "url": f"https://x/{job_id}", "apply_type": "easy",
            "posted_at": "2026-10-11T18:00+00:00",
            "match": {"score": score, "fresh": fresh, "flags": ["pay not listed"], "reasons": ["r"]}, **over}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ingest_is_idempotent_and_never_resets_a_review(self):
        r = store.ingest(self.conn, [m("1"), m("2")], NOW)
        self.assertEqual(r["added"], ["1", "2"])
        store.set_status(self.conn, "1", "kept", NOW)
        r = store.ingest(self.conn, [m("1", score=10), m("2", score=55)], NOW + timedelta(days=1))
        self.assertEqual((r["added"], r["updated"], r["skipped"]), ([], ["2"], ["1"]))
        self.assertEqual(store.get(self.conn, "1")["status"], "kept")
        two = store.get(self.conn, "2")
        self.assertEqual(two["match"]["score"], 55)                       # refreshed while unreviewed
        self.assertEqual(two["fetched_at"], NOW.isoformat(timespec="seconds"))   # first fetch kept

    def test_expire_after_seven_days_unreviewed_only(self):
        store.ingest(self.conn, [m("old"), m("kept")], NOW - timedelta(days=7))
        store.set_status(self.conn, "kept", "kept", NOW - timedelta(days=6))
        store.ingest(self.conn, [m("new")], NOW - timedelta(days=6, hours=23))
        self.assertEqual(store.expire(self.conn, NOW), ["old"])
        self.assertEqual(store.get(self.conn, "old")["history"][-1]["reason"], "unreviewed for 7 days")
        self.assertEqual(store.get(self.conn, "new")["status"], "new")

    def test_ingest_also_expires(self):
        store.ingest(self.conn, [m("old")], NOW - timedelta(days=8))
        self.assertEqual(store.ingest(self.conn, [m("x")], NOW)["expired"], ["old"])

    def test_transitions(self):
        store.ingest(self.conn, [m("1")], NOW)
        self.assertTrue(store.set_status(self.conn, "1", "discarded", NOW)["changed"])
        self.assertTrue(store.set_status(self.conn, "1", "kept", NOW)["changed"])         # change of mind
        self.assertFalse(store.set_status(self.conn, "1", "kept", NOW)["changed"])
        with self.assertRaises(store.TrackerError):
            store.set_status(self.conn, "1", "expired", NOW)
        with self.assertRaises(store.TrackerError):
            store.set_status(self.conn, "nope", "kept", NOW)
        hist = [h["to_status"] for h in store.get(self.conn, "1")["history"]]
        self.assertEqual(hist, ["new", "discarded", "kept"])

    def test_groups_by_local_fetch_date_with_days_left(self):
        store.ingest(self.conn, [m("a", score=60)], NOW)
        store.ingest(self.conn, [m("b", score=90, fresh=False), m("c", score=70)], NOW - timedelta(days=2))
        items = store.list_opportunities(self.conn, "new")
        groups = store.group_by_fetch_date(items, NOW, TORONTO)
        self.assertEqual([g["label"] for g in groups], ["Today", "Fri Oct 9"])
        self.assertEqual([g["expires_in_days"] for g in groups], [7, 5])
        self.assertEqual([i["id"] for i in groups[1]["items"]], ["c", "b"])      # fresh first, then score
        self.assertEqual(groups[0]["items"][0]["flags"], ["pay not listed"])

    def test_late_evening_utc_rollover_stays_on_local_day(self):
        # 23:30 Toronto is 03:30 UTC the next day; the group must still say that local day.
        late = datetime(2026, 10, 12, 3, 30, tzinfo=timezone.utc)
        store.ingest(self.conn, [m("a")], late)
        groups = store.group_by_fetch_date(store.list_opportunities(self.conn, "new"), late, TORONTO)
        self.assertEqual(groups[0]["date"], "2026-10-11")

    def test_runs_last_success(self):
        self.assertIsNone(store.last_success(self.conn, "daily_search"))
        a = store.run_start(self.conn, "daily_search", NOW - timedelta(days=1))
        store.run_finish(self.conn, a["id"], "ok", {"added": 3}, NOW - timedelta(days=1))
        b = store.run_start(self.conn, "daily_search", NOW)
        store.run_finish(self.conn, b["id"], "failed", {}, NOW)
        last = store.last_success(self.conn, "daily_search")
        self.assertEqual(last["id"], a["id"])
        self.assertEqual(last["stats"], {"added": 3})


class LocationMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def role(self, job_id, location, score, desc="Own payouts."):
        return m(job_id, score=score, title="Staff PM, Payments", company="Stripe", location=location,
                 description=desc)

    def test_same_role_in_two_cities_is_one_opportunity_best_score_main(self):
        r = store.ingest(self.conn, [self.role("sf", "San Francisco, CA", 74), self.role("ny", "New York, NY", 78)], NOW)
        self.assertEqual((r["added"], r["merged"]), (["ny"], ["sf"]))
        items = store.list_opportunities(self.conn, "new")
        self.assertEqual([(i["id"], i["locations"]) for i in items], [("ny", ["New York, NY", "San Francisco, CA"])])
        locs = store.get(self.conn, "ny")["locations"]
        self.assertEqual([(l["posting_id"], l["main"]) for l in locs], [("ny", True), ("sf", False)])
        again = store.ingest(self.conn, [self.role("sf", "San Francisco, CA", 74)], NOW)     # idempotent
        self.assertEqual((again["skipped"], again["merged"]), (["sf"], []))

    def test_later_city_joins_even_after_review(self):
        store.ingest(self.conn, [self.role("ny", "New York, NY", 78)], NOW)
        store.set_status(self.conn, "ny", "kept", NOW)
        r = store.ingest(self.conn, [self.role("sea", "Seattle, WA", 70)], NOW)
        self.assertEqual(r["merged"], ["sea"])
        self.assertEqual(len(store.get(self.conn, "ny")["locations"]), 2)

    def test_different_description_is_a_different_role(self):
        r = store.ingest(self.conn, [self.role("a", "Austin, TX", 80), self.role("b", "Dallas, TX", 70, "Other.")], NOW)
        self.assertEqual(sorted(r["added"]), ["a", "b"])

    def test_dedupe_merges_existing_unreviewed_only(self):
        # Rows stored before merging existed: simulate by clearing role_key and the merge.
        for i, (jid, loc, sc) in enumerate([("p1", "Purchase, NY", 88), ("p2", "O'Fallon, MO", 86)]):
            store.ingest(self.conn, [self.role(jid, loc, sc, desc=f"tmp{i}")], NOW)
        with self.conn:
            self.conn.execute("UPDATE opportunities SET role_key = NULL, data = json_set(data, '$.description', 'Same.')")
        store.ingest(self.conn, [m("g1", title="PM", company="Google", description="X"),
                                 m("g2", title="PM", company="Google", description="X ")], NOW)  # merged on ingest
        with self.conn:
            self.conn.execute("DELETE FROM opportunity_locations")
            self.conn.execute("INSERT INTO opportunities (id, source, data, status, fetched_at) "
                              "SELECT 'g2', source, data, 'discarded', fetched_at FROM opportunities WHERE id = 'g1'")
            self.conn.execute("UPDATE opportunities SET role_key = NULL")
        out = store.dedupe(self.conn, NOW)
        self.assertEqual(out["merged"], [{"posting": "p2", "into": "p1"}])      # Google pair has a reviewed row
        self.assertEqual([l["location"] for l in store.get(self.conn, "p1")["locations"]],
                         ["Purchase, NY", "O'Fallon, MO"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TRACKER_DB": str(Path(self.tmp.name) / "jobs.db")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = tracker.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_ingest_list_keep_show(self):
        path = Path(self.tmp.name) / "matches.json"
        path.write_text(json.dumps({"matches": [m("1"), m("2")]}))
        code, out = self.cli("ingest", str(path))
        self.assertEqual((code, out["added"]), (0, ["1", "2"]))
        code, out = self.cli("list", "--status", "new", "--group-by", "fetch-date")
        self.assertEqual(len(out["groups"][0]["items"]), 2)
        self.assertEqual(self.cli("keep", "1")[1]["results"][0]["status"], "kept")
        self.assertEqual(self.cli("discard", "2")[1]["results"][0]["status"], "discarded")
        code, out = self.cli("show", "1")
        self.assertEqual((out["status"], out["match"]["reasons"]), ("kept", ["r"]))
        self.assertEqual(self.cli("list", "--status", "kept")[1]["items"][0]["id"], "1")

    def test_errors_exit_1_with_json(self):
        code, out = self.cli("keep", "missing")
        self.assertEqual(code, 1)
        self.assertIn("no opportunity", out["error"])

    def test_run_commands(self):
        run_id = self.cli("run", "start", "--kind", "daily_search")[1]["id"]
        self.cli("run", "finish", run_id, "--status", "ok", "--stats", '{"added": 2}')
        out = self.cli("run", "last-success", "--kind", "daily_search")[1]
        self.assertEqual(out["last_success"]["id"], run_id)


if __name__ == "__main__":
    unittest.main()
