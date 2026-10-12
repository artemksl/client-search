"""Exact-scope metrics, evidence gates, competitor review, and v2 verdicts."""

from __future__ import annotations

import hmac
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Iterable, Mapping, Optional, Sequence

from clinic_geo.parsing.clinic_names import normalize_clinic_name

from .competitors_v2 import (
    COMPETITOR_REVIEW_SCHEMA_VERSION,
    build_competitor_review,
    build_competitor_validations,
)
from .copilot_synthesis_v2 import (
    COPILOT_MEASUREMENT_MODE,
    COPILOT_PLATFORM,
    is_derived_copilot_scope,
    scope_result_available,
    synthesize_copilot_scopes,
)
from .contracts_v2 import (
    ANALYSIS_V4_SCHEMA_VERSION,
    BROWSER_MEASUREMENT_MODE,
    COPY_POLICY_VERSION,
    DEFAULT_EVIDENCE_POLICY,
    EVIDENCE_POLICY_V3,
    EVIDENCE_POLICY_V4,
    EVIDENCE_POLICY_V3_VERSION,
    EVIDENCE_POLICY_V4_VERSION,
    NAMED_RECOMMENDATION_CLASSES,
    PRESENTATION_V4_SCHEMA_VERSION,
    PRESENTATION_SCHEMA_VERSION,
    PROMPT_PACK_V4_VERSION,
    QUALIFIED_RECOMMENDATION_CLASSES,
    RECOMMENDATION_RUBRIC_VERSION,
    RecommendationClass,
    EvidencePolicy,
    canonical_sha256,
)
from .observations_v2 import (
    frozen_observation_fields,
    is_valid_observation,
    validate_observation_integrity,
)
from .recommendations_v2 import validate_extraction_result
from .scope_v2 import area_scopes, assert_frozen_scope, build_observation_plan, build_prompt_pack
from .verdicts_v2 import (
    determine_scope_verdict,
    evaluate_evidence_gates,
    evaluate_factual_inconsistency,
    evaluate_material_gap,
    overall_verdict,
    select_primary_finding,
)


ANALYSIS_SCHEMA_VERSION = PRESENTATION_SCHEMA_VERSION
RESULTS_HASH_EXCLUDED_FIELDS = frozenset(
    {
        "results_hash",
        "presentation",
        "quality",
        "publication_state",
        "founder_review",
        "approval",
        "release_id",
        "copy_hash",
        "review_bundle_sha256",
        "generated_at",
        "created_at",
    }
)


class SnapshotAnalysisError(ValueError):
    pass


def _preliminary_competitiveness(
    valid_observations: int,
    appearance_gap_pp: float,
    qualified_gap_pp: float,
    *,
    has_competitor: bool,
    clinic_visibility_rate: float,
    clinic_qualified_rate: float,
) -> dict:
    """Return an easy client-facing reading without changing evidence gates.

    Gaps are competitor minus clinic. Qualified recommendation is the primary
    signal; appearance is the fallback when it alone crosses 20 points.
    """

    if valid_observations <= 0:
        status = "not_measured"
        label = "Not measured"
        basis = "collection"
    elif not has_competitor:
        basis = "clinic_visibility"
        best_clinic_rate = max(clinic_visibility_rate, clinic_qualified_rate)
        if best_clinic_rate >= 0.25:
            status, label = "strong", "Strong competitiveness"
        elif best_clinic_rate <= 0:
            status, label = "weak", "Weak competitiveness"
        else:
            status, label = "balanced", "Balanced competitiveness"
    else:
        decisive_gap = (
            qualified_gap_pp
            if abs(qualified_gap_pp) >= 20.0
            else appearance_gap_pp
        )
        basis = (
            "qualified_recommendation"
            if abs(qualified_gap_pp) >= 20.0
            else "appearance"
        )
        if decisive_gap >= 20.0:
            status, label = "weak", "Weak competitiveness"
        elif decisive_gap <= -20.0:
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


def _rows(value: object, *keys: str) -> list[dict]:
    if isinstance(value, list):
        source = value
    elif isinstance(value, Mapping):
        source = None
        for key in keys:
            if isinstance(value.get(key), list):
                source = value[key]
                break
        if source is None and all(isinstance(item, Mapping) for item in value.values()):
            source = list(value.values())
        source = source or []
    else:
        source = []
    return [dict(row) for row in source if isinstance(row, Mapping)]


def _confirmed(value: object) -> bool:
    return str(value or "").strip().casefold() in {
        "confirmed", "verified", "resolved", "relevant", "active"
    }


def _rate(numerator: int, denominator: int) -> dict:
    if not denominator:
        return {
            "numerator": int(numerator),
            "denominator": 0,
            "rate": None,
            "percentage": None,
            "display": "Not measured",
        }
    rate = numerator / denominator
    return {
        "numerator": int(numerator),
        "denominator": int(denominator),
        "rate": round(rate, 6),
        "percentage": round(rate * 100, 1),
        "display": f"{numerator}/{denominator} ({rate:.1%})",
    }


def _entity_key(row: Mapping) -> str:
    return normalize_clinic_name(row.get("canonical_name") or row.get("name"))


def _is_client(row: Mapping, clinic: Mapping) -> bool:
    if row.get("entity_id") and row.get("entity_id") == clinic.get("account_id"):
        return True
    keys = {
        normalize_clinic_name(clinic.get("name")),
        *(normalize_clinic_name(value) for value in clinic.get("verified_aliases") or []),
    } - {""}
    return _entity_key(row) in keys


def _classification_map(
    valid_observations: Sequence[Mapping],
    extractions: Mapping[str, Mapping],
    clinic: Mapping,
) -> tuple[dict[str, dict[str, dict]], dict[str, dict]]:
    by_observation: dict[str, dict[str, dict]] = {}
    entities: dict[str, dict] = {}
    for observation in valid_observations:
        observation_id = str(observation["observation_id"])
        extraction = extractions.get(observation_id) or {}
        rows = extraction.get("entities") if isinstance(extraction.get("entities"), list) else []
        mapping: dict[str, dict] = {}
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            key = "__client__" if _is_client(row, clinic) else _entity_key(row)
            if not key:
                continue
            if key == "__client__":
                row["entity_id"] = clinic["account_id"]
                row["canonical_name"] = clinic["name"]
            mapping[key] = row
            if key != "__client__":
                entities.setdefault(
                    key,
                    {
                        "entity_id": row.get("entity_id"),
                        "canonical_name": row.get("canonical_name"),
                        "aliases": [],
                    },
                )
        by_observation[observation_id] = mapping
    return by_observation, entities


def _canonicalize_competitor_classifications(
    classifications: Mapping[str, Mapping[str, Mapping]],
    entities: Mapping[str, Mapping],
    validations: object,
) -> tuple[dict[str, dict[str, dict]], dict[str, dict]]:
    """Merge database-confirmed aliases before metrics and ranking.

    The merge happens at observation level, so two spellings of one clinic in
    the same answer still count once. Unresolved candidates remain separate
    and therefore visible for founder resolution rather than being guessed
    together by name similarity.
    """

    validation_rows = _rows(validations, "candidates", "validations")
    lookup: dict[str, Mapping] = {}
    for row in validation_rows:
        for value in (
            row.get("entity_id"),
            row.get("source_entity_id"),
            row.get("canonical_name"),
            row.get("name"),
        ):
            raw = str(value or "").strip()
            normalized = normalize_clinic_name(raw)
            if raw:
                lookup[raw] = row
            if normalized:
                lookup[normalized] = row

    alias_to_group: dict[str, str] = {}
    grouped_entities: dict[str, dict] = {}
    for source_key, entity in entities.items():
        validation = (
            lookup.get(str(entity.get("entity_id") or ""))
            or lookup.get(normalize_clinic_name(entity.get("canonical_name")))
            or {}
        )
        identity = str(
            validation.get("identity_status")
            or validation.get("entity_identity")
            or ""
        ).strip().casefold()
        resolved_id = str(
            validation.get("resolved_entity_id")
            or validation.get("database_record_id")
            or ""
        ).strip()
        confirmed = identity in {"confirmed", "verified", "resolved"} and bool(resolved_id)
        group_key = f"resolved:{resolved_id}" if confirmed else source_key
        alias_to_group[source_key] = group_key
        canonical_name = str(
            validation.get("canonical_name") or entity.get("canonical_name") or ""
        ).strip()
        grouped = grouped_entities.setdefault(
            group_key,
            {
                "entity_id": resolved_id if confirmed else entity.get("entity_id"),
                "canonical_name": canonical_name,
                "aliases": [],
            },
        )
        alias = str(entity.get("canonical_name") or "").strip()
        if alias and alias.casefold() != str(grouped["canonical_name"]).casefold():
            if alias not in grouped["aliases"]:
                grouped["aliases"].append(alias)

    precedence = {
        "not_named": 0,
        "unclassifiable": 1,
        "neutral_mention": 2,
        "shortlisted_positive": 3,
        "strong_recommendation": 4,
        "caveated_negative": 5,
    }
    canonicalized: dict[str, dict[str, dict]] = {}
    for observation_id, source_rows in classifications.items():
        target_rows: dict[str, dict] = {}
        for source_key, raw in source_rows.items():
            if source_key == "__client__":
                target_rows[source_key] = dict(raw)
                continue
            group_key = alias_to_group.get(source_key, source_key)
            entity = grouped_entities.get(group_key, entities.get(source_key) or {})
            candidate = dict(raw)
            candidate["entity_id"] = entity.get("entity_id")
            candidate["canonical_name"] = entity.get("canonical_name")
            existing = target_rows.get(group_key)
            if existing is None:
                target_rows[group_key] = candidate
                continue
            existing_rank = precedence.get(str(existing.get("classification") or ""), -1)
            candidate_rank = precedence.get(str(candidate.get("classification") or ""), -1)
            existing_position = existing.get("position")
            candidate_position = candidate.get("position")
            candidate_is_earlier = (
                candidate_position is not None
                and (existing_position is None or int(candidate_position) < int(existing_position))
            )
            if candidate_rank > existing_rank or (
                candidate_rank == existing_rank and candidate_is_earlier
            ):
                candidate["first_named"] = bool(
                    candidate.get("first_named") or existing.get("first_named")
                )
                target_rows[group_key] = candidate
            else:
                existing["first_named"] = bool(
                    existing.get("first_named") or candidate.get("first_named")
                )
                if candidate_is_earlier:
                    existing["position"] = candidate_position
        canonicalized[str(observation_id)] = target_rows
    return canonicalized, grouped_entities


def _metrics_for_entity(
    entity_key: str,
    valid_observations: Sequence[Mapping],
    classifications: Mapping[str, Mapping[str, Mapping]],
    extraction_complete_ids: set[str],
) -> dict:
    denominator = len(valid_observations)
    distribution = Counter({classification.value: 0 for classification in RecommendationClass})
    named_observations = []
    qualified_observations = []
    first_named = []
    positions = []
    missing = 0
    for observation in valid_observations:
        observation_id = str(observation["observation_id"])
        if observation.get("status") == "not_shown":
            classification = RecommendationClass.NOT_NAMED.value
            row = None
        elif observation_id not in extraction_complete_ids:
            missing += 1
            continue
        else:
            row = (classifications.get(observation_id) or {}).get(entity_key)
            classification = (
                row.get("classification") if row else RecommendationClass.NOT_NAMED.value
            )
        if classification not in distribution:
            missing += 1
            continue
        distribution[classification] += 1
        if classification in NAMED_RECOMMENDATION_CLASSES:
            named_observations.append(observation)
            if row and row.get("position") is not None:
                positions.append(int(row["position"]))
            if row and row.get("first_named"):
                first_named.append(observation)
        if classification in QUALIFIED_RECOMMENDATION_CLASSES:
            qualified_observations.append(observation)

    def dimensions(rows: Sequence[Mapping]) -> dict:
        return {
            "prompt_ids": sorted({str(row["prompt_id"]) for row in rows}),
            "runs": sorted({int(row["run"]) for row in rows}),
            "intents": sorted({str(row["prompt_intent"]) for row in rows}),
        }

    named_dimensions = dimensions(named_observations)
    qualified_dimensions = dimensions(qualified_observations)
    return {
        "visibility": _rate(len(named_observations), denominator),
        "qualified_recommendation": _rate(len(qualified_observations), denominator),
        "recommendation_distribution": dict(distribution),
        "missing_extractions": missing,
        "first_named": _rate(len(first_named), denominator),
        "mean_position_when_named": (
            round(sum(positions) / len(positions), 2) if positions else None
        ),
        "named_prompt_count": len(named_dimensions["prompt_ids"]),
        "named_run_count": len(named_dimensions["runs"]),
        "named_intent_count": len(named_dimensions["intents"]),
        "named_prompt_ids": named_dimensions["prompt_ids"],
        "named_runs": named_dimensions["runs"],
        "named_intents": named_dimensions["intents"],
        "qualified_prompt_count": len(qualified_dimensions["prompt_ids"]),
        "qualified_run_count": len(qualified_dimensions["runs"]),
        "qualified_intent_count": len(qualified_dimensions["intents"]),
        "qualified_prompt_ids": qualified_dimensions["prompt_ids"],
        "qualified_runs": qualified_dimensions["runs"],
        "qualified_intents": qualified_dimensions["intents"],
    }


def _review_input(competitor_review: object, scope_id: str) -> Optional[Mapping]:
    if not isinstance(competitor_review, Mapping):
        return None
    if competitor_review.get("scope_id") == scope_id:
        return competitor_review
    scopes = competitor_review.get("scopes")
    if isinstance(scopes, Mapping):
        value = scopes.get(scope_id)
        return value if isinstance(value, Mapping) else None
    if isinstance(scopes, list):
        return next(
            (row for row in scopes if isinstance(row, Mapping) and row.get("scope_id") == scope_id),
            None,
        )
    value = competitor_review.get(scope_id)
    return value if isinstance(value, Mapping) else None


def _review_validations(review: Optional[Mapping]) -> object:
    if not review:
        return None
    if review.get("validations") is not None:
        return review.get("validations")
    rows = []
    for candidate in review.get("ranked_candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        verification = candidate.get("verification")
        if isinstance(verification, Mapping):
            rows.append(
                {
                    "entity_id": candidate.get("source_entity_id") or candidate.get("entity_id"),
                    "canonical_name": candidate.get("canonical_name"),
                    **dict(verification),
                }
            )
    return rows or review.get("candidates")


def _recurrence_for_gap(
    competitor_key: str,
    valid_observations: Sequence[Mapping],
    classifications: Mapping[str, Mapping[str, Mapping]],
    extraction_complete_ids: set[str],
) -> dict:
    result = {}
    for metric in ("visibility", "qualified_recommendation"):
        supporting = []
        for observation in valid_observations:
            observation_id = str(observation["observation_id"])
            if observation_id not in extraction_complete_ids:
                continue
            rows = classifications.get(observation_id) or {}
            client_class = (rows.get("__client__") or {}).get("classification", "not_named")
            competitor_class = (rows.get(competitor_key) or {}).get("classification", "not_named")
            if metric == "visibility":
                supports = (
                    competitor_class in NAMED_RECOMMENDATION_CLASSES
                    and client_class not in NAMED_RECOMMENDATION_CLASSES
                )
            else:
                supports = (
                    competitor_class in QUALIFIED_RECOMMENDATION_CLASSES
                    and client_class not in QUALIFIED_RECOMMENDATION_CLASSES
                )
            if supports:
                supporting.append(observation)
        result[metric] = {
            "prompt_ids": [row["prompt_id"] for row in supporting],
            "runs": [row["run"] for row in supporting],
            "observation_ids": [row["observation_id"] for row in supporting],
        }
    return result


def _example_rows(
    candidate_ids: Sequence[str],
    *,
    prompt_by_id: Mapping[str, Mapping],
    observation_by_id: Mapping[str, Mapping],
    raw_answers: Mapping[str, str],
    classifications: Mapping[str, Mapping[str, Mapping]],
    competitor_key: Optional[str],
    clinic: Mapping,
    competitor: Optional[Mapping],
    allow_raw_fallback: bool = False,
) -> list[dict]:
    ordered = [observation_by_id[value] for value in candidate_ids if value in observation_by_id]
    chosen: list[Mapping] = []
    seen_prompts: set[str] = set()
    for observation in ordered:
        if observation["prompt_id"] not in seen_prompts:
            chosen.append(observation)
            seen_prompts.add(observation["prompt_id"])
        if len(chosen) == 2:
            break
    if len(chosen) < 2:
        for observation in ordered:
            if observation not in chosen:
                chosen.append(observation)
            if len(chosen) == 2:
                break
    examples = []
    for observation in chosen:
        observation_id = str(observation["observation_id"])
        rows = classifications.get(observation_id) or {}
        client_row = rows.get("__client__") or {"classification": "not_named"}
        competitor_row = rows.get(competitor_key) if competitor_key else None
        evidence_row = competitor_row or (
            client_row if client_row.get("classification") in NAMED_RECOMMENDATION_CLASSES else None
        )
        raw = raw_answers.get(observation_id)
        if evidence_row and evidence_row.get("excerpt"):
            excerpt = str(evidence_row["excerpt"])[:160]
            start = int(evidence_row["excerpt_start"])
        elif allow_raw_fallback and raw:
            excerpt = str(raw)[:160]
            start = 0
        else:
            continue
        end = start + len(excerpt)
        if raw is None or raw[start:end] != excerpt:
            continue
        prompt = prompt_by_id.get(str(observation["prompt_id"])) or {}
        examples.append(
            {
                "observation_id": observation_id,
                "prompt_id": observation["prompt_id"],
                "prompt": prompt.get("text"),
                "prompt_intent": observation.get("prompt_intent"),
                "actual_variant": prompt.get("actual_variant"),
                "platform": observation.get("platform"),
                "model_or_surface": observation.get("model_or_surface"),
                "collected_at": observation.get("collected_at"),
                "run": observation.get("run"),
                "raw_answer_reference": observation.get("raw_answer_reference"),
                "excerpt": excerpt,
                "excerpt_start": start,
                "excerpt_end": end,
                "clinic_classification": {
                    "entity_id": clinic.get("account_id"),
                    "canonical_name": clinic.get("name"),
                    "classification": client_row.get("classification", "not_named"),
                },
                "client_classification": {
                    "entity_id": clinic.get("account_id"),
                    "canonical_name": clinic.get("name"),
                    "classification": client_row.get("classification", "not_named"),
                },
                "competitor_classification": (
                    {
                        "entity_id": competitor.get("entity_id"),
                        "canonical_name": competitor.get("canonical_name"),
                        "classification": competitor_row.get("classification", "not_named"),
                    }
                    if competitor and competitor_row
                    else None
                ),
                "competitor_classifications": (
                    [
                        {
                            "entity_id": competitor.get("entity_id"),
                            "name": competitor.get("canonical_name"),
                            "canonical_name": competitor.get("canonical_name"),
                            "classification": competitor_row.get("classification", "not_named"),
                        }
                    ]
                    if competitor and competitor_row
                    else []
                ),
            }
        )
    return examples


def _fact_rows(value: object, scope_id: str) -> list[Mapping]:
    if isinstance(value, Mapping):
        rows = value.get(scope_id)
        if rows is None and isinstance(value.get("scopes"), Mapping):
            rows = value["scopes"].get(scope_id)
    else:
        rows = None
    return [row for row in (rows or []) if isinstance(row, Mapping)]


def _v4_analysis_cells(scope: Mapping, planned: Sequence[Mapping]) -> list[dict]:
    """Return deterministic basket-platform cells from frozen observation metadata."""

    cells: dict[str, dict] = {}
    area_by_branch = {
        str((area.get("branch") or {}).get("id") or ""): area
        for area in area_scopes(scope)
    }
    for plan_order, observation in enumerate(planned):
        descriptors = observation.get("evidence_cells") or []
        if not isinstance(descriptors, (list, tuple)):
            continue
        for descriptor in descriptors:
            if not isinstance(descriptor, Mapping):
                continue
            cell_id = str(
                descriptor.get("analysis_scope_id")
                or descriptor.get("evidence_cell_id")
                or ""
            )
            if not cell_id:
                continue
            row = cells.setdefault(
                cell_id,
                {
                    **deepcopy(dict(descriptor)),
                    "scope_id": cell_id,
                    "plan_order": plan_order,
                    "observation_ids": [],
                    "target_geographies": [],
                    "geography_tiers": [],
                },
            )
            observation_id = str(observation.get("observation_id") or "")
            if observation_id and observation_id not in row["observation_ids"]:
                row["observation_ids"].append(observation_id)
            target = observation.get("target_geography")
            if isinstance(target, Mapping):
                target_row = deepcopy(dict(target))
                target_key = (
                    str(target_row.get("id") or target_row.get("area_id") or ""),
                    str(target_row.get("name") or target_row.get("area") or ""),
                )
                existing = {
                    (
                        str(value.get("id") or value.get("area_id") or ""),
                        str(value.get("name") or value.get("area") or ""),
                    )
                    for value in row["target_geographies"]
                    if isinstance(value, Mapping)
                }
                if target_key not in existing:
                    row["target_geographies"].append(target_row)
            tier = str(observation.get("geography_tier") or "")
            if tier and tier not in row["geography_tiers"]:
                row["geography_tiers"].append(tier)

    result: list[dict] = []
    for row in cells.values():
        area_scope = area_by_branch.get(str(row.get("origin_branch_id") or ""))
        if not isinstance(area_scope, Mapping):
            raise SnapshotAnalysisError(
                f"v4 evidence cell {row['scope_id']} references an unknown origin branch"
            )
        row["area_scope"] = area_scope
        if row.get("subject_kind") == "clinic_archetype":
            archetype = (scope.get("clinic") or {}).get("archetype") or {}
            row["subject"] = {
                "id": archetype.get("id"),
                "name": archetype.get("singular"),
                "singular": archetype.get("singular"),
                "plural": archetype.get("plural"),
                "confirmation_status": archetype.get("confirmation_status"),
            }
            row["treatment"] = row["subject"]
        else:
            treatment_id = str(row.get("treatment_id") or row.get("subject_id") or "")
            treatment = next(
                (
                    value
                    for value in area_scope.get("treatments") or []
                    if isinstance(value, Mapping) and str(value.get("id")) == treatment_id
                ),
                None,
            )
            if treatment is None:
                raise SnapshotAnalysisError(
                    f"v4 evidence cell {row['scope_id']} references an unknown treatment"
                )
            row["subject"] = treatment
            row["treatment"] = treatment
        result.append(row)

    basket_order = {
        "treatment_local": 0,
        "treatment_expanded": 1,
        "clinic_archetype": 2,
    }
    platform_order = {str(value): index for index, value in enumerate(scope.get("platforms") or [])}
    result.sort(
        key=lambda row: (
            int(row.get("plan_order") or 0),
            basket_order.get(str(row.get("basket_id") or ""), 99),
            platform_order.get(str(row.get("platform") or ""), 99),
            str(row.get("scope_id") or ""),
        )
    )
    return result


def _policy_for_scope(scope: Mapping, supplied: Optional[EvidencePolicy]) -> EvidencePolicy:
    expected = str(scope.get("evidence_policy_version") or "")
    if supplied is not None:
        if expected and supplied.version != expected:
            raise SnapshotAnalysisError(
                f"evidence policy {supplied.version!r} does not match frozen {expected!r}"
            )
        return supplied
    if expected == EVIDENCE_POLICY_V3_VERSION:
        return EVIDENCE_POLICY_V3
    if expected == EVIDENCE_POLICY_V4_VERSION:
        return EVIDENCE_POLICY_V4
    raise SnapshotAnalysisError(f"unsupported frozen evidence policy {expected!r}")


def _scope_analysis(
    scope: Mapping,
    prompt_pack: Mapping,
    planned: Sequence[Mapping],
    observations: Mapping[str, Mapping],
    extractions: Mapping[str, Mapping],
    *,
    area_scope: Mapping,
    treatment: Mapping,
    platform: str,
    order: int,
    competitor_review: object,
    raw_answers: Mapping[str, str],
    clinic_database: object,
    external_evidence: object,
    factual_inconsistencies: object,
    policy: EvidencePolicy,
    cell: Optional[Mapping] = None,
) -> tuple[dict, dict]:
    catchment = area_scope["catchment"]
    branch = area_scope.get("branch") or {}
    subject_kind = str((cell or {}).get("subject_kind") or "treatment")
    basket_id = str((cell or {}).get("basket_id") or "")
    scope_id = str((cell or {}).get("scope_id") or "") or (
        f"{treatment['id']}:{catchment['area_id']}:{platform}"
    )
    if cell is not None:
        cell_observation_ids = {
            str(value) for value in cell.get("observation_ids") or [] if str(value)
        }
        scope_plan = [
            row
            for row in planned
            if str(row.get("observation_id") or "") in cell_observation_ids
        ]
    else:
        scope_plan = [row for row in planned if row["scope_id"] == scope_id]
    target_geographies = [
        deepcopy(dict(value))
        for value in (cell or {}).get("target_geographies") or []
        if isinstance(value, Mapping)
    ]
    if not target_geographies:
        target_geographies = [deepcopy(dict(catchment))]
    plan_by_id = {str(row["observation_id"]): row for row in scope_plan}
    actual_rows = []
    status_counts = Counter()
    structural_errors = []
    for plan_row in scope_plan:
        observation_id = str(plan_row["observation_id"])
        actual = observations.get(observation_id)
        if actual is None:
            status_counts["missing"] += 1
            continue
        mismatches = [
            field
            for field in frozen_observation_fields(plan_row)
            if actual.get(field) != plan_row.get(field)
        ]
        if mismatches:
            status_counts["invalid"] += 1
            structural_errors.append({"observation_id": observation_id, "fields": mismatches})
            continue
        status_counts[str(actual.get("status") or "invalid")] += 1
        if is_valid_observation(actual):
            actual_rows.append(actual)

    valid_ids = {str(row["observation_id"]) for row in actual_rows}
    raw_integrity: dict[str, list[str]] = {}
    extraction_errors: dict[str, list[str]] = {}
    extraction_complete_ids: set[str] = set()
    clinic_known = [scope["clinic"]]
    for observation in actual_rows:
        observation_id = str(observation["observation_id"])
        raw = raw_answers.get(observation_id)
        raw_integrity[observation_id] = validate_observation_integrity(
            observation, raw_answer=raw
        )
        if observation.get("status") == "not_shown":
            extraction_complete_ids.add(observation_id)
            continue
        extraction = extractions.get(observation_id)
        if not extraction:
            extraction_errors[observation_id] = ["missing_extraction"]
            continue
        if extraction.get("raw_answer_sha256") != observation.get("raw_answer_sha256"):
            extraction_errors[observation_id] = ["extraction_observation_raw_hash_mismatch"]
            continue
        if raw is None:
            extraction_errors[observation_id] = ["raw_answer_not_provided"]
            continue
        errors = validate_extraction_result(
            extraction, observation, raw, known_entities=clinic_known
        )
        if extraction.get("status") != "complete" or extraction.get("release_eligible") is False:
            errors.append("extraction_not_release_eligible")
        if errors:
            extraction_errors[observation_id] = errors
        else:
            extraction_complete_ids.add(observation_id)

    scope_context = {
        **dict(scope),
        "catchment": catchment,
        "target_geography": (
            target_geographies[0] if len(target_geographies) == 1 else None
        ),
        "target_geographies": target_geographies,
        "treatments": area_scope.get("treatments") or [],
        "branch": branch,
        "scope_id": scope_id,
        "subject_kind": subject_kind,
        "treatment_id": treatment.get("id") if subject_kind == "treatment" else None,
        "treatment": treatment if subject_kind == "treatment" else None,
        "clinic_archetype": (
            treatment if subject_kind == "clinic_archetype" else None
        ),
    }
    if isinstance(external_evidence, Mapping):
        external_scope_evidence = external_evidence.get(scope_id)
        if external_scope_evidence is None and cell is not None:
            merged_external: dict = {}
            for collection_scope_id in {
                str(row.get("scope_id") or "") for row in scope_plan
            }:
                value = external_evidence.get(collection_scope_id)
                if isinstance(value, Mapping):
                    merged_external.update(deepcopy(dict(value)))
            external_scope_evidence = merged_external or None
    else:
        external_scope_evidence = external_evidence
    prior_review = _review_input(competitor_review, scope_id)
    prior_validations = _review_validations(prior_review)
    classifications, entities = _classification_map(
        actual_rows, extractions, scope["clinic"]
    )
    canonicalization_validations = prior_validations
    if canonicalization_validations is None and clinic_database is not None:
        canonicalization_validations = build_competitor_validations(
            scope_context,
            entities.values(),
            clinic_database,
            external_evidence=external_scope_evidence,
        )
    if canonicalization_validations is not None:
        classifications, entities = _canonicalize_competitor_classifications(
            classifications,
            entities,
            canonicalization_validations,
        )
    client_metrics = _metrics_for_entity(
        "__client__", actual_rows, classifications, extraction_complete_ids
    )
    candidate_rows = []
    for key, entity in entities.items():
        metrics = _metrics_for_entity(key, actual_rows, classifications, extraction_complete_ids)
        if metrics["visibility"]["numerator"]:
            candidate_rows.append({**entity, "entity_key": key, "metrics": metrics})

    validations = prior_validations
    if validations is None and clinic_database is not None:
        validations = build_competitor_validations(
            scope_context,
            candidate_rows,
            clinic_database,
            external_evidence=external_scope_evidence,
        )
    review = build_competitor_review(
        scope_id,
        candidate_rows,
        clinic=scope["clinic"],
        validations=validations,
    )
    if cell is not None:
        # v4 scope IDs are opaque. Persist the measured subject so founder
        # resolution never guesses "treatment" from an identifier.
        review.update(
            {
                "subject_kind": subject_kind,
                "subject": deepcopy(dict(treatment)),
                "basket_id": basket_id or None,
            }
        )
    selected = review["selected_competitors"]
    strongest = selected[0] if selected else None
    strongest_key = strongest.get("entity_key") if strongest else None

    pattern_metrics = client_metrics
    pattern_entity = {"entity_id": scope["clinic"]["account_id"], "name": scope["clinic"]["name"]}
    for candidate in selected:
        if candidate["metrics"]["visibility"]["numerator"] > pattern_metrics["visibility"]["numerator"]:
            pattern_metrics = candidate["metrics"]
            pattern_entity = {"entity_id": candidate["entity_id"], "name": candidate["canonical_name"]}
    pattern_prompt_ids = pattern_metrics["named_prompt_ids"]
    pattern_runs = pattern_metrics["named_runs"]

    factual_findings = [
        evaluate_factual_inconsistency(row, observations, policy=policy)
        for row in _fact_rows(factual_inconsistencies, scope_id)
    ]
    material_fact = next((row for row in factual_findings if row["material"]), None)
    if material_fact:
        pattern_prompt_ids = material_fact["supporting_prompt_ids"]
        pattern_runs = material_fact["supporting_runs"]

    family_runs = defaultdict(set)
    for observation in actual_rows:
        family_runs[str(observation["prompt_id"])].add(int(observation["run"]))
    complete_families = sum(
        1 for runs in family_runs.values()
        if len(runs) >= policy.required_runs_per_prompt_family
    )
    raw_consistent = not structural_errors and all(not errors for errors in raw_integrity.values())
    extraction_complete = (
        len(extraction_complete_ids) == len(actual_rows)
        and not extraction_errors
    )
    evidence = evaluate_evidence_gates(
        complete_prompt_families=complete_families,
        valid_observations=len(actual_rows),
        planned_observations=len(scope_plan),
        pattern_prompt_ids=len(set(pattern_prompt_ids)),
        pattern_runs=len(set(pattern_runs)),
        clinic_confirmed=_confirmed(scope["clinic"].get("identity_status")),
        treatment_confirmed=(
            _confirmed(treatment.get("commercial_relevance_status"))
            if subject_kind == "treatment"
            else _confirmed(treatment.get("confirmation_status"))
        ),
        catchment_confirmed=(
            _confirmed(catchment.get("confirmation_status"))
            and _confirmed(area_scope.get("validation_status"))
            and all(
                _confirmed(value.get("confirmation_status"))
                for value in target_geographies
            )
        ),
        comparator_identity_resolved=review["relevant_comparator_identity_resolved"],
        geographic_treatment_relevance_confirmed=review[
            "geographic_subject_relevance_confirmed"
            if subject_kind == "clinic_archetype"
            else "geographic_treatment_relevance_confirmed"
        ],
        extraction_complete=extraction_complete,
        raw_evidence_consistent=raw_consistent,
        subject_kind=subject_kind,
        policy=policy,
    )
    if structural_errors:
        evidence["structural_errors"] = structural_errors
    if extraction_errors:
        evidence["extraction_errors"] = extraction_errors
    if any(raw_integrity.values()):
        evidence["raw_integrity_errors"] = {
            key: value for key, value in raw_integrity.items() if value
        }

    material_findings = []
    for candidate in selected:
        recurrence = _recurrence_for_gap(
            candidate["entity_key"], actual_rows, classifications, extraction_complete_ids
        )
        finding = evaluate_material_gap(
            client_metrics,
            candidate["metrics"],
            recurrence=recurrence,
            policy=policy,
        )
        finding.update(
            {
                "finding_type": "competitor_gap",
                "competitor_id": candidate["entity_id"],
                "competitor_name": candidate["canonical_name"],
            }
        )
        material_findings.append(finding)
    material_findings.extend(factual_findings)
    meaningful_competitor_appearances = sum(
        row["metrics"]["visibility"]["numerator"] for row in selected
    )
    verdict = determine_scope_verdict(
        evidence,
        clinic_appearances=client_metrics["visibility"]["numerator"],
        meaningful_competitor_appearances=meaningful_competitor_appearances,
        material_findings=material_findings,
    )

    visibility_gap = (
        strongest["metrics"]["visibility"]["rate"] - client_metrics["visibility"]["rate"]
        if strongest else 0.0
    )
    qualified_gap = (
        strongest["metrics"]["qualified_recommendation"]["rate"]
        - client_metrics["qualified_recommendation"]["rate"]
        if strongest else 0.0
    )
    support_ids = (
        list(material_fact.get("supporting_observation_ids") or [])
        if material_fact
        else []
    )
    if strongest and not support_ids:
        strongest_finding = next(
            (row for row in material_findings if row.get("competitor_id") == strongest["entity_id"]),
            None,
        )
        if strongest_finding and strongest_finding.get("winning_metric"):
            support_ids = strongest_finding["metrics"][strongest_finding["winning_metric"]][
                "supporting_observation_ids"
            ]
    if not support_ids:
        support_ids = [row["observation_id"] for row in actual_rows]
    prompt_by_id = {
        str(row["prompt_id"]): row for row in prompt_pack.get("prompts") or []
    }
    examples = _example_rows(
        support_ids,
        prompt_by_id=prompt_by_id,
        observation_by_id={str(row["observation_id"]): row for row in actual_rows},
        raw_answers=raw_answers,
        classifications=classifications,
        competitor_key=strongest_key,
        clinic=scope["clinic"],
        competitor=strongest,
        allow_raw_fallback=bool(material_fact) or verdict == "insufficient_evidence",
    )
    coverage = len(actual_rows) / len(scope_plan) if scope_plan else 0.0
    collection_status = (
        "complete" if len(actual_rows) == len(scope_plan)
        else "measured" if len(actual_rows) >= policy.min_valid_observations and coverage >= policy.min_coverage
        else "partial" if actual_rows else "not_measured"
    )
    preliminary = _preliminary_competitiveness(
        len(actual_rows),
        round(visibility_gap * 100, 2),
        round(qualified_gap * 100, 2),
        has_competitor=strongest is not None,
        clinic_visibility_rate=float(client_metrics["visibility"].get("rate") or 0),
        clinic_qualified_rate=float(
            client_metrics["qualified_recommendation"].get("rate") or 0
        ),
    )
    geography_breakdown = []
    tier_order = {"local": 0, "greater_area": 1, "city": 2}
    tiers = sorted(
        {
            str(row.get("geography_tier") or "")
            for row in scope_plan
            if str(row.get("geography_tier") or "")
        },
        key=lambda value: (tier_order.get(value, 99), value),
    )
    for tier in tiers:
        tier_plan = [row for row in scope_plan if row.get("geography_tier") == tier]
        tier_ids = {str(row.get("observation_id") or "") for row in tier_plan}
        tier_actual = [
            row for row in actual_rows if str(row.get("observation_id") or "") in tier_ids
        ]
        tier_extraction_ids = extraction_complete_ids & tier_ids
        tier_client = _metrics_for_entity(
            "__client__", tier_actual, classifications, tier_extraction_ids
        )
        tier_competitors = []
        for candidate in selected:
            candidate_key = str(candidate.get("entity_key") or "")
            metrics = _metrics_for_entity(
                candidate_key, tier_actual, classifications, tier_extraction_ids
            )
            if metrics["visibility"]["numerator"]:
                tier_competitors.append(
                    {
                        "entity_id": candidate.get("entity_id"),
                        "name": candidate.get("canonical_name"),
                        "metrics": metrics,
                    }
                )
        tier_competitors.sort(
            key=lambda row: (
                -int(row["metrics"]["visibility"]["numerator"]),
                str(row.get("name") or "").casefold(),
            )
        )
        geography_breakdown.append(
            {
                "geography_tier": tier,
                "targets": [
                    deepcopy(dict(value))
                    for value in target_geographies
                    if any(
                        row.get("geography_tier") == tier
                        and (
                            (row.get("target_geography") or {}).get("id")
                            == value.get("id")
                        )
                        for row in tier_plan
                        if isinstance(row.get("target_geography"), Mapping)
                    )
                ],
                "planned_observations": len(tier_plan),
                "valid_observations": len(tier_actual),
                "clinic": tier_client,
                "strongest_competitor": tier_competitors[0] if tier_competitors else None,
            }
        )
    scope_row = {
        "scope_id": scope_id,
        "order": order,
        "basket_id": basket_id or None,
        "basket": (
            {
                "id": basket_id,
                "label": {
                    "treatment_local": "Treatment in the local area",
                    "treatment_expanded": "Treatment across the wider market",
                    "clinic_archetype": "General clinic-profile discovery",
                }.get(basket_id, basket_id.replace("_", " ").title()),
            }
            if basket_id
            else None
        ),
        "subject_kind": subject_kind,
        "subject": deepcopy(dict(treatment)),
        "treatment": (
            {
                key: treatment.get(key)
                for key in (
                    "id",
                    "name",
                    "commercial_relevance_status",
                )
                if treatment.get(key) is not None
            }
            if subject_kind == "treatment"
            else None
        ),
        "branch": {key: branch.get(key) for key in ("id", "name")},
        "origin_catchment": {
            key: catchment.get(key)
            for key in ("area_id", "name", "country_code", "confirmation_status")
        },
        "target_geographies": target_geographies,
        "geography_tiers": list((cell or {}).get("geography_tiers") or []),
        "geography_breakdown": geography_breakdown,
        "catchment": {
            **{
                key: catchment.get(key)
                for key in ("area_id", "name", "country_code", "confirmation_status")
            },
            "measured_targets": target_geographies,
        },
        "platform": platform,
        "collection": {
            "planned_observations": len(scope_plan),
            "valid_observations": len(actual_rows),
            "planned": len(scope_plan),
            "valid": len(actual_rows),
            "coverage": round(coverage, 6),
            "coverage_display": f"{len(actual_rows)}/{len(scope_plan)} ({coverage:.1%})",
            "status": collection_status,
            "status_counts": dict(sorted(status_counts.items())),
            "complete_prompt_families": complete_families,
        },
        "clinic": {
            "entity_id": scope["clinic"]["account_id"],
            "name": scope["clinic"]["name"],
            "metrics": client_metrics,
        },
        "competitors": selected,
        "strongest_competitor": strongest,
        "gaps": {
            "visibility": round(visibility_gap * 100, 2),
            "qualified_recommendation": round(qualified_gap * 100, 2),
            "appearance_percentage_points": round(visibility_gap * 100, 2),
            "qualified_percentage_points": round(qualified_gap * 100, 2),
        },
        "preliminary_competitiveness": preliminary,
        "repeatability": {
            "entity": pattern_entity,
            "prompt_ids": sorted(set(pattern_prompt_ids)),
            "runs": sorted(set(pattern_runs)),
            "intents": pattern_metrics.get("named_intents") or [],
            "prompt_count": len(set(pattern_prompt_ids)),
            "run_count": len(set(pattern_runs)),
            "intent_count": len(set(pattern_metrics.get("named_intents") or [])),
            "status": (
                "repeated"
                if len(set(pattern_prompt_ids)) >= policy.min_pattern_prompt_ids
                and len(set(pattern_runs)) >= policy.min_pattern_runs
                else "insufficient"
            ),
            "statement": (
                f"Repeated across {len(set(pattern_prompt_ids))} prompt formulations and "
                f"{len(set(pattern_runs))} independent runs."
                if len(set(pattern_prompt_ids)) >= policy.min_pattern_prompt_ids
                and len(set(pattern_runs)) >= policy.min_pattern_runs
                else "Repeatability was not established from the available evidence."
            ),
        },
        "evidence": evidence,
        "verdict": verdict,
        "material_findings": material_findings,
        "evidence_examples": examples[:2],
    }
    return scope_row, review


def _next_investigations(verdict: str, primary: Optional[Mapping]) -> list[dict]:
    if verdict == "Material opportunity demonstrated":
        return [
            {
                "id": "diagnostic-audit",
                "title": "Run a scoped Diagnostic Audit",
                "description": "Investigate the repeated visibility or recommendation gap without claiming its cause.",
                "recommended": True,
                "category": "Scoped Diagnostic Audit",
                "question_to_answer": "What verified public evidence and technical conditions contribute to the repeated answer-side gap?",
                "why_required": "The Snapshot demonstrates the repeated gap but does not establish its cause.",
            }
        ]
    if verdict == "More evidence required":
        collection = (primary or {}).get("collection") or {}
        if int(collection.get("valid_observations") or collection.get("valid") or 0) < 12:
            platform = str((primary or {}).get("platform") or "included platform")
            label = {
                "chatgpt": "ChatGPT",
                "gemini": "Gemini",
                "google_ai_overview": "Google AI Overview",
                "copilot": "Copilot",
            }.get(platform, platform.replace("_", " ").title())
            reason = str(
                (primary or {}).get("headline_missing_evidence")
                or "The included platform does not have enough valid observations."
            )
            return [
                {
                    "id": "complete-platform-collection",
                    "title": f"Complete {label} collection",
                    "description": reason,
                    "recommended": False,
                    "category": "Evidence completion",
                    "question_to_answer": f"Can the missing {label} observations be collected?",
                    "why_required": reason,
                }
            ]
        blockers = ((primary or {}).get("evidence") or {}).get("blockers") or []
        investigation_copy = {
            "complete_prompt_families": (
                "Complete prompt-family runs",
                "Can at least four prompt families be completed across all three runs?",
            ),
            "valid_observations": (
                "Complete valid observations",
                "Can at least 12 valid observations be collected for this scope?",
            ),
            "coverage": (
                "Complete scope coverage",
                "Can valid observation coverage reach at least 80%?",
            ),
            "pattern_prompt_ids": (
                "Confirm prompt repeatability",
                "Does the measured pattern recur across at least two prompt IDs?",
            ),
            "pattern_runs": (
                "Confirm run repeatability",
                "Does the measured pattern recur across at least two independent runs?",
            ),
            "relevant_comparator_identity_resolved": (
                "Verify comparator identities",
                "Which relevant competitor identities can be verified?",
            ),
            "geographic_treatment_relevance_confirmed": (
                "Verify competitor relevance",
                "Which candidate competitors serve this catchment and treatment?",
            ),
            "geographic_subject_relevance_confirmed": (
                "Verify clinic-profile competitor relevance",
                "Which candidate clinics serve the measured geography and match the confirmed clinic profile?",
            ),
            "recommendation_extraction_complete": (
                "Complete recommendation extraction",
                "Can every valid answer receive a verified recommendation classification?",
            ),
            "raw_evidence_hashes_consistent": (
                "Restore evidence integrity",
                "Can every stored answer and extraction be reconciled to its recorded hash?",
            ),
        }
        return [
            {
                "id": row.get("code"),
                "title": investigation_copy.get(
                    str(row.get("code") or ""),
                    (f"Resolve {str(row.get('code') or '').replace('_', ' ')}", ""),
                )[0],
                "description": row.get("message"),
                "recommended": False,
                "category": "Evidence completion",
                "question_to_answer": investigation_copy.get(
                    str(row.get("code") or ""),
                    ("", f"Can the {str(row.get('code') or '').replace('_', ' ')} evidence condition be resolved?"),
                )[1],
                "why_required": row.get("message"),
            }
            for row in blockers[:4]
        ]
    return [
        {
            "id": "no-audit-recommended",
            "title": "No Diagnostic Audit recommended",
            "description": "The bounded evidence does not currently demonstrate a strong opportunity.",
            "recommended": False,
            "category": "No further investigation",
            "question_to_answer": "Is a Diagnostic Audit warranted on this evidence?",
            "why_required": "No strong opportunity is currently visible within the frozen scope.",
        }
    ]


def compute_results_hash(analysis: Mapping) -> str:
    """Hash result-bearing fields, excluding presentation/publication review state."""

    payload = deepcopy(dict(analysis))
    for field in RESULTS_HASH_EXCLUDED_FIELDS:
        payload.pop(field, None)
    return canonical_sha256(payload)


def verify_results_hash(analysis: Mapping) -> bool:
    declared = str(analysis.get("results_hash") or "")
    return bool(declared) and hmac.compare_digest(declared, compute_results_hash(analysis))


def analyze_scopes(
    scope: Mapping,
    prompt_pack: Mapping,
    observations: object,
    extraction_results: object,
    *,
    competitor_review: object = None,
    raw_answers: Optional[Mapping[str, str]] = None,
    clinic_database: object = None,
    external_evidence: object = None,
    factual_inconsistencies: object = None,
    evidence_policy: Optional[EvidencePolicy] = None,
) -> dict:
    """Analyze exact frozen evidence cells with legacy v2/v3 dispatch."""

    assert_frozen_scope(scope)
    expected_pack = build_prompt_pack(scope)
    if prompt_pack.get("prompt_pack_hash") != expected_pack["prompt_pack_hash"]:
        raise SnapshotAnalysisError("prompt pack hash does not match scope")
    if list(prompt_pack.get("prompts") or []) != expected_pack["prompts"]:
        raise SnapshotAnalysisError("prompt pack content does not match its frozen hash")
    plan = build_observation_plan(scope, prompt_pack)
    evidence_policy = _policy_for_scope(scope, evidence_policy)
    observation_rows = _rows(observations, "observations", "results")
    extraction_rows = _rows(extraction_results, "extraction_results", "results", "extractions")
    observations_by_id = {
        str(row["observation_id"]): row for row in observation_rows if row.get("observation_id")
    }
    extractions_by_id = {
        str(row["observation_id"]): row for row in extraction_rows if row.get("observation_id")
    }
    raw_answers = dict(raw_answers or {})
    for observation in observation_rows:
        observation_id = str(observation.get("observation_id") or "")
        if observation_id and observation_id not in raw_answers:
            inline = observation.get("raw_answer_text")
            if isinstance(inline, str):
                raw_answers[observation_id] = inline

    scopes = []
    reviews = []
    order = 0
    selected_areas = area_scopes(scope)
    if scope.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION:
        cells = _v4_analysis_cells(scope, plan["observations"])
        for cell in cells:
            order += 1
            row, review = _scope_analysis(
                scope,
                prompt_pack,
                plan["observations"],
                observations_by_id,
                extractions_by_id,
                area_scope=cell["area_scope"],
                treatment=cell["treatment"],
                platform=str(cell.get("platform") or ""),
                order=order,
                competitor_review=competitor_review,
                raw_answers=raw_answers,
                clinic_database=clinic_database,
                external_evidence=external_evidence,
                factual_inconsistencies=factual_inconsistencies,
                policy=evidence_policy,
                cell=cell,
            )
            scopes.append(row)
            reviews.append(review)
    else:
        for area_scope in selected_areas:
            for treatment in area_scope["treatments"]:
                for platform in scope["platforms"]:
                    order += 1
                    row, review = _scope_analysis(
                        scope,
                        prompt_pack,
                        plan["observations"],
                        observations_by_id,
                        extractions_by_id,
                        area_scope=area_scope,
                        treatment=treatment,
                        platform=platform,
                        order=order,
                        competitor_review=competitor_review,
                        raw_answers=raw_answers,
                        clinic_database=clinic_database,
                        external_evidence=external_evidence,
                        factual_inconsistencies=factual_inconsistencies,
                        policy=evidence_policy,
                    )
                    scopes.append(row)
                    reviews.append(review)

    frozen_data_modes = (
        dict(scope.get("platform_data_modes") or {})
        if isinstance(scope.get("platform_data_modes"), Mapping)
        else {}
    )
    effective_data_modes = {
        platform: str(
            frozen_data_modes.get(platform) or BROWSER_MEASUREMENT_MODE
        )
        for platform in scope["platforms"]
    }
    copilot_is_derived = (
        effective_data_modes.get(COPILOT_PLATFORM) == COPILOT_MEASUREMENT_MODE
    )
    if copilot_is_derived:
        scopes = synthesize_copilot_scopes(scopes)
    derived_by_id = {
        str(row.get("scope_id") or ""): row
        for row in scopes
        if is_derived_copilot_scope(row)
    }
    for review in reviews:
        derived = derived_by_id.get(str(review.get("scope_id") or ""))
        if not derived:
            continue
        review.update(
            {
                "measurement_mode": derived.get("measurement_mode"),
                "data_origin": derived.get("data_origin"),
                "derivation": deepcopy(derived.get("derivation") or {}),
                "selected_competitors": deepcopy(derived.get("competitors") or []),
                "ranked_candidates": deepcopy(derived.get("competitors") or []),
                "blocking_unresolved_candidates": [],
                "unresolved_exceptions": [],
            }
        )

    # Synthetic values remain visible platform data but are never treated as
    # an independent observation source. Legacy scopes with browser-measured
    # Copilot retain all of their original independent evidence.
    independent_scopes = [row for row in scopes if not is_derived_copilot_scope(row)]
    verdict = overall_verdict(independent_scopes)
    primary = select_primary_finding(independent_scopes)
    if primary and primary.get("verdict") == "insufficient_evidence":
        missing_references = deepcopy(
            (primary.get("evidence") or {}).get("blockers") or []
        )
        no_meaningful_entities = (
            ((primary.get("clinic") or {}).get("metrics") or {})
            .get("visibility", {})
            .get("numerator", 0) == 0
            and not primary.get("strongest_competitor")
        )
        if not missing_references or no_meaningful_entities:
            primary_scope_id = str(primary.get("scope_id") or "")
            measured = [
                {
                    "observation_id": row.get("observation_id"),
                    "prompt_id": row.get("prompt_id"),
                    "platform": row.get("platform"),
                    "collected_at": row.get("collected_at"),
                    "run": row.get("run"),
                    "status": row.get("status"),
                    "raw_answer_reference": row.get("raw_answer_reference"),
                }
                for row in observation_rows
                if (
                    row.get("scope_id") == primary_scope_id
                    or primary_scope_id
                    in {
                        str(cell.get("analysis_scope_id") or "")
                        for cell in row.get("evidence_cells") or []
                        if isinstance(cell, Mapping)
                    }
                )
                and is_valid_observation(row)
            ]
            if not any(
                row.get("code") == "no_meaningful_entities_appeared"
                for row in missing_references
            ):
                missing_references.append(
                    {
                        "code": "no_meaningful_entities_appeared",
                        "message": (
                            "Neither the clinic nor a confirmed meaningful competitor appeared "
                            "in the complete frozen observations."
                        ),
                        "scope_id": primary_scope_id,
                        "evidence": measured,
                    }
                )
        primary["missing_evidence_references"] = missing_references
        primary_collection = primary.get("collection") or {}
        primary_valid = int(
            primary_collection.get("valid_observations")
            or primary_collection.get("valid")
            or 0
        )
        if primary_valid < evidence_policy.min_valid_observations:
            platform_key = str(primary.get("platform") or "included platform")
            platform_label = {
                "chatgpt": "ChatGPT",
                "gemini": "Gemini",
                "google_ai_overview": "Google AI Overview",
                "copilot": "Copilot",
            }.get(platform_key, platform_key.replace("_", " ").title())
            planned = int(primary_collection.get("planned_observations") or 0)
            status_counts = primary_collection.get("status_counts") or {}
            if status_counts.get("blocked") == planned and planned:
                headline_missing = (
                    f"{platform_label} could not be measured because all {planned} "
                    "planned observations were blocked."
                )
            else:
                headline_missing = (
                    f"{platform_label} has {primary_valid}/{planned} valid observations; "
                    f"at least {evidence_policy.min_valid_observations} are required."
                )
        else:
            headline_missing = str(
                (missing_references[0] if missing_references else {}).get("message")
                or "The measured pattern did not recur across enough prompts and runs."
            )
        primary["headline_missing_evidence"] = headline_missing
    blockers = []
    for row in independent_scopes:
        for blocker in row["evidence"]["blockers"]:
            blockers.append({"scope_id": row["scope_id"], **blocker})
    automated_gates = bool(independent_scopes) and all(
        row["evidence"]["passes"] for row in independent_scopes
    )
    is_v4 = scope.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION
    scope_definition = {
        "outside_in": scope["outside_in"],
        "clinic": deepcopy(scope["clinic"]),
        "catchment": deepcopy(selected_areas[0]["catchment"]),
        "treatments": deepcopy(selected_areas[0]["treatments"]),
        "platforms": list(scope["platforms"]),
        "planned_runs": scope["planned_runs"],
        "test_mode": scope["test_mode"],
        "prompt_pack_version": scope.get("prompt_pack_version"),
        "evidence_policy_version": scope.get("evidence_policy_version"),
        "platform_data_modes": effective_data_modes,
    }
    if is_v4:
        scope_definition.update(
            {
                "prompt_catalog_version": scope.get("prompt_catalog_version"),
                "prompt_catalog_content_sha256": scope.get(
                    "prompt_catalog_content_sha256"
                ),
                "prompt_geography_version": scope.get("prompt_geography_version"),
                "prompt_geography_hash": scope.get("prompt_geography_hash"),
                "baskets": [
                    {
                        "id": basket_id,
                        "evidence_cell_count": sum(
                            row.get("basket_id") == basket_id for row in scopes
                        ),
                    }
                    for basket_id in (
                        "treatment_local",
                        "treatment_expanded",
                        "clinic_archetype",
                    )
                ],
            }
        )
    # Only production scopes that explicitly froze the branch-treatment
    # matrix expose it as such. Legacy flat v2 scopes retain their original
    # presentation and approval contract even though ``area_scopes`` provides
    # a normalized internal view for analysis.
    if isinstance(scope.get("areas"), list) and scope.get("areas"):
        scope_definition["areas"] = deepcopy(selected_areas)
        if isinstance(scope.get("selection_provenance"), Mapping):
            scope_definition["selection_provenance"] = deepcopy(
                scope["selection_provenance"]
            )

    establishes = [
        "Observed clinic appearances and recommendation classifications in the exact frozen scopes.",
        "Repeated competitor differences only where evidence gates pass.",
    ]
    if copilot_is_derived:
        establishes.append(
            "Copilot reporting values are the rounded-down average of ChatGPT, "
            "Gemini, and Google AI Overview; Copilot is not independently measured."
        )

    analysis = {
        "schema_version": (
            ANALYSIS_V4_SCHEMA_VERSION if is_v4 else ANALYSIS_SCHEMA_VERSION
        ),
        "snapshot_id": scope["snapshot_id"],
        "report_id": scope["snapshot_id"],
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": scope["prompt_pack_hash"],
        "scope_definition": scope_definition,
        "scopes": scopes,
        "competitor_review": {
            "schema_version": COMPETITOR_REVIEW_SCHEMA_VERSION,
            "scopes": reviews,
        },
        "primary_finding": primary,
        "overall_verdict": verdict,
        "preliminary_competitiveness": deepcopy(
            (primary or {}).get("preliminary_competitiveness") or {}
        ),
        "next_investigations": _next_investigations(verdict, primary),
        "boundary": {
            "establishes": establishes,
            "does_not_establish": [
                "Root causes were not established.",
                "The focused competitor benchmark is not the complete market.",
                "No implementation recommendation has been made.",
                "No future ranking, patient or revenue outcome is guaranteed.",
                "Performance outside the selected branch, treatment, area, platform, prompts and runs was not measured.",
            ],
        },
        "copy_policy": {
            "version": COPY_POLICY_VERSION,
            "llm_generated_client_conclusions": False,
        },
    }
    analysis["results_hash"] = compute_results_hash(analysis)
    analysis["presentation"] = {
        "schema_version": (
            PRESENTATION_V4_SCHEMA_VERSION if is_v4 else PRESENTATION_SCHEMA_VERSION
        ),
        "bounded_scope_statement": (
            (
                f"This Snapshot keeps {len(scopes)} basket-platform evidence cells separate "
                f"across {len(selected_areas)} selected area(s), "
                f"{sum(len(row['treatments']) for row in selected_areas)} branch-treatment "
                f"combination(s), and {len(scope['platforms'])} platforms."
            )
            if is_v4
            else (
                f"This Snapshot covers {sum(len(row['treatments']) for row in selected_areas)} "
                f"branch-treatment combination(s) across {len(selected_areas)} area(s) and "
                f"{len(scope['platforms'])} platform(s)."
            )
        ),
        "outside_in_label": "Outside-in Snapshot" if scope["outside_in"] else "Verified-client Snapshot",
        "overall_verdict": verdict,
        "preliminary_competitiveness": deepcopy(
            (primary or {}).get("preliminary_competitiveness") or {}
        ),
        "primary_scope_id": (primary or {}).get("scope_id"),
    }
    primary_examples = (primary or {}).get("evidence_examples") or []
    if verdict == "More evidence required":
        primary_evidence_linked = bool(
            (primary or {}).get("missing_evidence_references")
        )
    else:
        primary_evidence_linked = len(primary_examples) >= 2 and len(
            {row.get("prompt_id") for row in primary_examples}
        ) >= 2
    if not primary_evidence_linked:
        blockers.append(
            {
                "scope_id": (primary or {}).get("scope_id"),
                "code": "primary_evidence_links",
                "message": "Primary finding lacks two prompt-distinct evidence links or structured missing-evidence references.",
            }
        )
    hard_blocker_codes = {
        "complete_prompt_families",
        "valid_observations",
        "coverage",
        "clinic_identity_confirmed",
        "treatment_relevance_confirmed",
        "clinic_archetype_relevance_confirmed",
        "catchment_confirmed",
        "relevant_comparator_identity_resolved",
        "geographic_treatment_relevance_confirmed",
        "geographic_subject_relevance_confirmed",
        "recommendation_extraction_complete",
        "raw_evidence_hashes_consistent",
        "primary_evidence_links",
    }
    release_blockers = [
        row for row in blockers if str(row.get("code") or "") in hard_blocker_codes
    ]
    evidence_limitations = [
        row for row in blockers if str(row.get("code") or "") not in hard_blocker_codes
    ]
    if scope["test_mode"]:
        release_blockers.append(
            {"code": "test_mode", "message": "Single-run test mode cannot be published."}
        )
    if release_blockers:
        publication_state = "partial"
    elif verdict == "More evidence required":
        publication_state = "inconclusive"
    else:
        publication_state = "awaiting_founder_review"
    basket_quality = []
    if is_v4:
        for basket_id in (
            "treatment_local",
            "treatment_expanded",
            "clinic_archetype",
        ):
            basket_scopes = [row for row in scopes if row.get("basket_id") == basket_id]
            independent_basket_scopes = [
                row for row in basket_scopes if not is_derived_copilot_scope(row)
            ]
            basket_quality.append(
                {
                    "basket_id": basket_id,
                    "evidence_cell_count": len(basket_scopes),
                    "platform_count": len(
                        {str(row.get("platform") or "") for row in basket_scopes}
                    ),
                    "independently_measured_platform_count": len(
                        {
                            str(row.get("platform") or "")
                            for row in independent_basket_scopes
                        }
                    ),
                    "derived_platform_count": len(basket_scopes)
                    - len(independent_basket_scopes),
                    "subject_count": len(
                        {
                            str(((row.get("subject") or {}).get("id")) or "")
                            for row in basket_scopes
                        }
                    ),
                    "planned_cell_observations": sum(
                        int((row.get("collection") or {}).get("planned_observations") or 0)
                        for row in basket_scopes
                    ),
                    "valid_cell_observations": sum(
                        int((row.get("collection") or {}).get("valid_observations") or 0)
                        for row in basket_scopes
                    ),
                    "all_cells_measured": bool(basket_scopes)
                    and all(scope_result_available(row) for row in basket_scopes),
                    "all_evidence_gates_passed": bool(independent_basket_scopes)
                    and all(
                        (row.get("evidence") or {}).get("passes")
                        for row in independent_basket_scopes
                    ),
                }
            )
    all_results_available = bool(scopes) and all(
        scope_result_available(row) for row in scopes
    )
    derived_platforms = [
        platform
        for platform in scope["platforms"]
        if any(
            str(row.get("platform") or "") == platform
            and is_derived_copilot_scope(row)
            for row in scopes
        )
    ]
    independently_measured_platforms = [
        platform
        for platform in scope["platforms"]
        if any(
            str(row.get("platform") or "") == platform
            and not is_derived_copilot_scope(row)
            and scope_result_available(row)
            for row in scopes
        )
    ]
    analysis["quality"] = {
        "automated_gates_passed": automated_gates,
        "evidence_integrity_complete": all(
            row["evidence"]["gates"]["raw_evidence_hashes_consistent"]["passed"]
            and row["evidence"]["gates"]["recommendation_extraction_complete"]["passed"]
            for row in independent_scopes
        ),
        "publication_eligible": automated_gates and primary_evidence_linked and not scope["test_mode"],
        "collection_complete": all_results_available,
        "prospect_ready": False,
        "test_mode": scope["test_mode"],
        "approval_required": True,
        "founder_review": {"status": "not_reviewed"},
        "primary_evidence_linked": primary_evidence_linked,
        "publication_state": publication_state,
        "release_blockers": release_blockers,
        "evidence_limitations": evidence_limitations,
        "blocker_messages": [
            str(row.get("message") or row.get("code")) for row in blockers
        ],
        "blockers": blockers
        + ([{"code": "test_mode", "message": "Single-run test mode cannot be published."}] if scope["test_mode"] else []),
        "scope_count": len(scopes),
        "basket_quality": basket_quality,
        "all_included_platforms_measured": all_results_available,
        "all_platform_results_available": all_results_available,
        "independently_measured_platforms": independently_measured_platforms,
        "derived_platforms": derived_platforms,
    }
    return analysis


# Public semantic alias used by integrations that describe the input rather
# than the exact scope loop.
analyze_observations = analyze_scopes
