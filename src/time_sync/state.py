"""Persistent state to avoid duplicate syncs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# Keep already-synced IDs around long enough to absorb late edits in Clockify
# without unbounded growth.
_RETENTION = timedelta(days=30)


@dataclass(slots=True)
class SyncState:
    # Per Clockify-workspace watermark: each workspace independently tracks
    # the timestamp of its last successful sync.
    last_sync: dict[str, datetime] = field(default_factory=dict[str, datetime])
    # Clockify entry IDs already mirrored to Toggl. IDs are globally unique
    # across Clockify workspaces, so a single set is fine.
    synced_ids: dict[str, datetime] = field(default_factory=dict[str, datetime])

    def has_synced(self, clockify_id: str) -> bool:
        return clockify_id in self.synced_ids

    def mark_synced(self, clockify_id: str, when: datetime) -> None:
        self.synced_ids[clockify_id] = when

    def watermark(self, clockify_workspace_id: str) -> datetime | None:
        return self.last_sync.get(clockify_workspace_id)

    def set_watermark(self, clockify_workspace_id: str, when: datetime) -> None:
        self.last_sync[clockify_workspace_id] = when

    def prune(self, *, now: datetime) -> None:
        cutoff = now - _RETENTION
        self.synced_ids = {k: v for k, v in self.synced_ids.items() if v >= cutoff}


def load_state(path: Path) -> tuple[SyncState, bool]:
    """Return (state, is_fresh). is_fresh=True means the file was missing or
    unparseable, so the caller should trigger the Toggl recovery scan rather
    than treat an empty cache as steady-state."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SyncState(), True

    if not raw.strip():
        return SyncState(), True

    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("state file %s is not valid JSON (%s); starting fresh", path, exc)
        return SyncState(), True
    if not isinstance(parsed, dict):
        logger.warning("state file %s is not a JSON object; starting fresh", path)
        return SyncState(), True
    data = cast(dict[str, Any], parsed)

    last_sync: dict[str, datetime] = {}
    last_sync_raw = data.get("last_sync")
    if isinstance(last_sync_raw, dict):
        for key, value in cast(dict[str, Any], last_sync_raw).items():
            if isinstance(value, str):
                parsed_value = _parse_iso(value)
                if parsed_value is not None:
                    last_sync[key] = parsed_value

    synced_ids: dict[str, datetime] = {}
    synced_raw = data.get("synced_ids", {})
    if isinstance(synced_raw, dict):
        for key, value in cast(dict[str, Any], synced_raw).items():
            if isinstance(value, str):
                parsed_value = _parse_iso(value)
                if parsed_value is not None:
                    synced_ids[key] = parsed_value

    return SyncState(last_sync=last_sync, synced_ids=synced_ids), False


def save_state(path: Path, state: SyncState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "last_sync": {k: v.isoformat() for k, v in state.last_sync.items()},
        "synced_ids": {k: v.isoformat() for k, v in state.synced_ids.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _parse_iso(value: str) -> datetime | None:
    try:
        cleaned = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
