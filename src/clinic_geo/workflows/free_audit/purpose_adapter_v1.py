"""Purpose boundary and explicit legacy views for Free Snapshot artifacts.

This module intentionally depends only on the shared evidence-purpose contract.
It must never import Market Evidence: publication may reject Market identifiers,
but it cannot reinterpret or consume Market artifacts.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from clinic_geo.evidence.purpose import (
    EvidencePurpose,
    legacy_purpose_view,
    require_evidence_purpose,
)


FREE_SNAPSHOT_PURPOSE = EvidencePurpose.FREE_SNAPSHOT_CONFIRMATION
SNAPSHOT_PURPOSE_ADAPTER_VERSION = "free-snapshot-purpose-adapter-v1"

FORBIDDEN_MARKET_REFERENCE_KEYS = frozenset(
    {
        "market_observation_id",
        "market_observation_ids",
        "market_artifact_id",
        "market_artifact_ids",
        "market_projection_id",
        "market_projection_ids",
    }
)


class SnapshotPurposeError(ValueError):
    """A Snapshot artifact crossed or omitted its evidence-purpose boundary."""


def reject_market_evidence_references(value: object, *, label: str) -> None:
    """Reject Market observation, artifact, or projection IDs at any depth."""

    def walk(item: object, path: str) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                child_path = f"{path}.{key}" if path else key
                if key in FORBIDDEN_MARKET_REFERENCE_KEYS:
                    raise SnapshotPurposeError(
                        f"{label} contains forbidden Market Evidence reference {child_path}"
                    )
                walk(child, child_path)
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")

    walk(value, "")


def validate_current_snapshot_artifact(value: Mapping, *, label: str) -> dict:
    """Validate a current, explicitly purpose-bound Snapshot mapping."""

    reject_market_evidence_references(value, label=label)
    try:
        require_evidence_purpose(value, FREE_SNAPSHOT_PURPOSE, label=label)
    except (TypeError, ValueError) as exc:
        raise SnapshotPurposeError(str(exc)) from exc
    return deepcopy(dict(value))


def adapt_legacy_snapshot_artifact(
    value: Mapping,
    *,
    label: str,
    artifact_kind: str,
    supported_schema_versions: tuple[str, ...] | frozenset[str],
    schema_field: str = "schema_version",
) -> dict:
    """Return the sole allowlisted read view for a purpose-less Snapshot file."""

    reject_market_evidence_references(value, label=label)
    try:
        adapted = legacy_purpose_view(
            value,
            FREE_SNAPSHOT_PURPOSE,
            artifact_kind=artifact_kind,
            supported_schema_versions=supported_schema_versions,
            schema_field=schema_field,
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotPurposeError(str(exc)) from exc
    compatibility = dict(adapted.get("purpose_compatibility") or {})
    compatibility["snapshot_adapter_version"] = SNAPSHOT_PURPOSE_ADAPTER_VERSION
    adapted["purpose_compatibility"] = compatibility
    return adapted


__all__ = [
    "FORBIDDEN_MARKET_REFERENCE_KEYS",
    "FREE_SNAPSHOT_PURPOSE",
    "SNAPSHOT_PURPOSE_ADAPTER_VERSION",
    "SnapshotPurposeError",
    "adapt_legacy_snapshot_artifact",
    "reject_market_evidence_references",
    "validate_current_snapshot_artifact",
]
