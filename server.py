"""MCP interface for the Research Desk application."""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP

from research_desk.arxiv import request_arxiv_papers
from research_desk.config import (
    ARXIV_MAX_RESULTS,
    ARXIV_MIN_PAPER_YEAR,
    DOWNLOADS_DIR,
)
from research_desk.reports import write_arxiv_report
from research_desk.storage import get_idea_by_id, load_ideas, save_ideas

mcp = FastMCP("Research Desk MCP Server")


def _paper_search_error(
    message: str,
    idea_id: int | None = None,
) -> dict[str, Any]:
    """Return one consistent error shape for paper-search failures."""

    return {
        "success": False,
        "message": message,
        "idea_id": idea_id,
        "count": 0,
        "papers": [],
        "report_created": False,
        "report_path": None,
        "report_error": None,
    }


@mcp.tool()
def add_research_idea(
    title: str,
    problem: str,
    tags: str,
    priority: int = 3,
) -> dict[str, Any]:
    """Save a new research idea with comma-separated tags."""

    clean_title = title.strip()
    clean_problem = problem.strip()

    if not clean_title:
        return {"success": False, "message": "Title cannot be empty."}
    if not clean_problem:
        return {
            "success": False,
            "message": "Research problem cannot be empty.",
        }
    if not 1 <= priority <= 5:
        return {
            "success": False,
            "message": "Priority must be between 1 and 5.",
        }

    ideas = load_ideas()
    next_id = max((int(idea["id"]) for idea in ideas), default=0) + 1
    new_idea = {
        "id": next_id,
        "title": clean_title,
        "problem": clean_problem,
        "tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
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


@mcp.tool()
def search_research_ideas(keyword: str) -> dict[str, Any]:
    """Search saved ideas across titles, problems, tags and statuses."""

    normalized_keyword = keyword.strip().casefold()
    if not normalized_keyword:
        return {
            "success": False,
            "message": "Search keyword cannot be empty.",
            "count": 0,
            "ideas": [],
        }

    matches: list[dict[str, Any]] = []
    for idea in load_ideas():
        searchable_text = " ".join(
            [
                str(idea.get("title", "")),
                str(idea.get("problem", "")),
                " ".join(str(tag) for tag in idea.get("tags", [])),
                str(idea.get("status", "")),
            ]
        ).casefold()
        if normalized_keyword in searchable_text:
            matches.append(idea)

    return {
        "success": True,
        "keyword": normalized_keyword,
        "count": len(matches),
        "ideas": matches,
    }


@mcp.tool()
def find_arxiv_papers(
    idea_id: int,
    max_results: int = 10,
) -> dict[str, Any]:
    """Find 2025+ arXiv papers and save a Markdown report to Downloads."""

    if not 1 <= max_results <= ARXIV_MAX_RESULTS:
        return _paper_search_error(
            f"max_results must be between 1 and {ARXIV_MAX_RESULTS}.",
            idea_id,
        )

    idea = get_idea_by_id(idea_id)
    if idea is None:
        return _paper_search_error(
            f"No research idea found with ID {idea_id}.",
            idea_id,
        )

    try:
        total_matches, papers = request_arxiv_papers(idea, max_results)
    except HTTPError as error:
        return _paper_search_error(
            f"arXiv API returned HTTP {error.code}. Try again later.",
            idea_id,
        )
    except (URLError, TimeoutError, OSError):
        return _paper_search_error(
            "Could not reach the arXiv API. Check the internet connection "
            "and try again.",
            idea_id,
        )
    except (ElementTree.ParseError, ValueError) as error:
        return _paper_search_error(
            f"Could not process the arXiv response: {error}",
            idea_id,
        )

    report_path: Path | None = None
    report_error: str | None = None
    try:
        report_path = write_arxiv_report(
            idea,
            papers,
            output_dir=DOWNLOADS_DIR,
        )
    except OSError as error:
        report_error = str(error)

    if report_path is None:
        report_message = " Markdown report could not be saved to Downloads."
    else:
        report_message = f" Markdown report saved to {report_path}."

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


@mcp.tool()
def update_idea_status(idea_id: int, status: str) -> dict[str, Any]:
    """Update a research idea's workflow status."""

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
                "Status must be new, exploring, experimenting, published or paused."
            ),
        }

    ideas = load_ideas()
    for idea in ideas:
        if idea.get("id") != idea_id:
            continue

        old_status = str(idea.get("status", "new"))
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


@mcp.tool()
def get_research_dashboard() -> dict[str, Any]:
    """Summarize the saved research portfolio."""

    ideas = load_ideas()
    status_counts = Counter(str(idea.get("status", "unknown")) for idea in ideas)
    tag_counts = Counter(str(tag) for idea in ideas for tag in idea.get("tags", []))
    high_priority_ideas = [
        {
            "id": idea.get("id"),
            "title": idea.get("title", ""),
            "priority": idea.get("priority", 0),
            "status": idea.get("status", "unknown"),
        }
        for idea in sorted(
            ideas,
            key=lambda item: int(item.get("priority", 0)),
            reverse=True,
        )
        if int(idea.get("priority", 0)) >= 4
    ]

    return {
        "total_ideas": len(ideas),
        "ideas_by_status": dict(status_counts),
        "most_common_tags": tag_counts.most_common(5),
        "high_priority_ideas": high_priority_ideas,
    }


@mcp.resource("research://ideas")
def get_all_research_ideas() -> str:
    """Expose all saved research ideas as formatted JSON."""

    return json.dumps(load_ideas(), indent=2, ensure_ascii=False)


@mcp.prompt()
def evaluate_research_idea(title: str, problem: str) -> str:
    """Create a structured research-idea evaluation prompt."""

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


def main() -> None:
    """Start the MCP server over STDIO."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
