"""Helpers for public-vs-diagnostic JSON path handling."""

from __future__ import annotations

import os
from pathlib import Path
import re


PRIVATE_JSON_PATHS_ENV = "RECALLLOOM_DEBUG_JSON_PATHS"
_FALSEY_ENV_VALUES = {"", "0", "false", "no", "off"}
_NON_PUBLICIZED_PATH_FIELDS = {"latest_file"}
_PATH_VALUE_FIELDS = {
    "cache_path",
    "config_path",
    "destination_dir",
    "entry_path",
    "file",
    "latest_active_daily_log",
    "latest_active_daily_log_seen",
    "latest_workspace_artifact",
    "latest_workspace_artifact_seen",
    "lock_path",
    "package_path",
    "path",
    "proposal_file",
    "proposal_path",
    "project_root",
    "review_file",
    "review_path",
    "source_file",
    "source_path",
    "start_path",
    "storage_root",
    "target",
    "target_path",
    "tombstone_path",
    "update_protocol",
}
_PATH_LIST_FIELDS = {
    "archived_targets",
    "bridge_targets",
    "changed_files",
    "checked_files",
    "created",
    "existing_targets",
    "invalid_paths",
    "malformed_bridge_targets",
    "missing_paths",
    "skipped",
    "unknown_assets",
}
_TRAILING_PATH_PUNCTUATION = ".,:;!?)]}"
_QUOTED_PATH_PATTERN = re.compile(r"(?P<quote>[\"'])(?P<path>(?:~|/|[A-Za-z]:[\\\\/])[^\"']+)(?P=quote)")
_UNQUOTED_PATH_PATTERN = re.compile(r"(?P<path>(?:~|/|[A-Za-z]:[\\\\/])\S+)")
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential)\s*[:=]\s*['\"]?[^\s,'\"]+"
)
_COMMON_TOKEN_PATTERN = re.compile(
    r"\b(ghp_[A-Za-z0-9_]{6,}|github_pat_[A-Za-z0-9_]{6,}|xox[baprs]-[A-Za-z0-9-]{6,}|AKIA[0-9A-Z]{12,})\b"
)
_OPENAI_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9]{6,}\b")
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]+")
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|file)://\S+")


def private_json_paths_enabled(env: dict[str, str] | None = None) -> bool:
    env = env or os.environ
    raw = env.get(PRIVATE_JSON_PATHS_ENV)
    if raw is None:
        return False
    return raw.strip().casefold() not in _FALSEY_ENV_VALUES


def _resolve_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def public_project_root_label(project_root: str | Path) -> str:
    resolved = _resolve_path(Path(project_root).expanduser())
    label = resolved.name or resolved.as_posix()
    return label or "."


def public_project_path(
    path: str | Path | None,
    *,
    project_root: str | Path,
) -> str | None:
    if path is None:
        return None
    root = _resolve_path(Path(project_root).expanduser())
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = _resolve_path(candidate)
    if resolved == root:
        return public_project_root_label(root)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.name or candidate.name or resolved.as_posix()


def display_project_root_label(
    project_root: str | Path,
    *,
    private: bool | None = None,
) -> str:
    private = private_json_paths_enabled() if private is None else private
    if private:
        return str(project_root)
    return public_project_root_label(project_root)


def display_project_path(
    path: str | Path | None,
    *,
    project_root: str | Path,
    private: bool | None = None,
) -> str | None:
    private = private_json_paths_enabled() if private is None else private
    if path is None:
        return None
    if private:
        return str(path)
    return public_project_path(path, project_root=project_root)


def _publicize_path_fragment(
    raw_path: str,
    *,
    project_root: str | Path | None,
    private: bool,
) -> str:
    candidate = raw_path
    suffix = ""
    while candidate and candidate[-1] in _TRAILING_PATH_PUNCTUATION:
        suffix = candidate[-1] + suffix
        candidate = candidate[:-1]
    if not candidate:
        return raw_path
    publicized = display_project_path(
        candidate,
        project_root=project_root or candidate,
        private=private,
    )
    if publicized is None:
        return raw_path
    return f"{publicized}{suffix}"


def publicize_text_paths(
    text: str | None,
    *,
    project_root: str | Path | None,
    private: bool | None = None,
) -> str | None:
    private = private_json_paths_enabled() if private is None else private
    if private or not isinstance(text, str) or not text:
        return text

    def replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        path = match.group("path")
        return (
            f"{quote}"
            f"{_publicize_path_fragment(path, project_root=project_root, private=private)}"
            f"{quote}"
        )

    def replace_unquoted(match: re.Match[str]) -> str:
        return _publicize_path_fragment(
            match.group("path"),
            project_root=project_root,
            private=private,
        )

    publicized = _QUOTED_PATH_PATTERN.sub(replace_quoted, text)
    return _UNQUOTED_PATH_PATTERN.sub(replace_unquoted, publicized)


def redact_public_text(
    text: str | None,
    *,
    project_root: str | Path | None = None,
    private: bool | None = None,
) -> str | None:
    if not isinstance(text, str) or not text:
        return text
    publicized = publicize_text_paths(text, project_root=project_root, private=private)
    if publicized is None:
        return publicized
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub("credential=redacted", publicized)
    redacted = _BEARER_TOKEN_PATTERN.sub("bearer redacted", redacted)
    redacted = _COMMON_TOKEN_PATTERN.sub("redacted-token", redacted)
    redacted = _OPENAI_TOKEN_PATTERN.sub("redacted-token", redacted)
    redacted = _EMAIL_PATTERN.sub("redacted-email", redacted)
    redacted = _URL_PATTERN.sub("redacted-url", redacted)
    return redacted


def _field_looks_pathlike(field_name: str | None) -> bool:
    if not field_name:
        return False
    if field_name in _NON_PUBLICIZED_PATH_FIELDS:
        return False
    return (
        field_name in _PATH_VALUE_FIELDS
        or field_name.endswith("_file")
        or field_name.endswith("_dir")
        or field_name.endswith("_path")
        or field_name.endswith("_root")
    )


def publicize_json_value(
    value,
    *,
    project_root: str | Path | None,
    field_name: str | None = None,
    private: bool | None = None,
):
    private = private_json_paths_enabled() if private is None else private
    if isinstance(value, dict):
        return {
            key: publicize_json_value(
                item,
                project_root=project_root,
                field_name=key,
                private=private,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        if field_name in _PATH_LIST_FIELDS:
            return [
                display_project_path(item, project_root=project_root or ".", private=private)
                if isinstance(item, (str, Path))
                else publicize_json_value(
                    item,
                    project_root=project_root,
                    private=private,
                )
                for item in value
            ]
        return [
            publicize_json_value(
                item,
                project_root=project_root,
                private=private,
            )
            for item in value
        ]
    if isinstance(value, (str, Path)) and _field_looks_pathlike(field_name):
        if field_name == "project_root":
            return display_project_root_label(value, private=private)
        return display_project_path(
            value,
            project_root=project_root or value,
            private=private,
        )
    return value
