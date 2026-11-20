---
name: verify-__APP__
description: Drive the __APP__ web app in a real browser against its local dev server and capture proof (screenshots, flow report tied to a commit). Use when a change touches pages, components, routing or copy and you need to show it works beyond unit tests, or when someone names a screen loosely ("the list on the left of the member page") and you need to find it, reproduce it and screenshot it.
---

# Verify __APP__

The app runs with `__START__` and is checked at `__ENTRY_URL__`.

**Find the feature first.** Read `features/README.md`, match the user's words against the feature list and its synonyms, then open that feature file. Do not ask the user where a screen is before searching the map.

## Launch, check, clean up

```bash
.claude/skills/verify-__APP__/scripts/launch.sh    # starts the dev server, waits for 200
.claude/skills/verify-__APP__/scripts/doctor.sh    # read-only: is it ours and serving?
.claude/skills/verify-__APP__/scripts/cleanup.sh   # stops only what launch.sh started
```

Never drive an instance this run did not start. Settings (port, host, start command) live in `scripts/lib.sh`.

## Drive and collect evidence

Each feature has a flow in `flows/<feature-id>.json`, written in visual-parity flow syntax with `{BASE}` for the origin and `{EVIDENCE}` for the evidence folder:

```bash
.claude/skills/verify-__APP__/scripts/verify.sh <feature-id>            # report + screenshots
.claude/skills/verify-__APP__/scripts/verify.sh <feature-id> --mutate   # also prove the assertions can fail
```

Evidence goes to `${VERIFY_OUT:-/tmp/verify-__APP__}/evidence/<feature-id>/`. The report starts with the commit and working-tree state; use that commit as the PR's `Verified at`.

For a path the map does not cover yet, write a new flow from the same syntax (see the visual-parity SKILL.md for every action), run it, then add the feature to the map.

## Keep the map true

```bash
.claude/skills/verify-__APP__/scripts/check-map.sh          # contract, index, flows
.claude/skills/verify-__APP__/scripts/check-map.sh --live   # also every route answers 200 (server up)
```

A change that adds, renames or removes a page updates `features/` and `flows/` in the same PR. A flow that no longer reaches its screen is a map bug: fix the map, then report.

## Not covered

__NOT_COVERED__
