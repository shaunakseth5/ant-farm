#!/usr/bin/env bash
set -euo pipefail

# Example only. Adjust model path and llama-server binary for your machine.
MODEL_PATH="${MODEL_PATH:-/path/to/qwen.gguf}"
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"

exec "$LLAMA_SERVER" \
  --model "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --alias qwen3.6-35b-a3b
