"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    stage: str
    table_name: str
    attachments_bucket: str
    events_topic_arn: str
    allow_header_identity: bool
    presign_ttl_seconds: int
    max_page_size: int
    max_attachment_bytes: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        stage=os.environ.get("STAGE", "dev"),
        table_name=os.environ.get("TABLE_NAME", "listlambda-dev-tasks"),
        attachments_bucket=os.environ.get("ATTACHMENTS_BUCKET", ""),
        events_topic_arn=os.environ.get("EVENTS_TOPIC_ARN", ""),
        allow_header_identity=_bool(os.environ.get("ALLOW_HEADER_IDENTITY"), False),
        presign_ttl_seconds=int(os.environ.get("PRESIGN_TTL_SECONDS", "900")),
        max_page_size=int(os.environ.get("MAX_PAGE_SIZE", "100")),
        max_attachment_bytes=int(os.environ.get("MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024))),
    )
