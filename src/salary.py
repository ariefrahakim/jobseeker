"""Salary parsing and the region-aware floor check.

Job boards state compensation in a dozen incompatible shapes. This module turns
free text like "Rp 35jt - 45jt/bulan", "$120k–150k a year" or "€75.000 p.a."
into a `Salary`, then decides whether it clears the floor for its region.

The rules encoded here come straight from `config/profile.yaml`:
  * Indonesia          -> hard floor, IDR 30,000,000 per month
  * Asia (non-ID), EU  -> no floor at all
  * everywhere else    -> no floor, but must be fully remote
"""

from __future__ import annotations

import re

from .models import Period, Salary, SalaryVerdict

# Currency spellings seen in the wild, mapped to ISO codes.
_CURRENCY_TOKENS = {
    "idr": "IDR", "rp": "IDR", "rupiah": "IDR",
    "usd": "USD", "us$": "USD", "$": "USD", "dollar": "USD",
    "eur": "EUR", "€": "EUR", "euro": "EUR",
    "gbp": "GBP", "£": "GBP", "pound": "GBP",
    "sgd": "SGD", "s$": "SGD",
    "myr": "MYR", "rm": "MYR",
    "aud": "AUD", "a$": "AUD",
    "jpy": "JPY", "¥": "JPY", "yen": "JPY",
    "inr": "INR", "₹": "INR",
    "aed": "AED", "chf": "CHF", "sek": "SEK", "pln": "PLN",
}

_PERIOD_TOKENS = {
    Period.HOUR: ("per hour", "/hour", "/hr", "hourly", "an hour", "per jam"),
    Period.DAY: ("per day", "/day", "daily", "a day", "per hari"),
    Period.MONTH: ("per month", "/month", "/mo", "monthly", "a month",
                   "per bulan", "/bulan", "sebulan", "pcm"),
    Period.YEAR: ("per year", "/year", "/yr", "annually", "annual", "a year",
                  "per annum", "p.a.", "pa", "per tahun", "/tahun"),
}

# Indonesian shorthand: 30jt / 30 juta = 30,000,000. rb / ribu = thousand.
_ID_MULTIPLIERS = {"jt": 1_000_000, "juta": 1_000_000, "m": 1_000_000,
                   "rb": 1_000, "ribu": 1_000, "k": 1_000}

_NUMBER = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:\.\d+)?"
_AMOUNT_RE = re.compile(
    rf"(?P<num>{_NUMBER})\s*(?P<mult>jt|juta|ribu|rb|k|m)?\b",
    re.IGNORECASE,
)


def _parse_number(raw: str, multiplier: str | None) -> float | None:
    """Turn "1.200,50", "1,200.50", "120k" or "35jt" into a float."""
    text = raw.strip()
    if not text:
        return None

    # Disambiguate thousands separators from decimal points. Whichever
    # separator appears last is the decimal one, if it has <= 2 trailing digits.
    last_dot, last_comma = text.rfind("."), text.rfind(",")
    if last_dot > -1 and last_comma > -1:
        dec_sep = "." if last_dot > last_comma else ","
        thou_sep = "," if dec_sep == "." else "."
        text = text.replace(thou_sep, "").replace(dec_sep, ".")
    elif last_comma > -1:
        tail = text[last_comma + 1:]
        text = text.replace(",", "." if len(tail) <= 2 else "")
    elif last_dot > -1:
        tail = text[last_dot + 1:]
        if len(tail) == 3:  # "1.200" is Indonesian/European thousands
            text = text.replace(".", "")

    try:
        value = float(text)
    except ValueError:
        return None

    if multiplier:
        value *= _ID_MULTIPLIERS.get(multiplier.lower(), 1)
    return value


# Indonesian-language salary vocabulary. A posting reading "Gaji 32 juta per
# bulan" never names a currency, because to its audience the currency is
# obvious — these tokens are how we recover it.
_ID_LANGUAGE_HINTS = ("jt", "juta", "ribu", "rb", "gaji", "bulan", "tahun",
                      "per bulan", "sebulan", "upah")


def detect_currency(text: str) -> str | None:
    lowered = text.lower()
    # Longest token first so "us$" wins over "$". Alphabetic tokens need word
    # boundaries — without them "rm" matches inside "platform" and prices a
    # Berlin salary in Malaysian ringgit. Symbol tokens ($, €) cannot use \b.
    for token in sorted(_CURRENCY_TOKENS, key=len, reverse=True):
        if token.isalpha():
            if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", lowered):
                return _CURRENCY_TOKENS[token]
        elif token in lowered:
            return _CURRENCY_TOKENS[token]

    # No explicit currency. Indonesian shorthand or vocabulary implies IDR.
    for hint in _ID_LANGUAGE_HINTS:
        if re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", lowered):
            return "IDR"
    return None


def detect_period(text: str) -> Period | None:
    lowered = text.lower()
    for period, tokens in _PERIOD_TOKENS.items():
        if any(t in lowered for t in tokens):
            return period
    return None


def infer_period(amount: float, currency: str | None) -> Period:
    """Guess the period when the posting does not say.

    Magnitude is the only clue available, and it is a good one: nobody is paid
    IDR 40,000,000 an hour, and nobody is paid USD 150,000 a month.
    """
    if (currency or "").upper() == "IDR":
        return Period.MONTH if amount >= 1_000_000 else Period.YEAR
    if amount < 500:
        return Period.HOUR
    if amount < 30_000:
        return Period.MONTH
    return Period.YEAR


def parse_salary(text: str | None) -> Salary:
    """Best-effort parse of a free-text compensation string."""
    if not text:
        return Salary()

    raw = " ".join(str(text).split())
    currency = detect_currency(raw)
    period = detect_period(raw)

    # Strip year-like tokens so "2024 bonus" is not read as a salary.
    cleaned = re.sub(r"\b(19|20)\d{2}\b", " ", raw)

    amounts: list[float] = []
    for m in _AMOUNT_RE.finditer(cleaned):
        value = _parse_number(m.group("num"), m.group("mult"))
        if value is None or value <= 0:
            continue
        # Discard obvious non-salary noise (headcounts, percentages, versions).
        if value < 3 and not m.group("mult"):
            continue
        amounts.append(value)

    if not amounts:
        return Salary(currency=currency, period=period, raw=raw)

    # A range shorthand like "35jt - 45jt" leaves both sides with the same
    # multiplier; "120 - 150k" leaves only the last one. Backfill the smaller
    # side when the gap is implausibly large.
    if len(amounts) >= 2:
        lo, hi = min(amounts), max(amounts)
        if hi / lo >= 1000:
            lo *= 1000 if hi / lo < 1_000_000 else 1_000_000
        amounts = [lo, hi]

    lo, hi = min(amounts), max(amounts)
    if period is None:
        period = infer_period(lo, currency)

    return Salary(
        min_amount=lo,
        max_amount=hi if hi != lo else None,
        currency=currency,
        period=period,
        raw=raw,
    )


def resolve_region(country: str | None, rules: dict) -> str:
    """Map a country name onto a profile region key, or `fallback`."""
    if not country:
        return "fallback"
    target = country.strip().lower()
    for region, cfg in (rules.get("regions") or {}).items():
        for candidate in cfg.get("countries", []):
            if candidate.strip().lower() == target:
                return region
    return "fallback"


def evaluate_salary(
    salary: Salary,
    region: str,
    rules: dict,
    fx_to_idr: dict[str, float],
) -> tuple[SalaryVerdict, float | None, str | None]:
    """Apply the region's floor.

    Returns `(verdict, monthly_idr, message)`. `message` is a human-readable
    note for the report — a warning when the salary is missing or short.
    """
    region_cfg = (rules.get("regions") or {}).get(region) or rules.get("fallback", {})
    monthly_idr = salary.monthly_idr(fx_to_idr)

    if not region_cfg.get("enforce", False):
        return SalaryVerdict.NOT_ENFORCED, monthly_idr, None

    floor_cfg = region_cfg.get("min_monthly") or {}
    floor_amount = float(floor_cfg.get("amount", 0) or 0)
    floor_currency = (floor_cfg.get("currency") or "IDR").upper()
    floor_idr = floor_amount * fx_to_idr.get(floor_currency, 1)

    if monthly_idr is None:
        on_missing = region_cfg.get("on_missing", "flag")
        note = (
            f"Salary not stated; region enforces a floor of "
            f"{floor_currency} {floor_amount:,.0f}/month — confirm before applying."
        )
        if on_missing == "reject":
            return SalaryVerdict.FAIL, None, note
        return SalaryVerdict.UNKNOWN, None, note

    if monthly_idr < floor_idr:
        note = (
            f"Below floor: ~IDR {monthly_idr:,.0f}/month vs required "
            f"IDR {floor_idr:,.0f}/month."
        )
        return SalaryVerdict.FAIL, monthly_idr, note

    return SalaryVerdict.PASS, monthly_idr, None
