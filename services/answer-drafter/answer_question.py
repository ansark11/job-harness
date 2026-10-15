#!/usr/bin/env python3
"""Answer a short screening question on the candidate's behalf, with a confidence.

    python3 answer_question.py --question "Are you able to work onsite Tue/Thu in SF?" --job job.json \
        --resume resume.txt --profile profile.json [--field-type radio] [--options '["Yes","No"]']

profile.json: {"answers": [{"question", "answer"}, ...], "preferences": {...}} - the
candidate's saved answers (work authorization, relocation, self-identification...) and
job preferences (locations, work modes). Contact details are never included.

The model proposes; code checks: for a choice field the answer must be one of the
options (otherwise it's dropped). The caller decides what confidence is enough to
submit; below that, it skips the job rather than guess.

Output: {"answer": <str or null>, "confidence": 0..1, "basis": <one sentence>}
"""

import argparse
import json
import re
import sys
from pathlib import Path

import llm
import runlog

HERE = Path(__file__).resolve().parent
DESCRIPTION_LIMIT = 6000
RESUME_LIMIT = 12000

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer", "confidence", "basis"],
    "properties": {"answer": {"type": ["string", "null"]}, "confidence": {"type": "number"},
                   "basis": {"type": "string"}},
}

PROMPT = """You are filling in one screening question on a job application for the candidate below.
Answer as the candidate would, using ONLY the facts given: their resume, their saved answers
and their job preferences.

Rules:
- Never claim a certification, clearance, license, degree, tool or experience the resume doesn't show.
- Years of experience: compute from the resume's dates; count only experience that clearly applies.
- Willingness questions (relocation, onsite days, travel, start date): use the saved answers and
  preferences. If they don't settle it, give your best answer with low confidence.
- Self-identification questions (gender, ethnicity, disability, veteran status...): choose the
  option closest to the candidate's saved answer (e.g. saved "Man" -> "Cisgender male" or "Male";
  saved "I am not a protected veteran" -> "No, I am not a veteran"). Only if no option is
  reasonably close, choose the option that declines to answer. Never infer these from the resume.
  Confidence here = how close the chosen option is to the saved answer: 0.95+ same meaning in
  other words, 0.85-0.95 a close equivalent that adds or drops a small detail, below 0.8 loose.
- {choice_rule}
- confidence: 0 to 1, how sure you are this is the answer the candidate would give. Use 0.9+ only
  when the facts state it directly; 0.5 or less when you're inferring.
- basis: one short sentence naming the fact you relied on.

QUESTION
{question}{options}

ROLE
{title} at {company}, {location}
{description}

CANDIDATE'S SAVED ANSWERS
{answers}

CANDIDATE'S JOB PREFERENCES
{preferences}

RESUME
{resume}
"""


def _norm(t):
    return " ".join(re.sub(r"[^\w\s]", " ", (t or "").lower()).split())


def answer(question, job, resume, profile, options=None, field_type=None, post=None):
    """(output dict, call details)."""
    choice_rule = ("answer must be exactly one of the options, copied verbatim." if options else
                   "answer as plain text; a number for numeric questions; keep it short.")
    saved = "\n".join(f"- {a['question']}: {a['answer']}" for a in profile.get("answers", []) if a.get("answer")) \
        or "(none)"
    prompt = PROMPT.format(choice_rule=choice_rule, question=question,
                           options=f"\nOptions: {json.dumps(options)}" if options else "",
                           title=job.get("title") or "", company=job.get("company") or "",
                           location=job.get("location") or "",
                           description=(job.get("description") or "")[:DESCRIPTION_LIMIT],
                           answers=saved, preferences=json.dumps(profile.get("preferences") or {}),
                           resume=resume[:RESUME_LIMIT])
    out, call = llm.call_json(prompt, "screening_answer", SCHEMA, post)
    ans = out.get("answer")
    conf = max(0.0, min(1.0, float(out.get("confidence") or 0)))
    if ans is not None and options:
        exact = [o for o in options if _norm(o) == _norm(ans)]
        ans = exact[0] if exact else None          # an answer that isn't an option can't be submitted
    if ans is None or not str(ans).strip():
        ans, conf = None, 0.0
    return {"answer": ans, "confidence": round(conf, 2), "basis": out.get("basis") or ""}, call


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--resume", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--field-type")
    ap.add_argument("--options", help="JSON list")
    args = ap.parse_args(argv)

    with runlog.start("answer-drafter", HERE, argv) as run:
        try:
            out, call = answer(args.question, json.loads(Path(args.job).read_text()), Path(args.resume).read_text(),
                               json.loads(Path(args.profile).read_text()),
                               json.loads(args.options) if args.options else None, args.field_type, post)
        except llm.ModelError as e:
            print(json.dumps({"error": str(e)}))
            run.error("answer_failed", question=args.question, error=str(e))
            run.finish(2)
            return 2
        print(json.dumps(out, indent=2))
        run.event("question_answered", question=args.question, **out, usage=call.get("usage"))
        run.finish(0)
        return 0


if __name__ == "__main__":
    sys.exit(main())
