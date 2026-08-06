"""Public JSON / RSS job boards.

These are the workhorses: no auth, no browser, no rate limits worth worrying
about. Each class maps one board's payload onto `Job` and does nothing else —
filtering and scoring happen later, in the matcher.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from ..models import Job, Period, Salary
from .base import Scraper, ScraperError

log = logging.getLogger(__name__)

# Only bother with postings whose title looks testing-related. Applied at the
# scraper layer purely to keep payload volume sane; the matcher is the real gate.
_RELEVANT = re.compile(
    r"\b(qa|q\.a\.|sdet|quality|test|testing|automation|reliability)\b", re.IGNORECASE
)


def _looks_relevant(title: str, tags: Any = None) -> bool:
    if _RELEVANT.search(title or ""):
        return True
    if tags:
        return bool(_RELEVANT.search(" ".join(str(t) for t in tags)))
    return False


class RemoteOKScraper(Scraper):
    """remoteok.com — one JSON array, first element is legal boilerplate."""

    name = "remoteok"

    def fetch(self) -> list[Job]:
        payload = self.get_json(self.config["url"])
        if not isinstance(payload, list):
            raise ScraperError("remoteok: expected a JSON array")

        jobs: list[Job] = []
        for entry in payload[1:][: int(self.config.get("max_results", 200))]:
            title = entry.get("position") or entry.get("title") or ""
            tags = entry.get("tags") or []
            if not _looks_relevant(title, tags):
                continue

            # RemoteOK gives annual USD bounds as separate integer fields.
            salary = Salary()
            if entry.get("salary_min") or entry.get("salary_max"):
                salary = Salary(
                    min_amount=float(entry["salary_min"]) if entry.get("salary_min") else None,
                    max_amount=float(entry["salary_max"]) if entry.get("salary_max") else None,
                    currency="USD",
                    period=Period.YEAR,
                    raw=f"{entry.get('salary_min')}-{entry.get('salary_max')} USD/year",
                )

            jobs.append(
                self.build_job(
                    title=title,
                    company=entry.get("company", ""),
                    url=entry.get("url") or entry.get("apply_url", ""),
                    description=entry.get("description", ""),
                    location_raw=entry.get("location") or "Remote",
                    salary=salary if salary.stated else None,
                    posted_at=entry.get("date") or entry.get("epoch"),
                    tags=tags,
                    remote_hint=True,
                )
            )
        return jobs


class RemotiveScraper(Scraper):
    """remotive.com — supports a category filter, which we use for QA."""

    name = "remotive"

    def fetch(self) -> list[Job]:
        url = self.config["url"]
        params: dict[str, Any] = {"limit": int(self.config.get("max_results", 200))}
        if self.config.get("category"):
            params["category"] = self.config["category"]

        payload = self.get_json(url, params=params)
        entries = payload.get("jobs", []) if isinstance(payload, dict) else []

        # The QA category is narrow, so sweep extra search terms as well. Each
        # is one request; duplicates collapse later on fingerprint.
        for term in self.config.get("searches", []):
            try:
                extra = self.get_json(url, params={"search": term, "limit": 100})
            except Exception as exc:
                log.warning("remotive search '%s' failed: %s", term, exc)
                continue
            entries += extra.get("jobs", []) if isinstance(extra, dict) else []

        jobs: list[Job] = []
        for entry in entries:
            title = entry.get("title", "")
            if not _looks_relevant(title, entry.get("tags")):
                continue
            jobs.append(
                self.build_job(
                    title=title,
                    company=entry.get("company_name", ""),
                    url=entry.get("url", ""),
                    description=entry.get("description", ""),
                    location_raw=entry.get("candidate_required_location") or "Remote",
                    salary_text=entry.get("salary"),
                    posted_at=entry.get("publication_date"),
                    tags=entry.get("tags") or [],
                    remote_hint=True,
                    extra={"job_type": entry.get("job_type")},
                )
            )
        return jobs


class JobicyScraper(Scraper):
    """jobicy.com — geo-filterable remote board, good Europe/Asia coverage.

    Its `geo` filter takes continent-or-country slugs (`europe`, `apac`,
    `singapore`, `germany`) and 400s on anything else — notably `asia` and
    `anywhere`, which look plausible but are not valid. Combining `geo` with
    `industry` also 400s, so the two are swept separately.
    """

    name = "jobicy"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        count = int(self.config.get("max_results", 100))

        # Each sweep is one query: either a geo or an industry, never both.
        sweeps: list[tuple[str, dict[str, Any]]] = [
            (f"geo={geo}", {"count": count, "geo": geo})
            for geo in self.config.get("geos", [])
        ] + [
            (f"industry={industry}", {"count": count, "industry": industry})
            for industry in self.config.get("industries", ["engineering"])
        ]

        for label, params in sweeps:
            geo = str(params.get("geo", "")) or label
            try:
                payload = self.get_json(self.config["url"], params=params)
            except Exception as exc:  # one bad sweep should not kill the rest
                log.warning("jobicy %s failed: %s", label, exc)
                continue

            for entry in (payload or {}).get("jobs", []):
                title = entry.get("jobTitle", "")
                url = entry.get("url", "")
                if url in seen_urls or not _looks_relevant(title, entry.get("jobIndustry")):
                    continue
                seen_urls.add(url)

                salary = Salary()
                if entry.get("salaryMin") or entry.get("salaryMax"):
                    salary = Salary(
                        min_amount=_as_float(entry.get("salaryMin")),
                        max_amount=_as_float(entry.get("salaryMax")),
                        currency=(entry.get("salaryCurrency") or "USD").upper(),
                        period=_period_from_word(entry.get("salaryPeriod")),
                        raw=f"{entry.get('salaryMin')}-{entry.get('salaryMax')} "
                            f"{entry.get('salaryCurrency')} {entry.get('salaryPeriod')}",
                    )

                jobs.append(
                    self.build_job(
                        title=title,
                        company=entry.get("companyName", ""),
                        url=url,
                        description=entry.get("jobDescription") or entry.get("jobExcerpt", ""),
                        location_raw=entry.get("jobGeo") or geo,
                        salary=salary if salary.stated else None,
                        posted_at=entry.get("pubDate"),
                        tags=entry.get("jobIndustry") or [],
                        remote_hint=True,
                        extra={"geo": geo},
                    )
                )
        return jobs


class HimalayasScraper(Scraper):
    """himalayas.app — remote board with structured salary fields.

    The API hard-caps at 20 records per call regardless of `limit`, and the feed
    spans every discipline (~97k postings), so QA roles are sparse. Pagination
    via `offset` is therefore mandatory, not an optimisation: without it you
    read 20 mixed postings and find nothing.
    """

    name = "himalayas"
    PAGE_SIZE = 20

    def fetch(self) -> list[Job]:
        wanted = int(self.config.get("max_results", 200))
        entries: list[dict[str, Any]] = []

        for offset in range(0, wanted, self.PAGE_SIZE):
            try:
                payload = self.get_json(
                    self.config["url"],
                    params={"limit": self.PAGE_SIZE, "offset": offset},
                )
            except Exception as exc:
                log.warning("himalayas offset=%s failed: %s", offset, exc)
                break

            page = payload.get("jobs", []) if isinstance(payload, dict) else []
            if not page:
                break
            entries.extend(page)

        jobs: list[Job] = []
        for entry in entries:
            title = entry.get("title", "")
            if not _looks_relevant(title, entry.get("categories")):
                continue

            salary = Salary()
            if entry.get("minSalary") or entry.get("maxSalary"):
                salary = Salary(
                    min_amount=_as_float(entry.get("minSalary")),
                    max_amount=_as_float(entry.get("maxSalary")),
                    currency=(entry.get("currency") or "USD").upper(),
                    period=_period_from_word(entry.get("salaryPeriod")),
                    raw=f"{entry.get('minSalary')}-{entry.get('maxSalary')} "
                        f"{entry.get('currency')} {entry.get('salaryPeriod')}",
                )

            locations = entry.get("locationRestrictions") or []
            jobs.append(
                self.build_job(
                    title=title,
                    company=entry.get("companyName", ""),
                    url=entry.get("applicationLink") or entry.get("guid", ""),
                    description=entry.get("description") or entry.get("excerpt", ""),
                    location_raw=", ".join(str(l) for l in locations) or "Remote",
                    salary=salary if salary.stated else None,
                    posted_at=entry.get("pubDate") or entry.get("publishedDate"),
                    tags=entry.get("categories") or [],
                    remote_hint=True,
                    extra={"seniority": entry.get("seniority")},
                )
            )
        return jobs


class ArbeitnowScraper(Scraper):
    """arbeitnow.com — Europe-heavy, exposes visa-sponsorship flags."""

    name = "arbeitnow"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for page in range(1, int(self.config.get("max_pages", 3)) + 1):
            try:
                payload = self.get_json(self.config["url"], params={"page": page})
            except Exception as exc:
                log.warning("arbeitnow page=%s failed: %s", page, exc)
                break

            entries = (payload or {}).get("data", [])
            if not entries:
                break

            for entry in entries:
                title = entry.get("title", "")
                tags = (entry.get("tags") or []) + (entry.get("job_types") or [])
                if not _looks_relevant(title, tags):
                    continue
                jobs.append(
                    self.build_job(
                        title=title,
                        company=entry.get("company_name", ""),
                        url=entry.get("url", ""),
                        description=entry.get("description", ""),
                        location_raw=entry.get("location", ""),
                        posted_at=entry.get("created_at"),
                        tags=tags,
                        remote_hint=bool(entry.get("remote")),
                        extra={"visa_sponsorship": entry.get("visa_sponsorship")},
                    )
                )
        return jobs


class WeWorkRemotelyScraper(Scraper):
    """weworkremotely.com — RSS only. Company sits in the title as "Co: Role"."""

    name = "weworkremotely"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for feed_url in self.config.get("feeds", []):
            try:
                response = self.get(feed_url)
                root = ET.fromstring(response.content)
            except Exception as exc:
                log.warning("weworkremotely feed %s failed: %s", feed_url, exc)
                continue

            for item in root.iter("item"):
                raw_title = (item.findtext("title") or "").strip()
                company, _, title = raw_title.partition(":")
                title = (title or raw_title).strip()
                if not _looks_relevant(title):
                    continue

                description = item.findtext("description") or ""
                region = item.findtext("region") or ""
                jobs.append(
                    self.build_job(
                        title=title,
                        company=company.strip() or "Unknown",
                        url=(item.findtext("link") or "").strip(),
                        description=description,
                        location_raw=region or "Remote",
                        posted_at=item.findtext("pubDate"),
                        tags=[c.text for c in item.findall("category") if c.text],
                        remote_hint=True,
                    )
                )
        return jobs


def _period_from_word(word: Any) -> Period:
    """Map a board's period word ("yearly", "monthly", "hourly") to a Period.

    Defaults to yearly: every board that ships a structured salary period uses
    annual figures unless it says otherwise.
    """
    text = str(word or "").lower()
    if text.startswith("hour"):
        return Period.HOUR
    if text.startswith("da"):
        return Period.DAY
    if text.startswith("month"):
        return Period.MONTH
    if text.startswith("week"):
        # No weekly period in the model; a week is ~1/4 of a month.
        return Period.MONTH
    return Period.YEAR


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None
