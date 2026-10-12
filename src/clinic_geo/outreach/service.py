"""Application service for the founder outreach workspace."""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from . import branch_backfill
from .db import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    connect,
    default_db_path,
    initialize_database,
    transaction,
)
from .importer import ReleaseImporter, cleanup_compound_phone_routes, verify_current_release
from .models import destination_family, destination_key, json_dumps, json_loads, new_id, normalize_destination, utc_now
from .interaction_records import insert_interaction_record
from .policy import (
    ROUTING_MESSAGE_POLICY_VERSION,
    current_outreach_policy_sha256,
    message_content_sha256,
    routing_message_errors,
)
from .readiness import (
    BUYER_AUTHORITY_ROLES,
    CURRENT_OUTREACH_POLICY_VERSION,
    OUTREACH_PURPOSES,
    evaluate_readiness,
    readiness_for_purpose,
    validate_visibility_artifact,
)


JSON_COLUMNS = {
    "member_uids_json": "member_uids",
    "risk_flags_json": "risk_flags",
    "reason_codes_json": "reason_codes",
    "raw_json": "raw",
    "raw_evidence_json": "raw_evidence",
    "browser_profiles_json": "browser_profiles",
    "evidence_urls_json": "evidence_urls",
    "payload_json": "payload",
    "candidate_sales_account_ids_json": "candidate_sales_account_ids",
}

PROSPECT_SCORE_FIELDS = (
    "provisional_score_0_100",
    "commercial_capacity_score",
    "geo_opportunity_score",
    "digital_readiness_score",
    "service_fit_score",
    "evidence_access_score",
    "geo_visibility_score",
    "visibility_status",
    "score_version",
)


def _required(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    raise ValueError(f"missing required field: {names[0]}")


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "approved"}
    return bool(value)


def _parse_time(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_time(value: str) -> str:
    return _parse_time(value).replace(microsecond=0).isoformat()


def _plus_days(value: str, days: int) -> str:
    return (_parse_time(value) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _reject_future(value: str, field: str, *, tolerance_minutes: int = 5) -> None:
    if _parse_time(value) > datetime.now(timezone.utc) + timedelta(minutes=tolerance_minutes):
        raise ValueError(f"{field} cannot be in the future")


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and " " not in value


def _validate_expiry(start: str | None, expires: str | None, *, field: str = "expires_at") -> None:
    if not expires:
        return
    expiry = _parse_time(expires)
    if start and expiry <= _parse_time(start):
        raise ValueError(f"{field} must be later than the verification/review timestamp")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for source, target in JSON_COLUMNS.items():
        if source in result:
            result[target] = json_loads(result.pop(source), {} if source.endswith("evidence_json") else [])
    for key in tuple(result):
        if key in {"is_primary", "organization_route_exception", "priority_override", "active", "logged_out_verified", "is_current", "manual_approval"}:
            result[key] = bool(result[key])
    return result


def _csv_safe(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _audit(
    connection: sqlite3.Connection,
    *,
    actor: str,
    entity_type: str,
    entity_id: str,
    action: str,
    before: Any = None,
    after: Any = None,
    correlation_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events
          (occurred_at, actor, entity_type, entity_id, action, before_json, after_json, correlation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(), actor, entity_type, entity_id, action,
            json_dumps(before) if before is not None else None,
            json_dumps(after) if after is not None else None,
            correlation_id,
        ),
    )


@contextmanager
def _null_transaction(connection: sqlite3.Connection):
    yield connection


class OutreachService:
    """High-level, JSON-serializable API used by the web UI and CLI."""

    def __init__(self, root: Path | str, db_path: Path | str | None = None):
        self.root = Path(root).resolve()
        self.db_path = Path(db_path).resolve() if db_path else default_db_path(self.root)
        initialize_database(self.db_path)

    def sync_current_release(self, *, dry_run: bool = False, actor: str = "system") -> dict[str, Any]:
        with closing(connect(self.db_path)) as connection:
            result = ReleaseImporter(self.root, connection).sync_current_release(dry_run=dry_run, actor=actor)
            if not dry_run:
                with transaction(connection, immediate=True):
                    result["compound_phone_cleanup"] = cleanup_compound_phone_routes(
                        connection, actor=actor
                    )
            return result

    def backfill_branches(
        self,
        *,
        dry_run: bool = True,
        manifest_path: Path | str | None = None,
        expected_manifest_sha: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Preview or hash-gate a branch backfill for the connected release.

        The default remains non-mutating for compatibility with older callers.
        Apply requires both the reviewed manifest file and its expected exact
        SHA-256; the preview is regenerated under the write transaction before
        any row is inserted.
        """

        if dry_run and expected_manifest_sha:
            raise ValueError("expected_manifest_sha is only valid for apply")
        if not dry_run and not manifest_path:
            raise ValueError("manifest_path is required for branch backfill apply")
        if not dry_run and not expected_manifest_sha:
            raise ValueError("expected_manifest_sha is required for branch backfill apply")

        verified = verify_current_release(self.root)
        checked = (
            branch_backfill.read_checked_manifest(
                manifest_path,
                str(expected_manifest_sha),
            )
            if not dry_run
            else None
        )
        with closing(connect(self.db_path)) as connection:
            transaction_scope = (
                transaction(connection, immediate=True)
                if not dry_run
                else _null_transaction(connection)
            )
            with transaction_scope:
                release = self._latest_release(connection)
                if not release:
                    raise NotFoundError("no connected research release")
                if release["release_id"] != verified.release_id:
                    raise IntegrityError(
                        "connected release does not match current verified release"
                    )
                if release["manifest_sha256"] != verified.manifest_sha256:
                    raise IntegrityError(
                        "connected release manifest hash is no longer verified"
                    )
                prepared = branch_backfill.prepare_manifest(
                    connection,
                    self.root,
                    release,
                )
                if dry_run:
                    if manifest_path:
                        branch_backfill.write_manifest(manifest_path, prepared)
                    return branch_backfill.result_payload(
                        prepared,
                        mode="dry-run",
                        manifest_path=manifest_path,
                    )

                assert checked is not None
                branch_backfill.assert_current_preview(checked, prepared)
                applied_summary = branch_backfill.apply_prepared_manifest(
                    connection,
                    checked,
                    actor=actor,
                )
                branch_backfill.assert_external_inputs_current(checked, self.root)
                return branch_backfill.result_payload(
                    checked,
                    mode="applied",
                    manifest_path=manifest_path,
                    summary=applied_summary,
                )

    @staticmethod
    def _latest_release(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM research_releases
            WHERE release_id = COALESCE(
                (SELECT value FROM workspace_state WHERE key='active_release_id'),
                (SELECT release_id FROM research_releases ORDER BY imported_at DESC, release_id DESC LIMIT 1)
            )
            LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT ras.* FROM research_account_snapshots ras
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

    @staticmethod
    def _ensure_account(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM sales_accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise NotFoundError(f"outreach account not found: {account_id}")
        return row

    @staticmethod
    def _ensure_branch(
        connection: sqlite3.Connection,
        account_id: str,
        branch_id: str,
        *,
        label: str = "branch",
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM clinic_branches WHERE id=? AND sales_account_id=?",
            (branch_id, account_id),
        ).fetchone()
        if not row:
            raise ValueError(f"{label} does not belong to this account")
        return row

    @staticmethod
    def _ensure_active_treatment(
        connection: sqlite3.Connection,
        treatment_slug: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM treatments WHERE slug=? AND active=1",
            (treatment_slug,),
        ).fetchone()
        if not row:
            raise ValueError(f"unknown or inactive treatment_slug: {treatment_slug}")
        return row

    def _ensure_branch_treatment_scope(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        branch_id: str | None,
        treatment_slug: str | None,
    ) -> sqlite3.Row | None:
        if bool(branch_id) != bool(treatment_slug):
            raise ValueError("branch_id and treatment_slug must be supplied together")
        if not branch_id:
            return None
        self._ensure_branch(connection, account_id, branch_id)
        return self._ensure_active_treatment(connection, str(treatment_slug))

    def _account_summary(self, connection: sqlite3.Connection, account_id: str) -> dict[str, Any]:
        account = self._ensure_account(connection, account_id)
        snapshot = self._snapshot(connection, account_id)
        display_snapshot = snapshot or connection.execute(
            """
            SELECT ras.* FROM research_account_snapshots ras
            JOIN research_releases rr ON rr.release_id=ras.release_id
            WHERE ras.sales_account_id=? ORDER BY rr.imported_at DESC, rr.release_id DESC LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        readiness = evaluate_readiness(connection, account_id)
        worklists = [
            row["name"]
            for row in connection.execute(
                """
                SELECT w.name FROM worklists w JOIN worklist_items wi ON wi.worklist_id = w.id
                WHERE wi.account_id = ? AND w.status = 'active' ORDER BY w.name
                """,
                (account_id,),
            )
        ]
        buyer = connection.execute(
            """
            SELECT full_name FROM people WHERE account_id = ? AND status = 'current'
              AND branch_id IS NULL
              AND authority_role IN ('economic_buyer','marketing_owner')
            ORDER BY is_primary DESC, verified_at DESC, created_at DESC LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        research_raw: dict[str, Any] = {}
        if display_snapshot:
            parsed_raw = json_loads(display_snapshot["raw_json"], {})
            if isinstance(parsed_raw, dict):
                research_raw = parsed_raw
        return {
            "id": account_id,
            "name": account["display_name_override"] or (display_snapshot["clinic_name"] if display_snapshot else "Unlinked clinic"),
            "area": display_snapshot["area"] if display_snapshot else None,
            "tier": display_snapshot["prospect_tier"] if display_snapshot else None,
            "score": display_snapshot["prospect_score"] if display_snapshot else None,
            **{field: research_raw.get(field) for field in PROSPECT_SCORE_FIELDS},
            "rank": display_snapshot["prospect_rank"] if display_snapshot else None,
            "research_status": display_snapshot["prospect_status"] if display_snapshot else None,
            "in_current_release": snapshot is not None,
            "status": account["status"],
            "stage": account["stage"],
            "updated_at": account["updated_at"],
            "version": account["version"],
            "priority_override": bool(account["priority_override"]),
            "readiness": readiness,
            "routing_ready": readiness["routing_ready"],
            "evidence_delivery_ready": readiness["evidence_delivery_ready"],
            "worklists": worklists,
            "buyer_name": buyer["full_name"] if buyer else None,
            "suppressed": "suppression" in readiness["failed_gate_codes"],
        }

    def list_accounts(
        self,
        *,
        view: str = "prospects",
        q: str = "",
        worklist: str | None = None,
        limit: int = 100,
        offset: int = 0,
        tier: str = "",
        stage: str = "",
        readiness: str = "",
        sort: str = "",
        direction: str = "asc",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        with closing(connect(self.db_path)) as connection:
            latest = self._latest_release(connection)
            if not latest:
                return {"view": view, "total": 0, "items": [], "limit": limit, "offset": offset}
            rows = list(
                connection.execute(
                    """
                    SELECT ras.sales_account_id, ras.clinic_name, ras.area, ras.prospect_tier,
                           ras.prospect_rank, ras.prospect_status, sa.stage
                    FROM research_account_snapshots ras
                    JOIN sales_accounts sa ON sa.id = ras.sales_account_id
                    WHERE ras.release_id = ?
                    ORDER BY COALESCE(ras.prospect_rank, 1000000000), ras.clinic_name
                    """,
                    (latest["release_id"],),
                )
            )
            worklist_ids: set[str] = set()
            if worklist or view in {"worklist", "founder", "today"}:
                name = worklist or "founder-25"
                worklist_ids = {
                    row["account_id"]
                    for row in connection.execute(
                        """
                        SELECT wi.account_id FROM worklist_items wi
                        JOIN worklists w ON w.id = wi.worklist_id WHERE w.name = ? OR w.id = ?
                        """,
                        (name, name),
                    )
                }
            query = q.strip().lower()
            selected: list[str] = []
            for source in rows:
                if view in {"worklist", "founder", "today"} and source["sales_account_id"] not in worklist_ids:
                    continue
                if view == "pipeline" and source["stage"] == "research":
                    continue
                if query and query not in f"{source['clinic_name']} {source['area'] or ''}".lower():
                    continue
                selected.append(source["sales_account_id"])
            items = [self._account_summary(connection, value) for value in selected]
            if tier:
                items = [item for item in items if item["tier"] == tier.upper()]
            if stage:
                items = [item for item in items if item["stage"] == stage.lower()]
            if readiness:
                readiness_key = readiness.lower()
                if readiness_key in {"ready", "true", "yes", "1"}:
                    items = [item for item in items if item["readiness"]["ready"]]
                elif readiness_key in {"routing", "routing_ready", "find_buyer"}:
                    items = [item for item in items if item["readiness"]["routing_ready"]]
                elif readiness_key in {"evidence", "evidence_ready", "evidence_delivery_ready"}:
                    items = [item for item in items if item["readiness"]["evidence_delivery_ready"]]
                elif readiness_key in {"blocked", "needs_work", "not_ready", "false", "no", "0"}:
                    items = [item for item in items if not item["readiness"]["ready"]]
                elif readiness_key == "suppressed":
                    items = [item for item in items if item["suppressed"]]
                else:
                    raise ValueError(f"unsupported readiness filter: {readiness}")
            sort_key = sort or "rank"
            stage_order = {
                value: index
                for index, value in enumerate(
                    ("research", "contacted", "replied", "positive", "meeting", "qualified", "proposal", "won", "lost", "nurture")
                )
            }
            tier_order = {value: index for index, value in enumerate(("A", "B", "C", "NURTURE", "REVIEW", "EXCLUDE"))}
            key_functions: dict[str, Callable[[dict[str, Any]], Any]] = {
                "rank": lambda item: item["rank"],
                "score": lambda item: item["score"],
                "provisional_score": lambda item: item["provisional_score_0_100"],
                "commercial_capacity": lambda item: item["commercial_capacity_score"],
                "geo_opportunity": lambda item: item["geo_opportunity_score"],
                "digital_readiness": lambda item: item["digital_readiness_score"],
                "service_fit": lambda item: item["service_fit_score"],
                "evidence_access": lambda item: item["evidence_access_score"],
                "geo_visibility": lambda item: item["geo_visibility_score"],
                "name": lambda item: item["name"].lower(),
                "area": lambda item: str(item["area"] or "").lower() or None,
                "stage": lambda item: stage_order.get(item["stage"], len(stage_order)),
                "tier": lambda item: tier_order.get(item["tier"], len(tier_order)),
                "research_status": lambda item: str(item["research_status"] or "").lower() or None,
                "visibility_status": lambda item: str(item["visibility_status"] or "").lower() or None,
                "readiness": lambda item: (
                    -1
                    if item["suppressed"]
                    else 2
                    if item["readiness"]["evidence_delivery_ready"]
                    else 1
                    if item["readiness"]["routing_ready"]
                    else 0
                ),
                "buyer": lambda item: str(item["buyer_name"] or "").lower() or None,
                "updated_at": lambda item: item["updated_at"],
            }
            if sort_key not in key_functions:
                raise ValueError(f"unsupported account sort: {sort_key}")
            sort_value = key_functions[sort_key]
            present_items = [item for item in items if sort_value(item) not in (None, "")]
            missing_items = [item for item in items if sort_value(item) in (None, "")]
            present_items.sort(key=sort_value, reverse=direction.lower() == "desc")
            items = present_items + missing_items
            total = len(items)
            items = items[offset : offset + limit]
            if worklist_ids:
                name = worklist or "founder-25"
                reasons = {
                    row["account_id"]: row["selection_reason"]
                    for row in connection.execute(
                        """
                        SELECT wi.account_id, wi.selection_reason FROM worklist_items wi
                        JOIN worklists w ON w.id = wi.worklist_id WHERE w.name = ? OR w.id = ?
                        """,
                        (name, name),
                    )
                }
                for item in items:
                    item["selection_reason"] = reasons.get(item["id"])
            return {"view": view, "total": total, "items": items, "limit": limit, "offset": offset}

    def get_account(
        self,
        account_id: str,
        *,
        branch_id: str | None = None,
        treatment_slug: str | None = None,
    ) -> dict[str, Any]:
        branch_id = str(branch_id or "").strip() or None
        treatment_slug = str(treatment_slug or "").strip() or None
        if bool(branch_id) != bool(treatment_slug):
            raise ValueError("branch_id and treatment_slug must be supplied together")
        with closing(connect(self.db_path)) as connection:
            summary = self._account_summary(connection, account_id)
            snapshot = self._snapshot(connection, account_id)
            account = self._ensure_account(connection, account_id)
            branch_readiness = None
            if branch_id:
                self._ensure_branch(connection, account_id, branch_id)
                self._ensure_active_treatment(connection, str(treatment_slug))
                branch_readiness = evaluate_readiness(
                    connection,
                    account_id,
                    branch_id=branch_id,
                    treatment_slug=treatment_slug,
                )
            def rows(table: str, order: str = "created_at DESC") -> list[dict[str, Any]]:
                return [
                    _row(item) or {}
                    for item in connection.execute(
                        f"SELECT * FROM {table} WHERE account_id = ? ORDER BY {order}",
                        (account_id,),
                    )
                ]
            conflicts = [
                _row(item) or {}
                for item in connection.execute(
                    "SELECT * FROM import_conflicts WHERE sales_account_id = ? ORDER BY created_at DESC",
                    (account_id,),
                )
            ]
            rationale = connection.execute(
                "SELECT * FROM account_rationales WHERE account_id = ? AND branch_id IS NULL "
                "AND is_current = 1 LIMIT 1",
                (account_id,),
            ).fetchone()
            visibility_checks = rows("visibility_checks", "checked_at DESC")
            latest_visibility = next(
                (item for item in visibility_checks if item.get("branch_id") is None),
                None,
            )
            visibility_summary = None
            if latest_visibility:
                visibility_summary = {
                    key: latest_visibility.get(key)
                    for key in (
                        "id", "evidence_standard", "base_prompt_count", "run_count",
                        "observation_count", "general_prompt_count",
                        "clinic_specific_prompt_count", "conclusion", "approval_status",
                        "logged_out_verified", "checked_at", "expires_at", "browser_profiles",
                    )
                }
            account_payload = {**dict(account), "name": summary["name"]}
            account_payload.pop("owner", None)
            branches = []
            for item in connection.execute(
                """
                SELECT cb.id,cb.sales_account_id,cb.first_release_id,cb.created_at,
                       cbs.release_id,cbs.branch_id,cbs.branch_key,cbs.member_uids_json,
                       cbs.derived_parent_name,cbs.derived_branch_name,cbs.area,cbs.area_name,
                       cbs.address,cbs.postcode,cbs.latitude,cbs.longitude,
                       cbs.is_compound_or_ambiguous,cbs.derivation_status,
                       cbs.created_at AS snapshot_created_at,
                       COALESCE(bc.status,'unconfirmed') AS status,bc.display_name,
                       bc.merged_into,bc.confirmed_by,bc.confirmed_at,bc.notes,
                       bc.version AS confirmation_version,bc.updated_at AS confirmation_updated_at
                FROM clinic_branches cb
                LEFT JOIN clinic_branch_snapshots cbs
                  ON cbs.branch_id=cb.id
                 AND cbs.release_id=(
                     SELECT cbs_latest.release_id
                     FROM clinic_branch_snapshots cbs_latest
                     JOIN research_releases rr ON rr.release_id=cbs_latest.release_id
                     WHERE cbs_latest.branch_id=cb.id
                     ORDER BY rr.imported_at DESC,rr.release_id DESC
                     LIMIT 1
                 )
                LEFT JOIN branch_confirmations bc ON bc.branch_id=cb.id
                WHERE cb.sales_account_id=?
                ORDER BY cbs.derived_parent_name,cbs.derived_branch_name,cb.id
                """,
                (account_id,),
            ):
                value = _row(item) or {}
                parent = str(value.get("derived_parent_name") or summary["name"])
                label = str(value.get("display_name") or value.get("derived_branch_name") or "Main")
                value["display"] = f"{parent} — {label}"
                branches.append(value)
            branch_treatments = [
                _row(item) or {}
                for item in connection.execute(
                    """
                    SELECT bt.* FROM branch_treatments bt
                    JOIN clinic_branches cb ON cb.id=bt.branch_id
                    WHERE cb.sales_account_id=?
                    ORDER BY bt.updated_at DESC,bt.branch_id,bt.treatment_slug,bt.source
                    """,
                    (account_id,),
                )
            ]
            treatment_observations = [
                _row(item) or {}
                for item in connection.execute(
                    """
                    SELECT * FROM account_treatment_observations
                    WHERE sales_account_id=?
                    ORDER BY created_at DESC,release_id DESC,treatment_slug
                    """,
                    (account_id,),
                )
            ]
            result = {
                "account": account_payload,
                "summary": summary,
                "research": _row(snapshot),
                "research_history": [
                    _row(item) or {}
                    for item in connection.execute(
                        """
                        SELECT ras.* FROM research_account_snapshots ras
                        JOIN research_releases rr ON rr.release_id=ras.release_id
                        WHERE ras.sales_account_id=? ORDER BY rr.imported_at DESC, rr.release_id DESC
                        """,
                        (account_id,),
                    )
                ],
                "readiness": summary["readiness"],
                "people": rows("people", "is_primary DESC, updated_at DESC"),
                "contacts": rows("contact_points", "updated_at DESC"),
                "channel_reviews": rows("channel_reviews", "reviewed_at DESC"),
                "visibility_checks": visibility_checks,
                "visibility_summary": visibility_summary,
                "rationale": _row(rationale),
                "drafts": rows("message_drafts", "updated_at DESC"),
                "suppressions": rows("suppressions", "created_at DESC"),
                "interactions": rows("interactions", "occurred_at DESC"),
                "opportunity": _row(connection.execute("SELECT * FROM opportunities WHERE account_id = ?", (account_id,)).fetchone()),
                "import_conflicts": conflicts,
                "worklists": summary["worklists"],
                "branches": branches,
                "branch_treatments": branch_treatments,
                "treatment_observations": treatment_observations,
            }
            if branch_readiness is not None:
                result["branch_readiness"] = branch_readiness
            return result

    def dashboard(self) -> dict[str, Any]:
        with closing(connect(self.db_path)) as connection:
            latest = self._latest_release(connection)
            account_ids = [row["id"] for row in connection.execute("SELECT id FROM sales_accounts")]
            worklist_count = connection.execute("SELECT COUNT(DISTINCT account_id) AS n FROM worklist_items").fetchone()["n"]
            readiness_rows = [evaluate_readiness(connection, account_id) for account_id in account_ids]
            blocker_counts = Counter(
                code for value in readiness_rows for code in value["failed_gate_codes"]
            )
            priority_ab = 0
            if latest:
                priority_ab = connection.execute(
                    "SELECT COUNT(*) AS n FROM research_account_snapshots WHERE release_id = ? AND prospect_tier IN ('A','B')",
                    (latest["release_id"],),
                ).fetchone()["n"]
            stages = {
                row["stage"]: row["n"]
                for row in connection.execute("SELECT stage, COUNT(*) AS n FROM sales_accounts GROUP BY stage")
            }
            interactions = list(
                connection.execute(
                    """
                    SELECT i.*, COALESCE(sa.display_name_override, ras.clinic_name) AS account_name
                    FROM interactions i JOIN sales_accounts sa ON sa.id = i.account_id
                    LEFT JOIN research_account_snapshots ras ON ras.sales_account_id = i.account_id
                      AND ras.release_id = (
                          SELECT COALESCE(
                              (SELECT value FROM workspace_state WHERE key='active_release_id'),
                              (SELECT release_id FROM research_releases ORDER BY imported_at DESC, release_id DESC LIMIT 1)
                          )
                      )
                    ORDER BY i.occurred_at DESC LIMIT 10
                    """
                )
            )
            worklists = []
            for row in connection.execute(
                """
                SELECT w.*, COUNT(wi.account_id) AS count FROM worklists w
                LEFT JOIN worklist_items wi ON wi.worklist_id = w.id
                WHERE w.status = 'active' GROUP BY w.id ORDER BY w.created_at
                """
            ):
                ready_count = sum(
                    1 for item in connection.execute("SELECT account_id FROM worklist_items WHERE worklist_id = ?", (row["id"],))
                    if evaluate_readiness(connection, item["account_id"])["ready"]
                )
                worklists.append({**dict(row), "ready_count": ready_count})
            # Funnel metrics use unique accounts, not touch counts.  This keeps
            # every rate on the same denominator and stops follow-ups from
            # making response performance look artificially weak.
            sent = connection.execute(
                "SELECT COUNT(DISTINCT account_id) AS n FROM interactions WHERE direction = 'outbound'"
            ).fetchone()["n"]
            replied = connection.execute(
                "SELECT COUNT(DISTINCT account_id) AS n FROM interactions WHERE direction = 'inbound'"
            ).fetchone()["n"]
            positive = connection.execute(
                """
                SELECT COUNT(DISTINCT sa.id) AS n
                FROM sales_accounts sa
                LEFT JOIN interactions i ON i.account_id = sa.id
                WHERE sa.stage IN ('positive','meeting','qualified','proposal','won')
                   OR (i.direction = 'inbound' AND i.interaction_type = 'positive_reply')
                """
            ).fetchone()["n"]
            meetings = connection.execute(
                """
                SELECT COUNT(DISTINCT sa.id) AS n
                FROM sales_accounts sa
                LEFT JOIN interactions i ON i.account_id = sa.id
                WHERE sa.stage IN ('meeting','qualified','proposal','won')
                   OR i.interaction_type = 'meeting_booked'
                """
            ).fetchone()["n"]
            return {
                "release": {
                    "id": latest["release_id"] if latest else None,
                    "imported_at": latest["imported_at"] if latest else None,
                },
                "counts": {
                    "accounts": len(account_ids),
                    "priority_ab": priority_ab,
                    "worklist": worklist_count,
                    "ready": sum(1 for row in readiness_rows if row["ready"]),
                    "routing_ready": sum(1 for row in readiness_rows if row["routing_ready"]),
                    "evidence_ready": sum(1 for row in readiness_rows if row["evidence_delivery_ready"]),
                    "needs_work": sum(1 for row in readiness_rows if not row["ready"]),
                },
                "stages": stages,
                "metrics": {
                    "sent": sent,
                    "replied": replied,
                    "positive": positive,
                    "meetings": meetings,
                    "won": stages.get("won", 0),
                },
                "worklists": worklists,
                "readiness_blockers": dict(blocker_counts),
                "recent_interactions": [_row(row) for row in interactions],
            }

    def create_worklist(
        self,
        *,
        name: str = "founder-25",
        limit: int = 25,
        tiers: Iterable[str] = ("A", "B"),
        actor: str = "founder",
        description: str = "Founder-verified first-client worklist",
        account_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        name = str(name).strip()
        if not name:
            raise ValueError("worklist name is required")
        limit = int(limit)
        if limit < 1 or limit > 25:
            raise ValueError("worklist limit must be between 1 and 25")
        tier_values = tuple(dict.fromkeys(str(value).upper() for value in tiers))
        if not tier_values:
            raise ValueError("at least one tier is required")
        with closing(connect(self.db_path)) as connection:
            existing = connection.execute("SELECT id FROM worklists WHERE name = ?", (name,)).fetchone()
            if existing:
                return self._worklist_result(connection, existing["id"])
            latest = self._latest_release(connection)
            if not latest:
                raise ValueError("sync a research release before creating a worklist")
            explicit = [str(value) for value in (account_ids or [])]
            if len(explicit) > limit:
                raise ValueError("account_ids contains more rows than the worklist limit")
            if len(explicit) != len(set(explicit)):
                raise ValueError("account_ids contains a duplicate")
            if explicit:
                candidates = []
                for account_id in explicit:
                    row = connection.execute(
                        "SELECT * FROM research_account_snapshots WHERE release_id=? AND sales_account_id=?",
                        (latest["release_id"], account_id),
                    ).fetchone()
                    if not row:
                        raise ValueError(f"account is not in the current research release: {account_id}")
                    candidates.append(row)
            else:
                placeholders = ",".join("?" for _ in tier_values)
                candidates = list(
                    connection.execute(
                        f"""
                        SELECT ras.* FROM research_account_snapshots ras
                        JOIN sales_accounts sa ON sa.id = ras.sales_account_id
                        WHERE ras.release_id = ? AND ras.prospect_tier IN ({placeholders})
                          AND ras.prospect_status = 'qualified' AND sa.status = 'active'
                        ORDER BY ras.prospect_rank, ras.clinic_name LIMIT ?
                        """,
                        (latest["release_id"], *tier_values, limit),
                    )
                )
            worklist_id = new_id("worklist")
            now = utc_now()
            with transaction(connection, immediate=True):
                connection.execute(
                    "INSERT INTO worklists (id,name,description,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (worklist_id, name, description, actor, now, now),
                )
                _audit(connection, actor=actor, entity_type="worklist", entity_id=worklist_id, action="created", after={"name": name, "limit": limit})
                for position, candidate in enumerate(candidates, 1):
                    account_id = candidate["sales_account_id"]
                    reason = (
                        f"Research tier {candidate['prospect_tier']}; rank {candidate['prospect_rank']}; "
                        f"score {candidate['prospect_score']}. Requires founder verification before outreach."
                    )
                    connection.execute(
                        "INSERT INTO worklist_items (worklist_id,account_id,position,selection_reason,added_at) VALUES (?,?,?,?,?)",
                        (worklist_id, account_id, position, reason, now),
                    )
                    _audit(connection, actor=actor, entity_type="worklist_item", entity_id=f"{worklist_id}:{account_id}", action="added", after={"position": position, "selection_reason": reason})
            return self._worklist_result(connection, worklist_id)

    def _worklist_result(self, connection: sqlite3.Connection, worklist_id: str) -> dict[str, Any]:
        worklist = connection.execute("SELECT * FROM worklists WHERE id = ?", (worklist_id,)).fetchone()
        if not worklist:
            raise NotFoundError(f"worklist not found: {worklist_id}")
        items = []
        for row in connection.execute(
            "SELECT * FROM worklist_items WHERE worklist_id = ? ORDER BY position", (worklist_id,)
        ):
            value = self._account_summary(connection, row["account_id"])
            value["position"] = row["position"]
            value["selection_reason"] = row["selection_reason"]
            items.append(value)
        return {
            "id": worklist["id"],
            "name": worklist["name"],
            "description": worklist["description"],
            "count": len(items),
            "ready_count": sum(1 for item in items if item["readiness"]["ready"]),
            "routing_ready_count": sum(1 for item in items if item["readiness"]["routing_ready"]),
            "evidence_ready_count": sum(1 for item in items if item["readiness"]["evidence_delivery_ready"]),
            "items": items,
        }

    def _result_with_account(self, connection: sqlite3.Connection, account_id: str, result: Any) -> dict[str, Any]:
        return {"result": result, "account": self.get_account(account_id)}

    def confirm_branch(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id")).strip()
        branch_id = str(_required(payload, "branch_id")).strip()
        status = str(_required(payload, "status")).strip().lower()
        if status not in {"confirmed", "rejected", "unconfirmed"}:
            raise ValueError("status must be confirmed, rejected, or unconfirmed")
        display_name = str(payload.get("display_name") or "").strip() or None
        notes = str(payload.get("notes") or "").strip() or None
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            self._ensure_branch(connection, account_id, branch_id)
            existing = connection.execute(
                "SELECT * FROM branch_confirmations WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            if existing:
                version = existing["version"] + 1
                connection.execute(
                    """
                    UPDATE branch_confirmations
                    SET status=?,display_name=?,merged_into=NULL,confirmed_by=?,confirmed_at=?,
                        notes=?,version=version+1,updated_at=?
                    WHERE branch_id=?
                    """,
                    (status, display_name, actor, now, notes, now, branch_id),
                )
            else:
                version = 1
                connection.execute(
                    """
                    INSERT INTO branch_confirmations
                      (branch_id,status,display_name,merged_into,confirmed_by,confirmed_at,
                       notes,version,updated_at)
                    VALUES (?,?,?,NULL,?,?,?,1,?)
                    """,
                    (branch_id, status, display_name, actor, now, notes, now),
                )
            _audit(
                connection,
                actor=actor,
                entity_type="clinic_branch",
                entity_id=branch_id,
                action="branch_confirmed",
                before=dict(existing) if existing else None,
                after={
                    "account_id": account_id,
                    "status": status,
                    "display_name": display_name,
                    "notes": notes,
                    "version": version,
                },
            )
        return {"id": branch_id, "status": status, "version": version}

    def merge_branches(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id")).strip()
        branch_id = str(_required(payload, "branch_id")).strip()
        merged_into = str(_required(payload, "merged_into")).strip()
        if branch_id == merged_into:
            raise ValueError("a branch cannot be merged into itself")
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            self._ensure_branch(connection, account_id, branch_id)
            self._ensure_branch(
                connection,
                account_id,
                merged_into,
                label="merge target",
            )
            target_confirmation = connection.execute(
                "SELECT * FROM branch_confirmations WHERE branch_id=?",
                (merged_into,),
            ).fetchone()
            if target_confirmation and (
                target_confirmation["status"] == "merged"
                or target_confirmation["merged_into"] is not None
            ):
                raise ValueError("merge target is already merged")
            existing = connection.execute(
                "SELECT * FROM branch_confirmations WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            if existing:
                version = existing["version"] + 1
                connection.execute(
                    """
                    UPDATE branch_confirmations
                    SET status='merged',merged_into=?,confirmed_by=?,confirmed_at=?,
                        version=version+1,updated_at=?
                    WHERE branch_id=?
                    """,
                    (merged_into, actor, now, now, branch_id),
                )
            else:
                version = 1
                connection.execute(
                    """
                    INSERT INTO branch_confirmations
                      (branch_id,status,merged_into,confirmed_by,confirmed_at,version,updated_at)
                    VALUES (?,'merged',?,?,?,1,?)
                    """,
                    (branch_id, merged_into, actor, now, now),
                )
            _audit(
                connection,
                actor=actor,
                entity_type="clinic_branch",
                entity_id=branch_id,
                action="branch_merged",
                before=dict(existing) if existing else None,
                after={
                    "account_id": account_id,
                    "status": "merged",
                    "merged_into": merged_into,
                    "version": version,
                },
            )
        return {
            "id": branch_id,
            "status": "merged",
            "merged_into": merged_into,
            "version": version,
        }

    def set_branch_treatment(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id")).strip()
        branch_id = str(_required(payload, "branch_id")).strip()
        treatment_slug = str(_required(payload, "treatment_slug")).strip()
        state = str(_required(payload, "state")).strip().lower()
        if state not in {"offered", "not_offered", "unknown"}:
            raise ValueError("state must be offered, not_offered, or unknown")
        confidence = str(payload.get("confidence") or "").strip().lower() or None
        if confidence not in {None, "high", "medium", "low"}:
            raise ValueError("confidence must be high, medium, or low")
        evidence_url = str(payload.get("evidence_url") or "").strip() or None
        if evidence_url and not _valid_http_url(evidence_url):
            raise ValueError("evidence_url must be a public http(s) URL")
        notes = str(payload.get("notes") or "").strip() or None
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            self._ensure_branch(connection, account_id, branch_id)
            treatment = connection.execute(
                "SELECT * FROM treatments WHERE slug=?",
                (treatment_slug,),
            ).fetchone()
            if not treatment:
                raise ValueError(f"unknown treatment_slug: {treatment_slug}")
            existing = connection.execute(
                """
                SELECT * FROM branch_treatments
                WHERE branch_id=? AND treatment_slug=? AND source='founder'
                """,
                (branch_id, treatment_slug),
            ).fetchone()
            if existing:
                treatment_id = existing["id"]
                version = existing["version"] + 1
                connection.execute(
                    """
                    UPDATE branch_treatments
                    SET state=?,confidence=?,evidence_url=?,confirmed_by=?,confirmed_at=?,notes=?,
                        version=version+1,updated_at=?
                    WHERE id=?
                    """,
                    (
                        state,
                        confidence,
                        evidence_url,
                        actor,
                        now,
                        notes,
                        now,
                        treatment_id,
                    ),
                )
            else:
                treatment_id = new_id("brtx")
                version = 1
                connection.execute(
                    """
                    INSERT INTO branch_treatments
                      (id,branch_id,treatment_slug,state,source,confidence,evidence_url,
                       release_id,confirmed_by,confirmed_at,notes,version,created_at,updated_at)
                    VALUES (?,?,?,?,'founder',?,?,NULL,?,?,?,1,?,?)
                    """,
                    (
                        treatment_id,
                        branch_id,
                        treatment_slug,
                        state,
                        confidence,
                        evidence_url,
                        actor,
                        now,
                        notes,
                        now,
                        now,
                    ),
                )
            _audit(
                connection,
                actor=actor,
                entity_type="branch_treatment",
                entity_id=treatment_id,
                action="branch_treatment_set",
                before=dict(existing) if existing else None,
                after={
                    "account_id": account_id,
                    "branch_id": branch_id,
                    "treatment_slug": treatment_slug,
                    "state": state,
                    "source": "founder",
                    "confidence": confidence,
                    "evidence_url": evidence_url,
                    "notes": notes,
                    "version": version,
                },
            )
        return {
            "id": treatment_id,
            "branch_id": branch_id,
            "treatment_slug": treatment_slug,
            "state": state,
            "version": version,
        }

    def save_buyer(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        branch_id = str(payload.get("branch_id") or "").strip() or None
        full_name = str(_required(payload, "full_name", "name")).strip()
        role_title = str(_required(payload, "role_title", "role")).strip()
        legacy_role = str(payload.get("role_category") or "").strip().lower()
        authority_role = str(payload.get("authority_role") or legacy_role or "unknown").strip().lower()
        authority_aliases = {
            "decision_maker": "economic_buyer",
            "owner": "economic_buyer",
            "founder": "economic_buyer",
            "director": "economic_buyer",
            "marketing": "marketing_owner",
            "reception": "router",
            "receptionist": "router",
        }
        authority_role = authority_aliases.get(authority_role, authority_role)
        allowed_authority = {*BUYER_AUTHORITY_ROLES, "router", "influencer", "unknown"}
        if authority_role not in allowed_authority:
            raise ValueError("authority_role must be economic_buyer, marketing_owner, router, influencer, or unknown")
        role_category = legacy_role or authority_role
        source_url = str(payload.get("source_url") or "").strip() or None
        verified_at = str(payload.get("verified_at") or "").strip() or None
        verification_status = str(payload.get("verification_status") or "confirmed").strip()
        if verification_status not in {"unverified", "confirmed", "rejected"}:
            raise ValueError("invalid buyer verification_status")
        if verification_status == "confirmed" and (not source_url or not verified_at):
            raise ValueError("a confirmed buyer requires source_url and verified_at")
        if source_url and not _valid_http_url(source_url):
            raise ValueError("buyer source_url must be a public http(s) URL")
        if verified_at:
            _reject_future(verified_at, "verified_at")
            verified_at = _canonical_time(verified_at)
        expires_at = str(payload.get("expires_at") or "").strip() or (
            _plus_days(verified_at, 90) if verified_at else None
        )
        _validate_expiry(verified_at, expires_at)
        if expires_at:
            expires_at = _canonical_time(expires_at)
        is_primary = _bool(payload.get("is_primary", True))
        person_id = new_id("person")
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            if branch_id:
                self._ensure_branch(connection, account_id, branch_id)
            if is_primary:
                previous = list(
                    connection.execute(
                        "SELECT * FROM people WHERE account_id=? AND branch_id IS ? AND is_primary=1",
                        (account_id, branch_id),
                    )
                )
                connection.execute(
                    "UPDATE people SET is_primary=0,version=version+1,updated_at=? "
                    "WHERE account_id=? AND branch_id IS ? AND is_primary=1",
                    (now, account_id, branch_id),
                )
                for row in previous:
                    _audit(connection, actor=actor, entity_type="person", entity_id=row["id"], action="primary_replaced", before={"is_primary": True}, after={"is_primary": False})
            connection.execute(
                """
                INSERT INTO people
                  (id,account_id,full_name,role_title,role_category,authority_role,is_primary,status,
                   verification_status,source_url,verified_at,expires_at,notes,created_at,updated_at,
                   branch_id)
                VALUES (?,?,?,?,?,?,?, 'current',?,?,?,?,?,?,?,?)
                """,
                (person_id, account_id, full_name, role_title, role_category, authority_role, int(is_primary), verification_status, source_url, verified_at, expires_at, payload.get("notes"), now, now, branch_id),
            )
            _audit(connection, actor=actor, entity_type="person", entity_id=person_id, action="created", after={"account_id": account_id, "branch_id": branch_id, "full_name": full_name, "role_title": role_title, "authority_role": authority_role, "verification_status": verification_status})
        return {"id": person_id}

    def save_contact(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        branch_id = str(payload.get("branch_id") or "").strip() or None
        channel = str(_required(payload, "channel")).strip().lower()
        value = str(_required(payload, "value")).strip()
        normalized = normalize_destination(channel, value)
        if not normalized:
            raise ValueError("contact value could not be normalized")
        person_id = str(payload.get("person_id") or "").strip() or None
        route_scope = str(payload.get("route_scope") or ("person" if person_id else "organization"))
        if channel not in {"email", "phone", "sms", "whatsapp", "linkedin", "form", "other"}:
            raise ValueError("invalid contact channel")
        if route_scope not in {"person", "organization"}:
            raise ValueError("invalid route_scope")
        if route_scope == "person" and not person_id:
            raise ValueError("a person route requires an explicit person_id")
        if route_scope == "organization":
            person_id = None
        organization_exception = _bool(payload.get("organization_route_exception", False))
        source_url = str(payload.get("source_url") or "").strip() or None
        verified_at = str(payload.get("verified_at") or "").strip() or None
        verification_status = str(payload.get("verification_status") or "verified")
        if verification_status not in {"imported_unverified", "unverified", "verified", "invalid", "stale"}:
            raise ValueError("invalid contact verification_status")
        if verification_status == "verified" and (not source_url or not verified_at):
            raise ValueError("a verified contact requires source_url and verified_at")
        if source_url and not _valid_http_url(source_url):
            raise ValueError("contact source_url must be a public http(s) URL")
        if verification_status == "verified" and channel == "email":
            if not re.fullmatch(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", normalized, flags=re.I):
                raise ValueError("verified email route is not syntactically valid")
        if verification_status == "verified" and channel in {"phone", "sms", "whatsapp"}:
            digits = normalized[1:] if normalized.startswith("+") else normalized
            if not digits.isdigit() or not 10 <= len(digits) <= 15:
                raise ValueError("verified phone route must normalize to 10-15 digits")
        if verification_status == "verified" and channel in {"linkedin", "form"} and not _valid_http_url(value):
            raise ValueError(f"verified {channel} route must be an http(s) URL")
        if verified_at:
            _reject_future(verified_at, "verified_at")
            verified_at = _canonical_time(verified_at)
        if route_scope == "organization" and not organization_exception and verification_status == "verified":
            # Organization routes may be researched, but cannot pass readiness
            # until their exception is explicitly approved.
            organization_exception = False
        expires_at = str(payload.get("expires_at") or "").strip() or (
            _plus_days(verified_at, 90) if verified_at else None
        )
        _validate_expiry(verified_at, expires_at)
        if expires_at:
            expires_at = _canonical_time(expires_at)
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            if branch_id:
                self._ensure_branch(connection, account_id, branch_id)
            if person_id:
                person = connection.execute("SELECT * FROM people WHERE id=? AND account_id=?", (person_id, account_id)).fetchone()
                if not person:
                    raise ValueError("contact person does not belong to this account")
                if person["branch_id"] not in {None, branch_id}:
                    raise ValueError("contact person is not eligible for this branch scope")
            existing = connection.execute(
                "SELECT * FROM contact_points WHERE account_id=? AND channel=? AND normalized_value=?",
                (account_id, channel, normalized),
            ).fetchone()
            if existing:
                contact_id = existing["id"]
                connection.execute(
                    """
                    UPDATE contact_points SET person_id=?,value=?,route_scope=?,organization_route_exception=?,
                      verification_status=?,source_url=?,verified_at=?,expires_at=?,status='active',notes=?,
                      branch_id=?,version=version+1,updated_at=? WHERE id=?
                    """,
                    (person_id, value, route_scope, int(organization_exception), verification_status, source_url, verified_at, expires_at, payload.get("notes"), branch_id, now, contact_id),
                )
                _audit(connection, actor=actor, entity_type="contact_point", entity_id=contact_id, action="updated", before=dict(existing), after={"branch_id": branch_id, "verification_status": verification_status, "route_scope": route_scope, "organization_route_exception": organization_exception})
            else:
                contact_id = new_id("contact")
                connection.execute(
                    """
                    INSERT INTO contact_points
                      (id,account_id,person_id,channel,value,normalized_value,route_scope,
                       organization_route_exception,verification_status,source_url,verified_at,
                       expires_at,status,notes,created_at,updated_at,branch_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?,?,?)
                    """,
                    (contact_id, account_id, person_id, channel, value, normalized, route_scope, int(organization_exception), verification_status, source_url, verified_at, expires_at, payload.get("notes"), now, now, branch_id),
                )
                _audit(connection, actor=actor, entity_type="contact_point", entity_id=contact_id, action="created", after={"account_id": account_id, "branch_id": branch_id, "channel": channel, "verification_status": verification_status, "route_scope": route_scope})
        return {"id": contact_id}

    def save_channel_review(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        contact_id = str(payload.get("contact_point_id") or "").strip()
        purpose = str(payload.get("outreach_purpose") or "evidence_delivery").strip().lower()
        if purpose not in OUTREACH_PURPOSES:
            raise ValueError("outreach_purpose must be routing or evidence_delivery")
        decision = str(payload.get("decision") or ("approved" if _bool(payload.get("manual_approval")) else "pending"))
        if decision not in {"approved", "rejected", "pending"}:
            raise ValueError("invalid channel review decision")
        manual_approval = _bool(payload.get("manual_approval", False))
        lawful = str(payload.get("lawful_basis_status") or "not_assessed")
        privacy = str(payload.get("privacy_notice_status") or "not_assessed")
        allowed_reviews = {"reviewed", "not_assessed", "rejected"}
        if lawful not in allowed_reviews or privacy not in allowed_reviews:
            raise ValueError("lawful_basis_status and privacy_notice_status must be reviewed, not_assessed, or rejected")
        if decision == "approved" and (not manual_approval or lawful != "reviewed" or privacy != "reviewed"):
            raise ValueError("approved channel review requires explicit manual_approval and both governance checks reviewed")
        reviewed_at = str(payload.get("reviewed_at") or utc_now())
        _reject_future(reviewed_at, "reviewed_at")
        reviewed_at = _canonical_time(reviewed_at)
        expires_at = str(payload.get("expires_at") or "").strip() or _plus_days(reviewed_at, 30)
        _validate_expiry(reviewed_at, expires_at)
        expires_at = _canonical_time(expires_at)
        reviewer = str(payload.get("reviewer") or actor).strip()
        policy_version = str(payload.get("policy_version") or "").strip() or None
        jurisdiction = str(payload.get("jurisdiction") or "").strip() or None
        subscriber_type = str(payload.get("subscriber_type") or "not_assessed").strip().lower()
        if subscriber_type not in {"corporate", "individual", "sole_trader", "not_assessed"}:
            raise ValueError("invalid subscriber_type")
        suppression_screen = str(payload.get("suppression_screen_status") or "not_screened").strip().lower()
        if suppression_screen not in {"clear", "blocked", "not_screened"}:
            raise ValueError("invalid suppression_screen_status")
        preference_status = str(payload.get("preference_service_status") or "not_assessed").strip().lower()
        if preference_status not in {"clear", "blocked", "not_applicable", "not_assessed"}:
            raise ValueError("invalid preference_service_status")
        screened_at = str(payload.get("screened_at") or "").strip() or None
        screened_by = str(payload.get("screened_by") or reviewer).strip() or None
        screening_notes = str(payload.get("screening_notes") or "").strip() or None
        if screened_at:
            _reject_future(screened_at, "screened_at")
            screened_at = _canonical_time(screened_at)
            screen_age_at_review = _parse_time(reviewed_at) - _parse_time(screened_at)
            if screen_age_at_review.total_seconds() < 0:
                raise ValueError("screened_at cannot be later than reviewed_at")
            if screen_age_at_review > timedelta(hours=24):
                raise ValueError("approved route screening must be performed within 24 hours of review")
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            if decision == "approved" and not contact_id:
                raise ValueError("an approved review requires an explicit contact_point_id")
            if not contact_id:
                channel = str(payload.get("channel") or "").lower()
                row = connection.execute(
                    "SELECT id FROM contact_points WHERE account_id=? AND channel=? ORDER BY updated_at DESC LIMIT 1",
                    (account_id, channel),
                ).fetchone()
                if not row:
                    raise ValueError("contact_point_id is required")
                contact_id = row["id"]
            contact = connection.execute("SELECT * FROM contact_points WHERE id=? AND account_id=?", (contact_id, account_id)).fetchone()
            if not contact:
                raise ValueError("reviewed contact does not belong to this account")
            route_destination_key = destination_key(contact["channel"], contact["normalized_value"])
            if decision == "approved":
                if policy_version != CURRENT_OUTREACH_POLICY_VERSION:
                    raise ValueError(f"approved review must use current policy {CURRENT_OUTREACH_POLICY_VERSION}")
                if not jurisdiction or subscriber_type == "not_assessed":
                    raise ValueError("approved review requires jurisdiction and assessed subscriber_type")
                if suppression_screen != "clear" or not screened_at or not screened_by or not screening_notes:
                    raise ValueError("approved review requires a dated, named, documented clear suppression screen")
                if destination_family(contact["channel"]) == "phone":
                    if preference_status != "clear":
                        raise ValueError("phone-family routes require a clear preference-service screen")
                elif preference_status != "not_applicable":
                    raise ValueError("non-phone routes must explicitly mark preference-service screening not_applicable")
            review_id = new_id("review")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO channel_reviews
                  (id,account_id,contact_point_id,decision,manual_approval,lawful_basis_status,
                   privacy_notice_status,reviewer,reviewed_at,expires_at,notes,created_at,updated_at,
                   outreach_purpose,contact_version,destination_key,policy_version,jurisdiction,
                   subscriber_type,suppression_screen_status,preference_service_status,
                   screened_by,screened_at,screening_notes,policy_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (review_id, account_id, contact_id, decision, int(manual_approval), lawful, privacy, reviewer, reviewed_at, expires_at, payload.get("notes"), now, now, purpose, contact["version"], route_destination_key, policy_version, jurisdiction, subscriber_type, suppression_screen, preference_status, screened_by, screened_at, screening_notes, current_outreach_policy_sha256() if policy_version == CURRENT_OUTREACH_POLICY_VERSION else None),
            )
            _audit(connection, actor=actor, entity_type="channel_review", entity_id=review_id, action="created", after={"account_id": account_id, "contact_point_id": contact_id, "outreach_purpose": purpose, "decision": decision, "screening": suppression_screen})
        return {"id": review_id}

    def save_visibility_check(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        branch_id = str(payload.get("branch_id") or "").strip() or None
        treatment_slug = str(payload.get("treatment_slug") or "").strip() or None
        artifact = payload.get("check_dir") or payload.get("artifact_path")
        trusted = validate_visibility_artifact(artifact)
        artifact_account = trusted.get("raw_evidence", {}).get("check", {}).get("sales_account_id")
        if artifact_account != account_id:
            raise ValueError("visibility artifact sales_account_id does not match the target account")
        check_id = new_id("visibility")
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            treatment = self._ensure_branch_treatment_scope(
                connection,
                account_id,
                branch_id,
                treatment_slug,
            )
            if treatment:
                check = trusted.get("raw_evidence", {}).get("check", {})
                evidence = trusted.get("raw_evidence", {}).get("evidence", {})
                clinic_scope = check.get("clinic") if isinstance(check.get("clinic"), dict) else {}
                artifact_treatment = (
                    check.get("treatment_slug")
                    or clinic_scope.get("treatment_slug")
                    or evidence.get("treatment_slug")
                )
                if not artifact_treatment:
                    artifact_treatments = check.get("treatments") or clinic_scope.get("treatments")
                    if isinstance(artifact_treatments, list) and len(artifact_treatments) == 1:
                        artifact_treatment = artifact_treatments[0]
                if artifact_treatment:
                    accepted_names = {
                        str(treatment["slug"]).strip().casefold(),
                        str(treatment["canonical_name"]).strip().casefold(),
                        *{
                            str(alias).strip().casefold()
                            for alias in json_loads(treatment["aliases_json"], [])
                        },
                    }
                    if str(artifact_treatment).strip().casefold() not in accepted_names:
                        raise ValueError("visibility artifact treatment does not match treatment_slug")
            connection.execute(
                """
                INSERT INTO visibility_checks
                  (id,account_id,platform,browser_profile,logged_out_verified,checked_at,
                   expires_at,prompt_count,general_prompt_count,clinic_specific_prompt_count,
                   gap_prompt_count,conclusion,approval_status,approved_by,approved_at,
                   raw_evidence_json,notes,created_at,updated_at,evidence_standard,
                   base_prompt_count,run_count,observation_count,browser_profiles_json,
                   branch_id,treatment_slug)
                VALUES (?,?,?,?,1,?,?,?,?,?,?,?,'approved',?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (check_id, account_id, trusted["platform"], trusted["browser_profile"], trusted["checked_at"], trusted["expires_at"], trusted["prompt_count"], trusted["general_prompt_count"], trusted["clinic_specific_prompt_count"], trusted["gap_prompt_count"], trusted["conclusion"], trusted.get("approved_by") or actor, trusted.get("approved_at") or now, json_dumps(trusted["raw_evidence"]), payload.get("notes"), now, now, trusted["evidence_standard"], trusted["base_prompt_count"], trusted["run_count"], trusted["observation_count"], json_dumps(trusted["browser_profiles"]), branch_id, treatment_slug),
            )
            _audit(connection, actor=actor, entity_type="visibility_check", entity_id=check_id, action="canonical_artifact_imported", after={"account_id": account_id, "branch_id": branch_id, "treatment_slug": treatment_slug, "check_dir": trusted["raw_evidence"]["check_dir"], "expires_at": trusted["expires_at"]})
        return {"id": check_id, "validation": trusted["raw_evidence"]["validation"]}

    @staticmethod
    def _current_visibility(
        connection: sqlite3.Connection,
        account_id: str,
        *,
        branch_id: str | None = None,
        treatment_slug: str | None = None,
    ) -> sqlite3.Row | None:
        if branch_id:
            scope_clause = "AND branch_id=? AND treatment_slug=?"
            parameters = (account_id, branch_id, treatment_slug)
        else:
            scope_clause = "AND branch_id IS NULL"
            parameters = (account_id,)
        for row in connection.execute(
            "SELECT * FROM visibility_checks WHERE account_id=? AND approval_status='approved' "
            + scope_clause
            + " ORDER BY checked_at DESC",
            parameters,
        ):
            try:
                evidence = json_loads(row["raw_evidence_json"], {})
                validated = validate_visibility_artifact(evidence)
                if validated.get("raw_evidence", {}).get("check", {}).get("sales_account_id") == account_id:
                    return row
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def save_rationale(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        branch_id = str(payload.get("branch_id") or "").strip() or None
        treatment_slug = str(payload.get("treatment_slug") or "").strip() or None
        why = str(_required(payload, "why_chosen")).strip()
        evidence_text = str(_required(payload, "problem_evidence")).strip()
        approved = _bool(payload.get("approved")) or payload.get("status") == "approved"
        now = utc_now()
        rationale_id = new_id("rationale")
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            self._ensure_branch_treatment_scope(
                connection,
                account_id,
                branch_id,
                treatment_slug,
            )
            visibility = self._current_visibility(
                connection,
                account_id,
                branch_id=branch_id,
                treatment_slug=treatment_slug,
            )
            if approved and not visibility:
                raise ValueError("an approved rationale must cite a current valid visibility check")
            previous = connection.execute(
                "SELECT * FROM account_rationales WHERE account_id=? AND branch_id IS ? "
                "AND treatment_slug IS ? AND is_current=1",
                (account_id, branch_id, treatment_slug),
            ).fetchone()
            if previous:
                connection.execute("UPDATE account_rationales SET is_current=0,version=version+1,updated_at=? WHERE id=?", (now, previous["id"]))
                _audit(connection, actor=actor, entity_type="rationale", entity_id=previous["id"], action="superseded", before={"is_current": True}, after={"is_current": False})
            connection.execute(
                """
                INSERT INTO account_rationales
                  (id,account_id,visibility_check_id,why_chosen,problem_evidence,evidence_urls_json,
                   status,approved_by,approved_at,is_current,created_at,updated_at,branch_id,
                   treatment_slug)
                VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                """,
                (rationale_id, account_id, visibility["id"] if visibility else None, why, evidence_text, json_dumps(payload.get("evidence_urls") or []), "approved" if approved else str(payload.get("status") or "draft"), (payload.get("approved_by") or actor) if approved else None, now if approved else None, now, now, branch_id, treatment_slug),
            )
            _audit(connection, actor=actor, entity_type="rationale", entity_id=rationale_id, action="created", after={"account_id": account_id, "branch_id": branch_id, "treatment_slug": treatment_slug, "status": "approved" if approved else "draft", "visibility_check_id": visibility["id"] if visibility else None})
        return {"id": rationale_id}

    def save_draft(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        body = str(_required(payload, "body")).strip()
        purpose = str(payload.get("outreach_purpose") or "evidence_delivery").strip().lower()
        if purpose not in OUTREACH_PURPOSES:
            raise ValueError("outreach_purpose must be routing or evidence_delivery")
        branch_id = str(payload.get("branch_id") or "").strip() or None
        treatment_slug = str(payload.get("treatment_slug") or "").strip() or None
        if purpose == "routing" and (branch_id or treatment_slug):
            raise ValueError("routing drafts are clinic-wide and do not accept branch scope")
        approved = _bool(payload.get("approved")) or payload.get("status") == "approved"
        routing_scope_attested = _bool(payload.get("routing_scope_attested", False))
        routing_errors = (
            routing_message_errors(body, subject=payload.get("subject"))
            if purpose == "routing"
            else []
        )
        if approved and purpose == "routing":
            if not routing_scope_attested:
                raise ValueError("approved routing message requires the buyer-identification-only attestation")
            if routing_errors:
                raise ValueError("routing message is outside the buyer-identification-only scope: " + "; ".join(routing_errors))
        content_policy_version = (
            ROUTING_MESSAGE_POLICY_VERSION
            if purpose == "routing" and routing_scope_attested and not routing_errors
            else None
        )
        draft_id = new_id("draft")
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            self._ensure_branch_treatment_scope(
                connection,
                account_id,
                branch_id,
                treatment_slug,
            )
            readiness = evaluate_readiness(
                connection,
                account_id,
                branch_id=branch_id,
                treatment_slug=treatment_slug,
            )
            selection = readiness_for_purpose(readiness, purpose)["selection"]
            contact_id = str(payload.get("contact_point_id") or selection.get("contact_point_id") or "").strip() or None
            selected_person = selection.get("router_id") if purpose == "routing" else selection.get("buyer_id")
            person_id = str(payload.get("person_id") or selected_person or "").strip() or None
            visibility_id = selection.get("visibility_check_id") if purpose == "evidence_delivery" else None
            contact = connection.execute("SELECT * FROM contact_points WHERE id=? AND account_id=?", (contact_id, account_id)).fetchone() if contact_id else None
            person = connection.execute("SELECT * FROM people WHERE id=? AND account_id=?", (person_id, account_id)).fetchone() if person_id else None
            if person_id and not person:
                raise ValueError("draft person does not belong to this account")
            if contact_id and not contact:
                raise ValueError("draft contact does not belong to this account")
            eligible_branches = {None, branch_id} if branch_id else {None}
            if person and person["branch_id"] not in eligible_branches:
                raise ValueError("draft person is not eligible for this branch scope")
            if contact and contact["branch_id"] not in eligible_branches:
                raise ValueError("draft contact is not eligible for this branch scope")
            if purpose == "routing":
                if contact and contact["route_scope"] == "organization":
                    if person_id:
                        raise ValueError("a general routing message must not be addressed to an inferred person")
                elif contact and (
                    not person
                    or contact["person_id"] != person["id"]
                    or person["authority_role"] != "router"
                ):
                    raise ValueError("a person-specific routing message must use a confirmed router route")
                if approved and not contact:
                    raise ValueError("an approved routing draft must be tied to a verified routing route")
            else:
                if person and person["authority_role"] not in BUYER_AUTHORITY_ROLES:
                    raise ValueError("evidence delivery requires an economic buyer or marketing owner")
                if contact and person and not (
                    (contact["route_scope"] == "person" and contact["person_id"] == person["id"])
                    or (contact["route_scope"] == "organization" and contact["organization_route_exception"] == 1)
                ):
                    raise ValueError("draft buyer and contact are not an approved evidence-delivery pair")
                if approved and (not contact or not person or not visibility_id):
                    raise ValueError("an approved evidence draft must use a confirmed buyer, selected route, and current visibility check")
            channel = str(payload.get("channel") or (contact["channel"] if contact else "")).strip().lower()
            if not channel:
                raise ValueError("draft channel is required")
            if contact and channel != contact["channel"]:
                raise ValueError("draft channel does not match the selected contact")
            subject = payload.get("subject")
            content_sha256 = (
                message_content_sha256(
                    account_id=account_id,
                    person_id=person_id,
                    contact_point_id=contact_id,
                    visibility_check_id=visibility_id,
                    subject=subject,
                    body=body,
                    channel=channel,
                    outreach_purpose=purpose,
                    branch_id=branch_id,
                    treatment_slug=treatment_slug,
                )
                if approved else None
            )
            connection.execute(
                """
                INSERT INTO message_drafts
                  (id,account_id,person_id,contact_point_id,visibility_check_id,channel,subject,
                   body,status,approved_by,approved_at,created_at,updated_at,outreach_purpose,
                   routing_scope_attested,content_policy_version,content_sha256,branch_id,
                   treatment_slug)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (draft_id, account_id, person_id, contact_id, visibility_id, channel, subject, body, "approved" if approved else str(payload.get("status") or "draft"), (payload.get("approved_by") or actor) if approved else None, now if approved else None, now, now, purpose, int(routing_scope_attested), content_policy_version, content_sha256, branch_id, treatment_slug),
            )
            _audit(connection, actor=actor, entity_type="message_draft", entity_id=draft_id, action="created", after={"account_id": account_id, "branch_id": branch_id, "treatment_slug": treatment_slug, "outreach_purpose": purpose, "status": "approved" if approved else "draft", "contact_point_id": contact_id, "visibility_check_id": visibility_id})
        return {"id": draft_id}

    def approve_draft(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        draft_id = str(_required(payload, "draft_id"))
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            draft = connection.execute("SELECT * FROM message_drafts WHERE id=? AND account_id=?", (draft_id, account_id)).fetchone()
            if not draft:
                raise NotFoundError(f"draft not found: {draft_id}")
            purpose = draft["outreach_purpose"]
            branch_id = draft["branch_id"]
            treatment_slug = draft["treatment_slug"]
            if purpose == "routing" and (branch_id or treatment_slug):
                raise ValueError("routing drafts are clinic-wide and do not accept branch scope")
            self._ensure_branch_treatment_scope(
                connection,
                account_id,
                branch_id,
                treatment_slug,
            )
            routing_scope_attested = _bool(
                payload.get("routing_scope_attested", draft["routing_scope_attested"])
            )
            if purpose == "routing":
                routing_errors = routing_message_errors(draft["body"], subject=draft["subject"])
                if not routing_scope_attested or routing_errors:
                    detail = "; ".join(routing_errors) if routing_errors else "attestation is missing"
                    raise ValueError("routing draft is outside the buyer-identification-only scope: " + detail)
            visibility = (
                self._current_visibility(
                    connection,
                    account_id,
                    branch_id=branch_id,
                    treatment_slug=treatment_slug,
                )
                if purpose == "evidence_delivery"
                else None
            )
            if not draft["contact_point_id"] or (purpose == "evidence_delivery" and not visibility):
                raise ValueError("draft is missing the route or current evidence required for its purpose")
            visibility_id = visibility["id"] if visibility else None
            content_sha256 = message_content_sha256(
                account_id=account_id,
                person_id=draft["person_id"],
                contact_point_id=draft["contact_point_id"],
                visibility_check_id=visibility_id,
                subject=draft["subject"],
                body=draft["body"],
                channel=draft["channel"],
                outreach_purpose=purpose,
                branch_id=branch_id,
                treatment_slug=treatment_slug,
            )
            connection.execute("UPDATE message_drafts SET status='approved',visibility_check_id=?,approved_by=?,approved_at=?,routing_scope_attested=?,content_policy_version=?,content_sha256=?,version=version+1,updated_at=? WHERE id=?", (visibility_id, payload.get("approved_by") or actor, now, int(routing_scope_attested), ROUTING_MESSAGE_POLICY_VERSION if purpose == "routing" else draft["content_policy_version"], content_sha256, now, draft_id))
            _audit(connection, actor=actor, entity_type="message_draft", entity_id=draft_id, action="approved", before={"status": draft["status"]}, after={"status": "approved", "branch_id": branch_id, "treatment_slug": treatment_slug, "outreach_purpose": purpose, "visibility_check_id": visibility_id})
        return {"id": draft_id, "status": "approved"}

    def set_priority_override(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        active = _bool(payload.get("active", payload.get("priority_override", True)))
        reason = str(payload.get("reason") or "").strip() or None
        if active and not reason:
            raise ValueError("a priority override requires a documented reason")
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            before = self._ensure_account(connection, account_id)
            connection.execute("UPDATE sales_accounts SET priority_override=?,priority_override_reason=?,version=version+1,updated_at=? WHERE id=?", (int(active), reason, now, account_id))
            _audit(connection, actor=actor, entity_type="sales_account", entity_id=account_id, action="priority_override_changed", before={"active": bool(before["priority_override"]), "reason": before["priority_override_reason"]}, after={"active": active, "reason": reason})
        return {"id": account_id, "priority_override": active, "reason": reason}

    def set_stage(self, payload: dict[str, Any], *, actor: str = "founder", connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        stage = str(_required(payload, "stage")).strip().lower()
        allowed = {"research", "contacted", "replied", "positive", "meeting", "qualified", "proposal", "won", "lost", "nurture"}
        if stage not in allowed:
            raise ValueError("invalid sales stage; readiness is computed and cannot be set as a stage")
        own_connection = connection is None
        conn = connection or connect(self.db_path)
        try:
            context = transaction(conn, immediate=True) if own_connection else _null_transaction(conn)
            with context:
                before = self._ensure_account(conn, account_id)
                expected = payload.get("expected_version")
                if expected is not None and int(expected) != before["version"]:
                    raise ConflictError(f"account version changed (expected {expected}, found {before['version']})")
                now = utc_now()
                status = "won" if stage == "won" else "lost" if stage == "lost" else "nurture" if stage == "nurture" else before["status"]
                conn.execute("UPDATE sales_accounts SET stage=?,status=?,version=version+1,updated_at=? WHERE id=?", (stage, status, now, account_id))
                opportunity = conn.execute("SELECT * FROM opportunities WHERE account_id=?", (account_id,)).fetchone()
                closed_at = now if stage in {"won", "lost"} else None
                if opportunity:
                    conn.execute("UPDATE opportunities SET stage=?,value_minor=COALESCE(?,value_minor),currency=COALESCE(?,currency),outcome_reason=COALESCE(?,outcome_reason),closed_at=?,version=version+1,updated_at=? WHERE account_id=?", (stage, payload.get("value_minor"), payload.get("currency"), payload.get("outcome_reason"), closed_at, now, account_id))
                    _audit(conn, actor=actor, entity_type="opportunity", entity_id=opportunity["id"], action="stage_changed", before={"stage": opportunity["stage"]}, after={"stage": stage})
                else:
                    opportunity_id = new_id("opportunity")
                    conn.execute("INSERT INTO opportunities (id,account_id,stage,value_minor,currency,outcome_reason,opened_at,closed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (opportunity_id, account_id, stage, payload.get("value_minor"), payload.get("currency") or "GBP", payload.get("outcome_reason"), now, closed_at, now, now))
                    _audit(conn, actor=actor, entity_type="opportunity", entity_id=opportunity_id, action="created", after={"account_id": account_id, "stage": stage})
                _audit(conn, actor=actor, entity_type="sales_account", entity_id=account_id, action="stage_changed", before={"stage": before["stage"], "status": before["status"]}, after={"stage": stage, "status": status})
            return {"id": account_id, "stage": stage, "status": status}
        finally:
            if own_connection:
                conn.close()

    def log_interaction(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        supplied_branch_id = str(payload.get("branch_id") or "").strip() or None
        purpose = str(payload.get("outreach_purpose") or "evidence_delivery").strip().lower()
        if purpose not in OUTREACH_PURPOSES:
            raise ValueError("outreach_purpose must be routing or evidence_delivery")
        interaction_type = str(_required(payload, "interaction_type", "type")).strip()
        direction = str(payload.get("direction") or "outbound").strip().lower()
        occurred_at = str(payload.get("occurred_at") or payload.get("date") or utc_now())
        _parse_time(occurred_at)
        if direction not in {"outbound", "inbound", "internal"}:
            raise ValueError("interaction direction must be outbound, inbound, or internal")
        outbound_touch = direction == "outbound"
        historical = _bool(payload.get("historical", False))
        interaction_id = new_id("interaction")
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            if supplied_branch_id:
                self._ensure_branch(connection, account_id, supplied_branch_id)
            branch_id = supplied_branch_id
            treatment_slug: str | None = None
            export_id = str(payload.get("export_id") or "").strip() or None
            contact_id = str(payload.get("contact_point_id") or "").strip() or None
            draft_id = str(payload.get("message_draft_id") or payload.get("draft_id") or "").strip() or None
            supplied_contact_id = contact_id
            supplied_draft_id = draft_id
            bound_draft = (
                connection.execute(
                    "SELECT * FROM message_drafts WHERE id=? AND account_id=?",
                    (draft_id, account_id),
                ).fetchone()
                if draft_id
                else None
            )
            if bound_draft:
                if supplied_branch_id and supplied_branch_id != bound_draft["branch_id"]:
                    raise ValueError("supplied branch_id does not match the bound draft")
                branch_id = bound_draft["branch_id"]
                treatment_slug = bound_draft["treatment_slug"]
            if outbound_touch:
                if historical:
                    if export_id or supplied_contact_id or supplied_draft_id or _bool(payload.get("enforce_readiness", False)):
                        raise ValueError("historical outbound activity cannot be combined with a send/export route")
                    dedupe_key = str(payload.get("dedupe_key") or "").strip() or None
                elif export_id:
                    exported = connection.execute(
                        """
                        SELECT oei.*,oe.outreach_purpose FROM outreach_export_items oei JOIN outreach_exports oe ON oe.id=oei.export_id
                        WHERE oei.export_id=? AND oei.account_id=? AND oe.status='complete'
                        """,
                        (export_id, account_id),
                    ).fetchone()
                    if not exported:
                        raise ValueError("outbound interaction export reference is not valid for this account")
                    if supplied_contact_id and supplied_contact_id != exported["contact_point_id"]:
                        raise ValueError("supplied contact does not match the exported outreach row")
                    if supplied_draft_id and supplied_draft_id != exported["message_draft_id"]:
                        raise ValueError("supplied draft does not match the exported outreach row")
                    if payload.get("outreach_purpose") and purpose != exported["outreach_purpose"]:
                        raise ValueError("supplied outreach_purpose does not match the export")
                    purpose = exported["outreach_purpose"]
                    contact_id = exported["contact_point_id"]
                    draft_id = exported["message_draft_id"]
                    if supplied_branch_id and supplied_branch_id != exported["branch_id"]:
                        raise ValueError("supplied branch_id does not match the export")
                    branch_id = exported["branch_id"]
                    bound_draft = connection.execute(
                        "SELECT * FROM message_drafts WHERE id=? AND account_id=?",
                        (draft_id, account_id),
                    ).fetchone()
                    if not bound_draft or bound_draft["branch_id"] != branch_id:
                        raise ValueError("exported draft scope is not valid for this account")
                    treatment_slug = bound_draft["treatment_slug"]
                    dedupe_key = str(payload.get("dedupe_key") or f"export:{export_id}:{account_id}")
                else:
                    if not (supplied_contact_id or supplied_draft_id or _bool(payload.get("enforce_readiness", False))):
                        raise ValueError("outbound activity without a send route must set historical=true")
                    if purpose == "routing" and branch_id:
                        raise ValueError("routing interactions are clinic-wide")
                    if branch_id and not treatment_slug:
                        raise ValueError("branch-scoped outbound activity requires a branch-scoped draft")
                    readiness = readiness_for_purpose(
                        evaluate_readiness(
                            connection,
                            account_id,
                            branch_id=branch_id,
                            treatment_slug=treatment_slug,
                        ),
                        purpose,
                    )
                    if not readiness["ready"]:
                        raise ValueError("outbound touch blocked by readiness gates: " + ", ".join(readiness["failed_gate_codes"]))
                    selected_contact = readiness["selection"]["contact_point_id"]
                    selected_draft = readiness["selection"]["message_draft_id"]
                    if supplied_contact_id and supplied_contact_id != selected_contact:
                        raise ValueError("supplied contact is not the route selected by current readiness")
                    if supplied_draft_id and supplied_draft_id != selected_draft:
                        raise ValueError("supplied draft is not the message selected by current readiness")
                    contact_id = selected_contact
                    draft_id = selected_draft
                    dedupe_key = str(payload.get("dedupe_key") or f"draft:{draft_id}")
                if dedupe_key and connection.execute("SELECT 1 FROM interactions WHERE dedupe_key=?", (dedupe_key,)).fetchone():
                    raise ConflictError("this outreach step has already been logged")
                # Suppression and canonical artifact validity are checked again at
                # the action boundary.  Export references permit the already-
                # exported draft, but never bypass a newly added suppression.
                if not historical:
                    if branch_id:
                        self._ensure_branch_treatment_scope(
                            connection,
                            account_id,
                            branch_id,
                            treatment_slug,
                        )
                    preflight = readiness_for_purpose(
                        evaluate_readiness(
                            connection,
                            account_id,
                            branch_id=branch_id,
                            treatment_slug=treatment_slug,
                        ),
                        purpose,
                    )
                    unsafe = [code for code in preflight["failed_gate_codes"] if code in {"identity", "priority", "buyer", "contact", "governance", "screening", "suppression", "destination_unique", "visibility", "rationale", "message"}]
                    if unsafe:
                        raise ValueError("outbound touch blocked by current safety gates: " + ", ".join(unsafe))
                    if export_id and (
                        preflight["selection"]["contact_point_id"] != contact_id
                        or preflight["selection"]["message_draft_id"] != draft_id
                    ):
                        raise ValueError("exported contact/draft is no longer the current safe outreach pair")
            else:
                dedupe_key = str(payload.get("dedupe_key") or "").strip() or None
            if purpose == "routing" and branch_id:
                raise ValueError("routing interactions are clinic-wide")
            data = dict(payload.get("payload") or {})
            if contact_id:
                contact = connection.execute("SELECT * FROM contact_points WHERE id=? AND account_id=?", (contact_id, account_id)).fetchone()
                if not contact:
                    raise ValueError("interaction contact does not belong to this account")
                eligible_branches = {None, branch_id} if branch_id else {None}
                if contact["branch_id"] not in eligible_branches:
                    raise ValueError("interaction contact is not eligible for this branch scope")
                supplied_channel = str(payload.get("channel") or "").strip().lower()
                if outbound_touch and not historical and supplied_channel and supplied_channel != contact["channel"]:
                    raise ValueError("interaction channel does not match the selected contact")
                data["destination_key"] = destination_key(contact["channel"], contact["normalized_value"])
            if draft_id:
                draft = connection.execute(
                    "SELECT * FROM message_drafts WHERE id=? AND account_id=? AND contact_point_id=? AND outreach_purpose=? AND status='approved'",
                    (draft_id, account_id, contact_id, purpose),
                ).fetchone()
                if not draft:
                    raise ValueError("interaction draft is not an approved message for the selected contact")
                if draft["branch_id"] != branch_id:
                    raise ValueError("interaction draft does not match the interaction branch scope")
                data["message_draft_id"] = draft_id
            if payload.get("person_id"):
                person = connection.execute(
                    "SELECT * FROM people WHERE id=? AND account_id=?",
                    (payload.get("person_id"), account_id),
                ).fetchone()
                if not person:
                    raise ValueError("interaction person does not belong to this account")
                eligible_branches = {None, branch_id} if branch_id else {None}
                if person["branch_id"] not in eligible_branches:
                    raise ValueError("interaction person is not eligible for this branch scope")
            if export_id:
                data["export_id"] = export_id
            data["historical"] = historical
            stored_channel = contact["channel"] if contact_id else payload.get("channel")
            now = utc_now()
            insert_interaction_record(connection, {
                "id": interaction_id, "account_id": account_id,
                "person_id": payload.get("person_id"), "contact_point_id": contact_id,
                "message_draft_id": draft_id, "export_id": export_id,
                "sequence_step": payload.get("sequence_step"),
                "interaction_type": interaction_type, "direction": direction,
                "channel": stored_channel, "occurred_at": occurred_at,
                "outcome": payload.get("outcome"), "notes": payload.get("notes"),
                "dedupe_key": dedupe_key, "payload_json": json_dumps(data),
                "created_at": now, "outreach_purpose": purpose, "branch_id": branch_id,
            })
            _audit(connection, actor=actor, entity_type="interaction", entity_id=interaction_id, action="logged", after={"account_id": account_id, "branch_id": branch_id, "outreach_purpose": purpose, "type": interaction_type, "direction": direction, "historical": historical, "dedupe_key": dedupe_key})
            stage = str(payload.get("stage") or payload.get("intended_stage") or ("contacted" if outbound_touch and not historical else "")).strip()
            if stage:
                self.set_stage(
                    {
                        "account_id": account_id,
                        "stage": stage,
                        "outcome_reason": payload.get("outcome"),
                        "expected_version": payload.get("expected_version"),
                    },
                    actor=actor,
                    connection=connection,
                )
        return {"id": interaction_id, "stage": stage or None}

    def suppress(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        branch_id = str(payload.get("branch_id") or "").strip() or None
        scope_type = str(payload.get("scope_type") or "account").strip().lower()
        reason = str(_required(payload, "reason")).strip()
        channel = str(payload.get("channel") or "").strip().lower() or None
        scope_value = str(payload.get("scope_value") or "").strip() or None
        person_id = str(payload.get("person_id") or "").strip() or None
        channels = {"email", "phone", "sms", "whatsapp", "linkedin", "form", "other"}
        if scope_type not in {"account", "person", "destination", "channel", "domain"}:
            raise ValueError("invalid suppression scope_type")
        if branch_id and scope_type != "account":
            raise ValueError("branch suppressions must use account scope_type")
        if scope_type == "account" and (person_id or scope_value or channel):
            raise ValueError("account suppression does not accept person, destination, or channel fields")
        if scope_type == "person" and (not person_id or scope_value or channel):
            raise ValueError("person suppression requires only person_id")
        if scope_type == "channel" and (channel not in channels or person_id or scope_value):
            raise ValueError("channel suppression requires only a valid channel")
        if scope_type == "destination":
            if channel not in channels or not scope_value or person_id:
                raise ValueError("destination suppression requires channel and scope_value")
            scope_value = normalize_destination(channel, scope_value)
            if channel == "email" and not re.fullmatch(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", scope_value, flags=re.I):
                raise ValueError("destination suppression email is invalid")
            if channel in {"phone", "sms", "whatsapp"}:
                digits = scope_value[1:] if scope_value.startswith("+") else scope_value
                if not digits.isdigit() or not 10 <= len(digits) <= 15:
                    raise ValueError("destination suppression phone is invalid")
        if scope_type == "domain":
            if not scope_value or person_id or (channel and channel != "email"):
                raise ValueError("domain suppression requires only a domain")
            scope_value = scope_value.lower().lstrip("@").strip().rstrip(".")
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", scope_value):
                raise ValueError("domain suppression value is invalid")
            channel = "email"
        suppression_id = new_id("suppression")
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            if branch_id:
                self._ensure_branch(connection, account_id, branch_id)
            if person_id and not connection.execute("SELECT 1 FROM people WHERE id=? AND account_id=?", (person_id, account_id)).fetchone():
                raise ValueError("suppressed person does not belong to this account")
            connection.execute(
                """
                INSERT INTO suppressions
                  (id,account_id,person_id,scope_type,scope_value,channel,reason,source,active,
                   created_by,created_at,branch_id)
                VALUES (?,?,?,?,?,?,?,?,1,?,?,?)
                """,
                (suppression_id, account_id, person_id, scope_type, scope_value, channel, reason, payload.get("source") or "manual", actor, now, branch_id),
            )
            _audit(connection, actor=actor, entity_type="suppression", entity_id=suppression_id, action="created", after={"account_id": account_id, "branch_id": branch_id, "scope_type": scope_type, "reason": reason})
        return {"id": suppression_id, "active": True}

    def unsuppress(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        suppression_id = str(_required(payload, "suppression_id"))
        reason = str(_required(payload, "lift_reason")).strip()
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            row = connection.execute("SELECT * FROM suppressions WHERE id=? AND account_id=?", (suppression_id, account_id)).fetchone()
            if not row:
                raise NotFoundError(f"suppression not found: {suppression_id}")
            connection.execute("UPDATE suppressions SET active=0,lifted_by=?,lifted_at=?,lift_reason=? WHERE id=?", (actor, now, reason, suppression_id))
            _audit(connection, actor=actor, entity_type="suppression", entity_id=suppression_id, action="lifted", before={"active": bool(row["active"])}, after={"active": False, "lift_reason": reason})
        return {"id": suppression_id, "active": False}

    def resolve_import_conflict(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        conflict_id = str(_required(payload, "conflict_id"))
        notes = str(_required(payload, "resolution_notes")).strip()
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            row = connection.execute("SELECT * FROM import_conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not row or row["sales_account_id"] != account_id:
                raise ValueError("import conflict does not belong to this account")
            connection.execute("UPDATE import_conflicts SET status='resolved',resolved_at=?,resolved_by=?,resolution_notes=? WHERE id=?", (now, actor, notes, conflict_id))
            _audit(connection, actor=actor, entity_type="import_conflict", entity_id=conflict_id, action="resolved", before={"status": row["status"]}, after={"status": "resolved", "notes": notes})
        return {"id": conflict_id, "status": "resolved"}

    def add_to_worklist(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        name = str(payload.get("worklist_name") or "founder-25").strip()
        now = utc_now()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            self._ensure_account(connection, account_id)
            if not self._snapshot(connection, account_id):
                raise ValueError("account is not present in the current connected research release")
            worklist = connection.execute("SELECT * FROM worklists WHERE name=? AND status='active'", (name,)).fetchone()
            if not worklist:
                worklist_id = new_id("worklist")
                connection.execute("INSERT INTO worklists (id,name,description,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)", (worklist_id, name, "Founder worklist", actor, now, now))
                _audit(connection, actor=actor, entity_type="worklist", entity_id=worklist_id, action="created", after={"name": name})
            else:
                worklist_id = worklist["id"]
            existing = connection.execute("SELECT * FROM worklist_items WHERE worklist_id=? AND account_id=?", (worklist_id, account_id)).fetchone()
            if not existing:
                position = connection.execute("SELECT COALESCE(MAX(position),0)+1 AS n FROM worklist_items WHERE worklist_id=?", (worklist_id,)).fetchone()["n"]
                if position > 25:
                    raise ValueError("worklist already contains 25 accounts")
                connection.execute("INSERT INTO worklist_items (worklist_id,account_id,position,selection_reason,added_at) VALUES (?,?,?,?,?)", (worklist_id, account_id, position, payload.get("selection_reason"), now))
                _audit(connection, actor=actor, entity_type="worklist_item", entity_id=f"{worklist_id}:{account_id}", action="added", after={"position": position})
        with closing(connect(self.db_path)) as connection:
            return self._worklist_result(connection, worklist_id)

    def remove_from_worklist(self, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        account_id = str(_required(payload, "account_id"))
        name = str(payload.get("worklist_name") or "founder-25").strip()
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            worklist = connection.execute("SELECT * FROM worklists WHERE name=?", (name,)).fetchone()
            if not worklist:
                raise NotFoundError(f"worklist not found: {name}")
            deleted = connection.execute("DELETE FROM worklist_items WHERE worklist_id=? AND account_id=?", (worklist["id"], account_id)).rowcount
            if deleted:
                # Positions are presentation state, so compact them atomically.
                rows = list(connection.execute("SELECT account_id FROM worklist_items WHERE worklist_id=? ORDER BY position", (worklist["id"],)))
                for position, row in enumerate(rows, 1):
                    connection.execute("UPDATE worklist_items SET position=? WHERE worklist_id=? AND account_id=?", (position, worklist["id"], row["account_id"]))
                _audit(connection, actor=actor, entity_type="worklist_item", entity_id=f"{worklist['id']}:{account_id}", action="removed", before={"present": True}, after={"present": False})
        with closing(connect(self.db_path)) as connection:
            return self._worklist_result(connection, worklist["id"])

    def export_ready(
        self,
        *,
        worklist_name: str = "founder-25",
        outreach_purpose: str = "evidence_delivery",
        branch_id: str | None = None,
        treatment_slug: str | None = None,
        dry_run: bool = True,
        actor: str = "founder",
        destination: Path | str | None = None,
    ) -> dict[str, Any]:
        purpose = str(outreach_purpose or "evidence_delivery").strip().lower()
        if purpose not in OUTREACH_PURPOSES:
            raise ValueError("outreach_purpose must be routing or evidence_delivery")
        branch_id = str(branch_id or "").strip() or None
        treatment_slug = str(treatment_slug or "").strip() or None
        if purpose == "routing":
            branch_id = None
            treatment_slug = None
        elif bool(branch_id) != bool(treatment_slug):
            raise ValueError("branch_id and treatment_slug must be supplied together")
        export_id = new_id("export")
        exports_dir = (self.root / "data/outreach/exports").resolve()
        output_path = (
            Path(destination).expanduser().resolve()
            if destination
            else exports_dir / f"{export_id}.csv"
        )
        if output_path.suffix.lower() != ".csv":
            raise ValueError("outreach export destination must be a new .csv file")
        if destination and output_path.exists():
            raise ValueError("outreach export destination already exists; choose a new file")
        immutable_database_dir = (self.root / "data/database").resolve()
        private_outreach_dir = (self.root / "data/outreach").resolve()
        if output_path.is_relative_to(immutable_database_dir):
            raise ValueError("outreach exports cannot be written inside the immutable research database")
        if output_path.is_relative_to(private_outreach_dir) and not output_path.is_relative_to(exports_dir):
            raise ValueError("outreach exports inside data/outreach must stay in its exports directory")
        verified_release = verify_current_release(self.root)
        ready_rows: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
            active_release = self._latest_release(connection)
            if not active_release:
                raise IntegrityError("no research release is connected; sync the current release before export")
            if (
                active_release["release_id"] != verified_release.release_id
                or active_release["manifest_sha256"] != verified_release.manifest_sha256
            ):
                raise IntegrityError("the current research release differs from the outreach workspace; sync it before export")
            branch_account_id: str | None = None
            if branch_id:
                branch = connection.execute(
                    "SELECT * FROM clinic_branches WHERE id=?",
                    (branch_id,),
                ).fetchone()
                if not branch:
                    raise ValueError(f"unknown branch_id: {branch_id}")
                branch_account_id = branch["sales_account_id"]
                self._ensure_active_treatment(connection, str(treatment_slug))
            worklist = connection.execute("SELECT * FROM worklists WHERE name=? AND status='active'", (worklist_name,)).fetchone()
            if not worklist:
                raise NotFoundError(f"active worklist not found: {worklist_name}")
            for item in connection.execute("SELECT * FROM worklist_items WHERE worklist_id=? ORDER BY position", (worklist["id"],)):
                account_id = item["account_id"]
                if branch_account_id and account_id != branch_account_id:
                    blocked.append({"account_id": account_id, "failed_gate_codes": ["branch_scope"]})
                    continue
                readiness = readiness_for_purpose(
                    evaluate_readiness(
                        connection,
                        account_id,
                        branch_id=branch_id,
                        treatment_slug=treatment_slug,
                    ),
                    purpose,
                )
                if not readiness["ready"]:
                    blocked.append({"account_id": account_id, "failed_gate_codes": readiness["failed_gate_codes"]})
                    continue
                selected = readiness["selection"]
                account = self._ensure_account(connection, account_id)
                snapshot = self._snapshot(connection, account_id)
                person_id = selected.get("buyer_id") if purpose == "evidence_delivery" else selected.get("router_id")
                buyer = connection.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone() if person_id else None
                contact = connection.execute("SELECT * FROM contact_points WHERE id=?", (selected["contact_point_id"],)).fetchone()
                draft = connection.execute("SELECT * FROM message_drafts WHERE id=?", (selected["message_draft_id"],)).fetchone()
                rationale = connection.execute("SELECT * FROM account_rationales WHERE id=?", (selected["rationale_id"],)).fetchone() if selected.get("rationale_id") else None
                required_records = (snapshot, contact, draft) if purpose == "routing" else (snapshot, buyer, contact, draft, rationale)
                if not all(required_records):
                    blocked.append({"account_id": account_id, "failed_gate_codes": ["transactional_selection_missing"]})
                    continue
                route_destination_key = destination_key(contact["channel"], contact["normalized_value"])
                ready_rows.append(
                    {
                        "account_id": account_id,
                        "outreach_purpose": purpose,
                        "clinic_name": account["display_name_override"] or snapshot["clinic_name"],
                        "buyer_name": buyer["full_name"] if buyer else "",
                        "buyer_role": buyer["role_title"] if buyer else "",
                        "channel": contact["channel"],
                        "destination": contact["value"],
                        "destination_key": route_destination_key,
                        "subject": draft["subject"] or "",
                        "message": draft["body"],
                        "why_chosen": rationale["why_chosen"] if rationale else "Buyer-identification enquiry only",
                        "problem_evidence": rationale["problem_evidence"] if rationale else "No visibility findings may be sent in this routing lane.",
                        "contact_point_id": contact["id"],
                        "message_draft_id": draft["id"],
                        "readiness": readiness,
                        **(
                            {"branch_id": branch_id, "treatment_slug": treatment_slug}
                            if branch_id
                            else {}
                        ),
                    }
                )
            if dry_run:
                result = {
                    "dry_run": True,
                    "worklist_name": worklist_name,
                    "outreach_purpose": purpose,
                    "count": len(ready_rows),
                    "blocked_count": len(blocked),
                    "rows": ready_rows,
                    "blocked": blocked,
                    "path": None,
                }
                if branch_id:
                    result["branch_id"] = branch_id
                    result["treatment_slug"] = treatment_slug
                return result
            if not ready_rows:
                raise ValueError("no accounts pass every outreach-readiness gate")
            now = utc_now()
            connection.execute("""
                INSERT INTO outreach_exports
                  (id,worklist_id,status,format,row_count,created_by,created_at,outreach_purpose,
                   research_release_id,research_manifest_sha256)
                VALUES (?,?,'prepared','csv',?,?,?,?,?,?)
                """, (export_id, worklist["id"], len(ready_rows), actor, now, purpose, verified_release.release_id, verified_release.manifest_sha256))
            for row in ready_rows:
                connection.execute(
                    """
                    INSERT INTO outreach_export_items
                      (export_id,account_id,contact_point_id,message_draft_id,destination_key,
                       readiness_json,branch_id)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (export_id, row["account_id"], row["contact_point_id"], row["message_draft_id"], row["destination_key"], json_dumps(row["readiness"]), branch_id),
                )
            _audit(connection, actor=actor, entity_type="outreach_export", entity_id=export_id, action="prepared", after={"worklist": worklist_name, "outreach_purpose": purpose, "branch_id": branch_id, "treatment_slug": treatment_slug, "row_count": len(ready_rows), "research_release_id": verified_release.release_id, "manifest_sha256": verified_release.manifest_sha256})

        verified_again = verify_current_release(self.root)
        if (
            verified_again.release_id != verified_release.release_id
            or verified_again.manifest_sha256 != verified_release.manifest_sha256
        ):
            with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
                connection.execute("UPDATE outreach_exports SET status='failed',error=? WHERE id=?", ("research release changed during export", export_id))
            raise IntegrityError("the research release changed during export; retry after syncing")

        exports_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(exports_dir, PRIVATE_DIRECTORY_MODE)
        if not destination:
            output_path = exports_dir / f"{export_id}.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        public_fields = ["account_id", "outreach_purpose", "clinic_name", "buyer_name", "buyer_role", "channel", "destination", "subject", "message", "why_chosen", "problem_evidence"]
        if branch_id:
            public_fields.extend(["branch_id", "treatment_slug"])
        temporary_name: str | None = None
        output_created = False
        try:
            # Hold a short write lock across the final preflight and atomic
            # rename.  A new suppression/contact edit therefore cannot race a
            # CSV that was already prepared from stale readiness.
            with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
                for row in ready_rows:
                    current = readiness_for_purpose(
                        evaluate_readiness(
                            connection,
                            row["account_id"],
                            branch_id=branch_id,
                            treatment_slug=treatment_slug,
                        ),
                        purpose,
                    )
                    unsafe = [code for code in current["failed_gate_codes"] if code != "dedupe"]
                    selected = current["selection"]
                    if unsafe or selected["contact_point_id"] != row["contact_point_id"] or selected["message_draft_id"] != row["message_draft_id"]:
                        raise ValueError(
                            "outreach readiness changed during export: "
                            + ", ".join(unsafe or ["selected_route_changed"])
                        )
                final_release = verify_current_release(self.root)
                if (
                    final_release.release_id != verified_release.release_id
                    or final_release.manifest_sha256 != verified_release.manifest_sha256
                ):
                    raise IntegrityError("the research release changed during export")
                descriptor, temporary_name = tempfile.mkstemp(dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", text=True)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=public_fields, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(
                        [{field: _csv_safe(row.get(field)) for field in public_fields} for row in ready_rows]
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, output_path)
                temporary_name = None
                output_created = True
                os.chmod(output_path, PRIVATE_FILE_MODE)
                completed = utc_now()
                connection.execute("UPDATE outreach_exports SET status='complete',path=?,completed_at=? WHERE id=?", (str(output_path), completed, export_id))
                _audit(connection, actor=actor, entity_type="outreach_export", entity_id=export_id, action="completed", after={"path": str(output_path), "branch_id": branch_id, "treatment_slug": treatment_slug, "row_count": len(ready_rows)})
        except Exception as exc:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()
            if output_created and output_path.exists():
                output_path.unlink()
            with closing(connect(self.db_path)) as connection, transaction(connection, immediate=True):
                connection.execute("UPDATE outreach_exports SET status='failed',error=? WHERE id=?", (str(exc), export_id))
                _audit(connection, actor=actor, entity_type="outreach_export", entity_id=export_id, action="failed", after={"error": str(exc)})
            raise
        result = {
            "dry_run": False,
            "export_id": export_id,
            "worklist_name": worklist_name,
            "outreach_purpose": purpose,
            "count": len(ready_rows),
            "blocked_count": len(blocked),
            "blocked": blocked,
            "path": str(output_path),
        }
        if branch_id:
            result["branch_id"] = branch_id
            result["treatment_slug"] = treatment_slug
        return result

    def status(self) -> dict[str, Any]:
        with closing(connect(self.db_path)) as connection:
            release = self._latest_release(connection)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            account_ids = [row["id"] for row in connection.execute("SELECT id FROM sales_accounts")]
            readiness = [evaluate_readiness(connection, value) for value in account_ids]
            counts = {}
            for table in ("sales_accounts", "contact_points", "import_conflicts", "worklists", "worklist_items", "audit_events"):
                counts[table] = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            return {
                "db_path": str(self.db_path),
                "integrity_check": integrity,
                "release": {
                    "id": release["release_id"] if release else None,
                    "manifest_sha256": release["manifest_sha256"] if release else None,
                    "imported_at": release["imported_at"] if release else None,
                },
                "counts": {
                    **counts,
                    "ready": sum(1 for item in readiness if item["ready"]),
                    "routing_ready": sum(1 for item in readiness if item["routing_ready"]),
                    "evidence_ready": sum(1 for item in readiness if item["evidence_delivery_ready"]),
                },
            }

    def perform_action(self, action: str, payload: dict[str, Any], *, actor: str = "founder") -> dict[str, Any]:
        actions: dict[str, Callable[..., dict[str, Any]]] = {
            "confirm_branch": self.confirm_branch,
            "merge_branches": self.merge_branches,
            "save_buyer": self.save_buyer,
            "save_contact": self.save_contact,
            "save_channel_review": self.save_channel_review,
            "save_visibility_check": self.save_visibility_check,
            "save_rationale": self.save_rationale,
            "save_draft": self.save_draft,
            "approve_draft": self.approve_draft,
            "set_priority_override": self.set_priority_override,
            "set_branch_treatment": self.set_branch_treatment,
            "set_stage": self.set_stage,
            "log_interaction": self.log_interaction,
            "suppress": self.suppress,
            "unsuppress": self.unsuppress,
            "resolve_import_conflict": self.resolve_import_conflict,
            "add_to_worklist": self.add_to_worklist,
            "remove_from_worklist": self.remove_from_worklist,
        }
        if action == "export_ready":
            result = self.export_ready(
                worklist_name=str(payload.get("worklist_name") or "founder-25"),
                outreach_purpose=str(payload.get("outreach_purpose") or "evidence_delivery"),
                branch_id=str(payload.get("branch_id") or "").strip() or None,
                treatment_slug=str(payload.get("treatment_slug") or "").strip() or None,
                dry_run=_bool(payload.get("dry_run", True)),
                actor=actor,
                destination=payload.get("destination"),
            )
        else:
            handler = actions.get(action)
            if not handler:
                raise ValueError(f"unknown outreach action: {action}")
            result = handler(payload, actor=actor)
        response: dict[str, Any] = {"ok": True, "action": action, "result": result}
        account_id = payload.get("account_id")
        if account_id:
            response["account"] = self.get_account(str(account_id))
        return response
