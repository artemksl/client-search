"""Market-evidence job reads and bounded pending-job cancellation.

The correlated job-view query deliberately includes one narrow, read-only
reference to ``campaign_revisions.state`` so its other-active-consumer count is
computed in the same SQLite statement and therefore the same snapshot.  This
is an ownership-boundary exception for that single lifecycle column; campaign
code still delegates all SQL over market-evidence tables to this module.
"""

from __future__ import annotations

import sqlite3


def job_views_for_revision(
    connection: sqlite3.Connection,
    campaign_revision_id: str,
) -> tuple[sqlite3.Row, ...]:
    """Return a revision's jobs with their other active consumer counts."""

    rows = connection.execute(
        """
        SELECT job.job_id AS job_id, job.state AS state,
               (
                 SELECT count(*)
                 FROM market_demand_consumers other
                 JOIN campaign_revisions other_revision
                   ON other_revision.id=other.campaign_revision_id
                 WHERE other.plan_sha256=job.plan_sha256
                   AND other.campaign_revision_id<>?
                   AND other_revision.state='active'
               ) AS other_active_consumers
        FROM market_collection_jobs job
        WHERE job.plan_sha256 IN (
            SELECT plan_sha256 FROM market_demand_consumers WHERE campaign_revision_id=?
        )
        """,
        (campaign_revision_id, campaign_revision_id),
    ).fetchall()
    return tuple(rows)


def cancel_job_if_pending(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    now_iso: str,
) -> int:
    """Cancel one queued/retry-wait job and return the affected-row count."""

    cursor = connection.execute(
        """
        UPDATE market_collection_jobs
        SET state='cancelled',
            next_attempt_at=NULL,
            state_version=state_version+1,
            updated_at=?
        WHERE job_id=? AND state IN ('queued','retry_wait')
        """,
        (now_iso, job_id),
    )
    return cursor.rowcount


__all__ = ["cancel_job_if_pending", "job_views_for_revision"]
