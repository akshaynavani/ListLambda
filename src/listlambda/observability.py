"""Shared Powertools instances: structured logging, X-Ray tracing and EMF metrics.

Metrics are emitted with the CloudWatch Embedded Metric Format, i.e. as structured log
lines that CloudWatch turns into metrics asynchronously - no PutMetricData calls on the
request path, which keeps latency down.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

SERVICE = "listlambda"
NAMESPACE = "ListLambda"

logger = Logger(service=SERVICE)
tracer = Tracer(service=SERVICE)
metrics = Metrics(namespace=NAMESPACE, service=SERVICE)


def count(name: str, value: int = 1) -> None:
    metrics.add_metric(name=name, unit=MetricUnit.Count, value=value)


__all__ = ["logger", "tracer", "metrics", "count", "MetricUnit"]
