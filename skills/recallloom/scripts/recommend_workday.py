#!/usr/bin/env python3
"""Recommend the current logical workday and append target date for RecallLoom."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime


_BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION = "3.10"


def _bootstrap_failure_language() -> str:
    lang = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "zh-CN" if lang.lower().startswith("zh") else "en"


def _bootstrap_minimum_python_version() -> tuple[tuple[int, ...], str]:
    from pathlib import Path

    fallback_parts = tuple(int(part) for part in _BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION.split("."))
    metadata_path = Path(__file__).resolve().parent.parent / "package-metadata.json"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raw = _BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION
    else:
        raw = payload.get("minimum_python_version", _BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION)
        if not isinstance(raw, str) or not raw.strip():
            raw = _BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION
    parts = raw.strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return fallback_parts, _BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION
    normalized = tuple(int(part) for part in parts)
    return normalized, ".".join(str(part) for part in normalized)


def _bootstrap_runtime_contract(minimum_text: str) -> dict:
    return {
        "blocked": True,
        "blocked_reason": "python_runtime_unavailable",
        "recoverability": "retryable",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["find_compatible_python", "report_blocked_runtime"],
        "user_message": {
            "en": (
                "RecallLoom cannot start yet because this environment does not provide "
                f"Python {minimum_text} or newer."
            ),
            "zh-CN": f"当前环境还不能启动 RecallLoom，因为这里没有可用的 Python {minimum_text}+ 运行时。",
        },
        "operator_note": {
            "en": f"Find or point the host at a compatible Python {minimum_text}+ interpreter before retrying.",
            "zh-CN": f"请先找到或指定兼容的 Python {minimum_text}+ 解释器，再重试。",
        },
    }


def _bootstrap_runtime_payload(message: str, minimum_text: str) -> dict:
    language = _bootstrap_failure_language()
    contract = _bootstrap_runtime_contract(minimum_text)
    return {
        "ok": False,
        "blocked": contract["blocked"],
        "blocked_reason": contract["blocked_reason"],
        "recoverability": contract["recoverability"],
        "surface_level": contract["surface_level"],
        "trust_effect": contract["trust_effect"],
        "next_actions": list(contract["next_actions"]),
        "user_message": contract["user_message"][language],
        "operator_note": contract["operator_note"][language],
        "error": message,
    }


def _exit_if_runtime_unsupported() -> None:
    minimum_parts, minimum_text = _bootstrap_minimum_python_version()
    current = sys.version_info[: len(minimum_parts)]
    if current >= minimum_parts:
        return
    message = (
        "RecallLoom helper scripts require "
        f"Python {minimum_text}+; current interpreter is "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if "--json" in sys.argv[1:]:
        print(json.dumps(_bootstrap_runtime_payload(message, minimum_text), ensure_ascii=False, indent=2))
    else:
        print(message, file=sys.stderr)
    raise SystemExit(2)


_exit_if_runtime_unsupported()

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.continuity.freshness import (
    is_effectively_empty_summary_next_step as shared_is_effectively_empty_summary_next_step,
)
from core.continuity.workday import (
    DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
    RECOMMENDATION_TYPES,
    build_workday_decision,
    describe_workday_guidance,
    detect_closure_signal,
)

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    detect_update_protocol_time_policy_cues,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_with_cli_error,
    exit_with_failure_contract,
    extract_section_text,
    FILE_KEYS,
    find_recallloom_root,
    invalid_iso_like_daily_log_files,
    latest_active_daily_log,
    load_workspace_state,
    parse_daily_log_entry_line,
    parse_file_state_marker,
    parse_iso_date,
    public_json_payload,
    read_text,
    StorageResolutionError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recommend the logical workday and append target date for a RecallLoom workspace."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument(
        "--now",
        help=(
            "Current time in ISO 8601 format. If omitted, the helper uses the host's current local time. "
            "If the value is naive, it is interpreted in --timezone or the host local timezone."
        ),
    )
    parser.add_argument(
        "--timezone",
        help="Optional IANA timezone such as Asia/Shanghai. Defaults to the host local timezone.",
    )
    parser.add_argument(
        "--rollover-hour",
        type=int,
        default=DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
        help="Logical day rollover hour in 24-hour form. Defaults to 3.",
    )
    parser.add_argument(
        "--preferred-date",
        help=(
            "Optional explicit append target date in YYYY-MM-DD form. "
            "When provided, this date takes priority over the helper's default suggestion. "
            "If it disagrees with the heuristic result, the helper returns "
            "`review_date_before_append`."
        ),
    )
    parser.add_argument(
        "--session-intent",
        choices=sorted(RECOMMENDATION_TYPES),
        help=(
            "Optional explicit session-intent hint using one of the recommendation types. "
            "When provided, this takes priority over heuristic recommendation selection."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def resolve_now(now_raw: str | None, timezone_name: str | None) -> tuple[datetime, str]:
    tzinfo = None
    if timezone_name:
        try:
            tzinfo = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone_name}") from exc

    if now_raw:
        try:
            now = datetime.fromisoformat(now_raw)
        except ValueError as exc:
            raise ValueError(f"Invalid --now value: {now_raw}") from exc
        if now.tzinfo is None:
            if tzinfo is None:
                tzinfo = datetime.now().astimezone().tzinfo
            now = now.replace(tzinfo=tzinfo)
        elif tzinfo is not None:
            now = now.astimezone(tzinfo)
    else:
        now = datetime.now().astimezone()
        if tzinfo is not None:
            now = now.astimezone(tzinfo)

    zone_label = timezone_name or str(now.tzinfo)
    return now, zone_label


def is_effectively_empty_summary_next_step(text: str) -> bool:
    return shared_is_effectively_empty_summary_next_step(text)


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

    if not 0 <= args.rollover_hour <= 23:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Invalid --rollover-hour value: {args.rollover_hour}. Expected 0..23.",
            reason="invalid_prepared_input",
        )
    preferred_date = None
    if args.preferred_date:
        try:
            preferred_date = parse_iso_date(args.preferred_date)
        except ValueError:
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Invalid --preferred-date value: {args.preferred_date}",
                reason="invalid_date",
                details={"preferred_date": args.preferred_date},
            )

    try:
        now, zone_label = resolve_now(args.now, args.timezone)
    except ValueError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            reason="invalid_prepared_input",
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

    try:
        logs_dir = workspace.storage_root / DAILY_LOGS_DIRNAME
        invalid_daily_logs = invalid_iso_like_daily_log_files(logs_dir)
        if invalid_daily_logs:
            invalid_paths = [str(path) for path in invalid_daily_logs]
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=(
                    "Refusing recommend_workday because one or more daily log filenames match the date pattern but are invalid ISO dates:\n"
                    + "\n".join(invalid_paths)
                ),
                reason="malformed_managed_file",
                details={"invalid_paths": invalid_paths},
            )
        load_workspace_state(workspace.storage_root / FILE_KEYS["state"])

        latest_daily_log = latest_active_daily_log(logs_dir)
        latest_active_day = parse_iso_date(latest_daily_log.stem) if latest_daily_log is not None else None
        update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
        if not update_protocol_path.is_file():
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file: {update_protocol_path}",
                reason="malformed_managed_file",
                details={"path": str(update_protocol_path)},
            )
        update_protocol_text = read_text(update_protocol_path)
        if parse_file_state_marker(update_protocol_text) is None:
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file-state metadata marker: {update_protocol_path}",
                reason="malformed_managed_file",
                details={"path": str(update_protocol_path)},
            )
        project_time_policy_cues = detect_update_protocol_time_policy_cues(update_protocol_text)

        summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
        if not summary_path.is_file():
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file: {summary_path}",
                reason="malformed_managed_file",
                details={"path": str(summary_path)},
            )
        summary_text = read_text(summary_path)
        if parse_file_state_marker(summary_text) is None:
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file-state metadata marker: {summary_path}",
                reason="malformed_managed_file",
                details={"path": str(summary_path)},
            )
        next_step_text = extract_section_text(summary_text, "next_step")
        next_step_empty = is_effectively_empty_summary_next_step(next_step_text)

        daily_log_text = read_text(latest_daily_log) if latest_daily_log is not None else ""
        if latest_daily_log is not None and not any(
            parse_daily_log_entry_line(line) is not None
            for line in daily_log_text.splitlines()
        ):
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=(
                    "Missing required daily-log-entry metadata marker in the latest ISO-dated daily log: "
                    f"{latest_daily_log}"
                ),
                reason="malformed_managed_file",
                details={"path": str(latest_daily_log)},
            )
        closure_detected, closure_keywords = detect_closure_signal(daily_log_text)
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Filesystem error: {exc}"
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            reason="damaged_sidecar",
        )
    except ConfigContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )

    decision = build_workday_decision(
        now=now,
        rollover_hour=args.rollover_hour,
        latest_active_day=latest_active_day,
        closure_detected=closure_detected,
        summary_next_step_is_empty=next_step_empty,
        preferred_date=preferred_date,
        session_intent=args.session_intent,
        project_time_policy_cues=project_time_policy_cues,
        host_explicit=(
            args.timezone is not None
            or args.rollover_hour != DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR
        ),
    )

    recommendation = decision["recommendation_type"]
    if recommendation not in RECOMMENDATION_TYPES:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Internal error: unsupported recommendation type '{recommendation}'.",
            reason="damaged_sidecar",
        )

    payload = {
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "timezone": zone_label,
        "now": now.isoformat(),
        "physical_date": decision["physical_date"],
        "rollover_hour": args.rollover_hour,
        "logical_workday": decision["logical_workday"],
        "latest_active_daily_log": str(latest_daily_log) if latest_daily_log else None,
        "latest_active_day": decision["latest_active_day"],
        "gap_days": decision["gap_days"],
        "summary_next_step_empty": next_step_empty,
        "summary_next_step_excerpt": None if next_step_empty else next_step_text,
        "closure_detected": closure_detected,
        "closure_keywords": closure_keywords,
        "session_intent": args.session_intent,
        "preferred_date": preferred_date.isoformat() if preferred_date is not None else None,
        "date_resolution_source": decision["date_resolution_source"],
        "decision_priority_applied": decision["decision_priority_applied"],
        "project_time_policy_present": update_protocol_path.is_file(),
        "project_time_policy_cues": project_time_policy_cues,
        "project_time_policy_review_required": bool(
            project_time_policy_cues
            and (latest_active_day is None or latest_active_day.isoformat() != decision["logical_workday"])
        ),
        "workday_state": decision["workday_state"],
        "heuristic_workday_state": decision["heuristic_workday_state"],
        "heuristic_recommendation_type": decision["heuristic_recommendation_type"],
        "heuristic_suggested_date": decision["heuristic_suggested_date"],
        "recommendation_type": recommendation,
        "suggested_date": decision["suggested_date"],
        "requires_user_confirmation": decision["requires_user_confirmation"],
        "user_visible_prompt_level": decision["user_visible_prompt_level"],
        "reasoning": decision["reasoning"],
    }

    if args.json:
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(f"RecallLoom root: {workspace.project_root}")
    print(f"Timezone: {zone_label}")
    print(f"Current time: {now.isoformat()}")
    print(f"Logical workday: {decision['logical_workday']}")
    print(f"Latest active daily log: {latest_daily_log if latest_daily_log else 'none'}")
    if preferred_date is not None:
        print(f"Preferred date: {preferred_date.isoformat()}")
    print(f"Date resolution source: {decision['date_resolution_source']}")
    guidance = describe_workday_guidance(decision, always_show=True)
    if guidance:
        print(f"Workday guidance: {guidance}")
    if decision["reasoning"]:
        print("Why:")
        for item in decision["reasoning"]:
            print(f"  - {item}")


if __name__ == "__main__":
    main()
