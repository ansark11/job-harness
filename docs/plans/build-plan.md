# job-harness: LinkedIn search, match review and auto-apply

## Context

This is the first part of a job-search harness, specified in `Harnesses/opportunity-search.md`. Each day it should:

1. find senior and principal PM roles on LinkedIn that fit your criteria;
2. show them in a LinkedIn-style web app where you keep or discard each one;
3. apply to the kept roles with the resume you choose. Easy Apply runs end to end. External portals are filled for you to approve, and new long-form or profile questions come back to you.

Mock-interview generation comes later. The Mock interview CTA exists now but stays disabled.

**Decisions made**
- LLM: OpenAI Responses API with `gpt-6-luna`, called through stdlib `urllib`, the same as `finance-harness/services/card-offers/discover.py`.
- Easy Apply submits fully automatically.
- Web app: FastAPI + React (Vite, TypeScript).
- New repo at `Harnesses/job-harness/`.

**Conventions carried over from finance-harness** (see its `CLAUDE.md`):
- Independent services that talk only through CLI calls and JSON. No cross-service imports.
- Glue code lives in `workflows/`.
- Each service owns its `data/`, `logs/`, tests and dependencies. Stdlib first.
- `runlog.py` structured logging is copied identically into every component, and `scripts/trace.py` reads it.
- "The model proposes; deterministic code decides."
- Fixture tests come before any live call.
- You handle every login.
- `scripts/test.sh` is `<TEST_COMMAND>`.

## Repo layout

```
job-harness/
  CLAUDE.md  README.md  .gitignore  criteria.json
  scripts/test.sh  scripts/trace.py            # trace.py copied from finance-harness
  services/
    job-search/      LinkedIn guest search → normalized postings JSON (stdlib)
    matcher/         hard filters + LLM fact extraction + fit score → matches JSON (stdlib)
    answer-drafter/  draft long-form answers; map new questions to answer-bank keys (stdlib)
    apply-agent/     Playwright: Easy Apply + external portals (own venv)
    tracker/         owns jobs.db, resumes, answer bank, applications; CLI now, FastAPI API later (own venv)
    web-app/         React (Vite) UI over tracker's API (built last, after UI options are chosen)
  workflows/
    daily_search/    job-search → matcher → web-app ingest
    apply/           worker: web-app queue → drafter/bank → apply-agent → web-app
  launchd/           LaunchAgent plists (daily search, server + apply worker)
```

- `runlog.py` is copied from `finance-harness/services/spend-tracker/runlog.py`. One change: add `cookie|li_at|session` to `SECRET_KEY` so LinkedIn session values are never logged.
- `.gitignore` is the finance-harness root file plus `services/*/data/browser-profile/`, `node_modules/` and `dist/`.

## Services

### 1. job-search (`search.py --since <iso> [--max-pages N]`)
- Uses LinkedIn's public guest endpoints, which need no login, so searching never puts your account at risk:
  - `jobs-guest/jobs/api/seeMoreJobPostings/search` returns result cards.
  - `jobs-guest/jobs/api/jobPosting/{id}` returns the description and the criteria list: seniority, employment type, job function and **Industries**.
- Query parameters:
  - keywords: one query per title (Senior / Principal / Lead / Staff / Group Product Manager);
  - `geoId`: Toronto, Vancouver and United States. Confirm the IDs while building and keep them in `criteria.json`;
  - `f_WT=2,3` (remote, hybrid), `f_JT=F,C` (full-time, contract), `f_E=4,5`;
  - `f_TPR`: seconds since the last successful run, at least 24h and at most 30 days.
- Parses the HTML with `html.parser` and outputs `{id, title, company, location, posted_at, url, apply_type (easy|external|unknown), apply_url, criteria{}, description, salary_text}`.
- Throttles to 2–4 s between requests, backs off on 429, and caps the number of pages. Drops anything posted 30 or more days ago.
- Tests run against saved HTML fixtures.

### 2. matcher (`match.py postings.json --criteria criteria.json --resume resume.txt`)
- **Deterministic filters** run first, using `criteria.json`:
  - title regex for seniority;
  - location;
  - recency;
  - industries excluded through LinkedIn's Industries field (CPG, Retail).
- **LLM extraction** (gpt-6-luna, JSON output). The model reports facts and the code decides:
  - comp min/max and currency;
  - contract length in months;
  - work mode;
  - industry category (fintech/payments, big tech, health tech, e-commerce platform, CPG, retailer, other). The prompt tells the model that a platform such as Shopify counts as e-commerce, while Walmart or Loblaw count as retailers.
- **Code then applies the rules:**
  - comp ≥ 225K CAD when listed. USD is converted at the FX rate in `criteria.json`, and a borderline tolerance is set there too.
  - contract ≥ 12 months;
  - the industry is on the allowlist, plus a Big Tech company allowlist.
- **Fit score** (0–100) compares the role with your default resume and gives 2–3 reasons. Output records the matches plus the rejections with their reasons, logged per finance-harness's "log decisions" rule.
- Tests use canned model replies through an injected `post`, the same pattern as `discover.py`.

### 3. answer-drafter (`draft.py` and `map_question.py`)
- `draft.py --question --job job.json --resume resume.txt [--steer "..."] [--examples approved.json]` writes a long-form draft with a character limit and reports which resume points it used.
- `map_question.py --question "..." --keys keys.json` proposes which answer-bank key a new question matches, with a confidence. **The model only ever sees question text, never your stored answers.** Self-ID answers never go to OpenAI.

### 4. apply-agent (Playwright, own venv)
- **Login:** `login.py` opens a headed Chromium with a persistent profile at `data/browser-profile/`, and you sign in to LinkedIn yourself.
- **Commands:** `apply.py packet.json --mode fill|submit`. The packet includes:
  - the job;
  - the resume file path;
  - contact fields;
  - the resolved answer bank;
  - approved drafts.
- **Easy Apply:** opens the job, then walks each modal step:
  1. extract the fields (label, type, options, required);
  2. resolve them deterministically from the packet;
  3. fill them and click Next;
  4. submit at the Review step.

  If any field is unresolved, or a long-form answer is new and unapproved, the agent stops and returns `needs_input`. Otherwise it submits with no confirmation, as you chose.
- **External portals:** detected from the URL.
  - Greenhouse, Lever and Ashby get dedicated adapters. They cover most fintech and tech postings.
  - Other portals get a generic DOM extractor plus LLM field mapping.
  - Workday, any portal that requires creating an account, and CAPTCHAs return `needs_user` with a prefilled answer sheet.
  - `fill` returns a summary of every field, its value and its source, plus page screenshots. `submit` refills from the frozen, approved values, verifies them, and then submits. This avoids keeping a browser open while you review.
- **Safeguards:**
  - a daily application cap (default 10) and random delays;
  - headed mode by default;
  - `--dry-run` (fill only, never submit) is the gate before live use.

### 5. tracker (SQLite `jobs.db`; CLI first, FastAPI later)
All of the state and behavior lives here, so every feature works and is tested from the terminal before any UI exists.

- **CLI (`tracker.py`), used for verification before the UI exists:**
  - `ingest`;
  - `list [--status new --group-by fetch-date]`;
  - `show <id>`;
  - `keep <id>` and `discard <id>`;
  - `expire`;
  - `resume add|list`;
  - `bank set|list`;
  - `inbox` (questions waiting for your answer);
  - `apply <id> --resume <rid>`;
  - `application show|approve|edit|regenerate|submit`.

  Every command prints JSON, so the workflows can call it as well.
- **Tables:**
  - `opportunities`: status new/kept/discarded/expired, plus `fetched_at`;
  - `runs`;
  - `resumes`: file + extracted text, using pypdf / python-docx;
  - `answer_bank`: key, answer, aliases[], jurisdiction, always_ask;
  - `approved_answers`;
  - `applications`: status queued/filling/needs_input/awaiting_approval/submitted/failed, plus items[].
- **Behavior (built and tested headless):**
  - ingest is idempotent on the LinkedIn id;
  - expiry: unreviewed items more than 7 days old are marked `expired`;
  - the answer bank resolves a question by exact text or alias, then by a mapping the drafter proposes and you confirm once;
  - the approved-answer library.
- **API** (added in the UI phase as a thin layer over the same functions as the CLI):
  - `POST /api/ingest`;
  - `GET /api/runs/last-success`;
  - `GET/POST /api/apply-queue`;
  - plus CRUD for the UI.

### 6. web-app (React + Vite; built last)
**UI options come first.** Before any React code, I'll generate 3–4 distinct, clickable HTML mockups. Each one covers the three key screens (Review, Kept, Application review) with realistic sample data. You give direction on which to use or combine, and only then is the real web app built. The functional requirements below apply to every option.

- **Review page (Step 1):**
  - The left pane lists cards grouped by fetch date ("Today", "Oct 9 · expires in 5 days").
  - The right pane shows details: title, company, location, mode, comp, posted time, fit score and reasons, the full description, and a LinkedIn link.
  - Keep and Discard buttons, with K / D / J keyboard shortcuts.
- **Kept page (Step 2):**
  - A thumbnail grid with the company logo, title and a status chip.
  - Each card has an **Apply** button and a **Mock interview** button. Mock interview stays disabled until the status is `submitted`, and shows "coming soon" for now.
  - Apply opens a resume picker that lists every version plus an upload option.
- **Application review screen** (my recommendation for long-form answers):
  - A checklist layout. On the left is every application item with a state: auto-filled ✓ / needs review / needs your answer.
  - On the right is the selected item. For a long-form question it shows the question, a character counter, the draft and "resume points used", with three actions:
    - **Approve**;
    - **Edit**, using an inline textarea;
    - **Regenerate**, with an optional one-line steer.
  - Submit is enabled only once every item is approved.
  - Approved answers go into a library and are offered as starting points when a similar question comes up again.
- **Profile / answer bank** (my recommendation for self-ID questions):
  - A one-time onboarding form with grouped canonical questions:
    - work authorization and sponsorship (CA and US);
    - relocation;
    - notice period;
    - salary expectation;
    - US EEO: gender, race/ethnicity, veteran status, disability;
    - Canadian employment equity: visible minority, Indigenous identity, disability, gender.
  - Every question includes "Prefer not to say", an "always ask me" toggle, and a jurisdiction scope.
  - When a portal asks something new, its exact text lands in a **"Needs your answer" inbox**, with the mapping the drafter proposes. You confirm or answer once. The question text is saved as an alias, so the next time it matches deterministically.
  - Stored only locally in `tracker/data/`, which is gitignored.

## Workflows
- **daily_search/run.py:**
  1. Run guard: exit if a successful run already happened since the most recent 19:00.
  2. Get the last success time.
  3. job-search, then matcher, then `tracker.py ingest`.
  4. macOS notification ("12 new matches") through `osascript`.
- **apply/run.py `<application_id>`:** run on demand, and triggered by `tracker.py apply`. It:
  1. builds the packet from the resume, bank and approved drafts;
  2. runs apply-agent `fill`;
  3. drafts long-form answers and maps new questions;
  4. writes the items back to tracker.

  Easy Apply with nothing pending submits automatically. External portals wait for `tracker.py application submit` (later, the Approve button), then run `submit`. In the UI phase, a `worker.py` poller of `/api/apply-queue` wraps the same `run.py` logic.
- **Scheduling (`launchd/`):**
  - The daily plist uses `StartCalendarInterval` (19:00). If the Mac was asleep at 7pm, launchd runs the missed job on wake.
  - `RunAtLoad` is also set to cover a full shutdown. The run guard prevents a double run.
  - A second plist keeps the server and apply worker alive (KeepAlive) on `localhost:8000`.

## Build order (each step on its own branch, merged per your agents.md)
1. Scaffold the repo:
   - CLAUDE.md with the boundary rules, `.gitignore`, `runlog.py`, `trace.py` and `test.sh`;
   - `git init` and creating the GitHub repo through the `safe-repo-publish` skill. I'll confirm the repo name and visibility with you then.
**Phase A: all functionality, verified headless (no UI)**

2. job-search with fixture tests, then one live smoke run.
3. matcher with canned-reply tests.
4. tracker core: DB, `ingest`, `list` grouped by fetch date, `keep`/`discard`, `expire`.
5. The daily_search workflow + launchd.
6. tracker: resumes, the answer bank + inbox, the approved-answer library. Plus answer-drafter.
7. apply-agent Easy Apply + the apply workflow: login, then `--dry-run`, then live.
8. External portals: Greenhouse, Lever, Ashby, then generic + the CLI approval flow.

**Checkpoint:** you run the whole loop from the terminal and confirm that it works correctly.

**Phase B: UI**

9. Generate 3–4 distinct UI mockup options as clickable HTML pages for the Review, Kept and Application review screens. You choose a direction or give guidance.
10. Add the FastAPI layer to tracker, then build the React web-app in the chosen direction, plus the apply-queue worker and the server plist.

## Verification
- `./scripts/test.sh` runs every Python suite (fixtures and canned model replies, no network) and checks that the `runlog.py` copies match. In Phase B it also runs `npm test` (vitest).
- Live search: `python3 services/job-search/search.py --since <24h ago> --max-pages 1`. Spot-check the results against LinkedIn in a browser.
- End to end, Phase A:
  1. Run `workflows/daily_search/run.py` by hand.
  2. `tracker.py list --status new --group-by fetch-date`, then `keep` and `discard`. Backdate a row and run `expire` to confirm the 7-day rule.
  3. Check `python3 scripts/trace.py --last` for logged filter and score decisions.
- Apply, Phase A:
  1. `--dry-run` on a real Easy Apply posting and a real Greenhouse posting.
  2. Check `application show`, approve/edit/regenerate, the `inbox`, and that a second application reuses the answer through its alias.
  3. Then one live submission to a role you actually want.
- Scheduler: `launchctl kickstart` the daily job twice and confirm that the guard skips the second run.
- Phase B: click through each screen against the same data. All CLI-verified behavior must match in the UI.

## Risks to know
- **Account risk:** automated Easy Apply is against LinkedIn's terms, and your account could be restricted. Searching uses the logged-out endpoints and so carries no account risk. The application cap and delays reduce the risk but don't remove it.
- **Endpoint changes:** LinkedIn can change or block the guest endpoints at any time. The parser is covered by fixtures, so a break shows up quickly. If it happens, a JSearch or SerpAPI adapter can replace job-search behind the same output schema.
- **Workday portals:** these stay manual (prefilled answer sheet) at first.
