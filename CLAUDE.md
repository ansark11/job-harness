# job-harness

Job-search harness: find matching roles on LinkedIn, review them, apply with
a chosen resume. Interview prep comes later. Independent services plus the
workflows that chain them. Spec: `../opportunity-search.md`.

## Layout

| Path | What |
|---|---|
| `services/job-search/` | LinkedIn guest-endpoint search → normalized postings JSON. Stdlib only. |
| `services/matcher/` | Hard filters + LLM fact extraction + fit score → matches JSON. Stdlib only. |
| `services/answer-drafter/` | Drafts long-form answers; maps new questions to answer-bank keys. Stdlib only. |
| `services/apply-agent/` | Playwright: LinkedIn Easy Apply and external portals. Own venv at `./python`. |
| `services/tracker/` | Owns `jobs.db`: opportunities, resumes, answer bank, applications. CLI now, API later. |
| `services/web-app/` | React UI over tracker's API. Built last (Phase B). |
| `workflows/` | Orchestration that calls services in sequence. The only place that knows more than one service exists. |
| `launchd/` | LaunchAgent plists for the daily run. |

Build order and design: `~/.claude/plans/i-m-looking-to-build-sleepy-floyd.md`.
Phase A builds every feature headless (CLI + tests); the UI comes after the
user signs off, starting with several mockup options to choose from.

## Boundary rules

- **No cross-service imports.** Services communicate only through CLI
  invocation and JSON output, so each one works standalone or as a subagent.
- **Glue goes in `workflows/`.**
- **Each service owns its dependencies, `data/`, `logs/` and tests.** Run a
  service's commands from inside its directory.
- **The model proposes; deterministic code decides.** LLM calls extract facts
  or draft text; filter and submit decisions are made by code from those facts.

## LLM

OpenAI Responses API, model `gpt-6-luna`, called with stdlib `urllib` (same
pattern as finance-harness `card-offers/discover.py`). Every function that
calls the model takes an injectable `post` so tests use canned replies.
`OPENAI_API_KEY` lives in each LLM-using service's `.env` (gitignored).
The client is `llm.py`, copied from `scripts/llm.py`.

## Logging

Every CLI entry point writes structured JSON-lines records through
`runlog.py` to `<component>/logs/<component>.jsonl` (gitignored).

- **Correlation:** a workflow's run ID is the trace ID; use
  `Run.child_env()` when starting a subprocess.
- **View a run** with `python3 scripts/trace.py --last` (or `<id>`,
  `--list`, `--errors`, `--full`).
- **Log decisions, not just outcomes:** why each posting was matched or
  rejected, its score and reasons, what each application field was filled
  with and where the value came from. Fingerprint inputs with `runlog.digest()`.
- **Never log secrets.** `runlog.redact()` masks credential keys, session
  cookies (`li_at`, `JSESSIONID`) and OpenAI-key-shaped values.
- **`scripts/runlog.py` and `scripts/llm.py` are masters.** Copy them
  identically into each component that uses them; `scripts/test.sh` fails
  if a copy drifts.
- **Tests must never write the real logs.** Each `tests/__init__.py` sets
  `HARNESS_LOG_DIR` to a temp dir, so run suites as packages (`-t .`).

## Sensitive data

- `services/tracker/data/` holds resumes and the answer bank, including
  self-identification answers (EEO, visible minority, disability). Never
  send stored answers to the LLM: only question text goes to the model.
- `services/apply-agent/data/browser-profile/` holds a logged-in LinkedIn
  session. The user handles every login; never automate credentials.
- All `data/`, `.env` and browser profiles are gitignored at the service
  level and again at the root.
- Build in stages: fixtures before live calls, `--dry-run` (fill, never
  submit) before any live application, idempotency before scheduling.

## Tests

**`<TEST_COMMAND>` for this repo is `./scripts/test.sh`** (run from the repo
root). It discovers and runs every `services/*/tests` and `workflows/*/tests`
suite (using the component's `./python` venv if present), the `scripts`
tests, and the `runlog.py` copy check.
