import tempfile
import unittest
from pathlib import Path

from linked_jobs_monitor.config import AppConfig, RunConfig, SearchConfig
from linked_jobs_monitor.database import open_database
from linked_jobs_monitor.parser import extract_jobs
from linked_jobs_monitor.web import render_page
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


if __name__ == "__main__":
    unittest.main()
