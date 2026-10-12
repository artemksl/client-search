"""Pure campaign revision contracts.

Campaign HTTP persistence is owned by :mod:`clinic_geo.campaigns.api_store`;
the web layer remains an application adapter rather than a SQL owner.
"""

from .contracts import (
    CAMPAIGN_FOUNDATION_SCHEMA,
    CAMPAIGN_QUERY_SCOPE_SCHEMA,
    CAMPAIGN_REVISION_SCHEMA,
    CampaignFoundationContract,
    CampaignQueryScopeContract,
    CampaignQueryScopeStatus,
    CampaignRevisionContract,
    CampaignRevisionState,
    can_transition_campaign_revision,
)

__all__ = [
    "CAMPAIGN_FOUNDATION_SCHEMA",
    "CAMPAIGN_QUERY_SCOPE_SCHEMA",
    "CAMPAIGN_REVISION_SCHEMA",
    "CampaignFoundationContract",
    "CampaignQueryScopeContract",
    "CampaignQueryScopeStatus",
    "CampaignRevisionContract",
    "CampaignRevisionState",
    "can_transition_campaign_revision",
]
