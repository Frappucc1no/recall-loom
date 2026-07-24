"""Shared workday recommendation and write-tier guidance helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path


DEFAULT_LOGICAL_WORKDAY_ROLLOVER_HOUR = 3

RECOMMENDATION_TYPES = (
    "continue_active_day",
    "start_new_active_day",
    "start_new_day_with_carryover",
    "close_previous_day_then_start_new_day",
    "backfill_previous_day_closure",
    "log_not_needed_for_this_session",
    "review_date_before_append",
)

WORKDAY_STATES = (
    "no_active_day",
    "same_day_active",
    "new_day_after_closed_day",
    "new_day_carryover",
    "historical_backfill_requested",
    "date_review_required",
)

USER_VISIBLE_PROMPT_LEVELS = (
    "silent",
    "receipt_only",
    "confirm",
)

CLOSURE_KEYWORDS = (
    "end-of-day",
    "end of day",
    "day closed",
    "close day",
    "closure recorded",
    "wrapped for the day",
    "收工",
    "收尾",
    "结束今天",
    "今日收尾",
)

WRITE_TIER_DEFAULTS = {
    "stable_rule": {
        "file_key": "context_brief",
        "relative_path": "context_brief.md",
    },
    "current_state": {
        "file_key": "rolling_summary",
        "relative_path": "rolling_summary.md",
    },
    "milestone_evidence": {
        "file_key": "daily_log",
        "path_pattern": "daily_logs/{date}.md",
    },
}

WRITE_EXIT_MODES = {
    "no_write": {
        "write_required": False,
        "default_target": None,
    },
    "merge_current_state": {
        "write_required": True,
        "default_target": "current_state",
    },
    "append_milestone": {
        "write_required": True,
        "default_target": "milestone_evidence",
    },
    "defer": {
        "write_required": False,
        "default_target": None,
    },
    "confirm": {
        "write_required": False,
        "default_target": None,
    },
    "blocked": {
        "write_required": False,
        "default_target": None,
    },
}


def logical_workday_for(now: datetime, rollover_hour: int) -> date:
    return now.date() if now.hour >= rollover_hour else (now.date() - timedelta(days=1))


def detect_closure_signal(daily_log_text: str) -> tuple[bool, list[str]]:
    lowered = daily_log_text.lower()
    matches = [keyword for keyword in CLOSURE_KEYWORDS if keyword in lowered]
    return (len(matches) > 0, matches)


def build_workday_decision(
    *,
    now: datetime,
    rollover_hour: int,
    latest_active_day: date | None,
    closure_detected: bool,
    summary_next_step_is_empty: bool,
    preferred_date: date | None,
    session_intent: str | None,
    project_time_policy_cues: list[str],
    host_explicit: bool,
) -> dict:
    logical_workday = logical_workday_for(now, rollover_hour)
    physical_date = now.date()
    suggested_date = logical_workday.isoformat()
    reasoning: list[str] = []
    project_time_policy_review_required = bool(project_time_policy_cues)

    if latest_active_day is None:
        workday_state = "no_active_day"
        recommendation = "start_new_active_day"
        suggested_date = logical_workday.isoformat()
        reasoning.append("No active daily log exists yet.")
    elif latest_active_day > logical_workday:
        workday_state = "date_review_required"
        recommendation = "review_date_before_append"
        suggested_date = latest_active_day.isoformat()
        reasoning.append(
            "Latest active day is ahead of the current logical workday; review the project date before appending."
        )
    elif latest_active_day == logical_workday:
        workday_state = "same_day_active"
        recommendation = "continue_active_day"
        suggested_date = latest_active_day.isoformat()
        reasoning.append("Latest active day matches the current logical workday.")
    elif closure_detected:
        workday_state = "new_day_after_closed_day"
        recommendation = "start_new_active_day"
        suggested_date = logical_workday.isoformat()
        reasoning.append("Latest active day appears closed based on closure language in the daily log.")
    else:
        workday_state = "new_day_carryover"
        recommendation = "start_new_day_with_carryover"
        suggested_date = logical_workday.isoformat()
        if summary_next_step_is_empty:
            reasoning.append(
                "Latest active day is behind the logical workday; continue on the new day without forcing a closure first."
            )
        else:
            reasoning.append(
                "Latest active day is behind the logical workday and the unfinished next step carries into the new day."
            )

    heuristic_workday_state = workday_state
    heuristic_recommendation = recommendation
    heuristic_suggested_date = suggested_date
    date_resolution_source = "heuristic_default"
    date_sensitive = latest_active_day is None or latest_active_day != logical_workday

    if session_intent is not None:
        recommendation = session_intent
        reasoning.append(
            f"Using the explicit session intent {session_intent} instead of the heuristic recommendation {heuristic_recommendation}."
        )
        if session_intent == "continue_active_day" and latest_active_day is not None:
            suggested_date = latest_active_day.isoformat()
        elif session_intent == "backfill_previous_day_closure":
            workday_state = "historical_backfill_requested"
            suggested_date = (
                latest_active_day.isoformat() if latest_active_day is not None else logical_workday.isoformat()
            )
        elif session_intent in {
            "start_new_active_day",
            "start_new_day_with_carryover",
            "close_previous_day_then_start_new_day",
        }:
            suggested_date = logical_workday.isoformat()
        date_resolution_source = "user_explicit"

    if preferred_date is not None:
        preferred_date_iso = preferred_date.isoformat()
        date_resolution_source = "user_explicit"
        if session_intent == "backfill_previous_day_closure":
            workday_state = "historical_backfill_requested"
            recommendation = "backfill_previous_day_closure"
            suggested_date = preferred_date_iso
            reasoning.append(f"Using the explicit preferred date {preferred_date_iso} for historical backfill.")
        elif preferred_date_iso != heuristic_suggested_date:
            recommendation = "review_date_before_append"
            workday_state = "date_review_required"
            suggested_date = preferred_date_iso
            reasoning.append(
                f"Using the explicit preferred date {preferred_date_iso} instead of the heuristic suggestion {heuristic_suggested_date}."
            )
        else:
            suggested_date = preferred_date_iso
            reasoning.append(f"Explicit preferred date {preferred_date_iso} matches the heuristic suggestion.")
    elif project_time_policy_review_required and date_sensitive:
        recommendation = "review_date_before_append"
        workday_state = "date_review_required"
        date_resolution_source = "project_local_review"
        reasoning.append(
            "Project-local time-policy cues were detected in update_protocol.md; review them before applying the heuristic date suggestion."
        )

    if (
        project_time_policy_review_required
        and date_sensitive
        and session_intent != "backfill_previous_day_closure"
    ):
        recommendation = "review_date_before_append"
        workday_state = "date_review_required"
        date_resolution_source = "project_local_review"
        if preferred_date is not None:
            reasoning.append(
                "Project-local time-policy review remains required even when an explicit preferred date matches the heuristic suggestion."
            )

    if project_time_policy_review_required and date_sensitive and session_intent != "backfill_previous_day_closure":
        decision_priority_applied = "project_local_review"
    elif preferred_date is not None:
        decision_priority_applied = "user_explicit"
    elif session_intent is not None:
        decision_priority_applied = "user_explicit"
    elif project_time_policy_review_required and date_sensitive:
        decision_priority_applied = "project_local_review"
    elif host_explicit:
        decision_priority_applied = "host_explicit"
    else:
        decision_priority_applied = "host_default"

    requires_user_confirmation = workday_state in {"historical_backfill_requested", "date_review_required"} or (
        recommendation == "close_previous_day_then_start_new_day"
    )
    if requires_user_confirmation:
        user_visible_prompt_level = "confirm"
    elif workday_state in {"new_day_after_closed_day", "new_day_carryover"}:
        user_visible_prompt_level = "receipt_only"
    else:
        user_visible_prompt_level = "silent"

    gap_days = None
    if latest_active_day is not None and latest_active_day <= logical_workday:
        gap_days = (logical_workday - latest_active_day).days

    return {
        "physical_date": physical_date.isoformat(),
        "logical_workday": logical_workday.isoformat(),
        "latest_active_day": latest_active_day.isoformat() if latest_active_day else None,
        "gap_days": gap_days,
        "closure_detected": closure_detected,
        "summary_next_step_empty": summary_next_step_is_empty,
        "session_intent": session_intent,
        "workday_state": workday_state,
        "heuristic_workday_state": heuristic_workday_state,
        "recommendation_type": recommendation,
        "heuristic_recommendation_type": heuristic_recommendation,
        "heuristic_suggested_date": heuristic_suggested_date,
        "suggested_date": suggested_date,
        "date_resolution_source": date_resolution_source,
        "decision_priority_applied": decision_priority_applied,
        "requires_user_confirmation": requires_user_confirmation,
        "user_visible_prompt_level": user_visible_prompt_level,
        "reasoning": reasoning,
    }


def describe_workday_guidance(decision: dict, *, always_show: bool) -> str | None:
    if not always_show and decision.get("user_visible_prompt_level") == "silent":
        return None

    state = decision.get("workday_state")
    suggested_date = decision.get("suggested_date")
    latest_active_day = decision.get("latest_active_day")
    recommendation = decision.get("recommendation_type")

    if recommendation == "review_date_before_append":
        return f"Review the date before appending. Suggested date: {suggested_date}."
    if state == "historical_backfill_requested":
        return f"Historical backfill was requested for {suggested_date}; confirm the date before appending."
    if state == "new_day_carryover":
        return (
            f"Last active day was {latest_active_day}. Continue on {suggested_date}; "
            "the previous day does not need explicit closure first."
        )
    if state == "new_day_after_closed_day":
        return f"Last active day {latest_active_day} appears closed; start a new day on {suggested_date}."
    if state == "same_day_active":
        return f"Continue the current active day on {suggested_date}."
    if state == "no_active_day":
        return f"Start a new active day on {suggested_date} when you need a milestone entry."
    if recommendation == "log_not_needed_for_this_session":
        return "No daily-log update is needed for this session."
    if suggestion := decision.get("suggested_date"):
        return f"Suggested date: {suggestion}."
    return None


def build_write_tier_judgment(*, project_root: Path, storage_root: Path) -> dict:
    storage_root_relative = storage_root.relative_to(project_root)
    default_targets = {
        "stable_rule": {
            "file_key": WRITE_TIER_DEFAULTS["stable_rule"]["file_key"],
            "path": str((storage_root_relative / WRITE_TIER_DEFAULTS["stable_rule"]["relative_path"]).as_posix()),
        },
        "current_state": {
            "file_key": WRITE_TIER_DEFAULTS["current_state"]["file_key"],
            "path": str((storage_root_relative / WRITE_TIER_DEFAULTS["current_state"]["relative_path"]).as_posix()),
        },
        "milestone_evidence": {
            "file_key": WRITE_TIER_DEFAULTS["milestone_evidence"]["file_key"],
            "path_pattern": str(
                (storage_root_relative / WRITE_TIER_DEFAULTS["milestone_evidence"]["path_pattern"]).as_posix()
            ),
        },
    }
    return {
        "default_targets": default_targets,
        "default_exit_modes": WRITE_EXIT_MODES,
    }
