"""Deterministic support planner for the plan-only recording workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    BLOCKED_REASON_SET,
    RECORD_CLASS_SET,
    RecordContractError,
    build_record_plan_output,
    digest_payload,
    normalize_record_plan_input,
    validate_layer_name,
    validate_record_class,
)
from .privacy import contains_unsafe_record_text, record_input_text


LAYER_HINT_ALIASES = {
    "daily-log": "daily_log",
    "daily_log": "daily_log",
    "current-state": "current_state",
    "current_state": "current_state",
    "stable-context": "stable_context",
    "stable_context": "stable_context",
    "protocol-rule": "protocol_rule",
    "protocol_rule": "protocol_rule",
    "metadata": "metadata",
}

RECORD_CLASS_ALIASES = {
    "append": "append_only",
    "append-only": "append_only",
    "append_only": "append_only",
    "current-state": "current_state_update",
    "current_state": "current_state_update",
    "current_state_update": "current_state_update",
    "stable-context": "stable_context_update",
    "stable_context": "stable_context_update",
    "stable_context_update": "stable_context_update",
    "protocol-rule": "protocol_rule_update",
    "protocol_rule": "protocol_rule_update",
    "protocol_rule_update": "protocol_rule_update",
    "multi-layer": "simple_multi_layer_plan",
    "multi_layer": "simple_multi_layer_plan",
    "simple_multi_layer_plan": "simple_multi_layer_plan",
    "metadata-refresh": "metadata_refresh_only",
    "metadata_refresh": "metadata_refresh_only",
    "metadata_refresh_only": "metadata_refresh_only",
    "amend": "amend_last",
    "amend_last": "amend_last",
    "duplicate": "duplicate_noop",
    "duplicate_noop": "duplicate_noop",
    "defer": "defer_no_write",
    "defer_no_write": "defer_no_write",
    "no-write": "no_write_success",
    "no_write": "no_write_success",
    "no_write_success": "no_write_success",
    "ambiguous": "ambiguous_needs_user",
    "ambiguous_needs_user": "ambiguous_needs_user",
    "unsafe": "unsafe_blocked",
    "unsafe_blocked": "unsafe_blocked",
}

APPEND_COMMAND = "recallloom.py append same_project --stdin --json"
CURRENT_STATE_COMMAND = (
    "recallloom.py write same_project --type current-state --stdin --input-format json --json"
)
METADATA_REFRESH_COMMAND = (
    "recallloom.py sync-current-state-after-append same_project --reuse-current-summary "
    "--semantic-unchanged-assertion-json same_bound_assertion_json --json"
)
MUTATING_READY_RECORD_CLASSES = frozenset(
    {
        "append_only",
        "current_state_update",
        "metadata_refresh_only",
    }
)
READY_AFTER_PREFLIGHT_LABELS = frozenset(
    {
        "structural_only_ready_after_preflight",
        "helper_evidenced_ready_after_preflight",
        "review_imported_baseline_ready_after_preflight",
    }
)

def normalize_layer_hint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = LAYER_HINT_ALIASES.get(value.strip().casefold().replace(" ", "_"))
    if normalized is None:
        normalized = value.strip().casefold().replace("-", "_")
    return validate_layer_name(normalized)


def normalize_record_class_hint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = RECORD_CLASS_ALIASES.get(value.strip().casefold().replace(" ", "_"))
    if normalized is None:
        normalized = value.strip().casefold().replace("-", "_")
    return validate_record_class(normalized)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _redacted_intent_text(intent_text: str) -> str:
    if not intent_text:
        return ""
    return f"redacted-intent:{digest_payload({'intent_text': intent_text})}"


def _redacted_record_payload(payload: object) -> dict[str, str]:
    if not payload:
        return {}
    return {
        "payload_ref": (
            "redacted-payload:"
            + digest_payload({"prepared_record_payload": payload})
        )
    }


def _privacy_classification(input_contract: Mapping[str, Any]) -> str:
    privacy = input_contract.get("privacy_safety_result")
    if not isinstance(privacy, Mapping):
        return "unknown"
    value = privacy.get("classification") or privacy.get("status") or privacy.get("safety_status")
    return value.strip().casefold().replace("-", "_") if isinstance(value, str) else "unknown"


def _has_no_write_language(text: str) -> bool:
    return _contains_any(
        text,
        (
            "只是探索",
            "还没形成结论",
            "defer",
            "no write",
            "no need to record",
            "no need to log",
            "no record needed",
            "do not record",
            "don't record",
            "dont record",
            "do not log",
            "don't log",
            "dont log",
            "do not save this",
            "don't save this",
            "dont save this",
            "do not write this",
            "don't write this",
            "dont write this",
            "do not write this down",
            "don't write this down",
            "dont write this down",
            "do not append this",
            "don't append this",
            "dont append this",
            "just confirm",
            "just confirming",
            "confirmation only",
            "for confirmation only",
            "only confirming",
            "just checking",
            "无需记录",
            "不用记录",
            "不要记录",
            "不需要记录",
            "不必记录",
            "无需写入",
            "不用写入",
            "不要写入",
            "不用写",
            "不要写",
            "只是确认",
            "仅确认",
            "只是核对",
            "只是检查",
            "只是问一下",
        ),
    )


def classify_record_plan_input(
    input_contract: Mapping[str, Any],
    *,
    record_class_hint: str | None = None,
) -> tuple[str, str]:
    """Return (record_class, confidence) without claiming full semantic understanding."""

    combined, payload_present = record_input_text(input_contract)
    lowered_combined = combined.casefold()
    if contains_unsafe_record_text(combined):
        return "unsafe_blocked", "high"
    normalized = normalize_record_plan_input(input_contract)
    if _privacy_classification(normalized) in {"unsafe", "blocked", "block"}:
        return "unsafe_blocked", "high"
    normalized_hint = normalize_record_class_hint(record_class_hint)
    if normalized_hint == "unsafe_blocked":
        return normalized_hint, "high"

    intent_text = str(input_contract.get("intent_text") or "").casefold()
    if _has_no_write_language(intent_text):
        return "defer_no_write", "medium"

    if normalized_hint is not None:
        return normalized_hint, "high"

    layer_hint = normalized.get("optional_layer_hint")
    if layer_hint == "daily_log":
        return "append_only", "medium"
    if layer_hint == "current_state":
        return "current_state_update", "medium"
    if layer_hint == "stable_context":
        return "stable_context_update", "medium"
    if layer_hint == "protocol_rule":
        return "protocol_rule_update", "medium"
    if layer_hint == "metadata":
        return "metadata_refresh_only", "medium"

    if _contains_any(lowered_combined, ("已经记过", "duplicate", "already recorded", "重复")):
        return "duplicate_noop", "medium"
    if _contains_any(lowered_combined, ("刚才那条", "改一下", "覆盖上一条", "amend", "overwrite")):
        return "amend_last", "medium"
    if _contains_any(lowered_combined, ("记录规则", "protocol rule", "update_protocol", "规则改成")):
        return "protocol_rule_update", "medium"
    if _contains_any(
        lowered_combined,
        (
            "以后默认",
            "长期保留",
            "长期上下文",
            "稳定上下文",
            "项目背景",
            "背景信息",
            "context brief",
            "stable context",
            "default",
            "boundary",
        ),
    ):
        return "stable_context_update", "medium"
    if _contains_any(
        lowered_combined,
        ("既要", "同时", "multi-layer", "multi layer", "current state and log"),
    ):
        return "simple_multi_layer_plan", "medium"
    if _contains_any(
        lowered_combined,
        (
            "当前状态",
            "当前摘要",
            "进展摘要",
            "更新摘要",
            "当前进展",
            "current state",
            "rolling summary",
        ),
    ):
        return "current_state_update", "medium"
    if _contains_any(
        lowered_combined,
        (
            "刚 append 完",
            "刚记录完",
            "metadata refresh",
            "reuse current summary",
            "同步摘要基线",
            "刷新摘要元数据",
            "元数据刷新",
        ),
    ):
        return "metadata_refresh_only", "medium"
    if str(input_contract.get("intent_text") or "").strip() or payload_present:
        return "append_only", "low"
    return "ambiguous_needs_user", "none"


def _expected_revision_binding(preflight_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preflight_payload, Mapping):
        return {}
    binding: dict[str, Any] = {}
    expected_revisions = preflight_payload.get("expected_revisions")
    if isinstance(expected_revisions, Mapping):
        binding["expected_revisions"] = dict(expected_revisions)
    for key in (
        "write_readiness",
        "allowed_operation_level",
        "continuity_state",
        "summary_stale",
        "workspace_newer_than_summary",
        "freshness_risk_level",
        "freshness_risk_note",
        "sidecar_trust_state",
        "provenance_state",
        "provenance_note",
        "continuity_drift_risk_level",
        "continuity_drift_review_required",
        "continuity_drift_note",
        "write_context_blocked_reason",
        "strict_sidecar_integrity_gate",
        "preflight_contract_identity",
    ):
        if key in preflight_payload:
            binding[key] = preflight_payload.get(key)
    binding["preflight_digest"] = digest_payload(preflight_payload)
    return binding


def _path_for_class(record_class: str) -> tuple[str, ...]:
    if record_class == "append_only":
        return (APPEND_COMMAND,)
    if record_class == "current_state_update":
        return (CURRENT_STATE_COMMAND,)
    if record_class == "metadata_refresh_only":
        return (METADATA_REFRESH_COMMAND,)
    if record_class == "simple_multi_layer_plan":
        return (APPEND_COMMAND, CURRENT_STATE_COMMAND)
    return ()


def _required_gates_for_class(record_class: str) -> tuple[str, ...]:
    if record_class == "metadata_refresh_only":
        return (
            "privacy_safety",
            "revision_binding",
            "cursor_binding",
            "preflight_binding",
            "semantic_unchanged",
            "receipt_finalization",
        )
    if record_class in {"append_only", "current_state_update", "simple_multi_layer_plan"}:
        return ("privacy_safety", "prepared_payload", "revision_binding", "preflight_binding")
    if record_class in {"stable_context_update", "protocol_rule_update", "amend_last"}:
        return ("privacy_safety", "prepared_payload", "user_confirmation")
    return ("privacy_safety",)


def _terminal_condition(record_class: str) -> str:
    if record_class == "append_only":
        return "append helper returns ok with expected workspace revision and finalized receipt"
    if record_class == "current_state_update":
        return "write helper returns ok with expected file/workspace revisions and finalized receipt"
    if record_class == "metadata_refresh_only":
        return "post-append sync helper returns ok with bound semantic_unchanged assertion and finalized receipt"
    if record_class == "simple_multi_layer_plan":
        return "all listed helper commands are reviewed and run one at a time after confirmation"
    if record_class == "duplicate_noop":
        return "no write needed because the record is already represented"
    if record_class in {"defer_no_write", "no_write_success"}:
        return "no write needed for this intent"
    if record_class == "unsafe_blocked":
        return "stop until unsafe input is removed or redacted"
    return "user confirms the intended durable memory target"


def _blocked_reason_for_class(record_class: str) -> str | None:
    if record_class == "unsafe_blocked":
        return "unsafe_record_input"
    if record_class == "ambiguous_needs_user":
        return "ambiguous_record_input"
    return None


def _preflight_readiness_label(preflight_payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(preflight_payload, Mapping):
        return None
    write_readiness = preflight_payload.get("write_readiness")
    if not isinstance(write_readiness, Mapping):
        return None
    readiness = write_readiness.get("readiness")
    return readiness if isinstance(readiness, str) else None


def _preflight_allows_metadata_refresh(preflight_payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(preflight_payload, Mapping):
        return False
    safe_write_context = preflight_payload.get("safe_write_context")
    if not isinstance(safe_write_context, Mapping):
        return False
    contract = safe_write_context.get("post_append_summary_sync")
    return isinstance(contract, Mapping) and contract.get("allowed") is True


def _preflight_blocks_mutating_plan(
    record_class: str,
    preflight_payload: Mapping[str, Any] | None,
) -> bool:
    if record_class not in MUTATING_READY_RECORD_CLASSES:
        return False
    if not isinstance(preflight_payload, Mapping) or not preflight_payload:
        return False
    if record_class == "metadata_refresh_only" and _preflight_allows_metadata_refresh(preflight_payload):
        return False
    if preflight_payload.get("write_context_authorized") is False:
        return True
    allowed_operation_level = preflight_payload.get("allowed_operation_level")
    if (
        allowed_operation_level is not None
        and allowed_operation_level != "write_current_state_after_preflight"
    ):
        return True
    readiness_label = _preflight_readiness_label(preflight_payload)
    return readiness_label is not None and readiness_label not in READY_AFTER_PREFLIGHT_LABELS


def _preflight_strict_gate_blocks_write(preflight_payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(preflight_payload, Mapping):
        return False
    strict_gate = preflight_payload.get("strict_sidecar_integrity_gate")
    if isinstance(strict_gate, Mapping) and strict_gate.get("allowed_for_mutation") is False:
        return True
    return preflight_payload.get("write_context_blocked_reason") == "strict_sidecar_integrity_failed"


def plan_record(
    *,
    input_contract: Mapping[str, Any],
    record_class_hint: str | None = None,
    preflight_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a validated plan-only record workflow output."""

    record_class, confidence = classify_record_plan_input(
        input_contract,
        record_class_hint=record_class_hint,
    )
    normalized_input = normalize_record_plan_input(input_contract)
    semantic_assertion = normalized_input.get("semantic_unchanged_assertion")
    semantic_changed_metadata_refresh = (
        record_class == "metadata_refresh_only"
        and semantic_assertion is False
    )
    semantic_unconfirmed_metadata_refresh = (
        record_class == "metadata_refresh_only"
        and semantic_assertion is None
    )
    if record_class == "unsafe_blocked":
        normalized_input["intent_text"] = _redacted_intent_text(normalized_input["intent_text"])
        normalized_input["prepared_record_payload"] = _redacted_record_payload(
            normalized_input.get("prepared_record_payload")
        )
    if record_class not in RECORD_CLASS_SET:
        raise RecordContractError(f"Unsupported record_class: {record_class}")

    path = _path_for_class(record_class)
    blocked_reason = _blocked_reason_for_class(record_class)
    if record_class == "unsafe_blocked":
        workflow_status = "blocked_unsafe"
    elif record_class in {"duplicate_noop", "defer_no_write"}:
        workflow_status = "no_write"
    elif record_class == "no_write_success":
        workflow_status = "complete"
    elif semantic_changed_metadata_refresh:
        workflow_status = "needs_user_confirmation"
        blocked_reason = "semantic_changed"
        path = ()
    elif semantic_unconfirmed_metadata_refresh:
        workflow_status = "needs_user_confirmation"
        blocked_reason = "user_decision_required"
        path = ()
    elif record_class in {
        "stable_context_update",
        "protocol_rule_update",
        "simple_multi_layer_plan",
        "amend_last",
        "ambiguous_needs_user",
    }:
        workflow_status = "needs_user_confirmation"
        blocked_reason = blocked_reason or "user_decision_required"
    else:
        workflow_status = "ready_to_run"

    preflight_blocks_write = _preflight_blocks_mutating_plan(record_class, preflight_payload)
    preflight_strict_blocks_write = _preflight_strict_gate_blocks_write(preflight_payload)
    strict_gate_blocks_mutating_plan = preflight_strict_blocks_write and record_class not in {
        "duplicate_noop",
        "defer_no_write",
        "no_write_success",
        "unsafe_blocked",
    }
    if strict_gate_blocks_mutating_plan:
        workflow_status = "blocked_fixable"
        blocked_reason = "strict_sidecar_integrity_failed"
        path = ()
    elif workflow_status == "ready_to_run" and preflight_blocks_write:
        workflow_status = "blocked_fixable"
        blocked_reason = (
            "strict_sidecar_integrity_failed"
            if preflight_strict_blocks_write
            else "preflight_write_not_ready"
        )
        path = ()

    current_safe_command = path[0] if workflow_status == "ready_to_run" and path else None
    recommended_action = (
        "stop_and_redact_input"
        if record_class == "unsafe_blocked"
        else "review_or_repair_sidecar_before_write"
        if workflow_status == "blocked_fixable" and preflight_strict_blocks_write
        else "resolve_preflight_write_readiness"
        if workflow_status == "blocked_fixable"
        else "prepare_reviewed_current_state_update"
        if semantic_changed_metadata_refresh
        else "confirm_semantic_unchanged_or_route_to_current_state"
        if semantic_unconfirmed_metadata_refresh
        else "ask_one_user_confirmation"
        if workflow_status == "needs_user_confirmation"
        else "no_write_needed"
        if workflow_status in {"no_write", "complete"}
        else "run_current_safe_command"
    )
    safety_notes = (
        "record --plan is side-effect-free",
        "commands are placeholders and do not include user payload",
    )
    planned_layers = (
        ()
        if preflight_strict_blocks_write
        else ("current_state",)
        if semantic_changed_metadata_refresh
        else ("metadata",)
        if semantic_unconfirmed_metadata_refresh
        else None
    )
    write_effect = (
        "none"
        if preflight_strict_blocks_write
        else "planned_only"
        if semantic_changed_metadata_refresh or semantic_unconfirmed_metadata_refresh
        else "planned_only"
        if preflight_blocks_write
        else None
    )
    return build_record_plan_output(
        record_class=record_class,
        workflow_status=workflow_status,
        confidence=confidence,
        write_effect=write_effect,
        planned_layers=planned_layers,
        ordered_executable_path=path,
        current_safe_command=current_safe_command,
        single_next_command=current_safe_command,
        user_decision_required=False if strict_gate_blocks_mutating_plan else None,
        required_gates=_required_gates_for_class(record_class),
        terminal_success_condition=(
            "a reviewed current-state write is separately confirmed and completed"
            if semantic_changed_metadata_refresh
            else "the semantic decision is made and record --plan is rerun with the matching assertion"
            if semantic_unconfirmed_metadata_refresh
            else _terminal_condition(record_class)
        ),
        blocked_reason=blocked_reason,
        expected_revision_binding=_expected_revision_binding(preflight_payload),
        input_contract={
            **normalized_input,
            "expected_revision_binding": _expected_revision_binding(preflight_payload),
        },
        validation_hint=recommended_action,
        safety_notes=safety_notes,
    )
