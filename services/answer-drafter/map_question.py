#!/usr/bin/env python3
"""Propose which answer-bank key an unfamiliar application question means.

    python3 map_question.py --question "Do you need an H-1B now or later?" --keys keys.json [--options '["Yes","No"]']

keys.json is `tracker.py bank keys` output: keys and canonical wording only.
The model never sees stored answers, so self-identification answers never
leave the machine.

The model proposes; code checks: a key that isn't in keys.json becomes null.
The user confirms every proposal in the tracker inbox before it is used.

Output: {"key": <key or null>, "confidence": 0..1, "reason"}
"""

import argparse
import json
import sys
from pathlib import Path

import llm
import runlog

HERE = Path(__file__).resolve().parent

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["key", "confidence", "reason"],
    "properties": {"key": {"type": ["string", "null"]}, "confidence": {"type": "number"},
                   "reason": {"type": "string"}},
}

PROMPT = """A job application asks the question below. Which of the known questions asks for the SAME
information, so the same answer could be reused? Different wording is fine; a different fact is not
(e.g. sponsorship in the US is not sponsorship in Canada; salary in USD is not salary in CAD).
If none asks for the same information, return key null.

APPLICATION QUESTION
{question}{options}

KNOWN QUESTIONS (key: wording)
{keys}

confidence: 0 to 1, how sure you are that the answer to the known question fully answers this one.
reason: one short sentence."""


def propose(question, keys, options=None, post=None):
    listing = "\n".join(f"{k['key']}: {k['question']}" for k in keys)
    opts = f"\nOptions offered: {', '.join(options)}" if options else ""
    out, call = llm.call_json(PROMPT.format(question=question, options=opts, keys=listing), "question_mapping",
                              SCHEMA, post)
    known = {k["key"] for k in keys}
    key = out.get("key")
    confidence = max(0.0, min(1.0, float(out.get("confidence") or 0)))
    if key not in known:
        key, confidence = None, 0.0
    return {"key": key, "confidence": round(confidence, 2), "reason": out.get("reason") or ""}, call


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", required=True)
    ap.add_argument("--keys", required=True, help="tracker.py bank keys output")
    ap.add_argument("--options", help="JSON list of the field's options")
    args = ap.parse_args(argv)

    with runlog.start("answer-drafter", HERE, argv) as run:
        data = json.loads(Path(args.keys).read_text())
        keys = data["keys"] if isinstance(data, dict) else data
        if any("answer" in k for k in keys):
            sys.exit("keys file contains answers; pass `tracker.py bank keys` output, which has none")
        try:
            out, call = propose(args.question, keys, json.loads(args.options) if args.options else None, post)
        except llm.ModelError as e:
            print(json.dumps({"error": str(e)}))
            run.error("map_failed", question=args.question, error=str(e))
            run.finish(2)
            return 2
        print(json.dumps(out, indent=2))
        run.event("question_mapped", question=args.question, **out, usage=call.get("usage"))
        run.finish(0)
        return 0


if __name__ == "__main__":
    sys.exit(main())
