#!/usr/bin/env python3
"""Bounded quick-summary helpers for RecallLoom continuity snapshots."""

from __future__ import annotations

import re
from pathlib import Path

from core.continuity.freshness import (
    continuity_state_for_workspace,
    evaluate_continuity_freshness,
    freshness_risk_summary,
    is_effectively_empty_summary_next_step,
    summary_matches_empty_shell_template,
)
from core.protocol.sections import extract_section_text


PROJECT_VERSION_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*)\s+v?\d+\.\d+(?:\.\d+)?\b")
BACKTICK_PATH_RE = re.compile(r"`([^`]+)`")
PHASE_LABEL_RE = re.compile(
    r"(?i)(?:^|\b)(?:current phase|phase|当前阶段|阶段)\s*[:：-]\s*(?P<value>.+)$"
)
WHITESPACE_RE = re.compile(r"\s+")

PLACEHOLDER_NEXT_STEP_LINES = {
    "describe the handoff-first next move:",
    "active task",
    "owner or role when known",
    "immediate next action",
    "这里写 handoff-first 的下一步：",
    "current task",
    "known owner or role",
    "current owner or role",
    "当前任务",
    "已知负责人或角色",
    "立刻要做的动作",
}

PHASE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("planning", ("planning", "plan", "design", "proposal", "spec", "规划", "设计", "方案")),
    ("development", ("development", "implementation", "implement", "build", "开发", "实现", "编码")),
    ("testing", ("testing", "validation", "verify", "qa", "测试", "验证", "复核")),
    ("release", ("release", "deploy", "ship", "发布", "上线", "交付")),
    ("maintenance", ("maintenance", "fix", "optimiz", "stabil", "维护", "修复", "优化", "稳定")),
)


def _meaningful_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped == "-":
            continue
        if stripped.startswith("<!--") or stripped.startswith("#"):
            continue
        stripped = stripped.lstrip("-* ").strip()
        if not stripped or stripped == "-":
            continue
        lines.append(stripped)
    return lines


def _compact(text: str | None, *, max_chars: int) -> str | None:
    if text is None:
        return None
    compacted = WHITESPACE_RE.sub(" ", text).strip()
    if not compacted:
        return None
    if len(compacted) <= max_chars:
        return compacted
    shortened = compacted[: max_chars - 3].rstrip(" ,;:|/-")
    return shortened + "..."


def _extract_project_label(current_state_text: str, project_root: Path) -> str:
    for line in _meaningful_lines(current_state_text)[:6]:
        version_match = PROJECT_VERSION_RE.search(line)
        if version_match:
            return _compact(version_match.group(1), max_chars=48) or project_root.name
        path_match = BACKTICK_PATH_RE.search(line)
        if path_match:
            candidate = Path(path_match.group(1)).name
            if candidate:
                return _compact(candidate, max_chars=48) or project_root.name
    return project_root.name


def _extract_phase(current_state_text: str, active_judgments_text: str) -> str:
    lines = _meaningful_lines(current_state_text)[:6] + _meaningful_lines(active_judgments_text)[:4]

    for line in lines:
        explicit = PHASE_LABEL_RE.search(line)
        if explicit:
            return _compact(explicit.group("value"), max_chars=72) or "active"

    for line in lines:
        lowered = line.casefold()
        if PROJECT_VERSION_RE.search(line):
            for _, keywords in PHASE_KEYWORDS:
                if any(keyword in lowered for keyword in keywords):
                    return _compact(line, max_chars=72) or "active"

    for phase_label, keywords in PHASE_KEYWORDS:
        for line in lines:
            lowered = line.casefold()
            if any(keyword in lowered for keyword in keywords):
                return phase_label

    return "active"


def _extract_next_step(next_step_text: str) -> str | None:
    if is_effectively_empty_summary_next_step(next_step_text):
        return None
    lines = [
        line
        for line in _meaningful_lines(next_step_text)
        if line.casefold() not in PLACEHOLDER_NEXT_STEP_LINES
    ]
    if not lines:
        return None
    return _compact(" | ".join(lines[:2]), max_chars=160)


def quick_summary_next_actions(
    *,
    no_project: bool = False,
    empty_shell: bool = False,
    summary_stale: bool = False,
) -> list[str]:
    if no_project:
        return ["rl-init", "choose_project_root"]
    actions = ["seed_initial_continuity"] if empty_shell else ["read_rolling_summary"]
    if summary_stale:
        actions.insert(1, "refresh_or_review_summary_before_write")
    actions.append("review_update_protocol_before_write")
    return actions


def build_quick_summary_payload(
    *,
    project_root: Path,
    storage_root: Path,
    summary_path: Path,
    summary_text: str,
    summary_revision: int,
    summary_base_workspace_revision: int,
    state: dict,
) -> dict:
    daily_logs_state = state.get("daily_logs")
    latest_daily_log_exists = isinstance(daily_logs_state, dict) and daily_logs_state.get("latest_file") is not None
    continuity_state, continuity_seeded = continuity_state_for_workspace(
        state=state,
        summary_text=summary_text,
        latest_daily_log_exists=latest_daily_log_exists,
    )
    freshness = evaluate_continuity_freshness(
        project_root=project_root,
        storage_root=storage_root,
        summary_path=summary_path,
        workspace_revision=state["workspace_revision"],
        summary_base_workspace_revision=summary_base_workspace_revision,
        latest_daily_log_exists=latest_daily_log_exists,
        scan_mode="quick",
    )
    freshness_risk = freshness_risk_summary(
        workspace_artifact_scan_mode=freshness["workspace_artifact_scan_mode"],
        workspace_artifact_scan_performed=freshness["workspace_artifact_scan_performed"],
        workspace_artifact_newer_than_summary=freshness["workspace_artifact_newer_than_summary"],
        summary_revision_stale=freshness["summary_revision_stale"],
        continuity_confidence=freshness["continuity_confidence"],
    )

    current_state_text = extract_section_text(summary_text, "current_state")
    active_judgments_text = extract_section_text(summary_text, "active_judgments")
    next_step_text = extract_section_text(summary_text, "next_step")
    empty_shell = continuity_state == "initialized_empty_shell"
    summary_looks_empty = summary_matches_empty_shell_template(summary_text)

    return {
        "schema_version": "1.1",
        "ok": True,
        "command": "quick-summary",
        "project_root": project_root.name or project_root.as_posix(),
        "storage_root": storage_root.relative_to(project_root).as_posix(),
        "summary": {
            "project": _extract_project_label(current_state_text, project_root),
            "phase": "unseeded" if empty_shell else _extract_phase(current_state_text, active_judgments_text),
            "next_step": None if empty_shell else _extract_next_step(next_step_text),
            "confidence": freshness["continuity_confidence"],
        },
        "freshness": {
            "workspace_revision": state["workspace_revision"],
            "rolling_summary_revision": summary_revision,
            "summary_stale": freshness["summary_stale"],
            "workspace_newer_than_summary": freshness["workspace_newer_than_summary"],
            "workspace_artifact_newer_than_summary": freshness["workspace_artifact_newer_than_summary"],
            "freshness_risk_level": freshness_risk["level"],
        },
        "continuity_state": continuity_state,
        "continuity_seeded": continuity_seeded,
        "summary_template_detected": summary_looks_empty,
        "next_actions": quick_summary_next_actions(
            empty_shell=empty_shell,
            summary_stale=freshness["summary_stale"],
        ),
    }


def build_no_project_payload(start_path: Path) -> dict:
    return {
        "schema_version": "1.1",
        "ok": True,
        "command": "quick-summary",
        "project_root": start_path.name or start_path.as_posix(),
        "storage_root": None,
        "summary": {
            "project": None,
            "phase": "no_project",
            "next_step": None,
            "confidence": "none",
        },
        "freshness": {
            "workspace_revision": None,
            "rolling_summary_revision": None,
            "summary_stale": False,
            "freshness_risk_level": "not_applicable",
        },
        "continuity_state": "no_project",
        "continuity_seeded": False,
        "summary_template_detected": False,
        "next_actions": quick_summary_next_actions(no_project=True),
    }
