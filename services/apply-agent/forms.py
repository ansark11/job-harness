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
    return clean(el.getAttribute('aria-label') || el.placeholder || el.name || '');
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
      const id = next();
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
    const id = next();
    el.setAttribute('data-jh-id', id);
    const label = labelFor(el);
    const d = {id, type, label, required: el.required || el.getAttribute('aria-required') === 'true'
                                          || /\*\s*$|\(required\)/i.test(label),
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


def extract_fields(page, root=None):
    fields = page.evaluate(EXTRACT_JS, root) + page.evaluate(BUTTON_CHOICES_JS, root)
    for f in fields:
        f["label"] = clean_label(f["label"])
        f["key"] = normalize(f["label"])
        f["options"] = f.get("options")
    return fields


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
    around it. Each attempt is verified; FillError if none took.
    """
    loc = page.locator(sel)
    if _is_checked(loc) == want:
        return
    try:
        loc.set_checked(want, force=True, timeout=3000)
    except Exception:
        pass
    if _is_checked(loc) == want:
        return
    loc.evaluate("el => el.click()")
    if _is_checked(loc) == want:
        return
    loc.evaluate(CLICK_VISIBLE_ANCESTOR_JS)
    if _is_checked(loc) != want:
        raise FillError("couldn't select this option: the page wouldn't accept a click on it")


TRUTHY = {"yes", "true", "1", "checked", "i agree", "agree", "y"}


def fill_field(page, field, value, pause=0.0):
    """Put `value` into `field`. Raises FillError when a choice can't be matched."""
    t = field["type"]
    sel = f'[data-jh-id="{field["id"]}"]'
    if t in ("radio", "checkbox_group"):
        wanted = value if isinstance(value, list) else [value] if t == "radio" else \
            [v.strip() for v in str(value).split(",") if v.strip()]
        for w in wanted:
            opt = match_option(w, field["options"])
            if opt is None:
                raise FillError(f"{w!r} matches none of {field['options']}")
            i = field["options"].index(opt)
            set_choice(page, f'[data-jh-id="{field["id"]}-{i}"]')
        chosen = [match_option(w, field["options"]) for w in wanted]
        return chosen[0] if t == "radio" else chosen
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
        result = str(value)
        _pick_suggestion(page, loc, value)
    if pause:
        time.sleep(pause)
    return result


def _pick_suggestion(page, loc, value):
    """Typeahead and searchable-select inputs only accept a value picked from their list:
    choose the listed option that matches the value, else the first suggestion."""
    if loc.get_attribute("role") != "combobox" and loc.get_attribute("aria-autocomplete") is None:
        return
    options = page.locator("[role=listbox] [role=option]")
    try:
        options.first.wait_for(state="visible", timeout=2500)
    except Exception:
        return   # no suggestions: the typed value stands
    texts = [t.strip() for t in options.all_inner_texts()]
    choice = match_option(value, texts)
    options.nth(texts.index(choice) if choice else 0).click()
