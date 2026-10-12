"""Ask the model for a posting's facts and its fit against the resume.

The model only reports what the posting says (work mode, pay ranges, contract
length, seniority, industry) and scores fit. It makes no keep/reject
decision: rules.py does that from these facts.
"""

import llm

DESCRIPTION_LIMIT = 14000
RESUME_LIMIT = 12000

INDUSTRIES = ["fintech_payments", "big_tech", "health_tech", "ecommerce_platform", "cpg", "retailer", "other"]
DOMAINS = ["fintech_payments", "health_tech", "ecommerce_platform", "other"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["work_mode", "employment_type", "contract_months", "seniority", "pay_ranges",
                 "industry", "industry_reason", "role_domain", "fit_score", "fit_reasons", "concerns"],
    "properties": {
        "work_mode": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
        "employment_type": {"type": "string", "enum": ["full_time", "contract", "part_time", "unknown"]},
        "contract_months": {"type": ["integer", "null"]},
        "seniority": {"type": "string", "enum": ["junior", "mid", "senior", "principal_or_above", "unknown"]},
        "pay_ranges": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["min", "max", "currency", "period", "location"],
            "properties": {
                "min": {"type": ["number", "null"]},
                "max": {"type": ["number", "null"]},
                "currency": {"type": "string"},
                "period": {"type": "string", "enum": ["year", "hour"]},
                "location": {"type": ["string", "null"]},
            }}},
        "industry": {"type": "string", "enum": INDUSTRIES},
        "industry_reason": {"type": "string"},
        "role_domain": {"type": "string", "enum": DOMAINS},
        "fit_score": {"type": "integer"},
        "fit_reasons": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT = """You are screening a job posting for a senior product manager. Report facts from the posting
and score how well the candidate's resume fits. Do not decide whether they should apply.

Facts (use "unknown" or null when the posting doesn't say; never guess):
- work_mode: remote, hybrid or onsite, as the posting states it.
- employment_type and contract_months (contract length in months, if a contract).
- seniority: the level of the role from its title, scope and required experience.
  senior = senior PM or ~6+ years; principal_or_above = principal, staff, group, lead of PMs, director.
- pay_ranges: every salary range the posting lists, one entry per location if it lists several.
  Numbers as plain numbers (205500, not "205.5K"). currency as an ISO code (USD, CAD).
  If the currency symbol is "$" with no code, use USD for US locations and CAD for Canadian ones.
- industry: the HIRING COMPANY's industry, one of:
  fintech_payments, big_tech, health_tech, ecommerce_platform, cpg, retailer, other.
  ecommerce_platform means companies that build commerce software or marketplaces (e.g. Shopify,
  Amazon marketplace, Etsy). A company that mainly sells goods to consumers in stores or online
  (e.g. Walmart, Loblaw, 7-Eleven, a pharmacy chain) is a retailer, even when the role is digital.
  cpg means consumer packaged goods makers (e.g. P&G, PepsiCo, Unilever).
  industry_reason: one short sentence.
- role_domain: what the product THIS ROLE owns is about, whatever the company does:
  fintech_payments (payments, cards, banking, lending, money movement, fraud), health_tech,
  ecommerce_platform (checkout, merchants, marketplaces), or other.
  E.g. a PM owning card processing at a B2B SaaS company is fintech_payments.

Fit:
- fit_score: 0-100, how well the resume matches this role's requirements and domain.
- fit_reasons: 2-3 short, specific reasons citing the resume (e.g. "Led card-issuing product at X").
- concerns: 0-3 short gaps or risks (e.g. "Requires healthcare claims experience").

POSTING
Title: {title}
Company: {company}
Location: {location}
LinkedIn criteria: {criteria}

{description}

RESUME
{resume}
"""


def build_prompt(posting, resume):
    criteria = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in (posting.get("criteria") or {}).items()) or "none"
    return PROMPT.format(title=posting.get("title") or "", company=posting.get("company") or "",
                         location=posting.get("location") or "", criteria=criteria,
                         description=(posting.get("description") or "(no description)")[:DESCRIPTION_LIMIT],
                         resume=resume[:RESUME_LIMIT])


def validate(facts):
    """Normalize a model reply; raise ModelError if it can't be used."""
    missing = [k for k in SCHEMA["required"] if k not in facts]
    if missing:
        raise llm.ModelError(f"model reply missing {missing}")
    if facts["industry"] not in INDUSTRIES:
        raise llm.ModelError(f"unknown industry {facts['industry']!r}")
    if facts["role_domain"] not in DOMAINS:
        raise llm.ModelError(f"unknown role domain {facts['role_domain']!r}")
    facts["fit_score"] = max(0, min(100, int(facts["fit_score"])))
    facts["pay_ranges"] = [r for r in facts["pay_ranges"] if r.get("min") or r.get("max")]
    return facts


def extract(posting, resume, post=None):
    """(facts, call details). Raises llm.ModelError."""
    facts, call = llm.call_json(build_prompt(posting, resume), "posting_facts", SCHEMA, post)
    return validate(facts), call
