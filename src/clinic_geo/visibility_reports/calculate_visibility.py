"""Calculate presence, position and frequency from recorded clinic names.

Use the recorded names, their exact positions and every occurrence, including
duplicates. Do not change names, correct positions or fill missing results.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from clinic_geo.evidence.purpose import EvidencePurpose
from .collection_period import collection_period

from .contracts_v3 import (
    SNAPSHOT_OBSERVATIONS_V3,
    SnapshotObservationsV3,
    SnapshotObservationsV3Error,
)


ANALYSIS_VERSION = "snapshot-analysis-v3"
ANALYSIS_SCHEMA_VERSION = "free_snapshot.analysis.v3.presence"
ANALYSIS_V3_SCHEMA_VERSION = ANALYSIS_SCHEMA_VERSION
ANALYSIS_V3_PRESENCE_SCHEMA_VERSION = ANALYSIS_SCHEMA_VERSION
NON_PUBLISHABLE_STATE = "non_publishable"
CANDIDATE_READY_STATE = "candidate_ready"

# SnapshotPublicationStore verifies candidates with this established canonical
# envelope.  Keeping the same exclusions preserves its immutable hash graph.
_RESULTS_HASH_EXCLUDED_FIELDS = frozenset(
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


class SnapshotAnalysisV3Error(ValueError):
    """A v3 prompt measurements document cannot be scored without changing it."""


def _plain(value: Any) -> Any:
    """Return a JSON-shaped copy without normalizing evidence values."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_plain(item) for item in value]
    return value


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_results_hash(analysis: Mapping[str, object]) -> str:
    """Hash the complete result-bearing v3 document.

    Carried source annotations are deliberately inside this integrity hash.
    They are excluded only from ``scoring_input_sha256`` and
    ``metrics_sha256``, because they are not scoring inputs.
    """

    payload = deepcopy(dict(analysis))
    for field in _RESULTS_HASH_EXCLUDED_FIELDS:
        payload.pop(field, None)
    return _canonical_sha256(payload)


def verify_results_hash(analysis: Mapping[str, object]) -> bool:
    declared = str(analysis.get("results_hash") or "")
    return bool(declared) and hmac.compare_digest(
        declared,
        compute_results_hash(analysis),
    )


def _document_mapping(
    document: SnapshotObservationsV3 | Mapping[str, object],
) -> dict[str, object]:
    try:
        if isinstance(document, SnapshotObservationsV3):
            return document.to_mapping()
        if isinstance(document, Mapping):
            return SnapshotObservationsV3.from_mapping(document).to_mapping()
    except SnapshotObservationsV3Error as exc:
        raise SnapshotAnalysisV3Error(str(exc)) from exc
    raise SnapshotAnalysisV3Error(
        "expected SnapshotObservationsV3 or a snapshot-observations-v3 mapping"
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean_recorded_position(values: Sequence[object]) -> float | None:
    """Average only a complete set of runner-recorded integer positions."""

    if not values or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in values
    ):
        return None
    return sum(values) / len(values)


def _occurrences(observation: Mapping[str, object]) -> list[dict[str, object]]:
    raw = observation.get("grounded_occurrences")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _prompt_summary(observation: Mapping[str, object]) -> dict[str, object]:
    occurrences = _occurrences(observation)
    return {
        "presence": bool(occurrences),
        "first_mention_rank": (
            occurrences[0].get("position") if occurrences else None
        ),
        "frequency": len(occurrences),
    }


def _summary(observations: Sequence[Mapping[str, object]]) -> dict[str, object]:
    prompt_summaries = [_prompt_summary(row) for row in observations]
    recorded_positions = [
        row["first_mention_rank"]
        for row in prompt_summaries
        if row["presence"]
    ]
    present = sum(bool(row["presence"]) for row in prompt_summaries)
    denominator = len(observations)
    return {
        "presence": {
            "numerator": present,
            "denominator": denominator,
            "rate": _rate(present, denominator),
        },
        "first_mention_rank": {
            "recorded_positions": recorded_positions,
            "mean": _mean_recorded_position(recorded_positions),
        },
        "frequency": {
            "occurrences": sum(int(row["frequency"]) for row in prompt_summaries)
        },
    }


def _name_occurrences(
    observation: Mapping[str, object],
    surface_form: object,
) -> list[dict[str, object]]:
    # Exact equality is intentional: no case folding, aliasing, or canonical ID
    # lookup is allowed in the runner-recorded scoring path.
    return [
        occurrence
        for occurrence in _occurrences(observation)
        if occurrence.get("surface_form") == surface_form
    ]


def _name_summary(
    observations: Sequence[Mapping[str, object]],
    surface_form: object,
) -> dict[str, object]:
    positions: list[object] = []
    frequency = 0
    present = 0
    for observation in observations:
        matches = _name_occurrences(observation, surface_form)
        frequency += len(matches)
        if not matches:
            continue
        present += 1
        positions.append(matches[0].get("position"))
    denominator = len(observations)
    return {
        "presence": {
            "numerator": present,
            "denominator": denominator,
            "rate": _rate(present, denominator),
        },
        "first_mention_rank": {
            "recorded_positions": positions,
            "mean": _mean_recorded_position(positions),
        },
        "frequency": {"occurrences": frequency},
    }


def _distinct_surface_forms_in_first_occurrence_order(
    observations: Sequence[Mapping[str, object]],
) -> list[object]:
    """Return the exact-surface keys used only by aggregate indexes.

    The ordered occurrence-facing ``names`` output is built separately and is
    never passed through this helper.
    """

    result: list[object] = []
    for observation in observations:
        for occurrence in _occurrences(observation):
            value = occurrence.get("surface_form")
            if not any(value == existing for existing in result):
                result.append(value)
    return result


def _platform_groups(
    observations: Sequence[Mapping[str, object]],
) -> list[tuple[object, list[Mapping[str, object]]]]:
    groups: list[tuple[object, list[Mapping[str, object]]]] = []
    for observation in observations:
        platform = observation.get("platform")
        existing = next((rows for key, rows in groups if key == platform), None)
        if existing is None:
            existing = []
            groups.append((platform, existing))
        existing.append(observation)
    return groups


def _evidence_row(
    observation: Mapping[str, object],
    occurrence: Mapping[str, object],
    *,
    observation_index: int,
    occurrence_index: int,
) -> dict[str, object]:
    return {
        "observation_index": observation_index,
        "occurrence_index": occurrence_index,
        "observation_id": observation.get("observation_id"),
        "platform": observation.get("platform"),
        "prompt_identity": _plain(observation.get("prompt_identity") or {}),
        "raw_answer_text": observation.get("raw_answer_text"),
        "surface_form": occurrence.get("surface_form"),
        "position": occurrence.get("position"),
        "character_start": occurrence.get("character_start"),
        "character_end": occurrence.get("character_end"),
        "byte_start": occurrence.get("byte_start"),
        "byte_end": occurrence.get("byte_end"),
        "evidence_references": _plain(
            observation.get("evidence_references") or {}
        ),
        **(
            {"grade": _plain(occurrence["grade"])}
            if "grade" in occurrence
            else {}
        ),
    }


def _prompt_row(observation: Mapping[str, object]) -> dict[str, object]:
    result = {
        "observation_id": observation.get("observation_id"),
        "packet_id": observation.get("packet_id"),
        "platform": observation.get("platform"),
        "profile_id": observation.get("profile_id"),
        "prompt_identity": _plain(observation.get("prompt_identity") or {}),
        "prompt_sha256": observation.get("prompt_sha256"),
        "status": observation.get("status"),
        "extraction_status": observation.get("extraction_status"),
        "raw_answer_text": observation.get("raw_answer_text"),
        "ordered_names": _plain(observation.get("ordered_names") or []),
        "grounded_occurrences": _plain(
            observation.get("grounded_occurrences") or []
        ),
        "evidence_references": _plain(
            observation.get("evidence_references") or {}
        ),
        "issues": _plain(observation.get("issues") or []),
        "transport_counts": _plain(observation.get("transport_counts") or {}),
        "metrics": _prompt_summary(observation),
    }
    if "grade" in observation:
        result["grade"] = _plain(observation["grade"])
    return result


def _scoring_input(
    source: Mapping[str, object],
    observations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Select only values that can influence v3 scores.

    Character and byte offsets remain in the evidence document, but do not
    affect presence/order/frequency scores.  Open-ended source annotations are
    likewise intentionally absent here.
    """

    return {
        "contract": source.get("contract"),
        "run_id": source.get("run_id"),
        "status": source.get("status"),
        "observations": [
            {
                "observation_id": observation.get("observation_id"),
                "platform": observation.get("platform"),
                "prompt_identity": _plain(
                    observation.get("prompt_identity") or {}
                ),
                "grounded_occurrences": [
                    {
                        "surface_form": occurrence.get("surface_form"),
                        "position": occurrence.get("position"),
                    }
                    for occurrence in _occurrences(observation)
                ],
            }
            for observation in observations
        ],
    }


def _metrics_hash_input(
    *,
    metrics: Mapping[str, object],
    prompt_metrics: Sequence[Mapping[str, object]],
    platforms: Sequence[Mapping[str, object]],
    names: Sequence[Mapping[str, object]],
    name_aggregates: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Select metric outputs without copying evidence-only coordinates.

    Occurrence order, exact surface form, and runner-recorded position are
    score-bearing.  Character/byte offsets and attached evidence are retained
    in the analysis and its ``results_hash``, but cannot change the metric hash.
    """

    return {
        "metrics": _plain(metrics),
        "prompt_metrics": _plain(prompt_metrics),
        "platforms": [
            {
                "platform": row.get("platform"),
                "metrics": _plain(row.get("metrics") or {}),
                "prompt_metrics": _plain(row.get("prompt_metrics") or []),
                "names": _plain(row.get("names") or []),
                "name_aggregates": [
                    {
                        "surface_form": aggregate.get("surface_form"),
                        "metrics": _plain(aggregate.get("metrics") or {}),
                    }
                    for aggregate in row.get("name_aggregates") or []
                    if isinstance(aggregate, Mapping)
                ],
            }
            for row in platforms
        ],
        "names": [
            {
                "observation_id": row.get("observation_id"),
                "platform": row.get("platform"),
                "prompt_identity": _plain(row.get("prompt_identity") or {}),
                "occurrence_index": row.get("occurrence_index"),
                "surface_form": row.get("surface_form"),
                "position": row.get("position"),
            }
            for row in names
        ],
        "name_aggregates": [
            {
                "surface_form": row.get("surface_form"),
                "metrics": _plain(row.get("metrics") or {}),
                "platforms": _plain(row.get("platforms") or []),
            }
            for row in name_aggregates
        ],
    }


def _base_document(source: Mapping[str, object]) -> dict[str, object]:
    frozen_scope = _plain(source.get("frozen_scope") or {})
    prompt_pack = _plain(source.get("prompt_pack_identity") or {})
    report_id = (
        frozen_scope.get("snapshot_id")
        if isinstance(frozen_scope, Mapping)
        else None
    ) or source.get("run_id")
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "purpose": EvidencePurpose.FREE_SNAPSHOT_CONFIRMATION.value,
        "source_contract": SNAPSHOT_OBSERVATIONS_V3,
        "adapter_version": source.get("adapter_version"),
        "run_id": source.get("run_id"),
        "report_id": report_id,
        "run_status": source.get("status"),
        "policy_version": source.get("policy_version"),
        "scope_hash": (
            frozen_scope.get("scope_hash")
            if isinstance(frozen_scope, Mapping)
            else None
        ),
        "prompt_pack_hash": (
            frozen_scope.get("prompt_pack_hash")
            if isinstance(frozen_scope, Mapping)
            else None
        )
        or (
            prompt_pack.get("prompt_pack_hash")
            if isinstance(prompt_pack, Mapping)
            else None
        ),
        "frozen_scope": frozen_scope,
        "prompt_pack_identity": prompt_pack,
        "observation_plan_identity": _plain(
            source.get("observation_plan_identity") or {}
        ),
        "plan_sha256": source.get("plan_sha256"),
        "plan_schema_version": source.get("plan_schema_version"),
        "run_evidence": {
            "packets": _plain(source.get("packets") or []),
            "evidence_references": _plain(
                source.get("evidence_references") or {}
            ),
            "issues": _plain(source.get("issues") or []),
            "submission_budget": _plain(
                source.get("submission_budget") or {}
            ),
            "transport_counts": _plain(
                source.get("transport_counts") or {}
            ),
        },
    }


def analyze_snapshot_observations(
    document: SnapshotObservationsV3 | Mapping[str, object],
) -> dict[str, object]:
    """Compute one v3 analysis document from runner-recorded prompt measurements."""

    source = _document_mapping(document)
    result = _base_document(source)
    status = str(source.get("status") or "")
    if status != "succeeded":
        result.update(
            {
                "candidate_eligible": False,
                "analysis_state": NON_PUBLISHABLE_STATE,
                "non_publishable": {
                    "run_status": status,
                    "reason_code": "run_status_not_succeeded",
                },
            }
        )
        return result

    observations = [
        dict(row)
        for row in source.get("observations") or []
        if isinstance(row, Mapping)
    ]
    # Old evidence without collection lineage retains its original analysis
    # envelope. Never manufacture a timestamp from report creation time.
    if any(isinstance(row.get("capture_evidence"), Mapping) and "collection" in row["capture_evidence"] for row in observations):
        result["collection_period"] = collection_period(observations)
    platforms = _platform_groups(observations)
    evidence: list[dict[str, object]] = []
    for observation_index, observation in enumerate(observations):
        for occurrence_index, occurrence in enumerate(_occurrences(observation)):
            evidence.append(
                _evidence_row(
                    observation,
                    occurrence,
                    observation_index=observation_index,
                    occurrence_index=occurrence_index,
                )
            )

    name_aggregates = []
    for surface_form in _distinct_surface_forms_in_first_occurrence_order(
        observations
    ):
        name_evidence = [
            row for row in evidence if row.get("surface_form") == surface_form
        ]
        name_aggregates.append(
            {
                "surface_form": surface_form,
                "metrics": _name_summary(observations, surface_form),
                "platforms": [
                    {
                        "platform": platform,
                        "metrics": _name_summary(rows, surface_form),
                    }
                    for platform, rows in platforms
                ],
                "evidence": name_evidence,
            }
        )

    platform_rows = [
        {
            "platform": platform,
            "metrics": _summary(rows),
            "prompt_metrics": [
                {
                    "observation_id": row.get("observation_id"),
                    **_prompt_summary(row),
                }
                for row in rows
            ],
                "names": [
                    {
                        "observation_id": item["observation_id"],
                        "occurrence_index": item["occurrence_index"],
                        "surface_form": item["surface_form"],
                        "position": item["position"],
                    }
                    for item in evidence
                    if item.get("platform") == platform
                ],
                "name_aggregates": [
                    {
                        "surface_form": surface_form,
                        "metrics": _name_summary(rows, surface_form),
                    }
                    for surface_form in (
                        _distinct_surface_forms_in_first_occurrence_order(rows)
                    )
                ],
        }
        for platform, rows in platforms
    ]
    scoring_input = _scoring_input(source, observations)
    result.update(
        {
            "candidate_eligible": True,
            "analysis_state": CANDIDATE_READY_STATE,
            "metrics": _summary(observations),
            "prompt_metrics": [
                {
                    "observation_id": row.get("observation_id"),
                    "platform": row.get("platform"),
                    "prompt_identity": _plain(row.get("prompt_identity") or {}),
                    **_prompt_summary(row),
                }
                for row in observations
            ],
            "platforms": platform_rows,
            "names": [
                {
                    "observation_id": row["observation_id"],
                    "platform": row["platform"],
                    "prompt_identity": row["prompt_identity"],
                    "occurrence_index": row["occurrence_index"],
                    "surface_form": row["surface_form"],
                    "position": row["position"],
                    "character_start": row["character_start"],
                    "character_end": row["character_end"],
                    "byte_start": row["byte_start"],
                    "byte_end": row["byte_end"],
                }
                for row in evidence
            ],
            "name_aggregates": name_aggregates,
            "observations": [_prompt_row(row) for row in observations],
            "grounded_evidence": evidence,
            "scoring_input_sha256": _canonical_sha256(scoring_input),
        }
    )
    result["metrics_sha256"] = _canonical_sha256(
        _metrics_hash_input(
            metrics=result["metrics"],
            prompt_metrics=result["prompt_metrics"],
            platforms=result["platforms"],
            names=result["names"],
            name_aggregates=name_aggregates,
        )
    )
    result["results_hash"] = compute_results_hash(result)
    return result


analyze_snapshot_observations_v3 = analyze_snapshot_observations
analyze = analyze_snapshot_observations


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "ANALYSIS_V3_PRESENCE_SCHEMA_VERSION",
    "ANALYSIS_V3_SCHEMA_VERSION",
    "ANALYSIS_VERSION",
    "CANDIDATE_READY_STATE",
    "NON_PUBLISHABLE_STATE",
    "SnapshotAnalysisV3Error",
    "analyze",
    "analyze_snapshot_observations",
    "analyze_snapshot_observations_v3",
    "compute_results_hash",
    "verify_results_hash",
]
