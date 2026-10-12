"""Deterministic, evidence-gated outreach readiness.

The workspace has two deliberately separate permissions:

``routing``
    A safe, manual enquiry to reception or a known router to identify the
    responsible buyer.  It never permits delivery of visibility findings.

``evidence_delivery``
    A manual message to a confirmed buyer/marketing owner, supported by the
    current 30-observation visibility contract.

Readiness is recomputed from current records every time.  It is never stored as
an editable flag, and the backwards-compatible top-level ``ready`` value always
means evidence-delivery ready.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from clinic_geo.evidence.purpose import (
    EvidencePurpose,
    legacy_purpose_view,
    require_evidence_purpose,
)

from .db import NotFoundError
from .models import (
    destination_family,
    destination_key,
    json_loads,
    normalize_destination,
    normalize_email,
    utc_now,
)
from .policy import (
    CURRENT_OUTREACH_POLICY_VERSION,
    ROUTING_MESSAGE_POLICY_VERSION,
    current_outreach_policy_sha256,
    message_content_sha256,
    routing_message_errors,
)


OUTREACH_PURPOSES = {"routing", "evidence_delivery"}
BUYER_AUTHORITY_ROLES = {"economic_buyer", "marketing_owner"}
ROUTER_AUTHORITY_ROLES = {"router"}
VISIBILITY_EVIDENCE_STANDARD = "prospect_visibility_30_v1"
OUTREACH_EVIDENCE_PURPOSE = EvidencePurpose.OUTREACH_GAP_CONFIRMATION

GATE_LABELS = {
    "identity": "Accepted clinic identity",
    "priority": "Priority prospect",
    "buyer": "Confirmed buying authority",
    "contact": "Verified route for this action",
    "governance": "Manual channel review approved",
    "screening": "Affirmative route screening is clear",
    "suppression": "No active suppression",
    "destination_unique": "Destination belongs to one account",
    "visibility": "Fresh 30-observation visibility evidence",
    "rationale": "Approved factual rationale",
    "message": "Approved purpose-specific message",
    "dedupe": "No duplicate outreach",
}


def _now(value: str | None = None) -> str:
    return value or utc_now()


def _parse_time(value: str) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _not_expired(expires_at: str | None, now: str) -> bool:
    if not expires_at:
        return True
    try:
        return _parse_time(expires_at) > _parse_time(now)
    except (AttributeError, TypeError, ValueError):
        return False


def _gate(code: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"code": code, "label": GATE_LABELS[code], "passed": bool(passed), "detail": detail}


def _latest_snapshot(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT ras.*, rr.imported_at AS release_imported_at
        FROM research_account_snapshots ras
        JOIN research_releases rr ON rr.release_id = ras.release_id
        WHERE ras.sales_account_id = ?
          AND ras.release_id = (
              SELECT COALESCE(
                  (SELECT value FROM workspace_state WHERE key='active_release_id'),
                  (SELECT release_id FROM research_releases ORDER BY imported_at DESC, release_id DESC LIMIT 1)
              )
          )
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()


def _suppression_matches(
    row: sqlite3.Row,
    *,
    account_id: str,
    person_id: str | None,
    channel: str | None,
    destination: str | None,
    branch_id: str | None = None,
) -> bool:
    if branch_id is None:
        if row["branch_id"] is not None:
            return False
    elif row["branch_id"] not in (None, branch_id):
        return False
    scope = row["scope_type"]
    if scope == "account":
        return row["account_id"] == account_id
    if scope == "person":
        return bool(person_id) and row["person_id"] == person_id
    if scope == "destination":
        return (
            row["account_id"] in (None, account_id)
            and bool(destination)
            and destination_family(row["channel"] or "") == destination_family(channel or "")
            and (row["scope_value"] or "").strip().lower() == destination.lower()
        )
    if scope == "channel":
        return row["account_id"] in (None, account_id) and row["channel"] == channel
    if scope == "domain" and destination and channel == "email" and "@" in destination:
        domain = normalize_email(destination).split("@", 1)[1]
        return (
            row["account_id"] in (None, account_id)
            and (row["scope_value"] or "").strip().lower() == domain
        )
    return False


def _current_people(
    connection: sqlite3.Connection,
    account_id: str,
    authority_roles: set[str],
    now: str,
    branch_id: str | None = None,
) -> list[sqlite3.Row]:
    scope_clause = "AND branch_id IS NULL" if branch_id is None else "AND (branch_id=? OR branch_id IS NULL)"
    parameters: tuple[Any, ...] = (account_id,) if branch_id is None else (account_id, branch_id)
    rows = connection.execute(
        f"""
        SELECT * FROM people
        WHERE account_id=? AND status='current' AND verification_status='confirmed'
          AND full_name<>'' AND role_title<>''
          AND source_url IS NOT NULL AND source_url<>''
          AND verified_at IS NOT NULL AND verified_at<>''
          {scope_clause}
        ORDER BY is_primary DESC, verified_at DESC, created_at DESC
        """,
        parameters,
    )
    return [
        row for row in rows
        if row["authority_role"] in authority_roles and _not_expired(row["expires_at"], now)
    ]


def _verified_contacts(
    connection: sqlite3.Connection,
    account_id: str,
    now: str,
    branch_id: str | None = None,
) -> list[sqlite3.Row]:
    scope_clause = "AND branch_id IS NULL" if branch_id is None else "AND (branch_id=? OR branch_id IS NULL)"
    parameters: tuple[Any, ...] = (account_id,) if branch_id is None else (account_id, branch_id)
    rows = connection.execute(
        f"""
        SELECT * FROM contact_points
        WHERE account_id=? AND status='active' AND verification_status='verified'
          AND source_url IS NOT NULL AND source_url<>''
          AND verified_at IS NOT NULL AND verified_at<>''
          {scope_clause}
        ORDER BY CASE channel WHEN 'email' THEN 0 WHEN 'phone' THEN 1 ELSE 2 END,
                 verified_at DESC, created_at DESC
        """,
        parameters,
    )
    return [
        row for row in rows
        if _not_expired(row["expires_at"], now)
        and normalize_destination(row["channel"], row["value"]) == row["normalized_value"]
    ]


def _latest_review(
    connection: sqlite3.Connection,
    contact_id: str,
    purpose: str,
) -> sqlite3.Row | None:
    # Latest purpose-specific decision wins.  We intentionally do not fall
    # back to an older approval if the route changed or a newer review blocks.
    return connection.execute(
        """
        SELECT * FROM channel_reviews
        WHERE contact_point_id=? AND outreach_purpose=?
        ORDER BY reviewed_at DESC, created_at DESC, rowid DESC LIMIT 1
        """,
        (contact_id, purpose),
    ).fetchone()


def _review_state(
    review: sqlite3.Row | None,
    contact: sqlite3.Row,
    purpose: str,
    now: str,
) -> tuple[bool, bool]:
    governance_ok = bool(
        review
        and review["decision"] == "approved"
        and review["manual_approval"] == 1
        and review["lawful_basis_status"] == "reviewed"
        and review["privacy_notice_status"] == "reviewed"
        and _not_expired(review["expires_at"], now)
    )
    if not governance_ok or not review:
        return governance_ok, False
    try:
        screen_age_at_review = _parse_time(review["reviewed_at"]) - _parse_time(review["screened_at"])
        screen_timing_ok = (
            0 <= screen_age_at_review.total_seconds() <= 24 * 60 * 60
            and _parse_time(review["screened_at"]) <= _parse_time(now)
        )
    except (AttributeError, TypeError, ValueError):
        screen_timing_ok = False
    route_key = destination_key(contact["channel"], contact["normalized_value"])
    preference_expected = (
        review["preference_service_status"] == "clear"
        if destination_family(contact["channel"]) == "phone"
        else review["preference_service_status"] == "not_applicable"
    )
    screening_ok = bool(
        review["outreach_purpose"] == purpose
        and review["contact_version"] == contact["version"]
        and review["destination_key"] == route_key
        and review["policy_version"] == CURRENT_OUTREACH_POLICY_VERSION
        and review["policy_sha256"] == current_outreach_policy_sha256()
        and (review["jurisdiction"] or "").strip()
        and review["subscriber_type"] != "not_assessed"
        and review["suppression_screen_status"] == "clear"
        and preference_expected
        and (review["screened_by"] or "").strip()
        and (review["screened_at"] or "").strip()
        and (review["screening_notes"] or "").strip()
        and screen_timing_ok
    )
    return governance_ok, screening_ok


def _duplicates(connection: sqlite3.Connection, contact: sqlite3.Row, account_id: str) -> list[sqlite3.Row]:
    if destination_family(contact["channel"]) == "phone":
        return list(
            connection.execute(
                """
                SELECT id,account_id FROM contact_points
                WHERE normalized_value=? AND channel IN ('phone','sms','whatsapp')
                  AND account_id<>? AND status='active'
                """,
                (contact["normalized_value"], account_id),
            )
        )
    return list(
        connection.execute(
            """
            SELECT id,account_id FROM contact_points
            WHERE normalized_value=? AND channel=? AND account_id<>? AND status='active'
            """,
            (contact["normalized_value"], contact["channel"], account_id),
        )
    )


def _matching_suppressions(
    suppressions: list[sqlite3.Row],
    *,
    account_id: str,
    person_id: str | None,
    contact: sqlite3.Row | None,
    branch_id: str | None = None,
) -> list[sqlite3.Row]:
    return [
        row for row in suppressions
        if _suppression_matches(
            row,
            account_id=account_id,
            person_id=person_id,
            channel=contact["channel"] if contact else None,
            destination=contact["normalized_value"] if contact else None,
            branch_id=branch_id,
        )
    ]


def _current_visibility(
    connection: sqlite3.Connection,
    account_id: str,
    now: str,
    branch_id: str | None = None,
    treatment_slug: str | None = None,
) -> tuple[sqlite3.Row | None, str | None]:
    if branch_id is None:
        scope_clause = "AND branch_id IS NULL"
        parameters: tuple[Any, ...] = (account_id,)
    else:
        scope_clause = "AND branch_id=? AND treatment_slug=?"
        parameters = (account_id, branch_id, treatment_slug)
    validation_error: str | None = None
    for row in connection.execute(
        f"""
        SELECT * FROM visibility_checks
        WHERE account_id=? AND approval_status='approved'
          AND logged_out_verified=1 AND conclusion='confirmed_gap'
          {scope_clause}
        ORDER BY checked_at DESC, created_at DESC
        """,
        parameters,
    ):
        if (
            row["evidence_standard"] != VISIBILITY_EVIDENCE_STANDARD
            or row["base_prompt_count"] != 15
            or row["run_count"] != 2
            or row["observation_count"] != 30
            or row["prompt_count"] != 30
            or row["general_prompt_count"] != 26
            or row["clinic_specific_prompt_count"] != 4
            or not _not_expired(row["expires_at"], now)
        ):
            continue
        try:
            stored = json_loads(row["raw_evidence_json"], {})
            checked = validate_visibility_artifact(stored, at=now)
            artifact_account = checked.get("raw_evidence", {}).get("check", {}).get("sales_account_id")
            if artifact_account != account_id:
                raise ValueError("stored visibility artifact belongs to a different account")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            validation_error = str(exc)
            continue
        return row, None
    return None, validation_error


def _approved_draft(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    purpose: str,
    contact: sqlite3.Row,
    person_id: str | None,
    visibility_id: str | None,
    branch_id: str | None = None,
    treatment_slug: str | None = None,
) -> sqlite3.Row | None:
    if purpose == "routing":
        candidate = connection.execute(
            """
            SELECT * FROM message_drafts
            WHERE account_id=? AND outreach_purpose='routing' AND status='approved' AND body<>''
              AND routing_scope_attested=1 AND content_policy_version=?
              AND contact_point_id=? AND channel=? AND visibility_check_id IS NULL
              AND branch_id IS NULL
              AND ((? IS NULL AND person_id IS NULL) OR person_id=?)
            ORDER BY approved_at DESC, updated_at DESC, rowid DESC LIMIT 1
            """,
            (account_id, ROUTING_MESSAGE_POLICY_VERSION, contact["id"], contact["channel"], person_id, person_id),
        ).fetchone()
        if candidate and routing_message_errors(candidate["body"], subject=candidate["subject"]):
            return None
        if candidate and candidate["content_sha256"] != message_content_sha256(
            account_id=candidate["account_id"],
            person_id=candidate["person_id"],
            contact_point_id=candidate["contact_point_id"],
            visibility_check_id=candidate["visibility_check_id"],
            subject=candidate["subject"],
            body=candidate["body"],
            channel=candidate["channel"],
            outreach_purpose=purpose,
            branch_id=candidate["branch_id"],
            treatment_slug=candidate["treatment_slug"],
        ):
            return None
        return candidate
    if not person_id or not visibility_id:
        return None
    if branch_id is None:
        scope_clause = "AND branch_id IS NULL"
        parameters: tuple[Any, ...] = (
            account_id,
            person_id,
            contact["id"],
            contact["channel"],
            visibility_id,
        )
    else:
        scope_clause = "AND branch_id=? AND treatment_slug=?"
        parameters = (
            account_id,
            person_id,
            contact["id"],
            contact["channel"],
            visibility_id,
            branch_id,
            treatment_slug,
        )
    candidate = connection.execute(
        f"""
        SELECT * FROM message_drafts
        WHERE account_id=? AND outreach_purpose='evidence_delivery'
          AND person_id=? AND status='approved' AND body<>''
          AND contact_point_id=? AND channel=? AND visibility_check_id=?
          {scope_clause}
        ORDER BY approved_at DESC, updated_at DESC, rowid DESC LIMIT 1
        """,
        parameters,
    ).fetchone()
    if candidate and candidate["content_sha256"] != message_content_sha256(
        account_id=candidate["account_id"],
        person_id=candidate["person_id"],
        contact_point_id=candidate["contact_point_id"],
        visibility_check_id=candidate["visibility_check_id"],
        subject=candidate["subject"],
        body=candidate["body"],
        channel=candidate["channel"],
        outreach_purpose=purpose,
        branch_id=candidate["branch_id"],
        treatment_slug=candidate["treatment_slug"],
    ):
        return None
    return candidate


def _dedupe_state(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    purpose: str,
    contact_id: str | None,
    draft_id: str | None,
    branch_id: str | None = None,
) -> bool:
    scope_clause = "AND branch_id IS NULL" if branch_id is None else "AND branch_id=?"
    sequence_parameters: tuple[Any, ...] = (
        (account_id, purpose, contact_id, contact_id)
        if branch_id is None
        else (account_id, purpose, contact_id, contact_id, branch_id)
    )
    active_sequence = connection.execute(
        f"""
        SELECT id FROM sequence_enrollments
        WHERE account_id=? AND outreach_purpose=? AND status='active'
          AND (contact_point_id IS NULL OR ? IS NULL OR contact_point_id=?)
          {scope_clause}
        LIMIT 1
        """,
        sequence_parameters,
    ).fetchone()
    if not draft_id:
        return not active_sequence
    draft_scope_clause = "AND oei.branch_id IS NULL" if branch_id is None else "AND oei.branch_id=?"
    export_parameters: tuple[Any, ...] = (
        (draft_id, purpose) if branch_id is None else (draft_id, purpose, branch_id)
    )
    already_exported = connection.execute(
        f"""
        SELECT oei.export_id FROM outreach_export_items oei
        JOIN outreach_exports oe ON oe.id=oei.export_id
        WHERE oei.message_draft_id=? AND oe.outreach_purpose=?
          AND oe.status IN ('prepared','complete')
          {draft_scope_clause}
        LIMIT 1
        """,
        export_parameters,
    ).fetchone()
    interaction_parameters: tuple[Any, ...] = (
        (account_id, purpose, draft_id, f"draft:{draft_id}")
        if branch_id is None
        else (account_id, purpose, draft_id, f"draft:{draft_id}", branch_id)
    )
    already_sent = connection.execute(
        f"""
        SELECT id FROM interactions
        WHERE account_id=? AND outreach_purpose=? AND direction='outbound'
          AND (message_draft_id=? OR dedupe_key=?)
          {scope_clause}
        LIMIT 1
        """,
        interaction_parameters,
    ).fetchone()
    return not active_sequence and not already_exported and not already_sent


def _route_score(route: dict[str, Any]) -> int:
    return sum(
        bool(route[key])
        for key in (
            "governance_ok",
            "screening_ok",
            "suppression_ok",
            "destination_unique_ok",
            "message_ok",
        )
    )


def _make_lane(
    connection: sqlite3.Connection,
    *,
    purpose: str,
    account: sqlite3.Row,
    snapshot: sqlite3.Row | None,
    conflicts: list[sqlite3.Row],
    buyers: list[sqlite3.Row],
    routers: list[sqlite3.Row],
    contacts: list[sqlite3.Row],
    suppressions: list[sqlite3.Row],
    visibility: sqlite3.Row | None,
    visibility_error: str | None,
    now: str,
    branch_id: str | None = None,
    treatment_slug: str | None = None,
) -> dict[str, Any]:
    account_id = account["id"]
    identity_ok = bool(
        snapshot and account["status"] == "active"
        and snapshot["prospect_status"] == "qualified" and not conflicts
    )
    if not snapshot:
        identity_detail = "Clinic is not present in the current connected research release."
    elif account["status"] != "active":
        identity_detail = f"Sales account status is {account['status']}."
    elif snapshot["prospect_status"] != "qualified":
        identity_detail = f"Research status is {snapshot['prospect_status'] or 'unknown'}, not qualified."
    elif conflicts:
        identity_detail = f"{len(conflicts)} research identity conflict(s) still need resolution."
    else:
        identity_detail = "Clinic is current, active, qualified, and identity is accepted."

    tier = snapshot["prospect_tier"] if snapshot else None
    priority_ok = tier in {"A", "B"} or bool(account["priority_override"])
    priority_detail = (
        f"Research tier is {tier}." if tier in {"A", "B"}
        else "A documented founder override is active." if account["priority_override"]
        else f"Research tier is {tier or 'unassigned'}; A/B or a documented override is required."
    )

    buyer_ok = bool(buyers)
    buyer_detail = (
        f"{buyers[0]['full_name']} has confirmed buying authority as {buyers[0]['role_title']}."
        if buyers else "Confirm a named economic buyer or marketing owner with a current source."
    )

    person_by_id = {row["id"]: row for row in (*buyers, *routers)}
    router_ids = {row["id"] for row in routers}
    route_evaluations: list[dict[str, Any]] = []
    for contact in contacts:
        eligible_people: list[sqlite.Row | None]
        if purpose == "routing":
            if contact["route_scope"] == "organization":
                eligible_people = [None]
            elif contact["route_scope"] == "person" and contact["person_id"] in router_ids:
                eligible_people = [person_by_id.get(contact["person_id"])]
            else:
                continue
        else:
            if contact["route_scope"] == "person":
                person = next((row for row in buyers if row["id"] == contact["person_id"]), None)
                if not person:
                    continue
                eligible_people = [person]
            elif contact["route_scope"] == "organization" and contact["organization_route_exception"] == 1:
                if not buyers:
                    continue
                eligible_people = list(buyers)
            else:
                continue
        review = _latest_review(connection, contact["id"], purpose)
        governance_ok, screening_ok = _review_state(review, contact, purpose, now)
        duplicates = _duplicates(connection, contact, account_id)
        for person in eligible_people:
            matched = _matching_suppressions(
                suppressions,
                account_id=account_id,
                person_id=person["id"] if person else None,
                contact=contact,
                branch_id=branch_id,
            )
            draft = _approved_draft(
                connection,
                account_id=account_id,
                purpose=purpose,
                contact=contact,
                person_id=person["id"] if person else None,
                visibility_id=visibility["id"] if visibility else None,
                branch_id=branch_id,
                treatment_slug=treatment_slug,
            )
            route_evaluations.append(
                {
                    "person": person,
                    "contact": contact,
                    "review": review,
                    "draft": draft,
                    "governance_ok": governance_ok,
                    "screening_ok": screening_ok,
                    "suppressions": matched,
                    "suppression_ok": not matched,
                    "duplicates": duplicates,
                    "destination_unique_ok": not duplicates,
                    "message_ok": draft is not None,
                }
            )

    selected = next(
        (
            route for route in route_evaluations
            if all(
                route[key]
                for key in (
                    "governance_ok", "screening_ok", "suppression_ok",
                    "destination_unique_ok", "message_ok",
                )
            )
        ),
        max(route_evaluations, key=_route_score) if route_evaluations else None,
    )
    contact = selected["contact"] if selected else None
    person = selected["person"] if selected else (buyers[0] if purpose == "evidence_delivery" and buyers else None)
    review = selected["review"] if selected else None
    draft = selected["draft"] if selected else None

    contact_ok = bool(route_evaluations)
    contact_detail = (
        f"{len(route_evaluations)} current verified route(s) can support this action."
        if contact_ok else (
            "Verify a general clinic/reception route or a route to a confirmed router."
            if purpose == "routing"
            else "Verify a direct buyer route, or explicitly approve an organization-route exception."
        )
    )
    governance_ok = bool(selected and selected["governance_ok"])
    governance_detail = (
        "The latest manual channel review for this purpose is approved and current."
        if governance_ok else "Approve a fresh manual channel, lawful-basis, and privacy review for this exact purpose."
    )
    screening_ok = bool(selected and selected["screening_ok"])
    screening_detail = (
        "A dated clear screen is bound to this route, purpose, version, and policy."
        if screening_ok else (
            "Record a fresh clear suppression and preference-service screen for this exact route and purpose "
            f"under {CURRENT_OUTREACH_POLICY_VERSION}."
        )
    )
    if selected:
        matched_suppressions = selected["suppressions"]
    else:
        matched_suppressions = _matching_suppressions(
            suppressions,
            account_id=account_id,
            person_id=person["id"] if person else None,
            contact=None,
            branch_id=branch_id,
        )
    suppression_ok = not matched_suppressions
    suppression_detail = (
        "No applicable active suppression exists."
        if suppression_ok else f"{len(matched_suppressions)} active suppression rule(s) block outreach."
    )
    destination_unique_ok = not (selected and selected["duplicates"])
    destination_unique_detail = (
        "Destination is not attached to another active sales account."
        if destination_unique_ok else (
            f"Destination is attached to {len(selected['duplicates'])} other account(s); resolve the duplicate first."
        )
    )

    visibility_ok = visibility is not None
    visibility_detail = (
        f"Approved 30-observation logged-out evidence is fresh through {visibility['expires_at']}."
        if visibility else (
            f"Stored visibility evidence is invalid: {visibility_error}"
            if visibility_error
            else "Run, human-review, and approve 15 frozen prompts in each of two independent logged-out sessions."
        )
    )
    rationale = None
    if visibility:
        if branch_id is None:
            rationale_scope_clause = "AND branch_id IS NULL"
            rationale_parameters: tuple[Any, ...] = (account_id, visibility["id"])
        else:
            rationale_scope_clause = "AND branch_id=? AND treatment_slug=?"
            rationale_parameters = (account_id, visibility["id"], branch_id, treatment_slug)
        rationale = connection.execute(
            f"""
            SELECT * FROM account_rationales
            WHERE account_id=? AND is_current=1 AND status='approved'
              AND why_chosen<>'' AND problem_evidence<>'' AND visibility_check_id=?
              {rationale_scope_clause}
            ORDER BY approved_at DESC,updated_at DESC LIMIT 1
            """,
            rationale_parameters,
        ).fetchone()
    rationale_ok = rationale is not None
    rationale_detail = (
        "Founder rationale and factual evidence are approved."
        if rationale_ok else "Approve why this clinic was selected and the factual evidence from the current check."
    )
    message_ok = draft is not None
    message_detail = (
        "An approved message is bound to this exact route and purpose."
        if message_ok else (
            "Approve a buyer-identification message for the selected routing route."
            if purpose == "routing"
            else "Approve an evidence-delivery message bound to the selected buyer, route, and check."
        )
    )

    dedupe_ok = _dedupe_state(
        connection,
        account_id=account_id,
        purpose=purpose,
        contact_id=contact["id"] if contact else None,
        draft_id=draft["id"] if draft else None,
        branch_id=branch_id,
    )
    dedupe_detail = (
        "No active sequence, prior export, or sent activity exists for this message step."
        if dedupe_ok else "This message step is already active, exported, or sent; create the next step instead."
    )

    gates = [
        _gate("identity", identity_ok, identity_detail),
        _gate("priority", priority_ok, priority_detail),
    ]
    if purpose == "evidence_delivery":
        gates.append(_gate("buyer", buyer_ok, buyer_detail))
    gates.extend(
        [
            _gate("contact", contact_ok, contact_detail),
            _gate("governance", governance_ok, governance_detail),
            _gate("screening", screening_ok, screening_detail),
            _gate("suppression", suppression_ok, suppression_detail),
            _gate("destination_unique", destination_unique_ok, destination_unique_detail),
        ]
    )
    if purpose == "evidence_delivery":
        gates.extend(
            [
                _gate("visibility", visibility_ok, visibility_detail),
                _gate("rationale", rationale_ok, rationale_detail),
            ]
        )
    gates.extend(
        [
            _gate("message", message_ok, message_detail),
            _gate("dedupe", dedupe_ok, dedupe_detail),
        ]
    )
    failed = [gate["code"] for gate in gates if not gate["passed"]]
    selection = {
        "buyer_id": person["id"] if purpose == "evidence_delivery" and person else None,
        "router_id": person["id"] if purpose == "routing" and person else None,
        "contact_point_id": contact["id"] if contact else None,
        "channel_review_id": review["id"] if review else None,
        "visibility_check_id": visibility["id"] if purpose == "evidence_delivery" and visibility else None,
        "rationale_id": rationale["id"] if purpose == "evidence_delivery" and rationale else None,
        "message_draft_id": draft["id"] if draft else None,
    }
    if branch_id is not None:
        selection.update({"branch_id": branch_id, "treatment_slug": treatment_slug})
    lane = {
        "purpose": purpose,
        "ready": not failed,
        "passed": len(gates) - len(failed),
        "total": len(gates),
        "gates": gates,
        "failed_gate_codes": failed,
        "next_step": next((gate["detail"] for gate in gates if not gate["passed"]), "Ready for a manual action."),
        "selection": selection,
    }
    if branch_id is not None:
        lane.update({"branch_id": branch_id, "treatment_slug": treatment_slug})
    return lane


def evaluate_readiness(
    connection: sqlite3.Connection,
    account_id: str,
    *,
    branch_id: str | None = None,
    treatment_slug: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Return both readiness lanes; top-level fields alias evidence delivery.

    Routing is always clinic-wide.  A supplied branch and treatment scope only
    the evidence-delivery lane.
    """
    if bool(branch_id) != bool(treatment_slug):
        raise ValueError("branch_id and treatment_slug must be supplied together")
    now = _now(now)
    account = connection.execute("SELECT * FROM sales_accounts WHERE id=?", (account_id,)).fetchone()
    if not account:
        raise NotFoundError(f"outreach account not found: {account_id}")
    snapshot = _latest_snapshot(connection, account_id)
    conflicts = list(
        connection.execute(
            "SELECT id,kind FROM import_conflicts WHERE sales_account_id=? AND status='open'",
            (account_id,),
        )
    )
    clinic_buyers = _current_people(connection, account_id, BUYER_AUTHORITY_ROLES, now)
    clinic_routers = _current_people(connection, account_id, ROUTER_AUTHORITY_ROLES, now)
    clinic_contacts = _verified_contacts(connection, account_id, now)
    suppressions = list(connection.execute("SELECT * FROM suppressions WHERE active=1"))
    clinic_visibility, clinic_visibility_error = _current_visibility(connection, account_id, now)
    routing = _make_lane(
        connection,
        purpose="routing",
        account=account,
        snapshot=snapshot,
        conflicts=conflicts,
        buyers=clinic_buyers,
        routers=clinic_routers,
        contacts=clinic_contacts,
        suppressions=suppressions,
        visibility=clinic_visibility,
        visibility_error=clinic_visibility_error,
        now=now,
    )
    if branch_id is None:
        delivery_buyers = clinic_buyers
        delivery_routers = clinic_routers
        delivery_contacts = clinic_contacts
        delivery_visibility = clinic_visibility
        delivery_visibility_error = clinic_visibility_error
    else:
        delivery_buyers = _current_people(
            connection, account_id, BUYER_AUTHORITY_ROLES, now, branch_id
        )
        delivery_routers = _current_people(
            connection, account_id, ROUTER_AUTHORITY_ROLES, now, branch_id
        )
        delivery_contacts = _verified_contacts(connection, account_id, now, branch_id)
        delivery_visibility, delivery_visibility_error = _current_visibility(
            connection,
            account_id,
            now,
            branch_id,
            treatment_slug,
        )
    delivery = _make_lane(
        connection,
        purpose="evidence_delivery",
        account=account,
        snapshot=snapshot,
        conflicts=conflicts,
        buyers=delivery_buyers,
        routers=delivery_routers,
        contacts=delivery_contacts,
        suppressions=suppressions,
        visibility=delivery_visibility,
        visibility_error=delivery_visibility_error,
        now=now,
        branch_id=branch_id,
        treatment_slug=treatment_slug,
    )
    result = dict(delivery)
    result.update(
        {
            "routing_ready": routing["ready"],
            "evidence_delivery_ready": delivery["ready"],
            "lanes": {"routing": routing, "evidence_delivery": delivery},
            "paths": {"routing": routing, "evidence_delivery": delivery},
        }
    )
    return result


def readiness_for_purpose(readiness: dict[str, Any], purpose: str) -> dict[str, Any]:
    if purpose not in OUTREACH_PURPOSES:
        raise ValueError("outreach_purpose must be routing or evidence_delivery")
    return readiness.get("lanes", {}).get(purpose) or readiness


def load_visibility_artifact(path: Path | str) -> dict[str, Any]:
    check_dir = Path(path).expanduser().resolve()
    if check_dir.is_file():
        check_dir = check_dir.parent
    required = ("check.json", "prompts.json", "evidence.json", "integrity.json")
    missing = [name for name in required if not (check_dir / name).is_file()]
    if missing:
        raise ValueError("visibility check directory is incomplete: " + ", ".join(missing))
    return {"check_dir": str(check_dir)}


def validate_visibility_artifact(path_or_value: Any, *, at: str | None = None) -> dict[str, Any]:
    """Validate the canonical 15×2 workflow artifact and derive DB fields."""
    from clinic_geo.outreach.visibility_evidence import (
        DISCOVERY_PROMPT_IDS,
        EXPECTED_PROMPT_IDS,
        RUN_IDS,
        PURPOSE_LEGACY_SCHEMA_VERSION,
        SCHEMA_VERSION,
        TEMPLATE_PROMPT_IDS,
        VERIFICATION_PROMPT_IDS,
        VISIBILITY_CONTRACT_ID,
        validate_visibility_check,
    )

    if isinstance(path_or_value, dict):
        path_or_value = path_or_value.get("check_dir") or path_or_value.get("artifact_path")
    if not path_or_value:
        raise ValueError("save_visibility_check requires a finalized check_dir/artifact_path")
    check_dir = Path(path_or_value).expanduser().resolve()
    if check_dir.is_file():
        check_dir = check_dir.parent
    validation = validate_visibility_check(check_dir, at=at)
    required_flags = (
        "artifact_valid",
        "collection_complete",
        "fresh",
        "passes_outreach_evidence_gate",
        "contract_current",
    )
    if not all(validation.get(flag) is True for flag in required_flags):
        errors = validation.get("errors") or ["visibility check does not pass the 30-observation evidence gate"]
        raise ValueError("invalid visibility check: " + "; ".join(str(item) for item in errors))
    check = json.loads((check_dir / "check.json").read_text(encoding="utf-8"))
    evidence = json.loads((check_dir / "evidence.json").read_text(encoding="utf-8"))
    prompts = json.loads((check_dir / "prompts.json").read_text(encoding="utf-8")).get("prompts", [])
    purpose_views = []
    for artifact_kind, value in (("outreach_visibility_check", check), ("outreach_visibility_evidence", evidence)):
        try:
            schema_version = str(value.get("schema_version") or "").strip()
            if schema_version == SCHEMA_VERSION:
                require_evidence_purpose(
                    value,
                    OUTREACH_EVIDENCE_PURPOSE,
                    label=artifact_kind,
                )
                purpose_views.append(value)
            elif schema_version == PURPOSE_LEGACY_SCHEMA_VERSION and "purpose" in value:
                require_evidence_purpose(
                    value,
                    OUTREACH_EVIDENCE_PURPOSE,
                    label=f"historical purpose-bound {artifact_kind}",
                )
                purpose_views.append(value)
            elif schema_version == PURPOSE_LEGACY_SCHEMA_VERSION:
                purpose_views.append(
                    legacy_purpose_view(
                        value,
                        OUTREACH_EVIDENCE_PURPOSE,
                        artifact_kind=artifact_kind,
                        supported_schema_versions=(PURPOSE_LEGACY_SCHEMA_VERSION,),
                    )
                )
            else:
                raise ValueError(
                    f"{artifact_kind} schema {schema_version!r} is not an allowlisted "
                    "current or historical purpose contract"
                )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid visibility evidence purpose: {exc}") from exc
    check, evidence = purpose_views
    if check.get("visibility_contract_id") != VISIBILITY_CONTRACT_ID:
        raise ValueError("visibility artifact does not use the current 30-observation contract")
    cited_discovery = {
        str(item)
        for item in evidence.get("review", {}).get("evidence_prompt_ids", [])
        if str(item) in DISCOVERY_PROMPT_IDS
    }
    profile_ids = [
        check.get("profile_proofs", {}).get(run_id, {}).get("profile_id") for run_id in RUN_IDS
    ]
    return {
        "evidence_purpose": OUTREACH_EVIDENCE_PURPOSE.value,
        "platform": str(check.get("platform") or evidence.get("platform") or ""),
        "browser_profile": " | ".join(str(value) for value in profile_ids if value),
        "browser_profiles": profile_ids,
        "logged_out_verified": True,
        "checked_at": str(evidence.get("completed_at") or ""),
        "expires_at": str(validation.get("expires_at") or evidence.get("expires_at") or ""),
        "prompt_count": len(EXPECTED_PROMPT_IDS),
        "general_prompt_count": len(DISCOVERY_PROMPT_IDS),
        "clinic_specific_prompt_count": len(VERIFICATION_PROMPT_IDS),
        "gap_prompt_count": len(cited_discovery),
        "conclusion": "confirmed_gap",
        "approved_by": evidence.get("review", {}).get("reviewer"),
        "approved_at": evidence.get("review", {}).get("reviewed_at"),
        "evidence_standard": VISIBILITY_CONTRACT_ID,
        "base_prompt_count": len(TEMPLATE_PROMPT_IDS),
        "run_count": len(RUN_IDS),
        "observation_count": len(EXPECTED_PROMPT_IDS),
        "raw_evidence": {
            "check_dir": str(check_dir),
            "validation": validation,
            "check": check,
            "evidence": evidence,
            "prompts": prompts,
        },
    }
