# apply-agent

Fills and submits applications in a real Chromium browser (Playwright).
Own venv: `python3 -m venv python && python/bin/pip install -r requirements.txt && python/bin/python -m playwright install chromium`.

```bash
./python/bin/python login.py                 # sign in to LinkedIn yourself, once
./python/bin/python login.py --check         # is the saved session still signed in?
./python/bin/python apply.py packet.json --mode fill|submit [--dry-run] [--headless] [--daily-cap 10]
```

The workflow (`workflows/apply`) builds `packet.json`; you rarely run
`apply.py` directly. The agent knows nothing about the answer bank: it fills
only the answers in the packet, keyed by each field's normalized label.

## LinkedIn Easy Apply (`easyapply.py`)

Walks the dialog page by page:

- fills fields the packet answers, keeps fields LinkedIn prefilled from your
  profile (email, phone), leaves `skip` fields blank;
- picks your resume: selects it if LinkedIn already has a file with the same
  name, otherwise uploads it under a readable name (`<resume name>.pdf`);
- **stops at the first page with an unknown field** and reports every field
  seen (label, type, options, required, outcome) so the workflow can resolve
  them and run again; each run starts a fresh dialog and discards it on exit;
- submits only in `--mode submit`, without `--dry-run`, under the daily cap,
  and only after confirming LinkedIn's "application sent" message.

Statuses: `submitted`, `ready` (reached review, didn't submit),
`needs_input`, `already_applied`, `not_easy_apply`, `login_required`,
`cap_reached`, `error`. Screenshots of each page go to `data/screens/`.

## Company portals (`portals.py`)

For postings that apply on the company's site:

- **Finding the form:** clicks LinkedIn's Apply button (signed in) and
  follows the tab it opens; follows an iframe that embeds a Greenhouse,
  Lever or Ashby form; goes to Lever's `/apply` and Ashby's `/application`
  pages; otherwise clicks the page's "Apply" link. The resolved URL is
  reported so later runs go straight there.
- **Filling:** the same engine as Easy Apply on one long page, plus
  yes/no button groups (Ashby) and searchable dropdowns that only accept a
  listed option (picks the listed option matching the answer).
- **Approval:** `--mode fill` fills everything and reports `ready` with a
  full-page screenshot, without submitting. `--mode submit` refills from the
  same answers, submits, and waits for the portal's confirmation (up to 3
  minutes when headed, so you can solve a CAPTCHA).
- **Left to you (`needs_user`):** Workday, iCIMS and Taleo (they need an
  account you create), and a CAPTCHA that wasn't solved.

Choice answers match the form's own wording where unambiguous ("No" →
"No, I will not require sponsorship"), and any decline-to-answer wording
matches the form's decline option ("I don't wish to answer" → "Decline To
Self Identify").

## Data (gitignored)

`data/browser-profile/` your LinkedIn session · `data/submissions.jsonl`
daily-cap counter · `data/uploads/` resume copies with readable names ·
`data/screens/` screenshots.

## Account risk

Automating LinkedIn is against its User Agreement and can get an account
restricted. The cap, pauses and headed browser reduce the risk; they don't
remove it.

## Tests

Run headless Chromium against stand-in pages in `tests/fixtures/` (an Easy
Apply dialog; a job page whose Apply opens a careers page embedding a
Greenhouse-style form; an Ashby-style form). No real site markup, no
network: `./python/bin/python -m unittest discover -s tests -t .`
