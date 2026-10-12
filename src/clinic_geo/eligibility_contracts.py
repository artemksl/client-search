"""Neutral eligibility contracts shared across application contexts."""

from enum import Enum


class EligibilityStatus(str, Enum):
    """The closed Market Evidence eligibility vocabulary."""

    HIT = "hit"
    MISS = "miss"
    STALE = "stale"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"
    SUPERSEDED = "superseded"
