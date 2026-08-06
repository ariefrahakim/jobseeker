"""Credential lookup for the sources that need a login.

Both naming conventions are accepted, because both are in the wild and it is not
worth making anyone rename their `.env`:

    LINKEDIN_EMAIL / LINKEDIN_PASSWORD      (SERVICE first)
    EMAIL_LINKEDIN / PASSWORD_LINKEDIN      (field first)

Nothing here reads a file directly — `main.py` loads `.env` once at startup via
python-dotenv, and everything after that goes through `os.environ`. `.env` is
gitignored; never log or print the values this module returns.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    email: str | None
    password: str | None

    @property
    def complete(self) -> bool:
        return bool(self.email and self.password)

    @property
    def partial(self) -> bool:
        """An email with no password — usually a half-filled `.env`."""
        return bool(self.email) and not self.password


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def for_service(service: str) -> Credentials:
    """Resolve credentials for e.g. "linkedin" or "jobstreet"."""
    key = service.upper()
    email = _first_env(
        f"{key}_EMAIL", f"EMAIL_{key}", f"{key}_USERNAME", f"USERNAME_{key}"
    )
    password = _first_env(f"{key}_PASSWORD", f"PASSWORD_{key}")

    creds = Credentials(email=email, password=password)
    if creds.partial:
        log.warning(
            "%s: found an email but no password (set %s_PASSWORD or PASSWORD_%s). "
            "Falling back to unauthenticated access.",
            service, key, key,
        )
    return creds


def available(service: str) -> bool:
    return for_service(service).complete
