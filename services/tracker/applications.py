"""Applications: one per kept opportunity, and the answer behind every form field.

Item states:
  filled        answered from the bank; goes in the packet
  needs_review  a drafted long-form answer waiting for the user's approval
  needs_answer  the user must answer (inbox, or `application edit`)
  approved      the user approved or wrote it; goes in the packet
  skipped       optional and unanswered: left blank

The packet (what the apply-agent receives) contains only filled and approved
values; anything else blocks submission.
"""

import json
import uuid

import bank
import resumes
from store import TrackerError, _iso, utcnow

STATUSES = ("queued", "in_progress", "needs_input", "ready", "submitted", "failed")
STATES = ("filled", "needs_review", "needs_answer", "approved", "skipped")
BLOCKING = ("needs_review", "needs_answer")
UPLOADABLE = (".pdf", ".docx")


def _app(row):
    out = dict(row)
    out["last_report"] = json.loads(row["last_report"]) if row["last_report"] else None
    return out


def _item(row):
    out = dict(row)
    out["options"] = json.loads(row["options"]) if row["options"] else None
    out["meta"] = json.loads(row["meta"]) if row["meta"] else None
    out["required"] = bool(row["required"])
    return out


def create(conn, opportunity_id, resume_id=None, now=None):
    """One live application per opportunity: an unfinished or submitted one is returned instead."""
    now = now or utcnow()
    opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    if opp is None:
        raise TrackerError(f"no opportunity {opportunity_id}")
    if opp["status"] != "kept":
        raise TrackerError(f"opportunity {opportunity_id} is {opp['status']}; keep it before applying")
    existing = conn.execute("SELECT * FROM applications WHERE opportunity_id = ? AND status != 'failed' "
                            "ORDER BY created_at DESC LIMIT 1", (opportunity_id,)).fetchone()
    if existing:
        return {**get(conn, existing["id"]), "existing": True}
    resume = resumes.get(conn, resume_id) if resume_id else resumes.default(conn)
    if not resume["file_path"].lower().endswith(UPLOADABLE):
        raise TrackerError(f"resume {resume['id']} ({resume['name']}) isn't a PDF or DOCX; "
                           "application portals only accept those. Add one with resume add")
    app_id = uuid.uuid4().hex[:10]
    with conn:
        conn.execute("INSERT INTO applications (id, opportunity_id, resume_id, apply_type, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (app_id, opportunity_id, resume["id"], opp["apply_type"], _iso(now), _iso(now)))
    return get(conn, app_id)


def get(conn, app_id):
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise TrackerError(f"no application {app_id}")
    app = _app(row)
    opp = conn.execute("SELECT id, title, company, url, apply_type, data FROM opportunities WHERE id = ?",
                       (row["opportunity_id"],)).fetchone()
    data = json.loads(opp["data"])
    app["job"] = {"id": opp["id"], "title": opp["title"], "company": opp["company"], "url": opp["url"],
                  "apply_type": opp["apply_type"], "location": data.get("location"),
                  "description": data.get("description")}
    app["resume"] = resumes.get(conn, row["resume_id"])
    app["items"] = items(conn, app_id)
    app["blocking"] = [i["key"] for i in app["items"] if i["state"] in BLOCKING]
    return app


def list_all(conn, status=None):
    q, args = ("SELECT a.*, o.title, o.company FROM applications a JOIN opportunities o ON o.id = a.opportunity_id",
               ())
    if status:
        q, args = q + " WHERE a.status = ?", (status,)
    return [{k: r[k] for k in ("id", "opportunity_id", "title", "company", "status", "note", "apply_type",
                               "updated_at", "submitted_at")} for r in conn.execute(q + " ORDER BY a.updated_at DESC",
                                                                                    args)]


def items(conn, app_id):
    return [_item(r) for r in conn.execute("SELECT * FROM application_items WHERE application_id = ? ORDER BY seq",
                                           (app_id,))]


def upsert_items(conn, app_id, new_items, now=None):
    """Insert or update items by key. Never overwrites an approved item unless the update is approved too."""
    get(conn, app_id)
    now = _iso(now or utcnow())
    with conn:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM application_items WHERE application_id = ?",
                           (app_id,)).fetchone()[0]
        for it in new_items:
            if it["state"] not in STATES:
                raise TrackerError(f"bad item state {it['state']!r}")
            cur = conn.execute("SELECT state, seq FROM application_items WHERE application_id = ? AND key = ?",
                               (app_id, it["key"])).fetchone()
            if cur and cur["state"] == "approved" and it["state"] != "approved":
                continue
            row = (it.get("label") or it["key"], it.get("field_type"),
                   json.dumps(it["options"]) if it.get("options") else None, int(bool(it.get("required"))),
                   it.get("page"), it.get("kind", "profile"), it["state"], it.get("value"), it.get("source"),
                   it.get("bank_key"), it.get("inbox_id"), json.dumps(it["meta"]) if it.get("meta") else None, now)
            if cur:
                conn.execute("UPDATE application_items SET label=?, field_type=?, options=?, required=?, page=?, "
                             "kind=?, state=?, value=?, source=?, bank_key=?, inbox_id=?, meta=?, updated_at=? "
                             "WHERE application_id = ? AND key = ?", (*row, app_id, it["key"]))
            else:
                seq += 1
                conn.execute("INSERT INTO application_items (label, field_type, options, required, page, kind, state, "
                             "value, source, bank_key, inbox_id, meta, updated_at, application_id, key, seq) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (*row, app_id, it["key"], seq))
    return items(conn, app_id)


def _find_item(conn, app_id, ref):
    """An item by key or by its 1-based position in `application show`."""
    its = items(conn, app_id)
    if str(ref).isdigit() and 1 <= int(ref) <= len(its):
        return its[int(ref) - 1]
    for it in its:
        if it["key"] == ref:
            return it
    raise TrackerError(f"application {app_id} has no item {ref!r}")


def _approve(conn, app_id, item, value, source, now):
    with conn:
        conn.execute("UPDATE application_items SET state = 'approved', value = ?, source = ?, updated_at = ? "
                     "WHERE application_id = ? AND key = ?", (value, source, _iso(now), app_id, item["key"]))
    if item["kind"] == "long_form":
        app = conn.execute("SELECT opportunity_id FROM applications WHERE id = ?", (app_id,)).fetchone()
        opp = conn.execute("SELECT company FROM opportunities WHERE id = ?", (app["opportunity_id"],)).fetchone()
        bank.approved_add(conn, item["label"], value, app["opportunity_id"], opp["company"], now)


def approve(conn, app_id, ref, now=None):
    now = now or utcnow()
    item = _find_item(conn, app_id, ref)
    if item["state"] == "approved":
        return item
    if item["value"] in (None, ""):
        raise TrackerError(f"{item['label']!r} has no value to approve: use application edit")
    _approve(conn, app_id, item, item["value"], item["source"], now)
    return _find_item(conn, app_id, item["key"])


def edit(conn, app_id, ref, value, now=None):
    """The user's own value: approved as written."""
    now = now or utcnow()
    item = _find_item(conn, app_id, ref)
    _approve(conn, app_id, item, value, "user", now)
    return _find_item(conn, app_id, item["key"])


def set_status(conn, app_id, status, note=None, report=None, apply_url=None, now=None):
    if status not in STATUSES:
        raise TrackerError(f"bad application status {status!r}")
    get(conn, app_id)
    now = _iso(now or utcnow())
    with conn:
        if apply_url:
            conn.execute("UPDATE applications SET apply_url = ? WHERE id = ?", (apply_url, app_id))
        conn.execute("UPDATE applications SET status = ?, note = ?, updated_at = ?, "
                     "last_report = COALESCE(?, last_report), "
                     "submitted_at = CASE WHEN ? = 'submitted' THEN ? ELSE submitted_at END WHERE id = ?",
                     (status, note, now, json.dumps(report) if report is not None else None, status, now, app_id))
    return {k: v for k, v in get(conn, app_id).items() if k not in ("items", "job")}


def packet(conn, app_id):
    """What the apply-agent may use: approved and bank-filled values, and fields to leave blank."""
    app = get(conn, app_id)
    answers, skip = {}, []
    for it in app["items"]:
        if it["state"] in ("filled", "approved"):
            answers[it["key"]] = it["value"]
        elif it["state"] == "skipped":
            skip.append(it["key"])
    ext = app["resume"]["file_path"].rsplit(".", 1)[-1]
    return {"application_id": app_id,
            "job": {**{k: app["job"][k] for k in ("id", "url", "apply_type")}, "apply_url": app["apply_url"]},
            "resume_file": app["resume"]["file_path"], "resume_upload_name": f"{app['resume']['name']}.{ext}",
            "answers": answers, "skip": skip, "blocking": app["blocking"]}
