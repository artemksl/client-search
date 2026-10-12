"""Campaign read models: independently derived dimensions, never a total score.

Every dimension below is derived on its own from typed Market Evidence inputs
(an ``EligibilityDecision`` and an optional ``ProjectionResult``) plus collection
job states.  They are deliberately NOT combined into a single ranking or score:
the operator sees eligibility, freshness, reuse, job state, appearance, evidence
date, warnings, and visibility impact side by side.  This module reads nothing
and mutates nothing; it consumes already-computed evidence values by value.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from clinic_geo.market_evidence.eligibility import (
    EligibilityDecision,
    EligibilityStatus,
)
from clinic_geo.market_evidence.jobs import (
    TERMINAL_JOB_STATES as _TERMINAL_JOB_STATES,
    UNSTARTED_JOB_STATES as _UNSTARTED_JOB_STATES,
    CollectionJobState,
)
from clinic_geo.market_evidence.projection import ProjectionOutcome, ProjectionResult


# A leased job is actively running work; queued/retry-wait jobs are unstarted.
_ACTIVE_JOB_STATES = frozenset({CollectionJobState.LEASED})


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class EligibilityDimension:
    status: str
    is_hit: bool

    def to_canonical_dict(self) -> dict:
        return {"dimension": "eligibility", "status": self.status, "is_hit": self.is_hit}


@dataclass(frozen=True, slots=True)
class FreshnessDimension:
    fresh: bool
    refresh_failed: bool
    freshness_age_seconds: float | None
    expires_at: str | None

    def to_canonical_dict(self) -> dict:
        return {
            "dimension": "freshness",
            "fresh": self.fresh,
            "refresh_failed": self.refresh_failed,
            "freshness_age_seconds": self.freshness_age_seconds,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ReuseDimension:
    reused_existing_evidence: bool
    selected_window_id: str | None

    def to_canonical_dict(self) -> dict:
        return {
            "dimension": "reuse",
            "reused_existing_evidence": self.reused_existing_evidence,
            "selected_window_id": self.selected_window_id,
        }


@dataclass(frozen=True, slots=True)
class JobStateDimension:
    counts_by_state: tuple[tuple[str, int], ...]
    has_active_work: bool
    has_unstarted_work: bool
    all_terminal: bool

    def to_canonical_dict(self) -> dict:
        return {
            "dimension": "job_state",
            "counts_by_state": [
                {"state": state, "count": count} for state, count in self.counts_by_state
            ],
            "has_active_work": self.has_active_work,
            "has_unstarted_work": self.has_unstarted_work,
            "all_terminal": self.all_terminal,
        }


@dataclass(frozen=True, slots=True)
class AppearanceDimension:
    outcome: str
    matched_evidence_count: int

    def to_canonical_dict(self) -> dict:
        return {
            "dimension": "appearance",
            "outcome": self.outcome,
            "matched_evidence_count": self.matched_evidence_count,
        }


@dataclass(frozen=True, slots=True)
class EvidenceDateDimension:
    oldest_accepted_at: str | None

    def to_canonical_dict(self) -> dict:
        return {
            "dimension": "evidence_date",
            "oldest_accepted_at": self.oldest_accepted_at,
        }


@dataclass(frozen=True, slots=True)
class WarningsDimension:
    coverage_warning: bool
    coverage_warning_codes: tuple[str, ...]

    def to_canonical_dict(self) -> dict:
        return {
            "dimension": "warnings",
            "coverage_warning": self.coverage_warning,
            "coverage_warning_codes": list(self.coverage_warning_codes),
        }


class VisibilityImpactLabel(str):
    pass


@dataclass(frozen=True, slots=True)
class VisibilityImpactDimension:
    """A categorical impact label, never a numeric rank or blended score."""

    label: str

    def to_canonical_dict(self) -> dict:
        return {"dimension": "visibility_impact", "label": self.label}


# Dimension attribute names.  ``CampaignCellReadModel`` exposes exactly these and
# nothing that collapses them.
CELL_DIMENSIONS = (
    "eligibility",
    "freshness",
    "reuse",
    "job_state",
    "appearance",
    "evidence_date",
    "warnings",
    "visibility_impact",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CampaignCellReadModel:
    """One coverage cell's read model: eight separate dimensions, no total.

    There is deliberately no aggregate score, rank, or combined health value.
    ``to_canonical_dict`` emits each dimension under its own key.
    """

    coverage_cell_id: str
    eligibility: EligibilityDimension
    freshness: FreshnessDimension
    reuse: ReuseDimension
    job_state: JobStateDimension
    appearance: AppearanceDimension
    evidence_date: EvidenceDateDimension
    warnings: WarningsDimension
    visibility_impact: VisibilityImpactDimension

    def dimensions(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in CELL_DIMENSIONS}

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": "campaign.cell_read_model.v1",
            "coverage_cell_id": self.coverage_cell_id,
            "dimensions": {
                name: getattr(self, name).to_canonical_dict()
                for name in CELL_DIMENSIONS
            },
        }


def _visibility_impact_label(
    projection: ProjectionResult | None,
) -> str:
    if projection is None:
        return "insufficient_evidence"
    outcome = projection.outcome
    if outcome is ProjectionOutcome.APPEARED:
        return "positive_presence"
    if outcome is ProjectionOutcome.MEASURED_NOT_OBSERVED:
        return "confirmed_absence"
    return "insufficient_evidence"


def build_cell_read_model(
    *,
    coverage_cell_id: str,
    eligibility: EligibilityDecision,
    projection: ProjectionResult | None,
    job_states: tuple[CollectionJobState, ...] = (),
) -> CampaignCellReadModel:
    """Derive the eight independent dimensions for one coverage cell.

    ``projection`` may be ``None`` when projection has not run; appearance is
    then ``unknown`` and visibility impact is ``insufficient_evidence``.  No
    dimension is a function of another dimension's derived score, because there
    is no score.
    """

    if not isinstance(coverage_cell_id, str) or not coverage_cell_id.strip():
        raise ValueError("coverage_cell_id must be non-empty text")
    if not isinstance(eligibility, EligibilityDecision):
        raise TypeError("eligibility must be an EligibilityDecision")
    if projection is not None and not isinstance(projection, ProjectionResult):
        raise TypeError("projection must be a ProjectionResult or None")
    states = tuple(job_states)
    if any(not isinstance(item, CollectionJobState) for item in states):
        raise TypeError("job_states must contain CollectionJobState values")

    age = eligibility.freshness_age
    eligibility_dim = EligibilityDimension(
        status=eligibility.status.value, is_hit=eligibility.is_hit
    )
    fresh = eligibility.status in {EligibilityStatus.HIT}
    freshness_dim = FreshnessDimension(
        fresh=fresh,
        refresh_failed=eligibility.refresh_failed,
        freshness_age_seconds=(None if age is None else age.total_seconds()),
        expires_at=_iso(eligibility.expires_at),
    )
    reuse_dim = ReuseDimension(
        reused_existing_evidence=eligibility.is_hit,
        selected_window_id=eligibility.selected_window_id,
    )
    counter = Counter(state.value for state in states)
    job_state_dim = JobStateDimension(
        counts_by_state=tuple(sorted(counter.items())),
        has_active_work=any(state in _ACTIVE_JOB_STATES for state in states),
        has_unstarted_work=any(state in _UNSTARTED_JOB_STATES for state in states),
        all_terminal=bool(states)
        and all(state in _TERMINAL_JOB_STATES for state in states),
    )
    if projection is None:
        appearance_dim = AppearanceDimension(
            outcome=ProjectionOutcome.UNKNOWN.value, matched_evidence_count=0
        )
        warnings_dim = WarningsDimension(coverage_warning=True, coverage_warning_codes=())
    else:
        appearance_dim = AppearanceDimension(
            outcome=projection.outcome.value,
            matched_evidence_count=len(projection.matched_evidence_ids),
        )
        warnings_dim = WarningsDimension(
            coverage_warning=projection.coverage_warning,
            coverage_warning_codes=tuple(
                code.value for code in projection.coverage_warning_codes
            ),
        )
    evidence_date_dim = EvidenceDateDimension(
        oldest_accepted_at=_iso(eligibility.freshness_started_at)
    )
    visibility_impact_dim = VisibilityImpactDimension(
        label=_visibility_impact_label(projection)
    )
    return CampaignCellReadModel(
        coverage_cell_id=coverage_cell_id,
        eligibility=eligibility_dim,
        freshness=freshness_dim,
        reuse=reuse_dim,
        job_state=job_state_dim,
        appearance=appearance_dim,
        evidence_date=evidence_date_dim,
        warnings=warnings_dim,
        visibility_impact=visibility_impact_dim,
    )


# --- Branch-scoped candidate queue -----------------------------------------
# Queue membership and human disposition are kept strictly separate: presence in
# the queue is not a decision, and a decision does not remove a branch from the
# queue.  ``disposition`` is the latest decision event for a member, or None.


@dataclass(frozen=True, slots=True)
class CandidateQueueMember:
    candidate_id: str
    branch_id: str
    disposition: str | None
    disposition_decided_at: str | None

    def to_canonical_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "branch_id": self.branch_id,
            "disposition": self.disposition,
            "disposition_decided_at": self.disposition_decided_at,
        }


@dataclass(frozen=True, slots=True)
class CandidateQueue:
    campaign_revision_id: str
    members: tuple[CandidateQueueMember, ...]

    @property
    def branch_ids(self) -> tuple[str, ...]:
        return tuple(member.branch_id for member in self.members)

    @property
    def undecided_branch_ids(self) -> tuple[str, ...]:
        return tuple(
            member.branch_id for member in self.members if member.disposition is None
        )

    def to_canonical_dict(self) -> dict:
        return {
            "schema_version": "campaign.candidate_queue.v1",
            "campaign_revision_id": self.campaign_revision_id,
            "members": [member.to_canonical_dict() for member in self.members],
        }


def build_candidate_queue(
    connection: sqlite3.Connection,
    campaign_revision_id: str,
) -> CandidateQueue:
    """Read a branch-scoped candidate queue with its latest dispositions.

    Membership (a row in ``campaign_candidates``) is separate from disposition
    (the latest ``campaign_candidate_decision_events`` row).  A member with no
    decision event has ``disposition=None``.  This is read-only.
    """

    if not isinstance(campaign_revision_id, str) or not campaign_revision_id.strip():
        raise ValueError("campaign_revision_id must be non-empty text")
    rows = connection.execute(
        """
        SELECT candidate.id AS candidate_id, candidate.branch_id AS branch_id,
               latest.decision AS disposition, latest.decided_at AS decided_at
        FROM campaign_candidates candidate
        LEFT JOIN (
            SELECT event.candidate_id, event.decision, event.decided_at
            FROM campaign_candidate_decision_events event
            JOIN (
                SELECT candidate_id, MAX(decided_at) AS max_decided_at
                FROM campaign_candidate_decision_events
                GROUP BY candidate_id
            ) newest
              ON newest.candidate_id=event.candidate_id
             AND newest.max_decided_at=event.decided_at
        ) latest ON latest.candidate_id=candidate.id
        WHERE candidate.campaign_revision_id=?
        ORDER BY candidate.branch_id, candidate.id
        """,
        (campaign_revision_id,),
    ).fetchall()
    members = tuple(
        CandidateQueueMember(
            candidate_id=row["candidate_id"],
            branch_id=row["branch_id"],
            disposition=row["disposition"],
            disposition_decided_at=row["decided_at"],
        )
        for row in rows
    )
    return CandidateQueue(
        campaign_revision_id=campaign_revision_id, members=members
    )


__all__ = [
    "CELL_DIMENSIONS",
    "AppearanceDimension",
    "CampaignCellReadModel",
    "CandidateQueue",
    "CandidateQueueMember",
    "EligibilityDimension",
    "EvidenceDateDimension",
    "FreshnessDimension",
    "JobStateDimension",
    "ReuseDimension",
    "VisibilityImpactDimension",
    "WarningsDimension",
    "build_candidate_queue",
    "build_cell_read_model",
]
