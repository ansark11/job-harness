"""External application portals (the company's own ATS page).

The same form engine as Easy Apply (forms.py), on one long page instead of a
dialog. Per-ATS knowledge is limited to URLs: where the application form
lives (Lever: /apply, Ashby: /application), and iframes that embed one
(a company careers page wrapping a Greenhouse form). Portals that require
creating an account (Workday, iCIMS, Taleo) are left to the user.

External applications always stop for the user's approval: mode "fill"
fills everything and reports "ready" with a full-page screenshot; mode
"submit" refills from the same frozen answers, submits, and waits for the
portal's confirmation. A CAPTCHA in a headed browser is left for the user
to solve while the agent waits.
"""

import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import forms

ATS = [
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$")),
    ("lever", re.compile(r"(^|\.)lever\.co$")),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$")),
    ("workday", re.compile(r"(^|\.)(myworkdayjobs|workday)\.com$")),
    ("icims", re.compile(r"(^|\.)icims\.com$")),
    ("taleo", re.compile(r"(^|\.)taleo\.net$")),
    ("smartrecruiters", re.compile(r"(^|\.)smartrecruiters\.com$")),
]
MANUAL = {"workday", "icims", "taleo"}
EMBED = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com", re.I)
APPLY_LINK = re.compile(r"^\s*apply( now| for this (job|role|position))?\s*$", re.I)
SUBMIT = re.compile(r"^\s*submit( (my )?application)?\s*$", re.I)
SUCCESS = re.compile(r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?"
                     r"(submitted|received)|we('ve| have) received your application", re.I)
CAPTCHA = "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare']"


def detect(url):
    host = (urlsplit(url).hostname or "").lower()
    for name, pattern in ATS:
        if pattern.search(host):
            return name
    return "generic"


def form_url(url):
    """Where the application form itself lives, for ATSs that split posting and form."""
    p = urlsplit(url)
    path = p.path.rstrip("/")
    ats = detect(url)
    if ats == "lever" and not path.endswith("/apply"):
        path += "/apply"
    elif ats == "ashby" and not path.endswith("/application"):
        path += "/application"
    else:
        return url
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


def resolve_apply_url(page, job_url, timeout=15000):
    """Click LinkedIn's (offsite) Apply button and return where it leads. None if there's no such button."""
    page.goto(job_url, wait_until="domcontentloaded")
    if re.search(r"/(login|authwall|checkpoint)", page.url):
        return "login_required"
    btn = None
    for role in ("button", "link"):
        cand = page.get_by_role(role, name=re.compile(r"^apply\b(?!.*easy)", re.I))
        if cand.count():
            btn = cand.first
            break
    if btn is None:
        return None
    try:
        with page.context.expect_page(timeout=timeout) as info:
            btn.click()
        popup = info.value
        popup.wait_for_load_state("domcontentloaded")
        url = popup.url
        popup.close()
        return url
    except Exception:
        # Some postings navigate in the same tab instead of opening one.
        page.wait_for_load_state("domcontentloaded")
        return page.url if page.url != job_url else None


class PortalWalk:
    def __init__(self, page, packet, mode, dry_run, shots_dir=None, pause=(0.3, 0.9), success_wait=20):
        self.page = page
        self.packet = packet
        self.mode = mode
        self.dry_run = dry_run
        self.shots_dir = Path(shots_dir) if shots_dir else None
        self.pause = pause
        self.success_wait = success_wait
        self.answers = packet.get("answers") or {}
        self.skip = set(packet.get("skip") or [])
        self.fields = []
        self.screenshots = []
        self.apply_url = packet["job"].get("apply_url")
        self.ats = None

    def _wait(self):
        if self.pause:
            time.sleep(random.uniform(*self.pause))

    def _shot(self, name, full=True):
        if self.shots_dir:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            path = self.shots_dir / f"{len(self.screenshots) + 1:02d}-{name}.png"
            self.page.screenshot(path=str(path), full_page=full)
            self.screenshots.append(str(path))

    def report(self, status, **extra):
        return {"status": status, "fields": self.fields, "screenshots": self.screenshots, "ats": self.ats,
                "apply_url": self.apply_url,
                "unknown": [f["key"] for f in self.fields if f.get("outcome") == "unknown"], **extra}

    # --- getting to the form ------------------------------------------------

    def open(self):
        if not self.apply_url:
            url = resolve_apply_url(self.page, self.packet["job"]["url"])
            if url == "login_required":
                return self.report("login_required", note="LinkedIn wants a sign-in: run login.py")
            if not url:
                return self.report("error", error="couldn't find the job's Apply button on LinkedIn")
            self.apply_url = url
        self.ats = detect(self.apply_url)
        if self.ats in MANUAL:
            return self.report("needs_user", note=f"{self.ats} portals need an account you create; "
                                                  f"apply by hand at {self.apply_url}")
        self.page.goto(form_url(self.apply_url), wait_until="domcontentloaded")
        self._follow_embed()
        if not self._has_form():
            self._click_apply_link()
        if not self._has_form():
            return self.report("error", error="no application form found on the portal page")
        return None

    def _follow_embed(self):
        for frame in self.page.locator("iframe").all():
            src = urljoin(self.page.url, frame.get_attribute("src") or "")
            if EMBED.search(src):
                self.ats = detect(src)
                self.page.goto(src, wait_until="domcontentloaded")
                return

    def _has_form(self):
        try:
            self.page.locator("input:not([type=hidden]), textarea, select").first.wait_for(state="attached",
                                                                                            timeout=4000)
        except Exception:
            return False
        return any(f["type"] in ("file", "text", "email", "tel", "textarea") for f in forms.extract_fields(self.page))

    def _click_apply_link(self):
        for role in ("link", "button"):
            cand = self.page.get_by_role(role, name=APPLY_LINK)
            if cand.count():
                cand.first.click()
                self.page.wait_for_load_state("domcontentloaded")
                return

    # --- filling and submitting ---------------------------------------------

    def fill(self):
        unknown = 0
        fields = forms.extract_fields(self.page)
        for f in fields:
            f["page"] = 1
            self.fields.append(f)
        resume = next((f for f in fields if f["type"] == "file" and re.search(r"resume|cv", f["label"], re.I)), None) \
            or next((f for f in fields if f["type"] == "file"), None)
        for f in fields:
            if f is resume:
                forms.fill_field(self.page, f, self.packet["resume_file"])
                f.update(outcome="filled", source="resume", filled=Path(self.packet["resume_file"]).name)
                continue
            key = f["key"]
            if key in self.answers:
                try:
                    f["filled"] = forms.fill_field(self.page, f, self.answers[key])
                    f["outcome"], f["source"] = "filled", "packet"
                except forms.FillError as e:
                    f["outcome"], f["error"] = "unknown", str(e)
                    unknown += 1
            elif key in self.skip:
                f["outcome"] = "skipped"
            elif f.get("value") not in (None, "", False, []):
                f["outcome"], f["source"] = "prefilled", "portal"
            else:
                f["outcome"] = "unknown"
                unknown += 1
            self._wait()
        return unknown

    def page_errors(self):
        loc = self.page.locator("[role=alert], .error, .field-error, [class*=error-message]")
        return list(dict.fromkeys(e.inner_text().strip() for e in loc.all() if e.is_visible() and e.inner_text().strip()))

    def submit(self):
        btn = None
        for cand in (self.page.get_by_role("button", name=SUBMIT), self.page.locator("[type=submit]")):
            if cand.count():
                btn = cand.first
                break
        if btn is None:
            return self.report("error", error="no Submit button on the portal page")
        btn.click()
        deadline = time.monotonic() + self.success_wait
        captcha_seen = False
        while time.monotonic() < deadline:
            if SUCCESS.search(self.page.locator("body").inner_text()):
                self._shot("confirmation", full=False)
                return self.report("submitted")
            if self.page.locator(CAPTCHA).count() and any(f.is_visible() for f in self.page.locator(CAPTCHA).all()):
                captcha_seen = True
            time.sleep(0.5)
        self._shot("after-submit")
        if captcha_seen:
            return self.report("needs_user", note=f"a CAPTCHA blocked submission; finish it at {self.apply_url}")
        errors = self.page_errors()
        if errors:
            return self.report("needs_input", errors=errors)
        return self.report("error", error="clicked Submit but saw no confirmation; check the portal")

    def run(self, cap_ok=lambda: True):
        early = self.open()
        if early:
            return early
        unknown = self.fill()
        self._shot("filled")
        if unknown:
            return self.report("needs_input")
        if self.mode != "submit" or self.dry_run:
            return self.report("ready", note="filled; waiting for your approval" if self.mode != "submit"
                               else "filled; not submitted (dry run)")
        if not cap_ok():
            return self.report("cap_reached")
        return self.submit()
