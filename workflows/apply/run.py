#!/usr/bin/env python3
"""Apply to a kept opportunity: agent -> resolve unknown fields -> agent again, until done or blocked on you.

    python3 workflows/apply/run.py start <opportunity id> [--resume <id>] [--posting <city's id>] [--dry-run] [--headless]
    python3 workflows/apply/run.py continue <application id> [--dry-run] [--headless]
    python3 workflows/apply/run.py submit <application id> [--headless]     # approve a filled portal application
    python3 workflows/apply/run.py regenerate <application id> <item> [--steer "..."]
    python3 workflows/apply/run.py status <application id>
    python3 workflows/apply/run.py summary [--hours 24]     # applied, answers chosen for you, skipped

Each round the apply-agent walks the form with every answer known so far and
stops at the first field it can't fill. Each unknown field is resolved:

  long-form (textarea)   required: drafted from the resume -> waits for your approval
                         optional: left blank
  profile question       answer bank (exact wording or a confirmed alias) -> filled
                         no answer yet: you answer it (inbox / bank set);
                         required ones block, optional ones are left blank this time
  other file uploads     required: you handle it; optional: left blank

Then the agent runs again. Easy Apply submits on its own once every field is
answered, as configured; --dry-run stops at the review step instead.
Company portals (Greenhouse, Lever, Ashby, others) always stop filled but
unsent: review `status`, then `submit` refills from the same answers and
submits. Workday-style portals that need an account, and CAPTCHAs, come back
to you with the link. When something needs you, the output lists each item
with the command to run; afterwards, `continue`.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import runlog

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parents[1] / "services"
DATA = HERE / "data"
MAX_ROUNDS = 8
LONG_FORM_MIN_MAXLENGTH = 300
DEFAULT_MAX_CHARS = 1500
SCRIPTS = {"tracker": "tracker.py", "apply-agent": "apply.py", "drafter": "draft.py", "mapper": "map_question.py",
           "answerer": "answer_question.py"}
SERVICE_DIRS = {"tracker": "tracker", "apply-agent": "apply-agent", "drafter": "answer-drafter",
                "mapper": "answer-drafter", "answerer": "answer-drafter"}
CRITERIA = HERE.parents[1] / "criteria.json"

# Hands-off Easy Apply (the user's choice): resolve everything without asking, but never guess.
AUTO_MAP_MIN = 0.85       # accept a suggested bank key for a new wording at this confidence
AUTO_ANSWER_MIN = 0.80    # submit a model-chosen screening answer at this confidence; below it, skip the job
SELF_ID_MATCH_MIN = 0.90  # self-ID: best-match option at this confidence; below it, the form's decline option
DECLINE = "I don't wish to answer"     # the agent matches it to the form's own decline option
SELF_ID = re.compile(r"gender|\brace\b|ethnic|veteran|disabilit|hispanic|latin[oa]|visible minorit|indigenous|"
                     r"aboriginal|lgbt|sexual orientation|pronoun|transgender", re.I)
AUTO_SOURCES = ("model", "draft_auto", "decline")
# Never sent to the model (the user's rule): forms get these from the bank on this machine.
CONTACT_KEYS = ("first_name", "last_name", "email", "phone", "linkedin_url", "website_url")
DECLINE_OPTION = re.compile(r"\b(decline|prefer not|don.?t wish|do not wish|not wish to|rather not|choose not|"
                            r"do not want|don.?t want|not to (say|answer|disclose|identify))", re.I)


class StepError(Exception):
    pass


def python_for(service_dir):
    venv = service_dir / "python" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def make_caller(run):
    def call(tool, args):
        sdir = SERVICES / SERVICE_DIRS[tool]
        t0 = time.monotonic()
        proc = subprocess.run([python_for(sdir), SCRIPTS[tool], *args], cwd=sdir, env=run.child_env(),
                              capture_output=True, text=True)
        err = proc.stderr.strip()[-2000:] or None
        run.event("service_call", service=SERVICE_DIRS[tool], args=_loggable(args), exit_code=proc.returncode,
                  duration_ms=round((time.monotonic() - t0) * 1000), stderr=err)
        if proc.returncode != 0:
            detail = proc.stdout.strip()[-500:] or (err.splitlines()[-1] if err else "no output")
            raise StepError(f"{SCRIPTS[tool]} exited {proc.returncode}: {detail}")
        return json.loads(proc.stdout)
    return call


def _loggable(args):
    out = list(args)
    for i, a in enumerate(out[:-1]):
        if a in ("--answer", "--value"):
            out[i + 1] = "[answer]"
    return out


def jurisdiction(location):
    loc = location or ""
    if "Canada" in loc:
        return "CA"
    return "US" if loc else None


def kind_of(field):
    if field.get("resume"):
        return "resume"
    if field["type"] == "file":
        return "file"
    if field["type"] == "textarea" or (field["type"] == "text" and (field.get("maxlength") or 0) >= LONG_FORM_MIN_MAXLENGTH):
        return "long_form"
    return "profile"


class Applier:
    def __init__(self, call, app_id, run, dry_run=False, headless=False, approved=False):
        self.approved = approved        # the user approved submitting a filled portal application
        self.channel = None             # easy_apply or portal, once the agent has said
        self.skip_reasons = []          # hands-off: why this job can't be submitted without guessing
        self.call = call
        self.app_id = app_id
        self.run = run
        self.dry_run = dry_run
        self.headless = headless
        self.dir = DATA / app_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def tracker(self, *args):
        return self.call("tracker", list(args))

    def app(self):
        return self.tracker("application", "show", self.app_id)

    def _write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj, indent=2) if not isinstance(obj, str) else obj)
        return str(path)

    def status(self, status, note=None, report=None):
        args = ["application", "status", self.app_id, status]
        if note:
            args += ["--note", note]
        if report is not None:
            args += ["--report", self._write("report.json", report)]
            if report.get("apply_url"):
                args += ["--apply-url", report["apply_url"]]
        self.tracker(*args)

    def hands_off(self, app):
        """Easy Apply resolves everything itself (the user's choice); portals keep the approval step."""
        return self.channel == "easy_apply" if self.channel else app["job"]["apply_type"] == "easy"

    def mode(self, apply_type):
        """Easy Apply submits on its own; a company portal only once you've approved it."""
        if self.approved:
            return "submit"
        easy = self.channel == "easy_apply" if self.channel else apply_type == "easy"
        return "submit" if easy else "fill"

    # --- resolving fields ---------------------------------------------------

    def resolve(self, fields, app):
        out = []
        hands = self.hands_off(app)
        for f in fields:
            if f.get("outcome") != "unknown":
                continue
            base = {"key": f["key"], "label": f["label"], "field_type": f["type"], "options": f.get("options"),
                    "required": f.get("required", False), "page": f.get("page"), "kind": kind_of(f)}
            if base["kind"] == "resume":
                # Never answered by the bank or the model: only the resume you chose is ever sent.
                why = f.get("error") or "couldn't select your resume"
                if hands:
                    self.skip_reasons.append(f"resume: {why}")
                out.append({**base, "kind": "file", "state": "needs_answer", "meta": {"note": why}})
            elif base["kind"] == "file":
                if hands and base["required"]:
                    self.skip_reasons.append(f"asks for an upload the agent doesn't handle ({base['label']})")
                out.append({**base, "state": "needs_answer" if base["required"] else "skipped",
                            "meta": {"note": "an upload the agent doesn't handle"}})
            elif base["kind"] == "long_form":
                if not base["required"]:
                    out.append({**base, "state": "skipped"})
                else:
                    try:
                        item = self.draft_item(base, f, app)
                        if hands:       # used as drafted; listed in the summary for you to read afterwards
                            item.update(state="approved", source="draft_auto")
                        out.append(item)
                    except StepError as e:     # you can still write it yourself
                        self.run.warn("draft_failed", application_id=self.app_id, key=f["key"], error=str(e))
                        if hands:
                            self.skip_reasons.append(f"couldn't draft an answer to {base['label']!r}")
                        out.append({**base, "state": "needs_answer", "meta": {"note": f"couldn't draft: {e}"}})
            else:
                out.append(self.profile_item(base, f, app, hands))
            self.run.event("field_resolved", application_id=self.app_id, key=f["key"], kind=base["kind"],
                           state=out[-1]["state"], source=out[-1].get("source"))
        return out

    def draft_item(self, base, field, app, steer=None, previous=None):
        job = self._write("job.json", app["job"])
        args = ["--question", base["label"], "--job", job, "--resume", app["resume"]["text_path"],
                "--max-chars", str(field.get("maxlength") or DEFAULT_MAX_CHARS)]
        similar = self.tracker("answers", "similar", "--question", base["label"])["similar"]
        if similar:
            args += ["--examples", self._write("examples.json", similar)]
        if steer:
            args += ["--steer", steer]
        if previous:
            args += ["--previous", self._write("previous.txt", previous)]
        d = self.call("drafter", args)
        return {**base, "state": "needs_review", "value": d["answer"], "source": "draft",
                "meta": {k: d[k] for k in ("resume_points", "char_count", "max_chars", "within_limit")}}

    def profile_item(self, base, field, app, hands=False):
        self_id = bool(SELF_ID.search(base["label"]))
        if field.get("error"):        # the bank's answer fit none of this form's options
            if hands:                 # the model maps your answer to the form's equivalent option
                return self.model_item(base, field, app, hint=field["error"], self_id=self_id)
            return {**base, "state": "needs_answer", "meta": {"note": field["error"], "fill_error": True}}
        r = self.tracker("bank", "resolve", base["label"])
        if r["status"] == "answered":
            return {**base, "state": "filled", "value": r["answer"], "source": "bank", "bank_key": r["key"]}
        if r["status"] in ("always_ask", "unanswered"):
            if hands:
                return self.decline_item(base) if self_id or r.get("sensitive") else self.model_item(base, field, app)
            return {**base, "state": "needs_answer", "bank_key": r["key"],
                    "meta": {"note": "always asked" if r["status"] == "always_ask" else "bank key has no answer yet"}}
        args = ["inbox", "add", "--question", base["label"], "--field-type", field["type"],
                "--application", self.app_id]
        if field.get("options"):
            args += ["--options", json.dumps(field["options"])]
        jur = jurisdiction(app["job"].get("location"))
        if jur:
            args += ["--jurisdiction", jur]
        item = self.tracker(*args)
        proposal = {"key": item.get("proposed_key"), "confidence": item.get("proposed_confidence") or 0}
        if not item.get("duplicate") and item.get("proposed_key") is None:
            proposal = self.propose(item["id"], base["label"], field.get("options")) or proposal
        if hands:
            auto = self.auto_map(item["id"], base, proposal)
            if auto:
                return auto
            if self_id:
                return {**self.decline_item(base), "inbox_id": item["id"]}
            if not base["required"]:
                return {**base, "state": "skipped", "inbox_id": item["id"]}
            return {**self.model_item(base, field, app), "inbox_id": item["id"]}
        state = "needs_answer" if base["required"] else "skipped"
        return {**base, "state": state, "inbox_id": item["id"]}

    def auto_map(self, inbox_id, base, proposal):
        """Hands-off: a confident match to an answered bank key is accepted and remembered."""
        key, conf = proposal.get("key"), proposal.get("confidence") or 0
        if not key or conf < AUTO_MAP_MIN:
            return None
        entry = self.tracker("bank", "show", key)
        if entry.get("answer") is None or entry.get("always_ask"):
            return None
        self.tracker("inbox", "resolve", str(inbox_id), "--key", key)
        self.run.event("auto_mapped", application_id=self.app_id, question=base["label"], key=key, confidence=conf)
        return {**base, "state": "filled", "value": entry["answer"], "source": "bank", "bank_key": key,
                "inbox_id": inbox_id, "meta": {"auto_mapped": key, "confidence": conf}}

    @staticmethod
    def decline_item(base):
        return {**base, "state": "approved", "value": DECLINE, "source": "decline"}

    def profile(self):
        """What the model may use to answer for you: your saved answers except contact details, and
        your job preferences."""
        entries = self.tracker("bank", "list")["entries"]
        answers = [{"question": e["question"], "answer": e["answer"]} for e in entries
                   if e["answer"] and e["key"] not in CONTACT_KEYS]
        prefs = {}
        try:
            search = json.loads(CRITERIA.read_text()).get("search", {})
            prefs = {k: search[k] for k in ("locations", "work_types", "job_types") if k in search}
        except (OSError, json.JSONDecodeError):
            pass
        return {"answers": answers, "preferences": prefs}

    def model_item(self, base, field, app, hint=None, self_id=False):
        """Hands-off: the model answers from your profile; not confident enough means skip the job.
        A self-identification question it can't map confidently gets the form's decline option."""
        question = base["label"] + (f" (your saved answer didn't fit the options: {hint})" if hint else "")
        args = ["--question", question, "--job", self._write("job.json", app["job"]),
                "--resume", app["resume"]["text_path"], "--profile", self._write("profile.json", self.profile()),
                "--field-type", field["type"]]
        if field.get("options"):
            args += ["--options", json.dumps(field["options"])]
        can_decline = self_id and any(DECLINE_OPTION.search(o or "") for o in (field.get("options") or []))
        try:
            a = self.call("answerer", args)
        except StepError as e:
            if can_decline:
                return self.decline_item(base)
            self.skip_reasons.append(f"couldn't get an answer to {base['label']!r}: {e}")
            return {**base, "state": "needs_answer", "meta": {"note": str(e)}}
        meta = {"confidence": a["confidence"], "basis": a["basis"]}
        bar = SELF_ID_MATCH_MIN if self_id else AUTO_ANSWER_MIN
        if a["answer"] is not None and a["confidence"] >= bar:
            return {**base, "state": "approved", "value": a["answer"], "source": "model", "meta": meta}
        if can_decline:
            return self.decline_item(base)
        self.skip_reasons.append(f"not confident answering {base['label']!r} ({a['confidence']:.0%}: {a['basis']})")
        return {**base, "state": "needs_answer", "meta": {**meta, "note": "not confident enough to answer for you"}}

    def propose(self, inbox_id, question, options):
        keys = self._write("keys.json", self.tracker("bank", "keys"))
        args = ["--question", question, "--keys", keys]
        if options:
            args += ["--options", json.dumps(options)]
        try:
            p = self.call("mapper", args)
        except StepError as e:          # a proposal is a convenience; the user can still answer
            self.run.warn("propose_failed", inbox_id=inbox_id, error=str(e))
            return None
        self.tracker("inbox", "propose", str(inbox_id), "--key", p["key"] or "none",
                     "--confidence", str(p["confidence"]), "--reason", p["reason"] or "")
        return p

    def reresolve(self, app):
        """Items waiting on the bank (you answered the inbox or set the key): fill them now."""
        updates = []
        for it in app["items"]:
            if it["kind"] == "profile" and it["state"] in ("needs_answer", "skipped") and \
                    (it["bank_key"] or it["inbox_id"]) and not (it.get("meta") or {}).get("fill_error"):
                r = self.tracker("bank", "resolve", it["label"])
                if r["status"] == "answered":
                    updates.append({**{k: it[k] for k in ("key", "label", "field_type", "options", "required",
                                                          "page", "kind", "inbox_id")},
                                    "state": "filled", "value": r["answer"], "source": "bank", "bank_key": r["key"]})
        if updates:
            self.tracker("application", "items", self.app_id, "--set", self._write("items.json", updates))

    # --- the loop -----------------------------------------------------------

    def process(self):
        for _ in range(MAX_ROUNDS):
            self.reresolve(self.app())
            pk = self.tracker("application", "packet", self.app_id)
            if pk["blocking"]:
                if self.hands_off(self.app()):
                    return self.skip(f"waiting on {len(pk['blocking'])} answer(s) from an earlier run")
                self.status("needs_input", f"{len(pk['blocking'])} item(s) need you")
                return self.summary()
            self.status("in_progress")
            mode = self.mode(pk["job"]["apply_type"])
            args = [self._write("packet.json", pk), "--mode", mode]
            if self.dry_run:
                args.append("--dry-run")
            if self.headless:
                args.append("--headless")
            report = self.call("apply-agent", args)
            saved = {**report, "fields": [{k: f.get(k) for k in ("key", "label", "type", "page", "required",
                                                                  "outcome", "source", "error")}
                                          for f in report.get("fields", [])]}
            s = report["status"]
            self.channel = report.get("channel") or self.channel
            if report.get("apply_url") and s == "needs_input":
                # Keep the portal URL now, so the next round goes straight there instead of via LinkedIn.
                self.status("in_progress", report=saved)
            if s == "submitted":
                self.status("submitted", report=saved)
                out = self.summary()
                if self.hands_off(self.app()):
                    auto = len(out.get("chosen_for_you", []))
                    notify("Applied", f"{out['job']}" + (f" ({auto} answer(s) chosen for you)" if auto else ""))
                return out
            if s == "ready":
                if self.dry_run:
                    self.status("ready", "every field answered; stopped before Submit (dry run)", saved)
                    return self.summary()
                if self.channel == "easy_apply" and mode == "fill":
                    continue                  # learned it's Easy Apply: go again and submit
                self.status("ready", f"filled on the company portal ({report.get('ats') or 'portal'}); review it, "
                                     f"then: python3 workflows/apply/run.py submit {self.app_id}", saved)
                return self.summary()
            if s == "needs_user":
                self.status("needs_input", report.get("note") + "; when done: services/tracker/python/bin/python "
                            f"services/tracker/tracker.py application status {self.app_id} submitted", saved)
                return self.summary()
            if s == "already_applied":
                self.status("submitted", "LinkedIn shows this job as already applied", saved)
                return self.summary()
            if s == "cap_reached":
                self.status("queued", "daily application cap reached; continue tomorrow", saved)
                return self.summary()
            if s != "needs_input":
                hint = {"login_required": "sign in: services/apply-agent/python/bin/python services/apply-agent/login.py"}
                self.status("failed", hint.get(s) or report.get("error") or s, saved)
                return self.summary()
            unknown = [f for f in report["fields"] if f.get("outcome") == "unknown"]
            app = self.app()
            if not unknown:
                note = "the form rejected a value: " + "; ".join(report.get("errors") or [])
                if self.hands_off(app):
                    return self.skip(note, saved)
                self.status("needs_input", note, saved)
                return self.summary()
            items = self.resolve(unknown, app)
            self.tracker("application", "items", self.app_id, "--set", self._write("items.json", items))
            if self.skip_reasons:
                return self.skip("; ".join(self.skip_reasons), saved)
        self.status("failed", f"no progress after {MAX_ROUNDS} rounds")
        return self.summary()

    def skip(self, why, report=None):
        """Hands-off: pass on this job rather than guess, and tell the user."""
        self.status("skipped", f"skipped: {why}", report)
        out = self.summary()
        notify("Skipped an application", f"{out['job']}: {why}"[:200])
        return out

    def regenerate(self, ref, steer=None):
        app = self.app()
        items = app["items"]
        item = items[int(ref) - 1] if str(ref).isdigit() and 1 <= int(ref) <= len(items) else \
            next((i for i in items if i["key"] == ref), None)
        if item is None or item["kind"] != "long_form":
            raise StepError(f"{ref!r} isn't a long-form item of application {self.app_id}")
        if item["state"] == "approved":
            raise StepError("that answer is already approved; edit it with tracker.py application edit")
        base = {k: item[k] for k in ("key", "label", "field_type", "options", "required", "page", "kind")}
        maxlength = (item.get("meta") or {}).get("max_chars")
        new = self.draft_item(base, {"maxlength": maxlength}, app, steer=steer, previous=item["value"])
        self.tracker("application", "items", self.app_id, "--set", self._write("items.json", [new]))
        return self.summary()

    def summary(self):
        app = self.app()
        t = "services/tracker/python/bin/python services/tracker/tracker.py"
        needs = []
        for n, it in enumerate(app["items"], 1):
            if it["state"] not in ("needs_review", "needs_answer"):
                continue
            entry = {"n": n, "label": it["label"], "state": it["state"], "required": it["required"]}
            if it["state"] == "needs_review":
                entry["draft"] = it["value"]
                entry["resume_points"] = (it.get("meta") or {}).get("resume_points")
                entry["do"] = [f"{t} application approve {self.app_id} {n}",
                               f"{t} application edit {self.app_id} {n} --value \"...\"",
                               f"python3 workflows/apply/run.py regenerate {self.app_id} {n} --steer \"...\""]
            elif it["inbox_id"]:
                entry["options"] = it["options"]
                entry["do"] = [f"{t} inbox list   # item {it['inbox_id']} shows the suggested key",
                               f"{t} inbox resolve {it['inbox_id']} --key <key> [--answer \"...\"]",
                               f"{t} inbox answer {it['inbox_id']} --answer \"...\""]
            elif it["bank_key"]:
                entry["options"] = it["options"]
                entry["do"] = [f"{t} bank set {it['bank_key']} \"...\"",
                               f"{t} application edit {self.app_id} {n} --value \"...\"   # this application only"]
            else:
                entry["options"] = it["options"]
                entry["note"] = (it.get("meta") or {}).get("note")
                entry["do"] = [f"{t} application edit {self.app_id} {n} --value \"...\""]
            needs.append(entry)
        out = {"application_id": self.app_id, "job": f"{app['job']['title']} at {app['job']['company']}",
               "status": app["status"], "note": app["note"], "needs": needs if app["status"] != "skipped" else []}
        chosen = chosen_for_you(app["items"])
        if chosen:
            out["chosen_for_you"] = chosen
        if app.get("apply_url"):
            out["apply_url"] = app["apply_url"]
        if app["status"] == "ready" and app.get("last_report"):
            # What the portal will receive, for your review before `submit`.
            values = {i["key"]: i["value"] for i in app["items"]}
            resume = f"(your resume: {app['resume']['name']})"
            out["review"] = {"fields": [{"label": f["label"], "outcome": f["outcome"], "source": f.get("source"),
                                         "value": resume if f.get("source") == "resume" else values.get(f["key"])}
                                        for f in app["last_report"]["fields"]],
                             "screenshots": app["last_report"].get("screenshots", [])}
        if needs:
            out["then"] = f"python3 workflows/apply/run.py continue {self.app_id}"
        return out


def chosen_for_you(items):
    """Answers the hands-off flow picked without you: model answers, auto-used drafts,
    declines, and new wordings matched to your saved answers."""
    out = []
    for it in items:
        meta = it.get("meta") or {}
        if it["source"] in AUTO_SOURCES or meta.get("auto_mapped"):
            how = {"model": "answered by the model", "draft_auto": "drafted from your resume",
                   "decline": "declined to answer"}.get(it["source"], f"matched to your saved {meta.get('auto_mapped')}")
            entry = {"question": it["label"], "answer": it["value"], "how": how}
            if meta.get("confidence") is not None:
                entry["confidence"] = meta["confidence"]
            if meta.get("basis"):
                entry["basis"] = meta["basis"]
            out.append(entry)
    return out


def notify(title, message):
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"],
                   capture_output=True)


def daily_summary(call, hours=24):
    """Applications touched in the last `hours`: what was submitted, what was chosen for you, what was skipped."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    lines = []
    for a in call("tracker", ["application", "list"])["applications"]:
        if datetime.fromisoformat(a["updated_at"]) < cutoff or a["status"] not in ("submitted", "skipped", "ready",
                                                                                   "needs_input"):
            continue
        app = call("tracker", ["application", "show", a["id"]])
        lines.append(f"\n{a['status'].upper():<11} {a['title']} at {a['company']}  ({a['id']})")
        if a["note"]:
            lines.append(f"            {a['note']}")
        for c in chosen_for_you(app["items"]):
            conf = f" [{c['confidence']:.0%}]" if "confidence" in c else ""
            lines.append(f"            - {c['question'][:90]}\n              → {str(c['answer'])[:160]}  "
                         f"({c['how']}{conf})")
    return "\n".join(lines).lstrip("\n") or f"No applications in the last {hours} hours."


def main(argv=None, call=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("opportunity")
    start.add_argument("--resume")
    start.add_argument("--posting", help="apply through this city's posting (see tracker.py show)")
    cont = sub.add_parser("continue")
    cont.add_argument("application")
    for p in (start, cont):
        p.add_argument("--dry-run", action="store_true", help="never click Submit")
        p.add_argument("--headless", action="store_true")
    p = sub.add_parser("submit", help="approve and submit a filled company-portal application")
    p.add_argument("application")
    p.add_argument("--headless", action="store_true")
    p = sub.add_parser("regenerate")
    p.add_argument("application")
    p.add_argument("item")
    p.add_argument("--steer")
    p = sub.add_parser("status")
    p.add_argument("application")
    p = sub.add_parser("summary", help="what was applied to, chosen for you, or skipped recently")
    p.add_argument("--hours", type=int, default=24)
    args = ap.parse_args(argv)

    with runlog.start("apply", HERE, argv) as run:
        call = call or make_caller(run)
        if args.cmd == "summary":
            print(daily_summary(call, args.hours))
            run.finish(0)
            return 0
        try:
            if args.cmd == "start":
                create = ["application", "create", args.opportunity] + (["--resume", args.resume] if args.resume else []) \
                    + (["--posting", args.posting] if args.posting else [])
                app_id = call("tracker", create)["id"]
            else:
                app_id = args.application
            applier = Applier(call, app_id, run, getattr(args, "dry_run", False), getattr(args, "headless", False),
                              approved=args.cmd == "submit")
            if args.cmd == "submit" and applier.app()["status"] != "ready":
                raise StepError("only a filled application (status ready) can be submitted; run continue first")
            if args.cmd in ("start", "continue", "submit"):
                out = applier.process()
            elif args.cmd == "regenerate":
                out = applier.regenerate(args.item, args.steer)
            else:
                out = applier.summary()
            code = 0
        except StepError as e:
            out, code = {"error": str(e)}, 1
            run.error("apply_failed", error=str(e))
        print(json.dumps(out, indent=2))
        run.finish(code, status=out.get("status"), needs=len(out.get("needs", [])))
        return code


if __name__ == "__main__":
    sys.exit(main())
