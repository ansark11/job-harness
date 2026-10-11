"""Option matching and label cleanup (no browser).

    ./python/bin/python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forms  # noqa: E402


class MatchOptionTests(unittest.TestCase):
    def test_exact_prefix_and_contains(self):
        self.assertEqual(forms.match_option("no", ["Yes", "No"]), "No")
        self.assertEqual(forms.match_option("No", ["Yes, I will", "No, I will not require sponsorship"]),
                         "No, I will not require sponsorship")
        self.assertEqual(forms.match_option("Asian", ["White", "Asian (Not Hispanic or Latino)", "Black"]),
                         "Asian (Not Hispanic or Latino)")

    def test_ambiguous_or_missing_is_none(self):
        self.assertIsNone(forms.match_option("Maybe", ["Yes", "No"]))
        self.assertIsNone(forms.match_option("Asian", ["Asian - East", "Asian - South"]))
        self.assertIsNone(forms.match_option(None, ["Yes"]))

    def test_clean_label_and_normalize_match_tracker_rules(self):
        self.assertEqual(forms.clean_label("Email address*"), "Email address")
        self.assertEqual(forms.clean_label("Phone Phone"), "Phone")
        self.assertEqual(forms.normalize("Will you require sponsorship? *"), "will you require sponsorship")


if __name__ == "__main__":
    unittest.main()
