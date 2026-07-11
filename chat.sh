#!/bin/bash
# Qwythos-9B Q8 Chat 启动脚本
# Metal 加速，针对 macOS 27 Tahoe 优化

LLAMA_DIR="$HOME/llama-cpp-project/src-latest"
MODEL="$HOME/llama-cpp-project/models/Qwythos-9B-Claude-Mythos-5-1M-Q8_0.gguf"

exec "$LLAMA_DIR/build/bin/llama-cli" \
  -m "$MODEL" \
  -ngl 999 \
  --chat-template chatml \
  --ctx-size 2048 \
  --temp 0.7 \
  --top-p 0.9 \
  --interactive \
  --interactive-first \
  --color \
  --in-prefix "user: " \
  --in-suffix "assistant: " \
  -cnv \
  "$@"
