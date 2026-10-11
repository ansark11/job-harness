# job-harness

Daily LinkedIn job search for senior/principal PM roles, a review step to
keep or discard matches, and assisted applications (LinkedIn Easy Apply and
external portals). Interview prep comes later.

| Component | Purpose |
|---|---|
| services/job-search | Find postings via LinkedIn's public job pages |
| services/matcher | Filter against criteria and score fit against your resume |
| services/answer-drafter | Draft long-form application answers |
| services/apply-agent | Fill and submit applications in a browser |
| services/tracker | Store opportunities, resumes, answer bank and applications |
| workflows/ | Chain the services (daily search, apply) |

Services never import each other; see [CLAUDE.md](CLAUDE.md) for the
boundary rules. Run all tests with `./scripts/test.sh`. See everything a run
did with `python3 scripts/trace.py --last`.

> **No personal data lives in this repo.** Each service's `data/`, `.env`
> and browser profile are gitignored.
