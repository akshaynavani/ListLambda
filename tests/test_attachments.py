import pytest

from listlambda.attachments import s3_handler, sanitise_filename
from listlambda.errors import ValidationError
from tests.conftest import BUCKET


def s3_event(key, size=10):
    return {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key, "size": size}}}]}


@pytest.fixture
def task_id(call, attachment_service):
    _, created, _ = call("POST", "/tasks", {"title": "with files"})
    return created["id"]


def test_sanitise_filename():
    assert sanitise_filename("../../etc/passwd") == "passwd"
    assert sanitise_filename("my report (final).pdf") == "my_report_final_.pdf"
    with pytest.raises(ValidationError):
        sanitise_filename("...")


def test_request_upload_returns_presigned_put(call, task_id):
    status, body, _ = call(
        "POST",
        f"/tasks/{task_id}/attachments",
        {"filename": "notes.txt", "content_type": "text/plain", "size_bytes": 42},
    )
    assert status == 201
    assert body["attachment"]["status"] == "pending"
    assert body["upload"]["method"] == "PUT"
    assert BUCKET in body["upload"]["url"]


def test_request_upload_validation(call, task_id):
    status, _, _ = call(
        "POST",
        f"/tasks/{task_id}/attachments",
        {"filename": "evil.exe", "content_type": "application/x-msdownload", "size_bytes": 1},
    )
    assert status == 400
    status, _, _ = call(
        "POST",
        "/tasks/missing/attachments",
        {"filename": "a.txt", "content_type": "text/plain", "size_bytes": 1},
    )
    assert status == 404


def test_upload_event_marks_attachment_and_counts(call, task_id, s3, lambda_context):
    _, body, _ = call(
        "POST",
        f"/tasks/{task_id}/attachments",
        {"filename": "a b.txt", "content_type": "text/plain", "size_bytes": 3},
    )
    att_id = body["attachment"]["id"]
    key = f"alice/{task_id}/{att_id}/a_b.txt"

    # Download is refused until the upload lands.
    assert call("GET", f"/tasks/{task_id}/attachments/{att_id}")[0] == 404

    s3.put_object(Bucket=BUCKET, Key=key, Body=b"abc")
    result = s3_handler(s3_event(key, 3), lambda_context)
    assert result == {"processed": 1, "marked": 1}
    # Duplicate delivery is ignored.
    assert s3_handler(s3_event(key, 3), lambda_context)["marked"] == 0

    _, task, _ = call("GET", f"/tasks/{task_id}")
    assert task["attachment_count"] == 1

    status, dl, _ = call("GET", f"/tasks/{task_id}/attachments/{att_id}")
    assert status == 200 and dl["download"]["url"]

    _, listing, _ = call("GET", f"/tasks/{task_id}/attachments")
    assert [a["status"] for a in listing["items"]] == ["uploaded"]

    assert call("DELETE", f"/tasks/{task_id}/attachments/{att_id}")[0] == 204
    _, task, _ = call("GET", f"/tasks/{task_id}")
    assert task["attachment_count"] == 0


def test_deleting_task_purges_attachments(call, task_id, s3, table, lambda_context):
    for name in ("one.txt", "two.txt"):
        _, body, _ = call(
            "POST",
            f"/tasks/{task_id}/attachments",
            {"filename": name, "content_type": "text/plain", "size_bytes": 1},
        )
        key = f"alice/{task_id}/{body['attachment']['id']}/{name}"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
        s3_handler(s3_event(key, 1), lambda_context)

    assert call("DELETE", f"/tasks/{task_id}")[0] == 204
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0
    remaining = table.scan()["Items"]
    assert remaining == []
