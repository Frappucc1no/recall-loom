"""Bounded current evidence checks for structural repair helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.protocol.contracts import (
    DAILY_LOGS_DIRNAME,
    FILE_KEYS,
    SUPPORTED_PROTOCOL_VERSIONS,
    SUPPORTED_STORAGE_MODES,
    SUPPORTED_WORKSPACE_LANGUAGES,
)
from core.protocol.markers import parse_file_marker, parse_file_state_marker
from core.provenance.store import (
    RECEIPT_STORE_RELATIVE_PATH,
    ReceiptStoreError,
    receipt_store_summary,
)
from core.provenance.state import provenance_facts_from_state


RECEIPT_VERIFIED_FILE_KEYS = (
    "context_brief",
    "daily_log",
    "rolling_summary",
    "update_protocol",
)


def _sha256_text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _is_json_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _receipt_store_result(summary: dict[str, Any], *, verified: bool = False) -> dict[str, object]:
    return {
        "store_file": summary["store_file"],
        "store_revision": summary["store_revision"],
        "receipt_count": summary["receipt_count"],
        "target_file_keys": summary["target_file_keys"],
        "verified": verified,
    }


def _daily_log_cursor_for_evidence(
    *,
    state: dict,
    daily_log_cursor: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if daily_log_cursor is not None:
        return daily_log_cursor
    state_cursor = state.get("daily_logs")
    return state_cursor if isinstance(state_cursor, dict) else None


def current_receipt_required_file_keys(
    *,
    storage_root: Path,
    state: dict,
    daily_log_cursor: dict[str, object] | None = None,
) -> list[str]:
    required = ["rolling_summary"]
    if (storage_root / FILE_KEYS["context_brief"]).is_file():
        required.append("context_brief")
    if (storage_root / FILE_KEYS["update_protocol"]).is_file():
        required.append("update_protocol")
    daily_state = _daily_log_cursor_for_evidence(
        state=state,
        daily_log_cursor=daily_log_cursor,
    )
    if isinstance(daily_state, dict) and isinstance(daily_state.get("latest_file"), str):
        required.append("daily_log")
    return sorted(required)


def current_receipt_target_path(
    *,
    storage_root: Path,
    state: dict,
    file_key: str,
    daily_log_cursor: dict[str, object] | None = None,
) -> Path:
    if file_key == "daily_log":
        daily_state = _daily_log_cursor_for_evidence(
            state=state,
            daily_log_cursor=daily_log_cursor,
        )
        latest_file = daily_state.get("latest_file") if isinstance(daily_state, dict) else None
        return storage_root / latest_file if isinstance(latest_file, str) else storage_root
    return storage_root / FILE_KEYS[file_key]


def _incomplete_result(
    *,
    reason_code: str,
    required_file_keys: list[str] | None = None,
    verified_file_keys: list[str] | None = None,
    receipt_store_available: bool = False,
    receipt_store: dict[str, object] | None = None,
    missing_file_keys: list[str] | None = None,
    config_guard: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "required": True,
        "verified": False,
        "receipt_store_available": receipt_store_available,
        "evidence_block_reason_code": None,
        "reason_code": reason_code,
        "required_current_file_keys": required_file_keys or [],
        "verified_current_file_keys": verified_file_keys or [],
        "missing_current_file_keys": missing_file_keys or [],
        **({"receipt_store": receipt_store} if receipt_store is not None else {}),
        **({"config_guard": config_guard} if config_guard is not None else {}),
    }


def _storage_mode_for_root(storage_root: Path) -> str | None:
    if storage_root.name == ".recallloom":
        return "hidden"
    if storage_root.name == "recallloom":
        return "visible"
    return None


def _managed_marker_targets(
    *,
    storage_root: Path,
    state: dict,
    required_file_keys: list[str],
    daily_log_cursor: dict[str, object] | None = None,
) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    for file_key in ("rolling_summary", "context_brief", "update_protocol"):
        path = storage_root / FILE_KEYS[file_key]
        if path.is_file() or file_key in required_file_keys:
            targets.append((file_key, path))
    logs_dir = storage_root / DAILY_LOGS_DIRNAME
    if logs_dir.is_dir():
        for path in sorted(logs_dir.glob("*.md")):
            if path.is_file():
                targets.append(("daily_log", path))
    elif "daily_log" in required_file_keys:
        targets.append(
            (
                "daily_log",
                current_receipt_target_path(
                    storage_root=storage_root,
                    state=state,
                    file_key="daily_log",
                    daily_log_cursor=daily_log_cursor,
                ),
            )
        )
    return targets


def current_config_marker_consistency_check(
    *,
    storage_root: Path,
    state: dict,
    required_file_keys: list[str],
    daily_log_cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    config_path = storage_root / FILE_KEYS["config"]
    if not config_path.is_file():
        return {
            "verified": False,
            "reason_code": "missing_config",
            "path": str(config_path),
        }
    try:
        config = json.loads(_read_text(config_path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "verified": False,
            "reason_code": "invalid_config",
            "path": str(config_path),
        }
    if not isinstance(config, dict):
        return {
            "verified": False,
            "reason_code": "invalid_config",
            "path": str(config_path),
        }

    protocol_version = config.get("protocol_version")
    storage_mode = config.get("storage_mode")
    workspace_language = config.get("workspace_language")
    if storage_mode not in SUPPORTED_STORAGE_MODES:
        return {
            "verified": False,
            "reason_code": "unsupported_storage_mode",
            "path": str(config_path),
        }
    implied_storage_mode = _storage_mode_for_root(storage_root)
    if implied_storage_mode is not None and storage_mode != implied_storage_mode:
        return {
            "verified": False,
            "reason_code": "storage_mode_path_mismatch",
            "path": str(config_path),
        }
    if workspace_language not in SUPPORTED_WORKSPACE_LANGUAGES:
        return {
            "verified": False,
            "reason_code": "unsupported_workspace_language",
            "path": str(config_path),
        }
    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        return {
            "verified": False,
            "reason_code": "unsupported_protocol_version",
            "path": str(config_path),
        }

    for file_key, path in _managed_marker_targets(
        storage_root=storage_root,
        state=state,
        required_file_keys=required_file_keys,
        daily_log_cursor=daily_log_cursor,
    ):
        if not path.is_file():
            continue
        try:
            marker = parse_file_marker(_read_text(path))
        except (OSError, UnicodeDecodeError):
            return {
                "verified": False,
                "reason_code": "managed_file_unreadable",
                "path": str(path),
            }
        if marker is None:
            return {
                "verified": False,
                "reason_code": "managed_file_marker_missing",
                "path": str(path),
            }
        if marker.file_key != file_key:
            return {
                "verified": False,
                "reason_code": "managed_file_marker_mismatch",
                "path": str(path),
                "file_key": file_key,
                "actual_file_key": marker.file_key,
            }
        if marker.language != workspace_language:
            return {
                "verified": False,
                "reason_code": "workspace_language_mismatch",
                "path": str(path),
                "workspace_language": workspace_language,
                "marker_language": marker.language,
            }
        if marker.version != protocol_version:
            return {
                "verified": False,
                "reason_code": "protocol_marker_version_mismatch",
                "path": str(path),
                "protocol_version": protocol_version,
                "marker_version": marker.version,
            }
    return {
        "verified": True,
        "reason_code": "config_marker_consistency_verified",
        "path": str(config_path),
    }


def bounded_current_helper_evidence_check(
    *,
    project_root: str | Path,
    storage_root: str | Path,
    state: dict,
    state_text: str,
    helper_evidenced_only: bool = True,
    require_receipt_store: bool = False,
    require_config_guard: bool = False,
    daily_log_cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    """Verify whether current receipt-store evidence can preserve helper_evidenced.

    The check is intentionally bounded to current managed files and the current
    state digest. It does not claim to audit historical receipt chains.
    """

    project_root = Path(project_root)
    storage_root = Path(storage_root)
    facts = provenance_facts_from_state(state, review_intent=True)
    required_file_keys = current_receipt_required_file_keys(
        storage_root=storage_root,
        state=state,
        daily_log_cursor=daily_log_cursor,
    )
    config_guard = None
    if require_config_guard:
        config_guard = current_config_marker_consistency_check(
            storage_root=storage_root,
            state=state,
            required_file_keys=required_file_keys,
            daily_log_cursor=daily_log_cursor,
        )
        if config_guard.get("verified") is not True:
            return {
                "required": True,
                "verified": False,
                "receipt_store_available": False,
                "evidence_block_reason_code": "direct_state_or_config_edit_detected",
                "reason_code": str(
                    config_guard.get("reason_code")
                    or "config_marker_consistency_mismatch"
                ),
                "required_current_file_keys": required_file_keys,
                "verified_current_file_keys": [],
                "missing_current_file_keys": [],
                "config_guard": config_guard,
            }

    if helper_evidenced_only and not facts["helper_evidenced"]:
        return {
            "required": False,
            "verified": False,
            "receipt_store_available": False,
            "evidence_block_reason_code": None,
            "reason_code": "helper_evidence_check_not_required",
            "required_current_file_keys": required_file_keys if require_config_guard else [],
            "verified_current_file_keys": [],
            **({"config_guard": config_guard} if config_guard is not None else {}),
        }

    store_path = storage_root / RECEIPT_STORE_RELATIVE_PATH
    try:
        summary = receipt_store_summary(
            storage_root=storage_root,
            project_root=project_root,
            require_exists=require_receipt_store,
        )
    except ReceiptStoreError as exc:
        return {
            "required": True,
            "verified": False,
            "receipt_store_available": store_path.exists(),
            "evidence_block_reason_code": "receipt_evidence_mismatch",
            "reason_code": exc.details.get("reason_code", "receipt_store_invalid"),
            "required_current_file_keys": [],
            "verified_current_file_keys": [],
            "missing_current_file_keys": [],
            **({"config_guard": config_guard} if config_guard is not None else {}),
        }

    receipt_store = _receipt_store_result(summary)
    latest_receipts = summary["latest_receipts_by_file_key"]
    if not latest_receipts:
        return _incomplete_result(
            reason_code="receipt_evidence_absent",
            receipt_store_available=True,
            receipt_store=receipt_store,
            config_guard=config_guard,
        )

    missing = sorted(set(required_file_keys).difference(latest_receipts))
    unsupported = sorted(set(latest_receipts).difference(RECEIPT_VERIFIED_FILE_KEYS))
    if unsupported:
        return {
            "required": True,
            "verified": False,
            "receipt_store_available": True,
            "evidence_block_reason_code": "receipt_evidence_mismatch",
            "reason_code": "receipt_store_contains_unsupported_target",
            "required_current_file_keys": required_file_keys,
            "verified_current_file_keys": [],
            "missing_current_file_keys": missing,
            "receipt_store": receipt_store,
            **({"config_guard": config_guard} if config_guard is not None else {}),
        }
    if config_guard is None:
        config_guard = current_config_marker_consistency_check(
            storage_root=storage_root,
            state=state,
            required_file_keys=required_file_keys,
            daily_log_cursor=daily_log_cursor,
        )
    if config_guard.get("verified") is not True:
        return {
            "required": True,
            "verified": False,
            "receipt_store_available": True,
            "evidence_block_reason_code": "direct_state_or_config_edit_detected",
            "reason_code": str(
                config_guard.get("reason_code") or "config_marker_consistency_mismatch"
            ),
            "required_current_file_keys": required_file_keys,
            "verified_current_file_keys": [],
            "missing_current_file_keys": missing,
            "receipt_store": receipt_store,
            "config_guard": config_guard,
        }
    current_state_digest = _sha256_text_digest(state_text)
    current_workspace_revision = state.get("workspace_revision")
    verified_file_keys: list[str] = []

    for file_key, receipt in sorted(latest_receipts.items()):
        target_path = current_receipt_target_path(
            storage_root=storage_root,
            state=state,
            file_key=file_key,
            daily_log_cursor=daily_log_cursor,
        )
        if file_key not in required_file_keys:
            if receipt.get("revision") == summary["store_revision"]:
                state_digest_matches = receipt.get("state_digest") == current_state_digest
                workspace_revision_matches = (
                    receipt.get("result_workspace_revision") == current_workspace_revision
                )
                if not state_digest_matches or not workspace_revision_matches:
                    return {
                        "required": True,
                        "verified": False,
                        "receipt_store_available": True,
                        "evidence_block_reason_code": "direct_state_or_config_edit_detected",
                        "reason_code": "latest_receipt_state_binding_mismatch",
                        "required_current_file_keys": required_file_keys,
                        "verified_current_file_keys": verified_file_keys,
                        "missing_current_file_keys": missing,
                        "receipt_store": receipt_store,
                        "config_guard": config_guard,
                    }
            continue
        if not target_path.is_file():
            return {
                "required": True,
                "verified": False,
                "receipt_store_available": True,
                "evidence_block_reason_code": "receipt_evidence_mismatch",
                "reason_code": f"{file_key}_receipt_target_missing",
                "required_current_file_keys": required_file_keys,
                "verified_current_file_keys": verified_file_keys,
                "missing_current_file_keys": missing,
                "receipt_store": receipt_store,
                "config_guard": config_guard,
            }

        target_text = _read_text(target_path)
        checks = {
            "target_digest_matches_current_file": (
                receipt.get("target_digest") == _sha256_text_digest(target_text)
            ),
            "finalized": receipt.get("finalization_status") == "finalized",
        }
        if file_key == "daily_log":
            daily_state = _daily_log_cursor_for_evidence(
                state=state,
                daily_log_cursor=daily_log_cursor,
            )
            latest_entry_seq = (
                daily_state.get("latest_entry_seq") if isinstance(daily_state, dict) else None
            )
            checks["latest_entry_seq_matches_receipt"] = (
                receipt.get("result_file_revision") == latest_entry_seq
            )
        else:
            file_state = parse_file_state_marker(target_text)
            state_entry = state.get("files", {}).get(file_key)
            checks.update(
                {
                    "file_state_marker_present": file_state is not None,
                    "state_entry_present": isinstance(state_entry, dict),
                }
            )
            if file_state is not None and isinstance(state_entry, dict):
                checks.update(
                    {
                        "file_revision_matches_marker": (
                            receipt.get("result_file_revision") == file_state.revision
                        ),
                        "state_entry_revision_matches_marker": (
                            state_entry.get("file_revision") == file_state.revision
                        ),
                        "receipt_workspace_revision_matches_marker_base": (
                            receipt.get("result_workspace_revision")
                            == file_state.base_workspace_revision
                        ),
                        "state_entry_base_workspace_revision_matches_marker": (
                            state_entry.get("base_workspace_revision")
                            == file_state.base_workspace_revision
                        ),
                    }
                )

        if receipt.get("revision") == summary["store_revision"]:
            state_digest_matches = receipt.get("state_digest") == current_state_digest
            workspace_revision_matches = (
                receipt.get("result_workspace_revision") == current_workspace_revision
            )
            if not state_digest_matches or not workspace_revision_matches:
                return {
                    "required": True,
                    "verified": False,
                    "receipt_store_available": True,
                    "evidence_block_reason_code": "direct_state_or_config_edit_detected",
                    "reason_code": "latest_receipt_state_binding_mismatch",
                    "required_current_file_keys": required_file_keys,
                    "verified_current_file_keys": verified_file_keys,
                    "missing_current_file_keys": missing,
                    "receipt_store": receipt_store,
                    "config_guard": config_guard,
                }
            checks["latest_state_digest_matches_current_state"] = True
            checks["latest_workspace_revision_matches_state"] = True
        else:
            checks["workspace_revision_not_from_future"] = (
                _is_json_int(receipt.get("result_workspace_revision"))
                and _is_json_int(current_workspace_revision)
                and receipt["result_workspace_revision"] <= current_workspace_revision
            )

        failed_checks = [key for key, passed in checks.items() if not passed]
        if failed_checks:
            return {
                "required": True,
                "verified": False,
                "receipt_store_available": True,
                "evidence_block_reason_code": "receipt_evidence_mismatch",
                "reason_code": f"{file_key}_receipt_mismatch",
                "failed_checks": failed_checks,
                "required_current_file_keys": required_file_keys,
                "verified_current_file_keys": verified_file_keys,
                "missing_current_file_keys": missing,
                "receipt_store": receipt_store,
                "config_guard": config_guard,
            }
        verified_file_keys.append(file_key)

    if missing:
        return _incomplete_result(
            reason_code="receipt_evidence_incomplete",
            required_file_keys=required_file_keys,
            verified_file_keys=sorted(verified_file_keys),
            receipt_store_available=True,
            receipt_store=receipt_store,
            missing_file_keys=missing,
        )

    verified = set(verified_file_keys) == set(required_file_keys)
    receipt_store["verified"] = verified
    return {
        "required": True,
        "verified": verified,
        "receipt_store_available": True,
        "evidence_block_reason_code": None if verified else None,
        "reason_code": (
            "bounded_current_evidence_verified"
            if verified
            else "receipt_evidence_incomplete"
        ),
        "required_current_file_keys": required_file_keys,
        "verified_current_file_keys": sorted(verified_file_keys),
        "missing_current_file_keys": missing,
        "receipt_store": receipt_store,
        "config_guard": config_guard,
    }
