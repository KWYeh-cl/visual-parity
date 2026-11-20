#!/usr/bin/env bash
# Keep the feature map honest: every feature file follows the entry contract, is listed in the
# index, and has a flow; every flow belongs to a feature. With the dev server up (--live), every
# route a feature names must answer 200. Exit 1 on any problem.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
F="$HERE/../features"; FL="$HERE/../flows"; bad=0
problem() { echo "MAP FAIL $1"; bad=1; }
for f in "$F"/*.md; do
  name="$(basename "$f" .md)"; [ "$name" = README ] && continue
  for h in "Sub-features" "How to get to it (user POV)" "Driving it" "Gotchas"; do
    grep -q "^## $h" "$f" || problem "$name.md lacks '## $h'"
  done
  grep -q "($name.md)\|(./$name.md)" "$F/README.md" || problem "$name.md is not listed in features/README.md"
  [ -f "$FL/$name.json" ] || problem "$name.md has no flows/$name.json"
  grep -q 'TODO' "$f" && problem "$name.md still has TODO"
done
for j in "$FL"/*.json; do
  [ -e "$j" ] || continue
  name="$(basename "$j" .json)"
  [ -f "$F/$name.md" ] || problem "flows/$name.json has no features/$name.md"
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$j" 2>/dev/null || problem "flows/$name.json is not valid JSON"
done
if [ "${1:-}" = --live ]; then
  for route in $(find "$F" -name '*.md' ! -name README.md -exec grep -ho '`/[^` ]*`' {} + | tr -d '`' | sort -u); do
    code=$(curl --noproxy '*' --resolve "$HOST:$PORT:127.0.0.1" -sk -o /dev/null -w '%{http_code}' "$SCHEME://$HOST:$PORT$route" || true)
    [ "$code" = 200 ] || problem "route $route -> $code"
  done
fi
[ $bad = 0 ] && echo "MAP: ok"
exit $bad
