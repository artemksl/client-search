"""Deterministic, scope-local focused competitor review."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Mapping, Optional

from clinic_geo.parsing.clinic_names import normalize_clinic_name
from clinic_geo.parsing.treatments import treatment_key

from .recommendations_v2 import stable_external_entity_id


COMPETITOR_REVIEW_SCHEMA_VERSION = "free_snapshot.competitor_review.v1"
_CONFIRMED = frozenset({"confirmed", "resolved", "verified", "relevant", "unique", "active"})
_IRRELEVANT = frozenset({"irrelevant", "outside_catchment", "wrong_area", "wrong_treatment"})
_INACTIVE = frozenset({"inactive", "closed", "not_trading", "dissolved"})
_DUPLICATE = frozenset({"duplicate", "alias", "branch_duplicate", "duplicate_of_client", "client_alias"})


class CompetitorReviewError(ValueError):
    pass


def _clean(value: object) -> str:
    return str(value or "").strip()


def _as_database_rows(clinic_database: object) -> list[Mapping]:
    if clinic_database is None:
        return []
    if isinstance(clinic_database, Mapping):
        rows = clinic_database.get("clinics") or clinic_database.get("rows") or clinic_database.get("records")
        if rows is None and all(isinstance(value, Mapping) for value in clinic_database.values()):
            rows = list(clinic_database.values())
    else:
        rows = clinic_database
    if not isinstance(rows, (list, tuple)):
        raise CompetitorReviewError("clinic_database must be a list or contain a clinics/rows list")
    return [row for row in rows if isinstance(row, Mapping)]


def _record_names(record: Mapping) -> set[str]:
    values = [
        record.get("clinic_name"),
        record.get("name"),
        record.get("canonical_name"),
        *(record.get("aliases") or []),
        *(record.get("verified_aliases") or []),
        *(record.get("trading_names") or []),
    ]
    return {normalize_clinic_name(value) for value in values if normalize_clinic_name(value)}


def _external_lookup(external_evidence: object, candidate: Mapping) -> Mapping:
    if not isinstance(external_evidence, Mapping):
        return {}
    matches: list[Mapping] = []
    seen: set[int] = set()
    for key in (
        _clean(candidate.get("entity_id")),
        _clean(candidate.get("canonical_name") or candidate.get("name")),
        normalize_clinic_name(candidate.get("canonical_name") or candidate.get("name")),
    ):
        row = external_evidence.get(key)
        if isinstance(row, Mapping) and id(row) not in seen:
            seen.add(id(row))
            matches.append(row)
    if not matches:
        return {}

    # The automatic index intentionally exposes a row under its source entity
    # ID, display name and normalized name.  A founder resolution may be keyed
    # under only one of those values, so returning the first match could let an
    # automatic row shadow the human decision.  Merge every matching row and
    # apply founder resolutions last, regardless of which lookup key matched.
    merged: dict = {}
    for row in sorted(
        matches,
        key=lambda value: _clean(value.get("evidence_source")).casefold()
        == "founder_resolution",
    ):
        merged.update(dict(row))
    return merged


def _external_status(
    external: Mapping,
    current: str,
    *keys: str,
    automatic_allowed: frozenset[str],
) -> str:
    """Apply web evidence only when it affirmatively proves that field.

    A hallucinated or mismatched AI URL must not weaken an existing canonical
    database verification. Founder/analyst rows (which do not identify
    themselves as ``automated_web``) retain full override authority.
    """

    supplied = ""
    for key in keys:
        supplied = _clean(external.get(key)).casefold()
        if supplied:
            break
    if not supplied:
        return current
    if _clean(external.get("evidence_source")).casefold() == "automated_web":
        # Canonical database decisions are authoritative.  Automatic web
        # evidence may fill an unresolved field, but it cannot turn a known
        # duplicate into ``unique``, an irrelevant clinic into relevant, or an
        # inactive record into active (and vice versa).  Such conflicts remain
        # for founder resolution.
        if _clean(current).casefold() not in {"", "unresolved"}:
            return current
        return supplied if supplied in automatic_allowed else current
    return supplied


def _record_area_status(scope: Mapping, record: Mapping) -> str:
    # v4 cells carry the exact geography measured by the prompt.  Legacy
    # treatment cells continue to use ``catchment``.  Never recover either
    # value by splitting a scope id: v4 ids are deliberately opaque.
    raw_targets = scope.get("target_geographies")
    targets = (
        [row for row in raw_targets if isinstance(row, Mapping)]
        if isinstance(raw_targets, (list, tuple))
        else []
    )
    target = scope.get("target_geography")
    if isinstance(target, Mapping):
        targets.append(target)
    if not targets and isinstance(scope.get("catchment"), Mapping):
        targets.append(scope["catchment"])
    expected: set[str] = set()
    for catchment in targets:
        expected.update(
            {
                normalize_clinic_name(catchment.get("id") or catchment.get("area_id")),
                normalize_clinic_name(catchment.get("name")),
                *(
                    normalize_clinic_name(row.get("value"))
                    for row in catchment.get("variants") or []
                    if isinstance(row, Mapping)
                ),
            }
        )
    expected.discard("")
    observed = {
        normalize_clinic_name(record.get("area_id")),
        normalize_clinic_name(record.get("area_unit")),
        normalize_clinic_name(record.get("area")),
        normalize_clinic_name(record.get("area_name")),
        normalize_clinic_name(record.get("legacy_area")),
    } - {""}
    if expected & observed:
        return "confirmed"
    if observed and _clean(record.get("area_status")).casefold() in {"assigned", "confirmed", "verified"}:
        return "irrelevant"
    return "unresolved"


def _record_treatment_status(scope: Mapping, record: Mapping) -> str:
    treatment_id = str(scope.get("treatment_id") or "")
    treatment = None
    for row in scope.get("treatments") or []:
        if isinstance(row, Mapping) and (not treatment_id or str(row.get("id")) == treatment_id):
            treatment = row
            break
    treatment = treatment or (scope.get("treatment") if isinstance(scope.get("treatment"), Mapping) else {})
    expected = treatment_key(treatment.get("name"))
    values = []
    for key in ("verified_treatments", "treatments", "procedures", "services", "observed_procedures"):
        raw = record.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, (list, tuple)):
            values.extend(raw)
    observed = {treatment_key(value) for value in values if treatment_key(value)}
    evidence_status = _clean(
        record.get("treatment_evidence_status")
        or record.get("procedure_evidence_status")
        or record.get("service_evidence_status")
    ).casefold()
    authoritative = any(word in evidence_status for word in ("confirmed", "verified", "authoritative"))
    if expected and expected in observed and authoritative:
        return "confirmed"
    if authoritative and record.get("service_list_complete") and expected not in observed:
        return "irrelevant"
    return "unresolved"


def _record_subject_status(scope: Mapping, record: Mapping) -> str:
    """Resolve treatment or general clinic-archetype relevance.

    The persisted validation field remains ``treatment_status`` for legacy
    readers, but v4 also labels it as subject relevance.  A general clinic
    prompt must never be made relevant by inventing a treatment.
    """

    if _clean(scope.get("subject_kind")).casefold() != "clinic_archetype":
        return _record_treatment_status(scope, record)

    archetype = scope.get("clinic_archetype")
    if not isinstance(archetype, Mapping):
        clinic = scope.get("clinic") if isinstance(scope.get("clinic"), Mapping) else {}
        archetype = clinic.get("archetype") if isinstance(clinic.get("archetype"), Mapping) else {}
    expected_values = {
        normalize_clinic_name(archetype.get("id")),
        normalize_clinic_name(archetype.get("singular")),
        normalize_clinic_name(archetype.get("plural")),
    } - {""}
    observed_values = {
        normalize_clinic_name(record.get(key))
        for key in (
            "clinic_archetype",
            "patient_category",
            "category",
            "venue_type",
            "business_type",
        )
        if normalize_clinic_name(record.get(key))
    }
    evidence_status = _clean(
        record.get("clinic_archetype_evidence_status")
        or record.get("category_evidence_status")
        or record.get("business_type_evidence_status")
    ).casefold()
    authoritative = any(
        word in evidence_status for word in ("confirmed", "verified", "authoritative")
    )
    if authoritative and expected_values & observed_values:
        return "confirmed"
    if authoritative and record.get("category_list_complete") and not (expected_values & observed_values):
        return "irrelevant"
    return "unresolved"


def _record_trading_status(record: Mapping) -> str:
    explicit = record.get("active")
    status = _clean(
        record.get("active_trading_status")
        or record.get("trading_status")
        or record.get("business_status")
        or record.get("status")
    ).casefold()
    if explicit is True or status in {"active", "trading", "operational", "open"}:
        return "active"
    if explicit is False or status in _INACTIVE:
        return "inactive"
    return "unresolved"


def build_competitor_validations(
    scope: Mapping,
    candidates: Iterable[Mapping],
    clinic_database: object,
    *,
    external_evidence: object = None,
) -> dict:
    """Resolve candidates against local identity, area, service, and trading evidence.

    Sparse legacy database rows can confirm identity, but they deliberately do
    not imply location, treatment relevance, or active trading.  Those checks
    stay unresolved until the corresponding authoritative field or supplied
    external evidence exists.
    """

    database = _as_database_rows(clinic_database)
    clinic = scope.get("clinic") if isinstance(scope.get("clinic"), Mapping) else {}
    client_names = {
        normalize_clinic_name(clinic.get("name")),
        *(normalize_clinic_name(alias) for alias in clinic.get("verified_aliases") or []),
    } - {""}
    rows = []
    for candidate in candidates:
        name = _clean(candidate.get("canonical_name") or candidate.get("name"))
        if not name:
            continue
        key = normalize_clinic_name(name)
        matches = [record for record in database if key in _record_names(record)]
        external = _external_lookup(external_evidence, candidate)
        record = matches[0] if len(matches) == 1 else {}
        db_id = _clean(record.get("uid") or record.get("account_id") or record.get("entity_id"))
        identity = "confirmed" if len(matches) == 1 else "unresolved"
        duplication = "unique" if len(matches) == 1 else "unresolved"
        if key in client_names or db_id and db_id == _clean(clinic.get("account_id")):
            duplication = "duplicate_of_client"
        elif len(matches) > 1:
            duplication = "branch_duplicate"
        geography = _record_area_status(scope, record) if record else "unresolved"
        subject = _record_subject_status(scope, record) if record else "unresolved"
        trading = _record_trading_status(record) if record else "unresolved"

        # Founder/analyst verified external evidence may resolve fields that the
        # local database does not carry.  It never silently weakens an explicit
        # local exclusion.
        identity = _external_status(
            external,
            identity,
            "identity_status",
            automatic_allowed=frozenset({"confirmed", "verified", "resolved"}),
        )
        duplication = _external_status(
            external,
            duplication,
            "alias_duplication_status",
            automatic_allowed=frozenset({"unique"}),
        )
        geography = _external_status(
            external,
            geography,
            "geography_status",
            automatic_allowed=frozenset({"confirmed", "verified", "relevant"}),
        )
        subject = _external_status(
            external,
            subject,
            "subject_relevance_status",
            "clinic_archetype_status",
            "treatment_status",
            "treatment_relevance_status",
            automatic_allowed=frozenset({"confirmed", "verified", "relevant"}),
        )
        trading = _external_status(
            external,
            trading,
            "active_trading_status",
            automatic_allowed=frozenset({"active", "inactive", "closed", "not_trading"}),
        )
        # Preserve a canonical database ID whenever there is one exact match.
        # Stable web IDs exist for database-absent entities; they must not
        # replace the local canonical identity.
        resolved_id = _clean(
            db_id
            or external.get("resolved_entity_id")
            or external.get("entity_id")
        ) or stable_external_entity_id(name)
        reconciled_name = _clean(external.get("canonical_name"))
        rows.append(
            {
                "entity_id": _clean(candidate.get("entity_id")) or stable_external_entity_id(name),
                "resolved_entity_id": resolved_id,
                "canonical_name": (
                    _clean(record.get("clinic_name") or record.get("name"))
                    or reconciled_name
                    or name
                ),
                "identity_status": identity,
                "alias_duplication_status": duplication,
                "geography_status": geography,
                "subject_kind": _clean(scope.get("subject_kind") or "treatment").casefold(),
                "subject_relevance_status": subject,
                # Compatibility field consumed by v2 review artifacts.
                "treatment_status": subject,
                "active_trading_status": trading,
                "database_match_count": len(matches),
                "database_record_id": db_id or None,
                "authoritative_references": deepcopy(
                    list(external.get("authoritative_references") or record.get("source_urls") or [])
                ),
            }
        )
    return {"scope_id": scope.get("scope_id"), "candidates": rows}


def _metric_numerator(candidate: Mapping, name: str, fallback: str) -> int:
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), Mapping) else {}
    metric = metrics.get(name) if isinstance(metrics.get(name), Mapping) else {}
    return int(metric.get("numerator") or candidate.get(fallback) or 0)


def rank_competitor_candidates(
    candidates: Iterable[Mapping],
    *,
    clinic: Optional[Mapping] = None,
) -> tuple[list[dict], list[dict]]:
    """Freeze the exact-scope ranking and exclude the client/verified aliases."""

    clinic = clinic or {}
    self_ids = {_clean(clinic.get("account_id")), _clean(clinic.get("entity_id"))} - {""}
    self_names = {
        normalize_clinic_name(clinic.get("name")),
        *(normalize_clinic_name(alias) for alias in clinic.get("verified_aliases") or []),
    } - {""}
    ranked: list[dict] = []
    excluded: list[dict] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for raw in candidates:
        candidate = deepcopy(dict(raw))
        name = _clean(candidate.get("canonical_name") or candidate.get("name"))
        if not name:
            continue
        name_key = normalize_clinic_name(name)
        entity_id = _clean(candidate.get("entity_id")) or stable_external_entity_id(name)
        candidate["entity_id"] = entity_id
        candidate["canonical_name"] = name
        if entity_id in self_ids or name_key in self_names:
            candidate.update({"review_status": "excluded", "exclusion_reason": "client_or_verified_alias"})
            excluded.append(candidate)
            continue
        if entity_id in seen_ids or name_key in seen_names:
            candidate.update({"review_status": "excluded", "exclusion_reason": "duplicate_candidate"})
            excluded.append(candidate)
            continue
        seen_ids.add(entity_id)
        seen_names.add(name_key)
        candidate["appearance_count"] = _metric_numerator(candidate, "visibility", "appearance_count")
        candidate["qualified_recommendation_count"] = _metric_numerator(
            candidate, "qualified_recommendation", "qualified_recommendation_count"
        )
        candidate["first_named_count"] = _metric_numerator(candidate, "first_named", "first_named_count")
        if candidate["appearance_count"] <= 0:
            continue
        ranked.append(candidate)
    ranked.sort(
        key=lambda row: (
            -row["appearance_count"],
            -row["qualified_recommendation_count"],
            -row["first_named_count"],
            row["canonical_name"].casefold(),
            row["canonical_name"],
        )
    )
    for index, row in enumerate(ranked, 1):
        row["frozen_rank"] = index
    return ranked, excluded


def _validation_lookup(validations: object) -> dict[str, Mapping]:
    if validations is None:
        return {}
    rows = []
    if isinstance(validations, Mapping):
        if isinstance(validations.get("candidates"), list):
            rows = validations["candidates"]
        else:
            for key, value in validations.items():
                if isinstance(value, Mapping):
                    rows.append({"_lookup_key": key, **value})
    elif isinstance(validations, list):
        rows = validations
    else:
        raise CompetitorReviewError("competitor validations must be a mapping or list")
    lookup: dict[str, Mapping] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for value in (
            row.get("entity_id"),
            row.get("canonical_name"),
            row.get("name"),
            row.get("_lookup_key"),
        ):
            key = normalize_clinic_name(value)
            if key:
                lookup[key] = row
            raw = _clean(value)
            if raw:
                lookup[raw] = row
    return lookup


def _candidate_validation(candidate: Mapping, lookup: Mapping[str, Mapping]) -> dict:
    source = (
        lookup.get(_clean(candidate.get("entity_id")))
        or lookup.get(normalize_clinic_name(candidate.get("canonical_name")))
        or candidate.get("verification")
        or {}
    )
    if not isinstance(source, Mapping):
        source = {}
    overall = _clean(source.get("review_status") or source.get("status")).casefold()
    identity = _clean(source.get("identity_status") or source.get("entity_identity_status")).casefold()
    duplicate = _clean(source.get("alias_duplication_status") or source.get("duplication_status")).casefold()
    geography = _clean(source.get("geography_status") or source.get("geographic_relevance_status")).casefold()
    subject = _clean(
        source.get("subject_relevance_status")
        or source.get("clinic_archetype_status")
        or source.get("treatment_status")
        or source.get("treatment_relevance_status")
    ).casefold()
    trading = _clean(source.get("active_trading_status") or source.get("trading_status")).casefold()

    if overall == "confirmed":
        identity = identity or "confirmed"
        duplicate = duplicate or "unique"
        geography = geography or "confirmed"
        subject = subject or "confirmed"
        trading = trading or "active"
    exclusion_reasons: list[str] = []
    if overall == "excluded":
        exclusion_reasons.append(_clean(source.get("reason")) or "manually_excluded")
    if duplicate in _DUPLICATE:
        exclusion_reasons.append("alias_or_branch_duplicate")
    if geography in _IRRELEVANT:
        exclusion_reasons.append("geographically_irrelevant")
    if subject in _IRRELEVANT:
        exclusion_reasons.append("subject_irrelevant")
    if trading in _INACTIVE:
        exclusion_reasons.append("not_actively_trading")

    required = {
        "identity": identity in _CONFIRMED,
        "alias_duplication": duplicate in _CONFIRMED,
        "geography": geography in _CONFIRMED,
        "subject": subject in _CONFIRMED,
        # Compatibility key retained for v2 callers and stored reviews.
        "treatment": subject in _CONFIRMED,
        "active_trading": trading in _CONFIRMED,
    }
    if exclusion_reasons:
        review_status = "excluded"
    elif overall == "unresolved" or not all(required.values()):
        review_status = "unresolved"
    else:
        review_status = "confirmed"
    return {
        "review_status": review_status,
        "entity_identity": identity or "unresolved",
        "alias_duplication": duplicate or "unresolved",
        "geographic_relevance": geography or "unresolved",
        "subject_relevance": subject or "unresolved",
        "treatment_relevance": subject or "unresolved",
        "active_trading": trading or "unresolved",
        "checks": required,
        "exclusion_reasons": exclusion_reasons,
        "authoritative_references": deepcopy(list(source.get("authoritative_references") or [])),
        "reviewer": source.get("reviewer"),
        "reviewed_at": source.get("reviewed_at"),
        "resolved_entity_id": source.get("resolved_entity_id"),
    }


def build_competitor_review(
    scope_id: str,
    candidates: Iterable[Mapping],
    *,
    clinic: Optional[Mapping] = None,
    validations: object = None,
    limit: int = 3,
) -> dict:
    """Validate and backfill the top three from the frozen candidate ranking."""

    if limit < 1:
        raise CompetitorReviewError("competitor limit must be positive")
    ranked, initially_excluded = rank_competitor_candidates(candidates, clinic=clinic)
    lookup = _validation_lookup(validations)
    selected: list[dict] = []
    excluded = initially_excluded
    unresolved: list[dict] = []
    reviewed: list[dict] = []
    resolved_entities: dict[str, dict] = {}
    for candidate in ranked:
        row = deepcopy(candidate)
        row["verification"] = _candidate_validation(row, lookup)
        if row["verification"].get("resolved_entity_id"):
            row["source_entity_id"] = row["entity_id"]
            row["entity_id"] = str(row["verification"]["resolved_entity_id"])
        row["review_status"] = row["verification"]["review_status"]
        row["name"] = row["canonical_name"]
        resolved_key = str(row.get("entity_id") or "").strip()
        if row["review_status"] == "confirmed" and resolved_key in resolved_entities:
            original = resolved_entities[resolved_key]
            row["review_status"] = "excluded"
            row["verification"]["review_status"] = "excluded"
            row["verification"]["exclusion_reasons"] = [
                *row["verification"].get("exclusion_reasons", []),
                "alias_or_branch_duplicate",
            ]
            row["alias_of_entity_id"] = original["entity_id"]
            row["alias_of_name"] = original["canonical_name"]
        elif row["review_status"] == "confirmed":
            resolved_entities[resolved_key] = row
        reviewed.append(row)
        if row["review_status"] == "confirmed" and len(selected) < limit:
            selected.append(row)
        elif row["review_status"] == "excluded":
            row["exclusion_reason"] = ",".join(row["verification"]["exclusion_reasons"])
            excluded.append(row)
        elif row["review_status"] == "unresolved":
            unresolved.append(row)

    cutoff_rank = selected[-1]["frozen_rank"] if len(selected) == limit else float("inf")
    blocking = [row for row in unresolved if row["frozen_rank"] < cutoff_rank]
    if len(selected) < limit:
        blocking = list(unresolved)
    selected_ids = {row["entity_id"] for row in selected}
    # A confirmed candidate below the selected cutoff is retained in the audit
    # trail but not exposed as one of the focused three.
    for row in reviewed:
        if row["review_status"] == "confirmed" and row["entity_id"] not in selected_ids:
            row["selection_status"] = "confirmed_below_cutoff"
        elif row["entity_id"] in selected_ids:
            row["selection_status"] = "selected"

    return {
        "schema_version": COMPETITOR_REVIEW_SCHEMA_VERSION,
        "scope_id": str(scope_id),
        "limit": limit,
        "ranking_frozen": True,
        "ranked_candidates": reviewed,
        "selected_competitors": selected,
        "excluded_candidates": excluded,
        "unresolved_exceptions": unresolved,
        "blocking_unresolved_candidates": blocking,
        "relevant_comparator_identity_resolved": not blocking,
        "geographic_subject_relevance_confirmed": not blocking
        and all(
            row["verification"]["checks"]["geography"]
            and row["verification"]["checks"]["subject"]
            for row in selected
        ),
        # Compatibility alias for v2 evidence gates.
        "geographic_treatment_relevance_confirmed": not blocking
        and all(
            row["verification"]["checks"]["geography"]
            and row["verification"]["checks"]["subject"]
            for row in selected
        ),
    }


def build_competitor_reviews(
    scoped_candidates: Mapping[str, Iterable[Mapping]],
    *,
    clinic: Optional[Mapping] = None,
    validations: object = None,
    limit: int = 3,
) -> dict:
    scopes = []
    for scope_id, candidates in scoped_candidates.items():
        scope_validations = None
        if isinstance(validations, Mapping):
            scope_validations = validations.get(scope_id)
            if scope_validations is None and isinstance(validations.get("scopes"), Mapping):
                scope_validations = validations["scopes"].get(scope_id)
        scopes.append(
            build_competitor_review(
                scope_id,
                candidates,
                clinic=clinic,
                validations=scope_validations,
                limit=limit,
            )
        )
    return {"schema_version": COMPETITOR_REVIEW_SCHEMA_VERSION, "scopes": scopes}
