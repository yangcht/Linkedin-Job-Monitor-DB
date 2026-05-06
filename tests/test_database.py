import tempfile
import unittest
import json
from pathlib import Path

from linked_jobs_monitor.database import (
    STATUS_APPLIED,
    STATUS_NEW,
    STATUS_NOT_INTERESTED,
    STATUS_SAVED,
    open_database,
)
from linked_jobs_monitor.config import SearchConfig
from linked_jobs_monitor.parser import JobListing, extract_jobs
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
            db.upsert_jobs(
                extract_jobs(
                    SEARCH_HTML,
                    keyword="Azure",
                    source_url="https://www.linkedin.com/jobs/search/?keywords=Azure",
                )
            )
            db.mark_saved("4408953784")
            updated = db.upsert_jobs(
                extract_jobs(
                    DETAIL_HTML,
                    keyword="Azure",
                    source_url="https://www.linkedin.com/jobs/view/4408953784/",
                )
            )
            job = db.get_job("4408953784")
            db.close()

        self.assertEqual(updated, [])
        self.assertEqual(job.user_status, STATUS_SAVED)
        self.assertEqual(job.application_deadline, "2026-05-20T14:54:44.000Z")
        self.assertEqual(job.employment_type, "FULL_TIME")
        self.assertEqual(job.source_url, "https://www.linkedin.com/jobs/search/?keywords=Azure")

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

    def test_job_tracks_multiple_search_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            azure_id = db.add_search_source(
                name="Azure Gothenburg",
                keywords="Azure",
                location="Gothenburg, Sweden",
            )
            logistics_id = db.add_search_source(
                name="Logistics Gothenburg",
                keywords="logistics",
                location="Gothenburg, Sweden",
            )

            db.upsert_jobs(
                [
                    job_listing(
                        "100",
                        "Shared job",
                        "Azure",
                        azure_id,
                        "Azure Gothenburg",
                    ),
                    job_listing(
                        "100",
                        "Shared job",
                        "logistics",
                        logistics_id,
                        "Logistics Gothenburg",
                    ),
                ]
            )
            sources = db.list_job_sources("100")
            job = db.get_job("100")
            db.close()

        self.assertEqual(
            {source.search_source_id for source in sources},
            {azure_id, logistics_id},
        )
        self.assertEqual(job.user_status, STATUS_NEW)

    def test_withdraw_search_removes_only_new_exclusive_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "jobs.sqlite3")
            source_id = db.add_search_source(
                name="Wrong keyword",
                keywords="wrong",
                location="Gothenburg, Sweden",
            )
            other_id = db.add_search_source(
                name="Kept keyword",
                keywords="kept",
                location="Gothenburg, Sweden",
            )
            db.upsert_jobs(
                [
                    job_listing("200", "Wrong only", "wrong", source_id, "Wrong keyword"),
                    job_listing("201", "Shared", "wrong", source_id, "Wrong keyword"),
                    job_listing("201", "Shared", "kept", other_id, "Kept keyword"),
                    job_listing("202", "Saved wrong", "wrong", source_id, "Wrong keyword"),
                ]
            )
            db.mark_saved("202")

            removed = db.withdraw_search_source(source_id)
            wrong_only = db.get_job("200")
            shared = db.get_job("201")
            saved = db.get_job("202")
            sources = db.list_search_sources()
            shared_sources = db.list_job_sources("201")
            saved_sources = db.list_job_sources("202")
            db.close()

            db = open_database(Path(tmp) / "jobs.sqlite3")
            saved_sources_after_reopen = db.list_job_sources("202")
            db.close()

        self.assertEqual(removed, 1)
        self.assertIsNone(wrong_only)
        self.assertEqual(shared.user_status, STATUS_NEW)
        self.assertEqual(saved.user_status, STATUS_SAVED)
        self.assertEqual(saved.source_keyword, "")
        self.assertEqual(saved.source_url, "")
        self.assertEqual([source.id for source in sources], [other_id])
        self.assertEqual({source.search_source_id for source in shared_sources}, {other_id})
        self.assertEqual(saved_sources, [])
        self.assertEqual(saved_sources_after_reopen, [])


def job_listing(
    job_id: str,
    title: str,
    keyword: str,
    source_id: int,
    source_name: str,
) -> JobListing:
    return JobListing(
        job_id=job_id,
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        title=title,
        keyword=keyword,
        source_url=f"https://www.linkedin.com/jobs/search/?keywords={keyword}",
        source_id=source_id,
        source_name=source_name,
    )


if __name__ == "__main__":
    unittest.main()
