import http.client
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from linked_jobs_monitor.config import AppConfig, RunConfig, SearchConfig
from linked_jobs_monitor.database import open_database
from linked_jobs_monitor.linkedin import build_search_urls_for_source, unique_urls
from linked_jobs_monitor.parser import JobListing, extract_jobs
from linked_jobs_monitor.web import (
    CSRF_FIELD,
    build_handler,
    find_matching_source_context,
    render_page,
)
from test_parser import SEARCH_HTML


class WebTests(unittest.TestCase):
    def test_not_interested_job_is_not_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file)
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_not_interested("4408953784")
            db.close()

            page = render_page(config)

        self.assertIn("Not Interested", page)
        self.assertNotIn("Infrastructure Engineer", page)
        self.assertNotIn("https://www.linkedin.com/jobs/view/4408953784/", page)

    def test_applied_job_metadata_is_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file)
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_applied("4408953784", applied_at="2026-05-06")
            db.update_user_tracking("4408953784", application_status="interview")
            db.close()

            page = render_page(config)

        self.assertIn("<h2>Jobs</h2>", page)
        self.assertIn("job-summary", page)
        self.assertIn("Infrastructure Engineer", page)
        self.assertIn("2026-05-06", page)
        self.assertIn("interview", page)

    def test_search_filter_hides_non_matching_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file)
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.close()

            page = render_page(config, query_params={"q": ["nonexistent"]})

        self.assertIn("No jobs match the current filters.", page)
        self.assertNotIn("Infrastructure Engineer", page)

    def test_status_filter_can_show_applied_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file)
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_applied("4408953784", applied_at="2026-05-06")
            db.close()

            page = render_page(config, query_params={"status": ["applied"]})

        self.assertIn("Infrastructure Engineer", page)
        self.assertIn('<option value="applied" selected>Applied</option>', page)

    def test_page_renders_search_setup_management(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)

            page = render_page(config)

        self.assertIn("Search Setups", page)
        self.assertIn("Add a new search setup", page)
        self.assertIn("Default LinkedIn search", page)
        self.assertIn("Refresh This", page)
        self.assertIn("Withdraw", page)
        self.assertIn("Manual LinkedIn URL", page)
        self.assertNotIn("LinkedIn AI search URL", page)

    def test_keyword_filter_uses_all_recorded_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file)
            source_id = db.add_search_source(
                name="Logistics",
                keywords="logistics",
                location="Gothenburg, Sweden",
            )
            db.upsert_jobs(
                [
                    JobListing(
                        job_id="900",
                        url="https://www.linkedin.com/jobs/view/900/",
                        title="Shared Role",
                        keyword="logistics",
                        source_url="https://www.linkedin.com/jobs/search/?keywords=logistics",
                        source_id=source_id,
                        source_name="Logistics",
                    ),
                    JobListing(
                        job_id="900",
                        url="https://www.linkedin.com/jobs/view/900/",
                        title="Shared Role",
                        keyword="Azure",
                        source_url="https://www.linkedin.com/jobs/search/?keywords=Azure",
                    ),
                ]
            )
            db.close()

            page = render_page(config, query_params={"keyword": ["logistics"]})

        self.assertIn("Shared Role", page)
        self.assertIn('<option value="logistics" selected>logistics</option>', page)

    def test_rendered_post_forms_include_csrf_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)

            page = render_page(config, csrf_token="test-token")

        self.assertIn(f'name="{CSRF_FIELD}" value="test-token"', page)

    def test_post_without_csrf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            server, thread = start_test_server(config)
            try:
                status = post_form(server, "/refresh", {})
            finally:
                stop_test_server(server, thread)

        self.assertEqual(status, 403)

    def test_post_with_csrf_can_add_search_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            server, thread = start_test_server(config)
            try:
                token = fetch_csrf_token(server)
                status = post_form(
                    server,
                    "/searches",
                    {
                        CSRF_FIELD: token,
                        "name": "Logistics",
                        "keywords": "logistics",
                        "location": "Gothenburg, Sweden",
                        "geo_id": "",
                        "radius_km": "100",
                        "posted_within_days": "7",
                        "sort_by": "DD",
                        "ai_search_url": "",
                        "is_active": "1",
                    },
                )
            finally:
                stop_test_server(server, thread)

            db = open_database(config.run.db_file)
            sources = db.list_search_sources()
            db.close()

        self.assertEqual(status, 303)
        self.assertIn("Logistics", {source.name for source in sources})

    def test_import_context_matches_generated_search_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file, seed_search_config=config.search)
            source = db.list_search_sources()[0]
            search = unique_urls(build_search_urls_for_source(source))[0]
            source_id, source_name, matched_keyword = find_matching_source_context(
                db.list_search_sources(),
                "",
                search.url,
            )
            db.close()

        self.assertEqual(
            (source_id, source_name, matched_keyword),
            (source.id, source.name, search.keyword),
        )

    def test_import_uses_generated_url_context_without_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file, seed_search_config=config.search)
            source = db.list_search_sources()[0]
            search = unique_urls(build_search_urls_for_source(source))[0]
            db.close()

            server, thread = start_test_server(config)
            try:
                token = fetch_csrf_token(server)
                status = post_form(
                    server,
                    "/import",
                    {
                        CSRF_FIELD: token,
                        "keyword": "",
                        "source_url": search.url,
                        "html": SEARCH_HTML,
                    },
                )
            finally:
                stop_test_server(server, thread)

            db = open_database(config.run.db_file)
            job_sources = db.list_job_sources("4408953784")
            db.close()

        self.assertEqual(status, 303)
        self.assertEqual(len(job_sources), 1)
        self.assertEqual(job_sources[0].search_source_id, source.id)
        self.assertEqual(job_sources[0].keyword, search.keyword)

    def test_unsafe_urls_are_not_rendered_as_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            db = open_database(config.run.db_file)
            db.add_search_source(
                name="Unsafe manual URL",
                keywords="Azure",
                location="Gothenburg, Sweden",
                ai_search_url="javascript:alert(1)",
            )
            db.upsert_jobs(
                [
                    JobListing(
                        job_id="777",
                        url="https://www.linkedin.com/jobs/view/777/",
                        title="Unsafe Source",
                        keyword="Azure",
                        source_url="javascript:alert(2)",
                    )
                ]
            )
            db.close()

            page = render_page(config)

        self.assertIn("Unsafe Source", page)
        self.assertNotIn('href="javascript:', page)


def make_config(tmp: str) -> AppConfig:
    return AppConfig(
        search=SearchConfig(
            keywords=["Azure"],
            location="Gothenburg, Sweden",
            geo_id="90009607",
            ai_search_url="https://www.linkedin.com/jobs/search-results/?keywords=Azure",
            radius_km=300,
            posted_within_days=7,
            sort_by="DD",
        ),
        run=RunConfig(
            db_file=Path(tmp) / "jobs.sqlite3",
            state_file=Path(tmp) / "jobs.json",
            report_dir=Path(tmp) / "reports",
            request_delay_seconds=0,
            user_agent="test",
        ),
    )


def start_test_server(config: AppConfig):
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_test_server(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


def fetch_csrf_token(server) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
    finally:
        conn.close()
    match = re.search(r'name="' + re.escape(CSRF_FIELD) + r'" value="([^"]+)"', body)
    if not match:
        raise AssertionError("CSRF token not found in page")
    return match.group(1)


def post_form(server, path: str, values: dict) -> int:
    body = urlencode(values)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
