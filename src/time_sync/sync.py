"""Core sync logic: pull Clockify entries and mirror them into Toggl Focus."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .clockify import ClockifyClient, ClockifyTimeEntry
from .config import Config, WorkspaceMapping
from .state import SyncState, load_state, save_state
from .toggl import TogglClient, TogglEntryDraft, format_description

logger = logging.getLogger(__name__)

# Small future buffer so an entry started right at "now" still falls inside
# the Toggl dedupe scan window.
_DEDUPE_FUTURE_BUFFER = timedelta(minutes=5)

# When state.json is missing, scan this far back in Toggl to rebuild the
# synced-Clockify-IDs cache. Matches state-file retention.
_RECOVERY_LOOKBACK = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class SyncResult:
    fetched: int
    created: int
    skipped: int
    failed: int

    def combine(self, other: SyncResult) -> SyncResult:
        return SyncResult(
            fetched=self.fetched + other.fetched,
            created=self.created + other.created,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
        )


def run_sync(config: Config) -> SyncResult:
    now = datetime.now(timezone.utc)
    state = load_state(config.state_file)
    state.prune(now=now)
    # Trigger the wider Toggl recovery scan whenever we have no synced-IDs
    # cache to lean on — covers both first-run and corrupt-state-file cases.
    needs_recovery = not state.synced_ids

    # The Toggl scan window must cover the oldest Clockify watermark across
    # all mappings, so we compute it before fetching Clockify data.
    # lookback_hours acts as a floor on every run (not just first-run): the
    # Clockify `start` filter is by entry start time, so retroactive entries
    # logged after the watermark advanced would otherwise be missed forever.
    lookback_floor = now - timedelta(hours=config.lookback_hours)
    per_mapping_windows: dict[str, datetime] = {}
    for mapping in config.mappings:
        watermark = state.watermark(mapping.clockify_workspace_id)
        per_mapping_windows[mapping.clockify_workspace_id] = (
            min(watermark, lookback_floor) if watermark else lookback_floor
        )
    earliest_window = min(per_mapping_windows.values())

    with (
        ClockifyClient(api_key=config.clockify_api_key) as clockify,
        TogglClient(
            api_token=config.toggl_api_token,
            organization_id=config.toggl_organization_id,
            workspace_id=config.toggl_workspace_id,
        ) as toggl,
    ):
        user_id = clockify.current_user_id()

        # One Toggl scan covers dedupe for every mapping since Clockify IDs
        # are globally unique and live in the description as [clk:<id>].
        scan_since = (
            earliest_window
            if not needs_recovery
            else min(earliest_window, now - _RECOVERY_LOOKBACK)
        )
        if needs_recovery:
            logger.warning(
                "no synced-IDs cache; recovering from Toggl since %s",
                scan_since.isoformat(),
            )
        toggl_dedupe = toggl.already_imported_clockify_ids(
            since=scan_since, until=now + _DEDUPE_FUTURE_BUFFER
        )
        logger.info(
            "found %d Clockify-tagged entries in Toggl for the scan window",
            len(toggl_dedupe),
        )
        if needs_recovery:
            for cid in toggl_dedupe:
                state.mark_synced(cid, now)
            logger.info("repopulated state with %d Clockify IDs from Toggl", len(toggl_dedupe))

        total = SyncResult(fetched=0, created=0, skipped=0, failed=0)
        for mapping in config.mappings:
            window_start = per_mapping_windows[mapping.clockify_workspace_id]
            result = _sync_mapping(
                mapping=mapping,
                window_start=window_start,
                now=now,
                clockify=clockify,
                clockify_user_id=user_id,
                toggl=toggl,
                toggl_dedupe=toggl_dedupe,
                state=state,
                dry_run=config.dry_run,
            )
            total = total.combine(result)

    if config.dry_run:
        logger.info("[dry-run] not persisting state.json")
    else:
        save_state(config.state_file, state)

    return total


def _sync_mapping(
    *,
    mapping: WorkspaceMapping,
    window_start: datetime,
    now: datetime,
    clockify: ClockifyClient,
    clockify_user_id: str,
    toggl: TogglClient,
    toggl_dedupe: set[str],
    state: SyncState,
    dry_run: bool,
) -> SyncResult:
    logger.info(
        "mapping %r: syncing Clockify workspace %s since %s -> Toggl project %d",
        mapping.name,
        mapping.clockify_workspace_id,
        window_start.isoformat(),
        mapping.toggl_project_id,
    )

    entries = clockify.time_entries_since(
        workspace_id=mapping.clockify_workspace_id,
        user_id=clockify_user_id,
        start=window_start,
    )
    logger.info("mapping %r: fetched %d candidate entries", mapping.name, len(entries))

    created = 0
    skipped = 0
    failed = 0

    for entry in entries:
        if state.has_synced(entry.id) or entry.id in toggl_dedupe:
            skipped += 1
            continue

        if dry_run:
            logger.info(
                "[dry-run] would create Toggl entry for Clockify %s in project %d: %r",
                entry.id, mapping.toggl_project_id, entry.description,
            )
            created += 1
            continue

        draft = _to_draft(entry, project_id=mapping.toggl_project_id)
        try:
            toggl_id = toggl.create_time_entry(draft)
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception(
                "mapping %r: failed to create Toggl entry for Clockify %s",
                mapping.name, entry.id,
            )
            continue
        state.mark_synced(entry.id, now)
        toggl_dedupe.add(entry.id)
        created += 1
        logger.info(
            "mapping %r: created Toggl entry %d for Clockify %s (%r)",
            mapping.name, toggl_id, entry.id, entry.description,
        )

    # Only advance the watermark for this mapping if everything landed cleanly;
    # otherwise we'd skip past failed entries next run.
    if not dry_run and failed == 0:
        state.set_watermark(mapping.clockify_workspace_id, now)

    return SyncResult(
        fetched=len(entries), created=created, skipped=skipped, failed=failed
    )


def _to_draft(entry: ClockifyTimeEntry, *, project_id: int) -> TogglEntryDraft:
    return TogglEntryDraft(
        description=format_description(entry.description, entry.id),
        start=entry.start,
        duration_seconds=entry.duration_seconds(),
        project_id=project_id,
        billable=entry.billable,
    )


__all__ = ["SyncResult", "SyncState", "run_sync"]
