"""LinkedIn's public (logged-out) job pages: build URLs, parse responses.

Two guest endpoints, no login, so searching never touches the user's account:

  seeMoreJobPostings/search   10 result cards per page (start=0,10,20...)
  jobPosting/{id}             one posting: description, criteria, apply type

Facts learned from live responses (Oct 2026), pinned by tests/fixtures:
- A card carries id (data-entity-urn), title, company, location, logo and
  <time datetime="YYYY-MM-DD"> plus relative text ("1 hour ago").
- Easy Apply is marked by data-tracking-control-name="public_jobs_apply-link-onsite";
  external applications by "...apply-link-offsite...". The external URL itself
  is not in the guest page: apply-agent resolves it while logged in.
- Pay is not a structured field; it appears in the description text (often as
  several per-location ranges), so the matcher extracts it.
"""

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit

import htmltree

BASE = "https://www.linkedin.com/jobs-guest/jobs/api"
PAGE_SIZE = 10
MIN_WINDOW = 24 * 3600
MAX_WINDOW = 30 * 24 * 3600

WORK_TYPES = {"onsite": "1", "remote": "2", "hybrid": "3"}
JOB_TYPES = {"full-time": "F", "part-time": "P", "contract": "C", "temporary": "T", "internship": "I"}

AGO_RE = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
AGO_UNITS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}


def window_seconds(since, now):
    """Seconds to search back: since the last successful run, clamped to 24h..30d."""
    if since is None:
        return MIN_WINDOW
    return int(min(MAX_WINDOW, max(MIN_WINDOW, (now - since).total_seconds())))


def search_url(keywords, location, window, work_types=(), job_types=(), start=0):
    params = {"keywords": keywords, "location": location, "f_TPR": f"r{window}"}
    if work_types:
        params["f_WT"] = ",".join(WORK_TYPES[w] for w in work_types)
    if job_types:
        params["f_JT"] = ",".join(JOB_TYPES[j] for j in job_types)
    params["start"] = start
    return f"{BASE}/seeMoreJobPostings/search?{urlencode(params)}"


def posting_url(job_id):
    return f"{BASE}/jobPosting/{job_id}"


def clean_url(url):
    """Drop tracking query strings: keep scheme, host and path."""
    if not url:
        return None
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _job_id(urn):
    m = re.search(r"jobPosting:(\d+)", urn or "")
    return m.group(1) if m else None


def parse_ago(text, now):
    """'8 hours ago' -> estimated datetime; None if unrecognised."""
    m = AGO_RE.search(text or "")
    if not m:
        return None
    return now - timedelta(seconds=int(m.group(1)) * AGO_UNITS[m.group(2).lower()])


def parse_search(html):
    """Result cards from one search page. An empty list means no more pages."""
    cards = []
    for card in htmltree.parse(html).find_all(cls="base-search-card"):
        job_id = _job_id(card.attrs.get("data-entity-urn"))
        if not job_id:
            continue
        title = card.find(cls="base-search-card__title")
        sub = card.find(cls="base-search-card__subtitle")
        company_link = sub.find("a") if sub else None
        loc = card.find(cls="job-search-card__location")
        link = card.find(cls="base-card__full-link")
        time_el = card.find("time")
        logo = card.find("img")
        salary = card.find(cls="job-search-card__salary-info")
        cards.append({
            "id": job_id,
            "title": title.text() if title else None,
            "company": (company_link or sub).text() if sub else None,
            "company_url": clean_url(company_link.attrs.get("href")) if company_link else None,
            "company_logo": logo.attrs.get("data-delayed-url") if logo else None,
            "location": loc.text() if loc else None,
            "posted_date": time_el.attrs.get("datetime") if time_el else None,
            "posted_ago": time_el.text() if time_el else None,
            "url": clean_url(link.attrs.get("href")) if link else None,
            "salary_text": salary.text() if salary else None,
        })
    return cards


def parse_posting(html):
    """Detail fields for one posting. Keys are absent when not on the page."""
    root = htmltree.parse(html)
    out = {}
    title = root.find(cls="top-card-layout__title")
    if title:
        out["title"] = title.text()
    org = root.find(cls="topcard__org-name-link")
    if org:
        out["company"] = org.text()
    bullet = root.find(cls="topcard__flavor--bullet")
    if bullet and "num-applicants__caption" not in bullet.classes:
        out["location"] = bullet.text()
    ago = root.find(cls="posted-time-ago__text")
    if ago:
        out["posted_ago"] = ago.text()
    applicants = root.find(cls="num-applicants__caption")
    if applicants:
        m = re.search(r"([\d,]+)", applicants.text())
        out["applicants"] = int(m.group(1).replace(",", "")) if m else None
        out["applicants_text"] = applicants.text()

    criteria = {}
    for item in root.find_all(cls="description__job-criteria-item"):
        head = item.find(cls="description__job-criteria-subheader")
        val = item.find(cls="description__job-criteria-text")
        if head and val:
            criteria[head.text().lower().replace(" ", "_")] = val.text()
    out["criteria"] = criteria

    desc = root.find(cls="show-more-less-html__markup") or root.find(cls="description__text")
    if desc:
        out["description"] = desc.block_text()

    salary = root.find(cls="compensation__salary")
    if salary:
        out["salary_text"] = salary.text()

    controls = " ".join(n.attrs.get("data-tracking-control-name", "") for n in root.find_all()
                        if "apply" in n.attrs.get("data-tracking-control-name", ""))
    if "apply-link-onsite" in controls:
        out["apply_type"] = "easy"
    elif "apply-link-offsite" in controls or any(
            "offsite-apply" in n.attrs.get("data-svg-class-name", "") for n in root.find_all("icon")):
        out["apply_type"] = "external"
    else:
        out["apply_type"] = "unknown"
    return out


def merge(card, detail, now):
    """One normalized posting from a search card plus its detail page."""
    posting = {**card, **{k: v for k, v in detail.items() if v not in (None, "", {})}}
    est = parse_ago(posting.get("posted_ago"), now)
    if est is None and posting.get("posted_date"):
        est = datetime.combine(date.fromisoformat(posting["posted_date"]), datetime.min.time(), timezone.utc)
    posting["posted_at"] = est.isoformat(timespec="minutes") if est else None
    posting.setdefault("criteria", {})
    posting.setdefault("apply_type", "unknown")
    posting["source"] = "linkedin"
    return posting


def age_days(posting, now):
    if not posting.get("posted_at"):
        return None
    return (now - datetime.fromisoformat(posting["posted_at"])).total_seconds() / 86400
