"""Bounded current evidence checks for structural repair helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from _common import (
    DailyLogCursorError,
    daily_log_cursor_is_legacy_empty,
    daily_log_cursor_state_fields,
    daily_log_cursors_equivalent,
    latest_active_daily_log_cursor,
)
from core.errors import ConfigContractError
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
from core.workspace.runtime import load_workspace_state


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


def _managed_file_requires_current_receipt(state: dict, file_key: str) -> bool:
    files_state = state.get("files")
    if not isinstance(files_state, dict):
        return True
    state_entry = files_state.get(file_key)
    if not isinstance(state_entry, dict):
        return True
    return not (
        state_entry.get("file_revision") == 1
        and state_entry.get("base_workspace_revision") == 1
    )


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
    required = []
    for file_key in ("rolling_summary", "context_brief", "update_protocol"):
        if (storage_root / FILE_KEYS[file_key]).is_file() and _managed_file_requires_current_receipt(
            state, file_key
        ):
            required.append(file_key)
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


def _is_positive_json_int(value: Any) -> bool:
    return _is_json_int(value) and value >= 1


def current_daily_log_cursor_consistency_check(
    *,
    storage_root: Path,
    state: dict,
) -> dict[str, object]:
    daily_state = state.get("daily_logs")
    if not isinstance(daily_state, dict):
        return {
            "verified": False,
            "reason_code": "invalid_daily_logs_state",
            "state_cursor": None,
        }

    state_cursor = daily_log_cursor_state_fields(daily_state)
    try:
        actual = latest_active_daily_log_cursor(storage_root)
    except DailyLogCursorError as exc:
        return {
            "verified": False,
            "reason_code": exc.reason_code,
            "state_cursor": state_cursor,
            "actual_cursor": exc.details,
            "path": str(exc.path) if exc.path is not None else None,
        }
    actual_cursor = actual.as_state_fields()

    if daily_log_cursors_equivalent(
        state_cursor,
        actual_cursor,
        actual_cursor=actual_cursor,
    ):
        return {
            "verified": True,
            "reason_code": "daily_log_cursor_verified",
            "state_cursor": state_cursor,
            "actual_cursor": actual_cursor,
            "path": str(actual.latest_path) if actual.latest_path is not None else None,
        }

    reason_code = "daily_log_cursor_mismatch"
    if actual.latest_file is None and not daily_log_cursor_is_legacy_empty(state_cursor):
        reason_code = "unexpected_latest_daily_log_state"
    elif state_cursor.get("latest_file") is None:
        reason_code = "missing_latest_daily_log_state"
    elif state_cursor.get("latest_file") != actual_cursor.get("latest_file"):
        reason_code = "latest_daily_log_state_mismatch"
    return {
        "verified": False,
        "reason_code": reason_code,
        "state_cursor": state_cursor,
        "actual_cursor": actual_cursor,
        "path": str(actual.latest_path) if actual.latest_path is not None else None,
    }


def _state_file_entry_issue(entry: Any, *, file_key: str) -> str | None:
    if not isinstance(entry, dict):
        return f"{file_key}_state_entry_missing"
    if not _is_positive_json_int(entry.get("file_revision")):
        return f"{file_key}_state_file_revision_invalid"
    if not isinstance(entry.get("updated_at"), str) or not entry["updated_at"].strip():
        return f"{file_key}_state_updated_at_invalid"
    if not isinstance(entry.get("writer_id"), str) or not entry["writer_id"].strip():
        return f"{file_key}_state_writer_id_invalid"
    if not _is_positive_json_int(entry.get("base_workspace_revision")):
        return f"{file_key}_state_base_workspace_revision_invalid"
    return None


def current_managed_file_state_consistency_check(
    *,
    storage_root: Path,
    state: dict,
    required_file_keys: list[str],
) -> dict[str, object]:
    files_state = state.get("files")
    if not isinstance(files_state, dict):
        return {
            "verified": False,
            "reason_code": "invalid_state_files",
            "checked_file_keys": [],
        }

    checked: list[str] = []
    for file_key in ("rolling_summary", "context_brief", "update_protocol"):
        path = storage_root / FILE_KEYS[file_key]
        should_check = path.is_file() or file_key in required_file_keys or file_key in files_state
        if not should_check:
            continue
        checked.append(file_key)
        if not path.is_file():
            return {
                "verified": False,
                "reason_code": f"{file_key}_managed_file_missing",
                "checked_file_keys": checked,
                "path": str(path),
            }
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError):
            return {
                "verified": False,
                "reason_code": f"{file_key}_managed_file_unreadable",
                "checked_file_keys": checked,
                "path": str(path),
            }
        marker = parse_file_marker(text)
        if marker is None or marker.file_key != file_key:
            return {
                "verified": False,
                "reason_code": f"{file_key}_managed_file_marker_mismatch",
                "checked_file_keys": checked,
                "path": str(path),
                "actual_file_key": marker.file_key if marker is not None else None,
            }
        file_state = parse_file_state_marker(text)
        if file_state is None:
            return {
                "verified": False,
                "reason_code": f"{file_key}_file_state_marker_missing",
                "checked_file_keys": checked,
                "path": str(path),
            }
        state_entry = files_state.get(file_key)
        state_issue = _state_file_entry_issue(state_entry, file_key=file_key)
        if state_issue is not None:
            return {
                "verified": False,
                "reason_code": state_issue,
                "checked_file_keys": checked,
                "path": str(storage_root / FILE_KEYS["state"]),
            }
        mismatches = []
        if state_entry.get("file_revision") != file_state.revision:
            mismatches.append("file_revision")
        if state_entry.get("updated_at") != file_state.updated_at:
            mismatches.append("updated_at")
        if state_entry.get("writer_id") != file_state.writer_id:
            mismatches.append("writer_id")
        if state_entry.get("base_workspace_revision") != file_state.base_workspace_revision:
            mismatches.append("base_workspace_revision")
        if (
            file_key == "update_protocol"
            and state.get("update_protocol_revision") != file_state.revision
        ):
            mismatches.append("update_protocol_revision")
        if mismatches:
            reason_code = (
                "update_protocol_workspace_revision_mismatch"
                if file_key == "update_protocol" and mismatches == ["update_protocol_revision"]
                else f"{file_key}_state_marker_mismatch"
            )
            return {
                "verified": False,
                "reason_code": reason_code,
                "failed_checks": mismatches,
                "checked_file_keys": checked,
                "path": str(path),
            }

    return {
        "verified": True,
        "reason_code": "managed_file_state_consistency_verified",
        "checked_file_keys": checked,
    }


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
                        "state_entry_updated_at_matches_marker": (
                            state_entry.get("updated_at") == file_state.updated_at
                        ),
                        "state_entry_writer_id_matches_marker": (
                            state_entry.get("writer_id") == file_state.writer_id
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


def _safe_next_action_for_reason(reason_code: str) -> str:
    if reason_code in {
        "state_json_missing",
        "state_json_unreadable",
        "state_json_invalid",
        "state_json_not_object",
        "state_contract_invalid",
    }:
        return "Stop mutating and run damaged-sidecar recovery or validation before repair."
    if reason_code in {"provenance_review_required", "provenance_evidence_inconsistent"}:
        return "Keep the sidecar read-only and run provenance recovery or repair review before mutating."
    if "receipt" in reason_code:
        return "Stop mutating and review or repair helper receipt evidence before the next write."
    if "daily_log" in reason_code:
        return "Run daily-log cursor preview/repair before appending or syncing sidecar state."
    if "config" in reason_code or "marker" in reason_code:
        return "Stop mutating and use the managed repair path before changing sidecar files."
    return "Keep the sidecar read-only until the integrity mismatch is reviewed and repaired."


def _strict_gate_block(
    *,
    reason_code: str,
    evidence: dict[str, object],
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "allowed_for_mutation": False,
        "blocked_reason": "strict_sidecar_integrity_failed",
        "reason_code": reason_code,
        "safe_next_action": _safe_next_action_for_reason(reason_code),
        "evidence": evidence,
        "details": details or {},
    }


def strict_sidecar_integrity_gate_public_summary(gate: dict[str, object]) -> dict[str, object]:
    """Return a public-safe summary of a strict sidecar mutation gate result."""

    details = gate.get("details")
    summary: dict[str, object] = {
        "allowed_for_mutation": gate.get("allowed_for_mutation") is True,
        "blocked_reason": gate.get("blocked_reason"),
        "reason_code": gate.get("reason_code"),
        "safe_next_action": gate.get("safe_next_action"),
    }
    if isinstance(details, dict):
        if "receipt_store_required" in details:
            summary["receipt_store_required"] = details.get("receipt_store_required")
        verified_file_keys = details.get("verified_current_file_keys")
        if isinstance(verified_file_keys, list):
            summary["verified_current_file_keys"] = sorted(
                key for key in verified_file_keys if isinstance(key, str)
            )
        receipt_store = details.get("receipt_store")
        if isinstance(receipt_store, dict):
            target_file_keys = receipt_store.get("target_file_keys")
            summary["receipt_store"] = {
                "verified": receipt_store.get("verified") is True,
                "store_revision": receipt_store.get("store_revision"),
                "receipt_count": receipt_store.get("receipt_count"),
                "target_file_keys": (
                    sorted(key for key in target_file_keys if isinstance(key, str))
                    if isinstance(target_file_keys, list)
                    else []
                ),
            }
    return summary


def strict_gate_current_receipts_verified(gate: dict[str, object]) -> bool:
    """Return whether the strict gate verified current helper receipts."""

    if gate.get("allowed_for_mutation") is not True:
        return False
    details = gate.get("details")
    if not isinstance(details, dict):
        return False
    receipt_store = details.get("receipt_store")
    if not isinstance(receipt_store, dict) or receipt_store.get("verified") is not True:
        return False
    verified_file_keys = details.get("verified_current_file_keys")
    return isinstance(verified_file_keys, list) and any(
        isinstance(key, str) for key in verified_file_keys
    )


def strict_sidecar_no_write_failure_extra(
    *,
    project_root: str | Path | None = None,
    storage_root: str | Path | None = None,
    continuity_confidence: str | None = None,
    include_recovery_actions: bool = True,
) -> dict[str, object]:
    """Return public-safe failure fields for read paths that cannot trust writes."""

    if project_root is None or storage_root is None:
        gate_summary = {
            "allowed_for_mutation": False,
            "blocked_reason": "strict_sidecar_integrity_failed",
            "reason_code": "strict_gate_unavailable",
            "safe_next_action": (
                "Keep the sidecar read-only until the integrity mismatch is reviewed and repaired."
            ),
        }
    else:
        try:
            gate_summary = strict_sidecar_integrity_gate_public_summary(
                strict_sidecar_integrity_gate(
                    project_root=project_root,
                    storage_root=storage_root,
                )
            )
        except Exception:
            gate_summary = {
                "allowed_for_mutation": False,
                "blocked_reason": "strict_sidecar_integrity_failed",
                "reason_code": "strict_gate_unavailable",
                "safe_next_action": (
                    "Keep the sidecar read-only until the integrity mismatch is reviewed and repaired."
                ),
            }
    return strict_sidecar_no_write_failure_extra_from_summary(
        gate_summary,
        continuity_confidence=continuity_confidence,
        include_recovery_actions=include_recovery_actions,
    )


def strict_sidecar_no_write_failure_extra_from_summary(
    gate_summary: dict[str, object],
    *,
    continuity_confidence: str | None = None,
    include_recovery_actions: bool = True,
) -> dict[str, object]:
    """Return public-safe no-write fields from an existing strict gate summary."""

    write_context_blocked_reason = (
        "strict_sidecar_integrity_failed"
        if gate_summary.get("allowed_for_mutation") is not True
        else "read_failure_no_write_context"
    )
    strict_gate_summary = dict(gate_summary)
    if strict_gate_summary.get("allowed_for_mutation") is True:
        strict_gate_summary["safe_next_action"] = (
            "Keep the sidecar read-only until the reported read failure is reviewed."
        )
    extra: dict[str, object] = {
        "strict_sidecar_integrity_gate": strict_gate_summary,
        "write_context_blocked_reason": write_context_blocked_reason,
        "safe_write_context": None,
        "side_effect": "none",
        "write_effect": "none",
    }
    if include_recovery_actions:
        extra["next_actions"] = [
            "review_or_repair_sidecar_before_write",
            "run_integrity_validation",
            "stage_recovery_proposal_for_manual_review",
            "record_recovery_review_after_manual_confirmation",
            "prepare_recovery_promotion_after_review",
        ]
    if continuity_confidence is not None:
        extra["continuity_confidence"] = continuity_confidence
    return extra


def _load_current_state_for_gate(storage_root: Path) -> tuple[str | None, dict | None, dict[str, object]]:
    state_path = storage_root / FILE_KEYS["state"]
    if not state_path.is_file():
        return None, None, {
            "verified": False,
            "reason_code": "state_json_missing",
            "path": str(state_path),
        }
    try:
        state_text = _read_text(state_path)
    except (OSError, UnicodeDecodeError):
        return None, None, {
            "verified": False,
            "reason_code": "state_json_unreadable",
            "path": str(state_path),
        }
    try:
        state = load_workspace_state(state_path)
    except json.JSONDecodeError:
        return state_text, None, {
            "verified": False,
            "reason_code": "state_json_invalid",
            "path": str(state_path),
        }
    except ConfigContractError as exc:
        return state_text, None, {
            "verified": False,
            "reason_code": "state_contract_invalid",
            "path": str(state_path),
            "error": str(exc),
        }
    return state_text, state, {
        "verified": True,
        "reason_code": "state_json_loaded",
        "path": str(state_path),
        "state_digest": _sha256_text_digest(state_text),
        "workspace_revision": state.get("workspace_revision"),
    }


def strict_sidecar_integrity_gate(
    *,
    project_root: str | Path,
    storage_root: str | Path,
) -> dict[str, object]:
    """Return the shared strict mutation gate for the current sidecar snapshot.

    This gate deliberately rereads filesystem state and recomputes the bounded
    current evidence surface. It is not a historical receipt-chain audit.
    """

    project_root = Path(project_root)
    storage_root = Path(storage_root)
    state_text, state, state_snapshot = _load_current_state_for_gate(storage_root)
    evidence: dict[str, object] = {
        "state_snapshot": state_snapshot,
    }
    if state_text is None or state is None:
        return _strict_gate_block(
            reason_code=str(state_snapshot.get("reason_code") or "state_json_invalid"),
            evidence=evidence,
        )

    daily_log_cursor = current_daily_log_cursor_consistency_check(
        storage_root=storage_root,
        state=state,
    )
    evidence["daily_log_cursor"] = daily_log_cursor
    actual_daily_log_cursor = (
        daily_log_cursor.get("actual_cursor")
        if isinstance(daily_log_cursor.get("actual_cursor"), dict)
        else None
    )
    required_file_keys = current_receipt_required_file_keys(
        storage_root=storage_root,
        state=state,
        daily_log_cursor=actual_daily_log_cursor,
    )
    evidence["required_current_file_keys"] = required_file_keys

    managed_file_state = current_managed_file_state_consistency_check(
        storage_root=storage_root,
        state=state,
        required_file_keys=required_file_keys,
    )
    evidence["managed_file_state"] = managed_file_state
    if managed_file_state.get("verified") is not True:
        return _strict_gate_block(
            reason_code=str(
                managed_file_state.get("reason_code")
                or "managed_file_state_consistency_mismatch"
            ),
            evidence=evidence,
            details=managed_file_state,
        )

    config_guard = current_config_marker_consistency_check(
        storage_root=storage_root,
        state=state,
        required_file_keys=required_file_keys,
        daily_log_cursor=actual_daily_log_cursor,
    )
    evidence["config_marker"] = config_guard
    if config_guard.get("verified") is not True:
        return _strict_gate_block(
            reason_code=str(config_guard.get("reason_code") or "config_marker_mismatch"),
            evidence=evidence,
            details=config_guard,
        )

    if daily_log_cursor.get("verified") is not True:
        return _strict_gate_block(
            reason_code=str(
                daily_log_cursor.get("reason_code")
                or "daily_log_cursor_consistency_mismatch"
            ),
            evidence=evidence,
            details=daily_log_cursor,
        )

    provenance_facts = provenance_facts_from_state(state, review_intent=True)
    evidence["provenance_facts"] = {
        "metadata_status": provenance_facts.get("metadata_status"),
        "helper_evidenced": provenance_facts.get("helper_evidenced"),
        "review_imported_baseline": provenance_facts.get("review_imported_baseline"),
        "review_required": provenance_facts.get("review_required"),
        "legacy_sidecar": provenance_facts.get("legacy_sidecar"),
        "inconsistent_evidence": provenance_facts.get("inconsistent_evidence"),
    }
    if provenance_facts.get("inconsistent_evidence") is True:
        return _strict_gate_block(
            reason_code="provenance_evidence_inconsistent",
            evidence=evidence,
            details=evidence["provenance_facts"],
        )
    if (
        provenance_facts.get("review_required") is True
        or provenance_facts.get("legacy_sidecar") is True
    ):
        return _strict_gate_block(
            reason_code="provenance_review_required",
            evidence=evidence,
            details=evidence["provenance_facts"],
        )

    if provenance_facts.get("helper_evidenced") is not True:
        return {
            "allowed_for_mutation": True,
            "blocked_reason": None,
            "reason_code": "strict_sidecar_integrity_verified_structural",
            "safe_next_action": "Proceed through the existing revision-aware managed helper path; do not claim receipt-backed provenance unless the write finalizes its own receipt.",
            "evidence": evidence,
            "details": {
                "receipt_store_required": False,
                "provenance_metadata_status": provenance_facts.get("metadata_status"),
            },
        }

    helper_evidence = bounded_current_helper_evidence_check(
        project_root=project_root,
        storage_root=storage_root,
        state=state,
        state_text=state_text,
        helper_evidenced_only=False,
        require_receipt_store=True,
        require_config_guard=False,
        daily_log_cursor=actual_daily_log_cursor,
    )
    evidence["helper_evidence"] = helper_evidence
    if helper_evidence.get("verified") is not True:
        return _strict_gate_block(
            reason_code=str(helper_evidence.get("reason_code") or "receipt_evidence_mismatch"),
            evidence=evidence,
            details=helper_evidence,
        )

    return {
        "allowed_for_mutation": True,
        "blocked_reason": None,
        "reason_code": "strict_sidecar_integrity_verified",
        "safe_next_action": "Proceed through a revision-aware managed helper mutation path.",
        "evidence": evidence,
        "details": {
            "verified_current_file_keys": helper_evidence.get(
                "verified_current_file_keys",
                [],
            ),
            "receipt_store": helper_evidence.get("receipt_store"),
        },
    }
