"""Profile and source-config loading, plus location normalisation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from .models import Arrangement

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Location strings are wildly inconsistent ("SF", "Remote - EU", "Jkt").
# These map the common forms onto canonical country names used by profile.yaml.
_COUNTRY_ALIASES = {
    "indonesia": "Indonesia", "id": "Indonesia", "jakarta": "Indonesia",
    "jkt": "Indonesia", "bandung": "Indonesia", "surabaya": "Indonesia",
    "yogyakarta": "Indonesia", "bali": "Indonesia", "tangerang": "Indonesia",
    "bekasi": "Indonesia", "bsd": "Indonesia", "depok": "Indonesia",
    "singapore": "Singapore", "sg": "Singapore",
    "malaysia": "Malaysia", "kuala lumpur": "Malaysia", "my": "Malaysia",
    "thailand": "Thailand", "bangkok": "Thailand",
    "vietnam": "Vietnam", "viet nam": "Vietnam", "hanoi": "Vietnam",
    "ho chi minh": "Vietnam",
    "philippines": "Philippines", "manila": "Philippines", "ph": "Philippines",
    "japan": "Japan", "tokyo": "Japan", "jp": "Japan",
    "south korea": "South Korea", "korea": "South Korea", "seoul": "South Korea",
    "hong kong": "Hong Kong", "hk": "Hong Kong",
    "taiwan": "Taiwan", "taipei": "Taiwan",
    "india": "India", "bangalore": "India", "bengaluru": "India",
    "hyderabad": "India", "mumbai": "India", "pune": "India", "delhi": "India",
    "uae": "United Arab Emirates", "dubai": "United Arab Emirates",
    "abu dhabi": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "saudi arabia": "Saudi Arabia", "riyadh": "Saudi Arabia",
    "qatar": "Qatar", "doha": "Qatar",
    "israel": "Israel", "tel aviv": "Israel",
    "turkey": "Turkey", "istanbul": "Turkey", "turkiye": "Turkey",
    "united kingdom": "United Kingdom", "uk": "United Kingdom",
    "england": "United Kingdom", "london": "United Kingdom",
    "scotland": "United Kingdom", "manchester": "United Kingdom",
    "gb": "United Kingdom",
    "ireland": "Ireland", "dublin": "Ireland",
    "netherlands": "Netherlands", "amsterdam": "Netherlands",
    "holland": "Netherlands", "nl": "Netherlands", "utrecht": "Netherlands",
    "germany": "Germany", "berlin": "Germany", "munich": "Germany",
    "münchen": "Germany", "hamburg": "Germany", "de": "Germany",
    "deutschland": "Germany", "frankfurt": "Germany", "cologne": "Germany",
    "france": "France", "paris": "France", "fr": "France", "lyon": "France",
    "spain": "Spain", "madrid": "Spain", "barcelona": "Spain", "es": "Spain",
    "portugal": "Portugal", "lisbon": "Portugal", "lisboa": "Portugal",
    "porto": "Portugal", "pt": "Portugal",
    "italy": "Italy", "milan": "Italy", "rome": "Italy", "it": "Italy",
    "switzerland": "Switzerland", "zurich": "Switzerland", "zürich": "Switzerland",
    "geneva": "Switzerland", "ch": "Switzerland",
    "austria": "Austria", "vienna": "Austria",
    "belgium": "Belgium", "brussels": "Belgium",
    "luxembourg": "Luxembourg",
    "denmark": "Denmark", "copenhagen": "Denmark",
    "sweden": "Sweden", "stockholm": "Sweden", "se": "Sweden",
    "norway": "Norway", "oslo": "Norway",
    "finland": "Finland", "helsinki": "Finland",
    "iceland": "Iceland", "reykjavik": "Iceland",
    "estonia": "Estonia", "tallinn": "Estonia",
    "latvia": "Latvia", "riga": "Latvia",
    "lithuania": "Lithuania", "vilnius": "Lithuania",
    "poland": "Poland", "warsaw": "Poland", "krakow": "Poland",
    "kraków": "Poland", "pl": "Poland", "wroclaw": "Poland",
    "czechia": "Czechia", "czech republic": "Czechia", "prague": "Czechia",
    "slovakia": "Slovakia", "bratislava": "Slovakia",
    "hungary": "Hungary", "budapest": "Hungary",
    "romania": "Romania", "bucharest": "Romania", "cluj": "Romania",
    "bulgaria": "Bulgaria", "sofia": "Bulgaria",
    "croatia": "Croatia", "zagreb": "Croatia",
    "slovenia": "Slovenia", "ljubljana": "Slovenia",
    "serbia": "Serbia", "belgrade": "Serbia",
    "greece": "Greece", "athens": "Greece",
    "cyprus": "Cyprus", "malta": "Malta",
    "united states": "United States", "usa": "United States",
    "us": "United States", "new york": "United States",
    "san francisco": "United States", "austin": "United States",
    "seattle": "United States", "remote us": "United States",
    "canada": "Canada", "toronto": "Canada", "vancouver": "Canada",
    "australia": "Australia", "sydney": "Australia", "melbourne": "Australia",
    "brazil": "Brazil", "mexico": "Mexico", "argentina": "Argentina",
}

_REMOTE_HINTS = ("remote", "work from home", "wfh", "distributed",
                 "anywhere", "fully remote", "remote-first", "kerja dari rumah")
_HYBRID_HINTS = ("hybrid", "flexible location", "partially remote", "2 days in office",
                 "3 days in office", "onsite/remote")
_ONSITE_HINTS = ("on-site", "onsite", "in office", "in-office", "on site",
                 "wfo", "work from office")


def normalise_country(text: str | None) -> str | None:
    """Pull a canonical country out of a messy location string."""
    if not text:
        return None
    lowered = text.lower()
    # Longest alias first so "south korea" beats "korea" and "new york" beats "us".
    for alias in sorted(_COUNTRY_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            return _COUNTRY_ALIASES[alias]
    return None


def detect_arrangement(*texts: str | None) -> Arrangement:
    """Read the work arrangement from title, location and description.

    Order matters: "hybrid" beats "remote" because hybrid postings almost
    always also say "remote", and onsite is only asserted when nothing else fits.
    """
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return Arrangement.UNKNOWN
    if any(h in blob for h in _HYBRID_HINTS):
        return Arrangement.HYBRID
    if any(h in blob for h in _REMOTE_HINTS):
        return Arrangement.REMOTE
    if any(h in blob for h in _ONSITE_HINTS):
        return Arrangement.ONSITE
    return Arrangement.UNKNOWN


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Profile:
    """Typed view over `config/profile.yaml`."""

    data: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Profile":
        target = Path(path) if path else CONFIG_DIR / "profile.yaml"
        return cls(_load_yaml(target))

    # --- identity -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.data.get("identity", {}).get("name", "Candidate")

    @property
    def identity(self) -> dict[str, Any]:
        return self.data.get("identity", {})

    # --- titles -------------------------------------------------------------
    @property
    def primary_titles(self) -> list[str]:
        return self.data.get("titles", {}).get("primary", [])

    @property
    def accept_titles(self) -> list[str]:
        return self.data.get("titles", {}).get("accept", [])

    @property
    def reject_titles(self) -> list[str]:
        return self.data.get("titles", {}).get("reject", [])

    # --- skills -------------------------------------------------------------
    @cached_property
    def skill_weights(self) -> dict[str, float]:
        """Flattened {skill: weight} across core/secondary/adjacent."""
        merged: dict[str, float] = {}
        for bucket in ("core", "secondary", "adjacent"):
            for skill, weight in (self.data.get("skills", {}).get(bucket) or {}).items():
                merged[skill.lower()] = float(weight)
        return merged

    @property
    def core_skills(self) -> list[str]:
        return [s.lower() for s in (self.data.get("skills", {}).get("core") or {})]

    @property
    def domains(self) -> list[str]:
        return [d.lower() for d in self.data.get("domains", [])]

    # --- preferences --------------------------------------------------------
    @property
    def seniority(self) -> dict[str, Any]:
        return self.data.get("seniority", {})

    @property
    def work_preference(self) -> dict[str, Any]:
        return self.data.get("work_preference", {})

    @property
    def salary_rules(self) -> dict[str, Any]:
        return self.data.get("salary_rules", {})

    @property
    def target_regions(self) -> list[str]:
        return self.data.get("target_regions", ["indonesia", "asia", "europe"])

    @cached_property
    def fx_to_idr(self) -> dict[str, float]:
        rates = {k.upper(): float(v) for k, v in (self.data.get("fx_to_idr") or {}).items()}
        rates.setdefault("IDR", 1.0)
        # Allow an env override for a single currency without editing YAML,
        # e.g. FX_USD_IDR=16800 during a rate swing.
        for currency in list(rates):
            override = os.getenv(f"FX_{currency}_IDR")
            if override:
                try:
                    rates[currency] = float(override)
                except ValueError:
                    pass
        return rates

    @property
    def scoring(self) -> dict[str, Any]:
        return self.data.get("scoring", {})

    @property
    def min_score(self) -> float:
        return float(self.scoring.get("min_score", 55))

    @property
    def max_age_days(self) -> int:
        return int(self.scoring.get("max_age_days", 30))

    def search_queries(self) -> list[str]:
        """Queries handed to keyword-driven sources."""
        return self.primary_titles or ["QA Automation Engineer"]


@dataclass
class SourceConfig:
    """Typed view over `config/sources.yaml`."""

    data: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SourceConfig":
        target = Path(path) if path else CONFIG_DIR / "sources.yaml"
        return cls(_load_yaml(target))

    @property
    def defaults(self) -> dict[str, Any]:
        return self.data.get("defaults", {})

    def for_source(self, name: str) -> dict[str, Any]:
        cfg = dict(self.defaults)
        cfg.update((self.data.get("sources", {}) or {}).get(name, {}) or {})
        return cfg

    def enabled_sources(self) -> list[str]:
        return [
            name
            for name, cfg in (self.data.get("sources", {}) or {}).items()
            if (cfg or {}).get("enabled", False)
        ]
