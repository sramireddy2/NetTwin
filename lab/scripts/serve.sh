#!/bin/bash
# Start the NetTwin MCP servers inside WSL and keep them in the foreground.
# Ctrl-C (or SIGTERM) stops them. Because a Windows-side session stays attached while this
# runs, it also keeps the WSL2 VM (and therefore the containerlab links) alive.
# Usage: REPO=/mnt/c/dev/NetTwin serve.sh
set -u
export PATH="$HOME/.local/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/nettwin}"
export NETTWIN_STATE_DIR="${NETTWIN_STATE_DIR:-$HOME/.nettwin}"
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
LOGS="$NETTWIN_STATE_DIR/logs"
mkdir -p "$LOGS"
cd "$REPO" || exit 1

pids=()
start() {
  local name=$1
  uv run "$name" >"$LOGS/$name.log" 2>&1 &
  pids+=($!)
  echo "$name pid $! log $LOGS/$name.log"
}
start twinlab-server
if uv run netverify-server --help >/dev/null 2>&1; then
  start netverify-server
fi

stop() {
  echo "stopping servers"
  kill "${pids[@]}" 2>/dev/null
  wait
  exit 0
}
trap stop INT TERM
wait -n "${pids[@]}"
echo "a server exited; see $LOGS" >&2
stop
