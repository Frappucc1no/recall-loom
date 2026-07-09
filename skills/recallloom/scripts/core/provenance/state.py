"""Stable provenance state labels and write-readiness routing helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from core.provenance.receipts import receipt_contract_identity


PROVENANCE_CONTRACT_VERSION = "0.4.2-mvp.2"
PROVENANCE_METADATA_SCHEMA_VERSION = "0.4.2-mvp.1"

PROVENANCE_STATE_LABELS = (
    "structurally_valid",
    "helper_evidenced",
    "review_imported_baseline",
    "unproven_sidecar_state",
    "inconsistent_or_tampered_evidence",
)

TRANSIT_ONLY_LEGACY_PROVENANCE_STATES = (
    "structurally_valid_legacy",
    "review_required",
)

NON_RECEIPT_BACKED_STRUCTURAL_REPAIR_KINDS = (
    "daily_log_cursor_repair",
)

CURSOR_REPAIR_BLOCKING_REASON_CODES = (
    "legacy_import_review_required",
    "provenance_evidence_inconsistent",
    "receipt_evidence_mismatch",
    "direct_state_or_config_edit_detected",
    "metadata_state_review_required",
)

PROVENANCE_ACTION_MATRIX = {
    "structurally_valid": {
        "allowed_actions": [
            "read_continuity",
            "run_preflight",
            "prepare_revision_checked_helper_write",
            "run_existing_revision_checked_helper_write_after_preflight",
        ],
        "blocked_actions": [
            "claim_receipt_backed_provenance",
            "skip_preflight",
            "write_receipt_store",
            "finalize_mutating_receipt",
        ],
        "write_readiness": "structural_only",
        "ux_gate": "warn",
        "ux_gate_requires_confirmation": False,
        "ux_gate_waivable": True,
        "ux_gate_reason": "structural_only_state",
        "note": (
            "Managed files and cursors are structurally readable, but no helper receipt "
            "chain has been verified."
        ),
    },
    "helper_evidenced": {
        "allowed_actions": [
            "read_continuity",
            "run_preflight",
            "prepare_revision_checked_helper_write",
            "run_existing_revision_checked_helper_write_after_preflight",
            "claim_helper_evidenced",
        ],
        "blocked_actions": [
            "skip_preflight",
            "write_receipt_store_without_redaction_contract",
        ],
        "write_readiness": "helper_evidenced",
        "ux_gate": "allow",
        "ux_gate_requires_confirmation": False,
        "ux_gate_waivable": True,
        "ux_gate_reason": "helper_evidenced_state",
        "note": "Structural state is backed by helper receipt evidence.",
    },
    "review_imported_baseline": {
        "allowed_actions": [
            "read_continuity",
            "run_preflight",
            "review_imported_baseline",
            "run_existing_revision_checked_helper_write_after_preflight",
        ],
        "blocked_actions": [
            "claim_receipt_backed_provenance_from_import",
            "run_mutating_helper_write_without_fresh_preflight",
            "finalize_import_receipt_as_helper_evidence",
        ],
        "write_readiness": "review_first",
        "ux_gate": "ask",
        "ux_gate_requires_confirmation": True,
        "ux_gate_waivable": True,
        "ux_gate_reason": "review_imported_baseline",
        "note": (
            "Imported baseline was reviewed; a later revision-checked helper write may "
            "claim helper evidence only after its own receipt finalizes."
        ),
    },
    "unproven_sidecar_state": {
        "allowed_actions": [
            "read_continuity",
            "run_preflight",
            "refresh_or_validate_sidecar_state",
        ],
        "blocked_actions": [
            "claim_receipt_backed_provenance",
            "skip_preflight",
            "run_mutating_helper_write_without_fresh_preflight",
            "finalize_mutating_receipt",
        ],
        "write_readiness": "unproven",
        "ux_gate": "warn",
        "ux_gate_requires_confirmation": False,
        "ux_gate_waivable": True,
        "ux_gate_reason": "unproven_sidecar_state",
        "note": "Sidecar state is readable but not currently proven enough for evidence-sensitive writes.",
    },
    "inconsistent_or_tampered_evidence": {
        "allowed_actions": [
            "read_diagnostic_output",
            "run_validate",
            "prepare_repair_review",
        ],
        "blocked_actions": [
            "claim_receipt_backed_provenance",
            "run_preflight_as_write_authority",
            "run_mutating_helper_write",
            "finalize_mutating_receipt",
        ],
        "write_readiness": "blocked",
        "ux_gate": "block",
        "ux_gate_requires_confirmation": False,
        "ux_gate_waivable": False,
        "ux_gate_reason": "inconsistent_or_tampered_evidence",
        "note": "Evidence or structural state is inconsistent; repair and validate before writing.",
    },
    "structurally_valid_legacy": {
        "allowed_actions": ["read_continuity", "run_preflight"],
        "blocked_actions": [
            "persist_legacy_state_label",
            "claim_receipt_backed_provenance",
            "finalize_mutating_receipt",
            "run_mutating_helper_write_without_review_import",
        ],
        "write_readiness": "readable_legacy",
        "ux_gate": "warn",
        "ux_gate_requires_confirmation": False,
        "ux_gate_waivable": True,
        "ux_gate_reason": "readable_legacy_state",
        "note": (
            "Legacy sidecar is structurally readable, but write authority requires "
            "explicit review or repair import."
        ),
    },
    "review_required": {
        "allowed_actions": ["read_continuity", "run_preflight", "manual_review"],
        "blocked_actions": [
            "persist_legacy_state_label",
            "claim_receipt_backed_provenance",
            "run_mutating_helper_write_without_review",
        ],
        "write_readiness": "review_required",
        "ux_gate": "ask",
        "ux_gate_requires_confirmation": True,
        "ux_gate_waivable": True,
        "ux_gate_reason": "legacy_review_required",
        "note": "Legacy or imported state must be reviewed before any mutating helper write.",
    },
}

_STRUCTURAL_SIDECAR_STATES = {"structurally_valid"}
_INCONSISTENT_SIDECAR_STATES = {"damaged", "conflicting", "security_blocked"}


def normalize_provenance_state(label: str | None) -> str:
    if label == "structurally_valid_legacy":
        return "structurally_valid"
    if label == "review_required":
        return "unproven_sidecar_state"
    if label in PROVENANCE_STATE_LABELS:
        return label
    return "unproven_sidecar_state"


def initial_provenance_metadata(*, timestamp: str) -> dict:
    return {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": "structurally_valid",
        "baseline_kind": "helper_initialized",
        "updated_at": timestamp,
    }


def review_imported_baseline_metadata(
    *,
    timestamp: str,
    review_action: str,
    proposal_digest: str | None = None,
    review_digest: str | None = None,
) -> dict:
    payload = {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": "review_imported_baseline",
        "baseline_kind": "review_import",
        "review_action": review_action,
        "updated_at": timestamp,
        "receipt_backed": False,
    }
    if proposal_digest is not None:
        payload["proposal_digest"] = proposal_digest
    if review_digest is not None:
        payload["review_digest"] = review_digest
    return payload


def helper_evidenced_metadata(
    *,
    timestamp: str,
    previous_state_label: str | None = None,
) -> dict:
    payload = {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": "helper_evidenced",
        "baseline_kind": "helper_receipt_finalized",
        "updated_at": timestamp,
        "receipt_backed": True,
    }
    if previous_state_label is not None:
        payload["previous_state_label"] = previous_state_label
    return payload


def unproven_sidecar_metadata(
    *,
    timestamp: str,
    reason_code: str,
    previous_state_label: str | None = None,
) -> dict:
    payload = {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": "unproven_sidecar_state",
        "baseline_kind": "receipt_evidence_incomplete",
        "updated_at": timestamp,
        "receipt_backed": False,
        "reason_code": reason_code,
    }
    if previous_state_label is not None:
        payload["previous_state_label"] = previous_state_label
    return payload


def inconsistent_evidence_metadata(
    *,
    timestamp: str,
    reason_code: str,
    previous_state_label: str | None = None,
) -> dict:
    payload = {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": "inconsistent_or_tampered_evidence",
        "baseline_kind": "receipt_evidence_inconsistent",
        "updated_at": timestamp,
        "receipt_backed": False,
        "reason_code": reason_code,
    }
    if previous_state_label is not None:
        payload["previous_state_label"] = previous_state_label
    return payload


def _state_provenance_metadata(state: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(state, Mapping):
        return None
    metadata = state.get("provenance")
    return metadata if isinstance(metadata, Mapping) else None


def _copy_current_provenance_metadata(
    state: Mapping[str, Any] | None,
    *,
    fallback_state_label: str,
    fallback_baseline_kind: str,
    fallback_receipt_backed: bool,
) -> dict:
    metadata = _state_provenance_metadata(state)
    if (
        metadata is not None
        and metadata.get("schema_version") == PROVENANCE_METADATA_SCHEMA_VERSION
        and metadata.get("state_label") in PROVENANCE_STATE_LABELS
    ):
        return dict(metadata)
    return {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": fallback_state_label,
        "baseline_kind": fallback_baseline_kind,
        "receipt_backed": fallback_receipt_backed,
    }


def _with_structural_repair_marker(
    metadata: Mapping[str, Any],
    *,
    timestamp: str,
    repair_kind: str,
    previous_state_label: str | None,
    reason_code: str | None,
    trust_effect: str,
) -> dict:
    payload = dict(metadata)
    payload["updated_at"] = timestamp
    payload["last_structural_repair"] = {
        "repair_kind": repair_kind,
        "receipt_backed": False,
        "finalizes_mutating_receipt": False,
        "updated_at": timestamp,
        "trust_effect": trust_effect,
    }
    if previous_state_label is not None:
        payload["last_structural_repair"]["previous_state_label"] = previous_state_label
    if reason_code is not None:
        payload["last_structural_repair"]["reason_code"] = reason_code
        payload["reason_code"] = reason_code
    return payload


def structural_repair_metadata(
    *,
    timestamp: str,
    repair_kind: str,
    previous_state_label: str | None = None,
    reason_code: str | None = None,
) -> dict:
    """Return non receipt-backed metadata for a structural sidecar repair."""

    payload = {
        "schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_label": "structurally_valid",
        "baseline_kind": "structural_repair",
        "updated_at": timestamp,
        "receipt_backed": False,
    }
    if previous_state_label is not None:
        payload["previous_state_label"] = previous_state_label
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return _with_structural_repair_marker(
        payload,
        timestamp=timestamp,
        repair_kind=repair_kind,
        previous_state_label=previous_state_label,
        reason_code=reason_code,
        trust_effect="structural_only",
    )


def provenance_facts_from_state(
    state: Mapping[str, Any] | None,
    *,
    review_intent: bool = False,
) -> dict:
    if not isinstance(state, Mapping):
        return {
            "metadata_status": "state_unavailable",
            "legacy_sidecar": False,
            "review_required": True,
            "review_imported_baseline": False,
            "helper_evidenced": False,
            "inconsistent_evidence": False,
        }

    metadata = state.get("provenance")
    if not isinstance(metadata, Mapping):
        return {
            "metadata_status": "legacy_metadata_missing",
            "legacy_sidecar": not review_intent,
            "review_required": review_intent,
            "review_imported_baseline": False,
            "helper_evidenced": False,
            "inconsistent_evidence": False,
        }

    schema_version = metadata.get("schema_version")
    state_label = metadata.get("state_label")
    if schema_version != PROVENANCE_METADATA_SCHEMA_VERSION:
        return {
            "metadata_status": "metadata_schema_review_required",
            "legacy_sidecar": False,
            "review_required": True,
            "review_imported_baseline": False,
            "helper_evidenced": False,
            "inconsistent_evidence": False,
        }
    if state_label == "helper_evidenced":
        return {
            "metadata_status": "helper_evidenced",
            "legacy_sidecar": False,
            "review_required": False,
            "review_imported_baseline": False,
            "helper_evidenced": True,
            "inconsistent_evidence": False,
        }
    if state_label == "review_imported_baseline":
        return {
            "metadata_status": "review_imported_baseline",
            "legacy_sidecar": False,
            "review_required": False,
            "review_imported_baseline": True,
            "helper_evidenced": False,
            "inconsistent_evidence": False,
        }
    if state_label == "structurally_valid":
        return {
            "metadata_status": "current_structural_baseline",
            "legacy_sidecar": False,
            "review_required": False,
            "review_imported_baseline": False,
            "helper_evidenced": False,
            "inconsistent_evidence": False,
        }
    if state_label == "inconsistent_or_tampered_evidence":
        return {
            "metadata_status": "inconsistent_or_tampered_evidence",
            "legacy_sidecar": False,
            "review_required": False,
            "review_imported_baseline": False,
            "helper_evidenced": False,
            "inconsistent_evidence": True,
        }
    return {
        "metadata_status": "metadata_state_review_required",
        "legacy_sidecar": False,
        "review_required": True,
        "review_imported_baseline": False,
        "helper_evidenced": False,
        "inconsistent_evidence": False,
    }


def bounded_evidence_supports_helper_evidenced(
    state: Mapping[str, Any] | None,
    *,
    bounded_receipt_evidence_verified: bool = False,
    receipt_store_available: bool = False,
) -> bool:
    """Return whether current bounded evidence can preserve helper evidence.

    This is a current-state check only. It is not a historical receipt-chain audit.
    """

    provenance_facts = provenance_facts_from_state(state, review_intent=True)
    return bool(
        provenance_facts["helper_evidenced"]
        and bounded_receipt_evidence_verified
        and receipt_store_available
    )


def cursor_repair_provenance_decision(
    state: Mapping[str, Any] | None,
    *,
    timestamp: str,
    bounded_evidence_supports_helper_evidenced: bool = False,
    repair_kind: str = "daily_log_cursor_repair",
    evidence_block_reason_code: str | None = None,
) -> dict:
    """Decide provenance metadata for non receipt-backed daily-log cursor repair.

    `evidence_block_reason_code` is for independent safety findings such as receipt
    mismatch, direct state/config edits, or tampered evidence. A plain cursor
    mismatch that this helper is repairing should not be passed as that reason.
    """

    if repair_kind not in NON_RECEIPT_BACKED_STRUCTURAL_REPAIR_KINDS:
        raise ValueError(f"Unsupported structural repair kind: {repair_kind}")

    metadata = _state_provenance_metadata(state)
    previous_state_label = (
        metadata.get("state_label")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("state_label"), str)
        else None
    )
    provenance_facts = provenance_facts_from_state(state, review_intent=True)
    result = {
        "allowed": True,
        "repair_kind": repair_kind,
        "receipt_backed": False,
        "receipt_store_write_allowed": False,
        "finalizes_mutating_receipt": False,
        "requires_full_receipt_chain_audit": False,
        "bounded_evidence_supports_helper_evidenced": bounded_evidence_supports_helper_evidenced,
        "previous_state_label": previous_state_label,
        "metadata_status": provenance_facts["metadata_status"],
        "blocked_reason_code": None,
        "route": "apply_structural_repair",
    }

    if evidence_block_reason_code is not None:
        return {
            **result,
            "allowed": False,
            "blocked_reason_code": evidence_block_reason_code,
            "route": "validate_and_repair_review",
            "result_state_label": "inconsistent_or_tampered_evidence",
            "provenance_metadata": None,
            "trust_effect": "none",
            "note": (
                "Structural repair must not proceed while independent evidence checks "
                "report tampering, receipt mismatch, or direct state/config edits."
            ),
        }

    if provenance_facts["inconsistent_evidence"]:
        return {
            **result,
            "allowed": False,
            "blocked_reason_code": "provenance_evidence_inconsistent",
            "route": "validate_and_repair_review",
            "result_state_label": "inconsistent_or_tampered_evidence",
            "provenance_metadata": None,
            "trust_effect": "none",
            "note": "Inconsistent provenance evidence must be reviewed before repair.",
        }

    if provenance_facts["review_required"] or provenance_facts["legacy_sidecar"]:
        return {
            **result,
            "allowed": False,
            "blocked_reason_code": "legacy_import_review_required",
            "route": "review_or_repair_import",
            "result_state_label": "review_required",
            "provenance_metadata": None,
            "trust_effect": "none",
            "note": "Legacy or unknown provenance must route through review/repair import first.",
        }

    if provenance_facts["review_imported_baseline"]:
        preserved = _copy_current_provenance_metadata(
            state,
            fallback_state_label="review_imported_baseline",
            fallback_baseline_kind="review_import",
            fallback_receipt_backed=False,
        )
        preserved["state_label"] = "review_imported_baseline"
        preserved["receipt_backed"] = False
        metadata_payload = _with_structural_repair_marker(
            preserved,
            timestamp=timestamp,
            repair_kind=repair_kind,
            previous_state_label=previous_state_label,
            reason_code="review_imported_baseline_preserved",
            trust_effect="structural_only",
        )
        return {
            **result,
            "result_state_label": "review_imported_baseline",
            "provenance_metadata": metadata_payload,
            "trust_effect": "structural_only",
            "note": "Reviewed import baseline is preserved as non receipt-backed provenance.",
        }

    if provenance_facts["helper_evidenced"] and bounded_evidence_supports_helper_evidenced:
        preserved = _copy_current_provenance_metadata(
            state,
            fallback_state_label="helper_evidenced",
            fallback_baseline_kind="helper_receipt_finalized",
            fallback_receipt_backed=True,
        )
        preserved["state_label"] = "helper_evidenced"
        preserved["receipt_backed"] = True
        metadata_payload = _with_structural_repair_marker(
            preserved,
            timestamp=timestamp,
            repair_kind=repair_kind,
            previous_state_label=previous_state_label,
            reason_code="bounded_evidence_preserved_helper_evidenced",
            trust_effect="helper_evidence_preserved_by_bounded_check",
        )
        return {
            **result,
            "result_state_label": "helper_evidenced",
            "provenance_metadata": metadata_payload,
            "trust_effect": "helper_evidence_preserved_by_bounded_check",
            "note": (
                "Helper-evidenced provenance is preserved only because current bounded "
                "evidence independently supports it."
            ),
        }

    reason_code = (
        "bounded_evidence_missing_for_helper_evidenced"
        if provenance_facts["helper_evidenced"]
        else "non_receipt_backed_structural_repair"
    )
    metadata_payload = structural_repair_metadata(
        timestamp=timestamp,
        repair_kind=repair_kind,
        previous_state_label=previous_state_label,
        reason_code=reason_code,
    )
    return {
        **result,
        "result_state_label": "structurally_valid",
        "provenance_metadata": metadata_payload,
        "trust_effect": "structural_only",
        "note": (
            "Cursor repair restores structural cursor metadata only; it does not prove "
            "historical daily-log origin or finalize helper receipt evidence."
        ),
    }


def classify_provenance_state(
    *,
    sidecar_trust_state: str | None,
    continuity_state: str | None = None,
    receipt_chain_verified: bool = False,
    receipt_store_available: bool = False,
    imported_baseline_review_required: bool = False,
    helper_evidenced_baseline: bool = False,
    legacy_sidecar: bool = False,
    review_required: bool = False,
    review_imported_baseline: bool = False,
) -> str:
    if sidecar_trust_state in _INCONSISTENT_SIDECAR_STATES:
        return "inconsistent_or_tampered_evidence"
    if receipt_chain_verified and receipt_store_available:
        return "helper_evidenced"
    if imported_baseline_review_required or review_imported_baseline:
        return "review_imported_baseline"
    if review_required or sidecar_trust_state == "review_required":
        return "review_required"
    if legacy_sidecar or sidecar_trust_state == "structurally_valid_legacy":
        return "structurally_valid_legacy"
    if sidecar_trust_state in _STRUCTURAL_SIDECAR_STATES:
        return "structurally_valid"
    if continuity_state == "initialized_empty_shell":
        return "unproven_sidecar_state"
    return "unproven_sidecar_state"


def provenance_contract_identity() -> dict:
    payload = {
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "metadata_schema_version": PROVENANCE_METADATA_SCHEMA_VERSION,
        "state_labels": PROVENANCE_STATE_LABELS,
        "transit_only_legacy_states": TRANSIT_ONLY_LEGACY_PROVENANCE_STATES,
        "action_matrix": PROVENANCE_ACTION_MATRIX,
        "receipt_contract": receipt_contract_identity(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "contract_name": "recallloom.provenance_core",
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_hash": f"sha256:{digest}",
    }


def legacy_v046_pre_recording_provenance_contract_identity() -> dict:
    """Return the v0.4.6 pre-recording identity accepted for stored receipts."""

    return {
        "contract_name": "recallloom.provenance_core",
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_hash": "sha256:48dfffb1deaa3d8421c93b22fa9af727658efc1bf9e153e52c18713b6411b0ba",
    }


def accepted_preflight_contract_identities_for_receipts() -> tuple[dict, ...]:
    """Return preflight identities accepted for already-finalized receipts."""

    return (
        provenance_contract_identity(),
        legacy_v046_pre_recording_provenance_contract_identity(),
    )


def preflight_write_binding_hash(binding: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in binding.items()
        if key != "preflight_contract_hash"
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def action_matrix_for_state(state_label: str | None) -> dict:
    matrix_label = (
        state_label
        if state_label in PROVENANCE_ACTION_MATRIX
        else normalize_provenance_state(state_label)
    )
    matrix = PROVENANCE_ACTION_MATRIX[matrix_label]
    return {
        "allowed_actions": list(matrix["allowed_actions"]),
        "blocked_actions": list(matrix["blocked_actions"]),
        "write_readiness": matrix["write_readiness"],
        "ux_gate": matrix["ux_gate"],
        "ux_gate_requires_confirmation": matrix["ux_gate_requires_confirmation"],
        "ux_gate_waivable": matrix["ux_gate_waivable"],
        "ux_gate_reason": matrix["ux_gate_reason"],
        "note": matrix["note"],
        "stable_state_label": normalize_provenance_state(matrix_label),
    }


def expected_revisions_payload(
    *,
    workspace_revision: int | None,
    rolling_summary_revision: int | None,
    context_brief_revision: int | None = None,
    update_protocol_revision: int | None = None,
) -> dict:
    return {
        "workspace_revision": workspace_revision,
        "rolling_summary_revision": rolling_summary_revision,
        "context_brief_revision": context_brief_revision,
        "update_protocol_revision": update_protocol_revision,
    }


def build_write_readiness(
    *,
    provenance_state: str,
    allowed_operation_level: str | None,
    summary_stale: bool | None,
    expected_revisions: Mapping[str, Any] | None = None,
    receipt_chain_verified: bool = False,
    receipt_store_available: bool = False,
) -> dict:
    state_label = (
        provenance_state
        if provenance_state in PROVENANCE_ACTION_MATRIX
        else normalize_provenance_state(provenance_state)
    )
    matrix = action_matrix_for_state(state_label)
    preflight_gate_open = (
        allowed_operation_level == "write_current_state_after_preflight"
        and summary_stale is False
    )

    if state_label == "inconsistent_or_tampered_evidence":
        readiness = "blocked"
        next_action = "Run validation or repair review before any mutating helper write."
    elif state_label == "review_required":
        readiness = "review_required"
        next_action = (
            "Review or repair-import the legacy sidecar through the recovery proposal, "
            "review, and promotion helpers before any mutating helper write."
        )
    elif state_label == "structurally_valid_legacy":
        readiness = "readable_legacy"
        next_action = (
            "Read continuity normally, then run preflight/recovery review before any "
            "mutating helper write."
        )
    elif preflight_gate_open and state_label == "structurally_valid":
        readiness = "structural_only_ready_after_preflight"
        next_action = (
            "Use the existing revision-checked helper write path with the expected revisions; "
            "do not claim receipt-backed provenance."
        )
    elif preflight_gate_open and state_label == "helper_evidenced":
        readiness = "helper_evidenced_ready_after_preflight"
        next_action = "Use the revision-checked helper write path with verified receipt evidence."
    elif (
        allowed_operation_level == "write_current_state_after_preflight"
        and state_label == "review_imported_baseline"
    ):
        readiness = "review_imported_baseline_ready_after_preflight"
        next_action = (
            "Use the revision-checked helper write path; claim helper evidence only if "
            "that write finalizes its own receipt."
        )
    else:
        readiness = matrix["write_readiness"]
        next_action = "Refresh preflight context or review current continuity before writing."

    payload = {
        "state_label": state_label,
        "stable_state_label": normalize_provenance_state(state_label),
        "readiness": readiness,
        "allowed_operation_level": allowed_operation_level,
        "summary_stale": summary_stale,
        "allowed_actions": matrix["allowed_actions"],
        "blocked_actions": matrix["blocked_actions"],
        "ux_gate": matrix["ux_gate"],
        "ux_gate_requires_confirmation": matrix["ux_gate_requires_confirmation"],
        "ux_gate_waivable": matrix["ux_gate_waivable"],
        "ux_gate_reason": matrix["ux_gate_reason"],
        "next_action": next_action,
        "receipt_chain_verified": receipt_chain_verified,
        "receipt_store_available": receipt_store_available,
        "receipt_finalization_enabled": False,
        "note": matrix["note"],
    }
    if expected_revisions is not None:
        payload["expected_revisions"] = dict(expected_revisions)
    return payload


def build_provenance_report(
    *,
    sidecar_trust_state: str | None,
    continuity_state: str | None,
    allowed_operation_level: str | None,
    summary_stale: bool | None,
    expected_revisions: Mapping[str, Any] | None = None,
    receipt_chain_verified: bool = False,
    receipt_store_available: bool = False,
    imported_baseline_review_required: bool = False,
    helper_evidenced_baseline: bool = False,
    legacy_sidecar: bool = False,
    review_required: bool = False,
    review_imported_baseline: bool = False,
    metadata_status: str | None = None,
) -> dict:
    state_label = classify_provenance_state(
        sidecar_trust_state=sidecar_trust_state,
        continuity_state=continuity_state,
        receipt_chain_verified=receipt_chain_verified,
        receipt_store_available=receipt_store_available,
        imported_baseline_review_required=imported_baseline_review_required,
        helper_evidenced_baseline=helper_evidenced_baseline,
        legacy_sidecar=legacy_sidecar,
        review_required=review_required,
        review_imported_baseline=review_imported_baseline,
    )
    return {
        "state_label": state_label,
        "stable_state_label": normalize_provenance_state(state_label),
        "metadata_status": metadata_status,
        "contract_identity": provenance_contract_identity(),
        "legacy_transit_only_states": list(TRANSIT_ONLY_LEGACY_PROVENANCE_STATES),
        "receipt_contract": receipt_contract_identity(),
        "action_matrix": action_matrix_for_state(state_label),
        "write_readiness": build_write_readiness(
            provenance_state=state_label,
            allowed_operation_level=allowed_operation_level,
            summary_stale=summary_stale,
            expected_revisions=expected_revisions,
            receipt_chain_verified=receipt_chain_verified,
            receipt_store_available=receipt_store_available,
        ),
    }


def helper_write_gate_from_state(
    state: Mapping[str, Any] | None,
    *,
    helper_name: str,
    operation_class: str,
    preflight_binding_present: bool = False,
    require_preflight_for_review_imported_baseline: bool = False,
    receipt_chain_verified: bool = False,
    receipt_store_available: bool = False,
) -> dict:
    """Return the provenance gate for a helper that is about to mutate sidecar state."""

    provenance_facts = provenance_facts_from_state(state, review_intent=True)
    sidecar_trust_state = (
        "security_blocked"
        if provenance_facts["inconsistent_evidence"]
        else "review_required"
        if provenance_facts["review_required"]
        else "structurally_valid"
    )
    state_label = classify_provenance_state(
        sidecar_trust_state=sidecar_trust_state,
        continuity_state=None,
        receipt_chain_verified=receipt_chain_verified,
        receipt_store_available=receipt_store_available,
        helper_evidenced_baseline=provenance_facts["helper_evidenced"],
        legacy_sidecar=provenance_facts["legacy_sidecar"],
        review_required=provenance_facts["review_required"],
        review_imported_baseline=provenance_facts["review_imported_baseline"],
    )
    write_readiness = build_write_readiness(
        provenance_state=state_label,
        allowed_operation_level=(
            "write_current_state_after_preflight" if preflight_binding_present else None
        ),
        summary_stale=False if preflight_binding_present else None,
        expected_revisions=None,
        receipt_chain_verified=receipt_chain_verified,
        receipt_store_available=receipt_store_available,
    )

    allowed = True
    blocked_reason_code = None
    if state_label in {"review_required", "structurally_valid_legacy"}:
        allowed = False
        blocked_reason_code = "legacy_import_review_required"
    elif state_label == "inconsistent_or_tampered_evidence":
        allowed = False
        blocked_reason_code = "provenance_evidence_inconsistent"
    elif (
        state_label == "review_imported_baseline"
        and require_preflight_for_review_imported_baseline
        and not preflight_binding_present
    ):
        allowed = False
        blocked_reason_code = "preflight_required_for_review_imported_baseline"
    elif not preflight_binding_present:
        allowed = False
        blocked_reason_code = "preflight_required_for_mutating_helper_write"

    return {
        "allowed": allowed,
        "helper_name": helper_name,
        "operation_class": operation_class,
        "blocked_reason_code": blocked_reason_code,
        "provenance_state": state_label,
        "stable_provenance_state": normalize_provenance_state(state_label),
        "provenance_metadata_status": provenance_facts["metadata_status"],
        "write_readiness": write_readiness,
        "preflight_binding_present": preflight_binding_present,
        "requires_preflight_for_review_imported_baseline": (
            require_preflight_for_review_imported_baseline
        ),
    }
