from __future__ import annotations

import argparse
import sys
import time
import threading
import webbrowser
from pathlib import Path
from typing import Iterable, List, Optional

from .config import DEFAULT_CONFIG, AppConfig, load_config
from .database import open_database
from .fetch import FetchError, fetch_searches, fetch_url
from .linkedin import build_search_urls_for_source, unique_urls
from .parser import (
    JobListing,
    extract_canonical_job_url,
    extract_detail_jobs,
    extract_jobs,
    merge_jobs,
)
from .report import format_jobs, write_report
from .web import serve


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "urls":
        return command_urls(config)
    if args.command == "open":
        return command_open(config)
    if args.command == "check":
        return command_check(config)
    if args.command == "import-html":
        return command_import_html(config, args.html_files)
    if args.command == "enrich-details":
        return command_enrich_details(config, args.limit)
    if args.command == "serve":
        return command_serve(config, args.host, args.port, args.open)

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linked-jobs",
        description="Open LinkedIn job searches and track newly seen job IDs.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to config.ini.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("urls", help="Print configured LinkedIn search URLs.")
    subparsers.add_parser("open", help="Open configured LinkedIn searches.")
    subparsers.add_parser("check", help="Fetch public result pages and record new jobs.")

    import_parser = subparsers.add_parser(
        "import-html", help="Extract jobs from one or more saved LinkedIn HTML files."
    )
    import_parser.add_argument("html_files", nargs="+", type=Path)

    enrich_parser = subparsers.add_parser(
        "enrich-details",
        help="Fetch public job detail pages for tracked jobs missing deadline/details.",
    )
    enrich_parser.add_argument("--limit", type=int, default=10)

    serve_parser = subparsers.add_parser("serve", help="Run the local web interface.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", default=8765, type=int)
    serve_parser.add_argument("--open", action="store_true", help="Open the app in a browser.")
    return parser


def command_urls(config: AppConfig) -> int:
    db = open_database(
        config.run.db_file,
        legacy_json_path=config.run.state_file,
        seed_search_config=config.search,
    )
    sources = db.list_search_sources()
    db.close()
    for source in sources:
        state = "active" if source.is_active else "inactive"
        print(f"{source.name} ({state})")
        for search in unique_urls(build_search_urls_for_source(source)):
            print(f"  {search.keyword}: {search.url}")
        if source.ai_search_url:
            print(f"  Manual LinkedIn URL: {source.ai_search_url}")
    return 0


def command_open(config: AppConfig) -> int:
    db = open_database(
        config.run.db_file,
        legacy_json_path=config.run.state_file,
        seed_search_config=config.search,
    )
    sources = db.list_search_sources(active_only=True)
    db.close()
    opened = 0
    for source in sources:
        for search in unique_urls(build_search_urls_for_source(source)):
            print(f"Opening {source.name} / {search.keyword}: {search.url}")
            webbrowser.open_new_tab(search.url)
            opened += 1
        if source.ai_search_url:
            print(f"Opening {source.name} manual LinkedIn URL: {source.ai_search_url}")
            webbrowser.open_new_tab(source.ai_search_url)
            opened += 1
    print(f"Opened {opened} searches.")
    return 0


def command_check(config: AppConfig) -> int:
    db = open_database(
        config.run.db_file,
        legacy_json_path=config.run.state_file,
        seed_search_config=config.search,
    )
    sources = db.list_search_sources(active_only=True)
    searches = [
        search
        for source in sources
        for search in unique_urls(build_search_urls_for_source(source))
    ]
    if not searches:
        db.close()
        print("No active search sources. Add one in the web app first.", file=sys.stderr)
        return 2
    try:
        pages = fetch_searches(
            searches,
            user_agent=config.run.user_agent,
            delay_seconds=config.run.request_delay_seconds,
        )
    except FetchError as exc:
        db.close()
        print(f"Fetch failed: {exc}", file=sys.stderr)
        print("Try `linked-jobs open`, save the page HTML, then run import-html.", file=sys.stderr)
        return 1

    groups = [
        extract_jobs(
            html_text,
            keyword=search.keyword,
            source_url=search.url,
            source_id=search.source_id,
            source_name=search.source_name,
        )
        for search, html_text in pages
    ]
    for source in sources:
        db.mark_search_source_run(source.id)
    db.close()
    return record_and_report(config, [job for group in groups for job in group])


def command_import_html(config: AppConfig, html_files: Iterable[Path]) -> int:
    groups = []
    for path in html_files:
        if not path.exists():
            print(f"Missing file: {path}", file=sys.stderr)
            return 2
        groups.append(extract_jobs(path.read_text(encoding="utf-8", errors="replace")))
    return record_and_report(config, merge_jobs(groups))


def record_and_report(config: AppConfig, listings: List[JobListing]) -> int:
    db = open_database(
        config.run.db_file,
        legacy_json_path=config.run.state_file,
        seed_search_config=config.search,
    )
    new_jobs = db.upsert_jobs(listings)
    report_path = write_report(config.run.report_dir, new_jobs)

    if new_jobs:
        print(f"Found {len(new_jobs)} new jobs:")
        print(format_jobs(new_jobs))
    else:
        print("No new jobs found.")
    print(f"Report: {report_path}")
    print(f"Tracked total: {db.total_count()} jobs")
    db.close()
    return 0


def command_enrich_details(config: AppConfig, limit: int) -> int:
    db = open_database(
        config.run.db_file,
        legacy_json_path=config.run.state_file,
        seed_search_config=config.search,
    )
    candidates = [
        job
        for job in db.list_jobs()
        if not job.application_deadline and not job.details_fetched_at
    ][: max(0, limit)]

    enriched = 0
    for index, job in enumerate(candidates):
        if index:
            time.sleep(config.run.request_delay_seconds)
        try:
            html_text = fetch_url(job.linkedin_url, user_agent=config.run.user_agent)
        except FetchError as exc:
            print(f"Skipping {job.job_id}: {exc}", file=sys.stderr)
            continue
        listings = extract_detail_jobs(
            html_text,
            keyword=job.source_keyword,
            source_url=job.linkedin_url,
        )
        if not listings:
            canonical_url = extract_canonical_job_url(html_text)
            if canonical_url and canonical_url != job.linkedin_url:
                try:
                    html_text = fetch_url(canonical_url, user_agent=config.run.user_agent)
                except FetchError as exc:
                    print(f"Skipping canonical {job.job_id}: {exc}", file=sys.stderr)
                    continue
                listings = extract_detail_jobs(
                    html_text,
                    keyword=job.source_keyword,
                    source_url=canonical_url,
                )
        if listings:
            db.upsert_jobs(listings)
            enriched += 1

    print(f"Enriched {enriched} of {len(candidates)} detail pages.")
    db.close()
    return 0


def command_serve(config: AppConfig, host: str, port: int, open_browser: bool = False) -> int:
    if open_browser:
        url = f"http://{host}:{port}"
        threading.Timer(0.8, lambda: webbrowser.open_new_tab(url)).start()
    try:
        serve(config, host=host, port=port)
    except KeyboardInterrupt:
        print("\nServer stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
