"""The match pipeline on canned model replies. No network.

    python3 -m unittest discover -s tests -t .
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import match  # noqa: E402
from tests.test_rules import CFG, facts, posting  # noqa: E402

NOW = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)


def reply(obj):
    return {"model": "gpt-6-luna", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj)}]}]}


class CannedModel:
    """Replies by the posting title found in the prompt."""

    def __init__(self, by_title):
        self.by_title = by_title
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        self.payload = payload
        for title, obj in self.by_title.items():
            if f"Title: {title}\n" in payload["input"]:
                if isinstance(obj, Exception):
                    raise obj
                return reply(obj)
        raise AssertionError("unexpected prompt")


class MatchPostingsTests(unittest.TestCase):
    def setUp(self):
        self.postings = [
            posting(id="1", title="Senior PM, Payments", posted_at="2026-10-11T20:00+00:00"),
            posting(id="2", title="Senior PM, Checkout", posted_at="2026-10-09T20:00+00:00"),
            posting(id="3", title="Senior PM, Stores", criteria={"industries": "Retail"}),
            posting(id="4", title="Senior PM, Snacks"),
            posting(id="5", title="Senior PM, Older but great", posted_at="2026-10-09T20:00+00:00"),
        ]
        self.model = CannedModel({
            "Senior PM, Payments": facts(fit_score=70),
            "Senior PM, Checkout": facts(fit_score=60),
            "Senior PM, Snacks": facts(industry="cpg", industry_reason="Snack maker.", role_domain="other"),
            "Senior PM, Older but great": facts(fit_score=95),
        })

    def test_prefilter_skips_model_rules_reject_and_fresh_first_sort(self):
        matches, rejected, stats = match.match_postings(self.postings, CFG, "RESUME", NOW, self.model)
        self.assertEqual([m["id"] for m in matches], ["1", "5", "2"])   # fresh first, then by score
        self.assertTrue(matches[0]["match"]["fresh"])
        self.assertEqual({r["id"]: r["stage"] for r in rejected}, {"3": "prefilter", "4": "rules"})
        self.assertEqual(stats["model_calls"], 4)                        # Retail posting never sent
        self.assertEqual(stats["prefiltered"], 1)

    def test_request_uses_strict_schema_and_includes_resume(self):
        match.match_postings(self.postings[:1], CFG, "MY RESUME TEXT", NOW, self.model)
        body = self.model.calls[0]
        self.assertEqual(body["model"], llm.MODEL)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertIn("MY RESUME TEXT", body["input"])

    def test_cache_avoids_repeat_calls_and_resume_change_invalidates(self):
        cache = {}
        match.match_postings(self.postings[:1], CFG, "R1", NOW, self.model, cache)
        _, _, stats = match.match_postings(self.postings[:1], CFG, "R1", NOW, self.model, cache)
        self.assertEqual((stats["model_calls"], stats["cache_hits"]), (0, 1))
        _, _, stats = match.match_postings(self.postings[:1], CFG, "R2", NOW, self.model, cache)
        self.assertEqual(stats["model_calls"], 1)

    def test_model_error_keeps_posting_flagged_not_scored(self):
        self.model.by_title["Senior PM, Payments"] = llm.ModelError("OpenAI API HTTP 500: boom")
        matches, _, stats = match.match_postings(self.postings[:1], CFG, "R", NOW, self.model)
        self.assertEqual(stats["model_errors"], 1)
        self.assertIsNone(matches[0]["match"]["score"])
        self.assertIn("not scored", matches[0]["match"]["flags"][0])

    def test_invalid_reply_is_model_error(self):
        bad = CannedModel({"Senior PM, Payments": {"work_mode": "hybrid"}})
        matches, _, stats = match.match_postings(self.postings[:1], CFG, "R", NOW, bad)
        self.assertEqual(stats["model_errors"], 1)

    def test_reply_wrapped_in_prose_is_parsed(self):
        def post(payload):
            r = reply({})
            r["output"][0]["content"][0]["text"] = "Here you go:\n```json\n" + json.dumps(facts()) + "\n```"
            return r
        matches, _, stats = match.match_postings(self.postings[:1], CFG, "R", NOW, post)
        self.assertEqual(stats["model_errors"], 0)


class ContactScrubTests(unittest.TestCase):
    def test_contact_shapes_never_reach_the_model(self):
        sent = []
        llm.call_json("Reach me at jane@x.com or (647) 779-7348, linkedin.com/in/jane. Pay $150,000-$200,000; "
                      "req 4478553230.", "s", {}, lambda body: sent.append(body) or reply({}))
        self.assertEqual(sent[0]["input"], "Reach me at [email] or [phone], [linkedin]. Pay $150,000-$200,000; "
                                           "req 4478553230.")


class TransientFailureTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.patch = mock.patch.object(llm, "sleep", self.sleeps.append)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_read_timeout_is_retried_then_succeeds(self):
        calls = []

        def post(payload):
            calls.append(1)
            if len(calls) < 3:
                raise llm.TransientError("OpenAI API connection failed: TimeoutError")
            return reply(facts())
        matches, _, stats = match.match_postings([posting(id="1", title="T")], CFG, "R", NOW, post)
        self.assertEqual((len(calls), stats["model_errors"]), (3, 0))
        self.assertEqual(self.sleeps, list(llm.RETRY_WAITS))

    def test_retries_exhausted_flags_posting_instead_of_crashing(self):
        def post(payload):
            raise llm.TransientError("OpenAI API connection failed: TimeoutError")
        matches, _, stats = match.match_postings([posting(id="1", title="T")], CFG, "R", NOW, post)
        self.assertEqual(stats["model_errors"], 1)
        self.assertIn("not scored", matches[0]["match"]["flags"][0])

    def test_permanent_errors_are_not_retried(self):
        calls = []

        def post(payload):
            calls.append(1)
            raise llm.ModelError("OpenAI API HTTP 401: bad key")
        match.match_postings([posting(id="1", title="T")], CFG, "R", NOW, post)
        self.assertEqual(len(calls), 1)

    def test_socket_timeout_maps_to_transient(self):
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=TimeoutError("The read operation timed out")), \
                mock.patch.object(llm, "api_key", return_value="k"):
            with self.assertRaises(llm.TransientError):
                llm._urllib_post({})

    def test_cache_saved_after_each_extraction_survives_a_crash(self):
        saved = []

        def post(payload):
            if "Title: Second" in payload["input"]:
                raise RuntimeError("something unexpected")
            return reply(facts())
        with mock.patch.object(match, "WORKERS", 1), self.assertRaises(RuntimeError):
            match.match_postings([posting(id="1", title="First"), posting(id="2", title="Second")], CFG, "R",
                                 NOW, post, {}, None, lambda c: saved.append(dict(c)))
        self.assertEqual([v["id"] for v in saved[-1].values()], ["1"])


class CliTests(unittest.TestCase):
    def test_cli_end_to_end_with_cache_file(self):
        model = CannedModel({"Senior PM, Payments": facts()})
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "postings.json").write_text(json.dumps({"postings": [posting(id="1", title="Senior PM, Payments")]}))
            (t / "criteria.json").write_text(json.dumps({"match": CFG}))
            (t / "resume.txt").write_text("resume")
            args = [str(t / "postings.json"), "--criteria", str(t / "criteria.json"),
                    "--resume", str(t / "resume.txt"), "--out", str(t / "out.json")]
            with mock.patch.object(match, "CACHE_PATH", t / "cache.json"), redirect_stderr(io.StringIO()):
                self.assertEqual(match.main(args, post=model), 0)
                self.assertEqual(match.main(args, post=model), 0)
            out = json.loads((t / "out.json").read_text())
            self.assertEqual([m["id"] for m in out["matches"]], ["1"])
            self.assertEqual(len(model.calls), 1)                     # second run served from cache

    def test_all_model_errors_exit_2(self):
        model = CannedModel({"Senior PM, Payments": llm.ModelError("down")})
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "p.json").write_text(json.dumps([posting(id="1", title="Senior PM, Payments")]))
            (t / "c.json").write_text(json.dumps({"match": CFG}))
            (t / "r.txt").write_text("r")
            with redirect_stderr(io.StringIO()):
                code = match.main([str(t / "p.json"), "--criteria", str(t / "c.json"), "--resume", str(t / "r.txt"),
                                   "--out", str(t / "o.json"), "--no-cache"], post=model)
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
