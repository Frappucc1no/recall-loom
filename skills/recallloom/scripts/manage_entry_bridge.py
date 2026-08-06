#!/usr/bin/env python3
"""Manage thin bridges from root entry files to RecallLoom continuity files."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from core.bridge.blocks import (
    bridge_block_integrity,
    detect_root_entry_files,
    remove_bridge_block,
    render_bridge_block,
    replace_or_insert_bridge,
)
from core.protocol.contracts import FILE_KEYS, ROOT_ENTRY_CANDIDATES
from core.provenance.state import helper_write_gate_from_state
from core.safety.attached_text import scan_auto_attached_context_text
from core.workspace.atomic_io import atomic_write_if_unchanged

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    EnvironmentContractError,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    enforce_package_support_gate,
    exit_with_cli_error,
    LockBusyError,
    load_workspace_state,
    dump_json,
    exit_if_startup_scratch_residue,
    latest_active_daily_log,
    managed_file_contract_issue,
    now_iso_timestamp,
    public_json_payload,
    restore_text_snapshot,
    StorageResolutionError,
    ensure_supported_python_version,
    find_recallloom_root,
    read_text,
    workspace_write_lock,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview adding, updating, or removing RecallLoom thin bridges in root entry "
            "files. Bridge apply is currently unsupported and blocked."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help=(
            "Specific project-root-relative entry file to bridge. "
            "Only officially supported root entry files are allowed. "
            "v1 accepts at most one target per invocation."
        ),
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Preview removal of managed bridge blocks instead of an add/update preview.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Legacy bridge-apply confirmation flag. It is currently unsupported and blocked; "
            "the script remains preview-only."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )
    return parser


def enforce_provenance_write_gate(parser, *, json_mode: bool, state: dict) -> None:
    gate = helper_write_gate_from_state(
        state,
        helper_name="manage_entry_bridge.py",
        operation_class="entry_bridge_update",
        require_preflight_for_review_imported_baseline=True,
    )
    if gate["allowed"]:
        return
    message = (
        "Refusing to update bridge files because bridge apply is outside the v0.4.2 "
        "dispatcher-issued receipt-backed mutating surface. Use preview mode only until "
        "bridge receipt support is added."
    )
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=3,
        message=message,
        payload=cli_failure_payload(
            "unsupported_mutating_surface",
            error=message,
            details={
                "reason_code": "bridge_apply_not_receipt_backed",
                "helper_name": gate["helper_name"],
                "operation_class": gate["operation_class"],
                "provenance_state": gate["provenance_state"],
                "provenance_metadata_status": gate["provenance_metadata_status"],
                "write_readiness": gate["write_readiness"],
                "side_effect": "none",
                "next_actions": ["run_bridge_preview", "wait_for_bridge_receipt_support"],
            },
        ),
    )


def resolve_targets(project_root: Path, explicit_files: list[str]) -> list[Path]:
    if explicit_files:
        targets: list[Path] = []
        allowed = {str(path.as_posix()) for path in ROOT_ENTRY_CANDIDATES}
        for rel in explicit_files:
            normalized_rel = Path(rel).as_posix()
            if normalized_rel not in allowed:
                raise ValueError(
                    "Refusing to bridge a non-supported file. Allowed targets are: "
                    + ", ".join(sorted(allowed))
                )
            candidate = (project_root / normalized_rel).resolve()
            try:
                candidate.relative_to(project_root)
            except ValueError:
                raise ValueError(f"Refusing to modify a file outside the project root: {candidate}")
            targets.append(candidate)
        return targets
    return detect_root_entry_files(project_root)


def bridge_integrity_message(reason: str | None, target: Path) -> str:
    detail_map = {
        "bridge_start_end_mismatch": "bridge start/end markers are mismatched",
        "bridge_duplicate_blocks": "multiple managed bridge blocks are present",
        "bridge_order_invalid": "bridge start/end markers are out of order",
    }
    detail = detail_map.get(reason, "the managed bridge block is malformed")
    return (
        f"Refusing to modify {target} because {detail}. "
        "Use validate_context.py and the recovery proposal/review/promotion helpers before retrying."
    )


def missing_continuity_files(workspace) -> list[Path]:
    issues: list[Path] = []
    required = [
        workspace.storage_root / "config.json",
    ]
    for path in required:
        if not path.is_file():
            issues.append(path)
    contract_paths = [
        (workspace.storage_root / FILE_KEYS["context_brief"], "context_brief"),
        (workspace.storage_root / FILE_KEYS["rolling_summary"], "rolling_summary"),
    ]
    update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
    if update_protocol_path.exists():
        contract_paths.append((update_protocol_path, "update_protocol"))
    for path, file_key in contract_paths:
        issue = managed_file_contract_issue(
            path,
            file_key=file_key,
            workspace_language=workspace.workspace_language,
            expected_protocol_version=workspace.protocol_version,
        )
        if issue is not None:
            issues.append(path)
    if not (workspace.storage_root / DAILY_LOGS_DIRNAME).is_dir():
        issues.append(workspace.storage_root / DAILY_LOGS_DIRNAME)
    return issues


def bridge_state_matches_observed_state(
    current_state: dict | None,
    *,
    update_protocol_revision_seen: int,
    latest_daily_log_seen: str | None,
) -> bool:
    if not isinstance(current_state, dict):
        return False
    return (
        current_state.get("update_protocol_revision_seen") == update_protocol_revision_seen
        and current_state.get("latest_daily_log_seen") == latest_daily_log_seen
    )


def refreshed_bridge_updated_at(previous_updated_at: str | None) -> str:
    updated_at = now_iso_timestamp()
    if updated_at != previous_updated_at:
        return updated_at
    return datetime.now().astimezone().isoformat(timespec="microseconds")


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
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )
    if workspace is None:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload("no_project_root", error="No RecallLoom project root found."),
        )

    exit_if_startup_scratch_residue(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )

    try:
        targets = resolve_targets(workspace.project_root, args.file)
    except ValueError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload("invalid_prepared_input", error=str(exc)),
        )
    if not targets:
        message = "No eligible entry files found. Use --file to specify a target."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message=message,
            payload=cli_failure_payload("invalid_prepared_input", error=message),
        )

    if len(targets) > 1:
        message = (
            "v1 bridge operations accept exactly one target per invocation. "
            "Re-run with a single explicit --file argument."
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("invalid_prepared_input", error=message),
        )

    try:
        with workspace_write_lock(workspace.project_root, "manage_entry_bridge.py"):
            state_path = workspace.storage_root / FILE_KEYS["state"]
            try:
                state = load_workspace_state(state_path)
            except ConfigContractError as exc:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=str(exc),
                    payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
                )
            if args.yes:
                enforce_provenance_write_gate(parser, json_mode=args.json, state=state)

            results = []
            for target in targets:
                if not target.is_file():
                    message = f"Target entry file does not exist: {target}"
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=message,
                        payload=cli_failure_payload("invalid_prepared_input", error=message),
                    )

                current_text = read_text(target)
                ok, reason = bridge_block_integrity(current_text)
                if not ok:
                    message = bridge_integrity_message(reason, target)
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=3,
                        message=message,
                        payload=cli_failure_payload("malformed_managed_file", error=message),
                    )

                missing = missing_continuity_files(workspace)
                if missing:
                    message = (
                        "Refusing to modify entry files because required continuity files are missing:\n"
                        + "\n".join(str(path) for path in missing)
                    )
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=message,
                        payload=cli_failure_payload(
                            "malformed_managed_file",
                            error=message,
                            details={"missing_paths": [str(path) for path in missing]},
                        ),
                    )

                if args.remove:
                    updated_text, changed = remove_bridge_block(current_text)
                    scan_result = None
                else:
                    block = render_bridge_block(workspace, target)
                    updated_text = replace_or_insert_bridge(current_text, block)
                    scan_result = scan_auto_attached_context_text(updated_text)
                    scan_result = {**scan_result, "scope": "full_file"}
                    if scan_result["blocked"]:
                        message = (
                            "Refusing to attach continuity text because the updated entry file "
                            "failed the attached-text safety scan: "
                            + ", ".join(scan_result["hard_block_reasons"])
                        )
                        exit_with_cli_error(
                            parser,
                            json_mode=args.json,
                            exit_code=2,
                            message=message,
                            payload=cli_failure_payload(
                                "attach_scan_blocked",
                                error=message,
                                details={
                                    "hard_block_reasons": scan_result["hard_block_reasons"],
                                    "scope": scan_result["scope"],
                                    "target": str(target),
                                },
                            ),
                        )
                    changed = updated_text != current_text

                rel_target = target.relative_to(workspace.project_root).as_posix()
                state_changed = False
                if args.remove:
                    if rel_target in state["bridged_entries"]:
                        state["bridged_entries"].pop(rel_target, None)
                        state_changed = True
                else:
                    latest_daily_log = latest_active_daily_log(workspace.storage_root / DAILY_LOGS_DIRNAME)
                    latest_daily_log_seen = (
                        latest_daily_log.relative_to(workspace.storage_root).as_posix()
                        if latest_daily_log is not None
                        else None
                    )
                    current_bridge_state = state["bridged_entries"].get(rel_target)
                    current_updated_at = (
                        current_bridge_state.get("updated_at")
                        if isinstance(current_bridge_state, dict)
                        else None
                    )
                    should_refresh_bridge_state = (
                        changed
                        or not bridge_state_matches_observed_state(
                            current_bridge_state,
                            update_protocol_revision_seen=state["update_protocol_revision"],
                            latest_daily_log_seen=latest_daily_log_seen,
                        )
                        or not isinstance(current_updated_at, str)
                        or not current_updated_at.strip()
                    )
                    next_bridge_state = None
                    if should_refresh_bridge_state:
                        next_bridge_state = {
                            "update_protocol_revision_seen": state["update_protocol_revision"],
                            "latest_daily_log_seen": latest_daily_log_seen,
                            "updated_at": refreshed_bridge_updated_at(current_updated_at),
                        }
                    if next_bridge_state is not None and current_bridge_state != next_bridge_state:
                        state["bridged_entries"][rel_target] = next_bridge_state
                        state_changed = True

                if args.yes and (changed or state_changed):
                    if changed:
                        try:
                            atomic_write_if_unchanged(target, expected_text=current_text, new_text=updated_text)
                        except OSError as exc:
                            message = f"Filesystem error while writing {target}: {exc}"
                            exit_with_cli_error(
                                parser,
                                json_mode=args.json,
                                exit_code=2,
                                message=message,
                                payload=cli_failure_payload("damaged_sidecar", error=message),
                            )
                    if state_changed:
                        try:
                            dump_json(state_path, state)
                        except OSError as exc:
                            if changed:
                                try:
                                    restore_text_snapshot(target, existed=True, text=current_text)
                                except OSError as rollback_exc:
                                    message = (
                                        f"Failed to update state after writing {target}: {exc}. "
                                        f"Rollback also failed: {rollback_exc}. Workspace may be partially updated."
                                    )
                                    exit_with_cli_error(
                                        parser,
                                        json_mode=args.json,
                                        exit_code=2,
                                        message=message,
                                        payload=cli_failure_payload("damaged_sidecar", error=message),
                                    )
                                message = (
                                    f"Failed to update state after writing {target}: {exc}. "
                                    "The target file was restored to its previous content."
                                )
                                exit_with_cli_error(
                                    parser,
                                    json_mode=args.json,
                                    exit_code=2,
                                    message=message,
                                    payload=cli_failure_payload("damaged_sidecar", error=message),
                                )
                            message = (
                                f"Failed to update bridge state for {target}: {exc}. "
                                "No file content was changed."
                            )
                            exit_with_cli_error(
                                parser,
                                json_mode=args.json,
                                exit_code=2,
                                message=message,
                                payload=cli_failure_payload("damaged_sidecar", error=message),
                            )

                results.append(
                    {
                        "target": str(target),
                        "action": "remove" if args.remove else "apply",
                        "changed": changed,
                        "state_changed": state_changed,
                        "applied": args.yes and (changed or state_changed),
                        "attach_scan": scan_result,
                    }
                )
    except LockBusyError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=str(exc),
            payload=cli_failure_payload("write_lock_busy", error=str(exc)),
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Filesystem error: {exc}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("damaged_sidecar", error=message),
        )

    payload = {
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "storage_mode": workspace.storage_mode,
        "workspace_language": workspace.workspace_language,
        "dry_run": not args.yes,
        "results": results,
    }

    if args.json:
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for result in results:
            if not args.yes:
                if result["changed"]:
                    state = "would change"
                elif result["state_changed"]:
                    state = "would update state"
                else:
                    state = "no change"
            else:
                if result["changed"]:
                    state = "changed"
                elif result["state_changed"]:
                    state = "updated state"
                else:
                    state = "no change"
            print(f"{result['target']}: {state}")


if __name__ == "__main__":
    main()
