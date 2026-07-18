"""Focused, rate-limited access to the public arXiv API."""

import copy
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from research_desk.config import (
    ARXIV_API_URL,
    ARXIV_CACHE_TTL_SECONDS,
    ARXIV_MIN_PAPER_YEAR,
    ARXIV_MIN_REQUEST_INTERVAL_SECONDS,
    ARXIV_REQUEST_TIMEOUT_SECONDS,
    XML_NAMESPACES,
)

CacheKey = tuple[str, str, tuple[str, ...], int]
CacheValue = tuple[float, int, list[dict[str, Any]]]

_request_lock = threading.Lock()
_last_request_started = 0.0
_response_cache: dict[CacheKey, CacheValue] = {}


def normalize_whitespace(value: str) -> str:
    """Collapse repeated whitespace so API results are easy to read."""

    return " ".join(value.split())


def sanitize_arxiv_phrase(value: str) -> str:
    """Remove query-control characters from a phrase used in arXiv search."""

    without_control_characters = re.sub(r'["\\]+', " ", value)
    return normalize_whitespace(without_control_characters)


def build_arxiv_search_query(
    idea: dict[str, Any],
    now: datetime | None = None,
) -> str:
    """Build a topic query with a hard arXiv submission-date filter."""

    current_time = now or datetime.now(timezone.utc)
    end_date = current_time.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    title = sanitize_arxiv_phrase(str(idea.get("title", "")))
    tags: list[str] = []

    for tag in idea.get("tags", []):
        sanitized_tag = sanitize_arxiv_phrase(str(tag))
        if sanitized_tag:
            tags.append(sanitized_tag)

    topic_clauses: list[str] = []

    if title:
        topic_clauses.extend([f'ti:"{title}"', f'abs:"{title}"'])

    if tags:
        combined_tags = " AND ".join(f'all:"{tag}"' for tag in tags)
        topic_clauses.append(f"({combined_tags})")
        topic_clauses.extend(f'all:"{tag}"' for tag in tags if len(tag.split()) > 1)

    if not topic_clauses:
        problem = sanitize_arxiv_phrase(str(idea.get("problem", "")))
        if problem:
            topic_clauses.append(f'all:"{problem}"')

    if not topic_clauses:
        raise ValueError("The selected idea has no searchable title, tags or problem.")

    topic_query = " OR ".join(topic_clauses)
    date_query = f"submittedDate:[{ARXIV_MIN_PAPER_YEAR}01010000 TO {end_date}]"
    return f"({topic_query}) AND {date_query}"


def canonical_arxiv_links(entry_id: str) -> tuple[str, str, str] | None:
    """Return a safe arXiv ID, HTTPS abstract URL and HTTPS PDF URL."""

    parsed_url = urlparse(entry_id)
    if parsed_url.hostname not in {
        "arxiv.org",
        "www.arxiv.org",
        "export.arxiv.org",
    }:
        return None
    if not parsed_url.path.startswith("/abs/"):
        return None

    arxiv_id = parsed_url.path.removeprefix("/abs/").strip("/")
    if not arxiv_id:
        return None

    return (
        arxiv_id,
        f"https://arxiv.org/abs/{arxiv_id}",
        f"https://arxiv.org/pdf/{arxiv_id}",
    )


def parse_arxiv_response(xml_content: bytes) -> tuple[int, list[dict[str, Any]]]:
    """Parse an Atom response and independently enforce the publication year."""

    root = ElementTree.fromstring(xml_content)
    total_results_text = root.findtext(
        "opensearch:totalResults",
        default="0",
        namespaces=XML_NAMESPACES,
    )

    try:
        total_results = int(total_results_text)
    except (TypeError, ValueError):
        total_results = 0

    papers: list[dict[str, Any]] = []

    for entry in root.findall("atom:entry", XML_NAMESPACES):
        entry_id = normalize_whitespace(
            entry.findtext("atom:id", default="", namespaces=XML_NAMESPACES)
        )

        if "/api/errors" in entry_id:
            error_summary = normalize_whitespace(
                entry.findtext(
                    "atom:summary",
                    default="Unknown arXiv API error.",
                    namespaces=XML_NAMESPACES,
                )
            )
            raise ValueError(error_summary)

        published = normalize_whitespace(
            entry.findtext(
                "atom:published",
                default="",
                namespaces=XML_NAMESPACES,
            )
        )

        try:
            published_year = datetime.fromisoformat(
                published.replace("Z", "+00:00")
            ).year
        except ValueError:
            continue

        if published_year < ARXIV_MIN_PAPER_YEAR:
            continue

        arxiv_links = canonical_arxiv_links(entry_id)
        if arxiv_links is None:
            continue

        arxiv_id, arxiv_url, pdf_url = arxiv_links
        authors = [
            normalize_whitespace(
                author.findtext(
                    "atom:name",
                    default="",
                    namespaces=XML_NAMESPACES,
                )
            )
            for author in entry.findall("atom:author", XML_NAMESPACES)
        ]
        categories = [
            category.get("term", "").strip()
            for category in entry.findall("atom:category", XML_NAMESPACES)
            if category.get("term", "").strip()
        ]
        primary_category_element = entry.find(
            "arxiv:primary_category",
            XML_NAMESPACES,
        )
        primary_category = (
            primary_category_element.get("term", "").strip()
            if primary_category_element is not None
            else ""
        )

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": normalize_whitespace(
                    entry.findtext(
                        "atom:title",
                        default="",
                        namespaces=XML_NAMESPACES,
                    )
                ),
                "authors": [author for author in authors if author],
                "published": published[:10],
                "updated": normalize_whitespace(
                    entry.findtext(
                        "atom:updated",
                        default="",
                        namespaces=XML_NAMESPACES,
                    )
                )[:10],
                "summary": normalize_whitespace(
                    entry.findtext(
                        "atom:summary",
                        default="",
                        namespaces=XML_NAMESPACES,
                    )
                ),
                "primary_category": primary_category,
                "categories": categories,
                "arxiv_url": arxiv_url,
                "pdf_url": pdf_url,
            }
        )

    return total_results, papers


def _cache_key(idea: dict[str, Any], max_results: int) -> CacheKey:
    return (
        str(idea.get("title", "")),
        str(idea.get("problem", "")),
        tuple(str(tag) for tag in idea.get("tags", [])),
        max_results,
    )


def clear_arxiv_cache() -> None:
    """Clear in-memory API state; primarily useful for deterministic tests."""

    global _last_request_started
    with _request_lock:
        _response_cache.clear()
        _last_request_started = 0.0


def request_arxiv_papers(
    idea: dict[str, Any],
    max_results: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Return cached results or make one polite, rate-limited arXiv request."""

    global _last_request_started
    key = _cache_key(idea, max_results)

    with _request_lock:
        monotonic_now = time.monotonic()
        cached = _response_cache.get(key)
        if cached and monotonic_now - cached[0] < ARXIV_CACHE_TTL_SECONDS:
            return cached[1], copy.deepcopy(cached[2])

        wait_seconds = ARXIV_MIN_REQUEST_INTERVAL_SECONDS - (
            monotonic_now - _last_request_started
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        search_query = build_arxiv_search_query(idea)
        query_parameters = urlencode(
            {
                "search_query": search_query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        request = Request(
            f"{ARXIV_API_URL}?{query_parameters}",
            headers={
                "Accept": "application/atom+xml",
                "User-Agent": (
                    "ResearchDeskMCP/1.0 (educational research-paper search)"
                ),
            },
        )

        _last_request_started = time.monotonic()
        with urlopen(request, timeout=ARXIV_REQUEST_TIMEOUT_SECONDS) as response:
            total_results, papers = parse_arxiv_response(response.read())

        _response_cache[key] = (
            time.monotonic(),
            total_results,
            copy.deepcopy(papers),
        )
        return total_results, papers
