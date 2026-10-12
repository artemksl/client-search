"""Runtime discovery for the standalone paid Diagnostic Audit application.

The Node application under :mod:`audit-automation` is a separate executable
boundary.  This module owns the Python-side knowledge required to invoke that
boundary: where its CLI and data directory live, which Node major it requires,
and which optional Chromium executable should be exposed to it.

No web/UI module is imported here.  The paid-audit application can therefore
be used from a CLI, worker, or test without depending on the control plane.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from clinic_geo.paths import root_or_default


PAID_AUDIT_ENGINE_DIRNAME = "audit-automation"
PAID_AUDIT_DATA_DIRNAME = "paid-audit"
MIN_NODE_MAJOR = 22
NODE_BIN_ENV_VARS = ("CLINIC_GEO_NODE_BIN", "NODE_BIN")
CHROME_PATH_ENV_VARS = (
    "CHROME_PATH",
    "CLINIC_GEO_CHROME_PATH",
    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
)
_CHROME_EXECUTABLE_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


def paid_audit_engine_dir(root: Path | None = None) -> Path:
    """Return the standalone Node application directory."""

    return root_or_default(root) / PAID_AUDIT_ENGINE_DIRNAME


def paid_audit_cli_path(root: Path | None = None) -> Path:
    """Return the CLI entry point used by the Python adapter."""

    return paid_audit_engine_dir(root) / "src" / "cli.js"


def paid_audit_data_dir(root: Path | None = None) -> Path:
    """Return the engine ``--data-dir`` used for paid runs."""

    return root_or_default(root) / "data" / PAID_AUDIT_DATA_DIRNAME


def _node_version_file(root: Path | None = None) -> Path:
    return paid_audit_engine_dir(root) / ".node-version"


def required_node_major(root: Path | None = None) -> int:
    """Read the engine's pinned Node major, with a fail-safe default."""

    try:
        text = _node_version_file(root).read_text(encoding="utf-8").strip()
    except OSError:
        return MIN_NODE_MAJOR
    match = re.match(r"v?(\d+)", text)
    return int(match.group(1)) if match else MIN_NODE_MAJOR


def _resolve_executable(value: str) -> Optional[str]:
    """Resolve either an executable file path or a name on ``PATH``."""

    value = str(value or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(value)


def discover_node_bin() -> Optional[str]:
    """Locate Node without assuming a machine-specific installation path."""

    for variable in NODE_BIN_ENV_VARS:
        resolved = _resolve_executable(os.environ.get(variable, ""))
        if resolved:
            return resolved
    return shutil.which("node")


def node_binary_satisfies(
    node_bin: str,
    min_major: int = MIN_NODE_MAJOR,
) -> bool:
    """Return whether ``node_bin --version`` meets the engine contract."""

    if not node_bin:
        return False
    try:
        process = subprocess.run(
            [node_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.match(r"v?(\d+)", (process.stdout or "").strip())
    return bool(match) and int(match.group(1)) >= min_major


def chrome_path_cache_file(root: Path | None = None) -> Path:
    """Return the machine-local Chromium cache path written by bootstrap."""

    return paid_audit_engine_dir(root) / ".chrome-path"


def discover_chrome_path(root: Path | None = None) -> Optional[str]:
    """Best-effort Chromium discovery for the standalone renderer."""

    for variable in CHROME_PATH_ENV_VARS:
        resolved = _resolve_executable(os.environ.get(variable, ""))
        if resolved:
            return resolved
    try:
        cached = chrome_path_cache_file(root).read_text(encoding="utf-8").strip()
    except OSError:
        cached = ""
    resolved = _resolve_executable(cached)
    if resolved:
        return resolved
    for executable_name in _CHROME_EXECUTABLE_NAMES:
        found = shutil.which(executable_name)
        if found:
            return found
    return None


def paid_audit_engine_env(root: Path | None = None) -> dict[str, str]:
    """Return the minimal environment overlay for one engine subprocess."""

    chrome = discover_chrome_path(root)
    return {"CHROME_PATH": chrome} if chrome else {}


__all__ = [
    "CHROME_PATH_ENV_VARS",
    "MIN_NODE_MAJOR",
    "NODE_BIN_ENV_VARS",
    "PAID_AUDIT_DATA_DIRNAME",
    "PAID_AUDIT_ENGINE_DIRNAME",
    "chrome_path_cache_file",
    "discover_chrome_path",
    "discover_node_bin",
    "node_binary_satisfies",
    "paid_audit_cli_path",
    "paid_audit_data_dir",
    "paid_audit_engine_dir",
    "paid_audit_engine_env",
    "required_node_major",
]
