#!/usr/bin/env bash
# Read-only check: is the instance on $PORT ours, and is it serving?
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
ok=1
if [ "$HOST" != localhost ] && [ "$HOST" != 127.0.0.1 ]; then
  if awk -v h="$HOST" '$1 !~ /^#/ && $1 == "127.0.0.1" { for (i = 2; i <= NF; i++) if ($i == h) f = 1 } END { exit !f }' /etc/hosts; then
    echo "ok   /etc/hosts maps $HOST to 127.0.0.1"
  else
    echo "FAIL /etc/hosts has no active 127.0.0.1 entry for $HOST"; ok=0
  fi
fi
pid=$(file_pid)
if [ -n "$pid" ] && pid_file_matches; then
  echo "ok   dev server pid $pid alive and unchanged since launch"
else
  echo "FAIL no live dev server started by this run (run scripts/launch.sh)"; ok=0; pid=""
fi
owner=$(port_listener)
if [ -z "$owner" ]; then
  echo "FAIL nothing listens on $PORT"; ok=0
elif [ -n "$pid" ] && is_descendant "$owner" "$pid"; then
  echo "ok   port $PORT is owned by pid $owner, started by our pid $pid"
else
  echo "FAIL port $PORT is owned by pid $owner, which this run did not start"; ok=0
fi
code=$(probe)
if [ "$code" = 200 ]; then echo "ok   $ENTRY_URL -> 200 (loopback, no proxy)"; else echo "FAIL $ENTRY_URL -> $code"; ok=0; fi
if [ $ok = 1 ]; then echo "DOCTOR: good"; else echo "DOCTOR: bad"; exit 1; fi
