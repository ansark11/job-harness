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

## Data (gitignored)

`data/browser-profile/` your LinkedIn session · `data/submissions.jsonl`
daily-cap counter · `data/uploads/` resume copies with readable names ·
`data/screens/` screenshots.

## Account risk

Automating LinkedIn is against its User Agreement and can get an account
restricted. The cap, pauses and headed browser reduce the risk; they don't
remove it.

## Tests

Run headless Chromium against `tests/fixtures/easy_apply.html`, a stand-in
page with the same roles and step structure as Easy Apply (no LinkedIn
markup, no network): `./python/bin/python -m unittest discover -s tests -t .`
