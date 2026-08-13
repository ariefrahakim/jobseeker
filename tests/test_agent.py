"""The agentic loop.

The most important assertions here are the negative ones: the agent must never
mark a job `applied`, and must never claim a draft exists when Claude was
unavailable. Both would be invisible in normal use and both would cost the user
something real — a double application, or a queue of empty promises.
"""

from __future__ import annotations

import pytest

from src import agent as agent_module
from src.agent import ApplyAgent
from src.models import Arrangement, Job, MatchResult, SalaryVerdict, ScoreBreakdown
from src.profile import Profile
from src.salary import parse_salary
from src.storage import Store

LONG_DESC = (
    "We are looking for a QA automation lead to own our Cypress and Playwright "
    "framework, mentor a team of engineers, build API automation with RestAssured, "
    "and run performance tests with k6 in Azure DevOps pipelines. " * 3
)


def make_result(**overrides) -> MatchResult:
    score = overrides.pop("score", 82.0)
    verdict = overrides.pop("salary_verdict", SalaryVerdict.PASS)
    monthly = overrides.pop("salary_monthly_idr", 40_000_000)
    llm_fit = overrides.pop("llm_fit", None)

    job_kwargs = dict(
        title="QA Automation Lead",
        company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/1",
        source="greenhouse",
        description=LONG_DESC,
        location_raw="Jakarta, Indonesia",
        country="Indonesia",
        region="indonesia",
        arrangement=Arrangement.REMOTE,
        salary=parse_salary("Rp 45jt per bulan"),
    )
    job_kwargs.update(overrides)
    return MatchResult(
        job=Job(**job_kwargs),
        score=score,
        breakdown=ScoreBreakdown(total=score),
        matched_skills=["cypress", "playwright", "restassured"],
        salary_verdict=verdict,
        salary_monthly_idr=monthly,
        llm_fit=llm_fit,
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "agent.db")


@pytest.fixture
def profile() -> Profile:
    return Profile.load()


@pytest.fixture
def stub_claude(monkeypatch, tmp_path):
    """Make drafting succeed without an API key."""
    monkeypatch.setattr(agent_module, "_claude_available", lambda: True)
    monkeypatch.setattr(
        agent_module,
        "write_application_kit",
        lambda result, profile: {
            "cv_bullets": "- Led the Cypress framework at Hubexo.",
            "cover_letter": "I lead QA automation at Hubexo.",
        },
    )
    monkeypatch.setattr(
        agent_module, "assess_fit", lambda results, p, **kw: results
    )


@pytest.fixture
def no_claude(monkeypatch):
    monkeypatch.setattr(agent_module, "_claude_available", lambda: False)


def build_agent(store, profile, tmp_path) -> ApplyAgent:
    return ApplyAgent(store=store, profile=profile, draft_dir=tmp_path / "drafts")


class TestSafetyBoundary:
    """The agent prepares; a human sends. These tests hold that line."""

    def test_agent_can_never_set_applied(self, store):
        result = make_result()
        store.upsert(result)
        with pytest.raises(ValueError, match="may not set 'applied'"):
            store.record_decision(result.job.fingerprint, "applied")

    def test_agent_can_never_set_interviewing(self, store):
        result = make_result()
        store.upsert(result)
        with pytest.raises(ValueError):
            store.record_decision(result.job.fingerprint, "interviewing")

    def test_a_full_run_leaves_nothing_applied(self, store, profile, tmp_path, stub_claude):
        store.save_all([make_result(company=f"C{i}") for i in range(4)])
        build_agent(store, profile, tmp_path).run(min_score=70, limit=4)
        statuses = {row["status"] for row in store.list_jobs(limit=50)}
        assert "applied" not in statuses
        assert statuses <= {"prepared", "shortlisted", "skipped", "new", "seen"}

    def test_already_applied_jobs_are_left_alone(self, store, profile, tmp_path, stub_claude):
        result = make_result()
        store.upsert(result)
        store.set_status(result.job.fingerprint, "applied")

        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=5)
        # Re-preparing an applied job is how you accidentally apply twice.
        assert report.considered == 0
        assert store.list_jobs(limit=5)[0]["status"] == "applied"

    @pytest.mark.parametrize("status", ["skipped", "rejected", "prepared"])
    def test_decided_jobs_are_not_reconsidered(self, store, profile, tmp_path,
                                               stub_claude, status):
        result = make_result()
        store.upsert(result)
        store.set_status(result.job.fingerprint, status)
        assert build_agent(store, profile, tmp_path).run(min_score=70).considered == 0


class TestDecisions:
    def test_strong_llm_verdict_gets_prepared(self, store, profile, tmp_path, stub_claude):
        store.save_all([make_result(llm_fit="[strong] Great match on your stack.")])
        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=1, use_llm=False)
        assert len(report.prepared) == 1
        assert report.prepared[0].draft_path.exists()

    def test_poor_fit_is_skipped_despite_a_high_score(self, store, profile, tmp_path,
                                                      stub_claude):
        store.save_all([
            make_result(score=95, llm_fit="[poor_fit] The body describes manual testing.")
        ])
        report = build_agent(store, profile, tmp_path).run(min_score=70, use_llm=False)
        # Claude's read must outrank the keyword score — that is the whole point
        # of asking it.
        assert len(report.skipped) == 1
        assert report.prepared == []

    def test_stretch_needs_a_human_look(self, store, profile, tmp_path, stub_claude):
        store.save_all([make_result(llm_fit="[stretch] Adjacent, not central.")])
        report = build_agent(store, profile, tmp_path).run(min_score=70, use_llm=False)
        assert len(report.shortlisted) == 1

    def test_unstated_indonesian_salary_is_not_auto_drafted(self, store, profile,
                                                            tmp_path, stub_claude):
        store.save_all([
            make_result(salary_verdict=SalaryVerdict.UNKNOWN, salary_monthly_idr=None)
        ])
        report = build_agent(store, profile, tmp_path).run(min_score=70, use_llm=False)
        # Drafting for a role that cannot be checked against the 30jt floor
        # invites applying below your own bar.
        assert len(report.shortlisted) == 1
        assert "IDR 30jt" in report.shortlisted[0].reason

    def test_unstated_salary_can_be_allowed_explicitly(self, store, profile,
                                                        tmp_path, stub_claude):
        store.save_all([
            make_result(salary_verdict=SalaryVerdict.UNKNOWN, salary_monthly_idr=None)
        ])
        report = build_agent(store, profile, tmp_path).run(
            min_score=70, require_salary_clear=False, use_llm=False
        )
        assert len(report.prepared) == 1

    def test_thin_posting_is_not_drafted_from(self, store, profile, tmp_path, stub_claude):
        store.save_all([make_result(description="QA role. Apply within.")])
        report = build_agent(store, profile, tmp_path).run(min_score=70, use_llm=False)
        # A generic cover letter is worse than no cover letter.
        assert len(report.shortlisted) == 1
        assert "too thin" in report.shortlisted[0].reason


class TestLimitAndOrdering:
    def test_limit_prepares_the_best_not_the_newest(self, store, profile, tmp_path,
                                                    stub_claude):
        # Insert ascending so recency order and score order disagree.
        for score in (72.0, 80.0, 91.0):
            store.upsert(make_result(company=f"Co{score:.0f}", score=score))

        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=1, use_llm=False)
        assert len(report.prepared) == 1
        assert report.prepared[0].score == 91.0, "limit must take the best, not the newest"

    def test_min_score_excludes_lower_ranked_jobs(self, store, profile, tmp_path,
                                                  stub_claude):
        store.save_all([make_result(score=60.0), make_result(company="B", score=90.0)])
        report = build_agent(store, profile, tmp_path).run(min_score=80, use_llm=False)
        assert report.considered == 1


class TestWithoutClaude:
    def test_no_draft_is_claimed_when_claude_is_unavailable(self, store, profile,
                                                            tmp_path, no_claude):
        store.save_all([make_result()])
        report = build_agent(store, profile, tmp_path).run(min_score=70, use_llm=False)

        # It must not report `prepared` with no draft behind it.
        assert report.prepared == []
        assert len(report.shortlisted) == 1
        assert "Claude unavailable" in report.shortlisted[0].reason

    def test_the_missing_key_is_reported_once_not_per_job(self, store, profile,
                                                          tmp_path, no_claude):
        store.save_all([make_result(company=f"C{i}") for i in range(6)])
        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=6,
                                                          use_llm=False)
        credential_errors = [e for e in report.errors if "ANTHROPIC_API_KEY" in e]
        assert len(credential_errors) == 1, report.errors

    def test_repeated_draft_failures_stop_the_run(self, store, profile, tmp_path,
                                                  monkeypatch):
        monkeypatch.setattr(agent_module, "_claude_available", lambda: True)
        monkeypatch.setattr(agent_module, "assess_fit", lambda r, p, **kw: r)
        monkeypatch.setattr(
            agent_module, "write_application_kit",
            lambda result, profile: None,   # simulates Claude refusing every time
        )
        store.save_all([make_result(company=f"C{i}") for i in range(6)])
        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=6,
                                                          use_llm=False)
        # Two failures prove the cause is the setup, not the job; grinding on
        # just produces six copies of the same error.
        assert len(report.prepared) == 0
        assert any("Stopping early" in e for e in report.errors)
        assert len(report.decisions) == 2


class TestDryRun:
    def test_dry_run_changes_nothing(self, store, profile, tmp_path, stub_claude):
        store.save_all([make_result()])
        agent = build_agent(store, profile, tmp_path)
        report = agent.run(min_score=70, limit=1, dry_run=True)

        assert len(report.prepared) == 1          # the decision is still reported
        assert report.prepared[0].draft_path is None
        assert store.list_jobs(limit=1)[0]["status"] == "new"
        assert not (tmp_path / "drafts").exists()


class TestDraftContents:
    def test_draft_carries_everything_needed_to_send_it(self, store, profile,
                                                        tmp_path, stub_claude):
        store.save_all([make_result(llm_fit="[strong] Good match.")])
        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=1,
                                                          use_llm=False)
        body = report.prepared[0].draft_path.read_text(encoding="utf-8")

        assert "https://boards.greenhouse.io/acme/jobs/1" in body   # where to apply
        assert "## CV bullets" in body and "## Cover letter" in body
        assert report.prepared[0].fingerprint in body               # how to mark it sent
        # A draft must say out loud that it is a draft.
        assert "Read it before sending" in body

    def test_warnings_are_carried_into_the_draft(self, store, profile, tmp_path,
                                                 stub_claude, monkeypatch):
        result = make_result()
        result.warnings = ["Salary not stated; confirm before applying."]
        store.upsert(result)
        # Feed the warning back through the row payload the agent reads.
        monkeypatch.setattr(
            agent_module, "_row_to_result",
            lambda row: result,
        )
        report = build_agent(store, profile, tmp_path).run(min_score=70, limit=1,
                                                          require_salary_clear=False,
                                                          use_llm=False)
        body = report.prepared[0].draft_path.read_text()
        assert "Check before sending" in body
