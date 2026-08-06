#!/usr/bin/env python3
"""Marker rendering/parsing and managed-text primitives for RecallLoom protocol files."""

from __future__ import annotations

from dataclasses import dataclass

from core.errors import ConfigContractError
from core.protocol.contracts import (
    CURRENT_PROTOCOL_VERSION,
    DAILY_LOG_ENTRY_MARKER_TEMPLATE,
    DAILY_LOG_ENTRY_RE,
    DAILY_LOG_SCAFFOLD_MARKER_TEMPLATE,
    DAILY_LOG_SCAFFOLD_RE,
    FILE_MARKER_RE,
    FILE_MARKER_TEMPLATE,
    FILE_STATE_MARKER_TEMPLATE,
    FILE_STATE_RE,
    LAST_WRITER_MARKER_TEMPLATE,
    SECTION_MARKER_RE,
)


@dataclass(frozen=True)
class FileMarkerInfo:
    file_key: str
    version: str
    language: str


@dataclass(frozen=True)
class FileStateInfo:
    revision: int
    updated_at: str
    writer_id: str
    base_workspace_revision: int


@dataclass(frozen=True)
class DailyLogEntryInfo:
    entry_id: str
    created_at: str
    writer_id: str
    entry_seq: int


def file_marker(file_key: str, language: str, version: str = CURRENT_PROTOCOL_VERSION) -> str:
    return FILE_MARKER_TEMPLATE.format(file_key=file_key, version=version, lang=language)


def file_state_marker(
    *,
    revision: int,
    updated_at: str,
    writer_id: str,
    base_workspace_revision: int,
) -> str:
    return FILE_STATE_MARKER_TEMPLATE.format(
        revision=revision,
        updated_at=updated_at,
        writer_id=writer_id,
        base_workspace_revision=base_workspace_revision,
    )


def daily_log_entry_marker(
    *,
    entry_id: str,
    created_at: str,
    writer_id: str,
    entry_seq: int,
) -> str:
    return DAILY_LOG_ENTRY_MARKER_TEMPLATE.format(
        entry_id=entry_id,
        created_at=created_at,
        writer_id=writer_id,
        entry_seq=entry_seq,
    )


def daily_log_scaffold_marker() -> str:
    return DAILY_LOG_SCAFFOLD_MARKER_TEMPLATE


def section_marker(section_key: str) -> str:
    return f"<!-- section: {section_key} -->"


def rolling_summary_header(tool_name: str, day: str) -> str:
    return LAST_WRITER_MARKER_TEMPLATE.format(tool=tool_name, date=day)


def canonicalize_managed_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_tool_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigContractError("tool_name must be a non-empty string")
    if any(ch in value for ch in {"|", "]", "\n", "\r"}):
        raise ConfigContractError(
            "tool_name may not contain '|', ']', or line-break characters because it is embedded in machine-readable markers"
        )
    return value.strip()


def validate_writer_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigContractError("writer_id must be a non-empty string")
    if any(ch in value for ch in {"|", "]", "\n", "\r"}):
        raise ConfigContractError(
            "writer_id may not contain '|', ']', or line-break characters because it is embedded in machine-readable markers"
        )
    return value.strip()


def parse_file_marker(text: str) -> FileMarkerInfo | None:
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    match = FILE_MARKER_RE.match(first_line)
    if not match:
        return None
    return FileMarkerInfo(
        file_key=match.group("file_key"),
        version=match.group("version"),
        language=match.group("lang"),
    )


def parse_file_state_marker(text: str) -> FileStateInfo | None:
    for line in text.splitlines()[:4]:
        match = FILE_STATE_RE.match(line.strip())
        if match:
            return FileStateInfo(
                revision=int(match.group("revision")),
                updated_at=match.group("updated_at"),
                writer_id=match.group("writer_id").strip(),
                base_workspace_revision=int(match.group("base_workspace_revision")),
            )
    return None


def parse_daily_log_entry_marker(text: str) -> DailyLogEntryInfo | None:
    for line in text.splitlines()[:4]:
        match = DAILY_LOG_ENTRY_RE.match(line.strip())
        if match:
            return DailyLogEntryInfo(
                entry_id=match.group("entry_id"),
                created_at=match.group("created_at"),
                writer_id=match.group("writer_id").strip(),
                entry_seq=int(match.group("entry_seq")),
            )
    return None


def parse_daily_log_scaffold_marker(text: str) -> bool:
    return any(DAILY_LOG_SCAFFOLD_RE.match(line.strip()) for line in text.splitlines()[:4])


__all__ = [
    "DailyLogEntryInfo",
    "FileMarkerInfo",
    "FileStateInfo",
    "canonicalize_managed_text_newlines",
    "daily_log_entry_marker",
    "daily_log_scaffold_marker",
    "file_marker",
    "file_state_marker",
    "parse_daily_log_entry_marker",
    "parse_daily_log_scaffold_marker",
    "parse_file_marker",
    "parse_file_state_marker",
    "rolling_summary_header",
    "section_marker",
    "validate_tool_name",
    "validate_writer_id",
    "SECTION_MARKER_RE",
]
