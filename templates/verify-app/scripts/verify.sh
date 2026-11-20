#!/usr/bin/env bash
# Run one feature's flow through visual-parity and keep the report as evidence.
# Usage: scripts/verify.sh <feature-id> [extra parity.py flow flags, e.g. --video x.mp4 --mutate]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
ID="${1:?usage: verify.sh <feature-id> [flow flags]}"; shift
FLOW="$HERE/../flows/$ID.json"
[ -f "$FLOW" ] || { echo "no flow for '$ID' at $FLOW; see features/README.md" >&2; exit 2; }
PARITY="${VISUAL_PARITY:-$HOME/.claude/skills/visual-parity/scripts/parity.py}"
[ -f "$PARITY" ] || { echo "visual-parity not found at $PARITY (set VISUAL_PARITY)" >&2; exit 2; }
"$HERE/doctor.sh" >/dev/null || { echo "doctor is not good; run scripts/launch.sh first" >&2; exit 1; }
mkdir -p "$OUT/evidence/$ID"
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
# Flows use {BASE} so the same file works whatever port or host this machine uses.
sed "s#{BASE}#$SCHEME://$HOST:$PORT#g; s#{EVIDENCE}#$OUT/evidence/$ID#g" "$FLOW" > "$TMP"
cd "$(cd "$HERE/../../../.." && pwd)"   # the app repo, so the report records its commit
python3 "$PARITY" flow --steps "$TMP" --out "$OUT/evidence/$ID/report.md" "$@"
