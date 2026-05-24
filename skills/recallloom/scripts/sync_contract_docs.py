#!/usr/bin/env python3
"""Sync registry-driven contract snippets into RecallLoom docs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from core.output.privacy import publicize_json_value
from core.protocol import contracts as protocol_contracts
from _common import enforce_package_support_gate


REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT_RESOLVED = REPO_ROOT.resolve()
SKILL_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
USAGE_PATH = REPO_ROOT / "USAGE.md"
SKILL_DOC_PATH = SKILL_ROOT / "SKILL.md"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ACTIVE_ENGLISH_RE = re.compile(r"\*\*\s*English\s*\*\*", re.IGNORECASE)
_ACTIVE_CHINESE_RE = re.compile(r"\*\*\s*简体中文\s*\*\*")


def resolve_doc_target(path_str: str) -> Path:
    path = Path(path_str)
    candidate = path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()
    try:
        candidate.relative_to(REPO_ROOT_RESOLVED)
    except ValueError as exc:
        raise RuntimeError(
            f"Doc sync target resolves outside the repository root and is not allowed: {path_str}"
        ) from exc
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync registry-driven contract snippets into RecallLoom docs."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether docs are already in sync without writing files.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )
    return parser


def infer_doc_language_from_text(text: str, *, fallback: str = "en") -> str:
    active_english = _ACTIVE_ENGLISH_RE.search(text) is not None
    active_chinese = _ACTIVE_CHINESE_RE.search(text) is not None
    if active_chinese and not active_english:
        return "zh-CN"
    if active_english and not active_chinese:
        return "en"

    sample = _FENCED_CODE_RE.sub("", text)
    sample = _INLINE_CODE_RE.sub("", sample)
    sample = _HTML_TAG_RE.sub(" ", sample)
    cjk_count = len(_CJK_RE.findall(sample))
    latin_count = len(_LATIN_RE.findall(sample))
    if cjk_count >= 8 and cjk_count >= max(1, latin_count) * 0.2:
        return "zh-CN"
    return fallback


def doc_language_for_path(path: Path) -> str:
    if path.name.endswith(".zh-CN.md"):
        return "zh-CN"
    if path.name.endswith(".en.md"):
        return "en"
    try:
        return infer_doc_language_from_text(path.read_text(encoding="utf-8"))
    except OSError:
        return "en"


def render_runtime_requirements_block(*, language: str) -> str:
    minimum_python_version = json.loads((SKILL_ROOT / "package-metadata.json").read_text(encoding="utf-8"))[
        "minimum_python_version"
    ]
    if language == "zh-CN":
        lines = [
            f"- Python 版本要求：`{minimum_python_version}` 及以上",
            "- 支持的工作区语言：",
        ]
    else:
        lines = [
            f"- minimum Python version: `{minimum_python_version}`",
            "- supported `workspace_language` values:",
        ]
    for workspace_language in protocol_contracts.CONTRACT_REGISTRY["workspace"]["languages"]:
        lines.append(f"  - `{workspace_language}`")
    lines.append("- 支持的入口桥接文件：" if language == "zh-CN" else "- supported root entry files for thin bridges:")
    for target in protocol_contracts.CONTRACT_REGISTRY["workspace"]["bridge_targets"]:
        lines.append(f"  - `{target}`")
    return "\n".join(lines)


def render_package_metadata_block(*, language: str) -> str:
    package_metadata = json.loads((SKILL_ROOT / "package-metadata.json").read_text(encoding="utf-8"))
    if language == "zh-CN":
        lines = [
            f"- 包版本：`{package_metadata['package_version']}`",
            f"- 协议版本：`{package_metadata['protocol_version']}`",
            "- 当前支持的协议版本：",
        ]
    else:
        lines = [
            f"- package version: `{package_metadata['package_version']}`",
            f"- protocol version: `{package_metadata['protocol_version']}`",
            "- supported protocol versions:",
        ]
    for version in package_metadata["supported_protocol_versions"]:
        lines.append(f"  - `{version}`")
    return "\n".join(lines)


def render_runtime_assumptions_block(*, language: str) -> str:
    package_metadata = json.loads((SKILL_ROOT / "package-metadata.json").read_text(encoding="utf-8"))
    if language == "zh-CN":
        lines = [
            f"- Python 版本要求：`{package_metadata['minimum_python_version']}` 及以上",
            "- 支持的工作区语言：",
        ]
    else:
        lines = [
            f"- Python {package_metadata['minimum_python_version']} or newer",
            "- supported workspace languages:",
        ]
    for workspace_language in package_metadata["supported_workspace_languages"]:
        lines.append(f"  - `{workspace_language}`")
    lines.append("- 支持的入口桥接文件：" if language == "zh-CN" else "- supported bridge targets:")
    for target in package_metadata["supported_bridge_targets"]:
        lines.append(f"  - `{target}`")
    return "\n".join(lines)


def render_protocol_registry_summary_block() -> str:
    lines = [
        "- current protocol version:",
        f"  - `{protocol_contracts.CURRENT_PROTOCOL_VERSION}`",
        "- supported protocol versions:",
    ]
    for version in sorted(protocol_contracts.SUPPORTED_PROTOCOL_VERSIONS):
        lines.append(f"  - `{version}`")
    lines.extend(
        [
            "- supported `workspace_language` values:",
        ]
    )
    for language in sorted(protocol_contracts.SUPPORTED_WORKSPACE_LANGUAGES):
        lines.append(f"  - `{language}`")
    lines.append("- allowed `storage_mode` values:")
    for mode in sorted(protocol_contracts.SUPPORTED_STORAGE_MODES):
        lines.append(f"  - `{mode}`")
    lines.append("- supported root entry files for thin bridges:")
    for target in protocol_contracts.CONTRACT_REGISTRY["workspace"]["bridge_targets"]:
        lines.append(f"  - `{target}`")
    lines.append("- supported dynamic asset rule kinds:")
    for kind in sorted(protocol_contracts.SUPPORTED_DYNAMIC_ASSET_RULE_KINDS):
        lines.append(f"  - `{kind}`")
    return "\n".join(lines)


def render_file_contract_registry_summary_block() -> str:
    lines = ["Generated from `references/contract-registry.json`.", ""]
    for file_key, contract in protocol_contracts.CONTRACT_REGISTRY["files"].items():
        path_label = contract.get("path", contract.get("path_pattern", "(dynamic)"))
        lines.append(f"### `{file_key}`")
        lines.append("")
        lines.append(f"- path: `{path_label}`")
        required = contract.get("required_sections", [])
        optional = contract.get("optional_sections", [])
        render_order = contract.get("render_order", [])
        lines.append("- required sections:")
        if required:
            for item in required:
                lines.append(f"  - `{item}`")
        else:
            lines.append("  - none")
        lines.append("- optional sections:")
        if optional:
            for item in optional:
                lines.append(f"  - `{item}`")
        else:
            lines.append("  - none")
        lines.append("- render order:")
        if render_order:
            for item in render_order:
                lines.append(f"  - `{item}`")
        else:
            lines.append("  - none")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_block(block_name: str, *, language: str) -> str:
    if block_name == "package-metadata":
        return render_package_metadata_block(language=language)
    if block_name == "runtime-assumptions":
        return render_runtime_assumptions_block(language=language)
    if block_name == "runtime-requirements":
        return render_runtime_requirements_block(language=language)
    if block_name == "protocol-registry-summary":
        return render_protocol_registry_summary_block()
    if block_name == "file-contract-registry-summary":
        return render_file_contract_registry_summary_block()
    raise KeyError(block_name)


def replace_sync_block(text: str, block_name: str, rendered: str) -> str:
    start_marker = f"<!-- RecallLoom metadata sync start: {block_name} -->"
    end_marker = f"<!-- RecallLoom metadata sync end: {block_name} -->"
    if start_marker not in text or end_marker not in text:
        raise RuntimeError(
            f"Missing sync markers for block '{block_name}'. "
            "Add start/end markers before running sync."
        )
    start_idx = text.index(start_marker) + len(start_marker)
    end_idx = text.index(end_marker)
    middle = "\n" + rendered.rstrip() + "\n"
    return text[:start_idx] + middle + text[end_idx:]


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    enforce_package_support_gate(
        parser,
        json_mode=args.json,
        action_name="sync_contract_docs.py",
        action_level="diagnostic" if args.check else "mutating",
    )

    changed_files: list[str] = []
    checked_files: list[str] = []
    seen_changed: set[str] = set()
    seen_checked: set[str] = set()

    sync_blocks = protocol_contracts.CONTRACT_REGISTRY["doc_sync_blocks"]
    for block_name, block in sync_blocks.items():
        for target in block["targets"]:
            path = resolve_doc_target(target)
            rendered = render_block(block_name, language=doc_language_for_path(path))
            original = path.read_text(encoding="utf-8")
            updated = replace_sync_block(original, block_name, rendered)
            path_str = str(path)
            if path_str not in seen_checked:
                seen_checked.add(path_str)
                checked_files.append(path_str)
            if updated != original:
                if path_str not in seen_changed:
                    seen_changed.add(path_str)
                    changed_files.append(path_str)
                if not args.check:
                    path.write_text(updated, encoding="utf-8")

    if args.check and changed_files:
        payload = {
            "ok": False,
            "changed_files": changed_files,
            "checked_files": checked_files,
        }
        if args.json:
            public_payload = publicize_json_value(payload, project_root=REPO_ROOT)
            print(json.dumps(public_payload, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    payload = {
        "ok": True,
        "changed_files": changed_files,
        "checked_files": checked_files,
        "mode": "check" if args.check else "write",
    }
    if args.json:
        public_payload = publicize_json_value(payload, project_root=REPO_ROOT)
        print(json.dumps(public_payload, ensure_ascii=False, indent=2))
    else:
        if changed_files:
            action = "Would update" if args.check else "Updated"
            print(f"{action} {len(changed_files)} doc file(s).")
            for path in changed_files:
                print(f"- {path}")
        else:
            print("Docs already in sync.")


if __name__ == "__main__":
    main()
