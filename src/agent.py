"""The agentic loop: read the shortlist, decide, draft, queue for sending.

What this automates end to end:

    read stored jobs -> triage with Claude -> shortlist or skip
                     -> draft tailored CV bullets + a cover letter
                     -> save the draft, mark the job `prepared`

What it deliberately stops short of: pressing Submit.

That boundary is not squeamishness, it is the failure mode. Sending an
application is irreversible, goes to a real employer under the user's real name,
and cannot be un-sent. This scorer has already been observed ranking a
"Working Student" role at 77 and a candidate's own HN self-advert at 88 — a
silent auto-submit would have posted the user's name to both, and they would
never have known. A wrong application also costs more than a missed one:
recruiters and ATS systems flag bulk-generic applicants, so volume actively
damages standing with the exact companies worth applying to.

So the agent does every part a machine does better than a person — reading
hundreds of postings, judging fit, writing a first draft — and hands over a
review queue. `main.py apply --assist` then drives the real form in a browser
and stops on the submit button.

Separately: LinkedIn's User Agreement prohibits automated interaction, and the
account at risk is the user's primary professional presence. Easy Apply is not
automated here at any point.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .llm import assess_fit, write_application_kit
from .models import Arrangement, Job, MatchResult, SalaryVerdict, ScoreBreakdown
from .profile import Profile
from .storage import Store

log = logging.getLogger(__name__)

DRAFT_DIR = Path(__file__).resolve().parent.parent / "output" / "applications"

# LLM verdicts that justify preparing an application without asking first.
_GOOD_VERDICTS = ("strong", "worth_applying")


@dataclass
class Decision:
    fingerprint: str
    title: str
    company: str
    score: float
    action: str                    # shortlisted | prepared | skipped
    reason: str
    draft_path: Path | None = None
    apply_url: str = ""


@dataclass
class AgentReport:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    considered: int = 0
    decisions: list[Decision] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def prepared(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "prepared"]

    @property
    def skipped(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "skipped"]

    @property
    def shortlisted(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "shortlisted"]


def _claude_available() -> bool:
    """Whether drafting is possible at all, checked once rather than per job."""
    from .llm import _client

    return _client() is not None


def _row_to_result(row: dict) -> MatchResult:
    """Rebuild enough of a MatchResult for the LLM helpers to work on."""
    import json

    payload = json.loads(row.get("payload_json") or "{}")
    job = Job(
        title=row["title"],
        company=row["company"],
        url=row["url"],
        source=row["source"],
        description=payload.get("description", ""),
        location_raw=row["location_raw"] or "",
        country=row["country"],
        region=row["region"],
        arrangement=Arrangement(row["arrangement"] or "unknown"),
    )
    verdict = row["salary_verdict"] or "unknown"
    return MatchResult(
        job=job,
        score=row["score"] or 0.0,
        breakdown=ScoreBreakdown(total=row["score"] or 0.0),
        matched_skills=[s for s in (row["matched_skills"] or "").split(", ") if s],
        salary_verdict=SalaryVerdict(verdict) if verdict in {v.value for v in SalaryVerdict}
        else SalaryVerdict.UNKNOWN,
        salary_monthly_idr=row["salary_monthly_idr"],
        warnings=[w for w in (row["warnings"] or "").split(" | ") if w],
        # Carry any verdict a previous `search --llm` already stored. Dropping
        # it here silently disables the poor_fit and stretch guards, which are
        # the only checks that can override a misleading keyword score.
        llm_fit=row["llm_fit"] or None,
        llm_summary=row["llm_summary"] or None,
    )


class ApplyAgent:
    def __init__(
        self,
        store: Store | None = None,
        profile: Profile | None = None,
        draft_dir: Path | None = None,
    ):
        self.store = store or Store()
        self.profile = profile or Profile.load()
        self.draft_dir = draft_dir or DRAFT_DIR

    # ------------------------------------------------------------------- run
    def run(
        self,
        min_score: float = 75.0,
        limit: int = 5,
        require_salary_clear: bool = True,
        use_llm: bool = True,
        dry_run: bool = False,
    ) -> AgentReport:
        """Triage the queue and prepare applications for what survives.

        `limit` is a deliberate cap. Preparing 40 applications in one run
        produces 40 drafts nobody reads; a handful per day is what actually
        gets sent.
        """
        report = AgentReport()

        candidates = [
            row
            for row in self.store.list_jobs(limit=500, order="recent")
            # Anything already decided on — by the agent or by hand — is left
            # alone. Re-preparing an applied job is how you double-apply.
            if row["status"] in ("new", "seen", "shortlisted")
            and (row["score"] or 0) >= min_score
        ]
        report.considered = len(candidates)

        if not candidates:
            log.info("Nothing at or above %.0f points is awaiting a decision", min_score)
            return report

        # Best-first, not newest-first. The dashboard defaults to recency
        # because you want to see what just arrived; `limit` here means "prepare
        # the N best", so recency order would let a 91-point role sit unread
        # while five 70-point ones get drafted.
        candidates.sort(key=lambda r: r["score"] or 0, reverse=True)
        results = [_row_to_result(row) for row in candidates]

        # Establish up front whether drafting is even possible. Without this the
        # run reports the same "Claude unavailable" error once per job, which
        # buries the one fact that matters: there is no API key.
        can_draft = _claude_available()
        if not can_draft:
            report.errors.append(
                "Claude is unavailable, so nothing can be drafted — jobs are being "
                "shortlisted instead. Set ANTHROPIC_API_KEY in .env (or run "
                "`ant auth login`) and re-run to get drafts."
            )

        # Claude reads the prose the keyword scorer cannot: a "Lead" title with
        # no team, an "SDET" role describing manual execution.
        if use_llm and can_draft:
            results = assess_fit(results, self.profile, max_jobs=min(len(results), 20))

        consecutive_failures = 0

        for result in results[: limit * 3]:  # room to skip and still fill `limit`
            if len(report.prepared) >= limit:
                break

            decision = self._decide(result, require_salary_clear)

            if decision.action == "prepared" and not dry_run and can_draft:
                try:
                    decision.draft_path = self._draft(result)
                    consecutive_failures = 0
                except Exception as exc:
                    report.errors.append(f"{result.job.title}: {exc}")
                    decision.action = "shortlisted"
                    decision.reason += " (drafting failed — shortlisted instead)"
                    consecutive_failures += 1
                    # Two failures in a row means the cause is the setup, not
                    # this particular job; grinding through the rest just
                    # produces N copies of the same error.
                    if consecutive_failures >= 2:
                        report.errors.append(
                            "Stopping early — drafting failed twice in a row, so the "
                            "problem is the configuration rather than these jobs."
                        )
                        report.decisions.append(decision)
                        if not dry_run:
                            self.store.record_decision(
                                result.job.fingerprint, decision.action,
                                note=decision.reason,
                            )
                        break
            elif decision.action == "prepared" and not can_draft:
                # Honest bookkeeping: without a draft it is a shortlist entry,
                # so do not mark it `prepared` and imply a draft exists.
                decision.action = "shortlisted"
                decision.reason += " (no draft — Claude unavailable)"

            if not dry_run:
                self.store.record_decision(
                    result.job.fingerprint,
                    decision.action,
                    note=decision.reason,
                    draft_path=str(decision.draft_path) if decision.draft_path else None,
                )
            report.decisions.append(decision)

        return report

    # -------------------------------------------------------------- decision
    def _decide(self, result: MatchResult, require_salary_clear: bool) -> Decision:
        """Decide what to do with one job, and be able to say why."""
        job = result.job
        base = dict(
            fingerprint=job.fingerprint,
            title=job.title,
            company=job.company,
            score=result.score,
            apply_url=job.url,
        )

        verdict = (result.llm_fit or "").lower()

        # Claude's read overrides the keyword score. It is the only part of the
        # pipeline that can catch a posting whose title and body disagree.
        if verdict.startswith("[poor_fit"):
            return Decision(**base, action="skipped",
                            reason=f"Claude judged this a poor fit: {result.llm_fit}")
        if verdict.startswith("[stretch"):
            return Decision(**base, action="shortlisted",
                            reason="A stretch — worth your eyes before drafting anything")

        # An Indonesian role with no stated salary cannot be checked against the
        # 30jt floor. Drafting for it invites applying below your own bar.
        if require_salary_clear and result.salary_verdict is SalaryVerdict.UNKNOWN:
            return Decision(**base, action="shortlisted",
                            reason="Salary not stated — confirm it clears IDR 30jt first")

        if not job.description or len(job.description) < 300:
            # Without a description there is nothing to tailor against, and a
            # generic cover letter is worse than none.
            return Decision(**base, action="shortlisted",
                            reason="Posting body too thin to tailor a draft from")

        if result.llm_fit and verdict.startswith(tuple(f"[{v}" for v in _GOOD_VERDICTS)):
            return Decision(**base, action="prepared",
                            reason=f"Claude: {result.llm_fit}")

        return Decision(**base, action="prepared",
                        reason=f"Scored {result.score:.0f} with "
                               f"{len(result.matched_skills)} skill matches")

    # ---------------------------------------------------------------- drafting
    def _draft(self, result: MatchResult) -> Path:
        """Write the tailored CV bullets and cover letter to a file."""
        kit = write_application_kit(result, self.profile)
        if kit is None:
            raise RuntimeError("Claude unavailable — cannot draft application material")

        job = result.job
        self.draft_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", f"{job.company}_{job.title}")[:70].strip("_")
        target = self.draft_dir / f"{slug}_{job.fingerprint[:8]}.md"

        target.write_text(
            f"# {job.title} — {job.company}\n\n"
            f"- **Apply at:** {job.url}\n"
            f"- **Location:** {job.location_raw or 'unstated'} ({job.arrangement.value})\n"
            f"- **Salary:** {job.salary.human()} — {result.salary_verdict.value}\n"
            f"- **Our score:** {result.score:.1f}/100\n"
            f"- **Job id:** `{job.fingerprint}`\n\n"
            + (f"> {result.llm_fit}\n\n" if result.llm_fit else "")
            + ("**Check before sending:** " + "; ".join(result.warnings) + "\n\n"
               if result.warnings else "")
            + f"## CV bullets\n\n{kit['cv_bullets']}\n\n"
            f"## Cover letter\n\n{kit['cover_letter']}\n\n"
            "---\n\n"
            "*Drafted automatically. Read it before sending — it is written from "
            "your profile, but only you know whether it is true today.*\n"
            f"\nWhen sent: `python main.py status {job.fingerprint[:8]} applied`\n",
            encoding="utf-8",
        )
        return target


def assisted_apply(store: Store, profile: Profile, fingerprint: str,
                   headless: bool = False) -> int:
    """Open a job's application form in a browser with fields pre-filled.

    This is the "apply for me" half that can be automated honestly: the browser
    navigates, fills what it can read from the profile, and then stops. You read
    the form and press Submit.

    It fills nothing it has to guess at. A wrong phone number or a fabricated
    answer to "why do you want to work here" is worse than an empty field.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Assisted apply needs Playwright:  pip install playwright && playwright install chromium")
        return 1

    row = next(
        (r for r in store.list_jobs(limit=10_000) if r["fingerprint"].startswith(fingerprint)),
        None,
    )
    if row is None:
        print(f"No stored job with id starting '{fingerprint}'.")
        return 1

    if "linkedin.com" in (row["url"] or ""):
        # Not a technical limitation. LinkedIn's User Agreement prohibits
        # automated interaction, and the account at stake is the user's main
        # professional presence.
        print(f"\n{row['title']} — {row['company']}")
        print(f"\nThis one is on LinkedIn, so I will not drive the form: their terms")
        print("prohibit automated interaction and the risk lands on your account.")
        print(f"\nOpen it yourself:  {row['url']}")
        if row["draft_path"]:
            print(f"Your draft:        {row['draft_path']}")
        return 0

    ident = profile.identity
    print(f"\nOpening the form for: {row['title']} — {row['company']}")
    if row["draft_path"]:
        print(f"Draft to paste from:  {row['draft_path']}")
    print("\nI will fill the fields I can read from your profile, then stop.")
    print("Review everything, attach your CV, and press Submit yourself.\n")

    # Field labels vary per ATS; match on several spellings and skip misses.
    fills = [
        (["input[name*='first_name' i]", "input[id*='first_name' i]"],
         ident.get("name", "").split()[0] if ident.get("name") else ""),
        (["input[name*='last_name' i]", "input[id*='last_name' i]"],
         " ".join(ident.get("name", "").split()[1:])),
        (["input[name*='name' i]:not([name*='first' i]):not([name*='last' i])"],
         ident.get("name", "")),
        (["input[type='email']", "input[name*='email' i]"], ident.get("email", "")),
        (["input[type='tel']", "input[name*='phone' i]"], ident.get("phone", "")),
        (["input[name*='linkedin' i]", "input[id*='linkedin' i]"], ident.get("linkedin", "")),
        (["input[name*='website' i]", "input[name*='portfolio' i]"], ident.get("portfolio", "")),
        (["input[name*='location' i]", "input[name*='city' i]"], ident.get("location", "")),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_context(viewport={"width": 1440, "height": 960}).new_page()
        try:
            page.goto(row["url"], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)

            filled: list[str] = []
            for selectors, value in fills:
                if not value:
                    continue
                for selector in selectors:
                    try:
                        element = page.query_selector(selector)
                        if element and element.is_visible() and not element.input_value():
                            element.fill(value)
                            filled.append(selector.split("[")[0] or selector)
                            break
                    except Exception:
                        continue

            print(f"Pre-filled {len(filled)} field(s). The browser stays open — it is yours now.")
            print("Close the window when you are done.\n")
            if not headless:
                # Block until the user closes the browser. Nothing is submitted
                # from here; there is no click on any submit button anywhere.
                page.wait_for_event("close", timeout=0)
        except Exception as exc:
            log.warning("assisted apply: %s", exc)
            print(f"\nCould not drive that page ({exc}).\nOpen it manually: {row['url']}")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    print(f"When you have sent it:  python main.py status {row['fingerprint'][:8]} applied")
    return 0
