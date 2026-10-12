"""Deterministic presentation adapter for the Free Snapshot v2 contract.

Analysis owns facts and verdicts.  This module normalizes the versioned payload
into the small view model used by the fixed report, builds approved copy, and
records a stable copy hash.  It deliberately contains no market-wide fallback
and never changes a finding verdict.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from clinic_geo.reporting.copy_policy import (
    attach_copy_notices,
    assert_copy_policy,
    scan_client_copy,
    scan_evidence_excerpts,
)


PRESENTATION_VERSION = "free-snapshot-report-v2"
PRESENTATION_V4_VERSION = "free-snapshot-report-v4"

MATERIAL = "material_gap_demonstrated"
COMPETITIVE = "competitive_or_leading"
INSUFFICIENT = "insufficient_evidence"

OVERALL_MATERIAL = "Material opportunity demonstrated"
OVERALL_MORE = "More evidence required"
OVERALL_NO_GAP = "No strong opportunity currently visible"
OVERALL_LABELS = (OVERALL_MATERIAL, OVERALL_MORE, OVERALL_NO_GAP)

FOUNDER_CONFIRMATIONS = (
    "clinic_identity",
    "branch_treatment_matrix",
    "treatment_relevance",
    "catchment",
    "focused_competitors",
    "evidence_quality",
    "client_wording",
)

LEGACY_FOUNDER_CONFIRMATIONS = tuple(
    key for key in FOUNDER_CONFIRMATIONS if key != "branch_treatment_matrix"
)

V4_FOUNDER_CONFIRMATIONS = FOUNDER_CONFIRMATIONS + (
    "clinic_archetype_wording",
    "prompt_geography",
)

FINDING_LABELS = {
    MATERIAL: "Material gap demonstrated",
    COMPETITIVE: "Competitive or leading",
    INSUFFICIENT: "Insufficient evidence",
}

PRELIMINARY_LABELS = {
    "strong": "Strong competitiveness",
    "balanced": "Balanced competitiveness",
    "weak": "Weak competitiveness",
    "not_measured": "Not measured",
}

DIAGNOSTIC_DISCLAIMER = "A full Diagnostic Audit establishes robust results."

DEFAULT_BOUNDARY = {
    "does_not_establish": [
        "The clinic's website was not diagnostically inspected.",
        "Controlled listings were not fully assessed.",
        "Root causes were not established.",
        "The focused competitor benchmark is not the complete market.",
        "No implementation recommendation has been made.",
        "No future ranking, patient or revenue outcome is guaranteed.",
    ]
}


class PresentationContractError(ValueError):
    """The normalized v2 payload cannot support the client report contract."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _get(value: Any, *paths: str, default: Any = None) -> Any:
    for path in paths:
        current = value
        found = True
        for key in path.split("."):
            if not isinstance(current, Mapping) or key not in current:
                found = False
                break
            current = current[key]
        if found and current is not None:
            return current
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _label(value: Any, default: str = "") -> str:
    if isinstance(value, Mapping):
        return _text(
            _get(
                value,
                "name",
                "singular",
                "canonical_name",
                "label",
                "title",
                "id",
                "key",
            ),
            default,
        )
    return _text(value, default)


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        percent = text.endswith("%")
        if percent:
            text = text[:-1]
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed / 100 if percent else parsed
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _rate(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is None:
        return None
    return numeric / 100 if abs(numeric) > 1 else numeric


def _integer(value: Any) -> int | None:
    numeric = _number(value)
    return None if numeric is None else int(numeric)


def _percent(value: Any) -> str:
    rate = _rate(value)
    if rate is None:
        return "Not measured"
    points = rate * 100
    return f"{points:.0f}%" if abs(points - round(points)) < 0.05 else f"{points:.1f}%"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def is_presentation_v2(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    version = _text(payload.get("schema_version"))
    return version.startswith("free_snapshot.") or (
        isinstance(payload.get("scope_definition"), Mapping)
        and isinstance(payload.get("scopes"), list)
    )


def _metric(container: Any, key: str) -> dict:
    """Validate and pass through an analysis-owned metric.

    Snapshot v2 deliberately does not repair incomplete metrics in the report
    layer. Reconstructing a numerator, denominator, rate, or display string
    here would make the renderer a second analysis engine.
    """

    root = _mapping(container)
    value = _mapping(root.get(key))
    if not value:
        raise PresentationContractError(f"Analysis did not supply metric {key!r}")
    numerator = _integer(_get(value, "numerator", "count"))
    denominator = _integer(_get(value, "denominator", "valid_observations", "valid_outcomes"))
    rate = _rate(_get(value, "rate", "value"))
    display = _text(_get(value, "display"))
    if numerator is None or denominator is None or not display:
        raise PresentationContractError(
            f"Metric {key!r} needs analysis-owned numerator, denominator and display"
        )
    if denominator > 0 and rate is None:
        raise PresentationContractError(f"Metric {key!r} needs an analysis-owned rate")
    if denominator == 0 and (rate is not None or display != "Not measured"):
        raise PresentationContractError(
            f"Zero-denominator metric {key!r} must be analysis-labelled Not measured"
        )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "display": display,
    }


def _entity(entity: Any, fallback_name: str = "") -> dict:
    row = _mapping(entity)
    metrics = _mapping(row.get("metrics"))
    if not metrics:
        raise PresentationContractError(
            f"Analysis did not supply metrics for {_label(row, fallback_name) or 'an entity'}"
        )
    distribution = _mapping(_get(metrics, "recommendation_distribution", default={}))
    return {
        "entity_id": _text(_get(row, "entity_id", "account_id", "id")) or None,
        "name": _label(row, fallback_name),
        "visibility": _metric(metrics, "visibility"),
        "qualified_recommendation": _metric(metrics, "qualified_recommendation"),
        "first_named": _metric(metrics, "first_named"),
        "mean_position": _number(
            _get(metrics, "mean_position_when_named", "mean_position", "average_position")
        ),
        "recommendation_distribution": dict(distribution),
        "named_prompt_count": _integer(_get(metrics, "named_prompt_count")),
        "named_run_count": _integer(_get(metrics, "named_run_count")),
        "named_intent_count": _integer(_get(metrics, "named_intent_count")),
    }


def _countish(value: Any) -> int:
    if isinstance(value, Mapping):
        return _integer(_get(value, "count", "value")) or 0
    values = _sequence(value)
    if values:
        return len(values)
    return _integer(value) or 0


def _repeatability(scope: Mapping[str, Any]) -> dict:
    source = _mapping(scope.get("repeatability"))
    prompts = _integer(source.get("prompt_count"))
    runs = _integer(source.get("run_count"))
    intents = _integer(source.get("intent_count"))
    status = _text(_get(source, "status"))
    statement = _text(source.get("statement"))
    if prompts is None or runs is None or intents is None or not status or not statement:
        raise PresentationContractError(
            "Analysis must supply repeatability counts, status and statement"
        )
    return {
        "status": status,
        "prompt_count": prompts,
        "run_count": runs,
        "intent_count": intents,
        "statement": statement,
    }


def _preliminary_competitiveness(
    row: Mapping[str, Any],
    *,
    valid: int,
    appearance_gap: float,
    qualified_gap: float,
) -> dict:
    supplied = _mapping(row.get("preliminary_competitiveness"))
    status = _text(supplied.get("status"))
    if status not in PRELIMINARY_LABELS:
        if valid <= 0:
            status = "not_measured"
            basis = "collection"
        elif not isinstance(row.get("strongest_competitor"), Mapping):
            clinic = _mapping(row.get("clinic"))
            metrics = _mapping(clinic.get("metrics"))
            visibility = _rate(_get(metrics, "visibility.rate")) or 0.0
            qualified = _rate(_get(metrics, "qualified_recommendation.rate")) or 0.0
            best = max(visibility, qualified)
            basis = "clinic_visibility"
            status = "strong" if best >= 0.25 else "weak" if best <= 0 else "balanced"
        else:
            decisive = qualified_gap if abs(qualified_gap) >= 20 else appearance_gap
            basis = "qualified_recommendation" if abs(qualified_gap) >= 20 else "appearance"
            status = "weak" if decisive >= 20 else "strong" if decisive <= -20 else "balanced"
    else:
        basis = _text(supplied.get("basis"), "qualified_recommendation")
    return {
        "status": status,
        "label": PRELIMINARY_LABELS[status],
        "basis": basis,
        "preliminary": True,
    }


def _classification(value: Any) -> str:
    labels = {
        "strong_recommendation": "Strong recommendation",
        "shortlisted_positive": "Shortlisted or positively presented",
        "neutral_mention": "Neutral mention",
        "caveated_negative": "Caveated or negative mention",
        "not_named": "Not named",
        "unclassifiable": "Unclassifiable",
    }
    raw = _text(_get(value, "classification", "status", default=value))
    return labels.get(raw, raw.replace("_", " ").capitalize() if raw else "Not measured")


def _evidence_example(value: Any) -> dict:
    row = _mapping(value)
    competitor_classes = []
    raw_competitors = _get(row, "competitor_classifications", default=[])
    if not _sequence(raw_competitors):
        singular = _get(row, "competitor_classification")
        raw_competitors = [singular] if isinstance(singular, Mapping) else []
    for item in _sequence(raw_competitors):
        competitor_classes.append({
            "name": _label(_get(item, "competitor", "name", default=item), "Competitor"),
            "classification": _classification(item),
        })
    return {
        "prompt_id": _text(_get(row, "prompt_id")),
        "prompt": _text(_get(row, "prompt", "prompt_text"), "Prompt unavailable"),
        "platform": _label(_get(row, "platform", "platform_label"), "Platform"),
        "model_or_surface": _text(_get(row, "model_or_surface", "model", "surface")),
        "timestamp": _text(_get(row, "timestamp", "collected_at")),
        "run": _text(_get(row, "run", "run_id")),
        "answer_reference": _text(
            _get(row, "answer_reference", "answer_ref", "raw_answer_reference")
        ),
        "excerpt": _text(_get(row, "excerpt", "supporting_excerpt"), "Excerpt unavailable"),
        "client_classification": _classification(
            _get(row, "client_classification", "clinic_classification")
        ),
        "competitor_classifications": competitor_classes,
    }


def _scope(value: Any, clinic_name: str) -> dict:
    row = _mapping(value)
    scope_id = _text(_get(row, "scope_id", "id"))
    if not scope_id:
        raise PresentationContractError("Every v2 scope needs a scope_id")
    subject_kind = _text(_get(row, "subject_kind"), "treatment")
    subject_value = _get(row, "subject", "treatment", "clinic_archetype", default={})
    treatment = _label(
        subject_value,
        "Clinic profile" if subject_kind == "clinic_archetype" else "Treatment",
    )
    target_geographies = [
        _label(item)
        for item in _sequence(_get(row, "target_geographies"))
        if _label(item)
    ]
    target = _label(_get(row, "target_geography"))
    area = " + ".join(dict.fromkeys(target_geographies)) or target or _label(
        _get(row, "catchment", "area"), "Area"
    )
    origin_area = _label(
        _get(row, "origin_catchment", "local_catchment", "catchment", "area"),
        area,
    )
    branch = _label(_get(row, "branch", "origin_branch"), origin_area)
    platform = _label(_get(row, "platform"), "Platform")
    clinic = _entity(_get(row, "clinic", "client", default={}), clinic_name)
    competitors = [_entity(item) for item in _sequence(_get(row, "competitors", "focused_competitors"))]
    if "strongest_competitor" not in row:
        raise PresentationContractError(
            f"Analysis did not select strongest_competitor for {scope_id}"
        )
    strongest_raw = row.get("strongest_competitor")
    strongest = _entity(strongest_raw) if isinstance(strongest_raw, Mapping) else None
    collection = _mapping(_get(row, "collection", default={}))
    planned = _integer(_get(collection, "planned", "planned_observations", default=_get(row, "planned_observations"))) or 0
    valid = _integer(_get(collection, "valid", "valid_observations", default=_get(row, "valid_observations"))) or 0
    coverage = _rate(_get(collection, "coverage", "coverage_rate", default=_get(row, "coverage_rate")))
    coverage_display = _text(collection.get("coverage_display"))
    if coverage is None or not coverage_display:
        raise PresentationContractError(
            f"Analysis did not supply coverage and coverage_display for {scope_id}"
        )
    evidence = _mapping(_get(row, "evidence", "evidence_gate", default={}))
    evidence_passes = bool(_get(evidence, "passes", default=False))
    blockers = [
        _text(_get(item, "message", "code", default=item))
        for item in _sequence(_get(evidence, "blockers", "limitations"))
    ]
    verdict = _text(_get(row, "verdict.status", "verdict"))
    if verdict not in FINDING_LABELS:
        raise PresentationContractError(f"Unknown finding verdict {verdict!r} for {scope_id}")
    repeatability = _repeatability(row)
    gaps = _mapping(_get(row, "gaps", default={}))
    appearance_gap = _number(_get(gaps, "appearance_percentage_points"))
    qualified_gap = _number(_get(gaps, "qualified_percentage_points"))
    if appearance_gap is None or qualified_gap is None:
        raise PresentationContractError(
            f"Analysis did not supply both competitor gaps for {scope_id}"
        )
    preliminary = _preliminary_competitiveness(
        row,
        valid=valid,
        appearance_gap=appearance_gap,
        qualified_gap=qualified_gap,
    )
    examples = [_evidence_example(item) for item in _sequence(_get(row, "evidence_examples"))]
    return {
        "scope_id": scope_id,
        "order": _integer(_get(row, "order")) or 0,
        "treatment": treatment,
        "subject": treatment,
        "subject_kind": subject_kind,
        "basket_id": _text(_get(row, "basket_id")),
        "basket_label": _label(_get(row, "basket", "basket_label")),
        "branch": branch,
        "area": area,
        "origin_area": origin_area,
        "platform": platform,
        "platform_id": _text(_get(row, "platform.id", "platform.key", "platform")),
        "model_or_surface": _text(_get(row, "platform.model_or_surface", "model_or_surface")),
        "prompt_ids": [str(item) for item in _sequence(_get(row, "prompt_ids"))],
        "planned": planned,
        "valid": valid,
        "coverage": coverage,
        "coverage_display": coverage_display,
        "collection_status": _text(_get(collection, "status"), "not_measured"),
        "measurement_mode": _text(_get(row, "measurement_mode"), "browser_measured"),
        "data_origin": _text(_get(row, "data_origin"), "browser_observation"),
        "derived_from_platforms": [
            str(item) for item in _sequence(_get(row, "derived_from_platforms"))
        ],
        "clinic": clinic,
        "competitors": competitors,
        "strongest_competitor": strongest,
        "appearance_gap_pp": appearance_gap,
        "qualified_gap_pp": qualified_gap,
        "preliminary_verdict": preliminary,
        "preliminary_verdict_label": preliminary["label"],
        "repeatability": repeatability,
        "evidence_passes": evidence_passes,
        "evidence_blockers": blockers,
        "verdict": verdict,
        "verdict_label": FINDING_LABELS[verdict],
        "evidence_examples": examples,
    }


def _overall_label(payload: Mapping[str, Any]) -> str:
    raw = _get(payload, "overall_verdict.label", "overall_verdict.status", "overall_verdict")
    text = _text(raw)
    if text not in OVERALL_LABELS:
        raise PresentationContractError(
            "overall_verdict must be exactly one permitted Free Snapshot verdict"
        )
    return text


def _default_establishes(overall: str, primary: Mapping[str, Any] | None, clinic: str) -> list[str]:
    return [
        "Observed clinic appearances and recommendation classifications in the exact measured scopes.",
        "Preliminary clinic-to-competitor differences shown by treatment and platform.",
    ]


def _investigation(value: Any) -> dict:
    row = _mapping(value)
    return {
        "category": _text(_get(row, "category", "title"), "Further investigation"),
        "question_to_answer": _text(
            _get(row, "question_to_answer", "question", "title")
        ),
        "why_required": _text(
            _get(
                row,
                "why_required",
                "why_this_must_be_investigated",
                "why",
                "description",
            )
        ),
    }


def _default_investigations(overall: str, primary: Mapping[str, Any] | None) -> list[dict]:
    return [
        {
            "category": "Competitor validation",
            "question_to_answer": "Which named clinics form the correct local treatment comparison set?",
            "why_required": "A full Diagnostic Audit validates clinic identity, location, treatment relevance and trading status.",
        },
        {
            "category": "Cross-platform validation",
            "question_to_answer": "How consistently does the competitiveness pattern appear across the selected platforms?",
            "why_required": DIAGNOSTIC_DISCLAIMER,
        },
    ]


def _publication(
    payload: Mapping[str, Any],
    *,
    scope_hash: str,
    results_hash: str,
    copy_hash: str,
) -> dict:
    quality = _mapping(_get(payload, "quality", default={}))
    review = _mapping(_get(quality, "founder_review", default={}))
    founder = _get(quality, "founder_review.status", "founder_review_status")
    founder_approved = _text(founder).lower() == "approved"
    confirmations = _mapping(review.get("confirmations"))
    explicit_area_matrix = bool(
        _sequence(_get(payload, "scope_definition.areas", default=[]))
    )
    is_v4 = _text(
        _get(payload, "scope_definition.prompt_pack_version", "prompt_pack_version")
    ) == "snapshot-prompts-v4"
    required_confirmations = (
        V4_FOUNDER_CONFIRMATIONS
        if is_v4
        else FOUNDER_CONFIRMATIONS
        if explicit_area_matrix
        else LEGACY_FOUNDER_CONFIRMATIONS
    )
    all_confirmations = all(
        confirmations.get(key) is True for key in required_confirmations
    )
    review_bundle = _text(review.get("review_bundle_sha256"))
    review_bundle_valid = bool(re.fullmatch(r"[0-9a-f]{64}", review_bundle))
    approved_hashes_match = founder_approved and all_confirmations and review_bundle_valid and all((
        scope_hash and _text(review.get("scope_hash")) == scope_hash,
        results_hash and _text(review.get("results_hash")) == results_hash,
        copy_hash and _text(review.get("copy_hash")) == copy_hash,
    ))
    force_internal = bool(_get(payload, "presentation.internal_candidate", default=False))
    prospect_ready = (
        bool(_get(quality, "prospect_ready", default=False))
        and approved_hashes_match
        and not force_internal
    )
    raw_state = _text(
        _get(quality, "publication_state", "release_state", "collection_status"),
        "draft",
    ).lower().replace(" ", "_")
    allowed = {
        "draft", "partial", "inconclusive", "awaiting_founder_review",
        "prospect_ready", "rejected",
    }
    state = "prospect_ready" if prospect_ready else (raw_state if raw_state in allowed else "draft")
    if state == "prospect_ready" and not prospect_ready:
        state = "awaiting_founder_review"
    watermark = None
    if not prospect_ready:
        watermark = (
            "REJECTED · DO NOT SHARE"
            if state == "rejected"
            else f"INTERNAL CANDIDATE · {state.replace('_', ' ').upper()}"
        )
    return {
        "state": state,
        "prospect_ready": prospect_ready,
        "founder_approved": founder_approved,
        "all_confirmations": all_confirmations,
        "required_confirmations": list(required_confirmations),
        "review_bundle_sha256": review_bundle or None,
        "review_bundle_valid": review_bundle_valid,
        "approved_hashes_match": approved_hashes_match,
        "internal_candidate": force_internal,
        "watermark": watermark,
    }


def build_presentation_model(payload: Mapping[str, Any]) -> dict:
    """Return the renderer-facing v2 view model without mutating ``payload``."""

    if not is_presentation_v2(payload):
        raise PresentationContractError("Payload is not a Free Snapshot presentation-v2 contract")
    scope_definition = _mapping(payload.get("scope_definition"))
    clinic_name = _label(_get(scope_definition, "clinic"), _text(_get(payload, "client.name"), "Clinic"))
    if not clinic_name:
        raise PresentationContractError("scope_definition.clinic needs a name")
    scopes = [_scope(value, clinic_name) for value in _sequence(payload.get("scopes"))]
    if not scopes:
        raise PresentationContractError("presentation-v2 needs at least one scope")
    if len(scopes) > 56:
        raise PresentationContractError(
            "The fixed report supports at most 56 basket-subject-platform evidence cells"
        )
    order = [row["order"] for row in scopes]
    if any(value <= 0 for value in order) or order != sorted(order) or len(set(order)) != len(order):
        raise PresentationContractError(
            "Analysis must supply scopes once in deterministic configured order"
        )
    overall = _overall_label(payload)
    primary_id = _text(_get(payload, "primary_finding.scope_id"))
    primary = next((row for row in scopes if row["scope_id"] == primary_id), None)
    if not primary_id:
        raise PresentationContractError("Analysis must select primary_finding.scope_id")
    if primary is None:
        raise PresentationContractError(f"primary_finding references unknown scope {primary_id!r}")
    primary_finding = _mapping(payload.get("primary_finding"))
    primary["headline_missing_evidence"] = _text(
        primary_finding.get("headline_missing_evidence")
    )
    area = primary["area"]
    platform = primary["platform"]
    treatment = primary["treatment"]
    clinic = primary["clinic"]
    rival = primary["strongest_competitor"]
    preliminary = primary["preliminary_verdict"]
    preliminary_status = preliminary["status"]
    preliminary_label = preliminary["label"]

    has_v4_baskets = any(scope.get("basket_id") for scope in scopes)
    scope_statement = (
        "This is a bounded answer-side measurement of local treatment, wider-area "
        "treatment and general clinic-profile queries. Each basket and geography keeps "
        "its own denominator; this is not a complete market, competitor or root-cause analysis."
        if has_v4_baskets
        else "This is a bounded answer-side measurement of selected treatment and location "
        "queries. It is not a complete market, competitor or root-cause analysis."
    )
    if preliminary_status == "not_measured":
        finding_summary = (
            f"{platform} was not measured for {treatment} in {area} in this Snapshot."
        )
    elif preliminary_status == "weak" and rival:
        finding_summary = (
            f"{rival['name']} appears more competitive than {clinic_name} in this measured sample. "
            f"{clinic_name} appeared in {clinic['visibility']['display']} and received qualified "
            f"recommendations in {clinic['qualified_recommendation']['display']}."
        )
    elif preliminary_status == "weak":
        finding_summary = (
            f"{clinic_name} shows weak preliminary competitiveness in this measured sample. "
            f"It appeared in {clinic['visibility']['display']} and received qualified "
            f"recommendations in {clinic['qualified_recommendation']['display']}."
        )
    elif preliminary_status == "strong":
        finding_summary = (
            f"{clinic_name} shows strong preliminary competitiveness in this measured sample. "
            f"It appeared in {clinic['visibility']['display']} and received qualified "
            f"recommendations in {clinic['qualified_recommendation']['display']}."
        )
    else:
        finding_summary = (
            f"{clinic_name} and the strongest measured competitor are within the preliminary "
            f"20-point competitiveness range in this sample. {clinic_name} appeared in "
            f"{clinic['visibility']['display']} and received qualified recommendations in "
            f"{clinic['qualified_recommendation']['display']}."
        )
    coverage_statement = (
        f"Coverage was {primary['coverage_display']}. {DIAGNOSTIC_DISCLAIMER}"
    )
    boundary = _mapping(_get(payload, "boundary", default={}))
    establishes = _default_establishes(overall, primary, clinic_name)
    does_not = [
        str(item) for item in _sequence(
            _get(boundary, "does_not_establish", "does_not_establishes")
        )
    ] or list(DEFAULT_BOUNDARY["does_not_establish"])
    investigations = _default_investigations(overall, primary)
    if len(establishes) > 4 or len(does_not) > 6 or len(investigations) > 4:
        raise PresentationContractError(
            "The fixed report allows 4 established facts, 6 limitations and 4 investigations"
        )
    if overall == OVERALL_MATERIAL:
        cta = {
            "kind": "diagnostic_audit",
            "title": "Next step",
            "body": (
                f"This preliminary Snapshot shows weak competitiveness for {treatment} in "
                f"{area} on {platform}. It does not establish the cause; a full Diagnostic "
                "Audit must investigate that pattern."
            ),
            "label": "Discuss the Diagnostic Audit",
            "href": "https://citeup.tech/#contact",
        }
    elif overall == OVERALL_MORE:
        cta = {
            "kind": "evidence_completion",
            "title": "Next step",
            "body": (
                f"This Snapshot indicates {preliminary_label.lower()} within the collected "
                "results. A Diagnostic Audit is not recommended until missing runs or "
                "unavailable platform collection are completed."
            ),
            "label": None,
            "href": None,
        }
    else:
        cta = {
            "kind": "none",
            "title": "Conclusion",
            "body": (
                f"This Snapshot indicates {preliminary_label.lower()} within the measured scope. "
                "No Diagnostic Audit is recommended at this stage."
            ),
            "label": None,
            "href": None,
        }
    copy_deck = {
        "page_1": {
            "scope_statement": scope_statement,
            "headline": preliminary_label,
            "finding_summary": finding_summary,
            "coverage_statement": coverage_statement,
        },
        "page_2": {
            "boundary": (
                "Focused competitor benchmark from the measured answers — not a complete "
                "competitor analysis."
            ),
        },
        "page_3": {
            "establishes": establishes,
            "does_not_establish": does_not,
            "investigations": investigations,
        },
        "cta": cta,
    }
    fragments: dict[str, str] = {
        "presentation.page_1.scope_statement": scope_statement,
        "presentation.page_1.headline": preliminary_label,
        "presentation.page_1.finding_summary": finding_summary,
        "presentation.page_1.coverage_statement": coverage_statement,
        "presentation.page_2.boundary": copy_deck["page_2"]["boundary"],
        "presentation.cta.body": cta["body"],
    }
    for index, statement in enumerate(establishes):
        fragments[f"presentation.page_3.establishes[{index}]"] = statement
    for index, statement in enumerate(does_not):
        fragments[f"presentation.page_3.does_not_establish[{index}]"] = statement
    for index, item in enumerate(investigations):
        fragments[f"presentation.page_3.investigations[{index}].question"] = item["question_to_answer"]
        fragments[f"presentation.page_3.investigations[{index}].why"] = item["why_required"]
    protected_terms = {clinic_name, treatment, area, platform}
    for scope in scopes:
        protected_terms.update({scope["treatment"], scope["area"], scope["platform"]})
        protected_terms.update(item["name"] for item in scope["competitors"] if item["name"])
    blocking_policy = scan_client_copy(fragments, protected_terms=protected_terms)
    examples = primary["evidence_examples"][:2]
    evidence_policy = scan_evidence_excerpts(
        {
            f"presentation.evidence_examples[{index}].excerpt": item["excerpt"]
            for index, item in enumerate(examples)
        },
        protected_terms=protected_terms,
    )
    policy = attach_copy_notices(blocking_policy, evidence_policy)
    assert_copy_policy(policy)
    scope_hash = _text(_get(payload, "scope_hash", "scope_definition.scope_hash"))
    results_hash = _text(_get(payload, "results_hash", "input_sha256"))
    prompt_pack_hash = _text(_get(payload, "prompt_pack_hash", "scope_definition.prompt_pack_hash"))
    report_id = _text(_get(payload, "report_id", "snapshot_id"), "Unassigned")
    generated_at = _text(_get(payload, "generated_at", "created_at"))
    copy_hash = _sha256(copy_deck)
    publication = _publication(
        payload,
        scope_hash=scope_hash,
        results_hash=results_hash,
        copy_hash=copy_hash,
    )
    grouped_scopes: dict[tuple[str, str, str], dict] = {}
    if _sequence(scope_definition.get("areas")):
        for scope in scopes:
            if scope.get("basket_id"):
                # v4 can contain 56 evidence cells, but there are at most eight
                # branch-subject report groups. Basket and platform detail stays
                # nested and is never averaged into a treatment claim.
                key = (scope["branch"], scope["subject_kind"], scope["subject"])
            else:
                key = (scope["branch"], scope["area"], scope["treatment"])
            group = grouped_scopes.setdefault(
                key,
                {
                    "branch": scope["branch"],
                    "area": scope.get("origin_area") or scope["area"],
                    "treatment": scope["treatment"],
                    "subject": scope["subject"],
                    "subject_kind": scope["subject_kind"],
                    "platform_scopes": [],
                },
            )
            group["platform_scopes"].append(scope)
    is_v4 = any(scope.get("basket_id") for scope in scopes)
    return {
        "presentation_version": (
            PRESENTATION_V4_VERSION if is_v4 else PRESENTATION_VERSION
        ),
        "report_id": report_id,
        "generated_at": generated_at,
        "scope_hash": scope_hash,
        "results_hash": results_hash,
        "prompt_pack_hash": prompt_pack_hash,
        "copy_hash": copy_hash,
        "clinic_name": clinic_name,
        "area": area,
        "outside_in": bool(_get(scope_definition, "outside_in", default=False)),
        "scope_definition": dict(scope_definition),
        "overall_verdict": preliminary_label,
        "preliminary_verdict": preliminary,
        "primary": primary,
        "scopes": scopes,
        "scope_groups": list(grouped_scopes.values()),
        "evidence_examples": examples,
        "copy": copy_deck,
        "copy_fragments": fragments,
        "protected_terms": sorted(protected_terms, key=str.casefold),
        "copy_policy": policy.as_dict(),
        "publication": publication,
    }


def prepare_presentation_payload(payload: Mapping[str, Any]) -> dict:
    """Attach deterministic copy/policy/identity data for rendering and export."""

    if not is_presentation_v2(payload):
        return dict(payload)
    prepared = copy.deepcopy(dict(payload))
    model = build_presentation_model(prepared)
    presentation = dict(_mapping(prepared.get("presentation")))
    existing_copy_hash = _text(presentation.get("copy_hash"))
    if existing_copy_hash and existing_copy_hash != model["copy_hash"]:
        raise PresentationContractError(
            "Existing presentation.copy_hash does not match deterministic report copy"
        )
    presentation.update({
        "version": model["presentation_version"],
        "copy": model["copy"],
        "copy_hash": model["copy_hash"],
        "publication": model["publication"],
        "preliminary_verdict": model["preliminary_verdict"],
    })
    prepared["presentation"] = presentation
    prepared["copy_policy"] = model["copy_policy"]
    prepared["artifact_identity"] = {
        "report_id": model["report_id"],
        "scope_hash": model["scope_hash"],
        "prompt_pack_hash": model["prompt_pack_hash"],
        "results_hash": model["results_hash"],
        "copy_hash": model["copy_hash"],
    }
    return prepared
