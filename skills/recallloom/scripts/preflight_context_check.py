#!/usr/bin/env python3
"""Run freshness and write-target checks before updating RecallLoom files.

Thin CLI adapter (T050-03A): the pure snapshot/readiness computation lives in
``core.continuity.preflight.evaluate_preflight``; this module only parses
arguments, delegates, and projects the result into the legacy JSON/human output
and exit codes unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.continuity.daily_log import DailyLogCursorError
from core.continuity.preflight import (
    PreflightSnapshotError,
    evaluate_preflight,
    strict_gate_preflight_failure_extra,
)
from core.output.user_status import print_user_summary
from core.provenance.evidence import strict_sidecar_no_write_failure_extra

from _common import (
    ConfigContractError,
    EnvironmentContractError,
    StorageResolutionError,
    cli_failure_payload,
    cli_failure_payload_for_exception,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue,
    exit_with_cli_error,
    find_recallloom_root,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check freshness and likely write targets before updating RecallLoom files."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    scan_mode_group = parser.add_mutually_exclusive_group()
    scan_mode_group.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Use the sidecar-visible freshness path only. This is now the default behavior and is kept "
            "as an explicit flag for compatibility."
        ),
    )
    scan_mode_group.add_argument(
        "--full",
        action="store_true",
        help=(
            "Run the heavier workspace artifact scan in addition to sidecar-visible signals. "
            "Use this when you want a deeper freshness pass before a high-confidence write."
        ),
    )
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="Exit non-zero if a non-context workspace artifact is newer than the rolling summary.",
    )
    parser.add_argument(
        "--skip-startup-residue-scan",
        action="store_true",
        help=argparse.SUPPRESS,
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
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra=strict_sidecar_no_write_failure_extra(continuity_confidence="broken"),
            ),
        )
    if workspace is None:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload(
                "no_project_root",
                error="No RecallLoom project root found.",
                details={"project_root": str(Path(args.path).expanduser().resolve())},
                extra=strict_sidecar_no_write_failure_extra(
                    continuity_confidence="broken",
                    include_recovery_actions=False,
                ),
            ),
        )
    startup_residue_report = None
    if not args.skip_startup_residue_scan:
        startup_residue_report = exit_if_startup_scratch_residue(
            parser,
            json_mode=args.json,
            project_root=workspace.project_root,
            storage_root=workspace.storage_root,
        )

    try:
        evaluation = evaluate_preflight(
            workspace=workspace,
            full=args.full,
            startup_residue_report=startup_residue_report,
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
                extra=strict_gate_preflight_failure_extra(workspace),
            ),
        )
    except PreflightSnapshotError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=str(exc),
                extra=strict_gate_preflight_failure_extra(workspace),
            ),
        )
    except (OSError, UnicodeDecodeError, ConfigContractError) as exc:
        message = f"Filesystem/state error: {exc}" if isinstance(exc, ConfigContractError) else f"Filesystem error: {exc}"
        if isinstance(exc, ConfigContractError):
            failure_contract = cli_failure_payload(
                getattr(exc, "failure_reason", None) or "damaged_sidecar",
                error=message,
                extra=strict_gate_preflight_failure_extra(workspace),
            )
        else:
            failure_contract = cli_failure_payload(
                "damaged_sidecar",
                error=message,
                extra=strict_gate_preflight_failure_extra(workspace),
            )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=failure_contract,
        )

    payload = evaluation.payload
    append_date_review_required = evaluation.append_date_review_required
    public_project_root = payload["project_root"]
    public_storage_root = payload["storage_root"]
    public_latest_daily_log = payload["latest_daily_log"]
    public_latest_workspace_artifact = payload["latest_workspace_artifact"]
    workspace_artifact_scan_mode = payload["workspace_artifact_scan_mode"]
    summary_revision_is_stale = payload["summary_revision_stale"]
    continuity_confidence = payload["continuity_confidence"]
    freshness_risk_level = payload["freshness_risk_level"]
    freshness_risk_note = payload["freshness_risk_note"]
    continuity_state = payload["continuity_state"]
    workspace_is_newer = payload["workspace_newer_than_summary"]
    recommended_actions = payload["recommended_actions"]
    conditional_review_targets = payload["conditional_review_targets"]
    override_review_targets = payload["override_review_targets"]
    latest_daily_log_present = payload["latest_daily_log"] is not None
    user_summary = payload["user_summary"]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_user_summary("RecallLoom preflight", user_summary)
        print(f"RecallLoom root: {public_project_root}")
        print(f"Storage root: {public_storage_root}")
        print(f"Storage mode: {payload['storage_mode']}")
        print(f"Workspace language: {payload['workspace_language']}")
        print(f"Rolling summary: {payload['rolling_summary']}")
        if append_date_review_required and latest_daily_log_present:
            print("Latest active daily log: redacted pending date review")
        else:
            print("Latest active daily log: " f"{public_latest_daily_log if public_latest_daily_log else 'none'}")
        print(
            "Latest workspace artifact: "
            f"{public_latest_workspace_artifact if public_latest_workspace_artifact else 'none'}"
        )
        print(f"Workspace artifact scan mode: {workspace_artifact_scan_mode}")
        print("Summary revision stale: " f"{'yes' if summary_revision_is_stale else 'no'}")
        print(f"Continuity confidence: {continuity_confidence}")
        if freshness_risk_note:
            print(f"Freshness risk: {freshness_risk_level} - {freshness_risk_note}")
        print(f"Continuity state: {continuity_state}")
        print(f"Workspace newer than summary: {'yes' if workspace_is_newer else 'no'}")
        if recommended_actions:
            print("Recommended actions:")
            for action in recommended_actions:
                print(f"  - {action}")
        print("Recommended write targets:")
        for target in payload["recommended_write_targets"]:
            print(f"  - {target}")
        if conditional_review_targets:
            print("Conditional review targets:")
            for target in conditional_review_targets:
                print(f"  - {target['path']}: {target['reason']}")
        if override_review_targets:
            print("Override review targets:")
            for target in override_review_targets:
                print(f"  - {target['path']}: {target['reason']}")
        if isinstance(payload.get("safe_write_context"), dict):
            print("Safe write context:")
            print(f"  - workspace_revision: {payload['workspace_revision']}")
            print(
                "  - use commit_context_file.py for revision-checked writes to "
                "context_brief.md, rolling_summary.md, or update_protocol.md"
            )
            print("  - use append_daily_log_entry.py for revision-checked daily-log milestone entries")
        else:
            reason = payload.get("write_context_blocked_reason") or "provenance_review_required"
            print(f"Safe write context: unavailable ({reason})")

    raise SystemExit(3 if args.fail_on_stale and workspace_is_newer else 0)


if __name__ == "__main__":
    main()
