"""Pure immutable state contracts for branch-scoped campaign revisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from clinic_geo.query_geography.contracts import QueryGeographyLevel


CAMPAIGN_REVISION_SCHEMA = "campaign.revision.v1"
CAMPAIGN_FOUNDATION_SCHEMA = "campaign.foundation.v2"
CAMPAIGN_QUERY_SCOPE_SCHEMA = "campaign.query_scope.v1"
_SHA256_RE_LENGTH = 64


class CampaignRevisionState(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"


class CampaignQueryScopeStatus(str, Enum):
    UNSCOPED = "unscoped"
    APPROVED = "approved"


class CampaignIntentSelectionAction(str, Enum):
    ADD = "add"
    DEFER = "defer"


class CampaignCandidateDisposition(str, Enum):
    SHORTLIST = "shortlist"
    RESEARCH_FIRST = "research_first"
    DEFER = "defer"
    EXCLUDE = "exclude"


_ALLOWED_TRANSITIONS = frozenset(
    {
        (CampaignRevisionState.PLANNED, CampaignRevisionState.ACTIVE),
        (CampaignRevisionState.ACTIVE, CampaignRevisionState.COMPLETED),
    }
)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")


def _require_sha256(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if len(value) != _SHA256_RE_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True, kw_only=True)
class CampaignQueryScopeContract:
    level: QueryGeographyLevel
    query_geography_version_row_id: str
    schema_version: str = field(default=CAMPAIGN_QUERY_SCOPE_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.level, QueryGeographyLevel):
            raise TypeError("level must be a QueryGeographyLevel")
        _require_text(
            self.query_geography_version_row_id,
            "query_geography_version_row_id",
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "level": self.level.value,
            "query_geography_version_row_id": self.query_geography_version_row_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CampaignFoundationContract:
    """One reviewed, planned revision used to establish campaign identity."""

    campaign_id: str
    revision_id: str
    revision_number: int
    territory_id: str
    territory_taxonomy_version: str
    state: CampaignRevisionState
    query_scope_status: CampaignQueryScopeStatus
    query_geography_release_id: str | None
    query_geography_payload_sha256: str | None
    query_scopes: tuple[CampaignQueryScopeContract, ...]
    foundation_manifest_sha256: str
    schema_version: str = field(default=CAMPAIGN_FOUNDATION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "campaign_id",
            "revision_id",
            "territory_id",
            "territory_taxonomy_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if (
            not isinstance(self.revision_number, int)
            or isinstance(self.revision_number, bool)
            or self.revision_number < 1
        ):
            raise ValueError("revision_number must be an integer >= 1")
        if self.state is not CampaignRevisionState.PLANNED:
            raise ValueError("campaign foundations must remain planned")
        if not isinstance(self.query_scope_status, CampaignQueryScopeStatus):
            raise TypeError("query_scope_status must be a CampaignQueryScopeStatus")
        if not isinstance(self.query_scopes, tuple):
            raise TypeError("query_scopes must be an immutable tuple")
        if any(
            not isinstance(scope, CampaignQueryScopeContract)
            for scope in self.query_scopes
        ):
            raise TypeError("query_scopes must contain CampaignQueryScopeContract")
        _require_sha256(
            self.foundation_manifest_sha256,
            "foundation_manifest_sha256",
        )

        if self.query_scope_status is CampaignQueryScopeStatus.UNSCOPED:
            if (
                self.query_geography_release_id is not None
                or self.query_geography_payload_sha256 is not None
                or self.query_scopes
            ):
                raise ValueError(
                    "unscoped campaigns cannot pin geography bytes or scopes"
                )
            return

        if (
            self.query_geography_release_id is None
            or self.query_geography_payload_sha256 is None
        ):
            raise ValueError(
                "approved query scopes require a geography release and payload hash"
            )
        _require_text(
            self.query_geography_release_id,
            "query_geography_release_id",
        )
        _require_sha256(
            self.query_geography_payload_sha256,
            "query_geography_payload_sha256",
        )
        levels = [scope.level for scope in self.query_scopes]
        required = {
            QueryGeographyLevel.LOCAL,
            QueryGeographyLevel.GREATER_AREA,
            QueryGeographyLevel.CITY,
        }
        if len(levels) != 3 or set(levels) != required:
            raise ValueError(
                "approved MVP query scopes require exactly one local, "
                "greater-area, and city scope"
            )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "territory_id": self.territory_id,
            "territory_taxonomy_version": self.territory_taxonomy_version,
            "state": self.state.value,
            "query_scope_status": self.query_scope_status.value,
            "query_geography_release_id": self.query_geography_release_id,
            "query_geography_payload_sha256": (
                self.query_geography_payload_sha256
            ),
            "query_scopes": [scope.to_canonical_dict() for scope in self.query_scopes],
            "foundation_manifest_sha256": self.foundation_manifest_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CampaignRevisionContract:
    campaign_id: str
    revision_id: str
    revision_number: int
    territory_id: str
    territory_taxonomy_version: str
    query_geography_release_id: str
    treatment_catalogue_release_id: str
    freshness_policy_version: str
    state: CampaignRevisionState
    parent_revision_id: str | None = None
    schema_version: str = field(default=CAMPAIGN_REVISION_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "campaign_id",
            "revision_id",
            "territory_id",
            "territory_taxonomy_version",
            "query_geography_release_id",
            "treatment_catalogue_release_id",
            "freshness_policy_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.revision_number, int) or isinstance(
            self.revision_number, bool
        ) or self.revision_number < 1:
            raise ValueError("revision_number must be an integer >= 1")
        if not isinstance(self.state, CampaignRevisionState):
            raise TypeError("state must be a CampaignRevisionState")
        if self.parent_revision_id is not None:
            _require_text(self.parent_revision_id, "parent_revision_id")
            if self.parent_revision_id == self.revision_id:
                raise ValueError("a campaign revision cannot be its own parent")


def can_transition_campaign_revision(
    current: CampaignRevisionState,
    target: CampaignRevisionState,
) -> bool:
    if not isinstance(current, CampaignRevisionState) or not isinstance(
        target, CampaignRevisionState
    ):
        raise TypeError("campaign revision transitions require closed state values")
    return (current, target) in _ALLOWED_TRANSITIONS


__all__ = [
    "CAMPAIGN_FOUNDATION_SCHEMA",
    "CAMPAIGN_QUERY_SCOPE_SCHEMA",
    "CAMPAIGN_REVISION_SCHEMA",
    "CampaignFoundationContract",
    "CampaignCandidateDisposition",
    "CampaignIntentSelectionAction",
    "CampaignQueryScopeContract",
    "CampaignQueryScopeStatus",
    "CampaignRevisionContract",
    "CampaignRevisionState",
    "can_transition_campaign_revision",
]
