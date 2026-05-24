"""Daily package-support advisory cache."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from core.support.policy import (
    action_allowed,
    install_topology_reason,
    normalize_advisory,
    support_state_from_advisory,
    user_message_for_state,
)


SUPPORT_STATE_ENV = "RECALLLOOM_SUPPORT_STATE_JSON"
SUPPORT_DISABLE_ENV = "RECALLLOOM_SUPPORT_DISABLE"
SUPPORT_CACHE_DIR_ENV = "RECALLLOOM_SUPPORT_CACHE_DIR"
SUPPORT_DATE_ENV = "RECALLLOOM_SUPPORT_DATE"
SUPPORT_ADVISORY_FILE_ENV = "RECALLLOOM_SUPPORT_ADVISORY_FILE"
SUPPORT_ADVISORY_URL_ENV = "RECALLLOOM_SUPPORT_ADVISORY_URL"
SUPPORT_FETCH_TIMEOUT_ENV = "RECALLLOOM_SUPPORT_FETCH_TIMEOUT_SECONDS"
DEFAULT_FETCH_TIMEOUT_SECONDS = 2.0
CACHED_ADVISORY_FIELDS = (
    "latest_version",
    "minimum_mutating_version",
    "minimum_readonly_version",
    "reason_code",
)
TRUSTED_INHERITED_FIELDS = (
    "package_support_state",
    "current_version",
    "latest_version",
    "minimum_mutating_version",
    "minimum_readonly_version",
    "advisory_level",
    "reason_code",
    "update_hints",
    "checked_date",
    "package_path",
    "support_diagnostic_reason",
    "user_message",
    "fetch_error",
)
INVALID_SUPPORT_ADVISORY_REASON = "invalid_support_advisory"


def today_label(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    override = env.get(SUPPORT_DATE_ENV)
    if override:
        date.fromisoformat(override)
        return override
    return date.today().isoformat()


def package_cache_key(package_root: Path) -> str:
    return hashlib.sha256(str(package_root.resolve()).encode("utf-8")).hexdigest()[:24]


def default_cache_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    configured = env.get(SUPPORT_CACHE_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = env.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "recallloom" / "support"
    return Path.home() / ".cache" / "recallloom" / "support"


def cache_path_for_package(package_root: Path, env: dict[str, str] | None = None) -> Path:
    return default_cache_dir(env) / f"{package_cache_key(package_root)}.json"


def load_cached_support(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_cached_support(path: Path, payload: dict) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return str(exc)
    return None


def trusted_cached_support(
    *,
    package_root: Path,
    package_version: str,
    checked_date: str,
    env: dict[str, str],
) -> dict | None:
    cache_path = cache_path_for_package(package_root, env)
    cached = load_cached_support(cache_path)
    if cached is None:
        return None
    if cached.get("checked_date") != checked_date:
        return None
    if cached.get("package_path") != str(package_root.resolve()):
        return None
    if cached.get("current_version") != package_version:
        return None
    return cached


def inherited_support_state(
    *,
    package_root: Path,
    package_version: str,
    checked_date: str,
    env: dict[str, str],
) -> dict | None:
    raw = env.get(SUPPORT_STATE_ENV)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    cached = trusted_cached_support(
        package_root=package_root,
        package_version=package_version,
        checked_date=checked_date,
        env=env,
    )
    if cached is None:
        return None
    if any(payload.get(field) != cached.get(field) for field in TRUSTED_INHERITED_FIELDS):
        return None
    inherited = dict(cached)
    inherited["cache_hit"] = True
    inherited["source"] = "cache_today"
    return inherited


def fetch_timeout(env: dict[str, str]) -> float:
    raw = env.get(SUPPORT_FETCH_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_FETCH_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_FETCH_TIMEOUT_SECONDS
    return max(0.1, min(value, 10.0))


def invalid_advisory(package_version: str) -> dict:
    return {
        "latest_version": package_version,
        "minimum_mutating_version": package_version,
        "minimum_readonly_version": package_version,
        "advisory_level": "diagnostic_only",
        "reason_code": INVALID_SUPPORT_ADVISORY_REASON,
        "update_hints": {},
    }


def read_advisory(
    env: dict[str, str],
    *,
    default_url: str | None = None,
) -> tuple[dict | None, str, str | None, bool]:
    file_raw = env.get(SUPPORT_ADVISORY_FILE_ENV)
    if file_raw:
        path = Path(file_raw).expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            return None, f"file:{path}", str(exc), False
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, f"file:{path}", str(exc), True
        try:
            return normalize_advisory(raw), f"file:{path}", None, False
        except ValueError as exc:
            return None, f"file:{path}", str(exc), True

    url = env.get(SUPPORT_ADVISORY_URL_ENV) or default_url
    if url:
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "RecallLoom-support-check"})
            with urlopen(request, timeout=fetch_timeout(env)) as response:
                raw = response.read(128 * 1024)
        except (URLError, TimeoutError, OSError) as exc:
            return None, f"url:{url}", str(exc), False
        try:
            return normalize_advisory(json.loads(raw.decode("utf-8"))), f"url:{url}", None, False
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return None, f"url:{url}", str(exc), True

    return None, "no_advisory_config", None, False


def result_from_advisory(
    *,
    package_root: Path,
    package_version: str,
    checked_date: str,
    source: str,
    advisory: dict | None,
    fetch_error: str | None = None,
    cache_path: Path | None = None,
    cache_hit: bool = False,
) -> dict:
    if advisory is None:
        state = "unknown_offline" if fetch_error else "supported"
        advisory = {}
    else:
        state = support_state_from_advisory(package_version, advisory)

    if advisory.get("reason_code") == INVALID_SUPPORT_ADVISORY_REASON:
        diagnostic_reason = INVALID_SUPPORT_ADVISORY_REASON
    elif state in {"readonly_only", "diagnostic_only"}:
        diagnostic_reason = install_topology_reason(package_root, source=source)
    else:
        diagnostic_reason = None
    if state == "unknown_offline" and fetch_error == "no_advisory_config":
        diagnostic_reason = "no_advisory_config"
    elif state == "unknown_offline" and fetch_error:
        diagnostic_reason = "offline_cached_state_used"

    return {
        "package_support_state": state,
        "current_version": package_version,
        "latest_version": advisory.get("latest_version"),
        "minimum_mutating_version": advisory.get("minimum_mutating_version"),
        "minimum_readonly_version": advisory.get("minimum_readonly_version"),
        "advisory_level": advisory.get("advisory_level", "supported"),
        "reason_code": advisory.get("reason_code"),
        "update_hints": advisory.get("update_hints", {}),
        "checked_date": checked_date,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source,
        "cache_hit": cache_hit,
        "cache_path": str(cache_path) if cache_path else None,
        "package_path": str(package_root.resolve()),
        "support_diagnostic_reason": diagnostic_reason,
        "user_message": advisory.get("user_message") or user_message_for_state(state),
        "fetch_error": fetch_error,
    }


def advisory_from_cached_support(payload: dict) -> dict | None:
    # Stale-cache fallback may cross a package upgrade/downgrade on the same install path.
    # Only carry forward advisory snapshot fields from cache; version- and action-specific
    # verdicts such as current_version, package_support_state, and allowed must be recomputed.
    advisory = {field: payload.get(field) for field in CACHED_ADVISORY_FIELDS}
    advisory["advisory_level"] = payload.get("advisory_level", "supported")
    advisory["update_hints"] = payload.get("update_hints", {})
    try:
        return normalize_advisory(advisory)
    except ValueError:
        return None


def stale_cached_user_message(payload: dict, recalculated_state: str) -> str | None:
    cached_message = payload.get("user_message")
    if payload.get("package_support_state") != recalculated_state:
        return None
    if not isinstance(cached_message, str) or not cached_message.strip():
        return None
    return cached_message


def package_support_result(
    *,
    package_root: Path,
    package_version: str,
    action_name: str,
    action_level: str,
    advisory_url: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    env = env or os.environ
    checked_date = today_label(env)
    package_root = package_root.resolve()
    disable_shortcuts = env.get(SUPPORT_DISABLE_ENV) == "1"

    if not disable_shortcuts:
        inherited = inherited_support_state(
            package_root=package_root,
            package_version=package_version,
            checked_date=checked_date,
            env=env,
        )
    else:
        inherited = None

    if inherited is not None:
        result = inherited
    else:
        cache_path = cache_path_for_package(package_root, env)
        same_day_cached = trusted_cached_support(
            package_root=package_root,
            package_version=package_version,
            checked_date=checked_date,
            env=env,
        )
        cached = load_cached_support(cache_path)
        if not disable_shortcuts and same_day_cached is not None:
            result = dict(same_day_cached)
            result["cache_hit"] = True
            result["source"] = "cache_today"
        else:
            advisory, source, fetch_error, advisory_invalid = read_advisory(env, default_url=advisory_url)
            if advisory is None and advisory_invalid:
                advisory = invalid_advisory(package_version)
            if advisory is None and fetch_error is None and source == "no_advisory_config":
                fetch_error = source
            if advisory is None and fetch_error and cached is not None:
                stale_advisory = advisory_from_cached_support(cached)
                result = result_from_advisory(
                    package_root=package_root,
                    package_version=package_version,
                    checked_date=checked_date,
                    source="stale_cache",
                    advisory=stale_advisory,
                    fetch_error=fetch_error,
                    cache_path=cache_path,
                    cache_hit=True,
                )
                cached_message = stale_cached_user_message(cached, result["package_support_state"])
                if cached_message is not None:
                    result["user_message"] = cached_message
                result["support_diagnostic_reason"] = "offline_cached_state_used"
            else:
                result = result_from_advisory(
                    package_root=package_root,
                    package_version=package_version,
                    checked_date=checked_date,
                    source=source,
                    advisory=advisory,
                    fetch_error=fetch_error,
                    cache_path=cache_path,
                )
            cache_error = write_cached_support(cache_path, result)
            if cache_error:
                result["cache_write_error"] = cache_error

    if disable_shortcuts:
        result["disabled"] = True

    result["action_name"] = action_name
    result["action_level"] = action_level
    result["allowed"] = action_allowed(result.get("package_support_state", "unknown_offline"), action_level)
    return result
