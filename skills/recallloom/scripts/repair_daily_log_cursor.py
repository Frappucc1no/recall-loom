#!/usr/bin/env python3
"""Preview or repair the state.json daily-log cursor."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from core.provenance.evidence import (
    bounded_current_helper_evidence_check,
    current_config_marker_consistency_check,
    current_receipt_required_file_keys,
)
from core.provenance.state import (
    bounded_evidence_supports_helper_evidenced as bounded_helper_evidence_supported,
    cursor_repair_provenance_decision,
)

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    DailyLogCursor,
    DailyLogCursorError,
    EnvironmentContractError,
    FILE_KEYS,
    LockBusyError,
    StorageResolutionError,
    atomic_write_if_unchanged,
    cli_failure_payload,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_with_cli_error,
    find_recallloom_root,
    invalid_iso_like_daily_log_files,
    latest_active_daily_log_cursor,
    load_workspace_state,
    now_iso_timestamp,
    public_json_payload,
    read_text,
    workspace_write_lock,
)


CURSOR_KEYS = (
    "latest_file",
    "latest_entry_id",
    "latest_entry_seq",
    "entry_count",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or repair state.json daily_logs cursor fields."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the repair. Requires --yes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the explicit --apply repair.",
    )
    parser.add_argument(
        "--expected-workspace-revision",
        type=int,
        help="Optional revision guard for apply mode.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )
    return parser


def cursor_from_state(state: dict) -> dict[str, object]:
    daily_logs = state.get("daily_logs")
    if not isinstance(daily_logs, dict):
        return {
            "latest_file": None,
            "latest_entry_id": None,
            "latest_entry_seq": None,
            "entry_count": None,
        }
    return {key: daily_logs.get(key) for key in CURSOR_KEYS}


def cursor_from_calculation(cursor: DailyLogCursor) -> dict[str, object]:
    return cursor.as_state_fields()


def cursor_changed(current: dict[str, object], expected: dict[str, object]) -> bool:
    return any(current.get(key) != expected.get(key) for key in CURSOR_KEYS)


def cursor_payload(cursor: dict[str, object]) -> dict[str, object]:
    return {key: cursor.get(key) for key in CURSOR_KEYS}


def preview_payload(
    *,
    current_cursor: dict[str, object],
    expected_cursor: dict[str, object],
    apply_mode: bool,
    applied: bool = False,
    provenance_decision: dict[str, object] | None = None,
) -> dict[str, object]:
    changed = cursor_changed(current_cursor, expected_cursor)
    reason_code = "daily_log_cursor_mismatch" if changed else "daily_log_cursor_already_canonical"
    if apply_mode and applied:
        next_action = "rerun_append_or_sync_preflight"
    elif changed:
        next_action = "rerun_with_apply_and_yes"
    else:
        next_action = "none"
    payload = {
        "ok": True,
        "mode": "apply" if apply_mode else "preview",
        "dry_run": not apply_mode,
        "applied": applied,
        "repair_eligible": changed,
        "reason_code": reason_code,
        "current_cursor": cursor_payload(current_cursor),
        "expected_cursor": cursor_payload(expected_cursor),
        "next_action": next_action,
    }
    if provenance_decision is not None:
        payload["provenance_decision"] = public_provenance_decision_summary(
            provenance_decision
        )
    return payload


def public_provenance_decision_summary(decision: dict[str, object]) -> dict[str, object]:
    return {
        "allowed": decision.get("allowed") is True,
        "repair_kind": decision.get("repair_kind"),
        "route": decision.get("route"),
        "result_state_label": decision.get("result_state_label"),
        "blocked_reason_code": decision.get("blocked_reason_code"),
        "trust_effect": decision.get("trust_effect"),
        "receipt_backed": decision.get("receipt_backed") is True,
        "receipt_store_write_allowed": decision.get("receipt_store_write_allowed") is True,
        "finalizes_mutating_receipt": decision.get("finalizes_mutating_receipt") is True,
        "bounded_evidence_supports_helper_evidenced": (
            decision.get("bounded_evidence_supports_helper_evidenced") is True
        ),
        "metadata_status": decision.get("metadata_status"),
    }


def public_bounded_evidence_check_summary(check: dict[str, object]) -> dict[str, object]:
    return {
        "required": check.get("required") is True,
        "verified": check.get("verified") is True,
        "receipt_store_available": check.get("receipt_store_available") is True,
        "evidence_block_reason_code": check.get("evidence_block_reason_code"),
        "reason_code": check.get("reason_code"),
        "required_current_file_keys": check.get("required_current_file_keys", []),
        "verified_current_file_keys": check.get("verified_current_file_keys", []),
    }


def config_marker_guard_check(
    *,
    storage_root: Path,
    state: dict,
    daily_log_cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    required_file_keys = current_receipt_required_file_keys(
        storage_root=storage_root,
        state=state,
        daily_log_cursor=daily_log_cursor,
    )
    config_guard = current_config_marker_consistency_check(
        storage_root=storage_root,
        state=state,
        required_file_keys=required_file_keys,
        daily_log_cursor=daily_log_cursor,
    )
    if config_guard.get("verified") is True:
        return {
            "required": True,
            "verified": True,
            "receipt_store_available": False,
            "evidence_block_reason_code": None,
            "reason_code": "config_marker_consistency_verified",
            "required_current_file_keys": required_file_keys,
            "verified_current_file_keys": required_file_keys,
            "missing_current_file_keys": [],
            "config_guard": config_guard,
        }
    return {
        "required": True,
        "verified": False,
        "receipt_store_available": False,
        "evidence_block_reason_code": "direct_state_or_config_edit_detected",
        "reason_code": str(
            config_guard.get("reason_code") or "config_marker_consistency_mismatch"
        ),
        "required_current_file_keys": required_file_keys,
        "verified_current_file_keys": [],
        "missing_current_file_keys": [],
        "config_guard": config_guard,
    }


def cursor_error_failure_payload(
    exc: DailyLogCursorError,
    *,
    mode: str,
) -> dict[str, object]:
    details = getattr(exc, "details", {})
    latest_file = details.get("latest_file") if isinstance(details, dict) else None
    next_action = "repair_daily_log_markers_before_cursor_repair"
    return cli_failure_payload(
        "malformed_managed_file",
        error=(
            "Daily-log cursor repair refused because latest daily-log marker "
            "evidence is malformed."
        ),
        details={
            "side_effect": "none",
            "operation": "repair_daily_log_cursor",
            "reason_code": exc.reason_code,
            "latest_file": latest_file if isinstance(latest_file, str) else None,
            "next_action": next_action,
        },
        extra={
            "mode": mode,
            "dry_run": mode != "apply",
            "applied": False,
            "repair_eligible": False,
            "reason_code": exc.reason_code,
            "latest_file": latest_file if isinstance(latest_file, str) else None,
            "next_action": next_action,
        },
    )


def exit_with_redacted_failure(
    parser: argparse.ArgumentParser,
    *,
    json_mode: bool,
    exit_code: int,
    reason: str,
    message: str,
    details: dict[str, object] | None = None,
) -> None:
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        payload=cli_failure_payload(
            reason,
            error=message,
            details={
                "side_effect": "none",
                "operation": "repair_daily_log_cursor",
                **(details or {}),
            },
        ),
    )


def exit_with_provenance_refusal(
    parser: argparse.ArgumentParser,
    *,
    json_mode: bool,
    decision: dict[str, object],
    evidence_check: dict[str, object] | None = None,
) -> None:
    reason = decision.get("blocked_reason_code")
    reason_code = reason if isinstance(reason, str) and reason else "cursor_repair_blocked"
    exit_with_redacted_failure(
        parser,
        json_mode=json_mode,
        exit_code=3,
        reason="trust_review_required",
        message="Refusing cursor repair because provenance decision did not allow state repair.",
        details={
            "side_effect": "none",
            "reason_code": reason_code,
            "provenance_decision": public_provenance_decision_summary(decision),
            "bounded_evidence_check": (
                public_bounded_evidence_check_summary(evidence_check)
                if evidence_check is not None
                else None
            ),
            "next_actions": ["run_validate", "review_repair_import_before_retry"],
        },
    )


def load_repair_view(
    *,
    state_path: Path,
    storage_root: Path,
) -> tuple[str, dict, dict[str, object], dict[str, object]]:
    state_text = read_text(state_path)
    state = load_workspace_state(state_path)
    invalid_daily_logs = invalid_iso_like_daily_log_files(storage_root / DAILY_LOGS_DIRNAME)
    if invalid_daily_logs:
        first = invalid_daily_logs[0].relative_to(storage_root).as_posix()
        raise DailyLogCursorError(
            reason_code="malformed_daily_log_filename",
            message="Refusing cursor repair because an active daily-log filename is not a valid ISO date.",
            details={"latest_file": first},
        )
    expected = cursor_from_calculation(latest_active_daily_log_cursor(storage_root))
    current = cursor_from_state(state)
    return state_text, state, current, expected


def print_payload(payload: dict[str, object], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"Mode: {payload.get('mode')}")
    print(f"Reason: {payload.get('reason_code')}")
    print(f"Repair eligible: {payload.get('repair_eligible')}")
    print(f"Applied: {payload.get('applied')}")
    print(f"Current cursor: {json.dumps(payload.get('current_cursor'), ensure_ascii=False)}")
    print(f"Expected cursor: {json.dumps(payload.get('expected_cursor'), ensure_ascii=False)}")
    print(f"Next action: {payload.get('next_action')}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="python_runtime_unavailable",
            message=str(exc),
        )

    if args.expected_workspace_revision is not None and args.expected_workspace_revision < 1:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="invalid_prepared_input",
            message="--expected-workspace-revision must be a positive integer.",
        )
    if args.yes and not args.apply:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="invalid_prepared_input",
            message="--yes is only valid with --apply.",
        )
    if args.apply and not args.yes:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="invalid_prepared_input",
            message="--apply requires --yes before repair can write state.json.",
        )

    enforce_package_support_gate(
        parser,
        json_mode=args.json,
        action_name="repair_daily_log_cursor.py",
        action_level="mutating" if args.apply else "readonly",
    )

    try:
        workspace = find_recallloom_root(args.path)
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason=getattr(exc, "failure_reason", None) or "damaged_sidecar",
            message="RecallLoom storage could not be resolved safely.",
        )

    if workspace is None:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=1,
            reason="no_project_root",
            message="No RecallLoom project root found.",
        )

    state_path = workspace.storage_root / FILE_KEYS["state"]

    if not args.apply:
        try:
            _state_text, state, current, expected = load_repair_view(
                state_path=state_path,
                storage_root=workspace.storage_root,
            )
        except DailyLogCursorError as exc:
            payload = cursor_error_failure_payload(exc, mode="preview")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                raise SystemExit(2)
            print(f"Reason: {payload['reason_code']}")
            print(f"Repair eligible: {payload['repair_eligible']}")
            print(f"Next action: {payload['next_action']}")
            raise SystemExit(2)
        except ConfigContractError:
            exit_with_redacted_failure(
                parser,
                json_mode=args.json,
                exit_code=2,
                reason="damaged_sidecar",
                message="state.json is not structurally valid enough for cursor repair.",
            )
        except (OSError, UnicodeDecodeError):
            exit_with_redacted_failure(
                parser,
                json_mode=args.json,
                exit_code=2,
                reason="damaged_sidecar",
                message="RecallLoom managed state could not be read.",
            )

        payload = preview_payload(
            current_cursor=current,
            expected_cursor=expected,
            apply_mode=False,
        )
        if args.json:
            print(
                json.dumps(
                    public_json_payload(payload, project_root=workspace.project_root),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print_payload(payload, json_mode=False)
        return

    try:
        with workspace_write_lock(workspace.project_root, "repair_daily_log_cursor.py"):
            state_text, state, current, expected = load_repair_view(
                state_path=state_path,
                storage_root=workspace.storage_root,
            )
            if (
                args.expected_workspace_revision is not None
                and state.get("workspace_revision") != args.expected_workspace_revision
            ):
                exit_with_redacted_failure(
                    parser,
                    json_mode=args.json,
                    exit_code=3,
                    reason="stale_write_context",
                    message="Workspace revision changed before cursor repair; rerun preview.",
                )
            if not cursor_changed(current, expected):
                evidence_check = config_marker_guard_check(
                    storage_root=workspace.storage_root,
                    state=state,
                    daily_log_cursor=expected,
                )
                evidence_block_reason_code = evidence_check.get("evidence_block_reason_code")
                if isinstance(evidence_block_reason_code, str):
                    provenance_decision = cursor_repair_provenance_decision(
                        state,
                        timestamp=now_iso_timestamp(),
                        bounded_evidence_supports_helper_evidenced=False,
                        repair_kind="daily_log_cursor_repair",
                        evidence_block_reason_code=evidence_block_reason_code,
                    )
                    exit_with_provenance_refusal(
                        parser,
                        json_mode=args.json,
                        decision=provenance_decision,
                        evidence_check=evidence_check,
                    )
                payload = preview_payload(
                    current_cursor=current,
                    expected_cursor=expected,
                    apply_mode=True,
                    applied=False,
                )
            else:
                repair_timestamp = now_iso_timestamp()
                evidence_check = bounded_current_helper_evidence_check(
                    project_root=workspace.project_root,
                    storage_root=workspace.storage_root,
                    state=state,
                    state_text=state_text,
                    require_config_guard=True,
                    daily_log_cursor=expected,
                )
                missing_active_daily_log_transition = (
                    isinstance(current.get("latest_file"), str)
                    and current.get("latest_file") != ""
                    and expected.get("latest_file") is None
                )
                bounded_evidence_supported = bounded_helper_evidence_supported(
                    state,
                    bounded_receipt_evidence_verified=evidence_check.get("verified") is True,
                    receipt_store_available=(
                        evidence_check.get("receipt_store_available") is True
                    ),
                ) and not missing_active_daily_log_transition
                evidence_block_reason_code = evidence_check.get("evidence_block_reason_code")
                provenance_decision = cursor_repair_provenance_decision(
                    state,
                    timestamp=repair_timestamp,
                    bounded_evidence_supports_helper_evidenced=bounded_evidence_supported,
                    repair_kind="daily_log_cursor_repair",
                    evidence_block_reason_code=(
                        evidence_block_reason_code
                        if isinstance(evidence_block_reason_code, str)
                        else None
                    ),
                )
                if not provenance_decision.get("allowed"):
                    exit_with_provenance_refusal(
                        parser,
                        json_mode=args.json,
                        decision=provenance_decision,
                        evidence_check=evidence_check,
                    )

                next_state = copy.deepcopy(state)
                next_state["workspace_revision"] = state["workspace_revision"] + 1
                next_state["provenance"] = provenance_decision["provenance_metadata"]
                daily_logs = next_state.setdefault("daily_logs", {})
                for key, value in expected.items():
                    daily_logs[key] = value
                daily_logs["updated_at"] = repair_timestamp
                next_state_text = json.dumps(next_state, ensure_ascii=False, indent=2) + "\n"
                atomic_write_if_unchanged(
                    state_path,
                    expected_text=state_text,
                    new_text=next_state_text,
                )
                payload = preview_payload(
                    current_cursor=current,
                    expected_cursor=expected,
                    apply_mode=True,
                    applied=True,
                    provenance_decision=provenance_decision,
                )
    except DailyLogCursorError as exc:
        payload = cursor_error_failure_payload(exc, mode="apply")
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            raise SystemExit(2)
        print(f"Reason: {payload['reason_code']}")
        print(f"Repair eligible: {payload['repair_eligible']}")
        print(f"Next action: {payload['next_action']}")
        raise SystemExit(2)
    except LockBusyError:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=3,
            reason="write_lock_busy",
            message="RecallLoom write lock is busy.",
        )
    except ConfigContractError:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="damaged_sidecar",
            message="state.json is not structurally valid enough for cursor repair.",
        )
    except (OSError, UnicodeDecodeError):
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="damaged_sidecar",
            message="RecallLoom managed state could not be read or written.",
        )

    if args.json:
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_payload(payload, json_mode=False)


if __name__ == "__main__":
    main()
