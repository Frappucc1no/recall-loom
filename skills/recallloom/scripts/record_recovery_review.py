#!/usr/bin/env python3
"""Record a prepared recovery review for a staged RecallLoom recovery proposal."""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import stat
import sys

from core.coldstart.structured import (
    REVIEW_SECTION_ALIASES,
    classify_review_action,
    extract_structured_sections,
    promotion_ready_for_action,
)
from core.protocol.contracts import FILE_KEYS
from core.provenance.evidence import (
    strict_sidecar_integrity_gate,
    strict_sidecar_integrity_gate_public_summary,
)
from core.provenance.inconsistent_review import (
    INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    INCONSISTENT_REVIEW_EVIDENCE_KEY,
    InconsistentReviewContractError,
    evaluate_current_inconsistent_review_binding,
    is_sha256_digest,
    staged_d5_proposal_digest_from_filename,
    validate_inconsistent_recovery_material_size,
    validate_inconsistent_recovery_proposal_text,
    validate_inconsistent_recovery_review_text,
)
from core.provenance.state import (
    inconsistent_review_imported_baseline_metadata,
    review_imported_baseline_metadata,
)
from core.safety.prepared_input import (
    PreparedInputSafetyError,
    read_prepared_input_source_text,
    validate_prepared_input_source_path,
)
from core.workspace.atomic_io import atomic_write_and_verify_if_unchanged

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_managed_directory_chain,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue_for_sources,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    load_workspace_state,
    LockBusyError,
    ManagedDirectorySafetyError,
    now_iso_timestamp,
    public_project_path,
    publicize_text_paths,
    public_json_payload,
    read_text,
    RECOVERY_PROPOSAL_FILE_RE,
    StorageResolutionError,
    scan_auto_attached_context_text,
    text_digest,
    validate_recovery_proposal_text,
    validate_recovery_review_text,
    workspace_write_lock,
    write_text,
    write_text_create_only,
)

DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record a recovery review for a staged RecallLoom recovery proposal."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument(
        "--proposal-file",
        required=True,
        help="Proposal filename or path. Relative values are resolved against companion/recovery/proposals/ first.",
    )
    parser.add_argument("--source-file", help="Path to prepared review markdown content.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared review markdown content from UTF-8 stdin.",
    )
    parser.add_argument(
        "--expected-inconsistent-review-binding-digest",
        help=(
            "Explicitly bind a D5 inconsistent-evidence promotion to the current "
            "sha256 review binding."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def exit_prepared_input_safety_error(
    parser,
    *,
    json_mode: bool,
    error: PreparedInputSafetyError,
) -> None:
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=error.message,
        reason="invalid_prepared_input",
        details=error.details,
    )


def enforce_recovery_import_strict_gate(
    parser,
    *,
    json_mode: bool,
    project_root: Path,
    storage_root: Path,
    allow_inconsistent_evidence: bool = False,
) -> dict:
    gate = strict_sidecar_integrity_gate(
        project_root=project_root,
        storage_root=storage_root,
    )
    gate_summary = strict_sidecar_integrity_gate_public_summary(gate)
    if (
        gate.get("allowed_for_mutation") is True
        or gate.get("reason_code") == "provenance_review_required"
    ):
        return gate_summary
    if (
        allow_inconsistent_evidence
        and gate.get("reason_code") == "provenance_evidence_inconsistent"
    ):
        return {
            **gate_summary,
            "allowed_for_mutation": True,
            "blocked_reason": None,
            "reason_code": "strict_sidecar_structure_verified_for_d5_review",
            "provenance_state_check": "excluded_for_bound_d5_promotion",
            "safe_next_action": (
                "Proceed only through the exact binding-checked D5 review promotion path."
            ),
        }
    if gate.get("reason_code") == "provenance_evidence_inconsistent":
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=3,
            message=(
                "D5 inconsistent-evidence review promotion requires an explicit "
                "--expected-inconsistent-review-binding-digest."
            ),
            reason="trust_review_required",
            details={
                "reason_code": "expected_inconsistent_review_binding_digest_required",
                "side_effect": "none",
                "safe_to_retry": False,
            },
        )
    message = (
        "Strict sidecar integrity gate blocked recovery review promotion. "
        "Repair lower-level sidecar evidence before recording review_imported_baseline."
    )
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=3,
        message=message,
        reason="trust_review_required",
        details={
            "reason_code": "strict_sidecar_integrity_failed",
            "strict_gate_reason_code": gate_summary.get("reason_code"),
            "strict_sidecar_integrity_gate": gate_summary,
            "command": "record_recovery_review",
            "operation": "recovery_review_promotion",
            "side_effect": "none",
        },
    )


def read_recovery_source(
    parser,
    *,
    json_mode: bool,
    raw_source_file: str | None,
    use_stdin: bool,
    project_root: Path,
    storage_root: Path,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> tuple[Path | None, str, str]:
    if bool(raw_source_file) == bool(use_stdin):
        message = "Provide prepared review content with exactly one of --source-file or --stdin."
        if raw_source_file and use_stdin:
            message = "Use exactly one prepared review input: --source-file or --stdin."
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            reason="invalid_prepared_input",
            details={"side_effect": "none"},
        )

    source_path: Path | None = None
    input_mode = "stdin" if use_stdin else "file"
    if raw_source_file:
        try:
            source = validate_prepared_input_source_path(
                raw_source_file,
                project_root=project_root,
                storage_root=storage_root,
                input_role="source-file",
                label="source",
            )
            body_text = read_prepared_input_source_text(
                source,
                max_input_bytes=max_input_bytes,
                label="source",
            )
        except PreparedInputSafetyError as exc:
            exit_prepared_input_safety_error(parser, json_mode=json_mode, error=exc)
        source_path = source.path
    else:
        if sys.stdin.isatty():
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="No prepared review content was provided on stdin.",
                reason="invalid_prepared_input",
                details={"input_mode": "stdin", "side_effect": "none"},
            )
        try:
            raw_bytes = sys.stdin.buffer.read(max_input_bytes + 1)
        except OSError as exc:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message=f"Failed to read prepared review stdin input: {exc}",
                reason="invalid_prepared_input",
                details={
                    "input_mode": "stdin",
                    "reason_code": "stdin_read_failed",
                    "error_type": type(exc).__name__,
                    "side_effect": "none",
                },
            )
        if len(raw_bytes) > max_input_bytes:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message=(
                    "Prepared review stdin input exceeds the maximum size "
                    f"({len(raw_bytes)} > {max_input_bytes})."
                ),
                reason="invalid_prepared_input",
                details={
                    "input_mode": "stdin",
                    "size": len(raw_bytes),
                    "max_input_bytes": max_input_bytes,
                    "side_effect": "none",
                },
            )
        try:
            body_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="Prepared review stdin input must be valid UTF-8.",
                reason="invalid_prepared_input",
                details={
                    "input_mode": "stdin",
                    "reason_code": "stdin_decode_failed",
                    "error_type": type(exc).__name__,
                    "side_effect": "none",
                },
            )

    if not body_text.strip():
        message = "Source file is empty."
        if use_stdin:
            message = "Prepared review stdin input is empty."
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={
                    "input_mode": input_mode,
                    "source_file_ref": "provided_source_file" if source_path else None,
                    "side_effect": "none",
                },
            ),
        )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        message = (
            "Refusing to record recovery review because the prepared source failed "
            "the attached-text safety scan: "
            + ", ".join(attach_scan["hard_block_reasons"])
        )
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "attach_scan_blocked",
                error=message,
                details={"hard_block_reasons": attach_scan["hard_block_reasons"]},
            ),
        )
    return source_path, body_text, input_mode


def resolve_proposal_path(raw_value: str, proposals_dir: Path, project_root: Path) -> Path:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    proposal_relative = proposals_dir / raw_value
    if proposal_relative.exists():
        return proposal_relative.resolve()

    project_relative = project_root / raw_value
    return project_relative.resolve()


def stable_regular_text_snapshot(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> str | None:
    file_fd: int | None = None
    try:
        file_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before_stat = os.fstat(file_fd)
        if not stat.S_ISREG(before_stat.st_mode) or before_stat.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
        after_stat = os.fstat(file_fd)
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
    before_identity = (
        before_stat.st_dev,
        before_stat.st_ino,
        before_stat.st_mode,
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )
    after_identity = (
        after_stat.st_dev,
        after_stat.st_ino,
        after_stat.st_mode,
        after_stat.st_size,
        after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
    )
    if (
        before_identity != after_identity
        or len(raw_bytes) > max_bytes
        or len(raw_bytes) != after_stat.st_size
    ):
        return None
    return text


def exact_d5_materials_match(
    *,
    proposal_filename: str,
    proposal_text: str | None,
    review_text: str | None,
    expected_proposal_text: str,
    expected_review_text: str,
    expected_proposal_digest: str,
    expected_review_digest: str,
) -> bool:
    """Compare exact D5 bytes and the proposal filename content binding."""

    return (
        proposal_text == expected_proposal_text
        and review_text == expected_review_text
        and text_digest(proposal_text or "") == expected_proposal_digest
        and text_digest(review_text or "") == expected_review_digest
        and staged_d5_proposal_digest_from_filename(proposal_filename)
        == "sha256:" + expected_proposal_digest
    )


def exit_d5_promotion_failure(
    parser,
    *,
    json_mode: bool,
    message: str,
    reason_code: str,
    expected_binding_digest: str,
    exit_code: int = 3,
    reason: str = "trust_review_required",
    side_effect: str = "none",
    safe_to_retry: bool = False,
    extra: dict | None = None,
) -> None:
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        reason=reason,
        details={
            "reason_code": reason_code,
            "side_effect": side_effect,
            "safe_to_retry": safe_to_retry,
            "expected_inconsistent_review_binding_digest": expected_binding_digest,
            **(extra or {}),
        },
    )


def current_d5_binding_or_exit(
    parser,
    *,
    json_mode: bool,
    project_root: Path,
    storage_root: Path,
    state: dict,
    state_text: str,
    expected_binding_digest: str,
    side_effect: str = "none",
) -> dict:
    result = evaluate_current_inconsistent_review_binding(
        project_root=project_root,
        storage_root=storage_root,
        state=state,
        state_text=state_text,
    )
    if (
        result.status != "current"
        or not isinstance(result.binding, dict)
        or result.binding_digest != expected_binding_digest
    ):
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message=(
                "The current inconsistent-evidence review binding no longer matches "
                "the explicitly expected digest."
            ),
            reason_code="post_hash_inconsistent_review_binding_changed",
            expected_binding_digest=expected_binding_digest,
            side_effect=side_effect,
            extra={
                "binding_status": result.status,
                "binding_reason_code": result.reason_code,
                "current_inconsistent_review_binding_digest": result.binding_digest,
            },
        )
    return result.binding


def exit_d5_review_recorded_not_committed(
    parser,
    *,
    json_mode: bool,
    expected_binding_digest: str,
    extra: dict | None = None,
) -> None:
    exit_d5_promotion_failure(
        parser,
        json_mode=json_mode,
        message=(
            "The exact recovery review evidence is recorded, but state promotion was not "
            "committed. Do not retry without explicitly reusing the same binding and review bytes."
        ),
        reason_code="post_hash_inconsistent_review_promotion_not_committed",
        expected_binding_digest=expected_binding_digest,
        side_effect="review_evidence_recorded_state_unchanged",
        safe_to_retry=False,
        extra=extra,
    )


def record_d5_inconsistent_review_promotion(
    parser,
    *,
    json_mode: bool,
    project_root: Path,
    storage_root: Path,
    proposal_path: Path,
    review_path: Path,
    source_path: Path | None,
    prepared_review_text: str,
    expected_binding_digest: str,
    recorded_at: str,
) -> dict:
    state_path = storage_root / FILE_KEYS["state"]
    state = load_workspace_state(state_path)
    current_state_text = stable_regular_text_snapshot(state_path)
    if current_state_text is None:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="The current state snapshot is unavailable for D5 promotion.",
            reason_code="post_hash_inconsistent_review_binding_changed",
            expected_binding_digest=expected_binding_digest,
            extra={"binding_reason_code": "inconsistent_review_state_unavailable"},
        )
    binding = current_d5_binding_or_exit(
        parser,
        json_mode=json_mode,
        project_root=project_root,
        storage_root=storage_root,
        state=state,
        state_text=current_state_text,
        expected_binding_digest=expected_binding_digest,
    )

    proposal_text = stable_regular_text_snapshot(
        proposal_path,
        max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    )
    if proposal_text is None:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="The staged D5 recovery proposal is unavailable or unstable.",
            reason_code="inconsistent_recovery_proposal_unavailable",
            expected_binding_digest=expected_binding_digest,
            reason="malformed_managed_file",
        )
    proposal_digest = text_digest(proposal_text)
    staged_proposal_digest = staged_d5_proposal_digest_from_filename(
        proposal_path.name
    )
    if staged_proposal_digest != "sha256:" + proposal_digest:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message=(
                "The staged D5 proposal bytes no longer match their create-only "
                "content binding."
            ),
            reason_code="post_hash_inconsistent_review_binding_changed",
            expected_binding_digest=expected_binding_digest,
            extra={
                "binding_reason_code": (
                    "inconsistent_review_proposal_bytes_changed"
                )
            },
        )
    if source_path is not None:
        source_review_text = stable_regular_text_snapshot(
            source_path,
            max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
        )
        if source_review_text is None or source_review_text != prepared_review_text:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                message="Prepared D5 review input changed after it was read.",
                reason_code="inconsistent_recovery_review_source_changed",
                expected_binding_digest=expected_binding_digest,
                reason="invalid_prepared_input",
                exit_code=2,
            )
    review_text = prepared_review_text.rstrip("\n") + "\n"

    proposal_errors = validate_recovery_proposal_text(proposal_text)
    if proposal_errors:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="The staged D5 recovery proposal failed structure checks.",
            reason_code="inconsistent_recovery_proposal_structure_invalid",
            expected_binding_digest=expected_binding_digest,
            reason="malformed_managed_file",
            exit_code=2,
            extra={"proposal_errors": proposal_errors},
        )
    review_errors = validate_recovery_review_text(review_text)
    if review_errors:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="Prepared D5 recovery review failed structure checks.",
            reason_code="inconsistent_recovery_review_structure_invalid",
            expected_binding_digest=expected_binding_digest,
            reason="invalid_prepared_input",
            exit_code=2,
            extra={"review_errors": review_errors},
        )
    try:
        proposal_material = validate_inconsistent_recovery_proposal_text(
            proposal_text,
            expected_binding_digest=expected_binding_digest,
        )
    except InconsistentReviewContractError as exc:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="The staged proposal does not contain exact D5 promotion material.",
            reason_code=exc.reason_code,
            expected_binding_digest=expected_binding_digest,
            reason="malformed_managed_file",
            exit_code=2,
        )
    try:
        review_material = validate_inconsistent_recovery_review_text(
            review_text,
            expected_binding_digest=expected_binding_digest,
        )
    except InconsistentReviewContractError as exc:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="Prepared review does not contain exact D5 promotion material.",
            reason_code=exc.reason_code,
            expected_binding_digest=expected_binding_digest,
            reason="invalid_prepared_input",
            exit_code=2,
        )
    if not isinstance(proposal_material, dict):
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="The staged proposal is missing required D5 promotion material.",
            reason_code="inconsistent_recovery_proposal_material_missing",
            expected_binding_digest=expected_binding_digest,
            reason="malformed_managed_file",
            exit_code=2,
        )
    if not isinstance(review_material, dict):
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="Prepared review is missing required D5 promotion material.",
            reason_code="inconsistent_recovery_review_material_missing",
            expected_binding_digest=expected_binding_digest,
            reason="invalid_prepared_input",
            exit_code=2,
        )

    review_sections = extract_structured_sections(review_text, REVIEW_SECTION_ALIASES)
    review_action = classify_review_action(review_sections)
    if review_action == "accept_after_edit":
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="D5 promotion does not permit accept-after-edit review outcomes.",
            reason_code="inconsistent_recovery_review_accept_after_edit_not_allowed",
            expected_binding_digest=expected_binding_digest,
            reason="invalid_prepared_input",
            exit_code=2,
        )
    if review_material["decision"] != review_action:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="D5 review JSON decision conflicts with the existing review classifier.",
            reason_code="inconsistent_recovery_review_outcome_mismatch",
            expected_binding_digest=expected_binding_digest,
            reason="invalid_prepared_input",
            exit_code=2,
            extra={
                "classified_review_action": review_action,
                "review_material_decision": review_material["decision"],
            },
        )
    if (
        review_action != "accept"
        or review_material["decision"] != "accept"
        or review_material["accept_current_target_as_reviewed_baseline"] is not True
    ):
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="D5 promotion requires an exact accept decision and explicit true confirmation.",
            reason_code="inconsistent_recovery_review_not_accepted",
            expected_binding_digest=expected_binding_digest,
            reason="invalid_prepared_input",
            exit_code=2,
        )

    review_digest = text_digest(review_text)
    next_state = dict(state)
    next_state["workspace_revision"] = state["workspace_revision"] + 1
    next_state["provenance"] = inconsistent_review_imported_baseline_metadata(
        timestamp=recorded_at,
        review_action=review_action,
        source_reason_code=str(binding["reason_code"]),
        inconsistent_review_binding=binding,
        inconsistent_review_binding_digest=expected_binding_digest,
        proposal_digest=proposal_digest,
        review_digest=review_digest,
    )
    next_state_text = json.dumps(next_state, ensure_ascii=False, indent=2) + "\n"

    existing_review_text = stable_regular_text_snapshot(
        review_path,
        max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    )
    orphan_reused = existing_review_text is not None
    if orphan_reused and existing_review_text != review_text:
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            message="An existing review file does not match the exact D5 review bytes.",
            reason_code="post_hash_inconsistent_review_binding_changed",
            expected_binding_digest=expected_binding_digest,
            extra={"binding_reason_code": "inconsistent_review_orphan_review_changed"},
        )
    if not orphan_reused:
        try:
            review_path.lstat()
        except FileNotFoundError:
            pass
        else:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                message="The D5 review path exists but is not a stable regular review file.",
                reason_code="post_hash_inconsistent_review_binding_changed",
                expected_binding_digest=expected_binding_digest,
                extra={"binding_reason_code": "inconsistent_review_orphan_review_invalid"},
            )
        try:
            write_text_create_only(review_path, review_text)
        except (LockBusyError, OSError, UnicodeDecodeError) as exc:
            existing_review_text = stable_regular_text_snapshot(
                review_path,
                max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
            )
            if existing_review_text != review_text:
                exit_d5_promotion_failure(
                    parser,
                    json_mode=json_mode,
                    message="The exact D5 review could not be recorded create-only.",
                    reason_code="inconsistent_recovery_review_create_failed",
                    expected_binding_digest=expected_binding_digest,
                    reason="damaged_sidecar",
                    exit_code=2,
                    extra={"error_type": type(exc).__name__},
                )

    committed_proposal_text = stable_regular_text_snapshot(
        proposal_path,
        max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    )
    committed_review_text = stable_regular_text_snapshot(
        review_path,
        max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    )
    if not exact_d5_materials_match(
        proposal_filename=proposal_path.name,
        proposal_text=committed_proposal_text,
        review_text=committed_review_text,
        expected_proposal_text=proposal_text,
        expected_review_text=review_text,
        expected_proposal_digest=proposal_digest,
        expected_review_digest=review_digest,
    ):
        exit_d5_review_recorded_not_committed(
            parser,
            json_mode=json_mode,
            expected_binding_digest=expected_binding_digest,
            extra={"binding_reason_code": "inconsistent_review_material_changed_before_commit"},
        )
    result = evaluate_current_inconsistent_review_binding(
        project_root=project_root,
        storage_root=storage_root,
        state=state,
        state_text=current_state_text,
    )
    if result.status != "current" or result.binding_digest != expected_binding_digest:
        exit_d5_review_recorded_not_committed(
            parser,
            json_mode=json_mode,
            expected_binding_digest=expected_binding_digest,
            extra={
                "binding_status": result.status,
                "binding_reason_code": result.reason_code,
                "current_inconsistent_review_binding_digest": result.binding_digest,
            },
        )

    try:
        atomic_write_and_verify_if_unchanged(
            state_path,
            expected_text=current_state_text,
            new_text=next_state_text,
        )
    except (LockBusyError, OSError, UnicodeDecodeError) as exc:
        observed_state_text = stable_regular_text_snapshot(state_path)
        if observed_state_text == next_state_text:
            pass
        elif observed_state_text == current_state_text:
            exit_d5_review_recorded_not_committed(
                parser,
                json_mode=json_mode,
                expected_binding_digest=expected_binding_digest,
                extra={"error_type": type(exc).__name__},
            )
        elif observed_state_text is not None:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                message=(
                    "The D5 state changed concurrently while promotion was being committed."
                ),
                reason_code="concurrent_external_modification_detected",
                expected_binding_digest=expected_binding_digest,
                side_effect="external_state_modification_preserved",
                extra={"state_rollback_status": "not_attempted_external_state"},
            )
        else:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="The D5 state write outcome is unreadable.",
                reason="damaged_sidecar",
                reason_code="state_write_outcome_unknown",
                expected_binding_digest=expected_binding_digest,
                side_effect="unknown",
                extra={"state_rollback_status": "not_attempted_state_unreadable"},
            )

    post_commit_proposal_text = stable_regular_text_snapshot(
        proposal_path,
        max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    )
    post_commit_review_text = stable_regular_text_snapshot(
        review_path,
        max_bytes=INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES,
    )
    if not exact_d5_materials_match(
        proposal_filename=proposal_path.name,
        proposal_text=post_commit_proposal_text,
        review_text=post_commit_review_text,
        expected_proposal_text=proposal_text,
        expected_review_text=review_text,
        expected_proposal_digest=proposal_digest,
        expected_review_digest=review_digest,
    ):
        post_material_state_text = stable_regular_text_snapshot(state_path)
        if post_material_state_text == next_state_text:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                message=(
                    "D5 proposal or review material changed after state promotion; "
                    "the promoted state and external material were preserved."
                ),
                reason_code="review_imported_baseline_material_invalid",
                expected_binding_digest=expected_binding_digest,
                side_effect="external_target_modification_preserved",
                extra={
                    "binding_reason_code": (
                        "inconsistent_review_material_changed_after_commit"
                    ),
                    "state_promotion_status": "committed_material_binding_invalid",
                },
            )
        if post_material_state_text is not None:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                message=(
                    "D5 material changed after state promotion, and the state also "
                    "changed externally; all external bytes were preserved."
                ),
                reason_code="concurrent_external_modification_detected",
                expected_binding_digest=expected_binding_digest,
                side_effect="external_state_modification_preserved",
                extra={
                    "binding_reason_code": (
                        "inconsistent_review_material_changed_after_commit"
                    )
                },
            )
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "D5 material changed after state promotion, and the current state "
                "cannot be read to determine the write outcome."
            ),
            reason="damaged_sidecar",
            reason_code="state_write_outcome_unknown",
            expected_binding_digest=expected_binding_digest,
            side_effect="unknown",
            extra={
                "binding_reason_code": (
                    "inconsistent_review_material_changed_after_commit"
                )
            },
        )

    post_commit_state_text = stable_regular_text_snapshot(state_path)
    if post_commit_state_text != next_state_text:
        if post_commit_state_text is not None:
            exit_d5_promotion_failure(
                parser,
                json_mode=json_mode,
                message="The D5 state changed after promotion verification.",
                reason_code="concurrent_external_modification_detected",
                expected_binding_digest=expected_binding_digest,
                side_effect="external_state_modification_preserved",
                extra={"state_rollback_status": "not_attempted_external_state"},
            )
        exit_d5_promotion_failure(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="The D5 state became unreadable after promotion.",
            reason="damaged_sidecar",
            reason_code="state_write_outcome_unknown",
            expected_binding_digest=expected_binding_digest,
            side_effect="unknown",
            extra={"state_rollback_status": "not_attempted_state_unreadable"},
        )

    return {
        "proposal_text": proposal_text,
        "review_text": review_text,
        "review_sections": review_sections,
        "review_action": review_action,
        "promotion_ready": True,
        "provenance_state_after": "review_imported_baseline",
        "new_workspace_revision": next_state["workspace_revision"],
        "orphan_review_reused": orphan_reused,
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    expected_inconsistent_review_binding_digest = (
        args.expected_inconsistent_review_binding_digest
    )
    if (
        expected_inconsistent_review_binding_digest is not None
        and not is_sha256_digest(expected_inconsistent_review_binding_digest)
    ):
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=(
                "--expected-inconsistent-review-binding-digest must be a canonical "
                "sha256: digest."
            ),
            reason="invalid_prepared_input",
            details={
                "reason_code": "expected_inconsistent_review_binding_digest_invalid",
                "side_effect": "none",
                "safe_to_retry": False,
            },
        )
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

    source_paths = [args.source_file] if args.source_file else []
    exit_if_startup_scratch_residue_for_sources(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        source_paths=source_paths,
    )
    source_path, body_text, input_mode = read_recovery_source(
        parser,
        json_mode=args.json,
        raw_source_file=args.source_file,
        use_stdin=args.stdin,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        max_input_bytes=(
            INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES
            if expected_inconsistent_review_binding_digest is not None
            else DEFAULT_MAX_INPUT_BYTES
        ),
    )
    if expected_inconsistent_review_binding_digest is None:
        review_errors = validate_recovery_review_text(body_text)
        if review_errors:
            message = "Invalid recovery review content:\n- " + "\n- ".join(review_errors)
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload(
                    "invalid_prepared_input",
                    error=message,
                    details={"review_errors": review_errors},
                ),
            )

    try:
        proposals_dir = ensure_managed_directory_chain(
            workspace.storage_root,
            ("companion", "recovery", "proposals"),
            project_root=workspace.project_root,
            create=False,
        )
    except ManagedDirectorySafetyError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=exc.message,
            payload=cli_failure_payload(
                exc.failure_reason,
                error=exc.message,
                details=exc.details,
            ),
        )
    review_log_dir = workspace.storage_root / "companion" / "recovery" / "review_log"

    proposal_path = resolve_proposal_path(args.proposal_file, proposals_dir, workspace.project_root)
    if not proposal_path.is_file():
        public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
        message = f"Proposal file does not exist: {public_proposal}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )
    if proposal_path.parent != proposals_dir.resolve():
        public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
        message = (
            "Proposal file must live under companion/recovery/proposals/: "
            f"{public_proposal}"
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )
    if not RECOVERY_PROPOSAL_FILE_RE.match(proposal_path.name):
        message = (
            "Proposal filename does not match the expected recovery proposal shape: "
            f"{proposal_path.name}"
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )

    review_path = review_log_dir / f"{proposal_path.stem}.review.md"

    review_sections = extract_structured_sections(body_text, REVIEW_SECTION_ALIASES)
    review_action = classify_review_action(review_sections)
    promotion_ready = promotion_ready_for_action(review_action)
    recorded_at = now_iso_timestamp()
    provenance_state_after = None
    new_workspace_revision = None
    strict_gate_summary = None
    orphan_review_reused = None

    try:
        with workspace_write_lock(workspace.project_root, "record_recovery_review.py"):
            strict_gate_summary = enforce_recovery_import_strict_gate(
                parser,
                json_mode=args.json,
                project_root=workspace.project_root,
                storage_root=workspace.storage_root,
                allow_inconsistent_evidence=(
                    expected_inconsistent_review_binding_digest is not None
                ),
            )
            if expected_inconsistent_review_binding_digest is None:
                gate_state = load_workspace_state(
                    workspace.storage_root / FILE_KEYS["state"]
                )
                gate_provenance = gate_state.get("provenance")
                if isinstance(gate_provenance, dict) and (
                    gate_provenance.get("state_label")
                    == "inconsistent_or_tampered_evidence"
                    or INCONSISTENT_REVIEW_EVIDENCE_KEY in gate_provenance
                ):
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=3,
                        message=(
                            "Inconsistent-evidence review state cannot use the legacy "
                            "recovery review promotion path."
                        ),
                        reason="trust_review_required",
                        details={
                            "reason_code": (
                                "expected_inconsistent_review_binding_digest_required"
                            ),
                            "side_effect": "none",
                            "safe_to_retry": False,
                        },
                    )
            proposals_dir = ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "proposals"),
                project_root=workspace.project_root,
                create=False,
            )
            if proposal_path.parent != proposals_dir.resolve():
                public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
                message = (
                    "Proposal file must live under companion/recovery/proposals/: "
                    f"{public_proposal}"
                )
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=message,
                    payload=cli_failure_payload(
                        "invalid_prepared_input",
                        error=message,
                        details={"proposal_file": str(proposal_path)},
                    ),
                )
            if expected_inconsistent_review_binding_digest is not None:
                review_log_dir = ensure_managed_directory_chain(
                    workspace.storage_root,
                    ("companion", "recovery", "review_log"),
                    project_root=workspace.project_root,
                    create=False,
                )
            else:
                review_log_dir = ensure_managed_directory_chain(
                    workspace.storage_root,
                    ("companion", "recovery", "review_log"),
                    project_root=workspace.project_root,
                )
                ensure_managed_directory_chain(
                    workspace.storage_root,
                    ("companion", "recovery", "archive"),
                    project_root=workspace.project_root,
                )
            review_path = review_log_dir / f"{proposal_path.stem}.review.md"
            if expected_inconsistent_review_binding_digest is not None:
                d5_result = record_d5_inconsistent_review_promotion(
                    parser,
                    json_mode=args.json,
                    project_root=workspace.project_root,
                    storage_root=workspace.storage_root,
                    proposal_path=proposal_path,
                    review_path=review_path,
                    source_path=source_path,
                    prepared_review_text=body_text,
                    expected_binding_digest=expected_inconsistent_review_binding_digest,
                    recorded_at=recorded_at,
                )
                review_sections = d5_result["review_sections"]
                review_action = d5_result["review_action"]
                promotion_ready = d5_result["promotion_ready"]
                provenance_state_after = d5_result["provenance_state_after"]
                new_workspace_revision = d5_result["new_workspace_revision"]
                orphan_review_reused = d5_result["orphan_review_reused"]
            else:
                try:
                    review_path.lstat()
                    review_exists = True
                except FileNotFoundError:
                    review_exists = False
                if review_exists:
                    public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
                    message = f"Refusing to overwrite an existing recovery review: {public_review}"
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=message,
                        payload=cli_failure_payload(
                            "malformed_managed_file",
                            error=message,
                            details={"review_path": str(review_path)},
                        ),
                    )
                ensure_managed_directory_chain(
                    workspace.storage_root,
                    ("companion", "recovery", "review_log"),
                    project_root=workspace.project_root,
                    create=False,
                    )
                proposal_text = read_text(proposal_path)
                stored_review_text = body_text.rstrip("\n") + "\n"
                state_path = workspace.storage_root / FILE_KEYS["state"]
                next_state_text = None
                if promotion_ready:
                    state = load_workspace_state(state_path)
                    state["workspace_revision"] += 1
                    state["provenance"] = review_imported_baseline_metadata(
                        timestamp=recorded_at,
                        review_action=review_action,
                        proposal_digest=text_digest(proposal_text),
                        review_digest=text_digest(stored_review_text),
                    )
                    new_workspace_revision = state["workspace_revision"]
                    next_state_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
                write_text(review_path, stored_review_text)
                if next_state_text is not None:
                    try:
                        write_text(state_path, next_state_text)
                    except OSError:
                        with suppress(FileNotFoundError):
                            review_path.unlink()
                        raise
                    provenance_state_after = "review_imported_baseline"
    except LockBusyError as exc:
        public_message = publicize_text_paths(
            str(exc),
            project_root=workspace.project_root,
        ) or "Refusing to continue because another RecallLoom mutating operation appears to be running."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=public_message,
            payload=cli_failure_payload("write_lock_busy", error=public_message),
        )
    except ManagedDirectorySafetyError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=exc.message,
            payload=cli_failure_payload(
                exc.failure_reason,
                error=exc.message,
                details=exc.details,
            ),
        )
    except (OSError, UnicodeDecodeError, ConfigContractError) as exc:
        message = "Filesystem error while recording recovery review."
        if isinstance(exc, ConfigContractError):
            message = str(exc)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"error_type": type(exc).__name__},
            ),
        )

    payload = {
        "ok": True,
        "proposal_path": str(proposal_path),
        "review_path": str(review_path),
        "input_mode": input_mode,
        "source_file": str(source_path) if source_path is not None else None,
        "source_digest": text_digest(body_text),
        "review_sections_present": sorted(review_sections.keys()),
        "review_action": review_action,
        "promotion_ready": promotion_ready,
        "provenance_state_after": provenance_state_after,
        "new_workspace_revision": new_workspace_revision,
        "workspace_revision_bumped": new_workspace_revision is not None,
        "strict_sidecar_integrity_gate": strict_gate_summary,
        "recorded_at": recorded_at,
    }
    if expected_inconsistent_review_binding_digest is not None:
        payload.update(
            {
                "inconsistent_review_binding_digest": (
                    expected_inconsistent_review_binding_digest
                ),
                "orphan_review_reused": orphan_review_reused,
                "receipt_backed": False,
            }
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
        public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
        print(f"Recorded recovery review: {public_review}")


if __name__ == "__main__":
    main()
