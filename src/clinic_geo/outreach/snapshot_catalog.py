"""Read-only Snapshot selection catalogue derived from an outreach release."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Mapping

from clinic_geo.parsing.treatments import treatment_key
from clinic_geo.persistence.json_store import read_json, read_jsonl

from .branching import location_key as branch_location_key
from .importer import VerifiedRelease


SNAPSHOT_PROCEDURES_RELATIVE_PATH = Path(
    "data/database/1_finding_cliniques/procedures.json"
)
SCORED_CLINICS_FILENAME = "scored-clinics.jsonl"


class SnapshotCatalogError(ValueError):
    """The verified release cannot produce a Snapshot selection catalogue."""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _selection_branches(config: Mapping) -> list[dict]:
    """Project outreach branch records into the Snapshot selection shape."""

    raw = config.get("branches")
    if raw is None:
        raw = config.get("areas")
    if raw is None:
        raw = [
            {
                "branch": {
                    "id": config.get("branch_id") or "primary-branch",
                    "name": config.get("branch_name")
                    or (config.get("catchment") or {}).get("name")
                    or "Primary branch",
                },
                "catchment": config.get("catchment") or {},
                "treatments": config.get("treatments") or [],
            }
        ]
    if not isinstance(raw, list) or not raw:
        raise SnapshotCatalogError("No branch catalogue is available for this clinic")

    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise SnapshotCatalogError(f"Branch catalogue row {index} is malformed")
        branch = item.get("branch") if isinstance(item.get("branch"), Mapping) else item
        catchment = item.get("catchment") if isinstance(item.get("catchment"), Mapping) else {}
        branch_id = _clean(branch.get("id") or item.get("branch_id") or catchment.get("area_id"))
        branch_name = _clean(branch.get("name") or item.get("branch_name") or catchment.get("name"))
        if not branch_id or not branch_name:
            raise SnapshotCatalogError(f"Branch catalogue row {index} needs an id and name")
        if branch_id in seen:
            raise SnapshotCatalogError(f"Duplicate branch id: {branch_id}")
        seen.add(branch_id)
        treatments = item.get("treatments")
        if not isinstance(treatments, list) or not treatments:
            raise SnapshotCatalogError(f"{branch_name} has no selectable treatments")
        treatment_rows = []
        treatment_ids: set[str] = set()
        for treatment_index, treatment in enumerate(treatments, 1):
            source = treatment if isinstance(treatment, Mapping) else {"name": treatment}
            treatment_id = _clean(source.get("id"))
            treatment_name = _clean(source.get("name"))
            if not treatment_id or not treatment_name:
                raise SnapshotCatalogError(
                    f"Treatment {treatment_index} for {branch_name} needs an id and name"
                )
            if treatment_id in treatment_ids:
                raise SnapshotCatalogError(
                    f"Duplicate treatment id {treatment_id} for {branch_name}"
                )
            treatment_ids.add(treatment_id)
            treatment_rows.append(
                {
                    "id": treatment_id,
                    "name": treatment_name,
                    "candidate_status": _clean(
                        source.get("commercial_relevance_status") or "unconfirmed"
                    ).casefold(),
                    "source_url": _clean(source.get("source_url")) or None,
                }
            )
        result.append(
            {
                "id": branch_id,
                "name": branch_name,
                "address": _clean(branch.get("address") or item.get("address")) or None,
                "source_url": _clean(branch.get("source_url") or item.get("source_url")) or None,
                "member_uids": [
                    _clean(value) for value in branch.get("member_uids") or [] if _clean(value)
                ],
                "location_confidence": _clean(
                    branch.get("location_confidence") or item.get("location_confidence")
                )
                or None,
                "requires_manual_confirmation": bool(
                    branch.get("requires_manual_confirmation")
                    or item.get("requires_manual_confirmation")
                ),
                "candidate_status": _clean(
                    item.get("validation_status")
                    or branch.get("validation_status")
                    or catchment.get("confirmation_status")
                    or "unconfirmed"
                ).casefold(),
                "catchment": deepcopy(dict(catchment)),
                "treatments": treatment_rows,
            }
        )
    return result


def build_snapshot_selection_catalog(
    verified_release: VerifiedRelease,
    *,
    procedures_path: Path | str,
) -> tuple[Mapping, Mapping]:
    """Build the deterministic branch/treatment choices for a verified release."""

    procedures = read_json(procedures_path)
    if not isinstance(procedures, list) or not procedures:
        raise SnapshotCatalogError("The treatment catalogue is unavailable")
    treatment_rows = [
        {"id": _clean(row.get("id")), "name": _clean(row.get("name"))}
        for row in procedures
        if isinstance(row, Mapping) and _clean(row.get("id")) and _clean(row.get("name"))
    ]
    treatment_by_name = {treatment_key(row["name"]): row["id"] for row in treatment_rows}
    scored_rows = read_jsonl(verified_release.release_dir / SCORED_CLINICS_FILENAME)
    branch_by_uid = {
        _clean(row.get("uid")): row
        for row in scored_rows
        if isinstance(row, Mapping) and _clean(row.get("uid"))
    }
    accounts = []
    account_catalog: dict[str, dict] = {}
    for account in verified_release.accounts:
        account_id = _clean(account.get("account_id") or account.get("stable_account_key"))
        if not account_id:
            continue
        observed_ids = {
            treatment_by_name[key]
            for key in (treatment_key(value) for value in account.get("observed_procedures") or [])
            if key in treatment_by_name
        }
        location_groups: dict[str, list[tuple[str, Mapping, bool]]] = {}
        for uid in account.get("member_uids") or []:
            uid = _clean(uid)
            row = branch_by_uid.get(uid)
            if not row:
                continue
            key, unresolved = branch_location_key(uid, row)
            location_groups.setdefault(key, []).append((uid, row, unresolved))
        branches = []
        primary_uid = _clean(account.get("primary_uid"))
        for location_key, members in sorted(location_groups.items()):
            members.sort(
                key=lambda item: (
                    item[0] != primary_uid,
                    item[2],
                    _clean(item[1].get("area_confidence")).casefold() != "high",
                    not bool(_clean(item[1].get("input_address"))),
                    item[0],
                )
            )
            uid, row, unresolved = members[0]
            member_uids = sorted(item[0] for item in members)
            area_id = _clean(row.get("area") or row.get("area_unit"))
            area_name = _clean(row.get("area_name") or row.get("legacy_area"))
            postcode = _clean(row.get("postcode"))
            if not area_id or not area_name:
                continue
            branch_id = "branch-" + hashlib.sha256(
                f"{account_id}|{location_key}|{'|'.join(member_uids)}".encode("utf-8")
            ).hexdigest()[:16]
            branch_treatments = [
                {
                    **treatment,
                    "commercial_relevance_status": (
                        "observed_account_level" if treatment["id"] in observed_ids else "unconfirmed"
                    ),
                }
                for treatment in sorted(
                    treatment_rows,
                    key=lambda item: (item["id"] not in observed_ids, item["name"].casefold()),
                )
            ]
            branches.append(
                {
                    "branch": {
                        "id": branch_id,
                        "name": _clean(row.get("clinic_name")) or _clean(uid),
                        "address": _clean(row.get("input_address")) or None,
                        "source_url": _clean(
                            row.get("official_website_in_input")
                            or row.get("website_booking_url_checked")
                        )
                        or None,
                        "member_uids": member_uids,
                        "location_confidence": _clean(row.get("area_confidence")) or None,
                        "requires_manual_confirmation": bool(unresolved),
                    },
                    "catchment": {
                        "area_id": area_id,
                        "name": area_name,
                        "country_code": "GB",
                        "confirmation_status": "unconfirmed",
                        "variants": [
                            {"type": "area", "value": area_name},
                            *([{"type": "postcode", "value": postcode}] if postcode else []),
                        ],
                    },
                    "validation_status": "unconfirmed",
                    "treatments": branch_treatments,
                }
            )
        if not branches:
            continue
        clinic_name = _clean(account.get("clinic_name")) or branches[0]["branch"]["name"]
        aliases = [
            _clean(value)
            for value in account.get("member_names") or []
            if _clean(value) and _clean(value).casefold() != clinic_name.casefold()
        ]
        dynamic_config = {
            "clinic": {
                "account_id": account_id,
                "name": clinic_name,
                "verified_aliases": list(dict.fromkeys(aliases)),
                "identity_status": "unconfirmed",
            },
            "branches": branches,
            "research_release_id": verified_release.release_id,
            "research_manifest_sha256": verified_release.manifest_sha256,
        }
        account_catalog[account_id] = dynamic_config
        public_branches = []
        for branch in _selection_branches(dynamic_config):
            public_branches.append(
                {
                    key: value
                    for key, value in branch.items()
                    if key != "treatments"
                }
                | {
                    "suggested_treatment_ids": [
                        row["id"]
                        for row in branch["treatments"]
                        if row.get("candidate_status") == "observed_account_level"
                    ]
                }
            )
        accounts.append(
            {
                "account_id": account_id,
                "clinic_name": clinic_name,
                "prospect_rank": account.get("prospect_rank"),
                "prospect_tier": account.get("prospect_tier"),
                "branches": public_branches,
            }
        )
    accounts.sort(
        key=lambda row: (
            row.get("prospect_rank") is None,
            row.get("prospect_rank") or 10**9,
            row["clinic_name"].casefold(),
        )
    )
    return {
        "schema_version": "free_snapshot.production_options.v2",
        "research_release_id": verified_release.release_id,
        "research_manifest_sha256": verified_release.manifest_sha256,
        "treatments": treatment_rows,
        "accounts": accounts,
    }, account_catalog


__all__ = [
    "SNAPSHOT_PROCEDURES_RELATIVE_PATH",
    "SnapshotCatalogError",
    "build_snapshot_selection_catalog",
]
