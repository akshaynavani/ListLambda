import json

from boto3.dynamodb.types import TypeSerializer

from listlambda import notifier
from listlambda.events import (
    ATTACHMENT_UPLOADED,
    TASK_COMPLETED,
    TASK_CREATED,
    TASK_DELETED,
    TASK_REOPENED,
    TASK_UPDATED,
    events_from_record,
)
from listlambda.models import Status, Task

_ser = TypeSerializer()


def _image(item):
    return {k: _ser.serialize(v) for k, v in item.items()}


def record(event_name, old=None, new=None, seq="1"):
    ddb = {"SequenceNumber": seq}
    if old:
        ddb["OldImage"] = _image(old)
    if new:
        ddb["NewImage"] = _image(new)
    return {"eventID": f"e{seq}", "eventName": event_name, "dynamodb": ddb}


def task_item(**overrides):
    task = Task(owner_id="alice", title="t", task_id="t1")
    for key, value in overrides.items():
        setattr(task, key, value)
    return task.to_item()


def types(events):
    return [e.event_type for e in events]


def test_insert_emits_created():
    assert types(events_from_record(record("INSERT", new=task_item()))) == [TASK_CREATED]


def test_remove_emits_deleted():
    assert types(events_from_record(record("REMOVE", old=task_item()))) == [TASK_DELETED]


def test_status_to_done_emits_completed_and_updated():
    old = task_item()
    new = task_item(status=Status.DONE, version=2)
    events = events_from_record(record("MODIFY", old, new))
    assert types(events) == [TASK_COMPLETED, TASK_UPDATED]
    assert events[1].changed_fields == ["status"]


def test_reopen_emits_reopened():
    old = task_item(status=Status.DONE)
    new = task_item(status=Status.TODO)
    assert types(events_from_record(record("MODIFY", old, new)))[0] == TASK_REOPENED


def test_attachment_count_change_is_silent():
    old = task_item()
    new = task_item(attachment_count=1)
    assert events_from_record(record("MODIFY", old, new)) == []


def test_attachment_uploaded_event():
    base = {
        "PK": "USER#alice",
        "SK": "ATT#t1#a1",
        "entity": "attachment",
        "owner_id": "alice",
        "task_id": "t1",
        "attachment_id": "a1",
        "filename": "f.txt",
        "size_bytes": 3,
        "status": "pending",
    }
    events = events_from_record(
        record("MODIFY", base, {**base, "status": "uploaded", "uploaded_at": "now"})
    )
    assert types(events) == [ATTACHMENT_UPLOADED]


class StubSNS:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def publish_batch(self, TopicArn, PublishBatchRequestEntries):
        self.calls.append(PublishBatchRequestEntries)
        if self.fail:
            return {"Successful": [], "Failed": [{"Id": "0"}]}
        return {"Successful": [{"Id": e["Id"]} for e in PublishBatchRequestEntries], "Failed": []}


def test_handler_publishes_with_filterable_attributes(sns, lambda_context):
    stub = StubSNS()
    notifier.set_sns_client(stub)
    result = notifier.handler(
        {"Records": [record("INSERT", new=task_item(tags=["work"]))]}, lambda_context
    )
    assert result == {"batchItemFailures": []}
    entry = stub.calls[0][0]
    assert entry["MessageAttributes"]["event_type"]["StringValue"] == TASK_CREATED
    message = json.loads(entry["Message"])
    assert message["task_id"] == "t1" and message["data"]["tags"] == ["work"]


def test_handler_works_against_moto_sns(sns, lambda_context):
    result = notifier.handler({"Records": [record("REMOVE", old=task_item())]}, lambda_context)
    assert result == {"batchItemFailures": []}


def test_handler_reports_failed_record(sns, lambda_context):
    notifier.set_sns_client(StubSNS(fail=True))
    records = [record("INSERT", new=task_item(), seq="10"), record("INSERT", new=task_item(), seq="11")]
    result = notifier.handler({"Records": records}, lambda_context)
    assert result == {"batchItemFailures": [{"itemIdentifier": "10"}]}
