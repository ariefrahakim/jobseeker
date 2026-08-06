"""Salary parsing and floor enforcement.

The IDR 30jt rule is the one piece of business logic that must not be wrong, so
these tests cover the formats Indonesian postings actually use, plus the
currency and period normalisation that feeds the comparison.
"""

from __future__ import annotations

import pytest

from src.models import Period, SalaryVerdict
from src.salary import (
    detect_currency,
    evaluate_salary,
    parse_salary,
    resolve_region,
)

FX = {"IDR": 1, "USD": 16300, "EUR": 17600, "SGD": 12100, "GBP": 20600}

RULES = {
    "regions": {
        "indonesia": {
            "countries": ["Indonesia"],
            "enforce": True,
            "min_monthly": {"amount": 30_000_000, "currency": "IDR"},
            "on_missing": "flag",
        },
        "asia": {"countries": ["Singapore", "Japan"], "enforce": False, "on_missing": "ignore"},
        "europe": {"countries": ["Germany"], "enforce": False, "on_missing": "ignore"},
    },
    "fallback": {"enforce": False, "on_missing": "ignore", "require_remote": True},
}


class TestParseIndonesian:
    @pytest.mark.parametrize(
        "text,expected_min",
        [
            ("Rp 35.000.000 - Rp 45.000.000 per bulan", 35_000_000),
            ("IDR 30jt - 40jt/bulan", 30_000_000),
            ("Gaji 32 juta per bulan", 32_000_000),
            ("Rp30.000.000/bulan", 30_000_000),
            ("28jt - 35jt", 28_000_000),
        ],
    )
    def test_rupiah_shorthand(self, text, expected_min):
        salary = parse_salary(text)
        assert salary.currency == "IDR"
        assert salary.min_amount == pytest.approx(expected_min)

    def test_monthly_period_inferred_for_large_idr(self):
        # No period stated; IDR magnitude implies monthly, not annual.
        salary = parse_salary("Rp 40.000.000")
        assert salary.period is Period.MONTH

    def test_ribu_multiplier(self):
        assert parse_salary("Rp 500rb per hari").min_amount == pytest.approx(500_000)


class TestParseForeign:
    def test_usd_annual_k_shorthand(self):
        salary = parse_salary("$120k - $150k a year")
        assert salary.currency == "USD"
        assert salary.period is Period.YEAR
        assert salary.min_amount == pytest.approx(120_000)
        assert salary.max_amount == pytest.approx(150_000)

    def test_european_thousands_separator(self):
        salary = parse_salary("€75.000 - €95.000 p.a.")
        assert salary.currency == "EUR"
        assert salary.period is Period.YEAR
        assert salary.min_amount == pytest.approx(75_000)

    def test_hourly_rate(self):
        salary = parse_salary("$85 per hour")
        assert salary.period is Period.HOUR
        assert salary.min_amount == pytest.approx(85)

    def test_comma_decimal_vs_thousands(self):
        assert parse_salary("SGD 8,500 per month").min_amount == pytest.approx(8500)

    def test_alpha_currency_tokens_need_word_boundaries(self):
        # Seen live: "rm" inside "platform"/"perform" priced a Berlin salary in
        # Malaysian ringgit. Alphabetic currency codes must not match substrings.
        salary = parse_salary("Our platform team performs well. €80.000 p.a.")
        assert salary.currency == "EUR"
        assert detect_currency("cross-platform performance work") is None

    def test_unparseable_text_is_not_a_salary(self):
        for text in (None, "", "Competitive salary", "Negotiable", "DOE"):
            assert not parse_salary(text).stated


class TestNormalisation:
    def test_annual_usd_to_monthly_idr(self):
        salary = parse_salary("$120,000 per year")
        monthly = salary.monthly_idr(FX)
        assert monthly == pytest.approx(120_000 / 12 * 16_300)

    def test_floor_uses_lower_bound_not_midpoint(self):
        # A range whose bottom misses the floor must not pass on its average.
        salary = parse_salary("Rp 25jt - 45jt per bulan")
        assert salary.monthly_idr(FX) == pytest.approx(25_000_000)

    def test_unknown_currency_yields_no_comparison(self):
        salary = parse_salary("BTC 2 per month")
        assert salary.monthly_idr(FX) is None


class TestRegionResolution:
    @pytest.mark.parametrize(
        "country,region",
        [
            ("Indonesia", "indonesia"),
            ("Singapore", "asia"),
            ("Germany", "europe"),
            ("United States", "fallback"),
            (None, "fallback"),
        ],
    )
    def test_country_maps_to_region(self, country, region):
        assert resolve_region(country, RULES) == region


class TestFloorEnforcement:
    def test_indonesian_role_above_floor_passes(self):
        verdict, monthly, note = evaluate_salary(
            parse_salary("Rp 40jt per bulan"), "indonesia", RULES, FX
        )
        assert verdict is SalaryVerdict.PASS
        assert monthly == pytest.approx(40_000_000)
        assert note is None

    def test_indonesian_role_below_floor_fails(self):
        verdict, monthly, note = evaluate_salary(
            parse_salary("Rp 22jt per bulan"), "indonesia", RULES, FX
        )
        assert verdict is SalaryVerdict.FAIL
        assert monthly == pytest.approx(22_000_000)
        assert "Below floor" in note

    def test_indonesian_role_at_exactly_the_floor_passes(self):
        verdict, _, _ = evaluate_salary(
            parse_salary("Rp 30.000.000 per bulan"), "indonesia", RULES, FX
        )
        assert verdict is SalaryVerdict.PASS

    def test_missing_salary_in_indonesia_is_flagged_not_rejected(self):
        verdict, monthly, note = evaluate_salary(
            parse_salary("Competitive"), "indonesia", RULES, FX
        )
        assert verdict is SalaryVerdict.UNKNOWN
        assert monthly is None
        assert "not stated" in note

    def test_on_missing_reject_turns_unknown_into_fail(self):
        strict = {
            "regions": {
                "indonesia": {
                    **RULES["regions"]["indonesia"],
                    "on_missing": "reject",
                }
            },
            "fallback": RULES["fallback"],
        }
        verdict, _, _ = evaluate_salary(parse_salary(""), "indonesia", strict, FX)
        assert verdict is SalaryVerdict.FAIL

    @pytest.mark.parametrize("region", ["asia", "europe"])
    def test_asia_and_europe_ignore_salary_entirely(self, region):
        # A low number and a missing number must both be accepted.
        for text in ("Rp 5jt per bulan", "", "€20.000 p.a."):
            verdict, _, note = evaluate_salary(parse_salary(text), region, RULES, FX)
            assert verdict is SalaryVerdict.NOT_ENFORCED
            assert note is None

    def test_foreign_currency_converted_before_comparison(self):
        # A Jakarta role quoted in USD: $2,500/month clears 30jt at 16,300.
        verdict, monthly, _ = evaluate_salary(
            parse_salary("$2,500 per month"), "indonesia", RULES, FX
        )
        assert monthly == pytest.approx(2500 * 16_300)
        assert verdict is SalaryVerdict.PASS
