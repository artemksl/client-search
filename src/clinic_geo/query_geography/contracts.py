"""Pure immutable contracts for patient-facing query geographies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


QUERY_GEOGRAPHY_VERSION_SCHEMA = "query_geography.version.v1"
QUERY_GEOGRAPHY_RELEASE_SCHEMA = "query_geography.release.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class QueryGeographyLevel(str, Enum):
    LOCAL = "local"
    GREATER_AREA = "greater_area"
    CITY = "city"


class QueryGeographyReleaseState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryGeographyVersion:
    """One immutable row from a versioned patient-query geography registry."""

    query_geography_id: str
    version_row_id: str
    level: QueryGeographyLevel
    rendered_name: str
    country_code: str
    registry_version: str
    registry_payload_sha256: str
    schema_version: str = field(default=QUERY_GEOGRAPHY_VERSION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "query_geography_id",
            "version_row_id",
            "rendered_name",
            "registry_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.level, QueryGeographyLevel):
            raise TypeError("level must be a QueryGeographyLevel")
        if not _COUNTRY_RE.fullmatch(self.country_code):
            raise ValueError("country_code must be an uppercase ISO alpha-2 code")
        if not _SHA256_RE.fullmatch(self.registry_payload_sha256):
            raise ValueError("registry_payload_sha256 must be a lowercase SHA-256")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "query_geography_id": self.query_geography_id,
            "version_row_id": self.version_row_id,
            "level": self.level.value,
            "rendered_name": self.rendered_name,
            "country_code": self.country_code,
            "registry_version": self.registry_version,
            "registry_payload_sha256": self.registry_payload_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryGeographyReleaseContract:
    """Identity and approval state for one immutable registry payload."""

    release_id: str
    registry_version: str
    payload_sha256: str
    state: QueryGeographyReleaseState
    approved_by: str | None = None
    approved_at: str | None = None
    schema_version: str = field(
        default=QUERY_GEOGRAPHY_RELEASE_SCHEMA,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_text(self.release_id, "release_id")
        _require_text(self.registry_version, "registry_version")
        if not _SHA256_RE.fullmatch(self.payload_sha256):
            raise ValueError("payload_sha256 must be a lowercase SHA-256")
        if not isinstance(self.state, QueryGeographyReleaseState):
            raise TypeError("state must be a QueryGeographyReleaseState")
        if self.state is QueryGeographyReleaseState.APPROVED:
            if self.approved_by is None or self.approved_at is None:
                raise ValueError("approved releases require approved_by and approved_at")
        elif self.approved_by is not None or self.approved_at is not None:
            raise ValueError("draft releases cannot carry approval metadata")
        if self.approved_by is not None:
            _require_text(self.approved_by, "approved_by")
        if self.approved_at is not None:
            _require_text(self.approved_at, "approved_at")

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "registry_version": self.registry_version,
            "payload_sha256": self.payload_sha256,
            "state": self.state.value,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


QueryGeographyContract = QueryGeographyVersion


__all__ = [
    "QUERY_GEOGRAPHY_RELEASE_SCHEMA",
    "QUERY_GEOGRAPHY_VERSION_SCHEMA",
    "QueryGeographyContract",
    "QueryGeographyLevel",
    "QueryGeographyReleaseContract",
    "QueryGeographyReleaseState",
    "QueryGeographyVersion",
]
