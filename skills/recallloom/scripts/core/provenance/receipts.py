"""Minimal receipt schema and redaction contract for provenance MVP surfaces."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from core.output.privacy import redact_public_text


RECEIPT_SCHEMA_VERSION = "0.2"
RECEIPT_REDACTION_CONTRACT_VERSION = "0.2"
RECEIPT_REDACTION_POLICY_VERSION = "0.2"
RECEIPT_DIGEST_ALGORITHM = "sha256"
RECEIPT_FINALIZATION_STATUSES = ("pending", "finalized", "failed")
RECEIPT_OPERATION_CLASSES = (
    "managed_file_commit",
    "daily_log_append",
    "post_append_summary_sync",
)

RECEIPT_ALLOWED_FIELDS = (
    "schema_version",
    "receipt_type",
    "helper_name",
    "helper_version",
    "operation",
    "operation_class",
    "side_effect",
    "result",
    "finalization_status",
    "redaction_policy_version",
    "state_label_before",
    "state_label_after",
    "revision",
    "digest",
    "target_file_key",
    "target_digest",
    "state_digest",
    "preflight_contract_identity",
    "expected_workspace_revision",
    "result_workspace_revision",
    "expected_file_revision",
    "result_file_revision",
    "contract_identity",
    "store_binding",
    "created_at",
    "assertion_source_kind",
    "assertion_source_id",
    "assertion_payload_digest",
    "input_digest",
    "preflight_binding_digest",
    "managed_body_digest_before",
    "assertion_binding_seed_digest",
    "controlled_metadata_refresh",
)
RECEIPT_V045_ADDED_V046_FIELDS = (
    "assertion_source_kind",
    "assertion_source_id",
    "assertion_payload_digest",
    "input_digest",
    "preflight_binding_digest",
    "managed_body_digest_before",
    "assertion_binding_seed_digest",
    "controlled_metadata_refresh",
)
RECEIPT_V045_ALLOWED_FIELDS = tuple(
    field for field in RECEIPT_ALLOWED_FIELDS if field not in RECEIPT_V045_ADDED_V046_FIELDS
)

RECEIPT_PROHIBITED_FIELDS = (
    "absolute_path",
    "artifact_path",
    "command",
    "command_line",
    "environment",
    "env",
    "full_payload",
    "host_memory_payload",
    "raw_payload",
    "remote_response",
    "remote_service_response",
    "secret",
    "shell_transcript",
    "sidecar_body",
    "source_path",
    "target_path",
    "token",
)

CONTROLLED_METADATA_REFRESH_ALLOWED_CHANGED_FIELDS = (
    "rolling_summary_last_writer.tool",
    "rolling_summary_last_writer.date",
    "file_state.revision",
    "file_state.updated_at",
    "file_state.writer_id",
    "file_state.base_workspace_revision",
)

RECEIPT_REDACTION_CONTRACT = {
    "contract_name": "recallloom.receipt_redaction",
    "contract_version": RECEIPT_REDACTION_CONTRACT_VERSION,
    "redaction_policy_version": RECEIPT_REDACTION_POLICY_VERSION,
    "prohibited_content": list(RECEIPT_PROHIBITED_FIELDS),
    "stores_payload": False,
    "stores_absolute_paths": False,
    "stores_commands": False,
    "stores_shell_transcripts": False,
    "stores_sidecar_bodies": False,
    "stores_host_memory_payloads": False,
    "stores_remote_service_responses": False,
}

_PRIVATE_PATH_OR_URL_RE = re.compile(
    r"(?i)(?:https?://|file://|(?<![A-Za-z0-9._-])(?:~|/|[A-Za-z]:[\\/]))"
)
_PRIVATE_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|token|secret|password|credential)\s*[:=]|"
    r"\bsk-[A-Za-z0-9_-]{6,}\b|"
    r"\bghp_[A-Za-z0-9_]{6,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{6,}\b|"
    r"\bbearer\s+[A-Za-z0-9._-]+)"
)
_PRIVATE_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class ReceiptPrivacyError(ValueError):
    """Raised when a receipt or binding would persist private data."""

    def __init__(self, message: str, *, field_path: str, reason_code: str) -> None:
        super().__init__(message)
        self.field_path = field_path
        self.reason_code = reason_code
        self.details = {"field_path": field_path, "reason_code": reason_code}


class ReceiptContractError(ValueError):
    """Raised when a receipt does not match the minimal receipt contract."""

    def __init__(self, message: str, *, field_path: str = "$", reason_code: str) -> None:
        super().__init__(message)
        self.field_path = field_path
        self.reason_code = reason_code
        self.details = {"field_path": field_path, "reason_code": reason_code}


def _minimal_receipt_schema_for_allowed_fields(allowed_fields: tuple[str, ...]) -> dict:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "allowed_fields": list(allowed_fields),
        "required": [
            "schema_version",
            "receipt_type",
            "helper_name",
            "operation",
            "operation_class",
            "side_effect",
            "result",
            "finalization_status",
            "redaction_policy_version",
            "digest",
            "contract_identity",
        ],
        "digest_algorithm": RECEIPT_DIGEST_ALGORITHM,
        "operation_classes": list(RECEIPT_OPERATION_CLASSES),
        "finalization_statuses": list(RECEIPT_FINALIZATION_STATUSES),
        "redaction_contract": RECEIPT_REDACTION_CONTRACT,
    }


def minimal_receipt_schema() -> dict:
    return _minimal_receipt_schema_for_allowed_fields(RECEIPT_ALLOWED_FIELDS)


def _receipt_contract_identity_for_allowed_fields(allowed_fields: tuple[str, ...]) -> dict:
    payload = {
        "schema": _minimal_receipt_schema_for_allowed_fields(allowed_fields),
        "redaction_contract": RECEIPT_REDACTION_CONTRACT,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "contract_name": "recallloom.minimal_receipt",
        "contract_version": RECEIPT_SCHEMA_VERSION,
        "contract_hash": f"sha256:{digest}",
    }


def receipt_contract_identity() -> dict:
    return _receipt_contract_identity_for_allowed_fields(RECEIPT_ALLOWED_FIELDS)


def legacy_v045_receipt_contract_identity() -> dict:
    """Return the v0.4.5 receipt identity accepted for read/append compatibility."""

    return _receipt_contract_identity_for_allowed_fields(RECEIPT_V045_ALLOWED_FIELDS)


def accepted_receipt_contract_identities() -> tuple[dict, ...]:
    return (
        receipt_contract_identity(),
        legacy_v045_receipt_contract_identity(),
    )


def _is_prohibited_field(key: str) -> bool:
    normalized = key.strip().casefold().replace("-", "_")
    return normalized in RECEIPT_PROHIBITED_FIELDS or any(
        normalized.endswith(f"_{field}") for field in RECEIPT_PROHIBITED_FIELDS
    )


def _text_requires_redaction(text: str) -> bool:
    return bool(
        _PRIVATE_PATH_OR_URL_RE.search(text)
        or _PRIVATE_SECRET_RE.search(text)
        or _PRIVATE_EMAIL_RE.search(text)
    )


def _redact_if_needed(
    text: str,
    *,
    project_root: str | None,
) -> str:
    if not _text_requires_redaction(text):
        return text
    redacted = redact_public_text(text, project_root=project_root, private=False)
    return redacted if isinstance(redacted, str) else text


def assert_public_safe_json(
    value: Any,
    *,
    project_root: str | None = None,
    field_path: str = "$",
) -> None:
    """Reject receipt JSON values that would need redaction before publication."""

    if isinstance(value, Mapping):
        for key, child_value in value.items():
            if not isinstance(key, str):
                raise ReceiptPrivacyError(
                    "Receipt JSON object keys must be strings.",
                    field_path=field_path,
                    reason_code="non_string_key",
                )
            child_path = f"{field_path}.{key}" if field_path else key
            if _is_prohibited_field(key):
                raise ReceiptPrivacyError(
                    f"Receipt field '{child_path}' is prohibited by the redaction policy.",
                    field_path=child_path,
                    reason_code="prohibited_field",
                )
            assert_public_safe_json(
                child_value,
                project_root=project_root,
                field_path=child_path,
            )
        return
    if isinstance(value, list):
        for index, child_value in enumerate(value):
            assert_public_safe_json(
                child_value,
                project_root=project_root,
                field_path=f"{field_path}[{index}]",
        )
        return
    if isinstance(value, str):
        redacted = _redact_if_needed(value, project_root=project_root)
        if redacted != value:
            raise ReceiptPrivacyError(
                f"Receipt field '{field_path}' is not public-safe under the redaction policy.",
                field_path=field_path,
                reason_code="value_requires_redaction",
            )
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ReceiptPrivacyError(
        f"Receipt field '{field_path}' uses unsupported JSON value type.",
        field_path=field_path,
        reason_code="unsupported_value_type",
    )


def canonical_receipt_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_digest_for_json(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_receipt_json(payload).encode("utf-8")).hexdigest()
    return f"{RECEIPT_DIGEST_ALGORITHM}:{digest}"


def receipt_digest_payload(payload: Mapping[str, Any]) -> dict:
    return {key: value for key, value in payload.items() if key not in {"digest", "store_binding"}}


def receipt_payload_digest(payload: Mapping[str, Any]) -> str:
    return sha256_digest_for_json(receipt_digest_payload(payload))


def validate_receipt_payload(
    payload: Mapping[str, Any],
    *,
    project_root: str | None = None,
) -> None:
    allowed = set(RECEIPT_ALLOWED_FIELDS)
    unknown = sorted(key for key in payload if key not in allowed)
    if unknown:
        raise ReceiptContractError(
            "Receipt contains fields outside the minimal receipt schema.",
            reason_code="unknown_receipt_field",
            field_path=",".join(unknown),
        )
    missing = [
        key
        for key in minimal_receipt_schema()["required"]
        if key not in payload
    ]
    if missing:
        raise ReceiptContractError(
            "Receipt is missing required fields.",
            reason_code="missing_required_receipt_field",
            field_path=",".join(missing),
        )
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ReceiptContractError(
            "Receipt schema version does not match the active contract.",
            reason_code="receipt_schema_version_mismatch",
            field_path="$.schema_version",
        )
    if payload.get("operation_class") not in RECEIPT_OPERATION_CLASSES:
        raise ReceiptContractError(
            "Receipt operation class is not supported.",
            reason_code="unsupported_operation_class",
            field_path="$.operation_class",
        )
    if payload.get("finalization_status") not in RECEIPT_FINALIZATION_STATUSES:
        raise ReceiptContractError(
            "Receipt finalization status is not supported.",
            reason_code="unsupported_finalization_status",
            field_path="$.finalization_status",
        )
    if payload.get("redaction_policy_version") != RECEIPT_REDACTION_POLICY_VERSION:
        raise ReceiptContractError(
            "Receipt redaction policy version does not match the active contract.",
            reason_code="redaction_policy_version_mismatch",
            field_path="$.redaction_policy_version",
        )
    expected_digest = receipt_payload_digest(payload)
    if payload.get("digest") != expected_digest:
        raise ReceiptContractError(
            "Receipt digest does not match the canonical receipt payload.",
            reason_code="receipt_digest_mismatch",
            field_path="$.digest",
        )
    _validate_controlled_metadata_refresh(payload)
    assert_public_safe_json(payload, project_root=project_root)


def _is_sha256_reference(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _validate_metadata_snapshot(value: Any, *, field_path: str) -> None:
    if not isinstance(value, Mapping):
        raise ReceiptContractError(
            "controlled_metadata_refresh snapshot must be an object.",
            reason_code="controlled_metadata_refresh_snapshot_invalid",
            field_path=field_path,
        )
    allowed = {"file_marker", "file_state", "rolling_summary_last_writer"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ReceiptContractError(
            "controlled_metadata_refresh snapshot contains unsupported fields.",
            reason_code="controlled_metadata_refresh_snapshot_unknown_field",
            field_path=f"{field_path}.{','.join(unknown)}",
        )
    file_marker = value.get("file_marker")
    file_state = value.get("file_state")
    if not isinstance(file_marker, Mapping) or not isinstance(file_state, Mapping):
        raise ReceiptContractError(
            "controlled_metadata_refresh snapshot is missing file marker or file state.",
            reason_code="controlled_metadata_refresh_snapshot_invalid",
            field_path=field_path,
        )
    if set(file_marker) != {"file_key", "version", "language"}:
        raise ReceiptContractError(
            "controlled_metadata_refresh file marker shape is invalid.",
            reason_code="controlled_metadata_refresh_file_marker_invalid",
            field_path=f"{field_path}.file_marker",
        )
    if set(file_state) != {"revision", "updated_at", "writer_id", "base_workspace_revision"}:
        raise ReceiptContractError(
            "controlled_metadata_refresh file state shape is invalid.",
            reason_code="controlled_metadata_refresh_file_state_invalid",
            field_path=f"{field_path}.file_state",
        )
    if not all(isinstance(file_marker.get(key), str) and file_marker.get(key) for key in file_marker):
        raise ReceiptContractError(
            "controlled_metadata_refresh file marker values must be non-empty strings.",
            reason_code="controlled_metadata_refresh_file_marker_invalid",
            field_path=f"{field_path}.file_marker",
        )
    for key in ("revision", "base_workspace_revision"):
        if not isinstance(file_state.get(key), int) or isinstance(file_state.get(key), bool):
            raise ReceiptContractError(
                "controlled_metadata_refresh file state revisions must be integers.",
                reason_code="controlled_metadata_refresh_file_state_invalid",
                field_path=f"{field_path}.file_state.{key}",
            )
    for key in ("updated_at", "writer_id"):
        if not isinstance(file_state.get(key), str) or not file_state.get(key):
            raise ReceiptContractError(
                "controlled_metadata_refresh file state metadata must be non-empty strings.",
                reason_code="controlled_metadata_refresh_file_state_invalid",
                field_path=f"{field_path}.file_state.{key}",
            )
    last_writer = value.get("rolling_summary_last_writer")
    if last_writer is not None:
        if not isinstance(last_writer, Mapping) or set(last_writer) != {"tool", "date"}:
            raise ReceiptContractError(
                "controlled_metadata_refresh rolling_summary_last_writer shape is invalid.",
                reason_code="controlled_metadata_refresh_last_writer_invalid",
                field_path=f"{field_path}.rolling_summary_last_writer",
            )
        if not all(isinstance(last_writer.get(key), str) and last_writer.get(key) for key in last_writer):
            raise ReceiptContractError(
                "controlled_metadata_refresh rolling_summary_last_writer values must be non-empty strings.",
                reason_code="controlled_metadata_refresh_last_writer_invalid",
                field_path=f"{field_path}.rolling_summary_last_writer",
            )


def _validate_controlled_metadata_refresh(payload: Mapping[str, Any]) -> None:
    claim = payload.get("controlled_metadata_refresh")
    if claim is None:
        return
    if payload.get("operation_class") != "post_append_summary_sync":
        raise ReceiptContractError(
            "controlled_metadata_refresh is only valid for post_append_summary_sync receipts.",
            reason_code="controlled_metadata_refresh_operation_invalid",
            field_path="$.controlled_metadata_refresh",
        )
    if not isinstance(claim, Mapping):
        raise ReceiptContractError(
            "controlled_metadata_refresh must be an object.",
            reason_code="controlled_metadata_refresh_invalid",
            field_path="$.controlled_metadata_refresh",
        )
    expected_keys = {
        "refresh_kind",
        "allowed_changed_fields",
        "body_digest_before",
        "body_digest_after",
        "before",
        "after",
    }
    unknown = sorted(set(claim) - expected_keys)
    missing = sorted(expected_keys - set(claim))
    if unknown or missing:
        raise ReceiptContractError(
            "controlled_metadata_refresh fields do not match the active contract.",
            reason_code="controlled_metadata_refresh_shape_invalid",
            field_path="$.controlled_metadata_refresh",
        )
    if claim.get("refresh_kind") != "metadata_only":
        raise ReceiptContractError(
            "controlled_metadata_refresh refresh_kind is invalid.",
            reason_code="controlled_metadata_refresh_kind_invalid",
            field_path="$.controlled_metadata_refresh.refresh_kind",
        )
    if claim.get("allowed_changed_fields") != list(CONTROLLED_METADATA_REFRESH_ALLOWED_CHANGED_FIELDS):
        raise ReceiptContractError(
            "controlled_metadata_refresh allowed_changed_fields changed unexpectedly.",
            reason_code="controlled_metadata_refresh_allowed_fields_invalid",
            field_path="$.controlled_metadata_refresh.allowed_changed_fields",
        )
    if not _is_sha256_reference(claim.get("body_digest_before")) or not _is_sha256_reference(claim.get("body_digest_after")):
        raise ReceiptContractError(
            "controlled_metadata_refresh body digests must be sha256 references.",
            reason_code="controlled_metadata_refresh_body_digest_invalid",
            field_path="$.controlled_metadata_refresh",
        )
    if claim.get("body_digest_before") != claim.get("body_digest_after"):
        raise ReceiptContractError(
            "controlled_metadata_refresh body digest changed.",
            reason_code="controlled_metadata_refresh_body_changed",
            field_path="$.controlled_metadata_refresh.body_digest_after",
        )
    _validate_metadata_snapshot(claim.get("before"), field_path="$.controlled_metadata_refresh.before")
    _validate_metadata_snapshot(claim.get("after"), field_path="$.controlled_metadata_refresh.after")


def _public_claim_value(
    value: Any,
    *,
    project_root: str | None,
) -> Any:
    if isinstance(value, str):
        return _redact_if_needed(value, project_root=project_root)
    if isinstance(value, (int, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        claim: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or _is_prohibited_field(child_key):
                continue
            safe_value = _public_claim_value(child_value, project_root=project_root)
            if safe_value is not None or child_value is None:
                claim[child_key] = safe_value
        return claim
    if isinstance(value, list):
        return [
            _public_claim_value(item, project_root=project_root)
            for item in value
            if isinstance(item, (str, int, bool, type(None), Mapping))
        ]
    return None


def public_receipt_claim(
    payload: Mapping[str, Any],
    *,
    project_root: str | None = None,
) -> dict:
    """Return a public-safe receipt claim without storing raw receipt payloads."""

    claim: dict[str, Any] = {}
    for key in RECEIPT_ALLOWED_FIELDS:
        if key not in payload or _is_prohibited_field(key):
            continue
        value = payload[key]
        safe_value = _public_claim_value(value, project_root=project_root)
        if safe_value is not None or value is None:
            claim[key] = safe_value
    claim.setdefault("schema_version", RECEIPT_SCHEMA_VERSION)
    return claim
