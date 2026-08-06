#!/usr/bin/env python3
"""RecallLoom single write-transaction core (T050-04A preview + T050-04B/05 apply).

Frozen by the v0.5.0 unique construction plan (§7.4 callable contracts, §7.5
mutation cut points, §7.6 topology phases, §7.8 landing map rows 04–06). This
module is the single owner of the 19-value ``TransactionStage`` sequence:

    SUPPORT → INPUT → INPUT_VALIDATION → LOCK → STRICT_GATE → PREFLIGHT →
    AUTHORITY → OPERATION_PREPLAN → RECEIPT_PRECHECK → OPERATION_PLAN →
    DRY_RUN_RETURN 或 TARGET_REPLACE → TARGET_READBACK → STATE_REPLACE →
    STATE_READBACK → POST_HASH → RECEIPT_FINALIZE → FINAL_EVIDENCE → RESULT

T050-04A landed the skeleton and the managed-write PREVIEW path up to
``DRY_RUN_RETURN`` (real dry-run per R3/V050-07). T050-04B lands the
managed-write APPLY cutover: the same shared stages run first, then the
frozen commit order TARGET_REPLACE → TARGET_READBACK → STATE_REPLACE →
STATE_READBACK → POST_HASH → RECEIPT_FINALIZE → FINAL_EVIDENCE → RESULT,
all inside the one identity lock, every replace through
``core.workspace.atomic_io``, the receipt precheck snapshot reused per the
frozen count discipline, and every failure outcome decided by the §7.5 truth
table (the v0.4.8.3 baseline handlers are extracted verbatim below — same
reason codes, side effects, exit projections; D5=false everywhere in this
lane; false success and unknown-result replay are structurally impossible).
T050-05 lands the post-append summary-sync APPLY cutover: the sync lane runs
through the SAME stage chain and the same commit implementation (no second
commit), dispatched to its concrete ``preplan_post_append_sync`` /
``seal_post_append_sync_plan`` pair (§7.4's named per-operation planners —
the managed implementation with the operation pinned; the strict-gate
failure context stays the frozen shared write/managed_file_commit spelling
for both lanes).

Composition (single owners, dependency direction preserved):

- ``core.safety.input_transport`` — acquisition (legacy file/stdin, capsule
  decode, scratch READ-ONLY acquisition; claim/consume are never referenced
  in preview).
- ``core.workspace.runtime`` — the guard-serialized identity lock. The
  preview's only allowed side effect is acquiring and releasing that lock.
- ``core.provenance.evidence`` — the strict sidecar integrity gate (in-lock).
- ``core.continuity.preflight`` — the callable fresh preflight (in-lock).
- ``core.provenance.state`` — the provenance write gate (in-lock).
- ``core.provenance.bindings`` — final process-local authority, consumed once
  inside the live identity lock; persisted leases are never consulted.
- ``core.recording.managed_write`` — the pure concrete planner pairs
  (``preplan_managed_write`` / ``seal_managed_write_plan`` and the T050-05
  ``preplan_post_append_sync`` / ``seal_post_append_sync_plan``; candidate
  computed exactly once; preplan → receipt precheck → seal order).
- ``core.provenance.store`` — the READ-ONLY receipt precheck snapshot; no
  finalization ever happens in preview. Apply reuses that one snapshot for
  the strict/duplicate check, the commit CAS, and the final evidence per the
  frozen count discipline (§7.7).
- ``core.workspace.atomic_io`` — every target/state replace (apply only).
- ``core.provenance.inconsistent_review`` — the frozen verified provenance
  downgrade evidence for the post-hash cut point (apply only; D5 stays
  decided solely by that module's exact predicate).
- ``core.failure.context`` — the frozen ``OperationContext``.

Preview side-effect budget (§7.3): the preview may briefly take and release
the identity lock and may read; it MUST NOT write the target, state, receipt
store, binding/lease store, scratch manifest/content/claim state, or any
managed/derived file.

Expected user/safety failures become ``TransactionFailure`` values (never
``SystemExit``); the helper/dispatcher adapters project them onto the legacy
exit contract. Only program defects raise (``TransactionContractError``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from core.continuity import preflight as continuity_preflight
from core.continuity.daily_log import DailyLogCursorError
from core.errors import (
    ConfigContractError,
    LockBusyError,
    RecallLoomError,
    StorageResolutionError,
)
from core.failure.context import (
    COMMAND_APPEND,
    COMMAND_WRITE,
    OPERATION_DOMAIN,
    OPERATION_MANAGED_WRITE,
    OPERATION_DAILY_LOG_APPEND,
    OPERATION_POST_APPEND_SUMMARY_SYNC,
    STAGE_INPUT,
    STAGE_PREFLIGHT,
    OperationContext,
    legacy_command_for,
    legacy_operation_for,
)
from core.protocol.contracts import FILE_KEYS
from core.provenance import bindings as provenance_bindings
from core.provenance import evidence as provenance_evidence
from core.provenance import state as provenance_state
from core.provenance import store as provenance_store
from core.provenance.inconsistent_review import (
    InconsistentReviewContractError,
    build_inconsistent_review_evidence,
)
from core.recording import managed_write
from core.recording import daily_log_append
from core.safety import input_transport
from core.safety.input_transport import (
    EXPECTED_INPUT_DIGEST_MISMATCH,
    EXPECTED_INPUT_DIGEST_RE,
    REASON_BOTH_INPUT_SOURCES,
    AcquiredInput,
    InputTransportError,
)
from core.workspace import atomic_io
from core.workspace import runtime as workspace_runtime


# --- frozen stage contract -----------------------------------------------------


class TransactionStage(Enum):
    """The frozen §7.4 transaction stage enum, in mandated call order."""

    SUPPORT = "SUPPORT"
    INPUT = "INPUT"
    INPUT_VALIDATION = "INPUT_VALIDATION"
    LOCK = "LOCK"
    STRICT_GATE = "STRICT_GATE"
    PREFLIGHT = "PREFLIGHT"
    AUTHORITY = "AUTHORITY"
    OPERATION_PREPLAN = "OPERATION_PREPLAN"
    RECEIPT_PRECHECK = "RECEIPT_PRECHECK"
    OPERATION_PLAN = "OPERATION_PLAN"
    DRY_RUN_RETURN = "DRY_RUN_RETURN"
    TARGET_REPLACE = "TARGET_REPLACE"
    TARGET_READBACK = "TARGET_READBACK"
    STATE_REPLACE = "STATE_REPLACE"
    STATE_READBACK = "STATE_READBACK"
    POST_HASH = "POST_HASH"
    RECEIPT_FINALIZE = "RECEIPT_FINALIZE"
    FINAL_EVIDENCE = "FINAL_EVIDENCE"
    RESULT = "RESULT"


# The mandated call order: preview stages up to DRY_RUN_RETURN, then the
# apply-only mutation stages (T050-04B+), then RESULT.
TRANSACTION_STAGE_ORDER: tuple[TransactionStage, ...] = tuple(TransactionStage)
PREVIEW_STAGE_ORDER: tuple[TransactionStage, ...] = TRANSACTION_STAGE_ORDER[
    : TRANSACTION_STAGE_ORDER.index(TransactionStage.DRY_RUN_RETURN) + 1
]
APPLY_MUTATION_STAGES: tuple[TransactionStage, ...] = TRANSACTION_STAGE_ORDER[
    TRANSACTION_STAGE_ORDER.index(TransactionStage.TARGET_REPLACE) :
]

MODE_PREVIEW = "preview"
MODE_APPLY = "apply"
TRANSACTION_MODE_DOMAIN = frozenset((MODE_PREVIEW, MODE_APPLY))

# §7.3: a failed preview lock release is a typed failure with inspect-only action.
REASON_LOCK_RELEASE_FAILED = "lock_release_failed"
# New typed reason for PreviewBinding drift (no v0.4.8.3 precedent: the
# preview binding itself is new in v0.5.0). Apply-side drift continues to use
# the frozen stale_write_context vocabulary.
REASON_PREVIEW_BINDING_DRIFT = "preview_binding_drift_detected"

_HELPER_OWNER = "commit_context_file.py"
_RECEIPT_STORE_FILE = "derived/helper-receipts.json"

# Frozen v0.4.8.3 review-imported-baseline confirmation vocabulary (enforced
# in apply only; since T050-05 this is the only remaining copy on the helper
# path — single-owner consolidation stays registered for T050-09A).
REVIEW_IMPORTED_BASELINE_CONFIRMATION = "review_imported_baseline_confirmed"

# The frozen readiness-label set (provenance vocabulary; the helper's mirror
# copy was deleted with the legacy sync lane in T050-05 — single-owner
# consolidation stays registered for T050-09A).
PREFLIGHT_WRITE_READINESS_LABELS = frozenset(
    (
        "structural_only_ready_after_preflight",
        "helper_evidenced_ready_after_preflight",
        "review_imported_baseline_ready_after_preflight",
    )
)


def _legacy_helper_name_for(operation: str) -> str:
    """Return the adapter name retained in legacy failure projections."""
    if operation == OPERATION_DAILY_LOG_APPEND:
        return "append_daily_log_entry.py"
    return _HELPER_OWNER


def _legacy_write_gate_message(operation: str, reason_code: object) -> str:
    """Project authority denials onto the operation's frozen CLI wording."""
    if operation == OPERATION_DAILY_LOG_APPEND:
        return (
            "Refusing to append because this RecallLoom sidecar requires provenance review "
            "or a fresh preflight binding before any mutating helper write "
            f"(reason_code: {reason_code})."
        )
    return (
        "Refusing to commit because this RecallLoom sidecar requires provenance review "
        "or a fresh preflight binding before a mutating helper write "
        f"(reason_code: {reason_code})."
    )


class TransactionContractError(RuntimeError):
    """A program defect in transaction wiring (never a user/safety failure)."""


class TransactionStageError(Exception):
    """Typed stage failure carrying the exact legacy exit projection.

    Mirrors the owner-error pattern (``InputTransportError`` /
    ``ManagedWritePlannerError``): the helper adapter projects ``message`` /
    ``reason`` / ``exit_code`` / ``details`` (+ ``findings`` / ``extra``)
    through ``exit_with_failure_contract``. ``stage`` / ``reason_code`` /
    ``side_effect`` / ``trust_effect`` are carried explicitly so no layer ever
    drops the typed failure context (§7.1 rule 6).
    """

    def __init__(
        self,
        *,
        message: str,
        reason: str,
        exit_code: int,
        stage: TransactionStage,
        reason_code: Optional[str] = None,
        side_effect: str = "none",
        trust_effect: str = "none",
        details: Optional[dict] = None,
        findings: Optional[list] = None,
        extra: Optional[dict] = None,
    ) -> None:
        self.message = message
        self.reason = reason
        self.exit_code = exit_code
        self.stage = stage
        self.reason_code = reason_code
        self.side_effect = side_effect
        self.trust_effect = trust_effect
        self.details = details
        self.findings = findings
        self.extra = extra
        super().__init__(message)


# --- frozen data contracts (§7.4) ----------------------------------------------


@dataclass(frozen=True)
class PreviewBinding:
    """Frozen §7.4 preview binding: exact-match drift gate for later mutation."""

    input_digest: str
    target_identity: Optional[str]
    expected_file_revision: int
    expected_workspace_revision: int
    expected_cursor: Any


@dataclass(frozen=True)
class TransactionRequest:
    """Frozen §7.4 request; no CLI dict may pass through into core."""

    context: OperationContext
    mode: str
    workspace_root: Path
    storage_root: Path
    acquired_input: AcquiredInput
    file_key: str
    write_type: Optional[str]
    expected_file_revision: int
    expected_workspace_revision: int
    expected_cursor: Any
    expected_input_digest: Optional[str]
    preview_binding: Optional[PreviewBinding]
    confirmation: Optional[str]
    writer_attribution: Any

    def __post_init__(self) -> None:
        if not isinstance(self.context, OperationContext):
            raise TypeError("context must be an OperationContext")
        if self.mode not in TRANSACTION_MODE_DOMAIN:
            raise ValueError(f"unknown transaction mode: {self.mode!r}")
        if not isinstance(self.acquired_input, AcquiredInput):
            raise TypeError("acquired_input must be an AcquiredInput")
        for field_name in ("workspace_root", "storage_root"):
            value = getattr(self, field_name)
            if not isinstance(value, Path):
                raise TypeError(f"{field_name} must be a pathlib.Path")
        for field_name in ("expected_file_revision", "expected_workspace_revision"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name} must be an int")
        if self.expected_input_digest is not None and not isinstance(
            self.expected_input_digest, str
        ):
            raise TypeError("expected_input_digest must be a string or None")
        if self.preview_binding is not None and not isinstance(
            self.preview_binding, PreviewBinding
        ):
            raise TypeError("preview_binding must be a PreviewBinding or None")
        if self.acquired_input.role != self.context.operation:
            raise ValueError(
                "acquired input role does not match the request operation"
            )


@dataclass(frozen=True)
class TransactionResult:
    """Frozen §7.4 success-only result; adapters project it into output."""

    ok: bool
    operation: str
    final_stage: str
    result_code: str
    revisions: dict
    receipt_evidence_ref: Optional[str]
    input_digest: Optional[str]
    target_digest: Optional[str]
    single_next_action: dict


@dataclass(frozen=True)
class TransactionFailure:
    """Frozen §7.4 failure-only result (v0.4.8.3 reason/side-effect vocabulary).

    ``cause`` is NOT part of the frozen public contract: it carries the
    originating typed owner/stage error so the legacy adapter can render the
    byte-identical exit projection. The compact adapter (T050-08C) consumes
    only the frozen fields.
    """

    ok: bool
    operation: str
    stage: str
    reason_code: Optional[str]
    reason_family: str
    side_effect: str
    trust_effect: str
    safe_to_retry: bool
    revisions: dict
    receipt_evidence_ref: Optional[str]
    scratch_disposition: Optional[str]
    single_next_action: dict
    cause: Optional[BaseException]


# --- INPUT stage seam (input_transport composition) -----------------------------


def acquire_preview_input(
    *,
    operation: str,
    source_file: Optional[str] = None,
    use_stdin: bool = False,
    input_capsule: Optional[str] = None,
    prepared_input_ref: Optional[str] = None,
    max_input_bytes: int,
    file_key: Optional[str] = None,
    write_type: Optional[str] = None,
    project_root: Optional[Path] = None,
    storage_root: Optional[Path] = None,
    expected_input_digest: Optional[str] = None,
    entry_json: Optional[str] = None,
    entry_file: Optional[str] = None,
) -> AcquiredInput:
    """The transaction's INPUT stage acquisition seam (shared preview/apply).

    Exactly one mode must be selected: legacy ``--source-file`` / ``--stdin``,
    ASCII capsule decode, or scratch READ-ONLY acquisition (the only preview
    form; claim/consume are strictly absent here and stay apply-only). The
    S3–S5 helper parser currently exposes only the legacy modes, so scratch
    claim/consume has no reachable caller in this packet's lane. Every
    rejection is the typed ``InputTransportError`` from the single owner.
    """
    selected = sum(
        1
        for present in (
            bool(source_file) or use_stdin,
            input_capsule is not None,
            prepared_input_ref is not None,
        )
        if present
    )
    if selected > 1:
        raise InputTransportError(
            message=(
                "Use exactly one prepared-content input: --source-file or --stdin, "
                "--input-capsule, or --prepared-input-ref."
            ),
            details={
                **OperationContext(
                    command=legacy_command_for(operation),
                    operation=operation,
                    write_type=write_type,
                    input_mode=None,
                    stage=STAGE_INPUT,
                ).legacy_details_fields(),
                "input_contract": "legacy_xor_capsule_xor_prepared_input_ref",
                "side_effect": "none",
                "reason_code": REASON_BOTH_INPUT_SOURCES,
            },
        )
    if input_capsule is not None:
        return input_transport.decode_input_capsule(
            input_capsule,
            expected_role=operation,
            expected_digest=expected_input_digest,
        )
    if prepared_input_ref is not None:
        return input_transport.acquire_prepared_input_scratch(
            ref=prepared_input_ref,
            expected_role=operation,
            expected_digest=expected_input_digest,
        )
    if operation == OPERATION_DAILY_LOG_APPEND:
        return input_transport.acquire_daily_log_entry_input(
            entry_json=entry_json, entry_file=entry_file, use_stdin=use_stdin,
            max_input_bytes=max_input_bytes, project_root=project_root,
            storage_root=storage_root,
        ).acquired
    return input_transport.acquire_managed_write_input(
        source_file=source_file,
        use_stdin=use_stdin,
        max_input_bytes=max_input_bytes,
        file_key=file_key,
        write_type=write_type,
        project_root=project_root,
        storage_root=storage_root,
        role=operation,
    )


# --- failure mapping ------------------------------------------------------------


_REASON_FAMILY_BY_STAGE = {
    TransactionStage.SUPPORT: "support",
    TransactionStage.INPUT: "input",
    TransactionStage.INPUT_VALIDATION: "input",
    TransactionStage.LOCK: "lock",
    TransactionStage.STRICT_GATE: "preflight",
    TransactionStage.PREFLIGHT: "preflight",
    TransactionStage.AUTHORITY: "preflight",
    TransactionStage.OPERATION_PREPLAN: "input",
    TransactionStage.RECEIPT_PRECHECK: "receipt",
    TransactionStage.OPERATION_PLAN: "input",
    TransactionStage.DRY_RUN_RETURN: "preflight",
    TransactionStage.TARGET_REPLACE: "target",
    TransactionStage.TARGET_READBACK: "target",
    TransactionStage.STATE_REPLACE: "state",
    TransactionStage.STATE_READBACK: "state",
    TransactionStage.POST_HASH: "post_hash",
    TransactionStage.RECEIPT_FINALIZE: "receipt",
    TransactionStage.FINAL_EVIDENCE: "final_evidence",
}

# §7.9 projection priority: existing privacy/security reasons → privacy;
# strict/provenance/history evidence inconsistency → tamper; otherwise the
# TransactionStage mapping above. T050-08C owns the final compact projector.
_PRIVACY_REASONS = frozenset(
    (
        "privacy_security_failure",
        "attach_scan_blocked",
        "reserved_marker_injection",
        "prohibited_field",
        "value_requires_redaction",
        "non_string_key",
        "unsupported_value_type",
    )
)
_TAMPER_REASON_CODES = frozenset(
    (
        "strict_sidecar_integrity_failed",
        "provenance_evidence_inconsistent",
    )
)

# §7.5: identity-drift failures never suggest a blind retry — the presented
# input/binding/assertions are no longer exact, so the only safe next step is
# a fresh read-only check (re-preview), not a retried mutation attempt.
_NO_RETRY_REASON_CODES = frozenset(
    (
        "preview_binding_drift_detected",
        "concurrent_external_modification_detected",
    )
)


def _reason_family(stage: TransactionStage, *, reason: Optional[str], reason_code: Optional[str]) -> str:
    if reason in _PRIVACY_REASONS or reason_code in _PRIVACY_REASONS:
        return "privacy"
    if reason_code in _TAMPER_REASON_CODES:
        return "tamper"
    return _REASON_FAMILY_BY_STAGE.get(stage, "internal")


def _cause_fields(cause: BaseException) -> tuple[Optional[str], str, str, Optional[str]]:
    """Extract (reason_code, side_effect, trust_effect, reason) from a typed cause."""
    reason = getattr(cause, "reason", None)
    reason_code = getattr(cause, "reason_code", None)
    details = getattr(cause, "details", None)
    if reason_code is None and isinstance(details, dict):
        value = details.get("reason_code")
        reason_code = value if isinstance(value, str) else None
    side_effect = getattr(cause, "side_effect", None)
    if not isinstance(side_effect, str):
        side_effect = details.get("side_effect") if isinstance(details, dict) else None
    trust_effect = getattr(cause, "trust_effect", None)
    if not isinstance(trust_effect, str):
        trust_effect = details.get("trust_effect") if isinstance(details, dict) else None
    return (
        reason_code if isinstance(reason_code, str) else None,
        side_effect if isinstance(side_effect, str) else "none",
        trust_effect if isinstance(trust_effect, str) else "none",
        reason if isinstance(reason, str) else None,
    )


def _failure(
    context: OperationContext,
    stage: TransactionStage,
    cause: BaseException,
    *,
    safe_to_retry: Optional[bool] = None,
) -> TransactionFailure:
    """Form the frozen TransactionFailure for an expected stage failure."""
    reason_code, side_effect, trust_effect, reason = _cause_fields(cause)
    family = _reason_family(stage, reason=reason, reason_code=reason_code)
    if safe_to_retry is None:
        # §7.5: only pre-mutation, no-side-effect, non-privacy/tamper failures
        # may suggest a retry; preview never mutates, so side_effect stays
        # "none" for every well-formed preview failure. Identity-drift
        # failures are excluded: the presented assertions are no longer exact.
        safe_to_retry = (
            side_effect == "none"
            and family not in {"privacy", "tamper"}
            and reason_code not in _NO_RETRY_REASON_CODES
        )
    if safe_to_retry:
        next_action: dict = {"kind": "none"}
    else:
        # blocked/unsafe/privacy/tamper/partial/unknown → read-only inspect only.
        next_action = {
            "kind": "inspect",
            "value": (
                "Inspect the workspace and this failure's typed details with a "
                "read-only RecallLoom status/validate command before any further write."
            ),
        }
    return TransactionFailure(
        ok=False,
        operation=context.operation,
        stage=stage.value,
        reason_code=reason_code,
        reason_family=family,
        side_effect=side_effect,
        trust_effect=trust_effect,
        safe_to_retry=safe_to_retry,
        revisions={},
        receipt_evidence_ref=None,
        scratch_disposition=None,
        single_next_action=next_action,
        cause=cause,
    )


# --- preview orchestration -------------------------------------------------------


def run_transaction(
    request: TransactionRequest,
    *,
    support: Mapping[str, Any],
    preflight_binding: Optional[Mapping[str, Any]] = None,
    authority: Any = None,
    package_version: str,
    input_format: str = "markdown",
    source_file: Optional[str] = None,
) -> Any:
    """Run one transaction for ``request`` (preview or apply).

    Both the managed-write lane and (since T050-05) the post-append
    summary-sync lane run through this single stage chain; the operation
    selects only its concrete pure planner pair, never a second commit.

    ``support`` is the caller-evaluated package-support gate result (the
    dispatcher owns evaluation in every topology; the stage enforces it in
    order). ``preflight_binding`` is the S3–S5 dispatcher-issued preflight
    binding AFTER the adapter's exact normalization — never a raw CLI payload —
    and is deleted with the phase at T050-07C. ``package_version`` seeds the
    receipt-seed inputs (the planner never reads package metadata itself).

    Preview returns ``(TransactionResult, PreviewBinding)``; apply returns
    ``(TransactionResult, receipt_finalization)`` where the second element is
    a public-safe receipt finalization mapping (the adapter's output-projection
    input, not a second result object). Every expected user/safety
    failure is a ``TransactionFailure``. Program defects raise
    ``TransactionContractError``.
    """
    if authority is None:
        return _preflight_binding_failure(
            request.context, TransactionStage.AUTHORITY,
            message="Transaction requires dispatcher-issued in-process authority.",
            reason_code="invalid_transaction_authority", field_path="$.authority",
            extra={"side_effect": "none"},
        )
    try:
        if request.mode == MODE_PREVIEW:
            return _run_preview(
                request,
                support=support,
                preflight_binding=preflight_binding,
                authority=authority,
                package_version=package_version,
                input_format=input_format,
                source_file=source_file,
            )
        if request.mode == MODE_APPLY:
            return _run_apply(
                request,
                support=support,
                preflight_binding=preflight_binding,
                authority=authority,
                package_version=package_version,
                input_format=input_format,
                source_file=source_file,
            )
        raise TransactionContractError(f"unknown transaction mode: {request.mode!r}")
    finally:
        if authority is not None:
            provenance_bindings.discard_transaction_authority(authority)


def _run_preview(
    request: TransactionRequest,
    *,
    support: Mapping[str, Any],
    preflight_binding: Optional[Mapping[str, Any]],
    authority: Any,
    package_version: str,
    input_format: str,
    source_file: Optional[str],
) -> Any:
    """The preview entry (T050-04A): stages up to DRY_RUN_RETURN (both lanes)."""
    return _run_transaction(
        request,
        MODE_PREVIEW,
        support=support,
        preflight_binding=preflight_binding,
        authority=authority,
        package_version=package_version,
        input_format=input_format,
        source_file=source_file,
    )


def _run_apply(
    request: TransactionRequest,
    *,
    support: Mapping[str, Any],
    preflight_binding: Optional[Mapping[str, Any]],
    authority: Any,
    package_version: str,
    input_format: str,
    source_file: Optional[str],
) -> Any:
    """The apply entry (T050-04B managed write, T050-05 sync): the full chain."""
    return _run_transaction(
        request,
        MODE_APPLY,
        support=support,
        preflight_binding=preflight_binding,
        authority=authority,
        package_version=package_version,
        input_format=input_format,
        source_file=source_file,
    )


def _run_transaction(
    request: TransactionRequest,
    mode: str,
    *,
    support: Mapping[str, Any],
    preflight_binding: Optional[Mapping[str, Any]],
    authority: Any,
    package_version: str,
    input_format: str,
    source_file: Optional[str],
) -> Any:
    context = request.context
    operation = context.operation
    acquired = request.acquired_input

    # SUPPORT — enforce the caller-evaluated support gate in stage order.
    if not isinstance(support, Mapping) or support.get("allowed") is not True:
        # ``enforce_package_support_gate`` returns the support mapping itself,
        # not a wrapper containing ``package_support``.  Accept the nested
        # form as well for callers that already projected it, but never turn a
        # real support refusal into an anonymous generic one.
        public_support = (
            support.get("package_support", support)
            if isinstance(support, Mapping)
            else None
        )
        message = (
            public_support.get("user_message")
            if isinstance(public_support, Mapping)
            and isinstance(public_support.get("user_message"), str)
            else "RecallLoom package support gate blocked this action."
        )
        return _failure(
            context,
            TransactionStage.SUPPORT,
            TransactionStageError(
                message=message,
                reason="package_support_blocked",
                exit_code=4,
                stage=TransactionStage.SUPPORT,
                reason_code=(
                    public_support.get("reason_code")
                    if isinstance(public_support, Mapping)
                    else None
                ),
                details={
                    "operation": "package_support_gate",
                    "reason_code": (
                        public_support.get("reason_code")
                        if isinstance(public_support, Mapping)
                        else None
                    ),
                    "side_effect": "none",
                    "package_support": public_support,
                },
                extra={"package_support": public_support},
            ),
            safe_to_retry=False,
        )

    # INPUT boundary — acquisition already ran through the INPUT-stage seam;
    # the request contract guarantees an AcquiredInput bound to this operation.
    if not isinstance(acquired, AcquiredInput) or acquired.role != operation:
        raise TransactionContractError(
            "transaction requires an AcquiredInput bound to the request operation"
        )

    # INPUT_VALIDATION — the optional --expected-input-digest preview binding
    # and target resolution (mirrors the legacy pre-lock target existence check).
    if request.expected_input_digest is not None:
        if not EXPECTED_INPUT_DIGEST_RE.match(request.expected_input_digest):
            return _failure(
                context,
                TransactionStage.INPUT_VALIDATION,
                TransactionStageError(
                    message="--expected-input-digest must be a 64-character lowercase SHA-256 hex digest.",
                    reason="invalid_prepared_input",
                    exit_code=2,
                    stage=TransactionStage.INPUT_VALIDATION,
                    reason_code=input_transport.REASON_INVALID_EXPECTED_INPUT_DIGEST,
                    details={
                        **context.legacy_details_fields(),
                        "reason_code": input_transport.REASON_INVALID_EXPECTED_INPUT_DIGEST,
                        "side_effect": "none",
                    },
                ),
            )
        if request.expected_input_digest != acquired.received_raw_sha256:
            return _failure(
                context,
                TransactionStage.INPUT_VALIDATION,
                TransactionStageError(
                    message="Prepared input digest does not equal --expected-input-digest.",
                    reason="invalid_prepared_input",
                    exit_code=2,
                    stage=TransactionStage.INPUT_VALIDATION,
                    reason_code=EXPECTED_INPUT_DIGEST_MISMATCH,
                    details={
                        **context.legacy_details_fields(),
                        "reason_code": EXPECTED_INPUT_DIGEST_MISMATCH,
                        "side_effect": "none",
                    },
                ),
            )
    if operation == OPERATION_DAILY_LOG_APPEND:
        if not isinstance(request.expected_cursor, Mapping) or not isinstance(request.expected_cursor.get("target_path"), str):
            raise TransactionContractError("daily_log_append requires an adapter-resolved target_path cursor input")
        target_path = Path(request.expected_cursor["target_path"])
    else:
        target_path = Path(request.storage_root) / FILE_KEYS[request.file_key]
    if operation != OPERATION_DAILY_LOG_APPEND and not target_path.is_file():
        return _failure(
            context,
            TransactionStage.INPUT_VALIDATION,
            TransactionStageError(
                message=f"Missing target file: {target_path}",
                reason="malformed_managed_file",
                exit_code=2,
                stage=TransactionStage.INPUT_VALIDATION,
                details={"path": str(target_path)},
            ),
        )
    state_path = Path(request.storage_root) / FILE_KEYS["state"]

    # LOCK — the preview's only allowed side effect: briefly take and release
    # the guard-serialized identity lock.
    try:
        handle = workspace_runtime._acquire_workspace_lock(
            Path(request.workspace_root), _HELPER_OWNER
        )
    except LockBusyError as exc:
        return _failure(
            context,
            TransactionStage.LOCK,
            TransactionStageError(
                message=str(exc),
                reason="write_lock_busy",
                exit_code=3,
                stage=TransactionStage.LOCK,
            ),
        )

    stage = TransactionStage.STRICT_GATE

    def _stages() -> Any:
        nonlocal authority
        nonlocal stage

        # STRICT_GATE — the shared strict sidecar integrity gate, re-evaluated
        # fresh inside the identity lock.
        strict_gate = provenance_evidence.strict_sidecar_integrity_gate(
            project_root=request.workspace_root,
            storage_root=request.storage_root,
        )
        if strict_gate.get("allowed_for_mutation") is not True:
            gate_summary = provenance_evidence.strict_sidecar_integrity_gate_public_summary(
                strict_gate
            )
            return _failure(
                context,
                stage,
                TransactionStageError(
                    message=(
                        "Strict sidecar integrity gate blocked this daily-log append. "
                        "Review or repair sidecar evidence before writing."
                        if operation == OPERATION_DAILY_LOG_APPEND
                        else "Strict sidecar integrity gate blocked this managed-file commit. "
                        "Review or repair sidecar evidence before writing."
                    ),
                    reason="trust_review_required",
                    exit_code=3,
                    stage=stage,
                    reason_code="strict_sidecar_integrity_failed",
                    trust_effect="review_required",
                    details={
                        "reason_code": "strict_sidecar_integrity_failed",
                        "strict_gate_reason_code": gate_summary.get("reason_code"),
                        "strict_sidecar_integrity_gate": gate_summary,
                        **OperationContext(
                            command=(COMMAND_APPEND if operation == OPERATION_DAILY_LOG_APPEND else COMMAND_WRITE),
                            operation=(OPERATION_DAILY_LOG_APPEND if operation == OPERATION_DAILY_LOG_APPEND else OPERATION_MANAGED_WRITE),
                            write_type=None,
                            input_mode=None,
                            stage=STAGE_PREFLIGHT,
                        ).legacy_details_fields(),
                        **(
                            {}
                            if operation == OPERATION_DAILY_LOG_APPEND
                            else {"file_key": request.file_key}
                        ),
                        "side_effect": "none",
                    },
                    extra=provenance_evidence.strict_sidecar_no_write_failure_extra_from_summary(
                        gate_summary,
                        continuity_confidence="broken",
                        include_recovery_actions=False,
                    ),
                ),
                safe_to_retry=False,
            )
        receipt_store_snapshot = strict_gate.get("_receipt_store_snapshot")

        # PREFLIGHT — state stability confirmation and the target read, inside
        # the lock. The fresh callable preflight is the preview's transitional
        # lease seed; apply uses the persisted dispatcher lease instead and
        # never re-evaluates preflight (v0.4.8.3 apply parity).
        stage = TransactionStage.PREFLIGHT
        state = workspace_runtime.load_workspace_state(state_path)
        current_state_text = workspace_runtime.read_text(state_path)
        confirmed_state = workspace_runtime.load_workspace_state(state_path)
        if (
            confirmed_state != state
            or workspace_runtime.read_text(state_path) != current_state_text
        ):
            return _concurrent_external_failure(
                context, stage, request, "external_state_modification_preserved"
            )
        try:
            current_target_text = workspace_runtime.read_text(target_path)
            target_stat = os.lstat(target_path)
            target_identity = (f"{target_stat.st_dev}:{target_stat.st_ino}:{target_stat.st_size}:" + hashlib.sha256(current_target_text.encode("utf-8")).hexdigest())
        except FileNotFoundError:
            if operation != OPERATION_DAILY_LOG_APPEND:
                raise
            current_target_text = None
            target_identity = None
        workspace = workspace_runtime.find_recallloom_root(request.workspace_root)
        if workspace is None:
            raise TransactionContractError(
                "workspace detached between adapter resolution and in-lock preflight"
            )
        fresh_preflight = (
            continuity_preflight.evaluate_preflight(workspace=workspace, full=False)
            if mode == MODE_PREVIEW
            else None
        )

        # AUTHORITY — provenance write gate, then the S3–S5 transitional
        # authority from the exactly validated dispatcher binding + live lock
        # handle: an in-memory lease record for preview (§7.3 forbids lease
        # persistence in dry-run), the store-backed persisted lease for apply.
        stage = TransactionStage.AUTHORITY
        receipts_verified = provenance_evidence.strict_gate_current_receipts_verified(
            strict_gate
        )
        write_gate = provenance_state.helper_write_gate_from_state(
            state,
            helper_name=_legacy_helper_name_for(operation),
            operation_class=legacy_operation_for(operation),
            preflight_binding_present=preflight_binding is not None,
            require_preflight_for_review_imported_baseline=True,
            receipt_chain_verified=receipts_verified,
            receipt_store_available=receipts_verified,
        )
        if preflight_binding is not None:
            binding_state = preflight_binding.get("provenance_state")
            if isinstance(binding_state, str) and binding_state != write_gate["provenance_state"]:
                return _preflight_binding_failure(
                    context,
                    stage,
                    message="Preflight binding provenance_state does not match current sidecar provenance.",
                    reason_code="preflight_binding_provenance_state_mismatch",
                    field_path="$.provenance_state",
                    extra={
                        "current_provenance_state": write_gate["provenance_state"],
                        "binding_provenance_state": binding_state,
                    },
                )
            readiness_label = preflight_binding.get("write_readiness_label")
            if readiness_label not in PREFLIGHT_WRITE_READINESS_LABELS:
                return _preflight_binding_failure(
                    context,
                    stage,
                    message="Preflight binding does not authorize a revision-checked helper write.",
                    reason_code="preflight_binding_write_readiness_not_authorized",
                    field_path="$.write_readiness_label",
                    extra={
                        "write_readiness_label": readiness_label,
                        "allowed_write_readiness_labels": sorted(
                            PREFLIGHT_WRITE_READINESS_LABELS
                        ),
                    },
                )
            # The review_imported_baseline UX confirmation trio is a mutation
            # authorization, not a validation: the dispatcher's dry-run never
            # demands it, and a preview must not. It stays enforced in apply.
            if mode == MODE_APPLY and write_gate["provenance_state"] == "review_imported_baseline":
                if preflight_binding.get("ux_gate") != "ask":
                    return _preflight_binding_failure(
                        context,
                        stage,
                        message="Review-imported baseline writes require an ask UX gate.",
                        reason_code="preflight_binding_ux_gate_mismatch",
                        field_path="$.ux_gate",
                        extra={"required_ux_gate": "ask"},
                    )
                if preflight_binding.get("ux_gate_requires_confirmation") is not True:
                    return _preflight_binding_failure(
                        context,
                        stage,
                        message="Review-imported baseline writes require explicit confirmation.",
                        reason_code="preflight_binding_confirmation_required",
                        field_path="$.ux_gate_requires_confirmation",
                    )
                if (
                    preflight_binding.get("ux_gate_confirmation")
                    != REVIEW_IMPORTED_BASELINE_CONFIRMATION
                ):
                    return _preflight_binding_failure(
                        context,
                        stage,
                        message="Review-imported baseline write confirmation is missing.",
                        reason_code="preflight_binding_confirmation_missing",
                        field_path="$.ux_gate_confirmation",
                        extra={
                            "required_confirmation": REVIEW_IMPORTED_BASELINE_CONFIRMATION
                        },
                    )
        if not write_gate["allowed"]:
            return _failure(
                context,
                stage,
                TransactionStageError(
                    message=_legacy_write_gate_message(
                        operation, write_gate["blocked_reason_code"]
                    ),
                    reason=provenance_state.helper_write_gate_failure_reason(write_gate),
                    exit_code=3,
                    stage=stage,
                    reason_code=str(write_gate["blocked_reason_code"]),
                    trust_effect="review_required",
                    details={
                        "reason_code": write_gate["blocked_reason_code"],
                        "helper_name": write_gate["helper_name"],
                        "operation_class": write_gate["operation_class"],
                        "provenance_state": write_gate["provenance_state"],
                        "provenance_metadata_status": write_gate["provenance_metadata_status"],
                        "write_readiness": write_gate["write_readiness"],
                        "preflight_binding_present": write_gate["preflight_binding_present"],
                        "side_effect": "none",
                    },
                ),
                safe_to_retry=False,
            )
        if preflight_binding is None:
            raise TransactionContractError(
                "transaction authority requires the validated dispatcher binding"
            )
        try:
            provenance_bindings.consume_transaction_authority(
                authority, operation=operation, workspace_root=request.workspace_root,
                lock_handle=handle,
            )
        except provenance_bindings.TransactionAuthorityError as exc:
            return _preflight_binding_failure(
                context, stage, message=str(exc), reason_code=exc.reason_code,
                field_path=exc.field_path, extra={"side_effect": "none"},
            )

        # OPERATION_PREPLAN — pure candidate render/revision/cursor + precheck
        # inputs, in the legacy validation order. Candidate computed exactly once.
        # §7.4: each operation names its concrete planner (no generic callback
        # protocol); the sync lane reuses the same managed implementation
        # through its own pinned pair (T050-05 — no second commit).
        stage = TransactionStage.OPERATION_PREPLAN
        timestamp = workspace_runtime.now_iso_timestamp()
        writer_id = str(getattr(request.writer_attribution, "writer_id"))
        if operation == OPERATION_DAILY_LOG_APPEND:
            # The adapter supplies routing facts only to locate the append
            # target.  The frozen cursor contract is deliberately stripped
            # before the pure planner and preview binding see it.
            target_is_latest_after_write = bool(
                request.expected_cursor.get("target_is_latest_after_write", True)
            )
            cursor_input = {
                key: request.expected_cursor.get(key)
                for key in ("latest_file", "latest_entry_id", "latest_entry_seq", "entry_count")
            }
            preplan = daily_log_append.preplan_daily_log_append(
                acquired, target_path=target_path, state_path=state_path,
                current_target_text=current_target_text, current_target_identity=target_identity, state=state,
                expected_file_revision=request.expected_file_revision, expected_workspace_revision=request.expected_workspace_revision,
                expected_cursor=cursor_input,
                expected_language=workspace.workspace_language, writer_id=writer_id, timestamp=timestamp,
                input_format=input_format, preflight_binding=preflight_binding,
                target_is_latest_after_write=target_is_latest_after_write,
                source_file=source_file, project_root=Path(request.workspace_root),
            )
        elif operation == OPERATION_POST_APPEND_SUMMARY_SYNC:
            preplan = managed_write.preplan_post_append_sync(
                acquired,
                file_key=request.file_key,
                input_format=input_format,
                expected_language=workspace.workspace_language,
                target_path=target_path,
                state_path=state_path,
                current_target_text=current_target_text,
                current_target_identity=target_identity,
                state=state,
                expected_file_revision=request.expected_file_revision,
                expected_workspace_revision=request.expected_workspace_revision,
                writer_id=writer_id,
                timestamp=timestamp,
                today=date.today().isoformat(),
                preflight_binding=preflight_binding,
                source_file=source_file,
                project_root=Path(request.workspace_root),
            )
        elif operation == OPERATION_MANAGED_WRITE:
            preplan = managed_write.preplan_managed_write(
                acquired,
                operation=operation,
                file_key=request.file_key,
                input_format=input_format,
                expected_language=workspace.workspace_language,
                target_path=target_path,
                state_path=state_path,
                current_target_text=current_target_text,
                current_target_identity=target_identity,
                state=state,
                expected_file_revision=request.expected_file_revision,
                expected_workspace_revision=request.expected_workspace_revision,
                writer_id=writer_id,
                timestamp=timestamp,
                today=date.today().isoformat(),
                preflight_binding=preflight_binding,
                source_file=source_file,
                project_root=Path(request.workspace_root),
            )
        else:
            raise TransactionContractError(
                f"no concrete operation preplan for operation: {operation!r}"
            )

        # RECEIPT_PRECHECK — READ-ONLY store snapshot against the preplan's
        # precheck inputs; no finalization, no writes of any kind.
        stage = TransactionStage.RECEIPT_PRECHECK
        if (
            preplan.receipt_precheck_inputs["preflight_binding_present"]
            and not isinstance(receipt_store_snapshot, provenance_store.ReceiptStoreSnapshot)
        ):
            try:
                receipt_store_snapshot = provenance_store.capture_receipt_store_snapshot(
                    storage_root=request.storage_root,
                    project_root=request.workspace_root,
                )
            except provenance_store.ReceiptStoreError as exc:
                return _receipt_precheck_failure(context, stage, exc, preplan)
        if not preplan.receipt_precheck_inputs["preflight_binding_present"]:
            receipt_store_snapshot = None

        # OPERATION_PLAN — pure final validation/seal; candidate bytes sealed
        # verbatim (no re-read, no re-canonicalization, no recomputation).
        # Same concrete per-operation dispatch as the preplan (§7.4).
        stage = TransactionStage.OPERATION_PLAN
        if operation == OPERATION_DAILY_LOG_APPEND:
            plan = daily_log_append.seal_daily_log_append_plan(
                preplan, current_target_text=current_target_text, current_state_text=current_state_text, state=state,
                previous_state_label=str(write_gate["provenance_state"]), preflight_binding=preflight_binding,
                timestamp=timestamp, helper_version=package_version,
            )
        elif operation == OPERATION_POST_APPEND_SUMMARY_SYNC:
            plan = managed_write.seal_post_append_sync_plan(
                preplan,
                current_target_text=current_target_text,
                current_state_text=current_state_text,
                state=state,
                writer_id=writer_id,
                timestamp=timestamp,
                previous_state_label=str(write_gate["provenance_state"]),
                preflight_binding=preflight_binding,
                helper_version=package_version,
            )
        elif operation == OPERATION_MANAGED_WRITE:
            plan = managed_write.seal_managed_write_plan(
                preplan,
                current_target_text=current_target_text,
                current_state_text=current_state_text,
                state=state,
                writer_id=writer_id,
                timestamp=timestamp,
                previous_state_label=str(write_gate["provenance_state"]),
                preflight_binding=preflight_binding,
                helper_version=package_version,
            )
        else:
            raise TransactionContractError(
                f"no concrete operation plan seal for operation: {operation!r}"
            )

        # DRY_RUN_RETURN — build the PreviewBinding and enforce exact-match
        # when a later mutation attempt presented one.
        stage = TransactionStage.DRY_RUN_RETURN
        preview_binding = PreviewBinding(
            input_digest=acquired.received_raw_sha256,
            target_identity=preplan.target_identity,
            expected_file_revision=preplan.expected_file_revision,
            expected_workspace_revision=preplan.expected_workspace_revision,
            expected_cursor=preplan.expected_cursor,
        )
        if request.preview_binding is not None:
            # §7.4: an apply attempt presenting a preview binding is an
            # exact-match gate at the fork — any drift is a no-write
            # stale_write_context block before any mutation.
            drifted_fields = _preview_binding_drift(request.preview_binding, preview_binding)
            if drifted_fields:
                return _failure(
                    context,
                    stage,
                    TransactionStageError(
                        message=(
                            "The preview binding no longer matches the current workspace "
                            "or input; rerun the preview before writing."
                        ),
                        reason="stale_write_context",
                        exit_code=3,
                        stage=stage,
                        reason_code=REASON_PREVIEW_BINDING_DRIFT,
                        details={
                            **context.legacy_details_fields(),
                            "reason_code": REASON_PREVIEW_BINDING_DRIFT,
                            "side_effect": "none",
                            "drifted_fields": drifted_fields,
                        },
                    ),
                )
        if mode == MODE_PREVIEW:
            result = TransactionResult(
                ok=True,
                operation=operation,
                final_stage=TransactionStage.DRY_RUN_RETURN.value,
                result_code="dry_run_preview_ok",
                revisions={
                    "expected_file_revision": preplan.expected_file_revision,
                    "new_file_revision": preplan.new_file_revision,
                    "expected_workspace_revision": preplan.expected_workspace_revision,
                    "new_workspace_revision": preplan.new_workspace_revision,
                    "expected_cursor": preplan.expected_cursor,
                    "new_cursor": preplan.new_cursor,
                },
                receipt_evidence_ref=None,
                input_digest=preview_binding.input_digest,
                target_digest="sha256:" + hashlib.sha256(plan.new_target_bytes).hexdigest(),
                single_next_action={"kind": "none"},
            )
            return result, preview_binding

        # --- apply-only mutation stages (T050-04B) -----------------------------
        # The frozen commit order, inside the one identity lock, with the
        # §7.5 baseline handlers deciding every failure outcome. The write
        # gate never allows a binding-less mutation, so the precheck snapshot
        # always exists here.
        if not isinstance(receipt_store_snapshot, provenance_store.ReceiptStoreSnapshot):
            raise TransactionContractError(
                "Receipt-backed writes require a frozen store snapshot."
            )
        previous_target_text = plan.previous_target_bytes.decode("utf-8")
        new_target_text = plan.new_target_bytes.decode("utf-8")
        previous_state_text = plan.previous_state_bytes.decode("utf-8")
        expected_state_text = plan.new_state_bytes.decode("utf-8")

        # TARGET_REPLACE → TARGET_READBACK — the atomic primitive's exact byte
        # readback after the replace IS the readback stage.
        stage = TransactionStage.TARGET_REPLACE
        try:
            if operation == OPERATION_DAILY_LOG_APPEND and plan.target_identity is None:
                _write_text_create_only(plan.target_path, new_target_text)
            else:
                atomic_io.atomic_write_and_verify_if_unchanged(plan.target_path, expected_text=previous_target_text, new_text=new_target_text)
        except (LockBusyError, OSError, UnicodeDecodeError) as exc:
            return _handle_target_write_failure(
                context,
                request,
                failure=exc,
                target_path=plan.target_path,
                previous_target_text=previous_target_text,
                expected_target_text=new_target_text,
            )

        # STATE_REPLACE → STATE_READBACK — same primitive, same discipline.
        stage = TransactionStage.STATE_REPLACE
        try:
            atomic_io.atomic_write_and_verify_if_unchanged(
                plan.state_path,
                expected_text=previous_state_text,
                new_text=expected_state_text,
            )
        except (LockBusyError, OSError, UnicodeDecodeError):
            return _handle_state_write_failure(
                context,
                request,
                target_path=plan.target_path,
                state_path=plan.state_path,
                expected_target_text=new_target_text,
                previous_state_text=previous_state_text,
                expected_state_text=expected_state_text,
            )

        # POST_HASH — post-write digest verification; on target read/mismatch
        # the frozen verified provenance downgrade (inconsistent-review
        # evidence) executes before the receipt-finalization failure.
        stage = TransactionStage.POST_HASH
        post_hash = _apply_post_hash(
            context,
            request,
            storage_root=request.storage_root,
            project_root=request.workspace_root,
            plan=plan,
            previous_state_label=str(write_gate["provenance_state"]),
            preflight_binding_digest=str(preflight_binding["preflight_contract_hash"]),
            receipt_store_snapshot=receipt_store_snapshot,
        )
        if isinstance(post_hash, TransactionFailure):
            return post_hash
        target_digest, state_digest = post_hash

        # RECEIPT_FINALIZE — receipt seed + store commit against the one
        # precheck snapshot; the verified-not-written row runs the frozen
        # provenance downgrade first.
        stage = TransactionStage.RECEIPT_FINALIZE
        finalized = _apply_receipt_finalize(
            context,
            request,
            storage_root=request.storage_root,
            project_root=request.workspace_root,
            plan=plan,
            target_digest=target_digest,
            state_digest=state_digest,
            previous_state_label=str(write_gate["provenance_state"]),
            receipt_store_snapshot=receipt_store_snapshot,
        )
        if isinstance(finalized, TransactionFailure):
            return finalized
        receipt_finalization = finalized
        finalized_store_snapshot = receipt_finalization.get("store_snapshot")
        if not isinstance(finalized_store_snapshot, provenance_store.ReceiptStoreSnapshot):
            raise TransactionContractError(
                "Finalized receipts require a verified store snapshot."
            )

        # FINAL_EVIDENCE — expected final target/state/store exact readback.
        stage = TransactionStage.FINAL_EVIDENCE
        evidence_failure = _apply_final_evidence(
            context,
            request,
            storage_root=request.storage_root,
            project_root=request.workspace_root,
            plan=plan,
            finalized_store_snapshot=finalized_store_snapshot,
        )
        if evidence_failure is not None:
            return evidence_failure

        # RESULT — success-only; the adapter projects the legacy payload.
        stage = TransactionStage.RESULT
        result = TransactionResult(
            ok=True,
            operation=operation,
            final_stage=TransactionStage.RESULT.value,
            result_code="apply_committed",
            revisions={
                "expected_file_revision": plan.expected_file_revision,
                "new_file_revision": plan.new_file_revision,
                "expected_workspace_revision": plan.expected_workspace_revision,
                "new_workspace_revision": plan.new_workspace_revision,
                "expected_cursor": plan.expected_cursor,
                "new_cursor": plan.new_cursor,
            },
            receipt_evidence_ref=str(receipt_finalization["receipt_digest"]),
            input_digest=acquired.received_raw_sha256,
            target_digest=target_digest,
            single_next_action={"kind": "none"},
        )
        return result, {
            key: value
            for key, value in receipt_finalization.items()
            if key != "store_snapshot"
        }

    try:
        outcome = _stages()
    except (managed_write.ManagedWritePlannerError, daily_log_append.DailyLogAppendPlannerError) as exc:
        outcome = _failure(context, stage, exc)
    except InputTransportError as exc:
        outcome = _failure(context, stage, exc)
    except LockBusyError as exc:
        # Legacy apply parity: a lock error escaping the locked body is the
        # write_lock_busy exit (preview never raises one in-lock).
        outcome = _failure(
            context,
            stage,
            TransactionStageError(
                message=str(exc),
                reason="write_lock_busy",
                exit_code=3,
                stage=stage,
            ),
        )
    except (
        continuity_preflight.PreflightSnapshotError,
        DailyLogCursorError,
        StorageResolutionError,
        ConfigContractError,
    ) as exc:
        outcome = _failure(
            context,
            stage,
            TransactionStageError(
                message=str(exc),
                reason=getattr(exc, "failure_reason", None) or "damaged_sidecar",
                exit_code=2,
                stage=stage,
            ),
        )
    except (OSError, UnicodeDecodeError) as exc:
        outcome = _failure(
            context,
            stage,
            TransactionStageError(
                message=f"Filesystem error: {exc}",
                reason="damaged_sidecar",
                exit_code=2,
                stage=stage,
            ),
        )
    finally:
        if mode == MODE_PREVIEW:
            release_failed = False
            try:
                workspace_runtime._finalize_workspace_lock(handle)
            except (OSError, RecallLoomError):
                release_failed = True
            if not release_failed:
                final_observation = workspace_runtime.observe_workspace_lock(handle.lock_path)
                if (
                    final_observation.exists
                    and final_observation.anomaly is None
                    and final_observation.identity == handle.lock_identity
                    and final_observation.payload.get("instance_token") == handle.instance_token
                ):
                    release_failed = True
        else:
            release_error: Optional[tuple[str, int, str]] = None
            try:
                workspace_runtime._finalize_workspace_lock(handle)
            except LockBusyError as exc:
                release_error = ("write_lock_busy", 3, str(exc))
            except RecallLoomError as exc:
                release_error = (
                    getattr(exc, "failure_reason", None) or "damaged_sidecar",
                    2,
                    str(exc),
                )
            except (OSError, UnicodeDecodeError) as exc:
                release_error = ("damaged_sidecar", 2, f"Filesystem error: {exc}")
    if mode == MODE_PREVIEW and release_failed and not isinstance(outcome, TransactionFailure):
        # §7.3: a failed preview lock release is a typed failure with an
        # inspect-only next action; a successful preview is never reported.
        outcome = _failure(
            context,
            TransactionStage.LOCK,
            TransactionStageError(
                message=(
                    "The preview could not release the workspace identity lock; "
                    "inspect the lock state before any further write."
                ),
                reason=REASON_LOCK_RELEASE_FAILED,
                exit_code=2,
                stage=TransactionStage.LOCK,
                reason_code=REASON_LOCK_RELEASE_FAILED,
            ),
            safe_to_retry=False,
        )
    if mode == MODE_APPLY and release_error is not None:
        # v0.4.8.3 apply parity: a lock-finalizer failure replaces whatever
        # outcome the locked body produced (the with-block unwind exception
        # always won in the legacy helper), never a false success.
        release_reason, release_exit_code, release_message = release_error
        outcome = _failure(
            context,
            stage,
            TransactionStageError(
                message=release_message,
                reason=release_reason,
                exit_code=release_exit_code,
                stage=stage,
            ),
            safe_to_retry=False,
        )
    if isinstance(outcome, TransactionFailure) and outcome.scratch_disposition is None:
        outcome = replace(outcome, scratch_disposition=acquired.scratch_disposition)
    return outcome


def _concurrent_external_failure(
    context: OperationContext,
    stage: TransactionStage,
    request: TransactionRequest,
    side_effect: str,
) -> TransactionFailure:
    """The frozen concurrent-external-modification projection (exit 3)."""
    return _failure(
        context,
        stage,
        TransactionStageError(
            message=(
                "A managed file changed outside the bounded helper write snapshots. "
                "RecallLoom preserved the detected external state."
            ),
            reason="damaged_sidecar",
            exit_code=3,
            stage=stage,
            reason_code="concurrent_external_modification_detected",
            side_effect=side_effect,
            details={
                "reason_code": "concurrent_external_modification_detected",
                "side_effect": side_effect,
                "file_key": request.file_key,
                "new_file_revision": request.expected_file_revision + 1,
                "new_workspace_revision": request.expected_workspace_revision + 1,
            },
        ),
        safe_to_retry=False,
    )


def _preflight_binding_failure(
    context: OperationContext,
    stage: TransactionStage,
    *,
    message: str,
    reason_code: str,
    field_path: str = "$",
    extra: Optional[dict] = None,
) -> TransactionFailure:
    """The frozen preflight-binding failure projection (exit 2)."""
    return _failure(
        context,
        stage,
        TransactionStageError(
            message=message,
            reason="invalid_prepared_input",
            exit_code=2,
            stage=stage,
            reason_code=reason_code,
            details={
                "reason_code": reason_code,
                "field_path": field_path,
                "side_effect": "none",
                **(extra or {}),
            },
        ),
    )


def _receipt_precheck_failure(
    context: OperationContext,
    stage: TransactionStage,
    exc: BaseException,
    preplan: managed_write.OperationPreplan,
) -> TransactionFailure:
    """The frozen receipt-precheck projection (blocked_before_write, exit 2)."""
    reason_code = str(getattr(exc, "reason_code", "receipt_store_error"))
    reason = (
        "privacy_security_failure"
        if reason_code
        in {
            "prohibited_field",
            "value_requires_redaction",
            "non_string_key",
            "unsupported_value_type",
        }
        else "damaged_sidecar"
    )
    return _failure(
        context,
        stage,
        TransactionStageError(
            message=str(exc),
            reason=reason,
            exit_code=2,
            stage=stage,
            reason_code=reason_code,
            details={
                "reason_code": reason_code,
                "side_effect": "none",
                "file_key": preplan.receipt_precheck_inputs["file_key"],
                "new_file_revision": preplan.receipt_precheck_inputs["new_file_revision"],
                "new_workspace_revision": preplan.receipt_precheck_inputs[
                    "new_workspace_revision"
                ],
                "receipt_finalization_status": "blocked_before_write",
                "receipt_store_file": _RECEIPT_STORE_FILE,
                **(getattr(exc, "details", None) or {}),
                "receipt_precheck": True,
            },
        ),
        safe_to_retry=False,
    )


def _preview_binding_drift(
    presented: PreviewBinding, current: PreviewBinding
) -> list[str]:
    """Exact-match comparison; returns the drifted field names (empty = match)."""
    drifted = []
    for field_name in (
        "input_digest",
        "target_identity",
        "expected_file_revision",
        "expected_workspace_revision",
        "expected_cursor",
    ):
        if getattr(presented, field_name) != getattr(current, field_name):
            drifted.append(field_name)
    return drifted


# --- apply baseline handlers (§7.5 truth table, extracted verbatim from the ---
# --- v0.4.8.3 helper orchestration; reason codes, side effects, messages and ---
# --- exit projections are byte-identical — only the projection target changed -
# --- from ``exit_with_failure_contract`` to the typed ``TransactionStageError``)


def _write_text_create_only(path: Path, text: str) -> None:
    """Create a previously absent append target without a replacement race."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _stable_text_readback(path: Path) -> tuple[str, Optional[str], Optional[str]]:
    """Stable readback: two consecutive equal UTF-8 reads or a typed status.

    Exact extraction of the legacy readback discipline (core cannot import the
    adapter facade): ``read`` with the verified text, ``changed`` when two
    consecutive reads disagree, ``read_failed`` with the error class otherwise.
    """
    try:
        first_text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "read_failed", None, "unicode_decode_error"
    except OSError:
        return "read_failed", None, "os_error"
    try:
        second_text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "read_failed", None, "unicode_decode_error"
    except OSError:
        return "read_failed", None, "os_error"
    if first_text != second_text:
        return "changed", None, None
    return "read", second_text, None


def _handle_target_write_failure(
    context: OperationContext,
    request: TransactionRequest,
    *,
    failure: BaseException,
    target_path: Path,
    previous_target_text: str,
    expected_target_text: str,
) -> TransactionFailure:
    """§7.5 target rows: classify the observed truth after a failed replace.

    target_old_verified → ``target_write_failed`` / ``none`` (a LockBusyError
    means a concurrent external change, preserved); target_new_verified →
    ``target_write_post_verify_failed`` / ``write_attempted`` (never replayed);
    target_unknown → ``target_write_outcome_unknown`` / ``unknown`` (never
    retried, never rolled back). D5=false; safe_to_retry=false on every row.
    """
    target_status, target_text, _ = _stable_text_readback(target_path)
    if target_status == "read" and target_text == previous_target_text:
        if isinstance(failure, LockBusyError):
            return _concurrent_external_failure(
                context,
                TransactionStage.TARGET_REPLACE,
                request,
                "external_target_modification_preserved",
            )
        return _failure(
            context,
            TransactionStage.TARGET_REPLACE,
            TransactionStageError(
                message=(
                    "The daily-log target write failed before a new target snapshot was verified."
                    if request.context.operation == OPERATION_DAILY_LOG_APPEND
                    else "The managed target write failed before a new target snapshot was verified."
                ),
                reason="damaged_sidecar",
                exit_code=2,
                stage=TransactionStage.TARGET_REPLACE,
                reason_code="target_write_failed",
                side_effect="none",
                details={
                    "reason_code": "target_write_failed",
                    "side_effect": "none",
                },
            ),
            safe_to_retry=False,
        )
    if target_status == "read" and target_text == expected_target_text:
        return _failure(
            context,
            TransactionStage.TARGET_READBACK,
            TransactionStageError(
                message="The managed target was written, but the write result could not be verified in-line.",
                reason="damaged_sidecar",
                exit_code=2,
                stage=TransactionStage.TARGET_READBACK,
                reason_code="target_write_post_verify_failed",
                side_effect="write_attempted",
                details={
                    "reason_code": "target_write_post_verify_failed",
                    "side_effect": "write_attempted",
                },
            ),
            safe_to_retry=False,
        )
    if target_status in {"read", "changed"}:
        return _concurrent_external_failure(
            context,
            TransactionStage.TARGET_REPLACE,
            request,
            "external_target_modification_preserved",
        )
    return _failure(
        context,
        TransactionStage.TARGET_READBACK,
        TransactionStageError(
            message="The managed target write outcome is unreadable and must be reviewed.",
            reason="damaged_sidecar",
            exit_code=2,
            stage=TransactionStage.TARGET_READBACK,
            reason_code="target_write_outcome_unknown",
            side_effect="unknown",
            details={
                "reason_code": "target_write_outcome_unknown",
                "side_effect": "unknown",
            },
        ),
        safe_to_retry=False,
    )


def _handle_state_write_failure(
    context: OperationContext,
    request: TransactionRequest,
    *,
    target_path: Path,
    state_path: Path,
    expected_target_text: str,
    previous_state_text: str,
    expected_state_text: str,
) -> TransactionFailure:
    """§7.5 state rows: classify after a failed state replace post-target.

    state_old_verified with the new target verified →
    ``state_write_failed_target_preserved`` / ``write_attempted`` (no automatic
    retry or rollback); state_new_verified → ``state_write_post_verify_failed``
    / ``target_and_state_written_receipt_not_stored``; external state or target
    drift → the concurrent-external projection; unknown →
    ``state_write_outcome_unknown`` / ``unknown`` (never retried). D5=false.
    """
    state_status, state_text, _ = _stable_text_readback(state_path)
    target_status, target_text, _ = _stable_text_readback(target_path)
    if state_status == "read" and state_text == previous_state_text:
        if target_status == "read" and target_text == expected_target_text:
            return _failure(
                context,
                TransactionStage.STATE_REPLACE,
                TransactionStageError(
                    message=(
                        "The state write failed after the daily-log target was written; "
                        if request.context.operation == OPERATION_DAILY_LOG_APPEND
                        else "The state write failed after the managed target was written; "
                    ) + (
                        "all target bytes were preserved for read-only validation."
                    ),
                    reason="damaged_sidecar",
                    exit_code=2,
                    stage=TransactionStage.STATE_REPLACE,
                    reason_code="state_write_failed_target_preserved",
                    side_effect="write_attempted",
                    details={
                        "reason_code": "state_write_failed_target_preserved",
                        "side_effect": "write_attempted",
                    },
                ),
                safe_to_retry=False,
            )
        if target_status in {"read", "changed"}:
            return _concurrent_external_failure(
                context,
                TransactionStage.STATE_REPLACE,
                request,
                "external_target_modification_preserved",
            )
        return _failure(
            context,
            TransactionStage.STATE_REPLACE,
            TransactionStageError(
                message=(
                    "The state write failed and the managed target cannot be read; "
                    "no automatic rollback was attempted."
                ),
                reason="damaged_sidecar",
                exit_code=2,
                stage=TransactionStage.STATE_REPLACE,
                reason_code="state_write_outcome_unknown",
                side_effect="unknown",
                details={
                    "reason_code": "state_write_outcome_unknown",
                    "side_effect": "unknown",
                },
            ),
            safe_to_retry=False,
        )
    if state_status == "read" and state_text == expected_state_text:
        return _failure(
            context,
            TransactionStage.STATE_READBACK,
            TransactionStageError(
                message="Target and state were written, but the state write could not be verified in-line.",
                reason="damaged_sidecar",
                exit_code=2,
                stage=TransactionStage.STATE_READBACK,
                reason_code="state_write_post_verify_failed",
                side_effect="target_and_state_written_receipt_not_stored",
                details={
                    "reason_code": "state_write_post_verify_failed",
                    "side_effect": "target_and_state_written_receipt_not_stored",
                },
            ),
            safe_to_retry=False,
        )
    if state_status in {"read", "changed"}:
        return _concurrent_external_failure(
            context,
            TransactionStage.STATE_REPLACE,
            request,
            "external_state_modification_preserved",
        )
    return _failure(
        context,
        TransactionStage.STATE_READBACK,
        TransactionStageError(
            message="The state write outcome is unreadable; the target was not rolled back.",
            reason="damaged_sidecar",
            exit_code=2,
            stage=TransactionStage.STATE_READBACK,
            reason_code="state_write_outcome_unknown",
            side_effect="unknown",
            details={
                "reason_code": "state_write_outcome_unknown",
                "side_effect": "unknown",
            },
        ),
        safe_to_retry=False,
    )


def _receipt_finalization_failure(
    context: OperationContext,
    stage: TransactionStage,
    request: TransactionRequest,
    *,
    message: str,
    reason_code: str,
    side_effect: str,
    extra: Optional[dict] = None,
) -> TransactionFailure:
    """The frozen receipt/post-hash failure projection (exit 2, no retry)."""
    revision_key = (
        "target_entry_seq"
        if request.context.operation == OPERATION_DAILY_LOG_APPEND
        else "new_file_revision"
    )
    details = {
        "reason_code": reason_code,
        "side_effect": side_effect,
        "file_key": request.file_key,
        revision_key: request.expected_file_revision + 1,
        "new_workspace_revision": request.expected_workspace_revision + 1,
        "receipt_finalization_status": "failed",
        "receipt_store_file": _RECEIPT_STORE_FILE,
        **(extra or {}),
    }
    reason = (
        "privacy_security_failure"
        if reason_code
        in {
            "prohibited_field",
            "value_requires_redaction",
            "non_string_key",
            "unsupported_value_type",
        }
        else "damaged_sidecar"
    )
    return _failure(
        context,
        stage,
        TransactionStageError(
            message=message,
            reason=reason,
            exit_code=2,
            stage=stage,
            reason_code=reason_code,
            side_effect=side_effect,
            details=details,
        ),
        safe_to_retry=False,
    )


def _apply_post_hash(
    context: OperationContext,
    request: TransactionRequest,
    *,
    storage_root: Path,
    project_root: Path,
    plan: Any,
    previous_state_label: str,
    preflight_binding_digest: str,
    receipt_store_snapshot: provenance_store.ReceiptStoreSnapshot,
) -> Any:
    """POST_HASH stage: exact post-write digests of target and state.

    Returns ``(target_digest, state_digest)`` on success. On state readback
    failure/mismatch this is the frozen ``post_hash_read_failed`` /
    ``post_hash_mismatch`` receipt-family failure. On target read/mismatch the
    frozen verified provenance downgrade executes first: the inconsistent-
    review evidence (origin ``helper_post_hash_verification``, protocol 1.0)
    is built and written to state, then the same receipt-family failure is
    returned — never a false success, never a replay. D5 eligibility stays
    decided solely by the inconsistent-review contract predicate.
    """
    stage = TransactionStage.POST_HASH
    expected_target_text = plan.new_target_bytes.decode("utf-8")
    expected_state_text = plan.new_state_bytes.decode("utf-8")
    target_status, post_target_text, target_error_class = _stable_text_readback(
        plan.target_path
    )
    state_status, post_state_text, _ = _stable_text_readback(plan.state_path)
    if target_status == "changed" or state_status == "changed":
        return _concurrent_external_failure(
            context, stage, request, "target_and_state_written_receipt_not_stored"
        )
    if state_status != "read" or post_state_text is None:
        return _receipt_finalization_failure(
            context,
            stage,
            request,
            message=(
                "Could not stably re-read post-append state for receipt finalization."
                if request.context.operation == OPERATION_DAILY_LOG_APPEND
                else "Could not stably re-read post-write state for receipt finalization."
            ),
            reason_code="post_hash_read_failed",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    state_digest = managed_write.sha256_text_digest(post_state_text)
    expected_state_digest = managed_write.sha256_text_digest(expected_state_text)
    if post_state_text != expected_state_text:
        mismatch_details = {
            "state_digest": state_digest,
            "expected_state_digest": expected_state_digest,
        }
        if target_status == "read" and post_target_text is not None:
            mismatch_details.update(
                {
                    "target_digest": managed_write.sha256_text_digest(post_target_text),
                    "expected_target_digest": managed_write.sha256_text_digest(
                        expected_target_text
                    ),
                }
            )
        return _receipt_finalization_failure(
            context,
            stage,
            request,
            message="Post-write state no longer matches the expected helper-written state.",
            reason_code="post_hash_mismatch",
            side_effect="target_and_state_written_receipt_not_stored",
            extra=mismatch_details,
        )
    if target_status == "read" and post_target_text == expected_target_text:
        return managed_write.sha256_text_digest(post_target_text), state_digest

    if not provenance_store.receipt_store_snapshot_matches_current(
        storage_root=storage_root,
        project_root=project_root,
        snapshot=receipt_store_snapshot,
    ):
        return _concurrent_external_failure(
            context, stage, request, "target_and_state_written_receipt_not_stored"
        )

    reason_code = (
        "post_hash_read_failed" if target_status == "read_failed" else "post_hash_mismatch"
    )
    observed_target_digest = (
        managed_write.sha256_text_digest(post_target_text)
        if target_status == "read" and post_target_text is not None
        else None
    )
    target_failure_details = None
    if observed_target_digest is not None:
        target_failure_details = {
            "target_digest": observed_target_digest,
            "state_digest": state_digest,
            "expected_target_digest": managed_write.sha256_text_digest(expected_target_text),
            "expected_state_digest": expected_state_digest,
        }
    if plan.operation == OPERATION_DAILY_LOG_APPEND:
        operation_class = "daily_log_append"
        target_file_key = "daily_log"
        latest_file = plan.new_cursor.get("latest_file")
        target_date = (
            Path(str(latest_file)).stem
            if isinstance(latest_file, str) and latest_file
            else plan.target_path.stem
        )
    else:
        operation_class = "managed_write"
        target_file_key = request.file_key
        target_date = None
    try:
        failure_evidence = build_inconsistent_review_evidence(
            reason_code=reason_code,
            operation_class=operation_class,
            target_file_key=target_file_key,
            target_date=target_date,
            target_verification=(
                "read_failed" if target_status == "read_failed" else "mismatch"
            ),
            target_error_class=(
                target_error_class if target_status == "read_failed" else None
            ),
            expected_target_digest=managed_write.sha256_text_digest(expected_target_text),
            observed_target_digest=observed_target_digest,
            expected_state_digest=expected_state_digest,
            observed_state_digest=state_digest,
            expected_workspace_revision=plan.new_workspace_revision - 1,
            result_workspace_revision=plan.new_workspace_revision,
            expected_file_revision=plan.new_file_revision - 1,
            result_file_revision=plan.new_file_revision,
            preflight_binding_digest=preflight_binding_digest,
            receipt_store_snapshot=receipt_store_snapshot,
            previous_state_label=previous_state_label,
        )
    except InconsistentReviewContractError:
        return _receipt_finalization_failure(
            context,
            stage,
            request,
            message="Post-write target verification failed and recovery evidence was ineligible.",
            reason_code=reason_code,
            side_effect="target_and_state_written_receipt_not_stored",
            extra=target_failure_details,
        )

    failure_metadata = provenance_state.inconsistent_evidence_metadata(
        timestamp=workspace_runtime.now_iso_timestamp(),
        reason_code=reason_code,
        previous_state_label=previous_state_label,
    )
    failure_metadata["inconsistent_review_evidence"] = failure_evidence
    failure_state = dict(json.loads(plan.new_state_bytes.decode("utf-8")))
    failure_state["provenance"] = failure_metadata
    try:
        atomic_io.atomic_write_and_verify_if_unchanged(
            plan.state_path,
            expected_text=expected_state_text,
            new_text=managed_write.expected_state_json_text(failure_state),
        )
    except LockBusyError:
        return _concurrent_external_failure(
            context, stage, request, "target_and_state_written_receipt_not_stored"
        )
    except (OSError, UnicodeDecodeError):
        return _receipt_finalization_failure(
            context,
            stage,
            request,
            message="Post-write target verification failed and recovery metadata was not verified.",
            reason_code=reason_code,
            side_effect="target_and_state_written_receipt_not_stored",
            extra=target_failure_details,
        )
    return _receipt_finalization_failure(
        context,
        stage,
        request,
        message="Post-write target verification failed; bounded recovery evidence was recorded.",
        reason_code=reason_code,
        side_effect="target_and_state_written_receipt_not_stored",
        extra=target_failure_details,
    )


def _downgrade_after_verified_receipt_not_written(
    context: OperationContext,
    request: TransactionRequest,
    *,
    storage_root: Path,
    project_root: Path,
    plan: managed_write.OperationPlan,
    previous_state_label: str,
    receipt_store_snapshot: provenance_store.ReceiptStoreSnapshot,
) -> Optional[TransactionFailure]:
    """§7.5 receipt_verified_not_written: the frozen verified downgrade.

    The unchanged pre-write snapshot proves the receipt was not stored, so
    provenance is downgraded to ``unproven_sidecar_state`` through one more
    CAS against the expected post-write state. Any drift is the concurrent-
    external projection; an unverifiable downgrade is
    ``receipt_failure_provenance_restore_failed``. Returns ``None`` when the
    downgrade itself landed (the receipt failure is then projected).
    """
    stage = TransactionStage.RECEIPT_FINALIZE
    expected_target_text = plan.new_target_bytes.decode("utf-8")
    expected_state_text = plan.new_state_bytes.decode("utf-8")
    downgraded_state = dict(json.loads(plan.new_state_bytes.decode("utf-8")))
    downgraded_state["provenance"] = provenance_state.unproven_sidecar_metadata(
        timestamp=workspace_runtime.now_iso_timestamp(),
        reason_code=provenance_store.RECEIPT_STORE_NOT_WRITTEN_VERIFIED,
        previous_state_label=previous_state_label,
    )
    downgraded_state_text = managed_write.expected_state_json_text(downgraded_state)
    target_status, target_text, _ = _stable_text_readback(plan.target_path)
    if (
        target_status != "read"
        or target_text != expected_target_text
        or not provenance_store.receipt_store_snapshot_matches_current(
            storage_root=storage_root,
            project_root=project_root,
            snapshot=receipt_store_snapshot,
        )
    ):
        return _concurrent_external_failure(
            context, stage, request, "target_and_state_written_receipt_not_stored"
        )
    try:
        atomic_io.atomic_write_and_verify_if_unchanged(
            plan.state_path,
            expected_text=expected_state_text,
            new_text=downgraded_state_text,
        )
    except LockBusyError:
        return _concurrent_external_failure(
            context, stage, request, "target_and_state_written_receipt_not_stored"
        )
    except (OSError, UnicodeDecodeError):
        return _receipt_finalization_failure(
            context,
            stage,
            request,
            message="Receipt finalization failed and provenance downgrade could not be verified.",
            reason_code="receipt_failure_provenance_restore_failed",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    return None


def _apply_receipt_finalize(
    context: OperationContext,
    request: TransactionRequest,
    *,
    storage_root: Path,
    project_root: Path,
    plan: managed_write.OperationPlan,
    target_digest: str,
    state_digest: str,
    previous_state_label: str,
    receipt_store_snapshot: provenance_store.ReceiptStoreSnapshot,
) -> Any:
    """RECEIPT_FINALIZE stage: build the seed and commit it against the one
    precheck snapshot (the frozen count discipline reuses it; no second full
    validation). Returns the store's finalization mapping on success. Every
    ``ReceiptStoreError`` keeps its exact reason/side-effect vocabulary; the
    verified-not-written row runs the frozen downgrade first. Never retried,
    never a false success.
    """
    stage = TransactionStage.RECEIPT_FINALIZE
    seed_inputs = plan.receipt_seed_inputs
    if plan.operation == OPERATION_DAILY_LOG_APPEND:
        receipt_seed = daily_log_append.build_append_receipt_seed(
            preflight_binding=seed_inputs["preflight_binding"],
            timestamp=str(seed_inputs["timestamp"]),
            target_digest=target_digest,
            state_digest=state_digest,
            previous_entry_seq=int(seed_inputs["previous_entry_seq"]),
            next_entry_seq=plan.new_file_revision,
            new_workspace_revision=plan.new_workspace_revision,
            helper_version=str(seed_inputs["helper_version"]),
        )
    else:
        receipt_seed = managed_write.build_receipt_seed(
            file_key=str(seed_inputs["file_key"]),
            expected_file_revision=int(seed_inputs["expected_file_revision"]),
            expected_workspace_revision=int(seed_inputs["expected_workspace_revision"]),
            helper_version=str(seed_inputs["helper_version"]),
            preflight_binding=seed_inputs["preflight_binding"],
            timestamp=str(seed_inputs["timestamp"]),
            target_digest=target_digest,
            state_digest=state_digest,
            new_file_revision=int(seed_inputs["new_file_revision"]),
            new_workspace_revision=int(seed_inputs["new_workspace_revision"]),
            controlled_metadata_refresh=seed_inputs["controlled_metadata_refresh"],
        )
    try:
        # The facade is the sole compatibility seam for existing fault
        # injection. Its implementation is exactly the frozen four-phase
        # API below, with this precheck snapshot passed through unchanged.
        return provenance_store.finalize_receipt_in_store(
            storage_root=storage_root,
            receipt=receipt_seed,
            project_root=project_root,
            expected_snapshot=receipt_store_snapshot,
        )
    except provenance_store.ReceiptStoreError as exc:
        if exc.reason_code == provenance_store.RECEIPT_STORE_NOT_WRITTEN_VERIFIED:
            downgrade_failure = _downgrade_after_verified_receipt_not_written(
                context,
                request,
                storage_root=storage_root,
                project_root=project_root,
                plan=plan,
                previous_state_label=previous_state_label,
                receipt_store_snapshot=receipt_store_snapshot,
            )
            if downgrade_failure is not None:
                return downgrade_failure
        return _receipt_finalization_failure(
            context,
            stage,
            request,
            message=str(exc),
            reason_code=exc.reason_code,
            side_effect=exc.side_effect,
            extra=exc.details,
        )


def _apply_final_evidence(
    context: OperationContext,
    request: TransactionRequest,
    *,
    storage_root: Path,
    project_root: Path,
    plan: Any,
    finalized_store_snapshot: provenance_store.ReceiptStoreSnapshot,
) -> Optional[TransactionFailure]:
    """FINAL_EVIDENCE stage: expected final snapshot vs exact readback.

    Any target/state/store drift after finalization is the frozen
    ``*_written_review_required`` concurrent-external projection — never a
    false success; the next action is read-only inspect.
    """
    stage = TransactionStage.FINAL_EVIDENCE
    expected_target_text = plan.new_target_bytes.decode("utf-8")
    expected_state_text = plan.new_state_bytes.decode("utf-8")
    target_status, target_text, _ = _stable_text_readback(plan.target_path)
    state_status, state_text, _ = _stable_text_readback(plan.state_path)
    store_matches = provenance_store.receipt_store_snapshot_matches_current(
        storage_root=storage_root,
        project_root=project_root,
        snapshot=finalized_store_snapshot,
    )
    if (
        target_status != "read"
        or target_text != expected_target_text
        or state_status != "read"
        or state_text != expected_state_text
        or not store_matches
    ):
        return _concurrent_external_failure(
            context,
            stage,
            request,
            "target_state_and_receipt_store_written_review_required",
        )
    return None
