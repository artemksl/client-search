"""Durable, lease-based Market Evidence collection orchestration.

This module owns operational state transitions, not persistence.  The
``JobStoreProtocol`` methods are deliberately atomic/CAS-shaped so a SQLite
adapter can implement every transition with ``BEGIN IMMEDIATE`` and a guarded
``UPDATE``.  Collectors are injected and split into a safe ``prepare`` phase
and a possibly irreversible ``send`` phase.  Calling ``send`` is therefore the
point after which an exception is ambiguous and can never be retried
automatically.

No browser, HTTP, credential, or external-client implementation is imported
here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from clinic_geo.evidence.purpose import EvidencePurpose
from clinic_geo.integrity import canonical_sha256

from .contracts import CollectionRegime, MarketMeasurementSpecification, PlatformContext


MARKET_JOB_SCHEMA = "market_evidence.collection_job.v1"
MARKET_ATTEMPT_SCHEMA = "market_evidence.observation_attempt.v1"
MARKET_RETRY_POLICY_SCHEMA = "market_evidence.retry_policy.v1"
MARKET_COLLECTION_REQUEST_SCHEMA = "market_evidence.collection_request.v1"

# Retry versions are executable contracts, not caller labels.  The production
# policy follows the existing collector convention.  ``retry-v1`` is retained
# solely for the small two-attempt fake-worker characterization in this module.
_REGISTERED_RETRY_POLICY_PARAMETERS: Mapping[
    str, tuple[int, tuple[int, ...]]
] = MappingProxyType(
    {
        "market-retry-v1": (3, (2, 4)),
        "retry-v1": (2, (5,)),
    }
)


class CollectionJobState(str, Enum):
    """The complete operational state vocabulary."""

    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    TERMINAL_FAILED = "terminal_failed"
    CANCELLED = "cancelled"
    SEND_UNKNOWN = "send_unknown"


# A shorter alias is convenient for callers and tests.
JobState = CollectionJobState


class SendState(str, Enum):
    """Knowledge about whether an irreversible external action occurred."""

    NOT_STARTED = "not_started"
    ATTEMPT_RESERVED = "attempt_reserved"
    STARTED = "send_started"
    ACKNOWLEDGED = "send_acknowledged"
    UNKNOWN = "send_unknown"


class AttemptOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    INCOMPATIBLE = "incompatible"
    SEND_UNKNOWN = "send_unknown"


class SendUnknownResolution(str, Enum):
    """Explicit operator-only resolutions for an ambiguous send."""

    TERMINAL_FAILED = "terminal_failed"


TERMINAL_JOB_STATES = frozenset(
    {
        CollectionJobState.SUCCEEDED,
        CollectionJobState.TERMINAL_FAILED,
        CollectionJobState.CANCELLED,
    }
)
UNSTARTED_JOB_STATES = frozenset(
    {CollectionJobState.QUEUED, CollectionJobState.RETRY_WAIT}
)
CLAIMABLE_JOB_STATES = UNSTARTED_JOB_STATES


class CollectionJobError(RuntimeError):
    """Base class for fail-closed job orchestration errors."""


class LeaseLost(CollectionJobError):
    """The lease token/generation no longer authorizes a mutation."""


class JobConflict(CollectionJobError):
    """Persisted job state conflicts with the requested operation."""


class CollectorContractError(CollectionJobError):
    """An injected collector violated the safe two-phase contract."""


class PreSendCollectionError(CollectionJobError):
    """A known-safe failure raised before ``CollectorProtocol.send``.

    Only this side of the send boundary may be retried automatically.  Unknown
    exceptions raised by ``prepare`` are also treated as safe because the
    protocol forbids that method from sending.
    """

    def __init__(
        self,
        error_code: str,
        *,
        retryable: bool = True,
        message: str | None = None,
    ) -> None:
        self.error_code = _require_text(error_code, "error_code")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be a bool")
        self.retryable = retryable
        super().__init__(message or self.error_code)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise ValueError(f"{field_name} cannot contain surrounding whitespace")
    return value


def _require_non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _job_state(value: CollectionJobState | str) -> CollectionJobState:
    if isinstance(value, CollectionJobState):
        return value
    try:
        return CollectionJobState(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported collection job state") from exc


def _send_state(value: SendState | str) -> SendState:
    if isinstance(value, SendState):
        return value
    try:
        return SendState(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported send state") from exc


@runtime_checkable
class ClockProtocol(Protocol):
    def now(self) -> datetime:
        """Return a timezone-aware wall-clock instant."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """A versioned, explicit retry ceiling and backoff schedule.

    ``backoff_seconds[n - 1]`` is the delay after failed attempt ``n``.
    Consequently a policy with ``max_attempts=3`` contains exactly two delays.
    The final failure has no delay because it becomes terminal.
    """

    policy_version: str
    max_attempts: int
    backoff_seconds: tuple[int, ...]
    schema_version: str = field(default=MARKET_RETRY_POLICY_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if not isinstance(self.backoff_seconds, tuple):
            raise TypeError("backoff_seconds must be a tuple")
        if len(self.backoff_seconds) != self.max_attempts - 1:
            raise ValueError("backoff_seconds must contain max_attempts - 1 values")
        for value in self.backoff_seconds:
            _require_non_negative_integer(value, "backoff_seconds item")
        registered = _REGISTERED_RETRY_POLICY_PARAMETERS.get(self.policy_version)
        if registered is None:
            raise ValueError("retry policy version is not registered")
        if registered != (self.max_attempts, self.backoff_seconds):
            raise ValueError(
                "retry policy parameters do not match the registered version"
            )

    def retry_at(self, *, attempt_number: int, failed_at: datetime) -> datetime:
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
            or attempt_number >= self.max_attempts
        ):
            raise ValueError("attempt_number has no retry under this policy")
        return _utc(failed_at, "failed_at") + timedelta(
            seconds=self.backoff_seconds[attempt_number - 1]
        )

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "max_attempts": self.max_attempts,
            "backoff_seconds": list(self.backoff_seconds),
        }


def retry_policy_for_version(policy_version: str) -> RetryPolicy:
    """Return the closed, immutable policy registered for ``policy_version``."""

    version = _require_text(policy_version, "policy_version")
    registered = _REGISTERED_RETRY_POLICY_PARAMETERS.get(version)
    if registered is None:
        raise ValueError("retry policy version is not registered")
    maximum_attempts, backoff_seconds = registered
    return RetryPolicy(
        policy_version=version,
        max_attempts=maximum_attempts,
        backoff_seconds=backoff_seconds,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeContext:
    """The exact context actually observed by a collector."""

    platform_context: PlatformContext
    collection_regime: CollectionRegime
    profile_id: str
    profile_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.platform_context, PlatformContext):
            raise TypeError("platform_context must be a PlatformContext")
        if not isinstance(self.collection_regime, CollectionRegime):
            raise TypeError("collection_regime must be a CollectionRegime")
        _require_text(self.profile_id, "profile_id")
        _require_text(self.profile_revision, "profile_revision")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "platform_context": self.platform_context.to_canonical_dict(),
            "collection_regime": self.collection_regime.to_canonical_dict(),
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionRequest:
    """A persisted-observation request passed to an injected collector."""

    job_id: str
    logical_observation_id: str
    measurement_specification: MarketMeasurementSpecification
    run_ordinal: int
    planned_profile_id: str
    planned_profile_revision: str
    exact_rendered_prompt: str
    schema_version: str = field(default=MARKET_COLLECTION_REQUEST_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in (
            "job_id",
            "logical_observation_id",
            "planned_profile_id",
            "planned_profile_revision",
            "exact_rendered_prompt",
        ):
            _require_text(getattr(self, name), name)
        if not isinstance(
            self.measurement_specification, MarketMeasurementSpecification
        ):
            raise TypeError(
                "measurement_specification must be a current "
                "MarketMeasurementSpecification"
            )
        if self.measurement_specification.purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError("collection requests require market_discovery purpose")
        if self.run_ordinal not in (1, 2, 3) or isinstance(self.run_ordinal, bool):
            raise ValueError("run_ordinal must be exactly 1, 2, or 3")
        if hashlib.sha256(self.exact_rendered_prompt.encode("utf-8")).hexdigest() != (
            self.measurement_specification.prompt.exact_rendered_prompt_sha256
        ):
            raise ValueError(
                "exact_rendered_prompt does not match the measurement specification"
            )

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())

    @property
    def expected_runtime(self) -> RuntimeContext:
        return RuntimeContext(
            platform_context=self.measurement_specification.platform_context,
            collection_regime=self.measurement_specification.collection_regime,
            profile_id=self.planned_profile_id,
            profile_revision=self.planned_profile_revision,
        )

    def runtime_is_compatible(self, actual: RuntimeContext) -> bool:
        if not isinstance(actual, RuntimeContext):
            return False
        return self.expected_runtime.to_canonical_dict() == actual.to_canonical_dict()

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "job_id": self.job_id,
            "logical_observation_id": self.logical_observation_id,
            "measurement_spec_sha256": (
                self.measurement_specification.measurement_spec_sha256
            ),
            "measurement_specification": (
                self.measurement_specification.to_canonical_dict()
            ),
            "run_ordinal": self.run_ordinal,
            "planned_profile_id": self.planned_profile_id,
            "planned_profile_revision": self.planned_profile_revision,
            "exact_rendered_prompt": self.exact_rendered_prompt,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedCollection:
    """Opaque collector preparation that is guaranteed not to have sent."""

    request_sha256: str
    payload: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.request_sha256, "request_sha256")
        if len(self.request_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_sha256
        ):
            raise ValueError("request_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionResult:
    """A known response returned after the external send completed."""

    content: bytes = field(repr=False)
    media_type: str
    runtime: RuntimeContext
    external_receipt_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes-like")
        content = bytes(self.content)
        if not content:
            raise ValueError("collector result content must be non-empty")
        object.__setattr__(self, "content", content)
        _require_text(self.media_type, "media_type")
        if not isinstance(self.runtime, RuntimeContext):
            raise TypeError("runtime must be a RuntimeContext")
        if self.external_receipt_id is not None:
            _require_text(self.external_receipt_id, "external_receipt_id")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@runtime_checkable
class CollectorProtocol(Protocol):
    """Injected collector with an explicit irreversible-send boundary."""

    def prepare(self, request: CollectionRequest) -> PreparedCollection:
        """Perform only local/safe work; this method must never send."""

    def send(self, prepared: PreparedCollection) -> CollectionResult:
        """Perform at most one external send and return its known response."""


# Descriptive alias retained for callers that prefer the shorter name.
Collector = CollectorProtocol


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptRecord:
    attempt_id: str
    job_id: str
    logical_observation_id: str
    attempt_number: int
    lease_generation: int
    started_at: datetime
    completed_at: datetime
    outcome: AttemptOutcome
    send_state: SendState
    runtime: RuntimeContext | None = None
    error_code: str | None = None
    schema_version: str = field(default=MARKET_ATTEMPT_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in ("attempt_id", "job_id", "logical_observation_id"):
            _require_text(getattr(self, name), name)
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise ValueError("attempt_number must be a positive integer")
        if (
            isinstance(self.lease_generation, bool)
            or not isinstance(self.lease_generation, int)
            or self.lease_generation < 1
        ):
            raise ValueError("lease_generation must be a positive integer")
        started = _utc(self.started_at, "started_at")
        completed = _utc(self.completed_at, "completed_at")
        if completed < started:
            raise ValueError("completed_at cannot precede started_at")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        if not isinstance(self.outcome, AttemptOutcome):
            raise TypeError("outcome must be an AttemptOutcome")
        object.__setattr__(self, "send_state", _send_state(self.send_state))
        if self.runtime is not None and not isinstance(self.runtime, RuntimeContext):
            raise TypeError("runtime must be a RuntimeContext or None")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")

        if self.outcome in {AttemptOutcome.SUCCEEDED, AttemptOutcome.INCOMPATIBLE}:
            if self.send_state is not SendState.ACKNOWLEDGED or self.runtime is None:
                raise ValueError(
                    "known send outcomes require acknowledged send state and runtime"
                )
        elif self.outcome is AttemptOutcome.SEND_UNKNOWN:
            if self.send_state is not SendState.UNKNOWN or self.runtime is not None:
                raise ValueError("send_unknown attempt must have unknown send state")
        elif self.send_state is SendState.NOT_STARTED:
            if self.runtime is not None:
                raise ValueError("pre-send failure cannot have a runtime context")
        elif self.send_state is not SendState.ACKNOWLEDGED:
            raise ValueError("failed attempts must be pre-send or acknowledged")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "job_id": self.job_id,
            "logical_observation_id": self.logical_observation_id,
            "attempt_number": self.attempt_number,
            "lease_generation": self.lease_generation,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "outcome": self.outcome.value,
            "send_state": self.send_state.value,
            "runtime": self.runtime.to_canonical_dict() if self.runtime else None,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionJob:
    job_id: str
    logical_observation_id: str
    confirmation_id: str
    state: CollectionJobState
    attempt_count: int
    max_attempts: int
    backoff_policy_version: str
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None = None
    worker_id: str | None = None
    lease_token: str | None = None
    lease_generation: int = 0
    lease_expires_at: datetime | None = None
    send_state: SendState = SendState.NOT_STARTED
    error_code: str | None = None
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    schema_version: str = field(default=MARKET_JOB_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in ("job_id", "logical_observation_id", "confirmation_id"):
            _require_text(getattr(self, name), name)
        object.__setattr__(self, "state", _job_state(self.state))
        _require_non_negative_integer(self.attempt_count, "attempt_count")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count cannot exceed max_attempts")
        _require_text(self.backoff_policy_version, "backoff_policy_version")
        created = _utc(self.created_at, "created_at")
        updated = _utc(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        if self.next_attempt_at is not None:
            object.__setattr__(
                self, "next_attempt_at", _utc(self.next_attempt_at, "next_attempt_at")
            )
        if self.lease_expires_at is not None:
            object.__setattr__(
                self,
                "lease_expires_at",
                _utc(self.lease_expires_at, "lease_expires_at"),
            )
        _require_non_negative_integer(self.lease_generation, "lease_generation")
        object.__setattr__(self, "send_state", _send_state(self.send_state))
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        if self.purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise ValueError("collection jobs require market_discovery purpose")

        lease_values = (self.worker_id, self.lease_token, self.lease_expires_at)
        if self.state is CollectionJobState.LEASED:
            if any(value is None for value in lease_values):
                raise ValueError("leased jobs require worker, token, and expiry")
            _require_text(self.worker_id, "worker_id")
            _require_text(self.lease_token, "lease_token")
            if self.lease_generation < 1:
                raise ValueError("leased jobs require a positive lease generation")
            if self.next_attempt_at is not None:
                raise ValueError("leased jobs cannot have next_attempt_at")
        elif any(value is not None for value in lease_values):
            raise ValueError("only leased jobs may carry lease fields")

        if self.state is CollectionJobState.RETRY_WAIT:
            if self.next_attempt_at is None:
                raise ValueError("retry_wait jobs require next_attempt_at")
            if self.attempt_count >= self.max_attempts:
                raise ValueError("retry_wait cannot exceed its retry ceiling")
            if self.send_state is not SendState.NOT_STARTED:
                raise ValueError("retry_wait is valid only for safe, known work")
        elif self.next_attempt_at is not None:
            raise ValueError("only retry_wait jobs may have next_attempt_at")

        if self.state is CollectionJobState.SUCCEEDED:
            if self.send_state is not SendState.ACKNOWLEDGED:
                raise ValueError("succeeded jobs require an acknowledged send")
        elif self.state is CollectionJobState.SEND_UNKNOWN:
            if self.send_state is not SendState.UNKNOWN:
                raise ValueError("send_unknown jobs require unknown send state")
        elif self.state is CollectionJobState.QUEUED and (
            self.send_state is not SendState.NOT_STARTED
        ):
            raise ValueError("queued jobs cannot carry a send marker")
        elif self.state is CollectionJobState.CANCELLED and (
            self.send_state is not SendState.NOT_STARTED
        ):
            raise ValueError("cancelled jobs must be proven unstarted")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    @property
    def blocks_campaign_completion(self) -> bool:
        return self.state in {
            CollectionJobState.LEASED,
            CollectionJobState.SEND_UNKNOWN,
        }

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose.value,
            "job_id": self.job_id,
            "logical_observation_id": self.logical_observation_id,
            "confirmation_id": self.confirmation_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "backoff_policy_version": self.backoff_policy_version,
            "next_attempt_at": _iso(self.next_attempt_at),
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "lease_generation": self.lease_generation,
            "lease_expires_at": _iso(self.lease_expires_at),
            "send_state": self.send_state.value,
            "error_code": self.error_code,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryReport:
    recovered_to_queue: int = 0
    recovered_to_retry_wait: int = 0
    moved_to_send_unknown: int = 0
    already_terminal: int = 0

    def __post_init__(self) -> None:
        for name in (
            "recovered_to_queue",
            "recovered_to_retry_wait",
            "moved_to_send_unknown",
            "already_terminal",
        ):
            _require_non_negative_integer(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumerCancellationResult:
    cancelled_job_ids: tuple[str, ...]
    preserved_shared_job_ids: tuple[str, ...]
    blocking_leased_job_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "cancelled_job_ids",
            "preserved_shared_job_ids",
            "blocking_leased_job_ids",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise TypeError(f"{name} must be a tuple of identifiers")
            object.__setattr__(self, name, tuple(sorted(set(values))))


@runtime_checkable
class JobStoreProtocol(Protocol):
    """Atomic persistence boundary required by :class:`CollectionWorker`.

    Every mutating method must compare ``job_id``, ``worker_id``, lease token,
    generation, unexpired lease, and expected state in one transaction.  A CAS
    miss must raise :class:`LeaseLost` (or an adapter-specific subclass).

    ``reserve_attempt`` must freeze the attempt ID and increment the attempt
    count. ``mark_send_started`` must then durably set ``send_state=send_started``
    *before* it returns.  Send-outcome methods append exactly one immutable
    attempt using that reserved ID and transition the mutable job.  A failure
    during request loading or collector preparation has no reservation; in
    that proven pre-send case ``record_pre_send_failure`` atomically appends
    the supplied next attempt, increments the count, and applies its target
    state.

    Expired recovery must append a pre-send failure for ``attempt_reserved``
    before making it ordinary work again, and must append ``send_unknown`` for
    ``send_started`` before publishing the unresolved state.  Thus no reserved
    attempt number can disappear across restart.  Successful known outcomes
    must persist and accept artifact/evidence before publishing ``succeeded``.
    """

    def claim_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> CollectionJob | None: ...

    def heartbeat_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> CollectionJob: ...

    def load_collection_request(self, job: CollectionJob) -> CollectionRequest: ...

    def mark_send_started(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        attempt_id: str,
        at: datetime,
    ) -> CollectionJob: ...

    def reserve_attempt(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        attempt_id: str,
        at: datetime,
    ) -> CollectionJob: ...

    def record_pre_send_failure(
        self,
        *,
        job: CollectionJob,
        attempt: AttemptRecord,
        target_state: CollectionJobState,
        retry_at: datetime | None,
    ) -> CollectionJob: ...

    def record_known_send_outcome(
        self,
        *,
        job: CollectionJob,
        attempt: AttemptRecord,
        result: CollectionResult,
        target_state: CollectionJobState,
        retry_at: datetime | None,
    ) -> CollectionJob: ...

    def record_send_unknown(
        self,
        *,
        job: CollectionJob,
        attempt: AttemptRecord,
    ) -> CollectionJob: ...

    def recover_expired_jobs(self, *, now: datetime) -> RecoveryReport: ...

    def cancel_unstarted_jobs_for_consumer(
        self, *, campaign_revision_id: str, cancelled_at: datetime
    ) -> ConsumerCancellationResult: ...

    def campaign_has_blocking_jobs(self, *, campaign_revision_id: str) -> bool: ...

    def resolve_send_unknown(
        self,
        *,
        job_id: str,
        resolution: SendUnknownResolution,
        actor: str,
        resolved_at: datetime,
    ) -> CollectionJob: ...


def validate_job_transition(
    before: CollectionJobState | str, after: CollectionJobState | str
) -> None:
    """Validate the closed state graph used by database triggers/adapters."""

    source = _job_state(before)
    target = _job_state(after)
    allowed = {
        CollectionJobState.QUEUED: {
            CollectionJobState.LEASED,
            CollectionJobState.CANCELLED,
        },
        CollectionJobState.LEASED: {
            # Only expired-lease recovery may use the queued edge.
            CollectionJobState.QUEUED,
            CollectionJobState.LEASED,
            CollectionJobState.RETRY_WAIT,
            CollectionJobState.SUCCEEDED,
            CollectionJobState.TERMINAL_FAILED,
            CollectionJobState.SEND_UNKNOWN,
        },
        CollectionJobState.RETRY_WAIT: {
            CollectionJobState.LEASED,
            CollectionJobState.CANCELLED,
        },
        CollectionJobState.SUCCEEDED: set(),
        CollectionJobState.TERMINAL_FAILED: set(),
        CollectionJobState.CANCELLED: set(),
        # These edges are operator-only.  CollectionWorker exposes no path to
        # them; the store must audit actor and resolution time.  Success is
        # intentionally absent: an operator label alone cannot create the
        # verified artifact and acceptance required for succeeded evidence.
        # Cancellation is also absent because the send boundary was crossed;
        # cancellation remains limited to proven-unstarted work.
        CollectionJobState.SEND_UNKNOWN: {
            CollectionJobState.TERMINAL_FAILED,
        },
    }
    if target not in allowed[source]:
        raise JobConflict(f"invalid collection job transition {source.value}->{target.value}")


def resolve_send_unknown(
    store: JobStoreProtocol,
    *,
    job_id: str,
    resolution: SendUnknownResolution,
    actor: str,
    resolved_at: datetime,
) -> CollectionJob:
    """Apply an explicit, audited operator resolution to ambiguous work.

    This function is intentionally separate from :class:`CollectionWorker` so
    no automatic worker path can turn ambiguity into retry work.  The adapter
    must compare the current state to ``send_unknown`` and persist actor/time.
    """

    _require_text(job_id, "job_id")
    _require_text(actor, "actor")
    at = _utc(resolved_at, "resolved_at")
    if not isinstance(resolution, SendUnknownResolution):
        raise TypeError("resolution must be a SendUnknownResolution")
    resolved = store.resolve_send_unknown(
        job_id=job_id,
        resolution=resolution,
        actor=actor,
        resolved_at=at,
    )
    expected = CollectionJobState(resolution.value)
    if resolved.state is not expected:
        raise JobConflict("store did not apply the requested send_unknown resolution")
    return resolved


class CollectionWorker:
    """One restart-safe worker over a durable ``JobStoreProtocol``."""

    def __init__(
        self,
        *,
        store: JobStoreProtocol,
        collector: CollectorProtocol,
        worker_id: str,
        retry_policy: RetryPolicy,
        lease_duration: timedelta,
        clock: ClockProtocol | None = None,
        send_gate: Callable[[str], None] | None = None,
    ) -> None:
        _require_text(worker_id, "worker_id")
        if not isinstance(retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be a RetryPolicy")
        if retry_policy != retry_policy_for_version(retry_policy.policy_version):
            raise ValueError("retry_policy does not match the registered version")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be a positive timedelta")
        if send_gate is not None and not callable(send_gate):
            raise TypeError("send_gate must be callable")
        self.store = store
        self.collector = collector
        self.worker_id = worker_id
        self.retry_policy = retry_policy
        self.lease_duration = lease_duration
        self.clock = clock or SystemClock()
        self.send_gate = send_gate

    def _now(self) -> datetime:
        return _utc(self.clock.now(), "clock.now()")

    def recover_expired(self) -> RecoveryReport:
        return self.store.recover_expired_jobs(now=self._now())

    def claim(self) -> CollectionJob | None:
        now = self._now()
        return self.store.claim_job(
            worker_id=self.worker_id,
            now=now,
            lease_expires_at=now + self.lease_duration,
        )

    def heartbeat(self, job: CollectionJob) -> CollectionJob:
        self._require_owned_lease(job)
        now = self._now()
        return self.store.heartbeat_job(
            job_id=job.job_id,
            worker_id=self.worker_id,
            lease_token=job.lease_token,
            lease_generation=job.lease_generation,
            now=now,
            lease_expires_at=now + self.lease_duration,
        )

    def run_once(self) -> CollectionJob | None:
        """Claim and execute at most one job.

        The method never loops and never sleeps.  Scheduling and process
        lifecycle remain outside this package, making restart behavior fully
        determined by persisted job/lease state.
        """

        job = self.claim()
        if job is None:
            return None
        return self.run_claimed(job)

    def run_claimed(self, job: CollectionJob) -> CollectionJob:
        self._require_owned_lease(job)
        attempt_number = job.attempt_count + 1
        started_at = self._now()
        attempt_id = self._attempt_id(job, attempt_number)

        if (
            job.backoff_policy_version != self.retry_policy.policy_version
            or job.max_attempts != self.retry_policy.max_attempts
        ):
            return self._pre_send_failure(
                job,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                started_at=started_at,
                error_code="retry_policy_mismatch",
                retryable=False,
            )

        try:
            # WP15b capability gate, checked inside the proven pre-send region:
            # "pipeline" refuses an off gate before any work; "send" refuses
            # the external send at the last provably-safe point before the
            # irreversible boundary.  A refusal raises PreSendCollectionError,
            # so it lands in the existing known-safe (retryable) taxonomy.
            if self.send_gate is not None:
                self.send_gate("pipeline")
            request = self.store.load_collection_request(job)
            if request.job_id != job.job_id or (
                request.logical_observation_id != job.logical_observation_id
            ):
                raise CollectorContractError(
                    "collection request does not match the leased job"
                )
            prepared = self.collector.prepare(request)
            if not isinstance(prepared, PreparedCollection):
                raise CollectorContractError(
                    "collector.prepare must return PreparedCollection"
                )
            if prepared.request_sha256 != request.request_sha256:
                raise CollectorContractError(
                    "prepared collection is bound to a different request"
                )
            if self.send_gate is not None:
                self.send_gate("send")
        except Exception as exc:
            retryable = (
                False
                if isinstance(exc, CollectorContractError)
                else not isinstance(exc, PreSendCollectionError) or exc.retryable
            )
            error_code = (
                exc.error_code
                if isinstance(exc, PreSendCollectionError)
                else "pre_send_failure"
            )
            return self._pre_send_failure(
                job,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                started_at=started_at,
                error_code=error_code,
                retryable=retryable,
            )

        # Refresh the lease immediately before crossing the send boundary.
        job = self.heartbeat(job)
        reserved_at = self._now()
        job = self.store.reserve_attempt(
            job_id=job.job_id,
            worker_id=self.worker_id,
            lease_token=job.lease_token,
            lease_generation=job.lease_generation,
            attempt_id=attempt_id,
            at=reserved_at,
        )
        if job.send_state is not SendState.ATTEMPT_RESERVED:
            raise JobConflict("store did not persist the attempt reservation")

        marked_at = self._now()
        job = self.store.mark_send_started(
            job_id=job.job_id,
            worker_id=self.worker_id,
            lease_token=job.lease_token,
            lease_generation=job.lease_generation,
            attempt_id=attempt_id,
            at=marked_at,
        )
        if job.send_state is not SendState.STARTED:
            raise JobConflict("store did not persist the send-start marker")

        try:
            result = self.collector.send(prepared)
            if not isinstance(result, CollectionResult):
                raise CollectorContractError(
                    "collector.send must return CollectionResult"
                )
        except BaseException:
            # Deliberately catch BaseException: once send has been entered,
            # cancellation/interrupt is just as ambiguous as an ordinary
            # exception.  Persist ambiguity before propagating nothing.
            completed_at = self._now()
            attempt = AttemptRecord(
                attempt_id=attempt_id,
                job_id=job.job_id,
                logical_observation_id=job.logical_observation_id,
                attempt_number=attempt_number,
                lease_generation=job.lease_generation,
                started_at=started_at,
                completed_at=completed_at,
                outcome=AttemptOutcome.SEND_UNKNOWN,
                send_state=SendState.UNKNOWN,
                error_code="post_send_exception",
            )
            return self.store.record_send_unknown(job=job, attempt=attempt)

        completed_at = self._now()
        if request.runtime_is_compatible(result.runtime):
            outcome = AttemptOutcome.SUCCEEDED
            target = CollectionJobState.SUCCEEDED
            retry_at = None
            error_code = None
        else:
            outcome = AttemptOutcome.INCOMPATIBLE
            error_code = "runtime_context_mismatch"
            # The external action is known to have happened, but under the
            # wrong compatibility context.  Preserve that immutable attempt
            # and stop; only failures proven to be pre-send become automatic
            # retry work.
            target = CollectionJobState.TERMINAL_FAILED
            retry_at = None
        attempt = AttemptRecord(
            attempt_id=attempt_id,
            job_id=job.job_id,
            logical_observation_id=job.logical_observation_id,
            attempt_number=attempt_number,
            lease_generation=job.lease_generation,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            send_state=SendState.ACKNOWLEDGED,
            runtime=result.runtime,
            error_code=error_code,
        )
        return self.store.record_known_send_outcome(
            job=job,
            attempt=attempt,
            result=result,
            target_state=target,
            retry_at=retry_at,
        )

    def cancel_consumer(self, campaign_revision_id: str) -> ConsumerCancellationResult:
        _require_text(campaign_revision_id, "campaign_revision_id")
        return self.store.cancel_unstarted_jobs_for_consumer(
            campaign_revision_id=campaign_revision_id,
            cancelled_at=self._now(),
        )

    def consumer_completion_is_blocked(self, campaign_revision_id: str) -> bool:
        _require_text(campaign_revision_id, "campaign_revision_id")
        return self.store.campaign_has_blocking_jobs(
            campaign_revision_id=campaign_revision_id
        )

    def _require_owned_lease(self, job: CollectionJob) -> None:
        if not isinstance(job, CollectionJob):
            raise TypeError("job must be a CollectionJob")
        if job.state is not CollectionJobState.LEASED:
            raise LeaseLost("job is not leased")
        if job.worker_id != self.worker_id or not job.lease_token:
            raise LeaseLost("job lease belongs to a different worker")

    @staticmethod
    def _attempt_id(job: CollectionJob, attempt_number: int) -> str:
        digest = canonical_sha256(
            {
                "schema_version": MARKET_ATTEMPT_SCHEMA,
                "job_id": job.job_id,
                "logical_observation_id": job.logical_observation_id,
                "attempt_number": attempt_number,
                "lease_generation": job.lease_generation,
            }
        )
        return f"market-attempt-{digest[:24]}"

    def _retry_target(
        self,
        *,
        attempt_number: int,
        failed_at: datetime,
        retryable: bool,
    ) -> tuple[CollectionJobState, datetime | None]:
        if retryable and attempt_number < self.retry_policy.max_attempts:
            return (
                CollectionJobState.RETRY_WAIT,
                self.retry_policy.retry_at(
                    attempt_number=attempt_number,
                    failed_at=failed_at,
                ),
            )
        return CollectionJobState.TERMINAL_FAILED, None

    def _pre_send_failure(
        self,
        job: CollectionJob,
        *,
        attempt_id: str,
        attempt_number: int,
        started_at: datetime,
        error_code: str,
        retryable: bool,
    ) -> CollectionJob:
        completed_at = self._now()
        target, retry_at = self._retry_target(
            attempt_number=attempt_number,
            failed_at=completed_at,
            retryable=retryable,
        )
        outcome = (
            AttemptOutcome.RETRYABLE_FAILURE
            if target is CollectionJobState.RETRY_WAIT
            else AttemptOutcome.TERMINAL_FAILURE
        )
        attempt = AttemptRecord(
            attempt_id=attempt_id,
            job_id=job.job_id,
            logical_observation_id=job.logical_observation_id,
            attempt_number=attempt_number,
            lease_generation=job.lease_generation,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            send_state=SendState.NOT_STARTED,
            error_code=error_code,
        )
        return self.store.record_pre_send_failure(
            job=job,
            attempt=attempt,
            target_state=target,
            retry_at=retry_at,
        )


DurableCollectionWorker = CollectionWorker


__all__ = [
    "CLAIMABLE_JOB_STATES",
    "MARKET_ATTEMPT_SCHEMA",
    "MARKET_COLLECTION_REQUEST_SCHEMA",
    "MARKET_JOB_SCHEMA",
    "MARKET_RETRY_POLICY_SCHEMA",
    "TERMINAL_JOB_STATES",
    "UNSTARTED_JOB_STATES",
    "AttemptOutcome",
    "AttemptRecord",
    "ClockProtocol",
    "CollectionJob",
    "CollectionJobError",
    "CollectionJobState",
    "CollectionRequest",
    "CollectionResult",
    "CollectionWorker",
    "Collector",
    "CollectorContractError",
    "CollectorProtocol",
    "ConsumerCancellationResult",
    "DurableCollectionWorker",
    "JobConflict",
    "JobState",
    "JobStoreProtocol",
    "LeaseLost",
    "PreSendCollectionError",
    "PreparedCollection",
    "RecoveryReport",
    "RetryPolicy",
    "RuntimeContext",
    "SendState",
    "SendUnknownResolution",
    "SystemClock",
    "resolve_send_unknown",
    "retry_policy_for_version",
    "validate_job_transition",
]
