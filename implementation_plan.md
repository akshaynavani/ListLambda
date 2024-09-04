# ListLambda — Implementation Plan

Serverless task-management REST backend on AWS.

**Stack:** Python 3.12 on AWS Lambda, API Gateway (HTTP API), DynamoDB, S3, SNS,
CloudWatch (logs, metrics via EMF, alarms, dashboard), AWS SAM for infrastructure,
Locust for load testing, pytest + moto for unit tests.

## Key design decisions

- **Single-table DynamoDB design.** `PK = USER#<owner>`, `SK = TASK#<task_id>`. A GSI
  (`GSI1`) keyed by `USER#<owner>#STATUS#<status>` / `<due_date>#<task_id>` serves
  "list my tasks by status, ordered by due date" without scans.
- **One API Lambda with an in-process router** (AWS Lambda Powertools `APIGatewayHttpResolver`)
  rather than one function per route — fewer cold starts, which matters for the p99 target.
- **Optimistic concurrency** via a `version` attribute and conditional writes; a stale update
  returns `409 Conflict`.
- **Event-driven notifications.** The API never talks to SNS directly. DynamoDB Streams feed a
  `Notifier` Lambda that diffs old/new images and publishes `task.created`, `task.completed`,
  `task.deleted`, etc. to an SNS topic with message attributes for subscription filtering.
- **S3 for attachments and static assets.** Attachments are uploaded/downloaded via pre-signed
  URLs so file bytes never pass through Lambda. A second bucket hosts a tiny static web client.
- **Identity.** If API Gateway forwards JWT claims (optional authorizer), `sub` is the owner;
  otherwise an `X-User-Id` header is used (dev / load-test mode, toggled by an env var).
- **Observability.** Powertools structured JSON logging, X-Ray tracing and EMF custom metrics,
  plus CloudWatch alarms (5xx rate, p99 latency, Lambda errors/throttles, DynamoDB throttles)
  and a dashboard, all declared in the SAM template.

## Phase 1 — Project scaffold, infrastructure skeleton, data layer

Deliverables:
- SAM template with DynamoDB table (+ GSI, streams, PITR), HTTP API, API function skeleton.
- Domain model (`Task`, status/priority enums, validation) and DynamoDB repository with
  create/get/list/update/delete, cursor pagination, optimistic locking.

Files: `template.yaml`, `samconfig.toml`, `src/requirements.txt`, `src/listlambda/__init__.py`,
`src/listlambda/config.py`, `src/listlambda/models.py`, `src/listlambda/repository.py`,
`src/listlambda/errors.py`.

## Phase 2 — REST API (CRUD) on Lambda + API Gateway

Deliverables:
- Routes: `POST /tasks`, `GET /tasks` (filter by status, paginate), `GET /tasks/{id}`,
  `PATCH /tasks/{id}`, `DELETE /tasks/{id}`, `POST /tasks/{id}/complete`, `GET /health`.
- Identity resolution, error-to-HTTP mapping, CORS, input validation with clear 400s.

Files: `src/listlambda/api.py`, `src/listlambda/identity.py`, `src/listlambda/http.py`,
`template.yaml`.

## Phase 3 — S3 attachments, static assets, SNS event notifications

Deliverables:
- Attachment routes issuing pre-signed PUT/GET URLs; attachment metadata stored as items
  under the task's partition (`SK = TASK#<id>#ATT#<att_id>`), cleaned up on task delete.
- Static web client bucket + minimal `static/index.html`.
- Notifier Lambda on the DynamoDB stream publishing typed events to SNS; optional email
  subscription parameter.

Files: `src/listlambda/attachments.py`, `src/listlambda/notifier.py`, `src/listlambda/events.py`,
`static/index.html`, `template.yaml`.

## Phase 4 — Observability (CloudWatch)

Deliverables:
- Powertools logger/tracer/metrics wiring with correlation IDs and cold-start metric.
- Business metrics (TasksCreated, TasksCompleted, AttachmentUploadsRequested, EventsPublished).
- CloudWatch alarms + dashboard + log retention, alarm topic.

Files: `src/listlambda/observability.py`, `src/listlambda/api.py`, `src/listlambda/notifier.py`,
`template.yaml`.

## Phase 5 — Tests and load testing

Deliverables:
- pytest suite using moto (repository, API routes, notifier diffing, attachments).
- Locust load test modelling a realistic task-management mix with a p99 check, plus a
  headless runner script and a `Makefile` for common tasks.

Files: `tests/*`, `requirements-dev.txt`, `loadtest/locustfile.py`, `loadtest/run.sh`,
`Makefile`, `pytest.ini`.

## Wrap-up

`.gitignore`, `README.md` (architecture diagram, how to deploy/run/test), `.env.example`.
