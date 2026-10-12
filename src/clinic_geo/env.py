"""Environment loading and typed environment reads.

Deliberately stdlib-only and free of any Playwright import: `clinic_geo.browser.ai_platforms`
imports Playwright lazily so that the web UI and tests can load it without a browser present,
so the helpers it shares with `chat_engine` cannot live in a module that pulls Playwright in.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from clinic_geo.paths import repo_root


ENV_DIR_NAME = "config"


def env_static_dir(root: Path | None = None) -> Path:
    """Return the directory holding `.env`, `prompts.json`, and the Chrome launchers."""
    return (root or repo_root()) / ENV_DIR_NAME


def dotenv_path(root: Path | None = None) -> Path:
    return env_static_dir(root) / ".env"


def load_env_file(env_path: Path | str | None = None) -> None:
    """Load `KEY=value` pairs into `os.environ` without overriding what is already set.

    Tolerates `export ` prefixes, `#` comments, and single/double-quoted values.
    """
    path = Path(env_path) if env_path is not None else dotenv_path()
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if os.environ.get(key) is None:
            os.environ[key] = value


def env_int(name: str, default: int) -> int:
    """Read an int from env, tolerating an inline comment like `3  # note`."""
    match = re.match(r"\s*(\d+)", os.getenv(name, "") or "")
    return int(match.group(1)) if match else default


def env_float(name: str, default: float) -> float:
    """Read a float from env, tolerating an inline comment like `1.5  # note`."""
    match = re.match(r"\s*([\d.]+)", os.getenv(name, "") or "")
    return float(match.group(1)) if match else default
