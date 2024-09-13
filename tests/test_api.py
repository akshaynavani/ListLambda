from listlambda import api
from tests.conftest import http_event


def test_health(call):
    status, body, _ = call("GET", "/health")
    assert status == 200
    assert body["status"] == "ok"


def test_stage_prefix_is_stripped(call):
    status, _, _ = call("GET", "/dev/health")
    assert status == 200


def test_create_and_fetch_task(call):
    status, created, headers = call(
        "POST", "/tasks", {"title": "Ship v1", "priority": "high", "due_date": "2026-10-31"}
    )
    assert status == 201
    assert headers["Location"] == f"/tasks/{created['id']}"
    assert "owner_id" not in created

    status, fetched, headers = call("GET", f"/tasks/{created['id']}")
    assert status == 200
    assert fetched["title"] == "Ship v1"
    assert headers["ETag"] == '"1"'


def test_missing_identity_is_401(call):
    status, body, _ = call("GET", "/tasks", user=None)
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_jwt_claim_takes_precedence_over_header(repo, lambda_context):
    event = http_event("POST", "/tasks", {"title": "from jwt"}, user="spoofed")
    event["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": "real-user"}}}
    resp = api.handler(event, lambda_context)
    assert resp["statusCode"] == 201
    assert repo.list("real-user").items
    assert not repo.list("spoofed").items


def test_users_cannot_see_each_others_tasks(call):
    _, created, _ = call("POST", "/tasks", {"title": "private"}, user="alice")
    status, _, _ = call("GET", f"/tasks/{created['id']}", user="bob")
    assert status == 404
    _, listing, _ = call("GET", "/tasks", user="bob")
    assert listing["items"] == []


def test_validation_errors_are_400(call):
    assert call("POST", "/tasks", {"priority": "high"})[0] == 400
    assert call("POST", "/tasks", "{not json")[0] == 400
    assert call("POST", "/tasks")[0] == 400
    assert call("GET", "/tasks", query={"status": "nope"})[0] == 400
    assert call("GET", "/tasks", query={"limit": "1000"})[0] == 400


def test_list_with_status_filter_and_pagination(call):
    for i in range(3):
        call("POST", "/tasks", {"title": f"todo {i}"})
    call("POST", "/tasks", {"title": "finished", "status": "done"})

    _, done, _ = call("GET", "/tasks", query={"status": "done"})
    assert [t["title"] for t in done["items"]] == ["finished"]

    _, first, _ = call("GET", "/tasks", query={"limit": "2"})
    assert len(first["items"]) == 2 and first["next_cursor"]
    _, second, _ = call("GET", "/tasks", query={"limit": "2", "cursor": first["next_cursor"]})
    titles = {t["title"] for t in first["items"] + second["items"]}
    assert len(titles) == 4


def test_patch_with_if_match(call):
    _, created, _ = call("POST", "/tasks", {"title": "draft"})
    tid = created["id"]
    status, updated, headers = call("PATCH", f"/tasks/{tid}", {"title": "final"}, headers={"If-Match": '"1"'})
    assert status == 200 and updated["version"] == 2 and headers["ETag"] == '"2"'

    status, body, _ = call("PATCH", f"/tasks/{tid}", {"title": "stale"}, headers={"If-Match": '"1"'})
    assert status == 409
    assert body["error"]["code"] == "conflict"


def test_complete_and_delete(call):
    _, created, _ = call("POST", "/tasks", {"title": "finish me"})
    tid = created["id"]
    status, done, _ = call("POST", f"/tasks/{tid}/complete")
    assert status == 200 and done["status"] == "done" and done["completed_at"]

    status, body, _ = call("DELETE", f"/tasks/{tid}")
    assert status == 204 and body is None
    assert call("GET", f"/tasks/{tid}")[0] == 404
    assert call("DELETE", f"/tasks/{tid}")[0] == 404


def test_unknown_route_is_404(call):
    status, body, _ = call("GET", "/nope")
    assert status == 404
    assert body["error"]["code"] == "route_not_found"


def test_unexpected_errors_are_500_without_leaking(call, monkeypatch, repo):
    def boom(*_args, **_kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(repo, "get", boom)
    status, body, _ = call("GET", "/tasks/abc")
    assert status == 500
    assert "exploded" not in body["error"]["message"]
