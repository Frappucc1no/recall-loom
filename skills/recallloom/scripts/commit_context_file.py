#!/usr/bin/env python3
"""Safely commit a prepared RecallLoom managed file with revision checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from core.failure.contracts import failure_payload, preferred_failure_language
from core.protocol.contracts import (
    CURRENT_PROTOCOL_VERSION,
    FILE_KEYS,
    LAST_WRITER_RE,
    OPTIONAL_SECTION_KEYS,
    SECTION_KEYS,
)
from core.protocol.markers import (
    file_marker,
    file_state_marker,
    parse_file_marker,
    parse_file_state_marker,
    rolling_summary_header,
    section_marker,
)
from core.protocol.sections import (
    duplicate_section_keys,
    missing_section_keys,
    unknown_section_keys,
)
from core.safety.attached_text import scan_auto_attached_context_text
from core.safety.prepared_input import (
    PreparedInputSafetyError,
    PreparedInputSource,
    read_prepared_input_source_text,
    validate_prepared_input_source_path,
)

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    LockBusyError,
    StorageResolutionError,
    atomic_write_if_unchanged,
    dump_json,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue_for_sources,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    load_workspace_state,
    normalize_wrapper_metadata_json,
    now_iso_timestamp,
    public_json_payload,
    read_text,
    resolve_writer_attribution,
    restore_text_snapshot,
    today_iso,
    validate_tool_name,
    validate_writer_id,
    WrapperMetadataSecurityError,
    workspace_write_lock,
)


WRITABLE_FILE_KEYS = {"context_brief", "rolling_summary", "update_protocol"}
WRITE_TYPE_BY_FILE_KEY = {
    "context_brief": "stable-context",
    "rolling_summary": "current-state",
    "update_protocol": "protocol-rules",
}
DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024
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


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--max-input-bytes must be an integer.") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--max-input-bytes must be greater than zero.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely commit a prepared RecallLoom managed file with revision checks."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument("--file-key", required=True, choices=sorted(WRITABLE_FILE_KEYS))
    parser.add_argument("--source-file", help="Path to prepared markdown content.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared markdown content from UTF-8 stdin instead of a file.",
    )
    parser.add_argument(
        "--input-format",
        choices=("markdown", "json"),
        default="markdown",
        help=(
            "Interpret prepared content as markdown or rolling-summary JSON. "
            "JSON input is only supported for --file-key rolling_summary."
        ),
    )
    parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        default=DEFAULT_MAX_INPUT_BYTES,
        help="Maximum prepared-content input size in bytes. Defaults to 4 MiB.",
    )
    parser.add_argument("--expected-file-revision", type=int, required=True)
    parser.add_argument("--expected-workspace-revision", type=int, required=True)
    parser.add_argument("--writer-id")
    parser.add_argument(
        "--wrapper-metadata-json",
        help=(
            "Optional wrapper metadata JSON object for additive public output. "
            "Only public-safe host/surface keys and version-like local_wrapper_version values are accepted."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def prepared_input_failure_details(
    error: PreparedInputSafetyError,
    *,
    input_mode: str | None = None,
) -> dict[str, object]:
    details = error.details
    if input_mode is not None:
        details["input_mode"] = input_mode
    return details


def exit_prepared_input_safety_error(
    parser,
    *,
    json_mode: bool,
    error: PreparedInputSafetyError,
    input_mode: str | None = None,
) -> None:
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=error.message,
        reason="invalid_prepared_input",
        details=prepared_input_failure_details(error, input_mode=input_mode),
    )


def read_limited_file_text(
    parser,
    *,
    json_mode: bool,
    source: PreparedInputSource,
    max_input_bytes: int,
) -> str:
    try:
        return read_prepared_input_source_text(
            source,
            max_input_bytes=max_input_bytes,
            label="source",
        )
    except PreparedInputSafetyError as exc:
        exit_prepared_input_safety_error(parser, json_mode=json_mode, error=exc)
    raise AssertionError("unreachable")


def read_limited_stdin(parser, *, json_mode: bool, max_input_bytes: int) -> bytes:
    try:
        raw = sys.stdin.buffer.read(max_input_bytes + 1)
    except OSError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=f"Failed to read stdin: {exc}",
            reason="invalid_prepared_input",
        )
    if len(raw) > max_input_bytes:
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=f"Stdin input exceeds --max-input-bytes ({len(raw)} > {max_input_bytes}).",
            reason="invalid_prepared_input",
            details={"size": len(raw), "max_input_bytes": max_input_bytes},
        )
    return raw


def load_prepared_text(
    parser,
    *,
    json_mode: bool,
    source_file: str | None,
    use_stdin: bool,
    max_input_bytes: int,
    project_root: Path | None = None,
    storage_root: Path | None = None,
) -> tuple[str, str]:
    if bool(source_file) == bool(use_stdin):
        if source_file and use_stdin:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="Use exactly one prepared-content input: --source-file or --stdin.",
                reason="invalid_prepared_input",
            )
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Provide prepared content with --source-file or --stdin.",
            reason="invalid_prepared_input",
        )

    if source_file:
        if project_root is None or storage_root is None:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="Internal error: project root is required before reading --source-file.",
                reason="invalid_prepared_input",
                details={"input_mode": "file", "reason_code": "prepared_input_context_missing"},
            )
        try:
            source = validate_prepared_input_source_path(
                source_file,
                project_root=project_root,
                storage_root=storage_root,
                input_role="source-file",
                label="source",
            )
        except PreparedInputSafetyError as exc:
            exit_prepared_input_safety_error(
                parser,
                json_mode=json_mode,
                error=exc,
                input_mode="file",
            )
        return (
            read_limited_file_text(
                parser,
                json_mode=json_mode,
                source=source,
                max_input_bytes=max_input_bytes,
            ),
            "file",
        )

    if sys.stdin.isatty():
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Stdin input is empty. Pipe or redirect UTF-8 prepared content when using --stdin.",
            reason="invalid_prepared_input",
        )
    raw = read_limited_stdin(parser, json_mode=json_mode, max_input_bytes=max_input_bytes)
    if raw == b"":
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Stdin input is empty. Pipe or redirect UTF-8 prepared content when using --stdin.",
            reason="invalid_prepared_input",
        )
    try:
        return raw.decode("utf-8"), "stdin"
    except UnicodeDecodeError:
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Stdin input must be valid UTF-8.",
            reason="invalid_prepared_input",
        )
    raise AssertionError("unreachable")


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
    parser,
    *,
    json_mode: bool,
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
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=(
            f"Refusing to commit because the {prepared_label} contains a reserved RecallLoom marker "
            f"on line {line_number}."
        ),
        reason="invalid_prepared_input",
        details=reserved_marker_failure_details(
            recovery_details,
            line_number=line_number,
            marker_family=str(hit["marker_family"]),
            include_route=include_reserved_marker_route,
        ),
    )


def validate_prepared_body(
    parser,
    *,
    json_mode: bool,
    body_text: str,
    recovery_details: dict | None = None,
    include_reserved_marker_route: bool = False,
) -> None:
    reject_reserved_markers(
        parser,
        json_mode=json_mode,
        text=body_text,
        recovery_details=recovery_details,
        include_reserved_marker_route=include_reserved_marker_route,
    )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Refusing to commit because the prepared body failed the attached-text safety scan: "
                + ", ".join(attach_scan["hard_block_reasons"])
            ),
            payload=failure_payload(
                "attach_scan_blocked",
                language=preferred_failure_language(os.environ),
                error=(
                    "Refusing to commit because the prepared body failed the attached-text safety scan: "
                    + ", ".join(attach_scan["hard_block_reasons"])
                ),
                details={"hard_block_reasons": attach_scan["hard_block_reasons"]},
            ),
        )


def rolling_summary_json_failure_details(
    recovery_details: dict | None = None,
    *,
    field_path: str = "$",
    expected_type: str = "rolling_summary_json_object",
    reason_code: str,
    section_key: str | None = None,
    extra: dict | None = None,
) -> dict:
    details = {
        **(recovery_details or {}),
        "prepared_input_builder": "rolling_summary_json",
        "file_key": "rolling_summary",
        "write_type": "current-state",
        "field_path": field_path,
        "expected_type": expected_type,
        "accepted_shapes": list(ROLLING_SUMMARY_JSON_ACCEPTED_SHAPES),
        "retry_payload_shape": dict(ROLLING_SUMMARY_JSON_RETRY_PAYLOAD_SHAPE),
        "allowed_section_keys": list(ROLLING_SUMMARY_JSON_KEYS),
        "reason_code": reason_code,
        "side_effect": "none",
    }
    if section_key is not None:
        details["section_key"] = section_key
    if extra:
        details.update(extra)
    return details


def invalid_json_section_value(
    parser,
    *,
    json_mode: bool,
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
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=message,
        reason="invalid_prepared_input",
        details=details,
    )


def render_json_list_item(text: str) -> str:
    lines = text.splitlines()
    rendered = [f"- {lines[0]}"]
    rendered.extend(f"  {line}" if line else "  " for line in lines[1:])
    return "\n".join(rendered)


def reject_json_reserved_markers(
    parser,
    *,
    json_mode: bool,
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
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=(
            "Refusing to commit because prepared rolling-summary JSON section "
            f"'{section_key}' contains a reserved RecallLoom marker on line {line_number}."
        ),
        reason="invalid_prepared_input",
        details=details,
    )


def normalize_json_section_value(
    parser,
    *,
    json_mode: bool,
    section_key: str,
    value: object,
    recovery_details: dict | None = None,
) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.casefold() in NOT_PROVIDED_SENTINELS:
            return ""
        if normalized:
            reject_json_reserved_markers(
                parser,
                json_mode=json_mode,
                section_key=section_key,
                text=normalized,
                recovery_details=recovery_details,
            )
            return normalized
        invalid_json_section_value(
            parser,
            json_mode=json_mode,
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
                    parser,
                    json_mode=json_mode,
                    section_key=section_key,
                    message=(
                        f"Prepared rolling-summary JSON section '{section_key}' list items must be non-empty strings."
                    ),
                    recovery_details=recovery_details,
                    field_path=f"$.{section_key}[]",
                    expected_type="non_empty_string",
                    reason_code="invalid_section_list_item_type",
                )
            normalized_item = item.strip()
            if normalized_item.casefold() in NOT_PROVIDED_SENTINELS:
                continue
            if not normalized_item:
                invalid_json_section_value(
                    parser,
                    json_mode=json_mode,
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
                parser,
                json_mode=json_mode,
                section_key=section_key,
                text=normalized_item,
                recovery_details=recovery_details,
            )
            rendered_items.append(render_json_list_item(normalized_item))
        return "\n".join(rendered_items)

    invalid_json_section_value(
        parser,
        json_mode=json_mode,
        section_key=section_key,
        message=(
            f"Prepared rolling-summary JSON section '{section_key}' must be a non-empty string "
            "or a list of non-empty strings; use [] or 'not_provided' for an empty section."
        ),
        recovery_details=recovery_details,
    )
    raise AssertionError("unreachable")


def normalize_rolling_summary_json_text(
    parser,
    *,
    json_mode: bool,
    raw_text: str,
    recovery_details: dict | None = None,
) -> str:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Prepared rolling-summary JSON must be a valid JSON object: "
                f"{exc.msg} at line {exc.lineno} column {exc.colno}."
            ),
            reason="invalid_prepared_input",
            details=rolling_summary_json_failure_details(
                recovery_details,
                field_path="$",
                expected_type="valid_json_object",
                reason_code="malformed_json",
                extra={"json_error_line": exc.lineno, "json_error_column": exc.colno},
            ),
        )

    if not isinstance(payload, dict):
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Prepared rolling-summary JSON must be an object keyed by rolling-summary section names.",
            reason="invalid_prepared_input",
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
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Prepared rolling-summary JSON contains "
                f"{unknown_key_count} unknown section key(s)."
            ),
            reason="invalid_prepared_input",
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
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Prepared rolling-summary JSON is missing required section keys: "
                + ", ".join(missing_keys)
            ),
            reason="invalid_prepared_input",
            details=details,
        )

    normalized_sections: list[tuple[str, str]] = []
    for section_key in required_keys:
        normalized_sections.append(
            (
                section_key,
                normalize_json_section_value(
                    parser,
                    json_mode=json_mode,
                    section_key=section_key,
                    value=payload[section_key],
                    recovery_details=recovery_details,
                ),
            )
        )
    if all(not text.strip() for _, text in normalized_sections):
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Prepared rolling-summary JSON normalized to an empty current-state payload. "
                "At least one section must contain real content. No RecallLoom files were changed."
            ),
            reason="invalid_prepared_input",
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


def rolling_summary_json_recovery_details(
    *,
    input_mode: str,
    source_file: str | None,
    project_root: Path | None,
) -> dict:
    return prepared_body_failure_details(
        input_mode=input_mode,
        source_file=source_file,
        project_root=project_root,
    )


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


def prepare_body_text(
    parser,
    *,
    json_mode: bool,
    file_key: str,
    source_text: str,
    source_kind: str,
    input_format: str,
    expected_language: str,
    source_file: str | None = None,
    project_root: Path | None = None,
) -> tuple[str, str]:
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
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Structured JSON input is only supported for --file-key rolling_summary.",
            reason="invalid_prepared_input",
            details={"input_format": "json", "file_key": file_key},
        )

    input_mode = "json-file" if source_kind == "file" else "json-stdin"
    recovery_details = rolling_summary_json_recovery_details(
        input_mode=input_mode,
        source_file=source_file,
        project_root=project_root,
    )
    return (
        normalize_rolling_summary_json_text(
            parser,
            json_mode=json_mode,
            raw_text=source_text,
            recovery_details=recovery_details,
        ),
        input_mode,
    )


def build_managed_text(
    *,
    file_key: str,
    body_text: str,
    language: str,
    writer_id: str,
    file_revision: int,
    base_workspace_revision: int,
    timestamp: str,
) -> str:
    parts = [file_marker(file_key, language)]
    if file_key == "rolling_summary":
        parts.append(rolling_summary_header(writer_id, today_iso()))
    parts.append(
        file_state_marker(
            revision=file_revision,
            updated_at=timestamp,
            writer_id=writer_id,
            base_workspace_revision=base_workspace_revision,
        )
    )
    body = body_text.rstrip("\n")
    if body:
        parts.extend(["", body])
    return "\n".join(parts) + "\n"


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload("python_runtime_unavailable", error=str(exc)),
        )
    enforce_package_support_gate(parser, json_mode=args.json)

    try:
        wrapper_metadata = normalize_wrapper_metadata_json(args.wrapper_metadata_json)
    except WrapperMetadataSecurityError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=4,
            message=str(exc),
            reason="privacy_security_failure",
            details=exc.details,
        )

    try:
        workspace = find_recallloom_root(args.path)
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )
    if workspace is None:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            reason="no_project_root",
        )
    startup_residue_report = exit_if_startup_scratch_residue_for_sources(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        source_paths=[args.source_file],
    )

    source_text, input_mode = load_prepared_text(
        parser,
        json_mode=args.json,
        source_file=args.source_file,
        use_stdin=args.stdin,
        max_input_bytes=args.max_input_bytes,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )

    target_path = workspace.storage_root / FILE_KEYS[args.file_key]
    if not target_path.is_file():
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Missing target file: {target_path}",
            reason="malformed_managed_file",
            details={"path": str(target_path)},
        )

    try:
        with workspace_write_lock(workspace.project_root, "commit_context_file.py"):
            try:
                attribution = resolve_writer_attribution(
                    explicit_writer_id=args.writer_id,
                    invocation_surface="commit_context_file.py",
                    explicit_marker_role=(
                        "tool_name" if args.file_key == "rolling_summary" else "writer_id"
                    ),
                    wrapper_metadata=wrapper_metadata,
                )
                writer_id = attribution.writer_id
            except ConfigContractError as exc:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=str(exc),
                    reason="invalid_tool_name",
                    details={"writer_id_source": "explicit_cli"},
                )

            state_path = workspace.storage_root / FILE_KEYS["state"]
            state = load_workspace_state(state_path)
            if state["workspace_revision"] != args.expected_workspace_revision:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=3,
                    message=(
                        f"Workspace revision changed from {args.expected_workspace_revision} to "
                        f"{state['workspace_revision']}. Rerun preflight before writing."
                    ),
                    payload=failure_payload(
                        "stale_write_context",
                        language=workspace.workspace_language,
                        error=(
                            f"Workspace revision changed from {args.expected_workspace_revision} to "
                            f"{state['workspace_revision']}. Rerun preflight before writing."
                        ),
                        details={
                            "expected_workspace_revision": args.expected_workspace_revision,
                            "current_workspace_revision": state["workspace_revision"],
                        },
                    ),
                )

            current_text = read_text(target_path)
            current_marker = parse_file_marker(current_text)
            if current_marker is None:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Target file is missing a valid file marker: {target_path}",
                    reason="malformed_managed_file",
                    details={"path": str(target_path)},
                )
            if current_marker.file_key != args.file_key:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        f"Target file marker '{current_marker.file_key}' does not match requested file key "
                        f"'{args.file_key}'. Repair the target file before committing."
                    ),
                    reason="malformed_managed_file",
                    details={"path": str(target_path)},
                )
            if current_marker.language != workspace.workspace_language:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        f"Target file language marker '{current_marker.language}' does not match workspace_language "
                        f"'{workspace.workspace_language}'. Repair the target file before committing."
                    ),
                    reason="malformed_managed_file",
                    details={"path": str(target_path)},
                )
            current_state = parse_file_state_marker(current_text)
            if current_state is None:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Target file is missing a valid file-state marker: {target_path}",
                    reason="malformed_managed_file",
                    details={"path": str(target_path)},
                )
            if current_state.revision != args.expected_file_revision:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=3,
                    message=(
                        f"File revision changed from {args.expected_file_revision} to "
                        f"{current_state.revision}. Reread the file before writing."
                    ),
                    payload=failure_payload(
                        "stale_write_context",
                        language=workspace.workspace_language,
                        error=(
                            f"File revision changed from {args.expected_file_revision} to "
                            f"{current_state.revision}. Reread the file before writing."
                        ),
                        details={
                            "expected_file_revision": args.expected_file_revision,
                            "current_file_revision": current_state.revision,
                        },
                    ),
                )

            raw_managed_write_recovery_details = prepared_body_failure_details(
                input_mode=input_mode,
                source_file=args.source_file,
                project_root=workspace.project_root,
                file_key=args.file_key,
                write_type=WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
            )
            source_marker = parse_file_marker(source_text)
            if source_marker is not None and source_marker.file_key != args.file_key:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        f"Source file marker '{source_marker.file_key}' does not match requested file key '{args.file_key}'."
                    ),
                    reason="invalid_prepared_input",
                    details=managed_markdown_structural_failure_details(
                        raw_managed_write_recovery_details,
                        reason_code="source_file_key_mismatch",
                        extra={
                            "source_file_key": source_marker.file_key,
                            "requested_file_key": args.file_key,
                        },
                    ),
                )

            timestamp = now_iso_timestamp()
            new_file_revision = current_state.revision + 1
            new_workspace_revision = state["workspace_revision"] + 1
            body_text, input_mode = prepare_body_text(
                parser,
                json_mode=args.json,
                file_key=args.file_key,
                source_text=source_text,
                source_kind=input_mode,
                input_format=args.input_format,
                expected_language=workspace.workspace_language,
                source_file=args.source_file,
                project_root=workspace.project_root,
            )
            managed_write_recovery_details = prepared_body_failure_details(
                input_mode=input_mode,
                source_file=args.source_file,
                project_root=workspace.project_root,
                file_key=args.file_key,
                write_type=WRITE_TYPE_BY_FILE_KEY.get(args.file_key),
            )
            validate_prepared_body(
                parser,
                json_mode=args.json,
                body_text=body_text,
                recovery_details=managed_write_recovery_details,
                include_reserved_marker_route=args.input_format == "markdown",
            )
            missing_keys = missing_section_keys(body_text, SECTION_KEYS[args.file_key])
            if missing_keys:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Refusing to commit because the prepared file is missing required section markers: "
                        + ", ".join(missing_keys)
                    ),
                    reason="invalid_prepared_input",
                    details=managed_markdown_structural_failure_details(
                        managed_write_recovery_details,
                        reason_code="missing_section_keys",
                        extra={"missing_section_keys": missing_keys},
                    ),
                )
            duplicate_keys = duplicate_section_keys(body_text)
            if duplicate_keys:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Refusing to commit because the prepared file contains duplicate section markers: "
                        + ", ".join(duplicate_keys)
                    ),
                    reason="invalid_prepared_input",
                    details=managed_markdown_structural_failure_details(
                        managed_write_recovery_details,
                        reason_code="duplicate_section_keys",
                        extra={"duplicate_section_keys": duplicate_keys},
                    ),
                )
            unknown_keys = unknown_section_keys(
                body_text,
                [*SECTION_KEYS[args.file_key], *OPTIONAL_SECTION_KEYS.get(args.file_key, [])],
            )
            if unknown_keys:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Refusing to commit because the prepared file contains unknown section markers: "
                        + ", ".join(unknown_keys)
                    ),
                    reason="invalid_prepared_input",
                    details=managed_markdown_structural_failure_details(
                        managed_write_recovery_details,
                        reason_code="unknown_section_keys",
                        extra={"unknown_section_keys": unknown_keys},
                    ),
                )
            new_text = build_managed_text(
                file_key=args.file_key,
                body_text=body_text,
                language=workspace.workspace_language,
                writer_id=writer_id,
                file_revision=new_file_revision,
                base_workspace_revision=new_workspace_revision,
                timestamp=timestamp,
            )
            try:
                atomic_write_if_unchanged(target_path, expected_text=current_text, new_text=new_text)
            except OSError as exc:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Filesystem error while writing {target_path}: {exc}",
                    reason="damaged_sidecar",
                )

            state["workspace_revision"] = new_workspace_revision
            state["files"][args.file_key] = {
                "file_revision": new_file_revision,
                "updated_at": timestamp,
                "writer_id": writer_id,
                "base_workspace_revision": new_workspace_revision,
            }
            if args.file_key == "update_protocol":
                state["update_protocol_revision"] = new_file_revision
            try:
                dump_json(state_path, state)
            except OSError as exc:
                try:
                    restore_text_snapshot(target_path, existed=True, text=current_text)
                except OSError as rollback_exc:
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=(
                            f"Failed to update state after writing {target_path}: {exc}. "
                            f"Rollback also failed: {rollback_exc}. Workspace may be partially updated."
                        ),
                        reason="damaged_sidecar",
                    )
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        f"Failed to update state after writing {target_path}: {exc}. "
                        "The target file was restored to its previous content."
                    ),
                    reason="damaged_sidecar",
                )
    except LockBusyError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=str(exc),
            reason="write_lock_busy",
        )
    except ConfigContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Filesystem error: {exc}"
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            reason="damaged_sidecar",
        )

    payload = {
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "file_key": args.file_key,
        "input_mode": input_mode,
        "target_path": str(target_path),
        "new_file_revision": new_file_revision,
        "new_workspace_revision": new_workspace_revision,
        **attribution.public_fields(),
        "ok": True,
    }
    if wrapper_metadata is not None:
        payload["wrapper_metadata"] = wrapper_metadata
    if args.json:
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Committed {args.file_key} to {target_path}")


if __name__ == "__main__":
    main()
