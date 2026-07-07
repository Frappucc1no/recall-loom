"""Plan-only recording workflow contracts for RecallLoom."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "1.1"
COMMAND = "record"
PLAN_MODE = "plan-only"
PLAN_ONLY_SIDE_EFFECT = "none"
RECORD_PLAN_OUTPUT_ID_PREFIX = "record-plan-output"

WORKFLOW_STATUSES = (
    "ready_to_run",
    "needs_user_confirmation",
    "blocked_fixable",
    "blocked_unsafe",
    "no_write",
    "complete",
)
WORKFLOW_STATUS_SET = frozenset(WORKFLOW_STATUSES)

RECORD_CLASSES = (
    "append_only",
    "current_state_update",
    "stable_context_update",
    "protocol_rule_update",
    "simple_multi_layer_plan",
    "metadata_refresh_only",
    "amend_last",
    "duplicate_noop",
    "defer_no_write",
    "no_write_success",
    "ambiguous_needs_user",
    "unsafe_blocked",
)
RECORD_CLASS_SET = frozenset(RECORD_CLASSES)

CONFIDENCE_LEVELS = ("none", "low", "medium", "high")
CONFIDENCE_LEVEL_SET = frozenset(CONFIDENCE_LEVELS)

WRITE_EFFECTS = (
    "none",
    "planned_only",
    "will_write_if_command_runs",
    "already_written",
)
WRITE_EFFECT_SET = frozenset(WRITE_EFFECTS)

LAYER_NAMES = (
    "daily_log",
    "current_state",
    "stable_context",
    "protocol_rule",
    "metadata",
)
LAYER_NAME_SET = frozenset(LAYER_NAMES)

REQUIRED_GATE_NAMES = (
    "privacy_safety",
    "prepared_payload",
    "revision_binding",
    "cursor_binding",
    "preflight_binding",
    "semantic_unchanged",
    "user_confirmation",
    "package_support",
    "receipt_finalization",
)
REQUIRED_GATE_NAME_SET = frozenset(REQUIRED_GATE_NAMES)

BLOCKED_REASONS = (
    "unsafe_record_input",
    "ambiguous_record_input",
    "invalid_record_contract",
    "stale_revision_binding",
    "missing_cursor_binding",
    "missing_preflight_binding",
    "semantic_changed",
    "user_decision_required",
    "package_support_blocked",
    "unsupported_record_class",
    "no_safe_command",
    "preflight_write_not_ready",
)
BLOCKED_REASON_SET = frozenset(BLOCKED_REASONS)

INPUT_CONTRACT_FIELDS = (
    "intent_text",
    "prepared_record_payload",
    "optional_layer_hint",
    "privacy_safety_result",
    "expected_revision_binding",
    "semantic_unchanged_assertion",
)
INPUT_CONTRACT_FIELD_SET = frozenset(INPUT_CONTRACT_FIELDS)

REQUIRED_OUTPUT_FIELDS = (
    "schema_version",
    "command",
    "plan_mode",
    "workflow_status",
    "record_class",
    "confidence",
    "write_effect",
    "planned_layers",
    "ordered_executable_path",
    "current_safe_command",
    "user_decision_required",
    "single_next_command",
    "remaining_step_count",
    "required_gates",
    "terminal_success_condition",
    "private_safe_for_local_output",
    "public_safe_for_docs_or_release",
    "blocked_reason",
    "failure_shape",
    "safe_retry_template",
    "expected_revision_binding",
    "input_contract",
    "validation_hint",
    "safety_notes",
    "side_effect",
    "record_plan_output_id",
    "input_digest",
)
REQUIRED_OUTPUT_FIELD_SET = frozenset(REQUIRED_OUTPUT_FIELDS)

CONFIRMATION_REQUIRED_RECORD_CLASSES = (
    "stable_context_update",
    "protocol_rule_update",
    "simple_multi_layer_plan",
    "amend_last",
    "ambiguous_needs_user",
)
CONFIRMATION_REQUIRED_RECORD_CLASS_SET = frozenset(CONFIRMATION_REQUIRED_RECORD_CLASSES)

NO_WRITE_RECORD_CLASSES = (
    "duplicate_noop",
    "defer_no_write",
    "no_write_success",
    "ambiguous_needs_user",
    "unsafe_blocked",
)
NO_WRITE_RECORD_CLASS_SET = frozenset(NO_WRITE_RECORD_CLASSES)

_DEFAULT_PLANNED_LAYERS_BY_RECORD_CLASS = {
    "append_only": ("daily_log",),
    "current_state_update": ("current_state",),
    "stable_context_update": ("stable_context",),
    "protocol_rule_update": ("protocol_rule",),
    "simple_multi_layer_plan": ("daily_log", "current_state"),
    "metadata_refresh_only": ("metadata",),
    "amend_last": ("daily_log",),
    "duplicate_noop": (),
    "defer_no_write": (),
    "no_write_success": (),
    "ambiguous_needs_user": (),
    "unsafe_blocked": (),
}

_DEFAULT_WORKFLOW_STATUS_BY_RECORD_CLASS = {
    "append_only": "ready_to_run",
    "current_state_update": "ready_to_run",
    "stable_context_update": "needs_user_confirmation",
    "protocol_rule_update": "needs_user_confirmation",
    "simple_multi_layer_plan": "needs_user_confirmation",
    "metadata_refresh_only": "ready_to_run",
    "amend_last": "needs_user_confirmation",
    "duplicate_noop": "no_write",
    "defer_no_write": "no_write",
    "no_write_success": "complete",
    "ambiguous_needs_user": "needs_user_confirmation",
    "unsafe_blocked": "blocked_unsafe",
}

_DEFAULT_WRITE_EFFECT_BY_RECORD_CLASS = {
    "append_only": "will_write_if_command_runs",
    "current_state_update": "will_write_if_command_runs",
    "stable_context_update": "planned_only",
    "protocol_rule_update": "planned_only",
    "simple_multi_layer_plan": "planned_only",
    "metadata_refresh_only": "will_write_if_command_runs",
    "amend_last": "planned_only",
    "duplicate_noop": "none",
    "defer_no_write": "none",
    "no_write_success": "none",
    "ambiguous_needs_user": "none",
    "unsafe_blocked": "none",
}

_DEFAULT_BLOCKED_REASON_BY_RECORD_CLASS = {
    "ambiguous_needs_user": "ambiguous_record_input",
    "unsafe_blocked": "unsafe_record_input",
}

_INPUT_CONTRACT_DEFAULTS = {
    "intent_text": "",
    "prepared_record_payload": {},
    "optional_layer_hint": None,
    "privacy_safety_result": {"classification": "unknown"},
    "expected_revision_binding": {},
    "semantic_unchanged_assertion": None,
}

_PAYLOAD_SUMMARY_KEY = "_recallloom_payload_summary"
_PAYLOAD_SUMMARY_FIELDS = frozenset(
    (
        "redacted",
        "payload_present",
        "payload_type",
        "payload_digest",
        "top_level_key_count",
        "top_level_keys",
    )
)

_SAFE_RETRY_TEMPLATE_FIELDS = frozenset(
    ("action", "reason", "retry_after", "required_refresh", "redacted_input_id")
)

_UNSAFE_RETRY_TEMPLATE_KEYS = frozenset(
    (
        "command",
        "command_line",
        "current_safe_command",
        "diff",
        "manual_patch",
        "patch",
        "payload",
        "prepared_record_payload",
        "private_payload",
        "raw_payload",
        "shell_command",
    )
)

_UNSAFE_COMMAND_TOKENS = (
    "\n",
    "\r",
    ">",
    "<",
    "|",
    ";",
    "&&",
    "||",
    "`",
    "$(",
    " state.json",
    "/state.json",
    " config.json",
    "/config.json",
    " receipt",
    "/receipt",
    "preflight-bindings",
    " patch",
    "python -c",
    " rm ",
)


class RecordContractError(ValueError):
    """Raised when a record workflow payload violates the frozen contract."""


def canonical_json(value: Any) -> str:
    """Return stable JSON for digesting contract payloads."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RecordContractError(f"Value is not canonical JSON serializable: {exc}") from exc


def digest_payload(value: Any) -> str:
    """Return a prefixed SHA-256 digest for a JSON-compatible value."""

    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _payload_type_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _normalize_payload_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    summary = value.get(_PAYLOAD_SUMMARY_KEY)
    if not isinstance(summary, Mapping):
        return None
    unknown = sorted(set(summary) - _PAYLOAD_SUMMARY_FIELDS)
    if unknown:
        raise RecordContractError(
            f"input_contract.prepared_record_payload.{_PAYLOAD_SUMMARY_KEY} "
            "contains unsupported fields: " + ", ".join(unknown)
        )
    if summary.get("redacted") is not True:
        raise RecordContractError(
            f"input_contract.prepared_record_payload.{_PAYLOAD_SUMMARY_KEY}.redacted must be true."
        )
    payload_digest = summary.get("payload_digest")
    if not isinstance(payload_digest, str) or not payload_digest.startswith("sha256:"):
        raise RecordContractError(
            f"input_contract.prepared_record_payload.{_PAYLOAD_SUMMARY_KEY}.payload_digest "
            "must be a sha256 digest."
        )
    payload_type = summary.get("payload_type")
    if not isinstance(payload_type, str) or not payload_type:
        raise RecordContractError(
            f"input_contract.prepared_record_payload.{_PAYLOAD_SUMMARY_KEY}.payload_type "
            "must be a non-empty string."
        )
    top_level_keys = summary.get("top_level_keys", [])
    if not isinstance(top_level_keys, list) or not all(isinstance(key, str) for key in top_level_keys):
        raise RecordContractError(
            f"input_contract.prepared_record_payload.{_PAYLOAD_SUMMARY_KEY}.top_level_keys "
            "must be a list of strings."
        )
    top_level_key_count = summary.get("top_level_key_count", len(top_level_keys))
    if not isinstance(top_level_key_count, int) or isinstance(top_level_key_count, bool) or top_level_key_count < 0:
        raise RecordContractError(
            f"input_contract.prepared_record_payload.{_PAYLOAD_SUMMARY_KEY}.top_level_key_count "
            "must be a non-negative integer."
        )
    return {
        _PAYLOAD_SUMMARY_KEY: {
            "redacted": True,
            "payload_present": bool(summary.get("payload_present", True)),
            "payload_type": payload_type,
            "payload_digest": payload_digest,
            "top_level_key_count": top_level_key_count,
            "top_level_keys": top_level_keys[:20],
        }
    }


def _summarize_prepared_record_payload(value: Any) -> dict[str, Any]:
    existing_summary = _normalize_payload_summary(value)
    if existing_summary is not None:
        return existing_summary
    normalized = _normalize_json_value(value, "input_contract.prepared_record_payload")
    if normalized == {}:
        return {}
    top_level_keys: list[str] = []
    top_level_key_count = 0
    if isinstance(normalized, Mapping):
        top_level_key_count = len(normalized)
        top_level_keys = [str(key) for key in sorted(normalized)[:20]]
    return {
        _PAYLOAD_SUMMARY_KEY: {
            "redacted": True,
            "payload_present": True,
            "payload_type": _payload_type_name(normalized),
            "payload_digest": digest_payload(normalized),
            "top_level_key_count": top_level_key_count,
            "top_level_keys": top_level_keys,
        }
    }


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecordContractError(f"{field_name} must be a mapping.")
    if not all(isinstance(key, str) for key in value):
        raise RecordContractError(f"{field_name} keys must be strings.")
    return value


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RecordContractError(f"{field_name} must be a string.")
    return value


def _require_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RecordContractError(f"{field_name} must be a boolean.")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RecordContractError(f"{field_name} must be a non-negative integer.")
    return value


def _normalize_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RecordContractError(f"{field_name} keys must be strings.")
        return {
            key: _normalize_json_value(inner_value, f"{field_name}.{key}")
            for key, inner_value in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [
            _normalize_json_value(inner_value, f"{field_name}[]")
            for inner_value in value
        ]
    if value is None or isinstance(value, (str, bool, int, float)):
        canonical_json(value)
        return value
    raise RecordContractError(f"{field_name} is not JSON-compatible.")


def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if isinstance(value, str) or isinstance(value, Mapping) or not isinstance(value, Sequence):
        raise RecordContractError(f"{field_name} must be a sequence of strings.")
    normalized = []
    for item in value:
        normalized.append(_require_string(item, field_name))
    return normalized


def _validate_enum(value: str, field_name: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise RecordContractError(f"Unsupported {field_name}: {value!r}")
    return value


def validate_record_class(value: str) -> str:
    return _validate_enum(value, "record_class", RECORD_CLASS_SET)


def validate_layer_name(value: str) -> str:
    return _validate_enum(value, "planned layer", LAYER_NAME_SET)


def _normalize_planned_layers(value: Any) -> list[str]:
    return [validate_layer_name(layer) for layer in _normalize_string_list(value, "planned_layers")]


def _normalize_required_gates(value: Any) -> list[str]:
    return [
        _validate_enum(gate, "required_gates[]", REQUIRED_GATE_NAME_SET)
        for gate in _normalize_string_list(value, "required_gates")
    ]


def _normalize_command_text(value: Any, field_name: str) -> str | None:
    text = _require_optional_string(value, field_name)
    if text is None:
        return None
    lowered = f" {text.casefold()} "
    for token in _UNSAFE_COMMAND_TOKENS:
        if token in lowered:
            raise RecordContractError(f"{field_name} must not bypass RecallLoom helpers.")
    return text


def _contains_unsafe_retry_key(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if key_text in _UNSAFE_RETRY_TEMPLATE_KEYS:
                return key_text
            nested = _contains_unsafe_retry_key(item)
            if nested is not None:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _contains_unsafe_retry_key(item)
            if nested is not None:
                return nested
    return None


def _normalize_safe_retry_template(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    template = dict(_require_mapping(value, "safe_retry_template"))
    unknown = sorted(set(template) - _SAFE_RETRY_TEMPLATE_FIELDS)
    if unknown:
        raise RecordContractError(
            "safe_retry_template contains unsupported fields: " + ", ".join(unknown)
        )
    unsafe_key = _contains_unsafe_retry_key(template)
    if unsafe_key is not None:
        raise RecordContractError(
            f"safe_retry_template must not carry private payloads or patch commands: {unsafe_key}"
        )
    if "required_refresh" in template:
        template["required_refresh"] = _normalize_required_gates(template["required_refresh"])
    for key in ("action", "reason", "retry_after", "redacted_input_id"):
        if key in template and template[key] is not None:
            template[key] = _require_string(template[key], f"safe_retry_template.{key}")
    return _normalize_json_value(template, "safe_retry_template")


def normalize_record_plan_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a record-plan input contract without claiming semantic understanding."""

    source = _require_mapping(payload, "input_contract")
    unknown = sorted(set(source) - INPUT_CONTRACT_FIELD_SET)
    if unknown:
        raise RecordContractError(f"input_contract contains unsupported fields: {', '.join(unknown)}")

    normalized: dict[str, Any] = {}
    for field_name in INPUT_CONTRACT_FIELDS:
        value = source.get(field_name, _INPUT_CONTRACT_DEFAULTS[field_name])
        if field_name == "prepared_record_payload":
            normalized[field_name] = _summarize_prepared_record_payload(value)
        else:
            normalized[field_name] = _normalize_json_value(value, f"input_contract.{field_name}")

    normalized["intent_text"] = _require_string(normalized["intent_text"], "input_contract.intent_text")
    if not isinstance(normalized["prepared_record_payload"], dict):
        raise RecordContractError("input_contract.prepared_record_payload must be a mapping.")
    layer_hint = normalized["optional_layer_hint"]
    if layer_hint is not None:
        normalized["optional_layer_hint"] = validate_layer_name(
            _require_string(layer_hint, "input_contract.optional_layer_hint")
        )
    if not isinstance(normalized["privacy_safety_result"], dict):
        raise RecordContractError("input_contract.privacy_safety_result must be a mapping.")
    if not isinstance(normalized["expected_revision_binding"], dict):
        raise RecordContractError("input_contract.expected_revision_binding must be a mapping.")
    semantic_assertion = normalized["semantic_unchanged_assertion"]
    if semantic_assertion is not None and not isinstance(semantic_assertion, bool):
        raise RecordContractError("input_contract.semantic_unchanged_assertion must be a boolean or null.")
    return normalized


def _record_plan_output_digest_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "record_plan_output_id"}


def record_plan_output_id(payload: Mapping[str, Any]) -> str:
    """Return the stable record-plan output id for a complete payload."""

    return f"{RECORD_PLAN_OUTPUT_ID_PREFIX}:{digest_payload(_record_plan_output_digest_payload(payload))}"


def _default_user_decision_required(record_class: str, workflow_status: str) -> bool:
    return (
        record_class in CONFIRMATION_REQUIRED_RECORD_CLASS_SET
        or workflow_status == "needs_user_confirmation"
    )


def _default_blocked_reason(record_class: str, workflow_status: str) -> str | None:
    if record_class in _DEFAULT_BLOCKED_REASON_BY_RECORD_CLASS:
        return _DEFAULT_BLOCKED_REASON_BY_RECORD_CLASS[record_class]
    if workflow_status == "needs_user_confirmation":
        return "user_decision_required"
    return None


def _default_failure_shape(blocked_reason: str | None) -> dict[str, str] | None:
    if blocked_reason is None:
        return None
    return {"reason_code": blocked_reason, "side_effect": PLAN_ONLY_SIDE_EFFECT}


def _validate_output_constraints(payload: Mapping[str, Any]) -> None:
    record_class = payload["record_class"]
    workflow_status = payload["workflow_status"]
    write_effect = payload["write_effect"]
    planned_layers = payload["planned_layers"]
    ordered_path = payload["ordered_executable_path"]
    current_safe_command = payload["current_safe_command"]
    user_decision_required = payload["user_decision_required"]
    blocked_reason = payload["blocked_reason"]

    if record_class in NO_WRITE_RECORD_CLASS_SET:
        if write_effect != "none":
            raise RecordContractError(f"{record_class} must use write_effect=none.")
        if planned_layers:
            raise RecordContractError(f"{record_class} must not target writable layers.")
        if ordered_path or current_safe_command is not None:
            raise RecordContractError(f"{record_class} must not expose executable write commands.")
    if workflow_status == "needs_user_confirmation" and not user_decision_required:
        raise RecordContractError("needs_user_confirmation outputs must require a user decision.")
    if record_class in CONFIRMATION_REQUIRED_RECORD_CLASS_SET and not user_decision_required:
        raise RecordContractError(f"{record_class} must require a user decision.")
    if workflow_status in {"blocked_fixable", "blocked_unsafe"} and blocked_reason is None:
        raise RecordContractError(f"{workflow_status} outputs must include blocked_reason.")
    if workflow_status == "blocked_unsafe" and payload["safe_retry_template"] is not None:
        raise RecordContractError("blocked_unsafe outputs must not include a safe_retry_template.")
    if current_safe_command is None and workflow_status == "ready_to_run":
        raise RecordContractError("ready_to_run outputs must include current_safe_command.")


def build_record_plan_output(
    *,
    record_class: str,
    workflow_status: str | None = None,
    confidence: str = "medium",
    write_effect: str | None = None,
    planned_layers: Sequence[str] | None = None,
    ordered_executable_path: Sequence[str] = (),
    current_safe_command: str | None = None,
    user_decision_required: bool | None = None,
    single_next_command: str | None = None,
    remaining_step_count: int | None = None,
    required_gates: Sequence[str] = (),
    terminal_success_condition: str | None = None,
    private_safe_for_local_output: bool = True,
    public_safe_for_docs_or_release: bool = False,
    blocked_reason: str | None = None,
    failure_shape: Mapping[str, Any] | None = None,
    safe_retry_template: Mapping[str, Any] | None = None,
    expected_revision_binding: Mapping[str, Any] | None = None,
    input_contract: Mapping[str, Any] | None = None,
    validation_hint: str | None = None,
    safety_notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build and validate a side-effect-free record-plan output."""

    normalized_record_class = validate_record_class(record_class)
    normalized_workflow_status = _validate_enum(
        workflow_status or _DEFAULT_WORKFLOW_STATUS_BY_RECORD_CLASS[normalized_record_class],
        "workflow_status",
        WORKFLOW_STATUS_SET,
    )
    input_source = dict(input_contract or {})
    if expected_revision_binding is not None:
        input_source["expected_revision_binding"] = _normalize_json_value(
            expected_revision_binding,
            "expected_revision_binding",
        )
    normalized_input_contract = normalize_record_plan_input(input_source)
    normalized_expected_revision_binding = normalized_input_contract["expected_revision_binding"]
    input_digest = digest_payload(normalized_input_contract)
    ordered_path = list(ordered_executable_path)
    if current_safe_command is None and normalized_workflow_status == "ready_to_run" and ordered_path:
        current_safe_command = ordered_path[0]
    if single_next_command is None:
        single_next_command = current_safe_command
    if remaining_step_count is None:
        remaining_step_count = len(ordered_path)
    if user_decision_required is None:
        user_decision_required = _default_user_decision_required(
            normalized_record_class,
            normalized_workflow_status,
        )
    if blocked_reason is None:
        blocked_reason = _default_blocked_reason(
            normalized_record_class,
            normalized_workflow_status,
        )
    if failure_shape is None:
        failure_shape = _default_failure_shape(blocked_reason)
    output = {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "plan_mode": PLAN_MODE,
        "workflow_status": normalized_workflow_status,
        "record_class": normalized_record_class,
        "confidence": confidence,
        "write_effect": write_effect or _DEFAULT_WRITE_EFFECT_BY_RECORD_CLASS[normalized_record_class],
        "planned_layers": list(
            planned_layers
            if planned_layers is not None
            else _DEFAULT_PLANNED_LAYERS_BY_RECORD_CLASS[normalized_record_class]
        ),
        "ordered_executable_path": ordered_path,
        "current_safe_command": current_safe_command,
        "user_decision_required": user_decision_required,
        "single_next_command": single_next_command,
        "remaining_step_count": remaining_step_count,
        "required_gates": list(required_gates),
        "terminal_success_condition": terminal_success_condition,
        "private_safe_for_local_output": private_safe_for_local_output,
        "public_safe_for_docs_or_release": public_safe_for_docs_or_release,
        "blocked_reason": blocked_reason,
        "failure_shape": dict(failure_shape) if failure_shape is not None else None,
        "safe_retry_template": dict(safe_retry_template) if safe_retry_template is not None else None,
        "expected_revision_binding": normalized_expected_revision_binding,
        "input_contract": normalized_input_contract,
        "validation_hint": validation_hint,
        "safety_notes": list(safety_notes),
        "side_effect": PLAN_ONLY_SIDE_EFFECT,
        "record_plan_output_id": "",
        "input_digest": input_digest,
    }
    output["record_plan_output_id"] = record_plan_output_id(output)
    return validate_record_plan_output(output)


def validate_record_plan_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a record-plan output payload."""

    source = _require_mapping(payload, "record_plan_output")
    missing = [field for field in REQUIRED_OUTPUT_FIELDS if field not in source]
    if missing:
        raise RecordContractError(f"record_plan_output is missing required fields: {', '.join(missing)}")
    unknown = sorted(set(source) - REQUIRED_OUTPUT_FIELD_SET)
    if unknown:
        raise RecordContractError(f"record_plan_output contains unsupported fields: {', '.join(unknown)}")
    if source["schema_version"] != SCHEMA_VERSION:
        raise RecordContractError(f"Unsupported schema_version: {source['schema_version']!r}")
    if source["command"] != COMMAND:
        raise RecordContractError(f"Unsupported command: {source['command']!r}")
    if source["plan_mode"] != PLAN_MODE:
        raise RecordContractError(f"Unsupported plan_mode: {source['plan_mode']!r}")
    if source["side_effect"] != PLAN_ONLY_SIDE_EFFECT:
        raise RecordContractError("record plan output side_effect must be 'none'.")

    normalized_input_contract = normalize_record_plan_input(
        _require_mapping(source["input_contract"], "input_contract")
    )
    normalized_expected_revision_binding = _normalize_json_value(
        source["expected_revision_binding"],
        "expected_revision_binding",
    )
    if not isinstance(normalized_expected_revision_binding, dict):
        raise RecordContractError("expected_revision_binding must be a mapping.")
    if normalized_input_contract["expected_revision_binding"] != normalized_expected_revision_binding:
        raise RecordContractError(
            "input_contract.expected_revision_binding must match expected_revision_binding."
        )

    ordered_path = [
        _normalize_command_text(command, f"ordered_executable_path[{index}]") or ""
        for index, command in enumerate(
            _normalize_string_list(source["ordered_executable_path"], "ordered_executable_path")
        )
    ]
    current_safe_command = _normalize_command_text(
        source["current_safe_command"],
        "current_safe_command",
    )
    single_next_command = _normalize_command_text(
        source["single_next_command"],
        "single_next_command",
    )
    if ordered_path and current_safe_command is not None and current_safe_command != ordered_path[0]:
        raise RecordContractError(
            "current_safe_command must match the first ordered_executable_path item."
        )
    if single_next_command is not None and single_next_command != current_safe_command:
        raise RecordContractError("single_next_command must match current_safe_command.")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "plan_mode": PLAN_MODE,
        "workflow_status": _validate_enum(
            _require_string(source["workflow_status"], "workflow_status"),
            "workflow_status",
            WORKFLOW_STATUS_SET,
        ),
        "record_class": validate_record_class(
            _require_string(source["record_class"], "record_class")
        ),
        "confidence": _validate_enum(
            _require_string(source["confidence"], "confidence"),
            "confidence",
            CONFIDENCE_LEVEL_SET,
        ),
        "write_effect": _validate_enum(
            _require_string(source["write_effect"], "write_effect"),
            "write_effect",
            WRITE_EFFECT_SET,
        ),
        "planned_layers": _normalize_planned_layers(source["planned_layers"]),
        "ordered_executable_path": ordered_path,
        "current_safe_command": current_safe_command,
        "user_decision_required": _require_bool(
            source["user_decision_required"],
            "user_decision_required",
        ),
        "single_next_command": single_next_command,
        "remaining_step_count": _require_non_negative_int(
            source["remaining_step_count"],
            "remaining_step_count",
        ),
        "required_gates": _normalize_required_gates(source["required_gates"]),
        "terminal_success_condition": _require_optional_string(
            source["terminal_success_condition"],
            "terminal_success_condition",
        ),
        "private_safe_for_local_output": _require_bool(
            source["private_safe_for_local_output"],
            "private_safe_for_local_output",
        ),
        "public_safe_for_docs_or_release": _require_bool(
            source["public_safe_for_docs_or_release"],
            "public_safe_for_docs_or_release",
        ),
        "blocked_reason": (
            None
            if source["blocked_reason"] is None
            else _validate_enum(
                _require_string(source["blocked_reason"], "blocked_reason"),
                "blocked_reason",
                BLOCKED_REASON_SET,
            )
        ),
        "failure_shape": (
            None
            if source["failure_shape"] is None
            else _normalize_json_value(_require_mapping(source["failure_shape"], "failure_shape"), "failure_shape")
        ),
        "safe_retry_template": _normalize_safe_retry_template(source["safe_retry_template"]),
        "expected_revision_binding": normalized_expected_revision_binding,
        "input_contract": normalized_input_contract,
        "validation_hint": _require_optional_string(source["validation_hint"], "validation_hint"),
        "safety_notes": _normalize_string_list(source["safety_notes"], "safety_notes"),
        "side_effect": PLAN_ONLY_SIDE_EFFECT,
        "record_plan_output_id": _require_string(
            source["record_plan_output_id"],
            "record_plan_output_id",
        ),
        "input_digest": _require_string(source["input_digest"], "input_digest"),
    }

    if normalized["remaining_step_count"] != len(normalized["ordered_executable_path"]):
        raise RecordContractError("remaining_step_count must match ordered_executable_path length.")
    if normalized["public_safe_for_docs_or_release"] and not normalized["private_safe_for_local_output"]:
        raise RecordContractError("public-safe output must also be private-safe locally.")
    _validate_output_constraints(normalized)

    expected_input_digest = digest_payload(normalized_input_contract)
    if normalized["input_digest"] != expected_input_digest:
        raise RecordContractError("input_digest does not match input_contract.")
    expected_output_id = record_plan_output_id(normalized)
    if normalized["record_plan_output_id"] != expected_output_id:
        raise RecordContractError("record_plan_output_id does not match payload.")
    return normalized
