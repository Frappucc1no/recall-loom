"""Side-effect-free proactive recording suggestions."""

from __future__ import annotations

from collections.abc import Mapping

from core.output.privacy import redact_public_text

from .contracts import digest_payload, normalize_record_plan_input
from .privacy import contains_unsafe_record_text, record_input_text


MILESTONE_TOKENS = (
    "approved",
    "authorized",
    "completed",
    "decided",
    "fixed",
    "implemented",
    "released",
    "resolved",
    "reviewed",
    "shipped",
    "validated",
    "验证通过",
    "修复",
    "决定",
    "完成",
    "实现",
    "已授权",
    "已发布",
    "已验证",
    "已评审",
)

STABLE_RULE_TOKENS = (
    "always",
    "must",
    "policy",
    "rule",
    "source of truth",
    "以后",
    "必须",
    "规则",
    "规范",
    "长期",
)

NO_PROMPT_TOKENS = (
    "do not record",
    "do not log",
    "do not save",
    "don't record",
    "don't log",
    "don't save",
    "no need to record",
    "no record needed",
    "不要记录",
    "不用记录",
    "无需记录",
    "不需要记录",
)

UNSTABLE_TOKENS = (
    "brainstorm",
    "draft",
    "maybe",
    "not sure",
    "rough idea",
    "thinking aloud",
    "探索",
    "草稿",
    "还没定",
    "不确定",
)

UNSAFE_PRIVACY_CLASSES = {
    "blocked",
    "contains_sensitive",
    "private",
    "sensitive",
    "unsafe",
}


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _privacy_classification(input_contract: Mapping[str, object]) -> str:
    privacy = input_contract.get("privacy_safety_result")
    if not isinstance(privacy, Mapping):
        return "unknown"
    value = privacy.get("classification") or privacy.get("status") or privacy.get("safety_status")
    return value.strip().casefold().replace("-", "_") if isinstance(value, str) else "unknown"


def _summary_from_text(
    text: str,
    *,
    unsafe: bool,
    max_chars: int = 220,
) -> tuple[str | None, bool]:
    if not text.strip():
        return None, False
    if unsafe:
        return f"redacted-candidate:{digest_payload({'candidate_text': text})}", True
    public_text = redact_public_text(text, project_root=None) or ""
    redacted = public_text != text
    compacted = " ".join(public_text.split())
    if not compacted:
        return None, redacted
    if len(compacted) <= max_chars:
        return compacted, redacted
    return compacted[: max_chars - 3].rstrip(" ,;:|/-") + "...", redacted


def _user_summary(
    *,
    category: str,
    conclusion: str,
    reason: str,
    next_step: str,
) -> dict[str, str]:
    return {
        "category": category,
        "conclusion": conclusion,
        "reason": reason,
        "next_step": next_step,
    }


def _suggested_record_class(text: str) -> tuple[str, str]:
    if _contains_any(text, STABLE_RULE_TOKENS):
        return "stable_context_update", "stable_context"
    return "append_only", "daily_log"


def build_recording_suggestion(input_contract: Mapping[str, object]) -> dict[str, object]:
    """Build a no-write recording suggestion from public-safe candidate material."""

    normalized = normalize_record_plan_input(input_contract)
    intent_text = normalized.get("intent_text") or ""
    if not isinstance(intent_text, str):
        intent_text = str(intent_text)
    lowered = intent_text.casefold()
    combined_text, _payload_present = record_input_text(input_contract)
    privacy_classification = _privacy_classification(normalized)
    unsafe = (
        privacy_classification in UNSAFE_PRIVACY_CLASSES
        or contains_unsafe_record_text(combined_text)
    )
    if unsafe:
        candidate_summary, candidate_summary_redacted = _summary_from_text(
            combined_text,
            unsafe=True,
        )
        user_summary = _user_summary(
            category="blocked",
            conclusion="No recording suggestion.",
            reason="The candidate text looks sensitive or private and cannot be suggested as-is.",
            next_step="Remove or redact the sensitive material before asking for a recording suggestion.",
        )
        return {
            "ok": False,
            "schema_version": "1.1",
            "command": "record",
            "mode": "suggest",
            "side_effect": "none",
            "user_visible_category": user_summary["category"],
            "user_summary": user_summary,
            "suggestion_status": "blocked",
            "should_prompt": False,
            "candidate_summary": candidate_summary,
            "candidate_summary_redacted": candidate_summary_redacted,
            "suggested_record_class": None,
            "suggested_layer": None,
            "suggested_path": None,
            "privacy_safety_classification": privacy_classification,
            "safety_notes": [
                "record --suggest is side-effect-free",
                "sensitive candidate material is never converted into a write path",
            ],
        }
    if _contains_any(lowered, NO_PROMPT_TOKENS):
        user_summary = _user_summary(
            category="no_write_needed",
            conclusion="No recording prompt needed.",
            reason="The candidate explicitly says not to record or save this.",
            next_step="Stop; do not suggest or prepare a RecallLoom write.",
        )
        return {
            "ok": True,
            "schema_version": "1.1",
            "command": "record",
            "mode": "suggest",
            "side_effect": "none",
            "user_visible_category": user_summary["category"],
            "user_summary": user_summary,
            "suggestion_status": "silent",
            "should_prompt": False,
            "candidate_summary": None,
            "candidate_summary_redacted": False,
            "suggested_record_class": None,
            "suggested_layer": None,
            "suggested_path": None,
            "privacy_safety_classification": privacy_classification,
            "safety_notes": ["record --suggest is side-effect-free"],
        }
    if _contains_any(lowered, UNSTABLE_TOKENS) or not _contains_any(lowered, MILESTONE_TOKENS):
        user_summary = _user_summary(
            category="diagnostic_only",
            conclusion="No proactive prompt.",
            reason="The candidate does not look like a durable milestone, decision, validation, or release fact yet.",
            next_step="Continue working; ask for record --plan only after a durable fact exists.",
        )
        return {
            "ok": True,
            "schema_version": "1.1",
            "command": "record",
            "mode": "suggest",
            "side_effect": "none",
            "user_visible_category": user_summary["category"],
            "user_summary": user_summary,
            "suggestion_status": "silent",
            "should_prompt": False,
            "candidate_summary": None,
            "candidate_summary_redacted": False,
            "suggested_record_class": None,
            "suggested_layer": None,
            "suggested_path": None,
            "privacy_safety_classification": privacy_classification,
            "safety_notes": ["record --suggest is side-effect-free"],
        }

    record_class, layer = _suggested_record_class(lowered)
    candidate_summary, candidate_summary_redacted = _summary_from_text(
        intent_text,
        unsafe=False,
    )
    user_summary = _user_summary(
        category="diagnostic_only",
        conclusion="Recording candidate found.",
        reason="The candidate looks like a durable milestone, decision, validation, or release fact.",
        next_step="Offer the sanitized candidate summary and run record --plan only if the user wants to record it.",
    )
    return {
        "ok": True,
        "schema_version": "1.1",
        "command": "record",
        "mode": "suggest",
        "side_effect": "none",
        "user_visible_category": user_summary["category"],
        "user_summary": user_summary,
        "suggestion_status": "candidate",
        "should_prompt": True,
        "candidate_summary": candidate_summary,
        "candidate_summary_redacted": candidate_summary_redacted,
        "suggested_record_class": record_class,
        "suggested_layer": layer,
        "suggested_path": (
            f"record --plan with record_class={record_class}, layer_hint={layer}, "
            "and the sanitized candidate summary"
        ),
        "privacy_safety_classification": privacy_classification,
        "safety_notes": [
            "record --suggest is side-effect-free",
            "a suggestion is not a write authorization",
            "record --plan must still run privacy, preflight, revision and receipt gates",
        ],
    }
