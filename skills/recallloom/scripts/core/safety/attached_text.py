#!/usr/bin/env python3
"""Attached-text safety scanning for auto-inserted continuity content."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

INVISIBLE_UNICODE_RE = re.compile(r"[\u200b-\u200f\u2060\u2066-\u2069\ufeff]")
SAFE_REMOTE_URL_RE = re.compile(r"\bhttps?://[^\s`\"'<>]+", re.I)
PATH_TOKEN_BOUNDARY = r"(?=$|[\s`\"'<>.,;:!?)\]}])"
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~%-]+$")
LOCAL_POSIX_SINGLE_SEGMENT_ROOTS = {
    "applications",
    "dev",
    "etc",
    "home",
    "library",
    "media",
    "mnt",
    "opt",
    "private",
    "root",
    "run",
    "tmp",
    "users",
    "usr",
    "var",
    "volumes",
    "workspace",
}
POSIX_ABSOLUTE_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9+.-])/(?!/)[^:/\s`\"'<>]+(?:/[^:/\s`\"'<>]+)*{PATH_TOKEN_BOUNDARY}"
)
WINDOWS_DRIVE_ABSOLUTE_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9+.-])(?<!//)[A-Za-z]:[\\/][^\\/\s`\"'<>]+(?:[\\/][^\\/\s`\"'<>]+)*"
    rf"{PATH_TOKEN_BOUNDARY}"
)
WINDOWS_UNC_ABSOLUTE_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9+.-])(?<!//)\\\\[^\\/\s`\"'<>]+\\[^\\/\s`\"'<>]+(?:\\[^\\/\s`\"'<>]+)*"
    rf"{PATH_TOKEN_BOUNDARY}"
)
FILE_URL_ABSOLUTE_PATH_RE = re.compile(r"\bfile://[^\s`\"'<>]+", re.I)
ABSOLUTE_PATH_PATTERNS = (
    POSIX_ABSOLUTE_PATH_RE,
    WINDOWS_DRIVE_ABSOLUTE_PATH_RE,
    WINDOWS_UNC_ABSOLUTE_PATH_RE,
    FILE_URL_ABSOLUTE_PATH_RE,
)
ENV_ASSIGNMENT_LINE_RE = re.compile(r"(?m)^\s*([A-Z][A-Z0-9_]{1,63})=(.+)\s*$")
SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|AUTH)(?:_|$)"
)
PATH_ENV_KEY_RE = re.compile(
    r"^(?:HOME|PWD|OLDPWD|PATH|SHELL|PYTHONPATH|VIRTUAL_ENV|TMPDIR|XDG_[A-Z0-9_]+|USERPROFILE|HOMEDRIVE|HOMEPATH)$"
)
ABSOLUTE_PATH_VALUE_RE = re.compile(r"^(?:/|~(?:/|$)|[A-Za-z]:\\)")

ATTACH_SCAN_HARD_BLOCK_PATTERNS = (
    re.compile(r"\bignore (all )?(previous|prior|above) (instructions|rules|guidance)\b", re.I),
    re.compile(r"\b(disregard|override) (the )?(system prompt|developer message|instructions?)\b", re.I),
    re.compile(r"\b(reveal|print|dump|show|exfiltrat\w*)\b.{0,40}\b(secret|token|password|api key|credential|ssh key|env)\b", re.I | re.S),
    re.compile(r"(忽略|无视).{0,12}(之前|先前|前面|上述|以上).{0,12}(指令|规则|要求|提示)", re.I | re.S),
    re.compile(r"(绕过|覆盖|忽略).{0,12}(系统提示|系统消息|开发者消息|开发者提示|指令|规则)", re.I | re.S),
    re.compile(r"(显示|泄露|输出|打印|导出).{0,40}(secret|token|password|api key|credential|ssh key|env|环境变量|密钥|令牌|密码|凭证|私钥)", re.I | re.S),
)

ATTACH_SCAN_WARNING_PATTERNS = (
    re.compile(r"\b(secret|token|password|credential|api key)\b", re.I),
    re.compile(r"\bignore\b", re.I),
    re.compile(r"(密钥|令牌|密码|凭证|环境变量|API ?key)", re.I),
    re.compile(r"(忽略|无视)", re.I),
)


def _normalize_path_token(token: str) -> str:
    return token.rstrip(".,;:!?)]}")


def _looks_like_local_posix_absolute_path(candidate: str) -> bool:
    normalized = _normalize_path_token(candidate)
    if not normalized.startswith("/") or normalized.startswith("//"):
        return False
    segments = [segment for segment in normalized.split("/") if segment]
    if not segments or any(not PATH_SEGMENT_RE.fullmatch(segment) for segment in segments):
        return False
    if len(segments) >= 2:
        return True
    if segments[0].casefold() in LOCAL_POSIX_SINGLE_SEGMENT_ROOTS:
        return True
    return any(character.isupper() for character in segments[0]) or any(
        marker in segments[0] for marker in "._-%~"
    )


def _looks_like_windows_drive_absolute_path(candidate: str) -> bool:
    normalized = _normalize_path_token(candidate)
    return bool(re.fullmatch(r"[A-Za-z]:[\\/][^\\/\s`\"'<>]+(?:[\\/][^\\/\s`\"'<>]+)*", normalized))


def _looks_like_windows_unc_absolute_path(candidate: str) -> bool:
    normalized = _normalize_path_token(candidate)
    return bool(
        re.fullmatch(r"\\\\[^\\/\s`\"'<>]+\\[^\\/\s`\"'<>]+(?:\\[^\\/\s`\"'<>]+)*", normalized)
    )


def _looks_like_local_file_url(candidate: str) -> bool:
    normalized = _normalize_path_token(candidate)
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() != "file":
        return False
    netloc = parsed.netloc
    if netloc.lower() == "localhost":
        netloc = ""
    decoded_path = unquote(parsed.path or "")
    if netloc:
        unc_candidate = "\\\\" + netloc + decoded_path.replace("/", "\\")
        return _looks_like_windows_unc_absolute_path(unc_candidate)
    trimmed_path = decoded_path.lstrip("/")
    return _looks_like_windows_drive_absolute_path(trimmed_path) or _looks_like_local_posix_absolute_path(
        decoded_path
    )


def contains_unsafe_absolute_path(text: str) -> bool:
    sanitized_text = SAFE_REMOTE_URL_RE.sub(" ", text)
    for match in WINDOWS_DRIVE_ABSOLUTE_PATH_RE.finditer(sanitized_text):
        if _looks_like_windows_drive_absolute_path(match.group(0)):
            return True
    for match in WINDOWS_UNC_ABSOLUTE_PATH_RE.finditer(sanitized_text):
        if _looks_like_windows_unc_absolute_path(match.group(0)):
            return True
    for match in FILE_URL_ABSOLUTE_PATH_RE.finditer(sanitized_text):
        if _looks_like_local_file_url(match.group(0)):
            return True
    for match in POSIX_ABSOLUTE_PATH_RE.finditer(sanitized_text):
        if _looks_like_local_posix_absolute_path(match.group(0)):
            return True
    return False


def scan_auto_attached_context_text(text: str) -> dict:
    hard_block_reasons: list[str] = []
    warnings: list[str] = []

    if INVISIBLE_UNICODE_RE.search(text):
        hard_block_reasons.append("invisible_unicode")

    for pattern in ATTACH_SCAN_HARD_BLOCK_PATTERNS:
        if pattern.search(text):
            hard_block_reasons.append(pattern.pattern)

    for pattern in ATTACH_SCAN_WARNING_PATTERNS:
        if pattern.search(text):
            warnings.append(pattern.pattern)

    env_assignment_matches = list(ENV_ASSIGNMENT_LINE_RE.finditer(text))
    sensitive_assignments = [
        match.group(1)
        for match in env_assignment_matches
        if SENSITIVE_ENV_KEY_RE.search(match.group(1))
    ]
    if sensitive_assignments:
        hard_block_reasons.append("sensitive_env_assignment_dump")

    path_assignment_matches = [
        match.group(1)
        for match in env_assignment_matches
        if PATH_ENV_KEY_RE.match(match.group(1))
        or ABSOLUTE_PATH_VALUE_RE.match(match.group(2).strip())
    ]
    if len(path_assignment_matches) >= 2:
        hard_block_reasons.append("environment_variable_listing_dump")

    if contains_unsafe_absolute_path(text):
        hard_block_reasons.append("absolute_path_dump")

    return {
        "blocked": bool(hard_block_reasons),
        "hard_block_reasons": hard_block_reasons,
        "warnings": warnings,
    }
