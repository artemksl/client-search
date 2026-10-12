"""Evidence-purpose firewall shared by immutable evidence contracts."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class EvidencePurpose(str, Enum):
    """The only evidence purposes accepted by the application."""

    MARKET_DISCOVERY = "market_discovery"
    FREE_SNAPSHOT_CONFIRMATION = "free_snapshot_confirmation"
    OUTREACH_GAP_CONFIRMATION = "outreach_gap_confirmation"


class EvidenceCompatibilityReason(str, Enum):
    EXACT_MATCH = "exact_match"
    PURPOSE_MISMATCH = "purpose_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"


class EvidenceRecordKind(str, Enum):
    """Purpose-bearing evidence records across both evidence domains."""

    SPECIFICATION = "specification"
    WINDOW = "window"
    LOGICAL_OBSERVATION = "logical_observation"
    ATTEMPT = "attempt"
    ARTIFACT = "artifact"
    OCCURRENCE_RUN = "occurrence_run"
    CLASSIFICATION = "classification"
    PROJECTION = "projection"
    RELEASE_MANIFEST = "release_manifest"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LEGACY_PURPOSE_ADAPTER_VERSION = "evidence-purpose-legacy-adapter-v1"


def validate_evidence_purpose(value: EvidencePurpose | str) -> EvidencePurpose:
    """Return a closed purpose value and reject arbitrary free text."""

    if isinstance(value, EvidencePurpose):
        return value
    if not isinstance(value, str):
        raise TypeError("evidence purpose must be an EvidencePurpose or string")
    try:
        return EvidencePurpose(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in EvidencePurpose)
        raise ValueError(f"unsupported evidence purpose {value!r}; expected {allowed}") from exc


def require_evidence_purpose(
    value: Mapping,
    expected: EvidencePurpose | str,
    *,
    label: str = "evidence record",
) -> EvidencePurpose:
    """Require an explicit, exact purpose on a current evidence mapping."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    if "purpose" not in value:
        raise ValueError(f"{label} has no explicit evidence purpose")
    actual = validate_evidence_purpose(value.get("purpose"))
    required = validate_evidence_purpose(expected)
    if actual is not required:
        raise ValueError(
            f"{label} purpose {actual.value!r} does not match required {required.value!r}"
        )
    return actual


def legacy_purpose_view(
    value: Mapping,
    expected: EvidencePurpose | str,
    *,
    artifact_kind: str,
    supported_schema_versions: tuple[str, ...] | frozenset[str],
    schema_field: str = "schema_version",
) -> dict:
    """Return an explicit read-only compatibility view for an old artifact.

    The source bytes and their historical self-hash remain untouched.  The
    returned mapping is an *effective read view*, not a newly self-hashed
    artifact: adapter-only fields are deliberately outside the historical hash
    envelope.  Use :func:`legacy_source_payload` to recover the exact mapping
    whose source hash was validated.  A present purpose is never relabelled
    through the legacy path.
    """

    if not isinstance(value, Mapping):
        raise TypeError("legacy evidence artifact must be a mapping")
    if "purpose" in value:
        raise ValueError("legacy purpose adapter cannot relabel an explicit purpose")
    schema_version = str(value.get(schema_field) or "").strip()
    allowed = frozenset(str(item) for item in supported_schema_versions)
    if schema_version not in allowed:
        raise ValueError(
            f"legacy {artifact_kind} schema {schema_version!r} is not allowlisted"
        )
    purpose = validate_evidence_purpose(expected)
    result = deepcopy(dict(value))
    result["purpose"] = purpose.value
    result["purpose_compatibility"] = {
        "adapter_version": LEGACY_PURPOSE_ADAPTER_VERSION,
        "artifact_kind": str(artifact_kind),
        "source_schema_version": schema_version,
        "inference": "historical_artifact_without_purpose",
        "read_only": True,
        "adapter_only_fields": ["purpose", "purpose_compatibility"],
    }
    return result


def legacy_source_payload(value: Mapping) -> dict:
    """Recover the exact source-hash envelope from a legacy effective view.

    This makes the hash boundary explicit and prevents callers from
    accidentally recomputing a historical self-hash over adapter metadata.
    Only views created by :func:`legacy_purpose_view` are accepted.
    """

    if not isinstance(value, Mapping):
        raise TypeError("legacy purpose view must be a mapping")
    compatibility = value.get("purpose_compatibility")
    if not isinstance(compatibility, Mapping) or compatibility.get(
        "adapter_version"
    ) != LEGACY_PURPOSE_ADAPTER_VERSION:
        raise ValueError("mapping is not an explicit legacy purpose view")
    if compatibility.get("adapter_only_fields") != [
        "purpose",
        "purpose_compatibility",
    ]:
        raise ValueError("legacy purpose view has an unknown hash boundary")
    source = deepcopy(dict(value))
    source.pop("purpose", None)
    source.pop("purpose_compatibility", None)
    return source


@dataclass(frozen=True, slots=True)
class EvidencePurposeBinding:
    """An immutable parent/child purpose assertion used by stores and artifacts."""

    record_kind: EvidenceRecordKind
    record_id: str
    purpose: EvidencePurpose
    parent_record_kind: EvidenceRecordKind | None = None
    parent_record_id: str | None = None
    parent_purpose: EvidencePurpose | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_kind, EvidenceRecordKind):
            raise TypeError("record_kind must be an EvidenceRecordKind")
        if not str(self.record_id or "").strip():
            raise ValueError("record_id is required")
        object.__setattr__(self, "record_id", str(self.record_id).strip())
        purpose = validate_evidence_purpose(self.purpose)
        object.__setattr__(self, "purpose", purpose)

        parent_values = (
            self.parent_record_kind,
            self.parent_record_id,
            self.parent_purpose,
        )
        if all(value is None for value in parent_values):
            return
        if any(value is None for value in parent_values):
            raise ValueError("parent kind, id, and purpose must be supplied together")
        if not isinstance(self.parent_record_kind, EvidenceRecordKind):
            raise TypeError("parent_record_kind must be an EvidenceRecordKind")
        parent_id = str(self.parent_record_id or "").strip()
        if not parent_id:
            raise ValueError("parent_record_id is required")
        object.__setattr__(self, "parent_record_id", parent_id)
        parent_purpose = validate_evidence_purpose(self.parent_purpose)
        object.__setattr__(self, "parent_purpose", parent_purpose)
        if parent_purpose is not purpose:
            raise ValueError("child evidence purpose does not match parent purpose")


@dataclass(frozen=True, slots=True)
class EvidenceCompatibilityKey:
    """A purpose-scoped immutable identity used at evidence boundaries."""

    purpose: EvidencePurpose
    identity_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", validate_evidence_purpose(self.purpose))
        if not _SHA256_RE.fullmatch(self.identity_sha256):
            raise ValueError("identity_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class EvidenceCompatibilityResult:
    compatible: bool
    reason: EvidenceCompatibilityReason
    left_purpose: EvidencePurpose
    right_purpose: EvidencePurpose
    same_identity: bool


def purposes_compatible(
    left: EvidencePurpose | str, right: EvidencePurpose | str
) -> bool:
    """Purpose equality is mandatory and never treated as a wildcard."""

    return validate_evidence_purpose(left) is validate_evidence_purpose(right)


def compare_evidence_compatibility(
    left: EvidenceCompatibilityKey,
    right: EvidenceCompatibilityKey,
) -> EvidenceCompatibilityResult:
    """Compare identities with a fail-closed cross-purpose firewall."""

    same_purpose = left.purpose is right.purpose
    same_identity = left.identity_sha256 == right.identity_sha256
    if not same_purpose:
        reason = EvidenceCompatibilityReason.PURPOSE_MISMATCH
    elif not same_identity:
        reason = EvidenceCompatibilityReason.IDENTITY_MISMATCH
    else:
        reason = EvidenceCompatibilityReason.EXACT_MATCH
    return EvidenceCompatibilityResult(
        compatible=same_purpose and same_identity,
        reason=reason,
        left_purpose=left.purpose,
        right_purpose=right.purpose,
        same_identity=same_identity,
    )


__all__ = [
    "EvidenceCompatibilityKey",
    "EvidenceCompatibilityReason",
    "EvidenceCompatibilityResult",
    "EvidencePurpose",
    "EvidencePurposeBinding",
    "EvidenceRecordKind",
    "LEGACY_PURPOSE_ADAPTER_VERSION",
    "compare_evidence_compatibility",
    "legacy_purpose_view",
    "legacy_source_payload",
    "purposes_compatible",
    "require_evidence_purpose",
    "validate_evidence_purpose",
]
