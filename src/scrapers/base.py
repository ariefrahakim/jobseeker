"""Scraper base class and shared HTTP plumbing.

Every scraper subclasses `Scraper` and implements `fetch()`, returning a list of
normalised `Job`s. A scraper that raises is logged and skipped — one dead board
must never take the run down with it.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from ..models import Job, Salary
from ..profile import Profile, detect_arrangement, normalise_country
from ..salary import parse_salary

log = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """Raised for a source-level failure the pipeline should report but survive."""


class Scraper(ABC):
    #: registry key, must match the name in sources.yaml
    name: str = "base"

    def __init__(self, profile: Profile, config: dict[str, Any]):
        self.profile = profile
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.get("user_agent", "jobseeker-agent/1.0"),
                "Accept": "application/json, text/xml, text/html;q=0.9",
            }
        )

    # ------------------------------------------------------------------ hooks
    @abstractmethod
    def fetch(self) -> list[Job]:
        """Pull postings from this source. Implemented per board."""

    # ------------------------------------------------------------- HTTP utils
    def get(self, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.config.get("timeout_seconds", 30))
        delay = float(self.config.get("delay_seconds", 1.0))
        response = self.session.get(url, timeout=timeout, **kwargs)
        response.raise_for_status()
        if delay:
            time.sleep(delay)
        return response

    def get_json(self, url: str, **kwargs) -> Any:
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ScraperError(f"{self.name}: response was not JSON ({url})") from exc

    # ------------------------------------------------------- normalisation kit
    @staticmethod
    def strip_html(text: str | None) -> str:
        if not text:
            return ""
        without_tags = re.sub(r"<[^>]+>", " ", text)
        collapsed = re.sub(r"&nbsp;?", " ", without_tags)
        collapsed = re.sub(r"&amp;", "&", collapsed)
        collapsed = re.sub(r"&(lt|gt|quot|#39);", " ", collapsed)
        return " ".join(collapsed.split())

    @staticmethod
    def parse_date(value: Any) -> datetime | None:
        """Accept ISO strings, epoch seconds/ms, and RFC 822 feed dates."""
        if value in (None, "", 0):
            return None

        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

        text = str(value).strip()
        # ISO 8601, with or without a trailing Z.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

        for fmt in (
            "%a, %d %b %Y %H:%M:%S %z",   # RSS
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        # "3 days ago", "2 weeks ago", "posted 5 hours ago"
        rel = re.search(r"(\d+)\s*(hour|day|week|month)s?\s*ago", text, re.IGNORECASE)
        if rel:
            n = int(rel.group(1))
            unit = rel.group(2).lower()
            hours = {"hour": 1, "day": 24, "week": 168, "month": 720}[unit] * n
            return datetime.now(timezone.utc) - timedelta(hours=hours)
        return None

    def build_job(
        self,
        *,
        title: str,
        company: str,
        url: str,
        description: str = "",
        location_raw: str = "",
        salary_text: str | None = None,
        salary: Salary | None = None,
        posted_at: Any = None,
        tags: Iterable[str] | None = None,
        extra: dict[str, Any] | None = None,
        remote_hint: bool | None = None,
    ) -> Job:
        """Assemble a `Job`, inferring country / arrangement / salary.

        Scrapers should pass whatever the board gives them and let this handle
        the normalisation, so all sources behave identically downstream.
        """
        desc = self.strip_html(description)
        loc = (location_raw or "").strip()

        arrangement = detect_arrangement(title, loc, desc[:2000])
        if remote_hint and arrangement.value == "unknown":
            from ..models import Arrangement

            arrangement = Arrangement.REMOTE

        # Salary can arrive structured (rare) or as free text (common). Also
        # sweep the description when the field itself is empty, since many
        # boards bury the number in the body.
        resolved = salary or parse_salary(salary_text)
        if not resolved.stated:
            snippet = self._salary_snippet(desc)
            if snippet:
                resolved = parse_salary(snippet)

        return Job(
            title=" ".join(title.split()),
            company=" ".join((company or "Unknown").split()),
            url=url,
            source=self.name,
            description=desc,
            location_raw=loc,
            country=normalise_country(loc) or normalise_country(desc[:400]),
            arrangement=arrangement,
            salary=resolved,
            posted_at=self.parse_date(posted_at),
            tags=[str(t) for t in (tags or [])],
            extra=extra or {},
        )

    @staticmethod
    def _salary_snippet(description: str) -> str | None:
        """Find the sentence in a description that talks about money."""
        if not description:
            return None
        pattern = re.compile(
            r"[^.]*?(?:salary|compensation|gaji|package|remuneration|pay range|"
            r"base pay|rp\s*\d|idr\s*\d|\$\s*\d|€\s*\d|£\s*\d)[^.]*\.",
            re.IGNORECASE,
        )
        match = pattern.search(description)
        return match.group(0) if match else None
