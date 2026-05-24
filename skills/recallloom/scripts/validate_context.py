#!/usr/bin/env python3
"""Validate a RecallLoom workspace and its managed file contracts."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from core.bridge.blocks import (
    bridge_block_integrity,
    exclude_block_integrity,
    managed_exclude_block_text,
)
from core.continuity.workday import logical_workday_for
from core.output.privacy import publicize_json_value
from core.protocol.contracts import (
    BRIDGE_START,
    FILE_KEYS,
    LAST_WRITER_RE,
    OPTIONAL_SECTION_KEYS,
    ROOT_ENTRY_CANDIDATES,
    SECTION_KEYS,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from core.protocol.markers import (
    parse_daily_log_entry_marker,
    parse_daily_log_scaffold_marker,
    parse_file_marker,
    parse_file_state_marker,
)
from core.protocol.sections import (
    duplicate_section_keys,
    missing_section_keys,
    unknown_section_keys,
)

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    EnvironmentContractError,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    enforce_package_support_gate,
    exit_if_startup_scratch_residue,
    exit_with_cli_error,
    StorageResolutionError,
    ValidationFinding,
    CONTEXT_DIRNAME,
    ensure_supported_python_version,
    find_recallloom_root,
    invalid_iso_like_daily_log_files,
    load_workspace_state,
    is_optional_storage_file,
    is_required_storage_directory,
    is_required_storage_file,
    parse_daily_log_entry_line,
    unknown_storage_assets,
    parse_iso_date,
    read_text,
    sorted_active_daily_log_files,
    sorted_daily_log_files,
    validate_iso_date,
)

DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a RecallLoom workspace and its managed file contracts."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )
    return parser


def add_finding(findings: list[ValidationFinding], level: str, code: str, message: str, path: Path) -> None:
    findings.append(ValidationFinding(level=level, code=code, message=message, path=path))


def validate_marker(path: Path, text: str, expected_file_key: str, workspace, findings: list[ValidationFinding]):
    marker = parse_file_marker(text)
    if marker is None:
        add_finding(findings, "error", "missing_file_marker", "Missing RecallLoom file marker", path)
        return None
    if marker.file_key != expected_file_key:
        add_finding(
            findings,
            "error",
            "wrong_file_marker",
            f"Expected file marker '{expected_file_key}', found '{marker.file_key}'",
            path,
        )
    if marker.version not in SUPPORTED_PROTOCOL_VERSIONS:
        add_finding(
            findings,
            "error",
            "unsupported_protocol_marker_version",
            (
                f"Unsupported file marker version '{marker.version}'. "
                f"Supported protocol versions: {sorted(SUPPORTED_PROTOCOL_VERSIONS)}"
            ),
            path,
        )
    elif (
        workspace.protocol_version is not None
        and workspace.protocol_version_supported
        and marker.version != workspace.protocol_version
    ):
        add_finding(
            findings,
            "error",
            "protocol_marker_version_mismatch",
            (
                f"File marker version '{marker.version}' does not match "
                f"config protocol_version '{workspace.protocol_version}'"
            ),
            path,
        )
    return marker


def validate_language_match(
    path: Path,
    detected_language: str | None,
    expected_language: str,
    findings: list[ValidationFinding],
) -> None:
    if detected_language is None:
        return
    if detected_language != expected_language:
        add_finding(
            findings,
            "error",
            "workspace_language_mismatch",
            (
                f"File marker language '{detected_language}' does not match "
                f"workspace_language '{expected_language}'"
            ),
            path,
        )


def validate_file_state_marker(path: Path, text: str, findings: list[ValidationFinding]) -> None:
    state = parse_file_state_marker(text)
    if state is None:
        add_finding(
            findings,
            "error",
            "missing_file_state_marker",
            "Missing required file-state metadata marker",
            path,
        )


def validate_state_file_entry(
    state: dict | None,
    *,
    file_key: str,
    state_path: Path,
    findings: list[ValidationFinding],
) -> dict | None:
    if state is None:
        return None
    files_state = state.get("files", {})
    entry = files_state.get(file_key)
    if not isinstance(entry, dict):
        add_finding(
            findings,
            "error",
            "missing_state_file_entry",
            f"state.json is missing the required files.{file_key} entry",
            state_path,
        )
        return None
    if not isinstance(entry.get("file_revision"), int) or entry["file_revision"] < 1:
        add_finding(
            findings,
            "error",
            "invalid_state_file_revision",
            f"state.json files.{file_key}.file_revision must be a positive integer",
            state_path,
        )
    if not isinstance(entry.get("updated_at"), str) or not entry["updated_at"].strip():
        add_finding(
            findings,
            "error",
            "invalid_state_file_updated_at",
            f"state.json files.{file_key}.updated_at must be a non-empty string",
            state_path,
        )
    if not isinstance(entry.get("writer_id"), str) or not entry["writer_id"].strip():
        add_finding(
            findings,
            "error",
            "invalid_state_file_writer_id",
            f"state.json files.{file_key}.writer_id must be a non-empty string",
            state_path,
        )
    if (
        not isinstance(entry.get("base_workspace_revision"), int)
        or entry["base_workspace_revision"] < 1
    ):
        add_finding(
            findings,
            "error",
            "invalid_state_file_base_workspace_revision",
            f"state.json files.{file_key}.base_workspace_revision must be a positive integer",
            state_path,
        )
    return entry


def validate_file_state_against_snapshot(
    path: Path,
    *,
    file_key: str,
    file_state,
    state: dict | None,
    findings: list[ValidationFinding],
) -> None:
    if state is None or file_state is None:
        return
    state_path = path.parent / FILE_KEYS["state"]
    expected = validate_state_file_entry(state, file_key=file_key, state_path=state_path, findings=findings)
    if not expected:
        return
    if expected.get("file_revision") != file_state.revision:
        add_finding(
            findings,
            "error",
            f"{file_key}_revision_mismatch",
            f"{file_key} file-state revision does not match state.json",
            path,
        )
    if expected.get("updated_at") != file_state.updated_at:
        add_finding(
            findings,
            "error",
            f"{file_key}_updated_at_mismatch",
            f"{file_key} file-state updated-at does not match state.json",
            path,
        )
    if expected.get("writer_id") != file_state.writer_id:
        add_finding(
            findings,
            "error",
            f"{file_key}_writer_id_mismatch",
            f"{file_key} file-state writer-id does not match state.json",
            path,
        )
    if expected.get("base_workspace_revision") != file_state.base_workspace_revision:
        add_finding(
            findings,
            "error",
            f"{file_key}_base_workspace_revision_mismatch",
            f"{file_key} file-state base-workspace-revision does not match state.json",
            path,
        )


def validate_section_semantics(
    path: Path,
    text: str,
    required_keys: list[str],
    optional_keys: list[str],
    findings: list[ValidationFinding],
    missing_level: str = "error",
) -> None:
    allowed = [*required_keys, *optional_keys]

    missing = missing_section_keys(text, required_keys)
    for key in missing:
        add_finding(
            findings,
            missing_level,
            "missing_required_section",
            f"Missing required section marker: {key}",
            path,
        )

    duplicates = duplicate_section_keys(text)
    for key in duplicates:
        add_finding(
            findings,
            "error",
            "duplicate_section_marker",
            f"Duplicate section marker detected: {key}",
            path,
        )

    unknown = unknown_section_keys(text, allowed)
    for key in unknown:
        add_finding(
            findings,
            "warning",
            "unknown_section_marker",
            f"Unknown section marker detected: {key}",
            path,
        )


def validate_context_brief(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    path = workspace.storage_root / FILE_KEYS["context_brief"]
    if not path.is_file():
        level = "error" if is_required_storage_file(FILE_KEYS["context_brief"]) else "warning"
        add_finding(findings, level, "missing_context_brief", "Missing required file: context_brief.md", path)
        return
    text = read_text(path)
    marker = validate_marker(path, text, "context_brief", workspace, findings)
    validate_language_match(path, marker.language if marker else None, workspace.workspace_language, findings)
    validate_file_state_marker(path, text, findings)
    file_state = parse_file_state_marker(text)
    validate_file_state_against_snapshot(
        path,
        file_key="context_brief",
        file_state=file_state,
        state=state,
        findings=findings,
    )
    validate_section_semantics(
        path,
        text,
        SECTION_KEYS["context_brief"],
        OPTIONAL_SECTION_KEYS.get("context_brief", []),
        findings,
    )


def validate_rolling_summary(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    path = workspace.storage_root / FILE_KEYS["rolling_summary"]
    if not path.is_file():
        level = "error" if is_required_storage_file(FILE_KEYS["rolling_summary"]) else "warning"
        add_finding(findings, level, "missing_rolling_summary", "Missing required file: rolling_summary.md", path)
        return

    text = read_text(path)
    marker = validate_marker(path, text, "rolling_summary", workspace, findings)
    validate_language_match(path, marker.language if marker else None, workspace.workspace_language, findings)
    validate_file_state_marker(path, text, findings)
    file_state = parse_file_state_marker(text)
    validate_file_state_against_snapshot(
        path,
        file_key="rolling_summary",
        file_state=file_state,
        state=state,
        findings=findings,
    )
    lines = text.splitlines()
    if len(lines) < 3:
        add_finding(findings, "error", "invalid_rolling_summary", "rolling_summary.md is too short", path)
    else:
        match = LAST_WRITER_RE.match(lines[1].strip())
        if not match:
            add_finding(
                findings,
                "error",
                "invalid_last_writer",
                "rolling_summary.md second line must be a valid last-writer marker",
                path,
            )
        elif not validate_iso_date(match.group("date")):
            add_finding(
                findings,
                "error",
                "invalid_last_writer_date",
                "rolling_summary.md contains an invalid last-writer date",
                path,
            )

    validate_section_semantics(
        path,
        text,
        SECTION_KEYS["rolling_summary"],
        OPTIONAL_SECTION_KEYS.get("rolling_summary", []),
        findings,
    )
    if (
        state is not None
        and file_state is not None
        and state.get("workspace_revision", 0) > file_state.base_workspace_revision
    ):
        add_finding(
            findings,
            "warning",
            "rolling_summary_workspace_revision_behind",
            (
                "rolling_summary.md was last committed against an older workspace revision. "
                "Rerun preflight and review it before treating it as the current state."
            ),
            path,
        )


def validate_daily_logs(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    logs_dir = workspace.storage_root / DAILY_LOGS_DIRNAME
    if not logs_dir.is_dir():
        level = "error" if is_required_storage_directory("daily_logs") else "warning"
        add_finding(findings, level, "missing_daily_logs", "Missing required directory: daily_logs", logs_dir)
        return

    for log_path in invalid_iso_like_daily_log_files(logs_dir):
        add_finding(
            findings,
            "error",
            "invalid_daily_log_date",
            "Daily log filename matches the date pattern but is not a valid ISO date",
            log_path,
        )

    dated_logs = sorted_daily_log_files(logs_dir)
    active_logs = sorted_active_daily_log_files(logs_dir)
    latest_log_path = active_logs[-1] if active_logs else None
    logical_workday = logical_workday_for(
        datetime.now().astimezone(),
        DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
    )

    for log_path in active_logs:
        if parse_iso_date(log_path.stem) > logical_workday:
            add_finding(
                findings,
                "error",
                "future_dated_active_daily_log",
                (
                    "Future-dated active daily log is ahead of the current logical workday "
                    f"{logical_workday.isoformat()}."
                ),
                log_path,
            )

    for log_path in dated_logs:
        text = read_text(log_path)
        marker = validate_marker(log_path, text, "daily_log", workspace, findings)
        validate_language_match(log_path, marker.language if marker else None, workspace.workspace_language, findings)
        scaffold = parse_daily_log_scaffold_marker(text)
        if scaffold and parse_daily_log_entry_marker(text) is not None:
            add_finding(
                findings,
                "error",
                "invalid_daily_log_scaffold",
                "A scaffold daily log may not already contain daily-log entry metadata markers",
                log_path,
            )
            continue
        if not scaffold and parse_daily_log_entry_marker(text) is None:
            add_finding(
                findings,
                "error",
                "missing_daily_log_entry_marker",
                "Missing required daily-log entry metadata marker",
                log_path,
            )
            continue
        entry_markers = []
        entry_lines: list[list[str]] = []
        current_entry_lines: list[str] = []
        for line in text.splitlines()[1:]:
            entry = parse_daily_log_entry_line(line)
            if entry is not None:
                if current_entry_lines:
                    entry_lines.append(current_entry_lines)
                    current_entry_lines = []
                entry_markers.append(entry)
                continue
            if entry_markers:
                current_entry_lines.append(line)
        if current_entry_lines:
            entry_lines.append(current_entry_lines)

        if scaffold:
            validate_section_semantics(
                log_path,
                text,
                SECTION_KEYS["daily_log"],
                OPTIONAL_SECTION_KEYS.get("daily_log", []),
                findings,
            )
            continue

        if len(entry_markers) != len(entry_lines):
            add_finding(
                findings,
                "error",
                "invalid_daily_log_entry_structure",
                "Daily log entry blocks are malformed or incomplete",
                log_path,
            )
            continue

        for idx, entry in enumerate(entry_markers):
            block_text = "\n".join(entry_lines[idx])
            expected_seq = idx + 1
            if entry.entry_seq != expected_seq:
                add_finding(
                    findings,
                    "error",
                    "invalid_daily_log_entry_sequence",
                    (
                        f"Daily log entry sequence is not contiguous. Expected entry_seq={expected_seq}, "
                        f"found {entry.entry_seq}."
                    ),
                    log_path,
                )
            if entry.entry_id != f"entry-{entry.entry_seq}":
                add_finding(
                    findings,
                    "warning",
                    "noncanonical_daily_log_entry_id",
                    (
                        f"Daily log entry id '{entry.entry_id}' does not match the canonical "
                        f"'entry-{entry.entry_seq}' shape."
                    ),
                    log_path,
                )
            validate_section_semantics(
                log_path,
                block_text,
                SECTION_KEYS["daily_log"],
                OPTIONAL_SECTION_KEYS.get("daily_log", []),
                findings,
            )

        if state is not None and log_path == latest_log_path:
            daily_state = state.get("daily_logs", {})
            latest_entry = entry_markers[-1] if entry_markers else None
            if daily_state.get("entry_count") != len(entry_markers):
                add_finding(
                    findings,
                    "error",
                    "daily_log_entry_count_mismatch",
                    "Latest daily log entry count does not match state.json",
                    log_path,
                )
            if latest_entry is not None:
                if daily_state.get("latest_entry_id") != latest_entry.entry_id:
                    add_finding(
                        findings,
                        "error",
                        "daily_log_entry_id_mismatch",
                        "Latest daily-log entry id does not match state.json",
                        log_path,
                    )
                if daily_state.get("latest_entry_seq") != latest_entry.entry_seq:
                    add_finding(
                        findings,
                        "error",
                        "daily_log_entry_seq_mismatch",
                        "Latest daily-log entry sequence does not match state.json",
                        log_path,
                    )
                updated_at = daily_state.get("updated_at")
                if updated_at is None:
                    add_finding(
                        findings,
                        "warning",
                        "legacy_optional_metadata_missing",
                        (
                            "state.json daily_logs.updated_at is absent; legacy protocol 1.0 "
                            "sidecars may omit this optional freshness annotation."
                        ),
                        workspace.storage_root / FILE_KEYS["state"],
                    )
                elif isinstance(updated_at, str) and updated_at.strip():
                    try:
                        updated_at_dt = datetime.fromisoformat(updated_at)
                        latest_entry_dt = datetime.fromisoformat(latest_entry.created_at)
                    except ValueError:
                        add_finding(
                            findings,
                            "warning",
                            "invalid_daily_logs_updated_at_freshness",
                            (
                                "state.json daily_logs.updated_at or latest daily-log entry "
                                "created-at is not ISO parseable."
                            ),
                            workspace.storage_root / FILE_KEYS["state"],
                        )
                    else:
                        if updated_at_dt < latest_entry_dt:
                            add_finding(
                                findings,
                                "warning",
                                "daily_logs_updated_at_stale",
                                "state.json daily_logs.updated_at is older than the latest daily-log entry.",
                                workspace.storage_root / FILE_KEYS["state"],
                            )


def validate_unknown_storage_assets(workspace, findings: list[ValidationFinding]) -> None:
    invalid_daily_logs = set(invalid_iso_like_daily_log_files(workspace.storage_root / DAILY_LOGS_DIRNAME))
    for path in unknown_storage_assets(workspace.storage_root):
        if path in invalid_daily_logs:
            continue
        add_finding(
            findings,
            "error",
            "unexpected_storage_asset",
            "Unexpected non-managed asset detected inside the RecallLoom storage root",
            path,
        )


def validate_update_protocol(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    path = workspace.storage_root / FILE_KEYS["update_protocol"]
    if not path.is_file():
        level = "warning" if is_optional_storage_file(FILE_KEYS["update_protocol"]) else "error"
        message = "Missing recommended file: update_protocol.md" if level == "warning" else "Missing required file: update_protocol.md"
        add_finding(findings, level, "missing_update_protocol", message, path)
        return
    text = read_text(path)
    marker = validate_marker(path, text, "update_protocol", workspace, findings)
    validate_language_match(path, marker.language if marker else None, workspace.workspace_language, findings)
    validate_file_state_marker(path, text, findings)
    file_state = parse_file_state_marker(text)
    validate_file_state_against_snapshot(
        path,
        file_key="update_protocol",
        file_state=file_state,
        state=state,
        findings=findings,
    )
    if state is not None and file_state is not None:
        if state.get("update_protocol_revision") != file_state.revision:
            add_finding(
                findings,
                "error",
                "update_protocol_workspace_revision_mismatch",
                "update_protocol_revision in state.json does not match update_protocol file-state revision",
                path,
            )
    validate_section_semantics(
        path,
        text,
        ["project_specific_overrides"],
        OPTIONAL_SECTION_KEYS.get("update_protocol", []),
        findings,
    )


def validate_config(workspace, findings: list[ValidationFinding]) -> None:
    if workspace.config_path is None or not workspace.config_path.is_file():
        level = "error" if is_required_storage_file(FILE_KEYS["config"]) else "warning"
        add_finding(
            findings,
            level,
            "missing_config",
            "Managed storage modes require config.json in the storage root",
            workspace.storage_root / FILE_KEYS["config"],
        )


def validate_state_file(workspace, findings: list[ValidationFinding]) -> None:
    path = workspace.storage_root / FILE_KEYS["state"]
    if not path.is_file():
        level = "error" if is_required_storage_file(FILE_KEYS["state"]) else "warning"
        add_finding(findings, level, "missing_state", "Missing required file: state.json", path)
        return
    try:
        state = load_workspace_state(path)
    except ConfigContractError as exc:
        add_finding(findings, "error", "invalid_state", str(exc), path)
        return
    if not isinstance(state.get("workspace_revision"), int) or state["workspace_revision"] < 1:
        add_finding(findings, "error", "invalid_workspace_revision", "state.json must contain a positive workspace_revision", path)
    if not isinstance(state.get("update_protocol_revision"), int) or state["update_protocol_revision"] < 1:
        add_finding(findings, "error", "invalid_update_protocol_revision", "state.json must contain a positive update_protocol_revision", path)
        return
    if state.get("git_exclude_mode") not in {"managed", "skipped", "not_applicable"}:
        add_finding(findings, "error", "invalid_git_exclude_mode", "state.json contains an invalid git_exclude_mode", path)
    if not isinstance(state.get("bridged_entries"), dict):
        add_finding(findings, "error", "invalid_bridged_entries", "state.json bridged_entries must be an object", path)
    else:
        for rel_target, bridge_state in state["bridged_entries"].items():
            if not isinstance(rel_target, str) or not rel_target:
                add_finding(findings, "error", "invalid_bridged_entry_key", "state.json bridged_entries keys must be non-empty strings", path)
            if not isinstance(bridge_state, dict):
                add_finding(findings, "error", "invalid_bridged_entry_state", "state.json bridged_entries values must be objects", path)
    if not isinstance(state.get("files"), dict):
        add_finding(findings, "error", "invalid_state_files", "state.json files must be an object", path)
    else:
        for file_key in ("context_brief", "rolling_summary", "update_protocol"):
            validate_state_file_entry(state, file_key=file_key, state_path=path, findings=findings)
    if not isinstance(state.get("daily_logs"), dict):
        add_finding(findings, "error", "invalid_daily_logs_state", "state.json daily_logs must be an object", path)
    else:
        daily_state = state["daily_logs"]
        latest_file = daily_state.get("latest_file")
        if latest_file is not None and (not isinstance(latest_file, str) or not latest_file.strip()):
            add_finding(findings, "error", "invalid_latest_daily_log", "state.json daily_logs.latest_file must be null or a non-empty string", path)
        if not isinstance(daily_state.get("latest_entry_seq"), int) or daily_state["latest_entry_seq"] < 0:
            add_finding(findings, "error", "invalid_latest_daily_log_entry_seq", "state.json daily_logs.latest_entry_seq must be a non-negative integer", path)
        latest_entry_id = daily_state.get("latest_entry_id")
        if latest_entry_id is not None and (not isinstance(latest_entry_id, str) or not latest_entry_id.strip()):
            add_finding(findings, "error", "invalid_latest_daily_log_entry_id", "state.json daily_logs.latest_entry_id must be null or a non-empty string", path)
        if not isinstance(daily_state.get("entry_count"), int) or daily_state["entry_count"] < 0:
            add_finding(findings, "error", "invalid_daily_log_entry_count", "state.json daily_logs.entry_count must be a non-negative integer", path)
        if daily_state.get("entry_count", 0) < daily_state.get("latest_entry_seq", 0):
            add_finding(findings, "error", "invalid_daily_log_entry_count_order", "state.json daily_logs.entry_count cannot be smaller than latest_entry_seq", path)

    if (
        workspace.protocol_version is not None
        and not workspace.protocol_version_supported
    ):
        add_finding(
            findings,
            "error",
            "unsupported_protocol_version",
            (
                f"Unsupported protocol_version '{workspace.protocol_version}'. "
                f"Supported protocol versions: {sorted(SUPPORTED_PROTOCOL_VERSIONS)}"
            ),
            workspace.config_path,
        )
    if not workspace.storage_mode_matches_path:
        add_finding(
            findings,
            "error",
            "storage_mode_path_mismatch",
            (
                f"Config declares storage_mode '{workspace.declared_storage_mode}', "
                f"but the detected storage root implies '{workspace.storage_mode}'"
            ),
            workspace.config_path,
        )


def load_state_snapshot(workspace, findings: list[ValidationFinding]) -> dict | None:
    path = workspace.storage_root / FILE_KEYS["state"]
    if not path.is_file():
        return None
    try:
        return load_workspace_state(path)
    except ConfigContractError as exc:
        if not any(f.code == "invalid_state" and f.path == path for f in findings):
            add_finding(findings, "error", "invalid_state", str(exc), path)
        return None


def validate_daily_log_state(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    if state is None:
        return
    daily_state = state.get("daily_logs", {})
    latest_file = daily_state.get("latest_file")
    actual_latest = sorted_active_daily_log_files(workspace.storage_root / DAILY_LOGS_DIRNAME)
    actual_latest_rel = (
        actual_latest[-1].relative_to(workspace.storage_root).as_posix()
        if actual_latest
        else None
    )
    if latest_file is None:
        if actual_latest_rel is not None:
            add_finding(
                findings,
                "error",
                "missing_latest_daily_log_state",
                "state.json does not record the latest daily log even though one exists",
                workspace.storage_root / FILE_KEYS["state"],
            )
        return
    if actual_latest_rel is None:
        add_finding(
            findings,
            "error",
            "unexpected_latest_daily_log_state",
            "state.json records a latest active daily log even though no active daily log currently exists",
            workspace.storage_root / FILE_KEYS["state"],
        )
        return
    latest_path = workspace.storage_root / latest_file
    if not latest_path.is_file():
        add_finding(
            findings,
            "error",
            "missing_latest_daily_log",
            "state.json points to a latest daily log file that does not exist",
            workspace.storage_root / FILE_KEYS["state"],
        )
    elif actual_latest_rel is not None and latest_file != actual_latest_rel:
        add_finding(
            findings,
            "error",
            "latest_daily_log_state_mismatch",
            "state.json latest daily log does not match the latest ISO-dated daily log",
            workspace.storage_root / FILE_KEYS["state"],
        )


def validate_bridge_blocks(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    bridged_entries = state.get("bridged_entries", {}) if state else {}
    for rel_path in ROOT_ENTRY_CANDIDATES:
        path = workspace.project_root / rel_path
        if not path.is_file():
            if state and rel_path.as_posix() in bridged_entries:
                add_finding(
                    findings,
                    "error",
                    "missing_bridged_entry_target",
                    "state.json records a bridged entry whose target file is missing",
                    workspace.project_root / rel_path,
                )
            continue
        ok, reason = bridge_block_integrity(read_text(path))
        if not ok:
            add_finding(
                findings,
                "error",
                reason or "bridge_block_invalid",
                "RecallLoom managed bridge block is malformed or incomplete",
                path,
            )
            continue

        has_bridge_block = BRIDGE_START in read_text(path)
        rel_key = rel_path.as_posix()
        state_records_entry = bool(state and rel_key in bridged_entries)
        if state_records_entry and not has_bridge_block:
            add_finding(
                findings,
                "error",
                "missing_bridged_entry_block",
                "state.json records a bridged entry but the target file has no managed bridge block",
                path,
            )
        if has_bridge_block and not state_records_entry:
            add_finding(
                findings,
                "error",
                "untracked_bridged_entry_block",
                "A managed bridge block exists in the target file but state.json does not record it",
                path,
            )


def validate_exclude_block(workspace, state: dict | None, findings: list[ValidationFinding]) -> None:
    if workspace.storage_mode != "hidden":
        return
    exclude_path = workspace.project_root / ".git" / "info" / "exclude"
    expected_mode = state.get("git_exclude_mode") if state else None
    if not exclude_path.is_file():
        if expected_mode == "managed":
            add_finding(
                findings,
                "error",
                "missing_git_exclude_block",
                "state.json expects a managed git exclude block, but .git/info/exclude is missing",
                exclude_path,
            )
        return
    exclude_text = read_text(exclude_path)
    ok, reason = exclude_block_integrity(exclude_text)
    if not ok:
        add_finding(
            findings,
            "error" if expected_mode == "managed" else "warning",
            reason or "exclude_block_invalid",
            "RecallLoom managed exclude block is malformed or incomplete",
            exclude_path,
        )
        return

    block_text = managed_exclude_block_text(exclude_text)
    has_block = block_text is not None
    if expected_mode == "managed":
        if not has_block:
            add_finding(
                findings,
                "error",
                "missing_git_exclude_block",
                "state.json expects a managed git exclude block, but none is present",
                exclude_path,
            )
        elif f"{CONTEXT_DIRNAME}/" not in block_text:
            add_finding(
                findings,
                "error",
                "git_exclude_entry_missing",
                f"state.json expects a managed git exclude entry for {CONTEXT_DIRNAME}/, but it is missing from the managed block",
                exclude_path,
            )
    elif has_block:
        add_finding(
            findings,
            "error",
            "unexpected_git_exclude_block",
            "A managed git exclude block exists, but state.json does not record git_exclude_mode=managed",
            exclude_path,
        )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload(
                "python_runtime_unavailable",
                error=str(exc),
                extra={"valid": False},
            ),
        )
    enforce_package_support_gate(parser, json_mode=args.json)

    try:
        workspace = find_recallloom_root(
            args.path,
            allow_unsupported_version=True,
            allow_storage_mode_mismatch=True,
        )
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"valid": False},
            ),
        )
    if workspace is None:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload(
                "no_project_root",
                error="No RecallLoom project root found.",
                extra={"valid": False},
            ),
        )
    startup_residue_report = exit_if_startup_scratch_residue(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )

    findings: list[ValidationFinding] = []
    try:
        validate_config(workspace, findings)
        validate_state_file(workspace, findings)
        state = load_state_snapshot(workspace, findings)
        validate_context_brief(workspace, state, findings)
        validate_rolling_summary(workspace, state, findings)
        validate_daily_logs(workspace, state, findings)
        validate_daily_log_state(workspace, state, findings)
        validate_update_protocol(workspace, state, findings)
        validate_bridge_blocks(workspace, state, findings)
        validate_exclude_block(workspace, state, findings)
        validate_unknown_storage_assets(workspace, findings)
    except (OSError, UnicodeDecodeError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Filesystem error: {exc}",
            payload=cli_failure_payload(
                "damaged_sidecar",
                error=f"Filesystem error: {exc}",
                extra={"valid": False},
            ),
        )

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]

    payload = {
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "storage_mode": workspace.storage_mode,
        "declared_storage_mode": workspace.declared_storage_mode,
        "workspace_language": workspace.workspace_language,
        "protocol_version": workspace.protocol_version,
        "valid": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "findings": [
            {
                "level": finding.level,
                "code": finding.code,
                "message": finding.message,
                "path": str(finding.path),
            }
            for finding in findings
        ],
    }

    if args.json:
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report
        public_payload = publicize_json_value(payload, project_root=workspace.project_root)
        print(json.dumps(public_payload, ensure_ascii=False, indent=2))
    else:
        print(f"RecallLoom root: {workspace.project_root}")
        print(f"Storage root: {workspace.storage_root}")
        print(f"Storage mode: {workspace.storage_mode}")
        print(f"Workspace language: {workspace.workspace_language}")
        if findings:
            for finding in findings:
                print(f"[{finding.level.upper()}] {finding.code}: {finding.message} ({finding.path})")
        else:
            print("No findings.")
        print(f"Errors: {len(errors)} | Warnings: {len(warnings)}")

    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
