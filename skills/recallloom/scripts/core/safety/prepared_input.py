"""Prepared file-input containment for mutating helper inputs."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat


HELPER_SCRATCH_MARKER_FILENAME = ".recallloom-prepared-input-scratch.json"
HELPER_SCRATCH_MARKER_KIND = "recallloom.prepared_input_scratch.v1"
HELPER_SCRATCH_OWNER = "recallloom-helper"
HELPER_SCRATCH_TOKEN_ENV = "RECALLLOOM_PREPARED_INPUT_TOKEN"
MAX_SCRATCH_MARKER_BYTES = 16 * 1024

_SECRET_EXACT_NAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
_SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdbx"}
_SECRET_SEGMENT_RE = re.compile(
    r"(^|[._-])("
    r"api[_-]?key|auth|credential|credentials|oauth|passwd|password|private[_-]?key|"
    r"secret|secrets|token|tokens"
    r")([._-]|$)",
    re.I,
)

_TRANSCRIPT_EXACT_NAMES = {
    ".bash_history",
    ".fish_history",
    ".python_history",
    ".sqlite_history",
    ".zsh_history",
}
_TRANSCRIPT_SUFFIXES = {".log", ".trace"}
_TRANSCRIPT_SEGMENT_RE = re.compile(
    r"("
    r"agent[_-]?runtime[_-]?log|agent[_-]?log|chat[_-]?history|conversation|"
    r"debug[_-]?dump|debug|dump|runtime[_-]?log|session[_-]?log|shell[_-]?history|"
    r"transcript|trace"
    r")",
    re.I,
)
_IDE_HISTORY_SEGMENTS = {
    ".codex",
    ".continue",
    ".cursor",
    ".idea",
    ".vscode",
    ".windsurf",
}

_RELEASE_ARTIFACT_SEGMENTS = {
    "artifact",
    "artifacts",
    "dist",
    "release",
    "release-artifacts",
    "release_artifacts",
    "releases",
}
_RELEASE_ARTIFACT_SUFFIXES = {
    ".tar",
    ".tgz",
    ".whl",
    ".zip",
}
_RELEASE_ARTIFACT_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz")


@dataclass(frozen=True)
class PreparedInputSource:
    """A prepared source path that passed deny-first source-file checks."""

    path: Path
    path_ref: str
    path_category: str
    st_dev: int
    st_ino: int
    st_mode: int
    st_nlink: int
    st_size: int
    scratch_root_ref: str | None = None


class PreparedInputSafetyError(Exception):
    """Public-safe prepared-input failure."""

    def __init__(
        self,
        *,
        reason_code: str,
        path_category: str,
        path_ref: str,
        message: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.path_category = path_category
        self.path_ref = path_ref
        self.message = message
        self.extra = dict(extra or {})
        super().__init__(message)

    @property
    def details(self) -> dict[str, object]:
        details: dict[str, object] = {
            "reason_code": self.reason_code,
            "path_category": self.path_category,
            "path_ref": self.path_ref,
            "side_effect": "none",
        }
        details.update(self.extra)
        return details


def helper_scratch_marker_payload(*, token: str) -> dict[str, object]:
    return {
        "kind": HELPER_SCRATCH_MARKER_KIND,
        "owner": HELPER_SCRATCH_OWNER,
        "token": token,
    }


def _path_ref(path: Path | str) -> str:
    return "sha256:" + hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _safe_path_for_ref(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _safe_resolve_existing(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _fail(
    *,
    reason_code: str,
    path_category: str,
    path_ref: str,
    label: str,
    extra: dict[str, object] | None = None,
) -> PreparedInputSafetyError:
    return PreparedInputSafetyError(
        reason_code=reason_code,
        path_category=path_category,
        path_ref=path_ref,
        message=(
            f"Prepared {label} file was refused before reading "
            f"(reason_code={reason_code}, path_category={path_category}, path_ref={path_ref})."
        ),
        extra=extra,
    )


def _path_segments(path: Path) -> list[str]:
    return [part.casefold() for part in path.parts if part not in {path.anchor, ""}]


def _secret_like(path: Path) -> bool:
    for part in _path_segments(path):
        if part in _SECRET_EXACT_NAMES:
            return True
        suffix = Path(part).suffix.casefold()
        if suffix in _SECRET_SUFFIXES:
            return True
        if _SECRET_SEGMENT_RE.search(part):
            return True
    return False


def _transcript_or_runtime_log_like(path: Path) -> bool:
    for part in _path_segments(path):
        if part in _TRANSCRIPT_EXACT_NAMES or part in _IDE_HISTORY_SEGMENTS:
            return True
        suffix = Path(part).suffix.casefold()
        if suffix in _TRANSCRIPT_SUFFIXES:
            return True
        if _TRANSCRIPT_SEGMENT_RE.search(part):
            return True
    return False


def _release_artifact_like(path: Path) -> bool:
    parts = _path_segments(path)
    if any(part in _RELEASE_ARTIFACT_SEGMENTS for part in parts):
        return True
    name = path.name.casefold()
    return name.endswith(_RELEASE_ARTIFACT_COMPOUND_SUFFIXES) or any(
        name.endswith(suffix) for suffix in _RELEASE_ARTIFACT_SUFFIXES
    )


def _version_managed_like(path: Path) -> bool:
    parts = _path_segments(path)
    return any(
        current == "docs" and next_part == "versions"
        for current, next_part in zip(parts, parts[1:])
    )


def _project_relative_path(path: Path, project_root: Path) -> Path | None:
    try:
        return path.relative_to(project_root)
    except ValueError:
        return None


def _managed_project_path_category(
    *,
    resolved_path: Path,
    project_root: Path,
    storage_root: Path,
) -> tuple[str, str] | None:
    storage_root_resolved = _safe_resolve_existing(storage_root) or storage_root.resolve(strict=False)
    if _is_relative_to(resolved_path, storage_root_resolved):
        return "source_path_managed_path", "managed_sidecar"

    rel_path = _project_relative_path(resolved_path, project_root)
    if rel_path is None:
        return None
    parts = rel_path.parts
    if parts and parts[0] in {".recallloom", "recallloom"}:
        return "source_path_managed_path", "managed_sidecar"
    if len(parts) >= 3 and parts[0] == "docs" and parts[1] == "versions":
        return "source_path_version_managed", "version_managed"
    return None


def _read_scratch_marker(marker_path: Path) -> dict[str, object] | None:
    try:
        marker_stat = marker_path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        return None
    if marker_stat.st_size > MAX_SCRATCH_MARKER_BYTES:
        return None
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _scratch_marker_authorizes(
    payload: dict[str, object],
    *,
    input_role: str,
    env: dict[str, str],
) -> bool:
    required_token = env.get(HELPER_SCRATCH_TOKEN_ENV)
    if not isinstance(required_token, str) or len(required_token) < 16:
        return False
    if payload.get("kind") != HELPER_SCRATCH_MARKER_KIND:
        return False
    if payload.get("owner") != HELPER_SCRATCH_OWNER:
        return False
    token = payload.get("token")
    if not isinstance(token, str) or len(token) < 16:
        return False
    if token != required_token:
        return False
    allowed_roles = payload.get("allowed_roles")
    if allowed_roles is not None:
        if not isinstance(allowed_roles, list) or not all(
            isinstance(item, str) for item in allowed_roles
        ):
            return False
        if input_role not in allowed_roles:
            return False
    return True


def _helper_scratch_root(
    *,
    resolved_path: Path,
    input_role: str,
    env: dict[str, str],
) -> Path | None:
    if resolved_path.name == HELPER_SCRATCH_MARKER_FILENAME:
        return None
    candidate_dir = resolved_path.parent
    marker_path = candidate_dir / HELPER_SCRATCH_MARKER_FILENAME
    payload = _read_scratch_marker(marker_path)
    if payload is not None and _scratch_marker_authorizes(
        payload,
        input_role=input_role,
        env=env,
    ):
        return candidate_dir
    return None


def _forbidden_scratch_root_category(
    *,
    scratch_root: Path,
    project_root: Path,
    storage_root: Path,
) -> tuple[str, str] | None:
    storage_root_resolved = _safe_resolve_existing(storage_root) or storage_root.resolve(strict=False)
    if _is_relative_to(scratch_root, storage_root_resolved):
        return "source_path_managed_path", "managed_sidecar"
    if _is_relative_to(scratch_root, project_root):
        return "source_path_project_file", "project_root_file"
    if _version_managed_like(scratch_root):
        return "source_path_version_managed", "version_managed"
    if _release_artifact_like(scratch_root):
        return "source_path_release_artifact", "release_artifact"
    return None


def validate_prepared_input_source_path(
    raw_path: str | Path,
    *,
    project_root: Path,
    storage_root: Path,
    input_role: str,
    label: str,
    env: dict[str, str] | None = None,
) -> PreparedInputSource:
    env = env or os.environ
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    ref_seed = _safe_path_for_ref(candidate)
    path_ref = _path_ref(ref_seed)

    try:
        source_lstat = candidate.lstat()
    except FileNotFoundError:
        raise _fail(
            reason_code="source_path_missing",
            path_category="missing",
            path_ref=path_ref,
            label=label,
        )
    except OSError as exc:
        raise _fail(
            reason_code="source_path_inspect_failed",
            path_category="unknown",
            path_ref=path_ref,
            label=label,
            extra={"errno": exc.errno if exc.errno is not None else errno.EIO},
        )

    if stat.S_ISLNK(source_lstat.st_mode):
        raise _fail(
            reason_code="source_path_symlink",
            path_category="symlink",
            path_ref=path_ref,
            label=label,
        )
    if not stat.S_ISREG(source_lstat.st_mode):
        raise _fail(
            reason_code="source_path_not_regular_file",
            path_category="not_regular_file",
            path_ref=path_ref,
            label=label,
        )
    if source_lstat.st_nlink != 1:
        raise _fail(
            reason_code="source_path_hardlink",
            path_category="hardlink",
            path_ref=path_ref,
            label=label,
        )

    try:
        resolved_path = candidate.resolve(strict=True)
    except OSError as exc:
        raise _fail(
            reason_code="source_path_realpath_failed",
            path_category="unknown",
            path_ref=path_ref,
            label=label,
            extra={"errno": exc.errno if exc.errno is not None else errno.EIO},
        )
    path_ref = _path_ref(resolved_path)

    if resolved_path.name == HELPER_SCRATCH_MARKER_FILENAME:
        raise _fail(
            reason_code="source_path_scratch_marker",
            path_category="helper_scratch_marker",
            path_ref=path_ref,
            label=label,
        )

    if _secret_like(resolved_path):
        raise _fail(
            reason_code="source_path_secret_like",
            path_category="secret_like",
            path_ref=path_ref,
            label=label,
        )
    if _transcript_or_runtime_log_like(resolved_path):
        raise _fail(
            reason_code="source_path_transcript_like",
            path_category="transcript_or_runtime_log",
            path_ref=path_ref,
            label=label,
        )

    project_root_resolved = project_root.resolve(strict=True)
    managed_category = _managed_project_path_category(
        resolved_path=resolved_path,
        project_root=project_root_resolved,
        storage_root=storage_root,
    )
    if managed_category is not None:
        reason_code, path_category = managed_category
        raise _fail(
            reason_code=reason_code,
            path_category=path_category,
            path_ref=path_ref,
            label=label,
        )
    if _release_artifact_like(resolved_path):
        raise _fail(
            reason_code="source_path_release_artifact",
            path_category="release_artifact",
            path_ref=path_ref,
            label=label,
        )
    if _is_relative_to(resolved_path, project_root_resolved):
        raise _fail(
            reason_code="source_path_project_file",
            path_category="project_root_file",
            path_ref=path_ref,
            label=label,
        )

    scratch_root = _helper_scratch_root(
        resolved_path=resolved_path,
        input_role=input_role,
        env=env,
    )
    if scratch_root is not None:
        forbidden_scratch_root = _forbidden_scratch_root_category(
            scratch_root=scratch_root,
            project_root=project_root_resolved,
            storage_root=storage_root,
        )
        if forbidden_scratch_root is not None:
            reason_code, path_category = forbidden_scratch_root
            raise _fail(
                reason_code=reason_code,
                path_category=path_category,
                path_ref=path_ref,
                label=label,
            )
        return PreparedInputSource(
            path=resolved_path,
            path_ref=path_ref,
            path_category="helper_scratch",
            st_dev=source_lstat.st_dev,
            st_ino=source_lstat.st_ino,
            st_mode=source_lstat.st_mode,
            st_nlink=source_lstat.st_nlink,
            st_size=source_lstat.st_size,
            scratch_root_ref=_path_ref(scratch_root),
        )

    raise _fail(
        reason_code="source_path_external",
        path_category="external",
        path_ref=path_ref,
        label=label,
    )


def read_prepared_input_source_text(
    source: PreparedInputSource,
    *,
    max_input_bytes: int,
    label: str,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(source.path, flags)
    except OSError as exc:
        reason_code = (
            "source_path_symlink" if exc.errno == errno.ELOOP else "source_path_open_failed"
        )
        raise _fail(
            reason_code=reason_code,
            path_category=source.path_category,
            path_ref=source.path_ref,
            label=label,
            extra={"errno": exc.errno if exc.errno is not None else errno.EIO},
        )
    try:
        with os.fdopen(fd, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise _fail(
                    reason_code="source_path_not_regular_file",
                    path_category="not_regular_file",
                    path_ref=source.path_ref,
                    label=label,
                )
            if file_stat.st_dev != source.st_dev or file_stat.st_ino != source.st_ino:
                raise _fail(
                    reason_code="source_path_changed_after_validation",
                    path_category=source.path_category,
                    path_ref=source.path_ref,
                    label=label,
                )
            if (
                file_stat.st_mode != source.st_mode
                or file_stat.st_nlink != source.st_nlink
                or file_stat.st_size != source.st_size
            ):
                raise _fail(
                    reason_code="source_path_changed_after_validation",
                    path_category=source.path_category,
                    path_ref=source.path_ref,
                    label=label,
                )
            if file_stat.st_size > max_input_bytes:
                raise _fail(
                    reason_code="source_path_size_limit",
                    path_category=source.path_category,
                    path_ref=source.path_ref,
                    label=label,
                    extra={"size": file_stat.st_size, "max_input_bytes": max_input_bytes},
                )
            raw = handle.read(max_input_bytes + 1)
    except PreparedInputSafetyError:
        raise
    except OSError as exc:
        raise _fail(
            reason_code="source_path_read_failed",
            path_category=source.path_category,
            path_ref=source.path_ref,
            label=label,
            extra={"errno": exc.errno if exc.errno is not None else errno.EIO},
        )
    if len(raw) > max_input_bytes:
        raise _fail(
            reason_code="source_path_size_limit",
            path_category=source.path_category,
            path_ref=source.path_ref,
            label=label,
            extra={"size": len(raw), "max_input_bytes": max_input_bytes},
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail(
            reason_code="source_path_not_utf8",
            path_category=source.path_category,
            path_ref=source.path_ref,
            label=label,
        )
