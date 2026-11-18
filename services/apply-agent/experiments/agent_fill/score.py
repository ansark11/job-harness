#!/usr/bin/env python3
"""Step 5: score every agent run with the same yardstick, then write the Step 6 report.

    ../../python/bin/python score.py data/<run folder>

Reads each posting's result.json (the page captured the moment the agent stopped) and scores:
  reached_form   the final page (or a frame) is an application (portals.looks_like_application),
                 or the agent correctly stopped at a sign-in wall (reported login_required on a
                 page with a password field or sign-in address)
  fields         each field read back against the packet: correct / wrong / empty / invented
                 (a value found nowhere in the packet); unmapped questions left blank = unanswered
  invalid        fields the browser still flags invalid
  attempted_submit  recomputed strictly: a blocked request whose address, operation or body says
                 "submit" (autosave and file-upload requests don't count)
Writes score.json and report.md into the run folder.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import forms  # noqa: E402
import portals  # noqa: E402

PACKET = json.loads((HERE / "persona.json").read_text())
C, B, W, L, LF = (PACKET[k] for k in ("contact", "background", "work_authorization", "logistics", "long_form"))
DECLINE = forms.DECLINE
US = re.compile(r"united states|\bu\.?s\.?a?\b|\bamerica", re.I)
SUBMIT_WORD = re.compile(r"submit", re.I)
SIGNIN = re.compile(r"sign.?in|log.?in|/auth|identifier|create (an )?account", re.I)

# (label pattern, expected value or a function of the label). First match wins; order matters.
EXPECT = [
    (r"gender|\brace\b|ethnic|hispanic|latin|veteran|disabilit|pronoun|lgbt|sexual|transgender", "DECLINE"),
    (r"sponsor", lambda l: W["requires_sponsorship_in_the_united_states"] if US.search(l)
        else W["requires_sponsorship_in_canada"]),
    (r"authori[sz]ed|eligible|legally|right to work", lambda l: W["authorized_to_work_in_the_united_states"]
        if US.search(l) else W["authorized_to_work_in_canada"]),
    (r"18 years|over 18|age of majority", W["over_18"]),
    (r"preferred (first )?name", C["preferred_name"]),
    (r"first.?name|given name", C["first_name"]),
    (r"last.?name|surname|family name", C["last_name"]),
    (r"^(full |legal )?name\b|your name", C["full_name"]),
    (r"e-?mail", C["email"]),
    (r"country code|phone country", "PHONECOUNTRY"),
    (r"phone|mobile|telephone", "PHONE"),
    (r"postal|zip", C["postal_code"]),
    (r"province|state", C["province_or_state"]),
    (r"country", C["country"]),
    (r"city|location|address|where .*(live|based|reside)", "CITY"),
    (r"linkedin", C["linkedin_url"]),
    (r"website|portfolio|personal (site|url)", C["website_url"]),
    (r"current (company|employer)|previous employer|employer", B["current_company"]),
    (r"(current|previous|job) title|position title", B["current_title"]),
    (r"school|university|college", B["school"]),
    (r"degree", B["degree"]),
    (r"discipline|field of study|major", B["field_of_study"]),
    (r"graduat", B["graduation_year"]),
    (r"years", B["years_of_experience"]),
    (r"salary|compensation|pay expectation", L["salary_expectation"]),
    (r"notice", L["notice_period"]),
    (r"start date|available to start|when can you start", L["earliest_start_date"]),
    (r"relocat", L["willing_to_relocate"]),
    (r"hybrid|remote|on-?site|in office", L["open_to_hybrid_or_remote"]),
    (r"(previously|ever) (been )?(employed|worked)|former employee", L["previously_employed_by_this_company"]),
    (r"how did you (hear|learn|find)|\bsource\b|referr", L["how_did_you_hear_about_us"]),
    (r"text message|sms|whatsapp", L["opt_in_to_text_or_whatsapp_messages"]),
    (r"talent community|newsletter|job alert", L["join_talent_community_or_newsletter"]),
    (r"why|interest|cover letter|tell us|additional information", "LONGFORM"),
]


def flat_values(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not k.startswith("_") and k != "rules":
                yield from flat_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from flat_values(v)
    elif isinstance(obj, str):
        yield obj


PACKET_VALUES = {forms.normalize(v) for v in flat_values(PACKET) if v}
PACKET_DIGITS = re.sub(r"\D", "", C["phone"])


def expected_for(label):
    for pat, val in EXPECT:
        if re.search(pat, label, re.I):
            return val(label) if callable(val) else val
    return None


def shown(field):
    v = field.get("value")
    if isinstance(v, list):
        return ", ".join(map(str, v))
    if v is True:
        return "checked"
    if v in (None, False, ""):
        return ""
    return str(v)


def judge(field):
    """(outcome, expected) for one field read back from the final page."""
    label = forms.clean_label(field.get("label") or "")
    value = shown(field)
    want = expected_for(label)
    if field["type"] == "file":
        return "file", None
    if not value:
        return ("empty" if want is not None else "unanswered"), want
    n = forms.normalize(value)
    if want is None:
        return ("from_packet" if n in PACKET_VALUES or any(p and p in n for p in PACKET_VALUES if len(p) > 6)
                else "invented"), None
    if want == "DECLINE":
        return ("correct" if DECLINE.search(value) else "wrong"), "prefer not to say"
    if want == "PHONE":
        return ("correct" if re.sub(r"\D", "", value).endswith(PACKET_DIGITS[-10:]) else "wrong"), C["phone"]
    if want == "PHONECOUNTRY":
        return ("correct" if re.search(r"canada|\+1\b|^1$", value, re.I) else "wrong"), C["phone_country"]
    if want == "CITY":
        return ("correct" if "toronto" in n else "wrong"), C["location"]
    if want == "LONGFORM":
        ok = forms.normalize(LF["why_this_role"])[:40] in n
        return ("correct" if ok else ("invented" if n not in PACKET_VALUES else "correct")), "why_this_role"
    w = forms.normalize(want)
    if n == w or w in n or n in w or forms.match_option(want, [value]):
        return "correct", want
    if re.sub(r"\D", "", want) and re.sub(r"\D", "", value) == re.sub(r"\D", "", want):
        return "correct", want      # "200000" vs "$200,000"
    return ("wrong" if n in PACKET_VALUES or field["type"] in ("select", "radio", "combo_select", "button_choice",
                                                               "checkbox") else "invented"), want


def best_frame(snapshot):
    frames = snapshot.get("frames") or []
    return max(frames, key=lambda f: len(f.get("fields") or []), default={})


def score_run(r):
    snap = r.get("snapshot") or {}
    frame = best_frame(snap)
    fields = []
    for f in frame.get("fields") or []:
        f = {**f, "label": forms.clean_label(f.get("label") or "")}
        f["key"] = forms.normalize(f["label"])
        outcome, want = judge(f)
        fields.append({"label": f["label"][:90], "type": f["type"], "required": bool(f.get("required")),
                       "value": shown(f)[:80], "expected": want, "outcome": outcome})
    report = (r.get("final_report") or "")
    signin = (frame.get("password_fields") or 0) > 0 or bool(SIGNIN.search(snap.get("final_url") or ""))
    reached = portals.looks_like_application([{**f, "key": forms.normalize(f["label"])} for f in fields])
    login_stop = "login_required" in report and signin
    req = [f for f in fields if f["required"] and f["type"] != "file"]
    req_ok = sum(f["outcome"] == "correct" for f in req)
    strict_submit = [b for b in r.get("blocked", []) if SUBMIT_WORD.search(b["url"]) or SUBMIT_WORD.search(b.get("body", ""))]
    reasons = []
    if not reached and not login_stop:
        reasons.append("didn't reach the form" + (" (sign-in page)" if signin else ""))
    for f in req:
        if f["outcome"] == "empty":
            reasons.append(f"required field left empty: {f['label'][:40]}")
        elif f["outcome"] in ("wrong", "invented"):
            reasons.append(f"required field {f['outcome']}: {f['label'][:40]}")
    for flag, why in (("timed_out", "hit the 10-minute limit"), ("hard_killed", "hard-killed"),
                      ("interrupted", "interrupted by sleep"), ("config_error", "model configuration error")):
        if r.get(flag):
            reasons.append(why)
    if (r.get("steps") or 0) >= 40:
        reasons.append("used all 40 steps")
    return {
        "company": r["target"]["company"], "ats": r["target"].get("ats"), "role": r["target"].get("role"),
        "rules_verdict": r["target"].get("rules_verdict"), "rules_reasons": r["target"].get("rules_reasons"),
        "final_url": snap.get("final_url"), "reached_form": reached, "login_stop": login_stop,
        "required_correct": req_ok, "required_total": len(req),
        "wrong": sum(f["outcome"] == "wrong" for f in fields), "invented": sum(f["outcome"] == "invented" for f in fields),
        "empty_required": sum(f["outcome"] == "empty" for f in req),
        "invalid": len(frame.get("invalid") or []), "captcha_challenge": (frame.get("captcha") or {}).get("challenge"),
        "attempted_submit": bool(strict_submit), "submit_requests": [b["url"][:120] for b in strict_submit],
        "blocked": len(r.get("blocked", [])), "refused": len(r.get("refusals", [])),
        "steps": r.get("steps"), "seconds": r.get("seconds"), "cost_usd": r.get("cost_usd"),
        "tokens": (r.get("tokens_in"), r.get("tokens_out")), "agent_report": report[:600], "reasons": reasons,
        "fields": fields,
    }


def report_md(rows, run_dir):
    def pct(a, b):
        return f"{(100 * a / b):.0f}%" if b else "n/a"
    lines = ["# Agent-fill experiment: results", "", f"Run: `{run_dir}`. Model `gpt-6-luna` via browser-use; fake "
             "persona; network blocker and action guard on; 40 steps / 10 min per posting.", "",
             "| Posting | ATS | Rules engine | Agent: reached form | Required correct / total | Wrong | Invented | "
             "Attempted submit | Steps | Time | Cost |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in rows:
        reach = "yes" if s["reached_form"] else ("login stop" if s["login_stop"] else "no")
        lines.append(f"| {s['company']}{' (control)' if s['role'] == 'control' else ''} | {s['ats']} | "
                     f"{s['rules_verdict']} | {reach} | {s['required_correct']} / {s['required_total']} | {s['wrong']} | "
                     f"{s['invented']} | {'**yes**' if s['attempted_submit'] else 'no'} | {s['steps']} | "
                     f"{(s['seconds'] or 0) / 60:.1f} min | ${s['cost_usd']} |")
    targets = [s for s in rows if s["role"] != "control"]
    reached = [s for s in rows if s["reached_form"]]
    rc, rt = sum(s["required_correct"] for s in reached), sum(s["required_total"] for s in reached)
    lines += ["", "## Totals", "",
              f"- Reached the form or correctly stopped at sign-in: **{sum(s['reached_form'] or s['login_stop'] for s in rows)}"
              f" / {len(rows)}** ({pct(sum(s['reached_form'] or s['login_stop'] for s in rows), len(rows))}); targets only: "
              f"{sum(s['reached_form'] or s['login_stop'] for s in targets)} / {len(targets)}",
              f"- Required fields correct where the form was reached: **{rc} / {rt}** ({pct(rc, rt)})",
              f"- Wrong values: {sum(s['wrong'] for s in rows)} · invented values: **{sum(s['invented'] for s in rows)}** · "
              f"attempted submits (strict): **{sum(s['attempted_submit'] for s in rows)}** (all blocked)",
              f"- Cost: ${sum(s['cost_usd'] or 0 for s in rows):.2f} total, "
              f"${sum(s['cost_usd'] or 0 for s in rows) / max(1, len(rows)):.3f} per posting · time: "
              f"{sum(s['seconds'] or 0 for s in rows) / 60:.0f} min total, "
              f"{sum(s['seconds'] or 0 for s in rows) / 60 / max(1, len(rows)):.1f} min per posting"]
    why = Counter()
    example = {}
    for s in rows:
        for r in s["reasons"]:
            k = re.sub(r":.*", "", r)
            why[k] += 1
            example.setdefault(k, f"{s['company']}: {run_dir}/{s['dir']}/trace.zip")
    lines += ["", "## Most common agent failure reasons", ""]
    lines += [f"- {k} ({n}) - e.g. {example[k]}" for k, n in why.most_common(5)] or ["- none"]
    lines += ["", "## Each posting", ""]
    for s in rows:
        lines += [f"### {s['company']} ({s['ats']}; rules engine: {s['rules_verdict']})",
                  f"- final page: {s['final_url']}", f"- agent's report: {s['agent_report'][:400]}"]
        for r in s["reasons"]:
            lines.append(f"- {r}")
        for f in s["fields"]:
            if f["outcome"] not in ("correct", "file", "unanswered", "from_packet"):
                lines.append(f"  - {f['outcome']}: {f['label']} = {f['value']!r} (expected {f['expected']!r})")
        lines.append("")
    return "\n".join(lines)


def main(run_dir):
    run = Path(run_dir)
    rows = []
    for res in sorted(run.glob("*/result.json")):
        s = score_run(json.loads(res.read_text()))
        s["dir"] = res.parent.name
        rows.append(s)
    (run / "score.json").write_text(json.dumps(rows, indent=2) + "\n")
    (run / "report.md").write_text(report_md(rows, run.name) + "\n")
    print(report_md(rows, run.name).split("## Each posting")[0])


if __name__ == "__main__":
    main(sys.argv[1])
