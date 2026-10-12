"""Deterministic, reviewable branch-backfill manifests.

The legacy branch derivation routine writes while it plans.  This module is the
fail-closed boundary used by the administrative CLI: preview produces a
portable content-addressed manifest, and apply first regenerates that exact
manifest under an immediate database transaction.  A changed byte, count, or
input hash therefore requires a new review.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Mapping, Sequence

from clinic_geo.integrity import canonical_json_bytes, sha256_bytes
from clinic_geo.treatment_library.catalog import (
    load_catalogue,
    load_legacy_mappings,
    verify_source_catalogue,
)

from . import branching
from .db import IntegrityError
from .models import json_dumps, json_loads, utc_now


MANIFEST_SCHEMA_VERSION = 2
MANIFEST_CONTRACT_VERSION = "branch-resolution-manifest.v2"
APPROVE_PROPOSED_BRANCH = "approve_proposed_branch"
MAP_EXISTING_BRANCH = "map_existing_branch"
SPLIT_EXACT_ASSIGNMENTS = "split_exact_assignments"
MERGE_EXISTING_BRANCHES = "merge_existing_branches"
DEFER_QUARANTINE = "defer_quarantine"
RESOLUTION_ACTIONS = frozenset(
    {
        APPROVE_PROPOSED_BRANCH,
        MAP_EXISTING_BRANCH,
        SPLIT_EXACT_ASSIGNMENTS,
        MERGE_EXISTING_BRANCHES,
        DEFER_QUARANTINE,
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PreparedManifest:
    """Canonical preview payload and its exact checked bytes."""

    payload: dict[str, Any]
    content: bytes
    sha256: str


def _manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    # The single trailing newline is part of the on-disk manifest contract.
    return canonical_json_bytes(payload) + b"\n"


def _sha_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise IntegrityError(f"branch backfill input not found: {path}")
    content = path.read_bytes()
    return {"bytes": len(content), "sha256": sha256_bytes(content)}


def _optional_sha_file(path: Path) -> dict[str, Any] | None:
    return _sha_file(path) if path.is_file() else None


def _state_sha(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> str:
    rows = [dict(row) for row in connection.execute(query, parameters)]
    return sha256_bytes(canonical_json_bytes(rows))


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _treatment_authority_state_sha(connection: sqlite3.Connection) -> str:
    if not _table_exists(connection, "treatment_catalog_releases"):
        return sha256_bytes(canonical_json_bytes({"installed": False}))
    tables = {
        "releases": [
            dict(row)
            for row in connection.execute(
                """
                SELECT id,lifecycle_state,review_state,payload_sha256,
                       legacy_mapping_payload_sha256
                FROM treatment_catalog_releases ORDER BY id
                """
            )
        ],
        "mappings": [
            dict(row)
            for row in connection.execute(
                """
                SELECT id,catalogue_release_id,legacy_slug,review_state
                FROM legacy_treatment_mappings ORDER BY catalogue_release_id,legacy_slug,id
                """
            )
        ],
        "concept_links": [
            dict(row)
            for row in connection.execute(
                """
                SELECT legacy_mapping_id,concept_version_id,authority_kind
                FROM legacy_treatment_mapping_concept_links
                ORDER BY legacy_mapping_id,concept_version_id,authority_kind
                """
            )
        ],
        "concept_versions": [
            dict(row)
            for row in connection.execute(
                """
                SELECT id,catalogue_release_id,concept_id,lifecycle_state,review_state
                FROM treatment_concept_versions
                ORDER BY catalogue_release_id,concept_id,id
                """
            )
        ],
    }
    return sha256_bytes(canonical_json_bytes({"installed": True, "tables": tables}))


def _deterministic_id(prefix: str, *parts: object) -> str:
    identity = canonical_json_bytes([str(value) for value in parts])
    return f"{prefix}_{sha256_bytes(identity)[:32]}"


def _resolution_source_hashes(inputs: Mapping[str, Any]) -> dict[str, str | None]:
    """Flatten every checked input digest into a portable resolution key."""

    result: dict[str, str | None] = {}
    for name in sorted(inputs):
        value = inputs[name]
        if isinstance(value, Mapping):
            digest = value.get("sha256")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise IntegrityError(f"branch backfill input {name} has no valid SHA-256")
            result[f"{name}_sha256"] = digest
        elif value is None:
            result[f"{name}_sha256"] = None
        elif name.endswith("_sha256"):
            digest = str(value)
            if not _SHA256_RE.fullmatch(digest):
                raise IntegrityError(f"branch backfill input {name} has no valid SHA-256")
            result[name] = digest
    return result


def _resolution_key(
    *,
    release_id: str,
    sales_account_id: str,
    branch_key: str,
    member_uids: Sequence[str],
    source_hashes: Mapping[str, str | None],
) -> tuple[dict[str, Any], str]:
    members = [str(value) for value in member_uids]
    if not members or members != sorted(set(members)):
        raise IntegrityError("branch resolution member_uids must be non-empty, sorted and unique")
    identity = {
        "research_release_id": release_id,
        "sales_account_id": sales_account_id,
        "branch_key": branch_key,
        "member_uids": members,
        "source_hashes": dict(source_hashes),
    }
    proposed_group_id = (
        f"branch_group_{sha256_bytes(canonical_json_bytes(identity))[:32]}"
    )
    key = {**identity, "proposed_group_id": proposed_group_id}
    return key, sha256_bytes(canonical_json_bytes(key))


def _default_resolution(
    *,
    branch_identity: str,
    branch_id: str,
    review_reasons: Sequence[str],
) -> dict[str, Any]:
    if review_reasons:
        return {"action": DEFER_QUARANTINE}
    if branch_identity == "existing":
        return {
            "action": MAP_EXISTING_BRANCH,
            "target_branch_id": branch_id,
        }
    return {"action": APPROVE_PROPOSED_BRANCH}


def _split_branch_id(
    release_id: str,
    sales_account_id: str,
    proposed_group_id: str,
    member_uids: Sequence[str],
) -> str:
    return _deterministic_id(
        "branch",
        MANIFEST_CONTRACT_VERSION,
        release_id,
        sales_account_id,
        proposed_group_id,
        *member_uids,
    )


def _split_branch_key(branch_key: str, member_uids: Sequence[str]) -> str:
    suffix = sha256_bytes(canonical_json_bytes(list(member_uids)))[:16]
    return f"{branch_key}#split:{suffix}"


def _confirmation(
    connection: sqlite3.Connection,
    branch_id: str | None,
) -> dict[str, Any]:
    if not branch_id:
        return {
            "status": "not_recorded",
            "display_name": None,
            "merged_into": None,
            "confirmed_by": None,
            "confirmed_at": None,
            "notes": None,
            "version": None,
            "updated_at": None,
        }
    row = connection.execute(
        """
        SELECT status,display_name,merged_into,confirmed_by,confirmed_at,notes,
               version,updated_at
        FROM branch_confirmations
        WHERE branch_id=?
        """,
        (branch_id,),
    ).fetchone()
    if not row:
        return {
            "status": "not_recorded",
            "display_name": None,
            "merged_into": None,
            "confirmed_by": None,
            "confirmed_at": None,
            "notes": None,
            "version": None,
            "updated_at": None,
        }
    return dict(row)


def _group_review_reasons(
    group: branching._BranchGroup,
    confirmation: Mapping[str, Any],
) -> list[str]:
    reasons: set[str] = set()
    if group.is_ambiguous or group.branch_key.startswith("unresolved:"):
        reasons.add("unresolved_location")
    if group.derivation_status == "needs_confirmation":
        reasons.add("needs_confirmation")
    if group.identity_ambiguous:
        reasons.add("identity_ambiguous")
    if not group.area or not group.area_name:
        reasons.add("missing_area")
    confirmation_status = str(confirmation.get("status") or "")
    if confirmation_status in {"unconfirmed", "rejected", "merged"}:
        reasons.add(f"confirmation_{confirmation_status}")
    return sorted(reasons)


def _existing_observation(
    connection: sqlite3.Connection,
    release_id: str,
    account_id: str,
    treatment_slug: str,
) -> bool:
    return bool(
        connection.execute(
            """
            SELECT 1 FROM account_treatment_observations
            WHERE release_id=? AND sales_account_id=? AND treatment_slug=?
            """,
            (release_id, account_id, treatment_slug),
        ).fetchone()
    )


def _input_integrity(
    connection: sqlite3.Connection,
    root: Path,
    release_id: str,
    release_dir: Path,
) -> dict[str, Any]:
    taxonomy_path = root / "data/database/1_finding_cliniques/procedures.json"
    catalogue_path = root / "data/treatments/catalog-v1.json"
    mapping_path = root / "data/treatments/legacy-treatment-mapping-v1.json"
    return {
        "current_release_pointer": _sha_file(
            root / "data/database/current-release.json"
        ),
        "release_manifest": _sha_file(release_dir / "manifest.json"),
        "clinic_evidence": _sha_file(release_dir / "clinic-evidence.jsonl"),
        "treatment_taxonomy": _sha_file(taxonomy_path),
        "treatment_catalogue_release": _optional_sha_file(catalogue_path),
        "legacy_treatment_mapping_release": _optional_sha_file(mapping_path),
        "research_snapshot_state_sha256": _state_sha(
            connection,
            """
            SELECT research_account_id,sales_account_id,raw_json
            FROM research_account_snapshots
            WHERE release_id=?
            ORDER BY sales_account_id,research_account_id
            """,
            (release_id,),
        ),
        "branch_state_sha256": _state_sha(
            connection,
            """
            SELECT cb.id,cb.sales_account_id,cb.first_release_id,cb.created_at,
                   cbs.release_id,cbs.branch_key,cbs.member_uids_json,
                   cbs.derivation_status
            FROM clinic_branches cb
            LEFT JOIN clinic_branch_snapshots cbs ON cbs.branch_id=cb.id
            ORDER BY cb.sales_account_id,cb.id,cbs.release_id,cbs.branch_key
            """,
        ),
        "confirmation_state_sha256": _state_sha(
            connection,
            """
            SELECT branch_id,status,display_name,merged_into,confirmed_by,
                   confirmed_at,notes,version,updated_at
            FROM branch_confirmations
            ORDER BY branch_id
            """,
        ),
        "treatment_state_sha256": _state_sha(
            connection,
            """
            SELECT slug,canonical_name,category,aliases_json,active,
                   taxonomy_version,created_at,updated_at
            FROM treatments
            ORDER BY slug
            """,
        ),
        "reviewed_treatment_authority_state_sha256": (
            _treatment_authority_state_sha(connection)
        ),
        "treatment_observation_state_sha256": _state_sha(
            connection,
            """
            SELECT release_id,sales_account_id,treatment_slug,source_name,created_at
            FROM account_treatment_observations
            ORDER BY release_id,sales_account_id,treatment_slug
            """,
        ),
        "branch_offering_state_sha256": _state_sha(
            connection,
            """
            SELECT id,branch_id,treatment_slug,state,source,release_id,created_at,updated_at
            FROM branch_treatments
            ORDER BY branch_id,treatment_slug,source,id
            """,
        ),
    }


def _read_only_treatment_resolver(
    root: Path,
) -> dict[str, str]:
    """Resolve only exact source-taxonomy names without seeding rows."""

    return {
        str(row["name"]).strip().casefold(): str(row["id"])
        for row in branching._taxonomy_rows(root)
    }


def _persisted_active_treatment_slugs(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["slug"])
        for row in connection.execute("SELECT slug FROM treatments WHERE active=1")
    }


def _reviewed_legacy_treatment_authority(
    root: Path,
) -> tuple[dict[str, str] | None, dict[str, dict[str, Any]]]:
    """Return reviewed treatment mappings with canonical concept authority.

    Missing reviewed artifacts are a normal preview state for isolated test or
    pre-release workspaces.  A partially present or invalid release fails
    closed.  Patient-intent-only mappings deliberately do not authorize an
    account treatment observation.
    """

    catalogue_path = root / "data/treatments/catalog-v1.json"
    mapping_path = root / "data/treatments/legacy-treatment-mapping-v1.json"
    if not catalogue_path.exists() and not mapping_path.exists():
        return None, {}
    if not catalogue_path.is_file() or not mapping_path.is_file():
        raise IntegrityError("reviewed treatment catalogue and legacy mapping must coexist")
    try:
        catalogue = load_catalogue(catalogue_path)
        mappings = load_legacy_mappings(mapping_path, catalogue=catalogue)
        verify_source_catalogue(
            root / "data/database/1_finding_cliniques/procedures.json",
            catalogue=catalogue,
            mappings=mappings,
        )
    except (OSError, ValueError) as exc:
        raise IntegrityError("reviewed treatment catalogue release is not valid") from exc

    authority: dict[str, dict[str, Any]] = {}
    for mapping in mappings.mappings:
        if not mapping.is_deterministically_authoritative:
            continue
        if not mapping.target_concept_ids:
            # A broad patient intent is not treatment/branch-offering evidence.
            continue
        authority[mapping.legacy_slug] = {
            "catalogue_release_id": mappings.catalogue_release_id,
            "target_concept_ids": list(mapping.target_concept_ids),
        }
    return (
        {
            "catalogue_release_id": mappings.catalogue_release_id,
            "catalogue_payload_sha256": catalogue.release.payload_sha256,
            "legacy_mapping_payload_sha256": mappings.payload_sha256,
        },
        authority,
    )


def _database_reviewed_treatment_authority(
    connection: sqlite3.Connection,
    release_identity: Mapping[str, str] | None,
    file_authority: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Require the explicit temporary-DB 007 join to match checked files."""

    if release_identity is None or not _table_exists(
        connection, "treatment_catalog_releases"
    ):
        return set()
    rows = connection.execute(
        """
        SELECT mapping.legacy_slug,concept.concept_id
        FROM treatment_catalog_releases AS release
        JOIN legacy_treatment_mappings AS mapping
          ON mapping.catalogue_release_id=release.id
         AND mapping.review_state='approved'
        JOIN legacy_treatment_mapping_concept_links AS link
          ON link.legacy_mapping_id=mapping.id
         AND link.authority_kind='approved'
        JOIN treatment_concept_versions AS concept
          ON concept.id=link.concept_version_id
         AND concept.catalogue_release_id=mapping.catalogue_release_id
         AND concept.lifecycle_state='active'
         AND concept.review_state='approved'
        WHERE release.id=?
          AND release.lifecycle_state='active'
          AND release.review_state='approved'
          AND release.payload_sha256=?
          AND release.legacy_mapping_payload_sha256=?
        ORDER BY mapping.legacy_slug,concept.concept_id
        """,
        (
            release_identity["catalogue_release_id"],
            release_identity["catalogue_payload_sha256"],
            release_identity["legacy_mapping_payload_sha256"],
        ),
    )
    database_targets: dict[str, list[str]] = {}
    for row in rows:
        database_targets.setdefault(str(row["legacy_slug"]), []).append(
            str(row["concept_id"])
        )
    return {
        slug
        for slug, target in file_authority.items()
        if database_targets.get(slug) == sorted(target["target_concept_ids"])
    }


def prepare_manifest(
    connection: sqlite3.Connection,
    root: Path | str,
    release: sqlite3.Row | Mapping[str, Any],
) -> PreparedManifest:
    """Build a deterministic write plan without mutating the database."""

    root_path = Path(root).resolve()
    release_id = str(release["release_id"])
    release_dir = branching._safe_release_dir(
        root_path,
        str(release["release_path"]),
        release_id,
    )
    initial_inputs = _input_integrity(
        connection,
        root_path,
        release_id,
        release_dir,
    )
    resolution_source_hashes = _resolution_source_hashes(initial_inputs)
    plan = branching._make_plan(
        connection,
        root_path,
        release_id,
        release_dir,
        branching._account_inputs_from_database(connection, release_id),
        _read_only_treatment_resolver(root_path),
    )
    persisted_treatments = _persisted_active_treatment_slugs(connection)
    release_identity, reviewed_treatments = _reviewed_legacy_treatment_authority(
        root_path
    )
    database_reviewed_treatments = _database_reviewed_treatment_authority(
        connection,
        release_identity,
        reviewed_treatments,
    )
    catalogue_release_id = (
        release_identity["catalogue_release_id"] if release_identity else None
    )

    summary: dict[str, Any] = {
        "accounts_total": len(plan.accounts),
        "accounts_eligible": 0,
        "accounts_quarantined": 0,
        "branch_groups_total": 0,
        "branch_groups_review_required": 0,
        "branches_new": 0,
        "branches_reused": 0,
        "snapshots_written": 0,
        "observations_written": 0,
        "treatment_observations_review_required": 0,
        "branch_treatments_written": 0,
        "needs_confirmation": 0,
        "unmapped_treatments": 0,
        "locationless_member_uids": plan.locationless_member_uids,
        "locationless_member_count": len(plan.locationless_member_uids),
        "locationless_rate": plan.locationless_rate,
        "prior_locationless_rate": plan.prior_locationless_rate,
        "resolution_actions": {action: 0 for action in sorted(RESOLUTION_ACTIONS)},
    }
    manifest_accounts: list[dict[str, Any]] = []
    release_created_at = release["created_at"]
    if not isinstance(release_created_at, str) or not release_created_at.strip():
        raise IntegrityError("connected research release has no deterministic created_at")
    created_at = release_created_at

    for account in sorted(plan.accounts, key=lambda item: item.sales_account_id):
        groups: list[dict[str, Any]] = []
        account_reasons: set[str] = set()
        for group in sorted(account.groups, key=lambda item: item.branch_key):
            member_uids = sorted(str(value) for value in group.member_uids)
            resolution_key, resolution_key_sha256 = _resolution_key(
                release_id=release_id,
                sales_account_id=account.sales_account_id,
                branch_key=group.branch_key,
                member_uids=member_uids,
                source_hashes=resolution_source_hashes,
            )
            proposed_group_id = str(resolution_key["proposed_group_id"])
            branch_id = group.branch_id or _deterministic_id(
                "branch",
                MANIFEST_CONTRACT_VERSION,
                release_id,
                account.sales_account_id,
                proposed_group_id,
            )
            if group.branch_id is None:
                collision = connection.execute(
                    "SELECT sales_account_id FROM clinic_branches WHERE id=?",
                    (branch_id,),
                ).fetchone()
                if collision:
                    raise IntegrityError(f"deterministic branch id collision: {branch_id}")
            confirmation = _confirmation(connection, branch_id)
            reasons = _group_review_reasons(group, confirmation)
            account_reasons.update(reasons)
            branch_identity = "existing" if group.branch_id else "proposed"
            resolution = _default_resolution(
                branch_identity=branch_identity,
                branch_id=branch_id,
                review_reasons=reasons,
            )
            summary["resolution_actions"][resolution["action"]] += 1
            summary["branch_groups_total"] += 1
            summary["branch_groups_review_required"] += int(bool(reasons))
            summary["needs_confirmation"] += int(
                group.derivation_status == "needs_confirmation"
            )
            groups.append(
                {
                    "proposed_group_id": proposed_group_id,
                    "resolution_key": resolution_key,
                    "resolution_key_sha256": resolution_key_sha256,
                    "branch_id": branch_id,
                    "branch_identity": branch_identity,
                    "branch_key": group.branch_key,
                    "member_uids": member_uids,
                    "derived_parent_name": group.derived_parent_name,
                    "derived_branch_name": group.derived_branch_name,
                    "area": group.area,
                    "area_name": group.area_name,
                    "address": group.address,
                    "postcode": group.postcode,
                    "latitude": group.latitude,
                    "longitude": group.longitude,
                    "is_compound_or_ambiguous": group.is_ambiguous,
                    "derivation_status": group.derivation_status,
                    "identity_ambiguous": group.identity_ambiguous,
                    "identity_candidates": group.identity_candidates,
                    "identity_reason": group.identity_reason,
                    "already_snapshotted": group.already_snapshotted,
                    "confirmation": confirmation,
                    "review_reasons": reasons,
                    "resolution": resolution,
                }
            )

        quarantined = bool(account_reasons)
        summary["accounts_quarantined" if quarantined else "accounts_eligible"] += 1
        summary["unmapped_treatments"] += len(account.unmapped_treatments)
        observations: list[dict[str, Any]] = []
        for slug, source_name in sorted(account.mapped_treatments.items()):
            reviewed_mapping = reviewed_treatments.get(slug)
            database_authoritative = slug in database_reviewed_treatments
            treatment_authoritative = (
                bool(reviewed_mapping)
                and database_authoritative
                and slug in persisted_treatments
            )
            summary["treatment_observations_review_required"] += int(
                not treatment_authoritative
            )
            if treatment_authoritative:
                authority_state = "approved_catalogue_mapping"
            elif reviewed_mapping and not database_authoritative:
                authority_state = "review_required_catalogue_database_binding"
            elif reviewed_mapping:
                authority_state = "review_required_storage_projection_missing"
            else:
                authority_state = "review_required_catalogue_mapping"
            observations.append(
                {
                    "treatment_slug": slug,
                    "source_name": source_name,
                    "catalogue_release_id": catalogue_release_id,
                    "target_concept_ids": (
                        reviewed_mapping["target_concept_ids"]
                        if reviewed_mapping
                        else []
                    ),
                    "authority_state": authority_state,
                    "write": not quarantined
                    and treatment_authoritative
                    and not _existing_observation(
                        connection,
                        release_id,
                        account.sales_account_id,
                        slug,
                    ),
                }
            )

        # WP2 never turns account-level source evidence into a branch offering.
        # A later workflow requires an explicit branch-offering decision.
        branch_treatments: list[dict[str, Any]] = []

        if not quarantined:
            for group in sorted(account.groups, key=lambda item: item.branch_key):
                summary["branches_reused" if group.branch_id else "branches_new"] += 1
                summary["snapshots_written"] += int(not group.already_snapshotted)
            summary["observations_written"] += sum(
                int(item["write"]) for item in observations
            )
            summary["branch_treatments_written"] += len(branch_treatments)

        source_member_uids = sorted(
            uid for group in groups for uid in group["member_uids"]
        )
        if len(source_member_uids) != len(set(source_member_uids)):
            raise IntegrityError(
                f"branch proposal duplicates a source member for {account.sales_account_id}"
            )
        manifest_accounts.append(
            {
                "sales_account_id": account.sales_account_id,
                "source_member_uids": source_member_uids,
                "review": {
                    "status": "quarantined" if quarantined else "eligible",
                    "reasons": sorted(account_reasons),
                    "write_authority": not quarantined,
                },
                "groups": groups,
                "treatment_observations": observations,
                "unmapped_treatments": sorted(account.unmapped_treatments),
                "branch_treatments": branch_treatments,
            }
        )

    final_inputs = _input_integrity(
        connection,
        root_path,
        release_id,
        release_dir,
    )
    if final_inputs != initial_inputs:
        raise IntegrityError("branch backfill inputs changed while preview was being built")

    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "contract_version": MANIFEST_CONTRACT_VERSION,
        "release": {
            "release_id": release_id,
            "manifest_sha256": str(release["manifest_sha256"]),
            "created_at": created_at,
        },
        "treatment_authority": {
            "catalogue_release_id": catalogue_release_id,
            "catalogue_database_binding_required": True,
            "branch_offering_inference_allowed": False,
        },
        "inputs": final_inputs,
        "summary": summary,
        "accounts": manifest_accounts,
    }
    _validate_manifest_resolution_keys(payload)
    content = _manifest_bytes(payload)
    return PreparedManifest(payload, content, sha256_bytes(content))


def write_manifest(path: Path | str, prepared: PreparedManifest) -> Path:
    """Atomically create a preview without overwriting any existing bytes."""

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == prepared.content:
            if destination.stat().st_mode & 0o777 != 0o600:
                raise IntegrityError(
                    "an existing identical branch manifest must have mode 0600"
                )
            return destination
        raise IntegrityError(
            f"refusing to overwrite an existing branch manifest path: {destination}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(prepared.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            # The hard-link create is atomic and fails if a destination appears
            # after the preflight check.  Unlike os.replace, it cannot clobber a
            # research input, database, catalogue, or reviewed prior manifest.
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_file() and destination.read_bytes() == prepared.content:
                if destination.stat().st_mode & 0o777 != 0o600:
                    raise IntegrityError(
                        "an existing identical branch manifest must have mode 0600"
                    )
                return destination
            raise IntegrityError(
                f"refusing to overwrite an existing branch manifest path: {destination}"
            ) from None
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise IntegrityError(
            f"{label} has invalid keys: expected {sorted(expected)}, found {sorted(actual)}"
        )


def _checked_member_uids(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise IntegrityError(f"{label} must be a non-empty list")
    members = [str(item) for item in value]
    if any(not item for item in members) or members != sorted(set(members)):
        raise IntegrityError(f"{label} must be sorted, unique and non-empty")
    return members


def _validate_resolution_shape(resolution: object, *, label: str) -> str:
    if not isinstance(resolution, Mapping):
        raise IntegrityError(f"{label} must be an object")
    action = resolution.get("action")
    if action not in RESOLUTION_ACTIONS:
        raise IntegrityError(f"{label} has unsupported action: {action}")
    if action in {APPROVE_PROPOSED_BRANCH, DEFER_QUARANTINE}:
        _require_exact_keys(resolution, {"action"}, label=label)
    elif action == MAP_EXISTING_BRANCH:
        _require_exact_keys(
            resolution,
            {"action", "target_branch_id"},
            label=label,
        )
        if not str(resolution.get("target_branch_id") or "").strip():
            raise IntegrityError(f"{label} requires target_branch_id")
    elif action == MERGE_EXISTING_BRANCHES:
        _require_exact_keys(
            resolution,
            {"action", "target_branch_id", "source_branch_ids"},
            label=label,
        )
        target = str(resolution.get("target_branch_id") or "").strip()
        sources = resolution.get("source_branch_ids")
        if not target:
            raise IntegrityError(f"{label} requires target_branch_id")
        if not isinstance(sources, list) or not sources:
            raise IntegrityError(f"{label} requires source_branch_ids")
        source_ids = [str(value) for value in sources]
        if (
            any(not value for value in source_ids)
            or source_ids != sorted(set(source_ids))
            or target in source_ids
        ):
            raise IntegrityError(
                f"{label} source_branch_ids must be non-empty, sorted, unique, "
                "and exclude the target"
            )
    else:
        _require_exact_keys(
            resolution,
            {"action", "assignments"},
            label=label,
        )
        assignments = resolution.get("assignments")
        if not isinstance(assignments, list) or len(assignments) < 2:
            raise IntegrityError(f"{label} requires at least two exact assignments")
        for index, assignment in enumerate(assignments):
            assignment_label = f"{label}.assignments[{index}]"
            if not isinstance(assignment, Mapping):
                raise IntegrityError(f"{assignment_label} must be an object")
            _require_exact_keys(
                assignment,
                {"member_uids", "target"},
                label=assignment_label,
            )
            _checked_member_uids(
                assignment.get("member_uids"),
                label=f"{assignment_label}.member_uids",
            )
            target = assignment.get("target")
            if not isinstance(target, Mapping):
                raise IntegrityError(f"{assignment_label}.target must be an object")
            kind = target.get("kind")
            if kind == "proposed":
                _require_exact_keys(target, {"kind"}, label=f"{assignment_label}.target")
            elif kind == "existing":
                _require_exact_keys(
                    target,
                    {"kind", "branch_id"},
                    label=f"{assignment_label}.target",
                )
                if not str(target.get("branch_id") or "").strip():
                    raise IntegrityError(
                        f"{assignment_label}.target requires branch_id"
                    )
            else:
                raise IntegrityError(
                    f"{assignment_label}.target kind must be proposed or existing"
                )
    return str(action)


def _validate_manifest_resolution_keys(payload: Mapping[str, Any]) -> None:
    """Validate the portable review keys without consulting mutable DB state."""

    try:
        release_id = str(payload["release"]["release_id"])
        inputs = payload["inputs"]
        accounts = payload["accounts"]
    except (KeyError, TypeError) as exc:
        raise IntegrityError("branch resolution manifest is missing required fields") from exc
    if not release_id or not isinstance(inputs, Mapping) or not isinstance(accounts, list):
        raise IntegrityError("branch resolution manifest has invalid required fields")
    source_hashes = _resolution_source_hashes(inputs)
    seen_groups: set[str] = set()
    seen_accounts: set[str] = set()
    seen_source_members: set[str] = set()
    for account_index, account in enumerate(accounts):
        label = f"accounts[{account_index}]"
        if not isinstance(account, Mapping):
            raise IntegrityError(f"{label} must be an object")
        account_id = str(account.get("sales_account_id") or "")
        if not account_id or account_id in seen_accounts:
            raise IntegrityError("branch resolution accounts must have unique IDs")
        seen_accounts.add(account_id)
        if account.get("branch_treatments") != []:
            raise IntegrityError("branch resolution manifests cannot contain branch offerings")
        source_members = _checked_member_uids(
            account.get("source_member_uids"),
            label=f"{label}.source_member_uids",
        )
        duplicate_source_members = seen_source_members.intersection(source_members)
        if duplicate_source_members:
            raise IntegrityError(
                "branch resolution source members occur in more than one account: "
                f"{sorted(duplicate_source_members)}"
            )
        seen_source_members.update(source_members)
        groups = account.get("groups")
        if not isinstance(groups, list) or not groups:
            raise IntegrityError(f"{label}.groups must be a non-empty list")
        grouped_members: list[str] = []
        for group_index, group in enumerate(groups):
            group_label = f"{label}.groups[{group_index}]"
            if not isinstance(group, Mapping):
                raise IntegrityError(f"{group_label} must be an object")
            members = _checked_member_uids(
                group.get("member_uids"),
                label=f"{group_label}.member_uids",
            )
            grouped_members.extend(members)
            branch_key = str(group.get("branch_key") or "")
            if not branch_key:
                raise IntegrityError(f"{group_label} requires branch_key")
            expected_key, expected_hash = _resolution_key(
                release_id=release_id,
                sales_account_id=account_id,
                branch_key=branch_key,
                member_uids=members,
                source_hashes=source_hashes,
            )
            proposed_group_id = str(group.get("proposed_group_id") or "")
            if proposed_group_id in seen_groups:
                raise IntegrityError("branch resolution proposed_group_id values must be unique")
            seen_groups.add(proposed_group_id)
            if group.get("resolution_key") != expected_key:
                raise IntegrityError(f"{group_label} resolution key changed")
            if not hmac.compare_digest(
                str(group.get("resolution_key_sha256") or ""),
                expected_hash,
            ):
                raise IntegrityError(f"{group_label} resolution key hash changed")
            if proposed_group_id != expected_key["proposed_group_id"]:
                raise IntegrityError(f"{group_label} proposed_group_id changed")
            _validate_resolution_shape(
                group.get("resolution"),
                label=f"{group_label}.resolution",
            )
        if len(grouped_members) != len(set(grouped_members)):
            raise IntegrityError(f"{label} source members are duplicated across groups")
        if sorted(grouped_members) != source_members:
            raise IntegrityError(f"{label} source member grouping changed")


def read_checked_manifest(
    path: Path | str,
    expected_sha256: str,
) -> PreparedManifest:
    """Read exact manifest bytes and validate their digest and canonical form."""

    if not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("expected_manifest_sha must be 64 lowercase hexadecimal characters")
    source = Path(path).resolve()
    if not source.is_file():
        raise IntegrityError(f"branch backfill manifest not found: {source}")
    content = source.read_bytes()
    actual_sha = sha256_bytes(content)
    if not hmac.compare_digest(actual_sha, expected_sha256):
        raise IntegrityError(
            "branch backfill manifest SHA changed after preview: "
            f"expected {expected_sha256}, found {actual_sha}"
        )
    try:
        payload = json.loads(content)
    except Exception as exc:
        raise IntegrityError("branch backfill manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise IntegrityError("branch backfill manifest must be a JSON object")
    if _manifest_bytes(payload) != content:
        raise IntegrityError("branch backfill manifest is not in canonical byte form")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise IntegrityError("unsupported branch backfill manifest schema_version")
    if payload.get("contract_version") != MANIFEST_CONTRACT_VERSION:
        raise IntegrityError("unsupported branch backfill manifest contract_version")
    _validate_manifest_resolution_keys(payload)
    return PreparedManifest(payload, content, actual_sha)


def assert_current_preview(
    checked: PreparedManifest,
    current: PreparedManifest,
) -> None:
    """Bind reviewed resolutions to the exact regenerated source proposal.

    A reviewer may change only each group's closed ``resolution`` object.  All
    release/account/group keys, source hashes, proposed IDs, derived fields,
    treatment guards, and preview counts must still be byte-equivalent.
    """

    _validate_manifest_resolution_keys(checked.payload)
    _validate_manifest_resolution_keys(current.payload)

    def immutable_view(payload: Mapping[str, Any]) -> dict[str, Any]:
        accounts: list[dict[str, Any]] = []
        for account in payload["accounts"]:
            account_view = dict(account)
            account_view["groups"] = [
                {key: value for key, value in group.items() if key != "resolution"}
                for group in account["groups"]
            ]
            accounts.append(account_view)
        return {**payload, "accounts": accounts}

    checked_immutable = canonical_json_bytes(immutable_view(checked.payload))
    current_immutable = canonical_json_bytes(immutable_view(current.payload))
    if not hmac.compare_digest(
        sha256_bytes(checked_immutable),
        sha256_bytes(current_immutable),
    ) or checked_immutable != current_immutable:
        raise IntegrityError(
            "branch backfill inputs or preview counts changed; grouping and source hashes "
            "must remain exact, so create and review a new manifest"
        )


def assert_external_inputs_current(
    prepared: PreparedManifest,
    root: Path | str,
) -> None:
    """Recheck file-backed inputs immediately before the DB transaction commits."""

    root_path = Path(root).resolve()
    release_id = str(prepared.payload["release"]["release_id"])
    release_dir = root_path / "data/database/releases" / release_id
    current = {
        "current_release_pointer": _sha_file(
            root_path / "data/database/current-release.json"
        ),
        "release_manifest": _sha_file(release_dir / "manifest.json"),
        "clinic_evidence": _sha_file(release_dir / "clinic-evidence.jsonl"),
        "treatment_taxonomy": _sha_file(
            root_path / "data/database/1_finding_cliniques/procedures.json"
        ),
        "treatment_catalogue_release": _optional_sha_file(
            root_path / "data/treatments/catalog-v1.json"
        ),
        "legacy_treatment_mapping_release": _optional_sha_file(
            root_path / "data/treatments/legacy-treatment-mapping-v1.json"
        ),
    }
    expected_inputs = prepared.payload["inputs"]
    expected = {key: expected_inputs[key] for key in current}
    if current != expected:
        raise IntegrityError(
            "branch backfill file inputs changed during apply; transaction rolled back"
        )


def _existing_branch_for_account(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    branch_id: str,
    label: str,
    allow_merge_source: bool = False,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM clinic_branches WHERE id=?",
        (branch_id,),
    ).fetchone()
    if not row or row["sales_account_id"] != account_id:
        raise IntegrityError(f"{label} is not an existing branch for this account")
    confirmation = connection.execute(
        "SELECT status,merged_into FROM branch_confirmations WHERE branch_id=?",
        (branch_id,),
    ).fetchone()
    if confirmation and confirmation["status"] == "merged":
        raise IntegrityError(f"{label} is already merged")
    if confirmation and confirmation["status"] == "rejected" and not allow_merge_source:
        raise IntegrityError(f"{label} is rejected and cannot receive members")
    return row


def _assignment(
    *,
    group: Mapping[str, Any],
    branch_id: str,
    branch_identity: str,
    branch_key: str,
    member_uids: Sequence[str],
) -> dict[str, Any]:
    return {
        "proposed_group_id": group["proposed_group_id"],
        "branch_id": branch_id,
        "branch_identity": branch_identity,
        "branch_key": branch_key,
        "member_uids": list(member_uids),
        "group": group,
        "snapshot_write": True,
    }


def _plan_group_resolution(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    account_id: str,
    group: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], bool]:
    resolution = group["resolution"]
    action = _validate_resolution_shape(
        resolution,
        label=f"resolution for {group['proposed_group_id']}",
    )
    members = _checked_member_uids(
        group["member_uids"],
        label=f"members for {group['proposed_group_id']}",
    )
    if action == DEFER_QUARANTINE:
        return [], [], True
    if action == APPROVE_PROPOSED_BRANCH:
        if group["branch_identity"] != "proposed":
            raise IntegrityError("approve_proposed_branch requires a proposed branch")
        branch_id = str(group["branch_id"])
        if connection.execute(
            "SELECT 1 FROM clinic_branches WHERE id=?",
            (branch_id,),
        ).fetchone():
            raise IntegrityError(f"proposed branch already exists: {branch_id}")
        return [
            _assignment(
                group=group,
                branch_id=branch_id,
                branch_identity="proposed",
                branch_key=str(group["branch_key"]),
                member_uids=members,
            )
        ], [], False
    if action == MAP_EXISTING_BRANCH:
        target = str(resolution["target_branch_id"])
        _existing_branch_for_account(
            connection,
            account_id=account_id,
            branch_id=target,
            label="map target",
        )
        if group["already_snapshotted"] and target != group["branch_id"]:
            raise IntegrityError("an existing current-release snapshot cannot be remapped")
        return [
            _assignment(
                group=group,
                branch_id=target,
                branch_identity="existing",
                branch_key=str(group["branch_key"]),
                member_uids=members,
            )
        ], [], False
    if action == SPLIT_EXACT_ASSIGNMENTS:
        if group["already_snapshotted"]:
            raise IntegrityError("an existing current-release snapshot cannot be split")
        assignments: list[dict[str, Any]] = []
        assigned_members: list[str] = []
        for split in resolution["assignments"]:
            split_members = _checked_member_uids(
                split["member_uids"],
                label=f"split members for {group['proposed_group_id']}",
            )
            assigned_members.extend(split_members)
            target = split["target"]
            if target["kind"] == "proposed":
                branch_id = _split_branch_id(
                    release_id,
                    account_id,
                    str(group["proposed_group_id"]),
                    split_members,
                )
                if connection.execute(
                    "SELECT 1 FROM clinic_branches WHERE id=?",
                    (branch_id,),
                ).fetchone():
                    raise IntegrityError(f"proposed split branch already exists: {branch_id}")
                branch_identity = "proposed"
            else:
                branch_id = str(target["branch_id"])
                _existing_branch_for_account(
                    connection,
                    account_id=account_id,
                    branch_id=branch_id,
                    label="split target",
                )
                branch_identity = "existing"
            assignments.append(
                _assignment(
                    group=group,
                    branch_id=branch_id,
                    branch_identity=branch_identity,
                    branch_key=_split_branch_key(
                        str(group["branch_key"]),
                        split_members,
                    ),
                    member_uids=split_members,
                )
            )
        if len(assigned_members) != len(set(assigned_members)):
            raise IntegrityError("split assignments duplicate a source member")
        if sorted(assigned_members) != members:
            raise IntegrityError("split assignments must account for every source member exactly once")
        return assignments, [], False

    target = str(resolution["target_branch_id"])
    sources = [str(value) for value in resolution["source_branch_ids"]]
    _existing_branch_for_account(
        connection,
        account_id=account_id,
        branch_id=target,
        label="merge target",
    )
    for source in sources:
        _existing_branch_for_account(
            connection,
            account_id=account_id,
            branch_id=source,
            label="merge source",
            allow_merge_source=True,
        )
    implicated = set(str(value) for value in group["identity_candidates"])
    if group["branch_identity"] == "existing":
        implicated.add(str(group["branch_id"]))
    expected_sources = implicated - {target}
    if not implicated or set(sources) != expected_sources:
        raise IntegrityError(
            "merge source_branch_ids must exactly account for the proposal's existing identities"
        )
    if group["already_snapshotted"]:
        raise IntegrityError("an existing current-release snapshot cannot be merged")
    return [
        _assignment(
            group=group,
            branch_id=target,
            branch_identity="existing",
            branch_key=str(group["branch_key"]),
            member_uids=members,
        )
    ], [(source, target) for source in sources], False


def _validate_snapshot_assignment(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    account_id: str,
    assignment: dict[str, Any],
) -> None:
    branch_id = assignment["branch_id"]
    branch_key = assignment["branch_key"]
    existing = connection.execute(
        """
        SELECT sales_account_id,branch_key,member_uids_json
        FROM clinic_branch_snapshots
        WHERE release_id=? AND branch_id=?
        """,
        (release_id, branch_id),
    ).fetchone()
    if existing:
        try:
            existing_members = sorted(
                str(value) for value in json_loads(existing["member_uids_json"], [])
            )
        except Exception as exc:
            raise IntegrityError("existing branch snapshot has invalid member_uids_json") from exc
        if (
            existing["sales_account_id"] != account_id
            or existing["branch_key"] != branch_key
            or existing_members != assignment["member_uids"]
        ):
            raise IntegrityError("existing branch snapshot does not match reviewed assignment")
        assignment["snapshot_write"] = False
        return
    key_collision = connection.execute(
        """
        SELECT branch_id FROM clinic_branch_snapshots
        WHERE release_id=? AND sales_account_id=? AND branch_key=?
        """,
        (release_id, account_id, branch_key),
    ).fetchone()
    if key_collision:
        raise IntegrityError("reviewed assignment branch_key is already used in this release")
    if assignment["group"]["already_snapshotted"]:
        raise IntegrityError("reviewed assignment lost its current-release snapshot")


def _validate_projected_snapshot_partition(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    account_id: str,
    source_members: Sequence[str],
    assignments: Sequence[Mapping[str, Any]],
    merge_sources: Sequence[str],
) -> None:
    """Prove the immutable current-release snapshots remain one exact partition."""

    existing_rows = list(
        connection.execute(
            """
            SELECT branch_id,member_uids_json
            FROM clinic_branch_snapshots
            WHERE release_id=? AND sales_account_id=?
            ORDER BY branch_id
            """,
            (release_id, account_id),
        )
    )
    existing_branch_ids = {str(row["branch_id"]) for row in existing_rows}
    current_merge_sources = existing_branch_ids.intersection(merge_sources)
    if current_merge_sources:
        raise IntegrityError(
            "merge sources already have immutable current-release snapshots: "
            f"{sorted(current_merge_sources)}"
        )

    projected_members: list[str] = []
    for row in existing_rows:
        try:
            raw_members = json.loads(row["member_uids_json"])
        except Exception as exc:
            raise IntegrityError(
                "existing current-release branch snapshot has invalid member_uids_json"
            ) from exc
        if not isinstance(raw_members, list):
            raise IntegrityError(
                "existing current-release branch snapshot members must be a list"
            )
        members = [str(value) for value in raw_members]
        if (
            not members
            or any(not value for value in members)
            or members != sorted(set(members))
        ):
            raise IntegrityError(
                "existing current-release branch snapshot members must be sorted, "
                "unique and non-empty"
            )
        projected_members.extend(members)

    for assignment in assignments:
        if not assignment["snapshot_write"]:
            continue
        branch_id = str(assignment["branch_id"])
        if branch_id in existing_branch_ids:
            raise IntegrityError(
                "planned snapshot would overwrite an immutable current-release branch"
            )
        projected_members.extend(str(value) for value in assignment["member_uids"])

    if len(projected_members) != len(set(projected_members)):
        raise IntegrityError(
            "post-apply current-release snapshots duplicate a source member"
        )
    if sorted(projected_members) != sorted(source_members):
        raise IntegrityError(
            "post-apply current-release snapshots must exactly partition source members"
        )


def _plan_resolutions(
    connection: sqlite3.Connection,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _validate_manifest_resolution_keys(payload)
    release_id = str(payload["release"]["release_id"])
    summary = dict(payload["summary"])
    summary.update(
        {
            "accounts_eligible": 0,
            "accounts_quarantined": 0,
            "branches_new": 0,
            "branches_reused": 0,
            "snapshots_written": 0,
            "observations_written": 0,
            "branch_treatments_written": 0,
            "branches_merged": 0,
            "resolution_actions": {
                action: 0 for action in sorted(RESOLUTION_ACTIONS)
            },
        }
    )
    account_plans: list[dict[str, Any]] = []
    expected_global_members = {
        str(uid)
        for account in payload["accounts"]
        for uid in account["source_member_uids"]
    }
    accounted_global_members: set[str] = set()
    for account in payload["accounts"]:
        account_id = str(account["sales_account_id"])
        assignments: list[dict[str, Any]] = []
        merges: list[tuple[str, str]] = []
        deferred = False
        for group in account["groups"]:
            action = str(group["resolution"]["action"])
            summary["resolution_actions"][action] += 1
            group_assignments, group_merges, group_deferred = _plan_group_resolution(
                connection,
                release_id=release_id,
                account_id=account_id,
                group=group,
            )
            assignments.extend(group_assignments)
            merges.extend(group_merges)
            deferred = deferred or group_deferred

        if deferred:
            quarantined_members = set(account["source_member_uids"])
            if accounted_global_members.intersection(quarantined_members):
                raise IntegrityError(
                    "reviewed manifest accounts duplicate a quarantined source member"
                )
            accounted_global_members.update(quarantined_members)
            summary["accounts_quarantined"] += 1
            account_plans.append(
                {
                    "account": account,
                    "assignments": [],
                    "merges": [],
                    "deferred": True,
                }
            )
            continue

        assigned_members = [
            uid for assignment in assignments for uid in assignment["member_uids"]
        ]
        if len(assigned_members) != len(set(assigned_members)):
            raise IntegrityError("reviewed account assignments duplicate a source member")
        if sorted(assigned_members) != account["source_member_uids"]:
            raise IntegrityError(
                "reviewed account assignments must account for every source member exactly once"
            )
        assigned_member_set = set(assigned_members)
        if accounted_global_members.intersection(assigned_member_set):
            raise IntegrityError(
                "reviewed manifest accounts assign one source member more than once"
            )
        accounted_global_members.update(assigned_member_set)
        target_ids = [assignment["branch_id"] for assignment in assignments]
        if len(target_ids) != len(set(target_ids)):
            raise IntegrityError("reviewed account assigns multiple groups to one target branch")
        merge_sources = [source for source, _target in merges]
        if len(merge_sources) != len(set(merge_sources)):
            raise IntegrityError("reviewed account merges one source branch more than once")
        if set(merge_sources) & set(target_ids):
            raise IntegrityError("a merged source branch cannot also receive source members")
        branch_keys = [assignment["branch_key"] for assignment in assignments]
        if len(branch_keys) != len(set(branch_keys)):
            raise IntegrityError("reviewed account assignments duplicate a branch_key")
        for assignment in assignments:
            _validate_snapshot_assignment(
                connection,
                release_id=release_id,
                account_id=account_id,
                assignment=assignment,
            )
            summary[
                "branches_new"
                if assignment["branch_identity"] == "proposed"
                else "branches_reused"
            ] += 1
            summary["snapshots_written"] += int(assignment["snapshot_write"])
        _validate_projected_snapshot_partition(
            connection,
            release_id=release_id,
            account_id=account_id,
            source_members=account["source_member_uids"],
            assignments=assignments,
            merge_sources=merge_sources,
        )
        summary["accounts_eligible"] += 1
        summary["branches_merged"] += len(merges)
        summary["observations_written"] += sum(
            int(item["write"]) for item in account["treatment_observations"]
        )
        account_plans.append(
            {
                "account": account,
                "assignments": assignments,
                "merges": merges,
                "deferred": False,
            }
        )
    if accounted_global_members != expected_global_members:
        raise IntegrityError(
            "reviewed manifest must assign or quarantine every source member exactly once"
        )
    return account_plans, summary


def _merge_resolution_branch(
    connection: sqlite3.Connection,
    *,
    source_branch_id: str,
    target_branch_id: str,
    actor: str,
    confirmed_at: str,
) -> None:
    existing = connection.execute(
        "SELECT * FROM branch_confirmations WHERE branch_id=?",
        (source_branch_id,),
    ).fetchone()
    if existing and existing["status"] == "merged":
        if existing["merged_into"] != target_branch_id:
            raise IntegrityError("merge source is already merged into another branch")
        return
    if existing:
        connection.execute(
            """
            UPDATE branch_confirmations
            SET status='merged',merged_into=?,confirmed_by=?,confirmed_at=?,
                version=version+1,updated_at=?
            WHERE branch_id=?
            """,
            (
                target_branch_id,
                actor,
                confirmed_at,
                confirmed_at,
                source_branch_id,
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO branch_confirmations
              (branch_id,status,merged_into,confirmed_by,confirmed_at,version,updated_at)
            VALUES (?,'merged',?,?,?,1,?)
            """,
            (
                source_branch_id,
                target_branch_id,
                actor,
                confirmed_at,
                confirmed_at,
            ),
        )


def _prior_applied_summary(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    manifest_sha256: str,
) -> dict[str, Any] | None:
    """Return one exact prior result and reject ambiguous audit history."""

    matches: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT after_json
        FROM audit_events
        WHERE entity_type='research_release'
          AND entity_id=?
          AND action='branch_backfill_manifest_applied'
        ORDER BY id
        """,
        (release_id,),
    ):
        try:
            recorded = json.loads(row["after_json"])
        except Exception as exc:
            raise IntegrityError(
                "branch manifest application audit history is not valid JSON"
            ) from exc
        if not isinstance(recorded, Mapping):
            raise IntegrityError(
                "branch manifest application audit history must be an object"
            )
        if recorded.get("manifest_sha256") != manifest_sha256:
            continue
        if recorded.get("contract_version") != MANIFEST_CONTRACT_VERSION:
            raise IntegrityError(
                "prior branch manifest application used a different contract"
            )
        summary = recorded.get("summary")
        if not isinstance(summary, Mapping):
            raise IntegrityError(
                "prior branch manifest application has no valid summary"
            )
        matches.append(dict(summary))
    if len(matches) > 1:
        raise IntegrityError(
            "branch manifest application audit history contains duplicate replays"
        )
    return matches[0] if matches else None


def apply_prepared_manifest(
    connection: sqlite3.Connection,
    prepared: PreparedManifest,
    *,
    actor: str,
) -> dict[str, Any]:
    """Apply reviewed resolutions inside the caller's immediate transaction."""

    payload = prepared.payload
    if _manifest_bytes(payload) != prepared.content or not hmac.compare_digest(
        sha256_bytes(prepared.content),
        prepared.sha256,
    ):
        raise IntegrityError("prepared branch manifest bytes or SHA are inconsistent")
    release = payload["release"]
    release_id = str(release["release_id"])
    created_at = str(release["created_at"])
    if not payload["treatment_authority"]["catalogue_database_binding_required"]:
        raise IntegrityError("WP2 treatment authority requires the reviewed DB binding")
    if payload["treatment_authority"]["branch_offering_inference_allowed"]:
        raise IntegrityError("WP2 branch manifests cannot authorize offering inference")
    if any(account["branch_treatments"] for account in payload["accounts"]):
        raise IntegrityError("WP2 branch manifests cannot contain branch-offering actions")
    account_plans, applied_summary = _plan_resolutions(connection, payload)
    prior_summary = _prior_applied_summary(
        connection,
        release_id=release_id,
        manifest_sha256=prepared.sha256,
    )
    if prior_summary is not None:
        if canonical_json_bytes(prior_summary) != canonical_json_bytes(applied_summary):
            raise IntegrityError(
                "prior branch manifest application summary no longer matches the plan"
            )
        return prior_summary
    applied_at = utc_now()

    for plan in account_plans:
        if plan["deferred"]:
            # Defer is account-atomic: no safe-looking sibling group, treatment
            # observation, confirmation, or merge is allowed to leak through.
            continue
        account = plan["account"]
        account_id = str(account["sales_account_id"])
        for assignment in plan["assignments"]:
            branch_id = str(assignment["branch_id"])
            if assignment["branch_identity"] == "proposed":
                connection.execute(
                    """
                    INSERT INTO clinic_branches
                      (id,sales_account_id,first_release_id,created_at)
                    VALUES (?,?,?,?)
                    """,
                    (branch_id, account_id, release_id, created_at),
                )
            if assignment["snapshot_write"]:
                group = assignment["group"]
                connection.execute(
                    """
                    INSERT INTO clinic_branch_snapshots
                      (release_id,branch_id,sales_account_id,branch_key,member_uids_json,
                       derived_parent_name,derived_branch_name,area,area_name,address,postcode,
                       latitude,longitude,is_compound_or_ambiguous,derivation_status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        release_id,
                        branch_id,
                        account_id,
                        assignment["branch_key"],
                        json_dumps(assignment["member_uids"]),
                        group["derived_parent_name"],
                        group["derived_branch_name"],
                        group["area"],
                        group["area_name"],
                        group["address"],
                        group["postcode"],
                        group["latitude"],
                        group["longitude"],
                        int(group["is_compound_or_ambiguous"]),
                        "derived",
                        created_at,
                    ),
                )
        for source_branch_id, target_branch_id in plan["merges"]:
            _merge_resolution_branch(
                connection,
                source_branch_id=source_branch_id,
                target_branch_id=target_branch_id,
                actor=actor,
                confirmed_at=applied_at,
            )

        for observation in account["treatment_observations"]:
            if observation["write"]:
                connection.execute(
                    """
                    INSERT INTO account_treatment_observations
                      (release_id,sales_account_id,treatment_slug,source_name,created_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        release_id,
                        account_id,
                        observation["treatment_slug"],
                        observation["source_name"],
                        created_at,
                    ),
                )
    connection.execute(
        """
        INSERT INTO audit_events
          (occurred_at,actor,entity_type,entity_id,action,after_json)
        VALUES (?,?,?,?,?,?)
        """,
        (
            applied_at,
            actor,
            "research_release",
            release_id,
            "branch_backfill_manifest_applied",
            json_dumps(
                {
                    "manifest_sha256": prepared.sha256,
                    "contract_version": MANIFEST_CONTRACT_VERSION,
                    "summary": applied_summary,
                }
            ),
        ),
    )
    return applied_summary


def result_payload(
    prepared: PreparedManifest,
    *,
    mode: str,
    manifest_path: Path | str | None,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "mode": mode,
        "release_id": prepared.payload["release"]["release_id"],
        "manifest_sha256": prepared.sha256,
        "manifest_path": str(Path(manifest_path).resolve()) if manifest_path else None,
        **dict(summary or prepared.payload["summary"]),
    }
    return result


__all__ = [
    "APPROVE_PROPOSED_BRANCH",
    "DEFER_QUARANTINE",
    "MANIFEST_CONTRACT_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "MAP_EXISTING_BRANCH",
    "MERGE_EXISTING_BRANCHES",
    "PreparedManifest",
    "RESOLUTION_ACTIONS",
    "SPLIT_EXACT_ASSIGNMENTS",
    "apply_prepared_manifest",
    "assert_external_inputs_current",
    "assert_current_preview",
    "prepare_manifest",
    "read_checked_manifest",
    "result_payload",
    "write_manifest",
]
