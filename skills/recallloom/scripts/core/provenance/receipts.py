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


def minimal_receipt_schema() -> dict:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "allowed_fields": list(RECEIPT_ALLOWED_FIELDS),
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


def receipt_contract_identity() -> dict:
    payload = {
        "schema": minimal_receipt_schema(),
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
    assert_public_safe_json(payload, project_root=project_root)


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
