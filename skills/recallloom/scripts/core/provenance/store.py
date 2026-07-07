"""Local helper receipt store for finalized RecallLoom write receipts."""

from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
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


def _load_store(path: Path, *, project_root: str | Path) -> dict:
    if not path.exists():
        return _empty_store()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptStoreError(
            f"Receipt store is not readable JSON: {exc}",
            reason_code="receipt_store_unreadable",
            side_effect="target_and_state_written_receipt_not_stored",
        ) from exc
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


def _receipt_summary_entry(receipt: dict) -> dict:
    return {key: receipt[key] for key in RECEIPT_SUMMARY_FIELDS if key in receipt}


def receipt_store_summary(
    *,
    storage_root: str | Path,
    project_root: str | Path,
    require_exists: bool = False,
) -> dict:
    """Return a verified, public-safe summary of the optional helper receipt store."""

    path = receipt_store_path(storage_root)
    if require_exists and not path.exists():
        raise ReceiptStoreError(
            "Receipt store is required for this provenance validation lane.",
            reason_code="receipt_store_missing",
            side_effect="provenance_validation_failed",
        )
    store = _load_store(path, project_root=project_root)
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


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
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


def _verified_reloaded_binding(
    *,
    path: Path,
    receipt_digest: str,
    expected_binding: dict,
    expected_index_entry: dict,
) -> None:
    try:
        reloaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptStoreError(
            f"Receipt store could not be verified after write: {exc}",
            reason_code="receipt_store_post_write_unreadable",
            side_effect="target_state_and_receipt_store_write_unknown_review_required",
        ) from exc
    index = reloaded.get("index") if isinstance(reloaded, dict) else None
    receipts = reloaded.get("receipts") if isinstance(reloaded, dict) else None
    if not isinstance(index, dict) or not isinstance(receipts, list):
        raise ReceiptStoreError(
            "Receipt store verification failed because index or receipts are malformed.",
            reason_code="receipt_store_index_mismatch",
            side_effect="target_state_and_receipt_store_written_review_required",
        )
    if index.get(receipt_digest) != expected_index_entry:
        raise ReceiptStoreError(
            "Receipt store index does not bind to the finalized receipt.",
            reason_code="receipt_store_index_mismatch",
            side_effect="target_state_and_receipt_store_written_review_required",
        )
    if not receipts:
        raise ReceiptStoreError(
            "Receipt store verification found no stored receipts.",
            reason_code="receipt_store_index_mismatch",
            side_effect="target_state_and_receipt_store_written_review_required",
        )
    stored_receipt = receipts[-1]
    if (
        not isinstance(stored_receipt, dict)
        or stored_receipt.get("digest") != receipt_digest
        or stored_receipt.get("store_binding") != expected_binding
    ):
        raise ReceiptStoreError(
            "Receipt store receipt entry does not match its store binding.",
            reason_code="receipt_store_binding_mismatch",
            side_effect="target_state_and_receipt_store_written_review_required",
        )


def finalize_receipt_in_store(
    *,
    storage_root: str | Path,
    receipt: dict,
    project_root: str | Path,
) -> dict:
    """Finalize a receipt and append it to the optional local receipt store."""

    path = receipt_store_path(storage_root)
    store = _load_store(path, project_root=project_root)
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
    if receipt_digest in index:
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

    try:
        _write_json_atomic(path, next_store)
    except OSError as exc:
        raise ReceiptStoreError(
            f"Receipt store could not be written: {exc}",
            reason_code="receipt_store_write_failed",
            side_effect="target_state_and_receipt_store_write_unknown_review_required",
        ) from exc

    _verified_reloaded_binding(
        path=path,
        receipt_digest=receipt_digest,
        expected_binding=binding,
        expected_index_entry=index_entry,
    )
    return {
        "receipt": finalized_receipt,
        "store_binding": binding,
        "store_path": path,
        "receipt_digest": receipt_digest,
        "store_revision": store_revision,
    }
