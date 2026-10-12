"""Evidence gates and deterministic verdict selection for Snapshot v2."""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from .contracts_v2 import (
    DEFAULT_EVIDENCE_POLICY,
    EvidencePolicy,
    FindingVerdict,
    OverallVerdict,
)


GATE_FAILURE_MESSAGES = {
    "complete_prompt_families": "Fewer than four prompt families have three valid runs.",
    "valid_observations": "Fewer than 12 observations are valid.",
    "coverage": "Valid observation coverage is below 80%.",
    "pattern_prompt_ids": "The measured pattern does not recur across two prompt IDs.",
    "pattern_runs": "The measured pattern does not recur across two independent runs.",
    "clinic_identity_confirmed": "The clinic identity is unconfirmed.",
    "treatment_relevance_confirmed": "Treatment relevance is unconfirmed.",
    "clinic_archetype_relevance_confirmed": "The clinic profile used by the general prompts is unconfirmed.",
    "catchment_confirmed": "The catchment is unconfirmed.",
    "relevant_comparator_identity_resolved": "A relevant comparator identity is unresolved.",
    "geographic_treatment_relevance_confirmed": "Comparator geography or treatment relevance is unconfirmed.",
    "geographic_subject_relevance_confirmed": "Comparator geography or clinic-profile relevance is unconfirmed.",
    "recommendation_extraction_complete": "Recommendation extraction is incomplete.",
    "raw_evidence_hashes_consistent": "Raw evidence or its stored hashes are inconsistent.",
}


def _threshold_gate(actual, required, *, comparator: str = ">=") -> dict:
    if comparator == ">=":
        passed = actual >= required
    elif comparator == "==":
        passed = actual == required
    else:
        raise ValueError(f"unsupported gate comparator {comparator!r}")
    return {"passed": bool(passed), "actual": actual, "required": required, "comparator": comparator}


def _boolean_gate(actual: object) -> dict:
    return {"passed": bool(actual), "actual": bool(actual), "required": True, "comparator": "=="}


def evaluate_evidence_gates(
    *,
    complete_prompt_families: int,
    valid_observations: int,
    planned_observations: int,
    pattern_prompt_ids: int,
    pattern_runs: int,
    clinic_confirmed: bool,
    treatment_confirmed: bool,
    catchment_confirmed: bool,
    comparator_identity_resolved: bool,
    geographic_treatment_relevance_confirmed: bool,
    extraction_complete: bool,
    raw_evidence_consistent: bool,
    subject_kind: str = "treatment",
    policy: EvidencePolicy = DEFAULT_EVIDENCE_POLICY,
) -> dict:
    coverage = valid_observations / planned_observations if planned_observations else 0.0
    subject_gate = (
        "clinic_archetype_relevance_confirmed"
        if str(subject_kind or "").strip().casefold() == "clinic_archetype"
        else "treatment_relevance_confirmed"
    )
    comparator_relevance_gate = (
        "geographic_subject_relevance_confirmed"
        if str(subject_kind or "").strip().casefold() == "clinic_archetype"
        else "geographic_treatment_relevance_confirmed"
    )
    gates = {
        "complete_prompt_families": _threshold_gate(
            int(complete_prompt_families), policy.min_complete_prompt_families
        ),
        "valid_observations": _threshold_gate(
            int(valid_observations), policy.min_valid_observations
        ),
        "coverage": _threshold_gate(round(coverage, 6), policy.min_coverage),
        "pattern_prompt_ids": _threshold_gate(
            int(pattern_prompt_ids), policy.min_pattern_prompt_ids
        ),
        "pattern_runs": _threshold_gate(int(pattern_runs), policy.min_pattern_runs),
        "clinic_identity_confirmed": _boolean_gate(clinic_confirmed),
        subject_gate: _boolean_gate(treatment_confirmed),
        "catchment_confirmed": _boolean_gate(catchment_confirmed),
        "relevant_comparator_identity_resolved": _boolean_gate(comparator_identity_resolved),
        comparator_relevance_gate: _boolean_gate(
            geographic_treatment_relevance_confirmed
        ),
        "recommendation_extraction_complete": _boolean_gate(extraction_complete),
        "raw_evidence_hashes_consistent": _boolean_gate(raw_evidence_consistent),
    }
    blockers = [
        {
            "code": name,
            "message": GATE_FAILURE_MESSAGES.get(
                name, f"The {name.replace('_', ' ')} evidence gate did not pass."
            ),
            "actual": gate["actual"],
            "required": gate["required"],
        }
        for name, gate in gates.items()
        if not gate["passed"]
    ]
    return {
        "policy_version": policy.version,
        "passes": not blockers,
        "gates": gates,
        "blockers": blockers,
    }


def _rate(metrics: Mapping, name: str) -> float:
    row = metrics.get(name) if isinstance(metrics.get(name), Mapping) else {}
    return float(row.get("rate") or 0.0)


def evaluate_material_gap(
    clinic_metrics: Mapping,
    competitor_metrics: Mapping,
    *,
    recurrence: Mapping[str, Mapping],
    policy: EvidencePolicy = DEFAULT_EVIDENCE_POLICY,
) -> dict:
    """Evaluate both allowed gap rules for one confirmed competitor."""

    metric_findings = {}
    for metric in ("visibility", "qualified_recommendation"):
        clinic_rate = _rate(clinic_metrics, metric)
        competitor_rate = _rate(competitor_metrics, metric)
        gap = competitor_rate - clinic_rate
        ratio = None if clinic_rate == 0 else competitor_rate / clinic_rate
        absolute_rule = gap >= policy.material_gap
        ratio_rule = (
            competitor_rate >= policy.material_min_competitor_rate
            and (clinic_rate == 0 or ratio is not None and ratio >= policy.material_ratio)
        )
        support = recurrence.get(metric) if isinstance(recurrence.get(metric), Mapping) else {}
        prompt_ids = sorted({str(value) for value in support.get("prompt_ids") or []})
        runs = sorted({int(value) for value in support.get("runs") or []})
        recurrence_passes = (
            len(prompt_ids) >= policy.min_pattern_prompt_ids
            and len(runs) >= policy.min_pattern_runs
        )
        metric_findings[metric] = {
            "material": bool((absolute_rule or ratio_rule) and recurrence_passes),
            "clinic_rate": round(clinic_rate, 6),
            "competitor_rate": round(competitor_rate, 6),
            "gap": round(gap, 6),
            "gap_percentage_points": round(gap * 100, 2),
            "ratio": None if ratio is None else round(ratio, 4),
            "absolute_rule_passes": bool(absolute_rule),
            "ratio_rule_passes": bool(ratio_rule),
            "recurrence_passes": recurrence_passes,
            "supporting_prompt_ids": prompt_ids,
            "supporting_runs": runs,
            "supporting_observation_ids": list(support.get("observation_ids") or []),
        }
    winning_metric: Optional[str] = None
    if metric_findings["qualified_recommendation"]["material"]:
        winning_metric = "qualified_recommendation"
    elif metric_findings["visibility"]["material"]:
        winning_metric = "visibility"
    return {
        "material": winning_metric is not None,
        "winning_metric": winning_metric,
        "metrics": metric_findings,
    }


def determine_scope_verdict(
    evidence: Mapping,
    *,
    clinic_appearances: int,
    meaningful_competitor_appearances: int,
    material_findings: Iterable[Mapping],
) -> str:
    if not evidence.get("passes"):
        return FindingVerdict.INSUFFICIENT_EVIDENCE.value
    if any(finding.get("material") for finding in material_findings):
        return FindingVerdict.MATERIAL_GAP_DEMONSTRATED.value
    if int(clinic_appearances) <= 0 and int(meaningful_competitor_appearances) <= 0:
        return FindingVerdict.INSUFFICIENT_EVIDENCE.value
    return FindingVerdict.COMPETITIVE_OR_LEADING.value


def evaluate_factual_inconsistency(
    finding: Mapping,
    observation_lookup: Mapping[str, Mapping],
    *,
    policy: EvidencePolicy = DEFAULT_EVIDENCE_POLICY,
) -> dict:
    """Allow only reproduced contradictions against a founder-confirmed fact."""

    observation_ids = [str(value) for value in finding.get("observation_ids") or []]
    observations = [observation_lookup[value] for value in observation_ids if value in observation_lookup]
    prompt_ids = sorted({str(row.get("prompt_id")) for row in observations if row.get("prompt_id")})
    runs = sorted({int(row.get("run")) for row in observations if row.get("run") is not None})
    reference = finding.get("authoritative_fact_reference")
    if not isinstance(reference, Mapping):
        reference = {}
    reference_confirmed = bool(
        reference.get("founder_confirmed")
        or reference.get("confirmation_status") == "confirmed"
    ) and bool(reference.get("url") or reference.get("reference") or reference.get("id"))
    recurrence_passes = (
        len(prompt_ids) >= policy.min_pattern_prompt_ids
        and len(runs) >= policy.min_pattern_runs
    )
    return {
        "finding_type": "factual_inconsistency",
        "fact_id": finding.get("fact_id"),
        "description": finding.get("description"),
        "material": bool(reference_confirmed and recurrence_passes),
        "authoritative_fact_reference": dict(reference),
        "reference_confirmed": reference_confirmed,
        "recurrence_passes": recurrence_passes,
        "supporting_prompt_ids": prompt_ids,
        "supporting_runs": runs,
        "supporting_observation_ids": observation_ids,
    }


def overall_verdict(scopes: Iterable[Mapping]) -> str:
    rows = [dict(scope) for scope in scopes]
    verdicts = [str(scope.get("verdict") or "") for scope in rows]
    has_baskets = any(row.get("basket_id") for row in rows)
    if has_baskets:
        # A local treatment gap can drive the primary diagnostic conclusion.
        # Wider-area and general-profile gaps must repeat on two independent
        # platforms, preventing one of the larger v4 basket's many cells from
        # becoming a cherry-picked headline.
        local_material = any(
            row.get("basket_id") == "treatment_local"
            and row.get("verdict") == FindingVerdict.MATERIAL_GAP_DEMONSTRATED.value
            for row in rows
        )
        repeated: dict[tuple[str, str, str], set[str]] = {}
        for row in rows:
            if (
                row.get("basket_id") not in {"treatment_expanded", "clinic_archetype"}
                or row.get("verdict") != FindingVerdict.MATERIAL_GAP_DEMONSTRATED.value
            ):
                continue
            subject = row.get("subject") if isinstance(row.get("subject"), Mapping) else {}
            branch = row.get("branch") if isinstance(row.get("branch"), Mapping) else {}
            key = (
                str(row.get("basket_id") or ""),
                str(subject.get("id") or subject.get("name") or ""),
                str(branch.get("id") or branch.get("name") or ""),
            )
            repeated.setdefault(key, set()).add(str(row.get("platform") or ""))
        if local_material or any(len(platforms) >= 2 for platforms in repeated.values()):
            return OverallVerdict.MATERIAL_OPPORTUNITY.value
    elif FindingVerdict.MATERIAL_GAP_DEMONSTRATED.value in verdicts:
        return OverallVerdict.MATERIAL_OPPORTUNITY.value
    if FindingVerdict.INSUFFICIENT_EVIDENCE.value in verdicts:
        return OverallVerdict.MORE_EVIDENCE.value
    return OverallVerdict.NO_STRONG_OPPORTUNITY.value


def select_primary_finding(scopes: Iterable[Mapping]) -> Optional[dict]:
    rows = [dict(scope) for scope in scopes]
    if not rows:
        return None
    verdict = overall_verdict(rows)

    def gaps(row: Mapping) -> tuple[float, float]:
        value = row.get("gaps") if isinstance(row.get("gaps"), Mapping) else {}
        return (
            float(value.get("qualified_recommendation") or value.get("qualified_percentage_points") or 0),
            float(value.get("visibility") or value.get("appearance_percentage_points") or 0),
        )

    if verdict == OverallVerdict.MATERIAL_OPPORTUNITY.value:
        eligible = [
            row for row in rows
            if row.get("verdict") == FindingVerdict.MATERIAL_GAP_DEMONSTRATED.value
        ]
        if any(row.get("basket_id") for row in rows):
            repeated_groups: dict[tuple[str, str, str], set[str]] = {}
            for row in eligible:
                subject = row.get("subject") if isinstance(row.get("subject"), Mapping) else {}
                branch = row.get("branch") if isinstance(row.get("branch"), Mapping) else {}
                key = (
                    str(row.get("basket_id") or ""),
                    str(subject.get("id") or subject.get("name") or ""),
                    str(branch.get("id") or branch.get("name") or ""),
                )
                repeated_groups.setdefault(key, set()).add(str(row.get("platform") or ""))
            eligible = [
                row
                for row in eligible
                if row.get("basket_id") == "treatment_local"
                or len(
                    repeated_groups.get(
                        (
                            str(row.get("basket_id") or ""),
                            str(
                                ((row.get("subject") or {}).get("id"))
                                or ((row.get("subject") or {}).get("name"))
                                or ""
                            ),
                            str(
                                ((row.get("branch") or {}).get("id"))
                                or ((row.get("branch") or {}).get("name"))
                                or ""
                            ),
                        ),
                        set(),
                    )
                )
                >= 2
            ]
        selected = min(
            eligible,
            key=lambda row: (
                -gaps(row)[0],
                -gaps(row)[1],
                -int(
                    (row.get("collection") or {}).get("valid_observations")
                    or (row.get("collection") or {}).get("valid")
                    or 0
                ),
                int(row.get("order") or 0),
            ),
        )
    elif verdict == OverallVerdict.MORE_EVIDENCE.value:
        selected = min(
            (row for row in rows if row.get("verdict") == FindingVerdict.INSUFFICIENT_EVIDENCE.value),
            key=lambda row: int(row.get("order") or 0),
        )
    else:
        selected = min(
            rows,
            key=lambda row: (
                -max(gaps(row)),
                int(row.get("order") or 0),
            ),
        )
    return {
        "scope_id": selected.get("scope_id"),
        "scope_order": selected.get("order"),
        "verdict": selected.get("verdict"),
        "basket_id": selected.get("basket_id"),
        "basket": selected.get("basket"),
        "subject_kind": selected.get("subject_kind"),
        "subject": selected.get("subject"),
        "treatment": selected.get("treatment"),
        "catchment": selected.get("catchment"),
        "origin_catchment": selected.get("origin_catchment"),
        "target_geographies": selected.get("target_geographies") or [],
        "geography_tiers": selected.get("geography_tiers") or [],
        "geography_breakdown": selected.get("geography_breakdown") or [],
        "platform": selected.get("platform"),
        "collection": selected.get("collection"),
        "clinic": selected.get("clinic"),
        "strongest_competitor": selected.get("strongest_competitor"),
        "gaps": selected.get("gaps"),
        "preliminary_competitiveness": selected.get("preliminary_competitiveness"),
        "repeatability": selected.get("repeatability"),
        "evidence": selected.get("evidence"),
        "evidence_examples": selected.get("evidence_examples") or [],
    }
