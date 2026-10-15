"""The answer bank: reusable answers to application questions.

Applications ask the same things in different words ("Will you require visa
sponsorship?" / "Do you now or in the future need sponsorship to work in
the US?"). Each canonical question has a key; every wording seen in the
wild becomes an alias of a key once the user confirms it, so the next
application resolves it deterministically, without the model.

Resolution order for a question: exact canonical wording -> alias -> unknown
(goes to the inbox, where answer-drafter can propose a key and the user
confirms or answers). Keys marked always_ask are never auto-filled.

keys() returns key + wording only (for matching new wordings); contact
details are never sent to the model by any caller.
"""

import json
import re
from collections import Counter

from store import TrackerError, _iso, utcnow

DECLINE = "I don't wish to answer"

# (key, group, jurisdiction, sensitive, question, options)
SEED = [
    ("first_name", "contact", "any", 0, "First name", None),
    ("last_name", "contact", "any", 0, "Last name", None),
    ("email", "contact", "any", 0, "Email", None),
    ("phone", "contact", "any", 0, "Phone number", None),
    ("city", "contact", "any", 0, "Current city / location", None),
    ("linkedin_url", "contact", "any", 0, "LinkedIn profile URL", None),
    ("website_url", "contact", "any", 0, "Website or portfolio URL", None),

    ("current_company", "background", "any", 0, "Current company", None),
    ("current_title", "background", "any", 0, "Current title", None),
    ("school", "background", "any", 0, "School / university", None),
    ("degree", "background", "any", 0, "Degree", None),
    ("field_of_study", "background", "any", 0, "Field of study / discipline", None),

    ("authorized_ca", "work_authorization", "any", 0,
     "Are you legally authorized to work in Canada?", ["Yes", "No"]),
    ("authorized_us", "work_authorization", "any", 0,
     "Are you legally authorized to work in the United States?", ["Yes", "No"]),
    ("sponsorship_ca", "work_authorization", "any", 0,
     "Will you now or in the future require sponsorship to work in Canada?", ["Yes", "No"]),
    ("sponsorship_us", "work_authorization", "any", 0,
     "Will you now or in the future require visa sponsorship to work in the United States?", ["Yes", "No"]),
    ("relocation", "logistics", "any", 0, "Are you willing to relocate?", ["Yes", "No", "Depends on the role"]),
    ("notice_period", "logistics", "any", 0, "What is your notice period / earliest start date?", None),
    ("salary_expectation_cad", "logistics", "any", 0, "What are your salary expectations (CAD)?", None),
    ("salary_expectation_usd", "logistics", "any", 0, "What are your salary expectations (USD)?", None),
    ("years_pm_experience", "logistics", "any", 0, "How many years of product management experience do you have?", None),
    ("how_heard", "logistics", "any", 0, "How did you hear about this role?", ["LinkedIn"]),
    ("previously_employed", "logistics", "any", 0, "Have you previously worked for this company?", ["Yes", "No"]),

    ("gender_us", "eeo_us", "US", 1, "Gender",
     ["Male", "Female", "Non-binary", DECLINE]),
    ("hispanic_latino_us", "eeo_us", "US", 1, "Are you Hispanic or Latino?", ["Yes", "No", DECLINE]),
    ("race_us", "eeo_us", "US", 1, "Race / ethnicity",
     ["American Indian or Alaska Native", "Asian", "Black or African American",
      "Native Hawaiian or Other Pacific Islander", "White", "Two or more races", DECLINE]),
    ("veteran_us", "eeo_us", "US", 1, "Veteran status",
     ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
      DECLINE]),
    ("disability_us", "eeo_us", "US", 1, "Disability status",
     ["Yes, I have a disability (or previously had a disability)", "No, I do not have a disability",
      DECLINE]),

    ("gender_ca", "employment_equity_ca", "CA", 1, "Gender identity",
     ["Man", "Woman", "Non-binary", "Prefer to self-describe", "Prefer not to say"]),
    ("visible_minority_ca", "employment_equity_ca", "CA", 1, "Do you identify as a member of a visible minority?",
     ["Yes", "No", "Prefer not to say"]),
    ("indigenous_ca", "employment_equity_ca", "CA", 1,
     "Do you identify as an Indigenous person (First Nations, Métis or Inuit)?", ["Yes", "No", "Prefer not to say"]),
    ("disability_ca", "employment_equity_ca", "CA", 1, "Do you identify as a person with a disability?",
     ["Yes", "No", "Prefer not to say"]),
    ("lgbtq_ca", "employment_equity_ca", "CA", 1, "Do you identify as 2SLGBTQI+?",
     ["Yes", "No", "Prefer not to say"]),
]


def normalize(question):
    """Lowercase, drop required-markers and punctuation, collapse spaces."""
    q = (question or "").lower()
    q = re.sub(r"\(required\)|\(optional\)|\*", " ", q)
    q = re.sub(r"[^\w\s]", " ", q)
    return " ".join(q.split())


def seed(conn):
    """Insert canonical questions that aren't present yet. Never touches answers."""
    with conn:
        for key, grp, jur, sens, question, options in SEED:
            conn.execute("INSERT OR IGNORE INTO answer_bank (key, question, grp, options, jurisdiction, sensitive) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (key, question, grp, json.dumps(options) if options else None,
                                                       jur, sens))


def _entry(row):
    return {"key": row["key"], "question": row["question"], "group": row["grp"],
            "options": json.loads(row["options"]) if row["options"] else None,
            "jurisdiction": row["jurisdiction"], "sensitive": bool(row["sensitive"]),
            "always_ask": bool(row["always_ask"]), "answer": row["answer"], "custom": bool(row["custom"])}


def list_entries(conn, group=None, unanswered=False):
    seed(conn)
    q, args = "SELECT * FROM answer_bank WHERE 1=1", []
    if group:
        q += " AND grp = ?"
        args.append(group)
    if unanswered:
        q += " AND answer IS NULL"
    return [_entry(r) for r in conn.execute(q + " ORDER BY rowid", args)]


def get(conn, key):
    seed(conn)
    row = conn.execute("SELECT * FROM answer_bank WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise TrackerError(f"no answer-bank key {key!r}")
    return _entry(row)


def set_answer(conn, key, answer, always_ask=None, now=None):
    get(conn, key)
    with conn:
        conn.execute("UPDATE answer_bank SET answer = ?, updated_at = ? WHERE key = ?",
                     (answer, _iso(now or utcnow()), key))
        if always_ask is not None:
            conn.execute("UPDATE answer_bank SET always_ask = ? WHERE key = ?", (int(always_ask), key))
    return get(conn, key)


def keys(conn):
    """Key + canonical wording + group: safe to give the model. No answers."""
    seed(conn)
    return [{"key": r["key"], "question": r["question"], "group": r["grp"]}
            for r in conn.execute("SELECT key, question, grp FROM answer_bank ORDER BY rowid")]


def resolve(conn, question):
    """{'status': 'answered'|'always_ask'|'unanswered'|'unknown', ...} without any model call."""
    seed(conn)
    norm = normalize(question)
    row = conn.execute("SELECT * FROM answer_bank").fetchall()
    hit = next((r for r in row if normalize(r["question"]) == norm), None)
    via = "exact"
    if hit is None:
        alias = conn.execute("SELECT key FROM answer_aliases WHERE question_norm = ?", (norm,)).fetchone()
        if alias:
            hit = conn.execute("SELECT * FROM answer_bank WHERE key = ?", (alias["key"],)).fetchone()
            via = "alias"
    if hit is None:
        return {"status": "unknown", "question": question}
    e = _entry(hit)
    base = {"key": e["key"], "via": via, "sensitive": e["sensitive"], "options": e["options"]}
    if e["always_ask"]:
        return {"status": "always_ask", **base}
    if e["answer"] is None:
        return {"status": "unanswered", **base}
    return {"status": "answered", "answer": e["answer"], **base}


def add_alias(conn, question, key, now=None):
    get(conn, key)
    with conn:
        conn.execute("INSERT OR REPLACE INTO answer_aliases VALUES (?, ?, ?, ?)",
                     (normalize(question), key, question, _iso(now or utcnow())))


# --- inbox: questions waiting for the user -----------------------------------

def _inbox(row):
    out = dict(row)
    out["options"] = json.loads(row["options"]) if row["options"] else None
    del out["question_norm"]
    return out


def inbox_add(conn, question, field_type=None, options=None, jurisdiction=None, application_id=None, now=None):
    """Queue an unknown question; an open duplicate (same normalized wording) is returned instead."""
    norm = normalize(question)
    existing = conn.execute("SELECT * FROM inbox WHERE question_norm = ? AND status = 'open'", (norm,)).fetchone()
    if existing:
        return {**_inbox(existing), "duplicate": True}
    with conn:
        cur = conn.execute(
            "INSERT INTO inbox (question, question_norm, field_type, options, jurisdiction, application_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (question, norm, field_type, json.dumps(options) if options else None, jurisdiction, application_id,
             _iso(now or utcnow())))
    return inbox_get(conn, cur.lastrowid)


def inbox_get(conn, item_id):
    row = conn.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise TrackerError(f"no inbox item {item_id}")
    return _inbox(row)


def inbox_list(conn, status="open"):
    q, args = "SELECT * FROM inbox", ()
    if status:
        q, args = q + " WHERE status = ?", (status,)
    return [_inbox(r) for r in conn.execute(q + " ORDER BY id", args)]


def inbox_propose(conn, item_id, key, confidence, reason=None):
    """Record the drafter's suggested key. Unknown keys are refused, not trusted."""
    if key is not None:
        get(conn, key)
    inbox_get(conn, item_id)
    with conn:
        conn.execute("UPDATE inbox SET proposed_key = ?, proposed_confidence = ?, proposed_reason = ? WHERE id = ?",
                     (key, confidence, reason, item_id))
    return inbox_get(conn, item_id)


def _close(conn, item_id, status, key, now):
    conn.execute("UPDATE inbox SET status = ?, resolved_key = ?, resolved_at = ? WHERE id = ?",
                 (status, key, _iso(now or utcnow()), item_id))


def inbox_resolve(conn, item_id, key, answer=None, now=None):
    """The question means `key`: alias it (and set the answer if given or still missing)."""
    item = inbox_get(conn, item_id)
    if item["status"] != "open":
        raise TrackerError(f"inbox item {item_id} is already {item['status']}")
    entry = get(conn, key)
    if answer is not None:
        set_answer(conn, key, answer, now=now)
    elif entry["answer"] is None and not entry["always_ask"]:
        raise TrackerError(f"{key} has no answer yet: pass --answer")
    add_alias(conn, item["question"], key, now)
    with conn:
        _close(conn, item_id, "answered", key, now)
    return {"inbox": inbox_get(conn, item_id), "entry": get(conn, key)}


def _slug(question, taken):
    base = "_".join(normalize(question).split()[:6])[:48] or "question"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}_{n}", n + 1
    return slug


def inbox_answer(conn, item_id, answer, key=None, sensitive=False, now=None):
    """A genuinely new question: create a custom bank entry from it and answer it."""
    item = inbox_get(conn, item_id)
    if item["status"] != "open":
        raise TrackerError(f"inbox item {item_id} is already {item['status']}")
    taken = {r["key"] for r in conn.execute("SELECT key FROM answer_bank")}
    key = key or _slug(item["question"], taken)
    if key in taken:
        raise TrackerError(f"key {key!r} exists: use inbox resolve to map to it")
    with conn:
        conn.execute("INSERT INTO answer_bank (key, question, grp, options, jurisdiction, sensitive, answer, custom, "
                     "updated_at) VALUES (?, ?, 'custom', ?, ?, ?, ?, 1, ?)",
                     (key, item["question"], json.dumps(item["options"]) if item["options"] else None,
                      item["jurisdiction"] or "any", int(sensitive), answer, _iso(now or utcnow())))
        _close(conn, item_id, "answered", key, now)
    return {"inbox": inbox_get(conn, item_id), "entry": get(conn, key)}


def inbox_dismiss(conn, item_id, now=None):
    inbox_get(conn, item_id)
    with conn:
        _close(conn, item_id, "dismissed", None, now)
    return inbox_get(conn, item_id)


# --- approved long-form answers ---------------------------------------------

def approved_add(conn, question, answer, job_id=None, company=None, now=None):
    with conn:
        cur = conn.execute("INSERT INTO approved_answers (question, answer, job_id, company, approved_at) "
                           "VALUES (?, ?, ?, ?, ?)", (question, answer, job_id, company, _iso(now or utcnow())))
    return {"id": cur.lastrowid, "question": question}


def approved_list(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM approved_answers ORDER BY id DESC")]


STOP = set("a an the to of and or for in on at with your you do does did is are what why how this that "
           "would be as about we our us me my i".split())


def _tokens(text):
    return Counter(w for w in normalize(text).split() if w not in STOP)


def approved_similar(conn, question, limit=3, min_overlap=0.2):
    """Past approved answers to similar questions, best first (token overlap, no model)."""
    q = _tokens(question)
    scored = []
    for r in approved_list(conn):
        t = _tokens(r["question"])
        union = sum((q | t).values())
        overlap = sum((q & t).values()) / union if union else 0
        if overlap >= min_overlap:
            scored.append({**r, "similarity": round(overlap, 2)})
    return sorted(scored, key=lambda r: r["similarity"], reverse=True)[:limit]
