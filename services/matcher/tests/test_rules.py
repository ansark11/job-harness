"""Deterministic match rules. No model calls.

    python3 -m unittest discover -s tests -t .
"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rules  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
CFG = json.loads((ROOT / "criteria.example.json").read_text())["match"]
CFG = {**CFG, "comp": {**CFG["comp"], "min_total_cad": 225000}}
NOW = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)


def facts(**over):
    base = {"work_mode": "hybrid", "employment_type": "full_time", "contract_months": None,
            "seniority": "senior", "pay_ranges": [], "industry": "fintech_payments",
            "industry_reason": "Payments processor.", "role_domain": "fintech_payments",
            "role_type": "product_management", "role_type_reason": "Owns the roadmap.", "fit_score": 80, "fit_reasons": ["a"], "concerns": []}
    return {**base, **over}


def posting(**over):
    return {"id": "1", "title": "Senior Product Manager, Payments", "company": "Stripe", "description": "Own payouts.",
            "location": "Toronto, Ontario, Canada", "posted_at": "2026-10-11T18:00+00:00", "criteria": {}, **over}


class CountryTests(unittest.TestCase):
    def test_country(self):
        self.assertEqual(rules.country("Toronto, Ontario, Canada"), "CA")
        self.assertEqual(rules.country("Canada"), "CA")
        self.assertEqual(rules.country("United States"), "US")
        self.assertEqual(rules.country("New York, NY"), "US")
        self.assertEqual(rules.country("San Francisco Bay Area"), "US")
        self.assertIsNone(rules.country("London, England, United Kingdom"))
        self.assertIsNone(rules.country(""))


class PrefilterTests(unittest.TestCase):
    def test_all_excluded_industries_reject(self):
        r = rules.prefilter(posting(criteria={"industries": "Retail"}), CFG, NOW)
        self.assertEqual(r, ["LinkedIn industry is excluded: Retail"])

    def test_mixed_industries_go_to_the_model(self):
        self.assertEqual(rules.prefilter(posting(criteria={"industries": "Retail, Software Development"}), CFG, NOW), [])

    def test_part_time_and_age(self):
        r = rules.prefilter(posting(criteria={"employment_type": "Part-time"}, posted_at="2026-09-01T00:00+00:00"),
                            CFG, NOW)
        self.assertEqual(len(r), 2)


class PayTests(unittest.TestCase):
    def test_prefers_range_for_posting_city(self):
        ranges = [{"min": 205500, "max": 278000, "currency": "USD", "period": "year", "location": "Mountain View"},
                  {"min": 162000, "max": 219000, "currency": "CAD", "period": "year", "location": "Toronto, Ontario"}]
        pay = rules.choose_pay(ranges, "Toronto, Ontario, Canada", CFG["comp"])
        self.assertEqual(pay["max_cad"], 219000)
        self.assertEqual(pay["chosen_because"], "matches posting location")

    def test_highest_band_when_no_city_match_and_usd_converted(self):
        ranges = [{"min": 150000, "max": 200000, "currency": "USD", "period": "year", "location": None}]
        pay = rules.choose_pay(ranges, "United States", CFG["comp"])
        self.assertEqual(pay["max_cad"], 276000)

    def test_hourly_annualized_and_unknown_currency_ignored(self):
        pay = rules.choose_pay([{"min": 100, "max": 120, "currency": "CAD", "period": "hour", "location": None},
                                {"min": 1, "max": 2, "currency": "XYZ", "period": "year", "location": None}],
                               "Canada", CFG["comp"])
        self.assertEqual(pay["max_cad"], 249600)
        self.assertIsNone(rules.choose_pay([], "Canada", CFG["comp"]))


class DecideTests(unittest.TestCase):
    def decide(self, p=None, f=None):
        reasons, flags, derived = rules.decide(p or posting(), f or facts(), CFG)
        return reasons, flags, derived

    def test_clean_match_flags_missing_pay(self):
        reasons, flags, _ = self.decide()
        self.assertEqual(reasons, [])
        self.assertEqual(flags, ["pay not listed"])

    def test_onsite_rejected(self):
        self.assertIn("on-site role", self.decide(f=facts(work_mode="onsite"))[0])

    def test_canada_outside_metro_needs_remote(self):
        p = posting(location="Ottawa, Ontario, Canada")
        self.assertTrue(self.decide(p)[0])
        self.assertEqual(self.decide(p, facts(work_mode="remote"))[0], [])

    def test_us_hybrid_anywhere_ok(self):
        self.assertEqual(self.decide(posting(location="Austin, TX"))[0], [])

    def test_short_contract_rejected_unknown_length_flagged(self):
        self.assertTrue(self.decide(f=facts(employment_type="contract", contract_months=6))[0])
        reasons, flags, _ = self.decide(f=facts(employment_type="contract"))
        self.assertEqual(reasons, [])
        self.assertIn("contract length not stated", flags)

    def test_pay_below_floor_rejected_within_tolerance_flagged(self):
        low = [{"min": 125000, "max": 145000, "currency": "CAD", "period": "year", "location": None}]
        self.assertIn("pay tops out", self.decide(f=facts(pay_ranges=low))[0][0])
        near = [{"min": 180000, "max": 220000, "currency": "CAD", "period": "year", "location": None}]
        reasons, flags, _ = self.decide(f=facts(pay_ranges=near))
        self.assertEqual(reasons, [])
        self.assertTrue(any("just under" in f for f in flags))

    def test_retailer_rejected_but_ecommerce_platform_kept(self):
        self.assertIn("company is a retailer", self.decide(f=facts(industry="retailer"))[0][0])
        self.assertEqual(self.decide(posting(company="Shopify"), facts(industry="ecommerce_platform"))[0], [])

    def test_payments_role_at_other_company_kept_but_not_at_a_retailer(self):
        self.assertEqual(self.decide(posting(company="B2B Saas Company"), facts(industry="other"))[0], [])
        reasons = self.decide(posting(company="Walmart"), facts(industry="retailer"))[0]
        self.assertIn("company is a retailer", reasons[0])
        self.assertTrue(self.decide(f=facts(industry="other", role_domain="other"))[0])

    def test_big_tech_allowlist_overrides_model_category(self):
        reasons, _, derived = self.decide(posting(company="Amazon"), facts(industry="other", role_domain="other"))
        self.assertEqual(reasons, [])
        self.assertEqual(derived["industry"], "big_tech")
        # Prefix match only on a word boundary: "Metaview" is not Meta.
        self.assertTrue(self.decide(posting(company="Metaview"), facts(industry="other", role_domain="other"))[0])

    def test_program_delivery_titled_product_is_rejected(self):
        p = posting(title="HCM Strategy, Data Program Product Management, Vice President", company="Goldman Sachs")
        reasons = self.decide(p, facts(role_type="program_or_delivery",
                                       role_type_reason="Runs an HR system rollout."))[0]
        self.assertIn("not a product management role: program or delivery", reasons[0])

    def test_unclear_role_type_and_missing_description_only_flag(self):
        reasons, flags, _ = self.decide(posting(description=""), facts(role_type="unclear"))
        self.assertEqual(reasons, [])
        self.assertIn("couldn't confirm it's a product management role", flags)
        self.assertTrue(any("no description" in f for f in flags))

    def test_untitled_seniority_uses_model_level(self):
        p = posting(title="Product Manager, Payments")
        self.assertTrue(self.decide(p, facts(seniority="mid"))[0])
        self.assertEqual(self.decide(p, facts(seniority="principal_or_above"))[0], [])
        self.assertIn("seniority unclear", self.decide(p, facts(seniority="unknown"))[1])


if __name__ == "__main__":
    unittest.main()
