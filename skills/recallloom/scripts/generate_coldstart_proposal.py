#!/usr/bin/env python3
"""Generate a structured cold-start proposal from controlled source tiers."""

from __future__ import annotations

import argparse
import json

from core.continuity.freshness import continuity_state_for_workspace
from core.coldstart.sources import (
    build_host_memory_adapter,
    gather_coldstart_sources,
    recommend_coldstart_path,
    render_coldstart_proposal,
)
from core.coldstart.structured import (
    PROPOSAL_SECTION_ALIASES,
    detect_promotion_targets,
    detect_source_tiers,
    extract_structured_sections,
)
from core.protocol.contracts import FILE_KEYS

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    DailyLogCursorError,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    load_workspace_state,
    public_json_payload,
    read_text,
    StorageResolutionError,
    validate_state_entry_bearing_latest_daily_log,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a structured cold-start proposal from controlled RecallLoom source tiers."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Explicit additional source path for Tier D. Can be repeated.",
    )
    parser.add_argument(
        "--include-git-signal",
        action="store_true",
        help="Opt in to Tier E limited git signal (recent commit subjects).",
    )
    parser.add_argument(
        "--enable-host-memory",
        action="store_true",
        help="Enable the reserved Tier F host-memory adapter interface. Disabled by default.",
    )
    parser.add_argument(
        "--host-memory-source",
        help="Explicit source label for Tier F host memory when enabled.",
    )
    parser.add_argument(
        "--host-memory-path",
        help="Explicit host-memory file path when enabling Tier F path mode.",
    )
    parser.add_argument(
        "--host-memory-command",
        help="Explicit host-memory command descriptor when enabling Tier F command mode. The command is declared, not auto-executed.",
    )
    parser.add_argument(
        "--host-memory-confidence",
        choices=["low", "medium", "high"],
        help="Confidence level for the explicit Tier F host-memory adapter.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload("python_runtime_unavailable", error=str(exc)),
        )
    enforce_package_support_gate(parser, json_mode=args.json)

    try:
        workspace = find_recallloom_root(args.path)
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )
    if workspace is None:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            reason="no_project_root",
        )

    try:
        host_memory_adapter = build_host_memory_adapter(
            workspace.project_root,
            enabled=args.enable_host_memory,
            source_label=args.host_memory_source,
            path_raw=args.host_memory_path,
            command_raw=args.host_memory_command,
            confidence=args.host_memory_confidence,
        )
    except ValueError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            reason="invalid_prepared_input",
        )

    try:
        sources = gather_coldstart_sources(
            workspace.project_root,
            explicit_sources=args.source,
            include_git_signal=args.include_git_signal,
            host_memory_adapter=host_memory_adapter,
        )
    except ValueError as exc:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            reason="invalid_prepared_input",
        )
    try:
        state = load_workspace_state(workspace.storage_root / FILE_KEYS["state"])
        summary_text = read_text(workspace.storage_root / FILE_KEYS["rolling_summary"])
    except (ConfigContractError, OSError, UnicodeDecodeError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
                payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )

    try:
        latest_daily_log_cursor = validate_state_entry_bearing_latest_daily_log(
            storage_root=workspace.storage_root,
            state=state,
        )
    except DailyLogCursorError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=str(exc),
                details={
                    **exc.details,
                    "project_root": str(workspace.project_root),
                },
            ),
        )

    continuity_state, continuity_seeded = continuity_state_for_workspace(
        state=state,
        summary_text=summary_text,
        latest_daily_log_exists=latest_daily_log_cursor is not None,
    )
    proposal_markdown = render_coldstart_proposal(
        workspace.project_root,
        sources,
        workspace_language=workspace.workspace_language,
    )
    proposal_sections = extract_structured_sections(proposal_markdown, PROPOSAL_SECTION_ALIASES)
    path_recommendation = recommend_coldstart_path(sources)

    payload = {
        "ok": True,
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "continuity_state": continuity_state,
        "continuity_seeded": continuity_seeded,
        "coldstart_action": "generate_structured_proposal",
        "source_tiers_used": sorted({source["tier"] for source in sources}),
        "host_memory_enabled": host_memory_adapter["enabled"],
        "host_memory_adapter": host_memory_adapter,
        "path_recommendation": path_recommendation,
        "sources_considered": sources,
        "proposal_sections_present": sorted(proposal_sections.keys()),
        "source_tiers_detected": detect_source_tiers(proposal_markdown),
        "promotion_targets_detected": detect_promotion_targets(proposal_markdown),
        "proposal_markdown": proposal_markdown,
    }

    if args.json:
        print(
            json.dumps(
                public_json_payload(payload, project_root=workspace.project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(proposal_markdown)


if __name__ == "__main__":
    main()
