"""Deterministic source provenance for an RN sealed run.

The resolved manifest freezes scientific inputs.  This module separately
freezes the *implementation* that is allowed to consume them.  It deliberately
hashes only the RN execution surface and its direct launcher rather than a
mutable ``latest`` directory or every generated file in the repository.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


class RNSourceProvenanceError(RuntimeError):
    """The sealed source snapshot is absent, malformed, or has drifted."""


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOTS = (
    "config.py",
    "twinmarket_kr",
    "scripts/09_run_realnews_community_ab.py",
    "scripts/12_operate_realnews_community_ab.py",
    "validation/validate_realnews_community_ab.py",
)
_DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile.lock")
_SKIP_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})


def build_source_snapshot(*, baseline_commit: str, checked_out_commit: str) -> dict[str, Any]:
    """Return the exact source/dependency state for a newly sealed run.

    A dirty worktree is recorded rather than silently treated as the named
    Git commit.  The digest of actual bytes is what execution later checks.
    """

    if not baseline_commit or not checked_out_commit:
        raise RNSourceProvenanceError("Both baseline and checked-out commit are required")
    files = _source_file_records(_PROJECT_ROOT)
    dependencies = _dependency_records(_PROJECT_ROOT)
    payload = {
        "artifact_type": "rn_source_hashes",
        "version": "rn-source-hashes-v1",
        "baseline_commit": baseline_commit,
        "checked_out_commit": checked_out_commit,
        "source_roots": list(_SOURCE_ROOTS),
        "source_files": files,
        "source_tree_sha256": _sha256({"source_files": files}),
        "dependency_files": dependencies,
        "dependency_tree_sha256": _sha256({"dependency_files": dependencies}),
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
        },
        "git": _git_metadata(_PROJECT_ROOT),
    }
    payload["snapshot_sha256"] = _sha256(payload)
    return payload


def validate_source_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact schema and self-hash without reading the current tree."""

    if not isinstance(value, Mapping):
        raise RNSourceProvenanceError("source_hashes.json must be an object")
    expected = {
        "artifact_type",
        "version",
        "baseline_commit",
        "checked_out_commit",
        "source_roots",
        "source_files",
        "source_tree_sha256",
        "dependency_files",
        "dependency_tree_sha256",
        "runtime",
        "git",
        "snapshot_sha256",
    }
    if set(value) != expected:
        raise RNSourceProvenanceError("source_hashes.json has an invalid exact key set")
    if value["artifact_type"] != "rn_source_hashes" or value["version"] != "rn-source-hashes-v1":
        raise RNSourceProvenanceError("source_hashes.json has an unsupported artifact/version")
    copied = dict(value)
    claimed = _sha_text(copied.pop("snapshot_sha256"), "source snapshot hash")
    if _sha256(copied) != claimed:
        raise RNSourceProvenanceError("source_hashes.json self-hash differs")
    source_files = _validate_file_records(value["source_files"], "source_files")
    dependency_files = _validate_file_records(value["dependency_files"], "dependency_files")
    if _sha256({"source_files": source_files}) != _sha_text(
        value["source_tree_sha256"], "source tree hash"
    ):
        raise RNSourceProvenanceError("source_hashes.json source tree hash differs")
    if _sha256({"dependency_files": dependency_files}) != _sha_text(
        value["dependency_tree_sha256"], "dependency tree hash"
    ):
        raise RNSourceProvenanceError("source_hashes.json dependency tree hash differs")
    return dict(value)


def assert_current_source_matches(snapshot: Mapping[str, Any]) -> None:
    """Fail before execution if the RN implementation differs from the seal."""

    validated = validate_source_snapshot(snapshot)
    expected_files = _validate_file_records(validated["source_files"], "source_files")
    actual_files = _source_file_records(_PROJECT_ROOT)
    if actual_files != expected_files:
        raise RNSourceProvenanceError(
            "RN execution source tree differs from source_hashes.json; create a new sealed run"
        )
    expected_dependencies = _validate_file_records(validated["dependency_files"], "dependency_files")
    actual_dependencies = _dependency_records(_PROJECT_ROOT)
    if actual_dependencies != expected_dependencies:
        raise RNSourceProvenanceError(
            "RN dependency-lock state differs from source_hashes.json; create a new sealed run"
        )


def _source_file_records(project_root: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for relative in _SOURCE_ROOTS:
        path = project_root / relative
        if not path.exists():
            raise RNSourceProvenanceError(f"RN source path is missing: {relative}")
        if path.is_file():
            records.append(_file_record(project_root, path))
            continue
        for candidate in sorted(path.rglob("*")):
            if any(part in _SKIP_NAMES for part in candidate.parts) or not candidate.is_file():
                continue
            records.append(_file_record(project_root, candidate))
    if not records:
        raise RNSourceProvenanceError("RN source tree is unexpectedly empty")
    return sorted(records, key=lambda item: item["path"])


def _dependency_records(project_root: Path) -> list[dict[str, str]]:
    return [
        _file_record(project_root, project_root / relative)
        for relative in _DEPENDENCY_FILES
        if (project_root / relative).is_file()
    ]


def _file_record(project_root: Path, path: Path) -> dict[str, str]:
    try:
        relative = path.relative_to(project_root).as_posix()
    except ValueError as exc:  # pragma: no cover - defensive against a bad root list.
        raise RNSourceProvenanceError("RN source path escaped project root") from exc
    if path.is_symlink():
        raise RNSourceProvenanceError(f"RN source path may not be a symlink: {relative}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": relative, "sha256": digest}


def _validate_file_records(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise RNSourceProvenanceError(f"{label} must be a non-empty list")
    records: list[dict[str, str]] = []
    previous = ""
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
            raise RNSourceProvenanceError(f"{label} contains an invalid file record")
        path = raw["path"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or path <= previous
        ):
            raise RNSourceProvenanceError(f"{label} paths must be unique sorted safe relative paths")
        records.append({"path": path, "sha256": _sha_text(raw["sha256"], f"{label} hash")})
        previous = path
    return records


def _git_metadata(project_root: Path) -> dict[str, Any]:
    """Best-effort Git context; byte hashes remain authoritative when Git is absent."""

    try:
        status = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain=v1", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {"available": False, "tracked_dirty_paths": []}
    return {"available": True, "tracked_dirty_paths": sorted(status)}


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise RNSourceProvenanceError(f"{label} must be a lowercase SHA-256")
    return value


__all__ = [
    "RNSourceProvenanceError",
    "assert_current_source_matches",
    "build_source_snapshot",
    "validate_source_snapshot",
]
