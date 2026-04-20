"""Runtime configuration loaded from environment variables and a TOML mapping file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class WorkspaceMapping:
    name: str
    clockify_workspace_id: str
    toggl_project_id: int


@dataclass(frozen=True, slots=True)
class Config:
    clockify_api_key: str
    toggl_api_token: str
    toggl_organization_id: int
    toggl_workspace_id: int
    state_file: Path
    lookback_hours: int
    dry_run: bool
    mappings: tuple[WorkspaceMapping, ...]

    @staticmethod
    def from_env() -> Config:
        clockify_api_key = _require_env("CLOCKIFY_API_KEY")
        toggl_api_token = _require_env("TOGGL_API_TOKEN")
        toggl_organization_id = _require_int_env("TOGGL_ORGANIZATION_ID")
        toggl_workspace_id = _require_int_env("TOGGL_WORKSPACE_ID")

        state_file_raw = os.environ.get("TIME_SYNC_STATE_FILE")
        if state_file_raw:
            state_file = Path(state_file_raw).expanduser()
        else:
            state_file = _default_state_path()

        lookback_hours = _optional_int_env("TIME_SYNC_LOOKBACK_HOURS", default=24)
        if lookback_hours <= 0:
            raise ConfigError("TIME_SYNC_LOOKBACK_HOURS must be positive")

        dry_run = _bool_env("TIME_SYNC_DRY_RUN", default=False)

        config_file_raw = os.environ.get("TIME_SYNC_CONFIG_FILE")
        config_file = (
            Path(config_file_raw).expanduser() if config_file_raw else Path("time-sync.toml")
        )
        mappings = _load_mappings(config_file)

        return Config(
            clockify_api_key=clockify_api_key,
            toggl_api_token=toggl_api_token,
            toggl_organization_id=toggl_organization_id,
            toggl_workspace_id=toggl_workspace_id,
            state_file=state_file,
            lookback_hours=lookback_hours,
            dry_run=dry_run,
            mappings=mappings,
        )


def _load_mappings(path: Path) -> tuple[WorkspaceMapping, ...]:
    if not path.exists():
        raise ConfigError(
            f"workspace mapping file not found: {path} "
            "(set TIME_SYNC_CONFIG_FILE or create ./time-sync.toml)"
        )

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    workspaces_raw = raw.get("workspaces")
    if not isinstance(workspaces_raw, list):
        raise ConfigError(f"{path}: must contain at least one [[workspaces]] entry")

    mappings: list[WorkspaceMapping] = []
    seen_clockify: set[str] = set()
    for index, item in enumerate(cast(list[Any], workspaces_raw)):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: [[workspaces]] entry {index} is not a table")
        block = cast(dict[str, Any], item)
        name = block.get("name")
        clockify_workspace_id = block.get("clockify_workspace_id")
        toggl_project_id = block.get("toggl_project_id")

        if not isinstance(name, str) or not name:
            raise ConfigError(f"{path}: [[workspaces]] entry {index} missing string 'name'")
        if not isinstance(clockify_workspace_id, str) or not clockify_workspace_id:
            raise ConfigError(
                f"{path}: workspace {name!r} missing string 'clockify_workspace_id'"
            )
        if not isinstance(toggl_project_id, int):
            raise ConfigError(
                f"{path}: workspace {name!r} missing integer 'toggl_project_id'"
            )

        if clockify_workspace_id in seen_clockify:
            raise ConfigError(
                f"{path}: clockify_workspace_id {clockify_workspace_id} "
                "appears in more than one [[workspaces]] entry"
            )
        seen_clockify.add(clockify_workspace_id)

        mappings.append(
            WorkspaceMapping(
                name=name,
                clockify_workspace_id=clockify_workspace_id,
                toggl_project_id=toggl_project_id,
            )
        )

    if not mappings:
        raise ConfigError(f"{path}: no [[workspaces]] entries defined")

    return tuple(mappings)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _require_int_env(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _optional_int_env(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_state_path() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "time-sync" / "state.json"
