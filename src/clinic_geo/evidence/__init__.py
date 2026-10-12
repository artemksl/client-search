"""Closed evidence-purpose contracts."""

from .purpose import (
    EvidenceCompatibilityKey,
    EvidenceCompatibilityReason,
    EvidenceCompatibilityResult,
    EvidencePurpose,
    EvidencePurposeBinding,
    EvidenceRecordKind,
    LEGACY_PURPOSE_ADAPTER_VERSION,
    compare_evidence_compatibility,
    legacy_purpose_view,
    legacy_source_payload,
    purposes_compatible,
    require_evidence_purpose,
    validate_evidence_purpose,
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
