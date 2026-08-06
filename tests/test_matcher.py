"""Scoring and hard-filter behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.matcher import Matcher
from src.models import Arrangement, Job, SalaryVerdict
from src.profile import Profile
from src.salary import parse_salary


@pytest.fixture(scope="module")
def profile() -> Profile:
    return Profile.load()


@pytest.fixture(scope="module")
def matcher(profile: Profile) -> Matcher:
    return Matcher(profile)


def make_job(**overrides) -> Job:
    defaults = dict(
        title="QA Automation Lead",
        company="Acme",
        url="https://example.com/job/1",
        source="test",
        description=(
            "Lead a team of QA engineers. Build and maintain a Cypress and "
            "Playwright automation framework. API automation with RestAssured. "
            "CI/CD on Azure DevOps. Performance testing with k6."
        ),
        location_raw="Jakarta, Indonesia",
        country="Indonesia",
        arrangement=Arrangement.REMOTE,
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestTitleScoring:
    def test_primary_title_scores_top(self, matcher):
        score, reasons = matcher.score_title(make_job(title="QA Automation Lead"))
        assert score == 100.0
        assert "primary target" in reasons[0]

    def test_accepted_variant_scores_high(self, matcher):
        score, _ = matcher.score_title(make_job(title="Senior Test Engineer"))
        assert 70 <= score <= 90

    def test_unrelated_title_scores_low(self, matcher):
        score, _ = matcher.score_title(make_job(title="Frontend Developer"))
        assert score <= 20

    def test_testing_adjacent_title_gets_partial_credit(self, matcher):
        score, _ = matcher.score_title(make_job(title="Quality Assurance Specialist"))
        assert score >= 40

    def test_word_boundaries_prevent_false_matches(self, matcher):
        # "qa" must not be found inside "Qatar".
        score, _ = matcher.score_title(make_job(title="Business Analyst, Qatar"))
        assert score <= 20


class TestSkillScoring:
    def test_dense_stack_match_scores_well(self, matcher):
        score, matched, _ = matcher.score_skills(make_job())
        assert score >= 60
        assert "cypress" in matched
        assert "playwright" in matched
        assert "restassured" in matched

    def test_no_tooling_mentioned_scores_near_zero(self, matcher):
        job = make_job(description="You will ensure product quality. Great team.")
        score, matched, missing = matcher.score_skills(job)
        assert score < 15
        assert matched == []
        assert len(missing) == len(matcher.profile.core_skills)

    def test_missing_core_skills_are_reported(self, matcher):
        job = make_job(description="Selenium and Java only.")
        _, matched, missing = matcher.score_skills(job)
        assert "selenium" in matched
        assert "cypress" in missing


class TestSeniority:
    def test_lead_role_meets_the_floor(self, matcher):
        score, reasons, warnings = matcher.score_seniority(
            make_job(title="Lead QA Engineer", description="Mentor a team of four.")
        )
        assert score == 100.0
        assert warnings == []
        assert any("leadership" in r or "Seniority" in r for r in reasons)

    def test_junior_role_is_penalised_and_warned(self, matcher):
        score, _, warnings = matcher.score_seniority(
            make_job(title="Junior QA Engineer", description="Entry level position.")
        )
        assert score < 50
        assert warnings

    def test_unstated_seniority_is_neutral(self, matcher):
        score, _, _ = matcher.score_seniority(
            make_job(title="QA Engineer", description="Write tests.")
        )
        assert 50 <= score <= 80


class TestArrangement:
    def test_remote_beats_hybrid_beats_onsite(self, matcher):
        remote, _ = matcher.score_arrangement(make_job(arrangement=Arrangement.REMOTE))
        hybrid, _ = matcher.score_arrangement(make_job(arrangement=Arrangement.HYBRID))
        onsite, _ = matcher.score_arrangement(make_job(arrangement=Arrangement.ONSITE))
        assert remote > hybrid > onsite


class TestHardFilters:
    def test_rejected_title_pattern_is_dropped(self, matcher):
        result = matcher.evaluate(make_job(title="Manual Tester"))
        assert result.rejected
        assert "Rejected title" in result.warnings[0]

    def test_stale_posting_is_dropped(self, matcher):
        old = datetime.now(timezone.utc) - timedelta(days=120)
        result = matcher.evaluate(make_job(posted_at=old))
        assert result.rejected
        assert "Stale" in result.warnings[0]

    def test_onsite_outside_indonesia_is_dropped(self, matcher):
        result = matcher.evaluate(
            make_job(
                arrangement=Arrangement.ONSITE,
                country="Germany",
                location_raw="Berlin, Germany",
            )
        )
        assert result.rejected
        assert "Onsite" in result.warnings[0]

    def test_non_qa_title_is_vetoed_however_good_the_skill_match(self, matcher):
        # Seen live: boards tag unrelated postings "qa"/"automation", and a
        # listing that name-drops every tool out-scores real QA roles.
        result = matcher.evaluate(
            make_job(
                title="Senior Graphic Designer",
                description=(
                    "Cypress Playwright Selenium RestAssured k6 JMeter Azure DevOps "
                    "Jenkins SQL BigQuery TypeScript Java Python Appium test automation"
                ),
            )
        )
        assert result.rejected
        assert "Not a QA/test role" in result.warnings[0]

    def test_onsite_in_jakarta_is_kept(self, matcher):
        result = matcher.evaluate(
            make_job(arrangement=Arrangement.ONSITE, salary=parse_salary("Rp 40jt per bulan"))
        )
        assert not result.rejected

    def test_indonesian_role_below_floor_is_dropped(self, matcher):
        result = matcher.evaluate(make_job(salary=parse_salary("Rp 18jt per bulan")))
        assert result.rejected
        assert result.salary_verdict is SalaryVerdict.FAIL

    def test_low_paid_european_role_is_kept(self, matcher):
        # Europe has no floor, so a small number must not disqualify the role.
        result = matcher.evaluate(
            make_job(
                country="Netherlands",
                location_raw="Amsterdam, Netherlands (remote)",
                salary=parse_salary("€38.000 p.a."),
            )
        )
        assert not result.rejected
        assert result.salary_verdict is SalaryVerdict.NOT_ENFORCED

    def test_non_target_region_kept_only_when_remote(self, matcher):
        remote_us = matcher.evaluate(
            make_job(country="United States", location_raw="Remote (US)",
                     arrangement=Arrangement.REMOTE)
        )
        hybrid_us = matcher.evaluate(
            make_job(country="United States", location_raw="Austin, TX",
                     arrangement=Arrangement.HYBRID)
        )
        assert not remote_us.rejected
        assert hybrid_us.rejected


class TestEndToEndScoring:
    def test_ideal_role_scores_high(self, matcher):
        result = matcher.evaluate(
            make_job(
                title="QA Automation Lead",
                description=(
                    "Lead and mentor a team of SDETs. Own our Cypress and Playwright "
                    "test automation framework. API automation with RestAssured, "
                    "performance testing with k6 and JMeter, CI/CD in Azure DevOps, "
                    "SQL and BigQuery data validation. Fintech platform."
                ),
                salary=parse_salary("Rp 45jt - 55jt per bulan"),
            )
        )
        assert result.score >= 75
        assert result.salary_verdict is SalaryVerdict.PASS

    def test_unknown_salary_in_indonesia_is_penalised_but_surfaced(self, matcher):
        stated = matcher.evaluate(make_job(salary=parse_salary("Rp 40jt per bulan")))
        unstated = matcher.evaluate(make_job(salary=parse_salary("Competitive")))
        assert unstated.salary_verdict is SalaryVerdict.UNKNOWN
        assert unstated.score < stated.score
        assert any("not stated" in w for w in unstated.warnings)

    def test_rank_drops_rejects_and_sorts_descending(self, matcher):
        jobs = [
            make_job(title="Manual Tester"),
            make_job(title="QA Automation Lead", salary=parse_salary("Rp 50jt per bulan")),
            make_job(title="Senior QA Automation Engineer",
                     salary=parse_salary("Rp 35jt per bulan")),
        ]
        ranked = matcher.rank(jobs)
        assert all(not r.rejected for r in ranked)
        assert [r.score for r in ranked] == sorted((r.score for r in ranked), reverse=True)
        assert all("Manual Tester" != r.job.title for r in ranked)
