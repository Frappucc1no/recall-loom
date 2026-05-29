"""Read-side trust and drift evaluation helpers for RecallLoom."""

from __future__ import annotations

from core.failure.contracts import failure_reason_contract, normalize_failure_reason
from core.provenance.state import classify_provenance_state


def continuity_drift_risk_level(
    *,
    continuity_confidence: str,
    summary_stale: bool = False,
    workspace_newer_than_summary: bool = False,
    conflict_state: str | None = None,
) -> str:
    if workspace_newer_than_summary or conflict_state == "workspace_artifact_newer_than_summary":
        return "high"
    if summary_stale or conflict_state in {"summary_revision_stale", "multi_source_review_recommended"}:
        return "medium"
    if continuity_confidence == "low":
        return "medium"
    if continuity_confidence == "broken":
        return "high"
    return "none"


def evaluate_trust_state(
    *,
    continuity_confidence: str,
    continuity_state: str,
    summary_stale: bool = False,
    workspace_newer_than_summary: bool = False,
    conflict_state: str | None = None,
    blocked_reason: str | None = None,
    legacy_sidecar: bool = False,
    legacy_review_required: bool = False,
    review_imported_baseline: bool = False,
    helper_evidenced: bool = False,
    inconsistent_evidence: bool = False,
) -> dict:
    sidecar_trust_state = "structurally_valid"
    if blocked_reason is not None:
        contract = failure_reason_contract(normalize_failure_reason(blocked_reason))
        trust_effect = contract["trust_effect"]
        if trust_effect == "damaged":
            sidecar_trust_state = "damaged"
        elif trust_effect == "conflicting":
            sidecar_trust_state = "conflicting"
        elif trust_effect == "security_blocked":
            sidecar_trust_state = "security_blocked"
        elif trust_effect == "review_required":
            sidecar_trust_state = "unknown"
        else:
            sidecar_trust_state = "structurally_valid"
    elif inconsistent_evidence:
        sidecar_trust_state = "security_blocked"
    elif legacy_review_required:
        sidecar_trust_state = "review_required"
    elif legacy_sidecar:
        sidecar_trust_state = "structurally_valid_legacy"

    drift_level = continuity_drift_risk_level(
        continuity_confidence=continuity_confidence,
        summary_stale=summary_stale,
        workspace_newer_than_summary=workspace_newer_than_summary,
        conflict_state=conflict_state,
    )

    if sidecar_trust_state in {"damaged", "conflicting", "security_blocked"}:
        allowed_operation_level = "none"
        read_confidence = "untrusted"
    elif sidecar_trust_state == "review_required":
        allowed_operation_level = "read_current_state"
        read_confidence = "review_required"
    elif continuity_state == "initialized_empty_shell":
        allowed_operation_level = "write_current_state_after_preflight"
        read_confidence = "empty_shell"
    elif review_imported_baseline:
        allowed_operation_level = "write_current_state_after_preflight"
        read_confidence = "review_imported_baseline"
    elif drift_level in {"high", "medium"}:
        allowed_operation_level = "read_current_state"
        read_confidence = "review_recommended"
    else:
        allowed_operation_level = "write_current_state_after_preflight"
        read_confidence = "trusted_current_read"

    provenance_state = classify_provenance_state(
        sidecar_trust_state=sidecar_trust_state,
        continuity_state=continuity_state,
        receipt_chain_verified=False,
        helper_evidenced_baseline=helper_evidenced,
        legacy_sidecar=legacy_sidecar,
        review_required=legacy_review_required,
        review_imported_baseline=review_imported_baseline,
    )

    return {
        "sidecar_trust_state": sidecar_trust_state,
        "provenance_state": provenance_state,
        "continuity_drift_risk_level": drift_level,
        "allowed_operation_level": allowed_operation_level,
        "read_confidence": read_confidence,
        "read_trust_note": (
            "Read-side trust only; this does not relax write gates or cursor guards."
        ),
    }
