"""Dedupe, location normalisation, storage and the scraper base helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.matcher import Matcher
from src.models import Arrangement, Job, MatchResult, ScoreBreakdown
from src.pipeline import dedupe
from src.profile import Profile, detect_arrangement, normalise_country
from src.salary import parse_salary
from src.scrapers.base import Scraper
from src.storage import Store


def make_job(**overrides) -> Job:
    defaults = dict(
        title="QA Automation Lead",
        company="Acme Corp",
        url="https://example.com/1",
        source="boardA",
        description="Cypress and Playwright automation.",
        location_raw="Jakarta, Indonesia",
        country="Indonesia",
        arrangement=Arrangement.REMOTE,
        posted_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestCountryNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Jakarta, Indonesia", "Indonesia"),
            ("Jakarta Selatan", "Indonesia"),
            ("Remote - Singapore", "Singapore"),
            ("Berlin, Germany", "Germany"),
            ("Amsterdam, NL", "Netherlands"),
            ("London, UK", "United Kingdom"),
            ("Kraków, Poland", "Poland"),
            ("Remote (Europe)", None),
            ("", None),
            (None, None),
        ],
    )
    def test_messy_locations_resolve(self, raw, expected):
        assert normalise_country(raw) == expected

    def test_south_korea_beats_bare_korea(self):
        assert normalise_country("Seoul, South Korea") == "South Korea"

    def test_substring_does_not_falsely_match(self):
        # "us" inside "Australia" must not resolve to the United States.
        assert normalise_country("Sydney, Australia") == "Australia"


class TestArrangementDetection:
    @pytest.mark.parametrize(
        "texts,expected",
        [
            (("QA Lead", "Remote", ""), Arrangement.REMOTE),
            (("QA Lead", "Jakarta", "Hybrid, 3 days in office"), Arrangement.HYBRID),
            (("QA Lead", "Jakarta", "This is an on-site role"), Arrangement.ONSITE),
            (("QA Lead", "Jakarta", "Great team"), Arrangement.UNKNOWN),
        ],
    )
    def test_detection(self, texts, expected):
        assert detect_arrangement(*texts) is expected

    def test_hybrid_wins_over_remote(self):
        # Hybrid postings almost always also say "remote"; hybrid is the truth.
        assert detect_arrangement("Hybrid remote role in Jakarta") is Arrangement.HYBRID


class TestFingerprint:
    def test_same_role_on_two_boards_collides(self):
        a = make_job(source="remoteok", url="https://a.com/1")
        b = make_job(source="jobicy", url="https://b.com/9", title="QA  Automation  Lead")
        assert a.fingerprint == b.fingerprint

    def test_different_company_does_not_collide(self):
        assert make_job().fingerprint != make_job(company="Other Inc").fingerprint

    def test_same_title_different_country_does_not_collide(self):
        assert (
            make_job(country="Indonesia").fingerprint
            != make_job(country="Germany").fingerprint
        )


class TestDedupe:
    def test_richest_description_wins(self):
        thin = make_job(source="stub", description="QA role.")
        rich = make_job(source="greenhouse", description="Cypress. " * 200)
        result = dedupe([thin, rich])
        assert len(result) == 1
        assert result[0].source == "greenhouse"

    def test_losing_source_is_recorded(self):
        thin = make_job(source="stub", description="short")
        rich = make_job(source="lever", description="long " * 100)
        kept = dedupe([thin, rich])[0]
        assert "stub" in kept.extra.get("also_on", [])

    def test_stated_salary_breaks_a_description_tie(self):
        without = make_job(source="a", description="same length text")
        with_salary = make_job(
            source="b", description="same length text", salary=parse_salary("Rp 40jt per bulan")
        )
        assert dedupe([without, with_salary])[0].source == "b"

    def test_distinct_jobs_are_untouched(self):
        assert len(dedupe([make_job(), make_job(company="Beta Ltd")])) == 2


class TestScraperHelpers:
    class Dummy(Scraper):
        name = "dummy"

        def fetch(self):
            return []

    @pytest.fixture
    def scraper(self):
        return self.Dummy(Profile.load(), {"delay_seconds": 0})

    def test_html_is_stripped(self, scraper):
        cleaned = scraper.strip_html("<p>Use <b>Cypress</b>&nbsp;&amp; Playwright</p>")
        assert cleaned == "Use Cypress & Playwright"

    @pytest.mark.parametrize(
        "value",
        [
            "2026-01-15T10:30:00Z",
            "2026-01-15",
            "Wed, 15 Jan 2026 10:30:00 +0000",
            1768473000,
            1768473000000,
        ],
    )
    def test_date_formats_parse(self, scraper, value):
        parsed = scraper.parse_date(value)
        assert parsed is not None and parsed.tzinfo is not None

    def test_relative_dates_parse(self, scraper):
        parsed = scraper.parse_date("posted 3 days ago")
        assert parsed is not None
        age = (datetime.now(timezone.utc) - parsed).days
        assert age == 3

    def test_unparseable_date_returns_none(self, scraper):
        assert scraper.parse_date("sometime last quarter") is None

    def test_build_job_infers_country_and_arrangement(self, scraper):
        job = scraper.build_job(
            title="SDET",
            company="Acme",
            url="https://x.com/1",
            description="<p>Fully remote. Cypress required. Salary Rp 40jt per bulan.</p>",
            location_raw="Jakarta",
        )
        assert job.country == "Indonesia"
        assert job.arrangement is Arrangement.REMOTE
        # Salary was not passed in — it was recovered from the description.
        assert job.salary.min_amount == pytest.approx(40_000_000)


class TestStore:
    @pytest.fixture
    def store(self, tmp_path):
        return Store(tmp_path / "test.db")

    def _result(self, **overrides) -> MatchResult:
        return MatchResult(
            job=make_job(**overrides),
            score=82.5,
            breakdown=ScoreBreakdown(total=82.5),
            matched_skills=["cypress", "playwright"],
        )

    def test_first_insert_is_new_second_is_not(self, store):
        result = self._result()
        assert store.upsert(result) is True
        assert store.upsert(result) is False

    def test_reseen_job_moves_from_new_to_seen(self, store):
        result = self._result()
        store.upsert(result)
        store.upsert(result)
        rows = store.list_jobs()
        assert rows[0]["status"] == "seen"
        assert rows[0]["seen_count"] == 2

    def test_manual_status_survives_a_rerun(self, store):
        result = self._result()
        store.upsert(result)
        store.set_status(result.job.fingerprint, "applied", notes="sent 2026-01-15")
        store.upsert(result)
        row = store.list_jobs()[0]
        assert row["status"] == "applied"
        assert row["notes"] == "sent 2026-01-15"

    def test_save_all_returns_only_the_new_ones(self, store):
        first, second = self._result(), self._result(company="Beta Ltd")
        assert len(store.save_all([first, second])) == 2
        third = self._result(company="Gamma Ltd")
        assert len(store.save_all([first, second, third])) == 1

    def test_filters_and_stats(self, store):
        store.save_all([self._result(), self._result(company="Beta Ltd")])
        assert len(store.list_jobs(status="new")) == 2
        assert len(store.list_jobs(min_score=90)) == 0
        assert len(store.list_jobs(region="indonesia")) == 0  # region unset on raw Job
        stats = store.stats()
        assert stats["total"] == 2
        assert stats["by_status"]["new"] == 2

    def test_unknown_status_is_rejected(self, store):
        result = self._result()
        store.upsert(result)
        with pytest.raises(ValueError):
            store.set_status(result.job.fingerprint, "maybe_later")

    def test_run_ledger_round_trip(self, store):
        run_id = store.start_run(["remoteok", "lever"])
        store.finish_run(run_id, scraped=40, matched=12, new=5, errors=["glints: 403"])
        assert run_id > 0


class TestRegionAssignmentDuringScoring:
    def test_matcher_fills_in_the_region(self):
        matcher = Matcher(Profile.load())
        job = make_job(country="Germany", location_raw="Berlin, Germany (remote)")
        result = matcher.evaluate(job)
        assert result.job.region == "europe"
