"""Founder-controlled branch and treatment selection for Free Snapshots."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from clinic_geo.execution.measurement_platforms import ACTIVE_MEASUREMENT_PLATFORMS, AVAILABLE_MEASUREMENT_PLATFORMS
from clinic_geo.evidence.purpose import EvidencePurpose
from clinic_geo.integrations.visibility_selection import (
    DETACHED_CLASSIFICATION_OPTION_KEYS,
    measurement_from_frozen_snapshot,
)
from clinic_geo.outreach.api import (
    SNAPSHOT_PROCEDURES_RELATIVE_PATH,
    SnapshotCatalogError,
    build_snapshot_selection_catalog,
    verify_current_release,
)
from clinic_geo.paths import repo_root as default_repo_root
from clinic_geo.workflows.free_audit.contracts_v2 import (
    DEFAULT_PLANNED_RUNS,
    MAX_SELECTED_AREAS,
    PROMPT_PACK_CONTRACTS,
    PROMPT_PACK_VERSION,
)


MAX_BRANCHES = 2
MAX_AREAS = MAX_SELECTED_AREAS
MAX_TREATMENTS_PER_BRANCH = 3
# Compatibility value for v3 clients. V4 totals must always come from the
# canonical planner because broader geography prompts can be deduplicated.
PROMPTS_PER_TREATMENT = 7
PLANNED_RUNS = DEFAULT_PLANNED_RUNS
NEW_SNAPSHOT_PLATFORMS = ACTIVE_MEASUREMENT_PLATFORMS
PLATFORMS = len(NEW_SNAPSHOT_PLATFORMS)
V4_PROMPT_PACK_VERSION = "snapshot-prompts-v4"
LEGACY_PROMPT_PACK_VERSIONS = frozenset({"snapshot-prompts-v2", "snapshot-prompts-v3"})

V4_BASKET_SIZES = {
    "treatment_local": 7,
    "treatment_expanded": 7,
    "clinic_archetype": 7,
}

GEOGRAPHY_TARGETS_PATH = Path("data/free-audit/prompt-geography-v1.json")
LOCAL_AREAS_PATH = Path("data/free-audit/area_geo.json")
AREA_SCOPE_KIND = "area_treatments"
SNAPSHOT_EVIDENCE_PURPOSE = EvidencePurpose.FREE_SNAPSHOT_CONFIRMATION.value


class SnapshotProductionError(ValueError):
    """The requested Snapshot branch-treatment selection is not valid."""


class SnapshotReleaseConflict(SnapshotProductionError):
    """The database release changed after the production choices were loaded."""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _slug(value: object, *, fallback: str = "clinic-type") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _clean(value).casefold()).strip("-")
    return slug or fallback


def _prompt_pack_version(config: Mapping) -> str:
    return _clean(config.get("prompt_pack_version") or PROMPT_PACK_VERSION)


def _uses_v4_selection(config: Mapping) -> bool:
    return _prompt_pack_version(config) not in LEGACY_PROMPT_PACK_VERSIONS


def _strip_detached_classification_options(
    value: object,
    *,
    classification_context: bool = False,
) -> object:
    """Copy catalog input into a new-run document without legacy backend knobs."""

    if isinstance(value, Mapping):
        result = {}
        for raw_key, nested in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            nested_classification_context = classification_context or "classif" in key
            if key in DETACHED_CLASSIFICATION_OPTION_KEYS or (
                nested_classification_context
                and any(
                    token in key
                    for token in ("backend", "engine", "provider", "mode")
                )
            ):
                continue
            result[raw_key] = _strip_detached_classification_options(
                nested,
                classification_context=nested_classification_context,
            )
        return result
    if isinstance(value, list):
        return [
            _strip_detached_classification_options(
                item,
                classification_context=classification_context,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _strip_detached_classification_options(
                item,
                classification_context=classification_context,
            )
            for item in value
        )
    return deepcopy(value)


def _validate_new_snapshot_options(selection: Mapping) -> list[str]:
    from clinic_geo.workflows.free_audit.scope_v2 import (
        FrozenScopeError,
        validate_new_snapshot_run_config,
    )

    try:
        return validate_new_snapshot_run_config(selection)
    except FrozenScopeError as exc:
        raise SnapshotProductionError(str(exc)) from exc


def snapshot_run_mode(selection: Mapping) -> dict:
    """Validate host publication intent separately from the engine config."""
    mode = selection.get("runs", "full")
    if not isinstance(mode, str) or mode not in {"single", "full"}:
        raise SnapshotProductionError("Snapshot runs must be single or full")
    repetitions = 1 if mode == "single" else 3
    test_mode = mode == "single"
    for name in ("repetitions", "planned_runs"):
        if name in selection and (type(selection[name]) is not int or selection[name] != repetitions):
            raise SnapshotProductionError(f"Snapshot {name} conflicts with runs mode")
    if "test_mode" in selection and (type(selection["test_mode"]) is not bool or selection["test_mode"] != test_mode):
        raise SnapshotProductionError("Snapshot test_mode conflicts with runs mode")
    return {"runs": mode, "planned_runs": repetitions, "repetitions": repetitions, "test_mode": test_mode}


def _target_payload(rows: list[dict]) -> list[dict]:
    return [deepcopy(row) for row in rows]


def _geography_targets(root: Path | str | None = None) -> dict[str, list[dict]]:
    path = Path(root or default_repo_root()).resolve() / GEOGRAPHY_TARGETS_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotProductionError(
            f"Controlled Snapshot geography catalogue is unavailable: {path}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SnapshotProductionError("Controlled Snapshot geography catalogue is malformed")

    result: dict[str, list[dict]] = {}
    for key in ("greater_areas", "cities"):
        raw = payload.get(key)
        if not isinstance(raw, list) or not raw:
            raise SnapshotProductionError(f"Controlled geography catalogue needs {key}")
        rows = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise SnapshotProductionError(f"Controlled geography {key} contains a malformed row")
            target_id = _clean(item.get("id"))
            name = _clean(item.get("name"))
            if not target_id or not name or target_id in seen:
                raise SnapshotProductionError(f"Controlled geography {key} needs unique IDs and names")
            seen.add(target_id)
            rows.append(
                {
                    "area_id": target_id,
                    "name": name,
                    "country_code": _clean(item.get("country_code") or "GB").upper(),
                }
            )
        result[key] = rows
    return result


def _production_contract(config: Mapping, root: Path | str | None = None) -> dict:
    version = _prompt_pack_version(config)
    uses_v4 = _uses_v4_selection(config)
    geography_targets = _geography_targets(root)
    legacy_contract = PROMPT_PACK_CONTRACTS.get(version)
    legacy_prompt_count = (
        PROMPTS_PER_TREATMENT
        if uses_v4
        else len(legacy_contract.get("templates") or [])
        if isinstance(legacy_contract, Mapping)
        else PROMPTS_PER_TREATMENT
    )
    return {
        "purpose": SNAPSHOT_EVIDENCE_PURPOSE,
        "prompt_pack_version": version,
        "requires_v4_scope": uses_v4,
        "prompt_baskets": deepcopy(V4_BASKET_SIZES) if uses_v4 else {},
        "supported_platforms": list(AVAILABLE_MEASUREMENT_PLATFORMS),
        "default_platforms": list(NEW_SNAPSHOT_PLATFORMS),
        "geography_targets": {
            "greater_areas": _target_payload(geography_targets["greater_areas"]),
            "cities": _target_payload(geography_targets["cities"]),
        },
        "limits": {
            "max_branches": MAX_BRANCHES,
            "max_areas": MAX_AREAS,
            "max_treatments_per_branch": MAX_TREATMENTS_PER_BRANCH,
            # Compatibility alias for v2/v3 clients. New clients use the
            # basket fields and the exact server preview below.
            "prompts_per_treatment": legacy_prompt_count,
            "local_treatment_prompts": V4_BASKET_SIZES["treatment_local"],
            "expanded_treatment_prompts": V4_BASKET_SIZES["treatment_expanded"],
            "general_clinic_prompts": V4_BASKET_SIZES["clinic_archetype"],
            "planned_runs": PLANNED_RUNS,
            "platforms": PLATFORMS,
        },
    }


def _controlled_target(target_id: object, rows: list[dict], *, label: str) -> dict:
    selected = _clean(target_id)
    target = next((row for row in rows if row["area_id"] == selected), None)
    if target is None:
        raise SnapshotProductionError(f"Select a controlled {label} target")
    return {
        "id": target["area_id"],
        "name": target["name"],
        "country_code": target["country_code"],
        "confirmation_status": "confirmed",
    }


def _clinic_archetype(config: Mapping, selection: Mapping, *, required: bool) -> dict | None:
    raw = selection.get("clinic_archetype")
    if not isinstance(raw, Mapping):
        raw = {}
    existing_clinic = config.get("clinic") if isinstance(config.get("clinic"), Mapping) else {}
    existing = (
        existing_clinic.get("archetype")
        if isinstance(existing_clinic.get("archetype"), Mapping)
        else {}
    )
    singular = _clean(raw.get("singular") or existing.get("singular"))
    plural = _clean(raw.get("plural") or existing.get("plural"))
    if not singular and not plural and not required:
        return None
    if not singular or not plural:
        raise SnapshotProductionError(
            "Enter both the singular and plural patient-language clinic type"
        )
    if not 3 <= len(singular) <= 80 or not 3 <= len(plural) <= 80:
        raise SnapshotProductionError("The patient-language clinic type must be 3 to 80 characters")
    if singular.casefold() == plural.casefold():
        raise SnapshotProductionError("Use distinct singular and plural clinic-type labels")
    clinic_names = [
        _clean(existing_clinic.get("name")),
        *[_clean(value) for value in existing_clinic.get("verified_aliases") or []],
    ]
    labels = {singular.casefold(), plural.casefold()}
    if any(name and name.casefold() in labels for name in clinic_names):
        raise SnapshotProductionError(
            "The clinic type must be generic patient language, not the clinic's brand name"
        )
    return {
        "id": _slug(singular),
        "singular": singular,
        "plural": plural,
        "confirmation_status": "confirmed",
    }


def _canonical_count_preview(config: Mapping) -> dict:
    """Use runner counts for current work and preserve legacy previews."""

    version = _prompt_pack_version(config)
    if {"copilot", "github_copilot"}.intersection(config.get("platforms", ())):
        return _area_count_preview(config)
    if version not in LEGACY_PROMPT_PACK_VERSIONS:
        return _area_count_preview(config, error_prefix="Snapshot scope preview failed")
    try:
        from clinic_geo.workflows.free_audit.scope_v2 import (
            build_observation_plan,
            build_prompt_pack,
            freeze_scope,
            validate_new_snapshot_run_config,
        )

        selected_platforms = validate_new_snapshot_run_config(config)
        scope = freeze_scope(
            config,
            snapshot_id="snapshot-production-preview",
            prompt_pack_version=version,
            test_mode=config["test_mode"],
        )
        prompts = build_prompt_pack(scope)
        plan = build_observation_plan(scope, prompts)
        semantic_count = int(prompts["prompt_count"])
        platform_count = len(scope["platforms"])
        raw = {
            "prompt_pack_version": version,
            "semantic_prompt_count": semantic_count,
            "platform_prompt_count": semantic_count * platform_count,
            "prompts_per_platform": semantic_count,
            "platform_count": platform_count,
            "platform_prompt_counts": {platform: semantic_count for platform in selected_platforms},
            "prompts_by_basket": {},
            "prompt_details_by_basket": {},
            "planned_runs": scope["planned_runs"],
            "planned_observation_count": plan["planned_observations"],
        }
    except Exception as exc:
        if isinstance(exc, SnapshotProductionError):
            raise
        raise SnapshotProductionError(f"Snapshot scope preview failed: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SnapshotProductionError("Snapshot count planner returned an invalid preview")
    semantic = int(raw.get("semantic_prompt_count") or raw.get("prompt_count") or 0)
    planned_runs = int(raw.get("planned_runs") or PLANNED_RUNS)
    renderings = semantic * len(selected_platforms)
    observations = renderings * planned_runs
    if semantic <= 0 or renderings <= 0 or observations <= 0:
        raise SnapshotProductionError("Snapshot count planner returned incomplete totals")
    return {
        **deepcopy(dict(raw)),
        "prompt_pack_version": _clean(raw.get("prompt_pack_version") or version),
        "semantic_prompt_count": semantic,
        "concept_count": semantic,
        "platform_prompt_count": renderings,
        "rendering_count": renderings,
        "planned_observation_count": observations,
    }


def _branch_catalog(config: Mapping) -> list[dict]:
    raw = config.get("branches")
    if raw is None:
        raw = config.get("areas")
    if raw is None:
        raw = [
            {
                "branch": {
                    "id": config.get("branch_id") or "primary-branch",
                    "name": config.get("branch_name")
                    or (config.get("catchment") or {}).get("name")
                    or "Primary branch",
                },
                "catchment": config.get("catchment") or {},
                "treatments": config.get("treatments") or [],
            }
        ]
    if not isinstance(raw, list) or not raw:
        raise SnapshotProductionError("No branch catalogue is available for this clinic")

    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise SnapshotProductionError(f"Branch catalogue row {index} is malformed")
        branch = item.get("branch") if isinstance(item.get("branch"), Mapping) else item
        catchment = item.get("catchment") if isinstance(item.get("catchment"), Mapping) else {}
        branch_id = _clean(branch.get("id") or item.get("branch_id") or catchment.get("area_id"))
        branch_name = _clean(branch.get("name") or item.get("branch_name") or catchment.get("name"))
        if not branch_id or not branch_name:
            raise SnapshotProductionError(f"Branch catalogue row {index} needs an id and name")
        if branch_id in seen:
            raise SnapshotProductionError(f"Duplicate branch id: {branch_id}")
        seen.add(branch_id)
        treatments = item.get("treatments")
        if not isinstance(treatments, list) or not treatments:
            raise SnapshotProductionError(f"{branch_name} has no selectable treatments")
        treatment_rows = []
        treatment_ids: set[str] = set()
        for treatment_index, treatment in enumerate(treatments, 1):
            source = treatment if isinstance(treatment, Mapping) else {"name": treatment}
            treatment_id = _clean(source.get("id"))
            treatment_name = _clean(source.get("name"))
            if not treatment_id or not treatment_name:
                raise SnapshotProductionError(
                    f"Treatment {treatment_index} for {branch_name} needs an id and name"
                )
            if treatment_id in treatment_ids:
                raise SnapshotProductionError(
                    f"Duplicate treatment id {treatment_id} for {branch_name}"
                )
            treatment_ids.add(treatment_id)
            treatment_rows.append(
                {
                    "id": treatment_id,
                    "name": treatment_name,
                    "candidate_status": _clean(
                        source.get("commercial_relevance_status") or "unconfirmed"
                    ).casefold(),
                    "source_url": _clean(source.get("source_url")) or None,
                }
            )
        result.append(
            {
                "id": branch_id,
                "name": branch_name,
                "address": _clean(branch.get("address") or item.get("address")) or None,
                "source_url": _clean(branch.get("source_url") or item.get("source_url")) or None,
                "member_uids": [
                    _clean(value) for value in branch.get("member_uids") or [] if _clean(value)
                ],
                "location_confidence": _clean(
                    branch.get("location_confidence") or item.get("location_confidence")
                )
                or None,
                "requires_manual_confirmation": bool(
                    branch.get("requires_manual_confirmation")
                    or item.get("requires_manual_confirmation")
                ),
                "candidate_status": _clean(
                    item.get("validation_status")
                    or branch.get("validation_status")
                    or catchment.get("confirmation_status")
                    or "unconfirmed"
                ).casefold(),
                "catchment": deepcopy(dict(catchment)),
                "treatments": treatment_rows,
            }
        )
    return result


def snapshot_production_options(config: Mapping) -> dict:
    branches = _branch_catalog(config)
    contract = _production_contract(config)
    return {
        "schema_version": "free_snapshot.production_options.v2",
        "clinic": deepcopy(dict(config.get("clinic") or {})),
        **contract,
        "branches": branches,
    }


def _database_catalog_with_contract(root: Path) -> tuple[dict, dict[str, dict]]:
    try:
        options, account_catalog = build_snapshot_selection_catalog(
            verify_current_release(root),
            procedures_path=root / SNAPSHOT_PROCEDURES_RELATIVE_PATH,
        )
    except SnapshotCatalogError as exc:
        raise SnapshotProductionError(str(exc)) from exc
    return {
        "schema_version": options["schema_version"],
        "research_release_id": options["research_release_id"],
        "research_manifest_sha256": options["research_manifest_sha256"],
        **_production_contract({}, root),
        "treatments": options["treatments"],
        "accounts": options["accounts"],
    }, account_catalog


def _database_catalog(root: Path | str) -> tuple[dict, dict[str, dict]]:
    return _database_catalog_with_contract(Path(root).resolve())


def snapshot_database_production_options(
    root: Path | str,
    base_config: Mapping | None = None,
) -> dict:
    options, _catalog = _database_catalog(root)
    contract = _production_contract(base_config or {}, root)
    return {**options, **contract, "schema_version": "free_snapshot.production_options.v2"}


def snapshot_area_production_options(
    root: Path | str,
    base_config: Mapping | None = None,
) -> dict:
    """Return independent area/treatment choices without loading clinic records.

    Local names come from the patient-query geography lookup, never quota
    territories or clinic addresses. Broader geography assignments remain an
    explicit operator choice. Reference stations/postcodes are not projected.
    """
    root = Path(root).resolve()
    try:
        local_source = json.loads((root / LOCAL_AREAS_PATH).read_text(encoding="utf-8"))
        treatment_source = json.loads(
            (root / SNAPSHOT_PROCEDURES_RELATIVE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise SnapshotProductionError("The area and treatment catalogues are unavailable") from exc
    local_names = local_source.get("areas") if isinstance(local_source, Mapping) else None
    if not isinstance(local_names, Mapping) or not local_names:
        raise SnapshotProductionError("The patient-facing local area catalogue is malformed")
    local_areas = [
        {"id": _slug(name), "area_id": _slug(name), "name": _clean(name), "country_code": "GB"}
        for name in local_names
        if _clean(name)
    ]
    if len(local_areas) != len(local_names) or len({row["id"] for row in local_areas}) != len(local_areas):
        raise SnapshotProductionError("Local area names must produce unique stable IDs")
    if not isinstance(treatment_source, list) or not treatment_source:
        raise SnapshotProductionError("The treatment catalogue is malformed")
    treatments = []
    for raw in treatment_source:
        if not isinstance(raw, Mapping) or not _clean(raw.get("id")) or not _clean(raw.get("name")):
            raise SnapshotProductionError("The treatment catalogue needs an ID and name for every treatment")
        treatments.append({"id": _clean(raw["id"]), "name": _clean(raw["name"])})
    if len({row["id"] for row in treatments}) != len(treatments):
        raise SnapshotProductionError("Treatment catalogue IDs must be unique")
    contract = _production_contract(base_config or {}, root)
    return {
        **contract,
        "schema_version": "free_snapshot.area_production_options.v1",
        "scope_kind": AREA_SCOPE_KIND,
        "local_areas": sorted(local_areas, key=lambda row: row["name"].casefold()),
        "treatments": treatments,
        "limits": {**contract["limits"], "max_treatments_per_area": MAX_TREATMENTS_PER_BRANCH},
    }


def _area_count_preview(
    config: Mapping, *, error_prefix: str = "Area measurement preview failed",
) -> dict:
    """Ask the runner's public pure planner for the exact reusable-job volume."""
    from visibility_runner.api import summarize_measurement

    try:
        preview = summarize_measurement(
            measurement_from_frozen_snapshot(config),
            prompt_pack_version=_prompt_pack_version(config),
            # Single/full publication intent belongs to the host. The runner
            # receives repetitions=1/3 and never the legacy test-mode switch.
            test_mode=False,
        )["counts"]
    except (TypeError, ValueError) as exc:
        raise SnapshotProductionError(f"{error_prefix}: {exc}") from exc
    return {
        **preview,
        "concept_count": preview["semantic_prompt_count"],
        "rendering_count": preview["platform_prompt_count"],
    }


def build_area_selected_snapshot_config(
    root: Path | str,
    base_config: Mapping,
    selection: Mapping,
    *,
    require_confirmation: bool = True,
) -> tuple[dict, dict]:
    """Freeze a reusable area/treatment measurement with no target clinic."""
    if not isinstance(selection, Mapping) or selection.get("scope_kind") != AREA_SCOPE_KIND:
        raise SnapshotProductionError("Area measurement requires scope_kind area_treatments")
    platforms = _validate_new_snapshot_options(selection)
    run_mode = snapshot_run_mode(selection)
    if any(key in selection for key in ("purpose", "evidence_purpose")):
        raise SnapshotProductionError("Snapshot evidence purpose is system-owned")
    if any(key in selection for key in ("clinic", "account_id", "branches")):
        raise SnapshotProductionError("Choose a clinic only when creating a report from the completed area results")
    if require_confirmation and selection.get("confirmed") is not True:
        raise SnapshotProductionError("Confirm the selected areas, geography combinations, and treatments")
    if not _uses_v4_selection(base_config):
        raise SnapshotProductionError("Area measurement requires the current v4 prompt catalogue")
    options = snapshot_area_production_options(root, base_config)
    local_catalog = {row["area_id"]: row for row in options["local_areas"]}
    treatment_catalog = {row["id"]: row for row in options["treatments"]}
    raw_areas = selection.get("areas")
    if not isinstance(raw_areas, list) or not 1 <= len(raw_areas) <= MAX_AREAS:
        raise SnapshotProductionError("Select one or two local areas")
    if selection.get("clinic_archetype") is not None and not isinstance(selection["clinic_archetype"], Mapping):
        raise SnapshotProductionError("Search category must be an object when provided")
    archetype = _clinic_archetype({}, selection, required=False)
    reviewer = _clean(selection.get("reviewer")) or ("Operator" if require_confirmation else "Preview only")
    validated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    areas, summary_rows = [], []
    selected_ids: set[str] = set()
    for raw in raw_areas:
        if not isinstance(raw, Mapping):
            raise SnapshotProductionError("Each selected area must be an object")
        area_id = _clean(raw.get("area_id"))
        if area_id not in local_catalog:
            raise SnapshotProductionError(f"Unknown selected local area: {area_id or '(blank)'}")
        if area_id in selected_ids:
            raise SnapshotProductionError(f"Local area selected twice: {area_id}")
        selected_ids.add(area_id)
        local = local_catalog[area_id]
        ids = raw.get("treatment_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TREATMENTS_PER_BRANCH:
            raise SnapshotProductionError(f"Select one to three treatments for {local['name']}")
        ids = [_clean(value) for value in ids]
        if len(set(ids)) != len(ids):
            raise SnapshotProductionError(f"A treatment was selected twice for {local['name']}")
        unknown = [value for value in ids if value not in treatment_catalog]
        if unknown:
            raise SnapshotProductionError(f"Unknown treatment for {local['name']}: {', '.join(unknown)}")
        geographies = {
            "local": _controlled_target(area_id, options["local_areas"], label="local area"),
            "greater_area": _controlled_target(raw.get("greater_area_id"), options["geography_targets"]["greater_areas"], label="greater-area"),
            "city": _controlled_target(raw.get("city_id"), options["geography_targets"]["cities"], label="city"),
        }
        areas.append({
            "id": area_id,
            "name": local["name"],
            "catchment": {
                "area_id": area_id, "name": local["name"], "country_code": local["country_code"],
                "confirmation_status": "confirmed", "variants": [{"type": "area", "value": local["name"]}],
            },
            "geographies": geographies,
            "treatments": [{**treatment_catalog[value], "commercial_relevance_status": "confirmed"} for value in ids],
            "validation_status": "confirmed", "validated_by": reviewer, "validated_at": validated_at,
        })
        summary_rows.append({
            "area_id": area_id, "area_name": local["name"], "treatment_ids": ids,
            "greater_area_id": geographies["greater_area"]["id"], "city_id": geographies["city"]["id"],
        })
    # A positive list prevents a clinic-specific base file from leaking identity,
    # branch assignments or publication provenance into reusable measurements.
    result = {
        key: deepcopy(base_config[key])
        for key in ("outside_in", "execution_policy_version", "platform_name_reconciliation")
        if key in base_config
    }
    result.update({
        "scope_kind": AREA_SCOPE_KIND, "areas": areas,
        **({"clinic_archetype": archetype} if archetype is not None else {}),
        "platforms": platforms, "prompt_pack_version": _prompt_pack_version(base_config),
        "purpose": SNAPSHOT_EVIDENCE_PURPOSE, **run_mode,
        "area_treatment_selection": {
            "schema_version": "free_snapshot.area_selection.v1", "confirmed": require_confirmation,
            "reviewer": reviewer, "validated_at": validated_at, "areas": summary_rows,
        },
    })
    if "github_copilot" in platforms:
        from clinic_geo.execution.copilot_settings import source_settings
        result["github_copilot"] = source_settings(selection.get("github_copilot"))
    preview = _area_count_preview(result)
    summary = {
        "schema_version": "free_snapshot.production_preview.v1", "scope_kind": AREA_SCOPE_KIND,
        "purpose": SNAPSHOT_EVIDENCE_PURPOSE, "area_count": len(areas), "area_ids": [row["id"] for row in areas],
        "area_names": [row["name"] for row in areas],
        "areas": deepcopy(summary_rows),
        "area_treatment_count": sum(len(row["treatments"]) for row in areas), **run_mode,
        "distinct_prompt_count": preview["semantic_prompt_count"], **preview,
    }
    return result, summary


def preview_area_snapshot_selection(root: Path | str, base_config: Mapping, selection: Mapping) -> dict:
    """Preview reusable measurements without creating jobs, runs, or reports."""
    selected, summary = build_area_selected_snapshot_config(root, base_config, selection, require_confirmation=False)
    return {
        **summary, "preview_only": True, "report_eligible": False,
        "selection_schema_version": selected["area_treatment_selection"]["schema_version"],
    }


def build_database_selected_snapshot_config(
    root: Path | str,
    base_config: Mapping,
    selection: Mapping,
    *,
    require_confirmation: bool = True,
) -> tuple[dict, dict]:
    options, accounts = _database_catalog(root)
    selected_release_id = _clean(selection.get("research_release_id"))
    selected_manifest = _clean(selection.get("research_manifest_sha256"))
    if (
        selected_release_id != options["research_release_id"]
        or selected_manifest != options["research_manifest_sha256"]
    ):
        raise SnapshotReleaseConflict(
            "The verified database release changed; reload the Snapshot choices before starting"
        )
    account_id = _clean(selection.get("account_id")) if isinstance(selection, Mapping) else ""
    if not account_id or account_id not in accounts:
        raise SnapshotProductionError("Select a clinic from the current verified database release")
    selected_base = deepcopy(dict(base_config))
    selected_base["clinic"] = deepcopy(accounts[account_id]["clinic"])
    selected_base["clinic"]["identity_status"] = "confirmed"
    selected_base["branches"] = deepcopy(accounts[account_id]["branches"])
    selected_base["research_release_id"] = accounts[account_id]["research_release_id"]
    selected_config, summary = build_selected_snapshot_config(
        selected_base,
        selection,
        require_confirmation=require_confirmation,
        geography_root=root,
    )
    selected_config["branch_treatment_selection"]["research_release_id"] = accounts[account_id][
        "research_release_id"
    ]
    selected_config["branch_treatment_selection"]["research_manifest_sha256"] = accounts[
        account_id
    ]["research_manifest_sha256"]
    selected_config["branch_treatment_selection"]["account_id"] = account_id
    return selected_config, summary


def build_selected_snapshot_config(
    config: Mapping,
    selection: Mapping,
    *,
    require_confirmation: bool = True,
    geography_root: Path | str | None = None,
) -> tuple[dict, dict]:
    if not isinstance(selection, Mapping):
        raise SnapshotProductionError("Snapshot production requires a branch selection")
    platforms = _validate_new_snapshot_options(selection)
    run_mode = snapshot_run_mode(selection)
    forbidden_purpose_fields = sorted(
        key for key in ("purpose", "evidence_purpose") if key in selection
    )
    if forbidden_purpose_fields:
        raise SnapshotProductionError(
            "Snapshot evidence purpose is system-owned; remove caller field(s): "
            + ", ".join(forbidden_purpose_fields)
        )
    if require_confirmation and selection.get("confirmed") is not True:
        raise SnapshotProductionError(
            "Confirm the clinic identity, patient-language clinic type, and every selected branch, geography, and treatment assignment have been manually validated"
        )
    reviewer = _clean(selection.get("reviewer"))
    if require_confirmation and not reviewer:
        raise SnapshotProductionError("Snapshot production requires the validator's name")
    reviewer = reviewer or "Preview only"
    selected = selection.get("branches")
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_BRANCHES:
        raise SnapshotProductionError("Select one or two branches")

    uses_v4 = _uses_v4_selection(config)
    archetype = _clinic_archetype(config, selection, required=uses_v4)
    controlled_geographies = _geography_targets(geography_root) if uses_v4 else {}

    catalog = {row["id"]: row for row in _branch_catalog(config)}
    selected_ids: set[str] = set()
    selected_area_ids: set[str] = set()
    areas = []
    summary_rows = []
    validated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for item in selected:
        if not isinstance(item, Mapping):
            raise SnapshotProductionError("Each selected branch must be an object")
        branch_id = _clean(item.get("branch_id"))
        if not branch_id or branch_id not in catalog:
            raise SnapshotProductionError(f"Unknown selected branch: {branch_id or '(blank)'}")
        if branch_id in selected_ids:
            raise SnapshotProductionError(f"Branch selected twice: {branch_id}")
        selected_ids.add(branch_id)
        branch = catalog[branch_id]
        catchment = deepcopy(branch["catchment"])
        area_id = _clean(catchment.get("area_id"))
        if not area_id or not _clean(catchment.get("name")):
            raise SnapshotProductionError(f"{branch['name']} needs a validated area")
        submitted_area_id = _clean(item.get("area_id"))
        if submitted_area_id and submitted_area_id != area_id:
            raise SnapshotProductionError(
                f"Selected area {submitted_area_id} does not belong to {branch['name']}"
            )
        if area_id in selected_area_ids:
            raise SnapshotProductionError("The two selected branches must represent different areas")
        selected_area_ids.add(area_id)

        treatment_ids = item.get("treatment_ids")
        if not isinstance(treatment_ids, list):
            raise SnapshotProductionError(f"Select treatments for {branch['name']}")
        treatment_ids = [_clean(value) for value in treatment_ids if _clean(value)]
        if len(treatment_ids) != len(set(treatment_ids)):
            raise SnapshotProductionError(f"A treatment was selected twice for {branch['name']}")
        if not 1 <= len(treatment_ids) <= MAX_TREATMENTS_PER_BRANCH:
            raise SnapshotProductionError(f"Select one to three treatments for {branch['name']}")
        treatment_catalog = {row["id"]: row for row in branch["treatments"]}
        unknown = [value for value in treatment_ids if value not in treatment_catalog]
        if unknown:
            raise SnapshotProductionError(
                f"Unknown treatment for {branch['name']}: {', '.join(unknown)}"
            )
        treatments = [
            {
                "id": treatment_catalog[value]["id"],
                "name": treatment_catalog[value]["name"],
                "commercial_relevance_status": "confirmed",
            }
            for value in treatment_ids
        ]
        catchment["confirmation_status"] = "confirmed"
        geographies = None
        if uses_v4:
            geographies = {
                "local": {
                    "id": area_id,
                    "name": _clean(catchment.get("name")),
                    "country_code": _clean(catchment.get("country_code") or "GB").upper(),
                    "confirmation_status": "confirmed",
                },
                "greater_area": _controlled_target(
                    item.get("greater_area_id"),
                    controlled_geographies["greater_areas"],
                    label="greater-area",
                ),
                "city": _controlled_target(
                    item.get("city_id"),
                    controlled_geographies["cities"],
                    label="city",
                ),
            }
        areas.append(
            {
                "branch": {
                    "id": branch["id"],
                    "name": branch["name"],
                    "address": branch.get("address"),
                    "source_url": branch.get("source_url"),
                    "member_uids": branch.get("member_uids") or [],
                    "location_confidence": branch.get("location_confidence"),
                    "requires_manual_confirmation": bool(
                        branch.get("requires_manual_confirmation")
                    ),
                },
                "catchment": catchment,
                **({"geographies": geographies} if geographies is not None else {}),
                "treatments": treatments,
                "validation_status": "confirmed",
                "validated_by": reviewer,
                "validated_at": validated_at,
            }
        )
        summary_rows.append(
            {
                "branch_id": branch["id"],
                "branch_name": branch["name"],
                "area_id": area_id,
                "member_uids": branch.get("member_uids") or [],
                "treatment_ids": treatment_ids,
                **(
                    {
                        "greater_area_id": geographies["greater_area"]["id"],
                        "city_id": geographies["city"]["id"],
                    }
                    if geographies is not None
                    else {}
                ),
            }
        )

    if not 1 <= len(selected_area_ids) <= MAX_AREAS:
        raise SnapshotProductionError("Select one or two distinct branch areas")

    result = _strip_detached_classification_options(config)
    assert isinstance(result, dict)
    result.pop("evidence_purpose", None)
    result["purpose"] = SNAPSHOT_EVIDENCE_PURPOSE
    result.pop("branches", None)
    result.pop("catchment", None)
    result.pop("treatments", None)
    result["areas"] = areas
    result["platforms"] = platforms
    result.pop("github_copilot", None)
    if "github_copilot" in platforms:
        from clinic_geo.execution.copilot_settings import source_settings
        result["github_copilot"] = source_settings(selection.get("github_copilot"))
    result.update(run_mode)
    result["prompt_pack_version"] = _prompt_pack_version(config)
    if archetype is not None:
        clinic = deepcopy(dict(result.get("clinic") or {}))
        clinic["archetype"] = archetype
        result["clinic"] = clinic
    result["branch_treatment_selection"] = {
        "schema_version": (
            "free_snapshot.production_selection.v2"
            if uses_v4
            else "free_snapshot.production_selection.v1"
        ),
        "confirmed": True,
        "reviewer": reviewer,
        "validated_at": validated_at,
        **({"clinic_archetype": deepcopy(archetype)} if archetype is not None else {}),
        "branches": summary_rows,
    }
    treatment_count = sum(len(row["treatment_ids"]) for row in summary_rows)
    preview = _canonical_count_preview(result)
    summary = {
        "schema_version": "free_snapshot.production_preview.v1",
        "purpose": SNAPSHOT_EVIDENCE_PURPOSE,
        "branch_count": len(summary_rows),
        "area_count": len(selected_area_ids),
        "area_ids": [row["area_id"] for row in summary_rows],
        "branch_treatment_count": treatment_count,
        **run_mode,
        "distinct_prompt_count": preview["semantic_prompt_count"],
        **preview,
    }
    return result, summary


def preview_database_snapshot_selection(
    root: Path | str,
    base_config: Mapping,
    selection: Mapping,
) -> dict:
    """Return exact frozen-plan totals without creating a job or run."""

    selected_config, summary = build_database_selected_snapshot_config(
        root,
        base_config,
        selection,
        require_confirmation=False,
    )
    provenance = selected_config.get("branch_treatment_selection") or {}
    return {
        **summary,
        "preview_only": True,
        "report_eligible": False,
        "selection_schema_version": provenance.get("schema_version"),
    }
