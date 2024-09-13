import pytest

from listlambda.errors import ConflictError, NotFoundError, ValidationError
from listlambda.models import Status, Task
from listlambda.repository import decode_cursor, encode_cursor


def make(repo, owner="alice", **kwargs):
    kwargs.setdefault("title", "task")
    return repo.create(Task(owner_id=owner, **kwargs))


def test_create_and_get(repo):
    task = make(repo, title="Buy milk")
    fetched = repo.get("alice", task.task_id)
    assert fetched == task


def test_get_is_scoped_to_owner(repo):
    task = make(repo)
    with pytest.raises(NotFoundError):
        repo.get("bob", task.task_id)


def test_list_by_status_orders_by_due_date(repo):
    late = make(repo, due_date="2026-12-01")
    undated = make(repo)
    soon = make(repo, due_date="2026-10-10")
    make(repo, status=Status.DONE)
    page = repo.list("alice", status=Status.TODO)
    assert [t.task_id for t in page.items] == [soon.task_id, late.task_id, undated.task_id]


def test_list_paginates_with_cursor(repo):
    created = {make(repo, title=f"t{i}").task_id for i in range(5)}
    seen, cursor = set(), None
    for _ in range(10):
        page = repo.list("alice", limit=2, cursor=cursor)
        seen.update(t.task_id for t in page.items)
        cursor = page.next_cursor
        if not cursor:
            break
    assert seen == created


def test_list_excludes_attachment_items(repo, table):
    task = make(repo)
    table.put_item(Item={"PK": "USER#alice", "SK": f"ATT#{task.task_id}#a1", "entity": "attachment"})
    assert [t.task_id for t in repo.list("alice").items] == [task.task_id]


def test_cursor_from_another_user_is_rejected():
    cursor = encode_cursor({"PK": "USER#bob", "SK": "TASK#1"})
    with pytest.raises(ValidationError):
        decode_cursor(cursor, "alice")
    with pytest.raises(ValidationError):
        decode_cursor("not-base64!!", "alice")


def test_update_bumps_version_and_moves_gsi_partition(repo, table):
    task = make(repo, due_date="2026-11-01")
    updated = repo.update("alice", task.task_id, {"status": Status.IN_PROGRESS, "title": "new"})
    assert updated.version == 2
    assert updated.title == "new"
    item = table.get_item(Key={"PK": "USER#alice", "SK": f"TASK#{task.task_id}"})["Item"]
    assert item["GSI1PK"] == "USER#alice#STATUS#in_progress"


def test_update_with_stale_version_conflicts(repo):
    task = make(repo)
    repo.update("alice", task.task_id, {"title": "v2"})
    with pytest.raises(ConflictError):
        repo.update("alice", task.task_id, {"title": "v3", "expected_version": 1})


def test_clearing_due_date_removes_attribute(repo, table):
    task = make(repo, due_date="2026-11-01")
    repo.update("alice", task.task_id, {"due_date": None})
    item = table.get_item(Key={"PK": "USER#alice", "SK": f"TASK#{task.task_id}"})["Item"]
    assert "due_date" not in item
    assert item["GSI1SK"].startswith("9999-12-31#")


def test_complete_is_idempotent_and_reopen_clears_timestamp(repo):
    task = make(repo)
    done = repo.complete("alice", task.task_id)
    assert done.status is Status.DONE and done.completed_at
    again = repo.complete("alice", task.task_id)
    assert again.version == done.version
    reopened = repo.update("alice", task.task_id, {"status": Status.TODO})
    assert reopened.completed_at is None


def test_delete(repo):
    task = make(repo)
    deleted = repo.delete("alice", task.task_id)
    assert deleted.task_id == task.task_id
    with pytest.raises(NotFoundError):
        repo.delete("alice", task.task_id)


def test_adjust_attachment_count(repo):
    task = make(repo)
    repo.adjust_attachment_count("alice", task.task_id, 2)
    repo.adjust_attachment_count("alice", task.task_id, -1)
    assert repo.get("alice", task.task_id).attachment_count == 1
    with pytest.raises(NotFoundError):
        repo.adjust_attachment_count("alice", "missing", 1)
