#!/usr/bin/env bash
# Start the dev server for verification and wait until the entry page serves.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
REPO="$(cd "$HERE/../../../.." && pwd)"
mkdir -p "$OUT"
if [ -n "$(port_listener)" ]; then
  echo "port $PORT already in use; refusing to drive an instance this run did not start" >&2
  exit 1
fi
cd "$REPO"
nohup sh -c "$START_CMD" > "$OUT/dev.log" 2>&1 &
pid=$!
for _ in $(seq 1 "$READY_TIMEOUT"); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "dev server exited early; see $OUT/dev.log" >&2
    exit 1
  fi
  if [ "$(probe)" = 200 ]; then
    write_pid_file "$pid"
    echo "ready: $ENTRY_URL (pid $pid)"
    exit 0
  fi
  sleep 1
done
# shellcheck disable=SC2046
kill $(descendants "$pid") "$pid" 2>/dev/null || true
echo "not ready after ${READY_TIMEOUT}s; stopped it; see $OUT/dev.log" >&2
exit 1
