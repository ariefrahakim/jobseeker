#!/usr/bin/env python3
"""jobseeker — a profile-driven job search agent.

    python main.py search                     # run every enabled source
    python main.py search --llm               # add Claude fit assessments
    python main.py search --sources greenhouse lever
    python main.py list --status new
    python main.py apply <fingerprint>        # draft CV bullets + cover letter
    python main.py status <fingerprint> applied
    python main.py stats
    python main.py sources
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is a convenience, not a requirement
    pass

from src import report
from src.llm import write_application_kit
from src.pipeline import Pipeline
from src.profile import Profile, SourceConfig
from src.scrapers import REGISTRY
from src.storage import VALID_STATUSES, Store


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Requests/urllib3 chatter drowns out our own logs at DEBUG.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------- search
def cmd_search(args: argparse.Namespace) -> int:
    profile = Profile.load(args.profile)
    sources = SourceConfig.load(args.sources_config)
    store = Store(args.db)

    pipeline = Pipeline(profile=profile, sources=sources, store=store)
    summary = pipeline.run(
        only_sources=args.sources,
        use_llm=args.llm,
        llm_max_jobs=args.llm_max,
        max_workers=args.workers,
        min_score=args.min_score,
    )

    report.print_summary(summary, top=args.top)

    if args.format:
        written = report.write_all(summary, args.format)
        if written:
            print("  reports written:")
            for fmt, path in written.items():
                print(f"    {fmt:<9} {path}")
            print()

    # Non-zero only when every source failed — a partial run is still useful.
    if summary.errors and summary.scraped == 0:
        return 1
    return 0


# ----------------------------------------------------------------------- list
def cmd_list(args: argparse.Namespace) -> int:
    store = Store(args.db)
    rows = store.list_jobs(
        status=args.status,
        region=args.region,
        min_score=args.min_score,
        limit=args.limit,
    )
    if not rows:
        print("No stored jobs match that filter.")
        return 0

    print(f"\n{len(rows)} job(s):\n")
    for row in rows:
        print(f"  [{row['score'] or 0:5.1f}] {row['title']}")
        print(f"          {row['company']} · {row['location_raw'] or '—'} · {row['arrangement']}")
        print(f"          {row['salary_human']} ({row['salary_verdict']})")
        print(f"          status={row['status']}  id={row['fingerprint']}")
        print(f"          {row['url']}\n")
    return 0


# ---------------------------------------------------------------------- apply
def cmd_apply(args: argparse.Namespace) -> int:
    """Draft tailored application material for one stored job."""
    import json

    from src.models import Arrangement, Job, MatchResult, ScoreBreakdown

    store = Store(args.db)
    rows = store.list_jobs(limit=10_000)
    row = next((r for r in rows if r["fingerprint"].startswith(args.fingerprint)), None)
    if row is None:
        print(f"No stored job with id starting '{args.fingerprint}'. Try `list` first.")
        return 1

    payload = json.loads(row["payload_json"] or "{}")
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
    result = MatchResult(job=job, score=row["score"] or 0, breakdown=ScoreBreakdown())

    profile = Profile.load(args.profile)
    print(f"\nDrafting application material for: {job.title} @ {job.company}\n")
    kit = write_application_kit(result, profile)
    if kit is None:
        print("Claude is unavailable — set ANTHROPIC_API_KEY (or run `ant auth login`)")
        print("and install the SDK: pip install anthropic")
        return 1

    print("=" * 78)
    print("  CV BULLETS")
    print("=" * 78)
    print(kit["cv_bullets"])
    print()
    print("=" * 78)
    print("  COVER LETTER")
    print("=" * 78)
    print(kit["cover_letter"])
    print()

    out_dir = Path("output/applications")
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "_" for c in f"{job.company}_{job.title}")[:80]
    target = out_dir / f"{slug}.md"
    target.write_text(
        f"# {job.title} — {job.company}\n\n{job.url}\n\n"
        f"## CV bullets\n\n{kit['cv_bullets']}\n\n"
        f"## Cover letter\n\n{kit['cover_letter']}\n",
        encoding="utf-8",
    )
    print(f"Saved to {target}\n")
    return 0


# --------------------------------------------------------------------- status
def cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.db)
    if store.set_status(args.fingerprint, args.new_status, args.note):
        print(f"{args.fingerprint} → {args.new_status}")
        return 0
    print(f"No job with id '{args.fingerprint}'.")
    return 1


# ------------------------------------------------------------------ dashboard
def cmd_dashboard(args: argparse.Namespace) -> int:
    from src import dashboard

    store = Store(args.db)
    if store.stats()["total"] == 0:
        print("Nothing to show yet — run `python main.py search` first.")
        return 1

    target = dashboard.build(store, path=args.out, limit=args.limit,
                             per_page=args.per_page)
    print(f"\nDashboard: {target}")
    print("Open it in a browser, or send the file to your phone.\n")

    if args.open_it:
        import webbrowser

        webbrowser.open(target.resolve().as_uri())
    return 0


# ---------------------------------------------------------------------- stats
def cmd_stats(args: argparse.Namespace) -> int:
    stats = Store(args.db).stats()
    print(f"\nStored jobs: {stats['total']}\n")
    if stats["by_status"]:
        print("  by status:")
        for status, count in sorted(stats["by_status"].items(), key=lambda kv: -kv[1]):
            print(f"    {status:<14} {count:>4}")
    if stats["by_region"]:
        print("\n  by region:")
        for region, count in sorted(stats["by_region"].items(), key=lambda kv: -kv[1]):
            print(f"    {region:<14} {count:>4}")
    if stats["top"]:
        print("\n  highest scoring:")
        for row in stats["top"]:
            print(f"    {row['score']:5.1f}  {row['title']} — {row['company']}")
    print()
    return 0


# -------------------------------------------------------------------- sources
def cmd_sources(args: argparse.Namespace) -> int:
    sources = SourceConfig.load(args.sources_config)
    enabled = set(sources.enabled_sources())
    print("\nAvailable sources:\n")
    for name in sorted(REGISTRY):
        cfg = sources.for_source(name)
        mark = "on " if name in enabled else "off"
        kind = cfg.get("kind", "?")
        print(f"  [{mark}] {name:<16} {kind}")
    print("\n  Toggle with the `enabled` flag in config/sources.yaml,")
    print("  or override per-run: python main.py search --sources greenhouse lever\n")
    return 0


# ----------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobseeker",
        description="Profile-driven job search agent for QA / SDET roles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--db", help="path to the SQLite database")
    parser.add_argument("--profile", help="path to profile.yaml")
    parser.add_argument("--sources-config", help="path to sources.yaml")

    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="scrape, score and report")
    p_search.add_argument(
        "--sources", nargs="+", metavar="NAME",
        help=f"override enabled sources ({', '.join(sorted(REGISTRY))})",
    )
    p_search.add_argument("--llm", action="store_true",
                          help="add Claude fit assessments to the top matches")
    p_search.add_argument("--llm-max", type=int, default=20,
                          help="how many matches to send to Claude (default 20)")
    p_search.add_argument("--min-score", type=float,
                          help="override scoring.min_score for this run")
    p_search.add_argument("--workers", type=int, default=6,
                          help="concurrent scrapers (default 6)")
    p_search.add_argument("--top", type=int, default=15,
                          help="matches to print per region (default 15)")
    p_search.add_argument("--format", nargs="*",
                          default=["markdown", "json"],
                          choices=["markdown", "md", "json", "csv", "html"],
                          help="report formats to write (default: markdown json)")
    p_search.set_defaults(func=cmd_search)

    p_list = sub.add_parser("list", help="list stored jobs")
    p_list.add_argument("--status", choices=sorted(VALID_STATUSES))
    p_list.add_argument("--region")
    p_list.add_argument("--min-score", type=float)
    p_list.add_argument("--limit", type=int, default=25)
    p_list.set_defaults(func=cmd_list)

    p_apply = sub.add_parser("apply", help="draft CV bullets and a cover letter")
    p_apply.add_argument("fingerprint", help="job id (a prefix is enough)")
    p_apply.set_defaults(func=cmd_apply)

    p_status = sub.add_parser("status", help="update a job's status")
    p_status.add_argument("fingerprint")
    p_status.add_argument("new_status", choices=sorted(VALID_STATUSES))
    p_status.add_argument("--note")
    p_status.set_defaults(func=cmd_status)

    p_dash = sub.add_parser("dashboard", help="build a single-file HTML dashboard")
    p_dash.add_argument("--out", help="where to write it (default output/dashboard.html)")
    p_dash.add_argument("--limit", type=int, default=500, help="jobs to include")
    p_dash.add_argument("--per-page", type=int, default=25,
                        help="rows per page in the dashboard (default 25)")
    p_dash.add_argument("--open", action="store_true", dest="open_it",
                        help="open it in your browser when done")
    p_dash.set_defaults(func=cmd_dashboard)

    sub.add_parser("stats", help="database summary").set_defaults(func=cmd_stats)
    sub.add_parser("sources", help="list available sources").set_defaults(func=cmd_sources)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except FileNotFoundError as exc:
        print(f"Config error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
