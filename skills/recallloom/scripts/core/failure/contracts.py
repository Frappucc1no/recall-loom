"""Stable failure contracts and reason registry for RecallLoom."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import sys

from core.output.privacy import private_json_paths_enabled, publicize_json_value, publicize_text_paths


ROLLING_SUMMARY_JSON_SECTION_KEYS = frozenset(
    (
        "current_state",
        "active_judgments",
        "risks_open_questions",
        "next_step",
        "recent_pivots",
    )
)

MANAGED_MARKDOWN_WRITE_ROUTES = {
    ("context_brief", "stable-context"): {
        "file_key": "context_brief",
        "write_type": "stable-context",
        "label": "context-brief",
        "source_placeholder": "<context-brief-source.md>",
    },
    ("rolling_summary", "current-state"): {
        "file_key": "rolling_summary",
        "write_type": "current-state",
        "label": "rolling-summary",
        "source_placeholder": "<rolling-summary-source.md>",
    },
    ("update_protocol", "protocol-rules"): {
        "file_key": "update_protocol",
        "write_type": "protocol-rules",
        "label": "update-protocol",
        "source_placeholder": "<update-protocol-source.md>",
    },
}

FAILURE_REASON_ALIASES = {
    "attached_text_safety_blocked": "attach_scan_blocked",
}


FAILURE_REASON_REGISTRY = {
    "python_runtime_unavailable": {
        "blocked": True,
        "recoverability": "retryable",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["find_compatible_python", "report_blocked_runtime"],
        "user_message": {
            "en": "RecallLoom cannot start yet because this environment does not provide Python 3.10 or newer.",
            "zh-CN": "当前环境还不能启动 RecallLoom，因为这里没有可用的 Python 3.10+ 运行时。",
        },
        "operator_note": {
            "en": "Find or point the host at a compatible Python 3.10+ interpreter before retrying.",
            "zh-CN": "请先找到或指定兼容的 Python 3.10+ 解释器，再重试。",
        },
    },
    "not_project_root": {
        "blocked": False,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["confirm_project_root", "retry_init"],
        "user_message": {
            "en": "This path does not look like the project root yet.",
            "zh-CN": "当前路径还不像真正的项目根目录。",
        },
        "operator_note": {
            "en": "Choose the real project root before retrying.",
            "zh-CN": "请先切到真实项目根目录，再重试。",
        },
    },
    "no_project_root": {
        "blocked": False,
        "recoverability": "not_initialized",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["rl-init", "choose_project_root"],
        "user_message": {
            "en": "This project has not been attached to RecallLoom yet.",
            "zh-CN": "当前项目还没有接入 RecallLoom。",
        },
        "operator_note": {
            "en": "Initialize RecallLoom at the correct project root before using status or bridge flows.",
            "zh-CN": "请先在正确的项目根目录初始化 RecallLoom，再使用 status 或 bridge 流程。",
        },
    },
    "damaged_sidecar": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "user_safe",
        "trust_effect": "damaged",
        "next_actions": ["repair_existing_sidecar", "rerun_validate_or_init"],
        "user_message": {
            "en": "The existing RecallLoom workspace is not trustworthy yet and needs repair before continuing.",
            "zh-CN": "当前已有的 RecallLoom 工作区还不可信，需要先修复后再继续。",
        },
        "operator_note": {
            "en": "Do not hand-build or patch managed files blindly; repair the damaged sidecar first.",
            "zh-CN": "不要手工拼接或盲改 managed 文件；请先修复 damaged sidecar。",
        },
    },
    "dual_sidecar_conflict": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "user_safe",
        "trust_effect": "conflicting",
        "next_actions": ["resolve_sidecar_conflict", "rerun_validate_or_init"],
        "user_message": {
            "en": "This project has conflicting RecallLoom sidecars, so RecallLoom should stop instead of guessing.",
            "zh-CN": "当前项目存在冲突的 RecallLoom sidecar，应该先停下而不是继续猜。",
        },
        "operator_note": {
            "en": "Resolve the hidden-vs-visible sidecar conflict before retrying.",
            "zh-CN": "请先处理隐藏 sidecar 与可见 sidecar 的冲突，再重试。",
        },
    },
    "attach_scan_blocked": {
        "blocked": True,
        "recoverability": "security_blocked",
        "surface_level": "user_safe",
        "trust_effect": "security_blocked",
        "next_actions": ["revise_bridge_text", "retry_bridge"],
        "user_message": {
            "en": "The current text did not pass the safety check.",
            "zh-CN": "当前文本没有通过安全检查。",
        },
        "operator_note": {
            "en": "Adjust the text without weakening the attached-text safety rules.",
            "zh-CN": "请调整文本，但不要削弱 attached-text safety 规则。",
        },
    },
    "invalid_date": {
        "blocked": False,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["correct_date_input", "retry_init"],
        "user_message": {
            "en": "The requested date is not a valid YYYY-MM-DD value.",
            "zh-CN": "当前给定的日期不是合法的 YYYY-MM-DD 值。",
        },
        "operator_note": {
            "en": "Fix the date value before retrying.",
            "zh-CN": "请先修正日期，再重试。",
        },
    },
    "invalid_tool_name": {
        "blocked": False,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["correct_tool_name", "retry_init"],
        "user_message": {
            "en": "The requested tool name is not valid for RecallLoom metadata.",
            "zh-CN": "当前给定的工具名不符合 RecallLoom 元数据约束。",
        },
        "operator_note": {
            "en": "Choose a valid tool name before retrying.",
            "zh-CN": "请先改成合法的工具名，再重试。",
        },
    },
    "invalid_storage_boundary": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "user_safe",
        "trust_effect": "conflicting",
        "next_actions": ["correct_storage_target", "retry_init"],
        "user_message": {
            "en": "The requested storage layout is not valid for this project path.",
            "zh-CN": "当前请求的存储布局与这个项目路径不兼容。",
        },
        "operator_note": {
            "en": "Choose a valid project root and storage layout before retrying.",
            "zh-CN": "请先确认合法的项目根目录与存储布局，再重试。",
        },
    },
    "reinit_create_daily_log_not_allowed": {
        "blocked": False,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["use_append_daily_log_entry", "retry_without_create_daily_log"],
        "user_message": {
            "en": "This project is already initialized. Create new milestone entries through the daily-log append helper instead.",
            "zh-CN": "当前项目已经初始化；如需记录新的日志条目，请改用 daily log append helper。",
        },
        "operator_note": {
            "en": "Do not use --create-daily-log during re-initialization of an existing workspace.",
            "zh-CN": "不要在已初始化工作区上继续使用 --create-daily-log。",
        },
    },
    "stale_write_context": {
        "blocked": True,
        "recoverability": "retryable",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["rerun_preflight", "reread_current_files"],
        "user_message": {
            "en": "The current write context is stale and needs to be refreshed before writing.",
            "zh-CN": "当前写入上下文已经过期，写入前需要先刷新。",
        },
        "operator_note": {
            "en": "Rerun preflight, reread current revisions, and retry with fresh expected revisions.",
            "zh-CN": "请重新执行 preflight，读取最新 revision 后再重试。",
        },
    },
    "write_lock_busy": {
        "blocked": True,
        "recoverability": "retryable",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["wait_for_active_writer", "retry_helper"],
        "user_message": {
            "en": "Another RecallLoom write appears to be in progress.",
            "zh-CN": "当前似乎已有另一个 RecallLoom 写入正在进行。",
        },
        "operator_note": {
            "en": "Wait for the active writer to finish, then retry. Only clear a stale lock after checking ownership and age.",
            "zh-CN": "请等待当前写入完成后再重试；只有在确认锁已过期且归属清楚后，才清理 stale lock。",
        },
    },
    "malformed_managed_file": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "operator",
        "trust_effect": "damaged",
        "next_actions": ["repair_managed_file", "rerun_validate_or_helper"],
        "user_message": {
            "en": "A managed RecallLoom file is malformed and must be repaired before continuing.",
            "zh-CN": "存在损坏的 RecallLoom managed 文件，需要先修复后再继续。",
        },
        "operator_note": {
            "en": "Repair the malformed managed file instead of bypassing marker or section checks.",
            "zh-CN": "请修复损坏的 managed 文件，不要绕过 marker 或 section 校验。",
        },
    },
    "derived_overlay_conflict": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "operator",
        "trust_effect": "conflicting",
        "next_actions": ["inspect_derived_overlay", "keep_rolling_summary_as_current_truth"],
        "user_message": {
            "en": "A derived overlay conflicts with rolling_summary current truth, so RecallLoom stopped instead of promoting derived data.",
            "zh-CN": "派生 overlay 与 rolling_summary 当前真相冲突，RecallLoom 已停止，未提升派生数据。",
        },
        "operator_note": {
            "en": "Treat rolling_summary.md as the current truth and repair or remove the optional derived overlay.",
            "zh-CN": "请把 rolling_summary.md 视为当前真相，并修复或移除可选派生 overlay。",
        },
    },
    "invalid_prepared_input": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["revise_prepared_input", "retry_helper"],
        "user_message": {
            "en": "The prepared input is not valid for this helper.",
            "zh-CN": "当前准备输入不符合这个 helper 的要求。",
        },
        "operator_note": {
            "en": "Fix the prepared source file or stdin content before retrying.",
            "zh-CN": "请先修正 source file 或 stdin 内容，再重试。",
        },
    },
    "privacy_security_failure": {
        "blocked": True,
        "recoverability": "security_blocked",
        "surface_level": "user_safe",
        "trust_effect": "security_blocked",
        "next_actions": ["revise_wrapper_metadata", "retry_helper"],
        "user_message": {
            "en": "The wrapper metadata did not pass the public-safety allowlist.",
            "zh-CN": "wrapper metadata 没有通过 public-safety allowlist。",
        },
        "operator_note": {
            "en": "Remove private identifiers, paths, tokens, fingerprints, and unsupported keys before retrying.",
            "zh-CN": "重试前请移除私有标识符、路径、token、fingerprint 和不支持的字段。",
        },
    },
    "startup_residue_detected": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "user_safe",
        "trust_effect": "review_required",
        "next_actions": ["inspect_helper_scratch_residue", "remove_confirmed_residue", "retry_helper"],
        "user_message": {
            "en": "RecallLoom found helper-owned startup scratch residue and stopped before making changes.",
            "zh-CN": "RecallLoom 发现 helper-owned 启动残留，已在作出改动前停止。",
        },
        "operator_note": {
            "en": "Inspect the public-safe residue report, remove only confirmed helper scratch residue, then retry.",
            "zh-CN": "请检查 public-safe 残留报告，只移除确认属于 helper scratch 的残留后再重试。",
        },
    },
    "historical_append_requires_confirmation": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "none",
        "next_actions": ["confirm_historical_append", "retry_with_allow_historical"],
        "user_message": {
            "en": "Appending to an older daily log requires explicit confirmation.",
            "zh-CN": "向较旧的 daily log 追加内容需要显式确认。",
        },
        "operator_note": {
            "en": "Use --allow-historical only when the historical append is intentional.",
            "zh-CN": "只有在确实要回填历史日志时，才使用 --allow-historical。",
        },
    },
    "project_time_policy_review_required": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "user_safe",
        "trust_effect": "review_required",
        "next_actions": ["review_update_protocol", "confirm_date_choice"],
        "user_message": {
            "en": "Project-local time policy requires a date review before continuing.",
            "zh-CN": "项目本地时间策略要求先复核日期，再继续。",
        },
        "operator_note": {
            "en": "Review update_protocol.md and confirm the intended date before writing.",
            "zh-CN": "请先检查 update_protocol.md，并确认目标日期后再写入。",
        },
    },
    "trust_review_required": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["review_current_continuity", "refresh_before_high_risk_actions"],
        "user_message": {
            "en": "RecallLoom needs a continuity review before a higher-risk action can continue.",
            "zh-CN": "在继续更高风险动作前，需要先复核当前 RecallLoom continuity。",
        },
        "operator_note": {
            "en": "Review the current continuity files and refresh state before proceeding.",
            "zh-CN": "请先复核当前 continuity 文件，并刷新状态后再继续。",
        },
    },
    "continuity_drift_review_required": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["review_current_workspace_state", "refresh_rolling_summary"],
        "user_message": {
            "en": "Current continuity may have drifted from the workspace and should be reviewed before higher-risk actions.",
            "zh-CN": "当前 continuity 可能已经和工作区现实脱节，继续高风险动作前应先复核。",
        },
        "operator_note": {
            "en": "Review current workspace reality and refresh the rolling summary before trusting it for writes.",
            "zh-CN": "请先复核当前工作区现实并刷新 rolling summary，再把它当作写入依据。",
        },
    },
    "storage_cleanup_incomplete": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "operator",
        "trust_effect": "none",
        "next_actions": ["remove_tombstone_storage", "verify_context_removal"],
        "user_message": {
            "en": "RecallLoom removal moved the storage root aside, but final cleanup is still incomplete.",
            "zh-CN": "RecallLoom 已把存储目录移走，但最后的清理还没有完成。",
        },
        "operator_note": {
            "en": "Delete the tombstone storage path and confirm that removal is complete before treating uninstall as finished.",
            "zh-CN": "请删除 tombstone 存储目录，并确认卸载已经真正完成后，再把这次移除视为结束。",
        },
    },
    "registry_contract_invalid": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "surface_level": "debug",
        "trust_effect": "damaged",
        "next_actions": ["repair_reason_registry", "rerun_bootstrap"],
        "user_message": {
            "en": "RecallLoom cannot continue because its failure-contract registry is invalid.",
            "zh-CN": "RecallLoom 当前无法继续，因为 failure-contract registry 已损坏。",
        },
        "operator_note": {
            "en": "Repair the failure-contract registry before retrying bootstrap or helper execution.",
            "zh-CN": "请先修复 failure-contract registry，再重新执行 bootstrap 或 helper。",
        },
    },
    "package_support_blocked": {
        "blocked": True,
        "recoverability": "upgrade_required",
        "surface_level": "user_safe",
        "trust_effect": "review_required",
        "next_actions": ["upgrade_recallloom_package", "rerun_support_check"],
        "user_message": {
            "en": "This RecallLoom package must be upgraded before this action can continue.",
            "zh-CN": "当前 RecallLoom 包需要先升级，才能继续执行这个动作。",
        },
        "operator_note": {
            "en": "Check the installed package path, native wrappers, and support advisory before retrying.",
            "zh-CN": "请先检查当前安装包路径、原生命令 wrapper 与 support advisory，再重试。",
        },
    },
}

FAILURE_PAYLOAD_SCHEMA_VERSION = "1.1"
_KNOWN_STORAGE_ROOT_NAMES = {".recallloom", "recallloom"}


def _localized_text(language: str, *, en: str, zh_cn: str) -> str:
    return zh_cn if language == "zh-CN" else en


def _normalize_script_name(script_name: str | None = None) -> str | None:
    candidate = script_name or (sys.argv[0] if sys.argv else "")
    if not candidate:
        return None
    name = Path(candidate).name.strip()
    return name or None


def _python_executable() -> str:
    candidate = sys.executable or "python3"
    if private_json_paths_enabled():
        if not os.path.isabs(candidate):
            candidate = shutil.which(candidate) or candidate
        return shlex.quote(candidate)
    public_candidate = Path(candidate).name.strip()
    return shlex.quote(public_candidate or "python3")


def _script_path(script_name: str | None) -> Path:
    normalized_name = _normalize_script_name(script_name) or "<recallloom-helper>.py"
    return Path(__file__).resolve().parents[2] / normalized_name


def _script_command(script_name: str | None, *args: str) -> str:
    normalized_name = _normalize_script_name(script_name) or "<recallloom-helper>.py"
    if normalized_name.endswith(".py"):
        script_ref = (
            shlex.quote(str(_script_path(script_name)))
            if private_json_paths_enabled()
            else shlex.quote(normalized_name)
        )
        base = f"{_python_executable()} {script_ref}"
    else:
        base = f"{_python_executable()} {shlex.quote(normalized_name)}"
    suffix = " ".join(part for part in args if part).strip()
    return base if not suffix else f"{base} {suffix}"


def _quote_or_placeholder(value: str | None, placeholder: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return placeholder
    return shlex.quote(value)


def _prepared_input_mode(details: dict | None) -> str | None:
    if not details:
        return None
    input_mode = details.get("input_mode")
    if isinstance(input_mode, str) and input_mode.strip():
        return input_mode
    return None


def _rolling_summary_section_from_field_path(field_path: object) -> str | None:
    if not isinstance(field_path, str) or not field_path.startswith("$."):
        return None
    section_key = field_path[2:].split(".", 1)[0].split("[", 1)[0]
    return section_key if section_key in ROLLING_SUMMARY_JSON_SECTION_KEYS else None


def _is_rolling_summary_json_reserved_marker(details: dict | None) -> bool:
    if not details or details.get("reason_code") != "reserved_marker_injection":
        return False
    input_mode = _prepared_input_mode(details)
    if not isinstance(input_mode, str) or not input_mode.startswith("json-"):
        return False
    section_key = details.get("section_key")
    return (
        section_key in ROLLING_SUMMARY_JSON_SECTION_KEYS
        or _rolling_summary_section_from_field_path(details.get("field_path")) is not None
    )


def _is_rolling_summary_json_builder(details: dict | None) -> bool:
    if not details:
        return False
    return (
        details.get("prepared_input_builder") == "rolling_summary_json"
        or _is_rolling_summary_json_reserved_marker(details)
        or (
            details.get("file_key") == "rolling_summary"
            and details.get("write_type") == "current-state"
            and _prepared_input_mode(details) in {"json-file", "json-stdin"}
        )
    )


def _managed_markdown_write_route(details: dict | None) -> dict | None:
    if not details:
        return None
    input_mode = _prepared_input_mode(details)
    if input_mode not in {"file", "stdin"}:
        return None
    route = MANAGED_MARKDOWN_WRITE_ROUTES.get(
        (
            details.get("file_key"),
            details.get("write_type"),
        )
    )
    return dict(route) if route is not None else None


def _append_input_source_args(details: dict | None) -> list[str] | None:
    if not details:
        return None
    input_mode = _prepared_input_mode(details)
    entry_path = details.get("entry_path")
    if input_mode == "json-file":
        entry_arg = _quote_or_placeholder(
            entry_path if isinstance(entry_path, str) else None,
            "entry.json",
        )
        return ["--entry-file", entry_arg, "--input-format", "json"]
    if input_mode == "json-stdin":
        return ["--stdin", "--input-format", "json"]
    if isinstance(entry_path, str) and entry_path.strip():
        return ["--entry-file", shlex.quote(entry_path)]
    if input_mode == "stdin":
        return ["--stdin"]
    return None


def _invalid_prepared_input_suggestion(language: str, details: dict | None) -> str:
    input_mode = _prepared_input_mode(details)
    if _is_rolling_summary_json_builder(details):
        if input_mode == "json-file":
            return _localized_text(
                language,
                en=(
                    "Fix the rolling-summary JSON source file, then rerun the current-state write "
                    "with --source-file and --input-format json."
                ),
                zh_cn=(
                    "请先修正 rolling-summary JSON source file，再用 --source-file 和 "
                    "--input-format json 重新执行 current-state write。"
                ),
            )
        return _localized_text(
            language,
            en=(
                "Fix the rolling-summary JSON payload on stdin, then rerun the current-state write "
                "with --stdin --input-format json."
            ),
            zh_cn=(
                "请先修正 stdin 中的 rolling-summary JSON payload，再用 --stdin "
                "--input-format json 重新执行 current-state write。"
            ),
        )
    managed_markdown_route = _managed_markdown_write_route(details)
    if managed_markdown_route is not None:
        label = managed_markdown_route["label"]
        write_type = managed_markdown_route["write_type"]
        if input_mode == "file":
            return _localized_text(
                language,
                en=(
                    f"Fix the {label} markdown source file, then rerun the {write_type} write "
                    "with --source-file."
                ),
                zh_cn=(
                    f"请先修正 {label} markdown source file，再用 --source-file "
                    f"重新执行 {write_type} write。"
                ),
            )
        return _localized_text(
            language,
            en=(
                f"Fix the {label} markdown payload on stdin, then rerun the {write_type} write "
                "with --stdin."
            ),
            zh_cn=(
                f"请先修正 stdin 中的 {label} markdown payload，再用 --stdin "
                f"重新执行 {write_type} write。"
            ),
        )
    if input_mode == "json-string":
        return _localized_text(
            language,
            en="Fix the JSON object passed via --entry-json, then rerun the helper with a valid daily-log section object.",
            zh_cn="请先修正通过 --entry-json 传入的 JSON 对象，再用合法的 daily-log section 对象重新执行 helper。",
        )
    if input_mode == "json-stdin":
        return _localized_text(
            language,
            en="Fix the JSON payload on stdin, then rerun with --stdin --input-format json.",
            zh_cn="请先修正 stdin 中的 JSON payload，再用 --stdin --input-format json 重新执行。",
        )
    if input_mode == "json-file":
        return _localized_text(
            language,
            en="Fix the JSON payload in the prepared file, then rerun with --entry-file and --input-format json.",
            zh_cn="请先修正 prepared file 里的 JSON payload，再用 --entry-file 和 --input-format json 重新执行。",
        )
    return _localized_text(
        language,
        en=(
            "Fix the prepared entry content first, then rerun the helper with one valid input source. "
            "Use --entry-json for direct JSON, or add --input-format json when stdin or --entry-file carries JSON."
        ),
        zh_cn=(
            "请先修正 prepared entry 内容，再用一个有效输入源重新执行 helper。"
            "直接传 JSON 时使用 --entry-json；如果 JSON 走 stdin 或 --entry-file，请补上 --input-format json。"
        ),
    )


def _invalid_prepared_input_recovery_action(
    script_name: str | None,
    details: dict | None,
) -> str | None:
    helper_name = _normalize_script_name(script_name) or "append_daily_log_entry.py"
    input_mode = _prepared_input_mode(details)
    if _is_rolling_summary_json_builder(details):
        source_path = details.get("source_path") if details else None
        source_arg = _quote_or_placeholder(
            source_path if isinstance(source_path, str) else None,
            "rolling-summary.json",
        )
        project_root = _infer_project_root(details)
        project_arg = _quote_or_placeholder(project_root, "<project-path>")
        if helper_name == "recallloom.py":
            if input_mode == "json-file":
                return (
                    f"Fix the rolling-summary JSON source file, then re-run {helper_name} write "
                    f"{project_arg} --type current-state --source-file {source_arg} "
                    "--input-format json --json."
                )
            return (
                f"Fix the rolling-summary JSON payload on stdin, then re-run {helper_name} write "
                f"{project_arg} --type current-state --stdin --input-format json --json."
            )
        if input_mode == "json-file":
            return (
                f"Fix the rolling-summary JSON source file, then re-run {helper_name} "
                f"{project_arg} --file-key rolling_summary --source-file {source_arg} "
                "--input-format json with fresh expected revisions."
            )
        return (
            f"Fix the rolling-summary JSON payload on stdin, then re-run {helper_name} "
            f"{project_arg} --file-key rolling_summary --stdin --input-format json "
            "with fresh expected revisions."
        )
    managed_markdown_route = _managed_markdown_write_route(details)
    if managed_markdown_route is not None:
        file_key = managed_markdown_route["file_key"]
        write_type = managed_markdown_route["write_type"]
        label = managed_markdown_route["label"]
        source_path = details.get("source_path") if details else None
        source_arg = _quote_or_placeholder(
            source_path if isinstance(source_path, str) else None,
            managed_markdown_route["source_placeholder"],
        )
        project_root = _infer_project_root(details)
        project_arg = _quote_or_placeholder(project_root, "<project-path>")
        dispatcher_file_action = (
            f"Fix the {label} markdown source file, then re-run recallloom.py write "
            f"{project_arg} --type {write_type} --source-file {source_arg} --json."
        )
        dispatcher_stdin_action = (
            f"Fix the {label} markdown payload on stdin, then re-run recallloom.py write "
            f"{project_arg} --type {write_type} --stdin --json."
        )
        if helper_name == "recallloom.py":
            if input_mode == "file":
                return dispatcher_file_action
            return dispatcher_stdin_action
        if input_mode == "file":
            return (
                f"Fix the {label} markdown source file, then re-run {helper_name} "
                f"{project_arg} --file-key {file_key} --source-file {source_arg} "
                f"with fresh expected revisions. Dispatcher equivalent: {dispatcher_file_action}"
            )
        return (
            f"Fix the {label} markdown payload on stdin, then re-run {helper_name} "
            f"{project_arg} --file-key {file_key} --stdin with fresh expected revisions. "
            f"Dispatcher equivalent: {dispatcher_stdin_action}"
        )
    if input_mode == "json-string":
        return f"Re-run {helper_name} with --entry-json and a valid daily-log JSON object."
    if input_mode == "json-stdin":
        return f"Fix the JSON payload on stdin, then re-run {helper_name} with --stdin --input-format json."
    if input_mode == "json-file":
        entry_path = details.get("entry_path") if details else None
        entry_arg = _quote_or_placeholder(
            entry_path if isinstance(entry_path, str) else None,
            "entry.json",
        )
        return (
            f"Fix the JSON payload in the prepared file, then re-run {helper_name} "
            f"with --entry-file {entry_arg} --input-format json."
        )
    return None


def _infer_project_root(details: dict | None) -> str | None:
    def _candidate_project_root(raw_value: str) -> str | None:
        candidate = Path(raw_value)
        if candidate.parent.name == "daily_logs" and candidate.parent.parent.name in _KNOWN_STORAGE_ROOT_NAMES:
            return str(candidate.parent.parent.parent)
        if candidate.parent.name in _KNOWN_STORAGE_ROOT_NAMES:
            return str(candidate.parent.parent)
        return None

    if not details:
        return None
    project_root = details.get("project_root")
    if isinstance(project_root, str) and project_root.strip():
        return project_root
    lock_path = details.get("lock_path")
    if isinstance(lock_path, str) and lock_path.strip():
        lock_candidate = Path(lock_path)
        if lock_candidate.name == ".recallloom-write.lock":
            return str(lock_candidate.parent)
    for key in ("target_path", "path", "latest_active_daily_log"):
        raw_value = details.get(key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        inferred_root = _candidate_project_root(raw_value)
        if inferred_root is not None:
            return inferred_root
    for key in (
        "bridge_targets",
        "existing_targets",
        "invalid_paths",
        "malformed_bridge_targets",
        "missing_paths",
        "unknown_assets",
    ):
        raw_values = details.get(key)
        if not isinstance(raw_values, list):
            continue
        for raw_value in raw_values:
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            inferred_root = _candidate_project_root(raw_value)
            if inferred_root is not None:
                return inferred_root
    return None


def _public_failure_details(details: dict | None) -> dict | None:
    if not details:
        return None
    project_root = _infer_project_root(details) or details.get("project_root")
    publicized = publicize_json_value(
        details,
        project_root=project_root,
        private=private_json_paths_enabled(),
    )
    return publicized if isinstance(publicized, dict) and publicized else None


def _public_failure_error(error: str | None, details: dict | None) -> str | None:
    if not isinstance(error, str) or not error:
        return error
    project_root = _infer_project_root(details) or (details or {}).get("project_root")
    return publicize_text_paths(
        error,
        project_root=project_root,
        private=private_json_paths_enabled(),
    )


def _python_runtime_stage(error: str | None) -> str:
    lowered = (error or "").casefold()
    bootstrap_markers = (
        "runtime bootstrap failed",
        "contract registry bootstrap failed",
        "missing package metadata file",
        "malformed package metadata file",
        "managed assets file",
        "failure-contract registry is invalid",
    )
    if any(marker in lowered for marker in bootstrap_markers):
        return "runtime_bootstrap"
    return "runtime_gate"


def _failure_stage(reason: str, error: str | None) -> str:
    if reason == "python_runtime_unavailable":
        return _python_runtime_stage(error)
    if reason == "package_support_blocked":
        return "package_support_gate"
    return "helper_execution"


def _failure_user_message(reason: str, *, language: str, error: str | None) -> str:
    if reason == "python_runtime_unavailable" and _python_runtime_stage(error) == "runtime_bootstrap":
        return _localized_text(
            language,
            en="RecallLoom cannot start because helper runtime bootstrap failed before execution could begin.",
            zh_cn="RecallLoom 当前无法启动，因为 helper 在真正执行前就遇到了 runtime bootstrap 失败。",
        )
    return failure_reason_contract(reason)["user_message"][language]


def _failure_operator_note(reason: str, *, language: str, error: str | None) -> str | None:
    contract = failure_reason_contract(reason)
    if reason == "python_runtime_unavailable" and _python_runtime_stage(error) == "runtime_bootstrap":
        return _localized_text(
            language,
            en="Repair the RecallLoom bootstrap inputs such as package metadata, managed assets, or contract registry files before retrying.",
            zh_cn="请先修复 RecallLoom 的 bootstrap 输入，例如 package metadata、managed assets 或 contract registry 文件，再重试。",
        )
    operator_note = contract.get("operator_note")
    if operator_note:
        return operator_note[language]
    return None


def _failure_suggestion(
    reason: str,
    *,
    language: str,
    error: str | None,
    details: dict | None,
) -> str:
    if reason == "stale_write_context":
        current_revision = details.get("current_workspace_revision") if details else None
        if isinstance(current_revision, int):
            return _localized_text(
                language,
                en=(
                    f"Refresh the write context first. Re-run preflight, pick up workspace revision "
                    f"{current_revision}, then retry the write."
                ),
                zh_cn=(
                    f"先刷新写入上下文。重新执行 preflight，读取最新的 workspace revision "
                    f"{current_revision}，再重试写入。"
                ),
            )
        return _localized_text(
            language,
            en="Refresh the write context first. Re-run preflight and retry with a fresh workspace revision.",
            zh_cn="先刷新写入上下文。重新执行 preflight，并使用最新的 workspace revision 重试。",
        )
    if reason == "historical_append_requires_confirmation":
        target_date = details.get("target_date") if details else None
        if isinstance(target_date, str) and target_date:
            return _localized_text(
                language,
                en=(
                    f"Only use --allow-historical if you really intend to backfill {target_date}; "
                    "otherwise switch to the latest active daily log before appending."
                ),
                zh_cn=(
                    f"只有在你确实要回填 {target_date} 时才使用 --allow-historical；"
                    "否则请改为向当前最新的 daily log 追加。"
                ),
            )
        return _localized_text(
            language,
            en="Only use --allow-historical when the backfill is intentional; otherwise append to the latest active daily log.",
            zh_cn="只有在确实需要回填历史日志时才使用 --allow-historical；否则请追加到最新的 daily log。",
        )
    if reason == "project_time_policy_review_required":
        logical_workday = details.get("logical_workday") if details else None
        if isinstance(logical_workday, str) and logical_workday:
            return _localized_text(
                language,
                en=(
                    f"Review the project's date policy before writing. The current logical workday is "
                    f"{logical_workday}; confirm that date or choose another explicitly."
                ),
                zh_cn=(
                    f"写入前请先复核项目日期策略。当前逻辑工作日是 {logical_workday}；"
                    "确认这个日期，或显式选择另一个日期后再继续。"
                ),
            )
        return _localized_text(
            language,
            en="Review update_protocol.md and confirm the intended date before writing again.",
            zh_cn="请先检查 update_protocol.md，并确认目标日期后再继续写入。",
        )
    if reason == "invalid_prepared_input":
        return _invalid_prepared_input_suggestion(language, details)
    if reason == "privacy_security_failure":
        return _localized_text(
            language,
            en=(
                "Retry with only supported wrapper metadata keys and short public enum-like values; "
                "do not include private paths, account IDs, tokens, fingerprints, or raw host state."
            ),
            zh_cn=(
                "请只使用受支持的 wrapper metadata 字段和短的 public enum-like 值重试；"
                "不要包含私有路径、账户 ID、token、fingerprint 或原始 host 状态。"
            ),
        )
    if reason == "startup_residue_detected":
        return _localized_text(
            language,
            en=(
                "Review the startup_residue_report, remove only confirmed helper-owned scratch "
                "residue, then rerun the same command."
            ),
            zh_cn="请检查 startup_residue_report，只移除确认属于 helper-owned scratch 的残留后再重新执行同一命令。",
        )
    if reason == "malformed_managed_file":
        return _localized_text(
            language,
            en="Repair the malformed managed file before writing again; do not append on top of a damaged marker layout.",
            zh_cn="请先修复损坏的 managed 文件，再重新写入；不要在损坏的 marker 结构上继续追加。",
        )
    if reason == "write_lock_busy":
        return _localized_text(
            language,
            en="Let the active writer finish, or inspect the lock and only clear it when you are sure it is stale.",
            zh_cn="请等待当前写入完成，或者先检查锁状态；只有确认它已经过期时才清理。",
        )
    if reason == "python_runtime_unavailable":
        if _python_runtime_stage(error) == "runtime_bootstrap":
            return _localized_text(
                language,
                en="This failed before helper execution. Repair the RecallLoom bootstrap/runtime files first, then rerun the helper.",
                zh_cn="这次失败发生在 helper 真正执行之前。请先修复 RecallLoom 的 bootstrap/runtime 文件，再重新运行 helper。",
            )
        return _localized_text(
            language,
            en="Run the helper with a compatible Python 3.10+ interpreter before retrying any RecallLoom action.",
            zh_cn="请先用兼容的 Python 3.10+ 解释器运行这个 helper，再重试 RecallLoom 动作。",
        )
    contract = failure_reason_contract(reason)
    operator_note = contract.get("operator_note")
    if operator_note:
        return operator_note[language]
    return contract["user_message"][language]


def _failure_recovery_command(
    reason: str,
    *,
    script_name: str | None,
    error: str | None,
    details: dict | None,
) -> str:
    project_root = _infer_project_root(details)
    project_arg = _quote_or_placeholder(project_root, "<project-path>")
    target_date = details.get("target_date") if details else None
    logical_workday = details.get("logical_workday") if details else None
    current_revision = details.get("current_workspace_revision") if details else None
    entry_source_args = _append_input_source_args(details)
    can_retry_append = (
        isinstance(project_root, str)
        and isinstance(current_revision, int)
        and entry_source_args is not None
    )

    def _append_retry_command(*, date_value: str, allow_historical: bool = False) -> str | None:
        if not can_retry_append:
            return None
        command_args = [
            project_arg,
            "--date",
            date_value,
            *entry_source_args,
            "--expected-workspace-revision",
            str(current_revision),
        ]
        if allow_historical:
            command_args.append("--allow-historical")
        command_args.append("--json")
        return _script_command("append_daily_log_entry.py", *command_args)

    if reason == "python_runtime_unavailable":
        if _python_runtime_stage(error) == "runtime_bootstrap":
            return (
                "Repair skills/recallloom/package-metadata.json, "
                "skills/recallloom/managed-assets.json, or the contract registry bootstrap inputs, "
                "then rerun the helper with Python 3.10+."
            )
        return _script_command(script_name, "...")
    if reason in {"not_project_root", "no_project_root", "invalid_storage_boundary"}:
        return _script_command("init_context.py", project_arg, "--json")
    if reason in {"damaged_sidecar", "dual_sidecar_conflict", "malformed_managed_file"}:
        if isinstance(project_root, str):
            return _script_command("validate_context.py", project_arg, "--json")
        return "Repair the managed RecallLoom files, then rerun validate_context.py from the project root."
    if reason == "attach_scan_blocked":
        return "Edit the prepared text to remove blocked content, then rerun the same helper command."
    if reason == "invalid_date":
        return _script_command(script_name, project_arg, "--date", "YYYY-MM-DD", "--json")
    if reason == "invalid_tool_name":
        return _script_command(script_name, project_arg, "--writer-id", "RecallLoom", "--json")
    if reason == "reinit_create_daily_log_not_allowed":
        if isinstance(target_date, str) and target_date:
            command = _append_retry_command(date_value=target_date)
            if command is not None:
                return command
        return "Create new milestone content with append_daily_log_entry.py using --entry-file or --stdin instead of --create-daily-log."
    if reason == "stale_write_context":
        if isinstance(project_root, str):
            return _script_command("preflight_context_check.py", project_arg, "--json")
        return "Rerun preflight_context_check.py from the project root, then retry with the fresh workspace revision."
    if reason == "write_lock_busy":
        if isinstance(project_root, str):
            return _script_command("unlock_write_lock.py", project_arg, "--json")
        return "Wait for the active writer to finish, then rerun the helper after the lock clears."
    if reason == "invalid_prepared_input":
        if _is_rolling_summary_json_builder(details):
            source_action = _invalid_prepared_input_recovery_action(script_name, details)
            if source_action is not None:
                return source_action
        if _managed_markdown_write_route(details) is not None:
            source_action = _invalid_prepared_input_recovery_action(script_name, details)
            if source_action is not None:
                return source_action
        retry_date = target_date if isinstance(target_date, str) and target_date else None
        if retry_date is not None:
            command = _append_retry_command(date_value=retry_date)
            if command is not None:
                return command
        source_action = _invalid_prepared_input_recovery_action(script_name, details)
        if source_action is not None:
            return source_action
        return (
            "Provide exactly one prepared entry source with --entry-json, --entry-file, or --stdin; "
            "use --input-format json for JSON file/stdin input, then rerun append_daily_log_entry.py "
            "from the project root with the current workspace revision."
        )
    if reason == "privacy_security_failure":
        return "Remove unsafe wrapper metadata fields or values, then rerun the same helper command."
    if reason == "startup_residue_detected":
        return "Inspect the public-safe startup_residue_report, remove confirmed helper scratch residue, then rerun the same command."
    if reason == "historical_append_requires_confirmation":
        append_date = target_date if isinstance(target_date, str) and target_date else None
        if append_date is not None:
            command = _append_retry_command(date_value=append_date, allow_historical=True)
            if command is not None:
                return command
        return "Use --allow-historical only for an intentional backfill, then rerun append_daily_log_entry.py from the project root."
    if reason == "project_time_policy_review_required":
        append_date = logical_workday if isinstance(logical_workday, str) and logical_workday else None
        if append_date is not None:
            command = _append_retry_command(date_value=append_date)
            if command is not None:
                return command
        return "Review the project date policy, then rerun append_daily_log_entry.py from the project root with the confirmed date."
    if reason == "trust_review_required":
        if isinstance(project_root, str):
            return _script_command("preflight_context_check.py", project_arg, "--json")
        return "Review the current continuity files, then rerun preflight_context_check.py."
    if reason == "continuity_drift_review_required":
        if isinstance(project_root, str):
            return _script_command("summarize_continuity_status.py", project_arg, "--json")
        return "Refresh the rolling summary, then rerun summarize_continuity_status.py."
    if reason == "storage_cleanup_incomplete":
        return "Delete the tombstone storage directory, confirm cleanup, then rerun the original removal command."
    if reason == "registry_contract_invalid":
        return "Repair the failure-contract registry definition, then rerun the helper bootstrap."
    if reason == "package_support_blocked":
        return "Run npx skills update, refresh the installed recallloom package, then rerun the helper."
    return "Review the error details, fix the blocking issue, and rerun the same helper command."


def preferred_failure_language(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    lang = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    return "zh-CN" if lang.lower().startswith("zh") else "en"


def normalize_failure_reason(reason: str) -> str:
    normalized = FAILURE_REASON_ALIASES.get(reason, reason)
    if normalized not in FAILURE_REASON_REGISTRY:
        raise RuntimeError(f"Unknown failure reason: {reason}")
    return normalized


def failure_reason_contract(reason: str) -> dict:
    return FAILURE_REASON_REGISTRY[normalize_failure_reason(reason)]


def failure_payload(
    reason: str,
    *,
    language: str,
    error: str | None = None,
    details: dict | None = None,
    findings: list | None = None,
    extra: dict | None = None,
    script_name: str | None = None,
) -> dict:
    normalized_reason = normalize_failure_reason(reason)
    contract = failure_reason_contract(normalized_reason)
    normalized_script_name = _normalize_script_name(script_name)
    normalized_details = _public_failure_details(details)
    normalized_error = _public_failure_error(error, details)
    payload = {
        "ok": False,
        "schema_version": FAILURE_PAYLOAD_SCHEMA_VERSION,
        "blocked": contract["blocked"],
        "blocked_reason": normalized_reason,
        "recoverability": contract["recoverability"],
        "surface_level": contract["surface_level"],
        "trust_effect": contract["trust_effect"],
        "failure_stage": _failure_stage(normalized_reason, error),
        "next_actions": list(contract["next_actions"]),
        "user_message": _failure_user_message(
            normalized_reason,
            language=language,
            error=error,
        ),
        "suggestion": _failure_suggestion(
            normalized_reason,
            language=language,
            error=error,
            details=normalized_details,
        ),
        "recovery_command": _failure_recovery_command(
            normalized_reason,
            script_name=normalized_script_name,
            error=error,
            details=normalized_details,
        ),
    }
    if normalized_error is not None:
        payload["error"] = normalized_error
    operator_note = _failure_operator_note(
        normalized_reason,
        language=language,
        error=error,
    )
    if operator_note:
        payload["operator_note"] = operator_note
    if normalized_details:
        payload["details"] = normalized_details
    if findings:
        payload["findings"] = findings
    if extra:
        payload.update(extra)
    if not payload.get("schema_version"):
        payload["schema_version"] = FAILURE_PAYLOAD_SCHEMA_VERSION
    if not payload.get("next_actions"):
        payload["next_actions"] = list(contract["next_actions"])
    if not payload.get("suggestion"):
        payload["suggestion"] = _failure_suggestion(
            normalized_reason,
            language=language,
            error=error,
            details=normalized_details,
        )
    if not payload.get("recovery_command"):
        payload["recovery_command"] = _failure_recovery_command(
            normalized_reason,
            script_name=normalized_script_name,
            error=error,
            details=normalized_details,
        )
    publicized_payload = publicize_json_value(
        payload,
        project_root=_infer_project_root(details) or (details or {}).get("project_root"),
        private=private_json_paths_enabled(),
    )
    return publicized_payload if isinstance(publicized_payload, dict) else payload
