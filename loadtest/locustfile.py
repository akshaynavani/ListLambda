"""Locust load test for the ListLambda API.

Each simulated user has its own identity (X-User-Id), seeds a handful of tasks and then runs
a read-heavy mix that mirrors a to-do app: lists and reads dominate, writes are rarer.

    locust -f loadtest/locustfile.py --host "$API_URL"

At the end of a run the process exits non-zero if the p99 latency or failure ratio exceed
the targets (P99_TARGET_MS, MAX_FAILURE_RATIO), so it can gate a CI pipeline.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import date, timedelta

from locust import HttpUser, between, events, task

P99_TARGET_MS = float(os.environ.get("P99_TARGET_MS", "100"))
MAX_FAILURE_RATIO = float(os.environ.get("MAX_FAILURE_RATIO", "0.01"))
SEED_TASKS = int(os.environ.get("SEED_TASKS", "5"))

TITLES = [
    "Review pull request",
    "Write weekly update",
    "Book flights",
    "Renew passport",
    "Plan sprint",
    "Fix flaky test",
    "Call the bank",
    "Groceries",
]
PRIORITIES = ["low", "medium", "high"]
STATUSES = ["todo", "in_progress", "done"]


def random_task() -> dict:
    body = {
        "title": random.choice(TITLES),
        "priority": random.choice(PRIORITIES),
        "tags": random.sample(["work", "home", "errand", "urgent"], k=random.randint(0, 2)),
    }
    if random.random() < 0.7:
        body["due_date"] = (date.today() + timedelta(days=random.randint(0, 30))).isoformat()
    return body


class TaskUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self):
        self.user_id = f"load-{uuid.uuid4().hex[:12]}"
        self.client.headers.update({"X-User-Id": self.user_id, "Content-Type": "application/json"})
        self.task_ids: list[str] = []
        for _ in range(SEED_TASKS):
            self._create()

    def _create(self):
        with self.client.post("/tasks", json=random_task(), name="POST /tasks", catch_response=True) as resp:
            if resp.status_code == 201:
                self.task_ids.append(resp.json()["id"])
                resp.success()
            else:
                resp.failure(f"create returned {resp.status_code}")

    def _pick(self) -> str | None:
        return random.choice(self.task_ids) if self.task_ids else None

    @task(5)
    def list_tasks(self):
        params = {"limit": 25}
        if random.random() < 0.5:
            params["status"] = random.choice(STATUSES)
        self.client.get("/tasks", params=params, name="GET /tasks")

    @task(4)
    def get_task(self):
        tid = self._pick()
        if tid:
            self.client.get(f"/tasks/{tid}", name="GET /tasks/{id}")

    @task(3)
    def create_task(self):
        self._create()

    @task(2)
    def update_task(self):
        tid = self._pick()
        if not tid:
            return
        body = {"priority": random.choice(PRIORITIES), "status": random.choice(STATUSES[:2])}
        with self.client.patch(
            f"/tasks/{tid}", json=body, name="PATCH /tasks/{id}", catch_response=True
        ) as resp:
            # Concurrent edits from the same user are legitimate; 409 is not a server failure.
            if resp.status_code in (200, 409):
                resp.success()

    @task(1)
    def complete_task(self):
        tid = self._pick()
        if tid:
            self.client.post(f"/tasks/{tid}/complete", name="POST /tasks/{id}/complete")

    @task(1)
    def delete_task(self):
        if len(self.task_ids) <= SEED_TASKS // 2:
            return
        tid = self.task_ids.pop(random.randrange(len(self.task_ids)))
        self.client.delete(f"/tasks/{tid}", name="DELETE /tasks/{id}")

    @task(1)
    def health(self):
        self.client.get("/health", name="GET /health")


@events.quitting.add_listener
def enforce_slo(environment, **_kwargs):
    stats = environment.stats.total
    if stats.num_requests == 0:
        return
    p99 = stats.get_response_time_percentile(0.99)
    failure_ratio = stats.fail_ratio
    print(
        f"\nSLO check: requests={stats.num_requests} p99={p99:.0f}ms "
        f"(target {P99_TARGET_MS:.0f}ms) failures={failure_ratio:.2%} "
        f"(max {MAX_FAILURE_RATIO:.2%})"
    )
    if p99 > P99_TARGET_MS or failure_ratio > MAX_FAILURE_RATIO:
        environment.process_exit_code = 1
