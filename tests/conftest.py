import json
import os
from dataclasses import dataclass

import boto3
import pytest

# Environment must be set before listlambda modules are imported.
os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "STAGE": "dev",
        "TABLE_NAME": "listlambda-test-tasks",
        "ATTACHMENTS_BUCKET": "listlambda-test-attachments",
        "ALLOW_HEADER_IDENTITY": "true",
        "POWERTOOLS_TRACE_DISABLED": "true",
        "POWERTOOLS_METRICS_NAMESPACE": "ListLambdaTest",
        "POWERTOOLS_SERVICE_NAME": "listlambda",
    }
)

from moto import mock_aws  # noqa: E402

from listlambda import api, attachments, notifier  # noqa: E402
from listlambda.attachments import AttachmentService  # noqa: E402
from listlambda.config import get_settings  # noqa: E402
from listlambda.repository import TaskRepository  # noqa: E402

TABLE = os.environ["TABLE_NAME"]
BUCKET = os.environ["ATTACHMENTS_BUCKET"]


def _create_table(ddb):
    return ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


@pytest.fixture
def aws():
    get_settings.cache_clear()
    with mock_aws():
        yield


@pytest.fixture
def table(aws):
    return _create_table(boto3.resource("dynamodb"))


@pytest.fixture
def repo(table):
    repository = TaskRepository(table)
    api.set_repository(repository)
    yield repository
    api.set_repository(None)


@pytest.fixture
def s3(aws):
    client = boto3.client("s3")
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture
def attachment_service(repo, s3):
    svc = AttachmentService(repository=repo, s3_client=s3, bucket=BUCKET)
    attachments.set_service(svc)
    yield svc
    attachments.set_service(None)


@pytest.fixture
def sns(aws):
    client = boto3.client("sns")
    arn = client.create_topic(Name="listlambda-test-events")["TopicArn"]
    os.environ["EVENTS_TOPIC_ARN"] = arn
    get_settings.cache_clear()
    notifier.set_sns_client(client)
    yield client, arn
    notifier.set_sns_client(None)
    os.environ.pop("EVENTS_TOPIC_ARN", None)


@dataclass
class FakeContext:
    function_name: str = "listlambda-test-api"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:listlambda-test-api"
    aws_request_id: str = "test-request"


@pytest.fixture
def lambda_context():
    return FakeContext()


def http_event(method, path, body=None, user="alice", headers=None, query=None):
    hdrs = {"content-type": "application/json"}
    if user:
        hdrs["x-user-id"] = user
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "headers": hdrs,
        "queryStringParameters": query,
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "api",
            "domainName": "api.example.com",
            "domainPrefix": "api",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest",
            },
            "requestId": "req-123",
            "routeKey": "$default",
            "stage": "$default",
            "time": "08/Oct/2026:12:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": body if body is None or isinstance(body, str) else json.dumps(body),
        "isBase64Encoded": False,
    }


@pytest.fixture
def call(repo, lambda_context):
    """Invoke the API handler and return (status, parsed body, headers)."""

    def _call(method, path, body=None, **kwargs):
        resp = api.handler(http_event(method, path, body, **kwargs), lambda_context)
        raw = resp.get("body") or ""
        return resp["statusCode"], (json.loads(raw) if raw else None), resp.get("headers", {})

    return _call
