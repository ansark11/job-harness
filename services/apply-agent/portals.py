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
    ("oracle", re.compile(r"(^|\.)oraclecloud\.com$")),
]
MANUAL = {"workday", "icims", "taleo", "oracle"}
MANUAL_WHY = {"oracle": "asks you to verify your email before applying"}
EMBEDDABLE = {"greenhouse", "lever", "ashby"}      # ATSs whose form a careers page can embed in an iframe
APPLY_LINK = re.compile(r"^\s*apply( now| for this (job|role|position))?\s*$", re.I)
# Visible text, for links whose accessible name is longer ("Apply Now for MASRUSR290187").
APPLY_TEXT = re.compile(r"^\s*apply( now| for this (job|role|position)| online)?\s*$", re.I)
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


LINKEDIN = re.compile(r"(^|\.)linkedin\.com$")
# LinkedIn sometimes asks before sending you off-site ("Continue to apply", share-your-profile prompts).
CONTINUE = re.compile(r"^\s*(continue( to apply)?|apply( on company (site|website))?|yes|ok)\b", re.I)
# Overlays that sit on top of a form: cookie banners, newsletter and chat prompts, sign-up nags.
OVERLAY = ("#onetrust-banner-sdk, #onetrust-consent-sdk, #CybotCookiebotDialog, #truste-consent-track, "
           "[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], [aria-modal=true], "
           "[role=dialog], [role=alertdialog]")
ACCEPT = re.compile(r"^\s*(accept( all)?( cookies)?|allow( all)?( cookies)?|i agree|agree|got it|ok(ay)?|"
                    r"i understand|understood|reject all|decline( all)?|no,? thanks|not now|maybe later|"
                    r"close|dismiss|×|x)\s*$", re.I)


def _left_linkedin(url, start):
    """True once a URL is somewhere other than LinkedIn and other than where we started."""
    if not url or url == start or url.startswith("about:"):
        return False
    return not LINKEDIN.search((urlsplit(url).hostname or "").lower())


def resolve_apply_url(page, job_url, timeout=15000):
    """Click LinkedIn's (offsite) Apply button and return where it leads.
    None if there's no such button or it never leaves LinkedIn (never a LinkedIn URL)."""
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
    ctx, start = page.context, page.url
    before = set(ctx.pages)
    btn.click()
    deadline = time.monotonic() + timeout / 1000
    prompts = 0
    while time.monotonic() < deadline:
        for p in [p for p in ctx.pages if p not in before]:
            # A new tab: wait for it to finish redirecting off LinkedIn, then keep its URL.
            end = time.monotonic() + 10
            while not _left_linkedin(p.url, start) and time.monotonic() < end:
                p.wait_for_timeout(250)
            url = p.url
            p.close()
            return url if _left_linkedin(url, start) else None
        if _left_linkedin(page.url, start):       # navigated in the same tab
            return page.url
        if prompts < 2:
            dlg = page.locator("dialog[open], [role=dialog], [role=alertdialog]")
            if dlg.count() and dlg.first.is_visible():
                go = dlg.first.get_by_role("button", name=CONTINUE)
                if go.count() and go.first.is_visible():
                    go.first.click()
                    prompts += 1
        page.wait_for_timeout(300)
    return None


DISMISS_JS = r"""
(sel) => {
  // Overlays that hold at most a couple of inputs (a cookie banner, a newsletter box), never the form itself.
  const out = [];
  for (const box of document.querySelectorAll(sel)) {
    const r = box.getBoundingClientRect();
    if (!r.width || !r.height || getComputedStyle(box).visibility === 'hidden') continue;
    if (box.querySelectorAll('input:not([type=hidden]):not([type=checkbox]), select, textarea').length > 2) continue;
    if (box.closest('form') && box.closest('form').querySelectorAll('input, select, textarea').length > 2) continue;
    out.push(box);
  }
  out.forEach((b, i) => b.setAttribute('data-jh-overlay', String(i)));
  return out.length;
}
"""


def dismiss_overlays(page, rounds=3):
    """Close cookie banners and pop-ups covering the page. Returns how many were closed.
    Only clicks accept/close-style buttons inside overlays that aren't the application form."""
    closed = 0
    for _ in range(rounds):
        try:
            n = page.evaluate(DISMISS_JS, OVERLAY)
        except Exception:
            return closed
        hit = False
        for i in range(n):
            box = page.locator(f'[data-jh-overlay="{i}"]')
            for cand in (box.get_by_role("button", name=ACCEPT), box.get_by_role("link", name=ACCEPT),
                         box.locator("[aria-label*=close i], [aria-label*=dismiss i], [class*=close i]")):
                try:
                    if cand.count() and cand.first.is_visible():
                        cand.first.click(timeout=2000)
                        closed += 1
                        hit = True
                        break
                except Exception:
                    continue
        if not hit:
            break
        page.wait_for_timeout(400)
    return closed


NAME_FIELD = re.compile(r"\b(first|last|full|legal|given|family|sur)\s*-?name\b|^name$", re.I)
EMAIL_FIELD = re.compile(r"e-?mail", re.I)
RESUME_FIELD = re.compile(r"resume|r[ée]sum[ée]|\bcv\b|curriculum", re.I)


def looks_like_application(fields):
    named = lambda f: f"{f['label']} {f.get('key', '')}"      # noqa: E731
    if any(f["type"] == "file" and RESUME_FIELD.search(named(f)) for f in fields):
        return True
    has_name = any(f["type"] in ("text",) and NAME_FIELD.search(named(f)) for f in fields)
    has_email = any(f["type"] in ("email", "text") and EMAIL_FIELD.search(named(f)) for f in fields)
    return has_name and has_email


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
            return self.report("needs_user", note=f"{self.ats} portals {MANUAL_WHY.get(self.ats, 'need an account you create')}; "
                                                  f"apply by hand at {self.apply_url}")
        self.page.goto(form_url(self.apply_url), wait_until="domcontentloaded")
        forms.settle(self.page)
        dismiss_overlays(self.page)
        self._follow_embed()
        if not self._has_form():
            # A job page (careers site): its Apply button leads to the form, often on another system.
            self._click_apply_link()
            forms.settle(self.page)
            landed = detect(self.page.url)
            if landed != "generic":
                self.ats = landed
            if self.ats in MANUAL:
                self.apply_url = self.page.url
                return self.report("needs_user", note=f"{self.ats} portals {MANUAL_WHY.get(self.ats, 'need an account you create')}; "
                                                      f"apply by hand at {self.page.url}")
            dismiss_overlays(self.page)
            self._follow_embed()
        if not self._has_form():
            return self.report("error", error="no application form found on the portal page")
        return None

    def _follow_embed(self):
        """A careers page that embeds its ATS form: open the form itself. Only when this page has no
        form of its own, and only a frame hosted by the ATS (a Google helper frame on Greenhouse
        merely mentions greenhouse.io in its address)."""
        if self._has_form():
            return
        for frame in self.page.locator("iframe").all():
            src = urljoin(self.page.url, frame.get_attribute("src") or "")
            if detect(src) in EMBEDDABLE:
                self.ats = detect(src)
                self.page.goto(src, wait_until="domcontentloaded")
                forms.settle(self.page)
                return

    def _has_form(self):
        """An application form, not just inputs: a resume upload, or name and email fields together.
        A careers site's job-search bar or job-alert email box doesn't count."""
        try:
            self.page.locator("input:not([type=hidden]), textarea, select").first.wait_for(state="attached",
                                                                                            timeout=4000)
        except Exception:
            return False
        return looks_like_application(forms.extract_fields(self.page))

    def _apply_controls(self):
        by_name = self.page.get_by_role("link", name=APPLY_LINK).or_(self.page.get_by_role("button", name=APPLY_LINK))
        by_text = self.page.locator("a:visible, button:visible, [role=button]:visible").filter(has_text=APPLY_TEXT)
        return by_name.or_(by_text)

    def _click_apply_link(self):
        # Careers sites render their Apply button late; give it a moment to appear.
        try:
            self._apply_controls().first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        cand = self._apply_controls()
        if cand.count():
            before = set(self.page.context.pages)
            cand.first.click()
            self.page.wait_for_timeout(1500)
            new = [p for p in self.page.context.pages if p not in before]
            if new:      # the form opened in a new tab: bring it into this one
                try:
                    new[0].wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                url = new[0].url
                for p in new:
                    p.close()
                self.page.goto(url, wait_until="domcontentloaded")
            else:
                self.page.wait_for_load_state("domcontentloaded")
            return

    # --- filling and submitting ---------------------------------------------

    def fill(self):
        unknown = 0
        fields = forms.extract_fields(self.page, probe=True)
        for f in fields:
            f["page"] = 1
            self.fields.append(f)
        resume = next((f for f in fields if f["type"] == "file" and RESUME_FIELD.search(f"{f['label']} {f['key']}")),
                      None) or next((f for f in fields if f["type"] == "file"), None)
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
        # A page still starting up, or re-rendering after an answer, can clear what was typed.
        lost = forms.refill_lost(self.page, [(f, f["filled"]) for f in fields if f.get("outcome") == "filled"
                                             and f.get("source") == "packet"])
        for f, _ in lost:
            f["outcome"], f["error"] = "unknown", "the page kept clearing this field"
            unknown += 1
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
