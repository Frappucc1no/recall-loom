#!/usr/bin/env python3
"""Shared helpers for RecallLoom support scripts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from contextlib import contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Iterable

PROTOCOL_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)*$")
RECOVERY_PROPOSAL_FILE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[A-Za-z0-9._-]+\.md$")
REVIEW_RECORD_FILE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[A-Za-z0-9._-]+\.review\.md$")
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.*?)\s*$")
HEADING_NUMBER_PREFIX_RE = re.compile(r"^\s*[0-9]+(?:\.[0-9]+)*[.)、:：-]?\s*")
INVISIBLE_UNICODE_RE = re.compile(r"[\u200b-\u200f\u2060\u2066-\u2069\ufeff]")
PACKAGE_SUPPORT_PUBLIC_REASON_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PACKAGE_SUPPORT_PUBLIC_HINT_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PACKAGE_SUPPORT_PUBLIC_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._,;:()'+-]{0,239}$")
PACKAGE_SUPPORT_PRIVATE_TEXT_RE = re.compile(
    r"(?i)(?:https?://|file://|/|\\|@|\b(?:api[_-]?key|token|secret|password|credential)\b|"
    r"\bsk-[A-Za-z0-9_-]{6,}\b|"
    r"\bghp_[A-Za-z0-9_]{6,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{6,}\b|"
    r"\bbearer\s+[A-Za-z0-9._-]+)"
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.protocol import contracts as protocol_contracts
from core.protocol import markers as protocol_markers
from core.protocol import sections as protocol_sections
from core.protocol import templates as protocol_templates
from core import errors as core_errors
from core.workspace import runtime as workspace_runtime
from core.bridge import blocks as bridge_blocks
from core.continuity import freshness as continuity_freshness
from core.failure.contracts import failure_payload, preferred_failure_language
from core.output.privacy import (
    display_project_path as shared_display_project_path,
    display_project_root_label as shared_display_project_root_label,
    public_project_path as shared_public_project_path,
    public_project_root_label as shared_public_project_root_label,
    publicize_json_value,
    redact_public_text as shared_redact_public_text,
    publicize_text_paths as shared_publicize_text_paths,
)
from core.safety.scratch_residue import (
    ScratchResidueReport,
    external_scratch_roots_for_sources,
    scan_startup_scratch_residue,
)
from core.support.cache import SUPPORT_STATE_ENV, package_support_result
from core.support.policy import action_level_for_script, user_message_for_state
from core.safety import attached_text as safety_attached_text

METADATA_PATH = PACKAGE_ROOT / "package-metadata.json"
MANAGED_ASSETS_OVERRIDE_ENV = "RECALLLOOM_MANAGED_ASSETS_PATH"
DEFAULT_MANAGED_ASSETS_PATH = PACKAGE_ROOT / "managed-assets.json"
MANAGED_ASSETS_PATH = Path(os.environ.get(MANAGED_ASSETS_OVERRIDE_ENV, str(DEFAULT_MANAGED_ASSETS_PATH))).expanduser().resolve()
CONTEXT_DIRNAME = workspace_runtime.CONTEXT_DIRNAME
VISIBLE_DIRNAME = workspace_runtime.VISIBLE_DIRNAME
DEFAULT_STORAGE_MODE = workspace_runtime.DEFAULT_STORAGE_MODE
VISIBLE_STORAGE_MODE = workspace_runtime.VISIBLE_STORAGE_MODE
RECOVERY_PROPOSAL_REQUIRED_HEADINGS = (
    ("来源摘要", "source summary"),
    ("来源类型与可信级别", "source type and confidence"),
    ("候选当前状态事实", "candidate current-state facts"),
    ("候选里程碑事件", "candidate milestone events"),
    ("候选判断反转", "candidate judgment reversals"),
    ("候选下一步变化", "candidate next-step changes"),
    ("与当前 sidecar 的冲突", "conflicts with current sidecar"),
    ("建议提升动作", "suggested promotion actions"),
    ("审阅结论", "review conclusion"),
)
RECOVERY_REVIEW_REQUIRED_HEADINGS = (
    ("proposal reference", "提案引用"),
    ("review outcome", "审阅结论"),
    ("approved items", "通过项"),
    ("rejected items", "拒绝项"),
    ("promotion status", "提升状态"),
    ("next action", "下一步"),
)
RECOVERY_REVIEW_HINT_MARKERS = (
    "hint-only",
    "hint only",
    "kept as hint",
    "retain as hint",
    "no items remain hint-only",
    "保留为 hint",
    "仅保留为 hint",
    "只保留为 hint",
    "无 hint",
)
RECOVERY_PROMOTION_TARGET_MARKERS = (
    "rolling_summary.md",
    "context_brief.md",
    "daily_logs/",
    "daily_logs\\",
    "daily log",
)
UPDATE_PROTOCOL_TIME_POLICY_KEYWORDS = (
    "workday",
    "logical workday",
    "work day",
    "active day",
    "rollover",
    "rollover_hour",
    "timezone",
    "time zone",
    "cross-day",
    "cross day",
    "append target",
    "append date",
    "start new day",
    "close day",
    "yesterday",
    "today",
    "工作日",
    "逻辑工作日",
    "时区",
    "跨天",
    "追加日期",
    "追加日志日期",
    "关闭昨天",
    "开启新的一天",
    "昨天",
    "今天",
)

DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_DIRS = continuity_freshness.DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_DIRS
DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_FILES = continuity_freshness.DEFAULT_WORKSPACE_ARTIFACT_EXCLUDED_FILES

ATTACH_SCAN_HARD_BLOCK_PATTERNS = safety_attached_text.ATTACH_SCAN_HARD_BLOCK_PATTERNS
ATTACH_SCAN_WARNING_PATTERNS = safety_attached_text.ATTACH_SCAN_WARNING_PATTERNS


def load_package_metadata() -> dict:
    try:
        payload = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing package metadata file: {METADATA_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Malformed package metadata file: {METADATA_PATH}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Package metadata file is not valid UTF-8: {METADATA_PATH}") from exc

    required = {
        "package_name",
        "display_name",
        "package_version",
        "protocol_version",
        "supported_protocol_versions",
        "minimum_python_version",
        "supported_workspace_languages",
        "supported_bridge_targets",
    }
    missing = sorted(required.difference(payload.keys()))
    if missing:
        raise RuntimeError(
            f"Package metadata is missing required fields {missing}: {METADATA_PATH}"
        )

    if not isinstance(payload["package_name"], str) or not payload["package_name"].strip():
        raise RuntimeError(f"package_name must be a non-empty string: {METADATA_PATH}")
    if not isinstance(payload["display_name"], str) or not payload["display_name"].strip():
        raise RuntimeError(f"display_name must be a non-empty string: {METADATA_PATH}")
    if not isinstance(payload["package_version"], str) or not payload["package_version"].strip():
        raise RuntimeError(f"package_version must be a non-empty string: {METADATA_PATH}")
    if not isinstance(payload["minimum_python_version"], str) or not payload["minimum_python_version"].strip():
        raise RuntimeError(f"minimum_python_version must be a non-empty string: {METADATA_PATH}")
    if not isinstance(payload["protocol_version"], str) or not payload["protocol_version"].strip():
        raise RuntimeError(f"protocol_version must be a non-empty string: {METADATA_PATH}")
    if not PROTOCOL_VERSION_RE.match(payload["protocol_version"]):
        raise RuntimeError(
            f"protocol_version must use dotted string form such as '1.0': {METADATA_PATH}"
        )

    supported_protocols = payload["supported_protocol_versions"]
    if not isinstance(supported_protocols, list) or not supported_protocols:
        raise RuntimeError(f"supported_protocol_versions must be a non-empty list: {METADATA_PATH}")
    if not all(isinstance(item, str) and PROTOCOL_VERSION_RE.match(item) for item in supported_protocols):
        raise RuntimeError(
            f"supported_protocol_versions must contain only dotted protocol-version strings: {METADATA_PATH}"
        )
    if payload["protocol_version"] not in supported_protocols:
        raise RuntimeError(
            f"protocol_version must be included in supported_protocol_versions: {METADATA_PATH}"
        )

    languages = payload["supported_workspace_languages"]
    if not isinstance(languages, list) or not languages or not all(isinstance(item, str) and item for item in languages):
        raise RuntimeError(
            f"supported_workspace_languages must be a non-empty list of strings: {METADATA_PATH}"
        )

    bridge_targets = payload["supported_bridge_targets"]
    if not isinstance(bridge_targets, list) or not bridge_targets or not all(isinstance(item, str) and item for item in bridge_targets):
        raise RuntimeError(
            f"supported_bridge_targets must be a non-empty list of strings: {METADATA_PATH}"
        )

    return payload


def _load_relative_path_list(payload: dict, *, field: str, source_path: Path) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise RuntimeError(f"{field} must be a list: {source_path}")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(f"{field} must contain non-empty strings: {source_path}")
        normalized_item = PurePosixPath(item.strip()).as_posix()
        normalized_parts = PurePosixPath(normalized_item).parts
        if (
            normalized_item in {".", ""}
            or normalized_item.startswith("../")
            or normalized_item.startswith("/")
            or ".." in normalized_parts
        ):
            raise RuntimeError(f"{field} contains an invalid relative path '{item}': {source_path}")
        if normalized_item in seen:
            raise RuntimeError(f"{field} contains a duplicate path '{normalized_item}': {source_path}")
        seen.add(normalized_item)
        normalized.append(normalized_item)
    return normalized


def extract_section_text(text: str, section_key: str) -> str:
    return protocol_sections.extract_section_text(text, section_key)


def markdown_heading_titles(text: str) -> list[str]:
    return protocol_sections.markdown_heading_titles(text)


def missing_recovery_headings(text: str, heading_groups: tuple[tuple[str, ...], ...]) -> list[str]:
    return protocol_sections.missing_recovery_headings(text, heading_groups)


def validate_recovery_proposal_text(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["Recovery proposal content is empty."]

    missing = missing_recovery_headings(text, RECOVERY_PROPOSAL_REQUIRED_HEADINGS)
    if missing:
        errors.append(
            "Recovery proposal is missing required sections: "
            + ", ".join(missing)
        )

    lowered = text.casefold()
    if not any(marker.casefold() in lowered for marker in RECOVERY_PROMOTION_TARGET_MARKERS):
        errors.append(
            "Recovery proposal must explicitly name at least one promotion target such as "
            "rolling_summary.md, context_brief.md, or daily_logs/."
        )

    return errors


def validate_recovery_review_text(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["Recovery review content is empty."]

    missing = missing_recovery_headings(text, RECOVERY_REVIEW_REQUIRED_HEADINGS)
    if missing:
        errors.append(
            "Recovery review is missing required sections: "
            + ", ".join(missing)
        )

    lowered = text.casefold()
    if not any(marker.casefold() in lowered for marker in RECOVERY_REVIEW_HINT_MARKERS):
        errors.append(
            "Recovery review must explicitly record hint-only handling, even if the conclusion is that no items remain hint-only."
        )

    return errors


def detect_update_protocol_time_policy_cues(text: str) -> list[str]:
    return protocol_sections.detect_update_protocol_time_policy_cues(text)


def load_managed_assets_metadata(*, supported_dynamic_asset_rule_kinds: set[str]) -> dict:
    try:
        payload = json.loads(MANAGED_ASSETS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing managed assets file: {MANAGED_ASSETS_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Malformed managed assets file: {MANAGED_ASSETS_PATH}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Managed assets file is not valid UTF-8: {MANAGED_ASSETS_PATH}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Managed assets file must be a JSON object: {MANAGED_ASSETS_PATH}")

    required = {
        "version",
        "required_files",
        "optional_files",
        "required_directories",
        "managed_directories",
        "dynamic_file_rules",
    }
    missing = sorted(required.difference(payload.keys()))
    if missing:
        raise RuntimeError(
            f"Managed assets file is missing required fields {missing}: {MANAGED_ASSETS_PATH}"
        )

    version = payload["version"]
    if not isinstance(version, int) or version < 1:
        raise RuntimeError(f"Managed assets version must be a positive integer: {MANAGED_ASSETS_PATH}")

    required_files = _load_relative_path_list(payload, field="required_files", source_path=MANAGED_ASSETS_PATH)
    optional_files = _load_relative_path_list(payload, field="optional_files", source_path=MANAGED_ASSETS_PATH)
    required_directories = _load_relative_path_list(
        payload, field="required_directories", source_path=MANAGED_ASSETS_PATH
    )
    managed_directories = _load_relative_path_list(
        payload, field="managed_directories", source_path=MANAGED_ASSETS_PATH
    )
    required_file_set = set(required_files)
    optional_file_set = set(optional_files)
    required_directory_set = set(required_directories)
    managed_directory_set = set(managed_directories)
    all_directory_set = required_directory_set | managed_directory_set

    overlap = sorted(required_file_set.intersection(optional_file_set))
    if overlap:
        raise RuntimeError(
            f"required_files and optional_files must be disjoint, found {overlap}: {MANAGED_ASSETS_PATH}"
        )

    file_directory_overlap = sorted((required_file_set | optional_file_set).intersection(all_directory_set))
    if file_directory_overlap:
        raise RuntimeError(
            "managed asset file paths and directory paths must be disjoint, found "
            f"{file_directory_overlap}: {MANAGED_ASSETS_PATH}"
        )

    dynamic_rules = payload["dynamic_file_rules"]
    if not isinstance(dynamic_rules, list):
        raise RuntimeError(f"dynamic_file_rules must be a list: {MANAGED_ASSETS_PATH}")
    normalized_rules: list[dict[str, str]] = []
    seen_rules: set[tuple[str, str]] = set()
    for item in dynamic_rules:
        if not isinstance(item, dict):
            raise RuntimeError(f"dynamic_file_rules must contain objects: {MANAGED_ASSETS_PATH}")
        base_dir = item.get("base_dir")
        kind = item.get("kind")
        if not isinstance(base_dir, str) or not base_dir.strip():
            raise RuntimeError(f"dynamic_file_rules.base_dir must be a non-empty string: {MANAGED_ASSETS_PATH}")
        if not isinstance(kind, str) or kind not in supported_dynamic_asset_rule_kinds:
            raise RuntimeError(
                "dynamic_file_rules.kind must be one of "
                f"{sorted(supported_dynamic_asset_rule_kinds)}: {MANAGED_ASSETS_PATH}"
            )
        normalized_base_dir = PurePosixPath(base_dir.strip()).as_posix()
        if normalized_base_dir in {".", ""} or normalized_base_dir.startswith("../") or normalized_base_dir.startswith("/"):
            raise RuntimeError(
                f"dynamic_file_rules contains an invalid base_dir '{base_dir}': {MANAGED_ASSETS_PATH}"
            )
        if normalized_base_dir not in all_directory_set:
            raise RuntimeError(
                "dynamic_file_rules.base_dir must reference a declared required_directories or managed_directories entry, "
                f"got '{normalized_base_dir}': {MANAGED_ASSETS_PATH}"
            )
        rule_key = (normalized_base_dir, kind)
        if rule_key in seen_rules:
            raise RuntimeError(
                f"dynamic_file_rules contains a duplicate rule {rule_key}: {MANAGED_ASSETS_PATH}"
            )
        seen_rules.add(rule_key)
        normalized_rules.append({"base_dir": normalized_base_dir, "kind": kind})

    return {
        "version": version,
        "required_files": required_files,
        "optional_files": optional_files,
        "required_directories": required_directories,
        "managed_directories": managed_directories,
        "dynamic_file_rules": normalized_rules,
    }


def validate_contract_registry_alignment(package_metadata: dict, contract_registry: dict) -> None:
    expected_pairs = (
        (
            "protocol_version",
            package_metadata["protocol_version"],
            contract_registry["protocol"]["current"],
        ),
        (
            "supported_protocol_versions",
            package_metadata["supported_protocol_versions"],
            contract_registry["protocol"]["supported"],
        ),
        (
            "supported_workspace_languages",
            package_metadata["supported_workspace_languages"],
            contract_registry["workspace"]["languages"],
        ),
        (
            "supported_bridge_targets",
            package_metadata["supported_bridge_targets"],
            contract_registry["workspace"]["bridge_targets"],
        ),
    )
    for field_name, metadata_value, registry_value in expected_pairs:
        if metadata_value != registry_value:
            raise RuntimeError(
                f"package metadata field '{field_name}' must stay aligned with contract registry"
            )


COMMON_BOOTSTRAP_ERROR: RuntimeError | None = None

try:
    PACKAGE_METADATA = load_package_metadata()
    CONTRACT_REGISTRY_PATH = protocol_contracts.CONTRACT_REGISTRY_PATH
    CONTRACT_SCHEMA_PATH = protocol_contracts.CONTRACT_SCHEMA_PATH
    CONTRACT_SCHEMA = protocol_contracts.CONTRACT_SCHEMA
    CONTRACT_REGISTRY = protocol_contracts.CONTRACT_REGISTRY
    if protocol_contracts.CONTRACT_BOOTSTRAP_ERROR is None:
        validate_contract_registry_alignment(PACKAGE_METADATA, CONTRACT_REGISTRY)
    MANAGED_ASSETS_METADATA = load_managed_assets_metadata(
        supported_dynamic_asset_rule_kinds=set(protocol_contracts.SUPPORTED_DYNAMIC_ASSET_RULE_KINDS)
    )
except RuntimeError as exc:
    COMMON_BOOTSTRAP_ERROR = exc
    PACKAGE_METADATA = {
        "package_name": "recallloom",
        "display_name": "RecallLoom",
        "package_version": "0.0.0-bootstrap-error",
        "protocol_version": protocol_contracts.CURRENT_PROTOCOL_VERSION,
        "supported_protocol_versions": sorted(protocol_contracts.SUPPORTED_PROTOCOL_VERSIONS),
        "minimum_python_version": "3.10",
        "supported_workspace_languages": sorted(protocol_contracts.SUPPORTED_WORKSPACE_LANGUAGES),
        "supported_bridge_targets": sorted(protocol_contracts.ROOT_ENTRY_CANDIDATE_STRINGS),
    }
    CONTRACT_REGISTRY_PATH = protocol_contracts.CONTRACT_REGISTRY_PATH
    CONTRACT_SCHEMA_PATH = protocol_contracts.CONTRACT_SCHEMA_PATH
    CONTRACT_SCHEMA = protocol_contracts.CONTRACT_SCHEMA
    CONTRACT_REGISTRY = protocol_contracts.CONTRACT_REGISTRY
    MANAGED_ASSETS_METADATA = {
        "required_files": [],
        "optional_files": [],
        "required_directories": [],
        "managed_directories": [],
        "dynamic_file_rules": [],
    }
PACKAGE_NAME = PACKAGE_METADATA["package_name"]
PACKAGE_VERSION = PACKAGE_METADATA["package_version"]


# ---------------------------------------------------------------------------
# Safe runtime writer attribution for public helper JSON.
# Protocol 1.0 markers and state still persist only the canonical writer_id.
# ---------------------------------------------------------------------------

UNKNOWN_WRITER_ID = "unknown"
WRITER_ID_ENV = "RECALLLOOM_WRITER_ID"
WRITER_ID_MAX_LENGTH = 64
_SAFE_WRITER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ :\-]{0,63}$")
_WRITER_ID_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_WRITER_ID_ACCOUNT_IDENTIFIER_RE = re.compile(
    r"(?i)(?:^|[._ +:\-])(?:account|acct|tenant|workspace|user|profile)(?=[A-Za-z0-9._ +:\-]|$)"
)
_WRITER_ID_PRIVATE_IDENTIFIER_RE = re.compile(
    r"(?i)(?:^|[._ +:\-])(?:private|session|sid|transcript|conversation|thread|trace)(?=[A-Za-z0-9._ +:\-]|$)"
)
_WRITER_ID_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential)\s*[:=]\s*['\"]?[^\s,'\"]+"
)
_WRITER_ID_SECRET_WORD_RE = re.compile(
    r"(?i)(?:^|[._ +:\-])(?:api[_-]?key|token|secret|password|credential)(?=[A-Za-z0-9._ +:\-]|$)"
)
_WRITER_ID_COMMON_TOKEN_RE = re.compile(
    r"\b(ghp_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,})\b"
)
_WRITER_ID_OPENAI_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_WRITER_ID_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]+")
_WRITER_ID_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_SENSITIVE_WRITER_PREFIXES = (
    "sk-",
    "xoxb-",
    "xoxp-",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "github_pat_",
    "bearer ",
)


@dataclass(frozen=True)
class WriterAttribution:
    writer_id: str
    writer_id_source: str
    invocation_surface: str
    attribution_confidence: str
    attribution_reason: str

    def public_fields(self) -> dict[str, str]:
        return {
            "writer_id": self.writer_id,
            "writer_id_source": self.writer_id_source,
            "invocation_surface": self.invocation_surface,
            "attribution_confidence": self.attribution_confidence,
            "attribution_reason": self.attribution_reason,
        }


def get_default_writer_id() -> str:
    """
    Return the protocol-safe fallback writer-id for the current session.

    Runtime host/model environment values are intentionally not trusted as
    writer identity. Helpers that need attribution should call
    ``resolve_writer_attribution`` so the public JSON can state the source.
    """
    return UNKNOWN_WRITER_ID


DISPLAY_NAME = get_default_writer_id()
CURRENT_PROTOCOL_VERSION = protocol_contracts.CURRENT_PROTOCOL_VERSION
SUPPORTED_PROTOCOL_VERSIONS = protocol_contracts.SUPPORTED_PROTOCOL_VERSIONS
MINIMUM_PYTHON_VERSION = PACKAGE_METADATA["minimum_python_version"]
MINIMUM_PYTHON_VERSION_PARTS = tuple(int(part) for part in MINIMUM_PYTHON_VERSION.split("."))
DEFAULT_WORKSPACE_LANGUAGE = protocol_contracts.DEFAULT_WORKSPACE_LANGUAGE
SUPPORTED_WORKSPACE_LANGUAGES = protocol_contracts.SUPPORTED_WORKSPACE_LANGUAGES
SUPPORTED_STORAGE_MODES = protocol_contracts.SUPPORTED_STORAGE_MODES
DAILY_LOGS_DIRNAME = protocol_contracts.DAILY_LOGS_DIRNAME
SUPPORTED_DYNAMIC_ASSET_RULE_KINDS = protocol_contracts.SUPPORTED_DYNAMIC_ASSET_RULE_KINDS
MANAGED_ASSET_REQUIRED_FILES = tuple(MANAGED_ASSETS_METADATA["required_files"])
MANAGED_ASSET_OPTIONAL_FILES = tuple(MANAGED_ASSETS_METADATA["optional_files"])
MANAGED_ASSET_REQUIRED_DIRECTORIES = tuple(MANAGED_ASSETS_METADATA["required_directories"])
MANAGED_ASSET_DIRECTORIES = tuple(MANAGED_ASSETS_METADATA["managed_directories"])
MANAGED_ASSET_DYNAMIC_RULES = tuple(MANAGED_ASSETS_METADATA["dynamic_file_rules"])

FILE_KEYS = protocol_contracts.FILE_KEYS


def is_required_storage_file(rel_path: str) -> bool:
    return PurePosixPath(rel_path).as_posix() in MANAGED_ASSET_REQUIRED_FILES


def is_optional_storage_file(rel_path: str) -> bool:
    return PurePosixPath(rel_path).as_posix() in MANAGED_ASSET_OPTIONAL_FILES


def is_required_storage_directory(rel_path: str) -> bool:
    return PurePosixPath(rel_path).as_posix() in MANAGED_ASSET_REQUIRED_DIRECTORIES

SECTION_KEYS = protocol_contracts.SECTION_KEYS
OPTIONAL_SECTION_KEYS = protocol_contracts.OPTIONAL_SECTION_KEYS
CONTEXT_BRIEF_RENDER_ORDER = protocol_contracts.CONTEXT_BRIEF_RENDER_ORDER
LABELS = protocol_contracts.LABELS

FILE_MARKER_TEMPLATE = protocol_contracts.FILE_MARKER_TEMPLATE
FILE_STATE_MARKER_TEMPLATE = protocol_contracts.FILE_STATE_MARKER_TEMPLATE
DAILY_LOG_ENTRY_MARKER_TEMPLATE = protocol_contracts.DAILY_LOG_ENTRY_MARKER_TEMPLATE
DAILY_LOG_SCAFFOLD_MARKER_TEMPLATE = protocol_contracts.DAILY_LOG_SCAFFOLD_MARKER_TEMPLATE
LAST_WRITER_MARKER_TEMPLATE = protocol_contracts.LAST_WRITER_MARKER_TEMPLATE
LAST_WRITER_RE = protocol_contracts.LAST_WRITER_RE
FILE_STATE_RE = protocol_contracts.FILE_STATE_RE
DAILY_LOG_ENTRY_RE = protocol_contracts.DAILY_LOG_ENTRY_RE
DAILY_LOG_SCAFFOLD_RE = protocol_contracts.DAILY_LOG_SCAFFOLD_RE
DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
FILE_MARKER_RE = protocol_contracts.FILE_MARKER_RE
SECTION_MARKER_RE = protocol_contracts.SECTION_MARKER_RE
BRIDGE_START = protocol_contracts.BRIDGE_START
BRIDGE_END = protocol_contracts.BRIDGE_END
EXCLUDE_BLOCK_START = protocol_contracts.EXCLUDE_BLOCK_START
EXCLUDE_BLOCK_END = protocol_contracts.EXCLUDE_BLOCK_END
ROOT_ENTRY_CANDIDATES = protocol_contracts.ROOT_ENTRY_CANDIDATES
ROOT_ENTRY_CANDIDATE_STRINGS = protocol_contracts.ROOT_ENTRY_CANDIDATE_STRINGS


def validate_tool_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigContractError("tool_name must be a non-empty string")
    if any(ch in value for ch in {"|", "]", "\n", "\r"}):
        raise ConfigContractError(
            "tool_name may not contain '|', ']', or line-break characters because it is embedded in machine-readable markers"
        )
    return value.strip()


def validate_writer_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigContractError("writer_id must be a non-empty string")
    if any(ch in value for ch in {"|", "]", "\n", "\r"}):
        raise ConfigContractError(
            "writer_id may not contain '|', ']', or line-break characters because it is embedded in machine-readable markers"
        )
    return value.strip()


def _token_like_writer_id(value: str) -> bool:
    lowered = value.strip().casefold()
    if any(lowered.startswith(prefix) for prefix in _SENSITIVE_WRITER_PREFIXES):
        return True
    return any(
        pattern.search(value)
        for pattern in (
            _WRITER_ID_SECRET_ASSIGNMENT_RE,
            _WRITER_ID_SECRET_WORD_RE,
            _WRITER_ID_COMMON_TOKEN_RE,
            _WRITER_ID_OPENAI_TOKEN_RE,
            _WRITER_ID_BEARER_TOKEN_RE,
        )
    )


def _unsafe_writer_id_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return "invalid_writer_id_type"
    normalized = value.strip()
    if not normalized:
        return "empty_writer_id"
    if len(normalized) > WRITER_ID_MAX_LENGTH:
        return "overlong_writer_id"
    if normalized.startswith("~") or "/" in normalized or "\\" in normalized or _WRITER_ID_DRIVE_PATH_RE.match(normalized):
        return "path_like_writer_id"
    if _WRITER_ID_EMAIL_RE.search(normalized) or _WRITER_ID_ACCOUNT_IDENTIFIER_RE.search(normalized):
        return "account_identifier_writer_id"
    if _token_like_writer_id(normalized):
        return "token_like_writer_id"
    if _WRITER_ID_PRIVATE_IDENTIFIER_RE.search(normalized):
        return "private_identifier_writer_id"
    if not _SAFE_WRITER_ID_RE.fullmatch(normalized):
        return "unsafe_writer_id_characters"
    try:
        validate_writer_id(normalized)
    except ConfigContractError:
        return "marker_unsafe_writer_id"
    return None


def normalize_safe_writer_id(value: str) -> str | None:
    if _unsafe_writer_id_reason(value) is not None:
        return None
    return validate_writer_id(value.strip())


def validate_explicit_writer_id(value: str, *, marker_role: str = "writer_id") -> str:
    writer_id = normalize_safe_writer_id(value)
    if writer_id is None:
        return UNKNOWN_WRITER_ID
    if marker_role == "tool_name":
        return validate_tool_name(writer_id)
    return validate_writer_id(writer_id)


def resolve_writer_attribution(
    *,
    explicit_writer_id: str | None,
    invocation_surface: str,
    explicit_marker_role: str = "writer_id",
    wrapper_metadata: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
) -> WriterAttribution:
    env = os.environ if env is None else env
    if explicit_writer_id is not None:
        writer_id = validate_explicit_writer_id(explicit_writer_id, marker_role=explicit_marker_role)
        if writer_id == UNKNOWN_WRITER_ID:
            return WriterAttribution(
                writer_id=UNKNOWN_WRITER_ID,
                writer_id_source="explicit_cli",
                invocation_surface=invocation_surface,
                attribution_confidence="low",
                attribution_reason="explicit_cli_rejected",
            )
        return WriterAttribution(
            writer_id=writer_id,
            writer_id_source="explicit_cli",
            invocation_surface=invocation_surface,
            attribution_confidence="high",
            attribution_reason="explicit_cli_valid",
        )

    if isinstance(wrapper_metadata, dict):
        wrapper_attribution = wrapper_metadata.get("writer_attribution")
        if isinstance(wrapper_attribution, dict):
            writer_id_raw = wrapper_attribution.get("writer_id")
            writer_id = writer_id_raw if isinstance(writer_id_raw, str) else UNKNOWN_WRITER_ID
            return WriterAttribution(
                writer_id=writer_id,
                writer_id_source="wrapper_metadata",
                invocation_surface=str(wrapper_attribution.get("invocation_surface") or invocation_surface),
                attribution_confidence=str(wrapper_attribution.get("confidence") or "medium"),
                attribution_reason=str(wrapper_attribution.get("reason") or "wrapper_metadata_valid"),
            )

    env_writer_id = normalize_safe_writer_id(env.get(WRITER_ID_ENV, ""))
    if env_writer_id is not None:
        return WriterAttribution(
            writer_id=env_writer_id,
            writer_id_source="env_allowlist",
            invocation_surface=invocation_surface,
            attribution_confidence="medium",
            attribution_reason="env_allowlist_valid",
        )

    return WriterAttribution(
        writer_id=UNKNOWN_WRITER_ID,
        writer_id_source="fallback_unknown",
        invocation_surface=invocation_surface,
        attribution_confidence="low",
        attribution_reason="no_safe_signal",
    )


# ---------------------------------------------------------------------------
# Safe wrapper metadata for additive public helper JSON.
# This never writes sidecar state or protocol markers.
# ---------------------------------------------------------------------------

WRAPPER_METADATA_SCHEMA_VERSION = "wrapper-metadata.public.v1"
WRAPPER_METADATA_ALLOWED_KEYS = frozenset(
    {
        "client_name",
        "confidence",
        "detected_by",
        "host",
        "host_id",
        "host_version",
        "invocation_surface",
        "surface",
        "local_wrapper_version",
        "metadata_schema_version",
        "session_id_hash",
        "task_id_hash",
        "thread_id_hash",
        "unknown_reason",
        "writer_id",
    }
)
WRAPPER_METADATA_MAX_VALUE_LENGTH = 64
_SAFE_WRAPPER_METADATA_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ -]{0,63}$")
_WRAPPER_METADATA_LOCAL_VERSION_RE = re.compile(
    r"^v?\d+(?:\.\d+){1,3}(?P<suffix>[-+][A-Za-z0-9][A-Za-z0-9.-]{0,39})?$"
)
_WRAPPER_METADATA_LOCAL_VERSION_SAFE_SUFFIX_TOKEN_RE = re.compile(
    r"(?i)^(?:alpha|beta|build|canary|ci|dev|dirty|local|nightly|pkg|preview|rc|release|"
    r"snapshot|test|wrapper|v?\d+|[0-9a-f]{1,12})$"
)
_WRAPPER_METADATA_HEX_FINGERPRINT_RE = re.compile(r"(?i)^[a-f0-9]{32,}$")
_WRAPPER_METADATA_COLON_FINGERPRINT_RE = re.compile(r"(?i)^(?:[a-f0-9]{2}:){15,}[a-f0-9]{2}$")
_WRAPPER_METADATA_URL_RE = re.compile(r"(?i)\b(?:https?|file)://")
_WRAPPER_METADATA_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_WRAPPER_METADATA_ACCOUNT_IDENTIFIER_RE = re.compile(
    r"(?i)(?:^|[._ +\-])(?:account|acct|tenant|workspace|user|profile)(?=[A-Za-z0-9._ +\-]|$)"
)
_WRAPPER_METADATA_PRIVATE_IDENTIFIER_RE = re.compile(
    r"(?i)(?:^|[._ +\-])(?:private|session|sid|transcript|conversation|thread|trace)(?=[A-Za-z0-9._ +\-]|$)"
)
_WRAPPER_METADATA_ARTIFACT_WORD_RE = re.compile(
    r"(?i)(?:^|[._ +\-])(?:artifact|source)(?=[A-Za-z0-9._ +\-]|$)"
)
_WRAPPER_METADATA_ARTIFACT_IDENTIFIER_RE = re.compile(
    r"(?i)^(?:P\d{3,}|WA|SRC|FI|UF|DG|LC|HM|HSC|SC|PS|RC|RO|RAG|MA|DOCSYNC|ENTRY|WRAP)(?:[-_][A-Z0-9]+)+$"
)
_WRAPPER_METADATA_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential)\s*[:=]\s*['\"]?[^\s,'\"]+"
)
_WRAPPER_METADATA_COMMON_TOKEN_RE = re.compile(
    r"\b(ghp_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,})\b"
)
_WRAPPER_METADATA_OPENAI_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_WRAPPER_METADATA_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]+")
_WRAPPER_METADATA_PRIVATE_PATH_RE = re.compile(r"(^|[\s\"'=])(?:~|/|[A-Za-z]:[\\/])")
_WRAPPER_METADATA_HASH_REF_RE = re.compile(r"^(?:hmac:[a-f0-9]{16,64}|ephemeral:[A-Za-z0-9._+-]{1,48}|none)$")

_WRAPPER_HOST_ALIASES = {
    "claude": "claude_code",
    "claude_code": "claude_code",
    "claude_desktop": "claude_code",
    "codex": "codex",
    "codex_cli": "codex",
    "codex_desktop": "codex",
    "opencode": "opencode",
    "open_code": "opencode",
}
_WRAPPER_GOOGLE_TRANSITION_ALIASES = {
    "antigravity_2_0",
    "antigravity_cli",
    "gemini",
    "gemini_cli",
    "gemini_cli_legacy_transition",
    "google_ai",
    "google_ai_studio",
    "google_cli",
    "google_gemini",
    "google_gemini_cli",
    "google_genai",
    "google_genai_cli",
}
_WRAPPER_SURFACE_ALIASES = {
    "api": "api",
    "cli": "cli",
    "cli_json": "cli_json",
    "direct_helper": "direct_helper",
    "direct_script": "direct_script",
    "dispatcher": "dispatcher",
    "entry_json": "cli_json",
    "helper": "direct_helper",
    "host_wrapper": "native_command",
    "json_cli": "cli_json",
    "native_command": "native_command",
    "native_wrapper": "native_command",
    "stdin_json": "cli_json",
    "unknown": "unknown",
    "wrapper": "native_command",
}
_WRAPPER_HOST_ID_VALUES = {"codex", "claude_code", "google_transition", "opencode", "generic_agent", "unknown"}
_WRAPPER_CLIENT_NAME_VALUES = {
    "codex_desktop",
    "codex_cli",
    "codex_exec",
    "claude_code",
    "antigravity_2_0",
    "antigravity_cli",
    "gemini_cli",
    "gemini_cli_legacy_transition",
    "opencode",
    "unknown",
}
_WRAPPER_DETECTED_BY_VALUES = {"explicit_cli", "wrapper_metadata", "env_allowlist", "fallback_unknown"}
_WRAPPER_CONFIDENCE_VALUES = {"high", "medium", "low"}
_WRAPPER_UNKNOWN_REASON_VALUES = {
    "no_safe_signal",
    "conflicting_safe_signals",
    "host_transition_fallback",
    "privacy_filtered",
}


class WrapperMetadataSecurityError(ValueError):
    """Public-safe wrapper metadata rejection."""

    def __init__(self, message: str, *, details: dict[str, object]):
        super().__init__(message)
        self.details = details


def _wrapper_metadata_failure_details(
    *,
    reason_code: str,
    field: str | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "input_role": "wrapper_metadata",
        "reason_code": reason_code,
        "allowed_keys": sorted(WRAPPER_METADATA_ALLOWED_KEYS),
        "raw_metadata_public_safe": False,
        "side_effect": "none",
    }
    if field is not None:
        details["field"] = field
    if extra:
        details.update(extra)
    return details


def _raise_wrapper_metadata_security_error(
    *,
    reason_code: str,
    field: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    raise WrapperMetadataSecurityError(
        "Wrapper metadata was rejected by the public-safe allowlist.",
        details=_wrapper_metadata_failure_details(
            reason_code=reason_code,
            field=field,
            extra=extra,
        ),
    )


def _wrapper_metadata_key_label(value: object) -> str | None:
    return value if isinstance(value, str) and value in WRAPPER_METADATA_ALLOWED_KEYS else None


def _canonical_wrapper_token(value: str) -> str:
    token = value.strip().casefold()
    token = re.sub(r"[^a-z0-9]+", "_", token)
    token = re.sub(r"_+", "_", token).strip("_")
    return token or "unknown"


def _wrapper_metadata_value_is_path_like(value: str) -> bool:
    if _WRAPPER_METADATA_URL_RE.search(value):
        return True
    if _WRAPPER_METADATA_PRIVATE_PATH_RE.search(value):
        return True
    return "/" in value or "\\" in value


def _wrapper_metadata_value_is_token_like(value: str) -> bool:
    lowered = value.strip().casefold()
    if any(lowered.startswith(prefix) for prefix in _SENSITIVE_WRITER_PREFIXES):
        return True
    return any(
        pattern.search(value)
        for pattern in (
            _WRAPPER_METADATA_SECRET_ASSIGNMENT_RE,
            _WRAPPER_METADATA_COMMON_TOKEN_RE,
            _WRAPPER_METADATA_OPENAI_TOKEN_RE,
            _WRAPPER_METADATA_BEARER_TOKEN_RE,
        )
    )


def _wrapper_metadata_value_is_fingerprint(value: str) -> bool:
    normalized = value.strip()
    return bool(
        _WRAPPER_METADATA_HEX_FINGERPRINT_RE.fullmatch(normalized)
        or _WRAPPER_METADATA_COLON_FINGERPRINT_RE.fullmatch(normalized)
    )


def _wrapper_metadata_private_identifier_reason(value: str) -> str | None:
    normalized = value.strip()
    artifact_token = re.sub(r"[ _]+", "-", normalized).upper()
    if _WRAPPER_METADATA_ARTIFACT_IDENTIFIER_RE.fullmatch(artifact_token):
        return "artifact_identifier_value"
    if _WRAPPER_METADATA_ARTIFACT_WORD_RE.search(normalized):
        return "artifact_identifier_value"
    if _WRAPPER_METADATA_ACCOUNT_IDENTIFIER_RE.search(normalized):
        return "account_identifier_value"
    if _WRAPPER_METADATA_PRIVATE_IDENTIFIER_RE.search(normalized):
        return "private_identifier_value"
    return None


def _validate_local_wrapper_version_surface(field: str, value: str) -> None:
    if field != "local_wrapper_version" or not value:
        return
    match = _WRAPPER_METADATA_LOCAL_VERSION_RE.fullmatch(value)
    if not match:
        _raise_wrapper_metadata_security_error(
            reason_code="local_wrapper_version_not_version_like",
            field=field,
        )
    suffix = match.group("suffix")
    if suffix is None:
        return
    suffix_tokens = re.findall(r"[A-Za-z0-9]+", suffix[1:])
    if not suffix_tokens or any(
        not _WRAPPER_METADATA_LOCAL_VERSION_SAFE_SUFFIX_TOKEN_RE.fullmatch(token)
        for token in suffix_tokens
    ):
        _raise_wrapper_metadata_security_error(
            reason_code="local_wrapper_version_not_version_like",
            field=field,
        )


def _validate_wrapper_metadata_enum(field: str, value: str, allowed: set[str]) -> str:
    token = _canonical_wrapper_token(value)
    if token not in allowed:
        _raise_wrapper_metadata_security_error(
            reason_code="enum_value_out_of_range",
            field=field,
        )
    return token


def _validate_wrapper_metadata_writer_id(field: str, value: object) -> str:
    if not isinstance(value, str):
        _raise_wrapper_metadata_security_error(
            reason_code="invalid_value_type",
            field=field,
            extra={"expected_type": "string"},
        )
    writer_id = normalize_safe_writer_id(value)
    if writer_id is None:
        _raise_wrapper_metadata_security_error(
            reason_code=_unsafe_writer_id_reason(value) or "unsafe_explicit_writer_id",
            field=field,
        )
    return writer_id


def _validate_wrapper_metadata_hash_ref(field: str, value: object) -> str:
    if not isinstance(value, str):
        _raise_wrapper_metadata_security_error(
            reason_code="invalid_value_type",
            field=field,
            extra={"expected_type": "string"},
        )
    normalized = value.strip()
    if len(normalized) > WRAPPER_METADATA_MAX_VALUE_LENGTH:
        _raise_wrapper_metadata_security_error(
            reason_code="overlong_value",
            field=field,
            extra={"max_value_length": WRAPPER_METADATA_MAX_VALUE_LENGTH},
        )
    if not _WRAPPER_METADATA_HASH_REF_RE.fullmatch(normalized):
        _raise_wrapper_metadata_security_error(reason_code="unsafe_hash_reference", field=field)
    return normalized


def _validate_wrapper_metadata_string(field: str, value: object) -> str:
    if not isinstance(value, str):
        _raise_wrapper_metadata_security_error(
            reason_code="invalid_value_type",
            field=field,
            extra={"expected_type": "string"},
        )
    normalized = value.strip()
    if len(normalized) > WRAPPER_METADATA_MAX_VALUE_LENGTH:
        _raise_wrapper_metadata_security_error(
            reason_code="overlong_value",
            field=field,
            extra={"max_value_length": WRAPPER_METADATA_MAX_VALUE_LENGTH},
        )
    if not normalized:
        return ""
    if _WRAPPER_METADATA_EMAIL_RE.search(normalized):
        _raise_wrapper_metadata_security_error(reason_code="account_identifier_value", field=field)
    if _wrapper_metadata_value_is_token_like(normalized):
        _raise_wrapper_metadata_security_error(reason_code="token_like_value", field=field)
    identifier_reason = _wrapper_metadata_private_identifier_reason(normalized)
    if identifier_reason is not None:
        _raise_wrapper_metadata_security_error(reason_code=identifier_reason, field=field)
    if _wrapper_metadata_value_is_path_like(normalized):
        _raise_wrapper_metadata_security_error(reason_code="path_like_value", field=field)
    if _wrapper_metadata_value_is_fingerprint(normalized):
        _raise_wrapper_metadata_security_error(reason_code="fingerprint_value", field=field)
    if not _SAFE_WRAPPER_METADATA_VALUE_RE.fullmatch(normalized):
        _raise_wrapper_metadata_security_error(reason_code="unsafe_value_characters", field=field)
    _validate_local_wrapper_version_surface(field, normalized)
    return normalized


def _validate_wrapper_metadata_field(field: str, value: object) -> str:
    if field == "writer_id":
        return _validate_wrapper_metadata_writer_id(field, value)
    if field == "host_id":
        return _validate_wrapper_metadata_enum(field, _validate_wrapper_metadata_string(field, value), _WRAPPER_HOST_ID_VALUES)
    if field == "client_name":
        return _validate_wrapper_metadata_enum(
            field,
            _validate_wrapper_metadata_string(field, value),
            _WRAPPER_CLIENT_NAME_VALUES,
        )
    if field == "invocation_surface":
        return _validate_wrapper_metadata_enum(
            field,
            _validate_wrapper_metadata_string(field, value),
            set(_WRAPPER_SURFACE_ALIASES),
        )
    if field == "detected_by":
        return _validate_wrapper_metadata_enum(
            field,
            _validate_wrapper_metadata_string(field, value),
            _WRAPPER_DETECTED_BY_VALUES,
        )
    if field == "confidence":
        return _validate_wrapper_metadata_enum(
            field,
            _validate_wrapper_metadata_string(field, value),
            _WRAPPER_CONFIDENCE_VALUES,
        )
    if field == "unknown_reason":
        return _validate_wrapper_metadata_enum(
            field,
            _validate_wrapper_metadata_string(field, value),
            _WRAPPER_UNKNOWN_REASON_VALUES,
        )
    if field == "metadata_schema_version":
        normalized = _validate_wrapper_metadata_string(field, value)
        if normalized != "1":
            _raise_wrapper_metadata_security_error(
                reason_code="metadata_schema_version_unsupported",
                field=field,
            )
        return normalized
    if field in {"session_id_hash", "task_id_hash", "thread_id_hash"}:
        return _validate_wrapper_metadata_hash_ref(field, value)
    if field == "host_version":
        normalized = _validate_wrapper_metadata_string(field, value)
        if normalized != "unknown":
            _validate_local_wrapper_version_surface("local_wrapper_version", normalized)
        return normalized
    return _validate_wrapper_metadata_string(field, value)


def _load_wrapper_metadata(raw_json: str) -> dict[str, str]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        _raise_wrapper_metadata_security_error(
            reason_code="malformed_json",
            extra={"json_error_line": exc.lineno, "json_error_column": exc.colno},
        )
    if not isinstance(payload, dict):
        _raise_wrapper_metadata_security_error(
            reason_code="top_level_not_object",
            extra={"expected_type": "object"},
        )

    unknown_key_count = sum(1 for key in payload if key not in WRAPPER_METADATA_ALLOWED_KEYS)
    if unknown_key_count:
        _raise_wrapper_metadata_security_error(
            reason_code="unknown_key",
            extra={"unknown_key_count": unknown_key_count},
        )

    normalized: dict[str, str] = {}
    for raw_key, raw_value in payload.items():
        field = _wrapper_metadata_key_label(raw_key)
        if field is None:
            _raise_wrapper_metadata_security_error(reason_code="unknown_key")
        normalized_value = _validate_wrapper_metadata_field(field, raw_value)
        if normalized_value:
            normalized[field] = normalized_value
    return normalized


def _normalize_wrapper_host(value: str | None) -> tuple[str, str]:
    if not value:
        return "unknown", "absent"
    token = _canonical_wrapper_token(value)
    if token in _WRAPPER_HOST_ALIASES:
        return _WRAPPER_HOST_ALIASES[token], "normalized"
    if token in _WRAPPER_GOOGLE_TRANSITION_ALIASES:
        return "google_transition", "google_transition_alias"
    if token.startswith("google_") or token.startswith("gemini_google_"):
        return "google_transition", "google_transition_alias"
    return "unknown", "safe_fallback_unknown"


def _normalize_wrapper_surface(value: str | None) -> tuple[str, str]:
    if not value:
        return "unknown", "absent"
    token = _canonical_wrapper_token(value)
    if token in _WRAPPER_SURFACE_ALIASES:
        return _WRAPPER_SURFACE_ALIASES[token], "normalized"
    return "unknown", "safe_fallback_unknown"


def _wrapper_host_input(normalized: dict[str, str]) -> str | None:
    if normalized.get("host"):
        return normalized["host"]
    if normalized.get("host_id"):
        return normalized["host_id"]
    if normalized.get("client_name"):
        return normalized["client_name"]
    return None


def _wrapper_surface_input(normalized: dict[str, str]) -> str | None:
    return normalized.get("surface") or normalized.get("invocation_surface")


def _wrapper_writer_attribution(normalized: dict[str, str], *, invocation_surface: str) -> dict[str, str] | None:
    writer_id = normalized.get("writer_id")
    if writer_id is None:
        return None
    reason = normalized.get("unknown_reason") if writer_id == UNKNOWN_WRITER_ID else "wrapper_metadata_valid"
    return {
        "writer_id": writer_id,
        "writer_id_source": "wrapper_metadata",
        "detected_by": normalized.get("detected_by", "wrapper_metadata"),
        "confidence": normalized.get("confidence", "medium"),
        "reason": reason or "wrapper_metadata_valid",
        "invocation_surface": invocation_surface,
    }


def _wrapper_version_observation(local_wrapper_version: str | None) -> dict[str, str | bool]:
    base: dict[str, str | bool] = {
        "observed_package_version": PACKAGE_VERSION,
        "comparison_basis": "local_package_metadata_only",
        "release_status_claim": "not_evaluated",
    }
    if not local_wrapper_version:
        return {
            **base,
            "status": "absent",
        }
    status = (
        "matches_observed_package"
        if local_wrapper_version == PACKAGE_VERSION
        else "differs_from_observed_package"
    )
    return {
        **base,
        "local_wrapper_version": local_wrapper_version,
        "status": status,
    }


def normalize_wrapper_metadata_json(raw_json: str | None) -> dict[str, object] | None:
    if raw_json is None:
        return None
    normalized = _load_wrapper_metadata(raw_json)
    host, host_signal = _normalize_wrapper_host(_wrapper_host_input(normalized))
    surface, surface_signal = _normalize_wrapper_surface(_wrapper_surface_input(normalized))
    payload: dict[str, object] = {
        "schema_version": WRAPPER_METADATA_SCHEMA_VERSION,
        "host": host,
        "surface": surface,
        "host_signal": host_signal,
        "surface_signal": surface_signal,
        "version": _wrapper_version_observation(normalized.get("local_wrapper_version")),
        "consumer_migration_required": False,
        "privacy": {
            "raw_metadata_retained": False,
            "host_private_identifiers": "absent_or_rejected",
            "host_memory_truth_contribution": False,
        },
    }
    writer_attribution = _wrapper_writer_attribution(normalized, invocation_surface=surface)
    if writer_attribution is not None:
        payload["writer_attribution"] = writer_attribution
    return payload
WORKSPACE_LOCK_FILENAME = workspace_runtime.WORKSPACE_LOCK_FILENAME
STALE_LOCK_MAX_AGE_SECONDS = 6 * 3600

WorkspaceInfo = workspace_runtime.WorkspaceInfo
RecoveryWorkspaceInfo = workspace_runtime.RecoveryWorkspaceInfo
FileMarkerInfo = protocol_markers.FileMarkerInfo
FileStateInfo = protocol_markers.FileStateInfo
DailyLogEntryInfo = protocol_markers.DailyLogEntryInfo


@dataclass(frozen=True)
class ValidationFinding:
    level: str
    code: str
    message: str
    path: Path


@dataclass(frozen=True)
class DailyLogCursor:
    latest_file: str | None
    latest_entry_id: str | None
    latest_entry_seq: int | None
    entry_count: int
    latest_path: Path | None = None

    def as_state_fields(self) -> dict[str, object]:
        return {
            "latest_file": self.latest_file,
            "latest_entry_id": self.latest_entry_id,
            "latest_entry_seq": self.latest_entry_seq,
            "entry_count": self.entry_count,
        }


DAILY_LOG_CURSOR_STATE_KEYS = (
    "latest_file",
    "latest_entry_id",
    "latest_entry_seq",
    "entry_count",
)


StorageResolutionError = core_errors.StorageResolutionError
ConfigContractError = core_errors.ConfigContractError
EnvironmentContractError = core_errors.EnvironmentContractError
LockBusyError = core_errors.LockBusyError


class DailyLogCursorError(Exception):
    """Structured refusal for damaged latest daily-log cursor evidence."""

    failure_reason = "malformed_managed_file"

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        path: Path | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.path = path
        self.details = {
            "reason_code": reason_code,
            "side_effect": "none",
            **(details or {}),
        }
        if path is not None:
            self.details.setdefault("path", str(path))


class ManagedDirectorySafetyError(Exception):
    """Public-safe failure for managed directory boundary checks."""

    failure_reason = "malformed_managed_file"

    def __init__(
        self,
        *,
        reason_code: str,
        managed_directory: str,
        path: Path,
        project_root: Path,
    ) -> None:
        self.reason_code = reason_code
        self.managed_directory = managed_directory
        self.path = path
        self.project_root = project_root
        public_path = public_project_path(path, project_root=project_root) or managed_directory
        self.message = (
            "Managed recovery directory boundary check failed "
            f"(reason_code={reason_code}, path={public_path})."
        )
        super().__init__(self.message)

    @property
    def details(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "managed_directory": self.managed_directory,
            "path": str(self.path),
            "side_effect": "none",
        }


def _is_relative_to_path(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def ensure_managed_directory_chain(
    storage_root: Path,
    rel_parts: Iterable[str],
    *,
    project_root: Path,
    create: bool = True,
) -> Path:
    """Ensure a managed directory chain has no symlink or non-directory component."""

    rel_tuple = tuple(rel_parts)
    rel_label = "/".join(rel_tuple) or "managed_directory"
    if not rel_tuple or any(
        not part or part in {".", ".."} or "/" in part or "\\" in part for part in rel_tuple
    ):
        raise ManagedDirectorySafetyError(
            reason_code="invalid_managed_directory_name",
            managed_directory=rel_label,
            path=storage_root,
            project_root=project_root,
        )

    try:
        storage_resolved = storage_root.resolve(strict=True)
    except OSError as exc:
        raise ManagedDirectorySafetyError(
            reason_code="storage_root_unavailable",
            managed_directory=rel_label,
            path=storage_root,
            project_root=project_root,
        ) from exc

    current = storage_root
    for part in rel_tuple:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            if not create:
                raise ManagedDirectorySafetyError(
                    reason_code="managed_directory_missing",
                    managed_directory=rel_label,
                    path=current,
                    project_root=project_root,
                )
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise ManagedDirectorySafetyError(
                    reason_code="managed_directory_create_failed",
                    managed_directory=rel_label,
                    path=current,
                    project_root=project_root,
                ) from exc
            try:
                current_stat = current.lstat()
            except OSError as exc:
                raise ManagedDirectorySafetyError(
                    reason_code="managed_directory_inspect_failed",
                    managed_directory=rel_label,
                    path=current,
                    project_root=project_root,
                ) from exc
        except OSError as exc:
            raise ManagedDirectorySafetyError(
                reason_code="managed_directory_inspect_failed",
                managed_directory=rel_label,
                path=current,
                project_root=project_root,
            ) from exc

        if stat.S_ISLNK(current_stat.st_mode):
            raise ManagedDirectorySafetyError(
                reason_code="managed_directory_symlink",
                managed_directory=rel_label,
                path=current,
                project_root=project_root,
            )
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ManagedDirectorySafetyError(
                reason_code="managed_directory_not_directory",
                managed_directory=rel_label,
                path=current,
                project_root=project_root,
            )

        try:
            current_resolved = current.resolve(strict=True)
        except OSError as exc:
            raise ManagedDirectorySafetyError(
                reason_code="managed_directory_realpath_failed",
                managed_directory=rel_label,
                path=current,
                project_root=project_root,
            ) from exc
        if current_resolved != storage_resolved and not _is_relative_to_path(
            current_resolved,
            storage_resolved,
        ):
            raise ManagedDirectorySafetyError(
                reason_code="managed_directory_escape",
                managed_directory=rel_label,
                path=current,
                project_root=project_root,
            )

    return current


def normalize_start_path(raw_path: str | Path) -> Path:
    return workspace_runtime.normalize_start_path(raw_path)


def ensure_supported_python_version() -> None:
    current = sys.version_info[: len(MINIMUM_PYTHON_VERSION_PARTS)]
    if current < MINIMUM_PYTHON_VERSION_PARTS:
        raise EnvironmentContractError(
            "RecallLoom helper scripts require "
            f"Python {MINIMUM_PYTHON_VERSION}+; current interpreter is "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
    contract_bootstrap_error = protocol_contracts.contract_bootstrap_error_message()
    if contract_bootstrap_error is not None:
        raise EnvironmentContractError(contract_bootstrap_error)
    if COMMON_BOOTSTRAP_ERROR is not None:
        raise EnvironmentContractError(
            f"RecallLoom runtime bootstrap failed: {COMMON_BOOTSTRAP_ERROR}"
        )


def validate_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def today_iso() -> str:
    return date.today().isoformat()


def now_iso_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonicalize_managed_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            with suppress(FileNotFoundError):
                temp_path.unlink()
        raise


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def exit_with_cli_error(
    parser,
    *,
    json_mode: bool,
    exit_code: int,
    message: str,
    payload: dict | None = None,
) -> None:
    message = message.rstrip("\n")
    if json_mode:
        body = {"ok": False, "error": message}
        if payload:
            body.update(payload)
        project_root = body.get("project_root") if isinstance(body.get("project_root"), str) else None
        public_body = publicize_json_value(body, project_root=project_root)
        if isinstance(public_body, dict) and isinstance(payload, dict):
            raw_details = payload.get("details")
            public_details = public_body.get("details")
            raw_command = raw_details.get("command") if isinstance(raw_details, dict) else None
            if (
                isinstance(public_details, dict)
                and isinstance(raw_command, str)
                and raw_command in {
                    "append",
                    "archive",
                    "bridge",
                    "init",
                    "quick-summary",
                    "repair-daily-log-cursor",
                    "resume",
                    "status",
                    "sync-current-state-after-append",
                    "validate",
                    "write",
                }
            ):
                public_details["command"] = raw_command
        print(json.dumps(public_body if isinstance(public_body, dict) else body, ensure_ascii=False, indent=2))
        raise SystemExit(exit_code)
    public_message = shared_redact_public_text(message, project_root=None) or message
    parser.exit(exit_code, public_message + "\n")


def cli_failure_payload(
    reason: str,
    *,
    error: str | None = None,
    details: dict | None = None,
    findings: list | None = None,
    extra: dict | None = None,
) -> dict:
    normalized_details = dict(details or {})
    if reason in {"no_project_root", "not_project_root", "invalid_storage_boundary"}:
        inferred_project_root = normalized_details.get("project_root")
        if not isinstance(inferred_project_root, str) or not inferred_project_root.strip():
            raw_target = None
            argv = list(sys.argv[1:]) if len(sys.argv) > 1 else []
            if Path(sys.argv[0]).name == "recallloom.py" and argv:
                argv = argv[1:]
            if argv and not argv[0].startswith("-"):
                raw_target = argv[0]
            else:
                raw_target = "."
            try:
                normalized_details["project_root"] = str(normalize_start_path(raw_target))
            except StorageResolutionError:
                normalized_details["project_root"] = str(Path(raw_target).expanduser().resolve())
    return failure_payload(
        reason,
        language=preferred_failure_language(os.environ),
        error=error,
        details=normalized_details or None,
        findings=findings,
        extra=extra,
        script_name=Path(sys.argv[0]).name if sys.argv else None,
    )


def public_project_root_label(project_root: str | Path) -> str:
    return shared_public_project_root_label(project_root)


def public_project_path(
    path: str | Path | None,
    *,
    project_root: str | Path,
) -> str | None:
    return shared_public_project_path(path, project_root=project_root)


def display_project_root_label(project_root: str | Path) -> str:
    return shared_display_project_root_label(project_root)


def display_project_path(
    path: str | Path | None,
    *,
    project_root: str | Path,
) -> str | None:
    return shared_display_project_path(path, project_root=project_root)


def publicize_text_paths(
    text: str | None,
    *,
    project_root: str | Path | None,
) -> str | None:
    return shared_publicize_text_paths(text, project_root=project_root)


def public_json_payload(
    payload: dict,
    *,
    project_root: str | Path | None,
) -> dict:
    publicized = publicize_json_value(payload, project_root=project_root)
    return publicized if isinstance(publicized, dict) else payload


def _public_package_support_text(value: object, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    stripped = value.strip()
    if not stripped:
        return fallback
    if PACKAGE_SUPPORT_PRIVATE_TEXT_RE.search(stripped):
        return fallback
    if not PACKAGE_SUPPORT_PUBLIC_TEXT_RE.match(stripped):
        return fallback
    return stripped


def _public_package_support_reason_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if PACKAGE_SUPPORT_PUBLIC_REASON_RE.match(stripped):
        return stripped
    return "redacted"


def _public_package_support_update_hints(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    public: dict[str, str] = {}
    safe_hint_fallbacks = {
        "directory_install": (
            "Replace the installed RecallLoom skill directory with the latest package copy."
        ),
    }
    for raw_key, raw_hint in value.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not PACKAGE_SUPPORT_PUBLIC_HINT_KEY_RE.match(key):
            continue
        public[key] = _public_package_support_text(
            raw_hint,
            fallback=safe_hint_fallbacks.get(key, "redacted"),
        )
    return public


def public_package_support_payload(support: dict | None) -> dict | None:
    if support is None:
        return None
    allowed_keys = (
        "allowed",
        "action_name",
        "action_level",
        "package_support_state",
        "current_version",
        "latest_version",
        "minimum_mutating_version",
        "minimum_readonly_version",
        "advisory_level",
        "reason_code",
        "update_hints",
        "checked_date",
        "checked_at",
        "source",
        "cache_hit",
        "support_diagnostic_reason",
        "user_message",
        "disabled",
    )
    public = {key: support[key] for key in allowed_keys if key in support}
    public["user_message"] = _public_package_support_text(
        support.get("user_message"),
        fallback=user_message_for_state(str(public.get("package_support_state") or "unknown_offline")),
    )
    public["reason_code"] = _public_package_support_reason_code(public.get("reason_code"))
    public["update_hints"] = _public_package_support_update_hints(public.get("update_hints"))
    source = public.get("source")
    if isinstance(source, str):
        if source.startswith("file:"):
            public["source"] = "file"
        elif source.startswith("url:"):
            public["source"] = "url"
    return public


def exit_with_failure_contract(
    parser,
    *,
    json_mode: bool,
    exit_code: int,
    message: str,
    reason: str,
    details: dict | None = None,
    findings: list | None = None,
    extra: dict | None = None,
) -> None:
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=exit_code,
        message=message,
        payload=cli_failure_payload(
            reason,
            error=message,
            details=details,
            findings=findings,
            extra=extra,
        ),
    )


def cli_failure_payload_for_exception(
    exc: BaseException,
    *,
    default_reason: str,
    extra: dict | None = None,
) -> dict:
    reason = getattr(exc, "failure_reason", None) or default_reason
    return cli_failure_payload(reason, error=str(exc), extra=extra)


def enforce_package_support_gate(
    parser,
    *,
    json_mode: bool,
    action_name: str | None = None,
    action_level: str | None = None,
) -> dict:
    metadata = load_package_metadata()
    script_name = Path(sys.argv[0]).name
    action_name = action_name or script_name
    action_level = action_level or action_level_for_script(script_name)
    support = package_support_result(
        package_root=PACKAGE_ROOT,
        package_version=metadata["package_version"],
        action_name=action_name,
        action_level=action_level,
        advisory_url=metadata.get("support_advisory_url"),
        env=os.environ,
    )
    os.environ[SUPPORT_STATE_ENV] = json.dumps(
        public_package_support_payload(support) or {},
        ensure_ascii=False,
    )
    if support["allowed"]:
        return support
    public_support = public_package_support_payload(support)
    message = (
        public_support.get("user_message")
        if isinstance(public_support, dict) and isinstance(public_support.get("user_message"), str)
        else "RecallLoom package support gate blocked this action."
    )
    exit_with_cli_error(
        parser,
        json_mode=json_mode,
        exit_code=4,
        message=message,
        payload=cli_failure_payload(
            "package_support_blocked",
            error=message,
            details={
                "operation": "package_support_gate",
                "reason_code": (
                    public_support.get("reason_code")
                    if isinstance(public_support, dict)
                    else None
                ),
                "side_effect": "none",
                "package_support": public_support,
            },
            extra={"package_support": public_support},
        ),
    )


def startup_scratch_residue_report(
    project_root: str | Path,
    storage_root: str | Path,
    *,
    external_roots: Iterable[str | Path] | None = None,
) -> ScratchResidueReport:
    return scan_startup_scratch_residue(
        project_root=project_root,
        storage_root=storage_root,
        external_roots=external_roots,
    )


def startup_residue_report_for_sources(
    project_root: str | Path,
    storage_root: str | Path,
    *source_paths: str | Path | None,
) -> ScratchResidueReport:
    return startup_scratch_residue_report(
        project_root,
        storage_root,
        external_roots=external_scratch_roots_for_sources(*source_paths),
    )


def exit_if_startup_scratch_residue(
    parser,
    *,
    json_mode: bool,
    project_root: str | Path,
    storage_root: str | Path,
    external_roots: Iterable[str | Path] | None = None,
) -> dict | None:
    report = startup_scratch_residue_report(
        project_root,
        storage_root,
        external_roots=external_roots,
    )
    if report.blocked:
        public_report = report.public_dict()
        message = "RecallLoom startup scratch residue detected; no files were changed."
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "startup_residue_detected",
                error=message,
                details={
                    "project_root": "project_root",
                    "startup_residue_report": public_report,
                },
                findings=public_report["findings"],
                extra={"startup_residue_report": public_report},
            ),
        )
    return report.report_only_public_dict()


def exit_if_startup_scratch_residue_for_sources(
    parser,
    *,
    json_mode: bool,
    project_root: str | Path,
    storage_root: str | Path,
    source_paths: Iterable[str | Path | None],
) -> dict | None:
    return exit_if_startup_scratch_residue(
        parser,
        json_mode=json_mode,
        project_root=project_root,
        storage_root=storage_root,
        external_roots=external_scratch_roots_for_sources(*source_paths),
    )


def load_json(path: Path) -> dict:
    return json.loads(read_text(path))


def dump_json(path: Path, payload: dict) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_if_unchanged(path: Path, *, expected_text: str, new_text: str) -> None:
    current_text = read_text(path) if path.exists() else ""
    if current_text != expected_text:
        raise LockBusyError(f"Refusing to write {path} because the file changed after it was read.")
    write_text(path, new_text)


def restore_text_snapshot(path: Path, *, existed: bool, text: str) -> None:
    if existed:
        write_text(path, text)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def rollback_moved_files(applied_moves: Iterable[tuple[Path, Path]]) -> None:
    for source, target in reversed(list(applied_moves)):
        if not target.exists():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(source))


def project_lock_path(project_root: Path) -> Path:
    return workspace_runtime.project_lock_path(project_root)


def load_lock_payload(lock_path: Path) -> dict:
    return workspace_runtime.load_lock_payload(lock_path)


def parse_lock_timestamp(value: str | None) -> datetime | None:
    return workspace_runtime.parse_lock_timestamp(value)


def pid_is_alive(pid: int) -> bool:
    return workspace_runtime.pid_is_alive(pid)


def reclaim_stale_workspace_lock(lock_path: Path) -> bool:
    return workspace_runtime.reclaim_stale_workspace_lock(lock_path)


@contextmanager
def workspace_write_lock(project_root: Path, owner: str):
    with workspace_runtime.workspace_write_lock(project_root, owner) as lock_path:
        yield lock_path


def config_payload(
    *,
    storage_mode: str,
    workspace_language: str,
    created_by: str,
    created_at: str,
    protocol_version: str = CURRENT_PROTOCOL_VERSION,
) -> dict:
    return workspace_runtime.config_payload(
        storage_mode=storage_mode,
        workspace_language=workspace_language,
        created_by=created_by,
        created_at=created_at,
        protocol_version=protocol_version,
    )


def initial_workspace_state(
    *,
    tool_name: str,
    timestamp: str,
    git_exclude_mode: str,
    daily_log_cursor: dict[str, object] | None = None,
) -> dict:
    state = workspace_runtime.initial_workspace_state(
        tool_name=tool_name,
        timestamp=timestamp,
        git_exclude_mode=git_exclude_mode,
    )
    if daily_log_cursor is not None:
        state["daily_logs"].update(
            {key: daily_log_cursor.get(key) for key in DAILY_LOG_CURSOR_STATE_KEYS}
        )
    return state


def load_workspace_state(path: Path) -> dict:
    return workspace_runtime.load_workspace_state(path)


def validate_storage_mode(value: str) -> str:
    return workspace_runtime.validate_storage_mode(value)


def validate_workspace_language(value: str) -> str:
    return workspace_runtime.validate_workspace_language(value)


def validate_protocol_version(value: str) -> str:
    return workspace_runtime.validate_protocol_version(value)


def load_and_validate_config(
    path: Path,
    default_storage_mode: str,
    *,
    allow_unsupported_version: bool = False,
    allow_storage_mode_mismatch: bool = False,
) -> dict:
    return workspace_runtime.load_and_validate_config(
        path,
        default_storage_mode,
        allow_unsupported_version=allow_unsupported_version,
        allow_storage_mode_mismatch=allow_storage_mode_mismatch,
    )


def hidden_storage_root(project_root: Path) -> Path:
    return workspace_runtime.hidden_storage_root(project_root)


def visible_storage_root(project_root: Path) -> Path:
    return workspace_runtime.visible_storage_root(project_root)


def recovery_storage_roots(project_root: Path) -> list[Path]:
    return workspace_runtime.recovery_storage_roots(project_root)


def visible_root_has_sidecar_signals(storage_root: Path) -> bool:
    return workspace_runtime.visible_root_has_sidecar_signals(storage_root)


def looks_like_installable_package_dir(storage_root: Path) -> bool:
    return workspace_runtime.looks_like_installable_package_dir(storage_root)


def damaged_sidecar_reason(project_root: Path, storage_root: Path, storage_mode: str) -> str | None:
    return workspace_runtime.damaged_sidecar_reason(project_root, storage_root, storage_mode)


def is_recovery_storage_candidate(project_root: Path, storage_root: Path, storage_mode: str) -> bool:
    return workspace_runtime.is_recovery_storage_candidate(project_root, storage_root, storage_mode)


def infer_storage_mode_from_root(storage_root: Path) -> str:
    return workspace_runtime.infer_storage_mode_from_root(storage_root)


def find_recovery_workspace(
    start_path: str | Path,
    *,
    requested_storage_mode: str | None = None,
) -> RecoveryWorkspaceInfo | None:
    return workspace_runtime.find_recovery_workspace(
        start_path,
        requested_storage_mode=requested_storage_mode,
    )


def find_recovery_project_root(start_path: str | Path) -> Path:
    return workspace_runtime.find_recovery_project_root(start_path)


def storage_root_for_mode(project_root: Path, storage_mode: str) -> Path:
    return workspace_runtime.storage_root_for_mode(project_root, storage_mode)


def config_path_for_mode(project_root: Path, storage_mode: str) -> Path:
    return workspace_runtime.config_path_for_mode(project_root, storage_mode)


def file_path(workspace: WorkspaceInfo, file_key: str) -> Path:
    return workspace_runtime.file_path(workspace, file_key)


def file_marker(file_key: str, language: str, version: str = CURRENT_PROTOCOL_VERSION) -> str:
    return protocol_markers.file_marker(file_key, language, version)


def file_state_marker(
    *,
    revision: int,
    updated_at: str,
    writer_id: str,
    base_workspace_revision: int,
) -> str:
    return protocol_markers.file_state_marker(
        revision=revision,
        updated_at=updated_at,
        writer_id=writer_id,
        base_workspace_revision=base_workspace_revision,
    )


def daily_log_entry_marker(
    *,
    entry_id: str,
    created_at: str,
    writer_id: str,
    entry_seq: int,
) -> str:
    return protocol_markers.daily_log_entry_marker(
        entry_id=entry_id,
        created_at=created_at,
        writer_id=writer_id,
        entry_seq=entry_seq,
    )


def daily_log_scaffold_marker() -> str:
    return protocol_markers.daily_log_scaffold_marker()


def section_marker(section_key: str) -> str:
    return protocol_markers.section_marker(section_key)


def render_heading(level: int, heading: str) -> str:
    return protocol_templates.render_heading(level, heading)


def render_section_block(level: int, section_key: str, heading: str, body: list[str] | None = None) -> str:
    return protocol_templates.render_section_block(level, section_key, heading, body)


def rolling_summary_header(tool_name: str, day: str) -> str:
    return protocol_markers.rolling_summary_header(tool_name, day)


def render_context_brief_template(language: str, *, tool_name: str, timestamp: str, workspace_revision: int) -> str:
    language = validate_workspace_language(language)
    return protocol_templates.render_context_brief_template(
        language,
        tool_name=tool_name,
        timestamp=timestamp,
        workspace_revision=workspace_revision,
    )


def render_rolling_summary_template(tool_name: str, day: str, language: str, *, timestamp: str, workspace_revision: int) -> str:
    language = validate_workspace_language(language)
    return protocol_templates.render_rolling_summary_template(
        tool_name,
        day,
        language,
        timestamp=timestamp,
        workspace_revision=workspace_revision,
    )


def render_daily_log_template(language: str, *, tool_name: str, timestamp: str) -> str:
    language = validate_workspace_language(language)
    return protocol_templates.render_daily_log_template(
        language,
        tool_name=tool_name,
        timestamp=timestamp,
    )


def render_update_protocol_template(language: str, *, tool_name: str, timestamp: str, workspace_revision: int) -> str:
    language = validate_workspace_language(language)
    return protocol_templates.render_update_protocol_template(
        language,
        tool_name=tool_name,
        timestamp=timestamp,
        workspace_revision=workspace_revision,
    )


def render_template(
    file_key: str,
    *,
    tool_name: str,
    day: str,
    language: str,
    timestamp: str,
    workspace_revision: int,
) -> str:
    language = validate_workspace_language(language)
    return protocol_templates.render_template(
        file_key,
        tool_name=tool_name,
        day=day,
        language=language,
        timestamp=timestamp,
        workspace_revision=workspace_revision,
    )


def detect_workspace(
    project_root: Path,
    *,
    allow_unsupported_version: bool = False,
    allow_storage_mode_mismatch: bool = False,
) -> WorkspaceInfo | None:
    return workspace_runtime.detect_workspace(
        project_root,
        allow_unsupported_version=allow_unsupported_version,
        allow_storage_mode_mismatch=allow_storage_mode_mismatch,
    )


def find_recallloom_root(
    start_path: str | Path,
    *,
    allow_unsupported_version: bool = False,
    allow_storage_mode_mismatch: bool = False,
) -> WorkspaceInfo | None:
    return workspace_runtime.find_recallloom_root(
        start_path,
        allow_unsupported_version=allow_unsupported_version,
        allow_storage_mode_mismatch=allow_storage_mode_mismatch,
    )

def ensure_git_exclude_entry(project_root: Path, entry: str = f"{CONTEXT_DIRNAME}/") -> bool:
    return workspace_runtime.ensure_git_exclude_entry(project_root, entry=entry)


def remove_git_exclude_block(project_root: Path) -> bool:
    return workspace_runtime.remove_git_exclude_block(project_root)


def sorted_daily_log_files(logs_dir: Path) -> list[Path]:
    dated_files: list[Path] = []
    if not logs_dir.is_dir():
        return dated_files
    for child in logs_dir.iterdir():
        if child.is_file() and DATE_FILE_RE.match(child.name):
            try:
                parse_iso_date(child.stem)
            except ValueError:
                continue
            dated_files.append(child)
    return sorted(dated_files, key=lambda path: path.stem)


def invalid_iso_like_daily_log_files(logs_dir: Path) -> list[Path]:
    invalid: list[Path] = []
    if not logs_dir.is_dir():
        return invalid
    for child in sorted(logs_dir.iterdir(), key=lambda path: path.name):
        if child.is_file() and DATE_FILE_RE.match(child.name):
            try:
                parse_iso_date(child.stem)
            except ValueError:
                invalid.append(child)
    return invalid


def latest_dated_daily_log(logs_dir: Path) -> Path | None:
    dated_files = sorted_daily_log_files(logs_dir)
    if not dated_files:
        return None
    return dated_files[-1]


def latest_file(paths: Iterable[Path]) -> Path | None:
    return continuity_freshness.latest_file(list(paths))


def parse_file_marker(text: str) -> FileMarkerInfo | None:
    return protocol_markers.parse_file_marker(text)


def parse_file_state_marker(text: str) -> FileStateInfo | None:
    return protocol_markers.parse_file_state_marker(text)


def parse_daily_log_entry_marker(text: str) -> DailyLogEntryInfo | None:
    return protocol_markers.parse_daily_log_entry_marker(text)


def parse_daily_log_scaffold_marker(text: str) -> bool:
    return protocol_markers.parse_daily_log_scaffold_marker(text)


def managed_file_contract_issue(
    path: Path,
    *,
    file_key: str,
    workspace_language: str,
    expected_protocol_version: str | None = None,
) -> str | None:
    if not path.is_file():
        return f"Missing required file: {path}"
    text = read_text(path)
    marker = parse_file_marker(text)
    if marker is None:
        return f"Missing required file marker: {path}"
    if marker.file_key != file_key:
        return (
            f"Managed file marker mismatch for {path}: expected '{file_key}', "
            f"found '{marker.file_key}'."
        )
    if marker.language != workspace_language:
        return (
            f"Managed file language mismatch for {path}: expected '{workspace_language}', "
            f"found '{marker.language}'."
        )
    if expected_protocol_version is not None and marker.version != expected_protocol_version:
        return (
            f"Managed file protocol version mismatch for {path}: expected '{expected_protocol_version}', "
            f"found '{marker.version}'."
        )
    if file_key in {"context_brief", "rolling_summary", "update_protocol"}:
        if parse_file_state_marker(text) is None:
            return f"Missing required file-state marker: {path}"
    if file_key == "rolling_summary":
        lines = text.splitlines()
        if len(lines) < 2 or LAST_WRITER_RE.match(lines[1].strip()) is None:
            return f"rolling_summary.md second line must be a valid last-writer marker: {path}"
        match = LAST_WRITER_RE.match(lines[1].strip())
        if match is not None and not validate_iso_date(match.group("date")):
            return f"rolling_summary.md contains an invalid last-writer date: {path}"
    return None


def parse_daily_log_entry_line(line: str) -> DailyLogEntryInfo | None:
    match = DAILY_LOG_ENTRY_RE.match(line.strip())
    if not match:
        return None
    return DailyLogEntryInfo(
        entry_id=match.group("entry_id"),
        created_at=match.group("created_at"),
        writer_id=match.group("writer_id").strip(),
        entry_seq=int(match.group("entry_seq")),
    )


def daily_log_entries(text: str) -> list[DailyLogEntryInfo]:
    entries: list[DailyLogEntryInfo] = []
    for line in text.splitlines():
        entry = parse_daily_log_entry_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


def malformed_daily_log_entry_marker_lines(text: str) -> list[int]:
    malformed: list[int] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if candidate.startswith("<!-- daily-log-entry:") and parse_daily_log_entry_line(candidate) is None:
            malformed.append(line_number)
    return malformed


def sorted_active_daily_log_files(logs_dir: Path) -> list[Path]:
    return sorted_daily_log_files(logs_dir)


def latest_active_daily_log(logs_dir: Path) -> Path | None:
    active = sorted_active_daily_log_files(logs_dir)
    if not active:
        return None
    return active[-1]


def _daily_log_cursor_sequence_error(
    entries: list[DailyLogEntryInfo],
    *,
    path: Path,
) -> DailyLogCursorError | None:
    if not entries:
        return None

    sequences = [entry.entry_seq for entry in entries]
    duplicate_sequences = sorted(
        seq for seq in set(sequences) if sequences.count(seq) > 1
    )
    if duplicate_sequences:
        return DailyLogCursorError(
            reason_code="duplicate_daily_log_entry_sequence",
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                f"daily log has duplicate entry-seq values: {duplicate_sequences}."
            ),
            path=path,
            details={
                "duplicate_entry_seq": duplicate_sequences,
                "actual_sequences": sequences,
            },
        )

    entry_ids = [entry.entry_id for entry in entries]
    duplicate_entry_ids = sorted(
        entry_id for entry_id in set(entry_ids) if entry_ids.count(entry_id) > 1
    )
    if duplicate_entry_ids:
        return DailyLogCursorError(
            reason_code="duplicate_daily_log_entry_id",
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                f"daily log has duplicate entry ids: {duplicate_entry_ids}."
            ),
            path=path,
            details={
                "duplicate_entry_ids": duplicate_entry_ids,
                "actual_entry_ids": entry_ids,
            },
        )

    noncanonical_entry_ids = [
        {
            "entry_seq": entry.entry_seq,
            "entry_id": entry.entry_id,
            "expected_entry_id": f"entry-{entry.entry_seq}",
        }
        for entry in entries
        if entry.entry_id != f"entry-{entry.entry_seq}"
    ]
    if noncanonical_entry_ids:
        return DailyLogCursorError(
            reason_code="noncanonical_daily_log_entry_id",
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                "daily log entry ids do not match their entry-seq values."
            ),
            path=path,
            details={
                "noncanonical_entry_ids": noncanonical_entry_ids,
                "actual_entry_ids": entry_ids,
                "actual_sequences": sequences,
            },
        )

    expected = list(range(1, len(entries) + 1))
    if sequences != expected:
        reason_code = (
            "out_of_order_daily_log_entry_sequence"
            if sorted(sequences) == expected
            else "noncontiguous_daily_log_entry_sequence"
        )
        return DailyLogCursorError(
            reason_code=reason_code,
            message=(
                "Refusing to calculate the daily-log cursor because the latest active "
                f"daily log entry sequence is not canonical. Expected {expected}, "
                f"found {sequences}."
            ),
            path=path,
            details={
                "expected_sequences": expected,
                "actual_sequences": sequences,
            },
        )
    return None


def daily_log_cursor_from_text(
    text: str,
    *,
    path: Path,
    latest_file: str | None,
) -> DailyLogCursor:
    file_marker_info = parse_file_marker(text)
    if file_marker_info is None or file_marker_info.file_key != "daily_log":
        raise DailyLogCursorError(
            reason_code="malformed_latest_daily_log_file_marker",
            message=(
                "Refusing to calculate the daily-log cursor because the daily log is "
                "missing the required daily_log file marker."
            ),
            path=path,
            details={
                "latest_file": latest_file,
                "file_key": file_marker_info.file_key if file_marker_info else None,
            },
        )

    malformed_lines = malformed_daily_log_entry_marker_lines(text)
    if malformed_lines:
        raise DailyLogCursorError(
            reason_code="malformed_daily_log_entry_marker",
            message=(
                "Refusing to calculate the daily-log cursor because the daily log has "
                f"malformed daily-log-entry markers on lines {malformed_lines}."
            ),
            path=path,
            details={
                "latest_file": latest_file,
                "malformed_lines": malformed_lines,
            },
        )

    entries = daily_log_entries(text)
    scaffold = parse_daily_log_scaffold_marker(text)
    if scaffold and entries:
        raise DailyLogCursorError(
            reason_code="scaffold_daily_log_has_entries",
            message=(
                "Refusing to calculate the daily-log cursor because the daily log has "
                "both a scaffold marker and entry markers."
            ),
            path=path,
            details={"latest_file": latest_file},
        )
    if not entries:
        if not scaffold:
            raise DailyLogCursorError(
                reason_code="missing_daily_log_entry_marker",
                message=(
                    "Refusing to calculate the daily-log cursor because the daily log has "
                    "no entry markers and is not an empty scaffold."
                ),
                path=path,
                details={"latest_file": latest_file},
            )
        return DailyLogCursor(
            latest_file=latest_file,
            latest_entry_id=None,
            latest_entry_seq=None,
            entry_count=0,
            latest_path=path,
        )

    sequence_error = _daily_log_cursor_sequence_error(entries, path=path)
    if sequence_error is not None:
        raise sequence_error

    latest_entry_seq = entries[-1].entry_seq
    return DailyLogCursor(
        latest_file=latest_file,
        latest_entry_id=f"entry-{latest_entry_seq}",
        latest_entry_seq=latest_entry_seq,
        entry_count=len(entries),
        latest_path=path,
    )


def latest_active_daily_log_cursor(storage_root: Path) -> DailyLogCursor:
    logs_dir = storage_root / DAILY_LOGS_DIRNAME
    latest_path = latest_active_daily_log(logs_dir)
    if latest_path is None:
        return DailyLogCursor(
            latest_file=None,
            latest_entry_id=None,
            latest_entry_seq=0,
            entry_count=0,
            latest_path=None,
        )

    latest_file = latest_path.relative_to(storage_root).as_posix()
    try:
        text = read_text(latest_path)
    except (OSError, UnicodeDecodeError) as exc:
        raise DailyLogCursorError(
            reason_code="unreadable_latest_daily_log",
            message=f"Could not read latest active daily log {latest_path}: {exc}",
            path=latest_path,
        ) from exc

    return daily_log_cursor_from_text(
        text,
        path=latest_path,
        latest_file=latest_file,
    )


def state_claims_entry_bearing_latest_daily_log(state: dict) -> bool:
    daily_logs = state.get("daily_logs")
    if not isinstance(daily_logs, dict):
        return False
    entry_count = daily_logs.get("entry_count")
    return isinstance(entry_count, int) and not isinstance(entry_count, bool) and entry_count > 0


def validate_state_entry_bearing_latest_daily_log(
    *,
    storage_root: Path,
    state: dict,
) -> DailyLogCursor | None:
    if not state_claims_entry_bearing_latest_daily_log(state):
        return None

    daily_logs = state.get("daily_logs")
    if not isinstance(daily_logs, dict):
        raise DailyLogCursorError(
            reason_code="state_daily_logs_missing",
            message=(
                "Refusing to treat continuity as seeded because state.json does not "
                "contain a valid daily_logs cursor object."
            ),
        )

    latest_file = daily_logs.get("latest_file")
    if not isinstance(latest_file, str) or not latest_file.strip():
        raise DailyLogCursorError(
            reason_code="state_latest_daily_log_missing",
            message=(
                "Refusing to treat continuity as seeded because state.json claims "
                "daily-log entries but does not name a latest daily log."
            ),
            details=daily_log_cursor_state_fields(daily_logs),
        )

    latest_path = storage_root / latest_file
    try:
        text = read_text(latest_path)
    except (OSError, UnicodeDecodeError) as exc:
        raise DailyLogCursorError(
            reason_code="unreadable_latest_daily_log",
            message=f"Could not read latest daily log named by state.json {latest_path}: {exc}",
            path=latest_path,
            details={
                "latest_file": latest_file,
                **daily_log_cursor_state_fields(daily_logs),
            },
        ) from exc

    cursor = daily_log_cursor_from_text(text, path=latest_path, latest_file=latest_file)
    if cursor.entry_count <= 0:
        raise DailyLogCursorError(
            reason_code="state_entry_bearing_daily_log_has_no_entries",
            message=(
                "Refusing to treat continuity as seeded because state.json claims "
                "daily-log entries but the latest daily log parsed as an empty scaffold."
            ),
            path=latest_path,
            details={
                "latest_file": latest_file,
                "state_cursor": daily_log_cursor_state_fields(daily_logs),
                "parsed_cursor": cursor.as_state_fields(),
            },
        )

    state_cursor = daily_log_cursor_state_fields(daily_logs)
    parsed_cursor = cursor.as_state_fields()
    if not daily_log_cursors_equivalent(state_cursor, parsed_cursor, actual_cursor=parsed_cursor):
        raise DailyLogCursorError(
            reason_code="state_daily_log_cursor_mismatch",
            message=(
                "Refusing to treat continuity as seeded because state.json daily-log "
                "cursor fields do not match the strictly parsed latest daily log."
            ),
            path=latest_path,
            details={
                "latest_file": latest_file,
                "state_cursor": state_cursor,
                "parsed_cursor": parsed_cursor,
            },
        )

    return cursor


def daily_log_cursor_state_fields(state_or_daily_logs: dict) -> dict[str, object]:
    daily_logs = state_or_daily_logs.get("daily_logs")
    if not isinstance(daily_logs, dict):
        daily_logs = state_or_daily_logs
    return {key: daily_logs.get(key) for key in DAILY_LOG_CURSOR_STATE_KEYS}


def daily_log_cursor_is_legacy_empty(cursor: dict[str, object]) -> bool:
    return (
        cursor.get("latest_file") is None
        and cursor.get("latest_entry_id") is None
        and cursor.get("latest_entry_seq") in {0, None}
        and cursor.get("entry_count") == 0
    )


def daily_log_cursor_is_empty_scaffold(cursor: dict[str, object]) -> bool:
    latest_file = cursor.get("latest_file")
    return (
        isinstance(latest_file, str)
        and bool(latest_file)
        and cursor.get("latest_entry_id") is None
        and cursor.get("latest_entry_seq") in {0, None}
        and cursor.get("entry_count") == 0
    )


def daily_log_cursor_matches_empty_scaffold(
    cursor: dict[str, object],
    *,
    scaffold_latest_file: str,
) -> bool:
    if daily_log_cursor_is_legacy_empty(cursor):
        return True
    return (
        daily_log_cursor_is_empty_scaffold(cursor)
        and cursor.get("latest_file") == scaffold_latest_file
    )


def daily_log_cursors_equivalent(
    left: dict[str, object],
    right: dict[str, object],
    *,
    actual_cursor: dict[str, object] | None = None,
) -> bool:
    if left == right:
        return True
    if actual_cursor is None or not daily_log_cursor_is_empty_scaffold(actual_cursor):
        return False
    scaffold_latest_file = actual_cursor.get("latest_file")
    if not isinstance(scaffold_latest_file, str) or not scaffold_latest_file:
        return False
    return daily_log_cursor_matches_empty_scaffold(
        left,
        scaffold_latest_file=scaffold_latest_file,
    ) and daily_log_cursor_matches_empty_scaffold(
        right,
        scaffold_latest_file=scaffold_latest_file,
    )


def continuity_confidence_level(
    *,
    workspace_valid: bool,
    summary_revision_is_stale: bool,
    workspace_artifact_is_newer: bool | None,
    latest_daily_log_exists: bool,
    workspace_artifact_scan_mode: str,
) -> str:
    return continuity_freshness.continuity_confidence_level(
        workspace_valid=workspace_valid,
        summary_revision_is_stale=summary_revision_is_stale,
        workspace_artifact_is_newer=workspace_artifact_is_newer,
        latest_daily_log_exists=latest_daily_log_exists,
        workspace_artifact_scan_mode=workspace_artifact_scan_mode,
    )


def _digest_excerpt(text: str, *, max_lines: int = 4) -> str | None:
    return continuity_freshness.digest_excerpt(text, max_lines=max_lines)


def continuity_digest_bundle(
    *,
    summary_text: str,
    latest_daily_log_text: str | None = None,
    project_root: str | Path | None = None,
) -> dict:
    return continuity_freshness.continuity_digest_bundle(
        summary_text=summary_text,
        latest_daily_log_text=latest_daily_log_text,
        project_root=project_root,
    )


def scan_auto_attached_context_text(text: str) -> dict:
    return safety_attached_text.scan_auto_attached_context_text(text)


def iter_workspace_artifacts(
    project_root: Path,
    storage_root: Path,
    *,
    excluded_dirs: set[str] | None = None,
    excluded_files: set[str] | None = None,
) -> list[Path]:
    return continuity_freshness.iter_workspace_artifacts(
        project_root,
        storage_root,
        excluded_dirs=excluded_dirs,
        excluded_files=excluded_files,
    )


def evaluate_continuity_freshness(
    *,
    project_root: Path,
    storage_root: Path,
    summary_path: Path,
    workspace_revision: int,
    summary_base_workspace_revision: int,
    latest_daily_log_exists: bool,
    scan_mode: str = "quick",
    state: dict | None = None,
) -> dict:
    return continuity_freshness.evaluate_continuity_freshness(
        project_root=project_root,
        storage_root=storage_root,
        summary_path=summary_path,
        workspace_revision=workspace_revision,
        summary_base_workspace_revision=summary_base_workspace_revision,
        latest_daily_log_exists=latest_daily_log_exists,
        scan_mode=scan_mode,
        state=state,
    )


def daily_log_sequence_error(entries: list[DailyLogEntryInfo]) -> str | None:
    if not entries:
        return "Missing required daily-log-entry metadata marker."
    expected = list(range(1, len(entries) + 1))
    actual = [entry.entry_seq for entry in entries]
    if actual != expected:
        return f"Expected contiguous entry_seq values {expected}, found {actual}."
    noncanonical = [
        f"{entry.entry_id} for entry_seq {entry.entry_seq}"
        for entry in entries
        if entry.entry_id != f"entry-{entry.entry_seq}"
    ]
    if noncanonical:
        return (
            "Expected canonical entry ids matching entry_seq values, found "
            + ", ".join(noncanonical)
            + "."
        )
    return None


def section_keys_in_text(text: str) -> list[str]:
    return protocol_sections.section_keys_in_text(text)


def missing_section_keys(text: str, required_keys: Iterable[str]) -> list[str]:
    return protocol_sections.missing_section_keys(text, required_keys)


def duplicate_section_keys(text: str) -> list[str]:
    return protocol_sections.duplicate_section_keys(text)


def unknown_section_keys(text: str, allowed_keys: Iterable[str]) -> list[str]:
    return protocol_sections.unknown_section_keys(text, allowed_keys)


def bridge_block_integrity(text: str) -> tuple[bool, str | None]:
    return bridge_blocks.bridge_block_integrity(text)


def exclude_block_integrity(text: str) -> tuple[bool, str | None]:
    return bridge_blocks.exclude_block_integrity(text)


def managed_exclude_block_text(text: str) -> str | None:
    return bridge_blocks.managed_exclude_block_text(text)


def to_posix_relative(from_dir: Path, to_path: Path) -> str:
    return Path(os.path.relpath(to_path, start=from_dir)).as_posix()


def detect_root_entry_files(project_root: Path) -> list[Path]:
    return bridge_blocks.detect_root_entry_files(project_root)


def known_storage_assets(storage_root: Path) -> set[Path]:
    return workspace_runtime.known_storage_assets(
        storage_root,
        required_files=MANAGED_ASSET_REQUIRED_FILES,
        optional_files=MANAGED_ASSET_OPTIONAL_FILES,
        required_directories=MANAGED_ASSET_REQUIRED_DIRECTORIES,
        managed_directories=MANAGED_ASSET_DIRECTORIES,
    )


def known_storage_asset_kind_map(storage_root: Path) -> dict[Path, str]:
    return workspace_runtime.known_storage_asset_kind_map(
        storage_root,
        required_files=MANAGED_ASSET_REQUIRED_FILES,
        optional_files=MANAGED_ASSET_OPTIONAL_FILES,
        required_directories=MANAGED_ASSET_REQUIRED_DIRECTORIES,
        managed_directories=MANAGED_ASSET_DIRECTORIES,
    )


def _matches_dynamic_storage_asset_rule(path: Path, storage_root: Path) -> bool:
    return workspace_runtime.matches_dynamic_storage_asset_rule(
        path,
        storage_root,
        dynamic_rules=MANAGED_ASSET_DYNAMIC_RULES,
    )


def unknown_storage_assets(storage_root: Path) -> list[Path]:
    return workspace_runtime.unknown_storage_assets(
        storage_root,
        required_files=MANAGED_ASSET_REQUIRED_FILES,
        optional_files=MANAGED_ASSET_OPTIONAL_FILES,
        required_directories=MANAGED_ASSET_REQUIRED_DIRECTORIES,
        managed_directories=MANAGED_ASSET_DIRECTORIES,
        dynamic_rules=MANAGED_ASSET_DYNAMIC_RULES,
    )


def is_official_temp_storage_asset(path: Path, storage_root: Path) -> bool:
    return workspace_runtime.is_official_temp_storage_asset(
        path,
        storage_root,
        required_files=MANAGED_ASSET_REQUIRED_FILES,
        optional_files=MANAGED_ASSET_OPTIONAL_FILES,
        dynamic_rules=MANAGED_ASSET_DYNAMIC_RULES,
    )


def render_bridge_block(workspace: WorkspaceInfo, target_file: Path) -> str:
    return bridge_blocks.render_bridge_block(workspace, target_file)


def replace_or_insert_bridge(text: str, block: str) -> str:
    return bridge_blocks.replace_or_insert_bridge(text, block)


def remove_bridge_block(text: str) -> tuple[str, bool]:
    return bridge_blocks.remove_bridge_block(text)


def daily_logs_dir(storage_root: Path) -> Path:
    return workspace_runtime.daily_logs_dir(storage_root)


def storage_root_boundary_issue(project_root: Path, storage_root: Path, storage_mode: str) -> str | None:
    return workspace_runtime.storage_root_boundary_issue(project_root, storage_root, storage_mode)
