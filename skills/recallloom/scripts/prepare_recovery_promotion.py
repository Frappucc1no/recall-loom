#!/usr/bin/env python3
"""Prepare structured promotion context for a reviewed recovery proposal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.coldstart.structured import (
    PROPOSAL_SECTION_ALIASES,
    REVIEW_SECTION_ALIASES,
    classify_review_action,
    detect_promotion_targets,
    detect_source_tiers,
    extract_structured_sections,
    promotion_ready_for_action,
)
from core.protocol.contracts import FILE_KEYS
from core.protocol.markers import parse_file_state_marker
from core.provenance.state import provenance_facts_from_state

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    EnvironmentContractError,
    enforce_package_support_gate,
    ensure_managed_directory_chain,
    ensure_supported_python_version,
    exit_with_cli_error,
    find_recallloom_root,
    ManagedDirectorySafetyError,
    latest_active_daily_log,
    load_workspace_state,
    parse_daily_log_entry_line,
    public_project_path,
    public_json_payload,
    read_text,
    RECOVERY_PROPOSAL_FILE_RE,
    REVIEW_RECORD_FILE_RE,
    StorageResolutionError,
    text_digest,
    validate_recovery_proposal_text,
    validate_recovery_review_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare safe-write promotion context for a reviewed recovery proposal."
    )
    parser.add_argument("path", nargs="?", default=".", help="Project path or a descendant path.")
    parser.add_argument(
        "--proposal-file",
        required=True,
        help="Proposal filename or path. Relative values are resolved against companion/recovery/proposals/ first.",
    )
    parser.add_argument(
        "--review-file",
        help=(
            "Optional review filename or path. Relative values are resolved against "
            "companion/recovery/review_log/ first. Defaults to <proposal-stem>.review.md."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def resolve_candidate_path(raw_value: str, base_dir: Path, project_root: Path) -> Path:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    base_relative = base_dir / raw_value
    if base_relative.exists():
        return base_relative.resolve()

    project_relative = project_root / raw_value
    return project_relative.resolve()


def latest_daily_log_entry_info(latest_daily_log: Path | None):
    if latest_daily_log is None:
        return None
    latest_entry = None
    for line in read_text(latest_daily_log).splitlines():
        entry = parse_daily_log_entry_line(line)
        if entry is not None:
            latest_entry = entry
    return latest_entry


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

    try:
        proposals_dir = ensure_managed_directory_chain(
            workspace.storage_root,
            ("companion", "recovery", "proposals"),
            project_root=workspace.project_root,
            create=False,
        ).resolve()
        review_log_dir = ensure_managed_directory_chain(
            workspace.storage_root,
            ("companion", "recovery", "review_log"),
            project_root=workspace.project_root,
            create=False,
        ).resolve()
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

    proposal_path = resolve_candidate_path(args.proposal_file, proposals_dir, workspace.project_root)
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
    if proposal_path.parent != proposals_dir:
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

    review_name = args.review_file or f"{proposal_path.stem}.review.md"
    review_path = resolve_candidate_path(review_name, review_log_dir, workspace.project_root)
    if not review_path.is_file():
        public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
        message = f"Review file does not exist: {public_review}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"review_file": str(review_path)},
            ),
        )
    if review_path.parent != review_log_dir:
        public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
        message = (
            "Review file must live under companion/recovery/review_log/: "
            f"{public_review}"
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"review_file": str(review_path)},
            ),
        )
    if not REVIEW_RECORD_FILE_RE.match(review_path.name):
        message = (
            "Review filename does not match the expected review record shape: "
            f"{review_path.name}"
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={"review_file": str(review_path)},
            ),
        )
    expected_review_name = f"{proposal_path.stem}.review.md"
    if review_path.name != expected_review_name:
        message = (
            "Review filename must map to the proposal stem exactly. "
            f"Expected {expected_review_name}, found {review_path.name}."
        )
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "invalid_prepared_input",
                error=message,
                details={
                    "expected_review_file": expected_review_name,
                    "review_file": str(review_path),
                },
            ),
        )

    try:
        proposal_text = read_text(proposal_path)
        review_text = read_text(review_path)
    except (OSError, UnicodeDecodeError) as exc:
        message = "Filesystem error while reading recovery proposal or review."
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
    if not proposal_text.strip():
        public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
        message = f"Proposal file is empty: {public_proposal}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=message,
                details={"proposal_file": str(proposal_path)},
            ),
        )
    if not review_text.strip():
        public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
        message = f"Review file is empty: {public_review}"
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=message,
                details={"review_file": str(review_path)},
            ),
        )
    proposal_errors = validate_recovery_proposal_text(proposal_text)
    if proposal_errors:
        message = "Recovery proposal failed structure checks:\n- " + "\n- ".join(proposal_errors)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=message,
                details={"proposal_errors": proposal_errors, "proposal_file": str(proposal_path)},
            ),
        )
    review_errors = validate_recovery_review_text(review_text)
    if review_errors:
        message = "Recovery review failed structure checks:\n- " + "\n- ".join(review_errors)
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            payload=cli_failure_payload(
                "malformed_managed_file",
                error=message,
                details={"review_errors": review_errors, "review_file": str(review_path)},
            ),
        )
    proposal_sections = extract_structured_sections(proposal_text, PROPOSAL_SECTION_ALIASES)
    review_sections = extract_structured_sections(review_text, REVIEW_SECTION_ALIASES)
    source_tiers_detected = detect_source_tiers(proposal_text)
    promotion_targets_detected = detect_promotion_targets(proposal_text)
    review_action = classify_review_action(review_sections)
    promotion_ready = promotion_ready_for_action(review_action)

    try:
        state_path = workspace.storage_root / FILE_KEYS["state"]
        state = load_workspace_state(state_path)

        summary_path = workspace.storage_root / FILE_KEYS["rolling_summary"]
        context_brief_path = workspace.storage_root / FILE_KEYS["context_brief"]
        if not summary_path.is_file():
            public_summary = public_project_path(summary_path, project_root=workspace.project_root) or summary_path.name
            message = f"Missing required file: {public_summary}"
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload(
                    "damaged_sidecar",
                    error=message,
                    details={"path": str(summary_path)},
                ),
            )
        summary_state = parse_file_state_marker(read_text(summary_path))
        if summary_state is None:
            public_summary = public_project_path(summary_path, project_root=workspace.project_root) or summary_path.name
            message = f"Missing required file-state metadata marker: {public_summary}"
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=message,
                    details={"path": str(summary_path)},
                ),
            )
        if not context_brief_path.is_file():
            public_context = public_project_path(context_brief_path, project_root=workspace.project_root) or context_brief_path.name
            message = f"Missing required file: {public_context}"
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload(
                    "damaged_sidecar",
                    error=message,
                    details={"path": str(context_brief_path)},
                ),
            )
        context_brief_state = parse_file_state_marker(read_text(context_brief_path))
        if context_brief_state is None:
            public_context = public_project_path(context_brief_path, project_root=workspace.project_root) or context_brief_path.name
            message = f"Missing required file-state metadata marker: {public_context}"
            exit_with_cli_error(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                payload=cli_failure_payload(
                    "malformed_managed_file",
                    error=message,
                    details={"path": str(context_brief_path)},
                ),
            )
        latest_daily_log = latest_active_daily_log(workspace.storage_root / DAILY_LOGS_DIRNAME)
        latest_daily_log_entry = latest_daily_log_entry_info(latest_daily_log)
        provenance_facts = provenance_facts_from_state(state, review_intent=False)
    except ConfigContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = "Filesystem error while preparing recovery promotion context."
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

    safe_write_context_allowed = (
        promotion_ready and provenance_facts["review_imported_baseline"]
    )
    payload = {
        "ok": True,
        "project_root": str(workspace.project_root),
        "storage_root": str(workspace.storage_root),
        "proposal_path": str(proposal_path),
        "proposal_digest": text_digest(proposal_text),
        "proposal_sections_present": sorted(proposal_sections.keys()),
        "source_tiers_detected": source_tiers_detected,
        "promotion_targets_detected": promotion_targets_detected,
        "review_path": str(review_path),
        "review_digest": text_digest(review_text),
        "review_sections_present": sorted(review_sections.keys()),
        "review_action": review_action,
        "promotion_ready": promotion_ready,
        "provenance_metadata_status": provenance_facts["metadata_status"],
        "provenance_state": (
            "helper_evidenced"
            if provenance_facts["helper_evidenced"]
            else "review_imported_baseline"
            if provenance_facts["review_imported_baseline"]
            else "review_required"
            if provenance_facts["review_required"]
            else "structurally_valid_legacy"
            if provenance_facts["legacy_sidecar"]
            else "structurally_valid"
        ),
        "review_import_does_not_claim_helper_evidenced": True,
        "safe_write_context": ({
            "workspace_revision": state["workspace_revision"],
            "commit_context_file": {
                "rolling_summary": {
                    "path": summary_path.relative_to(workspace.project_root).as_posix(),
                    "expected_file_revision": summary_state.revision if summary_state else None,
                    "expected_workspace_revision": state["workspace_revision"],
                },
                "context_brief": {
                    "path": context_brief_path.relative_to(workspace.project_root).as_posix(),
                    "expected_file_revision": context_brief_state.revision if context_brief_state else None,
                    "expected_workspace_revision": state["workspace_revision"],
                }
                if context_brief_state is not None
                else None,
            },
            "append_daily_log_entry": {
                "latest_file": (
                    latest_daily_log.relative_to(workspace.storage_root).as_posix()
                    if latest_daily_log is not None
                    else None
                ),
                "latest_entry_id": latest_daily_log_entry.entry_id if latest_daily_log_entry else None,
                "latest_entry_seq": latest_daily_log_entry.entry_seq if latest_daily_log_entry else None,
                "suggested_date": latest_daily_log.stem if latest_daily_log is not None else None,
                "expected_workspace_revision": state["workspace_revision"],
            },
        } if safe_write_context_allowed else None),
        "write_context_blocked_reason": (
            None
            if safe_write_context_allowed
            else "review_import_not_recorded"
            if promotion_ready
            else "promotion_not_ready"
        ),
        "notes": [
            "This helper does not promote any content into core continuity files.",
            "Only rolling_summary.md, context_brief.md, and daily log appends are valid promotion targets for reviewed recovery content.",
            "A model or human must still decide what content is durable enough to write and which target file is appropriate.",
            "Safe write context is emitted only after the reviewed import baseline is recorded in state.json.",
        ],
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
        public_proposal = public_project_path(proposal_path, project_root=workspace.project_root) or proposal_path.name
        public_review = public_project_path(review_path, project_root=workspace.project_root) or review_path.name
        print(f"Prepared recovery promotion context for: {public_proposal}")
        print(f"Review record: {public_review}")
        print("Use the returned safe_write_context with the normal write helpers after content review.")


if __name__ == "__main__":
    main()
