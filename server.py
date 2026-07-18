import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------
# 1. Create the MCP server
# ---------------------------------------------------------

mcp = FastMCP("Research Desk MCP Server")


# ---------------------------------------------------------
# 2. Configure local data storage
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "research_ideas.json"
DOWNLOADS_DIR = Path.home() / "Downloads"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_MIN_PAPER_YEAR = 2025
ARXIV_MAX_RESULTS = 25
ARXIV_REQUEST_TIMEOUT_SECONDS = 30

XML_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


def load_ideas() -> list[dict[str, Any]]:
    """Load research ideas from the local JSON file."""

    DATA_DIR.mkdir(exist_ok=True)

    if not DATA_FILE.exists():
        DATA_FILE.write_text("[]", encoding="utf-8")

    try:
        content = DATA_FILE.read_text(encoding="utf-8")
        return json.loads(content)

    except json.JSONDecodeError as error:
        raise ValueError("research_ideas.json contains invalid JSON.") from error


def save_ideas(ideas: list[dict[str, Any]]) -> None:
    """Save research ideas to the local JSON file."""

    DATA_DIR.mkdir(exist_ok=True)

    DATA_FILE.write_text(
        json.dumps(ideas, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_idea_by_id(idea_id: int) -> dict[str, Any] | None:
    """Return one saved research idea by its numeric ID."""

    return next(
        (idea for idea in load_ideas() if idea.get("id") == idea_id),
        None,
    )


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
    tags = [
        sanitize_arxiv_phrase(str(tag))
        for tag in idea.get("tags", [])
        if sanitize_arxiv_phrase(str(tag))
    ]

    topic_clauses: list[str] = []

    if title:
        topic_clauses.extend(
            [
                f'ti:"{title}"',
                f'abs:"{title}"',
            ]
        )

    # A combined tag clause favors papers that match the whole topic. Individual
    # multi-word tags provide a useful fallback when the exact title is uncommon.
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
    """Return safe HTTPS arXiv ID, abstract URL and PDF URL."""

    parsed_url = urlparse(entry_id)

    if parsed_url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return None

    path_prefix = "/abs/"
    if not parsed_url.path.startswith(path_prefix):
        return None

    arxiv_id = parsed_url.path.removeprefix(path_prefix).strip("/")
    if not arxiv_id:
        return None

    return (
        arxiv_id,
        f"https://arxiv.org/abs/{arxiv_id}",
        f"https://arxiv.org/pdf/{arxiv_id}",
    )


def parse_arxiv_response(xml_content: bytes) -> tuple[int, list[dict[str, Any]]]:
    """Parse an arXiv Atom response and enforce the minimum publication year."""

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

        # This second check protects the invariant even if the remote date query
        # ever returns an unexpected older result.
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
        authors = [author for author in authors if author]

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
                "authors": authors,
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


def request_arxiv_papers(
    idea: dict[str, Any],
    max_results: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Call the public arXiv API and return parsed, arXiv-only results."""

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
            "User-Agent": "ResearchDeskMCP/1.0 (educational research-paper search)",
        },
    )

    with urlopen(request, timeout=ARXIV_REQUEST_TIMEOUT_SECONDS) as response:
        return parse_arxiv_response(response.read())


def slugify_filename(value: str) -> str:
    """Create a short, filesystem-safe slug for a report filename."""

    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:70] or "research-idea"


def build_arxiv_report_markdown(
    idea: dict[str, Any],
    papers: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> str:
    """Build a polished Markdown report from an idea and arXiv results."""

    report_time = generated_at or datetime.now(timezone.utc)
    title = normalize_whitespace(str(idea.get("title", "Untitled Research Idea")))
    problem = normalize_whitespace(str(idea.get("problem", "Not provided.")))
    tags = [normalize_whitespace(str(tag)) for tag in idea.get("tags", [])]
    tags = [tag for tag in tags if tag]

    lines = [
        f"# {title}",
        "",
        "## Research Idea",
        "",
        f"- **Idea ID:** {idea.get('id', 'N/A')}",
        f"- **Priority:** {idea.get('priority', 'N/A')}",
        f"- **Status:** {idea.get('status', 'N/A')}",
        f"- **Tags:** {', '.join(tags) if tags else 'None'}",
        f"- **Report generated:** {report_time.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Research Problem",
        "",
        problem,
        "",
        "## Literature Scope",
        "",
        (
            f"This report contains {len(papers)} arXiv paper(s) originally "
            f"published in {ARXIV_MIN_PAPER_YEAR} or later."
        ),
        "",
        "## Papers",
        "",
    ]

    if not papers:
        lines.extend(
            [
                "No matching papers were found for this research idea.",
                "",
            ]
        )

    for index, paper in enumerate(papers, start=1):
        authors = paper.get("authors", [])
        author_text = ", ".join(authors) if authors else "Not listed"
        categories = paper.get("categories", [])
        category_text = ", ".join(categories) if categories else "Not listed"
        summary = normalize_whitespace(str(paper.get("summary", "")))

        lines.extend(
            [
                f"### {index}. {paper.get('title', 'Untitled Paper')}",
                "",
                f"- **Authors:** {author_text}",
                f"- **Published:** {paper.get('published', 'Unknown')}",
                f"- **Updated:** {paper.get('updated', 'Unknown')}",
                f"- **Primary category:** {paper.get('primary_category') or 'Not listed'}",
                f"- **Categories:** {category_text}",
                f"- **arXiv ID:** {paper.get('arxiv_id', 'Unknown')}",
                "",
                "**Abstract**",
                "",
                summary or "No abstract was provided.",
                "",
                f"[View on arXiv]({paper.get('arxiv_url', '')}) | "
                f"[Download PDF]({paper.get('pdf_url', '')})",
                "",
                "---",
                "",
            ]
        )

    lines.extend(
        [
            "*Generated by Research Desk MCP.*",
            "",
            "Thank you to arXiv for use of its open-access interoperability.",
            "",
        ]
    )

    return "\n".join(lines)


def write_arxiv_report(
    idea: dict[str, Any],
    papers: list[dict[str, Any]],
    output_dir: Path | None = None,
    generated_at: datetime | None = None,
) -> Path:
    """Write a uniquely named Markdown report to the Downloads directory."""

    report_time = generated_at or datetime.now(timezone.utc)
    destination = output_dir or DOWNLOADS_DIR
    destination.mkdir(parents=True, exist_ok=True)

    idea_id = int(idea["id"])
    title_slug = slugify_filename(str(idea.get("title", "")))
    timestamp = report_time.strftime("%Y%m%d-%H%M%S")
    base_name = f"research-desk-idea-{idea_id:03d}-{title_slug}-{timestamp}"
    report_path = destination / f"{base_name}.md"
    duplicate_number = 2

    while report_path.exists():
        report_path = destination / f"{base_name}-{duplicate_number}.md"
        duplicate_number += 1

    report_path.write_text(
        build_arxiv_report_markdown(idea, papers, report_time),
        encoding="utf-8",
    )

    return report_path


# ---------------------------------------------------------
# 3. MCP TOOL: Add a new research idea
# ---------------------------------------------------------


@mcp.tool()
def add_research_idea(
    title: str,
    problem: str,
    tags: str,
    priority: int = 3,
) -> dict[str, Any]:
    """
    Save a new research idea.

    Args:
        title: A short title for the research idea.
        problem: The research problem the idea will address.
        tags: Comma-separated tags, for example AI, cloud, systems.
        priority: Priority from 1 to 5, where 5 is highest.
    """

    title = title.strip()
    problem = problem.strip()

    if not title:
        return {
            "success": False,
            "message": "Title cannot be empty.",
        }

    if not problem:
        return {
            "success": False,
            "message": "Research problem cannot be empty.",
        }

    if priority < 1 or priority > 5:
        return {
            "success": False,
            "message": "Priority must be between 1 and 5.",
        }

    ideas = load_ideas()

    next_id = (
        max(
            (idea["id"] for idea in ideas),
            default=0,
        )
        + 1
    )

    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

    new_idea = {
        "id": next_id,
        "title": title,
        "problem": problem,
        "tags": tag_list,
        "priority": priority,
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    ideas.append(new_idea)
    save_ideas(ideas)

    return {
        "success": True,
        "message": "Research idea saved successfully.",
        "idea": new_idea,
    }


# ---------------------------------------------------------
# 4. MCP TOOL: Search research ideas
# ---------------------------------------------------------


@mcp.tool()
def search_research_ideas(keyword: str) -> dict[str, Any]:
    """
    Search saved research ideas using a keyword.

    Args:
        keyword: Word or phrase to search in titles, problems and tags.
    """

    keyword = keyword.strip().casefold()

    if not keyword:
        return {
            "success": False,
            "message": "Search keyword cannot be empty.",
            "count": 0,
            "ideas": [],
        }

    ideas = load_ideas()
    matched_ideas = []

    for idea in ideas:
        searchable_text = " ".join(
            [
                idea["title"],
                idea["problem"],
                " ".join(idea["tags"]),
                idea["status"],
            ]
        ).casefold()

        if keyword in searchable_text:
            matched_ideas.append(idea)

    return {
        "success": True,
        "keyword": keyword,
        "count": len(matched_ideas),
        "ideas": matched_ideas,
    }


# ---------------------------------------------------------
# 5. MCP TOOL: Find recent arXiv papers for an idea ID
# ---------------------------------------------------------


@mcp.tool()
def find_arxiv_papers(
    idea_id: int,
    max_results: int = 10,
) -> dict[str, Any]:
    """
    Find arXiv papers published in 2025 or later for a saved idea.

    Args:
        idea_id: Numeric ID of the research idea in research_ideas.json.
        max_results: Number of papers to return, from 1 to 25.
    """

    if max_results < 1 or max_results > ARXIV_MAX_RESULTS:
        return {
            "success": False,
            "message": (f"max_results must be between 1 and {ARXIV_MAX_RESULTS}."),
            "count": 0,
            "papers": [],
        }

    idea = get_idea_by_id(idea_id)
    if idea is None:
        return {
            "success": False,
            "message": f"No research idea found with ID {idea_id}.",
            "count": 0,
            "papers": [],
        }

    try:
        total_matches, papers = request_arxiv_papers(idea, max_results)
    except HTTPError as error:
        return {
            "success": False,
            "message": f"arXiv API returned HTTP {error.code}. Try again later.",
            "idea_id": idea_id,
            "count": 0,
            "papers": [],
        }
    except (URLError, TimeoutError, OSError):
        return {
            "success": False,
            "message": (
                "Could not reach the arXiv API. Check the internet connection "
                "and try again."
            ),
            "idea_id": idea_id,
            "count": 0,
            "papers": [],
        }
    except (ElementTree.ParseError, ValueError) as error:
        return {
            "success": False,
            "message": f"Could not process the arXiv response: {error}",
            "idea_id": idea_id,
            "count": 0,
            "papers": [],
        }

    report_path: Path | None = None
    report_error: str | None = None

    try:
        report_path = write_arxiv_report(idea, papers)
    except OSError as error:
        # Paper discovery remains useful even if the local export is blocked.
        report_error = str(error)

    report_message = (
        f" Markdown report saved to {report_path}."
        if report_path is not None
        else " Markdown report could not be saved to Downloads."
    )

    return {
        "success": True,
        "message": (
            f"Found {len(papers)} arXiv paper(s) published in "
            f"{ARXIV_MIN_PAPER_YEAR} or later.{report_message}"
        ),
        "idea_id": idea_id,
        "topic": idea.get("title", ""),
        "minimum_year": ARXIV_MIN_PAPER_YEAR,
        "total_arxiv_matches": total_matches,
        "count": len(papers),
        "papers": papers,
        "report_created": report_path is not None,
        "report_path": str(report_path) if report_path is not None else None,
        "report_error": report_error,
    }


# ---------------------------------------------------------
# 6. MCP TOOL: Create a Markdown report in Downloads
# ---------------------------------------------------------


@mcp.tool()
def create_arxiv_papers_report(
    idea_id: int,
    max_results: int = 10,
) -> dict[str, Any]:
    """
    Find recent arXiv papers and save a Markdown report in Downloads.

    Args:
        idea_id: Numeric ID of the research idea in research_ideas.json.
        max_results: Number of papers to include, from 1 to 25.
    """

    search_result = find_arxiv_papers(idea_id, max_results)
    if not search_result.get("success"):
        return {
            **search_result,
            "report_created": False,
            "report_path": None,
        }

    if not search_result.get("report_created"):
        return {
            **search_result,
            "success": False,
            "message": (
                "Papers were found, but the Markdown report could not be "
                "saved to Downloads."
            ),
        }

    return {
        **search_result,
        "message": (
            f"Created a Markdown report with {search_result.get('count', 0)} "
            "paper(s) in Downloads."
        ),
    }


# ---------------------------------------------------------
# 7. MCP TOOL: Update an idea's status
# ---------------------------------------------------------


@mcp.tool()
def update_idea_status(
    idea_id: int,
    status: str,
) -> dict[str, Any]:
    """
    Change the status of a research idea.

    Args:
        idea_id: Numeric ID of the research idea.
        status: New, exploring, experimenting, published or paused.
    """

    allowed_statuses = {
        "new",
        "exploring",
        "experimenting",
        "published",
        "paused",
    }

    normalized_status = status.strip().casefold()

    if normalized_status not in allowed_statuses:
        return {
            "success": False,
            "message": (
                "Status must be new, exploring, experimenting, " "published or paused."
            ),
        }

    ideas = load_ideas()

    for idea in ideas:
        if idea["id"] == idea_id:
            old_status = idea["status"]
            idea["status"] = normalized_status
            idea["updated_at"] = datetime.now(timezone.utc).isoformat()

            save_ideas(ideas)

            return {
                "success": True,
                "message": "Idea status updated successfully.",
                "idea_id": idea_id,
                "old_status": old_status,
                "new_status": normalized_status,
            }

    return {
        "success": False,
        "message": f"No research idea found with ID {idea_id}.",
    }


# ---------------------------------------------------------
# 8. MCP TOOL: Generate a dashboard
# ---------------------------------------------------------


@mcp.tool()
def get_research_dashboard() -> dict[str, Any]:
    """
    Return a summary dashboard of all saved research ideas.
    """

    ideas = load_ideas()

    status_counts = Counter(idea["status"] for idea in ideas)

    tag_counts = Counter(tag for idea in ideas for tag in idea["tags"])

    high_priority_ideas = [
        {
            "id": idea["id"],
            "title": idea["title"],
            "priority": idea["priority"],
            "status": idea["status"],
        }
        for idea in sorted(
            ideas,
            key=lambda item: item["priority"],
            reverse=True,
        )
        if idea["priority"] >= 4
    ]

    return {
        "total_ideas": len(ideas),
        "ideas_by_status": dict(status_counts),
        "most_common_tags": tag_counts.most_common(5),
        "high_priority_ideas": high_priority_ideas,
    }


# ---------------------------------------------------------
# 9. MCP RESOURCE: Allow the AI to read all ideas
# ---------------------------------------------------------


@mcp.resource("research://ideas")
def get_all_research_ideas() -> str:
    """
    Provide all saved research ideas as a readable resource.
    """

    ideas = load_ideas()

    return json.dumps(
        ideas,
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------
# 10. MCP PROMPT: Reusable research-evaluation instruction
# ---------------------------------------------------------


@mcp.prompt()
def evaluate_research_idea(
    title: str,
    problem: str,
) -> str:
    """
    Create a structured prompt for evaluating a research idea.
    """

    return f"""
Act as a senior computer science researcher.

Evaluate the following research idea:

Title:
{title}

Research problem:
{problem}

Analyse it using these dimensions:

1. Problem significance
2. Scientific novelty
3. Technical feasibility
4. Availability of datasets
5. Possible research methodology
6. Expected scientific contribution
7. Risks and limitations
8. Suitable publication venues
9. Recommended first experiment
10. Overall score out of 10

Provide a rigorous but practical evaluation.
"""


# ---------------------------------------------------------
# 11. Start the server using STDIO transport
# ---------------------------------------------------------


def main() -> None:
    """Start the MCP server."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
