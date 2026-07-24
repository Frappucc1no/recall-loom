#!/usr/bin/env python3
"""Run freshness and write-target checks before updating RecallLoom files."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from core.continuity.freshness import (
    continuity_digest_bundle,
    continuity_state_for_workspace as shared_continuity_state_for_workspace,
    evaluate_continuity_freshness,
    freshness_risk_summary,
    is_effectively_empty_summary_next_step as shared_is_effectively_empty_summary_next_step,
    summary_matches_empty_shell_template as shared_summary_matches_empty_shell_template,
)
from core.continuity.workday import (
    DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
    build_workday_decision,
    build_write_tier_judgment,
    detect_closure_signal,
)
from core.output.user_status import preflight_user_summary, print_user_summary
from core.trust.state import evaluate_trust_state
from core.provenance.evidence import (
    strict_gate_current_receipts_verified,
    strict_sidecar_no_write_failure_extra,
    strict_sidecar_integrity_gate,
    strict_sidecar_integrity_gate_public_summary,
)
from core.provenance.state import (
    build_provenance_report,
    expected_revisions_payload,
    provenance_facts_from_state,
    provenance_contract_identity,
)
from core.protocol.contracts import FILE_KEYS
from core.protocol.markers import parse_file_state_marker

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue,
    exit_with_cli_error,
    find_recallloom_root,
    invalid_iso_like_daily_log_files,
    DailyLogCursorError,
    latest_active_daily_log,
    latest_active_daily_log_cursor,
    load_workspace_state,
    validate_state_entry_bearing_latest_daily_log,
    detect_update_protocol_time_policy_cues,
    extract_section_text,
    parse_daily_log_entry_line,
    parse_daily_log_scaffold_marker,
    parse_iso_date,
    public_project_path,
    public_project_root_label,
    read_text,
    StorageResolutionError,
)

def sha256_text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def summary_matches_empty_shell_template(summary_text: str) -> bool:
    return shared_summary_matches_empty_shell_template(summary_text)


def continuity_state_for_workspace(
    *,
    state: dict,
    summary_text: str,
    latest_daily_log_exists: bool,
) -> tuple[str, bool]:
    return shared_continuity_state_for_workspace(
        state=state,
        summary_text=summary_text,
        latest_daily_log_exists=latest_daily_log_exists,
    )


def is_effectively_empty_summary_next_step(text: str) -> bool:
    return shared_is_effectively_empty_summary_next_step(text)


def latest_daily_log_marker_summary(path: Path | None) -> tuple[object | None, int, bool]:
    if path is None:
        return None, 0, False
    text = read_text(path)
    latest_entry = None
    entry_count = 0
    for line in text.splitlines():
        entry = parse_daily_log_entry_line(line)
        if entry is not None:
            latest_entry = entry
            entry_count += 1
    return latest_entry, entry_count, parse_daily_log_scaffold_marker(text)


def latest_daily_log_entry_text_digest(text: str, latest_entry) -> str | None:
    if latest_entry is None:
        return None
    lines = text.splitlines()
    start_index = None
    end_index = len(lines)
    for index, line in enumerate(lines):
        entry = parse_daily_log_entry_line(line)
        if entry is None:
            continue
        if start_index is None:
            if (
                entry.entry_id == latest_entry.entry_id
                and entry.entry_seq == latest_entry.entry_seq
                and entry.created_at == latest_entry.created_at
            ):
                start_index = index
        else:
            end_index = index
            break
    if start_index is None:
        return None
    entry_text = "\n".join(lines[start_index:end_index]).rstrip("\n") + "\n"
    return sha256_text_digest(entry_text)


def file_state_marker_from_path(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            state = parse_file_state_marker(line)
            if state is not None:
                return state
            if index >= 24:
                break
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check freshness and likely write targets before updating RecallLoom files."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    scan_mode_group = parser.add_mutually_exclusive_group()
    scan_mode_group.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Use the sidecar-visible freshness path only. This is now the default behavior and is kept "
            "as an explicit flag for compatibility."
        ),
    )
    scan_mode_group.add_argument(
        "--full",
        action="store_true",
        help=(
            "Run the heavier workspace artifact scan in addition to sidecar-visible signals. "
            "Use this when you want a deeper freshness pass before a high-confidence write."
        ),
    )
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="Exit non-zero if a non-context workspace artifact is newer than the rolling summary.",
    )
    parser.add_argument(
        "--skip-startup-residue-scan",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def recommended_actions_for_preflight(
    *,
    continuity_confidence: str,
    continuity_state: str,
    update_protocol_exists: bool,
    context_brief_exists: bool,
    latest_daily_log_exists: bool,
    workspace_is_newer: bool,
) -> list[str]:
    actions: list[str] = []
    if continuity_state == "initialized_empty_shell":
        actions.append("seed_initial_continuity")
    elif workspace_is_newer:
        actions.append("update_rolling_summary")
    else:
        actions.append("resume_from_summary")
    if update_protocol_exists:
        actions.append("review_update_protocol")
    if context_brief_exists:
        actions.append("review_context_brief")
    if latest_daily_log_exists:
        actions.append("review_latest_daily_log")
    if (
        continuity_state != "initialized_empty_shell"
        and continuity_confidence == "medium"
        and "update_rolling_summary" not in actions
    ):
        actions.append("consider_refresh_summary")
    return actions


def strict_gate_preflight_actions(actions: list[str], *, strict_gate_allows_write: bool) -> list[str]:
    if strict_gate_allows_write:
        return actions
    blocked_write_actions = {
        "seed_initial_continuity",
        "update_rolling_summary",
        "consider_refresh_summary",
    }
    return [
        "review_or_repair_sidecar_before_write",
        *(action for action in actions if action not in blocked_write_actions),
    ]


def strict_gate_preflight_failure_extra(workspace) -> dict[str, object]:
    return strict_sidecar_no_write_failure_extra(
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        continuity_confidence="broken",
    )


def managed_body_digest(text: str) -> str:
    """Digest managed markdown body excluding RecallLoom header metadata."""

    lines = text.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().startswith("<!-- recallloom:file=rolling_summary ")
        and lines[1].strip().startswith("<!-- last-writer:")
        and lines[2].strip().startswith("<!-- file-state:")
    ):
        body = "\n".join(lines[3:]).lstrip("\n").rstrip("\n") + "\n"
    else:
        body = text.rstrip("\n") + "\n"
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def post_append_assertion_binding_seed(
    *,
    expected_workspace_revision: int,
    expected_file_revision: int,
    summary_base_workspace_revision: int | None,
    latest_daily_log_entry,
    latest_daily_log_digest: str | None,
    latest_daily_log_entry_hash: str | None,
    body_digest_before: str,
) -> dict:
    latest_created_at = latest_daily_log_entry.created_at if latest_daily_log_entry else None
    return {
        "assertion_kind": "post_append_summary_metadata_only_semantic_unchanged",
        "expected_workspace_revision": expected_workspace_revision,
        "expected_file_revision": expected_file_revision,
        "summary_base_workspace_revision": summary_base_workspace_revision,
        "latest_daily_log_entry_id": (
            latest_daily_log_entry.entry_id if latest_daily_log_entry else None
        ),
        "latest_daily_log_entry_seq": (
            latest_daily_log_entry.entry_seq if latest_daily_log_entry else None
        ),
        "latest_daily_log_entry_created_at": latest_created_at,
        "latest_daily_log_entry_date": latest_created_at[:10] if latest_created_at else None,
        "latest_daily_log_entry_hash": latest_daily_log_entry_hash,
        "latest_daily_log_digest": latest_daily_log_digest,
        "body_digest_before": body_digest_before,
    }


def build_post_append_summary_sync_contract(
    *,
    workspace,
    state: dict,
    summary_path: Path,
    summary_state,
    latest_daily_log: Path | None,
    latest_daily_log_entry,
    latest_daily_log_entry_count: int,
    latest_daily_log_digest: str | None,
    continuity_seeded: bool,
    summary_revision_is_stale: bool,
    summary_stale: bool,
    workspace_is_newer: bool,
    allowed_operation_level: str,
    rolling_summary_handoff: dict,
) -> dict:
    safe_rolling_summary_handoff = {
        "active_task_digest": rolling_summary_handoff.get("active_task_digest"),
        "blocked_digest": rolling_summary_handoff.get("blocked_digest"),
        "latest_daily_log_digest_available": bool(
            rolling_summary_handoff.get("latest_relevant_log_digest")
        ),
        "latest_relevant_log_digest_redacted": bool(
            rolling_summary_handoff.get("latest_relevant_log_digest")
        ),
        "suggested_handoff_sections": rolling_summary_handoff.get("suggested_handoff_sections", []),
    }
    target_path = summary_path.relative_to(workspace.project_root).as_posix()
    summary_text = read_text(summary_path) if summary_path.is_file() else ""
    body_digest_before = managed_body_digest(summary_text) if summary_text else None
    latest_daily_log_path = (
        latest_daily_log.relative_to(workspace.project_root).as_posix()
        if latest_daily_log is not None
        else None
    )
    read_set = [target_path]
    if latest_daily_log_path is not None:
        read_set.append(latest_daily_log_path)

    daily_state = state.get("daily_logs")
    latest_file_from_state = daily_state.get("latest_file") if isinstance(daily_state, dict) else None
    latest_daily_log_storage_path = (
        latest_daily_log.relative_to(workspace.storage_root).as_posix()
        if latest_daily_log is not None
        else None
    )
    cursor_matches_latest_log = (
        isinstance(daily_state, dict)
        and latest_daily_log_entry is not None
        and latest_file_from_state == latest_daily_log_storage_path
        and daily_state.get("latest_entry_id") == latest_daily_log_entry.entry_id
        and daily_state.get("latest_entry_seq") == latest_daily_log_entry.entry_seq
        and daily_state.get("entry_count") == latest_daily_log_entry_count
    )
    workspace_revision = state["workspace_revision"]
    summary_base_workspace_revision = summary_state.base_workspace_revision if summary_state else None
    single_revision_append_delta = (
        isinstance(summary_base_workspace_revision, int)
        and workspace_revision == summary_base_workspace_revision + 1
    )
    provenance = state.get("provenance")
    review_imported_baseline_plus_append_delta = (
        isinstance(summary_base_workspace_revision, int)
        and workspace_revision == summary_base_workspace_revision + 2
        and isinstance(provenance, dict)
        and provenance.get("state_label") == "helper_evidenced"
        and provenance.get("previous_state_label") == "review_imported_baseline"
        and provenance.get("receipt_backed") is True
    )
    single_append_delta = (
        single_revision_append_delta or review_imported_baseline_plus_append_delta
    )
    non_summary_writes_after_summary_base: list[str] = []
    files_state = state.get("files")
    if isinstance(files_state, dict) and isinstance(summary_base_workspace_revision, int):
        for file_key, file_state in sorted(files_state.items()):
            if file_key == "rolling_summary" or not isinstance(file_state, dict):
                continue
            base_revision = file_state.get("base_workspace_revision")
            if isinstance(base_revision, int) and base_revision > summary_base_workspace_revision:
                non_summary_writes_after_summary_base.append(file_key)
    latest_daily_log_newer_than_summary = (
        latest_daily_log is not None
        and latest_daily_log.stat().st_mtime > summary_path.stat().st_mtime
    )
    latest_daily_log_not_older_than_summary = (
        latest_daily_log is not None
        and latest_daily_log.stat().st_mtime >= summary_path.stat().st_mtime
    )

    reason_code = None
    if not summary_stale or not workspace_is_newer:
        reason_code = "summary_not_stale"
    elif not summary_revision_is_stale:
        reason_code = "summary_revision_not_stale"
    elif not isinstance(summary_base_workspace_revision, int):
        reason_code = "missing_summary_base_workspace_revision"
    elif non_summary_writes_after_summary_base:
        reason_code = "stale_cause_not_append_only"
    elif not single_append_delta:
        reason_code = "stale_not_single_append_delta"
    elif not continuity_seeded:
        reason_code = "continuity_not_seeded"
    elif latest_daily_log is None:
        reason_code = "missing_latest_daily_log"
    elif latest_daily_log_entry is None:
        reason_code = "missing_latest_daily_log_entry"
    elif not isinstance(daily_state, dict):
        reason_code = "invalid_daily_log_cursor"
    elif not cursor_matches_latest_log:
        reason_code = "daily_log_cursor_mismatch"
    elif not latest_daily_log_not_older_than_summary:
        reason_code = "stale_cause_not_append_only"

    latest_entry_created_at = latest_daily_log_entry.created_at if latest_daily_log_entry else None
    latest_daily_log_entry_hash = (
        latest_daily_log_entry_text_digest(
            read_text(latest_daily_log) if latest_daily_log is not None else "",
            latest_daily_log_entry,
        )
        if latest_daily_log_entry is not None
        else None
    )
    append_cursor = {
        "latest_file": latest_file_from_state if isinstance(daily_state, dict) else None,
        "latest_entry_id": latest_daily_log_entry.entry_id if latest_daily_log_entry else None,
        "latest_entry_seq": latest_daily_log_entry.entry_seq if latest_daily_log_entry else None,
        "latest_entry_created_at": latest_entry_created_at,
        "latest_entry_date": latest_entry_created_at[:10] if latest_entry_created_at else None,
        "latest_entry_hash": latest_daily_log_entry_hash,
        "entry_count": latest_daily_log_entry_count if latest_daily_log is not None else None,
        "latest_file_digest": latest_daily_log_digest,
    }
    assertion_binding_seed = (
        post_append_assertion_binding_seed(
            expected_workspace_revision=workspace_revision,
            expected_file_revision=summary_state.revision,
            summary_base_workspace_revision=summary_base_workspace_revision,
            latest_daily_log_entry=latest_daily_log_entry,
            latest_daily_log_digest=latest_daily_log_digest,
            latest_daily_log_entry_hash=latest_daily_log_entry_hash,
            body_digest_before=body_digest_before,
        )
        if summary_state is not None and body_digest_before is not None
        else None
    )

    contract = {
        "contract_type": "post_append_summary_sync",
        "allowed": reason_code is None,
        "requires_repair_command_first": True,
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_format": "json",
        "target_path": target_path,
        "expected_file_revision": summary_state.revision if summary_state else None,
        "expected_workspace_revision": workspace_revision,
        "append_cursor": append_cursor,
        "managed_body_digest_before": body_digest_before,
        "metadata_only_refresh": {
            "allowed_assertion_sources": [
                "record_plan_output_id",
                "explicit_operator_confirmation",
            ],
            "requires_semantic_unchanged_assertion": True,
            "assertion_binding_seed": assertion_binding_seed,
            "assertion_binding_seed_digest": (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        assertion_binding_seed,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                if assertion_binding_seed is not None
                else None
            ),
        },
        "provenance_guard": {
            "summary_base_workspace_revision": summary_base_workspace_revision,
            "workspace_revision": workspace_revision,
            "expected_workspace_revision_delta": (
                2 if review_imported_baseline_plus_append_delta else 1
            ),
            "single_revision_append_delta": single_revision_append_delta,
            "review_imported_baseline_plus_append_delta": (
                review_imported_baseline_plus_append_delta
            ),
            "single_append_delta": single_append_delta,
            "cursor_matches_latest_log": cursor_matches_latest_log,
            "non_summary_writes_after_summary_base": non_summary_writes_after_summary_base,
            "latest_daily_log_newer_than_summary": latest_daily_log_newer_than_summary,
            "latest_daily_log_not_older_than_summary": latest_daily_log_not_older_than_summary,
        },
        "rolling_summary_handoff": safe_rolling_summary_handoff,
        "read_set": read_set,
        "ordinary_write_gate_preserved": True,
        "ordinary_write_gate": {
            "allowed_operation_level": allowed_operation_level,
            "summary_stale": summary_stale,
            "gate_condition": (
                "recallloom.py write requires allowed_operation_level="
                "write_current_state_after_preflight and summary_stale=false"
            ),
            "contract_does_not_authorize_recallloom_write": True,
        },
    }
    if reason_code is not None:
        contract["reason_code"] = reason_code
    return contract


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
            payload=cli_failure_payload("python_runtime_unavailable", error=str(exc)),
        )
    enforce_package_support_gate(parser, json_mode=args.json)

    try:
        workspace = find_recallloom_root(args.path)
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra=strict_sidecar_no_write_failure_extra(continuity_confidence="broken"),
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
                details={"project_root": str(Path(args.path).expanduser().resolve())},
                extra=strict_sidecar_no_write_failure_extra(
                    continuity_confidence="broken",
                    include_recovery_actions=False,
                ),
            ),
        )
    startup_residue_report = None
    if not args.skip_startup_residue_scan:
        startup_residue_report = exit_if_startup_scratch_residue(
            parser,
            json_mode=args.json,
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
        )

    try:
        summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
        context_brief_path = workspace.storage_root / FILE_KEYS["context_brief"]
        state_path = workspace.storage_root / FILE_KEYS["state"]
        update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
        logs_dir = workspace.storage_root / DAILY_LOGS_DIRNAME
        if not summary_path.is_file():
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file: {summary_path}",
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=f"Missing required file: {summary_path}",
                    extra=strict_gate_preflight_failure_extra(workspace),
                ),
            )

        invalid_daily_logs = invalid_iso_like_daily_log_files(logs_dir)
        if invalid_daily_logs:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=(
                    "Refusing preflight because one or more daily log filenames match the date pattern but are invalid ISO dates:\n"
                    + "\n".join(str(path) for path in invalid_daily_logs)
                ),
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=(
                        "Refusing preflight because one or more daily log filenames match the date pattern but are invalid ISO dates:\n"
                        + "\n".join(str(path) for path in invalid_daily_logs)
                    ),
                    extra=strict_gate_preflight_failure_extra(workspace),
                ),
            )

        directory_latest_daily_log = latest_active_daily_log(logs_dir)
        state = load_workspace_state(state_path)
        try:
            state_latest_daily_log_cursor = validate_state_entry_bearing_latest_daily_log(
                storage_root=workspace.storage_root,
                state=state,
            )
        except DailyLogCursorError as exc:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=str(exc),
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=str(exc),
                    details={
                        **exc.details,
                        "project_root": str(workspace.project_root),
                    },
                    extra=strict_gate_preflight_failure_extra(workspace),
                ),
            )
        latest_daily_log = (
            state_latest_daily_log_cursor.latest_path
            if state_latest_daily_log_cursor is not None
            else directory_latest_daily_log
        )
        daily_log_selection_rule = (
            "state_entry_bearing_latest_daily_log"
            if state_latest_daily_log_cursor is not None
            else "latest_active_daily_log"
        )
        summary_state = parse_file_state_marker(read_text(summary_path))
        if summary_state is None:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file-state metadata marker: {summary_path}",
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=f"Missing required file-state metadata marker: {summary_path}",
                    extra=strict_gate_preflight_failure_extra(workspace),
                ),
            )
        context_brief_state = None
        if context_brief_path.is_file():
            context_brief_state = file_state_marker_from_path(context_brief_path)
            if context_brief_state is None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Missing required file-state metadata marker: {context_brief_path}",
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=f"Missing required file-state metadata marker: {context_brief_path}",
                        extra=strict_gate_preflight_failure_extra(workspace),
                    ),
                )
        update_protocol_state = None
        if update_protocol_path.is_file():
            update_protocol_state = file_state_marker_from_path(update_protocol_path)
            if update_protocol_state is None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Missing required file-state metadata marker: {update_protocol_path}",
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=f"Missing required file-state metadata marker: {update_protocol_path}",
                        extra=strict_gate_preflight_failure_extra(workspace),
                    ),
                )

        latest_daily_log_entry = None
        latest_daily_log_entry_count = 0
        latest_daily_log_has_entries = False
        if directory_latest_daily_log is not None:
            try:
                latest_active_daily_log_cursor(workspace.storage_root)
            except DailyLogCursorError as exc:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=str(exc),
                        payload=cli_failure_payload(
                            "malformed_managed_file",
                            error=str(exc),
                            details={
                                **exc.details,
                                "project_root": str(workspace.project_root),
                            },
                            extra=strict_gate_preflight_failure_extra(workspace),
                        ),
                )
        if latest_daily_log is not None:
            (
                latest_daily_log_entry,
                latest_daily_log_entry_count,
                latest_daily_log_is_scaffold,
            ) = latest_daily_log_marker_summary(latest_daily_log)
            if latest_daily_log_entry is None and not latest_daily_log_is_scaffold:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Missing required daily-log-entry metadata marker in the latest ISO-dated daily log: "
                        f"{latest_daily_log}"
                    ),
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=(
                            "Missing required daily-log-entry metadata marker in the latest ISO-dated daily log: "
                            f"{latest_daily_log}"
                        ),
                        extra=strict_gate_preflight_failure_extra(workspace),
                    ),
                )
            latest_daily_log_has_entries = latest_daily_log_entry_count > 0

        workspace_artifact_scan_mode = "full" if args.full else "quick"
        summary_text = read_text(summary_path)
        latest_daily_log_full_text = read_text(latest_daily_log) if latest_daily_log is not None else ""
        latest_daily_log_digest = (
            sha256_text_digest(latest_daily_log_full_text)
            if latest_daily_log is not None
            else None
        )
        latest_daily_log_text = latest_daily_log_full_text if args.full else ""
        freshness = evaluate_continuity_freshness(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            summary_path=summary_path,
            workspace_revision=state["workspace_revision"],
            summary_base_workspace_revision=summary_state.base_workspace_revision,
            latest_daily_log_exists=latest_daily_log_has_entries,
            scan_mode=workspace_artifact_scan_mode,
            state=state,
        )
        digests = continuity_digest_bundle(
            summary_text=summary_text,
            latest_daily_log_text=latest_daily_log_text,
            project_root=workspace.project_root,
        )
        continuity_state, continuity_seeded = continuity_state_for_workspace(
            state=state,
            summary_text=summary_text,
            latest_daily_log_exists=latest_daily_log_has_entries,
        )
        if continuity_state == "initialized_empty_shell":
            digests = {
                "active_task_digest": None,
                "blocked_digest": None,
                "latest_relevant_log_digest": None,
                "suggested_handoff_sections": [],
            }

        latest_active_day = parse_iso_date(latest_daily_log.stem) if latest_daily_log is not None else None
        closure_detected, closure_keywords = (
            detect_closure_signal(latest_daily_log_text)
            if args.full
            else (False, [])
        )
        project_time_policy_cues = (
            detect_update_protocol_time_policy_cues(read_text(update_protocol_path))
            if args.full and update_protocol_path.is_file()
            else []
        )
        next_step_text = extract_section_text(summary_text, "next_step")
        next_step_empty = is_effectively_empty_summary_next_step(next_step_text)
        workday_decision = build_workday_decision(
            now=datetime.now().astimezone(),
            rollover_hour=DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
            latest_active_day=latest_active_day,
            closure_detected=closure_detected,
            summary_next_step_is_empty=next_step_empty,
            preferred_date=None,
            session_intent=None,
            project_time_policy_cues=project_time_policy_cues,
            host_explicit=False,
        )

        latest_workspace_artifact = freshness["latest_workspace_artifact"]
        workspace_artifact_scan_performed = freshness["workspace_artifact_scan_performed"]
        workspace_artifact_is_newer = freshness["workspace_artifact_newer_than_summary"]
        summary_revision_is_stale = freshness["summary_revision_stale"]
        workspace_is_newer = freshness["workspace_newer_than_summary"]
        summary_stale = freshness["summary_stale"]
        continuity_confidence = freshness["continuity_confidence"]
        freshness_risk = freshness_risk_summary(
            workspace_artifact_scan_mode=freshness["workspace_artifact_scan_mode"],
            workspace_artifact_scan_performed=freshness["workspace_artifact_scan_performed"],
            workspace_artifact_newer_than_summary=freshness["workspace_artifact_newer_than_summary"],
            summary_revision_stale=freshness["summary_revision_stale"],
            continuity_confidence=continuity_confidence,
        )
        recommended_actions = recommended_actions_for_preflight(
            continuity_confidence=continuity_confidence,
            continuity_state=continuity_state,
            update_protocol_exists=update_protocol_path.is_file(),
            context_brief_exists=context_brief_path.is_file(),
            latest_daily_log_exists=latest_daily_log_has_entries,
            workspace_is_newer=workspace_is_newer,
        )
        provenance_facts = provenance_facts_from_state(state, review_intent=True)
        trust_state = evaluate_trust_state(
            continuity_confidence=continuity_confidence,
            continuity_state=continuity_state,
            summary_stale=summary_stale,
            workspace_newer_than_summary=workspace_is_newer,
            conflict_state=None,
            legacy_sidecar=provenance_facts["legacy_sidecar"],
            legacy_review_required=provenance_facts["review_required"],
            review_imported_baseline=provenance_facts["review_imported_baseline"],
            helper_evidenced=provenance_facts["helper_evidenced"],
            inconsistent_evidence=provenance_facts["inconsistent_evidence"],
        )
        expected_revisions = expected_revisions_payload(
            workspace_revision=state["workspace_revision"],
            rolling_summary_revision=summary_state.revision if summary_state else None,
            context_brief_revision=context_brief_state.revision if context_brief_state else None,
            update_protocol_revision=(
                update_protocol_state.revision if update_protocol_state else None
            ),
        )
        strict_gate = strict_sidecar_integrity_gate(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
        )
        strict_gate_summary = strict_sidecar_integrity_gate_public_summary(strict_gate)
        strict_gate_allows_write = strict_gate.get("allowed_for_mutation") is True
        strict_gate_receipts_verified = strict_gate_current_receipts_verified(strict_gate)
        provenance_write_context_blocked_by_state = trust_state["provenance_state"] in {
            "review_required",
            "structurally_valid_legacy",
            "inconsistent_or_tampered_evidence",
        }
        provenance_write_context_blocked = (
            provenance_write_context_blocked_by_state or not strict_gate_allows_write
        )
        effective_allowed_operation_level = trust_state["allowed_operation_level"]
        if (
            not strict_gate_allows_write
            and effective_allowed_operation_level == "write_current_state_after_preflight"
        ):
            effective_allowed_operation_level = "read_current_state"
        if provenance_write_context_blocked_by_state:
            write_context_blocked_reason = "provenance_review_required"
        elif not strict_gate_allows_write:
            write_context_blocked_reason = "strict_sidecar_integrity_failed"
        else:
            write_context_blocked_reason = None
        recommended_actions = strict_gate_preflight_actions(
            recommended_actions,
            strict_gate_allows_write=strict_gate_allows_write,
        )
        write_expected_revisions = None if provenance_write_context_blocked else expected_revisions
        provenance = build_provenance_report(
            sidecar_trust_state=trust_state["sidecar_trust_state"],
            continuity_state=continuity_state,
            allowed_operation_level=effective_allowed_operation_level,
            summary_stale=summary_stale,
            expected_revisions=write_expected_revisions,
            receipt_chain_verified=strict_gate_receipts_verified,
            receipt_store_available=strict_gate_receipts_verified,
            legacy_sidecar=provenance_facts["legacy_sidecar"],
            review_required=provenance_facts["review_required"],
            review_imported_baseline=provenance_facts["review_imported_baseline"],
            helper_evidenced_baseline=provenance_facts["helper_evidenced"],
            unproven_sidecar_state=provenance_facts["unproven_sidecar_state"],
            metadata_status=provenance_facts["metadata_status"],
        )
    except (OSError, UnicodeDecodeError, ConfigContractError) as exc:
        message = f"Filesystem/state error: {exc}" if isinstance(exc, ConfigContractError) else f"Filesystem error: {exc}"
        if isinstance(exc, ConfigContractError):
            failure_contract = cli_failure_payload(
                getattr(exc, "failure_reason", None) or "damaged_sidecar",
                error=message,
                extra=strict_gate_preflight_failure_extra(workspace),
            )
        else:
            failure_contract = cli_failure_payload(
                "damaged_sidecar",
                error=message,
                extra=strict_gate_preflight_failure_extra(workspace),
            )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=failure_contract,
        )

    append_date_review_required = workday_decision["recommendation_type"] == "review_date_before_append"
    recommended_write_targets = [summary_path.relative_to(workspace.project_root).as_posix()]
    suggested_read_set = [summary_path.relative_to(workspace.project_root).as_posix()]
    conditional_review_targets = []
    if latest_daily_log is not None:
        suggested_read_set.append(latest_daily_log.relative_to(workspace.project_root).as_posix())
        conditional_review_targets.append(
            {
                "path": latest_daily_log.relative_to(workspace.project_root).as_posix(),
                "reason": (
                    "Review only if this session creates a new milestone entry or end-of-day log. "
                    "Do not treat an existing daily log as a default current-state write target."
                ),
            }
        )
    if context_brief_path.is_file():
        suggested_read_set.append(context_brief_path.relative_to(workspace.project_root).as_posix())
        conditional_review_targets.append(
            {
                "path": context_brief_path.relative_to(workspace.project_root).as_posix(),
                "reason": (
                    "Review if mission, audience, scope, source of truth, workflow, "
                    "boundaries, or current phase changed."
                ),
            }
        )
    override_review_targets = []
    if update_protocol_path.is_file():
        suggested_read_set.append(update_protocol_path.relative_to(workspace.project_root).as_posix())
        override_review_targets.append(
            {
                "path": update_protocol_path.relative_to(workspace.project_root).as_posix(),
                "reason": (
                    "Review project-local continuity rules before applying default cold-start, "
                    "write-target, or archive guidance. v1 helpers do not parse natural-language "
                    "override prose automatically."
                ),
            }
        )

    logical_workday_seen = workday_decision["logical_workday"]
    project_time_policy_review_required = bool(
        project_time_policy_cues
        and (latest_active_day is None or latest_active_day.isoformat() != logical_workday_seen)
    )
    append_daily_log_entry_suggested_date = (
        None
        if append_date_review_required
        else workday_decision["suggested_date"]
    )
    public_project_root = public_project_root_label(workspace.project_root)
    public_storage_root = public_project_path(workspace.storage_root, project_root=workspace.project_root)
    public_latest_daily_log = (
        public_project_path(latest_daily_log, project_root=workspace.project_root)
        if latest_daily_log is not None
        else None
    )
    public_latest_workspace_artifact = (
        public_project_path(latest_workspace_artifact, project_root=workspace.project_root)
        if latest_workspace_artifact is not None
        else None
    )
    payload = {
        "project_root": public_project_root,
        "storage_root": public_storage_root,
        "storage_mode": workspace.storage_mode,
        "workspace_language": workspace.workspace_language,
        "context_brief": (
            public_project_path(context_brief_path, project_root=workspace.project_root)
            if context_brief_path.is_file()
            else None
        ),
        "state": public_project_path(state_path, project_root=workspace.project_root),
        "update_protocol": (
            public_project_path(update_protocol_path, project_root=workspace.project_root)
            if update_protocol_path.is_file()
            else None
        ),
        "workspace_revision": state["workspace_revision"],
        "update_protocol_revision": state["update_protocol_revision"],
        "rolling_summary": public_project_path(summary_path, project_root=workspace.project_root),
        "rolling_summary_revision": summary_state.revision if summary_state else None,
        "context_brief_revision": context_brief_state.revision if context_brief_state else None,
        "update_protocol_file_revision": update_protocol_state.revision if update_protocol_state else None,
        "latest_daily_log": public_latest_daily_log,
        "latest_daily_log_entry_id": latest_daily_log_entry.entry_id if latest_daily_log_entry else None,
        "latest_daily_log_entry_seq": latest_daily_log_entry.entry_seq if latest_daily_log_entry else None,
        "latest_daily_log_entry_count": latest_daily_log_entry_count,
        "daily_log_selection_rule": daily_log_selection_rule,
        "workspace_artifact_scan_mode": workspace_artifact_scan_mode,
        "workspace_artifact_scan_performed": workspace_artifact_scan_performed,
        "latest_workspace_artifact": public_latest_workspace_artifact,
        "workspace_artifact_newer_than_summary": workspace_artifact_is_newer,
        "summary_revision_stale": summary_revision_is_stale,
        "summary_stale": summary_stale,
        "workspace_newer_than_summary": workspace_is_newer,
        "continuity_confidence": continuity_confidence,
        "sidecar_trust_state": trust_state["sidecar_trust_state"],
        "provenance_state": provenance["state_label"],
        "provenance_metadata_status": provenance["metadata_status"],
        "provenance_contract": provenance["contract_identity"],
        "preflight_contract_identity": provenance_contract_identity(),
        "expected_revisions": write_expected_revisions,
        "write_context_authorized": not provenance_write_context_blocked,
        "write_context_blocked_reason": write_context_blocked_reason,
        "write_readiness": provenance["write_readiness"],
        "allowed_operation_level": effective_allowed_operation_level,
        "strict_sidecar_integrity_gate": strict_gate_summary,
        "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
        "freshness_risk_level": freshness_risk["level"],
        "freshness_risk_note": freshness_risk["note"],
        "continuity_state": continuity_state,
        "continuity_seeded": continuity_seeded,
        "active_task_digest": digests["active_task_digest"],
        "blocked_digest": digests["blocked_digest"],
        "latest_relevant_log_digest": digests["latest_relevant_log_digest"],
        "suggested_handoff_sections": digests["suggested_handoff_sections"],
        "recommended_actions": recommended_actions,
        "continuity_snapshot": {
            "project_root": public_project_root,
            "storage_root": public_storage_root,
            "workspace_revision_seen": state["workspace_revision"],
            "rolling_summary_revision_seen": summary_state.revision if summary_state else None,
            "context_brief_revision_seen": context_brief_state.revision if context_brief_state else None,
            "update_protocol_revision_seen": update_protocol_state.revision if update_protocol_state else None,
            "latest_active_daily_log_seen": public_latest_daily_log,
            "latest_active_daily_log_entry_seq_seen": (
                latest_daily_log_entry.entry_seq if latest_daily_log_entry else None
            ),
            "latest_active_daily_log_entry_count_seen": latest_daily_log_entry_count,
            "logical_workday_seen": logical_workday_seen,
            "continuity_confidence": continuity_confidence,
            "continuity_state": continuity_state,
            "continuity_seeded": continuity_seeded,
            "task_type": "preflight_review",
        },
        "suggested_read_set": suggested_read_set,
        "recommended_write_targets": [] if provenance_write_context_blocked else recommended_write_targets,
        "conditional_review_targets": conditional_review_targets,
        "override_review_targets": override_review_targets,
        "write_tier_judgment": build_write_tier_judgment(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
        ),
        "safe_write_context": None if provenance_write_context_blocked else {
            "workspace_revision": state["workspace_revision"],
            "rolling_summary_handoff": {
                "active_task_digest": digests["active_task_digest"],
                "blocked_digest": digests["blocked_digest"],
                "latest_relevant_log_digest": digests["latest_relevant_log_digest"],
                "suggested_handoff_sections": digests["suggested_handoff_sections"],
            },
            "commit_context_file": {
                "rolling_summary": {
                    "path": summary_path.relative_to(workspace.project_root).as_posix(),
                    "expected_file_revision": summary_state.revision if summary_state else None,
                    "expected_workspace_revision": state["workspace_revision"],
                },
                "context_brief": {
                    "path": context_brief_path.relative_to(workspace.project_root).as_posix(),
                    "expected_file_revision": context_brief_state.revision if context_brief_state else None,
                    "expected_workspace_revision": state["workspace_revision"],
                }
                if context_brief_path.is_file()
                else None,
                "update_protocol": {
                    "path": update_protocol_path.relative_to(workspace.project_root).as_posix(),
                    "expected_file_revision": update_protocol_state.revision if update_protocol_state else None,
                    "expected_workspace_revision": state["workspace_revision"],
                }
                if update_protocol_path.is_file()
                else None,
            },
            "append_daily_log_entry": {
                "latest_file": (
                    None
                    if append_date_review_required
                    else (
                        latest_daily_log.relative_to(workspace.storage_root).as_posix()
                        if latest_daily_log is not None
                        else None
                    )
                ),
                "latest_entry_id": latest_daily_log_entry.entry_id if latest_daily_log_entry else None,
                "latest_entry_seq": latest_daily_log_entry.entry_seq if latest_daily_log_entry else None,
                "entry_count": latest_daily_log_entry_count if latest_daily_log is not None else None,
                "latest_file_digest": None if append_date_review_required else latest_daily_log_digest,
                "logical_workday": None if append_date_review_required else logical_workday_seen,
                "suggested_date": append_daily_log_entry_suggested_date,
                "recommendation_type": workday_decision["recommendation_type"],
                "workday_state": workday_decision["workday_state"],
                "heuristic_suggested_date": (
                    None if append_date_review_required else workday_decision["heuristic_suggested_date"]
                ),
                "date_resolution_source": workday_decision["date_resolution_source"],
                "requires_user_confirmation": workday_decision["requires_user_confirmation"],
                "user_visible_prompt_level": workday_decision["user_visible_prompt_level"],
                "project_time_policy_cues": project_time_policy_cues,
                "project_time_policy_review_required": project_time_policy_review_required,
                "closure_detected": closure_detected,
                "closure_keywords": closure_keywords,
                "summary_next_step_empty": next_step_empty,
                "reasoning": workday_decision["reasoning"],
                "expected_workspace_revision": state["workspace_revision"],
            },
        },
    }
    if startup_residue_report is not None:
        payload["startup_residue_report"] = startup_residue_report

    safe_write_context = payload.get("safe_write_context")
    if isinstance(safe_write_context, dict):
        rolling_summary_handoff = safe_write_context["rolling_summary_handoff"]
        safe_write_context["post_append_summary_sync"] = build_post_append_summary_sync_contract(
            workspace=workspace,
            state=state,
            summary_path=summary_path,
            summary_state=summary_state,
            latest_daily_log=latest_daily_log,
            latest_daily_log_entry=latest_daily_log_entry,
            latest_daily_log_entry_count=latest_daily_log_entry_count,
            latest_daily_log_digest=latest_daily_log_digest,
            continuity_seeded=continuity_seeded,
            summary_revision_is_stale=summary_revision_is_stale,
            summary_stale=summary_stale,
            workspace_is_newer=workspace_is_newer,
            allowed_operation_level=effective_allowed_operation_level,
            rolling_summary_handoff=rolling_summary_handoff,
        )

    user_summary = preflight_user_summary(payload)
    payload["user_visible_category"] = user_summary["category"]
    payload["user_summary"] = user_summary

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_user_summary("RecallLoom preflight", user_summary)
        print(f"RecallLoom root: {public_project_root}")
        print(f"Storage root: {public_storage_root}")
        print(f"Storage mode: {workspace.storage_mode}")
        print(f"Workspace language: {workspace.workspace_language}")
        print(f"Rolling summary: {payload['rolling_summary']}")
        if append_date_review_required and latest_daily_log is not None:
            print("Latest active daily log: redacted pending date review")
        else:
            print("Latest active daily log: " f"{public_latest_daily_log if public_latest_daily_log else 'none'}")
        print(
            "Latest workspace artifact: "
            f"{public_latest_workspace_artifact if public_latest_workspace_artifact else 'none'}"
        )
        print(f"Workspace artifact scan mode: {workspace_artifact_scan_mode}")
        print("Summary revision stale: " f"{'yes' if summary_revision_is_stale else 'no'}")
        print(f"Continuity confidence: {continuity_confidence}")
        if freshness_risk["note"]:
            print(f"Freshness risk: {freshness_risk['level']} - {freshness_risk['note']}")
        print(f"Continuity state: {continuity_state}")
        print(f"Workspace newer than summary: {'yes' if workspace_is_newer else 'no'}")
        if recommended_actions:
            print("Recommended actions:")
            for action in recommended_actions:
                print(f"  - {action}")
        print("Recommended write targets:")
        for target in payload["recommended_write_targets"]:
            print(f"  - {target}")
        if conditional_review_targets:
            print("Conditional review targets:")
            for target in conditional_review_targets:
                print(f"  - {target['path']}: {target['reason']}")
        if override_review_targets:
            print("Override review targets:")
            for target in override_review_targets:
                print(f"  - {target['path']}: {target['reason']}")
        if isinstance(payload.get("safe_write_context"), dict):
            print("Safe write context:")
            print(f"  - workspace_revision: {state['workspace_revision']}")
            print(
                "  - use commit_context_file.py for revision-checked writes to "
                "context_brief.md, rolling_summary.md, or update_protocol.md"
            )
            print("  - use append_daily_log_entry.py for revision-checked daily-log milestone entries")
        else:
            reason = payload.get("write_context_blocked_reason") or "provenance_review_required"
            print(f"Safe write context: unavailable ({reason})")

    raise SystemExit(3 if args.fail_on_stale and workspace_is_newer else 0)


if __name__ == "__main__":
    main()
