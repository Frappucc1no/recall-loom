#!/usr/bin/env python3
"""Preview or repair the state.json daily-log cursor."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from core.provenance.evidence import (
    ActualDailyLogCursorEvidence,
    bounded_current_helper_evidence_check,
    capture_actual_daily_log_cursor_evidence,
    current_config_marker_consistency_check,
    current_receipt_required_file_keys,
)
from core.provenance.state import (
    bounded_evidence_supports_helper_evidenced as bounded_helper_evidence_supported,
    cursor_repair_provenance_decision,
    provenance_facts_from_state,
)
from core.provenance.store import RECEIPT_STORE_RELATIVE_PATH
from core.output.confirmation_material import (
    build_confirmation_material,
    print_confirmation_material,
)
from core.workspace.atomic_io import atomic_write_if_unchanged

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    DailyLogCursor,
    DailyLogCursorError,
    EnvironmentContractError,
    FILE_KEYS,
    LockBusyError,
    StorageResolutionError,
    cli_failure_payload,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_with_cli_error,
    find_recallloom_root,
    invalid_iso_like_daily_log_files,
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
PREVIEW_DIGEST_PREFIX = "sha256:"


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
        "--preview-digest",
        help="Optional preview digest from a fresh non-mutating preview.",
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


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_payload(value: object) -> str:
    return PREVIEW_DIGEST_PREFIX + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_preview_identity(
    *,
    current_cursor: dict[str, object],
    expected_cursor: dict[str, object],
    workspace_revision: int | None,
) -> dict[str, object]:
    return {
        "contract_name": "recallloom.daily_log_cursor_repair_preview",
        "contract_version": "0.1",
        "repair_kind": "daily_log_cursor_repair",
        "expected_workspace_revision": workspace_revision,
        "current_cursor": cursor_payload(current_cursor),
        "expected_cursor": cursor_payload(expected_cursor),
    }


def repair_apply_confirmation_material(
    *,
    preview_digest: str,
    expected_workspace_revision: int | None,
) -> dict[str, str | None]:
    revision_text = (
        str(expected_workspace_revision)
        if isinstance(expected_workspace_revision, int)
        else "fresh preview revision"
    )
    return build_confirmation_material(
        confirmation_type="repair_apply",
        action_summary="Apply one bounded daily-log cursor repair from the current preview.",
        why_needed="Repair changes state.json cursor metadata and must be reviewed before mutation.",
        target_layer_or_surface="repair_state",
        expected_side_effect="repair_apply",
        files_or_keys_summary="state.daily_logs cursor fields only",
        safety_gate_summary=(
            "Package support, fresh preview digest, expected workspace revision, write lock, "
            "strict sidecar integrity, and provenance/config evidence gates must pass; rerun "
            "preview or validation after apply."
        ),
        risk_level="medium",
        approval_scope=(
            "Approve only the daily-log cursor repair matching preview "
            f"{preview_digest} at workspace revision {revision_text}; no append, write, "
            "sync, validation, release or advisory action is included."
        ),
        safe_retry_or_rollback=(
            "Decline by stopping without repair; if state changes, rerun preview and use the new digest."
        ),
        debug_reference=preview_digest,
    )


def user_summary_for_preview(*, changed: bool, apply_mode: bool, applied: bool) -> dict[str, str]:
    if apply_mode and applied:
        return {
            "category": "review_required",
            "conclusion": "Repair applied; validation required.",
            "reason": (
                "The cursor repair updated only the previewed state cursor fields, "
                "but it does not authorize a later write."
            ),
            "next_step": "Rerun repair preview or validation before continuing writes.",
        }
    if changed:
        return {
            "category": "needs_confirmation",
            "conclusion": "Repair candidate found.",
            "reason": "The daily-log cursor differs from parsed daily-log marker evidence.",
            "next_step": "Review the confirmation material, then apply only with the fresh preview binding.",
        }
    return {
        "category": "no_write_needed",
        "conclusion": "No repair needed.",
        "reason": "The daily-log cursor already matches parsed marker evidence.",
        "next_step": "Stop; no cursor repair is needed.",
    }


def preview_payload(
    *,
    current_cursor: dict[str, object],
    expected_cursor: dict[str, object],
    workspace_revision: int | None,
    apply_mode: bool,
    applied: bool = False,
    provenance_decision: dict[str, object] | None = None,
) -> dict[str, object]:
    changed = cursor_changed(current_cursor, expected_cursor)
    reason_code = "daily_log_cursor_mismatch" if changed else "daily_log_cursor_already_canonical"
    preview_identity = build_preview_identity(
        current_cursor=current_cursor,
        expected_cursor=expected_cursor,
        workspace_revision=workspace_revision,
    )
    preview_digest = digest_payload(preview_identity)
    user_summary = user_summary_for_preview(
        changed=changed,
        apply_mode=apply_mode,
        applied=applied,
    )
    if apply_mode and applied:
        next_action = "rerun_repair_preview_or_validate"
    elif changed:
        next_action = "review_confirmation_material_then_apply_with_preview_binding"
    else:
        next_action = "none"
    payload = {
        "ok": True,
        "schema_version": "1.1",
        "mode": "apply" if apply_mode else "preview",
        "dry_run": not apply_mode,
        "applied": applied,
        "repair_eligible": changed,
        "repair_kind": "daily_log_cursor_repair",
        "reason_code": reason_code,
        "user_visible_category": user_summary["category"],
        "user_summary": user_summary,
        "expected_workspace_revision": workspace_revision,
        "preview_digest": preview_digest,
        "preview_identity": preview_identity,
        "current_cursor": cursor_payload(current_cursor),
        "expected_cursor": cursor_payload(expected_cursor),
        "next_action": next_action,
        "post_repair_validation_step": "rerun repair-daily-log-cursor preview, then run validate if warnings remain",
    }
    if changed and not applied:
        payload["confirmation_material"] = repair_apply_confirmation_material(
            preview_digest=preview_digest,
            expected_workspace_revision=workspace_revision,
        )
        payload["apply_command_template"] = (
            "recallloom.py repair-daily-log-cursor <project-path> --apply --yes "
            f"--expected-workspace-revision {workspace_revision} "
            f"--preview-digest {preview_digest} --json"
        )
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
    state_text: str,
    daily_log_cursor: dict[str, object] | None = None,
    actual_daily_log_cursor_evidence: ActualDailyLogCursorEvidence | None = None,
) -> dict[str, object]:
    required_file_keys = current_receipt_required_file_keys(
        storage_root=storage_root,
        state=state,
        state_text=state_text,
        daily_log_cursor=daily_log_cursor,
        actual_daily_log_cursor_evidence=actual_daily_log_cursor_evidence,
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


def evaluate_cursor_repair_provenance(
    *,
    project_root: Path,
    storage_root: Path,
    state: dict,
    state_text: str,
    current_cursor: dict[str, object],
    expected_cursor: dict[str, object],
    actual_daily_log_cursor_evidence: ActualDailyLogCursorEvidence | None,
    timestamp: str,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence_check = bounded_current_helper_evidence_check(
        project_root=project_root,
        storage_root=storage_root,
        state=state,
        state_text=state_text,
        helper_evidenced_only=False,
        require_config_guard=True,
        daily_log_cursor=expected_cursor,
        actual_daily_log_cursor_evidence=actual_daily_log_cursor_evidence,
    )
    provenance_facts = provenance_facts_from_state(state, review_intent=True)
    receipt_store_present = (storage_root / RECEIPT_STORE_RELATIVE_PATH).is_file()
    evidence_reason_code = evidence_check.get("reason_code")
    evidence_block_reason_code = evidence_check.get("evidence_block_reason_code")
    if (
        not isinstance(evidence_block_reason_code, str)
        and evidence_reason_code in {"receipt_evidence_absent", "receipt_evidence_incomplete"}
        and (receipt_store_present or provenance_facts["helper_evidenced"])
    ):
        if not receipt_store_present:
            repair_reason_code = "helper_evidenced_receipt_store_missing"
        elif evidence_reason_code == "receipt_evidence_absent":
            repair_reason_code = "receipt_store_present_but_empty"
        else:
            repair_reason_code = "receipt_store_present_but_incomplete"
        evidence_check = {
            **evidence_check,
            "receipt_store_available": receipt_store_present,
            "evidence_block_reason_code": "receipt_evidence_mismatch",
            "reason_code": repair_reason_code,
        }
        evidence_block_reason_code = "receipt_evidence_mismatch"
    missing_active_daily_log_transition = (
        isinstance(current_cursor.get("latest_file"), str)
        and current_cursor.get("latest_file") != ""
        and expected_cursor.get("latest_file") is None
    )
    bounded_evidence_supported = bounded_helper_evidence_supported(
        state,
        bounded_receipt_evidence_verified=evidence_check.get("verified") is True,
        receipt_store_available=evidence_check.get("receipt_store_available") is True,
    ) and not missing_active_daily_log_transition
    decision = cursor_repair_provenance_decision(
        state,
        timestamp=timestamp,
        bounded_evidence_supports_helper_evidenced=bounded_evidence_supported,
        repair_kind="daily_log_cursor_repair",
        evidence_block_reason_code=(
            evidence_block_reason_code
            if isinstance(evidence_block_reason_code, str)
            else None
        ),
    )
    return evidence_check, decision


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
) -> tuple[
    str,
    dict,
    dict[str, object],
    dict[str, object],
    ActualDailyLogCursorEvidence | None,
]:
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
    actual_cursor, actual_daily_log_cursor_evidence = (
        capture_actual_daily_log_cursor_evidence(
            storage_root=storage_root,
            state=state,
            state_text=state_text,
        )
    )
    expected = cursor_from_calculation(actual_cursor)
    current = cursor_from_state(state)
    return state_text, state, current, expected, actual_daily_log_cursor_evidence


def print_payload(payload: dict[str, object], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    user_summary = payload.get("user_summary")
    if isinstance(user_summary, dict):
        print("RecallLoom cursor repair")
        print(f"{user_summary.get('category')}: {user_summary.get('conclusion')}")
        if user_summary.get("reason"):
            print(f"Reason: {user_summary.get('reason')}")
        print(f"Next step: {user_summary.get('next_step')}")
        confirmation_material = payload.get("confirmation_material")
        if isinstance(confirmation_material, dict):
            print_confirmation_material(confirmation_material)
        return
    print(f"Mode: {payload.get('mode')}")
    print(f"Reason: {payload.get('reason_code')}")
    print(f"Repair eligible: {payload.get('repair_eligible')}")
    print(f"Applied: {payload.get('applied')}")
    print(f"Preview digest: {payload.get('preview_digest')}")
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
    if args.preview_digest is not None and not (
        isinstance(args.preview_digest, str)
        and args.preview_digest.startswith(PREVIEW_DIGEST_PREFIX)
        and len(args.preview_digest) == len(PREVIEW_DIGEST_PREFIX) + 64
    ):
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="invalid_prepared_input",
            message="--preview-digest must be a sha256 digest from a fresh repair preview.",
            details={
                "reason_code": "invalid_preview_digest",
                "side_effect": "none",
            },
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
    if args.apply and args.expected_workspace_revision is None and args.preview_digest is None:
        exit_with_redacted_failure(
            parser,
            json_mode=args.json,
            exit_code=2,
            reason="invalid_prepared_input",
            message=(
                "--apply requires a fresh preview binding: provide "
                "--expected-workspace-revision or --preview-digest."
            ),
            details={
                "reason_code": "repair_apply_requires_preview_binding",
                "side_effect": "none",
                "next_actions": ["rerun_repair_preview", "retry_with_preview_binding"],
            },
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
            (
                state_text,
                state,
                current,
                expected,
                actual_daily_log_cursor_evidence,
            ) = load_repair_view(
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

        provenance_decision = None
        if cursor_changed(current, expected):
            evidence_check, provenance_decision = evaluate_cursor_repair_provenance(
                project_root=workspace.project_root,
                storage_root=workspace.storage_root,
                state=state,
                state_text=state_text,
                current_cursor=current,
                expected_cursor=expected,
                actual_daily_log_cursor_evidence=actual_daily_log_cursor_evidence,
                timestamp=now_iso_timestamp(),
            )
            if not provenance_decision.get("allowed"):
                exit_with_provenance_refusal(
                    parser,
                    json_mode=args.json,
                    decision=provenance_decision,
                    evidence_check=evidence_check,
                )
        payload = preview_payload(
            current_cursor=current,
            expected_cursor=expected,
            workspace_revision=state.get("workspace_revision")
            if isinstance(state.get("workspace_revision"), int)
            else None,
            apply_mode=False,
            provenance_decision=provenance_decision,
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
            (
                state_text,
                state,
                current,
                expected,
                actual_daily_log_cursor_evidence,
            ) = load_repair_view(
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
            workspace_revision = (
                state.get("workspace_revision")
                if isinstance(state.get("workspace_revision"), int)
                else None
            )
            current_preview_identity = build_preview_identity(
                current_cursor=current,
                expected_cursor=expected,
                workspace_revision=workspace_revision,
            )
            current_preview_digest = digest_payload(current_preview_identity)
            if args.preview_digest is not None and args.preview_digest != current_preview_digest:
                exit_with_redacted_failure(
                    parser,
                    json_mode=args.json,
                    exit_code=3,
                    reason="stale_write_context",
                    message=(
                        "Repair preview digest no longer matches current cursor evidence; "
                        "rerun preview before applying repair."
                    ),
                    details={
                        "reason_code": "repair_preview_digest_mismatch",
                        "side_effect": "none",
                        "provided_preview_digest": args.preview_digest,
                        "current_preview_digest": current_preview_digest,
                        "next_actions": ["rerun_repair_preview", "retry_with_fresh_preview_binding"],
                    },
                )
            if not cursor_changed(current, expected):
                evidence_check = config_marker_guard_check(
                    storage_root=workspace.storage_root,
                    state=state,
                    state_text=state_text,
                    daily_log_cursor=expected,
                    actual_daily_log_cursor_evidence=actual_daily_log_cursor_evidence,
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
                    workspace_revision=workspace_revision,
                    apply_mode=True,
                    applied=False,
                )
            else:
                repair_timestamp = now_iso_timestamp()
                evidence_check, provenance_decision = evaluate_cursor_repair_provenance(
                    project_root=workspace.project_root,
                    storage_root=workspace.storage_root,
                    state=state,
                    state_text=state_text,
                    timestamp=repair_timestamp,
                    current_cursor=current,
                    expected_cursor=expected,
                    actual_daily_log_cursor_evidence=actual_daily_log_cursor_evidence,
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
                    workspace_revision=workspace_revision,
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
