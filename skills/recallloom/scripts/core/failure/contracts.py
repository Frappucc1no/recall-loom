"""Stable failure contracts and reason registry for RecallLoom."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import sys

from core.output.privacy import (
    private_json_paths_enabled,
    publicize_json_value,
    publicize_text_paths,
    redact_public_text,
)


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
        "next_actions": [
            "validate_context.py",
            "stage_recovery_proposal.py",
            "record_recovery_review.py",
            "prepare_recovery_promotion.py",
        ],
        "user_message": {
            "en": "The existing RecallLoom workspace is not trustworthy yet and needs repair before continuing.",
            "zh-CN": "当前已有的 RecallLoom 工作区还不可信，需要先修复后再继续。",
        },
        "operator_note": {
            "en": "Use validate_context.py and the recovery proposal/review/promotion helpers; do not hand-edit managed sidecar files.",
            "zh-CN": "请使用 validate_context.py 以及 recovery proposal/review/promotion helpers；不要手工编辑 managed sidecar 文件。",
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
    "review_imported_baseline_confirmation_required": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["review_preflight_readiness", "retry_with_confirm_review_imported_baseline"],
        "user_message": {
            "en": "This reviewed imported baseline requires explicit confirmation before a mutating write.",
            "zh-CN": "这个已复核导入的 baseline 在执行变更写入前需要显式确认。",
        },
        "operator_note": {
            "en": "Review the preflight readiness output, then retry with --confirm-review-imported-baseline if the write is intentional.",
            "zh-CN": "请先复核 preflight readiness 输出；若确认要写入，再使用 --confirm-review-imported-baseline 重试。",
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
        "next_actions": [
            "validate_context.py",
            "stage_recovery_proposal.py",
            "record_recovery_review.py",
            "prepare_recovery_promotion.py",
        ],
        "user_message": {
            "en": "A managed RecallLoom file is malformed and must be repaired before continuing.",
            "zh-CN": "存在损坏的 RecallLoom managed 文件，需要先修复后再继续。",
        },
        "operator_note": {
            "en": "Use validate_context.py and the recovery proposal/review/promotion helpers instead of bypassing marker or section checks.",
            "zh-CN": "请使用 validate_context.py 以及 recovery proposal/review/promotion helpers，不要绕过 marker 或 section 校验。",
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
    "historical_append_not_receipt_backed": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["append_to_latest_daily_log", "wait_for_historical_append_receipt_support"],
        "user_message": {
            "en": "Historical daily-log appends are not part of the current receipt-backed write contract.",
            "zh-CN": "历史 daily-log 追加还不属于当前 receipt-backed 写入合同。",
        },
        "operator_note": {
            "en": "Append only to the current latest daily-log cursor for receipt-backed provenance in this package line.",
            "zh-CN": "在当前包版本中，只有追加到当前最新 daily-log cursor 才能进入 receipt-backed provenance 路径。",
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
        "next_actions": [
            "stage_recovery_proposal.py",
            "record_recovery_review.py",
            "prepare_recovery_promotion.py",
            "preflight_context_check.py",
        ],
        "user_message": {
            "en": "RecallLoom needs a continuity review before a higher-risk action can continue.",
            "zh-CN": "在继续更高风险动作前，需要先复核当前 RecallLoom continuity。",
        },
        "operator_note": {
            "en": "Use the recovery proposal/review/promotion helpers, then rerun preflight before mutating sidecar state.",
            "zh-CN": "请先使用 recovery proposal/review/promotion helpers，然后重新运行 preflight，再修改 sidecar state。",
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
    "unsupported_mutating_surface": {
        "blocked": True,
        "recoverability": "user_input_required",
        "surface_level": "operator",
        "trust_effect": "review_required",
        "next_actions": ["run_preview_mode", "wait_for_receipt_backed_surface_support"],
        "user_message": {
            "en": "This mutating surface is not part of the current receipt-backed write contract.",
            "zh-CN": "这个写入入口还不属于当前 receipt-backed 写入合同。",
        },
        "operator_note": {
            "en": "Use preview/read-only mode for this surface until it has its own receipt-backed transaction path.",
            "zh-CN": "在该入口具备自己的 receipt-backed transaction 路径之前，请只使用 preview/read-only 模式。",
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

_INVALID_LOCAL_SUPPORT_CACHE_REASON_CODES = frozenset(
    (
        "support_cache_unreadable",
        "support_cache_non_utf8",
        "support_cache_malformed_json",
        "support_cache_not_object",
        "support_cache_wrong_package_path",
        "support_cache_invalid_checked_date",
        "support_cache_invalid_advisory",
    )
)

_CANONICAL_DETAIL_REASON_ROUTES = {
    "receipt_store_not_written_verified": {
        "blocked": True,
        "recoverability": "retryable",
        "trust_effect": "review_required",
        "next_actions": [
            "run_read_only_status",
            "rerun_fresh_preflight",
        ],
        "next_action": "run_read_only_status_then_fresh_preflight_before_retry",
        "side_effect": "target_and_state_written_receipt_store_verified_unchanged",
        "safe_to_retry": False,
        "single_next_command": "recallloom.py status <project-path> --json",
        "user_message": {
            "en": (
                "The target and state were written while the receipt store was verified "
                "unchanged. Diagnose read-only, then obtain a fresh preflight before retrying."
            ),
            "zh-CN": (
                "目标文件和状态已经写入，同时 receipt store 已确认保持不变。"
                "请先只读诊断，再获取 fresh preflight 后重试。"
            ),
        },
        "operator_note": {
            "en": (
                "Do not retry the helper directly. Run read-only status, then retry only from "
                "a newly issued preflight binding."
            ),
            "zh-CN": (
                "不要直接重试 helper。请先运行只读 status，再仅使用新签发的 preflight "
                "binding 重试。"
            ),
        },
        "suggestion": {
            "en": (
                "Run read-only status first, then rerun preflight and use only the fresh binding; "
                "never directly retry the previous helper invocation."
            ),
            "zh-CN": (
                "请先运行只读 status，再重新执行 preflight 并仅使用 fresh binding；"
                "不要直接重试上一次 helper 调用。"
            ),
        },
        "recovery_command": (
            "Run recallloom.py status <project-path> --json, then rerun fresh preflight "
            "before retrying the mutation."
        ),
    },
    "concurrent_external_modification_detected": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "trust_effect": "damaged",
        "next_actions": [
            "run_read_only_status",
            "run_read_only_validate",
        ],
        "next_action": "run_read_only_status_or_validate",
        "safe_to_retry": False,
        "single_next_command": "recallloom.py status <project-path> --json",
        "user_message": {
            "en": (
                "External modification was detected during the bounded write. Preserve the "
                "observed state and diagnose it read-only."
            ),
            "zh-CN": (
                "在有界写入期间检测到外部修改。请保留当前观测状态，并仅做只读诊断。"
            ),
        },
        "operator_note": {
            "en": (
                "Run only status or validate next. Do not roll back and do not retry the write "
                "from the prior context."
            ),
            "zh-CN": (
                "下一步只能运行 status 或 validate。不要回滚，也不要沿用之前的写入上下文重试。"
            ),
        },
        "suggestion": {
            "en": (
                "Use read-only status or validation to establish the current state; do not "
                "roll back or retry the mutation."
            ),
            "zh-CN": (
                "请使用只读 status 或 validation 确认当前状态；不要回滚或重试该变更。"
            ),
        },
        "recovery_command": (
            "Run recallloom.py status <project-path> --json or recallloom.py validate "
            "<project-path> --json; do not roll back or retry the mutation."
        ),
    },
    "state_write_failed_target_preserved": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "trust_effect": "damaged",
        "next_actions": [
            "run_read_only_status",
            "run_read_only_validate",
        ],
        "next_action": "run_read_only_status_or_validate",
        "side_effect": "write_attempted",
        "safe_to_retry": False,
        "single_next_command": "recallloom.py status <project-path> --json",
        "user_message": {
            "en": (
                "The target was written but the state write failed. Preserve all bytes and "
                "diagnose the resulting mismatch read-only."
            ),
            "zh-CN": (
                "目标文件已写入，但 state 写入失败。请保留全部字节，并仅通过只读诊断该不一致状态。"
            ),
        },
        "operator_note": {
            "en": (
                "Run only status or validate next. Do not roll back and do not retry the "
                "write from the prior context."
            ),
            "zh-CN": (
                "下一步只能运行 status 或 validate。不要回滚，也不要沿用之前的写入上下文重试。"
            ),
        },
        "suggestion": {
            "en": (
                "Use read-only status or validation to establish the current state; do not "
                "roll back or retry the mutation."
            ),
            "zh-CN": (
                "请使用只读 status 或 validation 确认当前状态；不要回滚或重试该变更。"
            ),
        },
        "recovery_command": (
            "Run recallloom.py status <project-path> --json or recallloom.py validate "
            "<project-path> --json; do not roll back or retry the mutation."
        ),
    },
    "post_hash_inconsistent_review_eligible": {
        "blocked": True,
        "recoverability": "user_input_required",
        "trust_effect": "review_required",
        "next_actions": [
            "prepare_exact_bound_recovery_proposal",
            "prepare_exact_bound_recovery_review",
        ],
        "next_action": "prepare_exact_binding_manual_recovery_materials",
        "safe_to_retry": False,
        "single_next_command": (
            "stage_recovery_proposal.py <project-path> "
            "--source-file <exact-d5-proposal.md> --json"
        ),
        "user_message": {
            "en": (
                "This inconsistent-evidence state is eligible for manual review using the exact "
                "current binding."
            ),
            "zh-CN": (
                "当前不一致证据状态可以进入人工复核，但必须使用精确的当前 binding。"
            ),
        },
        "operator_note": {
            "en": (
                "Prepare the proposal and review material against the exact binding digest; "
                "keep diagnosis read-only until those materials are ready."
            ),
            "zh-CN": (
                "请针对精确 binding digest 准备 proposal 和 review 材料；"
                "材料就绪前保持只读诊断。"
            ),
        },
        "suggestion": {
            "en": (
                "Prepare exact-binding manual recovery proposal and review material before any "
                "promotion attempt."
            ),
            "zh-CN": (
                "在尝试 promotion 前，请先准备与 exact binding 对应的人工 recovery proposal "
                "和 review 材料。"
            ),
        },
        "recovery_command": (
            "Prepare exact-binding D5 proposal and review material, then run "
            "stage_recovery_proposal.py <project-path> --source-file "
            "<exact-d5-proposal.md> --json."
        ),
    },
    "post_hash_inconsistent_review_binding_changed": {
        "blocked": True,
        "recoverability": "user_input_required",
        "trust_effect": "review_required",
        "next_actions": [
            "rerun_read_only_changed_provenance_validation",
            "repeat_manual_inconsistent_review",
        ],
        "next_action": "rerun_read_only_validation_then_repeat_manual_review",
        "safe_to_retry": False,
        "single_next_command": (
            "recallloom.py validate <project-path> --require-provenance "
            "--changed-only --json"
        ),
        "user_message": {
            "en": (
                "The inconsistent-review binding or its material changed. Earlier proposal or "
                "review material must not be reused."
            ),
            "zh-CN": (
                "inconsistent-review binding 或其材料已经变化，不能复用之前的 proposal 或 review。"
            ),
        },
        "operator_note": {
            "en": (
                "Rerun read-only changed-only provenance validation and repeat the manual review "
                "against the new exact binding."
            ),
            "zh-CN": (
                "请重新运行只读 changed-only provenance validation，并针对新的 exact binding "
                "重新人工复核。"
            ),
        },
        "suggestion": {
            "en": (
                "Rerun read-only validation, inspect the new binding, and restart the manual "
                "review; do not reuse earlier material."
            ),
            "zh-CN": (
                "请重新运行只读 validation，检查新的 binding，并重新开始人工复核；"
                "不要复用旧材料。"
            ),
        },
        "recovery_command": (
            "Run recallloom.py validate <project-path> --require-provenance "
            "--changed-only --json, then repeat manual review against the new binding."
        ),
    },
    "review_imported_baseline_material_invalid": {
        "blocked": True,
        "recoverability": "operator_repair_required",
        "trust_effect": "damaged",
        "next_actions": [
            "run_read_only_status",
            "run_read_only_validate",
        ],
        "next_action": "run_read_only_status_and_validate_before_operator_repair",
        "side_effect": "external_target_modification_preserved",
        "safe_to_retry": False,
        "single_next_command": "recallloom.py status <project-path> --json",
        "user_message": {
            "en": (
                "The reviewed-baseline state was committed, but its exact proposal or review "
                "material is no longer valid. All observed bytes were preserved."
            ),
            "zh-CN": (
                "reviewed-baseline 状态已经提交，但其精确 proposal 或 review 材料已无效。"
                "所有观测到的字节均已保留。"
            ),
        },
        "operator_note": {
            "en": (
                "Run status and validation read-only, then repair the reviewed-baseline "
                "material through the operator recovery process. Do not retry promotion or "
                "reuse the prior D5 binding."
            ),
            "zh-CN": (
                "先只读运行 status 与 validation，再通过 operator 修复流程修复 "
                "reviewed-baseline 材料。不要重试 promotion，也不要复用之前的 D5 binding。"
            ),
        },
        "suggestion": {
            "en": (
                "Preserve the current state, diagnose it read-only, and repair the committed "
                "reviewed-baseline material before any later mutation."
            ),
            "zh-CN": (
                "保留当前状态，只读诊断，并在任何后续写入前修复已提交的 "
                "reviewed-baseline 材料。"
            ),
        },
        "recovery_command": (
            "Run recallloom.py status <project-path> --json and recallloom.py validate "
            "<project-path> --require-provenance --changed-only --json, then repair the "
            "reviewed-baseline material through the operator recovery process."
        ),
    },
    "post_hash_inconsistent_review_promotion_not_committed": {
        "blocked": True,
        "recoverability": "user_input_required",
        "trust_effect": "review_required",
        "next_actions": [
            "inspect_exact_orphan_review_read_only",
            "explicitly_reuse_exact_orphan_review",
        ],
        "next_action": "inspect_exact_orphan_then_explicitly_reuse_it",
        "safe_to_retry": False,
        "single_next_command": (
            "recallloom.py validate <project-path> --require-provenance "
            "--changed-only --json"
        ),
        "user_message": {
            "en": (
                "Exact review evidence exists, but the reviewed-baseline state promotion was not "
                "committed."
            ),
            "zh-CN": (
                "精确 review evidence 已存在，但 reviewed-baseline 状态 promotion 尚未提交。"
            ),
        },
        "operator_note": {
            "en": (
                "Inspect the exact orphan review read-only. Reuse it only through an explicit "
                "promotion attempt with the same binding and review bytes."
            ),
            "zh-CN": (
                "请先只读检查精确 orphan review。只有在 binding 与 review bytes 完全一致时，"
                "才能通过显式 promotion 尝试复用它。"
            ),
        },
        "suggestion": {
            "en": (
                "Validate the exact orphan read-only, then explicitly reuse only that same binding "
                "and review bytes; do not perform a generic retry."
            ),
            "zh-CN": (
                "请先只读验证精确 orphan，再仅显式复用同一 binding 和 review bytes；"
                "不要进行普通重试。"
            ),
        },
        "recovery_command": (
            "Run recallloom.py validate <project-path> --require-provenance "
            "--changed-only --json, inspect the exact orphan, then explicitly reuse only the "
            "same binding and review bytes."
        ),
    },
}

_GENERIC_RECEIPT_FAILURE_ROUTE = {
    "blocked": True,
    "recoverability": "operator_repair_required",
    "trust_effect": "review_required",
    "next_actions": [
        "run_read_only_status",
        "run_read_only_validate",
    ],
    "next_action": "run_read_only_status_and_validate_before_operator_repair",
    "safe_to_retry": False,
    "single_next_command": "recallloom.py status <project-path> --json",
    "user_message": {
        "en": (
            "Receipt finalization is not trustworthy enough to retry. Diagnose the current state "
            "read-only before operator repair."
        ),
        "zh-CN": (
            "receipt finalization 当前不够可信，不能重试。请先只读诊断当前状态，再由 operator 修复。"
        ),
    },
    "operator_note": {
        "en": (
            "Treat this receipt failure as blocked. Run status and validation read-only; do not "
            "retry the mutation until the evidence is repaired."
        ),
        "zh-CN": (
            "请把这类 receipt failure 视为阻断状态。仅运行只读 status 和 validation；"
            "证据修复前不要重试变更。"
        ),
    },
    "suggestion": {
        "en": (
            "Run read-only status and validation, then repair receipt evidence before considering "
            "another mutation."
        ),
        "zh-CN": (
            "请先运行只读 status 和 validation，再修复 receipt evidence，之后才能考虑新的变更。"
        ),
    },
    "recovery_command": (
        "Run recallloom.py status <project-path> --json and recallloom.py validate "
        "<project-path> --json before operator repair; do not retry the mutation."
    ),
}

_HELPER_RECEIPT_FINALIZATION_DETAIL_ROUTE = {
    "next_action": "review_or_repair_receipt_store_before_claiming_helper_evidenced",
}

_INVALID_LOCAL_SUPPORT_CACHE_ROUTE = {
    "blocked": True,
    "recoverability": "operator_repair_required",
    "trust_effect": "review_required",
    "next_actions": [
        "run_read_only_support_diagnostic",
        "refresh_or_repair_package_support_cache",
        "remove_package_scoped_support_cache_and_retry",
    ],
    "next_action": "diagnose_then_refresh_repair_or_remove_package_support_cache",
    "safe_to_retry": False,
    "single_next_command": "recallloom.py status <project-path> --json",
    "user_message": {
        "en": (
            "The package-scoped support cache is invalid and needs operator repair before this "
            "action can continue."
        ),
        "zh-CN": (
            "当前 package-scoped support cache 无效，需要 operator 修复后才能继续。"
        ),
    },
    "operator_note": {
        "en": (
            "Diagnose read-only, then refresh or atomically repair the package-scoped cache; if "
            "that cannot succeed, remove only that cache and retry the support check."
        ),
        "zh-CN": (
            "请先只读诊断，再在线刷新或原子修复 package-scoped cache；若无法完成，"
            "仅移除该 cache 后重试 support check。"
        ),
    },
    "suggestion": {
        "en": (
            "Run a read-only support diagnosis, refresh or atomically repair the local support "
            "cache, or remove only the package-scoped cache before retrying."
        ),
        "zh-CN": (
            "请运行只读 support diagnosis，在线刷新或原子修复本地 support cache；"
            "也可以仅移除 package-scoped cache 后重试。"
        ),
    },
    "recovery_command": (
        "Run read-only package support diagnosis, then refresh or atomically repair the "
        "package-scoped support cache; if needed, remove only that cache and retry the "
        "support check."
    ),
}

_INVALID_SUPPORT_ADVISORY_ROUTE = {
    "blocked": True,
    "recoverability": "operator_repair_required",
    "trust_effect": "review_required",
    "next_actions": [
        "correct_or_refresh_package_support_advisory",
        "rerun_package_support_check",
    ],
    "next_action": "correct_or_refresh_advisory_then_rerun_support_check",
    "safe_to_retry": False,
    "single_next_command": "recallloom.py status <project-path> --json",
    "user_message": {
        "en": (
            "The configured package support advisory is invalid. Correct or refresh it before "
            "rerunning the support check."
        ),
        "zh-CN": (
            "当前配置的 package support advisory 无效。请先修正或刷新 advisory，"
            "再重新运行 support check。"
        ),
    },
    "operator_note": {
        "en": (
            "Correct or refresh the configured advisory, then rerun the package support check."
        ),
        "zh-CN": (
            "请修正或刷新当前配置的 advisory，然后重新运行 package support check。"
        ),
    },
    "suggestion": {
        "en": (
            "Correct or refresh the configured package support advisory, then rerun the support check."
        ),
        "zh-CN": (
            "请修正或刷新当前配置的 package support advisory，然后重新运行 support check。"
        ),
    },
    "recovery_command": (
        "Correct or refresh the configured package support advisory, then rerun the support check."
    ),
}


def _localized_text(language: str, *, en: str, zh_cn: str) -> str:
    return zh_cn if language == "zh-CN" else en


def _localized_reason_route(template: dict, *, language: str) -> dict:
    route: dict = {}
    for key, value in template.items():
        if key in {"user_message", "operator_note", "suggestion"}:
            route[key] = value[language]
        elif isinstance(value, list):
            route[key] = list(value)
        else:
            route[key] = value
    return route


def canonical_detail_reason_contract(
    reason_code: str | None,
    *,
    language: str = "en",
) -> dict | None:
    """Return the canonical public route for a nested failure reason code."""

    if language not in {"en", "zh-CN"}:
        language = "en"
    template = _CANONICAL_DETAIL_REASON_ROUTES.get(reason_code)
    if template is None and isinstance(reason_code, str) and (
        reason_code.startswith("receipt_store_")
        or reason_code.startswith("receipt_failure_")
    ):
        template = _GENERIC_RECEIPT_FAILURE_ROUTE
    if template is None:
        return None
    return _localized_reason_route(template, language=language)


def _canonical_failure_route(
    reason: str,
    *,
    language: str,
    details: dict | None,
) -> dict | None:
    reason_code = details.get("reason_code") if isinstance(details, dict) else None
    if reason == "package_support_blocked" and reason_code == "invalid_support_advisory":
        return _localized_reason_route(
            _INVALID_SUPPORT_ADVISORY_ROUTE,
            language=language,
        )
    if (
        reason == "package_support_blocked"
        and reason_code in _INVALID_LOCAL_SUPPORT_CACHE_REASON_CODES
    ):
        return _localized_reason_route(
            _INVALID_LOCAL_SUPPORT_CACHE_ROUTE,
            language=language,
        )
    route = canonical_detail_reason_contract(reason_code, language=language)
    if reason_code == "concurrent_external_modification_detected" and route is not None:
        route = dict(route)
        side_effect = details.get("side_effect") if isinstance(details, dict) else None
        if isinstance(side_effect, str) and side_effect:
            route["side_effect"] = side_effect
    if (
        route is None
        and isinstance(details, dict)
        and details.get("receipt_store_file") == "derived/helper-receipts.json"
        and details.get("receipt_finalization_status")
        in {"failed", "blocked_before_write"}
    ):
        route = dict(_HELPER_RECEIPT_FINALIZATION_DETAIL_ROUTE)
    return route


def _canonicalize_failure_details(
    reason: str,
    *,
    language: str,
    details: dict | None,
) -> dict | None:
    if not isinstance(details, dict):
        return None
    canonical_details = dict(details)
    route = _canonical_failure_route(
        reason,
        language=language,
        details=canonical_details,
    )
    if route is not None and isinstance(route.get("next_action"), str):
        canonical_details["next_action"] = route["next_action"]
    if route is not None and isinstance(route.get("side_effect"), str):
        canonical_details["side_effect"] = route["side_effect"]
    return canonical_details


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
    if input_mode == "json-string":
        return ["--entry-json", "'<prepared-entry-json>'"]
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


def _append_placeholder_args(details: dict | None) -> list[str]:
    input_mode = _prepared_input_mode(details)
    input_format = details.get("input_format") if isinstance(details, dict) else None
    if input_mode == "json-string":
        return ["--entry-json", "'<prepared-entry-json>'"]
    if input_mode == "json-file":
        return ["--entry-file", "<entry.json>", "--input-format", "json"]
    if input_mode == "json-stdin":
        return ["--stdin", "--input-format", "json"]
    if input_mode == "stdin":
        args = ["--stdin"]
        if input_format == "json":
            args.extend(["--input-format", "json"])
        return args
    if input_mode == "file":
        args = ["--entry-file", "<prepared-entry.md>"]
        if input_format == "json":
            args = ["--entry-file", "<entry.json>", "--input-format", "json"]
        return args
    if input_mode in {"ambiguous", "missing"}:
        return ["--entry-json", "'<prepared-entry-json>'"]
    return ["--entry-file", "<prepared-entry.md>"]


def _invalid_prepared_input_suggestion(language: str, details: dict | None) -> str:
    input_mode = _prepared_input_mode(details)
    command = details.get("command") if isinstance(details, dict) else None
    operation = details.get("operation") if isinstance(details, dict) else None
    if command == "repair-daily-log-cursor" or operation == "repair_daily_log_cursor":
        reason_code = details.get("reason_code") if isinstance(details, dict) else None
        if reason_code == "repair_apply_requires_preview_binding":
            return _localized_text(
                language,
                en=(
                    "Run repair-daily-log-cursor preview first, review the preview digest "
                    "and confirmation material, then retry apply with the fresh preview binding."
                ),
                zh_cn=(
                    "请先运行 repair-daily-log-cursor preview，复核 preview digest 和确认材料，"
                    "再携带 fresh preview binding 重试 apply。"
                ),
            )
        return _localized_text(
            language,
            en="Fix the repair cursor arguments, then rerun repair-daily-log-cursor preview.",
            zh_cn="请先修正 repair cursor 参数，再重新运行 repair-daily-log-cursor preview。",
        )
    if command == "validate":
        return _localized_text(
            language,
            en=(
                "Fix the validate flag combination, then rerun validate. Use "
                "--require-provenance with exactly one scope flag: --changed-only or --full."
            ),
            zh_cn=(
                "请先修正 validate 参数组合，再重新运行 validate。使用 "
                "--require-provenance 时必须且只能选择 --changed-only 或 --full。"
            ),
        )
    if command == "sync-current-state-after-append" or operation == "post_append_summary_sync":
        return _localized_text(
            language,
            en=(
                "Fix the reviewed rolling-summary JSON payload on stdin, then rerun "
                "sync-current-state-after-append with --stdin --input-format json."
            ),
            zh_cn=(
                "请先修正 stdin 中已复核的 rolling-summary JSON payload，再用 "
                "sync-current-state-after-append --stdin --input-format json 重试。"
            ),
        )
    if command == "archive" or operation == "daily_log_archive":
        return _localized_text(
            language,
            en="Fix the archive arguments, then rerun archive preview or status.",
            zh_cn="请先修正 archive 参数，再重新运行 archive preview 或 status。",
        )
    if command == "write" or operation == "managed_file_commit":
        return _localized_text(
            language,
            en=(
                "Fix the managed-file source selection, then rerun write with one "
                "explicit --type and exactly one of --source-file or --stdin."
            ),
            zh_cn=(
                "请先修正 managed-file 输入源选择，再用一个明确的 --type，"
                "并在 --source-file 或 --stdin 中二选一重试 write。"
            ),
        )
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
    command = details.get("command") if isinstance(details, dict) else None
    operation = details.get("operation") if isinstance(details, dict) else None
    if (
        helper_name == "repair_daily_log_cursor.py"
        or command == "repair-daily-log-cursor"
        or operation == "repair_daily_log_cursor"
    ):
        return (
            "Fix the repair cursor arguments, then re-run recallloom.py "
            "repair-daily-log-cursor <project-path> --json."
        )
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
    public_error = publicize_text_paths(
        error,
        project_root=project_root,
        private=private_json_paths_enabled(),
    )
    return redact_public_text(public_error, project_root=project_root, private=False)


def _is_archive_before_date_invalid(details: dict | None) -> bool:
    if not isinstance(details, dict):
        return False
    if details.get("reason_code") == "archive_before_date_invalid":
        return True
    return details.get("operation") == "daily_log_archive" and "before" in details


def _archive_before_invalid_value(details: dict | None) -> str | None:
    if not _is_archive_before_date_invalid(details):
        return None
    for key in ("invalid_value", "before"):
        value = details.get(key) if isinstance(details, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def _archive_before_recovery_command(details: dict | None) -> str:
    return _script_command(
        "archive_logs.py",
        _dispatcher_project_arg(details),
        "--before",
        "<valid-before-date>",
        "--json",
    )


def _archive_before_single_next_command(details: dict | None) -> str:
    return (
        f"archive_logs.py {_dispatcher_project_arg(details)} "
        "--before <valid-before-date> --json"
    )


def _augment_archive_before_details(reason: str, details: dict | None) -> None:
    if reason != "invalid_date" or not isinstance(details, dict):
        return
    invalid_value = _archive_before_invalid_value(details)
    if invalid_value is None:
        return
    details.setdefault("invalid_value", invalid_value)
    details.setdefault("argument", "--before")
    details.setdefault("expected_format", "YYYY-MM-DD")
    details.setdefault("replacement_placeholder", "<valid-before-date>")


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


def _is_repair_daily_log_cursor_context(details: dict | None) -> bool:
    command = details.get("command") if isinstance(details, dict) else None
    operation = details.get("operation") if isinstance(details, dict) else None
    reason_code = details.get("reason_code") if isinstance(details, dict) else None
    return (
        command == "repair-daily-log-cursor"
        or operation == "repair_daily_log_cursor"
        or reason_code in {
            "repair_apply_requires_preview_binding",
            "repair_preview_digest_mismatch",
        }
    )


def _failure_user_message(reason: str, *, language: str, error: str | None) -> str:
    if reason == "python_runtime_unavailable" and _python_runtime_stage(error) == "runtime_bootstrap":
        return _localized_text(
            language,
            en="RecallLoom cannot start because helper runtime bootstrap failed before execution could begin.",
            zh_cn="RecallLoom 当前无法启动，因为 helper 在真正执行前就遇到了 runtime bootstrap 失败。",
        )
    return failure_reason_contract(reason)["user_message"][language]


def _failure_operator_note(
    reason: str,
    *,
    language: str,
    error: str | None,
    details: dict | None,
) -> str | None:
    contract = failure_reason_contract(reason)
    if reason == "python_runtime_unavailable" and _python_runtime_stage(error) == "runtime_bootstrap":
        return _localized_text(
            language,
            en="Repair the RecallLoom bootstrap inputs such as package metadata, managed assets, or contract registry files before retrying.",
            zh_cn="请先修复 RecallLoom 的 bootstrap 输入，例如 package metadata、managed assets 或 contract registry 文件，再重试。",
        )
    reason_code = details.get("reason_code") if isinstance(details, dict) else None
    is_repair_cursor = _is_repair_daily_log_cursor_context(details)
    if reason == "invalid_prepared_input" and is_repair_cursor:
        if reason_code == "repair_apply_requires_preview_binding":
            return _localized_text(
                language,
                en=(
                    "Review a fresh repair preview and its confirmation material, then retry "
                    "apply only with --expected-workspace-revision or --preview-digest from that preview."
                ),
                zh_cn=(
                    "请复核 fresh repair preview 及其确认材料；只使用该 preview 里的 "
                    "--expected-workspace-revision 或 --preview-digest 重试 apply。"
                ),
            )
        return _localized_text(
            language,
            en="Fix the repair cursor arguments before retrying; this is not a prepared-entry content error.",
            zh_cn="请先修正 repair cursor 参数再重试；这不是 prepared-entry 内容错误。",
        )
    if reason == "stale_write_context" and is_repair_cursor:
        return _localized_text(
            language,
            en=(
                "Do not reuse the stale repair preview binding. Rerun repair preview and retry "
                "apply only with the new preview digest or expected workspace revision."
            ),
            zh_cn=(
                "不要复用已经过期的 repair preview binding。请重新运行 repair preview，"
                "并只用新的 preview digest 或 expected workspace revision 重试 apply。"
            ),
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
        command = details.get("command") if isinstance(details, dict) else None
        operation = details.get("operation") if isinstance(details, dict) else None
        reason_code = details.get("reason_code") if isinstance(details, dict) else None
        if (
            command == "repair-daily-log-cursor"
            or operation == "repair_daily_log_cursor"
            or reason_code == "repair_preview_digest_mismatch"
        ):
            return _localized_text(
                language,
                en=(
                    "Rerun repair-daily-log-cursor preview and use the new preview digest "
                    "or expected workspace revision before applying repair."
                ),
                zh_cn=(
                    "请重新运行 repair-daily-log-cursor preview，并使用新的 preview digest "
                    "或 expected workspace revision 后再执行 repair apply。"
                ),
            )
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
    if reason == "invalid_date" and _is_archive_before_date_invalid(details):
        invalid_value = _archive_before_invalid_value(details)
        if invalid_value is not None:
            return _localized_text(
                language,
                en=(
                    f"Replace the invalid --before value {invalid_value!r} with a YYYY-MM-DD "
                    "date, then rerun archive_logs.py with --before <valid-before-date>."
                ),
                zh_cn=(
                    f"请把无效的 --before 值 {invalid_value!r} 替换为 YYYY-MM-DD 日期，"
                    "再使用 archive_logs.py --before <valid-before-date> 重试。"
                ),
            )
        return _localized_text(
            language,
            en=(
                "Replace the invalid --before value with a YYYY-MM-DD date, then rerun "
                "archive_logs.py with --before <valid-before-date>."
            ),
            zh_cn=(
                "请把无效的 --before 值替换为 YYYY-MM-DD 日期，"
                "再使用 archive_logs.py --before <valid-before-date> 重试。"
            ),
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
            en=(
                "Run validate_context.py, then use stage_recovery_proposal.py, "
                "record_recovery_review.py, and prepare_recovery_promotion.py before writing again."
            ),
            zh_cn=(
                "请先运行 validate_context.py，然后使用 stage_recovery_proposal.py、"
                "record_recovery_review.py 和 prepare_recovery_promotion.py，再重新写入。"
            ),
        )
    if reason == "trust_review_required":
        return _localized_text(
            language,
            en=(
                "Use stage_recovery_proposal.py, record_recovery_review.py, "
                "and prepare_recovery_promotion.py, then rerun preflight_context_check.py."
            ),
            zh_cn=(
                "请使用 stage_recovery_proposal.py、record_recovery_review.py "
                "和 prepare_recovery_promotion.py，然后重新运行 preflight_context_check.py。"
            ),
        )
    if reason == "review_imported_baseline_confirmation_required":
        return _localized_text(
            language,
            en=(
                "Review the preflight readiness output. If the write is intentional, "
                "rerun with --confirm-review-imported-baseline."
            ),
            zh_cn=(
                "请复核 preflight readiness 输出；如果确认要写入，"
                "使用 --confirm-review-imported-baseline 重试。"
            ),
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
        return (
            "Run validate_context.py from the project root, then use "
            "stage_recovery_proposal.py, record_recovery_review.py, and "
            "prepare_recovery_promotion.py for reviewed recovery."
        )
    if reason == "attach_scan_blocked":
        return "Edit the prepared text to remove blocked content, then rerun the same helper command."
    if reason == "invalid_date":
        command = details.get("command") if details else None
        operation = details.get("operation") if details else None
        if (
            command == "archive"
            or operation == "daily_log_archive"
            or _is_archive_before_date_invalid(details)
        ):
            return _archive_before_recovery_command(details)
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
        command = details.get("command") if details else None
        operation = details.get("operation") if details else None
        reason_code = details.get("reason_code") if details else None
        if (
            command == "repair-daily-log-cursor"
            or operation == "repair_daily_log_cursor"
            or reason_code == "repair_preview_digest_mismatch"
        ):
            return f"recallloom.py repair-daily-log-cursor {project_arg} --json"
        if isinstance(project_root, str):
            return _script_command("preflight_context_check.py", project_arg, "--json")
        return "Rerun preflight_context_check.py from the project root, then retry with the fresh workspace revision."
    if reason == "write_lock_busy":
        if isinstance(project_root, str):
            return _script_command("unlock_write_lock.py", project_arg, "--json")
        return "Wait for the active writer to finish, then rerun the helper after the lock clears."
    if reason == "invalid_prepared_input":
        command = details.get("command") if details else None
        operation = details.get("operation") if details else None
        if command == "sync-current-state-after-append" or operation == "post_append_summary_sync":
            return (
                "Fix the reviewed rolling-summary JSON payload on stdin, then rerun "
                "recallloom.py sync-current-state-after-append <project-path> "
                "--stdin --input-format json --json."
            )
        if command == "validate":
            return (
                "Fix the validate flag combination, then rerun recallloom.py validate "
                "<project-path> --require-provenance --changed-only --json."
            )
        if command == "archive" or operation == "daily_log_archive":
            return (
                "Fix the archive arguments, then rerun archive_logs.py <project-path> "
                "--max-active <non-negative-count> --json."
            )
        if command == "write" or operation == "managed_file_commit":
            write_type = details.get("write_type") if details else None
            if not isinstance(write_type, str) or not write_type.strip():
                write_type = "current-state"
            return (
                f"Fix the prepared managed-file input, then rerun recallloom.py write "
                f"<project-path> --type {write_type} --source-file <prepared-file> "
                "--dry-run --json."
            )
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
            return _script_command("stage_recovery_proposal.py", project_arg, "--source-file", "<proposal.md>", "--json")
        return (
            "Use stage_recovery_proposal.py, record_recovery_review.py, and "
            "prepare_recovery_promotion.py, then rerun preflight_context_check.py."
        )
    if reason == "review_imported_baseline_confirmation_required":
        return (
            "Review preflight readiness, then rerun the same write command with "
            "--confirm-review-imported-baseline if the write is intentional."
        )
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


def _dispatcher_project_arg(details: dict | None) -> str:
    return "<project-path>"


_SAFE_ROUTING_COMMANDS = {
    "append",
    "archive",
    "bridge",
    "init",
    "quick-summary",
    "repair-daily-log-cursor",
    "resume",
    "status",
    "sync-current-state-after-append",
    "validate",
    "write",
}
_SAFE_ROUTING_OPERATIONS = {
    "daily_log_append",
    "daily_log_archive",
    "managed_file_commit",
    "package_support_gate",
    "post_append_summary_sync",
    "repair_daily_log_cursor",
}
_SAFE_ROUTING_INPUT_MODES = {
    "ambiguous",
    "file",
    "json-file",
    "json-stdin",
    "json-string",
    "missing",
    "stdin",
}
_SAFE_ROUTING_INPUT_FORMATS = {"auto", "json", "markdown"}
_SAFE_ROUTING_FILE_KEYS = {"context_brief", "daily_log", "rolling_summary", "update_protocol"}
_SAFE_ROUTING_WRITE_TYPES = {"current-state", "protocol-rules", "stable-context"}
_SAFE_ROUTING_PREPARED_BUILDERS = {"rolling_summary_json"}
_SAFE_ROUTING_SIDE_EFFECTS = {
    "none",
    "partial",
    "write_attempted",
    "unknown",
    "external_state_modification_preserved",
    "external_target_modification_preserved",
    "provenance_validation_failed",
    "review_evidence_recorded_state_unchanged",
    "target_and_state_written_receipt_store_verified_unchanged",
    "target_and_state_written_receipt_not_stored",
    "target_restored_external_state_preserved",
    "target_state_and_receipt_store_write_unknown_review_required",
    "target_state_and_receipt_store_written_review_required",
}
_SAFE_ROUTING_REASON_CODES = {
    "all_sections_empty",
    "archive_before_date_invalid",
    "archive_input_invalid",
    "archive_max_active_invalid",
    "both_input_sources",
    "empty_section_list",
    "empty_section_list_item",
    "empty_section_string",
    "invalid_section_list_item_type",
    "invalid_section_value_type",
    "malformed_json",
    "missing_input_source",
    "missing_section_key",
    "missing_write_type",
    "provenance_scope_required",
    "provenance_scope_without_requirement",
    "concurrent_external_modification_detected",
    "state_write_failed_target_preserved",
    "post_hash_inconsistent_review_binding_changed",
    "post_hash_inconsistent_review_eligible",
    "post_hash_inconsistent_review_promotion_not_committed",
    "review_imported_baseline_material_invalid",
    "receipt_failure_provenance_restore_failed",
    "receipt_store_contract_invalid",
    "receipt_store_concurrent_change_detected",
    "receipt_store_duplicate_digest",
    "receipt_store_index_mismatch",
    "receipt_store_missing",
    "receipt_store_not_written_verified",
    "receipt_store_post_write_unreadable",
    "receipt_store_revision_invalid",
    "receipt_store_schema_version_mismatch",
    "receipt_store_snapshot_invalid",
    "receipt_store_snapshot_mismatch",
    "receipt_store_type_mismatch",
    "receipt_store_unreadable",
    "receipt_store_write_failed",
    "repair_apply_requires_preview_binding",
    "repair_preview_digest_mismatch",
    "reserved_marker_injection",
    "source_selection_invalid",
    "source_file_not_supported",
    "stdin_required",
    "top_level_not_object",
    "unknown_section_key",
    "unsupported_write_type",
    "write_argument_invalid",
    "json_input_requires_current_state",
    "invalid_support_advisory",
    *_INVALID_LOCAL_SUPPORT_CACHE_REASON_CODES,
}


def _safe_enum_value(value: object, allowed: set[str]) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped in allowed else None


def _safe_command_value(value: object) -> str | None:
    direct = _safe_enum_value(value, _SAFE_ROUTING_COMMANDS)
    if direct is not None:
        return direct
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    for token in tokens:
        candidate = Path(token).name.strip()
        if candidate in _SAFE_ROUTING_COMMANDS:
            return candidate
    return None


def _failure_routing_details(details: dict | None) -> dict | None:
    if not isinstance(details, dict):
        return None
    routed: dict[str, str] = {}
    command = _safe_command_value(details.get("command"))
    if command is not None:
        routed["command"] = command
    safe_fields = {
        "operation": _SAFE_ROUTING_OPERATIONS,
        "input_mode": _SAFE_ROUTING_INPUT_MODES,
        "input_format": _SAFE_ROUTING_INPUT_FORMATS,
        "file_key": _SAFE_ROUTING_FILE_KEYS,
        "write_type": _SAFE_ROUTING_WRITE_TYPES,
        "prepared_input_builder": _SAFE_ROUTING_PREPARED_BUILDERS,
        "side_effect": _SAFE_ROUTING_SIDE_EFFECTS,
        "reason_code": _SAFE_ROUTING_REASON_CODES,
        "section_key": ROLLING_SUMMARY_JSON_SECTION_KEYS,
    }
    for key, allowed in safe_fields.items():
        value = _safe_enum_value(details.get(key), allowed)
        if value is not None:
            routed[key] = value
    return routed or None


def _single_next_for_invalid_prepared_input(details: dict | None) -> str:
    project_arg = _dispatcher_project_arg(details)
    command = details.get("command") if isinstance(details, dict) else None
    operation = details.get("operation") if isinstance(details, dict) else None
    reason_code = details.get("reason_code") if isinstance(details, dict) else None
    if command == "validate":
        if reason_code in {"provenance_scope_required", "provenance_scope_without_requirement"}:
            return f"recallloom.py validate {project_arg} --require-provenance --changed-only --json"
        return f"recallloom.py validate {project_arg} --json"
    if command == "repair-daily-log-cursor" or operation == "repair_daily_log_cursor":
        return f"recallloom.py repair-daily-log-cursor {project_arg} --json"
    if command == "sync-current-state-after-append" or operation == "post_append_summary_sync":
        return (
            f"recallloom.py sync-current-state-after-append {project_arg} "
            "--stdin --input-format json --json"
        )
    if command == "archive" or operation == "daily_log_archive":
        return f"archive_logs.py {project_arg} --max-active <non-negative-count> --json"
    input_mode = _prepared_input_mode(details)
    if _is_rolling_summary_json_builder(details):
        if input_mode == "json-file":
            return (
                f"recallloom.py write {project_arg} --type current-state "
                "--source-file <rolling-summary.json> --input-format json --dry-run --json"
            )
        return (
            f"recallloom.py write {project_arg} --type current-state "
            "--stdin --input-format json --dry-run --json"
        )
    managed_markdown_route = _managed_markdown_write_route(details)
    if managed_markdown_route is not None:
        write_type = managed_markdown_route["write_type"]
        source_placeholder = managed_markdown_route["source_placeholder"]
        if input_mode == "stdin":
            return (
                f"recallloom.py write {project_arg} --type {write_type} "
                "--stdin --dry-run --json"
            )
        return (
            f"recallloom.py write {project_arg} --type {write_type} "
            f"--source-file {source_placeholder} --dry-run --json"
        )
    command = details.get("command") if isinstance(details, dict) else None
    if command == "write":
        write_type = details.get("write_type") if isinstance(details, dict) else None
        if not isinstance(write_type, str) or not write_type.strip():
            write_type = "current-state"
        return (
            f"recallloom.py write {project_arg} --type {write_type} "
            "--source-file <prepared-file> --dry-run --json"
        )
    if command == "append":
        return (
            f"recallloom.py append {project_arg} "
            + " ".join(_append_placeholder_args(details))
            + " --json"
        )
    if input_mode == "json-string":
        return f"recallloom.py append {project_arg} --entry-json '<prepared-entry-json>' --json"
    if input_mode == "json-stdin":
        return f"recallloom.py append {project_arg} --stdin --input-format json --json"
    if input_mode == "json-file":
        return (
            f"recallloom.py append {project_arg} --entry-file <entry.json> "
            "--input-format json --json"
        )
    if input_mode == "stdin":
        return f"recallloom.py append {project_arg} --stdin --json"
    if input_mode is None:
        return f"recallloom.py status {project_arg} --json"
    return f"recallloom.py append {project_arg} --entry-file <prepared-entry.md> --json"


def _single_next_for_confirmation_required(details: dict | None) -> str:
    project_arg = _dispatcher_project_arg(details)
    command = details.get("command") if isinstance(details, dict) else None
    if command == "append":
        return (
            f"recallloom.py append {project_arg} "
            + " ".join(_append_placeholder_args(details))
            + " "
            "--confirm-review-imported-baseline --json"
        )
    if command == "sync-current-state-after-append":
        return (
            f"recallloom.py sync-current-state-after-append {project_arg} "
            "--stdin --input-format json --confirm-review-imported-baseline --json"
        )
    return (
        f"recallloom.py write {project_arg} --type current-state "
        "--source-file <prepared-file> --confirm-review-imported-baseline --json"
    )


def _failure_single_next_command(
    reason: str,
    *,
    details: dict | None,
) -> str:
    project_arg = _dispatcher_project_arg(details)
    command = details.get("command") if isinstance(details, dict) else None
    operation = details.get("operation") if isinstance(details, dict) else None
    if reason in {"not_project_root", "no_project_root", "invalid_storage_boundary"}:
        return f"recallloom.py init {project_arg} --json"
    if reason in {"damaged_sidecar", "dual_sidecar_conflict", "malformed_managed_file"}:
        return f"recallloom.py validate {project_arg} --json"
    if reason == "invalid_date":
        if (
            command == "archive"
            or operation == "daily_log_archive"
            or _is_archive_before_date_invalid(details)
        ):
            return _archive_before_single_next_command(details)
        return f"recallloom.py status {project_arg} --json"
    if reason == "invalid_prepared_input":
        return _single_next_for_invalid_prepared_input(details)
    if reason == "stale_write_context":
        if command == "repair-daily-log-cursor" or operation == "repair_daily_log_cursor":
            return f"recallloom.py repair-daily-log-cursor {project_arg} --json"
        return f"recallloom.py status {project_arg} --json"
    if reason == "trust_review_required":
        return f"recallloom.py validate {project_arg} --json"
    if reason == "review_imported_baseline_confirmation_required":
        return _single_next_for_confirmation_required(details)
    if reason == "continuity_drift_review_required":
        return f"recallloom.py status {project_arg} --json"
    if reason == "package_support_blocked":
        return f"recallloom.py status {project_arg} --json"
    if reason == "write_lock_busy":
        return f"recallloom.py status {project_arg} --json"
    if reason == "python_runtime_unavailable":
        return f"recallloom.py status {project_arg} --json"
    return f"recallloom.py status {project_arg} --json"


def _explicit_side_effect(payload: dict, details: dict | None) -> str | None:
    side_effect = payload.get("side_effect")
    if isinstance(side_effect, str) and side_effect.strip():
        return side_effect
    if isinstance(details, dict):
        side_effect = details.get("side_effect")
        if isinstance(side_effect, str) and side_effect.strip():
            return side_effect
    return None


def _apply_additive_failure_fields(
    payload: dict,
    *,
    reason: str,
    language: str,
    details: dict | None,
) -> None:
    side_effect = _explicit_side_effect(payload, details)
    if side_effect is not None:
        payload.setdefault("side_effect", side_effect)
    payload["single_next_command"] = _failure_single_next_command(reason, details=details)
    payload["safe_to_retry"] = (
        side_effect == "none"
        and payload.get("trust_effect") == "none"
    )
    if reason == "invalid_date" and _is_archive_before_date_invalid(details):
        payload["next_actions"] = ["replace_invalid_before_date", "retry_archive"]
    if reason == "invalid_prepared_input" and _is_repair_daily_log_cursor_context(details):
        reason_code = details.get("reason_code") if isinstance(details, dict) else None
        if reason_code == "repair_apply_requires_preview_binding":
            payload["next_actions"] = [
                "rerun_repair_preview",
                "retry_with_fresh_preview_binding",
            ]
        else:
            payload["next_actions"] = [
                "fix_repair_cursor_arguments",
                "rerun_repair_preview",
            ]
    if reason == "stale_write_context" and _is_repair_daily_log_cursor_context(details):
        payload["next_actions"] = [
            "rerun_repair_preview",
            "retry_with_fresh_preview_binding",
        ]
    route = _canonical_failure_route(
        reason,
        language=language,
        details=details,
    )
    if route is None:
        return
    for key in (
        "blocked",
        "recoverability",
        "trust_effect",
        "next_actions",
        "user_message",
        "suggestion",
        "recovery_command",
        "operator_note",
        "single_next_command",
        "safe_to_retry",
    ):
        if key in route:
            value = route[key]
            payload[key] = list(value) if isinstance(value, list) else value
    if isinstance(route.get("side_effect"), str):
        payload["side_effect"] = route["side_effect"]
    public_details = payload.get("details")
    if isinstance(public_details, dict) and isinstance(route.get("next_action"), str):
        public_details["next_action"] = route["next_action"]
        if isinstance(route.get("side_effect"), str):
            public_details["side_effect"] = route["side_effect"]


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
    canonical_details = _canonicalize_failure_details(
        normalized_reason,
        language=language,
        details=details,
    )
    normalized_details = _public_failure_details(canonical_details)
    _augment_archive_before_details(normalized_reason, normalized_details)
    routing_details = _failure_routing_details(canonical_details)
    suggestion_details = dict(normalized_details or {})
    if routing_details:
        suggestion_details.update(routing_details)
    suggestion_details = suggestion_details or None
    normalized_error = _public_failure_error(error, canonical_details)
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
            details=suggestion_details,
        ),
        "recovery_command": _failure_recovery_command(
            normalized_reason,
            script_name=normalized_script_name,
            error=error,
            details=suggestion_details,
        ),
    }
    if normalized_error is not None:
        payload["error"] = normalized_error
    operator_note = _failure_operator_note(
        normalized_reason,
        language=language,
        error=error,
        details=suggestion_details,
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
            details=suggestion_details,
        )
    if not payload.get("recovery_command"):
        payload["recovery_command"] = _failure_recovery_command(
            normalized_reason,
            script_name=normalized_script_name,
            error=error,
            details=suggestion_details,
        )
    _apply_additive_failure_fields(
        payload,
        reason=normalized_reason,
        language=language,
        details=routing_details,
    )
    publicized_payload = publicize_json_value(
        payload,
        project_root=(
            _infer_project_root(canonical_details)
            or (canonical_details or {}).get("project_root")
        ),
        private=private_json_paths_enabled(),
    )
    if isinstance(publicized_payload, dict):
        public_details = publicized_payload.get("details")
        safe_command = _safe_command_value((canonical_details or {}).get("command"))
        if isinstance(public_details, dict) and safe_command is not None:
            public_details["command"] = safe_command
    return publicized_payload if isinstance(publicized_payload, dict) else payload
