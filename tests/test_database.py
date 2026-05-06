import tempfile
import unittest
import json
from pathlib import Path

from linked_jobs_monitor.database import (
    STATUS_APPLIED,
    STATUS_NOT_INTERESTED,
    STATUS_SAVED,
    open_database,
)
from linked_jobs_monitor.config import SearchConfig
from linked_jobs_monitor.parser import extract_jobs
from test_parser import DETAIL_HTML, SEARCH_HTML


class DatabaseTests(unittest.TestCase):
    def test_upsert_is_idempotent_and_preserves_saved_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            first = db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_saved("4408953784")
            second = db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            job = db.get_job("4408953784")
            db.close()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(job.user_status, STATUS_SAVED)

    def test_not_interested_job_stays_hidden_and_is_not_reimported(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_not_interested("4408953784")
            reimported = db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            visible = db.list_jobs()
            hidden = db.count_by_status(STATUS_NOT_INTERESTED)
            db.close()

        self.assertEqual(reimported, [])
        self.assertEqual(visible, [])
        self.assertEqual(hidden, 1)

    def test_applied_job_tracks_date_status_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_applied("4408953784", applied_at="2026-05-06")
            db.update_user_tracking(
                "4408953784",
                application_status="interview",
                notes="Recruiter screen booked",
            )
            job = db.get_job("4408953784")
            db.close()

        self.assertEqual(job.user_status, STATUS_APPLIED)
        self.assertEqual(job.applied_at, "2026-05-06")
        self.assertEqual(job.application_status, "interview")
        self.assertEqual(job.notes, "Recruiter screen booked")

    def test_detail_upsert_adds_deadline_without_resetting_user_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            db.upsert_jobs(extract_jobs(SEARCH_HTML, keyword="Azure"))
            db.mark_saved("4408953784")
            updated = db.upsert_jobs(extract_jobs(DETAIL_HTML, keyword="Azure"))
            job = db.get_job("4408953784")
            db.close()

        self.assertEqual(updated, [])
        self.assertEqual(job.user_status, STATUS_SAVED)
        self.assertEqual(job.application_deadline, "2026-05-20T14:54:44.000Z")
        self.assertEqual(job.employment_type, "FULL_TIME")

    def test_legacy_json_migration_does_not_overwrite_user_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy = tmp_path / "jobs.json"
            legacy.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "job_id": "3",
                                "url": "https://se.linkedin.com/jobs/view/3-month-temporary-courier-at-fedex-4410565804/",
                                "first_seen_at": "2026-05-06T11:22:38+00:00",
                                "title": "3 month temporary courier",
                                "keyword": "logistics",
                                "status": "saved",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            db_path = tmp_path / "jobs.sqlite3"
            db = open_database(db_path, legacy_json_path=legacy)
            db.mark_not_interested("4410565804")
            db.close()

            db = open_database(db_path, legacy_json_path=legacy)
            job = db.get_job("4410565804")
            db.close()

        self.assertEqual(job.user_status, STATUS_NOT_INTERESTED)

    def test_seed_search_source_from_config_only_when_empty(self):
        config = SearchConfig(
            keywords=["Azure", "logistics"],
            location="Gothenburg, Sweden",
            geo_id="90009607",
            ai_search_url="https://www.linkedin.com/jobs/search-results/?keywords=Azure",
            radius_km=300,
            posted_within_days=7,
            sort_by="DD",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.sqlite3"
            db = open_database(db_path, seed_search_config=config)
            sources = db.list_search_sources()
            db.add_search_source(
                name="Stockholm BI",
                keywords="PowerBI",
                location="Stockholm, Sweden",
                radius_km=50,
            )
            db.close()

            db = open_database(db_path, seed_search_config=config)
            sources_after_reopen = db.list_search_sources()
            db.close()

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].keyword_list(), ["Azure", "logistics"])
        self.assertEqual(len(sources_after_reopen), 2)

    def test_search_source_active_toggle_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            source_id = db.add_search_source(
                name="Marine",
                keywords="marine",
                location="Gothenburg, Sweden",
                radius_km=300,
            )
            self.assertEqual(len(db.list_search_sources(active_only=True)), 1)
            db.set_search_source_active(source_id, False)
            self.assertEqual(db.list_search_sources(active_only=True), [])
            self.assertTrue(db.delete_search_source(source_id))
            self.assertEqual(db.list_search_sources(), [])
            db.close()


if __name__ == "__main__":
    unittest.main()
