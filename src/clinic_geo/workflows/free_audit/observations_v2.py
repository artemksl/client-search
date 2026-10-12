"""Normalize and protect immutable v2 collection observations."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .contracts_v2 import DEFAULT_EVIDENCE_POLICY, PROMPT_PACK_V4_VERSION, ObservationStatus


STANDARD_OBSERVATION_STATUSES = frozenset(status.value for status in ObservationStatus)
ACCEPTED_OBSERVATION_STATUSES = frozenset(
    {ObservationStatus.COLLECTED.value, ObservationStatus.NOT_SHOWN.value}
)
RETRYABLE_OBSERVATION_STATUSES = frozenset(
    {ObservationStatus.EMPTY.value, ObservationStatus.TRANSIENT_ERROR.value}
)

FROZEN_OBSERVATION_FIELDS = (
    "observation_id",
    "scope_id",
    "scope_hash",
    "prompt_pack_hash",
    "prompt_id",
    "prompt_intent",
    "treatment_id",
    "area_id",
    "run",
    "platform",
    "location_context",
)

FROZEN_OBSERVATION_FIELDS_V4 = FROZEN_OBSERVATION_FIELDS + (
    "prompt_pack_version",
    "prompt_text",
    "prompt_text_sha256",
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
    "branch_id",
    "branch_name",
    "actual_variant",
    "naturalness_score",
    "slot_naturalness_score",
    "report_eligible",
)


def frozen_observation_fields(observation: Mapping) -> tuple[str, ...]:
    """Select the immutable identity contract without changing legacy rows."""

    purpose_fields = ("purpose",) if "purpose" in observation else ()
    if (
        observation.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION
        or observation.get("basket_id")
    ):
        return FROZEN_OBSERVATION_FIELDS_V4 + purpose_fields
    return FROZEN_OBSERVATION_FIELDS + purpose_fields


class ObservationError(ValueError):
    pass


class ImmutableObservationError(ObservationError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def raw_answer_sha256(raw_answer: str) -> str:
    return hashlib.sha256(str(raw_answer).encode("utf-8")).hexdigest()


def normalize_status(platform: object, status: object, *, raw_answer: Optional[str] = None) -> str:
    """Map all legacy/provider outcomes to the seven v2 statuses."""

    key = str(status or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "ok": "collected",
        "success": "collected",
        "complete": "collected",
        "completed": "collected",
        "overview_shown": "collected",
        "no_ai_overview": "not_shown",
        "overview_not_shown": "not_shown",
        "missing_overview": "not_shown",
        "overview_empty": "empty",
        "blank": "empty",
        "timeout": "transient_error",
        "error": "transient_error",
        "partial": "transient_error",
        "network_error": "transient_error",
        "rate_limited": "transient_error",
        "auth_required": "blocked",
        "captcha": "blocked",
        "unusual_traffic": "blocked",
        "unavailable": "unsupported",
        "not_supported": "unsupported",
    }
    key = aliases.get(key, key)
    if key not in STANDARD_OBSERVATION_STATUSES:
        return ObservationStatus.INVALID.value
    if key == ObservationStatus.COLLECTED.value and raw_answer is not None and not raw_answer.strip():
        return ObservationStatus.EMPTY.value
    if key == ObservationStatus.NOT_SHOWN.value and str(platform) != "google_ai_overview":
        return ObservationStatus.INVALID.value
    return key


def _relative_reference(reference: object, run_dir: Optional[Path]) -> Optional[str]:
    value = str(reference or "").strip()
    if not value:
        return None
    path = Path(value)
    if run_dir is not None and path.is_absolute():
        try:
            return path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
        except ValueError:
            raise ObservationError("raw answer path must be inside the Snapshot run directory")
    return path.as_posix()


def normalize_observation(
    planned: Mapping,
    result: Optional[Mapping] = None,
    *,
    raw_answer: Optional[str] = None,
    run_dir: Optional[Path] = None,
    timestamp: Optional[str] = None,
) -> dict:
    """Create one standardized attempt result from its frozen plan row."""

    if not isinstance(planned, Mapping):
        raise ObservationError("planned observation must be a mapping")
    result = dict(result or {})
    for field in frozen_observation_fields(planned):
        if field not in planned:
            raise ObservationError(f"planned observation is missing {field}")
        if field in result and result[field] != planned[field]:
            raise ObservationError(f"collection result cannot change frozen field {field}")

    when = timestamp or str(result.get("collected_at") or "").strip() or utc_now()
    source_status = result.get("status") or planned.get("status")
    status = normalize_status(planned.get("platform"), source_status, raw_answer=raw_answer)
    previous_attempts = int(planned.get("attempts") or 0)
    attempts = int(result.get("attempts") or (previous_attempts + 1))
    if attempts < previous_attempts or attempts > DEFAULT_EVIDENCE_POLICY.max_collection_attempts:
        raise ObservationError(
            f"attempts must be between {previous_attempts} and "
            f"{DEFAULT_EVIDENCE_POLICY.max_collection_attempts}"
        )

    answer_reference = _relative_reference(
        result.get("raw_answer_reference") or result.get("output_file"), run_dir
    )
    answer_hash = str(result.get("raw_answer_sha256") or "").strip() or None
    if raw_answer is not None and status in ACCEPTED_OBSERVATION_STATUSES:
        computed = raw_answer_sha256(raw_answer)
        if answer_hash and answer_hash != computed:
            raise ObservationError("provided raw answer does not match raw_answer_sha256")
        answer_hash = computed
    if status not in ACCEPTED_OBSERVATION_STATUSES:
        answer_reference = None
        answer_hash = None

    citations = result.get("citations") or []
    if not isinstance(citations, list):
        raise ObservationError("citations must be a list")
    return {
        **deepcopy(dict(planned)),
        "model_or_surface": str(
            result.get("model_or_surface") or planned.get("model_or_surface") or planned["platform"]
        ),
        "access_mode": str(result.get("access_mode") or planned.get("access_mode") or "browser"),
        "started_at": result.get("started_at") or planned.get("started_at") or when,
        "collected_at": when,
        "status": status,
        "raw_answer_reference": answer_reference,
        "raw_answer_sha256": answer_hash,
        "citations": deepcopy(citations),
        "attempts": attempts,
        "collection_detail": str(result.get("detail") or result.get("collection_detail") or ""),
    }


def is_valid_observation(observation: Mapping) -> bool:
    status = str(observation.get("status") or "")
    if status == ObservationStatus.COLLECTED.value:
        return True
    return (
        status == ObservationStatus.NOT_SHOWN.value
        and observation.get("platform") == "google_ai_overview"
    )


def should_retry(observation: Mapping, *, max_attempts: Optional[int] = None) -> bool:
    maximum = max_attempts or DEFAULT_EVIDENCE_POLICY.max_collection_attempts
    return (
        str(observation.get("status") or "") in RETRYABLE_OBSERVATION_STATUSES
        and int(observation.get("attempts") or 0) < maximum
    )


def merge_observation(existing: Optional[Mapping], update: Mapping) -> dict:
    """Merge an attempt without ever replacing an accepted measurement."""

    if existing is None:
        return deepcopy(dict(update))
    existing_fields = frozen_observation_fields(existing)
    if existing_fields != frozen_observation_fields(update):
        raise ImmutableObservationError("attempt changes frozen observation contract")
    for field in existing_fields:
        if existing.get(field) != update.get(field):
            raise ImmutableObservationError(f"attempt changes frozen field {field}")
    existing_status = str(existing.get("status") or "")
    if existing_status in ACCEPTED_OBSERVATION_STATUSES:
        evidence_fields = (
            "status",
            "raw_answer_reference",
            "raw_answer_sha256",
            "citations",
            "collected_at",
        )
        if any(existing.get(field) != update.get(field) for field in evidence_fields):
            raise ImmutableObservationError("accepted observation evidence cannot be overwritten")
        return deepcopy(dict(existing))
    if int(update.get("attempts") or 0) < int(existing.get("attempts") or 0):
        raise ImmutableObservationError("attempt count cannot decrease")
    return deepcopy(dict(update))


def validate_observation_integrity(
    observation: Mapping,
    *,
    raw_answer: Optional[str] = None,
) -> list[str]:
    errors: list[str] = []
    is_v4 = bool(
        observation.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION
        or observation.get("basket_id")
    )
    nullable_v4 = {
        "branch_id",
        "branch_name",
        "analysis_scope_id",
        "evidence_cell_id",
    }
    if is_v4:
        if observation.get("subject_kind") == "clinic_archetype":
            nullable_v4.update({"treatment_id", "treatment_name", "treatment_order"})
        else:
            nullable_v4.update(
                {
                    "clinic_archetype_id",
                    "clinic_archetype_singular",
                    "clinic_archetype_plural",
                }
            )
    for field in frozen_observation_fields(observation):
        if field in nullable_v4:
            continue
        if observation.get(field) in (None, "", [], {}):
            errors.append(f"missing_{field}")
    if is_v4:
        prompt_text = str(observation.get("prompt_text") or "")
        if prompt_text and raw_answer_sha256(prompt_text) != observation.get("prompt_text_sha256"):
            errors.append("prompt_text_hash_mismatch")
        target = observation.get("target_geography")
        if not isinstance(target, Mapping) or target.get("confirmation_status") != "confirmed":
            errors.append("invalid_target_geography")
        analysis_ids = observation.get("analysis_scope_ids")
        evidence_ids = observation.get("evidence_cell_ids")
        cells = observation.get("evidence_cells")
        if not isinstance(analysis_ids, list) or not analysis_ids:
            errors.append("missing_analysis_scope_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            errors.append("missing_evidence_cell_ids")
        if not isinstance(cells, list) or len(cells) != len(evidence_ids or []):
            errors.append("invalid_evidence_cells")
        if len(analysis_ids or []) == 1 and observation.get("analysis_scope_id") != analysis_ids[0]:
            errors.append("analysis_scope_id_mismatch")
        if len(evidence_ids or []) == 1 and observation.get("evidence_cell_id") != evidence_ids[0]:
            errors.append("evidence_cell_id_mismatch")
        if len(analysis_ids or []) > 1 and observation.get("analysis_scope_id") is not None:
            errors.append("deduplicated_analysis_scope_id_must_be_null")
        if len(evidence_ids or []) > 1 and observation.get("evidence_cell_id") is not None:
            errors.append("deduplicated_evidence_cell_id_must_be_null")
    status = str(observation.get("status") or "")
    if status not in STANDARD_OBSERVATION_STATUSES:
        errors.append("invalid_status")
        return errors
    attempts = int(observation.get("attempts") or 0)
    if attempts < 1 or attempts > DEFAULT_EVIDENCE_POLICY.max_collection_attempts:
        errors.append("invalid_attempt_count")
    if status in ACCEPTED_OBSERVATION_STATUSES:
        if not observation.get("raw_answer_reference"):
            errors.append("missing_raw_answer_reference")
        stored_hash = str(observation.get("raw_answer_sha256") or "")
        if not stored_hash:
            errors.append("missing_raw_answer_sha256")
        if raw_answer is None:
            errors.append("raw_answer_not_provided")
        elif stored_hash and raw_answer_sha256(raw_answer) != stored_hash:
            errors.append("raw_answer_hash_mismatch")
    elif observation.get("raw_answer_reference") or observation.get("raw_answer_sha256"):
        errors.append("non_measured_observation_has_raw_answer")
    return errors


def normalize_observations(
    planned_rows: Iterable[Mapping],
    results: Mapping[str, Mapping],
    *,
    raw_answers: Optional[Mapping[str, str]] = None,
    run_dir: Optional[Path] = None,
) -> list[dict]:
    raw_answers = raw_answers or {}
    normalized = []
    for planned in planned_rows:
        observation_id = str(planned.get("observation_id") or "")
        if observation_id not in results:
            normalized.append(deepcopy(dict(planned)))
            continue
        normalized.append(
            normalize_observation(
                planned,
                results[observation_id],
                raw_answer=raw_answers.get(observation_id),
                run_dir=run_dir,
            )
        )
    return normalized


def retryable_observation_ids(observations: Iterable[Mapping]) -> list[str]:
    return [
        str(row["observation_id"])
        for row in observations
        if row.get("observation_id") and should_retry(row)
    ]
