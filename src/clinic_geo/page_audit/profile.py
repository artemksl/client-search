"""Exact profile resolution for the one-page AI audit lane (ADR-002 §5).

Resolves an explicitly selected display name to *exactly one* configured
logged-in profile. Absence or ambiguity is a terminal configuration error; the
lane never falls back to another profile and never rotates. The pinned model is
recorded separately from the profile — the profile alone does not prove which
model produced an answer.

This module is deliberately stdlib + config-only: it performs no browser
interaction. It reuses the same ``config/logged-in-profiles.json`` loader seam
as the archived assist client (``chat_engine.load_logged_in_profiles``), but
matches strictly on the display name and never substitutes a corrected value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from clinic_geo.browser import chat_engine
from clinic_geo.env import load_env_file
from clinic_geo.paths import repo_root


# There is no default seat. Deployment startup must select one explicitly.
PROFILE_DISPLAY_NAME_ENV = "PAGE_AUDIT_PROFILE_DISPLAY_NAME"

# 128K-context single-seat plan (ADR-002 §5 / research §6). One profile, one
# route; the assumed window is recorded at startup so later size thresholds are
# auditable. Config-overridable via env for a differently-provisioned seat.
DEFAULT_CONTEXT_WINDOW = 131072
CONTEXT_WINDOW_ENV = "PAGE_AUDIT_CONTEXT_WINDOW"


class ProfileResolutionError(RuntimeError):
    """The exact assist profile could not be resolved to one safe identity.

    Raised for absence, ambiguity, or a missing pinned model. Terminal: the lane
    must stop rather than proceed with a fallback or rotated profile.
    """


@dataclass(frozen=True)
class ResolvedProfile:
    """The single resolved logged-in profile identity (never rotated)."""

    id: str
    display_name: str
    chrome_directory: str
    project_url: Optional[str] = None


@dataclass(frozen=True)
class StartupRecord:
    """The auditable startup identity persisted by the ledger at wave time.

    Records the resolved profile, the *separately* pinned model id/version, and
    the assumed context window. The profile alone does not prove the model.
    """

    profile: ResolvedProfile
    model_id: str
    model_version: str
    context_window: int


def _configured_profiles_path(
    *,
    repo: Path | None = None,
    profiles_path: Path | str | None = None,
) -> Path:
    """Resolve the ``logged-in-profiles.json`` seam, repo-relative by default."""

    root = (repo or repo_root()).resolve()
    configured = (
        profiles_path
        or os.getenv("LOGGEDIN_PROFILES_PATH")
        or "config/logged-in-profiles.json"
    )
    path = Path(configured).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _rows_from(
    rows: Optional[Sequence[Mapping[str, object]]],
    *,
    repo: Path | None,
    profiles_path: Path | str | None,
) -> list[dict]:
    if rows is not None:
        loaded = [dict(row) for row in rows if isinstance(row, Mapping)]
        if len(loaded) != len(list(rows)):
            raise ProfileResolutionError(
                "logged-in profile rows must all be mappings"
            )
        return loaded
    path = _configured_profiles_path(repo=repo, profiles_path=profiles_path)
    # ``load_logged_in_profiles`` normalises rows to
    # {id, display_name, chrome_directory, project_url} and is the same seam the
    # archived client used. It returns [] on a missing/invalid file.
    return list(chat_engine.load_logged_in_profiles(path))


def resolve_profile(
    rows: Optional[Sequence[Mapping[str, object]]] = None,
    *,
    repo: Path | None = None,
    profiles_path: Path | str | None = None,
    display_name: str | None = None,
) -> ResolvedProfile:
    """Resolve exactly one profile whose display name equals ``display_name``.

    ``rows`` may be supplied directly (tests); otherwise the configured
    ``logged-in-profiles.json`` seam is loaded. Zero matches → terminal error;
    two or more matches → terminal error. Never returns a fallback profile.
    """

    if not display_name or not display_name.strip():
        raise ProfileResolutionError(
            "select an explicit page-audit profile display name; "
            "no default or fallback is permitted"
        )
    loaded = _rows_from(rows, repo=repo, profiles_path=profiles_path)
    matches = [
        row
        for row in loaded
        if str(row.get("display_name") or "") == display_name
    ]
    if not matches:
        raise ProfileResolutionError(
            f"no logged-in profile has display name {display_name!r}; "
            "the assist seat is not configured (no fallback is permitted)"
        )
    if len(matches) > 1:
        raise ProfileResolutionError(
            f"{len(matches)} logged-in profiles share display name "
            f"{display_name!r}; the assist profile must be unambiguous"
        )
    row = matches[0]
    chrome_directory = str(
        row.get("chrome_directory") or row.get("profile") or ""
    ).strip()
    if not chrome_directory:
        raise ProfileResolutionError(
            f"assist profile {display_name!r} has no Chrome directory"
        )
    project_url = str(row.get("project_url") or "").strip() or None
    return ResolvedProfile(
        id=str(row.get("id") or "").strip(),
        display_name=display_name,
        chrome_directory=chrome_directory,
        project_url=project_url,
    )


def pin_model(env: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
    """Pin the model id/version from env, recorded separately from the profile.

    Mirrors the archived client's seam precedence:
    ``ASSIST_MODEL_LABEL`` / ``MODEL_LABEL`` / ``NEW_CHAT_MODEL`` for the id, and
    ``ASSIST_MODEL_VERSION`` / ``MODEL_VERSION`` (else the id) for the version.
    Absence is terminal — the profile does not prove which model was used.
    """

    # An explicitly supplied empty mapping means "no model variables".  Using
    # truthiness here would leak ambient process variables into callers and
    # make the documented terminal-absence branch impossible to exercise.
    getenv = (os.environ if env is None else env).get
    model_id = str(
        getenv("ASSIST_MODEL_LABEL")
        or getenv("MODEL_LABEL")
        or getenv("NEW_CHAT_MODEL")
        or ""
    ).strip()
    if not model_id:
        raise ProfileResolutionError(
            "assist requires a pinned model "
            "(ASSIST_MODEL_LABEL / MODEL_LABEL / NEW_CHAT_MODEL)"
        )
    model_version = str(
        getenv("ASSIST_MODEL_VERSION")
        or getenv("MODEL_VERSION")
        or model_id
    ).strip()
    return model_id, model_version


def resolve_context_window(
    env: Optional[Mapping[str, str]] = None,
    *,
    default: int = DEFAULT_CONTEXT_WINDOW,
) -> int:
    """Return the assumed context window, config-overridable, else the default.

    A non-positive or non-integer override is rejected rather than silently
    ignored, so a mis-set value cannot make later size thresholds untrustworthy.
    """

    source = os.environ if env is None else env
    raw = str(source.get(CONTEXT_WINDOW_ENV) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProfileResolutionError(
            f"{CONTEXT_WINDOW_ENV} must be a positive integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ProfileResolutionError(
            f"{CONTEXT_WINDOW_ENV} must be a positive integer, got {value}"
        )
    return value


def build_startup_record(
    rows: Optional[Sequence[Mapping[str, object]]] = None,
    *,
    repo: Path | None = None,
    profiles_path: Path | str | None = None,
    env: Optional[Mapping[str, str]] = None,
    load_dotenv: bool = False,
    display_name: str | None = None,
) -> StartupRecord:
    """Resolve profile + pinned model + context window into one startup record.

    ``load_dotenv`` optionally loads ``config/.env`` (without overriding already
    set values) before reading the configuration seams. Select the profile via
    ``display_name`` or ``PAGE_AUDIT_PROFILE_DISPLAY_NAME``; there is no default.
    The ledger persists the returned record.
    """

    root = (repo or repo_root()).resolve()
    if load_dotenv and env is None:
        load_env_file(root / "config" / ".env")
    source = os.environ if env is None else env
    selected_name = (
        display_name if display_name is not None else source.get(PROFILE_DISPLAY_NAME_ENV)
    )
    profile = resolve_profile(
        rows, repo=root, profiles_path=profiles_path, display_name=selected_name
    )
    model_id, model_version = pin_model(env)
    context_window = resolve_context_window(env)
    return StartupRecord(
        profile=profile,
        model_id=model_id,
        model_version=model_version,
        context_window=context_window,
    )


__all__ = [
    "PROFILE_DISPLAY_NAME_ENV",
    "DEFAULT_CONTEXT_WINDOW",
    "CONTEXT_WINDOW_ENV",
    "ProfileResolutionError",
    "ResolvedProfile",
    "StartupRecord",
    "resolve_profile",
    "pin_model",
    "resolve_context_window",
    "build_startup_record",
]
