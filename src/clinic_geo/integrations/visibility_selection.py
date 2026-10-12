"""Project frozen host selections into runner-owned measurement inputs.

Selection provenance and publication intent stay in the host. The runner's
public validator remains responsible for the projected measurement contract.
"""

from __future__ import annotations

import copy
from typing import Mapping

from clinic_geo.execution.measurement_platforms import (
    ACTIVE_MEASUREMENT_PLATFORMS, AVAILABLE_MEASUREMENT_PLATFORMS,
    require_active_measurement,
)
from clinic_geo.platforms import parse_platforms


RUNNER_SNAPSHOT_PLATFORMS = ACTIVE_MEASUREMENT_PLATFORMS
RUNNER_EXECUTION_POLICY_VERSION = "snapshot-module-measurement-v1"
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



def _mapping_projection(
    value: object,
    allowed: tuple[str, ...],
    *,
    field_name: str,
) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"Frozen Snapshot {field_name} must be an object")
    return {key: copy.deepcopy(value[key]) for key in allowed if key in value}


def _detached_classification_option_paths(
    value: object,
    *,
    prefix: str = "",
) -> list[str]:
    """Find selectable detached-classification knobs in a new-run payload."""

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


def _reject_detached_classification_options(value: object) -> None:
    requested = sorted(set(_detached_classification_option_paths(value)))
    if requested:
        raise ValueError(
            "New Snapshot runs do not accept detached-classification backend "
            "options; ordered-name extraction is runner-owned. Remove: "
            + ", ".join(requested)
        )


def _normalize_new_snapshot_platforms(raw: object) -> list[str]:
    try:
        platforms = parse_platforms(raw, default=RUNNER_SNAPSHOT_PLATFORMS)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "New Snapshot runs support ChatGPT, Gemini, Copilot and explicitly configured GitHub Copilot: "
            f"{exc}"
        ) from exc
    require_active_measurement({"platforms": platforms})
    unsupported = [
        platform
        for platform in platforms
        if platform not in AVAILABLE_MEASUREMENT_PLATFORMS
    ]
    if unsupported:
        raise ValueError(
            "New Snapshot runs support ChatGPT, Gemini, Copilot and explicitly configured GitHub Copilot; "
            "unsupported platform(s): " + ", ".join(unsupported)
        )
    return platforms


def measurement_from_frozen_snapshot(frozen: Mapping[str, object]) -> dict:
    """Project only runner measurement fields from one job-local selection.

    The projection preserves input order, duplicates, spelling, and values. It
    removes host-only selection/provenance metadata. Unsupported platforms and
    detached-classification options fail explicitly instead of being dropped;
    the runner's public validator remains the authority for every remaining
    measurement rule.
    """

    if not isinstance(frozen, Mapping):
        raise ValueError("Frozen Snapshot selection must be an object")
    _reject_detached_classification_options(frozen)
    if frozen.get("scope_kind") == "diagnostic":
        # Diagnostic prompts are engine-owned inputs, not a trimmed catalog
        # plan. Validate before allocating a job and keep their exact bytes.
        from clinic_geo.integrations.visibility_runner import coerce_measurement_config
        measurement = copy.deepcopy(dict(frozen))
        for key in ("runs", "test_mode"):
            measurement.pop(key, None)
        require_active_measurement(measurement)
        return coerce_measurement_config(measurement).to_mapping()
    area_scope = frozen.get("scope_kind") == "area_treatments"
    identity = {}
    if area_scope:
        if frozen.get("clinic") is not None:
            raise ValueError("Area measurements cannot contain a target clinic")
        identity["scope_kind"] = "area_treatments"
        if frozen.get("clinic_archetype") is not None:
            identity["clinic_archetype"] = _mapping_projection(
                frozen["clinic_archetype"],
                ("id", "singular", "plural", "confirmation_status"),
                field_name="clinic archetype",
            )
    else:
        clinic = _mapping_projection(
            frozen.get("clinic"),
            ("account_id", "name", "aliases", "verified_aliases", "identity_status", "archetype"),
            field_name="clinic",
        )
        if isinstance(clinic.get("archetype"), Mapping):
            clinic["archetype"] = _mapping_projection(
                clinic["archetype"],
                ("id", "singular", "plural", "confirmation_status"),
                field_name="clinic archetype",
            )
        identity["clinic"] = clinic
        if "scope_kind" in frozen:
            identity["scope_kind"] = copy.deepcopy(frozen["scope_kind"])

    raw_areas = frozen.get("areas")
    if not isinstance(raw_areas, list):
        raise ValueError("Frozen Snapshot areas must be a list")
    areas = []
    for index, raw_area in enumerate(raw_areas):
        area = _mapping_projection(
            raw_area,
            (
                "name",
                "id",
                "area_id",
                "branch_id",
                "branch_name",
                "country_code",
                "station",
                "postcode",
                "landmark",
                "variants",
                "validation_status",
                "branch_treatment_validation_status",
                "validated_by",
            ),
            field_name=f"area {index}",
        )
        source = raw_area
        assert isinstance(source, Mapping)
        if "branch" in source:
            area["branch"] = _mapping_projection(
                source["branch"],
                ("id", "name"),
                field_name=f"area {index} branch",
            )
        if "catchment" in source:
            area["catchment"] = _mapping_projection(
                source["catchment"],
                (
                    "area_id",
                    "name",
                    "area",
                    "country_code",
                    "confirmation_status",
                    "variants",
                ),
                field_name=f"area {index} catchment",
            )
        if "geographies" in source:
            raw_geographies = source["geographies"]
            if not isinstance(raw_geographies, Mapping):
                raise ValueError(
                    f"Frozen Snapshot area {index} geographies must be an object"
                )
            area["geographies"] = {
                key: _mapping_projection(
                    raw_geographies[key],
                    (
                        "id",
                        "area_id",
                        "name",
                        "area",
                        "country_code",
                        "confirmation_status",
                    ),
                    field_name=f"area {index} geography {key}",
                )
                for key in ("local", "greater_area", "city")
                if key in raw_geographies
            }
        treatments = source.get("treatments")
        if not isinstance(treatments, list):
            raise ValueError(
                f"Frozen Snapshot area {index} treatments must be a list"
            )
        area["treatments"] = [
            _mapping_projection(
                treatment,
                ("id", "name", "commercial_relevance_status"),
                field_name=f"area {index} treatment {treatment_index}",
            )
            for treatment_index, treatment in enumerate(treatments)
        ]
        areas.append(area)

    raw_platforms = frozen.get("platforms", list(RUNNER_SNAPSHOT_PLATFORMS))
    if not isinstance(raw_platforms, (list, tuple)):
        raise ValueError("Frozen Snapshot platforms must be a list")
    require_active_measurement({"platforms": raw_platforms})
    # Validate each requested value without rewriting the frozen runner input:
    # order, duplicates, and supported surface spelling remain lossless here.
    for raw_platform in raw_platforms:
        try:
            canonical = parse_platforms([raw_platform], default=())
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "New Snapshot runs support ChatGPT, Gemini, Copilot and explicitly configured GitHub Copilot: "
                f"{exc}"
            ) from exc
        unsupported = [
            platform
            for platform in canonical
            if platform not in AVAILABLE_MEASUREMENT_PLATFORMS
        ]
        if unsupported:
            raise ValueError(
                "New Snapshot runs support ChatGPT, Gemini, Copilot and explicitly configured GitHub Copilot; "
                "unsupported platform(s): " + ", ".join(unsupported)
            )
    platforms = copy.deepcopy(list(raw_platforms))
    measurement: dict[str, object] = {
        **identity,
        "areas": areas,
        "platforms": platforms,
        "outside_in": copy.deepcopy(frozen.get("outside_in", True)),
        "execution_policy_version": copy.deepcopy(
            frozen.get(
                "execution_policy_version",
                RUNNER_EXECUTION_POLICY_VERSION,
            )
        ),
        "platform_name_reconciliation": copy.deepcopy(
            frozen.get("platform_name_reconciliation", True)
        ),
    }
    for key in (
        "repetitions",
        "planned_runs",
        "prompt_catalog_version",
        "prompt_pack_version",
        "github_copilot",
    ):
        if key in frozen:
            measurement[key] = copy.deepcopy(frozen[key])
    if "github_copilot" in platforms:
        from clinic_geo.execution.copilot_settings import source_settings
        measurement["github_copilot"] = source_settings(frozen.get("github_copilot"))
    return measurement

