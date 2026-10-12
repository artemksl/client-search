"""Pure, fail-closed projection of Market Evidence coverage cells.

The projection boundary deliberately derives the complete set of required
evidence identifiers from a hash-pinned planner output.  Callers cannot declare
a convenient subset to make incomplete evidence look like measured
nonappearance.

This module performs no persistence, collection, or external I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable

from clinic_geo.evidence.purpose import EvidencePurpose, validate_evidence_purpose
from clinic_geo.integrity import canonical_sha256

from .contracts import MeasurementSubjectKind
from .planner import MarketDemandPlan
from .classification import (
    MarketClassification,
    ValidatedMarketClassification,
)
from .occurrences import (
    ArtifactCoverageProof,
    OccurrenceCoverageState,
    OccurrenceExtractionRun,
)


MARKET_PROJECTION_SCHEMA = "market_evidence.coverage_projection.v1"
MARKET_PROJECTION_ALGORITHM_VERSION = "market-coverage-projection-v1"
MARKET_COVERAGE_REQUIREMENT_SCHEMA = "market_evidence.coverage_requirement.v1"
IMMUTABLE_PROJECTION_RUN_SCHEMA = "market_evidence.immutable_projection_run.v1"
IMMUTABLE_PROJECTION_OUTPUT_SCHEMA = "market_evidence.projection_output.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_EXPECTED_SEMANTIC_PROMPT_COUNTS = {
    (MeasurementSubjectKind.TREATMENT_VISIBILITY, "local"): 7,
    (MeasurementSubjectKind.TREATMENT_VISIBILITY, "greater_area"): 4,
    (MeasurementSubjectKind.TREATMENT_VISIBILITY, "city"): 3,
    (MeasurementSubjectKind.ARCHETYPE_RESEARCH, "local"): 3,
    (MeasurementSubjectKind.ARCHETYPE_RESEARCH, "greater_area"): 2,
    (MeasurementSubjectKind.ARCHETYPE_RESEARCH, "city"): 2,
}


class ProjectionOutcome(str, Enum):
    """The closed Market Evidence visibility vocabulary."""

    APPEARED = "appeared"
    MEASURED_NOT_OBSERVED = "measured_not_observed"
    UNKNOWN = "unknown"


# ``ProjectionState`` is retained as a domain-friendly spelling for callers
# that refer to the values as states rather than outcomes.
ProjectionState = ProjectionOutcome


class ProjectionTargetKind(str, Enum):
    """Kinds that can be matched without conflating account and branch IDs."""

    BRANCH = "branch"
    ENTITY = "entity"


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageRequirement:
    """Exact observation universe for one validated planner coverage cell.

    Only the plan and a cell identifier are caller inputs.  All hashes and
    required observation keys are derived after independently checking the
    plan's cell/specification/rendering/observation links and the fixed
    semantic-slot cardinality.  This is the authority required for a negative
    projection.
    """

    plan: MarketDemandPlan = field(repr=False, compare=False)
    coverage_cell_id: str
    plan_sha256: str = field(init=False)
    coverage_cell_sha256: str = field(init=False)
    required_evidence_ids: tuple[str, ...] = field(init=False)
    requirement_sha256: str = field(init=False)
    schema_version: str = field(
        default=MARKET_COVERAGE_REQUIREMENT_SCHEMA, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MarketDemandPlan):
            raise TypeError("plan must be a MarketDemandPlan")
        coverage_cell_id = _require_identifier(
            self.coverage_cell_id, "coverage_cell_id"
        )
        cells = tuple(
            cell
            for cell in self.plan.coverage_cells
            if cell.coverage_cell_id == coverage_cell_id
        )
        if len(cells) != 1:
            raise ValueError(
                "coverage_cell_id must identify exactly one cell in the plan"
            )
        cell = cells[0]

        expected_semantic_count = _EXPECTED_SEMANTIC_PROMPT_COUNTS.get(
            (cell.subject_kind, cell.geography_level.value)
        )
        if expected_semantic_count is None:
            raise ValueError("coverage cell has an unsupported subject/geography pair")
        expected_rendering_count = expected_semantic_count * 4
        expected_observation_count = expected_rendering_count * 3

        semantic_rows = tuple(
            row
            for row in self.plan.semantic_prompts
            if row.coverage_cell_id == coverage_cell_id
        )
        semantic_ids = tuple(sorted(row.semantic_prompt_id for row in semantic_rows))
        if (
            len(semantic_rows) != expected_semantic_count
            or semantic_ids != tuple(sorted(cell.semantic_prompt_ids))
        ):
            raise ValueError(
                "coverage cell does not contain its exact semantic-prompt universe"
            )

        rendering_rows = tuple(
            row
            for row in self.plan.platform_renderings
            if row.coverage_cell_id == coverage_cell_id
        )
        rendering_ids = tuple(
            sorted(row.platform_rendering_id for row in rendering_rows)
        )
        if (
            len(rendering_rows) != expected_rendering_count
            or rendering_ids != tuple(sorted(cell.platform_rendering_ids))
        ):
            raise ValueError(
                "coverage cell does not contain four renderings per semantic prompt"
            )
        renderings_by_semantic: dict[str, list[object]] = {}
        for row in rendering_rows:
            renderings_by_semantic.setdefault(row.semantic_prompt_id, []).append(row)
        if set(renderings_by_semantic) != set(semantic_ids) or any(
            len(rows) != 4
            or len({row.source_platform for row in rows}) != 4
            for rows in renderings_by_semantic.values()
        ):
            raise ValueError(
                "coverage renderings must span four platforms for every prompt"
            )

        specification_hashes = tuple(
            sorted(row.measurement_spec_sha256 for row in rendering_rows)
        )
        if (
            len(set(specification_hashes)) != expected_rendering_count
            or tuple(sorted(cell.measurement_spec_sha256s))
            != specification_hashes
        ):
            raise ValueError(
                "coverage cell specification links are incomplete or duplicated"
            )
        planned_specifications = {
            row.measurement_spec_sha256: row for row in self.plan.specifications
        }
        if any(
            spec_hash not in planned_specifications
            or coverage_cell_id
            not in planned_specifications[spec_hash].coverage_cell_ids
            for spec_hash in specification_hashes
        ):
            raise ValueError("coverage cell has an invalid planned-specification link")

        specification_set = frozenset(specification_hashes)
        observation_rows = tuple(
            row
            for row in self.plan.observation_demands
            if row.measurement_spec_sha256 in specification_set
        )
        observations_by_spec: dict[str, list[object]] = {}
        for row in observation_rows:
            observations_by_spec.setdefault(row.measurement_spec_sha256, []).append(row)
        if set(observations_by_spec) != specification_set or any(
            len(rows) != 3
            or {row.run_ordinal for row in rows} != {1, 2, 3}
            or len({row.observation_demand_key for row in rows}) != 3
            for rows in observations_by_spec.values()
        ):
            raise ValueError(
                "coverage cell requires exactly three observations per specification"
            )
        required = tuple(
            sorted(row.observation_demand_key for row in observation_rows)
        )
        if len(required) != expected_observation_count or len(set(required)) != len(
            required
        ):
            raise ValueError(
                "coverage cell does not resolve to its exact observation universe"
            )
        plan_sha256 = self.plan.plan_sha256
        requirement_payload = {
            "schema_version": self.schema_version,
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "plan_sha256": plan_sha256,
            "coverage_cell_id": coverage_cell_id,
            "coverage_cell_sha256": cell.coverage_cell_sha256,
            "required_evidence_ids": list(required),
        }
        object.__setattr__(self, "plan_sha256", plan_sha256)
        object.__setattr__(
            self, "coverage_cell_sha256", cell.coverage_cell_sha256
        )
        object.__setattr__(self, "required_evidence_ids", required)
        object.__setattr__(
            self, "requirement_sha256", canonical_sha256(requirement_payload)
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "plan_sha256": self.plan_sha256,
            "coverage_cell_id": self.coverage_cell_id,
            "coverage_cell_sha256": self.coverage_cell_sha256,
            "required_evidence_ids": list(self.required_evidence_ids),
            "requirement_sha256": self.requirement_sha256,
        }


class CoverageWarningCode(str, Enum):
    """Deterministic reasons why an exact coverage cell is not complete."""

    MISSING_REQUIRED_EVIDENCE = "missing_required_evidence"
    DUPLICATE_REQUIRED_EVIDENCE = "duplicate_required_evidence"
    UNEXPECTED_EVIDENCE = "unexpected_evidence"
    INVALID_ARTIFACT_HASH = "invalid_artifact_hash"
    INCOMPATIBLE_EVIDENCE = "incompatible_evidence"
    STALE_EVIDENCE = "stale_evidence"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    UNCLASSIFIED_EVIDENCE = "unclassified_evidence"
    UNPROJECTED_EVIDENCE = "unprojected_evidence"
    UNPROVEN_OCCURRENCE_COVERAGE = "unproven_occurrence_coverage"
    FAILED_EVIDENCE = "failed_evidence"
    AMBIGUOUS_EVIDENCE = "ambiguous_evidence"
    SUPERSEDED_EVIDENCE = "superseded_evidence"
    MATCH_SOURCE_OUTSIDE_CELL = "match_source_outside_cell"
    UNVALIDATED_MATCH = "unvalidated_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    MATCH_SOURCE_NOT_ELIGIBLE = "match_source_not_eligible"


def _require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise ValueError(f"{field_name} cannot contain surrounding whitespace")
    return value


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a bool")


def _require_market_purpose(value: EvidencePurpose | str) -> EvidencePurpose:
    purpose = validate_evidence_purpose(value)
    if purpose is not EvidencePurpose.MARKET_DISCOVERY:
        raise ValueError("Market Evidence projection requires market_discovery purpose")
    return purpose


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionTarget:
    """The exact branch or entity whose appearance is being projected."""

    kind: ProjectionTargetKind
    target_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProjectionTargetKind):
            raise TypeError("kind must be a ProjectionTargetKind")
        _require_identifier(self.target_id, "target_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionEvidenceItem:
    """Projection-relevant state for one required prompt/platform/run item.

    ``complete`` describes the collection item itself.  Classification and
    projection completion remain explicit so that unfinished downstream work
    cannot silently become nonappearance.
    """

    evidence_id: str
    fresh: bool
    complete: bool
    compatible: bool
    artifact_hash_valid: bool
    classified: bool
    projected: bool
    occurrence_coverage_complete: bool = False
    failed: bool = False
    ambiguous: bool = False
    superseded: bool = False
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        _require_identifier(self.evidence_id, "evidence_id")
        for field_name in (
            "fresh",
            "complete",
            "compatible",
            "artifact_hash_valid",
            "classified",
            "projected",
            "occurrence_coverage_complete",
            "failed",
            "ambiguous",
            "superseded",
        ):
            _require_bool(getattr(self, field_name), field_name)
        object.__setattr__(self, "purpose", _require_market_purpose(self.purpose))

    @property
    def eligible(self) -> bool:
        """Whether this exact item may support either projection conclusion."""

        return (
            self.fresh
            and self.complete
            and self.compatible
            and self.artifact_hash_valid
            and self.classified
            and self.projected
            and self.occurrence_coverage_complete
            and not self.failed
            and not self.ambiguous
            and not self.superseded
        )

    @property
    def eligible_for_appearance(self) -> bool:
        """Positive exact matches do not require exhaustive negative coverage."""

        return (
            self.fresh
            and self.complete
            and self.compatible
            and self.artifact_hash_valid
            and self.classified
            and self.projected
            and not self.failed
            and not self.ambiguous
            and not self.superseded
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedProjectionMatch:
    """One candidate match tied to its exact source evidence item.

    A validation record can represent rejection or ambiguity.  Only
    ``validated=True`` and ``ambiguous=False`` is an affirmative match.
    Retaining rejected records lets the projector fail closed instead of
    accidentally treating defective matching work as nonappearance.
    """

    source_evidence_id: str
    target_kind: ProjectionTargetKind
    target_id: str
    validated: bool = True
    ambiguous: bool = False
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        _require_identifier(self.source_evidence_id, "source_evidence_id")
        if not isinstance(self.target_kind, ProjectionTargetKind):
            raise TypeError("target_kind must be a ProjectionTargetKind")
        _require_identifier(self.target_id, "target_id")
        _require_bool(self.validated, "validated")
        _require_bool(self.ambiguous, "ambiguous")
        object.__setattr__(self, "purpose", _require_market_purpose(self.purpose))

    @property
    def is_validated(self) -> bool:
        return self.validated and not self.ambiguous

    def matches(self, target: ProjectionTarget) -> bool:
        if not isinstance(target, ProjectionTarget):
            raise TypeError("target must be a ProjectionTarget")
        return (
            self.is_validated
            and self.target_kind is target.kind
            and self.target_id == target.target_id
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionResult:
    """Immutable, self-validating projection and its exact input manifest.

    Negative authority (the ability to conclude ``measured_not_observed``) is
    never a caller-suppliable flag.  It is derived structurally: only the
    presence of validated :class:`CompletedProjectionDecisions` — each of which
    can be built only from a validated immutable projection run bound to a
    raw-byte-revalidated occurrence run — grants it.  A ``ProjectionResult``
    constructed without such runs can never become ``measured_not_observed``,
    regardless of the evidence-item booleans a caller supplies.
    """

    requirement: CoverageRequirement
    target: ProjectionTarget
    evidence_items: tuple[ProjectionEvidenceItem, ...] = field(repr=False)
    candidate_matches: tuple[ValidatedProjectionMatch, ...] = field(repr=False)
    completed_runs: tuple["CompletedProjectionDecisions", ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    outcome: ProjectionOutcome = field(init=False)
    coverage_warning: bool = field(init=False)
    coverage_warning_codes: tuple[CoverageWarningCode, ...] = field(init=False)
    matched_evidence_ids: tuple[str, ...] = field(init=False)
    evidence_input_sha256: str = field(init=False)
    negative_authority_run_ids: tuple[str, ...] = field(init=False)
    purpose: EvidencePurpose = field(
        default=EvidencePurpose.MARKET_DISCOVERY, init=False
    )
    schema_version: str = field(default=MARKET_PROJECTION_SCHEMA, init=False)
    algorithm_version: str = field(
        default=MARKET_PROJECTION_ALGORITHM_VERSION, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.requirement, CoverageRequirement):
            raise TypeError("requirement must be a CoverageRequirement")
        if not isinstance(self.target, ProjectionTarget):
            raise TypeError("target must be a ProjectionTarget")
        if not isinstance(self.evidence_items, tuple) or any(
            not isinstance(item, ProjectionEvidenceItem)
            for item in self.evidence_items
        ):
            raise TypeError(
                "evidence_items must be a tuple of ProjectionEvidenceItem values"
            )
        if not isinstance(self.candidate_matches, tuple) or any(
            not isinstance(item, ValidatedProjectionMatch)
            for item in self.candidate_matches
        ):
            raise TypeError(
                "candidate_matches must be a tuple of ValidatedProjectionMatch values"
            )
        if not isinstance(self.completed_runs, tuple) or any(
            not isinstance(item, CompletedProjectionDecisions)
            for item in self.completed_runs
        ):
            raise TypeError(
                "completed_runs must be a tuple of CompletedProjectionDecisions values"
            )
        # Negative authority is granted structurally, and only by the presence
        # of genuine completed-projection runs actually carried on this result.
        negative_authorized = bool(self.completed_runs)
        if negative_authorized:
            if self.candidate_matches:
                raise ValueError(
                    "completed-decision projection derives its own matches; "
                    "candidate_matches must be empty"
                )
            working_items, working_matches, run_ids = (
                _authoritative_items_and_matches(
                    self.requirement, self.evidence_items, self.completed_runs
                )
            )
        else:
            working_items = self.evidence_items
            working_matches = self.candidate_matches
            run_ids = ()
        ordered_items = tuple(
            sorted(
                working_items,
                key=lambda item: (
                    item.evidence_id,
                    item.fresh,
                    item.complete,
                    item.compatible,
                    item.artifact_hash_valid,
                    item.classified,
                    item.projected,
                    item.occurrence_coverage_complete,
                    item.failed,
                    item.ambiguous,
                    item.superseded,
                ),
            )
        )
        ordered_matches = tuple(
            sorted(
                working_matches,
                key=lambda item: (
                    item.source_evidence_id,
                    item.target_kind.value,
                    item.target_id,
                    item.validated,
                    item.ambiguous,
                ),
            )
        )
        object.__setattr__(self, "evidence_items", ordered_items)
        object.__setattr__(self, "candidate_matches", ordered_matches)
        object.__setattr__(self, "negative_authority_run_ids", run_ids)
        outcome, warning_codes, matched_ids = _derive_projection_state(
            self.requirement,
            ordered_items,
            self.target,
            ordered_matches,
            negative_authorized=negative_authorized,
        )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "coverage_warning_codes", warning_codes)
        object.__setattr__(self, "coverage_warning", bool(warning_codes))
        object.__setattr__(self, "matched_evidence_ids", matched_ids)
        input_payload = {
            "coverage_requirement_sha256": self.requirement.requirement_sha256,
            "target_kind": self.target.kind.value,
            "target_id": self.target.target_id,
            "evidence_items": self._evidence_manifest(),
            "candidate_matches": self._match_manifest(),
            "negative_authority": (
                {
                    "schema_version": "verified-completed-projection-decisions.v2",
                    "projection_run_ids": list(run_ids),
                }
                if negative_authorized
                else None
            ),
        }
        object.__setattr__(
            self, "evidence_input_sha256", canonical_sha256(input_payload)
        )

    @property
    def required_evidence_count(self) -> int:
        return len(self.requirement.required_evidence_ids)

    @property
    def received_evidence_count(self) -> int:
        return len(self.evidence_items)

    def _evidence_manifest(self) -> list[dict]:
        return [
            {
                "evidence_id": item.evidence_id,
                "purpose": item.purpose.value,
                "fresh": item.fresh,
                "complete": item.complete,
                "compatible": item.compatible,
                "artifact_hash_valid": item.artifact_hash_valid,
                "classified": item.classified,
                "projected": item.projected,
                "occurrence_coverage_complete": item.occurrence_coverage_complete,
                "failed": item.failed,
                "ambiguous": item.ambiguous,
                "superseded": item.superseded,
            }
            for item in self.evidence_items
        ]

    def _match_manifest(self) -> list[dict]:
        return [
            {
                "source_evidence_id": item.source_evidence_id,
                "purpose": item.purpose.value,
                "target_kind": item.target_kind.value,
                "target_id": item.target_id,
                "validated": item.validated,
                "ambiguous": item.ambiguous,
            }
            for item in self.candidate_matches
        ]

    @property
    def state(self) -> ProjectionOutcome:
        return self.outcome

    @property
    def projection_state(self) -> ProjectionOutcome:
        return self.outcome

    @property
    def coverage_complete(self) -> bool:
        return not self.coverage_warning

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "purpose": self.purpose.value,
            "coverage_requirement": self.requirement.to_canonical_dict(),
            "target": {
                "kind": self.target.kind.value,
                "target_id": self.target.target_id,
            },
            "outcome": self.outcome.value,
            "coverage_warning": self.coverage_warning,
            "coverage_warning_codes": tuple(
                code.value for code in self.coverage_warning_codes
            ),
            "matched_evidence_ids": self.matched_evidence_ids,
            "required_evidence_count": self.required_evidence_count,
            "received_evidence_count": self.received_evidence_count,
            "evidence_input_sha256": self.evidence_input_sha256,
            "evidence_items": self._evidence_manifest(),
            "candidate_matches": self._match_manifest(),
            "negative_authority": (
                {
                    "schema_version": "verified-completed-projection-decisions.v2",
                    "projection_run_ids": list(self.negative_authority_run_ids),
                }
                if self.negative_authority_run_ids
                else None
            ),
        }


def _item_warning_codes(
    item: ProjectionEvidenceItem,
) -> tuple[CoverageWarningCode, ...]:
    warnings: list[CoverageWarningCode] = []
    if not item.artifact_hash_valid:
        warnings.append(CoverageWarningCode.INVALID_ARTIFACT_HASH)
    if not item.compatible:
        warnings.append(CoverageWarningCode.INCOMPATIBLE_EVIDENCE)
    if not item.fresh:
        warnings.append(CoverageWarningCode.STALE_EVIDENCE)
    if not item.complete:
        warnings.append(CoverageWarningCode.INCOMPLETE_EVIDENCE)
    if not item.classified:
        warnings.append(CoverageWarningCode.UNCLASSIFIED_EVIDENCE)
    if not item.projected:
        warnings.append(CoverageWarningCode.UNPROJECTED_EVIDENCE)
    if not item.occurrence_coverage_complete:
        warnings.append(CoverageWarningCode.UNPROVEN_OCCURRENCE_COVERAGE)
    if item.failed:
        warnings.append(CoverageWarningCode.FAILED_EVIDENCE)
    if item.ambiguous:
        warnings.append(CoverageWarningCode.AMBIGUOUS_EVIDENCE)
    if item.superseded:
        warnings.append(CoverageWarningCode.SUPERSEDED_EVIDENCE)
    return tuple(warnings)


def _deduplicate_warnings(
    warnings: Iterable[CoverageWarningCode],
) -> tuple[CoverageWarningCode, ...]:
    seen = set(warnings)
    return tuple(code for code in CoverageWarningCode if code in seen)


def _derive_projection_state(
    requirement: CoverageRequirement,
    items: tuple[ProjectionEvidenceItem, ...],
    target: ProjectionTarget,
    candidate_matches: tuple[ValidatedProjectionMatch, ...],
    *,
    negative_authorized: bool,
) -> tuple[
    ProjectionOutcome,
    tuple[CoverageWarningCode, ...],
    tuple[str, ...],
]:
    """Derive output fields from the complete immutable input manifest."""

    required = requirement.required_evidence_ids
    required_set = frozenset(required)
    for item in items:
        # Constructors enforce this too.  The explicit boundary check prevents
        # forged or future relaxed objects from crossing the purpose firewall.
        _require_market_purpose(item.purpose)

    items_by_id: dict[str, list[ProjectionEvidenceItem]] = {}
    for item in items:
        items_by_id.setdefault(item.evidence_id, []).append(item)

    warnings: list[CoverageWarningCode] = []
    missing_ids = required_set.difference(items_by_id)
    if missing_ids:
        warnings.append(CoverageWarningCode.MISSING_REQUIRED_EVIDENCE)
    if any(
        len(items_by_id.get(evidence_id, ())) > 1 for evidence_id in required_set
    ):
        warnings.append(CoverageWarningCode.DUPLICATE_REQUIRED_EVIDENCE)
    if set(items_by_id).difference(required_set):
        warnings.append(CoverageWarningCode.UNEXPECTED_EVIDENCE)

    unique_required_items: dict[str, ProjectionEvidenceItem] = {}
    for evidence_id in required:
        candidates = items_by_id.get(evidence_id, ())
        if len(candidates) == 1:
            item = candidates[0]
            unique_required_items[evidence_id] = item
            warnings.extend(_item_warning_codes(item))

    matched_evidence_ids: set[str] = set()
    for match in candidate_matches:
        _require_market_purpose(match.purpose)
        source = unique_required_items.get(match.source_evidence_id)
        if match.source_evidence_id not in required_set or source is None:
            warnings.append(CoverageWarningCode.MATCH_SOURCE_OUTSIDE_CELL)
            continue
        if not match.validated:
            warnings.append(CoverageWarningCode.UNVALIDATED_MATCH)
            continue
        if match.ambiguous:
            warnings.append(CoverageWarningCode.AMBIGUOUS_MATCH)
            continue
        if not source.eligible_for_appearance:
            warnings.append(CoverageWarningCode.MATCH_SOURCE_NOT_ELIGIBLE)
            continue
        if match.matches(target):
            matched_evidence_ids.add(match.source_evidence_id)

    if not matched_evidence_ids and not negative_authorized:
        warnings.append(CoverageWarningCode.UNPROVEN_OCCURRENCE_COVERAGE)
    warning_codes = _deduplicate_warnings(warnings)
    ordered_matched_ids = tuple(sorted(matched_evidence_ids))
    if matched_evidence_ids:
        outcome = ProjectionOutcome.APPEARED
    elif not warning_codes:
        outcome = ProjectionOutcome.MEASURED_NOT_OBSERVED
    else:
        outcome = ProjectionOutcome.UNKNOWN

    return outcome, warning_codes, ordered_matched_ids


def project_coverage_cell(
    requirement: CoverageRequirement,
    evidence_items: Iterable[ProjectionEvidenceItem],
    target: ProjectionTarget,
    matches: Iterable[ValidatedProjectionMatch] = (),
) -> ProjectionResult:
    """Project positive matches without granting negative authority.

    The planner-bound requirement is the authoritative cell universe.  A
    validated match counts only when its source is a unique required item that
    is itself fresh, complete, compatible, artifact-valid, classified, and
    projected.  Other cell defects do not erase that appearance; they are
    exposed through ``coverage_warning``.  This lower-level operation never
    emits measured nonappearance: only verified completed decisions can prove
    exhaustive occurrence coverage.  Without an appearance it returns
    ``unknown``.
    """

    if not isinstance(requirement, CoverageRequirement):
        raise TypeError("requirement must be a CoverageRequirement")
    if not isinstance(target, ProjectionTarget):
        raise TypeError("target must be a ProjectionTarget")
    try:
        items = tuple(evidence_items)
    except TypeError as exc:
        raise TypeError("evidence_items must be iterable") from exc
    if any(not isinstance(item, ProjectionEvidenceItem) for item in items):
        raise TypeError(
            "evidence_items must contain ProjectionEvidenceItem values"
        )
    try:
        candidate_matches = tuple(matches)
    except TypeError as exc:
        raise TypeError("matches must be iterable") from exc
    if any(
        not isinstance(item, ValidatedProjectionMatch)
        for item in candidate_matches
    ):
        raise TypeError(
            "matches must contain ValidatedProjectionMatch values"
        )
    return ProjectionResult(
        requirement=requirement,
        target=target,
        evidence_items=items,
        candidate_matches=candidate_matches,
    )


# Small aliases make the pure operation natural to call from either a coverage
# read model or a projection workflow without creating separate semantics.
project_cell = project_coverage_cell
project_visibility = project_coverage_cell


class ProjectionDecisionState(str, Enum):
    """Per-occurrence processing result kept separate from coverage outcome."""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    ACCOUNT_LEVEL_REVIEW = "account_level_review"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionAlias:
    """One reviewed literal alias and its exact branch/entity candidates."""

    literal_name: str
    targets: tuple[ProjectionTarget, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.literal_name, "literal_name")
        if not isinstance(self.targets, tuple) or any(
            not isinstance(item, ProjectionTarget) for item in self.targets
        ):
            raise TypeError("targets must be a tuple of ProjectionTarget values")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("projection alias targets cannot repeat")

    def to_canonical_dict(self) -> dict:
        return {
            "literal_name": self.literal_name,
            "targets": [
                {"kind": item.kind.value, "target_id": item.target_id}
                for item in sorted(
                    self.targets, key=lambda value: (value.kind.value, value.target_id)
                )
            ],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionIdentityRelease:
    """Immutable alias set; changing any alias creates a new release hash."""

    branch_identity_version: str
    aliases: tuple[ProjectionAlias, ...]
    alias_set_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identifier(self.branch_identity_version, "branch_identity_version")
        if not isinstance(self.aliases, tuple) or any(
            not isinstance(item, ProjectionAlias) for item in self.aliases
        ):
            raise TypeError("aliases must be a tuple of ProjectionAlias values")
        names = [item.literal_name for item in self.aliases]
        if len(set(names)) != len(names):
            raise ValueError("projection alias literal names cannot repeat")
        ordered = tuple(sorted(self.aliases, key=lambda item: item.literal_name))
        object.__setattr__(self, "aliases", ordered)
        object.__setattr__(
            self,
            "alias_set_sha256",
            canonical_sha256(
                {
                    "schema_version": "market_evidence.projection_alias_set.v1",
                    "branch_identity_version": self.branch_identity_version,
                    "aliases": [item.to_canonical_dict() for item in ordered],
                }
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ImmutableProjectionDecision:
    occurrence_id: str
    state: ProjectionDecisionState
    matches: tuple[ValidatedProjectionMatch, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.occurrence_id, "occurrence_id")
        if not isinstance(self.state, ProjectionDecisionState):
            raise TypeError("state must be a ProjectionDecisionState")
        if not isinstance(self.matches, tuple) or any(
            not isinstance(item, ValidatedProjectionMatch) for item in self.matches
        ):
            raise TypeError("matches must contain ValidatedProjectionMatch values")
        if any(item.source_evidence_id != self.occurrence_id for item in self.matches):
            raise ValueError("projection decision matches must use its occurrence")

    def to_canonical_dict(self) -> dict:
        return {
            "occurrence_id": self.occurrence_id,
            "state": self.state.value,
            "matches": [
                {
                    "source_evidence_id": item.source_evidence_id,
                    "target_kind": item.target_kind.value,
                    "target_id": item.target_id,
                    "validated": item.validated,
                    "ambiguous": item.ambiguous,
                }
                for item in sorted(
                    self.matches,
                    key=lambda value: (
                        value.target_kind.value,
                        value.target_id,
                        value.validated,
                        value.ambiguous,
                    ),
                )
            ],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ImmutableProjectionRun:
    classification: ValidatedMarketClassification = field(repr=False)
    identity_release: ProjectionIdentityRelease
    plan_sha256: str
    treatment_catalogue_release_id: str
    treatment_catalogue_payload_sha256: str
    projection_algorithm_version: str = "market-identity-projection-v1"
    schema_version: str = IMMUTABLE_PROJECTION_RUN_SCHEMA
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    decisions: tuple[ImmutableProjectionDecision, ...] = field(init=False)
    input_manifest_sha256: str = field(init=False)
    output_manifest_sha256: str = field(init=False)
    processed_occurrence_manifest_sha256: str = field(init=False)
    projection_run_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.classification, ValidatedMarketClassification):
            raise TypeError("classification must be a ValidatedMarketClassification")
        if not isinstance(self.identity_release, ProjectionIdentityRelease):
            raise TypeError("identity_release must be a ProjectionIdentityRelease")
        _require_identifier(self.plan_sha256, "plan_sha256")
        if len(self.plan_sha256) != 64:
            raise ValueError("plan_sha256 must be a SHA-256")
        _require_identifier(
            self.treatment_catalogue_release_id, "treatment_catalogue_release_id"
        )
        _require_identifier(
            self.treatment_catalogue_payload_sha256,
            "treatment_catalogue_payload_sha256",
        )
        if len(self.treatment_catalogue_payload_sha256) != 64:
            raise ValueError("treatment catalogue payload hash must be a SHA-256")
        _require_identifier(self.projection_algorithm_version, "projection_algorithm_version")
        if self.schema_version != IMMUTABLE_PROJECTION_RUN_SCHEMA:
            raise ValueError("unsupported immutable projection run schema")
        if self.purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError("immutable projection requires market_discovery")
        # P2-A007: per-occurrence decision state is not a caller input.  It is
        # re-derived here from the validated classification (whose results are
        # bound to the raw classifier response) and the reviewed identity
        # release.  A caller can neither hand-author a decision nor turn a
        # clinic-name appearance into an unmatched negative.
        decisions = _derive_projection_decisions(
            self.classification, self.identity_release
        )
        object.__setattr__(self, "decisions", decisions)
        expected = {item.occurrence_id for item in self.classification.results}
        processed_hash = canonical_sha256(
            {
                "schema_version": "market_evidence.projected_occurrence_manifest.v1",
                "occurrence_ids": sorted(expected),
            }
        )
        input_manifest = {
            "schema_version": "market_evidence.projection_input.v1",
            "purpose": self.purpose.value,
            "classification_run_id": self.classification.request.classification_run_id,
            "classification_output_manifest_sha256": (
                self.classification.output_manifest_sha256
            ),
            "occurrence_run_id": self.classification.request.occurrence_run_id,
            "occurrence_coverage_manifest_sha256": (
                self.classification.request.occurrence_coverage_manifest_sha256
            ),
            "occurrence_coverage_complete": (
                self.classification.request.occurrence_coverage_complete
            ),
            "alias_set_sha256": self.identity_release.alias_set_sha256,
            "branch_identity_version": self.identity_release.branch_identity_version,
            "treatment_catalogue_release_id": self.treatment_catalogue_release_id,
            "treatment_catalogue_payload_sha256": (
                self.treatment_catalogue_payload_sha256
            ),
            "projection_algorithm_version": self.projection_algorithm_version,
            "processed_occurrence_manifest_sha256": processed_hash,
        }
        output_manifest = self.output_manifest()
        input_hash = canonical_sha256(input_manifest)
        output_hash = canonical_sha256(output_manifest)
        run_hash = canonical_sha256(
            {
                "schema_version": self.schema_version,
                "purpose": self.purpose.value,
                "plan_sha256": self.plan_sha256,
                "input_manifest_sha256": input_hash,
                "output_manifest_sha256": output_hash,
            }
        )
        object.__setattr__(self, "processed_occurrence_manifest_sha256", processed_hash)
        object.__setattr__(self, "input_manifest_sha256", input_hash)
        object.__setattr__(self, "output_manifest_sha256", output_hash)
        object.__setattr__(self, "projection_run_id", f"market-projection-run-{run_hash}")

    @property
    def matches(self) -> tuple[ValidatedProjectionMatch, ...]:
        return tuple(match for decision in self.decisions for match in decision.matches)

    @property
    def has_ambiguity(self) -> bool:
        return any(
            item.state in {
                ProjectionDecisionState.AMBIGUOUS,
                ProjectionDecisionState.ACCOUNT_LEVEL_REVIEW,
            }
            for item in self.decisions
        )

    def output_manifest(self) -> dict:
        return {
            "schema_version": IMMUTABLE_PROJECTION_OUTPUT_SCHEMA,
            "decisions": [item.to_canonical_dict() for item in self.decisions],
        }

    def completed_decisions(self) -> "CompletedProjectionDecisions":
        return CompletedProjectionDecisions(projection_run=self)


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletedProjectionDecisions:
    """Exact completed projection facts consumed by coverage calculation.

    P2-A007: this value can be constructed ONLY from a validated
    :class:`ImmutableProjectionRun`, whose decisions are re-derived from a
    :class:`ValidatedMarketClassification` bound to the raw classifier response,
    which is in turn bound to a raw-byte-revalidated occurrence run.  Every
    field below is derived from that run; nothing is caller-authored.  A caller
    can neither hand-author a decision nor supply self-consistent proof hashes
    without the classifier run that produced them.
    """

    projection_run: ImmutableProjectionRun = field(repr=False)
    projection_run_id: str = field(init=False)
    plan_sha256: str = field(init=False)
    source_evidence_ids: tuple[str, ...] = field(init=False)
    occurrence_evidence_links: tuple[tuple[str, str], ...] = field(init=False)
    decisions: tuple[ImmutableProjectionDecision, ...] = field(init=False)
    output_manifest_sha256: str = field(init=False)
    occurrence_coverage_manifest_sha256: str = field(init=False)
    source_occurrence_coverage: tuple[tuple[str, bool, str], ...] = field(init=False)
    occurrence_coverage_proofs: tuple[ArtifactCoverageProof, ...] = field(
        init=False, repr=False
    )
    occurrence_run: OccurrenceExtractionRun = field(init=False, repr=False)

    def __post_init__(self) -> None:
        run = self.projection_run
        if not isinstance(run, ImmutableProjectionRun):
            raise TypeError(
                "completed projection decisions require a validated ImmutableProjectionRun"
            )
        request = run.classification.request
        occurrence_run = request.occurrence_run
        proofs = tuple(
            sorted(
                occurrence_run.coverage_proofs,
                key=lambda item: item.logical_observation_id,
            )
        )
        coverage = tuple(
            sorted(
                (
                    item.coverage_evidence_id,
                    item.state is OccurrenceCoverageState.COMPLETE,
                    item.proof_sha256,
                )
                for item in proofs
            )
        )
        links = tuple(
            sorted(
                (item.occurrence_id, item.coverage_evidence_id)
                for item in occurrence_run.occurrences
            )
        )
        # Defence in depth: the underlying objects are already cross-validated,
        # but reassert the exact bindings the coverage projector relies upon.
        if {item.occurrence_id for item in run.decisions} != {item[0] for item in links}:
            raise ValueError(
                "completed projection decisions must exactly cover the verified occurrences"
            )
        object.__setattr__(self, "projection_run_id", run.projection_run_id)
        object.__setattr__(self, "plan_sha256", run.plan_sha256)
        object.__setattr__(
            self,
            "source_evidence_ids",
            tuple(sorted(occurrence_run.source_evidence_ids)),
        )
        object.__setattr__(self, "occurrence_evidence_links", links)
        object.__setattr__(self, "decisions", run.decisions)
        object.__setattr__(self, "output_manifest_sha256", run.output_manifest_sha256)
        object.__setattr__(
            self,
            "occurrence_coverage_manifest_sha256",
            occurrence_run.coverage_manifest_sha256,
        )
        object.__setattr__(self, "source_occurrence_coverage", coverage)
        object.__setattr__(self, "occurrence_coverage_proofs", proofs)
        object.__setattr__(self, "occurrence_run", occurrence_run)

    @property
    def occurrence_evidence_map(self) -> dict[str, str]:
        return dict(self.occurrence_evidence_links)

    @property
    def occurrence_coverage_map(self) -> dict[str, bool]:
        return {item[0]: item[1] for item in self.source_occurrence_coverage}

    @property
    def has_ambiguity(self) -> bool:
        return any(
            item.state in {
                ProjectionDecisionState.AMBIGUOUS,
                ProjectionDecisionState.ACCOUNT_LEVEL_REVIEW,
            }
            for item in self.decisions
        )

    def manifest_dict(self) -> dict[str, object]:
        return {
            "schema_version": "market_evidence.completed_projection_decisions.v2",
            "projection_run_id": self.projection_run_id,
            "plan_sha256": self.plan_sha256,
            "source_evidence_ids": list(self.source_evidence_ids),
            "occurrence_evidence_links": [
                {"occurrence_id": occurrence_id, "evidence_id": evidence_id}
                for occurrence_id, evidence_id in self.occurrence_evidence_links
            ],
            "output_manifest_sha256": self.output_manifest_sha256,
            "occurrence_coverage_manifest_sha256": (
                self.occurrence_coverage_manifest_sha256
            ),
            "source_occurrence_coverage": [
                {
                    "coverage_evidence_id": evidence_id,
                    "complete": complete,
                    "coverage_proof_sha256": proof_sha256,
                }
                for evidence_id, complete, proof_sha256
                in self.source_occurrence_coverage
            ],
        }


def _authoritative_items_and_matches(
    requirement: CoverageRequirement,
    items: tuple[ProjectionEvidenceItem, ...],
    runs: tuple["CompletedProjectionDecisions", ...],
) -> tuple[
    tuple[ProjectionEvidenceItem, ...],
    tuple[ValidatedProjectionMatch, ...],
    tuple[str, ...],
]:
    """Derive authoritative items, matches, and provenance run IDs from runs.

    Projection completion, occurrence-coverage completeness, ambiguity, and the
    match set are taken exclusively from the completed runs; a caller cannot
    fabricate an evidence item's ``projected`` or ``occurrence_coverage_complete``
    flags to earn a negative.  A missing or overlapping run leaves the affected
    evidence unprojected, which yields ``unknown`` unless another eligible item
    establishes an appearance.
    """

    required = set(requirement.required_evidence_ids)
    if any(run.plan_sha256 != requirement.plan_sha256 for run in runs):
        raise ValueError("completed projection run belongs to another demand plan")
    if any(not set(run.source_evidence_ids).issubset(required) for run in runs):
        raise ValueError(
            "completed projection run contains evidence outside the coverage cell"
        )

    runs_by_evidence: dict[str, list["CompletedProjectionDecisions"]] = {}
    for run in runs:
        for evidence_id in run.source_evidence_ids:
            runs_by_evidence.setdefault(evidence_id, []).append(run)

    authoritative_items: list[ProjectionEvidenceItem] = []
    authoritative_matches: list[ValidatedProjectionMatch] = []
    for item in items:
        covering = runs_by_evidence.get(item.evidence_id, [])
        derived_ambiguity = len(covering) > 1
        derived_coverage_complete = False
        if len(covering) == 1:
            run = covering[0]
            derived_coverage_complete = run.occurrence_coverage_map.get(
                item.evidence_id, False
            )
            occurrence_map = run.occurrence_evidence_map
            relevant = tuple(
                decision
                for decision in run.decisions
                if occurrence_map[decision.occurrence_id] == item.evidence_id
            )
            derived_ambiguity = any(
                decision.state in {
                    ProjectionDecisionState.AMBIGUOUS,
                    ProjectionDecisionState.ACCOUNT_LEVEL_REVIEW,
                }
                for decision in relevant
            )
            for decision in relevant:
                authoritative_matches.extend(
                    ValidatedProjectionMatch(
                        source_evidence_id=item.evidence_id,
                        target_kind=match.target_kind,
                        target_id=match.target_id,
                        validated=match.validated,
                        ambiguous=match.ambiguous,
                    )
                    for match in decision.matches
                )
        authoritative_items.append(
            replace(
                item,
                projected=len(covering) == 1,
                occurrence_coverage_complete=derived_coverage_complete,
                ambiguous=item.ambiguous or derived_ambiguity,
            )
        )
    run_ids = tuple(sorted(run.projection_run_id for run in runs))
    return tuple(authoritative_items), tuple(authoritative_matches), run_ids


def project_completed_projection_decisions(
    requirement: CoverageRequirement,
    evidence_items: Iterable[ProjectionEvidenceItem],
    target: ProjectionTarget,
    completed_runs: Iterable[CompletedProjectionDecisions],
) -> ProjectionResult:
    """Authoritatively carry completed decision state into coverage outcome.

    Projection completion and ambiguity are derived from exact completed runs;
    callers cannot turn an ambiguous run into nonappearance by supplying an
    empty match collection.  A missing or overlapping run marks the affected
    evidence unprojected and therefore produces ``unknown`` unless another
    eligible item positively establishes appearance.  Negative authority is
    granted only because genuine :class:`CompletedProjectionDecisions` are
    passed here and structurally carried on the resulting projection.
    """

    if not isinstance(requirement, CoverageRequirement):
        raise TypeError("requirement must be a CoverageRequirement")
    if not isinstance(target, ProjectionTarget):
        raise TypeError("target must be a ProjectionTarget")
    items = tuple(evidence_items)
    if any(not isinstance(item, ProjectionEvidenceItem) for item in items):
        raise TypeError("evidence_items must contain ProjectionEvidenceItem values")
    runs = tuple(completed_runs)
    if any(not isinstance(item, CompletedProjectionDecisions) for item in runs):
        raise TypeError("completed_runs must contain CompletedProjectionDecisions")
    return ProjectionResult(
        requirement=requirement,
        target=target,
        evidence_items=items,
        candidate_matches=(),
        completed_runs=runs,
    )


def _derive_projection_decisions(
    classification: ValidatedMarketClassification,
    identity_release: ProjectionIdentityRelease,
) -> tuple[ImmutableProjectionDecision, ...]:
    """Deterministically map a validated classification to projection decisions.

    This is the single authority for decision state.  It is called by
    :class:`ImmutableProjectionRun`'s constructor so that decisions are never a
    caller input: a clinic-name appearance with a matching branch alias must
    surface as a match, and no caller can relabel it as an unmatched negative.
    """

    aliases = {item.literal_name: item.targets for item in identity_release.aliases}
    decisions: list[ImmutableProjectionDecision] = []
    for result in classification.results:
        targets = aliases.get(result.literal_name, ())
        matches: tuple[ValidatedProjectionMatch, ...] = ()
        if result.classification is MarketClassification.AMBIGUOUS:
            state = ProjectionDecisionState.AMBIGUOUS
        elif result.classification is not MarketClassification.CLINIC_NAME or not targets:
            state = ProjectionDecisionState.UNMATCHED
        elif any(item.kind is ProjectionTargetKind.ENTITY for item in targets):
            state = ProjectionDecisionState.ACCOUNT_LEVEL_REVIEW
            matches = tuple(
                ValidatedProjectionMatch(
                    source_evidence_id=result.occurrence_id,
                    target_kind=item.kind,
                    target_id=item.target_id,
                    validated=True,
                    ambiguous=True,
                )
                for item in targets
            )
        elif len(targets) != 1:
            state = ProjectionDecisionState.AMBIGUOUS
            matches = tuple(
                ValidatedProjectionMatch(
                    source_evidence_id=result.occurrence_id,
                    target_kind=item.kind,
                    target_id=item.target_id,
                    validated=True,
                    ambiguous=True,
                )
                for item in targets
            )
        else:
            state = ProjectionDecisionState.MATCHED
            only_target = targets[0]
            matches = (
                ValidatedProjectionMatch(
                    source_evidence_id=result.occurrence_id,
                    target_kind=only_target.kind,
                    target_id=only_target.target_id,
                ),
            )
        decisions.append(
            ImmutableProjectionDecision(
                occurrence_id=result.occurrence_id,
                state=state,
                matches=matches,
            )
        )
    return tuple(sorted(decisions, key=lambda item: item.occurrence_id))


def build_immutable_projection_run(
    classification: ValidatedMarketClassification,
    identity_release: ProjectionIdentityRelease,
    *,
    plan_sha256: str,
    treatment_catalogue_release_id: str,
    treatment_catalogue_payload_sha256: str,
    projection_algorithm_version: str = "market-identity-projection-v1",
) -> ImmutableProjectionRun:
    """Reproject completed classification without queueing collection.

    Decision state is re-derived inside :class:`ImmutableProjectionRun`; this
    helper only assembles the run from its validated inputs.
    """

    if not isinstance(classification, ValidatedMarketClassification):
        raise TypeError("classification must be a ValidatedMarketClassification")
    if not isinstance(identity_release, ProjectionIdentityRelease):
        raise TypeError("identity_release must be a ProjectionIdentityRelease")
    return ImmutableProjectionRun(
        classification=classification,
        identity_release=identity_release,
        plan_sha256=plan_sha256,
        treatment_catalogue_release_id=treatment_catalogue_release_id,
        treatment_catalogue_payload_sha256=treatment_catalogue_payload_sha256,
        projection_algorithm_version=projection_algorithm_version,
    )


__all__ = [
    "MARKET_COVERAGE_REQUIREMENT_SCHEMA",
    "MARKET_PROJECTION_ALGORITHM_VERSION",
    "MARKET_PROJECTION_SCHEMA",
    "IMMUTABLE_PROJECTION_OUTPUT_SCHEMA",
    "IMMUTABLE_PROJECTION_RUN_SCHEMA",
    "CoverageWarningCode",
    "CoverageRequirement",
    "CompletedProjectionDecisions",
    "ProjectionEvidenceItem",
    "ProjectionOutcome",
    "ProjectionAlias",
    "ProjectionDecisionState",
    "ProjectionIdentityRelease",
    "ImmutableProjectionDecision",
    "ImmutableProjectionRun",
    "ProjectionResult",
    "ProjectionState",
    "ProjectionTarget",
    "ProjectionTargetKind",
    "ValidatedProjectionMatch",
    "project_cell",
    "project_coverage_cell",
    "project_visibility",
    "project_completed_projection_decisions",
    "build_immutable_projection_run",
]
