"""LinkedIn Easy Apply: walk the dialog, fill from the packet, submit.

Each page of the dialog is read with forms.extract_fields. A field is filled
when the packet has an answer for its key; a field LinkedIn prefilled from
the profile (email, phone) is kept; a key listed in `skip` is left alone.
Anything else is unknown: the walk stops there and reports every field seen
so far, so the workflow can resolve them (answer bank, drafter, inbox) and
run again. Every run starts a fresh dialog and discards it on the way out
unless it submitted.

Submission happens only in mode "submit", without --dry-run, and only when
every field on every page was answerable.
"""

import random
import re
import time
from pathlib import Path

import forms

# LinkedIn's Easy Apply is a native <dialog open> with no role attribute; older UIs used role=dialog.
DIALOG = "dialog[open], [role=dialog]"
MAX_PAGES = 12
NEXT = re.compile(r"^(next|continue|continue to next step)$", re.I)
REVIEW = re.compile(r"^review( your application)?$", re.I)
SUBMIT = re.compile(r"^submit( application)?$", re.I)
EASY_APPLY = re.compile(r"easy apply", re.I)
APPLIED = re.compile(r"^Applied \d+ \w+ ago|^Application submitted", re.I)
SENT = re.compile(r"application (was )?sent|your application was sent", re.I)
RESUME_EXT = re.compile(r"\.(pdf|docx?)\b", re.I)


class Walk:
    def __init__(self, page, packet, mode, dry_run, shots_dir=None, pause=(0.3, 0.9)):
        self.page = page
        self.packet = packet
        self.mode = mode
        self.dry_run = dry_run
        self.shots_dir = Path(shots_dir) if shots_dir else None
        self.pause = pause
        self.answers = packet.get("answers") or {}
        self.skip = set(packet.get("skip") or [])
        self.fields = []
        self.screenshots = []

    def _wait(self):
        if self.pause:
            time.sleep(random.uniform(*self.pause))

    def _shot(self, name):
        if self.shots_dir:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            path = self.shots_dir / f"{len(self.screenshots) + 1:02d}-{name}.png"
            self.page.screenshot(path=str(path), full_page=False)
            self.screenshots.append(str(path))

    def _button(self, pattern):
        """A visible dialog button whose text or aria-label matches."""
        dialog = self.page.locator(DIALOG).first
        for b in dialog.get_by_role("button").all():
            if not b.is_visible():
                continue
            names = [(b.inner_text() or "").strip(), (b.get_attribute("aria-label") or "").strip()]
            if any(pattern.match(n) for n in names if n):
                return b
        return None

    def report(self, status, **extra):
        return {"status": status, "fields": self.fields, "screenshots": self.screenshots,
                "unknown": [f["key"] for f in self.fields if f.get("outcome") == "unknown"], **extra}

    # --- the walk ---------------------------------------------------------

    def open(self):
        page = self.page
        page.goto(self.packet["job"]["url"], wait_until="domcontentloaded")
        if re.search(r"/(login|authwall|checkpoint)", page.url):
            return self.report("login_required", note="LinkedIn wants a sign-in: run login.py")
        applied = page.get_by_text(APPLIED)
        if applied.count() and applied.first.is_visible():
            return self.report("already_applied")
        btn = page.get_by_role("button", name=EASY_APPLY).first
        try:
            btn.wait_for(state="visible", timeout=10000)
        except Exception:
            return self.report("not_easy_apply", note="no Easy Apply button on the job page")
        btn.click()
        page.locator(DIALOG).first.wait_for(state="visible", timeout=10000)
        return None

    def run(self, cap_ok=lambda: True):
        early = self.open()
        if early:
            return early
        try:
            for page_no in range(1, MAX_PAGES + 1):
                self._wait()
                self._shot(f"page{page_no}")
                unknown = self.fill_page(page_no)
                if unknown:
                    return self.report("needs_input")
                submit = self._button(SUBMIT)
                if submit:
                    if self.mode != "submit" or self.dry_run:
                        return self.report("ready", note="every field answered; not submitted "
                                                         f"({'dry run' if self.dry_run else 'fill mode'})")
                    if not cap_ok():
                        return self.report("cap_reached")
                    submit.click()
                    return self.confirm_sent()
                nxt = self._button(REVIEW) or self._button(NEXT)
                if nxt is None:
                    return self.report("error", error="no Next, Review or Submit button on this page")
                nxt.click()
                self._wait()
                errors = self.page_errors()
                if errors:
                    return self.report("needs_input", errors=errors)
            return self.report("error", error=f"more than {MAX_PAGES} pages")
        finally:
            if self.page.locator(DIALOG).count() and not getattr(self, "_sent", False):
                self.discard()

    def fill_page(self, page_no):
        unknown = 0
        fields = forms.extract_fields(self.page, DIALOG)
        for f in fields:
            f["page"] = page_no
            self.fields.append(f)
        resume_fields = [f for f in fields if self._is_resume_field(f)]
        if resume_fields:
            unknown += self.choose_resume(resume_fields)
        for f in fields:
            if f in resume_fields:
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
                f["outcome"], f["source"] = "prefilled", "linkedin"
            else:
                f["outcome"] = "unknown"
                unknown += 1
            self._wait()
        return unknown

    @staticmethod
    def _is_resume_field(f):
        if f["type"] == "radio":
            return bool(f["options"]) and all(RESUME_EXT.search(o or "") for o in f["options"])
        return f["type"] == "file" and not re.search(r"cover|letter|transcript|portfolio", f["label"], re.I)

    def choose_resume(self, resume_fields):
        """Pick our resume: select it if LinkedIn already has it by name, else upload it.
        Returns the number of fields left unknown (0 or 1)."""
        name = self.packet.get("resume_upload_name") or Path(self.packet["resume_file"]).name
        cards = next((f for f in resume_fields if f["type"] == "radio"), None)
        upload = next((f for f in resume_fields if f["type"] == "file"), None)
        if cards:
            match = next((o for o in cards["options"] if name.lower() in (o or "").lower()), None)
            if match:
                try:
                    forms.fill_field(self.page, cards, match)
                    cards.update(outcome="filled", source="resume", filled=match)
                    if upload:
                        upload["outcome"] = "not_needed"
                    return 0
                except forms.FillError as e:
                    if not upload:
                        cards.update(outcome="unknown", error=f"couldn't select {match}: {e}")
                        return 1
                    # Couldn't pick the existing copy: upload ours instead, below.
        if upload:
            forms.fill_field(self.page, upload, self.packet["resume_file"])
            upload.update(outcome="filled", source="resume", filled=name)
            if cards:
                cards["outcome"] = "replaced_by_upload"
            return 0
        cards.update(outcome="unknown", error=f"{name} isn't among LinkedIn's resumes and there's no upload button")
        return 1

    def page_errors(self):
        loc = self.page.locator(DIALOG).first.locator("[role=alert], .artdeco-inline-feedback--error")
        # innerText of a hidden element is still its text, so filter on visibility.
        return list(dict.fromkeys(e.inner_text().strip() for e in loc.all() if e.is_visible() and e.inner_text().strip()))

    def confirm_sent(self):
        try:
            self.page.get_by_text(SENT).first.wait_for(state="visible", timeout=15000)
        except Exception:
            self._shot("after-submit")
            return self.report("error", error="clicked Submit but saw no confirmation; check LinkedIn")
        self._sent = True
        self._shot("sent")
        return self.report("submitted")

    def discard(self):
        """Close the dialog and discard the draft so the next run starts clean."""
        try:
            dialog = self.page.locator(DIALOG).first
            close = dialog.get_by_role("button", name=re.compile(r"dismiss|close", re.I)).first
            if close.count():
                close.click()
            confirm = self.page.get_by_role("button", name=re.compile(r"^discard$", re.I)).first
            confirm.wait_for(state="visible", timeout=3000)
            confirm.click()
        except Exception:
            pass
