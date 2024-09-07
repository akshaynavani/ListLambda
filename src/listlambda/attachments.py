"""Task attachments stored in S3, with metadata in DynamoDB.

Upload flow:
  1. Client calls ``POST /tasks/{id}/attachments`` with filename, content type and size.
  2. API writes a ``pending`` metadata item and returns a pre-signed PUT URL.
  3. Client PUTs the bytes straight to S3 (file data never transits Lambda).
  4. The S3 ``ObjectCreated`` event triggers :func:`s3_handler`, which marks the item
     ``uploaded`` and bumps the task's ``attachment_count``.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

from .config import get_settings
from .errors import NotFoundError, ValidationError
from .models import Task, new_id, utc_now
from .repository import TaskRepository

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/json",
    "application/zip",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/csv",
    "text/markdown",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Attachment:
    owner_id: str
    task_id: str
    filename: str
    content_type: str
    size_bytes: int
    attachment_id: str = field(default_factory=new_id)
    status: str = "pending"
    created_at: str = field(default_factory=utc_now)
    uploaded_at: str | None = None

    @staticmethod
    def sk(task_id: str, attachment_id: str = "") -> str:
        return f"ATT#{task_id}#{attachment_id}"

    @property
    def s3_key(self) -> str:
        return f"{self.owner_id}/{self.task_id}/{self.attachment_id}/{self.filename}"

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item.update(
            {
                "PK": Task.pk(self.owner_id),
                "SK": self.sk(self.task_id, self.attachment_id),
                "entity": "attachment",
                "s3_key": self.s3_key,
            }
        )
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Attachment":
        return cls(
            owner_id=item["owner_id"],
            task_id=item["task_id"],
            filename=item["filename"],
            content_type=item["content_type"],
            size_bytes=int(item["size_bytes"]),
            attachment_id=item["attachment_id"],
            status=item.get("status", "pending"),
            created_at=item["created_at"],
            uploaded_at=item.get("uploaded_at"),
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.attachment_id,
            "task_id": self.task_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "uploaded_at": self.uploaded_at,
        }


def sanitise_filename(name: str) -> str:
    name = name.strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip("._")
    if not name:
        raise ValidationError("filename is invalid")
    return name[:120]


def parse_upload_request(payload: Any, owner_id: str, task_id: str) -> Attachment:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")
    filename = payload.get("filename")
    content_type = payload.get("content_type")
    size = payload.get("size_bytes")
    if not isinstance(filename, str):
        raise ValidationError("filename is required")
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            "content_type is not allowed", {"allowed": sorted(ALLOWED_CONTENT_TYPES)}
        )
    max_bytes = get_settings().max_attachment_bytes
    if not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > max_bytes:
        raise ValidationError(f"size_bytes must be an integer between 1 and {max_bytes}")
    return Attachment(
        owner_id=owner_id,
        task_id=task_id,
        filename=sanitise_filename(filename),
        content_type=content_type,
        size_bytes=size,
    )


class AttachmentService:
    def __init__(self, repository: TaskRepository | None = None, s3_client=None, bucket=None):
        settings = get_settings()
        self.repo = repository or TaskRepository()
        self.table = self.repo.table
        self.s3 = s3_client or boto3.client(
            "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
        )
        self.bucket = bucket or settings.attachments_bucket
        self.ttl = settings.presign_ttl_seconds

    # ---- API operations ---------------------------------------------------------
    def request_upload(self, attachment: Attachment) -> dict[str, Any]:
        self.repo.get(attachment.owner_id, attachment.task_id)  # 404 if task missing
        self.table.put_item(Item=attachment.to_item())
        url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": attachment.s3_key,
                "ContentType": attachment.content_type,
                "ContentLength": attachment.size_bytes,
            },
            ExpiresIn=self.ttl,
        )
        return {
            "attachment": attachment.to_public(),
            "upload": {
                "method": "PUT",
                "url": url,
                "headers": {"Content-Type": attachment.content_type},
                "expires_in": self.ttl,
            },
        }

    def list(self, owner_id: str, task_id: str) -> list[Attachment]:
        self.repo.get(owner_id, task_id)
        items = self._query_task_attachments(owner_id, task_id)
        return [Attachment.from_item(i) for i in items]

    def get(self, owner_id: str, task_id: str, attachment_id: str) -> Attachment:
        resp = self.table.get_item(
            Key={"PK": Task.pk(owner_id), "SK": Attachment.sk(task_id, attachment_id)}
        )
        if "Item" not in resp:
            raise NotFoundError(f"Attachment {attachment_id} not found")
        return Attachment.from_item(resp["Item"])

    def download_url(self, owner_id: str, task_id: str, attachment_id: str) -> dict[str, Any]:
        att = self.get(owner_id, task_id, attachment_id)
        if att.status != "uploaded":
            raise NotFoundError("Attachment upload has not completed yet")
        url = self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": att.s3_key,
                "ResponseContentDisposition": f'attachment; filename="{att.filename}"',
            },
            ExpiresIn=self.ttl,
        )
        return {"attachment": att.to_public(), "download": {"url": url, "expires_in": self.ttl}}

    def delete(self, owner_id: str, task_id: str, attachment_id: str) -> None:
        att = self.get(owner_id, task_id, attachment_id)
        self.s3.delete_object(Bucket=self.bucket, Key=att.s3_key)
        self.table.delete_item(Key={"PK": Task.pk(owner_id), "SK": Attachment.sk(task_id, attachment_id)})
        if att.status == "uploaded":
            self.repo.adjust_attachment_count(owner_id, task_id, -1)

    def purge_task(self, owner_id: str, task_id: str) -> int:
        """Remove every attachment (metadata + object) for a deleted task."""
        items = self._query_task_attachments(owner_id, task_id)
        if not items:
            return 0
        keys = [{"Key": i["s3_key"]} for i in items]
        for start in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": keys[start : start + 1000], "Quiet": True}
            )
        with self.table.batch_writer() as batch:
            for i in items:
                batch.delete_item(Key={"PK": i["PK"], "SK": i["SK"]})
        return len(items)

    # ---- S3 event handling -----------------------------------------------------
    def mark_uploaded(self, s3_key: str, size: int) -> bool:
        parts = s3_key.split("/")
        if len(parts) < 4:
            return False
        owner_id, task_id, attachment_id = parts[0], parts[1], parts[2]
        try:
            self.table.update_item(
                Key={"PK": Task.pk(owner_id), "SK": Attachment.sk(task_id, attachment_id)},
                UpdateExpression="SET #s = :uploaded, uploaded_at = :now, size_bytes = :size",
                ConditionExpression="attribute_exists(PK) AND #s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":uploaded": "uploaded",
                    ":pending": "pending",
                    ":now": utc_now(),
                    ":size": size,
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False  # duplicate event or orphaned object
        try:
            self.repo.adjust_attachment_count(owner_id, task_id, 1)
        except NotFoundError:
            return False
        return True

    def _query_task_attachments(self, owner_id: str, task_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(Task.pk(owner_id))
            & Key("SK").begins_with(Attachment.sk(task_id)),
        }
        while True:
            resp = self.table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


_service: AttachmentService | None = None


def service() -> AttachmentService:
    global _service
    if _service is None:
        _service = AttachmentService()
    return _service


def set_service(svc: AttachmentService | None) -> None:
    global _service
    _service = svc


def s3_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    """Lambda entry point for S3 ObjectCreated notifications on the attachments bucket."""
    marked = 0
    for record in event.get("Records", []):
        obj = record.get("s3", {}).get("object", {})
        key = urllib.parse.unquote_plus(obj.get("key", ""))
        if service().mark_uploaded(key, int(obj.get("size", 0))):
            marked += 1
    return {"processed": len(event.get("Records", [])), "marked": marked}
