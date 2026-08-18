#!/bin/sh
# Pull Ollama models for autorefactor (not baked into images).
# Driven by axes: CHAT_BACKEND / EMBED_BACKEND / EMBED_DEVICE / LOCAL_CHAT
set -eu

HOST="${OLLAMA_HOST:-http://ollama:11434}"
CHAT_BACKEND="${AUTOREFACTOR_CHAT_BACKEND:-agent}"
EMBED_BACKEND="${AUTOREFACTOR_EMBED_BACKEND:-local}"
EMBED_DEVICE="${AUTOREFACTOR_EMBED_DEVICE:-cpu}"
LOCAL_CHAT="${AUTOREFACTOR_LOCAL_CHAT:-}"
CHAT="${AUTOREFACTOR_LOCAL_LLM_MODEL:-gemma4:e2b}"
EMBED="${AUTOREFACTOR_LOCAL_EMBED_MODEL:-nomic-embed-text}"
EMBED_GPU="${AUTOREFACTOR_LOCAL_EMBED_GPU_MODEL:-qwen3-embedding:0.6b}"

echo "Waiting for Ollama at ${HOST}..."
export OLLAMA_HOST="$HOST"
i=0
while [ "$i" -lt 90 ]; do
  if ollama list >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

echo "chat=${CHAT_BACKEND} embed=${EMBED_BACKEND} device=${EMBED_DEVICE} local_chat=${LOCAL_CHAT} — ensuring models..."

need_embed=0
need_chat=0
case "$EMBED_BACKEND" in
  local) need_embed=1 ;;
esac
case "$LOCAL_CHAT" in
  1|true|TRUE|yes|YES) need_chat=1 ;;
esac

if [ "$need_embed" = "1" ]; then
  if [ "$EMBED_DEVICE" = "gpu" ]; then
    echo "Pulling GPU embed model: ${EMBED_GPU}"
    ollama pull "$EMBED_GPU"
  else
    echo "Pulling embed model: ${EMBED}"
    ollama pull "$EMBED"
  fi
fi

if [ "$need_chat" = "1" ]; then
  echo "Pulling chat model: ${CHAT}"
  ollama pull "$CHAT"
fi

if [ "$need_embed" = "0" ] && [ "$need_chat" = "0" ]; then
  echo "No local Ollama models required for this config"
fi

echo "Models ready."
