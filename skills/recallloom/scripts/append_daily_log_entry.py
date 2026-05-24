#!/usr/bin/env python3
"""Safely append a milestone entry to a RecallLoom daily log."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys

from core.continuity.workday import logical_workday_for
from core.failure.contracts import failure_payload, preferred_failure_language
from core.output.privacy import publicize_json_value
from core.protocol.contracts import FILE_KEYS, SECTION_KEYS
from core.protocol.markers import (
    daily_log_entry_marker,
    file_marker,
    parse_daily_log_scaffold_marker,
    parse_file_marker,
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
    atomic_write_if_unchanged,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    DAILY_LOG_ENTRY_RE,
    DAILY_LOGS_DIRNAME,
    EnvironmentContractError,
    LockBusyError,
    StorageResolutionError,
    dump_json,
    detect_update_protocol_time_policy_cues,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue_for_sources,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    latest_active_daily_log,
    load_workspace_state,
    normalize_wrapper_metadata_json,
    now_iso_timestamp,
    parse_daily_log_entry_line,
    parse_iso_date,
    read_text,
    resolve_writer_attribution,
    restore_text_snapshot,
    validate_iso_date,
    WrapperMetadataSecurityError,
    workspace_write_lock,
)


DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024
DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR = 3
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
        description="Safely append a milestone entry to a RecallLoom daily log."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument("--date", help="Daily log date in YYYY-MM-DD.")
    parser.add_argument(
        "--allow-historical",
        action="store_true",
        help=(
            "Allow appending to a non-latest ISO-dated daily log. "
            "Without this flag, appends to older daily logs are rejected."
        ),
    )
    parser.add_argument("--entry-file", help="Path to prepared entry content.")
    parser.add_argument("--entry-json", help="Prepared entry JSON object as a string.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared entry content from UTF-8 stdin instead of a file.",
    )
    parser.add_argument(
        "--input-format",
        choices=("auto", "markdown", "json"),
        default="auto",
        help=(
            "Interpret prepared entry input as markdown or JSON. "
            "auto treats --entry-json as JSON and other sources as markdown."
        ),
    )
    parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        default=DEFAULT_MAX_INPUT_BYTES,
        help="Maximum prepared-entry input size in bytes. Defaults to 4 MiB.",
    )
    parser.add_argument("--expected-workspace-revision", type=int)
    parser.add_argument(
        "--no-auto-detect",
        action="store_true",
        help=(
            "Require explicit --date and --expected-workspace-revision instead of auto-detecting "
            "missing values from the locked workspace state."
        ),
    )
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
    entry_source: PreparedInputSource,
    max_input_bytes: int,
) -> str:
    try:
        return read_prepared_input_source_text(
            entry_source,
            max_input_bytes=max_input_bytes,
            label="entry",
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


def load_entry_source(
    parser,
    *,
    json_mode: bool,
    entry_json: str | None,
    entry_file: str | None,
    use_stdin: bool,
    max_input_bytes: int,
    project_root: Path | None = None,
    storage_root: Path | None = None,
) -> tuple[str, str, Path | None]:
    selected_sources = int(entry_json is not None) + int(entry_file is not None) + int(use_stdin)
    if selected_sources != 1:
        if selected_sources > 1:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="Use exactly one prepared-entry input: --entry-json, --entry-file, or --stdin.",
                reason="invalid_prepared_input",
            )
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Provide prepared entry content with exactly one of --entry-json, --entry-file, or --stdin.",
            reason="invalid_prepared_input",
        )

    if entry_json is not None:
        entry_json_size = len(entry_json.encode("utf-8"))
        if entry_json_size > max_input_bytes:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message=f"Entry JSON input exceeds --max-input-bytes ({entry_json_size} > {max_input_bytes}).",
                reason="invalid_prepared_input",
                details={"size": entry_json_size, "max_input_bytes": max_input_bytes},
            )
        return entry_json, "entry-json", None

    if entry_file:
        if project_root is None or storage_root is None:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="Internal error: project root is required before reading --entry-file.",
                reason="invalid_prepared_input",
                details={"input_mode": "file", "reason_code": "prepared_input_context_missing"},
            )
        try:
            entry_source = validate_prepared_input_source_path(
                entry_file,
                project_root=project_root,
                storage_root=storage_root,
                input_role="entry-file",
                label="entry",
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
                entry_source=entry_source,
                max_input_bytes=max_input_bytes,
            ),
            "file",
            entry_source.path,
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
        return raw.decode("utf-8"), "stdin", None
    except UnicodeDecodeError:
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Stdin input must be valid UTF-8.",
            reason="invalid_prepared_input",
        )
    raise AssertionError("unreachable")


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
    details = daily_log_json_failure_details(
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


def reject_json_reserved_markers(
    parser,
    *,
    json_mode: bool,
    section_key: str,
    text: str,
    recovery_details: dict | None = None,
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
            "Refusing to append because prepared entry JSON section "
            f"'{section_key}' contains a reserved RecallLoom marker on line {line_number}."
        ),
        reason="invalid_prepared_input",
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
    parser,
    *,
    json_mode: bool,
    section_key: str,
    value: object,
    recovery_details: dict | None = None,
) -> str:
    if isinstance(value, str):
        normalized = value.strip()
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
                f"Prepared entry JSON section '{section_key}' must be a non-empty string "
                "or a non-empty list of strings."
            ),
            recovery_details=recovery_details,
            reason_code="empty_section_string",
        )

    if isinstance(value, list):
        if not value:
            invalid_json_section_value(
                parser,
                json_mode=json_mode,
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
                    parser,
                    json_mode=json_mode,
                    section_key=section_key,
                    message=(
                        f"Prepared entry JSON section '{section_key}' list items must be non-empty strings."
                    ),
                    recovery_details=recovery_details,
                    field_path=f"$.{section_key}[]",
                    expected_type="non_empty_string",
                    reason_code="invalid_section_list_item_type",
                )
            normalized_item = item.strip()
            if not normalized_item:
                invalid_json_section_value(
                    parser,
                    json_mode=json_mode,
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
            f"Prepared entry JSON section '{section_key}' must be a non-empty string "
            "or a non-empty list of strings."
        ),
        recovery_details=recovery_details,
    )
    raise AssertionError("unreachable")


def normalize_json_entry_text(
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
                "Prepared entry JSON must be a valid JSON object: "
                f"{exc.msg} at line {exc.lineno} column {exc.colno}."
            ),
            reason="invalid_prepared_input",
            details=daily_log_json_failure_details(
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
            message="Prepared entry JSON must be an object keyed by daily-log section names.",
            reason="invalid_prepared_input",
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
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message="Prepared entry JSON contains unknown daily-log section keys.",
            reason="invalid_prepared_input",
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
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Prepared entry JSON is missing required daily-log section keys: "
                + ", ".join(missing_keys)
            ),
            reason="invalid_prepared_input",
            details=details,
        )

    sections: list[str] = []
    for section_key in required_keys:
        sections.append(
            section_marker(section_key)
            + "\n"
            + normalize_json_section_value(
                parser,
                json_mode=json_mode,
                section_key=section_key,
                value=payload[section_key],
                recovery_details=recovery_details,
            )
        )
    return "\n\n".join(sections) + "\n"


def prepare_entry_text(
    parser,
    *,
    json_mode: bool,
    source_kind: str,
    raw_text: str,
    input_format: str,
    entry_path: Path | None,
    project_root: Path | None = None,
) -> tuple[str, str]:
    if source_kind == "entry-json":
        recovery_details = {"input_mode": "json-string"}
        if input_format == "markdown":
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="--entry-json only supports JSON input. Use --input-format auto or --input-format json.",
                reason="invalid_prepared_input",
                details=recovery_details,
            )
        return (
            normalize_json_entry_text(
                parser,
                json_mode=json_mode,
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
                parser,
                json_mode=json_mode,
                raw_text=raw_text,
                recovery_details=recovery_details,
            ),
            input_mode,
        )

    return raw_text, source_kind


def build_entry_block(body_text: str, *, writer_id: str, entry_seq: int) -> str:
    marker = daily_log_entry_marker(
        entry_id=f"entry-{entry_seq}",
        created_at=now_iso_timestamp(),
        writer_id=writer_id,
        entry_seq=entry_seq,
    )
    body = body_text.strip("\n")
    return marker if not body else marker + "\n\n" + body


def existing_entry_sequences(text: str) -> list[int]:
    sequences: list[int] = []
    for line in text.splitlines():
        entry = parse_daily_log_entry_line(line)
        if entry is not None:
            sequences.append(entry.entry_seq)
    return sequences


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


def validate_entry_body(
    parser,
    *,
    json_mode: bool,
    body_text: str,
    recovery_details: dict | None = None,
) -> None:
    reserved = reserved_marker_lines(body_text, match_embedded=True)
    if reserved:
        hit = reserved[0]
        line_number = int(hit["line_number"])
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Refusing to append because the prepared entry contains a reserved RecallLoom marker "
                f"on line {line_number}."
            ),
            reason="invalid_prepared_input",
            details=reserved_marker_failure_details(
                recovery_details,
                line_number=line_number,
                marker_family=str(hit["marker_family"]),
            ),
        )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=(
                "Refusing to append because the prepared entry failed the attached-text safety scan: "
                + ", ".join(attach_scan["hard_block_reasons"])
            ),
            payload=failure_payload(
                "attach_scan_blocked",
                language=preferred_failure_language(os.environ),
                error=(
                    "Refusing to append because the prepared entry failed the attached-text safety scan: "
                    + ", ".join(attach_scan["hard_block_reasons"])
                ),
                details={
                    **(recovery_details or {}),
                    "hard_block_reasons": attach_scan["hard_block_reasons"],
                },
            ),
        )


def build_append_failure_details(
    *,
    project_root: Path,
    target_path: Path,
    target_date: str,
    current_workspace_revision: int,
    entry_path: Path | None,
    input_mode: str,
    extra: dict | None = None,
) -> dict:
    details: dict[str, object] = {
        "project_root": str(project_root),
        "target_path": str(target_path),
        "target_date": target_date,
        "current_workspace_revision": current_workspace_revision,
        "input_mode": input_mode,
    }
    if entry_path is not None:
        details["entry_path"] = str(entry_path)
    if extra:
        details.update(extra)
    return details


def resolve_target_date(
    *,
    explicit_date: str | None,
    latest_existing: Path | None,
    logical_workday: date,
) -> tuple[date, date | None, str]:
    latest_existing_date = parse_iso_date(latest_existing.stem) if latest_existing is not None else None
    if explicit_date is not None:
        return parse_iso_date(explicit_date), latest_existing_date, "explicit"
    if latest_existing_date is not None and latest_existing_date == logical_workday:
        return latest_existing_date, latest_existing_date, "auto_same_day_active"
    return logical_workday, latest_existing_date, "auto_logical_workday"


def exit_with_append_date_guard(
    parser,
    *,
    json_mode: bool,
    workspace_language: str,
    exit_code: int,
    reason: str,
    message: str,
    details: dict,
) -> None:
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        payload=failure_payload(
            reason,
            language=workspace_language,
            error=message,
            details=details,
        ),
    )


def enforce_logical_workday_append_guards(
    parser,
    *,
    json_mode: bool,
    workspace_language: str,
    target_path: Path,
    target_date: date,
    latest_existing: Path | None,
    logical_workday: date | None = None,
    recovery_details: dict | None = None,
) -> date | None:
    if logical_workday is None:
        logical_workday = logical_workday_for(
            datetime.now().astimezone(),
            DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
        )
    logical_workday_iso = logical_workday.isoformat()
    latest_existing_date = parse_iso_date(latest_existing.stem) if latest_existing is not None else None
    latest_existing_text = str(latest_existing) if latest_existing is not None else None
    base_details = dict(recovery_details or {})
    base_details.update(
        {
            "target_path": str(target_path),
            "target_date": target_date.isoformat(),
            "logical_workday": logical_workday_iso,
            "latest_active_daily_log": latest_existing_text,
        }
    )

    if target_date > logical_workday:
        message = (
            f"Refusing to append to future-dated daily log {target_path}. "
            f"The current logical workday is {logical_workday_iso}. "
            "--allow-historical only applies to intentional historical backfills and cannot override "
            "future-dated append guards."
        )
        exit_with_append_date_guard(
            parser,
            json_mode=json_mode,
            workspace_language=workspace_language,
            exit_code=2,
            reason="project_time_policy_review_required",
            message=message,
            details=base_details,
        )

    if latest_existing_date is not None and latest_existing_date > logical_workday:
        details = dict(base_details)
        details["latest_active_day"] = latest_existing_date.isoformat()
        message = (
            "Refusing to append because the latest active ISO-dated daily log "
            f"{latest_existing} is ahead of the current logical workday {logical_workday_iso}. "
            "Review the active date before appending to any daily log. "
            "--allow-historical only applies to intentional historical backfills and cannot override "
            "future-dated append guards."
        )
        exit_with_append_date_guard(
            parser,
            json_mode=json_mode,
            workspace_language=workspace_language,
            exit_code=2,
            reason="project_time_policy_review_required",
            message=message,
            details=details,
        )

    return latest_existing_date


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

    if args.date is not None and not validate_iso_date(args.date):
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Invalid --date value: {args.date}",
            reason="invalid_date",
            details={"date": args.date},
        )
    try:
        attribution = resolve_writer_attribution(
            explicit_writer_id=args.writer_id,
            invocation_surface="append_daily_log_entry.py",
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
        source_paths=[args.entry_file],
    )

    raw_entry_text, source_kind, entry_path = load_entry_source(
        parser,
        json_mode=args.json,
        entry_json=args.entry_json,
        entry_file=args.entry_file,
        use_stdin=args.stdin,
        max_input_bytes=args.max_input_bytes,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )
    body_text, input_mode = prepare_entry_text(
        parser,
        json_mode=args.json,
        source_kind=source_kind,
        raw_text=raw_entry_text,
        input_format=args.input_format,
        entry_path=entry_path,
        project_root=workspace.project_root,
    )
    if args.no_auto_detect and (
        args.date is None or args.expected_workspace_revision is None
    ):
        missing_fields = []
        if args.date is None:
            missing_fields.append("date")
        if args.expected_workspace_revision is None:
            missing_fields.append("expected_workspace_revision")
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=(
                "--no-auto-detect requires explicit --date and --expected-workspace-revision. "
                f"Missing: {', '.join(missing_fields)}."
            ),
            reason="invalid_prepared_input",
            details={
                "project_root": str(workspace.project_root),
                "input_mode": input_mode,
                "missing_fields": missing_fields,
                "no_auto_detect": True,
            },
        )

    try:
        with workspace_write_lock(workspace.project_root, "append_daily_log_entry.py"):
            state_path = workspace.storage_root / FILE_KEYS["state"]
            state = load_workspace_state(state_path)
            logs_dir = workspace.storage_root / DAILY_LOGS_DIRNAME
            latest_existing = latest_active_daily_log(logs_dir)
            logical_workday = logical_workday_for(
                datetime.now().astimezone(),
                DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
            )
            update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
            project_time_policy_cues = (
                detect_update_protocol_time_policy_cues(read_text(update_protocol_path))
                if update_protocol_path.is_file()
                else []
            )
            if args.date is None and project_time_policy_cues:
                suggested_target_path = workspace.storage_root / DAILY_LOGS_DIRNAME / f"{logical_workday.isoformat()}.md"
                append_failure_details = build_append_failure_details(
                    project_root=workspace.project_root,
                    target_path=suggested_target_path,
                    target_date=logical_workday.isoformat(),
                    current_workspace_revision=state["workspace_revision"],
                    entry_path=entry_path,
                    input_mode=input_mode,
                    extra={
                        "auto_detected_date": True,
                        "auto_detected_workspace_revision": args.expected_workspace_revision is None,
                        "date_resolution_source": "project_local_review_required",
                        "workspace_revision_source": (
                            "state_current" if args.expected_workspace_revision is None else "explicit"
                        ),
                        "logical_workday": logical_workday.isoformat(),
                        "latest_active_daily_log": str(latest_existing) if latest_existing is not None else None,
                        "latest_active_day": latest_existing.stem if latest_existing is not None else None,
                        "project_time_policy_cues": project_time_policy_cues,
                    },
                )
                message = (
                    "Project-local time-policy cues were detected in update_protocol.md. "
                    "Append requires an explicit --date before writing when date auto-detect would otherwise apply."
                )
                exit_with_append_date_guard(
                    parser,
                    json_mode=args.json,
                    workspace_language=workspace.workspace_language,
                    exit_code=2,
                    reason="project_time_policy_review_required",
                    message=message,
                    details=append_failure_details,
                )
            target_date, latest_existing_date, date_resolution_source = resolve_target_date(
                explicit_date=args.date,
                latest_existing=latest_existing,
                logical_workday=logical_workday,
            )
            target_date_iso = target_date.isoformat()
            target_path = workspace.storage_root / DAILY_LOGS_DIRNAME / f"{target_date_iso}.md"
            resolved_workspace_revision = (
                state["workspace_revision"]
                if args.expected_workspace_revision is None
                else args.expected_workspace_revision
            )
            workspace_revision_source = (
                "state_current"
                if args.expected_workspace_revision is None
                else "explicit"
            )
            workspace_revision_guard_mode = (
                "lock_snapshot_current"
                if args.expected_workspace_revision is None
                else "explicit_mismatch_check"
            )
            append_failure_details = build_append_failure_details(
                project_root=workspace.project_root,
                target_path=target_path,
                target_date=target_date_iso,
                current_workspace_revision=state["workspace_revision"],
                entry_path=entry_path,
                input_mode=input_mode,
                extra={
                    "auto_detected_date": args.date is None,
                    "auto_detected_workspace_revision": args.expected_workspace_revision is None,
                    "date_resolution_source": date_resolution_source,
                    "workspace_revision_source": workspace_revision_source,
                    "logical_workday": logical_workday.isoformat(),
                    "latest_active_daily_log": str(latest_existing) if latest_existing is not None else None,
                    "latest_active_day": (
                        latest_existing_date.isoformat() if latest_existing_date is not None else None
                    ),
                },
            )
            if (
                args.expected_workspace_revision is not None
                and state["workspace_revision"] != args.expected_workspace_revision
            ):
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=3,
                    message=(
                        f"Workspace revision changed from {args.expected_workspace_revision} to "
                        f"{state['workspace_revision']}. Rerun preflight before appending."
                    ),
                    payload=failure_payload(
                        "stale_write_context",
                        language=workspace.workspace_language,
                        error=(
                            f"Workspace revision changed from {args.expected_workspace_revision} to "
                            f"{state['workspace_revision']}. Rerun preflight before appending."
                        ),
                        details={
                            "expected_workspace_revision": args.expected_workspace_revision,
                            **append_failure_details,
                        },
                    ),
                )

            latest_existing_date = enforce_logical_workday_append_guards(
                parser,
                json_mode=args.json,
                workspace_language=workspace.workspace_language,
                target_path=target_path,
                target_date=target_date,
                latest_existing=latest_existing,
                logical_workday=logical_workday,
                recovery_details=append_failure_details,
            )
            if (
                latest_existing_date is not None
                and target_date < latest_existing_date
                and not args.allow_historical
            ):
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        f"Refusing to append to non-latest daily log {target_path}. "
                        f"The latest active ISO-dated daily log is {latest_existing}. "
                        "Re-run with --allow-historical only when you intentionally need a historical append."
                    ),
                    payload=failure_payload(
                        "historical_append_requires_confirmation",
                        language=workspace.workspace_language,
                        error=(
                            f"Refusing to append to non-latest daily log {target_path}. "
                            f"The latest active ISO-dated daily log is {latest_existing}. "
                            "Re-run with --allow-historical only when you intentionally need a historical append."
                        ),
                        details={
                            **append_failure_details,
                            "latest_active_daily_log": str(latest_existing),
                        },
                    ),
                )

            missing_keys = missing_section_keys(body_text, SECTION_KEYS["daily_log"])
            validate_entry_body(
                parser,
                json_mode=args.json,
                body_text=body_text,
                recovery_details=append_failure_details,
            )
            if missing_keys:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Refusing to append a daily-log entry because the prepared entry file is missing required "
                        "section markers: " + ", ".join(missing_keys)
                    ),
                    reason="invalid_prepared_input",
                    details={
                        **append_failure_details,
                        "missing_section_keys": missing_keys,
                    },
                )
            duplicate_keys = duplicate_section_keys(body_text)
            if duplicate_keys:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Refusing to append a daily-log entry because the prepared entry file contains duplicate "
                        "section markers: " + ", ".join(duplicate_keys)
                    ),
                    reason="invalid_prepared_input",
                    details={
                        **append_failure_details,
                        "duplicate_section_keys": duplicate_keys,
                    },
                )
            unknown_keys = unknown_section_keys(body_text, SECTION_KEYS["daily_log"])
            if unknown_keys:
                exit_with_failure_contract(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        "Refusing to append a daily-log entry because the prepared entry file contains unknown "
                        "section markers: " + ", ".join(unknown_keys)
                    ),
                    reason="invalid_prepared_input",
                    details={
                        **append_failure_details,
                        "unknown_section_keys": unknown_keys,
                    },
                )
            next_seq = 1
            target_existed = target_path.exists()
            current_text = read_text(target_path) if target_existed else ""
            if target_path.exists():
                marker = parse_file_marker(current_text)
                if marker is None or marker.file_key != "daily_log":
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=f"Target daily log is missing a valid daily_log file marker: {target_path}",
                        reason="malformed_managed_file",
                        details={"path": str(target_path)},
                    )
                if marker.language != workspace.workspace_language:
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=(
                            f"Target daily log language marker '{marker.language}' does not match workspace_language "
                            f"'{workspace.workspace_language}'. Repair the target file before appending."
                        ),
                        reason="malformed_managed_file",
                        details={"path": str(target_path)},
                    )
                scaffold = parse_daily_log_scaffold_marker(current_text)
                sequences = existing_entry_sequences(current_text)
                if scaffold and sequences:
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=f"Refusing to append to malformed scaffold daily log {target_path}: scaffold marker cannot coexist with entry markers.",
                        reason="malformed_managed_file",
                        details={"path": str(target_path)},
                    )
                expected_sequences = list(range(1, len(sequences) + 1))
                if sequences != expected_sequences:
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=(
                            f"Refusing to append to damaged daily log {target_path}. "
                            f"Expected contiguous entry_seq values {expected_sequences}, found {sequences}."
                        ),
                        reason="malformed_managed_file",
                        details={
                            "path": str(target_path),
                            "expected_sequences": expected_sequences,
                            "actual_sequences": sequences,
                        },
                    )
                next_seq = len(sequences) + 1
                if scaffold and not sequences:
                    header = file_marker("daily_log", workspace.workspace_language)
                    updated_text = header + "\n" + build_entry_block(body_text, writer_id=writer_id, entry_seq=next_seq) + "\n"
                else:
                    updated_text = (
                        current_text.rstrip("\n")
                        + "\n\n"
                        + build_entry_block(body_text, writer_id=writer_id, entry_seq=next_seq)
                        + "\n"
                    )
                try:
                    atomic_write_if_unchanged(
                        target_path,
                        expected_text=current_text,
                        new_text=updated_text,
                    )
                except OSError as exc:
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=f"Filesystem error while writing {target_path}: {exc}",
                        reason="damaged_sidecar",
                    )
            else:
                header = file_marker("daily_log", workspace.workspace_language)
                updated_text = header + "\n" + build_entry_block(body_text, writer_id=writer_id, entry_seq=next_seq) + "\n"
                try:
                    atomic_write_if_unchanged(
                        target_path,
                        expected_text="",
                        new_text=updated_text,
                    )
                except OSError as exc:
                    exit_with_failure_contract(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=f"Filesystem error while writing {target_path}: {exc}",
                        reason="damaged_sidecar",
                    )

            state["workspace_revision"] += 1
            target_is_latest_after_write = (
                latest_existing_date is None or target_date >= latest_existing_date
            )
            if target_is_latest_after_write:
                state["daily_logs"]["latest_file"] = target_path.relative_to(workspace.storage_root).as_posix()
                state["daily_logs"]["latest_entry_id"] = f"entry-{next_seq}"
                state["daily_logs"]["latest_entry_seq"] = next_seq
                state["daily_logs"]["entry_count"] = next_seq
                if "updated_at" in state["daily_logs"]:
                    refreshed_at = now_iso_timestamp()
                    if refreshed_at == state["daily_logs"].get("updated_at"):
                        refreshed_at = datetime.now().astimezone().isoformat(timespec="microseconds")
                    state["daily_logs"]["updated_at"] = refreshed_at
            try:
                dump_json(state_path, state)
            except OSError as exc:
                try:
                    restore_text_snapshot(target_path, existed=target_existed, text=current_text)
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
        "ok": True,
        "input_mode": input_mode,
        "target_path": str(target_path),
        "entry_seq": next_seq,
        "new_workspace_revision": state["workspace_revision"],
        "allow_historical": args.allow_historical,
        "state_cursor_updated": target_is_latest_after_write,
        **attribution.public_fields(),
        "auto_detect": {
            "date_used": args.date is None,
            "workspace_revision_used": args.expected_workspace_revision is None,
            "logical_workday": logical_workday.isoformat(),
            "latest_active_daily_log": str(latest_existing) if latest_existing is not None else None,
            "latest_active_day": (
                latest_existing_date.isoformat() if latest_existing_date is not None else None
            ),
            "resolved_date": target_date.isoformat(),
            "resolved_workspace_revision": resolved_workspace_revision,
            "date_resolution_source": date_resolution_source,
            "workspace_revision_source": workspace_revision_source,
            "workspace_revision_guard_mode": workspace_revision_guard_mode,
        },
    }
    if wrapper_metadata is not None:
        payload["wrapper_metadata"] = wrapper_metadata
    if args.json:
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report
        public_payload = publicize_json_value(payload, project_root=workspace.project_root)
        print(json.dumps(public_payload, ensure_ascii=False, indent=2))
    else:
        print(f"Appended daily log entry to {target_path}")


if __name__ == "__main__":
    main()
