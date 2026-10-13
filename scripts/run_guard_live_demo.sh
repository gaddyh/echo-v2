#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
ALEMBIC="$ROOT_DIR/.venv/bin/alembic"
UVICORN="$ROOT_DIR/.venv/bin/uvicorn"
DB_HOST_PORT="${GUARD_DEMO_DB_PORT:-55432}"
LOCAL_DATABASE_URL="${LOCAL_DATABASE_URL:-postgresql+psycopg://echo:echo@127.0.0.1:${DB_HOST_PORT}/echo}"
HOST="${GUARD_DEMO_HOST:-127.0.0.1}"
PORT="${GUARD_DEMO_PORT:-8000}"
LOG_FILE="${TMPDIR:-/tmp}/echo-guard-live-demo.log"
PID_FILE="${TMPDIR:-/tmp}/echo-guard-live-demo.pid"

stop_demo() {
  if [[ -f "$PID_FILE" ]]; then
    APP_PID="$(cat "$PID_FILE")"
    if kill -0 "$APP_PID" 2>/dev/null; then
      echo "Stopping Echo Guard (PID $APP_PID)..."
      kill "$APP_PID" 2>/dev/null || true
      for attempt in $(seq 1 20); do
        if ! kill -0 "$APP_PID" 2>/dev/null; then
          break
        fi
        sleep 0.5
      done
    fi
    rm -f "$PID_FILE"
  else
    echo "No managed Echo Guard process found."
  fi

  if docker info >/dev/null 2>&1; then
    echo "Stopping local Postgres..."
    COMPOSE_PROJECT_NAME=echo-guard-live docker compose down --remove-orphans >/dev/null
  fi
  echo "Echo Guard Live demo stopped."
}

case "${1:-start}" in
  stop)
    stop_demo
    exit 0
    ;;
  start)
    ;;
  *)
    echo "Usage: $0 [start|stop]" >&2
    exit 2
    ;;
esac

if [[ ! -x "$VENV_PYTHON" || ! -x "$ALEMBIC" || ! -x "$UVICORN" ]]; then
  echo "Missing project virtualenv tools. Run the project setup first." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running. Start Docker Desktop and retry." >&2
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Stop that server or set GUARD_DEMO_PORT." >&2
  exit 1
fi

echo "Starting local Postgres on host port $DB_HOST_PORT..."
COMPOSE_PROJECT_NAME=echo-guard-live POSTGRES_HOST_PORT="$DB_HOST_PORT" docker compose up -d db >/dev/null

for attempt in $(seq 1 30); do
  if COMPOSE_PROJECT_NAME=echo-guard-live docker compose exec -T db pg_isready -U echo -d echo >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" == 30 ]]; then
    echo "Local Postgres did not become ready." >&2
    exit 1
  fi
  sleep 1
done

echo "Applying migrations to local Postgres..."
DATABASE_URL="$LOCAL_DATABASE_URL" "$ALEMBIC" upgrade head

echo "Starting Echo Guard with the real LLM analyzer..."
: > "$LOG_FILE"
DATABASE_URL="$LOCAL_DATABASE_URL" "$UVICORN" echo_v2.app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --log-level info >"$LOG_FILE" 2>&1 &
APP_PID=$!
printf '%s\n' "$APP_PID" > "$PID_FILE"

cleanup() {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  if [[ -f "$PID_FILE" ]] && [[ "$(cat "$PID_FILE")" == "$APP_PID" ]]; then
    rm -f "$PID_FILE"
  fi
}
trap cleanup EXIT INT TERM

for attempt in $(seq 1 60); do
  if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    echo
    echo "Echo Guard Live Compare is ready:"
    echo "  http://$HOST:$PORT/demo/guard/he"
    echo "  Logs: $LOG_FILE"
    echo
    wait "$APP_PID"
    exit $?
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "Echo Guard failed to start. Last logs:" >&2
    cat "$LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done

echo "Echo Guard did not become ready. Last logs:" >&2
cat "$LOG_FILE" >&2
exit 1
