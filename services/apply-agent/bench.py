#!/usr/bin/env python3
"""Portal benchmark: how well the form engine handles real employer application pages.

    ./python/bin/python bench.py run targets.json [--out DIR] [--headless] [--limit N] [--no-resolve]
    ./python/bin/python bench.py replay DIR          # re-read saved pages offline (after changing forms.py)

targets.json: [{"id", "company", "title", "url" (LinkedIn job), "apply_url" (optional)}, ...]
              or {"targets": [...]}. Built by workflows/portal_bench.

For each target:
  1. Resolve:  find the company portal behind LinkedIn's Apply button (signed-in
               profile, slow pauses). Cached in data/bench/resolved.json, so a
               posting is only ever clicked once.
  2. Classify: which applicant-tracking system (ATS) runs the portal, from the
               host name and, failing that, the page's own markup.
  3. Audit:    in a fresh browser with no cookies, open the form the same way
               the agent does (portals.PortalWalk.open) and measure:
                 coverage    controls on the page the engine didn't find
                 fill        each field filled with FAKE test data, then read back
                 follow-ups  fields that only appear after an answer
                 blockers    CAPTCHA, multi-page flow, no Submit button,
                             fields the browser still marks invalid

Safety: never clicks Submit. Uses a made-up test person and a placeholder PDF,
never your answers or resume. While filling, requests that could send data
(anything but GET, except search/autocomplete lookups) are blocked and listed.

Output in --out (default data/bench/<timestamp>/): results.json, report.md,
and per target: page.html, aria.txt, filled.png, trace.zip (open with
`./python/bin/python -m playwright show-trace trace.zip`).
"""

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import forms
import portals
import runlog

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
BENCH = DATA / "bench"
RESOLVED = BENCH / "resolved.json"
PROFILE = DATA / "browser-profile"

# Host names of applicant-tracking systems, beyond the ones portals.py fills.
ATS_HOSTS = [
    ("greenhouse", r"greenhouse\.io"), ("lever", r"lever\.co"), ("ashby", r"ashbyhq\.com"),
    ("workday", r"myworkdayjobs\.com|myworkdaysite\.com|workday\.com"), ("icims", r"icims\.com"),
    ("taleo", r"taleo\.net"), ("smartrecruiters", r"smartrecruiters\.com"),
    ("successfactors", r"successfactors\.(com|eu)|jobs\.sap\.com|sapsf\.(com|eu)"),
    ("oracle", r"oraclecloud\.com"), ("jobvite", r"jobvite\.com"), ("workable", r"workable\.com"),
    ("bamboohr", r"bamboohr\.com"), ("rippling", r"rippling\.com"), ("dayforce", r"dayforcehcm\.com"),
    ("eightfold", r"eightfold\.ai"), ("phenom", r"phenompeople\.com"), ("avature", r"avature\.net"),
    ("jazzhr", r"applytojob\.com"), ("recruitee", r"recruitee\.com"), ("teamtailor", r"teamtailor\.com"),
    ("ukg", r"ultipro\.com|ukg\.net"), ("paylocity", r"paylocity\.com"), ("breezy", r"breezy\.hr"),
]
# The same systems recognised from markup, for careers pages on the company's own domain.
ATS_MARKUP = [
    ("greenhouse", r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|grnhse"),
    ("lever", r"jobs\.lever\.co"), ("ashby", r"ashbyhq\.com"), ("workday", r"myworkdayjobs|wd\d\.myworkday"),
    ("icims", r"icims\.com"), ("taleo", r"taleo\.net"), ("smartrecruiters", r"smartrecruiters\.com"),
    ("successfactors", r"successfactors|sapsf"), ("oracle", r"oraclecloud\.com|CandidateExperience"),
    ("phenom", r"phenom(people)?\.com|phenom-"), ("eightfold", r"eightfold\.ai"), ("avature", r"avature"),
    ("jobvite", r"jobvite"), ("workable", r"workable\.com"),
]

# A made-up applicant. Nothing of yours is ever typed into a portal by the benchmark.
PERSONA = [
    # Realistic answers a form can accept, so a failure is the engine's, not the test data's.
    (r"gender|race|ethnic|hispanic|latin|veteran|disabilit|pronoun|lgbt|sexual", "I don't wish to answer"),
    (r"sponsor", "No"),
    (r"authori[sz]ed|eligible|legally|right to work", "Yes"),
    (r"country", "Canada"),
    (r"degree", "Bachelor's Degree"),
    (r"state|province", "Ontario"),
    (r"how did you hear|\bsource\b|\breferr", "LinkedIn"),
    (r"first.?name|given name|prenom", "Bench"),
    (r"last.?name|surname|family name", "Tester"),
    (r"^(full )?name$|your name|legal name", "Bench Tester"),
    (r"e.?mail", "bench.tester@example.com"),
    (r"phone|mobile|telephone", "4165550123"),
    (r"linkedin", "https://www.linkedin.com/in/bench-tester-example"),
    (r"website|portfolio|github|url", "https://example.com"),
    (r"city|location|address|where .*(live|based)", "Toronto"),
    (r"postal|zip", "M5V 2T6"),
    (r"salary|compensation|pay", "100000"),
    (r"years", "5"),
    (r"company|employer", "Example Corp"),
    (r"title|position|role", "Product Manager"),
    (r"school|university|college", "University of Toronto"),
]
LONG_TEXT = "Automated form check. Not a real application."
NO_DATA_OUT = re.compile(r"autocomplete|places|geocod|location|suggest|search|typeahead|lookup", re.I)
NEXT = re.compile(r"^\s*(next|continue|save (and|&) continue|next step)\b", re.I)

CENSUS_JS = r"""
() => {
  // Every control a person could interact with, in the form's area of the page, that the
  // engine did not tag with data-jh-id. Reaches into open shadow roots.
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const tagged = el => !!(el.closest('[data-jh-id]') || el.querySelector('[data-jh-id]'));
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (r.width > 0 && r.height > 0) return true;
    const l = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return !!(l && l.getClientRects().length);
  };
  const SEL = 'input, select, textarea, [role=combobox], [role=listbox], [role=radiogroup], [role=radio], ' +
              '[role=checkbox], [role=switch], [role=textbox], [role=spinbutton], [contenteditable=""], ' +
              '[contenteditable=true]';
  const SKIP_TYPES = ['hidden', 'submit', 'button', 'reset', 'image', 'search'];
  const anyTagged = document.querySelector('[data-jh-id]');
  const form = anyTagged && anyTagged.closest('form');
  const scope = form || document.body;
  const chrome = el => !form && el.closest('nav, header, footer, [role=search], [role=navigation], [role=banner]');
  const labelOf = el => {
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = clean(by.split(/\s+/).map(i => { const n = document.getElementById(i); return n ? n.innerText : ''; }).join(' ')); if (t) return t; }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    for (let n = el, i = 0; n && i < 4; n = n.parentElement, i++) {
      const lab = n.querySelector && n.querySelector(':scope > label, :scope > legend');
      if (lab && clean(lab.innerText)) return clean(lab.innerText);
      const p = n.previousElementSibling;
      if (p && clean(p.innerText) && clean(p.innerText).length < 200) return clean(p.innerText);
    }
    return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };
  const out = [], seen = new Set();
  const walk = (root, inShadow) => {
    for (const el of root.querySelectorAll(SEL)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'input' && SKIP_TYPES.includes(type)) continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.getAttribute('aria-hidden') === 'true') continue;
      if (!visible(el) || tagged(el) || chrome(el)) continue;
      // Wrapper roles around an input we already list count once.
      if (el.parentElement && el.parentElement.closest(SEL) && out.some(o => o.el === el.parentElement.closest(SEL))) continue;
      const label = labelOf(el);
      out.push({el, tag, type: type || null, role: el.getAttribute('role'), label: label.slice(0, 200),
                required: el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label),
                in_shadow: inShadow});
    }
    for (const host of root.querySelectorAll('*')) if (host.shadowRoot) walk(host.shadowRoot, true);
  };
  walk(scope, false);
  const frames = [...scope.querySelectorAll('iframe')].filter(f => f.getClientRects().length)
    .map(f => f.getAttribute('src') || '').filter(s => !/recaptcha|hcaptcha|challenges\.cloudflare|google|doubleclick|facebook/i.test(s));
  return {missed: out.map(({el, ...rest}) => rest), iframes: frames, scope: form ? 'form' : 'body'};
}
"""

READBACK_JS = r"""
(id) => {
  // A field's value as the page now holds it. Custom dropdowns often clear their input after a
  // pick and show the choice in a sibling, so the surrounding text is returned too.
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const el = document.querySelector(`[data-jh-id="${id}"]`);
  if (!el) return null;
  let around = '', shown = null;
  for (let n = el.parentElement, i = 0; n && i < 4; n = n.parentElement, i++) {
    if (i < 3) around = clean(n.innerText);
    // The choice a custom dropdown displays after a pick (react-select's single-value element).
    const v = !shown && n.querySelector('[class*=singleValue], [class*=single-value], [class*=single_value], ' +
                                        '[class*=selected-value], [class*=select__value]');
    if (v && clean(v.innerText)) shown = clean(v.innerText);
  }
  return {value: el.value ?? null, files: el.files ? el.files.length : null, around: around.slice(0, 500), shown};
}
"""

INVALID_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const bad = [...document.querySelectorAll('input:invalid, select:invalid, textarea:invalid, [aria-invalid=true]')]
    .filter(el => el.getClientRects().length || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)));
  return bad.map(el => {
    const l = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return clean((l && l.innerText) || el.getAttribute('aria-label') || el.name || el.type).slice(0, 150);
  });
}
"""


# --- classification ---------------------------------------------------------

def ats_from_host(url):
    host = (urlsplit(url or "").hostname or "").lower()
    for name, pat in ATS_HOSTS:
        if re.search(rf"(^|\.){pat}$", host) or re.search(pat, host):
            return name
    return None


def ats_from_markup(html):
    for name, pat in ATS_MARKUP:
        if re.search(pat, html or "", re.I):
            return name
    return None


def classify(url, html=None):
    return ats_from_host(url) or (ats_from_markup(html) and f"{ats_from_markup(html)} (embedded)") or "generic"


# --- fake answers ------------------------------------------------------------

def persona_value(field):
    """A fake but plausible value for one field: the first option for choices."""
    t, label = field["type"], (field.get("label") or "").lower()
    opts = [o for o in (field.get("options") or []) if o]
    if t in ("radio", "select", "button_choice", "combo_select"):
        # The option a real answer would pick if there is one ("Canada", "No"), else the first.
        for pat, val in PERSONA:
            if re.search(pat, label):
                fit = forms.match_option(val, opts)
                if fit:
                    return fit
                break
        return opts[0] if opts else None
    if t == "checkbox_group":
        return [opts[0]] if opts else None
    if t == "checkbox":
        return "yes"
    if t == "email":
        return "bench.tester@example.com"
    if t == "tel":
        return "4165550123"
    if t == "number":
        return "5"
    if t == "url":
        return "https://example.com"
    if t == "date":
        return datetime.now().strftime("%Y-%m-%d")
    if t == "textarea" or (field.get("maxlength") or 0) >= 300:
        return LONG_TEXT
    for pat, val in PERSONA:
        if re.search(pat, label):
            return val
    return "Test"


def placeholder_pdf(path):
    """A one-page PDF saying it's a test file."""
    text = "Automated form check. Not a real resume."
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.encode("latin-1"))
    return path


# --- checking a fill ---------------------------------------------------------

def same(a, b):
    return forms.normalize(str(a)) == forms.normalize(str(b))


def verify(field, intended, now, raw):
    """(ok, why) for one field after filling, from a fresh read of the form."""
    t = field["type"]
    if raw is None and now is None:
        return False, "the field disappeared after filling"
    if t == "file":
        return (bool(raw and raw.get("files")), None if raw and raw.get("files") else "no file attached")
    if now is None:
        return False, "couldn't read the field back"
    v = now.get("value")
    if t == "checkbox":
        return (v is True, None if v is True else "checkbox not ticked")
    if t == "checkbox_group":
        want = intended if isinstance(intended, list) else [intended]
        got = v or []
        ok = all(any(same(w, g) for g in got) for w in want)
        return ok, None if ok else f"shows {got!r}"
    if t in ("radio", "select", "button_choice"):
        ok = v is not None and same(v, intended)
        return ok, None if ok else f"shows {v!r}"
    if v is not None and same(v, intended):
        return True, None
    if (t in ("tel", "number") or re.search(r"phone|mobile", field.get("label") or "", re.I)) and v:
        want, got = re.sub(r"\D", "", str(intended)), re.sub(r"\D", "", str(v))
        if len(want) >= 3 and got.endswith(want):
            return True, None      # the site reformatted it: "4165550123" -> "(416) 555-0123"
    shown = raw.get("shown") if raw else None
    if shown and field.get("combobox"):
        a, b = forms.normalize(shown), forms.normalize(str(intended))
        if a and (a in b or b in a):
            return True, None      # the dropdown displays the pick, maybe shortened ("+1" for "Canada +1")
    if field.get("combobox") and raw and forms.normalize(str(intended)) in forms.normalize(raw.get("around") or ""):
        return True, None       # a dropdown that shows the pick beside its (now empty) input
    if field.get("combobox") and v:
        return True, f"picked a suggestion: {v!r}"     # typed text replaced by a listed choice: fine
    return False, f"typed {intended!r}, field now holds {v!r}"


class Blocker:
    """While armed, blocks requests that could send form data anywhere."""

    def __init__(self):
        self.armed = False
        self.blocked = []

    def __call__(self, route):
        req = route.request
        if self.armed and req.method not in ("GET", "HEAD", "OPTIONS") and not NO_DATA_OUT.search(req.url):
            body = req.post_data or ""
            is_query = '"query":"query' in body.replace(" ", "") or '"query":"{' in body.replace(" ", "")
            if not is_query:
                self.blocked.append({"method": req.method, "url": req.url[:200]})
                return route.abort()
        return route.continue_()


# --- auditing one portal -------------------------------------------------------

def audit(page, target, out_dir, resume_pdf, blocker, pause=(0.1, 0.3), budget=120):
    """Measure one live (or saved) application page. Never submits."""
    res = {"id": target.get("id"), "company": target.get("company"), "title": target.get("title"),
           "apply_url": target["apply_url"], "ats": ats_from_host(target["apply_url"]) or "generic"}
    packet = {"job": {"url": target.get("url") or target["apply_url"], "apply_url": target["apply_url"]},
              "answers": {}, "resume_file": str(resume_pdf)}
    walk = portals.PortalWalk(page, packet, "fill", True, None, pause=None)
    page.on("dialog", lambda d: d.dismiss())      # alert()/confirm() boxes would freeze the page
    t0 = time.monotonic()
    try:
        early = walk.open()
    except Exception as e:
        early = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}
    res["final_url"] = page.url
    html = ""
    try:
        html = page.content()
        (out_dir / "page.html").write_text(html)
    except Exception:
        pass
    res["ats"] = classify(page.url, html) if res["ats"] == "generic" else res["ats"]
    if walk.ats and walk.ats != "generic":
        res["ats"] = walk.ats
    if early:
        res["opened"] = early["status"]
        res["open_error"] = early.get("error") or early.get("note")
        return finish(res, page, out_dir, t0)
    res["opened"] = "form"

    res["overlays_closed"] = portals.dismiss_overlays(page)
    fields = forms.extract_fields(page, probe=True)      # as the portal agent reads it
    res["dropdowns_read"] = sum(f["type"] == "combo_select" for f in fields)
    census = page.evaluate(CENSUS_JS)
    try:
        (out_dir / "aria.txt").write_text(page.locator("body").aria_snapshot())
    except Exception:
        pass

    # Fill everything with fake values, then keep filling fields that appear in response.
    blocker.armed = True
    seen, seen_keys, results, passes = set(), set(), [], 0
    batch = fields
    resume = next((f for f in fields if f["type"] == "file"
                   and portals.RESUME_FIELD.search(f"{f['label']} {f['key']}")), None)
    while batch and passes < 3 and time.monotonic() - t0 < budget:
        passes += 1
        for f in batch:
            if time.monotonic() - t0 > budget:
                res["timed_out"] = True
                break
            seen.add(f["id"])
            seen_keys.add((f["key"], f["type"]))
            value = str(resume_pdf) if f["type"] == "file" else persona_value(f)
            r = {"label": f["label"][:150], "type": f["type"], "required": bool(f.get("required")),
                 "pass": passes, "combobox": f.get("combobox", False)}
            if f["type"] == "file" and f is not resume and not f.get("required"):
                r.update(outcome="left_blank")
                results.append((f, None, r))
                continue
            if value is None:
                r.update(outcome="no_options", why="a choice field with no readable options")
                results.append((f, None, r))
                continue
            try:
                r["intended"] = forms.fill_field(page, f, value)
                r["outcome"] = "filled"
            except forms.FillError as e:
                r.update(outcome="fill_error", why=str(e)[:200])
            except Exception as e:
                r.update(outcome="crash", why=f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")
            results.append((f, r.get("intended"), r))
            if pause:
                time.sleep(random.uniform(*pause))
        # Button groups get fresh ids on every read, so a field counts as new only if its question is new.
        later = [f for f in forms.extract_fields(page, probe=True)
                 if f["id"] not in seen and (f["key"], f["type"]) not in seen_keys]
        for f in later:
            f["appeared_after_fill"] = True
        batch = later
    # As the portal agent does: retype anything the page cleared (a form still starting up, a re-render).
    typed = [(f, r["intended"]) for f, _, r in results if r.get("outcome") == "filled" and "intended" in r]
    page.locator("body").click(position={"x": 1, "y": 1}, force=True)    # leave the last field, as a person would
    cleared = forms.lost_values(page, typed)
    res["retyped"] = len(cleared) - len(forms.refill_lost(page, cleared)) if cleared else 0
    blocker.armed = False
    res["overlays_closed"] += portals.dismiss_overlays(page)

    # Read every field back.
    final = forms.extract_fields(page)
    now = {f["id"]: f for f in final}
    by_key = {(f["key"], f["type"]): f for f in final}
    rows = []
    for f, intended, r in results:
        if r["outcome"] == "filled":
            raw = page.evaluate(READBACK_JS, f["id"] if f["type"] not in ("radio", "checkbox_group", "button_choice")
                                else f"{f['id']}-0")
            ok, why = verify(f, intended, now.get(f["id"]) or by_key.get((f["key"], f["type"])), raw)
            r["outcome"] = "verified" if ok else "did_not_stick"
            if why:
                r["why"] = why
        r.pop("intended", None)
        r["appeared_after_fill"] = bool(f.get("appeared_after_fill"))
        rows.append(r)

    res.update(fields=rows, missed=census["missed"], uninspected_iframes=census["iframes"], census_scope=census["scope"],
               invalid_after_fill=page.evaluate(INVALID_JS), blocked_requests=blocker.blocked[:20])
    res["submit_button"] = any(c.count() for c in (page.get_by_role("button", name=portals.SUBMIT),
                                                   page.locator("[type=submit]")))
    res["next_button"] = page.get_by_role("button", name=NEXT).count() > 0
    # An invisible reCAPTCHA usually lets a real browser through; only a visible challenge blocks.
    res["captcha"] = any(c.is_visible() for c in page.locator(portals.CAPTCHA).all())
    res["fill_passes"] = passes
    return finish(res, page, out_dir, t0)


def finish(res, page, out_dir, t0):
    try:
        page.screenshot(path=str(out_dir / "filled.png"), full_page=True)
    except Exception:
        pass
    res["seconds"] = round(time.monotonic() - t0, 1)
    res.update(score(res))
    return res


def score(res):
    """verdict: pass | risky | fail | manual | unreachable, with the reasons."""
    if res.get("opened") == "needs_user":
        return {"verdict": "manual", "reasons": [res.get("open_error") or "portal needs an account"]}
    if res.get("opened") != "form":
        return {"verdict": "unreachable", "reasons": [res.get("open_error") or res.get("opened") or "no form"]}
    fields = res.get("fields", [])
    reasons = []
    missed_req = [m for m in res.get("missed", []) if m["required"]]
    if missed_req:
        reasons.append(f"{len(missed_req)} required control(s) not found by the engine")
    elif res.get("missed"):
        reasons.append(f"{len(res['missed'])} optional control(s) not found")
    bad = [f for f in fields if f["outcome"] in ("did_not_stick", "fill_error", "crash", "no_options")]
    bad_req = [f for f in bad if f["required"]]
    if bad_req:
        reasons.append(f"{len(bad_req)} required field(s) failed to fill")
    if any(f["appeared_after_fill"] for f in fields):
        reasons.append(f"{sum(f['appeared_after_fill'] for f in fields)} follow-up field(s) appeared after answers "
                       f"(the agent reads the form once)")
    if res.get("invalid_after_fill"):
        reasons.append(f"browser still flags {len(res['invalid_after_fill'])} field(s) invalid")
    if res.get("uninspected_iframes"):
        reasons.append(f"{len(res['uninspected_iframes'])} embedded frame(s) not inspected")
    if res.get("next_button") and not res.get("submit_button"):
        reasons.append("multi-page form (the portal agent handles one page)")
    elif not res.get("submit_button"):
        reasons.append("no Submit button found")
    if res.get("captcha"):
        reasons.append("CAPTCHA present")
    if res.get("timed_out"):
        reasons.append("ran out of time (over 2 minutes on one form)")
    hard = missed_req or bad_req or res.get("invalid_after_fill") or \
        (res.get("next_button") and not res.get("submit_button")) or not res.get("submit_button") or \
        any(f["appeared_after_fill"] and f["required"] for f in fields)
    soft = reasons and not hard
    return {"verdict": "fail" if hard else "risky" if soft else "pass", "reasons": reasons,
            "counts": {"fields": len(fields), "verified": sum(f["outcome"] == "verified" for f in fields),
                       "failed": len(bad), "missed": len(res.get("missed", [])), "missed_required": len(missed_req)}}


# --- resolving portal URLs through LinkedIn ------------------------------------------

def load_cache(path=RESOLVED):
    """Saved portal URLs. A LinkedIn URL saved by an older version is not a portal: dropped."""
    try:
        cache = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in cache.items() if v and not portals.LINKEDIN.search((urlsplit(v).hostname or "").lower())}


def resolve_all(targets, profile_dir, headless, run, cache_path=RESOLVED, pause=(6, 14)):
    """Fill in apply_url for targets that lack one, by clicking Apply on LinkedIn once per posting."""
    cache = load_cache(cache_path)
    todo = [t for t in targets if not t.get("apply_url") and t.get("id") not in cache]
    for t in targets:
        if not t.get("apply_url") and t.get("id") in cache:
            t["apply_url"] = cache[t["id"]]
    if not todo:
        return targets
    print(f"Finding company portals behind LinkedIn's Apply button: {len(todo)} posting(s), "
          f"{len(targets) - len(todo)} already known. One LinkedIn window, about 15 s each.", file=sys.stderr, flush=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless,
                                                   viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for i, t in enumerate(todo):
                try:
                    url = portals.resolve_apply_url(page, t["url"])
                except Exception as e:
                    url = None
                    run.warn("resolve_failed", target=t.get("id"), error=f"{type(e).__name__}: {e}"[:200])
                if url == "login_required":
                    run.warn("resolve_login_required")
                    print("LinkedIn wants a sign-in: run login.py, then rerun.", file=sys.stderr)
                    break
                if url:
                    t["apply_url"] = cache[t["id"]] = url
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(cache, indent=2))
                run.event("resolved", target=t.get("id"), apply_url=url)
                where = (urlsplit(url).hostname if url and url != "login_required" else None) or "no portal found"
                print(f"[find portal {i + 1}/{len(todo)}] {t.get('company')}: {where}", file=sys.stderr, flush=True)
                if i < len(todo) - 1:
                    time.sleep(random.uniform(*pause))
        finally:
            ctx.close()
    return targets


# --- running and reporting -------------------------------------------------------

def run_targets(targets, out, headless, run, browser=None):
    """Audit each target with an apply_url in its own clean context. Returns the results list."""
    from playwright.sync_api import sync_playwright
    resume_pdf = placeholder_pdf(out / "Test_Resume.pdf")
    results = []
    own = browser is None
    pw = sync_playwright().start() if own else None
    browser = browser or pw.chromium.launch(headless=headless)
    try:
        for i, t in enumerate(targets, 1):
            if not t.get("apply_url"):
                results.append({**{k: t.get(k) for k in ("id", "company", "title")}, "verdict": "unresolved",
                                "reasons": ["no portal URL (LinkedIn Apply not resolved)"], "ats": "unknown"})
                continue
            tdir = out / "targets" / f"{i:02d}-{re.sub(r'[^A-Za-z0-9]+', '-', t.get('company') or 'x')[:30]}"
            tdir.mkdir(parents=True, exist_ok=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            ctx.set_default_timeout(15000)
            blocker = Blocker()
            ctx.route("**/*", blocker)
            ctx.tracing.start(screenshots=True, snapshots=True)
            page = ctx.new_page()
            try:
                res = audit(page, t, tdir, resume_pdf, blocker)
            except Exception as e:
                res = {**{k: t.get(k) for k in ("id", "company", "title", "apply_url")}, "ats": ats_from_host(t["apply_url"]) or "generic",
                       "verdict": "unreachable", "reasons": [f"benchmark crashed: {type(e).__name__}: {e}"[:300]]}
            finally:
                try:
                    ctx.tracing.stop(path=str(tdir / "trace.zip"))
                except Exception:
                    pass
                ctx.close()
            res["dir"] = str(tdir)
            results.append(res)
            run.event("bench_target", target=t.get("id"), ats=res.get("ats"), verdict=res.get("verdict"),
                      reasons=res.get("reasons"), counts=res.get("counts"))
            print(f"[check form {i}/{len(targets)}] {res.get('verdict'):11} {res.get('ats'):24} {t.get('company')}",
                  file=sys.stderr, flush=True)
    finally:
        if own:
            browser.close()
            pw.stop()
    return results


def ats_family(ats):
    return (ats or "unknown").replace(" (embedded)", "")


def summarize(results):
    by = defaultdict(Counter)
    reasons = Counter()
    for r in results:
        by[ats_family(r.get("ats"))][r.get("verdict")] += 1
        for why in r.get("reasons", []):
            why = re.sub(r";? *(apply by hand at|finish it at)? *https?://\S+", "", why)
            reasons[re.sub(r"^\d+ ", "N ", why).strip()] += 1
    rows = []
    for ats, c in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(c.values())
        rows.append({"ats": ats, "postings": n, **{v: c.get(v, 0) for v in
                     ("pass", "risky", "fail", "manual", "unreachable", "unresolved")},
                     "pass_rate": round(c.get("pass", 0) / n, 2)})
    return {"postings": len(results), "by_ats": rows, "top_reasons": reasons.most_common(12),
            "verdicts": dict(Counter(r.get("verdict") for r in results))}


def report_md(results, summary):
    lines = ["# Portal benchmark", "", f"{summary['postings']} postings · " +
             " · ".join(f"{v}: {n}" for v, n in summary["verdicts"].items()), "",
             "## By portal", "", "| Portal | Postings | Pass | Risky | Fail | Manual | Unreachable | Unresolved |",
             "|---|---|---|---|---|---|---|---|"]
    for r in summary["by_ats"]:
        lines.append(f"| {r['ats']} | {r['postings']} | {r['pass']} | {r['risky']} | {r['fail']} | {r['manual']} | "
                     f"{r['unreachable']} | {r['unresolved']} |")
    lines += ["", "## Most common problems", ""] + [f"- {why} ({n})" for why, n in summary["top_reasons"]]
    lines += ["", "## Each posting", ""]
    for r in results:
        lines.append(f"### {r.get('company')}: {r.get('title')}")
        lines.append(f"- **{r.get('verdict')}** on {r.get('ats')} · {r.get('apply_url') or 'no URL'}")
        for why in r.get("reasons", []):
            lines.append(f"  - {why}")
        for m in r.get("missed", [])[:8]:
            lines.append(f"  - not found: {m.get('label') or '(no label)'} [{m.get('role') or m.get('tag')}"
                         f"{', required' if m.get('required') else ''}{', shadow DOM' if m.get('in_shadow') else ''}]")
        for f in r.get("fields", []):
            if f["outcome"] not in ("verified", "left_blank"):
                lines.append(f"  - {f['outcome']}: {f['label'] or '(no label)'} [{f['type']}] {f.get('why') or ''}")
        if r.get("dir"):
            lines.append(f"  - files: {r['dir']}")
        lines.append("")
    return "\n".join(lines)


def load_targets(path):
    data = json.loads(Path(path).read_text())
    return data["targets"] if isinstance(data, dict) else data


def cmd_run(args, run):
    targets = load_targets(args.targets)[: args.limit or None]
    out = Path(args.out) if args.out else BENCH / datetime.now().strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    if not args.no_resolve:
        targets = resolve_all(targets, Path(args.profile_dir), args.headless, run)
    print(f"Checking {sum(bool(t.get('apply_url')) for t in targets)} form(s)"
          f"{' in visible windows' if args.watch else ' in the background (no windows)'}.", file=sys.stderr, flush=True)
    results = run_targets(targets, out, not args.watch, run)
    summary = summarize(results)
    (out / "results.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    (out / "report.md").write_text(report_md(results, summary))
    return {"out": str(out), "report": str(out / "report.md"), **summary}


def cmd_replay(args, run):
    """Re-read saved pages without their scripts: what extraction finds now vs. when captured."""
    from playwright.sync_api import sync_playwright
    base = Path(args.dir)
    old = {r.get("dir"): r for r in json.loads((base / "results.json").read_text())["results"]}
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for d in sorted((base / "targets").glob("*")):
            html = d / "page.html"
            if not html.exists():
                continue
            ctx = browser.new_context(java_script_enabled=False)
            ctx.route(re.compile(r"^https?://"), lambda route: route.abort())     # offline
            page = ctx.new_page()
            page.goto(html.resolve().as_uri(), wait_until="domcontentloaded")
            fields = forms.extract_fields(page)
            census = page.evaluate(CENSUS_JS)
            ctx.close()
            before = old.get(str(d), {}).get("counts", {})
            rows.append({"target": d.name, "fields_now": len(fields), "missed_now": len(census["missed"]),
                         "fields_before": before.get("fields"), "missed_before": before.get("missed")})
        browser.close()
    return {"targets": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("targets")
    r.add_argument("--out")
    r.add_argument("--headless", action="store_true", help="hide the LinkedIn window too")
    r.add_argument("--watch", action="store_true", help="show a window for each form check")
    r.add_argument("--limit", type=int)
    r.add_argument("--no-resolve", action="store_true", help="skip LinkedIn; audit only targets with apply_url")
    r.add_argument("--profile-dir", default=str(PROFILE))
    r = sub.add_parser("replay")
    r.add_argument("dir")
    args = ap.parse_args(argv)
    with runlog.start("apply-agent", HERE, argv) as run:
        out = cmd_run(args, run) if args.cmd == "run" else cmd_replay(args, run)
        print(json.dumps(out, indent=2))
        run.finish(0, cmd=f"bench {args.cmd}", verdicts=out.get("verdicts"))
        return 0


if __name__ == "__main__":
    sys.exit(main())
