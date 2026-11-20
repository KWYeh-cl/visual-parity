#!/usr/bin/env bash
# Stop only the dev server this run started (recorded pid and all its descendants).
# Refuses to kill a pid that is no longer the process launch.sh started (pid reuse).
# Keeps evidence in $OUT/evidence. Fails if the port is still held afterwards.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
if [ -f "$PID_FILE" ]; then
  pid=$(file_pid)
  if pid_file_matches; then
    # shellcheck disable=SC2046
    kill $(descendants "$pid") "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ -z "$(port_listener)" ] && break
      sleep 1
    done
  else
    echo "pid $pid is not the process launch.sh started (gone or reused); not stopping anything" >&2
  fi
  rm -f "$PID_FILE" "$OUT/dev.log"
fi
if [ -n "$(port_listener)" ]; then
  echo "port $PORT is still in use after cleanup; not killing by name, check it manually" >&2
  exit 1
fi
echo "cleaned; port $PORT free; evidence kept in $OUT/evidence"
