#!/usr/bin/env python3
"""Record a prepared recovery review for a staged RecallLoom recovery proposal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.coldstart.structured import (
    REVIEW_SECTION_ALIASES,
    classify_review_action,
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
    RECOVERY_PROPOSAL_FILE_RE,
    StorageResolutionError,
    text_digest,
    validate_recovery_review_text,
    workspace_write_lock,
    write_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record a recovery review for a staged RecallLoom recovery proposal."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument(
        "--proposal-file",
        required=True,
        help="Proposal filename or path. Relative values are resolved against companion/recovery/proposals/ first.",
    )
    parser.add_argument("--source-file", required=True, help="Path to prepared review markdown content.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def resolve_proposal_path(raw_value: str, proposals_dir: Path, project_root: Path) -> Path:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    proposal_relative = proposals_dir / raw_value
    if proposal_relative.exists():
        return proposal_relative.resolve()

    project_relative = project_root / raw_value
    return project_relative.resolve()


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
    review_errors = validate_recovery_review_text(body_text)
    if review_errors:
        message = "Invalid recovery review content:\n- " + "\n- ".join(review_errors)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"review_errors": review_errors},
            ),
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

    proposal_path = resolve_proposal_path(args.proposal_file, proposals_dir, workspace.project_root)
    if not proposal_path.is_file():
        message = f"Proposal file does not exist: {proposal_path}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )
    if proposal_path.parent != proposals_dir.resolve():
        message = (
            "Proposal file must live under companion/recovery/proposals/: "
            f"{proposal_path}"
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )
    if not RECOVERY_PROPOSAL_FILE_RE.match(proposal_path.name):
        message = (
            "Proposal filename does not match the expected recovery proposal shape: "
            f"{proposal_path.name}"
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )

    review_path = review_log_dir / f"{proposal_path.stem}.review.md"

    try:
        with workspace_write_lock(workspace.project_root, "record_recovery_review.py"):
            review_log_dir.mkdir(parents=True, exist_ok=True)
            archive_dir.mkdir(parents=True, exist_ok=True)
            if review_path.exists():
                message = f"Refusing to overwrite an existing recovery review: {review_path}"
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=message,
                    payload=cli_failure_payload(
                        "malformed_managed_file",
                        error=message,
                        details={"review_path": str(review_path)},
                    ),
                )
            write_text(review_path, body_text.rstrip("\n") + "\n")
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
        "proposal_path": str(proposal_path),
        "review_path": str(review_path),
        "source_file": str(source_path),
        "source_digest": text_digest(body_text),
        "review_sections_present": sorted(extract_structured_sections(body_text, REVIEW_SECTION_ALIASES).keys()),
        "review_action": classify_review_action(extract_structured_sections(body_text, REVIEW_SECTION_ALIASES)),
        "recorded_at": now_iso_timestamp(),
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
        print(f"Recorded recovery review: {review_path}")


if __name__ == "__main__":
    main()
