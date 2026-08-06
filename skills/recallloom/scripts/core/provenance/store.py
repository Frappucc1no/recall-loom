"""Local helper receipt store for finalized RecallLoom write receipts."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from core.provenance.receipts import (
    accepted_receipt_contract_identities,
    RECEIPT_ALLOWED_FIELDS,
    RECEIPT_REDACTION_POLICY_VERSION,
    RECEIPT_SCHEMA_VERSION,
    assert_public_safe_json,
    legacy_v045_receipt_contract_identity,
    ReceiptContractError,
    ReceiptPrivacyError,
    receipt_contract_identity,
    receipt_payload_digest,
    validate_receipt_payload,
)
from core.provenance.state import accepted_preflight_contract_identities_for_receipts
from core.workspace.atomic_io import atomic_write_bytes


RECEIPT_STORE_SCHEMA_VERSION = "0.1"
RECEIPT_STORE_RELATIVE_PATH = "derived/helper-receipts.json"
RECEIPT_STORE_TYPE = "recallloom.helper_receipt_store"
RECEIPT_STORE_FIELDS = (
    "schema_version",
    "store_type",
    "store_revision",
    "contract_identity",
    "receipts",
    "index",
)
PERSISTED_RECEIPT_FIELDS = RECEIPT_ALLOWED_FIELDS
PERSISTED_INDEX_ENTRY_FIELDS = (
    "receipt_digest",
    "store_revision",
    "receipt_offset",
    "target_file_key",
    "result_workspace_revision",
    "result_file_revision",
    "created_at",
)
RECEIPT_SUMMARY_FIELDS = (
    "revision",
    "finalization_status",
    "target_file_key",
    "target_digest",
    "state_digest",
    "result_workspace_revision",
    "result_file_revision",
    "created_at",
)
STORE_BINDING_FIELDS = (
    "store_file",
    "store_revision",
    "index_key",
    "receipt_digest",
    "store_contract_identity",
)
CONTRACT_IDENTITY_FIELDS = ("contract_name", "contract_version", "contract_hash")
RECEIPT_STORE_SNAPSHOT_STATUSES = ("absent", "present")
RECEIPT_STORE_NOT_WRITTEN_VERIFIED = "receipt_store_not_written_verified"
RECEIPT_STORE_NOT_WRITTEN_SIDE_EFFECT = (
    "target_and_state_written_receipt_store_verified_unchanged"
)
_RECEIPT_STORE_SNAPSHOT_TOKEN = object()
_RECEIPT_COMMIT_PLAN_TOKEN = object()
_RECEIPT_FINALIZATION_TOKEN = object()


def _is_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


class _DuplicateReceiptStoreKey(ValueError):
    pass


@dataclass(frozen=True)
class ReceiptStoreSnapshot:
    status: str
    revision: int | None
    digest: str | None
    contract_identity: dict | None
    receipt_digests: tuple[str, ...]
    index_keys: tuple[str, ...]
    _payload: dict = field(repr=False, compare=False)
    _source_identity: tuple[int, ...] | None = field(repr=False, compare=False)
    _raw_digest: str | None = field(repr=False, compare=False)
    _storage_root: Path | None = field(repr=False, compare=False)
    _project_root: Path | None = field(repr=False, compare=False)
    _construction_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.status not in RECEIPT_STORE_SNAPSHOT_STATUSES:
            raise ValueError("Receipt-store snapshot status is invalid.")
        if self.status == "absent":
            if (
                self.revision is not None
                or self.digest is not None
                or self.contract_identity is not None
                or self.receipt_digests
                or self.index_keys
                or self._source_identity is not None
                or self._raw_digest is not None
                or not isinstance(self._storage_root, Path)
                or not isinstance(self._project_root, Path)
            ):
                raise ValueError("Absent receipt-store snapshot fields must use null/empty sentinels.")
            return
        if (
            not _is_json_int(self.revision)
            or self.revision < 0
            or not _is_sha256_digest(self.digest)
            or not isinstance(self.contract_identity, dict)
            or not isinstance(self.receipt_digests, tuple)
            or not isinstance(self.index_keys, tuple)
            or not all(_is_sha256_digest(digest) for digest in self.receipt_digests)
            or self.index_keys != tuple(sorted(self.receipt_digests))
            or self.revision != len(self.receipt_digests)
            or not isinstance(self._source_identity, tuple)
            or len(self._source_identity) != 6
            or not all(_is_json_int(value) for value in self._source_identity)
            or not _is_sha256_digest(self._raw_digest)
            or not isinstance(self._storage_root, Path)
            or not isinstance(self._project_root, Path)
        ):
            raise ValueError("Present receipt-store snapshot fields are incomplete.")


@dataclass(frozen=True)
class ReceiptCommitPlan:
    """Module-authenticated, single-snapshot receipt-store commit plan."""

    storage_root: Path
    project_root: Path
    path: Path
    snapshot: ReceiptStoreSnapshot
    finalized_receipt: dict = field(repr=False, compare=False)
    store_binding: dict = field(repr=False, compare=False)
    next_store: dict = field(repr=False, compare=False)
    next_store_bytes: bytes = field(repr=False, compare=False)
    receipt_digest: str
    store_revision: int
    _construction_token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class ReceiptFinalization:
    plan: ReceiptCommitPlan = field(repr=False, compare=False)
    _construction_token: object = field(repr=False, compare=False)


class ReceiptStoreError(RuntimeError):
    """Raised when receipt finalization cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        side_effect: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.side_effect = side_effect
        self.details = {
            "reason_code": reason_code,
            "side_effect": side_effect,
            **(details or {}),
        }


def receipt_store_path(storage_root: str | Path) -> Path:
    return Path(storage_root) / RECEIPT_STORE_RELATIVE_PATH


def _receipt_store_contract_identity_for_receipt_contract(receipt_contract: dict) -> dict:
    payload = {
        "schema_version": RECEIPT_STORE_SCHEMA_VERSION,
        "store_type": RECEIPT_STORE_TYPE,
        "store_file": RECEIPT_STORE_RELATIVE_PATH,
        "receipt_contract": receipt_contract,
        "redaction_policy_version": RECEIPT_REDACTION_POLICY_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "contract_name": "recallloom.helper_receipt_store",
        "contract_version": RECEIPT_STORE_SCHEMA_VERSION,
        "contract_hash": f"sha256:{digest}",
    }


def receipt_store_contract_identity() -> dict:
    return _receipt_store_contract_identity_for_receipt_contract(receipt_contract_identity())


def legacy_v045_receipt_store_contract_identity() -> dict:
    """Return the v0.4.5 store identity accepted for read/append compatibility."""

    return _receipt_store_contract_identity_for_receipt_contract(legacy_v045_receipt_contract_identity())


def accepted_receipt_store_contract_identities() -> tuple[dict, ...]:
    return (
        receipt_store_contract_identity(),
        legacy_v045_receipt_store_contract_identity(),
    )


def _empty_store() -> dict:
    return {
        "schema_version": RECEIPT_STORE_SCHEMA_VERSION,
        "store_type": RECEIPT_STORE_TYPE,
        "store_revision": 0,
        "contract_identity": receipt_store_contract_identity(),
        "receipts": [],
        "index": {},
    }


def _receipt_store_contract_error(message: str) -> ReceiptStoreError:
    return ReceiptStoreError(
        message,
        reason_code="receipt_store_contract_invalid",
        side_effect="target_and_state_written_receipt_not_stored",
    )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateReceiptStoreKey(key)
        payload[key] = value
    return payload


def _decode_receipt_store_payload(raw_bytes: bytes) -> Any:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptStoreError(
            "Receipt store is not valid UTF-8.",
            reason_code="receipt_store_unreadable",
            side_effect="target_and_state_written_receipt_not_stored",
        ) from exc
    try:
        return json.loads(text, object_pairs_hook=_strict_json_object)
    except _DuplicateReceiptStoreKey as exc:
        raise _receipt_store_contract_error(
            "Receipt store contains duplicate JSON object keys."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReceiptStoreError(
            "Receipt store is not readable JSON.",
            reason_code="receipt_store_unreadable",
            side_effect="target_and_state_written_receipt_not_stored",
        ) from exc


def _validate_receipt_store_payload(payload: Any, *, project_root: str | Path) -> dict:
    if not isinstance(payload, dict):
        raise _receipt_store_contract_error("Receipt store must be a JSON object.")
    if set(payload) != set(RECEIPT_STORE_FIELDS):
        raise _receipt_store_contract_error(
            "Receipt store contains fields outside the minimized store contract."
        )
    if payload.get("schema_version") != RECEIPT_STORE_SCHEMA_VERSION:
        raise ReceiptStoreError(
            "Receipt store schema version does not match the active contract.",
            reason_code="receipt_store_schema_version_mismatch",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    if payload.get("store_type") != RECEIPT_STORE_TYPE:
        raise ReceiptStoreError(
            "Receipt store type does not match the active contract.",
            reason_code="receipt_store_type_mismatch",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    if not _is_json_int(payload.get("store_revision")) or payload["store_revision"] < 0:
        raise ReceiptStoreError(
            "Receipt store revision must be a non-negative integer.",
            reason_code="receipt_store_revision_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    if not isinstance(payload.get("receipts"), list) or not isinstance(payload.get("index"), dict):
        raise _receipt_store_contract_error("Receipt store must contain receipts and index collections.")
    if payload.get("contract_identity") not in accepted_receipt_store_contract_identities():
        raise _receipt_store_contract_error(
            "Receipt store contract identity does not match the active store contract."
        )
    _validate_loaded_store_payload(payload, project_root=project_root)
    return payload


def _load_store(path: Path, *, project_root: str | Path) -> dict:
    if not path.exists():
        return _empty_store()
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ReceiptStoreError(
            "Receipt store could not be read.",
            reason_code="receipt_store_unreadable",
            side_effect="target_and_state_written_receipt_not_stored",
        ) from exc
    payload = _decode_receipt_store_payload(raw_bytes)
    return _validate_receipt_store_payload(payload, project_root=project_root)


def _receipt_summary_entry(receipt: dict) -> dict:
    return {key: receipt[key] for key in RECEIPT_SUMMARY_FIELDS if key in receipt}


def receipt_store_summary(
    *,
    storage_root: str | Path,
    project_root: str | Path,
    require_exists: bool = False,
    snapshot: ReceiptStoreSnapshot | None = None,
) -> dict:
    """Return a verified, public-safe summary of the optional helper receipt store."""

    path = receipt_store_path(storage_root)
    if require_exists and not path.exists():
        raise ReceiptStoreError(
            "Receipt store is required for this provenance validation lane.",
            reason_code="receipt_store_missing",
            side_effect="provenance_validation_failed",
        )
    if snapshot is None:
        store = _load_store(path, project_root=project_root)
    else:
        validate_receipt_store_snapshot(snapshot)
        if require_exists and snapshot.status == "absent":
            raise ReceiptStoreError(
                "Receipt store is required for this provenance validation lane.",
                reason_code="receipt_store_missing",
                side_effect="provenance_validation_failed",
            )
        store = snapshot._payload
    latest_receipts_by_file_key: dict[str, dict] = {}
    for receipt in store["receipts"]:
        latest_receipts_by_file_key[receipt["target_file_key"]] = _receipt_summary_entry(receipt)
    return {
        "store_file": RECEIPT_STORE_RELATIVE_PATH,
        "store_revision": store["store_revision"],
        "receipt_count": len(store["receipts"]),
        "target_file_keys": sorted(latest_receipts_by_file_key),
        "latest_receipts_by_file_key": latest_receipts_by_file_key,
    }


def capture_receipt_store_snapshot(
    *,
    storage_root: str | Path,
    project_root: str | Path,
) -> ReceiptStoreSnapshot:
    """Capture a bounded, contract-validated snapshot of the optional receipt store."""

    path = receipt_store_path(storage_root)
    try:
        before_stat = path.lstat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return ReceiptStoreSnapshot(
                status="absent",
                revision=None,
                digest=None,
                contract_identity=None,
                receipt_digests=(),
                index_keys=(),
                _payload=_empty_store(),
                _source_identity=None,
                _raw_digest=None,
                _storage_root=Path(storage_root),
                _project_root=Path(project_root),
                _construction_token=_RECEIPT_STORE_SNAPSHOT_TOKEN,
            )
        else:
            raise ReceiptStoreError(
                "Receipt store changed while its absent snapshot was captured.",
                reason_code="receipt_store_concurrent_change_detected",
                side_effect="target_and_state_written_receipt_not_stored",
            )
    if not stat.S_ISREG(before_stat.st_mode):
        raise _receipt_store_contract_error(
            "Receipt store snapshot source must be a regular file."
        )
    try:
        before_bytes = path.read_bytes()
        store = _validate_receipt_store_payload(
            _decode_receipt_store_payload(before_bytes),
            project_root=project_root,
        )
        after_bytes = path.read_bytes()
        after_stat = path.lstat()
    except OSError as exc:
        raise ReceiptStoreError(
            "Receipt store snapshot could not be read.",
            reason_code="receipt_store_unreadable",
            side_effect="target_and_state_written_receipt_not_stored",
        ) from exc
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
    if before_identity != after_identity or before_bytes != after_bytes:
        raise ReceiptStoreError(
            "Receipt store changed while its snapshot was captured.",
            reason_code="receipt_store_concurrent_change_detected",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    canonical_bytes = json.dumps(
        store,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ReceiptStoreSnapshot(
        status="present",
        revision=store["store_revision"],
        digest="sha256:" + hashlib.sha256(canonical_bytes).hexdigest(),
        contract_identity=dict(store["contract_identity"]),
        receipt_digests=tuple(
            receipt["digest"]
            for receipt in store["receipts"]
            if isinstance(receipt, dict) and isinstance(receipt.get("digest"), str)
        ),
        index_keys=tuple(sorted(store["index"])),
        _payload=copy.deepcopy(store),
        _source_identity=after_identity,
        _raw_digest="sha256:" + hashlib.sha256(after_bytes).hexdigest(),
        _storage_root=Path(storage_root),
        _project_root=Path(project_root),
        _construction_token=_RECEIPT_STORE_SNAPSHOT_TOKEN,
    )


def validate_receipt_store_snapshot(snapshot: ReceiptStoreSnapshot) -> None:
    """Reject hand-built or internally inconsistent receipt-store snapshots."""

    if not isinstance(snapshot, ReceiptStoreSnapshot):
        raise ReceiptStoreError(
            "Expected receipt-store snapshot is not a ReceiptStoreSnapshot.",
            reason_code="receipt_store_snapshot_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    if snapshot._construction_token is not _RECEIPT_STORE_SNAPSHOT_TOKEN:
        raise ReceiptStoreError(
            "Expected receipt-store snapshot was not captured by this module.",
            reason_code="receipt_store_snapshot_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    try:
        snapshot.__post_init__()
    except ValueError as exc:
        raise ReceiptStoreError(
            "Expected receipt-store snapshot fields are invalid.",
            reason_code="receipt_store_snapshot_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        ) from exc
    payload = snapshot._payload
    if not isinstance(payload, dict):
        raise ReceiptStoreError(
            "Expected receipt-store snapshot payload is invalid.",
            reason_code="receipt_store_snapshot_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    if snapshot.status == "absent":
        if payload != _empty_store():
            raise ReceiptStoreError(
                "Absent receipt-store snapshot payload is invalid.",
                reason_code="receipt_store_snapshot_invalid",
                side_effect="target_and_state_written_receipt_not_stored",
            )
        return
    receipts = payload.get("receipts")
    index = payload.get("index")
    if (
        not isinstance(receipts, list)
        or not all(isinstance(receipt, dict) for receipt in receipts)
        or not isinstance(index, dict)
    ):
        raise ReceiptStoreError(
            "Present receipt-store snapshot payload collections are invalid.",
            reason_code="receipt_store_snapshot_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    if (
        payload.get("store_revision") != snapshot.revision
        or payload.get("contract_identity") != snapshot.contract_identity
        or tuple(receipt.get("digest") for receipt in receipts) != snapshot.receipt_digests
        or tuple(sorted(index)) != snapshot.index_keys
    ):
        raise ReceiptStoreError(
            "Present receipt-store snapshot payload does not match its public fields.",
            reason_code="receipt_store_snapshot_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    # The construction token proves that the expensive payload validation and
    # canonicalization happened in capture_receipt_store_snapshot.  Callers
    # may validate/reuse the snapshot without walking the historical chain.


def receipt_store_snapshot_fields(snapshot: ReceiptStoreSnapshot) -> dict[str, object]:
    validate_receipt_store_snapshot(snapshot)
    return {
        "receipt_store_snapshot_status": snapshot.status,
        "receipt_store_revision": snapshot.revision,
        "receipt_store_digest": snapshot.digest,
        "receipt_store_contract_identity": (
            dict(snapshot.contract_identity)
            if isinstance(snapshot.contract_identity, dict)
            else None
        ),
    }


def _receipt_store_snapshots_equal(
    left: ReceiptStoreSnapshot,
    right: ReceiptStoreSnapshot,
) -> bool:
    return (
        left.status == right.status
        and left.revision == right.revision
        and left.digest == right.digest
        and left.contract_identity == right.contract_identity
        and left.receipt_digests == right.receipt_digests
        and left.index_keys == right.index_keys
        and left._source_identity == right._source_identity
        and left._raw_digest == right._raw_digest
    )


def receipt_store_snapshot_matches_current(
    *,
    storage_root: str | Path,
    project_root: str | Path,
    snapshot: ReceiptStoreSnapshot,
    expected_receipt_digest: str | None = None,
) -> bool:
    """Positively prove that the receipt store is still the frozen snapshot."""

    try:
        validate_receipt_store_snapshot(snapshot)
    except ReceiptStoreError:
        return False
    if not _raw_snapshot_matches(receipt_store_path(storage_root), snapshot):
        return False
    if expected_receipt_digest is None:
        return True
    if not _is_sha256_digest(expected_receipt_digest):
        return False
    expected_binding = {
        "store_file": RECEIPT_STORE_RELATIVE_PATH,
        "store_revision": (snapshot.revision or 0) + 1,
        "index_key": expected_receipt_digest,
        "receipt_digest": expected_receipt_digest,
        "store_contract_identity": receipt_store_contract_identity(),
    }
    if expected_receipt_digest in snapshot.receipt_digests or expected_receipt_digest in snapshot.index_keys:
        return False
    return not any(
        isinstance(receipt, dict) and receipt.get("store_binding") == expected_binding
        for receipt in snapshot._payload.get("receipts", [])
    )


def expected_finalized_receipt_digest(
    *,
    receipt: dict,
    snapshot: ReceiptStoreSnapshot,
) -> str:
    """Project the digest that finalization would bind to the frozen store revision."""

    validate_receipt_store_snapshot(snapshot)
    store_revision = (snapshot.revision or 0) + 1
    finalized_receipt = {
        **receipt,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "revision": store_revision,
        "finalization_status": "finalized",
        "redaction_policy_version": RECEIPT_REDACTION_POLICY_VERSION,
        "contract_identity": receipt_contract_identity(),
    }
    created_at = _coarse_timestamp(finalized_receipt.get("created_at"))
    if created_at is not None:
        finalized_receipt["created_at"] = created_at
    return receipt_payload_digest(finalized_receipt)


def _has_exact_keys(value: Any, expected: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and set(value) == set(expected)


def _has_only_allowed_keys(value: Any, allowed: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and set(value).issubset(set(allowed))


def _is_json_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_contract_identity(value: Any) -> bool:
    if not _has_exact_keys(value, CONTRACT_IDENTITY_FIELDS):
        return False
    return all(isinstance(value.get(key), str) for key in CONTRACT_IDENTITY_FIELDS)


def _validate_store_binding(value: Any, *, expected_store_contract_identity: dict | None = None) -> bool:
    expected_identity = expected_store_contract_identity or receipt_store_contract_identity()
    return (
        _has_exact_keys(value, STORE_BINDING_FIELDS)
        and value.get("store_file") == RECEIPT_STORE_RELATIVE_PATH
        and _is_json_int(value.get("store_revision"))
        and isinstance(value.get("index_key"), str)
        and isinstance(value.get("receipt_digest"), str)
        and value.get("store_contract_identity") == expected_identity
    )


def _store_contract_identity_for_receipt(receipt: dict) -> dict | None:
    receipt_contract = receipt.get("contract_identity")
    if receipt_contract == receipt_contract_identity():
        return receipt_store_contract_identity()
    if receipt_contract == legacy_v045_receipt_contract_identity():
        return legacy_v045_receipt_store_contract_identity()
    return None


def _validate_persisted_receipt_value(receipt: dict) -> bool:
    try:
        validate_receipt_payload(receipt)
    except (ReceiptContractError, ReceiptPrivacyError):
        return False
    expected_store_contract_identity = _store_contract_identity_for_receipt(receipt)
    if expected_store_contract_identity is None:
        return False
    return (
        isinstance(receipt.get("digest"), str)
        and receipt["digest"].startswith("sha256:")
        and _is_json_int(receipt.get("revision"))
        and receipt.get("finalization_status") == "finalized"
        and receipt.get("redaction_policy_version") == RECEIPT_REDACTION_POLICY_VERSION
        and isinstance(receipt.get("target_file_key"), str)
        and isinstance(receipt.get("target_digest"), str)
        and receipt["target_digest"].startswith("sha256:")
        and isinstance(receipt.get("state_digest"), str)
        and receipt["state_digest"].startswith("sha256:")
        and receipt.get("preflight_contract_identity")
        in accepted_preflight_contract_identities_for_receipts()
        and receipt.get("contract_identity") in accepted_receipt_contract_identities()
        and _validate_store_binding(
            receipt.get("store_binding"),
            expected_store_contract_identity=expected_store_contract_identity,
        )
        and receipt["store_binding"].get("receipt_digest") == receipt["digest"]
        and receipt["store_binding"].get("index_key") == receipt["digest"]
        and receipt["store_binding"].get("store_revision") == receipt["revision"]
        and _is_json_int(receipt.get("expected_workspace_revision"))
        and _is_json_int(receipt.get("result_workspace_revision"))
        and _is_json_int(receipt.get("expected_file_revision"))
        and _is_json_int(receipt.get("result_file_revision"))
        and isinstance(receipt.get("created_at"), str)
        and len(receipt["created_at"]) == 10
    )


def _validate_loaded_store_payload(payload: dict, *, project_root: str | Path) -> None:
    try:
        assert_public_safe_json(payload, project_root=str(project_root))
    except ReceiptPrivacyError as exc:
        raise ReceiptStoreError(
            "Receipt store contains data outside the public-safe redaction contract.",
            reason_code=exc.reason_code,
            side_effect="target_and_state_written_receipt_not_stored",
            details=exc.details,
        ) from exc

    receipts_by_digest: dict[str, tuple[dict, int]] = {}
    for offset, receipt in enumerate(payload["receipts"]):
        if (
            not _has_only_allowed_keys(receipt, PERSISTED_RECEIPT_FIELDS)
            or not _validate_contract_identity(receipt.get("preflight_contract_identity"))
            or not _validate_contract_identity(receipt.get("contract_identity"))
            or not _validate_store_binding(
                receipt.get("store_binding"),
                expected_store_contract_identity=_store_contract_identity_for_receipt(receipt),
            )
            or not _validate_persisted_receipt_value(receipt)
        ):
            raise _receipt_store_contract_error(
                "Receipt store contains an entry outside the minimized persistence contract.",
            )
        receipt_digest = receipt["digest"]
        if receipt_digest in receipts_by_digest:
            raise _receipt_store_contract_error("Receipt store contains duplicate receipt digests.")
        if receipt["revision"] != offset + 1:
            raise _receipt_store_contract_error(
                "Receipt store revisions must be contiguous and match receipt order."
            )
        receipts_by_digest[receipt_digest] = (receipt, offset)

    if payload["store_revision"] != len(payload["receipts"]):
        raise _receipt_store_contract_error(
            "Receipt store revision must match the latest persisted receipt revision."
        )
    if set(payload["index"]) != set(receipts_by_digest):
        raise _receipt_store_contract_error(
            "Receipt store index does not match its minimized receipt entries."
        )
    for digest, index_entry in payload["index"].items():
        receipt, offset = receipts_by_digest[digest]
        if (
            not isinstance(digest, str)
            or not _has_exact_keys(index_entry, PERSISTED_INDEX_ENTRY_FIELDS)
            or index_entry.get("receipt_digest") != digest
            or not _is_json_int(index_entry.get("store_revision"))
            or not _is_json_int(index_entry.get("receipt_offset"))
            or index_entry.get("store_revision") != receipt["revision"]
            or index_entry.get("receipt_offset") != offset
            or index_entry.get("target_file_key") != receipt["target_file_key"]
            or index_entry.get("result_workspace_revision") != receipt["result_workspace_revision"]
            or index_entry.get("result_file_revision") != receipt["result_file_revision"]
            or index_entry.get("created_at") != receipt["created_at"]
        ):
            raise _receipt_store_contract_error(
                "Receipt store index contains an entry outside the minimized persistence contract.",
            )


def _write_json_atomic(
    path: Path,
    payload: dict,
    *,
    create_only: bool = False,
    before_replace=None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if not create_only:
        # The staged temp + fsync + os.replace mechanism is owned by
        # core.workspace.atomic_io; the hook still runs between fsync and replace.
        atomic_write_bytes(path, data, before_replace=before_replace)
        return
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.link(temp_path, path)
        temp_path.unlink()
        temp_path = None
    except BaseException:
        if temp_path is not None:
            with suppress(FileNotFoundError):
                temp_path.unlink()
        raise


def _write_receipt_store_bytes(
    path: Path,
    data: bytes,
    *,
    create_only: bool,
    before_replace,
) -> None:
    """Write already-serialized store bytes without serializing them again."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if not create_only:
        atomic_write_bytes(path, data, before_replace=before_replace)
        return
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.tmp-", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.link(temp_path, path)
        temp_path.unlink()
        temp_path = None
    except BaseException:
        if temp_path is not None:
            with suppress(FileNotFoundError):
                temp_path.unlink()
        raise


def _coarse_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    return value


def _persisted_receipt_entry(finalized_receipt: dict) -> dict:
    entry = {
        key: finalized_receipt[key]
        for key in PERSISTED_RECEIPT_FIELDS
        if key in finalized_receipt
    }
    created_at = _coarse_timestamp(finalized_receipt.get("created_at"))
    if created_at is not None:
        entry["created_at"] = created_at
    return entry


def _index_entry(*, receipt: dict, receipt_offset: int, binding: dict) -> dict:
    created_at = _coarse_timestamp(receipt.get("created_at"))
    return {
        "receipt_digest": receipt["digest"],
        "store_revision": binding["store_revision"],
        "receipt_offset": receipt_offset,
        "target_file_key": receipt.get("target_file_key"),
        "result_workspace_revision": receipt.get("result_workspace_revision"),
        "result_file_revision": receipt.get("result_file_revision"),
        "created_at": created_at,
    }


def _verified_reloaded_store(
    *,
    path: Path,
    expected_store: dict,
    project_root: str | Path,
) -> None:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ReceiptStoreError(
            f"Receipt store could not be verified after write: {exc}",
            reason_code="receipt_store_post_write_unreadable",
            side_effect="target_state_and_receipt_store_write_unknown_review_required",
        ) from exc
    try:
        reloaded = _validate_receipt_store_payload(
            _decode_receipt_store_payload(raw_bytes),
            project_root=project_root,
        )
    except ReceiptStoreError as exc:
        raise ReceiptStoreError(
            "Receipt store failed its complete contract after write.",
            reason_code=exc.reason_code,
            side_effect="target_state_and_receipt_store_written_review_required",
            details=exc.details,
        ) from exc
    expected_bytes = (
        json.dumps(expected_store, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if raw_bytes != expected_bytes or reloaded != expected_store:
        raise ReceiptStoreError(
            "Receipt store does not exactly match the complete finalized store.",
            reason_code="receipt_store_index_mismatch",
            side_effect="target_state_and_receipt_store_written_review_required",
        )


def prepare_receipt_commit(
    snapshot: ReceiptStoreSnapshot,
    receipt: dict,
) -> ReceiptCommitPlan:
    """Prepare one append using only an already validated store snapshot."""

    validate_receipt_store_snapshot(snapshot)
    storage_root = snapshot._storage_root
    project_root = snapshot._project_root
    path = receipt_store_path(storage_root)
    store = copy.deepcopy(snapshot._payload)
    store_revision = store["store_revision"] + 1
    finalized_receipt = {
        **receipt,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "revision": store_revision,
        "finalization_status": "finalized",
        "redaction_policy_version": RECEIPT_REDACTION_POLICY_VERSION,
        "contract_identity": receipt_contract_identity(),
    }
    created_at = _coarse_timestamp(finalized_receipt.get("created_at"))
    if created_at is not None:
        finalized_receipt["created_at"] = created_at
    finalized_receipt["digest"] = receipt_payload_digest(finalized_receipt)
    binding = {
        "store_file": RECEIPT_STORE_RELATIVE_PATH,
        "store_revision": store_revision,
        "index_key": finalized_receipt["digest"],
        "receipt_digest": finalized_receipt["digest"],
        "store_contract_identity": receipt_store_contract_identity(),
    }
    finalized_receipt["store_binding"] = binding

    try:
        validate_receipt_payload(finalized_receipt, project_root=str(project_root))
    except ReceiptPrivacyError as exc:
        raise ReceiptStoreError(
            "Receipt finalization failed the redaction policy.",
            reason_code=exc.reason_code,
            side_effect="target_and_state_written_receipt_not_stored",
            details=exc.details,
        ) from exc
    except ReceiptContractError as exc:
        raise ReceiptStoreError(
            "Receipt finalization failed the receipt contract.",
            reason_code=exc.reason_code,
            side_effect="target_and_state_written_receipt_not_stored",
            details=exc.details,
        ) from exc

    persisted_receipt = _persisted_receipt_entry(finalized_receipt)
    receipts = [*store["receipts"], persisted_receipt]
    index = dict(store["index"])
    receipt_digest = finalized_receipt["digest"]
    if (
        receipt_digest in index
        or any(
            stored_receipt.get("digest") == receipt_digest
            or stored_receipt.get("store_binding") == binding
            for stored_receipt in store["receipts"]
        )
    ):
        raise ReceiptStoreError(
            "Receipt store already contains this receipt digest.",
            reason_code="receipt_store_duplicate_digest",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    index_entry = _index_entry(
        receipt=finalized_receipt,
        receipt_offset=len(receipts) - 1,
        binding=binding,
    )
    index[receipt_digest] = index_entry
    next_store = {
        "schema_version": RECEIPT_STORE_SCHEMA_VERSION,
        "store_type": RECEIPT_STORE_TYPE,
        "store_revision": store_revision,
        "contract_identity": receipt_store_contract_identity(),
        "receipts": receipts,
        "index": index,
    }
    next_store_bytes = (
        json.dumps(next_store, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    return ReceiptCommitPlan(
        storage_root=Path(storage_root),
        project_root=Path(project_root),
        path=path,
        snapshot=snapshot,
        finalized_receipt=copy.deepcopy(finalized_receipt),
        store_binding=copy.deepcopy(binding),
        next_store=copy.deepcopy(next_store),
        next_store_bytes=next_store_bytes,
        receipt_digest=receipt_digest,
        store_revision=store_revision,
        _construction_token=_RECEIPT_COMMIT_PLAN_TOKEN,
    )


def _validate_receipt_commit_plan(plan: ReceiptCommitPlan) -> None:
    if not isinstance(plan, ReceiptCommitPlan) or plan._construction_token is not _RECEIPT_COMMIT_PLAN_TOKEN:
        raise ReceiptStoreError(
            "Receipt commit plan was not prepared by this module.",
            reason_code="receipt_store_commit_plan_invalid",
            side_effect="target_and_state_written_receipt_not_stored",
        )
    validate_receipt_store_snapshot(plan.snapshot)


def _raw_snapshot_matches(path: Path, snapshot: ReceiptStoreSnapshot) -> bool:
    """CAS check identity and bytes without parsing the historical store."""

    if snapshot.status == "absent":
        return not path.exists()
    try:
        before = path.lstat()
        raw = path.read_bytes()
        after = path.lstat()
    except OSError:
        return False
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    return (
        stat.S_ISREG(before.st_mode)
        and identity(before) == identity(after) == snapshot._source_identity
        and "sha256:" + hashlib.sha256(raw).hexdigest() == snapshot._raw_digest
    )


def commit_receipt_plan(plan: ReceiptCommitPlan) -> ReceiptFinalization:
    """CAS-commit pre-serialized bytes without another full store parse."""

    _validate_receipt_commit_plan(plan)

    def verify_expected_snapshot_before_replace() -> None:
        if not _raw_snapshot_matches(plan.path, plan.snapshot):
            raise ReceiptStoreError(
                "Receipt store changed before the finalized store replace.",
                reason_code="receipt_store_snapshot_mismatch",
                side_effect="target_and_state_written_receipt_not_stored",
                details=receipt_store_snapshot_fields(plan.snapshot),
            )

    try:
        _write_receipt_store_bytes(
            plan.path, plan.next_store_bytes,
            create_only=plan.snapshot.status == "absent",
            before_replace=verify_expected_snapshot_before_replace,
        )
    except OSError as exc:
        if (
            _raw_snapshot_matches(plan.path, plan.snapshot)
        ):
            raise ReceiptStoreError(
                "Receipt store write failed, and the unchanged pre-write snapshot proves "
                "that the receipt was not stored.",
                reason_code=RECEIPT_STORE_NOT_WRITTEN_VERIFIED,
                side_effect=RECEIPT_STORE_NOT_WRITTEN_SIDE_EFFECT,
                details={
                    "store_failure_reason_code": "receipt_store_write_failed",
                    "expected_receipt_digest": plan.receipt_digest,
                    "expected_store_binding": plan.store_binding,
                    **receipt_store_snapshot_fields(plan.snapshot),
                },
            ) from exc
        raise ReceiptStoreError(
            f"Receipt store could not be written: {exc}",
            reason_code="receipt_store_write_failed",
            side_effect="target_state_and_receipt_store_write_unknown_review_required",
        ) from exc
    return ReceiptFinalization(plan, _RECEIPT_FINALIZATION_TOKEN)


def verify_exact_receipt_readback(
    plan: ReceiptCommitPlan,
    finalization: ReceiptFinalization,
) -> ReceiptStoreSnapshot:
    """Verify exact committed bytes and construct the trusted final snapshot."""

    _validate_receipt_commit_plan(plan)
    if (
        not isinstance(finalization, ReceiptFinalization)
        or finalization._construction_token is not _RECEIPT_FINALIZATION_TOKEN
        or finalization.plan is not plan
    ):
        raise ReceiptStoreError(
            "Receipt finalization does not belong to this commit plan.",
            reason_code="receipt_store_finalization_invalid",
            side_effect="target_state_and_receipt_store_write_unknown_review_required",
        )
    try:
        before = plan.path.lstat()
        raw = plan.path.read_bytes()
        after = plan.path.lstat()
    except OSError as exc:
        raise ReceiptStoreError(
            "Receipt store could not be verified after write.",
            reason_code="receipt_store_post_write_unreadable",
            side_effect="target_state_and_receipt_store_write_unknown_review_required",
        ) from exc
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    if identity(before) != identity(after) or raw != plan.next_store_bytes:
        raise ReceiptStoreError(
            "Receipt store does not exactly match the finalized commit plan.",
            reason_code="receipt_store_index_mismatch",
            side_effect="target_state_and_receipt_store_written_review_required",
        )
    canonical = json.dumps(
        plan.next_store, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return ReceiptStoreSnapshot(
        status="present",
        revision=plan.store_revision,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        contract_identity=dict(plan.next_store["contract_identity"]),
        receipt_digests=tuple(receipt["digest"] for receipt in plan.next_store["receipts"]),
        index_keys=tuple(sorted(plan.next_store["index"])),
        _payload=copy.deepcopy(plan.next_store),
        _source_identity=identity(after),
        _raw_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        _storage_root=plan.snapshot._storage_root,
        _project_root=plan.snapshot._project_root,
        _construction_token=_RECEIPT_STORE_SNAPSHOT_TOKEN,
    )


def finalize_receipt_in_store(
    *, storage_root: str | Path, receipt: dict, project_root: str | Path,
    expected_snapshot: ReceiptStoreSnapshot | None = None,
) -> dict:
    """Compatibility facade over the frozen four-phase receipt API."""

    snapshot = expected_snapshot or capture_receipt_store_snapshot(
        storage_root=storage_root, project_root=project_root
    )
    plan = prepare_receipt_commit(snapshot, receipt)
    finalization = commit_receipt_plan(plan)
    final_snapshot = verify_exact_receipt_readback(plan, finalization)
    return {
        "receipt": plan.finalized_receipt,
        "store_binding": plan.store_binding,
        "store_path": plan.path,
        "receipt_digest": plan.receipt_digest,
        "store_revision": plan.store_revision,
        "store_snapshot": final_snapshot,
    }
