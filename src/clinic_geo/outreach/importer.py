"""Verified, idempotent import of immutable prospect-research releases."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable

from .db import IntegrityError, transaction
from .models import json_dumps, new_id, normalize_destination, utc_now


REQUIRED_OUTPUTS = ("accounts.jsonl", "contacts.jsonl")


def _branch_backfill_preview_required() -> dict[str, Any]:
    """Compatibility-shaped zero-write status for release synchronization."""

    return {
        "branch_backfill_status": "preview_required",
        "branches_new": 0,
        "branches_reused": 0,
        "snapshots_written": 0,
        "observations_written": 0,
        "branch_treatments_written": 0,
        "needs_confirmation": 0,
        "unmapped_treatments": 0,
        "locationless_member_uids": [],
        "locationless_member_count": 0,
        "locationless_rate": None,
        "prior_locationless_rate": None,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise IntegrityError(f"{path.name}:{line_number} is not a JSON object")
            rows.append(value)
    return rows


def _resolve_pointer_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


@dataclass(frozen=True)
class VerifiedRelease:
    release_id: str
    release_dir: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, Any]
    accounts: list[dict[str, Any]]
    contacts: list[dict[str, Any]]


def _portable_release_path(root: Path, release_dir: Path) -> str:
    """Return the canonical workspace-relative locator for a verified release."""
    try:
        relative = release_dir.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise IntegrityError("research release directory is outside the workspace root") from exc
    if len(relative.parts) != 4 or relative.parts[:3] != ("data", "database", "releases"):
        raise IntegrityError("research release directory is not in data/database/releases")
    return relative.as_posix()


def verify_current_release(root: Path | str) -> VerifiedRelease:
    root = Path(root).resolve()
    pointer_path = root / "data/database/current-release.json"
    if not pointer_path.is_file():
        raise IntegrityError(f"current research release pointer not found: {pointer_path}")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    release_id = str(pointer.get("release_id") or "").strip()
    manifest_value = pointer.get("manifest")
    if not release_id or not manifest_value:
        raise IntegrityError("current-release.json must include release_id and manifest")
    manifest_path = _resolve_pointer_path(root, str(manifest_value))
    release_dir = manifest_path.parent.resolve()
    expected_dir = (root / "data/database/releases" / release_id).resolve()
    if release_dir != expected_dir:
        raise IntegrityError("release manifest path does not match the declared release_id")
    if not manifest_path.is_file():
        raise IntegrityError(f"research release manifest not found: {manifest_path}")
    manifest_sha = _sha256(manifest_path)
    expected_manifest_sha = str(pointer.get("manifest_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha):
        raise IntegrityError("current-release.json must include a valid manifest_sha256")
    if manifest_sha != expected_manifest_sha:
        raise IntegrityError("research release manifest hash does not match current-release.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("release_id") != release_id:
        raise IntegrityError("research release manifest has a different release_id")
    outputs = manifest.get("output_integrity")
    if not isinstance(outputs, dict):
        raise IntegrityError("research release manifest has no output_integrity map")
    for name in REQUIRED_OUTPUTS:
        if name not in outputs:
            raise IntegrityError(f"required research output is missing from manifest: {name}")
    for name, metadata in outputs.items():
        relative = Path(str(name))
        if relative.is_absolute() or ".." in relative.parts:
            raise IntegrityError(f"unsafe research output path in manifest: {name}")
        path = (release_dir / relative).resolve()
        try:
            path.relative_to(release_dir)
        except ValueError as exc:
            raise IntegrityError(f"research output escapes the release directory: {name}") from exc
        if not isinstance(metadata, dict) or not path.is_file():
            raise IntegrityError(f"research output is missing: {name}")
        if path.stat().st_size != int(metadata.get("bytes", -1)):
            raise IntegrityError(f"research output byte count mismatch: {name}")
        if _sha256(path) != metadata.get("sha256"):
            raise IntegrityError(f"research output hash mismatch: {name}")
    return VerifiedRelease(
        release_id=release_id,
        release_dir=release_dir,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        manifest=manifest,
        accounts=_read_jsonl(release_dir / "accounts.jsonl"),
        contacts=_read_jsonl(release_dir / "contacts.jsonl"),
    )


def _validate_release_identity(accounts: list[dict[str, Any]]) -> None:
    research_ids: dict[str, int] = {}
    member_uids: dict[str, int] = {}
    for index, account in enumerate(accounts):
        research_id = _account_key(account, index)
        if research_id in research_ids:
            raise IntegrityError(
                f"duplicate research account ID {research_id!r} at rows "
                f"{research_ids[research_id] + 1} and {index + 1}"
            )
        research_ids[research_id] = index
        local_members = _member_uids(account)
        if len(local_members) != len(set(local_members)):
            raise IntegrityError(f"research account {research_id!r} repeats a member UID")
        for uid in local_members:
            if uid in member_uids:
                raise IntegrityError(
                    f"member UID {uid!r} occurs in more than one research account "
                    f"(rows {member_uids[uid] + 1} and {index + 1})"
                )
            member_uids[uid] = index


def _member_uids(account: dict[str, Any]) -> list[str]:
    values = account.get("member_uids") or []
    if not isinstance(values, list):
        values = []
    result = [str(value).strip() for value in values if str(value).strip()]
    primary = str(account.get("primary_uid") or "").strip()
    if primary and primary not in result:
        result.append(primary)
    return result


def _account_key(account: dict[str, Any], index: int) -> str:
    value = str(account.get("account_id") or account.get("stable_account_key") or "").strip()
    return value or f"row:{index}"


def _prior_member_map(connection: sqlite3.Connection) -> dict[str, str]:
    mapping: dict[str, str] = {}
    rows = connection.execute(
        """
        SELECT rm.member_uid, rm.sales_account_id
        FROM research_memberships rm
        JOIN research_releases rr ON rr.release_id = rm.release_id
        ORDER BY rr.imported_at DESC, rr.release_id DESC
        """
    )
    for row in rows:
        mapping.setdefault(row["member_uid"], row["sales_account_id"])
    return mapping


def _plan_mappings(
    connection: sqlite3.Connection,
    accounts: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]], int]:
    prior = _prior_member_map(connection)
    candidate_counts: list[Counter[str]] = []
    old_to_current: dict[str, list[int]] = defaultdict(list)
    for index, account in enumerate(accounts):
        counts = Counter(prior[uid] for uid in _member_uids(account) if uid in prior)
        candidate_counts.append(counts)
        for sales_id in counts:
            old_to_current[sales_id].append(index)

    # A split keeps the historical sales account on the current account with the
    # strongest UID overlap.  Other branches receive new stable IDs and a review
    # conflict; history is never silently copied or destroyed.
    keep_for_old: dict[str, int] = {}
    for sales_id, indexes in old_to_current.items():
        keep_for_old[sales_id] = min(
            indexes,
            key=lambda idx: (
                -candidate_counts[idx][sales_id],
                accounts[idx].get("prospect_rank") or 10**9,
                _account_key(accounts[idx], idx),
            ),
        )

    mappings: list[str] = []
    conflicts: list[dict[str, Any]] = []
    used: set[str] = set()
    matched = 0
    for index, (account, candidates) in enumerate(zip(accounts, candidate_counts)):
        eligible = [sales_id for sales_id in candidates if keep_for_old[sales_id] == index]
        eligible.sort(key=lambda value: (-candidates[value], value))
        # A many-to-one match is ambiguous.  It receives a neutral new account so
        # neither historical sales timeline is silently selected as canonical.
        selected = (
            eligible[0]
            if len(candidates) == 1 and eligible and eligible[0] not in used
            else new_id("acct")
        )
        if selected in candidates:
            matched += 1
        used.add(selected)
        mappings.append(selected)
        if len(candidates) > 1:
            conflicts.append(
                {
                    "account_index": index,
                    "kind": "merge",
                    "candidates": sorted(candidates),
                    "selected": selected,
                    "resolution": "created neutral sales account pending manual merge resolution; all prior histories preserved",
                }
            )
        split_candidates = [
            sales_id for sales_id in candidates if len(old_to_current.get(sales_id, [])) > 1
        ]
        if split_candidates:
            retained = any(keep_for_old.get(sales_id) == index and selected == sales_id for sales_id in split_candidates)
            conflicts.append(
                {
                    "account_index": index,
                    "kind": "split",
                    "candidates": sorted(split_candidates),
                    "selected": selected,
                    "resolution": (
                        "retained historical sales account pending manual split resolution; all branch histories preserved"
                        if retained
                        else "created separate sales account pending manual split resolution; source history preserved"
                    ),
                }
            )
    return mappings, conflicts, matched


def _extract_emails(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        values = [str(item) for item in value]
    else:
        values = [str(value)]
    result: list[str] = []
    for item in values:
        matches = re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", item, flags=re.I)
        result.extend(matches)
    return list(dict.fromkeys(result))


def _extract_phones(value: Any) -> list[tuple[str, str]]:
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_value in values:
        # Research sources sometimes join separate routes with semicolons,
        # newlines, or pipes.  Treat each segment as its own destination; a
        # joined value would otherwise normalize into one invalid long number.
        for segment in re.split(r"[;|\r\n]+", str(raw_value)):
            raw = segment.strip()
            if not raw:
                continue
            channel = "whatsapp" if "whatsapp" in raw.lower() else "phone"
            normalized = normalize_destination(channel, raw)
            digits = normalized[1:] if normalized.startswith("+") else normalized
            if not digits.isdigit() or not 10 <= len(digits) <= 15:
                continue
            key = (channel, normalized)
            if key in seen:
                continue
            seen.add(key)
            result.append((channel, raw))
    return result


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


def cleanup_compound_phone_routes(
    connection: sqlite3.Connection,
    *,
    actor: str = "system",
) -> dict[str, int]:
    """Split and archive malformed imported compound phone routes without loss.

    A compound is archived only after every valid component exists separately
    on the same account.  Original value and provenance remain available for
    audit/history, and repeated runs are idempotent.
    """
    candidates = list(
        connection.execute(
            """
            SELECT * FROM contact_points
            WHERE status='active' AND verification_status='imported_unverified'
              AND channel IN ('phone','sms','whatsapp')
              AND (instr(value, ';') > 0 OR instr(value, '|') > 0
                   OR instr(value, char(10)) > 0 OR instr(value, char(13)) > 0)
            """
        )
    )
    inserted = 0
    archived = 0
    now = utc_now()
    for parent in candidates:
        components = _extract_phones(parent["value"])
        if len(components) < 2:
            continue
        component_ids: list[str] = []
        complete = True
        for channel, value in components:
            normalized = normalize_destination(channel, value)
            existing = connection.execute(
                """
                SELECT id FROM contact_points
                WHERE account_id=? AND channel=? AND normalized_value=?
                  AND status='active'
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (parent["account_id"], channel, normalized),
            ).fetchone()
            if existing:
                component_ids.append(existing["id"])
                continue
            contact_id = new_id("contact")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO contact_points
                  (id,account_id,channel,value,normalized_value,route_scope,
                   organization_route_exception,verification_status,source_url,
                   status,notes,created_at,updated_at)
                VALUES (?,?,?,?,?,'organization',0,'imported_unverified',?,
                        'active',?,?,?)
                """,
                (
                    contact_id,
                    parent["account_id"],
                    channel,
                    value,
                    normalized,
                    parent["source_url"],
                    "Split from malformed compound imported route; manual verification required.",
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                inserted += 1
                component_ids.append(contact_id)
                _audit(
                    connection,
                    actor=actor,
                    entity_type="contact_point",
                    entity_id=contact_id,
                    action="compound_component_created",
                    after={"parent_contact_id": parent["id"], "channel": channel},
                )
            else:
                resolved = connection.execute(
                    "SELECT id FROM contact_points WHERE account_id=? AND channel=? AND normalized_value=? AND status='active'",
                    (parent["account_id"], channel, normalized),
                ).fetchone()
                if resolved:
                    component_ids.append(resolved["id"])
                else:
                    complete = False
                    break
        if not complete or len(component_ids) != len(components):
            continue
        note = (parent["notes"] or "").rstrip()
        reason = "Archived after safe split into individual phone routes."
        updated_notes = f"{note} {reason}".strip()
        connection.execute(
            """
            UPDATE contact_points
            SET status='archived',notes=?,version=version+1,updated_at=?
            WHERE id=? AND status='active'
            """,
            (updated_notes, now, parent["id"]),
        )
        archived += 1
        _audit(
            connection,
            actor=actor,
            entity_type="contact_point",
            entity_id=parent["id"],
            action="compound_route_archived",
            before={"status": "active", "value": parent["value"]},
            after={"status": "archived", "component_ids": component_ids},
        )
    return {"candidates": len(candidates), "inserted": inserted, "archived": archived}


class ReleaseImporter:
    def __init__(self, root: Path | str, connection: sqlite3.Connection):
        self.root = Path(root).resolve()
        self.connection = connection

    def sync_current_release(self, *, dry_run: bool = False, actor: str = "system") -> dict[str, Any]:
        release = verify_current_release(self.root)
        _validate_release_identity(release.accounts)
        portable_release_path = _portable_release_path(self.root, release.release_dir)
        existing = self.connection.execute(
            "SELECT * FROM research_releases WHERE release_id = ?", (release.release_id,)
        ).fetchone()
        if existing:
            if existing["manifest_sha256"] != release.manifest_sha256:
                raise IntegrityError("an imported release ID now has different immutable bytes")
            existing_mappings = {
                row["research_account_id"]: row["sales_account_id"]
                for row in self.connection.execute(
                    """
                    SELECT research_account_id,sales_account_id
                    FROM research_account_snapshots
                    WHERE release_id=?
                    """,
                    (release.release_id,),
                )
            }
            expected_research_ids = {
                _account_key(account, index)
                for index, account in enumerate(release.accounts)
            }
            if set(existing_mappings) != expected_research_ids:
                raise IntegrityError(
                    "imported release account snapshots no longer match verified release"
                )
            branch_counts = _branch_backfill_preview_required()
            if not dry_run:
                now = utc_now()
                with transaction(self.connection, immediate=True):
                    if existing["release_path"] != portable_release_path:
                        self.connection.execute(
                            "UPDATE research_releases SET release_path=? WHERE release_id=?",
                            (portable_release_path, release.release_id),
                        )
                        _audit(
                            self.connection,
                            actor=actor,
                            entity_type="research_release",
                            entity_id=release.release_id,
                            action="release_path_canonicalized",
                            before={"release_path": existing["release_path"]},
                            after={"release_path": portable_release_path},
                        )
                    previous = self.connection.execute(
                        "SELECT value FROM workspace_state WHERE key='active_release_id'"
                    ).fetchone()
                    self.connection.execute(
                        """
                        INSERT INTO workspace_state (key,value,updated_at)
                        VALUES ('active_release_id',?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                        """,
                        (release.release_id, now),
                    )
                    if not previous or previous["value"] != release.release_id:
                        _audit(
                            self.connection,
                            actor=actor,
                            entity_type="workspace_state",
                            entity_id="active_release_id",
                            action="research_release_activated",
                            before={"release_id": previous["value"] if previous else None},
                            after={"release_id": release.release_id},
                        )
            return {
                "release_id": release.release_id,
                "status": "already_imported",
                "dry_run": dry_run,
                "accounts": existing["account_count"],
                "matched_accounts": existing["account_count"],
                "new_accounts": 0,
                "contact_routes": existing["imported_contact_route_count"],
                "conflicts": 0,
                **branch_counts,
            }

        mappings, conflicts, matched = _plan_mappings(self.connection, release.accounts)
        plan = {
            "release_id": release.release_id,
            "status": "planned" if dry_run else "imported",
            "dry_run": dry_run,
            "accounts": len(release.accounts),
            "matched_accounts": matched,
            "new_accounts": len(release.accounts) - matched,
            "contact_routes": 0,
            "conflicts": len(conflicts),
        }
        if dry_run:
            plan.update(_branch_backfill_preview_required())
            return plan

        now = utc_now()
        correlation_id = new_id("import")
        account_by_uid: dict[str, str] = {}
        account_source: dict[str, dict[str, Any]] = {}
        with transaction(self.connection, immediate=True):
            previous_active = self.connection.execute(
                "SELECT value FROM workspace_state WHERE key='active_release_id'"
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO research_releases
                  (release_id, manifest_sha256, release_path, created_at, imported_at,
                   account_count, imported_contact_route_count, manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    release.release_id,
                    release.manifest_sha256,
                    portable_release_path,
                    release.manifest.get("created_at"),
                    now,
                    len(release.accounts),
                    json_dumps(release.manifest),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO workspace_state (key,value,updated_at)
                VALUES ('active_release_id',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (release.release_id, now),
            )
            _audit(
                self.connection,
                actor=actor,
                entity_type="workspace_state",
                entity_id="active_release_id",
                action="research_release_activated",
                before={"release_id": previous_active["value"] if previous_active else None},
                after={"release_id": release.release_id},
                correlation_id=correlation_id,
            )
            # Reading existing IDs avoids SQLite's host-parameter limit on large
            # future releases (and is tiny compared with the snapshot payload).
            existing_ids = {row["id"] for row in self.connection.execute("SELECT id FROM sales_accounts")}
            for index, (account, sales_id) in enumerate(zip(release.accounts, mappings)):
                if sales_id not in existing_ids:
                    self.connection.execute(
                        """
                        INSERT INTO sales_accounts (id, created_at, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (sales_id, now, now),
                    )
                    existing_ids.add(sales_id)
                    _audit(
                        self.connection,
                        actor=actor,
                        entity_type="sales_account",
                        entity_id=sales_id,
                        action="created_from_research",
                        after={"release_id": release.release_id},
                        correlation_id=correlation_id,
                    )
                research_id = _account_key(account, index)
                members = _member_uids(account)
                self.connection.execute(
                    """
                    INSERT INTO research_account_snapshots
                      (release_id, research_account_id, sales_account_id, clinic_name,
                       primary_uid, stable_account_key, member_uids_json, prospect_rank,
                       prospect_score, prospect_tier, prospect_status, prospect_analysis,
                       area, category, owned_website, outreach_route, risk_flags_json,
                       reason_codes_json, raw_json, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        release.release_id,
                        research_id,
                        sales_id,
                        str(account.get("clinic_name") or "Unnamed clinic"),
                        account.get("primary_uid"),
                        account.get("stable_account_key"),
                        json_dumps(members),
                        account.get("prospect_rank"),
                        account.get("prospect_score_0_100"),
                        account.get("prospect_tier"),
                        account.get("prospect_status"),
                        account.get("prospect_analysis"),
                        account.get("area"),
                        account.get("category"),
                        account.get("owned_website"),
                        account.get("outreach_route"),
                        json_dumps(account.get("risk_flags") or []),
                        json_dumps(account.get("prospect_reason_codes") or []),
                        json_dumps(account),
                        now,
                    ),
                )
                account_source[sales_id] = account
                for uid in members:
                    self.connection.execute(
                        """
                        INSERT INTO research_memberships
                          (release_id, member_uid, sales_account_id, research_account_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (release.release_id, uid, sales_id, research_id),
                    )
                    account_by_uid[uid] = sales_id

            for conflict in conflicts:
                account = release.accounts[conflict["account_index"]]
                research_id = _account_key(account, conflict["account_index"])
                conflict_id = new_id("conflict")
                self.connection.execute(
                    """
                    INSERT INTO import_conflicts
                      (id, sales_account_id, release_id, research_account_id, kind,
                       candidate_sales_account_ids_json, resolution, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conflict_id,
                        mappings[conflict["account_index"]],
                        release.release_id,
                        research_id,
                        conflict["kind"],
                        json_dumps(conflict["candidates"]),
                        conflict["resolution"],
                        now,
                    ),
                )
                _audit(
                    self.connection,
                    actor=actor,
                    entity_type="import_conflict",
                    entity_id=conflict_id,
                    action="created",
                    after=conflict,
                    correlation_id=correlation_id,
                )

            route_count = self._import_contact_routes(
                release=release,
                account_by_uid=account_by_uid,
                account_source=account_source,
                now=now,
                actor=actor,
                correlation_id=correlation_id,
            )
            self.connection.execute(
                """
                UPDATE research_releases
                SET imported_contact_route_count = ?
                WHERE release_id = ?
                """,
                (route_count, release.release_id),
            )
            _audit(
                self.connection,
                actor=actor,
                entity_type="research_release",
                entity_id=release.release_id,
                action="imported",
                after={"accounts": len(release.accounts), "contact_routes": route_count},
                correlation_id=correlation_id,
            )
        plan["contact_routes"] = route_count
        plan.update(_branch_backfill_preview_required())
        return plan

    def _import_contact_routes(
        self,
        *,
        release: VerifiedRelease,
        account_by_uid: dict[str, str],
        account_source: dict[str, dict[str, Any]],
        now: str,
        actor: str,
        correlation_id: str,
    ) -> int:
        candidates: list[tuple[str, str, str, str | None]] = []
        for row in release.contacts:
            sales_id = account_by_uid.get(str(row.get("uid") or "").strip())
            if not sales_id:
                continue
            source_url = row.get("contact_source_url") or row.get("official_website")
            for value in _extract_emails(row.get("contact_email")):
                candidates.append((sales_id, "email", value, source_url))
            for channel, value in _extract_phones(row.get("contact_phone")):
                candidates.append((sales_id, channel, value, source_url))
        # Account snapshots sometimes contain an additional route consolidated
        # from multiple members.  Import it too, while retaining unverified status.
        for sales_id, account in account_source.items():
            source_url = account.get("owned_website")
            for value in _extract_emails(account.get("contact_emails")):
                candidates.append((sales_id, "email", value, source_url))
            for channel, value in _extract_phones(account.get("contact_phones")):
                candidates.append((sales_id, channel, value, source_url))

        inserted = 0
        seen: set[tuple[str, str, str]] = set()
        for sales_id, channel, value, source_url in candidates:
            normalized = normalize_destination(channel, value)
            key = (sales_id, channel, normalized)
            if not normalized or key in seen:
                continue
            seen.add(key)
            contact_id = new_id("contact")
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO contact_points
                  (id, account_id, channel, value, normalized_value, route_scope,
                   organization_route_exception, verification_status, source_url,
                   status, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'organization', 0, 'imported_unverified', ?,
                        'active', 'Imported from immutable research; manual verification required.', ?, ?)
                """,
                (contact_id, sales_id, channel, value, normalized, source_url, now, now),
            )
            if cursor.rowcount:
                inserted += 1
                _audit(
                    self.connection,
                    actor=actor,
                    entity_type="contact_point",
                    entity_id=contact_id,
                    action="imported_unverified",
                    after={"account_id": sales_id, "channel": channel, "source_release": release.release_id},
                    correlation_id=correlation_id,
                )
        return inserted
