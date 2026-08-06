#!/usr/bin/env python3
"""Daily-log-append pure planner owner for RecallLoom (T050-03C seed).

Frozen by the v0.5.0 unique construction plan (§7.4 frozen callable contracts,
§7.8 landing map row 03C) — extracted from ``append_daily_log_entry.py``
(extract -> delegate -> parity, behavior unchanged).

What lives here (single owner):

- ``prepare_entry_text``: prepared-content -> candidate entry body text. The
  only content input boundary is the frozen ``AcquiredInput``
  (``core.safety.input_transport``); this module never reads stdin, files, or
  scratch itself. The entry-JSON normalization (including every
  input-validation rejection) moved verbatim.
- ``validate_entry_body`` / ``validate_entry_section_structure``: the pure
  reserved-marker, attached-text safety scan, and section-structure validation
  with their exact failure-details assembly.
- ``build_entry_block``: candidate entry-block render (entry marker + body).
  The entry ``created_at`` is an explicit ``timestamp`` parameter so the
  planner stays deterministic.
- ``resolve_target_date``: the pure explicit/auto target-date resolution.
- ``build_append_receipt_seed``: the helper_write receipt seed (argparse-free;
  the helper passes ``helper_version``).
- The pure state-delta computation (``apply_append_state_delta``) and
  ``expected_state_json_text`` serialization, plus the ``sha256_text_digest``
  digest helper used by the post-append hash assertions.

Typed failure model: validation raises ``DailyLogAppendPlannerError`` carrying
the exact legacy exit projection (``message`` / ``reason`` / ``exit_code`` /
``details``, or a prebuilt ``payload`` for the attach-scan path, mirroring the
``InputTransportError`` pattern); the helper adapter projects it onto the
legacy exit contract. This module never calls ``SystemExit``, never uses
argparse/subprocess/locks, performs no IO orchestration and no persistent
writes, and never imports ``scripts/`` or ``_common.py`` (core must not depend
on adapter facades). ``entry_path``/``project_root`` remain explicit planner
parameters used only for byte-identical failure-details path rendering
(non-strict path normalization, no filesystem access).

Daily-log cursor parsing is single-owned by ``core.continuity.daily_log``
(T050-03A); this planner does not duplicate it — the helper keeps parsing
cursors during its IO orchestration and passes plain values in.

The helper ``append_daily_log_entry.py`` keeps thin delegating wrappers; the
helper implementation bodies are deleted only after the T050-06 transaction
cutover (§7.8 deletion condition for row 03C).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

from core.failure.contracts import failure_payload, preferred_failure_language
from core.protocol.contracts import SECTION_KEYS
from core.protocol.markers import (
    canonicalize_managed_text_newlines,
    daily_log_entry_marker,
    file_marker,
    parse_daily_log_scaffold_marker,
    parse_file_marker,
    section_marker,
)
from core.continuity.daily_log import daily_log_cursor_from_text
from core.protocol.sections import (
    duplicate_section_keys,
    missing_section_keys,
    unknown_section_keys,
)
from core.provenance.receipts import RECEIPT_SCHEMA_VERSION
from core.provenance.state import helper_evidenced_metadata
from core.safety.attached_text import scan_auto_attached_context_text
from core.safety.input_transport import AcquiredInput
from core.recording.managed_write import OperationPlan, OperationPreplan


DAILY_LOG_ENTRY_JSON_RETRY_PAYLOAD_SHAPE = {
    key: "non-empty string | list[non-empty string]"
    for key in SECTION_KEYS["daily_log"]
}
DAILY_LOG_ENTRY_JSON_ACCEPTED_SHAPES = ("string", "list[string]")
RESERVED_MARKER_FAMILIES = (
    ("<!-- recallloom:file=", "file_marker"),
    ("<!-- last-writer:", "last_writer_marker"),
    ("<!-- file-state:", "file_state_marker"),
    ("<!-- daily-log-entry:", "daily_log_entry_marker"),
    ("<!-- daily-log-scaffold", "daily_log_scaffold_marker"),
)


class DailyLogAppendPlannerError(Exception):
    """Pure-planner validation failure carrying the exact legacy exit projection.

    The helper adapter projects ``message`` / ``reason`` / ``exit_code`` /
    ``details`` through ``exit_with_failure_contract``; when ``payload`` is set
    the adapter uses ``exit_with_cli_error`` with the prebuilt failure payload
    instead (the attach-scan path). This module never raises ``SystemExit``.
    """

    def __init__(
        self,
        *,
        message: str,
        reason: str = "invalid_prepared_input",
        exit_code: int = 2,
        details: dict | None = None,
        payload: dict | None = None,
    ) -> None:
        self.message = message
        self.reason = reason
        self.exit_code = exit_code
        self.details = details
        self.payload = payload
        super().__init__(message)


# --- digest / state serializers ------------------------------------------------


def sha256_text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def expected_state_json_text(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"


# --- receipt seed ----------------------------------------------------------------


def build_append_receipt_seed(
    *,
    preflight_binding: dict,
    timestamp: str,
    target_digest: str,
    state_digest: str,
    previous_entry_seq: int,
    next_entry_seq: int,
    new_workspace_revision: int,
    helper_version: str,
) -> dict:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_type": "helper_write",
        "helper_name": "append_daily_log_entry.py",
        "helper_version": helper_version,
        "operation": "milestone_evidence",
        "operation_class": "daily_log_append",
        "side_effect": "target_and_state_written",
        "result": "ok",
        "state_label_before": preflight_binding.get("provenance_state") or "structurally_valid",
        "state_label_after": "helper_evidenced",
        "target_file_key": "daily_log",
        "target_digest": target_digest,
        "state_digest": state_digest,
        "preflight_contract_identity": preflight_binding["preflight_contract_identity"],
        "expected_workspace_revision": preflight_binding["expected_workspace_revision"],
        "result_workspace_revision": new_workspace_revision,
        "expected_file_revision": previous_entry_seq,
        "result_file_revision": next_entry_seq,
        "created_at": timestamp,
    }


# --- reserved marker + attached-text validation -------------------------------


def reserved_marker_lines(text: str, *, match_embedded: bool = False) -> list[dict[str, object]]:
    results = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        for prefix, marker_family in RESERVED_MARKER_FAMILIES:
            if candidate.startswith(prefix) or (match_embedded and prefix in candidate):
                results.append(
                    {
                        "line_number": line_number,
                        "marker_family": marker_family,
                    }
                )
                break
    return results


def reserved_marker_failure_details(
    recovery_details: dict | None = None,
    *,
    line_number: int,
    marker_family: str,
    section_key: str | None = None,
) -> dict:
    details: dict[str, object] = {}
    input_mode = (recovery_details or {}).get("input_mode")
    if isinstance(input_mode, str) and input_mode.strip():
        details["input_mode"] = input_mode
    details.update(
        {
            "reason_code": "reserved_marker_injection",
            "line_number": line_number,
            "marker_family": marker_family,
            "side_effect": "none",
        }
    )
    if section_key is not None:
        details["section_key"] = section_key
        details["field_path"] = f"$.{section_key}"
    return details


def validate_entry_body(
    *,
    body_text: str,
    recovery_details: dict | None = None,
) -> None:
    reserved = reserved_marker_lines(body_text, match_embedded=True)
    if reserved:
        hit = reserved[0]
        line_number = int(hit["line_number"])
        raise DailyLogAppendPlannerError(
            message=(
                "Refusing to append because the prepared entry contains a reserved RecallLoom marker "
                f"on line {line_number}."
            ),
            details=reserved_marker_failure_details(
                recovery_details,
                line_number=line_number,
                marker_family=str(hit["marker_family"]),
            ),
        )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        message = (
            "Refusing to append because the prepared entry failed the attached-text safety scan: "
            + ", ".join(attach_scan["hard_block_reasons"])
        )
        raise DailyLogAppendPlannerError(
            message=message,
            payload=failure_payload(
                "attach_scan_blocked",
                language=preferred_failure_language(os.environ),
                error=message,
                details={
                    **(recovery_details or {}),
                    "hard_block_reasons": attach_scan["hard_block_reasons"],
                },
            ),
        )


# --- entry JSON input normalization ---------------------------------------------


def daily_log_json_failure_details(
    recovery_details: dict | None = None,
    *,
    field_path: str = "$",
    expected_type: str = "daily_log_json_object",
    reason_code: str,
    section_key: str | None = None,
    extra: dict | None = None,
) -> dict:
    details = {
        **(recovery_details or {}),
        "prepared_input_builder": "daily_log_entry_json",
        "field_path": field_path,
        "expected_type": expected_type,
        "accepted_shapes": list(DAILY_LOG_ENTRY_JSON_ACCEPTED_SHAPES),
        "retry_payload_shape": dict(DAILY_LOG_ENTRY_JSON_RETRY_PAYLOAD_SHAPE),
        "allowed_section_keys": list(SECTION_KEYS["daily_log"]),
        "reason_code": reason_code,
        "side_effect": "none",
    }
    if section_key is not None:
        details["section_key"] = section_key
    if extra:
        details.update(extra)
    return details


def invalid_json_section_value(
    *,
    section_key: str,
    message: str,
    recovery_details: dict | None = None,
    field_path: str | None = None,
    expected_type: str = "string_or_string_array",
    reason_code: str = "invalid_section_value_type",
) -> None:
    details = daily_log_json_failure_details(
        recovery_details,
        field_path=field_path or f"$.{section_key}",
        expected_type=expected_type,
        reason_code=reason_code,
        section_key=section_key,
    )
    raise DailyLogAppendPlannerError(message=message, details=details)


def reject_json_reserved_markers(
    *,
    section_key: str,
    text: str,
    recovery_details: dict | None = None,
) -> None:
    reserved = reserved_marker_lines(text, match_embedded=True)
    if not reserved:
        return
    hit = reserved[0]
    line_number = int(hit["line_number"])
    raise DailyLogAppendPlannerError(
        message=(
            "Refusing to append because prepared entry JSON section "
            f"'{section_key}' contains a reserved RecallLoom marker on line {line_number}."
        ),
        details=reserved_marker_failure_details(
            recovery_details,
            line_number=line_number,
            marker_family=str(hit["marker_family"]),
            section_key=section_key,
        ),
    )


def render_json_list_item(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return "- "
    rendered = [f"- {lines[0]}"]
    rendered.extend(f"  {line}" if line else "  " for line in lines[1:])
    return "\n".join(rendered)


def normalize_json_section_value(
    *,
    section_key: str,
    value: object,
    recovery_details: dict | None = None,
) -> str:
    if isinstance(value, str):
        normalized = canonicalize_managed_text_newlines(value.strip())
        if normalized:
            reject_json_reserved_markers(
                section_key=section_key,
                text=normalized,
                recovery_details=recovery_details,
            )
            return normalized
        invalid_json_section_value(
            section_key=section_key,
            message=(
                f"Prepared entry JSON section '{section_key}' must be a non-empty string "
                "or a non-empty list of strings."
            ),
            recovery_details=recovery_details,
            reason_code="empty_section_string",
        )

    if isinstance(value, list):
        if not value:
            invalid_json_section_value(
                section_key=section_key,
                message=(
                    f"Prepared entry JSON section '{section_key}' must be a non-empty string "
                    "or a non-empty list of strings."
                ),
                recovery_details=recovery_details,
                reason_code="empty_section_list",
            )
        rendered_items: list[str] = []
        for item in value:
            if not isinstance(item, str):
                invalid_json_section_value(
                    section_key=section_key,
                    message=(
                        f"Prepared entry JSON section '{section_key}' list items must be non-empty strings."
                    ),
                    recovery_details=recovery_details,
                    field_path=f"$.{section_key}[]",
                    expected_type="non_empty_string",
                    reason_code="invalid_section_list_item_type",
                )
            normalized_item = canonicalize_managed_text_newlines(item.strip())
            if not normalized_item:
                invalid_json_section_value(
                    section_key=section_key,
                    message=(
                        f"Prepared entry JSON section '{section_key}' list items must be non-empty strings."
                    ),
                    recovery_details=recovery_details,
                    field_path=f"$.{section_key}[]",
                    expected_type="non_empty_string",
                    reason_code="empty_section_list_item",
                )
            reject_json_reserved_markers(
                section_key=section_key,
                text=normalized_item,
                recovery_details=recovery_details,
            )
            rendered_items.append(render_json_list_item(normalized_item))
        return "\n".join(rendered_items)

    invalid_json_section_value(
        section_key=section_key,
        message=(
            f"Prepared entry JSON section '{section_key}' must be a non-empty string "
            "or a non-empty list of strings."
        ),
        recovery_details=recovery_details,
    )
    raise AssertionError("unreachable")


def normalize_json_entry_text(
    *,
    raw_text: str,
    recovery_details: dict | None = None,
) -> str:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DailyLogAppendPlannerError(
            message=(
                "Prepared entry JSON must be a valid JSON object: "
                f"{exc.msg} at line {exc.lineno} column {exc.colno}."
            ),
            details=daily_log_json_failure_details(
                recovery_details,
                field_path="$",
                expected_type="valid_json_object",
                reason_code="malformed_json",
                extra={"json_error_line": exc.lineno, "json_error_column": exc.colno},
            ),
        ) from exc

    if not isinstance(payload, dict):
        raise DailyLogAppendPlannerError(
            message="Prepared entry JSON must be an object keyed by daily-log section names.",
            details=daily_log_json_failure_details(
                recovery_details,
                field_path="$",
                expected_type="object",
                reason_code="top_level_not_object",
            ),
        )

    required_keys = list(SECTION_KEYS["daily_log"])
    unknown_keys = sorted(key for key in payload if key not in required_keys)
    if unknown_keys:
        details = daily_log_json_failure_details(
            recovery_details,
            field_path="$.<section_key>",
            expected_type="allowed_section_key",
            reason_code="unknown_section_key",
            extra={
                "unknown_section_key_count": len(unknown_keys),
                "unknown_key_values_public_safe": False,
            },
        )
        raise DailyLogAppendPlannerError(
            message="Prepared entry JSON contains unknown daily-log section keys.",
            details=details,
        )

    missing_keys = [key for key in required_keys if key not in payload]
    if missing_keys:
        details = daily_log_json_failure_details(
            recovery_details,
            field_path="$",
            expected_type="object_with_all_required_sections",
            reason_code="missing_section_key",
            extra={"missing_section_keys": missing_keys},
        )
        raise DailyLogAppendPlannerError(
            message=(
                "Prepared entry JSON is missing required daily-log section keys: "
                + ", ".join(missing_keys)
            ),
            details=details,
        )

    sections: list[str] = []
    for section_key in required_keys:
        sections.append(
            section_marker(section_key)
            + "\n"
            + normalize_json_section_value(
                section_key=section_key,
                value=payload[section_key],
                recovery_details=recovery_details,
            )
        )
    return "\n\n".join(sections) + "\n"


# --- prepared entry pipeline (AcquiredInput content boundary) -------------------


def prepare_entry_text(
    acquired: AcquiredInput,
    *,
    input_format: str,
    entry_path: Path | None,
    project_root: Path | None = None,
) -> tuple[str, str]:
    """Prepared content -> (candidate entry body text, effective input mode).

    The content arrives only as the frozen ``AcquiredInput``; ``entry_path`` /
    ``project_root`` are failure-details routing parameters, never read.
    """
    source_kind = acquired.input_mode
    raw_text = acquired.decoded_text
    if source_kind == "entry-json":
        recovery_details = {"input_mode": "json-string"}
        if input_format == "markdown":
            raise DailyLogAppendPlannerError(
                message="--entry-json only supports JSON input. Use --input-format auto or --input-format json.",
                details=recovery_details,
            )
        return (
            normalize_json_entry_text(
                raw_text=raw_text,
                recovery_details=recovery_details,
            ),
            "json-string",
        )

    effective_input_format = "markdown" if input_format == "auto" else input_format
    if effective_input_format == "json":
        input_mode = "json-file" if source_kind == "file" else "json-stdin"
        recovery_details: dict[str, object] = {"input_mode": input_mode}
        if entry_path is not None:
            recovery_details["entry_path"] = str(entry_path)
        if project_root is not None:
            recovery_details["project_root"] = str(project_root)
        return (
            normalize_json_entry_text(
                raw_text=raw_text,
                recovery_details=recovery_details,
            ),
            input_mode,
        )

    return canonicalize_managed_text_newlines(raw_text), source_kind


# --- entry-body section-structure validation -------------------------------------


def validate_entry_section_structure(
    *,
    body_text: str,
    recovery_details: dict,
) -> None:
    missing_keys = missing_section_keys(body_text, SECTION_KEYS["daily_log"])
    if missing_keys:
        raise DailyLogAppendPlannerError(
            message=(
                "Refusing to append a daily-log entry because the prepared entry file is missing required "
                "section markers: " + ", ".join(missing_keys)
            ),
            details={**recovery_details, "missing_section_keys": missing_keys},
        )
    duplicate_keys = duplicate_section_keys(body_text)
    if duplicate_keys:
        raise DailyLogAppendPlannerError(
            message=(
                "Refusing to append a daily-log entry because the prepared entry file contains duplicate "
                "section markers: " + ", ".join(duplicate_keys)
            ),
            details={**recovery_details, "duplicate_section_keys": duplicate_keys},
        )
    unknown_keys = unknown_section_keys(body_text, SECTION_KEYS["daily_log"])
    if unknown_keys:
        raise DailyLogAppendPlannerError(
            message=(
                "Refusing to append a daily-log entry because the prepared entry file contains unknown "
                "section markers: " + ", ".join(unknown_keys)
            ),
            details={**recovery_details, "unknown_section_keys": unknown_keys},
        )


# --- candidate entry block render --------------------------------------------------


def build_entry_block(body_text: str, *, writer_id: str, entry_seq: int, timestamp: str) -> str:
    marker = daily_log_entry_marker(
        entry_id=f"entry-{entry_seq}",
        created_at=timestamp,
        writer_id=writer_id,
        entry_seq=entry_seq,
    )
    body = canonicalize_managed_text_newlines(body_text).strip("\n")
    return marker if not body else marker + "\n\n" + body


# --- target date resolution ---------------------------------------------------------


def resolve_target_date(
    *,
    explicit_date: str | None,
    latest_existing: Path | None,
    logical_workday: date,
) -> tuple[date, date | None, str]:
    latest_existing_date = (
        date.fromisoformat(latest_existing.stem) if latest_existing is not None else None
    )
    if explicit_date is not None:
        return date.fromisoformat(explicit_date), latest_existing_date, "explicit"
    if latest_existing_date is not None and latest_existing_date == logical_workday:
        return latest_existing_date, latest_existing_date, "auto_same_day_active"
    return logical_workday, latest_existing_date, "auto_logical_workday"


# --- state delta ---------------------------------------------------------------------


def apply_append_state_delta(
    state: dict,
    *,
    new_workspace_revision: int,
    target_is_latest_after_write: bool,
    target_latest_file: str,
    next_entry_seq: int,
    refreshed_at: str | None,
    record_provenance: bool,
    previous_state_label: str | None,
    provenance_timestamp: str,
) -> dict:
    """Apply the daily-log append state delta in place and return ``state``."""
    state["workspace_revision"] = new_workspace_revision
    if target_is_latest_after_write:
        state["daily_logs"]["latest_file"] = target_latest_file
        state["daily_logs"]["latest_entry_id"] = f"entry-{next_entry_seq}"
        state["daily_logs"]["latest_entry_seq"] = next_entry_seq
        state["daily_logs"]["entry_count"] = next_entry_seq
        state["daily_logs"]["updated_at"] = refreshed_at
    if record_provenance:
        state["provenance"] = helper_evidenced_metadata(
            timestamp=provenance_timestamp,
            previous_state_label=(
                previous_state_label if isinstance(previous_state_label, str) else None
            ),
        )
    return state


# --- concrete transaction planners (T050-06) ---------------------------------


def _canonical_cursor(cursor: object) -> dict[str, object]:
    """Return the frozen daily-log cursor shape without cursor objects."""
    if not isinstance(cursor, dict):
        raise TypeError("daily-log cursor must be a mapping")
    return {
        "latest_file": cursor.get("latest_file"),
        "latest_entry_id": cursor.get("latest_entry_id"),
        "latest_entry_seq": cursor.get("latest_entry_seq"),
        "entry_count": cursor.get("entry_count"),
    }


def preplan_daily_log_append(
    acquired: AcquiredInput,
    *,
    target_path: Path,
    state_path: Path,
    current_target_text: str | None,
    current_target_identity: str | None,
    state: dict,
    expected_file_revision: int,
    expected_workspace_revision: int,
    expected_cursor: dict[str, object],
    expected_language: str,
    writer_id: str,
    timestamp: str,
    input_format: str,
    preflight_binding: dict | None,
    target_is_latest_after_write: bool,
    source_file: str | None = None,
    project_root: Path | None = None,
) -> OperationPreplan:
    """Build the append candidate once; no I/O or cursor-object leakage."""
    if state["workspace_revision"] != expected_workspace_revision:
        message = (
            f"Workspace revision changed from {expected_workspace_revision} to "
            f"{state['workspace_revision']}. Rerun preflight before appending."
        )
        raise DailyLogAppendPlannerError(
            message=message,
            exit_code=3,
            payload=failure_payload(
                "stale_write_context",
                language=expected_language,
                error=message,
                details={
                    "expected_workspace_revision": expected_workspace_revision,
                    "current_workspace_revision": state["workspace_revision"],
                },
            ),
        )

    recovery = {"input_mode": acquired.input_mode}
    body, _ = prepare_entry_text(
        acquired,
        input_format=input_format,
        entry_path=Path(source_file) if source_file else None,
        project_root=project_root,
    )
    validate_entry_body(body_text=body, recovery_details=recovery)
    validate_entry_section_structure(body_text=body, recovery_details=recovery)

    expected_cursor = _canonical_cursor(expected_cursor)
    if current_target_text is None:
        next_seq = 1
        candidate = (
            file_marker("daily_log", expected_language)
            + "\n"
            + build_entry_block(body, writer_id=writer_id, entry_seq=next_seq, timestamp=timestamp)
            + "\n"
        )
    else:
        marker = parse_file_marker(current_target_text)
        if (
            marker is None
            or marker.file_key != "daily_log"
            or marker.language != expected_language
        ):
            raise DailyLogAppendPlannerError(
                message=(
                    "Target daily log is missing a valid daily_log file marker: "
                    f"{target_path}"
                ),
                reason="malformed_managed_file",
                details={"path": str(target_path)},
            )
        parsed_cursor = daily_log_cursor_from_text(
            current_target_text, path=target_path, latest_file=target_path.name
        )
        next_seq = parsed_cursor.entry_count + 1
        managed = canonicalize_managed_text_newlines(current_target_text)
        block = build_entry_block(
            body, writer_id=writer_id, entry_seq=next_seq, timestamp=timestamp
        )
        if parse_daily_log_scaffold_marker(managed) and parsed_cursor.entry_count == 0:
            candidate = file_marker("daily_log", expected_language) + "\n" + block + "\n"
        else:
            candidate = managed.rstrip("\n") + "\n\n" + block + "\n"

    if target_is_latest_after_write:
        new_cursor = {
            "latest_file": target_path.relative_to(state_path.parent).as_posix(),
            "latest_entry_id": f"entry-{next_seq}",
            "latest_entry_seq": next_seq,
            "entry_count": next_seq,
        }
    else:
        new_cursor = dict(expected_cursor)

    return OperationPreplan(
        operation="daily_log_append",
        target_path=target_path,
        target_identity=current_target_identity,
        state_path=state_path,
        expected_file_revision=expected_file_revision,
        new_file_revision=next_seq,
        expected_workspace_revision=expected_workspace_revision,
        new_workspace_revision=state["workspace_revision"] + 1,
        expected_cursor=expected_cursor,
        new_cursor=new_cursor,
        candidate_target_bytes=candidate.encode("utf-8"),
        receipt_precheck_inputs={
            "file_key": "daily_log",
            "new_file_revision": next_seq,
            "new_workspace_revision": state["workspace_revision"] + 1,
            "preflight_binding_present": preflight_binding is not None,
            "target_is_latest_after_write": target_is_latest_after_write,
        },
    )


def seal_daily_log_append_plan(
    preplan: OperationPreplan,
    *,
    current_target_text: str | None,
    current_state_text: str,
    state: dict,
    previous_state_label: str,
    preflight_binding: dict | None,
    timestamp: str,
    helper_version: str,
) -> OperationPlan:
    """Seal the precomputed candidate and state delta without recomputation."""
    target_is_latest = bool(preplan.receipt_precheck_inputs["target_is_latest_after_write"])
    new_state = apply_append_state_delta(
        json.loads(json.dumps(state)),
        new_workspace_revision=preplan.new_workspace_revision,
        target_is_latest_after_write=target_is_latest,
        target_latest_file=str(preplan.new_cursor["latest_file"]),
        next_entry_seq=preplan.new_file_revision,
        refreshed_at=timestamp if target_is_latest else None,
        record_provenance=preflight_binding is not None and target_is_latest,
        previous_state_label=previous_state_label,
        provenance_timestamp=timestamp,
    )
    return OperationPlan(
        operation=preplan.operation,
        target_path=preplan.target_path,
        previous_target_bytes=(current_target_text or "").encode("utf-8"),
        new_target_bytes=preplan.candidate_target_bytes,
        target_identity=preplan.target_identity,
        state_path=preplan.state_path,
        previous_state_bytes=current_state_text.encode("utf-8"),
        new_state_bytes=expected_state_json_text(new_state).encode("utf-8"),
        expected_file_revision=preplan.expected_file_revision,
        new_file_revision=preplan.new_file_revision,
        expected_workspace_revision=preplan.expected_workspace_revision,
        new_workspace_revision=preplan.new_workspace_revision,
        expected_cursor=preplan.expected_cursor,
        new_cursor=preplan.new_cursor,
        receipt_seed_inputs={
            "preflight_binding": preflight_binding,
            "timestamp": timestamp,
            "helper_version": helper_version,
            "previous_entry_seq": preplan.new_file_revision - 1,
            "target_is_latest_after_write": target_is_latest,
        },
    )
