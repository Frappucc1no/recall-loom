#!/usr/bin/env python3
"""Workspace, state, lock, and storage-root helpers for RecallLoom."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from core.errors import (
    ConfigContractError,
    LockBusyError,
    StorageResolutionError,
)
from core.bridge import blocks as bridge_blocks
from core.protocol import contracts as protocol_contracts

PROTOCOL_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)*$")
DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
RECOVERY_PROPOSAL_FILE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[A-Za-z0-9._-]+\.md$")
REVIEW_RECORD_FILE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[A-Za-z0-9._-]+\.review\.md$")

CONTEXT_DIRNAME = ".recallloom"
VISIBLE_DIRNAME = "recallloom"
DEFAULT_STORAGE_MODE = "hidden"
VISIBLE_STORAGE_MODE = "visible"
WORKSPACE_LOCK_FILENAME = ".recallloom.write.lock"
DAILY_LOG_PATH_PATTERN = protocol_contracts.DAILY_LOG_PATH_PATTERN
DAILY_LOGS_DIRNAME = protocol_contracts.DAILY_LOGS_DIRNAME


@dataclass(frozen=True)
class WorkspaceInfo:
    project_root: Path
    storage_root: Path
    storage_mode: str
    workspace_language: str
    config_path: Path | None
    declared_storage_mode: str | None = None
    storage_mode_matches_path: bool = True
    protocol_version: str | None = None
    protocol_version_supported: bool = True


@dataclass(frozen=True)
class RecoveryWorkspaceInfo:
    project_root: Path
    storage_root: Path
    storage_mode: str


def now_iso_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def load_json(path: Path) -> dict:
    return json.loads(read_text(path))


def atomic_write_if_unchanged(path: Path, *, expected_text: str, new_text: str) -> None:
    current_text = read_text(path) if path.exists() else ""
    if current_text != expected_text:
        raise LockBusyError(f"Refusing to write {path} because the file changed after it was read.")
    write_text(path, new_text)


def validate_storage_mode(value: str) -> str:
    if value not in protocol_contracts.SUPPORTED_STORAGE_MODES:
        raise ConfigContractError(f"Unsupported storage_mode: {value}")
    return value


def validate_workspace_language(value: str) -> str:
    if value not in protocol_contracts.SUPPORTED_WORKSPACE_LANGUAGES:
        raise ConfigContractError(f"Unsupported workspace_language: {value}")
    return value


def validate_protocol_version(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigContractError("protocol_version must be a non-empty string")
    if not PROTOCOL_VERSION_RE.match(value):
        raise ConfigContractError(
            f"protocol_version must use dotted string form such as '1.0', got: {value}"
        )
    return value


def validate_daily_log_protocol_path(value: str | None, *, field_name: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigContractError(f"{field_name} must be null or a non-empty string: {path}")
    parts = PurePosixPath(value).parts
    if len(parts) != 2 or parts[0] != DAILY_LOGS_DIRNAME or not DATE_FILE_RE.match(parts[1]):
        raise ConfigContractError(
            f"{field_name} must point to an active ISO-dated daily log under {DAILY_LOGS_DIRNAME}/: {path}"
        )
    try:
        date.fromisoformat(Path(parts[1]).stem)
    except ValueError as exc:
        raise ConfigContractError(
            f"{field_name} must use a valid ISO-dated daily log filename under {DAILY_LOGS_DIRNAME}/: {path}"
        ) from exc
    return value


def config_payload(
    *,
    storage_mode: str,
    workspace_language: str,
    created_by: str,
    created_at: str,
    protocol_version: str = protocol_contracts.CURRENT_PROTOCOL_VERSION,
) -> dict:
    return {
        "protocol_version": protocol_version,
        "storage_mode": storage_mode,
        "workspace_language": workspace_language,
        "created_by": created_by,
        "created_at": created_at,
    }


def initial_workspace_state(
    *,
    tool_name: str,
    timestamp: str,
    git_exclude_mode: str,
) -> dict:
    return {
        "workspace_revision": 1,
        "update_protocol_revision": 1,
        "git_exclude_mode": git_exclude_mode,
        "bridged_entries": {},
        "files": {
            "context_brief": {
                "file_revision": 1,
                "updated_at": timestamp,
                "writer_id": tool_name,
                "base_workspace_revision": 1,
            },
            "rolling_summary": {
                "file_revision": 1,
                "updated_at": timestamp,
                "writer_id": tool_name,
                "base_workspace_revision": 1,
            },
            "update_protocol": {
                "file_revision": 1,
                "updated_at": timestamp,
                "writer_id": tool_name,
                "base_workspace_revision": 1,
            },
        },
        "daily_logs": {
            "latest_file": None,
            "latest_entry_id": None,
            "latest_entry_seq": 0,
            "entry_count": 0,
        },
    }


def load_workspace_state(path: Path) -> dict:
    try:
        data = load_json(path)
    except FileNotFoundError as exc:
        raise ConfigContractError(f"Missing state file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigContractError(f"Malformed JSON in state file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigContractError(f"State file is not valid UTF-8: {path}") from exc
    if not isinstance(data, dict):
        raise ConfigContractError(f"State file must contain a JSON object: {path}")
    required = {
        "workspace_revision",
        "update_protocol_revision",
        "git_exclude_mode",
        "bridged_entries",
        "files",
        "daily_logs",
    }
    missing = sorted(required.difference(data.keys()))
    if missing:
        raise ConfigContractError(f"State file is missing required fields {missing}: {path}")

    if not isinstance(data["workspace_revision"], int) or data["workspace_revision"] < 1:
        raise ConfigContractError(
            f"state.json workspace_revision must be a positive integer: {path}"
        )
    if not isinstance(data["update_protocol_revision"], int) or data["update_protocol_revision"] < 1:
        raise ConfigContractError(
            f"state.json update_protocol_revision must be a positive integer: {path}"
        )
    if data["git_exclude_mode"] not in {"managed", "skipped", "not_applicable"}:
        raise ConfigContractError(
            f"state.json git_exclude_mode must be one of managed/skipped/not_applicable: {path}"
        )

    bridged_entries = data["bridged_entries"]
    if not isinstance(bridged_entries, dict):
        raise ConfigContractError(f"state.json bridged_entries must be an object: {path}")
    for rel_target, bridge_state in bridged_entries.items():
        if not isinstance(rel_target, str) or not rel_target.strip():
            raise ConfigContractError(
                f"state.json bridged_entries keys must be non-empty strings: {path}"
            )
        if rel_target not in protocol_contracts.ROOT_ENTRY_CANDIDATE_STRINGS:
            raise ConfigContractError(
                "state.json bridged_entries keys must be supported root entry file paths "
                f"{sorted(protocol_contracts.ROOT_ENTRY_CANDIDATE_STRINGS)}: {path}"
            )
        if not isinstance(bridge_state, dict):
            raise ConfigContractError(
                f"state.json bridged_entries values must be objects: {path}"
            )
        if "update_protocol_revision_seen" in bridge_state:
            value = bridge_state["update_protocol_revision_seen"]
            if not isinstance(value, int) or value < 1:
                raise ConfigContractError(
                    f"state.json bridged_entries.{rel_target}.update_protocol_revision_seen must be a positive integer: {path}"
                )
        if "latest_daily_log_seen" in bridge_state:
            validate_daily_log_protocol_path(
                bridge_state["latest_daily_log_seen"],
                field_name=f"state.json bridged_entries.{rel_target}.latest_daily_log_seen",
                path=path,
            )
        if "updated_at" in bridge_state:
            value = bridge_state["updated_at"]
            if not isinstance(value, str) or not value.strip():
                raise ConfigContractError(
                    f"state.json bridged_entries.{rel_target}.updated_at must be a non-empty string: {path}"
                )

    files = data["files"]
    if not isinstance(files, dict):
        raise ConfigContractError(f"state.json files must be an object: {path}")
    for file_key in ("context_brief", "rolling_summary", "update_protocol"):
        entry = files.get(file_key)
        if not isinstance(entry, dict):
            raise ConfigContractError(
                f"state.json files.{file_key} must be an object: {path}"
            )
        if not isinstance(entry.get("file_revision"), int) or entry["file_revision"] < 1:
            raise ConfigContractError(
                f"state.json files.{file_key}.file_revision must be a positive integer: {path}"
            )
        if not isinstance(entry.get("updated_at"), str) or not entry["updated_at"].strip():
            raise ConfigContractError(
                f"state.json files.{file_key}.updated_at must be a non-empty string: {path}"
            )
        if not isinstance(entry.get("writer_id"), str) or not entry["writer_id"].strip():
            raise ConfigContractError(
                f"state.json files.{file_key}.writer_id must be a non-empty string: {path}"
            )
        if (
            not isinstance(entry.get("base_workspace_revision"), int)
            or entry["base_workspace_revision"] < 1
        ):
            raise ConfigContractError(
                f"state.json files.{file_key}.base_workspace_revision must be a positive integer: {path}"
            )

    daily_logs = data["daily_logs"]
    if not isinstance(daily_logs, dict):
        raise ConfigContractError(f"state.json daily_logs must be an object: {path}")
    latest_file = validate_daily_log_protocol_path(
        daily_logs.get("latest_file"),
        field_name="state.json daily_logs.latest_file",
        path=path,
    )
    latest_entry_id = daily_logs.get("latest_entry_id")
    if latest_entry_id is not None and (
        not isinstance(latest_entry_id, str) or not latest_entry_id.strip()
    ):
        raise ConfigContractError(
            f"state.json daily_logs.latest_entry_id must be null or a non-empty string: {path}"
        )
    latest_entry_seq = daily_logs.get("latest_entry_seq")
    if not isinstance(latest_entry_seq, int) or latest_entry_seq < 0:
        raise ConfigContractError(
            f"state.json daily_logs.latest_entry_seq must be a non-negative integer: {path}"
        )
    entry_count = daily_logs.get("entry_count")
    if not isinstance(entry_count, int) or entry_count < 0:
        raise ConfigContractError(
            f"state.json daily_logs.entry_count must be a non-negative integer: {path}"
        )
    if entry_count < latest_entry_seq:
        raise ConfigContractError(
            f"state.json daily_logs.entry_count cannot be smaller than latest_entry_seq: {path}"
        )
    if latest_file is None:
        if latest_entry_id is not None or latest_entry_seq != 0 or entry_count != 0:
            raise ConfigContractError(
                "state.json daily_logs must use the null/zero cursor shape when no active daily log exists: "
                f"{path}"
            )
    else:
        if latest_entry_id is None or latest_entry_seq < 1 or entry_count < 1:
            raise ConfigContractError(
                "state.json daily_logs must record a non-null latest_entry_id and positive entry counters "
                f"when latest_file is set: {path}"
            )
    if "updated_at" in daily_logs:
        updated_at = daily_logs["updated_at"]
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ConfigContractError(
                f"state.json daily_logs.updated_at must be a non-empty string when present: {path}"
            )
    return data


def load_and_validate_config(
    path: Path,
    default_storage_mode: str,
    *,
    allow_unsupported_version: bool = False,
    allow_storage_mode_mismatch: bool = False,
) -> dict:
    try:
        data = load_json(path)
    except FileNotFoundError as exc:
        raise ConfigContractError(f"Missing config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigContractError(f"Malformed JSON in config file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigContractError(f"Config file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise ConfigContractError(f"Config file is not readable: {path} ({exc})") from exc

    if not isinstance(data, dict):
        raise ConfigContractError(f"Config file must contain a JSON object: {path}")

    required = {
        "protocol_version",
        "storage_mode",
        "workspace_language",
        "created_by",
        "created_at",
    }
    missing = sorted(required.difference(data.keys()))
    if missing:
        raise ConfigContractError(
            f"Config file is missing required fields {missing}: {path}"
        )

    storage_mode = validate_storage_mode(data.get("storage_mode", default_storage_mode))
    storage_mode_matches_path = storage_mode == default_storage_mode
    if not storage_mode_matches_path and not allow_storage_mode_mismatch:
        raise ConfigContractError(
            f"storage_mode '{storage_mode}' does not match the storage root implied by {path}"
        )
    workspace_language = validate_workspace_language(
        data.get("workspace_language", protocol_contracts.DEFAULT_WORKSPACE_LANGUAGE)
    )

    protocol_version = validate_protocol_version(data.get("protocol_version"))
    protocol_version_supported = protocol_version in protocol_contracts.SUPPORTED_PROTOCOL_VERSIONS
    if not protocol_version_supported and not allow_unsupported_version:
        raise ConfigContractError(
            f"Unsupported protocol_version {protocol_version} in config file: {path}"
        )

    created_by = data.get("created_by")
    created_at = data.get("created_at")
    if not isinstance(created_by, str) or not created_by.strip():
        raise ConfigContractError(f"created_by must be a non-empty string in config file: {path}")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ConfigContractError(f"created_at must be a non-empty string in config file: {path}")

    return {
        "protocol_version": protocol_version,
        "protocol_version_supported": protocol_version_supported,
        "storage_mode": storage_mode,
        "storage_mode_matches_path": storage_mode_matches_path,
        "workspace_language": workspace_language,
        "created_by": created_by,
        "created_at": created_at,
    }


def normalize_start_path(raw_path: str | Path) -> Path:
    try:
        path = Path(raw_path).expanduser().resolve()
        return path.parent if path.is_file() else path
    except OSError as exc:
        raise StorageResolutionError(
            f"Failed to resolve start path {raw_path}: {exc}",
            failure_reason="not_project_root",
        ) from exc


def project_lock_path(project_root: Path) -> Path:
    return project_root / WORKSPACE_LOCK_FILENAME


def load_lock_payload(lock_path: Path) -> dict:
    try:
        data = json.loads(read_text(lock_path))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_lock_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def reclaim_stale_workspace_lock(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False

    payload = load_lock_payload(lock_path)
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return False
    if pid_is_alive(pid):
        return False
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def workspace_write_lock(project_root: Path, owner: str):
    lock_path = project_lock_path(project_root)
    fd: int | None = None
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = json.dumps({"owner": owner, "pid": os.getpid(), "created_at": now_iso_timestamp()})
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
            break
        except FileExistsError:
            if not reclaim_stale_workspace_lock(lock_path):
                lock_payload = load_lock_payload(lock_path)
                lock_owner = lock_payload.get("owner", "unknown")
                lock_pid = lock_payload.get("pid", "unknown")
                lock_created_at = lock_payload.get("created_at", "unknown")
                raise LockBusyError(
                    "Refusing to continue because another RecallLoom mutating operation appears to be running for "
                    f"{project_root} (owner={lock_owner}, pid={lock_pid}, created_at={lock_created_at}). "
                    "If this lock is stale or malformed, inspect or remove it explicitly with unlock_write_lock.py."
                )
    else:
        raise LockBusyError(
            f"Refusing to continue because a stale RecallLoom lock could not be reclaimed for {project_root}."
        )
    try:
        yield lock_path
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def hidden_storage_root(project_root: Path) -> Path:
    return project_root / CONTEXT_DIRNAME


def visible_storage_root(project_root: Path) -> Path:
    return project_root / VISIBLE_DIRNAME


def daily_logs_dir(storage_root: Path) -> Path:
    return storage_root / DAILY_LOGS_DIRNAME


def storage_root_boundary_issue(project_root: Path, storage_root: Path, storage_mode: str) -> str | None:
    if not storage_root.exists() and not storage_root.is_symlink():
        return None
    mode_label = "hidden" if storage_mode == DEFAULT_STORAGE_MODE else "visible"
    try:
        project_root_resolved = project_root.resolve()
    except OSError as exc:
        raise ConfigContractError(
            f"Failed to resolve RecallLoom project root {project_root}: {exc}"
        ) from exc
    if storage_root.is_symlink():
        return (
            f"Refusing to continue because the {mode_label} RecallLoom storage root is a symlink: "
            f"{storage_root}. RecallLoom sidecars must be real directories directly under the project root."
        )
    try:
        resolved_storage_root = storage_root.resolve()
    except OSError as exc:
        raise ConfigContractError(
            f"Failed to resolve RecallLoom storage root {storage_root}: {exc}"
        ) from exc
    if resolved_storage_root.parent != project_root_resolved:
        return (
            f"Refusing to continue because the {mode_label} RecallLoom storage root resolves outside the project root: "
            f"{storage_root} -> {resolved_storage_root}."
        )
    return None


def recovery_storage_roots(project_root: Path) -> list[Path]:
    roots: list[Path] = []
    hidden = hidden_storage_root(project_root)
    visible = visible_storage_root(project_root)
    hidden_issue = storage_root_boundary_issue(project_root, hidden, DEFAULT_STORAGE_MODE)
    if hidden_issue is not None:
        raise ConfigContractError(hidden_issue)
    if is_recovery_storage_candidate(project_root, hidden, DEFAULT_STORAGE_MODE):
        roots.append(hidden)
    visible_issue = storage_root_boundary_issue(project_root, visible, VISIBLE_STORAGE_MODE)
    if visible_issue is not None:
        raise ConfigContractError(visible_issue)
    if is_recovery_storage_candidate(project_root, visible, VISIBLE_STORAGE_MODE):
        roots.append(visible)
    return roots


def visible_root_has_sidecar_signals(storage_root: Path) -> bool:
    signals = set(protocol_contracts.FILE_KEYS.values())
    signals.add(DAILY_LOGS_DIRNAME)
    if not storage_root.is_dir():
        return False
    return any((storage_root / name).exists() for name in signals)


def looks_like_installable_package_dir(storage_root: Path) -> bool:
    if not storage_root.is_dir():
        return False
    return (
        (storage_root / "package-metadata.json").is_file()
        and (storage_root / "SKILL.md").is_file()
        and (storage_root / "scripts").is_dir()
    )


def damaged_sidecar_reason(project_root: Path, storage_root: Path, storage_mode: str) -> str | None:
    boundary_issue = storage_root_boundary_issue(project_root, storage_root, storage_mode)
    if boundary_issue is not None:
        return boundary_issue
    config_path = storage_root / protocol_contracts.FILE_KEYS["config"]
    if config_path.is_file():
        return None

    if storage_mode == DEFAULT_STORAGE_MODE:
        if storage_root.is_file():
            return (
                f"Detected a damaged RecallLoom hidden sidecar under {project_root}: "
                f"{storage_root} exists but {config_path} is missing."
            )
        if storage_root.is_dir() and any(storage_root.iterdir()):
            return (
                f"Detected a damaged RecallLoom hidden sidecar under {project_root}: "
                f"{storage_root} exists and is non-empty, but {config_path} is missing."
            )
        return None

    if storage_root.is_dir():
        if looks_like_installable_package_dir(storage_root):
            return None
        if visible_root_has_sidecar_signals(storage_root):
            return (
                f"Detected a damaged RecallLoom visible sidecar under {project_root}: "
                f"{storage_root} contains RecallLoom sidecar files, but {config_path} is missing."
            )
    return None


def is_recovery_storage_candidate(project_root: Path, storage_root: Path, storage_mode: str) -> bool:
    if (storage_root / protocol_contracts.FILE_KEYS["config"]).is_file():
        return True
    return damaged_sidecar_reason(project_root, storage_root, storage_mode) is not None


def infer_storage_mode_from_root(storage_root: Path) -> str:
    if storage_root.name == CONTEXT_DIRNAME:
        return DEFAULT_STORAGE_MODE
    if storage_root.name == VISIBLE_DIRNAME:
        return VISIBLE_STORAGE_MODE
    raise StorageResolutionError(f"Cannot infer RecallLoom storage mode from path: {storage_root}")


def storage_root_for_mode(project_root: Path, storage_mode: str) -> Path:
    storage_mode = validate_storage_mode(storage_mode)
    if storage_mode == DEFAULT_STORAGE_MODE:
        return hidden_storage_root(project_root)
    return visible_storage_root(project_root)


def find_recovery_workspace(
    start_path: str | Path,
    *,
    requested_storage_mode: str | None = None,
) -> RecoveryWorkspaceInfo | None:
    start = normalize_start_path(start_path)
    if requested_storage_mode is not None:
        requested_storage_mode = validate_storage_mode(requested_storage_mode)

    for candidate in (start, *start.parents):
        roots = recovery_storage_roots(candidate)
        if not roots:
            continue

        if requested_storage_mode is not None:
            target_root = storage_root_for_mode(candidate, requested_storage_mode)
            if is_recovery_storage_candidate(candidate, target_root, requested_storage_mode):
                return RecoveryWorkspaceInfo(
                    project_root=candidate,
                    storage_root=target_root,
                    storage_mode=requested_storage_mode,
                )
            continue

        for root in roots:
            if start == root or root in start.parents:
                return RecoveryWorkspaceInfo(
                    project_root=candidate,
                    storage_root=root,
                    storage_mode=infer_storage_mode_from_root(root),
                )

        if len(roots) == 1:
            root = roots[0]
            return RecoveryWorkspaceInfo(
                project_root=candidate,
                storage_root=root,
                storage_mode=infer_storage_mode_from_root(root),
            )

        raise StorageResolutionError(
            f"Multiple RecallLoom storage roots exist under {candidate}. "
            "Re-run with an explicit storage mode or target a path inside the desired sidecar.",
            failure_reason="dual_sidecar_conflict",
        )

    return None


def find_recovery_project_root(start_path: str | Path) -> Path:
    start = normalize_start_path(start_path)
    for candidate in (start, *start.parents):
        if project_lock_path(candidate).exists():
            return candidate
        if recovery_storage_roots(candidate):
            return candidate
    return start


def config_path_for_mode(project_root: Path, storage_mode: str) -> Path:
    return storage_root_for_mode(project_root, storage_mode) / protocol_contracts.FILE_KEYS["config"]


def file_path(workspace: WorkspaceInfo, file_key: str) -> Path:
    if file_key == "daily_logs_dir":
        return daily_logs_dir(workspace.storage_root)
    return workspace.storage_root / protocol_contracts.FILE_KEYS[file_key]


def detect_workspace(
    project_root: Path,
    *,
    allow_unsupported_version: bool = False,
    allow_storage_mode_mismatch: bool = False,
) -> WorkspaceInfo | None:
    hidden_root = hidden_storage_root(project_root)
    hidden_config = hidden_root / protocol_contracts.FILE_KEYS["config"]
    visible_root = visible_storage_root(project_root)
    visible_config = visible_root / protocol_contracts.FILE_KEYS["config"]

    hidden_boundary_issue = storage_root_boundary_issue(project_root, hidden_root, DEFAULT_STORAGE_MODE)
    if hidden_boundary_issue is not None:
        raise ConfigContractError(hidden_boundary_issue, failure_reason="invalid_storage_boundary")
    visible_boundary_issue = storage_root_boundary_issue(project_root, visible_root, VISIBLE_STORAGE_MODE)
    if visible_boundary_issue is not None:
        raise ConfigContractError(visible_boundary_issue, failure_reason="invalid_storage_boundary")

    try:
        hidden_exists = hidden_config.is_file()
        visible_exists = visible_config.is_file()
        hidden_damaged = damaged_sidecar_reason(project_root, hidden_root, DEFAULT_STORAGE_MODE)
        visible_damaged = damaged_sidecar_reason(project_root, visible_root, VISIBLE_STORAGE_MODE)
    except OSError as exc:
        raise ConfigContractError(
            f"Failed to inspect RecallLoom storage roots under {project_root}: {exc}"
        ) from exc

    if hidden_exists and visible_exists:
        raise StorageResolutionError(
            f"Conflicting RecallLoom storage roots found under {project_root}: "
            f"{hidden_root} and {visible_root}. Resolve the conflict before continuing.",
            failure_reason="dual_sidecar_conflict",
        )

    if hidden_exists:
        if visible_damaged is not None:
            raise StorageResolutionError(
                f"Conflicting RecallLoom storage roots found under {project_root}: "
                f"{hidden_root} is valid, but {visible_root} is a damaged visible sidecar. "
                "Resolve the conflict before continuing.",
                failure_reason="dual_sidecar_conflict",
            )
        data = load_and_validate_config(
            hidden_config,
            DEFAULT_STORAGE_MODE,
            allow_unsupported_version=allow_unsupported_version,
            allow_storage_mode_mismatch=allow_storage_mode_mismatch,
        )
        return WorkspaceInfo(
            project_root=project_root,
            storage_root=hidden_root,
            storage_mode=DEFAULT_STORAGE_MODE,
            workspace_language=data["workspace_language"],
            config_path=hidden_config,
            declared_storage_mode=data["storage_mode"],
            storage_mode_matches_path=data["storage_mode_matches_path"],
            protocol_version=data["protocol_version"],
            protocol_version_supported=data["protocol_version_supported"],
        )

    if visible_exists:
        if hidden_damaged is not None:
            raise StorageResolutionError(
                f"Conflicting RecallLoom storage roots found under {project_root}: "
                f"{visible_root} is valid, but {hidden_root} is a damaged hidden sidecar. "
                "Resolve the conflict before continuing.",
                failure_reason="dual_sidecar_conflict",
            )
        data = load_and_validate_config(
            visible_config,
            VISIBLE_STORAGE_MODE,
            allow_unsupported_version=allow_unsupported_version,
            allow_storage_mode_mismatch=allow_storage_mode_mismatch,
        )
        return WorkspaceInfo(
            project_root=project_root,
            storage_root=visible_root,
            storage_mode=VISIBLE_STORAGE_MODE,
            workspace_language=data["workspace_language"],
            config_path=visible_config,
            declared_storage_mode=data["storage_mode"],
            storage_mode_matches_path=data["storage_mode_matches_path"],
            protocol_version=data["protocol_version"],
            protocol_version_supported=data["protocol_version_supported"],
        )

    if hidden_damaged is not None and visible_damaged is not None:
        raise StorageResolutionError(
            f"Detected conflicting damaged RecallLoom storage roots under {project_root}: "
            f"{hidden_root} and {visible_root}. Resolve the conflict before continuing.",
            failure_reason="dual_sidecar_conflict",
        )
    if hidden_damaged is not None:
        raise ConfigContractError(hidden_damaged, failure_reason="damaged_sidecar")
    if visible_damaged is not None:
        raise ConfigContractError(visible_damaged, failure_reason="damaged_sidecar")

    return None


def find_recallloom_root(
    start_path: str | Path,
    *,
    allow_unsupported_version: bool = False,
    allow_storage_mode_mismatch: bool = False,
) -> WorkspaceInfo | None:
    start = normalize_start_path(start_path)
    for candidate in (start, *start.parents):
        workspace = detect_workspace(
            candidate,
            allow_unsupported_version=allow_unsupported_version,
            allow_storage_mode_mismatch=allow_storage_mode_mismatch,
        )
        if workspace is not None:
            return workspace
    return None


def ensure_git_exclude_entry(project_root: Path, entry: str = f"{CONTEXT_DIRNAME}/") -> bool:
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return False

    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    block = [
        protocol_contracts.EXCLUDE_BLOCK_START,
        entry,
        protocol_contracts.EXCLUDE_BLOCK_END,
    ]
    if exclude_path.exists():
        current = exclude_path.read_text(encoding="utf-8")
        ok, reason = bridge_blocks.exclude_block_integrity(current)
        if not ok:
            detail_map = {
                "exclude_start_end_mismatch": "managed block start/end markers are mismatched",
                "exclude_duplicate_blocks": "multiple managed exclude blocks are present",
                "exclude_order_invalid": "managed block markers are out of order",
            }
            detail = detail_map.get(reason, "the managed exclude block is malformed")
            raise ConfigContractError(
                f"Refusing to update .git/info/exclude because {detail}: {exclude_path}"
            )
        managed_block = bridge_blocks.managed_exclude_block_text(current)
        if managed_block is not None and entry in managed_block:
            return True
        text = current.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
        atomic_write_if_unchanged(exclude_path, expected_text=current, new_text=text)
    else:
        text = "\n".join(block) + "\n"
        write_text(exclude_path, text)
    return True


def remove_git_exclude_block(project_root: Path) -> bool:
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return False

    exclude_path = git_dir / "info" / "exclude"
    if not exclude_path.exists():
        return False

    current = exclude_path.read_text(encoding="utf-8")
    ok, reason = bridge_blocks.exclude_block_integrity(current)
    if not ok:
        detail_map = {
            "exclude_start_end_mismatch": "managed block start/end markers are mismatched",
            "exclude_duplicate_blocks": "multiple managed exclude blocks are present",
            "exclude_order_invalid": "managed block markers are out of order",
        }
        detail = detail_map.get(reason, "the managed exclude block is malformed")
        raise ConfigContractError(
            f"Refusing to update .git/info/exclude because {detail}: {exclude_path}"
        )

    if bridge_blocks.managed_exclude_block_text(current) is None:
        return False

    lines = current.splitlines()
    start_marker = protocol_contracts.EXCLUDE_BLOCK_START
    end_marker = protocol_contracts.EXCLUDE_BLOCK_END
    start_idx = lines.index(start_marker)
    end_idx = lines.index(end_marker, start_idx)
    new_lines = lines[:start_idx] + lines[end_idx + 1 :]
    text = "\n".join(new_lines).strip("\n")
    atomic_write_if_unchanged(exclude_path, expected_text=current, new_text=((text + "\n") if text else ""))
    return True


def known_storage_assets(
    storage_root: Path,
    *,
    required_files: tuple[str, ...],
    optional_files: tuple[str, ...],
    required_directories: tuple[str, ...],
    managed_directories: tuple[str, ...],
) -> set[Path]:
    assets: set[Path] = set()
    for rel_path in [
        *required_files,
        *optional_files,
        *required_directories,
        *managed_directories,
    ]:
        assets.add(storage_root / PurePosixPath(rel_path))
    return assets


def known_storage_asset_kind_map(
    storage_root: Path,
    *,
    required_files: tuple[str, ...],
    optional_files: tuple[str, ...],
    required_directories: tuple[str, ...],
    managed_directories: tuple[str, ...],
) -> dict[Path, str]:
    asset_kinds: dict[Path, str] = {}
    for rel_path in [*required_files, *optional_files]:
        asset_kinds[storage_root / PurePosixPath(rel_path)] = "file"
    for rel_path in required_directories:
        asset_kinds[storage_root / PurePosixPath(rel_path)] = "directory"
    for rel_path in managed_directories:
        asset_kinds[storage_root / PurePosixPath(rel_path)] = "directory"
    return asset_kinds


def matches_dynamic_storage_asset_rule(
    path: Path,
    storage_root: Path,
    *,
    dynamic_rules: tuple[dict[str, str], ...],
) -> bool:
    if not path.is_file():
        return False
    for rule in dynamic_rules:
        base_dir = storage_root / PurePosixPath(rule["base_dir"])
        if path.parent != base_dir:
            continue
        kind = rule["kind"]
        if kind == "iso_daily_log" and DATE_FILE_RE.match(path.name):
            try:
                date.fromisoformat(path.stem)
            except ValueError:
                continue
            return True
        if kind == "recovery_proposal" and RECOVERY_PROPOSAL_FILE_RE.match(path.name):
            return True
        if kind == "review_record" and REVIEW_RECORD_FILE_RE.match(path.name):
            return True
    return False


def _matches_dynamic_storage_asset_rule(
    path: Path,
    storage_root: Path,
    *,
    dynamic_rules: tuple[dict[str, str], ...],
) -> bool:
    return matches_dynamic_storage_asset_rule(
        path,
        storage_root,
        dynamic_rules=dynamic_rules,
    )


def is_official_temp_storage_asset(
    path: Path,
    storage_root: Path,
    *,
    required_files: tuple[str, ...],
    optional_files: tuple[str, ...],
    dynamic_rules: tuple[dict[str, str], ...],
) -> bool:
    if not path.is_file():
        return False
    if not path.name.startswith("."):
        return False
    candidate_name, sep, _suffix = path.name[1:].partition(".tmp-")
    if not sep or not candidate_name:
        return False

    for rel_path in [*required_files, *optional_files]:
        rel = PurePosixPath(rel_path)
        if rel.name != candidate_name:
            continue
        expected_parent = storage_root if rel.parent.as_posix() in {".", ""} else storage_root / rel.parent
        if path.parent == expected_parent:
            return True

    for rule in dynamic_rules:
        base_dir = storage_root / PurePosixPath(rule["base_dir"])
        if path.parent != base_dir:
            continue
        kind = rule["kind"]
        if kind == "iso_daily_log" and DATE_FILE_RE.match(candidate_name):
            try:
                date.fromisoformat(Path(candidate_name).stem)
            except ValueError:
                return False
            return True
        if kind == "recovery_proposal" and RECOVERY_PROPOSAL_FILE_RE.match(candidate_name):
            return True
        if kind == "review_record" and REVIEW_RECORD_FILE_RE.match(candidate_name):
            return True

    return False


def unknown_storage_assets(
    storage_root: Path,
    *,
    required_files: tuple[str, ...],
    optional_files: tuple[str, ...],
    required_directories: tuple[str, ...],
    managed_directories: tuple[str, ...],
    dynamic_rules: tuple[dict[str, str], ...],
) -> list[Path]:
    if not storage_root.is_dir():
        return []
    known = known_storage_asset_kind_map(
        storage_root,
        required_files=required_files,
        optional_files=optional_files,
        required_directories=required_directories,
        managed_directories=managed_directories,
    )
    unknown: list[Path] = []

    for path in sorted(storage_root.rglob("*")):
        if path.name == ".DS_Store":
            continue
        if is_official_temp_storage_asset(
            path,
            storage_root,
            required_files=required_files,
            optional_files=optional_files,
            dynamic_rules=dynamic_rules,
        ):
            continue
        expected_kind = known.get(path)
        if expected_kind == "file" and path.is_file():
            continue
        if expected_kind == "directory" and path.is_dir():
            continue
        if matches_dynamic_storage_asset_rule(
            path,
            storage_root,
            dynamic_rules=dynamic_rules,
        ):
            continue
        unknown.append(path)

    return unknown
