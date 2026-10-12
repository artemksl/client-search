"""Profile and run-configuration helpers for the local control UI."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from clinic_geo.paths import root_or_default
from clinic_geo.persistence.json_store import read_json_or_none, write_json


ENV_DIR_NAME = "config"
PROFILE_CONFIG_NAME = "logged-in-profiles.json"
CHROME_PROFILE_RE = re.compile(r"^Profile Path[^\n]*?/Chrome/(.+?)\s*$", re.MULTILINE)
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
SNAPSHOT_ROUTINE_PROFILE_IDS = (
    "ortemksl_personal", "ortemksl_finance", "sokr4r3", "kysil_work",
    "loggedin_1", "loggedin_2", "loggedin_3",
)


def github_copilot_defaults(root: Path | None = None) -> dict | None:
    """Load optional form defaults containing an OS locator, never credentials."""
    from clinic_geo.execution.copilot_settings import runtime_settings, source_settings

    path = root_or_default(root) / "config" / "github-copilot.json"
    if not path.exists():
        return None
    value = read_json_or_none(path)
    source_keys = {"account_ref", "github_account_id", "source_method", "model", "repetitions",
                   "request_policy_version"}
    runtime_keys = {"account_ref", "github_account_id", "credential_ref"}
    try:
        if not isinstance(value, dict) or set(value) != {"schema_version", "source", "runtime"}:
            raise ValueError
        if value["schema_version"] != "github_copilot.setup.v1":
            raise ValueError
        if not isinstance(value["source"], dict) or set(value["source"]) - source_keys:
            raise ValueError
        if not isinstance(value["runtime"], dict) or set(value["runtime"]) - runtime_keys:
            raise ValueError
        # These are defaults for new form selections, never frozen-job inputs.
        source = source_settings({"request_policy_version": "github_copilot.cost_optimized.v1",
                                  **value["source"]})
        runtime = runtime_settings(value["runtime"], source)
    except (TypeError, ValueError):
        # Do not echo malformed configuration, unknown keys or supplied values.
        raise ValueError("GitHub Copilot setup config is invalid; use account settings and a Keychain reference only") from None
    return {**{key: source[key] for key in source_keys}, "credential_ref": runtime["credential_ref"]}


def snapshot_profile_rows(root: Path | None = None) -> List[dict]:
    """Load the complete routine catalog without consulting discovery aliases."""
    path = root_or_default(root) / "config" / "routine task profiles" / PROFILE_CONFIG_NAME
    data = read_json_or_none(path)
    if not isinstance(data, list):
        raise ValueError("Snapshot routine profile catalog is unavailable")
    rows = {}
    directories = set()
    for item in data:
        row = _canonical_profile_row(item) if isinstance(item, dict) else None
        if row is None or row["id"] not in SNAPSHOT_ROUTINE_PROFILE_IDS:
            raise ValueError("Snapshot routine profile catalog contains an invalid profile")
        if row["id"] in rows or row["chrome_directory"] in directories:
            raise ValueError("Snapshot routine profile bindings must be unique")
        rows[row["id"]] = row
        directories.add(row["chrome_directory"])
    if set(rows) != set(SNAPSHOT_ROUTINE_PROFILE_IDS):
        raise ValueError("Snapshot requires all seven routine profile bindings in its catalog")
    configured = {row["id"]: row for row in load_logged_in_profiles(root)}
    for profile_id, row in rows.items():
        if profile_id in configured and configured[profile_id]["chrome_directory"] != row["chrome_directory"]:
            raise ValueError(f"Snapshot routine Chrome directory mismatch for {profile_id}")
    return [rows[profile_id] for profile_id in SNAPSHOT_ROUTINE_PROFILE_IDS]


def selected_snapshot_profile_rows(profile_ids: Iterable[str], root: Path | None = None) -> List[dict]:
    """Require explicit, exact routine IDs; never expand an empty selection."""
    if isinstance(profile_ids, (str, bytes)):
        raise ValueError("Select explicit Snapshot routine profile IDs")
    selected = list(profile_ids)
    catalog = {row["id"]: row for row in snapshot_profile_rows(root)}
    if not selected or any(not isinstance(value, str) or value not in catalog for value in selected):
        raise ValueError("Select at least one permitted Snapshot routine profile")
    if len(set(selected)) != len(selected):
        raise ValueError("Snapshot profile selection contains duplicate IDs")
    return [catalog[value] for value in selected]


def _derived_profile_id(display_name: str, chrome_directory: str) -> str:
    """Derive a readable stable ID while migrating the legacy profile schema."""
    source = str(display_name or "").strip() or str(chrome_directory or "").strip()
    wrapped = re.fullmatch(r"loggedin\((.+)\)", source, flags=re.IGNORECASE)
    numbered = re.fullmatch(r"loggedin\s*(\d+)", source, flags=re.IGNORECASE)
    if wrapped:
        source = wrapped.group(1)
    elif numbered:
        source = f"loggedin_{numbered.group(1)}"
    profile_id = re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    return profile_id or "chrome_profile"


def _canonical_profile_row(item: dict) -> dict | None:
    chrome_directory = str(
        item.get("chrome_directory") or item.get("profile") or ""
    ).strip()
    if not chrome_directory:
        return None
    display_name = str(
        item.get("display_name") or item.get("label") or chrome_directory
    ).strip() or chrome_directory
    explicit_id = str(item.get("id") or "").strip()
    profile_id = explicit_id or _derived_profile_id(display_name, chrome_directory)
    if not PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError(
            f"Profile ID {profile_id!r} must contain lowercase letters, numbers and underscores only"
        )
    return {
        "id": profile_id,
        "display_name": display_name,
        "chrome_directory": chrome_directory,
        "project_url": str(item.get("project_url") or "").strip(),
    }


@dataclass(frozen=True)
class ChromeProfile:
    """A Chrome profile discovered from a descriptive or legacy metadata file."""

    index: int
    id: str
    display_name: str
    chrome_directory: str
    source_file: str

    @property
    def profile(self) -> str:
        """Legacy alias retained for callers that still expect the raw directory."""
        return self.chrome_directory


def profile_config_path(root: Path | None = None) -> Path:
    """Return the editable logged-in profile/project configuration path."""
    return root_or_default(root) / ENV_DIR_NAME / PROFILE_CONFIG_NAME


def chrome_profiles_dir(root: Path | None = None) -> Path:
    """Return the Chrome-profile metadata directory."""
    return root_or_default(root) / "config" / "chrome-profiles"


def load_logged_in_profiles(root: Path | None = None) -> List[dict]:
    """Read profiles using the canonical ID/name/directory schema.

    Legacy ``profile``/``label`` rows remain readable so an existing installation
    is migrated without ever confusing a friendly name with Chrome's on-disk
    directory.
    """
    data = read_json_or_none(profile_config_path(root))
    if not isinstance(data, list):
        return []
    profiles: List[dict] = []
    seen_ids = set()
    seen_directories = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        row = _canonical_profile_row(item)
        if row is None:
            continue
        if row["id"] in seen_ids:
            raise ValueError(f"Duplicate profile ID: {row['id']}")
        if row["chrome_directory"] in seen_directories:
            raise ValueError(f"Duplicate Chrome directory: {row['chrome_directory']}")
        seen_ids.add(row["id"])
        seen_directories.add(row["chrome_directory"])
        profiles.append(row)
    return profiles


def save_logged_in_profiles(profiles: Iterable[dict], root: Path | None = None) -> List[dict]:
    """Validate and save the canonical logged-in profile list."""
    existing = load_logged_in_profiles(root) if profile_config_path(root).exists() else []
    existing_by_id = {row["id"]: row for row in existing}
    existing_by_directory = {row["chrome_directory"]: row for row in existing}
    cleaned: List[dict] = []
    seen_ids = set()
    seen_directories = set()
    for item in profiles:
        if not isinstance(item, dict):
            continue
        row = _canonical_profile_row(item)
        if row is None:
            continue
        previous_id = existing_by_id.get(row["id"])
        if previous_id and previous_id["chrome_directory"] != row["chrome_directory"]:
            raise ValueError(
                f"Chrome directory for stable profile ID '{row['id']}' cannot be changed"
            )
        previous_directory = existing_by_directory.get(row["chrome_directory"])
        if previous_directory and previous_directory["id"] != row["id"]:
            raise ValueError(
                f"Stable profile ID for Chrome directory '{row['chrome_directory']}' cannot be changed"
            )
        if row["id"] in seen_ids:
            raise ValueError(f"Duplicate profile ID: {row['id']}")
        if row["chrome_directory"] in seen_directories:
            raise ValueError(f"Duplicate Chrome directory: {row['chrome_directory']}")
        seen_ids.add(row["id"])
        seen_directories.add(row["chrome_directory"])
        cleaned.append(row)
    write_json(profile_config_path(root), cleaned)
    return cleaned


def _metadata_sort_key(path: Path) -> tuple:
    """Sort numbered descriptive files and legacy profileN files naturally."""
    parts = re.split(r"(\d+)", path.name.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def discover_chrome_profiles(root: Path | None = None) -> List[ChromeProfile]:
    """Return every ``*.txt`` Chrome metadata file in deterministic order.

    Current files have descriptive names. Legacy ``profileN.txt`` files are
    intentionally still accepted during migration.
    """
    base = root_or_default(root)
    configured = {
        row["chrome_directory"]: row for row in load_logged_in_profiles(base)
    }
    profiles: List[ChromeProfile] = []
    seen_directories = set()
    paths = sorted(chrome_profiles_dir(base).glob("*.txt"), key=_metadata_sort_key)
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = CHROME_PROFILE_RE.search(text)
        if not match:
            continue
        chrome_directory = match.group(1).strip()
        if chrome_directory in seen_directories:
            continue
        seen_directories.add(chrome_directory)
        row = configured.get(chrome_directory)
        display_name = row["display_name"] if row else chrome_directory
        profile_id = row["id"] if row else _derived_profile_id(display_name, chrome_directory)
        profiles.append(
            ChromeProfile(
                index=len(profiles) + 1,
                id=profile_id,
                display_name=display_name,
                chrome_directory=chrome_directory,
                source_file=str(path.relative_to(base)),
            )
        )
    return profiles


def profile_payload(root: Path | None = None) -> dict:
    """Build the canonical profile/project payload used by the UI."""
    try:
        snapshot_rows = snapshot_profile_rows(root)
        snapshot_error = None
    except ValueError as exc:
        snapshot_rows, snapshot_error = [], str(exc)
    return {
        "logged_in_profiles": load_logged_in_profiles(root),
        "chrome_profiles": [profile.__dict__ for profile in discover_chrome_profiles(root)],
        "config_path": str(profile_config_path(root).relative_to(root_or_default(root))),
        "snapshot_profiles": snapshot_rows,
        "snapshot_profile_ids": [row["id"] for row in snapshot_rows],
        "snapshot_default_profile_ids": [row["id"] for row in snapshot_rows],
        "snapshot_profile_error": snapshot_error,
    }


def selected_profile_rows(profile_ids: Iterable[str], root: Path | None = None) -> List[dict]:
    """Resolve canonical IDs or legacy raw directories, preserving selection order."""
    configured = load_logged_in_profiles(root)
    discovered = discover_chrome_profiles(root)
    lookup = {}
    for row in configured:
        lookup[row["id"]] = row
        lookup[row["chrome_directory"]] = row
        lookup[row["display_name"]] = row
    for item in discovered:
        row = {
            "id": item.id,
            "display_name": item.display_name,
            "chrome_directory": item.chrome_directory,
            "project_url": "",
        }
        lookup.setdefault(item.id, row)
        lookup.setdefault(item.chrome_directory, row)

    rows: List[dict] = []
    seen_ids = set()
    for value in profile_ids:
        identifier = str(value or "").strip()
        if not identifier:
            continue
        row = lookup.get(identifier)
        if row is None:
            raise ValueError(f"Unknown browser profile selection: {identifier}")
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        rows.append(dict(row))
    return rows


def default_profile_ids(
    count: int,
    root: Path | None = None,
    *,
    regime: str = "logged_in",
) -> List[str]:
    """Pick stable IDs from the pool matching the requested browser regime."""
    configured = [row["id"] for row in load_logged_in_profiles(root)]
    discovered = [row.id for row in discover_chrome_profiles(root)]
    profile_ids = (discovered or configured) if regime == "rotating" else (configured or discovered)
    return profile_ids[: max(0, count)]


def default_profile_names(
    count: int,
    root: Path | None = None,
    *,
    regime: str = "logged_in",
) -> List[str]:
    """Compatibility alias for :func:`default_profile_ids`."""
    return default_profile_ids(count, root, regime=regime)


def write_logged_in_profile_snapshot(rows: Iterable[dict], destination: Path) -> None:
    """Write a job-local profile config using canonical field names."""
    cleaned: List[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = _canonical_profile_row(item)
        if row is not None:
            cleaned.append(row)
    write_json(destination, cleaned)


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "profile"


def write_chrome_profile_snapshot(
    profile_ids: Iterable[str],
    destination: Path,
    root: Path | None = None,
) -> None:
    """Write descriptive job-local Chrome metadata for a selected profile set."""
    base = root_or_default(root)
    destination.mkdir(parents=True, exist_ok=True)
    rows = selected_profile_rows(profile_ids, base)
    discovered = discover_chrome_profiles(base)
    sources = {}
    for item in discovered:
        source = base / item.source_file
        sources[item.id] = source
        sources[item.chrome_directory] = source
    for old in destination.glob("*.txt"):
        old.unlink()
    for index, row in enumerate(rows, start=1):
        filename = (
            f"{index:02d}_{_safe_filename_part(row['id'])}"
            f"__{_safe_filename_part(row['chrome_directory'])}.txt"
        )
        target = destination / filename
        source = sources.get(row["id"]) or sources.get(row["chrome_directory"])
        if source and source.exists():
            shutil.copyfile(source, target)
        else:
            target.write_text(
                f"Profile Path\tChrome/{row['chrome_directory']}\n",
                encoding="utf-8",
            )


# Compatibility exports for older UI/job callers.  Runtime discovery is owned
# by the paid-audit application and new consumers import its public ``api``.
from clinic_geo.workflows.paid_audit.api import (  # noqa: E402,F401
    CHROME_PATH_ENV_VARS,
    MIN_NODE_MAJOR,
    NODE_BIN_ENV_VARS,
    PAID_AUDIT_DATA_DIRNAME,
    PAID_AUDIT_ENGINE_DIRNAME,
    chrome_path_cache_file,
    discover_chrome_path,
    discover_node_bin,
    node_binary_satisfies,
    paid_audit_cli_path,
    paid_audit_data_dir,
    paid_audit_engine_dir,
    paid_audit_engine_env,
    required_node_major,
)
