#!/bin/bash
# Stop the Qwythos-9B llama-server started by start_server.sh.
cd "$(dirname "$0")"

PORT="${1:-8007}"
PIDFILE=".llama-server.pid"

kill_by_pid() {
  local pid="$1"
  if [ -z "$pid" ]; then
    return 1
  fi
  echo "→ Stopping PID $pid (SIGTERM)"
  kill "$pid" 2>/dev/null
  for i in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "✓ Process stopped."
      return 0
    fi
    sleep 0.5
  done
  echo "× Process still alive, sending SIGKILL"
  kill -9 "$pid" 2>/dev/null
  sleep 0.5
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "✓ Process killed."
    return 0
  fi
  echo "× Unable to stop PID $pid"
  return 1
}

EXIT=0

# 1) Prefer the PID file written by start_server.sh — exact, no guessing.
PID_FROM_FILE=""
if [ -f "$PIDFILE" ]; then
  PID_FROM_FILE="$(cat "$PIDFILE" 2>/dev/null)"
  if [ -n "$PID_FROM_FILE" ] && ! kill -0 "$PID_FROM_FILE" 2>/dev/null; then
    # Stale PID file: process already gone.
    rm -f "$PIDFILE"
    PID_FROM_FILE=""
  fi
fi

if [ -n "$PID_FROM_FILE" ]; then
  kill_by_pid "$PID_FROM_FILE" || EXIT=1
  rm -f "$PIDFILE"
  exit "$EXIT"
fi

# 2) PID file missing (e.g. server started by hand). Fall back to finding
#    a process actually bound to the configured port.
PIDS=""
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null)"
fi

PIDS="$(echo "$PIDS" | tr ' ' '\n' | sort -un | tr '\n' ' ' | sed 's/^ *//;s/ *$//')"

if [ -z "$PIDS" ]; then
  echo "No llama-server process found on port $PORT."
  exit 0
fi

for pid in $PIDS; do
  kill_by_pid "$pid" || EXIT=1
done

exit "$EXIT"
