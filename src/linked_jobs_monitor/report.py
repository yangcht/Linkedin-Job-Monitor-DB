from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

from .database import JobRecord


def format_jobs(jobs: Iterable[JobRecord]) -> str:
    lines = []
    for job in jobs:
        label = job.title or f"LinkedIn job {job.job_id}"
        keyword = f" [{job.source_keyword}]" if job.source_keyword else ""
        company = f" - {job.company}" if job.company else ""
        lines.append(f"-{keyword} [{label}]({job.linkedin_url}){company}")
    return "\n".join(lines)


def write_report(report_dir: Path, new_jobs: List[JobRecord]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    path = report_dir / f"linkedin-new-jobs-{timestamp}.md"
    if new_jobs:
        body = "# New LinkedIn Jobs\n\n" + format_jobs(new_jobs) + "\n"
    else:
        body = "# New LinkedIn Jobs\n\nNo new jobs found in this run.\n"
    path.write_text(body, encoding="utf-8")
    return path
