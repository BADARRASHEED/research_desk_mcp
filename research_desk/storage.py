"""Validated, atomic persistence for local research ideas."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

from research_desk.config import DATA_DIR, DATA_FILE


REQUIRED_IDEA_FIELDS = {
    "id",
    "title",
    "problem",
    "tags",
    "priority",
    "status",
}


def validate_ideas(value: Any) -> list[dict[str, Any]]:
    """Validate the stored collection before it is used or persisted."""

    if not isinstance(value, list):
        raise ValueError("research_ideas.json must contain a JSON array.")

    seen_ids: set[int] = set()

    for index, idea in enumerate(value):
        if not isinstance(idea, dict):
            raise ValueError(f"Research idea at index {index} must be an object.")

        missing_fields = REQUIRED_IDEA_FIELDS.difference(idea)
        if missing_fields:
            missing = ", ".join(sorted(missing_fields))
            raise ValueError(f"Research idea at index {index} is missing: {missing}.")

        idea_id = idea["id"]
        if isinstance(idea_id, bool) or not isinstance(idea_id, int) or idea_id < 1:
            raise ValueError(f"Research idea at index {index} has an invalid ID.")
        if idea_id in seen_ids:
            raise ValueError(f"Duplicate research idea ID {idea_id}.")
        seen_ids.add(idea_id)

        if not isinstance(idea["title"], str) or not idea["title"].strip():
            raise ValueError(f"Research idea {idea_id} has an invalid title.")
        if not isinstance(idea["problem"], str) or not idea["problem"].strip():
            raise ValueError(f"Research idea {idea_id} has an invalid problem.")
        if not isinstance(idea["tags"], list) or not all(
            isinstance(tag, str) for tag in idea["tags"]
        ):
            raise ValueError(f"Research idea {idea_id} has invalid tags.")

        priority = idea["priority"]
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or priority < 1
            or priority > 5
        ):
            raise ValueError(f"Research idea {idea_id} has an invalid priority.")
        if not isinstance(idea["status"], str) or not idea["status"].strip():
            raise ValueError(f"Research idea {idea_id} has an invalid status.")

    return cast(list[dict[str, Any]], value)


def load_ideas() -> list[dict[str, Any]]:
    """Load and validate research ideas from the local JSON file."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not DATA_FILE.exists():
        save_ideas([])
        return []

    try:
        content = DATA_FILE.read_text(encoding="utf-8")
        return validate_ideas(json.loads(content))
    except json.JSONDecodeError as error:
        raise ValueError("research_ideas.json contains invalid JSON.") from error


def save_ideas(ideas: list[dict[str, Any]]) -> None:
    """Validate and atomically replace the local research-idea file."""

    validated_ideas = validate_ideas(ideas)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=DATA_DIR,
            prefix=".research_ideas.",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as temporary_file:
            json.dump(
                validated_ideas,
                temporary_file,
                indent=2,
                ensure_ascii=False,
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)

        os.replace(temporary_path, DATA_FILE)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def get_idea_by_id(idea_id: int) -> dict[str, Any] | None:
    """Return one saved research idea by its numeric ID."""

    return next(
        (idea for idea in load_ideas() if idea.get("id") == idea_id),
        None,
    )
