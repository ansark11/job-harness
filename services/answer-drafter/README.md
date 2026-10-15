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

## answer_question.py — answer a screening question for you (hands-off Easy Apply)

```bash
python3 answer_question.py --question "Can you work onsite Tue/Thu in SF?" --job job.json --resume resume.txt \
                           --profile profile.json [--field-type radio] [--options '["Yes","No"]']
```

Answers from the resume, your saved answers (contact details excluded) and
your job preferences only (`profile.json`, built by the apply workflow), with
a 0–1 confidence and the fact it relied on. For a choice field the answer
must be one of the options or it's dropped. Never claims credentials or
experience the resume doesn't show. Self-identification answers are mapped
to the option meaning exactly the same, or the decline option. The workflow
submits only confident answers.

## map_question.py — which bank key does a new question mean?

```bash
python3 map_question.py --question "Do you need an H-1B now or later?" --keys keys.json [--options '["Yes","No"]']
```

`keys.json` is `tracker.py bank keys` output: keys and wording, **never
answers** (the script refuses a file containing answers). A key the model
invents becomes `null`. Proposals go to the tracker inbox; you confirm
each before it's used.

Tests use canned replies: `python3 -m unittest discover -s tests -t .`
