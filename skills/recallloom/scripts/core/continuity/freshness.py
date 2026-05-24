#!/usr/bin/env python3
"""Freshness and digest helpers for RecallLoom continuity reads."""

from __future__ import annotations

from pathlib import Path

from core.output.privacy import redact_public_text
from core.protocol.sections import extract_section_text

DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_FILES = {
    ".DS_Store",
    ".recallloom.write.lock",
}

ROLLING_SUMMARY_EMPTY_SHELL_TEMPLATES = (
    {
        "current_state": (
            "Write the validated handoff-first state here:",
            "Active state",
            "Relevant files",
            "Critical context",
        ),
        "active_judgments": (
            "Record the coordination judgments that matter right now:",
            "Key decisions",
            "Active assumptions",
            "Tradeoffs in force",
        ),
        "risks_open_questions": (
            "Make blocker visibility explicit:",
            "Blocked items",
            "Open questions",
            "External dependencies",
        ),
        "next_step": (
            "Describe the handoff-first next move:",
            "Active task",
            "Owner or role when known",
            "Immediate next action",
        ),
    },
    {
        "current_state": (
            "这里优先写 handoff-first 的已确认当前状态：",
            "当前活跃状态",
            "相关文件",
            "关键上下文",
        ),
        "active_judgments": (
            "这里记录当前真正影响推进的判断：",
            "关键决策",
            "当前假设",
            "仍在生效的取舍",
        ),
        "risks_open_questions": (
            "把阻塞与未决问题写清楚：",
            "当前阻塞",
            "未决问题",
            "外部依赖",
        ),
        "next_step": (
            "这里写 handoff-first 的下一步：",
            "当前任务",
            "已知负责人或角色",
            "立刻要做的动作",
        ),
    },
)

EMPTY_NEXT_STEP_MARKERS = {
    "none",
    "n/a",
    "na",
    "no next step",
    "no next action",
    "无",
    "暂无",
    "无下一步",
    "暂无下一步",
    "无后续动作",
}


def latest_file(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    return max(paths, key=lambda path: path.stat().st_mtime)


def normalized_section_lines(section_text: str) -> list[str]:
    lines: list[str] = []
    for raw in section_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            continue
        if stripped.startswith("#"):
            continue
        stripped = stripped.lstrip("-* ").strip()
        if not stripped:
            continue
        lines.append(stripped.casefold())
    return lines


def summary_matches_empty_shell_template(summary_text: str) -> bool:
    for template in ROLLING_SUMMARY_EMPTY_SHELL_TEMPLATES:
        if all(
            normalized_section_lines(extract_section_text(summary_text, section_key))
            == [line.casefold() for line in expected_lines]
            for section_key, expected_lines in template.items()
        ):
            return True
    return False


def continuity_state_for_workspace(
    *,
    state: dict,
    summary_text: str,
    latest_daily_log_exists: bool,
) -> tuple[str, bool]:
    daily_logs_state = state.get("daily_logs")
    entry_count = daily_logs_state.get("entry_count") if isinstance(daily_logs_state, dict) else None

    empty_shell = (
        not latest_daily_log_exists
        and isinstance(entry_count, int)
        and entry_count == 0
        and summary_matches_empty_shell_template(summary_text)
    )
    continuity_state = "initialized_empty_shell" if empty_shell else "initialized_seeded"
    return continuity_state, (not empty_shell)


def is_effectively_empty_summary_next_step(text: str) -> bool:
    cleaned = normalized_section_lines(text)
    if not cleaned:
        return True
    lowered = " ".join(cleaned).strip().strip(" .。!！")
    return lowered in EMPTY_NEXT_STEP_MARKERS


def continuity_confidence_level(
    *,
    workspace_valid: bool,
    summary_revision_is_stale: bool,
    workspace_artifact_is_newer: bool | None,
    latest_daily_log_exists: bool,
    workspace_artifact_scan_mode: str,
) -> str:
    if not workspace_valid:
        return "broken"
    artifact_newer = bool(workspace_artifact_is_newer)
    if (summary_revision_is_stale and not latest_daily_log_exists) or (
        artifact_newer and not latest_daily_log_exists
    ):
        return "low"
    if summary_revision_is_stale or artifact_newer or not latest_daily_log_exists:
        return "medium"
    if workspace_artifact_scan_mode != "full":
        return "medium"
    return "high"


def freshness_risk_summary(
    *,
    workspace_artifact_scan_mode: str,
    workspace_artifact_scan_performed: bool,
    workspace_artifact_newer_than_summary: bool | None,
    summary_revision_stale: bool,
    continuity_confidence: str,
) -> dict:
    if continuity_confidence == "broken":
        return {
            "level": "high",
            "note": "Continuity state is broken. Validate the workspace before trusting recall or writing.",
        }
    if workspace_artifact_newer_than_summary:
        return {
            "level": "high",
            "note": "Workspace artifacts are newer than rolling_summary.md. Review current workspace state before trusting recall or writing.",
        }
    if summary_revision_stale:
        return {
            "level": "medium",
            "note": "workspace_revision is ahead of the rolling summary base revision. Review rolling_summary.md before trusting recall or writing.",
        }
    if not workspace_artifact_scan_performed or workspace_artifact_scan_mode != "full":
        return {
            "level": "medium",
            "note": "Only the quick freshness path was used. Run a full scan before high-confidence writes.",
        }
    return {
        "level": "low",
        "note": None,
    }


def read_freshness_semantics(
    *,
    workspace_artifact_scan_mode: str,
    workspace_artifact_scan_performed: bool,
    workspace_artifact_newer_than_summary: bool | None,
    summary_revision_stale: bool,
    workspace_newer_than_summary: bool,
    continuity_confidence: str,
) -> dict:
    freshness_risk = freshness_risk_summary(
        workspace_artifact_scan_mode=workspace_artifact_scan_mode,
        workspace_artifact_scan_performed=workspace_artifact_scan_performed,
        workspace_artifact_newer_than_summary=workspace_artifact_newer_than_summary,
        summary_revision_stale=summary_revision_stale,
        continuity_confidence=continuity_confidence,
    )
    if continuity_confidence == "high" and freshness_risk["level"] == "low":
        read_confidence = "current"
    elif workspace_artifact_newer_than_summary:
        read_confidence = "workspace_newer_than_summary"
    elif summary_revision_stale or workspace_newer_than_summary:
        read_confidence = "stale_summary_review_recommended"
    elif continuity_confidence == "broken":
        read_confidence = "broken"
    else:
        read_confidence = "bounded"

    return {
        "legacy_path": "protocol_1_0_sidecar_compatible",
        "legacy_path_compatible": True,
        "stale_summary": summary_revision_stale,
        "workspace_newer_than_summary": workspace_newer_than_summary,
        "read_confidence": read_confidence,
        "freshness_risk_level": freshness_risk["level"],
        "freshness_risk_note": freshness_risk["note"],
    }


def digest_excerpt(
    text: str,
    *,
    max_lines: int = 4,
    project_root: str | Path | None = None,
) -> str | None:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            continue
        stripped = stripped.lstrip("-* ").strip()
        if not stripped:
            continue
        stripped = redact_public_text(stripped, project_root=project_root) or "redacted"
        lines.append(stripped)
        if len(lines) >= max_lines:
            break
    if not lines:
        return None
    return " | ".join(lines)


def _digest_excerpt(
    text: str,
    *,
    max_lines: int = 4,
    project_root: str | Path | None = None,
) -> str | None:
    return digest_excerpt(text, max_lines=max_lines, project_root=project_root)


def continuity_digest_bundle(
    *,
    summary_text: str,
    latest_daily_log_text: str | None = None,
    project_root: str | Path | None = None,
) -> dict:
    summary_is_empty_shell_template = summary_matches_empty_shell_template(summary_text)
    if summary_is_empty_shell_template:
        active_task_digest = None
        blocked_digest = None
    else:
        next_step_text = extract_section_text(summary_text, "next_step")
        risks_text = extract_section_text(summary_text, "risks_open_questions")
        active_task_digest = (
            None
            if is_effectively_empty_summary_next_step(next_step_text)
            else digest_excerpt(next_step_text, project_root=project_root)
        )
        blocked_digest = digest_excerpt(risks_text, project_root=project_root)

    latest_relevant_log_digest = None
    if latest_daily_log_text:
        log_sections = [
            extract_section_text(latest_daily_log_text, "work_completed"),
            extract_section_text(latest_daily_log_text, "key_decisions"),
            extract_section_text(latest_daily_log_text, "recommended_next_step"),
        ]
        latest_relevant_log_digest = digest_excerpt(
            "\n".join(section for section in log_sections if section.strip()),
            project_root=project_root,
        )

    suggested_handoff_sections: list[str] = []
    if active_task_digest:
        suggested_handoff_sections.append("next_step")
    if blocked_digest:
        suggested_handoff_sections.append("risks_open_questions")
    if latest_relevant_log_digest:
        suggested_handoff_sections.append("latest_daily_log")

    return {
        "active_task_digest": active_task_digest,
        "blocked_digest": blocked_digest,
        "latest_relevant_log_digest": latest_relevant_log_digest,
        "suggested_handoff_sections": suggested_handoff_sections,
    }


def iter_workspace_artifacts(
    project_root: Path,
    storage_root: Path,
    *,
    excluded_dirs: set[str] | None = None,
    excluded_files: set[str] | None = None,
) -> list[Path]:
    excluded_dirs = excluded_dirs or DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_DIRS
    excluded_files = excluded_files or DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_FILES
    artifacts: list[Path] = []
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            path.relative_to(storage_root)
            continue
        except ValueError:
            pass
        if path.name in excluded_files:
            continue
        rel_path = path.relative_to(project_root)
        if any(part in excluded_dirs for part in rel_path.parent.parts):
            continue
        artifacts.append(path)
    return artifacts


def evaluate_continuity_freshness(
    *,
    project_root: Path,
    storage_root: Path,
    summary_path: Path,
    workspace_revision: int,
    summary_base_workspace_revision: int,
    latest_daily_log_exists: bool,
    scan_mode: str = "quick",
) -> dict:
    if scan_mode not in {"quick", "full"}:
        raise ValueError(f"Unsupported scan_mode: {scan_mode}")

    workspace_artifact_scan_performed = scan_mode == "full"
    latest_workspace_artifact = None
    workspace_artifact_is_newer = None
    summary_mtime = summary_path.stat().st_mtime

    if workspace_artifact_scan_performed:
        latest_workspace_artifact = latest_file(
            iter_workspace_artifacts(project_root, storage_root)
        )
        workspace_artifact_is_newer = (
            latest_workspace_artifact is not None
            and latest_workspace_artifact.stat().st_mtime > summary_mtime
        )

    summary_revision_is_stale = workspace_revision > summary_base_workspace_revision
    workspace_is_newer = (
        (workspace_artifact_is_newer if workspace_artifact_is_newer is not None else False)
        or summary_revision_is_stale
    )
    continuity_confidence = continuity_confidence_level(
        workspace_valid=True,
        summary_revision_is_stale=summary_revision_is_stale,
        workspace_artifact_is_newer=workspace_artifact_is_newer,
        latest_daily_log_exists=latest_daily_log_exists,
        workspace_artifact_scan_mode=scan_mode,
    )

    return {
        "workspace_artifact_scan_mode": scan_mode,
        "workspace_artifact_scan_performed": workspace_artifact_scan_performed,
        "latest_workspace_artifact": latest_workspace_artifact,
        "workspace_artifact_newer_than_summary": workspace_artifact_is_newer,
        "summary_revision_stale": summary_revision_is_stale,
        "workspace_newer_than_summary": workspace_is_newer,
        "summary_stale": workspace_is_newer,
        "continuity_confidence": continuity_confidence,
        "read_freshness": read_freshness_semantics(
            workspace_artifact_scan_mode=scan_mode,
            workspace_artifact_scan_performed=workspace_artifact_scan_performed,
            workspace_artifact_newer_than_summary=workspace_artifact_is_newer,
            summary_revision_stale=summary_revision_is_stale,
            workspace_newer_than_summary=workspace_is_newer,
            continuity_confidence=continuity_confidence,
        ),
    }
