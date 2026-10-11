# answer-drafter

Two small model tools for applications. Stdlib only; OpenAI Responses API
(`gpt-6-luna`), strict JSON output. Key in `.env` (see `.env.example`).

## draft.py — long-form answers

```bash
python3 draft.py --question "Why are you a good fit?" --job job.json --resume resume.txt \
                 [--max-chars 1500] [--steer "lean on payouts"] [--previous draft.txt] [--examples approved.json]
```

- Written in your voice from **resume facts only**; the reply lists the
  `resume_points` it relied on so you can check nothing was invented.
- Over `--max-chars`: one retry asking for a shorter version, then returned
  with `within_limit: false`.
- `--previous` + `--steer` revise a draft the way you asked (the
  "Regenerate with a steer" action).
- `--examples`: past answers you approved to similar questions
  (`tracker.py answers similar`), for voice only.

## map_question.py — which bank key does a new question mean?

```bash
python3 map_question.py --question "Do you need an H-1B now or later?" --keys keys.json [--options '["Yes","No"]']
```

`keys.json` is `tracker.py bank keys` output: keys and wording, **never
answers** (the script refuses a file containing answers). A key the model
invents becomes `null`. Proposals go to the tracker inbox; you confirm
each before it's used.

Tests use canned replies: `python3 -m unittest discover -s tests -t .`
