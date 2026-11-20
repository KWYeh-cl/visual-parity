# __APP__ verification map

Read this index, match the request against the names and synonyms below, then use the feature file as the recipe.

## Baseline preconditions

- `scripts/launch.sh` has run and `scripts/doctor.sh` reports `DOCTOR: good`.
- Never enter real credentials. Prefer paths that stay client-side, or stub the API in the flow.

## Feature entry contract

Each feature file has an H1, one paragraph, then four H2s: `Sub-features`, `How to get to it (user POV)`, `Driving it`, `Gotchas`. Routes are written in backticks (`/path`) so `check-map.sh --live` can probe them. Each feature has `flows/<id>.json`.

## Features

| id | what users call it | file |
|---|---|---|
| home | 首頁, landing page, entry page | [home](./home.md) |
