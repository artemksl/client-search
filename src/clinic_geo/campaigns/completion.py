"""Campaign completion: pure blockers, immutable records, bounded cancellation.

Completion is refused while relevant work is actively running.  When it does
proceed it cancels only unstarted local collection jobs that have no other
active campaign consumer, appends an immutable completion record, and marks the
revision completed.  It never touches global outreach, evidence, projection, or
another campaign's demand.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from clinic_geo.integrity import canonical_sha256
from clinic_geo.market_evidence.jobs import CollectionJobState
from clinic_geo.market_evidence.jobs_read_models import (
    cancel_job_if_pending,
    job_views_for_revision,
)

from .contracts import CampaignRevisionState
from .runtime import CampaignRuntimeError


Clock = Callable[[], datetime]

# A leased job is running; a send-unknown job is in an unresolved send state.
# Either is "relevant running work" that must block completion.
_BLOCKING_STATES = frozenset(
    {CollectionJobState.LEASED, CollectionJobState.SEND_UNKNOWN}
)
# Only genuinely unstarted work may be cancelled by completion.
_CANCELLABLE_STATES = frozenset(
    {CollectionJobState.QUEUED, CollectionJobState.RETRY_WAIT}
)


class CampaignCompletionBlocked(CampaignRuntimeError):
    """Completion cannot proceed because relevant work is still running."""

    def __init__(self, blocker_job_ids: tuple[str, ...]) -> None:
        super().__init__(
            "campaign completion is blocked by running work: "
            + ", ".join(blocker_job_ids)
        )
        self.blocker_job_ids = blocker_job_ids


class CampaignCompletionConflict(CampaignRuntimeError):
    """A completion replay attested a different reason."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CampaignRuntimeError("campaign timestamps must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class CampaignJobView:
    """One local collection job as seen by completion planning.

    ``other_active_consumers`` counts campaign revisions other than the one being
    completed that are active and consume this job's demand plan.
    """

    job_id: str
    state: CollectionJobState
    other_active_consumers: int

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise CampaignRuntimeError("job_id must be non-empty text")
        if not isinstance(self.state, CollectionJobState):
            raise CampaignRuntimeError("state must be a CollectionJobState")
        if (
            isinstance(self.other_active_consumers, bool)
            or not isinstance(self.other_active_consumers, int)
            or self.other_active_consumers < 0
        ):
            raise CampaignRuntimeError("other_active_consumers must be a non-negative int")


@dataclass(frozen=True, slots=True)
class CompletionPlan:
    blockers: tuple[str, ...]
    cancellations: tuple[str, ...]

    @property
    def is_blocked(self) -> bool:
        return bool(self.blockers)

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": "campaign.completion_plan.v1",
            "blockers": list(self.blockers),
            "cancellations": list(self.cancellations),
        }


def plan_campaign_completion(
    job_views: tuple[CampaignJobView, ...],
) -> CompletionPlan:
    """Pure completion planner over the revision's local job views.

    Running/unresolved work blocks completion.  When nothing blocks, only
    unstarted jobs with no other active consumer are scheduled for cancellation;
    an unstarted job still needed by another active campaign is left untouched.
    """

    views = tuple(job_views)
    if any(not isinstance(view, CampaignJobView) for view in views):
        raise CampaignRuntimeError("job_views must contain CampaignJobView values")
    blockers = tuple(
        sorted(view.job_id for view in views if view.state in _BLOCKING_STATES)
    )
    if blockers:
        # Never cancel anything while blocked; completion will be refused.
        return CompletionPlan(blockers=blockers, cancellations=())
    cancellations = tuple(
        sorted(
            view.job_id
            for view in views
            if view.state in _CANCELLABLE_STATES and view.other_active_consumers == 0
        )
    )
    return CompletionPlan(blockers=(), cancellations=cancellations)


def collect_campaign_job_views(
    connection: sqlite3.Connection,
    campaign_revision_id: str,
) -> tuple[CampaignJobView, ...]:
    """Read the revision's local collection jobs and their other active consumers."""

    rows = job_views_for_revision(connection, campaign_revision_id)
    return tuple(
        CampaignJobView(
            job_id=row["job_id"],
            state=CollectionJobState(row["state"]),
            other_active_consumers=row["other_active_consumers"],
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class CampaignCompletionResult:
    revision_id: str
    campaign_id: str
    state: str
    completed_at: str
    completion_reason: str
    payload_sha256: str
    cancelled_job_ids: tuple[str, ...]
    replayed: bool


def _completion_payload_sha256(
    *, revision_id: str, completed_by: str, completion_reason: str, completed_at: str
) -> str:
    return canonical_sha256(
        {
            "schema_version": "campaign.completion_record.v1",
            "campaign_revision_id": revision_id,
            "completed_by": completed_by,
            "completion_reason": completion_reason,
            "completed_at": completed_at,
        }
    )


def complete_campaign_revision(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    completed_by: str,
    completion_reason: str,
    clock: Clock = _utc_now,
) -> CampaignCompletionResult:
    """Atomically complete an active revision if no running work blocks it.

    Cancels only unstarted local jobs with no other active consumer, appends an
    immutable completion record, and marks the revision completed.  Global
    outreach and every evidence/projection row are untouched.  A replay of a
    completed revision returns its record; a different reason is a conflict.
    """

    for name, value in (
        ("revision_id", revision_id),
        ("completed_by", completed_by),
        ("completion_reason", completion_reason),
    ):
        if not isinstance(value, str) or not value.strip():
            raise CampaignRuntimeError(f"{name} must be non-empty text")
    if connection.in_transaction:
        raise CampaignRuntimeError(
            "campaign completion requires an independent top-level transaction"
        )

    now = _iso(clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        revision = connection.execute(
            "SELECT id,campaign_id,state,completed_at FROM campaign_revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        if revision is None:
            raise CampaignRuntimeError("campaign revision does not exist")
        if revision["state"] == CampaignRevisionState.COMPLETED.value:
            record = connection.execute(
                "SELECT completion_reason,completed_at,payload_sha256 "
                "FROM campaign_completion_records WHERE campaign_revision_id=?",
                (revision_id,),
            ).fetchone()
            if record is None or record["completion_reason"] != completion_reason:
                raise CampaignCompletionConflict(
                    "campaign revision already completed with a different reason"
                )
            connection.commit()
            return CampaignCompletionResult(
                revision_id=revision_id,
                campaign_id=revision["campaign_id"],
                state=CampaignRevisionState.COMPLETED.value,
                completed_at=record["completed_at"],
                completion_reason=completion_reason,
                payload_sha256=record["payload_sha256"],
                cancelled_job_ids=(),
                replayed=True,
            )
        if revision["state"] != CampaignRevisionState.ACTIVE.value:
            raise CampaignRuntimeError("only an active campaign revision can be completed")

        plan = plan_campaign_completion(
            collect_campaign_job_views(connection, revision_id)
        )
        if plan.is_blocked:
            connection.rollback()
            raise CampaignCompletionBlocked(plan.blockers)

        for job_id in plan.cancellations:
            cancel_job_if_pending(connection, job_id, now_iso=now)
        # The completion record must exist before the revision transitions to
        # completed: a migration-008 trigger refuses the transition otherwise.
        payload_sha256 = _completion_payload_sha256(
            revision_id=revision_id,
            completed_by=completed_by,
            completion_reason=completion_reason,
            completed_at=now,
        )
        connection.execute(
            """
            INSERT INTO campaign_completion_records
              (id,campaign_revision_id,completed_by,completed_at,
               completion_reason,payload_sha256)
            VALUES (?,?,?,?,?,?)
            """,
            (
                f"campaign-completion-{payload_sha256}",
                revision_id,
                completed_by,
                now,
                completion_reason,
                payload_sha256,
            ),
        )
        connection.execute(
            """
            UPDATE campaign_revisions
            SET state='completed', completed_at=?
            WHERE id=? AND state='active'
            """,
            (now, revision_id),
        )
        connection.commit()
        return CampaignCompletionResult(
            revision_id=revision_id,
            campaign_id=revision["campaign_id"],
            state=CampaignRevisionState.COMPLETED.value,
            completed_at=now,
            completion_reason=completion_reason,
            payload_sha256=payload_sha256,
            cancelled_job_ids=plan.cancellations,
            replayed=False,
        )
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


__all__ = [
    "CampaignCompletionBlocked",
    "CampaignCompletionConflict",
    "CampaignCompletionResult",
    "CampaignJobView",
    "CompletionPlan",
    "collect_campaign_job_views",
    "complete_campaign_revision",
    "plan_campaign_completion",
]
