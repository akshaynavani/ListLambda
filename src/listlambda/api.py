"""HTTP API Lambda entry point.

A single function serves every route through Powertools' in-process router. Clients are
created at import time so warm invocations reuse connections.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, CORSConfig
from aws_lambda_powertools.event_handler.exceptions import NotFoundError as RouteNotFound

from .config import get_settings
from .errors import ListLambdaError, ValidationError
from .http import error_response, json_response, parse_limit
from .identity import resolve_owner_id
from .models import Status, parse_create, parse_update
from .repository import TaskRepository

_settings = get_settings()

cors = CORSConfig(
    allow_origin="*",
    allow_headers=["Content-Type", "Authorization", "X-User-Id"],
    max_age=600,
)
app = APIGatewayHttpResolver(cors=cors, strip_prefixes=[f"/{_settings.stage}"])

_repo: TaskRepository | None = None


def repo() -> TaskRepository:
    global _repo
    if _repo is None:
        _repo = TaskRepository()
    return _repo


def set_repository(repository: TaskRepository | None) -> None:
    """Test hook for injecting a repository bound to a mocked table."""
    global _repo
    _repo = repository


def _owner() -> str:
    return resolve_owner_id(app.current_event.raw_event)


def _body() -> Any:
    try:
        return app.current_event.json_body
    except ValueError as exc:
        raise ValidationError("Request body is not valid JSON") from exc


# ---- routes -------------------------------------------------------------------


@app.get("/health")
def health():
    return json_response(200, {"status": "ok", "stage": _settings.stage})


@app.post("/tasks")
def create_task():
    owner = _owner()
    task = parse_create(_body(), owner)
    repo().create(task)
    return json_response(201, task.to_public(), headers={"Location": f"/tasks/{task.task_id}"})


@app.get("/tasks")
def list_tasks():
    owner = _owner()
    params = app.current_event.query_string_parameters or {}
    status = None
    if params.get("status"):
        try:
            status = Status(params["status"])
        except ValueError as exc:
            raise ValidationError(
                f"status must be one of {[s.value for s in Status]}"
            ) from exc
    limit = parse_limit(params.get("limit"), default=25, maximum=_settings.max_page_size)
    page = repo().list(owner, status=status, limit=limit, cursor=params.get("cursor"))
    return json_response(
        200,
        {"items": [t.to_public() for t in page.items], "next_cursor": page.next_cursor},
    )


@app.get("/tasks/<task_id>")
def get_task(task_id: str):
    task = repo().get(_owner(), task_id)
    return json_response(200, task.to_public(), headers={"ETag": f'"{task.version}"'})


@app.patch("/tasks/<task_id>")
def update_task(task_id: str):
    owner = _owner()
    changes = parse_update(_body())
    # Also honour If-Match for clients that prefer HTTP-native concurrency control.
    if_match = (app.current_event.headers or {}).get("if-match")
    if if_match and "expected_version" not in changes:
        try:
            changes["expected_version"] = int(if_match.strip('W/"'))
        except ValueError as exc:
            raise ValidationError("If-Match must contain a task version") from exc
    task = repo().update(owner, task_id, changes)
    return json_response(200, task.to_public(), headers={"ETag": f'"{task.version}"'})


@app.post("/tasks/<task_id>/complete")
def complete_task(task_id: str):
    task = repo().complete(_owner(), task_id)
    return json_response(200, task.to_public())


@app.delete("/tasks/<task_id>")
def delete_task(task_id: str):
    repo().delete(_owner(), task_id)
    return json_response(204, None)


# ---- error mapping ------------------------------------------------------------


@app.exception_handler(ListLambdaError)
def handle_domain_error(exc: ListLambdaError):
    return error_response(exc, app.current_event.request_context.request_id)


@app.not_found
def handle_not_found(_exc: RouteNotFound):
    return json_response(404, {"error": {"code": "route_not_found", "message": "No such route"}})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return app.resolve(event, context)
