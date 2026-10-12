"""Strict loader for reviewed, immutable treatment catalogue releases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from clinic_geo.integrity import canonical_sha256, sha256_bytes

from .contracts import (
    CampaignSelectabilityState,
    PatientSearchIntentVersion,
    RecommendationPromptEligibilityState,
    TreatmentConceptVersion,
    TreatmentLifecycleState,
    TreatmentReviewClassification,
    TreatmentReviewState,
    TreatmentTermKind,
    TreatmentTermVersion,
)


CATALOGUE_SCHEMA_VERSION = "treatment-catalog.v1"
LEGACY_MAPPING_SCHEMA_VERSION = "legacy-treatment-mapping.v1"


def _require_exact_keys(
    payload: Mapping[str, Any], expected: set[str], label: str
) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _require_text_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(value)


def _enum(enum_type: type, value: Any, label: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a supported state: {value!r}") from exc


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    hashable = dict(payload)
    hashable.pop("payload_sha256", None)
    return canonical_sha256(hashable)


@dataclass(frozen=True, slots=True)
class TreatmentCatalogueRelease:
    id: str
    release_version: int
    lifecycle_state: TreatmentLifecycleState
    review_state: TreatmentReviewState
    source_catalogue_path: str
    source_catalogue_sha256: str
    created_at: str
    approved_at: str
    approved_by: str
    payload_sha256: str

    def __post_init__(self) -> None:
        for label in (
            "id",
            "source_catalogue_path",
            "source_catalogue_sha256",
            "created_at",
            "approved_at",
            "approved_by",
            "payload_sha256",
        ):
            _require_text(getattr(self, label), label)
        if self.release_version != 1:
            raise ValueError("catalogue release_version must be 1")
        if self.lifecycle_state is not TreatmentLifecycleState.ACTIVE:
            raise ValueError("checked-in catalogue release must be active")
        if self.review_state is not TreatmentReviewState.APPROVED:
            raise ValueError("checked-in catalogue release must be approved")


@dataclass(frozen=True, slots=True)
class TreatmentCatalogue:
    schema_version: str
    release: TreatmentCatalogueRelease
    concepts: tuple[TreatmentConceptVersion, ...]
    terms: tuple[TreatmentTermVersion, ...]
    patient_search_intents: tuple[PatientSearchIntentVersion, ...]

    @property
    def concept_by_id(self) -> dict[str, TreatmentConceptVersion]:
        return {item.concept_id: item for item in self.concepts}

    @property
    def term_by_id(self) -> dict[str, TreatmentTermVersion]:
        return {item.term_id: item for item in self.terms}

    @property
    def intent_by_id(self) -> dict[str, PatientSearchIntentVersion]:
        return {item.patient_search_intent_id: item for item in self.patient_search_intents}


@dataclass(frozen=True, slots=True)
class LegacyTreatmentMapping:
    legacy_slug: str
    legacy_name: str
    classification: TreatmentReviewClassification
    review_state: TreatmentReviewState
    target_concept_ids: tuple[str, ...]
    target_search_intent_ids: tuple[str, ...]
    candidate_target_concept_ids: tuple[str, ...]
    candidate_target_search_intent_ids: tuple[str, ...]
    reason_summary: str

    def __post_init__(self) -> None:
        for label in ("legacy_slug", "legacy_name", "reason_summary"):
            _require_text(getattr(self, label), label)
        targets = (*self.target_concept_ids, *self.target_search_intent_ids)
        candidates = (
            *self.candidate_target_concept_ids,
            *self.candidate_target_search_intent_ids,
        )
        if self.review_state is TreatmentReviewState.REVIEW_REQUIRED:
            if targets:
                raise ValueError(
                    "review-required legacy mappings cannot carry authoritative targets"
                )
        elif self.review_state is TreatmentReviewState.APPROVED:
            if not targets:
                raise ValueError("approved legacy mappings must carry an approved target")
            if candidates:
                raise ValueError("approved legacy mappings cannot carry candidate targets")
            if self.classification is TreatmentReviewClassification.INSUFFICIENT_EVIDENCE:
                raise ValueError("insufficient-evidence mappings cannot be approved")
        else:
            raise ValueError("legacy mapping review_state must be approved or review_required")

    @property
    def is_deterministically_authoritative(self) -> bool:
        return self.review_state is TreatmentReviewState.APPROVED


@dataclass(frozen=True, slots=True)
class LegacyTreatmentMappingRelease:
    schema_version: str
    catalogue_release_id: str
    source_catalogue_path: str
    source_catalogue_sha256: str
    payload_sha256: str
    mappings: tuple[LegacyTreatmentMapping, ...]


_TOP_LEVEL_KEYS = {
    "schema_version",
    "release",
    "concepts",
    "terms",
    "patient_search_intents",
    "payload_sha256",
}
_RELEASE_KEYS = {
    "id",
    "release_version",
    "lifecycle_state",
    "review_state",
    "source_catalogue_path",
    "source_catalogue_sha256",
    "created_at",
    "approved_at",
    "approved_by",
}
_POLICY_KEYS = {
    "lifecycle_state",
    "review_state",
    "campaign_selectability_state",
    "recommendation_prompt_eligibility_state",
}
_CONCEPT_KEYS = {"concept_id", "version_row_id", "canonical_name", *_POLICY_KEYS}
_TERM_KEYS = {
    "term_id",
    "version_row_id",
    "term_kind",
    "exact_term",
    "term_content_sha256",
    "target_concept_ids",
    *_POLICY_KEYS,
}
_INTENT_KEYS = {
    "patient_search_intent_id",
    "version_row_id",
    "language_locale",
    "exact_patient_phrase",
    "exact_phrase_content_sha256",
    "rendered_treatment_term",
    "rendered_treatment_term_sha256",
    "target_concept_ids",
    "branch_offering_inference_allowed",
    *_POLICY_KEYS,
}


def _policy_kwargs(payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    return {
        "lifecycle_state": _enum(
            TreatmentLifecycleState, payload["lifecycle_state"], f"{label}.lifecycle_state"
        ),
        "review_state": _enum(
            TreatmentReviewState, payload["review_state"], f"{label}.review_state"
        ),
        "campaign_selectability_state": _enum(
            CampaignSelectabilityState,
            payload["campaign_selectability_state"],
            f"{label}.campaign_selectability_state",
        ),
        "recommendation_prompt_eligibility_state": _enum(
            RecommendationPromptEligibilityState,
            payload["recommendation_prompt_eligibility_state"],
            f"{label}.recommendation_prompt_eligibility_state",
        ),
    }


def load_catalogue(path: Path | str) -> TreatmentCatalogue:
    source_path = Path(path)
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("catalogue must be a JSON object")
    _require_exact_keys(raw, _TOP_LEVEL_KEYS, "catalogue")
    if raw["schema_version"] != CATALOGUE_SCHEMA_VERSION:
        raise ValueError("unsupported treatment catalogue schema_version")
    if raw["payload_sha256"] != _payload_sha256(raw):
        raise ValueError("catalogue payload_sha256 mismatch")

    release_raw = raw["release"]
    if not isinstance(release_raw, dict):
        raise ValueError("catalogue.release must be an object")
    _require_exact_keys(release_raw, _RELEASE_KEYS, "catalogue.release")
    release = TreatmentCatalogueRelease(
        id=release_raw["id"],
        release_version=release_raw["release_version"],
        lifecycle_state=_enum(
            TreatmentLifecycleState,
            release_raw["lifecycle_state"],
            "catalogue.release.lifecycle_state",
        ),
        review_state=_enum(
            TreatmentReviewState,
            release_raw["review_state"],
            "catalogue.release.review_state",
        ),
        source_catalogue_path=release_raw["source_catalogue_path"],
        source_catalogue_sha256=release_raw["source_catalogue_sha256"],
        created_at=release_raw["created_at"],
        approved_at=release_raw["approved_at"],
        approved_by=release_raw["approved_by"],
        payload_sha256=raw["payload_sha256"],
    )

    concepts: list[TreatmentConceptVersion] = []
    for index, item in enumerate(raw["concepts"]):
        label = f"catalogue.concepts[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        _require_exact_keys(item, _CONCEPT_KEYS, label)
        concepts.append(
            TreatmentConceptVersion(
                concept_id=item["concept_id"],
                version_row_id=item["version_row_id"],
                catalogue_release_id=release.id,
                canonical_name=item["canonical_name"],
                **_policy_kwargs(item, label),
            )
        )

    terms: list[TreatmentTermVersion] = []
    for index, item in enumerate(raw["terms"]):
        label = f"catalogue.terms[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        _require_exact_keys(item, _TERM_KEYS, label)
        terms.append(
            TreatmentTermVersion(
                term_id=item["term_id"],
                version_row_id=item["version_row_id"],
                catalogue_release_id=release.id,
                term_kind=_enum(TreatmentTermKind, item["term_kind"], f"{label}.term_kind"),
                exact_term=item["exact_term"],
                term_content_sha256=item["term_content_sha256"],
                target_concept_ids=_require_text_list(
                    item["target_concept_ids"], f"{label}.target_concept_ids"
                ),
                **_policy_kwargs(item, label),
            )
        )

    intents: list[PatientSearchIntentVersion] = []
    for index, item in enumerate(raw["patient_search_intents"]):
        label = f"catalogue.patient_search_intents[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        _require_exact_keys(item, _INTENT_KEYS, label)
        if not isinstance(item["branch_offering_inference_allowed"], bool):
            raise ValueError(f"{label}.branch_offering_inference_allowed must be a bool")
        rendered_term = _require_text(
            item["rendered_treatment_term"], f"{label}.rendered_treatment_term"
        )
        if sha256_bytes(rendered_term.encode("utf-8")) != item["rendered_treatment_term_sha256"]:
            raise ValueError(f"{label}.rendered_treatment_term_sha256 mismatch")
        intents.append(
            PatientSearchIntentVersion(
                patient_search_intent_id=item["patient_search_intent_id"],
                version_row_id=item["version_row_id"],
                catalogue_release_id=release.id,
                language_locale=item["language_locale"],
                exact_patient_phrase=item["exact_patient_phrase"],
                exact_phrase_content_sha256=item["exact_phrase_content_sha256"],
                rendered_treatment_term_sha256=item["rendered_treatment_term_sha256"],
                target_concept_ids=_require_text_list(
                    item["target_concept_ids"], f"{label}.target_concept_ids"
                ),
                branch_offering_inference_allowed=item[
                    "branch_offering_inference_allowed"
                ],
                **_policy_kwargs(item, label),
            )
        )

    catalogue = TreatmentCatalogue(
        schema_version=raw["schema_version"],
        release=release,
        concepts=tuple(concepts),
        terms=tuple(terms),
        patient_search_intents=tuple(intents),
    )
    _validate_catalogue(catalogue, raw)
    return catalogue


def _validate_catalogue(catalogue: TreatmentCatalogue, raw: Mapping[str, Any]) -> None:
    collections = (
        ("concept_id", [item.concept_id for item in catalogue.concepts], True),
        (
            "concept version_row_id",
            [item.version_row_id for item in catalogue.concepts],
            False,
        ),
        ("term_id", [item.term_id for item in catalogue.terms], True),
        (
            "term version_row_id",
            [item.version_row_id for item in catalogue.terms],
            False,
        ),
        (
            "patient_search_intent_id",
            [item.patient_search_intent_id for item in catalogue.patient_search_intents],
            True,
        ),
        (
            "intent version_row_id",
            [item.version_row_id for item in catalogue.patient_search_intents],
            False,
        ),
    )
    for label, values, require_sorted in collections:
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate {label}")
        if require_sorted and values != sorted(values):
            raise ValueError(f"{label} rows must be sorted deterministically")

    concept_by_id = catalogue.concept_by_id
    concept_ids = set(concept_by_id)
    for item in (*catalogue.terms, *catalogue.patient_search_intents):
        unknown = sorted(set(item.target_concept_ids) - concept_ids)
        if unknown:
            raise ValueError(f"{item.version_row_id} references unknown concepts: {unknown}")
        if item.is_deterministically_authoritative:
            inactive = sorted(
                concept_id
                for concept_id in item.target_concept_ids
                if not concept_by_id[concept_id].is_deterministically_authoritative
            )
            if inactive:
                raise ValueError(
                    f"authoritative row {item.version_row_id} targets "
                    f"non-authoritative concepts: {inactive}"
                )

    if not isinstance(raw["concepts"], list) or not isinstance(raw["terms"], list):
        raise ValueError("catalogue collections must be lists")
    if not isinstance(raw["patient_search_intents"], list):
        raise ValueError("catalogue patient_search_intents must be a list")


_LEGACY_TOP_LEVEL_KEYS = {
    "schema_version",
    "catalogue_release_id",
    "source_catalogue_path",
    "source_catalogue_sha256",
    "mappings",
    "payload_sha256",
}
_LEGACY_MAPPING_KEYS = {
    "legacy_slug",
    "legacy_name",
    "classification",
    "review_state",
    "target_concept_ids",
    "target_search_intent_ids",
    "candidate_target_concept_ids",
    "candidate_target_search_intent_ids",
    "reason_summary",
}


def load_legacy_mappings(
    path: Path | str, *, catalogue: TreatmentCatalogue
) -> LegacyTreatmentMappingRelease:
    source_path = Path(path)
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("legacy mapping release must be a JSON object")
    _require_exact_keys(raw, _LEGACY_TOP_LEVEL_KEYS, "legacy mapping release")
    if raw["schema_version"] != LEGACY_MAPPING_SCHEMA_VERSION:
        raise ValueError("unsupported legacy mapping schema_version")
    if raw["payload_sha256"] != _payload_sha256(raw):
        raise ValueError("legacy mapping payload_sha256 mismatch")
    if raw["catalogue_release_id"] != catalogue.release.id:
        raise ValueError("legacy mapping catalogue_release_id mismatch")
    if raw["source_catalogue_path"] != catalogue.release.source_catalogue_path:
        raise ValueError("legacy mapping source catalogue path mismatch")
    if raw["source_catalogue_sha256"] != catalogue.release.source_catalogue_sha256:
        raise ValueError("legacy mapping source catalogue hash mismatch")

    mappings: list[LegacyTreatmentMapping] = []
    for index, item in enumerate(raw["mappings"]):
        label = f"legacy mappings[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        _require_exact_keys(item, _LEGACY_MAPPING_KEYS, label)
        mappings.append(
            LegacyTreatmentMapping(
                legacy_slug=item["legacy_slug"],
                legacy_name=item["legacy_name"],
                classification=_enum(
                    TreatmentReviewClassification,
                    item["classification"],
                    f"{label}.classification",
                ),
                review_state=_enum(
                    TreatmentReviewState, item["review_state"], f"{label}.review_state"
                ),
                target_concept_ids=_require_text_list(
                    item["target_concept_ids"], f"{label}.target_concept_ids"
                ),
                target_search_intent_ids=_require_text_list(
                    item["target_search_intent_ids"],
                    f"{label}.target_search_intent_ids",
                ),
                candidate_target_concept_ids=_require_text_list(
                    item["candidate_target_concept_ids"],
                    f"{label}.candidate_target_concept_ids",
                ),
                candidate_target_search_intent_ids=_require_text_list(
                    item["candidate_target_search_intent_ids"],
                    f"{label}.candidate_target_search_intent_ids",
                ),
                reason_summary=item["reason_summary"],
            )
        )

    slugs = [item.legacy_slug for item in mappings]
    if len(slugs) != len(set(slugs)):
        raise ValueError("legacy mappings contain duplicate slugs")
    if slugs != sorted(slugs):
        raise ValueError("legacy mappings must be sorted by legacy_slug")
    concept_by_id = catalogue.concept_by_id
    intent_by_id = catalogue.intent_by_id
    concept_ids = set(concept_by_id)
    intent_ids = set(intent_by_id)
    for item in mappings:
        referenced_concepts = {
            *item.target_concept_ids,
            *item.candidate_target_concept_ids,
        }
        referenced_intents = {
            *item.target_search_intent_ids,
            *item.candidate_target_search_intent_ids,
        }
        unknown_concepts = sorted(referenced_concepts - concept_ids)
        unknown_intents = sorted(referenced_intents - intent_ids)
        if unknown_concepts or unknown_intents:
            raise ValueError(
                f"legacy mapping {item.legacy_slug} has unknown targets: "
                f"concepts={unknown_concepts}, intents={unknown_intents}"
            )
        if item.is_deterministically_authoritative:
            inactive_concepts = sorted(
                concept_id
                for concept_id in item.target_concept_ids
                if not concept_by_id[concept_id].is_deterministically_authoritative
            )
            inactive_intents = sorted(
                intent_id
                for intent_id in item.target_search_intent_ids
                if not intent_by_id[intent_id].is_deterministically_authoritative
            )
            if inactive_concepts or inactive_intents:
                raise ValueError(
                    f"approved legacy mapping {item.legacy_slug} targets "
                    f"non-authoritative rows: concepts={inactive_concepts}, "
                    f"intents={inactive_intents}"
                )

    return LegacyTreatmentMappingRelease(
        schema_version=raw["schema_version"],
        catalogue_release_id=raw["catalogue_release_id"],
        source_catalogue_path=raw["source_catalogue_path"],
        source_catalogue_sha256=raw["source_catalogue_sha256"],
        payload_sha256=raw["payload_sha256"],
        mappings=tuple(mappings),
    )


def verify_source_catalogue(
    source_path: Path | str,
    *,
    catalogue: TreatmentCatalogue,
    mappings: LegacyTreatmentMappingRelease,
) -> None:
    if mappings.catalogue_release_id != catalogue.release.id:
        raise ValueError("legacy mapping catalogue_release_id mismatch")
    if mappings.source_catalogue_path != catalogue.release.source_catalogue_path:
        raise ValueError("legacy mapping source catalogue path mismatch")
    path = Path(source_path)
    source_bytes = path.read_bytes()
    digest = sha256_bytes(source_bytes)
    if digest != catalogue.release.source_catalogue_sha256:
        raise ValueError("source treatment catalogue bytes do not match reviewed release")
    if digest != mappings.source_catalogue_sha256:
        raise ValueError("source treatment catalogue bytes do not match legacy mapping release")
    # Hash and parse the same immutable in-memory byte string so a replacement
    # between separate filesystem reads cannot split verification from coverage.
    rows = json.loads(source_bytes)
    if not isinstance(rows, list):
        raise ValueError("source treatment catalogue must be a list")
    if len(rows) != len(mappings.mappings) or any(
        not isinstance(item, dict) for item in rows
    ):
        raise ValueError(
            "legacy mapping release does not cover all source catalogue rows"
        )
    source_pairs = sorted(
        (
            _require_text(item.get("id"), "source treatment id"),
            _require_text(item.get("name"), "source treatment name"),
        )
        for item in rows
    )
    mapping_pairs = sorted((item.legacy_slug, item.legacy_name) for item in mappings.mappings)
    if source_pairs != mapping_pairs:
        raise ValueError("legacy mapping release does not cover the exact source catalogue")


__all__ = [
    "CATALOGUE_SCHEMA_VERSION",
    "LEGACY_MAPPING_SCHEMA_VERSION",
    "LegacyTreatmentMapping",
    "LegacyTreatmentMappingRelease",
    "TreatmentCatalogue",
    "TreatmentCatalogueRelease",
    "load_catalogue",
    "load_legacy_mappings",
    "verify_source_catalogue",
]
