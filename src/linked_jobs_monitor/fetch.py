from __future__ import annotations

import time
from typing import Iterable, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .linkedin import SearchUrl


class FetchError(RuntimeError):
    pass


def fetch_url(url: str, user_agent: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": user_agent})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise FetchError(f"Could not fetch {url}: {exc.reason}") from exc


def fetch_searches(
    searches: Iterable[SearchUrl], user_agent: str, delay_seconds: float
) -> List[Tuple[SearchUrl, str]]:
    pages = []
    for index, search in enumerate(searches):
        if index:
            time.sleep(delay_seconds)
        pages.append((search, fetch_url(search.url, user_agent=user_agent)))
    return pages
