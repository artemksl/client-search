"""Map a control-UI clinic/project selection to a frozen paid-audit scope.

The paid engine (`citeup-audit`) is driven by a scope document per run. This
module produces that document from the fields the founder fills in on the paid
audit launch form (or from existing clinic/project data on the Clinic GEO side).

Design rules that keep this honest and safe:

* Every default mirrors ``audit-automation/examples/small-dental-controlled-pilot.json``
  verbatim — those exact shapes are already accepted by the engine's own scope
  loader, so a generated scope validates against ``doctor`` / ``browser-prompts``
  without us inventing enum values the engine might reject.
* ``ai_measurement`` stays **off** unless the caller supplies an explicit browser
  answer recording. We never synthesize a recording (engine invariant #2).
* Identifiers used in on-disk report paths (client/engagement/run) are slugified
  to a filesystem-safe, path-segment-safe form.
"""

from __future__ import annotations

from copy import deepcopy
import re
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from clinic_geo.persistence.json_store import write_json


SCOPE_VERSION = "1.0.0"
DEFAULT_PROFILE = "paid_visibility"
DEFAULT_RUN_MODE = "controlled_pilot"
DEFAULT_ENGAGEMENT_ID = "diagnostic-audit"
DEFAULT_COUNTRY = "GB"
DEFAULT_CATEGORY = "dental clinic"

# Verbatim defaults from the engine's shipped example. Overridable per selection.
DEFAULT_PRIORITY_INTENTS = [
    "price",
    "safety",
    "practitioner qualifications",
    "suitability",
    "aftercare",
    "booking",
]
DEFAULT_CANONICAL_FACT_SURFACES = [
    "address",
    "phone",
    "fees",
    "practitioners",
    "booking",
]
DEFAULT_EVIDENCE_BEARING_PATHS = ["/treatments/", "/fees/", "/about/"]
DEFAULT_CRAWLER_ACCESS = [
    {"agent": "OAI-SearchBot", "path": "/", "effect": "allow"},
    {"agent": "ChatGPT-User", "path": "/", "effect": "allow"},
]


class ScopeError(ValueError):
    """Raised when a UI selection cannot be turned into a valid engine scope."""


def slugify(value: Any, *, fallback: str = "unknown") -> str:
    """Return a lowercase, filesystem/path-safe slug (``[a-z0-9-]``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    return slug or fallback


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    return [s for s in (_as_str(item) for item in items) if s]


def _unique_str_list(value: Any) -> list[str]:
    """Return non-empty strings once, preserving their first-seen order."""
    result: list[str] = []
    seen: set[str] = set()
    for item in _as_str_list(value):
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _branches_block(selection: Mapping, clinic_name: str, default_area: str) -> list[dict]:
    """Normalize branch references and manual branch rows for the frozen scope.

    A branch supplied by the outreach catalogue carries its stable ``branch_id``
    plus the resolved snapshot fields. An off-pipeline branch omits ``branch_id``
    and keeps the same inline shape. Identity hints are deliberately manual in
    both cases; they are preserved as provenance rather than inferred here.
    """
    raw_branches = selection.get("branches")
    if raw_branches is None:
        return []
    if not isinstance(raw_branches, (list, tuple)):
        raise ScopeError("branches must be a list")

    branches: list[dict] = []
    seen_references: set[str] = set()
    for index, raw in enumerate(raw_branches, start=1):
        if not isinstance(raw, Mapping):
            raise ScopeError(f"Branch {index} must be a mapping")

        branch_id = _as_str(raw.get("branch_id") or raw.get("id"))
        name = _as_str(raw.get("name") or raw.get("branch_name"))
        area = _as_str(raw.get("area") or default_area)
        if not name:
            raise ScopeError(f"Branch {index} requires a name")
        if not area:
            raise ScopeError(f"Branch {index} requires an area")

        identity = raw.get("identity") if isinstance(raw.get("identity"), Mapping) else {}
        entity_id = _as_str(
            raw.get("entity_id")
            or raw.get("entityId")
            or raw.get("@id")
            or identity.get("entity_id")
            or identity.get("entityId")
            or identity.get("@id")
        )
        location_id = _as_str(
            raw.get("location_id")
            or raw.get("locationId")
            or identity.get("location_id")
            or identity.get("locationId")
        )

        reference_key = branch_id or f"inline:{name.casefold()}:{area.casefold()}"
        if reference_key in seen_references:
            raise ScopeError(f"Branch {index} duplicates an earlier branch")
        seen_references.add(reference_key)

        branch: dict[str, Any] = {
            "name": name,
            "area": area,
            "seed_urls": _unique_str_list(raw.get("seed_urls")),
            "entity_id": entity_id,
            "location_id": location_id,
            "display_name": f"{clinic_name} — {name}",
        }
        if branch_id:
            branch["branch_id"] = branch_id
        postcode = _as_str(raw.get("postcode"))
        if postcode:
            branch["postcode"] = postcode
        branches.append(branch)
    return branches


def _url_origin(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ScopeError(f"Invalid scope URL: {value}") from exc
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host:
        raise ScopeError(f"Scope URLs must be absolute http(s) URLs: {value}")
    return scheme, host, port or (443 if scheme == "https" else 80)


def _validate_seed_urls(base_url: str, seed_urls: list[str]) -> None:
    base_origin = _url_origin(base_url)
    for seed_url in seed_urls:
        if _url_origin(seed_url) != base_origin:
            raise ScopeError(f"Seed URL is outside the website origin: {seed_url}")


def _clinic_block(selection: Mapping) -> dict:
    clinic = selection.get("clinic") if isinstance(selection.get("clinic"), Mapping) else selection
    name = _as_str(clinic.get("name") or selection.get("clinic_name"))
    if not name:
        raise ScopeError("A clinic name is required to build a paid audit scope")
    return {
        "name": name,
        "aliases": _as_str_list(clinic.get("aliases")),
        "area": _as_str(clinic.get("area") or selection.get("area")),
        "country": _as_str(clinic.get("country") or selection.get("country")) or DEFAULT_COUNTRY,
        "category": _as_str(clinic.get("category") or selection.get("category")) or DEFAULT_CATEGORY,
    }


def _policy_block(selection: Mapping, base_url: str) -> dict:
    override = selection.get("policy") if isinstance(selection.get("policy"), Mapping) else {}
    require_jsonld_on = _as_str_list(override.get("require_jsonld_on")) or [base_url]
    return {
        "require_sitemap": bool(override.get("require_sitemap", True)),
        "require_jsonld_on": require_jsonld_on,
        "canonical_fact_surfaces": _as_str_list(override.get("canonical_fact_surfaces"))
        or list(DEFAULT_CANONICAL_FACT_SURFACES),
        "desired_crawler_access": list(
            override.get("desired_crawler_access") or DEFAULT_CRAWLER_ACCESS
        ),
        "evidence_bearing_paths": _as_str_list(override.get("evidence_bearing_paths"))
        or list(DEFAULT_EVIDENCE_BEARING_PATHS),
    }


def _ai_measurement_block(selection: Mapping) -> dict:
    """Build the AI-visibility measurement config.

    Off by default. Only enabled when the caller supplies a validated browser
    answer recording file — the engine reads a *frozen* recording and never calls
    a model API, so we must never fabricate one here.
    """
    raw = selection.get("ai_measurement") if isinstance(selection.get("ai_measurement"), Mapping) else {}
    recording_file = _as_str(raw.get("browser_recording_file") or selection.get("browser_recording_file"))
    if not recording_file:
        return {"enabled": False, "run_limit": 0, "transport": "browser_recording"}
    try:
        run_limit = int(raw.get("run_limit") or 0)
    except (TypeError, ValueError):
        run_limit = 0
    run_limit = max(1, run_limit)
    return {
        "enabled": True,
        "run_limit": run_limit,
        "transport": "browser_recording",
        "browser_recording_file": recording_file,
    }


def build_scope(selection: Mapping) -> dict:
    """Return the immutable, branch-aware scope for a UI selection.

    Required: ``clinic.name`` (or ``clinic_name``) and ``base_url``. Everything
    else falls back to the engine's known-accepted example defaults and is fully
    overridable through the selection. Call :func:`build_engine_scope` before
    handing a branch-aware result to the engine's strict v1 schema.
    """
    if not isinstance(selection, Mapping):
        raise ScopeError("Selection must be a mapping")

    base_url = _as_str(selection.get("base_url"))
    if not base_url:
        raise ScopeError("A base_url is required to build a paid audit scope")

    clinic = _clinic_block(selection)
    client_id = slugify(
        selection.get("client_id") or clinic["name"],
        fallback="clinic",
    )
    engagement_id = slugify(
        selection.get("engagement_id") or DEFAULT_ENGAGEMENT_ID,
        fallback=DEFAULT_ENGAGEMENT_ID,
    )

    branches = _branches_block(selection, clinic["name"], clinic["area"])
    seed_urls = _unique_str_list(selection.get("seed_urls")) or [base_url]
    seed_urls = _unique_str_list(
        [*seed_urls, *(url for branch in branches for url in branch["seed_urls"])]
    )
    _validate_seed_urls(base_url, seed_urls)
    priority_treatments = _unique_str_list(selection.get("priority_treatments"))
    if not 5 <= len(priority_treatments) <= 20:
        raise ScopeError("priority_treatments must contain between 5 and 20 unique values")

    scope: dict[str, Any] = {
        "scope_version": _as_str(selection.get("scope_version")) or SCOPE_VERSION,
        "client_id": client_id,
        "engagement_id": engagement_id,
        "profile": _as_str(selection.get("profile")) or DEFAULT_PROFILE,
        "run_mode": _as_str(selection.get("run_mode")) or DEFAULT_RUN_MODE,
        "clinic": clinic,
        "base_url": base_url,
        "seed_urls": seed_urls,
        "priority_treatments": priority_treatments,
        "priority_intents": _as_str_list(selection.get("priority_intents"))
        or list(DEFAULT_PRIORITY_INTENTS),
        "competitors": list(selection.get("competitors") or []),
        "policy": _policy_block(selection, base_url),
        "ai_measurement": _ai_measurement_block(selection),
    }
    if branches:
        scope["branches"] = branches

    # limits_override is optional in the engine's schema. Only forward it when the
    # caller supplies one, so a real audit uses the engine's production defaults
    # rather than the tiny controlled-pilot limits.
    limits = selection.get("limits_override")
    if isinstance(limits, Mapping) and limits:
        scope["limits_override"] = dict(limits)

    operator_note = _as_str(selection.get("operator_note"))
    if operator_note:
        scope["operator_note"] = operator_note

    return scope


def build_engine_scope(scope: Mapping) -> dict:
    """Project a frozen branch-aware scope into the engine's strict v1 schema.

    The vendored engine currently rejects unknown top-level properties, including
    ``branches``. The full document remains the job's immutable ``scope.json``;
    this projection removes only that unsupported metadata. Branch seed URLs have
    already been folded into the top-level ``seed_urls``, so one engine run still
    covers the configured branch pages.
    """
    if not isinstance(scope, Mapping):
        raise ScopeError("Scope must be a mapping")
    projected = deepcopy(dict(scope))
    projected.pop("branches", None)
    return projected


def scope_identity(scope: Mapping) -> dict:
    """Return the ``{client_id, engagement_id}`` identity of a built scope."""
    return {
        "client_id": slugify(scope.get("client_id"), fallback="clinic"),
        "engagement_id": slugify(scope.get("engagement_id"), fallback=DEFAULT_ENGAGEMENT_ID),
    }


def write_scope(selection: Mapping, path: Path | str) -> Path:
    """Build the full provenance scope and freeze it to ``path`` as JSON."""
    scope = build_scope(selection)
    target = Path(path)
    write_json(target, scope)
    return target
