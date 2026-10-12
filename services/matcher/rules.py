"""Deterministic match decisions from criteria and extracted facts.

prefilter() runs before any model call, on what LinkedIn states outright.
decide() runs after extraction. Both return reasons for every rejection.
Facts the model couldn't determine ("unknown", no pay listed) never reject
on their own: they pass with a flag so the user sees the posting.
"""

import re
from datetime import datetime

US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
                "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
CA_PROVINCES = {"Ontario", "British Columbia", "Alberta", "Quebec", "Québec", "Manitoba", "Saskatchewan",
                "Nova Scotia", "New Brunswick", "Newfoundland and Labrador", "Prince Edward Island"}


def country(location):
    """'CA', 'US' or None from a LinkedIn location string."""
    loc = (location or "").strip()
    if not loc:
        return None
    if "Canada" in loc or any(p in loc for p in CA_PROVINCES):
        return "CA"
    if "United States" in loc:
        return "US"
    m = re.search(r",\s*([A-Z]{2})\b", loc)
    if m and m.group(1) in US_STATES:
        return "US"
    if re.search(r"\b(New York City|San Francisco Bay Area|Greater Seattle|Los Angeles Metropolitan)\b", loc):
        return "US"
    return None


def prefilter(posting, cfg, now):
    """Reasons to reject without a model call ([] = continue)."""
    reasons = []
    crit = posting.get("criteria") or {}

    industries = [i.strip() for i in (crit.get("industries") or "").split(",") if i.strip()]
    excluded = set(cfg.get("excluded_linkedin_industries", []))
    if industries and all(i in excluded for i in industries):
        reasons.append(f"LinkedIn industry is excluded: {', '.join(industries)}")

    etype = (crit.get("employment_type") or "").lower()
    if etype in ("part-time", "internship", "volunteer", "temporary"):
        reasons.append(f"employment type is {crit['employment_type']}")

    if posting.get("posted_at"):
        age = (now - datetime.fromisoformat(posting["posted_at"])).total_seconds() / 86400
        if age >= cfg.get("max_age_days", 30):
            reasons.append(f"posted {age:.0f} days ago")
    return reasons


def annual(amount, period, hours_per_year):
    if amount is None:
        return None
    return amount * hours_per_year if period == "hour" else amount


def choose_pay(pay_ranges, location, comp_cfg):
    """The range that applies to this posting, converted to annual CAD.

    Prefers a range whose location names the posting's city; otherwise the
    range with the highest top end (if any band clears the bar, the role can).
    Returns None when nothing usable is listed.
    """
    fx = comp_cfg.get("fx_to_cad", {"CAD": 1.0})
    hours = comp_cfg.get("hours_per_year", 2080)
    usable = []
    for r in pay_ranges:
        rate = fx.get((r.get("currency") or "").upper())
        if rate is None:
            continue
        lo, hi = annual(r.get("min"), r.get("period"), hours), annual(r.get("max"), r.get("period"), hours)
        top = hi if hi is not None else lo
        usable.append({**r, "min_cad": round(lo * rate) if lo else None, "max_cad": round(top * rate)})
    if not usable:
        return None
    city = (location or "").split(",")[0].strip().lower()
    local = [r for r in usable if city and r.get("location") and city in r["location"].lower()]
    pool = local or usable
    chosen = max(pool, key=lambda r: r["max_cad"])
    chosen["chosen_because"] = "matches posting location" if local else "highest band listed"
    return chosen


def _metro(location, metros):
    return any(m.lower() in (location or "").lower() for m in metros)


def decide(posting, facts, cfg):
    """(reasons to reject, flags). Empty reasons = match."""
    reasons, flags = [], []
    title = posting.get("title") or ""
    loc = posting.get("location") or ""

    # Seniority: a senior word in the title is enough; otherwise trust the posting's scope.
    if not re.search(cfg["title_seniority"], title, re.I):
        if facts["seniority"] in ("junior", "mid"):
            reasons.append(f"role reads as {facts['seniority']}-level and the title has no seniority marker")
        elif facts["seniority"] == "unknown":
            flags.append("seniority unclear")

    # Work mode and location.
    mode = facts["work_mode"]
    if mode == "onsite":
        reasons.append("on-site role")
    elif mode == "unknown":
        flags.append("work mode not stated")
    where = country(loc)
    if where == "CA":
        if mode != "remote" and not _metro(loc, cfg["canada_metros"]):
            reasons.append(f"{loc} is outside the Toronto/Vancouver areas and the role isn't remote")
    elif where is None:
        flags.append(f"couldn't tell the country of {loc!r}")
    elif where not in cfg.get("countries", ["CA", "US"]):
        reasons.append(f"located in {where}")

    # Employment type.
    etype = facts["employment_type"]
    if etype == "part_time":
        reasons.append("part-time")
    elif etype == "contract":
        months = facts.get("contract_months")
        if months is None:
            flags.append("contract length not stated")
        elif months < cfg.get("contract_min_months", 12):
            reasons.append(f"contract is {months} months (minimum {cfg.get('contract_min_months', 12)})")

    # Pay, only when listed.
    comp_cfg = cfg["comp"]
    pay = choose_pay(facts["pay_ranges"], loc, comp_cfg)
    floor = comp_cfg["min_total_cad"]
    if pay is None:
        flags.append("pay not listed")
    else:
        bar = floor * (1 - comp_cfg.get("tolerance", 0))
        if pay["max_cad"] < bar:
            reasons.append(f"pay tops out at ~{pay['max_cad']:,} CAD (target {floor:,})")
        elif pay["max_cad"] < floor:
            flags.append(f"pay tops out just under target (~{pay['max_cad']:,} CAD)")

    # Industry: excluded company types (CPG, retailers) always lose; otherwise the company's
    # industry or the domain of the product this role owns must be on the list. The Big Tech
    # company allowlist overrides the model's category.
    industry = facts["industry"]
    domain = facts.get("role_domain", "other")
    company = posting.get("company") or ""
    if any(re.fullmatch(rf"{re.escape(c)}(\b.*)?", company, re.I) for c in cfg.get("big_tech_companies", [])):
        industry = "big_tech"
    if industry in cfg.get("excluded_industries", ["cpg", "retailer"]):
        reasons.append(f"company is a {industry.replace('_', ' ')} ({facts['industry_reason']})")
    elif industry not in cfg["industries"] and domain not in cfg["industries"]:
        reasons.append(f"industry is {industry.replace('_', ' ')} and the role isn't in a target domain "
                       f"({facts['industry_reason']})")

    return reasons, flags, {"pay": pay, "industry": industry, "role_domain": domain}
