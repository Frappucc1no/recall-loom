#!/usr/bin/env python3
"""Bridge block rendering and integrity helpers."""

from __future__ import annotations

import os
from pathlib import Path
import re

from core.protocol.contracts import (
    BRIDGE_END,
    BRIDGE_START,
    DAILY_LOGS_DIRNAME,
    EXCLUDE_BLOCK_END,
    EXCLUDE_BLOCK_START,
    FILE_KEYS,
    ROOT_ENTRY_CANDIDATES,
)


def bridge_block_integrity(text: str) -> tuple[bool, str | None]:
    start_count = text.count(BRIDGE_START)
    end_count = text.count(BRIDGE_END)
    if start_count == 0 and end_count == 0:
        return True, None
    if start_count != end_count:
        return False, "bridge_start_end_mismatch"
    if start_count > 1:
        return False, "bridge_duplicate_blocks"
    if text.find(BRIDGE_START) > text.find(BRIDGE_END):
        return False, "bridge_order_invalid"
    return True, None


def _exact_marker_line_indices(text: str, marker: str) -> list[int]:
    return [idx for idx, line in enumerate(text.splitlines()) if line == marker]


def exclude_block_integrity(text: str) -> tuple[bool, str | None]:
    start_indices = _exact_marker_line_indices(text, EXCLUDE_BLOCK_START)
    end_indices = _exact_marker_line_indices(text, EXCLUDE_BLOCK_END)
    start_count = len(start_indices)
    end_count = len(end_indices)
    if text.count(EXCLUDE_BLOCK_START) != start_count or text.count(EXCLUDE_BLOCK_END) != end_count:
        return False, "exclude_marker_line_invalid"
    if start_count == 0 and end_count == 0:
        return True, None
    if start_count != end_count:
        return False, "exclude_start_end_mismatch"
    if start_count > 1:
        return False, "exclude_duplicate_blocks"
    if start_indices[0] > end_indices[0]:
        return False, "exclude_order_invalid"
    return True, None


def managed_exclude_block_text(text: str) -> str | None:
    lines = text.splitlines()
    start_indices = [idx for idx, line in enumerate(lines) if line == EXCLUDE_BLOCK_START]
    end_indices = [idx for idx, line in enumerate(lines) if line == EXCLUDE_BLOCK_END]
    if not start_indices and not end_indices:
        return None
    if len(start_indices) != 1 or len(end_indices) != 1 or start_indices[0] > end_indices[0]:
        return None
    return "\n".join(lines[start_indices[0] : end_indices[0] + 1])


def _to_posix_relative(from_dir: Path, to_path: Path) -> str:
    return Path(os.path.relpath(to_path, start=from_dir)).as_posix()


def detect_root_entry_files(project_root: Path) -> list[Path]:
    found: list[Path] = []
    for rel_path in ROOT_ENTRY_CANDIDATES:
        candidate = project_root / rel_path
        if candidate.is_file():
            found.append(candidate)
    return found


def render_bridge_block(workspace, target_file: Path) -> str:
    target_dir = target_file.parent
    language = workspace.workspace_language
    config = _to_posix_relative(target_dir, workspace.storage_root / FILE_KEYS["config"])
    update_protocol = _to_posix_relative(target_dir, workspace.storage_root / FILE_KEYS["update_protocol"])
    context_brief = _to_posix_relative(target_dir, workspace.storage_root / FILE_KEYS["context_brief"])
    rolling_summary = _to_posix_relative(target_dir, workspace.storage_root / FILE_KEYS["rolling_summary"])
    daily_logs_dir = _to_posix_relative(target_dir, workspace.storage_root / DAILY_LOGS_DIRNAME)
    has_update_protocol = (workspace.storage_root / FILE_KEYS["update_protocol"]).is_file()

    if language == "zh-CN":
        body = [
            BRIDGE_START,
            "本项目使用 RecallLoom 管理持久化项目连续性上下文。",
            "",
            "需要恢复项目连续性时，请优先读取：",
            f"- {config}",
        ]
        if has_update_protocol:
            body.append(f"- {update_protocol}（先人工查看其中的项目级约束与补充说明）")
        body.extend(
            [
                f"- {context_brief}",
                f"- {rolling_summary}",
                f"- {daily_logs_dir}/（如存在 active 日志，则读取其中最新的一份 active daily log）",
                "",
                "平台入口文档负责工具行为规则；RecallLoom 负责项目连续性状态。",
                "如果存在 update_protocol.md，请先人工查看其中的项目级约束与补充说明；v1 helper 不会自动解析其中的自然语言内容。",
                "不要随意覆盖这些文件。",
                BRIDGE_END,
            ]
        )
    else:
        body = [
            BRIDGE_START,
            "This project uses RecallLoom for persistent project continuity.",
            "",
            "For continuity state, read:",
            f"- {config}",
        ]
        if has_update_protocol:
            body.append(f"- {update_protocol} (review project-local constraints and notes first)")
        body.extend(
            [
                f"- {context_brief}",
                f"- {rolling_summary}",
                f"- the latest active daily log under {daily_logs_dir}/ if one exists",
                "",
                "Platform entry files define tool behavior; RecallLoom defines project continuity state.",
                "If update_protocol.md exists, review its project-local constraints and notes first; v1 helpers do not parse natural-language override prose automatically.",
                "Do not overwrite these files casually.",
                BRIDGE_END,
            ]
        )
    return "\n".join(body)


def replace_or_insert_bridge(text: str, block: str) -> str:
    if BRIDGE_START in text and BRIDGE_END in text:
        pattern = re.compile(
            re.escape(BRIDGE_START) + r".*?" + re.escape(BRIDGE_END),
            flags=re.S,
        )
        updated = pattern.sub(block, text, count=1)
        return updated if updated.endswith("\n") else updated + "\n"

    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end_idx = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        except StopIteration:
            end_idx = -1
        if end_idx != -1:
            head = "\n".join(lines[: end_idx + 1]).rstrip("\n")
            tail = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
            pieces = [head, "", block]
            if tail:
                pieces.extend(["", tail])
            result = "\n".join(pieces)
            return result if result.endswith("\n") else result + "\n"

    stripped = text.rstrip("\n")
    if stripped:
        result = stripped + "\n\n" + block + "\n"
    else:
        result = block + "\n"
    return result


def remove_bridge_block(text: str) -> tuple[str, bool]:
    if BRIDGE_START not in text or BRIDGE_END not in text:
        return text, False
    pattern = re.compile(
        r"\n*" + re.escape(BRIDGE_START) + r".*?" + re.escape(BRIDGE_END) + r"\n*",
        flags=re.S,
    )
    updated = pattern.sub("\n\n", text, count=1)
    updated = re.sub(r"\n{3,}", "\n\n", updated).strip("\n")
    return ((updated + "\n") if updated else ""), True
