"""Core data structures shared by scrapers, matcher, storage and reports."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Arrangement(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class SalaryVerdict(str, Enum):
    PASS = "pass"           # meets the region's floor
    FAIL = "fail"           # stated and below the floor
    UNKNOWN = "unknown"     # not stated, region enforces -> flagged
    NOT_ENFORCED = "n/a"    # region has no floor (Asia / Europe)


class Period(str, Enum):
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


# Hours/days assumed when normalising a rate to a monthly figure.
_HOURS_PER_MONTH = 173.33
_DAYS_PER_MONTH = 21.67


@dataclass
class Salary:
    """A salary as stated by the posting, plus its monthly IDR equivalent."""

    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    period: Period | None = None
    raw: str = ""

    @property
    def stated(self) -> bool:
        return self.min_amount is not None or self.max_amount is not None

    @property
    def midpoint(self) -> float | None:
        values = [v for v in (self.min_amount, self.max_amount) if v is not None]
        return sum(values) / len(values) if values else None

    def to_monthly(self, amount: float) -> float:
        """Convert `amount` from this salary's period into a monthly figure."""
        if self.period is Period.YEAR:
            return amount / 12
        if self.period is Period.HOUR:
            return amount * _HOURS_PER_MONTH
        if self.period is Period.DAY:
            return amount * _DAYS_PER_MONTH
        return amount  # already monthly, or unknown -> assume monthly

    def monthly_idr(self, fx_to_idr: dict[str, float]) -> float | None:
        """Lower bound of the range, normalised to IDR per month.

        The lower bound is deliberate: a floor check should use the worst case
        the posting commits to, not its optimistic ceiling.
        """
        base = self.min_amount if self.min_amount is not None else self.max_amount
        if base is None:
            return None
        rate = fx_to_idr.get((self.currency or "IDR").upper())
        if rate is None:
            return None
        return self.to_monthly(base) * rate

    def human(self) -> str:
        if not self.stated:
            return "not stated"
        cur = self.currency or ""
        per = f"/{self.period.value}" if self.period else ""
        if self.min_amount is not None and self.max_amount is not None:
            return f"{cur} {self.min_amount:,.0f}–{self.max_amount:,.0f}{per}".strip()
        one = self.min_amount if self.min_amount is not None else self.max_amount
        return f"{cur} {one:,.0f}{per}".strip()


@dataclass
class Job:
    """A normalised posting. Every scraper must produce one of these."""

    title: str
    company: str
    url: str
    source: str
    description: str = ""
    location_raw: str = ""
    country: str | None = None
    region: str | None = None          # resolved profile region key
    arrangement: Arrangement = Arrangement.UNKNOWN
    salary: Salary = field(default_factory=Salary)
    posted_at: datetime | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable identity across sources, so the same job posted to three
        boards collapses into one row.

        Company + normalised title is the strongest available signal; URLs
        differ per board and descriptions get reworded.
        """
        norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        seed = f"{norm(self.company)}|{norm(self.title)}|{norm(self.country or '')}"
        return hashlib.sha256(seed.encode()).hexdigest()[:16]

    @property
    def age_days(self) -> float | None:
        if self.posted_at is None:
            return None
        delta = datetime.now(timezone.utc) - self.posted_at
        return max(delta.total_seconds() / 86400, 0.0)

    def searchable_text(self) -> str:
        return " ".join(
            [self.title, self.company, self.description, " ".join(self.tags)]
        ).lower()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["arrangement"] = self.arrangement.value
        d["salary"]["period"] = self.salary.period.value if self.salary.period else None
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["scraped_at"] = self.scraped_at.isoformat()
        d["fingerprint"] = self.fingerprint
        return d


@dataclass
class ScoreBreakdown:
    """Per-dimension scores, kept so a report can explain *why* a job ranked."""

    title: float = 0.0
    skills: float = 0.0
    seniority: float = 0.0
    arrangement: float = 0.0
    domain: float = 0.0
    freshness: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "title": round(self.title, 1),
            "skills": round(self.skills, 1),
            "seniority": round(self.seniority, 1),
            "arrangement": round(self.arrangement, 1),
            "domain": round(self.domain, 1),
            "freshness": round(self.freshness, 1),
            "total": round(self.total, 1),
        }


@dataclass
class MatchResult:
    job: Job
    score: float
    breakdown: ScoreBreakdown
    matched_skills: list[str] = field(default_factory=list)
    missing_core_skills: list[str] = field(default_factory=list)
    salary_verdict: SalaryVerdict = SalaryVerdict.UNKNOWN
    salary_monthly_idr: float | None = None
    reasons: list[str] = field(default_factory=list)   # why it passed
    warnings: list[str] = field(default_factory=list)  # caveats worth reading
    llm_summary: str | None = None
    llm_fit: str | None = None

    @property
    def rejected(self) -> bool:
        return self.score < 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.job.to_dict(),
            "score": round(self.score, 1),
            "breakdown": self.breakdown.as_dict(),
            "matched_skills": self.matched_skills,
            "missing_core_skills": self.missing_core_skills,
            "salary_verdict": self.salary_verdict.value,
            "salary_monthly_idr": self.salary_monthly_idr,
            "salary_human": self.job.salary.human(),
            "reasons": self.reasons,
            "warnings": self.warnings,
            "llm_summary": self.llm_summary,
            "llm_fit": self.llm_fit,
        }
