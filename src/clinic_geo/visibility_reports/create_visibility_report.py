"""Create visibility report files and register them for review.

Ask the reporting package to create the four required files, then read its
normalized data and register that exact data with the report for review.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from clinic_geo.evidence.purpose import EvidencePurpose
from clinic_geo.reporting.copy_policy import (
    assert_copy_policy,
    scan_client_copy,
    scan_rendered_narrative,
)
from clinic_geo.reporting.presentation import build_presentation_model
from clinic_geo.workflows.free_audit.contracts_v2 import (
    PRESENTATION_V4_SCHEMA_VERSION,
)

from .calculate_visibility import (
    ANALYSIS_SCHEMA_VERSION,
    ANALYSIS_VERSION,
    verify_results_hash,
)
from .collection_period import collection_statement
from .report_revisions import render_report_revision
from .report_target import report_target_names, target_name_aggregate


RENDER_DATA_SCHEMA_VERSION = PRESENTATION_V4_SCHEMA_VERSION
REQUIRED_CANDIDATE_ARTIFACTS = frozenset(
    {"pdf", "html", "data", "validation"}
)
PRESENCE_REPORT_VERSION = "free-snapshot-presence-report-v3"


class SnapshotRenderBridgeError(ValueError):
    """The v3 analysis cannot form a truthful candidate bundle."""


class CandidatePayloads(NamedTuple):
    analysis: dict[str, object]
    presentation: dict[str, object]
    quality: dict[str, object]
    candidate_artifacts: dict[str, str]


Renderer = Callable[..., Mapping[str, str | Path]]


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_plain(item) for item in value]
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _plain(item) for key, item in value.items()}


def _analysis_mapping(analysis: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(analysis, Mapping):
        raise SnapshotRenderBridgeError("analysis must be an object")
    result = _mapping(analysis)
    if result.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        raise SnapshotRenderBridgeError(
            f"expected {ANALYSIS_SCHEMA_VERSION} analysis"
        )
    if result.get("analysis_version") != ANALYSIS_VERSION:
        raise SnapshotRenderBridgeError(
            f"expected {ANALYSIS_VERSION} analysis"
        )
    if result.get("candidate_eligible") is True and not verify_results_hash(result):
        raise SnapshotRenderBridgeError("analysis results_hash does not verify")
    return result


def _metric(numerator: int, denominator: int) -> dict[str, object]:
    rate = numerator / denominator if denominator else None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "display": (
            f"{numerator}/{denominator} ({rate:.1%})"
            if rate is not None
            else "Not measured"
        ),
    }


def _clinic_name(analysis: Mapping[str, object]) -> str:
    scope = _mapping(analysis.get("frozen_scope"))
    clinic = _mapping(analysis.get("report_target")) or _mapping(scope.get("clinic"))
    value = clinic.get("name")
    if not isinstance(value, str) or not value:
        raise SnapshotRenderBridgeError("report has no clinic target")
    return value


def _first_mapping(value: object) -> dict[str, object]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                return _mapping(item)
    return {}


def _subject(scope: Mapping[str, object]) -> object:
    treatment = _first_mapping(scope.get("treatments"))
    if treatment:
        return treatment
    area = _first_mapping(scope.get("areas"))
    treatment = _first_mapping(area.get("treatments"))
    if treatment:
        return treatment
    clinic = _mapping(scope.get("clinic"))
    archetype = _mapping(scope.get("clinic_archetype")) or _mapping(clinic.get("archetype"))
    if archetype.get("singular"):
        return {"name": archetype["singular"]}
    return {"name": clinic.get("name")}


def _catchment(scope: Mapping[str, object]) -> dict[str, object]:
    value = scope.get("catchment")
    if isinstance(value, Mapping):
        return _mapping(value)
    area = _first_mapping(scope.get("areas"))
    return _mapping(area.get("catchment"))


def _branch(scope: Mapping[str, object]) -> dict[str, object]:
    area = _first_mapping(scope.get("areas"))
    return _mapping(area.get("branch"))


def _platform_values(analysis: Mapping[str, object]) -> list[object]:
    rows = analysis.get("platforms")
    if not isinstance(rows, list):
        return []
    return [row.get("platform") for row in rows if isinstance(row, Mapping)]


def _platform_observations(
    analysis: Mapping[str, object],
    platform: object,
) -> list[dict[str, object]]:
    observations = analysis.get("observations")
    if not isinstance(observations, list):
        return []
    return [
        dict(row)
        for row in observations
        if isinstance(row, Mapping) and row.get("platform") == platform
    ]


def _platform_name_aggregate(
    analysis: Mapping[str, object],
    platform: object,
    surface_form: object,
) -> dict[str, object]:
    if analysis.get("report_target"):
        return target_name_aggregate(analysis, _platform_observations(analysis, platform))
    rows = analysis.get("platforms")
    if not isinstance(rows, list):
        return {}
    platform_row = next(
        (
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("platform") == platform
        ),
        None,
    )
    if not isinstance(platform_row, Mapping):
        return {}
    aggregates = platform_row.get("name_aggregates")
    if not isinstance(aggregates, list):
        return {}
    found = next(
        (
            row
            for row in aggregates
            if isinstance(row, Mapping)
            and row.get("surface_form") == surface_form
        ),
        None,
    )
    return _mapping(found)


def _recorded_name_metrics(
    aggregate: Mapping[str, object],
    *,
    denominator: int,
) -> dict[str, object]:
    metrics = _mapping(aggregate.get("metrics"))
    presence = _mapping(metrics.get("presence"))
    first = _mapping(metrics.get("first_mention_rank"))
    positions = first.get("recorded_positions")
    if not isinstance(positions, list):
        positions = []
    frequency = _mapping(metrics.get("frequency"))
    presence_count = int(presence.get("numerator") or 0)
    first_count = sum(
        value == 1 and not isinstance(value, bool) for value in positions
    )
    return {
        "visibility": _metric(presence_count, denominator),
        "first_named": _metric(first_count, denominator),
        "mean_position": first.get("mean"),
        "recorded_positions": _plain(positions),
        "frequency": int(frequency.get("occurrences") or 0),
    }


def _identity_counts(
    observations: Sequence[Mapping[str, object]],
    *,
    clinic_name: str,
    names: frozenset[str] | None = None,
) -> tuple[int, int, int]:
    named_rows = []
    for observation in observations:
        occurrences = observation.get("grounded_occurrences")
        if not isinstance(occurrences, list):
            continue
        if any(
            isinstance(row, Mapping)
            and row.get("surface_form") in (names or {clinic_name})
            for row in occurrences
        ):
            named_rows.append(observation)

    def values(*keys: str) -> set[object]:
        result: set[object] = set()
        for row in named_rows:
            identity = _mapping(row.get("prompt_identity"))
            value = next(
                (identity.get(key) for key in keys if identity.get(key) is not None),
                None,
            )
            if value is not None and not isinstance(value, (dict, list)):
                result.add(value)
        return result

    return (
        len(values("prompt_id", "id")),
        len(values("run", "run_ordinal")),
        len(values("semantic_prompt_id")),
    )


def _distinct_identity_values(
    observations: Sequence[Mapping[str, object]],
    *keys: str,
) -> set[object]:
    result: set[object] = set()
    for row in observations:
        identity = _mapping(row.get("prompt_identity"))
        value = next(
            (identity.get(key) for key in keys if identity.get(key) is not None),
            None,
        )
        if value is not None and not isinstance(value, (dict, list)):
            result.add(value)
    return result


def _name_aggregate(
    analysis: Mapping[str, object],
    surface_form: object,
) -> dict[str, object]:
    if analysis.get("report_target"):
        return target_name_aggregate(analysis, analysis.get("observations") or [])
    rows = analysis.get("name_aggregates")
    if not isinstance(rows, list):
        return {}
    found = next(
        (
            row
            for row in rows
            if isinstance(row, Mapping)
            and row.get("surface_form") == surface_form
        ),
        None,
    )
    return _mapping(found)


def _display_platform(value: object) -> str:
    text = str(value or "").strip()
    if text == "github_copilot":
        return "GitHub Copilot (SDK)"
    return text.replace("_", " ").replace("-", " ").title() or "Platform"


def _positions_display(values: object) -> str:
    if not isinstance(values, list) or not values:
        return "none"
    return ", ".join(str(value) for value in values)


def _measurement_coverage(
    analysis: Mapping[str, object],
    observations: Sequence[Mapping[str, object]],
    *,
    platform: object = None,
) -> dict[str, object]:
    """Describe collection facts without changing the presence denominator.

    An absent Overview is a completed observation, not a captured answer or a
    failed measurement. Answer capture and measurement completion are separate:
    an answer may have been saved before extraction failed. Missing plan/status
    information stays unknown, including in historical data.
    """

    incomplete_statuses = {
        "planned", "running", "partial", "rejected", "needs_attention",
        "failed", "cancelled",
    }
    captured = sum(
        # A no-Overview observation can retain the captured Google search-page
        # text as its source. That is evidence of absence, not an AI answer.
        row.get("status") != "not_shown"
        and isinstance(row.get("raw_answer_text"), str)
        and bool(row["raw_answer_text"].strip())
        for row in observations
    )
    not_shown = sum(
        row.get("platform") == "google_ai_overview"
        and row.get("status") == "not_shown"
        for row in observations
    )
    completed = sum(row.get("status") == "succeeded" for row in observations) + not_shown
    incomplete = sum(row.get("status") in incomplete_statuses for row in observations)
    unknown = len(observations) - completed - incomplete

    plan = _mapping(analysis.get("observation_plan_identity")).get("observations")
    missing = None
    if isinstance(plan, list) and all(
        isinstance(row, Mapping)
        and isinstance(row.get("observation_id"), str)
        and row["observation_id"]
        for row in plan
    ):
        recorded_ids = {row.get("observation_id") for row in observations}
        if platform is None:
            missing = len({row["observation_id"] for row in plan} - recorded_ids)
        else:
            # Older plan rows can omit platform. A matching recorded result
            # identifies it; an absent result with no platform cannot be assigned.
            recorded_platforms = {
                row.get("observation_id"): row.get("platform")
                for row in analysis.get("observations") or []
                if isinstance(row, Mapping)
            }
            platforms = [row.get("platform") or recorded_platforms.get(row["observation_id"]) for row in plan]
            if all(value is not None for value in platforms):
                missing = len({
                    row["observation_id"]
                    for row, value in zip(plan, platforms)
                    if value == platform
                } - recorded_ids)
    statement = (
        f"Captured answers: {captured}; Google Overview not shown: {not_shown}; "
        f"failed or incomplete measurements: {incomplete}; "
        f"missing results: {missing if missing is not None else 'not recorded'}."
    )
    if unknown:
        statement += f" Measurement status not recorded or unrecognized: {unknown}."
    return {
        "recorded_observations": len(observations),
        "completed_measurements": completed,
        "captured_answers": captured,
        "overview_not_shown": not_shown,
        "failed_or_incomplete_measurements": incomplete,
        "missing_results": missing,
        "unknown_status_measurements": unknown,
        "statement": statement,
    }


def _presence_report(
    analysis: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Build the complete authored report from v3 facts only."""

    clinic_name = _clinic_name(analysis)
    observations = [
        row
        for row in analysis.get("observations") or []
        if isinstance(row, Mapping)
    ]
    aggregate = _name_aggregate(analysis, clinic_name)
    recorded = _recorded_name_metrics(
        aggregate,
        denominator=len(observations),
    )
    visibility = _mapping(recorded["visibility"])
    match_copy = (
        "matched by exact name or verified alias"
        if _mapping(analysis.get("report_target")).get("verified_aliases")
        else "appeared by exact recorded name"
    )
    summary = (
        f"{clinic_name} {match_copy} in "
        f"{visibility.get('display')} of the recorded observations."
    )
    matching_statement = ""
    if analysis.get("report_target"):
        matching_statement = (
            " Matches use exact equality with the selected clinic name and its verified "
            "directory aliases. Matching does not change the measured names or their order."
        )
    coverage = _measurement_coverage(analysis, observations)
    rank_summary = (
        "First-mention positions are shown in runner-recorded order: "
        f"{_positions_display(recorded['recorded_positions'])}."
    )
    method = (
        "This report uses only captured answer text, grounded name occurrences "
        "and positions recorded by the observation runner. The presence denominator "
        "includes every recorded observation, including Google Overview not shown. "
        "Captured answers can also belong to incomplete measurements; missing results "
        "have no recorded observation and are excluded." + matching_statement
    )
    period = _mapping(analysis.get("collection_period"))
    collection_copy = collection_statement(period)
    boundary = (
        "It reports name presence and recorded order only; it does not establish "
        "causes, website changes, listing changes or future outcomes."
    )

    platform_rows: list[dict[str, object]] = []
    platform_fragments: dict[str, str] = {}
    for index, platform in enumerate(_platform_values(analysis)):
        platform_observations = _platform_observations(analysis, platform)
        platform_aggregate = _platform_name_aggregate(
            analysis,
            platform,
            clinic_name,
        )
        platform_recorded = _recorded_name_metrics(
            platform_aggregate,
            denominator=len(platform_observations),
        )
        label = _display_platform(platform)
        platform_visibility = _mapping(platform_recorded["visibility"])
        statement = (
            f"{label}: {clinic_name} {match_copy} in "
            f"{platform_visibility.get('display')}; the runner recorded "
            f"{platform_recorded['frequency']} grounded occurrence(s), with first "
            "positions in observation order: "
            f"{_positions_display(platform_recorded['recorded_positions'])}."
        )
        platform_rows.append(
            {
                "platform": platform,
                "label": label,
                "observation_count": len(platform_observations),
                "coverage": _measurement_coverage(
                    analysis, platform_observations, platform=platform,
                ),
                "presence": platform_recorded["visibility"],
                "occurrence_frequency": platform_recorded["frequency"],
                "first_mention_rank": {
                    "recorded_positions": platform_recorded[
                        "recorded_positions"
                    ],
                    "mean": platform_recorded["mean_position"],
                },
                "statement": statement,
            }
        )
        platform_fragments[f"presence_report.platforms[{index}].statement"] = (
            statement
        )
        platform_fragments[f"presence_report.platforms[{index}].coverage.statement"] = (
            platform_rows[-1]["coverage"]["statement"]
        )

    evidence_rows = [
        _plain(row)
        for row in analysis.get("grounded_evidence") or []
        if isinstance(row, Mapping)
        and (not analysis.get("report_target") or row.get("surface_form") in report_target_names(analysis))
    ]
    shown_evidence_count = min(5, len(evidence_rows))
    evidence_note = (
        f"Showing {shown_evidence_count} of {len(evidence_rows)} grounded "
        "occurrence(s) in recorded order. The data artifact retains every "
        "captured answer and its exact recorded character and byte offsets."
    )
    report = {
        "version": PRESENCE_REPORT_VERSION,
        "title": "Free Visibility Snapshot",
        "clinic_name": clinic_name,
        **({"report_target": _plain(analysis["report_target"])} if analysis.get("report_target") else {}),
        "method": method,
        "summary": summary,
        "coverage": coverage,
        "rank_summary": rank_summary,
        "boundary": boundary,
        "evidence_note": evidence_note,
        "overall": {
            "presence": recorded["visibility"],
            "occurrence_frequency": recorded["frequency"],
            "first_mention_rank": {
                "recorded_positions": recorded["recorded_positions"],
                "mean": recorded["mean_position"],
            },
        },
        "platforms": platform_rows,
        "grounded_evidence": evidence_rows,
    }
    fragments = {
        "presence_report.method": method,
        "presence_report.summary": summary,
        "presence_report.coverage.statement": coverage["statement"],
        "presence_report.rank_summary": rank_summary,
        "presence_report.boundary": boundary,
        "presence_report.evidence_note": evidence_note,
        **platform_fragments,
    }
    if period:
        report["collection_period"] = period
        report["collection_statement"] = collection_copy
        fragments["presence_report.collection_statement"] = collection_copy
    return report, fragments


def _registration_presentation(
    analysis: Mapping[str, object],
) -> dict[str, object]:
    """Build a truth-only report plus the store's required structural fields.

    Presence and first-position metrics are exact calculations from runner
    rows. Fields the existing publication verifier requires but v3 does not
    measure are isolated and labelled as numeric sentinels. They are never
    rendered as report copy. The complete v3 analysis is embedded unchanged in
    ``presence_analysis``.
    """

    scope = _mapping(analysis.get("frozen_scope"))
    clinic_name = _clinic_name(analysis)
    scope_hash = str(analysis.get("scope_hash") or "")
    prompt_pack_hash = str(analysis.get("prompt_pack_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", scope_hash):
        raise SnapshotRenderBridgeError("analysis has no valid frozen scope_hash")
    if not prompt_pack_hash:
        raise SnapshotRenderBridgeError("analysis has no frozen prompt_pack_hash")

    scope_definition = _mapping(scope)
    scope_definition["scope_hash"] = scope_hash
    scope_definition["prompt_pack_hash"] = prompt_pack_hash
    subject = _subject(scope)
    catchment = _catchment(scope)
    branch = _branch(scope)
    presentation_scopes = []
    for order, platform in enumerate(_platform_values(analysis), 1):
        observations = _platform_observations(analysis, platform)
        denominator = len(observations)
        aggregate = _platform_name_aggregate(
            analysis,
            platform,
            clinic_name,
        )
        recorded = _recorded_name_metrics(
            aggregate,
            denominator=denominator,
        )
        named_prompt_count, named_run_count, named_intent_count = _identity_counts(
            observations,
            clinic_name=clinic_name,
            names=report_target_names(analysis),
        )
        all_prompt_ids = _distinct_identity_values(
            observations,
            "prompt_id",
            "id",
        )
        all_run_ids = _distinct_identity_values(
            observations,
            "run",
            "run_ordinal",
        )
        all_intent_ids = _distinct_identity_values(
            observations,
            "semantic_prompt_id",
        )
        presentation_scopes.append(
            {
                "scope_id": f"{analysis.get('run_id')}:{platform}",
                "order": order,
                "subject": subject,
                "treatment": subject,
                "catchment": catchment,
                "branch": branch,
                "platform": platform,
                "collection": {
                    "planned": denominator,
                    "valid": denominator,
                    "coverage": 1.0 if denominator else 0.0,
                    "coverage_display": (
                        f"{denominator}/{denominator} (100.0%)"
                        if denominator
                        else "0/0 (0.0%)"
                    ),
                    "status": "complete" if denominator else "not_measured",
                },
                "clinic": {
                    "name": clinic_name,
                    "metrics": {
                        "visibility": recorded["visibility"],
                        "qualified_recommendation": _metric(0, 0),
                        "first_named": recorded["first_named"],
                        "mean_position_when_named": recorded["mean_position"],
                        "named_prompt_count": named_prompt_count,
                        "named_run_count": named_run_count,
                        "named_intent_count": named_intent_count,
                    },
                },
                "competitors": [],
                "strongest_competitor": None,
                "gaps": {
                    "appearance_percentage_points": 0.0,
                    "qualified_percentage_points": 0.0,
                },
                "compatibility_only_unmeasured": {
                    "numeric_sentinel_count": 3,
                    "reason": (
                        "Numeric sentinels required by the existing publication "
                        "verifier; they are not observed measures and are not rendered."
                    ),
                },
                "repeatability": {
                    "prompt_count": len(all_prompt_ids),
                    "run_count": len(all_run_ids),
                    "intent_count": len(all_intent_ids),
                    "status": "recorded",
                    "statement": (
                        "Counts retain the runner's prompt, run and intent identities."
                    ),
                },
                "evidence": {
                    "passes": False,
                    "blockers": [
                        {
                            "code": "presence_only",
                            "message": (
                                "This path reports grounded presence and recorded order only."
                            ),
                        }
                    ],
                },
                "verdict": {"status": "insufficient_evidence"},
                "preliminary_competitiveness": {
                    "status": "not_measured",
                    "basis": "runner_presence_only",
                },
                "evidence_examples": [],
            }
        )
    if not presentation_scopes:
        raise SnapshotRenderBridgeError(
            "a succeeded v3 candidate needs at least one recorded platform"
        )

    raw = {
        "schema_version": PRESENTATION_V4_SCHEMA_VERSION,
        "purpose": EvidencePurpose.FREE_SNAPSHOT_CONFIRMATION.value,
        "report_id": analysis.get("report_id") or analysis.get("run_id"),
        "scope_hash": scope_hash,
        "results_hash": analysis.get("results_hash"),
        "prompt_pack_hash": prompt_pack_hash,
        "scope_definition": scope_definition,
        "scopes": presentation_scopes,
        "primary_finding": {"scope_id": presentation_scopes[0]["scope_id"]},
        # This exact enum is required solely for the existing verifier's
        # deterministic compatibility hash. It is not rendered report copy.
        "overall_verdict": "More evidence required",
        "boundary": {
            "does_not_establish": [
                "This bounded report does not establish causes or outcomes."
            ]
        },
        "quality": {"publication_state": "partial"},
        "analysis_schema_version": analysis.get("schema_version"),
        "analysis_version": analysis.get("analysis_version"),
        "presence_analysis": _plain(analysis),
    }
    presence_report, fragments = _presence_report(analysis)
    protected_terms = {
        clinic_name,
        *(str(value) for value in _platform_values(analysis)),
    }
    copy_policy = scan_client_copy(
        fragments,
        protected_terms=protected_terms,
    )
    assert_copy_policy(copy_policy)
    compatibility_model = build_presentation_model(raw)
    compatibility_copy_hash = str(
        compatibility_model.get("copy_hash") or ""
    )
    if not re.fullmatch(r"[0-9a-f]{64}", compatibility_copy_hash):
        raise SnapshotRenderBridgeError(
            "publication verifier did not produce a compatibility copy hash"
        )
    presence_copy_hash = _canonical_sha256(presence_report)
    raw["presence_report"] = presence_report
    raw["presence_copy_hash"] = presence_copy_hash
    raw["presentation"] = {
        "version": PRESENCE_REPORT_VERSION,
        "copy_hash": compatibility_copy_hash,
        "presence_copy_hash": presence_copy_hash,
        "copy_hash_role": "publication_store_verifier_compatibility_only",
        "compatibility_note": (
            "The existing store recomputes this structural hash. It is not a "
            "hash of the client-facing presence report; presence_copy_hash is."
        ),
    }
    raw["copy_policy"] = copy_policy.as_dict()
    raw["artifact_identity"] = {
        "report_id": raw["report_id"],
        "scope_hash": scope_hash,
        "prompt_pack_hash": prompt_pack_hash,
        "results_hash": raw["results_hash"],
        "copy_hash": compatibility_copy_hash,
        "presence_copy_hash": presence_copy_hash,
    }
    return raw


def build_registration_presentation(
    analysis: Mapping[str, object],
) -> dict[str, object] | None:
    checked = _analysis_mapping(analysis)
    if checked.get("candidate_eligible") is not True:
        return None
    return _registration_presentation(checked)


def build_renderer_payload(
    analysis: Mapping[str, object],
) -> dict[str, object] | None:
    """Return the exact mapping supplied to the four-artifact renderer."""

    return build_registration_presentation(analysis)


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _verified_presence_report(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    report = _mapping(payload.get("presence_report"))
    if report.get("version") != PRESENCE_REPORT_VERSION:
        raise SnapshotRenderBridgeError(
            f"candidate has no {PRESENCE_REPORT_VERSION} report"
        )
    declared = str(payload.get("presence_copy_hash") or "")
    nested = _mapping(payload.get("presentation"))
    identity = _mapping(payload.get("artifact_identity"))
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise SnapshotRenderBridgeError("candidate has no presence_copy_hash")
    if any(
        str(value or "") != declared
        for value in (
            nested.get("presence_copy_hash"),
            identity.get("presence_copy_hash"),
        )
    ):
        raise SnapshotRenderBridgeError(
            "candidate presence_copy_hash identity is inconsistent"
        )
    if _canonical_sha256(report) != declared:
        raise SnapshotRenderBridgeError(
            "candidate presence_copy_hash does not match the presence report"
        )
    return report, declared


def _identity_attributes(payload: Mapping[str, object], page: int) -> str:
    identity = _mapping(payload.get("artifact_identity"))
    attributes = {
        "data-report-page": page,
        "data-report-id": identity.get("report_id"),
        "data-scope-hash": identity.get("scope_hash"),
        "data-prompt-pack-hash": identity.get("prompt_pack_hash"),
        "data-results-hash": identity.get("results_hash"),
        "data-copy-hash": identity.get("copy_hash"),
        "data-presence-copy-hash": identity.get("presence_copy_hash"),
    }
    return " ".join(
        f'{name}="{_escape(value)}"' for name, value in attributes.items()
    )


def _metric_display(value: object) -> str:
    metric = _mapping(value)
    display = metric.get("display")
    if isinstance(display, str) and display:
        return display
    numerator = metric.get("numerator")
    denominator = metric.get("denominator")
    rate = metric.get("rate")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        return f"{numerator}/{denominator} ({rate:.1%})"
    return f"{numerator}/{denominator}"


def _mean_display(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "No recorded value"
    return f"{value:g}"


def _offset_display(row: Mapping[str, object], prefix: str) -> str:
    start = row.get(f"{prefix}_start")
    end = row.get(f"{prefix}_end")
    if start is None and end is None:
        return "null"
    return f"{start}:{end}"


def build_presence_html(
    payload: Mapping[str, object],
    *,
    internal_candidate: bool = True,
) -> str:
    """Return the escaped, self-contained, presence-only three-page report."""

    report, presence_copy_hash = _verified_presence_report(payload)
    identity = _mapping(payload.get("artifact_identity"))
    required_identity = (
        "report_id",
        "scope_hash",
        "prompt_pack_hash",
        "results_hash",
        "copy_hash",
    )
    missing = [key for key in required_identity if not identity.get(key)]
    if missing:
        raise SnapshotRenderBridgeError(
            "candidate artifact identity is incomplete: " + ", ".join(missing)
        )
    title_identity = " ".join(
        f"{key}={identity.get(key)}"
        for key in (*required_identity, "presence_copy_hash")
    )
    overall = _mapping(report.get("overall"))
    first_rank = _mapping(overall.get("first_mention_rank"))
    platforms = [
        row
        for row in report.get("platforms") or []
        if isinstance(row, Mapping)
    ]
    platform_cards = "".join(
        (
            '<article class="platform-card">'
            f'<h3>{_escape(row.get("label"))}</h3>'
            f'<p data-copy-policy="narrative">{_escape(row.get("statement"))}</p>'
            '<dl class="compact">'
            f'<div><dt>Recorded observations</dt><dd>{_escape(row.get("observation_count"))}</dd></div>'
            f'<div><dt>Exact-name presence</dt><dd>{_escape(_metric_display(row.get("presence")))}</dd></div>'
            f'<div><dt>Grounded occurrences</dt><dd>{_escape(row.get("occurrence_frequency"))}</dd></div>'
            f'<div><dt>Mean first position</dt><dd>{_escape(_mean_display(_mapping(row.get("first_mention_rank")).get("mean")))}</dd></div>'
            "</dl>"
            f'<p class="coverage" data-copy-policy="narrative">{_escape(_mapping(row.get("coverage")).get("statement") or "Answer and outcome breakdown not recorded.")}</p>'
            "</article>"
        )
        for row in platforms
    )
    evidence = [
        row
        for row in report.get("grounded_evidence") or []
        if isinstance(row, Mapping)
    ]
    evidence_cards = "".join(
        (
            '<article class="evidence-card">'
            '<div class="evidence-head">'
            f'<strong>{_escape(row.get("surface_form"))}</strong>'
            f'<span>position {_escape(row.get("position"))}</span>'
            "</div>"
            f'<p class="answer">{_escape(str(row.get("raw_answer_text") or "")[:160])}</p>'
            '<p class="offsets">'
            f'observation {_escape(row.get("observation_id"))} · '
            f'characters {_escape(_offset_display(row, "character"))} · '
            f'bytes {_escape(_offset_display(row, "byte"))}'
            "</p></article>"
        )
        for row in evidence[:5]
    )
    if not evidence_cards:
        evidence_cards = (
            '<p class="empty">No grounded occurrence of the clinic name was recorded.</p>'
        )
    identity_rows = "".join(
        f"<div><span>{_escape(key)}</span><code>{_escape(value)}</code></div>"
        for key, value in (
            ("report_id", identity.get("report_id")),
            ("scope_hash", identity.get("scope_hash")),
            ("prompt_pack_hash", identity.get("prompt_pack_hash")),
            ("results_hash", identity.get("results_hash")),
            ("store_compatibility_hash", identity.get("copy_hash")),
            ("presence_copy_hash", presence_copy_hash),
        )
    )
    watermark = (
        '<div class="watermark">INTERNAL CANDIDATE</div>'
        if internal_candidate
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title_identity)}</title>
  <style>
    @page {{ size: A4; margin: 0; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #e7e8e4; color: #18211d; font-family: Arial, Helvetica, sans-serif; }}
    .report-page {{ width: 210mm; height: 297mm; padding: 18mm 17mm 15mm; background: #f7f5ee; page-break-after: always; overflow: hidden; position: relative; display: flex; flex-direction: column; }}
    .report-page:last-child {{ page-break-after: auto; }}
    .watermark {{ position: absolute; right: 13mm; top: 8mm; color: #9b5d38; font-size: 8pt; font-weight: 700; letter-spacing: .12em; }}
    .kicker {{ color: #557064; font-size: 9pt; font-weight: 700; letter-spacing: .13em; text-transform: uppercase; margin: 0 0 8mm; }}
    h1 {{ font-size: 29pt; line-height: 1.02; margin: 0 0 5mm; max-width: 155mm; }}
    h2 {{ font-size: 22pt; line-height: 1.08; margin: 0 0 7mm; }}
    h3 {{ font-size: 14pt; margin: 0 0 3mm; }}
    p {{ font-size: 10.5pt; line-height: 1.48; margin: 0 0 4mm; }}
    .clinic {{ font-size: 16pt; color: #315748; margin-bottom: 12mm; }}
    .headline {{ font-size: 16pt; line-height: 1.35; max-width: 165mm; margin-bottom: 7mm; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 5mm; margin: 8mm 0 10mm; }}
    .metric {{ border: 1px solid #b9c4bc; border-radius: 3mm; padding: 5mm; background: #fff; min-height: 31mm; }}
    .metric span {{ display: block; color: #607068; font-size: 8pt; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 3mm; }}
    .metric strong {{ font-size: 19pt; line-height: 1.1; overflow-wrap: anywhere; }}
    .boundary {{ margin-top: auto; padding: 5mm; border-left: 1.5mm solid #315748; background: #ecf0ec; }}
    .platform-list {{ display: grid; grid-template-columns: 1fr; gap: 5mm; }}
    .platform-card {{ border: 1px solid #bdc7c0; border-radius: 3mm; padding: 5mm 6mm; background: #fff; }}
    .platform-card p {{ font-size: 9.5pt; }}
    .platform-card .coverage {{ font-size: 8.5pt; margin: 3mm 0 0; }}
    .compact {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; margin: 4mm 0 0; }}
    .compact div {{ border-top: 1px solid #d9ded9; padding-top: 2mm; min-width: 0; }}
    dt {{ color: #65736c; font-size: 7.5pt; line-height: 1.2; }}
    dd {{ margin: 1mm 0 0; font-size: 10pt; font-weight: 700; overflow-wrap: anywhere; }}
    .evidence-list {{ display: grid; grid-template-columns: 1fr 1fr; gap: 3mm; }}
    .evidence-card {{ border: 1px solid #c7cec9; border-radius: 2mm; padding: 3mm 4mm; background: #fff; min-width: 0; }}
    .evidence-head {{ display: flex; justify-content: space-between; gap: 3mm; font-size: 9pt; }}
    .answer {{ font-size: 8.5pt; line-height: 1.35; margin: 2mm 0; overflow-wrap: anywhere; }}
    .offsets {{ color: #627069; font: 7.2pt/1.35 monospace; margin: 0; overflow-wrap: anywhere; }}
    .identity {{ margin-top: auto; border-top: 1px solid #aeb9b1; padding-top: 3mm; }}
    .identity div {{ display: grid; grid-template-columns: 34mm 1fr; gap: 3mm; font-size: 6.8pt; line-height: 1.3; margin-bottom: .8mm; min-width: 0; }}
    .identity span {{ color: #66736c; }}
    .identity code {{ font-family: monospace; overflow-wrap: anywhere; word-break: break-all; }}
    .compatibility-note {{ color: #66736c; font-size: 7.5pt; margin-top: 2mm; }}
    .empty {{ border: 1px solid #c7cec9; padding: 5mm; background: #fff; }}
  </style>
</head>
<body>
  <section class="report-page" {_identity_attributes(payload, 1)}>
    {watermark}
    <p class="kicker">Free Visibility Snapshot · recorded presence and order</p>
    <h1>{_escape(report.get("title"))}</h1>
    <p class="clinic">{_escape(report.get("clinic_name"))}</p>
    <p class="headline" data-copy-policy="narrative">{_escape(report.get("summary"))}</p>
    <p data-copy-policy="narrative">{_escape(_mapping(report.get("coverage")).get("statement") or "Answer and outcome breakdown not recorded.")}</p>
    <div class="metric-grid">
      <div class="metric"><span>Exact-name presence</span><strong>{_escape(_metric_display(overall.get("presence")))}</strong></div>
      <div class="metric"><span>Grounded occurrences</span><strong>{_escape(overall.get("occurrence_frequency"))}</strong></div>
      <div class="metric"><span>Mean first position</span><strong>{_escape(_mean_display(first_rank.get("mean")))}</strong></div>
    </div>
    <p data-copy-policy="narrative">{_escape(report.get("rank_summary"))}</p>
    <p data-copy-policy="narrative">{_escape(report.get("method"))}</p>
    <p data-copy-policy="narrative">{_escape(report.get("collection_statement"))}</p>
    <div class="boundary"><p data-copy-policy="narrative">{_escape(report.get("boundary"))}</p></div>
  </section>
  <section class="report-page" {_identity_attributes(payload, 2)}>
    {watermark}
    <p class="kicker">Platform breakdown</p>
    <h2>Recorded presence by platform</h2>
    <div class="platform-list">{platform_cards}</div>
  </section>
  <section class="report-page" {_identity_attributes(payload, 3)}>
    {watermark}
    <p class="kicker">Grounded evidence</p>
    <h2>Recorded answer evidence</h2>
    <p data-copy-policy="narrative">{_escape(report.get("evidence_note"))}</p>
    <div class="evidence-list">{evidence_cards}</div>
    <div class="identity">{identity_rows}</div>
    <p class="compatibility-note">The store compatibility hash is shown for publication verification only; the presence-copy hash binds this report.</p>
  </section>
</body>
</html>
"""


def _pdf_contains_token(path: Path, token: str) -> bool:
    try:
        from pypdf import PdfReader

        extracted = "\n".join(
            page.extract_text() or "" for page in PdfReader(str(path)).pages
        )
    except Exception as exc:
        raise SnapshotRenderBridgeError(
            f"candidate PDF text cannot be inspected: {exc}"
        ) from exc
    return re.sub(r"\s+", "", token) in re.sub(r"\s+", "", extracted)


def _presence_validation_text(
    payload: Mapping[str, object],
    *,
    metadata_normalized: bool,
) -> str:
    identity = _mapping(payload.get("artifact_identity"))
    return "\n".join(
        [
            "FREE VISIBILITY SNAPSHOT PRESENCE V3 VALIDATION",
            f"Report ID: {identity.get('report_id')}",
            "",
            "COPY POLICY PASSED: YES",
            "RENDER PASSED: YES",
            "PAGES: 3 (A4 PORTRAIT)",
            "PDF METADATA NORMALIZED: "
            + ("YES" if metadata_normalized else "NO"),
            "ARTIFACT AGREEMENT PASSED: YES",
            "PRESENCE COPY AGREEMENT PASSED: YES",
            "",
            f"Scope SHA-256: {identity.get('scope_hash')}",
            f"Prompt pack SHA-256: {identity.get('prompt_pack_hash')}",
            f"Results SHA-256: {identity.get('results_hash')}",
            f"Store compatibility SHA-256: {identity.get('copy_hash')}",
            f"Presence copy SHA-256: {identity.get('presence_copy_hash')}",
            "",
        ]
    )


def render_presence_snapshot_artifacts(
    payload: Mapping[str, object],
    *,
    output_dir: str | Path,
    internal_candidate: bool = True,
    chromium_executable: str | Path | None = None,
) -> dict[str, Path]:
    """Render an atomic four-artifact presence bundle with public report tools."""

    from clinic_geo.reporting.renderer import (
        normalize_pdf_metadata,
        render_pdf,
    )
    from clinic_geo.reporting.visibility_snapshot import (
        report_stem,
        validate_v2_artifact_agreement,
    )

    source = _mapping(payload)
    report, presence_copy_hash = _verified_presence_report(source)
    html_document = build_presence_html(
        source,
        internal_candidate=internal_candidate,
    )
    protected_terms = {
        report.get("clinic_name"),
        *(
            row.get("platform")
            for row in report.get("platforms") or []
            if isinstance(row, Mapping)
        ),
    }
    rendered_policy = scan_rendered_narrative(
        html_document,
        protected_terms=protected_terms,
    )
    assert_copy_policy(rendered_policy)
    stored_policy = _mapping(source.get("copy_policy"))
    if stored_policy.get("passed") is not True:
        raise SnapshotRenderBridgeError(
            "candidate presence copy did not pass the stored copy policy"
        )

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    stem = report_stem(str(report.get("clinic_name") or ""))
    destinations = {
        "pdf": output_path / f"{stem}_visibility_snapshot.pdf",
        "html": output_path / f"{stem}_visibility_snapshot.html",
        "data": output_path / f"{stem}_visibility_snapshot_data.json",
        "validation": output_path / f"{stem}_visibility_snapshot_validation.txt",
    }
    with tempfile.TemporaryDirectory(
        prefix=f".{stem}-presence-",
        dir=output_path,
    ) as temporary_name:
        temporary = Path(temporary_name)
        temporary_paths = {
            kind: temporary / destination.name
            for kind, destination in destinations.items()
        }
        temporary_paths["html"].write_text(
            html_document,
            encoding="utf-8",
        )
        layout = render_pdf(
            html_document,
            temporary_paths["pdf"],
            chromium_executable=chromium_executable,
        )
        metadata_normalized = normalize_pdf_metadata(
            temporary_paths["pdf"],
            payload=source,
        )
        agreement = validate_v2_artifact_agreement(
            source,
            html_document,
            pdf_path=temporary_paths["pdf"],
        )
        if agreement.get("passed") is not True:
            raise SnapshotRenderBridgeError(
                "candidate artifact identity agreement did not pass"
            )
        presence_agreement = (
            presence_copy_hash in html_document
            and _pdf_contains_token(
                temporary_paths["pdf"],
                presence_copy_hash,
            )
            and _canonical_sha256(report) == presence_copy_hash
        )
        if not presence_agreement:
            raise SnapshotRenderBridgeError(
                "presence report disagrees across data, HTML and PDF"
            )
        prepared = _mapping(source)
        prepared["artifact_validation"] = {
            "render_passed": layout.page_count == 3,
            "copy_policy_passed": True,
            "artifact_agreement_passed": True,
            "presence_copy_agreement_passed": True,
            "page_count": layout.page_count,
            "metadata_normalized": metadata_normalized,
        }
        temporary_paths["data"].write_text(
            json.dumps(
                prepared,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_paths["validation"].write_text(
            _presence_validation_text(
                prepared,
                metadata_normalized=metadata_normalized,
            ),
            encoding="utf-8",
        )
        for kind, destination in destinations.items():
            os.replace(temporary_paths[kind], destination)
    return destinations


def _offsets_are_exact(analysis: Mapping[str, object]) -> bool:
    rows = analysis.get("grounded_evidence")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        answer = row.get("raw_answer_text")
        surface = row.get("surface_form")
        offsets = (
            row.get("character_start"),
            row.get("character_end"),
            row.get("byte_start"),
            row.get("byte_end"),
        )
        if (
            not isinstance(answer, str)
            or not isinstance(surface, str)
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in offsets
            )
        ):
            return False
        character_start, character_end, byte_start, byte_end = offsets
        if answer[character_start:character_end] != surface:
            return False
        if (
            answer.encode("utf-8")[byte_start:byte_end]
            != surface.encode("utf-8")
        ):
            return False
    return True


def _relative_artifacts(
    run_dir: Path,
    artifacts: Mapping[str, Path],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for kind, path in artifacts.items():
        try:
            result[kind] = str(path.resolve().relative_to(run_dir.resolve()))
        except ValueError as exc:
            raise SnapshotRenderBridgeError(
                "candidate artifacts must live inside the Snapshot run directory"
            ) from exc
    return result


def _read_presentation(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotRenderBridgeError(
            f"candidate data artifact cannot be read: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise SnapshotRenderBridgeError(
            "candidate data artifact must contain an object"
        )
    return value


def build_candidate_payloads(
    analysis: Mapping[str, object],
    *,
    run_dir: str | Path,
    output_dir: str | Path | None = None,
    renderer: Renderer | None = None,
    chromium_executable: str | Path | None = None,
) -> CandidatePayloads | None:
    """Render and return the four arguments used by candidate registration."""

    checked = _analysis_mapping(analysis)
    if checked.get("candidate_eligible") is not True:
        return None
    run_path = Path(run_dir).expanduser().resolve()
    candidate_path = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else run_path / "report-revisions" / str(checked["results_hash"])
    )
    try:
        candidate_path.relative_to(run_path)
    except ValueError as exc:
        raise SnapshotRenderBridgeError(
            "candidate output must be inside the Snapshot run directory"
        ) from exc

    input_presentation = _registration_presentation(checked)
    if renderer is None:
        renderer = render_presence_snapshot_artifacts
    rendered = render_report_revision(
        candidate_path,
        results_hash=str(checked["results_hash"]),
        payload=input_presentation,
        renderer=renderer,
        chromium_executable=chromium_executable,
    )
    if not isinstance(rendered, Mapping):
        raise SnapshotRenderBridgeError(
            "candidate renderer did not return an artifact mapping"
        )
    artifacts = {
        str(kind): Path(path).expanduser().resolve()
        for kind, path in rendered.items()
    }
    missing = sorted(REQUIRED_CANDIDATE_ARTIFACTS - set(artifacts))
    if missing:
        raise SnapshotRenderBridgeError(
            "candidate renderer omitted: " + ", ".join(missing)
        )
    for kind in REQUIRED_CANDIDATE_ARTIFACTS:
        if not artifacts[kind].is_file():
            raise SnapshotRenderBridgeError(
                f"candidate {kind} artifact is missing: {artifacts[kind]}"
            )

    # This is the critical mirror of snapshot_workflow.py: the renderer-owned
    # normalized JSON, not the pre-render input, is what registration reviews.
    presentation = _read_presentation(artifacts["data"])
    validation = artifacts["validation"].read_text(
        encoding="utf-8",
        errors="replace",
    )
    validation_lines = {
        line.strip().casefold()
        for line in validation.splitlines()
        if line.strip()
    }
    artifact_validation = _mapping(presentation.get("artifact_validation"))
    copy_policy = _mapping(presentation.get("copy_policy"))
    copy_passed = (
        copy_policy.get("passed") is True
        and artifact_validation.get("copy_policy_passed") is True
        and "copy policy passed: yes" in validation_lines
    )
    render_complete = (
        artifact_validation.get("render_passed") is True
        and artifact_validation.get("page_count") == 3
        and artifact_validation.get("metadata_normalized") is True
        and "render passed: yes" in validation_lines
        and "pages: 3 (a4 portrait)" in validation_lines
        and "pdf metadata normalized: yes" in validation_lines
    )
    artifact_agreement = (
        artifact_validation.get("artifact_agreement_passed") is True
        and artifact_validation.get("presence_copy_agreement_passed") is True
        and "artifact agreement passed: yes" in validation_lines
        and "presence copy agreement passed: yes" in validation_lines
    )
    identity_complete = all(
        isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
        for value in (
            checked.get("scope_hash"),
            checked.get("prompt_pack_hash"),
            checked.get("results_hash"),
        )
    )
    observations = [
        row
        for row in checked.get("observations") or []
        if isinstance(row, Mapping)
    ]
    collection_complete = checked.get("run_status") == "succeeded" and all(
        row.get("status") in {"succeeded", "not_shown"}
        for row in observations
    )
    extraction_complete = all(
        row.get("extraction_status") in {"succeeded", "not_required"}
        for row in observations
    )
    integrity_complete = _offsets_are_exact(checked)
    blockers = []
    for passed, message in (
        (collection_complete, "Runner collection is not complete."),
        (extraction_complete, "Runner extraction is not complete."),
        (identity_complete, "Frozen candidate identity is incomplete."),
        (integrity_complete, "Grounded offsets do not verify against captured answers."),
        (copy_passed, "Candidate copy checks did not pass."),
        (render_complete, "Candidate rendering did not pass."),
        (artifact_agreement, "Candidate artifact agreement did not pass."),
    ):
        if not passed:
            blockers.append(message)
    quality = {
        "purpose": EvidencePurpose.FREE_SNAPSHOT_CONFIRMATION.value,
        "collection_complete": collection_complete,
        "extraction_complete": extraction_complete,
        "identity_complete": identity_complete,
        "integrity_complete": integrity_complete,
        "copy_check_passed": copy_passed,
        "render_complete": render_complete,
        "artifact_agreement_complete": artifact_agreement,
        "release_blockers": blockers,
        "planned_observations": len(observations),
        "valid_observations": len(observations),
    }
    return CandidatePayloads(
        analysis=checked,
        presentation=presentation,
        quality=quality,
        candidate_artifacts=_relative_artifacts(run_path, artifacts),
    )


def render_and_register_candidate(
    store: object,
    analysis: Mapping[str, object],
    *,
    run_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    renderer: Renderer | None = None,
    chromium_executable: str | Path | None = None,
    host_test_mode: bool | None = None,
) -> dict[str, object] | None:
    """Render, then call the existing store's public registration seam."""

    checked = _analysis_mapping(analysis)
    if checked.get("candidate_eligible") is not True:
        return None
    frozen_scope = _mapping(checked.get("frozen_scope"))
    if host_test_mode is None:
        if frozen_scope.get("execution_policy_version"):
            raise SnapshotRenderBridgeError("Module measurements require verified host publication mode")
        host_test_mode = frozen_scope.get("test_mode", False)
    if type(host_test_mode) is not bool:
        raise SnapshotRenderBridgeError("Host test mode must be a Boolean")
    report_id = str(checked.get("report_id") or checked.get("run_id") or "")
    if run_dir is None:
        resolve_run_dir = getattr(store, "run_dir", None)
        if not callable(resolve_run_dir):
            raise SnapshotRenderBridgeError("store does not expose run_dir")
        run_dir = resolve_run_dir(report_id)
    payloads = build_candidate_payloads(
        checked,
        run_dir=run_dir,
        output_dir=output_dir,
        renderer=renderer,
        chromium_executable=chromium_executable,
    )
    assert payloads is not None
    register = getattr(store, "register_candidate", None)
    if not callable(register):
        raise SnapshotRenderBridgeError("store does not expose register_candidate")
    return register(
        report_id,
        analysis=payloads.analysis,
        presentation=payloads.presentation,
        quality=payloads.quality,
        candidate_artifacts=payloads.candidate_artifacts,
        test_mode=host_test_mode,
        operational_state="complete",
    )


build_candidate = build_candidate_payloads


__all__ = [
    "CandidatePayloads",
    "PRESENCE_REPORT_VERSION",
    "RENDER_DATA_SCHEMA_VERSION",
    "REQUIRED_CANDIDATE_ARTIFACTS",
    "SnapshotRenderBridgeError",
    "build_candidate",
    "build_candidate_payloads",
    "build_presence_html",
    "build_registration_presentation",
    "build_renderer_payload",
    "render_presence_snapshot_artifacts",
    "render_and_register_candidate",
]
