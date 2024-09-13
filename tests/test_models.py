import pytest

from listlambda.errors import ValidationError
from listlambda.models import NO_DUE_DATE, Priority, Status, Task, parse_create, parse_update


def test_parse_create_applies_defaults_and_normalises():
    task = parse_create({"title": "  Write report ", "tags": ["Work", "work", " q4 "]}, "alice")
    assert task.title == "Write report"
    assert task.status is Status.TODO
    assert task.priority is Priority.MEDIUM
    assert task.tags == ["work", "q4"]
    assert task.completed_at is None
    assert task.version == 1


def test_parse_create_done_sets_completed_at():
    task = parse_create({"title": "x", "status": "done"}, "alice")
    assert task.completed_at == task.created_at


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "x" * 201},
        {"title": "ok", "status": "blocked"},
        {"title": "ok", "priority": "urgent"},
        {"title": "ok", "due_date": "31/12/2026"},
        {"title": "ok", "tags": "work"},
        {"title": "ok", "tags": [f"t{i}" for i in range(11)]},
        {"title": "ok", "owner_id": "mallory"},
        [],
    ],
)
def test_parse_create_rejects_invalid_payloads(payload):
    with pytest.raises(ValidationError):
        parse_create(payload, "alice")


def test_parse_update_returns_only_supplied_fields():
    changes = parse_update({"priority": "high", "due_date": None, "expected_version": 3})
    assert changes == {"priority": Priority.HIGH, "due_date": None, "expected_version": 3}


@pytest.mark.parametrize(
    "payload",
    [{}, {"expected_version": 2}, {"title": "x", "expected_version": 0}, {"bogus": 1}],
)
def test_parse_update_rejects_invalid_payloads(payload):
    with pytest.raises(ValidationError):
        parse_update(payload)


def test_item_round_trip_and_keys():
    task = Task(owner_id="alice", title="t", due_date="2026-11-01", tags=["a"])
    item = task.to_item()
    assert item["PK"] == "USER#alice"
    assert item["SK"] == f"TASK#{task.task_id}"
    assert item["GSI1PK"] == "USER#alice#STATUS#todo"
    assert item["GSI1SK"] == f"2026-11-01#{task.task_id}"
    assert "completed_at" not in item
    assert Task.from_item(item) == task


def test_undated_tasks_sort_last():
    task = Task(owner_id="alice", title="t")
    assert task.gsi1_sk().startswith(NO_DUE_DATE)
