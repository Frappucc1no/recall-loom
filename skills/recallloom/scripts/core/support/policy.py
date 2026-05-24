"""Package support state and action-gating policy."""

from __future__ import annotations

from pathlib import Path
import re


PACKAGE_SUPPORT_STATES = {
    "supported",
    "upgrade_recommended",
    "readonly_only",
    "diagnostic_only",
    "unknown_offline",
}

ACTION_LEVELS = {"diagnostic", "readonly", "mutating"}

SCRIPT_ACTION_LEVELS = {
    "detect_project_root.py": "diagnostic",
    "validate_context.py": "diagnostic",
    "summarize_continuity_status.py": "diagnostic",
    "unlock_write_lock.py": "diagnostic",
    "sync_contract_docs.py": "mutating",
    "query_continuity.py": "readonly",
    "preflight_context_check.py": "readonly",
    "recommend_workday.py": "readonly",
    "generate_coldstart_proposal.py": "readonly",
    "prepare_recovery_promotion.py": "readonly",
    "init_context.py": "mutating",
    "manage_entry_bridge.py": "mutating",
    "commit_context_file.py": "mutating",
    "append_daily_log_entry.py": "mutating",
    "archive_logs.py": "mutating",
    "stage_recovery_proposal.py": "mutating",
    "record_recovery_review.py": "mutating",
    "remove_context.py": "mutating",
    "install_native_commands.py": "mutating",
}

DISPATCHER_ACTION_LEVELS = {
    "validate": "diagnostic",
    "status": "diagnostic",
    "resume": "readonly",
    "init": "mutating",
    "bridge": "mutating",
}

SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)*$")


def parse_version(value: str | None) -> tuple[int, ...]:
    if not isinstance(value, str) or not SEMVER_RE.match(value):
        raise ValueError(f"Invalid version string: {value!r}")
    return tuple(int(part) for part in value.split("."))


def compare_versions(left: str | None, right: str | None) -> int:
    left_parts = parse_version(left)
    right_parts = parse_version(right)
    max_len = max(len(left_parts), len(right_parts))
    left_parts = left_parts + (0,) * (max_len - len(left_parts))
    right_parts = right_parts + (0,) * (max_len - len(right_parts))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def version_lt(left: str | None, right: str | None) -> bool:
    if not right:
        return False
    return compare_versions(left, right) < 0


def normalize_advisory(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Package support advisory must be a JSON object.")
    advisory = dict(raw)
    for field in ("latest_version", "minimum_mutating_version", "minimum_readonly_version"):
        value = advisory.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string.")
        try:
            parse_version(value)
        except ValueError as exc:
            raise ValueError(f"{field} must use dotted numeric version form such as '0.3.4'.") from exc
    advisory_level = advisory.get("advisory_level", "supported")
    if advisory_level not in {
        "supported",
        "upgrade_recommended",
        "readonly_only",
        "diagnostic_only",
        "upgrade_required",
    }:
        raise ValueError(f"Unsupported advisory_level: {advisory_level}")
    update_hints = advisory.get("update_hints")
    if update_hints is not None and not isinstance(update_hints, dict):
        raise ValueError("update_hints must be an object when present.")
    return advisory


def support_state_from_advisory(package_version: str, advisory: dict) -> str:
    advisory = normalize_advisory(advisory)
    if version_lt(package_version, advisory.get("minimum_readonly_version")):
        return "diagnostic_only"
    if version_lt(package_version, advisory.get("minimum_mutating_version")):
        return "readonly_only"

    advisory_level = advisory.get("advisory_level", "supported")
    below_latest = version_lt(package_version, advisory.get("latest_version"))
    if advisory_level == "diagnostic_only":
        return "diagnostic_only"
    if advisory_level == "readonly_only":
        return "readonly_only"
    if advisory_level in {"upgrade_recommended", "upgrade_required"} and below_latest:
        return "upgrade_recommended"
    return "supported"


def action_level_for_script(script_name: str) -> str:
    return SCRIPT_ACTION_LEVELS.get(script_name, "readonly")


def action_level_for_dispatcher(command: str) -> str:
    return DISPATCHER_ACTION_LEVELS.get(command, "readonly")


def action_allowed(state: str, action_level: str) -> bool:
    if state not in PACKAGE_SUPPORT_STATES:
        state = "unknown_offline"
    if action_level not in ACTION_LEVELS:
        action_level = "readonly"
    if state in {"supported", "upgrade_recommended"}:
        return True
    if state == "unknown_offline":
        return action_level in {"diagnostic", "readonly"}
    if state == "readonly_only":
        return action_level in {"diagnostic", "readonly"}
    if state == "diagnostic_only":
        return action_level == "diagnostic"
    return True


def install_topology_reason(package_root: Path, *, source: str | None = None) -> str:
    if source in {"stale_cache", "offline_fallback"}:
        return "offline_cached_state_used"
    if package_root.is_symlink():
        return "wrapper_may_be_outdated"

    parts = [part.lower() for part in package_root.parts]
    looks_like_package_root = (
        (package_root / "scripts" / "recallloom.py").is_file()
        or (package_root / "package-metadata.json").is_file()
    )
    if package_root.name != "recallloom" and (
        looks_like_package_root
        or any(marker in parts for marker in (".agents", ".codex", "skills", "node_modules"))
    ):
        return "wrapper_may_be_outdated"
    for marker in (".agents", ".codex"):
        if marker not in parts:
            continue
        marker_index = parts.index(marker)
        owner_segment = parts[marker_index - 1] if marker_index > 0 else ""
        if owner_segment in {"home", "user", "users", Path.home().name.lower()}:
            return "user_install_outdated"
        return "project_copy_outdated"
    if "node_modules" in parts:
        return "wrapper_may_be_outdated"
    return "unknown_install_topology"


def user_message_for_state(state: str) -> str:
    if state == "readonly_only":
        return "This RecallLoom package needs an upgrade before mutating actions can continue."
    if state == "diagnostic_only":
        return "This RecallLoom package is only allowed to run diagnostic actions until it is upgraded."
    if state == "upgrade_recommended":
        return "A newer RecallLoom package is available, but this action is still allowed."
    if state == "unknown_offline":
        return "RecallLoom could not refresh package support status today, so mutating actions stay blocked until support can be verified."
    return "This RecallLoom package is currently supported."
