"""Shared privacy classification for recording plan and suggestion inputs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.output.privacy import contains_local_absolute_path


UNSAFE_RECORD_TEXT_TOKENS = (
    "<!-- recallloom:",
    "<!-- file-state:",
    "<!-- daily-log-entry:",
    "<!-- daily-log-scaffold",
    "<!-- last-writer:",
    "file://",
    "api secret",
    "api key",
    "secret",
    "token",
    "password",
    "密码",
    "密钥",
    "私钥",
    "访问令牌",
    "<attached",
    "attached text",
    "manual patch",
    "state.json",
    "config.json",
    "receipt store",
)


def _iter_record_text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            fragments.append(str(key))
            fragments.extend(_iter_record_text_fragments(item))
    elif isinstance(value, list):
        for item in value:
            fragments.extend(_iter_record_text_fragments(item))
    elif value is not None:
        fragments.append(str(value))
    return fragments


def record_input_text(input_contract: Mapping[str, Any]) -> tuple[str, bool]:
    intent = str(input_contract.get("intent_text") or "")
    payload = input_contract.get("prepared_record_payload")
    payload_fragments = _iter_record_text_fragments(payload)
    return " ".join([intent, *payload_fragments]), bool(payload_fragments)


def contains_unsafe_record_text(text: str) -> bool:
    lowered = text.casefold()
    return contains_local_absolute_path(text) or any(
        token in lowered for token in UNSAFE_RECORD_TEXT_TOKENS
    )
