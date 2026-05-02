"""Tests for the --since CLI argument parser."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from time_sync.__main__ import _parse_since


class TestParseSince:
    def test_bare_date_becomes_utc_midnight(self) -> None:
        result = _parse_since("2026-04-20")
        assert result == datetime(2026, 4, 20, tzinfo=timezone.utc)

    def test_iso_with_z(self) -> None:
        result = _parse_since("2026-04-20T12:34:56Z")
        assert result == datetime(2026, 4, 20, 12, 34, 56, tzinfo=timezone.utc)

    def test_iso_with_offset_normalized_to_utc(self) -> None:
        result = _parse_since("2026-04-20T12:34:56-04:00")
        # 12:34:56 UTC-4 == 16:34:56 UTC
        assert result == datetime(2026, 4, 20, 16, 34, 56, tzinfo=timezone.utc)
        assert result.tzinfo == timezone.utc

    def test_naive_datetime_assumed_utc(self) -> None:
        result = _parse_since("2026-04-20T08:00:00")
        assert result == datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)

    def test_garbage_raises_argparse_error(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError) as exc_info:
            _parse_since("not a date")
        assert "ISO 8601" in str(exc_info.value)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_since("")
