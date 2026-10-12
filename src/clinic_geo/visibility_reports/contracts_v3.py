"""Immutable publication input contracts for runner-owned Snapshot results.

``snapshot-observations-v3`` is deliberately a one-way evidence document.  It
contains only values recorded by ``visibility_runner`` plus the already-frozen
host scope; it does not classify, normalize, deduplicate, or infer mentions.

The small ``from_mapping``/``to_mapping`` surface makes the contract suitable
for durable JSON fixtures without coupling it to the optional runner package.
Mappings are frozen recursively on input.  In particular, a future prompt measurement
``grade`` object is open-ended: every unknown member is retained unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Optional


SNAPSHOT_OBSERVATIONS_V3 = "snapshot-observations-v3"
SNAPSHOT_OBSERVATIONS_V3_SCHEMA_VERSION = SNAPSHOT_OBSERVATIONS_V3

TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "partial", "needs_attention", "failed", "cancelled"}
)


class SnapshotObservationsV3Error(ValueError):
    """A ``snapshot-observations-v3`` document is malformed."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_thaw(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    return value


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotObservationsV3Error(f"{field_name} is required")
    # Validate without normalizing: identity and surface-form bytes are evidence.
    return value


def _optional_text(value: object, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SnapshotObservationsV3Error(
            f"{field_name} must be text or null"
        )
    return value


def _optional_nonnegative_int(
    value: object,
    *,
    field_name: str,
) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SnapshotObservationsV3Error(
            f"{field_name} must be a non-negative integer or null"
        )
    return value


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SnapshotObservationsV3Error(f"{field_name} must be an object")
    return _freeze(value)


def _rows(value: object, *, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise SnapshotObservationsV3Error(f"{field_name} must be an array")
    return value


def _mapping_rows(
    value: object,
    *,
    field_name: str,
) -> tuple[Mapping[str, object], ...]:
    rows = _rows(value, field_name=field_name)
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise SnapshotObservationsV3Error(
                f"{field_name}[{index}] must be an object"
            )
    return tuple(rows)


def _identity_sha256(
    value: Mapping[str, object],
    key: str,
    *,
    field_name: str,
) -> str:
    digest = _required_text(value.get(key), field_name=f"{field_name}.{key}")
    if len(digest) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in digest
    ):
        raise SnapshotObservationsV3Error(
            f"{field_name}.{key} must be exactly 64 hexadecimal characters"
        )
    return digest


def validate_plan_identities_v3(
    prompt_pack_identity: object,
    observation_plan_identity: object,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Validate the two runner-frozen identity documents required by v3."""

    prompt_pack = _mapping(
        prompt_pack_identity,
        field_name="prompt_pack_identity",
    )
    observation_plan = _mapping(
        observation_plan_identity,
        field_name="observation_plan_identity",
    )
    for key in ("schema_version", "prompt_pack_version"):
        _required_text(
            prompt_pack.get(key),
            field_name=f"prompt_pack_identity.{key}",
        )
    prompt_pack_hash = _identity_sha256(
        prompt_pack,
        "prompt_pack_hash",
        field_name="prompt_pack_identity",
    )
    _required_text(
        observation_plan.get("schema_version"),
        field_name="observation_plan_identity.schema_version",
    )
    _identity_sha256(
        observation_plan,
        "observation_plan_hash",
        field_name="observation_plan_identity",
    )
    observation_prompt_pack_hash = _identity_sha256(
        observation_plan,
        "prompt_pack_hash",
        field_name="observation_plan_identity",
    )
    if observation_prompt_pack_hash != prompt_pack_hash:
        raise SnapshotObservationsV3Error(
            "plan identities carry different prompt_pack_hash values"
        )
    _mapping_rows(
        observation_plan.get("observations"),
        field_name="observation_plan_identity.observations",
    )
    return prompt_pack, observation_plan


@dataclass(frozen=True)
class ArtifactReferenceV3:
    """One public runner artifact reference, copied field-for-field."""

    relative_path: str
    sha256: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            _required_text(self.relative_path, field_name="relative_path"),
        )
        object.__setattr__(
            self,
            "sha256",
            _required_text(self.sha256, field_name="sha256"),
        )
        byte_length = _optional_nonnegative_int(
            self.byte_length,
            field_name="byte_length",
        )
        assert byte_length is not None
        object.__setattr__(self, "byte_length", byte_length)
        object.__setattr__(
            self,
            "media_type",
            _required_text(self.media_type, field_name="media_type"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ArtifactReferenceV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error(
                "artifact reference must be an object"
            )
        return cls(
            relative_path=value.get("relative_path", value.get("path")),
            sha256=value.get("sha256"),
            byte_length=value.get("byte_length"),
            media_type=value.get("media_type"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
        }

    to_dict = to_mapping
    from_dict = from_mapping


@dataclass(frozen=True)
class EvidenceReferencesV3:
    """All independently hash-bound evidence attached to a runner result."""

    turn_1: Optional[ArtifactReferenceV3] = None
    turn_2: Optional[ArtifactReferenceV3] = None
    google_source: Optional[ArtifactReferenceV3] = None
    grounded_names: Optional[ArtifactReferenceV3] = None
    state: Optional[ArtifactReferenceV3] = None
    additional: tuple[ArtifactReferenceV3, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "turn_1",
            "turn_2",
            "google_source",
            "grounded_names",
            "state",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, ArtifactReferenceV3):
                raise SnapshotObservationsV3Error(
                    f"{field_name} must be an ArtifactReferenceV3 or null"
                )
        additional = tuple(self.additional)
        if any(
            not isinstance(reference, ArtifactReferenceV3)
            for reference in additional
        ):
            raise SnapshotObservationsV3Error(
                "additional must contain ArtifactReferenceV3 values"
            )
        object.__setattr__(self, "additional", additional)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "EvidenceReferencesV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error(
                "evidence_references must be an object"
            )

        def optional_reference(name: str) -> Optional[ArtifactReferenceV3]:
            item = value.get(name)
            if item is None:
                return None
            if not isinstance(item, Mapping):
                raise SnapshotObservationsV3Error(
                    f"evidence_references.{name} must be an object or null"
                )
            return ArtifactReferenceV3.from_mapping(item)

        return cls(
            turn_1=optional_reference("turn_1"),
            turn_2=optional_reference("turn_2"),
            google_source=optional_reference("google_source"),
            grounded_names=optional_reference("grounded_names"),
            state=optional_reference("state"),
            additional=tuple(
                ArtifactReferenceV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("additional", ()),
                    field_name="evidence_references.additional",
                )
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "turn_1": self.turn_1.to_mapping() if self.turn_1 else None,
            "turn_2": self.turn_2.to_mapping() if self.turn_2 else None,
            "google_source": (
                self.google_source.to_mapping() if self.google_source else None
            ),
            "grounded_names": (
                self.grounded_names.to_mapping()
                if self.grounded_names
                else None
            ),
            "state": self.state.to_mapping() if self.state else None,
            "additional": [item.to_mapping() for item in self.additional],
        }

    @property
    def all(self) -> tuple[ArtifactReferenceV3, ...]:
        return tuple(
            reference
            for reference in (
                self.turn_1,
                self.turn_2,
                self.google_source,
                self.grounded_names,
                self.state,
                *self.additional,
            )
            if reference is not None
        )

    to_dict = to_mapping
    from_dict = from_mapping


@dataclass(frozen=True)
class IssueV3:
    """A runner issue copied without changing its severity or detail."""

    code: str
    message: str
    severity: str
    observation_id: Optional[str] = None
    packet_id: Optional[str] = None
    retry_safe: bool = False
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "code", _required_text(self.code, field_name="issue code")
        )
        object.__setattr__(
            self,
            "message",
            _required_text(self.message, field_name="issue message"),
        )
        object.__setattr__(
            self,
            "severity",
            _required_text(self.severity, field_name="issue severity"),
        )
        object.__setattr__(
            self,
            "observation_id",
            _optional_text(
                self.observation_id,
                field_name="issue observation_id",
            ),
        )
        object.__setattr__(
            self,
            "packet_id",
            _optional_text(self.packet_id, field_name="issue packet_id"),
        )
        if not isinstance(self.retry_safe, bool):
            raise SnapshotObservationsV3Error(
                "issue retry_safe must be a boolean"
            )
        object.__setattr__(
            self,
            "detail",
            _mapping(self.detail, field_name="issue detail"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IssueV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error("issue must be an object")
        return cls(
            code=value.get("code"),
            message=value.get("message"),
            severity=value.get("severity"),
            observation_id=value.get("observation_id"),
            packet_id=value.get("packet_id"),
            retry_safe=value.get("retry_safe", False),
            detail=value.get("detail", {}),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "observation_id": self.observation_id,
            "packet_id": self.packet_id,
            "retry_safe": self.retry_safe,
            "detail": _thaw(self.detail),
        }

    to_dict = to_mapping
    from_dict = from_mapping


@dataclass(frozen=True)
class GroundedOccurrenceV3:
    """One exact, ordered surface-form occurrence exactly matched by the runner."""

    surface_form: Optional[str]
    position: Optional[int]
    character_start: Optional[int]
    character_end: Optional[int]
    byte_start: Optional[int]
    byte_end: Optional[int]
    grade: Optional[Mapping[str, object]] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "surface_form",
            _optional_text(self.surface_form, field_name="surface_form"),
        )
        for field_name in (
            "position",
            "character_start",
            "character_end",
            "byte_start",
            "byte_end",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_nonnegative_int(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if self.grade is not None:
            object.__setattr__(
                self,
                "grade",
                _mapping(self.grade, field_name="grade"),
            )

    @property
    def name(self) -> Optional[str]:
        """Compatibility spelling for the runner's source field."""

        return self.surface_form

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "GroundedOccurrenceV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error(
                "grounded occurrence must be an object"
            )
        return cls(
            surface_form=value.get("surface_form", value.get("name")),
            position=value.get("position"),
            character_start=value.get("character_start"),
            character_end=value.get("character_end"),
            byte_start=value.get("byte_start"),
            byte_end=value.get("byte_end"),
            grade=value.get("grade"),
        )

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "surface_form": self.surface_form,
            "position": self.position,
            "character_start": self.character_start,
            "character_end": self.character_end,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }
        if self.grade is not None:
            value["grade"] = _thaw(self.grade)
        return value

    to_dict = to_mapping
    from_dict = from_mapping


@dataclass(frozen=True)
class SnapshotObservationV3:
    """One prompt measurement with its exact plan identity and exactly matched evidence."""

    observation_id: str
    packet_id: str
    platform: str
    profile_id: str
    prompt_identity: Mapping[str, object]
    status: str
    extraction_status: str
    raw_answer_text: Optional[str]
    ordered_names: tuple[Optional[str], ...]
    grounded_occurrences: tuple[GroundedOccurrenceV3, ...]
    evidence_references: EvidenceReferencesV3
    issues: tuple[IssueV3, ...]
    transport_counts: Mapping[str, object]
    prompt_sha256: Optional[str] = None
    chrome_directory: Optional[str] = None
    accepted_attempt_id: Optional[str] = None
    accepted_attempt_ids: Mapping[str, str] = field(default_factory=dict)
    attempt_ids: tuple[str, ...] = ()
    capture_evidence: Mapping[str, object] = field(default_factory=dict)
    grade: Optional[Mapping[str, object]] = None

    def __post_init__(self) -> None:
        for field_name in (
            "observation_id",
            "packet_id",
            "platform",
            "profile_id",
            "status",
            "extraction_status",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "prompt_identity",
            _mapping(self.prompt_identity, field_name="prompt_identity"),
        )
        object.__setattr__(
            self,
            "raw_answer_text",
            _optional_text(self.raw_answer_text, field_name="raw_answer_text"),
        )
        names = tuple(self.ordered_names)
        for name in names:
            _optional_text(name, field_name="ordered name")
        occurrences = tuple(self.grounded_occurrences)
        if any(
            not isinstance(item, GroundedOccurrenceV3)
            for item in occurrences
        ):
            raise SnapshotObservationsV3Error(
                "grounded_occurrences must contain GroundedOccurrenceV3 values"
            )
        occurrence_names = tuple(item.surface_form for item in occurrences)
        if names != occurrence_names:
            raise SnapshotObservationsV3Error(
                "ordered_names must exactly match grounded occurrence order"
            )
        issues = tuple(self.issues)
        if any(not isinstance(issue, IssueV3) for issue in issues):
            raise SnapshotObservationsV3Error(
                "observation issues must contain IssueV3 values"
            )
        if not isinstance(self.evidence_references, EvidenceReferencesV3):
            raise SnapshotObservationsV3Error(
                "evidence_references must be EvidenceReferencesV3"
            )
        object.__setattr__(self, "ordered_names", names)
        object.__setattr__(self, "grounded_occurrences", occurrences)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(
            self,
            "transport_counts",
            _mapping(self.transport_counts, field_name="transport_counts"),
        )
        object.__setattr__(
            self,
            "prompt_sha256",
            _optional_text(self.prompt_sha256, field_name="prompt_sha256"),
        )
        object.__setattr__(
            self,
            "chrome_directory",
            _optional_text(
                self.chrome_directory,
                field_name="chrome_directory",
            ),
        )
        object.__setattr__(
            self,
            "accepted_attempt_id",
            _optional_text(
                self.accepted_attempt_id,
                field_name="accepted_attempt_id",
            ),
        )
        accepted = _mapping(
            self.accepted_attempt_ids,
            field_name="accepted_attempt_ids",
        )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in accepted.items()
        ):
            raise SnapshotObservationsV3Error(
                "accepted_attempt_ids must map text to text"
            )
        object.__setattr__(self, "accepted_attempt_ids", accepted)
        attempts = tuple(self.attempt_ids)
        if any(not isinstance(item, str) for item in attempts):
            raise SnapshotObservationsV3Error(
                "attempt_ids must contain text values"
            )
        object.__setattr__(self, "attempt_ids", attempts)
        object.__setattr__(
            self,
            "capture_evidence",
            _mapping(self.capture_evidence, field_name="capture_evidence"),
        )
        if self.grade is not None:
            object.__setattr__(
                self,
                "grade",
                _mapping(self.grade, field_name="observation grade"),
            )

    @property
    def turn_1_raw_answer_text(self) -> Optional[str]:
        return self.raw_answer_text

    @property
    def prompt_id(self) -> Optional[str]:
        value = self.prompt_identity.get("prompt_id")
        if value is None:
            value = self.prompt_identity.get("id")
        return value if isinstance(value, str) else None

    @property
    def semantic_prompt_id(self) -> Optional[str]:
        value = self.prompt_identity.get("semantic_prompt_id")
        return value if isinstance(value, str) else None

    @property
    def run_ordinal(self) -> Optional[int]:
        value = self.prompt_identity.get("run")
        if value is None:
            value = self.prompt_identity.get("run_ordinal")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SnapshotObservationV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error("observation must be an object")
        evidence = value.get("evidence_references", {})
        if not isinstance(evidence, Mapping):
            raise SnapshotObservationsV3Error(
                "observation evidence_references must be an object"
            )
        return cls(
            observation_id=value.get("observation_id"),
            packet_id=value.get("packet_id"),
            platform=value.get("platform"),
            profile_id=value.get("profile_id"),
            prompt_identity=value.get("prompt_identity", {}),
            status=value.get("status"),
            extraction_status=value.get("extraction_status"),
            raw_answer_text=value.get(
                "raw_answer_text",
                value.get("turn_1_raw_answer_text"),
            ),
            ordered_names=tuple(
                _rows(value.get("ordered_names", ()), field_name="ordered_names")
            ),
            grounded_occurrences=tuple(
                GroundedOccurrenceV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("grounded_occurrences", ()),
                    field_name="grounded_occurrences",
                )
            ),
            evidence_references=EvidenceReferencesV3.from_mapping(evidence),
            issues=tuple(
                IssueV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("issues", ()),
                    field_name="issues",
                )
            ),
            transport_counts=value.get("transport_counts", {}),
            prompt_sha256=value.get("prompt_sha256"),
            chrome_directory=value.get("chrome_directory"),
            accepted_attempt_id=value.get("accepted_attempt_id"),
            accepted_attempt_ids=value.get("accepted_attempt_ids", {}),
            attempt_ids=tuple(
                _rows(value.get("attempt_ids", ()), field_name="attempt_ids")
            ),
            capture_evidence=value.get("capture_evidence", {}),
            grade=value.get("grade"),
        )

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "observation_id": self.observation_id,
            "packet_id": self.packet_id,
            "platform": self.platform,
            "profile_id": self.profile_id,
            "prompt_identity": _thaw(self.prompt_identity),
            "prompt_sha256": self.prompt_sha256,
            "status": self.status,
            "extraction_status": self.extraction_status,
            "raw_answer_text": self.raw_answer_text,
            "ordered_names": list(self.ordered_names),
            "grounded_occurrences": [
                item.to_mapping() for item in self.grounded_occurrences
            ],
            "evidence_references": self.evidence_references.to_mapping(),
            "issues": [issue.to_mapping() for issue in self.issues],
            "transport_counts": _thaw(self.transport_counts),
            "chrome_directory": self.chrome_directory,
            "accepted_attempt_id": self.accepted_attempt_id,
            "accepted_attempt_ids": _thaw(self.accepted_attempt_ids),
            "attempt_ids": list(self.attempt_ids),
            "capture_evidence": _thaw(self.capture_evidence),
        }
        if self.grade is not None:
            value["grade"] = _thaw(self.grade)
        return value

    to_dict = to_mapping
    from_dict = from_mapping


@dataclass(frozen=True)
class SnapshotPacketV3:
    """Measurement group-level evidence retained so no runner issue/reference is lost."""

    packet_id: str
    status: str
    observation_ids: tuple[str, ...]
    evidence_references: EvidenceReferencesV3
    issues: tuple[IssueV3, ...]
    transport_counts: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "packet_id",
            _required_text(self.packet_id, field_name="packet_id"),
        )
        object.__setattr__(
            self,
            "status",
            _required_text(self.status, field_name="packet status"),
        )
        observation_ids = tuple(self.observation_ids)
        if any(not isinstance(item, str) for item in observation_ids):
            raise SnapshotObservationsV3Error(
                "packet observation_ids must contain text values"
            )
        issues = tuple(self.issues)
        if any(not isinstance(item, IssueV3) for item in issues):
            raise SnapshotObservationsV3Error(
                "packet issues must contain IssueV3 values"
            )
        if not isinstance(self.evidence_references, EvidenceReferencesV3):
            raise SnapshotObservationsV3Error(
                "packet evidence_references must be EvidenceReferencesV3"
            )
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(
            self,
            "transport_counts",
            _mapping(
                self.transport_counts,
                field_name="packet transport_counts",
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SnapshotPacketV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error("packet must be an object")
        evidence = value.get("evidence_references", {})
        if not isinstance(evidence, Mapping):
            raise SnapshotObservationsV3Error(
                "packet evidence_references must be an object"
            )
        return cls(
            packet_id=value.get("packet_id"),
            status=value.get("status"),
            observation_ids=tuple(
                _rows(
                    value.get("observation_ids", ()),
                    field_name="packet observation_ids",
                )
            ),
            evidence_references=EvidenceReferencesV3.from_mapping(evidence),
            issues=tuple(
                IssueV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("issues", ()),
                    field_name="packet issues",
                )
            ),
            transport_counts=value.get("transport_counts", {}),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "packet_id": self.packet_id,
            "status": self.status,
            "observation_ids": list(self.observation_ids),
            "evidence_references": self.evidence_references.to_mapping(),
            "issues": [issue.to_mapping() for issue in self.issues],
            "transport_counts": _thaw(self.transport_counts),
        }

    to_dict = to_mapping
    from_dict = from_mapping


@dataclass(frozen=True)
class SnapshotObservationsV3:
    """The complete ``snapshot-observations-v3`` publication input."""

    run_id: str
    status: str
    publishable: bool
    policy_version: str
    adapter_version: str
    frozen_scope: Mapping[str, object]
    prompt_pack_identity: Mapping[str, object]
    observation_plan_identity: Mapping[str, object]
    plan_sha256: Optional[str]
    plan_schema_version: str
    observations: tuple[SnapshotObservationV3, ...]
    packets: tuple[SnapshotPacketV3, ...]
    evidence_references: EvidenceReferencesV3
    issues: tuple[IssueV3, ...]
    submission_budget: Mapping[str, object]
    transport_counts: Mapping[str, object]
    contract: str = SNAPSHOT_OBSERVATIONS_V3

    def __post_init__(self) -> None:
        if self.contract != SNAPSHOT_OBSERVATIONS_V3:
            raise SnapshotObservationsV3Error(
                f"contract must equal {SNAPSHOT_OBSERVATIONS_V3!r}"
            )
        object.__setattr__(
            self, "run_id", _required_text(self.run_id, field_name="run_id")
        )
        status = _required_text(self.status, field_name="status")
        if status not in TERMINAL_RUN_STATUSES:
            raise SnapshotObservationsV3Error(
                f"status is not terminal: {status!r}"
            )
        object.__setattr__(self, "status", status)
        if not isinstance(self.publishable, bool):
            raise SnapshotObservationsV3Error(
                "publishable must be a boolean"
            )
        if self.publishable != (status == "succeeded"):
            raise SnapshotObservationsV3Error(
                "only succeeded results are publishable"
            )
        for field_name in (
            "policy_version",
            "adapter_version",
            "plan_schema_version",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "plan_sha256",
            _optional_text(self.plan_sha256, field_name="plan_sha256"),
        )
        for field_name in (
            "frozen_scope",
            "submission_budget",
            "transport_counts",
        ):
            object.__setattr__(
                self,
                field_name,
                _mapping(getattr(self, field_name), field_name=field_name),
            )
        prompt_pack, observation_plan = validate_plan_identities_v3(
            self.prompt_pack_identity,
            self.observation_plan_identity,
        )
        object.__setattr__(self, "prompt_pack_identity", prompt_pack)
        object.__setattr__(
            self,
            "observation_plan_identity",
            observation_plan,
        )
        observations = tuple(self.observations)
        if any(
            not isinstance(item, SnapshotObservationV3)
            for item in observations
        ):
            raise SnapshotObservationsV3Error(
                "observations must contain SnapshotObservationV3 values"
            )
        packets = tuple(self.packets)
        if any(not isinstance(item, SnapshotPacketV3) for item in packets):
            raise SnapshotObservationsV3Error(
                "packets must contain SnapshotPacketV3 values"
            )
        issues = tuple(self.issues)
        if any(not isinstance(item, IssueV3) for item in issues):
            raise SnapshotObservationsV3Error(
                "issues must contain IssueV3 values"
            )
        if not isinstance(self.evidence_references, EvidenceReferencesV3):
            raise SnapshotObservationsV3Error(
                "evidence_references must be EvidenceReferencesV3"
            )
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "packets", packets)
        object.__setattr__(self, "issues", issues)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def prompt_pack(self) -> Mapping[str, object]:
        return self.prompt_pack_identity

    @property
    def observation_plan(self) -> Mapping[str, object]:
        return self.observation_plan_identity

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SnapshotObservationsV3":
        if not isinstance(value, Mapping):
            raise SnapshotObservationsV3Error(
                "snapshot observations document must be an object"
            )
        evidence = value.get("evidence_references", {})
        if not isinstance(evidence, Mapping):
            raise SnapshotObservationsV3Error(
                "evidence_references must be an object"
            )
        return cls(
            contract=value.get("contract"),
            run_id=value.get("run_id"),
            status=value.get("status"),
            publishable=value.get("publishable"),
            policy_version=value.get("policy_version"),
            adapter_version=value.get("adapter_version"),
            frozen_scope=value.get("frozen_scope", {}),
            prompt_pack_identity=value.get("prompt_pack_identity", {}),
            observation_plan_identity=value.get(
                "observation_plan_identity",
                {},
            ),
            plan_sha256=value.get("plan_sha256"),
            plan_schema_version=value.get("plan_schema_version"),
            observations=tuple(
                SnapshotObservationV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("observations", ()),
                    field_name="observations",
                )
            ),
            packets=tuple(
                SnapshotPacketV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("packets", ()),
                    field_name="packets",
                )
            ),
            evidence_references=EvidenceReferencesV3.from_mapping(evidence),
            issues=tuple(
                IssueV3.from_mapping(item)
                for item in _mapping_rows(
                    value.get("issues", ()),
                    field_name="issues",
                )
            ),
            submission_budget=value.get("submission_budget", {}),
            transport_counts=value.get("transport_counts", {}),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "adapter_version": self.adapter_version,
            "run_id": self.run_id,
            "status": self.status,
            "publishable": self.publishable,
            "policy_version": self.policy_version,
            "frozen_scope": _thaw(self.frozen_scope),
            "plan_sha256": self.plan_sha256,
            "plan_schema_version": self.plan_schema_version,
            "prompt_pack_identity": _thaw(self.prompt_pack_identity),
            "observation_plan_identity": _thaw(
                self.observation_plan_identity
            ),
            "observations": [item.to_mapping() for item in self.observations],
            "packets": [item.to_mapping() for item in self.packets],
            "evidence_references": self.evidence_references.to_mapping(),
            "issues": [item.to_mapping() for item in self.issues],
            "submission_budget": _thaw(self.submission_budget),
            "transport_counts": _thaw(self.transport_counts),
        }

    to_dict = to_mapping
    from_dict = from_mapping


# Readable aliases for callers that use the word "row" for document members.
GroundedOccurrenceRowV3 = GroundedOccurrenceV3
SnapshotObservationRowV3 = SnapshotObservationV3


__all__ = [
    "ArtifactReferenceV3",
    "EvidenceReferencesV3",
    "GroundedOccurrenceRowV3",
    "GroundedOccurrenceV3",
    "IssueV3",
    "SNAPSHOT_OBSERVATIONS_V3",
    "SNAPSHOT_OBSERVATIONS_V3_SCHEMA_VERSION",
    "SnapshotObservationRowV3",
    "SnapshotObservationV3",
    "SnapshotObservationsV3",
    "SnapshotObservationsV3Error",
    "SnapshotPacketV3",
    "TERMINAL_RUN_STATUSES",
    "validate_plan_identities_v3",
]
