"""Strict recommendation-rubric extraction for v2 Snapshot answers.

The transport remains injectable so the workflow can use the existing browser
extraction session.  Parsing and evidence validation are deterministic and
fail closed: a malformed extraction is never converted to ``unclassifiable``.
"""

from __future__ import annotations

import inspect
import json
import re
from copy import deepcopy
from typing import Callable, Iterable, Mapping, Optional, Sequence

from clinic_geo.parsing.clinic_names import iter_json_objects, normalize_clinic_name

from .contracts_v2 import (
    NAMED_RECOMMENDATION_CLASSES,
    RECOMMENDATION_RUBRIC_VERSION,
    RecommendationClass,
    canonical_sha256,
)
from .observations_v2 import raw_answer_sha256


EXTRACTION_SCHEMA_VERSION = "free_snapshot.recommendation_extraction.v1"


class RecommendationExtractionError(ValueError):
    """The extraction transport returned evidence that cannot be verified."""


def stable_external_entity_id(name: object) -> str:
    key = normalize_clinic_name(str(name or ""))
    if not key:
        raise RecommendationExtractionError("entity name cannot be blank")
    return f"ext-{canonical_sha256({'canonical_name': key})[:20]}"


def _known_entity_rows(known_entities: Iterable[object]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for value in known_entities or ():
        source = value if isinstance(value, Mapping) else {}
        name = str(source.get("canonical_name") or source.get("name") or value or "").strip()
        key = normalize_clinic_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases = source.get("aliases") or source.get("verified_aliases") or []
        if not isinstance(aliases, (list, tuple)):
            raise RecommendationExtractionError("known entity aliases must be a list")
        rows.append(
            {
                "entity_id": str(source.get("entity_id") or source.get("account_id") or "").strip()
                or stable_external_entity_id(name),
                "canonical_name": name,
                "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
            }
        )
    return rows


def _rubric_instructions() -> list[str]:
    return [
        "Extract every clinic or clinic branch named in raw_answer, including competitors; do not limit entities to the client or known_entities.",
        "If the entity is absent, use not_named.",
        "A material warning, negative statement, or suitability caveat takes precedence and uses caveated_negative.",
        "An explicit best, top, or primary recommendation uses strong_recommendation.",
        "A recommended shortlist or positive suitability statement uses shortlisted_positive.",
        "A factual mention without endorsement uses neutral_mention.",
        "Use unclassifiable only for a genuinely ambiguous or conflicting named role.",
        "Every named entity needs one exact contiguous excerpt and zero-based start/end offsets into raw_answer.",
        "Position is one-based recommendation order. Do not invent entities or evidence.",
    ]


def _required_entity_schema() -> dict:
    return {
        "entity_id": "stable id when known, otherwise null",
        "canonical_name": "clinic name; required even when entity_id is known",
        "mentioned_name": "exact clinic-name spelling copied contiguously from raw_answer",
        "name_start": "zero-based start of mentioned_name, null only for not_named",
        "name_end": "exclusive end of mentioned_name, null only for not_named",
        "classification": "one allowed class; use this exact key, never class",
        "excerpt": "exact contiguous excerpt, null only for not_named",
        "start": "zero-based integer, null only for not_named",
        "end": "exclusive integer, null only for not_named",
        "position": "one-based integer, null only for not_named",
    }


def build_extraction_prompt(
    scope: Mapping,
    observation: Mapping,
    raw_answer: str,
    *,
    known_entities: Iterable[object] = (),
) -> str:
    """Build one strict JSON extraction request for the existing transport."""

    payload = {
        "task": "classify_clinic_recommendations",
        "rubric_version": RECOMMENDATION_RUBRIC_VERSION,
        "observation_id": observation.get("observation_id"),
        "clinic": scope.get("clinic"),
        "known_entities": _known_entity_rows(known_entities),
        "allowed_classes": [classification.value for classification in RecommendationClass],
        "precedence": _rubric_instructions(),
        "required_response": {
            "observation_id": "same value",
            "entities": [_required_entity_schema()],
        },
        "raw_answer": str(raw_answer),
    }
    return (
        "Return JSON only. Apply the rubric in precedence order.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def build_batch_extraction_prompt(
    scope: Mapping,
    observations: Sequence[Mapping],
    raw_answers: Mapping[str, str],
    *,
    known_entities: Iterable[object] = (),
) -> str:
    """Build a batched request with the same per-answer evidence contract."""

    items = []
    for observation in observations:
        observation_id = str(observation.get("observation_id") or "")
        if observation_id not in raw_answers:
            raise RecommendationExtractionError(f"missing raw answer for {observation_id}")
        items.append(
            {
                "observation_id": observation_id,
                "raw_answer": str(raw_answers[observation_id]),
            }
        )
    payload = {
        "task": "classify_clinic_recommendations_batch",
        "rubric_version": RECOMMENDATION_RUBRIC_VERSION,
        "clinic": scope.get("clinic"),
        "known_entities": _known_entity_rows(known_entities),
        "allowed_classes": [classification.value for classification in RecommendationClass],
        "precedence": _rubric_instructions(),
        "schema_rules": [
            "Return one entity object for every clinic or clinic branch named in each raw_answer, including competitors; known_entities is an identity aid, not an extraction allowlist.",
            "For every observation also return every known_entities row; use not_named with null evidence fields when that known entity is absent.",
        "Every entity object must include canonical_name.",
            "For every named entity, mentioned_name must be only the clinic name exactly as written in raw_answer, with exact name_start/name_end offsets.",
            "Use the exact key classification; never use class.",
            "Return every required key shown below for every entity object.",
        ],
        "required_response": {
            "results": [
                {
                    "observation_id": "same value",
                    "entities": [_required_entity_schema()],
                }
            ]
        },
        "observations": items,
    }
    return "Return one JSON object only.\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def _parse_json_object(response: object) -> dict:
    if isinstance(response, Mapping):
        return deepcopy(dict(response))
    text = str(response or "").strip()
    if not text:
        raise RecommendationExtractionError("empty extraction response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else list(iter_json_objects(text))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    raise RecommendationExtractionError("extraction response does not contain a JSON object")


def _row_matches_known(row: Mapping, known: Mapping) -> bool:
    if row.get("entity_id") and row.get("entity_id") == known.get("entity_id"):
        return True
    keys = {
        normalize_clinic_name(known.get("canonical_name")),
        *(normalize_clinic_name(alias) for alias in known.get("aliases") or []),
    }
    return normalize_clinic_name(row.get("canonical_name")) in keys


def _name_occurs(raw_answer: str, known: Mapping) -> bool:
    values = [known.get("canonical_name"), *(known.get("aliases") or [])]
    lowered = raw_answer.casefold()
    return any(str(value or "").strip().casefold() in lowered for value in values if str(value or "").strip())


def _normalize_entity_row(row: Mapping, raw_answer: str) -> dict:
    name = str(row.get("canonical_name") or row.get("name") or "").strip()
    if not name:
        raise RecommendationExtractionError("every extraction entity needs canonical_name")
    classification = str(row.get("classification") or "").strip()
    allowed = {value.value for value in RecommendationClass}
    if classification not in allowed:
        raise RecommendationExtractionError(f"invalid recommendation class {classification!r}")
    entity_id = str(row.get("entity_id") or "").strip() or stable_external_entity_id(name)

    if classification == RecommendationClass.NOT_NAMED.value:
        if row.get("excerpt") not in (None, ""):
            raise RecommendationExtractionError("not_named cannot include an excerpt")
        if row.get("start", row.get("excerpt_start")) is not None:
            raise RecommendationExtractionError("not_named cannot include offsets")
        if row.get("end", row.get("excerpt_end")) is not None:
            raise RecommendationExtractionError("not_named cannot include offsets")
        if row.get("position") is not None:
            raise RecommendationExtractionError("not_named cannot include a position")
        if row.get("mentioned_name") not in (None, ""):
            raise RecommendationExtractionError("not_named cannot include mentioned_name")
        if row.get("name_start") is not None or row.get("name_end") is not None:
            raise RecommendationExtractionError("not_named cannot include name offsets")
        return {
            "entity_id": entity_id,
            "canonical_name": name,
            "mentioned_name": None,
            "name_start": None,
            "name_end": None,
            "classification": classification,
            "excerpt": None,
            "excerpt_start": None,
            "excerpt_end": None,
            "position": None,
            "first_named": False,
        }

    excerpt = row.get("excerpt")
    start = row.get("start", row.get("excerpt_start"))
    end = row.get("end", row.get("excerpt_end"))
    position = row.get("position")
    if not isinstance(excerpt, str) or not excerpt:
        raise RecommendationExtractionError("named classifications require a non-empty excerpt")
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise RecommendationExtractionError("named classifications require integer start/end offsets")
    if start < 0 or end <= start or end > len(raw_answer):
        raise RecommendationExtractionError("named classification offsets are outside raw_answer")
    if raw_answer[start:end] != excerpt:
        raise RecommendationExtractionError("named classification excerpt is not contiguous raw evidence")
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        raise RecommendationExtractionError("named classifications require a one-based position")
    mentioned_name = str(row.get("mentioned_name") or "").strip()
    name_start = row.get("name_start")
    name_end = row.get("name_end")
    if not mentioned_name:
        # Compatibility for injected and pre-v2.9 transports: recover only an
        # exact spelling already present in the immutable raw answer.
        matches = list(re.finditer(re.escape(name), raw_answer, flags=re.IGNORECASE))
        within_excerpt = [
            match for match in matches if start <= match.start() and match.end() <= end
        ]
        if len(within_excerpt) != 1:
            raise RecommendationExtractionError(
                "named classifications require exact mentioned_name evidence"
            )
        match = within_excerpt[0]
        mentioned_name = raw_answer[match.start() : match.end()]
        name_start, name_end = match.start(), match.end()
    if (
        not isinstance(name_start, int)
        or isinstance(name_start, bool)
        or not isinstance(name_end, int)
        or isinstance(name_end, bool)
        or name_start < start
        or name_end > end
        or name_end <= name_start
        or raw_answer[name_start:name_end] != mentioned_name
    ):
        raise RecommendationExtractionError(
            "mentioned_name offsets are not exact contiguous raw evidence"
        )
    return {
        "entity_id": entity_id,
        "canonical_name": name,
        "mentioned_name": mentioned_name,
        "name_start": name_start,
        "name_end": name_end,
        "classification": classification,
        "excerpt": excerpt,
        "excerpt_start": start,
        "excerpt_end": end,
        "position": position,
        "first_named": False,
    }


def align_batch_extraction_offsets(
    response: object,
    observations: Sequence[Mapping],
    raw_answers: Mapping[str, str],
) -> tuple[dict, list[dict]]:
    """Align exact model-selected evidence to deterministic raw-answer positions.

    Recommendation semantics and excerpt text remain transport outputs. Only
    Character arithmetic is corrected, and only when the exact excerpt occurs
    in the hashed raw answer and the intended occurrence is unambiguous (or is
    uniquely nearest to the reported start). The original response remains the
    hashed transport evidence; corrections are returned for the audit trail.
    A missing recommendation position may be filled only after every excerpt
    is aligned: secondary named mentions are ordered after all explicit model
    positions, in their exact raw-answer order.
    """

    value = _parse_json_object(response)
    results = value.get("results")
    if not isinstance(results, list) or any(not isinstance(row, Mapping) for row in results):
        raise RecommendationExtractionError("batch extraction results must be a list of objects")
    expected = {str(row.get("observation_id") or "") for row in observations}
    corrections: list[dict] = []
    for result in results:
        observation_id = str(result.get("observation_id") or "")
        if observation_id not in expected or observation_id not in raw_answers:
            continue
        entities = result.get("entities")
        if not isinstance(entities, list):
            continue
        raw_answer = str(raw_answers[observation_id])
        for entity_index, entity in enumerate(entities):
            if not isinstance(entity, Mapping):
                continue
            classification = str(entity.get("classification") or "")
            if classification == RecommendationClass.NOT_NAMED.value:
                continue
            excerpt = entity.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt:
                continue
            reported_start = entity.get("start", entity.get("excerpt_start"))
            reported_end = entity.get("end", entity.get("excerpt_end"))
            if (
                isinstance(reported_start, int)
                and not isinstance(reported_start, bool)
                and isinstance(reported_end, int)
                and not isinstance(reported_end, bool)
                and 0 <= reported_start < reported_end <= len(raw_answer)
                and raw_answer[reported_start:reported_end] == excerpt
            ):
                continue
            occurrences: list[int] = []
            cursor = 0
            while True:
                index = raw_answer.find(excerpt, cursor)
                if index < 0:
                    break
                occurrences.append(index)
                cursor = index + 1
            if not occurrences:
                raise RecommendationExtractionError(
                    "named classification excerpt is not contiguous raw evidence"
                )
            if len(occurrences) == 1:
                aligned_start = occurrences[0]
            elif isinstance(reported_start, int) and not isinstance(reported_start, bool):
                ranked = sorted((abs(index - reported_start), index) for index in occurrences)
                if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                    raise RecommendationExtractionError(
                        "named classification excerpt occurrence is ambiguous"
                    )
                aligned_start = ranked[0][1]
            else:
                raise RecommendationExtractionError(
                    "named classification excerpt occurrence is ambiguous"
                )
            aligned_end = aligned_start + len(excerpt)
            entity["start"] = aligned_start
            entity["end"] = aligned_end
            corrections.append(
                {
                    "correction_type": "offset_alignment",
                    "observation_id": observation_id,
                    "entity_index": entity_index,
                    "excerpt_sha256": canonical_sha256(excerpt),
                    "reported_start": reported_start,
                    "reported_end": reported_end,
                    "aligned_start": aligned_start,
                    "aligned_end": aligned_end,
                }
            )
            # Continue below so independently supplied name offsets can be
            # aligned against the now-verified excerpt.
        for entity_index, entity in enumerate(entities):
            if not isinstance(entity, Mapping):
                continue
            if str(entity.get("classification") or "") == RecommendationClass.NOT_NAMED.value:
                continue
            mentioned_name = str(entity.get("mentioned_name") or "").strip()
            if not mentioned_name:
                continue
            excerpt_start = entity.get("start", entity.get("excerpt_start"))
            excerpt_end = entity.get("end", entity.get("excerpt_end"))
            if not isinstance(excerpt_start, int) or not isinstance(excerpt_end, int):
                continue
            reported_start = entity.get("name_start")
            reported_end = entity.get("name_end")
            if (
                isinstance(reported_start, int)
                and not isinstance(reported_start, bool)
                and isinstance(reported_end, int)
                and not isinstance(reported_end, bool)
                and excerpt_start <= reported_start < reported_end <= excerpt_end
                and raw_answer[reported_start:reported_end] == mentioned_name
            ):
                continue
            occurrences = [
                match.start()
                for match in re.finditer(re.escape(mentioned_name), raw_answer)
                if excerpt_start <= match.start()
                and match.end() <= excerpt_end
            ]
            if len(occurrences) != 1:
                raise RecommendationExtractionError(
                    "mentioned_name occurrence is missing or ambiguous within excerpt"
                )
            aligned_start = occurrences[0]
            aligned_end = aligned_start + len(mentioned_name)
            entity["name_start"] = aligned_start
            entity["name_end"] = aligned_end
            corrections.append(
                {
                    "correction_type": "name_offset_alignment",
                    "observation_id": observation_id,
                    "entity_index": entity_index,
                    "reported_start": reported_start,
                    "reported_end": reported_end,
                    "aligned_start": aligned_start,
                    "aligned_end": aligned_end,
                }
            )
        explicit_positions = [
            int(entity.get("position"))
            for entity in entities
            if isinstance(entity, Mapping)
            and isinstance(entity.get("position"), int)
            and not isinstance(entity.get("position"), bool)
            and int(entity.get("position")) >= 1
        ]
        missing_positions = [
            (int(entity.get("start", entity.get("excerpt_start"))), entity_index, entity)
            for entity_index, entity in enumerate(entities)
            if isinstance(entity, Mapping)
            and str(entity.get("classification") or "") != RecommendationClass.NOT_NAMED.value
            and not (
                isinstance(entity.get("position"), int)
                and not isinstance(entity.get("position"), bool)
                and int(entity.get("position")) >= 1
            )
            and isinstance(entity.get("start", entity.get("excerpt_start")), int)
            and not isinstance(entity.get("start", entity.get("excerpt_start")), bool)
        ]
        next_position = max(explicit_positions, default=0) + 1
        for _start, entity_index, entity in sorted(missing_positions):
            entity["position"] = next_position
            corrections.append(
                {
                    "correction_type": "position_alignment",
                    "observation_id": observation_id,
                    "entity_index": entity_index,
                    "reported_position": None,
                    "aligned_position": next_position,
                }
            )
            next_position += 1
    return value, corrections


def parse_extraction_response(
    response: object,
    observation: Mapping,
    raw_answer: str,
    *,
    known_entities: Iterable[object] = (),
) -> dict:
    """Parse and validate one result, adding explicit not_named known entities."""

    value = _parse_json_object(response)
    observation_id = str(observation.get("observation_id") or "")
    if str(value.get("observation_id") or observation_id) != observation_id:
        raise RecommendationExtractionError("extraction observation_id does not match")
    entities = value.get("entities")
    if not isinstance(entities, list) or any(not isinstance(row, Mapping) for row in entities):
        raise RecommendationExtractionError("extraction entities must be a list of objects")
    stored_raw_hash = str(value.get("raw_answer_sha256") or "")
    actual_raw_hash = raw_answer_sha256(raw_answer)
    if stored_raw_hash and stored_raw_hash != actual_raw_hash:
        raise RecommendationExtractionError("extraction raw_answer_sha256 does not match evidence")

    normalized_rows = [_normalize_entity_row(row, raw_answer) for row in entities]
    normalized: list[dict] = []
    seen_rows: dict[str, dict] = {}
    for row in normalized_rows:
        key = normalize_clinic_name(row["canonical_name"])
        previous = seen_rows.get(key)
        if previous is not None and previous != row:
            raise RecommendationExtractionError(f"duplicate extraction entity {row['canonical_name']!r}")
        if previous is not None:
            # Some extraction transports occasionally repeat the exact same
            # structured row.  This is deterministic duplication, not
            # conflicting evidence, so coalesce it before metrics are built.
            continue
        seen_rows[key] = row
        normalized.append(row)

    known_rows = _known_entity_rows(known_entities)
    for known in known_rows:
        matching = next((row for row in normalized if _row_matches_known(row, known)), None)
        if matching is None:
            if _name_occurs(raw_answer, known):
                raise RecommendationExtractionError(
                    f"known entity {known['canonical_name']!r} appears in raw evidence but was omitted"
                )
            normalized.append(
                {
                    "entity_id": known["entity_id"],
                    "canonical_name": known["canonical_name"],
                    "mentioned_name": None,
                    "name_start": None,
                    "name_end": None,
                    "classification": RecommendationClass.NOT_NAMED.value,
                    "excerpt": None,
                    "excerpt_start": None,
                    "excerpt_end": None,
                    "position": None,
                    "first_named": False,
                }
            )
        else:
            if (
                matching["classification"] == RecommendationClass.NOT_NAMED.value
                and _name_occurs(raw_answer, known)
            ):
                raise RecommendationExtractionError(
                    f"known entity {known['canonical_name']!r} is present and cannot be not_named"
                )
            matching["entity_id"] = known["entity_id"]
            matching["canonical_name"] = known["canonical_name"]

    named = [row for row in normalized if row["classification"] in NAMED_RECOMMENDATION_CLASSES]
    first_position = min((row["position"] for row in named), default=None)
    for row in named:
        row["first_named"] = row["position"] == first_position
    normalized.sort(
        key=lambda row: (
            row["position"] is None,
            row["position"] if row["position"] is not None else 10**9,
            row["canonical_name"].casefold(),
            row["canonical_name"],
        )
    )
    result = {
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "rubric_version": RECOMMENDATION_RUBRIC_VERSION,
        "observation_id": observation_id,
        "prompt_id": observation.get("prompt_id"),
        "scope_id": observation.get("scope_id"),
        "raw_answer_sha256": actual_raw_hash,
        "status": "complete",
        "entities": normalized,
        "release_eligible": True,
    }
    result["extraction_sha256"] = canonical_sha256(result)
    return result


def _transport_response(transport: Callable[[str], object], prompt: str) -> object:
    result = transport(prompt)
    if inspect.isawaitable(result):
        raise RecommendationExtractionError(
            "async extraction transport must be awaited by the caller before parsing"
        )
    return result


def extract_recommendations(
    scope: Mapping,
    observation: Mapping,
    raw_answer: str,
    *,
    transport: Optional[Callable[[str], object]] = None,
    response_text: Optional[object] = None,
    known_entities: Iterable[object] = (),
) -> dict:
    """Execute one extraction through an injected transport and validate it."""

    if response_text is None:
        if transport is None:
            raise RecommendationExtractionError("extraction requires transport or response_text")
        response_text = _transport_response(
            transport,
            build_extraction_prompt(
                scope, observation, raw_answer, known_entities=known_entities
            ),
        )
    return parse_extraction_response(
        response_text,
        observation,
        raw_answer,
        known_entities=known_entities,
    )


def _known_for_observation(known_entities: object, observation_id: str) -> Iterable[object]:
    if isinstance(known_entities, Mapping):
        return known_entities.get(observation_id) or known_entities.get("default") or ()
    return known_entities or ()


def parse_batch_extraction_response(
    response: object,
    observations: Sequence[Mapping],
    raw_answers: Mapping[str, str],
    *,
    known_entities: object = (),
) -> list[dict]:
    value = _parse_json_object(response)
    results = value.get("results")
    if not isinstance(results, list) or any(not isinstance(row, Mapping) for row in results):
        raise RecommendationExtractionError("batch extraction must contain a results list")
    by_id: dict[str, Mapping] = {}
    for result in results:
        observation_id = str(result.get("observation_id") or "")
        if not observation_id or observation_id in by_id:
            raise RecommendationExtractionError("batch result ids must be present and unique")
        by_id[observation_id] = result
    expected_ids = [str(row.get("observation_id") or "") for row in observations]
    if set(by_id) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(by_id))
        extra = sorted(set(by_id) - set(expected_ids))
        raise RecommendationExtractionError(
            f"batch result ids differ from observations (missing={missing}, extra={extra})"
        )
    parsed = []
    for observation in observations:
        observation_id = str(observation["observation_id"])
        if observation_id not in raw_answers:
            raise RecommendationExtractionError(f"missing raw answer for {observation_id}")
        parsed.append(
            parse_extraction_response(
                by_id[observation_id],
                observation,
                str(raw_answers[observation_id]),
                known_entities=_known_for_observation(known_entities, observation_id),
            )
        )
    return parsed


def batch_extract_recommendations(
    scope: Mapping,
    observations: Sequence[Mapping],
    raw_answers: Mapping[str, str],
    *,
    transport: Optional[Callable[[str], object]] = None,
    response_text: Optional[object] = None,
    known_entities: object = (),
) -> list[dict]:
    if response_text is None:
        if transport is None:
            raise RecommendationExtractionError("batch extraction requires transport or response_text")
        prompt_known = known_entities.get("default", ()) if isinstance(known_entities, Mapping) else known_entities
        response_text = _transport_response(
            transport,
            build_batch_extraction_prompt(
                scope,
                observations,
                raw_answers,
                known_entities=prompt_known,
            ),
        )
    return parse_batch_extraction_response(
        response_text,
        observations,
        raw_answers,
        known_entities=known_entities,
    )


def validate_extraction_result(
    extraction: Mapping,
    observation: Mapping,
    raw_answer: str,
    *,
    known_entities: Iterable[object] = (),
) -> list[str]:
    deterministic_outcome = extraction.get("deterministic_outcome")
    if deterministic_outcome is not None and (
        deterministic_outcome != "not_shown"
        or observation.get("status") != "not_shown"
    ):
        return ["deterministic_outcome is not bound to a not_shown observation"]
    try:
        parsed = parse_extraction_response(
            extraction,
            observation,
            raw_answer,
            known_entities=known_entities,
        )
    except RecommendationExtractionError as exc:
        return [str(exc)]
    expected_hash = extraction.get("extraction_sha256")
    if expected_hash and expected_hash != parsed["extraction_sha256"]:
        # A previously stored extraction can contain a byte-for-byte duplicate
        # entity row that the current parser safely coalesces.  Preserve its
        # original integrity check while accepting the normalized semantics.
        stored_payload = {
            key: deepcopy(extraction.get(key))
            for key in (
                "schema_version",
                "rubric_version",
                "observation_id",
                "prompt_id",
                "scope_id",
                "raw_answer_sha256",
                "status",
                "entities",
                "release_eligible",
            )
        }
        if expected_hash != canonical_sha256(stored_payload):
            return ["extraction_sha256 does not match normalized extraction"]
    return []


def derive_legacy_extracted_names(extraction_results: Iterable[Mapping]) -> dict[str, list[str]]:
    """Generate the legacy ordered-name derivative without affecting v2 evidence."""

    output: dict[str, list[str]] = {}
    for extraction in extraction_results:
        if extraction.get("status") != "complete":
            continue
        # Run-expanded v2 observations share a source prompt_id.  The immutable
        # observation id is the only safe legacy derivative key; using prompt_id
        # would silently overwrite runs two and three.
        prompt_id = str(extraction.get("observation_id") or "")
        names = [
            row["canonical_name"]
            for row in extraction.get("entities") or []
            if isinstance(row, Mapping)
            and row.get("classification") in NAMED_RECOMMENDATION_CLASSES
        ]
        output[prompt_id] = names
    return output
