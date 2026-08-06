"""Indonesia-local boards: JobStreet ID and Glints.

These matter because the IDR 30jt floor only bites on Indonesian roles, and
Indonesian roles largely do not appear on the remote-first boards.

Both are best-effort by design — any failure is logged and the run continues —
because neither site offers a documented public API:

  * JobStreet backs its SPA with a JSON search endpoint. When that endpoint
    answers 403 (it does, under Cloudflare), a signed-in browser is the fallback.
  * Glints has no working unauthenticated JSON endpoint left, so it is
    browser-only. Its cards do publish a monthly IDR range, which is the single
    most useful field for the salary floor.

Be a good citizen. The polite delay from `sources.yaml` applies to every request,
and page counts should stay low.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from .. import credentials
from ..models import Job
from .base import Scraper

log = logging.getLogger(__name__)


class JobStreetScraper(Scraper):
    """JobStreet Indonesia, via the search endpoint its own SPA calls.

    Search itself needs no account. Credentials (`JOBSTREET_EMAIL` +
    `JOBSTREET_PASSWORD`, or the `EMAIL_JOBSTREET` + `PASSWORD_JOBSTREET` form)
    are only used for a signed-in browser fallback, which JobStreet occasionally
    forces when it decides the anonymous endpoint has seen enough traffic.
    """

    name = "jobstreet"
    SEARCH = "https://id.jobstreet.com/api/chalice-search/v4/search"

    def fetch(self) -> list[Job]:
        jobs = self._search_api()
        if jobs:
            return jobs

        creds = credentials.for_service("jobstreet")
        if creds.complete:
            log.info("jobstreet: search API empty, trying signed-in browser")
            return self._browser_search()

        if creds.partial:
            log.info(
                "jobstreet: search API returned nothing. A password is needed for the "
                "browser fallback — set JOBSTREET_PASSWORD (or PASSWORD_JOBSTREET)."
            )
        return []

    def _search_api(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()

        for query in self.config.get("queries", ["qa-automation"]):
            keywords = query.replace("-", " ")
            params = {
                "siteKey": "ID-Main",
                "sourcesystem": "houston",
                "keywords": keywords,
                "pageSize": 30,
                "page": 1,
                "sortmode": "ListedDate",
                "locale": "id-ID",
            }
            try:
                payload = self.get_json(self.SEARCH, params=params)
            except Exception as exc:
                log.warning("jobstreet query '%s' failed: %s", query, exc)
                continue

            for entry in (payload or {}).get("data", []):
                job_id = str(entry.get("id", ""))
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)

                advertiser = entry.get("advertiser") or {}
                location = ", ".join(
                    filter(
                        None,
                        [
                            entry.get("location"),
                            entry.get("area"),
                            "Indonesia",
                        ],
                    )
                )
                # The search payload is a summary; the teaser plus bullet points
                # is all the text available without a second request per job.
                description = " ".join(
                    filter(
                        None,
                        [entry.get("teaser", ""), *(entry.get("bulletPoints") or [])],
                    )
                )
                jobs.append(
                    self.build_job(
                        title=entry.get("title", ""),
                        company=advertiser.get("description") or entry.get("companyName", ""),
                        url=f"https://id.jobstreet.com/id/job/{job_id}",
                        description=description,
                        location_raw=location,
                        salary_text=entry.get("salary"),
                        posted_at=entry.get("listingDate"),
                        tags=[entry.get("classification", {}).get("description", "")],
                        extra={"jobstreet_id": job_id, "work_type": entry.get("workType")},
                    )
                )
        return jobs

    # ------------------------------------------------------------ browser path
    def _browser_search(self) -> list[Job]:
        """Signed-in Playwright fallback for when the JSON endpoint dries up."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("jobstreet: playwright not installed; skipping browser fallback")
            return []

        creds = credentials.for_service("jobstreet")
        jobs: list[Job] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=bool(self.config.get("headless", True)))
            context = browser.new_context(
                user_agent=self.config.get("user_agent"),
                viewport={"width": 1440, "height": 900},
                locale="id-ID",
            )
            page = context.new_page()
            try:
                # Sign in first; JobStreet shows more per card to members, and
                # an anonymous session is what got throttled in the first place.
                try:
                    page.goto("https://id.jobstreet.com/oauth/login",
                              wait_until="domcontentloaded", timeout=45_000)
                    page.fill("input[type=email], #email", creds.email or "")
                    page.click("button[type=submit]")
                    page.fill("input[type=password], #password", creds.password or "")
                    page.click("button[type=submit]")
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception as exc:
                    log.warning("jobstreet: login flow did not complete (%s); "
                                "continuing anonymously", exc)

                for query in self.config.get("queries", ["qa-automation"]):
                    slug = query.replace(" ", "-").lower()
                    url = f"https://id.jobstreet.com/id/{slug}-jobs?sortmode=ListedDate"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        page.wait_for_selector("article[data-card-type='JobCard'], "
                                               "[data-automation='normalJob']", timeout=20_000)
                    except Exception as exc:
                        log.warning("jobstreet: results page failed for '%s': %s", query, exc)
                        continue
                    jobs += self._harvest_cards(page)
            finally:
                context.close()
                browser.close()

        return jobs

    def _harvest_cards(self, page: Any) -> list[Job]:
        jobs: list[Job] = []
        try:
            cards = page.query_selector_all(
                "article[data-card-type='JobCard'], [data-automation='normalJob']"
            )
        except Exception:
            return jobs

        for card in cards:
            try:
                title_el = card.query_selector("[data-automation='jobTitle'], a[href*='/job/']")
                company_el = card.query_selector("[data-automation='jobCompany']")
                location_el = card.query_selector("[data-automation='jobLocation']")
                salary_el = card.query_selector("[data-automation='jobSalary']")
                if title_el is None:
                    continue

                href = title_el.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://id.jobstreet.com{href}"

                jobs.append(
                    self.build_job(
                        title=title_el.inner_text().strip(),
                        company=company_el.inner_text().strip() if company_el else "Unknown",
                        url=href.split("?")[0],
                        description=card.inner_text()[:1500],
                        location_raw=(
                            location_el.inner_text().strip() if location_el else "Indonesia"
                        ),
                        salary_text=salary_el.inner_text().strip() if salary_el else None,
                        extra={"via": "browser"},
                    )
                )
            except Exception as exc:
                log.debug("jobstreet: skipped a card: %s", exc)
        return jobs


class GlintsScraper(Scraper):
    """Glints Indonesia, via Playwright against its public explore pages.

    Glints used to expose a REST search endpoint; it now answers 400 to every
    unauthenticated shape, so there is no JSON path left to take. The rendered
    explore page is public, which makes a browser the honest option here.

    Worth the trouble because Glints is the one Indonesian board that reliably
    publishes a monthly IDR range on the card itself — exactly what the 30jt
    floor needs. Requires `playwright install chromium`; degrades to returning
    nothing when Playwright is absent.
    """

    name = "glints"
    EXPLORE = "https://glints.com/id/opportunities/jobs/explore"

    def fetch(self) -> list[Job]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.info("glints: playwright not installed; skipping this source")
            return []

        jobs: list[Job] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=bool(self.config.get("headless", True)))
            context = browser.new_context(
                user_agent=self.config.get("user_agent"),
                viewport={"width": 1440, "height": 900},
                locale="id-ID",
            )
            page = context.new_page()
            try:
                for query in self.config.get("queries", ["QA Automation"]):
                    url = (
                        f"{self.EXPLORE}?keyword={urllib.parse.quote(query)}"
                        "&country=ID&locationName=All+Cities%2FProvinces"
                    )
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        page.wait_for_selector(
                            "[class*='JobCard'], [data-cy='job-card'], a[href*='/opportunities/jobs/']",
                            timeout=20_000,
                        )
                        # Cards lazy-load; one scroll pulls in the rest of page 1.
                        page.mouse.wheel(0, 4000)
                        page.wait_for_timeout(1500)
                    except Exception as exc:
                        log.warning("glints: explore page failed for '%s': %s", query, exc)
                        continue

                    for job in self._harvest_cards(page):
                        if job.url in seen:
                            continue
                        seen.add(job.url)
                        jobs.append(job)
            finally:
                context.close()
                browser.close()

        return jobs

    def _harvest_cards(self, page: Any) -> list[Job]:
        jobs: list[Job] = []
        try:
            cards = page.query_selector_all(
                "[class*='JobCard'], [data-cy='job-card']"
            ) or page.query_selector_all("a[href*='/opportunities/jobs/']")
        except Exception:
            return jobs

        for card in cards:
            try:
                text = card.inner_text().strip()
                if not text:
                    continue
                link = card if card.get_attribute("href") else card.query_selector(
                    "a[href*='/opportunities/jobs/']"
                )
                href = (link.get_attribute("href") if link else "") or ""
                if not href:
                    continue
                if href.startswith("/"):
                    href = f"https://glints.com{href}"

                # The card is a text block: title, company, location, then the
                # salary line. Splitting it is more robust than chasing the
                # hashed class names Glints regenerates on every deploy.
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                title = lines[0] if lines else ""
                company = lines[1] if len(lines) > 1 else "Unknown"
                salary_line = next(
                    (ln for ln in lines if "IDR" in ln.upper() or "RP" in ln.upper()), None
                )
                location_line = next(
                    (ln for ln in lines[1:] if "," in ln and "IDR" not in ln.upper()), ""
                )

                jobs.append(
                    self.build_job(
                        title=title,
                        company=company,
                        url=href.split("?")[0],
                        description=text[:1500],
                        location_raw=location_line or "Indonesia",
                        salary_text=salary_line,
                        extra={"via": "browser"},
                    )
                )
            except Exception as exc:
                log.debug("glints: skipped a card: %s", exc)
        return jobs
