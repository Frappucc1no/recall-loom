"""Dispatcher-issued preflight write binding leases.

T050-07C final phase: persisted lease helpers remain only for historical
read-only diagnosis. Final mutation authority is process-local and is never
serialized, persisted, or derived from a lease.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from core.failure.context import LEGACY_OPERATION_BY_OPERATION, OPERATION_DOMAIN
from core.provenance.receipts import assert_public_safe_json
from core.workspace.atomic_io import atomic_write_bytes
from core.workspace.runtime import WorkspaceLockHandle, observe_workspace_lock


PREFLIGHT_BINDING_STORE_RELATIVE_PATH = "derived/preflight-bindings.json"
PREFLIGHT_BINDING_STORE_SCHEMA_VERSION = "0.4.2-mvp.1"
PREFLIGHT_BINDING_LEASE_VERSION = "0.1"
PREFLIGHT_BINDING_LEASE_TYPE = "recallloom.preflight_write_binding_lease"
MAX_PREFLIGHT_BINDING_LEASES = 48


class PreflightBindingLeaseError(ValueError):
    """Raised when a direct helper binding is not backed by a dispatcher lease."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        field_path: str = "$",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.field_path = field_path
        self.details = {"reason_code": reason_code, "field_path": field_path}


def preflight_binding_store_path(storage_root: str | Path) -> Path:
    return Path(storage_root) / PREFLIGHT_BINDING_STORE_RELATIVE_PATH


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def preflight_payload_digest(preflight_payload: Mapping[str, Any]) -> str:
    return _sha256_json(preflight_payload)


def _empty_store() -> dict:
    return {
        "schema_version": PREFLIGHT_BINDING_STORE_SCHEMA_VERSION,
        "store_type": "recallloom.preflight_write_binding_leases",
        "records": [],
    }


def _load_store(path: Path) -> dict:
    if not path.exists():
        return _empty_store()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightBindingLeaseError(
            f"Preflight binding lease store is unreadable: {exc}",
            reason_code="preflight_binding_lease_store_unreadable",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PREFLIGHT_BINDING_STORE_SCHEMA_VERSION
        or payload.get("store_type") != "recallloom.preflight_write_binding_leases"
        or not isinstance(payload.get("records"), list)
    ):
        raise PreflightBindingLeaseError(
            "Preflight binding lease store does not match the expected contract.",
            reason_code="preflight_binding_lease_store_invalid",
        )
    return payload


def _write_store(path: Path, payload: Mapping[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # The staged temp + fsync + os.replace mechanism is owned by
    # core.workspace.atomic_io.
    atomic_write_bytes(path, data)


def _lease_digest_payload(record: Mapping[str, Any]) -> dict:
    return {key: value for key, value in record.items() if key != "lease_digest"}


def _build_lease_record(
    *,
    binding: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    issued_by: str,
    issued_at: str | None,
) -> dict:
    record = {
        "lease_type": PREFLIGHT_BINDING_LEASE_TYPE,
        "lease_version": PREFLIGHT_BINDING_LEASE_VERSION,
        "issued_by": issued_by,
        "issued_at": issued_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "binding_hash": binding.get("preflight_contract_hash"),
        "preflight_payload_digest": preflight_payload_digest(preflight_payload),
        "operation_class": binding.get("operation_class"),
        "file_key": binding.get("file_key"),
        "write_type": binding.get("write_type"),
        "contract_type": binding.get("contract_type"),
        "expected_workspace_revision": binding.get("expected_workspace_revision"),
        "expected_file_revision": binding.get("expected_file_revision"),
        "preflight_contract_identity": binding.get("preflight_contract_identity"),
    }
    record["lease_digest"] = _sha256_json(record)
    return record


def _validate_record(record: Any, *, project_root: str | Path) -> dict:
    if not isinstance(record, dict):
        raise PreflightBindingLeaseError(
            "Preflight binding lease record must be a JSON object.",
            reason_code="preflight_binding_lease_record_invalid",
        )
    if record.get("lease_type") != PREFLIGHT_BINDING_LEASE_TYPE:
        raise PreflightBindingLeaseError(
            "Preflight binding lease record has an unexpected type.",
            reason_code="preflight_binding_lease_type_mismatch",
            field_path="$.lease_type",
        )
    if record.get("lease_version") != PREFLIGHT_BINDING_LEASE_VERSION:
        raise PreflightBindingLeaseError(
            "Preflight binding lease record has an unexpected version.",
            reason_code="preflight_binding_lease_version_mismatch",
            field_path="$.lease_version",
        )
    expected_digest = _sha256_json(_lease_digest_payload(record))
    if record.get("lease_digest") != expected_digest:
        raise PreflightBindingLeaseError(
            "Preflight binding lease digest does not match its canonical record.",
            reason_code="preflight_binding_lease_digest_mismatch",
            field_path="$.lease_digest",
        )
    try:
        assert_public_safe_json(record, project_root=str(project_root))
    except ValueError as exc:
        details = getattr(exc, "details", {})
        raise PreflightBindingLeaseError(
            "Preflight binding lease record is not public-safe.",
            reason_code=str(details.get("reason_code") or "preflight_binding_lease_privacy_violation"),
            field_path=str(details.get("field_path") or "$"),
        ) from exc
    return record


def write_preflight_binding_lease(
    *,
    storage_root: str | Path,
    project_root: str | Path,
    binding: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    issued_by: str,
    issued_at: str | None = None,
) -> dict:
    record = _build_lease_record(
        binding=binding,
        preflight_payload=preflight_payload,
        issued_by=issued_by,
        issued_at=issued_at,
    )
    _validate_record(record, project_root=project_root)
    path = preflight_binding_store_path(storage_root)
    store = _load_store(path)
    records = [
        _validate_record(item, project_root=project_root)
        for item in store.get("records", [])
        if isinstance(item, dict)
    ]
    records = [item for item in records if item.get("lease_digest") != record["lease_digest"]]
    records.append(record)
    store["records"] = records[-MAX_PREFLIGHT_BINDING_LEASES:]
    try:
        _write_store(path, store)
    except OSError as exc:
        raise PreflightBindingLeaseError(
            f"Could not persist preflight binding lease: {exc}",
            reason_code="preflight_binding_lease_store_write_failed",
        ) from exc
    return record


def verify_preflight_binding_lease(
    *,
    storage_root: str | Path,
    project_root: str | Path,
    binding: Mapping[str, Any],
) -> dict:
    path = preflight_binding_store_path(storage_root)
    if not path.exists():
        raise PreflightBindingLeaseError(
            "Preflight binding was not issued by the dispatcher in this sidecar.",
            reason_code="preflight_binding_lease_missing",
        )
    store = _load_store(path)
    binding_hash = binding.get("preflight_contract_hash")
    matching_records = [
        _validate_record(record, project_root=project_root)
        for record in store.get("records", [])
        if isinstance(record, dict) and record.get("binding_hash") == binding_hash
    ]
    if not matching_records:
        raise PreflightBindingLeaseError(
            "Preflight binding has no matching dispatcher-issued lease.",
            reason_code="preflight_binding_lease_missing",
        )
    record = matching_records[-1]
    expected_fields = (
        "operation_class",
        "file_key",
        "write_type",
        "contract_type",
        "expected_workspace_revision",
        "expected_file_revision",
        "preflight_contract_identity",
    )
    for field in expected_fields:
        if record.get(field) != binding.get(field):
            raise PreflightBindingLeaseError(
                "Preflight binding does not match its dispatcher-issued lease.",
                reason_code="preflight_binding_lease_mismatch",
                field_path=f"$.{field}",
            )
    return record


# --- S3–S5 transitional lease authority (frozen §7.4; deleted by T050-07C) -----

TRANSITIONAL_AUTHORITY_REASON = "invalid_transaction_authority"


class TransactionAuthorityError(ValueError):
    """Raised when final in-memory transaction authority is invalid."""

    def __init__(self, message: str, *, field_path: str = "$") -> None:
        super().__init__(message)
        self.reason_code = TRANSITIONAL_AUTHORITY_REASON
        self.field_path = field_path
        self.details = {
            "reason_code": TRANSITIONAL_AUTHORITY_REASON,
            "field_path": field_path,
        }


class TransactionAuthority:
    """Opaque, single-use authority for one dispatcher invocation."""

    __slots__ = ()

    def __new__(cls):
        raise TypeError("TransactionAuthority can only be issued in-process")

    def __reduce__(self):
        raise TypeError("TransactionAuthority is not serializable")

    def __copy__(self):
        raise TypeError("TransactionAuthority cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("TransactionAuthority cannot be copied")


_FINAL_AUTHORITY_REGISTRY: dict[
    int, tuple[TransactionAuthority, str, str, str]
] = {}
_FINAL_AUTHORITY_REGISTRY_LOCK = threading.Lock()


def issue_transaction_authority(
    *, operation: str, workspace_root: str | Path
) -> TransactionAuthority:
    """Issue final authority without consulting or persisting a lease."""
    if operation not in OPERATION_DOMAIN:
        raise TransactionAuthorityError(
            "Transaction authority operation is not a typed mutation operation.",
            field_path="$.operation",
        )
    authority = object.__new__(TransactionAuthority)
    with _FINAL_AUTHORITY_REGISTRY_LOCK:
        _FINAL_AUTHORITY_REGISTRY[id(authority)] = (
            authority,
            secrets.token_hex(32),
            operation,
            str(Path(workspace_root).resolve()),
        )
    return authority


def consume_transaction_authority(
    authority: Any,
    *,
    operation: str,
    workspace_root: str | Path,
    lock_handle: Any,
) -> None:
    """Single-consume an issued authority inside the live identity lock."""
    if not isinstance(authority, TransactionAuthority):
        raise TransactionAuthorityError(
            "Transaction authority is not an issued in-process capability."
        )
    # Every consume attempt is destructive. A capability presented with the
    # wrong invocation or lock must not remain replayable.
    with _FINAL_AUTHORITY_REGISTRY_LOCK:
        record = _FINAL_AUTHORITY_REGISTRY.pop(id(authority), None)
    if record is None or record[0] is not authority:
        raise TransactionAuthorityError(
            "Transaction authority is unknown, forged, or already consumed."
        )
    _, nonce, issued_operation, issued_root = record
    if not nonce or issued_operation != operation:
        raise TransactionAuthorityError(
            "Transaction authority operation does not match this invocation.",
            field_path="$.operation",
        )
    if issued_root != str(Path(workspace_root).resolve()):
        raise TransactionAuthorityError(
            "Transaction authority workspace does not match this invocation.",
            field_path="$.workspace_root",
        )
    if not isinstance(lock_handle, WorkspaceLockHandle):
        raise TransactionAuthorityError(
            "Transaction authority requires the live WorkspaceLockHandle.",
            field_path="$.lock_handle",
        )
    observation = observe_workspace_lock(lock_handle.lock_path)
    if (
        not observation.exists
        or observation.anomaly is not None
        or observation.identity != lock_handle.lock_identity
        or observation.payload.get("instance_token") != lock_handle.instance_token
    ):
        raise TransactionAuthorityError(
            "Transaction authority is outside the live identity-lock scope.",
            field_path="$.lock_handle",
        )


def discard_transaction_authority(authority: Any) -> None:
    """Forget an unconsumed authority when its transaction invocation ends."""
    with _FINAL_AUTHORITY_REGISTRY_LOCK:
        record = _FINAL_AUTHORITY_REGISTRY.get(id(authority))
        if record is not None and record[0] is authority:
            del _FINAL_AUTHORITY_REGISTRY[id(authority)]
