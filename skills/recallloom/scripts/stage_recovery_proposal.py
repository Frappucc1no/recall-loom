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
from core.safety.prepared_input import (
    PreparedInputSafetyError,
    read_prepared_input_source_text,
    validate_prepared_input_source_path,
)

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_managed_directory_chain,
    ensure_supported_python_version,
    exit_if_startup_scratch_residue_for_sources,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recallloom_root,
    LockBusyError,
    ManagedDirectorySafetyError,
    now_iso_timestamp,
    public_project_path,
    publicize_text_paths,
    public_json_payload,
    StorageResolutionError,
    scan_auto_attached_context_text,
    text_digest,
    validate_recovery_proposal_text,
    workspace_write_lock,
    write_text,
)


FILENAME_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}$")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024


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


def exit_prepared_input_safety_error(
    parser,
    *,
    json_mode: bool,
    error: PreparedInputSafetyError,
) -> None:
    exit_with_failure_contract(
        parser,
        json_mode=json_mode,
        exit_code=2,
        message=error.message,
        reason="invalid_prepared_input",
        details=error.details,
    )


def read_recovery_source_file(
    parser,
    *,
    json_mode: bool,
    raw_source_file: str,
    project_root: Path,
    storage_root: Path,
) -> tuple[Path, str]:
    try:
        source = validate_prepared_input_source_path(
            raw_source_file,
            project_root=project_root,
            storage_root=storage_root,
            input_role="source-file",
            label="source",
        )
        body_text = read_prepared_input_source_text(
            source,
            max_input_bytes=DEFAULT_MAX_INPUT_BYTES,
            label="source",
        )
    except PreparedInputSafetyError as exc:
        exit_prepared_input_safety_error(parser, json_mode=json_mode, error=exc)
    if not body_text.strip():
        message = "Source file is empty."
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"source_file_ref": "provided_source_file", "side_effect": "none"},
            ),
        )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        message = (
            "Refusing to stage recovery proposal because the prepared source failed "
            "the attached-text safety scan: "
            + ", ".join(attach_scan["hard_block_reasons"])
        )
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "attach_scan_blocked",
                error=message,
                details={"hard_block_reasons": attach_scan["hard_block_reasons"]},
            ),
        )
    return source.path, body_text


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
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=1,
            message="No RecallLoom project root found.",
            payload=cli_failure_payload("no_project_root", error="No RecallLoom project root found."),
        )

    exit_if_startup_scratch_residue_for_sources(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        source_paths=[args.source_file],
    )
    source_path, body_text = read_recovery_source_file(
        parser,
        json_mode=args.json,
        raw_source_file=args.source_file,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
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

    proposals_dir = workspace.storage_root / "companion" / "recovery" / "proposals"
    target_path = proposals_dir / f"{filename_stamp}-{proposal_id}.md"

    try:
        with workspace_write_lock(workspace.project_root, "stage_recovery_proposal.py"):
            proposals_dir = ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "proposals"),
                project_root=workspace.project_root,
            )
            ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "review_log"),
                project_root=workspace.project_root,
            )
            ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "archive"),
                project_root=workspace.project_root,
            )
            target_path = proposals_dir / f"{filename_stamp}-{proposal_id}.md"
            try:
                target_path.lstat()
                target_exists = True
            except FileNotFoundError:
                target_exists = False
            if target_exists:
                public_target = public_project_path(target_path, project_root=workspace.project_root) or target_path.name
                message = f"Refusing to overwrite an existing recovery proposal: {public_target}"
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
            ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "proposals"),
                project_root=workspace.project_root,
                create=False,
                )
            write_text(target_path, body_text.rstrip("\n") + "\n")
    except LockBusyError as exc:
        public_message = publicize_text_paths(
            str(exc),
            project_root=workspace.project_root,
        ) or "Refusing to continue because another RecallLoom mutating operation appears to be running."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=public_message,
            payload=cli_failure_payload("write_lock_busy", error=public_message),
        )
    except ManagedDirectorySafetyError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=exc.message,
            payload=cli_failure_payload(
                exc.failure_reason,
                error=exc.message,
                details=exc.details,
            ),
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = "Filesystem error while writing recovery proposal."
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "damaged_sidecar",
                error=message,
                details={"error_type": type(exc).__name__},
            ),
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
        public_target = public_project_path(target_path, project_root=workspace.project_root) or target_path.name
        print(f"Staged recovery proposal: {public_target}")


if __name__ == "__main__":
    main()
