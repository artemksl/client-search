"""Deterministic, side-effect-free Market Evidence demand planning.

The planner expands only the frozen slot that names each exact geography
level.  It never crosses every treatment with every geography, never creates a
Snapshot identifier, and never invents an acquisition window.  Collection
reuse is deduplicated solely by the complete current measurement-specification
hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from clinic_geo.evidence.purpose import EvidencePurpose
from clinic_geo.integrity import canonical_json, canonical_sha256
from clinic_geo.query_geography.contracts import (
    QueryGeographyLevel,
    QueryGeographyVersion,
)
from clinic_geo.treatment_library.contracts import PatientSearchIntentVersion

from .contracts import (
    MARKET_PROFILE_ASSIGNMENT_PLAN_SCHEMA,
    CollectionRegime,
    MarketMeasurementSpecification,
    MarketMeasurementSubject,
    MeasurementSubjectKind,
    PlatformContext,
    PlatformDataMode,
    ProfileRunAssignment,
    PromptIdentity,
    QueryGeographyIdentity,
    RunProtocol,
    SourcePlatform,
    canonical_profile_assignments,
    profile_assignment_plan_sha256,
)
from .costing import (
    CollectionCostEstimate,
    CollectionCostModel,
    estimate_collection_cost,
)
from .eligibility import EligibilityDecision, EligibilityStatus
from .prompt_rendering import (
    MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION,
    render_market_area_slot,
)


MARKET_DEMAND_PLAN_SCHEMA = "market_evidence.demand_plan.v1"
MARKET_COVERAGE_CELL_SCHEMA = "market_evidence.coverage_cell.v1"
MARKET_SEMANTIC_PROMPT_SCHEMA = "market_evidence.semantic_prompt_demand.v1"
MARKET_PLATFORM_RENDERING_SCHEMA = "market_evidence.platform_rendering_demand.v1"
MARKET_OBSERVATION_DEMAND_SCHEMA = "market_evidence.observation_demand.v1"
MARKET_LIVE_ACTION_SCHEMA = "market_evidence.live_collector_action.v1"
PROFILE_ASSIGNMENT_PLAN_SCHEMA = MARKET_PROFILE_ASSIGNMENT_PLAN_SCHEMA

MARKET_PLATFORM_ORDER = (
    SourcePlatform.CHATGPT,
    SourcePlatform.GEMINI,
    SourcePlatform.GOOGLE_AI_OVERVIEW,
    SourcePlatform.COPILOT,
)
MARKET_RUN_ORDINALS = (1, 2, 3)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEVEL_ORDER = {
    QueryGeographyLevel.LOCAL: 0,
    QueryGeographyLevel.GREATER_AREA: 1,
    QueryGeographyLevel.CITY: 2,
}
_EXPECTED_SLOT_COUNTS = {
    ("treatment", QueryGeographyLevel.LOCAL): 7,
    ("treatment", QueryGeographyLevel.GREATER_AREA): 4,
    ("treatment", QueryGeographyLevel.CITY): 3,
    ("clinic_archetype", QueryGeographyLevel.LOCAL): 3,
    ("clinic_archetype", QueryGeographyLevel.GREATER_AREA): 2,
    ("clinic_archetype", QueryGeographyLevel.CITY): 2,
}


class DemandPlanningError(ValueError):
    """Demand cannot be planned without weakening an invariant."""


class DemandEvidenceStatus(str, Enum):
    REUSED = "reused"
    MISSING = "missing"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DemandPlanningError(f"{field_name} must be non-empty text")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise DemandPlanningError(f"{field_name} must be a lowercase SHA-256")
    return value


def _status(value: DemandEvidenceStatus | str) -> DemandEvidenceStatus:
    if isinstance(value, DemandEvidenceStatus):
        return value
    if isinstance(value, str):
        try:
            return DemandEvidenceStatus(value)
        except ValueError as exc:
            raise DemandPlanningError(
                "evidence status must be reused, missing, stale, or incompatible"
            ) from exc
    raise TypeError("evidence status must be a DemandEvidenceStatus")


def _status_from_eligibility(decision: EligibilityDecision) -> DemandEvidenceStatus:
    if not isinstance(decision, EligibilityDecision):
        raise TypeError("eligibility values must be validated EligibilityDecision objects")
    if decision.purpose is not EvidencePurpose.MARKET_DISCOVERY:
        raise DemandPlanningError("eligibility decision purpose must be market_discovery")
    if decision.status is EligibilityStatus.HIT:
        if not decision.is_hit or decision.selected_window_evaluation is None:
            raise DemandPlanningError("reused demand requires a selected validated hit")
        return DemandEvidenceStatus.REUSED
    if decision.status is EligibilityStatus.STALE:
        return DemandEvidenceStatus.STALE
    if decision.status is EligibilityStatus.INCOMPATIBLE:
        return DemandEvidenceStatus.INCOMPATIBLE
    return DemandEvidenceStatus.MISSING


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentDemand:
    """One approved patient intent requested by a campaign consumer."""

    intent: PatientSearchIntentVersion
    exact_rendered_treatment_term: str
    selected: bool = True
    decision_relevant: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.intent, PatientSearchIntentVersion):
            raise TypeError("intent must be a PatientSearchIntentVersion")
        term = _require_text(
            self.exact_rendered_treatment_term,
            "exact_rendered_treatment_term",
        )
        if hashlib.sha256(term.encode("utf-8")).hexdigest() != (
            self.intent.rendered_treatment_term_sha256
        ):
            raise DemandPlanningError(
                "exact_rendered_treatment_term does not match the intent hash"
            )
        if not isinstance(self.selected, bool):
            raise TypeError("selected must be a bool")
        if not isinstance(self.decision_relevant, bool):
            raise TypeError("decision_relevant must be a bool")

    @property
    def included(self) -> bool:
        return self.selected or self.decision_relevant


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchetypeDemand:
    """One treatment-independent clinic archetype, enabled separately."""

    archetype_id: str
    archetype_revision: str
    singular: str
    plural: str
    archetype_wording_content_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "archetype_id",
            "archetype_revision",
            "singular",
            "plural",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(
            self.archetype_wording_content_sha256,
            "archetype_wording_content_sha256",
        )


PlannedProfileAssignment = ProfileRunAssignment


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformPlanningContract:
    platform_context: PlatformContext
    collection_regime: CollectionRegime
    profile_assignments: tuple[PlannedProfileAssignment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.platform_context, PlatformContext):
            raise TypeError("platform_context must be a PlatformContext")
        if not isinstance(self.collection_regime, CollectionRegime):
            raise TypeError("collection_regime must be a CollectionRegime")
        ordered = canonical_profile_assignments(self.profile_assignments)
        object.__setattr__(self, "profile_assignments", ordered)
        if self.collection_regime.profile_ordering_sha256 != (
            profile_assignment_plan_sha256(ordered)
        ):
            raise DemandPlanningError(
                "profile assignments do not match the collection regime's "
                "profile_ordering_sha256"
            )

    def assignment_for_run(self, run_ordinal: int) -> PlannedProfileAssignment:
        return next(
            row for row in self.profile_assignments if row.run_ordinal == run_ordinal
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketDemandRequest:
    """All compatibility-sensitive inputs required to derive exact demand."""

    intents: tuple[IntentDemand, ...]
    query_geographies: tuple[QueryGeographyVersion, ...]
    prompt_catalogue: Mapping[str, Any]
    prompt_pack_version: str
    prompt_pack_content_sha256: str
    platforms: tuple[PlatformPlanningContract, ...]
    run_protocol: RunProtocol
    cost_model: CollectionCostModel
    archetype_enabled: bool = False
    archetype: ArchetypeDemand | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intents, tuple) or any(
            not isinstance(row, IntentDemand) for row in self.intents
        ):
            raise TypeError("intents must be a tuple of IntentDemand values")
        if not isinstance(self.query_geographies, tuple) or any(
            not isinstance(row, QueryGeographyVersion)
            for row in self.query_geographies
        ):
            raise TypeError(
                "query_geographies must be a tuple of QueryGeographyVersion values"
            )
        if not isinstance(self.prompt_catalogue, Mapping):
            raise TypeError("prompt_catalogue must be a mapping")
        try:
            catalogue_snapshot = json.loads(canonical_json(self.prompt_catalogue))
        except (TypeError, ValueError) as exc:
            raise DemandPlanningError(
                "prompt_catalogue must be canonical-JSON serializable"
            ) from exc
        object.__setattr__(
            self,
            "prompt_catalogue",
            _deep_freeze(catalogue_snapshot),
        )
        _require_text(self.prompt_pack_version, "prompt_pack_version")
        _require_sha256(
            self.prompt_pack_content_sha256,
            "prompt_pack_content_sha256",
        )
        if not isinstance(self.platforms, tuple) or any(
            not isinstance(row, PlatformPlanningContract) for row in self.platforms
        ):
            raise TypeError(
                "platforms must be a tuple of PlatformPlanningContract values"
            )
        if not isinstance(self.run_protocol, RunProtocol):
            raise TypeError("run_protocol must be a RunProtocol")
        if self.run_protocol.required_runs != 3:
            raise DemandPlanningError("Market demand requires exactly three runs")
        if not isinstance(self.cost_model, CollectionCostModel):
            raise TypeError("cost_model must be a CollectionCostModel")
        if not isinstance(self.archetype_enabled, bool):
            raise TypeError("archetype_enabled must be a bool")
        if self.archetype_enabled and not isinstance(self.archetype, ArchetypeDemand):
            raise DemandPlanningError(
                "archetype_enabled=true requires an exact archetype contract"
            )
        if not self.archetype_enabled and self.archetype is not None:
            raise DemandPlanningError(
                "archetype input requires archetype_enabled=true; it is never implicit"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedSpecification:
    measurement_spec_sha256: str
    specification: MarketMeasurementSpecification
    evidence_status: DemandEvidenceStatus
    eligibility_decision: EligibilityDecision | None
    coverage_cell_ids: tuple[str, ...]
    platform_rendering_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        if not isinstance(self.specification, MarketMeasurementSpecification):
            raise TypeError(
                "new demand specifications must use MarketMeasurementSpecification v2"
            )
        if self.measurement_spec_sha256 != self.specification.measurement_spec_sha256:
            raise DemandPlanningError("measurement specification hash mismatch")
        object.__setattr__(self, "evidence_status", _status(self.evidence_status))
        if self.eligibility_decision is None:
            if self.evidence_status is not DemandEvidenceStatus.MISSING:
                raise DemandPlanningError(
                    "non-missing evidence status requires a validated eligibility decision"
                )
        else:
            if not isinstance(self.eligibility_decision, EligibilityDecision):
                raise TypeError(
                    "eligibility_decision must be an EligibilityDecision or None"
                )
            if self.eligibility_decision.measurement_spec_sha256 != (
                self.measurement_spec_sha256
            ):
                raise DemandPlanningError(
                    "eligibility decision specification hash mismatch"
                )
            if _status_from_eligibility(self.eligibility_decision) is not (
                self.evidence_status
            ):
                raise DemandPlanningError(
                    "evidence status does not match the eligibility decision"
                )
        if not isinstance(self.coverage_cell_ids, tuple) or not isinstance(
            self.platform_rendering_ids, tuple
        ):
            raise TypeError("specification links must be tuples")
        coverage_ids = tuple(sorted(self.coverage_cell_ids))
        rendering_ids = tuple(sorted(self.platform_rendering_ids))
        if any(
            not isinstance(value, str) or not value.startswith("market-cell-")
            for value in coverage_ids
        ) or len(set(coverage_ids)) != len(coverage_ids):
            raise DemandPlanningError("invalid or duplicate specification coverage links")
        if any(
            not isinstance(value, str)
            or not value.startswith("market-rendering-demand-")
            for value in rendering_ids
        ) or len(set(rendering_ids)) != len(rendering_ids):
            raise DemandPlanningError("invalid or duplicate specification rendering links")
        object.__setattr__(self, "coverage_cell_ids", coverage_ids)
        object.__setattr__(self, "platform_rendering_ids", rendering_ids)
        if not coverage_ids or not rendering_ids:
            raise DemandPlanningError(
                "planned specifications require coverage-cell and rendering links"
            )

    def to_canonical_dict(self) -> dict:
        return {
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "evidence_status": self.evidence_status.value,
            "eligibility_decision": (
                self.eligibility_decision.to_plan_binding_dict()
                if self.eligibility_decision is not None
                else None
            ),
            "coverage_cell_ids": list(self.coverage_cell_ids),
            "platform_rendering_ids": list(self.platform_rendering_ids),
            "measurement_specification": self.specification.to_canonical_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticPromptDemand:
    semantic_prompt_id: str
    semantic_prompt_sha256: str
    coverage_cell_id: str
    subject_kind: MeasurementSubjectKind
    subject_id: str
    query_geography_version_row_id: str
    geography_level: QueryGeographyLevel
    basket: str
    semantic_slot: str
    semantic_intent: str
    prompt_pack_version: str
    prompt_pack_content_sha256: str
    prompt_catalogue_version: str
    prompt_catalogue_content_sha256: str
    subject: MarketMeasurementSubject
    query_geography: QueryGeographyVersion
    platform_rendering_ids: tuple[str, ...]
    schema_version: str = field(default=MARKET_SEMANTIC_PROMPT_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "semantic_prompt_id",
            "coverage_cell_id",
            "subject_id",
            "query_geography_version_row_id",
            "basket",
            "semantic_slot",
            "semantic_intent",
            "prompt_pack_version",
            "prompt_catalogue_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(self.semantic_prompt_sha256, "semantic_prompt_sha256")
        _require_sha256(
            self.prompt_pack_content_sha256,
            "prompt_pack_content_sha256",
        )
        _require_sha256(
            self.prompt_catalogue_content_sha256,
            "prompt_catalogue_content_sha256",
        )
        if not isinstance(self.subject_kind, MeasurementSubjectKind):
            raise TypeError("subject_kind must be a MeasurementSubjectKind")
        if not isinstance(self.geography_level, QueryGeographyLevel):
            raise TypeError("geography_level must be a QueryGeographyLevel")
        if not isinstance(self.subject, MarketMeasurementSubject):
            raise TypeError("subject must be a current MarketMeasurementSubject")
        if not isinstance(self.query_geography, QueryGeographyVersion):
            raise TypeError("query_geography must be a QueryGeographyVersion")
        expected_subject_id = (
            self.subject.patient_search_intent_id
            if self.subject.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY
            else self.subject.archetype_id
        )
        if self.subject_kind is not self.subject.kind or self.subject_id != expected_subject_id:
            raise DemandPlanningError("semantic prompt subject fields disagree")
        if (
            self.query_geography_version_row_id != self.query_geography.version_row_id
            or self.geography_level is not self.query_geography.level
        ):
            raise DemandPlanningError("semantic prompt geography fields disagree")
        if not self.coverage_cell_id.startswith("market-cell-"):
            raise DemandPlanningError("semantic prompt has an invalid coverage cell ID")
        if not isinstance(self.platform_rendering_ids, tuple):
            raise TypeError("platform_rendering_ids must be a tuple")
        rendering_ids = tuple(sorted(self.platform_rendering_ids))
        if (
            len(rendering_ids) != 4
            or len(set(rendering_ids)) != 4
            or any(
                not isinstance(value, str)
                or not value.startswith("market-rendering-demand-")
                for value in rendering_ids
            )
        ):
            raise DemandPlanningError(
                "each semantic prompt requires four distinct platform renderings"
            )
        object.__setattr__(self, "platform_rendering_ids", rendering_ids)
        hash_payload = {
            "contract": self.schema_version,
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "prompt_pack_version": self.prompt_pack_version,
            "prompt_pack_content_sha256": self.prompt_pack_content_sha256,
            "prompt_catalogue_version": self.prompt_catalogue_version,
            "prompt_catalogue_content_sha256": self.prompt_catalogue_content_sha256,
            "subject": self.subject.to_canonical_dict(),
            "query_geography": self.query_geography.to_canonical_dict(),
            "basket": self.basket,
            "semantic_slot": self.semantic_slot,
            "semantic_intent": self.semantic_intent,
        }
        expected_hash = canonical_sha256(hash_payload)
        if self.semantic_prompt_sha256 != expected_hash or self.semantic_prompt_id != (
            f"market-semantic-demand-{expected_hash[:24]}"
        ):
            raise DemandPlanningError("semantic prompt hash or ID mismatch")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "semantic_prompt_id": self.semantic_prompt_id,
            "semantic_prompt_sha256": self.semantic_prompt_sha256,
            "coverage_cell_id": self.coverage_cell_id,
            "subject_kind": self.subject_kind.value,
            "subject_id": self.subject_id,
            "query_geography_version_row_id": self.query_geography_version_row_id,
            "geography_level": self.geography_level.value,
            "basket": self.basket,
            "semantic_slot": self.semantic_slot,
            "semantic_intent": self.semantic_intent,
            "prompt_pack_version": self.prompt_pack_version,
            "prompt_pack_content_sha256": self.prompt_pack_content_sha256,
            "prompt_catalogue_version": self.prompt_catalogue_version,
            "prompt_catalogue_content_sha256": self.prompt_catalogue_content_sha256,
            "subject": self.subject.to_canonical_dict(),
            "query_geography": self.query_geography.to_canonical_dict(),
            "platform_rendering_ids": list(self.platform_rendering_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformRenderingDemand:
    platform_rendering_id: str
    platform_rendering_sha256: str
    semantic_prompt_id: str
    semantic_prompt_sha256: str
    coverage_cell_id: str
    measurement_spec_sha256: str
    source_platform: SourcePlatform
    exact_rendered_prompt: str
    exact_rendered_prompt_sha256: str
    exact_template_sha256: str
    exact_rendering_bindings_sha256: str
    schema_version: str = field(default=MARKET_PLATFORM_RENDERING_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "platform_rendering_id",
            "semantic_prompt_id",
            "coverage_cell_id",
            "exact_rendered_prompt",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "platform_rendering_sha256",
            "semantic_prompt_sha256",
            "measurement_spec_sha256",
            "exact_rendered_prompt_sha256",
            "exact_template_sha256",
            "exact_rendering_bindings_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if not isinstance(self.source_platform, SourcePlatform):
            raise TypeError("source_platform must be a SourcePlatform")
        if hashlib.sha256(self.exact_rendered_prompt.encode("utf-8")).hexdigest() != (
            self.exact_rendered_prompt_sha256
        ):
            raise DemandPlanningError("exact rendered prompt hash mismatch")
        if not self.semantic_prompt_id.startswith("market-semantic-demand-"):
            raise DemandPlanningError("invalid semantic prompt link")
        if self.semantic_prompt_id != (
            f"market-semantic-demand-{self.semantic_prompt_sha256[:24]}"
        ):
            raise DemandPlanningError("semantic prompt link does not match its hash")
        if not self.coverage_cell_id.startswith("market-cell-"):
            raise DemandPlanningError("invalid coverage cell link")
        payload = {
            "contract": self.schema_version,
            "semantic_prompt_sha256": self.semantic_prompt_sha256,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "source_platform": self.source_platform.value,
            "exact_rendered_prompt": self.exact_rendered_prompt,
            "exact_rendered_prompt_sha256": self.exact_rendered_prompt_sha256,
            "exact_template_sha256": self.exact_template_sha256,
            "exact_rendering_bindings_sha256": self.exact_rendering_bindings_sha256,
        }
        expected_hash = canonical_sha256(payload)
        if self.platform_rendering_sha256 != expected_hash or (
            self.platform_rendering_id
            != f"market-rendering-demand-{expected_hash[:24]}"
        ):
            raise DemandPlanningError("platform rendering hash or ID mismatch")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "platform_rendering_id": self.platform_rendering_id,
            "platform_rendering_sha256": self.platform_rendering_sha256,
            "semantic_prompt_id": self.semantic_prompt_id,
            "semantic_prompt_sha256": self.semantic_prompt_sha256,
            "coverage_cell_id": self.coverage_cell_id,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "source_platform": self.source_platform.value,
            "exact_rendered_prompt": self.exact_rendered_prompt,
            "exact_rendered_prompt_sha256": self.exact_rendered_prompt_sha256,
            "exact_template_sha256": self.exact_template_sha256,
            "exact_rendering_bindings_sha256": self.exact_rendering_bindings_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationDemand:
    """Window-neutral demand key; not a persisted logical observation ID."""

    observation_demand_key: str
    observation_demand_sha256: str
    measurement_spec_sha256: str
    source_platform: SourcePlatform
    run_ordinal: int
    planned_profile_id: str
    planned_profile_revision: str
    evidence_status: DemandEvidenceStatus
    data_mode: PlatformDataMode
    schema_version: str = field(default=MARKET_OBSERVATION_DEMAND_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "observation_demand_key",
            "planned_profile_id",
            "planned_profile_revision",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(self.observation_demand_sha256, "observation_demand_sha256")
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        if not isinstance(self.source_platform, SourcePlatform):
            raise TypeError("source_platform must be a SourcePlatform")
        if not isinstance(self.data_mode, PlatformDataMode):
            raise TypeError("data_mode must be a PlatformDataMode")
        object.__setattr__(self, "evidence_status", _status(self.evidence_status))
        if (
            isinstance(self.run_ordinal, bool)
            or not isinstance(self.run_ordinal, int)
            or self.run_ordinal not in MARKET_RUN_ORDINALS
        ):
            raise DemandPlanningError("run_ordinal must be exactly 1, 2, or 3")
        payload = {
            "contract": self.schema_version,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "run_ordinal": self.run_ordinal,
            "planned_profile_id": self.planned_profile_id,
            "planned_profile_revision": self.planned_profile_revision,
        }
        expected_hash = canonical_sha256(payload)
        if self.observation_demand_sha256 != expected_hash or (
            self.observation_demand_key
            != f"market-observation-demand-{expected_hash[:24]}"
        ):
            raise DemandPlanningError("observation demand hash or key mismatch")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "observation_demand_key": self.observation_demand_key,
            "observation_demand_sha256": self.observation_demand_sha256,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "source_platform": self.source_platform.value,
            "run_ordinal": self.run_ordinal,
            "planned_profile_id": self.planned_profile_id,
            "planned_profile_revision": self.planned_profile_revision,
            "evidence_status": self.evidence_status.value,
            "data_mode": self.data_mode.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveCollectorAction:
    live_collector_action_id: str
    live_collector_action_sha256: str
    observation_demand_key: str
    observation_demand_sha256: str
    measurement_spec_sha256: str
    source_platform: SourcePlatform
    run_ordinal: int
    planned_profile_id: str
    planned_profile_revision: str
    schema_version: str = field(default=MARKET_LIVE_ACTION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "live_collector_action_id",
            "observation_demand_key",
            "planned_profile_id",
            "planned_profile_revision",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "live_collector_action_sha256",
            "observation_demand_sha256",
            "measurement_spec_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if not isinstance(self.source_platform, SourcePlatform):
            raise TypeError("source_platform must be a SourcePlatform")
        if (
            isinstance(self.run_ordinal, bool)
            or not isinstance(self.run_ordinal, int)
            or self.run_ordinal not in MARKET_RUN_ORDINALS
        ):
            raise DemandPlanningError("run_ordinal must be exactly 1, 2, or 3")
        if not self.observation_demand_key.startswith("market-observation-demand-"):
            raise DemandPlanningError("invalid observation demand link")
        if self.observation_demand_key != (
            f"market-observation-demand-{self.observation_demand_sha256[:24]}"
        ):
            raise DemandPlanningError("observation demand link does not match its hash")
        payload = {
            "contract": self.schema_version,
            "observation_demand_sha256": self.observation_demand_sha256,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "source_platform": self.source_platform.value,
            "run_ordinal": self.run_ordinal,
            "planned_profile_id": self.planned_profile_id,
            "planned_profile_revision": self.planned_profile_revision,
        }
        expected_hash = canonical_sha256(payload)
        if self.live_collector_action_sha256 != expected_hash or (
            self.live_collector_action_id != f"market-live-action-{expected_hash[:24]}"
        ):
            raise DemandPlanningError("live collector action hash or ID mismatch")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "live_collector_action_id": self.live_collector_action_id,
            "live_collector_action_sha256": self.live_collector_action_sha256,
            "observation_demand_key": self.observation_demand_key,
            "observation_demand_sha256": self.observation_demand_sha256,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "source_platform": self.source_platform.value,
            "run_ordinal": self.run_ordinal,
            "planned_profile_id": self.planned_profile_id,
            "planned_profile_revision": self.planned_profile_revision,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageCell:
    coverage_cell_id: str
    coverage_cell_sha256: str
    subject_kind: MeasurementSubjectKind
    subject_id: str
    query_geography_id: str
    query_geography_version_row_id: str
    geography_level: QueryGeographyLevel
    subject: MarketMeasurementSubject
    query_geography: QueryGeographyVersion
    demand_reasons: tuple[str, ...]
    evidence_status: DemandEvidenceStatus
    semantic_prompt_ids: tuple[str, ...]
    platform_rendering_ids: tuple[str, ...]
    measurement_spec_sha256s: tuple[str, ...]
    schema_version: str = field(default=MARKET_COVERAGE_CELL_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "coverage_cell_id",
            "subject_id",
            "query_geography_id",
            "query_geography_version_row_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(self.coverage_cell_sha256, "coverage_cell_sha256")
        if not isinstance(self.subject_kind, MeasurementSubjectKind):
            raise TypeError("subject_kind must be a MeasurementSubjectKind")
        if not isinstance(self.geography_level, QueryGeographyLevel):
            raise TypeError("geography_level must be a QueryGeographyLevel")
        if not isinstance(self.subject, MarketMeasurementSubject):
            raise TypeError("subject must be a current MarketMeasurementSubject")
        if not isinstance(self.query_geography, QueryGeographyVersion):
            raise TypeError("query_geography must be a QueryGeographyVersion")
        expected_subject_id = (
            self.subject.patient_search_intent_id
            if self.subject.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY
            else self.subject.archetype_id
        )
        if self.subject_kind is not self.subject.kind or self.subject_id != expected_subject_id:
            raise DemandPlanningError("coverage cell subject fields disagree")
        if (
            self.query_geography_id != self.query_geography.query_geography_id
            or self.query_geography_version_row_id
            != self.query_geography.version_row_id
            or self.geography_level is not self.query_geography.level
        ):
            raise DemandPlanningError("coverage cell geography fields disagree")
        object.__setattr__(self, "evidence_status", _status(self.evidence_status))
        if not isinstance(self.demand_reasons, tuple):
            raise TypeError("demand_reasons must be a tuple")
        reasons = tuple(sorted(self.demand_reasons))
        if (
            not reasons
            or len(set(reasons)) != len(reasons)
            or any(
                reason not in {"selected", "decision_relevant", "explicit_archetype"}
                for reason in reasons
            )
        ):
            raise DemandPlanningError("coverage cell has invalid demand reasons")
        object.__setattr__(self, "demand_reasons", reasons)
        link_fields = (
            ("semantic_prompt_ids", "market-semantic-demand-"),
            ("platform_rendering_ids", "market-rendering-demand-"),
        )
        for field_name, prefix in link_fields:
            raw_values = getattr(self, field_name)
            if not isinstance(raw_values, tuple):
                raise TypeError(f"{field_name} must be a tuple")
            values = tuple(sorted(raw_values))
            if (
                not values
                or len(set(values)) != len(values)
                or any(
                    not isinstance(value, str) or not value.startswith(prefix)
                    for value in values
                )
            ):
                raise DemandPlanningError(f"{field_name} contains invalid links")
            object.__setattr__(self, field_name, values)
        if not isinstance(self.measurement_spec_sha256s, tuple):
            raise TypeError("measurement_spec_sha256s must be a tuple")
        spec_hashes = tuple(sorted(self.measurement_spec_sha256s))
        if (
            not spec_hashes
            or len(set(spec_hashes)) != len(spec_hashes)
            or any(
                not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
                for value in spec_hashes
            )
        ):
            raise DemandPlanningError("measurement_spec_sha256s contains invalid links")
        object.__setattr__(self, "measurement_spec_sha256s", spec_hashes)
        expected_prompt_count = _EXPECTED_SLOT_COUNTS[
            (
                "treatment"
                if self.subject_kind is MeasurementSubjectKind.TREATMENT_VISIBILITY
                else "clinic_archetype",
                self.geography_level,
            )
        ]
        if len(self.semantic_prompt_ids) != expected_prompt_count:
            raise DemandPlanningError("coverage cell semantic-prompt count is invalid")
        if len(self.platform_rendering_ids) != expected_prompt_count * 4:
            raise DemandPlanningError("coverage cell platform-rendering count is invalid")
        if len(self.measurement_spec_sha256s) != expected_prompt_count * 4:
            raise DemandPlanningError("coverage cell specification count is invalid")
        hash_payload = {
            "contract": self.schema_version,
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "subject": self.subject.to_canonical_dict(),
            "query_geography": self.query_geography.to_canonical_dict(),
        }
        expected_hash = canonical_sha256(hash_payload)
        if self.coverage_cell_sha256 != expected_hash or self.coverage_cell_id != (
            f"market-cell-{expected_hash[:24]}"
        ):
            raise DemandPlanningError("coverage cell hash or ID mismatch")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "coverage_cell_id": self.coverage_cell_id,
            "coverage_cell_sha256": self.coverage_cell_sha256,
            "subject_kind": self.subject_kind.value,
            "subject_id": self.subject_id,
            "query_geography_id": self.query_geography_id,
            "query_geography_version_row_id": self.query_geography_version_row_id,
            "geography_level": self.geography_level.value,
            "subject": self.subject.to_canonical_dict(),
            "query_geography": self.query_geography.to_canonical_dict(),
            "demand_reasons": list(self.demand_reasons),
            "evidence_status": self.evidence_status.value,
            "semantic_prompt_ids": list(self.semantic_prompt_ids),
            "platform_rendering_ids": list(self.platform_rendering_ids),
            "measurement_spec_sha256s": list(self.measurement_spec_sha256s),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DemandPlanCounts:
    coverage_cell_count: int
    reused_cell_count: int
    missing_cell_count: int
    stale_cell_count: int
    incompatible_cell_count: int
    collection_cell_count: int
    measurement_specification_count: int
    semantic_prompt_count: int
    platform_rendering_count: int
    logical_observation_count: int
    live_collector_action_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "coverage_cell_count",
            "reused_cell_count",
            "missing_cell_count",
            "stale_cell_count",
            "incompatible_cell_count",
            "collection_cell_count",
            "measurement_specification_count",
            "semantic_prompt_count",
            "platform_rendering_count",
            "logical_observation_count",
            "live_collector_action_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DemandPlanningError(f"{field_name} must be a non-negative integer")
        status_total = (
            self.reused_cell_count
            + self.missing_cell_count
            + self.stale_cell_count
            + self.incompatible_cell_count
        )
        if status_total != self.coverage_cell_count:
            raise DemandPlanningError("coverage status counts do not match cell count")
        if self.collection_cell_count != (
            self.coverage_cell_count - self.reused_cell_count
        ):
            raise DemandPlanningError("collection cell count is inconsistent")
        if self.platform_rendering_count != self.measurement_specification_count:
            raise DemandPlanningError("each rendering must have one exact specification")
        if self.logical_observation_count != self.measurement_specification_count * 3:
            raise DemandPlanningError("each specification requires exactly three runs")
        if self.platform_rendering_count != self.semantic_prompt_count * 4:
            raise DemandPlanningError("each semantic prompt requires four renderings")

    def to_canonical_dict(self) -> dict:
        return {
            "coverage_cell_count": self.coverage_cell_count,
            "reused_cell_count": self.reused_cell_count,
            "missing_cell_count": self.missing_cell_count,
            "stale_cell_count": self.stale_cell_count,
            "incompatible_cell_count": self.incompatible_cell_count,
            "collection_cell_count": self.collection_cell_count,
            "measurement_specification_count": self.measurement_specification_count,
            "semantic_prompt_count": self.semantic_prompt_count,
            "platform_rendering_count": self.platform_rendering_count,
            "logical_observation_count": self.logical_observation_count,
            "live_collector_action_count": self.live_collector_action_count,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketDemandPlan:
    prompt_pack_version: str
    prompt_pack_content_sha256: str
    prompt_catalogue_version: str
    prompt_catalogue_content_sha256: str
    specifications: tuple[PlannedSpecification, ...]
    coverage_cells: tuple[CoverageCell, ...]
    semantic_prompts: tuple[SemanticPromptDemand, ...]
    platform_renderings: tuple[PlatformRenderingDemand, ...]
    observation_demands: tuple[ObservationDemand, ...]
    live_collector_actions: tuple[LiveCollectorAction, ...]
    counts: DemandPlanCounts
    cost_model: CollectionCostModel
    cost_estimate: CollectionCostEstimate
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    schema_version: str = field(default=MARKET_DEMAND_PLAN_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_text(self.prompt_pack_version, "prompt_pack_version")
        _require_sha256(
            self.prompt_pack_content_sha256,
            "prompt_pack_content_sha256",
        )
        _require_text(self.prompt_catalogue_version, "prompt_catalogue_version")
        _require_sha256(
            self.prompt_catalogue_content_sha256,
            "prompt_catalogue_content_sha256",
        )
        if self.purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise DemandPlanningError("Market demand plans require market_discovery")
        tuple_contracts = (
            ("specifications", PlannedSpecification, lambda row: row.measurement_spec_sha256),
            ("coverage_cells", CoverageCell, lambda row: row.coverage_cell_id),
            ("semantic_prompts", SemanticPromptDemand, lambda row: row.semantic_prompt_id),
            (
                "platform_renderings",
                PlatformRenderingDemand,
                lambda row: row.platform_rendering_id,
            ),
            (
                "observation_demands",
                ObservationDemand,
                lambda row: (
                    row.measurement_spec_sha256,
                    row.run_ordinal,
                    row.planned_profile_id,
                    row.planned_profile_revision,
                ),
            ),
            (
                "live_collector_actions",
                LiveCollectorAction,
                lambda row: (
                    row.measurement_spec_sha256,
                    row.run_ordinal,
                    row.planned_profile_id,
                    row.planned_profile_revision,
                ),
            ),
        )
        for field_name, row_type, sort_key in tuple_contracts:
            rows = getattr(self, field_name)
            if not isinstance(rows, tuple) or any(
                not isinstance(row, row_type) for row in rows
            ):
                raise TypeError(f"{field_name} must be a tuple of {row_type.__name__}")
            ordered = tuple(sorted(rows, key=sort_key))
            if len({sort_key(row) for row in ordered}) != len(ordered):
                raise DemandPlanningError(f"{field_name} contains duplicate identities")
            object.__setattr__(self, field_name, ordered)
        if not self.specifications or not self.coverage_cells:
            raise DemandPlanningError("a Market demand plan cannot be empty")
        if not isinstance(self.counts, DemandPlanCounts):
            raise TypeError("counts must be DemandPlanCounts")
        if not isinstance(self.cost_model, CollectionCostModel):
            raise TypeError("cost_model must be a CollectionCostModel")
        if not isinstance(self.cost_estimate, CollectionCostEstimate):
            raise TypeError("cost_estimate must be a CollectionCostEstimate")

        specs = {row.measurement_spec_sha256: row for row in self.specifications}
        cells = {row.coverage_cell_id: row for row in self.coverage_cells}
        semantics = {row.semantic_prompt_id: row for row in self.semantic_prompts}
        renderings = {
            row.platform_rendering_id: row for row in self.platform_renderings
        }
        observations = {
            row.observation_demand_key: row for row in self.observation_demands
        }
        actions = {
            row.live_collector_action_id: row for row in self.live_collector_actions
        }
        if len(observations) != len(self.observation_demands):
            raise DemandPlanningError("observation demand keys must be unique")
        if len(actions) != len(self.live_collector_actions):
            raise DemandPlanningError("live collector action IDs must be unique")

        cells_by_subject: dict[str, list[CoverageCell]] = {}
        geographies_by_level: dict[QueryGeographyLevel, str] = {}
        for cell in cells.values():
            subject_hash = canonical_sha256(cell.subject.to_canonical_dict())
            cells_by_subject.setdefault(subject_hash, []).append(cell)
            geography_hash = canonical_sha256(cell.query_geography.to_canonical_dict())
            existing_geography = geographies_by_level.setdefault(
                cell.geography_level, geography_hash
            )
            if existing_geography != geography_hash:
                raise DemandPlanningError(
                    "all subjects must use the same approved geography triple"
                )
        if set(geographies_by_level) != set(_LEVEL_ORDER):
            raise DemandPlanningError(
                "plan requires the exact Area, Bigger area, and City geography triple"
            )
        for subject_cells in cells_by_subject.values():
            if len(subject_cells) != 3 or {
                cell.geography_level for cell in subject_cells
            } != set(_LEVEL_ORDER):
                raise DemandPlanningError(
                    "each subject requires exactly one Area, Bigger area, and City cell"
                )

        for spec_hash, row in specs.items():
            if (
                row.specification.prompt.prompt_pack_version
                != self.prompt_pack_version
                or row.specification.prompt.prompt_pack_content_sha256
                != self.prompt_pack_content_sha256
            ):
                raise DemandPlanningError("specification prompt-pack version mismatch")
            if (
                row.specification.prompt.prompt_catalogue_version
                != self.prompt_catalogue_version
                or row.specification.prompt.prompt_catalogue_content_sha256
                != self.prompt_catalogue_content_sha256
            ):
                raise DemandPlanningError("specification prompt-catalogue mismatch")
            expected_cells = tuple(
                sorted(
                    cell_id
                    for cell_id, cell in cells.items()
                    if spec_hash in cell.measurement_spec_sha256s
                )
            )
            expected_renderings = tuple(
                sorted(
                    rendering_id
                    for rendering_id, rendering in renderings.items()
                    if rendering.measurement_spec_sha256 == spec_hash
                )
            )
            if row.coverage_cell_ids != expected_cells or (
                row.platform_rendering_ids != expected_renderings
            ):
                raise DemandPlanningError("specification reverse links are incomplete")
            if len(expected_renderings) != 1:
                raise DemandPlanningError("each specification requires exactly one rendering")

        eligibility_contexts = {
            (
                row.eligibility_decision.evaluated_at,
                row.eligibility_decision.freshness_policy.policy_version,
                row.eligibility_decision.freshness_policy.purpose,
                row.eligibility_decision.freshness_policy.maximum_age,
            )
            for row in specs.values()
            if row.eligibility_decision is not None
        }
        if len(eligibility_contexts) > 1:
            raise DemandPlanningError(
                "all eligibility decisions must share one as-of time and freshness policy"
            )

        for semantic_id, row in semantics.items():
            cell = cells.get(row.coverage_cell_id)
            if cell is None:
                raise DemandPlanningError("semantic prompt references an unknown cell")
            if (
                row.subject.to_canonical_dict() != cell.subject.to_canonical_dict()
                or row.query_geography.to_canonical_dict()
                != cell.query_geography.to_canonical_dict()
            ):
                raise DemandPlanningError(
                    "semantic prompt subject/geography disagrees with its cell"
                )
            if (
                row.prompt_pack_version != self.prompt_pack_version
                or row.prompt_pack_content_sha256
                != self.prompt_pack_content_sha256
                or row.prompt_catalogue_version != self.prompt_catalogue_version
                or row.prompt_catalogue_content_sha256
                != self.prompt_catalogue_content_sha256
            ):
                raise DemandPlanningError("semantic prompt catalogue binding mismatch")
            expected_rendering_ids = tuple(
                sorted(
                    rendering_id
                    for rendering_id, rendering in renderings.items()
                    if rendering.semantic_prompt_id == semantic_id
                )
            )
            if row.platform_rendering_ids != expected_rendering_ids:
                raise DemandPlanningError("semantic prompt rendering links are incomplete")
            if {
                renderings[rendering_id].source_platform
                for rendering_id in expected_rendering_ids
            } != set(MARKET_PLATFORM_ORDER):
                raise DemandPlanningError("semantic prompt lacks the four platforms")

        for rendering in renderings.values():
            semantic = semantics.get(rendering.semantic_prompt_id)
            cell = cells.get(rendering.coverage_cell_id)
            spec = specs.get(rendering.measurement_spec_sha256)
            if semantic is None or cell is None or spec is None:
                raise DemandPlanningError("platform rendering has a broken link")
            if (
                rendering.semantic_prompt_sha256 != semantic.semantic_prompt_sha256
                or semantic.coverage_cell_id != cell.coverage_cell_id
                or rendering.platform_rendering_id not in cell.platform_rendering_ids
                or rendering.semantic_prompt_id not in cell.semantic_prompt_ids
            ):
                raise DemandPlanningError("platform rendering cross-links disagree")
            specification = spec.specification
            if (
                rendering.source_platform
                is not specification.platform_context.source_platform
                or specification.subject.to_canonical_dict()
                != semantic.subject.to_canonical_dict()
                or specification.query_geography.geography.to_canonical_dict()
                != semantic.query_geography.to_canonical_dict()
                or specification.prompt.basket != semantic.basket
                or specification.prompt.semantic_slot != semantic.semantic_slot
                or specification.prompt.semantic_intent != semantic.semantic_intent
                or rendering.exact_rendered_prompt_sha256
                != specification.prompt.exact_rendered_prompt_sha256
                or rendering.exact_template_sha256
                != specification.prompt.exact_template_sha256
                or rendering.exact_rendering_bindings_sha256
                != specification.prompt.exact_rendering_bindings_sha256
            ):
                raise DemandPlanningError("rendering disagrees with its exact specification")

        for cell in cells.values():
            expected_semantics = tuple(
                sorted(
                    semantic_id
                    for semantic_id, semantic in semantics.items()
                    if semantic.coverage_cell_id == cell.coverage_cell_id
                )
            )
            expected_renderings = tuple(
                sorted(
                    rendering_id
                    for rendering_id, rendering in renderings.items()
                    if rendering.coverage_cell_id == cell.coverage_cell_id
                )
            )
            expected_specs = tuple(
                sorted(
                    {
                        renderings[rendering_id].measurement_spec_sha256
                        for rendering_id in expected_renderings
                    }
                )
            )
            if (
                cell.semantic_prompt_ids != expected_semantics
                or cell.platform_rendering_ids != expected_renderings
                or cell.measurement_spec_sha256s != expected_specs
            ):
                raise DemandPlanningError("coverage cell links are incomplete")
            expected_status = _coverage_status(
                tuple(specs[spec_hash].evidence_status for spec_hash in expected_specs)
            )
            if cell.evidence_status is not expected_status:
                raise DemandPlanningError("coverage cell status disagrees with specifications")

        observations_by_spec: dict[str, list[ObservationDemand]] = {}
        for observation in observations.values():
            spec = specs.get(observation.measurement_spec_sha256)
            if spec is None:
                raise DemandPlanningError("observation demand references an unknown spec")
            specification = spec.specification
            if (
                observation.source_platform
                is not specification.platform_context.source_platform
                or observation.data_mode is not specification.platform_context.data_mode
                or observation.evidence_status is not spec.evidence_status
            ):
                raise DemandPlanningError("observation demand disagrees with its spec")
            observations_by_spec.setdefault(
                observation.measurement_spec_sha256, []
            ).append(observation)
        for spec_hash, spec in specs.items():
            rows = sorted(
                observations_by_spec.get(spec_hash, []),
                key=lambda item: item.run_ordinal,
            )
            if tuple(row.run_ordinal for row in rows) != MARKET_RUN_ORDINALS:
                raise DemandPlanningError("each specification requires runs 1, 2, and 3")
            assignments = tuple(
                ProfileRunAssignment(
                    run_ordinal=row.run_ordinal,
                    planned_profile_id=row.planned_profile_id,
                    planned_profile_revision=row.planned_profile_revision,
                )
                for row in rows
            )
            if profile_assignment_plan_sha256(assignments) != (
                spec.specification.collection_regime.profile_ordering_sha256
            ):
                raise DemandPlanningError(
                    "observation profiles disagree with the specification regime"
                )

        expected_action_observations = {
            row.observation_demand_key: row
            for row in observations.values()
            if row.evidence_status is not DemandEvidenceStatus.REUSED
            and row.data_mode is PlatformDataMode.LIVE
        }
        if {row.observation_demand_key for row in actions.values()} != set(
            expected_action_observations
        ):
            raise DemandPlanningError("live collector actions are incomplete or excessive")
        for action in actions.values():
            observation = expected_action_observations[action.observation_demand_key]
            if (
                action.observation_demand_sha256
                != observation.observation_demand_sha256
                or action.measurement_spec_sha256
                != observation.measurement_spec_sha256
                or action.source_platform is not observation.source_platform
                or action.run_ordinal != observation.run_ordinal
                or action.planned_profile_id != observation.planned_profile_id
                or action.planned_profile_revision
                != observation.planned_profile_revision
            ):
                raise DemandPlanningError("live action disagrees with observation demand")

        status_counts = {
            status: sum(cell.evidence_status is status for cell in cells.values())
            for status in DemandEvidenceStatus
        }
        expected_counts = DemandPlanCounts(
            coverage_cell_count=len(cells),
            reused_cell_count=status_counts[DemandEvidenceStatus.REUSED],
            missing_cell_count=status_counts[DemandEvidenceStatus.MISSING],
            stale_cell_count=status_counts[DemandEvidenceStatus.STALE],
            incompatible_cell_count=status_counts[DemandEvidenceStatus.INCOMPATIBLE],
            collection_cell_count=sum(
                cell.evidence_status is not DemandEvidenceStatus.REUSED
                for cell in cells.values()
            ),
            measurement_specification_count=len(specs),
            semantic_prompt_count=len(semantics),
            platform_rendering_count=len(renderings),
            logical_observation_count=len(observations),
            live_collector_action_count=len(actions),
        )
        if self.counts.to_canonical_dict() != expected_counts.to_canonical_dict():
            raise DemandPlanningError("plan counts do not match exact plan rows")

        action_counts = {
            platform: sum(
                action.source_platform is platform for action in actions.values()
            )
            for platform in MARKET_PLATFORM_ORDER
        }
        expected_estimate = estimate_collection_cost(action_counts, self.cost_model)
        if self.cost_estimate.to_canonical_dict() != expected_estimate.to_canonical_dict():
            raise DemandPlanningError("cost estimate does not match exact live actions")

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())

    @property
    def exact_plan_hash(self) -> str:
        return self.plan_sha256

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose.value,
            "prompt_pack_version": self.prompt_pack_version,
            "prompt_pack_content_sha256": self.prompt_pack_content_sha256,
            "prompt_catalogue_version": self.prompt_catalogue_version,
            "prompt_catalogue_content_sha256": self.prompt_catalogue_content_sha256,
            "specifications": [row.to_canonical_dict() for row in self.specifications],
            "coverage_cells": [row.to_canonical_dict() for row in self.coverage_cells],
            "semantic_prompts": [row.to_canonical_dict() for row in self.semantic_prompts],
            "platform_renderings": [
                row.to_canonical_dict() for row in self.platform_renderings
            ],
            "observation_demands": [
                row.to_canonical_dict() for row in self.observation_demands
            ],
            "live_collector_actions": [
                row.to_canonical_dict() for row in self.live_collector_actions
            ],
            "counts": self.counts.to_canonical_dict(),
            "cost_model": self.cost_model.to_canonical_dict(),
            "cost_estimate": self.cost_estimate.to_canonical_dict(),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_canonical_dict())


def _validated_catalogue(catalogue: Mapping[str, Any]) -> tuple[str, str, tuple[dict, ...]]:
    required_content_fields = ("platforms", "rendering_policy", "baskets", "slots")
    try:
        content = {field_name: catalogue[field_name] for field_name in required_content_fields}
    except KeyError as exc:
        raise DemandPlanningError("prompt catalogue lacks frozen content fields") from exc
    declared_hash = _require_sha256(
        catalogue.get("content_sha256"),
        "prompt_catalogue.content_sha256",
    )
    if canonical_sha256(content) != declared_hash:
        raise DemandPlanningError("prompt catalogue content hash mismatch")
    catalogue_version = _require_text(
        catalogue.get("catalog_version"),
        "prompt_catalogue.catalog_version",
    )
    raw_platforms = tuple(catalogue.get("platforms") or ())
    if set(raw_platforms) != {platform.value for platform in MARKET_PLATFORM_ORDER} or len(
        raw_platforms
    ) != len(MARKET_PLATFORM_ORDER):
        raise DemandPlanningError("prompt catalogue requires exactly four platform identities")
    raw_slots = catalogue.get("slots")
    if not isinstance(raw_slots, (list, tuple)):
        raise DemandPlanningError("prompt catalogue slots must be a sequence")
    slots = tuple(dict(slot) for slot in raw_slots if isinstance(slot, Mapping))
    if len(slots) != len(raw_slots):
        raise DemandPlanningError("every prompt catalogue slot must be an object")
    slot_ids = tuple(str(slot.get("id") or "") for slot in slots)
    if any(not value for value in slot_ids) or len(set(slot_ids)) != len(slot_ids):
        raise DemandPlanningError("prompt catalogue slot IDs must be non-empty and unique")
    actual_counts: dict[tuple[str, QueryGeographyLevel], int] = {}
    for slot in slots:
        subject_kind = str(slot.get("subject_kind") or "")
        try:
            level = QueryGeographyLevel(str(slot.get("geography_level") or ""))
        except ValueError as exc:
            raise DemandPlanningError("prompt slot has an unsupported geography level") from exc
        key = (subject_kind, level)
        actual_counts[key] = actual_counts.get(key, 0) + 1
        renderings = slot.get("renderings")
        if not isinstance(renderings, Mapping) or set(renderings) != {
            platform.value for platform in MARKET_PLATFORM_ORDER
        }:
            raise DemandPlanningError(
                f"prompt slot {slot['id']} requires all four exact renderings"
            )
    if actual_counts != _EXPECTED_SLOT_COUNTS:
        raise DemandPlanningError(
            "prompt catalogue must contain treatment slots 7/4/3 and archetype slots 3/2/2"
        )
    return catalogue_version, declared_hash, slots


def _geography_map(
    values: tuple[QueryGeographyVersion, ...],
) -> dict[QueryGeographyLevel, QueryGeographyVersion]:
    result: dict[QueryGeographyLevel, QueryGeographyVersion] = {}
    for geography in values:
        if geography.level in result:
            raise DemandPlanningError("query geographies cannot repeat a level")
        result[geography.level] = geography
    if set(result) != set(_LEVEL_ORDER):
        raise DemandPlanningError(
            "Market demand requires exactly Area, Bigger area, and City geographies"
        )
    return result


def _platform_map(
    values: tuple[PlatformPlanningContract, ...],
) -> dict[SourcePlatform, PlatformPlanningContract]:
    result: dict[SourcePlatform, PlatformPlanningContract] = {}
    for contract in values:
        platform = contract.platform_context.source_platform
        if platform in result:
            raise DemandPlanningError("platform planning contracts cannot repeat a platform")
        result[platform] = contract
    if set(result) != set(MARKET_PLATFORM_ORDER):
        raise DemandPlanningError("Market demand requires exactly four platform contracts")
    return result


def _target_geography(geography: QueryGeographyVersion) -> dict:
    return {
        "id": geography.query_geography_id,
        "version_row_id": geography.version_row_id,
        "level": geography.level.value,
        "name": geography.rendered_name,
        "country_code": geography.country_code,
        "registry_version": geography.registry_version,
        "registry_payload_sha256": geography.registry_payload_sha256,
    }


def _coverage_status(statuses: tuple[DemandEvidenceStatus, ...]) -> DemandEvidenceStatus:
    if all(status is DemandEvidenceStatus.REUSED for status in statuses):
        return DemandEvidenceStatus.REUSED
    if DemandEvidenceStatus.INCOMPATIBLE in statuses:
        return DemandEvidenceStatus.INCOMPATIBLE
    if DemandEvidenceStatus.STALE in statuses:
        return DemandEvidenceStatus.STALE
    return DemandEvidenceStatus.MISSING


def build_demand_plan(
    request: MarketDemandRequest,
    *,
    eligibility_by_spec_sha256: Mapping[str, EligibilityDecision] | None = None,
) -> MarketDemandPlan:
    """Build a byte-stable preview for exact selected/decision-relevant demand."""

    if not isinstance(request, MarketDemandRequest):
        raise TypeError("request must be a MarketDemandRequest")
    status_input = eligibility_by_spec_sha256 or {}
    if not isinstance(status_input, Mapping):
        raise TypeError("eligibility_by_spec_sha256 must be a mapping")
    normalized_statuses: dict[str, DemandEvidenceStatus] = {}
    normalized_decisions: dict[str, EligibilityDecision] = {}
    for spec_hash, decision in status_input.items():
        normalized_hash = _require_sha256(spec_hash, "eligibility specification hash")
        if not isinstance(decision, EligibilityDecision):
            raise TypeError(
                "eligibility_by_spec_sha256 values must be EligibilityDecision objects"
            )
        if decision.measurement_spec_sha256 != normalized_hash:
            raise DemandPlanningError("eligibility decision specification hash mismatch")
        normalized_statuses[normalized_hash] = _status_from_eligibility(decision)
        normalized_decisions[normalized_hash] = decision

    catalogue_version, catalogue_hash, slots = _validated_catalogue(
        request.prompt_catalogue
    )
    geographies = _geography_map(request.query_geographies)
    platforms = _platform_map(request.platforms)

    active_intents = tuple(row for row in request.intents if row.included)
    if not active_intents and not request.archetype_enabled:
        raise DemandPlanningError("demand contains no selected or decision-relevant subject")
    for row in active_intents:
        if not row.intent.is_campaign_selectable:
            raise DemandPlanningError(
                "Market demand requires active, approved, campaign-selectable intents"
            )
        if not row.intent.is_recommendation_prompt_eligible:
            raise DemandPlanningError(
                "Market demand requires recommendation-prompt-eligible intents"
            )

    subjects: list[dict[str, Any]] = []
    for row in active_intents:
        market_subject = MarketMeasurementSubject.for_search_intent(
            row.intent,
            exact_rendered_treatment_term=row.exact_rendered_treatment_term,
        )
        reasons = tuple(
            reason
            for reason, enabled in (
                ("selected", row.selected),
                ("decision_relevant", row.decision_relevant),
            )
            if enabled
        )
        subjects.append(
            {
                "declared_kind": "treatment",
                "market_subject": market_subject,
                "render_subject": {
                    "id": row.intent.patient_search_intent_id,
                    "version_row_id": row.intent.version_row_id,
                    "name": row.exact_rendered_treatment_term,
                },
                "treatment_name": row.exact_rendered_treatment_term,
                "demand_reasons": reasons,
            }
        )
    if request.archetype_enabled:
        archetype = request.archetype
        assert archetype is not None
        market_subject = MarketMeasurementSubject.for_archetype(
            archetype_id=archetype.archetype_id,
            archetype_revision=archetype.archetype_revision,
            archetype_wording_content_sha256=(
                archetype.archetype_wording_content_sha256
            ),
        )
        subjects.append(
            {
                "declared_kind": "clinic_archetype",
                "market_subject": market_subject,
                "render_subject": {
                    "id": archetype.archetype_id,
                    "revision": archetype.archetype_revision,
                    "name": archetype.singular,
                    "singular": archetype.singular,
                    "plural": archetype.plural,
                },
                "treatment_name": None,
                "demand_reasons": ("explicit_archetype",),
            }
        )

    local_name = geographies[QueryGeographyLevel.LOCAL].rendered_name
    greater_area_name = geographies[
        QueryGeographyLevel.GREATER_AREA
    ].rendered_name
    city_name = geographies[QueryGeographyLevel.CITY].rendered_name
    archetype_singular = request.archetype.singular if request.archetype else "not used"
    archetype_plural = request.archetype.plural if request.archetype else "not used"

    specification_builds: dict[str, dict[str, Any]] = {}
    semantic_builds: dict[str, dict[str, Any]] = {}
    rendering_builds: dict[str, PlatformRenderingDemand] = {}
    coverage_builds: dict[str, dict[str, Any]] = {}

    sorted_subjects = sorted(
        subjects,
        key=lambda row: canonical_sha256(row["market_subject"].to_canonical_dict()),
    )
    sorted_slots = sorted(
        slots,
        key=lambda slot: (
            str(slot["subject_kind"]),
            _LEVEL_ORDER[QueryGeographyLevel(str(slot["geography_level"]))],
            str(slot.get("basket_id") or ""),
            int(slot.get("sequence") or 0),
            str(slot["id"]),
        ),
    )
    for subject_row in sorted_subjects:
        market_subject: MarketMeasurementSubject = subject_row["market_subject"]
        for level in sorted(_LEVEL_ORDER, key=_LEVEL_ORDER.get):
            geography = geographies[level]
            coverage_payload = {
                "contract": MARKET_COVERAGE_CELL_SCHEMA,
                "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
                "subject": market_subject.to_canonical_dict(),
                "query_geography": geography.to_canonical_dict(),
            }
            coverage_hash = canonical_sha256(coverage_payload)
            coverage_id = f"market-cell-{coverage_hash[:24]}"
            coverage = coverage_builds.setdefault(
                coverage_id,
                {
                    "hash": coverage_hash,
                    "subject": market_subject,
                    "geography": geography,
                    "reasons": set(),
                    "semantic_ids": set(),
                    "rendering_ids": set(),
                    "spec_hashes": set(),
                },
            )
            coverage["reasons"].update(subject_row["demand_reasons"])

            applicable_slots = tuple(
                slot
                for slot in sorted_slots
                if slot["subject_kind"] == subject_row["declared_kind"]
                and slot["geography_level"] == level.value
            )
            for slot in applicable_slots:
                semantic_payload = {
                    "contract": MARKET_SEMANTIC_PROMPT_SCHEMA,
                    "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
                    "prompt_pack_version": request.prompt_pack_version,
                    "prompt_pack_content_sha256": (
                        request.prompt_pack_content_sha256
                    ),
                    "prompt_catalogue_version": catalogue_version,
                    "prompt_catalogue_content_sha256": catalogue_hash,
                    "subject": market_subject.to_canonical_dict(),
                    "query_geography": geography.to_canonical_dict(),
                    "basket": slot["basket_id"],
                    "semantic_slot": slot["id"],
                    "semantic_intent": slot["semantic_intent"],
                }
                semantic_hash = canonical_sha256(semantic_payload)
                semantic_id = f"market-semantic-demand-{semantic_hash[:24]}"
                semantic = semantic_builds.setdefault(
                    semantic_id,
                    {
                        "hash": semantic_hash,
                        "coverage_cell_id": coverage_id,
                        "subject": market_subject,
                        "geography": geography,
                        "slot": slot,
                        "rendering_ids": set(),
                    },
                )
                coverage["semantic_ids"].add(semantic_id)

                for platform in MARKET_PLATFORM_ORDER:
                    platform_contract = platforms[platform]
                    rendered = render_market_area_slot(
                        platform=platform.value,
                        slot=slot,
                        subject_kind=subject_row["declared_kind"],
                        subject=subject_row["render_subject"],
                        target_geography=_target_geography(geography),
                        expected_target_geography_id=geography.query_geography_id,
                        treatment_name=subject_row["treatment_name"],
                        local_area=local_name,
                        greater_area=greater_area_name,
                        city=city_name,
                        archetype_singular=archetype_singular,
                        archetype_plural=archetype_plural,
                    )
                    prompt_identity = PromptIdentity(
                        prompt_pack_version=request.prompt_pack_version,
                        prompt_pack_content_sha256=(
                            request.prompt_pack_content_sha256
                        ),
                        prompt_catalogue_version=catalogue_version,
                        prompt_catalogue_content_sha256=catalogue_hash,
                        basket=str(slot["basket_id"]),
                        semantic_slot=str(slot["id"]),
                        semantic_intent=str(slot["semantic_intent"]),
                        renderer_contract_version=(
                            MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION
                        ),
                        exact_template_sha256=rendered["template_sha256"],
                        exact_rendering_bindings_sha256=rendered[
                            "rendering_bindings_sha256"
                        ],
                        exact_rendered_prompt_sha256=rendered[
                            "prompt_text_sha256"
                        ],
                    )
                    specification = MarketMeasurementSpecification(
                        subject=market_subject,
                        prompt=prompt_identity,
                        query_geography=QueryGeographyIdentity(geography=geography),
                        platform_context=platform_contract.platform_context,
                        collection_regime=platform_contract.collection_regime,
                        run_protocol=request.run_protocol,
                    )
                    spec_hash = specification.measurement_spec_sha256
                    rendering_payload = {
                        "contract": MARKET_PLATFORM_RENDERING_SCHEMA,
                        "semantic_prompt_sha256": semantic_hash,
                        "measurement_spec_sha256": spec_hash,
                        "source_platform": platform.value,
                        "exact_rendered_prompt": rendered["text"],
                        "exact_rendered_prompt_sha256": rendered[
                            "prompt_text_sha256"
                        ],
                        "exact_template_sha256": rendered["template_sha256"],
                        "exact_rendering_bindings_sha256": rendered[
                            "rendering_bindings_sha256"
                        ],
                    }
                    rendering_hash = canonical_sha256(rendering_payload)
                    rendering_id = f"market-rendering-demand-{rendering_hash[:24]}"
                    rendering = PlatformRenderingDemand(
                        platform_rendering_id=rendering_id,
                        platform_rendering_sha256=rendering_hash,
                        semantic_prompt_id=semantic_id,
                        semantic_prompt_sha256=semantic_hash,
                        coverage_cell_id=coverage_id,
                        measurement_spec_sha256=spec_hash,
                        source_platform=platform,
                        exact_rendered_prompt=rendered["text"],
                        exact_rendered_prompt_sha256=rendered[
                            "prompt_text_sha256"
                        ],
                        exact_template_sha256=rendered["template_sha256"],
                        exact_rendering_bindings_sha256=rendered[
                            "rendering_bindings_sha256"
                        ],
                    )
                    existing_rendering = rendering_builds.get(rendering_id)
                    if existing_rendering is not None and (
                        existing_rendering.to_canonical_dict()
                        != rendering.to_canonical_dict()
                    ):
                        raise DemandPlanningError("platform rendering hash collision")
                    rendering_builds[rendering_id] = rendering
                    semantic["rendering_ids"].add(rendering_id)
                    coverage["rendering_ids"].add(rendering_id)
                    coverage["spec_hashes"].add(spec_hash)

                    existing_spec = specification_builds.get(spec_hash)
                    if existing_spec is not None and (
                        existing_spec["specification"].to_canonical_dict()
                        != specification.to_canonical_dict()
                    ):
                        raise DemandPlanningError("measurement specification hash collision")
                    spec_build = specification_builds.setdefault(
                        spec_hash,
                        {
                            "specification": specification,
                            "coverage_ids": set(),
                            "rendering_ids": set(),
                        },
                    )
                    spec_build["coverage_ids"].add(coverage_id)
                    spec_build["rendering_ids"].add(rendering_id)

    unexpected_statuses = sorted(set(normalized_statuses) - set(specification_builds))
    if unexpected_statuses:
        raise DemandPlanningError(
            "evidence statuses reference specifications outside exact planned demand"
        )

    planned_specs: list[PlannedSpecification] = []
    observation_demands: list[ObservationDemand] = []
    live_actions: list[LiveCollectorAction] = []
    action_counts: dict[SourcePlatform, int] = {
        platform: 0 for platform in MARKET_PLATFORM_ORDER
    }
    for spec_hash in sorted(specification_builds):
        build = specification_builds[spec_hash]
        specification: MarketMeasurementSpecification = build["specification"]
        evidence_status = normalized_statuses.get(
            spec_hash, DemandEvidenceStatus.MISSING
        )
        planned_specs.append(
            PlannedSpecification(
                measurement_spec_sha256=spec_hash,
                specification=specification,
                evidence_status=evidence_status,
                eligibility_decision=normalized_decisions.get(spec_hash),
                coverage_cell_ids=tuple(sorted(build["coverage_ids"])),
                platform_rendering_ids=tuple(sorted(build["rendering_ids"])),
            )
        )
        platform = specification.platform_context.source_platform
        platform_contract = platforms[platform]
        for run_ordinal in MARKET_RUN_ORDINALS:
            assignment = platform_contract.assignment_for_run(run_ordinal)
            observation_payload = {
                "contract": MARKET_OBSERVATION_DEMAND_SCHEMA,
                "measurement_spec_sha256": spec_hash,
                "run_ordinal": run_ordinal,
                "planned_profile_id": assignment.planned_profile_id,
                "planned_profile_revision": assignment.planned_profile_revision,
            }
            observation_hash = canonical_sha256(observation_payload)
            observation_key = f"market-observation-demand-{observation_hash[:24]}"
            observation_demands.append(
                ObservationDemand(
                    observation_demand_key=observation_key,
                    observation_demand_sha256=observation_hash,
                    measurement_spec_sha256=spec_hash,
                    source_platform=platform,
                    run_ordinal=run_ordinal,
                    planned_profile_id=assignment.planned_profile_id,
                    planned_profile_revision=assignment.planned_profile_revision,
                    evidence_status=evidence_status,
                    data_mode=specification.platform_context.data_mode,
                )
            )
            if (
                evidence_status is DemandEvidenceStatus.REUSED
                or specification.platform_context.data_mode is PlatformDataMode.DERIVED
            ):
                continue
            action_payload = {
                "contract": MARKET_LIVE_ACTION_SCHEMA,
                "observation_demand_sha256": observation_hash,
                "measurement_spec_sha256": spec_hash,
                "source_platform": platform.value,
                "run_ordinal": run_ordinal,
                "planned_profile_id": assignment.planned_profile_id,
                "planned_profile_revision": assignment.planned_profile_revision,
            }
            action_hash = canonical_sha256(action_payload)
            live_actions.append(
                LiveCollectorAction(
                    live_collector_action_id=f"market-live-action-{action_hash[:24]}",
                    live_collector_action_sha256=action_hash,
                    observation_demand_key=observation_key,
                    observation_demand_sha256=observation_hash,
                    measurement_spec_sha256=spec_hash,
                    source_platform=platform,
                    run_ordinal=run_ordinal,
                    planned_profile_id=assignment.planned_profile_id,
                    planned_profile_revision=assignment.planned_profile_revision,
                )
            )
            action_counts[platform] += 1

    spec_status = {
        row.measurement_spec_sha256: row.evidence_status for row in planned_specs
    }
    coverage_cells: list[CoverageCell] = []
    for coverage_id in sorted(coverage_builds):
        build = coverage_builds[coverage_id]
        subject: MarketMeasurementSubject = build["subject"]
        geography: QueryGeographyVersion = build["geography"]
        spec_hashes = tuple(sorted(build["spec_hashes"]))
        coverage_cells.append(
            CoverageCell(
                coverage_cell_id=coverage_id,
                coverage_cell_sha256=build["hash"],
                subject_kind=subject.kind,
                subject_id=(
                    subject.patient_search_intent_id
                    if subject.kind is MeasurementSubjectKind.TREATMENT_VISIBILITY
                    else subject.archetype_id
                ),
                query_geography_id=geography.query_geography_id,
                query_geography_version_row_id=geography.version_row_id,
                geography_level=geography.level,
                subject=subject,
                query_geography=geography,
                demand_reasons=tuple(sorted(build["reasons"])),
                evidence_status=_coverage_status(
                    tuple(spec_status[spec_hash] for spec_hash in spec_hashes)
                ),
                semantic_prompt_ids=tuple(sorted(build["semantic_ids"])),
                platform_rendering_ids=tuple(sorted(build["rendering_ids"])),
                measurement_spec_sha256s=spec_hashes,
            )
        )

    semantic_prompts = tuple(
        SemanticPromptDemand(
            semantic_prompt_id=semantic_id,
            semantic_prompt_sha256=build["hash"],
            coverage_cell_id=build["coverage_cell_id"],
            subject_kind=build["subject"].kind,
            subject_id=(
                build["subject"].patient_search_intent_id
                if build["subject"].kind
                is MeasurementSubjectKind.TREATMENT_VISIBILITY
                else build["subject"].archetype_id
            ),
            query_geography_version_row_id=build["geography"].version_row_id,
            geography_level=build["geography"].level,
            basket=str(build["slot"]["basket_id"]),
            semantic_slot=str(build["slot"]["id"]),
            semantic_intent=str(build["slot"]["semantic_intent"]),
            prompt_pack_version=request.prompt_pack_version,
            prompt_pack_content_sha256=request.prompt_pack_content_sha256,
            prompt_catalogue_version=catalogue_version,
            prompt_catalogue_content_sha256=catalogue_hash,
            subject=build["subject"],
            query_geography=build["geography"],
            platform_rendering_ids=tuple(sorted(build["rendering_ids"])),
        )
        for semantic_id, build in sorted(semantic_builds.items())
    )
    platform_renderings = tuple(
        rendering_builds[row_id] for row_id in sorted(rendering_builds)
    )
    observation_tuple = tuple(
        sorted(
            observation_demands,
            key=lambda row: (
                row.measurement_spec_sha256,
                row.run_ordinal,
                row.planned_profile_id,
                row.planned_profile_revision,
            ),
        )
    )
    live_action_tuple = tuple(
        sorted(
            live_actions,
            key=lambda row: (
                row.measurement_spec_sha256,
                row.run_ordinal,
                row.planned_profile_id,
                row.planned_profile_revision,
            ),
        )
    )
    estimate = estimate_collection_cost(action_counts, request.cost_model)
    coverage_status_counts = {
        status: sum(cell.evidence_status is status for cell in coverage_cells)
        for status in DemandEvidenceStatus
    }
    counts = DemandPlanCounts(
        coverage_cell_count=len(coverage_cells),
        reused_cell_count=coverage_status_counts[DemandEvidenceStatus.REUSED],
        missing_cell_count=coverage_status_counts[DemandEvidenceStatus.MISSING],
        stale_cell_count=coverage_status_counts[DemandEvidenceStatus.STALE],
        incompatible_cell_count=coverage_status_counts[
            DemandEvidenceStatus.INCOMPATIBLE
        ],
        collection_cell_count=sum(
            cell.evidence_status is not DemandEvidenceStatus.REUSED
            for cell in coverage_cells
        ),
        measurement_specification_count=len(planned_specs),
        semantic_prompt_count=len(semantic_prompts),
        platform_rendering_count=len(platform_renderings),
        logical_observation_count=len(observation_tuple),
        live_collector_action_count=len(live_action_tuple),
    )
    return MarketDemandPlan(
        prompt_pack_version=request.prompt_pack_version,
        prompt_pack_content_sha256=request.prompt_pack_content_sha256,
        prompt_catalogue_version=catalogue_version,
        prompt_catalogue_content_sha256=catalogue_hash,
        specifications=tuple(planned_specs),
        coverage_cells=tuple(coverage_cells),
        semantic_prompts=semantic_prompts,
        platform_renderings=platform_renderings,
        observation_demands=observation_tuple,
        live_collector_actions=live_action_tuple,
        counts=counts,
        cost_model=request.cost_model,
        cost_estimate=estimate,
    )


__all__ = [
    "MARKET_COVERAGE_CELL_SCHEMA",
    "MARKET_DEMAND_PLAN_SCHEMA",
    "MARKET_LIVE_ACTION_SCHEMA",
    "MARKET_OBSERVATION_DEMAND_SCHEMA",
    "MARKET_PLATFORM_ORDER",
    "MARKET_PLATFORM_RENDERING_SCHEMA",
    "MARKET_RUN_ORDINALS",
    "MARKET_SEMANTIC_PROMPT_SCHEMA",
    "PROFILE_ASSIGNMENT_PLAN_SCHEMA",
    "ArchetypeDemand",
    "CoverageCell",
    "DemandEvidenceStatus",
    "DemandPlanCounts",
    "DemandPlanningError",
    "IntentDemand",
    "LiveCollectorAction",
    "MarketDemandPlan",
    "MarketDemandRequest",
    "ObservationDemand",
    "PlannedProfileAssignment",
    "PlannedSpecification",
    "PlatformPlanningContract",
    "PlatformRenderingDemand",
    "SemanticPromptDemand",
    "build_demand_plan",
    "profile_assignment_plan_sha256",
]
