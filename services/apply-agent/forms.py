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
      const checked = members.filter(m => m.checked).map(labelFor);
      const fs = el.closest('fieldset');
      out.push({id, type: type === 'radio' ? 'radio' : 'checkbox_group', label: legend,
                options: members.map(labelFor),
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
    fields = page.evaluate(EXTRACT_JS, root)
    for f in fields:
        f["label"] = clean_label(f["label"])
        f["key"] = normalize(f["label"])
        f["options"] = f.get("options")
    return fields


def match_option(value, options):
    """The single option meaning `value`, or None. Exact, then prefix, then whole-word containment."""
    if value is None or not options:
        return None
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
            page.locator(f'[data-jh-id="{field["id"]}-{i}"]').check(force=True)
        chosen = [match_option(w, field["options"]) for w in wanted]
        return chosen[0] if t == "radio" else chosen
    loc = page.locator(sel)
    if t == "select":
        opt = match_option(value, field["options"])
        if opt is None:
            raise FillError(f"{value!r} matches none of {field['options']}")
        loc.select_option(label=opt)
        result = opt
    elif t == "checkbox":
        want = str(value).strip().lower() in TRUTHY
        loc.set_checked(want, force=True)
        result = want
    elif t == "file":
        loc.set_input_files(value)
        result = value
    else:
        loc.fill(str(value))
        result = str(value)
        _pick_suggestion(page, loc)
    if pause:
        time.sleep(pause)
    return result


def _pick_suggestion(page, loc):
    """Typeahead inputs (city, school) only accept a value picked from their list."""
    if loc.get_attribute("role") != "combobox" and loc.get_attribute("aria-autocomplete") is None:
        return
    option = page.locator("[role=listbox] [role=option]").first
    try:
        option.wait_for(state="visible", timeout=2500)
        option.click()
    except Exception:
        pass   # no suggestions: the typed value stands
