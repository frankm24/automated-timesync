# time-sync

Syncs time entries from Clockify (CISC275 team workspace) into Toggl Focus (personal workspace) automatically.

## Purpose

Clockify is required for CISC275 team time tracking. Toggl Focus is the personal source of truth for all time tracking across projects. This script polls Clockify for new entries and mirrors them into Toggl Focus as taskless "activity" time entries.

## Stack

- Python 3.12+
- Clockify REST API (free tier)
- Toggl Focus REST API — base URL `https://focus.toggl.com/api`, Bearer-token auth with a `toggl_sk_*` API key, OpenAPI spec at https://engineering.toggl.com/docs/focus/openapi/
- systemd service + timer on Ubuntu Server (Dell OptiPlex 3070)

## Behavior

- Poll Clockify workspace for new time entries since last sync
- Transform and POST to Toggl Track
- Track last sync timestamp to avoid duplicates
- Run every 15 minutes via systemd timer

## Deployment

Runs as a systemd service + timer pair on the home lab Ubuntu Server:

- `time-sync.service` — defines what to run
- `time-sync.timer` — fires the service every 15 minutes
- Logs available via `journalctl -u time-sync`
