"""Pure eligibility and freshness selection for Market Evidence windows.

The selector deliberately works on immutable values containing the bytes that
were read for each selected artifact.  Persistence code may load those bytes,
but it cannot replace the hash check performed here with a caller-supplied
``valid`` flag.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

from clinic_geo.eligibility_contracts import EligibilityStatus
from clinic_geo.evidence.purpose import EvidencePurpose, validate_evidence_purpose
from clinic_geo.market_evidence.contracts import (
    MarketMeasurementSpecification,
    ProfileRunAssignment,
    canonical_profile_assignments,
    profile_assignment_plan_sha256,
)


MARKET_ELIGIBILITY_SCHEMA = "market_evidence.eligibility_decision.v1"
MARKET_WINDOW_ELIGIBILITY_SCHEMA = "market_evidence.window_eligibility.v1"
MARKET_ELIGIBILITY_PLAN_BINDING_SCHEMA = (
    "market_evidence.eligibility_plan_binding.v1"
)
MARKET_DISCOVERY_FRESHNESS_POLICY_VERSION = "market-discovery-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_REQUIRED_RUN_ORDINALS = frozenset((1, 2, 3))


class EvidenceArtifactKind(str, Enum):
    """The closed artifact roles understood by the pure selector."""

    RAW_ANSWER = "raw_answer"
    OCCURRENCE_EXTRACTION = "occurrence_extraction"
    CLASSIFICATION = "classification"
    PROJECTION = "projection"


ELIGIBILITY_STATUSES = tuple(item.value for item in EligibilityStatus)


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise ValueError(f"{field_name} cannot contain surrounding whitespace")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _require_bool(value: bool, field_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool")


def _artifact_kind(value: EvidenceArtifactKind | str) -> EvidenceArtifactKind:
    if isinstance(value, EvidenceArtifactKind):
        return value
    if isinstance(value, str):
        try:
            return EvidenceArtifactKind(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in EvidenceArtifactKind)
            raise ValueError(f"artifact_kind must be one of {allowed}") from exc
    raise TypeError("artifact_kind must be an EvidenceArtifactKind or string")


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _parse_duration(value: str) -> timedelta | None:
    """Parse the closed ISO-8601 subset used by run-protocol span policies."""

    match = _DURATION_RE.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        return None
    return timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=float(match.group("seconds") or 0),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FreshnessPolicy:
    """A purpose-bound maximum evidence age selected by the consumer."""

    maximum_age: timedelta
    policy_version: str = MARKET_DISCOVERY_FRESHNESS_POLICY_VERSION
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        if not isinstance(self.maximum_age, timedelta):
            raise TypeError("maximum_age must be a timedelta")
        if self.maximum_age <= timedelta(0):
            raise ValueError("maximum_age must be positive")
        object.__setattr__(
            self, "policy_version", _require_text(self.policy_version, "policy_version")
        )
        object.__setattr__(self, "purpose", validate_evidence_purpose(self.purpose))


DEFAULT_MARKET_DISCOVERY_FRESHNESS_POLICY = FreshnessPolicy(
    maximum_age=timedelta(days=7)
)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceArtifact:
    """Exact artifact bytes and their immutable Market Evidence bindings."""

    artifact_id: str
    artifact_kind: EvidenceArtifactKind
    content: bytes = field(repr=False)
    artifact_sha256: str
    measurement_spec_sha256: str
    window_id: str
    run_ordinal: int
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _require_text(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "artifact_kind", _artifact_kind(self.artifact_kind))
        if not isinstance(self.content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes-like")
        object.__setattr__(self, "content", bytes(self.content))
        if not self.content:
            raise ValueError("artifact content must be non-empty")
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        object.__setattr__(self, "window_id", _require_text(self.window_id, "window_id"))
        if isinstance(self.run_ordinal, bool) or self.run_ordinal not in _REQUIRED_RUN_ORDINALS:
            raise ValueError("run_ordinal must be exactly 1, 2, or 3")
        object.__setattr__(self, "purpose", validate_evidence_purpose(self.purpose))

    @property
    def actual_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def hash_valid(self) -> bool:
        return hmac.compare_digest(self.artifact_sha256, self.actual_sha256)


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptedRun:
    """One accepted logical observation within one immutable window."""

    run_ordinal: int
    accepted_at: datetime
    window_id: str
    measurement_spec_sha256: str
    planned_profile_id: str
    planned_profile_revision: str
    artifacts: tuple[EvidenceArtifact, ...]
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    validation_complete: bool = True
    occurrence_extraction_complete: bool = True
    classification_complete: bool = True
    projection_complete: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.run_ordinal, bool) or self.run_ordinal not in _REQUIRED_RUN_ORDINALS:
            raise ValueError("run_ordinal must be exactly 1, 2, or 3")
        object.__setattr__(self, "accepted_at", _aware_utc(self.accepted_at, "accepted_at"))
        object.__setattr__(self, "window_id", _require_text(self.window_id, "window_id"))
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        object.__setattr__(
            self,
            "planned_profile_id",
            _require_text(self.planned_profile_id, "planned_profile_id"),
        )
        object.__setattr__(
            self,
            "planned_profile_revision",
            _require_text(
                self.planned_profile_revision, "planned_profile_revision"
            ),
        )
        artifacts = tuple(self.artifacts)
        if any(not isinstance(item, EvidenceArtifact) for item in artifacts):
            raise TypeError("artifacts must contain only EvidenceArtifact values")
        object.__setattr__(
            self,
            "artifacts",
            tuple(
                sorted(
                    artifacts,
                    key=lambda item: (
                        item.artifact_id,
                        item.artifact_kind.value,
                        item.artifact_sha256,
                        item.content,
                    ),
                )
            ),
        )
        object.__setattr__(self, "purpose", validate_evidence_purpose(self.purpose))
        for field_name in (
            "validation_complete",
            "occurrence_extraction_complete",
            "classification_complete",
            "projection_complete",
        ):
            _require_bool(getattr(self, field_name), field_name)

    @property
    def processing_complete(self) -> bool:
        return (
            self.validation_complete
            and self.occurrence_extraction_complete
            and self.classification_complete
            and self.projection_complete
        )


def _run_artifact_manifest_valid(run: AcceptedRun) -> bool:
    kinds = tuple(artifact.artifact_kind for artifact in run.artifacts)
    return (
        kinds.count(EvidenceArtifactKind.RAW_ANSWER) == 1
        and len(kinds) == len(set(kinds))
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibilityWindow:
    """One dated acquisition episode; runs never leave this boundary."""

    window_id: str
    specification: MarketMeasurementSpecification
    measurement_spec_sha256: str
    opened_at: datetime
    accepted_runs: tuple[AcceptedRun, ...]
    profile_assignments: tuple[ProfileRunAssignment, ...]
    finalized: bool
    terminal_failed: bool = False
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", _require_text(self.window_id, "window_id"))
        if not isinstance(self.specification, MarketMeasurementSpecification):
            raise TypeError("specification must be a MarketMeasurementSpecification")
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        object.__setattr__(self, "opened_at", _aware_utc(self.opened_at, "opened_at"))
        runs = tuple(self.accepted_runs)
        if any(not isinstance(item, AcceptedRun) for item in runs):
            raise TypeError("accepted_runs must contain only AcceptedRun values")
        object.__setattr__(
            self,
            "accepted_runs",
            tuple(
                sorted(
                    runs,
                    key=lambda item: (
                        item.run_ordinal,
                        item.accepted_at,
                        tuple(
                            (artifact.artifact_id, artifact.artifact_sha256)
                            for artifact in item.artifacts
                        ),
                    ),
                )
            ),
        )
        assignments = canonical_profile_assignments(tuple(self.profile_assignments))
        assignment_hash = profile_assignment_plan_sha256(assignments)
        if assignment_hash != self.specification.collection_regime.profile_ordering_sha256:
            raise ValueError(
                "profile assignment plan hash does not match the measurement specification"
            )
        object.__setattr__(self, "profile_assignments", assignments)
        _require_bool(self.finalized, "finalized")
        _require_bool(self.terminal_failed, "terminal_failed")
        if self.finalized and self.terminal_failed:
            raise ValueError("a window cannot be both finalized and terminally failed")
        object.__setattr__(self, "purpose", validate_evidence_purpose(self.purpose))


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowEligibility:
    """The deterministic eligibility state of one candidate window."""

    window: EligibilityWindow
    status: EligibilityStatus
    purpose: EvidencePurpose
    measurement_spec_sha256: str
    evaluated_at: datetime
    freshness_policy: FreshnessPolicy
    compatible: bool
    specification_hash_valid: bool
    artifact_hashes_valid: bool
    chronology_valid: bool
    coherent: bool
    complete: bool
    fresh: bool
    freshness_started_at: datetime | None
    freshness_age: timedelta | None
    expires_at: datetime | None
    reasons: tuple[str, ...]
    schema_version: str = field(default=MARKET_WINDOW_ELIGIBILITY_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.window, EligibilityWindow):
            raise TypeError("window must be an EligibilityWindow")
        if not isinstance(self.status, EligibilityStatus):
            raise TypeError("status must be an EligibilityStatus")
        purpose = validate_evidence_purpose(self.purpose)
        if purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError("eligibility output purpose must be market_discovery")
        object.__setattr__(self, "purpose", purpose)
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        object.__setattr__(
            self, "evaluated_at", _aware_utc(self.evaluated_at, "evaluated_at")
        )
        if not isinstance(self.freshness_policy, FreshnessPolicy):
            raise TypeError("freshness_policy must be a FreshnessPolicy")
        if self.freshness_policy.purpose is not purpose:
            raise ValueError("freshness policy purpose does not match window output")
        for field_name in (
            "compatible",
            "specification_hash_valid",
            "artifact_hashes_valid",
            "chronology_valid",
            "coherent",
            "complete",
            "fresh",
        ):
            _require_bool(getattr(self, field_name), field_name)
        temporal = (
            self.freshness_started_at,
            self.freshness_age,
            self.expires_at,
        )
        if any(item is None for item in temporal) and not all(
            item is None for item in temporal
        ):
            raise ValueError("freshness timestamps and age must be supplied together")
        if bool(self.window.accepted_runs) != bool(
            self.freshness_started_at is not None
        ):
            raise ValueError("accepted runs and freshness fields must be supplied together")
        if self.freshness_started_at is not None:
            object.__setattr__(
                self,
                "freshness_started_at",
                _aware_utc(self.freshness_started_at, "freshness_started_at"),
            )
            if not isinstance(self.freshness_age, timedelta):
                raise TypeError("freshness_age must be a timedelta")
            object.__setattr__(
                self, "expires_at", _aware_utc(self.expires_at, "expires_at")
            )
            if self.expires_at != (
                self.freshness_started_at + self.freshness_policy.maximum_age
            ):
                raise ValueError("expires_at does not match the freshness policy")
            if self.freshness_age != self.evaluated_at - self.freshness_started_at:
                raise ValueError("freshness_age does not match evaluated_at")
        reasons = tuple(self.reasons)
        if any(not isinstance(item, str) or not item for item in reasons):
            raise ValueError("reasons must contain non-empty text")
        if len(reasons) != len(set(reasons)):
            raise ValueError("reasons must be unique")
        object.__setattr__(self, "reasons", reasons)

        candidate_hash = self.window.specification.measurement_spec_sha256
        expected_specification_hash_valid = hmac.compare_digest(
            self.window.measurement_spec_sha256, candidate_hash
        )
        if self.specification_hash_valid is not expected_specification_hash_valid:
            raise ValueError("specification_hash_valid does not match window bytes")
        artifact_ids = [
            artifact.artifact_id
            for run in self.window.accepted_runs
            for artifact in run.artifacts
        ]
        expected_artifact_hashes_valid = (
            all(
                _run_artifact_manifest_valid(run)
                for run in self.window.accepted_runs
            )
            and all(
                artifact.hash_valid
                for run in self.window.accepted_runs
                for artifact in run.artifacts
            )
            and len(artifact_ids) == len(set(artifact_ids))
        )
        if self.artifact_hashes_valid is not expected_artifact_hashes_valid:
            raise ValueError("artifact_hashes_valid does not match selected bytes")
        expected_bindings = (
            self.window.purpose is self.purpose
            and all(
                run.window_id == self.window.window_id
                and run.purpose is self.purpose
                and run.measurement_spec_sha256 == self.measurement_spec_sha256
                and run.planned_profile_id
                == self.window.profile_assignments[
                    run.run_ordinal - 1
                ].planned_profile_id
                and run.planned_profile_revision
                == self.window.profile_assignments[
                    run.run_ordinal - 1
                ].planned_profile_revision
                and all(
                    artifact.window_id == self.window.window_id
                    and artifact.run_ordinal == run.run_ordinal
                    and artifact.purpose is self.purpose
                    and artifact.measurement_spec_sha256
                    == self.measurement_spec_sha256
                    for artifact in run.artifacts
                )
                for run in self.window.accepted_runs
            )
        )
        expected_compatible = (
            expected_specification_hash_valid
            and hmac.compare_digest(candidate_hash, self.measurement_spec_sha256)
            and expected_bindings
        )
        if self.compatible is not expected_compatible:
            raise ValueError("compatible does not match exact purpose/specification bindings")

        ordinals = [run.run_ordinal for run in self.window.accepted_runs]
        exact_runs = (
            len(ordinals) == 3 and frozenset(ordinals) == _REQUIRED_RUN_ORDINALS
        )
        if self.window.accepted_runs:
            accepted_times = [run.accepted_at for run in self.window.accepted_runs]
            if self.freshness_started_at != min(accepted_times):
                raise ValueError("freshness_started_at must be the oldest accepted run")
            expected_chronology_valid = (
                self.window.opened_at <= self.evaluated_at
                and all(
                    self.window.opened_at <= item <= self.evaluated_at
                    for item in accepted_times
                )
            )
            maximum_span = _parse_duration(
                self.window.specification.run_protocol.maximum_window_span_policy
            )
            within_span = bool(
                maximum_span is not None
                and max(accepted_times) - min(accepted_times) <= maximum_span
            )
        else:
            expected_chronology_valid = self.window.opened_at <= self.evaluated_at
            within_span = False
        if self.chronology_valid is not expected_chronology_valid:
            raise ValueError("chronology_valid does not match evaluated_at")
        expected_coherent = exact_runs and expected_chronology_valid and within_span
        if self.coherent is not expected_coherent:
            raise ValueError("coherent does not match the accepted three-run window")
        expected_complete = (
            expected_compatible
            and expected_coherent
            and self.window.finalized
            and not self.window.terminal_failed
            and expected_artifact_hashes_valid
            and all(run.processing_complete for run in self.window.accepted_runs)
        )
        if self.complete is not expected_complete:
            raise ValueError("complete does not match the validated window")
        expected_fresh = bool(
            self.complete
            and self.freshness_age is not None
            and timedelta(0)
            <= self.freshness_age
            <= self.freshness_policy.maximum_age
        )
        if self.fresh is not expected_fresh:
            raise ValueError("fresh does not match freshness age and policy")
        distinct_partial = (
            len(ordinals) == len(set(ordinals))
            and 0 < len(set(ordinals) & _REQUIRED_RUN_ORDINALS) < 3
        )
        if not self.compatible:
            expected_status = EligibilityStatus.INCOMPATIBLE
        elif not self.chronology_valid:
            expected_status = EligibilityStatus.INCOMPLETE
        elif self.window.terminal_failed:
            expected_status = EligibilityStatus.FAILED
        elif not self.artifact_hashes_valid:
            expected_status = EligibilityStatus.INCOMPLETE
        elif not exact_runs:
            expected_status = (
                EligibilityStatus.PARTIAL
                if distinct_partial
                else EligibilityStatus.INCOMPLETE
            )
        elif not self.complete:
            expected_status = EligibilityStatus.INCOMPLETE
        elif self.fresh:
            expected_status = EligibilityStatus.HIT
        else:
            expected_status = EligibilityStatus.STALE
        allowed_statuses = (
            {EligibilityStatus.HIT, EligibilityStatus.SUPERSEDED}
            if expected_status is EligibilityStatus.HIT
            else {expected_status}
        )
        if self.status not in allowed_statuses:
            raise ValueError("window status does not match validated evidence state")
        if self.status is EligibilityStatus.MISS:
            raise ValueError("miss is an overall selector status, not a window status")
        if self.status is EligibilityStatus.INCOMPATIBLE and self.compatible:
            raise ValueError("incompatible output cannot be exact-compatible")
        if not self.compatible and self.status is not EligibilityStatus.INCOMPATIBLE:
            raise ValueError("a non-compatible window must be incompatible")
        if self.status is EligibilityStatus.FAILED and not self.window.terminal_failed:
            raise ValueError("failed output requires a terminally failed window")
        if self.status is EligibilityStatus.PARTIAL and not (
            self.compatible
            and not self.complete
            and len(ordinals) == len(set(ordinals))
            and 0 < len(set(ordinals) & _REQUIRED_RUN_ORDINALS) < 3
        ):
            raise ValueError("partial output requires one or two accepted runs")
        if self.status is EligibilityStatus.HIT and not (
            self.compatible
            and self.complete
            and self.fresh
            and self.coherent
            and self.specification_hash_valid
            and self.artifact_hashes_valid
        ):
            raise ValueError("hit output must be compatible, complete, fresh, and valid")
        if self.status is EligibilityStatus.SUPERSEDED and not (
            self.compatible and self.complete and self.fresh
        ):
            raise ValueError("superseded output must remain complete and fresh")
        if self.status is EligibilityStatus.STALE and (
            not self.complete or self.fresh
        ):
            raise ValueError("stale output must be complete but not fresh")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "window_id": self.window.window_id,
            "window": {
                "opened_at": _isoformat(self.window.opened_at),
                "finalized": self.window.finalized,
                "terminal_failed": self.window.terminal_failed,
                "recorded_measurement_spec_sha256": (
                    self.window.measurement_spec_sha256
                ),
                "profile_assignment_plan_sha256": (
                    profile_assignment_plan_sha256(
                        self.window.profile_assignments
                    )
                ),
                "accepted_runs": [
                    {
                        "run_ordinal": run.run_ordinal,
                        "accepted_at": _isoformat(run.accepted_at),
                        "planned_profile_id": run.planned_profile_id,
                        "planned_profile_revision": run.planned_profile_revision,
                        "validation_complete": run.validation_complete,
                        "occurrence_extraction_complete": (
                            run.occurrence_extraction_complete
                        ),
                        "classification_complete": run.classification_complete,
                        "projection_complete": run.projection_complete,
                        "artifacts": [
                            {
                                "artifact_id": artifact.artifact_id,
                                "artifact_kind": artifact.artifact_kind.value,
                                "recorded_sha256": artifact.artifact_sha256,
                                "actual_sha256": artifact.actual_sha256,
                            }
                            for artifact in run.artifacts
                        ],
                    }
                    for run in self.window.accepted_runs
                ],
            },
            "status": self.status.value,
            "purpose": self.purpose.value,
            "window_purpose": self.window.purpose.value,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "evaluated_at": _isoformat(self.evaluated_at),
            "freshness_policy_version": self.freshness_policy.policy_version,
            "compatible": self.compatible,
            "specification_hash_valid": self.specification_hash_valid,
            "artifact_hashes_valid": self.artifact_hashes_valid,
            "chronology_valid": self.chronology_valid,
            "coherent": self.coherent,
            "complete": self.complete,
            "fresh": self.fresh,
            "freshness_started_at": _isoformat(self.freshness_started_at),
            "freshness_age_seconds": (
                self.freshness_age.total_seconds() if self.freshness_age is not None else None
            ),
            "expires_at": _isoformat(self.expires_at),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibilityDecision:
    """Overall exact-compatible selection and its auditable window decisions."""

    status: EligibilityStatus
    purpose: EvidencePurpose
    measurement_spec_sha256: str
    evaluated_at: datetime
    freshness_policy: FreshnessPolicy
    selected_window: EligibilityWindow | None
    refresh_failed: bool
    windows: tuple[WindowEligibility, ...]
    schema_version: str = field(default=MARKET_ELIGIBILITY_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, EligibilityStatus):
            raise TypeError("status must be an EligibilityStatus")
        if self.status is EligibilityStatus.SUPERSEDED:
            raise ValueError("superseded is a window status, not an overall selection")
        purpose = validate_evidence_purpose(self.purpose)
        if purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError("eligibility decision purpose must be market_discovery")
        object.__setattr__(self, "purpose", purpose)
        _require_sha256(self.measurement_spec_sha256, "measurement_spec_sha256")
        object.__setattr__(
            self, "evaluated_at", _aware_utc(self.evaluated_at, "evaluated_at")
        )
        if not isinstance(self.freshness_policy, FreshnessPolicy):
            raise TypeError("freshness_policy must be a FreshnessPolicy")
        if self.freshness_policy.purpose is not purpose:
            raise ValueError("freshness policy purpose does not match decision purpose")
        if self.selected_window is not None and not isinstance(
            self.selected_window, EligibilityWindow
        ):
            raise TypeError("selected_window must be an EligibilityWindow or None")
        _require_bool(self.refresh_failed, "refresh_failed")
        windows = tuple(self.windows)
        if any(not isinstance(item, WindowEligibility) for item in windows):
            raise TypeError("windows must contain only WindowEligibility values")
        if any(
            item.purpose is not purpose
            or item.measurement_spec_sha256 != self.measurement_spec_sha256
            or item.evaluated_at != self.evaluated_at
            or item.freshness_policy != self.freshness_policy
            for item in windows
        ):
            raise ValueError(
                "window outputs must match the decision purpose, specification, and policy"
            )
        window_ids = [item.window.window_id for item in windows]
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("window outputs must have unique window_id values")
        ordered = tuple(
            sorted(windows, key=lambda item: _window_sort_key(item.window), reverse=True)
        )
        object.__setattr__(self, "windows", ordered)
        eligible_states: list[WindowEligibility] = []
        for item in ordered:
            runs = item.window.accepted_runs
            if runs:
                accepted_times = [run.accepted_at for run in runs]
                expected_start = min(accepted_times)
                expected_age = self.evaluated_at - expected_start
                expected_expires = (
                    expected_start + self.freshness_policy.maximum_age
                )
                if (
                    item.freshness_started_at != expected_start
                    or item.freshness_age != expected_age
                    or item.expires_at != expected_expires
                ):
                    raise ValueError(
                        "window freshness fields do not match evaluated_at and policy"
                    )
            elif any(
                value is not None
                for value in (
                    item.freshness_started_at,
                    item.freshness_age,
                    item.expires_at,
                )
            ):
                raise ValueError("a window without accepted runs has no freshness age")
            expected_fresh = bool(
                item.complete
                and item.freshness_age is not None
                and timedelta(0)
                <= item.freshness_age
                <= self.freshness_policy.maximum_age
            )
            if item.fresh is not expected_fresh:
                raise ValueError("fresh does not match evaluated_at and policy")

            ordinals = [run.run_ordinal for run in runs]
            exact_runs = (
                len(ordinals) == 3
                and frozenset(ordinals) == _REQUIRED_RUN_ORDINALS
            )
            chronology_valid = item.window.opened_at <= self.evaluated_at and all(
                item.window.opened_at <= run.accepted_at <= self.evaluated_at
                for run in runs
            )
            if item.chronology_valid is not chronology_valid:
                raise ValueError("window chronology does not match evaluated_at")
            distinct_partial = (
                len(ordinals) == len(set(ordinals))
                and 0 < len(set(ordinals) & _REQUIRED_RUN_ORDINALS) < 3
            )
            if not item.compatible:
                expected_status = EligibilityStatus.INCOMPATIBLE
            elif not chronology_valid:
                expected_status = EligibilityStatus.INCOMPLETE
            elif item.window.terminal_failed:
                expected_status = EligibilityStatus.FAILED
            elif not item.artifact_hashes_valid:
                expected_status = EligibilityStatus.INCOMPLETE
            elif not exact_runs:
                expected_status = (
                    EligibilityStatus.PARTIAL
                    if distinct_partial
                    else EligibilityStatus.INCOMPLETE
                )
            elif not item.complete:
                expected_status = EligibilityStatus.INCOMPLETE
            elif expected_fresh:
                expected_status = EligibilityStatus.HIT
            else:
                expected_status = EligibilityStatus.STALE
            if expected_status is EligibilityStatus.HIT:
                if item.status not in {
                    EligibilityStatus.HIT,
                    EligibilityStatus.SUPERSEDED,
                }:
                    raise ValueError("eligible window must be hit or superseded")
                eligible_states.append(item)
            elif item.status is not expected_status:
                raise ValueError("window status does not match validated evidence state")
        if self.status is EligibilityStatus.MISS:
            if self.selected_window is not None or ordered:
                raise ValueError("miss output cannot select or contain a candidate window")
            if self.refresh_failed:
                raise ValueError("miss output cannot report a failed refresh")
            return
        if self.selected_window is None:
            raise ValueError("non-miss output must select its decisive window")
        matching = [
            item
            for item in ordered
            if item.window.window_id == self.selected_window.window_id
        ]
        if len(matching) != 1 or matching[0].window != self.selected_window:
            raise ValueError("selected_window must be present in window outputs")
        selected = matching[0]
        if selected.status is not self.status:
            raise ValueError("selected window status must match overall decision status")
        if eligible_states:
            expected_selected = max(
                eligible_states, key=lambda item: _window_sort_key(item.window)
            )
            if (
                self.status is not EligibilityStatus.HIT
                or selected is not expected_selected
                or any(
                    item.status is not EligibilityStatus.SUPERSEDED
                    for item in eligible_states
                    if item is not expected_selected
                )
            ):
                raise ValueError("selection does not choose the newest eligible window")
        else:
            exact_visible = [
                item
                for item in ordered
                if item.compatible and item.chronology_valid
            ]
            exact = [item for item in ordered if item.compatible]
            visible = [
                item for item in ordered if item.chronology_valid
            ]
            if exact_visible:
                expected_selected = max(
                    exact_visible, key=lambda item: _window_sort_key(item.window)
                )
            elif exact:
                expected_selected = min(
                    exact, key=lambda item: _window_sort_key(item.window)
                )
            else:
                expected_selected = max(
                    visible or list(ordered),
                    key=lambda item: _window_sort_key(item.window),
                )
            if selected is not expected_selected:
                raise ValueError("selection does not choose the decisive evidence window")
        expected_refresh_failed = bool(
            self.status is EligibilityStatus.HIT
            and any(
                item.compatible
                and item.status is EligibilityStatus.FAILED
                and _window_sort_key(item.window)
                > _window_sort_key(self.selected_window)
                for item in ordered
            )
        )
        if self.refresh_failed is not expected_refresh_failed:
            raise ValueError("refresh_failed does not match newer failed refresh evidence")

    @property
    def selected_window_id(self) -> str | None:
        return self.selected_window.window_id if self.selected_window is not None else None

    @property
    def is_hit(self) -> bool:
        return self.status is EligibilityStatus.HIT

    @property
    def selected_window_evaluation(self) -> WindowEligibility | None:
        if self.selected_window is None:
            return None
        return next(
            item for item in self.windows if item.window.window_id == self.selected_window.window_id
        )

    @property
    def freshness_started_at(self) -> datetime | None:
        selected = self.selected_window_evaluation
        return selected.freshness_started_at if selected is not None else None

    @property
    def freshness_age(self) -> timedelta | None:
        selected = self.selected_window_evaluation
        return selected.freshness_age if selected is not None else None

    @property
    def expires_at(self) -> datetime | None:
        selected = self.selected_window_evaluation
        return selected.expires_at if selected is not None else None

    @property
    def superseded_window_ids(self) -> tuple[str, ...]:
        return tuple(
            item.window.window_id
            for item in self.windows
            if item.status is EligibilityStatus.SUPERSEDED
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "purpose": self.purpose.value,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "evaluated_at": _isoformat(self.evaluated_at),
            "freshness_policy": {
                "policy_version": self.freshness_policy.policy_version,
                "purpose": self.freshness_policy.purpose.value,
                "maximum_age_seconds": self.freshness_policy.maximum_age.total_seconds(),
            },
            "selected_window_id": self.selected_window_id,
            "refresh_failed": self.refresh_failed,
            "superseded_window_ids": list(self.superseded_window_ids),
            "windows": [item.to_canonical_dict() for item in self.windows],
        }

    def to_plan_binding_dict(self) -> dict:
        """Return the material eligibility identity bound into a demand plan.

        ``evaluated_at`` and ``freshness_age_seconds`` are observation-clock
        values: they change continuously even while the selected immutable
        evidence, status, and collection demand remain identical.  Binding
        them into ``plan_sha256`` would make every non-immediate confirmation
        fail solely because time passed.  The stable binding retains the
        freshness policy, start and expiry instants, current ``fresh``/status
        result, every immutable window/run/artifact fact, and all reasons.
        Consequently an expiry crossing or any evidence change still changes
        the exact plan hash.
        """

        payload = self.to_canonical_dict()
        payload["schema_version"] = MARKET_ELIGIBILITY_PLAN_BINDING_SCHEMA
        payload.pop("evaluated_at")
        for window in payload["windows"]:
            window.pop("evaluated_at")
            window.pop("freshness_age_seconds")
        return payload


def _window_sort_key(window: EligibilityWindow) -> tuple[datetime, str]:
    return (window.opened_at, window.window_id)


def _evaluate_window(
    required: MarketMeasurementSpecification,
    window: EligibilityWindow,
    *,
    evaluated_at: datetime,
    freshness_policy: FreshnessPolicy,
) -> WindowEligibility:
    required_hash = required.measurement_spec_sha256
    candidate_hash = window.specification.measurement_spec_sha256
    specification_hash_valid = hmac.compare_digest(
        window.measurement_spec_sha256, candidate_hash
    )
    full_spec_match = (
        required.to_canonical_dict() == window.specification.to_canonical_dict()
        and hmac.compare_digest(required_hash, candidate_hash)
    )

    reasons: list[str] = []
    if not specification_hash_valid:
        reasons.append("measurement_specification_hash_mismatch")
    if not full_spec_match:
        reasons.append("measurement_specification_mismatch")
    if window.purpose is not required.purpose:
        reasons.append("window_purpose_mismatch")

    artifact_ids: list[str] = []
    artifacts_present = True
    artifact_hashes_valid = True
    bindings_valid = True
    processing_complete = True
    chronology_valid = window.opened_at <= evaluated_at
    assignments = {
        item.run_ordinal: item for item in window.profile_assignments
    }
    if not chronology_valid:
        reasons.append("window_opened_after_evaluation")
    for run in window.accepted_runs:
        assignment = assignments[run.run_ordinal]
        if (
            run.planned_profile_id != assignment.planned_profile_id
            or run.planned_profile_revision != assignment.planned_profile_revision
        ):
            bindings_valid = False
            reasons.append(f"run_{run.run_ordinal}_planned_profile_mismatch")
        if run.window_id != window.window_id:
            bindings_valid = False
            reasons.append(f"run_{run.run_ordinal}_window_mismatch")
        if run.purpose is not required.purpose:
            bindings_valid = False
            reasons.append(f"run_{run.run_ordinal}_purpose_mismatch")
        if not hmac.compare_digest(run.measurement_spec_sha256, required_hash):
            bindings_valid = False
            reasons.append(f"run_{run.run_ordinal}_specification_mismatch")
        if run.accepted_at < window.opened_at or run.accepted_at > evaluated_at:
            chronology_valid = False
            reasons.append(f"run_{run.run_ordinal}_timestamp_outside_window")
        artifact_kinds = tuple(
            artifact.artifact_kind for artifact in run.artifacts
        )
        if not run.artifacts:
            artifacts_present = False
            artifact_hashes_valid = False
            reasons.append(f"run_{run.run_ordinal}_artifact_missing")
        if artifact_kinds.count(EvidenceArtifactKind.RAW_ANSWER) != 1:
            artifacts_present = False
            artifact_hashes_valid = False
            reasons.append(f"run_{run.run_ordinal}_raw_answer_artifact_missing")
        if len(artifact_kinds) != len(set(artifact_kinds)):
            artifacts_present = False
            artifact_hashes_valid = False
            reasons.append(f"run_{run.run_ordinal}_duplicate_artifact_kind")
        if not run.processing_complete:
            processing_complete = False
            reasons.append(f"run_{run.run_ordinal}_processing_incomplete")
        for artifact in run.artifacts:
            artifact_ids.append(artifact.artifact_id)
            if artifact.window_id != window.window_id or artifact.run_ordinal != run.run_ordinal:
                bindings_valid = False
                reasons.append(f"artifact_{artifact.artifact_id}_window_run_mismatch")
            if artifact.purpose is not required.purpose:
                bindings_valid = False
                reasons.append(f"artifact_{artifact.artifact_id}_purpose_mismatch")
            if not hmac.compare_digest(artifact.measurement_spec_sha256, required_hash):
                bindings_valid = False
                reasons.append(f"artifact_{artifact.artifact_id}_specification_mismatch")
            if not artifact.hash_valid:
                artifact_hashes_valid = False
                reasons.append(f"artifact_{artifact.artifact_id}_hash_mismatch")

    if len(artifact_ids) != len(set(artifact_ids)):
        artifact_hashes_valid = False
        reasons.append("duplicate_artifact_id")

    compatible = (
        specification_hash_valid
        and full_spec_match
        and window.purpose is required.purpose
        and bindings_valid
    )
    ordinals = [run.run_ordinal for run in window.accepted_runs]
    exact_runs = len(ordinals) == 3 and frozenset(ordinals) == _REQUIRED_RUN_ORDINALS
    if len(ordinals) != len(set(ordinals)):
        reasons.append("duplicate_run_ordinal")
    if not exact_runs:
        reasons.append("required_runs_not_complete")

    freshness_started_at: datetime | None = None
    freshness_age: timedelta | None = None
    expires_at: datetime | None = None
    within_span = False
    if window.accepted_runs:
        accepted_times = [run.accepted_at for run in window.accepted_runs]
        freshness_started_at = min(accepted_times)
        freshness_age = evaluated_at - freshness_started_at
        expires_at = freshness_started_at + freshness_policy.maximum_age
        maximum_span = _parse_duration(
            window.specification.run_protocol.maximum_window_span_policy
        )
        if maximum_span is None:
            reasons.append("unsupported_maximum_window_span_policy")
        else:
            within_span = max(accepted_times) - min(accepted_times) <= maximum_span
            if not within_span:
                reasons.append("coherent_window_span_exceeded")
    else:
        reasons.append("no_accepted_runs")

    coherent = exact_runs and within_span and chronology_valid
    complete = (
        compatible
        and coherent
        and window.finalized
        and not window.terminal_failed
        and artifacts_present
        and artifact_hashes_valid
        and processing_complete
    )
    fresh = bool(
        complete
        and freshness_age is not None
        and timedelta(0) <= freshness_age <= freshness_policy.maximum_age
    )

    if not compatible:
        status = EligibilityStatus.INCOMPATIBLE
    elif not chronology_valid:
        status = EligibilityStatus.INCOMPLETE
    elif window.terminal_failed:
        status = EligibilityStatus.FAILED
        reasons.append("terminal_refresh_failure")
    elif not artifact_hashes_valid:
        status = EligibilityStatus.INCOMPLETE
    elif not exact_runs:
        status = (
            EligibilityStatus.PARTIAL
            if len(ordinals) == len(set(ordinals))
            and 0 < len(set(ordinals) & _REQUIRED_RUN_ORDINALS) < 3
            else EligibilityStatus.INCOMPLETE
        )
    elif not complete:
        status = EligibilityStatus.INCOMPLETE
        if not window.finalized:
            reasons.append("window_not_finalized")
    elif fresh:
        status = EligibilityStatus.HIT
    else:
        status = EligibilityStatus.STALE
        reasons.append("freshness_policy_expired")

    return WindowEligibility(
        window=window,
        status=status,
        purpose=required.purpose,
        measurement_spec_sha256=required_hash,
        evaluated_at=evaluated_at,
        freshness_policy=freshness_policy,
        compatible=compatible,
        specification_hash_valid=specification_hash_valid,
        artifact_hashes_valid=artifact_hashes_valid,
        chronology_valid=chronology_valid,
        coherent=coherent,
        complete=complete,
        fresh=fresh,
        freshness_started_at=freshness_started_at,
        freshness_age=freshness_age,
        expires_at=expires_at,
        reasons=tuple(sorted(set(reasons))),
    )


def evaluate_eligibility(
    required_specification: MarketMeasurementSpecification,
    windows: Iterable[EligibilityWindow],
    *,
    as_of: datetime,
    freshness: FreshnessPolicy | timedelta = DEFAULT_MARKET_DISCOVERY_FRESHNESS_POLICY,
) -> EligibilityDecision:
    """Select the newest eligible exact-compatible window without combining runs.

    Input ordering has no effect.  A newer incompatible candidate never
    outranks an exact-compatible candidate, and a failed refresh cannot hide a
    still-fresh older hit.
    """

    if not isinstance(required_specification, MarketMeasurementSpecification):
        raise TypeError(
            "required_specification must be a MarketMeasurementSpecification"
        )
    evaluated_at = _aware_utc(as_of, "as_of")
    if isinstance(freshness, timedelta):
        freshness_policy = FreshnessPolicy(maximum_age=freshness)
    elif isinstance(freshness, FreshnessPolicy):
        freshness_policy = freshness
    else:
        raise TypeError("freshness must be a FreshnessPolicy or timedelta")
    if freshness_policy.purpose is not required_specification.purpose:
        raise ValueError("freshness policy purpose does not match required evidence purpose")

    candidates = tuple(windows)
    if any(not isinstance(item, EligibilityWindow) for item in candidates):
        raise TypeError("windows must contain only EligibilityWindow values")
    window_ids = [item.window_id for item in candidates]
    if len(window_ids) != len(set(window_ids)):
        raise ValueError("window_id values must be unique")

    evaluations = [
        _evaluate_window(
            required_specification,
            window,
            evaluated_at=evaluated_at,
            freshness_policy=freshness_policy,
        )
        for window in candidates
    ]
    eligible = [item for item in evaluations if item.status is EligibilityStatus.HIT]

    selected: WindowEligibility | None
    refresh_failed = False
    if eligible:
        selected = max(eligible, key=lambda item: _window_sort_key(item.window))
        selected_key = _window_sort_key(selected.window)
        evaluations = [
            replace(item, status=EligibilityStatus.SUPERSEDED)
            if item is not selected and item.status is EligibilityStatus.HIT
            else item
            for item in evaluations
        ]
        refresh_failed = any(
            item.compatible
            and item.status is EligibilityStatus.FAILED
            and _window_sort_key(item.window) > selected_key
            for item in evaluations
        )
        status = EligibilityStatus.HIT
    else:
        exact = [item for item in evaluations if item.compatible]
        visible_exact = [
            item for item in exact if item.chronology_valid
        ]
        if visible_exact:
            selected = max(
                visible_exact, key=lambda item: _window_sort_key(item.window)
            )
            status = selected.status
        elif exact:
            selected = min(exact, key=lambda item: _window_sort_key(item.window))
            status = EligibilityStatus.INCOMPLETE
        elif evaluations:
            visible = [
                item for item in evaluations if item.chronology_valid
            ]
            selected = max(
                visible or evaluations, key=lambda item: _window_sort_key(item.window)
            )
            status = EligibilityStatus.INCOMPATIBLE
        else:
            selected = None
            status = EligibilityStatus.MISS

    ordered = tuple(
        sorted(evaluations, key=lambda item: _window_sort_key(item.window), reverse=True)
    )
    return EligibilityDecision(
        status=status,
        purpose=required_specification.purpose,
        measurement_spec_sha256=required_specification.measurement_spec_sha256,
        evaluated_at=evaluated_at,
        freshness_policy=freshness_policy,
        selected_window=selected.window if selected is not None else None,
        refresh_failed=refresh_failed,
        windows=ordered,
    )


# Descriptive aliases keep call sites readable while retaining one contract.
ArtifactEvidence = EvidenceArtifact
WindowEvidence = EligibilityWindow
evaluate_market_evidence_eligibility = evaluate_eligibility
select_eligible_evidence = evaluate_eligibility


__all__ = [
    "DEFAULT_MARKET_DISCOVERY_FRESHNESS_POLICY",
    "ELIGIBILITY_STATUSES",
    "MARKET_DISCOVERY_FRESHNESS_POLICY_VERSION",
    "MARKET_ELIGIBILITY_SCHEMA",
    "MARKET_ELIGIBILITY_PLAN_BINDING_SCHEMA",
    "MARKET_WINDOW_ELIGIBILITY_SCHEMA",
    "AcceptedRun",
    "ArtifactEvidence",
    "EligibilityDecision",
    "EligibilityStatus",
    "EligibilityWindow",
    "EvidenceArtifact",
    "EvidenceArtifactKind",
    "FreshnessPolicy",
    "WindowEligibility",
    "WindowEvidence",
    "evaluate_eligibility",
    "evaluate_market_evidence_eligibility",
    "select_eligible_evidence",
]
