"""Toggl Focus REST client (https://focus.toggl.com/api)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

import httpx

_BASE_URL = "https://focus.toggl.com/api"
# Toggl Focus caps per_page at 100.
_PAGE_SIZE = 100

# Marker appended to Toggl descriptions so we can recognise entries we
# previously imported from a given Clockify entry.
_CLOCKIFY_MARKER = re.compile(r"\[clk:([^\]\s]+)\]")


def clockify_marker(clockify_id: str) -> str:
    return f"[clk:{clockify_id}]"


def format_description(base: str, clockify_id: str) -> str:
    base = base.strip()
    marker = clockify_marker(clockify_id)
    return f"{base} {marker}".strip()


@dataclass(frozen=True, slots=True)
class TogglEntryDraft:
    description: str
    start: datetime
    duration_seconds: int
    project_id: int | None
    billable: bool


class TogglClient:
    def __init__(
        self,
        api_token: str,
        organization_id: int,
        workspace_id: int,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def __enter__(self) -> TogglClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _entries_path(self) -> str:
        return (
            f"/organizations/{self._organization_id}"
            f"/workspaces/{self._workspace_id}/time-entries"
        )

    def create_time_entry(self, draft: TogglEntryDraft) -> int:
        # Focus rejects negative or zero durations on the create endpoint
        # ("activity" entries must be > 0 seconds).
        duration = max(draft.duration_seconds, 1)
        payload: dict[str, Any] = {
            "description": draft.description,
            "start": _format_rfc3339(draft.start),
            "duration": duration,
            "type": "activity",
            "billable": draft.billable,
        }
        if draft.project_id is not None:
            payload["project_id"] = draft.project_id

        response = self._client.post(self._entries_path(), json=payload)
        response.raise_for_status()
        body: object = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("unexpected Toggl Focus create response")
        new_id = cast(dict[str, Any], body).get("id")
        if not isinstance(new_id, int):
            raise RuntimeError("Toggl Focus create response missing integer 'id'")
        return new_id

    def already_imported_clockify_ids(
        self, *, since: datetime, until: datetime
    ) -> set[str]:
        """Scan Toggl Focus entries in the window and return Clockify IDs we already imported."""
        ids: set[str] = set()
        page = 1
        path = self._entries_path()
        while True:
            params: dict[str, str | int] = {
                "date_from": _format_rfc3339(since),
                "date_to": _format_rfc3339(until),
                "include_taskless": "true",
                "page": page,
                "per_page": _PAGE_SIZE,
            }
            response = self._client.get(path, params=params)
            response.raise_for_status()
            body: object = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("unexpected Toggl Focus list response")
            page_obj = cast(dict[str, Any], body)
            data = page_obj.get("data")
            if not isinstance(data, list):
                raise RuntimeError("Toggl Focus list response missing 'data' array")
            for raw in cast(list[Any], data):
                if not isinstance(raw, dict):
                    continue
                description = cast(dict[str, Any], raw).get("description")
                if not isinstance(description, str):
                    continue
                for match in _CLOCKIFY_MARKER.finditer(description):
                    ids.add(match.group(1))
            if len(cast(list[Any], data)) < _PAGE_SIZE:
                break
            page += 1
        return ids


def _format_rfc3339(when: datetime) -> str:
    utc = when.astimezone(timezone.utc).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
