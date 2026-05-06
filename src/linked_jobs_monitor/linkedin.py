from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Iterable, List
from urllib.parse import urlencode

from .config import SearchConfig


SECONDS_PER_DAY = 86400
KM_PER_MILE = 1.609344


@dataclass(frozen=True)
class SearchUrl:
    keyword: str
    url: str
    source_id: int = 0
    source_name: str = ""


def km_to_linkedin_distance(radius_km: int) -> int:
    """Convert a requested kilometer radius to LinkedIn's distance parameter."""
    return max(1, ceil(radius_km / KM_PER_MILE))


def posted_within_filter(days: int) -> str:
    return f"r{max(1, days) * SECONDS_PER_DAY}"


def build_search_url(config: SearchConfig, keyword: str) -> str:
    return build_search_url_from_values(
        keyword=keyword,
        location=config.location,
        geo_id=config.geo_id,
        radius_km=config.radius_km,
        posted_within_days=config.posted_within_days,
        sort_by=config.sort_by,
    )


def build_search_url_from_values(
    keyword: str,
    location: str,
    geo_id: str,
    radius_km: int,
    posted_within_days: int,
    sort_by: str,
) -> str:
    query = {
        "keywords": keyword,
        "location": location,
        "geoId": geo_id,
        "distance": str(km_to_linkedin_distance(radius_km)),
        "f_TPR": posted_within_filter(posted_within_days),
        "sortBy": sort_by,
        "origin": "JOB_SEARCH_PAGE_SEARCH_BUTTON",
    }
    return "https://www.linkedin.com/jobs/search/?" + urlencode(query)


def build_search_urls(config: SearchConfig) -> List[SearchUrl]:
    return [
        SearchUrl(keyword=keyword, url=build_search_url(config, keyword))
        for keyword in config.keywords
    ]


def build_search_urls_for_source(source) -> List[SearchUrl]:
    return [
        SearchUrl(
            keyword=keyword,
            url=build_search_url_from_values(
                keyword=keyword,
                location=source.location,
                geo_id=source.geo_id,
                radius_km=source.radius_km,
                posted_within_days=source.posted_within_days,
                sort_by=source.sort_by,
            ),
            source_id=source.id,
            source_name=source.name,
        )
        for keyword in source.keyword_list()
    ]


def unique_urls(urls: Iterable[SearchUrl]) -> List[SearchUrl]:
    seen = set()
    result = []
    for item in urls:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result
