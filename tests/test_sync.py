"""Tests for run_sync — the core sync orchestrator."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from time_sync.clockify import ClockifyTimeEntry
from time_sync.config import Config, WorkspaceMapping
from time_sync.state import SyncState, load_state, save_state
from time_sync.sync import _RECOVERY_LOOKBACK, run_sync


# ---------------------------------------------------------------------------
# Fakes for ClockifyClient / TogglClient
# ---------------------------------------------------------------------------


@dataclass
class FakeClockifyClient:
    user_id: str = "user-1"
    entries_by_workspace: dict[str, list[ClockifyTimeEntry]] = field(default_factory=dict)
    queries: list[tuple[str, str, datetime]] = field(default_factory=list)

    def __enter__(self) -> "FakeClockifyClient":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def current_user_id(self) -> str:
        return self.user_id

    def time_entries_since(
        self, *, workspace_id: str, user_id: str, start: datetime
    ) -> list[ClockifyTimeEntry]:
        self.queries.append((workspace_id, user_id, start))
        return list(self.entries_by_workspace.get(workspace_id, []))


@dataclass
class FakeTogglClient:
    dedupe_ids: set[str] = field(default_factory=set)
    scan_calls: list[tuple[datetime, datetime]] = field(default_factory=list)
    create_calls: list[object] = field(default_factory=list)
    next_id: int = 1
    fail_create_for: set[str] = field(default_factory=set)

    def __enter__(self) -> "FakeTogglClient":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def already_imported_clockify_ids(
        self, *, since: datetime, until: datetime
    ) -> set[str]:
        self.scan_calls.append((since, until))
        return set(self.dedupe_ids)

    def create_time_entry(self, draft: object) -> int:
        self.create_calls.append(draft)
        # `draft.description` ends with [clk:<id>]; pull it back out for failure injection.
        desc = getattr(draft, "description", "")
        for cid in self.fail_create_for:
            if f"[clk:{cid}]" in desc:
                raise RuntimeError(f"injected failure for {cid}")
        toggl_id = self.next_id
        self.next_id += 1
        return toggl_id


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_config(
    *,
    state_file: Path,
    mappings: tuple[WorkspaceMapping, ...] = (
        WorkspaceMapping(
            name="cisc275", clockify_workspace_id="ws-A", toggl_project_id=1001
        ),
    ),
    lookback_hours: int = 24,
    dry_run: bool = False,
) -> Config:
    return Config(
        clockify_api_key="ck",
        toggl_api_token="tk",
        toggl_organization_id=1,
        toggl_workspace_id=2,
        state_file=state_file,
        lookback_hours=lookback_hours,
        dry_run=dry_run,
        mappings=mappings,
    )


def make_entry(
    entry_id: str, start: datetime, *, duration_minutes: int = 30, billable: bool = False
) -> ClockifyTimeEntry:
    return ClockifyTimeEntry(
        id=entry_id,
        description=f"work on {entry_id}",
        project_id=None,
        task_id=None,
        tag_ids=(),
        billable=billable,
        start=start,
        end=start + timedelta(minutes=duration_minutes),
    )


@pytest.fixture
def install_fakes() -> Iterator[tuple[FakeClockifyClient, FakeTogglClient]]:
    """Patch the Clockify/Toggl client classes that run_sync instantiates."""
    clockify = FakeClockifyClient()
    toggl = FakeTogglClient()

    with (
        patch("time_sync.sync.ClockifyClient", return_value=clockify),
        patch("time_sync.sync.TogglClient", return_value=toggl),
    ):
        yield clockify, toggl


@pytest.fixture
def fixed_now() -> Iterator[datetime]:
    """Pin datetime.now(timezone.utc) inside sync.py."""
    pinned = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return pinned

    with patch("time_sync.sync.datetime", _DT):
        yield pinned


# ---------------------------------------------------------------------------
# force_since
# ---------------------------------------------------------------------------


class TestForceSince:
    def test_in_future_raises(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        config = make_config(state_file=tmp_path / "state.json")
        future = fixed_now + timedelta(hours=1)
        with pytest.raises(ValueError, match="is in the future"):
            run_sync(config, force_since=future)

    def test_overrides_watermark_and_floor(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, _toggl = install_fakes
        # Pre-existing state with a recent watermark; --since should ignore it.
        state = SyncState()
        state.set_watermark("ws-A", fixed_now - timedelta(hours=1))
        save_state(tmp_path / "state.json", state)

        force_since = fixed_now - timedelta(days=10)
        config = make_config(state_file=tmp_path / "state.json")
        run_sync(config, force_since=force_since)

        assert clockify.queries == [("ws-A", "user-1", force_since)]

    def test_logs_warning(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = make_config(state_file=tmp_path / "state.json")
        # Save a non-fresh state so recovery noise doesn't dominate caplog.
        save_state(tmp_path / "state.json", SyncState())
        force_since = fixed_now - timedelta(days=2)
        with caplog.at_level("WARNING"):
            run_sync(config, force_since=force_since)
        assert any("force_since active" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Lookback floor
# ---------------------------------------------------------------------------


class TestLookbackFloor:
    def test_no_watermark_uses_floor(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, _toggl = install_fakes
        # Save a non-fresh state with empty synced_ids to skip recovery scan logic.
        save_state(tmp_path / "state.json", SyncState())
        config = make_config(state_file=tmp_path / "state.json", lookback_hours=24)

        run_sync(config)

        ws, _user, since = clockify.queries[0]
        assert ws == "ws-A"
        assert since == fixed_now - timedelta(hours=24)

    def test_recent_watermark_capped_by_floor(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        """User logs Clockify retroactively: watermark is recent (1h ago) but
        floor of 24h must still cover entries logged for the prior day."""
        clockify, _toggl = install_fakes
        state = SyncState()
        state.set_watermark("ws-A", fixed_now - timedelta(hours=1))
        save_state(tmp_path / "state.json", state)
        config = make_config(state_file=tmp_path / "state.json", lookback_hours=24)

        run_sync(config)

        _ws, _user, since = clockify.queries[0]
        assert since == fixed_now - timedelta(hours=24)

    def test_old_watermark_used_when_older_than_floor(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, _toggl = install_fakes
        old_watermark = fixed_now - timedelta(days=5)
        state = SyncState()
        state.set_watermark("ws-A", old_watermark)
        save_state(tmp_path / "state.json", state)
        config = make_config(state_file=tmp_path / "state.json", lookback_hours=24)

        run_sync(config)

        _ws, _user, since = clockify.queries[0]
        assert since == old_watermark


# ---------------------------------------------------------------------------
# Recovery scan
# ---------------------------------------------------------------------------


class TestRecoveryScan:
    def test_missing_state_triggers_30day_recovery(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        _clockify, toggl = install_fakes
        toggl.dedupe_ids = {"clk-existing"}

        config = make_config(state_file=tmp_path / "state.json")
        run_sync(config)

        # Toggl scan since must extend back to recovery lookback.
        scan_since, _scan_until = toggl.scan_calls[0]
        assert scan_since <= fixed_now - _RECOVERY_LOOKBACK + timedelta(seconds=1)

        # Recovery should repopulate state with the existing clk-id.
        loaded, _ = load_state(tmp_path / "state.json")
        assert loaded.has_synced("clk-existing")

    def test_corrupt_state_triggers_recovery(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        _clockify, toggl = install_fakes
        path = tmp_path / "state.json"
        path.write_text("not json{", encoding="utf-8")
        toggl.dedupe_ids = {"clk-recovered"}

        config = make_config(state_file=path)
        run_sync(config)

        scan_since, _ = toggl.scan_calls[0]
        assert scan_since <= fixed_now - _RECOVERY_LOOKBACK + timedelta(seconds=1)
        loaded, _ = load_state(path)
        assert loaded.has_synced("clk-recovered")

    def test_valid_state_with_empty_synced_ids_does_NOT_recover(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        """Regression: after prune empties synced_ids, the next run must NOT
        trigger a 30-day Toggl rescan. The file existed and parsed fine."""
        _clockify, toggl = install_fakes
        # Save a state file that exists but has empty synced_ids.
        save_state(tmp_path / "state.json", SyncState())
        config = make_config(state_file=tmp_path / "state.json", lookback_hours=24)

        run_sync(config)

        scan_since, _ = toggl.scan_calls[0]
        # Should be the regular lookback (24h), not 30d.
        assert scan_since == fixed_now - timedelta(hours=24)


# ---------------------------------------------------------------------------
# Dedupe and watermark behaviour
# ---------------------------------------------------------------------------


class TestSyncMappingBehaviour:
    def test_creates_new_entries_and_advances_watermark(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, toggl = install_fakes
        clockify.entries_by_workspace = {
            "ws-A": [
                make_entry("clk-1", fixed_now - timedelta(hours=2)),
                make_entry("clk-2", fixed_now - timedelta(hours=1)),
            ],
        }
        save_state(tmp_path / "state.json", SyncState())
        config = make_config(state_file=tmp_path / "state.json")

        result = run_sync(config)

        assert result.fetched == 2
        assert result.created == 2
        assert result.skipped == 0
        assert result.failed == 0
        assert len(toggl.create_calls) == 2

        loaded, _ = load_state(tmp_path / "state.json")
        assert loaded.has_synced("clk-1") and loaded.has_synced("clk-2")
        assert loaded.watermark("ws-A") == fixed_now

    def test_skips_already_synced_via_state(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, toggl = install_fakes
        clockify.entries_by_workspace = {
            "ws-A": [make_entry("clk-1", fixed_now - timedelta(hours=2))],
        }
        state = SyncState()
        state.mark_synced("clk-1", fixed_now - timedelta(minutes=30))
        save_state(tmp_path / "state.json", state)
        config = make_config(state_file=tmp_path / "state.json")

        result = run_sync(config)

        assert result.skipped == 1
        assert result.created == 0
        assert toggl.create_calls == []

    def test_skips_already_imported_via_toggl_dedupe(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, toggl = install_fakes
        clockify.entries_by_workspace = {
            "ws-A": [make_entry("clk-1", fixed_now - timedelta(hours=2))],
        }
        toggl.dedupe_ids = {"clk-1"}
        save_state(tmp_path / "state.json", SyncState())
        config = make_config(state_file=tmp_path / "state.json")

        result = run_sync(config)

        assert result.skipped == 1
        assert result.created == 0

    def test_failed_create_does_not_advance_watermark(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, toggl = install_fakes
        clockify.entries_by_workspace = {
            "ws-A": [
                make_entry("clk-good", fixed_now - timedelta(hours=2)),
                make_entry("clk-bad", fixed_now - timedelta(hours=1)),
            ],
        }
        toggl.fail_create_for = {"clk-bad"}
        save_state(tmp_path / "state.json", SyncState())
        config = make_config(state_file=tmp_path / "state.json")

        result = run_sync(config)

        assert result.created == 1
        assert result.failed == 1
        loaded, _ = load_state(tmp_path / "state.json")
        # Watermark must NOT advance because of the failure.
        assert loaded.watermark("ws-A") is None
        # Successful entry must still be marked, so it isn't re-created next run.
        assert loaded.has_synced("clk-good")
        assert not loaded.has_synced("clk-bad")

    def test_dry_run_does_not_persist_state_or_create(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, toggl = install_fakes
        clockify.entries_by_workspace = {
            "ws-A": [make_entry("clk-1", fixed_now - timedelta(hours=2))],
        }
        path = tmp_path / "state.json"
        config = make_config(state_file=path, dry_run=True)

        result = run_sync(config)

        assert result.created == 1
        assert toggl.create_calls == []  # no real create in dry-run
        assert not path.exists()  # no persistence in dry-run


# ---------------------------------------------------------------------------
# Multiple mappings
# ---------------------------------------------------------------------------


class TestMultipleMappings:
    def test_per_mapping_windows_independent(
        self,
        tmp_path: Path,
        install_fakes: tuple[FakeClockifyClient, FakeTogglClient],
        fixed_now: datetime,
    ) -> None:
        clockify, _toggl = install_fakes
        state = SyncState()
        state.set_watermark("ws-A", fixed_now - timedelta(days=5))  # older than floor
        state.set_watermark("ws-B", fixed_now - timedelta(hours=1))  # newer than floor
        save_state(tmp_path / "state.json", state)

        mappings = (
            WorkspaceMapping("a", "ws-A", 1001),
            WorkspaceMapping("b", "ws-B", 1002),
        )
        config = make_config(
            state_file=tmp_path / "state.json", mappings=mappings, lookback_hours=24
        )

        run_sync(config)

        # ws-A uses its old watermark; ws-B is capped at the floor.
        by_ws = {ws: since for ws, _, since in clockify.queries}
        assert by_ws["ws-A"] == fixed_now - timedelta(days=5)
        assert by_ws["ws-B"] == fixed_now - timedelta(hours=24)
