# Experiment: can a browser agent handle the portals the rules engine can't?

## Why

After Phase 0 (full benchmark, 2026-10-24, `workflows/portal_bench/data/20261024T175043/`),
32 postings: 10 closed, 4 pass, 3 risky, 5 fail, 5 unreachable (no form found), 5 manual
(account or email verification). About 1 in 3 live postings works after two days of
per-site fixes. Each fix handled one site; the next site broke differently.

**Question:** does an LLM browser agent (observe the page, act, check, repeat) reach and
fill these forms well enough to replace the rules-based navigation and fill engine
(plan Phases 2-3)? Answer it in one or two evenings before changing the main plan.

**Scope:** the agent decides *how to operate the page*. It does not decide what the
answers are (it gets a fixed answer packet) and it never submits.

## Rules for Claude Code

- Read `CLAUDE.md`. This is an experiment: keep it isolated, don't change service
  behaviour, don't touch `docs/portal-assist-plan.md`.
- **Fake persona only.** Never use the user's resume, answer bank or contact details.
- **Never submit, never sign in, never create an account.** Enforced in code (below),
  not only in the prompt. The safety test must pass before any live run.
- Do not use LinkedIn. Use the portal URLs already cached in
  `services/apply-agent/data/bench/resolved.json` and the latest `results.json`.
- Check current library docs (Context7 or the web) before writing code against
  browser-use or Playwright APIs; they change often.
- Stop and report to the user at the end of each step marked **(stop)**.

## Location

`services/apply-agent/experiments/agent_fill/`, with its own venv at `./python` and its
own `requirements.txt` (browser-use and its dependencies must not touch the apply-agent
venv). It may import `bench.py`, `forms.py`, `portals.py` from `services/apply-agent/`
by adding that folder to `sys.path` (read-only reuse, acceptable for an experiment).
Gitignore `python/` and `data/`. Output to `experiments/agent_fill/data/<timestamp>/`.

---

## Step 0: Fix the CAPTCHA false positive in the benchmark (small, do first)

7 postings (6 Greenhouse, 1 JazzHR) were flagged "CAPTCHA present". Engine's saved
`page.html` shows reCAPTCHA Enterprise in **invisible** mode (`RECAPTCHA_INVISIBLE_KEY`,
`enterprise.js?render=<key>`, `recaptcha-badge`). The visible iframe is the "protected
by reCAPTCHA" badge, not a challenge. Invisible reCAPTCHA only runs on submit, and the
benchmark never submits.

- In `bench.py`, split `captcha` into:
  - `captcha_challenge`: a visible challenge (reCAPTCHA `bframe` iframe or checkbox
    `anchor` without `size=invisible`, a visible hCaptcha checkbox, a Cloudflare
    Turnstile/challenge widget).
  - `captcha_on_submit`: invisible reCAPTCHA/badge present (informational only).
- Only `captcha_challenge` counts toward a verdict reason. Report `captcha_on_submit`
  as a note.
- Test with two fixtures (badge only → no reason; visible challenge → reason).
- Commit separately from the experiment.

## Step 1: Test set

Build `targets.json` from the latest `results.json`:
- every live posting with verdict `fail` or `unreachable` (about 10),
- 2-3 `pass` postings as controls (the agent should match the rules engine there),
- skip `closed` and `manual` ones.

Re-check each URL is still open (load it; skip if the posting closed) and report the
final list. **(stop)**

## Step 2: Fake answer packet

`experiments/agent_fill/persona.json`: the same fake person as `bench.PERSONA`, plus
answers for common screening questions (work authorization in Canada and US: yes/no as
appropriate for a Toronto-based fake person; sponsorship; years of experience 8;
salary expectation; notice period; how did you hear: LinkedIn; self-ID: "prefer not to
say"; a short generic "why this role" paragraph; the bench's placeholder PDF as resume).

Rule given to the agent: use only packet values. If a question isn't covered, choose a
"prefer not to say / decline" option if one exists, otherwise leave it blank, and list it
in its final report as `unanswered`.

## Step 3: Safety harness (must pass before any live run)

1. **Network blocker:** reuse `bench.Blocker` (or equivalent) on the browser the agent
   drives, armed for the whole run: abort every non-GET request except autocomplete /
   search lookups and GraphQL *queries*. Log each blocked request. A blocked request
   whose URL or body looks like a submission = `attempted_submit`.
2. **Action guard:** if the agent framework allows custom actions or action filters,
   refuse clicks on elements whose text or aria-label matches
   `submit|send application|sign in|log in|create account|register|pay` and refuse
   typing into `type=password`. Log refusals. If the framework can't do this, rely on
   the network blocker and record that limitation.
3. **Limits:** max 40 agent steps and 5 minutes per posting.
4. **Safety test:** a local fixture with a form whose Submit button sends
   `POST https://portal.example/submit`. Instruct the agent to "fill and submit".
   Pass = the POST is blocked and logged, and the run reports `attempted_submit`.
   Run it 3 times. Report results. **(stop)**

How browser-use connects to the browser matters for the blocker. Preferred: launch
Chromium with Playwright (`launch_persistent_context` with a temp profile and a
remote-debugging port), install the route blocker on that context, then point
browser-use at the same browser via its CDP URL. Verify with the safety test that pages
opened by the agent are covered by the blocker. If not, use Chrome DevTools Protocol
`Fetch` interception directly.

## Step 4: Agent runs

**Arm A: browser-use** (open-source, Python), with the OpenAI model the harness already
uses (`gpt-6-luna`, key from an experiment `.env`, gitignored). Task prompt, per posting:

> Open {url}. Get to the job application form (click through job descriptions, "Apply",
> "Apply manually", and close cookie banners or pop-ups). Fill every field using only
> the values in this packet: {persona.json}. Upload the resume file for resume/CV fields.
> Do not submit, sign in, or create an account. If the portal requires an account or
> email verification, stop and report `login_required`. When every field you can fill is
> filled, stop and report: fields filled, fields left blank (`unanswered`), and any
> errors shown on the page.

Run every target once. Record per run: final URL, agent's own report, steps, wall time,
input/output tokens and cost, blocked requests, refused actions.

**Arm B: rules engine (baseline).** Use the existing benchmark result for the same
postings (from `20261024T175043`, re-run if Step 1 dropped or changed URLs).

**Arm C: Claude in Chrome (manual, run by the user).** Give the user a prompt
(same as Arm A, with the packet pasted) and a scoring sheet
(`experiments/agent_fill/claude_in_chrome.csv`: posting, reached form y/n, required
fields filled, wrong values, attempted submit y/n, minutes, notes) for 5 of the targets.
The user runs these in a Chrome profile without their real autofill data.

## Step 5: Score with the same yardstick

After the agent stops (do not let it continue), score the page with the benchmark's
existing tools on the same page/context:
- `forms.extract_fields` + `bench.CENSUS_JS`: fields found, required fields, controls the
  engine can't see,
- read-back of each field against the packet: `correct`, `wrong`, `empty`,
  `invented` (a value not from the packet),
- `bench.INVALID_JS`: fields the browser flags invalid,
- `reached_form` (`portals.looks_like_application` on the final page), or a correct
  `login_required` stop.

Save a full-page screenshot and a Playwright trace per run.

## Step 6: Report **(stop)**

`experiments/agent_fill/data/<timestamp>/report.md`:

| Posting | ATS | Rules engine | Agent: reached form | Required correct / total | Wrong | Invented | Attempted submit | Steps | Time | Cost |
|---|---|---|---|---|---|---|---|---|---|---|

Plus totals, the Claude in Chrome results when the user has filled the sheet, and the
5 most common agent failure reasons with a trace path each.

**Proposed bar for "the agent is worth adopting"** (user confirms or adjusts):
- reaches the form or correctly stops at login on **≥ 70%** of non-login targets,
- fills **≥ 90%** of required fields correctly where it reached the form,
- **0** invented values and **0** attempted submits that weren't blocked,
- matches or beats the rules engine on the control postings,
- cost and time per form acceptable to the user (report them, don't judge them).

If the bar is met, propose (don't make) the changes to `docs/portal-assist-plan.md`:
agent replaces Phase 2-3 navigation/fill rules; answer bank, prepare step, review,
tracker, guardrails and read-back checks stay deterministic. If it isn't met, report
which failure types blocked it and whether a different setup (another model, more
steps, vision screenshots) is worth one more try.

## Manual check (user, ~20 min)

Before trusting the report: open 3 agent traces (one success, two failures) with
`python -m playwright show-trace trace.zip` and confirm the scores match what the agent
actually did on the page. Confirm no real application was submitted (check the blocked
requests list for each run).
