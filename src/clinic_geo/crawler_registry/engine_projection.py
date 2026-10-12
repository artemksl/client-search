"""Project an approved v2 registry into the engine's flat capability shape.

The Node engine's ``capability-registry.json`` schema does NOT change. Python
projects one approved v2 registry (``crawler_registry.store`` envelope) into the
engine's existing flat shape so the engine consumes it unchanged:

* ``fetch_limits[]`` — one rule per crawler that carries a published fetch-size
  limit, in the exact field shape ``technical.js:fetchLimitRules`` accepts
  (``status``, ``resource_kind``, ``limit_bytes``, ``engineering_margin_bytes``,
  ``byte_count_convention``, ``source_url``, ``review_by`` …);
* ``agents[]`` — every crawler AND every policy-control token, preserving the
  engine's current field names (``name``/``purpose``/``status``) and carrying a
  ``kind`` field the engine ignores today but the registry semantics require
  recorded, so a control token is never silently indistinguishable from a fetcher.

Staleness is carried through the engine's EXISTING review-window logic: top-level
and per-rule ``review_by`` = approval time + a configurable window (default 7
days, the weekly cadence). An approved-but-overdue registry therefore makes the
A21-class checks ``could_not_assess`` via ``registryIsCurrent`` /
``fetchLimitRules`` — never a silent pass.

Determinism: every value derives from the approved envelope (fixed
``approved_at``/facts), never from wall-clock time, so the same approved version
projects to a byte-identical document.

Two v2 facts have no engine-published counterpart and are engine-side operational
defaults, applied here (not invented into the registry): the engineering margin
(a ratio of the published limit) and the byte-count convention (fixed to the
engine's ``uncompressed_response_body``). Both are recorded in ``provenance``.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional


DEFAULT_REVIEW_WINDOW_SECONDS = 7 * 24 * 3600  # weekly cadence
DEFAULT_ENGINEERING_MARGIN_RATIO = 0.75
RESOURCE_KIND = "initial_html_non_pdf"
BYTE_COUNT_CONVENTION = "uncompressed_response_body"
PROJECTED_FROM = "crawler-registry-v2"

# v2 status vocabulary -> engine agents vocabulary. The engine reacts to
# 'inactive' (an explicitly-listed inactive agent) and 'active'; v2 forbids
# 'inactive' and uses 'deprecated'. Without this bridge a deprecated crawler
# could never be represented to the engine — a silent loss of the A08/A09 signal.
_STATUS_TO_ENGINE = {"active": "active", "deprecated": "inactive"}


class ProjectionError(ValueError):
    """The approved envelope could not be projected to the engine shape."""


def _iso_utc(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().casefold()).strip("-") or "x"


def _engine_status(v2_status: str) -> str:
    if v2_status not in _STATUS_TO_ENGINE:
        raise ProjectionError(f"unmappable v2 status {v2_status!r}")
    return _STATUS_TO_ENGINE[v2_status]


def _engineering_margin(limit_bytes: int, ratio: float) -> int:
    margin = int(limit_bytes * ratio)
    if margin < 1:
        margin = 1
    if margin >= limit_bytes:
        margin = limit_bytes - 1
    return margin


def project_to_engine(
    approved: dict,
    *,
    review_window_seconds: int = DEFAULT_REVIEW_WINDOW_SECONDS,
    engineering_margin_ratio: float = DEFAULT_ENGINEERING_MARGIN_RATIO,
) -> dict:
    """Project one approved v2 envelope into the engine's flat registry.

    ``approved`` is the ``crawler_registry.store`` approved envelope
    ``{content_hash, approved_by, approved_at, registry}``. Deterministic: same
    envelope + same config -> byte-identical output.
    """

    for key in ("content_hash", "approved_by", "approved_at", "registry"):
        if key not in approved:
            raise ProjectionError(f"approved envelope missing {key!r}")
    registry = approved["registry"]
    if not isinstance(registry, dict) or "crawlers" not in registry:
        raise ProjectionError("approved envelope does not carry a v2 registry")

    approved_at = approved["approved_at"]
    reviewed_at_iso = _iso_utc(approved_at)
    review_by_iso = _iso_utc(float(approved_at) + review_window_seconds)

    agents: list[dict] = []
    fetch_limits: list[dict] = []

    for crawler in registry["crawlers"]:
        name = crawler["name"]["value"]
        provider = crawler["provider"]["value"]
        purpose = crawler["purpose"]["value"]
        status = _engine_status(crawler["status"]["value"])
        agents.append(
            {"name": name, "purpose": purpose, "status": status, "kind": "crawler"}
        )
        limit_fact = crawler["fetch_size_limit"]
        if limit_fact is None:
            continue
        limit_bytes = int(limit_fact["value"])
        fetch_limits.append(
            {
                "id": f"{_slug(provider)}-{_slug(name)}-fetch-limit",
                "provider": provider,
                "crawler": name,
                "purpose": purpose,
                "status": status,
                "resource_kind": RESOURCE_KIND,
                "limit_bytes": limit_bytes,
                "engineering_margin_bytes": _engineering_margin(
                    limit_bytes, engineering_margin_ratio
                ),
                "byte_count_convention": BYTE_COUNT_CONVENTION,
                "source_url": limit_fact["source_url"],
                "source_last_updated_at": limit_fact["retrieved_at"],
                "verified_effective_at": limit_fact["retrieved_at"],
                "reviewed_at": reviewed_at_iso,
                "review_by": review_by_iso,
            }
        )

    for token in registry["policy_control_tokens"]:
        agents.append(
            {
                "name": token["name"]["value"],
                # A control token is NOT a fetching agent: no fetch purpose is
                # asserted, so the engine never buckets it as retrieval/training.
                "purpose": None,
                "status": _engine_status(token["status"]["value"]),
                "kind": "policy_control_token",
                "controls_what": token["controls_what"]["value"],
            }
        )

    return {
        "registry_version": registry["registry_version"],
        "reviewed_at": reviewed_at_iso,
        "review_by": review_by_iso,
        "source_note": (
            "Projected from an approved crawler-registry v2 version; do not edit "
            "by hand. Regenerate by approving a new registry."
        ),
        "fetch_limits": fetch_limits,
        "agents": agents,
        "provenance": {
            "approved_hash": approved["content_hash"],
            "approved_at": reviewed_at_iso,
            "approved_by": approved["approved_by"],
            "projected_from": PROJECTED_FROM,
            "review_window_seconds": int(review_window_seconds),
            "engineering_margin_ratio": engineering_margin_ratio,
        },
    }


__all__ = [
    "DEFAULT_REVIEW_WINDOW_SECONDS",
    "DEFAULT_ENGINEERING_MARGIN_RATIO",
    "RESOURCE_KIND",
    "BYTE_COUNT_CONVENTION",
    "PROJECTED_FROM",
    "ProjectionError",
    "project_to_engine",
]
