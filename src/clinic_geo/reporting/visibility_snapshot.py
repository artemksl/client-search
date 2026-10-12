"""Generate the client-facing competitive visibility snapshot artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from clinic_geo.paths import repo_path, resolve_repo_relative
from clinic_geo.platforms import FULL_AUDIT_PLATFORMS, parse_platforms
from clinic_geo.reporting.data import ReportDataError, build_visibility_snapshot_payload
from clinic_geo.reporting.presentation import (
    PresentationContractError,
    build_presentation_model,
    is_presentation_v2,
    prepare_presentation_payload,
)
from clinic_geo.reporting.renderer import ReportRenderError, render_report


def report_stem(clinic_name: str) -> str:
    """Match the audit-results filename contract used by the web job runner."""
    clean = re.sub(r"[^A-Za-z0-9]+", "-", str(clinic_name or "").strip()).strip("-").lower()
    return clean or "unknown-clinic"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validation_text(payload: dict, *, metadata_normalized: bool) -> str:
    coverage = payload.get("coverage") or {}
    lines = [
        "VISIBILITY SNAPSHOT VALIDATION",
        f"Report ID: {payload.get('report_id') or 'unassigned'}",
        f"Client: {(payload.get('client') or {}).get('name') or 'unknown'}",
        f"Generated: {payload.get('generated_at') or 'unknown'}",
        f"Input SHA-256: {payload.get('input_sha256') or 'unknown'}",
        "Status: PARTIAL" if payload.get("warnings") else "Status: PASS",
        "",
        "COVERAGE",
        f"Valid outcomes: {coverage.get('valid_outcomes', 0)} of {coverage.get('expected_outcomes', 0)}",
        f"Market: {(coverage.get('market') or {}).get('valid_outcomes', 0)} of {(coverage.get('market') or {}).get('expected_outcomes', 0)}",
        f"Brand: {(coverage.get('brand') or {}).get('valid_outcomes', 0)} of {(coverage.get('brand') or {}).get('expected_outcomes', 0)}",
        "",
        "PLATFORMS",
    ]
    for platform in payload.get("platforms") or []:
        collection = platform.get("collection") or {}
        market = collection.get("market") or {}
        brand = collection.get("brand") or {}
        lines.append(
            f"{platform.get('label') or platform.get('key')}: "
            f"market {market.get('valid_outcomes', 0)}/{market.get('expected_outcomes', 0)} "
            f"({market.get('status', 'not_measured')}), "
            f"brand {brand.get('valid_outcomes', 0)}/{brand.get('expected_outcomes', 0)} "
            f"({brand.get('status', 'not_measured')})"
        )
    warnings = payload.get("warnings") or []
    lines.extend(["", f"WARNINGS ({len(warnings)})"])
    if warnings:
        for warning in warnings:
            context = "/".join(
                str(warning[key]) for key in ("stage", "platform") if warning.get(key)
            )
            prefix = f"[{warning.get('code', 'warning')}]"
            if context:
                prefix += f" {context}"
            lines.append(f"- {prefix}: {warning.get('message', '')}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "RENDER",
            "Pages: 3 (A4 portrait)",
            "External runtime assets: none",
            f"PDF metadata normalized: {'yes' if metadata_normalized else 'no'}",
            "",
            "All percentages use valid prompt outcomes as their denominator. "
            "Unavailable runs are excluded and remain visible as Not measured.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class _ArtifactPageParser(HTMLParser):
    """Collect report-page identity attributes without interpreting copy."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pages: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "section":
            return
        values = {key: value or "" for key, value in attrs}
        if "report-page" in values.get("class", "").split():
            self.pages.append(values)


def validate_v2_artifact_agreement(
    payload: Mapping[str, Any],
    html_document: str,
    *,
    pdf_path: Path | str | None = None,
) -> dict[str, Any]:
    """Fail closed unless data and all three rendered pages share one identity.

    The PDF is printed from this exact self-contained HTML in the same render
    operation, so agreement at this seam binds the PDF, HTML and normalized
    data artifacts to the same report, scope, result and copy hashes.
    """

    if not is_presentation_v2(payload):
        raise PresentationContractError("Artifact agreement is defined for versioned Snapshot presentations")
    identity = payload.get("artifact_identity")
    if not isinstance(identity, Mapping):
        raise PresentationContractError("Snapshot presentation needs artifact_identity")
    expected = {
        "data-report-id": str(identity.get("report_id") or "").strip(),
        "data-scope-hash": str(identity.get("scope_hash") or "").strip(),
        "data-prompt-pack-hash": str(identity.get("prompt_pack_hash") or "").strip(),
        "data-results-hash": str(identity.get("results_hash") or "").strip(),
        "data-copy-hash": str(identity.get("copy_hash") or "").strip(),
    }
    missing = [name.removeprefix("data-") for name, value in expected.items() if not value]
    if missing:
        raise PresentationContractError(
            "Artifact identity is incomplete: " + ", ".join(missing)
        )
    top_level = {
        "data-report-id": str(payload.get("report_id") or payload.get("snapshot_id") or "").strip(),
        "data-scope-hash": str(payload.get("scope_hash") or "").strip(),
        "data-prompt-pack-hash": str(payload.get("prompt_pack_hash") or "").strip(),
        "data-results-hash": str(payload.get("results_hash") or "").strip(),
        "data-copy-hash": str(
            ((payload.get("presentation") or {}).get("copy_hash")
             if isinstance(payload.get("presentation"), Mapping) else "") or ""
        ).strip(),
    }
    mismatches = [name for name in expected if top_level[name] != expected[name]]
    if mismatches:
        raise PresentationContractError(
            "Normalized data identity disagrees with artifact_identity: "
            + ", ".join(name.removeprefix("data-") for name in mismatches)
        )
    parser = _ArtifactPageParser()
    parser.feed(str(html_document or ""))
    parser.close()
    if len(parser.pages) != 3:
        raise PresentationContractError(
            f"Artifact agreement expected 3 HTML pages, found {len(parser.pages)}"
        )
    page_numbers = {row.get("data-report-page") for row in parser.pages}
    if page_numbers != {"1", "2", "3"}:
        raise PresentationContractError("HTML page identity must be exactly 1, 2 and 3")
    for page in parser.pages:
        for attribute, value in expected.items():
            if page.get(attribute) != value:
                raise PresentationContractError(
                    f"HTML page {page.get('data-report-page') or '?'} disagrees on "
                    f"{attribute.removeprefix('data-')}"
                )
    policy = payload.get("copy_policy")
    copy_passed = isinstance(policy, Mapping) and policy.get("passed") is True
    if not copy_passed:
        raise PresentationContractError("Artifact agreement requires a passing copy policy")
    pdf_checked = False
    if pdf_path is not None:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(Path(pdf_path).expanduser().resolve()))
        except Exception as exc:
            raise PresentationContractError(f"Could not inspect rendered PDF identity: {exc}") from exc
        if len(reader.pages) != 3:
            raise PresentationContractError(
                f"Artifact agreement expected 3 PDF pages, found {len(reader.pages)}"
            )
        metadata = reader.metadata or {}
        title = str(getattr(metadata, "title", None) or metadata.get("/Title") or "")
        title_tokens = {
            "report_id": f"report_id={expected['data-report-id']}",
            "scope_hash": f"scope_hash={expected['data-scope-hash']}",
            "prompt_pack_hash": f"prompt_pack_hash={expected['data-prompt-pack-hash']}",
            "results_hash": f"results_hash={expected['data-results-hash']}",
            "copy_hash": f"copy_hash={expected['data-copy-hash']}",
        }
        missing_title = [name for name, token in title_tokens.items() if token not in title]
        if missing_title:
            raise PresentationContractError(
                "PDF metadata identity is incomplete: " + ", ".join(missing_title)
            )
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        compact_text = re.sub(r"\s+", "", extracted)
        missing_text = [
            name
            for name, value in (
                ("report_id", expected["data-report-id"]),
                ("scope_hash", expected["data-scope-hash"]),
                ("prompt_pack_hash", expected["data-prompt-pack-hash"]),
                ("results_hash", expected["data-results-hash"]),
                ("copy_hash", expected["data-copy-hash"]),
            )
            if re.sub(r"\s+", "", value) not in compact_text
        ]
        if missing_text:
            raise PresentationContractError(
                "PDF visible identity is incomplete: " + ", ".join(missing_text)
            )
        pdf_checked = True
    return {
        "passed": True,
        "page_count": 3,
        "pdf_checked": pdf_checked,
        "report_id": expected["data-report-id"],
        "scope_hash": expected["data-scope-hash"],
        "prompt_pack_hash": expected["data-prompt-pack-hash"],
        "results_hash": expected["data-results-hash"],
        "copy_hash": expected["data-copy-hash"],
    }


def _v2_validation_text(
    payload: Mapping[str, Any],
    *,
    metadata_normalized: bool,
    agreement: Mapping[str, Any],
) -> str:
    presentation = payload.get("presentation")
    publication = (
        presentation.get("publication")
        if isinstance(presentation, Mapping)
        and isinstance(presentation.get("publication"), Mapping)
        else {}
    )
    policy = payload.get("copy_policy") if isinstance(payload.get("copy_policy"), Mapping) else {}
    notices = policy.get("notices") if isinstance(policy.get("notices"), list) else []
    scopes = payload.get("scopes") if isinstance(payload.get("scopes"), list) else []
    identity = payload.get("artifact_identity") if isinstance(payload.get("artifact_identity"), Mapping) else {}
    is_v4 = str(payload.get("schema_version") or "").endswith(".v4") or str(
        (presentation or {}).get("version") if isinstance(presentation, Mapping) else ""
    ).endswith("-v4")
    lines = [
        f"FREE VISIBILITY SNAPSHOT {'V4' if is_v4 else 'V2'} VALIDATION",
        f"Report ID: {identity.get('report_id') or 'unassigned'}",
        f"Publication state: {publication.get('state') or 'draft'}",
        f"Prospect ready: {'yes' if publication.get('prospect_ready') else 'no'}",
        "",
        "REQUIRED GATES",
        "Render passed: yes",
        f"Copy policy passed: {'yes' if policy.get('passed') is True else 'no'}",
        f"Copy review notices: {len(notices)}",
        f"Artifact agreement passed: {'yes' if agreement.get('passed') is True else 'no'}",
        "Pages: 3 (A4 portrait)",
        f"PDF metadata normalized: {'yes' if metadata_normalized else 'no'}",
        "",
        "ARTIFACT IDENTITY",
        f"Scope SHA-256: {identity.get('scope_hash') or 'unknown'}",
        f"Prompt pack SHA-256: {identity.get('prompt_pack_hash') or 'unknown'}",
        f"Results SHA-256: {identity.get('results_hash') or 'unknown'}",
        f"Copy SHA-256: {identity.get('copy_hash') or 'unknown'}",
        "",
        f"COPY POLICY NOTICES ({len(notices)})",
    ]
    if notices:
        for notice in notices:
            if not isinstance(notice, Mapping):
                continue
            lines.append(
                f"- [{notice.get('rule_id') or 'notice'}] "
                f"{notice.get('field_path') or 'evidence excerpt'}: "
                f"{notice.get('matched_text') or ''}"
            )
    else:
        lines.append("- none")
    if is_v4:
        basket_quality = (
            (payload.get("quality") or {}).get("basket_quality")
            if isinstance(payload.get("quality"), Mapping)
            else []
        )
        if isinstance(basket_quality, list):
            lines.extend(["", f"BASKETS ({len(basket_quality)})"])
            for basket in basket_quality:
                if not isinstance(basket, Mapping):
                    continue
                lines.append(
                    f"- {basket.get('basket_id') or 'unknown'}: "
                    f"{basket.get('evidence_cell_count') or 0} evidence cells; "
                    f"{basket.get('valid_cell_observations') or 0}/"
                    f"{basket.get('planned_cell_observations') or 0} valid cell observations"
                )
    lines.extend([
        "",
        f"SCOPES ({len(scopes)})",
    ])
    for scope in scopes:
        if not isinstance(scope, Mapping):
            continue
        collection = scope.get("collection") if isinstance(scope.get("collection"), Mapping) else {}
        evidence = scope.get("evidence") if isinstance(scope.get("evidence"), Mapping) else {}
        lines.append(
            f"- {scope.get('scope_id') or 'unassigned'}: "
            f"{collection.get('valid', 0)}/{collection.get('planned', 0)} valid; "
            f"evidence {'passed' if evidence.get('passes') else 'did not pass'}; "
            f"verdict {scope.get('verdict') or 'unknown'}"
        )
    return "\n".join(lines) + "\n"


def _publish_payload_artifacts(
    payload: dict,
    *,
    output_path: Path,
    stem: str,
    chromium_executable: Path | str | None,
    v2: bool,
) -> dict[str, Path]:
    destinations = {
        "pdf": output_path / f"{stem}_visibility_snapshot.pdf",
        "html": output_path / f"{stem}_visibility_snapshot.html",
        "data": output_path / f"{stem}_visibility_snapshot_data.json",
        "validation": output_path / f"{stem}_visibility_snapshot_validation.txt",
    }
    output_path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{stem}-snapshot-", dir=output_path) as temp:
        temporary = Path(temp)
        temp_html = temporary / destinations["html"].name
        temp_pdf = temporary / destinations["pdf"].name
        result = render_report(
            payload,
            html_path=temp_html,
            pdf_path=temp_pdf,
            normalize_metadata=True,
            chromium_executable=chromium_executable,
        )
        if result.page_count != 3:
            raise ReportRenderError(f"Expected three report pages, rendered {result.page_count}")
        if v2:
            agreement = validate_v2_artifact_agreement(
                payload,
                temp_html.read_text(encoding="utf-8"),
                pdf_path=temp_pdf,
            )
            payload["artifact_validation"] = {
                "render_passed": True,
                "copy_policy_passed": True,
                "artifact_agreement_passed": True,
                "page_count": result.page_count,
                "metadata_normalized": result.metadata_normalized,
            }
            validation = _v2_validation_text(
                payload,
                metadata_normalized=result.metadata_normalized,
                agreement=agreement,
            )
        else:
            validation = _validation_text(
                payload,
                metadata_normalized=result.metadata_normalized,
            )
        temp_data = temporary / destinations["data"].name
        temp_validation = temporary / destinations["validation"].name
        _write_json(temp_data, payload)
        temp_validation.write_text(validation, encoding="utf-8")
        temporary_paths = {
            "pdf": temp_pdf,
            "html": temp_html,
            "data": temp_data,
            "validation": temp_validation,
        }
        missing = [kind for kind, path in temporary_paths.items() if not path.is_file()]
        if missing:
            raise ReportRenderError("Report generation omitted: " + ", ".join(missing))
        for kind, destination in destinations.items():
            os.replace(temporary_paths[kind], destination)
    return destinations


def generate_visibility_snapshot_from_payload(
    payload: Mapping[str, Any],
    *,
    output_dir: Path | str,
    internal_candidate: bool = True,
    chromium_executable: Path | str | None = None,
) -> dict[str, Path]:
    """Render an already-normalized versioned payload as one atomic artifact set.

    Candidate generation is the safe default and forces the internal watermark
    on every page.  Prospect generation fails closed unless the payload records
    both a passing quality gate and explicit founder approval.
    """

    if not isinstance(payload, Mapping) or not is_presentation_v2(payload):
        raise PresentationContractError("Expected a normalized versioned Free Snapshot payload")
    source = dict(payload)
    if internal_candidate:
        presentation = dict(source.get("presentation") or {})
        presentation["internal_candidate"] = True
        source["presentation"] = presentation
    prepared = prepare_presentation_payload(source)
    model = build_presentation_model(prepared)
    if not internal_candidate and not model["publication"]["prospect_ready"]:
        raise PresentationContractError(
            "Prospect publication requires passing quality gates and approved founder review"
        )
    output_path = resolve_repo_relative(output_dir)
    stem = report_stem(model["clinic_name"])
    return _publish_payload_artifacts(
        prepared,
        output_path=output_path,
        stem=stem,
        chromium_executable=chromium_executable,
        v2=True,
    )


def generate_visibility_snapshot(
    *,
    source: Path | str,
    output_dir: Path | str,
    generated_at: str,
    platforms=FULL_AUDIT_PLATFORMS,
    strict: bool = False,
    chromium_executable: Path | str | None = None,
) -> dict[str, Path]:
    """Build and atomically publish PDF, HTML, normalized data, and validation."""
    source_path = resolve_repo_relative(source)
    output_path = resolve_repo_relative(output_dir)
    payload = build_visibility_snapshot_payload(
        source_path,
        generated_at=generated_at,
        platforms=platforms,
        strict=strict,
    )
    stem = report_stem((payload.get("client") or {}).get("name") or "")
    return _publish_payload_artifacts(
        payload,
        output_path=output_path,
        stem=stem,
        chromium_executable=chromium_executable,
        v2=False,
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default=str(repo_path("data", "free-audit")))
    p.add_argument("--output-dir", default=str(repo_path("audit-results")))
    p.add_argument("--generated-at", default=None)
    p.add_argument("--platforms", default=",".join(FULL_AUDIT_PLATFORMS))
    p.add_argument("--strict", action="store_true")
    p.add_argument("--chromium-executable", default=None)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        platforms = parse_platforms(args.platforms, default=FULL_AUDIT_PLATFORMS)
        outputs = generate_visibility_snapshot(
            source=args.source,
            output_dir=args.output_dir,
            generated_at=args.generated_at or utc_now(),
            platforms=platforms,
            strict=args.strict,
            chromium_executable=args.chromium_executable,
        )
    except (ReportDataError, ReportRenderError, OSError, ValueError) as exc:
        print(f"visibility snapshot failed: {exc}", file=sys.stderr)
        return 2
    for kind, path in outputs.items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
