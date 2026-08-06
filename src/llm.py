"""Optional Claude enrichment: fit verdicts, tailored CV bullets, cover letters.

Everything here is optional. With no `ANTHROPIC_API_KEY` the pipeline runs
end-to-end on the deterministic scorer alone — the LLM adds judgement on top of
a ranking that already exists, it does not produce the ranking.

Why that split matters: a scoring engine you can read and unit-test is what
makes the results trustworthy. The model is used where it is genuinely better
than rules — reading a wall of prose and saying "this is a manual-QA role
wearing an SDET title" — and nowhere else.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .models import MatchResult
from .profile import Profile

log = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

# Batched fit assessment. One request covers many jobs, which keeps cost
# proportional to the run rather than to the job count.
_FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from the input list"},
                    "verdict": {
                        "type": "string",
                        "enum": ["strong", "worth_applying", "stretch", "poor_fit"],
                    },
                    "summary": {
                        "type": "string",
                        "description": "One sentence on what the role actually is.",
                    },
                    "fit": {
                        "type": "string",
                        "description": (
                            "Two or three sentences: why this fits the candidate "
                            "or does not, naming specific evidence from the posting."
                        ),
                    },
                    "red_flags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "verdict", "summary", "fit", "red_flags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assessments"],
    "additionalProperties": False,
}


def _client():
    """Return an Anthropic client, or None when the SDK/key is unavailable."""
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; skipping LLM enrichment")
        return None

    # A bare constructor also picks up an `ant auth login` profile, so an unset
    # ANTHROPIC_API_KEY does not by itself mean there are no credentials.
    try:
        return anthropic.Anthropic()
    except Exception as exc:
        log.info("Anthropic client unavailable: %s", exc)
        return None


def _profile_brief(profile: Profile) -> str:
    ident = profile.identity
    return (
        f"Name: {ident.get('name')}\n"
        f"Current headline: {ident.get('headline')}\n"
        f"Based in: {ident.get('location')} ({ident.get('timezone')})\n"
        f"Experience: {ident.get('years_experience')}+ years in QA / test engineering\n"
        f"Target titles: {', '.join(profile.primary_titles)}\n"
        f"Core tooling: {', '.join(profile.core_skills)}\n"
        f"Domains shipped in: {', '.join(profile.domains)}\n"
        f"Seniority floor: {profile.seniority.get('min_level')}\n"
        f"Work preference: remote-first; onsite only in "
        f"{', '.join(profile.work_preference.get('onsite_countries', []))}\n"
        "Compensation rule: Indonesian roles must clear IDR 30,000,000/month. "
        "Asia and Europe roles have no salary floor."
    )


SYSTEM_PROMPT = """You assess job postings for one specific engineer.

You are given their profile and a numbered list of postings that already passed a \
deterministic scoring filter. For each posting, say what the role actually is and \
whether it is worth this person's time.

Be direct and specific. Cite evidence from the posting — a tool named, a \
responsibility described, a seniority signal. Generic encouragement is useless to \
someone deciding where to spend an evening writing an application.

Watch for these mismatches, which the keyword scorer cannot catch:
- A title saying "SDET" or "automation" over a body describing manual test execution.
- "Lead" in the title with no team, mentoring, or strategy scope in the body.
- Roles that are really full-stack development with testing attached.
- Postings that restrict to a timezone or country incompatible with Asia/Jakarta.
- Contract or staffing-agency listings presented as permanent roles.

verdict meanings:
  strong          - matches their level and stack; apply.
  worth_applying  - good overlap with one real gap; apply if interested.
  stretch         - a reach, or a partial mismatch; apply only if the company appeals.
  poor_fit        - the scorer was fooled; explain what gave it away."""


def assess_fit(
    results: list[MatchResult],
    profile: Profile,
    max_jobs: int = 20,
    description_chars: int = 2500,
) -> list[MatchResult]:
    """Attach `llm_summary` / `llm_fit` to the top `max_jobs` results.

    Mutates and returns the same list. Failures are logged and swallowed —
    losing the commentary is not worth losing the run.
    """
    client = _client()
    if client is None or not results:
        return results

    batch = results[:max_jobs]
    payload = [
        {
            "index": i,
            "title": r.job.title,
            "company": r.job.company,
            "location": r.job.location_raw or "unspecified",
            "arrangement": r.job.arrangement.value,
            "salary": r.job.salary.human(),
            "our_score": r.score,
            "matched_skills": r.matched_skills[:12],
            "description": r.job.description[:description_chars],
        }
        for i, r in enumerate(batch)
    ]

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _FIT_SCHEMA},
            },
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # The system prompt and profile are identical every run;
                    # caching them makes repeat runs materially cheaper.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"CANDIDATE PROFILE\n{_profile_brief(profile)}\n\n"
                        f"POSTINGS\n{json.dumps(payload, ensure_ascii=False)}"
                    ),
                }
            ],
        )
    except Exception as exc:
        log.warning("LLM fit assessment failed: %s", exc)
        return results

    if response.stop_reason == "refusal":
        log.warning("LLM declined the fit assessment request")
        return results

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned unparseable JSON for fit assessment")
        return results

    for item in parsed.get("assessments", []):
        idx = item.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(batch):
            continue
        result = batch[idx]
        result.llm_summary = item.get("summary")
        verdict = item.get("verdict", "")
        result.llm_fit = f"[{verdict}] {item.get('fit', '')}".strip()
        for flag in item.get("red_flags", []):
            if flag:
                result.warnings.append(f"LLM: {flag}")
        # A model verdict of poor_fit is worth more than a keyword score;
        # demote it so it stops occupying the top of the report.
        if verdict == "poor_fit":
            result.score = round(result.score * 0.6, 1)

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def write_application_kit(result: MatchResult, profile: Profile) -> dict[str, str] | None:
    """Draft tailored CV bullets and a cover letter for one specific job.

    Returns `{"cv_bullets": ..., "cover_letter": ...}`, or None if the model is
    unavailable. Everything it writes must be grounded in the profile — the
    prompt forbids inventing experience, because a fabricated bullet is worse
    than no bullet in an interview.
    """
    client = _client()
    if client is None:
        return None

    job = result.job
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=(
                "You write job application material for one engineer.\n\n"
                "Hard rule: use only experience present in the profile. Never invent "
                "a tool, employer, metric, or certification. If the posting asks for "
                "something the candidate lacks, either omit it or address the gap "
                "honestly — do not paper over it.\n\n"
                "Write plainly. No 'passionate', no 'dynamic', no 'I am writing to "
                "express my interest'. A hiring manager should be able to skim it in "
                "twenty seconds and know why this person is relevant."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"CANDIDATE PROFILE\n{_profile_brief(profile)}\n\n"
                        f"CAREER HISTORY\n{_history_brief()}\n\n"
                        f"TARGET ROLE\n"
                        f"{job.title} at {job.company} ({job.location_raw})\n"
                        f"{job.description[:6000]}\n\n"
                        "Produce two sections separated by the line '---':\n"
                        "1. Five CV bullets rewritten to target this posting. Each "
                        "bullet: what was done, with what, to what effect.\n"
                        "2. A cover letter of at most 200 words."
                    ),
                }
            ],
        )
    except Exception as exc:
        log.warning("Application kit generation failed: %s", exc)
        return None

    if response.stop_reason == "refusal":
        return None

    text = next((b.text for b in response.content if b.type == "text"), "")
    bullets, _, letter = text.partition("---")
    return {"cv_bullets": bullets.strip(), "cover_letter": letter.strip() or text.strip()}


def _history_brief() -> str:
    """Career history, kept here so the LLM prompt has grounding facts.

    Sourced from the CV. Update alongside profile.yaml when roles change.
    """
    return (
        "Hubexo — QA Automation Lead (Feb 2024–present, Jakarta): leads a team of "
        "QA automation engineers; owns the Cypress framework; CI/CD on Azure DevOps "
        "and Bitbucket Pipelines; automation strategy and coverage.\n"
        "Nikel — Sr. QA Engineer (Oct 2023–Feb 2024): API automation in RestAssured/Java, "
        "web automation in Selenium/Java, GCP data-pipeline testing, BigQuery SQL "
        "validation, load and stress testing with JMeter.\n"
        "Hijra — Senior SDET (Sep 2022–Sep 2023): core banking; RestAssured API "
        "automation, WebdriverIO UI automation, k6 load testing, TestRail, Agile.\n"
        "SiCepat Ekspres — Senior Staff IT QA Engineer (Jan 2022–Sep 2022): led QA for "
        "the superapp tribe; Serenity/JS automation; k6 load testing; UAT and regression.\n"
        "Qualysoft — Senior Software QA Engineer (Jun 2021–Jan 2022): API and UI "
        "automation; Bitbucket and Jenkins integration testing.\n"
        "RCTI+ — SPV QA Specialist (Jun 2020–May 2021): Robot Framework web automation, "
        "Appium mobile automation, Postman API testing, k6 load testing, all platforms.\n"
        "Asian Technology Solutions — QA Automation Engineer (Dec 2019–Jun 2020): IFRS17 "
        "at Prudential Life; Greenplum/LifeAsia data extraction and validation.\n"
        "Mitra Integrasi Informatika — Technical Consultant Analyst (Oct 2018–Nov 2019).\n"
        "Also: QA mentor at Dibimbing.id; currently exploring AI agents for test "
        "creation and maintenance."
    )
