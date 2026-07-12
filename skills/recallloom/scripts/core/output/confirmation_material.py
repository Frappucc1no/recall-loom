"""User-visible confirmation material for RecallLoom approval boundaries."""

from __future__ import annotations

from typing import TextIO


CONFIRMATION_TYPES = frozenset(
    {
        "record_append",
        "current_state_write",
        "metadata_sync",
        "repair_preview",
        "repair_apply",
        "stable_rule_write",
        "protocol_rule_write",
        "release_surface_action",
    }
)

TARGET_LAYERS_OR_SURFACES = frozenset(
    {
        "daily_log",
        "current_state",
        "stable_context",
        "protocol_rule",
        "metadata",
        "repair_state",
        "public_surface",
    }
)

EXPECTED_SIDE_EFFECTS = frozenset(
    {
        "none",
        "single_append",
        "managed_file_write",
        "metadata_only_sync",
        "repair_apply",
        "public_surface_change",
    }
)

RISK_LEVELS = frozenset({"low", "medium", "high", "blocked"})

MATERIAL_FIELDS = (
    "confirmation_type",
    "user_visible_category",
    "action_summary",
    "why_needed",
    "target_layer_or_surface",
    "expected_side_effect",
    "files_or_keys_summary",
    "safety_gate_summary",
    "risk_level",
    "approval_actor_required",
    "approval_scope",
    "safe_retry_or_rollback",
    "debug_reference",
)


def _required_enum(value: str, *, field: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return value


def _public_sentence(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return " ".join(value.strip().split())


def build_confirmation_material(
    *,
    confirmation_type: str,
    action_summary: str,
    why_needed: str,
    target_layer_or_surface: str,
    expected_side_effect: str,
    files_or_keys_summary: str,
    safety_gate_summary: str,
    risk_level: str,
    approval_scope: str,
    safe_retry_or_rollback: str | None = None,
    debug_reference: str | None = None,
    user_visible_category: str = "needs_confirmation",
    approval_actor_required: str = "user_or_operator",
) -> dict[str, str | None]:
    """Build the frozen v0.4.7 confirmation material shape."""

    if user_visible_category != "needs_confirmation":
        raise ValueError("confirmation material must use user_visible_category=needs_confirmation.")
    if approval_actor_required != "user_or_operator":
        raise ValueError("confirmation material cannot name the generating agent as the approver.")
    material = {
        "confirmation_type": _required_enum(
            confirmation_type,
            field="confirmation_type",
            allowed=CONFIRMATION_TYPES,
        ),
        "user_visible_category": "needs_confirmation",
        "action_summary": _public_sentence(action_summary, field="action_summary"),
        "why_needed": _public_sentence(why_needed, field="why_needed"),
        "target_layer_or_surface": _required_enum(
            target_layer_or_surface,
            field="target_layer_or_surface",
            allowed=TARGET_LAYERS_OR_SURFACES,
        ),
        "expected_side_effect": _required_enum(
            expected_side_effect,
            field="expected_side_effect",
            allowed=EXPECTED_SIDE_EFFECTS,
        ),
        "files_or_keys_summary": _public_sentence(
            files_or_keys_summary,
            field="files_or_keys_summary",
        ),
        "safety_gate_summary": _public_sentence(
            safety_gate_summary,
            field="safety_gate_summary",
        ),
        "risk_level": _required_enum(risk_level, field="risk_level", allowed=RISK_LEVELS),
        "approval_actor_required": "user_or_operator",
        "approval_scope": _public_sentence(approval_scope, field="approval_scope"),
        "safe_retry_or_rollback": (
            _public_sentence(safe_retry_or_rollback, field="safe_retry_or_rollback")
            if safe_retry_or_rollback is not None
            else None
        ),
        "debug_reference": (
            _public_sentence(debug_reference, field="debug_reference")
            if debug_reference is not None
            else None
        ),
    }
    if material["risk_level"] == "blocked" and material["expected_side_effect"] != "none":
        raise ValueError("blocked confirmation material cannot carry a mutating side effect.")
    return material


def record_plan_confirmation_material(payload: dict, user_summary: dict) -> dict[str, str | None] | None:
    """Return confirmation material for record-plan paths that need a user decision."""

    if user_summary.get("category") != "needs_confirmation":
        return None
    record_class = payload.get("record_class")
    record_id = payload.get("record_plan_output_id")
    debug_reference = record_id if isinstance(record_id, str) and record_id else None
    common = {
        "user_visible_category": "needs_confirmation",
        "approval_actor_required": "user_or_operator",
        "debug_reference": debug_reference,
    }
    input_contract = payload.get("input_contract")
    semantic_changed_metadata_refresh = (
        record_class == "metadata_refresh_only"
        and isinstance(input_contract, dict)
        and input_contract.get("semantic_unchanged_assertion") is False
    )
    semantic_unconfirmed_metadata_refresh = (
        record_class == "metadata_refresh_only"
        and isinstance(input_contract, dict)
        and input_contract.get("semantic_unchanged_assertion") is None
    )
    if semantic_changed_metadata_refresh:
        return build_confirmation_material(
            confirmation_type="current_state_write",
            action_summary="Confirm a reviewed current-state update instead of a metadata-only refresh.",
            why_needed="The current-state meaning changed after the append, so metadata-only reconciliation is not safe.",
            target_layer_or_surface="current_state",
            expected_side_effect="managed_file_write",
            files_or_keys_summary="rolling_summary current_state key",
            safety_gate_summary="Privacy, reviewed-content, package-support, preflight, revision and receipt gates must pass before a current-state write.",
            risk_level="medium",
            approval_scope="Approve only one reviewed current-state write; no metadata-only sync, append, repair or release action is included.",
            safe_retry_or_rollback="Decline by stopping without writing, then rerun record --plan for a reviewed current-state update when ready.",
            **common,
        )
    if semantic_unconfirmed_metadata_refresh:
        return build_confirmation_material(
            confirmation_type="metadata_sync",
            action_summary="Confirm whether current-state meaning stayed unchanged before preparing a metadata-only refresh.",
            why_needed="Metadata-only reconciliation is safe only after an explicit semantic-unchanged decision.",
            target_layer_or_surface="metadata",
            expected_side_effect="none",
            files_or_keys_summary="rolling_summary metadata decision only",
            safety_gate_summary="No sync command is exposed until the semantic decision is explicit; later preflight, revision, cursor, receipt and bound-assertion gates still apply.",
            risk_level="medium",
            approval_scope="Confirm only whether meaning stayed unchanged; this material does not approve metadata sync or any file write.",
            safe_retry_or_rollback="Decline by stopping without writing, or route changed meaning to a reviewed current-state update.",
            **common,
        )
    if record_class == "stable_context_update":
        return build_confirmation_material(
            confirmation_type="stable_rule_write",
            action_summary="Confirm a stable project-memory rule before any helper write is prepared.",
            why_needed="Stable context changes affect future restores and must not be inferred silently.",
            target_layer_or_surface="stable_context",
            expected_side_effect="managed_file_write",
            files_or_keys_summary="context_brief stable_context key",
            safety_gate_summary="Privacy, prepared-payload, package-support, preflight, revision and receipt gates must pass before a write.",
            risk_level="medium",
            approval_scope="Approve only this stable-context write target; no append, validation, repair or release action is included.",
            safe_retry_or_rollback="Decline by stopping without writing, then rerun record --plan with a narrower intent if needed.",
            **common,
        )
    if record_class == "protocol_rule_update":
        return build_confirmation_material(
            confirmation_type="protocol_rule_write",
            action_summary="Confirm a project update-protocol rule before any helper write is prepared.",
            why_needed="Protocol-rule changes alter future write routing and require explicit operator approval.",
            target_layer_or_surface="protocol_rule",
            expected_side_effect="managed_file_write",
            files_or_keys_summary="update_protocol protocol_rule key",
            safety_gate_summary="Privacy, prepared-payload, package-support, preflight, revision and receipt gates must pass before a write.",
            risk_level="medium",
            approval_scope="Approve only this protocol-rule write target; no append, validation, repair or release action is included.",
            safe_retry_or_rollback="Decline by stopping without writing, then rerun record --plan with the intended rule made explicit.",
            **common,
        )
    if record_class == "simple_multi_layer_plan":
        return build_confirmation_material(
            confirmation_type="record_append",
            action_summary="Confirm the first daily-log step of a multi-layer recording plan.",
            why_needed="A multi-layer plan must be reviewed one helper action at a time instead of auto-approved as a batch.",
            target_layer_or_surface="daily_log",
            expected_side_effect="single_append",
            files_or_keys_summary="daily_log first step; current_state step remains separate",
            safety_gate_summary="Privacy, prepared-payload, package-support, preflight, revision/cursor and receipt gates must pass for each helper action.",
            risk_level="medium",
            approval_scope="Approve only the first daily-log append step; any current-state write needs its own confirmation.",
            safe_retry_or_rollback="Decline by stopping without writing, then split the record intent into one layer and rerun record --plan.",
            **common,
        )
    if record_class == "amend_last":
        return build_confirmation_material(
            confirmation_type="record_append",
            action_summary="Confirm the intended daily-log amendment target before any write path is prepared.",
            why_needed="Amending previous memory can rewrite intent, so the target and scope must be explicit.",
            target_layer_or_surface="daily_log",
            expected_side_effect="none",
            files_or_keys_summary="daily_log target not yet selected",
            safety_gate_summary="Privacy and prepared-payload gates must pass before any later helper path is prepared.",
            risk_level="medium",
            approval_scope="Approve only the amendment target decision; this material does not approve a write.",
            safe_retry_or_rollback="Decline by stopping without writing, then provide the exact entry or date to review.",
            **common,
        )
    if record_class == "ambiguous_needs_user":
        return build_confirmation_material(
            confirmation_type="record_append",
            action_summary="Choose one durable memory target before RecallLoom prepares a write action.",
            why_needed="The record intent is not specific enough to select one safe layer silently.",
            target_layer_or_surface="metadata",
            expected_side_effect="none",
            files_or_keys_summary="no managed file selected yet",
            safety_gate_summary="Privacy and prepared-payload gates must pass before any later append or managed-file write.",
            risk_level="low",
            approval_scope="Approve only the target-layer choice; no RecallLoom file write is approved by this material.",
            safe_retry_or_rollback="Decline by stopping without writing, then rerun record --plan with a clearer target.",
            **common,
        )
    return None


def review_imported_baseline_confirmation_material(
    *,
    command: str,
    operation: str,
    target_layer_or_surface: str,
    expected_side_effect: str,
    files_or_keys_summary: str,
    approval_scope: str,
    debug_reference: str | None = None,
) -> dict[str, str | None]:
    """Confirmation material for review_imported_baseline ask gates."""

    confirmation_type = {
        "daily_log_append": "record_append",
        "managed_file_commit": "current_state_write",
        "post_append_summary_sync": "metadata_sync",
    }.get(operation, "current_state_write")
    if target_layer_or_surface == "stable_context":
        confirmation_type = "stable_rule_write"
    elif target_layer_or_surface == "protocol_rule":
        confirmation_type = "protocol_rule_write"
    return build_confirmation_material(
        confirmation_type=confirmation_type,
        action_summary=(
            f"Confirm one {operation.replace('_', ' ')} on a reviewed imported baseline."
        ),
        why_needed=(
            "The sidecar is readable but not helper-evidenced, so mutation needs explicit user/operator confirmation."
        ),
        target_layer_or_surface=target_layer_or_surface,
        expected_side_effect=expected_side_effect,
        files_or_keys_summary=files_or_keys_summary,
        safety_gate_summary=(
            "Package support, preflight, strict sidecar integrity, revision/cursor and receipt/provenance gates must pass."
        ),
        risk_level="medium",
        approval_scope=approval_scope,
        safe_retry_or_rollback=(
            "Decline by stopping without writing; retry only after reviewing preflight and adding the explicit confirmation flag."
        ),
        debug_reference=debug_reference or f"{command}:{operation}:review_imported_baseline",
    )


def confirmation_material_lines(
    material: dict[str, str | None],
    *,
    title: str = "RecallLoom confirmation material",
    include_debug_reference: bool = False,
) -> list[str]:
    lines = [title]
    for field in MATERIAL_FIELDS:
        if field == "debug_reference" and not include_debug_reference:
            continue
        value = material.get(field)
        if value is not None:
            lines.append(f"{field}: {value}")
    return lines


def print_confirmation_material(
    material: dict[str, str | None],
    *,
    title: str = "RecallLoom confirmation material",
    file: TextIO | None = None,
    include_debug_reference: bool = False,
) -> None:
    for line in confirmation_material_lines(
        material,
        title=title,
        include_debug_reference=include_debug_reference,
    ):
        print(line, file=file)
