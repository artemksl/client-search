"""Pure immutable contracts for treatment concepts, terms, and search intents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum


TREATMENT_CONCEPT_VERSION_SCHEMA = "treatment_library.concept_version.v1"
TREATMENT_TERM_VERSION_SCHEMA = "treatment_library.term_version.v1"
PATIENT_SEARCH_INTENT_VERSION_SCHEMA = "treatment_library.search_intent_version.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TreatmentTermKind(str, Enum):
    CANONICAL = "canonical"
    ALIAS = "alias"
    SPELLING = "spelling"
    BRAND = "brand"
    DEVICE = "device"
    SUBTYPE = "subtype"
    CONCERN = "concern"
    BROAD_CATEGORY = "broad_category"
    PACKAGE = "package"


class TreatmentReviewClassification(str, Enum):
    """Closed pre-approval outcomes for deterministic catalogue review."""

    EXISTING_TREATMENT = "existing_treatment"
    ALIAS_OR_SPELLING = "alias_or_spelling"
    BRAND_OR_DEVICE = "brand_or_device"
    SUBTYPE = "subtype"
    RELATED_BUT_DISTINCT = "related_but_distinct"
    PATIENT_CONCERN_OR_BROAD_CATEGORY = "patient_concern_or_broad_category"
    PACKAGE_CONTAINING_MULTIPLE_TREATMENTS = (
        "package_containing_multiple_treatments"
    )
    GENUINELY_NEW_TREATMENT = "genuinely_new_treatment"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class TreatmentLifecycleState(str, Enum):
    """Closed lifecycle for immutable catalogue version rows."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class TreatmentReviewState(str, Enum):
    """Human-review state; only ``approved`` rows can be authoritative."""

    REVIEW_REQUIRED = "review_required"
    DEFERRED = "deferred"
    APPROVED = "approved"
    REJECTED = "rejected"


class CampaignSelectabilityState(str, Enum):
    """Whether an exact patient intent may be selected for a campaign."""

    SELECTABLE = "selectable"
    EXCLUDED = "excluded"


class RecommendationPromptEligibilityState(str, Enum):
    """Whether recommendation slots may render an exact patient intent."""

    ELIGIBLE = "eligible"
    RESTRICTED_POM = "restricted_pom"
    EXCLUDED = "excluded"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")


def _require_sha256(value: str, field_name: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_policy_states(
    *,
    lifecycle_state: TreatmentLifecycleState,
    review_state: TreatmentReviewState,
    campaign_selectability_state: CampaignSelectabilityState,
    recommendation_prompt_eligibility_state: RecommendationPromptEligibilityState,
) -> None:
    if not isinstance(lifecycle_state, TreatmentLifecycleState):
        raise TypeError("lifecycle_state must be a TreatmentLifecycleState")
    if not isinstance(review_state, TreatmentReviewState):
        raise TypeError("review_state must be a TreatmentReviewState")
    if not isinstance(campaign_selectability_state, CampaignSelectabilityState):
        raise TypeError(
            "campaign_selectability_state must be a CampaignSelectabilityState"
        )
    if not isinstance(
        recommendation_prompt_eligibility_state,
        RecommendationPromptEligibilityState,
    ):
        raise TypeError(
            "recommendation_prompt_eligibility_state must be a "
            "RecommendationPromptEligibilityState"
        )

    authoritative = (
        lifecycle_state is TreatmentLifecycleState.ACTIVE
        and review_state is TreatmentReviewState.APPROVED
    )
    if lifecycle_state is TreatmentLifecycleState.ACTIVE and not authoritative:
        raise ValueError("active treatment rows must be approved")
    if review_state in {
        TreatmentReviewState.REVIEW_REQUIRED,
        TreatmentReviewState.DEFERRED,
    } and lifecycle_state is not TreatmentLifecycleState.PROPOSED:
        raise ValueError("review-required or deferred treatment rows must remain proposed")
    if lifecycle_state is TreatmentLifecycleState.PROPOSED and review_state not in {
        TreatmentReviewState.REVIEW_REQUIRED,
        TreatmentReviewState.DEFERRED,
    }:
        raise ValueError("proposed treatment rows must remain review-required or deferred")
    if (
        review_state is TreatmentReviewState.REJECTED
        and lifecycle_state is not TreatmentLifecycleState.RETIRED
    ):
        raise ValueError("rejected treatment rows must be retired")

    if campaign_selectability_state is CampaignSelectabilityState.SELECTABLE and not authoritative:
        raise ValueError("only active approved rows can be campaign-selectable")
    if (
        recommendation_prompt_eligibility_state
        is not RecommendationPromptEligibilityState.EXCLUDED
        and (
            not authoritative
            or campaign_selectability_state is not CampaignSelectabilityState.SELECTABLE
        )
    ):
        raise ValueError(
            "recommendation-prompt eligibility requires an active, approved, "
            "campaign-selectable row"
        )
    if lifecycle_state in {
        TreatmentLifecycleState.SUPERSEDED,
        TreatmentLifecycleState.RETIRED,
    } and (
        campaign_selectability_state is not CampaignSelectabilityState.EXCLUDED
        or recommendation_prompt_eligibility_state
        is not RecommendationPromptEligibilityState.EXCLUDED
    ):
        raise ValueError("inactive treatment rows must be excluded")


class _TreatmentPolicyMixin:
    lifecycle_state: TreatmentLifecycleState
    review_state: TreatmentReviewState
    campaign_selectability_state: CampaignSelectabilityState
    recommendation_prompt_eligibility_state: RecommendationPromptEligibilityState

    @property
    def is_deterministically_authoritative(self) -> bool:
        return (
            self.lifecycle_state is TreatmentLifecycleState.ACTIVE
            and self.review_state is TreatmentReviewState.APPROVED
        )

    @property
    def is_campaign_selectable(self) -> bool:
        return (
            self.is_deterministically_authoritative
            and self.campaign_selectability_state
            is CampaignSelectabilityState.SELECTABLE
        )

    @property
    def is_recommendation_prompt_eligible(self) -> bool:
        return (
            self.is_campaign_selectable
            and self.recommendation_prompt_eligibility_state
            is RecommendationPromptEligibilityState.ELIGIBLE
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TreatmentConceptVersion(_TreatmentPolicyMixin):
    concept_id: str
    version_row_id: str
    catalogue_release_id: str
    canonical_name: str
    lifecycle_state: TreatmentLifecycleState = TreatmentLifecycleState.PROPOSED
    review_state: TreatmentReviewState = TreatmentReviewState.REVIEW_REQUIRED
    campaign_selectability_state: CampaignSelectabilityState = (
        CampaignSelectabilityState.EXCLUDED
    )
    recommendation_prompt_eligibility_state: RecommendationPromptEligibilityState = (
        RecommendationPromptEligibilityState.EXCLUDED
    )
    schema_version: str = field(default=TREATMENT_CONCEPT_VERSION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "concept_id",
            "version_row_id",
            "catalogue_release_id",
            "canonical_name",
        ):
            _require_text(getattr(self, field_name), field_name)
        _validate_policy_states(
            lifecycle_state=self.lifecycle_state,
            review_state=self.review_state,
            campaign_selectability_state=self.campaign_selectability_state,
            recommendation_prompt_eligibility_state=self.recommendation_prompt_eligibility_state,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TreatmentTermVersion(_TreatmentPolicyMixin):
    term_id: str
    version_row_id: str
    catalogue_release_id: str
    term_kind: TreatmentTermKind
    exact_term: str
    term_content_sha256: str
    target_concept_ids: tuple[str, ...]
    lifecycle_state: TreatmentLifecycleState = TreatmentLifecycleState.PROPOSED
    review_state: TreatmentReviewState = TreatmentReviewState.REVIEW_REQUIRED
    campaign_selectability_state: CampaignSelectabilityState = (
        CampaignSelectabilityState.EXCLUDED
    )
    recommendation_prompt_eligibility_state: RecommendationPromptEligibilityState = (
        RecommendationPromptEligibilityState.EXCLUDED
    )
    schema_version: str = field(default=TREATMENT_TERM_VERSION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in ("term_id", "version_row_id", "catalogue_release_id", "exact_term"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.term_kind, TreatmentTermKind):
            raise TypeError("term_kind must be a TreatmentTermKind")
        _require_sha256(self.term_content_sha256, "term_content_sha256")
        if self.term_content_sha256 != _text_sha256(self.exact_term):
            raise ValueError("term_content_sha256 does not match exact_term")
        if not isinstance(self.target_concept_ids, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.target_concept_ids
        ):
            raise ValueError("target_concept_ids must be a tuple of non-empty IDs")
        if len(set(self.target_concept_ids)) != len(self.target_concept_ids):
            raise ValueError("target_concept_ids cannot contain duplicates")
        _validate_policy_states(
            lifecycle_state=self.lifecycle_state,
            review_state=self.review_state,
            campaign_selectability_state=self.campaign_selectability_state,
            recommendation_prompt_eligibility_state=self.recommendation_prompt_eligibility_state,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PatientSearchIntentVersion(_TreatmentPolicyMixin):
    """Exact approved wording; catalogue context is intentionally a sidecar."""

    patient_search_intent_id: str
    version_row_id: str
    catalogue_release_id: str
    language_locale: str
    exact_patient_phrase: str
    exact_phrase_content_sha256: str
    rendered_treatment_term_sha256: str
    target_concept_ids: tuple[str, ...] = ()
    lifecycle_state: TreatmentLifecycleState = TreatmentLifecycleState.PROPOSED
    review_state: TreatmentReviewState = TreatmentReviewState.REVIEW_REQUIRED
    campaign_selectability_state: CampaignSelectabilityState = (
        CampaignSelectabilityState.EXCLUDED
    )
    recommendation_prompt_eligibility_state: RecommendationPromptEligibilityState = (
        RecommendationPromptEligibilityState.EXCLUDED
    )
    branch_offering_inference_allowed: bool = False
    schema_version: str = field(default=PATIENT_SEARCH_INTENT_VERSION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "patient_search_intent_id",
            "version_row_id",
            "catalogue_release_id",
            "language_locale",
            "exact_patient_phrase",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(self.exact_phrase_content_sha256, "exact_phrase_content_sha256")
        _require_sha256(
            self.rendered_treatment_term_sha256,
            "rendered_treatment_term_sha256",
        )
        if self.exact_phrase_content_sha256 != _text_sha256(self.exact_patient_phrase):
            raise ValueError(
                "exact_phrase_content_sha256 does not match exact_patient_phrase"
            )
        if not isinstance(self.target_concept_ids, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.target_concept_ids
        ):
            raise ValueError("target_concept_ids must be a tuple of non-empty IDs")
        if len(set(self.target_concept_ids)) != len(self.target_concept_ids):
            raise ValueError("target_concept_ids cannot contain duplicates")
        _validate_policy_states(
            lifecycle_state=self.lifecycle_state,
            review_state=self.review_state,
            campaign_selectability_state=self.campaign_selectability_state,
            recommendation_prompt_eligibility_state=self.recommendation_prompt_eligibility_state,
        )
        if not isinstance(self.branch_offering_inference_allowed, bool):
            raise TypeError("branch_offering_inference_allowed must be a bool")
        if self.branch_offering_inference_allowed:
            raise ValueError(
                "patient search intents cannot infer branch offerings; use a reviewed "
                "branch-offering decision"
            )


__all__ = [
    "PATIENT_SEARCH_INTENT_VERSION_SCHEMA",
    "TREATMENT_CONCEPT_VERSION_SCHEMA",
    "TREATMENT_TERM_VERSION_SCHEMA",
    "CampaignSelectabilityState",
    "PatientSearchIntentVersion",
    "RecommendationPromptEligibilityState",
    "TreatmentConceptVersion",
    "TreatmentLifecycleState",
    "TreatmentReviewClassification",
    "TreatmentReviewState",
    "TreatmentTermKind",
    "TreatmentTermVersion",
]
