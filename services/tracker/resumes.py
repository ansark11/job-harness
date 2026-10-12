"""Resume versions: a stored copy of each file plus its extracted text.

The file is what gets uploaded to applications; the text is what the
matcher, answer-drafter and form filling read. The text can come from a
different file (text_source): e.g. upload the PDF but read the Word
version, which extracts more reliably. The first resume added becomes the
default; adding the same upload file twice returns the existing record.
"""

import hashlib
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import db
from store import TrackerError, _iso, utcnow

SUPPORTED = {".pdf", ".docx", ".txt", ".md"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def resume_dir():
    return db.path().parent / "resumes"


def extract_text(path):
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        text = path.read_text(errors="replace")
    elif ext == ".docx":
        text = _docx_text(path)
    elif ext == ".pdf":
        text = _pdf_text(path)
    else:
        raise TrackerError(f"unsupported resume type {ext} (use {', '.join(sorted(SUPPORTED))})")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) < 200:
        raise TrackerError(f"only {len(text)} characters of text found in {path.name}; "
                           "is it a scanned image? Export a text-based PDF or DOCX")
    return text


def _docx_text(path):
    try:
        with zipfile.ZipFile(path) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError) as e:
        raise TrackerError(f"{path.name} isn't a readable .docx: {e}") from e
    lines = []
    for p in root.iter(f"{W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _pdf_text(path):
    try:
        import pypdf
    except ImportError as e:
        raise TrackerError("PDF resumes need pypdf: run tracker with ./python/bin/python "
                           "(python3 -m venv python && python/bin/pip install -r requirements.txt)") from e
    try:
        reader = pypdf.PdfReader(str(path))
        pages = []
        for page in reader.pages:
            # Layout mode keeps lines whole; plain mode splits some exports (Google Docs)
            # into one word per line. Fall back to plain if layout finds nothing.
            try:
                text = page.extract_text(extraction_mode="layout")
            except Exception:
                text = ""
            if not text.strip():
                text = page.extract_text() or ""
            # A wide gap inside a line is a column break (e.g. Education | Certifications side by side).
            pages.append("\n".join(re.sub(r"\s{4,}", " | ", line.strip()) for line in text.splitlines() if line.strip()))
        return "\n\n".join(pages)
    except pypdf.errors.PdfReadError as e:
        raise TrackerError(f"{path.name} isn't a readable PDF: {e}") from e


def _record(row):
    return {k: row[k] for k in ("id", "name", "file_path", "text_path", "added_at")} | \
        {"is_default": bool(row["is_default"]), "text_source": row["text_source"] or row["file_path"]}


def add(conn, src, name=None, make_default=False, text_from=None, now=None):
    src = Path(src).expanduser()
    if not src.is_file():
        raise TrackerError(f"no such file: {src}")
    if src.suffix.lower() not in SUPPORTED:
        raise TrackerError(f"unsupported resume type {src.suffix} (use {', '.join(sorted(SUPPORTED))})")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    existing = conn.execute("SELECT * FROM resumes WHERE sha256 = ?", (sha,)).fetchone()
    if existing:
        if text_from:
            set_text_source(conn, existing["id"], text_from)
        if make_default:
            set_default(conn, existing["id"])
        return {**get(conn, existing["id"]), "already_added": True}

    text = extract_text(text_from or src)
    rid = uuid.uuid4().hex[:8]
    d = resume_dir()
    d.mkdir(parents=True, exist_ok=True)
    file_path = d / f"{rid}{src.suffix.lower()}"
    text_path = d / f"{rid}.txt"
    shutil.copy2(src, file_path)
    text_path.write_text(text)
    with conn:
        first = conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0
        conn.execute("INSERT INTO resumes (id, name, file_path, text_path, sha256, is_default, added_at) "
                     "VALUES (?, ?, ?, ?, ?, 0, ?)",
                     (rid, name or src.stem, str(file_path), str(text_path), sha, _iso(now or utcnow())))
    if first or make_default:
        set_default(conn, rid)
    if text_from:
        _store_text_source(conn, rid, text_from)
    rec = get(conn, rid)
    return {**rec, "characters": len(text), "preview": text[:300]}


def _store_text_source(conn, rid, src):
    src = Path(src).expanduser()
    dest = resume_dir() / f"{rid}-text{src.suffix.lower()}"
    shutil.copy2(src, dest)
    with conn:
        conn.execute("UPDATE resumes SET text_source = ? WHERE id = ?", (str(dest), rid))


def set_text_source(conn, rid, src):
    """Read this resume's text from another file (the upload file is unchanged)."""
    rec = get(conn, rid)
    src = Path(src).expanduser()
    if not src.is_file():
        raise TrackerError(f"no such file: {src}")
    text = extract_text(src)
    _store_text_source(conn, rid, src)
    Path(rec["text_path"]).write_text(text)
    return {**get(conn, rid), "characters": len(text), "preview": text[:300]}


def remove(conn, rid):
    """Forget a resume version. Refused if any application used it."""
    rec = get(conn, rid)
    used = conn.execute("SELECT COUNT(*) FROM applications WHERE resume_id = ?", (rid,)).fetchone()[0]
    if used:
        raise TrackerError(f"resume {rid} was used by {used} application(s); it's kept for their record")
    with conn:
        conn.execute("DELETE FROM resumes WHERE id = ?", (rid,))
    for p in {rec["file_path"], rec["text_path"], rec["text_source"]}:
        Path(p).unlink(missing_ok=True)
    return {"removed": rid, "was_default": rec["is_default"]}


def refresh(conn, rid):
    """Re-extract a stored resume's text (after an extraction fix)."""
    rec = get(conn, rid)
    text = extract_text(rec["text_source"])
    Path(rec["text_path"]).write_text(text)
    return {**rec, "characters": len(text), "preview": text[:300]}


def list_all(conn):
    return [_record(r) for r in conn.execute("SELECT * FROM resumes ORDER BY added_at DESC")]


def get(conn, rid):
    row = conn.execute("SELECT * FROM resumes WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise TrackerError(f"no resume {rid}")
    return _record(row)


def default(conn):
    row = conn.execute("SELECT * FROM resumes WHERE is_default = 1").fetchone()
    if row is None:
        raise TrackerError("no default resume: add one with tracker.py resume add <file>")
    return _record(row)


def set_default(conn, rid):
    get(conn, rid)
    with conn:
        conn.execute("UPDATE resumes SET is_default = (id = ?)", (rid,))
    return get(conn, rid)
