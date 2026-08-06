#!/usr/bin/env python3
"""Safely append a milestone entry to a RecallLoom daily log."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path

from core.continuity.workday import (
    DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
    logical_workday_for,
)
from core.failure.context import (
    COMMAND_APPEND,
    OPERATION_DAILY_LOG_APPEND,
    STAGE_PREFLIGHT,
    OperationContext,
)
from core.failure.contracts import failure_payload
from core.output.privacy import publicize_json_value
from core.protocol.contracts import FILE_KEYS
from core.provenance.bindings import (
    PreflightBindingLeaseError,
    verify_preflight_binding_lease,
)
from core.provenance.receipts import assert_public_safe_json, public_receipt_claim
from core.provenance.state import (
    provenance_contract_identity,
    preflight_write_binding_hash,
)
from core.recording import daily_log_append
from core.recording import transaction as recording_transaction
from core.safety import input_transport

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    DailyLogCursorError,
    EnvironmentContractError,
    StorageResolutionError,
    daily_log_cursors_equivalent,
    daily_log_cursor_from_text,
    daily_log_cursor_is_legacy_empty,
    daily_log_cursor_state_fields,
    detect_update_protocol_time_policy_cues,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue_for_sources,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    latest_active_daily_log,
    latest_active_daily_log_cursor,
    load_workspace_state,
    normalize_wrapper_metadata_json,
    PACKAGE_VERSION,
    parse_iso_date,
    public_project_path,
    read_text,
    resolve_writer_attribution,
    validate_iso_date,
    WrapperMetadataSecurityError,
)


DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024
PREFLIGHT_BINDING_TYPE = "recallloom.preflight_write_binding"
PREFLIGHT_BINDING_VERSION = "0.1"
REVIEW_IMPORTED_BASELINE_CONFIRMATION = "review_imported_baseline_confirmed"
PREFLIGHT_BINDING_ALLOWED_KEYS = {
    "binding_type",
    "binding_version",
    "operation_class",
    "file_key",
    "write_type",
    "target_date",
    "latest_file",
    "latest_entry_id",
    "latest_entry_seq",
    "entry_count",
    "latest_file_digest",
    "contract_type",
    "expected_workspace_revision",
    "expected_revisions",
    "preflight_contract_identity",
    "preflight_contract_hash",
    "provenance_state",
    "write_readiness_label",
    "ux_gate",
    "ux_gate_requires_confirmation",
    "ux_gate_confirmation",
    "ux_gate_reason",
}
PREFLIGHT_BINDING_REQUIRED_KEYS = {
    "binding_type",
    "binding_version",
    "operation_class",
    "file_key",
    "write_type",
    "target_date",
    "latest_file",
    "latest_entry_id",
    "latest_entry_seq",
    "entry_count",
    "latest_file_digest",
    "expected_workspace_revision",
    "preflight_contract_identity",
    "preflight_contract_hash",
}
PREFLIGHT_WRITE_READINESS_LABELS = {
    "structural_only_ready_after_preflight",
    "helper_evidenced_ready_after_preflight",
    "review_imported_baseline_ready_after_preflight",
}


def sha256_text_digest(text: str) -> str:
    return daily_log_append.sha256_text_digest(text)




def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--max-input-bytes must be an integer.") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--max-input-bytes must be greater than zero.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely append a milestone entry to a RecallLoom daily log."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument("--date", help="Daily log date in YYYY-MM-DD.")
    parser.add_argument(
        "--allow-historical",
        action="store_true",
        help=(
            "Allow appending to a non-latest ISO-dated daily log. "
            "Without this flag, appends to older daily logs are rejected."
        ),
    )
    parser.add_argument("--entry-file", help="Path to prepared entry content.")
    parser.add_argument("--entry-json", help="Prepared entry JSON object as a string.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared entry content from UTF-8 stdin instead of a file.",
    )
    parser.add_argument(
        "--input-format",
        choices=("auto", "markdown", "json"),
        default="auto",
        help=(
            "Interpret prepared entry input as markdown or JSON. "
            "auto treats --entry-json as JSON and other sources as markdown."
        ),
    )
    parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        default=DEFAULT_MAX_INPUT_BYTES,
        help="Maximum prepared-entry input size in bytes. Defaults to 4 MiB.",
    )
    parser.add_argument("--expected-workspace-revision", type=int)
    parser.add_argument(
        "--no-auto-detect",
        action="store_true",
        help=(
            "Require explicit --date and --expected-workspace-revision instead of auto-detecting "
            "missing values from the locked workspace state."
        ),
    )
    parser.add_argument("--writer-id")
    parser.add_argument(
        "--wrapper-metadata-json",
        help=(
            "Optional wrapper metadata JSON object for additive public output. "
            "Only public-safe host/surface keys and version-like local_wrapper_version values are accepted."
        ),
    )
    parser.add_argument(
        "--preflight-binding-json",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def _exit_input_transport_error(
    parser,
    *,
    json_mode: bool,
    error: input_transport.InputTransportError,
) -> None:
    """Project a typed input-transport failure onto the legacy exit contract."""
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=error.exit_code,
        message=error.message,
        reason=error.reason,
        details=error.details,
    )


def _exit_daily_log_append_planner_error(
    parser,
    *,
    json_mode: bool,
    error: daily_log_append.DailyLogAppendPlannerError,
    legacy_details: dict | None = None,
) -> None:
    """Project a typed pure-planner failure onto the legacy exit contract."""
    def needs_legacy_details(details: dict) -> bool:
        return (
            "hard_block_reasons" in details
            or (
                "missing_section_keys" in details
                and details.get("input_mode") == "stdin"
            )
        )

    if legacy_details:
        if error.details is not None and needs_legacy_details(error.details):
            error.details = {**legacy_details, **error.details}
        if (
            error.payload is not None
            and isinstance(error.payload.get("details"), dict)
            and needs_legacy_details(error.payload["details"])
        ):
            error.payload["details"] = {
                **legacy_details,
                **error.payload["details"],
            }
    if error.payload is not None:
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=error.exit_code,
            message=error.message,
            payload=error.payload,
        )
        raise AssertionError("unreachable")
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=error.exit_code,
        message=error.message,
        reason=error.reason,
        details=error.details,
    )








def preflight_binding_failure(
    parser,
    *,
    json_mode: bool,
    message: str,
    reason_code: str,
    field_path: str = "$",
    extra: dict | None = None,
) -> None:
    details = {
        "reason_code": reason_code,
        "field_path": field_path,
        "side_effect": "none",
        **(extra or {}),
    }
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=message,
        reason="invalid_prepared_input",
        details=details,
    )




def normalize_preflight_binding(
    parser,
    *,
    json_mode: bool,
    raw: str | None,
    project_root: Path,
    expected_workspace_revision: int,
) -> dict | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message=f"--preflight-binding-json must be valid JSON: {exc.msg}.",
            reason_code="malformed_preflight_binding_json",
        )
    if not isinstance(payload, dict):
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="--preflight-binding-json must be a JSON object.",
            reason_code="preflight_binding_not_object",
        )
    unknown = sorted(set(payload).difference(PREFLIGHT_BINDING_ALLOWED_KEYS))
    if unknown:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding contains unsupported keys.",
            reason_code="preflight_binding_unknown_key",
            field_path=",".join(unknown),
        )
    missing = sorted(PREFLIGHT_BINDING_REQUIRED_KEYS.difference(payload))
    if missing:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding is missing required fields.",
            reason_code="preflight_binding_missing_required_key",
            field_path=",".join(missing),
        )
    if payload.get("binding_type") != PREFLIGHT_BINDING_TYPE:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding type does not match this helper.",
            reason_code="preflight_binding_type_mismatch",
            field_path="$.binding_type",
        )
    if payload.get("binding_version") != PREFLIGHT_BINDING_VERSION:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding version does not match this helper.",
            reason_code="preflight_binding_version_mismatch",
            field_path="$.binding_version",
        )
    if payload.get("operation_class") != "daily_log_append":
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding operation_class does not match daily-log append.",
            reason_code="preflight_binding_operation_class_invalid",
            field_path="$.operation_class",
        )
    if payload.get("file_key") != "daily_log":
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding file_key does not match daily-log append.",
            reason_code="preflight_binding_file_key_mismatch",
            field_path="$.file_key",
        )
    if payload.get("write_type") != "milestone_evidence":
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding write_type does not match daily-log append.",
            reason_code="preflight_binding_write_type_mismatch",
            field_path="$.write_type",
        )
    target_date = payload.get("target_date")
    if not isinstance(target_date, str) or not target_date.strip():
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding target_date must be an ISO date string.",
            reason_code="preflight_binding_target_date_invalid",
            field_path="$.target_date",
        )
    try:
        parse_iso_date(target_date)
    except ValueError:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding target_date is not a valid ISO date.",
            reason_code="preflight_binding_target_date_invalid",
            field_path="$.target_date",
        )
    for field in ("latest_file", "latest_entry_id"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            preflight_binding_failure(
                parser,
                json_mode=json_mode,
                message=f"Preflight binding {field} must be null or a non-empty string.",
                reason_code=f"preflight_binding_{field}_invalid",
                field_path=f"$.{field}",
            )
    for field in ("latest_entry_seq", "entry_count"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            preflight_binding_failure(
                parser,
                json_mode=json_mode,
                message=f"Preflight binding {field} must be null or a non-negative integer.",
                reason_code=f"preflight_binding_{field}_invalid",
                field_path=f"$.{field}",
            )
    latest_file_digest = payload.get("latest_file_digest")
    if latest_file_digest is not None and (
        not isinstance(latest_file_digest, str)
        or not latest_file_digest.startswith("sha256:")
    ):
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding latest_file_digest must be null or a sha256 digest string.",
            reason_code="preflight_binding_latest_file_digest_invalid",
            field_path="$.latest_file_digest",
        )
    if payload.get("expected_workspace_revision") != expected_workspace_revision:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding expected_workspace_revision does not match this append.",
            reason_code="preflight_binding_workspace_revision_mismatch",
            field_path="$.expected_workspace_revision",
        )
    expected_preflight_identity = provenance_contract_identity()
    if payload.get("preflight_contract_identity") != expected_preflight_identity:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding contract identity does not match the active provenance contract.",
            reason_code="preflight_binding_contract_identity_mismatch",
            field_path="$.preflight_contract_identity",
            extra={"expected_preflight_contract_identity": expected_preflight_identity},
        )
    expected_binding_hash = preflight_write_binding_hash(payload)
    if payload.get("preflight_contract_hash") != expected_binding_hash:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding hash does not match the canonical binding payload.",
            reason_code="preflight_binding_hash_mismatch",
            field_path="$.preflight_contract_hash",
            extra={"expected_preflight_contract_hash": expected_binding_hash},
        )
    try:
        assert_public_safe_json(payload, project_root=str(project_root))
    except ValueError as exc:
        details = getattr(exc, "details", {})
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding is not public-safe.",
            reason_code=str(details.get("reason_code") or "preflight_binding_privacy_violation"),
            field_path=str(details.get("field_path") or "$"),
        )
    return payload


def preflight_cursor_is_no_log(cursor: dict[str, object]) -> bool:
    return (
        cursor.get("latest_file") is None
        and cursor.get("latest_entry_id") is None
        and cursor.get("latest_entry_seq") in {0, None}
        and cursor.get("entry_count") in {0, None}
    )


def enforce_preflight_daily_log_cursor(
    parser,
    *,
    json_mode: bool,
    workspace,
    state: dict,
    preflight_binding: dict | None,
    target_date: date,
    latest_existing: Path | None,
) -> None:
    if preflight_binding is None:
        return
    target_date_iso = target_date.isoformat()
    if preflight_binding.get("target_date") != target_date_iso:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding target_date does not match this append.",
            reason_code="preflight_binding_target_date_mismatch",
            field_path="$.target_date",
            extra={
                "binding_target_date": preflight_binding.get("target_date"),
                "actual_target_date": target_date_iso,
            },
        )

    daily_state = state.get("daily_logs")
    if not isinstance(daily_state, dict):
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="state.json daily_logs cursor is unavailable for append binding verification.",
            reason_code="preflight_binding_daily_log_cursor_unavailable",
            field_path="$.daily_logs",
        )

    latest_file = (
        latest_existing.relative_to(workspace.storage_root).as_posix()
        if latest_existing is not None
        else None
    )
    state_cursor = {
        "latest_file": daily_state.get("latest_file"),
        "latest_entry_id": daily_state.get("latest_entry_id"),
        "latest_entry_seq": daily_state.get("latest_entry_seq"),
        "entry_count": daily_state.get("entry_count"),
    }
    actual_cursor = dict(state_cursor)
    actual_latest_digest = None
    if latest_existing is not None:
        try:
            latest_text = read_text(latest_existing)
        except (OSError, UnicodeDecodeError) as exc:
            preflight_binding_failure(
                parser,
                json_mode=json_mode,
                message=f"Could not read latest daily log for append binding verification: {exc}",
                reason_code="preflight_binding_daily_log_cursor_unreadable",
                field_path="$.latest_file",
            )
        actual_latest_digest = sha256_text_digest(latest_text)
        try:
            actual_cursor = daily_log_cursor_state_fields(
                latest_active_daily_log_cursor(workspace.storage_root).as_state_fields()
            )
        except DailyLogCursorError as exc:
            preflight_binding_failure(
                parser,
                json_mode=json_mode,
                message=str(exc),
                reason_code=exc.reason_code,
                field_path="$.daily_logs",
                extra={"actual_latest_file": latest_file},
            )
    elif daily_state.get("latest_file") is None:
        actual_cursor = {
            "latest_file": None,
            "latest_entry_id": None,
            "latest_entry_seq": 0,
            "entry_count": 0,
        }
        state_cursor = dict(actual_cursor)

    binding_cursor = {
        "latest_file": preflight_binding.get("latest_file"),
        "latest_entry_id": preflight_binding.get("latest_entry_id"),
        "latest_entry_seq": preflight_binding.get("latest_entry_seq"),
        "entry_count": preflight_binding.get("entry_count"),
    }
    if preflight_cursor_is_no_log(binding_cursor) and daily_log_cursor_is_legacy_empty(actual_cursor):
        binding_cursor = dict(actual_cursor)
    binding_latest_digest = preflight_binding.get("latest_file_digest")

    if not daily_log_cursors_equivalent(
        binding_cursor,
        state_cursor,
        actual_cursor=actual_cursor,
    ):
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding daily-log cursor does not match current state.",
            reason_code="preflight_binding_daily_log_cursor_mismatch",
            field_path="$.daily_logs",
            extra={
                "binding_cursor": binding_cursor,
                "state_cursor": state_cursor,
            },
        )
    if not daily_log_cursors_equivalent(
        state_cursor,
        actual_cursor,
        actual_cursor=actual_cursor,
    ):
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Current latest daily-log file does not match the state cursor.",
            reason_code="preflight_binding_daily_log_file_cursor_mismatch",
            field_path="$.daily_logs",
            extra={
                "state_cursor": state_cursor,
                "actual_cursor": actual_cursor,
            },
        )
    if binding_latest_digest != actual_latest_digest:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding latest daily-log digest does not match the current file.",
            reason_code="preflight_binding_daily_log_digest_mismatch",
            field_path="$.latest_file_digest",
            extra={
                "binding_latest_file_digest": binding_latest_digest,
                "actual_latest_file_digest": actual_latest_digest,
            },
        )


def build_append_failure_details(
    *,
    project_root: Path,
    target_path: Path,
    target_date: str,
    current_workspace_revision: int,
    entry_path: Path | None,
    input_mode: str,
    extra: dict | None = None,
) -> dict:
    details: dict[str, object] = {
        "project_root": str(project_root),
        "target_path": str(target_path),
        "target_date": target_date,
        "current_workspace_revision": current_workspace_revision,
        "input_mode": input_mode,
    }
    if entry_path is not None:
        details["entry_path"] = str(entry_path)
    if extra:
        details.update(extra)
    return details


def legacy_append_input_mode(*, acquired_mode: str, input_format: str) -> str:
    """Map the transport vocabulary onto the frozen append CLI vocabulary."""
    if acquired_mode == "entry-json":
        return "json-string"
    if input_format == "json":
        return "json-file" if acquired_mode == "file" else "json-stdin"
    return acquired_mode


def resolve_target_date(
    *,
    explicit_date: str | None,
    latest_existing: Path | None,
    logical_workday: date,
) -> tuple[date, date | None, str]:
    return daily_log_append.resolve_target_date(
        explicit_date=explicit_date,
        latest_existing=latest_existing,
        logical_workday=logical_workday,
    )


def exit_with_append_date_guard(
    parser,
    *,
    json_mode: bool,
    workspace_language: str,
    exit_code: int,
    reason: str,
    message: str,
    details: dict,
) -> None:
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        payload=failure_payload(
            reason,
            language=workspace_language,
            error=message,
            details=details,
        ),
    )


def enforce_logical_workday_append_guards(
    parser,
    *,
    json_mode: bool,
    workspace_language: str,
    target_path: Path,
    target_date: date,
    latest_existing: Path | None,
    logical_workday: date | None = None,
    recovery_details: dict | None = None,
) -> date | None:
    if logical_workday is None:
        logical_workday = logical_workday_for(
            datetime.now().astimezone(),
            DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
        )
    logical_workday_iso = logical_workday.isoformat()
    latest_existing_date = parse_iso_date(latest_existing.stem) if latest_existing is not None else None
    latest_existing_text = str(latest_existing) if latest_existing is not None else None
    base_details = dict(recovery_details or {})
    base_details.update(
        {
            "target_path": str(target_path),
            "target_date": target_date.isoformat(),
            "logical_workday": logical_workday_iso,
            "latest_active_daily_log": latest_existing_text,
        }
    )

    if target_date > logical_workday:
        message = (
            f"Refusing to append to future-dated daily log {target_path}. "
            f"The current logical workday is {logical_workday_iso}. "
            "--allow-historical only applies to intentional historical backfills and cannot override "
            "future-dated append guards."
        )
        exit_with_append_date_guard(
            parser,
            json_mode=json_mode,
            workspace_language=workspace_language,
            exit_code=2,
            reason="project_time_policy_review_required",
            message=message,
            details=base_details,
        )

    if latest_existing_date is not None and latest_existing_date > logical_workday:
        details = dict(base_details)
        details["latest_active_day"] = latest_existing_date.isoformat()
        message = (
            "Refusing to append because the latest active ISO-dated daily log "
            f"{latest_existing} is ahead of the current logical workday {logical_workday_iso}. "
            "Review the active date before appending to any daily log. "
            "--allow-historical only applies to intentional historical backfills and cannot override "
            "future-dated append guards."
        )
        exit_with_append_date_guard(
            parser,
            json_mode=json_mode,
            workspace_language=workspace_language,
            exit_code=2,
            reason="project_time_policy_review_required",
            message=message,
            details=details,
        )

    return latest_existing_date


def main() -> None:
    """CLI/input/legacy-output adapter; transaction owns every mutation stage."""
    parser = build_parser()
    args = parser.parse_args()
    exit_with_failure_contract(
        parser, json_mode=args.json, exit_code=3,
        message=("Direct mutation helper invocation is not authorized. "
                 "Use the RecallLoom dispatcher for this transaction."),
        reason="invalid_transaction_authority",
        details={"side_effect": "none"},
    )
    raise AssertionError("unreachable")


def run_from_dispatcher(argv: list[str], *, authority) -> None:
    """Trusted in-process adapter entry; the dispatcher owns authority."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_cli_error(parser, json_mode=args.json, exit_code=2, message=str(exc),
                            payload=cli_failure_payload("python_runtime_unavailable", error=str(exc)))
    support = enforce_package_support_gate(parser, json_mode=args.json)
    try:
        wrapper_metadata = normalize_wrapper_metadata_json(args.wrapper_metadata_json)
        if args.date is not None and not validate_iso_date(args.date):
            exit_with_failure_contract(parser, json_mode=args.json, exit_code=2,
                                       message=f"Invalid --date value: {args.date}", reason="invalid_date", details={"date": args.date})
        attribution = resolve_writer_attribution(explicit_writer_id=args.writer_id,
                                                 invocation_surface="append_daily_log_entry.py",
                                                 wrapper_metadata=wrapper_metadata)
        workspace = find_recallloom_root(args.path)
    except WrapperMetadataSecurityError as exc:
        exit_with_failure_contract(parser, json_mode=args.json, exit_code=4, message=str(exc),
                                   reason="privacy_security_failure", details=exc.details)
    except ConfigContractError as exc:
        exit_with_failure_contract(parser, json_mode=args.json, exit_code=2, message=str(exc),
                                   reason="invalid_tool_name", details={"writer_id_source": "explicit_cli"})
    except StorageResolutionError as exc:
        exit_with_cli_error(parser, json_mode=args.json, exit_code=2, message=str(exc),
                            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"))
    if workspace is None:
        exit_with_failure_contract(parser, json_mode=args.json, exit_code=1,
                                   message="No RecallLoom project root found.", reason="no_project_root")
    startup_residue_report = exit_if_startup_scratch_residue_for_sources(
        parser, json_mode=args.json, project_root=workspace.project_root,
        storage_root=workspace.storage_root, source_paths=[args.entry_file])
    state_path = workspace.storage_root / FILE_KEYS["state"]
    state = load_workspace_state(state_path)
    latest_existing = latest_active_daily_log(workspace.storage_root / DAILY_LOGS_DIRNAME)
    logical_workday = logical_workday_for(datetime.now().astimezone(), DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR)
    cues_path = workspace.storage_root / FILE_KEYS["update_protocol"]
    cues = detect_update_protocol_time_policy_cues(read_text(cues_path)) if cues_path.is_file() else []
    if args.date is None and cues:
        exit_with_append_date_guard(parser, json_mode=args.json, workspace_language=workspace.workspace_language,
            exit_code=2, reason="project_time_policy_review_required",
            message="Project-local time-policy cues were detected in update_protocol.md. Append requires an explicit --date before writing when date auto-detect would otherwise apply.",
            details={"logical_workday": logical_workday.isoformat(), "project_time_policy_cues": cues})
    target_date, latest_existing_date, date_resolution_source = resolve_target_date(
        explicit_date=args.date, latest_existing=latest_existing, logical_workday=logical_workday)
    target_path = workspace.storage_root / DAILY_LOGS_DIRNAME / f"{target_date.isoformat()}.md"
    expected_workspace_revision = state["workspace_revision"] if args.expected_workspace_revision is None else args.expected_workspace_revision
    details = build_append_failure_details(project_root=workspace.project_root, target_path=target_path,
        target_date=target_date.isoformat(), current_workspace_revision=state["workspace_revision"],
        entry_path=Path(args.entry_file) if args.entry_file else None, input_mode=None, extra={})
    latest_existing_date = enforce_logical_workday_append_guards(parser, json_mode=args.json,
        workspace_language=workspace.workspace_language, target_path=target_path, target_date=target_date,
        latest_existing=latest_existing, logical_workday=logical_workday, recovery_details=details)
    historical = latest_existing_date is not None and target_date < latest_existing_date
    if historical and not args.allow_historical:
        historical_input_mode = (
            "json-string" if args.entry_json is not None
            else "file" if args.entry_file is not None
            else "stdin"
        )
        historical_details = build_append_failure_details(
            project_root=workspace.project_root, target_path=target_path,
            target_date=target_date.isoformat(),
            current_workspace_revision=state["workspace_revision"],
            entry_path=Path(args.entry_file) if args.entry_file else None,
            input_mode=historical_input_mode, extra={
                "auto_detected_date": args.date is None,
                "auto_detected_workspace_revision": args.expected_workspace_revision is None,
                "date_resolution_source": date_resolution_source,
                "workspace_revision_source": "state_current" if args.expected_workspace_revision is None else "explicit",
                "logical_workday": logical_workday.isoformat(),
                "latest_active_daily_log": str(latest_existing),
                "latest_active_day": latest_existing_date.isoformat(),
            })
        message = (
            f"Refusing to append to non-latest daily log {target_path}. "
            f"The latest active ISO-dated daily log is {latest_existing}. "
            "Re-run with --allow-historical only when you intentionally need a historical append."
        )
        exit_with_cli_error(parser, json_mode=args.json, exit_code=2,
            message=message,
            payload=failure_payload("historical_append_requires_confirmation", language=workspace.workspace_language, error=message, details=historical_details))
    binding = normalize_preflight_binding(parser, json_mode=args.json, raw=args.preflight_binding_json,
        project_root=workspace.project_root, expected_workspace_revision=expected_workspace_revision)
    if binding is not None and historical:
        exit_with_cli_error(parser, json_mode=args.json, exit_code=3,
            message="Refusing receipt-backed append to a historical daily log. v0.4.2 provenance receipts only bind the current latest daily-log cursor.",
            payload=failure_payload("historical_append_not_receipt_backed", language=workspace.workspace_language, error="Historical append is not receipt backed.", details=details))
    enforce_preflight_daily_log_cursor(parser, json_mode=args.json, workspace=workspace, state=state,
        preflight_binding=binding, target_date=target_date, latest_existing=latest_existing)
    try:
        acquired = recording_transaction.acquire_preview_input(operation=OPERATION_DAILY_LOG_APPEND,
            entry_json=args.entry_json, entry_file=args.entry_file, use_stdin=args.stdin,
            max_input_bytes=args.max_input_bytes, project_root=workspace.project_root, storage_root=workspace.storage_root)
    except input_transport.InputTransportError as exc:
        _exit_input_transport_error(parser, json_mode=args.json, error=exc)
        raise AssertionError("unreachable")
    output_input_mode = legacy_append_input_mode(
        acquired_mode=acquired.input_mode,
        input_format=args.input_format,
    )
    if args.entry_json is not None:
        try:
            daily_log_append.prepare_entry_text(
                acquired, input_format=args.input_format, entry_path=None,
                project_root=workspace.project_root,
            )
        except daily_log_append.DailyLogAppendPlannerError as exc:
            _exit_daily_log_append_planner_error(parser, json_mode=args.json, error=exc)
    planner_failure_details = build_append_failure_details(
        project_root=workspace.project_root,
        target_path=target_path,
        target_date=target_date.isoformat(),
        current_workspace_revision=state["workspace_revision"],
        entry_path=Path(args.entry_file) if args.entry_file else None,
        input_mode=output_input_mode,
        extra={
            "auto_detected_date": args.date is None,
            "auto_detected_workspace_revision": args.expected_workspace_revision is None,
            "date_resolution_source": date_resolution_source,
            "workspace_revision_source": (
                "state_current"
                if args.expected_workspace_revision is None
                else "explicit"
            ),
            "logical_workday": logical_workday.isoformat(),
            "latest_active_daily_log": (
                str(latest_existing) if latest_existing else None
            ),
            "latest_active_day": (
                latest_existing_date.isoformat() if latest_existing_date else None
            ),
        },
    )
    if args.no_auto_detect and (args.date is None or args.expected_workspace_revision is None):
        missing = [name for name, value in (("date", args.date), ("expected_workspace_revision", args.expected_workspace_revision)) if value is None]
        exit_with_failure_contract(parser, json_mode=args.json, exit_code=2,
            message="--no-auto-detect requires explicit --date and --expected-workspace-revision. Missing: " + ", ".join(missing) + ".",
            reason="invalid_prepared_input", details={
                "project_root": str(workspace.project_root),
                "input_mode": output_input_mode,
                "missing_fields": missing, "no_auto_detect": True,
            })
    if state["workspace_revision"] != expected_workspace_revision:
        message = (
            f"Workspace revision changed from {expected_workspace_revision} to "
            f"{state['workspace_revision']}. Rerun preflight before appending."
        )
        exit_with_cli_error(parser, json_mode=args.json, exit_code=3, message=message,
            payload=failure_payload("stale_write_context", language=workspace.workspace_language,
                error=message, details={
                    "expected_workspace_revision": expected_workspace_revision,
                    **planner_failure_details,
                }))
    cursor = daily_log_cursor_state_fields(state.get("daily_logs", {}))
    cursor["target_path"] = str(target_path)
    cursor["target_is_latest_after_write"] = not historical
    expected_file_revision = 0
    if target_path.is_file():
        expected_file_revision = daily_log_cursor_from_text(read_text(target_path), path=target_path, latest_file=target_path.relative_to(workspace.storage_root).as_posix()).entry_count
    request = recording_transaction.TransactionRequest(context=OperationContext(command=COMMAND_APPEND, operation=OPERATION_DAILY_LOG_APPEND, write_type="milestone_evidence", input_mode=acquired.input_mode, stage=STAGE_PREFLIGHT),
        mode=recording_transaction.MODE_APPLY, workspace_root=workspace.project_root, storage_root=workspace.storage_root,
        acquired_input=acquired, file_key="daily_log", write_type="milestone_evidence",
        expected_file_revision=expected_file_revision, expected_workspace_revision=expected_workspace_revision,
        expected_cursor=cursor, expected_input_digest=None, preview_binding=None, confirmation=None, writer_attribution=attribution)
    outcome = recording_transaction.run_transaction(request, support=support, preflight_binding=binding, authority=authority,
        package_version=PACKAGE_VERSION, input_format=args.input_format, source_file=args.entry_file)
    if isinstance(outcome, recording_transaction.TransactionFailure):
        cause = outcome.cause
        if isinstance(cause, daily_log_append.DailyLogAppendPlannerError):
            _exit_daily_log_append_planner_error(
                parser,
                json_mode=args.json,
                error=cause,
                legacy_details=planner_failure_details,
            )
        if isinstance(cause, recording_transaction.TransactionStageError):
            exit_with_failure_contract(parser, json_mode=args.json, exit_code=cause.exit_code, message=cause.message, reason=cause.reason, details=cause.details, findings=cause.findings, extra=cause.extra)
        raise AssertionError(f"untyped transaction failure: {cause!r}")
    result, receipt_finalization = outcome
    payload = {"ok": True, "input_mode": output_input_mode, "target_path": str(target_path),
        "entry_seq": result.revisions["new_file_revision"], "new_workspace_revision": result.revisions["new_workspace_revision"],
        "allow_historical": args.allow_historical, "state_cursor_updated": not historical, **attribution.public_fields(),
        "auto_detect": {"date_used": args.date is None, "workspace_revision_used": args.expected_workspace_revision is None,
            "logical_workday": logical_workday.isoformat(), "latest_active_daily_log": str(latest_existing) if latest_existing else None,
            "latest_active_day": latest_existing_date.isoformat() if latest_existing_date else None, "resolved_date": target_date.isoformat(),
            "resolved_workspace_revision": expected_workspace_revision, "date_resolution_source": date_resolution_source,
            "workspace_revision_source": "state_current" if args.expected_workspace_revision is None else "explicit",
            "workspace_revision_guard_mode": "lock_snapshot_current" if args.expected_workspace_revision is None else "explicit_mismatch_check"}}
    if wrapper_metadata is not None: payload["wrapper_metadata"] = wrapper_metadata
    if receipt_finalization is not None:
        receipt = receipt_finalization["receipt"]
        payload.update({"provenance_state": "helper_evidenced", "provenance_result": {"state_label": "helper_evidenced", "receipt_backed": True, "receipt_finalization_status": "finalized", "receipt_store_available": True, "receipt_digest": receipt_finalization["receipt_digest"], "store_binding": receipt_finalization["store_binding"], "redaction_policy_version": receipt.get("redaction_policy_version")}, "public_receipt_claim": public_receipt_claim(receipt, project_root=str(workspace.project_root))})
    if args.json:
        if startup_residue_report is not None: payload["startup_residue_report"] = startup_residue_report
        print(json.dumps(publicize_json_value(payload, project_root=workspace.project_root), ensure_ascii=False, indent=2))
    else:
        print(f"Appended daily log entry to {public_project_path(target_path, project_root=workspace.project_root) or 'daily log'}")


if __name__ == "__main__":
    main()
