"""Startup scanner for helper-owned prepared-input scratch residue."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import stat
from typing import Iterable

from core.safety.prepared_input import (
    HELPER_SCRATCH_MARKER_FILENAME,
    HELPER_SCRATCH_MARKER_KIND,
    HELPER_SCRATCH_OWNER,
    MAX_SCRATCH_MARKER_BYTES,
    _is_relative_to,
    _path_ref,
    _release_artifact_like,
    _safe_path_for_ref,
    _version_managed_like,
)


DEFAULT_PROJECT_SCAN_DEPTH = 4
DEFAULT_STORAGE_SCAN_DEPTH = 6
DEFAULT_EXTERNAL_SCAN_DEPTH = 1
MAX_RESIDUE_COUNT = 100

_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "site-packages",
    "venv",
}


@dataclass(frozen=True)
class ScratchResidueFinding:
    """Public-safe scratch residue finding."""

    reason_code: str
    path_category: str
    path_ref: str
    scope: str
    residue_count: int
    blocking: bool
    side_effect: str = "none"

    def public_dict(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "path_category": self.path_category,
            "path_ref": self.path_ref,
            "scope": self.scope,
            "residue_count": self.residue_count,
            "side_effect": self.side_effect,
        }


@dataclass(frozen=True)
class ScratchResidueReport:
    """Scanner result split into blocking and report-only findings."""

    blocking_findings: tuple[ScratchResidueFinding, ...]
    report_only_findings: tuple[ScratchResidueFinding, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_findings)

    def public_dict(self) -> dict[str, object]:
        findings = [finding.public_dict() for finding in self.blocking_findings]
        findings.extend(finding.public_dict() for finding in self.report_only_findings)
        return {
            "blocked": self.blocked,
            "side_effect": "none",
            "blocking_count": len(self.blocking_findings),
            "report_only_count": len(self.report_only_findings),
            "residue_count": sum(finding.residue_count for finding in findings_from_report(self)),
            "findings": findings,
        }

    def report_only_public_dict(self) -> dict[str, object] | None:
        if not self.report_only_findings:
            return None
        return {
            "blocked": False,
            "side_effect": "none",
            "report_only_count": len(self.report_only_findings),
            "residue_count": sum(
                finding.residue_count for finding in self.report_only_findings
            ),
            "findings": [finding.public_dict() for finding in self.report_only_findings],
        }


@dataclass(frozen=True)
class _ScanArea:
    root: Path
    max_depth: int
    external: bool = False


def findings_from_report(report: ScratchResidueReport) -> tuple[ScratchResidueFinding, ...]:
    return (*report.blocking_findings, *report.report_only_findings)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _path_ref_for(path: Path) -> str:
    return _path_ref(_safe_path_for_ref(path))


def _same_path(left: Path, right: Path) -> bool:
    return _safe_resolve(left) == _safe_resolve(right)


def _raw_scan_key(path: Path) -> str:
    return path.expanduser().absolute().as_posix()


def _dedupe_scan_areas(areas: Iterable[_ScanArea]) -> list[_ScanArea]:
    deduped: list[_ScanArea] = []
    seen: set[tuple[str, int, bool]] = set()
    for area in areas:
        key = (_raw_scan_key(area.root), area.max_depth, area.external)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(area)
    return deduped


def _scan_areas(
    *,
    project_root: Path,
    storage_root: Path,
    external_roots: Iterable[str | Path] | None,
) -> list[_ScanArea]:
    areas = [
        _ScanArea(project_root, DEFAULT_PROJECT_SCAN_DEPTH),
        _ScanArea(storage_root, DEFAULT_STORAGE_SCAN_DEPTH),
    ]
    versions_root = project_root / "docs" / "versions"
    if versions_root.is_dir() or versions_root.is_symlink():
        areas.append(_ScanArea(versions_root, DEFAULT_STORAGE_SCAN_DEPTH))
    for name in ("artifact", "artifacts", "dist", "release", "releases"):
        candidate = project_root / name
        if candidate.is_dir() or candidate.is_symlink():
            areas.append(_ScanArea(candidate, DEFAULT_STORAGE_SCAN_DEPTH))
    for raw_root in external_roots or ():
        if raw_root is None:
            continue
        areas.append(_ScanArea(Path(raw_root).expanduser(), DEFAULT_EXTERNAL_SCAN_DEPTH, True))
    return _dedupe_scan_areas(areas)


def _iter_scan_area_entries(
    area: _ScanArea,
    *,
    explicit_root_keys: set[str],
) -> Iterable[tuple[str, Path]]:
    root = area.root
    try:
        root_stat = root.lstat()
    except OSError:
        return
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for child in children:
            if child.name == HELPER_SCRATCH_MARKER_FILENAME:
                yield "marker", child
                continue
            if depth >= area.max_depth:
                continue
            if child.name in _EXCLUDED_DIR_NAMES:
                continue
            try:
                child_stat = child.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(child_stat.st_mode):
                if _raw_scan_key(child) in explicit_root_keys:
                    continue
                yield "scratch_scan_child_symlink", child
                continue
            if stat.S_ISDIR(child_stat.st_mode):
                stack.append((child, depth + 1))


def _scan_area_path_category(
    area: _ScanArea,
    *,
    project_root: Path,
    storage_root: Path,
) -> str:
    return _scan_path_category(
        area.root,
        external=area.external,
        project_root=project_root,
        storage_root=storage_root,
    )


def _scan_path_category(
    path: Path,
    *,
    external: bool,
    project_root: Path,
    storage_root: Path,
) -> str:
    root = path.expanduser()
    if not root.is_absolute():
        root = root.absolute()
    project = _safe_resolve(project_root)
    storage = _safe_resolve(storage_root)
    if external:
        return "external_helper_scratch"
    if _is_relative_to(root, storage):
        return "managed_sidecar"
    if _is_relative_to(root, project):
        try:
            rel_parts = root.relative_to(project).parts
        except ValueError:
            rel_parts = ()
        if rel_parts and rel_parts[0] in {".recallloom", "recallloom"}:
            return "managed_sidecar"
        if len(rel_parts) >= 2 and rel_parts[0] == "docs" and rel_parts[1] == "versions":
            return "version_managed"
        if any(
            part.casefold() in {"artifact", "artifacts", "dist", "release", "releases"}
            for part in rel_parts
        ):
            return "release_artifact"
        return "project_root"
    if _version_managed_like(root):
        return "version_managed"
    if _release_artifact_like(root):
        return "release_artifact"
    return "project_root"


def _internal_path_category(
    root: Path,
    *,
    project_root: Path,
    storage_root: Path,
) -> str | None:
    if _is_relative_to(root, storage_root):
        return "managed_sidecar"
    if not _is_relative_to(root, project_root):
        return None
    try:
        rel_parts = root.relative_to(project_root).parts
    except ValueError:
        rel_parts = ()
    if rel_parts and rel_parts[0] in {".recallloom", "recallloom"}:
        return "managed_sidecar"
    if len(rel_parts) >= 2 and rel_parts[0] == "docs" and rel_parts[1] == "versions":
        return "version_managed"
    if any(
        part.casefold() in {"artifact", "artifacts", "dist", "release", "releases"}
        for part in rel_parts
    ):
        return "release_artifact"
    return "project_root"


def _marker_path_category(
    scratch_root: Path,
    *,
    external: bool,
    project_root: Path,
    storage_root: Path,
) -> str:
    root = scratch_root.expanduser()
    if not root.is_absolute():
        root = root.absolute()
    resolved_root = _safe_resolve(root)
    project = _safe_resolve(project_root)
    storage = _safe_resolve(storage_root)
    internal_category = _internal_path_category(
        resolved_root,
        project_root=project,
        storage_root=storage,
    )
    if internal_category is not None:
        return internal_category
    if external:
        return "external_helper_scratch"
    if _version_managed_like(root):
        return "version_managed"
    if _release_artifact_like(root):
        return "release_artifact"
    return "external_helper_scratch"


def _blocking_for(path_category: str, *, suspicious: bool) -> bool:
    if path_category == "external_helper_scratch":
        return False
    if suspicious:
        return True
    return path_category in {
        "managed_sidecar",
        "project_root",
        "release_artifact",
        "version_managed",
    }


def _scope_for(*, blocking: bool) -> str:
    return "blocking" if blocking else "report_only"


def _direct_residue_count(scratch_root: Path) -> int:
    count = 1
    try:
        children = list(scratch_root.iterdir())
    except OSError:
        return count
    for child in children:
        if child.name == HELPER_SCRATCH_MARKER_FILENAME:
            continue
        count += 1
        if count >= MAX_RESIDUE_COUNT:
            return count
    return count


def _marker_payload(marker_path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        marker_stat = marker_path.lstat()
    except OSError:
        return None, "scratch_marker_inspect_failed"
    if stat.S_ISLNK(marker_stat.st_mode):
        return None, "scratch_marker_symlink"
    if not stat.S_ISREG(marker_stat.st_mode):
        return None, "scratch_marker_not_regular_file"
    if marker_stat.st_size > MAX_SCRATCH_MARKER_BYTES:
        return None, "scratch_marker_too_large"
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "scratch_marker_unreadable"
    if not isinstance(payload, dict):
        return None, "scratch_marker_unreadable"
    return payload, None


def _scan_area_root_finding(
    area: _ScanArea,
    *,
    project_root: Path,
    storage_root: Path,
) -> ScratchResidueFinding | None:
    root = area.root
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        reason_code = "scratch_scan_area_root_inspect_failed"
    else:
        if stat.S_ISLNK(root_stat.st_mode):
            reason_code = "scratch_scan_area_root_symlink"
        elif not stat.S_ISDIR(root_stat.st_mode):
            reason_code = "scratch_scan_area_root_not_directory"
        else:
            return None

    path_category = _scan_area_path_category(
        area,
        project_root=project_root,
        storage_root=storage_root,
    )
    blocking = path_category != "external_helper_scratch"
    return ScratchResidueFinding(
        reason_code=reason_code,
        path_category=path_category,
        path_ref=_path_ref_for(root),
        scope=_scope_for(blocking=blocking),
        residue_count=1,
        blocking=blocking,
    )


def _suspicious_scan_path_finding(
    path: Path,
    *,
    reason_code: str,
    area: _ScanArea,
    project_root: Path,
    storage_root: Path,
) -> ScratchResidueFinding:
    path_category = _scan_path_category(
        path,
        external=area.external,
        project_root=project_root,
        storage_root=storage_root,
    )
    blocking = _blocking_for(path_category, suspicious=True)
    return ScratchResidueFinding(
        reason_code=reason_code,
        path_category=path_category,
        path_ref=_path_ref_for(path),
        scope=_scope_for(blocking=blocking),
        residue_count=1,
        blocking=blocking,
    )


def _is_helper_owned_payload(payload: dict[str, object]) -> bool:
    token = payload.get("token")
    return (
        payload.get("kind") == HELPER_SCRATCH_MARKER_KIND
        and payload.get("owner") == HELPER_SCRATCH_OWNER
        and isinstance(token, str)
        and len(token) >= 16
    )


def _finding_for_marker(
    marker_path: Path,
    *,
    external: bool,
    project_root: Path,
    storage_root: Path,
) -> ScratchResidueFinding | None:
    scratch_root = marker_path.parent
    path_category = _marker_path_category(
        scratch_root,
        external=external,
        project_root=project_root,
        storage_root=storage_root,
    )
    path_ref = _path_ref_for(scratch_root)

    payload, marker_error = _marker_payload(marker_path)
    if marker_error is not None:
        blocking = _blocking_for(path_category, suspicious=True)
        return ScratchResidueFinding(
            reason_code=marker_error,
            path_category=path_category,
            path_ref=path_ref,
            scope=_scope_for(blocking=blocking),
            residue_count=1,
            blocking=blocking,
        )

    marker_realpath = _safe_resolve(marker_path)
    if not _same_path(marker_path, marker_realpath):
        blocking = _blocking_for(path_category, suspicious=True)
        return ScratchResidueFinding(
            reason_code="scratch_marker_realpath_mismatch",
            path_category=path_category,
            path_ref=path_ref,
            scope=_scope_for(blocking=blocking),
            residue_count=1,
            blocking=blocking,
        )

    if payload is None or not _is_helper_owned_payload(payload):
        blocking = _blocking_for(path_category, suspicious=True)
        return ScratchResidueFinding(
            reason_code="scratch_marker_untrusted_payload",
            path_category=path_category,
            path_ref=path_ref,
            scope=_scope_for(blocking=blocking),
            residue_count=1,
            blocking=blocking,
        )

    residue_count = _direct_residue_count(scratch_root)
    blocking = _blocking_for(path_category, suspicious=False)
    return ScratchResidueFinding(
        reason_code="helper_scratch_residue",
        path_category=path_category,
        path_ref=path_ref,
        scope=_scope_for(blocking=blocking),
        residue_count=residue_count,
        blocking=blocking,
    )


def scan_startup_scratch_residue(
    *,
    project_root: str | Path,
    storage_root: str | Path,
    external_roots: Iterable[str | Path] | None = None,
) -> ScratchResidueReport:
    """Scan bounded startup surfaces for helper-owned scratch residue."""

    project = _safe_resolve(Path(project_root).expanduser())
    storage = _safe_resolve(Path(storage_root).expanduser())
    blocking: list[ScratchResidueFinding] = []
    report_only: list[ScratchResidueFinding] = []
    seen_markers: set[str] = set()
    scan_areas = _scan_areas(
        project_root=project,
        storage_root=storage,
        external_roots=external_roots,
    )
    explicit_root_keys = {_raw_scan_key(area.root) for area in scan_areas}
    for area in scan_areas:
        root_finding = _scan_area_root_finding(
            area,
            project_root=project,
            storage_root=storage,
        )
        if root_finding is not None:
            if root_finding.blocking:
                blocking.append(root_finding)
            else:
                report_only.append(root_finding)
            continue
        for entry_kind, entry_path in _iter_scan_area_entries(
            area,
            explicit_root_keys=explicit_root_keys,
        ):
            if entry_kind != "marker":
                finding = _suspicious_scan_path_finding(
                    entry_path,
                    reason_code=entry_kind,
                    area=area,
                    project_root=project,
                    storage_root=storage,
                )
                if finding.blocking:
                    blocking.append(finding)
                else:
                    report_only.append(finding)
                continue
            marker_path = entry_path
            marker_key = _safe_resolve(marker_path).as_posix()
            if marker_key in seen_markers:
                continue
            seen_markers.add(marker_key)
            finding = _finding_for_marker(
                marker_path,
                external=area.external,
                project_root=project,
                storage_root=storage,
            )
            if finding is None:
                continue
            if finding.blocking:
                blocking.append(finding)
            else:
                report_only.append(finding)
    return ScratchResidueReport(
        blocking_findings=tuple(blocking),
        report_only_findings=tuple(report_only),
    )


def external_scratch_roots_for_sources(*paths: str | Path | None) -> list[Path]:
    """Return candidate external scratch roots adjacent to prepared source files."""

    roots: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        if raw_path is None:
            continue
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        root = candidate.parent
        key = _safe_resolve(root).as_posix()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots
