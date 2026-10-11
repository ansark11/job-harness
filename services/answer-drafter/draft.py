#!/usr/bin/env python3
"""Draft an answer to a long-form application question from the resume.

    python3 draft.py --question "Why are you a good fit?" --job job.json --resume resume.txt
                     [--max-chars 1500] [--steer "lean on the payments work"] [--previous draft.txt]
                     [--examples approved.json]

The model writes; code checks. The draft must stay within --max-chars (one
retry asking for a shorter version, then it is returned with
within_limit=false for the user to trim). It must only use facts from the
resume: the reply lists the resume points it relied on so the user can check.

--examples: previously approved answers to similar questions
(tracker.py answers similar), used for the user's voice, not copied.
--previous + --steer: revise an earlier draft as the user asked.

Output: {"answer", "resume_points", "char_count", "max_chars", "within_limit"}
"""

import argparse
import json
import sys
from pathlib import Path

import llm
import runlog

HERE = Path(__file__).resolve().parent
DEFAULT_MAX_CHARS = 1500
DESCRIPTION_LIMIT = 8000
RESUME_LIMIT = 12000

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer", "resume_points"],
    "properties": {"answer": {"type": "string"}, "resume_points": {"type": "array", "items": {"type": "string"}}},
}

PROMPT = """You are writing one answer on a job application, in the candidate's own voice (first person).

Rules:
- Use ONLY facts in the resume below. Never invent employers, numbers, titles or outcomes.
- Tie the candidate's real experience to what this role and company need. Be specific, not generic.
- Plain, confident, warm. No clichés ("I am passionate about", "I am excited to apply"), no headings, no bullet lists.
- At most {max_chars} characters. Aim for {target} characters unless the question needs less.
- resume_points: the short resume facts you relied on, quoted or closely paraphrased.

QUESTION
{question}

ROLE
{title} at {company}
{description}

RESUME
{resume}
{examples}{revision}"""


def build_prompt(question, job, resume, max_chars, steer=None, previous=None, examples=None):
    ex = ""
    if examples:
        ex = "\nPAST ANSWERS THE CANDIDATE APPROVED (match their voice; don't copy):\n" + "\n\n".join(
            f"Q: {e['question']}\nA: {e['answer']}" for e in examples[:3]) + "\n"
    rev = ""
    if previous:
        rev = f"\nREVISE THIS EARLIER DRAFT:\n{previous}\n"
    if steer:
        rev += f"\nTHE CANDIDATE ASKS: {steer}\n"
    return PROMPT.format(max_chars=max_chars, target=int(max_chars * 0.75), question=question,
                         title=job.get("title") or "", company=job.get("company") or "",
                         description=(job.get("description") or "")[:DESCRIPTION_LIMIT],
                         resume=resume[:RESUME_LIMIT], examples=ex, revision=rev)


def draft(question, job, resume, max_chars=DEFAULT_MAX_CHARS, steer=None, previous=None, examples=None,
          post=None, calls=None):
    """Returns the output dict. `calls` (a list) collects model-call details for the run log."""
    prompt = build_prompt(question, job, resume, max_chars, steer, previous, examples)
    for attempt in range(2):
        out, call = llm.call_json(prompt, "application_answer", SCHEMA, post)
        if calls is not None:
            calls.append(call)
        answer = (out.get("answer") or "").strip()
        if not answer:
            raise llm.ModelError("model returned an empty answer")
        if len(answer) <= max_chars:
            break
        prompt += (f"\nYOUR LAST VERSION WAS {len(answer)} CHARACTERS. Rewrite it in at most {max_chars} "
                   f"characters, keeping the strongest points:\n{answer}\n")
    return {"answer": answer, "resume_points": out.get("resume_points") or [], "char_count": len(answer),
            "max_chars": max_chars, "within_limit": len(answer) <= max_chars}


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", required=True)
    ap.add_argument("--job", required=True, help="posting JSON (title, company, description)")
    ap.add_argument("--resume", required=True, help="resume text file")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--steer")
    ap.add_argument("--previous", help="file with an earlier draft to revise")
    ap.add_argument("--examples", help="JSON list of {question, answer}")
    args = ap.parse_args(argv)

    with runlog.start("answer-drafter", HERE, argv) as run:
        job = json.loads(Path(args.job).read_text())
        resume = Path(args.resume).read_text()
        previous = Path(args.previous).read_text() if args.previous else None
        examples = json.loads(Path(args.examples).read_text()) if args.examples else None
        calls = []
        try:
            out = draft(args.question, job, resume, args.max_chars, args.steer, previous, examples, post, calls)
        except llm.ModelError as e:
            print(json.dumps({"error": str(e)}))
            run.error("draft_failed", question=args.question, error=str(e))
            run.finish(2)
            return 2
        print(json.dumps(out, indent=2))
        run.finish(0, question=args.question, job_id=job.get("id"), char_count=out["char_count"],
                   within_limit=out["within_limit"], attempts=len(calls), steer=args.steer,
                   usage=[c.get("usage") for c in calls], resume_digest=runlog.digest(resume))
        return 0


if __name__ == "__main__":
    sys.exit(main())
