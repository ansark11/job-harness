# job-search

Finds postings on LinkedIn's public (logged-out) job pages and prints them as
normalized JSON. Stdlib only; no login, so it never touches your account.

```bash
python3 search.py --criteria ../../criteria.json [--since <ISO time>] [--max-pages N] [--out postings.json]
```

- **Window:** from `--since` (the last successful run) to now, at least 24h and
  at most 30 days. Without `--since`, the last 24 hours.
- **Queries:** every `search.keywords` × `search.locations` in the criteria
  file, filtered to `work_types` and `job_types`. Pages (10 cards each) until a
  short page, a page with nothing new, or `max_pages`.
- **Title pre-filter:** cards failing `title_include` / `title_exclude` are
  skipped *before* their detail page is fetched, to save requests. Seniority
  and everything else is the matcher's job.
- **Throttle:** 2–4 s between requests; 30 s then 90 s backoff on 429/999.
  Exit code 2 if every query failed (likely rate-limited).

Each posting: `id, url, title, company, company_url, company_logo, location,
posted_at` (estimated from "N hours ago"), `posted_date, applicants,
apply_type` (`easy` | `external` | `unknown`), `criteria` (seniority_level,
employment_type, job_function, industries), `description, search_hits`.
`skipped` lists every posting dropped and why.

Pay is not structured on guest pages; it stays in `description` for the
matcher to extract. External application URLs aren't exposed while logged
out; apply-agent resolves them at apply time.

Tests run on real responses saved in `tests/fixtures/`:
`python3 -m unittest discover -s tests -t .`
