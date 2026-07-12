"""User-visible status categories shared by RecallLoom command surfaces."""

from __future__ import annotations

from typing import Mapping


USER_VISIBLE_CATEGORIES = frozenset(
    {
        "ready_to_record",
        "no_write_needed",
        "needs_confirmation",
        "review_required",
        "blocked",
        "diagnostic_only",
    }
)


def build_user_summary(
    *,
    category: str,
    conclusion: str,
    reason: str,
    next_step: str,
) -> dict:
    if category not in USER_VISIBLE_CATEGORIES:
        raise ValueError(f"Unsupported user-visible category: {category}")
    return {
        "category": category,
        "conclusion": conclusion,
        "reason": reason,
        "next_step": next_step,
    }


def print_user_summary(title: str, summary: Mapping[str, object]) -> None:
    print(title)
    print(f"{summary['category']}: {summary['conclusion']}")
    reason = summary.get("reason")
    if reason:
        print(f"Reason: {reason}")
    print(f"Next step: {summary['next_step']}")


def _strict_gate_blocks(summary: Mapping[str, object] | None) -> bool:
    if not isinstance(summary, Mapping):
        return False
    return summary.get("allowed_for_mutation") is False


def read_surface_user_summary(
    *,
    surface: str,
    summary_stale: bool = False,
    strict_sidecar_integrity_gate: Mapping[str, object] | None = None,
    continuity_drift_risk_level: str | None = None,
    continuity_state: str | None = None,
    no_project: bool = False,
) -> dict:
    if _strict_gate_blocks(strict_sidecar_integrity_gate):
        return build_user_summary(
            category="blocked",
            conclusion="Mutation is blocked.",
            reason="Managed sidecar integrity is not safe for writes; read-only diagnostics may still be available.",
            next_step="Run validation or the supported repair path before writing.",
        )
    if summary_stale or continuity_drift_risk_level in {"medium", "high"}:
        return build_user_summary(
            category="review_required",
            conclusion="Review required before writing.",
            reason="Continuity is readable, but current state needs review before any mutation.",
            next_step="Review or refresh current state before writing.",
        )
    if continuity_state == "initialized_empty_shell":
        return build_user_summary(
            category="needs_confirmation",
            conclusion="Initial continuity needs seeding.",
            reason="The RecallLoom sidecar exists but does not yet contain durable project state.",
            next_step="Confirm the initial project summary before writing.",
        )
    if no_project:
        return build_user_summary(
            category="diagnostic_only",
            conclusion="No RecallLoom project is attached.",
            reason="This result only identifies that no sidecar was found.",
            next_step="Initialize RecallLoom for this project if continuity should be tracked.",
        )
    return build_user_summary(
        category="diagnostic_only",
        conclusion=f"{surface} check complete.",
        reason="This read-only result describes state; it is not a write authorization.",
        next_step="Continue from the reported current state or take the relevant next action.",
    )


def validate_user_summary(*, error_count: int, warning_count: int) -> dict:
    if error_count > 0:
        return build_user_summary(
            category="blocked",
            conclusion="Validation failed.",
            reason="RecallLoom found structural errors that block safe mutation.",
            next_step="Fix the validation errors or run the supported repair path before writing.",
        )
    if warning_count > 0:
        return build_user_summary(
            category="review_required",
            conclusion="Validation passed with warnings.",
            reason="The sidecar is readable, but warnings need review before treating it as clean.",
            next_step="Review the warnings before writing.",
        )
    return build_user_summary(
        category="diagnostic_only",
        conclusion="Validation passed.",
        reason="Structural validation is diagnostic; it does not authorize a write by itself.",
        next_step="Continue or run preflight before any managed write.",
    )


def preflight_user_summary(payload: Mapping[str, object]) -> dict:
    strict_gate = payload.get("strict_sidecar_integrity_gate")
    if _strict_gate_blocks(strict_gate if isinstance(strict_gate, Mapping) else None):
        return build_user_summary(
            category="blocked",
            conclusion="Preflight blocks mutation.",
            reason="Strict sidecar integrity does not allow managed writes.",
            next_step="Run validation or the supported repair path before writing.",
        )
    blocked_reason = payload.get("write_context_blocked_reason")
    if isinstance(blocked_reason, str) and blocked_reason:
        return build_user_summary(
            category="review_required",
            conclusion="Review required before writing.",
            reason="Preflight did not produce a safe write context for this state.",
            next_step="Review current state or rerun preflight after resolving the blocker.",
        )
    if payload.get("summary_stale") is True:
        return build_user_summary(
            category="review_required",
            conclusion="Current state needs review.",
            reason="The workspace is newer than rolling_summary.md.",
            next_step="Review or refresh the current-state summary before writing.",
        )
    return build_user_summary(
        category="diagnostic_only",
        conclusion="Preflight complete.",
        reason="Revision-bound write context may be available, but this diagnostic result has no side effect.",
        next_step="Use the bound action if a managed write is still intended.",
    )
