"""Shell the paid Diagnostic Audit engine CLI and manage the UI-side run index.

The engine (`@citeup/audit-automation`) is a sealed black box behind its CLI. This
store never imports or forks it; it runs ``node audit-automation/src/cli.js <cmd>``
as a subprocess, parses the single JSON document the engine prints to stdout, and
maps a non-zero exit (whose stderr carries ``{message, code}``) to a typed error.

Three things live only on the Python side and never touch engine state:

* a small **UI run index** (``data/paid-audit/ui-runs.json``) recording which paid
  audits the founder launched from the dashboard, plus cached state and links;
* **productions** — timestamped, immutable copies of the engine's finished packet
  plus a separately labelled AI-assistance disclosure, created by one-click
  :meth:`produce`. Producing never mutates the working run,
  so it is fully repeatable; :meth:`delete` removes only the dashboard record and
  that run's reports/productions, and never the engine's shared, content-addressed
  evidence vault or its SQLite state (engine invariant #4);
* **client reports** — editable clause documents, numbered restore points, and
  client-ready PDFs derived from the engine's exported operator-report artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from types import MappingProxyType
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping, Optional

from clinic_geo.paths import repo_root as _repo_root
from clinic_geo.persistence.json_store import read_json_or_none, write_json
from .engine import (
    discover_chrome_path,
    discover_node_bin,
    node_binary_satisfies,
    paid_audit_cli_path,
    paid_audit_data_dir,
    paid_audit_engine_env,
    required_node_major,
)


# --- errors -----------------------------------------------------------------


class PaidAuditError(RuntimeError):
    """Base error for the paid audit integration; carries an HTTP status + code."""

    status = HTTPStatus.BAD_REQUEST
    code = "paid_audit_error"

    def __init__(self, message: str, *, code: str | None = None, status: Any = None):
        super().__init__(message)
        if code:
            self.code = code
        if status is not None:
            self.status = status


class PaidAuditNotFoundError(PaidAuditError):
    status = HTTPStatus.NOT_FOUND
    code = "paid_audit_not_found"


class PaidAuditConflictError(PaidAuditError):
    status = HTTPStatus.CONFLICT
    code = "paid_audit_conflict"


class PaidAuditUnprocessableError(PaidAuditError):
    status = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "paid_audit_unprocessable"


class PaidAuditEngineUnavailableError(PaidAuditError):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    code = "engine_unavailable"


# --- helpers ----------------------------------------------------------------


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
ENGINE_CLI_CONTRACT_VERSION = "engine-cli-v1"
OPERATOR_REPORT_ARTIFACT_KIND = "operator_report_json"
# Production aliases remain the sealed engine packet only.  REPORT_KINDS is
# intentionally broader: advisory files can be downloaded from a working run,
# but must never be swept into a verbatim operator-packet production.
PRODUCTION_REPORT_KINDS = MappingProxyType({
    "packet.html": "operator-review-packet.html",
    "packet.pdf": "operator-review-packet.pdf",
    "workbench.html": "review-workbench.html",
})
REPORT_KINDS = MappingProxyType({
    **PRODUCTION_REPORT_KINDS,
    "ai-brief.html": "review-brief.html",
})
ATTESTATION = "all_items_reviewed"
PRODUCTION_REVIEW_ATTESTATION = "i_have_reviewed_this"
PRODUCTION_REVIEW_STATEMENT = "I have reviewed this packet."
AI_ASSISTANCE_DISCLOSURE_FILENAME = "AI-ASSISTANCE-DISCLOSURE.txt"
AI_ASSISTANCE_DISCLOSURE_LABEL = "AI-assisted advisory disclosure"
AI_ASSISTANCE_DISCLOSURE_STATEMENT = (
    "AI-assisted advisory material may be incorporated into the separately generated, "
    "editable client report. This production contains only the copied operator packet files "
    "listed in its manifest; those files remain byte-for-byte copies of the engine outputs "
    "and do not include the editable client report."
)
AI_ASSISTANCE_DISCLOSURE_ALIAS = "ai-assistance-disclosure.txt"
PRODUCTION_ARTIFACT_KINDS = MappingProxyType({
    **PRODUCTION_REPORT_KINDS,
    AI_ASSISTANCE_DISCLOSURE_ALIAS: AI_ASSISTANCE_DISCLOSURE_FILENAME,
})
ENGINE_CORE_ERROR_HTTP_STATUSES = MappingProxyType({
    "NOT_FOUND": HTTPStatus.NOT_FOUND,
    "CONFLICT": HTTPStatus.CONFLICT,
    "INVALID_STATE_TRANSITION": HTTPStatus.CONFLICT,
    "STALE_LEASE": HTTPStatus.CONFLICT,
    "TENANT_REFERENCE_DENIED": HTTPStatus.UNPROCESSABLE_ENTITY,
    "CORE_ERROR": HTTPStatus.BAD_REQUEST,
})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stamp() -> str:
    # Microsecond precision so two clicks in the same second never collide.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _safe_segment(value: str, *, field: str) -> str:
    value = str(value or "").strip()
    if not value or value in {".", ".."} or not _SAFE_SEGMENT.match(value):
        raise PaidAuditNotFoundError(f"Invalid {field}: {value!r}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PaidAuditStore:
    """Adapter over the engine CLI plus the UI-side run index / productions."""

    def __init__(
        self,
        repo_root: Path | str | None = None,
        *,
        node_bin: str | None = None,
        cli_path: Path | str | None = None,
        data_dir: Path | str | None = None,
        timeout: float = 900.0,
    ):
        self.repo_root = Path(repo_root) if repo_root else _repo_root()
        self._node_bin = node_bin
        self._cli_path = Path(cli_path) if cli_path else None
        self.data_dir = Path(data_dir) if data_dir else paid_audit_data_dir(self.repo_root)
        self.timeout = timeout
        self._lock = threading.Lock()

    # -- provisioning ------------------------------------------------------

    def node_bin(self) -> str:
        node = self._node_bin or discover_node_bin()
        if not node:
            raise PaidAuditEngineUnavailableError(
                "Node.js was not found. Run ./bootstrap.sh to provision the paid audit engine."
            )
        return node

    def cli_path(self) -> Path:
        cli = self._cli_path or paid_audit_cli_path(self.repo_root)
        if not Path(cli).exists():
            raise PaidAuditEngineUnavailableError(f"Paid audit engine CLI is missing at {cli}")
        return Path(cli)

    def availability(self) -> dict:
        """Best-effort provisioning summary for the dashboard (never raises)."""
        node = self._node_bin or discover_node_bin()
        cli = self._cli_path or paid_audit_cli_path(self.repo_root)
        chrome = discover_chrome_path(self.repo_root)
        node_ok = bool(node) and node_binary_satisfies(node)
        cli_exists = Path(cli).exists()
        reasons = []
        if not node:
            reasons.append("Node.js not found")
        elif not node_ok:
            reasons.append(f"Node.js < {required_node_major(self.repo_root)} required")
        if not cli_exists:
            reasons.append("engine CLI missing")
        return {
            "ok": bool(node_ok and cli_exists),
            "node_bin": node,
            "node_ok": node_ok,
            "cli": str(cli),
            "cli_exists": cli_exists,
            "chrome_path": chrome,
            "data_dir": str(self.data_dir),
            "reason": "; ".join(reasons) or None,
        }

    # -- CLI seam ----------------------------------------------------------

    def _run_cli(self, args: list[str]):
        """Run one engine CLI command and return its parsed JSON stdout document."""
        cmd = [
            self.node_bin(),
            str(self.cli_path()),
            *args,
            "--data-dir",
            str(self.data_dir),
        ]
        env = dict(os.environ)
        env.update(paid_audit_engine_env(self.repo_root))
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise PaidAuditEngineUnavailableError(f"Could not launch the engine: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PaidAuditError(f"Engine command timed out: {' '.join(args)}") from exc
        if proc.returncode != 0:
            raise self._error_from_stderr(proc.stderr, proc.stdout, proc.returncode)
        return self._validated_contract_document(self._parse_stdout(proc.stdout))

    @staticmethod
    def _validated_contract_document(document):
        if (
            not isinstance(document, Mapping)
            or document.get("contract_version") != ENGINE_CLI_CONTRACT_VERSION
        ):
            actual = (
                document.get("contract_version")
                if isinstance(document, Mapping)
                else None
            )
            raise PaidAuditError(
                "Paid audit engine contract mismatch: expected "
                f"{ENGINE_CLI_CONTRACT_VERSION!r}, got {actual!r}",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        return document

    @staticmethod
    def _envelope_items(document: Mapping, key: str) -> list:
        items = document.get(key)
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) for item in items
        ):
            raise PaidAuditError(
                f"Paid audit engine contract mismatch: {key!r} is not a list of objects",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        return items

    @staticmethod
    def _parse_stdout(stdout: str):
        text = (stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # The engine prints exactly one JSON document, but be forgiving if a
            # stray line precedes it: take the last balanced JSON value.
            for start in range(len(text)):
                if text[start] in "{[":
                    try:
                        return json.loads(text[start:])
                    except json.JSONDecodeError:
                        continue
            raise PaidAuditError("The engine returned output that was not valid JSON")

    @staticmethod
    def _error_from_stderr(stderr: str, stdout: str, returncode: int) -> PaidAuditError:
        message = ""
        code = None
        for line in reversed((stderr or "").strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                message = message or line
                continue
            if isinstance(parsed, Mapping) and parsed.get("message"):
                if (
                    parsed.get("contract_version")
                    != ENGINE_CLI_CONTRACT_VERSION
                ):
                    return PaidAuditError(
                        "Paid audit engine error contract mismatch: expected "
                        f"{ENGINE_CLI_CONTRACT_VERSION!r}, got "
                        f"{parsed.get('contract_version')!r}",
                        code="engine_contract_mismatch",
                        status=HTTPStatus.BAD_GATEWAY,
                    )
                message = str(parsed.get("message"))
                code = parsed.get("code")
                break
        if not message:
            message = (stderr or stdout or "").strip() or f"Engine command failed (exit {returncode})"
        code = str(code) if code else None
        kwargs = {"code": code} if code else {}
        status = ENGINE_CORE_ERROR_HTTP_STATUSES.get(code)
        if status == HTTPStatus.NOT_FOUND:
            return PaidAuditNotFoundError(message, **kwargs)
        if status == HTTPStatus.CONFLICT:
            return PaidAuditConflictError(message, **kwargs)
        if status == HTTPStatus.UNPROCESSABLE_ENTITY:
            return PaidAuditUnprocessableError(message, **kwargs)
        if status == HTTPStatus.BAD_REQUEST:
            return PaidAuditError(message, **kwargs)

        # Native, workflow, and browser error codes are intentionally outside
        # the CoreError contract. Preserve the legacy text mapping for them,
        # and for older/codeless engine errors.
        low = message.lower()
        if "not found" in low or "no such" in low or "does not exist" in low:
            return PaidAuditNotFoundError(message, **kwargs)
        if any(term in low for term in ("already", "immutable", "stale", "changed", "conflict", "in progress")):
            return PaidAuditConflictError(message, **kwargs)
        if any(term in low for term in ("required", "must be", "attestation", "invalid", "missing")):
            return PaidAuditUnprocessableError(message, **kwargs)
        return PaidAuditError(message, **kwargs)

    # -- read commands -----------------------------------------------------

    def list_runs(self, client_id: str) -> list:
        result = self._run_cli(["list-runs", "--client", str(client_id)])
        return self._envelope_items(result, "runs")

    def status(self, client_id: str, engagement_id: str, run_id: str) -> dict:
        return self._run_cli(
            ["status", "--client", client_id, "--engagement", engagement_id, "--run-id", run_id]
        )

    def review_bundles(self, client_id: str, engagement_id: str, run_id: str, state: str | None = None) -> dict:
        args = ["review-bundles", "--client", client_id, "--engagement", engagement_id, "--run-id", run_id]
        if state:
            args += ["--state", state]
        return self._run_cli(args)

    def reviews(self, client_id: str, engagement_id: str, run_id: str, state: str | None = None) -> list:
        args = ["reviews", "--client", client_id, "--engagement", engagement_id, "--run-id", run_id]
        if state:
            args += ["--state", state]
        result = self._run_cli(args)
        return self._envelope_items(result, "reviews")

    def export_run(self, client_id: str, engagement_id: str, run_id: str) -> dict:
        """Return the engine's versioned, read-only run export document."""
        client_id = _safe_segment(client_id, field="client")
        engagement_id = _safe_segment(engagement_id, field="engagement")
        run_id = _safe_segment(run_id, field="run")
        document = self._run_cli(
            [
                "export-run",
                "--client",
                client_id,
                "--engagement",
                engagement_id,
                "--run-id",
                run_id,
            ]
        )
        run = document.get("run")
        expected = (client_id, engagement_id, run_id)
        actual = (
            str(run.get("client_id") or ""),
            str(run.get("engagement_id") or ""),
            str(run.get("run_id") or ""),
        ) if isinstance(run, Mapping) else ("", "", "")
        mapping_keys = ("reports", "artifacts", "artifacts_by_id")
        if actual != expected or any(
            not isinstance(document.get(key), Mapping) for key in mapping_keys
        ):
            raise PaidAuditError(
                "Paid audit engine export-run document has an invalid shape or identity",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        return document

    def load_analysis_catalogue(self) -> dict:
        """Load the tracked paid-audit point catalogue used only for UI tags."""
        path = self.repo_root / "design-v1.4" / "02_analysis_catalogue_v2.2.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaidAuditError(
                "The paid-audit analysis catalogue is unavailable or invalid",
                code="analysis_catalogue_contract_mismatch",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc
        points = document.get("points") if isinstance(document, Mapping) else None
        if not isinstance(points, list) or any(
            not isinstance(point, Mapping) for point in points
        ):
            raise PaidAuditError(
                "The paid-audit analysis catalogue has an invalid points list",
                code="analysis_catalogue_contract_mismatch",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return dict(document)

    def _artifact_path_from_export(
        self,
        document: Mapping,
        client_id: str,
        engagement_id: str,
        run_id: str,
        kind: str,
    ) -> Path:
        """Resolve and SHA-verify one content-addressed export-run artifact."""
        _safe_segment(client_id, field="client")
        _safe_segment(engagement_id, field="engagement")
        _safe_segment(run_id, field="run")
        artifacts = document.get("artifacts") if isinstance(document, Mapping) else None
        locator = artifacts.get(kind) if isinstance(artifacts, Mapping) else None
        if not isinstance(locator, Mapping):
            if kind == OPERATOR_REPORT_ARTIFACT_KIND:
                raise PaidAuditNotFoundError(
                    "This audit has no structured operator report yet. Re-run the audit "
                    "before opening its client report."
                )
            raise PaidAuditNotFoundError(f"This audit has no {kind} artifact yet")
        storage_path = locator.get("storage_path")
        expected_sha = locator.get("sha256")
        if (
            not isinstance(storage_path, str)
            or not storage_path
            or not isinstance(expected_sha, str)
            or not expected_sha
        ):
            raise PaidAuditError(
                f"Paid audit engine returned an invalid locator for {kind}",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        artifacts_root = (self.data_dir / "artifacts").resolve()
        path = (artifacts_root / storage_path).resolve()
        if artifacts_root not in path.parents:
            raise PaidAuditNotFoundError("Artifact path escapes the engine artifacts directory")
        if not path.is_file():
            if kind == OPERATOR_REPORT_ARTIFACT_KIND:
                raise PaidAuditNotFoundError(
                    "This audit's structured operator report is missing. Re-run the audit "
                    "before opening its client report."
                )
            raise PaidAuditNotFoundError(f"This audit has no {kind} artifact yet")
        if _sha256_file(path) != expected_sha:
            raise PaidAuditError(
                f"Engine artifact {kind!r} failed its SHA-256 check",
                code="engine_artifact_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        return path

    def load_operator_report(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> tuple[dict, dict]:
        """Read the structured report envelope through export-run only."""
        report, validation, _document = self.load_operator_report_with_export(
            client_id, engagement_id, run_id
        )
        return report, validation

    def load_operator_report_with_export(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> tuple[dict, dict, dict]:
        """Read a report and return the exact export snapshot that located it."""
        document = self.export_run(client_id, engagement_id, run_id)
        path = self._artifact_path_from_export(
            document,
            client_id,
            engagement_id,
            run_id,
            OPERATOR_REPORT_ARTIFACT_KIND,
        )
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaidAuditError(
                "The structured operator report artifact is not valid JSON",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            ) from exc
        report = envelope.get("report") if isinstance(envelope, Mapping) else None
        validation = envelope.get("validation") if isinstance(envelope, Mapping) else None
        expected = (str(client_id), str(engagement_id), str(run_id))
        actual = (
            str(report.get("client_id") or ""),
            str(report.get("engagement_id") or ""),
            str(report.get("run_id") or ""),
        ) if isinstance(report, Mapping) else ("", "", "")
        if not isinstance(report, Mapping) or not isinstance(validation, Mapping) or actual != expected:
            raise PaidAuditError(
                "The structured operator report envelope has an invalid shape or identity",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        return dict(report), dict(validation), document

    # -- decision commands -------------------------------------------------

    def decide_bundle(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        *,
        bundle_id: str,
        actor: str,
        rationale: str,
        attestation: str = ATTESTATION,
        decision: str | None = None,
        decisions: list | None = None,
        resolves_action_id: str | None = None,
        payload: Mapping | None = None,
    ) -> dict:
        actor = str(actor or "").strip()
        rationale = str(rationale or "").strip()
        if not actor:
            raise PaidAuditUnprocessableError("An actor (who is deciding) is required")
        if not rationale:
            raise PaidAuditUnprocessableError("A rationale is required")
        if attestation != ATTESTATION:
            raise PaidAuditUnprocessableError(f"attestation must be {ATTESTATION!r}")
        if bool(decision) == bool(decisions):
            raise PaidAuditUnprocessableError("Provide exactly one of decision or decisions")
        args = [
            "decide-review-bundle",
            "--client", client_id,
            "--engagement", engagement_id,
            "--run-id", run_id,
            "--bundle-id", str(bundle_id),
            "--actor", actor,
            "--rationale", rationale,
            "--attestation", attestation,
        ]
        if resolves_action_id:
            args += ["--resolves-action-id", str(resolves_action_id)]
        if payload:
            args += ["--payload", json.dumps(payload)]

        tmp_path: Optional[Path] = None
        try:
            if decisions is not None:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = self.data_dir / f".decisions-{_stamp()}.json"
                write_json(tmp_path, list(decisions))
                args += ["--decisions", str(tmp_path)]
            else:
                if decision not in {"approve", "reject", "escalate"}:
                    raise PaidAuditUnprocessableError("decision must be approve, reject, or escalate")
                args += ["--decision", decision]
            return self._run_cli(args)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def cancel(self, client_id: str, engagement_id: str, run_id: str, *, reason: str) -> dict:
        reason = str(reason or "").strip()
        if not reason:
            raise PaidAuditUnprocessableError("A cancellation reason is required")
        return self._run_cli(
            ["cancel", "--client", client_id, "--engagement", engagement_id, "--run-id", run_id, "--reason", reason]
        )

    # -- pinned crawler registry (Stage 5b) -------------------------------

    def pinned_registry_path(self, run_id: str) -> Path:
        """Where this run's projected engine registry lives (stable across resume)."""
        run_seg = _safe_segment(str(run_id), field="run")
        return self.data_dir / "pinned-registries" / f"{run_seg}-capability-registry.json"

    def prepare_pinned_registry(
        self,
        run_id: str,
        *,
        registry_store: Any = None,
        review_window_seconds: int | None = None,
    ) -> dict:
        """Pin one approved crawler-registry version for this run at launch.

        Projects the current approved v2 registry into the engine's flat shape,
        writes it to ``<data-dir>/pinned-registries/<run>-capability-registry.json``,
        and returns ``{override_path, registry_pinned, reason}`` for the launch
        path to pass ``--capability-registry`` and record in the run metadata.

        When NO approved version exists, returns an explicit
        ``registry_pinned=None`` with ``reason="no_approved_version"`` — the engine
        then launches with its sealed seed, but the absence is recorded, never
        silent. A tampered approved version or a projection failure raises
        :class:`PaidAuditError` and ABORTS the launch — we never fall back from a
        broken approved version to the seed silently.
        """
        from clinic_geo.crawler_registry import engine_projection
        from clinic_geo.crawler_registry.store import Store as _RegistryStore

        store = registry_store if registry_store is not None else _RegistryStore()
        try:
            approved = store.current_approved()
        except Exception as exc:  # tamper / integrity failure on load
            raise PaidAuditError(
                "The approved crawler registry failed its integrity check; refusing "
                f"to launch rather than silently fall back to the seed: {exc}",
                code="registry_pin_failed",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc

        if approved is None:
            return {
                "override_path": None,
                "registry_pinned": None,
                "reason": "no_approved_version",
            }

        try:
            kwargs = {}
            if review_window_seconds is not None:
                kwargs["review_window_seconds"] = review_window_seconds
            projection = engine_projection.project_to_engine(approved, **kwargs)
        except Exception as exc:
            raise PaidAuditError(
                "Failed to project the approved crawler registry "
                f"(hash {approved.get('content_hash')}); refusing to launch: {exc}",
                code="registry_pin_failed",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc

        path = self.pinned_registry_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, projection)
        return {
            "override_path": str(path),
            "registry_pinned": {
                "approved_hash": approved["content_hash"],
                "registry_version": projection.get("registry_version"),
                "projected_at": utc_now(),
            },
            "reason": None,
        }

    # -- UI run index ------------------------------------------------------

    def _index_path(self) -> Path:
        return self.data_dir / "ui-runs.json"

    def _read_index(self) -> list[dict]:
        data = read_json_or_none(self._index_path())
        return [dict(row) for row in data] if isinstance(data, list) else []

    def _write_index(self, records: list[dict]) -> None:
        write_json(self._index_path(), records)

    @staticmethod
    def _key(record: Mapping) -> tuple:
        return (
            str(record.get("client_id") or ""),
            str(record.get("engagement_id") or ""),
            str(record.get("run_id") or ""),
        )

    def record_run(self, record: Mapping) -> dict:
        """Insert or merge a run into the UI index (called at launch and finalize)."""
        incoming = dict(record)
        key = self._key(incoming)
        with self._lock:
            records = self._read_index()
            merged = None
            for existing in records:
                if self._key(existing) == key:
                    existing.update({k: v for k, v in incoming.items() if v is not None})
                    existing["updated_at"] = utc_now()
                    merged = existing
                    break
            if merged is None:
                incoming.setdefault("launched_at", utc_now())
                incoming["updated_at"] = utc_now()
                incoming.setdefault("productions", [])
                records.append(incoming)
                merged = incoming
            self._write_index(records)
        return merged

    def _find_record(self, client_id: str, engagement_id: str, run_id: str) -> Optional[dict]:
        key = (str(client_id), str(engagement_id), str(run_id))
        for row in self._read_index():
            if self._key(row) == key:
                return row
        return None

    def list_paid_audits(self, *, refresh: bool = True) -> list[dict]:
        """Return the UI index, enriched (best effort) with fresh engine state."""
        records = self._read_index()
        state_by_run: dict[str, dict] = {}
        if refresh and records:
            for client_id in sorted({str(r.get("client_id") or "") for r in records if r.get("client_id")}):
                try:
                    for row in self.list_runs(client_id):
                        if isinstance(row, Mapping) and row.get("run_id"):
                            state_by_run[str(row["run_id"])] = dict(row)
                except PaidAuditError:
                    # Engine unavailable or client not yet in the DB: keep cached state.
                    continue
        enriched = []
        for record in records:
            row = dict(record)
            live = state_by_run.get(str(record.get("run_id") or ""))
            if live:
                row["state"] = live.get("state", row.get("state"))
                row["engine"] = live
            row["downloads"] = self._download_links(record)
            enriched.append(row)
        enriched.sort(key=lambda r: r.get("launched_at") or "", reverse=True)
        return enriched

    def _download_links(self, record: Mapping) -> dict:
        client = record.get("client_id")
        engagement = record.get("engagement_id")
        run = record.get("run_id")
        if not (client and engagement and run):
            return {}
        base = f"/api/paid-audits/{client}/{engagement}/{run}"
        links = {}
        try:
            export_document = self.export_run(str(client), str(engagement), str(run))
        except PaidAuditError:
            export_document = None
        for alias in REPORT_KINDS:
            if alias == "ai-brief.html":
                try:
                    path = self._page_audit_brief_path(client, engagement, run)
                except PaidAuditNotFoundError:
                    continue
            else:
                if export_document is None:
                    continue
                try:
                    path = self._report_path_from_export(
                        export_document, client, engagement, run, alias
                    )
                except PaidAuditError:
                    continue
            if path.is_file():
                links[alias] = f"{base}/{alias}"
        return links

    def get_paid_audit(self, client_id: str, engagement_id: str, run_id: str) -> dict:
        client_id = _safe_segment(client_id, field="client")
        engagement_id = _safe_segment(engagement_id, field="engagement")
        run_id = _safe_segment(run_id, field="run")
        record = self._find_record(client_id, engagement_id, run_id)
        detail: dict = {"record": record}
        detail["status"] = self.status(client_id, engagement_id, run_id)
        detail["review_bundles"] = self.review_bundles(client_id, engagement_id, run_id)
        detail["downloads"] = self._download_links(
            record or {"client_id": client_id, "engagement_id": engagement_id, "run_id": run_id}
        )
        detail["productions"] = list((record or {}).get("productions") or [])
        return detail

    # -- editable client-report files -------------------------------------

    def client_report_dir(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> Path:
        return (
            self.data_dir
            / "client-reports"
            / _safe_segment(client_id, field="client")
            / _safe_segment(engagement_id, field="engagement")
            / _safe_segment(run_id, field="run")
        )

    def _ensure_private_directory(self, directory: Path) -> Path:
        root = (self.data_dir / "client-reports").resolve()
        target = directory.resolve()
        if target != root and root not in target.parents:
            raise PaidAuditNotFoundError("Client-report path escapes its storage directory")
        current = root
        current.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(current, 0o700)
        for part in target.relative_to(root).parts:
            current = current / part
            current.mkdir(mode=0o700, exist_ok=True)
            os.chmod(current, 0o700)
        return target

    def _read_private_json(
        self,
        path: Path,
        *,
        missing_ok: bool = False,
        expected: type = dict,
    ) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if missing_ok:
                return None
            raise PaidAuditNotFoundError(f"Client-report file not found: {path.name}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaidAuditError(
                f"Client-report storage is invalid at {path.name}",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc
        if not isinstance(value, expected):
            raise PaidAuditError(
                f"Client-report storage has the wrong shape at {path.name}",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return value

    def _write_private_json(self, path: Path, value: Any) -> None:
        directory = self._ensure_private_directory(path.parent)
        payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    def _write_private_copy(self, source: Path, target: Path) -> None:
        directory = self._ensure_private_directory(target.parent)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                fd = -1
                with source.open("rb") as input_handle:
                    shutil.copyfileobj(input_handle, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _positive_number(value: int | str, *, field: str) -> int:
        text = str(value)
        if not text.isdigit() or int(text) <= 0:
            raise PaidAuditNotFoundError(f"Invalid {field}: {value!r}")
        return int(text)

    @staticmethod
    def _visible_clause_count(document: Mapping) -> int:
        clauses = document.get("clauses")
        if not isinstance(clauses, list):
            raise PaidAuditError(
                "Client-report document has no clause list",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return sum(
            1
            for clause in clauses
            if isinstance(clause, Mapping) and clause.get("removed") is not True
        )

    def read_client_document(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> dict | None:
        directory = self.client_report_dir(client_id, engagement_id, run_id)
        document = self._read_private_json(
            directory / "document.json", missing_ok=True, expected=dict
        )
        if document is None:
            return None
        expected = (str(client_id), str(engagement_id), str(run_id))
        actual = (
            str(document.get("client_id") or ""),
            str(document.get("engagement_id") or ""),
            str(document.get("run_id") or ""),
        )
        if actual != expected:
            raise PaidAuditError(
                "Client-report document identity does not match its storage path",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return dict(document)

    def write_client_document(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        document: Mapping,
    ) -> dict:
        if not isinstance(document, Mapping):
            raise PaidAuditError(
                "Client-report document must be a mapping",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        expected = (str(client_id), str(engagement_id), str(run_id))
        actual = (
            str(document.get("client_id") or ""),
            str(document.get("engagement_id") or ""),
            str(document.get("run_id") or ""),
        )
        if actual != expected:
            raise PaidAuditError(
                "Client-report document identity does not match the requested run",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        saved = dict(document)
        with self._lock:
            self._write_private_json(
                self.client_report_dir(client_id, engagement_id, run_id) / "document.json",
                saved,
            )
        return saved

    def _restore_index_path(self, client_id: str, engagement_id: str, run_id: str) -> Path:
        return self.client_report_dir(client_id, engagement_id, run_id) / "restore-points" / "index.json"

    def _produced_index_path(self, client_id: str, engagement_id: str, run_id: str) -> Path:
        return self.client_report_dir(client_id, engagement_id, run_id) / "produced" / "index.json"

    def _index_entries(self, path: Path) -> list[dict]:
        value = self._read_private_json(path, missing_ok=True, expected=list)
        if value is None:
            return []
        if any(not isinstance(item, Mapping) for item in value):
            raise PaidAuditError(
                f"Client-report index has an invalid entry at {path.name}",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return [dict(item) for item in value]

    def save_restore_point(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        document: Mapping,
        *,
        now: str | None = None,
        label: str | None = None,
    ) -> dict:
        directory = self.client_report_dir(client_id, engagement_id, run_id) / "restore-points"
        index_path = directory / "index.json"
        saved_at = str(now or utc_now())
        with self._lock:
            entries = self._index_entries(index_path)
            used = {
                int(item["number"])
                for item in entries
                if str(item.get("number") or "").isdigit()
            }
            if directory.is_dir():
                for path in directory.glob("[0-9][0-9][0-9][0-9].json"):
                    if path.stem.isdigit():
                        used.add(int(path.stem))
            number = max(used, default=0) + 1
            snapshot = {
                "source": json.loads(json.dumps(document.get("source"))),
                "advisory_source": json.loads(
                    json.dumps(document.get("advisory_source"))
                ),
                "sections": json.loads(json.dumps(document.get("sections"))),
                "clauses": json.loads(json.dumps(document.get("clauses"))),
                "clause_set_revision": int(document.get("clause_set_revision") or 0),
                "saved_at": saved_at,
                "label": label,
            }
            if not isinstance(snapshot["sections"], list) or not isinstance(snapshot["clauses"], list):
                raise PaidAuditError(
                    "Client-report document cannot be checkpointed without sections and clauses",
                    code="client_report_storage_error",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            if not isinstance(snapshot["source"], Mapping) or (
                snapshot["advisory_source"] is not None
                and not isinstance(snapshot["advisory_source"], Mapping)
            ):
                raise PaidAuditError(
                    "Client-report document cannot be checkpointed without valid source identities",
                    code="client_report_storage_error",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            self._write_private_json(directory / f"{number:04d}.json", snapshot)
            metadata = {
                "number": number,
                "saved_at": saved_at,
                "clause_count": self._visible_clause_count(document),
            }
            entries.append(metadata)
            self._write_private_json(index_path, entries)
        return metadata

    def list_restore_points(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> list[dict]:
        return self._index_entries(self._restore_index_path(client_id, engagement_id, run_id))

    def load_restore_point(
        self, client_id: str, engagement_id: str, run_id: str, number: int | str
    ) -> dict:
        resolved = self._positive_number(number, field="restore point")
        path = (
            self.client_report_dir(client_id, engagement_id, run_id)
            / "restore-points"
            / f"{resolved:04d}.json"
        )
        snapshot = self._read_private_json(path, expected=dict)
        if not isinstance(snapshot.get("sections"), list) or not isinstance(snapshot.get("clauses"), list):
            raise PaidAuditError(
                "The restore point has an invalid document snapshot",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        revision = snapshot.get("clause_set_revision")
        if type(revision) is not int or revision < 0:
            raise PaidAuditError(
                "The restore point has an invalid clause-set revision",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        if "source" in snapshot and not isinstance(snapshot["source"], Mapping):
            raise PaidAuditError(
                "The restore point has an invalid operator source identity",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        if (
            "advisory_source" in snapshot
            and snapshot["advisory_source"] is not None
            and not isinstance(snapshot["advisory_source"], Mapping)
        ):
            raise PaidAuditError(
                "The restore point has an invalid advisory source identity",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return dict(snapshot)

    def record_produced_pdf(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        pdf_path: Path | str,
        *,
        produced_at: str | None = None,
        restore_point: int | None = None,
        clause_count: int,
    ) -> dict:
        source = Path(pdf_path).resolve()
        if not source.is_file():
            raise PaidAuditNotFoundError("The rendered client-report PDF is missing")
        directory = self.client_report_dir(client_id, engagement_id, run_id) / "produced"
        index_path = directory / "index.json"
        with self._lock:
            entries = self._index_entries(index_path)
            used = {
                int(item["number"])
                for item in entries
                if str(item.get("number") or "").isdigit()
            }
            if directory.is_dir():
                for path in directory.glob("client-report-[0-9][0-9][0-9][0-9].pdf"):
                    suffix = path.stem.removeprefix("client-report-")
                    if suffix.isdigit():
                        used.add(int(suffix))
            number = max(used, default=0) + 1
            filename = f"client-report-{number:04d}.pdf"
            target = directory / filename
            self._write_private_copy(source, target)
            metadata = {
                "number": number,
                "produced_at": str(produced_at or utc_now()),
                "sha256": _sha256_file(target),
                "restore_point": restore_point,
                "clause_count": int(clause_count),
                "filename": filename,
            }
            entries.append(metadata)
            self._write_private_json(index_path, entries)
        return metadata

    def list_produced_pdfs(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> list[dict]:
        return self._index_entries(self._produced_index_path(client_id, engagement_id, run_id))

    def produced_pdf_path(
        self, client_id: str, engagement_id: str, run_id: str, number: int | str
    ) -> Path:
        resolved = self._positive_number(number, field="produced PDF")
        entries = self.list_produced_pdfs(client_id, engagement_id, run_id)
        entry = next(
            (item for item in entries if item.get("number") == resolved),
            None,
        )
        if not isinstance(entry, Mapping):
            raise PaidAuditNotFoundError(f"Produced client-report PDF {resolved} was not found")
        expected_name = f"client-report-{resolved:04d}.pdf"
        if entry.get("filename") != expected_name or not isinstance(entry.get("sha256"), str):
            raise PaidAuditError(
                "The produced client-report index is invalid",
                code="client_report_storage_error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        directory = (
            self.client_report_dir(client_id, engagement_id, run_id) / "produced"
        ).resolve()
        path = (directory / expected_name).resolve()
        if directory not in path.parents or not path.is_file():
            raise PaidAuditNotFoundError(f"Produced client-report PDF {resolved} was not found")
        if _sha256_file(path) != entry["sha256"]:
            raise PaidAuditError(
                "Produced client-report PDF failed its SHA-256 check",
                code="client_report_artifact_mismatch",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return path

    # -- report / production files ----------------------------------------

    def _report_dir(self, client_id: str, engagement_id: str, run_id: str) -> Path:
        return (
            self.data_dir
            / "reports"
            / _safe_segment(client_id, field="client")
            / _safe_segment(engagement_id, field="engagement")
            / _safe_segment(run_id, field="run")
        )

    def _page_audit_brief_path(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> Path:
        results_root = (self.data_dir / "page-audit" / "results").resolve()
        run_dir = (
            results_root
            / _safe_segment(client_id, field="client")
            / _safe_segment(engagement_id, field="engagement")
            / _safe_segment(run_id, field="run")
        ).resolve()
        if results_root not in run_dir.parents:
            raise PaidAuditNotFoundError("Page-audit brief path escapes its results directory")
        path = (run_dir / REPORT_KINDS["ai-brief.html"]).resolve()
        if run_dir not in path.parents:
            raise PaidAuditNotFoundError("Page-audit brief path escapes its run directory")
        return path

    def _productions_root(self, client_id: str, engagement_id: str, run_id: str) -> Path:
        return (
            self.data_dir
            / "productions"
            / _safe_segment(client_id, field="client")
            / _safe_segment(engagement_id, field="engagement")
            / _safe_segment(run_id, field="run")
        )

    def _report_path_from_export(
        self,
        document: Mapping,
        client_id: str,
        engagement_id: str,
        run_id: str,
        alias: str,
    ) -> Path:
        reports = document.get("reports")
        locator = reports.get(alias) if isinstance(reports, Mapping) else None
        if not isinstance(locator, Mapping):
            raise PaidAuditNotFoundError(f"This audit has no {alias} yet")
        relative_path = locator.get("relative_path")
        expected_sha = locator.get("sha256")
        if not isinstance(relative_path, str) or not relative_path or not isinstance(expected_sha, str) or not expected_sha:
            raise PaidAuditError(
                f"Paid audit engine returned an invalid locator for {alias}",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        reports_root = (self.data_dir / "reports").resolve()
        run_dir = self._report_dir(client_id, engagement_id, run_id).resolve()
        path = (reports_root / relative_path).resolve()
        if (
            reports_root not in run_dir.parents
            or run_dir not in path.parents
            or reports_root not in path.parents
        ):
            raise PaidAuditNotFoundError("Report path escapes its run directory")
        if not path.is_file():
            raise PaidAuditNotFoundError(f"This audit has no {alias} yet")
        if _sha256_file(path) != expected_sha:
            raise PaidAuditError(
                f"Engine report {alias!r} failed its SHA-256 check",
                code="engine_artifact_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        return path

    def _legacy_report_path(
        self, client_id: str, engagement_id: str, run_id: str, alias: str
    ) -> Path:
        """Temporary pinned-layout fallback; remove with MOD-027."""
        report_dir = self._report_dir(client_id, engagement_id, run_id).resolve()
        path = (report_dir / REPORT_KINDS[alias]).resolve()
        if report_dir not in path.parents:
            raise PaidAuditNotFoundError("Report path escapes its run directory")
        if not path.is_file():
            raise PaidAuditNotFoundError(f"This audit has no {alias} yet")
        return path

    def report_artifact(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        alias: str,
        *,
        legacy_layout: bool = False,
    ) -> Path:
        filename = REPORT_KINDS.get(alias)
        if filename is None:
            raise PaidAuditNotFoundError(f"Unknown report kind: {alias!r}")
        if alias == "ai-brief.html":
            path = self._page_audit_brief_path(client_id, engagement_id, run_id).resolve()
        else:
            try:
                export_document = self.export_run(
                    client_id, engagement_id, run_id
                )
            except PaidAuditError:
                if not legacy_layout:
                    raise
                # One compatibility window only: callers must opt in explicitly.
                path = self._legacy_report_path(
                    client_id, engagement_id, run_id, alias
                )
            else:
                try:
                    path = self._report_path_from_export(
                        export_document,
                        client_id,
                        engagement_id,
                        run_id,
                        alias,
                    )
                except PaidAuditNotFoundError:
                    if not legacy_layout:
                        raise
                    path = self._legacy_report_path(
                        client_id, engagement_id, run_id, alias
                    )
        if not path.is_file():
            raise PaidAuditNotFoundError(f"This audit has no {alias} yet")
        return path

    def production_artifact(
        self, client_id: str, engagement_id: str, run_id: str, production_id: str, alias: str
    ) -> Path:
        fallback_filename = PRODUCTION_ARTIFACT_KINDS.get(alias)
        if fallback_filename is None:
            raise PaidAuditNotFoundError(f"Unknown report kind: {alias!r}")
        prod_dir = (
            self._productions_root(client_id, engagement_id, run_id)
            / _safe_segment(production_id, field="production")
        ).resolve()
        root = self._productions_root(client_id, engagement_id, run_id).resolve()
        if root not in prod_dir.parents and prod_dir != root and root != prod_dir.parent:
            raise PaidAuditNotFoundError("Production path escapes its run directory")
        manifest_path = prod_dir / "production.json"
        manifest = None
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(parsed, Mapping):
                manifest = parsed
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # Historical productions predate manifests; keep them immutable and
            # use the pinned filename map without writing a replacement manifest.
            manifest = None
        expected_sha = None
        if manifest is None:
            filename = fallback_filename
        else:
            files = manifest.get("files")
            info = files.get(alias) if isinstance(files, Mapping) else None
            if not isinstance(info, Mapping) or not isinstance(info.get("filename"), str) or not info.get("filename"):
                raise PaidAuditNotFoundError(f"This production has no {alias}")
            filename = info["filename"]
            if info.get("sha256") is not None:
                expected_sha = str(info["sha256"])
                if not expected_sha:
                    raise PaidAuditError(
                        f"Production artifact {alias!r} has an invalid SHA-256 manifest value",
                        code="production_artifact_mismatch",
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
        path = (prod_dir / filename).resolve()
        if prod_dir not in path.parents:
            raise PaidAuditNotFoundError("Production path escapes its production directory")
        if not path.is_file():
            raise PaidAuditNotFoundError(f"This production has no {alias}")
        if expected_sha and _sha256_file(path) != expected_sha:
            raise PaidAuditError(
                f"Production artifact {alias!r} failed its SHA-256 check",
                code="production_artifact_mismatch",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return path

    # -- produce (one-click, no gate, non-destructive) --------------------

    def produce(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        *,
        actor: str = "founder",
        note: str = "",
        attestation: str | None = None,
    ) -> dict:
        """Create a timestamped, immutable production copy of the engine's packet.

        One click, no approval gate. The working run is never modified, so this is
        fully repeatable. We copy the engine's own finished, self-contained
        outputs byte-for-byte and add only a separate disclosure sidecar — we do
        not re-derive, strip, or add packet content — so the engine's honesty
        stays intact.
        """
        client_id = _safe_segment(client_id, field="client")
        engagement_id = _safe_segment(engagement_id, field="engagement")
        run_id = _safe_segment(run_id, field="run")

        actor_text = str(actor or "")
        attestation_actor: str | None = None
        if attestation is not None:
            if attestation != PRODUCTION_REVIEW_ATTESTATION:
                raise PaidAuditUnprocessableError(
                    f"Production attestation must be exactly {PRODUCTION_REVIEW_ATTESTATION!r}.",
                    code="invalid_production_attestation",
                )
            attestation_actor = actor_text.strip()
            if not attestation_actor:
                raise PaidAuditUnprocessableError(
                    "A nonblank actor is required with a production attestation.",
                    code="production_attestation_actor_required",
                )

        report_dir = self._report_dir(client_id, engagement_id, run_id)
        export_document = self.export_run(client_id, engagement_id, run_id)
        available = {}
        for alias in PRODUCTION_REPORT_KINDS:
            try:
                available[alias] = self._report_path_from_export(
                    export_document, client_id, engagement_id, run_id, alias
                )
            except PaidAuditNotFoundError:
                continue
        if not available:
            raise PaidAuditNotFoundError(
                "This audit has not produced a review packet yet. Run it to human_review first."
            )

        # Best-effort provenance snapshot from the engine (never blocks producing).
        engine_snapshot: dict = {"available": False}
        try:
            status = self.status(client_id, engagement_id, run_id)
            bundles = self.review_bundles(client_id, engagement_id, run_id)
            engine_snapshot = {
                "available": True,
                "state": (status.get("run") or {}).get("state") if isinstance(status.get("run"), Mapping) else status.get("state"),
                "review_bundles": bundles,
                "status": status,
            }
        except PaidAuditError as exc:
            engine_snapshot = {"available": False, "error": str(exc)}

        production_id = f"production-{_stamp()}"
        prod_dir = self._productions_root(client_id, engagement_id, run_id) / production_id
        prod_dir.mkdir(parents=True, exist_ok=True)

        files: dict[str, dict] = {}
        for alias, source in available.items():
            target = prod_dir / source.name
            shutil.copy2(source, target)
            files[alias] = {
                "filename": source.name,
                "sha256": _sha256_file(target),
                "bytes": target.stat().st_size,
                "path": self._display(target),
            }

        disclosure_path = prod_dir / AI_ASSISTANCE_DISCLOSURE_FILENAME
        disclosure_path.write_text(
            f"{AI_ASSISTANCE_DISCLOSURE_LABEL}\n\n{AI_ASSISTANCE_DISCLOSURE_STATEMENT}\n",
            encoding="utf-8",
        )
        disclosure_file = {
            "filename": disclosure_path.name,
            "sha256": _sha256_file(disclosure_path),
            "bytes": disclosure_path.stat().st_size,
            "path": self._display(disclosure_path),
        }
        files[AI_ASSISTANCE_DISCLOSURE_ALIAS] = disclosure_file
        disclosure = {
            "label": AI_ASSISTANCE_DISCLOSURE_LABEL,
            "statement": AI_ASSISTANCE_DISCLOSURE_STATEMENT,
            "advisory_brief_included": False,
            "ai_authored_advisory_content_in_packet": False,
            "sidecar": disclosure_file,
        }
        produced_at = utc_now()
        review_attestation = None
        if attestation_actor is not None:
            review_attestation = {
                "token": PRODUCTION_REVIEW_ATTESTATION,
                "statement": PRODUCTION_REVIEW_STATEMENT,
                "actor": attestation_actor,
                "attested_at": produced_at,
            }

        manifest = {
            "production_id": production_id,
            "client_id": client_id,
            "engagement_id": engagement_id,
            "run_id": run_id,
            "produced_at": produced_at,
            "produced_by": actor_text or "founder",
            "note": str(note or ""),
            "source_report_dir": self._display(report_dir),
            "files": files,
            "ai_assistance_disclosure": disclosure,
            "review_attestation": review_attestation,
            "engine_snapshot": engine_snapshot,
            # We copy the engine's verbatim, already-honest packet. No coverage
            # ratio is computed or added here (engine invariant #5).
            "provenance": "verbatim_engine_packet",
        }
        write_json(prod_dir / "production.json", manifest)

        prod_record = {
            "production_id": production_id,
            "produced_at": manifest["produced_at"],
            "produced_by": manifest["produced_by"],
            "note": manifest["note"],
            "dir": self._display(prod_dir),
            "files": {alias: info["filename"] for alias, info in files.items()},
            "ai_assistance_disclosure": disclosure,
            "review_attestation": review_attestation,
            "downloads": {
                alias: f"/api/paid-audits/{client_id}/{engagement_id}/{run_id}/productions/{production_id}/{alias}"
                for alias in files
            },
        }

        record = self._find_record(client_id, engagement_id, run_id) or {
            "client_id": client_id,
            "engagement_id": engagement_id,
            "run_id": run_id,
        }
        productions = list(record.get("productions") or [])
        productions.append(prod_record)
        record["productions"] = productions
        self.record_run(record)
        return prod_record

    # -- delete (dashboard record + this run's reports only) --------------

    def delete(self, client_id: str, engagement_id: str, run_id: str) -> dict:
        """Remove the dashboard record and this run's reports/productions.

        The engine's shared, content-addressed evidence vault (``artifacts/``) and
        its SQLite state are deliberately left intact — deleting them could corrupt
        other audits that reference the same evidence (engine invariant #4).
        """
        client_id = _safe_segment(client_id, field="client")
        engagement_id = _safe_segment(engagement_id, field="engagement")
        run_id = _safe_segment(run_id, field="run")

        removed: list[str] = []
        data_root = self.data_dir.resolve()
        for directory in (
            self._report_dir(client_id, engagement_id, run_id),
            self._productions_root(client_id, engagement_id, run_id),
        ):
            resolved = directory.resolve()
            if data_root not in resolved.parents:
                # Never delete anything outside data/paid-audit.
                continue
            if resolved.exists():
                shutil.rmtree(resolved)
                removed.append(self._display(directory))

        with self._lock:
            records = self._read_index()
            key = (client_id, engagement_id, run_id)
            kept = [row for row in records if self._key(row) != key]
            found = len(kept) != len(records)
            self._write_index(kept)

        return {
            "deleted": True,
            "found": found,
            "removed": removed,
            "kept_evidence": True,
            "client_id": client_id,
            "engagement_id": engagement_id,
            "run_id": run_id,
        }

    # -- misc --------------------------------------------------------------

    def _display(self, path: Path) -> str:
        path = Path(path)
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve()))
        except ValueError:
            return str(path)
