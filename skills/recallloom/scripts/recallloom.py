#!/usr/bin/env python3
"""Unified operator-friendly entrypoint for RecallLoom helper workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


_BOOTSTRAP_DEFAULT_MINIMUM_PYTHON_VERSION = "3.10"
_SHA256_REFERENCE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORD_PLAN_OUTPUT_ID_RE = re.compile(r"^record-plan-output:sha256:[0-9a-f]{64}$")
_EXPLICIT_OPERATOR_CONFIRMATION_ID_RE = re.compile(
    r"^explicit-operator-confirmation:sha256:[0-9a-f]{64}$"
)


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
from core.continuity.workday import (
    DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
    RECOMMENDATION_TYPES,
    describe_workday_guidance,
)
from core.failure.contracts import failure_payload, preferred_failure_language
from core.output.confirmation_material import (
    print_confirmation_material,
    record_plan_confirmation_material,
    review_imported_baseline_confirmation_material,
)
from core.output.privacy import redact_public_text
from core.output.user_status import print_user_summary, read_surface_user_summary
from core.provenance.bindings import (
    PreflightBindingLeaseError,
    write_preflight_binding_lease,
)
from core.provenance.evidence import (
    strict_gate_current_receipts_verified,
    strict_sidecar_no_write_failure_extra,
    strict_sidecar_no_write_failure_extra_from_summary,
    strict_sidecar_integrity_gate,
    strict_sidecar_integrity_gate_public_summary,
)
from core.provenance.state import (
    build_provenance_report,
    expected_revisions_payload,
    provenance_facts_from_state,
    provenance_contract_identity,
    preflight_write_binding_hash,
)
from core.protocol.contracts import FILE_KEYS, ROOT_ENTRY_CANDIDATES
from core.protocol.markers import parse_file_state_marker
from core.recording import (
    digest_payload,
    LAYER_NAMES,
    RECORD_CLASSES,
    RecordContractError,
    normalize_layer_hint,
    normalize_record_class_hint,
    plan_record,
    build_recording_suggestion,
    validate_record_plan_output,
)
from core.support.policy import action_level_for_dispatcher
from core.trust.state import evaluate_trust_state

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    DailyLogCursorError,
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
    startup_scratch_residue_error_message,
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
            "quick-summary, append, write, post-append summary sync, and bridge preview flows."
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
        help=(
            "Legacy post-init bridge-apply target selector. It is currently unsupported and "
            "blocked; omit --bridge and --yes to inspect bridge candidates in normal init output."
        ),
    )
    init_parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Legacy bridge-apply confirmation flag. It is currently unsupported and blocked; "
            "omit --bridge and --yes to inspect bridge candidates in normal init output."
        ),
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
    validate_parser.add_argument(
        "--require-provenance",
        action="store_true",
        help="Require explicit helper receipt evidence in addition to structural validation.",
    )
    validate_scope = validate_parser.add_mutually_exclusive_group()
    validate_scope.add_argument(
        "--changed-only",
        action="store_true",
        help="With --require-provenance, verify bounded current receipt-store evidence.",
    )
    validate_scope.add_argument(
        "--full",
        action="store_true",
        help=(
            "With --require-provenance, run the bounded current receipt-store audit. "
            "This MVP does not perform historical receipt-chain validation."
        ),
    )

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
        default=DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
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
        default=DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR,
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
    status_parser.add_argument(
        "--expanded",
        action="store_true",
        help=(
            "Opt into the expanded status lane that may inspect context_brief.md, "
            "update_protocol.md, and latest daily-log content."
        ),
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

    record_parser = subparsers.add_parser(
        "record",
        help="Plan a side-effect-free recording workflow and return the next safe step.",
    )
    record_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    record_parser.add_argument(
        "--plan",
        action="store_true",
        help="Return a plan-only workflow. This is the current v0.4.6-line record mode.",
    )
    record_parser.add_argument(
        "--suggest",
        action="store_true",
        help=(
            "Return a side-effect-free proactive recording suggestion with a sanitized "
            "candidate summary and suggested record --plan path."
        ),
    )
    record_parser.add_argument(
        "--intent-text",
        default="",
        help="Original user or agent recording intent. Public-safe text only.",
    )
    record_parser.add_argument(
        "--payload-json",
        help="Prepared record payload JSON object. Do not include private raw payloads.",
    )
    record_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the prepared record payload JSON object from UTF-8 stdin.",
    )
    record_parser.add_argument(
        "--max-input-bytes",
        type=positive_int,
        help="Maximum prepared record payload size in bytes when reading stdin.",
    )
    record_parser.add_argument(
        "--record-class",
        help=(
            "Optional classifier hint. Accepted canonical classes include: "
            + ", ".join(RECORD_CLASSES)
        ),
    )
    record_parser.add_argument(
        "--layer-hint",
        help=(
            "Optional target layer hint. Accepted canonical layers include: "
            + ", ".join(LAYER_NAMES)
        ),
    )
    record_parser.add_argument(
        "--privacy-safety-json",
        help="Optional privacy/safety result JSON object such as {\"classification\":\"safe\"}.",
    )
    record_parser.add_argument(
        "--expected-revision-json",
        help="Optional expected revision binding JSON object. Usually supplied by preflight.",
    )
    semantic_group = record_parser.add_mutually_exclusive_group()
    semantic_group.add_argument(
        "--semantic-unchanged",
        action="store_true",
        help="Assert semantic unchanged for a later metadata-only refresh plan.",
    )
    semantic_group.add_argument(
        "--semantic-changed",
        action="store_true",
        help="Assert semantic changed, blocking metadata-only refresh use.",
    )
    record_parser.add_argument(
        "--compact",
        action="store_true",
        help="With --json, print compact record-plan output with safety receipt.",
    )
    record_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

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
        "--confirm-review-imported-baseline",
        action="store_true",
        help=(
            "Confirm a review_imported_baseline ask gate before a mutating daily-log append. "
            "Only use after reviewing the preflight readiness output."
        ),
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
        "--confirm-review-imported-baseline",
        action="store_true",
        help=(
            "Confirm a review_imported_baseline ask gate before a mutating current-state write. "
            "Only use after reviewing the preflight readiness output."
        ),
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
        "--reuse-current-summary",
        action="store_true",
        help=(
            "Reuse the current rolling_summary managed body and refresh only metadata. "
            "Requires --semantic-unchanged-assertion-json."
        ),
    )
    post_append_sync_parser.add_argument(
        "--semantic-unchanged-assertion-json",
        help=(
            "Bound semantic-unchanged assertion JSON for --reuse-current-summary. "
            "Must echo the preflight metadata_only_refresh assertion binding seed."
        ),
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
        "--confirm-review-imported-baseline",
        action="store_true",
        help=(
            "Confirm a review_imported_baseline ask gate before a post-append current-state sync. "
            "Only use after reviewing the preflight post_append_summary_sync contract."
        ),
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

    repair_parser = subparsers.add_parser(
        "repair-daily-log-cursor",
        help="Preview or apply the structural daily-log cursor repair helper.",
    )
    repair_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cursor repair. Requires --yes.",
    )
    repair_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the explicit --apply repair.",
    )
    repair_parser.add_argument(
        "--expected-workspace-revision",
        type=int,
        help="Optional revision guard forwarded to repair_daily_log_cursor.py apply mode.",
    )
    repair_parser.add_argument(
        "--preview-digest",
        help="Optional fresh preview digest forwarded to repair_daily_log_cursor.py apply mode.",
    )
    repair_parser.add_argument("--json", action="store_true", help="Print structured JSON output.")

    bridge_parser = subparsers.add_parser(
        "bridge",
        help=(
            "Preview adding, updating, or removing a RecallLoom thin bridge. Bridge apply is "
            "currently unsupported and blocked."
        ),
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
        help="Preview removal of managed bridge blocks instead of an add/update preview.",
    )
    bridge_parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Legacy bridge-apply confirmation flag. It is currently unsupported and blocked; "
            "bridge remains preview-only."
        ),
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
    public_payload = _with_package_support(payload, support)
    if not json_mode and isinstance(public_payload, dict):
        confirmation_material = public_payload.get("confirmation_material")
        if isinstance(confirmation_material, dict):
            public_message = redact_public_text(message, project_root=None) or message
            print(public_message, file=sys.stderr)
            print_confirmation_material(confirmation_material, file=sys.stderr)
            raise SystemExit(exit_code)
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        payload=public_payload,
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
        message = startup_scratch_residue_error_message(report)
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
    stdin_text: str | None = None,
) -> dict:
    cmd = [sys.executable, str(SCRIPT_DIR / helper_name), *helper_args, "--json"]
    proc = subprocess.run(cmd, text=True, capture_output=True, input=stdin_text, check=False)
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
    if getattr(args, "expanded", False):
        helper_args.append("--expanded")
    return helper_args


def _validate_helper_args(args: argparse.Namespace) -> list[str]:
    helper_args = [args.target]
    if args.require_provenance:
        helper_args.append("--require-provenance")
    if args.changed_only:
        helper_args.append("--changed-only")
    if args.full:
        helper_args.append("--full")
    return helper_args


def _validate_provenance_scope_failure_payload(message: str, *, reason_code: str) -> dict:
    return cli_failure_payload(
        "invalid_prepared_input",
        error=message,
        details={
            "command": "validate",
            "reason_code": reason_code,
            "side_effect": "none",
        },
        extra={
            "next_actions": ["choose_validate_provenance_scope", "retry_validate"],
            "suggestion": (
                "Use --require-provenance with exactly one scope flag: --changed-only "
                "or --full. Omit all provenance flags for structural-only validation."
            ),
            "recovery_command": (
                "recallloom.py validate <project-path> --require-provenance "
                "--changed-only --json"
            ),
            "operator_note": (
                "Explicit provenance validation requires --require-provenance plus "
                "exactly one scope flag."
            ),
        },
    )


def _validate_provenance_scope_args(parser, args: argparse.Namespace) -> None:
    scope_count = int(bool(args.changed_only)) + int(bool(args.full))
    if args.require_provenance:
        if scope_count != 1:
            message = "--require-provenance requires exactly one scope flag: --changed-only or --full."
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=_validate_provenance_scope_failure_payload(
                    message,
                    reason_code="provenance_scope_required",
                ),
            )
        return
    if scope_count:
        message = "--changed-only and --full require --require-provenance."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=_validate_provenance_scope_failure_payload(
                message,
                reason_code="provenance_scope_without_requirement",
            ),
        )


def _append_helper_args(
    parser,
    args: argparse.Namespace,
    *,
    preflight_payload: dict,
    support: dict,
) -> list[str]:
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
    expected_workspace_revision = (
        args.expected_workspace_revision
        if args.expected_workspace_revision is not None
        else _append_expected_workspace_revision(preflight_payload)
    )
    helper_args.extend(["--expected-workspace-revision", str(expected_workspace_revision)])
    if _append_write_readiness_label(preflight_payload) in {
        "structural_only_ready_after_preflight",
        "helper_evidenced_ready_after_preflight",
        "review_imported_baseline_ready_after_preflight",
    }:
        helper_args.extend(
            [
                "--preflight-binding-json",
                _issue_preflight_binding_json(
                    parser,
                    args,
                    preflight_payload=preflight_payload,
                    binding_json=_append_preflight_binding_json(
                        expected_workspace_revision=expected_workspace_revision,
                        preflight_payload=preflight_payload,
                        target_date=args.date,
                        confirm_review_imported_baseline=args.confirm_review_imported_baseline,
                    ),
                    support=support,
                ),
            ]
        )
    return helper_args


def _append_dispatcher_input_mode(args: argparse.Namespace) -> str | None:
    selected_sources = int(args.entry_json is not None) + int(args.entry_file is not None) + int(args.stdin)
    if selected_sources == 0:
        return "missing"
    if selected_sources > 1:
        return "ambiguous"
    input_format = args.input_format or "auto"
    if args.entry_json is not None:
        return "json-string"
    if args.entry_file is not None:
        return "json-file" if input_format == "json" else "file"
    if args.stdin:
        return "json-stdin" if input_format == "json" else "stdin"
    return None


def _append_retry_payload(
    args: argparse.Namespace,
    *,
    extra_args: list[str] | None = None,
    requires_repair_command_first: bool = False,
) -> dict:
    input_mode = _append_dispatcher_input_mode(args) or "missing"
    input_format = args.input_format or "auto"
    input_args: list[str]
    input_ref: str
    if input_mode == "json-string":
        input_args = ["--entry-json", "<prepared-entry-json>"]
        input_ref = "same_entry_json_payload"
    elif input_mode == "json-file":
        input_args = ["--entry-file", "<entry.json>", "--input-format", "json"]
        input_ref = "same_prepared_entry_file"
    elif input_mode == "json-stdin":
        input_args = ["--stdin", "--input-format", "json"]
        input_ref = "resubmit_same_stdin_payload"
    elif input_mode == "stdin":
        input_args = ["--stdin"]
        input_ref = "resubmit_same_stdin_payload"
    elif input_mode in {"ambiguous", "missing"}:
        input_args = ["--entry-json", "<prepared-entry-json>"]
        input_ref = "choose_one_prepared_entry_source"
    else:
        input_args = ["--entry-file", "<prepared-entry.md>"]
        input_ref = "same_prepared_entry_file"
    if args.max_input_bytes is not None:
        input_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    if args.date is not None:
        input_args.extend(["--date", args.date])
    if args.allow_historical:
        input_args.append("--allow-historical")
    if args.no_auto_detect:
        input_args.append("--no-auto-detect")
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
    return {
        "command": "recallloom.py append",
        "project_ref": "same_project",
        "input_mode": input_mode,
        "input_ref": input_ref,
        "input_format": input_format,
        "argv_template": [
            "recallloom.py",
            "append",
            "same_project",
            *input_args,
            *writer_args,
            *(extra_args or []),
            "--json",
        ],
        "requires_repair_command_first": requires_repair_command_first,
        "side_effect": "none_until_retry",
        **writer_fields,
    }


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


def _write_argument_failure_details(
    args: argparse.Namespace,
    *,
    write_type: str | None = None,
    file_key: str | None = None,
    input_mode: str | None = None,
    prepared_input_builder: str | None = None,
    reason_code: str = "write_argument_invalid",
    extra: dict | None = None,
) -> dict:
    details = {
        "command": "write",
        "operation": "managed_file_commit",
        "reason_code": reason_code,
        "side_effect": "none",
        **(extra or {}),
    }
    routing_write_type = write_type if write_type in WRITE_TYPE_FILE_KEYS else args.write_type
    if routing_write_type in WRITE_TYPE_FILE_KEYS:
        details["write_type"] = routing_write_type
        details["file_key"] = WRITE_TYPE_FILE_KEYS[routing_write_type]
    elif file_key is not None:
        details["file_key"] = file_key
    if input_mode in {"file", "json-file", "json-stdin", "json-string", "stdin"}:
        details["input_mode"] = input_mode
    if prepared_input_builder == "rolling_summary_json":
        details["prepared_input_builder"] = prepared_input_builder
    return details


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
            details=_write_argument_failure_details(
                args,
                reason_code="missing_write_type",
                extra={
                    "accepted_write_types": sorted(WRITE_TYPE_FILE_KEYS),
                    "phase_1_infers_target": False,
                },
            ),
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
            details=_write_argument_failure_details(
                args,
                reason_code="unsupported_write_type",
                extra={
                    "accepted_write_types": sorted(WRITE_TYPE_FILE_KEYS),
                    "received_write_type": args.write_type,
                    "phase_1_infers_target": False,
                },
            ),
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
            details=_write_argument_failure_details(
                args,
                file_key=file_key,
                reason_code=(
                    "both_input_sources"
                    if args.source_file is not None and args.stdin
                    else "missing_input_source"
                ),
                extra={
                    "input_contract": "source-file_xor_stdin",
                    "source_file_present": args.source_file is not None,
                    "stdin_present": bool(args.stdin),
                },
            ),
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
            details=_write_argument_failure_details(
                args,
                write_type="current-state",
                input_mode=f"json-{input_mode}",
                prepared_input_builder="rolling_summary_json",
                reason_code="json_input_requires_current_state",
                extra={
                    "input_format": "json",
                    "received_write_type": args.write_type,
                    "accepted_write_type": "current-state",
                },
            ),
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


def _issue_preflight_binding_json(
    parser,
    args: argparse.Namespace,
    *,
    preflight_payload: dict,
    binding_json: str,
    support: dict,
) -> str:
    try:
        binding = json.loads(binding_json)
    except json.JSONDecodeError as exc:
        message = f"Dispatcher generated an invalid preflight binding: {exc.msg}."
        _exit_with_support(
            parser,
            json_mode=getattr(args, "json", False),
            exit_code=2,
            message=message,
            payload=cli_failure_payload("registry_contract_invalid", error=message),
            support=support,
        )
    if not isinstance(binding, dict):
        message = "Dispatcher generated a non-object preflight binding."
        _exit_with_support(
            parser,
            json_mode=getattr(args, "json", False),
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
            json_mode=getattr(args, "json", False),
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
            support=support,
        )
    if workspace is None:
        message = "Preflight binding target is no longer attached to RecallLoom."
        _exit_with_support(
            parser,
            json_mode=getattr(args, "json", False),
            exit_code=2,
            message=message,
            payload=cli_failure_payload("no_project_root", error=message),
            support=support,
        )
    strict_gate = strict_sidecar_integrity_gate(
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )
    if strict_gate.get("allowed_for_mutation") is not True:
        gate_summary = strict_sidecar_integrity_gate_public_summary(strict_gate)
        message = (
            "Strict sidecar integrity gate blocked preflight binding issuance. "
            "Review or repair sidecar evidence before any mutating helper write."
        )
        _exit_with_support(
            parser,
            json_mode=getattr(args, "json", False),
            exit_code=3,
            message=message,
            payload=cli_failure_payload(
                "trust_review_required",
                error=message,
                details={
                    **_preflight_gate_details(preflight_payload),
                    "reason_code": "strict_sidecar_integrity_failed",
                    "strict_gate_reason_code": gate_summary.get("reason_code"),
                    "strict_sidecar_integrity_gate": gate_summary,
                    "command": getattr(args, "command", None),
                    "operation_class": binding.get("operation_class"),
                    "file_key": binding.get("file_key"),
                    "side_effect": "none",
                    "lease_store": "derived/preflight-bindings.json",
                },
                extra=strict_sidecar_no_write_failure_extra_from_summary(
                    gate_summary,
                    continuity_confidence="broken",
                    include_recovery_actions=False,
                ),
            ),
            support=support,
        )
    try:
        write_preflight_binding_lease(
            storage_root=workspace.storage_root,
            project_root=workspace.project_root,
            binding=binding,
            preflight_payload=preflight_payload,
            issued_by="recallloom.py",
        )
    except PreflightBindingLeaseError as exc:
        message = str(exc)
        _exit_with_support(
            parser,
            json_mode=getattr(args, "json", False),
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "damaged_sidecar",
                error=message,
                details={
                    **exc.details,
                    "side_effect": "none",
                    "lease_store": "derived/preflight-bindings.json",
                },
            ),
            support=support,
        )
    return binding_json


def _preflight_gate_details(preflight_payload: dict) -> dict:
    detail_keys = (
        "allowed_operation_level",
        "summary_stale",
        "continuity_drift_risk_level",
        "freshness_risk_level",
        "recommended_actions",
        "continuity_confidence",
        "continuity_state",
        "provenance_state",
        "write_readiness",
        "expected_revisions",
        "strict_sidecar_integrity_gate",
        "preflight_contract_identity",
        "workspace_newer_than_summary",
        "summary_revision_stale",
    )
    return {key: preflight_payload.get(key) for key in detail_keys if key in preflight_payload}


def _strict_preflight_blocked(preflight_payload: dict) -> bool:
    strict_gate = preflight_payload.get("strict_sidecar_integrity_gate")
    if isinstance(strict_gate, dict) and strict_gate.get("allowed_for_mutation") is False:
        return True
    return preflight_payload.get("write_context_blocked_reason") == "strict_sidecar_integrity_failed"


def _effective_allowed_operation_level_for_strict_gate(
    allowed_operation_level: str,
    strict_gate: dict,
) -> str:
    if (
        strict_gate.get("allowed_for_mutation") is not True
        and allowed_operation_level == "write_current_state_after_preflight"
    ):
        return "read_current_state"
    return allowed_operation_level


def _strict_gate_read_actions(actions: list[str], *, strict_gate_allows_write: bool) -> list[str]:
    if strict_gate_allows_write:
        return actions
    blocked_write_actions = {
        "seed_initial_continuity",
        "update_rolling_summary",
        "consider_refresh_summary",
        "refresh_or_review_summary_before_write",
    }
    review_action = "review_or_repair_sidecar_before_write"
    return [
        review_action,
        *(
            action
            for action in actions
            if action not in blocked_write_actions
            and action != review_action
        ),
    ]


def _strict_gate_read_failure_extra(workspace) -> dict[str, object]:
    return strict_sidecar_no_write_failure_extra(
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        continuity_confidence="broken",
    )


def _strict_preflight_failure_details(
    preflight_payload: dict,
    *,
    command: str,
    operation: str,
    file_key: str,
    extra_details: dict | None = None,
) -> dict:
    strict_gate = preflight_payload.get("strict_sidecar_integrity_gate")
    strict_gate_reason_code = (
        strict_gate.get("reason_code") if isinstance(strict_gate, dict) else None
    )
    return {
        **_preflight_gate_details(preflight_payload),
        "reason_code": "strict_sidecar_integrity_failed",
        "strict_gate_reason_code": strict_gate_reason_code,
        "command": command,
        "operation": operation,
        "file_key": file_key,
        "side_effect": "none",
        **(extra_details or {}),
    }


def _strict_preflight_failure_extra(preflight_payload: dict) -> dict[str, object]:
    strict_gate = preflight_payload.get("strict_sidecar_integrity_gate")
    gate_summary = strict_gate if isinstance(strict_gate, dict) else {
        "allowed_for_mutation": False,
        "blocked_reason": "strict_sidecar_integrity_failed",
        "reason_code": "strict_gate_unavailable",
        "safe_next_action": (
            "Keep the sidecar read-only until the integrity mismatch is reviewed and repaired."
        ),
    }
    return strict_sidecar_no_write_failure_extra_from_summary(
        gate_summary,
        continuity_confidence=str(preflight_payload.get("continuity_confidence") or "broken"),
        include_recovery_actions=False,
    )


def _exit_strict_preflight_failure(
    parser,
    args: argparse.Namespace,
    *,
    preflight_payload: dict,
    support: dict,
    command: str,
    operation: str,
    file_key: str,
    extra_details: dict | None = None,
) -> None:
    message = (
        "Strict sidecar integrity gate blocked this mutating action. "
        "Review or repair sidecar evidence before append/write/sync."
    )
    _exit_with_support(
        parser,
        json_mode=getattr(args, "json", False),
        exit_code=3,
        message=message,
        payload=cli_failure_payload(
            "trust_review_required",
            error=message,
            details=_strict_preflight_failure_details(
                preflight_payload,
                command=command,
                operation=operation,
                file_key=file_key,
                extra_details=extra_details,
            ),
            extra=_strict_preflight_failure_extra(preflight_payload),
        ),
        support=support,
    )


def _write_retry_payload(
    args: argparse.Namespace,
    *,
    file_key: str,
    input_mode: str,
    extra_args: list[str] | None = None,
    requires_repair_command_first: bool = True,
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
            *(extra_args or []),
            "--json",
        ],
        "requires_repair_command_first": requires_repair_command_first,
        "side_effect": "none_until_retry",
        **writer_fields,
    }


def _confirmation_target_for_file_key(file_key: str) -> str:
    return {
        "rolling_summary": "current_state",
        "context_brief": "stable_context",
        "update_protocol": "protocol_rule",
    }.get(file_key, "current_state")


def _write_confirmation_material(*, file_key: str) -> dict:
    target_layer = _confirmation_target_for_file_key(file_key)
    return review_imported_baseline_confirmation_material(
        command="write",
        operation="managed_file_commit",
        target_layer_or_surface=target_layer,
        expected_side_effect="managed_file_write",
        files_or_keys_summary=f"{file_key} {target_layer} key",
        approval_scope=(
            f"Approve only this revision-checked {target_layer} write for {file_key}; "
            "no append, sync, repair, validation or release action is included."
        ),
        debug_reference=f"write:{file_key}:review_imported_baseline",
    )


def _write_preflight_failure_payload(
    args: argparse.Namespace,
    *,
    file_key: str,
    input_mode: str,
    message: str,
    details: dict,
    reason: str = "stale_write_context",
    retry_extra_args: list[str] | None = None,
    retry_requires_repair: bool = True,
) -> dict:
    # Keep repair_command tied to the public failure contract while letting
    # retry_payload pass through the contract's final publicization step.
    base_payload = cli_failure_payload(
        reason,
        error=message,
        details=details,
    )
    extra = {
        "repair_command": base_payload["recovery_command"],
        "retry_payload": _write_retry_payload(
            args,
            file_key=file_key,
            input_mode=input_mode,
            extra_args=retry_extra_args,
            requires_repair_command_first=retry_requires_repair,
        ),
    }
    if reason == "review_imported_baseline_confirmation_required":
        extra["confirmation_material"] = _write_confirmation_material(file_key=file_key)
    return cli_failure_payload(
        reason,
        error=message,
        details=details,
        extra=extra,
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
    readiness_label = _write_readiness_label(preflight_payload)
    allowed_readiness = {
        "structural_only_ready_after_preflight",
        "helper_evidenced_ready_after_preflight",
        "review_imported_baseline_ready_after_preflight",
    }
    if _strict_preflight_blocked(preflight_payload):
        _exit_strict_preflight_failure(
            parser,
            args,
            preflight_payload=preflight_payload,
            support=support,
            command="write",
            operation="managed_file_commit",
            file_key=file_key,
            extra_details={"input_mode": input_mode},
        )
    current_state_refresh_allowed = _current_state_refresh_allowed(
        file_key=file_key,
        write_type=args.write_type,
        preflight_payload=preflight_payload,
    )
    if (
        allowed_operation_level == "write_current_state_after_preflight"
        and summary_stale is False
        and readiness_label in allowed_readiness
    ) or current_state_refresh_allowed:
        if (
            _binding_write_readiness_label(
                file_key=file_key,
                write_type=args.write_type,
                preflight_payload=preflight_payload,
            )
            == "review_imported_baseline_ready_after_preflight"
            and not args.dry_run
            and not getattr(args, "confirm_review_imported_baseline", False)
        ):
            message = (
                "Preflight returned an ask gate for review_imported_baseline. "
                "Review the readiness output and rerun with --confirm-review-imported-baseline "
                "to confirm this mutating write."
            )
            payload = _write_preflight_failure_payload(
                args,
                file_key=file_key,
                input_mode=input_mode,
                message=message,
                reason="review_imported_baseline_confirmation_required",
                retry_extra_args=["--confirm-review-imported-baseline"],
                retry_requires_repair=False,
                details={
                    **_preflight_gate_details(preflight_payload),
                    "reason_code": "review_imported_baseline_confirmation_required",
                    "side_effect": "none",
                    "required_flag": "--confirm-review-imported-baseline",
                },
            )
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=3,
                message=message,
                payload=payload,
                support=support,
            )
        return

    message = (
        "Preflight requires review before write. recallloom.py write only proceeds when "
        "allowed_operation_level is write_current_state_after_preflight, summary_stale is false, "
        "and provenance write_readiness allows a revision-checked helper write."
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


def _enforce_append_preflight_gate(
    parser,
    args: argparse.Namespace,
    *,
    preflight_payload: dict,
    support: dict,
) -> None:
    if _strict_preflight_blocked(preflight_payload):
        _exit_strict_preflight_failure(
            parser,
            args,
            preflight_payload=preflight_payload,
            support=support,
            command="append",
            operation="daily_log_append",
            file_key="daily_log",
        )
    provenance_state = preflight_payload.get("provenance_state")
    readiness_label = _append_write_readiness_label(preflight_payload)
    if provenance_state == "review_imported_baseline":
        if readiness_label != "review_imported_baseline_ready_after_preflight":
            message = (
                "Preflight did not authorize a revision-checked append for "
                "review_imported_baseline. Rerun preflight after resolving freshness or "
                "continuity review warnings."
            )
            payload = cli_failure_payload(
                "stale_write_context",
                error=message,
                details={
                    **_preflight_gate_details(preflight_payload),
                    "reason_code": "review_imported_baseline_append_not_authorized",
                    "command": "append",
                    "side_effect": "none",
                },
            )
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=3,
                message=message,
                payload=payload,
                support=support,
            )
        if not getattr(args, "confirm_review_imported_baseline", False):
            message = (
                "Preflight returned an ask gate for review_imported_baseline. "
                "Review the readiness output and rerun with --confirm-review-imported-baseline "
                "to confirm this mutating append."
            )
            input_mode = _append_dispatcher_input_mode(args)
            input_format = args.input_format or "auto"
            confirmation_material = review_imported_baseline_confirmation_material(
                command="append",
                operation="daily_log_append",
                target_layer_or_surface="daily_log",
                expected_side_effect="single_append",
                files_or_keys_summary="latest daily_log cursor",
                approval_scope=(
                    "Approve only this revision-checked daily-log append; no current-state "
                    "sync, repair, validation or release action is included."
                ),
                debug_reference="append:daily_log:review_imported_baseline",
            )
            payload = cli_failure_payload(
                "review_imported_baseline_confirmation_required",
                error=message,
                details={
                    **_preflight_gate_details(preflight_payload),
                    "reason_code": "review_imported_baseline_confirmation_required",
                    "command": "append",
                    "operation": "daily_log_append",
                    **({"input_mode": input_mode} if input_mode is not None else {}),
                    "input_format": input_format,
                    "side_effect": "none",
                    "required_flag": "--confirm-review-imported-baseline",
                },
                extra={
                    "retry_payload": _append_retry_payload(
                        args,
                        extra_args=["--confirm-review-imported-baseline"],
                        requires_repair_command_first=False,
                    ),
                    "confirmation_material": confirmation_material,
                },
            )
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=3,
                message=message,
                payload=payload,
                support=support,
            )
        return
    if readiness_label in {
        "structural_only_ready_after_preflight",
        "helper_evidenced_ready_after_preflight",
    }:
        return

    if provenance_state not in {"review_required", "structurally_valid_legacy"} and (
        readiness_label not in {"review_required", "readable_legacy", "blocked"}
    ):
        message = (
            "Preflight did not authorize a revision-checked append. Refresh current "
            "state or resolve the write-readiness warning before appending."
        )
        payload = cli_failure_payload(
            "stale_write_context",
            error=message,
            details={
                **_preflight_gate_details(preflight_payload),
                "reason_code": "append_write_readiness_not_authorized",
                "command": "append",
                "side_effect": "none",
            },
        )
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=payload,
            support=support,
        )

    message = (
        "Preflight requires provenance review before append. recallloom.py append only "
        "delegates to append_daily_log_entry.py when the sidecar is not in a legacy "
        "review-required state."
    )
    payload = cli_failure_payload(
        "trust_review_required",
        error=message,
        details={
            **_preflight_gate_details(preflight_payload),
            "command": "append",
            "side_effect": "none",
            "next_actions": [
                "stage_recovery_proposal.py",
                "record_recovery_review.py",
                "prepare_recovery_promotion.py",
                "preflight_context_check.py",
            ],
        },
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


def _write_readiness_label(preflight_payload: dict) -> str | None:
    write_readiness = preflight_payload.get("write_readiness")
    if isinstance(write_readiness, dict):
        readiness = write_readiness.get("readiness")
        return readiness if isinstance(readiness, str) else None
    return None


def _strict_preflight_daily_log_receipts_verified(preflight_payload: dict) -> bool:
    strict_gate = preflight_payload.get("strict_sidecar_integrity_gate")
    if not isinstance(strict_gate, dict) or strict_gate.get("allowed_for_mutation") is not True:
        return False
    verified_file_keys = strict_gate.get("verified_current_file_keys")
    receipt_store = strict_gate.get("receipt_store")
    target_file_keys = (
        receipt_store.get("target_file_keys") if isinstance(receipt_store, dict) else None
    )
    return (
        isinstance(verified_file_keys, list)
        and "daily_log" in verified_file_keys
        and isinstance(receipt_store, dict)
        and receipt_store.get("verified") is True
        and isinstance(target_file_keys, list)
        and "daily_log" in target_file_keys
    )


def _append_write_readiness_label(preflight_payload: dict) -> str | None:
    readiness_label = _write_readiness_label(preflight_payload)
    if _is_ready_after_preflight_label(readiness_label):
        return readiness_label
    if (
        preflight_payload.get("provenance_state") == "helper_evidenced"
        and readiness_label == "helper_evidenced"
        and _strict_preflight_daily_log_receipts_verified(preflight_payload)
    ):
        return "helper_evidenced_ready_after_preflight"
    return readiness_label


def _is_ready_after_preflight_label(label: str | None) -> bool:
    return label in {
        "structural_only_ready_after_preflight",
        "helper_evidenced_ready_after_preflight",
        "review_imported_baseline_ready_after_preflight",
    }


def _ready_after_preflight_label_for_provenance(provenance_state: object) -> str | None:
    if provenance_state in {"structurally_valid", "unproven_sidecar_state"}:
        return "structural_only_ready_after_preflight"
    if provenance_state == "helper_evidenced":
        return "helper_evidenced_ready_after_preflight"
    if provenance_state == "review_imported_baseline":
        return "review_imported_baseline_ready_after_preflight"
    return None


def _current_state_refresh_allowed(
    *,
    file_key: str,
    write_type: str,
    preflight_payload: dict,
) -> bool:
    if file_key != "rolling_summary" or write_type != "current-state":
        return False
    if preflight_payload.get("summary_stale") is not True:
        return False
    if preflight_payload.get("workspace_newer_than_summary") is not True:
        return False
    if preflight_payload.get("write_context_authorized") is False:
        return False
    if "update_rolling_summary" not in preflight_payload.get("recommended_actions", []):
        return False
    if _ready_after_preflight_label_for_provenance(preflight_payload.get("provenance_state")) is None:
        return False
    safe_write_context = preflight_payload.get("safe_write_context")
    commit_contexts = (
        safe_write_context.get("commit_context_file")
        if isinstance(safe_write_context, dict)
        else None
    )
    rolling_summary_context = (
        commit_contexts.get("rolling_summary")
        if isinstance(commit_contexts, dict)
        else None
    )
    return (
        isinstance(rolling_summary_context, dict)
        and isinstance(rolling_summary_context.get("expected_file_revision"), int)
        and isinstance(rolling_summary_context.get("expected_workspace_revision"), int)
    )


def _binding_write_readiness_label(
    *,
    file_key: str,
    write_type: str,
    preflight_payload: dict,
) -> str | None:
    readiness_label = _write_readiness_label(preflight_payload)
    if _is_ready_after_preflight_label(readiness_label):
        return readiness_label
    if _current_state_refresh_allowed(
        file_key=file_key,
        write_type=write_type,
        preflight_payload=preflight_payload,
    ):
        return _ready_after_preflight_label_for_provenance(
            preflight_payload.get("provenance_state")
        )
    return readiness_label


def _append_expected_workspace_revision(preflight_payload: dict) -> int:
    safe_write_context = preflight_payload.get("safe_write_context")
    if isinstance(safe_write_context, dict):
        append_context = safe_write_context.get("append_daily_log_entry")
        if isinstance(append_context, dict):
            revision = append_context.get("expected_workspace_revision")
            if isinstance(revision, int):
                return revision
    expected_revisions = preflight_payload.get("expected_revisions")
    if isinstance(expected_revisions, dict):
        revision = expected_revisions.get("workspace_revision")
        if isinstance(revision, int):
            return revision
    revision = preflight_payload.get("workspace_revision")
    if isinstance(revision, int):
        return revision
    raise ConfigContractError("Preflight payload is missing append expected_workspace_revision.")


def _append_preflight_binding_json(
    *,
    expected_workspace_revision: int,
    preflight_payload: dict,
    target_date: str | None,
    confirm_review_imported_baseline: bool = False,
) -> str:
    write_readiness = preflight_payload.get("write_readiness")
    safe_write_context = preflight_payload.get("safe_write_context")
    append_context = (
        safe_write_context.get("append_daily_log_entry")
        if isinstance(safe_write_context, dict)
        else {}
    )
    if not isinstance(append_context, dict):
        append_context = {}
    binding = {
        "binding_type": "recallloom.preflight_write_binding",
        "binding_version": "0.1",
        "operation_class": "daily_log_append",
        "file_key": "daily_log",
        "write_type": "milestone_evidence",
        "target_date": target_date or append_context.get("suggested_date"),
        "latest_file": append_context.get("latest_file"),
        "latest_entry_id": append_context.get("latest_entry_id"),
        "latest_entry_seq": append_context.get("latest_entry_seq"),
        "entry_count": append_context.get("entry_count"),
        "latest_file_digest": append_context.get("latest_file_digest"),
        "expected_workspace_revision": expected_workspace_revision,
        "preflight_contract_identity": (
            preflight_payload.get("preflight_contract_identity")
            or provenance_contract_identity()
        ),
        "provenance_state": preflight_payload.get("provenance_state"),
        "write_readiness_label": _append_write_readiness_label(preflight_payload),
    }
    if isinstance(write_readiness, dict):
        for key in ("ux_gate", "ux_gate_requires_confirmation", "ux_gate_reason"):
            if key in write_readiness:
                binding[key] = write_readiness[key]
    if confirm_review_imported_baseline:
        binding["ux_gate_confirmation"] = "review_imported_baseline_confirmed"
    expected_revisions = preflight_payload.get("expected_revisions")
    if isinstance(expected_revisions, dict):
        binding["expected_revisions"] = expected_revisions
    binding["preflight_contract_hash"] = preflight_write_binding_hash(binding)
    return json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _preflight_write_binding_json(
    *,
    file_key: str,
    write_type: str,
    operation_class: str,
    expected_file_revision: int,
    expected_workspace_revision: int,
    preflight_payload: dict,
    contract_type: str | None = None,
    confirm_review_imported_baseline: bool = False,
    write_readiness_label: str | None = None,
    extra_binding_fields: dict | None = None,
) -> str:
    write_readiness = preflight_payload.get("write_readiness")
    binding = {
        "binding_type": "recallloom.preflight_write_binding",
        "binding_version": "0.1",
        "operation_class": operation_class,
        "file_key": file_key,
        "write_type": write_type,
        "expected_file_revision": expected_file_revision,
        "expected_workspace_revision": expected_workspace_revision,
        "preflight_contract_identity": (
            preflight_payload.get("preflight_contract_identity")
            or provenance_contract_identity()
        ),
        "provenance_state": preflight_payload.get("provenance_state"),
        "write_readiness_label": write_readiness_label or _write_readiness_label(preflight_payload),
    }
    if isinstance(write_readiness, dict):
        for key in ("ux_gate", "ux_gate_requires_confirmation", "ux_gate_reason"):
            if key in write_readiness:
                binding[key] = write_readiness[key]
    if confirm_review_imported_baseline:
        binding["ux_gate_confirmation"] = "review_imported_baseline_confirmed"
    expected_revisions = preflight_payload.get("expected_revisions")
    if isinstance(expected_revisions, dict):
        binding["expected_revisions"] = expected_revisions
    if contract_type is not None:
        binding["contract_type"] = contract_type
    if extra_binding_fields:
        binding.update(extra_binding_fields)
    binding["preflight_contract_hash"] = preflight_write_binding_hash(binding)
    return json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _commit_context_file_args(
    parser,
    args: argparse.Namespace,
    *,
    file_key: str,
    write_context: dict,
    preflight_payload: dict,
    support: dict,
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
    helper_args.extend(
        [
            "--preflight-binding-json",
            _issue_preflight_binding_json(
                parser,
                args,
                preflight_payload=preflight_payload,
                binding_json=_preflight_write_binding_json(
                    file_key=file_key,
                    write_type=args.write_type,
                    operation_class="managed_file_commit",
                    expected_file_revision=write_context["expected_file_revision"],
                    expected_workspace_revision=write_context["expected_workspace_revision"],
                    preflight_payload=preflight_payload,
                    confirm_review_imported_baseline=args.confirm_review_imported_baseline,
                    write_readiness_label=_binding_write_readiness_label(
                        file_key=file_key,
                        write_type=args.write_type,
                        preflight_payload=preflight_payload,
                    ),
                ),
                support=support,
            ),
        ]
    )
    return helper_args


POST_APPEND_SYNC_COMMAND = "sync-current-state-after-append"
POST_APPEND_SYNC_CONTRACT_TYPE = "post_append_summary_sync"
POST_APPEND_SYNC_NOOP_REASONS = frozenset(
    {
        "summary_not_stale",
        "summary_revision_not_stale",
    }
)


def _post_append_sync_user_summary(
    *,
    category: str,
    conclusion: str,
    reason: str,
    next_step: str,
) -> dict:
    return {
        "category": category,
        "conclusion": conclusion,
        "reason": reason,
        "next_step": next_step,
    }


def _post_append_sync_success_summary(*, input_mode: str) -> dict:
    if input_mode == "reuse-current-summary":
        reason = (
            "The authorized append was reconciled as metadata-only; "
            "the current-state summary body was not rewritten."
        )
    else:
        reason = "The reviewed current-state summary was synced after the append."
    return _post_append_sync_user_summary(
        category="no_write_needed",
        conclusion="Recording closeout complete.",
        reason=reason,
        next_step="Stop; no further RecallLoom write is needed.",
    )


def _post_append_sync_noop_summary(*, reason_code: str) -> dict:
    reason = "The rolling summary is already current enough for this closeout."
    if reason_code == "summary_not_stale":
        reason = "There is no stale post-append summary state to reconcile."
    elif reason_code == "summary_revision_not_stale":
        reason = "The summary revision is not behind the workspace revision."
    return _post_append_sync_user_summary(
        category="no_write_needed",
        conclusion="Recording closeout already complete.",
        reason=reason,
        next_step="Stop; no RecallLoom write is needed.",
    )


def _post_append_sync_review_summary(*, reason_code: str) -> dict:
    reason_by_code = {
        "stale_cause_not_append_only": "The stale state includes changes beyond one append.",
        "stale_not_single_append_delta": "The workspace changed by more than the single authorized append.",
        "daily_log_cursor_mismatch": "The daily-log cursor does not match the latest log.",
        "missing_latest_daily_log": "The latest daily log could not be identified.",
        "missing_latest_daily_log_entry": "The latest daily-log entry could not be identified.",
        "invalid_daily_log_cursor": "The daily-log cursor is not safe enough for automatic closeout.",
        "continuity_not_seeded": "Continuity state is not seeded enough for automatic closeout.",
    }
    return _post_append_sync_user_summary(
        category="review_required",
        conclusion="Review required before closeout.",
        reason=reason_by_code.get(
            reason_code,
            "The post-append closeout contract is not safe enough for automatic sync.",
        ),
        next_step="Rerun preflight and review the post-append closeout state before writing.",
    )


def _print_post_append_sync_summary(summary: dict) -> None:
    print("RecallLoom post-recording closeout")
    print(f"{summary['category']}: {summary['conclusion']}")
    if summary.get("reason"):
        print(f"Reason: {summary['reason']}")
    print(f"Next step: {summary['next_step']}")


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

    if input_mode == "reuse-current-summary":
        input_ref = "reissue_bound_semantic_unchanged_assertion"
        input_format = "markdown"
        input_contract = "metadata_only_refresh_assertion"
        input_args = [
            "--reuse-current-summary",
            "--semantic-unchanged-assertion-json",
            "same_bound_assertion_json",
        ]
    else:
        input_ref = "resubmit_same_stdin_payload"
        input_format = "json"
        input_contract = "json-stdin"
        input_args = ["--stdin", "--input-format", "json"]
    if args.max_input_bytes is not None:
        input_args.extend(["--max-input-bytes", str(args.max_input_bytes)])
    if getattr(args, "confirm_review_imported_baseline", False):
        input_args.append("--confirm-review-imported-baseline")
    return {
        "command": f"recallloom.py {POST_APPEND_SYNC_COMMAND}",
        "project_ref": "same_project",
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": input_mode,
        "input_ref": input_ref,
        "input_format": input_format,
        "input_contract": input_contract,
        "argv_template": [
            "recallloom.py",
            POST_APPEND_SYNC_COMMAND,
            "same_project",
            *input_args,
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
    user_summary = _post_append_sync_review_summary(reason_code=reason_code)
    return {
        "ok": False,
        "schema_version": "1.1",
        "blocked": True,
        "blocked_reason": "post_append_summary_sync_not_allowed",
        "recoverability": "retryable",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "command": POST_APPEND_SYNC_COMMAND,
        "side_effect": "none",
        "write_effect": "none",
        "contract_type": (
            contract.get("contract_type")
            if isinstance(contract, dict) and isinstance(contract.get("contract_type"), str)
            else POST_APPEND_SYNC_CONTRACT_TYPE
        ),
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": input_mode,
        "input_format": _post_append_sync_input_format(args),
        "reason_code": reason_code,
        "error": message,
        "user_visible_category": user_summary["category"],
        "user_summary": user_summary,
        "user_message": "Post-append summary sync is not allowed for the current sidecar state.",
        "operator_note": (
            "Rerun preflight and only retry this command when the post_append_summary_sync "
            "contract is allowed for a single append delta."
        ),
        "next_actions": ["rerun_preflight", "review_post_append_summary_sync_contract"],
        "single_next_command": "recallloom.py status <project-path> --json",
        "safe_to_retry": False,
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


def _post_append_sync_noop_payload(
    args: argparse.Namespace,
    *,
    input_mode: str,
    reason_code: str,
    contract: dict | None,
    support: dict,
) -> dict:
    append_cursor = contract.get("append_cursor") if isinstance(contract, dict) else None
    ordinary_write_gate = contract.get("ordinary_write_gate") if isinstance(contract, dict) else None
    user_summary = _post_append_sync_noop_summary(reason_code=reason_code)
    return {
        "ok": True,
        "schema_version": "1.1",
        "command": POST_APPEND_SYNC_COMMAND,
        "contract_type": (
            contract.get("contract_type")
            if isinstance(contract, dict) and isinstance(contract.get("contract_type"), str)
            else POST_APPEND_SYNC_CONTRACT_TYPE
        ),
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": input_mode,
        "input_format": _post_append_sync_input_format(args),
        "side_effect": "none",
        "write_effect": "none",
        "reason_code": reason_code,
        "user_visible_category": user_summary["category"],
        "user_summary": user_summary,
        "next_actions": ["stop_no_write_needed"],
        "single_next_command": None,
        "safe_to_retry": True,
        "append_cursor": append_cursor if isinstance(append_cursor, dict) else None,
        "ordinary_write_gate_preserved": (
            contract.get("ordinary_write_gate_preserved")
            if isinstance(contract, dict)
            else None
        ),
        "ordinary_write_gate": ordinary_write_gate if isinstance(ordinary_write_gate, dict) else None,
        "package_support": public_package_support_payload(support),
    }


def _post_append_sync_confirmation_payload(
    args: argparse.Namespace,
    *,
    input_mode: str,
    preflight_payload: dict,
) -> dict:
    retry_payload = _post_append_sync_retry_payload(args, input_mode=input_mode)
    argv_template = retry_payload.get("argv_template")
    if isinstance(argv_template, list) and "--confirm-review-imported-baseline" not in argv_template:
        insert_at = len(argv_template) - 1 if argv_template and argv_template[-1] == "--json" else len(argv_template)
        argv_template.insert(insert_at, "--confirm-review-imported-baseline")
    retry_payload["requires_repair_command_first"] = False
    confirmation_material = review_imported_baseline_confirmation_material(
        command=POST_APPEND_SYNC_COMMAND,
        operation="post_append_summary_sync",
        target_layer_or_surface="metadata",
        expected_side_effect="metadata_only_sync",
        files_or_keys_summary="rolling_summary metadata and latest daily_log cursor",
        approval_scope=(
            "Approve only metadata-only post-append current-state sync for the already "
            "authorized append; no semantic current-state rewrite is included."
        ),
        debug_reference=f"{POST_APPEND_SYNC_COMMAND}:metadata_sync:review_imported_baseline",
    )
    return cli_failure_payload(
        "review_imported_baseline_confirmation_required",
        error=(
            "Preflight returned an ask gate for review_imported_baseline. "
            "Review the post_append_summary_sync contract and rerun with "
            "--confirm-review-imported-baseline to confirm this current-state sync."
        ),
        details={
            **_preflight_gate_details(preflight_payload),
            "reason_code": "review_imported_baseline_confirmation_required",
            "command": POST_APPEND_SYNC_COMMAND,
            "side_effect": "none",
            "required_flag": "--confirm-review-imported-baseline",
        },
        extra={"retry_payload": retry_payload, "confirmation_material": confirmation_material},
    )


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
    reason_code = "source_file_not_supported" if source_file_present else "stdin_required"
    return cli_failure_payload(
        "invalid_prepared_input",
        error=message,
        details={
            "command": POST_APPEND_SYNC_COMMAND,
            "operation": "post_append_summary_sync",
            "reason_code": reason_code,
            "side_effect": "none",
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
    if getattr(args, "reuse_current_summary", False):
        return "reuse-current-summary"
    return "json-stdin"


def _post_append_sync_input_format(args: argparse.Namespace) -> str:
    if getattr(args, "reuse_current_summary", False):
        return "markdown"
    return "json"


def _sha256_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _post_append_sync_assertion_fields(
    parser,
    args: argparse.Namespace,
    *,
    contract: dict,
    preflight_payload: dict,
    support: dict,
) -> dict:
    try:
        assertion = json.loads(args.semantic_unchanged_assertion_json)
    except json.JSONDecodeError as exc:
        message = f"--semantic-unchanged-assertion-json must be valid JSON: {exc.msg}."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("invalid_prepared_input", error=message),
            support=support,
        )
    if not isinstance(assertion, dict):
        message = "--semantic-unchanged-assertion-json must be a JSON object."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("invalid_prepared_input", error=message),
            support=support,
        )
    metadata_only_refresh = contract.get("metadata_only_refresh")
    if not isinstance(metadata_only_refresh, dict):
        message = "Preflight did not return metadata_only_refresh binding details."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=_post_append_sync_failure_payload(
                args,
                input_mode="reuse-current-summary",
                message=message,
                reason_code="missing_metadata_only_refresh_contract",
                contract=contract,
            ),
            support=support,
        )
    seed = metadata_only_refresh.get("assertion_binding_seed")
    seed_digest = metadata_only_refresh.get("assertion_binding_seed_digest")
    body_digest_before = contract.get("managed_body_digest_before")
    if not isinstance(seed, dict) or not isinstance(seed_digest, str) or not isinstance(body_digest_before, str):
        message = "Preflight metadata-only refresh binding details are incomplete."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=_post_append_sync_failure_payload(
                args,
                input_mode="reuse-current-summary",
                message=message,
                reason_code="metadata_only_refresh_binding_incomplete",
                contract=contract,
            ),
            support=support,
        )

    def fail(message: str, reason_code: str) -> None:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=_post_append_sync_failure_payload(
                args,
                input_mode="reuse-current-summary",
                message=message,
                reason_code=reason_code,
                contract=contract,
            ),
            support=support,
        )

    source_kind = assertion.get("assertion_source_kind")
    assertion_source_id = assertion.get("assertion_source_id")
    input_digest = assertion.get("input_digest")
    if assertion.get("semantic_unchanged") is not True:
        fail(
            "metadata-only refresh requires semantic_unchanged=true in the bound assertion.",
            "semantic_unchanged_assertion_missing",
        )
    if source_kind not in {"record_plan_output_id", "explicit_operator_confirmation"}:
        fail(
            "metadata-only refresh assertion_source_kind is not allowed.",
            "assertion_source_kind_invalid",
        )
    if "source_id" in assertion:
        fail(
            "metadata-only refresh assertions must use assertion_source_id, not source_id.",
            "assertion_source_id_legacy_field",
        )
    if not isinstance(assertion_source_id, str) or not assertion_source_id:
        fail(
            "metadata-only refresh assertion_source_id is required.",
            "assertion_source_id_missing",
        )
    if (
        source_kind == "record_plan_output_id"
        and _RECORD_PLAN_OUTPUT_ID_RE.fullmatch(assertion_source_id) is None
    ):
        fail(
            "metadata-only refresh record_plan_output_id assertion_source_id is malformed.",
            "assertion_source_id_invalid",
        )
    if (
        source_kind == "explicit_operator_confirmation"
        and _EXPLICIT_OPERATOR_CONFIRMATION_ID_RE.fullmatch(assertion_source_id) is None
    ):
        fail(
            "metadata-only refresh explicit_operator_confirmation assertion_source_id is malformed.",
            "assertion_source_id_invalid",
        )
    if not isinstance(input_digest, str) or _SHA256_REFERENCE_RE.fullmatch(input_digest) is None:
        fail(
            "metadata-only refresh assertion input_digest is required.",
            "assertion_input_digest_missing",
        )
    if source_kind == "record_plan_output_id":
        raw_record_plan_output = assertion.get("record_plan_output")
        if not isinstance(raw_record_plan_output, dict):
            fail(
                "metadata-only refresh record_plan_output_id assertions must include the full record_plan_output.",
                "record_plan_output_missing",
            )
        try:
            record_plan_output = validate_record_plan_output(raw_record_plan_output)
        except RecordContractError as exc:
            fail(
                f"metadata-only refresh record_plan_output is invalid: {exc}",
                "record_plan_output_invalid",
            )
        if record_plan_output["record_plan_output_id"] != assertion_source_id:
            fail(
                "metadata-only refresh assertion_source_id does not match record_plan_output.record_plan_output_id.",
                "record_plan_output_id_mismatch",
            )
        if record_plan_output["input_digest"] != input_digest:
            fail(
                "metadata-only refresh assertion input_digest does not match record_plan_output.input_digest.",
                "record_plan_input_digest_mismatch",
            )
        if record_plan_output.get("record_class") != "metadata_refresh_only":
            fail(
                "metadata-only refresh assertion source must be a metadata_refresh_only record plan.",
                "record_plan_class_not_metadata_refresh",
            )
        current_safe_command = record_plan_output.get("current_safe_command")
        if not (
            isinstance(current_safe_command, str)
            and POST_APPEND_SYNC_COMMAND in current_safe_command
            and "--reuse-current-summary" in current_safe_command
        ):
            fail(
                "metadata-only refresh assertion source must point to the metadata refresh fast lane.",
                "record_plan_command_not_metadata_refresh",
            )
        required_gates = record_plan_output.get("required_gates")
        if not isinstance(required_gates, list) or not {"semantic_unchanged", "receipt_finalization"}.issubset(set(required_gates)):
            fail(
                "metadata-only refresh assertion source is missing required semantic or receipt gates.",
                "record_plan_metadata_gates_missing",
            )
        expected_binding = record_plan_output.get("expected_revision_binding")
        if not isinstance(expected_binding, dict) or expected_binding.get("preflight_digest") != digest_payload(preflight_payload):
            fail(
                "metadata-only refresh record_plan_output is not bound to the current preflight payload.",
                "record_plan_preflight_digest_mismatch",
            )
    if assertion.get("assertion_binding_seed") != seed:
        message = "metadata-only refresh assertion binding seed does not match preflight."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=_post_append_sync_failure_payload(
                args,
                input_mode="reuse-current-summary",
                message=message,
                reason_code="assertion_binding_seed_mismatch",
                contract=contract,
            ),
            support=support,
        )
    if assertion.get("assertion_binding_seed_digest") != seed_digest:
        message = "metadata-only refresh assertion binding seed digest does not match preflight."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=_post_append_sync_failure_payload(
                args,
                input_mode="reuse-current-summary",
                message=message,
                reason_code="assertion_binding_seed_digest_mismatch",
                contract=contract,
            ),
            support=support,
        )
    fields = {
        "assertion_source_kind": source_kind,
        "assertion_source_id": assertion_source_id,
        "assertion_payload_digest": _sha256_json(assertion),
        "managed_body_digest_before": body_digest_before,
        "assertion_binding_seed_digest": seed_digest,
        "input_digest": input_digest,
    }
    return fields


def _validate_post_append_sync_args(parser, args: argparse.Namespace, *, support: dict) -> None:
    source_file_present = args.source_file is not None
    stdin_present = bool(args.stdin)
    reuse_current_summary = bool(getattr(args, "reuse_current_summary", False))
    if reuse_current_summary:
        if source_file_present or stdin_present:
            message = (
                "sync-current-state-after-append --reuse-current-summary must not also "
                "use --source-file or --stdin."
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
        if not isinstance(args.semantic_unchanged_assertion_json, str):
            message = (
                "sync-current-state-after-append --reuse-current-summary requires "
                "--semantic-unchanged-assertion-json."
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
        return

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
                    "command": POST_APPEND_SYNC_COMMAND,
                    "operation": "post_append_summary_sync",
                    "reason_code": "unsafe_explicit_writer_id",
                    "side_effect": "none",
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


def _current_rolling_summary_text_for_reuse(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
) -> str:
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
        message = "Post-append summary sync target is not attached to RecallLoom."
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("no_project_root", error=message),
            support=support,
        )
    summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
    try:
        return read_text(summary_path)
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Could not read current rolling_summary for metadata-only refresh: {exc}"
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "damaged_sidecar",
                error=message,
                details={
                    "command": POST_APPEND_SYNC_COMMAND,
                    "operation": "post_append_summary_sync",
                    "input_mode": "reuse-current-summary",
                    "side_effect": "none",
                },
            ),
            support=support,
        )
    raise AssertionError("unreachable")


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
    allowed_gate_levels = {"read_current_state", "write_current_state_after_preflight"}
    if ordinary_write_gate.get("allowed_operation_level") not in allowed_gate_levels:
        return "ordinary_write_gate_level_mismatch"
    # The ordinary write route is still preserved by summary_stale=true;
    # post-append sync is the narrower single-append reconciliation path.
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


def _post_append_sync_commit_args(
    parser,
    args: argparse.Namespace,
    *,
    contract: dict,
    preflight_payload: dict,
    support: dict,
    input_format: str = "json",
    extra_binding_fields: dict | None = None,
) -> list[str]:
    helper_args = [
        args.target,
        "--file-key",
        "rolling_summary",
        "--expected-file-revision",
        str(contract["expected_file_revision"]),
        "--expected-workspace-revision",
        str(contract["expected_workspace_revision"]),
        "--input-format",
        input_format,
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
    helper_args.extend(
        [
            "--preflight-binding-json",
            _issue_preflight_binding_json(
                parser,
                args,
                preflight_payload=preflight_payload,
                binding_json=_preflight_write_binding_json(
                    file_key="rolling_summary",
                    write_type="current-state",
                    operation_class="post_append_summary_sync",
                    expected_file_revision=contract["expected_file_revision"],
                    expected_workspace_revision=contract["expected_workspace_revision"],
                    preflight_payload=preflight_payload,
                    contract_type=POST_APPEND_SYNC_CONTRACT_TYPE,
                    confirm_review_imported_baseline=args.confirm_review_imported_baseline,
                    write_readiness_label=_ready_after_preflight_label_for_provenance(
                        preflight_payload.get("provenance_state")
                    ),
                    extra_binding_fields=extra_binding_fields,
                ),
                support=support,
            ),
        ]
    )
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
    if _strict_preflight_blocked(preflight_payload):
        _exit_strict_preflight_failure(
            parser,
            args,
            preflight_payload=preflight_payload,
            support=support,
            command=POST_APPEND_SYNC_COMMAND,
            operation="post_append_summary_sync",
            file_key="rolling_summary",
            extra_details={"input_mode": input_mode},
        )
    contract = _post_append_sync_contract_from_preflight(preflight_payload)
    reason_code = _post_append_sync_contract_reason(contract)
    if reason_code is not None:
        if reason_code in POST_APPEND_SYNC_NOOP_REASONS:
            payload = _post_append_sync_noop_payload(
                args,
                input_mode=input_mode,
                reason_code=reason_code,
                contract=contract,
                support=support,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_post_append_sync_summary(payload["user_summary"])
            return
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
    provenance_guard = contract.get("provenance_guard")
    input_format = _post_append_sync_input_format(args)
    stdin_text = None
    extra_binding_fields = None
    if getattr(args, "reuse_current_summary", False):
        extra_binding_fields = _post_append_sync_assertion_fields(
            parser,
            args,
            contract=contract,
            preflight_payload=preflight_payload,
            support=support,
        )
        stdin_text = _current_rolling_summary_text_for_reuse(parser, args, support=support)
    review_imported_baseline_sync_confirmation_required = (
        preflight_payload.get("provenance_state") == "review_imported_baseline"
        or (
            isinstance(provenance_guard, dict)
            and provenance_guard.get("review_imported_baseline_plus_append_delta") is True
        )
    )
    if (
        review_imported_baseline_sync_confirmation_required
        and not args.confirm_review_imported_baseline
    ):
        message = (
            "Preflight returned an ask gate for review_imported_baseline. "
            "Review the post_append_summary_sync contract and rerun with "
            "--confirm-review-imported-baseline to confirm this current-state sync."
        )
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=message,
            payload=_post_append_sync_confirmation_payload(
                args,
                input_mode=input_mode,
                preflight_payload=preflight_payload,
            ),
            support=support,
        )
    payload = _run_helper_json(
        parser,
        helper_name="commit_context_file.py",
        helper_args=_post_append_sync_commit_args(
            parser,
            args,
            contract=contract,
            preflight_payload=preflight_payload,
            support=support,
            input_format=input_format,
            extra_binding_fields=extra_binding_fields,
        ),
        json_mode_on_failure=args.json,
        support=support,
        package_support_on_failure=True,
        stdin_text=stdin_text,
    )
    result = {
        **payload,
        "schema_version": "1.1",
        "command": POST_APPEND_SYNC_COMMAND,
        "contract_type": POST_APPEND_SYNC_CONTRACT_TYPE,
        "write_type": "current-state",
        "file_key": "rolling_summary",
        "input_mode": payload.get("input_mode", input_mode),
        "input_format": input_format,
        "expected_file_revision": contract["expected_file_revision"],
        "expected_workspace_revision": contract["expected_workspace_revision"],
        "append_cursor": contract.get("append_cursor"),
        "provenance_guard": contract.get("provenance_guard"),
        "ordinary_write_gate_preserved": contract.get("ordinary_write_gate_preserved"),
        "ordinary_write_gate": contract.get("ordinary_write_gate"),
        "package_support": public_package_support_payload(support),
    }
    user_summary = _post_append_sync_success_summary(input_mode=input_mode)
    result["user_visible_category"] = user_summary["category"]
    result["user_summary"] = user_summary
    result["next_actions"] = ["stop_no_further_recallloom_write_needed"]
    result["single_next_command"] = None
    if wrapper_metadata is not None:
        result["wrapper_metadata"] = wrapper_metadata
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_post_append_sync_summary(user_summary)


def _repair_daily_log_cursor_args(args: argparse.Namespace) -> list[str]:
    helper_args = [args.target]
    if args.apply:
        helper_args.append("--apply")
    if args.yes:
        helper_args.append("--yes")
    if args.expected_workspace_revision is not None:
        helper_args.extend(
            ["--expected-workspace-revision", str(args.expected_workspace_revision)]
        )
    if getattr(args, "preview_digest", None) is not None:
        helper_args.extend(["--preview-digest", args.preview_digest])
    return helper_args


def _handle_repair_daily_log_cursor(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
) -> None:
    helper_args = _repair_daily_log_cursor_args(args)
    if args.json:
        payload = _run_helper_json(
            parser,
            helper_name="repair_daily_log_cursor.py",
            helper_args=helper_args,
            json_mode_on_failure=True,
            support=support,
            package_support_on_failure=True,
        )
        payload["schema_version"] = payload.get("schema_version", "1.1")
        payload["command"] = "repair-daily-log-cursor"
        payload["package_support"] = public_package_support_payload(support)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    _run_helper_passthrough(
        helper_name="repair_daily_log_cursor.py",
        helper_args=helper_args,
    )


def _exit_repair_argument_error(
    parser,
    args: argparse.Namespace,
    *,
    message: str,
    reason_code: str,
) -> None:
    exit_with_cli_error(
        parser,
        json_mode=args.json,
        exit_code=2,
        message=message,
        payload=cli_failure_payload(
            "invalid_prepared_input",
            error=message,
            details={
                "side_effect": "none",
                "command": "repair-daily-log-cursor",
                "operation": "repair_daily_log_cursor",
                "reason_code": reason_code,
            },
        ),
    )


def _validate_repair_dispatcher_args(
    parser,
    args: argparse.Namespace,
) -> None:
    if args.command != "repair-daily-log-cursor":
        return
    if args.yes and not args.apply:
        _exit_repair_argument_error(
            parser,
            args,
            message="--yes is only valid with --apply.",
            reason_code="yes_without_apply",
        )
    if args.apply and not args.yes:
        _exit_repair_argument_error(
            parser,
            args,
            message="--apply requires --yes before repair can write state.json.",
            reason_code="apply_requires_yes",
        )
    if args.apply and args.expected_workspace_revision is None and args.preview_digest is None:
        _exit_repair_argument_error(
            parser,
            args,
            message=(
                "--apply requires a fresh repair preview binding: provide "
                "--expected-workspace-revision or --preview-digest."
            ),
            reason_code="repair_apply_requires_preview_binding",
        )


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


def _dispatcher_action_level(args: argparse.Namespace) -> str:
    return action_level_for_dispatcher(
        args.command,
        apply=bool(getattr(args, "apply", False)),
    )


def _handle_quick_summary(parser, args: argparse.Namespace, *, support: dict) -> None:
    try:
        start_path = normalize_start_path(args.target)
    except StorageResolutionError as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="not_project_root",
                extra=strict_sidecar_no_write_failure_extra(continuity_confidence="broken"),
            ),
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
                extra=strict_sidecar_no_write_failure_extra(continuity_confidence="broken"),
            ),
            support=support,
        )

    if workspace is None:
        payload = build_no_project_payload(start_path)
        payload.update(
            strict_sidecar_no_write_failure_extra(
                continuity_confidence="none",
                include_recovery_actions=False,
            )
        )
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
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=message,
                        extra=_strict_gate_read_failure_extra(workspace),
                    ),
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
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=message,
                        extra=_strict_gate_read_failure_extra(workspace),
                    ),
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
                    extra=_strict_gate_read_failure_extra(workspace),
                ),
                support=support,
            )
        try:
            payload = build_quick_summary_payload(
                project_root=workspace.project_root,
                storage_root=workspace.storage_root,
                summary_path=summary_path,
                summary_text=summary_text,
                summary_revision=summary_state.revision,
                summary_base_workspace_revision=summary_state.base_workspace_revision,
                state=state,
            )
            strict_gate = strict_sidecar_integrity_gate(
                project_root=workspace.project_root,
                storage_root=workspace.storage_root,
            )
            payload["strict_sidecar_integrity_gate"] = (
                strict_sidecar_integrity_gate_public_summary(strict_gate)
            )
            if strict_gate.get("allowed_for_mutation") is not True:
                payload["next_actions"] = _strict_gate_read_actions(
                    list(payload.get("next_actions", [])),
                    strict_gate_allows_write=False,
                )
        except DailyLogCursorError as exc:
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=str(exc),
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=str(exc),
                    details={
                        **exc.details,
                        "project_root": str(workspace.project_root),
                    },
                    extra=_strict_gate_read_failure_extra(workspace),
                ),
                support=support,
            )
        if startup_residue_report is not None:
            payload["startup_residue_report"] = startup_residue_report

    if args.json:
        user_summary = read_surface_user_summary(
            surface="Quick summary",
            summary_stale=bool(payload.get("freshness", {}).get("summary_stale")),
            strict_sidecar_integrity_gate=payload.get("strict_sidecar_integrity_gate"),
            continuity_state=payload.get("continuity_state"),
            no_project=payload.get("continuity_state") == "no_project",
        )
        payload["user_visible_category"] = user_summary["category"]
        payload["user_summary"] = user_summary
        payload["package_support"] = public_package_support_payload(support)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    user_summary = read_surface_user_summary(
        surface="Quick summary",
        summary_stale=bool(payload.get("freshness", {}).get("summary_stale")),
        strict_sidecar_integrity_gate=payload.get("strict_sidecar_integrity_gate"),
        continuity_state=payload.get("continuity_state"),
        no_project=payload.get("continuity_state") == "no_project",
    )
    payload["user_visible_category"] = user_summary["category"]
    payload["user_summary"] = user_summary
    print_user_summary("RecallLoom quick summary", user_summary)
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


def _record_invalid_input(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
    message: str,
    reason_code: str,
) -> None:
    suggest_mode = bool(getattr(args, "suggest", False)) and not bool(
        getattr(args, "plan", False)
    )
    if suggest_mode:
        user_summary = {
            "category": "blocked",
            "conclusion": "Recording suggestion input is invalid.",
            "reason": "The suggestion request must be corrected before RecallLoom can classify it.",
            "next_step": "Revise the record --suggest input and rerun the suggestion.",
        }
        payload = {
            "ok": False,
            "schema_version": "1.1",
            "command": "record",
            "mode": "suggest",
            "blocked": True,
            "blocked_reason": "invalid_record_suggestion_input",
            "reason_code": reason_code,
            "recoverability": "user_input_required",
            "surface_level": "user_safe",
            "trust_effect": "none",
            "suggestion_status": "blocked",
            "should_prompt": False,
            "side_effect": "none",
            "error": message,
            "user_visible_category": user_summary["category"],
            "user_summary": user_summary,
            "user_message": "The record suggestion input is not valid.",
            "operator_note": "Fix the record --suggest input shape, then rerun record --suggest.",
            "next_actions": [
                "revise_record_suggestion_input",
                "rerun_record_suggestion",
            ],
            "current_safe_command": None,
            "single_next_command": None,
            "safe_to_retry": True,
        }
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=payload,
            support=support,
        )
    payload = {
        "ok": False,
        "schema_version": "1.1",
        "command": "record",
        "plan_mode": "plan-only",
        "blocked": True,
        "blocked_reason": "invalid_record_plan_input",
        "reason_code": reason_code,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "workflow_status": "blocked_fixable",
        "record_class": "ambiguous_needs_user",
        "side_effect": "none",
        "error": message,
        "user_message": "The record plan input is not valid.",
        "operator_note": "Fix the record --plan input shape, then rerun without using record --apply.",
        "next_actions": ["revise_record_plan_input", "rerun_record_plan"],
        "current_safe_command": None,
        "single_next_command": None,
        "safe_to_retry": True,
    }
    _exit_with_support(
        parser,
        json_mode=args.json,
        exit_code=2,
        message=message,
        payload=payload,
        support=support,
    )


def _record_json_object(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
    raw: str,
    field_name: str,
) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=f"{field_name} must be valid JSON: {exc.msg}.",
            reason_code=f"malformed_{field_name}",
        )
    if not isinstance(payload, dict):
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=f"{field_name} must be a JSON object.",
            reason_code=f"{field_name}_not_object",
        )
    return payload


def _record_payload_from_args(parser, args: argparse.Namespace, *, support: dict) -> dict:
    if args.payload_json is not None and args.stdin:
        record_mode = "--suggest" if getattr(args, "suggest", False) else "--plan"
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=f"record {record_mode} accepts either --payload-json or --stdin, not both.",
            reason_code="multiple_payload_sources",
        )
    if args.payload_json is not None:
        return _record_json_object(
            parser,
            args,
            support=support,
            raw=args.payload_json,
            field_name="payload_json",
        )
    if not args.stdin:
        return {}
    max_bytes = args.max_input_bytes
    try:
        raw_bytes = sys.stdin.buffer.read(max_bytes + 1 if max_bytes is not None else -1)
    except OSError as exc:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=f"Could not read record payload from stdin: {exc}",
            reason_code="stdin_read_failed",
        )
    if max_bytes is not None and len(raw_bytes) > max_bytes:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message="Prepared record payload exceeded --max-input-bytes.",
            reason_code="stdin_payload_too_large",
        )
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=f"Prepared record payload stdin must be UTF-8: {exc}.",
            reason_code="stdin_not_utf8",
        )
    return _record_json_object(
        parser,
        args,
        support=support,
        raw=raw,
        field_name="stdin_payload",
    )


def _semantic_unchanged_assertion(args: argparse.Namespace) -> bool | None:
    if args.semantic_unchanged:
        return True
    if args.semantic_changed:
        return False
    return None


def _record_input_contract(
    parser,
    args: argparse.Namespace,
    *,
    support: dict,
) -> tuple[dict, str | None]:
    try:
        record_class_hint = normalize_record_class_hint(args.record_class)
        layer_hint = normalize_layer_hint(args.layer_hint)
    except RecordContractError as exc:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=str(exc),
            reason_code="invalid_record_hint",
        )
    privacy_result = (
        _record_json_object(
            parser,
            args,
            support=support,
            raw=args.privacy_safety_json,
            field_name="privacy_safety_json",
        )
        if args.privacy_safety_json is not None
        else {"classification": "unknown"}
    )
    expected_revision_binding = (
        _record_json_object(
            parser,
            args,
            support=support,
            raw=args.expected_revision_json,
            field_name="expected_revision_json",
        )
        if args.expected_revision_json is not None
        else {}
    )
    input_contract = {
        "intent_text": args.intent_text or "",
        "prepared_record_payload": _record_payload_from_args(parser, args, support=support),
        "optional_layer_hint": layer_hint,
        "privacy_safety_result": privacy_result,
        "expected_revision_binding": expected_revision_binding,
        "semantic_unchanged_assertion": _semantic_unchanged_assertion(args),
    }
    return input_contract, record_class_hint


def _record_plan_strict_gate_blocks(payload: dict) -> bool:
    expected_revision_binding = payload.get("expected_revision_binding")
    if isinstance(expected_revision_binding, dict):
        strict_gate = expected_revision_binding.get("strict_sidecar_integrity_gate")
        if isinstance(strict_gate, dict) and strict_gate.get("allowed_for_mutation") is False:
            return True
    return payload.get("blocked_reason") == "strict_sidecar_integrity_failed"


def _record_plan_user_view(payload: dict) -> dict:
    workflow_status = payload.get("workflow_status")
    record_class = payload.get("record_class")
    blocked_reason = payload.get("blocked_reason")

    if (
        workflow_status == "blocked_unsafe"
        or blocked_reason == "package_support_blocked"
        or _record_plan_strict_gate_blocks(payload)
    ):
        reason = "The requested path is blocked by a safety gate."
        if record_class == "unsafe_blocked":
            reason = "The input includes unsafe or private-looking content and cannot be recorded as-is."
        elif blocked_reason == "strict_sidecar_integrity_failed":
            reason = "Managed sidecar integrity is not safe for mutation."
        elif blocked_reason == "package_support_blocked":
            reason = "Package support status blocks mutation."
        return {
            "category": "blocked",
            "conclusion": "Blocked.",
            "reason": reason,
            "next_step": "Stop and remove, redact, or repair the blocking cause through the supported path.",
        }

    if workflow_status == "blocked_fixable":
        reason = "Current state needs review before any write."
        if blocked_reason == "preflight_write_not_ready":
            reason = "Write readiness is not safe enough yet."
        return {
            "category": "review_required",
            "conclusion": "Review required before recording.",
            "reason": reason,
            "next_step": "Review current state or rerun preflight before writing.",
        }

    if workflow_status == "needs_user_confirmation":
        reason = "One target, scope, or material decision is needed before RecallLoom can write."
        next_step = "Ask one confirmation question before any write."
        if record_class == "ambiguous_needs_user":
            reason = "The recording intent is not specific enough to choose one safe target."
        elif record_class == "metadata_refresh_only":
            input_contract = payload.get("input_contract")
            semantic_assertion = (
                input_contract.get("semantic_unchanged_assertion")
                if isinstance(input_contract, dict)
                else None
            )
            if semantic_assertion is False:
                reason = "Current-state meaning changed, so metadata-only reconciliation is not safe."
                next_step = "Prepare and confirm a reviewed current-state update."
            else:
                reason = "Metadata-only reconciliation needs an explicit semantic-unchanged decision."
                next_step = "Confirm whether meaning stayed unchanged, then rerun the plan with that assertion."
        return {
            "category": "needs_confirmation",
            "conclusion": "Confirmation required.",
            "reason": reason,
            "next_step": next_step,
        }

    if workflow_status == "ready_to_run":
        ready_reasons = {
            "append_only": "A daily-log note is prepared; safety gates stay active.",
            "current_state_update": "A current-state update is prepared; safety gates stay active.",
            "metadata_refresh_only": "A metadata-only refresh is prepared; safety gates stay active.",
        }
        return {
            "category": "ready_to_record",
            "conclusion": "Ready to record.",
            "reason": ready_reasons.get(
                record_class,
                "The requested RecallLoom action is prepared; safety gates stay active.",
            ),
            "next_step": "Run the prepared record action.",
        }

    if workflow_status in {"no_write", "complete"}:
        no_write_reasons = {
            "duplicate_noop": "The same durable fact appears to be already covered.",
            "defer_no_write": "The request does not add a durable RecallLoom fact yet.",
            "no_write_success": "The request is complete without changing RecallLoom memory.",
        }
        return {
            "category": "no_write_needed",
            "conclusion": "No write needed.",
            "reason": no_write_reasons.get(
                record_class,
                "The request is complete without changing RecallLoom memory.",
            ),
            "next_step": "Stop; no RecallLoom write is needed.",
        }

    return {
        "category": "diagnostic_only",
        "conclusion": "Diagnostic result only.",
        "reason": "This result is not a write authorization.",
        "next_step": "Continue reading or run a diagnostic; do not mutate from this result alone.",
    }


def _print_record_plan_summary(payload: dict) -> None:
    user_view = _record_plan_user_view(payload)
    print("RecallLoom record plan")
    print(f"{user_view['category']}: {user_view['conclusion']}")
    if user_view.get("reason"):
        print(f"Reason: {user_view['reason']}")
    print(f"Next step: {user_view['next_step']}")
    confirmation_material = record_plan_confirmation_material(payload, user_view)
    if confirmation_material is not None:
        print_confirmation_material(confirmation_material)


def _print_record_suggestion(payload: dict) -> None:
    user_summary = payload.get("user_summary") if isinstance(payload, dict) else None
    if not isinstance(user_summary, dict):
        user_summary = {
            "category": payload.get("user_visible_category") or "diagnostic_only",
            "conclusion": "Recording suggestion result.",
            "reason": "This result is side-effect-free.",
            "next_step": "Continue or run record --plan only if a durable fact should be recorded.",
        }
    print("RecallLoom record suggestion")
    print(f"{user_summary['category']}: {user_summary['conclusion']}")
    if user_summary.get("reason"):
        print(f"Reason: {user_summary['reason']}")
    candidate_summary = payload.get("candidate_summary")
    if candidate_summary:
        print(f"Candidate summary: {candidate_summary}")
    suggested_path = payload.get("suggested_path")
    if suggested_path:
        print(f"Suggested path: {suggested_path}")
    print(f"Next step: {user_summary['next_step']}")


def _compact_record_plan_payload(payload: dict) -> dict:
    user_view = _record_plan_user_view(payload)
    current_safe_command = payload.get("current_safe_command")
    blocked_reason = payload.get("blocked_reason")
    if current_safe_command:
        action_type = "run_command"
        action_value = current_safe_command
    elif payload.get("user_decision_required"):
        action_type = "ask_user"
        action_value = blocked_reason or "user_decision_required"
    elif payload.get("workflow_status") in {"no_write", "complete"}:
        action_type = "no_write"
        action_value = payload.get("terminal_success_condition")
    else:
        action_type = "stop"
        action_value = blocked_reason or payload.get("workflow_status")

    expected_revision_binding = payload.get("expected_revision_binding")
    expected_revisions = (
        expected_revision_binding.get("expected_revisions")
        if isinstance(expected_revision_binding, dict)
        else None
    )
    input_contract = payload.get("input_contract")
    privacy_safety_result = (
        input_contract.get("privacy_safety_result")
        if isinstance(input_contract, dict)
        else None
    )
    privacy_safety_classification = (
        privacy_safety_result.get("classification")
        if isinstance(privacy_safety_result, dict)
        else None
    )
    ready_to_run_current_command = (
        action_type == "run_command"
        and payload.get("workflow_status") == "ready_to_run"
        and payload.get("side_effect") == "none"
    )
    compact_payload = {
        "schema_version": "1.1",
        "ok": payload.get("workflow_status") not in {"blocked_fixable", "blocked_unsafe"},
        "command": "record",
        "record_class": payload.get("record_class"),
        "workflow_status": payload.get("workflow_status"),
        "side_effect": payload.get("side_effect"),
        "ready_to_mutate": False,
        "ready_to_run_current_command": ready_to_run_current_command,
        "user_visible_category": user_view["category"],
        "topline": f"{user_view['category']}: {user_view['conclusion']}",
        "user_summary": user_view,
        "next_action": {
            "type": action_type,
            "value": action_value,
            "current_safe_command": current_safe_command,
            "blocked_reason": blocked_reason,
            "validation_hint": payload.get("validation_hint"),
        },
        "safety_receipt": {
            "record_plan_output_id": payload.get("record_plan_output_id"),
            "input_digest": payload.get("input_digest"),
            "plan_mode": payload.get("plan_mode"),
            "write_effect": payload.get("write_effect"),
            "planned_layers": payload.get("planned_layers"),
            "required_gates": payload.get("required_gates"),
            "expected_revisions": expected_revisions,
            "privacy_safety_classification": privacy_safety_classification,
            "write_readiness": (
                expected_revision_binding.get("write_readiness")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "allowed_operation_level": (
                expected_revision_binding.get("allowed_operation_level")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "sidecar_trust_state": (
                expected_revision_binding.get("sidecar_trust_state")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "provenance_state": (
                expected_revision_binding.get("provenance_state")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "provenance_note": (
                expected_revision_binding.get("provenance_note")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "freshness_risk_level": (
                expected_revision_binding.get("freshness_risk_level")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "freshness_risk_note": (
                expected_revision_binding.get("freshness_risk_note")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "continuity_drift_risk_level": (
                expected_revision_binding.get("continuity_drift_risk_level")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "continuity_drift_review_required": (
                expected_revision_binding.get("continuity_drift_review_required")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "summary_stale": (
                expected_revision_binding.get("summary_stale")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "workspace_newer_than_summary": (
                expected_revision_binding.get("workspace_newer_than_summary")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "preflight_contract_identity": (
                expected_revision_binding.get("preflight_contract_identity")
                if isinstance(expected_revision_binding, dict)
                else None
            ),
            "validation_hint": payload.get("validation_hint"),
        },
    }
    confirmation_material = record_plan_confirmation_material(payload, user_view)
    if confirmation_material is not None:
        compact_payload["confirmation_material"] = confirmation_material
    return compact_payload


def _handle_record_plan(parser, args: argparse.Namespace, *, support: dict) -> None:
    if args.plan and getattr(args, "suggest", False):
        _record_invalid_input(
            parser,
            args,
            support=support,
            message="Use either record --plan or record --suggest, not both.",
            reason_code="record_mode_conflict",
        )
    if getattr(args, "suggest", False):
        input_contract, _record_class_hint = _record_input_contract(parser, args, support=support)
        payload = build_recording_suggestion(input_contract)
        payload["package_support"] = public_package_support_payload(support)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_record_suggestion(payload)
        return
    if not args.plan:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=(
                "record currently supports --plan and --suggest. record --apply is not "
                "part of the current v0.4.6-line package."
            ),
            reason_code="record_plan_required",
        )
    input_contract, record_class_hint = _record_input_contract(parser, args, support=support)
    preflight_payload = _preflight_payload(parser, args, support=support)
    try:
        payload = plan_record(
            input_contract=input_contract,
            record_class_hint=record_class_hint,
            preflight_payload=preflight_payload,
        )
    except RecordContractError as exc:
        _record_invalid_input(
            parser,
            args,
            support=support,
            message=str(exc),
            reason_code="record_plan_contract_invalid",
        )
    if args.json:
        output_payload = _compact_record_plan_payload(payload) if args.compact else payload
        print(json.dumps(output_payload, ensure_ascii=False, indent=2))
    else:
        _print_record_plan_summary(payload)


def _project_relative_path(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix()


def _compact_guidance(
    text: str,
    *,
    project_root: Path | None = None,
    max_chars: int = 360,
) -> str | None:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        stripped = stripped.lstrip("#-* ").strip()
        if stripped:
            lines.append(redact_public_text(stripped, project_root=project_root) or "redacted")
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
    provenance_facts: dict | None = None,
) -> dict:
    if continuity_state == "no_project":
        return {
            "sidecar_trust_state": "unknown",
            "continuity_drift_risk_level": "none",
            "allowed_operation_level": "none",
            "read_confidence": "untrusted",
            "read_trust_note": "No RecallLoom sidecar was found.",
        }
    facts = provenance_facts or {}
    return evaluate_trust_state(
        continuity_confidence=continuity_confidence,
        continuity_state=continuity_state,
        summary_stale=summary_stale,
        workspace_newer_than_summary=workspace_newer_than_summary,
        conflict_state=None,
        legacy_sidecar=bool(facts.get("legacy_sidecar")),
        legacy_review_required=bool(facts.get("review_required")),
        review_imported_baseline=bool(facts.get("review_imported_baseline")),
        helper_evidenced=bool(facts.get("helper_evidenced")),
        inconsistent_evidence=bool(facts.get("inconsistent_evidence")),
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
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="not_project_root",
                extra=strict_sidecar_no_write_failure_extra(continuity_confidence="broken"),
            ),
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
                extra=strict_sidecar_no_write_failure_extra(continuity_confidence="broken"),
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
        expected_revisions = expected_revisions_payload(
            workspace_revision=None,
            rolling_summary_revision=None,
        )
        provenance = build_provenance_report(
            sidecar_trust_state=trust_state["sidecar_trust_state"],
            continuity_state=continuity_state,
            allowed_operation_level=trust_state["allowed_operation_level"],
            summary_stale=False,
            expected_revisions=expected_revisions,
            receipt_chain_verified=False,
        )
        payload = {
            "schema_version": "1.1",
            "ok": True,
            "command": "resume",
            "routing_target": "rl-resume",
            "resume_mode": mode,
            "fast_lane_contract": {
                "read_only": True,
                "attach_safe": True,
                "receipt_store_audit_performed": False,
                "receipt_chain_scan_performed": False,
                "daily_log_content_read": False,
                "context_brief_read": False,
                "update_protocol_read": False,
                "startup_scratch_scan_performed": False,
            },
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
                "provenance_state": provenance["state_label"],
                "write_readiness": provenance["write_readiness"],
                "allowed_operation_level": trust_state["allowed_operation_level"],
                "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
                "read_confidence": trust_state["read_confidence"],
                "read_trust_note": trust_state["read_trust_note"],
            },
            "continuity_confidence": "none",
            "continuity_state": continuity_state,
            "sidecar_trust_state": trust_state["sidecar_trust_state"],
            "provenance_state": provenance["state_label"],
            "provenance_contract": provenance["contract_identity"],
            "preflight_contract_identity": provenance_contract_identity(),
            "expected_revisions": expected_revisions,
            "write_readiness": provenance["write_readiness"],
            "allowed_operation_level": trust_state["allowed_operation_level"],
            "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
            **strict_sidecar_no_write_failure_extra(
                continuity_confidence="none",
                include_recovery_actions=False,
            ),
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
    startup_residue_report = None

    try:
        summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
        if not summary_path.is_file():
            message = f"Missing required file: {summary_path}"
            _exit_with_support(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=message,
                    extra=_strict_gate_read_failure_extra(workspace),
                ),
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
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=message,
                    extra=_strict_gate_read_failure_extra(workspace),
                ),
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
                extra=_strict_gate_read_failure_extra(workspace),
            ),
            support=support,
        )

    try:
        quick_payload = build_quick_summary_payload(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            summary_path=summary_path,
            summary_text=summary_text,
            summary_revision=summary_state.revision,
            summary_base_workspace_revision=summary_state.base_workspace_revision,
            state=state,
        )
    except DailyLogCursorError as exc:
        _exit_with_support(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=str(exc),
                details={
                    **exc.details,
                    "project_root": str(workspace.project_root),
                },
                extra=_strict_gate_read_failure_extra(workspace),
            ),
            support=support,
        )
    continuity_state = _resume_continuity_state(quick_payload)
    continuity_confidence = quick_payload["summary"]["confidence"]
    provenance_facts = provenance_facts_from_state(state, review_intent=False)
    trust_state = _resume_trust_state(
        continuity_confidence=continuity_confidence,
        continuity_state=continuity_state,
        summary_stale=quick_payload["freshness"]["summary_stale"],
        workspace_newer_than_summary=quick_payload["freshness"].get("workspace_newer_than_summary", False),
        provenance_facts=provenance_facts,
    )
    strict_gate = strict_sidecar_integrity_gate(
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )
    strict_gate_summary = strict_sidecar_integrity_gate_public_summary(strict_gate)
    strict_gate_receipts_verified = strict_gate_current_receipts_verified(strict_gate)
    effective_allowed_operation_level = _effective_allowed_operation_level_for_strict_gate(
        trust_state["allowed_operation_level"],
        strict_gate,
    )
    expected_revisions = expected_revisions_payload(
        workspace_revision=state["workspace_revision"],
        rolling_summary_revision=summary_state.revision,
    )
    write_expected_revisions = (
        expected_revisions if strict_gate.get("allowed_for_mutation") is True else None
    )
    provenance = build_provenance_report(
        sidecar_trust_state=trust_state["sidecar_trust_state"],
        continuity_state=continuity_state,
        allowed_operation_level=effective_allowed_operation_level,
        summary_stale=quick_payload["freshness"]["summary_stale"],
        expected_revisions=write_expected_revisions,
        receipt_chain_verified=strict_gate_receipts_verified,
        receipt_store_available=strict_gate_receipts_verified,
        legacy_sidecar=provenance_facts["legacy_sidecar"],
        review_required=provenance_facts["review_required"],
        review_imported_baseline=provenance_facts["review_imported_baseline"],
        helper_evidenced_baseline=provenance_facts["helper_evidenced"],
        unproven_sidecar_state=provenance_facts["unproven_sidecar_state"],
        metadata_status=provenance_facts["metadata_status"],
    )
    base_payload = {
        "schema_version": "1.1",
        "ok": True,
        "command": "resume",
        "routing_target": "rl-resume",
        "resume_mode": mode,
        "fast_lane_contract": {
            "read_only": True,
            "attach_safe": True,
            "receipt_store_audit_performed": False,
            "receipt_chain_scan_performed": False,
            "daily_log_content_read": False,
            "context_brief_read": mode == "full",
            "update_protocol_read": mode == "full",
            "startup_scratch_scan_performed": False,
        },
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
            "provenance_state": provenance["state_label"],
            "provenance_metadata_status": provenance["metadata_status"],
            "write_readiness": provenance["write_readiness"],
            "allowed_operation_level": effective_allowed_operation_level,
            "strict_sidecar_integrity_gate": strict_gate_summary,
            "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
            "read_confidence": trust_state["read_confidence"],
            "read_trust_note": trust_state["read_trust_note"],
        },
        "continuity_confidence": continuity_confidence,
        "continuity_state": continuity_state,
        "sidecar_trust_state": trust_state["sidecar_trust_state"],
        "provenance_state": provenance["state_label"],
        "provenance_metadata_status": provenance["metadata_status"],
        "provenance_contract": provenance["contract_identity"],
        "preflight_contract_identity": provenance_contract_identity(),
        "expected_revisions": write_expected_revisions,
        "write_readiness": provenance["write_readiness"],
        "allowed_operation_level": effective_allowed_operation_level,
        "strict_sidecar_integrity_gate": strict_gate_summary,
        "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
        "next_actions": _strict_gate_read_actions(
            _resume_next_actions(mode=mode, quick_actions=quick_payload["next_actions"]),
            strict_gate_allows_write=strict_gate.get("allowed_for_mutation") is True,
        ),
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
                extra=_strict_gate_read_failure_extra(workspace),
            ),
            support=support,
        )
    base_payload["fast_lane_contract"]["context_brief_read"] = context_brief_path.is_file()
    base_payload["fast_lane_contract"]["update_protocol_read"] = update_protocol_path.is_file()
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
        "guidance": _compact_guidance(
            context_brief_text,
            project_root=workspace.project_root,
        ),
    }
    base_payload["update_protocol_guidance"] = {
        "available": update_protocol_path.is_file(),
        "path": _project_relative_path(update_protocol_path, workspace.project_root),
        "guidance": _compact_guidance(
            update_protocol_text,
            project_root=workspace.project_root,
        ),
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
            "provenance_state": preflight_payload.get("provenance_state"),
            "preflight_contract_identity": preflight_payload.get("preflight_contract_identity"),
            "write_readiness": preflight_payload.get("write_readiness"),
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
        helper_args=_commit_context_file_args(
            parser,
            args,
            file_key=file_key,
            write_context=write_context,
            preflight_payload=preflight_payload,
            support=support,
        ),
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
    _validate_repair_dispatcher_args(parser, args)
    support = enforce_package_support_gate(
        parser,
        json_mode=getattr(args, "json", False),
        action_name=f"recallloom.py {args.command}",
        action_level=_dispatcher_action_level(args),
    )

    if args.command == "init":
        _handle_init(parser, args, support=support)
        return

    if args.command == "validate":
        _validate_provenance_scope_args(parser, args)
        helper_args = _validate_helper_args(args)
        if args.json:
            payload = _run_helper_json(
                parser,
                helper_name="validate_context.py",
                helper_args=helper_args,
                json_mode_on_failure=True,
                support=support,
            )
            public_payload = dict(payload)
            public_payload["package_support"] = public_package_support_payload(support)
            print(json.dumps(public_payload, ensure_ascii=False, indent=2))
        else:
            _run_helper_passthrough(helper_name="validate_context.py", helper_args=helper_args)
        return

    if args.command == "resume":
        mode = "full" if args.full else "fast"
        payload = _build_progressive_resume_payload(parser, args, mode=mode, support=support)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_progressive_resume_summary(payload)
        return

    if args.command == "status":
        helper_args = _status_like_helper_args(args)
        if args.json:
            payload = _run_helper_json(
                parser,
                helper_name="summarize_continuity_status.py",
                helper_args=helper_args,
                json_mode_on_failure=True,
                support=support,
            )
            payload["package_support"] = public_package_support_payload(support)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _run_helper_passthrough(
                helper_name="summarize_continuity_status.py", helper_args=helper_args
            )
        return

    if args.command == "quick-summary":
        _handle_quick_summary(parser, args, support=support)
        return

    if args.command == "record":
        _handle_record_plan(parser, args, support=support)
        return

    if args.command == "append":
        preflight_payload = _preflight_payload(parser, args, support=support)
        _enforce_append_preflight_gate(
            parser,
            args,
            preflight_payload=preflight_payload,
            support=support,
        )
        helper_args = _append_helper_args(
            parser,
            args,
            preflight_payload=preflight_payload,
            support=support,
        )
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

    if args.command == "repair-daily-log-cursor":
        _handle_repair_daily_log_cursor(parser, args, support=support)
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
