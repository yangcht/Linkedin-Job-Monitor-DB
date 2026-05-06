from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List

from .parser import JobListing


STATUS_NEW = "new"
STATUS_SAVED = "saved"
STATUS_DELETED = "deleted"
VISIBLE_STATUSES = {STATUS_NEW, STATUS_SAVED}


@dataclass(frozen=True)
class StoredJob:
    job_id: str
    url: str
    first_seen_at: str
    title: str = ""
    keyword: str = ""
    status: str = STATUS_NEW


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.jobs: Dict[str, StoredJob] = {}

    @classmethod
    def load(cls, path: Path) -> "JobStore":
        store = cls(path)
        if not path.exists():
            return store
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("jobs", []):
            job = StoredJob(
                job_id=item["job_id"],
                url=item["url"],
                first_seen_at=item["first_seen_at"],
                title=item.get("title", ""),
                keyword=item.get("keyword", ""),
                status=item.get("status", STATUS_NEW),
            )
            store.jobs[job.job_id] = job
        return store

    def add_new(self, listings: Iterable[JobListing]) -> List[StoredJob]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_jobs = []
        for listing in listings:
            if listing.job_id in self.jobs:
                continue
            stored = StoredJob(
                job_id=listing.job_id,
                url=listing.url,
                first_seen_at=now,
                title=listing.title or "",
                keyword=listing.keyword or "",
            )
            self.jobs[listing.job_id] = stored
            new_jobs.append(stored)
        return new_jobs

    def inbox_jobs(self) -> List[StoredJob]:
        return self.jobs_with_status(STATUS_NEW)

    def saved_jobs(self) -> List[StoredJob]:
        return self.jobs_with_status(STATUS_SAVED)

    def visible_jobs(self) -> List[StoredJob]:
        return [
            job
            for job in self.sorted_jobs()
            if job.status in VISIBLE_STATUSES
        ]

    def deleted_count(self) -> int:
        return len(self.jobs_with_status(STATUS_DELETED))

    def mark_saved(self, job_id: str) -> bool:
        return self.set_status(job_id, STATUS_SAVED)

    def mark_new(self, job_id: str) -> bool:
        return self.set_status(job_id, STATUS_NEW)

    def mark_deleted(self, job_id: str) -> bool:
        return self.set_status(job_id, STATUS_DELETED)

    def set_status(self, job_id: str, status: str) -> bool:
        if status not in {STATUS_NEW, STATUS_SAVED, STATUS_DELETED}:
            raise ValueError(f"Unsupported job status: {status}")
        job = self.jobs.get(job_id)
        if job is None:
            return False
        self.jobs[job_id] = replace(job, status=status)
        return True

    def jobs_with_status(self, status: str) -> List[StoredJob]:
        return [
            job
            for job in self.sorted_jobs()
            if job.status == status
        ]

    def sorted_jobs(self) -> List[StoredJob]:
        return sorted(
            self.jobs.values(),
            key=lambda item: (item.first_seen_at, item.job_id),
            reverse=True,
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "jobs": [
                asdict(job)
                for job in sorted(self.jobs.values(), key=lambda item: item.job_id)
            ]
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
