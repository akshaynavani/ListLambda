"""DynamoDB access for tasks.

Item layout (single table):

    PK = USER#<owner>   SK = TASK#<task_id>              -> task
    PK = USER#<owner>   SK = ATT#<task_id>#<att_id>      -> attachment metadata

    GSI1PK = USER#<owner>#STATUS#<status>
    GSI1SK = <due_date or 9999-12-31>#<task_id>          -> tasks by status, ordered by due date
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .config import get_settings
from .errors import ConflictError, NotFoundError, ValidationError
from .models import Status, Task, utc_now

_TASK_PREFIX = "TASK#"


@dataclass
class Page:
    items: list[Task]
    next_cursor: str | None


def encode_cursor(key: dict[str, Any] | None) -> str | None:
    if not key:
        return None
    raw = json.dumps(key, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, owner_id: str) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("cursor is malformed") from exc
    # A cursor must never let a caller page through someone else's partition.
    if not isinstance(key, dict) or key.get("PK") != Task.pk(owner_id):
        raise ValidationError("cursor is not valid for this user")
    return key


class TaskRepository:
    def __init__(self, table=None):
        if table is None:
            table = boto3.resource("dynamodb").Table(get_settings().table_name)
        self.table = table

    # ---- create / read ---------------------------------------------------------
    def create(self, task: Task) -> Task:
        try:
            self.table.put_item(
                Item=task.to_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise ConflictError("A task with this id already exists") from exc
            raise
        return task

    def get(self, owner_id: str, task_id: str) -> Task:
        resp = self.table.get_item(Key={"PK": Task.pk(owner_id), "SK": Task.sk(task_id)})
        item = resp.get("Item")
        if not item:
            raise NotFoundError(f"Task {task_id} not found")
        return Task.from_item(item)

    def list(
        self,
        owner_id: str,
        *,
        status: Status | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> Page:
        start_key = decode_cursor(cursor, owner_id)
        kwargs: dict[str, Any] = {"Limit": limit}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        if status is not None:
            kwargs["IndexName"] = "GSI1"
            kwargs["KeyConditionExpression"] = Key("GSI1PK").eq(Task.gsi1_pk(owner_id, status))
        else:
            kwargs["KeyConditionExpression"] = Key("PK").eq(Task.pk(owner_id)) & Key(
                "SK"
            ).begins_with(_TASK_PREFIX)
        resp = self.table.query(**kwargs)
        tasks = [Task.from_item(item) for item in resp.get("Items", [])]
        return Page(items=tasks, next_cursor=encode_cursor(resp.get("LastEvaluatedKey")))

    # ---- update ----------------------------------------------------------------
    def update(self, owner_id: str, task_id: str, changes: dict[str, Any]) -> Task:
        """Apply validated changes using read-modify-write with a version check.

        Status and due date feed the GSI keys, so rewriting the whole item keeps the
        derived attributes consistent without a large hand-built UpdateExpression.
        """
        expected_version = changes.pop("expected_version", None)
        current = self.get(owner_id, task_id)
        if expected_version is not None and expected_version != current.version:
            raise ConflictError(
                "Task was modified by another request",
                {"expected_version": expected_version, "current_version": current.version},
            )

        updated = Task.from_item(current.to_item())
        for name, value in changes.items():
            setattr(updated, name, value)

        if "status" in changes and changes["status"] != current.status:
            updated.completed_at = utc_now() if updated.status is Status.DONE else None

        updated.updated_at = utc_now()
        updated.version = current.version + 1
        self._put_if_version(updated, current.version)
        return updated

    def complete(self, owner_id: str, task_id: str) -> Task:
        current = self.get(owner_id, task_id)
        if current.status is Status.DONE:
            return current  # idempotent
        return self.update(owner_id, task_id, {"status": Status.DONE})

    def adjust_attachment_count(self, owner_id: str, task_id: str, delta: int) -> None:
        try:
            self.table.update_item(
                Key={"PK": Task.pk(owner_id), "SK": Task.sk(task_id)},
                UpdateExpression="ADD attachment_count :d SET updated_at = :now",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":d": delta, ":now": utc_now()},
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise NotFoundError(f"Task {task_id} not found") from exc
            raise

    # ---- delete ----------------------------------------------------------------
    def delete(self, owner_id: str, task_id: str) -> Task:
        try:
            resp = self.table.delete_item(
                Key={"PK": Task.pk(owner_id), "SK": Task.sk(task_id)},
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="ALL_OLD",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise NotFoundError(f"Task {task_id} not found") from exc
            raise
        return Task.from_item(resp["Attributes"])

    # ---- internals -------------------------------------------------------------
    def _put_if_version(self, task: Task, expected_version: int) -> None:
        try:
            self.table.put_item(
                Item=task.to_item(),
                ConditionExpression="attribute_exists(PK) AND #v = :expected",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":expected": expected_version},
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise ConflictError("Task was modified by another request") from exc
            raise


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
