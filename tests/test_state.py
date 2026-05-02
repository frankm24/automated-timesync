"""Tests for time_sync.state — the persistent dedupe / watermark store."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from time_sync.state import SyncState, _parse_iso, load_state, save_state


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


class TestParseIso:
    def test_bare_date_gets_utc(self) -> None:
        result = _parse_iso("2026-04-20")
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.year == 2026 and result.month == 4 and result.day == 20

    def test_iso_with_z_suffix(self) -> None:
        result = _parse_iso("2026-04-20T12:34:56Z")
        assert result == datetime(2026, 4, 20, 12, 34, 56, tzinfo=timezone.utc)

    def test_iso_with_explicit_offset(self) -> None:
        result = _parse_iso("2026-04-20T12:34:56-04:00")
        assert result is not None
        assert result.utcoffset() == timedelta(hours=-4)

    def test_naive_datetime_assumed_utc(self) -> None:
        result = _parse_iso("2026-04-20T12:34:56")
        assert result == datetime(2026, 4, 20, 12, 34, 56, tzinfo=timezone.utc)

    def test_garbage_returns_none(self) -> None:
        assert _parse_iso("not a date") is None
        assert _parse_iso("") is None
        assert _parse_iso("2026-13-99") is None


# ---------------------------------------------------------------------------
# SyncState methods
# ---------------------------------------------------------------------------


class TestSyncState:
    def test_has_synced_and_mark(self) -> None:
        state = SyncState()
        when = datetime(2026, 4, 20, tzinfo=timezone.utc)
        assert not state.has_synced("clk-1")
        state.mark_synced("clk-1", when)
        assert state.has_synced("clk-1")
        assert state.synced_ids["clk-1"] == when

    def test_watermark_round_trip(self) -> None:
        state = SyncState()
        ws = "ws-A"
        when = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
        assert state.watermark(ws) is None
        state.set_watermark(ws, when)
        assert state.watermark(ws) == when

    def test_prune_drops_old_ids_keeps_recent(self) -> None:
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = SyncState()
        state.mark_synced("old", now - timedelta(days=31))
        state.mark_synced("edge", now - timedelta(days=30))  # exactly at retention
        state.mark_synced("recent", now - timedelta(days=1))
        state.prune(now=now)
        assert "old" not in state.synced_ids
        assert "edge" in state.synced_ids
        assert "recent" in state.synced_ids

    def test_prune_does_not_touch_watermarks(self) -> None:
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        state = SyncState()
        state.set_watermark("ws-A", now - timedelta(days=365))
        state.prune(now=now)
        assert state.watermark("ws-A") == now - timedelta(days=365)


# ---------------------------------------------------------------------------
# load_state
# ---------------------------------------------------------------------------


class TestLoadState:
    def test_missing_file_returns_fresh(self, tmp_path: Path) -> None:
        state, fresh = load_state(tmp_path / "nope.json")
        assert fresh is True
        assert not state.synced_ids and not state.last_sync

    def test_empty_file_returns_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("", encoding="utf-8")
        state, fresh = load_state(path)
        assert fresh is True
        assert not state.synced_ids

    def test_whitespace_only_file_returns_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("   \n\t  \n", encoding="utf-8")
        state, fresh = load_state(path)
        assert fresh is True

    def test_corrupt_json_returns_fresh_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("not json{{", encoding="utf-8")
        with caplog.at_level("WARNING"):
            state, fresh = load_state(path)
        assert fresh is True
        assert not state.synced_ids
        assert any("not valid JSON" in rec.message for rec in caplog.records)

    def test_non_dict_json_returns_fresh_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            state, fresh = load_state(path)
        assert fresh is True
        assert any("not a JSON object" in rec.message for rec in caplog.records)

    def test_valid_state_returns_not_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        payload = {
            "last_sync": {"ws-A": "2026-04-20T09:00:00Z"},
            "synced_ids": {"clk-1": "2026-04-20T09:00:00Z"},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        state, fresh = load_state(path)
        assert fresh is False
        assert state.watermark("ws-A") == datetime(
            2026, 4, 20, 9, 0, tzinfo=timezone.utc
        )
        assert state.has_synced("clk-1")

    def test_valid_but_empty_dicts_returns_not_fresh(self, tmp_path: Path) -> None:
        """Regression guard: a file that exists but legitimately has no IDs
        (e.g. after prune cleared everything) must NOT trigger recovery."""
        path = tmp_path / "state.json"
        path.write_text('{"last_sync": {}, "synced_ids": {}}', encoding="utf-8")
        state, fresh = load_state(path)
        assert fresh is False
        assert not state.synced_ids

    def test_drops_unparseable_entries_silently(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        payload = {
            "last_sync": {"ws-A": "garbage", "ws-B": "2026-04-20T09:00:00Z"},
            "synced_ids": {
                "clk-1": "also-garbage",
                "clk-2": "2026-04-20T09:00:00Z",
                "clk-3": 42,  # wrong type
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        state, fresh = load_state(path)
        assert fresh is False
        assert state.watermark("ws-A") is None
        assert state.watermark("ws-B") is not None
        assert not state.has_synced("clk-1")
        assert state.has_synced("clk-2")
        assert not state.has_synced("clk-3")


# ---------------------------------------------------------------------------
# save_state
# ---------------------------------------------------------------------------


class TestSaveState:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "state.json"
        original = SyncState()
        when = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
        original.set_watermark("ws-A", when)
        original.mark_synced("clk-1", when)

        save_state(path, original)
        assert path.exists()

        loaded, fresh = load_state(path)
        assert fresh is False
        assert loaded.watermark("ws-A") == when
        assert loaded.has_synced("clk-1")
        assert loaded.synced_ids["clk-1"] == when

    def test_atomic_write_no_tmp_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        save_state(path, SyncState())
        siblings = list(tmp_path.iterdir())
        assert siblings == [path]
