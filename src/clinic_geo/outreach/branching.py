"""Branch and treatment derivation for outreach research releases."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from clinic_geo.parsing.clinic_names import normalize_clinic_name

from .db import IntegrityError
from .models import json_dumps, json_loads, new_id, utc_now


TAXONOMY_VERSION = "treatments-v1"
MAX_LOCATIONLESS_RATE = 0.02
# A half-percentage-point increase is large enough to flag a release even when
# its absolute locationless rate remains below the two-percent ceiling.
MATERIAL_LOCATIONLESS_RATE_INCREASE = 0.005


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def location_key(uid: str, row: Mapping) -> tuple[str, bool]:
    address = _clean(row.get("input_address"))
    compound = bool(
        re.search(
            r"\b(multiple|various|several)\b.*\b(location|branch|clinic)s?\b|"
            r"\bincluding\b.*[,;]",
            address,
            re.IGNORECASE,
        )
    )
    postcode = re.sub(r"\s+", "", _clean(row.get("postcode")).upper())
    if postcode and not compound:
        return f"postcode:{postcode}", False
    latitude = row.get("latitude")
    longitude = row.get("longitude")
    if latitude is not None and longitude is not None and not compound:
        try:
            return f"coordinates:{float(latitude):.5f}:{float(longitude):.5f}", False
        except (TypeError, ValueError):
            pass
    normalized_address = re.sub(r"[^a-z0-9]+", " ", address.casefold()).strip()
    if normalized_address and not compound:
        return f"address:{normalized_address}", False
    return f"unresolved:{uid}", True


@dataclass
class _BranchGroup:
    sales_account_id: str
    branch_key: str
    member_uids: list[str]
    representative_uid: str
    derived_parent_name: str
    derived_branch_name: str
    area: str | None
    area_name: str | None
    address: str | None
    postcode: str | None
    latitude: float | None
    longitude: float | None
    is_ambiguous: bool
    derivation_status: str
    branch_id: str | None = None
    already_snapshotted: bool = False
    identity_ambiguous: bool = False
    identity_candidates: list[str] = field(default_factory=list)
    identity_reason: str | None = None


@dataclass
class _AccountPlan:
    sales_account_id: str
    raw: dict[str, Any]
    groups: list[_BranchGroup]
    mapped_treatments: dict[str, str]
    unmapped_treatments: list[str]


@dataclass
class _ReleasePlan:
    release_id: str
    accounts: list[_AccountPlan]
    locationless_member_uids: list[str]
    locationless_rate: float
    prior_locationless_rate: float | None


def _audit(
    connection: sqlite3.Connection,
    *,
    actor: str,
    entity_type: str,
    entity_id: str,
    action: str,
    before: Any = None,
    after: Any = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events
          (occurred_at,actor,entity_type,entity_id,action,before_json,after_json)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            utc_now(),
            actor,
            entity_type,
            entity_id,
            action,
            json_dumps(before) if before is not None else None,
            json_dumps(after) if after is not None else None,
        ),
    )


def _matching_audit_exists(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    after: Mapping[str, Any],
) -> bool:
    for row in connection.execute(
        """
        SELECT after_json FROM audit_events
        WHERE entity_type=? AND entity_id=? AND action=?
        """,
        (entity_type, entity_id, action),
    ):
        try:
            value = json_loads(row["after_json"], {})
        except Exception:
            continue
        if isinstance(value, Mapping) and all(value.get(key) == expected for key, expected in after.items()):
            return True
    return False


def _taxonomy_rows(root: Path | str) -> list[dict[str, str]]:
    path = Path(root).resolve() / "data/database/1_finding_cliniques/procedures.json"
    if not path.is_file():
        raise IntegrityError(f"treatment taxonomy not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise IntegrityError(f"treatment taxonomy is not valid JSON: {path}") from exc
    if not isinstance(payload, list):
        raise IntegrityError("treatment taxonomy must be a JSON list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(payload, 1):
        if not isinstance(row, Mapping):
            raise IntegrityError(f"treatment taxonomy row {index} is not an object")
        slug = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not slug or not name:
            raise IntegrityError(f"treatment taxonomy row {index} needs id and name")
        if slug in seen:
            raise IntegrityError(f"treatment taxonomy contains duplicate id: {slug}")
        seen.add(slug)
        result.append({"id": slug, "name": name})
    return result


def _treatment_resolver(
    connection: sqlite3.Connection,
    taxonomy: Iterable[Mapping[str, str]] = (),
) -> dict[str, str]:
    resolver: dict[str, str] = {}
    for row in taxonomy:
        resolver[str(row["name"]).casefold()] = str(row["id"])
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='treatments'"
    ).fetchone()
    if not table:
        return resolver
    for row in connection.execute("SELECT slug,canonical_name,aliases_json FROM treatments"):
        resolver[str(row["canonical_name"]).casefold()] = row["slug"]
        try:
            aliases = json_loads(row["aliases_json"], [])
        except Exception as exc:
            raise IntegrityError(f"treatment {row['slug']} has invalid aliases_json") from exc
        if not isinstance(aliases, list):
            raise IntegrityError(f"treatment {row['slug']} aliases_json must be a list")
        for alias in aliases:
            value = str(alias).strip()
            if value:
                resolver[value.casefold()] = row["slug"]
    return resolver


def ensure_treatment_taxonomy(
    connection: sqlite3.Connection,
    root: Path | str,
    *,
    actor: str = "system",
) -> dict[str, str]:
    """Validate and idempotently seed the canonical treatment vocabulary."""

    rows = _taxonomy_rows(root)
    now = utc_now()
    inserted = 0
    for row in rows:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO treatments
              (slug,canonical_name,category,aliases_json,active,taxonomy_version,created_at,updated_at)
            VALUES (?,?,NULL,'[]',1,?,?,?)
            """,
            (row["id"], row["name"], TAXONOMY_VERSION, now, now),
        )
        inserted += cursor.rowcount
    if inserted:
        _audit(
            connection,
            actor=actor,
            entity_type="treatment_taxonomy",
            entity_id=TAXONOMY_VERSION,
            action="treatment_taxonomy_seeded",
            after={"inserted": inserted, "source_rows": len(rows)},
        )
    return _treatment_resolver(connection)


def _read_evidence(path: Path) -> tuple[dict[str, dict[str, Any]], list[str], int]:
    if not path.is_file():
        raise IntegrityError(f"clinic evidence not found: {path}")
    evidence: dict[str, dict[str, Any]] = {}
    locationless: list[str] = []
    total = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise IntegrityError(f"{path.name}:{line_number} is not a JSON object")
                uid = str(value.get("uid") or "").strip()
                if not uid:
                    raise IntegrityError(f"{path.name}:{line_number} has no uid")
                if uid in evidence:
                    raise IntegrityError(f"{path.name} contains duplicate uid: {uid}")
                evidence[uid] = value
                total += 1
                complete_coordinates = (
                    _float_or_none(value.get("latitude")) is not None
                    and _float_or_none(value.get("longitude")) is not None
                )
                if not (
                    _clean(value.get("postcode"))
                    or complete_coordinates
                    or _clean(value.get("input_address"))
                ):
                    locationless.append(uid)
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError(f"clinic evidence is not valid JSONL: {path}") from exc
    return evidence, sorted(locationless), total


def _safe_release_dir(root: Path, value: str, release_id: str) -> Path:
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    releases_root = (root / "data/database/releases").resolve()
    try:
        relative = path.relative_to(releases_root)
    except ValueError as exc:
        raise IntegrityError("stored research release path is outside data/database/releases") from exc
    if len(relative.parts) != 1 or relative.name != release_id:
        raise IntegrityError("stored research release path does not match its release_id")
    return path


def _prior_release_dir(
    connection: sqlite3.Connection,
    root: Path,
    release_id: str,
    release_dir: Path,
) -> Path | None:
    current = connection.execute(
        "SELECT imported_at FROM research_releases WHERE release_id=?",
        (release_id,),
    ).fetchone()
    if current:
        row = connection.execute(
            """
            SELECT release_id,release_path
            FROM research_releases
            WHERE imported_at < ? OR (imported_at = ? AND release_id < ?)
            ORDER BY imported_at DESC,release_id DESC
            LIMIT 1
            """,
            (current["imported_at"], current["imported_at"], release_id),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT release_id,release_path
            FROM research_releases
            ORDER BY imported_at DESC,release_id DESC
            LIMIT 1
            """
        ).fetchone()
    if row:
        try:
            candidate = _safe_release_dir(root, row["release_path"], row["release_id"])
        except IntegrityError:
            candidate = None
        if candidate is not None and (candidate / "clinic-evidence.jsonl").is_file():
            return candidate

    releases_root = root / "data/database/releases"
    current_key: tuple[str, str] | None = None
    current_manifest = release_dir / "manifest.json"
    if current_manifest.is_file():
        try:
            manifest = json.loads(current_manifest.read_text(encoding="utf-8"))
            current_key = (str(manifest.get("created_at") or ""), release_id)
        except Exception:
            current_key = None
    candidates: list[tuple[tuple[str, str], Path]] = []
    if releases_root.is_dir():
        for directory in releases_root.iterdir():
            if not directory.is_dir() or directory.resolve() == release_dir.resolve():
                continue
            manifest_path = directory / "manifest.json"
            evidence_path = directory / "clinic-evidence.jsonl"
            if not manifest_path.is_file() or not evidence_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                key = (str(manifest.get("created_at") or ""), directory.name)
            except Exception:
                continue
            if current_key is None or key < current_key:
                candidates.append((key, directory.resolve()))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def _validate_locationless_rate(
    connection: sqlite3.Connection,
    root: Path,
    release_id: str,
    release_dir: Path,
    locationless: list[str],
    total: int,
) -> tuple[float, float | None]:
    rate = len(locationless) / total if total else 0.0
    prior_rate: float | None = None
    prior_dir = _prior_release_dir(connection, root, release_id, release_dir)
    if prior_dir is not None:
        _prior_evidence, prior_locationless, prior_total = _read_evidence(
            prior_dir / "clinic-evidence.jsonl"
        )
        prior_rate = len(prior_locationless) / prior_total if prior_total else 0.0
    if rate > MAX_LOCATIONLESS_RATE:
        raise IntegrityError(
            f"locationless clinic evidence exceeds 2% for {release_id}: "
            f"{len(locationless)}/{total} ({rate:.2%})"
        )
    if (
        prior_rate is not None
        and rate - prior_rate > MATERIAL_LOCATIONLESS_RATE_INCREASE
    ):
        raise IntegrityError(
            f"locationless clinic evidence jumped materially for {release_id}: "
            f"{prior_rate:.2%} to {rate:.2%}"
        )
    return rate, prior_rate


def _member_uids(raw: Mapping[str, Any]) -> list[str]:
    values = raw.get("member_uids")
    if not isinstance(values, list):
        values = []
    result = [str(value).strip() for value in values if str(value).strip()]
    primary = str(raw.get("primary_uid") or "").strip()
    if primary and primary not in result:
        result.append(primary)
    return result


def _member_name_map(
    raw: Mapping[str, Any],
    member_uids: list[str],
    evidence: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    raw_names = raw.get("member_names")
    names = (
        [str(value).strip() for value in raw_names if str(value).strip()]
        if isinstance(raw_names, list)
        else []
    )
    mapped: dict[str, str] = {}
    if len(names) == len(member_uids):
        mapped.update(zip(member_uids, names))
    account_name = str(raw.get("clinic_name") or "Unnamed clinic").strip()
    for uid in member_uids:
        evidence_name = str((evidence.get(uid) or {}).get("clinic_name") or "").strip()
        if uid not in mapped and evidence_name:
            mapped[uid] = evidence_name
        else:
            mapped.setdefault(uid, account_name)
    return mapped


def _common_prefix(names: list[str]) -> list[str]:
    token_rows = [normalize_clinic_name(name).split() for name in names]
    token_rows = [tokens for tokens in token_rows if tokens]
    if not token_rows:
        return []
    prefix: list[str] = []
    for values in zip(*token_rows):
        if len(set(values)) != 1:
            break
        prefix.append(values[0])
    shortest = min(len(tokens) for tokens in token_rows)
    if len(prefix) >= 2 or (shortest and len(prefix) / shortest >= 0.60):
        return prefix
    return []


def _word_matches(value: str) -> list[re.Match[str]]:
    return list(re.finditer(r"&|[^\W_]+", value, re.UNICODE))


def _recased_prefix(value: str, token_count: int, fallback: list[str]) -> str:
    matches = _word_matches(value)
    if token_count and len(matches) >= token_count:
        return value[: matches[token_count - 1].end()].strip(" -–(),\t\r\n")
    return " ".join(fallback)


def _without_prefix(value: str, token_count: int) -> str:
    matches = _word_matches(value)
    if token_count and len(matches) >= token_count:
        value = value[matches[token_count - 1].end() :]
    return value.strip(" -–(),\t\r\n")


def _postcode_district(value: object) -> str:
    postcode = _clean(value).upper()
    if not postcode:
        return ""
    if " " in postcode:
        return postcode.split()[0]
    compact = re.sub(r"\s+", "", postcode)
    return compact[:-3] if len(compact) > 3 else compact


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _group_rows(
    sales_account_id: str,
    raw: dict[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
) -> list[_BranchGroup]:
    members = _member_uids(raw)
    if not members:
        raise IntegrityError(f"research account {sales_account_id} has no member uid")
    grouped: dict[str, list[str]] = {}
    ambiguous: dict[str, bool] = {}
    for uid in members:
        key, is_ambiguous = location_key(uid, evidence.get(uid) or {})
        grouped.setdefault(key, []).append(uid)
        ambiguous[key] = ambiguous.get(key, False) or is_ambiguous

    name_by_uid = _member_name_map(raw, members, evidence)
    primary_uid = str(raw.get("primary_uid") or "").strip()
    primary_name = name_by_uid.get(primary_uid) or name_by_uid[members[0]]
    prefix = _common_prefix([name_by_uid[uid] for uid in members]) if len(members) > 1 else []
    parent_name = (
        name_by_uid[members[0]]
        if len(members) == 1
        else _recased_prefix(primary_name, len(prefix), prefix)
        if prefix
        else primary_name
    )
    no_common_prefix = len(members) > 1 and not prefix

    result: list[_BranchGroup] = []
    for branch_number, branch_key in enumerate(sorted(grouped), 1):
        uids = sorted(grouped[branch_key])
        representative_uid = primary_uid if primary_uid in uids else uids[0]
        row = evidence.get(representative_uid) or {}
        if len(members) == 1:
            branch_name = (
                _clean(row.get("area_name"))
                or _postcode_district(row.get("postcode"))
                or "Main"
            )
        else:
            representative_name = name_by_uid[representative_uid]
            branch_name = _without_prefix(representative_name, len(prefix)) if prefix else ""
            branch_name = (
                branch_name
                or _clean(row.get("area_name"))
                or _clean(row.get("postcode"))
                or f"Branch {branch_number}"
            )
        needs_confirmation = ambiguous[branch_key] or no_common_prefix
        result.append(
            _BranchGroup(
                sales_account_id=sales_account_id,
                branch_key=branch_key,
                member_uids=uids,
                representative_uid=representative_uid,
                derived_parent_name=parent_name,
                derived_branch_name=branch_name,
                area=_clean(row.get("area")) or None,
                area_name=_clean(row.get("area_name")) or None,
                address=_clean(row.get("input_address")) or None,
                postcode=_clean(row.get("postcode")) or None,
                latitude=_float_or_none(row.get("latitude")),
                longitude=_float_or_none(row.get("longitude")),
                is_ambiguous=ambiguous[branch_key],
                derivation_status="needs_confirmation" if needs_confirmation else "derived",
            )
        )
    return result


def _history(
    connection: sqlite3.Connection,
    sales_account_id: str,
    release_id: str,
) -> tuple[dict[str, tuple[str, str]], dict[str, str], dict[str, set[str]], set[str]]:
    rows = list(
        connection.execute(
            """
            SELECT cbs.release_id,cbs.branch_id,cbs.branch_key,cbs.member_uids_json,
                   cbs.derivation_status,rr.imported_at
            FROM clinic_branch_snapshots cbs
            JOIN research_releases rr ON rr.release_id=cbs.release_id
            WHERE cbs.sales_account_id=?
            ORDER BY rr.imported_at DESC,cbs.release_id DESC,cbs.branch_id
            """,
            (sales_account_id,),
        )
    )
    current_by_key: dict[str, tuple[str, str]] = {}
    exact_by_key: dict[str, str] = {}
    latest_members: dict[str, set[str]] = {}
    for row in rows:
        try:
            members = {str(value) for value in json_loads(row["member_uids_json"], [])}
        except Exception as exc:
            raise IntegrityError(
                f"branch snapshot {row['branch_id']} has invalid member_uids_json"
            ) from exc
        latest_members.setdefault(row["branch_id"], members)
        if row["release_id"] == release_id:
            current_by_key[row["branch_key"]] = (
                row["branch_id"],
                row["derivation_status"],
            )
        else:
            exact_by_key.setdefault(row["branch_key"], row["branch_id"])
    branch_ids = {
        row["id"]
        for row in connection.execute(
            "SELECT id FROM clinic_branches WHERE sales_account_id=?",
            (sales_account_id,),
        )
    }
    return current_by_key, exact_by_key, latest_members, branch_ids


def _assign_branch_identities(
    connection: sqlite3.Connection,
    release_id: str,
    account: _AccountPlan,
) -> None:
    current_by_key, exact_by_key, latest_members, _branch_ids = _history(
        connection, account.sales_account_id, release_id
    )
    claimed: set[str] = set()

    # Step 0: a repeated derivation of this release always reuses its persisted
    # identity before considering historical matches or minting anything.
    remaining: list[_BranchGroup] = []
    for group in sorted(account.groups, key=lambda item: item.branch_key):
        current = current_by_key.get(group.branch_key)
        if current:
            group.branch_id = current[0]
            group.derivation_status = current[1]
            group.already_snapshotted = True
            claimed.add(current[0])
        else:
            remaining.append(group)

    # Pass A: exact keys are resolved first so they outrank overlap contention.
    overlap_pass: list[_BranchGroup] = []
    for group in remaining:
        exact = exact_by_key.get(group.branch_key)
        if exact and exact not in claimed:
            group.branch_id = exact
            claimed.add(exact)
        elif exact:
            group.identity_ambiguous = True
            group.identity_candidates = [exact]
            group.identity_reason = "historical branch already claimed by an exact-key group"
        else:
            overlap_pass.append(group)

    # Pass B: compare only with each branch's latest snapshot member set.
    for group in overlap_pass:
        members = set(group.member_uids)
        candidates = sorted(
            branch_id
            for branch_id, historical_members in latest_members.items()
            if members & historical_members
        )
        if len(candidates) == 1 and candidates[0] not in claimed:
            group.branch_id = candidates[0]
            claimed.add(candidates[0])
        elif candidates:
            group.identity_ambiguous = True
            group.identity_candidates = candidates
            group.identity_reason = (
                "historical branch already claimed in this release"
                if len(candidates) == 1
                else "current group overlaps multiple historical branches"
            )

    for group in account.groups:
        if group.identity_ambiguous:
            group.derivation_status = "needs_confirmation"


def _account_inputs_from_database(
    connection: sqlite3.Connection,
    release_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for row in connection.execute(
        """
        SELECT sales_account_id,raw_json
        FROM research_account_snapshots
        WHERE release_id=?
        ORDER BY sales_account_id,research_account_id
        """,
        (release_id,),
    ):
        try:
            raw = json_loads(row["raw_json"], {})
        except Exception as exc:
            raise IntegrityError(
                f"research snapshot for {row['sales_account_id']} has invalid raw_json"
            ) from exc
        if not isinstance(raw, dict):
            raise IntegrityError(
                f"research snapshot for {row['sales_account_id']} raw_json is not an object"
            )
        result.append((row["sales_account_id"], raw))
    return result


def _make_plan(
    connection: sqlite3.Connection,
    root: Path,
    release_id: str,
    release_dir: Path,
    account_inputs: list[tuple[str, dict[str, Any]]],
    resolver: Mapping[str, str],
) -> _ReleasePlan:
    evidence, row_locationless, total_evidence = _read_evidence(
        release_dir / "clinic-evidence.jsonl"
    )
    expected_members = {
        uid
        for _sales_account_id, raw in account_inputs
        for uid in _member_uids(raw)
    }
    locationless = sorted(set(row_locationless) | (expected_members - set(evidence)))
    locationless_rate, prior_rate = _validate_locationless_rate(
        connection,
        root,
        release_id,
        release_dir,
        locationless,
        max(total_evidence, len(expected_members)),
    )
    accounts: list[_AccountPlan] = []
    total_mentions = 0
    total_unmapped = 0
    for sales_account_id, raw in account_inputs:
        groups = _group_rows(sales_account_id, raw, evidence)
        mapped: dict[str, str] = {}
        unmapped: list[str] = []
        observed = raw.get("observed_procedures")
        if not isinstance(observed, list):
            observed = []
        for value in observed:
            source_name = str(value).strip()
            if not source_name:
                continue
            total_mentions += 1
            slug = resolver.get(source_name.casefold())
            if slug:
                mapped.setdefault(slug, source_name)
            else:
                total_unmapped += 1
                unmapped.append(source_name)
        account = _AccountPlan(
            sales_account_id=sales_account_id,
            raw=raw,
            groups=groups,
            mapped_treatments=mapped,
            unmapped_treatments=unmapped,
        )
        _assign_branch_identities(connection, release_id, account)
        accounts.append(account)
    if total_mentions and total_unmapped / total_mentions > 0.05:
        raise IntegrityError(
            f"unmapped observed treatments exceed 5% for {release_id}: "
            f"{total_unmapped}/{total_mentions} ({total_unmapped / total_mentions:.2%})"
        )
    return _ReleasePlan(
        release_id=release_id,
        accounts=accounts,
        locationless_member_uids=locationless,
        locationless_rate=locationless_rate,
        prior_locationless_rate=prior_rate,
    )


def _planned_counts(connection: sqlite3.Connection, plan: _ReleasePlan) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "branches_new": 0,
        "branches_reused": 0,
        "snapshots_written": 0,
        "observations_written": 0,
        "branch_treatments_written": 0,
        "needs_confirmation": 0,
        "unmapped_treatments": 0,
        "locationless_member_uids": plan.locationless_member_uids,
        "locationless_member_count": len(plan.locationless_member_uids),
        "locationless_rate": plan.locationless_rate,
        "prior_locationless_rate": plan.prior_locationless_rate,
    }
    for account in plan.accounts:
        for group in account.groups:
            if group.branch_id:
                counts["branches_reused"] += 1
            else:
                counts["branches_new"] += 1
            if not group.already_snapshotted:
                counts["snapshots_written"] += 1
            if group.derivation_status == "needs_confirmation":
                counts["needs_confirmation"] += 1
        counts["unmapped_treatments"] += len(account.unmapped_treatments)
        for slug in account.mapped_treatments:
            exists = connection.execute(
                """
                SELECT 1 FROM account_treatment_observations
                WHERE release_id=? AND sales_account_id=? AND treatment_slug=?
                """,
                (plan.release_id, account.sales_account_id, slug),
            ).fetchone()
            if not exists:
                counts["observations_written"] += 1
        if len(account.groups) == 1:
            group = account.groups[0]
            for slug in account.mapped_treatments:
                exists = (
                    connection.execute(
                        """
                        SELECT 1 FROM branch_treatments
                        WHERE branch_id=? AND treatment_slug=? AND source='research_observed'
                        """,
                        (group.branch_id, slug),
                    ).fetchone()
                    if group.branch_id
                    else None
                )
                if not exists:
                    counts["branch_treatments_written"] += 1
    return counts


def plan_for_import(
    connection: sqlite3.Connection,
    root: Path | str,
    *,
    release_id: str,
    release_dir: Path | str,
    accounts: list[dict[str, Any]],
    sales_account_ids: list[str],
) -> dict[str, Any]:
    """Return branch/treatment counts for an importer dry-run without mutations."""

    if len(accounts) != len(sales_account_ids):
        raise IntegrityError("branch derivation inputs do not align with account mappings")
    root_path = Path(root).resolve()
    taxonomy = _taxonomy_rows(root_path)
    resolver = _treatment_resolver(connection, taxonomy)
    inputs = [(sales_id, dict(raw)) for sales_id, raw in zip(sales_account_ids, accounts)]
    plan = _make_plan(
        connection,
        root_path,
        release_id,
        Path(release_dir).resolve(),
        inputs,
        resolver,
    )
    return _planned_counts(connection, plan)


def derive_for_release(
    connection: sqlite3.Connection,
    root: Path | str,
    release_id: str,
    actor: str = "system",
) -> dict[str, Any]:
    """Derive stable branches and treatment observations for one imported release."""

    root_path = Path(root).resolve()
    release = connection.execute(
        "SELECT release_path FROM research_releases WHERE release_id=?",
        (release_id,),
    ).fetchone()
    if not release:
        raise IntegrityError(f"research release is not imported: {release_id}")
    release_dir = _safe_release_dir(root_path, release["release_path"], release_id)
    resolver = ensure_treatment_taxonomy(connection, root_path, actor=actor)
    plan = _make_plan(
        connection,
        root_path,
        release_id,
        release_dir,
        _account_inputs_from_database(connection, release_id),
        resolver,
    )
    counts = {
        "branches_new": 0,
        "branches_reused": 0,
        "snapshots_written": 0,
        "observations_written": 0,
        "branch_treatments_written": 0,
        "needs_confirmation": 0,
        "unmapped_treatments": 0,
        "locationless_member_uids": plan.locationless_member_uids,
        "locationless_member_count": len(plan.locationless_member_uids),
        "locationless_rate": plan.locationless_rate,
        "prior_locationless_rate": plan.prior_locationless_rate,
    }
    summary_exists = connection.execute(
        """
        SELECT 1 FROM audit_events
        WHERE entity_type='research_release' AND entity_id=? AND action='branches_derived'
        LIMIT 1
        """,
        (release_id,),
    ).fetchone()
    unmapped_audits_written = 0
    now = utc_now()
    for account in plan.accounts:
        for group in sorted(account.groups, key=lambda item: item.branch_key):
            if group.branch_id:
                counts["branches_reused"] += 1
            else:
                group.branch_id = new_id("branch")
                connection.execute(
                    """
                    INSERT INTO clinic_branches
                      (id,sales_account_id,first_release_id,created_at)
                    VALUES (?,?,?,?)
                    """,
                    (group.branch_id, account.sales_account_id, release_id, now),
                )
                counts["branches_new"] += 1
                _audit(
                    connection,
                    actor=actor,
                    entity_type="clinic_branch",
                    entity_id=group.branch_id,
                    action="created",
                    after={
                        "sales_account_id": account.sales_account_id,
                        "first_release_id": release_id,
                        "branch_key": group.branch_key,
                    },
                )
            if group.identity_ambiguous:
                _audit(
                    connection,
                    actor=actor,
                    entity_type="clinic_branch",
                    entity_id=group.branch_id,
                    action="branch_identity_ambiguous",
                    after={
                        "release_id": release_id,
                        "sales_account_id": account.sales_account_id,
                        "branch_key": group.branch_key,
                        "candidate_branch_ids": group.identity_candidates,
                        "reason": group.identity_reason,
                    },
                )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO clinic_branch_snapshots
                  (release_id,branch_id,sales_account_id,branch_key,member_uids_json,
                   derived_parent_name,derived_branch_name,area,area_name,address,postcode,
                   latitude,longitude,is_compound_or_ambiguous,derivation_status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    release_id,
                    group.branch_id,
                    account.sales_account_id,
                    group.branch_key,
                    json_dumps(group.member_uids),
                    group.derived_parent_name,
                    group.derived_branch_name,
                    group.area,
                    group.area_name,
                    group.address,
                    group.postcode,
                    group.latitude,
                    group.longitude,
                    int(group.is_ambiguous),
                    group.derivation_status,
                    now,
                ),
            )
            counts["snapshots_written"] += cursor.rowcount
            if group.derivation_status == "needs_confirmation":
                counts["needs_confirmation"] += 1

        for source_name in account.unmapped_treatments:
            counts["unmapped_treatments"] += 1
            audit_after = {"release_id": release_id, "source_name": source_name}
            if not _matching_audit_exists(
                connection,
                entity_type="sales_account",
                entity_id=account.sales_account_id,
                action="treatment_unmapped",
                after=audit_after,
            ):
                _audit(
                    connection,
                    actor=actor,
                    entity_type="sales_account",
                    entity_id=account.sales_account_id,
                    action="treatment_unmapped",
                    after=audit_after,
                )
                unmapped_audits_written += 1
        for slug, source_name in account.mapped_treatments.items():
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO account_treatment_observations
                  (release_id,sales_account_id,treatment_slug,source_name,created_at)
                VALUES (?,?,?,?,?)
                """,
                (release_id, account.sales_account_id, slug, source_name, now),
            )
            counts["observations_written"] += cursor.rowcount
        if len(account.groups) == 1:
            branch_id = account.groups[0].branch_id
            for slug in account.mapped_treatments:
                existing = connection.execute(
                    """
                    SELECT 1 FROM branch_treatments
                    WHERE branch_id=? AND treatment_slug=? AND source='research_observed'
                    """,
                    (branch_id, slug),
                ).fetchone()
                if existing:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO branch_treatments
                      (id,branch_id,treatment_slug,state,source,release_id,version,created_at,updated_at)
                    VALUES (?,?,?,'observed','research_observed',?,1,?,?)
                    """,
                    (new_id("brtx"), branch_id, slug, release_id, now, now),
                )
                counts["branch_treatments_written"] += cursor.rowcount

    reference_writes = sum(
        counts[key]
        for key in (
            "branches_new",
            "snapshots_written",
            "observations_written",
            "branch_treatments_written",
        )
    )
    if reference_writes or unmapped_audits_written or not summary_exists:
        _audit(
            connection,
            actor=actor,
            entity_type="research_release",
            entity_id=release_id,
            action="branches_derived",
            after=counts,
        )
    return counts
