"""Project collection dates from recorded evidence, without inventing dates.

The engine owns capture and recovery lineage.  This projection deliberately
does not consult the clock, filesystem modification times or job start times.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime


class CollectionPeriodError(ValueError):
    """A recorded collection timestamp cannot be represented truthfully."""


def _timestamp(value: object) -> tuple[datetime, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CollectionPeriodError("collection timestamp must be timezone-aware text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionPeriodError("invalid collection timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollectionPeriodError("collection timestamp must include its timezone")
    return parsed, value


def collection_period(observations: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Keep exact per-item lineage and summarize the actual observed interval."""

    captures: list[tuple[datetime, str]] = []
    retrievals: list[tuple[datetime, str]] = []
    rows: list[dict[str, object]] = []
    missing = 0
    for observation in observations:
        evidence = observation.get("capture_evidence", {})
        collection = evidence.get("collection", {}) if isinstance(evidence, Mapping) else {}
        if not isinstance(collection, Mapping):
            raise CollectionPeriodError("collection lineage must be an object")
        row: dict[str, object] = {"observation_id": observation.get("observation_id")}
        for key in ("captured_at", "original_captured_at", "retrieved_at", "recovered_at", "provider_generated_at"):
            stamp = _timestamp(collection.get(key))
            if stamp is not None:
                row[key] = stamp[1]
                if key in {"captured_at", "original_captured_at"}:
                    captures.append(stamp)
                elif key in {"retrieved_at", "recovered_at"}:
                    retrievals.append(stamp)
        if "captured_at" not in row and "original_captured_at" not in row:
            missing += 1
        for key in ("recovery_pass_id", "result_revision_id", "prior_revision_id", "source_attempt_id", "parser_version", "validator_version"):
            value = collection.get(key)
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 256:
                    raise CollectionPeriodError("invalid collection lineage identifier")
                row[key] = value
        rows.append(row)
    observed = captures + retrievals
    return {
        "schema_version": "visibility-collection-period-v1",
        "started_at": min(observed, key=lambda row: row[0])[1] if observed else None,
        "ended_at": max(observed, key=lambda row: row[0])[1] if observed else None,
        "capture_started_at": min(captures, key=lambda row: row[0])[1] if captures else None,
        "capture_ended_at": max(captures, key=lambda row: row[0])[1] if captures else None,
        "retrieval_ended_at": max(retrievals, key=lambda row: row[0])[1] if retrievals else None,
        "missing_capture_times": missing,
        "observation_count": len(rows),
        "observations": rows,
    }


def collection_statement(period: Mapping[str, object]) -> str:
    start, end = period.get("started_at"), period.get("ended_at")
    if start is None or end is None:
        return "Collection period: capture times were not recorded in the accepted evidence."
    statement = f"Collection period: {start} to {end} (recorded captures and retrievals)."
    missing = period.get("missing_capture_times", 0)
    if missing:
        statement += f" Original capture time is unavailable for {missing} measurement(s)."
    return statement
