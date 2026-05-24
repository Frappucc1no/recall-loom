#!/usr/bin/env python3
"""Inspect or remove a RecallLoom project write lock."""

from __future__ import annotations

import argparse
import json

from _common import (
    cli_failure_payload,
    cli_failure_payload_for_exception,
    ConfigContractError,
    EnvironmentContractError,
    enforce_package_support_gate,
    StorageResolutionError,
    ensure_supported_python_version,
    exit_with_cli_error,
    exit_with_failure_contract,
    find_recovery_project_root,
    load_lock_payload,
    pid_is_alive,
    public_json_payload,
    project_lock_path,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or remove a RecallLoom project write lock."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project path or a descendant path. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Remove the lock file. Without this flag, the script only reports lock state.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow lock removal even if the recorded pid still appears alive.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser


def resolve_project_root(path_arg: str):
    return find_recovery_project_root(path_arg)


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
    enforce_package_support_gate(
        parser,
        json_mode=args.json,
        action_name="unlock_write_lock.py",
        action_level="mutating" if args.yes else "diagnostic",
    )

    try:
        project_root = resolve_project_root(args.path)
        lock_path = project_lock_path(project_root)
        lock_exists = lock_path.is_file()
        lock_payload = load_lock_payload(lock_path) if lock_exists else {}
        lock_pid = lock_payload.get("pid")
        pid_alive = bool(isinstance(lock_pid, int) and pid_is_alive(lock_pid))
    except (StorageResolutionError, ConfigContractError) as exc:
        exit_with_cli_error(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=str(exc),
            payload=cli_failure_payload_for_exception(exc, default_reason="damaged_sidecar"),
        )
    except (OSError, UnicodeDecodeError) as exc:
        message = f"Filesystem error: {exc}"
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=2,
            message=message,
            reason="damaged_sidecar",
        )

    if args.yes and lock_exists and pid_alive and not args.force:
        exit_with_failure_contract(
            parser,
            json_mode=args.json,
            exit_code=3,
            message=(
                "Refusing to remove the write lock because the recorded pid still appears alive. "
                "Re-run with --force only if you are sure the lock is stale."
            ),
            reason="write_lock_busy",
            details={
                "project_root": str(project_root),
                "lock_path": str(lock_path),
                "lock_payload": lock_payload,
            },
        )

    removed = False
    if args.yes and lock_exists:
        try:
            lock_path.unlink()
        except OSError as exc:
            message = f"Filesystem error while removing write lock {lock_path}: {exc}"
            exit_with_failure_contract(
                parser,
                json_mode=args.json,
                exit_code=2,
                message=message,
                reason="damaged_sidecar",
            )
        removed = True

    payload = {
        "project_root": str(project_root),
        "lock_path": str(lock_path),
        "dry_run": not args.yes,
        "force": args.force,
        "lock_exists": lock_exists,
        "lock_payload": lock_payload if lock_payload else None,
        "pid_alive": pid_alive,
        "removed": removed,
    }

    if args.json:
        print(
            json.dumps(
                public_json_payload(payload, project_root=project_root),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not lock_exists:
            print(f"No RecallLoom write lock found at {lock_path}.")
        elif args.yes:
            print(f"Removed RecallLoom write lock: {lock_path}")
        else:
            print(f"RecallLoom write lock found: {lock_path}")
            if lock_payload:
                print(f"  owner: {lock_payload.get('owner', 'unknown')}")
                print(f"  pid: {lock_payload.get('pid', 'unknown')}")
                print(f"  created_at: {lock_payload.get('created_at', 'unknown')}")
            print(f"  pid_alive: {'yes' if pid_alive else 'no'}")


if __name__ == "__main__":
    main()
