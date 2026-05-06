from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import List


DEFAULT_CONFIG = Path("config.ini")


@dataclass(frozen=True)
class SearchConfig:
    keywords: List[str]
    location: str
    geo_id: str
    ai_search_url: str
    radius_km: int
    posted_within_days: int
    sort_by: str


@dataclass(frozen=True)
class RunConfig:
    db_file: Path
    state_file: Path
    report_dir: Path
    request_delay_seconds: float
    user_agent: str


@dataclass(frozen=True)
class AppConfig:
    search: SearchConfig
    run: RunConfig


def load_config(path: Path = DEFAULT_CONFIG) -> AppConfig:
    parser = ConfigParser(interpolation=None)
    read_files = parser.read(path)
    if not read_files:
        raise FileNotFoundError(
            f"Could not read {path}. Copy config.example.ini to config.ini first."
        )

    search = parser["search"]
    run = parser["run"]

    keywords = [
        item.strip()
        for item in search.get("keywords", "").split(",")
        if item.strip()
    ]
    if not keywords:
        raise ValueError("Config must include at least one keyword in [search].")

    return AppConfig(
        search=SearchConfig(
            keywords=keywords,
            location=search.get(
                "location", "Gothenburg, Västra Götaland County, Sweden"
            ),
            geo_id=search.get("geo_id", "90009607"),
            ai_search_url=search.get("ai_search_url", ""),
            radius_km=search.getint("radius_km", fallback=300),
            posted_within_days=search.getint("posted_within_days", fallback=7),
            sort_by=search.get("sort_by", "DD"),
        ),
        run=RunConfig(
            db_file=Path(run.get("db_file", ".job_state/jobs.sqlite3")),
            state_file=Path(run.get("state_file", ".job_state/jobs.json")),
            report_dir=Path(run.get("report_dir", "reports")),
            request_delay_seconds=run.getfloat("request_delay_seconds", fallback=2),
            user_agent=run.get("user_agent", "Mozilla/5.0 linked-jobs-monitor/0.1"),
        ),
    )
