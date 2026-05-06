from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .config import AppConfig
from .database import (
    STATUS_APPLIED,
    STATUS_ARCHIVED,
    STATUS_NEW,
    STATUS_NOT_INTERESTED,
    STATUS_SAVED,
    JobRecord,
    SearchSource,
    open_database,
)
from .fetch import FetchError, fetch_searches, fetch_url
from .linkedin import build_search_urls_for_source, unique_urls
from .parser import extract_canonical_job_url, extract_detail_jobs, extract_jobs, merge_jobs


VISIBLE_STATUSES = {STATUS_NEW, STATUS_SAVED, STATUS_APPLIED}


def serve(config: AppConfig, host: str, port: int) -> None:
    handler = build_handler(config)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Linked Jobs Monitor running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def build_handler(config: AppConfig):
    class LinkedJobsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/":
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            message = first_value(query, "message")
            error = first_value(query, "error")
            body = render_page(config, message=message, error=error, query_params=query)
            self.respond_html(body)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/refresh":
                self.refresh_jobs()
                return
            if parsed.path == "/enrich":
                self.enrich_details()
                return
            if parsed.path == "/import":
                self.import_html()
                return
            if parsed.path == "/searches":
                self.add_search_source()
                return
            if parsed.path.startswith("/searches/"):
                self.update_search_source(parsed.path)
                return
            if parsed.path.startswith("/jobs/"):
                self.update_job(parsed.path)
                return
            self.send_error(404)

        def refresh_jobs(self) -> None:
            db = open_database(
                config.run.db_file,
                legacy_json_path=config.run.state_file,
                seed_search_config=config.search,
            )
            sources = db.list_search_sources(active_only=True)
            db.close()
            self.run_search_sources(sources)

        def run_search_sources(self, sources: List[SearchSource]) -> None:
            if not sources:
                self.redirect("/", error="No active search sources. Add or enable a search first.")
                return
            searches = unique_urls(
                search
                for source in sources
                for search in build_search_urls_for_source(source)
            )
            try:
                pages = fetch_searches(
                    searches,
                    user_agent=config.run.user_agent,
                    delay_seconds=config.run.request_delay_seconds,
                )
            except FetchError as exc:
                self.redirect("/", error=str(exc))
                return

            groups = [
                extract_jobs(html_text, keyword=search.keyword, source_url=search.url)
                for search, html_text in pages
            ]
            db = open_database(
                config.run.db_file,
                legacy_json_path=config.run.state_file,
                seed_search_config=config.search,
            )
            new_jobs = db.upsert_jobs(merge_jobs(groups))
            for source in sources:
                db.mark_search_source_run(source.id)
            total = db.total_count()
            db.close()
            source_label = "search" if len(sources) == 1 else "searches"
            self.redirect("/", message=f"Refresh complete for {len(sources)} {source_label}. Added {len(new_jobs)} new jobs. Tracking {total} total.")

        def enrich_details(self) -> None:
            db = open_database(
                config.run.db_file,
                legacy_json_path=config.run.state_file,
                seed_search_config=config.search,
            )
            candidates = [
                job
                for job in db.list_jobs()
                if not job.application_deadline and not job.details_fetched_at
            ][:10]

            enriched = 0
            for index, job in enumerate(candidates):
                if index:
                    time.sleep(config.run.request_delay_seconds)
                try:
                    html_text = fetch_url(job.linkedin_url, user_agent=config.run.user_agent)
                except FetchError:
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
                        except FetchError:
                            continue
                        listings = extract_detail_jobs(
                            html_text,
                            keyword=job.source_keyword,
                            source_url=canonical_url,
                        )
                if listings:
                    db.upsert_jobs(listings)
                    enriched += 1

            db.close()
            self.redirect("/", message=f"Enriched {enriched} of {len(candidates)} visible jobs.")

        def import_html(self) -> None:
            form = self.read_form()
            html_text = first_value(form, "html")
            keyword = first_value(form, "keyword")
            source_url = first_value(form, "source_url")
            if not html_text.strip():
                self.redirect("/", error="Paste saved LinkedIn result or detail-page HTML first.")
                return

            db = open_database(
                config.run.db_file,
                legacy_json_path=config.run.state_file,
                seed_search_config=config.search,
            )
            new_jobs = db.upsert_jobs(
                extract_jobs(html_text, keyword=keyword or None, source_url=source_url)
            )
            total = db.total_count()
            db.close()
            self.redirect("/", message=f"Imported {len(new_jobs)} new jobs. Tracking {total} total.")

        def add_search_source(self) -> None:
            form = self.read_form()
            try:
                db = open_database(
                    config.run.db_file,
                    legacy_json_path=config.run.state_file,
                    seed_search_config=config.search,
                )
                db.add_search_source(
                    name=first_value(form, "name"),
                    keywords=first_value(form, "keywords"),
                    location=first_value(form, "location"),
                    geo_id=first_value(form, "geo_id"),
                    ai_search_url=first_value(form, "ai_search_url"),
                    radius_km=parse_int(first_value(form, "radius_km"), 300),
                    posted_within_days=parse_int(first_value(form, "posted_within_days"), 7),
                    sort_by=first_value(form, "sort_by") or "DD",
                    is_active=first_value(form, "is_active") == "1",
                )
                db.close()
            except ValueError as exc:
                self.redirect("/", error=str(exc))
                return
            self.redirect("/", message="Search source added.")

        def update_search_source(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) != 3:
                self.send_error(404)
                return
            _, source_id_text, action = parts
            try:
                source_id = int(source_id_text)
            except ValueError:
                self.send_error(404)
                return

            db = open_database(
                config.run.db_file,
                legacy_json_path=config.run.state_file,
                seed_search_config=config.search,
            )
            source = db.get_search_source(source_id)
            if source is None:
                db.close()
                self.redirect("/", error="Search source not found.")
                return

            if action == "refresh":
                db.close()
                self.run_search_sources([source])
                return
            if action == "toggle":
                db.set_search_source_active(source_id, not source.is_active)
                db.close()
                self.redirect("/", message="Search source updated.")
                return
            if action == "delete":
                db.delete_search_source(source_id)
                db.close()
                self.redirect("/", message="Search source removed. Existing jobs remain in the database.")
                return
            if action == "update":
                form = self.read_form()
                try:
                    db.update_search_source(
                        source_id=source_id,
                        name=first_value(form, "name"),
                        keywords=first_value(form, "keywords"),
                        location=first_value(form, "location"),
                        geo_id=first_value(form, "geo_id"),
                        ai_search_url=first_value(form, "ai_search_url"),
                        radius_km=parse_int(first_value(form, "radius_km"), 300),
                        posted_within_days=parse_int(first_value(form, "posted_within_days"), 7),
                        sort_by=first_value(form, "sort_by") or "DD",
                        is_active=first_value(form, "is_active") == "1",
                    )
                except ValueError as exc:
                    db.close()
                    self.redirect("/", error=str(exc))
                    return
                db.close()
                self.redirect("/", message="Search source updated.")
                return

            db.close()
            self.send_error(404)

        def update_job(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) != 3:
                self.send_error(404)
                return

            _, job_id, action = parts
            db = open_database(
                config.run.db_file,
                legacy_json_path=config.run.state_file,
                seed_search_config=config.search,
            )
            message = ""
            if action == "save":
                changed = db.mark_saved(job_id)
                message = "Job saved."
            elif action == "new":
                changed = db.mark_new(job_id)
                message = "Job moved back to new jobs."
            elif action == "delete":
                changed = db.mark_not_interested(job_id)
                message = "Job marked not interested and hidden."
            elif action == "applied":
                changed = db.mark_applied(job_id)
                message = "Job marked as applied."
            elif action == "update":
                form = self.read_form()
                changed = db.update_user_tracking(
                    job_id,
                    user_status=first_value(form, "user_status") or None,
                    application_status=first_value(form, "application_status"),
                    applied_at=first_value(form, "applied_at"),
                    notes=first_value(form, "notes"),
                )
                message = "Job tracking updated."
            else:
                db.close()
                self.send_error(404)
                return

            db.close()
            if not changed:
                self.redirect("/", error="Job not found.")
                return
            self.redirect("/", message=message)

        def read_form(self) -> Dict[str, List[str]]:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            return parse_qs(raw_body, keep_blank_values=True)

        def redirect(
            self,
            path: str,
            message: Optional[str] = None,
            error: Optional[str] = None,
        ) -> None:
            query = {}
            if message:
                query["message"] = message
            if error:
                query["error"] = error
            location = path
            if query:
                location += "?" + urlencode(query)
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def respond_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

    return LinkedJobsHandler


def render_page(
    config: AppConfig,
    message: Optional[str] = None,
    error: Optional[str] = None,
    query_params: Optional[Dict[str, List[str]]] = None,
) -> str:
    query_params = query_params or {}
    filters = read_filters(query_params)
    db = open_database(
        config.run.db_file,
        legacy_json_path=config.run.state_file,
        seed_search_config=config.search,
    )
    sources = db.list_search_sources()
    all_jobs = db.list_jobs(include_hidden=True)
    new_count = db.count_by_status(STATUS_NEW)
    saved_count = db.count_by_status(STATUS_SAVED)
    applied_count = db.count_by_status(STATUS_APPLIED)
    hidden_count = db.count_by_status(STATUS_NOT_INTERESTED)
    archived_count = db.count_by_status(STATUS_ARCHIVED)
    total_count = db.total_count()
    visible_jobs = [job for job in all_jobs if job.user_status in VISIBLE_STATUSES]
    needs_details_count = len(
        [
            job
            for job in visible_jobs
            if not job.application_deadline and not job.details_fetched_at
        ]
    )
    source_keywords = {
        keyword
        for source in sources
        for keyword in source.keyword_list()
    }
    keyword_options = sorted(
        {job.source_keyword for job in all_jobs if job.source_keyword} | source_keywords
    )
    filtered_jobs = sort_jobs(filter_jobs(all_jobs, filters), filters["sort"])
    default_ai_url = next((source.ai_search_url for source in sources if source.ai_search_url), "")
    db.close()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Linked Jobs Monitor</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">Linked Jobs Monitor</p>
        <h1>Application Tracker</h1>
      </div>
      <div class="top-actions">
        <form method="post" action="/refresh">
          <button class="primary" type="submit">Refresh Active Searches</button>
        </form>
        <form method="post" action="/enrich">
          <button type="submit">Enrich Details</button>
        </form>
      </div>
    </header>

    {render_notice(message, "notice")}
    {render_notice(error, "error")}

    <section class="summary" aria-label="Job summary">
      {summary_item(new_count, "New")}
      {summary_item(saved_count, "Saved")}
      {summary_item(applied_count, "Applied")}
      {summary_item(hidden_count, "Not Interested")}
      {summary_item(needs_details_count, "Needs Details")}
      {summary_item(archived_count, "Archived")}
      {summary_item(total_count, "Total Tracked")}
    </section>

    {render_search_sources(config, sources)}

    <section class="jobs-panel">
      <div class="panel-heading">
        <div>
          <h2>Jobs</h2>
          <p>{len(filtered_jobs)} shown · expand a row for details and tracking</p>
        </div>
      </div>
      {render_filter_form(filters, keyword_options)}
      {render_jobs(filtered_jobs, empty_text="No jobs match the current filters.")}
    </section>

    <section class="import">
      <h2>Import Saved LinkedIn HTML</h2>
      <form method="post" action="/import">
        <div class="inline-fields">
          <label>
            Keyword
            <input name="keyword" placeholder="Azure">
          </label>
          <label>
            Source URL
            <input name="source_url" value="{escape(default_ai_url)}" placeholder="https://www.linkedin.com/jobs/search-results/...">
          </label>
        </div>
        <label>
          HTML
          <textarea name="html" rows="8" placeholder="Paste a saved LinkedIn search result or job detail page here"></textarea>
        </label>
        <button type="submit">Import HTML</button>
      </form>
    </section>
  </main>
</body>
</html>"""


def summary_item(count: int, label: str) -> str:
    return f"<div><span>{count}</span><p>{escape(label)}</p></div>"


def render_search_sources(config: AppConfig, sources: List[SearchSource]) -> str:
    return f"""
    <section class="searches">
      <div class="panel-heading">
        <div>
          <h2>Search Setups</h2>
          <p>Add different keyword/location searches. Refresh runs all active setups.</p>
        </div>
      </div>
      <details class="add-search">
        <summary>Add a new search setup</summary>
        {render_search_source_form(
            action="/searches",
            name="",
            keywords=", ".join(config.search.keywords),
            location=config.search.location,
            geo_id=config.search.geo_id,
            radius_km=config.search.radius_km,
            posted_within_days=config.search.posted_within_days,
            sort_by=config.search.sort_by,
            ai_search_url="",
            is_active=True,
            submit_label="Add Search",
        )}
      </details>
      <div class="source-list">
        {''.join(render_search_source(source) for source in sources)}
      </div>
    </section>
    """


def render_search_source(source: SearchSource) -> str:
    status = "Active" if source.is_active else "Paused"
    toggle_label = "Pause" if source.is_active else "Enable"
    run_text = source.last_run_at or "never"
    return f"""
      <article class="source-card">
        <div class="source-head">
          <div>
            <h3>{escape(source.name)}</h3>
            <p>{escape(source.keywords)} · {escape(source.location)} · past {source.posted_within_days} days · {source.radius_km} km · {status} · last run {escape(format_value(run_text))}</p>
          </div>
          <div class="source-actions">
            <form method="post" action="/searches/{source.id}/refresh">
              <button class="primary" type="submit">Refresh This</button>
            </form>
            <form method="post" action="/searches/{source.id}/toggle">
              <button type="submit">{toggle_label}</button>
            </form>
            <form method="post" action="/searches/{source.id}/delete">
              <button class="danger" type="submit">Remove</button>
            </form>
          </div>
        </div>
        <div class="search-grid">
          {''.join(render_search_link(search.keyword, search.url) for search in unique_urls(build_search_urls_for_source(source)))}
          {render_ai_search_link(source.ai_search_url)}
        </div>
        <details class="edit-search">
          <summary>Edit setup</summary>
          {render_search_source_form(
              action=f"/searches/{source.id}/update",
              name=source.name,
              keywords=source.keywords,
              location=source.location,
              geo_id=source.geo_id,
              radius_km=source.radius_km,
              posted_within_days=source.posted_within_days,
              sort_by=source.sort_by,
              ai_search_url=source.ai_search_url,
              is_active=source.is_active,
              submit_label="Save Setup",
          )}
        </details>
      </article>
    """


def render_search_source_form(
    action: str,
    name: str,
    keywords: str,
    location: str,
    geo_id: str,
    radius_km: int,
    posted_within_days: int,
    sort_by: str,
    ai_search_url: str,
    is_active: bool,
    submit_label: str,
) -> str:
    checked = " checked" if is_active else ""
    return f"""
      <form class="search-source-form" method="post" action="{escape(action)}">
        <div class="form-grid">
          <label>
            Name
            <input name="name" value="{escape(name)}" placeholder="Gothenburg Azure / logistics">
          </label>
          <label>
            Keywords
            <textarea name="keywords" rows="3" placeholder="Azure, PowerBI, marine">{escape(keywords)}</textarea>
          </label>
          <label>
            Location
            <input name="location" value="{escape(location)}" placeholder="Gothenburg, Sweden">
          </label>
          <label>
            LinkedIn geoId
            <input name="geo_id" value="{escape(geo_id)}" placeholder="90009607">
          </label>
          <label>
            Radius km
            <input name="radius_km" type="number" min="1" value="{radius_km}">
          </label>
          <label>
            Posted within days
            <input name="posted_within_days" type="number" min="1" value="{posted_within_days}">
          </label>
          <label>
            Sort
            <select name="sort_by">
              {select_options([("DD", "Newest first"), ("R", "Most relevant")], sort_by)}
            </select>
          </label>
          <label>
            LinkedIn AI search URL
            <input name="ai_search_url" value="{escape(ai_search_url)}" placeholder="Optional logged-in AI search URL">
          </label>
        </div>
        <label class="checkbox-label">
          <input name="is_active" value="1" type="checkbox"{checked}>
          Active when refreshing all searches
        </label>
        <button class="primary" type="submit">{escape(submit_label)}</button>
      </form>
    """


def render_jobs(jobs: List[JobRecord], empty_text: str = "") -> str:
    if not jobs:
        return f'<p class="empty">{escape(empty_text)}</p>'
    return '<div class="job-list">' + "".join(render_job(job) for job in jobs) + "</div>"


def render_job(job: JobRecord) -> str:
    title = escape(job.title or f"LinkedIn job {job.job_id}")
    company = escape(job.company or "Unknown company")
    location = escape(job.location or "Unknown location")
    keyword = escape(job.source_keyword or "no keyword")
    deadline_class = "fact-warn" if not job.application_deadline else ""
    description = render_description(job.description)
    deadline = job.application_deadline or "No deadline"
    posted = job.posted_at or job.posted_text or "No post date"

    return f"""
      <details class="job-row">
        <summary class="job-summary">
          <span class="summary-main">
            <span class="summary-title">{title}</span>
            <span class="summary-meta">{company} · {location}</span>
          </span>
          <span class="summary-badges">
            {chip(job.user_status.replace("_", " ").title())}
            {chip(keyword)}
            {chip("Posted " + format_compact(posted))}
            {chip(format_compact(deadline))}
          </span>
        </summary>
        <div class="job-details">
        <div class="job-main">
          <div class="detail-title-row">
            <a class="job-title" href="{escape(job.linkedin_url)}" target="_blank" rel="noreferrer">{title}</a>
            <a class="external-link" href="{escape(job.linkedin_url)}" target="_blank" rel="noreferrer">Open LinkedIn</a>
          </div>
          <p>{company} · {location}</p>
          <div class="chips">
            {chip(job.user_status.replace("_", " ").title())}
            {chip("Keyword: " + keyword)}
            {chip(job.application_status) if job.application_status else ""}
            {chip(job.insight) if job.insight else ""}
          </div>
          <dl class="facts">
            {fact("Posted", job.posted_at or job.posted_text)}
            {fact("Deadline", job.application_deadline, deadline_class)}
            {fact("Employment", humanize_code(job.employment_type))}
            {fact("Seniority", job.seniority_level)}
            {fact("Function", job.job_function)}
            {fact("Industries", job.industries)}
            {fact("Applicants", job.applicants)}
            {fact("Applied", job.applied_at)}
            {fact("First Seen", job.first_seen_at)}
            {fact("Last Seen", job.last_seen_at)}
            {fact("Details", "fetched" if job.details_fetched_at else "search-card only", "fact-warn" if not job.details_fetched_at else "")}
            {fact_link("Source", job.source_url)}
          </dl>
          {description}
        </div>
        <div class="actions">
          {action_button(job.job_id, "save", "Save", "primary")}
          {action_button(job.job_id, "applied", "Applied", "secondary")}
          {action_button(job.job_id, "delete", "Not Interested", "danger")}
        </div>
        {render_tracking_form(job)}
        </div>
      </details>
    """


def read_filters(query_params: Dict[str, List[str]]) -> Dict[str, str]:
    return {
        "q": first_value(query_params, "q").strip(),
        "status": first_value(query_params, "status") or "visible",
        "keyword": first_value(query_params, "keyword"),
        "details": first_value(query_params, "details") or "all",
        "sort": first_value(query_params, "sort") or "posted_desc",
    }


def filter_jobs(jobs: List[JobRecord], filters: Dict[str, str]) -> List[JobRecord]:
    result = list(jobs)
    status = filters["status"]
    if status == "visible":
        result = [job for job in result if job.user_status in VISIBLE_STATUSES]
    elif status != "all":
        result = [job for job in result if job.user_status == status]

    keyword = filters["keyword"]
    if keyword:
        result = [job for job in result if job.source_keyword == keyword]

    details = filters["details"]
    if details == "needs_details":
        result = [
            job
            for job in result
            if not job.application_deadline and not job.details_fetched_at
        ]
    elif details == "enriched":
        result = [
            job
            for job in result
            if job.application_deadline or job.details_fetched_at
        ]

    query = filters["q"].lower()
    if query:
        result = [job for job in result if query in searchable_text(job)]
    return result


def sort_jobs(jobs: List[JobRecord], sort_key: str) -> List[JobRecord]:
    if sort_key == "posted_asc":
        return sorted(jobs, key=lambda job: sort_value(job.posted_at or job.posted_text))
    if sort_key == "deadline_asc":
        return sorted(jobs, key=lambda job: sort_value(job.application_deadline or "9999"))
    if sort_key == "deadline_desc":
        return sorted(jobs, key=lambda job: sort_value(job.application_deadline), reverse=True)
    if sort_key == "title_asc":
        return sorted(jobs, key=lambda job: job.title.lower())
    if sort_key == "company_asc":
        return sorted(jobs, key=lambda job: job.company.lower())
    if sort_key == "first_seen_desc":
        return sorted(jobs, key=lambda job: sort_value(job.first_seen_at), reverse=True)
    return sorted(
        jobs,
        key=lambda job: sort_value(job.posted_at or job.last_seen_at),
        reverse=True,
    )


def sort_value(value: str) -> str:
    return value or ""


def searchable_text(job: JobRecord) -> str:
    fields = [
        job.title,
        job.company,
        job.location,
        job.source_keyword,
        job.employment_type,
        job.seniority_level,
        job.job_function,
        job.industries,
        job.application_status,
        job.notes,
        job.description,
    ]
    return " ".join(fields).lower()


def render_filter_form(filters: Dict[str, str], keyword_options: List[str]) -> str:
    keyword_select = select_options(
        [("", "All keywords")] + [(keyword, keyword) for keyword in keyword_options],
        filters["keyword"],
    )
    return f"""
      <form class="filters" method="get" action="/">
        <label class="search-field">
          Search
          <input name="q" value="{escape(filters['q'])}" placeholder="Title, company, location, notes">
        </label>
        <label>
          Status
          <select name="status">
            {select_options([
                ("visible", "Visible"),
                ("all", "All"),
                (STATUS_NEW, "New"),
                (STATUS_SAVED, "Saved"),
                (STATUS_APPLIED, "Applied"),
                (STATUS_ARCHIVED, "Archived"),
                (STATUS_NOT_INTERESTED, "Not Interested"),
            ], filters["status"])}
          </select>
        </label>
        <label>
          Keyword
          <select name="keyword">{keyword_select}</select>
        </label>
        <label>
          Details
          <select name="details">
            {select_options([
                ("all", "All"),
                ("needs_details", "Needs details"),
                ("enriched", "Enriched"),
            ], filters["details"])}
          </select>
        </label>
        <label>
          Sort
          <select name="sort">
            {select_options([
                ("posted_desc", "Posted newest"),
                ("posted_asc", "Posted oldest"),
                ("deadline_asc", "Deadline soonest"),
                ("deadline_desc", "Deadline latest"),
                ("first_seen_desc", "First seen newest"),
                ("title_asc", "Title A-Z"),
                ("company_asc", "Company A-Z"),
            ], filters["sort"])}
          </select>
        </label>
        <div class="filter-actions">
          <button class="primary" type="submit">Apply</button>
          <a href="/">Reset</a>
        </div>
      </form>
    """


def select_options(options: List[tuple], current: str) -> str:
    items = []
    for value, label in options:
        selected = " selected" if value == current else ""
        items.append(f'<option value="{escape(value)}"{selected}>{escape(label)}</option>')
    return "".join(items)


def chip(value: str) -> str:
    if not value:
        return ""
    return f'<span class="chip">{escape(value)}</span>'


def fact(label: str, value: str, class_name: str = "") -> str:
    display = escape(format_value(value))
    class_attr = f' class="{class_name}"' if class_name else ""
    return f"<div{class_attr}><dt>{escape(label)}</dt><dd>{display}</dd></div>"


def fact_link(label: str, url: str) -> str:
    if not url:
        return fact(label, "")
    return (
        f"<div><dt>{escape(label)}</dt>"
        f'<dd><a href="{escape(url)}" target="_blank" rel="noreferrer">Open</a></dd></div>'
    )


def render_description(description: str) -> str:
    if not description:
        return ""
    return f'<details class="description"><summary>Description</summary><p>{escape(shorten(description, 520))}</p></details>'


def format_value(value: str) -> str:
    if not value:
        return "not captured"
    value = re.sub(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}).*Z", r"\1 \2 UTC", value)
    value = re.sub(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}).*\+00:00", r"\1 \2 UTC", value)
    return value.replace("+00:00", " UTC")


def format_compact(value: str) -> str:
    if not value:
        return "not captured"
    value = format_value(value)
    if value == "not captured":
        return value
    if len(value) > 18:
        return value[:16].rstrip()
    return value


def humanize_code(value: str) -> str:
    if not value:
        return ""
    if value.isupper() and "_" in value:
        return value.replace("_", " ").title()
    return value


def shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def render_tracking_form(job: JobRecord) -> str:
    return f"""
      <form class="tracking" method="post" action="/jobs/{escape(job.job_id)}/update">
        <label>
          Status
          <select name="user_status">
            {status_option(STATUS_NEW, "New", job.user_status)}
            {status_option(STATUS_SAVED, "Saved", job.user_status)}
            {status_option(STATUS_APPLIED, "Applied", job.user_status)}
            {status_option(STATUS_ARCHIVED, "Archived", job.user_status)}
            {status_option(STATUS_NOT_INTERESTED, "Not Interested", job.user_status)}
          </select>
        </label>
        <label>
          Application
          <input name="application_status" value="{escape(job.application_status)}" placeholder="applied, interview, rejected">
        </label>
        <label>
          Applied At
          <input name="applied_at" type="date" value="{escape(job.applied_at)}">
        </label>
        <label class="notes">
          Notes
          <input name="notes" value="{escape(job.notes)}" placeholder="Contact, next step, reminder">
        </label>
        <button type="submit">Update</button>
      </form>
    """


def status_option(value: str, label: str, current: str) -> str:
    selected = " selected" if value == current else ""
    return f'<option value="{value}"{selected}>{escape(label)}</option>'


def action_button(job_id: str, action: str, label: str, class_name: str) -> str:
    return f"""
      <form method="post" action="/jobs/{escape(job_id)}/{action}">
        <button class="{class_name}" type="submit">{label}</button>
      </form>
    """


def render_search_link(keyword: str, url: str) -> str:
    return (
        f'<a href="{escape(url)}" target="_blank" rel="noreferrer">'
        f"{escape(keyword)}</a>"
    )


def render_ai_search_link(url: str) -> str:
    if not url:
        return ""
    return f'<a href="{escape(url)}" target="_blank" rel="noreferrer">LinkedIn AI Search</a>'


def render_notice(text: Optional[str], class_name: str) -> str:
    if not text:
        return ""
    return f'<p class="{class_name}">{escape(text)}</p>'


def first_value(values: Dict[str, List[str]], key: str) -> str:
    items = values.get(key)
    if not items:
        return ""
    return items[0]


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


CSS = """
:root {
  color-scheme: light;
  --bg: #f5f7f5;
  --panel: #ffffff;
  --text: #172026;
  --muted: #5b6670;
  --line: #dce3de;
  --accent: #0f766e;
  --accent-dark: #0a5e58;
  --danger: #b42318;
  --danger-bg: #fff1ef;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.shell {
  width: min(1180px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 48px;
}

.topbar,
.summary,
.searches,
.import,
.jobs-panel,
.job-row {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 22px;
}

.top-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.eyebrow,
h1,
h2,
p {
  margin: 0;
}

.eyebrow {
  color: var(--accent);
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
}

h1 {
  margin-top: 4px;
  font-size: 30px;
  line-height: 1.15;
}

h2 {
  margin-bottom: 12px;
  font-size: 18px;
}

button,
.search-grid a,
.filter-actions a,
.external-link,
select,
input,
textarea {
  border-radius: 6px;
  border: 1px solid var(--line);
  font: inherit;
}

button,
.search-grid a,
.filter-actions a,
.external-link {
  min-height: 38px;
  padding: 8px 12px;
  background: #ffffff;
  color: var(--text);
  font-weight: 650;
  text-decoration: none;
  cursor: pointer;
}

button:hover,
.search-grid a:hover,
.filter-actions a:hover,
.external-link:hover {
  border-color: var(--accent);
}

button.primary {
  border-color: var(--accent);
  background: var(--accent);
  color: #ffffff;
}

button.primary:hover {
  background: var(--accent-dark);
}

button.danger {
  border-color: #f3c6c1;
  background: var(--danger-bg);
  color: var(--danger);
}

.notice,
.error {
  margin-top: 16px;
  padding: 12px 14px;
  border-radius: 6px;
  background: #e7f4ef;
  color: #164e43;
}

.error {
  background: var(--danger-bg);
  color: var(--danger);
}

.summary {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 1px;
  overflow: hidden;
  margin-top: 16px;
}

.summary div {
  padding: 16px;
  background: #ffffff;
}

.summary span {
  display: block;
  font-size: 26px;
  font-weight: 750;
}

.summary p,
.searches p,
.job-row p,
.empty {
  color: var(--muted);
}

.searches,
.import,
.jobs-panel {
  margin-top: 16px;
  padding: 20px;
}

.search-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.add-search,
.edit-search {
  margin-top: 14px;
}

.add-search summary,
.edit-search summary {
  cursor: pointer;
  font-weight: 750;
}

.source-list {
  display: grid;
  gap: 12px;
  margin-top: 16px;
}

.source-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
  background: #fbfcfb;
  min-width: 0;
}

.source-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: start;
}

.source-head > div,
.panel-heading > div {
  min-width: 0;
}

.source-head h3 {
  margin: 0 0 4px;
  font-size: 17px;
}

.source-head p,
.panel-heading p {
  overflow-wrap: anywhere;
}

.source-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.source-actions form {
  margin: 0;
}

.search-source-form {
  display: grid;
  gap: 12px;
  margin-top: 12px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 12px;
}

.form-grid label:nth-child(2),
.form-grid label:nth-child(8) {
  grid-column: span 2;
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 8px;
}

.checkbox-label input {
  width: auto;
}

.panel-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 16px;
}

.filters {
  display: grid;
  grid-template-columns: minmax(220px, 2fr) minmax(130px, 1fr) minmax(130px, 1fr) minmax(150px, 1fr) minmax(160px, 1fr) auto;
  gap: 10px;
  align-items: end;
  margin: 16px 0;
}

.filter-actions {
  display: flex;
  gap: 8px;
}

.filter-actions a {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 22px;
}

.job-list {
  display: grid;
  gap: 10px;
}

.job-row {
  display: grid;
  grid-template-columns: 1fr;
  overflow: hidden;
}

.job-row[open] {
  border-color: #c6d7d2;
}

.job-summary {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) auto;
  gap: 16px;
  align-items: center;
  padding: 12px 14px;
  cursor: pointer;
  list-style-position: outside;
}

.job-summary:hover {
  background: #f9fbfa;
}

.summary-main {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.summary-title {
  font-weight: 750;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-title::before {
  content: "+ ";
  color: var(--accent);
  font-weight: 800;
}

.job-row[open] .summary-title::before {
  content: "- ";
}

.summary-meta {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}

.job-details {
  display: grid;
  gap: 14px;
  padding: 14px;
  border-top: 1px solid var(--line);
}

.job-main {
  min-width: 0;
}

.detail-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.job-title {
  color: var(--text);
  font-weight: 750;
  text-decoration: none;
  overflow-wrap: anywhere;
}

.job-title:hover {
  color: var(--accent);
}

.external-link {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.chip {
  display: inline-flex;
  min-height: 26px;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 8px;
  background: #f7faf8;
  color: var(--muted);
  font-size: 13px;
  font-weight: 650;
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
  gap: 8px;
  margin: 12px 0 0;
}

.facts div {
  min-width: 0;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fbfcfb;
}

.facts dt {
  color: var(--muted);
  font-size: 12px;
  font-weight: 750;
  text-transform: uppercase;
}

.facts dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
}

.facts a {
  color: var(--accent);
  font-weight: 700;
}

.facts .fact-warn {
  background: #fff9eb;
  border-color: #f2dc9b;
}

.description {
  margin-top: 12px;
  color: var(--muted);
}

.description summary {
  cursor: pointer;
  color: var(--text);
  font-weight: 700;
}

.description p {
  margin-top: 8px;
  line-height: 1.45;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-start;
}

.actions form {
  margin: 0;
}

.tracking {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  align-items: end;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.tracking button {
  align-self: end;
}

.empty {
  padding: 18px;
  border: 1px dashed var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.55);
}

.import form {
  display: grid;
  gap: 12px;
}

.inline-fields {
  display: grid;
  grid-template-columns: minmax(160px, 260px) 1fr;
  gap: 12px;
}

label {
  display: grid;
  gap: 6px;
  color: var(--muted);
  font-size: 14px;
  font-weight: 650;
}

input,
select,
textarea {
  width: 100%;
  padding: 10px 12px;
  color: var(--text);
  background: #ffffff;
}

textarea {
  resize: vertical;
}

@media (max-width: 980px) {
  .summary {
    grid-template-columns: repeat(3, 1fr);
  }

  .filters {
    grid-template-columns: 1fr 1fr;
  }

  .form-grid,
  .source-head {
    grid-template-columns: 1fr 1fr;
  }

  .form-grid label:nth-child(2),
  .form-grid label:nth-child(8) {
    grid-column: span 2;
  }

  .facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 680px) {
  .shell {
    width: calc(100% - 20px);
    max-width: calc(100% - 20px);
    margin: 0 10px;
  }

  .topbar {
    flex-direction: column;
    align-items: stretch;
  }

  .topbar,
  .job-summary,
  .detail-title-row,
  .source-head {
    grid-template-columns: 1fr;
  }

  .topbar,
  .top-actions,
  .filter-actions,
  .source-actions,
  .actions {
    align-items: stretch;
  }

  .summary,
  .inline-fields,
  .filters,
  .form-grid,
  .facts,
  .tracking {
    grid-template-columns: 1fr;
  }

  .form-grid label:nth-child(2),
  .form-grid label:nth-child(8) {
    grid-column: auto;
  }

  .job-summary {
    gap: 8px;
  }

  .summary-badges {
    justify-content: flex-start;
  }

  .summary-title,
  .summary-meta {
    white-space: normal;
  }

  .actions {
    display: grid;
    grid-template-columns: 1fr;
  }

  .top-actions,
  .filter-actions,
  .source-actions {
    display: grid;
    grid-template-columns: 1fr;
  }

  button {
    width: 100%;
  }
}
"""
