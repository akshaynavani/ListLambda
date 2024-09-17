#!/usr/bin/env bash
# Headless Locust run against a deployed stack.
#   API_URL=https://abc.execute-api.us-east-1.amazonaws.com/dev ./loadtest/run.sh
set -euo pipefail

: "${API_URL:?Set API_URL to the deployed API base URL (see the ApiUrl stack output)}"
USERS="${USERS:-500}"
SPAWN_RATE="${SPAWN_RATE:-50}"
DURATION="${DURATION:-5m}"
OUT_DIR="${OUT_DIR:-loadtest/results}"

mkdir -p "$OUT_DIR"
cd "$(dirname "$0")/.."

locust -f loadtest/locustfile.py \
  --host "$API_URL" \
  --headless \
  --users "$USERS" \
  --spawn-rate "$SPAWN_RATE" \
  --run-time "$DURATION" \
  --csv "$OUT_DIR/listlambda" \
  --html "$OUT_DIR/report.html" \
  --only-summary
