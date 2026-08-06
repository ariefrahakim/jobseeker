"""Orchestration: scrape -> dedupe -> score -> enrich -> persist -> report.

Scrapers run concurrently because they are all I/O-bound against different
hosts, and one slow board should not set the pace for the run. Each one is
isolated: a source that raises is recorded in `errors` and the run continues.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .llm import assess_fit
from .matcher import Matcher
from .models import Job, MatchResult
from .profile import Profile, SourceConfig
from .scrapers import build_scrapers
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """Everything a caller needs to report on a completed run."""

    started_at: datetime
    finished_at: datetime | None = None
    sources_run: list[str] = field(default_factory=list)
    scraped: int = 0
    after_dedupe: int = 0
    matched: int = 0
    new: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    results: list[MatchResult] = field(default_factory=list)
    new_results: list[MatchResult] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse the same posting seen on multiple boards.

    When two sources carry the same job, keep whichever record has more
    description text — that is the one the matcher can actually score, and the
    thin one is usually a syndicated stub.
    """
    best: dict[str, Job] = {}
    for job in jobs:
        key = job.fingerprint
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = job
            continue

        # Prefer richer description; break ties on a stated salary, then recency.
        challenger_rank = (
            len(job.description),
            int(job.salary.stated),
            job.posted_at.timestamp() if job.posted_at else 0,
        )
        incumbent_rank = (
            len(incumbent.description),
            int(incumbent.salary.stated),
            incumbent.posted_at.timestamp() if incumbent.posted_at else 0,
        )
        if challenger_rank > incumbent_rank:
            # Keep a breadcrumb so the report can show all boards carrying it.
            job.extra.setdefault("also_on", []).append(incumbent.source)
            best[key] = job
        else:
            incumbent.extra.setdefault("also_on", []).append(job.source)

    return list(best.values())


class Pipeline:
    def __init__(
        self,
        profile: Profile | None = None,
        sources: SourceConfig | None = None,
        store: Store | None = None,
    ):
        self.profile = profile or Profile.load()
        self.sources = sources or SourceConfig.load()
        self.store = store or Store()
        self.matcher = Matcher(self.profile)

    def run(
        self,
        only_sources: list[str] | None = None,
        use_llm: bool = False,
        llm_max_jobs: int = 20,
        max_workers: int = 6,
        min_score: float | None = None,
    ) -> RunSummary:
        summary = RunSummary(started_at=datetime.now(timezone.utc))
        scrapers = build_scrapers(self.profile, self.sources, only_sources)

        if not scrapers:
            summary.errors.append(
                "No sources to run. Enable some in config/sources.yaml or pass --sources."
            )
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        summary.sources_run = [s.name for s in scrapers]
        run_id = self.store.start_run(summary.sources_run)
        log.info("Scraping %d source(s): %s", len(scrapers), ", ".join(summary.sources_run))

        # --- scrape -----------------------------------------------------------
        collected: list[Job] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(scraper.fetch): scraper.name for scraper in scrapers}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    jobs = future.result()
                except Exception as exc:
                    msg = f"{name}: {type(exc).__name__}: {exc}"
                    log.warning("Source failed — %s", msg)
                    summary.errors.append(msg)
                    summary.per_source[name] = 0
                    continue

                summary.per_source[name] = len(jobs)
                collected.extend(jobs)
                log.info("  %-16s %3d posting(s)", name, len(jobs))

        summary.scraped = len(collected)

        # --- dedupe + score ---------------------------------------------------
        unique = dedupe(collected)
        summary.after_dedupe = len(unique)

        if min_score is not None:
            # A per-run override, e.g. a wider sweep on a quiet week.
            self.profile.scoring["min_score"] = min_score

        results = self.matcher.rank(unique)
        summary.matched = len(results)
        log.info(
            "Scored %d unique posting(s); %d cleared the %.0f-point threshold",
            len(unique), len(results), self.profile.min_score,
        )

        # --- enrich (optional) ------------------------------------------------
        if use_llm and results:
            log.info("Asking Claude to assess the top %d match(es)", min(llm_max_jobs, len(results)))
            results = assess_fit(results, self.profile, max_jobs=llm_max_jobs)

        # --- persist ----------------------------------------------------------
        summary.new_results = self.store.save_all(results)
        summary.new = len(summary.new_results)
        summary.results = results
        summary.finished_at = datetime.now(timezone.utc)

        self.store.finish_run(
            run_id,
            scraped=summary.scraped,
            matched=summary.matched,
            new=summary.new,
            errors=summary.errors,
        )
        log.info(
            "Run finished in %.1fs — %d new, %d total match(es)",
            summary.duration_seconds, summary.new, summary.matched,
        )
        return summary
