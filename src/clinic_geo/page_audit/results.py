"""Stage 4 — persist-first results artifact, then advisory HTML brief (ADR-002 §4).

The structured JSON results artifact is written and fsync-safe BEFORE any HTML is
rendered, so a render failure loses nothing. AI results are advisory throughout;
the deterministic assessments are authoritative and are never overwritten,
weakened or re-scored. The pinned model + version are disclosed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Optional

RESULTS_SCHEMA_VERSION = "1.0.0"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class ResultsStore:
    """Accumulates per-page validated results and persists the JSON first."""

    def __init__(self, results_path: Path | str, *, run_identity: Mapping, provenance: Mapping):
        self.results_path = Path(results_path)
        self.run_identity = dict(run_identity)
        self.provenance = dict(provenance)  # model/profile/context window from startup record
        self.pages: dict = {}
        self.c05_aggregate: Optional[dict] = None
        self.review_records: list = []
        self.excluded_pages: list = []

    def add_page_result(self, page_id: str, record: Mapping) -> None:
        self.pages[page_id] = dict(record)
        self.persist()

    def set_c05(self, aggregate: Mapping) -> None:
        self.c05_aggregate = dict(aggregate)

    def set_excluded(self, excluded) -> None:
        self.excluded_pages = [dict(e) for e in excluded]

    def add_review_record(self, record: Mapping) -> None:
        self.review_records.append(dict(record))

    def document(self) -> dict:
        body = {
            "results_schema_version": RESULTS_SCHEMA_VERSION,
            "run": self.run_identity,
            "provenance": self.provenance,
            "advisory_notice": (
                "AI results are ADVISORY. Deterministic assessments are authoritative and are "
                "not modified here. Model: "
                + str(self.provenance.get("model_id")) + " (" + str(self.provenance.get("model_version")) + ")."
            ),
            "pages": self.pages,
            "c05_aggregate": self.c05_aggregate,
            "review_records": self.review_records,
            "excluded_pages": self.excluded_pages,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        body["results_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return body

    def persist(self) -> str:
        doc = self.document()
        _atomic_write(self.results_path, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
        return doc["results_sha256"]

    # -- targeted-review derivation (deterministic) ------------------------
    def derive_review_records(self, deterministic_by_page: Optional[Mapping] = None) -> None:
        """Flag material uncertain/unavailable, det-vs-AI conflicts, high-impact unexpected."""
        deterministic_by_page = deterministic_by_page or {}
        for page_id, page in self.pages.items():
            results = page.get("results", {}) or {}
            for check_id, res in results.items():
                state = res.get("state")
                if state in ("uncertain", "unavailable"):
                    self.add_review_record({
                        "page_id": page_id, "check_id": check_id, "kind": "material_" + state,
                        "reason": res.get("reason"),
                    })
            for obs in page.get("high_impact_unexpected", []) or []:
                self.add_review_record({"page_id": page_id, "kind": "high_impact_unexpected", "detail": obs})
            det = deterministic_by_page.get(page_id, {})
            for check_id, res in results.items():
                det_state = det.get(check_id)
                if det_state and res.get("state") == "issue" and det_state == "no_issue":
                    self.add_review_record({
                        "page_id": page_id, "check_id": check_id, "kind": "deterministic_ai_conflict",
                        "deterministic": det_state, "ai": "issue",
                    })

    # -- HTML brief (rendered FROM the persisted JSON) ---------------------
    def render_html(self, doc: Optional[Mapping] = None) -> str:
        doc = doc if doc is not None else self.document()
        prov = doc.get("provenance", {})
        run = doc.get("run", {})
        esc = _html_escape
        rows = []
        for page_id, page in sorted(doc.get("pages", {}).items()):
            rows.append(f"<h3>{esc(page_id)} <span class='url'>{esc(page.get('primary_url',''))}</span></h3>")
            rows.append(f"<p class='routing'>routing: {esc(page.get('routing',''))}</p>")
            rows.append("<table><tr><th>check</th><th>state</th><th>reason</th></tr>")
            for check_id, res in sorted((page.get("results", {}) or {}).items()):
                rows.append(
                    f"<tr><td>{esc(check_id)}</td><td class='s-{esc(res.get('state',''))}'>"
                    f"{esc(res.get('state',''))}</td><td>{esc(str(res.get('reason') or ''))}</td></tr>"
                )
            rows.append("</table>")
        c05 = doc.get("c05_aggregate") or {}
        review = doc.get("review_records", [])
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>AI Review Brief (advisory)</title>"
            "<style>body{font-family:system-ui,Arial,sans-serif;margin:2rem}"
            ".advisory{background:#fff8e1;border:1px solid #e0c060;padding:.6rem;border-radius:6px}"
            "table{border-collapse:collapse;margin:.4rem 0}td,th{border:1px solid #ccc;padding:.2rem .5rem}"
            ".url{color:#666;font-weight:normal;font-size:.8em}</style></head><body>"
            f"<h1>AI Review Brief — advisory</h1>"
            f"<p class='advisory'>{esc(doc.get('advisory_notice',''))}</p>"
            f"<p>Run: {esc(run.get('client_id',''))}/{esc(run.get('engagement_id',''))}/{esc(run.get('run_id',''))} "
            f"· model {esc(str(prov.get('model_id')))} ({esc(str(prov.get('model_version')))}) "
            f"· context {esc(str(prov.get('context_window')))}</p>"
            f"<h2>C05 site aggregate</h2><pre>{esc(json.dumps(c05, indent=2, ensure_ascii=False))}</pre>"
            f"<h2>Pages</h2>{''.join(rows)}"
            f"<h2>Targeted review ({len(review)})</h2><pre>{esc(json.dumps(review, indent=2, ensure_ascii=False))}</pre>"
            "</body></html>"
        )

    def render_and_write_html(self, html_path: Path | str) -> str:
        """Persist JSON FIRST (already done via add_page_result), then render HTML."""
        self.persist()  # ensure the JSON is on disk before any HTML
        doc = self.document()
        html = self.render_html(doc)
        Path(html_path).parent.mkdir(parents=True, exist_ok=True)
        Path(html_path).write_text(html, encoding="utf-8")
        return html


def _html_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
