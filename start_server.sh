#!/bin/bash
# Qwythos-9B llama-server (Apple Silicon, 128K ctx, 无思考块, DRY 防重复)
cd "$(dirname "$0")"

PIDFILE=".llama-server.pid"

# If a PID file already exists, check if it's still running.
if [ -f "$PIDFILE" ]; then
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "✗ llama-server is already running (PID $OLD_PID). Stop it first."
    exit 1
  fi
  rm -f "$PIDFILE"
fi

# exec reuses the current shell's PID, so writing $$ here captures the
# exact PID the server will run under — no race, no guesswork.
echo $$ > "$PIDFILE"

# Always clean up the PID file when this script exits (for any reason).
trap 'rm -f "$PIDFILE"' EXIT INT TERM

exec ./src-latest/build/bin/llama-server \
  --model models/Qwythos-9B-Claude-Mythos-5-1M-Q8_0.gguf \
  --alias qwythos \
  --host 0.0.0.0 --port 8007 \
  --ctx-size 262144 \
  --parallel 1 --batch-size 2048 --ubatch-size 512 \
  --reasoning-format deepseek --reasoning-budget 0 \
  --temp 0.6 --top-p 0.9 --top-k 40 \
  --repeat-penalty 1.2 \
  --dry-multiplier 1.0 --dry-base 2 --dry-penalty-last-n 2048 \
  --n-gpu-layers 99 --flash-attn on --fit on
