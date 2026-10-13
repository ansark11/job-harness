# daily_search

job-search → matcher → tracker ingest, then a macOS notification.

```bash
python3 workflows/daily_search/run.py [--resume resume.txt] [--force] [--max-pages N] [--no-notify]
```

- **Guard:** exits without doing anything if a run already succeeded since
  the most recent 19:00 (local). `--force` overrides.
- **Window:** job-search looks back to where the last successful run
  *started* (24h minimum, 30 days maximum), so nothing is missed between runs.
- **Resume:** `--resume`, else the tracker's default resume.
- **Run record:** every run is recorded in the tracker (`tracker.py run ...`)
  as ok or failed with its stats; a failure also notifies.
- **Artifacts:** each run's `postings.json` and `matches.json` are kept in
  `data/<time>-<run id>/` (gitignored), last 14 runs.
- **Trace:** `python3 scripts/trace.py --last` shows the workflow and every
  service it called.

## Schedule (launchd)

```bash
./launchd/install.sh      # installs and loads com.job-harness.daily-search
./launchd/uninstall.sh
```

Runs at 19:00; if the Mac is asleep then, launchd runs it on wake. The job
runs under `caffeinate -i`, so the Mac doesn't idle-sleep mid-run (a full run
takes ~15–25 minutes; closing the lid still sleeps it, and the run resumes
on wake). Also runs
at login, to cover a full shutdown; the guard makes extra triggers harmless.
Loading the agent runs it once immediately. Output goes to
`workflows/daily_search/logs/launchd.{out,err}.log`.

The repo is under `~/Documents`, which macOS protects: the first background
run may fail with "Operation not permitted" until the Python binary that
`install.sh` prints is allowed (System Settings → Privacy & Security → Files
and Folders, or Full Disk Access).
