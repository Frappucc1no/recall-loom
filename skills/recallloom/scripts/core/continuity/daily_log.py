#!/usr/bin/env python3
"""Daily-log discovery, entry parsing, and cursor validation for RecallLoom.

Single owner of the daily-log snapshot machinery extracted from `_common.py`
(T050-03A): ISO-dated filename discovery, entry-marker line parsing, and the
strict cursor derivation/validation shared by preflight, status, provenance
evidence, and the mutation helpers. `_common` re-exports these names for
compatibility; behavior is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.protocol.contracts import DAILY_LOG_ENTRY_RE, DAILY_LOGS_DIRNAME
from core.protocol.markers import (
    DailyLogEntryInfo,
    parse_daily_log_scaffold_marker,
    parse_file_marker,
)
from core.workspace.runtime import DATE_FILE_RE, read_text


def sorted_daily_log_files(logs_dir: Path) -> list[Path]:
    dated_files: list[Path] = []
    if not logs_dir.is_dir():
        return dated_files
    for child in logs_dir.iterdir():
        if child.is_file() and DATE_FILE_RE.match(child.name):
            try:
                date.fromisoformat(child.stem)
            except ValueError:
                continue
            dated_files.append(child)
    return sorted(dated_files, key=lambda path: path.stem)


def invalid_iso_like_daily_log_files(logs_dir: Path) -> list[Path]:
    invalid: list[Path] = []
    if not logs_dir.is_dir():
        return invalid
    for child in sorted(logs_dir.iterdir(), key=lambda path: path.name):
        if child.is_file() and DATE_FILE_RE.match(child.name):
            try:
                date.fromisoformat(child.stem)
            except ValueError:
                invalid.append(child)
    return invalid


def sorted_active_daily_log_files(logs_dir: Path) -> list[Path]:
    return sorted_daily_log_files(logs_dir)


def latest_active_daily_log(logs_dir: Path) -> Path | None:
    active = sorted_active_daily_log_files(logs_dir)
    if not active:
        return None
    return active[-1]


def parse_daily_log_entry_line(line: str) -> DailyLogEntryInfo | None:
    match = DAILY_LOG_ENTRY_RE.match(line.strip())
    if not match:
        return None
    return DailyLogEntryInfo(
        entry_id=match.group("entry_id"),
        created_at=match.group("created_at"),
        writer_id=match.group("writer_id").strip(),
        entry_seq=int(match.group("entry_seq")),
    )


def daily_log_entries(text: str) -> list[DailyLogEntryInfo]:
    entries: list[DailyLogEntryInfo] = []
    for line in text.splitlines():
        entry = parse_daily_log_entry_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


def malformed_daily_log_entry_marker_lines(text: str) -> list[int]:
    malformed: list[int] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if candidate.startswith("<!-- daily-log-entry:") and parse_daily_log_entry_line(candidate) is None:
            malformed.append(line_number)
    return malformed


@dataclass(frozen=True)
class DailyLogCursor:
    latest_file: str | None
    latest_entry_id: str | None
    latest_entry_seq: int | None
    entry_count: int
    latest_path: Path | None = None

    def as_state_fields(self) -> dict[str, object]:
        return {
            "latest_file": self.latest_file,
            "latest_entry_id": self.latest_entry_id,
            "latest_entry_seq": self.latest_entry_seq,
            "entry_count": self.entry_count,
        }


DAILY_LOG_CURSOR_STATE_KEYS = (
    "latest_file",
    "latest_entry_id",
    "latest_entry_seq",
    "entry_count",
)


class DailyLogCursorError(Exception):
    """Structured refusal for damaged latest daily-log cursor evidence."""

    failure_reason = "malformed_managed_file"

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        path: Path | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.path = path
        self.details = {
            "reason_code": reason_code,
            "side_effect": "none",
            **(details or {}),
        }
        if path is not None:
            self.details.setdefault("path", str(path))


def _daily_log_cursor_sequence_error(
    entries: list[DailyLogEntryInfo],
    *,
    path: Path,
) -> DailyLogCursorError | None:
    if not entries:
        return None

    sequences = [entry.entry_seq for entry in entries]
    duplicate_sequences = sorted(
        seq for seq in set(sequences) if sequences.count(seq) > 1
    )
    if duplicate_sequences:
        return DailyLogCursorError(
            reason_code="duplicate_daily_log_entry_sequence",
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                f"daily log has duplicate entry-seq values: {duplicate_sequences}."
            ),
            path=path,
            details={
                "duplicate_entry_seq": duplicate_sequences,
                "actual_sequences": sequences,
            },
        )

    entry_ids = [entry.entry_id for entry in entries]
    duplicate_entry_ids = sorted(
        entry_id for entry_id in set(entry_ids) if entry_ids.count(entry_id) > 1
    )
    if duplicate_entry_ids:
        return DailyLogCursorError(
            reason_code="duplicate_daily_log_entry_id",
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                f"daily log has duplicate entry ids: {duplicate_entry_ids}."
            ),
            path=path,
            details={
                "duplicate_entry_ids": duplicate_entry_ids,
                "actual_entry_ids": entry_ids,
            },
        )

    noncanonical_entry_ids = [
        {
            "entry_seq": entry.entry_seq,
            "entry_id": entry.entry_id,
            "expected_entry_id": f"entry-{entry.entry_seq}",
        }
        for entry in entries
        if entry.entry_id != f"entry-{entry.entry_seq}"
    ]
    if noncanonical_entry_ids:
        return DailyLogCursorError(
            reason_code="noncanonical_daily_log_entry_id",
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                "daily log entry ids do not match their entry-seq values."
            ),
            path=path,
            details={
                "noncanonical_entry_ids": noncanonical_entry_ids,
                "actual_entry_ids": entry_ids,
                "actual_sequences": sequences,
            },
        )

    expected = list(range(1, len(entries) + 1))
    if sequences != expected:
        reason_code = (
            "out_of_order_daily_log_entry_sequence"
            if sorted(sequences) == expected
            else "noncontiguous_daily_log_entry_sequence"
        )
        return DailyLogCursorError(
            reason_code=reason_code,
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                f"daily log entry sequence is not canonical. Expected {expected}, "
                f"found {sequences}."
            ),
            path=path,
            details={
                "expected_sequences": expected,
                "actual_sequences": sequences,
            },
        )
    return None


def daily_log_cursor_from_text(
    text: str,
    *,
    path: Path,
    latest_file: str | None,
) -> DailyLogCursor:
    file_marker_info = parse_file_marker(text)
    if file_marker_info is None or file_marker_info.file_key != "daily_log":
        raise DailyLogCursorError(
            reason_code="malformed_latest_daily_log_file_marker",
            message=(
                "Refusing to calculate the daily-log cursor because the daily log is "
                "missing the required daily_log file marker."
            ),
            path=path,
            details={
                "latest_file": latest_file,
                "file_key": file_marker_info.file_key if file_marker_info else None,
            },
        )

    malformed_lines = malformed_daily_log_entry_marker_lines(text)
    if malformed_lines:
        raise DailyLogCursorError(
            reason_code="malformed_daily_log_entry_marker",
            message=(
                "Refusing to calculate the daily-log cursor because the daily log has "
                f"malformed daily-log-entry markers on lines {malformed_lines}."
            ),
            path=path,
            details={
                "latest_file": latest_file,
                "malformed_lines": malformed_lines,
            },
        )

    entries = daily_log_entries(text)
    scaffold = parse_daily_log_scaffold_marker(text)
    if scaffold and entries:
        raise DailyLogCursorError(
            reason_code="scaffold_daily_log_has_entries",
            message=(
                "Refusing to calculate the daily-log cursor because the daily log has "
                "both a scaffold marker and entry markers."
            ),
            path=path,
            details={"latest_file": latest_file},
        )
    if not entries:
        if not scaffold:
            raise DailyLogCursorError(
                reason_code="missing_daily_log_entry_marker",
                message=(
                    "Refusing to calculate the daily-log cursor because the daily log has "
                    "no entry markers and is not an empty scaffold."
                ),
                path=path,
                details={"latest_file": latest_file},
            )
        return DailyLogCursor(
            latest_file=latest_file,
            latest_entry_id=None,
            latest_entry_seq=None,
            entry_count=0,
            latest_path=path,
        )

    sequence_error = _daily_log_cursor_sequence_error(entries, path=path)
    if sequence_error is not None:
        raise sequence_error

    latest_entry_seq = entries[-1].entry_seq
    return DailyLogCursor(
        latest_file=latest_file,
        latest_entry_id=f"entry-{latest_entry_seq}",
        latest_entry_seq=latest_entry_seq,
        entry_count=len(entries),
        latest_path=path,
    )


def latest_active_daily_log_cursor(storage_root: Path) -> DailyLogCursor:
    logs_dir = storage_root / DAILY_LOGS_DIRNAME
    latest_path = latest_active_daily_log(logs_dir)
    if latest_path is None:
        return DailyLogCursor(
            latest_file=None,
            latest_entry_id=None,
            latest_entry_seq=0,
            entry_count=0,
            latest_path=None,
        )

    latest_file = latest_path.relative_to(storage_root).as_posix()
    try:
        text = read_text(latest_path)
    except (OSError, UnicodeDecodeError) as exc:
        raise DailyLogCursorError(
            reason_code="unreadable_latest_daily_log",
            message=f"Could not read latest active daily log {latest_path}: {exc}",
            path=latest_path,
        ) from exc

    return daily_log_cursor_from_text(
        text,
        path=latest_path,
        latest_file=latest_file,
    )


def state_claims_entry_bearing_latest_daily_log(state: dict) -> bool:
    daily_logs = state.get("daily_logs")
    if not isinstance(daily_logs, dict):
        return False
    entry_count = daily_logs.get("entry_count")
    return isinstance(entry_count, int) and not isinstance(entry_count, bool) and entry_count > 0


def validate_state_entry_bearing_latest_daily_log(
    *,
    storage_root: Path,
    state: dict,
) -> DailyLogCursor | None:
    if not state_claims_entry_bearing_latest_daily_log(state):
        return None

    daily_logs = state.get("daily_logs")
    if not isinstance(daily_logs, dict):
        raise DailyLogCursorError(
            reason_code="state_daily_logs_missing",
            message=(
                "Refusing to treat continuity as seeded because state.json does not "
                "contain a valid daily_logs cursor object."
            ),
        )

    latest_file = daily_logs.get("latest_file")
    if not isinstance(latest_file, str) or not latest_file.strip():
        raise DailyLogCursorError(
            reason_code="state_latest_daily_log_missing",
            message=(
                "Refusing to treat continuity as seeded because state.json claims "
                "daily-log entries but does not name a latest daily log."
            ),
            details=daily_log_cursor_state_fields(daily_logs),
        )

    latest_path = storage_root / latest_file
    try:
        text = read_text(latest_path)
    except (OSError, UnicodeDecodeError) as exc:
        raise DailyLogCursorError(
            reason_code="unreadable_latest_daily_log",
            message=f"Could not read latest daily log named by state.json {latest_path}: {exc}",
            path=latest_path,
            details={
                "latest_file": latest_file,
                **daily_log_cursor_state_fields(daily_logs),
            },
        ) from exc

    cursor = daily_log_cursor_from_text(text, path=latest_path, latest_file=latest_file)
    if cursor.entry_count <= 0:
        raise DailyLogCursorError(
            reason_code="state_entry_bearing_daily_log_has_no_entries",
            message=(
                "Refusing to treat continuity as seeded because state.json claims "
                "daily-log entries but the latest daily log parsed as an empty scaffold."
            ),
            path=latest_path,
            details={
                "latest_file": latest_file,
                "state_cursor": daily_log_cursor_state_fields(daily_logs),
                "parsed_cursor": cursor.as_state_fields(),
            },
        )

    state_cursor = daily_log_cursor_state_fields(daily_logs)
    parsed_cursor = cursor.as_state_fields()
    if not daily_log_cursors_equivalent(state_cursor, parsed_cursor, actual_cursor=parsed_cursor):
        raise DailyLogCursorError(
            reason_code="state_daily_log_cursor_mismatch",
            message=(
                "Refusing to treat continuity as seeded because state.json daily-log "
                "cursor fields do not match the strictly parsed latest daily log."
            ),
            path=latest_path,
            details={
                "latest_file": latest_file,
                "state_cursor": state_cursor,
                "parsed_cursor": parsed_cursor,
            },
        )

    return cursor


def daily_log_cursor_state_fields(state_or_daily_logs: dict) -> dict[str, object]:
    daily_logs = state_or_daily_logs.get("daily_logs")
    if not isinstance(daily_logs, dict):
        daily_logs = state_or_daily_logs
    return {key: daily_logs.get(key) for key in DAILY_LOG_CURSOR_STATE_KEYS}


def daily_log_cursor_is_legacy_empty(cursor: dict[str, object]) -> bool:
    return (
        cursor.get("latest_file") is None
        and cursor.get("latest_entry_id") is None
        and cursor.get("latest_entry_seq") in {0, None}
        and cursor.get("entry_count") == 0
    )


def daily_log_cursor_is_empty_scaffold(cursor: dict[str, object]) -> bool:
    latest_file = cursor.get("latest_file")
    return (
        isinstance(latest_file, str)
        and bool(latest_file)
        and cursor.get("latest_entry_id") is None
        and cursor.get("latest_entry_seq") in {0, None}
        and cursor.get("entry_count") == 0
    )


def daily_log_cursor_matches_empty_scaffold(
    cursor: dict[str, object],
    *,
    scaffold_latest_file: str,
) -> bool:
    if daily_log_cursor_is_legacy_empty(cursor):
        return True
    return (
        daily_log_cursor_is_empty_scaffold(cursor)
        and cursor.get("latest_file") == scaffold_latest_file
    )


def daily_log_cursors_equivalent(
    left: dict[str, object],
    right: dict[str, object],
    *,
    actual_cursor: dict[str, object] | None = None,
) -> bool:
    if left == right:
        return True
    if actual_cursor is None or not daily_log_cursor_is_empty_scaffold(actual_cursor):
        return False
    scaffold_latest_file = actual_cursor.get("latest_file")
    if not isinstance(scaffold_latest_file, str) or not scaffold_latest_file:
        return False
    return daily_log_cursor_matches_empty_scaffold(
        left,
        scaffold_latest_file=scaffold_latest_file,
    ) and daily_log_cursor_matches_empty_scaffold(
        right,
        scaffold_latest_file=scaffold_latest_file,
    )
