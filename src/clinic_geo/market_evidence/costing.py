"""Pure, integer-only costing for Market Evidence collection previews.

The module deliberately knows nothing about jobs, collectors, databases, or
wall-clock execution.  It prices an already-derived count of irreversible live
collector actions using an immutable rate card.  Durations are a conservative
serial sum; a future concurrency policy must use a new version rather than
silently changing this estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from clinic_geo.integrity import canonical_json, canonical_sha256

from .contracts import SourcePlatform


MARKET_COLLECTION_RATE_SCHEMA = "market_evidence.collection_rate.v1"
MARKET_COST_MODEL_SCHEMA = "market_evidence.cost_model.v1"
MARKET_COST_ESTIMATE_SCHEMA = "market_evidence.cost_estimate.v1"


def _require_non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _platform_value(value: SourcePlatform | str, field_name: str) -> SourcePlatform:
    if isinstance(value, SourcePlatform):
        return value
    if isinstance(value, str):
        try:
            return SourcePlatform(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a Market Evidence platform") from exc
    raise TypeError(f"{field_name} must be a SourcePlatform")


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionActionRate:
    """The conservative price and duration of one live external action."""

    source_platform: SourcePlatform
    duration_seconds_per_action: int
    cost_minor_units_per_action: int
    schema_version: str = field(default=MARKET_COLLECTION_RATE_SCHEMA, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_platform",
            _platform_value(self.source_platform, "source_platform"),
        )
        _require_non_negative_integer(
            self.duration_seconds_per_action,
            "duration_seconds_per_action",
        )
        _require_non_negative_integer(
            self.cost_minor_units_per_action,
            "cost_minor_units_per_action",
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source_platform": self.source_platform.value,
            "duration_seconds_per_action": self.duration_seconds_per_action,
            "cost_minor_units_per_action": self.cost_minor_units_per_action,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionCostModel:
    """An immutable, versioned rate card used by one demand preview."""

    model_version: str
    currency_code: str
    rates: tuple[CollectionActionRate, ...]
    fixed_duration_seconds: int = 0
    fixed_cost_minor_units: int = 0
    schema_version: str = field(default=MARKET_COST_MODEL_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_text(self.model_version, "model_version")
        currency = _require_text(self.currency_code, "currency_code")
        if len(currency) != 3 or currency.upper() != currency:
            raise ValueError("currency_code must be an uppercase ISO 4217 code")
        if not isinstance(self.rates, tuple) or any(
            not isinstance(rate, CollectionActionRate) for rate in self.rates
        ):
            raise TypeError("rates must be a tuple of CollectionActionRate values")
        platforms = tuple(rate.source_platform for rate in self.rates)
        if len(set(platforms)) != len(platforms):
            raise ValueError("rates cannot contain duplicate platform entries")
        _require_non_negative_integer(
            self.fixed_duration_seconds,
            "fixed_duration_seconds",
        )
        _require_non_negative_integer(
            self.fixed_cost_minor_units,
            "fixed_cost_minor_units",
        )

    @property
    def model_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "currency_code": self.currency_code,
            "duration_aggregation": "serial_action_sum",
            "fixed_duration_seconds": self.fixed_duration_seconds,
            "fixed_cost_minor_units": self.fixed_cost_minor_units,
            "rates": [
                rate.to_canonical_dict()
                for rate in sorted(self.rates, key=lambda item: item.source_platform.value)
            ],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformCostBreakdown:
    source_platform: SourcePlatform
    live_collector_action_count: int
    duration_seconds: int
    cost_minor_units: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_platform",
            _platform_value(self.source_platform, "source_platform"),
        )
        _require_non_negative_integer(
            self.live_collector_action_count,
            "live_collector_action_count",
        )
        _require_non_negative_integer(self.duration_seconds, "duration_seconds")
        _require_non_negative_integer(self.cost_minor_units, "cost_minor_units")

    def to_canonical_dict(self) -> dict:
        return {
            "source_platform": self.source_platform.value,
            "live_collector_action_count": self.live_collector_action_count,
            "duration_seconds": self.duration_seconds,
            "cost_minor_units": self.cost_minor_units,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionCostEstimate:
    """Exact estimate returned with, and bound into, a demand plan."""

    cost_model_version: str
    cost_model_sha256: str
    currency_code: str
    live_collector_action_count: int
    estimated_duration_seconds: int
    estimated_cost_minor_units: int
    platform_breakdown: tuple[PlatformCostBreakdown, ...]
    fixed_duration_seconds: int = 0
    fixed_cost_minor_units: int = 0
    schema_version: str = field(default=MARKET_COST_ESTIMATE_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _require_text(self.cost_model_version, "cost_model_version")
        if (
            not isinstance(self.cost_model_sha256, str)
            or len(self.cost_model_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.cost_model_sha256)
        ):
            raise ValueError("cost_model_sha256 must be a lowercase SHA-256")
        currency = _require_text(self.currency_code, "currency_code")
        if len(currency) != 3 or currency.upper() != currency:
            raise ValueError("currency_code must be an uppercase ISO 4217 code")
        _require_non_negative_integer(
            self.live_collector_action_count,
            "live_collector_action_count",
        )
        _require_non_negative_integer(
            self.estimated_duration_seconds,
            "estimated_duration_seconds",
        )
        _require_non_negative_integer(
            self.estimated_cost_minor_units,
            "estimated_cost_minor_units",
        )
        _require_non_negative_integer(
            self.fixed_duration_seconds,
            "fixed_duration_seconds",
        )
        _require_non_negative_integer(
            self.fixed_cost_minor_units,
            "fixed_cost_minor_units",
        )
        if not isinstance(self.platform_breakdown, tuple) or any(
            not isinstance(row, PlatformCostBreakdown)
            for row in self.platform_breakdown
        ):
            raise TypeError(
                "platform_breakdown must be a tuple of PlatformCostBreakdown values"
            )
        platforms = tuple(row.source_platform for row in self.platform_breakdown)
        if len(set(platforms)) != len(platforms):
            raise ValueError("platform_breakdown cannot repeat a platform")
        if sum(row.live_collector_action_count for row in self.platform_breakdown) != (
            self.live_collector_action_count
        ):
            raise ValueError("platform action counts do not match the estimate total")
        expected_duration = self.fixed_duration_seconds + sum(
            row.duration_seconds for row in self.platform_breakdown
        )
        expected_cost = self.fixed_cost_minor_units + sum(
            row.cost_minor_units for row in self.platform_breakdown
        )
        if self.estimated_duration_seconds != expected_duration:
            raise ValueError(
                "estimated_duration_seconds must equal breakdown plus fixed overhead"
            )
        if self.estimated_cost_minor_units != expected_cost:
            raise ValueError(
                "estimated_cost_minor_units must equal breakdown plus fixed overhead"
            )
        if not self.live_collector_action_count and (
            self.fixed_duration_seconds or self.fixed_cost_minor_units
        ):
            raise ValueError("fixed overhead cannot apply when no live actions exist")

    @property
    def estimate_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "cost_model_version": self.cost_model_version,
            "cost_model_sha256": self.cost_model_sha256,
            "currency_code": self.currency_code,
            "duration_aggregation": "serial_action_sum",
            "fixed_duration_seconds": self.fixed_duration_seconds,
            "fixed_cost_minor_units": self.fixed_cost_minor_units,
            "live_collector_action_count": self.live_collector_action_count,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "estimated_cost_minor_units": self.estimated_cost_minor_units,
            "platform_breakdown": [
                row.to_canonical_dict()
                for row in sorted(
                    self.platform_breakdown,
                    key=lambda item: item.source_platform.value,
                )
            ],
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_canonical_dict())


def estimate_collection_cost(
    action_counts: Mapping[SourcePlatform | str, int],
    cost_model: CollectionCostModel,
) -> CollectionCostEstimate:
    """Price exact live-action counts using ``cost_model``.

    Fixed overhead applies only when at least one live collector action is
    required.  A non-zero action count without an explicit platform rate fails
    closed instead of silently treating the work as free.
    """

    if not isinstance(action_counts, Mapping):
        raise TypeError("action_counts must be a mapping")
    if not isinstance(cost_model, CollectionCostModel):
        raise TypeError("cost_model must be a CollectionCostModel")

    normalized: dict[SourcePlatform, int] = {}
    for raw_platform, raw_count in action_counts.items():
        platform = _platform_value(raw_platform, "action_counts platform")
        count = _require_non_negative_integer(raw_count, "action count")
        if platform in normalized:
            raise ValueError("action_counts cannot repeat a normalized platform")
        normalized[platform] = count

    rate_by_platform = {rate.source_platform: rate for rate in cost_model.rates}
    missing_rates = sorted(
        platform.value
        for platform, count in normalized.items()
        if count and platform not in rate_by_platform
    )
    if missing_rates:
        raise ValueError(f"cost model has no rate for live platforms: {missing_rates}")

    breakdown: list[PlatformCostBreakdown] = []
    for platform in sorted(normalized, key=lambda item: item.value):
        count = normalized[platform]
        if not count:
            continue
        rate = rate_by_platform[platform]
        breakdown.append(
            PlatformCostBreakdown(
                source_platform=platform,
                live_collector_action_count=count,
                duration_seconds=count * rate.duration_seconds_per_action,
                cost_minor_units=count * rate.cost_minor_units_per_action,
            )
        )

    action_count = sum(row.live_collector_action_count for row in breakdown)
    fixed_duration = cost_model.fixed_duration_seconds if action_count else 0
    fixed_cost = cost_model.fixed_cost_minor_units if action_count else 0
    return CollectionCostEstimate(
        cost_model_version=cost_model.model_version,
        cost_model_sha256=cost_model.model_sha256,
        currency_code=cost_model.currency_code,
        live_collector_action_count=action_count,
        estimated_duration_seconds=(
            fixed_duration + sum(row.duration_seconds for row in breakdown)
        ),
        estimated_cost_minor_units=(
            fixed_cost + sum(row.cost_minor_units for row in breakdown)
        ),
        platform_breakdown=tuple(breakdown),
        fixed_duration_seconds=fixed_duration,
        fixed_cost_minor_units=fixed_cost,
    )


__all__ = [
    "MARKET_COLLECTION_RATE_SCHEMA",
    "MARKET_COST_ESTIMATE_SCHEMA",
    "MARKET_COST_MODEL_SCHEMA",
    "CollectionActionRate",
    "CollectionCostEstimate",
    "CollectionCostModel",
    "PlatformCostBreakdown",
    "estimate_collection_cost",
]
