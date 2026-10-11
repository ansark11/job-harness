# tracker

Owns `data/jobs.db` (gitignored): opportunities and their review status,
and the history of daily runs. Resumes, the answer bank and applications
are added in later steps. Every command prints JSON; the web app's API
(Phase B) will call the same functions in `store.py`.

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

`TRACKER_DB` points at a different database (tests use a temp one).
Tests: `python3 -m unittest discover -s tests -t .`
