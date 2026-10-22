"""Generic form reading and filling, shared by Easy Apply and external portals.

extract_fields() runs one JavaScript pass over a container: every input,
select and textarea is tagged with data-jh-id and described (label, type,
options, required, current value). Radio buttons and multi-checkboxes that
share a name become one field whose options are their labels.

fill_field() puts a value into a described field. For choice fields the
value must match one of the options (match_option); otherwise filling fails
and the caller treats the field as unanswered rather than guessing.
"""

import re
import time

EXTRACT_JS = r"""
(rootSelector) => {
  const scope = rootSelector ? document.querySelector(rootSelector) : document;
  if (!scope) return [];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const text = el => el ? clean(el.innerText || el.textContent) : '';
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none';
  };
  // Rendered at all (not inside a display:none step), even if styled invisible:
  // custom radios and upload buttons hide the input but render its label.
  const labelEl = el => (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
  const displayed = el => el.getClientRects().length > 0 || !!(labelEl(el) && labelEl(el).getClientRects().length > 0);
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && text(l)) return text(l);
    }
    const wrap = el.closest('label');
    if (wrap && text(wrap)) return text(wrap);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = clean(by.split(/\s+/).map(i => text(document.getElementById(i))).join(' '));
      if (t) return t;
    }
    const own = clean(el.getAttribute('aria-label') || el.placeholder || '');
    if (own) return own;
    // Named by a surrounding group instead (Greenhouse: role=group "Resume/CV*" around a hidden file input).
    const grp = el.closest('[role=group], fieldset');
    if (grp) {
      const by = grp.getAttribute('aria-labelledby');
      const t = clean((by && document.getElementById(by) && text(document.getElementById(by))) ||
                      grp.getAttribute('aria-label') || '');
      if (t) return t;
    }
    return clean(el.name || el.id || '');
  };
  // Text shown just before an element: LinkedIn puts a radio question's text in a sibling
  // above the radiogroup rather than in a <legend>.
  const textBefore = el => {
    for (let n = el, i = 0; n && n !== scope && i < 3; n = n.parentElement, i++) {
      const prev = n.previousElementSibling;
      if (prev && prev.getClientRects().length && !prev.querySelector('input, select, textarea')) {
        const t = text(prev);
        if (t && t.length < 400) return t;
      }
    }
    return '';
  };
  // An option's own text. LinkedIn renders it in a sibling of the input's wrapper, with no <label>,
  // and the option wrapper's aria-label is the question, not the option.
  const optionLabel = el => {
    const l = labelEl(el);
    if (l && text(l)) return text(l);
    for (let n = el.parentElement, i = 0; n && n !== scope && i < 3; n = n.parentElement, i++) {
      if (n.querySelectorAll('input').length > 1) break;      // reached the group: stop
      const t = text(n);
      if (t && t.length < 120) return t;
    }
    return clean(el.getAttribute('aria-label') || el.value || el.name || '');
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg && text(lg)) return text(lg);
      if (fs.getAttribute('aria-label')) return clean(fs.getAttribute('aria-label'));
    }
    const grp = el.closest('[role=radiogroup], [role=group]');
    if (grp) {
      const by = grp.getAttribute('aria-labelledby');
      if (by && document.getElementById(by)) return text(document.getElementById(by));
      if (grp.getAttribute('aria-label')) return clean(grp.getAttribute('aria-label'));
    }
    const container = fs || grp;
    const before = container ? textBefore(container) : '';
    if (before) return before;
    const opt = el.closest('[role=radio], [role=checkbox]');     // LinkedIn: the question as each option's aria-label
    if (opt && opt.getAttribute('aria-label')) return clean(opt.getAttribute('aria-label'));
    return el.name || '';
  };
  const out = [], groups = new Set();
  // One counter per page, so ids stay unique across steps of a multi-page form.
  const next = () => 'jh-' + (window.__jhSeq = (window.__jhSeq || 0) + 1);
  for (const el of scope.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.disabled) continue;
    const isChoice = type === 'radio' || type === 'checkbox';
    if (!displayed(el)) continue;
    if (!isChoice && type !== 'file' && !shown(el)) continue;
    const named = el.name ? scope.querySelectorAll(`input[type=${type}][name="${CSS.escape(el.name)}"]`) : [];
    if (type === 'radio' || (type === 'checkbox' && named.length > 1)) {
      const key = type + ':' + el.name;
      if (groups.has(key)) continue;
      groups.add(key);
      const members = [...named];
      // Keep an id already given on an earlier read, so fields read before stay addressable.
      const prior = members[0].getAttribute('data-jh-id');
      const id = prior && /-\d+$/.test(prior) ? prior.replace(/-\d+$/, '') : next();
      members.forEach((m, i) => m.setAttribute('data-jh-id', `${id}-${i}`));
      const legend = groupLabel(el);
      const checked = members.filter(m => m.checked).map(optionLabel);
      const fs = el.closest('fieldset');
      out.push({id, type: type === 'radio' ? 'radio' : 'checkbox_group', label: legend,
                options: members.map(optionLabel),
                required: members.some(m => m.required) || (fs && fs.getAttribute('aria-required') === 'true')
                          || /\*\s*$|\(required\)/i.test(legend),
                value: type === 'radio' ? (checked[0] || null) : (checked.length ? checked : null)});
      continue;
    }
    const id = el.getAttribute('data-jh-id') || next();
    el.setAttribute('data-jh-id', id);
    const label = labelFor(el);
    const d = {id, type, label, required: el.required || el.getAttribute('aria-required') === 'true'
                                          || /\*\s*$|\(required\)/i.test(label),
               combobox: el.getAttribute('role') === 'combobox' || el.hasAttribute('aria-autocomplete'),
               value: type === 'checkbox' ? el.checked : (el.value || null),
               maxlength: el.maxLength > 0 ? el.maxLength : null};
    if (type === 'file') { d.value = null; d.accept = el.accept || null; }
    if (type === 'select') {
      const opts = [...el.options];
      const isPlaceholder = o => !o || !clean(o.text) || /^(select|choose)\b|^--/i.test(clean(o.text)) || o.value === '';
      d.options = opts.filter(o => !isPlaceholder(o)).map(o => clean(o.text));
      const cur = opts[el.selectedIndex];
      d.value = isPlaceholder(cur) ? null : clean(cur.text);
    }
    out.push(d);
  }
  return out;
}
"""

BUTTON_CHOICES_JS = r"""
(rootSelector) => {
  // Yes/No style questions rendered as a small group of buttons under a label (Ashby and others).
  const scope = rootSelector ? document.querySelector(rootSelector) : document;
  if (!scope) return [];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const next = () => 'jh-' + (window.__jhSeq = (window.__jhSeq || 0) + 1);
  const out = [], seen = new Set();
  for (const b of scope.querySelectorAll('button[type=button]')) {
    const box = b.parentElement;
    if (!box || seen.has(box)) continue;
    const buttons = [...box.children].filter(c => c.tagName === 'BUTTON');
    if (buttons.length < 2 || buttons.length > 4 || buttons.some(x => clean(x.innerText).length > 30)) continue;
    if (box.getClientRects().length === 0) continue;
    seen.add(box);
    let lab = null;
    for (let n = box; n && n !== scope && !lab; n = n.parentElement) {
      lab = n.querySelector(':scope > label') || (n.previousElementSibling && n.previousElementSibling.tagName === 'LABEL'
            ? n.previousElementSibling : null);
    }
    if (!lab) continue;
    const id = next();
    buttons.forEach((x, i) => x.setAttribute('data-jh-id', `${id}-${i}`));
    const label = clean(lab.innerText);
    const pressed = buttons.find(x => x.getAttribute('aria-pressed') === 'true' || /selected|active/i.test(x.className));
    out.push({id, type: 'button_choice', label, options: buttons.map(x => clean(x.innerText)),
              required: /\*\s*$/.test(label), value: pressed ? clean(pressed.innerText) : null});
  }
  return out;
}
"""

REQUIRED_MARK = re.compile(r"\s*(\*|\(required\)|required)\s*$", re.I)


def normalize(text):
    """Same rules as the tracker's bank.normalize: case, punctuation, required-markers, spaces."""
    q = (text or "").lower()
    q = re.sub(r"\(required\)|\(optional\)|\*", " ", q)
    q = re.sub(r"[^\w\s]", " ", q)
    return " ".join(q.split())


def clean_label(label):
    """Drop a duplicated label ("Email Email") and trailing required markers."""
    label = REQUIRED_MARK.sub("", (label or "").strip())
    words = label.split()
    half = len(words) // 2
    if words and len(words) % 2 == 0 and words[:half] == words[half:]:
        label = " ".join(words[:half])
    return label


# Visible options only: a dropdown read earlier can leave its (hidden) options in the page.
OPTIONS = "[role=listbox] [role=option]:visible, [class*=menu] [class*=option]:visible"
MAX_PROBED_OPTIONS = 300


def extract_fields(page, root=None, probe=False):
    """Every fillable field in `root`. probe=True opens each searchable dropdown to read its
    options, so it can be answered like a normal dropdown (company portals; slower)."""
    fields = page.evaluate(EXTRACT_JS, root) + page.evaluate(BUTTON_CHOICES_JS, root)
    for f in fields:
        f["label"] = clean_label(f["label"])
        f["key"] = normalize(f["label"])
        f["options"] = f.get("options")
        f.setdefault("combobox", False)
    if probe:
        for f in fields:
            if f["combobox"] and f["type"] == "text":
                opts = probe_options(page, f)
                if opts:
                    f["type"], f["options"] = "combo_select", opts
    return fields


def _visible_options(page):
    out = []
    for o in page.locator(OPTIONS).all()[:MAX_PROBED_OPTIONS]:
        try:
            if o.is_visible():
                t = " ".join((o.inner_text() or "").split())
                if t:
                    out.append(t)
        except Exception:
            continue
    return out


def probe_options(page, field):
    """Open a searchable dropdown without typing and read its fixed options. [] for a typeahead
    that only suggests after you type (a city, a school), which stays a text field."""
    loc = page.locator(f'[data-jh-id="{field["id"]}"]')
    try:
        loc.click(timeout=2000)
        page.locator(OPTIONS).first.wait_for(state="visible", timeout=1500)
        opts = _visible_options(page)
    except Exception:
        opts = []
    try:
        loc.press("Escape", timeout=1000)
    except Exception:
        pass
    if opts and len(set(opts)) == 1 and re.search(r"no options|type to search|searching", opts[0], re.I):
        return []
    return list(dict.fromkeys(opts))


# Self-identification forms word "decline" differently; any of these means the same answer.
DECLINE = re.compile(r"\b(decline|prefer not|don.?t wish|do not wish|not wish to|rather not|choose not|"
                     r"do not want|don.?t want|not to (say|answer|disclose|identify))", re.I)


def match_option(value, options):
    """The single option meaning `value`, or None. Exact, then prefix, then whole-word
    containment; a decline-to-answer value matches the form's own decline option."""
    if value is None or not options:
        return None
    if DECLINE.search(str(value)):
        declines = [o for o in options if DECLINE.search(o or "")]
        if len(declines) == 1:
            return declines[0]
    v = normalize(str(value))
    norm = [normalize(o) for o in options]
    exact = [o for o, n in zip(options, norm) if n == v]
    if exact:
        return exact[0]
    prefix = [o for o, n in zip(options, norm) if n.startswith(v + " ") or v.startswith(n + " ")]
    if len(prefix) == 1:
        return prefix[0]
    contains = [o for o, n in zip(options, norm) if re.search(rf"\b{re.escape(v)}\b", n)]
    if len(contains) == 1:
        return contains[0]
    return None


class FillError(Exception):
    pass


def _is_checked(loc):
    try:
        return loc.is_checked()
    except Exception:
        return False


CLICK_VISIBLE_ANCESTOR_JS = """el => {
  for (let n = el.parentElement, i = 0; n && i < 4; n = n.parentElement, i++) {
    const r = n.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) { n.click(); return true; }
  }
  return false;
}"""


def set_choice(page, sel, want=True):
    """Check (or uncheck) a radio or checkbox that may be styled out of reach.

    Sites hide the real input off-screen behind a styled card, so a normal
    click can fail ("outside of the viewport"). In order: Playwright's check,
    a click dispatched on the input itself, then a click on the visible card
    around it. Each attempt is verified. Returns True when verified, None when
    the page redrew the control (the caller re-reads the form to verify);
    FillError if no attempt took.
    """
    loc = page.locator(sel)

    def state():
        return "gone" if loc.count() == 0 else _is_checked(loc)

    if state() == want:
        return True
    attempts = (lambda: loc.set_checked(want, force=True, timeout=3000),
                lambda: loc.evaluate("el => el.click()", timeout=2000),
                lambda: loc.evaluate(CLICK_VISIBLE_ANCESTOR_JS, timeout=2000))
    for attempt in attempts:
        try:
            attempt()
        except Exception:
            pass
        now = state()
        if now == "gone":
            return None
        if now == want:
            return True
    raise FillError("couldn't select this option: the page wouldn't accept a click on it")


def read_value(page, key, root=None, wait=0.0):
    """A field's current value after the page may have redrawn it (None if it's gone).
    Waits up to `wait` seconds for the field to reappear."""
    deadline = time.monotonic() + wait
    while True:
        for f in extract_fields(page, root):
            if f["key"] == key:
                return f["value"]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


TRUTHY = {"yes", "true", "1", "checked", "i agree", "agree", "y"}


def fill_field(page, field, value, pause=0.0, root=None):
    """Put `value` into `field`. Raises FillError when a choice can't be matched or won't stick.
    `root` is the container the field was read from, for re-reading after a redraw."""
    t = field["type"]
    sel = f'[data-jh-id="{field["id"]}"]'
    if isinstance(value, dict) and "any_of" in value:
        # Several acceptable wordings: use the first the form offers (text fields take the first).
        choices = value["any_of"]
        if t in ("radio", "select", "button_choice", "checkbox_group") and field.get("options"):
            fit = next((v for v in choices if match_option(v, field["options"])), None)
            if fit is None:
                raise FillError(f"none of {choices} matches {field['options']}")
            value = fit
        else:
            value = choices[0]
    if t in ("radio", "checkbox_group"):
        wanted = value if isinstance(value, list) else [value] if t == "radio" else \
            [v.strip() for v in str(value).split(",") if v.strip()]
        for w in wanted:
            opt = match_option(w, field["options"])
            if opt is None:
                raise FillError(f"{w!r} matches none of {field['options']}")
            i = field["options"].index(opt)
            if set_choice(page, f'[data-jh-id="{field["id"]}-{i}"]') is None and t == "radio":
                # The site redrew the options on click: confirm from a fresh read.
                now = read_value(page, field["key"], root, wait=5)
                if normalize(now) != normalize(opt):
                    raise FillError(f"selected {opt!r} but the form now shows {now!r}")
        chosen = [match_option(w, field["options"]) for w in wanted]
        return chosen[0] if t == "radio" else chosen
    if t == "combo_select":
        opt = match_option(value, field["options"])
        if opt is None:
            raise FillError(f"{value!r} matches none of {field['options'][:20]}")
        loc = page.locator(sel)
        loc.click()
        loc.fill(opt)              # narrows the list; the choice is the click on the option below
        choice = page.locator(OPTIONS).filter(has_text=opt)
        try:
            choice.first.wait_for(state="visible", timeout=3000)
        except Exception as e:
            raise FillError(f"{opt!r} didn't appear in the dropdown") from e
        exact = [o for o in choice.all() if normalize(o.inner_text()) == normalize(opt)]
        (exact[0] if exact else choice.first).click()
        if pause:
            time.sleep(pause)
        return opt
    if t == "button_choice":
        opt = match_option(value, field["options"])
        if opt is None:
            raise FillError(f"{value!r} matches none of {field['options']}")
        page.locator(f'[data-jh-id="{field["id"]}-{field["options"].index(opt)}"]').click()
        return opt
    loc = page.locator(sel)
    if t == "select":
        opt = match_option(value, field["options"])
        if opt is None:
            raise FillError(f"{value!r} matches none of {field['options']}")
        loc.select_option(label=opt)
        result = opt
    elif t == "checkbox":
        want = str(value).strip().lower() in TRUTHY
        set_choice(page, sel, want)
        result = want
    elif t == "file":
        loc.set_input_files(value)
        result = value
    else:
        loc.fill(str(value))
        result = _pick_suggestion(page, loc, value) or str(value)     # a typeahead holds the picked suggestion
    if pause:
        time.sleep(pause)
    return result


TEXTLIKE = {"text", "email", "tel", "number", "url", "textarea", "date", "password"}


def settle(page, timeout=8000):
    """Wait for a page to finish loading its scripts before typing: a form that's still starting
    up resets anything typed into it."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(500)


def lost_values(page, filled, root=None):
    """The (field, value) pairs of typed fields that no longer hold what was typed."""
    now = {f["id"]: f.get("value") for f in extract_fields(page, root)}
    return [(f, v) for f, v in filled if f["type"] in TEXTLIKE
            and normalize(str(now.get(f["id"]) or "")) != normalize(str(v))]


def refill_lost(page, filled, root=None, passes=2):
    """Retype fields the page cleared, as real keystrokes, then leave the field and check again.
    Covers a form still starting up (it resets early typing) and inputs that only accept
    keystrokes (they drop a pasted value on blur). `filled` is [(field, value)].
    Returns the pairs still not holding their value."""
    page.wait_for_timeout(700)
    pending = lost_values(page, filled, root)
    for _ in range(passes):
        if not pending:
            break
        for f, v in pending:
            loc = page.locator(f'[data-jh-id="{f["id"]}"]')
            try:
                if f.get("combobox"):            # a typeahead: type and pick the suggestion again
                    fill_field(page, f, v)
                else:
                    loc.fill("")
                    loc.press_sequentially(str(v), delay=15)
                loc.evaluate("el => el.blur()")
            except Exception:
                pass
        page.wait_for_timeout(700)
        pending = lost_values(page, pending, root)
    return pending


def _pick_suggestion(page, loc, value):
    """Typeahead inputs (a city, a school) only accept a value picked from their list: choose
    the suggestion matching what was typed; failing that, the first one only if it starts with
    it ("Toronto" -> "Toronto, ON, Canada"). Anything else is a FillError, never a guess."""
    if loc.get_attribute("role") != "combobox" and loc.get_attribute("aria-autocomplete") is None:
        return None
    options = page.locator(OPTIONS)
    try:
        options.first.wait_for(state="visible", timeout=2500)
    except Exception:
        return None   # no suggestions: the typed value stands
    visible = [o for o in options.all()[:MAX_PROBED_OPTIONS] if o.is_visible()]
    texts = [" ".join((o.inner_text() or "").split()) for o in visible]
    choice = match_option(value, texts)
    if choice is None and texts and normalize(texts[0]).startswith(normalize(str(value))):
        choice = texts[0]
    if choice is None:
        loc.press("Escape")
        raise FillError(f"{value!r} matches none of the suggestions {texts[:10]}")
    visible[texts.index(choice)].click()
    return choice
