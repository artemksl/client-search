"""Convert verified engine results into the host's saved report records.

Read response files only through the engine's hash-checking reader. Preserve
the recorded values, order, duplicates and references.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Optional

from .api import FreeSnapshotResult
from .contracts_v3 import (
    ArtifactReferenceV3,
    EvidenceReferencesV3,
    GroundedOccurrenceV3,
    IssueV3,
    SnapshotObservationV3,
    SnapshotObservationsV3,
    SnapshotObservationsV3Error,
    SnapshotPacketV3,
    TERMINAL_RUN_STATUSES,
    validate_plan_identities_v3,
)


ADAPTER_VERSION = "runner-publication-adapter-v1"


class PublicationAdapterError(ValueError):
    """Base class for a typed adapter contract failure."""


class RunPlanRequiredError(PublicationAdapterError):
    """A runner result omitted the plan required by the host profile and website pair."""


class NonTerminalRunStatusError(PublicationAdapterError):
    """A planned or running result was supplied for publication adaptation."""


class PromptIdentityError(PublicationAdapterError):
    """An prompt measurement cannot be tied to exactly one frozen plan row."""


class PlanIdentityError(PublicationAdapterError):
    """The required prompt-pack or prompt measurement-plan identity is invalid."""


class ArtifactPayloadMismatchError(PublicationAdapterError):
    """An injected reader returned bytes outside their hash-bound reference."""


class RawAnswerDecodeError(PublicationAdapterError):
    """A runner raw-answer artifact is not UTF-8 text."""


# Concise compatibility spellings for callers that key on the failure cause.
MissingRunPlanError = RunPlanRequiredError
NonTerminalResultError = NonTerminalRunStatusError


ReadArtifact = Callable[[str, object], bytes]


_MISSING = object()


def _field(value: object, name: str, default: object = _MISSING) -> object:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    elif hasattr(value, name):
        return getattr(value, name)
    if default is not _MISSING:
        return default
    raise PublicationAdapterError(f"runner value is missing {name}")


def _enum_value(value: object, *, field_name: str) -> str:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or not value:
        raise PublicationAdapterError(f"{field_name} must be text")
    return value


def _as_mapping(value: object, *, field_name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    to_mapping = getattr(value, "to_mapping", None)
    if callable(to_mapping):
        mapped = to_mapping()
        if not isinstance(mapped, Mapping):
            raise PublicationAdapterError(
                f"{field_name}.to_mapping() did not return an object"
            )
        return {str(key): item for key, item in mapped.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    raise PublicationAdapterError(f"{field_name} is not a public mapping value")


def _sequence(value: object, *, field_name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise PublicationAdapterError(f"{field_name} must be an ordered sequence")
    return tuple(value)


def _artifact_reference(reference: object) -> ArtifactReferenceV3:
    return ArtifactReferenceV3(
        relative_path=_field(
            reference,
            "relative_path",
            _field(reference, "path", None),
        ),
        sha256=_field(reference, "sha256"),
        byte_length=_field(reference, "byte_length"),
        media_type=_field(reference, "media_type"),
    )


def _evidence_references(artifacts: object) -> EvidenceReferencesV3:
    def optional(name: str) -> Optional[ArtifactReferenceV3]:
        reference = _field(artifacts, name, None)
        return _artifact_reference(reference) if reference is not None else None

    additional = _sequence(
        _field(artifacts, "additional", ()),
        field_name="artifact additional references",
    )
    return EvidenceReferencesV3(
        turn_1=optional("turn_1"),
        turn_2=optional("turn_2"),
        google_source=optional("google_source"),
        grounded_names=optional("grounded_names"),
        state=optional("state"),
        additional=tuple(_artifact_reference(item) for item in additional),
    )


def _issue(issue: object) -> IssueV3:
    return IssueV3(
        code=_field(issue, "code"),
        message=_field(issue, "message"),
        severity=_enum_value(
            _field(issue, "severity"),
            field_name="issue severity",
        ),
        observation_id=_field(issue, "observation_id", None),
        packet_id=_field(issue, "packet_id", None),
        retry_safe=_field(issue, "retry_safe", False),
        detail=_field(issue, "detail", {}),
    )


def _plan_rows(
    observation_plan: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    raw_rows = observation_plan.get("observations")
    rows = _sequence(raw_rows, field_name="plan observation rows")
    by_id: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise PromptIdentityError("plan observation rows must be objects")
        observation_id = row.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id:
            raise PromptIdentityError(
                "plan observation row is missing observation_id"
            )
        if observation_id in by_id:
            raise PromptIdentityError(
                f"plan contains duplicate observation identity {observation_id!r}"
            )
        by_id[observation_id] = row
    return by_id


def _optional_identity_text(
    value: Mapping[str, object],
    key: str,
    *,
    field_name: str,
) -> Optional[str]:
    if key not in value:
        return None
    item = value[key]
    if not isinstance(item, str) or not item:
        raise PlanIdentityError(f"{field_name}.{key} must be nonempty text")
    return item


def _validate_scope_identity_bindings(
    frozen_scope: Mapping[str, object],
    prompt_pack: Mapping[str, object],
    observation_plan: Mapping[str, object],
) -> None:
    """Reject contradictory optional scope-to-plan identity bindings."""

    frozen_prompt_pack_hash = _optional_identity_text(
        frozen_scope,
        "prompt_pack_hash",
        field_name="frozen_scope",
    )
    if (
        frozen_prompt_pack_hash is not None
        and frozen_prompt_pack_hash != prompt_pack["prompt_pack_hash"]
    ):
        raise PlanIdentityError(
            "frozen_scope.prompt_pack_hash differs from the runner plan identity"
        )

    # ``scope_hash`` is not newly required.  The two runner plan documents are
    # in one identity domain and must agree whenever both expose it.
    prompt_scope_hash = _optional_identity_text(
        prompt_pack,
        "scope_hash",
        field_name="prompt_pack_identity",
    )
    observation_scope_hash = _optional_identity_text(
        observation_plan,
        "scope_hash",
        field_name="observation_plan_identity",
    )
    if (
        prompt_scope_hash is not None
        and observation_scope_hash is not None
        and prompt_scope_hash != observation_scope_hash
    ):
        raise PlanIdentityError(
            "scope_hash differs across runner plan identities"
        )

    # A host publication scope can have its own snapshot identity.  Compare it
    # with the runner scope only when the snapshot IDs do not explicitly show
    # that the hashes belong to different envelopes.
    frozen_scope_hash = _optional_identity_text(
        frozen_scope,
        "scope_hash",
        field_name="frozen_scope",
    )
    runner_scope_hash = prompt_scope_hash or observation_scope_hash
    if frozen_scope_hash is None or runner_scope_hash is None:
        return
    frozen_snapshot_id = _optional_identity_text(
        frozen_scope,
        "snapshot_id",
        field_name="frozen_scope",
    )
    runner_snapshot_ids = frozenset(
        snapshot_id
        for field_name, mapping in (
            ("prompt_pack_identity", prompt_pack),
            ("observation_plan_identity", observation_plan),
        )
        if (
            snapshot_id := _optional_identity_text(
                mapping,
                "snapshot_id",
                field_name=field_name,
            )
        )
        is not None
    )
    same_scope_identity = (
        frozen_snapshot_id is None
        or not runner_snapshot_ids
        or frozen_snapshot_id in runner_snapshot_ids
    )
    if same_scope_identity and frozen_scope_hash != runner_scope_hash:
        raise PlanIdentityError(
            "scope_hash differs across comparable frozen scope and runner plan identities"
        )


def _raw_answer_reference(observation: object) -> object | None:
    artifacts = _field(observation, "artifacts")
    platform = _enum_value(_field(observation, "platform"), field_name="observation platform")
    if platform == "github_copilot":
        return _field(artifacts, "github_copilot_source", None) or _field(artifacts, "turn_1", None)
    return _field(artifacts, "google_source" if platform == "google_ai_overview" else "turn_1", None)


def _read_raw_answer(
    observation: object,
    *,
    run_id: str,
    read_artifact: ReadArtifact,
) -> Optional[str]:
    reference = _raw_answer_reference(observation)
    if reference is None:
        return None

    # Propagate typed errors from Runner.read_artifact unchanged.  The local
    # check also fails closed if a test or future injection violates that
    # callable's verified-reader contract.
    payload = read_artifact(run_id, reference)
    if not isinstance(payload, bytes):
        raise ArtifactPayloadMismatchError(
            "read_artifact must return immutable bytes"
        )
    expected_hash = _field(reference, "sha256")
    expected_length = _field(reference, "byte_length")
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ArtifactPayloadMismatchError(
            "read_artifact returned bytes with a different SHA-256"
        )
    if len(payload) != expected_length:
        raise ArtifactPayloadMismatchError(
            "read_artifact returned bytes with a different byte length"
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RawAnswerDecodeError(
            "raw source evidence is not UTF-8 text"
        ) from exc


def _occurrence(value: object) -> GroundedOccurrenceV3:
    grade = _field(value, "grade", None)
    if grade is not None and not isinstance(grade, Mapping):
        raise PublicationAdapterError("occurrence grade must be an object")
    return GroundedOccurrenceV3(
        surface_form=_field(value, "name", None),
        position=_field(value, "position", None),
        character_start=_field(value, "character_start", None),
        character_end=_field(value, "character_end", None),
        byte_start=_field(value, "byte_start", None),
        byte_end=_field(value, "byte_end", None),
        grade=grade,
    )


def _observation(
    value: object,
    *,
    run_id: str,
    prompt_identity: Mapping[str, object],
    read_artifact: ReadArtifact,
) -> SnapshotObservationV3:
    occurrences = tuple(
        _occurrence(item)
        for item in _sequence(
            _field(value, "grounded_occurrences"),
            field_name="grounded_occurrences",
        )
    )
    ordered_names = _sequence(
        _field(value, "ordered_names"),
        field_name="ordered_names",
    )
    return SnapshotObservationV3(
        observation_id=_field(value, "observation_id"),
        packet_id=_field(value, "packet_id"),
        platform=_enum_value(
            _field(value, "platform"),
            field_name="observation platform",
        ),
        profile_id=_field(value, "profile_id"),
        prompt_identity=prompt_identity,
        prompt_sha256=_field(value, "prompt_sha256", None),
        status=_enum_value(
            _field(value, "status"),
            field_name="observation status",
        ),
        extraction_status=_enum_value(
            _field(value, "extraction_status"),
            field_name="extraction status",
        ),
        raw_answer_text=_read_raw_answer(
            value,
            run_id=run_id,
            read_artifact=read_artifact,
        ),
        ordered_names=tuple(ordered_names),
        grounded_occurrences=occurrences,
        evidence_references=_evidence_references(
            _field(value, "artifacts")
        ),
        issues=tuple(
            _issue(item)
            for item in _sequence(
                _field(value, "issues", ()),
                field_name="observation issues",
            )
        ),
        transport_counts=_as_mapping(
            _field(value, "transport_counts"),
            field_name="observation transport_counts",
        ),
        chrome_directory=_field(value, "chrome_directory", None),
        accepted_attempt_id=_field(value, "accepted_attempt_id", None),
        accepted_attempt_ids=_field(value, "accepted_attempt_ids", {}),
        attempt_ids=tuple(
            _sequence(
                _field(value, "attempt_ids", ()),
                field_name="observation attempt_ids",
            )
        ),
        capture_evidence=_capture_evidence_with_collection(value),
        grade=_field(value, "grade", None),
    )


def _capture_evidence_with_collection(value: object) -> Mapping[str, object]:
    # Collection lineage is public result metadata. Keep saved Google source
    # evidence untouched; this is only the host analysis input projection.
    evidence = dict(_field(value, "capture_evidence", {}))
    collection = _field(value, "collection_metadata", {})
    if collection:
        if not isinstance(collection, Mapping):
            raise PublicationAdapterError("collection metadata must be an object")
        if "collection" in evidence and evidence["collection"] != collection:
            raise PublicationAdapterError("conflicting collection metadata")
        evidence["collection"] = dict(collection)
    return evidence


def _packet(value: object) -> SnapshotPacketV3:
    observations = _sequence(
        _field(value, "observations"),
        field_name="packet observations",
    )
    return SnapshotPacketV3(
        packet_id=_field(value, "packet_id"),
        status=_enum_value(
            _field(value, "status"),
            field_name="packet status",
        ),
        observation_ids=tuple(
            _field(observation, "observation_id")
            for observation in observations
        ),
        evidence_references=_evidence_references(_field(value, "artifacts")),
        issues=tuple(
            _issue(item)
            for item in _sequence(
                _field(value, "issues", ()),
                field_name="packet issues",
            )
        ),
        transport_counts=_as_mapping(
            _field(value, "transport_counts"),
            field_name="packet transport_counts",
        ),
    )


def adapt_run_result(
    result: FreeSnapshotResult,
    *,
    frozen_scope: Mapping[str, object],
    read_artifact: ReadArtifact,
) -> SnapshotObservationsV3:
    """Build one immutable v3 document from a finished runner result.

    ``read_artifact`` is called as ``read_artifact(run_id, reference)`` and is
    never used for turn-2, exact name matching, state, or diagnostic evidence.
    """

    if not isinstance(result, FreeSnapshotResult):
        raise PublicationAdapterError("result must be a FreeSnapshotResult")
    if not isinstance(frozen_scope, Mapping):
        raise PublicationAdapterError("frozen_scope must be an object")
    if not callable(read_artifact):
        raise PublicationAdapterError("read_artifact must be callable")

    runner_result = result.runner_result
    plan = _field(runner_result, "plan", None)
    if plan is None:
        raise RunPlanRequiredError(
            "RunResult.plan is required for snapshot-observations-v3"
        )
    runner_scope = _field(plan, "scope_manifest", {})
    if frozen_scope.get("scope_kind") == "diagnostic" or (
            isinstance(runner_scope, Mapping) and runner_scope.get("scope_kind") == "diagnostic"):
        raise PublicationAdapterError("Diagnostic runs cannot produce clinic or area reports")

    status = _enum_value(
        _field(runner_result, "status"),
        field_name="run status",
    )
    if status in {"planned", "running"}:
        raise NonTerminalRunStatusError(
            f"cannot adapt non-terminal run status {status!r}"
        )
    if status not in TERMINAL_RUN_STATUSES:
        raise PublicationAdapterError(f"unknown run status {status!r}")

    run_id = _field(runner_result, "run_id")
    if not isinstance(run_id, str) or not run_id:
        raise PublicationAdapterError("run_id must be text")

    # Validate both frozen identities before adapting any prompt measurement.  This
    # ordering is important: even a failed/cancelled zero-row result must fail
    # closed, and a malformed plan must never authorize an artifact read.
    try:
        prompt_pack, observation_plan = validate_plan_identities_v3(
            _field(plan, "snapshot_prompts"),
            _field(plan, "observation_plan"),
        )
    except SnapshotObservationsV3Error as exc:
        raise PlanIdentityError(str(exc)) from exc
    _validate_scope_identity_bindings(
        frozen_scope,
        prompt_pack,
        observation_plan,
    )

    identities = _plan_rows(observation_plan)
    raw_observations = _sequence(
        _field(runner_result, "observations"),
        field_name="run observations",
    )
    effective_ids = tuple(_field(row, "observation_id") for row in raw_observations)
    if len(set(effective_ids)) != len(effective_ids):
        raise PublicationAdapterError("runner result repeats an effective observation")
    if status == "succeeded" and set(effective_ids) != set(identities):
        raise PublicationAdapterError("successful runner result does not cover its frozen observation plan")
    observations: list[SnapshotObservationV3] = []
    for raw_observation in raw_observations:
        observation_id = _field(raw_observation, "observation_id")
        prompt_identity = identities.get(observation_id)
        if prompt_identity is None:
            raise PromptIdentityError(
                f"observation {observation_id!r} has no frozen plan identity"
            )
        observations.append(
            _observation(
                raw_observation,
                run_id=run_id,
                prompt_identity=prompt_identity,
                read_artifact=read_artifact,
            )
        )

    return SnapshotObservationsV3(
        run_id=run_id,
        status=status,
        publishable=status == "succeeded",
        policy_version=_field(runner_result, "policy_version"),
        adapter_version=ADAPTER_VERSION,
        frozen_scope=frozen_scope,
        prompt_pack_identity=prompt_pack,
        observation_plan_identity=observation_plan,
        plan_sha256=_field(plan, "plan_sha256", None) or None,
        plan_schema_version=_field(plan, "schema_version"),
        observations=tuple(observations),
        packets=tuple(
            _packet(item)
            for item in _sequence(
                _field(runner_result, "packets"),
                field_name="run packets",
            )
        ),
        evidence_references=_evidence_references(
            _field(runner_result, "artifacts")
        ),
        issues=tuple(
            _issue(item)
            for item in _sequence(
                _field(runner_result, "issues", ()),
                field_name="run issues",
            )
        ),
        submission_budget=_as_mapping(
            _field(runner_result, "submission_budget"),
            field_name="submission_budget",
        ),
        transport_counts=_as_mapping(
            _field(runner_result, "transport_counts"),
            field_name="transport_counts",
        ),
    )


__all__ = [
    "ADAPTER_VERSION",
    "ArtifactPayloadMismatchError",
    "MissingRunPlanError",
    "NonTerminalResultError",
    "NonTerminalRunStatusError",
    "PlanIdentityError",
    "PromptIdentityError",
    "PublicationAdapterError",
    "RawAnswerDecodeError",
    "ReadArtifact",
    "RunPlanRequiredError",
    "adapt_run_result",
]
