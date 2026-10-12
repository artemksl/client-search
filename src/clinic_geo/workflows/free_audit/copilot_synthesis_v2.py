"""Deterministic Copilot reporting data derived from the three live platforms.

Copilot has no browser worker.  This module operates only on completed
analysis-scope rows; it never creates observations, answer text, citations, or
extraction evidence.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Iterable, Mapping, Optional, Sequence

from clinic_geo.parsing.clinic_names import normalize_clinic_name

from .contracts_v2 import (
    COPILOT_DERIVED_MEASUREMENT_MODE,
    FindingVerdict,
    RecommendationClass,
    canonical_sha256,
)


COPILOT_PLATFORM = "copilot"
COPILOT_SOURCE_PLATFORMS = (
    "chatgpt",
    "gemini",
    "google_ai_overview",
)
COPILOT_MEASUREMENT_MODE = COPILOT_DERIVED_MEASUREMENT_MODE
COPILOT_DATA_ORIGIN = "synthetic_floor_average"
COPILOT_DERIVATION_POLICY_VERSION = "copilot-synthetic-floor-average-v1"

_DISTRIBUTION_KEYS = tuple(value.value for value in RecommendationClass)
_MEASURED_SOURCE_STATUSES = frozenset({"complete", "measured", "partial"})


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _floor_mean(values: Iterable[object]) -> int:
    rows = list(values)
    if len(rows) != len(COPILOT_SOURCE_PLATFORMS):
        raise ValueError("Copilot synthesis requires exactly three source values")
    return sum(_integer(value) for value in rows) // len(COPILOT_SOURCE_PLATFORMS)


def _rate(numerator: int, denominator: int) -> dict:
    numerator = max(0, min(_integer(numerator), _integer(denominator)))
    denominator = _integer(denominator)
    if not denominator:
        return {
            "numerator": numerator,
            "denominator": 0,
            "rate": None,
            "percentage": None,
            "display": "Not measured",
        }
    value = numerator / denominator
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(value, 6),
        "percentage": round(value * 100, 1),
        "display": f"{numerator}/{denominator} ({value:.1%})",
    }


def _metric(value: object, key: str) -> Mapping:
    if not isinstance(value, Mapping):
        return {}
    row = value.get(key)
    return row if isinstance(row, Mapping) else {}


def _metric_numerator(value: object, key: str) -> int:
    return _integer(_metric(value, key).get("numerator"))


def _metric_denominator(value: object, key: str = "visibility") -> int:
    return _integer(_metric(value, key).get("denominator"))


def _source_scope_unavailability_reason(scope: object) -> Optional[str]:
    """Return why a donor cannot support a synthetic Copilot result.

    Analysis creates a scope row even when a platform collected no usable
    observations.  Presence of that placeholder is not measured data: a donor
    must have a measured/partial collection state, at least one valid
    observation, and a matching non-zero clinic-metric denominator.
    """

    if not isinstance(scope, Mapping):
        return "source_scope_missing"
    collection = (
        scope.get("collection")
        if isinstance(scope.get("collection"), Mapping)
        else {}
    )
    status = str(collection.get("status") or "")
    if status not in _MEASURED_SOURCE_STATUSES:
        return f"collection_status:{status or 'missing'}"
    valid = _integer(
        collection.get("valid_observations")
        if collection.get("valid_observations") is not None
        else collection.get("valid")
    )
    if valid <= 0:
        return "no_valid_observations"
    clinic = scope.get("clinic") if isinstance(scope.get("clinic"), Mapping) else {}
    metrics = clinic.get("metrics") if isinstance(clinic.get("metrics"), Mapping) else {}
    denominator = _metric_denominator(metrics)
    if denominator <= 0:
        return "no_measured_metric_denominator"
    if denominator != valid:
        return "metric_denominator_does_not_match_valid_observations"
    return None


def _zero_metrics_for_scope(scope: Mapping) -> dict:
    clinic = scope.get("clinic") if isinstance(scope.get("clinic"), Mapping) else {}
    source = clinic.get("metrics") if isinstance(clinic.get("metrics"), Mapping) else {}
    denominator = _metric_denominator(source)
    missing = min(denominator, _integer(source.get("missing_extractions")))
    distribution = {key: 0 for key in _DISTRIBUTION_KEYS}
    distribution[RecommendationClass.NOT_NAMED.value] = denominator - missing
    return {
        "visibility": _rate(0, denominator),
        "qualified_recommendation": _rate(0, denominator),
        "recommendation_distribution": distribution,
        "missing_extractions": missing,
        "first_named": _rate(0, denominator),
        "mean_position_when_named": None,
        "named_prompt_count": 0,
        "named_run_count": 0,
        "named_intent_count": 0,
        "named_prompt_ids": [],
        "named_runs": [],
        "named_intents": [],
        "qualified_prompt_count": 0,
        "qualified_run_count": 0,
        "qualified_intent_count": 0,
        "qualified_prompt_ids": [],
        "qualified_runs": [],
        "qualified_intents": [],
    }


def synthesize_metrics(source_metrics: Sequence[Mapping]) -> dict:
    """Floor-average count-bearing metrics and rebuild every dependent field."""

    if len(source_metrics) != len(COPILOT_SOURCE_PLATFORMS):
        raise ValueError("Copilot synthesis requires all three source platforms")
    denominator = _floor_mean(
        _metric_denominator(metrics) for metrics in source_metrics
    )
    missing = min(
        denominator,
        _floor_mean(_integer(metrics.get("missing_extractions")) for metrics in source_metrics),
    )
    classified = max(0, denominator - missing)
    visibility = min(
        classified,
        _floor_mean(_metric_numerator(metrics, "visibility") for metrics in source_metrics),
    )
    qualified = min(
        visibility,
        _floor_mean(
            _metric_numerator(metrics, "qualified_recommendation")
            for metrics in source_metrics
        ),
    )
    first_named = min(
        visibility,
        _floor_mean(_metric_numerator(metrics, "first_named") for metrics in source_metrics),
    )

    def distribution_mean(key: str) -> int:
        return _floor_mean(
            _integer(
                (metrics.get("recommendation_distribution") or {}).get(key)
                if isinstance(metrics.get("recommendation_distribution"), Mapping)
                else 0
            )
            for metrics in source_metrics
        )

    # Preserve the top-level visibility and qualified counts exactly.  Flooring
    # six buckets independently can leave a small residue, assigned
    # deterministically to shortlist, neutral mention, and finally not named.
    strong = min(distribution_mean(RecommendationClass.STRONG_RECOMMENDATION.value), qualified)
    shortlisted = qualified - strong
    other_named = visibility - qualified
    unclassifiable = min(
        distribution_mean(RecommendationClass.UNCLASSIFIABLE.value),
        other_named,
    )
    classifiable_named = other_named - unclassifiable
    caveated = min(
        distribution_mean(RecommendationClass.CAVEATED_NEGATIVE.value),
        classifiable_named,
    )
    neutral = classifiable_named - caveated
    not_named = classified - visibility
    distribution = {
        RecommendationClass.STRONG_RECOMMENDATION.value: strong,
        RecommendationClass.SHORTLISTED_POSITIVE.value: shortlisted,
        RecommendationClass.NEUTRAL_MENTION.value: neutral,
        RecommendationClass.CAVEATED_NEGATIVE.value: caveated,
        RecommendationClass.NOT_NAMED.value: not_named,
        RecommendationClass.UNCLASSIFIABLE.value: unclassifiable,
    }

    positions = []
    for metrics in source_metrics:
        value = metrics.get("mean_position_when_named")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            positions.append(float(value))
    mean_position = math.floor(sum(positions) / len(positions)) if positions else None
    return {
        "visibility": _rate(visibility, denominator),
        "qualified_recommendation": _rate(qualified, denominator),
        "recommendation_distribution": distribution,
        "missing_extractions": missing,
        "first_named": _rate(first_named, denominator),
        "mean_position_when_named": mean_position,
        # A derived row has no Copilot prompt/run IDs.  Donor IDs remain on
        # their measured platform rows and are never relabelled as Copilot.
        "named_prompt_count": 0,
        "named_run_count": 0,
        "named_intent_count": 0,
        "named_prompt_ids": [],
        "named_runs": [],
        "named_intents": [],
        "qualified_prompt_count": 0,
        "qualified_run_count": 0,
        "qualified_intent_count": 0,
        "qualified_prompt_ids": [],
        "qualified_runs": [],
        "qualified_intents": [],
    }


def _scope_key(row: Mapping) -> tuple[str, str, str, str]:
    branch = row.get("branch") if isinstance(row.get("branch"), Mapping) else {}
    subject = row.get("subject") if isinstance(row.get("subject"), Mapping) else {}
    return (
        str(row.get("basket_id") or ""),
        str(branch.get("id") or branch.get("name") or ""),
        str(row.get("subject_kind") or "treatment"),
        str(subject.get("id") or subject.get("name") or ""),
    )


def _entity_name(row: Mapping) -> str:
    return str(row.get("canonical_name") or row.get("name") or "").strip()


def _entity_key(row: Mapping) -> str:
    verification = row.get("verification") if isinstance(row.get("verification"), Mapping) else {}
    resolved_id = str(verification.get("resolved_entity_id") or "").strip()
    if resolved_id:
        return f"resolved:{resolved_id}"
    name_key = normalize_clinic_name(_entity_name(row))
    if name_key:
        return f"name:{name_key}"
    return f"entity:{str(row.get('entity_id') or row.get('entity_key') or '').strip()}"


def _synthesize_entity(
    source_entities: Sequence[Optional[Mapping]],
    source_scopes: Sequence[Mapping],
) -> dict:
    present = [row for row in source_entities if isinstance(row, Mapping)]
    if not present:
        raise ValueError("Cannot synthesize an entity absent from every source")
    base = deepcopy(dict(present[0]))
    metrics = []
    for entity, scope in zip(source_entities, source_scopes):
        if isinstance(entity, Mapping) and isinstance(entity.get("metrics"), Mapping):
            metrics.append(dict(entity["metrics"]))
        else:
            metrics.append(_zero_metrics_for_scope(scope))
    derived_metrics = synthesize_metrics(metrics)
    aliases = sorted(
        {
            str(alias).strip()
            for row in present
            for alias in row.get("aliases") or []
            if str(alias).strip()
        },
        key=lambda value: (value.casefold(), value),
    )
    base.update(
        {
            "canonical_name": _entity_name(present[0]),
            "name": _entity_name(present[0]),
            "aliases": aliases,
            "metrics": derived_metrics,
            "appearance_count": derived_metrics["visibility"]["numerator"],
            "qualified_recommendation_count": derived_metrics[
                "qualified_recommendation"
            ]["numerator"],
            "first_named_count": derived_metrics["first_named"]["numerator"],
            "measurement_mode": COPILOT_MEASUREMENT_MODE,
            "data_origin": COPILOT_DATA_ORIGIN,
        }
    )
    return base


def _synthesize_competitors(source_scopes: Sequence[Mapping]) -> list[dict]:
    grouped: dict[str, dict[str, Mapping]] = {}
    for platform, scope in zip(COPILOT_SOURCE_PLATFORMS, source_scopes):
        for raw in scope.get("competitors") or []:
            if not isinstance(raw, Mapping) or not _entity_name(raw):
                continue
            grouped.setdefault(_entity_key(raw), {})[platform] = raw
    result = []
    for key in sorted(grouped):
        by_platform = grouped[key]
        entity = _synthesize_entity(
            [by_platform.get(platform) for platform in COPILOT_SOURCE_PLATFORMS],
            source_scopes,
        )
        if entity["metrics"]["visibility"]["numerator"] > 0:
            result.append(entity)
    result.sort(
        key=lambda row: (
            -_metric_numerator(row.get("metrics"), "visibility"),
            -_metric_numerator(row.get("metrics"), "qualified_recommendation"),
            -_metric_numerator(row.get("metrics"), "first_named"),
            _entity_name(row).casefold(),
            _entity_name(row),
        )
    )
    for index, row in enumerate(result[:3], 1):
        row["frozen_rank"] = index
        row["selection_status"] = "selected_derived"
    return result[:3]


def _preliminary(
    valid: int,
    appearance_gap: float,
    qualified_gap: float,
    *,
    has_competitor: bool,
    clinic_visibility: float,
    clinic_qualified: float,
) -> dict:
    if valid <= 0:
        status, label, basis = "not_measured", "Not measured", "collection"
    elif not has_competitor:
        basis = "clinic_visibility"
        best = max(clinic_visibility, clinic_qualified)
        if best >= 0.25:
            status, label = "strong", "Strong competitiveness"
        elif best <= 0:
            status, label = "weak", "Weak competitiveness"
        else:
            status, label = "balanced", "Balanced competitiveness"
    else:
        decisive = qualified_gap if abs(qualified_gap) >= 20 else appearance_gap
        basis = "qualified_recommendation" if abs(qualified_gap) >= 20 else "appearance"
        if decisive >= 20:
            status, label = "weak", "Weak competitiveness"
        elif decisive <= -20:
            status, label = "strong", "Strong competitiveness"
        else:
            status, label = "balanced", "Balanced competitiveness"
    return {
        "status": status,
        "label": label,
        "basis": basis,
        "threshold_percentage_points": 20.0,
        "preliminary": True,
    }


def _synthesize_geography_breakdown(source_scopes: Sequence[Mapping]) -> list[dict]:
    tier_order = []
    source_by_tier = []
    for scope in source_scopes:
        mapping = {}
        for row in scope.get("geography_breakdown") or []:
            if not isinstance(row, Mapping):
                continue
            tier = str(row.get("geography_tier") or "")
            mapping[tier] = row
            if tier and tier not in tier_order:
                tier_order.append(tier)
        source_by_tier.append(mapping)
    result = []
    for tier in tier_order:
        donors = [mapping.get(tier) or {} for mapping in source_by_tier]
        clinic_metrics = synthesize_metrics(
            [
                (row.get("clinic") if isinstance(row.get("clinic"), Mapping) else {})
                for row in donors
            ]
        )
        pseudo_scopes = [
            {
                "clinic": {"metrics": row.get("clinic") or {}},
                "competitors": (
                    [row["strongest_competitor"]]
                    if isinstance(row.get("strongest_competitor"), Mapping)
                    else []
                ),
            }
            for row in donors
        ]
        competitors = _synthesize_competitors(pseudo_scopes)
        base = next((row for row in donors if row), {})
        result.append(
            {
                "geography_tier": tier,
                "targets": deepcopy(list(base.get("targets") or [])),
                "planned_observations": _floor_mean(
                    row.get("planned_observations") for row in donors
                ),
                "valid_observations": _floor_mean(
                    row.get("valid_observations") for row in donors
                ),
                "clinic": clinic_metrics,
                "strongest_competitor": competitors[0] if competitors else None,
            }
        )
    return result


def _unavailable_row(
    placeholder: Mapping,
    missing: Sequence[str],
    unavailable: Optional[Mapping[str, str]] = None,
) -> dict:
    unavailable = dict(unavailable or {})
    row = deepcopy(dict(placeholder))
    collection = dict(row.get("collection") or {})
    collection["status"] = "derived_unavailable"
    collection["valid_observations"] = 0
    collection["valid"] = 0
    collection["coverage"] = 0.0
    collection["coverage_display"] = (
        f"0/{_integer(collection.get('planned_observations') or collection.get('planned'))} (0.0%)"
    )
    row.update(
        {
            "measurement_mode": "derived_floor_average_unavailable",
            "data_origin": COPILOT_DATA_ORIGIN,
            "derived_from_platforms": list(COPILOT_SOURCE_PLATFORMS),
            "collection": collection,
            "evidence_examples": [],
            "derivation": {
                "policy_version": COPILOT_DERIVATION_POLICY_VERSION,
                "rounding": "floor",
                "rounding_level": "integer_count_fields_before_rate_recalculation",
                "divisor": len(COPILOT_SOURCE_PLATFORMS),
                "missing_source_platforms": list(missing),
                "unavailable_source_platforms": list(unavailable),
                "source_unavailability_reasons": unavailable,
            },
        }
    )
    return row


def synthesize_copilot_scope(
    placeholder: Mapping,
    source_scopes: Sequence[Mapping],
) -> dict:
    if len(source_scopes) != len(COPILOT_SOURCE_PLATFORMS):
        raise ValueError("Copilot synthesis requires all three source scope rows")
    unavailable = {
        platform: reason
        for platform, scope in zip(COPILOT_SOURCE_PLATFORMS, source_scopes)
        if (reason := _source_scope_unavailability_reason(scope)) is not None
    }
    if unavailable:
        return _unavailable_row(placeholder, (), unavailable)
    clinic_sources = []
    for scope in source_scopes:
        clinic = scope.get("clinic") if isinstance(scope.get("clinic"), Mapping) else {}
        metrics = clinic.get("metrics") if isinstance(clinic.get("metrics"), Mapping) else {}
        clinic_sources.append(metrics)
    clinic_metrics = synthesize_metrics(clinic_sources)
    competitors = _synthesize_competitors(source_scopes)
    strongest = competitors[0] if competitors else None
    clinic_visibility = float(clinic_metrics["visibility"].get("rate") or 0)
    clinic_qualified = float(
        clinic_metrics["qualified_recommendation"].get("rate") or 0
    )
    competitor_visibility = float(
        ((strongest or {}).get("metrics") or {}).get("visibility", {}).get("rate") or 0
    )
    competitor_qualified = float(
        ((strongest or {}).get("metrics") or {})
        .get("qualified_recommendation", {})
        .get("rate")
        or 0
    )
    visibility_gap = round((competitor_visibility - clinic_visibility) * 100, 2)
    qualified_gap = round((competitor_qualified - clinic_qualified) * 100, 2)
    collections = [
        scope.get("collection") if isinstance(scope.get("collection"), Mapping) else {}
        for scope in source_scopes
    ]
    planned = _floor_mean(
        row.get("planned_observations") or row.get("planned") for row in collections
    )
    valid = _floor_mean(
        row.get("valid_observations") or row.get("valid") for row in collections
    )
    coverage = valid / planned if planned else 0.0
    source_payload = [
        {
            "platform": platform,
            "scope_id": scope.get("scope_id"),
            "collection": scope.get("collection"),
            "clinic": scope.get("clinic"),
            "competitors": scope.get("competitors"),
            "geography_breakdown": scope.get("geography_breakdown"),
        }
        for platform, scope in zip(COPILOT_SOURCE_PLATFORMS, source_scopes)
    ]
    source_hash = canonical_sha256(source_payload)
    row = deepcopy(dict(placeholder))
    clinic = dict(row.get("clinic") or {})
    clinic["metrics"] = clinic_metrics
    row.update(
        {
            "platform": COPILOT_PLATFORM,
            "measurement_mode": COPILOT_MEASUREMENT_MODE,
            "data_origin": COPILOT_DATA_ORIGIN,
            "derived_from_platforms": list(COPILOT_SOURCE_PLATFORMS),
            "collection": {
                "planned_observations": planned,
                "valid_observations": valid,
                "planned": planned,
                "valid": valid,
                "coverage": round(coverage, 6),
                "coverage_display": f"{valid}/{planned} ({coverage:.1%})",
                "status": "derived_floor_average",
                "status_counts": {
                    "derived_from_source_observations": valid,
                    **({"source_unavailable": planned - valid} if planned > valid else {}),
                },
                "complete_prompt_families": _floor_mean(
                    row.get("complete_prompt_families") for row in collections
                ),
                "source_statuses": {
                    platform: str(source.get("status") or "not_measured")
                    for platform, source in zip(COPILOT_SOURCE_PLATFORMS, collections)
                },
            },
            "clinic": clinic,
            "competitors": competitors,
            "strongest_competitor": strongest,
            "gaps": {
                "visibility": visibility_gap,
                "qualified_recommendation": qualified_gap,
                "appearance_percentage_points": visibility_gap,
                "qualified_percentage_points": qualified_gap,
            },
            "preliminary_competitiveness": _preliminary(
                valid,
                visibility_gap,
                qualified_gap,
                has_competitor=strongest is not None,
                clinic_visibility=clinic_visibility,
                clinic_qualified=clinic_qualified,
            ),
            "repeatability": {
                "entity": {
                    "entity_id": clinic.get("entity_id"),
                    "name": clinic.get("name"),
                },
                "prompt_ids": [],
                "runs": [],
                "intents": [],
                "prompt_count": 0,
                "run_count": 0,
                "intent_count": 0,
                "status": "derived",
                "statement": (
                    "Derived from three source-platform totals; this is not "
                    "independent Copilot evidence."
                ),
            },
            "geography_breakdown": _synthesize_geography_breakdown(source_scopes),
            "evidence": {
                "policy_version": str(
                    ((placeholder.get("evidence") or {}).get("policy_version"))
                    or "snapshot-evidence-v4"
                ),
                "passes": False,
                "gates": {
                    "derived_source_platforms": {
                        "passed": True,
                        "actual": len(source_scopes),
                        "required": len(COPILOT_SOURCE_PLATFORMS),
                        "comparator": "==",
                    }
                },
                "blockers": [
                    {
                        "code": "derived_result_not_independent_evidence",
                        "message": (
                            "Copilot is a derived floor-average result and is not "
                            "independent browser evidence."
                        ),
                        "actual": COPILOT_MEASUREMENT_MODE,
                        "required": "independent_measurement",
                    }
                ],
            },
            "verdict": FindingVerdict.INSUFFICIENT_EVIDENCE.value,
            "material_findings": [],
            "evidence_examples": [],
            "derivation": {
                "policy_version": COPILOT_DERIVATION_POLICY_VERSION,
                "source_platforms": list(COPILOT_SOURCE_PLATFORMS),
                "source_scope_ids": [str(scope.get("scope_id") or "") for scope in source_scopes],
                "divisor": len(COPILOT_SOURCE_PLATFORMS),
                "rounding": "floor",
                "rounding_level": "integer_count_fields_before_rate_recalculation",
                "source_results_sha256": source_hash,
            },
        }
    )
    row["derivation"]["derived_result_sha256"] = canonical_sha256(
        {
            "collection": row["collection"],
            "clinic": row["clinic"],
            "competitors": row["competitors"],
            "gaps": row["gaps"],
            "preliminary_competitiveness": row["preliminary_competitiveness"],
            "geography_breakdown": row["geography_breakdown"],
        }
    )
    return row


def synthesize_copilot_scopes(scopes: Sequence[Mapping]) -> list[dict]:
    """Replace every Copilot cell with its matching three-platform floor mean."""

    source_index: dict[tuple[str, str, str, str], dict[str, Mapping]] = {}
    for row in scopes:
        platform = str(row.get("platform") or "")
        if platform in COPILOT_SOURCE_PLATFORMS:
            source_index.setdefault(_scope_key(row), {})[platform] = row
    result = []
    for raw in scopes:
        row = dict(raw)
        if str(row.get("platform") or "") != COPILOT_PLATFORM:
            result.append(deepcopy(row))
            continue
        donors = source_index.get(_scope_key(row), {})
        missing = [platform for platform in COPILOT_SOURCE_PLATFORMS if platform not in donors]
        unavailable = {
            platform: reason
            for platform in COPILOT_SOURCE_PLATFORMS
            if platform in donors
            if (reason := _source_scope_unavailability_reason(donors[platform]))
            is not None
        }
        if missing or unavailable:
            result.append(_unavailable_row(row, missing, unavailable))
            continue
        result.append(
            synthesize_copilot_scope(
                row,
                [donors[platform] for platform in COPILOT_SOURCE_PLATFORMS],
            )
        )
    return result


def is_derived_copilot_scope(scope: Mapping) -> bool:
    return (
        str(scope.get("platform") or "") == COPILOT_PLATFORM
        and str(scope.get("data_origin") or "") == COPILOT_DATA_ORIGIN
    )


def scope_result_available(scope: Mapping) -> bool:
    if is_derived_copilot_scope(scope):
        return str(scope.get("measurement_mode") or "") == COPILOT_MEASUREMENT_MODE
    collection = scope.get("collection") if isinstance(scope.get("collection"), Mapping) else {}
    return str(collection.get("status") or "") in {"complete", "measured"}


__all__ = [
    "COPILOT_DATA_ORIGIN",
    "COPILOT_DERIVATION_POLICY_VERSION",
    "COPILOT_MEASUREMENT_MODE",
    "COPILOT_PLATFORM",
    "COPILOT_SOURCE_PLATFORMS",
    "is_derived_copilot_scope",
    "scope_result_available",
    "synthesize_copilot_scope",
    "synthesize_copilot_scopes",
    "synthesize_metrics",
]
