"""Output writers: terminal, Markdown, JSON, CSV, HTML.

The Markdown report is the one to read daily — it groups by region so the
Indonesian salary-floor picture stays separate from the Asia/Europe picture,
where salary is not a filter at all.
"""

from __future__ import annotations

import csv
import html as html_escape
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import MatchResult, SalaryVerdict
from .pipeline import RunSummary

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

_VERDICT_LABEL = {
    SalaryVerdict.PASS: "meets IDR 30jt floor",
    SalaryVerdict.UNKNOWN: "salary not stated — verify",
    SalaryVerdict.NOT_ENFORCED: "no floor for this region",
    SalaryVerdict.FAIL: "below floor",
}

_REGION_TITLE = {
    "indonesia": "Indonesia — IDR 30,000,000/month floor enforced",
    "asia": "Asia (excl. Indonesia) — no salary floor",
    "europe": "Europe — no salary floor",
    "fallback": "Elsewhere — remote-only",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _group_by_region(results: list[MatchResult]) -> dict[str, list[MatchResult]]:
    grouped: dict[str, list[MatchResult]] = {}
    for result in results:
        grouped.setdefault(result.job.region or "fallback", []).append(result)
    # Keep the profile's region order, with anything unexpected last.
    order = ["indonesia", "asia", "europe", "fallback"]
    return {k: grouped[k] for k in order if k in grouped} | {
        k: v for k, v in grouped.items() if k not in order
    }


# ------------------------------------------------------------------- terminal
def print_summary(summary: RunSummary, top: int = 15) -> None:
    """Human-readable run digest for the console."""
    print()
    print("=" * 78)
    print(f"  Job search run — {summary.started_at.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 78)
    print(f"  sources    : {', '.join(summary.sources_run) or 'none'}")
    print(f"  scraped    : {summary.scraped} postings ({summary.after_dedupe} unique)")
    print(f"  matched    : {summary.matched} above threshold")
    print(f"  new to you : {summary.new}")
    print(f"  duration   : {summary.duration_seconds:.1f}s")

    if summary.per_source:
        print("\n  per source:")
        for name, count in sorted(summary.per_source.items(), key=lambda kv: -kv[1]):
            print(f"    {name:<18} {count:>4}")

    if summary.errors:
        print(f"\n  {len(summary.errors)} source error(s):")
        for err in summary.errors:
            print(f"    ! {err}")

    if not summary.results:
        print("\n  No matches. Loosen scoring.min_score in config/profile.yaml,")
        print("  or add more companies under greenhouse/lever in config/sources.yaml.")
        print()
        return

    new_fingerprints = {r.job.fingerprint for r in summary.new_results}

    for region, results in _group_by_region(summary.results).items():
        print()
        print("-" * 78)
        print(f"  {_REGION_TITLE.get(region, region.title())}  ({len(results)})")
        print("-" * 78)

        for result in results[:top]:
            job = result.job
            marker = "NEW " if job.fingerprint in new_fingerprints else "    "
            print(f"\n  {marker}[{result.score:5.1f}] {job.title}")
            print(f"        {job.company} · {job.location_raw or 'location unstated'} · {job.arrangement.value}")
            print(f"        salary: {job.salary.human()} ({_VERDICT_LABEL[result.salary_verdict]})")
            if result.salary_monthly_idr:
                print(f"                ≈ IDR {result.salary_monthly_idr:,.0f}/month")
            if result.matched_skills:
                print(f"        skills: {', '.join(result.matched_skills[:10])}")
            if result.llm_fit:
                print(f"        fit   : {result.llm_fit}")
            for warning in result.warnings[:3]:
                print(f"        warn  : {warning}")
            print(f"        {job.url}")

        if len(results) > top:
            print(f"\n    … and {len(results) - top} more in the full report")
    print()


# ------------------------------------------------------------------- markdown
def write_markdown(summary: RunSummary, path: str | Path | None = None) -> Path:
    target = Path(path) if path else OUTPUT_DIR / f"jobs_{_timestamp()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    new_fingerprints = {r.job.fingerprint for r in summary.new_results}

    lines: list[str] = [
        "# Job search report",
        "",
        f"*Generated {summary.started_at.strftime('%Y-%m-%d %H:%M UTC')} "
        f"in {summary.duration_seconds:.1f}s*",
        "",
        "| | |",
        "|---|---|",
        f"| Sources | {', '.join(summary.sources_run) or '—'} |",
        f"| Postings scraped | {summary.scraped} ({summary.after_dedupe} unique) |",
        f"| Above threshold | {summary.matched} |",
        f"| New since last run | {summary.new} |",
        "",
    ]

    if summary.errors:
        lines += ["## Source errors", ""]
        lines += [f"- `{err}`" for err in summary.errors]
        lines += [""]

    if not summary.results:
        lines += ["No matches this run.", ""]
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    lines += ["## Matches", ""]

    for region, results in _group_by_region(summary.results).items():
        lines += [f"### {_REGION_TITLE.get(region, region.title())}", ""]
        lines += [
            "| | Score | Role | Company | Location | Setup | Salary | Source |",
            "|---|---:|---|---|---|---|---|---|",
        ]
        for result in results:
            job = result.job
            flag = "🆕" if job.fingerprint in new_fingerprints else ""
            salary = job.salary.human()
            if result.salary_verdict is SalaryVerdict.UNKNOWN:
                salary = f"{salary} ⚠️"
            lines.append(
                f"| {flag} | {result.score:.0f} | [{job.title}]({job.url}) | {job.company} "
                f"| {job.location_raw or '—'} | {job.arrangement.value} | {salary} | {job.source} |"
            )
        lines += [""]

        # Detail blocks for the strongest matches in this region.
        for result in results[:10]:
            job = result.job
            lines += [f"#### {job.title} — {job.company}  ·  {result.score:.1f}/100", ""]
            if result.llm_summary:
                lines += [f"> {result.llm_summary}", ""]
            lines += [
                f"- **Link:** {job.url}",
                f"- **Location / setup:** {job.location_raw or 'unstated'} · {job.arrangement.value}",
                f"- **Salary:** {job.salary.human()} — {_VERDICT_LABEL[result.salary_verdict]}",
            ]
            if result.salary_monthly_idr:
                lines.append(f"- **Normalised:** ≈ IDR {result.salary_monthly_idr:,.0f}/month")
            if job.posted_at:
                age = job.age_days
                lines.append(
                    f"- **Posted:** {job.posted_at.strftime('%Y-%m-%d')}"
                    + (f" ({age:.0f} days ago)" if age is not None else "")
                )
            if result.matched_skills:
                lines.append(f"- **Skill hits:** {', '.join(result.matched_skills)}")
            if result.missing_core_skills:
                lines.append(f"- **Not mentioned:** {', '.join(result.missing_core_skills[:8])}")
            if result.llm_fit:
                lines.append(f"- **Assessment:** {result.llm_fit}")
            if result.reasons:
                lines.append(f"- **Why it scored:** {'; '.join(result.reasons)}")
            if result.warnings:
                lines.append(f"- **Watch out:** {'; '.join(result.warnings)}")

            b = result.breakdown.as_dict()
            lines += [
                "",
                "<details><summary>Score breakdown</summary>",
                "",
                "| Dimension | Score |",
                "|---|---:|",
                *[
                    f"| {k} | {v} |"
                    for k, v in b.items()
                    if k != "total"
                ],
                "",
                "</details>",
                "",
            ]

    target.write_text("\n".join(lines), encoding="utf-8")
    return target


# ----------------------------------------------------------------------- json
def write_json(summary: RunSummary, path: str | Path | None = None) -> Path:
    target = Path(path) if path else OUTPUT_DIR / f"jobs_{_timestamp()}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": summary.started_at.isoformat(),
        "duration_seconds": round(summary.duration_seconds, 2),
        "sources": summary.sources_run,
        "counts": {
            "scraped": summary.scraped,
            "unique": summary.after_dedupe,
            "matched": summary.matched,
            "new": summary.new,
        },
        "per_source": summary.per_source,
        "errors": summary.errors,
        "new_fingerprints": [r.job.fingerprint for r in summary.new_results],
        "matches": [r.to_dict() for r in summary.results],
    }
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return target


# ------------------------------------------------------------------------ csv
def write_csv(summary: RunSummary, path: str | Path | None = None) -> Path:
    target = Path(path) if path else OUTPUT_DIR / f"jobs_{_timestamp()}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)

    columns = [
        "score", "title", "company", "region", "country", "location_raw",
        "arrangement", "salary_human", "salary_monthly_idr", "salary_verdict",
        "matched_skills", "warnings", "posted_at", "source", "url",
    ]
    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for result in summary.results:
            row = result.to_dict()
            writer.writerow(
                [
                    row.get("score"), row.get("title"), row.get("company"),
                    row.get("region"), row.get("country"), row.get("location_raw"),
                    row.get("arrangement"), row.get("salary_human"),
                    row.get("salary_monthly_idr"), row.get("salary_verdict"),
                    "; ".join(row.get("matched_skills") or []),
                    "; ".join(row.get("warnings") or []),
                    row.get("posted_at"), row.get("source"), row.get("url"),
                ]
            )
    return target


# ----------------------------------------------------------------------- html
def write_html(summary: RunSummary, path: str | Path | None = None) -> Path:
    """A single self-contained page — openable from a phone, no build step."""
    target = Path(path) if path else OUTPUT_DIR / f"jobs_{_timestamp()}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    e = html_escape.escape
    new_fingerprints = {r.job.fingerprint for r in summary.new_results}

    cards: list[str] = []
    for region, results in _group_by_region(summary.results).items():
        cards.append(
            f'<h2>{e(_REGION_TITLE.get(region, region.title()))} '
            f'<span class="count">{len(results)}</span></h2>'
        )
        for result in results:
            job = result.job
            badge = '<span class="new">NEW</span>' if job.fingerprint in new_fingerprints else ""
            warn = (
                '<p class="warn">' + "<br>".join(e(w) for w in result.warnings) + "</p>"
                if result.warnings else ""
            )
            fit = f'<p class="fit">{e(result.llm_fit)}</p>' if result.llm_fit else ""
            skills = "".join(
                f'<span class="tag">{e(s)}</span>' for s in result.matched_skills[:12]
            )
            cards.append(
                f"""
  <article>
    <div class="row">
      <span class="score">{result.score:.0f}</span>
      <div>
        <h3><a href="{e(job.url)}" target="_blank" rel="noopener">{e(job.title)}</a> {badge}</h3>
        <p class="meta">{e(job.company)} · {e(job.location_raw or 'location unstated')}
           · {e(job.arrangement.value)} · via {e(job.source)}</p>
        <p class="salary">{e(job.salary.human())}
           <em>{e(_VERDICT_LABEL[result.salary_verdict])}</em></p>
        <div class="tags">{skills}</div>
        {fit}{warn}
      </div>
    </div>
  </article>"""
            )

    body = "\n".join(cards) or "<p>No matches this run.</p>"
    target.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job search — {summary.started_at.strftime('%Y-%m-%d')}</title>
<style>
  :root {{
    --bg:#fbfaf8; --fg:#1c1b19; --muted:#6b6862; --line:#e4e0d8;
    --accent:#0f766e; --warnfg:#92400e; --warnbg:#fef3c7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#161513; --fg:#eceae6; --muted:#9a958c; --line:#302e2a;
             --accent:#5eead4; --warnfg:#fcd34d; --warnbg:#3a2f10; }}
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; padding:2rem 1rem 4rem; background:var(--bg); color:var(--fg);
    font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:60rem; margin:0 auto }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem }}
  .sub {{ color:var(--muted); margin:0 0 2rem }}
  h2 {{ font-size:1.05rem; text-transform:uppercase; letter-spacing:.06em;
    color:var(--muted); margin:2.5rem 0 .75rem; padding-bottom:.4rem;
    border-bottom:1px solid var(--line); }}
  .count {{ background:var(--line); color:var(--fg); border-radius:1rem;
    padding:.05rem .5rem; font-size:.8rem; }}
  article {{ border:1px solid var(--line); border-radius:.6rem; padding:1rem;
    margin-bottom:.75rem; background:color-mix(in srgb, var(--bg) 90%, var(--fg)); }}
  .row {{ display:flex; gap:1rem; align-items:flex-start }}
  .score {{ flex:0 0 3rem; height:3rem; display:grid; place-items:center;
    border-radius:.5rem; background:var(--accent); color:var(--bg);
    font-weight:700; font-variant-numeric:tabular-nums; }}
  h3 {{ margin:0 0 .2rem; font-size:1.05rem }}
  h3 a {{ color:var(--fg); text-decoration:none }}
  h3 a:hover {{ color:var(--accent); text-decoration:underline }}
  .new {{ background:var(--accent); color:var(--bg); font-size:.65rem;
    padding:.1rem .35rem; border-radius:.25rem; vertical-align:middle; }}
  .meta,.salary {{ margin:.15rem 0; color:var(--muted); font-size:.9rem }}
  .salary em {{ font-style:normal; opacity:.75 }}
  .tags {{ margin:.5rem 0 0 }}
  .tag {{ display:inline-block; border:1px solid var(--line); border-radius:.25rem;
    padding:.05rem .4rem; margin:.15rem .25rem .15rem 0; font-size:.75rem;
    color:var(--muted); }}
  .fit {{ font-size:.9rem; margin:.6rem 0 0 }}
  .warn {{ background:var(--warnbg); color:var(--warnfg); font-size:.85rem;
    padding:.4rem .6rem; border-radius:.35rem; margin:.6rem 0 0 }}
</style></head>
<body><main>
  <h1>Job search report</h1>
  <p class="sub">{summary.started_at.strftime('%Y-%m-%d %H:%M UTC')} ·
    {summary.matched} matches from {summary.scraped} postings ·
    {summary.new} new · sources: {e(', '.join(summary.sources_run))}</p>
{body}
</main></body></html>
""",
        encoding="utf-8",
    )
    return target


def write_all(summary: RunSummary, formats: list[str]) -> dict[str, Path]:
    """Write every requested format; returns {format: path}."""
    writers = {
        "markdown": write_markdown,
        "md": write_markdown,
        "json": write_json,
        "csv": write_csv,
        "html": write_html,
    }
    written: dict[str, Path] = {}
    for fmt in formats:
        writer = writers.get(fmt.lower())
        if writer is None:
            continue
        written[fmt] = writer(summary)
    return written
