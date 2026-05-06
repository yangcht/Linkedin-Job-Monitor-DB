from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional
from urllib.parse import unquote


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SECONDS_PER_DAY = 86400

JOB_URL_RE = re.compile(
    r"https?://(?:[a-z]{2}\.)?linkedin\.com/jobs/view/[^\"'<\s)]+"
)
JOB_ID_RE = re.compile(r"urn:li:jobPosting:(\d+)")
JOB_ID_IN_URL_RE = re.compile(r"/jobs/view/(?:[^/?#]+-)?(\d+)(?:[/?#]|$)")
CURRENT_JOB_ID_RE = re.compile(r"(?:currentJobId|jobId)[=:](\d+)")
TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class JobListing:
    job_id: str
    url: str
    title: str = ""
    keyword: str = ""
    source_url: str = ""
    company: str = ""
    company_url: str = ""
    location: str = ""
    posted_at: str = ""
    posted_text: str = ""
    application_deadline: str = ""
    employment_type: str = ""
    seniority_level: str = ""
    job_function: str = ""
    industries: str = ""
    applicants: str = ""
    description: str = ""
    insight: str = ""


def extract_jobs(
    html_text: str,
    keyword: Optional[str] = None,
    source_url: str = "",
) -> List[JobListing]:
    """Extract LinkedIn jobs from search-result or detail-page HTML."""
    search_cards = extract_search_cards(html_text, keyword=keyword, source_url=source_url)
    detail_jobs = extract_detail_jobs(html_text, keyword=keyword, source_url=source_url)
    groups = [search_cards, detail_jobs]
    if not search_cards and not detail_jobs:
        groups.append(extract_fallback_jobs(html_text, keyword=keyword, source_url=source_url))
    return merge_jobs(groups)


def extract_search_cards(
    html_text: str,
    keyword: Optional[str] = None,
    source_url: str = "",
) -> List[JobListing]:
    parser = LinkedInSearchCardParser(keyword=keyword or "", source_url=source_url)
    parser.feed(html_text)
    return parser.jobs


def extract_detail_jobs(
    html_text: str,
    keyword: Optional[str] = None,
    source_url: str = "",
) -> List[JobListing]:
    job_id = extract_primary_job_id(html_text)
    if not job_id:
        return []

    canonical_url = extract_canonical_job_url(html_text) or f"https://www.linkedin.com/jobs/view/{job_id}/"
    details: Dict[str, str] = {}
    for item in extract_json_ld(html_text):
        if item.get("@type") != "JobPosting":
            continue
        details = normalize_json_ld_job(item)
        break

    dom_details = extract_dom_detail_fields(html_text)
    if not details:
        details = {key: value for key, value in dom_details.items() if value}
    if not details:
        return []
    for key, value in dom_details.items():
        if not value:
            continue
        if key in {"posted_text", "applicants", "seniority_level", "job_function"}:
            details[key] = value
        else:
            details.setdefault(key, value)

    return [
        JobListing(
            job_id=job_id,
            url=normalize_job_url(canonical_url, job_id),
            title=details.get("title", ""),
            keyword=keyword or "",
            source_url=source_url,
            company=details.get("company", ""),
            company_url=details.get("company_url", ""),
            location=details.get("location", ""),
            posted_at=details.get("posted_at", ""),
            posted_text=details.get("posted_text", ""),
            application_deadline=details.get("application_deadline", ""),
            employment_type=details.get("employment_type", ""),
            seniority_level=details.get("seniority_level", ""),
            job_function=details.get("job_function", ""),
            industries=details.get("industries", ""),
            applicants=details.get("applicants", ""),
            description=details.get("description", ""),
        )
    ]


def extract_fallback_jobs(
    html_text: str,
    keyword: Optional[str] = None,
    source_url: str = "",
) -> List[JobListing]:
    jobs: Dict[str, JobListing] = {}

    for match in JOB_URL_RE.finditer(html_text):
        raw_url = match.group(0)
        job_id = extract_job_id_from_url(raw_url)
        if not job_id:
            continue
        jobs[job_id] = JobListing(
            job_id=job_id,
            url=normalize_job_url(raw_url, job_id),
            title=extract_nearby_title(html_text, match.start()),
            keyword=keyword or "",
            source_url=source_url,
        )

    for match in JOB_ID_RE.finditer(html_text):
        job_id = match.group(1)
        jobs.setdefault(
            job_id,
            JobListing(
                job_id=job_id,
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                title=extract_nearby_title(html_text, match.start()),
                keyword=keyword or "",
                source_url=source_url,
            ),
        )

    return sorted(jobs.values(), key=lambda item: item.job_id)


def extract_title(html_text: str) -> Optional[str]:
    match = TITLE_RE.search(html_text)
    if not match:
        return None
    return clean_text(match.group(1))


def extract_job_id_from_url(raw_url: str) -> Optional[str]:
    decoded = html.unescape(unquote(raw_url))
    match = JOB_ID_IN_URL_RE.search(decoded)
    if match:
        return match.group(1)
    match = CURRENT_JOB_ID_RE.search(decoded)
    if match:
        return match.group(1)
    return None


def normalize_job_url(raw_url: str, job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def extract_primary_job_id(html_text: str) -> Optional[str]:
    canonical_url = extract_canonical_job_url(html_text)
    if canonical_url:
        job_id = extract_job_id_from_url(canonical_url)
        if job_id:
            return job_id

    og_url = extract_meta_content(html_text, "og:url")
    if og_url:
        job_id = extract_job_id_from_url(og_url)
        if job_id:
            return job_id

    match = JOB_ID_RE.search(html_text)
    if match:
        return match.group(1)
    return None


def extract_canonical_job_url(html_text: str) -> str:
    match = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    if match:
        return html.unescape(match.group(1))
    return extract_meta_content(html_text, "lnkd:url") or extract_meta_content(html_text, "og:url")


def extract_meta_content(html_text: str, property_name: str) -> str:
    pattern = (
        r'<meta[^>]+(?:property|name)=["\']'
        + re.escape(property_name)
        + r'["\'][^>]+content=["\']([^"\']+)["\']'
    )
    match = re.search(pattern, html_text, re.IGNORECASE)
    if not match:
        return ""
    return html.unescape(match.group(1))


def extract_json_ld(html_text: str) -> List[dict]:
    result = []
    for match in JSON_LD_RE.finditer(html_text):
        raw = match.group(1).strip()
        for candidate in (raw, html.unescape(raw)):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                result.extend(item for item in parsed if isinstance(item, dict))
            elif isinstance(parsed, dict):
                result.append(parsed)
            break
    return result


def normalize_json_ld_job(item: dict) -> Dict[str, str]:
    organization = item.get("hiringOrganization") or {}
    location = format_json_ld_location(item.get("jobLocation"))
    return {
        "title": clean_text(str(item.get("title", ""))),
        "company": clean_text(str(organization.get("name", ""))),
        "company_url": html.unescape(str(organization.get("sameAs", ""))),
        "location": location,
        "posted_at": clean_text(str(item.get("datePosted", ""))),
        "application_deadline": clean_text(str(item.get("validThrough", ""))),
        "employment_type": clean_text(str(item.get("employmentType", ""))),
        "industries": clean_text(str(item.get("industry", ""))),
        "description": clean_text(str(item.get("description", ""))),
    }


def extract_dom_detail_fields(html_text: str) -> Dict[str, str]:
    details = {
        "title": extract_class_text(html_text, "top-card-layout__title"),
        "company": extract_class_text(html_text, "topcard__org-name-link"),
        "company_url": extract_first_class_href(html_text, "topcard__org-name-link"),
        "location": extract_topcard_location(html_text),
        "posted_text": extract_class_text(html_text, "posted-time-ago__text"),
        "applicants": extract_class_text(html_text, "num-applicants__caption"),
        "description": extract_class_text(html_text, "description__text"),
    }
    criteria = extract_job_criteria(html_text)
    details.update(criteria)
    return details


def extract_job_criteria(html_text: str) -> Dict[str, str]:
    criteria: Dict[str, str] = {}
    pattern = re.compile(
        r'<li[^>]+class=["\'][^"\']*description__job-criteria-item[^"\']*["\'][^>]*>'
        r'(.*?)</li>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text):
        block = match.group(1)
        label = clean_text(extract_class_text(block, "description__job-criteria-subheader")).lower()
        value = clean_text(extract_class_text(block, "description__job-criteria-text"))
        if not label or not value:
            continue
        if "seniority" in label:
            criteria["seniority_level"] = value
        elif "employment" in label:
            criteria["employment_type"] = value
        elif "function" in label:
            criteria["job_function"] = value
        elif "industr" in label:
            criteria["industries"] = value
    return criteria


def extract_class_text(html_text: str, class_fragment: str) -> str:
    pattern = re.compile(
        r'<(?P<tag>[a-z0-9]+)[^>]+class=["\'][^"\']*'
        + re.escape(class_fragment)
        + r'[^"\']*["\'][^>]*>(?P<body>.*?)</(?P=tag)>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html_text)
    if not match:
        return ""
    return clean_text(match.group("body"))


def extract_first_class_href(html_text: str, class_fragment: str) -> str:
    pattern = re.compile(
        r'<a[^>]+class=["\'][^"\']*'
        + re.escape(class_fragment)
        + r'[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html_text)
    if not match:
        return ""
    return html.unescape(match.group(1))


def extract_topcard_location(html_text: str) -> str:
    pattern = re.compile(
        r'<span[^>]+class=["\'][^"\']*topcard__flavor--bullet[^"\']*["\'][^>]*>(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html_text)
    if not match:
        return ""
    return clean_text(match.group(1))


def format_json_ld_location(value) -> str:
    if isinstance(value, list):
        return "; ".join(filter(None, [format_json_ld_location(item) for item in value]))
    if not isinstance(value, dict):
        return ""
    address = value.get("address") or {}
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry"),
    ]
    return clean_text(", ".join(str(part) for part in parts if part))


def extract_nearby_title(html_text: str, index: int) -> str:
    window = html_text[max(0, index - 1200) : index + 1200]
    patterns = [
        r'<h3[^>]*class=["\'][^"\']*base-search-card__title[^"\']*["\'][^>]*>(.*?)</h3>',
        r'aria-label="([^"]+)"',
        r'alt="([^"]+)"',
        r"<h3[^>]*>(.*?)</h3>",
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, window, re.IGNORECASE | re.DOTALL))
        if not matches:
            continue
        text = clean_text(matches[-1].group(1))
        if text and not looks_like_navigation(text):
            return text
    return ""


def clean_text(value: str) -> str:
    previous = None
    while previous != value:
        previous = value
        value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"</(?:li|p|div|h\d)>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    previous = None
    while previous != value:
        previous = value
        value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def looks_like_navigation(text: str) -> bool:
    lowered = text.lower()
    blocked = ["linkedin", "sign in", "join now", "jobs", "search"]
    return any(item == lowered for item in blocked)


def merge_jobs(groups: Iterable[Iterable[JobListing]]) -> List[JobListing]:
    merged: Dict[str, JobListing] = {}
    for group in groups:
        for job in group:
            existing = merged.get(job.job_id)
            if existing is None:
                merged[job.job_id] = job
            else:
                merged[job.job_id] = merge_job(existing, job)
    return sorted(merged.values(), key=lambda item: item.job_id)


def merge_job(existing: JobListing, incoming: JobListing) -> JobListing:
    values = {}
    for field in existing.__dataclass_fields__:
        current_value = getattr(existing, field)
        incoming_value = getattr(incoming, field)
        values[field] = current_value or incoming_value
    return replace(existing, **values)


class LinkedInSearchCardParser(HTMLParser):
    def __init__(self, keyword: str, source_url: str) -> None:
        super().__init__(convert_charrefs=False)
        self.keyword = keyword
        self.source_url = source_url
        self.jobs: List[JobListing] = []
        self.current: Optional[Dict[str, str]] = None
        self.depth = 0
        self.capture_field: Optional[str] = None
        self.capture_tag: Optional[str] = None
        self.capture_data: List[str] = []
        self.in_company = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        class_name = attr.get("class") or ""

        if self.current is None and tag == "div" and "job-search-card" in class_name:
            self.current = {
                "job_id": "",
                "url": "",
                "title": "",
                "company": "",
                "company_url": "",
                "location": "",
                "posted_at": "",
                "posted_text": "",
                "insight": "",
            }
            self.depth = 1
            urn = attr.get("data-entity-urn", "")
            match = JOB_ID_RE.search(urn)
            if match:
                self.current["job_id"] = match.group(1)
            return

        if self.current is None:
            return

        if tag not in VOID_TAGS:
            self.depth += 1

        href = attr.get("href", "")
        if tag == "a" and href:
            if "/jobs/view/" in href:
                job_id = extract_job_id_from_url(href)
                if job_id:
                    self.current["job_id"] = self.current["job_id"] or job_id
                    self.current["url"] = normalize_job_url(href, job_id)
            elif self.in_company and not self.current["company_url"]:
                self.current["company_url"] = html.unescape(href)

        if tag == "h3" and "base-search-card__title" in class_name:
            self.start_capture("title", tag)
        elif tag == "h4" and "base-search-card__subtitle" in class_name:
            self.in_company = True
            self.start_capture("company", tag)
        elif tag == "span" and "job-search-card__location" in class_name:
            self.start_capture("location", tag)
        elif tag == "span" and "job-posting-benefits__text" in class_name:
            self.start_capture("insight", tag)
        elif tag == "time" and "job-search-card__listdate" in class_name:
            self.current["posted_at"] = attr.get("datetime", "")
            self.start_capture("posted_text", tag)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return

        if self.capture_tag == tag:
            self.current[self.capture_field or ""] = clean_text("".join(self.capture_data))
            if self.capture_field == "company":
                self.in_company = False
            self.capture_field = None
            self.capture_tag = None
            self.capture_data = []

        if tag not in VOID_TAGS:
            self.depth -= 1

        if self.depth == 0:
            self.finish_current()

    def handle_data(self, data: str) -> None:
        if self.capture_field:
            self.capture_data.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.capture_field:
            self.capture_data.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.capture_field:
            self.capture_data.append(f"&#{name};")

    def start_capture(self, field: str, tag: str) -> None:
        self.capture_field = field
        self.capture_tag = tag
        self.capture_data = []

    def finish_current(self) -> None:
        if not self.current:
            return
        job_id = self.current.get("job_id", "")
        if job_id:
            self.jobs.append(
                JobListing(
                    job_id=job_id,
                    url=self.current.get("url") or f"https://www.linkedin.com/jobs/view/{job_id}/",
                    title=self.current.get("title", ""),
                    keyword=self.keyword,
                    source_url=self.source_url,
                    company=self.current.get("company", ""),
                    company_url=self.current.get("company_url", ""),
                    location=self.current.get("location", ""),
                    posted_at=self.current.get("posted_at", ""),
                    posted_text=self.current.get("posted_text", ""),
                    insight=self.current.get("insight", ""),
                )
            )
        self.current = None
        self.depth = 0
        self.capture_field = None
        self.capture_tag = None
        self.capture_data = []
        self.in_company = False
