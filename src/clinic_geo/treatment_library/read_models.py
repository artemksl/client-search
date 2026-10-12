"""Production-safe, read-only access to treatment-library release state."""

from __future__ import annotations

import sqlite3


def catalogue_release_state(
    connection: sqlite3.Connection,
    release_id: str,
) -> dict[str, str] | None:
    """Return the lifecycle/review state for a catalogue release, if present."""

    row = connection.execute(
        "SELECT lifecycle_state,review_state FROM treatment_catalog_releases WHERE id=?",
        (release_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "lifecycle_state": row[0],
        "review_state": row[1],
    }


__all__ = ["catalogue_release_state"]
