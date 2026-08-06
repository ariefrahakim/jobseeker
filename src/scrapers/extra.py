"""Additional sources, added to widen coverage.

Each was verified live before being added here, and each covers a gap the
original set left:

  * **Kalibrr** — Indonesian board with a *structured* salary object. This is the
    single most valuable addition for the IDR 30jt floor: most Indonesian
    listings bury pay in prose or omit it, and Kalibrr publishes min/max/period
    as fields.
  * **Ashby** — a modern ATS used by companies Greenhouse and Lever do not cover.
  * **SmartRecruiters** — another ATS, with a per-company keyword search.
  * **WorkingNomads** — a remote board that does not overlap much with RemoteOK.
  * **Hacker News "Who is hiring"** — the monthly thread. No board indexes it, so
    it is genuinely additive; postings are free text, which the salary parser and
    scorer already handle.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..models import Job, Period, Salary
from .base import Scraper

log = logging.getLogger(__name__)

_RELEVANT = re.compile(
    r"\b(qa|q\.a\.|sdet|quality|test|testing|automation|reliability)\b", re.IGNORECASE
)


def _as_number(value: Any) -> float | None:
    """Coerce whatever a board put in a numeric field, or None.

    Boards are inconsistent about this: the same field arrives as a float, a
    string, an empty string, or null depending on the record.
    """
    if value in (None, "", 0):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class KalibrrScraper(Scraper):
    """Kalibrr Indonesia — the board search API its own site calls."""

    name = "kalibrr"
    SEARCH = "https://www.kalibrr.com/kjs/job_board/search"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        per_page = int(self.config.get("page_size", 30))
        max_pages = int(self.config.get("max_pages", 3))

        for query in self.config.get("queries", ["qa automation"]):
            for page in range(max_pages):
                params = {
                    "limit": per_page,
                    "offset": page * per_page,
                    "text": query,
                    "country": "Indonesia",
                    "sort_direction": "DESC",
                    "sort_field": "activation_date",
                }
                try:
                    payload = self.get_json(self.SEARCH, params=params)
                except Exception as exc:
                    log.warning("kalibrr '%s' page %s failed: %s", query, page, exc)
                    break

                entries = (payload or {}).get("jobs", [])
                if not entries:
                    break

                for entry in entries:
                    job_id = str(entry.get("id", ""))
                    title = entry.get("name") or entry.get("title", "")
                    if not job_id or job_id in seen or not _RELEVANT.search(title):
                        continue
                    seen.add(job_id)

                    company = entry.get("company_name") or (
                        entry.get("company") or {}
                    ).get("name", "")
                    google_loc = entry.get("google_location") or {}
                    address = (google_loc.get("address_components") or {})
                    location = ", ".join(
                        x for x in (address.get("city"), address.get("region"), "Indonesia") if x
                    )

                    jobs.append(
                        self.build_job(
                            title=title,
                            company=company,
                            url=entry.get("apply_redirect_url")
                            or f"https://www.kalibrr.com/c/{entry.get('company_info', {}).get('code', '')}/jobs/{job_id}",
                            description=" ".join(
                                filter(
                                    None,
                                    [
                                        entry.get("description", ""),
                                        entry.get("qualifications", ""),
                                    ],
                                )
                            ),
                            location_raw=location,
                            salary=self._salary(entry),
                            posted_at=entry.get("activation_date") or entry.get("created_at"),
                            tags=[s for s in (entry.get("tags") or []) if isinstance(s, str)],
                            extra={"kalibrr_id": job_id},
                        )
                    )
        return jobs

    @staticmethod
    def _salary(entry: dict[str, Any]) -> Salary | None:
        """Read Kalibrr's structured salary fields.

        The shape is flat, not nested: `base_salary` is the minimum as a bare
        number (or None), with `maximum_salary`, `salary_currency` and
        `salary_interval` alongside it. An earlier version of this method assumed
        a nested schema.org object and crashed on the float — hence the explicit
        numeric coercion below rather than any attribute access.

        `salary_interval` is usually null even when amounts are present, so the
        period is inferred from magnitude the same way free text is: an
        Indonesian figure in the millions is monthly, not annual.
        """
        lo = _as_number(entry.get("base_salary"))
        hi = _as_number(entry.get("maximum_salary"))
        if lo is None and hi is None:
            return None

        currency = str(entry.get("salary_currency") or "IDR").upper()
        interval = str(entry.get("salary_interval") or "").upper()
        period = {
            "HOUR": Period.HOUR, "HOURLY": Period.HOUR,
            "DAY": Period.DAY, "DAILY": Period.DAY,
            "MONTH": Period.MONTH, "MONTHLY": Period.MONTH,
            "YEAR": Period.YEAR, "YEARLY": Period.YEAR, "ANNUAL": Period.YEAR,
        }.get(interval)

        if period is None:
            from ..salary import infer_period

            period = infer_period(lo if lo is not None else hi or 0, currency)

        return Salary(
            min_amount=lo,
            max_amount=hi if hi != lo else None,
            currency=currency,
            period=period,
            raw=f"{lo}-{hi} {currency} per {period.value}",
        )


class AshbyScraper(Scraper):
    """Ashby job boards. One request per company, same shape as Greenhouse."""

    name = "ashby"
    API = "https://api.ashbyhq.com/posting-api/job-board/{board}"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for board in self.config.get("boards", []):
            try:
                payload = self.get_json(
                    self.API.format(board=board), params={"includeCompensation": "true"}
                )
            except Exception as exc:
                log.info("ashby board '%s' unavailable: %s", board, exc)
                continue

            for entry in (payload or {}).get("jobs", []):
                title = entry.get("title", "")
                # `isListed` false means the posting is not public yet.
                if not _RELEVANT.search(title) or entry.get("isListed") is False:
                    continue

                secondary = entry.get("secondaryLocations") or []
                location = " | ".join(
                    filter(
                        None,
                        [
                            entry.get("location", ""),
                            *[str(s.get("location", "")) for s in secondary],
                        ],
                    )
                )
                jobs.append(
                    self.build_job(
                        title=title,
                        company=(entry.get("organizationName")
                                 or board.replace("-", " ").title()),
                        url=entry.get("jobUrl") or entry.get("applyUrl", ""),
                        description=entry.get("descriptionPlain")
                        or entry.get("descriptionHtml", ""),
                        location_raw=location,
                        salary_text=self._compensation_text(entry),
                        posted_at=entry.get("publishedAt") or entry.get("updatedAt"),
                        tags=[t for t in (entry.get("department"), entry.get("team")) if t],
                        remote_hint=bool(entry.get("isRemote")),
                        extra={"board": board, "ats": "ashby",
                               "employment_type": entry.get("employmentType")},
                    )
                )
        return jobs

    @staticmethod
    def _compensation_text(entry: dict[str, Any]) -> str | None:
        """Flatten Ashby's compensation tiers into text the parser can read."""
        comp = entry.get("compensation") or {}
        summary = comp.get("compensationTierSummary") or comp.get("summary")
        if summary:
            return str(summary)
        tiers = comp.get("compensationTiers") or []
        return str(tiers[0].get("tierSummary")) if tiers and tiers[0].get("tierSummary") else None


class SmartRecruitersScraper(Scraper):
    """SmartRecruiters postings API — public, one call per company."""

    name = "smartrecruiters"
    API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for company in self.config.get("companies", []):
            for query in self.config.get("queries", ["qa"]):
                try:
                    payload = self.get_json(
                        self.API.format(company=company),
                        params={"q": query, "limit": 100},
                    )
                except Exception as exc:
                    log.info("smartrecruiters '%s' unavailable: %s", company, exc)
                    break

                for entry in (payload or {}).get("content", []):
                    title = entry.get("name", "")
                    if not _RELEVANT.search(title):
                        continue

                    loc = entry.get("location") or {}
                    location = ", ".join(
                        str(x) for x in (loc.get("city"), loc.get("region"), loc.get("country"))
                        if x
                    )
                    jobs.append(
                        self.build_job(
                            title=title,
                            company=(entry.get("company") or {}).get("name") or company,
                            url=entry.get("applyUrl")
                            or f"https://jobs.smartrecruiters.com/{company}/{entry.get('id', '')}",
                            description=entry.get("jobAd", {}).get("sections", {}).get(
                                "jobDescription", {}
                            ).get("text", ""),
                            location_raw=location,
                            posted_at=entry.get("releasedDate") or entry.get("createdOn"),
                            tags=[(entry.get("department") or {}).get("label", "")],
                            remote_hint=bool(loc.get("remote")),
                            extra={"company_slug": company, "ats": "smartrecruiters"},
                        )
                    )
        return jobs


class WorkingNomadsScraper(Scraper):
    """workingnomads.com — a flat JSON array of currently open remote roles."""

    name = "workingnomads"
    API = "https://www.workingnomads.com/api/exposed_jobs/"

    def fetch(self) -> list[Job]:
        payload = self.get_json(self.API)
        if not isinstance(payload, list):
            return []

        jobs: list[Job] = []
        for entry in payload:
            title = entry.get("title", "")
            tags = str(entry.get("tags") or "").split(",")
            if not (_RELEVANT.search(title) or _RELEVANT.search(str(entry.get("tags") or ""))):
                continue

            jobs.append(
                self.build_job(
                    title=title,
                    company=entry.get("company_name", ""),
                    url=entry.get("url", ""),
                    description=entry.get("description", ""),
                    location_raw=entry.get("location") or "Remote",
                    posted_at=entry.get("pub_date"),
                    tags=[t.strip() for t in tags if t.strip()],
                    remote_hint=True,
                    extra={"category": entry.get("category_name")},
                )
            )
        return jobs


class HackerNewsScraper(Scraper):
    """The monthly "Ask HN: Who is hiring?" thread, via the Algolia search API.

    Genuinely additive: these postings appear on no job board. The trade-off is
    format — each is one free-text comment, conventionally
    `Company | Role | Location | REMOTE | stack`. That is enough for the salary
    parser and the scorer, and the first line is a usable title.
    """

    name = "hackernews"
    # search_by_date, not search: relevance ranking happily returns a 2015
    # comment, and a filled 2015 vacancy is worth nothing.
    SEARCH = "https://hn.algolia.com/api/v1/search_by_date"

    #: Only comments under a hiring thread count. Algolia's comment index spans
    #: all of HN, so without this you collect opinions about testing tools.
    _HIRING_THREAD = re.compile(r"who\s*(is|'s)\s*hiring", re.IGNORECASE)
    #: The sibling thread is candidates advertising themselves — the opposite of
    #: a job posting, and it parses into convincing-looking garbage.
    _CANDIDATE_THREAD = re.compile(
        r"wants?\s+to\s+be\s+hired|seeking\s+(work|freelancer)", re.IGNORECASE
    )

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()

        for query in self.config.get("queries", ["QA automation", "SDET"]):
            params = {
                "query": query,
                "tags": "comment",
                "hitsPerPage": int(self.config.get("max_results", 50)),
                # Only the last ~90 days; older threads are filled.
                "numericFilters": f"created_at_i>{self._cutoff()}",
            }
            try:
                payload = self.get_json(self.SEARCH, params=params)
            except Exception as exc:
                log.warning("hackernews '%s' failed: %s", query, exc)
                continue

            for hit in (payload or {}).get("hits", []):
                text = self.strip_html(hit.get("comment_text") or "")
                object_id = str(hit.get("objectID", ""))
                story = str(hit.get("story_title") or "")

                if not text or object_id in seen:
                    continue
                if not self._HIRING_THREAD.search(story) or self._CANDIDATE_THREAD.search(story):
                    continue
                # A candidate's self-advert inside a hiring thread reads the
                # same to a keyword scan; these phrases only appear in one.
                if re.search(r"willing to relocate|seeking\s+(work|position|role)",
                             text[:600], re.IGNORECASE):
                    continue
                if not _RELEVANT.search(text[:400]):
                    continue
                seen.add(object_id)

                company, title, location = self._parse_header(text)
                jobs.append(
                    self.build_job(
                        title=title,
                        company=company,
                        url=f"https://news.ycombinator.com/item?id={object_id}",
                        description=text,
                        location_raw=location,
                        posted_at=hit.get("created_at"),
                        tags=["hn-who-is-hiring"],
                        extra={"author": hit.get("author"), "thread": story},
                    )
                )
        return jobs

    @staticmethod
    def _cutoff() -> int:
        # 90 days of threads, computed from the newest hit's own clock rather
        # than ours would be nicer; a fixed window is close enough here.
        from datetime import datetime, timedelta, timezone

        return int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())

    @staticmethod
    def _parse_header(text: str) -> tuple[str, str, str]:
        """Read the conventional `Company | Role | Location | ...` first line."""
        first_line = text.split("\n")[0][:300]
        parts = [p.strip() for p in re.split(r"\s*[|•]\s*", first_line) if p.strip()]

        company = parts[0] if parts else "Unknown (HN)"
        # The role is whichever segment mentions the job, not necessarily the 2nd.
        title = next((p for p in parts[1:] if _RELEVANT.search(p)), "")
        if not title:
            title = parts[1] if len(parts) > 1 else "QA role (see post)"
        location = next(
            (
                p
                for p in parts[1:]
                if p is not title
                and re.search(r"remote|onsite|hybrid|,|\b[A-Z]{2}\b", p, re.IGNORECASE)
            ),
            "",
        )
        return company[:80], title[:120], location[:120]
