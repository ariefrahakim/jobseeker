"""The scoring engine: how well does a posting fit this specific profile?

Six dimensions, each scored 0-100 in isolation, then blended by the weights in
`profile.yaml`. Hard filters (reject titles, salary floor, onsite in the wrong
country, stale postings) short-circuit to a negative score so callers can drop
them without re-deriving the reason.
"""

from __future__ import annotations

import re

from .models import (
    Arrangement,
    Job,
    MatchResult,
    ScoreBreakdown,
    SalaryVerdict,
)
from .profile import Profile
from .salary import evaluate_salary, resolve_region

# Words that signal a level, mapped to a rank we can compare numerically.
_LEVELS = {
    "intern": 0, "internship": 0, "trainee": 0, "fresh graduate": 0,
    "junior": 1, "jr": 1, "entry level": 1, "entry-level": 1, "associate": 1,
    "mid": 2, "mid-level": 2, "intermediate": 2,
    "senior": 3, "sr": 3, "senior-level": 3,
    "lead": 4, "team lead": 4, "tech lead": 4, "manager": 4, "head of": 4,
    "principal": 5, "staff": 5, "architect": 5, "director": 5,
}
_LEVEL_ORDER = {"junior": 1, "mid": 2, "senior": 3, "lead": 4, "principal": 5}

_LEADERSHIP_HINTS = ("mentor", "coach", "lead a team", "team of", "line manage",
                     "grow the team", "hiring", "strategy", "roadmap",
                     "stakeholder", "leadership")


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Word-boundary containment, so "qa" does not match "qatar"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(needle.lower())}(?![a-z0-9])", haystack) is not None


def _token_overlap(a: str, b: str) -> float:
    """Fraction of `b`'s meaningful tokens present in `a`.

    Seniority words are stopped along with the usual filler: "senior" carries
    no information about *what* the role is, and leaving it in means
    "Senior Graphic Designer" scores 50% against "Senior Test Engineer".
    """
    stop = {
        "a", "an", "the", "of", "in", "and", "or", "for", "to",
        "engineer", "senior", "sr", "junior", "jr", "mid", "lead",
        "staff", "principal", "head", "manager",
    }
    tokens_b = {t for t in re.findall(r"[a-z0-9]+", b.lower()) if t not in stop}
    if not tokens_b:
        return 0.0
    tokens_a = set(re.findall(r"[a-z0-9]+", a.lower()))
    return len(tokens_b & tokens_a) / len(tokens_b)


class Matcher:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.weights = profile.scoring.get("weights", {})

    # ------------------------------------------------------------------ title
    def score_title(self, job: Job) -> tuple[float, list[str]]:
        title = job.title.lower()
        reasons: list[str] = []

        for primary in self.profile.primary_titles:
            if _contains_phrase(title, primary):
                return 100.0, [f"Title is a primary target: {primary}"]

        for accepted in self.profile.accept_titles:
            if _contains_phrase(title, accepted):
                return 80.0, [f"Title is an accepted variant: {accepted}"]

        # No exact hit — fall back to token overlap against the best target.
        best_overlap, best_title = 0.0, ""
        for candidate in self.profile.primary_titles + self.profile.accept_titles:
            overlap = _token_overlap(title, candidate)
            if overlap > best_overlap:
                best_overlap, best_title = overlap, candidate

        if best_overlap >= 0.5:
            reasons.append(f"Title partially matches {best_title}")
            return best_overlap * 70.0, reasons

        # Last resort: does it look like a testing role at all? Word boundaries
        # matter here — "qa" as a substring also appears inside "Qatar".
        if any(_contains_phrase(title, k) for k in ("qa", "quality", "test",
                                                    "testing", "sdet")):
            return 45.0, ["Testing-adjacent title"]
        return 10.0, []

    # ----------------------------------------------------------------- skills
    def score_skills(self, job: Job) -> tuple[float, list[str], list[str]]:
        text = job.searchable_text()
        weights = self.profile.skill_weights

        matched: list[str] = []
        earned = 0.0
        for skill, weight in weights.items():
            if _contains_phrase(text, skill):
                matched.append(skill)
                earned += weight

        # Normalise against a realistic ceiling rather than the sum of every
        # weight — no single posting mentions 60 tools, so dividing by the full
        # total would squash every score into the low teens.
        ceiling = sum(sorted(weights.values(), reverse=True)[:8]) or 1.0
        score = min(earned / ceiling, 1.0) * 100.0

        missing_core = [s for s in self.profile.core_skills if s not in matched]
        return score, sorted(matched), missing_core

    # -------------------------------------------------------------- seniority
    def score_seniority(self, job: Job) -> tuple[float, list[str], list[str]]:
        blob = f"{job.title} {job.description}".lower()
        reasons: list[str] = []
        warnings: list[str] = []

        found = [rank for word, rank in _LEVELS.items() if _contains_phrase(blob, word)]
        detected = max(found) if found else None

        floor = _LEVEL_ORDER.get(
            str(self.profile.seniority.get("min_level", "senior")).lower(), 3
        )

        if detected is None:
            score = 60.0  # unstated: neither a plus nor a dealbreaker
        elif detected >= floor:
            score = 100.0
            reasons.append("Seniority meets or exceeds your floor")
        else:
            # One notch below is a maybe; two or more is noise.
            gap = floor - detected
            score = max(0.0, 50.0 - gap * 25.0)
            warnings.append(
                f"Posting reads {gap} level(s) below your {self.profile.seniority.get('min_level')} floor"
            )

        if self.profile.seniority.get("prefer_leadership") and any(
            h in blob for h in _LEADERSHIP_HINTS
        ):
            score = min(score + 15.0, 100.0)
            reasons.append("Mentions leadership / mentoring scope")

        return score, reasons, warnings

    # ------------------------------------------------------------ arrangement
    def score_arrangement(self, job: Job) -> tuple[float, list[str]]:
        bonuses = self.profile.work_preference.get("arrangement_bonus", {})
        remote_bonus = float(bonuses.get("remote", 25))
        hybrid_bonus = float(bonuses.get("hybrid", 8))
        onsite_bonus = float(bonuses.get("onsite", 0))
        ceiling = max(remote_bonus, hybrid_bonus, onsite_bonus, 1.0)

        if job.arrangement is Arrangement.REMOTE:
            return remote_bonus / ceiling * 100.0, ["Fully remote"]
        if job.arrangement is Arrangement.HYBRID:
            return hybrid_bonus / ceiling * 100.0, ["Hybrid"]
        if job.arrangement is Arrangement.ONSITE:
            return onsite_bonus / ceiling * 100.0, []
        return 40.0, []  # unknown: assume the worst-but-not-fatal case

    # ----------------------------------------------------------------- domain
    def score_domain(self, job: Job) -> tuple[float, list[str]]:
        text = job.searchable_text()
        hits = [d for d in self.profile.domains if _contains_phrase(text, d)]
        if not hits:
            return 50.0, []
        return min(100.0, 60.0 + 20.0 * len(hits)), [f"Domain overlap: {', '.join(hits)}"]

    # -------------------------------------------------------------- freshness
    def score_freshness(self, job: Job) -> float:
        age = job.age_days
        if age is None:
            return 50.0
        max_age = self.profile.max_age_days
        return max(0.0, (1 - age / max_age)) * 100.0 if max_age else 50.0

    # ------------------------------------------------------------------ gates
    def _hard_reject(self, job: Job) -> str | None:
        """Return a rejection reason, or None to continue scoring."""
        title = job.title.lower()
        for bad in self.profile.reject_titles:
            if _contains_phrase(title, bad):
                return f"Rejected title pattern: {bad}"

        age = job.age_days
        if age is not None and age > self.profile.max_age_days:
            return f"Stale posting ({age:.0f} days old)"

        # Onsite is only acceptable where you already live.
        allowed_onsite = {
            c.lower() for c in self.profile.work_preference.get("onsite_countries", [])
        }
        if job.arrangement is Arrangement.ONSITE and job.country:
            if job.country.lower() not in allowed_onsite:
                return f"Onsite in {job.country}, outside your onsite countries"

        # Regions the profile does not target, unless the role is fully remote.
        region = job.region or "fallback"
        if region == "fallback":
            fallback = self.profile.salary_rules.get("fallback", {})
            if fallback.get("require_remote", True) and job.arrangement is not Arrangement.REMOTE:
                return f"Outside target regions and not remote ({job.location_raw or 'unknown'})"
        elif region not in self.profile.target_regions:
            if job.arrangement is not Arrangement.REMOTE:
                return f"Region '{region}' is not targeted and role is not remote"

        return None

    # ----------------------------------------------------------------- public
    def evaluate(self, job: Job) -> MatchResult:
        """Score one job. A negative score means it was hard-rejected."""
        # Resolve the region first — the salary rules and gates both need it.
        if job.region is None:
            job.region = resolve_region(job.country, self.profile.salary_rules)

        reject_reason = self._hard_reject(job)
        if reject_reason:
            return MatchResult(
                job=job,
                score=-1.0,
                breakdown=ScoreBreakdown(),
                warnings=[reject_reason],
            )

        verdict, monthly_idr, salary_note = evaluate_salary(
            job.salary, job.region, self.profile.salary_rules, self.profile.fx_to_idr
        )
        if verdict is SalaryVerdict.FAIL:
            return MatchResult(
                job=job,
                score=-1.0,
                breakdown=ScoreBreakdown(),
                salary_verdict=verdict,
                salary_monthly_idr=monthly_idr,
                warnings=[salary_note or "Salary below floor"],
            )

        reasons: list[str] = []
        warnings: list[str] = []
        if salary_note:
            warnings.append(salary_note)

        title_score, title_reasons = self.score_title(job)

        # A title veto. Some boards tag unrelated postings with "qa" or
        # "automation", and a generalist listing that name-drops every tool in
        # existence can out-score a real QA role on skills alone. If the title
        # is not a testing role, nothing else about the posting can rescue it.
        min_title = float(self.profile.scoring.get("min_title_score", 25))
        if title_score < min_title:
            return MatchResult(
                job=job,
                score=-1.0,
                breakdown=ScoreBreakdown(title=title_score),
                warnings=[f"Not a QA/test role: '{job.title}'"],
            )

        skills_score, matched, missing_core = self.score_skills(job)
        seniority_score, sen_reasons, sen_warnings = self.score_seniority(job)
        arrangement_score, arr_reasons = self.score_arrangement(job)
        domain_score, dom_reasons = self.score_domain(job)
        freshness_score = self.score_freshness(job)

        reasons += title_reasons + sen_reasons + arr_reasons + dom_reasons
        warnings += sen_warnings
        if matched:
            reasons.append(f"{len(matched)} skill match(es): {', '.join(matched[:8])}")
        if len(missing_core) > len(self.profile.core_skills) - 2:
            warnings.append("Barely any of your core tooling is mentioned")

        w = self.weights
        total_weight = sum(
            float(w.get(k, 0))
            for k in ("title", "skills", "seniority", "arrangement", "domain", "freshness")
        ) or 1.0
        total = (
            title_score * float(w.get("title", 0))
            + skills_score * float(w.get("skills", 0))
            + seniority_score * float(w.get("seniority", 0))
            + arrangement_score * float(w.get("arrangement", 0))
            + domain_score * float(w.get("domain", 0))
            + freshness_score * float(w.get("freshness", 0))
        ) / total_weight

        # A salary the region cannot verify should not outrank a verified one.
        if verdict is SalaryVerdict.UNKNOWN:
            total *= 0.92

        breakdown = ScoreBreakdown(
            title=title_score,
            skills=skills_score,
            seniority=seniority_score,
            arrangement=arrangement_score,
            domain=domain_score,
            freshness=freshness_score,
            total=total,
        )

        return MatchResult(
            job=job,
            score=round(min(total, 100.0), 1),
            breakdown=breakdown,
            matched_skills=matched,
            missing_core_skills=missing_core,
            salary_verdict=verdict,
            salary_monthly_idr=monthly_idr,
            reasons=reasons,
            warnings=warnings,
        )

    def rank(self, jobs: list[Job]) -> list[MatchResult]:
        """Score every job, drop rejects and sub-threshold matches, sort desc."""
        results = [self.evaluate(job) for job in jobs]
        kept = [r for r in results if not r.rejected and r.score >= self.profile.min_score]
        kept.sort(key=lambda r: r.score, reverse=True)
        return kept
