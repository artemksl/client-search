"""Freeze v2 Snapshot scope, prompts, and the complete observation plan.

All functions are pure: callers decide where and when to persist the returned
JSON-compatible dictionaries.  Rebuilding a pack from a mutated manifest fails
closed, which prevents post-result scope edits from being mistaken for the
original Snapshot.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from copy import deepcopy
from typing import Iterable, Mapping, Optional

from clinic_geo.execution.measurement_platforms import (
    ACTIVE_MEASUREMENT_PLATFORMS, AVAILABLE_MEASUREMENT_PLATFORMS,
    GOOGLE_AI_OVERVIEW_DEFERRED,
)
from clinic_geo.browser.ai_platforms import normalize_platform
from clinic_geo.evidence.purpose import EvidencePurpose
from clinic_geo.parsing.treatments import treatment_key

from .contracts_v2 import (
    BROWSER_MEASUREMENT_MODE,
    COPILOT_DERIVED_MEASUREMENT_MODE,
    COPY_POLICY_VERSION,
    DEFAULT_PLANNED_RUNS,
    DEFAULT_PLATFORMS,
    NAME_RECONCILIATION_ARTIFACT_HASH,
    NAME_RECONCILIATION_POLICY_VERSION,
    MAX_SELECTED_AREAS,
    MIN_SELECTED_AREAS,
    PROMPT_CATALOG_V4_CONTENT_HASH,
    PROMPT_CATALOG_V4_VERSION,
    PROMPT_GEOGRAPHY_HASH,
    PROMPT_GEOGRAPHY_V1,
    PROMPT_GEOGRAPHY_VERSION,
    PROMPT_PACK_CONTRACTS,
    PROMPT_PACK_V4_VERSION,
    PROMPT_PACK_VERSION,
    TEST_MODE_PLANNED_RUNS,
    canonical_sha256,
)
from .prompt_rendering_v4 import render_v4_slot
from .purpose_adapter_v1 import (
    SnapshotPurposeError,
    adapt_legacy_snapshot_artifact,
    validate_current_snapshot_artifact,
)


_SPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VARIANT_TYPES = ("area", "postcode", "station", "landmark")
SNAPSHOT_EVIDENCE_PURPOSE = EvidencePurpose.FREE_SNAPSHOT_CONFIRMATION.value
NEW_SNAPSHOT_PLATFORMS = ACTIVE_MEASUREMENT_PLATFORMS
DETACHED_CLASSIFICATION_OPTION_KEYS = frozenset(
    {
        "snapshot_classifier_engine",
        "classifier_engine",
        "classifier_engine_version",
        "classifier_backend",
        "classifier_provider",
        "classification_engine",
        "classification_engine_version",
        "classification_backend",
        "classification_provider",
        "classification_mode",
    }
)


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


class FrozenScopeError(ValueError):
    """The supplied scope is incomplete, invalid, or no longer hash-identical."""


def _detached_classification_option_paths(
    value: object,
    *,
    prefix: str = "",
) -> list[str]:
    if isinstance(value, (list, tuple)):
        found: list[str] = []
        for index, nested in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            found.extend(
                _detached_classification_option_paths(nested, prefix=path)
            )
        return found
    if not isinstance(value, Mapping):
        return []
    found: list[str] = []
    for raw_key, nested in value.items():
        key = str(raw_key).strip().casefold().replace("-", "_")
        path = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        classification_context = "classif" in key or "classif" in prefix.casefold()
        if key in DETACHED_CLASSIFICATION_OPTION_KEYS or (
            classification_context
            and any(
                token in key
                for token in ("backend", "engine", "provider", "mode")
            )
        ):
            found.append(path)
        if isinstance(nested, (Mapping, list, tuple)):
            found.extend(
                _detached_classification_option_paths(nested, prefix=path)
            )
    return found


def _reject_detached_classification_options(config: Mapping) -> None:
    detached = sorted(set(_detached_classification_option_paths(config)))
    if detached:
        raise FrozenScopeError(
            "New Snapshot runs do not accept detached-classification backend "
            "options; ordered-name extraction is runner-owned. Remove: "
            + ", ".join(detached)
        )


def validate_new_snapshot_run_config(config: Mapping) -> list[str]:
    """Validate only a current new-run request, never a historical rebuild."""

    if not isinstance(config, Mapping):
        raise FrozenScopeError("Snapshot config must be a mapping")
    _reject_detached_classification_options(config)
    raw = config.get("platforms")
    values = NEW_SNAPSHOT_PLATFORMS if raw in (None, "") else raw
    if isinstance(values, str):
        values = re.split(r"[,;\s]+", values.strip())
    if not isinstance(values, (list, tuple)):
        raise FrozenScopeError("platforms must be a list or comma-separated string")
    result: list[str] = []
    for value in values:
        if not _clean(value):
            continue
        try:
            platform = normalize_platform(_clean(value))
        except ValueError as exc:
            raise FrozenScopeError(
                "New Snapshot runs support ChatGPT, Gemini, Copilot and explicitly configured GitHub Copilot: "
                f"{exc}"
            ) from exc
        if platform == "google_ai_overview":
            raise FrozenScopeError(GOOGLE_AI_OVERVIEW_DEFERRED)
        if platform not in AVAILABLE_MEASUREMENT_PLATFORMS:
            raise FrozenScopeError(
                "New Snapshot runs support ChatGPT, Gemini, Copilot and explicitly configured GitHub Copilot; "
                f"unsupported platform: {platform}"
            )
        if platform not in result:
            result.append(platform)
    if not result:
        raise FrozenScopeError("New Snapshot runs require at least one platform")
    if "github_copilot" in result:
        from clinic_geo.execution.copilot_settings import source_settings
        try:
            source_settings(config.get("github_copilot"))
        except (TypeError, ValueError) as exc:
            raise FrozenScopeError(str(exc)) from exc
    return result


def _prompt_contract(version: object) -> Mapping:
    normalized = _clean(version or PROMPT_PACK_VERSION)
    contract = PROMPT_PACK_CONTRACTS.get(normalized)
    if contract is None:
        supported = ", ".join(PROMPT_PACK_CONTRACTS)
        raise FrozenScopeError(
            f"unsupported prompt_pack_version {normalized!r}; expected one of: {supported}"
        )
    return contract


def _scope_schema_mode(scope: Mapping, contract: Mapping) -> str:
    """Classify a scope from an allowlisted schema, never from purpose absence."""

    schema_version = _clean(scope.get("schema_version"))
    if schema_version == contract["scope_schema_version"]:
        return "current"
    if schema_version in frozenset(contract.get("legacy_scope_schema_versions") or ()):
        return "legacy"
    raise FrozenScopeError(
        f"scope schema {schema_version!r} is unsupported for "
        f"{scope.get('prompt_pack_version')!r}"
    )


def _derived_schema_for_scope(
    scope: Mapping,
    contract: Mapping,
    *,
    current_field: str,
    legacy_field: str,
) -> str:
    mode = _scope_schema_mode(scope, contract)
    if mode == "current":
        return str(contract[current_field])
    legacy_versions = tuple(contract.get(legacy_field) or ())
    if len(legacy_versions) != 1:
        raise FrozenScopeError(f"legacy {legacy_field} is not an exact schema contract")
    return str(legacy_versions[0])


def _slug(value: object, *, fallback: str = "item") -> str:
    text = unicodedata.normalize("NFKD", _clean(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SLUG_RE.sub("-", text).strip("-") or fallback


def _dedupe(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean(value)
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _normalize_clinic(config: Mapping, *, require_archetype: bool = False) -> dict:
    source = config.get("clinic") if isinstance(config.get("clinic"), Mapping) else {}
    name = _clean(source.get("name") or config.get("clinic_name"))
    if not name:
        raise FrozenScopeError("Snapshot scope requires clinic.name (or legacy clinic_name)")

    explicit_account_id = _clean(source.get("account_id") or config.get("account_id"))
    account_id = explicit_account_id or f"external-{_slug(name)}"
    aliases = source.get("verified_aliases")
    if aliases is None:
        aliases = config.get("clinic_aliases") or []
    if not isinstance(aliases, (list, tuple)):
        raise FrozenScopeError("clinic.verified_aliases must be a list")
    aliases = [alias for alias in _dedupe(aliases) if alias.casefold() != name.casefold()]
    identity_status = _clean(
        source.get("identity_status")
        or config.get("clinic_identity_status")
        or ("confirmed" if explicit_account_id else "unconfirmed")
    ).casefold()
    normalized = {
        "account_id": account_id,
        "name": name,
        "verified_aliases": aliases,
        "identity_status": identity_status,
    }
    if not require_archetype:
        return normalized

    archetype_source = source.get("archetype")
    if not isinstance(archetype_source, Mapping):
        raise FrozenScopeError("v4 Snapshot scope requires clinic.archetype")
    archetype = {
        "id": _clean(archetype_source.get("id")),
        "singular": _clean(archetype_source.get("singular")),
        "plural": _clean(archetype_source.get("plural")),
        "confirmation_status": _clean(
            archetype_source.get("confirmation_status")
        ).casefold(),
    }
    for field in ("id", "singular", "plural"):
        if not archetype[field]:
            raise FrozenScopeError(f"clinic.archetype.{field} is required for v4")
    if archetype["confirmation_status"] != "confirmed":
        raise FrozenScopeError("clinic.archetype must be confirmed before freezing v4")
    normalized["archetype"] = archetype
    return normalized


def _variant_rows(source: Mapping, area_name: str) -> list[dict]:
    raw = source.get("variants")
    values: dict[str, str] = {}
    if isinstance(raw, Mapping):
        for kind in _VARIANT_TYPES:
            values[kind] = _clean(raw.get(kind))
    elif isinstance(raw, (list, tuple)):
        for row in raw:
            if isinstance(row, Mapping):
                kind = _clean(row.get("type") or row.get("kind")).casefold()
                value = _clean(row.get("value") or row.get("name"))
                if kind in _VARIANT_TYPES and value and kind not in values:
                    values[kind] = value
            elif isinstance(row, str) and "area" not in values:
                values["area"] = _clean(row)
    for kind in _VARIANT_TYPES:
        if kind not in values:
            values[kind] = _clean(source.get(kind))
    values["area"] = values.get("area") or area_name
    return [
        {"type": kind, "value": values[kind]}
        for kind in _VARIANT_TYPES
        if values.get(kind)
    ]


def _normalize_catchment(config: Mapping) -> dict:
    source = config.get("catchment") if isinstance(config.get("catchment"), Mapping) else {}
    name = _clean(source.get("name") or source.get("area") or config.get("area"))
    if not name:
        raise FrozenScopeError("Snapshot scope requires catchment.name (or legacy area)")
    explicit_area_id = _clean(source.get("area_id") or config.get("area_id"))
    country_code = _clean(source.get("country_code") or config.get("country_code") or "GB").upper()
    if len(country_code) != 2 or not country_code.isalpha():
        raise FrozenScopeError("catchment.country_code must be a two-letter country code")
    confirmation_status = _clean(
        source.get("confirmation_status")
        or source.get("relevance_status")
        or config.get("catchment_confirmation_status")
        or ("confirmed" if explicit_area_id else "unconfirmed")
    ).casefold()
    return {
        "area_id": explicit_area_id or _slug(name, fallback="area"),
        "name": name,
        "country_code": country_code,
        "variants": _variant_rows(source, name),
        "confirmation_status": confirmation_status,
    }


def _normalize_treatments(config: Mapping) -> list[dict]:
    raw = config.get("treatments")
    if raw is None:
        raw = [config.get(f"treatment_{index}") for index in range(1, 4)]
    if not isinstance(raw, (list, tuple)):
        raise FrozenScopeError("treatments must be a list containing one to three treatments")

    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        source = item if isinstance(item, Mapping) else {}
        name = _clean(source.get("name") if source else item)
        if not name:
            continue
        key = treatment_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        explicit_id = _clean(source.get("id") if source else "")
        status = _clean(
            (source.get("commercial_relevance_status") if source else None)
            or config.get(f"treatment_{index}_commercial_relevance_status")
            or ("confirmed" if source else "unconfirmed")
        ).casefold()
        result.append(
            {
                "id": explicit_id or _slug(name, fallback=f"treatment-{index}"),
                "name": name,
                "commercial_relevance_status": status,
            }
        )
    if not 1 <= len(result) <= 3:
        raise FrozenScopeError("Snapshot scope requires one to three unique treatments")
    ids = [row["id"] for row in result]
    if len(ids) != len(set(ids)):
        raise FrozenScopeError("treatment ids must be unique")
    return result


def _normalize_geography_target(
    value: object,
    *,
    tier: str,
    country_code: str,
) -> dict:
    if not isinstance(value, Mapping):
        raise FrozenScopeError(f"areas[].geographies.{tier} must be an object")
    target = {
        "id": _clean(value.get("id") or value.get("area_id")),
        "name": _clean(value.get("name") or value.get("area")),
        "country_code": _clean(value.get("country_code") or country_code).upper(),
        "confirmation_status": _clean(value.get("confirmation_status")).casefold(),
    }
    if not target["id"] or not target["name"]:
        raise FrozenScopeError(
            f"areas[].geographies.{tier} requires confirmed id and name"
        )
    if target["confirmation_status"] != "confirmed":
        raise FrozenScopeError(
            f"areas[].geographies.{tier} must be confirmed before freezing v4"
        )
    if len(target["country_code"]) != 2 or not target["country_code"].isalpha():
        raise FrozenScopeError(
            f"areas[].geographies.{tier}.country_code must be a two-letter country code"
        )
    return target


def _assert_patient_facing_taxonomy_target(target: Mapping, *, tier: str) -> None:
    taxonomy_key = "greater_areas" if tier == "greater_area" else "cities"
    allowed = {
        (_clean(row.get("id")), _clean(row.get("name")))
        for row in PROMPT_GEOGRAPHY_V1[taxonomy_key]
        if isinstance(row, Mapping)
    }
    if (_clean(target.get("id")), _clean(target.get("name"))) not in allowed:
        raise FrozenScopeError(
            f"areas[].geographies.{tier} must use the frozen patient-facing geography taxonomy"
        )


def _normalize_area_scopes(
    config: Mapping,
    *,
    require_v4_geographies: bool = False,
) -> list[dict]:
    """Normalize one or two independently validated selected-area scopes.

    ``areas`` is the production contract.  The legacy top-level
    ``catchment`` + ``treatments`` shape remains readable so frozen v2 inputs
    and command-line fixtures do not become unusable.
    """

    raw = config.get("areas")
    if raw is None:
        if require_v4_geographies:
            raise FrozenScopeError(
                "v4 Snapshot scope requires areas with local, greater_area, and city geographies"
            )
        return [
            {
                "branch": {
                    "id": _clean(config.get("branch_id")) or "primary-branch",
                    "name": _clean(config.get("branch_name"))
                    or _clean((config.get("catchment") or {}).get("name"))
                    or "Primary branch",
                },
                "catchment": _normalize_catchment(config),
                "treatments": _normalize_treatments(config),
                "validation_status": _clean(
                    config.get("branch_treatment_validation_status") or "confirmed"
                ).casefold(),
            }
        ]
    if not isinstance(raw, (list, tuple)):
        raise FrozenScopeError("areas must be a list containing one or two selected-area scopes")
    if not MIN_SELECTED_AREAS <= len(raw) <= MAX_SELECTED_AREAS:
        raise FrozenScopeError(
            "Free Snapshot scope requires one or two selected areas (maximum 2)"
        )

    result: list[dict] = []
    area_ids: set[str] = set()
    branch_ids: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise FrozenScopeError(f"areas[{index - 1}] must be an object")
        branch_source = item.get("branch") if isinstance(item.get("branch"), Mapping) else {}
        branch_id = _clean(branch_source.get("id") or item.get("branch_id"))
        branch_name = _clean(branch_source.get("name") or item.get("branch_name"))
        catchment = _normalize_catchment(item)
        treatments = _normalize_treatments(item)
        branch_id = branch_id or catchment["area_id"]
        branch_name = branch_name or catchment["name"]
        if branch_id in branch_ids:
            raise FrozenScopeError("selected branch ids must be unique")
        if catchment["area_id"] in area_ids:
            raise FrozenScopeError("selected branches must use distinct area ids")
        branch_ids.add(branch_id)
        area_ids.add(catchment["area_id"])
        normalized_area = {
                "branch": {
                    "id": branch_id,
                    "name": branch_name,
                    "address": _clean(branch_source.get("address") or item.get("address")) or None,
                    "source_url": _clean(
                        branch_source.get("source_url") or item.get("source_url")
                    )
                    or None,
                    "member_uids": [
                        _clean(value)
                        for value in branch_source.get("member_uids") or []
                        if _clean(value)
                    ],
                    "location_confidence": _clean(
                        branch_source.get("location_confidence")
                        or item.get("location_confidence")
                    )
                    or None,
                    "requires_manual_confirmation": bool(
                        branch_source.get("requires_manual_confirmation")
                        or item.get("requires_manual_confirmation")
                    ),
                },
                "catchment": catchment,
                "treatments": treatments,
                "validation_status": _clean(
                    item.get("validation_status")
                    or item.get("branch_treatment_validation_status")
                    or "unconfirmed"
                ).casefold(),
                "validated_by": _clean(item.get("validated_by")) or None,
                "validated_at": _clean(item.get("validated_at")) or None,
            }
        if require_v4_geographies:
            geography_source = item.get("geographies")
            if not isinstance(geography_source, Mapping):
                raise FrozenScopeError(
                    f"areas[{index - 1}].geographies is required for v4"
                )
            geographies = {
                tier: _normalize_geography_target(
                    geography_source.get(tier),
                    tier=tier,
                    country_code=catchment["country_code"],
                )
                for tier in ("local", "greater_area", "city")
            }
            local = geographies["local"]
            if local["id"] != catchment["area_id"] or local["name"] != catchment["name"]:
                raise FrozenScopeError(
                    "areas[].geographies.local must mirror catchment area_id and name"
                )
            _assert_patient_facing_taxonomy_target(
                geographies["greater_area"], tier="greater_area"
            )
            _assert_patient_facing_taxonomy_target(geographies["city"], tier="city")
            normalized_area["geographies"] = geographies
        result.append(normalized_area)
    return result


def area_scopes(scope: Mapping) -> list[dict]:
    """Return the area-treatment matrix for new and legacy frozen scopes."""

    raw = scope.get("areas")
    if isinstance(raw, list) and raw:
        return [dict(row) for row in raw if isinstance(row, Mapping)]
    return [
        {
            "branch": {
                "id": str((scope.get("catchment") or {}).get("area_id") or "primary-branch"),
                "name": str((scope.get("catchment") or {}).get("name") or "Primary branch"),
            },
            "catchment": scope.get("catchment") or {},
            "treatments": scope.get("treatments") or [],
            "validation_status": "confirmed",
        }
    ]


def _normalize_platforms(
    config: Mapping,
    *,
    exact_platforms: Optional[Iterable[str]] = None,
) -> list[str]:
    raw = config.get("platforms")
    values = DEFAULT_PLATFORMS if raw in (None, "") else raw
    if isinstance(values, str):
        values = re.split(r"[,;\s]+", values.strip())
    if not isinstance(values, (list, tuple)):
        raise FrozenScopeError("platforms must be a list or comma-separated string")
    result: list[str] = []
    for value in values:
        if not _clean(value):
            continue
        try:
            platform = normalize_platform(_clean(value))
        except ValueError as exc:
            raise FrozenScopeError(str(exc)) from exc
        if platform not in result:
            result.append(platform)
    if not result:
        raise FrozenScopeError("Snapshot scope requires at least one platform")
    if exact_platforms is not None:
        canonical = [str(platform) for platform in exact_platforms]
        if set(result) != set(canonical) or len(result) != len(canonical):
            raise FrozenScopeError(
                "v4 Snapshot scope requires exactly the four frozen platforms: "
                + ", ".join(canonical)
            )
        # Input aliases and ordering are normalized before freezing so prompt
        # and observation ordering is deterministic across equivalent configs.
        return canonical
    return result


def _variants_by_type(catchment: Mapping) -> dict[str, str]:
    return {
        str(row.get("type")): _clean(row.get("value"))
        for row in catchment.get("variants") or []
        if isinstance(row, Mapping) and _clean(row.get("type")) and _clean(row.get("value"))
    }


def _v4_evidence_cells(prompt: Mapping) -> list[dict]:
    cells: list[dict] = []
    subject = prompt["subject"]
    for origin in prompt["origin_branches"]:
        payload = {
            "contract": "free_snapshot.evidence_cell.v4",
            "basket_id": prompt["basket_id"],
            "platform": prompt["platform"],
            "origin_branch_id": origin["branch_id"],
            "origin_area_id": origin["local_area_id"],
            "subject_kind": prompt["subject_kind"],
            "subject_id": subject["id"],
        }
        digest = canonical_sha256(payload)[:24]
        cells.append(
            {
                "analysis_scope_id": f"analysis-scope-{digest}",
                "evidence_cell_id": f"evidence-cell-{digest}",
                "basket_id": prompt["basket_id"],
                "platform": prompt["platform"],
                "origin_branch_id": origin["branch_id"],
                "origin_branch_name": origin["branch_name"],
                "origin_area_id": origin["local_area_id"],
                "origin_area_name": origin["local_area_name"],
                "subject_kind": prompt["subject_kind"],
                "subject_id": subject["id"],
                "subject_name": subject["name"],
                "treatment_id": prompt["treatment_id"],
                "treatment_name": prompt["treatment_name"],
            }
        )
    return cells


def _v4_prompt_rows(scope: Mapping) -> list[dict]:
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    archetype = scope["clinic"]["archetype"]
    accumulated: dict[tuple, dict] = {}

    for platform in scope["platforms"]:
        if platform not in contract["platforms"]:
            raise FrozenScopeError(
                f"{scope['prompt_pack_version']} has no frozen rendering for {platform}"
            )
        for area_order, area_scope in enumerate(area_scopes(scope), 1):
            catchment = area_scope["catchment"]
            branch = area_scope.get("branch") or {}
            geographies = area_scope["geographies"]
            variants = _variants_by_type(catchment)
            local_area = variants.get("area") or catchment["name"]
            origin_base = {
                "branch_id": branch.get("id"),
                "branch_name": branch.get("name"),
                "local_area_id": catchment["area_id"],
                "local_area_name": catchment["name"],
                "area_order": area_order,
            }
            subjects: list[tuple[dict, Optional[dict], Optional[int]]] = [
                (
                    {"id": treatment["id"], "name": treatment["name"]},
                    treatment,
                    treatment_order,
                )
                for treatment_order, treatment in enumerate(area_scope["treatments"], 1)
            ]
            subjects.append(
                (
                    {
                        "id": archetype["id"],
                        "name": archetype["singular"],
                        "singular": archetype["singular"],
                        "plural": archetype["plural"],
                    },
                    None,
                    None,
                )
            )
            for subject, treatment, treatment_order in subjects:
                subject_kind = "treatment" if treatment is not None else "clinic_archetype"
                for slot in contract["slots"]:
                    if slot["subject_kind"] != subject_kind:
                        continue
                    geography_tier = str(slot["geography_level"])
                    target = deepcopy(dict(geographies[geography_tier]))
                    rendered = render_v4_slot(
                        platform=platform,
                        slot=slot,
                        subject_kind=subject_kind,
                        subject=subject,
                        target_geography=target,
                        expected_target_geography_id=(
                            catchment["area_id"]
                            if geography_tier == "local"
                            else geographies[geography_tier]["id"]
                        ),
                        treatment_name=(
                            treatment["name"] if treatment is not None else None
                        ),
                        local_area=local_area,
                        greater_area=geographies["greater_area"]["name"],
                        city=geographies["city"]["name"],
                        archetype_singular=archetype["singular"],
                        archetype_plural=archetype["plural"],
                        variants=variants,
                    )
                    text = rendered["text"]
                    text_hash = rendered["prompt_text_sha256"]
                    semantic_prompt_id = rendered["semantic_prompt_id"]
                    prompt_id = rendered["prompt_id"]
                    collection_scope_id = rendered["collection_scope_id"]
                    origin = {
                        **origin_base,
                        "treatment_order": treatment_order,
                    }
                    dedupe_key = (
                        platform,
                        text,
                        subject_kind,
                        canonical_sha256(subject),
                        geography_tier,
                        canonical_sha256(target),
                    )
                    row = accumulated.get(dedupe_key)
                    if row is not None and geography_tier in {"greater_area", "city"}:
                        if origin["branch_id"] not in row["origin_branch_ids"]:
                            row["origin_branches"].append(origin)
                            row["origin_branch_ids"].append(origin["branch_id"])
                            row["origin_area_ids"].append(origin["local_area_id"])
                        continue

                    # Local prompts intentionally remain branch-specific.  Their
                    # target IDs are required to be distinct, but include the
                    # origin in the key as an additional fail-closed guard.
                    if geography_tier == "local":
                        dedupe_key = (*dedupe_key, origin["branch_id"])
                    row = {
                        "id": prompt_id,
                        "prompt_id": prompt_id,
                        "platform": platform,
                        "text": text,
                        "prompt_text_sha256": text_hash,
                        "text_sha256": text_hash,
                        "prompt_pack_version": scope["prompt_pack_version"],
                        "catalog_version": contract["catalog_version"],
                        "catalog_content_sha256": contract["catalog_content_sha256"],
                        "basket_id": slot["basket_id"],
                        "basket_sequence": int(slot["sequence"]),
                        "semantic_slot_id": slot["id"],
                        "semantic_prompt_id": semantic_prompt_id,
                        "semantic_intent": slot["semantic_intent"],
                        "prompt_intent": slot["semantic_intent"],
                        "intent": slot["semantic_intent"],
                        "prompt_family": str(slot["id"]).casefold(),
                        "subject_kind": subject_kind,
                        "subject_id": subject["id"],
                        "subject_name": subject["name"],
                        "subject": subject,
                        "treatment_id": treatment["id"] if treatment is not None else None,
                        "treatment_name": treatment["name"] if treatment is not None else None,
                        "treatment_order": treatment_order,
                        "clinic_archetype_id": archetype["id"] if treatment is None else None,
                        "clinic_archetype_singular": (
                            archetype["singular"] if treatment is None else None
                        ),
                        "clinic_archetype_plural": (
                            archetype["plural"] if treatment is None else None
                        ),
                        "geography_tier": geography_tier,
                        "target_geography": target,
                        "area_id": target["id"],
                        "area_name": target["name"],
                        "origin_branches": [origin],
                        "origin_branch_ids": [origin["branch_id"]],
                        "origin_area_ids": [origin["local_area_id"]],
                        "collection_scope_id": collection_scope_id,
                        "scope_id": collection_scope_id,
                        "requested_variant_type": rendered["requested_variant_type"],
                        "actual_variant": rendered["actual_variant"],
                        "naturalness_score": rendered["naturalness_score"],
                        "slot_naturalness_score": rendered[
                            "slot_naturalness_score"
                        ],
                        "report_eligible": True,
                    }
                    accumulated[dedupe_key] = row

    rows = list(accumulated.values())
    for row in rows:
        cells = _v4_evidence_cells(row)
        row["origin"] = {"branches": deepcopy(row["origin_branches"])}
        row["branch_id"] = (
            row["origin_branches"][0]["branch_id"]
            if len(row["origin_branches"]) == 1
            else None
        )
        row["branch_name"] = (
            row["origin_branches"][0]["branch_name"]
            if len(row["origin_branches"]) == 1
            else None
        )
        row["area_order"] = (
            row["origin_branches"][0]["area_order"]
            if len(row["origin_branches"]) == 1
            else None
        )
        row["evidence_cells"] = cells
        row["analysis_scope_ids"] = [cell["analysis_scope_id"] for cell in cells]
        row["evidence_cell_ids"] = [cell["evidence_cell_id"] for cell in cells]
        row["analysis_scope_id"] = (
            row["analysis_scope_ids"][0] if len(cells) == 1 else None
        )
        row["evidence_cell_id"] = row["evidence_cell_ids"][0] if len(cells) == 1 else None
    return rows


def _prompt_rows(scope: Mapping) -> list[dict]:
    if _clean(scope.get("prompt_pack_version")) == PROMPT_PACK_V4_VERSION:
        return _v4_prompt_rows(scope)
    templates = _prompt_contract(scope.get("prompt_pack_version"))["templates"]
    if not isinstance(scope.get("areas"), list):
        catchment = scope["catchment"]
        variants = _variants_by_type(catchment)
        area = variants.get("area") or _clean(catchment.get("name"))
        legacy_rows: list[dict] = []
        for treatment_order, treatment in enumerate(scope["treatments"], 1):
            for template in templates:
                sequence = int(template["sequence"])
                requested_variant = str(template["variant_type"])
                actual_value = variants.get(requested_variant) or area
                actual_type = requested_variant if variants.get(requested_variant) else "area"
                text = str(template["template"]).format(
                    treatment=treatment["name"],
                    area=area,
                    station_or_area=variants.get("station") or area,
                    postcode_or_area=variants.get("postcode") or area,
                    landmark_or_area=variants.get("landmark") or area,
                )
                prompt_id = f"{treatment['id']}:p{sequence:02d}"
                legacy_rows.append(
                    {
                        "id": prompt_id,
                        "prompt_id": prompt_id,
                        "text": text,
                        "treatment_id": treatment["id"],
                        "treatment_name": treatment["name"],
                        "treatment_order": treatment_order,
                        "area_id": catchment["area_id"],
                        "prompt_intent": template["intent"],
                        "intent": template["intent"],
                        "prompt_family": f"p{sequence:02d}",
                        "requested_variant_type": requested_variant,
                        "actual_variant": {
                            "type": actual_type,
                            "value": actual_value,
                            "used_fallback": actual_type != requested_variant,
                        },
                        "report_eligible": True,
                    }
                )
        return legacy_rows

    rows: list[dict] = []
    matrix = area_scopes(scope)
    for area_order, area_scope in enumerate(matrix, 1):
        catchment = area_scope["catchment"]
        variants = _variants_by_type(catchment)
        area = variants.get("area") or _clean(catchment.get("name"))
        branch = area_scope.get("branch") or {}
        for treatment_order, treatment in enumerate(area_scope["treatments"], 1):
            for template in templates:
                sequence = int(template["sequence"])
                requested_variant = str(template["variant_type"])
                actual_value = variants.get(requested_variant) or area
                actual_type = requested_variant if variants.get(requested_variant) else "area"
                text = str(template["template"]).format(
                    treatment=treatment["name"],
                    area=area,
                    station_or_area=variants.get("station") or area,
                    postcode_or_area=variants.get("postcode") or area,
                    landmark_or_area=variants.get("landmark") or area,
                )
                prompt_id = f"{treatment['id']}:{catchment['area_id']}:p{sequence:02d}"
                rows.append(
                    {
                        "id": prompt_id,
                        "prompt_id": prompt_id,
                        "text": text,
                        "branch_id": branch.get("id"),
                        "branch_name": branch.get("name"),
                        "area_id": catchment["area_id"],
                        "area_name": catchment["name"],
                        "area_order": area_order,
                        "treatment_id": treatment["id"],
                        "treatment_name": treatment["name"],
                        "treatment_order": treatment_order,
                        "prompt_intent": template["intent"],
                        "intent": template["intent"],
                        "prompt_family": f"p{sequence:02d}",
                        "requested_variant_type": requested_variant,
                        "actual_variant": {
                            "type": actual_type,
                            "value": actual_value,
                            "used_fallback": actual_type != requested_variant,
                        },
                        "report_eligible": True,
                    }
                )
    return rows


def _prompt_hash_payload(scope: Mapping, rows: list[dict]) -> dict:
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    payload = {
        "schema_version": _derived_schema_for_scope(
            scope,
            contract,
            current_field="schema_version",
            legacy_field="legacy_schema_versions",
        ),
        "snapshot_id": scope["snapshot_id"],
        "prompt_pack_version": scope["prompt_pack_version"],
        "report_eligible": True,
        "prompts": rows,
    }
    if "purpose" in scope:
        payload["purpose"] = scope["purpose"]
    return payload


def prompt_pack_hash(scope: Mapping) -> str:
    """Recompute the prompt hash implied by a scope's treatment/catchment data."""

    return canonical_sha256(_prompt_hash_payload(scope, _prompt_rows(scope)))


def scope_hash(scope: Mapping) -> str:
    payload = deepcopy(dict(scope))
    payload.pop("scope_hash", None)
    return canonical_sha256(payload)


def freeze_scope(
    config: Mapping,
    *,
    snapshot_id: Optional[str] = None,
    test_mode: bool = False,
    prompt_pack_version: Optional[str] = None,
) -> dict:
    """Normalize and hash the immutable v2 scope manifest.

    Normal mode is always three runs.  A one-run scope must be explicitly
    created with ``test_mode=True`` and remains permanently marked as such.
    """

    if not isinstance(config, Mapping):
        raise FrozenScopeError("Snapshot config must be a mapping")
    for purpose_key in ("purpose", "evidence_purpose"):
        if purpose_key in config and str(config.get(purpose_key) or "") != SNAPSHOT_EVIDENCE_PURPOSE:
            raise FrozenScopeError(
                f"{purpose_key} is system-owned and must equal {SNAPSHOT_EVIDENCE_PURPOSE!r}"
            )
    resolved_prompt_pack_version = _clean(
        prompt_pack_version or config.get("prompt_pack_version") or PROMPT_PACK_VERSION
    )
    prompt_contract = _prompt_contract(resolved_prompt_pack_version)
    if config.get("platform_name_reconciliation") is False:
        raise FrozenScopeError(
            "Free Snapshot requires platform_name_reconciliation=true"
        )
    for key, expected in (
        ("scope_version", prompt_contract["scope_version"]),
        ("prompt_pack_version", resolved_prompt_pack_version),
        ("evidence_policy_version", prompt_contract["evidence_policy_version"]),
        ("copy_policy_version", COPY_POLICY_VERSION),
    ):
        supplied = _clean(config.get(key))
        if supplied and supplied != expected:
            raise FrozenScopeError(f"{key} is immutable and must equal {expected!r}")

    configured_runs = config.get("planned_runs")
    planned_runs = TEST_MODE_PLANNED_RUNS if test_mode else DEFAULT_PLANNED_RUNS
    # Explicit test mode is a safe one-run override of the normal config.  The
    # same checked-in config can therefore drive both modes without being
    # rewritten.  Normal mode still rejects any value other than the frozen 3.
    if not test_mode and configured_runs not in (None, "") and int(configured_runs) != planned_runs:
        raise FrozenScopeError(f"normal mode requires planned_runs={planned_runs}")

    prompt_intents = config.get("prompt_intents") or list(prompt_contract["prompt_intents"])
    if list(prompt_intents) != list(prompt_contract["prompt_intents"]):
        raise FrozenScopeError(
            f"{resolved_prompt_pack_version} requires its frozen prompt intents in order"
        )

    resolved_snapshot_id = _clean(snapshot_id or config.get("snapshot_id"))
    if not resolved_snapshot_id:
        resolved_snapshot_id = f"snapshot-v2-{uuid.uuid4().hex[:16]}"

    is_v4 = resolved_prompt_pack_version == PROMPT_PACK_V4_VERSION
    if is_v4 and config.get("areas") is None:
        raise FrozenScopeError(
            "v4 Snapshot scope requires the areas matrix; use an explicit v2/v3 prompt pack "
            "for legacy flat scope shapes"
        )
    selected_areas = (
        _normalize_area_scopes(config, require_v4_geographies=is_v4)
        if config.get("areas") is not None
        else None
    )
    scope_dimensions = (
        {
            "areas": selected_areas,
            # Compatibility mirrors for older readers.  The authoritative mapping
            # is always ``areas``; treatments must never be inferred across areas.
            "catchment": deepcopy(selected_areas[0]["catchment"]),
            "treatments": deepcopy(selected_areas[0]["treatments"]),
        }
        if selected_areas
        else {
            "catchment": _normalize_catchment(config),
            "treatments": _normalize_treatments(config),
        }
    )
    platforms = _normalize_platforms(
        config,
        exact_platforms=prompt_contract["platforms"] if is_v4 else None,
    )
    platform_data_modes = {
        platform: (
            COPILOT_DERIVED_MEASUREMENT_MODE
            if platform == "copilot"
            else BROWSER_MEASUREMENT_MODE
        )
        for platform in platforms
    }
    configured_data_modes = config.get("platform_data_modes")
    if configured_data_modes is not None:
        if not isinstance(configured_data_modes, Mapping):
            raise FrozenScopeError("platform_data_modes must be a mapping")
        if dict(configured_data_modes) != platform_data_modes:
            raise FrozenScopeError(
                "platform_data_modes is immutable and must match the current collection contract"
            )
    manifest = {
        "schema_version": prompt_contract["scope_schema_version"],
        "scope_version": prompt_contract["scope_version"],
        "purpose": SNAPSHOT_EVIDENCE_PURPOSE,
        "snapshot_id": resolved_snapshot_id,
        "outside_in": bool(config.get("outside_in", True)),
        "clinic": _normalize_clinic(config, require_archetype=is_v4),
        **scope_dimensions,
        "platforms": platforms,
        "platform_data_modes": platform_data_modes,
        "prompt_intents": list(prompt_contract["prompt_intents"]),
        "planned_runs": planned_runs,
        "test_mode": bool(test_mode),
        "prompt_pack_version": resolved_prompt_pack_version,
        "evidence_policy_version": prompt_contract["evidence_policy_version"],
        "copy_policy_version": COPY_POLICY_VERSION,
        "competitor_link_verification": False,
        "platform_name_reconciliation": True,
        "name_reconciliation_policy_version": NAME_RECONCILIATION_POLICY_VERSION,
        "name_reconciliation_artifact_hash": NAME_RECONCILIATION_ARTIFACT_HASH,
        "prompt_template_artifact_hash": prompt_contract["template_artifact_hash"],
        "evidence_policy_artifact_hash": prompt_contract["evidence_policy_artifact_hash"],
    }
    if is_v4:
        manifest.update(
            {
                "prompt_catalog_version": PROMPT_CATALOG_V4_VERSION,
                "prompt_catalog_content_sha256": PROMPT_CATALOG_V4_CONTENT_HASH,
                "prompt_geography_version": PROMPT_GEOGRAPHY_VERSION,
                "prompt_geography_hash": PROMPT_GEOGRAPHY_HASH,
            }
        )
    if isinstance(config.get("branch_treatment_selection"), Mapping):
        manifest["selection_provenance"] = deepcopy(dict(config["branch_treatment_selection"]))
    manifest["prompt_pack_hash"] = prompt_pack_hash(manifest)
    manifest["scope_hash"] = scope_hash(manifest)
    return manifest


def assert_frozen_scope(scope: Mapping) -> None:
    if not isinstance(scope, Mapping):
        raise FrozenScopeError("scope manifest must be a mapping")
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    try:
        schema_mode = _scope_schema_mode(scope, contract)
        if schema_mode == "current":
            validate_current_snapshot_artifact(scope, label="Snapshot scope manifest")
        else:
            # Historical manifests retain their original bytes and hashes.  A
            # purpose-bearing transition artifact is checked exactly; an older
            # purpose-less artifact can only enter through the named adapter.
            if "purpose" in scope:
                validate_current_snapshot_artifact(
                    scope,
                    label="historical purpose-bound Snapshot scope manifest",
                )
            else:
                adapt_legacy_snapshot_artifact(
                    scope,
                    label="historical Snapshot scope manifest",
                    artifact_kind="scope_manifest",
                    supported_schema_versions=tuple(
                        contract.get("legacy_scope_schema_versions") or ()
                    ),
                )
    except SnapshotPurposeError as exc:
        raise FrozenScopeError(str(exc)) from exc
    if scope.get("scope_version") != contract["scope_version"]:
        raise FrozenScopeError(
            f"scope_version must equal {contract['scope_version']!r} for "
            f"{scope.get('prompt_pack_version')}"
        )
    # ``_scope_schema_mode`` above has already restricted this value to the
    # exact current schema or an explicit historical allowlist.
    if list(scope.get("prompt_intents") or []) != list(contract["prompt_intents"]):
        raise FrozenScopeError(
            f"{scope.get('prompt_pack_version')} prompt intents do not match its frozen contract"
        )
    if scope.get("prompt_template_artifact_hash") != contract["template_artifact_hash"]:
        raise FrozenScopeError(
            f"{scope.get('prompt_pack_version')} template artifact hash does not match its contract"
        )
    if scope.get("evidence_policy_version") != contract["evidence_policy_version"]:
        raise FrozenScopeError("evidence policy version does not match the prompt contract")
    if scope.get("evidence_policy_artifact_hash") != contract["evidence_policy_artifact_hash"]:
        raise FrozenScopeError("evidence policy hash does not match the prompt contract")
    if scope.get("platform_name_reconciliation"):
        if scope.get("competitor_link_verification"):
            raise FrozenScopeError(
                "new scopes cannot enable retired conversational link follow-ups"
            )
        if (
            scope.get("name_reconciliation_policy_version")
            != NAME_RECONCILIATION_POLICY_VERSION
        ):
            raise FrozenScopeError("name-reconciliation policy version does not match")
        if (
            scope.get("name_reconciliation_artifact_hash")
            != NAME_RECONCILIATION_ARTIFACT_HASH
        ):
            raise FrozenScopeError("name-reconciliation artifact hash does not match")
    if scope.get("platform_data_modes") is not None:
        expected_data_modes = {
            platform: (
                COPILOT_DERIVED_MEASUREMENT_MODE
                if platform == "copilot"
                else BROWSER_MEASUREMENT_MODE
            )
            for platform in scope.get("platforms") or []
        }
        if dict(scope.get("platform_data_modes") or {}) != expected_data_modes:
            raise FrozenScopeError("platform data modes no longer match the frozen contract")
    selected_areas = scope.get("areas")
    if selected_areas is not None:
        if not isinstance(selected_areas, list):
            raise FrozenScopeError("frozen areas must be a list")
        if not MIN_SELECTED_AREAS <= len(selected_areas) <= MAX_SELECTED_AREAS:
            raise FrozenScopeError(
                "Free Snapshot scope requires one or two selected areas (maximum 2)"
            )
    if scope.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION:
        if not isinstance(selected_areas, list):
            raise FrozenScopeError("v4 frozen scope requires the areas matrix")
        if scope.get("prompt_catalog_version") != PROMPT_CATALOG_V4_VERSION:
            raise FrozenScopeError("v4 prompt catalog version no longer matches its contract")
        if scope.get("prompt_catalog_content_sha256") != PROMPT_CATALOG_V4_CONTENT_HASH:
            raise FrozenScopeError("v4 prompt catalog content hash no longer matches its contract")
        if scope.get("prompt_geography_version") != PROMPT_GEOGRAPHY_VERSION:
            raise FrozenScopeError("v4 prompt geography version no longer matches its contract")
        if scope.get("prompt_geography_hash") != PROMPT_GEOGRAPHY_HASH:
            raise FrozenScopeError("v4 prompt geography hash no longer matches its contract")
        if list(scope.get("platforms") or []) != list(contract["platforms"]):
            raise FrozenScopeError(
                "v4 frozen platforms must exactly match the canonical four-platform contract"
            )
        clinic = scope.get("clinic") or {}
        archetype = clinic.get("archetype") if isinstance(clinic, Mapping) else None
        if not isinstance(archetype, Mapping) or archetype.get("confirmation_status") != "confirmed":
            raise FrozenScopeError("v4 frozen scope requires a confirmed clinic archetype")
        if any(not _clean(archetype.get(field)) for field in ("id", "singular", "plural")):
            raise FrozenScopeError("v4 frozen clinic archetype is incomplete")
        for selected in selected_areas:
            geographies = selected.get("geographies") if isinstance(selected, Mapping) else None
            if not isinstance(geographies, Mapping):
                raise FrozenScopeError("v4 frozen areas require geographies")
            for tier in ("local", "greater_area", "city"):
                target = geographies.get(tier)
                if not isinstance(target, Mapping) or target.get("confirmation_status") != "confirmed":
                    raise FrozenScopeError(f"v4 {tier} geography must remain confirmed")
            catchment = selected.get("catchment") or {}
            local = geographies["local"]
            if local.get("id") != catchment.get("area_id") or local.get("name") != catchment.get("name"):
                raise FrozenScopeError("v4 local geography must mirror its catchment")
            _assert_patient_facing_taxonomy_target(
                geographies["greater_area"], tier="greater_area"
            )
            _assert_patient_facing_taxonomy_target(geographies["city"], tier="city")
    expected_prompt_hash = prompt_pack_hash(scope)
    if scope.get("prompt_pack_hash") != expected_prompt_hash:
        raise FrozenScopeError("prompt pack hash no longer matches the frozen scope")
    expected_scope_hash = scope_hash(scope)
    if scope.get("scope_hash") != expected_scope_hash:
        raise FrozenScopeError("scope hash no longer matches the frozen manifest")


def build_prompt_pack(scope: Mapping) -> dict:
    """Build the versioned report-eligible prompts for every treatment and area."""

    assert_frozen_scope(scope)
    rows = _prompt_rows(scope)
    payload = _prompt_hash_payload(scope, rows)
    computed_hash = canonical_sha256(payload)
    if computed_hash != scope["prompt_pack_hash"]:
        raise FrozenScopeError("rebuilt prompt pack differs from its frozen hash")
    return {
        **payload,
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": computed_hash,
        "prompt_count": len(rows),
    }


def _assert_prompt_pack_boundary(scope: Mapping, prompt_pack: Mapping) -> None:
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    schema_mode = _scope_schema_mode(scope, contract)
    expected_schema = _derived_schema_for_scope(
        scope,
        contract,
        current_field="schema_version",
        legacy_field="legacy_schema_versions",
    )
    if prompt_pack.get("schema_version") != expected_schema:
        raise FrozenScopeError("prompt pack schema does not match the frozen scope era")
    try:
        if schema_mode == "current":
            validate_current_snapshot_artifact(
                prompt_pack,
                label="Snapshot prompt pack",
            )
        elif "purpose" in prompt_pack:
            validate_current_snapshot_artifact(
                prompt_pack,
                label="historical purpose-bound Snapshot prompt pack",
            )
        else:
            adapt_legacy_snapshot_artifact(
                prompt_pack,
                label="historical Snapshot prompt pack",
                artifact_kind="prompt_pack",
                supported_schema_versions=tuple(
                    contract.get("legacy_schema_versions") or ()
                ),
            )
    except SnapshotPurposeError as exc:
        raise FrozenScopeError(str(exc)) from exc


def _scope_id(treatment_id: str, area_id: str, platform: str) -> str:
    return f"{treatment_id}:{area_id}:{platform}"


_V4_OBSERVATION_PROMPT_FIELDS = (
    "prompt_pack_version",
    "catalog_version",
    "catalog_content_sha256",
    "basket_id",
    "basket_sequence",
    "semantic_slot_id",
    "semantic_prompt_id",
    "semantic_intent",
    "subject_kind",
    "subject_id",
    "subject_name",
    "subject",
    "treatment_name",
    "treatment_order",
    "clinic_archetype_id",
    "clinic_archetype_singular",
    "clinic_archetype_plural",
    "geography_tier",
    "target_geography",
    "origin",
    "origin_branches",
    "origin_branch_ids",
    "origin_area_ids",
    "collection_scope_id",
    "analysis_scope_id",
    "analysis_scope_ids",
    "evidence_cell_id",
    "evidence_cell_ids",
    "evidence_cells",
    "actual_variant",
    "naturalness_score",
    "slot_naturalness_score",
    "report_eligible",
)


def _build_v4_observation_plan(scope: Mapping, prompt_pack: Mapping) -> dict:
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    rows: list[dict] = []
    for prompt in prompt_pack["prompts"]:
        platform = str(prompt["platform"])
        if platform not in scope["platforms"]:
            raise FrozenScopeError("v4 prompt platform is outside the frozen scope")
        for run in range(1, int(scope["planned_runs"]) + 1):
            identity = {
                "snapshot_id": scope["snapshot_id"],
                "scope_id": prompt["collection_scope_id"],
                "prompt_id": prompt["prompt_id"],
                "run": run,
                "platform": platform,
            }
            observation_id = f"obs-v4-{canonical_sha256(identity)[:24]}"
            target = prompt["target_geography"]
            row = {
                "observation_id": observation_id,
                **(
                    {"purpose": SNAPSHOT_EVIDENCE_PURPOSE}
                    if "purpose" in scope
                    else {}
                ),
                "scope_id": prompt["collection_scope_id"],
                "scope_hash": scope["scope_hash"],
                "prompt_pack_hash": scope["prompt_pack_hash"],
                "prompt_id": prompt["prompt_id"],
                "prompt_intent": prompt["prompt_intent"],
                "prompt_text": prompt["text"],
                "prompt_text_sha256": prompt["prompt_text_sha256"],
                "branch_id": prompt.get("branch_id"),
                "branch_name": prompt.get("branch_name"),
                "treatment_id": prompt.get("treatment_id"),
                "area_id": target["id"],
                "run": run,
                "platform": platform,
                "model_or_surface": platform,
                "access_mode": "browser",
                "location_context": {
                    "country_code": target["country_code"],
                    "area_id": target["id"],
                    "area": target["name"],
                    "geography_tier": prompt["geography_tier"],
                    "target_geography": deepcopy(target),
                    "origin_branches": deepcopy(prompt["origin_branches"]),
                    "prompt_variant": deepcopy(prompt["actual_variant"]),
                },
                "started_at": None,
                "collected_at": None,
                "status": None,
                "raw_answer_reference": None,
                "raw_answer_sha256": None,
                "citations": [],
                "attempts": 0,
            }
            for field in _V4_OBSERVATION_PROMPT_FIELDS:
                row[field] = deepcopy(prompt.get(field))
            rows.append(row)
    plan_hash_payload = {
        "schema_version": _derived_schema_for_scope(
            scope,
            contract,
            current_field="observation_plan_schema_version",
            legacy_field="legacy_observation_plan_schema_versions",
        ),
        **(
            {"purpose": SNAPSHOT_EVIDENCE_PURPOSE}
            if "purpose" in scope
            else {}
        ),
        "snapshot_id": scope["snapshot_id"],
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": scope["prompt_pack_hash"],
        "prompt_pack_version": scope["prompt_pack_version"],
        "prompt_catalog_version": scope["prompt_catalog_version"],
        "prompt_catalog_content_sha256": scope["prompt_catalog_content_sha256"],
        "observations": rows,
    }
    return {
        **plan_hash_payload,
        "observation_plan_hash": canonical_sha256(plan_hash_payload),
        "planned_observations": len(rows),
    }


def build_observation_plan(scope: Mapping, prompt_pack: Mapping) -> dict:
    """Expand prompts to every configured run and platform.

    Planned rows use ``status=None`` and ``attempts=0``.  Once attempted, the
    normalizer replaces that null with one of the seven standardized statuses.
    """

    assert_frozen_scope(scope)
    _assert_prompt_pack_boundary(scope, prompt_pack)
    if prompt_pack.get("prompt_pack_hash") != scope["prompt_pack_hash"]:
        raise FrozenScopeError("observation plan prompt hash does not match scope")
    expected_pack = build_prompt_pack(scope)
    if canonical_sha256(_prompt_hash_payload(scope, list(prompt_pack.get("prompts") or []))) != expected_pack["prompt_pack_hash"]:
        raise FrozenScopeError("observation plan cannot use a mutated prompt pack")
    if scope.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION:
        return _build_v4_observation_plan(scope, prompt_pack)

    rows: list[dict] = []
    catchments = {
        str(row["catchment"]["area_id"]): row["catchment"]
        for row in area_scopes(scope)
    }
    for prompt in prompt_pack["prompts"]:
        catchment = catchments.get(str(prompt.get("area_id")))
        if not catchment:
            raise FrozenScopeError("prompt references an area outside the frozen scope")
        for platform in scope["platforms"]:
            scope_id = _scope_id(prompt["treatment_id"], prompt["area_id"], platform)
            for run in range(1, int(scope["planned_runs"]) + 1):
                identity = {
                    "snapshot_id": scope["snapshot_id"],
                    "scope_id": scope_id,
                    "prompt_id": prompt["prompt_id"],
                    "run": run,
                    "platform": platform,
                }
                observation_id = f"obs-{canonical_sha256(identity)[:20]}"
                rows.append(
                    {
                        "observation_id": observation_id,
                        **(
                            {"purpose": SNAPSHOT_EVIDENCE_PURPOSE}
                            if "purpose" in scope
                            else {}
                        ),
                        "scope_id": scope_id,
                        "scope_hash": scope["scope_hash"],
                        "prompt_pack_hash": scope["prompt_pack_hash"],
                        "prompt_id": prompt["prompt_id"],
                        "prompt_intent": prompt["prompt_intent"],
                        **(
                            {
                                "branch_id": prompt.get("branch_id"),
                                "branch_name": prompt.get("branch_name"),
                            }
                            if isinstance(scope.get("areas"), list)
                            else {}
                        ),
                        "treatment_id": prompt["treatment_id"],
                        "area_id": catchment["area_id"],
                        "run": run,
                        "platform": platform,
                        "model_or_surface": platform,
                        "access_mode": "browser",
                        "location_context": {
                            "country_code": catchment["country_code"],
                            "area_id": catchment["area_id"],
                            "area": catchment["name"],
                            "prompt_variant": deepcopy(prompt["actual_variant"]),
                        },
                        "started_at": None,
                        "collected_at": None,
                        "status": None,
                        "raw_answer_reference": None,
                        "raw_answer_sha256": None,
                        "citations": [],
                        "attempts": 0,
                    }
                )
    plan_hash_payload = {
        "schema_version": _derived_schema_for_scope(
            scope,
            _prompt_contract(scope.get("prompt_pack_version")),
            current_field="observation_plan_schema_version",
            legacy_field="legacy_observation_plan_schema_versions",
        ),
        **(
            {"purpose": SNAPSHOT_EVIDENCE_PURPOSE}
            if "purpose" in scope
            else {}
        ),
        "snapshot_id": scope["snapshot_id"],
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": scope["prompt_pack_hash"],
        "observations": rows,
    }
    return {
        **plan_hash_payload,
        "observation_plan_hash": canonical_sha256(plan_hash_payload),
        "planned_observations": len(rows),
    }


def freeze_snapshot(
    config: Mapping,
    *,
    snapshot_id: Optional[str] = None,
    test_mode: bool = False,
    prompt_pack_version: Optional[str] = None,
) -> dict:
    scope = freeze_scope(
        config,
        snapshot_id=snapshot_id,
        test_mode=test_mode,
        prompt_pack_version=prompt_pack_version,
    )
    prompts = build_prompt_pack(scope)
    plan = build_observation_plan(scope, prompts)
    return {
        "scope_manifest": scope,
        "snapshot_prompts": prompts,
        "observation_plan": plan,
    }


def preview_snapshot_counts(
    config: Mapping,
    *,
    prompt_pack_version: str = PROMPT_PACK_VERSION,
    test_mode: bool = False,
    selected_platforms: Iterable[str] | None = None,
) -> dict:
    """Return exact canonical volumes, optionally projecting selected platforms.

    The catalog's historical platform set stays frozen; callers can preview a
    supported subset without including unselected renderings in any count.
    """

    frozen = freeze_snapshot(
        config,
        snapshot_id="snapshot-preview-counts",
        test_mode=test_mode,
        prompt_pack_version=prompt_pack_version,
    )
    scope = frozen["scope_manifest"]
    prompts = frozen["snapshot_prompts"]["prompts"]
    platforms = list(scope["platforms"])
    if selected_platforms is not None:
        if isinstance(selected_platforms, (str, bytes)):
            raise FrozenScopeError("Preview platforms must be an explicit list")
        platforms = list(selected_platforms)
        if (not platforms or any(not isinstance(value, str) or value not in scope["platforms"] for value in platforms)
                or len(set(platforms)) != len(platforms)):
            raise FrozenScopeError("Preview platforms must be a nonempty unique subset of the frozen catalog")
    planned_runs = int(scope["planned_runs"])
    if scope["prompt_pack_version"] != PROMPT_PACK_V4_VERSION:
        semantic_count = len(prompts)
        platform_count = len(platforms)
        return {
            "prompt_pack_version": scope["prompt_pack_version"],
            "platform_prompt_count": semantic_count * platform_count,
            "semantic_prompt_count": semantic_count,
            "prompts_per_platform": semantic_count,
            "platform_prompt_counts": {
                platform: semantic_count for platform in platforms
            },
            "prompts_by_basket": {},
            "prompt_details_by_basket": {},
            "planned_runs": planned_runs,
            "platform_count": platform_count,
            "planned_observation_count": semantic_count * platform_count * planned_runs,
            "deduplicated_origin_assignments": 0,
        }
    prompts = [row for row in prompts if row.get("platform") in platforms]
    platform_prompt_counts = {
        platform: sum(row.get("platform") == platform for row in prompts)
        for platform in platforms
    }
    baskets: dict[str, int] = {}
    basket_details: dict[str, dict] = {}
    for basket_id in ("treatment_local", "treatment_expanded", "clinic_archetype"):
        basket_rows = [row for row in prompts if row.get("basket_id") == basket_id]
        semantic_basket_count = len(
            {row.get("semantic_prompt_id") for row in basket_rows}
        )
        baskets[basket_id] = semantic_basket_count
        basket_details[basket_id] = {
            "platform_prompt_count": len(basket_rows),
            "semantic_prompt_count": semantic_basket_count,
            "platform_prompt_counts": {
                platform: sum(row.get("platform") == platform for row in basket_rows)
                for platform in platforms
            },
        }
    return {
        "prompt_pack_version": scope["prompt_pack_version"],
        "platform_prompt_count": len(prompts),
        "semantic_prompt_count": len(
            {row.get("semantic_prompt_id") for row in prompts}
        ),
        "prompts_per_platform": next(iter(platform_prompt_counts.values()), 0),
        "platform_prompt_counts": platform_prompt_counts,
        "prompts_by_basket": baskets,
        "prompt_details_by_basket": basket_details,
        "planned_runs": planned_runs,
        "platform_count": len(platforms),
        "planned_observation_count": len(prompts) * planned_runs,
        "deduplicated_origin_assignments": sum(
            max(0, len(row.get("origin_branches") or []) - 1) for row in prompts
        ),
    }


def verify_frozen_snapshot(
    scope: Mapping,
    prompt_pack: Mapping,
    observation_plan: Mapping,
) -> list[str]:
    """Return integrity errors; an empty list means all three artifacts agree."""

    errors: list[str] = []
    try:
        assert_frozen_scope(scope)
    except FrozenScopeError as exc:
        errors.append(str(exc))
        return errors
    try:
        expected_pack = build_prompt_pack(scope)
        if prompt_pack != expected_pack:
            errors.append("snapshot prompt pack differs from the frozen pack")
    except FrozenScopeError as exc:
        errors.append(str(exc))
    try:
        expected_plan = build_observation_plan(scope, prompt_pack)
        if observation_plan != expected_plan:
            errors.append("observation plan differs from the frozen plan")
    except FrozenScopeError as exc:
        errors.append(str(exc))
    return errors
