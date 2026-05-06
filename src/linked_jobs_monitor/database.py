from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from .config import SearchConfig

from .parser import JobListing, extract_job_id_from_url


STATUS_NEW = "new"
STATUS_SAVED = "saved"
STATUS_NOT_INTERESTED = "not_interested"
STATUS_APPLIED = "applied"
STATUS_ARCHIVED = "archived"

VISIBLE_STATUSES = {STATUS_NEW, STATUS_SAVED, STATUS_APPLIED}
VALID_USER_STATUSES = {
    STATUS_NEW,
    STATUS_SAVED,
    STATUS_NOT_INTERESTED,
    STATUS_APPLIED,
    STATUS_ARCHIVED,
}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    linkedin_url: str
    title: str
    company: str
    company_url: str
    location: str
    posted_at: str
    posted_text: str
    application_deadline: str
    employment_type: str
    seniority_level: str
    job_function: str
    industries: str
    applicants: str
    description: str
    insight: str
    source_keyword: str
    source_url: str
    first_seen_at: str
    last_seen_at: str
    details_fetched_at: str
    user_status: str
    application_status: str
    applied_at: str
    notes: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SearchSource:
    id: int
    name: str
    keywords: str
    location: str
    geo_id: str
    ai_search_url: str
    radius_km: int
    posted_within_days: int
    sort_by: str
    is_active: bool
    last_run_at: str
    created_at: str
    updated_at: str

    def keyword_list(self) -> List[str]:
        return split_keywords(self.keywords)


class JobDatabase:
    def __init__(
        self,
        path: Path,
        legacy_json_path: Optional[Path] = None,
        seed_search_config: Optional[SearchConfig] = None,
    ) -> None:
        self.path = path
        self.legacy_json_path = legacy_json_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()
        if seed_search_config:
            self.seed_search_source(seed_search_config)
        if legacy_json_path:
            self.migrate_legacy_json(legacy_json_path)

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                linkedin_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                company_url TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                posted_at TEXT NOT NULL DEFAULT '',
                posted_text TEXT NOT NULL DEFAULT '',
                application_deadline TEXT NOT NULL DEFAULT '',
                employment_type TEXT NOT NULL DEFAULT '',
                seniority_level TEXT NOT NULL DEFAULT '',
                job_function TEXT NOT NULL DEFAULT '',
                industries TEXT NOT NULL DEFAULT '',
                applicants TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                insight TEXT NOT NULL DEFAULT '',
                source_keyword TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                details_fetched_at TEXT NOT NULL DEFAULT '',
                user_status TEXT NOT NULL DEFAULT 'new',
                application_status TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_user_status ON jobs(user_status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_seen ON jobs(last_seen_at)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                keywords TEXT NOT NULL,
                location TEXT NOT NULL,
                geo_id TEXT NOT NULL DEFAULT '',
                ai_search_url TEXT NOT NULL DEFAULT '',
                radius_km INTEGER NOT NULL DEFAULT 300,
                posted_within_days INTEGER NOT NULL DEFAULT 7,
                sort_by TEXT NOT NULL DEFAULT 'DD',
                is_active INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_sources_active ON search_sources(is_active)"
        )
        self.conn.commit()

    def seed_search_source(self, config: SearchConfig) -> None:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM search_sources").fetchone()
        if int(row["count"]):
            return
        name = "Default LinkedIn search"
        self.add_search_source(
            name=name,
            keywords=", ".join(config.keywords),
            location=config.location,
            geo_id=config.geo_id,
            ai_search_url=config.ai_search_url,
            radius_km=config.radius_km,
            posted_within_days=config.posted_within_days,
            sort_by=config.sort_by,
            is_active=True,
        )

    def migrate_legacy_json(self, path: Path) -> int:
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0

        migrated = 0
        for item in payload.get("jobs", []):
            raw_url = item.get("url", "")
            job_id = extract_job_id_from_url(raw_url) or str(item.get("job_id", ""))
            if not job_id:
                continue
            if self.get_job(job_id) is not None:
                continue
            status = map_legacy_status(item.get("status", "new"))
            listing = JobListing(
                job_id=job_id,
                url=raw_url or f"https://www.linkedin.com/jobs/view/{job_id}/",
                title=item.get("title", ""),
                keyword=item.get("keyword", ""),
            )
            new_jobs = self.upsert_jobs([listing], now=item.get("first_seen_at") or utc_now())
            self.update_user_tracking(job_id, user_status=status)
            migrated += len(new_jobs)
        return migrated

    def upsert_jobs(
        self,
        listings: Iterable[JobListing],
        now: Optional[str] = None,
    ) -> List[JobRecord]:
        timestamp = now or utc_now()
        new_jobs: List[JobRecord] = []
        for listing in listings:
            existing = self.get_job(listing.job_id)
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, linkedin_url, title, company, company_url, location,
                        posted_at, posted_text, application_deadline, employment_type,
                        seniority_level, job_function, industries, applicants,
                        description, insight, source_keyword, source_url,
                        first_seen_at, last_seen_at, details_fetched_at,
                        user_status, application_status, applied_at, notes,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    job_insert_values(listing, timestamp),
                )
                inserted = self.get_job(listing.job_id)
                if inserted:
                    new_jobs.append(inserted)
            else:
                self.conn.execute(
                    """
                    UPDATE jobs SET
                        linkedin_url = COALESCE(NULLIF(?, ''), linkedin_url),
                        title = COALESCE(NULLIF(?, ''), title),
                        company = COALESCE(NULLIF(?, ''), company),
                        company_url = COALESCE(NULLIF(?, ''), company_url),
                        location = COALESCE(NULLIF(?, ''), location),
                        posted_at = COALESCE(NULLIF(?, ''), posted_at),
                        posted_text = COALESCE(NULLIF(?, ''), posted_text),
                        application_deadline = COALESCE(NULLIF(?, ''), application_deadline),
                        employment_type = COALESCE(NULLIF(?, ''), employment_type),
                        seniority_level = COALESCE(NULLIF(?, ''), seniority_level),
                        job_function = COALESCE(NULLIF(?, ''), job_function),
                        industries = COALESCE(NULLIF(?, ''), industries),
                        applicants = COALESCE(NULLIF(?, ''), applicants),
                        description = COALESCE(NULLIF(?, ''), description),
                        insight = COALESCE(NULLIF(?, ''), insight),
                        source_keyword = COALESCE(NULLIF(?, ''), source_keyword),
                        source_url = COALESCE(NULLIF(?, ''), source_url),
                        last_seen_at = ?,
                        details_fetched_at = CASE WHEN ? != '' THEN ? ELSE details_fetched_at END,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    job_update_values(listing, timestamp),
                )
        self.conn.commit()
        return new_jobs

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return row_to_job(row)

    def list_jobs(self, user_status: Optional[str] = None, include_hidden: bool = False) -> List[JobRecord]:
        params = []
        where = []
        if user_status:
            where.append("user_status = ?")
            params.append(user_status)
        elif not include_hidden:
            where.append("user_status IN (?, ?, ?)")
            params.extend([STATUS_NEW, STATUS_SAVED, STATUS_APPLIED])

        query = "SELECT * FROM jobs"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY COALESCE(NULLIF(posted_at, ''), last_seen_at) DESC, last_seen_at DESC"
        return [row_to_job(row) for row in self.conn.execute(query, params).fetchall()]

    def count_by_status(self, status: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE user_status = ?",
            (status,),
        ).fetchone()
        return int(row["count"])

    def total_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        return int(row["count"])

    def update_user_tracking(
        self,
        job_id: str,
        user_status: Optional[str] = None,
        application_status: Optional[str] = None,
        applied_at: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        if user_status and user_status not in VALID_USER_STATUSES:
            raise ValueError(f"Unsupported user status: {user_status}")

        updates = []
        params = []
        if user_status is not None:
            updates.append("user_status = ?")
            params.append(user_status)
        if application_status is not None:
            updates.append("application_status = ?")
            params.append(application_status)
        if applied_at is not None:
            updates.append("applied_at = ?")
            params.append(applied_at)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if not updates:
            return True

        updates.append("updated_at = ?")
        params.append(utc_now())
        params.append(job_id)
        self.conn.execute(
            f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?",
            params,
        )
        self.conn.commit()
        return True

    def mark_saved(self, job_id: str) -> bool:
        return self.update_user_tracking(job_id, user_status=STATUS_SAVED)

    def mark_new(self, job_id: str) -> bool:
        return self.update_user_tracking(job_id, user_status=STATUS_NEW)

    def mark_not_interested(self, job_id: str) -> bool:
        return self.update_user_tracking(job_id, user_status=STATUS_NOT_INTERESTED)

    def mark_applied(self, job_id: str, applied_at: Optional[str] = None) -> bool:
        return self.update_user_tracking(
            job_id,
            user_status=STATUS_APPLIED,
            application_status="applied",
            applied_at=applied_at or date.today().isoformat(),
        )

    def add_search_source(
        self,
        name: str,
        keywords: str,
        location: str,
        geo_id: str = "",
        ai_search_url: str = "",
        radius_km: int = 300,
        posted_within_days: int = 7,
        sort_by: str = "DD",
        is_active: bool = True,
    ) -> int:
        keyword_values = split_keywords(keywords)
        if not keyword_values:
            raise ValueError("Search source must include at least one keyword.")
        if not location.strip():
            raise ValueError("Search source must include a location.")
        timestamp = utc_now()
        cursor = self.conn.execute(
            """
            INSERT INTO search_sources (
                name, keywords, location, geo_id, ai_search_url, radius_km,
                posted_within_days, sort_by, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip() or ", ".join(keyword_values),
                ", ".join(keyword_values),
                location.strip(),
                geo_id.strip(),
                ai_search_url.strip(),
                max(1, int(radius_km)),
                max(1, int(posted_within_days)),
                sort_by.strip() or "DD",
                1 if is_active else 0,
                timestamp,
                timestamp,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def update_search_source(
        self,
        source_id: int,
        name: str,
        keywords: str,
        location: str,
        geo_id: str = "",
        ai_search_url: str = "",
        radius_km: int = 300,
        posted_within_days: int = 7,
        sort_by: str = "DD",
        is_active: bool = True,
    ) -> bool:
        if self.get_search_source(source_id) is None:
            return False
        keyword_values = split_keywords(keywords)
        if not keyword_values:
            raise ValueError("Search source must include at least one keyword.")
        if not location.strip():
            raise ValueError("Search source must include a location.")
        self.conn.execute(
            """
            UPDATE search_sources SET
                name = ?,
                keywords = ?,
                location = ?,
                geo_id = ?,
                ai_search_url = ?,
                radius_km = ?,
                posted_within_days = ?,
                sort_by = ?,
                is_active = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip() or ", ".join(keyword_values),
                ", ".join(keyword_values),
                location.strip(),
                geo_id.strip(),
                ai_search_url.strip(),
                max(1, int(radius_km)),
                max(1, int(posted_within_days)),
                sort_by.strip() or "DD",
                1 if is_active else 0,
                utc_now(),
                source_id,
            ),
        )
        self.conn.commit()
        return True

    def set_search_source_active(self, source_id: int, is_active: bool) -> bool:
        if self.get_search_source(source_id) is None:
            return False
        self.conn.execute(
            "UPDATE search_sources SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, utc_now(), source_id),
        )
        self.conn.commit()
        return True

    def delete_search_source(self, source_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM search_sources WHERE id = ?", (source_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_search_source_run(self, source_id: int, timestamp: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE search_sources SET last_run_at = ?, updated_at = ? WHERE id = ?",
            (timestamp or utc_now(), utc_now(), source_id),
        )
        self.conn.commit()

    def get_search_source(self, source_id: int) -> Optional[SearchSource]:
        row = self.conn.execute(
            "SELECT * FROM search_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        return row_to_search_source(row)

    def list_search_sources(self, active_only: bool = False) -> List[SearchSource]:
        query = "SELECT * FROM search_sources"
        params = []
        if active_only:
            query += " WHERE is_active = ?"
            params.append(1)
        query += " ORDER BY is_active DESC, updated_at DESC, id ASC"
        return [row_to_search_source(row) for row in self.conn.execute(query, params).fetchall()]


def open_database(
    db_file: Path,
    legacy_json_path: Optional[Path] = None,
    seed_search_config: Optional[SearchConfig] = None,
) -> JobDatabase:
    return JobDatabase(
        db_file,
        legacy_json_path=legacy_json_path,
        seed_search_config=seed_search_config,
    )


def job_insert_values(listing: JobListing, timestamp: str) -> tuple:
    details_fetched_at = timestamp if has_detail_fields(listing) else ""
    return (
        listing.job_id,
        listing.url,
        listing.title,
        listing.company,
        listing.company_url,
        listing.location,
        listing.posted_at,
        listing.posted_text,
        listing.application_deadline,
        listing.employment_type,
        listing.seniority_level,
        listing.job_function,
        listing.industries,
        listing.applicants,
        listing.description,
        listing.insight,
        listing.keyword,
        listing.source_url,
        timestamp,
        timestamp,
        details_fetched_at,
        STATUS_NEW,
        "",
        "",
        "",
        timestamp,
        timestamp,
    )


def job_update_values(listing: JobListing, timestamp: str) -> tuple:
    details_fetched_at = timestamp if has_detail_fields(listing) else ""
    return (
        listing.url,
        listing.title,
        listing.company,
        listing.company_url,
        listing.location,
        listing.posted_at,
        listing.posted_text,
        listing.application_deadline,
        listing.employment_type,
        listing.seniority_level,
        listing.job_function,
        listing.industries,
        listing.applicants,
        listing.description,
        listing.insight,
        listing.keyword,
        listing.source_url,
        timestamp,
        details_fetched_at,
        details_fetched_at,
        timestamp,
        listing.job_id,
    )


def has_detail_fields(listing: JobListing) -> bool:
    return bool(
        listing.application_deadline
        or listing.employment_type
        or listing.description
        or listing.industries
    )


def row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(**{key: row[key] for key in row.keys()})


def row_to_search_source(row: sqlite3.Row) -> SearchSource:
    values = {key: row[key] for key in row.keys()}
    values["is_active"] = bool(values["is_active"])
    return SearchSource(**values)


def map_legacy_status(status: str) -> str:
    mapping = {
        "new": STATUS_NEW,
        "saved": STATUS_SAVED,
        "deleted": STATUS_NOT_INTERESTED,
        "not_interested": STATUS_NOT_INTERESTED,
        "applied": STATUS_APPLIED,
    }
    return mapping.get(status, STATUS_NEW)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_keywords(value: str) -> List[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
