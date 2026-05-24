#!/usr/bin/env python3
"""Summarize current continuity status, confidence, and workday recommendation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


_BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION = "3.10"


def _bootstrap_failure_language() -> str:
    lang = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "zh-CN" if lang.lower().startswith("zh") else "en"


def _bootstrap_minimum_python_version() -> tuple[tuple[int, ...], str]:
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
    script_name = Path(__file__).name
    recovery_command = {
        "en": f"Use a Python {minimum_text}+ interpreter to run {script_name} --json",
        "zh-CN": f"请使用 Python {minimum_text}+ 解释器运行 {script_name} --json",
    }
    suggestion = {
        "en": (
            "Repair the RecallLoom bootstrap/runtime files or switch to a compatible Python "
            f"{minimum_text}+ interpreter before retrying."
        ),
        "zh-CN": f"请先修复 RecallLoom 的 bootstrap/runtime 文件，或切换到兼容的 Python {minimum_text}+ 解释器后再重试。",
    }
    return {
        "ok": False,
        "schema_version": "1.1",
        "blocked": contract["blocked"],
        "blocked_reason": contract["blocked_reason"],
        "recoverability": contract["recoverability"],
        "surface_level": contract["surface_level"],
        "trust_effect": contract["trust_effect"],
        "failure_stage": "runtime_bootstrap",
        "next_actions": list(contract["next_actions"]),
        "user_message": contract["user_message"][language],
        "suggestion": suggestion[language],
        "recovery_command": recovery_command[language],
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
    continuity_digest_bundle,
    continuity_state_for_workspace as shared_continuity_state_for_workspace,
    evaluate_continuity_freshness,
    freshness_risk_summary,
    summary_matches_empty_shell_template as shared_summary_matches_empty_shell_template,
)
from core.continuity.workday import (
    RECOMMENDATION_TYPES,
    build_workday_decision,
    describe_workday_guidance,
    detect_closure_signal,
)
from core.trust.state import evaluate_trust_state
from core.protocol.contracts import FILE_KEYS
from core.protocol.markers import parse_file_state_marker
from core.protocol.sections import extract_section_text

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    detect_update_protocol_time_policy_cues,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue,
    exit_with_cli_error,
    find_recallloom_root,
    latest_active_daily_log,
    load_workspace_state,
    parse_daily_log_entry_line,
    parse_iso_date,
    public_project_path,
    public_project_root_label,
    read_text,
    StorageResolutionError,
)


def summary_matches_empty_shell_template(summary_text: str) -> bool:
    return shared_summary_matches_empty_shell_template(summary_text)


def continuity_state_for_workspace(
    *,
    state: dict,
    summary_text: str,
    latest_daily_log_exists: bool,
) -> tuple[str, bool]:
    return shared_continuity_state_for_workspace(
        state=state,
        summary_text=summary_text,
        latest_daily_log_exists=latest_daily_log_exists,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize continuity confidence, recommended actions, and workday guidance."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument(
        "--timezone",
        help="Optional IANA timezone such as Asia/Shanghai. Defaults to the host local timezone.",
    )
    parser.add_argument(
        "--now",
        help=(
            "Current time in ISO 8601 format. If omitted, the helper uses the host's current local time. "
            "If the value is naive, it is interpreted in --timezone or the host local timezone."
        ),
    )
    parser.add_argument(
        "--rollover-hour",
        type=int,
        default=3,
        help="Logical day rollover hour in 24-hour form. Defaults to 3.",
    )
    parser.add_argument(
        "--preferred-date",
        help="Optional explicit append target date in YYYY-MM-DD form for workday guidance.",
    )
    parser.add_argument(
        "--session-intent",
        choices=sorted(RECOMMENDATION_TYPES),
        help="Optional explicit session-intent hint using one of the recommendation types.",
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


def latest_daily_log_entry_info(latest_daily_log: Path | None):
    if latest_daily_log is None:
        return None
    latest_entry = None
    for line in read_text(latest_daily_log).splitlines():
        entry = parse_daily_log_entry_line(line)
        if entry is not None:
            latest_entry = entry
    return latest_entry


def recommended_actions_for_status(
    *,
    continuity_confidence: str,
    continuity_state: str,
    update_protocol_exists: bool,
    context_brief_exists: bool,
    latest_daily_log_exists: bool,
    summary_stale: bool,
) -> list[str]:
    actions: list[str] = []
    if continuity_state == "initialized_empty_shell":
        actions.append("seed_initial_continuity")
    elif summary_stale:
        actions.append("update_rolling_summary")
    else:
        actions.append("resume_from_summary")
    if update_protocol_exists:
        actions.append("review_update_protocol")
    if context_brief_exists:
        actions.append("review_context_brief")
    if latest_daily_log_exists:
        actions.append("review_latest_daily_log")
    if (
        continuity_state != "initialized_empty_shell"
        and continuity_confidence == "medium"
        and "update_rolling_summary" not in actions
    ):
        actions.append("consider_refresh_summary")
    return actions


def _project_relative_path(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix()


def _append_unique(items: list[str], value: str | None) -> None:
    if value and value not in items:
        items.append(value)


def _estimated_tokens_for_files(files: list[str], text_by_path: dict[str, str]) -> int:
    total = 0
    for rel_path in files:
        text = text_by_path.get(rel_path, "")
        total += max(64, (len(text) + 3) // 4) if text else 64
    return total


def _read_plan_reason(
    base: str,
    *,
    summary_stale: bool,
    update_protocol_present: bool,
) -> str:
    review_notes: list[str] = []
    if summary_stale:
        review_notes.append("rolling_summary.md is stale against state.json")
    if update_protocol_present:
        review_notes.append("update_protocol.md may narrow project-local constraints")
    if not review_notes:
        return base
    return f"{base} review-before-write: {'; '.join(review_notes)}."


def build_status_read_plan(
    *,
    project_root: Path,
    storage_root: Path,
    state: dict,
    summary_path: Path,
    summary_text: str,
    context_brief_path: Path,
    context_brief_text: str,
    update_protocol_path: Path,
    update_protocol_text: str,
    latest_daily_log: Path | None,
    latest_daily_log_text: str,
    summary_stale: bool,
    continuity_state: str,
) -> dict:
    state_rel = _project_relative_path(storage_root / FILE_KEYS["state"], project_root)
    summary_rel = _project_relative_path(summary_path, project_root)
    context_rel = (
        _project_relative_path(context_brief_path, project_root) if context_brief_path.is_file() else None
    )
    update_protocol_rel = (
        _project_relative_path(update_protocol_path, project_root) if update_protocol_path.is_file() else None
    )
    latest_daily_log_rel = (
        _project_relative_path(latest_daily_log, project_root) if latest_daily_log is not None else None
    )

    text_by_path = {
        state_rel: json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        summary_rel: summary_text,
    }
    if context_rel:
        text_by_path[context_rel] = context_brief_text
    if update_protocol_rel:
        text_by_path[update_protocol_rel] = update_protocol_text
    if latest_daily_log_rel:
        text_by_path[latest_daily_log_rel] = latest_daily_log_text

    minimal_files = [summary_rel, state_rel]
    _append_unique(minimal_files, update_protocol_rel)

    standard_files = list(minimal_files)
    _append_unique(standard_files, context_rel)

    comprehensive_files = list(standard_files)
    if continuity_state != "initialized_empty_shell":
        _append_unique(comprehensive_files, latest_daily_log_rel)

    read_plan = {
        "minimal": {
            "files": minimal_files,
            "reason": _read_plan_reason(
                "Smallest bounded continuity set for current-state orientation.",
                summary_stale=summary_stale,
                update_protocol_present=bool(update_protocol_rel),
            ),
            "estimated_tokens": _estimated_tokens_for_files(minimal_files, text_by_path),
        },
        "standard": {
            "files": standard_files,
            "reason": _read_plan_reason(
                "Default balanced continuity read that adds stable framing when available.",
                summary_stale=summary_stale,
                update_protocol_present=bool(update_protocol_rel),
            ),
            "estimated_tokens": _estimated_tokens_for_files(standard_files, text_by_path),
        },
        "comprehensive": {
            "files": comprehensive_files,
            "reason": _read_plan_reason(
                "Highest-confidence continuity read for evidence-heavy follow-up.",
                summary_stale=summary_stale,
                update_protocol_present=bool(update_protocol_rel),
            ),
            "estimated_tokens": _estimated_tokens_for_files(comprehensive_files, text_by_path),
        },
    }
    return read_plan


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
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message="Invalid --rollover-hour value.",
            payload=cli_failure_payload("invalid_prepared_input", error="Invalid --rollover-hour value."),
        )

    preferred_date = None
    if args.preferred_date:
        try:
            preferred_date = parse_iso_date(args.preferred_date)
        except ValueError:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Invalid --preferred-date value: {args.preferred_date}",
                payload=cli_failure_payload(
                    "invalid_date",
                    error=f"Invalid --preferred-date value: {args.preferred_date}",
                    extra={"continuity_confidence": "broken"},
                ),
            )

    try:
        now, zone_label = resolve_now(args.now, args.timezone)
    except ValueError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload("invalid_prepared_input", error=str(exc)),
        )

    try:
        workspace = find_recallloom_root(args.path)
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"continuity_confidence": "broken"},
            ),
        )
    if workspace is None:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload(
                "no_project_root",
                error="No RecallLoom project root found.",
                details={"project_root": str(Path(args.path).expanduser().resolve())},
                extra={"continuity_confidence": "broken"},
            ),
        )
    startup_residue_report = exit_if_startup_scratch_residue(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )

    try:
        summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
        if not summary_path.is_file():
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file: {summary_path}",
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=f"Missing required file: {summary_path}",
                    extra={"continuity_confidence": "broken"},
                ),
            )
        summary_state = parse_file_state_marker(read_text(summary_path))
        if summary_state is None:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file-state metadata marker: {summary_path}",
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=f"Missing required file-state metadata marker: {summary_path}",
                    extra={"continuity_confidence": "broken"},
                ),
            )

        try:
            state = load_workspace_state(workspace.storage_root / FILE_KEYS["state"])
        except ConfigContractError as exc:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=str(exc),
                payload=cli_failure_payload_for_exception(
                    exc,
                    default_reason="damaged_sidecar",
                    extra={"continuity_confidence": "broken"},
                ),
            )

        context_brief_path = workspace.storage_root / FILE_KEYS["context_brief"]
        update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
        context_brief_text = read_text(context_brief_path) if context_brief_path.is_file() else ""
        context_brief_state = None
        if context_brief_path.is_file():
            context_brief_state = parse_file_state_marker(context_brief_text)
            if context_brief_state is None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Missing required file-state metadata marker: {context_brief_path}",
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=f"Missing required file-state metadata marker: {context_brief_path}",
                        extra={"continuity_confidence": "broken"},
                    ),
                )
        update_protocol_text = read_text(update_protocol_path) if update_protocol_path.is_file() else ""
        update_protocol_state = None
        if update_protocol_path.is_file():
            update_protocol_state = parse_file_state_marker(update_protocol_text)
            if update_protocol_state is None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Missing required file-state metadata marker: {update_protocol_path}",
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=f"Missing required file-state metadata marker: {update_protocol_path}",
                        extra={"continuity_confidence": "broken"},
                    ),
                )

        summary_text = read_text(summary_path)
        latest_daily_log = latest_active_daily_log(workspace.storage_root / DAILY_LOGS_DIRNAME)
        latest_daily_log_entry = latest_daily_log_entry_info(latest_daily_log)
        latest_daily_log_text = read_text(latest_daily_log) if latest_daily_log is not None else ""
        if latest_daily_log is not None and latest_daily_log_entry is None:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=(
                    "Missing required daily-log-entry metadata marker in the latest ISO-dated daily log: "
                    f"{latest_daily_log}"
                ),
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=(
                        "Missing required daily-log-entry metadata marker in the latest ISO-dated daily log: "
                        f"{latest_daily_log}"
                    ),
                    extra={"continuity_confidence": "broken"},
                ),
            )

        freshness = evaluate_continuity_freshness(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            summary_path=summary_path,
            workspace_revision=state["workspace_revision"],
            summary_base_workspace_revision=summary_state.base_workspace_revision,
            latest_daily_log_exists=latest_daily_log is not None,
            scan_mode="quick",
        )
        digests = continuity_digest_bundle(
            summary_text=summary_text,
            latest_daily_log_text=latest_daily_log_text,
            project_root=workspace.project_root,
        )
        continuity_state, continuity_seeded = continuity_state_for_workspace(
            state=state,
            summary_text=summary_text,
            latest_daily_log_exists=latest_daily_log is not None,
        )
        if continuity_state == "initialized_empty_shell":
            digests = {
                "active_task_digest": None,
                "blocked_digest": None,
                "latest_relevant_log_digest": None,
                "suggested_handoff_sections": [],
            }

        summary_stale = freshness["summary_stale"]
        confidence = freshness["continuity_confidence"]
        freshness_risk = freshness_risk_summary(
            workspace_artifact_scan_mode=freshness["workspace_artifact_scan_mode"],
            workspace_artifact_scan_performed=freshness["workspace_artifact_scan_performed"],
            workspace_artifact_newer_than_summary=freshness["workspace_artifact_newer_than_summary"],
            summary_revision_stale=freshness["summary_revision_stale"],
            continuity_confidence=confidence,
        )
        actions = recommended_actions_for_status(
            continuity_confidence=confidence,
            continuity_state=continuity_state,
            update_protocol_exists=update_protocol_path.is_file(),
            context_brief_exists=context_brief_path.is_file(),
            latest_daily_log_exists=latest_daily_log is not None,
            summary_stale=summary_stale,
        )

        latest_active_day = parse_iso_date(latest_daily_log.stem) if latest_daily_log is not None else None
        closure_detected, closure_keywords = detect_closure_signal(latest_daily_log_text)
        project_time_policy_cues = detect_update_protocol_time_policy_cues(update_protocol_text)
        workday = build_workday_decision(
            now=now,
            rollover_hour=args.rollover_hour,
            latest_active_day=latest_active_day,
            closure_detected=closure_detected,
            summary_next_step_is_empty=(
                continuity_state == "initialized_empty_shell"
                or digests["active_task_digest"] is None
            ),
            preferred_date=preferred_date,
            session_intent=args.session_intent,
            project_time_policy_cues=project_time_policy_cues,
            host_explicit=args.timezone is not None or args.rollover_hour != 3,
        )
        workday.update(
            {
                "closure_keywords": closure_keywords,
                "project_time_policy_present": bool(update_protocol_text.strip()),
                "project_time_policy_cues": project_time_policy_cues,
                "project_time_policy_review_required": bool(
                    project_time_policy_cues
                    and (latest_active_day is None or latest_active_day.isoformat() != workday["logical_workday"])
                ),
                "preferred_date": args.preferred_date,
            }
        )
        trust_state = evaluate_trust_state(
            continuity_confidence=confidence,
            continuity_state=continuity_state,
            summary_stale=summary_stale,
            workspace_newer_than_summary=freshness["workspace_newer_than_summary"],
            conflict_state=None,
        )
        read_plan = build_status_read_plan(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            state=state,
            summary_path=summary_path,
            summary_text=summary_text,
            context_brief_path=context_brief_path,
            context_brief_text=context_brief_text,
            update_protocol_path=update_protocol_path,
            update_protocol_text=update_protocol_text,
            latest_daily_log=latest_daily_log,
            latest_daily_log_text=latest_daily_log_text,
            summary_stale=summary_stale,
            continuity_state=continuity_state,
        )
        public_project_root = public_project_root_label(workspace.project_root)
        public_storage_root = public_project_path(workspace.storage_root, project_root=workspace.project_root)
        public_latest_workspace_artifact = (
            public_project_path(freshness["latest_workspace_artifact"], project_root=workspace.project_root)
            if freshness["latest_workspace_artifact"] is not None
            else None
        )
        public_latest_daily_log = (
            public_project_path(latest_daily_log, project_root=workspace.project_root)
            if latest_daily_log is not None
            else None
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Filesystem error: {exc}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "damaged_sidecar",
                error=message,
                extra={"continuity_confidence": "broken"},
            ),
        )

    payload = {
        "schema_version": "1.1",
        "project_root": public_project_root,
        "storage_root": public_storage_root,
        "timezone": zone_label,
        "now": now.isoformat(),
        "workspace_revision": state["workspace_revision"],
        "rolling_summary_revision": summary_state.revision,
        "workspace_artifact_scan_mode": freshness["workspace_artifact_scan_mode"],
        "workspace_artifact_scan_performed": freshness["workspace_artifact_scan_performed"],
        "latest_workspace_artifact": public_latest_workspace_artifact,
        "workspace_artifact_newer_than_summary": freshness["workspace_artifact_newer_than_summary"],
        "summary_revision_stale": freshness["summary_revision_stale"],
        "workspace_newer_than_summary": freshness["workspace_newer_than_summary"],
        "summary_stale": summary_stale,
        "continuity_confidence": confidence,
        "sidecar_trust_state": trust_state["sidecar_trust_state"],
        "allowed_operation_level": trust_state["allowed_operation_level"],
        "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
        "freshness_risk_level": freshness_risk["level"],
        "freshness_risk_note": freshness_risk["note"],
        "continuity_state": continuity_state,
        "continuity_seeded": continuity_seeded,
        "active_task_digest": digests["active_task_digest"],
        "blocked_digest": digests["blocked_digest"],
        "latest_relevant_log_digest": digests["latest_relevant_log_digest"],
        "suggested_handoff_sections": digests["suggested_handoff_sections"],
        "recommended_actions": actions,
        "read_plan": read_plan,
        "estimated_tokens": read_plan["standard"]["estimated_tokens"],
        "latest_active_daily_log": public_latest_daily_log,
        "continuity_snapshot": {
            "project_root": public_project_root,
            "storage_root": public_storage_root,
            "workspace_revision_seen": state["workspace_revision"],
            "rolling_summary_revision_seen": summary_state.revision,
            "context_brief_revision_seen": context_brief_state.revision if context_brief_state else None,
            "update_protocol_revision_seen": update_protocol_state.revision if update_protocol_state else None,
            "latest_active_daily_log_seen": public_latest_daily_log,
            "latest_active_daily_log_entry_seq_seen": (
                latest_daily_log_entry.entry_seq if latest_daily_log_entry is not None else None
            ),
            "logical_workday_seen": workday["logical_workday"],
            "continuity_confidence": confidence,
            "continuity_state": continuity_state,
            "continuity_seeded": continuity_seeded,
            "task_type": "status_review",
        },
        "workday": workday,
    }

    if args.json:
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"RecallLoom root: {workspace.project_root}")
        print(f"Continuity confidence: {confidence}")
        if freshness_risk["note"]:
            print(f"Freshness risk: {freshness_risk['level']} - {freshness_risk['note']}")
        print(f"Continuity state: {continuity_state}")
        print("Recommended actions:")
        for action in actions:
            print(f"  - {action}")
        guidance = describe_workday_guidance(workday, always_show=False)
        if guidance:
            print(f"Workday guidance: {guidance}")


if __name__ == "__main__":
    main()
