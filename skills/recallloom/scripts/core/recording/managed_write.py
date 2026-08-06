#!/usr/bin/env python3
"""Managed-write pure planner owner for RecallLoom (T050-03B seed).

Frozen by the v0.5.0 unique construction plan (§7.4 frozen callable contracts,
§7.8 landing map row 03B) — extracted from ``commit_context_file.py``
(extract -> delegate -> parity, behavior unchanged).

What lives here (single owner):

- ``prepare_body_text``: prepared-content -> candidate body text. The only
  content input boundary is the frozen ``AcquiredInput``
  (``core.safety.input_transport``); this module never reads stdin, files, or
  scratch itself. Markdown header stripping and the rolling-summary JSON
  normalization (including every input-validation rejection) moved verbatim.
- ``validate_prepared_body`` / ``validate_managed_section_structure``: the pure
  reserved-marker, attached-text safety scan, and section-structure validation
  with their exact failure-details assembly.
- ``build_managed_text``: candidate managed-file render (marker + state header
  + body). The rolling-summary last-writer date is an explicit ``today``
  parameter so the planner stays deterministic.
- ``build_receipt_seed``: the helper_write receipt seed (argparse-free; the
  helper passes ``file_key`` / expected revisions / ``helper_version``).
- The pure state-delta computation (``apply_managed_write_state_delta``) and
  ``expected_state_json_text`` serialization, plus the digest/snapshot helpers
  (``sha256_text_digest``, ``managed_body_digest``, ``managed_metadata_snapshot``,
  ``controlled_metadata_refresh_claim``) used by the metadata-only refresh
  assertions.
- The frozen §7.4 operation contracts (``OperationPreplan`` / ``OperationPlan``)
  and the pure ``preplan_managed_write`` / ``seal_managed_write_plan`` pair
  (T050-04A): the preplan computes candidate bytes, new revisions, and receipt
  precheck inputs exactly once in the legacy validation order; the seal runs
  after the transaction's read-only receipt precheck and only performs final
  validation/seal without re-reading or recomputing the candidate.
- The concrete post-append summary-sync planners ``preplan_post_append_sync`` /
  ``seal_post_append_sync_plan`` (T050-05, §7.4's named per-operation pairs):
  the sync lane reuses this single managed render/validate/delta
  implementation with the operation pinned to ``post_append_summary_sync``
  (v0.4.8.3 sync semantics exactly — both the metadata-only
  reuse-current-summary assertion family and the regular current-state sync);
  no second commit implementation exists.

Typed failure model: validation raises ``ManagedWritePlannerError`` carrying the
exact legacy exit projection (``message`` / ``reason`` / ``exit_code`` /
``details``, or a prebuilt ``payload`` for the attach-scan path, mirroring the
``InputTransportError`` pattern); the helper adapter projects it onto the legacy
exit contract. This module never calls ``SystemExit``, never uses
argparse/subprocess/locks, performs no IO orchestration and no persistent
writes, and never imports ``scripts/`` or ``_common.py`` (core must not depend
on adapter facades). ``source_file``/``project_root`` remain explicit planner
parameters used only for byte-identical failure-details path rendering
(non-strict path normalization, no filesystem access).

The helper ``commit_context_file.py`` keeps thin delegating wrappers; the helper
implementation bodies are deleted only after the T050-04 transaction cutover
(§7.8 deletion condition for row 03B).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.errors import ConfigContractError
from core.failure.context import (
    COMMAND_SYNC_CURRENT_STATE_AFTER_APPEND,
    COMMAND_WRITE,
    OPERATION_MANAGED_WRITE,
    OPERATION_POST_APPEND_SUMMARY_SYNC,
    STAGE_INPUT,
    OperationContext,
)
from core.failure.contracts import failure_payload, preferred_failure_language
from core.protocol.contracts import (
    CURRENT_PROTOCOL_VERSION,
    LAST_WRITER_RE,
    OPTIONAL_SECTION_KEYS,
    SECTION_KEYS,
)
from core.protocol.markers import (
    canonicalize_managed_text_newlines,
    file_marker,
    file_state_marker,
    parse_file_marker,
    parse_file_state_marker,
    rolling_summary_header,
    section_marker,
    validate_tool_name,
    validate_writer_id,
)
from core.protocol.sections import (
    duplicate_section_keys,
    missing_section_keys,
    unknown_section_keys,
)
from core.provenance.receipts import RECEIPT_SCHEMA_VERSION
from core.provenance.state import helper_evidenced_metadata
from core.safety.attached_text import scan_auto_attached_context_text
from core.safety.input_transport import AcquiredInput


WRITE_TYPE_BY_FILE_KEY = {
    "context_brief": "stable-context",
    "rolling_summary": "current-state",
    "update_protocol": "protocol-rules",
}
RESERVED_MARKER_FAMILIES = (
    ("<!-- recallloom:file=", "file_marker"),
    ("<!-- last-writer:", "last_writer_marker"),
    ("<!-- file-state:", "file_state_marker"),
    ("<!-- daily-log-entry:", "daily_log_entry_marker"),
    ("<!-- daily-log-scaffold", "daily_log_scaffold_marker"),
)
ROLLING_SUMMARY_JSON_KEYS = (
    "current_state",
    "active_judgments",
    "risks_open_questions",
    "next_step",
    "recent_pivots",
)
NOT_PROVIDED_SENTINELS = {"not_provided"}
ROLLING_SUMMARY_JSON_ACCEPTED_SHAPES = (
    "top-level object with exactly the allowed rolling-summary section keys",
    "section value as a non-empty string",
    "section value as a list of non-empty strings",
    "section value as [] for an intentionally empty section",
    "section value as 'not_provided' for an intentionally empty section",
)
ROLLING_SUMMARY_JSON_RETRY_PAYLOAD_SHAPE = {
    key: "non-empty string | list[non-empty string] | [] | 'not_provided'"
    for key in ROLLING_SUMMARY_JSON_KEYS
}


class ManagedWritePlannerError(Exception):
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


# --- managed header stripping -------------------------------------------------


def _managed_header_line_count(file_key: str, text: str, *, expected_language: str) -> int | None:
    lines = text.splitlines()
    idx = 0
    last_writer_tool: str | None = None
    if idx >= len(lines):
        return None
    marker = parse_file_marker(lines[idx])
    if (
        marker is None
        or marker.file_key != file_key
        or marker.version != CURRENT_PROTOCOL_VERSION
        or marker.language != expected_language
    ):
        return None
    idx += 1
    if file_key == "rolling_summary":
        if idx >= len(lines):
            return None
        last_writer_match = LAST_WRITER_RE.match(lines[idx].strip())
        if last_writer_match is None:
            return None
        tool_name = last_writer_match.group("tool").strip()
        try:
            last_writer_tool = validate_tool_name(tool_name)
        except ConfigContractError:
            return None
        idx += 1
    if idx >= len(lines):
        return None
    file_state = parse_file_state_marker(lines[idx])
    if file_state is None:
        return None
    try:
        writer_id = validate_writer_id(file_state.writer_id)
    except ConfigContractError:
        return None
    if file_key == "rolling_summary" and last_writer_tool != writer_id:
        return None
    return idx + 1


def strip_managed_headers(
    file_key: str,
    text: str,
    *,
    expected_language: str,
) -> str:
    source_lines = text.splitlines()
    source_header_count = _managed_header_line_count(file_key, text, expected_language=expected_language)
    if source_header_count is None:
        return text

    return "\n".join(source_lines[source_header_count:]).lstrip("\n")


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
    include_route: bool = False,
) -> dict:
    details: dict[str, object] = {}
    input_mode = (recovery_details or {}).get("input_mode")
    if isinstance(input_mode, str) and input_mode.strip():
        details["input_mode"] = input_mode
    if include_route:
        file_key = (recovery_details or {}).get("file_key")
        write_type = (recovery_details or {}).get("write_type")
        if isinstance(file_key, str) and file_key.strip():
            details["file_key"] = file_key
        if isinstance(write_type, str) and write_type.strip():
            details["write_type"] = write_type
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


def reject_reserved_markers(
    *,
    text: str,
    recovery_details: dict | None = None,
    include_reserved_marker_route: bool = False,
    prepared_label: str = "prepared body",
) -> None:
    reserved = reserved_marker_lines(text, match_embedded=True)
    if not reserved:
        return
    hit = reserved[0]
    line_number = int(hit["line_number"])
    raise ManagedWritePlannerError(
        message=(
            f"Refusing to commit because the {prepared_label} contains a reserved RecallLoom marker "
            f"on line {line_number}."
        ),
        details=reserved_marker_failure_details(
            recovery_details,
            line_number=line_number,
            marker_family=str(hit["marker_family"]),
            include_route=include_reserved_marker_route,
        ),
    )


def validate_prepared_body(
    *,
    body_text: str,
    recovery_details: dict | None = None,
    include_reserved_marker_route: bool = False,
) -> None:
    reject_reserved_markers(
        text=body_text,
        recovery_details=recovery_details,
        include_reserved_marker_route=include_reserved_marker_route,
    )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        message = (
            "Refusing to commit because the prepared body failed the attached-text safety scan: "
            + ", ".join(attach_scan["hard_block_reasons"])
        )
        raise ManagedWritePlannerError(
            message=message,
            payload=failure_payload(
                "attach_scan_blocked",
                language=preferred_failure_language(os.environ),
                error=message,
                details={"hard_block_reasons": attach_scan["hard_block_reasons"]},
            ),
        )


# --- rolling-summary JSON input normalization ---------------------------------


def rolling_summary_json_failure_details(
    recovery_details: dict | None = None,
    *,
    field_path: str = "$",
    expected_type: str = "rolling_summary_json_object",
    reason_code: str,
    section_key: str | None = None,
    extra: dict | None = None,
) -> dict:
    context = OperationContext(
        command=COMMAND_WRITE,
        operation=OPERATION_MANAGED_WRITE,
        write_type="current-state",
        input_mode=None,
        stage=STAGE_INPUT,
    )
    details = {
        **(recovery_details or {}),
        "command": context.command,
        "operation": context.legacy_operation,
        "prepared_input_builder": "rolling_summary_json",
        "file_key": "rolling_summary",
        "write_type": context.write_type,
        "field_path": field_path,
        "expected_type": expected_type,
        "accepted_shapes": list(ROLLING_SUMMARY_JSON_ACCEPTED_SHAPES),
        "retry_payload_shape": dict(ROLLING_SUMMARY_JSON_RETRY_PAYLOAD_SHAPE),
        "allowed_section_keys": list(ROLLING_SUMMARY_JSON_KEYS),
        "reason_code": reason_code,
        "side_effect": "none",
        "trust_effect": "none",
    }
    if section_key is not None:
        details["section_key"] = section_key
    if extra:
        details.update(extra)
    if recovery_details:
        for route_key in ("command", "operation"):
            route_value = recovery_details.get(route_key)
            if isinstance(route_value, str) and route_value.strip():
                details[route_key] = route_value
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
    details = rolling_summary_json_failure_details(
        recovery_details,
        field_path=field_path or f"$.{section_key}",
        expected_type=expected_type,
        reason_code=reason_code,
        section_key=section_key,
    )
    raise ManagedWritePlannerError(message=message, details=details)


def render_json_list_item(text: str) -> str:
    lines = text.splitlines()
    rendered = [f"- {lines[0]}"]
    rendered.extend(f"  {line}" if line else "  " for line in lines[1:])
    return "\n".join(rendered)


def reject_json_reserved_markers(
    *,
    section_key: str,
    text: str,
    recovery_details: dict | None,
) -> None:
    reserved = reserved_marker_lines(text, match_embedded=True)
    if not reserved:
        return
    hit = reserved[0]
    line_number = int(hit["line_number"])
    details = reserved_marker_failure_details(
        recovery_details,
        line_number=line_number,
        marker_family=str(hit["marker_family"]),
        section_key=section_key,
    )
    raise ManagedWritePlannerError(
        message=(
            "Refusing to commit because prepared rolling-summary JSON section "
            f"'{section_key}' contains a reserved RecallLoom marker on line {line_number}."
        ),
        details=details,
    )


def normalize_json_section_value(
    *,
    section_key: str,
    value: object,
    recovery_details: dict | None = None,
) -> str:
    if isinstance(value, str):
        normalized = canonicalize_managed_text_newlines(value.strip())
        if normalized.casefold() in NOT_PROVIDED_SENTINELS:
            return ""
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
                f"Prepared rolling-summary JSON section '{section_key}' must be a non-empty string "
                "or a list of non-empty strings; use [] or 'not_provided' for an empty section."
            ),
            recovery_details=recovery_details,
            reason_code="empty_section_string",
        )

    if isinstance(value, list):
        if not value:
            return ""
        rendered_items: list[str] = []
        for item in value:
            if not isinstance(item, str):
                invalid_json_section_value(
                    section_key=section_key,
                    message=(
                        f"Prepared rolling-summary JSON section '{section_key}' list items must be non-empty strings."
                    ),
                    recovery_details=recovery_details,
                    field_path=f"$.{section_key}[]",
                    expected_type="non_empty_string",
                    reason_code="invalid_section_list_item_type",
                )
            normalized_item = canonicalize_managed_text_newlines(item.strip())
            if normalized_item.casefold() in NOT_PROVIDED_SENTINELS:
                continue
            if not normalized_item:
                invalid_json_section_value(
                    section_key=section_key,
                    message=(
                        f"Prepared rolling-summary JSON section '{section_key}' list items must be non-empty strings."
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
            f"Prepared rolling-summary JSON section '{section_key}' must be a non-empty string "
            "or a list of non-empty strings; use [] or 'not_provided' for an empty section."
        ),
        recovery_details=recovery_details,
    )
    raise AssertionError("unreachable")


def normalize_rolling_summary_json_text(
    *,
    raw_text: str,
    recovery_details: dict | None = None,
) -> str:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManagedWritePlannerError(
            message=(
                "Prepared rolling-summary JSON must be a valid JSON object: "
                f"{exc.msg} at line {exc.lineno} column {exc.colno}."
            ),
            details=rolling_summary_json_failure_details(
                recovery_details,
                field_path="$",
                expected_type="valid_json_object",
                reason_code="malformed_json",
                extra={"json_error_line": exc.lineno, "json_error_column": exc.colno},
            ),
        ) from exc

    if not isinstance(payload, dict):
        raise ManagedWritePlannerError(
            message="Prepared rolling-summary JSON must be an object keyed by rolling-summary section names.",
            details=rolling_summary_json_failure_details(
                recovery_details,
                field_path="$",
                expected_type="object",
                reason_code="top_level_not_object",
            ),
        )

    required_keys = list(ROLLING_SUMMARY_JSON_KEYS)
    unknown_key_count = sum(1 for key in payload if key not in required_keys)
    if unknown_key_count:
        details = rolling_summary_json_failure_details(
            recovery_details,
            field_path="$.<section_key>",
            expected_type="allowed_section_key",
            reason_code="unknown_section_key",
            extra={
                "unknown_section_key_count": unknown_key_count,
                "unknown_key_values_public_safe": False,
            },
        )
        raise ManagedWritePlannerError(
            message=(
                "Prepared rolling-summary JSON contains "
                f"{unknown_key_count} unknown section key(s)."
            ),
            details=details,
        )

    missing_keys = [key for key in required_keys if key not in payload]
    if missing_keys:
        details = rolling_summary_json_failure_details(
            recovery_details,
            field_path="$",
            expected_type="object_with_all_required_sections",
            reason_code="missing_section_key",
            extra={"missing_section_keys": missing_keys},
        )
        raise ManagedWritePlannerError(
            message=(
                "Prepared rolling-summary JSON is missing required section keys: "
                + ", ".join(missing_keys)
            ),
            details=details,
        )

    normalized_sections: list[tuple[str, str]] = []
    for section_key in required_keys:
        normalized_sections.append(
            (
                section_key,
                normalize_json_section_value(
                    section_key=section_key,
                    value=payload[section_key],
                    recovery_details=recovery_details,
                ),
            )
        )
    if all(not text.strip() for _, text in normalized_sections):
        raise ManagedWritePlannerError(
            message=(
                "Prepared rolling-summary JSON normalized to an empty current-state payload. "
                "At least one section must contain real content. No RecallLoom files were changed."
            ),
            details=rolling_summary_json_failure_details(
                recovery_details,
                field_path="$",
                expected_type="object_with_at_least_one_non_empty_section",
                reason_code="all_sections_empty",
                extra={"empty_section_count": len(normalized_sections)},
            ),
        )
    sections = [
        section_marker(section_key) + "\n" + normalized
        for section_key, normalized in normalized_sections
    ]
    return "\n\n".join(sections) + "\n"


# --- failure-details assembly (path rendering only, no filesystem access) -----


def rolling_summary_json_recovery_details(
    *,
    input_mode: str,
    source_file: str | None,
    project_root: Path | None,
    route_details: dict | None = None,
) -> dict:
    details = prepared_body_failure_details(
        input_mode=input_mode,
        source_file=source_file,
        project_root=project_root,
    )
    if route_details:
        details.update(route_details)
    return details


def prepared_body_failure_details(
    *,
    input_mode: str,
    source_file: str | None,
    project_root: Path | None,
    file_key: str | None = None,
    write_type: str | None = None,
) -> dict:
    details: dict[str, object] = {"input_mode": input_mode}
    if file_key:
        details["file_key"] = file_key
    if write_type:
        details["write_type"] = write_type
    if source_file:
        details["source_path"] = str(Path(source_file).expanduser().resolve())
    if project_root is not None:
        details["project_root"] = str(project_root)
    return details


def managed_markdown_structural_failure_details(
    recovery_details: dict,
    *,
    reason_code: str,
    extra: dict,
) -> dict:
    route_fields = ("input_mode", "file_key", "write_type")
    structural_fields = (
        "missing_section_keys",
        "duplicate_section_keys",
        "unknown_section_keys",
        "source_file_key",
        "requested_file_key",
    )
    details = {key: recovery_details[key] for key in route_fields if key in recovery_details}
    details["reason_code"] = reason_code
    details["side_effect"] = "none"
    details.update({key: extra[key] for key in structural_fields if key in extra})
    return details


# --- prepared body pipeline (AcquiredInput content boundary) ------------------


def prepare_body_text(
    acquired: AcquiredInput,
    *,
    file_key: str,
    input_format: str,
    expected_language: str,
    source_file: str | None = None,
    project_root: Path | None = None,
    route_details: dict | None = None,
) -> tuple[str, str]:
    """Prepared content -> (candidate body text, effective input mode).

    The content arrives only as the frozen ``AcquiredInput``; ``source_file`` /
    ``project_root`` are failure-details routing parameters, never read.
    """
    source_text = canonicalize_managed_text_newlines(acquired.decoded_text)
    source_kind = acquired.input_mode
    if input_format == "markdown":
        return (
            strip_managed_headers(
                file_key,
                source_text,
                expected_language=expected_language,
            ),
            source_kind,
        )

    if file_key != "rolling_summary":
        input_mode = "json-file" if source_kind == "file" else "json-stdin"
        raise ManagedWritePlannerError(
            message="Structured JSON input is only supported for --file-key rolling_summary.",
            details={
                **prepared_body_failure_details(
                    input_mode=input_mode,
                    source_file=source_file,
                    project_root=project_root,
                    file_key=file_key,
                ),
                **OperationContext(
                    command=COMMAND_WRITE,
                    operation=OPERATION_MANAGED_WRITE,
                    write_type=None,
                    input_mode=None,
                    stage=STAGE_INPUT,
                ).legacy_details_fields(),
                "input_format": "json",
                "reason_code": "json_input_requires_current_state",
                "side_effect": "none",
                "trust_effect": "none",
            },
        )

    input_mode = "json-file" if source_kind == "file" else "json-stdin"
    recovery_details = rolling_summary_json_recovery_details(
        input_mode=input_mode,
        source_file=source_file,
        project_root=project_root,
        route_details=route_details,
    )
    return (
        normalize_rolling_summary_json_text(
            raw_text=source_text,
            recovery_details=recovery_details,
        ),
        input_mode,
    )


def validate_managed_section_structure(
    *,
    body_text: str,
    file_key: str,
    recovery_details: dict,
) -> None:
    missing_keys = missing_section_keys(body_text, SECTION_KEYS[file_key])
    if missing_keys:
        raise ManagedWritePlannerError(
            message=(
                "Refusing to commit because the prepared file is missing required section markers: "
                + ", ".join(missing_keys)
            ),
            details=managed_markdown_structural_failure_details(
                recovery_details,
                reason_code="missing_section_keys",
                extra={"missing_section_keys": missing_keys},
            ),
        )
    duplicate_keys = duplicate_section_keys(body_text)
    if duplicate_keys:
        raise ManagedWritePlannerError(
            message=(
                "Refusing to commit because the prepared file contains duplicate section markers: "
                + ", ".join(duplicate_keys)
            ),
            details=managed_markdown_structural_failure_details(
                recovery_details,
                reason_code="duplicate_section_keys",
                extra={"duplicate_section_keys": duplicate_keys},
            ),
        )
    unknown_keys = unknown_section_keys(
        body_text,
        [*SECTION_KEYS[file_key], *OPTIONAL_SECTION_KEYS.get(file_key, [])],
    )
    if unknown_keys:
        raise ManagedWritePlannerError(
            message=(
                "Refusing to commit because the prepared file contains unknown section markers: "
                + ", ".join(unknown_keys)
            ),
            details=managed_markdown_structural_failure_details(
                recovery_details,
                reason_code="unknown_section_keys",
                extra={"unknown_section_keys": unknown_keys},
            ),
        )


# --- candidate managed text render --------------------------------------------


def build_managed_text(
    *,
    file_key: str,
    body_text: str,
    language: str,
    writer_id: str,
    file_revision: int,
    base_workspace_revision: int,
    timestamp: str,
    today: str,
) -> str:
    parts = [file_marker(file_key, language)]
    if file_key == "rolling_summary":
        parts.append(rolling_summary_header(writer_id, today))
    parts.append(
        file_state_marker(
            revision=file_revision,
            updated_at=timestamp,
            writer_id=writer_id,
            base_workspace_revision=base_workspace_revision,
        )
    )
    body = canonicalize_managed_text_newlines(body_text).rstrip("\n")
    if body:
        parts.extend(["", body])
    return "\n".join(parts) + "\n"


# --- digest / metadata snapshot helpers ---------------------------------------


def sha256_text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def managed_body_digest(text: str) -> str:
    marker = parse_file_marker(text)
    if marker is None:
        body = text.rstrip("\n") + "\n"
    else:
        body = (
            strip_managed_headers(
                marker.file_key,
                text,
                expected_language=marker.language,
            ).rstrip("\n")
            + "\n"
        )
    return sha256_text_digest(body)


def managed_metadata_snapshot(text: str) -> dict | None:
    marker = parse_file_marker(text)
    state = parse_file_state_marker(text)
    if marker is None or state is None:
        return None
    snapshot = {
        "file_marker": {
            "file_key": marker.file_key,
            "version": marker.version,
            "language": marker.language,
        },
        "file_state": {
            "revision": state.revision,
            "updated_at": state.updated_at,
            "writer_id": state.writer_id,
            "base_workspace_revision": state.base_workspace_revision,
        },
    }
    if marker.file_key == "rolling_summary":
        lines = text.splitlines()
        if len(lines) < 2:
            return None
        last_writer_match = LAST_WRITER_RE.match(lines[1].strip())
        if last_writer_match is None:
            return None
        snapshot["rolling_summary_last_writer"] = {
            "tool": last_writer_match.group("tool").strip(),
            "date": last_writer_match.group("date"),
        }
    return snapshot


def controlled_metadata_refresh_claim(
    *,
    before_text: str,
    after_text: str,
    body_digest_before: str,
) -> dict | None:
    before = managed_metadata_snapshot(before_text)
    after = managed_metadata_snapshot(after_text)
    if before is None or after is None:
        return None
    return {
        "refresh_kind": "metadata_only",
        "allowed_changed_fields": [
            "rolling_summary_last_writer.tool",
            "rolling_summary_last_writer.date",
            "file_state.revision",
            "file_state.updated_at",
            "file_state.writer_id",
            "file_state.base_workspace_revision",
        ],
        "body_digest_before": body_digest_before,
        "body_digest_after": managed_body_digest(after_text),
        "before": before,
        "after": after,
    }


# --- state delta + receipt seed ------------------------------------------------


def expected_state_json_text(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"


def apply_managed_write_state_delta(
    state: dict,
    *,
    file_key: str,
    new_file_revision: int,
    new_workspace_revision: int,
    writer_id: str,
    timestamp: str,
    record_provenance: bool,
    previous_state_label: str | None,
) -> dict:
    """Apply the managed-write state delta in place and return ``state``."""
    state["workspace_revision"] = new_workspace_revision
    state["files"][file_key] = {
        "file_revision": new_file_revision,
        "updated_at": timestamp,
        "writer_id": writer_id,
        "base_workspace_revision": new_workspace_revision,
    }
    if file_key == "update_protocol":
        state["update_protocol_revision"] = new_file_revision
    if record_provenance:
        state["provenance"] = helper_evidenced_metadata(
            timestamp=timestamp,
            previous_state_label=(
                previous_state_label if isinstance(previous_state_label, str) else None
            ),
        )
    return state


def build_receipt_seed(
    *,
    file_key: str,
    expected_file_revision: int,
    expected_workspace_revision: int,
    helper_version: str,
    preflight_binding: dict,
    timestamp: str,
    target_digest: str,
    state_digest: str,
    new_file_revision: int,
    new_workspace_revision: int,
    controlled_metadata_refresh: dict | None = None,
) -> dict:
    operation_class = str(preflight_binding["operation_class"])
    operation = (
        "post_append_summary_sync"
        if operation_class == "post_append_summary_sync"
        else str(preflight_binding.get("write_type") or WRITE_TYPE_BY_FILE_KEY.get(file_key))
    )
    optional_binding_fields = {
        key: preflight_binding[key]
        for key in (
            "assertion_source_kind",
            "assertion_source_id",
            "assertion_payload_digest",
            "input_digest",
            "managed_body_digest_before",
            "assertion_binding_seed_digest",
        )
        if key in preflight_binding
    }
    if (
        operation_class == "post_append_summary_sync"
        and "managed_body_digest_before" in preflight_binding
    ):
        optional_binding_fields["preflight_binding_digest"] = preflight_binding[
            "preflight_contract_hash"
        ]
    if controlled_metadata_refresh is not None:
        optional_binding_fields["controlled_metadata_refresh"] = controlled_metadata_refresh
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_type": "helper_write",
        "helper_name": "commit_context_file.py",
        "helper_version": helper_version,
        "operation": operation,
        "operation_class": operation_class,
        "side_effect": "target_and_state_written",
        "result": "ok",
        "state_label_before": preflight_binding.get("provenance_state") or "structurally_valid",
        "state_label_after": "helper_evidenced",
        "target_file_key": file_key,
        "target_digest": target_digest,
        "state_digest": state_digest,
        "preflight_contract_identity": preflight_binding["preflight_contract_identity"],
        "expected_workspace_revision": expected_workspace_revision,
        "result_workspace_revision": new_workspace_revision,
        "expected_file_revision": expected_file_revision,
        "result_file_revision": new_file_revision,
        "created_at": timestamp,
        **optional_binding_fields,
    }


# --- §7.4 frozen operation preplan/plan contracts (T050-04A) -------------------
#
# ``preplan_managed_write`` computes the candidate render, the new
# revision/cursor pair, and every input the read-only receipt precheck needs,
# in exactly the validation order the legacy helper apply path uses (revision
# and marker checks before content validation). ``seal_managed_write_plan``
# runs after the transaction's receipt precheck and only performs final
# validation/seal: it never re-reads, never re-canonicalizes, and never
# changes the candidate bytes the preplan produced (the candidate is computed
# exactly once). Both functions are pure: no IO, no locks, no argparse, no
# wall-clock (``timestamp`` / ``today`` are explicit parameters), and every
# validation rejection raises ``ManagedWritePlannerError`` with the exact
# legacy exit projection.


@dataclass(frozen=True)
class OperationPreplan:
    """Frozen §7.4 preplan contract: candidate + revisions + precheck inputs."""

    operation: str
    target_path: Path
    target_identity: str | None
    state_path: Path
    expected_file_revision: int
    new_file_revision: int
    expected_workspace_revision: int
    new_workspace_revision: int
    expected_cursor: object | None
    new_cursor: object | None
    candidate_target_bytes: bytes
    receipt_precheck_inputs: dict


@dataclass(frozen=True)
class OperationPlan:
    """Frozen §7.4 plan contract: sealed previous/new bytes + receipt seed inputs."""

    operation: str
    target_path: Path
    previous_target_bytes: bytes
    new_target_bytes: bytes
    target_identity: str | None
    state_path: Path
    previous_state_bytes: bytes
    new_state_bytes: bytes
    expected_file_revision: int
    new_file_revision: int
    expected_workspace_revision: int
    new_workspace_revision: int
    expected_cursor: object | None
    new_cursor: object | None
    receipt_seed_inputs: dict


def _preflight_binding_reject(
    *,
    message: str,
    reason_code: str,
    field_path: str = "$",
    extra: dict | None = None,
) -> None:
    """Raise the exact ``preflight_binding_failure`` legacy projection."""
    raise ManagedWritePlannerError(
        message=message,
        reason="invalid_prepared_input",
        exit_code=2,
        details={
            "reason_code": reason_code,
            "field_path": field_path,
            "side_effect": "none",
            **(extra or {}),
        },
    )


def _stale_write_context_reject(
    *,
    language: str,
    message: str,
    details: dict,
) -> None:
    """Raise the exact ``stale_write_context`` payload exit (exit code 3)."""
    raise ManagedWritePlannerError(
        message=message,
        exit_code=3,
        payload=failure_payload(
            "stale_write_context",
            language=language,
            error=message,
            details=details,
        ),
    )


def preplan_managed_write(
    acquired: AcquiredInput,
    *,
    operation: str,
    file_key: str,
    input_format: str,
    expected_language: str,
    target_path: Path,
    state_path: Path,
    current_target_text: str,
    current_target_identity: str | None,
    state: dict,
    expected_file_revision: int,
    expected_workspace_revision: int,
    writer_id: str,
    timestamp: str,
    today: str,
    preflight_binding: dict | None,
    source_file: str | None = None,
    project_root: Path | None = None,
) -> OperationPreplan:
    """Pure managed-write preplan (frozen §7.4).

    Reproduces the legacy in-lock validation order exactly: workspace revision,
    metadata-only body digest, target marker/file-key/language/file-state
    checks, file revision, source marker, prepared-body pipeline, safety scan,
    section structure, then the single candidate render. ``state`` is never
    mutated and nothing is read or written; every rejection is the typed
    ``ManagedWritePlannerError`` with the byte-identical legacy projection.
    """
    if state["workspace_revision"] != expected_workspace_revision:
        message = (
            f"Workspace revision changed from {expected_workspace_revision} to "
            f"{state['workspace_revision']}. Rerun preflight before writing."
        )
        _stale_write_context_reject(
            language=expected_language,
            message=message,
            details={
                "expected_workspace_revision": expected_workspace_revision,
                "current_workspace_revision": state["workspace_revision"],
            },
        )

    expected_body_digest = (
        preflight_binding.get("managed_body_digest_before")
        if isinstance(preflight_binding, dict)
        else None
    )
    if isinstance(expected_body_digest, str):
        current_body_digest = managed_body_digest(current_target_text)
        if current_body_digest != expected_body_digest:
            _preflight_binding_reject(
                message="Current managed body digest no longer matches the preflight-bound metadata-only refresh assertion.",
                reason_code="managed_body_digest_before_mismatch",
                field_path="$.managed_body_digest_before",
                extra={
                    "expected_body_digest": expected_body_digest,
                    "current_body_digest": current_body_digest,
                },
            )

    current_marker = parse_file_marker(current_target_text)
    if current_marker is None:
        raise ManagedWritePlannerError(
            message=f"Target file is missing a valid file marker: {target_path}",
            reason="malformed_managed_file",
            exit_code=2,
            details={"path": str(target_path)},
        )
    if current_marker.file_key != file_key:
        raise ManagedWritePlannerError(
            message=(
                f"Target file marker '{current_marker.file_key}' does not match requested file key "
                f"'{file_key}'. Repair the target file before committing."
            ),
            reason="malformed_managed_file",
            exit_code=2,
            details={"path": str(target_path)},
        )
    if current_marker.language != expected_language:
        raise ManagedWritePlannerError(
            message=(
                f"Target file language marker '{current_marker.language}' does not match workspace_language "
                f"'{expected_language}'. Repair the target file before committing."
            ),
            reason="malformed_managed_file",
            exit_code=2,
            details={"path": str(target_path)},
        )
    current_state = parse_file_state_marker(current_target_text)
    if current_state is None:
        raise ManagedWritePlannerError(
            message=f"Target file is missing a valid file-state marker: {target_path}",
            reason="malformed_managed_file",
            exit_code=2,
            details={"path": str(target_path)},
        )
    if current_state.revision != expected_file_revision:
        message = (
            f"File revision changed from {expected_file_revision} to "
            f"{current_state.revision}. Reread the file before writing."
        )
        _stale_write_context_reject(
            language=expected_language,
            message=message,
            details={
                "expected_file_revision": expected_file_revision,
                "current_file_revision": current_state.revision,
            },
        )

    raw_recovery_details = prepared_body_failure_details(
        input_mode=acquired.input_mode,
        source_file=source_file,
        project_root=project_root,
        file_key=file_key,
        write_type=WRITE_TYPE_BY_FILE_KEY.get(file_key),
    )
    source_marker = parse_file_marker(acquired.decoded_text)
    if source_marker is not None and source_marker.file_key != file_key:
        raise ManagedWritePlannerError(
            message=(
                f"Source file marker '{source_marker.file_key}' does not match requested file key '{file_key}'."
            ),
            reason="invalid_prepared_input",
            exit_code=2,
            details=managed_markdown_structural_failure_details(
                raw_recovery_details,
                reason_code="source_file_key_mismatch",
                extra={
                    "source_file_key": source_marker.file_key,
                    "requested_file_key": file_key,
                },
            ),
        )

    new_file_revision = current_state.revision + 1
    new_workspace_revision = state["workspace_revision"] + 1
    route_details = None
    if operation == OPERATION_POST_APPEND_SUMMARY_SYNC:
        route_details = OperationContext(
            command=COMMAND_SYNC_CURRENT_STATE_AFTER_APPEND,
            operation=OPERATION_POST_APPEND_SUMMARY_SYNC,
            write_type=None,
            input_mode=None,
            stage=STAGE_INPUT,
        ).legacy_details_fields()
    body_text, effective_input_mode = prepare_body_text(
        acquired,
        file_key=file_key,
        input_format=input_format,
        expected_language=expected_language,
        source_file=source_file,
        project_root=project_root,
        route_details=route_details,
    )
    recovery_details = prepared_body_failure_details(
        input_mode=effective_input_mode,
        source_file=source_file,
        project_root=project_root,
        file_key=file_key,
        write_type=WRITE_TYPE_BY_FILE_KEY.get(file_key),
    )
    if isinstance(expected_body_digest, str):
        prepared_body_digest = sha256_text_digest(body_text.rstrip("\n") + "\n")
        if prepared_body_digest != expected_body_digest:
            _preflight_binding_reject(
                message="Prepared metadata-only refresh body does not match the preflight-bound managed body digest.",
                reason_code="metadata_only_prepared_body_changed",
                field_path="$.managed_body_digest_before",
                extra={
                    "expected_body_digest": expected_body_digest,
                    "prepared_body_digest": prepared_body_digest,
                },
            )
    validate_prepared_body(
        body_text=body_text,
        recovery_details=recovery_details,
        include_reserved_marker_route=input_format == "markdown",
    )
    validate_managed_section_structure(
        body_text=body_text,
        file_key=file_key,
        recovery_details=recovery_details,
    )
    new_text = build_managed_text(
        file_key=file_key,
        body_text=body_text,
        language=expected_language,
        writer_id=writer_id,
        file_revision=new_file_revision,
        base_workspace_revision=new_workspace_revision,
        timestamp=timestamp,
        today=today,
    )
    return OperationPreplan(
        operation=operation,
        target_path=target_path,
        target_identity=current_target_identity,
        state_path=state_path,
        expected_file_revision=expected_file_revision,
        new_file_revision=new_file_revision,
        expected_workspace_revision=expected_workspace_revision,
        new_workspace_revision=new_workspace_revision,
        expected_cursor=None,
        new_cursor=None,
        candidate_target_bytes=new_text.encode("utf-8"),
        receipt_precheck_inputs={
            "file_key": file_key,
            "new_file_revision": new_file_revision,
            "new_workspace_revision": new_workspace_revision,
            "preflight_binding_present": preflight_binding is not None,
        },
    )


def seal_managed_write_plan(
    preplan: OperationPreplan,
    *,
    current_target_text: str,
    current_state_text: str,
    state: dict,
    writer_id: str,
    timestamp: str,
    previous_state_label: str,
    preflight_binding: dict | None,
    helper_version: str,
) -> OperationPlan:
    """Pure managed-write plan seal (frozen §7.4).

    Runs only after the transaction's read-only receipt precheck: the
    metadata-only refresh claim validation and the final previous/new byte
    assembly. The candidate bytes from ``preplan`` are sealed verbatim (never
    re-read, never re-canonicalized, never recomputed).
    """
    new_text = preplan.candidate_target_bytes.decode("utf-8")
    controlled_metadata_refresh = None
    expected_body_digest = (
        preflight_binding.get("managed_body_digest_before")
        if isinstance(preflight_binding, dict)
        else None
    )
    if isinstance(expected_body_digest, str):
        controlled_metadata_refresh = controlled_metadata_refresh_claim(
            before_text=current_target_text,
            after_text=new_text,
            body_digest_before=expected_body_digest,
        )
        if controlled_metadata_refresh is None:
            _preflight_binding_reject(
                message="Metadata-only refresh could not build a controlled metadata before/after receipt claim.",
                reason_code="controlled_metadata_refresh_snapshot_invalid",
                field_path="$.managed_body_digest_before",
            )
        if controlled_metadata_refresh["body_digest_after"] != expected_body_digest:
            _preflight_binding_reject(
                message="Metadata-only refresh would change the managed body after rebuilding controlled metadata.",
                reason_code="metadata_only_rebuilt_body_changed",
                field_path="$.managed_body_digest_before",
                extra={
                    "expected_body_digest": expected_body_digest,
                    "body_digest_after": controlled_metadata_refresh["body_digest_after"],
                },
            )
    new_state = apply_managed_write_state_delta(
        copy.deepcopy(state),
        file_key=str(preplan.receipt_precheck_inputs["file_key"]),
        new_file_revision=preplan.new_file_revision,
        new_workspace_revision=preplan.new_workspace_revision,
        writer_id=writer_id,
        timestamp=timestamp,
        record_provenance=preflight_binding is not None,
        previous_state_label=previous_state_label,
    )
    new_state_text = expected_state_json_text(new_state)
    return OperationPlan(
        operation=preplan.operation,
        target_path=preplan.target_path,
        previous_target_bytes=current_target_text.encode("utf-8"),
        new_target_bytes=preplan.candidate_target_bytes,
        target_identity=preplan.target_identity,
        state_path=preplan.state_path,
        previous_state_bytes=current_state_text.encode("utf-8"),
        new_state_bytes=new_state_text.encode("utf-8"),
        expected_file_revision=preplan.expected_file_revision,
        new_file_revision=preplan.new_file_revision,
        expected_workspace_revision=preplan.expected_workspace_revision,
        new_workspace_revision=preplan.new_workspace_revision,
        expected_cursor=preplan.expected_cursor,
        new_cursor=preplan.new_cursor,
        receipt_seed_inputs={
            "file_key": str(preplan.receipt_precheck_inputs["file_key"]),
            "expected_file_revision": preplan.expected_file_revision,
            "expected_workspace_revision": preplan.expected_workspace_revision,
            "helper_version": helper_version,
            "preflight_binding": preflight_binding,
            "timestamp": timestamp,
            "new_file_revision": preplan.new_file_revision,
            "new_workspace_revision": preplan.new_workspace_revision,
            "controlled_metadata_refresh": controlled_metadata_refresh,
        },
    )


# --- §7.4 concrete post-append summary-sync planners (T050-05) -------------------
#
# The sync lane's named planner pair. Both reuse the single managed-write
# implementation above with the operation pinned to
# ``OPERATION_POST_APPEND_SUMMARY_SYNC``: the v0.4.8.3 sync semantics (the
# metadata-only reuse-current-summary assertion family driven by the binding's
# ``managed_body_digest_before``, and the regular current-state sync) are
# exactly the managed render/validate/delta semantics with sync route details
# and the sync receipt identity. There is no second commit implementation and
# no generic callback protocol — the transaction names these concrete
# functions for this operation.


def preplan_post_append_sync(
    acquired: AcquiredInput,
    *,
    file_key: str,
    input_format: str,
    expected_language: str,
    target_path: Path,
    state_path: Path,
    current_target_text: str,
    current_target_identity: str | None,
    state: dict,
    expected_file_revision: int,
    expected_workspace_revision: int,
    writer_id: str,
    timestamp: str,
    today: str,
    preflight_binding: dict | None,
    source_file: str | None = None,
    project_root: Path | None = None,
) -> OperationPreplan:
    """Pure post-append summary-sync preplan (frozen §7.4).

    Same purity and validation-order contract as ``preplan_managed_write``;
    the operation is intrinsic (``post_append_summary_sync``) so the sync
    route details and receipt-precheck inputs are pinned by construction.
    """
    return preplan_managed_write(
        acquired,
        operation=OPERATION_POST_APPEND_SUMMARY_SYNC,
        file_key=file_key,
        input_format=input_format,
        expected_language=expected_language,
        target_path=target_path,
        state_path=state_path,
        current_target_text=current_target_text,
        current_target_identity=current_target_identity,
        state=state,
        expected_file_revision=expected_file_revision,
        expected_workspace_revision=expected_workspace_revision,
        writer_id=writer_id,
        timestamp=timestamp,
        today=today,
        preflight_binding=preflight_binding,
        source_file=source_file,
        project_root=project_root,
    )


def seal_post_append_sync_plan(
    preplan: OperationPreplan,
    *,
    current_target_text: str,
    current_state_text: str,
    state: dict,
    writer_id: str,
    timestamp: str,
    previous_state_label: str,
    preflight_binding: dict | None,
    helper_version: str,
) -> OperationPlan:
    """Pure post-append summary-sync plan seal (frozen §7.4).

    Runs only after the transaction's read-only receipt precheck and seals
    the preplan's candidate bytes verbatim through the shared managed-write
    seal — including the metadata-only refresh claim validation (the
    ``managed_body_digest_before`` refusal family). Never re-reads, never
    re-canonicalizes, never recomputes the candidate.
    """
    if preplan.operation != OPERATION_POST_APPEND_SUMMARY_SYNC:
        raise ValueError(
            "seal_post_append_sync_plan requires a post_append_summary_sync preplan"
        )
    return seal_managed_write_plan(
        preplan,
        current_target_text=current_target_text,
        current_state_text=current_state_text,
        state=state,
        writer_id=writer_id,
        timestamp=timestamp,
        previous_state_label=previous_state_label,
        preflight_binding=preflight_binding,
        helper_version=helper_version,
    )
