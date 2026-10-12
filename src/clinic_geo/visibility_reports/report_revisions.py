"""Immutable report bundles; publication approval remains in its existing store.

These are derived reports, never recovery inputs. A commit names every complete
artifact by digest. Uncommitted files cannot be mistaken for a complete report.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from clinic_geo.execution.worker_registry import publish_json, read_record


class ReportRevisionError(ValueError):
    """An existing report bundle is corrupt, incomplete or differently bound."""


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_report_revision(directory: Path, *, results_hash: str) -> dict[str, Path]:
    commit = read_record(directory / "commit.json")
    if commit.get("schema_version") != "visibility-report-revision-v1" or commit.get("results_hash") != results_hash:
        raise ReportRevisionError("report revision identity mismatch")
    artifacts = commit.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"pdf", "html", "data", "validation"}:
        raise ReportRevisionError("report revision artifact set invalid")
    paths = {}
    for kind, ref in artifacts.items():
        if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
            raise ReportRevisionError("report revision artifact invalid")
        path = directory / ref["name"]
        if path.parent != directory or path.is_symlink() or not path.is_file():
            raise ReportRevisionError("report revision artifact unavailable")
        if _hash(path) != ref.get("sha256") or path.stat().st_size != ref.get("byte_length"):
            raise ReportRevisionError("report revision artifact digest mismatch")
        paths[kind] = path
    return paths


def render_report_revision(
    directory: Path,
    *,
    results_hash: str,
    payload: Mapping[str, object],
    renderer: Callable,
    chromium_executable: object = None,
) -> dict[str, Path]:
    """Stage and commit a report exactly once; retry can reuse only exact bytes."""
    if not re.fullmatch(r"[a-f0-9]{64}", results_hash):
        raise ReportRevisionError("invalid report revision hash")
    directory = Path(directory)
    if directory.is_symlink():
        raise ReportRevisionError("report revision directory is a symlink")
    if (directory / "commit.json").exists():
        return read_report_revision(directory, results_hash=results_hash)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".report-stage-", dir=directory.parent) as name:
        staged = Path(name)
        rendered = renderer(payload, output_dir=staged, internal_candidate=True, chromium_executable=chromium_executable)
        if not isinstance(rendered, Mapping) or set(rendered) != {"pdf", "html", "data", "validation"}:
            raise ReportRevisionError("report renderer omitted required artifacts")
        artifacts = {}
        for kind, filename in rendered.items():
            path = Path(filename).resolve()
            if path.parent != staged.resolve() or path.is_symlink() or not path.is_file():
                raise ReportRevisionError("report renderer returned an invalid artifact")
            artifacts[kind] = {"name": path.name, "sha256": _hash(path), "byte_length": path.stat().st_size}
            # Make all files durable before publishing any manifest. Existing
            # bytes from an interrupted publication can only be reused exactly.
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            destination = directory / path.name
            try:
                os.link(path, destination)
            except FileExistsError:
                if destination.is_symlink() or _hash(destination) != artifacts[kind]["sha256"]:
                    raise ReportRevisionError("uncommitted report revision conflicts") from None
        commit = {
            "schema_version": "visibility-report-revision-v1",
            "report_id": payload.get("report_id"),
            "results_hash": results_hash,
            "collection_period": dict(payload.get("presence_report", {}).get("collection_period", {})),
            "artifacts": artifacts,
        }
        try:
            publish_json(directory / "commit.json", commit)
        except FileExistsError:
            # Concurrent identical render: only the committed winner counts.
            return read_report_revision(directory, results_hash=results_hash)
    return read_report_revision(directory, results_hash=results_hash)
