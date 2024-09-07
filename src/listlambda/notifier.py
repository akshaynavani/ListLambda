"""DynamoDB Streams -> SNS fan-out.

Each stream record becomes zero or more domain events published to the events topic with
``event_type`` and ``owner_id`` message attributes, so subscribers can use SNS filter
policies (e.g. only ``task.completed``). Failed records are reported individually so the
stream retries just those (ReportBatchItemFailures).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import boto3

from .config import get_settings
from .events import DomainEvent, events_from_record

_sns = None


def sns_client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def set_sns_client(client) -> None:
    global _sns
    _sns = client


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(type(value))


def publish(events: list[DomainEvent], topic_arn: str) -> None:
    for start in range(0, len(events), 10):
        chunk = events[start : start + 10]
        entries = [
            {
                "Id": str(i),
                "Message": json.dumps(e.to_message(), default=_json_default),
                "Subject": e.event_type,
                "MessageAttributes": {
                    "event_type": {"DataType": "String", "StringValue": e.event_type},
                    "owner_id": {"DataType": "String", "StringValue": e.owner_id},
                },
            }
            for i, e in enumerate(chunk)
        ]
        resp = sns_client().publish_batch(TopicArn=topic_arn, PublishBatchRequestEntries=entries)
        if resp.get("Failed"):
            raise RuntimeError(f"SNS publish failed for {len(resp['Failed'])} entries")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    topic_arn = get_settings().events_topic_arn
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            domain_events = events_from_record(record)
            if domain_events:
                publish(domain_events, topic_arn)
        except Exception:  # noqa: BLE001 - report and let the stream retry this record
            seq = record.get("dynamodb", {}).get("SequenceNumber")
            if seq:
                failures.append({"itemIdentifier": seq})
            # Records after a failure are retried too, so stop to preserve ordering.
            break
    return {"batchItemFailures": failures}
