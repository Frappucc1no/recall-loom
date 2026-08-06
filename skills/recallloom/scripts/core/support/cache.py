"""Daily package-support advisory cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.error import URLError
from urllib.request import Request, urlopen

from core.support.policy import (
    action_allowed,
    install_topology_reason,
    invalid_support_advisory_update_hints,
    invalid_support_advisory_user_message,
    invalid_support_cache_update_hints,
    invalid_support_cache_user_message,
    normalize_advisory,
    support_cache_repair_failed_user_message,
    support_state_from_advisory,
    user_message_for_state,
)
from core.workspace.atomic_io import atomic_write_bytes


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
    "support_diagnostic_reason",
    "user_message",
    "fetch_error",
)
INVALID_SUPPORT_ADVISORY_REASON = "invalid_support_advisory"
CACHE_STATUS_MISSING = "missing"
CACHE_STATUS_VALID = "valid"
CACHE_STATUS_INVALID = "invalid"
CACHE_REASON_MISSING = "support_cache_missing"
CACHE_REASON_VALID = "support_cache_valid"
CACHE_REASON_UNREADABLE = "support_cache_unreadable"
CACHE_REASON_NON_UTF8 = "support_cache_non_utf8"
CACHE_REASON_MALFORMED_JSON = "support_cache_malformed_json"
CACHE_REASON_NOT_OBJECT = "support_cache_not_object"
CACHE_REASON_WRONG_PACKAGE = "support_cache_wrong_package_path"
CACHE_REASON_INVALID_DATE = "support_cache_invalid_checked_date"
CACHE_REASON_INVALID_ADVISORY = "support_cache_invalid_advisory"
CACHE_REASON_REPAIRED = "support_cache_repaired"
CACHE_REASON_REPAIR_FAILED = "support_cache_repair_failed"
CACHE_REASON_WRITE_FAILED = "support_cache_write_failed"


@dataclass(frozen=True)
class SupportCacheLoad:
    """Tri-state result for the local, non-authenticated support-policy cache."""

    status: str
    reason_code: str
    payload: dict | None = None
    advisory: dict | None = None


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


def load_cached_support(path: Path, *, package_root: Path) -> SupportCacheLoad:
    try:
        entry_stat = path.lstat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return SupportCacheLoad(CACHE_STATUS_MISSING, CACHE_REASON_MISSING)
        except OSError:
            return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)
    except OSError:
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)
    if not stat.S_ISREG(entry_stat.st_mode):
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)

    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            fd = None
            opened_before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or (entry_stat.st_dev, entry_stat.st_ino)
                != (opened_before.st_dev, opened_before.st_ino)
            ):
                return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)
            raw = handle.read()
            opened_after = os.fstat(handle.fileno())
            canonical_after = path.lstat()
    except OSError:
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if (
        not stat.S_ISREG(opened_after.st_mode)
        or not stat.S_ISREG(canonical_after.st_mode)
        or (entry_stat.st_dev, entry_stat.st_ino)
        != (opened_after.st_dev, opened_after.st_ino)
        or (entry_stat.st_dev, entry_stat.st_ino)
        != (canonical_after.st_dev, canonical_after.st_ino)
    ):
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_UNREADABLE)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_NON_UTF8)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_MALFORMED_JSON)
    if not isinstance(payload, dict):
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_NOT_OBJECT)
    if payload.get("package_path") != str(package_root.resolve()):
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_WRONG_PACKAGE)
    checked_date = payload.get("checked_date")
    if not isinstance(checked_date, str):
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_INVALID_DATE)
    try:
        date.fromisoformat(checked_date)
    except ValueError:
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_INVALID_DATE)
    advisory = advisory_from_cached_support(payload)
    if advisory is None:
        return SupportCacheLoad(CACHE_STATUS_INVALID, CACHE_REASON_INVALID_ADVISORY)
    return SupportCacheLoad(
        CACHE_STATUS_VALID,
        CACHE_REASON_VALID,
        payload=payload,
        advisory=advisory,
    )


def write_cached_support(path: Path, payload: dict) -> str | None:
    try:
        # The staged temp + fsync + os.replace mechanism is owned by
        # core.workspace.atomic_io.
        atomic_write_bytes(
            path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    except OSError:
        return CACHE_REASON_WRITE_FAILED
    return None


def trusted_cached_support(
    *,
    package_root: Path,
    package_version: str,
    checked_date: str,
    env: dict[str, str],
) -> dict | None:
    cache_path = cache_path_for_package(package_root, env)
    cache_load = load_cached_support(cache_path, package_root=package_root)
    if cache_load.status != CACHE_STATUS_VALID or cache_load.payload is None or cache_load.advisory is None:
        return None
    cached = cache_load.payload
    if cached.get("checked_date") != checked_date:
        return None
    if cached.get("current_version") != package_version:
        return None
    recalculated = result_from_advisory(
        package_root=package_root,
        package_version=package_version,
        checked_date=checked_date,
        source="cache_today",
        advisory=cache_load.advisory,
        cache_path=cache_path,
        cache_hit=True,
    )
    cached_message = stale_cached_user_message(cached, recalculated["package_support_state"])
    if cached_message is not None:
        recalculated["user_message"] = cached_message
    return recalculated


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
        "update_hints": invalid_support_advisory_update_hints(),
        "user_message": invalid_support_advisory_user_message(),
    }


def invalid_cache_advisory(package_version: str, *, reason_code: str) -> dict:
    advisory = invalid_advisory(package_version)
    advisory["reason_code"] = reason_code
    advisory["update_hints"] = invalid_support_cache_update_hints()
    advisory["user_message"] = invalid_support_cache_user_message()
    return advisory


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

    cache_path = cache_path_for_package(package_root, env)
    cache_load = load_cached_support(cache_path, package_root=package_root)

    # A cache is a network freshness shortcut only when it is for this exact
    # package version and logical day and passed the full cache contract.  The
    # resulting advisory is still projected per invocation below, so `allowed`
    # is never inherited from an earlier action.  SUPPORT_DISABLE_ENV remains
    # an explicit diagnostic escape hatch that forces a fresh advisory check.
    has_explicit_local_advisory = bool(env.get(SUPPORT_ADVISORY_FILE_ENV))
    same_day_cache = (
        None
        if disable_shortcuts or has_explicit_local_advisory
        else trusted_cached_support(
            package_root=package_root,
            package_version=package_version,
            checked_date=checked_date,
            env=env,
        )
    )

    inherited = (
        None
        if disable_shortcuts or same_day_cache is not None
        else inherited_support_state(
            package_root=package_root,
            package_version=package_version,
            checked_date=checked_date,
            env=env,
        )
    )
    if same_day_cache is not None:
        result = same_day_cache
    elif inherited is not None:
        result = inherited
    else:
        advisory, source, fetch_error, advisory_invalid = read_advisory(env, default_url=advisory_url)
        advisory_was_valid = advisory is not None
        if advisory is None and advisory_invalid:
            advisory = invalid_advisory(package_version)
        if advisory is None and fetch_error is None and source == "no_advisory_config":
            fetch_error = source
        if advisory is None and fetch_error and cache_load.status == CACHE_STATUS_VALID:
            result = result_from_advisory(
                package_root=package_root,
                package_version=package_version,
                checked_date=checked_date,
                source="stale_cache",
                advisory=cache_load.advisory,
                fetch_error=fetch_error,
                cache_path=cache_path,
                cache_hit=True,
            )
            cached_message = stale_cached_user_message(
                cache_load.payload or {},
                result["package_support_state"],
            )
            if cached_message is not None:
                result["user_message"] = cached_message
            result["support_diagnostic_reason"] = "offline_cached_state_used"
        elif advisory is None and fetch_error and cache_load.status == CACHE_STATUS_INVALID:
            result = result_from_advisory(
                package_root=package_root,
                package_version=package_version,
                checked_date=checked_date,
                source="invalid_cache",
                advisory=invalid_cache_advisory(
                    package_version,
                    reason_code=cache_load.reason_code,
                ),
                fetch_error=fetch_error,
                cache_path=cache_path,
            )
            result["support_diagnostic_reason"] = cache_load.reason_code
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
            if advisory is None and cache_load.status == CACHE_STATUS_MISSING:
                result["support_diagnostic_reason"] = "offline_no_cache_local_first"
                result["user_message"] = (
                    "RecallLoom could not refresh package support status; local actions remain available "
                    "without a support cache."
                )
        if advisory_was_valid:
            cache_error = write_cached_support(cache_path, result)
            if cache_error:
                result["cache_write_error"] = cache_error
                if cache_load.status == CACHE_STATUS_INVALID:
                    result["support_diagnostic_reason"] = CACHE_REASON_REPAIR_FAILED
                    result["user_message"] = support_cache_repair_failed_user_message()
                    result["update_hints"] = invalid_support_cache_update_hints()
            elif cache_load.status == CACHE_STATUS_INVALID:
                result["support_diagnostic_reason"] = CACHE_REASON_REPAIRED

    if disable_shortcuts:
        result["disabled"] = True

    result["action_name"] = action_name
    result["action_level"] = action_level
    result["allowed"] = action_allowed(result.get("package_support_state", "diagnostic_only"), action_level)
    return result
