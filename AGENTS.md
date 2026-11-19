# job-harness

Job-search harness: finds matching roles on LinkedIn, lets the user review them,
and applies with a chosen resume. Independent services plus the workflows that
chain them. Spec, build order and design: `docs/plans/build-plan.md`.

## Commands
- Test (before every commit): `./scripts/test.sh` (about 2 minutes). It runs every
  `services/*/tests` and `workflows/*/tests` suite with the component's own
  `./python` environment if it has one, the `scripts` tests, and the check that
  shared module copies are identical.
- One service: `(cd services/<name> && python3 -m unittest discover -s tests -t .)`
- Read a run's log: `python3 scripts/trace.py --last` (or `<id>`, `--list`,
  `--errors`, `--full`).
- Tracker CLI (every command prints JSON): `python3 services/tracker/tracker.py --help`

## Done means
- `./scripts/test.sh` passes and new behaviour has a test.
- Apply-agent changes: a `--dry-run` on a real posting fills every field and submits nothing.

## Conventions
- Services: job-search (LinkedIn guest endpoints to postings JSON), matcher (hard
  filters, LLM fact extraction, fit score), answer-drafter, tracker (owns `jobs.db`),
  apply-agent (Playwright). All standard library only except apply-agent and
  tracker, which have their own environment at `./python`.
- `services/web-app/` is planned, not built: Phase A is headless (CLI and tests);
  the UI comes after the user signs off on mockups.
- Workflows: `daily_search` (scheduled: search, match, ingest), `apply`
  (apply-agent rounds with the answer bank and drafter until submitted),
  `portal_bench` (scores the apply-agent on real postings with fake data; never submits).
- Model calls: OpenAI Responses API, model `gpt-6-luna`, through `llm.py` with
  standard-library `urllib`. The client is passed in as `post` so tests use canned replies.
- `scripts/runlog.py` and `scripts/llm.py` are masters. Copy them identically
  into each component that uses them; the test script fails if a copy drifts.
- Log these decisions: why each posting was matched or rejected, its score and
  reasons, what each application field was filled with and where the value came
  from. Fingerprint inputs with `runlog.digest()`. A workflow's run ID is the
  trace ID; pass it to subprocesses with `Run.child_env()`.
- Tests send logs to a temp dir (each `tests/__init__.py` sets
  `HARNESS_LOG_DIR`), so run suites as packages (`-t .`).

## Gotchas
- The matcher drops postings older than 24 hours. Tests that go through the CLI
  must pass a fixed clock (`main(..., now=NOW)`), or they fail the day after
  they're written.
- `criteria.json` is personal and gitignored; `criteria.example.json` is the template.
- This repo is public. Keep personal details (compensation, names, contact
  details, real postings) out of committed files, fixtures and docs.

## Boundaries
- Contact details (name, email, phone, LinkedIn URL) never go to the model.
  Resume text is stored with placeholders (`resumes.redact_contact`), the apply
  workflow leaves contact keys out, and `llm.scrub_contact` strips them from
  every prompt. Other saved answers, self-identification included, may go to
  the model (the user's choice, 2026-10-14).
- `services/tracker/data/` holds resumes and the answer bank;
  `services/apply-agent/data/browser-profile/` holds a logged-in LinkedIn
  session. Never commit or print them, and never automate the LinkedIn login.
- Never submit a real application without the user's approval. Use `--dry-run`
  (fill every field, never submit) when testing.
