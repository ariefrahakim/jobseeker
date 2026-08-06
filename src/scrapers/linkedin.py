"""LinkedIn job search via Playwright.

Opt-in and deliberately last in the pipeline. LinkedIn is the richest source
for QA leadership roles in Indonesia and Europe, but it is also the most hostile
to automation: it rate-limits aggressively, changes its DOM often, and will
throw a login wall or checkpoint at you without warning.

Design choices that follow from that:
  * Guest search first. The public `/jobs-guest/jobs/api/seeMoreJobPostings`
    endpoint returns rendered job cards with no session at all. It gives less
    per card than the logged-in view, but it never risks the account.
  * Login only if credentials exist AND guest search came back empty.
  * Everything is wrapped. A checkpoint or DOM change degrades this source to
    "returned nothing", never to a failed run.

Install the browser once: `playwright install chromium`.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from .. import credentials
from ..models import Job
from .base import Scraper

log = logging.getLogger(__name__)

GUEST_SEARCH = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)


class LinkedInScraper(Scraper):
    name = "linkedin"

    def fetch(self) -> list[Job]:
        queries = self.config.get("queries") or self.profile.search_queries()
        geos: dict[str, str] = self.config.get("geos") or {}

        jobs = self._guest_search(queries, geos)
        if jobs:
            return jobs

        if credentials.available("linkedin"):
            log.info("linkedin: guest search empty, trying authenticated browser")
            return self._browser_search(queries, geos)

        log.info(
            "linkedin: guest search returned nothing, and no complete credentials "
            "found (set LINKEDIN_EMAIL + LINKEDIN_PASSWORD, or the EMAIL_LINKEDIN "
            "+ PASSWORD_LINKEDIN form, in .env)"
        )
        return []

    # --------------------------------------------------------------- guest API
    def _guest_search(self, queries: list[str], geos: dict[str, str]) -> list[Job]:
        """Hit the public job-card endpoint. No session, no browser."""
        jobs: list[Job] = []
        seen: set[str] = set()
        limit = int(self.config.get("max_jobs_per_query", 25))

        for query in queries:
            for region, geo_id in (geos or {"anywhere": ""}).items():
                for start in range(0, limit, 25):
                    params = {
                        "keywords": query,
                        "start": start,
                        "f_TPR": "r604800",   # last 7 days
                        "sortBy": "DD",       # most recent
                    }
                    if geo_id:
                        params["geoId"] = geo_id

                    try:
                        html = self.get(GUEST_SEARCH, params=params).text
                    except Exception as exc:
                        log.warning("linkedin guest search failed (%s/%s): %s",
                                    query, region, exc)
                        break

                    cards = self._parse_cards(html, region)
                    if not cards:
                        break
                    for job in cards:
                        if job.url in seen:
                            continue
                        seen.add(job.url)
                        jobs.append(job)
        return jobs

    def _parse_cards(self, html: str, region: str) -> list[Job]:
        """Extract job cards from the guest endpoint's HTML fragment.

        Regex rather than a DOM parser on purpose: the fragment is a flat list
        of `<li>` blocks with stable class hooks, and this avoids a bs4/lxml
        dependency for one endpoint.
        """
        jobs: list[Job] = []
        blocks = re.split(r"<li>\s*<div class=\"base-card", html)
        for block in blocks[1:]:
            url_match = re.search(r'href="(https://[^"?]*?/jobs/view/[^"?]+)', block)
            title_match = re.search(
                r'class="base-search-card__title"[^>]*>(.*?)</h3>', block, re.DOTALL
            )
            company_match = re.search(
                r'class="hidden-nested-link"[^>]*>(.*?)</a>', block, re.DOTALL
            )
            location_match = re.search(
                r'class="job-search-card__location"[^>]*>(.*?)</span>', block, re.DOTALL
            )
            date_match = re.search(r'datetime="([\d-]+)"', block)

            if not (url_match and title_match):
                continue

            jobs.append(
                self.build_job(
                    title=self.strip_html(title_match.group(1)),
                    company=self.strip_html(company_match.group(1)) if company_match else "Unknown",
                    url=url_match.group(1),
                    description="",  # guest cards carry no body; enrich later
                    location_raw=self.strip_html(location_match.group(1)) if location_match else region,
                    posted_at=date_match.group(1) if date_match else None,
                    extra={"search_region": region, "detail_fetched": False},
                )
            )
        return jobs

    # ------------------------------------------------------------- browser API
    def _browser_search(self, queries: list[str], geos: dict[str, str]) -> list[Job]:
        """Authenticated search. Only reached when guest search yields nothing."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("linkedin: playwright not installed; skipping browser path")
            return []

        jobs: list[Job] = []
        headless = bool(self.config.get("headless", True))
        limit = int(self.config.get("max_jobs_per_query", 25))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=self.config.get("user_agent"),
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            try:
                if not self._login(page):
                    return []

                for query in queries:
                    for region, geo_id in (geos or {"anywhere": ""}).items():
                        url = (
                            "https://www.linkedin.com/jobs/search/?"
                            + urllib.parse.urlencode(
                                {
                                    "keywords": query,
                                    "geoId": geo_id,
                                    "f_TPR": "r604800",
                                    "sortBy": "DD",
                                    **({"f_WT": "2"} if self.profile.work_preference.get("remote_first") else {}),
                                }
                            )
                        )
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                            page.wait_for_selector(".jobs-search__results-list, .scaffold-layout__list",
                                                   timeout=20_000)
                        except Exception as exc:
                            log.warning("linkedin: search page failed (%s): %s", query, exc)
                            continue

                        jobs += self._harvest_page(page, region, limit)
            finally:
                context.close()
                browser.close()

        return jobs

    def _login(self, page: Any) -> bool:
        creds = credentials.for_service("linkedin")
        if not creds.complete:
            return False
        try:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            page.fill("#username", creds.email or "")
            page.fill("#password", creds.password or "")
            page.click("button[type=submit]")
            page.wait_for_load_state("domcontentloaded")

            if "checkpoint" in page.url or "challenge" in page.url:
                log.warning(
                    "linkedin: hit a security checkpoint. Re-run with "
                    "headless: false in sources.yaml and clear it by hand."
                )
                return False
            return "feed" in page.url or "linkedin.com" in page.url
        except Exception as exc:
            log.warning("linkedin login failed: %s", exc)
            return False

    def _harvest_page(self, page: Any, region: str, limit: int) -> list[Job]:
        jobs: list[Job] = []
        try:
            cards = page.query_selector_all(
                ".jobs-search__results-list li, .scaffold-layout__list li.jobs-search-results__list-item"
            )[:limit]
        except Exception:
            return jobs

        for card in cards:
            try:
                title_el = card.query_selector("a.job-card-list__title, .base-search-card__title")
                company_el = card.query_selector(
                    ".job-card-container__primary-description, .base-search-card__subtitle"
                )
                location_el = card.query_selector(
                    ".job-card-container__metadata-item, .job-search-card__location"
                )
                link_el = card.query_selector("a[href*='/jobs/view/']")
                if not (title_el and link_el):
                    continue

                href = link_el.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://www.linkedin.com{href}"

                jobs.append(
                    self.build_job(
                        title=title_el.inner_text().strip(),
                        company=company_el.inner_text().strip() if company_el else "Unknown",
                        url=href.split("?")[0],
                        location_raw=location_el.inner_text().strip() if location_el else region,
                        extra={"search_region": region},
                    )
                )
            except Exception as exc:  # one bad card is not worth aborting for
                log.debug("linkedin: skipped a card: %s", exc)
        return jobs
