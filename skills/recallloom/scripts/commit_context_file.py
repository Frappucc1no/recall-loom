#!/usr/bin/env python3
"""Safely commit a prepared RecallLoom managed file with revision checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.failure.context import (
    OPERATION_MANAGED_WRITE,
    OPERATION_POST_APPEND_SUMMARY_SYNC,
    STAGE_INPUT,
    OperationContext,
    legacy_command_for,
)
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
from core.provenance.store import (
    RECEIPT_STORE_NOT_WRITTEN_VERIFIED,
    ReceiptStoreError,
    finalize_receipt_in_store,
)
from core.recording import managed_write
from core.recording import transaction as recording_transaction
from core.safety import input_transport
from core.workspace.atomic_io import atomic_write_and_verify_if_unchanged

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    LockBusyError,
    StorageResolutionError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue_for_sources,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    normalize_wrapper_metadata_json,
    PACKAGE_VERSION,
    public_json_payload,
    public_project_path,
    resolve_writer_attribution,
    stable_text_readback,
    WrapperMetadataSecurityError,
)


# Harness cut-point seams (no in-module callers since the T050-05 legacy
# deletion): the mother-workspace launchers (tests/harness/v050_workspace.py,
# the provenance race launcher, the crash-race write launcher) patch
# ``atomic_write_and_verify_if_unchanged``, ``stable_text_readback`` and
# ``finalize_receipt_in_store`` and reference ``LockBusyError`` /
# ``ReceiptStoreError`` / ``RECEIPT_STORE_NOT_WRITTEN_VERIFIED`` on this
# module so one injector covers both write-path topologies. These are
# imports from the final owners, not second implementations; T050-09A owns
# any final cleanup.


WRITABLE_FILE_KEYS = {"context_brief", "rolling_summary", "update_protocol"}
DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024
PREFLIGHT_BINDING_TYPE = "recallloom.preflight_write_binding"
PREFLIGHT_BINDING_VERSION = "0.1"
PREFLIGHT_BINDING_ALLOWED_KEYS = {
    "binding_type",
    "binding_version",
    "operation_class",
    "file_key",
    "write_type",
    "contract_type",
    "expected_file_revision",
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
    "assertion_source_kind",
    "assertion_source_id",
    "assertion_payload_digest",
    "input_digest",
    "preflight_binding_digest",
    "managed_body_digest_before",
    "assertion_binding_seed_digest",
}
PREFLIGHT_BINDING_REQUIRED_KEYS = {
    "binding_type",
    "binding_version",
    "operation_class",
    "file_key",
    "write_type",
    "expected_file_revision",
    "expected_workspace_revision",
    "preflight_contract_identity",
    "preflight_contract_hash",
}
RECEIPT_OPERATION_CLASSES = {"managed_file_commit", "post_append_summary_sync"}


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
        description="Safely commit a prepared RecallLoom managed file with revision checks."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument("--file-key", required=True, choices=sorted(WRITABLE_FILE_KEYS))
    parser.add_argument("--source-file", help="Path to prepared markdown content.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared markdown content from UTF-8 stdin instead of a file.",
    )
    parser.add_argument(
        "--input-format",
        choices=("markdown", "json"),
        default="markdown",
        help=(
            "Interpret prepared content as markdown or rolling-summary JSON. "
            "JSON input is only supported for --file-key rolling_summary."
        ),
    )
    parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        default=DEFAULT_MAX_INPUT_BYTES,
        help="Maximum prepared-content input size in bytes. Defaults to 4 MiB.",
    )
    parser.add_argument("--expected-file-revision", type=int, required=True)
    parser.add_argument("--expected-workspace-revision", type=int, required=True)
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--expected-input-digest",
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


def _exit_managed_write_planner_error(
    parser,
    *,
    json_mode: bool,
    error: managed_write.ManagedWritePlannerError,
) -> None:
    """Project a typed pure-planner failure onto the legacy exit contract."""
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
    file_key: str,
    expected_file_revision: int,
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
    operation_class = payload.get("operation_class")
    if operation_class not in RECEIPT_OPERATION_CLASSES:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding operation_class is not supported.",
            reason_code="preflight_binding_operation_class_invalid",
            field_path="$.operation_class",
        )
    if payload.get("file_key") != file_key:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding file_key does not match this write.",
            reason_code="preflight_binding_file_key_mismatch",
            field_path="$.file_key",
        )
    if payload.get("expected_file_revision") != expected_file_revision:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding expected_file_revision does not match this write.",
            reason_code="preflight_binding_file_revision_mismatch",
            field_path="$.expected_file_revision",
        )
    if payload.get("expected_workspace_revision") != expected_workspace_revision:
        preflight_binding_failure(
            parser,
            json_mode=json_mode,
            message="Preflight binding expected_workspace_revision does not match this write.",
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























def _exit_transaction_failure(
    parser,
    *,
    json_mode: bool,
    failure: "recording_transaction.TransactionFailure",
) -> None:
    """Project a transaction failure onto the legacy exit contract, byte-identical."""
    cause = failure.cause
    if isinstance(cause, input_transport.InputTransportError):
        _exit_input_transport_error(parser, json_mode=json_mode, error=cause)
    elif isinstance(cause, managed_write.ManagedWritePlannerError):
        _exit_managed_write_planner_error(parser, json_mode=json_mode, error=cause)
    elif isinstance(cause, recording_transaction.TransactionStageError):
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=cause.exit_code,
            message=cause.message,
            reason=cause.reason,
            details=cause.details,
            findings=cause.findings,
            extra=cause.extra,
        )
    else:  # pragma: no cover - program defect: every expected failure carries a typed cause
        raise AssertionError(f"untyped transaction failure cause: {cause!r}")


def _main_preview(parser, args: argparse.Namespace, *, authority) -> None:
    """The managed-write dry-run/preview adapter (T050-04A, S3–S5 topology).

    Parses exactly like the apply path, acquires the input, then delegates to
    ``core.recording.transaction`` (mode=preview): the transaction runs the
    frozen stage sequence up to DRY_RUN_RETURN with a transitional authority
    built in-process, in-lock, after exact lease validation. The preview may
    briefly take/release the identity lock and read; it never writes target,
    state, receipt, binding/lease, scratch, or managed/derived files. The
    managed-write apply path delegates to the same transaction (mode=apply)
    in ``_main_apply_managed``; the post-append sync apply path delegates to
    the same transaction since T050-05.
    """
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
    support = enforce_package_support_gate(parser, json_mode=args.json)

    try:
        wrapper_metadata = normalize_wrapper_metadata_json(args.wrapper_metadata_json)
    except WrapperMetadataSecurityError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=4,
            message=str(exc),
            reason="privacy_security_failure",
            details=exc.details,
        )

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
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            reason="no_project_root",
        )
    preflight_binding = normalize_preflight_binding(
        parser,
        json_mode=args.json,
        raw=args.preflight_binding_json,
        project_root=workspace.project_root,
        file_key=args.file_key,
        expected_file_revision=args.expected_file_revision,
        expected_workspace_revision=args.expected_workspace_revision,
    )
    # No persisted-lease enforcement in preview: §7.3 forbids binding/lease
    # store writes in dry-run. The transaction's AUTHORITY stage performs the
    # exact lease validation in-process and builds the transitional authority
    # inside the identity lock.
    startup_residue_report = exit_if_startup_scratch_residue_for_sources(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        source_paths=[args.source_file],
    )
    operation = (
        OPERATION_POST_APPEND_SUMMARY_SYNC
        if isinstance(preflight_binding, dict)
        and preflight_binding.get("operation_class") == "post_append_summary_sync"
        else OPERATION_MANAGED_WRITE
    )
    try:
        acquired = recording_transaction.acquire_preview_input(
            operation=operation,
            source_file=args.source_file,
            use_stdin=args.stdin,
            max_input_bytes=args.max_input_bytes,
            file_key=args.file_key,
            write_type=managed_write.WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            expected_input_digest=args.expected_input_digest,
        )
    except input_transport.InputTransportError as exc:
        _exit_input_transport_error(parser, json_mode=args.json, error=exc)
        raise AssertionError("unreachable")
    try:
        attribution = resolve_writer_attribution(
            explicit_writer_id=args.writer_id,
            invocation_surface="commit_context_file.py",
            explicit_marker_role=(
                "tool_name" if args.file_key == "rolling_summary" else "writer_id"
            ),
            wrapper_metadata=wrapper_metadata,
        )
    except ConfigContractError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            reason="invalid_tool_name",
            details={"writer_id_source": "explicit_cli"},
        )
    context = OperationContext(
        command=legacy_command_for(operation),
        operation=operation,
        write_type=managed_write.WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
        input_mode=acquired.input_mode,
        stage=STAGE_INPUT,
    )
    request = recording_transaction.TransactionRequest(
        context=context,
        mode=recording_transaction.MODE_PREVIEW,
        workspace_root=workspace.project_root,
        storage_root=workspace.storage_root,
        acquired_input=acquired,
        file_key=args.file_key,
        write_type=managed_write.WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
        expected_file_revision=args.expected_file_revision,
        expected_workspace_revision=args.expected_workspace_revision,
        expected_cursor=None,
        expected_input_digest=args.expected_input_digest,
        preview_binding=None,
        confirmation=None,
        writer_attribution=attribution,
    )
    outcome = recording_transaction.run_transaction(
        request,
        support=support,
        preflight_binding=preflight_binding,
        authority=authority,
        package_version=PACKAGE_VERSION,
        input_format=args.input_format,
        source_file=args.source_file,
    )
    if isinstance(outcome, recording_transaction.TransactionFailure):
        _exit_transaction_failure(parser, json_mode=args.json, failure=outcome)
        raise AssertionError("unreachable")
    result, preview_binding = outcome
    target_path = workspace.storage_root / FILE_KEYS[args.file_key]
    payload = {
        "ok": True,
        "schema_version": "1.1",
        "command": context.command,
        "operation": context.legacy_operation,
        "helper_name": "commit_context_file.py",
        "dry_run": True,
        "preview": True,
        "transaction_result_code": result.result_code,
        "file_key": args.file_key,
        "input_mode": acquired.input_mode,
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "target_path": str(target_path),
        "expected_file_revision": args.expected_file_revision,
        "expected_workspace_revision": args.expected_workspace_revision,
        "new_file_revision": result.revisions["new_file_revision"],
        "new_workspace_revision": result.revisions["new_workspace_revision"],
        "input_digest": result.input_digest,
        "candidate_target_digest": result.target_digest,
        "preview_binding": {
            "input_digest": preview_binding.input_digest,
            "target_identity": preview_binding.target_identity,
            "expected_file_revision": preview_binding.expected_file_revision,
            "expected_workspace_revision": preview_binding.expected_workspace_revision,
            "expected_cursor": preview_binding.expected_cursor,
        },
        **attribution.public_fields(),
    }
    if wrapper_metadata is not None:
        payload["wrapper_metadata"] = wrapper_metadata
    if args.json:
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        public_target = public_project_path(target_path, project_root=workspace.project_root)
        print(
            f"Previewed {args.file_key} write against {public_target or args.file_key}; "
            "no files changed."
        )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    exit_with_failure_contract(
        parser,
        json_mode=args.json,
        exit_code=3,
        message=(
            "Direct mutation helper invocation is not authorized. "
            "Use the RecallLoom dispatcher for this transaction."
        ),
        reason="invalid_transaction_authority",
        details={"side_effect": "none"},
    )
    raise AssertionError("unreachable")


def run_from_dispatcher(argv: list[str], *, authority) -> None:
    """Trusted in-process adapter entry; the dispatcher owns authority."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        _main_preview(parser, args, authority=authority)
        return
    _main_apply(parser, args, authority=authority)


def _main_apply(parser, args: argparse.Namespace, *, authority) -> None:
    """Apply entry: shared prologue, then the transaction adapter.

    Both operation lanes are transaction cutovers (S3–S5 topology): managed
    write since T050-04B, post-append summary sync since T050-05. This
    adapter keeps argument compatibility, the pre-lock store-backed lease
    enforcement, and the byte-identical failure/success projection;
    ``core.recording.transaction`` owns the full frozen stage chain for both
    operations (no second commit implementation; the legacy precheck→final-
    evidence orchestration block was deleted in T050-05 per §7.8).
    """
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
    support = enforce_package_support_gate(parser, json_mode=args.json)

    try:
        wrapper_metadata = normalize_wrapper_metadata_json(args.wrapper_metadata_json)
    except WrapperMetadataSecurityError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=4,
            message=str(exc),
            reason="privacy_security_failure",
            details=exc.details,
        )

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
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            reason="no_project_root",
        )
    preflight_binding = normalize_preflight_binding(
        parser,
        json_mode=args.json,
        raw=args.preflight_binding_json,
        project_root=workspace.project_root,
        file_key=args.file_key,
        expected_file_revision=args.expected_file_revision,
        expected_workspace_revision=args.expected_workspace_revision,
    )
    startup_residue_report = exit_if_startup_scratch_residue_for_sources(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        source_paths=[args.source_file],
    )
    operation = (
        OPERATION_POST_APPEND_SUMMARY_SYNC
        if isinstance(preflight_binding, dict)
        and preflight_binding.get("operation_class") == "post_append_summary_sync"
        else OPERATION_MANAGED_WRITE
    )
    _main_apply_managed(
        parser,
        args,
        support=support,
        workspace=workspace,
        wrapper_metadata=wrapper_metadata,
        preflight_binding=preflight_binding,
        startup_residue_report=startup_residue_report,
        operation=operation,
        authority=authority,
    )


def _main_apply_managed(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
    workspace,
    wrapper_metadata: dict | None,
    preflight_binding: dict | None,
    startup_residue_report: dict | None,
    operation: str,
    authority,
) -> None:
    """The managed-file commit apply adapter (T050-04B/05, S3–S5 topology).

    Serves both operation classes — managed write and post-append summary
    sync. Acquires the input, then delegates to ``core.recording.transaction``
    (mode=apply): the transaction owns the frozen stage chain with the
    transitional authority built in-process, in-lock, from the dispatcher-
    persisted lease (store-backed exact verification). What remains here is
    only the thin CLI adapter: input acquisition, writer attribution, and the
    byte-identical failure/success output projection.
    """
    try:
        acquired = recording_transaction.acquire_preview_input(
            operation=operation,
            source_file=args.source_file,
            use_stdin=args.stdin,
            max_input_bytes=args.max_input_bytes,
            file_key=args.file_key,
            write_type=managed_write.WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            expected_input_digest=args.expected_input_digest,
        )
    except input_transport.InputTransportError as exc:
        _exit_input_transport_error(parser, json_mode=args.json, error=exc)
        raise AssertionError("unreachable")
    try:
        attribution = resolve_writer_attribution(
            explicit_writer_id=args.writer_id,
            invocation_surface="commit_context_file.py",
            explicit_marker_role=(
                "tool_name" if args.file_key == "rolling_summary" else "writer_id"
            ),
            wrapper_metadata=wrapper_metadata,
        )
    except ConfigContractError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            reason="invalid_tool_name",
            details={"writer_id_source": "explicit_cli"},
        )
    context = OperationContext(
        command=legacy_command_for(operation),
        operation=operation,
        write_type=managed_write.WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
        input_mode=acquired.input_mode,
        stage=STAGE_INPUT,
    )
    request = recording_transaction.TransactionRequest(
        context=context,
        mode=recording_transaction.MODE_APPLY,
        workspace_root=workspace.project_root,
        storage_root=workspace.storage_root,
        acquired_input=acquired,
        file_key=args.file_key,
        write_type=managed_write.WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
        expected_file_revision=args.expected_file_revision,
        expected_workspace_revision=args.expected_workspace_revision,
        expected_cursor=None,
        expected_input_digest=args.expected_input_digest,
        preview_binding=None,
        confirmation=None,
        writer_attribution=attribution,
    )
    outcome = recording_transaction.run_transaction(
        request,
        support=support,
        preflight_binding=preflight_binding,
        authority=authority,
        package_version=PACKAGE_VERSION,
        input_format=args.input_format,
        source_file=args.source_file,
    )
    if isinstance(outcome, recording_transaction.TransactionFailure):
        _exit_transaction_failure(parser, json_mode=args.json, failure=outcome)
        raise AssertionError("unreachable")
    result, receipt_finalization = outcome
    # The success payload renders the planner's effective input mode
    # (markdown keeps the acquisition mode; rolling-summary JSON renders
    # json-file/json-stdin), byte-identical to the legacy payload.
    input_mode = acquired.input_mode
    if args.input_format == "json":
        input_mode = "json-file" if input_mode == "file" else "json-stdin"
    target_path = workspace.storage_root / FILE_KEYS[args.file_key]
    payload = {
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "file_key": args.file_key,
        "input_mode": input_mode,
        "target_path": str(target_path),
        "new_file_revision": result.revisions["new_file_revision"],
        "new_workspace_revision": result.revisions["new_workspace_revision"],
        **attribution.public_fields(),
        "ok": True,
    }
    if receipt_finalization is not None:
        receipt = receipt_finalization["receipt"]
        payload.update(
            {
                "provenance_state": "helper_evidenced",
                "provenance_result": {
                    "state_label": "helper_evidenced",
                    "receipt_backed": True,
                    "receipt_finalization_status": "finalized",
                    "receipt_store_available": True,
                    "receipt_digest": receipt_finalization["receipt_digest"],
                    "store_binding": receipt_finalization["store_binding"],
                    "redaction_policy_version": receipt.get("redaction_policy_version"),
                },
                "public_receipt_claim": public_receipt_claim(
                    receipt,
                    project_root=str(workspace.project_root),
                ),
            }
        )
    if wrapper_metadata is not None:
        payload["wrapper_metadata"] = wrapper_metadata
    if args.json:
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        public_target = public_project_path(target_path, project_root=workspace.project_root)
        print(f"Committed {args.file_key} to {public_target or args.file_key}")




if __name__ == "__main__":
    main()
