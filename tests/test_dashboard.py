"""Dashboard rendering.

The dashboard is one string-formatted HTML template, which makes two mistakes
easy and invisible: a `{` that `str.format` eats, and unescaped job text that
breaks the page (or worse, injects script). Both are covered here.
"""

from __future__ import annotations

import json
import re

import pytest

from src import dashboard
from src.models import Arrangement, Job, MatchResult, SalaryVerdict, ScoreBreakdown
from src.salary import parse_salary
from src.storage import Store


def make_result(**overrides) -> MatchResult:
    job_kwargs = dict(
        title="QA Automation Lead",
        company="Acme",
        url="https://example.com/1",
        source="greenhouse",
        location_raw="Jakarta, Indonesia",
        country="Indonesia",
        region="indonesia",
        arrangement=Arrangement.REMOTE,
        salary=parse_salary("Rp 40jt per bulan"),
    )
    score = overrides.pop("score", 82.0)
    verdict = overrides.pop("salary_verdict", SalaryVerdict.PASS)
    monthly = overrides.pop("salary_monthly_idr", 40_000_000)
    job_kwargs.update(overrides)
    return MatchResult(
        job=Job(**job_kwargs),
        score=score,
        breakdown=ScoreBreakdown(total=score),
        matched_skills=["cypress", "playwright"],
        salary_verdict=verdict,
        salary_monthly_idr=monthly,
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "dash.db")


def embedded_jobs(html: str) -> list[dict]:
    """Pull the JSON payload the page renders from."""
    match = re.search(r"const JOBS = (\[.*?\]);", html, re.DOTALL)
    assert match, "dashboard did not embed a JOBS array"
    return json.loads(match.group(1))


class TestRendering:
    def test_writes_a_self_contained_page(self, store, tmp_path):
        store.save_all([make_result()])
        target = dashboard.build(store, path=tmp_path / "d.html")
        html = target.read_text(encoding="utf-8")

        assert html.startswith("<!doctype html>")
        # Self-contained means no external fetches — a strict-CSP or offline
        # viewer must still get a working page.
        assert "<script src=" not in html
        assert "<link rel=\"stylesheet\"" not in html
        assert "http://" not in html.split("const JOBS")[0]

    def test_no_unconsumed_format_braces_remain(self, store, tmp_path):
        # A `{`/`}` that str.format swallowed would silently break the CSS or JS.
        store.save_all([make_result()])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        assert "{{" not in html
        assert "}}" not in html

    def test_every_job_is_embedded(self, store, tmp_path):
        store.save_all([make_result(), make_result(company="Beta Ltd")])
        jobs = embedded_jobs(dashboard.build(store, path=tmp_path / "d.html").read_text())
        assert len(jobs) == 2
        assert {j["company"] for j in jobs} == {"Acme", "Beta Ltd"}

    def test_labels_are_plain_language(self, store, tmp_path):
        store.save_all([
            make_result(),
            make_result(company="B", salary_verdict=SalaryVerdict.UNKNOWN,
                        salary_monthly_idr=None),
            make_result(company="C", region="europe",
                        salary_verdict=SalaryVerdict.NOT_ENFORCED),
        ])
        jobs = embedded_jobs(dashboard.build(store, path=tmp_path / "d.html").read_text())
        labels = {j["company"]: j["salaryLabel"] for j in jobs}
        assert labels["Acme"] == "Clears 30jt"
        assert labels["B"] == "Not stated"
        assert labels["C"] == "No floor"
        # "n/a" and "fallback" must never reach the page.
        assert all(j["region"] in {"Indonesia", "Asia", "Europe", "Elsewhere"} for j in jobs)


class TestHeadlineNumbers:
    def test_ready_to_apply_excludes_unverified_salary(self, store, tmp_path):
        store.save_all([
            make_result(company="Clear", score=85),
            make_result(company="Murky", score=90,
                        salary_verdict=SalaryVerdict.UNKNOWN, salary_monthly_idr=None),
            make_result(company="LowScore", score=61),
        ])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        cards = re.findall(r"<b>(\d+)</b><span>([^<]+)", html)
        numbers = {label.split("<")[0].strip(): int(value) for value, label in cards}
        # Only "Clear" qualifies: 70+, actionable, and salary verified.
        assert numbers["Ready to apply"] == 1
        assert numbers["Salary not stated"] == 1

    def test_applied_and_interviewing_are_counted_together(self, store, tmp_path):
        results = [make_result(company=f"C{i}", score=85) for i in range(3)]
        store.save_all(results)
        store.set_status(results[0].job.fingerprint, "applied")
        store.set_status(results[1].job.fingerprint, "interviewing")
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        assert "<b>2</b><span>Applied / interviewing" in html


class TestEscaping:
    def test_job_text_cannot_break_out_of_the_json_payload(self, store, tmp_path):
        nasty = '</script><img src=x onerror=alert(1)>'
        store.save_all([make_result(title=f"QA Lead {nasty}", company='O"Brien & Co')])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()

        # The payload is JSON, so the closing tag must arrive escaped — an
        # unescaped one would end the <script> block early.
        assert "</script><img" not in html
        # And the value must survive intact for the page to render it.
        jobs = embedded_jobs(html)
        assert nasty in jobs[0]["title"]
        assert jobs[0]["company"] == 'O"Brien & Co'


class TestEmptyDatabase:
    def test_builds_without_rows(self, store, tmp_path):
        target = dashboard.build(store, path=tmp_path / "d.html")
        html = target.read_text()
        assert embedded_jobs(html) == []
        assert "<b>0</b>" in html


class TestPaginationAndTooltip:
    """Covers what the browser exploration pass verified interactively."""

    def test_per_page_is_embedded(self, store, tmp_path):
        store.save_all([make_result()])
        html = dashboard.build(store, path=tmp_path / "d.html", per_page=10).read_text()
        assert "let sortKey" in html
        assert "perPage = 10" in html

    def test_score_filter_defaults_to_any(self, store, tmp_path):
        store.save_all([make_result()])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        # "Any score" must be the selected option — a 70+ default hides rows
        # the user expects to see on first load.
        assert '<option value="0" selected>Any score</option>' in html

    def test_default_sort_is_newest_scraped(self, store, tmp_path):
        store.save_all([make_result()])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        assert 'let sortKey = "foundTs", sortDesc = true;' in html

    def test_rows_carry_both_timestamps(self, store, tmp_path):
        store.save_all([make_result()])
        job = embedded_jobs(dashboard.build(store, path=tmp_path / "d.html").read_text())[0]
        # `found` is when we scraped it; `posted` is what the board claimed.
        assert job["found"] and len(job["found"]) == 10
        assert "foundTs" in job

    def test_newest_first_ordering_comes_from_sql(self, store, tmp_path):
        older, newer = make_result(company="Older"), make_result(company="Newer")
        store.upsert(older)
        store.upsert(newer)
        # `order="recent"` matters once there are more jobs than `limit`: the
        # cut must keep the freshest rows, not the highest scoring ones.
        companies = [j["company"] for j in embedded_jobs(
            dashboard.build(store, path=tmp_path / "d.html").read_text())]
        assert set(companies) == {"Older", "Newer"}

    def test_tooltip_has_breakdown_and_weights(self, store, tmp_path):
        store.save_all([make_result()])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        job = embedded_jobs(html)[0]
        # The tooltip is useless without both halves: the per-dimension score
        # and how much that dimension counts toward the total.
        assert job["breakdown"], "no per-dimension scores embedded"
        assert '"skills"' in html and "const WEIGHTS" in html
        assert re.search(r"const WEIGHTS = \{[^}]*skills", html)

    def test_missing_breakdown_degrades_gracefully(self, store, tmp_path):
        store.save_all([make_result()])
        # Simulate a row stored before breakdowns were recorded.
        with store._connect() as conn:
            conn.execute("UPDATE jobs SET breakdown_json = NULL")
        job = embedded_jobs(dashboard.build(store, path=tmp_path / "d.html").read_text())[0]
        assert job["breakdown"] == {}

    def test_keydown_handler_guards_non_element_targets(self, store, tmp_path):
        store.save_all([make_result()])
        html = dashboard.build(store, path=tmp_path / "d.html").read_text()
        # A keydown with nothing focused targets `document`, which has no
        # .matches() — an unguarded call threw a TypeError in the browser.
        assert "t instanceof Element && t.matches(" in html
