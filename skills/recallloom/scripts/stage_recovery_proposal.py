#!/usr/bin/env python3
"""Stage a prepared recovery proposal into the RecallLoom companion namespace."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from core.coldstart.structured import (
    PROPOSAL_SECTION_ALIASES,
    detect_promotion_targets,
    detect_source_tiers,
    extract_structured_sections,
)

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_supported_python_version,
    exit_with_cli_error,
    find_recallloom_root,
    LockBusyError,
    now_iso_timestamp,
    public_json_payload,
    read_text,
    StorageResolutionError,
    text_digest,
    validate_recovery_proposal_text,
    workspace_write_lock,
    write_text,
)


FILENAME_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}$")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage a prepared recovery proposal into companion/recovery/proposals."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument("--source-file", required=True, help="Path to prepared proposal markdown content.")
    parser.add_argument(
        "--proposal-id",
        help="Optional stable identifier used in the staged proposal filename. Defaults to a slug from the source filename.",
    )
    parser.add_argument(
        "--filename-stamp",
        help="Optional filename stamp in YYYY-MM-DD-HHMMSS form. Defaults to the current local time.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def normalize_proposal_id(raw_value: str) -> str:
    normalized = SAFE_ID_RE.sub("-", raw_value.strip()).strip("-._")
    if not normalized:
        raise ValueError("Proposal id is empty after normalization.")
    return normalized


def resolve_filename_stamp(raw_value: str | None) -> str:
    if raw_value:
        if not FILENAME_STAMP_RE.match(raw_value):
            raise ValueError(
                f"Invalid --filename-stamp value: {raw_value}. Expected YYYY-MM-DD-HHMMSS."
            )
        return raw_value
    return datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")


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

    source_path = Path(args.source_file).expanduser().resolve()
    if not source_path.is_file():
        message = "Source file does not exist or is not a regular file."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"source_file_ref": "provided_source_file", "side_effect": "none"},
            ),
        )

    try:
        body_text = read_text(source_path)
    except (OSError, UnicodeDecodeError) as exc:
        message = "Source file could not be read."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={
                    "source_file_ref": "provided_source_file",
                    "side_effect": "none",
                    "error_type": type(exc).__name__,
                },
            ),
        )
    if not body_text.strip():
        message = "Source file is empty."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"source_file_ref": "provided_source_file", "side_effect": "none"},
            ),
        )
    proposal_errors = validate_recovery_proposal_text(body_text)
    if proposal_errors:
        message = "Invalid recovery proposal content:\n- " + "\n- ".join(proposal_errors)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_errors": proposal_errors},
            ),
        )

    try:
        proposal_id = normalize_proposal_id(args.proposal_id or source_path.stem)
        filename_stamp = resolve_filename_stamp(args.filename_stamp)
    except ValueError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload("invalid_prepared_input", error=str(exc)),
        )

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
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload("no_project_root", error="No RecallLoom project root found."),
        )

    proposals_dir = workspace.storage_root / "companion" / "recovery" / "proposals"
    review_log_dir = workspace.storage_root / "companion" / "recovery" / "review_log"
    archive_dir = workspace.storage_root / "companion" / "recovery" / "archive"
    target_path = proposals_dir / f"{filename_stamp}-{proposal_id}.md"

    try:
        with workspace_write_lock(workspace.project_root, "stage_recovery_proposal.py"):
            proposals_dir.mkdir(parents=True, exist_ok=True)
            review_log_dir.mkdir(parents=True, exist_ok=True)
            archive_dir.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                message = f"Refusing to overwrite an existing recovery proposal: {target_path}"
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=message,
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=message,
                        details={"proposal_path": str(target_path)},
                    ),
                )
            write_text(target_path, body_text.rstrip("\n") + "\n")
    except LockBusyError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=str(exc),
            payload=cli_failure_payload("write_lock_busy", error=str(exc)),
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Filesystem error: {exc}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload("damaged_sidecar", error=message),
        )

    payload = {
        "ok": True,
        "proposal_path": str(target_path),
        "proposal_id": proposal_id,
        "filename_stamp": filename_stamp,
        "source_file": str(source_path),
        "source_digest": text_digest(body_text),
        "proposal_sections_present": sorted(extract_structured_sections(body_text, PROPOSAL_SECTION_ALIASES).keys()),
        "source_tiers_detected": detect_source_tiers(body_text),
        "promotion_targets_detected": detect_promotion_targets(body_text),
        "staged_at": now_iso_timestamp(),
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
        print(f"Staged recovery proposal: {target_path}")


if __name__ == "__main__":
    main()
