"""Translate DynamoDB stream records into domain events.

The diff is done on old/new images so the API code never has to know about notifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boto3.dynamodb.types import TypeDeserializer

_deserializer = TypeDeserializer()

TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
TASK_COMPLETED = "task.completed"
TASK_REOPENED = "task.reopened"
TASK_DELETED = "task.deleted"
ATTACHMENT_UPLOADED = "attachment.uploaded"

# Fields whose change is interesting enough to announce as task.updated.
_TRACKED_FIELDS = ("title", "description", "status", "priority", "due_date", "tags")


@dataclass
class DomainEvent:
    event_type: str
    owner_id: str
    task_id: str
    occurred_at: str
    data: dict[str, Any] = field(default_factory=dict)
    changed_fields: list[str] = field(default_factory=list)

    def to_message(self) -> dict[str, Any]:
        return {
            "type": self.event_type,
            "owner_id": self.owner_id,
            "task_id": self.task_id,
            "occurred_at": self.occurred_at,
            "changed_fields": self.changed_fields,
            "data": self.data,
        }


def _image(record: dict[str, Any], name: str) -> dict[str, Any] | None:
    raw = record.get("dynamodb", {}).get(name)
    if not raw:
        return None
    return {k: _deserializer.deserialize(v) for k, v in raw.items()}


def _task_view(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("title", "status", "priority", "due_date", "tags", "version", "completed_at")
    return {k: item[k] for k in keep if k in item}


def events_from_record(record: dict[str, Any]) -> list[DomainEvent]:
    old = _image(record, "OldImage")
    new = _image(record, "NewImage")
    current = new or old
    if not current:
        return []
    entity = current.get("entity")
    if entity == "task":
        return _task_events(record.get("eventName", ""), old, new)
    if entity == "attachment":
        return _attachment_events(old, new)
    return []


def _task_events(event_name: str, old, new) -> list[DomainEvent]:
    if event_name == "INSERT" and new:
        return [_event(TASK_CREATED, new, new["created_at"])]
    if event_name == "REMOVE" and old:
        return [_event(TASK_DELETED, old, old.get("updated_at", ""))]
    if event_name != "MODIFY" or not (old and new):
        return []

    changed = [f for f in _TRACKED_FIELDS if old.get(f) != new.get(f)]
    if not changed:
        return []  # e.g. only attachment_count moved
    events: list[DomainEvent] = []
    if "status" in changed:
        if new.get("status") == "done":
            events.append(_event(TASK_COMPLETED, new, new["updated_at"]))
        elif old.get("status") == "done":
            events.append(_event(TASK_REOPENED, new, new["updated_at"]))
    events.append(_event(TASK_UPDATED, new, new["updated_at"], changed))
    return events


def _attachment_events(old, new) -> list[DomainEvent]:
    if not (old and new):
        return []
    if old.get("status") == "pending" and new.get("status") == "uploaded":
        return [
            DomainEvent(
                event_type=ATTACHMENT_UPLOADED,
                owner_id=new["owner_id"],
                task_id=new["task_id"],
                occurred_at=new.get("uploaded_at", ""),
                data={
                    "attachment_id": new["attachment_id"],
                    "filename": new["filename"],
                    "size_bytes": int(new.get("size_bytes", 0)),
                },
            )
        ]
    return []


def _event(event_type: str, item: dict[str, Any], at: str, changed=None) -> DomainEvent:
    return DomainEvent(
        event_type=event_type,
        owner_id=item["owner_id"],
        task_id=item["task_id"],
        occurred_at=at,
        data=_task_view(item),
        changed_fields=list(changed or []),
    )
