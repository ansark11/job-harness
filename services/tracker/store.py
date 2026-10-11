"""Tracker behavior over jobs.db. The CLI (and later the API) are thin layers on these functions.

Review lifecycle: new -> kept | discarded; new -> expired after EXPIRE_DAYS
unreviewed. A kept or discarded posting can be switched; an expired one can
still be kept. Nothing goes back to new. Every change is recorded in
status_events.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

EXPIRE_DAYS = 7
TRANSITIONS = {
    "kept": {"new", "discarded", "expired"},
    "discarded": {"new", "kept", "expired"},
    "expired": {"new"},
}


class TrackerError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# --- opportunities ---------------------------------------------------------

def ingest(conn, matches, now=None, run_id=None):
    """Add matcher output. Idempotent on the LinkedIn id: a posting already
    present is never reset; if still unreviewed its data/score are refreshed.
    Also expires stale unreviewed postings."""
    now = now or utcnow()
    added, updated, skipped = [], [], []
    with conn:
        for m in matches:
            match = m.get("match") or {}
            row = conn.execute("SELECT status FROM opportunities WHERE id = ?", (m["id"],)).fetchone()
            fields = {"title": m.get("title"), "company": m.get("company"), "location": m.get("location"),
                      "url": m.get("url"), "company_logo": m.get("company_logo"), "posted_at": m.get("posted_at"),
                      "apply_type": m.get("apply_type"), "score": match.get("score"),
                      "fresh": int(bool(match.get("fresh"))), "data": json.dumps(m)}
            if row is None:
                conn.execute(
                    f"INSERT INTO opportunities (id, source, {', '.join(fields)}, status, fetched_at, run_id) "
                    f"VALUES (?, ?, {', '.join('?' * len(fields))}, 'new', ?, ?)",
                    (m["id"], m.get("source", "linkedin"), *fields.values(), _iso(now), run_id))
                conn.execute("INSERT INTO status_events VALUES (?, ?, NULL, 'new', 'ingested')", (m["id"], _iso(now)))
                added.append(m["id"])
            elif row["status"] == "new":
                fields.pop("fresh")      # freshness is as of first fetch
                conn.execute(f"UPDATE opportunities SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                             (*fields.values(), m["id"]))
                updated.append(m["id"])
            else:
                skipped.append(m["id"])
    expired = expire(conn, now)
    return {"added": added, "updated": updated, "skipped": skipped, "expired": expired}


def expire(conn, now=None, days=EXPIRE_DAYS):
    now = now or utcnow()
    cutoff = _iso(now - timedelta(days=days))
    with conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM opportunities WHERE status = 'new' AND fetched_at <= ?", (cutoff,))]
        for i in ids:
            _set(conn, i, "new", "expired", now, f"unreviewed for {days} days")
    return ids


def _set(conn, opp_id, frm, to, now, reason):
    conn.execute("UPDATE opportunities SET status = ?, status_changed_at = ? WHERE id = ?", (to, _iso(now), opp_id))
    conn.execute("INSERT INTO status_events VALUES (?, ?, ?, ?, ?)", (opp_id, _iso(now), frm, to, reason))


def set_status(conn, opp_id, to, now=None, reason="user"):
    now = now or utcnow()
    with conn:
        row = conn.execute("SELECT status FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
        if row is None:
            raise TrackerError(f"no opportunity {opp_id}")
        if row["status"] == to:
            return {"id": opp_id, "status": to, "changed": False}
        if row["status"] not in TRANSITIONS[to]:
            raise TrackerError(f"can't move {opp_id} from {row['status']} to {to}")
        _set(conn, opp_id, row["status"], to, now, reason)
    return {"id": opp_id, "status": to, "changed": True, "from": row["status"]}


def _summary(row):
    fetched = datetime.fromisoformat(row["fetched_at"])
    out = {k: row[k] for k in ("id", "title", "company", "location", "url", "company_logo", "posted_at",
                               "apply_type", "score", "status", "fetched_at")}
    out["fresh"] = bool(row["fresh"])
    data = json.loads(row["data"])
    out["flags"] = (data.get("match") or {}).get("flags", [])
    if row["status"] == "new":
        out["expires_at"] = _iso(fetched + timedelta(days=EXPIRE_DAYS))
    return out


def list_opportunities(conn, status=None):
    q, args = "SELECT * FROM opportunities", ()
    if status:
        q, args = q + " WHERE status = ?", (status,)
    rows = conn.execute(q + " ORDER BY fetched_at DESC, fresh DESC, score IS NULL, score DESC", args).fetchall()
    return [_summary(r) for r in rows]


def group_by_fetch_date(items, now=None, tz=None):
    """Backlog groups, newest first: [{date, label, expires_in_days, items}].
    Dates are in local time (the user's day), not UTC."""
    now = (now or utcnow()).astimezone(tz)
    today = now.date()
    groups = {}
    for it in items:
        d = datetime.fromisoformat(it["fetched_at"]).astimezone(tz).date()
        groups.setdefault(d, []).append(it)
    out = []
    for d in sorted(groups, reverse=True):
        age = (today - d).days
        label = "Today" if age == 0 else "Yesterday" if age == 1 else d.strftime("%a %b %-d")
        out.append({"date": d.isoformat(), "label": label, "expires_in_days": max(0, EXPIRE_DAYS - age),
                    "items": groups[d]})
    return out


def get(conn, opp_id):
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
    if row is None:
        raise TrackerError(f"no opportunity {opp_id}")
    out = {**json.loads(row["data"]), "status": row["status"], "fetched_at": row["fetched_at"],
           "status_changed_at": row["status_changed_at"]}
    out["history"] = [dict(r) for r in conn.execute(
        "SELECT at, from_status, to_status, reason FROM status_events WHERE opportunity_id = ? ORDER BY rowid",
        (opp_id,))]
    return out


# --- runs ------------------------------------------------------------------

def run_start(conn, kind, now=None):
    run_id = uuid.uuid4().hex[:12]
    with conn:
        conn.execute("INSERT INTO runs (id, kind, started_at) VALUES (?, ?, ?)", (run_id, kind, _iso(now or utcnow())))
    return {"id": run_id, "kind": kind}


def run_finish(conn, run_id, status, stats=None, now=None):
    if status not in ("ok", "failed"):
        raise TrackerError(f"bad run status {status!r}")
    with conn:
        cur = conn.execute("UPDATE runs SET finished_at = ?, status = ?, stats = ? WHERE id = ?",
                           (_iso(now or utcnow()), status, json.dumps(stats or {}), run_id))
    if cur.rowcount == 0:
        raise TrackerError(f"no run {run_id}")
    return {"id": run_id, "status": status}


def last_success(conn, kind):
    row = conn.execute("SELECT * FROM runs WHERE kind = ? AND status = 'ok' ORDER BY finished_at DESC LIMIT 1",
                       (kind,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "stats": json.loads(row["stats"] or "{}")}
