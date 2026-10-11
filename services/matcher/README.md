# matcher

Filters job-search postings against your criteria and scores fit against a
resume. Stdlib only; OpenAI Responses API (`gpt-6-luna`) with strict JSON
schema output.

```bash
python3 match.py postings.json --criteria ../../criteria.json --resume resume.txt [--out matches.json] [--no-cache]
```

The model proposes, code decides:

1. **prefilter** (no model call): every LinkedIn industry on the excluded list
   (e.g. only "Retail"), part-time / internship, or posted ≥ `max_age_days`.
2. **extract** (model, `extract.py`): work mode, employment type, contract
   length, seniority, every pay range listed, the company's industry category,
   a 0–100 fit score with reasons and concerns. The model makes no decision.
   Cached in `data/extractions.json` per posting + resume + prompt version.
3. **decide** (`rules.py`):
   - Canada: Toronto/Vancouver metros (`canada_metros`), or anywhere if remote. US: anywhere.
   - On-site rejected; contracts under `contract_min_months` rejected.
   - Pay (only if listed): the range for the posting's city, else the highest
     band, annualized and converted with `fx_to_cad`; rejected below
     `min_total_cad × (1 − tolerance)`, flagged if just under.
   - Industry in `industries`; `big_tech_companies` overrides the model's category.
   - A seniority word in the title is enough; otherwise the model's level must be senior+.

Anything the model couldn't determine (no pay, unknown work mode, contract
length not stated) passes with a **flag** rather than rejecting. A posting
the model failed on is kept, flagged "not scored".

Matches sort fresh (last 24h) first, then by score. `rejected` keeps every
dropped posting with its stage and reasons; each decision is also in the run
log (`python3 ../../scripts/trace.py --last --full`).

Tests use canned model replies: `python3 -m unittest discover -s tests -t .`
