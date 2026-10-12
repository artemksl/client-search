"""Campaign-owned persistence operations used by the HTTP application adapter.

Functions are deliberately connection-first and preserve the SQL previously
embedded in ``clinic_geo.web.campaign_api``.  Callers retain transaction control
except for :func:`record_idempotency_key`, whose conflict recheck and insert
must remain one indivisible ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .runtime import CampaignRuntimeError


def find_idempotent_response(
    connection: sqlite3.Connection,
    endpoint: str,
    idempotency_key: str,
) -> Any:
    """Return the response recorded for one endpoint/key pair, if present."""

    return connection.execute(
        "SELECT request_sha256,response_json,http_status "
        "FROM campaign_api_idempotency WHERE endpoint=? AND idempotency_key=?",
        (endpoint, idempotency_key),
    ).fetchone()


def record_idempotency_key(
    connection: sqlite3.Connection,
    *,
    endpoint: str,
    idempotency_key: str,
    request_sha256: str,
    response_json: Callable[[], str],
    http_status: int,
    created_at: Callable[[], str],
) -> Any:
    """Atomically recheck and record an idempotent HTTP response.

    A returned row means another request recorded the key before this
    transaction acquired its write lock.  ``None`` means this call inserted the
    supplied response.
    """

    if connection.in_transaction:
        raise CampaignRuntimeError(
            "idempotency recording requires a closed transaction"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        conflict = connection.execute(
            "SELECT request_sha256,response_json,http_status "
            "FROM campaign_api_idempotency WHERE endpoint=? AND idempotency_key=?",
            (endpoint, idempotency_key),
        ).fetchone()
        if conflict is not None:
            connection.commit()
            return conflict
        connection.execute(
            """
            INSERT INTO campaign_api_idempotency
              (endpoint,idempotency_key,request_sha256,response_json,
               http_status,created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                endpoint,
                idempotency_key,
                request_sha256,
                response_json(),
                http_status,
                created_at(),
            ),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    return None


def revision_counts(connection: sqlite3.Connection) -> tuple[int, int]:
    """Return active and completed campaign-revision counts."""

    active = connection.execute(
        "SELECT count(*) FROM campaign_revisions WHERE state='active'"
    ).fetchone()[0]
    completed = connection.execute(
        "SELECT count(*) FROM campaign_revisions WHERE state='completed'"
    ).fetchone()[0]
    return active, completed


def revision_rows(connection: sqlite3.Connection) -> list[Any]:
    """Return campaign revisions in stable campaign/revision order."""

    return connection.execute(
        "SELECT id,campaign_id,revision_number,state,query_scope_status,"
        "treatment_catalogue_release_id,activated_at,completed_at "
        "FROM campaign_revisions ORDER BY campaign_id,revision_number"
    ).fetchall()


def revision_row(connection: sqlite3.Connection, revision_id: str) -> Any:
    """Return one campaign revision summary row, if present."""

    return connection.execute(
        "SELECT id,campaign_id,revision_number,state,query_scope_status,"
        "treatment_catalogue_release_id,activated_at,completed_at "
        "FROM campaign_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()


def revision_exists(connection: sqlite3.Connection, revision_id: str) -> bool:
    """Return whether the requested campaign revision exists."""

    return (
        connection.execute(
            "SELECT 1 FROM campaign_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        is not None
    )


def activation_revision_row(
    connection: sqlite3.Connection,
    revision_id: str,
) -> Any:
    """Return lifecycle fields used to preview activation."""

    return connection.execute(
        "SELECT state,query_scope_status FROM campaign_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()


def selected_intent_count(
    connection: sqlite3.Connection,
    revision_id: str,
) -> int:
    """Return the number of selected search intents for one revision."""

    return connection.execute(
        "SELECT count(*) FROM campaign_search_intents WHERE campaign_revision_id=?",
        (revision_id,),
    ).fetchone()[0]


def completion_revision_row(
    connection: sqlite3.Connection,
    revision_id: str,
) -> Any:
    """Return lifecycle state used to preview completion."""

    return connection.execute(
        "SELECT state FROM campaign_revisions WHERE id=?", (revision_id,)
    ).fetchone()


def candidate_exists(connection: sqlite3.Connection, candidate_id: str) -> bool:
    """Return whether a campaign candidate exists."""

    return (
        connection.execute(
            "SELECT id FROM campaign_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        is not None
    )


def insert_candidate_decision_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    candidate_id: str,
    decision: str,
    decided_by: str,
    decided_at: str,
    notes: str | None,
) -> None:
    """Insert one immutable candidate decision event."""

    connection.execute(
        """
        INSERT INTO campaign_candidate_decision_events
          (id,candidate_id,decision,decided_by,decided_at,notes)
        VALUES (?,?,?,?,?,?)
        """,
        (
            event_id,
            candidate_id,
            decision,
            decided_by,
            decided_at,
            notes,
        ),
    )


def insert_action_preview(
    connection: sqlite3.Connection,
    *,
    preview_id: str,
    endpoint: str,
    revision_id: str,
    revision_version_token: str,
    payload_sha256: str,
    response_json: str,
    expires_at: str,
    created_at: str,
) -> None:
    """Insert one immutable action preview."""

    connection.execute(
        """
        INSERT INTO campaign_action_previews
          (preview_id,endpoint,campaign_revision_id,revision_version_token,
           payload_sha256,response_json,expires_at,created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            preview_id,
            endpoint,
            revision_id,
            revision_version_token,
            payload_sha256,
            response_json,
            expires_at,
            created_at,
        ),
    )


def read_action_preview(
    connection: sqlite3.Connection,
    *,
    preview_id: str,
    endpoint: str,
) -> Any:
    """Return immutable validation fields for one action preview."""

    return connection.execute(
        "SELECT campaign_revision_id,revision_version_token,payload_sha256,expires_at "
        "FROM campaign_action_previews WHERE preview_id=? AND endpoint=?",
        (preview_id, endpoint),
    ).fetchone()


__all__ = [
    "find_idempotent_response",
    "record_idempotency_key",
    "revision_counts",
    "revision_rows",
    "revision_row",
    "revision_exists",
    "activation_revision_row",
    "selected_intent_count",
    "completion_revision_row",
    "candidate_exists",
    "insert_candidate_decision_event",
    "insert_action_preview",
    "read_action_preview",
]
