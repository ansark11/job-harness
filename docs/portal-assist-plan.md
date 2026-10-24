# Portal Assist: plan for job-harness

## Summary

**Problem.** 31 of 41 opportunities apply on company portals. Fully automating portals is
unreliable: account sign-ups, login walls, CAPTCHAs, pop-ups on submit, custom widgets and
multi-page flows vary by vendor and by employer.

**Decision.** Split the two channels:

| Channel | Mode |
|---|---|
| LinkedIn Easy Apply | Hands-off, as today (skip rather than guess) |
| Company portals | Prepared by the harness, reviewed and submitted by the user |

**What we're building:**

1. **Coverage scan.** Sample ~100 real portal postings, rank portal vendors by frequency,
   and capture real pages. This decides build order.
2. **Navigation engine.** A page-type loop that gets from a portal's landing page to the
   application form (job description, "Apply manually" choices, pop-ups, new tabs), and
   stops cleanly at sign-in pages and expired postings.
3. **Prepare step.** For public forms: navigate, extract the questions as structured
   fields, and answer them with the existing answer bank, drafter and answerer. Stored for
   review. Never filled or submitted by the agent.
4. **Local API.** Lets the web app and the Chrome extension read and update applications.
5. **Chrome extension.** In the user's own Chrome: guides navigation, fills the form from
   approved answers, flags mismatches and follow-up questions, never clicks Submit, marks
   the application submitted when the portal confirms, and logs every navigation path.
   For login-gated portals it extracts the questions on arrival and shows answers in a
   side panel.
6. **Web app review screens.** List portal applications ready for review, edit answers,
   and an Apply button that opens the portal with the extension ready.

**User flow (public form):** daily search → prepare runs overnight → user reviews answers
in the web app → clicks Apply → portal opens, extension fills → user checks and submits →
extension records it.

**User flow (login-gated, e.g. Workday, Oracle):** web app shows "sign-in needed" → user
clicks Apply, signs in or creates the account (including any email verification) →
extension detects the application form (or the user clicks "Scan this page"), extracts
the questions, fetches answers, shows them in a side panel → user approves, extension
fills → user submits.

**Why this split:** across three 5-posting benchmark runs on Oct 22, 0 portal checks
passed. In the latest run, 3 of 5 postings needed an account or email verification
(Workday x2, Oracle) or had no reachable form. The extension is the main path for
portals; the Playwright navigation and fill engine supports it (preparing answers ahead
of time for public forms, and measuring coverage).

---

## Instructions for Claude Code

Read `CLAUDE.md` first and follow its rules throughout. In particular:

- No cross-service imports. Services talk via CLI + JSON. Glue goes in `workflows/`.
- The model proposes; deterministic code decides.
- Contact details (name, email, phone, LinkedIn URL) never go to the model.
- The user handles every login. Never automate credentials or account creation.
- Fixtures before live calls. Every phase ships with tests. `./scripts/test.sh` must pass.
- `scripts/runlog.py` and `scripts/llm.py` are masters; copy identically where used.
- Log decisions, not just outcomes, through `runlog.py`.
- Also read `~/.claude/plans/i-m-looking-to-build-sleepy-floyd.md` (the original design).
  Where this plan conflicts with it, stop and ask the user.

Work phase by phase. At the end of each phase:
1. Run `./scripts/test.sh`.
2. Summarise what changed.
3. Give the user that phase's **Manual check** steps (each phase has a section), with the
   exact commands and what "correct" looks like.
4. Wait for the user to run the manual check and sign off before starting the next phase.
   If the check fails, fix and repeat it.

### Current state (as of commit `0ac8367`, Oct 22)

**Built:**
- `services/apply-agent/bench.py` + `workflows/portal_bench/`: portal benchmark (resolve
  portal URLs via LinkedIn with a cache, classify ATS, audit forms with a fake persona,
  never submit, score pass/risky/fail/manual/unreachable/unresolved, save page.html,
  aria.txt, filled.png, trace.zip). Realistic fake answers since `6de2eb7`.
- `services/apply-agent/portals.py`:
  - `resolve_apply_url()` clicks through LinkedIn "Continue" dialogs, never returns a
    LinkedIn URL.
  - `dismiss_overlays()` closes cookie banners and pop-ups, not a modal holding the form.
  - `_click_apply_link()` handles new tabs; `APPLY_TEXT` / `_apply_controls()` find
    Apply buttons by visible text.
  - `looks_like_application()` treats a page as an application only with a resume upload
    or name + email fields.
  - Frames are followed only when the page has no form and the frame is hosted by the ATS.
  - `MANUAL = {"workday", "icims", "taleo", "oracle"}` (Oracle: email verification first).
- `services/apply-agent/forms.py`: `settle()` waits for the page; `lost_values()` /
  `refill_lost()` retype fields the site cleared; `combo_select` type with
  `probe_options()` for searchable dropdowns; `_pick_suggestion` no longer picks an
  unrelated suggestion.
- `workflows/apply` still has the portal `fill` + `submit` (approve) path.
- `services/apply-agent/data/bench/resolved.json`: real portal URLs cached.

**Not built:** `services/web-app/` (no folder), any API (tracker is CLI only).

**Benchmark so far:** three runs of `--limit 5` on Oct 22, 0 passes. Latest
(`workflows/portal_bench/data/20261022T193810/`): Equifax company site unreachable (no
form found); AssetMark Workday manual; Goldman Sachs Oracle manual; Engine Greenhouse
(new design) fail with 7 required fields not sticking + CAPTCHA; Mastercard (Phenom site
→ Workday) fail with follow-up fields and its job-search box treated as a form field.
The full set of postings has never been run.

**Still-open engine gaps:** follow-up fields (portals read once); multi-page portals;
success detection is a text regex; site search boxes can be mistaken for form fields.

---

## Phase 0: Make the benchmark trustworthy, then run it fully

The coverage scan (Phase 1) ranks problems by what the benchmark reports, so the
benchmark must not miscount first.

1. Run `./scripts/test.sh`. Fix anything that fails.
2. **Fix measurement false negatives** in `bench.py verify()`, checked against
   `filled.png` and `trace.zip` from `workflows/portal_bench/data/20261022T193810/`:
   - Phone/number fields: compare digits only (Engine reformatted `4165550123` to
     `(416) 555-0123` and it was counted as "did not stick").
   - Engine fields reported as "now holds None" right after filling (Location, School,
     Degree, Country): open the trace and confirm whether the value really was lost or
     the read-back looked at the wrong element. Fix whichever is wrong (engine or bench).
   - The three `TimeoutError: Locator.fill` crashes on unlabeled Engine fields: find what
     those elements are (likely hidden inputs behind custom widgets) and skip or handle them.
3. **Exclude site search from forms:** Mastercard's "Search job title" box was extracted
   as an application field. Ignore inputs inside `role=search`, `header`, `nav`, or with
   search-like labels/placeholders, in both `forms.extract_fields` and the bench census.
4. Add tests for each fix (fixtures reproducing the reformatted phone, the search box).
5. Run the **full** benchmark on all portal opportunities (no `--limit`). Share
   `report.md` with the user.

**Done when:** tests pass, every failure reason in the full report reflects a real engine
problem (spot-check 5 against screenshots), and the user has the report.

**Manual check (user, ~15 min):** open `report.md` and 5 postings' `filled.png`. For each
"did not stick" or "not found" item, confirm on the screenshot that the field really is
empty or missing. Confirm no LinkedIn URL appears as a portal.

---

## Phase 1: Coverage scan

**Goal:** know which portal vendors and navigation paths are most common in the user's
market, and capture real pages for each.

1. **Broad criteria file.** Add `criteria.scan.example.json`: PM roles of any seniority,
   Toronto plus 3-4 US cities, any industry, no compensation floor. The user copies it to
   `criteria.scan.json` (gitignored like `criteria.json`).
2. **Scan workflow.** Add `--criteria` and `--sample N` (default 100) to
   `workflows/portal_bench/run.py`. When `--criteria` is given, run `job-search` with that
   file instead of reading the tracker, keep only external-apply postings, and take a random
   sample of N. Do not ingest these into the tracker (they aren't real matches).
3. **Resolve politely.** Keep the 6-14 s pause. Add `--max-resolve N` (default 100) so one
   run never clicks LinkedIn more than that. Cache as today.
4. **Record HAR.** In `bench.py run_targets`, create each audit context with
   `record_har_path=<target dir>/page.har` and `record_har_content="embed"`. Exclude
   images/fonts/media to keep files small.
5. **Group the results.** Add `bench.py groups <run dir>`: group targets by ATS family,
   then by landing-page type (form on landing page / job description with Apply /
   embedded iframe / sign-in required / other). Output a table: group, count, share of
   total, pass rate, 3 example URLs. Add a "new vendors per 20 postings" line to show
   saturation.
6. **Report to the user** with a recommended build order (top groups by count).

**Tests:** grouping logic on synthetic results; HAR file is written for a fixture target.

**Done when:** the user has a frequency-ranked list of portal groups and real captures
(page.html, aria.txt, page.har, trace.zip) for the top groups.

**Manual check (user, ~15 min):** open 5 postings from different groups in Chrome.
Confirm each one's vendor and landing-page type (form / description / embedded /
sign-in) match what the grouping says.

**User step after this phase:** for portals the scan marks manual or unreachable (likely
Workday, iCIMS, Taleo, SuccessFactors, Oracle), the user records 5-8 navigation paths with
Chrome DevTools Recorder (export as JSON) into
`services/apply-agent/tests/recordings/<vendor>-<n>.json`. Ask the user to do this before
Phase 2 ends.

---

## Phase 2: Navigation engine (`services/apply-agent/navigate.py`)

**Goal:** from any portal URL, reach the application form or stop with a clear reason.

**Build on what exists.** `portals.py` already has a partial classifier and navigation:
`looks_like_application()`, `_apply_controls()`, `APPLY_TEXT`, `_click_apply_link()`,
`_follow_embed()`, `dismiss_overlays()`, `MANUAL` / `MANUAL_WHY`. Move these into
`navigate.py` and extend them; don't write parallel versions. Keep `PortalWalk.open()`
behaviour passing its existing tests while refactoring.

1. **Page classifier** `classify_page(page) -> {"type", "signals", "candidates"}`.
   Deterministic signals only (reuse `looks_like_application()` for `form`):

   | Type | Signals |
   |---|---|
   | `form` | 3+ fillable fields, or a file input + name/email fields, and a submit-like button |
   | `description` | apply-like button or link (`Apply`, `Apply now`, `I'm interested`, `Apply for this job`), few or no fields |
   | `apply_choice` | buttons like `Apply manually`, `Autofill with resume`, `Use my last application`, `Apply with LinkedIn` |
   | `login` | a password input, or `Sign in` / `Create account` / `Forgot password` with no form fields |
   | `closed` | text like `no longer accepting`, `job not found`, `position has been filled`, HTTP 404/410 |
   | `unknown` | none of the above |

   `candidates`: up to 25 visible buttons/links with text, aria-label, href, and an index.
2. **Vendor shortcuts** (checked before the loop), from Phase 1 data. Start with: Lever
   `/apply`, Ashby `/application`, Greenhouse embed `?for=` / `#app`, Workday
   `Apply` → `Apply Manually`, SmartRecruiters `I'm interested`. Add others the scan shows
   are common.
3. **Loop** `navigate(page, url, max_steps=6) -> {"status", "path", "form_url"}`:
   dismiss overlays → classify → act → repeat.
   - `form` → stop, `status: form`.
   - `description` → click the best apply candidate (handle new tabs).
   - `apply_choice` → prefer `Apply manually`.
   - `login` → stop, `status: login_required` (never type credentials).
   - `closed` → stop, `status: closed`.
   - `unknown` → model fallback (below), else stop `status: stuck`.
   - Stop with `status: loop` if the same URL + page type repeats.
4. **Guard rails.** Never click text matching `submit|sign in|log in|create account|register|pay|purchase|delete`.
   Never type into password fields. Step limit 6. Total time limit 90 s.
5. **Model fallback for `unknown`.** Copy `scripts/llm.py` into apply-agent. Send only the
   numbered candidate list (text, aria-label, href path) and the page title. Prompt: which
   one index leads toward the job application form, or `none`. Code checks the returned
   index exists and isn't a guarded action, clicks it, and requires the next
   classification to be closer to `form` (description/apply_choice/form). Otherwise stop
   `stuck`. Injectable `post` for tests.
6. **Path record.** Every step appends
   `{"url", "type", "action", "clicked_text", "signals"}`. Returned in the report and logged.
7. **Wire it in.** `portals.PortalWalk.open()` uses `navigate()`. `bench.py` reports
   `nav_status` and `nav_path` per target and adds `nav_status` counts per group.

**Tests:** one fixture per page type; a two-hop flow (description → apply_choice → form);
a login stop; a closed posting; a loop stop; the guard rail refusing a "Sign in" link;
model fallback with canned replies (valid index, invalid index, `none`). Convert the
user's Recorder JSON files into test cases where feasible (assert our loop reaches the
same final page type, using saved HAR or page.html).

**Done when:** benchmark navigation reach rate (form reached or correctly stopped at
login/closed) is reported per vendor, and the top vendors from Phase 1 reach the form.

**Manual check (user, ~20 min):** run the benchmark with `--watch --limit 5` on 5 real
portals from different groups (include Equifax, which failed with "no form found").
Watch each one; confirm it reaches the form or stops at the right page (sign-in, closed),
never clicks a sign-in or submit control, and the reported `nav_path` matches what you saw.

---

## Phase 3: Real fixtures and offline replay

1. Promote 1-2 captures per top vendor from Phase 1 into
   `services/apply-agent/tests/fixtures/real/<vendor>/` (page.html + page.har + a
   `expected.json` listing the fields a person sees: label, type, required).
   **Scrub before committing:** no tracking tokens in URLs, nothing from the user.
2. Tests:
   - Extraction coverage on saved `page.html` (JS disabled): engine finds every field in
     `expected.json`.
   - Live-behaviour replay with `page.route_from_har(page.har, not_found="abort")`:
     navigation reaches the form and follow-up fields appear after answers.
3. Fix the `forms.py` gaps that these fixtures expose. Known open ones: re-scan for
   follow-up fields until stable (portals are read once today); div-based dropdowns that
   `combo_select` / `probe_options` still miss; read-back for every field type (extend
   `lost_values()`). Include the Engine (new Greenhouse design) form as one of the
   fixtures, since it failed 7 required fields live.

**Done when:** each top vendor has a real fixture with passing extraction and replay tests.

**Manual check (user, ~10 min):** for 2 fixtures, open the saved `page.html` next to the
real live page and confirm `expected.json` lists every question a person would answer,
with the right required markers.

---

## Phase 4: Prepare step (portals stop at "ready for review")

**Goal:** for public portal forms, produce a complete set of answers for the user to review.
The agent never fills or submits a portal.

1. **apply-agent:** new mode `apply.py packet.json --mode prepare`: navigate, extract all
   fields (including after-answer follow-ups where detectable without filling), return
   `{"status": "form"|"login_required"|"closed"|"stuck", "form_url", "nav_path",
   "fields": [...], "form_fingerprint"}`. `form_fingerprint` = hash of sorted
   (normalized label, type, options) so later changes are detectable.
2. **tracker:** store per application: `form_url`, `nav_path` (JSON), `form_fingerprint`,
   `channel` (`easy_apply` | `portal`), and a new status `ready_for_review`. Add migration
   + CLI commands. Fields become application items, as today.
3. **workflows/apply:** for portal applications, call `--mode prepare` instead of
   fill/submit, then resolve every field with the existing logic (bank, aliases, drafter,
   answerer, decline for self-ID, contact details from the bank only). End in
   `ready_for_review`, or `needs_signin` when the portal needs an account (no questions
   yet). Remove the old portal `submit` command path or mark it deprecated.
4. **workflows/daily_search** (optional, ask the user): after ingest, run prepare for
   newly kept portal opportunities.

**Tests:** workflow test with a scripted prepare report; tracker migration and status
transitions; fingerprint stability.

**Done when:** `run.py start <portal opportunity>` ends in `ready_for_review` with every
answer stored and nothing typed into the portal.

**Manual check (user, ~20 min):** run prepare on 2 real public-form portal postings (e.g.
a Greenhouse and an Ashby one) with `--watch`. Confirm nothing is typed into the portal.
Then `run.py status <id>`: read every drafted answer; confirm contact fields come from
the bank, self-ID answers are your saved answers or "decline", and nothing is invented.
Run it on one Workday posting and confirm it ends in `needs_signin` with no questions.

---

## Phase 5: Local API

The extension and web app need HTTP.

**Check the original design first.** `CLAUDE.md` says the web app sits on "tracker's API",
and `services/tracker/store.py` says "The CLI (and later the API) are thin layers on these
functions". Read `~/.claude/plans/i-m-looking-to-build-sleepy-floyd.md` for the intended
API design. Proposed split, to confirm with the user:

- **Tracker API** (`services/tracker/api.py`): thin HTTP layer over `store.py`,
  `applications.py`, `bank.py`. Reads and writes data only. Serves the web app and most
  extension calls.
- **Answer generation** needs the drafter and answerer (other services), so it can't live
  in the tracker. Put the one endpoint that generates answers for newly found fields in a
  workflow (e.g. `workflows/apply` gains a `serve-fields` command, or a small
  `workflows/assist_api/`), calling services by CLI as today.

Stdlib `http.server` unless the user prefers a framework.

- Bind to `127.0.0.1` only. Require a token header (generated once, stored gitignored,
  pasted into the extension options). CORS: allow only the extension's origin and the
  web app's dev origin.
- Endpoints:
  - `GET /applications?status=ready_for_review|needs_signin`
  - `GET /applications/{id}`: job, form_url, items (label, key, type, options, value,
    state), fingerprint
  - `PATCH /applications/{id}/items`: user edits/approvals
  - `POST /applications/{id}/fields`: extension sends fields extracted on arrival
    (login-gated case or changed form); API resolves new ones via the existing workflow
    logic and returns answers
  - `POST /applications/{id}/events`: `filled`, `field_mismatch`, `followup_found`,
    `submitted` (with confirmation text), `nav_path`
  - `GET /match?url=`: find an application by portal URL (fallback when the `#jh=` tag
    is lost on redirect)
- Never return contact fields to anything except the extension (they're needed to fill),
  and never log them.

**Tests:** each endpoint against a temp tracker DB; auth rejected without token; CORS.

**Manual check (user, ~10 min):** start the API; open `/applications` in a browser with
and without the token (no token must be refused); confirm a `ready_for_review`
application from Phase 4 shows every answer; edit one via `curl` and confirm
`tracker.py application show` reflects it. Confirm the server isn't reachable from
another device on your network.

---

## Phase 6: Chrome extension (`extension/`, Manifest V3)

**Structure:** `manifest.json`, `background.js` (service worker: API calls, state),
`content.js` (all_frames: true, on the user's job portals), `sidepanel.html/js`,
`options.html` (API URL + token). No remote code. Minimal permissions:
`storage`, `sidePanel`, `activeTab`, `scripting`, host permission for `http://127.0.0.1/*`
plus portal hosts (request optional host permissions per site rather than `<all_urls>` if
practical).

**Shared extraction code:** move `EXTRACT_JS` and `BUTTON_CHOICES_JS` from `forms.py` into
`services/apply-agent/extract.js`; `forms.py` reads the file. The extension keeps an
identical copy at `extension/extract.js`; add a drift check to `scripts/test.sh` (same
pattern as runlog copies).

**Logic to port from Python to JavaScript** (it lives in Python today, so it can't be
copied as-is). Keep the same behaviour and test both against the same fixtures:
- `forms.py`: `normalize`, `clean_label`, `match_option` (incl. decline matching),
  `combo_select` detection and `probe_options`, `fill_field` per type, `set_choice`
  (hidden radios), `settle`, `lost_values` / `refill_lost`, `_pick_suggestion`
  (never pick an unrelated suggestion).
- `portals.py` / `navigate.py`: `looks_like_application`, `classify_page` signals,
  `dismiss_overlays` (with the "don't close a modal holding the form" rule), apply-button
  matching.

**Behaviour:**

1. **Finding the application.** The web app's Apply link opens `form_url#jh=<id>`. The
   content script reads the tag, stores the id in `chrome.storage.session` keyed by tab, and
   keeps it across redirects and pages. Fallback: `GET /match?url=`.
2. **Navigation help.** On non-form pages, classify the page; on `description` or
   `apply_choice`, highlight the button to click (do not auto-click by default; make
   auto-click an option). On `login`, show "Sign in or create your account, then continue".
3. **On the form:**
   - Extract fields; compute the fingerprint; match fields to saved items by normalized
     label + type (+ name attribute).
   - Fill matched, approved items:
     - text/textarea: native value setter + `input` and `change` events, then read back;
     - select/radio/checkbox: set and dispatch events, read back;
     - custom combobox: click to open, wait for `role=option`, click the match, read back;
     - file: build a `File` from the resume (fetched from the API) and attach via
       `DataTransfer`, dispatch `change`;
     - contact fields from the API (never from the model).
   - Highlight: green = filled and verified; amber = no saved answer or changed field;
     red = fill didn't stick.
   - Re-scan after filling; new follow-up fields are sent to `POST /fields`, answers shown
     in the side panel for approval before filling.
   - **Never click Submit.**
4. **Login-gated forms** (`needs_signin`): this is a hand-off, not a pause. The prepare
   step has already ended; the extension takes over in the user's Chrome.
   - On `login` pages (password field, sign in/up, verify email): stay quiet, never fill,
     show "Sign in or create your account, then continue".
   - Detect arrival at the form with the `form` classifier signals (not "login
     succeeded"), re-checking on every page load and DOM change.
   - **"Scan this page" button** in the side panel: the user's manual fallback when
     detection misses. Treats the current page as the application form.
   - **Workday-style profile steps** after sign-up ("My Information", "My Experience",
     "Application Questions", "Voluntary Disclosures") are part of the application: treat
     each as a form page and fill it.
   - **Application picker:** if the extension can't tell which application this tab is
     (the `#jh=` tag was lost, e.g. an email-verification link opened a new tab, as with
     Oracle), show recent `needs_signin` / `ready_for_review` applications for the user to
     pick, after trying `GET /match?url=`.
   - Then send the extracted fields to `POST /fields`, show answers in the side panel,
     fill after the user approves.
5. **Multi-page forms:** keep progress per tab in `chrome.storage.session`; repeat step 3
   on each page.
6. **Submission:** detect confirmation (URL change + success text, or the form
   disappearing after the user's submit click); post `submitted`; the tracker sets status
   `submitted`.
7. **Path logging:** every page type and user click (button text and the next page type)
   goes to `POST /events` as `nav_path`. Phase 2 rules are reviewed against this log
   periodically.
8. **Fallback for stubborn fields (later, optional):** `chrome.debugger` with
   `Input.insertText` / `DOM.setFileInputFiles` for fields that reject untrusted events.
   Off by default; Chrome shows a debugging banner while attached.

**Tests:** run the content-script logic in Playwright against the Phase 3 real fixtures
and the existing stand-in fixtures (load `extract.js` + fill code via `add_init_script`
or `page.evaluate`); verify every fill reads back. A manual test checklist for loading the
unpacked extension in Chrome.

**Done when:** on each top-vendor fixture, the extension fills all matched fields with
verified read-back, flags unmatched ones, and never submits.

**Manual check (user, ~45 min, split over two sessions if needed):** load the unpacked
extension in Chrome.
1. Greenhouse and Ashby postings that are `ready_for_review`: click Apply from the API/web
   app link; confirm every field fills, each value is visible on screen, colours match
   (green/amber/red), follow-up questions appear in the side panel, and Submit is never
   clicked. Submit one real application yourself and confirm the tracker marks it
   submitted.
2. One Workday posting with a new account: confirm the extension stays quiet during
   sign-up and email verification, detects the first profile page, and fills it.
3. Close the tab mid-application and reopen the portal URL directly: confirm the
   application picker appears and resumes the right application.
4. Click "Scan this page" on a form it didn't detect.

---

## Phase 7: Web app review screens

Per `CLAUDE.md`, the UI comes after the user signs off, starting with several mockup
options. Do that first; do not build before the user picks.

Screens needed:
- **Portal queue:** applications in `ready_for_review` and `needs_signin`, with company,
  role, vendor, number of answers needing attention.
- **Review:** every question with its answer, source (bank / model / drafted / decline)
  and confidence; edit and approve; regenerate a drafted answer.
- **Apply button:** opens `form_url#jh=<id>`.
- **Status:** submitted, in progress, needs sign-in.

**Manual check (user, ~15 min):** review one `ready_for_review` application end to end in
the web app: edit an answer, regenerate a drafted one, click Apply, and confirm the
extension uses the edited answers.

---

## Success measures (report after each phase)

- Coverage scan: share of postings in the top 5 vendors.
- Navigation: % of postings reaching the form or correctly stopping at login/closed, per vendor.
- Extraction: % of fields in `expected.json` found, per vendor fixture.
- Fill (extension): % of filled fields verified by read-back.
- User effort: minutes from clicking Apply to ready-to-submit (user reports a few).

## Decisions to confirm with the user (ask, don't assume)

1. Auto-run prepare after the daily search, or only on demand?
2. Extension navigation: highlight buttons only, or auto-click on `description` and
   `apply_choice` pages?
3. Local API: confirm the split (tracker owns the data API; answer generation in a
   workflow) against the original design file, and stdlib `http.server` vs. a small
   framework (FastAPI/Flask).
4. Should the web app also be able to open Easy Apply jobs, or portals only?
