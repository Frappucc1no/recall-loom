#!/usr/bin/env python3
"""Record a prepared recovery review for a staged RecallLoom recovery proposal."""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
from pathlib import Path
import sys

from core.coldstart.structured import (
    REVIEW_SECTION_ALIASES,
    classify_review_action,
    extract_structured_sections,
    promotion_ready_for_action,
)
from core.protocol.contracts import FILE_KEYS
from core.provenance.state import review_imported_baseline_metadata
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
    load_workspace_state,
    LockBusyError,
    ManagedDirectorySafetyError,
    now_iso_timestamp,
    public_project_path,
    publicize_text_paths,
    public_json_payload,
    read_text,
    RECOVERY_PROPOSAL_FILE_RE,
    StorageResolutionError,
    scan_auto_attached_context_text,
    text_digest,
    validate_recovery_review_text,
    workspace_write_lock,
    write_text,
)

DEFAULT_MAX_INPUT_BYTES = 4 * 1024 * 1024


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
    parser.add_argument("--source-file", help="Path to prepared review markdown content.")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read prepared review markdown content from UTF-8 stdin.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


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


def read_recovery_source(
    parser,
    *,
    json_mode: bool,
    raw_source_file: str | None,
    use_stdin: bool,
    project_root: Path,
    storage_root: Path,
) -> tuple[Path | None, str, str]:
    if bool(raw_source_file) == bool(use_stdin):
        message = "Provide prepared review content with exactly one of --source-file or --stdin."
        if raw_source_file and use_stdin:
            message = "Use exactly one prepared review input: --source-file or --stdin."
        exit_with_failure_contract(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            reason="invalid_prepared_input",
            details={"side_effect": "none"},
        )

    source_path: Path | None = None
    input_mode = "stdin" if use_stdin else "file"
    if raw_source_file:
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
        source_path = source.path
    else:
        if sys.stdin.isatty():
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="No prepared review content was provided on stdin.",
                reason="invalid_prepared_input",
                details={"input_mode": "stdin", "side_effect": "none"},
            )
        try:
            raw_bytes = sys.stdin.buffer.read(DEFAULT_MAX_INPUT_BYTES + 1)
        except OSError as exc:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message=f"Failed to read prepared review stdin input: {exc}",
                reason="invalid_prepared_input",
                details={
                    "input_mode": "stdin",
                    "reason_code": "stdin_read_failed",
                    "error_type": type(exc).__name__,
                    "side_effect": "none",
                },
            )
        if len(raw_bytes) > DEFAULT_MAX_INPUT_BYTES:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message=(
                    "Prepared review stdin input exceeds the maximum size "
                    f"({len(raw_bytes)} > {DEFAULT_MAX_INPUT_BYTES})."
                ),
                reason="invalid_prepared_input",
                details={
                    "input_mode": "stdin",
                    "size": len(raw_bytes),
                    "max_input_bytes": DEFAULT_MAX_INPUT_BYTES,
                    "side_effect": "none",
                },
            )
        try:
            body_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            exit_with_failure_contract(
                parser,
                json_mode=json_mode,
                exit_code=2,
                message="Prepared review stdin input must be valid UTF-8.",
                reason="invalid_prepared_input",
                details={
                    "input_mode": "stdin",
                    "reason_code": "stdin_decode_failed",
                    "error_type": type(exc).__name__,
                    "side_effect": "none",
                },
            )

    if not body_text.strip():
        message = "Source file is empty."
        if use_stdin:
            message = "Prepared review stdin input is empty."
        exit_with_cli_error(
            parser,
            json_mode=json_mode,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={
                    "input_mode": input_mode,
                    "source_file_ref": "provided_source_file" if source_path else None,
                    "side_effect": "none",
                },
            ),
        )
    attach_scan = scan_auto_attached_context_text(body_text)
    if attach_scan["blocked"]:
        message = (
            "Refusing to record recovery review because the prepared source failed "
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
    return source_path, body_text, input_mode


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

    source_paths = [args.source_file] if args.source_file else []
    exit_if_startup_scratch_residue_for_sources(
        parser,
        json_mode=args.json,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
        source_paths=source_paths,
    )
    source_path, body_text, input_mode = read_recovery_source(
        parser,
        json_mode=args.json,
        raw_source_file=args.source_file,
        use_stdin=args.stdin,
        project_root=workspace.project_root,
        storage_root=workspace.storage_root,
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
        proposals_dir = ensure_managed_directory_chain(
            workspace.storage_root,
            ("companion", "recovery", "proposals"),
            project_root=workspace.project_root,
            create=False,
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
    review_log_dir = workspace.storage_root / "companion" / "recovery" / "review_log"

    proposal_path = resolve_proposal_path(args.proposal_file, proposals_dir, workspace.project_root)
    if not proposal_path.is_file():
        public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
        message = f"Proposal file does not exist: {public_proposal}"
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
        public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
        message = (
            "Proposal file must live under companion/recovery/proposals/: "
            f"{public_proposal}"
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

    review_sections = extract_structured_sections(body_text, REVIEW_SECTION_ALIASES)
    review_action = classify_review_action(review_sections)
    promotion_ready = promotion_ready_for_action(review_action)
    recorded_at = now_iso_timestamp()
    provenance_state_after = None
    new_workspace_revision = None

    try:
        with workspace_write_lock(workspace.project_root, "record_recovery_review.py"):
            proposals_dir = ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "proposals"),
                project_root=workspace.project_root,
                create=False,
            )
            if proposal_path.parent != proposals_dir.resolve():
                public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
                message = (
                    "Proposal file must live under companion/recovery/proposals/: "
                    f"{public_proposal}"
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
            review_log_dir = ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "review_log"),
                project_root=workspace.project_root,
            )
            ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "archive"),
                project_root=workspace.project_root,
            )
            review_path = review_log_dir / f"{proposal_path.stem}.review.md"
            try:
                review_path.lstat()
                review_exists = True
            except FileNotFoundError:
                review_exists = False
            if review_exists:
                public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
                message = f"Refusing to overwrite an existing recovery review: {public_review}"
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
            ensure_managed_directory_chain(
                workspace.storage_root,
                ("companion", "recovery", "review_log"),
                project_root=workspace.project_root,
                create=False,
                )
            proposal_text = read_text(proposal_path)
            state_path = workspace.storage_root / FILE_KEYS["state"]
            next_state_text = None
            if promotion_ready:
                state = load_workspace_state(state_path)
                state["workspace_revision"] += 1
                state["provenance"] = review_imported_baseline_metadata(
                    timestamp=recorded_at,
                    review_action=review_action,
                    proposal_digest=text_digest(proposal_text),
                    review_digest=text_digest(body_text),
                )
                new_workspace_revision = state["workspace_revision"]
                next_state_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
            write_text(review_path, body_text.rstrip("\n") + "\n")
            if next_state_text is not None:
                try:
                    write_text(state_path, next_state_text)
                except OSError:
                    with suppress(FileNotFoundError):
                        review_path.unlink()
                    raise
                provenance_state_after = "review_imported_baseline"
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
    except (OSError, UnicodeDecodeError, ConfigContractError) as exc:
        message = "Filesystem error while recording recovery review."
        if isinstance(exc, ConfigContractError):
            message = str(exc)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload_for_exception(
                exc,
                default_reason="damaged_sidecar",
                extra={"error_type": type(exc).__name__},
            ),
        )

    payload = {
        "ok": True,
        "proposal_path": str(proposal_path),
        "review_path": str(review_path),
        "input_mode": input_mode,
        "source_file": str(source_path) if source_path is not None else None,
        "source_digest": text_digest(body_text),
        "review_sections_present": sorted(review_sections.keys()),
        "review_action": review_action,
        "promotion_ready": promotion_ready,
        "provenance_state_after": provenance_state_after,
        "new_workspace_revision": new_workspace_revision,
        "workspace_revision_bumped": new_workspace_revision is not None,
        "recorded_at": recorded_at,
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
        public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
        print(f"Recorded recovery review: {public_review}")


if __name__ == "__main__":
    main()
