"""SQLite persistence.

Two things this buys you that a pile of JSON files does not:

1. **Cross-run dedupe.** A posting seen yesterday is not "new" today, so daily
   runs can report only what changed instead of re-reading the same 40 jobs.
2. **Application tracking.** Mark a job applied/skipped and it stays marked,
   so the agent stops resurfacing roles you have already decided about.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import MatchResult

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint        TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    company            TEXT NOT NULL,
    url                TEXT NOT NULL,
    source             TEXT NOT NULL,
    location_raw       TEXT,
    country            TEXT,
    region             TEXT,
    arrangement        TEXT,
    salary_human       TEXT,
    salary_monthly_idr REAL,
    salary_verdict     TEXT,
    score              REAL,
    breakdown_json     TEXT,
    matched_skills     TEXT,
    warnings           TEXT,
    llm_summary        TEXT,
    llm_fit            TEXT,
    posted_at          TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    seen_count         INTEGER NOT NULL DEFAULT 1,
    status             TEXT NOT NULL DEFAULT 'new',
    notes              TEXT,
    payload_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_region ON jobs(region);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    sources       TEXT,
    scraped_count INTEGER DEFAULT 0,
    matched_count INTEGER DEFAULT 0,
    new_count     INTEGER DEFAULT 0,
    errors        TEXT
);
"""

# Statuses the CLI can set. 'new' and 'seen' are managed automatically.
VALID_STATUSES = {"new", "seen", "shortlisted", "applied", "interviewing",
                  "rejected", "skipped"}


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ upsert
    def upsert(self, result: MatchResult) -> bool:
        """Insert or refresh a match. Returns True if this job is newly seen.

        A re-seen job keeps its `first_seen_at`, `status` and `notes` — those
        are yours — but takes the fresh score and salary read, since the
        posting itself may have been edited.
        """
        job = result.job
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT fingerprint, status FROM jobs WHERE fingerprint = ?",
                (job.fingerprint,),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE jobs SET
                        last_seen_at = ?, seen_count = seen_count + 1,
                        score = ?, breakdown_json = ?, matched_skills = ?,
                        warnings = ?, salary_human = ?, salary_monthly_idr = ?,
                        salary_verdict = ?, llm_summary = COALESCE(?, llm_summary),
                        llm_fit = COALESCE(?, llm_fit), payload_json = ?,
                        status = CASE WHEN status = 'new' THEN 'seen' ELSE status END
                    WHERE fingerprint = ?
                    """,
                    (
                        now,
                        result.score,
                        json.dumps(result.breakdown.as_dict()),
                        ", ".join(result.matched_skills),
                        " | ".join(result.warnings),
                        job.salary.human(),
                        result.salary_monthly_idr,
                        result.salary_verdict.value,
                        result.llm_summary,
                        result.llm_fit,
                        json.dumps(result.to_dict(), default=str),
                        job.fingerprint,
                    ),
                )
                return False

            conn.execute(
                """
                INSERT INTO jobs (
                    fingerprint, title, company, url, source, location_raw,
                    country, region, arrangement, salary_human,
                    salary_monthly_idr, salary_verdict, score, breakdown_json,
                    matched_skills, warnings, llm_summary, llm_fit, posted_at,
                    first_seen_at, last_seen_at, seen_count, status, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'new',?)
                """,
                (
                    job.fingerprint, job.title, job.company, job.url, job.source,
                    job.location_raw, job.country, job.region,
                    job.arrangement.value, job.salary.human(),
                    result.salary_monthly_idr, result.salary_verdict.value,
                    result.score, json.dumps(result.breakdown.as_dict()),
                    ", ".join(result.matched_skills), " | ".join(result.warnings),
                    result.llm_summary, result.llm_fit,
                    job.posted_at.isoformat() if job.posted_at else None,
                    now, now, json.dumps(result.to_dict(), default=str),
                ),
            )
            return True

    def save_all(self, results: list[MatchResult]) -> list[MatchResult]:
        """Persist every result and return the subset that is new to the DB."""
        return [r for r in results if self.upsert(r)]

    # ------------------------------------------------------------------ reads
    def known_fingerprints(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT fingerprint FROM jobs").fetchall()
        return {r["fingerprint"] for r in rows}

    def list_jobs(
        self,
        status: str | None = None,
        region: str | None = None,
        min_score: float | None = None,
        limit: int = 50,
        order: str = "score",
    ) -> list[dict[str, Any]]:
        """Read stored jobs.

        `order` matters once you have more jobs than `limit`: "recent" returns
        the most recently scraped, "score" the highest ranked. Ordering in SQL
        rather than after the fact means the cut keeps the rows you asked for.
        """
        order_sql = {
            "recent": "first_seen_at DESC, score DESC",
            "score": "score DESC",
        }.get(order, "score DESC")

        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if region:
            clauses.append("region = ?")
            params.append(region)
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY {order_sql} LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, fingerprint: str, status: str, notes: str | None = None) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"Unknown status '{status}'. Use one of: {sorted(VALID_STATUSES)}")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, notes = COALESCE(?, notes) WHERE fingerprint = ?",
                (status, notes, fingerprint),
            )
        return cursor.rowcount > 0

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            by_status = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
                ).fetchall()
            }
            by_region = {
                (r["region"] or "unknown"): r["n"]
                for r in conn.execute(
                    "SELECT region, COUNT(*) AS n FROM jobs GROUP BY region"
                ).fetchall()
            }
            top = conn.execute(
                "SELECT title, company, score FROM jobs ORDER BY score DESC LIMIT 5"
            ).fetchall()
        return {
            "total": total,
            "by_status": by_status,
            "by_region": by_region,
            "top": [dict(r) for r in top],
        }

    # -------------------------------------------------------------- run ledger
    def start_run(self, sources: list[str]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (started_at, sources) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), ", ".join(sources)),
            )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        scraped: int,
        matched: int,
        new: int,
        errors: list[str] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET finished_at = ?, scraped_count = ?,
                    matched_count = ?, new_count = ?, errors = ?
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    scraped, matched, new,
                    " | ".join(errors or []),
                    run_id,
                ),
            )
