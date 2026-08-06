"""Scraper registry.

`build_scrapers()` is the only entry point the pipeline needs: it reads
`sources.yaml`, instantiates whatever is enabled (or whatever was named on the
command line) and hands back ready-to-run scrapers.
"""

from __future__ import annotations

import logging

from ..profile import Profile, SourceConfig
from .ats import GreenhouseScraper, LeverScraper
from .base import Scraper, ScraperError
from .extra import (
    AshbyScraper,
    HackerNewsScraper,
    KalibrrScraper,
    SmartRecruitersScraper,
    WorkingNomadsScraper,
)
from .boards import (
    ArbeitnowScraper,
    HimalayasScraper,
    JobicyScraper,
    RemoteOKScraper,
    RemotiveScraper,
    WeWorkRemotelyScraper,
)
from .linkedin import LinkedInScraper
from .local_id import GlintsScraper, JobStreetScraper

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Scraper]] = {
    RemoteOKScraper.name: RemoteOKScraper,
    RemotiveScraper.name: RemotiveScraper,
    JobicyScraper.name: JobicyScraper,
    HimalayasScraper.name: HimalayasScraper,
    ArbeitnowScraper.name: ArbeitnowScraper,
    WeWorkRemotelyScraper.name: WeWorkRemotelyScraper,
    GreenhouseScraper.name: GreenhouseScraper,
    LeverScraper.name: LeverScraper,
    AshbyScraper.name: AshbyScraper,
    SmartRecruitersScraper.name: SmartRecruitersScraper,
    WorkingNomadsScraper.name: WorkingNomadsScraper,
    HackerNewsScraper.name: HackerNewsScraper,
    KalibrrScraper.name: KalibrrScraper,
    JobStreetScraper.name: JobStreetScraper,
    GlintsScraper.name: GlintsScraper,
    LinkedInScraper.name: LinkedInScraper,
}


def build_scrapers(
    profile: Profile,
    sources: SourceConfig,
    only: list[str] | None = None,
) -> list[Scraper]:
    """Instantiate the requested scrapers.

    `only` overrides the `enabled` flag in sources.yaml — asking for a source
    explicitly is taken as intent to run it.
    """
    names = only if only else sources.enabled_sources()
    built: list[Scraper] = []

    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            log.warning("Unknown source '%s' (known: %s)", name, ", ".join(sorted(REGISTRY)))
            continue
        built.append(cls(profile, sources.for_source(name)))

    return built


__all__ = ["REGISTRY", "Scraper", "ScraperError", "build_scrapers"]
