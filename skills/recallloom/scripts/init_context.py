#!/usr/bin/env python3
"""Initialize a RecallLoom file structure in a target project."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.bridge.blocks import bridge_block_integrity
from core.continuity.freshness import (
    continuity_state_for_workspace as shared_continuity_state_for_workspace,
    summary_matches_empty_shell_template as shared_summary_matches_empty_shell_template,
)
from core.failure.contracts import failure_payload
from core.output.privacy import publicize_json_value
from core.protocol.contracts import (
    BRIDGE_START,
    DEFAULT_WORKSPACE_LANGUAGE,
    FILE_KEYS,
    ROOT_ENTRY_CANDIDATES,
    SUPPORTED_STORAGE_MODES,
    SUPPORTED_WORKSPACE_LANGUAGES,
)
from core.protocol.markers import parse_file_state_marker
from core.protocol.sections import extract_section_text
from core.protocol.templates import render_template

from _common import (
    ConfigContractError,
    DAILY_LOGS_DIRNAME,
    DEFAULT_STORAGE_MODE,
    EnvironmentContractError,
    enforce_package_support_gate,
    exit_if_startup_scratch_residue,
    exit_with_cli_error,
    LockBusyError,
    VISIBLE_STORAGE_MODE,
    config_path_for_mode,
    config_payload,
    ensure_git_exclude_entry,
    ensure_supported_python_version,
    initial_workspace_state,
    is_recovery_storage_candidate,
    latest_active_daily_log,
    load_and_validate_config,
    load_workspace_state,
    managed_file_contract_issue,
    now_iso_timestamp,
    read_text,
    restore_text_snapshot,
    storage_root_for_mode,
    storage_root_boundary_issue,
    today_iso,
    MANAGED_ASSET_REQUIRED_DIRECTORIES,
    MANAGED_ASSET_REQUIRED_FILES,
    unknown_storage_assets,
    validate_iso_date,
    validate_storage_mode,
    validate_tool_name,
    validate_workspace_language,
    workspace_write_lock,
    write_text,
)

def summary_matches_empty_shell_template(summary_text: str) -> bool:
    return shared_summary_matches_empty_shell_template(summary_text)


def continuity_state_for_workspace(
    *,
    state: dict,
    summary_text: str,
    latest_daily_log_exists: bool,
) -> tuple[str, bool]:
    return shared_continuity_state_for_workspace(
        state=state,
        summary_text=summary_text,
        latest_daily_log_exists=latest_daily_log_exists,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize a RecallLoom file structure in a project directory."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Project root directory to initialize. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--tool-name",
        default="RecallLoom",
        help="Tool name used in generated metadata such as the rolling summary marker.",
    )
    parser.add_argument(
        "--date",
        default=today_iso(),
        help="Date to use for generated metadata and optional daily log file.",
    )
    parser.add_argument(
        "--storage-mode",
        default=DEFAULT_STORAGE_MODE,
        choices=sorted(SUPPORTED_STORAGE_MODES),
        help="Storage layout mode. Defaults to hidden sidecar mode.",
    )
    parser.add_argument(
        "--workspace-language",
        default=DEFAULT_WORKSPACE_LANGUAGE,
        choices=sorted(SUPPORTED_WORKSPACE_LANGUAGES),
        help="Language used for generated workspace files.",
    )
    parser.add_argument(
        "--create-daily-log",
        action="store_true",
        help="Optionally create today's daily log scaffold during initialization.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Apply first-time initialization writes even if a managed file path already exists; reruns on a healthy workspace remain idempotent and do not rebuild it.",
    )
    parser.add_argument(
        "--skip-git-exclude",
        action="store_true",
        help="Do not add .recallloom/ to .git/info/exclude when using hidden mode in a git repo.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON summary instead of human-readable output.",
    )
    return parser


def write_if_needed(path: Path, text: str, force: bool, created: list[str], skipped: list[str]) -> None:
    if path.exists() and not force:
        skipped.append(str(path))
        return
    write_text(path, text)
    created.append(str(path))


def bridge_state_snapshot(workspace, state: dict, timestamp: str) -> tuple[dict, bool]:
    latest_daily_log = latest_active_daily_log(workspace.storage_root / DAILY_LOGS_DIRNAME)
    next_entries: dict[str, dict] = {}
    changed = False
    for rel_path in ROOT_ENTRY_CANDIDATES:
        target = workspace.project_root / rel_path
        if not target.is_file():
            continue
        text = read_text(target)
        ok, reason = bridge_block_integrity(text)
        if not ok:
            raise ConfigContractError(
                f"Refusing to initialize because {target} contains a malformed managed bridge block ({reason})."
            )
        has_bridge_block = BRIDGE_START in text
        rel_key = rel_path.as_posix()
        if has_bridge_block:
            next_entries[rel_key] = {
                "update_protocol_revision_seen": state["update_protocol_revision"],
                "latest_daily_log_seen": (
                    latest_daily_log.relative_to(workspace.storage_root).as_posix()
                    if latest_daily_log is not None
                    else None
                ),
                "updated_at": timestamp,
            }
    if next_entries != state.get("bridged_entries", {}):
        changed = True
    state["bridged_entries"] = next_entries
    return state, changed


def rollback_init_writes(file_snapshots: list[tuple[Path, bool, str]], created_dirs: list[Path]) -> None:
    for path, existed, text in reversed(file_snapshots):
        restore_text_snapshot(path, existed=existed, text=text)
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def existing_storage_issue(
    *,
    storage_root: Path,
    project_root: Path,
    storage_mode: str,
    workspace_language: str,
) -> str | None:
    boundary_issue = storage_root_boundary_issue(project_root, storage_root, storage_mode)
    if boundary_issue is not None:
        return boundary_issue
    if not storage_root.exists():
        return None
    if not storage_root.is_dir():
        return f"Refusing to initialize because the storage root path already exists as a non-directory: {storage_root}"

    has_content = any(storage_root.iterdir())
    if not has_content:
        return None

    unknown_assets = unknown_storage_assets(storage_root)
    if unknown_assets:
        return (
            "Refusing to initialize because the storage root already contains non-managed assets:\n"
            + "\n".join(str(path) for path in unknown_assets)
        )

    config_path = storage_root / FILE_KEYS["config"]
    if not config_path.is_file():
        return (
            "Refusing to initialize because the storage root already contains files but no valid config.json. "
            "This looks like a partial or non-RecallLoom sidecar."
        )

    try:
        existing_config = load_and_validate_config(config_path, storage_mode)
    except ConfigContractError as exc:
        return (
            "Refusing to initialize because the existing storage root has an invalid config.json. "
            f"Details: {exc}"
        )

    state_path = storage_root / FILE_KEYS["state"]
    if not state_path.is_file():
        return (
            "Refusing to initialize because the existing storage root is missing required state.json. "
            "This looks like a partial or damaged RecallLoom sidecar."
        )
    try:
        load_workspace_state(state_path)
    except ConfigContractError as exc:
        return (
            "Refusing to initialize because the existing storage root has an invalid state.json. "
            f"Details: {exc}"
        )

    if existing_config["workspace_language"] != workspace_language:
        return (
            "Refusing to initialize because the existing workspace uses a different workspace_language "
            f"({existing_config['workspace_language']} vs requested {workspace_language})."
        )

    required_files = {
        rel_path: storage_root / rel_path
        for rel_path in MANAGED_ASSET_REQUIRED_FILES
        if rel_path not in {FILE_KEYS["config"], FILE_KEYS["state"]}
    }
    missing_files = [name for name, path in required_files.items() if not path.is_file()]
    if missing_files:
        return (
            "Refusing to initialize because the existing storage root is missing required managed files "
            "for a healthy workspace: "
            + ", ".join(missing_files)
            + ". This looks like a partial or damaged RecallLoom sidecar."
        )

    required_dirs = {rel_path: storage_root / rel_path for rel_path in MANAGED_ASSET_REQUIRED_DIRECTORIES}
    missing_dirs = [name for name, path in required_dirs.items() if not path.is_dir()]
    if missing_dirs:
        return (
            "Refusing to initialize because the existing storage root is missing required managed directories "
            "for a healthy workspace: "
            + ", ".join(missing_dirs)
            + ". This looks like a partial or damaged RecallLoom sidecar."
        )

    contract_checks = [
        (
            storage_root / FILE_KEYS["context_brief"],
            "context_brief",
            existing_config["workspace_language"],
            existing_config["protocol_version"],
        ),
        (
            storage_root / FILE_KEYS["rolling_summary"],
            "rolling_summary",
            existing_config["workspace_language"],
            existing_config["protocol_version"],
        ),
    ]
    update_protocol_path = storage_root / FILE_KEYS["update_protocol"]
    if update_protocol_path.exists():
        contract_checks.append(
            (
                update_protocol_path,
                "update_protocol",
                existing_config["workspace_language"],
                existing_config["protocol_version"],
            )
        )
    for path, file_key, expected_language, expected_version in contract_checks:
        issue = managed_file_contract_issue(
            path,
            file_key=file_key,
            workspace_language=expected_language,
            expected_protocol_version=expected_version,
        )
        if issue is not None:
            return (
                "Refusing to initialize because the existing storage root contains a damaged managed file. "
                f"Details: {issue}"
            )

    return None


PROJECT_ROOT_SIGNAL_FILES = {
    "README.md",
    "README.en.md",
    "README.zh-CN.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "requirements.txt",
}

PROJECT_ROOT_SIGNAL_DIRS = {
    ".git",
    ".github",
    "docs",
    "src",
    "app",
    "apps",
}


def looks_like_project_root(project_root: Path) -> bool:
    if not any(project_root.iterdir()):
        return True
    for name in PROJECT_ROOT_SIGNAL_FILES:
        if (project_root / name).exists():
            return True
    for name in PROJECT_ROOT_SIGNAL_DIRS:
        if (project_root / name).exists():
            return True
    for storage_mode in (DEFAULT_STORAGE_MODE, VISIBLE_STORAGE_MODE):
        if is_recovery_storage_candidate(
            project_root,
            storage_root_for_mode(project_root, storage_mode),
            storage_mode,
        ):
            return True
    return False


def init_failure_payload(
    *,
    project_root: Path,
    workspace_language: str,
    reason: str | None = None,
    error: str | None = None,
) -> dict:
    payload = {"project_root": str(project_root), "initialized": False}
    if reason is None:
        publicized = publicize_json_value(payload, project_root=project_root)
        return publicized if isinstance(publicized, dict) else payload
    payload.update(failure_payload(reason, language=workspace_language, error=error))
    publicized = publicize_json_value(payload, project_root=project_root)
    return publicized if isinstance(publicized, dict) else payload


def infer_init_failure_reason(message: str) -> str | None:
    lowered = message.lower()
    if "python 3.10+" in lowered or "runtime bootstrap failed" in lowered:
        return "python_runtime_unavailable"
    if (
        "does not look like a project root" in lowered
        or "target path does not exist" in lowered
        or "target path is not a directory" in lowered
    ):
        return "not_project_root"
    if "different storage mode" in lowered or "conflicting or partial recallloom sidecar" in lowered:
        return "dual_sidecar_conflict"
    if any(token in lowered for token in ("damaged", "partial", "symlink", "non-directory")):
        return "damaged_sidecar"
    return None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    created_dirs: list[Path] = []
    file_snapshots: list[tuple[Path, bool, str]] = []
    exclude_snapshot: tuple[Path, bool, str] | None = None
    requested_workspace_language = args.workspace_language
    try:
        ensure_supported_python_version()
    except EnvironmentContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=init_failure_payload(
                project_root=Path(args.target).expanduser().resolve(),
                workspace_language=requested_workspace_language,
                reason="python_runtime_unavailable",
            ),
        )
    enforce_package_support_gate(parser, json_mode=args.json)

    target_path = Path(args.target).expanduser()
    if not target_path.exists():
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Target path does not exist: {target_path.resolve()}",
            payload=init_failure_payload(
                project_root=target_path.expanduser().resolve(),
                workspace_language=requested_workspace_language,
                reason="not_project_root",
            ),
        )
    project_root = target_path.resolve()
    if not project_root.is_dir():
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Target path is not a directory: {project_root}",
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=requested_workspace_language,
                reason="not_project_root",
            ),
        )
    if not looks_like_project_root(project_root):
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=(
                "Target path does not look like a project root yet. "
                "Create or point RecallLoom at an existing project directory with recognizable project signals first."
            ),
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=requested_workspace_language,
                reason="not_project_root",
            ),
        )

    if not validate_iso_date(args.date):
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Invalid --date value: {args.date}. Expected YYYY-MM-DD.",
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=requested_workspace_language,
                reason="invalid_date",
            ),
        )

    storage_mode = validate_storage_mode(args.storage_mode)
    workspace_language = validate_workspace_language(args.workspace_language)
    startup_residue_report = exit_if_startup_scratch_residue(
        parser,
        json_mode=args.json,
        project_root=project_root,
        storage_root=storage_root_for_mode(project_root, storage_mode),
    )
    try:
        tool_name = validate_tool_name(args.tool_name)
    except ConfigContractError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=workspace_language,
                reason="invalid_tool_name",
            ),
        )

    try:
        with workspace_write_lock(project_root, "init_context.py"):
            opposite_mode = VISIBLE_STORAGE_MODE if storage_mode == DEFAULT_STORAGE_MODE else DEFAULT_STORAGE_MODE
            opposite_root = storage_root_for_mode(project_root, opposite_mode)
            opposite_config = config_path_for_mode(project_root, opposite_mode)
            opposite_boundary_issue = storage_root_boundary_issue(project_root, opposite_root, opposite_mode)
            if opposite_boundary_issue is not None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=opposite_boundary_issue,
                    payload=init_failure_payload(
                        project_root=project_root,
                        workspace_language=workspace_language,
                        reason="invalid_storage_boundary",
                    ),
                )
            if opposite_config.exists():
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                            "A RecallLoom workspace already exists in a different storage mode for this project. "
                        "Remove it first or keep using the existing mode."
                    ),
                    payload=init_failure_payload(
                        project_root=project_root,
                        workspace_language=workspace_language,
                        reason="dual_sidecar_conflict",
                    ),
                )
            if opposite_root.exists():
                if opposite_root.is_file():
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=(
                            "A path for the opposite storage mode already exists and is not a directory. "
                            "Remove it before initializing this project."
                        ),
                        payload=init_failure_payload(
                            project_root=project_root,
                            workspace_language=workspace_language,
                            reason="dual_sidecar_conflict",
                        ),
                    )
                if is_recovery_storage_candidate(project_root, opposite_root, opposite_mode):
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=(
                            "A sidecar-like storage root already exists for the opposite storage mode. "
                            "This looks like a conflicting or partial RecallLoom sidecar. Remove or repair it before initializing this project."
                        ),
                        payload=init_failure_payload(
                            project_root=project_root,
                            workspace_language=workspace_language,
                            reason="dual_sidecar_conflict",
                        ),
                    )

            storage_root = storage_root_for_mode(project_root, storage_mode)
            boundary_issue = storage_root_boundary_issue(project_root, storage_root, storage_mode)
            if boundary_issue is not None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=boundary_issue,
                    payload=init_failure_payload(
                        project_root=project_root,
                        workspace_language=workspace_language,
                        reason="invalid_storage_boundary",
                    ),
                )
            if storage_root.exists() and not storage_root.is_dir():
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=(
                        f"Refusing to initialize because the storage root path already exists as a non-directory: {storage_root}"
                    ),
                    payload=init_failure_payload(
                        project_root=project_root,
                        workspace_language=workspace_language,
                        reason="damaged_sidecar",
                    ),
                )
            issue = existing_storage_issue(
                storage_root=storage_root,
                project_root=project_root,
                storage_mode=storage_mode,
                workspace_language=workspace_language,
            )
            if issue is not None:
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=issue,
                    payload=init_failure_payload(
                        project_root=project_root,
                        workspace_language=workspace_language,
                        reason="damaged_sidecar",
                    ),
                )

            existing_valid_storage = storage_root.exists() and any(storage_root.iterdir())
            if existing_valid_storage:
                state_path = storage_root / FILE_KEYS["state"]
                state = load_workspace_state(state_path)
                summary_path = storage_root / FILE_KEYS["rolling_summary"]
                summary_text = read_text(summary_path)
                summary_state = parse_file_state_marker(summary_text)
                continuity_state, continuity_seeded = continuity_state_for_workspace(
                    state=state,
                    summary_text=summary_text,
                    latest_daily_log_exists=latest_active_daily_log(storage_root / DAILY_LOGS_DIRNAME) is not None,
                )
                requested_daily_log = storage_root / DAILY_LOGS_DIRNAME / f"{args.date}.md"
                if args.create_daily_log and not requested_daily_log.exists():
                    exit_with_cli_error(
                        parser,
                        json_mode=args.json,
                        exit_code=2,
                        message=(
                            "Refusing to create a new daily log during re-initialization of an existing workspace. "
                            "Use append_daily_log_entry.py for new milestone entries instead."
                        ),
                        payload=init_failure_payload(
                            project_root=project_root,
                            workspace_language=workspace_language,
                            reason="reinit_create_daily_log_not_allowed",
                        ),
                    )

                git_exclude_updated = False
                state_dirty = False
                if storage_mode == DEFAULT_STORAGE_MODE and not args.skip_git_exclude:
                    git_exclude_updated = ensure_git_exclude_entry(project_root)
                    if git_exclude_updated and state.get("git_exclude_mode") != "managed":
                        state["git_exclude_mode"] = "managed"
                        state_dirty = True

                state, state_reconciled = bridge_state_snapshot(
                    workspace=type(
                        "InitWorkspace",
                        (),
                        {
                            "project_root": project_root,
                            "storage_root": storage_root,
                            "update_protocol_revision": state["update_protocol_revision"],
                        },
                    )(),
                    state=state,
                    timestamp=now_iso_timestamp(),
                )
                created: list[str] = []
                if state_reconciled or state_dirty:
                    write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                    created.append(str(state_path))

                skipped: list[str] = []
                created_set = set(created)
                for path in (
                    storage_root / FILE_KEYS["context_brief"],
                    storage_root / FILE_KEYS["rolling_summary"],
                    storage_root / FILE_KEYS["update_protocol"],
                    config_path_for_mode(project_root, storage_mode),
                    storage_root / FILE_KEYS["state"],
                ):
                    if path.exists() and str(path) not in created_set:
                        skipped.append(str(path))
                if args.create_daily_log and requested_daily_log.exists():
                    skipped.append(str(requested_daily_log))

                summary = {
                    "project_root": str(project_root),
                    "storage_root": str(storage_root),
                    "storage_mode": storage_mode,
                    "workspace_language": workspace_language,
                    "created": created,
                    "skipped": skipped,
                    "daily_log_created": False,
                    "config_created": False,
                    "git_exclude_updated": git_exclude_updated,
                    "already_initialized": True,
                    "state_reconciled": state_reconciled,
                    "continuity_state": continuity_state,
                    "continuity_seeded": continuity_seeded,
                }
                if args.json:
                    public_summary = publicize_json_value(summary, project_root=project_root)
                    print(json.dumps(public_summary, ensure_ascii=False, indent=2))
                else:
                    print(f"RecallLoom is already initialized in {project_root}")
                    print(f"Storage mode: {storage_mode}")
                    print(f"Workspace language: {workspace_language}")
                    print(f"Storage root: {storage_root}")
                    print(f"Continuity state: {continuity_state}")
                    if skipped:
                        print("Skipped existing:")
                        for item in skipped:
                            print(f"  - {item}")
                    if git_exclude_updated:
                        print("Updated .git/info/exclude for .recallloom/")
                return

            storage_root_preexisted = storage_root.exists()
            storage_root.mkdir(parents=True, exist_ok=True)
            required_directory_paths = [
                storage_root / rel_path for rel_path in MANAGED_ASSET_REQUIRED_DIRECTORIES
            ]
            daily_logs_dir = storage_root / DAILY_LOGS_DIRNAME
            preexisting_dirs = {path: path.exists() for path in required_directory_paths}
            for directory in sorted(required_directory_paths, key=lambda item: len(item.parts)):
                directory.mkdir(parents=True, exist_ok=True)
            if not storage_root_preexisted:
                created_dirs.append(storage_root)
            for directory in required_directory_paths:
                if not preexisting_dirs[directory]:
                    created_dirs.append(directory)

            created: list[str] = []
            skipped: list[str] = []

            timestamp = now_iso_timestamp()
            workspace_revision = 1

            managed_files = {
                storage_root / FILE_KEYS["context_brief"]: render_template(
                    "context_brief",
                    tool_name=tool_name,
                    day=args.date,
                    language=workspace_language,
                    timestamp=timestamp,
                    workspace_revision=workspace_revision,
                ),
                storage_root / FILE_KEYS["rolling_summary"]: render_template(
                    "rolling_summary",
                    tool_name=tool_name,
                    day=args.date,
                    language=workspace_language,
                    timestamp=timestamp,
                    workspace_revision=workspace_revision,
                ),
                storage_root / FILE_KEYS["update_protocol"]: render_template(
                    "update_protocol",
                    tool_name=tool_name,
                    day=args.date,
                    language=workspace_language,
                    timestamp=timestamp,
                    workspace_revision=workspace_revision,
                ),
            }

            if args.create_daily_log:
                managed_files[daily_logs_dir / f"{args.date}.md"] = render_template(
                    "daily_log",
                    tool_name=tool_name,
                    day=args.date,
                    language=workspace_language,
                    timestamp=timestamp,
                    workspace_revision=workspace_revision,
                )

            for path, text in managed_files.items():
                if path.exists() and not args.force:
                    skipped.append(str(path))
                    continue
                existed = path.exists()
                previous_text = read_text(path) if existed else ""
                write_text(path, text)
                file_snapshots.append((path, existed, previous_text))
                created.append(str(path))

            config_created = False
            config_path = config_path_for_mode(project_root, storage_mode)
            payload = config_payload(
                storage_mode=storage_mode,
                workspace_language=workspace_language,
                created_by=tool_name,
                created_at=timestamp,
            )
            if config_path.exists() and not args.force:
                skipped.append(str(config_path))
            else:
                existed = config_path.exists()
                previous_text = read_text(config_path) if existed else ""
                write_text(config_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                file_snapshots.append((config_path, existed, previous_text))
                created.append(str(config_path))
                config_created = True

            git_exclude_updated = False
            if storage_mode == DEFAULT_STORAGE_MODE and not args.skip_git_exclude:
                exclude_path = project_root / ".git" / "info" / "exclude"
                exclude_snapshot = (
                    exclude_path,
                    exclude_path.exists(),
                    read_text(exclude_path) if exclude_path.exists() else "",
                )
                git_exclude_updated = ensure_git_exclude_entry(project_root)

            git_exclude_mode = (
                "managed"
                if storage_mode == DEFAULT_STORAGE_MODE and git_exclude_updated
                else "skipped"
                if storage_mode == DEFAULT_STORAGE_MODE
                else "not_applicable"
            )

            state_path = storage_root / FILE_KEYS["state"]
            state_payload = initial_workspace_state(
                tool_name=tool_name,
                timestamp=timestamp,
                git_exclude_mode=git_exclude_mode,
                daily_log_cursor=(
                    {
                        "latest_file": f"{DAILY_LOGS_DIRNAME}/{args.date}.md",
                        "latest_entry_id": None,
                        "latest_entry_seq": None,
                        "entry_count": 0,
                    }
                    if args.create_daily_log
                    else None
                ),
            )
            try:
                state_payload, state_reconciled = bridge_state_snapshot(
                    workspace=type(
                        "InitWorkspace",
                        (),
                        {
                            "project_root": project_root,
                            "storage_root": storage_root,
                            "update_protocol_revision": state_payload["update_protocol_revision"],
                        },
                    )(),
                    state=state_payload,
                    timestamp=timestamp,
                )
            except ConfigContractError as exc:
                rollback_init_writes(file_snapshots, created_dirs)
                if exclude_snapshot is not None:
                    restore_text_snapshot(
                        exclude_snapshot[0], existed=exclude_snapshot[1], text=exclude_snapshot[2]
                    )
                exit_with_cli_error(
                    parser,
                    json_mode=args.json,
                    exit_code=2,
                    message=str(exc),
                    payload=init_failure_payload(
                        project_root=project_root,
                        workspace_language=workspace_language,
                        reason=infer_init_failure_reason(str(exc)),
                    ),
                )
            if state_path.exists() and not args.force:
                skipped.append(str(state_path))
            else:
                existed = state_path.exists()
                previous_text = read_text(state_path) if existed else ""
                write_text(state_path, json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n")
                file_snapshots.append((state_path, existed, previous_text))
                created.append(str(state_path))

            rendered_summary_text = managed_files[storage_root / FILE_KEYS["rolling_summary"]]
            rendered_summary_state = parse_file_state_marker(rendered_summary_text)
            continuity_state, continuity_seeded = continuity_state_for_workspace(
                state=state_payload,
                summary_text=rendered_summary_text,
                latest_daily_log_exists=False,
            )
            summary = {
                "project_root": str(project_root),
                "storage_root": str(storage_root),
                "storage_mode": storage_mode,
                "workspace_language": workspace_language,
                "created": created,
                "skipped": skipped,
                "daily_log_created": args.create_daily_log,
                "config_created": config_created,
                "git_exclude_updated": git_exclude_updated,
                "state_reconciled": state_reconciled,
                "continuity_state": continuity_state,
                "continuity_seeded": continuity_seeded,
            }
            if startup_residue_report is not None:
                summary["startup_residue_report"] = startup_residue_report
    except LockBusyError as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=str(exc),
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=requested_workspace_language,
            ),
        )
    except ConfigContractError as exc:
        try:
            rollback_init_writes(file_snapshots, created_dirs)
        except OSError:
            pass
        if exclude_snapshot is not None:
            try:
                restore_text_snapshot(exclude_snapshot[0], existed=exclude_snapshot[1], text=exclude_snapshot[2])
            except OSError:
                pass
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=requested_workspace_language,
                reason=infer_init_failure_reason(str(exc)),
            ),
        )
    except (OSError, UnicodeDecodeError) as exc:
        try:
            rollback_init_writes(file_snapshots, created_dirs)
        except OSError:
            pass
        if "exclude_snapshot" in locals() and exclude_snapshot is not None:
            try:
                restore_text_snapshot(exclude_snapshot[0], existed=exclude_snapshot[1], text=exclude_snapshot[2])
            except OSError:
                pass
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=f"Filesystem error: {exc}",
            payload=init_failure_payload(
                project_root=project_root,
                workspace_language=requested_workspace_language,
            ),
        )

    if args.json:
        public_summary = publicize_json_value(summary, project_root=project_root)
        print(json.dumps(public_summary, ensure_ascii=False, indent=2))
    else:
        print(f"Initialized RecallLoom in {project_root}")
        print(f"Storage mode: {storage_mode}")
        print(f"Workspace language: {workspace_language}")
        print(f"Storage root: {storage_root}")
        print(f"Continuity state: {summary['continuity_state']}")
        if created:
            print("Created:")
            for item in created:
                print(f"  - {item}")
        if skipped:
            print("Skipped existing:")
            for item in skipped:
                print(f"  - {item}")
        if git_exclude_updated:
            print("Updated .git/info/exclude for .recallloom/")


if __name__ == "__main__":
    main()
