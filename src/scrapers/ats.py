"""Company career pages, read through their ATS APIs.

Greenhouse and Lever both expose a documented public JSON endpoint per board.
This is the highest-signal source in the whole pipeline: you pick the companies,
so every hit is somewhere you would actually work, and there is no ranking
algorithm between you and the posting.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..models import Job
from .base import Scraper

log = logging.getLogger(__name__)

_RELEVANT = re.compile(
    r"\b(qa|sdet|quality|test|testing|automation)\b", re.IGNORECASE
)


class GreenhouseScraper(Scraper):
    """boards-api.greenhouse.io — `?content=true` includes the full description."""

    name = "greenhouse"
    API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for board in self.config.get("boards", []):
            try:
                payload = self.get_json(
                    self.API.format(board=board), params={"content": "true"}
                )
            except Exception as exc:
                # A renamed or private board is expected attrition, not a bug.
                log.info("greenhouse board '%s' unavailable: %s", board, exc)
                continue

            for entry in (payload or {}).get("jobs", []):
                title = entry.get("title", "")
                if not _RELEVANT.search(title):
                    continue

                location = (entry.get("location") or {}).get("name", "")
                # Departments/offices carry useful region hints when location is
                # vague ("Remote"), so fold them into the location string.
                offices = ", ".join(
                    o.get("name", "") for o in (entry.get("offices") or []) if o.get("name")
                )
                jobs.append(
                    self.build_job(
                        title=title,
                        company=board.replace("-", " ").title(),
                        url=entry.get("absolute_url", ""),
                        description=entry.get("content", ""),
                        location_raw=" | ".join(x for x in (location, offices) if x),
                        posted_at=entry.get("updated_at") or entry.get("created_at"),
                        tags=[
                            d.get("name", "")
                            for d in (entry.get("departments") or [])
                            if d.get("name")
                        ],
                        extra={"board": board, "ats": "greenhouse"},
                    )
                )
        return jobs


class LeverScraper(Scraper):
    """api.lever.co postings endpoint."""

    name = "lever"
    API = "https://api.lever.co/v0/postings/{board}"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for board in self.config.get("boards", []):
            try:
                payload = self.get_json(self.API.format(board=board), params={"mode": "json"})
            except Exception as exc:
                log.info("lever board '%s' unavailable: %s", board, exc)
                continue

            for entry in payload or []:
                title = entry.get("text", "")
                if not _RELEVANT.search(title):
                    continue

                categories: dict[str, Any] = entry.get("categories") or {}
                description = " ".join(
                    filter(
                        None,
                        [
                            entry.get("descriptionPlain") or entry.get("description", ""),
                            *[
                                f"{l.get('text', '')} {l.get('content', '')}"
                                for l in (entry.get("lists") or [])
                            ],
                        ],
                    )
                )
                jobs.append(
                    self.build_job(
                        title=title,
                        company=board.replace("-", " ").title(),
                        url=entry.get("hostedUrl") or entry.get("applyUrl", ""),
                        description=description,
                        location_raw=categories.get("location", ""),
                        salary_text=entry.get("salaryRange") and str(entry["salaryRange"]),
                        posted_at=entry.get("createdAt"),
                        tags=[
                            v
                            for k, v in categories.items()
                            if k in ("team", "department", "commitment") and v
                        ],
                        remote_hint=str(categories.get("location", "")).lower().startswith("remote"),
                        extra={"board": board, "ats": "lever"},
                    )
                )
        return jobs
