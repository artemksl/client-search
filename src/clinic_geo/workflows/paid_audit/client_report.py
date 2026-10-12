"""Pure document model for the editable paid-audit client report.

The Node engine remains the source of the operator report, while optional
Python page-audit results supply advisory observations.  This module only
turns those structured inputs into stable editable clauses and applies
user-requested edits without performing any I/O.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping, Sequence


CLIENT_REPORT_SCHEMA_VERSION = "client-report-document-1"
REMOVED_METHOD_LIMITATION = (
    "Candidate findings require separate human proof and complete-issue approval "
    "before client issuance."
)
_DETERMINISTIC_INFERENCE = {
    "deterministic",
    "deterministic_aggregate_over_validated_inputs",
}
_EXECUTIVE_SUMMARY_LABELS = (
    ("pages_tested", "Pages tested"),
    ("known_pages_in_scope", "Known pages in scope"),
    ("pages_selected", "Pages selected"),
    ("pages_collected", "Pages collected"),
    ("pages_rendered", "Pages rendered"),
    ("page_acquisition_failures", "Page acquisition failures"),
    ("intentionally_unselected_pages", "Intentionally unselected pages"),
    ("assessment_records", "Assessment records"),
    ("unresolved_records", "Unresolved records"),
    ("ai_answer_runs", "AI answer runs"),
    ("verified_machine_failures", "Verified failures"),
)
_EXECUTIVE_SUMMARY_EXCLUDED = frozenset(
    {"known_pages_discovered", "issue_candidates_pending_human_review"}
)
_EXECUTIVE_SUMMARY_FIELDS = frozenset(
    field for field, _label in _EXECUTIVE_SUMMARY_LABELS
) | _EXECUTIVE_SUMMARY_EXCLUDED
_VISIBLE_METRIC_FIELDS = (
    "instrument_compatible",
    "eligible_answer_runs",
    "excluded_answer_runs",
    "appearance",
    "raw_share_of_voice",
    "cited_domains",
)
_QUALIFIED_RECOMMENDATION_FIELD = "qualified_recommendation_rate"
_METRIC_FIELDS = frozenset(
    (*_VISIBLE_METRIC_FIELDS, _QUALIFIED_RECOMMENDATION_FIELD)
)
_UNRESOLVED_FIELDS = frozenset(
    {"assessment_record_id", "status", "reason", "unblock"}
)
_PILOT_SCOPE_REASON = "listing capture not in pilot scope"
_ADVISORY_FIELDS = frozenset(
    {
        "results_schema_version",
        "run",
        "provenance",
        "advisory_notice",
        "pages",
        "c05_aggregate",
        "review_records",
        "excluded_pages",
        "results_sha256",
    }
)


class ClientReportError(ValueError):
    """The editable document or requested clause operation is invalid."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "client_report_error",
        clause_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.clause_id = clause_id


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _human_label(value: Any) -> str:
    return str(value or "").replace("_", " ").strip().capitalize()


def _inline_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Mapping):
        parts = []
        for key, nested in value.items():
            rendered = _inline_text(nested)
            if rendered:
                parts.append(f"{_human_label(key)}: {rendered}")
        return "; ".join(parts)
    if _is_sequence(value):
        return ", ".join(
            rendered for item in value if (rendered := _inline_text(item))
        )
    return str(value).strip()


def _block_text(value: Any) -> str:
    """Render exported structured values as deterministic, editable plain text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        lines = []
        for key, nested in value.items():
            if nested in (None, "", [], {}):
                continue
            if isinstance(nested, Mapping) or _is_sequence(nested):
                rendered = _inline_text(nested)
            else:
                rendered = _inline_text(nested)
            if rendered:
                lines.append(f"{_human_label(key)}: {rendered}")
        return "\n".join(lines)
    if _is_sequence(value):
        return "\n".join(
            f"- {rendered}"
            for item in value
            if (rendered := _inline_text(item))
        )
    return _inline_text(value)


def _section_text(title: str, value: Any) -> str:
    rendered = _block_text(value)
    return f"{title}\n{rendered}" if rendered else ""


def _require_exact_fields(
    value: Mapping,
    expected: frozenset[str],
    *,
    contract: str,
    code: str,
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unexpected = sorted(str(field) for field in actual - expected)
    details = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unexpected:
        details.append("unexpected " + ", ".join(unexpected))
    raise ClientReportError(
        f"The {contract} fields do not match the producing contract ({'; '.join(details)})",
        code=code,
    )


def _compose_executive_summary(value: Any) -> tuple[str, dict]:
    if not isinstance(value, Mapping):
        raise ClientReportError(
            "The operator report executive_summary value must be an object",
            code="invalid_executive_summary",
        )
    _require_exact_fields(
        value,
        _EXECUTIVE_SUMMARY_FIELDS,
        contract="executive_summary",
        code="invalid_executive_summary",
    )
    lines = [
        f"{label}: {_inline_text(value[field])}".rstrip()
        for field, label in _EXECUTIVE_SUMMARY_LABELS
    ]
    scaffolding = {
        "issue_candidates_pending_human_review": deepcopy(
            value["issue_candidates_pending_human_review"]
        )
    }
    return "\n".join(lines), scaffolding


def _compose_metrics(value: Any) -> tuple[str, dict]:
    if value is None:
        return "", {}
    if not isinstance(value, Mapping):
        raise ClientReportError(
            "The operator report metrics value must be an object or null",
            code="invalid_metrics",
        )
    _require_exact_fields(
        value,
        _METRIC_FIELDS,
        contract="metrics",
        code="invalid_metrics",
    )
    visible = {field: deepcopy(value[field]) for field in _VISIBLE_METRIC_FIELDS}
    scaffolding = {
        _QUALIFIED_RECOMMENDATION_FIELD: deepcopy(
            value[_QUALIFIED_RECOMMENDATION_FIELD]
        )
    }
    return _block_text(visible), scaffolding


def _compose_unresolved_items(value: Any) -> tuple[str, list[dict]]:
    if value is None:
        return "", []
    if not _is_sequence(value):
        raise ClientReportError(
            "The operator report unresolved_items value must be a list",
            code="invalid_unresolved_items",
        )
    visible_rows = []
    scaffolding = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ClientReportError(
                "The operator report contains a non-object unresolved item",
                code="invalid_unresolved_items",
            )
        _require_exact_fields(
            row,
            _UNRESOLVED_FIELDS,
            contract="unresolved item",
            code="invalid_unresolved_items",
        )
        if row["reason"] == _PILOT_SCOPE_REASON:
            visible_rows.append(
                {
                    "assessment_record_id": deepcopy(row["assessment_record_id"]),
                    "unblock": deepcopy(row["unblock"]),
                }
            )
            scaffolding.append(
                {
                    "assessment_record_id": deepcopy(row["assessment_record_id"]),
                    "status": deepcopy(row["status"]),
                    "reason": deepcopy(row["reason"]),
                }
            )
        else:
            visible_rows.append(
                {
                    "assessment_record_id": deepcopy(row["assessment_record_id"]),
                    "status": deepcopy(row["status"]),
                    "reason": deepcopy(row["reason"]),
                    "unblock": deepcopy(row["unblock"]),
                }
            )
    return _block_text(visible_rows), scaffolding


def compose_clause_wording(finding: Mapping) -> str:
    """Compose one finding clause in a fixed, reproducible field order."""

    if not isinstance(finding, Mapping):
        raise ClientReportError(
            "A candidate finding must be a mapping",
            code="invalid_candidate_finding",
        )

    blocks: list[str] = []
    title = _block_text(finding.get("title"))
    if title:
        blocks.append(title)
    proof = _block_text(finding.get("proof_summary"))
    if proof:
        blocks.append(proof)

    ordered_sections = (
        ("What good looks like", "required_corrected_state"),
        ("How to fix", "permitted_approaches"),
        ("Constraints", "constraints"),
        ("Risks", "risks"),
        ("Dependencies", "dependencies"),
        ("Owner", "owner_categories"),
        ("How to verify", "acceptance_test"),
    )
    for heading, field in ordered_sections:
        block = _section_text(heading, finding.get(field))
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _catalogue_points(analysis_catalogue: Mapping) -> dict[str, Mapping]:
    points = analysis_catalogue.get("points") if isinstance(analysis_catalogue, Mapping) else None
    if not _is_sequence(points):
        raise ClientReportError(
            "The analysis catalogue has no points list",
            code="analysis_catalogue_mismatch",
        )
    by_id: dict[str, Mapping] = {}
    for point in points:
        if not isinstance(point, Mapping):
            raise ClientReportError(
                "The analysis catalogue contains a non-object point",
                code="analysis_catalogue_mismatch",
            )
        point_id = str(point.get("id") or "").strip()
        if not point_id or point_id in by_id:
            raise ClientReportError(
                "The analysis catalogue contains a missing or duplicate point id",
                code="analysis_catalogue_mismatch",
            )
        by_id[point_id] = point
    return by_id


def _finding_point(finding: Mapping, points: Mapping[str, Mapping]) -> Mapping:
    template_id = str(finding.get("template_id") or "").strip()
    check_ids_value = finding.get("source_check_ids")
    check_ids = (
        [str(value) for value in check_ids_value]
        if _is_sequence(check_ids_value)
        else []
    )
    candidates = [value for value in (template_id, *check_ids) if value]
    for candidate in candidates:
        if candidate in points:
            return points[candidate]
    reference = candidates[0] if candidates else "(missing)"
    raise ClientReportError(
        f"Candidate finding references unknown analysis point {reference!r}",
        code="analysis_catalogue_mismatch",
    )


def _new_clause(
    *,
    clause_id: str,
    section_id: str,
    position: int,
    origin: str,
    source_ref: Mapping,
    tags: Mapping,
    derived_counts: bool,
    wording: str,
) -> dict:
    return {
        "clause_id": clause_id,
        "section_id": section_id,
        "position": position,
        "origin": origin,
        "source_ref": deepcopy(dict(source_ref)),
        "tags": deepcopy(dict(tags)),
        "derived_counts": bool(derived_counts),
        "wording": wording,
        "not_checked": True,
        "removed": False,
        "edited_at": None,
        "clause_set_revision_at_edit": 0,
    }


def _advisory_clauses(
    advisory_results: Mapping | None,
    *,
    client_id: str,
    engagement_id: str,
    run_id: str,
) -> tuple[dict | None, list[dict]]:
    if advisory_results is None:
        return None, []
    if not isinstance(advisory_results, Mapping):
        raise ClientReportError(
            "The advisory results must be an object or null",
            code="invalid_advisory_results",
        )
    _require_exact_fields(
        advisory_results,
        _ADVISORY_FIELDS,
        contract="advisory results",
        code="invalid_advisory_results",
    )
    results_schema_version = advisory_results["results_schema_version"]
    results_sha256 = advisory_results["results_sha256"]
    if not isinstance(results_schema_version, str) or not results_schema_version.strip():
        raise ClientReportError(
            "The advisory results schema version is missing",
            code="invalid_advisory_results",
        )
    if not isinstance(results_sha256, str) or not results_sha256.strip():
        raise ClientReportError(
            "The advisory results digest is missing",
            code="invalid_advisory_results",
        )
    run = advisory_results["run"]
    if not isinstance(run, Mapping):
        raise ClientReportError(
            "The advisory results run identity must be an object",
            code="invalid_advisory_results",
        )
    _require_exact_fields(
        run,
        frozenset({"client_id", "engagement_id", "run_id"}),
        contract="advisory run identity",
        code="invalid_advisory_results",
    )
    advisory_identity = tuple(
        str(run[field] or "").strip()
        for field in ("client_id", "engagement_id", "run_id")
    )
    if advisory_identity != (client_id, engagement_id, run_id):
        raise ClientReportError(
            "The advisory results identity does not match the operator report",
            code="invalid_advisory_results",
        )
    pages = advisory_results["pages"]
    if not isinstance(pages, Mapping):
        raise ClientReportError(
            "The advisory results pages value must be an object",
            code="invalid_advisory_results",
        )

    observations: list[tuple[str, str, Mapping]] = []
    for page_id, page in pages.items():
        if not isinstance(page_id, str) or not page_id.strip():
            raise ClientReportError(
                "An advisory page id is missing or invalid",
                code="invalid_advisory_results",
            )
        if not isinstance(page, Mapping):
            raise ClientReportError(
                "An advisory page result must be an object",
                code="invalid_advisory_results",
            )
        results = page.get("results")
        if not isinstance(results, Mapping):
            raise ClientReportError(
                "An advisory page has no results object",
                code="invalid_advisory_results",
            )
        for check_id, result in results.items():
            if not isinstance(check_id, str) or not check_id.strip():
                raise ClientReportError(
                    "An advisory check id is missing or invalid",
                    code="invalid_advisory_results",
                )
            if not isinstance(result, Mapping):
                raise ClientReportError(
                    "An advisory check result must be an object",
                    code="invalid_advisory_results",
                )
            if result.get("state") == "issue":
                observations.append((page_id, check_id, page))

    clauses = []
    for position, (page_id, check_id, page) in enumerate(
        sorted(observations, key=lambda item: (item[0], item[1]))
    ):
        primary_url = page.get("primary_url")
        if not isinstance(primary_url, str) or not primary_url.strip():
            raise ClientReportError(
                "An advisory issue has no primary page URL",
                code="invalid_advisory_results",
            )
        result = page["results"][check_id]
        note = result.get("note")
        if note is not None and not isinstance(note, str):
            raise ClientReportError(
                "An advisory issue note must be text or null",
                code="invalid_advisory_results",
            )
        evidence = result.get("evidence")
        if not _is_sequence(evidence):
            raise ClientReportError(
                "An advisory issue has no evidence list",
                code="invalid_advisory_results",
            )
        body = note.strip() if isinstance(note, str) and note.strip() else ""
        if not body:
            quotes = []
            for item in evidence:
                if not isinstance(item, Mapping):
                    continue
                quote = item.get("quote")
                if isinstance(quote, str) and quote.strip() and quote not in quotes:
                    quotes.append(quote)
            body = "\n".join(quotes)
        if not body:
            raise ClientReportError(
                "An advisory issue has neither a note nor a grounded evidence quote",
                code="invalid_advisory_results",
            )
        digest = hashlib.sha256(
            f"{page_id}\0{check_id}".encode("utf-8")
        ).hexdigest()[:24]
        clauses.append(
            _new_clause(
                clause_id=f"advisory-{digest}",
                section_id="ai-brief",
                position=position,
                origin="ai",
                source_ref={
                    "page_id": page_id,
                    "check_id": check_id,
                    "results_sha256": results_sha256,
                },
                tags={},
                derived_counts=False,
                wording=(
                    f"Page\n{primary_url.strip()}\n\n"
                    f"Observation\n{body}"
                ),
            )
        )
    return (
        {
            "results_schema_version": results_schema_version,
            "results_sha256": results_sha256,
        },
        clauses,
    )


def build_document(
    report: Mapping,
    *,
    analysis_catalogue: Mapping,
    source: Mapping,
    now: str,
    advisory_results: Mapping | None = None,
) -> dict:
    """Build the first editable document deterministically from exported data."""

    if not isinstance(report, Mapping):
        raise ClientReportError("The operator report must be a mapping", code="invalid_report")
    if not isinstance(source, Mapping):
        raise ClientReportError("The source record must be a mapping", code="invalid_source")
    timestamp = str(now or "").strip()
    if not timestamp:
        raise ClientReportError("A document timestamp is required", code="invalid_timestamp")

    client_id = str(report.get("client_id") or "").strip()
    engagement_id = str(report.get("engagement_id") or "").strip()
    run_id = str(report.get("run_id") or "").strip()
    if not all((client_id, engagement_id, run_id)):
        raise ClientReportError(
            "The operator report identity is incomplete",
            code="invalid_report_identity",
        )

    points = _catalogue_points(analysis_catalogue)
    advisory_source, advisory_clauses = _advisory_clauses(
        advisory_results,
        client_id=client_id,
        engagement_id=engagement_id,
        run_id=run_id,
    )
    sections: list[dict] = []
    clauses: list[dict] = []

    def add_section(section_id: str, title: str) -> None:
        sections.append(
            {"section_id": section_id, "title": title, "position": len(sections)}
        )

    def add_fixed(
        clause_id: str,
        section_id: str,
        section_title: str,
        wording: str,
        *,
        derived_counts: bool,
        source_fields: Sequence[str],
        tags: Mapping | None = None,
    ) -> None:
        if not wording:
            return
        add_section(section_id, section_title)
        clauses.append(
            _new_clause(
                clause_id=clause_id,
                section_id=section_id,
                position=0,
                origin="deterministic",
                source_ref={"report_fields": list(source_fields)},
                tags=tags or {},
                derived_counts=derived_counts,
                wording=wording,
            )
        )

    executive_summary, executive_scaffolding = _compose_executive_summary(
        report.get("executive_summary")
    )
    metrics, metrics_scaffolding = _compose_metrics(report.get("metrics"))
    summary_parts = [executive_summary]
    if metrics:
        summary_parts.append(f"Key figures\n{metrics}")
    summary_scaffolding = {"executive_summary": executive_scaffolding}
    if metrics_scaffolding:
        summary_scaffolding["metrics"] = metrics_scaffolding
    add_fixed(
        "summary",
        "summary",
        "Executive summary",
        "\n\n".join(summary_parts),
        derived_counts=True,
        source_fields=("executive_summary", "metrics"),
        tags={"source_scaffolding": summary_scaffolding},
    )

    findings_value = report.get("candidate_findings")
    if findings_value is None:
        findings_value = []
    if not _is_sequence(findings_value):
        raise ClientReportError(
            "The operator report candidate_findings value must be a list",
            code="invalid_report",
        )
    finding_clauses = []
    seen_ids: set[str] = set()
    for position, finding in enumerate(findings_value):
        if not isinstance(finding, Mapping):
            raise ClientReportError(
                "The operator report contains a non-object candidate finding",
                code="invalid_report",
            )
        candidate_id = str(finding.get("candidate_id") or "").strip()
        if not candidate_id or candidate_id in seen_ids:
            raise ClientReportError(
                "Candidate finding ids must be present and unique",
                code="invalid_report",
            )
        seen_ids.add(candidate_id)
        point = _finding_point(finding, points)
        inference = deepcopy(point.get("infer"))
        origin = "deterministic" if inference in _DETERMINISTIC_INFERENCE else "ai"
        check_ids_value = finding.get("source_check_ids")
        check_ids = (
            [str(value) for value in check_ids_value]
            if _is_sequence(check_ids_value)
            else []
        )
        finding_clauses.append(
            _new_clause(
                clause_id=candidate_id,
                section_id="findings",
                position=position,
                origin=origin,
                source_ref={
                    "candidate_id": candidate_id,
                    "check_ids": check_ids,
                    "template_id": finding.get("template_id"),
                    "template_version": finding.get("template_version"),
                },
                tags={
                    "rev": deepcopy(point.get("rev")),
                    "infer": inference,
                    "conf_max": deepcopy(point.get("conf_max")),
                    "issue_creation": deepcopy(point.get("issue_creation")),
                    "provisional_severity": finding.get("provisional_severity"),
                },
                derived_counts=False,
                wording=compose_clause_wording(finding),
            )
        )
    if finding_clauses:
        add_section("findings", "What we found")
        clauses.extend(finding_clauses)

    add_fixed(
        "coverage",
        "coverage",
        "Audit coverage",
        _block_text(report.get("coverage_by_layer")),
        derived_counts=True,
        source_fields=("coverage_by_layer",),
    )
    unresolved_wording, unresolved_scaffolding = _compose_unresolved_items(
        report.get("unresolved_items")
    )
    add_fixed(
        "unresolved",
        "unresolved",
        "Items not assessed",
        unresolved_wording,
        derived_counts=True,
        source_fields=("unresolved_items",),
        tags=(
            {"source_scaffolding": {"unresolved_items": unresolved_scaffolding}}
            if unresolved_scaffolding
            else None
        ),
    )

    limitations_value = report.get("method_limitations")
    if limitations_value is None:
        limitations = []
    elif _is_sequence(limitations_value):
        limitations = [
            item for item in limitations_value if item != REMOVED_METHOD_LIMITATION
        ]
    else:
        limitations = (
            [] if limitations_value == REMOVED_METHOD_LIMITATION else [limitations_value]
        )
    add_fixed(
        "limitations",
        "limitations",
        "Method and limitations",
        _block_text(limitations),
        derived_counts=False,
        source_fields=("method_limitations",),
    )
    add_fixed(
        "evidence-appendix",
        "evidence-appendix",
        "Evidence appendix",
        _block_text(report.get("evidence_appendix")),
        derived_counts=True,
        source_fields=("evidence_appendix",),
    )
    if advisory_clauses:
        add_section("ai-brief", "Additional observations")
        clauses.extend(advisory_clauses)

    return {
        "schema_version": CLIENT_REPORT_SCHEMA_VERSION,
        "client_id": client_id,
        "engagement_id": engagement_id,
        "run_id": run_id,
        "source": deepcopy(dict(source)),
        "advisory_source": deepcopy(advisory_source),
        "created_at": timestamp,
        "updated_at": timestamp,
        "sections": sections,
        "clauses": clauses,
        "clause_set_revision": 0,
    }


def _copy_with_clause(document: Mapping, clause_id: str) -> tuple[dict, dict]:
    if not isinstance(document, Mapping):
        raise ClientReportError("The client report document must be a mapping")
    copied = deepcopy(dict(document))
    clauses = copied.get("clauses")
    if not isinstance(clauses, list):
        raise ClientReportError("The client report has no clause list", code="invalid_document")
    target_id = str(clause_id or "")
    for clause in clauses:
        if isinstance(clause, dict) and clause.get("clause_id") == target_id:
            return copied, clause
    raise ClientReportError(
        f"Unknown client-report clause {target_id!r}",
        code="clause_not_found",
        clause_id=target_id,
    )


def edit_clause(document: Mapping, clause_id: str, wording: str, *, now: str) -> dict:
    copied, clause = _copy_with_clause(document, clause_id)
    if clause.get("removed") is True:
        raise ClientReportError(
            "Bring the clause back before editing it",
            code="clause_removed",
            clause_id=str(clause_id),
        )
    if not isinstance(wording, str):
        raise ClientReportError("Clause wording must be text", code="invalid_wording")
    clause["wording"] = wording
    clause["not_checked"] = False
    clause["edited_at"] = str(now)
    clause["clause_set_revision_at_edit"] = int(
        copied.get("clause_set_revision") or 0
    )
    copied["updated_at"] = str(now)
    return copied


def set_clause_not_checked(
    document: Mapping,
    clause_id: str,
    value: bool,
    *,
    now: str,
) -> dict:
    if not isinstance(value, bool):
        raise ClientReportError(
            "Clause not-checked state must be true or false",
            code="invalid_not_checked",
            clause_id=str(clause_id),
        )
    copied, clause = _copy_with_clause(document, clause_id)
    clause["not_checked"] = value
    copied["updated_at"] = str(now)
    return copied


def remove_clause(document: Mapping, clause_id: str, *, now: str) -> dict:
    copied, clause = _copy_with_clause(document, clause_id)
    if clause.get("removed") is not True:
        clause["removed"] = True
        copied["clause_set_revision"] = int(
            copied.get("clause_set_revision") or 0
        ) + 1
    copied["updated_at"] = str(now)
    return copied


def restore_clause(document: Mapping, clause_id: str, *, now: str) -> dict:
    copied, clause = _copy_with_clause(document, clause_id)
    if clause.get("removed") is True:
        clause["removed"] = False
        copied["clause_set_revision"] = int(
            copied.get("clause_set_revision") or 0
        ) + 1
    copied["updated_at"] = str(now)
    return copied


def render_model(document: Mapping) -> dict:
    """Return the ordered, client-visible view; removed clauses vanish here."""

    if not isinstance(document, Mapping):
        raise ClientReportError("The client report document must be a mapping")
    sections_value = document.get("sections")
    clauses_value = document.get("clauses")
    if not isinstance(sections_value, list) or not isinstance(clauses_value, list):
        raise ClientReportError("The client report document is malformed", code="invalid_document")
    revision = int(document.get("clause_set_revision") or 0)
    grouped = []
    for section in sorted(
        (item for item in sections_value if isinstance(item, Mapping)),
        key=lambda item: int(item.get("position") or 0),
    ):
        section_id = section.get("section_id")
        visible = []
        for clause in sorted(
            (
                item
                for item in clauses_value
                if isinstance(item, Mapping)
                and item.get("section_id") == section_id
                and item.get("removed") is not True
            ),
            key=lambda item: int(item.get("position") or 0),
        ):
            rendered_clause = deepcopy(dict(clause))
            rendered_clause["stale"] = bool(clause.get("derived_counts")) and (
                revision != int(clause.get("clause_set_revision_at_edit") or 0)
            )
            visible.append(rendered_clause)
        if visible:
            grouped.append(
                {
                    "section_id": section_id,
                    "title": section.get("title"),
                    "position": section.get("position"),
                    "clauses": visible,
                }
            )
    return {
        "schema_version": document.get("schema_version"),
        "client_id": document.get("client_id"),
        "engagement_id": document.get("engagement_id"),
        "run_id": document.get("run_id"),
        "title": "Diagnostic Audit",
        "generated_at": document.get("updated_at"),
        "sections": grouped,
    }


__all__ = [
    "CLIENT_REPORT_SCHEMA_VERSION",
    "ClientReportError",
    "REMOVED_METHOD_LIMITATION",
    "build_document",
    "compose_clause_wording",
    "edit_clause",
    "remove_clause",
    "render_model",
    "restore_clause",
    "set_clause_not_checked",
]
