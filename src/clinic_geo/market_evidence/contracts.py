"""Pure exact-compatibility contracts for the Market Evidence Ledger."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

from clinic_geo.evidence.purpose import (
    EvidenceCompatibilityKey,
    EvidenceCompatibilityResult,
    EvidencePurpose,
    EvidencePurposeBinding,
    EvidenceRecordKind,
    compare_evidence_compatibility,
    validate_evidence_purpose,
)
from clinic_geo.integrity import canonical_json, canonical_sha256
from clinic_geo.query_geography.contracts import QueryGeographyVersion
from clinic_geo.treatment_library.contracts import PatientSearchIntentVersion


MARKET_MEASUREMENT_SPEC_V1_SCHEMA = "market_evidence.measurement_specification.v1"
MARKET_MEASUREMENT_SPEC_SCHEMA = "market_evidence.measurement_specification.v2"
MARKET_SUBJECT_V1_SCHEMA = "market_evidence.measurement_subject.v1"
MARKET_SUBJECT_SCHEMA = "market_evidence.measurement_subject.v2"
MARKET_PROMPT_IDENTITY_V1_SCHEMA = "market_evidence.prompt_identity.v1"
MARKET_PROMPT_IDENTITY_SCHEMA = "market_evidence.prompt_identity.v2"
MARKET_QUERY_GEOGRAPHY_IDENTITY_SCHEMA = "market_evidence.query_geography_identity.v1"
MARKET_PLATFORM_CONTEXT_SCHEMA = "market_evidence.platform_context.v1"
MARKET_COLLECTION_REGIME_SCHEMA = "market_evidence.collection_regime.v1"
MARKET_RUN_PROTOCOL_SCHEMA = "market_evidence.run_protocol.v1"
MARKET_PROFILE_ASSIGNMENT_SCHEMA = "market_evidence.profile_assignment.v1"
MARKET_PROFILE_ASSIGNMENT_PLAN_SCHEMA = (
    "market_evidence.profile_assignment_plan.v1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPLICIT_SURFACE_EPOCH_RE = re.compile(
    r"^[^\s*?@]+@surface_epoch_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    re.IGNORECASE,
)
_MODEL_WILDCARD_TOKENS = frozenset(
    {"any", "auto", "current", "default", "latest", "unspecified", "wildcard"}
)


class MeasurementSubjectKind(str, Enum):
    TREATMENT_VISIBILITY = "treatment_visibility"
    ARCHETYPE_RESEARCH = "archetype_research"


class SourcePlatform(str, Enum):
    CHATGPT = "chatgpt"
    GEMINI = "gemini"
    GOOGLE_AI_OVERVIEW = "google_ai_overview"
    COPILOT = "copilot"


class PlatformDataMode(str, Enum):
    LIVE = "live"
    DERIVED = "derived"


class AccessAuthenticationMode(str, Enum):
    LOGGED_IN = "logged_in"
    SIGNED_OUT = "signed_out"
    DERIVED = "derived"


class SettingState(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileRunAssignment:
    """The exact planned profile identity for one required run."""

    run_ordinal: int
    planned_profile_id: str
    planned_profile_revision: str
    schema_version: str = field(default=MARKET_PROFILE_ASSIGNMENT_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.run_ordinal, bool)
            or not isinstance(self.run_ordinal, int)
            or self.run_ordinal not in {1, 2, 3}
        ):
            raise ValueError("run_ordinal must be exactly 1, 2, or 3")
        for field_name in ("planned_profile_id", "planned_profile_revision"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
            if value != value.strip():
                raise ValueError(f"{field_name} cannot contain surrounding whitespace")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_ordinal": self.run_ordinal,
            "planned_profile_id": self.planned_profile_id,
            "planned_profile_revision": self.planned_profile_revision,
        }


def canonical_profile_assignments(
    assignments: tuple[ProfileRunAssignment, ...],
) -> tuple[ProfileRunAssignment, ...]:
    """Validate and order the complete three-run profile allocation."""

    if not isinstance(assignments, tuple) or any(
        not isinstance(item, ProfileRunAssignment) for item in assignments
    ):
        raise TypeError("assignments must be a tuple of ProfileRunAssignment values")
    ordered = tuple(sorted(assignments, key=lambda item: item.run_ordinal))
    if tuple(item.run_ordinal for item in ordered) != (1, 2, 3):
        raise ValueError("assignments must contain exactly runs 1, 2, and 3")
    return ordered


def profile_assignment_plan_sha256(
    assignments: tuple[ProfileRunAssignment, ...],
) -> str:
    """Hash the exact profile IDs/revisions allocated to all three runs."""

    ordered = canonical_profile_assignments(assignments)
    return canonical_sha256(
        {
            "schema_version": MARKET_PROFILE_ASSIGNMENT_PLAN_SCHEMA,
            "assignments": [
                {
                    "run_ordinal": item.run_ordinal,
                    "planned_profile_id": item.planned_profile_id,
                    "planned_profile_revision": item.planned_profile_revision,
                }
                for item in ordered
            ],
        }
    )


def _require_text(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _require_sha256(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketMeasurementSubjectV1:
    """One treatment intent or one treatment-independent archetype subject.

    Both variants serialize the same key set. Inapplicable optional keys are
    explicit JSON nulls rather than omitted values.
    """

    kind: MeasurementSubjectKind
    patient_search_intent_id: str | None = None
    patient_search_intent_version_id: str | None = None
    language_locale: str | None = None
    exact_patient_phrase: str | None = None
    exact_phrase_content_sha256: str | None = None
    rendered_treatment_term_sha256: str | None = None
    archetype_id: str | None = None
    archetype_revision: str | None = None
    archetype_wording_content_sha256: str | None = None
    schema_version: str = field(default=MARKET_SUBJECT_V1_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_enum(self.kind, MeasurementSubjectKind, "kind")
        treatment_fields = (
            "patient_search_intent_id",
            "patient_search_intent_version_id",
            "language_locale",
            "exact_patient_phrase",
            "exact_phrase_content_sha256",
            "rendered_treatment_term_sha256",
        )
        archetype_fields = (
            "archetype_id",
            "archetype_revision",
            "archetype_wording_content_sha256",
        )
        required = treatment_fields if self.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY else archetype_fields
        forbidden = archetype_fields if self.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY else treatment_fields
        for field_name in required:
            value = getattr(self, field_name)
            if field_name.endswith("sha256"):
                _require_sha256(value, field_name)
            else:
                _require_text(value, field_name)
        if self.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY:
            expected_phrase_hash = hashlib.sha256(
                self.exact_patient_phrase.encode("utf-8")
            ).hexdigest()
            if self.exact_phrase_content_sha256 != expected_phrase_hash:
                raise ValueError(
                    "exact_phrase_content_sha256 does not match exact_patient_phrase"
                )
        if any(getattr(self, field_name) is not None for field_name in forbidden):
            raise ValueError(f"{self.kind.value} cannot carry fields from the other subject kind")

    @classmethod
    def for_search_intent(
        cls, intent: PatientSearchIntentVersion
    ) -> "MarketMeasurementSubjectV1":
        if not isinstance(intent, PatientSearchIntentVersion):
            raise TypeError("intent must be a PatientSearchIntentVersion")
        return cls(
            kind=MeasurementSubjectKind.TREATMENT_VISIBILITY,
            patient_search_intent_id=intent.patient_search_intent_id,
            patient_search_intent_version_id=intent.version_row_id,
            language_locale=intent.language_locale,
            exact_patient_phrase=intent.exact_patient_phrase,
            exact_phrase_content_sha256=intent.exact_phrase_content_sha256,
            rendered_treatment_term_sha256=intent.rendered_treatment_term_sha256,
        )

    @classmethod
    def for_archetype(
        cls,
        *,
        archetype_id: str,
        archetype_revision: str,
        archetype_wording_content_sha256: str,
    ) -> "MarketMeasurementSubjectV1":
        return cls(
            kind=MeasurementSubjectKind.ARCHETYPE_RESEARCH,
            archetype_id=archetype_id,
            archetype_revision=archetype_revision,
            archetype_wording_content_sha256=archetype_wording_content_sha256,
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "patient_search_intent_id": self.patient_search_intent_id,
            "patient_search_intent_version_id": self.patient_search_intent_version_id,
            "language_locale": self.language_locale,
            "exact_patient_phrase": self.exact_patient_phrase,
            "exact_phrase_content_sha256": self.exact_phrase_content_sha256,
            "rendered_treatment_term_sha256": self.rendered_treatment_term_sha256,
            "archetype_id": self.archetype_id,
            "archetype_revision": self.archetype_revision,
            "archetype_wording_content_sha256": self.archetype_wording_content_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketMeasurementSubject:
    """Current exact subject identity, including rendered treatment-term bytes.

    The v1 predecessor recorded only the rendered-term hash.  It remains
    available as :class:`MarketMeasurementSubjectV1` for frozen WP1 vectors,
    while every new demand uses this successor contract so the unhashed source
    text and its digest are validated together.
    """

    kind: MeasurementSubjectKind
    patient_search_intent_id: str | None = None
    patient_search_intent_version_id: str | None = None
    language_locale: str | None = None
    exact_patient_phrase: str | None = None
    exact_phrase_content_sha256: str | None = None
    exact_rendered_treatment_term: str | None = None
    rendered_treatment_term_sha256: str | None = None
    archetype_id: str | None = None
    archetype_revision: str | None = None
    archetype_wording_content_sha256: str | None = None
    schema_version: str = field(default=MARKET_SUBJECT_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_enum(self.kind, MeasurementSubjectKind, "kind")
        treatment_fields = (
            "patient_search_intent_id",
            "patient_search_intent_version_id",
            "language_locale",
            "exact_patient_phrase",
            "exact_phrase_content_sha256",
            "exact_rendered_treatment_term",
            "rendered_treatment_term_sha256",
        )
        archetype_fields = (
            "archetype_id",
            "archetype_revision",
            "archetype_wording_content_sha256",
        )
        required = (
            treatment_fields
            if self.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY
            else archetype_fields
        )
        forbidden = (
            archetype_fields
            if self.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY
            else treatment_fields
        )
        for field_name in required:
            value = getattr(self, field_name)
            if field_name.endswith("sha256"):
                _require_sha256(value, field_name)
            else:
                _require_text(value, field_name)
        if self.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY:
            expected_phrase_hash = hashlib.sha256(
                self.exact_patient_phrase.encode("utf-8")
            ).hexdigest()
            if self.exact_phrase_content_sha256 != expected_phrase_hash:
                raise ValueError(
                    "exact_phrase_content_sha256 does not match exact_patient_phrase"
                )
            expected_term_hash = hashlib.sha256(
                self.exact_rendered_treatment_term.encode("utf-8")
            ).hexdigest()
            if self.rendered_treatment_term_sha256 != expected_term_hash:
                raise ValueError(
                    "rendered_treatment_term_sha256 does not match "
                    "exact_rendered_treatment_term"
                )
        if any(getattr(self, field_name) is not None for field_name in forbidden):
            raise ValueError(
                f"{self.kind.value} cannot carry fields from the other subject kind"
            )

    @classmethod
    def for_search_intent(
        cls,
        intent: PatientSearchIntentVersion,
        *,
        exact_rendered_treatment_term: str,
    ) -> "MarketMeasurementSubject":
        if not isinstance(intent, PatientSearchIntentVersion):
            raise TypeError("intent must be a PatientSearchIntentVersion")
        return cls(
            kind=MeasurementSubjectKind.TREATMENT_VISIBILITY,
            patient_search_intent_id=intent.patient_search_intent_id,
            patient_search_intent_version_id=intent.version_row_id,
            language_locale=intent.language_locale,
            exact_patient_phrase=intent.exact_patient_phrase,
            exact_phrase_content_sha256=intent.exact_phrase_content_sha256,
            exact_rendered_treatment_term=exact_rendered_treatment_term,
            rendered_treatment_term_sha256=intent.rendered_treatment_term_sha256,
        )

    @classmethod
    def for_archetype(
        cls,
        *,
        archetype_id: str,
        archetype_revision: str,
        archetype_wording_content_sha256: str,
    ) -> "MarketMeasurementSubject":
        return cls(
            kind=MeasurementSubjectKind.ARCHETYPE_RESEARCH,
            archetype_id=archetype_id,
            archetype_revision=archetype_revision,
            archetype_wording_content_sha256=archetype_wording_content_sha256,
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "patient_search_intent_id": self.patient_search_intent_id,
            "patient_search_intent_version_id": self.patient_search_intent_version_id,
            "language_locale": self.language_locale,
            "exact_patient_phrase": self.exact_patient_phrase,
            "exact_phrase_content_sha256": self.exact_phrase_content_sha256,
            "exact_rendered_treatment_term": self.exact_rendered_treatment_term,
            "rendered_treatment_term_sha256": self.rendered_treatment_term_sha256,
            "archetype_id": self.archetype_id,
            "archetype_revision": self.archetype_revision,
            "archetype_wording_content_sha256": self.archetype_wording_content_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptIdentityV1:
    """Frozen prompt identity retained for WP1 fixture reconstruction."""

    prompt_pack_version: str
    prompt_catalogue_version: str
    prompt_catalogue_content_sha256: str
    basket: str
    semantic_slot: str
    semantic_intent: str
    renderer_contract_version: str
    exact_template_sha256: str
    exact_rendering_bindings_sha256: str
    exact_rendered_prompt_sha256: str
    schema_version: str = field(
        default=MARKET_PROMPT_IDENTITY_V1_SCHEMA, init=False
    )

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_pack_version",
            "prompt_catalogue_version",
            "basket",
            "semantic_slot",
            "semantic_intent",
            "renderer_contract_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "prompt_catalogue_content_sha256",
            "exact_template_sha256",
            "exact_rendering_bindings_sha256",
            "exact_rendered_prompt_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "prompt_pack_version": self.prompt_pack_version,
            "prompt_catalogue_version": self.prompt_catalogue_version,
            "prompt_catalogue_content_sha256": self.prompt_catalogue_content_sha256,
            "basket": self.basket,
            "semantic_slot": self.semantic_slot,
            "semantic_intent": self.semantic_intent,
            "renderer_contract_version": self.renderer_contract_version,
            "exact_template_sha256": self.exact_template_sha256,
            "exact_rendering_bindings_sha256": self.exact_rendering_bindings_sha256,
            "exact_rendered_prompt_sha256": self.exact_rendered_prompt_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptIdentity:
    """Current prompt identity, binding pack and catalogue source bytes."""

    prompt_pack_version: str
    prompt_pack_content_sha256: str
    prompt_catalogue_version: str
    prompt_catalogue_content_sha256: str
    basket: str
    semantic_slot: str
    semantic_intent: str
    renderer_contract_version: str
    exact_template_sha256: str
    exact_rendering_bindings_sha256: str
    exact_rendered_prompt_sha256: str
    schema_version: str = field(default=MARKET_PROMPT_IDENTITY_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_pack_version",
            "prompt_catalogue_version",
            "basket",
            "semantic_slot",
            "semantic_intent",
            "renderer_contract_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "prompt_pack_content_sha256",
            "prompt_catalogue_content_sha256",
            "exact_template_sha256",
            "exact_rendering_bindings_sha256",
            "exact_rendered_prompt_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "prompt_pack_version": self.prompt_pack_version,
            "prompt_pack_content_sha256": self.prompt_pack_content_sha256,
            "prompt_catalogue_version": self.prompt_catalogue_version,
            "prompt_catalogue_content_sha256": self.prompt_catalogue_content_sha256,
            "basket": self.basket,
            "semantic_slot": self.semantic_slot,
            "semantic_intent": self.semantic_intent,
            "renderer_contract_version": self.renderer_contract_version,
            "exact_template_sha256": self.exact_template_sha256,
            "exact_rendering_bindings_sha256": self.exact_rendering_bindings_sha256,
            "exact_rendered_prompt_sha256": self.exact_rendered_prompt_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryGeographyIdentity:
    """One exact approved area, greater-area, or city geography version.

    Patient-query geography has no station, postcode, or fallback sub-identity.
    Physical-address postcodes remain a separate branch-attribution concern.
    """

    geography: QueryGeographyVersion
    schema_version: str = field(
        default=MARKET_QUERY_GEOGRAPHY_IDENTITY_SCHEMA, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.geography, QueryGeographyVersion):
            raise TypeError("geography must be a QueryGeographyVersion")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "query_geography_version": self.geography.to_canonical_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformContext:
    source_platform: SourcePlatform
    product_surface: str
    data_mode: PlatformDataMode
    adapter_version: str
    requested_model_contract: str
    observed_surface_model_epoch_contract: str
    schema_version: str = field(default=MARKET_PLATFORM_CONTEXT_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_enum(self.source_platform, SourcePlatform, "source_platform")
        _require_enum(self.data_mode, PlatformDataMode, "data_mode")
        for field_name in (
            "product_surface",
            "adapter_version",
            "requested_model_contract",
            "observed_surface_model_epoch_contract",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "requested_model_contract",
            "observed_surface_model_epoch_contract",
        ):
            value = getattr(self, field_name).strip()
            ordered_tokens = tuple(
                token
                for token in re.split(r"[^a-z0-9]+", value.casefold())
                if token
            )
            if (
                set(ordered_tokens).intersection(_MODEL_WILDCARD_TOKENS)
                or "*" in value
                or "?" in value
            ):
                raise ValueError(f"{field_name} cannot be a wildcard")
        observed = self.observed_surface_model_epoch_contract.strip()
        explicit_surface_epoch = _EXPLICIT_SURFACE_EPOCH_RE.fullmatch(observed)
        if not explicit_surface_epoch:
            raise ValueError(
                "observed model identity requires an explicit surface epoch"
            )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source_platform": self.source_platform.value,
            "product_surface": self.product_surface,
            "data_mode": self.data_mode.value,
            "adapter_version": self.adapter_version,
            "requested_model_contract": self.requested_model_contract,
            "observed_surface_model_epoch_contract": self.observed_surface_model_epoch_contract,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionRegime:
    access_authentication_mode: AccessAuthenticationMode
    platform_account_cohort_id: str
    platform_account_cohort_revision: str
    profile_pool_id: str
    profile_pool_revision: str
    profile_ordering_sha256: str
    profile_allocation_policy_version: str
    fresh_conversation_search_policy_version: str
    project_setting: SettingState
    history_setting: SettingState
    personalisation_setting: SettingState
    search_setting: SettingState
    ui_language: str
    browser_locale: str
    accept_language: str
    timezone: str
    market_country: str
    google_q_policy: str
    google_hl_policy: str
    google_gl_policy: str
    google_pws_policy: str
    device_user_agent_class: str
    network_vpn_egress_region_policy: str
    schema_version: str = field(default=MARKET_COLLECTION_REGIME_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_enum(
            self.access_authentication_mode,
            AccessAuthenticationMode,
            "access_authentication_mode",
        )
        for field_name in (
            "project_setting",
            "history_setting",
            "personalisation_setting",
            "search_setting",
        ):
            _require_enum(getattr(self, field_name), SettingState, field_name)
        for field_name in (
            "platform_account_cohort_id",
            "platform_account_cohort_revision",
            "profile_pool_id",
            "profile_pool_revision",
            "profile_allocation_policy_version",
            "fresh_conversation_search_policy_version",
            "ui_language",
            "browser_locale",
            "accept_language",
            "timezone",
            "market_country",
            "google_q_policy",
            "google_hl_policy",
            "google_gl_policy",
            "google_pws_policy",
            "device_user_agent_class",
            "network_vpn_egress_region_policy",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(self.profile_ordering_sha256, "profile_ordering_sha256")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "access_authentication_mode": self.access_authentication_mode.value,
            "platform_account_cohort_id": self.platform_account_cohort_id,
            "platform_account_cohort_revision": self.platform_account_cohort_revision,
            "profile_pool_id": self.profile_pool_id,
            "profile_pool_revision": self.profile_pool_revision,
            "profile_ordering_sha256": self.profile_ordering_sha256,
            "profile_allocation_policy_version": self.profile_allocation_policy_version,
            "fresh_conversation_search_policy_version": self.fresh_conversation_search_policy_version,
            "project_setting": self.project_setting.value,
            "history_setting": self.history_setting.value,
            "personalisation_setting": self.personalisation_setting.value,
            "search_setting": self.search_setting.value,
            "ui_language": self.ui_language,
            "browser_locale": self.browser_locale,
            "accept_language": self.accept_language,
            "timezone": self.timezone,
            "market_country": self.market_country,
            "google_q_policy": self.google_q_policy,
            "google_hl_policy": self.google_hl_policy,
            "google_gl_policy": self.google_gl_policy,
            "google_pws_policy": self.google_pws_policy,
            "device_user_agent_class": self.device_user_agent_class,
            "network_vpn_egress_region_policy": self.network_vpn_egress_region_policy,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RunProtocol:
    coherent_window_policy_version: str
    maximum_window_span_policy: str
    retry_policy_version: str
    required_runs: int = 3
    schema_version: str = field(default=MARKET_RUN_PROTOCOL_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if self.required_runs != 3 or isinstance(self.required_runs, bool):
            raise ValueError("Market Evidence Ledger run protocol requires exactly 3 runs")
        for field_name in (
            "coherent_window_policy_version",
            "maximum_window_span_policy",
            "retry_policy_version",
        ):
            _require_text(getattr(self, field_name), field_name)

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "required_runs": self.required_runs,
            "coherent_window_policy_version": self.coherent_window_policy_version,
            "maximum_window_span_policy": self.maximum_window_span_policy,
            "retry_policy_version": self.retry_policy_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketMeasurementSpecificationV1:
    """Frozen WP1 exact-reuse identity retained for fixture reconstruction."""

    subject: MarketMeasurementSubjectV1
    prompt: PromptIdentityV1
    query_geography: QueryGeographyIdentity
    platform_context: PlatformContext
    collection_regime: CollectionRegime
    run_protocol: RunProtocol
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    schema_version: str = field(default=MARKET_MEASUREMENT_SPEC_V1_SCHEMA, init=False)

    def __post_init__(self) -> None:
        purpose = validate_evidence_purpose(self.purpose)
        if purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError(
                "market measurement specifications require market_discovery purpose"
            )
        object.__setattr__(self, "purpose", purpose)
        expected = (
            (self.subject, MarketMeasurementSubjectV1, "subject"),
            (self.prompt, PromptIdentityV1, "prompt"),
            (self.query_geography, QueryGeographyIdentity, "query_geography"),
            (self.platform_context, PlatformContext, "platform_context"),
            (self.collection_regime, CollectionRegime, "collection_regime"),
            (self.run_protocol, RunProtocol, "run_protocol"),
        )
        for value, value_type, field_name in expected:
            if not isinstance(value, value_type):
                raise TypeError(f"{field_name} must be a {value_type.__name__}")

    def to_canonical_dict(self) -> dict:
        """Return the closed hash payload, including every optional key."""

        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose.value,
            "subject": self.subject.to_canonical_dict(),
            "prompt": self.prompt.to_canonical_dict(),
            "query_geography": self.query_geography.to_canonical_dict(),
            "platform_context": self.platform_context.to_canonical_dict(),
            "collection_regime": self.collection_regime.to_canonical_dict(),
            "run_protocol": self.run_protocol.to_canonical_dict(),
        }

    @property
    def measurement_spec_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())

    @property
    def spec_sha256(self) -> str:
        return self.measurement_spec_sha256

    @property
    def compatibility_key(self) -> EvidenceCompatibilityKey:
        return EvidenceCompatibilityKey(
            purpose=self.purpose,
            identity_sha256=self.measurement_spec_sha256,
        )

    def canonical_json(self) -> str:
        return canonical_json(self.to_canonical_dict())

    def compatibility_with(
        self, other: "MarketMeasurementSpecificationV1"
    ) -> EvidenceCompatibilityResult:
        if not isinstance(other, MarketMeasurementSpecificationV1):
            raise TypeError("other must be a MarketMeasurementSpecificationV1")
        return compare_evidence_compatibility(
            self.compatibility_key, other.compatibility_key
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketMeasurementSpecification:
    """Current canonical exact-reuse identity for new Market demand."""

    subject: MarketMeasurementSubject
    prompt: PromptIdentity
    query_geography: QueryGeographyIdentity
    platform_context: PlatformContext
    collection_regime: CollectionRegime
    run_protocol: RunProtocol
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    schema_version: str = field(default=MARKET_MEASUREMENT_SPEC_SCHEMA, init=False)

    def __post_init__(self) -> None:
        purpose = validate_evidence_purpose(self.purpose)
        if purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError(
                "market measurement specifications require market_discovery purpose"
            )
        object.__setattr__(self, "purpose", purpose)
        expected = (
            (self.subject, MarketMeasurementSubject, "subject"),
            (self.prompt, PromptIdentity, "prompt"),
            (self.query_geography, QueryGeographyIdentity, "query_geography"),
            (self.platform_context, PlatformContext, "platform_context"),
            (self.collection_regime, CollectionRegime, "collection_regime"),
            (self.run_protocol, RunProtocol, "run_protocol"),
        )
        for value, value_type, field_name in expected:
            if not isinstance(value, value_type):
                raise TypeError(f"{field_name} must be a {value_type.__name__}")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose.value,
            "subject": self.subject.to_canonical_dict(),
            "prompt": self.prompt.to_canonical_dict(),
            "query_geography": self.query_geography.to_canonical_dict(),
            "platform_context": self.platform_context.to_canonical_dict(),
            "collection_regime": self.collection_regime.to_canonical_dict(),
            "run_protocol": self.run_protocol.to_canonical_dict(),
        }

    @property
    def measurement_spec_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())

    @property
    def spec_sha256(self) -> str:
        return self.measurement_spec_sha256

    @property
    def compatibility_key(self) -> EvidenceCompatibilityKey:
        return EvidenceCompatibilityKey(
            purpose=self.purpose,
            identity_sha256=self.measurement_spec_sha256,
        )

    def canonical_json(self) -> str:
        return canonical_json(self.to_canonical_dict())

    def compatibility_with(
        self, other: "MarketMeasurementSpecification"
    ) -> EvidenceCompatibilityResult:
        if not isinstance(other, MarketMeasurementSpecification):
            raise TypeError("other must be a MarketMeasurementSpecification")
        return compare_evidence_compatibility(
            self.compatibility_key, other.compatibility_key
        )


def compare_measurement_specifications(
    left: MarketMeasurementSpecification,
    right: MarketMeasurementSpecification,
) -> EvidenceCompatibilityResult:
    if not isinstance(left, MarketMeasurementSpecification) or not isinstance(
        right, MarketMeasurementSpecification
    ):
        raise TypeError(
            "current compatibility requires MarketMeasurementSpecification v2 values"
        )
    return left.compatibility_with(right)


def measurement_specifications_compatible(
    left: MarketMeasurementSpecification,
    right: MarketMeasurementSpecification,
) -> bool:
    return compare_measurement_specifications(left, right).compatible


def market_purpose_binding(
    record_kind: EvidenceRecordKind,
    record_id: str,
    *,
    parent: EvidencePurposeBinding | None = None,
) -> EvidencePurposeBinding:
    """Build a system-owned Market Evidence purpose edge.

    This is the pure contract used by future window, observation, attempt,
    artifact, occurrence, classification, and projection stores.  Supplying a
    Snapshot or outreach parent fails before persistence.
    """

    if parent is None:
        return EvidencePurposeBinding(
            record_kind=record_kind,
            record_id=record_id,
            purpose=EvidencePurpose.MARKET_DISCOVERY,
        )
    return EvidencePurposeBinding(
        record_kind=record_kind,
        record_id=record_id,
        purpose=EvidencePurpose.MARKET_DISCOVERY,
        parent_record_kind=parent.record_kind,
        parent_record_id=parent.record_id,
        parent_purpose=parent.purpose,
    )


MeasurementSpecification = MarketMeasurementSpecification
MeasurementSpecificationV1 = MarketMeasurementSpecificationV1


__all__ = [
    "MARKET_COLLECTION_REGIME_SCHEMA",
    "MARKET_MEASUREMENT_SPEC_SCHEMA",
    "MARKET_MEASUREMENT_SPEC_V1_SCHEMA",
    "MARKET_PLATFORM_CONTEXT_SCHEMA",
    "MARKET_PROFILE_ASSIGNMENT_PLAN_SCHEMA",
    "MARKET_PROFILE_ASSIGNMENT_SCHEMA",
    "MARKET_PROMPT_IDENTITY_SCHEMA",
    "MARKET_PROMPT_IDENTITY_V1_SCHEMA",
    "MARKET_QUERY_GEOGRAPHY_IDENTITY_SCHEMA",
    "MARKET_RUN_PROTOCOL_SCHEMA",
    "MARKET_SUBJECT_SCHEMA",
    "MARKET_SUBJECT_V1_SCHEMA",
    "AccessAuthenticationMode",
    "CollectionRegime",
    "MarketMeasurementSpecification",
    "MarketMeasurementSpecificationV1",
    "MarketMeasurementSubject",
    "MarketMeasurementSubjectV1",
    "MeasurementSpecification",
    "MeasurementSpecificationV1",
    "MeasurementSubjectKind",
    "PlatformContext",
    "PlatformDataMode",
    "ProfileRunAssignment",
    "PromptIdentity",
    "PromptIdentityV1",
    "QueryGeographyIdentity",
    "RunProtocol",
    "SettingState",
    "SourcePlatform",
    "compare_measurement_specifications",
    "canonical_profile_assignments",
    "market_purpose_binding",
    "measurement_specifications_compatible",
    "profile_assignment_plan_sha256",
]
