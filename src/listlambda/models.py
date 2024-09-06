"""Task domain model and request validation.

Validation is hand-rolled rather than using pydantic to keep the deployment package small
and cold starts fast; the schema is small enough that this stays readable.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from .errors import ValidationError

TITLE_MAX = 200
DESCRIPTION_MAX = 4000
TAGS_MAX = 10
TAG_MAX_LEN = 32
NO_DUE_DATE = "9999-12-31"  # sorts undated tasks last in the GSI


class Status(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Task:
    owner_id: str
    title: str
    task_id: str = field(default_factory=new_id)
    description: str = ""
    status: Status = Status.TODO
    priority: Priority = Priority.MEDIUM
    due_date: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    attachment_count: int = 0
    version: int = 1

    # ---- DynamoDB key helpers -------------------------------------------------
    @staticmethod
    def pk(owner_id: str) -> str:
        return f"USER#{owner_id}"

    @staticmethod
    def sk(task_id: str) -> str:
        return f"TASK#{task_id}"

    @staticmethod
    def gsi1_pk(owner_id: str, status: Status | str) -> str:
        value = status.value if isinstance(status, Status) else status
        return f"USER#{owner_id}#STATUS#{value}"

    def gsi1_sk(self) -> str:
        return f"{self.due_date or NO_DUE_DATE}#{self.task_id}"

    # ---- (de)serialisation ----------------------------------------------------
    def to_item(self) -> dict[str, Any]:
        item = self.to_dict()
        item.update(
            {
                "PK": self.pk(self.owner_id),
                "SK": self.sk(self.task_id),
                "GSI1PK": self.gsi1_pk(self.owner_id, self.status),
                "GSI1SK": self.gsi1_sk(),
                "entity": "task",
            }
        )
        return {k: v for k, v in item.items() if v is not None and v != []}

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Task":
        return cls(
            owner_id=item["owner_id"],
            task_id=item["task_id"],
            title=item["title"],
            description=item.get("description", ""),
            status=Status(item.get("status", Status.TODO.value)),
            priority=Priority(item.get("priority", Priority.MEDIUM.value)),
            due_date=item.get("due_date"),
            tags=list(item.get("tags", [])),
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            completed_at=item.get("completed_at"),
            attachment_count=int(item.get("attachment_count", 0)),
            version=int(item.get("version", 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["priority"] = self.priority.value
        return data

    def to_public(self) -> dict[str, Any]:
        """Representation returned by the API (owner_id is implicit)."""
        data = self.to_dict()
        data.pop("owner_id", None)
        data["id"] = data.pop("task_id")
        return data


# ---- request validation -------------------------------------------------------

CREATE_FIELDS = {"title", "description", "status", "priority", "due_date", "tags"}
UPDATE_FIELDS = CREATE_FIELDS


def _require_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")
    return payload


def _reject_unknown(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValidationError("Unknown fields in request body", {"fields": unknown})


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("title must be a non-empty string")
    value = value.strip()
    if len(value) > TITLE_MAX:
        raise ValidationError(f"title must be at most {TITLE_MAX} characters")
    return value


def _description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError("description must be a string")
    if len(value) > DESCRIPTION_MAX:
        raise ValidationError(f"description must be at most {DESCRIPTION_MAX} characters")
    return value


def _enum(enum_cls: type[Enum], name: str, value: Any) -> Enum:
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = [m.value for m in enum_cls]
        raise ValidationError(f"{name} must be one of {allowed}") from exc


def _due_date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("due_date must be an ISO date string (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError("due_date must be an ISO date string (YYYY-MM-DD)") from exc


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ValidationError("tags must be a list of strings")
    cleaned: list[str] = []
    for tag in value:
        tag = tag.strip().lower()
        if not tag:
            continue
        if len(tag) > TAG_MAX_LEN:
            raise ValidationError(f"each tag must be at most {TAG_MAX_LEN} characters")
        if tag not in cleaned:
            cleaned.append(tag)
    if len(cleaned) > TAGS_MAX:
        raise ValidationError(f"at most {TAGS_MAX} tags are allowed")
    return cleaned


def parse_create(payload: Any, owner_id: str) -> Task:
    data = _require_object(payload)
    _reject_unknown(data, CREATE_FIELDS)
    if "title" not in data:
        raise ValidationError("title is required")
    status = _enum(Status, "status", data.get("status", Status.TODO.value))
    task = Task(
        owner_id=owner_id,
        title=_title(data["title"]),
        description=_description(data.get("description")),
        status=status,  # type: ignore[arg-type]
        priority=_enum(Priority, "priority", data.get("priority", Priority.MEDIUM.value)),  # type: ignore[arg-type]
        due_date=_due_date(data.get("due_date")),
        tags=_tags(data.get("tags")),
    )
    if task.status is Status.DONE:
        task.completed_at = task.created_at
    return task


def parse_update(payload: Any) -> dict[str, Any]:
    """Validate a PATCH body and return the normalised changes.

    An optional ``expected_version`` may be supplied for optimistic locking; it is returned
    separately under that key.
    """
    data = _require_object(payload)
    _reject_unknown(data, UPDATE_FIELDS | {"expected_version"})
    changes: dict[str, Any] = {}
    if "title" in data:
        changes["title"] = _title(data["title"])
    if "description" in data:
        changes["description"] = _description(data["description"])
    if "status" in data:
        changes["status"] = _enum(Status, "status", data["status"])
    if "priority" in data:
        changes["priority"] = _enum(Priority, "priority", data["priority"])
    if "due_date" in data:
        changes["due_date"] = _due_date(data["due_date"])
    if "tags" in data:
        changes["tags"] = _tags(data["tags"])
    if not changes:
        raise ValidationError("Request body contains no updatable fields")
    if "expected_version" in data:
        version = data["expected_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValidationError("expected_version must be a positive integer")
        changes["expected_version"] = version
    return changes
