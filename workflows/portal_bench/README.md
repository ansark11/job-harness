# portal_bench

Measures how well the apply-agent handles the company portals your matched
jobs actually use. Nothing is ever submitted.

```bash
python3 workflows/portal_bench/run.py                 # every portal posting in the tracker
python3 workflows/portal_bench/run.py --limit 5       # try a few first
python3 workflows/portal_bench/run.py --urls extra.txt   # add portal URLs, one per line
python3 workflows/portal_bench/run.py --watch     # show a window for each form check
```

What happens:

1. **Find the portals.** The tracker lists opportunities that apply on a
   company site. For each one, the apply-agent clicks LinkedIn's Apply once
   (signed-in profile, pauses of 6-14 s between postings) and caches where it
   leads in `services/apply-agent/data/bench/resolved.json`. Later runs reuse it.
   LinkedIn's "Continue to apply" prompts are clicked through; a posting that
   never leaves LinkedIn counts as unresolved. One LinkedIn window stays open
   for this step and moves from posting to posting (that's expected).
2. **Audit each form** in the background (no windows unless `--watch`), in a
   clean browser with no cookies, at most 2 minutes per form. Cookie banners
   and pop-ups over the form are closed first: open it the way the
   agent does, fill every field with a made-up test person and a placeholder
   PDF, read every value back, and check what the engine missed. Requests that
   could send data out are blocked while filling.
3. **Score it** per posting and per portal type.

| Verdict | Meaning |
|---|---|
| pass | every control found, every value stuck, a Submit button, no visible CAPTCHA |
| risky | would likely submit, with a caveat (an optional field missed, a follow-up question) |
| fail | something required wasn't found or didn't stick, or the flow is multi-page |
| manual | portal needs an account (Workday, iCIMS, Taleo) |
| unreachable | no form found, page error |
| unresolved | LinkedIn's Apply didn't lead anywhere |

Output: `data/<timestamp>/bench/report.md` (read this), `results.json`, and a
folder per posting with `page.html`, `aria.txt`, `filled.png` and `trace.zip`
(`services/apply-agent/python/bin/python -m playwright show-trace trace.zip`).

After changing the form engine, re-check the saved pages offline:
`cd services/apply-agent && ./python/bin/python bench.py replay <bench dir>`.
