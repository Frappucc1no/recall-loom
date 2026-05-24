#!/usr/bin/env python3
"""Unified operator-friendly entrypoint for RecallLoom helper workflows."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
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

from core.continuity.quick_summary import build_no_project_payload, build_quick_summary_payload
from core.continuity.workday import RECOMMENDATION_TYPES, describe_workday_guidance
from core.failure.contracts import failure_payload, preferred_failure_language
from core.protocol.contracts import FILE_KEYS, ROOT_ENTRY_CANDIDATES
from core.protocol.markers import parse_file_state_marker
from core.support.policy import action_level_for_dispatcher
from core.trust.state import evaluate_trust_state

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_with_cli_error,
    find_recallloom_root,
    load_workspace_state,
    normalize_safe_writer_id,
    normalize_wrapper_metadata_json,
    normalize_start_path,
    public_package_support_payload,
    public_project_path,
    public_project_root_label,
    read_text,
    StorageResolutionError,
    startup_scratch_residue_report,
    WrapperMetadataSecurityError,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SUPPORTED_BRIDGE_TARGETS = [path.as_posix() for path in ROOT_ENTRY_CANDIDATES]


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
        description=(
            "Unified RecallLoom command entry for init, resume, validate, status, "
            "quick-summary, append, write, post-append summary sync, and bridge flows."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a RecallLoom workspace, validate it, and return next-step guidance.",
    )
    init_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project root directory to initialize. Defaults to the current working directory.",
    )
    init_parser.add_argument(
        "--tool-name",
        default="RecallLoom",
        help="Tool name used in generated metadata such as the rolling summary marker.",
    )
    init_parser.add_argument(
        "--date",
        help="Date to use for generated metadata and optional daily log file.",
    )
    init_parser.add_argument(
        "--storage-mode",
        choices=["hidden", "visible"],
        help="Storage layout mode. Defaults to hidden sidecar mode.",
    )
    init_parser.add_argument(
        "--workspace-language",
        choices=["en", "zh-CN"],
        help="Language used for generated workspace files.",
    )
    init_parser.add_argument(
        "--create-daily-log",
        action="store_true",
        help="Optionally create today's daily log scaffold during initialization.",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Apply first-time initialization writes even if a managed file path already exists.",
    )
    init_parser.add_argument(
        "--skip-git-exclude",
        action="store_true",
        help="Do not add .recallloom/ to .git/info/exclude when using hidden mode in a git repo.",
    )
    init_parser.add_argument(
        "--bridge",
        choices=SUPPORTED_BRIDGE_TARGETS,
        help="Optionally apply a thin bridge to one supported root entry file after successful init+validate.",
    )
    init_parser.add_argument(
        "--yes",
        action="store_true",
        help="Required together with --bridge to apply the bridge instead of only suggesting it.",
    )
    init_parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a RecallLoom workspace and managed file contracts.",
    )
    validate_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    validate_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    resume_parser = subparsers.add_parser(
        "resume",
        help="Run the RecallLoom fast-path resume checkpoint for the current project.",
    )
    resume_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    resume_parser.add_argument(
        "--timezone",
        help="Optional IANA timezone such as Asia/Shanghai. Defaults to the host local timezone.",
    )
    resume_parser.add_argument(
        "--now",
        help="Current time in ISO 8601 format.",
    )
    resume_parser.add_argument(
        "--rollover-hour",
        type=int,
        default=3,
        help="Logical day rollover hour in 24-hour form. Defaults to 3.",
    )
    resume_parser.add_argument(
        "--preferred-date",
        help="Optional explicit append target date in YYYY-MM-DD form for workday guidance.",
    )
    resume_parser.add_argument(
        "--session-intent",
        choices=sorted(RECOMMENDATION_TYPES),
        help="Optional explicit session-intent hint using one of the recommendation types.",
    )
    resume_mode = resume_parser.add_mutually_exclusive_group()
    resume_mode.add_argument(
        "--fast",
        action="store_true",
        help="Return the bounded progressive resume surface from state.json and rolling_summary.md.",
    )
    resume_mode.add_argument(
        "--full",
        action="store_true",
        help="Return the bounded progressive resume surface plus context and update-protocol guidance.",
    )
    resume_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    status_parser = subparsers.add_parser(
        "status",
        help="Summarize current continuity status, confidence, and workday recommendation.",
    )
    status_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    status_parser.add_argument(
        "--timezone",
        help="Optional IANA timezone such as Asia/Shanghai. Defaults to the host local timezone.",
    )
    status_parser.add_argument(
        "--now",
        help="Current time in ISO 8601 format.",
    )
    status_parser.add_argument(
        "--rollover-hour",
        type=int,
        default=3,
        help="Logical day rollover hour in 24-hour form. Defaults to 3.",
    )
    status_parser.add_argument(
        "--preferred-date",
        help="Optional explicit append target date in YYYY-MM-DD form for workday guidance.",
    )
    status_parser.add_argument(
        "--session-intent",
        choices=sorted(RECOMMENDATION_TYPES),
        help="Optional explicit session-intent hint using one of the recommendation types.",
    )
    status_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    quick_summary_parser = subparsers.add_parser(
        "quick-summary",
        help="Return a low-latency continuity snapshot from state.json and rolling_summary.md.",
    )
    quick_summary_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    quick_summary_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    append_parser = subparsers.add_parser(
        "append",
        help="Append a prepared daily-log entry through append_daily_log_entry.py.",
    )
    append_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    append_parser.add_argument("--date", help="Daily log date in YYYY-MM-DD.")
    append_parser.add_argument(
        "--expected-workspace-revision",
        type=int,
        help="Expected workspace revision for the append guard.",
    )
    append_parser.add_argument(
        "--entry-file",
        help="Path to prepared entry content.",
    )
    append_parser.add_argument(
        "--entry-json",
        help="Prepared entry JSON object as a string.",
    )
    append_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared entry content from UTF-8 stdin instead of a file.",
    )
    append_parser.add_argument(
        "--input-format",
        choices=("auto", "markdown", "json"),
        help="Interpret prepared entry input as markdown or JSON.",
    )
    append_parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        help="Maximum prepared-entry input size in bytes forwarded to append_daily_log_entry.py.",
    )
    append_parser.add_argument(
        "--allow-historical",
        action="store_true",
        help="Allow appending to a non-latest ISO-dated daily log.",
    )
    append_parser.add_argument(
        "--no-auto-detect",
        action="store_true",
        help="Require explicit --date and --expected-workspace-revision instead of helper auto-detect.",
    )
    append_parser.add_argument(
        "--writer-id",
        help="Override the writer ID for appended daily-log entries.",
    )
    append_parser.add_argument(
        "--wrapper-metadata-json",
        help=(
            "Optional wrapper metadata JSON object for additive public output. "
            "Only public-safe host/surface keys and version-like local_wrapper_version values are accepted."
        ),
    )
    append_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    write_parser = subparsers.add_parser(
        "write",
        help="Write a prepared managed continuity file through commit_context_file.py.",
    )
    write_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    write_parser.add_argument(
        "--type",
        dest="write_type",
        help="Prepared file target: current-state, stable-context, or protocol-rules.",
    )
    write_parser.add_argument(
        "--source-file",
        help="Path to prepared managed-file markdown content.",
    )
    write_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared managed-file markdown content from UTF-8 stdin instead of a file.",
    )
    write_parser.add_argument(
        "--input-format",
        choices=("markdown", "json"),
        default="markdown",
        help=(
            "Interpret prepared managed-file input as markdown or rolling-summary JSON. "
            "JSON input is only supported with --type current-state."
        ),
    )
    write_parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        help="Maximum prepared-content input size in bytes forwarded to commit_context_file.py.",
    )
    write_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run preflight and report the target/revisions without writing sidecar state or files.",
    )
    write_parser.add_argument(
        "--writer-id",
        help="Override the writer ID used by commit_context_file.py.",
    )
    write_parser.add_argument(
        "--wrapper-metadata-json",
        help=(
            "Optional wrapper metadata JSON object for additive public output. "
            "Only public-safe host/surface keys and version-like local_wrapper_version values are accepted."
        ),
    )
    write_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    post_append_sync_parser = subparsers.add_parser(
        "sync-current-state-after-append",
        help=(
            "Consume the post_append_summary_sync preflight contract and sync "
            "rolling_summary.md from reviewed JSON after an append."
        ),
    )
    post_append_sync_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    post_append_sync_parser.add_argument(
        "--source-file",
        dest="source_file",
        help=argparse.SUPPRESS,
    )
    post_append_sync_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read reviewed rolling-summary JSON from UTF-8 stdin.",
    )
    post_append_sync_parser.add_argument(
        "--input-format",
        choices=("json",),
        default="json",
        help="Input format for this command. Only rolling-summary JSON is supported.",
    )
    post_append_sync_parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        help="Maximum reviewed-summary input size in bytes forwarded to commit_context_file.py.",
    )
    post_append_sync_parser.add_argument(
        "--writer-id",
        help="Override the writer ID used by commit_context_file.py.",
    )
    post_append_sync_parser.add_argument(
        "--wrapper-metadata-json",
        help=(
            "Optional wrapper metadata JSON object for additive public output. "
            "Only public-safe host/surface keys and version-like local_wrapper_version values are accepted."
        ),
    )
    post_append_sync_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    bridge_parser = subparsers.add_parser(
        "bridge",
        help="Preview, apply, or remove a RecallLoom thin bridge in one supported root entry file.",
    )
    bridge_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    bridge_parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="Specific project-root-relative entry file to bridge. At most one target per invocation.",
    )
    bridge_parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove RecallLoom managed bridge blocks instead of adding or updating them.",
    )
    bridge_parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply the change. Without this flag, the command runs in preview mode.",
    )
    bridge_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    return parser


def _preferred_language() -> str:
    return preferred_failure_language(os.environ)


def _contract_payload(reason: str, *, error: str | None = None) -> dict:
    return failure_payload(reason, language=_preferred_language(), error=error)


def _with_package_support(payload: dict | None, support: dict | None) -> dict | None:
    if payload is None or support is None:
        return payload
    return {**payload, "package_support": public_package_support_payload(support)}


def _exit_with_support(
    parser,
    *,
    json_mode: bool,
    exit_code: int,
    message: str,
    payload: dict | None = None,
    support: dict | None = None,
) -> None:
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        payload=_with_package_support(payload, support),
    )


def _exit_if_startup_scratch_residue_with_support(
    parser,
    *,
    json_mode: bool,
    project_root: Path,
    storage_root: Path,
    support: dict,
) -> dict | None:
    report = startup_scratch_residue_report(
        project_root,
        storage_root,
    )
    if report.blocked:
        public_report = report.public_dict()
        message = "RecallLoom startup scratch residue detected; no files were changed."
        _exit_with_support(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "startup_residue_detected",
                error=message,
                details={
                    "project_root": str(project_root),
                    "startup_residue_report": public_report,
                },
                findings=public_report["findings"],
                extra={"startup_residue_report": public_report},
            ),
            support=support,
        )
    return report.report_only_public_dict()


def _infer_helper_failure_reason(helper_name: str, message: str) -> str | None:
    lowered = message.lower()
    if (
        (
            "recallloom helper scripts require python " in lowered
            and "current interpreter is " in lowered
        )
        or "runtime bootstrap failed" in message
        or "minimum_python_version" in message
    ):
        return "python_runtime_unavailable"
    if helper_name == "manage_entry_bridge.py" and "attached-text safety scan" in message:
        return "attach_scan_blocked"
    if "No RecallLoom project root found." in message:
        return "no_project_root"
    if (
        "does not look like a project root" in lowered
        or "target path does not exist" in lowered
        or "target path is not a directory" in lowered
    ):
        return "not_project_root"
    if (
        "conflicting recallloom storage roots" in lowered
        or "different storage mode" in lowered
        or "conflicting recallloom sidecar" in lowered
        or "conflicting or partial recallloom sidecar" in lowered
    ):
        return "dual_sidecar_conflict"
    if "rerun preflight before writing" in lowered or "rerun preflight before appending" in lowered:
        return "stale_write_context"
    if "allow-historical" in lowered or "non-latest daily log" in lowered:
        return "historical_append_requires_confirmation"
    if (
        "missing required file" in lowered
        or "missing required file-state" in lowered
        or "invalid state.json" in lowered
        or "missing required section markers" in lowered
        or "duplicate section markers" in lowered
        or "unknown section markers" in lowered
        or any(token in lowered for token in ("partial", "damaged", "symlink", "non-directory"))
    ):
        return "malformed_managed_file" if "section markers" in lowered else "damaged_sidecar"
    return None


def _normalize_helper_error(helper_name: str, payload: dict) -> dict:
    normalized = dict(payload)
    if normalized.get("blocked_reason"):
        return normalized
    message = normalized.get("error")
    if not isinstance(message, str):
        return normalized
    reason = _infer_helper_failure_reason(helper_name, message)
    if reason is None:
        return normalized
    normalized.update(_contract_payload(reason, error=message))
    return normalized


def _run_helper_json(
    parser,
    *,
    helper_name: str,
    helper_args: list[str],
    json_mode_on_failure: bool,
    support: dict | None = None,
    package_support_on_failure: bool = False,
) -> dict:
    cmd = [sys.executable, str(SCRIPT_DIR / helper_name), *helper_args, "--json"]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        parsed_error = None
        if proc.stdout:
            try:
                parsed_error = json.loads(proc.stdout)
            except json.JSONDecodeError:
                parsed_error = None
        if isinstance(parsed_error, dict):
            parsed_error = _normalize_helper_error(helper_name, parsed_error)
        if json_mode_on_failure:
            if parsed_error is not None:
                failure_payload = (
                    _with_package_support(parsed_error, support)
                    if package_support_on_failure
                    else parsed_error
                )
                print(
                    json.dumps(
                        failure_payload,
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                raise SystemExit(proc.returncode)
            if proc.stdout:
                print(proc.stdout, end="")
                raise SystemExit(proc.returncode)
            message = proc.stderr.strip() or f"{helper_name} failed."
            reason = _infer_helper_failure_reason(helper_name, message) or "damaged_sidecar"
            _exit_with_support(
                parser,
                json_mode=True,
                exit_code=proc.returncode,
                message=message,
                payload=_contract_payload(reason, error=message),
                support=support if package_support_on_failure else None,
            )
        error_message = (
            parsed_error.get("error")
            if isinstance(parsed_error, dict) and isinstance(parsed_error.get("error"), str)
            else proc.stderr.strip() or f"{helper_name} failed."
        )
        exit_with_cli_error(
            parser,
            json_mode=False,
            exit_code=proc.returncode,
            message=error_message,
            payload=_contract_payload(
                _infer_helper_failure_reason(helper_name, error_message) or "damaged_sidecar",
                error=error_message,
            ),
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        message = f"{helper_name} returned invalid JSON: {exc}"
        _exit_with_support(
            parser,
            json_mode=True,
            exit_code=2,
            message=message,
            payload=_contract_payload("registry_contract_invalid", error=message),
            support=support if package_support_on_failure else None,
        )
    raise AssertionError("unreachable")


def _run_helper_passthrough(*, helper_name: str, helper_args: list[str]) -> None:
    cmd = [sys.executable, str(SCRIPT_DIR / helper_name), *helper_args]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    raise SystemExit(proc.returncode)


def _bridge_candidates(project_root: Path) -> list[str]:
    return [rel for rel in SUPPORTED_BRIDGE_TARGETS if (project_root / rel).is_file()]


def _bridge_action_surface(*, bridge_candidates: list[str]) -> dict | None:
    if not bridge_candidates:
        return None
    return {
        "action_label": "rl-bridge",
        "surface": "dispatcher/helper",
        "wrapper_guaranteed": False,
        "suggested_target": bridge_candidates[0],
    }


def _suggested_next_actions(*, bridge_candidates: list[str]) -> list[str]:
    actions = ["rl-resume", "rl-status"]
    if bridge_candidates:
        actions.insert(1, "review_bridge_candidates")
    return actions


def _print_init_summary(payload: dict) -> None:
    print(f"RecallLoom init completed for {payload['project_root']}")
    print(f"Storage root: {payload['storage_root']}")
    print(f"Storage mode: {payload['storage_mode']}")
    print(f"Workspace language: {payload['workspace_language']}")
    print(f"Validated: {'yes' if payload['validated'] else 'no'}")
    if payload.get("bridge_candidates"):
        print("Bridge candidates:")
        for item in payload["bridge_candidates"]:
            print(f"  - {item}")
    bridge_action_surface = payload.get("bridge_action_surface")
    if isinstance(bridge_action_surface, dict):
        action_label = bridge_action_surface.get("action_label")
        surface = bridge_action_surface.get("surface")
        suggested_target = bridge_action_surface.get("suggested_target")
        if action_label and surface and suggested_target:
            print(
                "Bridge action surface: "
                f"{action_label} via {surface} (review target: {suggested_target})"
            )
    if payload.get("bridge_applied"):
        print(f"Bridge applied: {payload['bridge_applied'][0]['target']}")
    print("Suggested next actions:")
    for action in payload["suggested_next_actions"]:
        print(f"  - {action}")


def _public_path_list(paths: object, *, project_root: Path) -> list[str]:
    if not isinstance(paths, list):
        return []
    public_paths: list[str] = []
    for path in paths:
        public_path = public_project_path(path, project_root=project_root)
        if public_path is not None:
            public_paths.append(public_path)
    return public_paths


def _public_validate_payload(payload: dict, *, project_root: Path) -> dict:
    public_payload = dict(payload)
    public_payload["project_root"] = public_project_root_label(project_root)
    public_payload["storage_root"] = public_project_path(
        payload.get("storage_root"),
        project_root=project_root,
    )
    findings = payload.get("findings")
    if isinstance(findings, list):
        public_payload["findings"] = []
        for finding in findings:
            if not isinstance(finding, dict):
                public_payload["findings"].append(finding)
                continue
            public_finding = dict(finding)
            if "path" in public_finding:
                public_finding["path"] = public_project_path(
                    public_finding.get("path"),
                    project_root=project_root,
                )
            public_payload["findings"].append(public_finding)
    return public_payload


def _public_bridge_results(results: object, *, project_root: Path) -> list[dict] | None:
    if results is None:
        return None
    if not isinstance(results, list):
        return results
    public_results: list[dict] = []
    for result in results:
        if not isinstance(result, dict):
            public_results.append(result)
            continue
        public_result = dict(result)
        if "target" in public_result:
            public_result["target"] = public_project_path(
                public_result.get("target"),
                project_root=project_root,
            )
        attach_scan = public_result.get("attach_scan")
        if isinstance(attach_scan, dict) and "target" in attach_scan:
            public_attach_scan = dict(attach_scan)
            public_attach_scan["target"] = public_project_path(
                public_attach_scan.get("target"),
                project_root=project_root,
            )
            public_result["attach_scan"] = public_attach_scan
        public_results.append(public_result)
    return public_results


def _public_init_payload(payload: dict, *, project_root: Path) -> dict:
    public_payload = dict(payload)
    public_payload["project_root"] = public_project_root_label(project_root)
    public_payload["storage_root"] = public_project_path(
        payload.get("storage_root"),
        project_root=project_root,
    )
    for field in ("created", "skipped"):
        public_payload[field] = _public_path_list(
            payload.get(field),
            project_root=project_root,
        )
    return public_payload


def _status_like_helper_args(args: argparse.Namespace) -> list[str]:
    helper_args = [args.target]
    if args.timezone:
        helper_args.extend(["--timezone", args.timezone])
    if args.now:
        helper_args.extend(["--now", args.now])
    if args.rollover_hour is not None:
        helper_args.extend(["--rollover-hour", str(args.rollover_hour)])
    if args.preferred_date:
        helper_args.extend(["--preferred-date", args.preferred_date])
    if args.session_intent:
        helper_args.extend(["--session-intent", args.session_intent])
    return helper_args


def _append_helper_args(args: argparse.Namespace) -> list[str]:
    helper_args = [args.target]
    if args.entry_json is not None:
        helper_args.extend(["--entry-json", args.entry_json])
    if args.input_format is not None:
        helper_args.extend(["--input-format", args.input_format])
    if args.max_input_bytes is not None:
        helper_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    if args.stdin:
        helper_args.append("--stdin")
    if args.entry_file is not None:
        helper_args.extend(["--entry-file", args.entry_file])
    if args.date is not None:
        helper_args.extend(["--date", args.date])
    if args.allow_historical:
        helper_args.append("--allow-historical")
    if args.no_auto_detect:
        helper_args.append("--no-auto-detect")
    if args.writer_id is not None:
        helper_args.extend(["--writer-id", args.writer_id])
    if args.wrapper_metadata_json is not None:
        helper_args.extend(["--wrapper-metadata-json", args.wrapper_metadata_json])
    if args.expected_workspace_revision is not None:
        helper_args.extend(["--expected-workspace-revision", str(args.expected_workspace_revision)])
    return helper_args


WRITE_TYPE_FILE_KEYS = {
    "current-state": "rolling_summary",
    "stable-context": "context_brief",
    "protocol-rules": "update_protocol",
}


def _write_input_mode(args: argparse.Namespace) -> str | None:
    if args.source_file is not None and args.stdin:
        return None
    if args.source_file is not None:
        return "file"
    if args.stdin:
        return "stdin"
    return None


def _write_invalid_input_payload(
    *,
    message: str,
    recovery_command: str,
    details: dict | None = None,
) -> dict:
    return cli_failure_payload(
        "invalid_prepared_input",
        error=message,
        details=details,
        extra={
            "suggestion": (
                "Phase 1 does not infer write targets from prepared content. "
                "Choose one explicit --type and one explicit input source, then retry."
            ),
            "recovery_command": recovery_command,
        },
    )


def _exit_write_invalid_input(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
    message: str,
    recovery_command: str,
    details: dict | None = None,
) -> None:
    _exit_with_support(
        parser,
        json_mode=args.json,
        exit_code=2,
        message=message,
        payload=_write_invalid_input_payload(
            message=message,
            recovery_command=recovery_command,
            details=details,
        ),
        support=support,
    )


def _validate_write_args(parser, args: argparse.Namespace, *, support: dict) -> tuple[str, str]:
    if args.write_type is None:
        _exit_write_invalid_input(
            parser,
            args,
            support=support,
            message=(
                "Missing --type. Phase 1 write dispatch does not infer whether prepared content "
                "belongs to current-state, stable-context, or protocol-rules."
            ),
            recovery_command=(
                "recallloom.py write <project> --type current-state "
                "--source-file <prepared-file> --json"
            ),
            details={
                "accepted_write_types": sorted(WRITE_TYPE_FILE_KEYS),
                "phase_1_infers_target": False,
            },
        )
    file_key = WRITE_TYPE_FILE_KEYS.get(args.write_type)
    if file_key is None:
        _exit_write_invalid_input(
            parser,
            args,
            support=support,
            message=(
                f"Unsupported --type '{args.write_type}'. Phase 1 write dispatch only accepts "
                "current-state, stable-context, or protocol-rules and does not infer targets."
            ),
            recovery_command=(
                "recallloom.py write <project> --type current-state "
                "--source-file <prepared-file> --json"
            ),
            details={
                "accepted_write_types": sorted(WRITE_TYPE_FILE_KEYS),
                "received_write_type": args.write_type,
                "phase_1_infers_target": False,
            },
        )
    input_mode = _write_input_mode(args)
    if input_mode is None:
        if args.source_file is not None and args.stdin:
            message = "Use exactly one prepared-content input for write: --source-file or --stdin."
        else:
            message = "Provide prepared content for write with exactly one of --source-file or --stdin."
        _exit_write_invalid_input(
            parser,
            args,
            support=support,
            message=message,
            recovery_command=(
                f"recallloom.py write <project> --type {args.write_type} "
                "--source-file <prepared-file> --json"
            ),
            details={
                "input_contract": "source-file_xor_stdin",
                "source_file_present": args.source_file is not None,
                "stdin_present": bool(args.stdin),
            },
        )
    if args.input_format == "json" and file_key != "rolling_summary":
        _exit_write_invalid_input(
            parser,
            args,
            support=support,
            message="Structured JSON input is only supported for --type current-state.",
            recovery_command=(
                "recallloom.py write <project> --type current-state "
                "--stdin --input-format json --json"
            ),
            details={
                "input_format": "json",
                "received_write_type": args.write_type,
                "accepted_write_type": "current-state",
            },
        )
    return file_key, input_mode


def _preflight_payload(parser, args: argparse.Namespace, *, support: dict) -> dict:
    return _run_helper_json(
        parser,
        helper_name="preflight_context_check.py",
        helper_args=[args.target, "--skip-startup-residue-scan"],
        json_mode_on_failure=args.json,
        support=support,
        package_support_on_failure=True,
    )


def _preflight_gate_details(preflight_payload: dict) -> dict:
    detail_keys = (
        "allowed_operation_level",
        "summary_stale",
        "continuity_drift_risk_level",
        "freshness_risk_level",
        "recommended_actions",
        "continuity_confidence",
        "continuity_state",
        "workspace_newer_than_summary",
        "summary_revision_stale",
    )
    return {key: preflight_payload.get(key) for key in detail_keys if key in preflight_payload}


def _write_retry_payload(
    args: argparse.Namespace,
    *,
    file_key: str,
    input_mode: str,
) -> dict:
    writer_args: list[str] = []
    writer_fields: dict[str, str | bool] = {}
    if args.writer_id is not None:
        safe_writer_id = normalize_safe_writer_id(args.writer_id)
        writer_arg = safe_writer_id or "same_explicit_writer_id"
        writer_args = ["--writer-id", writer_arg]
        writer_fields = {
            "writer_id_source": "explicit_cli",
            "writer_id_ref": "same_explicit_writer_id",
            "writer_id_public_safe": safe_writer_id is not None,
        }
        if safe_writer_id is not None:
            writer_fields["writer_id"] = safe_writer_id

    if input_mode == "file":
        input_ref = "same_prepared_source_file"
        input_args = ["--source-file", "same_prepared_source_file"]
    else:
        input_ref = "resubmit_same_stdin_payload"
        input_args = ["--stdin"]
    if args.input_format != "markdown":
        input_args.extend(["--input-format", args.input_format])
    if args.max_input_bytes is not None:
        input_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    if args.dry_run:
        input_args.append("--dry-run")
    return {
        "command": "recallloom.py write",
        "project_ref": "same_project",
        "write_type": args.write_type,
        "file_key": file_key,
        "input_mode": input_mode,
        "input_ref": input_ref,
        "input_format": args.input_format,
        "argv_template": [
            "recallloom.py",
            "write",
            "same_project",
            "--type",
            args.write_type,
            *input_args,
            *writer_args,
            "--json",
        ],
        "requires_repair_command_first": True,
        "side_effect": "none_until_retry",
        **writer_fields,
    }


def _write_preflight_failure_payload(
    args: argparse.Namespace,
    *,
    file_key: str,
    input_mode: str,
    message: str,
    details: dict,
) -> dict:
    # Keep repair_command tied to the public failure contract while letting
    # retry_payload pass through the contract's final publicization step.
    base_payload = cli_failure_payload(
        "stale_write_context",
        error=message,
        details=details,
    )
    return cli_failure_payload(
        "stale_write_context",
        error=message,
        details=details,
        extra={
            "repair_command": base_payload["recovery_command"],
            "retry_payload": _write_retry_payload(
                args,
                file_key=file_key,
                input_mode=input_mode,
            ),
        },
    )


def _enforce_write_preflight_gate(
    parser,
    args: argparse.Namespace,
    *,
    file_key: str,
    input_mode: str,
    preflight_payload: dict,
    support: dict,
) -> None:
    allowed_operation_level = preflight_payload.get("allowed_operation_level")
    summary_stale = preflight_payload.get("summary_stale")
    if allowed_operation_level == "write_current_state_after_preflight" and summary_stale is False:
        return

    message = (
        "Preflight requires review before write. recallloom.py write only proceeds when "
        "allowed_operation_level is write_current_state_after_preflight and summary_stale is false."
    )
    payload = _write_preflight_failure_payload(
        args,
        file_key=file_key,
        input_mode=input_mode,
        message=message,
        details=_preflight_gate_details(preflight_payload),
    )
    _exit_with_support(
        parser,
        json_mode=args.json,
        exit_code=3,
        message=message,
        payload=payload,
        support=support,
    )


def _write_context_from_preflight(
    parser,
    args: argparse.Namespace,
    *,
    file_key: str,
    preflight_payload: dict,
    support: dict,
) -> dict:
    safe_write_context = preflight_payload.get("safe_write_context")
    commit_contexts = (
        safe_write_context.get("commit_context_file")
        if isinstance(safe_write_context, dict)
        else None
    )
    write_context = commit_contexts.get(file_key) if isinstance(commit_contexts, dict) else None
    if not isinstance(write_context, dict):
        message = f"Preflight did not provide a safe commit_context_file context for {file_key}."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("malformed_managed_file", error=message),
            support=support,
        )
    expected_file_revision = write_context.get("expected_file_revision")
    expected_workspace_revision = write_context.get("expected_workspace_revision")
    if not isinstance(expected_file_revision, int) or not isinstance(expected_workspace_revision, int):
        message = f"Preflight returned incomplete write revisions for {file_key}."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("malformed_managed_file", error=message),
            support=support,
        )
    relative_path = write_context.get("target_path") or write_context.get("path")
    if not isinstance(relative_path, str):
        message = f"Preflight returned incomplete target path information for {file_key}."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("registry_contract_invalid", error=message),
            support=support,
        )
    try:
        workspace = find_recallloom_root(args.target)
    except (StorageResolutionError, ConfigContractError) as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
            support=support,
        )
    if workspace is None:
        message = "Preflight target is no longer attached to RecallLoom."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("no_project_root", error=message),
            support=support,
        )
    target_path = public_project_path(relative_path, project_root=workspace.project_root)
    if not isinstance(target_path, str):
        message = f"Preflight returned an invalid public target path for {file_key}."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("registry_contract_invalid", error=message),
            support=support,
        )
    return {
        "target_path": target_path,
        "expected_file_revision": expected_file_revision,
        "expected_workspace_revision": expected_workspace_revision,
    }


def _commit_context_file_args(
    args: argparse.Namespace,
    *,
    file_key: str,
    write_context: dict,
) -> list[str]:
    helper_args = [
        args.target,
        "--file-key",
        file_key,
        "--expected-file-revision",
        str(write_context["expected_file_revision"]),
        "--expected-workspace-revision",
        str(write_context["expected_workspace_revision"]),
    ]
    if args.source_file is not None:
        helper_args.extend(["--source-file", args.source_file])
    if args.stdin:
        helper_args.append("--stdin")
    if args.input_format != "markdown":
        helper_args.extend(["--input-format", args.input_format])
    if args.max_input_bytes is not None:
        helper_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    if args.writer_id is not None:
        helper_args.extend(["--writer-id", args.writer_id])
    if args.wrapper_metadata_json is not None:
        helper_args.extend(["--wrapper-metadata-json", args.wrapper_metadata_json])
    return helper_args


POST_APPEND_SYNC_COMMAND = "sync-current-state-after-append"
POST_APPEND_SYNC_CONTRACT_TYPE = "post_append_summary_sync"


def _post_append_sync_retry_payload(
    args: argparse.Namespace,
    *,
    input_mode: str,
) -> dict:
    writer_args: list[str] = []
    writer_fields: dict[str, str | bool] = {}
    if args.writer_id is not None:
        safe_writer_id = normalize_safe_writer_id(args.writer_id)
        writer_arg = safe_writer_id or "same_explicit_writer_id"
        writer_args = ["--writer-id", writer_arg]
        writer_fields = {
            "writer_id_source": "explicit_cli",
            "writer_id_ref": "same_explicit_writer_id",
            "writer_id_public_safe": safe_writer_id is not None,
        }
        if safe_writer_id is not None:
            writer_fields["writer_id"] = safe_writer_id

    input_ref = "resubmit_same_stdin_payload"
    input_args = ["--stdin"]
    if args.max_input_bytes is not None:
        input_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    return {
        "command": f"recallloom.py {POST_APPEND_SYNC_COMMAND}",
        "project_ref": "same_project",
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": input_mode,
        "input_ref": input_ref,
        "input_format": "json",
        "argv_template": [
            "recallloom.py",
            POST_APPEND_SYNC_COMMAND,
            "same_project",
            *input_args,
            "--input-format",
            "json",
            *writer_args,
            "--json",
        ],
        "requires_repair_command_first": True,
        "side_effect": "none_until_contract_allows_retry",
        **writer_fields,
    }


def _post_append_sync_failure_payload(
    args: argparse.Namespace,
    *,
    input_mode: str,
    message: str,
    reason_code: str,
    contract: dict | None,
) -> dict:
    provenance_guard = contract.get("provenance_guard") if isinstance(contract, dict) else None
    append_cursor = contract.get("append_cursor") if isinstance(contract, dict) else None
    ordinary_write_gate = contract.get("ordinary_write_gate") if isinstance(contract, dict) else None
    return {
        "ok": False,
        "schema_version": "1.1",
        "blocked": True,
        "blocked_reason": "post_append_summary_sync_not_allowed",
        "recoverability": "retryable",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "command": POST_APPEND_SYNC_COMMAND,
        "contract_type": (
            contract.get("contract_type")
            if isinstance(contract, dict) and isinstance(contract.get("contract_type"), str)
            else POST_APPEND_SYNC_CONTRACT_TYPE
        ),
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": input_mode,
        "input_format": "json",
        "reason_code": reason_code,
        "error": message,
        "user_message": "Post-append summary sync is not allowed for the current sidecar state.",
        "operator_note": (
            "Rerun preflight and only retry this command when the post_append_summary_sync "
            "contract is allowed for a single append delta."
        ),
        "next_actions": ["rerun_preflight", "review_post_append_summary_sync_contract"],
        "append_cursor": append_cursor if isinstance(append_cursor, dict) else None,
        "provenance_guard": provenance_guard if isinstance(provenance_guard, dict) else {},
        "ordinary_write_gate_preserved": (
            contract.get("ordinary_write_gate_preserved")
            if isinstance(contract, dict)
            else None
        ),
        "ordinary_write_gate": ordinary_write_gate if isinstance(ordinary_write_gate, dict) else None,
        "retry_payload": _post_append_sync_retry_payload(args, input_mode=input_mode),
    }


def _post_append_sync_invalid_input_payload(
    args: argparse.Namespace,
    *,
    message: str,
    source_file_present: bool,
    stdin_present: bool,
) -> dict:
    input_args = ["--stdin", "--input-format", "json"]
    if args.max_input_bytes is not None:
        input_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    return cli_failure_payload(
        "invalid_prepared_input",
        error=message,
        details={
            "input_contract": "stdin_only_json",
            "source_file_present": source_file_present,
            "stdin_present": stdin_present,
            "accepted_input": "--stdin",
            "input_format": "json",
        },
        extra={
            "command": POST_APPEND_SYNC_COMMAND,
            "write_type": "current-state",
            "file_key": "rolling_summary",
            "input_format": "json",
            "retry_payload": {
                "command": f"recallloom.py {POST_APPEND_SYNC_COMMAND}",
                "project_ref": "same_project",
                "input_mode": "json-stdin",
                "input_ref": "resubmit_same_stdin_payload",
                "input_format": "json",
                "argv_template": [
                    "recallloom.py",
                    POST_APPEND_SYNC_COMMAND,
                    "same_project",
                    *input_args,
                    "--json",
                ],
                "requires_repair_command_first": True,
                "side_effect": "none_until_contract_allows_retry",
            },
        },
    )


def _post_append_sync_input_mode(args: argparse.Namespace) -> str:
    return "json-stdin"


def _validate_post_append_sync_args(parser, args: argparse.Namespace, *, support: dict) -> None:
    source_file_present = args.source_file is not None
    stdin_present = bool(args.stdin)
    if source_file_present or not stdin_present:
        if source_file_present:
            message = (
                "sync-current-state-after-append only accepts reviewed rolling-summary JSON "
                "through --stdin; --source-file is not part of this command surface."
            )
        else:
            message = (
                "sync-current-state-after-append requires reviewed rolling-summary JSON "
                "through --stdin."
            )
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=_post_append_sync_invalid_input_payload(
                args,
                message=message,
                source_file_present=source_file_present,
                stdin_present=stdin_present,
            ),
            support=support,
        )

    if args.writer_id is not None and normalize_safe_writer_id(args.writer_id) is None:
        message = "Explicit writer id for post-append summary sync is not public-safe."
        input_args = ["--stdin", "--input-format", "json"]
        if args.max_input_bytes is not None:
            input_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=4,
            message=message,
            payload=cli_failure_payload(
                "privacy_security_failure",
                error=message,
                details={
                    "field": "writer_id",
                    "reason_code": "unsafe_explicit_writer_id",
                    "writer_id_source": "explicit_cli",
                    "writer_id_public_safe": False,
                },
                extra={
                    "command": POST_APPEND_SYNC_COMMAND,
                    "write_type": "current-state",
                    "file_key": "rolling_summary",
                    "input_mode": "json-stdin",
                    "input_format": "json",
                    "retry_payload": {
                        "command": f"recallloom.py {POST_APPEND_SYNC_COMMAND}",
                        "project_ref": "same_project",
                        "input_mode": "json-stdin",
                        "input_ref": "resubmit_same_stdin_payload",
                        "input_format": "json",
                        "writer_id_source": "explicit_cli",
                        "writer_id_ref": "same_explicit_writer_id",
                        "writer_id_public_safe": False,
                        "argv_template": [
                            "recallloom.py",
                            POST_APPEND_SYNC_COMMAND,
                            "same_project",
                            *input_args,
                            "--writer-id",
                            "same_public_safe_writer_id",
                            "--json",
                        ],
                        "requires_repair_command_first": True,
                        "side_effect": "none_until_contract_allows_retry",
                    },
                },
            ),
            support=support,
        )


def _required_contract_bool_guard(
    provenance_guard: dict,
    *,
    key: str,
    expected: bool,
) -> str | None:
    if provenance_guard.get(key) is expected:
        return None
    return f"provenance_guard_{key}_invalid"


def _post_append_sync_contract_reason(contract: object) -> str | None:
    if not isinstance(contract, dict):
        return "missing_post_append_summary_sync_contract"
    if contract.get("allowed") is not True:
        reason_code = contract.get("reason_code")
        return reason_code if isinstance(reason_code, str) and reason_code else "contract_not_allowed"
    if contract.get("contract_type") != POST_APPEND_SYNC_CONTRACT_TYPE:
        return "contract_type_mismatch"
    if contract.get("file_key") != "rolling_summary":
        return "file_key_mismatch"
    if contract.get("write_type") != "current-state":
        return "write_type_mismatch"
    if contract.get("input_format") != "json":
        return "input_format_mismatch"
    expected_file_revision = contract.get("expected_file_revision")
    expected_workspace_revision = contract.get("expected_workspace_revision")
    if not isinstance(expected_file_revision, int) or not isinstance(expected_workspace_revision, int):
        return "missing_expected_revision"
    if contract.get("ordinary_write_gate_preserved") is not True:
        return "ordinary_write_gate_not_preserved"
    ordinary_write_gate = contract.get("ordinary_write_gate")
    if not isinstance(ordinary_write_gate, dict):
        return "missing_ordinary_write_gate"
    if ordinary_write_gate.get("contract_does_not_authorize_recallloom_write") is not True:
        return "ordinary_write_gate_authorization_mismatch"
    if ordinary_write_gate.get("allowed_operation_level") != "read_current_state":
        return "ordinary_write_gate_level_mismatch"
    if ordinary_write_gate.get("summary_stale") is not True:
        return "ordinary_write_gate_stale_state_mismatch"
    provenance_guard = contract.get("provenance_guard")
    if not isinstance(provenance_guard, dict):
        return "missing_provenance_guard"
    for key in (
        "single_append_delta",
        "cursor_matches_latest_log",
        "latest_daily_log_not_older_than_summary",
    ):
        reason = _required_contract_bool_guard(provenance_guard, key=key, expected=True)
        if reason is not None:
            return reason
    non_summary_writes = provenance_guard.get("non_summary_writes_after_summary_base")
    if non_summary_writes != []:
        return "stale_cause_not_append_only"
    append_cursor = contract.get("append_cursor")
    if not isinstance(append_cursor, dict):
        return "missing_append_cursor"
    if not isinstance(append_cursor.get("latest_file"), str):
        return "missing_append_cursor_latest_file"
    if not isinstance(append_cursor.get("latest_entry_id"), str):
        return "missing_append_cursor_latest_entry_id"
    if not isinstance(append_cursor.get("latest_entry_seq"), int):
        return "missing_append_cursor_latest_entry_seq"
    if not isinstance(append_cursor.get("entry_count"), int):
        return "missing_append_cursor_entry_count"
    return None


def _post_append_sync_contract_from_preflight(preflight_payload: dict) -> dict | None:
    safe_write_context = preflight_payload.get("safe_write_context")
    if not isinstance(safe_write_context, dict):
        return None
    contract = safe_write_context.get(POST_APPEND_SYNC_CONTRACT_TYPE)
    return contract if isinstance(contract, dict) else None


def _post_append_sync_commit_args(args: argparse.Namespace, *, contract: dict) -> list[str]:
    helper_args = [
        args.target,
        "--file-key",
        "rolling_summary",
        "--expected-file-revision",
        str(contract["expected_file_revision"]),
        "--expected-workspace-revision",
        str(contract["expected_workspace_revision"]),
        "--input-format",
        "json",
    ]
    helper_args.append("--stdin")
    if args.max_input_bytes is not None:
        helper_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    if args.writer_id is not None:
        safe_writer_id = normalize_safe_writer_id(args.writer_id)
        if safe_writer_id is None:
            raise ConfigContractError("writer_id must be public-safe for post-append summary sync")
        helper_args.extend(["--writer-id", safe_writer_id])
    if args.wrapper_metadata_json is not None:
        helper_args.extend(["--wrapper-metadata-json", args.wrapper_metadata_json])
    return helper_args


def _handle_post_append_summary_sync(parser, args: argparse.Namespace, *, support: dict) -> None:
    _validate_post_append_sync_args(parser, args, support=support)
    input_mode = _post_append_sync_input_mode(args)
    try:
        wrapper_metadata = normalize_wrapper_metadata_json(args.wrapper_metadata_json)
    except WrapperMetadataSecurityError as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=4,
            message=str(exc),
            payload=cli_failure_payload(
                "privacy_security_failure",
                error=str(exc),
                details=exc.details,
            ),
            support=support,
        )
    preflight_payload = _preflight_payload(parser, args, support=support)
    contract = _post_append_sync_contract_from_preflight(preflight_payload)
    reason_code = _post_append_sync_contract_reason(contract)
    if reason_code is not None:
        message = (
            "Preflight did not authorize post-append current-state summary sync "
            f"for this sidecar state: {reason_code}."
        )
        payload = _post_append_sync_failure_payload(
            args,
            input_mode=input_mode,
            message=message,
            reason_code=reason_code,
            contract=contract,
        )
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=payload,
            support=support,
        )

    assert contract is not None
    payload = _run_helper_json(
        parser,
        helper_name="commit_context_file.py",
        helper_args=_post_append_sync_commit_args(args, contract=contract),
        json_mode_on_failure=args.json,
        support=support,
        package_support_on_failure=True,
    )
    result = {
        **payload,
        "schema_version": "1.1",
        "command": POST_APPEND_SYNC_COMMAND,
        "contract_type": POST_APPEND_SYNC_CONTRACT_TYPE,
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": payload.get("input_mode", input_mode),
        "input_format": "json",
        "expected_file_revision": contract["expected_file_revision"],
        "expected_workspace_revision": contract["expected_workspace_revision"],
        "append_cursor": contract.get("append_cursor"),
        "provenance_guard": contract.get("provenance_guard"),
        "ordinary_write_gate_preserved": contract.get("ordinary_write_gate_preserved"),
        "ordinary_write_gate": contract.get("ordinary_write_gate"),
        "package_support": public_package_support_payload(support),
    }
    if wrapper_metadata is not None:
        result["wrapper_metadata"] = wrapper_metadata
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Synced rolling_summary after append to {result.get('target_path', '.recallloom/rolling_summary.md')}")


def _resume_ready(payload: dict) -> bool:
    if payload.get("continuity_confidence") == "broken":
        return False
    if payload.get("continuity_state") == "initialized_empty_shell":
        return False
    return True


def _resume_payload(payload: dict) -> dict:
    result = dict(payload)
    result["command"] = "resume"
    result["routing_target"] = "rl-resume"
    result["resume_ready"] = _resume_ready(result)
    snapshot = result.get("continuity_snapshot")
    if isinstance(snapshot, dict):
        result["continuity_snapshot"] = {**snapshot, "task_type": "resume_checkpoint"}
    return result


def _dispatcher_action_level(command: str) -> str:
    if command == "quick-summary":
        return "diagnostic"
    if command in {"append", "write", POST_APPEND_SYNC_COMMAND}:
        return "mutating"
    return action_level_for_dispatcher(command)


def _handle_quick_summary(parser, args: argparse.Namespace, *, support: dict) -> None:
    try:
        start_path = normalize_start_path(args.target)
    except StorageResolutionError as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="not_project_root"),
            support=support,
        )
    try:
        workspace = find_recallloom_root(args.target)
    except (StorageResolutionError, ConfigContractError) as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"continuity_confidence": "broken"},
            ),
            support=support,
        )

    if workspace is None:
        payload = build_no_project_payload(start_path)
    else:
        startup_residue_report = _exit_if_startup_scratch_residue_with_support(
            parser,
            json_mode=args.json,
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            support=support,
        )
        try:
            summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
            if not summary_path.is_file():
                message = f"Missing required file: {summary_path}"
                _exit_with_support(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=message,
                    payload=cli_failure_payload("malformed_managed_file", error=message),
                    support=support,
                )
            summary_text = read_text(summary_path)
            summary_state = parse_file_state_marker(summary_text)
            if summary_state is None:
                message = f"Missing required file-state metadata marker: {summary_path}"
                _exit_with_support(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=message,
                    payload=cli_failure_payload("malformed_managed_file", error=message),
                    support=support,
                )
            state = load_workspace_state(workspace.storage_root / FILE_KEYS["state"])
        except (ConfigContractError, OSError, UnicodeDecodeError) as exc:
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=str(exc),
                payload=cli_failure_payload_for_exception(
                    exc,
                    default_reason="damaged_sidecar",
                    extra={"continuity_confidence": "broken"},
                ),
                support=support,
            )
        payload = build_quick_summary_payload(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            summary_path=summary_path,
            summary_text=summary_text,
            summary_revision=summary_state.revision,
            summary_base_workspace_revision=summary_state.base_workspace_revision,
            state=state,
        )
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report

    if args.json:
        payload["package_support"] = public_package_support_payload(support)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"RecallLoom quick summary target: {payload['project_root']}")
    storage_root = payload.get("storage_root")
    if storage_root:
        print(f"Storage root: {storage_root}")
    summary = payload["summary"]
    print(f"Project: {summary['project'] or 'none'}")
    print(f"Phase: {summary['phase']}")
    print(f"Confidence: {summary['confidence']}")
    if summary["next_step"]:
        print(f"Next step: {summary['next_step']}")
    freshness = payload["freshness"]
    print(
        "Freshness: "
        f"stale={'yes' if freshness['summary_stale'] else 'no'} "
        f"(risk={freshness['freshness_risk_level']})"
    )
    print("Next actions:")
    for action in payload["next_actions"]:
        print(f"  - {action}")


def _project_relative_path(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix()


def _compact_guidance(text: str, *, max_chars: int = 360) -> str | None:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        stripped = stripped.lstrip("#-* ").strip()
        if stripped:
            lines.append(stripped)
    compacted = " ".join(lines)
    if not compacted:
        return None
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 3].rstrip(" ,;:|/-") + "..."


def _estimated_tokens_for_texts(files: list[str], text_by_path: dict[str, str]) -> int:
    total = 0
    for rel_path in files:
        text = text_by_path.get(rel_path, "")
        total += max(64, (len(text) + 3) // 4) if text else 64
    return total


def _progressive_resume_read_plan(
    *,
    mode: str,
    project_root: Path,
    storage_root: Path,
    state_text: str,
    summary_text: str,
    context_brief_text: str = "",
    update_protocol_text: str = "",
    update_protocol_available: bool = False,
) -> dict:
    state_rel = _project_relative_path(storage_root / FILE_KEYS["state"], project_root)
    summary_rel = _project_relative_path(storage_root / FILE_KEYS["rolling_summary"], project_root)
    files = [state_rel, summary_rel]
    text_by_path = {
        state_rel: state_text,
        summary_rel: summary_text,
    }
    if mode == "full":
        context_rel = _project_relative_path(storage_root / FILE_KEYS["context_brief"], project_root)
        files.append(context_rel)
        text_by_path[context_rel] = context_brief_text
        if update_protocol_available:
            update_protocol_rel = _project_relative_path(storage_root / FILE_KEYS["update_protocol"], project_root)
            files.append(update_protocol_rel)
            text_by_path[update_protocol_rel] = update_protocol_text
    reason = (
        "Fast bounded resume reads only state.json and rolling_summary.md for current-state orientation."
        if mode == "fast"
        else (
            "Full bounded resume adds context_brief.md and any available update_protocol.md guidance; "
            "evidence expansion remains on demand."
        )
    )
    return {
        "mode": mode,
        "files": files,
        "reason": reason,
        "estimated_tokens": _estimated_tokens_for_texts(files, text_by_path),
        "bounded": True,
    }


def _resume_trust_state(
    *,
    continuity_confidence: str,
    continuity_state: str,
    summary_stale: bool,
    workspace_newer_than_summary: bool,
) -> dict:
    if continuity_state == "no_project":
        return {
            "sidecar_trust_state": "unknown",
            "continuity_drift_risk_level": "none",
            "allowed_operation_level": "none",
            "read_confidence": "untrusted",
            "read_trust_note": "No RecallLoom sidecar was found.",
        }
    return evaluate_trust_state(
        continuity_confidence=continuity_confidence,
        continuity_state=continuity_state,
        summary_stale=summary_stale,
        workspace_newer_than_summary=workspace_newer_than_summary,
        conflict_state=None,
    )


def _resume_continuity_state(quick_payload: dict) -> str:
    phase = quick_payload.get("summary", {}).get("phase")
    if phase == "no_project":
        return "no_project"
    if phase == "unseeded":
        return "initialized_empty_shell"
    return "initialized_seeded"


def _resume_next_actions(*, mode: str, quick_actions: list[str]) -> list[str]:
    actions = list(quick_actions)
    if mode == "fast":
        actions.append("rerun_resume_with_full_when_stable_context_or_protocol_guidance_is_needed")
    actions.append("use_query_continuity.py_on_demand_for_daily_log_evidence")
    return actions


def _build_progressive_resume_payload(
    parser,
    args: argparse.Namespace,
    *,
    mode: str,
    support: dict,
) -> dict:
    try:
        start_path = normalize_start_path(args.target)
    except StorageResolutionError as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="not_project_root"),
            support=support,
        )
    try:
        workspace = find_recallloom_root(args.target)
    except (StorageResolutionError, ConfigContractError) as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"continuity_confidence": "broken"},
            ),
            support=support,
        )

    if workspace is None:
        quick_payload = build_no_project_payload(start_path)
        continuity_state = _resume_continuity_state(quick_payload)
        trust_state = _resume_trust_state(
            continuity_confidence="none",
            continuity_state=continuity_state,
            summary_stale=False,
            workspace_newer_than_summary=False,
        )
        payload = {
            "schema_version": "1.1",
            "ok": True,
            "command": "resume",
            "routing_target": "rl-resume",
            "resume_mode": mode,
            "resume_ready": False,
            "project_root": public_project_root_label(start_path),
            "storage_root": None,
            "current_state": quick_payload["summary"],
            "summary": quick_payload["summary"],
            "freshness": quick_payload["freshness"],
            "trust": {
                "continuity_confidence": "none",
                "continuity_state": continuity_state,
                "summary_stale": False,
                "resume_ready": False,
                "sidecar_trust_state": trust_state["sidecar_trust_state"],
                "allowed_operation_level": trust_state["allowed_operation_level"],
                "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
                "read_confidence": trust_state["read_confidence"],
                "read_trust_note": trust_state["read_trust_note"],
            },
            "continuity_confidence": "none",
            "continuity_state": continuity_state,
            "sidecar_trust_state": trust_state["sidecar_trust_state"],
            "allowed_operation_level": trust_state["allowed_operation_level"],
            "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
            "progressive_read_plan": {
                "mode": mode,
                "files": [],
                "reason": "No RecallLoom sidecar was found, so no continuity files were read.",
                "estimated_tokens": 0,
                "bounded": True,
            },
            "next_actions": list(quick_payload["next_actions"]),
            "package_support": public_package_support_payload(support),
        }
        return payload
    startup_residue_report = _exit_if_startup_scratch_residue_with_support(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        support=support,
    )

    try:
        summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
        if not summary_path.is_file():
            message = f"Missing required file: {summary_path}"
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload("malformed_managed_file", error=message),
                support=support,
            )
        summary_text = read_text(summary_path)
        summary_state = parse_file_state_marker(summary_text)
        if summary_state is None:
            message = f"Missing required file-state metadata marker: {summary_path}"
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload("malformed_managed_file", error=message),
                support=support,
            )
        state = load_workspace_state(workspace.storage_root / FILE_KEYS["state"])
        state_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    except (ConfigContractError, OSError, UnicodeDecodeError) as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"continuity_confidence": "broken"},
            ),
            support=support,
        )

    quick_payload = build_quick_summary_payload(
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        summary_path=summary_path,
        summary_text=summary_text,
        summary_revision=summary_state.revision,
        summary_base_workspace_revision=summary_state.base_workspace_revision,
        state=state,
    )
    continuity_state = _resume_continuity_state(quick_payload)
    continuity_confidence = quick_payload["summary"]["confidence"]
    trust_state = _resume_trust_state(
        continuity_confidence=continuity_confidence,
        continuity_state=continuity_state,
        summary_stale=quick_payload["freshness"]["summary_stale"],
        workspace_newer_than_summary=quick_payload["freshness"].get("workspace_newer_than_summary", False),
    )
    base_payload = {
        "schema_version": "1.1",
        "ok": True,
        "command": "resume",
        "routing_target": "rl-resume",
        "resume_mode": mode,
        "project_root": public_project_root_label(workspace.project_root),
        "storage_root": public_project_path(workspace.storage_root, project_root=workspace.project_root),
        "current_state": quick_payload["summary"],
        "summary": quick_payload["summary"],
        "freshness": quick_payload["freshness"],
        "trust": {
            "continuity_confidence": continuity_confidence,
            "continuity_state": continuity_state,
            "summary_stale": quick_payload["freshness"]["summary_stale"],
            "sidecar_trust_state": trust_state["sidecar_trust_state"],
            "allowed_operation_level": trust_state["allowed_operation_level"],
            "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
            "read_confidence": trust_state["read_confidence"],
            "read_trust_note": trust_state["read_trust_note"],
        },
        "continuity_confidence": continuity_confidence,
        "continuity_state": continuity_state,
        "sidecar_trust_state": trust_state["sidecar_trust_state"],
        "allowed_operation_level": trust_state["allowed_operation_level"],
        "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
        "next_actions": _resume_next_actions(mode=mode, quick_actions=quick_payload["next_actions"]),
        "package_support": public_package_support_payload(support),
    }
    if startup_residue_report is not None:
        base_payload["startup_residue_report"] = startup_residue_report
    base_payload["resume_ready"] = _resume_ready(base_payload)
    base_payload["trust"]["resume_ready"] = base_payload["resume_ready"]

    if mode == "fast":
        base_payload["progressive_read_plan"] = _progressive_resume_read_plan(
            mode=mode,
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            state_text=state_text,
            summary_text=summary_text,
        )
        return base_payload

    context_brief_path = workspace.storage_root / FILE_KEYS["context_brief"]
    update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
    try:
        context_brief_text = read_text(context_brief_path) if context_brief_path.is_file() else ""
        update_protocol_text = read_text(update_protocol_path) if update_protocol_path.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload(
                "damaged_sidecar",
                error=f"Filesystem error: {exc}",
                extra={"continuity_confidence": "broken"},
            ),
            support=support,
        )
    base_payload["progressive_read_plan"] = _progressive_resume_read_plan(
        mode=mode,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        state_text=state_text,
        summary_text=summary_text,
        context_brief_text=context_brief_text,
        update_protocol_text=update_protocol_text,
        update_protocol_available=update_protocol_path.is_file(),
    )
    base_payload["expansion"] = {
        "reason": (
            "Explicit --full requested bounded stable framing and project-local update-protocol guidance. "
            "Daily-log evidence is intentionally left to query_continuity.py on demand."
        ),
        "default_reads_daily_log_content": False,
    }
    base_payload["context_brief"] = {
        "available": context_brief_path.is_file(),
        "path": _project_relative_path(context_brief_path, workspace.project_root),
        "guidance": _compact_guidance(context_brief_text),
    }
    base_payload["update_protocol_guidance"] = {
        "available": update_protocol_path.is_file(),
        "path": _project_relative_path(update_protocol_path, workspace.project_root),
        "guidance": _compact_guidance(update_protocol_text),
    }
    return base_payload


def _print_progressive_resume_summary(payload: dict) -> None:
    print(f"RecallLoom resume target: {payload['project_root']}")
    print("Routing target: rl-resume")
    print(f"Resume mode: {payload['resume_mode']}")
    print(f"Resume ready: {'yes' if payload['resume_ready'] else 'no'}")
    print(f"Confidence: {payload.get('continuity_confidence')}")
    print(f"State: {payload.get('continuity_state')}")
    next_step = payload.get("current_state", {}).get("next_step")
    if next_step:
        print(f"Next step: {next_step}")
    read_plan = payload.get("progressive_read_plan", {})
    reason = read_plan.get("reason")
    if reason:
        print(f"Bounded read: {reason}")
    if payload.get("resume_mode") == "full":
        expansion = payload.get("expansion", {})
        if expansion.get("reason"):
            print(f"Bounded expansion reason: {expansion['reason']}")
        guidance = payload.get("update_protocol_guidance", {}).get("guidance")
        if guidance:
            print(f"Update protocol guidance: {guidance}")
    print("Read files:")
    for rel_path in read_plan.get("files", []):
        print(f"  - {rel_path}")
    print("Next actions:")
    for action in payload.get("next_actions", []):
        print(f"  - {action}")


def _print_resume_summary(payload: dict) -> None:
    print(f"RecallLoom resume target: {payload['project_root']}")
    print("Routing target: rl-resume")
    print(f"Resume ready: {'yes' if payload['resume_ready'] else 'no'}")
    print(f"Continuity confidence: {payload.get('continuity_confidence')}")
    print(f"Continuity state: {payload.get('continuity_state')}")
    print("Recommended actions:")
    for action in payload.get("recommended_actions", []):
        print(f"  - {action}")
    workday = payload.get("workday")
    if isinstance(workday, dict):
        guidance = describe_workday_guidance(workday, always_show=False)
        if guidance:
            print(f"Workday guidance: {guidance}")


def _handle_init(parser, args: argparse.Namespace, *, support: dict) -> None:
    init_args = [args.target]
    if args.tool_name:
        init_args.extend(["--tool-name", args.tool_name])
    if args.date:
        init_args.extend(["--date", args.date])
    if args.storage_mode:
        init_args.extend(["--storage-mode", args.storage_mode])
    if args.workspace_language:
        init_args.extend(["--workspace-language", args.workspace_language])
    if args.create_daily_log:
        init_args.append("--create-daily-log")
    if args.force:
        init_args.append("--force")
    if args.skip_git_exclude:
        init_args.append("--skip-git-exclude")

    init_payload = _run_helper_json(
        parser,
        helper_name="init_context.py",
        helper_args=init_args,
        json_mode_on_failure=args.json,
        support=support,
    )
    if not init_payload.get("project_root") or not init_payload.get("storage_root"):
        message = "init_context.py returned an incomplete payload."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=_contract_payload("registry_contract_invalid", error=message),
        )

    validate_payload = _run_helper_json(
        parser,
        helper_name="validate_context.py",
        helper_args=[args.target],
        json_mode_on_failure=args.json,
        support=support,
    )

    bridge_payload = None
    if args.bridge:
        if not args.yes:
            message = "--bridge requires --yes. Bridge application stays explicit."
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=_contract_payload("invalid_prepared_input", error=message),
            )
        bridge_payload = _run_helper_json(
            parser,
            helper_name="manage_entry_bridge.py",
            helper_args=[args.target, "--file", args.bridge, "--yes"],
            json_mode_on_failure=args.json,
            support=support,
        )

    project_root = Path(args.target).expanduser().resolve()
    bridge_candidates = _bridge_candidates(project_root)
    bridge_results = bridge_payload.get("results") if bridge_payload else None
    payload = {
        "ok": True,
        "command": "init",
        "project_root": init_payload["project_root"],
        "storage_root": init_payload["storage_root"],
        "storage_mode": init_payload["storage_mode"],
        "workspace_language": init_payload["workspace_language"],
        "initialized": True,
        "already_initialized": bool(init_payload.get("already_initialized", False)),
        "validated": bool(validate_payload.get("valid", False)),
        "init": init_payload,
        "validate": validate_payload,
        "bridge_candidates": bridge_candidates,
        "bridge_action_surface": _bridge_action_surface(bridge_candidates=bridge_candidates),
        "bridge_applied": bridge_results,
        "suggested_next_actions": _suggested_next_actions(bridge_candidates=bridge_candidates),
        "package_support": public_package_support_payload(support),
    }
    if args.json:
        public_payload = {
            **payload,
            "project_root": public_project_root_label(project_root),
            "storage_root": public_project_path(
                init_payload.get("storage_root"),
                project_root=project_root,
            ),
            "init": _public_init_payload(init_payload, project_root=project_root),
            "validate": _public_validate_payload(validate_payload, project_root=project_root),
            "bridge_applied": _public_bridge_results(bridge_results, project_root=project_root),
        }
        print(json.dumps(public_payload, ensure_ascii=False, indent=2))
    else:
        _print_init_summary(payload)


def _handle_write(parser, args: argparse.Namespace, *, support: dict) -> None:
    file_key, input_mode = _validate_write_args(parser, args, support=support)
    try:
        wrapper_metadata = normalize_wrapper_metadata_json(args.wrapper_metadata_json)
    except WrapperMetadataSecurityError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=4,
            message=str(exc),
            payload=cli_failure_payload(
                "privacy_security_failure",
                error=str(exc),
                details=exc.details,
            ),
        )
    preflight_payload = _preflight_payload(parser, args, support=support)
    _enforce_write_preflight_gate(
        parser,
        args,
        file_key=file_key,
        input_mode=input_mode,
        preflight_payload=preflight_payload,
        support=support,
    )
    write_context = _write_context_from_preflight(
        parser,
        args,
        file_key=file_key,
        preflight_payload=preflight_payload,
        support=support,
    )

    if args.dry_run:
        payload = {
            "ok": True,
            "schema_version": "1.1",
            "command": "write",
            "write_type": args.write_type,
            "file_key": file_key,
            "dry_run": True,
            "input_mode": input_mode,
            "project_root": preflight_payload.get("project_root"),
            "storage_root": preflight_payload.get("storage_root"),
            "target_path": write_context["target_path"],
            "expected_file_revision": write_context["expected_file_revision"],
            "expected_workspace_revision": write_context["expected_workspace_revision"],
            "package_support": public_package_support_payload(support),
        }
        if wrapper_metadata is not None:
            payload["wrapper_metadata"] = wrapper_metadata
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"RecallLoom write dry-run target: {payload['target_path']}")
            print(f"Write type: {args.write_type} ({file_key})")
            print(f"Expected file revision: {write_context['expected_file_revision']}")
            print(f"Expected workspace revision: {write_context['expected_workspace_revision']}")
        return

    payload = _run_helper_json(
        parser,
        helper_name="commit_context_file.py",
        helper_args=_commit_context_file_args(args, file_key=file_key, write_context=write_context),
        json_mode_on_failure=args.json,
        support=support,
        package_support_on_failure=True,
    )
    payload.update(
        {
            "schema_version": "1.1",
            "command": "write",
            "write_type": args.write_type,
            "dry_run": False,
            "package_support": public_package_support_payload(support),
            "target_path": write_context["target_path"],
        }
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Committed {file_key} to {payload.get('target_path', write_context['target_path'])}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=getattr(args, "json", False),
            exit_code=2,
            message=str(exc),
            payload=_contract_payload("python_runtime_unavailable"),
        )
    support = enforce_package_support_gate(
        parser,
        json_mode=getattr(args, "json", False),
        action_name=f"recallloom.py {args.command}",
        action_level=_dispatcher_action_level(args.command),
    )

    if args.command == "init":
        _handle_init(parser, args, support=support)
        return

    if args.command == "validate":
        if args.json:
            payload = _run_helper_json(
                parser,
                helper_name="validate_context.py",
                helper_args=[args.target],
                json_mode_on_failure=True,
                support=support,
            )
            public_payload = dict(payload)
            public_payload["package_support"] = public_package_support_payload(support)
            print(json.dumps(public_payload, ensure_ascii=False, indent=2))
        else:
            _run_helper_passthrough(helper_name="validate_context.py", helper_args=[args.target])
        return

    if args.command == "resume" and (args.fast or args.full):
        mode = "full" if args.full else "fast"
        payload = _build_progressive_resume_payload(parser, args, mode=mode, support=support)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_progressive_resume_summary(payload)
        return

    if args.command in {"status", "resume"}:
        helper_args = _status_like_helper_args(args)
        if args.json:
            payload = _run_helper_json(
                parser,
                helper_name="summarize_continuity_status.py",
                helper_args=helper_args,
                json_mode_on_failure=True,
                support=support,
            )
            if args.command == "resume":
                payload = _resume_payload(payload)
            payload["package_support"] = public_package_support_payload(support)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if args.command == "resume":
                payload = _run_helper_json(
                    parser,
                    helper_name="summarize_continuity_status.py",
                    helper_args=helper_args,
                    json_mode_on_failure=False,
                    support=support,
                )
                _print_resume_summary(_resume_payload(payload))
            else:
                _run_helper_passthrough(
                    helper_name="summarize_continuity_status.py", helper_args=helper_args
                )
        return

    if args.command == "quick-summary":
        _handle_quick_summary(parser, args, support=support)
        return

    if args.command == "append":
        helper_args = _append_helper_args(args)
        if args.json:
            payload = _run_helper_json(
                parser,
                helper_name="append_daily_log_entry.py",
                helper_args=helper_args,
                json_mode_on_failure=True,
                support=support,
                package_support_on_failure=True,
            )
            payload["package_support"] = public_package_support_payload(support)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _run_helper_passthrough(helper_name="append_daily_log_entry.py", helper_args=helper_args)
        return

    if args.command == "write":
        _handle_write(parser, args, support=support)
        return

    if args.command == POST_APPEND_SYNC_COMMAND:
        _handle_post_append_summary_sync(parser, args, support=support)
        return

    if args.command == "bridge":
        helper_args = [args.target]
        for rel in args.file:
            helper_args.extend(["--file", rel])
        if args.remove:
            helper_args.append("--remove")
        if args.yes:
            helper_args.append("--yes")
        if args.json:
            payload = _run_helper_json(
                parser,
                helper_name="manage_entry_bridge.py",
                helper_args=helper_args,
                json_mode_on_failure=True,
                support=support,
            )
            payload["package_support"] = public_package_support_payload(support)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _run_helper_passthrough(helper_name="manage_entry_bridge.py", helper_args=helper_args)
        return

    raise ConfigContractError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
