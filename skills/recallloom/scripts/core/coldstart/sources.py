#!/usr/bin/env python3
"""Source-tier scanning and structured proposal synthesis for cold start."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.protocol.contracts import validate_workspace_language

ROOT_ENTRY_CANDIDATES = (
    Path("README.md"),
    Path("README.en.md"),
    Path("README.zh-CN.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("GEMINI.md"),
    Path(".github/copilot-instructions.md"),
)

HIGH_SIGNAL_DOC_RE = re.compile(r"(change ?log|spec|roadmap|architecture)", re.I)
USER_REALITY_DOC_RE = re.compile(r"(memory|status|summary|handoff|decision|milestone|progress)", re.I)
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
HOST_MEMORY_CONFIDENCE_LEVELS = {"low", "medium", "high"}
HOST_MEMORY_PATH_REF = "host-memory:path"
HOST_MEMORY_COMMAND_REF = "host-memory:command"
HOST_MEMORY_PATH_PLACEHOLDER = (
    "Host memory path declared explicitly as hint-only; contents are not copied into public output."
)
HOST_MEMORY_COMMAND_PLACEHOLDER = (
    "Host memory command declared explicitly but not executed automatically."
)
HOST_MEMORY_SOURCE_MAX_LENGTH = 64
_HOST_MEMORY_SOURCE_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ -]{0,63}$")
_HOST_MEMORY_SOURCE_URL_RE = re.compile(r"(?i)\b(?:https?|file)://")
_HOST_MEMORY_SOURCE_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_HOST_MEMORY_SOURCE_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|token|secret|password|credential)\b|"
    r"\bsk-[A-Za-z0-9]{8,}\b|"
    r"\bghp_[A-Za-z0-9_]{8,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{8,}\b|"
    r"\bbearer\s+[A-Za-z0-9._-]+)"
)
_HOST_MEMORY_SOURCE_PRIVATE_WORD_RE = re.compile(
    r"(?i)(?:^|[._ +\-])(?:account|acct|tenant|workspace|user|profile|private|session|sid|transcript|conversation|thread|trace)(?=[A-Za-z0-9._ +\-]|$)"
)


def _resolved_path_within_project(candidate: Path, *, project_root: Path) -> Path | None:
    try:
        resolved_candidate = candidate.resolve(strict=True)
        resolved_project_root = project_root.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved_candidate.relative_to(resolved_project_root)
    except ValueError:
        return None
    return resolved_candidate


def _dedupe_internal_project_candidates(
    candidates: list[Path],
    *,
    project_root: Path,
) -> list[tuple[Path, Path]]:
    accepted: dict[str, dict[str, object]] = {}
    for index, candidate in enumerate(candidates):
        resolved_candidate = _resolved_path_within_project(
            candidate,
            project_root=project_root,
        )
        if not candidate.is_file() or resolved_candidate is None:
            continue
        resolved_key = resolved_candidate.as_posix()
        existing = accepted.get(resolved_key)
        if existing is None:
            accepted[resolved_key] = {
                "candidate": candidate,
                "resolved_candidate": resolved_candidate,
                "index": index,
            }
            continue
        existing_candidate = existing["candidate"]
        if isinstance(existing_candidate, Path) and existing_candidate.is_symlink() and not candidate.is_symlink():
            existing["candidate"] = candidate
            existing["resolved_candidate"] = resolved_candidate
    return [
        (entry["candidate"], entry["resolved_candidate"])
        for entry in sorted(accepted.values(), key=lambda item: int(item["index"]))
        if isinstance(entry["candidate"], Path) and isinstance(entry["resolved_candidate"], Path)
    ]


def normalize_text_lines(text: str, *, max_lines: int = 4) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            continue
        if MARKDOWN_HEADING_RE.match(stripped):
            continue
        stripped = stripped.lstrip("-* ").strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= max_lines:
            break
    return lines


def read_source_excerpt(path: Path, *, max_lines: int = 4) -> list[str]:
    try:
        return normalize_text_lines(path.read_text(encoding="utf-8"), max_lines=max_lines)
    except (OSError, UnicodeDecodeError):
        return []


def sanitize_host_memory_source_label(source_label: str | None) -> str:
    if not source_label or not source_label.strip():
        raise ValueError("Enabling host memory requires --host-memory-source.")
    normalized = source_label.strip()
    if len(normalized) > HOST_MEMORY_SOURCE_MAX_LENGTH:
        raise ValueError("Host memory source label must be a short public-safe host label.")
    if (
        _HOST_MEMORY_SOURCE_EMAIL_RE.search(normalized)
        or _HOST_MEMORY_SOURCE_URL_RE.search(normalized)
        or _HOST_MEMORY_SOURCE_SECRET_RE.search(normalized)
        or _HOST_MEMORY_SOURCE_PRIVATE_WORD_RE.search(normalized)
        or "/" in normalized
        or "\\" in normalized
        or not _HOST_MEMORY_SOURCE_SAFE_RE.fullmatch(normalized)
    ):
        raise ValueError("Host memory source label must be a short public-safe host label.")
    return normalized


def tier_a_sources(project_root: Path) -> list[dict]:
    sources: list[dict] = []
    candidates = [project_root / rel_path for rel_path in ROOT_ENTRY_CANDIDATES]
    for candidate, _resolved_candidate in _dedupe_internal_project_candidates(
        candidates,
        project_root=project_root,
    ):
        rel_path = candidate.relative_to(project_root)
        sources.append(
            {
                "tier": "A",
                "path": str(candidate),
                "relative_path": rel_path.as_posix(),
                "kind": "root_entry",
                "confidence": "high",
                "excerpt_lines": read_source_excerpt(candidate),
            }
        )
    return sources


def tier_b_sources(project_root: Path) -> list[dict]:
    sources: list[dict] = []
    docs_dir = project_root / "docs"
    candidates: list[Path] = []
    if docs_dir.is_dir():
        for candidate in sorted(docs_dir.rglob("*")):
            if not HIGH_SIGNAL_DOC_RE.search(candidate.stem):
                continue
            candidates.append(candidate)
    for candidate in sorted(project_root.iterdir()):
        if not HIGH_SIGNAL_DOC_RE.search(candidate.stem):
            continue
        candidates.append(candidate)
    for candidate, _resolved_candidate in _dedupe_internal_project_candidates(
        candidates,
        project_root=project_root,
    ):
        rel = candidate.relative_to(project_root).as_posix()
        sources.append(
            {
                "tier": "B",
                "path": str(candidate),
                "relative_path": rel,
                "kind": "high_signal_doc",
                "confidence": "medium",
                "excerpt_lines": read_source_excerpt(candidate),
            }
        )
    return sources


def tier_c_sources(project_root: Path) -> list[dict]:
    candidates = list(project_root.glob("*.md"))
    docs_dir = project_root / "docs"
    if docs_dir.is_dir():
        candidates.extend(docs_dir.rglob("*.md"))

    filtered_candidates = [
        candidate
        for candidate in sorted(candidates, key=lambda path: path.as_posix())
        if USER_REALITY_DOC_RE.search(candidate.stem)
    ]

    sources: list[dict] = []
    for candidate, _resolved_candidate in _dedupe_internal_project_candidates(
        filtered_candidates,
        project_root=project_root,
    ):
        rel = candidate.relative_to(project_root).as_posix()
        sources.append(
            {
                "tier": "C",
                "path": str(candidate),
                "relative_path": rel,
                "kind": "user_reality_doc",
                "confidence": "medium",
                "excerpt_lines": read_source_excerpt(candidate),
            }
        )
    return sources


def tier_d_sources(project_root: Path, explicit_sources: list[str]) -> list[dict]:
    sources: list[dict] = []
    missing_count = 0
    try:
        project_resolved = project_root.resolve(strict=True)
    except OSError:
        project_resolved = project_root.resolve()
    for raw in explicit_sources:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        if not candidate.is_file():
            missing_count += 1
            continue
        try:
            resolved_candidate = candidate.resolve(strict=True)
            relative_path = resolved_candidate.relative_to(project_resolved).as_posix()
            path_ref = str(resolved_candidate)
            excerpt_lines = read_source_excerpt(resolved_candidate)
        except ValueError:
            relative_path = "external-source"
            path_ref = "explicit-source:external"
            excerpt_lines = []
        except OSError:
            missing_count += 1
            continue
        sources.append(
            {
                "tier": "D",
                "path": path_ref,
                "relative_path": relative_path,
                "kind": "explicit_source",
                "confidence": "high",
                "excerpt_lines": excerpt_lines,
            }
        )
    if missing_count:
        raise ValueError("One or more explicit Tier-D source paths must exist as regular files.")
    return sources


def tier_e_sources(project_root: Path, *, include_git_signal: bool, max_commits: int = 5) -> list[dict]:
    if not include_git_signal:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "log", f"-n{max_commits}", "--pretty=%s"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    return [
        {
            "tier": "E",
            "path": f"git:{index + 1}",
            "relative_path": f"git:{index + 1}",
            "kind": "recent_commit_subject",
            "confidence": "low",
            "excerpt_lines": [line],
        }
        for index, line in enumerate(lines)
    ]


def build_host_memory_adapter(
    project_root: Path,
    *,
    enabled: bool,
    source_label: str | None,
    path_raw: str | None,
    command_raw: str | None,
    confidence: str | None,
) -> dict:
    if not enabled:
        return {
            "enabled": False,
            "source": None,
            "mode": None,
            "path": None,
            "command": None,
            "confidence": None,
            "hint_only": True,
            "ingested": False,
            "error": None,
        }

    safe_source_label = sanitize_host_memory_source_label(source_label)
    if confidence not in HOST_MEMORY_CONFIDENCE_LEVELS:
        raise ValueError(
            "Enabling host memory requires --host-memory-confidence with one of: low, medium, high."
        )
    provided = [bool(path_raw), bool(command_raw)]
    if sum(provided) != 1:
        raise ValueError(
            "Enabling host memory requires exactly one of --host-memory-path or --host-memory-command."
        )

    if path_raw:
        candidate = Path(path_raw).expanduser()
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        if not candidate.is_file():
            raise ValueError("Host memory path does not exist or is not a regular file.")
        return {
            "enabled": True,
            "source": safe_source_label,
            "mode": "path",
            "path": HOST_MEMORY_PATH_REF,
            "command": None,
            "confidence": confidence,
            "hint_only": True,
            "ingested": False,
            "error": None,
        }

    return {
        "enabled": True,
        "source": safe_source_label,
        "mode": "command",
        "path": None,
        "command": HOST_MEMORY_COMMAND_REF,
        "confidence": confidence,
        "hint_only": True,
        "ingested": False,
        "error": None,
    }


def tier_f_sources(project_root: Path, adapter: dict) -> list[dict]:
    if not adapter.get("enabled"):
        return []
    if adapter.get("mode") == "path" and adapter.get("path"):
        return [
            {
                "tier": "F",
                "path": HOST_MEMORY_PATH_REF,
                "relative_path": HOST_MEMORY_PATH_REF,
                "kind": "host_memory_path",
                "confidence": adapter["confidence"],
                "hint_only": True,
                "excerpt_lines": [HOST_MEMORY_PATH_PLACEHOLDER],
            }
        ]
    return [
        {
            "tier": "F",
            "path": HOST_MEMORY_COMMAND_REF,
            "relative_path": HOST_MEMORY_COMMAND_REF,
            "kind": "host_memory_command",
            "confidence": adapter["confidence"],
            "hint_only": True,
            "excerpt_lines": [HOST_MEMORY_COMMAND_PLACEHOLDER],
        }
    ]


def gather_coldstart_sources(
    project_root: Path,
    *,
    explicit_sources: list[str] | None = None,
    include_git_signal: bool = False,
    host_memory_adapter: dict | None = None,
) -> list[dict]:
    explicit_sources = explicit_sources or []
    host_memory_adapter = host_memory_adapter or {
        "enabled": False,
        "hint_only": True,
    }
    sources: list[dict] = []
    sources.extend(tier_a_sources(project_root))
    sources.extend(tier_b_sources(project_root))
    sources.extend(tier_c_sources(project_root))
    sources.extend(tier_d_sources(project_root, explicit_sources))
    sources.extend(tier_e_sources(project_root, include_git_signal=include_git_signal))
    sources.extend(tier_f_sources(project_root, host_memory_adapter))
    return sources


def _merge_excerpt_lines(sources: list[dict], *, tiers: tuple[str, ...], max_lines: int = 4) -> list[str]:
    lines: list[str] = []
    for source in sources:
        if source["tier"] not in tiers:
            continue
        for line in source.get("excerpt_lines", []):
            if line not in lines:
                lines.append(line)
            if len(lines) >= max_lines:
                return lines
    return lines


def _localized_coldstart_strings(language: str) -> dict[str, object]:
    if language == "zh-CN":
        return {
            "source_line": "- 第 {tier} 层 | {confidence} | {relative_path}",
            "fallback_background": "扫描到的来源还不足以明确背景，需要人工审阅。",
            "fallback_current_state": "扫描到的来源尚未明确当前状态，需要人工审阅。",
            "fallback_milestones": "扫描到的来源里没有发现明确的里程碑证据。",
            "fallback_next_step": "下一步尚不明确，提升前需要人工审阅。",
            "tier_f_disabled": "第 F 层 host-memory 来源默认关闭，且本次未读取。",
            "tier_f_path": (
                "第 F 层 host-memory 来源以显式的仅提示输入纳入，但仍需要人工审阅。"
            ),
            "tier_f_command": (
                "第 F 层 host-memory command 元数据以仅提示输入纳入；命令本身没有被自动执行。"
            ),
            "uncertainty_generated": "该提案是基于文件摘录的启发式结果，仍需要人工审阅。",
            "uncertainty_used_tiers": "使用层级：{tiers}。",
            "promotion_lines": [
                "- context_brief.md：适合承载持久的项目背景、范围和事实来源",
                "- rolling_summary.md：适合承载当前状态、下一步和当前判断",
                "- daily_logs/：仅在值得长期保留时记录里程碑证据",
            ],
            "review_before_promotion": "- 提升前需要人工审阅。",
            "generated_from": "- 本提案基于 {count} 个来源生成。",
            "no_eligible_sources": "- 没有发现符合条件的来源。",
            "no_judgment_reversals": "- 没有自动推断出判断反转。",
            "section_headings": [
                "来源摘要",
                "来源类型与可信级别",
                "候选当前状态事实",
                "候选里程碑事件",
                "候选判断反转",
                "候选下一步变化",
                "与当前 sidecar 的冲突",
                "建议提升动作",
                "审阅结论",
            ],
        }
    return {
        "source_line": "- Tier {tier} | {confidence} | {relative_path}",
        "fallback_background": "Unclear from scanned sources; needs review.",
        "fallback_current_state": "Current state is not yet explicit in the scanned sources; needs review.",
        "fallback_milestones": "No clear milestone evidence was found in the scanned sources.",
        "fallback_next_step": "Next step is not explicit; review manually before promotion.",
        "tier_f_disabled": "Tier F host-memory sources are disabled by default and were not read.",
        "tier_f_path": (
            "Tier F host-memory sources were included as explicit hint-only inputs and still require manual review."
        ),
        "tier_f_command": (
            "Tier F host-memory command metadata was included as hint-only input; the command was not auto-executed."
        ),
        "uncertainty_generated": "Proposal is generated heuristically from file excerpts and still requires review.",
        "uncertainty_used_tiers": "Used tiers: {tiers}.",
        "promotion_lines": [
            "- context_brief.md: durable project background, scope, and source-of-truth facts",
            "- rolling_summary.md: current state, next step, and active judgments",
            "- daily_logs/: milestone evidence only if worth long-term retention",
        ],
        "review_before_promotion": "- Review before promotion.",
        "generated_from": "- Generated from {count} source(s).",
        "no_eligible_sources": "- No eligible sources were found.",
        "no_judgment_reversals": "- No judgment reversals were inferred automatically.",
        "section_headings": [
            "source summary",
            "source type and confidence",
            "candidate current-state facts",
            "candidate milestone events",
            "candidate judgment reversals",
            "candidate next-step changes",
            "conflicts with current sidecar",
            "suggested promotion actions",
            "review conclusion",
        ],
    }


def render_coldstart_proposal(project_root: Path, sources: list[dict], *, workspace_language: str) -> str:
    language = validate_workspace_language(workspace_language)
    strings = _localized_coldstart_strings(language)
    tiers_used = sorted({source["tier"] for source in sources})
    tier_f_sources = [source for source in sources if source["tier"] == "F"]
    source_lines = [
        str(strings["source_line"]).format(
            tier=source["tier"],
            confidence=source["confidence"],
            relative_path=source["relative_path"],
        )
        for source in sources
    ]
    background_lines = _merge_excerpt_lines(sources, tiers=("A", "B"), max_lines=4) or [
        strings["fallback_background"]
    ]
    current_state_lines = _merge_excerpt_lines(sources, tiers=("C", "D"), max_lines=4) or [
        strings["fallback_current_state"]
    ]
    milestone_lines = _merge_excerpt_lines(sources, tiers=("B", "E"), max_lines=4) or [
        strings["fallback_milestones"]
    ]
    next_step_lines = _merge_excerpt_lines(sources, tiers=("C", "D", "E"), max_lines=3) or [
        strings["fallback_next_step"]
    ]
    tier_f_note = strings["tier_f_disabled"]
    if tier_f_sources:
        if any(source.get("kind") == "host_memory_path" for source in tier_f_sources):
            tier_f_note = strings["tier_f_path"]
        else:
            tier_f_note = strings["tier_f_command"]
    uncertainty_lines = [
        strings["uncertainty_generated"],
        str(strings["uncertainty_used_tiers"]).format(tiers=", ".join(tiers_used) if tiers_used else "none"),
        tier_f_note,
    ]
    promotion_lines = list(strings["promotion_lines"])
    headings = list(strings["section_headings"])

    sections = [
        (headings[0], [str(strings["generated_from"]).format(count=len(sources)), *background_lines[:2]]),
        (headings[1], source_lines or [strings["no_eligible_sources"]]),
        (headings[2], [*background_lines, *current_state_lines]),
        (headings[3], milestone_lines),
        (headings[4], [strings["no_judgment_reversals"]]),
        (headings[5], next_step_lines),
        (headings[6], [f"- {line}" for line in uncertainty_lines]),
        (headings[7], promotion_lines),
        (headings[8], [strings["review_before_promotion"]]),
    ]

    parts: list[str] = []
    for heading, lines in sections:
        parts.append(f"## {heading}")
        parts.extend(lines)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def recommend_coldstart_path(sources: list[dict]) -> dict:
    tiers = {source["tier"] for source in sources}
    has_current_state_signal = any(source["tier"] in {"C", "D"} and source.get("excerpt_lines") for source in sources)
    has_core_project_signal = any(source["tier"] in {"A", "B"} and source.get("excerpt_lines") for source in sources)
    uses_host_memory = "F" in tiers
    uses_explicit_or_git = bool({"D", "E"} & tiers)

    fast_path = has_core_project_signal and has_current_state_signal and not uses_host_memory
    if fast_path:
        reasoning = [
            "Tier A/B project framing and Tier C/D current-state signals are both present.",
            "No host-memory fallback was needed for the default path.",
        ]
        if uses_explicit_or_git:
            reasoning.append("Tier D/E supplemental signals exist but do not by themselves force a deep path.")
        return {
            "interaction_mode": "fast_path",
            "needs_deep_review": False,
            "reasoning": reasoning,
        }

    reasoning = []
    if not has_core_project_signal:
        reasoning.append("Project framing sources are insufficient for a low-ambiguity cold start.")
    if not has_current_state_signal:
        reasoning.append("Current-state signals are insufficient to seed continuity safely.")
    if uses_host_memory:
        reasoning.append("Host memory is hint-only and should trigger explicit review before promotion.")
    if not reasoning:
        reasoning.append("Cold-start proposal still requires explicit review because source coverage is ambiguous.")
    return {
        "interaction_mode": "deep_path",
        "needs_deep_review": True,
        "reasoning": reasoning,
    }
