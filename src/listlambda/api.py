"""HTTP API Lambda entry point.

A single function serves every route through Powertools' in-process router. Clients are
created at import time so warm invocations reuse connections.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, CORSConfig, Response
from aws_lambda_powertools.event_handler.exceptions import NotFoundError as RouteNotFound
from aws_lambda_powertools.logging import correlation_paths

from . import attachments
from .config import get_settings
from .errors import ListLambdaError, ValidationError
from .http import error_response, json_response, parse_limit
from .identity import resolve_owner_id
from .models import Status, parse_create, parse_update
from .observability import count, logger, metrics, tracer
from .repository import TaskRepository

_settings = get_settings()

cors = CORSConfig(
    allow_origin="*",
    allow_headers=["Content-Type", "Authorization", "X-User-Id", "If-Match"],
    expose_headers=["ETag", "Location"],
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
    if not app.current_event.body:
        raise ValidationError("Request body is required")
    try:
        return app.current_event.json_body
    except (ValueError, TypeError) as exc:
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
    count("TasksCreated")
    logger.info("task created", extra={"task_id": task.task_id})
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
    count("TasksCompleted")
    return json_response(200, task.to_public())


@app.delete("/tasks/<task_id>")
def delete_task(task_id: str):
    owner = _owner()
    repo().delete(owner, task_id)
    purged = attachments.service().purge_task(owner, task_id)
    count("TasksDeleted")
    if purged:
        count("AttachmentsPurged", purged)
    return Response(status_code=204, body="")


# ---- attachments --------------------------------------------------------------


@app.post("/tasks/<task_id>/attachments")
def request_attachment_upload(task_id: str):
    owner = _owner()
    attachment = attachments.parse_upload_request(_body(), owner, task_id)
    count("AttachmentUploadsRequested")
    return json_response(201, attachments.service().request_upload(attachment))


@app.get("/tasks/<task_id>/attachments")
def list_attachments(task_id: str):
    items = attachments.service().list(_owner(), task_id)
    return json_response(200, {"items": [a.to_public() for a in items]})


@app.get("/tasks/<task_id>/attachments/<attachment_id>")
def get_attachment(task_id: str, attachment_id: str):
    return json_response(200, attachments.service().download_url(_owner(), task_id, attachment_id))


@app.delete("/tasks/<task_id>/attachments/<attachment_id>")
def delete_attachment(task_id: str, attachment_id: str):
    attachments.service().delete(_owner(), task_id, attachment_id)
    return Response(status_code=204, body="")


# ---- error mapping ------------------------------------------------------------


@app.exception_handler(ListLambdaError)
def handle_domain_error(exc: ListLambdaError):
    count("ClientErrors" if exc.status_code < 500 else "ServerErrors")
    logger.info("request rejected", extra={"code": exc.code, "status": exc.status_code})
    return error_response(exc, app.current_event.request_context.request_id)


@app.not_found
def handle_not_found(_exc: RouteNotFound):
    return json_response(404, {"error": {"code": "route_not_found", "message": "No such route"}})


@app.exception_handler(Exception)
def handle_unexpected(exc: Exception):
    logger.exception("unhandled error")
    count("ServerErrors")
    return json_response(
        500,
        {
            "error": {
                "code": "internal_error",
                "message": "Internal server error",
                "request_id": app.current_event.request_context.request_id,
            }
        },
    )


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_HTTP)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return app.resolve(event, context)
