# tracker

Owns `data/jobs.db` and `data/resumes/` (gitignored): opportunities and
their review status, daily runs, resumes, the answer bank, the inbox of
unanswered questions, and approved long-form answers. Every command prints
JSON; the web app's API (Phase B) will call the same functions.

Setup (PDF resumes need `pypdf`):
`python3 -m venv python && python/bin/pip install -r requirements.txt`, then
run commands with `./python/bin/python tracker.py ...`.

```bash
python3 tracker.py ingest matches.json [--run-id ID]      # matcher output; '-' reads stdin
python3 tracker.py list --status new --group-by fetch-date
python3 tracker.py show <id>
python3 tracker.py keep <id> [<id> ...]
python3 tracker.py discard <id> [<id> ...]
python3 tracker.py expire
python3 tracker.py run start --kind daily_search
python3 tracker.py run finish <run id> --status ok|failed [--stats JSON]
python3 tracker.py run last-success --kind daily_search
```

- **Ingest is idempotent** on the LinkedIn id. A posting you've already kept
  or discarded is never reset; one still unreviewed gets its data and score
  refreshed but keeps its first fetch time.
- **Expiry:** postings unreviewed 7 days after they were fetched become
  `expired`. Runs on every ingest and with `expire`.
- **Status changes:** new → kept | discarded | expired; kept ↔ discarded;
  expired → kept or discarded. Every change is in `show`'s `history`.
- **Backlog groups** use your local date of fetch, newest first, with days
  left before expiry.

## Resumes

```bash
tracker.py resume add ~/Downloads/resume.pdf --name "PM 2026" [--default]
tracker.py resume list | show <id> [--text] | default [<id>]
```

A copy of the file is kept (it's what gets uploaded) plus its extracted text
(what the matcher and drafter read). PDF, DOCX, TXT or MD. The first resume
added is the default; the daily search scores fit against the default.

## Answer bank and inbox

Applications ask the same things in different words. The bank holds one
answer per **key**; every new wording you confirm becomes an **alias**, so
the next application fills it without asking or calling the model.

```bash
tracker.py bank onboard                 # answer the standard questions once (interactive)
tracker.py bank set sponsorship_us No [--always-ask]
tracker.py bank resolve "Do you require visa sponsorship?"
tracker.py inbox list                   # new questions from applications
tracker.py inbox resolve 3 --key sponsorship_us          # same as an existing key
tracker.py inbox answer 4 --answer "Yes"                  # a genuinely new question
```

- Standard keys: contact details, work authorization and sponsorship
  (Canada and US separately), relocation, notice period, salary
  expectations (CAD and USD), US EEO and Canadian employment equity
  self-identification, each with "prefer not to say" options.
- `--always-ask` keeps a key from ever being auto-filled (e.g. salary).
- **Self-identification answers never leave this machine:** `bank keys`
  (what the model sees) has no answers, and answers are masked in the run log.

## Approved answers

`answers add` records a long-form answer you approved; `answers similar
--question ...` finds past approved answers to similar questions (token
overlap, no model) for the drafter to match your voice.

`TRACKER_DB` points at a different database (tests use a temp one).
Tests: `python3 -m unittest discover -s tests -t .`
