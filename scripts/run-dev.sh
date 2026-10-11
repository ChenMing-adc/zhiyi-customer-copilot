#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${SERVER_HOST:-127.0.0.1}"
PORT="${SERVER_PORT:-8080}"
# Model settings are read from the existing .env; do not shadow them here.
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
