#!/usr/bin/env python3
"""Query RecallLoom continuity files through a read-only recall surface."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core.continuity.freshness import (
    continuity_state_for_workspace as shared_continuity_state_for_workspace,
    evaluate_continuity_freshness,
    freshness_risk_summary,
    summary_matches_empty_shell_template as shared_summary_matches_empty_shell_template,
)
from core.trust.state import evaluate_trust_state

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    enforce_package_support_gate,
    exit_with_cli_error,
    ensure_supported_python_version,
    EnvironmentContractError,
    exit_if_startup_scratch_residue,
    extract_section_text,
    find_recallloom_root,
    FILE_KEYS,
    invalid_iso_like_daily_log_files,
    latest_active_daily_log,
    load_workspace_state,
    MARKDOWN_HEADING_RE,
    parse_daily_log_entry_line,
    parse_file_state_marker,
    parse_iso_date,
    public_project_path,
    public_project_root_label,
    read_text,
    scan_auto_attached_context_text,
    section_keys_in_text,
    sorted_daily_log_files,
    StorageResolutionError,
)


SOURCE_TYPE_PRIORITY = {
    "rolling_summary": 4,
    "derived_overlay": 3,
    "context_brief": 2,
    "latest_daily_log": 2,
    "recent_daily_log": 1,
}

DERIVED_OVERLAY_REL_PATH = "derived/current-routing-overlay.json"
DERIVED_OVERLAY_SCHEMA_VERSION = "recallloom.derived-current-routing-overlay.v1"
DERIVED_OVERLAY_FALLBACK_SOURCE = "rolling_summary"
RECALL_ACCURACY_MARKER = "rl-rag-contract-v1"
RECALL_ACCURACY_FIXTURE_IDS = frozenset(
    {"RAG-001", "RAG-002", "RAG-003", "RAG-004", "RAG-005", "RAG-006"}
)
RECALL_ACCURACY_MARKER_SOURCE_CLASSES = frozenset(
    {
        "release_truth",
        "stale_candidate",
        "redirect_stub",
        "retired_material",
        "boundary_marker",
        "release_blocker",
    }
)

SUPPORTING_CONTEXT_WINDOW_MAX_TOKENS = 160

CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_/-]+")

PLACEHOLDER_RECALL_LINES = {
    "write the validated handoff-first state here:",
    "active state",
    "relevant files",
    "critical context",
    "record the coordination judgments that matter right now:",
    "key decisions",
    "active assumptions",
    "tradeoffs in force",
    "make blocker visibility explicit:",
    "blocked items",
    "open questions",
    "external dependencies",
    "describe the handoff-first next move:",
    "active task",
    "owner or role when known",
    "immediate next action",
    "这里优先写 handoff-first 的已确认当前状态：",
    "当前活跃状态",
    "相关文件",
    "关键上下文",
    "这里记录当前真正影响推进的判断：",
    "关键决策",
    "当前假设",
    "仍在生效的取舍",
    "把阻塞与未决问题写清楚：",
    "当前阻塞",
    "未决问题",
    "外部依赖",
    "这里写 handoff-first 的下一步：",
    "当前任务",
    "已知负责人或角色",
    "立刻要做的动作",
    "-",
    "none",
    "n/a",
    "todo",
    "tbd",
    "无",
    "暂无",
}
PLACEHOLDER_RECALL_LINES = {item.casefold() for item in PLACEHOLDER_RECALL_LINES}

QUERY_INTENT_KEYWORDS = {
    "status": (
        "status",
        "current state",
        "state",
        "状态",
        "现状",
        "当前状态",
        "现在情况",
    ),
    "next_step": (
        "next step",
        "next",
        "todo",
        "下一步",
        "接下来",
        "下一项",
        "后续",
    ),
    "risk": (
        "risk",
        "blocker",
        "issue",
        "风险",
        "阻塞",
        "问题",
        "隐患",
    ),
    "decision": (
        "decision",
        "judgment",
        "choice",
        "决策",
        "决定",
        "判断",
    ),
    "progress": (
        "progress",
        "milestone",
        "done",
        "进展",
        "里程碑",
        "完成",
        "推进",
    ),
    "background": (
        "background",
        "context",
        "why",
        "背景",
        "上下文",
        "缘由",
        "为什么",
    ),
    "timeline": (
        "timeline",
        "when",
        "date",
        "时间线",
        "什么时候",
        "日期",
    ),
}

INTENT_SECTION_BOOSTS = {
    "status": {
        ("rolling_summary", "current_state"): 6,
        ("context_brief", "current_phase"): 3,
        ("latest_daily_log", "confirmed_facts"): 2,
    },
    "next_step": {
        ("rolling_summary", "next_step"): 8,
        ("latest_daily_log", "recommended_next_step"): 5,
        ("recent_daily_log", "recommended_next_step"): 4,
    },
    "risk": {
        ("rolling_summary", "risks_open_questions"): 8,
        ("latest_daily_log", "risks_blockers"): 5,
        ("recent_daily_log", "risks_blockers"): 4,
    },
    "decision": {
        ("rolling_summary", "active_judgments"): 7,
        ("latest_daily_log", "key_decisions"): 6,
        ("recent_daily_log", "key_decisions"): 5,
    },
    "progress": {
        ("latest_daily_log", "work_completed"): 7,
        ("recent_daily_log", "work_completed"): 6,
        ("rolling_summary", "recent_pivots"): 3,
    },
    "background": {
        ("context_brief", "mission"): 7,
        ("context_brief", "source_of_truth"): 6,
        ("context_brief", "core_workflow"): 5,
        ("context_brief", "scope"): 4,
    },
    "timeline": {
        ("recent_daily_log", "work_completed"): 5,
        ("latest_daily_log", "work_completed"): 4,
        ("context_brief", "current_phase"): 3,
    },
}

SECTION_PRIORITY = {
    "rolling_summary": {
        "current_state": 5,
        "active_judgments": 4,
        "next_step": 3,
        "risks_open_questions": 2,
        "recent_pivots": 1,
    },
    "context_brief": {
        "current_phase": 5,
        "mission": 4,
        "source_of_truth": 4,
        "core_workflow": 3,
        "scope": 2,
        "boundaries": 2,
        "audience_stakeholders": 1,
    },
    "latest_daily_log": {
        "confirmed_facts": 5,
        "key_decisions": 4,
        "recommended_next_step": 3,
        "work_completed": 2,
        "risks_blockers": 1,
    },
    "recent_daily_log": {
        "confirmed_facts": 5,
        "key_decisions": 4,
        "recommended_next_step": 3,
        "work_completed": 2,
        "risks_blockers": 1,
    },
}

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
        description="Query RecallLoom continuity through a read-only recall surface."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument("--query", required=True, help="Query text.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum number of hits to return. Defaults to 5.")
    parser.add_argument(
        "--include-daily-logs",
        action="store_true",
        help="Include recent daily logs beyond the latest active daily log.",
    )
    parser.add_argument(
        "--mode",
        choices=["brief", "detailed"],
        default="brief",
        help="Output verbosity. Defaults to brief.",
    )
    scan_mode_group = parser.add_mutually_exclusive_group()
    scan_mode_group.add_argument(
        "--quick",
        action="store_true",
        help="Use the quick freshness path only. This is the default query behavior.",
    )
    scan_mode_group.add_argument(
        "--full",
        action="store_true",
        help="Run the heavier workspace-artifact freshness scan before answering.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def _add_unique(target: list[str], seen: set[str], value: str) -> None:
    normalized = value.casefold()
    if normalized in seen:
        return
    seen.add(normalized)
    target.append(normalized)


def analyze_query(query: str) -> dict:
    stripped = query.strip()
    if not stripped:
        return {
            "terms": [],
            "error_kind": "empty_query",
            "intent": "general",
            "matched_keywords": [],
        }

    ascii_tokens = ASCII_TOKEN_RE.findall(query)
    cjk_runs = CJK_RUN_RE.findall(query)
    terms: list[str] = []
    seen: set[str] = set()
    short_fragments_found = False

    for token in ascii_tokens:
        if len(token) >= 2:
            _add_unique(terms, seen, token)
        else:
            short_fragments_found = True

    for run in cjk_runs:
        if len(run) >= 2:
            _add_unique(terms, seen, run)
            for idx in range(0, len(run) - 1):
                _add_unique(terms, seen, run[idx : idx + 2])
        else:
            short_fragments_found = True

    if terms:
        interpretation = interpret_query(query)
        return {
            "terms": terms,
            "error_kind": None,
            "intent": interpretation["intent"],
            "matched_keywords": interpretation["matched_keywords"],
        }

    if ascii_tokens or cjk_runs or short_fragments_found:
        error_kind = "query_too_short"
    else:
        error_kind = "no_searchable_fragments"
    return {
        "terms": [],
        "error_kind": error_kind,
        "intent": "general",
        "matched_keywords": [],
    }


def tokenize_query(query: str) -> list[str]:
    return analyze_query(query)["terms"]


def interpret_query(query: str) -> dict:
    lowered = query.casefold()
    best_intent = "general"
    best_keywords: list[str] = []
    best_score = 0

    for intent, keywords in QUERY_INTENT_KEYWORDS.items():
        matched = [keyword for keyword in keywords if keyword.casefold() in lowered]
        if len(matched) > best_score:
            best_score = len(matched)
            best_intent = intent
            best_keywords = matched

    return {
        "intent": best_intent,
        "matched_keywords": best_keywords,
    }


def score_text(text: str, query_terms: list[str]) -> int:
    lowered = normalized_recall_text(text)
    return sum(lowered.count(term) for term in query_terms)


def matched_query_terms(text: str, query_terms: list[str]) -> int:
    lowered = normalized_recall_text(text)
    unique_terms = []
    seen: set[str] = set()
    for term in query_terms:
        if term in seen:
            continue
        seen.add(term)
        unique_terms.append(term)
    return sum(1 for term in unique_terms if term in lowered)


def normalized_content_lines(text: str) -> list[str]:
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
        if stripped.casefold() in PLACEHOLDER_RECALL_LINES:
            continue
        lines.append(stripped)
    return lines


def normalized_recall_text(text: str) -> str:
    return "\n".join(normalized_content_lines(text)).casefold()


def excerpt_text(text: str, *, max_lines: int = 5) -> str:
    return "\n".join(normalized_content_lines(text)[:max_lines])


def gather_file_hits(
    *,
    path: Path,
    source_type: str,
    query_terms: list[str],
    full_query: str,
) -> list[dict]:
    text = read_text(path)
    hits: list[dict] = []
    keys = section_keys_in_text(text)
    if not keys:
        score = score_text(text, query_terms)
        if score > 0:
            exact_phrase = full_query in normalized_recall_text(text)
            hits.append(
                {
                    "path": str(path),
                    "section": None,
                    "score": score,
                    "matched_terms": matched_query_terms(text, query_terms),
                    "exact_phrase": exact_phrase,
                    "source_type": source_type,
                    "excerpt": excerpt_text(text),
                    "full_text": text,
                }
            )
        return hits

    for key in keys:
        section_text = extract_section_text(text, key)
        score = score_text(section_text, query_terms)
        if score <= 0:
            continue
        exact_phrase = full_query in normalized_recall_text(section_text)
        hits.append(
            {
                "path": str(path),
                "section": key,
                "score": score,
                "matched_terms": matched_query_terms(section_text, query_terms),
                "exact_phrase": exact_phrase,
                "source_type": source_type,
                "excerpt": excerpt_text(section_text),
                "full_text": section_text,
            }
        )
    return hits


def gather_daily_log_hits(
    *,
    path: Path,
    source_type: str,
    query_terms: list[str],
    full_query: str,
    text: str | None = None,
) -> list[dict]:
    log_text = read_text(path) if text is None else text
    hits: list[dict] = []
    current_entry = None
    current_section: str | None = None
    section_lines: list[str] = []

    def flush_section() -> None:
        if current_entry is None or current_section is None:
            return
        section_text = "\n".join(section_lines)
        score = score_text(section_text, query_terms)
        if score <= 0:
            return
        exact_phrase = full_query in normalized_recall_text(section_text)
        hits.append(
            {
                "path": str(path),
                "section": current_section,
                "score": score,
                "matched_terms": matched_query_terms(section_text, query_terms),
                "exact_phrase": exact_phrase,
                "source_type": source_type,
                "excerpt": excerpt_text(section_text),
                "full_text": section_text,
                "evidenced_by": {
                    "daily_log_path": str(path),
                    "entry_id": current_entry.entry_id,
                    "entry_seq": current_entry.entry_seq,
                },
            }
        )

    for raw in log_text.splitlines():
        entry = parse_daily_log_entry_line(raw)
        if entry is not None:
            flush_section()
            current_entry = entry
            current_section = None
            section_lines = []
            continue
        stripped = raw.strip()
        if stripped.startswith("<!-- section: "):
            flush_section()
            current_section = stripped.removeprefix("<!-- section: ").removesuffix(" -->")
            section_lines = []
            continue
        section_lines.append(raw)

    flush_section()
    return hits


def _overlay_malformed_review(
    *,
    path: Path,
    reason_code: str,
) -> dict:
    return {
        "path": str(path),
        "status": "fallback",
        "included": False,
        "derived_overlay_read": True,
        "warning_code": "derived_overlay_malformed",
        "malformed_reason": reason_code,
        "fallback_source": DERIVED_OVERLAY_FALLBACK_SOURCE,
        "truth_contribution": "none",
        "public_safe": True,
    }


def _overlay_stale_review(
    *,
    path: Path,
    base_revision: int,
    current_revision: int,
) -> dict:
    return {
        "path": str(path),
        "status": "fallback",
        "included": False,
        "derived_overlay_read": True,
        "warning_code": "derived_overlay_stale",
        "base_rolling_summary_revision": base_revision,
        "current_rolling_summary_revision": current_revision,
        "fallback_source": DERIVED_OVERLAY_FALLBACK_SOURCE,
        "truth_contribution": "none",
        "public_safe": True,
    }


def _normalize_overlay_answer(value: str) -> str:
    return " ".join(normalized_content_lines(value)).casefold()


def _overlay_claim_conflicts_with_primary(*, claim_answer: str, primary_excerpt: str) -> bool:
    normalized_claim = _normalize_overlay_answer(claim_answer)
    if not normalized_claim:
        return False
    normalized_primary = _normalize_overlay_answer(primary_excerpt)
    if normalized_claim == normalized_primary:
        return False
    primary_lines = {_normalize_overlay_answer(line) for line in normalized_content_lines(primary_excerpt)}
    primary_lines.discard("")
    return normalized_claim not in primary_lines


def _overlay_claim_matches_query(claim: dict, query_terms: list[str]) -> bool:
    terms = claim.get("query_terms")
    if not isinstance(terms, list):
        return False
    normalized_query_terms = set(query_terms)
    for item in terms:
        if not isinstance(item, str):
            continue
        analyzed = analyze_query(item)
        if set(analyzed["terms"]).intersection(normalized_query_terms):
            return True
    return False


def _validate_overlay_claim(raw_claim: object) -> dict | str:
    if not isinstance(raw_claim, dict):
        return "claim_not_object"
    raw_query_terms = raw_claim.get("query_terms")
    answer = raw_claim.get("answer")
    if not isinstance(raw_query_terms, list) or not raw_query_terms:
        return "claim_query_terms_invalid"
    if not all(isinstance(item, str) and item.strip() for item in raw_query_terms):
        return "claim_query_terms_invalid"
    if not isinstance(answer, str) or not answer.strip():
        return "claim_answer_invalid"
    return {
        "query_terms": [item.strip() for item in raw_query_terms],
        "answer": answer.strip(),
    }


def load_derived_overlay_review(
    *,
    path: Path,
    summary_revision: int,
    query_terms: list[str],
    primary_truth_hit: dict | None,
) -> dict:
    if not path.exists():
        return {
            "path": str(path),
            "status": "absent",
            "included": False,
            "derived_overlay_read": False,
            "fallback_source": DERIVED_OVERLAY_FALLBACK_SOURCE,
            "truth_contribution": "none",
            "public_safe": True,
        }
    if not path.is_file():
        return _overlay_malformed_review(path=path, reason_code="overlay_not_file")
    try:
        payload = json.loads(read_text(path))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return _overlay_malformed_review(path=path, reason_code="malformed_json")
    if not isinstance(payload, dict):
        return _overlay_malformed_review(path=path, reason_code="top_level_not_object")

    schema_version = payload.get("schema_version")
    base_revision = payload.get("base_rolling_summary_revision")
    raw_claims = payload.get("current_truth_claims")
    if schema_version != DERIVED_OVERLAY_SCHEMA_VERSION:
        return _overlay_malformed_review(path=path, reason_code="invalid_schema_version")
    if not isinstance(base_revision, int) or base_revision < 1:
        return _overlay_malformed_review(path=path, reason_code="invalid_base_rolling_summary_revision")
    if not isinstance(raw_claims, list):
        return _overlay_malformed_review(path=path, reason_code="invalid_current_truth_claims")

    claims: list[dict] = []
    for raw_claim in raw_claims:
        claim = _validate_overlay_claim(raw_claim)
        if isinstance(claim, str):
            return _overlay_malformed_review(path=path, reason_code=claim)
        claims.append(claim)

    if base_revision != summary_revision:
        return _overlay_stale_review(
            path=path,
            base_revision=base_revision,
            current_revision=summary_revision,
        )

    matching_claims = [claim for claim in claims if _overlay_claim_matches_query(claim, query_terms)]
    supporting_hits = [
        {
            "path": str(path),
            "section": "current_truth_claims",
            "score": matched_query_terms(" ".join(claim["query_terms"]), query_terms),
            "matched_terms": matched_query_terms(" ".join(claim["query_terms"]), query_terms),
            "exact_phrase": False,
            "source_type": "derived_overlay",
            "excerpt": claim["answer"],
            "full_text": claim["answer"],
        }
        for claim in matching_claims
    ]
    primary_excerpt = primary_truth_hit.get("excerpt") if primary_truth_hit else None
    for claim in matching_claims:
        if (
            isinstance(primary_excerpt, str)
            and primary_excerpt.strip()
            and _overlay_claim_conflicts_with_primary(
                claim_answer=claim["answer"],
                primary_excerpt=primary_excerpt,
            )
        ):
            return {
                "path": str(path),
                "status": "conflict",
                "included": False,
                "derived_overlay_read": True,
                "reason_code": "derived_overlay_conflict",
                "fallback_source": DERIVED_OVERLAY_FALLBACK_SOURCE,
                "truth_contribution": "none",
                "base_rolling_summary_revision": base_revision,
                "current_rolling_summary_revision": summary_revision,
                "matched_claim_count": len(matching_claims),
                "conflicting_claim_terms": claim["query_terms"],
                "conflicting_claim_term_count": len(claim["query_terms"]),
                "primary_source": DERIVED_OVERLAY_FALLBACK_SOURCE,
                "primary_source_class": primary_truth_hit.get("source_class"),
                "primary_source_section": primary_truth_hit.get("section"),
                "primary_excerpt_char_count": len(primary_excerpt),
                "public_safe": True,
            }

    return {
        "path": str(path),
        "status": "included" if matching_claims else "fresh_no_match",
        "included": bool(matching_claims),
        "derived_overlay_read": True,
        "fallback_source": None,
        "truth_contribution": "supporting_derived_only" if matching_claims else "none",
        "base_rolling_summary_revision": base_revision,
        "current_rolling_summary_revision": summary_revision,
        "matched_claim_count": len(matching_claims),
        "supporting_hits": supporting_hits,
        "public_safe": True,
    }


def source_class_for_hit(source_type: str, section: str | None) -> str:
    if source_type == "rolling_summary":
        if section in {"current_state", "active_judgments", "next_step", "risks_open_questions"}:
            return "current_truth"
        return "current_summary"
    if source_type == "derived_overlay":
        return "supporting_derived_overlay"
    if source_type == "context_brief":
        return "durable_context"
    if source_type in {"latest_daily_log", "recent_daily_log"}:
        return "historical_evidence"
    if source_type == "update_protocol":
        return "routing_policy"
    return "supporting_context"


def hit_is_primary_truth_candidate(hit: dict, *, operation_class: str) -> bool:
    source_class = hit.get("source_class")
    if source_class == "supporting_derived_overlay":
        return False
    if operation_class == "read_historical_fact" and source_class == "historical_evidence":
        return True
    if hit.get("do_not_use_as_current_fact"):
        return False
    return source_class in {"current_truth", "current_summary"}


def annotate_hit_truth_metadata(hit: dict, *, active_source: bool) -> dict:
    source_class = source_class_for_hit(hit["source_type"], hit.get("section"))
    if source_class == "current_truth":
        active_boundary = "rolling_summary_current_state"
    elif source_class == "current_summary":
        active_boundary = "rolling_summary_supporting_summary"
    elif source_class == "supporting_derived_overlay":
        active_boundary = "derived_overlay_support_only"
    else:
        active_boundary = "historical_or_context_support_only"
    annotated = {
        **hit,
        "source_class": source_class,
        "active_source": active_source,
        "evidenced_by": hit.get("evidenced_by"),
        "active_boundary": active_boundary,
    }
    if source_class == "historical_evidence":
        annotated.update(
            {
                "do_not_promote_to_current": True,
                "do_not_use_as_current_fact": True,
            }
        )
    elif source_class in {"durable_context", "routing_policy", "supporting_derived_overlay"}:
        annotated.update(
            {
                "do_not_promote_to_current": True,
                "do_not_use_as_current_fact": source_class
                in {"durable_context", "supporting_derived_overlay"},
            }
        )
    else:
        annotated.update(
            {
                "do_not_promote_to_current": False,
                "do_not_use_as_current_fact": False,
            }
        )
    return annotated


def supporting_context_window(
    hits: list[dict],
    *,
    mode: str,
) -> list[dict]:
    if mode != "detailed":
        return []
    window: list[dict] = []
    consumed_tokens = 0
    for item in hits[:3]:
        excerpt_budget = token_estimate(item["excerpt"])
        if window and consumed_tokens + excerpt_budget > SUPPORTING_CONTEXT_WINDOW_MAX_TOKENS:
            break
        window.append(
            {
                "path": item["path"],
                "section": item["section"],
                "source_type": item["source_type"],
                "date": item.get("date"),
                "excerpt": item["excerpt"],
                "score": item["score"],
            }
        )
        consumed_tokens += excerpt_budget
    return window


def token_estimate(text: str) -> int:
    ascii_tokens = len(ASCII_TOKEN_RE.findall(text))
    cjk_chars = sum(len(run) for run in CJK_RUN_RE.findall(text))
    non_whitespace = len(re.findall(r"\S", text))
    residual_tokens = max(0, non_whitespace - cjk_chars - ascii_tokens)
    estimate = (ascii_tokens / 0.75) + (cjk_chars * 0.65) + residual_tokens
    return max(1, round(estimate))


def budget_hint(estimate: int) -> str:
    if estimate <= 120:
        return "small"
    if estimate <= 300:
        return "medium"
    return "large"


def output_variant_for_mode(mode: str) -> str:
    if mode == "detailed":
        return "expanded_contextual"
    return "compact_attach_safe"


def source_priority(source_type: str) -> int:
    return SOURCE_TYPE_PRIORITY.get(source_type, 0)


def section_priority(source_type: str, section: str | None, *, query_intent: str = "general") -> int:
    if section is None:
        return 0
    base = SECTION_PRIORITY.get(source_type, {}).get(section, 0)
    boost = INTENT_SECTION_BOOSTS.get(query_intent, {}).get((source_type, section), 0)
    return base + boost


def log_recency_value(path_raw: str, source_type: str) -> int:
    if source_type not in {"latest_daily_log", "recent_daily_log"}:
        return 0
    try:
        return parse_iso_date(Path(path_raw).stem).toordinal()
    except ValueError:
        return 0


def sort_hits(hits: list[dict], *, query_intent: str = "general") -> list[dict]:
    ordered = list(hits)
    ordered.sort(
        key=lambda item: (
            -int(item["exact_phrase"]),
            -item["matched_terms"],
            -item["score"],
            -source_priority(item["source_type"]),
            -section_priority(item["source_type"], item["section"], query_intent=query_intent),
            -log_recency_value(item["path"], item["source_type"]),
            item["path"],
            item["section"] or "",
        )
    )
    return ordered


def citation_date(path_raw: str, source_type: str) -> str | None:
    if source_type not in {"latest_daily_log", "recent_daily_log"}:
        return None
    try:
        return parse_iso_date(Path(path_raw).stem).isoformat()
    except ValueError:
        return None


def conflict_state_for_hits(
    *,
    freshness: dict,
    hits: list[dict],
    continuity_state: str,
) -> str:
    if continuity_state == "initialized_empty_shell":
        return "empty_shell_not_seeded"
    if freshness["workspace_artifact_newer_than_summary"]:
        return "workspace_artifact_newer_than_summary"
    if freshness["summary_revision_stale"]:
        return "summary_revision_stale"
    if len(hits) >= 2:
        first = hits[0]
        second = hits[1]
        same_strength = (
            first["exact_phrase"] == second["exact_phrase"]
            and first["matched_terms"] == second["matched_terms"]
            and first["score"] == second["score"]
        )
        if same_strength and first["source_type"] != second["source_type"]:
            return "multi_source_review_recommended"
    return "none"


def confidence_for_hits(
    continuity_confidence: str,
    hits: list[dict],
    *,
    continuity_state: str,
    conflict_state: str,
    query_terms: list[str],
) -> str:
    if continuity_state == "initialized_empty_shell":
        return "low"
    if not hits:
        return "low"
    if continuity_confidence == "broken":
        return "low"
    top_hit = hits[0]
    strong_match = (
        top_hit["exact_phrase"]
        or top_hit["matched_terms"] >= max(1, len(set(query_terms)))
        or top_hit["score"] >= max(2, len(set(query_terms)))
    )
    if conflict_state in {"workspace_artifact_newer_than_summary", "summary_revision_stale"}:
        return "medium" if strong_match and continuity_confidence != "low" else "low"
    if conflict_state == "multi_source_review_recommended":
        return "medium"
    if continuity_confidence == "high" and strong_match:
        return "high"
    if strong_match:
        return "medium"
    return "low"


def public_hits(hits: list[dict]) -> list[dict]:
    return [
        {
            "path": item["path"],
            "section": item["section"],
            "score": item["score"],
            "source_type": item["source_type"],
            "source_class": item.get("source_class"),
            "active_source": item.get("active_source", False),
            "evidenced_by": evidence_tuple_for_hit(item),
            "active_boundary": item.get("active_boundary"),
            "do_not_promote_to_current": item.get("do_not_promote_to_current", False),
            "do_not_use_as_current_fact": item.get("do_not_use_as_current_fact", False),
            "date": citation_date(item["path"], item["source_type"]),
            "excerpt": item["excerpt"],
        }
        for item in hits
    ]


def evidence_tuple_for_hit(hit: dict) -> dict | None:
    evidence = hit.get("evidenced_by")
    if not evidence:
        return None
    return {
        **evidence,
        "section": hit.get("section"),
        "source_type": hit.get("source_type"),
        "source_class": hit.get("source_class"),
        "active_boundary": hit.get("active_boundary"),
        "do_not_promote_to_current": hit.get("do_not_promote_to_current", True),
        "do_not_use_as_current_fact": hit.get("do_not_use_as_current_fact", True),
        "date": citation_date(hit["path"], hit["source_type"]),
        "citation_assembly_required": False,
    }


def publicize_evidence_tuple_paths(
    evidence: dict | None,
    *,
    project_root: Path,
) -> dict | None:
    if not evidence:
        return None
    public_evidence = dict(evidence)
    if public_evidence.get("daily_log_path"):
        public_evidence["daily_log_path"] = public_project_path(
            public_evidence["daily_log_path"],
            project_root=project_root,
        )
    if public_evidence.get("path"):
        public_evidence["path"] = public_project_path(
            public_evidence["path"],
            project_root=project_root,
        )
    return public_evidence


def publicize_public_hit_paths(
    hits: list[dict],
    *,
    project_root: Path,
) -> list[dict]:
    return [
        {
            **item,
            "path": public_project_path(item["path"], project_root=project_root),
            "evidenced_by": publicize_evidence_tuple_paths(
                item.get("evidenced_by"),
                project_root=project_root,
            ),
        }
        for item in hits
    ]


def operation_metadata_for_query(
    *,
    query_intent: str,
    include_daily_logs: bool,
    context_brief_included: bool,
    recent_daily_logs_included: bool,
    derived_overlay_read: bool,
) -> dict:
    if query_intent in {"timeline", "progress"} or include_daily_logs:
        operation_class = "read_historical_fact"
        recommended_path = "query_continuity --include-daily-logs"
        reason = "Historical or timeline-oriented query; daily-log evidence must remain evidence, not current truth."
        followup = "Use daily_log_path, entry_id, and entry_seq when citing evidence."
        read_set = ["rolling_summary", "latest_daily_log"]
        do_not_read_by_default = ["context_brief"]
    else:
        operation_class = "read_current_status"
        recommended_path = "query_continuity --quick"
        reason = "Fast current-status read uses bounded current sources and avoids context_brief by default."
        followup = "Escalate to detailed/background review only when current sources are insufficient."
        read_set = ["rolling_summary", "latest_daily_log"]
        do_not_read_by_default = ["context_brief"]
    if context_brief_included:
        read_set.append("context_brief")
        do_not_read_by_default = []
    if derived_overlay_read:
        read_set.append("derived_overlay")
    if recent_daily_logs_included:
        read_set.append("recent_daily_log")
    return {
        "operation_class": operation_class,
        "recommended_path": recommended_path,
        "reason": reason,
        "followup": followup,
        "read_set": read_set,
        "do_not_read_by_default": do_not_read_by_default,
    }


def read_set_evidence_for_query(
    *,
    operation_metadata: dict,
    sources_considered: list[dict],
    context_brief_included: bool,
    derived_overlay_review: dict,
    include_daily_logs: bool,
    recent_daily_logs_included: bool,
    scan_mode: str,
) -> dict:
    return {
        "read_set": operation_metadata["read_set"],
        "sources": [
            {
                "source_type": item["source_type"],
                "included": item["included"],
                "do_not_read_by_default": item.get("do_not_read_by_default", False),
                "derived_overlay_read": item.get("derived_overlay_read", False),
            }
            for item in sources_considered
        ],
        "context_brief_read": context_brief_included,
        "context_brief_default_read": False,
        "derived_overlay_read": derived_overlay_review.get("derived_overlay_read", False),
        "derived_overlay_review": derived_overlay_review,
        "include_daily_logs_requested": include_daily_logs,
        "historical_log_sweep_performed": recent_daily_logs_included,
        "workspace_artifact_scan_mode": scan_mode,
        "public_safe": True,
    }


def historical_evidence_for_hits(
    public_hit_list: list[dict],
    *,
    operation_class: str,
    primary_truth_source_class: str | None,
) -> dict:
    tuples = [
        item["evidenced_by"]
        for item in public_hit_list
        if item.get("source_class") == "historical_evidence" and item.get("evidenced_by")
    ]
    historical_hit_exists = any(
        item.get("source_class") == "historical_evidence" for item in public_hit_list
    )
    if operation_class == "read_historical_fact" and primary_truth_source_class == "historical_evidence":
        verdict = "historical_evidence_only"
    elif historical_hit_exists:
        verdict = "supporting_historical_evidence_available"
    else:
        verdict = "no_historical_evidence_match"
    return {
        "verdict": verdict,
        "current_truth_promoted": False,
        "citation_assembly_required": False,
        "evidence_tuple": tuples[0] if tuples else None,
        "evidence_tuples": tuples,
    }


def _marker_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "y"}


def _marker_list(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item for item in (part.strip() for part in value.split(",")) if item]


def recall_accuracy_contract_marker_for_hit(hit: dict) -> dict | None:
    for line in normalized_content_lines(hit.get("full_text", "")):
        if RECALL_ACCURACY_MARKER not in line:
            continue
        fields: dict[str, str] = {}
        for token in line.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key.strip()] = value.strip().strip(".,;")
        fixture_id = fields.get("fixture_id")
        marker_source_class = fields.get("source_class")
        verdict = fields.get("verdict")
        if (
            fixture_id not in RECALL_ACCURACY_FIXTURE_IDS
            or marker_source_class not in RECALL_ACCURACY_MARKER_SOURCE_CLASSES
            or not verdict
        ):
            continue
        return {
            **fields,
            "fixture_id": fixture_id,
            "marker_source_class": marker_source_class,
            "verdict": verdict,
            "marker_active_source": _marker_bool(fields.get("active_source")),
            "do_not_promote_to_current": _marker_bool(
                fields.get("do_not_promote_to_current"),
                default=not _marker_bool(fields.get("active_source")),
            ),
            "do_not_use_as_current_fact": _marker_bool(
                fields.get("do_not_use_as_current_fact"),
                default=not _marker_bool(fields.get("active_source")),
            ),
        }
    return None


def recall_accuracy_evidence_tuple_for_hit(item: dict, marker: dict) -> dict:
    evidence = dict(item.get("evidenced_by") or {})
    canonical_active_source = item.get("active_source", False)
    evidence.update(
        {
            "fixture_id": marker["fixture_id"],
            "path": item["path"],
            "section": item["section"],
            "source_type": item["source_type"],
            "source_class": item.get("source_class"),
            "marker_source_class": marker["marker_source_class"],
            "verdict": marker["verdict"],
            "active_source": canonical_active_source,
            "marker_active_source": marker["marker_active_source"],
            "active_boundary": item.get("active_boundary"),
            "do_not_promote_to_current": marker["do_not_promote_to_current"],
            "do_not_use_as_current_fact": marker["do_not_use_as_current_fact"],
            "date": item.get("date"),
        }
    )
    return evidence


def recall_accuracy_relation_payload(*, first_hit: dict, marker: dict) -> dict[str, object]:
    marker_source_class = marker["marker_source_class"]
    if marker_source_class == "release_truth":
        return {
            "relation_class": "release_truth",
            "today_verdict_first": True,
            "historical_evidence_not_promoted_to_current": _marker_bool(
                marker.get("historical_evidence_no_promotion"),
                default=True,
            ),
            "current_truth_promoted_from_history": False,
        }
    if marker_source_class == "stale_candidate":
        return {
            "relation_class": "stale_candidate",
            "superseded_by": marker.get("superseded_by", "release_truth"),
            "stale_action_default": marker.get("stale_action_default", "replacement"),
            "old_material_no_current_flag": _marker_bool(
                marker.get("old_material_no_current_flag"),
                default=True,
            ),
        }
    if marker_source_class == "redirect_stub":
        return {
            "relation_class": "redirect_stub",
            "redirects_to": marker.get("redirects_to", "successor"),
            "redirect_stub_body_truth": False,
            "successor_traceable": _marker_bool(marker.get("successor_traceable"), default=True),
            "successor_active_source": _marker_bool(marker.get("successor_active_source"), default=True),
        }
    if marker_source_class == "retired_material":
        return {
            "relation_class": "retired_material",
            "retired_by": marker.get("retired_by", "release_truth"),
            "recovery_path": marker.get("recovery_path", "historical_reference"),
            "retired_without_successor": _marker_bool(marker.get("retired_without_successor")),
            "retired_material_reference_only": True,
        }
    if marker_source_class == "boundary_marker":
        forbidden_scope = _marker_list(marker.get("forbidden_scope")) or [
            "runtime",
            "db",
            "semantic_truth",
        ]
        return {
            "relation_class": "boundary_marker",
            "allowed_merge_scope": _marker_list(marker.get("allowed_merge_scope"))
            or ["lightweight_route_markers"],
            "forbidden_scope": forbidden_scope,
            "runtime_truth_rejected": "runtime" in forbidden_scope,
            "db_truth_rejected": "db" in forbidden_scope,
            "semantic_truth_rejected": "semantic_truth" in forbidden_scope,
            "forbidden_scope_bypass_rejected": _marker_bool(
                marker.get("forbidden_scope_bypass_rejected"),
                default=True,
            ),
        }
    if marker_source_class == "release_blocker":
        return {
            "relation_class": "release_blocker",
            "must_fix_for_next_release": _marker_list(marker.get("must_fix_for_next_release"))
            or ["release_blocking_marker"],
            "release_blocking": _marker_bool(marker.get("release_blocking"), default=True),
            "auditable_evidence_pointer": {
                "path": first_hit["path"],
                "section": first_hit["section"],
                "source_type": first_hit["source_type"],
                "source_class": first_hit.get("source_class"),
                "marker_source_class": marker["marker_source_class"],
            },
        }
    return {}


def recall_accuracy_evidence_for_query(
    *,
    operation_metadata: dict,
    primary_truth_hit: dict | None,
    hits: list[dict],
    project_root: Path,
) -> dict:
    evidence_hits = []
    for item in hits:
        if item.get("source_class") == "supporting_derived_overlay":
            continue
        marker = recall_accuracy_contract_marker_for_hit(item)
        if marker is None:
            continue
        evidence_hits.append({**item, "recall_accuracy_marker": marker})
    if not evidence_hits:
        return {
            "verdict": "no_recall_accuracy_evidence_match",
            "source_class": "no_match",
            "fixture_id": None,
            "active_source": False,
            "evidence_tuple": None,
            "evidence_tuples": [],
            "current_truth_promoted": False,
            "historical_evidence_not_promoted_to_current": True,
            "do_not_promote_to_current": True,
            "do_not_use_as_current_fact": True,
            "public_safe": True,
        }

    first_hit = evidence_hits[0]
    first_marker = first_hit["recall_accuracy_marker"]
    evidence_tuples = [
        recall_accuracy_evidence_tuple_for_hit(item, item["recall_accuracy_marker"])
        for item in evidence_hits
    ]
    evidence_tuples = [
        publicize_evidence_tuple_paths(item, project_root=project_root)
        for item in evidence_tuples
    ]
    relation_payload = recall_accuracy_relation_payload(
        first_hit=first_hit,
        marker=first_marker,
    )
    if relation_payload.get("auditable_evidence_pointer"):
        pointer = dict(relation_payload["auditable_evidence_pointer"])
        pointer["path"] = public_project_path(pointer.get("path"), project_root=project_root)
        relation_payload["auditable_evidence_pointer"] = pointer
    historical_evidence_tuples = [
        publicize_evidence_tuple_paths(
            evidence_tuple_for_hit(item),
            project_root=project_root,
        )
        for item in hits
        if item.get("source_class") == "historical_evidence" and item.get("evidenced_by")
    ]

    return {
        "verdict": first_marker["verdict"],
        "source_class": first_hit.get("source_class"),
        "marker_source_class": first_marker["marker_source_class"],
        "fixture_id": first_marker["fixture_id"],
        "active_source": first_hit.get("active_source", False),
        "marker_active_source": first_marker["marker_active_source"],
        "evidence_tuple": evidence_tuples[0] if evidence_tuples else None,
        "evidence_tuples": evidence_tuples,
        "current_truth_promoted": False,
        "historical_evidence_not_promoted_to_current": True,
        "historical_evidence_tuples": historical_evidence_tuples,
        "do_not_promote_to_current": first_marker["do_not_promote_to_current"],
        "do_not_use_as_current_fact": first_marker["do_not_use_as_current_fact"],
        "public_safe": True,
        "operation_class": operation_metadata["operation_class"],
        "primary_truth_source_class": (
            primary_truth_hit.get("source_class") if primary_truth_hit is not None else None
        ),
        **relation_payload,
    }


def ux_verdict_for_query(
    *,
    operation_metadata: dict,
    primary_truth_hit: dict | None,
    read_set_evidence: dict,
    historical_evidence: dict,
) -> dict:
    operation_class = operation_metadata["operation_class"]
    if operation_class == "read_historical_fact":
        verdict = (
            "historical_evidence_only"
            if primary_truth_hit is not None and primary_truth_hit.get("source_class") == "historical_evidence"
            else "historical_query_no_direct_evidence"
        )
        current_vs_historical = (
            "historical_evidence_not_current_truth"
            if verdict == "historical_evidence_only"
            else "no_current_or_historical_truth_match"
        )
    else:
        verdict = (
            "current_status_found"
            if primary_truth_hit is not None and primary_truth_hit.get("source_class") == "current_truth"
            else "current_status_not_found"
        )
        current_vs_historical = (
            "active_current_truth"
            if verdict == "current_status_found"
            else "no_active_current_truth_match"
        )
    return {
        "operation_class": operation_class,
        "verdict": verdict,
        "current_vs_historical": current_vs_historical,
        "reason": operation_metadata["reason"],
        "followup": operation_metadata["followup"],
        "read_set_evidence": read_set_evidence,
        "historical_evidence": historical_evidence,
        "current_truth_promoted_from_history": False,
        "citation_assembly_required": False,
        "public_safe": True,
    }


def context_risk_review_for_query(
    *,
    context_brief_included: bool,
) -> dict:
    if context_brief_included:
        return {
            "mode": "deep_context_risk_review",
            "review_lane": "deep_context_risk_review",
            "reason_code": "background_or_detailed_context_review_requested",
            "human_decision_required": True,
            "human_decision_point": (
                "Decide before any later write, promotion, or escalation whether "
                "context_brief background should influence the reviewed update."
            ),
            "context_brief_included": True,
            "context_brief_default_read": False,
            "context_brief_default_written": False,
            "write_effect": "none",
            "public_safe": True,
        }
    return {
        "mode": "fast_current_status",
        "review_lane": "fast_current_status",
        "reason_code": "context_brief_not_default_read",
        "human_decision_required": False,
        "human_decision_point": "none",
        "context_brief_included": False,
        "context_brief_default_read": False,
        "context_brief_default_written": False,
        "write_effect": "none",
        "public_safe": True,
    }


def render_synthesized_recall(
    *,
    query: str,
    answer: str,
    citations: list[dict],
    hits: list[dict],
    mode: str,
    risk_freshness_note: str | None,
) -> str:
    lines = [f"Query: {query}", "", f"Answer: {answer}"]
    if citations:
        lines.extend(["", "Supporting citations:"])
        citation_items = citations if mode == "detailed" else citations[:3]
        for item in citation_items:
            section_label = f" [{item['section']}]" if item["section"] else ""
            date_suffix = f" ({item['date']})" if item["date"] else ""
            lines.append(f"- {Path(item['path']).name}{section_label}{date_suffix}")
    if hits and mode == "detailed":
        lines.extend(["", "Supporting excerpts:"])
        for item in hits:
            section_label = f" [{item['section']}]" if item["section"] else ""
            source_label = f" ({item['source_type']})"
            lines.append(
                f"- {Path(item['path']).name}{section_label}{source_label}: {item['excerpt']}"
            )
    if risk_freshness_note:
        lines.extend(["", f"Risk/Freshness note: {risk_freshness_note}"])
    return "\n".join(lines).strip()


def answer_for_query(*, hits: list[dict], continuity_state: str) -> str:
    if continuity_state == "initialized_empty_shell":
        return "Continuity is initialized but not seeded yet; no project-state answer is available."
    if not hits:
        return "No strong continuity answer was found in the current core continuity files."
    return hits[0]["excerpt"]


def surface_hits_for_query(
    hits: list[dict],
    *,
    limit: int,
    primary_truth_hit: dict | None,
) -> list[dict]:
    if not hits:
        return []
    if primary_truth_hit is None:
        visible_hits = [hit for hit in hits if hit.get("source_class") != "supporting_derived_overlay"]
        return visible_hits[:limit]

    visible_hits = hits[:limit]
    if primary_truth_hit in visible_hits:
        return visible_hits
    overlay_index = next(
        (
            idx
            for idx, hit in enumerate(visible_hits)
            if hit.get("source_class") == "supporting_derived_overlay"
        ),
        None,
    )
    if overlay_index is not None:
        visible_hits[overlay_index] = primary_truth_hit
    else:
        visible_hits = [
            hit
            for hit in visible_hits
            if hit.get("source_class") != "supporting_derived_overlay"
        ]
    return visible_hits


def truth_surface_hit(
    *,
    hits: list[dict],
    operation_class: str,
) -> dict | None:
    if not hits:
        return None
    if operation_class == "read_current_status":
        for hit in hits:
            if hit.get("source_class") == "current_truth" and hit.get("active_source"):
                return hit
        for hit in hits:
            if hit_is_primary_truth_candidate(hit, operation_class=operation_class):
                return hit
        return None
    for hit in hits:
        if hit_is_primary_truth_candidate(hit, operation_class=operation_class):
            return hit
    return None


def risk_freshness_note_for_query(
    *,
    freshness: dict,
    conflict_state: str,
    continuity_state: str,
    update_protocol_present: bool,
) -> str | None:
    notes: list[str] = []
    if continuity_state == "initialized_empty_shell":
        notes.append("Seed rolling_summary.md with real state before relying on continuity recall.")
    elif conflict_state == "multi_source_review_recommended":
        notes.append("Multiple sources tie for the top answer. Review supporting citations before trusting recall for writes.")
    elif conflict_state != "none":
        notes.append(
            f"{conflict_state}. Review current workspace state before trusting this recall for writes."
        )

    freshness_risk = freshness_risk_summary(
        workspace_artifact_scan_mode=freshness["workspace_artifact_scan_mode"],
        workspace_artifact_scan_performed=freshness["workspace_artifact_scan_performed"],
        workspace_artifact_newer_than_summary=freshness["workspace_artifact_newer_than_summary"],
        summary_revision_stale=freshness["summary_revision_stale"],
        continuity_confidence=freshness["continuity_confidence"],
    )
    if freshness_risk["note"] and freshness_risk["note"] not in notes:
        notes.append(freshness_risk["note"])

    if update_protocol_present:
        notes.append("Review update_protocol.md before turning continuity recall into a write decision.")

    return " ".join(notes) if notes else None


def attach_scan_text_surface(
    *,
    synthesized_recall: str,
    hits: list[dict],
    supporting_window: list[dict],
) -> str:
    parts = [synthesized_recall]
    for item in hits:
        parts.append(item["excerpt"])
    for item in supporting_window:
        parts.append(item["excerpt"])
    return "\n".join(part for part in parts if part.strip())


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

    if args.limit < 1:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message="--limit must be >= 1",
            payload=cli_failure_payload("invalid_prepared_input", error="--limit must be >= 1"),
        )

    query_analysis = analyze_query(args.query)
    query_terms = query_analysis["terms"]
    if not query_terms:
        error_messages = {
            "empty_query": "Query is empty. Provide a search question or phrase.",
            "query_too_short": "Query is too short. Add at least one meaningful English token or a two-character Chinese phrase.",
            "no_searchable_fragments": "Query does not contain any searchable fragments.",
        }
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=error_messages[query_analysis["error_kind"]],
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=error_messages[query_analysis["error_kind"]],
            ),
        )
    full_query = args.query.strip().casefold()

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
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload(
                "no_project_root",
                error="No RecallLoom project root found.",
                details={"project_root": str(Path(args.path).expanduser().resolve())},
            ),
        )
    startup_residue_report = exit_if_startup_scratch_residue(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
    )

    summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
    context_brief_path = workspace.storage_root / FILE_KEYS["context_brief"]
    state_path = workspace.storage_root / FILE_KEYS["state"]
    update_protocol_path = workspace.storage_root / FILE_KEYS["update_protocol"]
    derived_overlay_path = workspace.storage_root / DERIVED_OVERLAY_REL_PATH
    logs_dir = workspace.storage_root / DAILY_LOGS_DIRNAME

    try:
        invalid_daily_logs = invalid_iso_like_daily_log_files(logs_dir)
        if invalid_daily_logs:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=(
                    "Refusing query because one or more daily log filenames match the date pattern but are invalid ISO dates:\n"
                    + "\n".join(str(path) for path in invalid_daily_logs)
                ),
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=(
                        "Refusing query because one or more daily log filenames match the date pattern but are invalid ISO dates:\n"
                        + "\n".join(str(path) for path in invalid_daily_logs)
                    ),
                ),
            )
        state = load_workspace_state(state_path)
        summary_text = read_text(summary_path)
        summary_state = parse_file_state_marker(summary_text)
        if summary_state is None:
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=f"Missing required file-state metadata marker: {summary_path}",
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=f"Missing required file-state metadata marker: {summary_path}",
                ),
            )
        context_brief_included = query_analysis["intent"] == "background" or args.mode == "detailed"
        if context_brief_included and context_brief_path.is_file():
            context_brief_state = parse_file_state_marker(read_text(context_brief_path))
            if context_brief_state is None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Missing required file-state metadata marker: {context_brief_path}",
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=f"Missing required file-state metadata marker: {context_brief_path}",
                    ),
                )
        if update_protocol_path.is_file():
            update_protocol_state = parse_file_state_marker(read_text(update_protocol_path))
            if update_protocol_state is None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=f"Missing required file-state metadata marker: {update_protocol_path}",
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=f"Missing required file-state metadata marker: {update_protocol_path}",
                    ),
                )
        latest_daily_log = latest_active_daily_log(logs_dir)
        latest_daily_log_text = read_text(latest_daily_log) if latest_daily_log is not None else ""
        if latest_daily_log is not None and not any(
            parse_daily_log_entry_line(line) is not None
            for line in latest_daily_log_text.splitlines()
        ):
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
                ),
            )
        scan_mode = "full" if args.full else "quick"
        freshness = evaluate_continuity_freshness(
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
            summary_path=summary_path,
            workspace_revision=state["workspace_revision"],
            summary_base_workspace_revision=summary_state.base_workspace_revision,
            latest_daily_log_exists=latest_daily_log is not None,
            scan_mode=scan_mode,
        )
        continuity_state, continuity_seeded = continuity_state_for_workspace(
            state=state,
            summary_text=summary_text,
            latest_daily_log_exists=latest_daily_log is not None,
        )
    except (OSError, UnicodeDecodeError, KeyError, ConfigContractError) as exc:
        message = f"Filesystem/state error: {exc}"
        if isinstance(exc, ConfigContractError):
            failure_contract = cli_failure_payload(
                getattr(exc, "failure_reason", None) or "damaged_sidecar",
                error=message,
            )
        else:
            failure_contract = cli_failure_payload("damaged_sidecar", error=message)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=failure_contract,
        )

    continuity_has_seeded_state = continuity_state != "initialized_empty_shell"
    sources_considered: list[dict] = [
        {
            "path": str(summary_path),
            "source_type": "rolling_summary",
            "included": continuity_has_seeded_state,
        },
        {
            "path": str(context_brief_path),
            "source_type": "context_brief",
            "included": continuity_has_seeded_state and context_brief_included and context_brief_path.is_file(),
            "do_not_read_by_default": True,
            "reason": "context_brief is reserved for background/deep review, not fast current-status reads.",
        },
        {"path": str(update_protocol_path), "source_type": "update_protocol", "included": update_protocol_path.is_file()},
        {
            "path": str(derived_overlay_path),
            "source_type": "derived_overlay",
            "included": False,
            "derived_overlay_read": derived_overlay_path.exists(),
            "do_not_promote_to_current": True,
            "reason": "derived overlay is optional supporting evidence and cannot become current truth.",
        },
    ]

    hits: list[dict] = []
    if continuity_has_seeded_state:
        hits.extend(
            gather_file_hits(
                path=summary_path,
                source_type="rolling_summary",
                query_terms=query_terms,
                full_query=full_query,
            )
        )
    if continuity_has_seeded_state and context_brief_included and context_brief_path.is_file():
        hits.extend(
            gather_file_hits(
                path=context_brief_path,
                source_type="context_brief",
                query_terms=query_terms,
                full_query=full_query,
            )
        )

    if continuity_has_seeded_state and latest_daily_log is not None:
        sources_considered.append(
            {"path": str(latest_daily_log), "source_type": "latest_daily_log", "included": True}
        )
        hits.extend(
            gather_daily_log_hits(
                path=latest_daily_log,
                source_type="latest_daily_log",
                query_terms=query_terms,
                full_query=full_query,
                text=latest_daily_log_text,
            )
        )

    recent_daily_logs_included = False
    if continuity_has_seeded_state and args.include_daily_logs:
        recent_logs = sorted_daily_log_files(logs_dir)[-3:]
        for log_path in recent_logs:
            if latest_daily_log is not None and log_path == latest_daily_log:
                continue
            recent_daily_logs_included = True
            sources_considered.append(
                {"path": str(log_path), "source_type": "recent_daily_log", "included": True}
            )
            log_text = read_text(log_path)
            hits.extend(
                gather_daily_log_hits(
                    path=log_path,
                    source_type="recent_daily_log",
                    query_terms=query_terms,
                    full_query=full_query,
                    text=log_text,
                )
            )

    sorted_hits = sort_hits(hits, query_intent=query_analysis["intent"])
    truth_candidate_hits = [
        annotate_hit_truth_metadata(
            item,
            active_source=source_class_for_hit(item["source_type"], item.get("section")) == "current_truth",
        )
        for item in sorted_hits
    ]
    operation_metadata = operation_metadata_for_query(
        query_intent=query_analysis["intent"],
        include_daily_logs=args.include_daily_logs,
        context_brief_included=context_brief_included,
        recent_daily_logs_included=recent_daily_logs_included,
        derived_overlay_read=derived_overlay_path.exists(),
    )
    context_risk_review = context_risk_review_for_query(
        context_brief_included=context_brief_included,
    )
    primary_truth_hit = truth_surface_hit(
        hits=truth_candidate_hits,
        operation_class=operation_metadata["operation_class"],
    )
    derived_overlay_review = load_derived_overlay_review(
        path=derived_overlay_path,
        summary_revision=summary_state.revision,
        query_terms=query_terms,
        primary_truth_hit=primary_truth_hit,
    )
    if derived_overlay_review["status"] == "conflict":
        public_review = {
            **derived_overlay_review,
            "path": public_project_path(
                derived_overlay_review["path"],
                project_root=workspace.project_root,
            ),
        }
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=7,
            message="Derived overlay conflicts with rolling_summary current truth.",
            payload=cli_failure_payload(
                "derived_overlay_conflict",
                error="Derived overlay conflicts with rolling_summary current truth.",
                details={
                    "project_root": str(workspace.project_root),
                    "path": str(derived_overlay_path),
                    "reason_code": "derived_overlay_conflict",
                    "fallback_source": DERIVED_OVERLAY_FALLBACK_SOURCE,
                },
                extra={
                    "reason_code": "derived_overlay_conflict",
                    "derived_overlay_review": public_review,
                },
            ),
        )
    if derived_overlay_review.get("included"):
        sources_considered[-1]["included"] = True
        hits.extend(derived_overlay_review.get("supporting_hits", []))
        sorted_hits = sort_hits(hits, query_intent=query_analysis["intent"])
        truth_candidate_hits = [
            annotate_hit_truth_metadata(
                item,
                active_source=source_class_for_hit(item["source_type"], item.get("section")) == "current_truth",
            )
            for item in sorted_hits
        ]
        primary_truth_hit = truth_surface_hit(
            hits=truth_candidate_hits,
            operation_class=operation_metadata["operation_class"],
        )
        operation_metadata = operation_metadata_for_query(
            query_intent=query_analysis["intent"],
            include_daily_logs=args.include_daily_logs,
            context_brief_included=context_brief_included,
            recent_daily_logs_included=recent_daily_logs_included,
            derived_overlay_read=derived_overlay_review.get("derived_overlay_read", True),
        )
    conflict_state = conflict_state_for_hits(
        freshness=freshness,
        hits=[
            item
            for item in truth_candidate_hits
            if item.get("source_class") != "supporting_derived_overlay"
        ],
        continuity_state=continuity_state,
    )
    surface_hits = surface_hits_for_query(
        truth_candidate_hits,
        limit=args.limit,
        primary_truth_hit=primary_truth_hit,
    )

    citations = [
        {
            "path": item["path"],
            "section": item["section"],
            "source_type": item["source_type"],
            "date": citation_date(item["path"], item["source_type"]),
        }
        for item in surface_hits
    ]
    public_hit_list = public_hits(surface_hits)
    support_window = supporting_context_window(public_hit_list, mode=args.mode)
    answer_hits = [primary_truth_hit] if primary_truth_hit is not None else []
    answer = answer_for_query(hits=answer_hits, continuity_state=continuity_state)
    risk_freshness_note = risk_freshness_note_for_query(
        freshness=freshness,
        conflict_state=conflict_state,
        continuity_state=continuity_state,
        update_protocol_present=update_protocol_path.is_file(),
    )
    synthesized_recall = render_synthesized_recall(
        query=args.query,
        answer=answer,
        citations=citations,
        hits=public_hit_list,
        mode=args.mode,
        risk_freshness_note=risk_freshness_note,
    )

    attach_scan = scan_auto_attached_context_text(
        attach_scan_text_surface(
            synthesized_recall=synthesized_recall,
            hits=public_hit_list,
            supporting_window=support_window,
        )
    )
    if attach_scan["blocked"]:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=(
                "Refusing to return attach-safe continuity text because the synthesized recall "
                "failed the attached-text safety scan: "
                + ", ".join(attach_scan["hard_block_reasons"])
            ),
            payload=cli_failure_payload(
                "attach_scan_blocked",
                error=(
                    "Refusing to return attach-safe continuity text because the synthesized recall "
                    "failed the attached-text safety scan: "
                    + ", ".join(attach_scan["hard_block_reasons"])
                ),
                details={"hard_block_reasons": attach_scan["hard_block_reasons"]},
            ),
        )

    estimate = token_estimate(
        attach_scan_text_surface(
            synthesized_recall=synthesized_recall,
            hits=[],
            supporting_window=support_window if args.mode == "detailed" else [],
        )
    )
    trust_state = evaluate_trust_state(
        continuity_confidence=freshness["continuity_confidence"],
        continuity_state=continuity_state,
        summary_stale=freshness["summary_stale"],
        workspace_newer_than_summary=freshness["workspace_newer_than_summary"],
        conflict_state=conflict_state,
    )
    public_project_root = public_project_root_label(workspace.project_root)
    public_storage_root = public_project_path(workspace.storage_root, project_root=workspace.project_root)
    public_update_protocol_path = public_project_path(update_protocol_path, project_root=workspace.project_root)
    public_latest_daily_log = (
        public_project_path(latest_daily_log, project_root=workspace.project_root)
        if latest_daily_log is not None
        else None
    )
    public_latest_workspace_artifact = (
        public_project_path(freshness["latest_workspace_artifact"], project_root=workspace.project_root)
        if freshness["latest_workspace_artifact"] is not None
        else None
    )
    public_sources_considered = [
        {**item, "path": public_project_path(item["path"], project_root=workspace.project_root)}
        for item in sources_considered
    ]
    public_hit_list = publicize_public_hit_paths(
        public_hit_list,
        project_root=workspace.project_root,
    )
    public_truth_candidate_hits = publicize_public_hit_paths(
        public_hits(truth_candidate_hits),
        project_root=workspace.project_root,
    )
    citations = [
        {**item, "path": public_project_path(item["path"], project_root=workspace.project_root)}
        for item in citations
    ]
    citations = [
        {
            **item,
            "source_class": source_class_for_hit(item["source_type"], item.get("section")),
            "do_not_promote_to_current": item["source_type"]
            in {"latest_daily_log", "recent_daily_log", "derived_overlay"},
            "do_not_use_as_current_fact": item["source_type"] == "derived_overlay",
        }
        for item in citations
    ]
    support_window = [
        {**item, "path": public_project_path(item["path"], project_root=workspace.project_root)}
        for item in support_window
    ]
    read_set_evidence = read_set_evidence_for_query(
        operation_metadata=operation_metadata,
        sources_considered=public_sources_considered,
        context_brief_included=context_brief_included,
        derived_overlay_review={
            **derived_overlay_review,
            "path": public_project_path(
                derived_overlay_review["path"],
                project_root=workspace.project_root,
            ),
            "supporting_hits": publicize_public_hit_paths(
                public_hits(
                    [
                        annotate_hit_truth_metadata(item, active_source=False)
                        for item in derived_overlay_review.get("supporting_hits", [])
                    ]
                ),
                project_root=workspace.project_root,
            ),
        },
        include_daily_logs=args.include_daily_logs,
        recent_daily_logs_included=recent_daily_logs_included,
        scan_mode=freshness["workspace_artifact_scan_mode"],
    )
    historical_evidence = historical_evidence_for_hits(
        public_truth_candidate_hits,
        operation_class=operation_metadata["operation_class"],
        primary_truth_source_class=(
            primary_truth_hit.get("source_class") if primary_truth_hit is not None else None
        ),
    )
    ux_verdict = ux_verdict_for_query(
        operation_metadata=operation_metadata,
        primary_truth_hit=primary_truth_hit,
        read_set_evidence=read_set_evidence,
        historical_evidence=historical_evidence,
    )
    recall_accuracy_evidence = recall_accuracy_evidence_for_query(
        operation_metadata=operation_metadata,
        primary_truth_hit=primary_truth_hit,
        hits=truth_candidate_hits,
        project_root=workspace.project_root,
    )
    payload = {
        "schema_version": "1.1",
        "project_root": public_project_root,
        "storage_root": public_storage_root,
        "continuity_confidence": freshness["continuity_confidence"],
        "sidecar_trust_state": trust_state["sidecar_trust_state"],
        "allowed_operation_level": trust_state["allowed_operation_level"],
        "continuity_drift_risk_level": trust_state["continuity_drift_risk_level"],
        "continuity_state": continuity_state,
        "continuity_seeded": continuity_seeded,
        "query": args.query,
        "query_interpretation": {
            "intent": query_analysis["intent"],
            "matched_keywords": query_analysis["matched_keywords"],
            "terms": query_terms,
        },
        "operation_metadata": operation_metadata,
        "ux_verdict": ux_verdict,
        "read_set_evidence": read_set_evidence,
        "historical_evidence": historical_evidence,
        "recall_accuracy_evidence": recall_accuracy_evidence,
        "context_risk_review": context_risk_review,
        "derived_overlay_review": read_set_evidence["derived_overlay_review"],
        "answer": answer,
        "risk_freshness_note": risk_freshness_note,
        "token_estimate": estimate,
        "budget_hint": budget_hint(estimate),
        "output_variant": output_variant_for_mode(args.mode),
        "sources_considered": public_sources_considered,
        "override_review_targets": (
            [
                {
                    "path": public_update_protocol_path,
                    "reason": "review_update_protocol_before_write",
                }
            ]
            if update_protocol_path.is_file()
            else []
        ),
        "hits": public_hit_list,
        "synthesized_recall": synthesized_recall,
        "citations": citations,
        "supporting_context_window": support_window,
        "continuity_snapshot": {
            "workspace_revision_seen": state["workspace_revision"],
            "rolling_summary_revision_seen": summary_state.revision,
            "latest_active_daily_log_seen": public_latest_daily_log,
            "latest_workspace_artifact_seen": public_latest_workspace_artifact,
            "continuity_confidence": freshness["continuity_confidence"],
            "continuity_state": continuity_state,
            "continuity_seeded": continuity_seeded,
            "task_type": "query_continuity",
        },
        "source_type": "core_continuity_only",
        "source_class": primary_truth_hit["source_class"] if primary_truth_hit is not None else "no_match",
        "active_source": primary_truth_hit["active_source"] if primary_truth_hit is not None else False,
        "active_boundary": primary_truth_hit["active_boundary"] if primary_truth_hit is not None else None,
        "do_not_promote_to_current": (
            primary_truth_hit["do_not_promote_to_current"] if primary_truth_hit is not None else True
        ),
        "do_not_use_as_current_fact": (
            primary_truth_hit["do_not_use_as_current_fact"] if primary_truth_hit is not None else True
        ),
        "confidence": confidence_for_hits(
            freshness["continuity_confidence"],
            answer_hits,
            continuity_state=continuity_state,
            conflict_state=conflict_state,
            query_terms=query_terms,
        ),
        "freshness_state": {
            "workspace_artifact_scan_mode": freshness["workspace_artifact_scan_mode"],
            "workspace_artifact_scan_performed": freshness["workspace_artifact_scan_performed"],
            "latest_workspace_artifact": public_latest_workspace_artifact,
            "workspace_artifact_newer_than_summary": freshness["workspace_artifact_newer_than_summary"],
            "summary_revision_stale": freshness["summary_revision_stale"],
            "workspace_newer_than_summary": freshness["workspace_newer_than_summary"],
            "freshness_risk_level": freshness_risk_summary(
                workspace_artifact_scan_mode=freshness["workspace_artifact_scan_mode"],
                workspace_artifact_scan_performed=freshness["workspace_artifact_scan_performed"],
                workspace_artifact_newer_than_summary=freshness["workspace_artifact_newer_than_summary"],
                summary_revision_stale=freshness["summary_revision_stale"],
                continuity_confidence=freshness["continuity_confidence"],
            )["level"],
            "freshness_risk_note": freshness_risk_summary(
                workspace_artifact_scan_mode=freshness["workspace_artifact_scan_mode"],
                workspace_artifact_scan_performed=freshness["workspace_artifact_scan_performed"],
                workspace_artifact_newer_than_summary=freshness["workspace_artifact_newer_than_summary"],
                summary_revision_stale=freshness["summary_revision_stale"],
                continuity_confidence=freshness["continuity_confidence"],
            )["note"],
            "read_freshness": freshness["read_freshness"],
        },
        "read_trust_state": {
            "read_confidence": trust_state["read_confidence"],
            "read_trust_note": trust_state["read_trust_note"],
        },
        "conflict_state": conflict_state,
        "attach_scan": attach_scan,
    }
    if startup_residue_report is not None:
        payload["startup_residue_report"] = startup_residue_report

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(synthesized_recall)


if __name__ == "__main__":
    main()
