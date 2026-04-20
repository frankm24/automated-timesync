"""Read-only Clockify REST client for fetching time entries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict, cast

import httpx

_BASE_URL = "https://api.clockify.me/api/v1"
_PAGE_SIZE = 200


class _TimeIntervalPayload(TypedDict, total=False):
    start: str
    end: str | None
    duration: str | None


class _TimeEntryPayload(TypedDict, total=False):
    id: str
    description: str
    projectId: str | None
    taskId: str | None
    tagIds: list[str]
    billable: bool
    timeInterval: _TimeIntervalPayload


@dataclass(frozen=True, slots=True)
class ClockifyTimeEntry:
    id: str
    description: str
    project_id: str | None
    task_id: str | None
    tag_ids: tuple[str, ...]
    billable: bool
    start: datetime
    end: datetime | None

    @property
    def is_running(self) -> bool:
        return self.end is None

    def duration_seconds(self) -> int:
        if self.end is None:
            return -int(self.start.timestamp())
        return int((self.end - self.start).total_seconds())


class ClockifyClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def __enter__(self) -> ClockifyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def current_user_id(self) -> str:
        response = self._client.get("/user")
        response.raise_for_status()
        body: object = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("unexpected /user response from Clockify")
        user_id = cast(dict[str, Any], body).get("id")
        if not isinstance(user_id, str):
            raise RuntimeError("Clockify /user response missing 'id'")
        return user_id

    def time_entries_since(
        self, *, workspace_id: str, user_id: str, start: datetime
    ) -> list[ClockifyTimeEntry]:
        path = f"/workspaces/{workspace_id}/user/{user_id}/time-entries"
        page = 1
        results: list[ClockifyTimeEntry] = []
        while True:
            params: dict[str, str | int] = {
                "start": _format_clockify_instant(start),
                "page-size": _PAGE_SIZE,
                "page": page,
                "in-progress": "false",
            }
            response = self._client.get(path, params=params)
            response.raise_for_status()
            body: object = response.json()
            if not isinstance(body, list):
                raise RuntimeError("unexpected time-entries response from Clockify")
            entries_raw = cast(list[Any], body)
            for raw in entries_raw:
                if isinstance(raw, dict):
                    parsed = _parse_entry(cast(_TimeEntryPayload, raw))
                    if parsed is not None:
                        results.append(parsed)
            if len(entries_raw) < _PAGE_SIZE:
                break
            page += 1
        return results


def _parse_entry(raw: _TimeEntryPayload) -> ClockifyTimeEntry | None:
    entry_id = raw.get("id")
    interval = raw.get("timeInterval")
    if not isinstance(entry_id, str) or not isinstance(interval, dict):
        return None

    interval_dict = cast(dict[str, Any], interval)
    start_raw = interval_dict.get("start")
    if not isinstance(start_raw, str):
        return None
    start = _parse_instant(start_raw)
    if start is None:
        return None

    end_raw = interval_dict.get("end")
    end = _parse_instant(end_raw) if isinstance(end_raw, str) else None
    if end is None:
        # Skip running entries; we only mirror completed entries.
        return None

    description = raw.get("description") or ""
    project_id_raw = raw.get("projectId")
    task_id_raw = raw.get("taskId")
    tag_ids_raw = raw.get("tagIds") or []
    billable = bool(raw.get("billable", False))

    project_id = project_id_raw if isinstance(project_id_raw, str) and project_id_raw else None
    task_id = task_id_raw if isinstance(task_id_raw, str) and task_id_raw else None
    tag_ids: tuple[str, ...] = tuple(tag_ids_raw)

    return ClockifyTimeEntry(
        id=entry_id,
        description=description,
        project_id=project_id,
        task_id=task_id,
        tag_ids=tag_ids,
        billable=billable,
        start=start,
        end=end,
    )


def _parse_instant(value: str) -> datetime | None:
    try:
        cleaned = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_clockify_instant(when: datetime) -> str:
    utc = when.astimezone(timezone.utc).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
