#!/usr/bin/env python3
"""Tracker CLI: every command prints JSON.

Opportunities and runs
    tracker.py ingest matches.json [--run-id ID]
    tracker.py list [--status new|kept|discarded|expired] [--group-by fetch-date] [--pretty]
    tracker.py show <id>
    tracker.py keep|discard <id> [<id> ...]
    tracker.py expire
    tracker.py dedupe                           # merge one role posted in several cities (unreviewed only)
    tracker.py run start --kind K | finish <run id> --status ok|failed [--stats JSON] | last-success --kind K

Resumes
    tracker.py resume add <file> [--name NAME] [--default] [--text-from <file>]   # upload file; text source
    tracker.py resume list | show <id> [--text] | default [<id>] | refresh <id>
    tracker.py resume text <id> <file>          # read this resume's text from another file
    tracker.py resume remove <id>

Answer bank (reusable answers to application questions)
    tracker.py bank onboard                     # interactive: answer the standard questions once
    tracker.py bank list [--group G] [--unanswered]
    tracker.py bank show <key> | set <key> <answer> [--always-ask | --auto]
    tracker.py bank keys                        # keys + wording only (safe to give the model)
    tracker.py bank resolve "<question>"

Inbox (questions an application asked that the bank couldn't answer)
    tracker.py inbox list [--status open|answered|dismissed|all]
    tracker.py inbox add --question Q [--field-type T] [--options JSON] [--jurisdiction CA|US] [--application ID]
    tracker.py inbox propose <id> --key K --confidence C [--reason R]
    tracker.py inbox resolve <id> --key K [--answer A]     # same question as an existing key
    tracker.py inbox answer <id> --answer A [--key K] [--sensitive]   # a new question
    tracker.py inbox dismiss <id>

Applications (driven by workflows/apply/run.py; these are the data and the review actions)
    tracker.py application create <opportunity id> [--resume <id>] [--posting <city's posting id>]
    tracker.py application list [--status S] | show <id> | packet <id>
    tracker.py application approve <id> <item>             # item = key or its number in `show`
    tracker.py application edit <id> <item> --value V | --value-file F
    tracker.py application items <id> --set items.json      # workflow use
    tracker.py application status <id> <status> [--note N] [--report F] [--apply-url U]   # workflow use

Approved long-form answers
    tracker.py answers add --question Q --answer A [--job-id ID] [--company C]
    tracker.py answers list | similar --question Q [--limit N]

The database is data/jobs.db (TRACKER_DB overrides it). Exit 1 on a bad
request, with {"error": ...}.
"""

import argparse
import json
import sys
from pathlib import Path

import applications
import bank
import db
import resumes
import runlog
import store

HERE = Path(__file__).resolve().parent


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="add matcher output")
    p.add_argument("matches", help="matcher output JSON ('-' for stdin)")
    p.add_argument("--run-id")
    p = sub.add_parser("list")
    p.add_argument("--status", choices=["new", "kept", "discarded", "expired"])
    p.add_argument("--group-by", choices=["fetch-date"])
    p.add_argument("--pretty", action="store_true", help="readable table instead of JSON")
    p = sub.add_parser("show")
    p.add_argument("id")
    for name in ("keep", "discard"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")
    sub.add_parser("expire", help=f"expire postings unreviewed for {store.EXPIRE_DAYS} days")
    sub.add_parser("dedupe", help="merge one role posted in several cities")

    p = sub.add_parser("run", help="record daily runs")
    s = p.add_subparsers(dest="sub", required=True)
    r = s.add_parser("start")
    r.add_argument("--kind", required=True)
    r = s.add_parser("finish")
    r.add_argument("id")
    r.add_argument("--status", required=True, choices=["ok", "failed"])
    r.add_argument("--stats", default="{}")
    r = s.add_parser("last-success")
    r.add_argument("--kind", required=True)

    p = sub.add_parser("resume")
    s = p.add_subparsers(dest="sub", required=True)
    r = s.add_parser("add")
    r.add_argument("file")
    r.add_argument("--name")
    r.add_argument("--default", action="store_true")
    r.add_argument("--text-from", help="read the text from this file instead (e.g. the .docx of a PDF)")
    s.add_parser("list")
    r = s.add_parser("text")
    r.add_argument("id")
    r.add_argument("file")
    r = s.add_parser("remove")
    r.add_argument("id")
    r = s.add_parser("show")
    r.add_argument("id")
    r.add_argument("--text", action="store_true")
    r = s.add_parser("default")
    r.add_argument("id", nargs="?")
    r = s.add_parser("refresh", help="re-extract a resume's text")
    r.add_argument("id")

    p = sub.add_parser("bank")
    s = p.add_subparsers(dest="sub", required=True)
    s.add_parser("onboard")
    r = s.add_parser("list")
    r.add_argument("--group")
    r.add_argument("--unanswered", action="store_true")
    r = s.add_parser("show")
    r.add_argument("key")
    r = s.add_parser("set")
    r.add_argument("key")
    r.add_argument("answer")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--always-ask", dest="always_ask", action="store_true", default=None)
    g.add_argument("--auto", dest="always_ask", action="store_false")
    s.add_parser("keys")
    r = s.add_parser("resolve")
    r.add_argument("question")

    p = sub.add_parser("inbox")
    s = p.add_subparsers(dest="sub", required=True)
    r = s.add_parser("list")
    r.add_argument("--status", default="open", choices=["open", "answered", "dismissed", "all"])
    r = s.add_parser("add")
    r.add_argument("--question", required=True)
    r.add_argument("--field-type")
    r.add_argument("--options", help="JSON list")
    r.add_argument("--jurisdiction", choices=["CA", "US"])
    r.add_argument("--application")
    r = s.add_parser("propose")
    r.add_argument("id", type=int)
    r.add_argument("--key", required=True, help="'none' if no key fits")
    r.add_argument("--confidence", type=float, required=True)
    r.add_argument("--reason")
    r = s.add_parser("resolve")
    r.add_argument("id", type=int)
    r.add_argument("--key", required=True)
    r.add_argument("--answer")
    r = s.add_parser("answer")
    r.add_argument("id", type=int)
    r.add_argument("--answer", required=True)
    r.add_argument("--key")
    r.add_argument("--sensitive", action="store_true")
    r = s.add_parser("dismiss")
    r.add_argument("id", type=int)

    p = sub.add_parser("application")
    s = p.add_subparsers(dest="sub", required=True)
    r = s.add_parser("create")
    r.add_argument("opportunity")
    r.add_argument("--resume")
    r.add_argument("--posting", help="apply through this city's posting (see `show`); default: the main one")
    r = s.add_parser("list")
    r.add_argument("--status", choices=applications.STATUSES)
    for name in ("show", "packet"):
        r = s.add_parser(name)
        r.add_argument("id")
    r = s.add_parser("approve")
    r.add_argument("id")
    r.add_argument("item")
    r = s.add_parser("edit")
    r.add_argument("id")
    r.add_argument("item")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--value")
    g.add_argument("--value-file")
    r = s.add_parser("items")
    r.add_argument("id")
    r.add_argument("--set", required=True, help="JSON list of items ('-' for stdin)")
    r = s.add_parser("status")
    r.add_argument("id")
    r.add_argument("status", choices=applications.STATUSES)
    r.add_argument("--note")
    r.add_argument("--report", help="JSON file with the agent's report")
    r.add_argument("--apply-url", help="the company portal, once resolved")

    p = sub.add_parser("answers")
    s = p.add_subparsers(dest="sub", required=True)
    r = s.add_parser("add")
    r.add_argument("--question", required=True)
    r.add_argument("--answer", required=True)
    r.add_argument("--job-id")
    r.add_argument("--company")
    s.add_parser("list")
    r = s.add_parser("similar")
    r.add_argument("--question", required=True)
    r.add_argument("--limit", type=int, default=3)
    return ap


def cmd_opportunities(conn, args, run):
    if args.cmd == "ingest":
        text = sys.stdin.read() if args.matches == "-" else Path(args.matches).read_text()
        data = json.loads(text)
        matches = data["matches"] if isinstance(data, dict) else data
        result = store.ingest(conn, matches, run_id=args.run_id)
        run.event("ingest_result", **{k: len(v) for k, v in result.items()}, input_digest=runlog.digest(matches))
        return result
    if args.cmd == "list":
        items = store.list_opportunities(conn, args.status)
        if args.pretty:
            return Text(pretty_list(store.group_by_fetch_date(items)))
        return {"groups": store.group_by_fetch_date(items)} if args.group_by else {"items": items}
    if args.cmd == "show":
        return store.get(conn, args.id)
    if args.cmd in ("keep", "discard"):
        to = "kept" if args.cmd == "keep" else "discarded"
        results = [store.set_status(conn, i, to) for i in args.ids]
        for r in results:
            run.event("status_change", **r)
        return {"results": results}
    if args.cmd == "dedupe":
        out = store.dedupe(conn)
        run.event("dedupe", merged=len(out["merged"]))
        return out
    if args.cmd == "expire":
        ids = store.expire(conn)
        run.event("expired", count=len(ids), ids=ids)
        return {"expired": ids}
    if args.sub == "start":
        return store.run_start(conn, args.kind)
    if args.sub == "finish":
        return store.run_finish(conn, args.id, args.status, json.loads(args.stats))
    return {"last_success": store.last_success(conn, args.kind)}


class Text(str):
    """A command result to print as-is rather than as JSON."""


def pretty_list(groups):
    lines = []
    for g in groups:
        expiry = f" · expires in {g['expires_in_days']} days" if g["items"][0]["status"] == "new" else ""
        lines.append(f"\n== {g['label']} ({len(g['items'])}){expiry}")
        for i in g["items"]:
            score = "  -" if i["score"] is None else f"{i['score']:>3}"
            apply = "Easy Apply" if i["apply_type"] == "easy" else "portal" if i["apply_type"] == "external" else "?"
            lines.append(f"  {i['id']}  {score}  {(i['title'] or '')[:46]:<46}  {(i['company'] or '')[:22]:<22}  "
                         f"{apply:<10}  {' / '.join(i['locations'])[:44]}")
            if i["flags"]:
                lines.append(f"  {'':10}       ⚑ {', '.join(i['flags'])}")
    if not lines:
        return "Nothing here."
    return "\n".join(lines).lstrip("\n") + "\n\nid  fit  role  company  how to apply  location(s)"


def cmd_resume(conn, args, run):
    if args.sub == "add":
        rec = resumes.add(conn, args.file, args.name, args.default, args.text_from)
        run.event("resume_added", id=rec["id"], resume_name=rec["name"], characters=rec.get("characters"),
                  already_added=rec.get("already_added", False))
        return rec
    if args.sub == "list":
        return {"resumes": resumes.list_all(conn)}
    if args.sub == "show":
        rec = resumes.get(conn, args.id)
        return {**rec, "text": Path(rec["text_path"]).read_text()} if args.text else rec
    if args.sub == "refresh":
        return resumes.refresh(conn, args.id)
    if args.sub == "text":
        return resumes.set_text_source(conn, args.id, args.file)
    if args.sub == "remove":
        return resumes.remove(conn, args.id)
    return resumes.set_default(conn, args.id) if args.id else resumes.default(conn)


def onboard(conn, ask=input, say=lambda s: print(s, file=sys.stderr)):
    """Walk the unanswered standard questions. Enter skips, q stops. Prompts go to stderr."""
    answered, skipped = [], []
    say("Answer each question once; it's reused on every application. Enter = skip, q = stop.\n"
        "Self-identification answers stay on this machine and are never sent to the model.")
    group = None
    for e in bank.list_entries(conn, unanswered=True):
        if e["always_ask"]:
            continue
        if e["group"] != group:
            group = e["group"]
            say(f"\n== {group.replace('_', ' ')}")
        say(f"\n{e['question']}" + (f"   [{e['jurisdiction']} applications]" if e["jurisdiction"] != "any" else ""))
        if e["options"]:
            for i, o in enumerate(e["options"], 1):
                say(f"  {i}. {o}")
        raw = ask("> ").strip()
        if raw.lower() == "q":
            break
        if not raw:
            skipped.append(e["key"])
            continue
        if e["options"] and raw.isdigit() and 1 <= int(raw) <= len(e["options"]):
            raw = e["options"][int(raw) - 1]
        bank.set_answer(conn, e["key"], raw)
        answered.append(e["key"])
    remaining = [e["key"] for e in bank.list_entries(conn, unanswered=True)]
    return {"answered": answered, "skipped": skipped, "still_unanswered": remaining}


def cmd_bank(conn, args, run):
    if args.sub == "onboard":
        out = onboard(conn)
        run.event("bank_onboard", answered=len(out["answered"]), unanswered=len(out["still_unanswered"]))
        return out
    if args.sub == "list":
        return {"entries": bank.list_entries(conn, args.group, args.unanswered)}
    if args.sub == "show":
        return bank.get(conn, args.key)
    if args.sub == "set":
        entry = bank.set_answer(conn, args.key, args.answer, args.always_ask)
        run.event("bank_set", key=args.key, sensitive=entry["sensitive"])     # never the answer itself
        return entry
    if args.sub == "keys":
        return {"keys": bank.keys(conn)}
    out = bank.resolve(conn, args.question)
    run.event("bank_resolve", question=args.question, status=out["status"], key=out.get("key"), via=out.get("via"))
    return out


def cmd_inbox(conn, args, run):
    if args.sub == "list":
        return {"items": bank.inbox_list(conn, None if args.status == "all" else args.status)}
    if args.sub == "add":
        item = bank.inbox_add(conn, args.question, args.field_type, json.loads(args.options) if args.options else None,
                              args.jurisdiction, args.application)
        run.event("inbox_add", id=item["id"], question=args.question, duplicate=item.get("duplicate", False))
        return item
    if args.sub == "propose":
        key = None if args.key == "none" else args.key
        return bank.inbox_propose(conn, args.id, key, args.confidence, args.reason)
    if args.sub == "resolve":
        out = bank.inbox_resolve(conn, args.id, args.key, args.answer)
        run.event("inbox_resolved", id=args.id, key=args.key, how="alias")
        return out
    if args.sub == "answer":
        out = bank.inbox_answer(conn, args.id, args.answer, args.key, args.sensitive)
        run.event("inbox_resolved", id=args.id, key=out["entry"]["key"], how="new key")
        return out
    return bank.inbox_dismiss(conn, args.id)


def cmd_application(conn, args, run):
    if args.sub == "create":
        app = applications.create(conn, args.opportunity, args.resume, posting_id=args.posting)
        run.event("application_created", id=app["id"], opportunity=args.opportunity, existing=app.get("existing", False))
        return app
    if args.sub == "list":
        return {"applications": applications.list_all(conn, args.status)}
    if args.sub == "show":
        return applications.get(conn, args.id)
    if args.sub == "packet":
        return applications.packet(conn, args.id)
    if args.sub == "approve":
        item = applications.approve(conn, args.id, args.item)
        run.event("item_approved", application_id=args.id, key=item["key"], kind=item["kind"])
        return item
    if args.sub == "edit":
        value = Path(args.value_file).read_text().strip() if args.value_file else args.value
        item = applications.edit(conn, args.id, args.item, value)
        run.event("item_edited", application_id=args.id, key=item["key"], kind=item["kind"])
        return item
    if args.sub == "items":
        text = sys.stdin.read() if args.set == "-" else Path(args.set).read_text()
        return {"items": applications.upsert_items(conn, args.id, json.loads(text))}
    report = json.loads(Path(args.report).read_text()) if args.report else None
    out = applications.set_status(conn, args.id, args.status, args.note, report, args.apply_url)
    run.event("application_status", id=args.id, status=args.status, note=args.note)
    return out


def cmd_answers(conn, args, run):
    if args.sub == "add":
        return bank.approved_add(conn, args.question, args.answer, args.job_id, args.company)
    if args.sub == "list":
        return {"answers": bank.approved_list(conn)}
    return {"similar": bank.approved_similar(conn, args.question, args.limit)}


HANDLERS = {"resume": cmd_resume, "bank": cmd_bank, "inbox": cmd_inbox, "answers": cmd_answers,
            "application": cmd_application}


def loggable_argv(argv):
    """argv with answers masked: the run log records the command line, and
    bank answers include self-identification."""
    out = list(argv)
    if out[:2] == ["bank", "set"] and len(out) > 3:
        out[3] = "[answer]"
    for i, a in enumerate(out[:-1]):
        if a in ("--answer", "--value"):
            out[i + 1] = "[answer]"
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(argv)
    with runlog.start("tracker", HERE, loggable_argv(argv)) as run:
        conn = db.connect()
        try:
            out, code = HANDLERS.get(args.cmd, cmd_opportunities)(conn, args, run), 0
        except store.TrackerError as e:
            out, code = {"error": str(e)}, 1
            run.warn("bad_request", error=str(e))
        finally:
            conn.close()
        print(out if isinstance(out, Text) else json.dumps(out, indent=2))
        run.finish(code)
        return code


if __name__ == "__main__":
    sys.exit(main())
