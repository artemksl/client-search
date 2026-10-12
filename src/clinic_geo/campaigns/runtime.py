"""Campaign runtime: bounded lifecycle transitions over approved query scopes.

Activation binds a reviewed treatment catalogue release and transitions only
the campaign revision it owns, atomically and idempotently.  Market-evidence
reads and pending-job cancellation used by campaign completion are delegated
to the market-evidence owner; campaign code does not issue SQL against its
tables.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from clinic_geo.integrity import canonical_sha256
from clinic_geo.treatment_library.read_models import catalogue_release_state

from .contracts import CampaignRevisionState


Clock = Callable[[], datetime]

# The exact set of query-scope levels an approved MVP revision must carry.
_REQUIRED_SCOPE_LEVELS = frozenset({"local", "greater_area", "city"})


class CampaignRuntimeError(RuntimeError):
    """A campaign runtime transition is missing, invalid, or unverifiable."""


class CampaignActivationConflict(CampaignRuntimeError):
    """Activation was replayed with different content, or another revision is active."""


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


def revision_version_token(
    connection: sqlite3.Connection, revision_id: str
) -> str:
    """Return a stable content hash of a revision's mutable lifecycle state.

    The token changes whenever the revision's state, activation/completion, or
    bound catalogue changes, so a preview computed against one token is rejected
    once the revision moves on.  It is derived, never stored.
    """

    row = connection.execute(
        """
        SELECT id,campaign_id,revision_number,state,query_scope_status,
               query_geography_release_id,query_geography_payload_sha256,
               treatment_catalogue_release_id,freshness_policy_version,
               foundation_manifest_sha256,activated_at,completed_at
        FROM campaign_revisions WHERE id=?
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise CampaignRuntimeError("campaign revision does not exist")
    keys = row.keys() if hasattr(row, "keys") else [
        "id",
        "campaign_id",
        "revision_number",
        "state",
        "query_scope_status",
        "query_geography_release_id",
        "query_geography_payload_sha256",
        "treatment_catalogue_release_id",
        "freshness_policy_version",
        "foundation_manifest_sha256",
        "activated_at",
        "completed_at",
    ]
    payload = {key: row[key] for key in keys}
    scopes = [
        {"scope_level": scope["scope_level"], "query_geography_version_id": scope["query_geography_version_id"]}
        for scope in connection.execute(
            "SELECT scope_level,query_geography_version_id FROM campaign_query_scopes "
            "WHERE campaign_revision_id=? ORDER BY scope_level",
            (revision_id,),
        )
    ]
    return canonical_sha256(
        {
            "schema_version": "campaign.revision_version_token.v1",
            "revision": payload,
            "query_scopes": scopes,
        }
    )


@dataclass(frozen=True, slots=True)
class CampaignActivationResult:
    revision_id: str
    campaign_id: str
    state: str
    treatment_catalogue_release_id: str
    activated_at: str
    version_token: str
    replayed: bool


def _load_revision(connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id,campaign_id,revision_number,state,query_scope_status,
               treatment_catalogue_release_id,activated_at,completed_at
        FROM campaign_revisions WHERE id=?
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise CampaignRuntimeError("campaign revision does not exist")
    return row


def _approved_scope_levels(
    connection: sqlite3.Connection, revision_id: str
) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in connection.execute(
            "SELECT scope_level FROM campaign_query_scopes WHERE campaign_revision_id=?",
            (revision_id,),
        )
    )


def _require_approved_catalogue(
    connection: sqlite3.Connection, treatment_catalogue_release_id: str
) -> None:
    state = catalogue_release_state(connection, treatment_catalogue_release_id)
    if state != {
        "lifecycle_state": "active",
        "review_state": "approved",
    }:
        raise CampaignRuntimeError(
            "campaign activation requires an active, approved treatment catalogue release"
        )


def bind_treatment_catalogue(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    treatment_catalogue_release_id: str,
) -> str:
    """Bind a reviewed treatment catalogue to a planned revision (preparation).

    The database ordering is bind-catalogue -> select-intent -> activate: an
    intent can only be selected against a planned revision whose catalogue is
    already bound, so binding is a separate preparation step.  Binding is
    idempotent; binding a different catalogue to an already-bound revision is a
    conflict.  Returns the revision version token after binding.
    """

    if not isinstance(revision_id, str) or not revision_id.strip():
        raise CampaignRuntimeError("revision_id must be non-empty text")
    if (
        not isinstance(treatment_catalogue_release_id, str)
        or not treatment_catalogue_release_id.strip()
    ):
        raise CampaignRuntimeError("treatment_catalogue_release_id must be non-empty text")
    if connection.in_transaction:
        raise CampaignRuntimeError(
            "campaign catalogue binding requires an independent top-level transaction"
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        revision = _load_revision(connection, revision_id)
        if revision["state"] != CampaignRevisionState.PLANNED.value:
            raise CampaignRuntimeError(
                "only a planned campaign revision can bind a treatment catalogue"
            )
        current = revision["treatment_catalogue_release_id"]
        if current is not None:
            if current != treatment_catalogue_release_id:
                raise CampaignActivationConflict(
                    "campaign revision already binds a different treatment catalogue"
                )
            token = revision_version_token(connection, revision_id)
            connection.commit()
            return token
        _require_approved_catalogue(connection, treatment_catalogue_release_id)
        connection.execute(
            """
            UPDATE campaign_revisions
            SET treatment_catalogue_release_id=?
            WHERE id=? AND state='planned'
            """,
            (treatment_catalogue_release_id, revision_id),
        )
        token = revision_version_token(connection, revision_id)
        connection.commit()
        return token
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def activate_campaign_revision(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    activated_by: str,
    clock: Clock = _utc_now,
) -> CampaignActivationResult:
    """Atomically transition a prepared planned revision to active.

    Preconditions checked before any write: the revision is planned, its query
    scope is approved with exactly the three required levels, and a treatment
    catalogue is already bound.  The remaining activation contract (approved
    geography bytes, at least one approved selectable intent bound to the exact
    catalogue) is enforced by the migration-008 activation triggers.  A replay
    of an already-active revision returns the existing activation.  The
    single-active-revision database index is the ultimate arbiter under
    concurrency.
    """

    if not isinstance(revision_id, str) or not revision_id.strip():
        raise CampaignRuntimeError("revision_id must be non-empty text")
    if not isinstance(activated_by, str) or not activated_by.strip():
        raise CampaignRuntimeError("activated_by must be non-empty text")
    if connection.in_transaction:
        raise CampaignRuntimeError(
            "campaign activation requires an independent top-level transaction"
        )

    now = _iso(clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        revision = _load_revision(connection, revision_id)
        catalogue = revision["treatment_catalogue_release_id"]
        if revision["state"] == CampaignRevisionState.ACTIVE.value:
            connection.commit()
            return CampaignActivationResult(
                revision_id=revision_id,
                campaign_id=revision["campaign_id"],
                state=CampaignRevisionState.ACTIVE.value,
                treatment_catalogue_release_id=catalogue,
                activated_at=revision["activated_at"],
                version_token=revision_version_token(connection, revision_id),
                replayed=True,
            )
        if revision["state"] != CampaignRevisionState.PLANNED.value:
            raise CampaignRuntimeError(
                "only a planned campaign revision can be activated"
            )
        if revision["query_scope_status"] != "approved":
            raise CampaignRuntimeError(
                "campaign activation requires an approved query scope"
            )
        if _approved_scope_levels(connection, revision_id) != _REQUIRED_SCOPE_LEVELS:
            raise CampaignRuntimeError(
                "campaign activation requires exactly local, greater_area, and city scopes"
            )
        if catalogue is None:
            raise CampaignRuntimeError(
                "campaign activation requires a bound treatment catalogue"
            )
        active_elsewhere = connection.execute(
            "SELECT id FROM campaign_revisions WHERE state='active' AND id<>?",
            (revision_id,),
        ).fetchone()
        if active_elsewhere is not None:
            raise CampaignActivationConflict(
                "another campaign revision is already active"
            )
        try:
            connection.execute(
                """
                UPDATE campaign_revisions
                SET state='active', activated_at=?
                WHERE id=? AND state='planned'
                """,
                (now, revision_id),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "unique" in message.lower():
                # The single-active-revision index lost a concurrent race.
                raise CampaignActivationConflict(
                    "another campaign revision is already active"
                ) from exc
            # A schema activation trigger refused a precondition (missing or
            # invalid selected intent, geography, or catalogue binding).
            raise CampaignRuntimeError(
                f"campaign activation preconditions not met: {message}"
            ) from exc
        token = revision_version_token(connection, revision_id)
        connection.commit()
        return CampaignActivationResult(
            revision_id=revision_id,
            campaign_id=revision["campaign_id"],
            state=CampaignRevisionState.ACTIVE.value,
            treatment_catalogue_release_id=catalogue,
            activated_at=now,
            version_token=token,
            replayed=False,
        )
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


__all__ = [
    "CampaignActivationConflict",
    "CampaignActivationResult",
    "CampaignRuntimeError",
    "activate_campaign_revision",
    "bind_treatment_catalogue",
    "revision_version_token",
]
