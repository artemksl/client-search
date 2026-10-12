"""Restricted background job runner for the local control UI."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from clinic_geo.execution.measurement_platforms import (
    deferred_measurement_reason, require_active_measurement,
)
from clinic_geo.execution.measurement_inputs import FrozenExecutionInputs, FrozenExecutionRequest
from clinic_geo.integrations.visibility_selection import (
    DETACHED_CLASSIFICATION_OPTION_KEYS,
    RUNNER_EXECUTION_POLICY_VERSION,
    RUNNER_SNAPSHOT_PLATFORMS,
    _detached_classification_option_paths,
    _mapping_projection,
    _normalize_new_snapshot_platforms,
    _reject_detached_classification_options,
    measurement_from_frozen_snapshot,
)
from clinic_geo.integrity import canonical_sha256
from clinic_geo.paths import repo_root, resolve_repo_relative
from clinic_geo.persistence.json_store import read_json_or_none, write_json
from clinic_geo.execution.worker_registry import (
    RegistryError, WorkerRegistry, WorkerWatchdog, publish_json, read_record,
    discover_legacy_worker,
)
from clinic_geo.web.config import (
    default_profile_ids,
    selected_profile_rows,
    selected_snapshot_profile_rows,
    write_chrome_profile_snapshot,
    write_logged_in_profile_snapshot,
)
from clinic_geo.web.start_receipts import (
    StartRequestConflict, load_start_receipt, locked_start_receipt,
    rejected_start_path, reserve_start_receipt,
)
from clinic_geo.web.record_navigation import (
    PLATFORM_URLS, RecordNavigationError, conversation_url, latest_conversation_reference,
    launch_inspection, verified_profile_rows,
)
from clinic_geo.platforms import FULL_AUDIT_PLATFORMS, parse_platforms
from clinic_geo.workflows.paid_audit.api import (
    ScopeError,
    build_engine_scope,
    build_scope,
    discover_node_bin,
    paid_audit_cli_path,
    paid_audit_data_dir,
    paid_audit_engine_env,
    PaidAuditStore,
)


REPO_ROOT = repo_root()
SRC_DIR = REPO_ROOT / "src"


@dataclass(frozen=True)
class Workflow:
    id: str
    label: str
    section: str
    group: str
    command: List[str]
    opens_browser: bool
    supports_project: bool = False
    supports_runs: bool = False
    supports_audit_platforms: bool = False
    default_regime: str = "rotating"
    default_project: bool = False
    accepts_cli_flags: bool = True
    runner_owned_snapshot: bool = False


def _free_audit_workflow(
    *,
    workflow_id: str,
    label: str,
    command: List[str],
    runner_owned_snapshot: bool,
) -> Workflow:
    return Workflow(
        id=workflow_id,
        label=label,
        section="client_work",
        group="Visibility Snapshot",
        command=list(command),
        opens_browser=True,
        supports_project=True,
        supports_runs=True,
        supports_audit_platforms=True,
        default_regime="logged_in",
        runner_owned_snapshot=runner_owned_snapshot,
    )


RUNNER_FREE_AUDIT_WORKFLOW = _free_audit_workflow(
    workflow_id="free_audit_stage1",
    label="Free Visibility Snapshot",
    command=[sys.executable, "scripts/run_visibility_measurement.py"],
    runner_owned_snapshot=True,
)


# Freeze production response/reconciliation bounds into every new runner-owned
# Snapshot plan.  The runner adds this response envelope to the separate
# worker_timeout_seconds base; max_attempts remains the existing send ceiling.
#
# Basis (founder decision 2026-09-09).  Serial timings on job
# 20260908T184653Z-c271ee31 were first turn up to 125 s, second turn up to
# 33 s, Google search within 6 s.  Running many tabs at once inflates all of
# them: on job 20260909T165816Z-97086d6a (63 tabs) first turns reached 125 s,
# second turns 96 s and Google searches 42 s, so the deadlines below carry
# headroom for a loaded machine.  A send that has not settled by its deadline
# goes to the runner's read-only reconciliation (30 s grace) instead of
# holding its profile/provider tab for the previous 900 s bound.
# Seconds between owned-tab waves within one profile/provider pair.
PACKET_START_STAGGER_SECONDS = 10

RUNNER_SNAPSHOT_RETRY_POLICY = {
    "enabled": True,
    "max_attempts": 2,
    "late_response_grace_seconds": 30,
    "backoff_seconds": 5,
    "response_timeouts": {
        "chatgpt_turn_1_seconds": 240,
        "chatgpt_turn_2_seconds": 240,
        "gemini_turn_1_seconds": 240,
        "gemini_turn_2_seconds": 240,
        "google_ai_overview_seconds": 90,
        "gaio_gemini_parser_seconds": 240,
    },
}


WORKFLOWS: Dict[str, Workflow] = {
    "client_research": Workflow(
        id="client_research", label="Client research", section="client_work",
        group="Client research", command=[sys.executable, "scripts/run_visibility_measurement.py"],
        opens_browser=True, supports_runs=True, supports_audit_platforms=True,
        default_regime="logged_in", runner_owned_snapshot=True,
    ),
    "smoke": Workflow(
        id="smoke",
        label="Smoke",
        section="client_work",
        group="Local",
        command=[sys.executable, "-m", "clinic_geo.cli.smoke", "--fixture", "tests/fixtures/smoke"],
        opens_browser=False,
    ),
    "free_audit_stage1": RUNNER_FREE_AUDIT_WORKFLOW,
    "paid_audit": Workflow(
        id="paid_audit",
        label="Diagnostic Audit (paid)",
        section="client_work",
        group="Diagnostic Audit",
        # The real command is built at launch from the resolved Node binary and the
        # engine CLI (see _paid_audit_command); the base list is intentionally empty.
        command=[],
        opens_browser=False,
        accepts_cli_flags=False,
    ),
    "database_complete_build": Workflow(
        id="database_complete_build",
        label="Build Complete Database",
        section="database",
        group="Complete Database (Recommended)",
        command=[
            sys.executable,
            "-m",
            "clinic_geo.workflows.database.complete_database",
            "--publish",
        ],
        opens_browser=False,
    ),
    "database_search_clinics": Workflow(
        id="database_search_clinics",
        label="Search For Clinics",
        section="database",
        group="1. Search For Clinics",
        command=["bash", "src/clinic_geo/workflows/database/finding_clinics/run.sh"],
        opens_browser=True,
        supports_project=True,
    ),
    "database_matching": Workflow(
        id="database_matching",
        label="Match Existing Database",
        section="database",
        group="2. Match Existing Database",
        command=["bash", "src/clinic_geo/workflows/database/matching/run.sh"],
        opens_browser=False,
    ),
    "database_enriching": Workflow(
        id="database_enriching",
        label="Enrich Missing Clinics",
        section="database",
        group="3. Enrich Missing Clinics",
        command=["bash", "src/clinic_geo/workflows/database/enriching/run.sh"],
        opens_browser=True,
        supports_project=True,
        default_regime="logged_in",
    ),
    "database_merge_new_clinics": Workflow(
        id="database_merge_new_clinics",
        label="Merge New Clinics",
        section="database",
        group="4. Merge New Clinics",
        command=[sys.executable, "-m", "clinic_geo.workflows.database.merge_old_and_new_data"],
        opens_browser=False,
    ),
    "database_tagging": Workflow(
        id="database_tagging",
        label="Tag Clinic Areas",
        section="database",
        group="5. Tag Clinic Areas",
        command=["bash", "src/clinic_geo/workflows/tagging/run.sh"],
        opens_browser=True,
        default_regime="rotating",
        accepts_cli_flags=False,
    ),
    "database_prospect_scoring": Workflow(
        id="database_prospect_scoring",
        label="Score Prospects",
        section="database",
        group="6. Score Prospects",
        command=[sys.executable, "-m", "clinic_geo.workflows.database.prospect_scoring"],
        opens_browser=False,
    ),
    "clinic_analysis": Workflow(
        id="clinic_analysis",
        label="Close Clinic Analysis",
        section="client_work",
        group="Clinic Analysis",
        command=["bash", "src/clinic_geo/workflows/clinic_analysis/run.sh"],
        opens_browser=True,
        default_regime="logged_in",
        accepts_cli_flags=False,
    ),
}


DEFAULT_RUN_CONFIG = {
    "vpn_enabled": True,
    "relaunch": False,
    "regime": "rotating",
    "project_enabled": False,
    "project_url": "",
    "intelligence_level": "",
    "profile_count": 1,
    "selected_profile_ids": [],
    # Deprecated request/response alias. New callers use selected_profile_ids.
    "selected_profiles": [],
    "parallel_tabs": 3,
    # Runner-owned Snapshot: owned tabs per profile per platform, one chat
    # each, all at the same time (founder decision 2026-09-09; lowered from 3
    # to 2 after 63 tabs saturated the machine).
    "tabs_per_profile": 2,
    "max_prompts_per_profile": 3,
    "runs": "single",
    "audit_platforms": ["chatgpt"],
}


AUDIT_RESULT_SOURCES = {
    "free_audit_stage1": {
        "stage": "snapshot",
        "sources": [
            Path("data/free-audit/data/share_of_voice.json"),
            Path("data/free-audit/data/analysis_result.json"),
        ],
    },
}


@dataclass
class Job:
    id: str
    workflow_id: str
    label: str
    command: List[str]
    status: str
    returncode: Optional[int]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    log_file: str
    run_config: dict = field(default_factory=dict)
    pid: Optional[int] = None
    stop_requested_at: Optional[str] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_job_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )


def _is_snapshot_workflow(workflow: Workflow) -> bool:
    return workflow.runner_owned_snapshot or workflow.id == "free_audit_stage1"


def _job_is_runner_owned(job: Job) -> bool:
    """Use frozen job identity, not the mutable default workflow registry."""

    config = job.run_config if isinstance(job.run_config, dict) else {}
    return bool(
        config.get("runner_owned_snapshot") is True
        or (
            config.get("runner_runtime")
            and config.get("runner_measurement")
            and str(config.get("run_id") or "") == job.id
        )
    )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def visibility_measurement_plan_command(
    workflow: Workflow,
    measurement_path: Path,
    runtime_path: Path,
    run_id: str,
    preflight_path: Path,
) -> List[str]:
    if not workflow.runner_owned_snapshot:
        raise ValueError("Visibility measurement planning requires a runner workflow")
    return [
        *workflow.command,
        "--measurement",
        str(measurement_path),
        "--runtime",
        str(runtime_path),
        "--run-id",
        run_id,
        "--mode",
        "plan",
        "--preflight-output",
        str(preflight_path),
    ]


class JobStore:
    def __init__(
        self,
        repo_root: Path = REPO_ROOT,
        run_root: Optional[Path] = None,
        workflows: Optional[Dict[str, Workflow]] = None,
        measurement_component_factory: Optional[Callable[[Mapping[str, object]], object]] = None,
    ):
        self.repo_root = Path(repo_root)
        self.run_root = Path(run_root) if run_root else self.repo_root / "data" / "ui-runs"
        self.workflows_map = dict(WORKFLOWS if workflows is None else workflows)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._processes: Dict[str, subprocess.Popen] = {}
        self._stop_requests = set()
        self._runner_cancel_pending: set[tuple[str, str]] = set()
        self._preparing_executions: set[tuple[str, str]] = set()
        self._measurement_component_factory = measurement_component_factory
        self._read_saved_jobs()

    def workflows(self) -> List[dict]:
        return [asdict(w) for w in self.workflows_map.values() if w.id != "client_research"]

    def start(self, workflow_id: str, run_config: Optional[dict] = None, *, request_id: str | None = None) -> Job:
        if request_id is None:
            # Existing programmatic callers keep their intentional new-job
            # semantics. Browser Starts always supply a persisted delivery ID.
            return self._start_new(workflow_id, run_config)
        fingerprint = canonical_sha256({"workflow_id": workflow_id, "run_config": run_config or {}})
        with locked_start_receipt(self.run_root, request_id) as path:
            receipt = load_start_receipt(path)
            if receipt is not None:
                if receipt.get("request_id") != request_id or receipt.get("fingerprint") != fingerprint:
                    raise StartRequestConflict("start_request_mismatch: this Start request already belongs to different settings")
                job_id = receipt.get("job_id")
                if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                    raise StartRequestConflict("start_receipt_invalid: Start outcome requires review")
                rejected = load_start_receipt(rejected_start_path(path))
                if rejected is not None:
                    raise ValueError(str(rejected.get("error") or "Start validation failed"))
                existing = self.get(job_id)
                if existing is None:
                    # A second server may have loaded its list before this
                    # request was accepted. Do not needlessly require restart.
                    try:
                        existing = Job(**read_record(self.run_root / job_id / "job.json"))
                    except (RegistryError, TypeError, ValueError):
                        raise StartRequestConflict(
                            f"start_interrupted: request reserved job {job_id}, but preparation did not finish; inspect this job before starting another"
                        ) from None
                    with self._lock:
                        self._jobs[job_id] = existing
                if (existing.run_config.get("host_start_request_id") != request_id
                    or existing.run_config.get("host_start_fingerprint") != fingerprint):
                    raise StartRequestConflict("start_job_binding_mismatch: Start outcome requires review")
                return existing
            job_id = new_job_id()
            if workflow_id == "client_research":
                job_id = str((run_config or {}).get("client_research", {}).get("runner_run_id") or "")
                if not re.fullmatch(r"cr_[0-9a-f]{32}", job_id):
                    raise ValueError("Client research requires its reserved runner run ID")
            reserve_start_receipt(path, request_id=request_id, fingerprint=fingerprint, job_id=job_id)
            try:
                return self._start_new(workflow_id, run_config, job_id=job_id,
                                       start_request_id=request_id, start_fingerprint=fingerprint)
            except (KeyError, ValueError) as exc:
                # Only a pre-job rejection is a definite failure. Once saved,
                # a worker may have started even if the response path failed.
                if not (self.run_root / job_id / "job.json").exists():
                    publish_json(rejected_start_path(path), {"error": str(exc)})
                else:
                    raise StartRequestConflict(f"start_outcome_unknown: request reserved job {job_id}; retry the same request") from exc
                raise

    def _start_new(self, workflow_id: str, run_config: Optional[dict] = None, *,
                   job_id: str | None = None, start_request_id: str | None = None,
                   start_fingerprint: str | None = None) -> Job:
        workflow = self.workflows_map.get(workflow_id)
        if workflow is None:
            raise KeyError(f"Unknown workflow: {workflow_id}")
        normalized = normalize_run_config(run_config, workflow, self.repo_root)
        if workflow.runner_owned_snapshot and normalized.get("retry_missing"):
            raise ValueError(
                "Runner-owned Snapshot recovery must use the explicit resume action"
            )
        selected_config = None
        selection_summary = None
        if workflow.id == "client_research":
            selected_config = copy.deepcopy(normalized["client_research"]["measurement"])
        elif workflow.runner_owned_snapshot and normalized.get("visibility_test"):
            selected_config = copy.deepcopy(normalized["visibility_test"])
            selection_summary = {
                "scope_kind": "diagnostic",
                "prompt_count": len(selected_config["diagnostic_prompts"]),
                "platforms": list(selected_config["platforms"]),
                "publication_eligible": False,
            }
        elif _is_snapshot_workflow(workflow) and normalized.get("snapshot_selection"):
            if normalized.get("retry_missing"):
                raise ValueError("A missing-only retry cannot change the frozen branch selection")
            from clinic_geo.web.snapshot_production import (
                build_area_selected_snapshot_config,
                build_database_selected_snapshot_config,
            )

            # A normal production selection always starts from the controlled
            # workspace config. Browser-supplied paths cannot replace it.
            source_path = free_audit_config_path(self.repo_root)
            source_config = read_json_or_none(source_path)
            if not isinstance(source_config, dict):
                raise ValueError(f"Snapshot source config is unavailable: {source_path}")
            production_selection = copy.deepcopy(normalized["snapshot_selection"])
            production_selection["platforms"] = list(normalized["audit_platforms"])
            production_selection["runs"] = normalized["runs"]
            selection_builder = (
                build_area_selected_snapshot_config
                if production_selection.get("scope_kind") == "area_treatments"
                else build_database_selected_snapshot_config
            )
            selected_config, selection_summary = selection_builder(
                self.repo_root,
                source_config,
                production_selection,
            )
        elif workflow.runner_owned_snapshot:
            frozen_source = str(normalized.get("snapshot_config") or "").strip()
            if not frozen_source:
                raise ValueError(
                    "Runner-owned Snapshot launch requires a frozen Snapshot selection"
                )
            source_path = resolve_repo_relative(frozen_source, base=self.repo_root)
            selected_config = read_json_or_none(source_path)
            if not isinstance(selected_config, dict):
                raise ValueError(f"Frozen Snapshot config is unavailable: {source_path}")
        if workflow.runner_owned_snapshot and selected_config is not None:
            selected_config = copy.deepcopy(selected_config)
            if workflow.id == "client_research":
                from clinic_geo.integrations.client_research_selection import measurement_from_client_research
                measurement_from_client_research(selected_config)
            else:
                from clinic_geo.web.snapshot_production import snapshot_run_mode
                selected_config.update(snapshot_run_mode({"runs": normalized["runs"]}))
                measurement_from_frozen_snapshot(selected_config)
        job_id = job_id or new_job_id()
        if start_request_id is not None:
            normalized.update(host_start_request_id=start_request_id, host_start_fingerprint=start_fingerprint)
        job_dir = self.run_root / job_id
        # This exclusive directory creation is the new-run uniqueness guard.
        # It deliberately precedes planning or any runner/browser action;
        # Runner.inspect() is never used as an admission check.
        job_dir.mkdir(parents=True, exist_ok=False)
        if selected_config is not None:
            selected_path = job_dir / "snapshot-config.json"
            write_json(selected_path, selected_config)
            normalized["snapshot_config"] = relative_display(selected_path, self.repo_root)
            if workflow.runner_owned_snapshot and workflow.id != "client_research":
                normalized["runner_host_snapshot_policy_version"] = 1
                normalized["runner_snapshot_config_sha256"] = file_sha256(selected_path)
            if selection_summary is not None:
                normalized["snapshot_selection_summary"] = selection_summary
        if workflow.runner_owned_snapshot:
            self._prepare_visibility_measurement_job(
                normalized,
                job_id,
                job_dir,
                workflow,
            )
        if workflow.id == "paid_audit":
            self._prepare_paid_audit_job(normalized, job_id, job_dir)
        log_file = Path(normalized.get("runner_execution_log") or job_dir / "stdout.log")
        command = build_command(workflow, normalized)
        job = Job(
            id=job_id,
            workflow_id=workflow.id,
            label=(
                f"Visibility test · {normalized['audit_platforms'][0]} · "
                f"{len(normalized['visibility_test']['diagnostic_prompts'])} prompts"
                if normalized.get("visibility_test") else workflow.label
            ),
            command=command,
            status="queued",
            returncode=None,
            created_at=utc_now(),
            started_at=None,
            finished_at=None,
            log_file=str(log_file.relative_to(self.repo_root) if log_file.is_relative_to(self.repo_root) else log_file),
            run_config=normalized,
        )
        if _job_is_runner_owned(job):
            publish_json(job_dir / "job-origin.json", asdict(job))
        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        thread = threading.Thread(target=self._run_job, args=(job, workflow), daemon=True)
        thread.start()
        return job

    def stop(self, job_id: str, *, execution_id: str | None = None) -> Job:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if _job_is_runner_owned(current):
            return self.cancel(job_id, execution_id=execution_id)
        stopped_at = utc_now()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in {"queued", "running", "stopping"}:
                return job
            self._stop_requests.add(job_id)
            job.status = "stopping"
            job.stop_requested_at = stopped_at
            self._save(job)
            proc = self._processes.get(job_id)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
        return self.get(job_id)

    def resume(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if _job_is_runner_owned(job):
            measurement, _ = self._verified_measurement_inputs(job)
            require_active_measurement(measurement)
            component = self._measurement_component(job)
            inspected = component.inspect(job.id)
            self._validate_measurement_result_binding(job, inspected)
            return self._launch_existing_measurement(job, mode="resume", request_id=str(uuid.uuid4()))
        return self.start(job.workflow_id, job.run_config)

    def plan_refetch(self, job_id: str, *, request_id: str) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Refetch is available only for runner-owned measurements")
        measurement, runtime = self._verified_measurement_inputs(job)
        require_active_measurement(measurement)
        component = self._component_from_runtime(runtime)
        try:
            return component.plan_refetch(job.id, request_id=request_id)
        except Exception as exc:
            from visibility_runner.api import RunNotFoundError
            if not isinstance(exc, RunNotFoundError):
                raise
            self._require_uncreated_measurement(job, component)
            plan, revision = self._initial_remaining_plan(job, component)
            return {"kind": "initial_remaining", "request_id": request_id, "run_id": job.id, "plan_sha256": plan.plan_sha256, "revision_digest": revision}

    def refetch(self, job_id: str, request: Mapping) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job) or not isinstance(request, Mapping):
            raise ValueError("Invalid Refetch request")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Refetch requires its preview request identity")
        if request.get("kind") == "initial_remaining":
            return self._launch_existing_measurement(job, mode="run", request_id=request_id, recovery=request)
        return self._launch_existing_measurement(job, mode="refetch", request_id=request_id, recovery=request)

    def _initial_remaining_plan(self, job: Job, component, *, measurement=None):
        registry = WorkerRegistry(self.run_root / job.id)
        requests = registry.requests()
        if not requests:
            raise RegistryError("initial_execution_not_proven_unsent")
        marks = []
        for request in requests:
            directory = registry.path(request["execution_id"])
            if any((directory / name).exists() for name in ("worker.json", "launch.json", "exit.json")):
                raise RegistryError("initial_execution_not_proven_unsent")
            marker = read_record(directory / "launch-failure.json", optional=True)
            if marker is None:
                # A dead managed launcher cannot write a failure marker. Its
                # intact guard plus absent spawn intent is an independent,
                # restart-safe proof that this request never reached Popen.
                # observe() verifies death before re-reading the spawn boundary
                # and rejects legacy, uncertain, corrupt, or worker-bearing
                # records. Bind the preview to immutable proof, not sample time.
                observation = registry.observe(request["execution_id"])
                if (observation.get("state") != "stopped"
                    or observation.get("reason") != "launcher_abandoned_before_spawn"):
                    raise RegistryError("initial_execution_not_proven_unsent")
                marks.append({"request": request["request_sha256"],
                              "launch_guard_sha256": request["launch_guard_sha256"],
                              "abandoned_before_spawn": True})
                continue
            cancelled_before_spawn = marker.get("reason") == "cancelled_before_spawn"
            failed_before_spawn = (
                marker.get("reason") == "worker_launch_failed"
                and marker.get("spawn_status") == "not_started"
                and marker.get("request_sha256") == request["request_sha256"]
            )
            if marker.get("execution_id") != request["execution_id"] or not (
                cancelled_before_spawn or failed_before_spawn
            ):
                raise RegistryError("initial_execution_not_proven_unsent")
            marks.append({"request": request["request_sha256"], "marker": marker})
        if measurement is None:
            measurement, _ = self._verified_measurement_inputs(job)
        plan = component.plan(measurement, run_id=job.id)
        if plan.plan_sha256 != job.run_config["runner_plan_sha256"]:
            raise RegistryError("initial_plan_changed")
        return plan, canonical_sha256(marks)

    @staticmethod
    def _require_uncreated_measurement(job: Job, component) -> None:
        """A missing plan inside an existing run is corruption, not unsent work."""
        from visibility_runner.api import RunNotFoundError
        try:
            component.ownership(job.id)
        except RunNotFoundError:
            return
        raise RegistryError("initial_run_already_created")

    @staticmethod
    def _ownership_release_available(observation: Mapping, ownership: Mapping) -> bool:
        return bool(
            observation.get("state") not in {"launching", "running", "unresponsive"}
            and ownership.get("state") == "unknown"
            and ownership.get("status") == "unknown"
            and ownership.get("reason_code") in {
                "owner_record_corrupt", "process_identity_invalid", "process_identity_incomplete",
                "boot_identity_unavailable", "process_inspection_unavailable",
            }
        )

    @staticmethod
    def _add_failure_context(readout: dict, plan) -> None:
        """Join display context by frozen observation identity, never by array order."""
        packets = {}
        for packet in getattr(plan, "packets", ()):
            if str(getattr(packet.kind, "value", packet.kind)) == "measurement":
                for observation_id in packet.observation_ids:
                    packets[observation_id] = packet
        observations = getattr(plan, "observation_plan", {}).get("observations", ())
        contexts = {}
        platform_ordinals = {}
        for observation in observations:
            context = {key: observation[key] for key in (
                "prompt_text", "prompt_id", "platform", "semantic_prompt_id", "semantic_slot_id",
                "branch_name", "subject_name", "subject_kind", "treatment_name", "area_id",
                "clinic_archetype_singular", "clinic_archetype_plural",
            ) if key in observation}
            context["run_ordinal"] = observation.get("run")
            platform = observation.get("platform")
            if platform:
                # Number the frozen plan, so filtering, recovery, and readout
                # reordering cannot rename an observation in the console.
                platform_ordinals[platform] = platform_ordinals.get(platform, 0) + 1
                context["platform_ordinal"] = platform_ordinals[platform]
            location = observation.get("location_context") or {}
            target = observation.get("target_geography") or location.get("target_geography") or {}
            area_name = target.get("name") or location.get("area")
            geography_tier = observation.get("geography_tier") or location.get("geography_tier")
            if area_name:
                context["area_name"] = area_name
            if geography_tier:
                context["geography_tier"] = geography_tier
            # A city/wider-area prompt may be shared by several selected local
            # areas; their origins are distinct from the actual search area.
            origins = observation.get("origin_branches") or location.get("origin_branches") or ()
            origin_area_names = list(dict.fromkeys(
                origin["local_area_name"] for origin in origins if origin.get("local_area_name")
            ))
            if origin_area_names:
                context["origin_area_names"] = origin_area_names
            packet = packets.get(observation["observation_id"])
            if packet is not None:
                context.update(profile_id=packet.profile_id, packet_id=packet.packet_id)
            contexts[observation["observation_id"]] = context
        readout["items"] = [
            {**item, "context": contexts[item["item_id"]]} if item.get("item_id") in contexts else dict(item)
            for item in readout.get("items", ())
        ]

    def verification_control(self, job_id: str, **command) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Verification requires a runner-owned measurement")
        _, runtime = self._verified_measurement_inputs(job)
        component = self._component_from_runtime(runtime)
        from clinic_geo.browser.browser_lifecycle import shared_browser_lifecycle
        endpoint = runtime.get("browser", {}).get("cdp_url", "http://127.0.0.1:9222")
        with shared_browser_lifecycle(environment={"CDP_URL": endpoint}, blocking=False):
            return component.verification_control(job_id, **command)

    def failure_readout(self, job_id: str) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Failure readout requires a runner-owned measurement")
        registry = WorkerRegistry(self.run_root / job_id)
        execution_id = job.run_config.get("runner_execution_id")
        observation = registry.observe(execution_id) if execution_id else {"state": "unknown", "reason": "legacy_worker_identity_unavailable", "sampled_at": utc_now()}
        if not execution_id:
            legacy = discover_legacy_worker(job.command)
            observation.update(state={"alive": "running", "absent": "stopped"}.get(legacy["state"], "unknown"), reason=legacy["reason"])
        observation["status"] = {"launching": "alive", "running": "alive", "unresponsive": "alive", "finished": "dead", "stopped": "dead"}.get(observation["state"], "unknown")
        observation["reason_code"] = observation.get("reason", "")
        observation["heartbeat_at"] = (observation.get("heartbeat") or {}).get("utc")
        try:
            measurement, runtime = self._verified_measurement_inputs(job)
            component = self._component_from_runtime(runtime)
        except (ValueError, RegistryError, OSError) as exc:
            return {"run_id": job_id, "items": [], "preview": {"selections": []},
                    "host": {"worker_state": observation["state"], "ownership_state": "unknown",
                             "result_available": observation.get("result_available", False),
                             "finalize_available": False, "refetch_available": False,
                             "ownership_release_available": False,
                             "settings_match_current_runtime": False,
                             "blocked_reason": str(exc)}}
        plan = None
        try:
            ownership = component.ownership(job_id)
        except Exception as exc:
            from visibility_runner.api import RunNotFoundError
            if not isinstance(exc, RunNotFoundError):
                raise
            deferred_reason = deferred_measurement_reason(measurement)
            if deferred_reason:
                # No engine evidence exists to inspect. Keep the frozen job
                # readable without recreating its now-deferred Google plan.
                return {"run_id": job_id, "items": [], "preview": {"selections": []},
                        "host": {"worker_state": observation["state"], "ownership_state": "available",
                                 "result_available": observation.get("result_available", False),
                                 "finalize_available": False, "refetch_available": False,
                                 "ownership_release_available": False,
                                 "settings_match_current_runtime": True,
                                 "blocked_reason": deferred_reason}}
            plan, _ = self._initial_remaining_plan(job, component, measurement=measurement)
            ownership = {"state": "available", "status": "dead", "reason_code": "never_spawned"}
        observation["ownership_status"] = ownership.get("state", "unknown")
        if plan is not None:
            readout = dict(component.failure_readout_for_plan(plan, worker_observation=observation))
        else:
            # An existing run with missing initialized artifacts must remain
            # blocked; only ownership's missing-run result authorizes fallback.
            readout = dict(component.failure_readout(job_id, worker_observation=observation))
        context_reason = ""
        if plan is None and callable(getattr(component, "inspect", None)):
            # Display the original scope, including any subsequently deferred
            # platform. Replanning here would apply today's admission policy
            # to historical evidence and could change its denominator.
            try:
                inspected = component.inspect(job.id)
                self._validate_measurement_result_binding(job, inspected)
                plan = inspected.plan
            except Exception:
                context_reason = "The saved measurement plan could not be verified."
        if plan is not None:
            self._add_failure_context(readout, plan)
        # Engine 0.2.1 describes a run whose frozen execution settings differ
        # from the current runtime under ``execution_policy`` but refuses to
        # execute it; the UI must not offer Refetch for such a run.
        policy = readout.get("execution_policy")
        settings_match = not isinstance(policy, Mapping) or policy.get("matches_current_runtime", True) is not False
        deferred_reason = deferred_measurement_reason(measurement)
        saved_scope = getattr(plan, "scope_manifest", {})
        saved_platforms = saved_scope.get("platforms", ()) if isinstance(saved_scope, Mapping) else ()
        if not deferred_reason and (
            "copilot" in (measurement.get("platforms") or ())
            or "copilot" in (saved_platforms or ())
        ):
            # Availability of new Copilot browser runs does not authorize
            # execution of a saved derived/native plan under a new method.
            saved_modes = saved_scope.get("platform_data_modes") if isinstance(saved_scope, Mapping) else None
            if not isinstance(saved_modes, Mapping) or saved_modes.get("copilot") != "copilot_browser_same_chat_v1":
                deferred_reason = (
                    "This saved Copilot run does not declare the restored browser collection method. "
                    "Its evidence remains available for inspection; Refetch is disabled."
                )
        finalize_available = False
        if (
            execution_id
            and observation["state"] in {"stopped", "finished"}
            and not observation.get("result_available")
            and ownership.get("state") == "available"
        ):
            try:
                result = component.inspect(job.id)
                self._validate_measurement_result_binding(job, result)
                raw_status = getattr(result, "status", "")
                finalize_available = str(getattr(raw_status, "value", raw_status)).casefold() == "succeeded"
            except Exception:
                # A repair button needs verified completed measurements, not
                # an inference from absent host output or a dead worker.
                pass
        readout["host"] = {
            "startup_error": (
                "Chrome could not be restarted safely. Open this run's log for details; measurement did not start."
                if (observation.get("completion") or {}).get("error_code") == "browser_startup_failed" else None
            ),
            "worker_state": observation["state"],
            "ownership_state": ownership.get("state", "unknown"),
            "ownership": {key: ownership.get(key) for key in ("state", "status", "reason_code", "execution_id") if ownership.get(key) is not None},
            "ownership_release_available": bool(settings_match and self._ownership_release_available(observation, ownership)),
            "result_available": observation.get("result_available", False),
            "finalize_available": finalize_available,
            "settings_match_current_runtime": settings_match,
            "refetch_available": bool(
                component.recovery_available
                and observation["status"] == "dead"
                and ownership.get("state") == "available"
                and settings_match
                and not deferred_reason
                and not context_reason
                and readout.get("preview", {}).get("selections")
            ),
            **({"blocked_reason": deferred_reason or context_reason} if deferred_reason or context_reason else {}),
        }
        if execution_id:
            # Host containment explains a stopped pass even when the engine's
            # item projection now says only cancelled/dependency blocked.
            # This is diagnostic context, never recovery authorization.
            try:
                containment = read_record(registry.path(execution_id) / "containment.json", optional=True)
            except (RegistryError, OSError):
                containment = None
            if (containment and containment.get("execution_id") == execution_id
                    and containment.get("reason") == "operation_deadline_exceeded"):
                readout["host"]["automatic_stop"] = {
                    "execution_id": execution_id,
                    "reason_code": "operation_deadline_exceeded",
                }
        self._add_record_navigation(readout, runtime, job_id=job_id, plan_available=plan is not None)
        return readout

    def _record_navigation_block_reason(self, readout: Mapping, job_id: str) -> str:
        host = readout.get("host") or {}
        if host.get("worker_state") not in {"finished", "stopped"} or host.get("ownership_state") != "available":
            return "Wait until this run has stopped and its browser ownership is released."
        with self._lock:
            if any(job.id != job_id and job.status in {"queued", "running", "stopping", "finalizing"}
                   for job in self._jobs.values()):
                return "Another job is active. Wait for it to finish before opening this profile."
        return ""

    def _add_record_navigation(self, readout: dict, runtime: dict, *, job_id: str, plan_available: bool) -> None:
        reason = self._record_navigation_block_reason(readout, job_id)
        try:
            profiles = verified_profile_rows(runtime, self.repo_root) if plan_available else {}
        except RecordNavigationError as exc:
            profiles = {}
            reason = str(exc)
        for item in readout.get("items", ()):
            context = item.get("context") or {}
            profile_id, platform = context.get("profile_id"), context.get("platform")
            if platform == "github_copilot":
                item["navigation"] = {
                    "kind": "sdk_transcript", "available": False,
                    "reason": ("Read the saved SDK answer and available Gemini parser evidence."
                               if item.get("source_saved") is True or context.get("source_saved") is True
                               else "No SDK answer was saved; this source has no browser chat."),
                    "profile_id": profile_id or "", "platform": platform,
                }
                continue
            row_reason = reason
            if profile_id not in profiles:
                row_reason = "The saved routine profile cannot be verified."
            elif platform not in PLATFORM_URLS:
                row_reason = "The saved platform cannot be verified."
            item["navigation"] = {
                "kind": "conversation" if latest_conversation_reference(item) else "platform",
                "available": not bool(row_reason), "reason": row_reason,
                "profile_id": profile_id or "", "platform": platform or "",
            }

    def open_record(self, job_id: str, *, item_id: str) -> Mapping:
        """Open a human inspection window without attaching or sending anything."""
        from clinic_geo.browser.browser_lifecycle import BrowserLifecycleLockError, shared_browser_lifecycle

        if not isinstance(item_id, str) or not item_id or len(item_id) > 500:
            raise RecordNavigationError("invalid_item", "Choose a saved measurement record.")
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise RecordNavigationError("unsupported_job", "Chat inspection is available only for area visibility measurements.")
        # Serialize with this job's Refetch admission, then lease the browser
        # exclusively. A live owner of any profile on this endpoint refuses
        # the action before a Chrome process or window can be opened.
        registry = WorkerRegistry(self.run_root / job_id)
        with registry.launch_lock():
            _, runtime = self._verified_measurement_inputs(job)
            endpoint = (runtime.get("browser") or {}).get("cdp_url")
            if not isinstance(endpoint, str) or not endpoint:
                raise RecordNavigationError("browser_unavailable", "The saved browser endpoint cannot be verified.")
            try:
                with shared_browser_lifecycle(environment={"CDP_URL": endpoint}, blocking=False) as lease:
                    with lease.exclusive(blocking=False):
                        readout = self.failure_readout(job_id)
                        matches = [item for item in readout.get("items", ()) if item.get("item_id") == item_id]
                        if len(matches) != 1:
                            raise RecordNavigationError("record_unavailable", "This measurement record is unavailable. Refresh the results and try again.")
                        item = matches[0]
                        navigation = item.get("navigation") or {}
                        if navigation.get("available") is not True:
                            raise RecordNavigationError("record_open_blocked", navigation.get("reason") or "This record cannot be opened right now.")
                        profiles = verified_profile_rows(runtime, self.repo_root)
                        reference = latest_conversation_reference(item)
                        if reference:
                            try:
                                link = self._component_from_runtime(runtime).resolve_recovery_link(job_id, reference)
                                url = conversation_url(link, navigation["platform"])
                            except RecordNavigationError:
                                raise
                            except Exception as exc:
                                raise RecordNavigationError("conversation_unavailable", "The saved chat link cannot be verified. Refresh the results and try again.") from exc
                        else:
                            url = PLATFORM_URLS[navigation["platform"]]
                        launch_inspection(root=self.repo_root, runtime=runtime,
                                          profile=profiles[navigation["profile_id"]], url=url)
                        return navigation
            except BrowserLifecycleLockError as exc:
                raise RecordNavigationError("browser_busy", "Chrome is being used by another job. Wait for it to finish before opening this profile.") from exc

    def release_stale_ownership(self, job_id: str, *, reason: str) -> Mapping:
        """Operator release of a blocked owner record; refused for a live worker."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Ownership release is available only for runner-owned measurements")
        reason = str(reason or "").strip()
        if len(reason) < 8 or len(reason) > 500:
            raise ValueError("A release reason of 8 to 500 characters is required")
        registry = WorkerRegistry(self.run_root / job_id)
        execution_id = job.run_config.get("runner_execution_id")
        if execution_id:
            observation = registry.observe(execution_id)
            if observation.get("state") in {"launching", "running", "unresponsive"}:
                raise ValueError("The measurement worker is still alive; stop it before releasing ownership")
        component = self._measurement_component(job)
        outcome = dict(component.release_stale_ownership(job_id, reason=reason))
        try:
            with self._log_path(job).open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"[{utc_now()}] ownership release requested by operator: {reason} -> released={outcome.get('released')}\n")
        except OSError:
            pass
        return outcome

    def resolve_recovery_link(self, job_id: str, reference: str) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not isinstance(reference, str) or len(reference) > 500:
            raise ValueError("Invalid evidence reference")
        component = self._measurement_component(job)
        link = component.resolve_recovery_link(job_id, reference)
        if link.get("kind") == "artifact":
            # Public artifact verification occurs before returning user-
            # requested evidence. Rendering the summary never reads bodies.
            payload = component.read_artifact(job_id, link["reference"])
            return {"kind": "artifact", "text": payload.decode("utf-8", errors="replace")}
        if link.get("kind") == "conversation":
            return {"kind": "conversation", "url": link["url"]}
        raise ValueError("Unsupported recovery link")

    @staticmethod
    def _parsed_recovery(job_id: str, mode: str, request_id: str, recovery: Mapping | None):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("request_id is required")
        if mode not in {"run", "refetch"}:
            if recovery is not None:
                raise ValueError("Unexpected recovery payload")
            return None
        if not isinstance(recovery, Mapping) or recovery.get("run_id") != job_id or recovery.get("request_id") != request_id:
            raise ValueError("Recovery request does not belong to this job")
        if mode == "run":
            if set(recovery) != {"kind", "request_id", "run_id", "plan_sha256", "revision_digest"} or recovery.get("kind") != "initial_remaining":
                raise ValueError("Invalid initial recovery request")
            return dict(recovery)
        from visibility_runner.recovery.api import RecoveryRequest
        try:
            return RecoveryRequest.from_dict(recovery).to_dict()
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise ValueError("Invalid recovery request") from exc

    def _accepted_execution(self, job_id: str, execution_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        # Response-only metadata; dataclass persistence remains the current job.
        job.accepted_execution_id = execution_id
        return job

    def finalize_output(self, job_id: str, *, request_id: str, expected_execution_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job) or not isinstance(expected_execution_id, str) or not expected_execution_id:
            raise RegistryError("execution_changed")
        return self._launch_existing_measurement(job, mode="finalize", request_id=request_id,
                                                 expected_execution_id=expected_execution_id)

    def _launch_existing_measurement(self, job: Job, *, mode: str, request_id: str,
                                     recovery: Mapping | None = None,
                                     expected_execution_id: str | None = None) -> Job:
        if mode != "finalize":
            measurement, _ = self._verified_measurement_inputs(job)
            require_active_measurement(measurement)
        recovery = self._parsed_recovery(job.id, mode, request_id, recovery)
        if mode == "finalize":
            expected_execution_id = expected_execution_id or job.run_config.get("runner_execution_id")
            if not expected_execution_id:
                raise RegistryError("execution_changed")
        registry = WorkerRegistry(self.run_root / job.id)
        with registry.launch_lock():
            existing = registry.find_request(request_id)
            if existing is not None:
                if (existing.get("recovery") != recovery or existing.get("mode") != mode
                    or existing.get("run_id") != job.id
                    or existing.get("expected_execution_id") != expected_execution_id):
                    raise RegistryError("request_identity_conflict")
                return self._accepted_execution(job.id, existing["execution_id"])
            current = self.get(job.id)
            if current is None:
                raise KeyError(job.id)
            current_id = current.run_config.get("runner_execution_id")
            active = registry.active()
            healthy = [value for value in active if value["state"] in {"launching", "running", "unresponsive"}]
            # A queued request in this host is also an active admission, even
            # before a worker identity can exist. Unknown reconstructed workers
            # remain blocked rather than being assumed queued.
            queued = current.status in {"queued", "finalizing"} and current.run_config.get("runner_worker_state") == "launch_requested"
            with self._lock:
                preparing = ((job.id, current_id) in self._preparing_executions
                             and current.run_config.get("runner_worker_state") == "preparing"
                             and current.status in {"running", "finalizing"})
            if healthy or queued or preparing:
                if current_id is None or (healthy and any(value["execution_id"] != current_id for value in healthy)):
                    raise RegistryError("worker_identity_unknown")
                target = registry.request(current_id)
                payload = {"run_id": job.id, "mode": mode, "bindings": target["bindings"], "recovery": recovery}
                if expected_execution_id is not None:
                    payload["expected_execution_id"] = expected_execution_id
                registry.join_request(current_id, request_id=request_id, payload=payload)
                return self._accepted_execution(job.id, current_id)
            if current_id != job.run_config.get("runner_execution_id") or (mode == "finalize" and current_id != expected_execution_id):
                raise RegistryError("execution_changed")
            if active:
                raise RegistryError("worker_identity_unknown")
            job = current
            legacy_clearance = None
            if not registry.requests():
                legacy_clearance = discover_legacy_worker(job.command)
                if legacy_clearance["state"] != "absent":
                    raise RegistryError("legacy_worker_identity_unknown")
            # Read exact frozen bytes and verify all bindings before component
            # construction. No worker/request record exists on these refusals.
            measurement, runtime = self._verified_measurement_inputs(job)
            if mode != "finalize":
                require_active_measurement(measurement)
            component = self._component_from_runtime(runtime)
            if mode == "run":
                self._require_uncreated_measurement(job, component)
                ownership = {"state": "available"}
                plan, revision = self._initial_remaining_plan(job, component, measurement=measurement)
                expected = {"kind": "initial_remaining", "request_id": request_id, "run_id": job.id,
                            "plan_sha256": plan.plan_sha256, "revision_digest": revision}
                if recovery != expected:
                    raise RegistryError("initial_preview_stale")
                from visibility_runner.api import RunNotFoundError
                try:
                    component.inspect(job.id)
                except RunNotFoundError:
                    pass
                else:
                    raise RegistryError("initial_run_already_created")
            else:
                ownership = component.ownership(job.id)
                if ownership.get("state") not in {"available", "unowned", "released"}:
                    raise RegistryError("engine_ownership_unavailable")
                inspected = component.inspect(job.id)
                self._validate_measurement_result_binding(job, inspected)
                if mode == "refetch":
                    fresh = self._parsed_recovery(job.id, mode, request_id, component.plan_refetch(job.id, request_id=request_id))
                    if fresh != recovery or not fresh.get("selections"):
                        raise RegistryError("recovery_preview_stale")
                if mode == "finalize":
                    sample = registry.observe(current_id)
                    raw_status = getattr(inspected, "status", "")
                    if (sample["state"] not in {"stopped", "finished"} or sample.get("result_available")
                        or str(getattr(raw_status, "value", raw_status)).casefold() != "succeeded"):
                        raise RegistryError("finalization_unavailable")
            normalized = copy.deepcopy(job.run_config)
            if legacy_clearance is not None:
                normalized["runner_legacy_command"] = list(job.command)
            request_record = self._prepare_execution(normalized, job.id, registry, mode=mode, request_id=request_id,
                                                     recovery=recovery, expected_execution_id=expected_execution_id,
                                                     restart_browser=mode in {"run", "refetch"})
            if legacy_clearance is not None:
                publish_json(registry.path(request_record["execution_id"]) / "legacy-preflight.json", {"execution_id": request_record["execution_id"], "recorded_at": utc_now(), "process_observation": legacy_clearance, "plan_sha256": normalized["runner_plan_sha256"], "engine_ownership": ownership.get("state")})
            workflow = self.workflows_map[job.workflow_id]
            command_prefix = job.command[:job.command.index("--measurement")]
            workflow = replace(workflow, command=command_prefix, runner_owned_snapshot=True)
            current = copy.deepcopy(job)
            current.run_config = normalized
            current.command = build_command(workflow, normalized)
            current.log_file = relative_display(Path(normalized["runner_execution_log"]), self.repo_root)
            current.status, current.returncode, current.pid = ("finalizing" if mode == "finalize" else "queued"), None, None
            current.finished_at, current.stop_requested_at = None, None
            for name in ("runner_cancel_requested", "runner_cancel_execution_id", "runner_cancel_error", "runner_cancel_previous_status", "finalize_available"):
                current.run_config.pop(name, None)
            current.run_config["runner_output_committed"] = False
            current.run_config["runner_worker_state"] = "launch_requested"
            with self._lock:
                self._jobs[job.id] = current
                self._stop_requests.discard(job.id)
                self._runner_cancel_pending = {key for key in self._runner_cancel_pending if key[0] != job.id}
                self._save(current)
            threading.Thread(target=self._run_job, args=(current, workflow), daemon=True).start()
            return self._accepted_execution(job.id, request_record["execution_id"])

    def _prepare_execution(self, config: dict, job_id: str, registry: WorkerRegistry, *, mode: str, request_id: str, recovery: Mapping | None = None, expected_execution_id: str | None = None, restart_browser: bool = False) -> dict:
        inputs = FrozenExecutionInputs.from_job_config(config)
        sign_in = {}
        if mode in {"run", "refetch"} and (self.repo_root / "config/copilot-sign-in.json").exists():
            from clinic_geo.execution.copilot_settings import browser_account_bindings
            from clinic_geo.integrations.visibility_runner import coerce_runtime_config
            frozen_bytes = resolve_repo_relative(config["runner_runtime"], base=self.repo_root).read_bytes()
            if hashlib.sha256(frozen_bytes).hexdigest() != config["runner_runtime_sha256"]:
                raise ValueError("Frozen runtime changed before Copilot sign-in admission")
            frozen_runtime = json.loads(frozen_bytes)
            selected = coerce_runtime_config(frozen_runtime).selected_profiles
            sign_in = browser_account_bindings(self.repo_root,
                [{"id": row.id, "chrome_directory": row.chrome_directory} for row in selected])
        record, _ = registry.create_request(request_id=request_id, run_id=job_id, mode=mode, bindings={
            # Start/Refetch reopen the same selected profiles without old tabs.
            # Normal work stays background; verification has its own opt-in.
            "browser_restart_on_start": restart_browser and mode in {"run", "refetch"},
            **({"copilot_verification_policy": "automatic_hold_v1"} if mode in {"run", "refetch"} else {}),
            **({"copilot_account_emails": sign_in} if sign_in else {}),
            **inputs.request_bindings(),
            "prior_result_reference": config.get("runner_result") if mode != "run" else None,
            "legacy_command": config.get("runner_legacy_command"),
            **({"host_snapshot_policy_version": config["runner_host_snapshot_policy_version"],
                "snapshot_config_sha256": config["runner_snapshot_config_sha256"]}
               if "runner_host_snapshot_policy_version" in config else {}),
            **({"client_research_admission_sha256": config["runner_client_research_admission_sha256"]}
               if "runner_client_research_admission_sha256" in config else {}),
        }, recovery=recovery, expected_execution_id=expected_execution_id, managed_launch=True)
        execution = FrozenExecutionRequest.from_record(record)
        directory = registry.path(execution.execution_id)
        config.update({
            "runner_execution_id": execution.execution_id,
            "runner_execution_mode": execution.mode,
            "runner_execution_request": relative_display(directory / "request.json", self.repo_root),
            "runner_execution_log": str(directory / "stdout.log"),
            "runner_run_preflight": relative_display(directory / "preflight.json", self.repo_root),
            "runner_execution_result": relative_display(directory / "result.json", self.repo_root),
        })
        if mode == "run":
            config["runner_result"] = config["runner_execution_result"]
        return record

    def cancel(self, job_id: str, *, execution_id: str | None = None) -> Job:
        """Cancel only the execution explicitly named by the caller."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            return self.stop(job_id)
        registry = WorkerRegistry(self.run_root / job_id)
        key = (job_id, execution_id)
        with registry.launch_lock():
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    raise KeyError(job_id)
                if not execution_id or current.run_config.get("runner_execution_id") != execution_id:
                    raise RegistryError("execution_changed")
                if current.run_config.get("runner_execution_mode") == "finalize" and current.status in {"queued", "running", "finalizing"}:
                    raise RegistryError("finalizer_cannot_be_stopped")
                if current.status not in {"queued", "running", "stopping", "planned"}:
                    return copy.deepcopy(current)
                current.run_config["runner_cancel_requested"] = True
                current.run_config["runner_cancel_execution_id"] = execution_id
                cancel_marker = registry.path(execution_id) / "cancel-request.json"
                if not cancel_marker.exists():
                    publish_json(cancel_marker, {"execution_id": execution_id,
                        "request_sha256": registry.request(execution_id)["request_sha256"], "recorded_at": utc_now()})
                current.run_config.pop("runner_cancel_error", None)
                current.run_config.setdefault("runner_cancel_previous_status", current.status)
                current.stop_requested_at = current.stop_requested_at or utc_now()
                if current.status == "queued" or current.run_config.get("runner_worker_state") == "preparing":
                    self._stop_requests.add(job_id)
                    marker = registry.path(execution_id) / "launch-failure.json"
                    if not marker.exists():
                        publish_json(marker, {"execution_id": execution_id, "reason": "cancelled_before_spawn", "recorded_at": utc_now()})
                    current.status = "cancelled"
                    current.finished_at = current.finished_at or utc_now()
                    self._save(current)
                    return copy.deepcopy(current)
                if key in self._runner_cancel_pending:
                    return copy.deepcopy(current)
                self._runner_cancel_pending.add(key)
                current.status = "stopping"
                self._save(current)
                job = copy.deepcopy(current)
        # Engine cancellation may wait on storage/transport. Keep the store
        # unlocked, retain the captured target and fence every callback.
        try:
            component = self._measurement_component(job)
            inspected = component.inspect(job.id)
            self._validate_measurement_result_binding(job, inspected)
            result = component.cancel(job.id, execution_id=execution_id)
            applied = self._apply_measurement_result(job.id, result, execution_id=execution_id)
            with self._lock:
                self._runner_cancel_pending.discard(key)
            return applied
        except Exception as exc:
            from visibility_runner.api import RunNotFoundError
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    raise KeyError(job_id) from exc
                if current.run_config.get("runner_execution_id") != execution_id:
                    self._runner_cancel_pending.discard(key)
                    return copy.deepcopy(current)
                if not isinstance(exc, RunNotFoundError):
                    current.run_config["runner_cancel_error"] = f"{type(exc).__name__}: {exc}"
                self._save(current)
            threading.Thread(target=self._cancel_runner_when_created,
                             args=(job_id, execution_id), daemon=True).start()
            return self.get(job_id)

    def inspect(self, job_id: str) -> Job:
        """Refresh a runner job from its component-owned durable state."""

        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            return job
        component = self._measurement_component(job)
        result = component.inspect(job.id)
        return self._apply_measurement_result(job.id, result, execution_id=job.run_config.get("runner_execution_id"))

    def _finalize_measurement_if_ready(self, job: Job, component, result, *, sample=None) -> Job | None:
        """Queue only local output publication after a proven worker exit."""

        self._validate_measurement_result_binding(job, result)
        if job.run_config.get("runner_execution_mode") == "finalize" or not self._execution_is_current(job):
            return None
        execution_id = job.run_config.get("runner_execution_id")
        raw_status = getattr(result, "status", "")
        result_status = str(getattr(raw_status, "value", raw_status)).casefold()
        if execution_id and result_status == "succeeded" and hasattr(component, "ownership"):
            registry = WorkerRegistry(self.run_root / job.id)
            sample = registry.observe(execution_id) if sample is None else sample
            if sample["state"] in {"stopped", "finished"} and not sample.get("result_available"):
                # This authorizes only local output publication. The worker's
                # finalize mode cannot submit a measurement or start recovery.
                # A result without its completion receipt is not committed;
                # a fresh execution preserves that uncommitted file as well.
                return self._launch_existing_measurement(job, mode="finalize", request_id=f"finalize-{execution_id}", expected_execution_id=execution_id)
        return None

    def _cancel_runner_when_created(self, job_id: str, execution_id: str) -> None:
        """Deliver one pending cancel after the guarded worker creates state.

        Runner.create() makes the run visible through public ``inspect`` before
        execution, and Runner.cancel() installs the interprocess admission
        barrier used by all send fences. This watcher never signals the worker
        or reads private runner artifacts; it delegates the explicit request as
        soon as that public run state is available.
        """

        key = (job_id, execution_id)
        while True:
            with self._lock:
                if key not in self._runner_cancel_pending:
                    return
                live = self._jobs.get(job_id)
                if live is None:
                    self._runner_cancel_pending.discard(key)
                    return
                if live.run_config.get("runner_execution_id") != execution_id:
                    self._runner_cancel_pending.discard(key)
                    return
                job = copy.deepcopy(live)
            if job.status in {
                "succeeded",
                "partial",
                "needs_attention",
                "failed",
                "cancelled",
            }:
                with self._lock:
                    self._runner_cancel_pending.discard(key)
                return
            try:
                component = self._measurement_component(job)
                # This is readiness for an already-accepted cancellation, not
                # new-run admission or a uniqueness guard.
                inspected = component.inspect(job.id)
                self._validate_measurement_result_binding(job, inspected)
                result = component.cancel(job.id, execution_id=execution_id)
                self._apply_measurement_result(job.id, result, execution_id=execution_id)
            except Exception as exc:
                from visibility_runner.api import RunNotFoundError

                if not isinstance(exc, RunNotFoundError):
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if current is not None and current.run_config.get("runner_execution_id") == execution_id:
                            current.run_config["runner_cancel_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                            self._save(current)
                time.sleep(0.05)
                continue
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None and current.run_config.get("runner_execution_id") == execution_id:
                    current.run_config.pop("runner_cancel_error", None)
                    self._save(current)
                    self._runner_cancel_pending.discard(key)
            return

    def list(self, limit: int | None = 25, *, scope: str | None = None) -> List[Job]:
        """Read saved jobs newest first; a named history filters before limiting."""
        if scope not in {None, "visibility", "automation"}:
            raise ValueError("Unknown job history scope")
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            if scope is not None:
                visibility = scope == "visibility"
                jobs = [job for job in jobs if _job_is_runner_owned(job) == visibility]
            return copy.deepcopy(jobs[:limit])

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return copy.deepcopy(self._jobs.get(job_id))

    def log_text(self, job_id: str, max_bytes: int = 100_000) -> str:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        path = self._log_path(job)
        if not path.exists():
            return ""
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    def _run_job(self, job: Job, workflow: Workflow):
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.run_config.get("runner_execution_id") != job.run_config.get("runner_execution_id"):
                return
            if job.id in self._stop_requests:
                job.status = (
                    "cancelled" if _job_is_runner_owned(job) else "stopped"
                )
                job.finished_at = job.finished_at or utc_now()
                self._save(job)
                self._stop_requests.discard(job.id)
                return
            job.status = "finalizing" if job.run_config.get("runner_execution_mode") == "finalize" else "running"
            job.started_at = job.started_at or utc_now()
            if _job_is_runner_owned(job):
                job.run_config["runner_worker_state"] = "preparing"
                self._preparing_executions.add((job.id, job.run_config["runner_execution_id"]))
            self._save(job)
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
        job_dir = self.run_root / job.id
        log_path = self._log_path(job)
        spawn_status = "not_started"
        try:
            env.update(prepare_job_environment(workflow, job.run_config, self.repo_root, job_dir))
            with log_path.open("w", encoding="utf-8", newline="\n") as log:
                log.write("$ " + " ".join(job.command) + "\n\n")
                log.write("[run config]\n")
                log.write(json.dumps(job.run_config, indent=2, ensure_ascii=False) + "\n\n")
                log.flush()
                registry = WorkerRegistry(job_dir) if _job_is_runner_owned(job) else None
                # Serialize the last admission check and Popen with Stop and
                # replacement requests. Environment/log preparation above may
                # block, so its earlier state is never permission to spawn.
                with registry.launch_lock() if registry else contextlib.nullcontext():
                    with self._lock:
                        current = self._jobs.get(job.id)
                        execution_id = job.run_config.get("runner_execution_id")
                        if current is None or current.run_config.get("runner_execution_id") != execution_id:
                            return
                        cancelled = job.id in self._stop_requests or (
                            registry is not None and current.run_config.get("runner_cancel_requested") is True
                            and current.run_config.get("runner_cancel_execution_id") == execution_id)
                        if cancelled:
                            if registry is not None:
                                marker = registry.path(execution_id) / "launch-failure.json"
                                if not marker.exists():
                                    publish_json(marker, {"execution_id": execution_id, "reason": "cancelled_before_spawn", "recorded_at": utc_now()})
                            current.status = "cancelled" if registry else "stopped"
                            current.finished_at = current.finished_at or utc_now()
                            self._runner_cancel_pending.discard((job.id, execution_id))
                            self._save(current)
                            return
                    spawn_status = "unknown"
                    if registry is not None and execution_id:
                        registry.begin_spawn(execution_id)
                    try:
                        proc = subprocess.Popen(
                            job.command, cwd=self.repo_root, env=env, stdout=log,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True,
                        )
                    except OSError:
                        spawn_status = "not_started"
                        raise
                    spawn_status = "started"
                    if registry is not None and execution_id:
                        registry.record_launch(execution_id, proc.pid)
                    with self._lock:
                        current = self._jobs.get(job.id)
                        if current is None or current.run_config.get("runner_execution_id") != execution_id:
                            return
                        self._processes[job.id] = proc
                        stop_requested = job.id in self._stop_requests
                        current.pid = proc.pid
                        if registry is not None:
                            current.run_config["runner_worker_state"] = "launching"
                            self._preparing_executions.discard((job.id, execution_id))
                        self._save(current)
                if _job_is_runner_owned(job) and job.run_config.get("runner_execution_id"):
                    threading.Thread(target=self._monitor_execution, args=(job.id, job.run_config["runner_execution_id"]), daemon=True).start()
                if stop_requested and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                returncode = proc.wait()
            if _job_is_runner_owned(job):
                execution_id = job.run_config.get("runner_execution_id")
                if execution_id:
                    WorkerRegistry(job_dir).observe_exit(execution_id, returncode)
                    sample = WorkerRegistry(job_dir).observe(execution_id)
                    if (sample.get("completion") or {}).get("error_code") == "cancelled_before_measurement":
                        self._update_execution(job, status="cancelled", returncode=returncode, finished_at=utc_now(), pid=None)
                        return
                    if sample.get("result_available"):
                        with self._lock:
                            current = self._jobs[job.id]
                            if current.run_config.get("runner_execution_id") == execution_id:
                                current.run_config["runner_result"] = current.run_config["runner_execution_result"]
                                current.run_config["runner_output_committed"] = True
                                self._save(current)
                if not self._execution_is_current(job):
                    return
                # Only the subprocess supplies its exit code. Record that fact
                # before reconciling durable runner state: the runner may have
                # finished before this worker serialized/fsynced its result,
                # or the worker may have died while runner state says running.
                self._update_execution(job, returncode=returncode, pid=None)
                try:
                    component = self._measurement_component(job)
                    result = component.inspect(job.id)
                    settled = self._apply_measurement_result(job.id, result, execution_id=execution_id)
                    self._finalize_measurement_if_ready(settled, component, result)
                except Exception:
                    # A typed non-SUCCEEDED result normally exits 2 and remains
                    # inspectable. If no durable runner result exists at all,
                    # surface the worker failure without attempting recovery.
                    self._update_execution(
                        job,
                        status="failed",
                        returncode=returncode,
                        finished_at=utc_now(),
                        pid=None,
                    )
                return
            with self._lock:
                stopped = job.id in self._stop_requests or returncode < 0
            if stopped or returncode != 0:
                self._update_execution(
                    job,
                    status="stopped" if stopped else "failed",
                    returncode=returncode,
                    finished_at=utc_now(),
                    pid=None,
                )
            elif workflow.id == "paid_audit":
                # The engine run succeeded and stopped at human_review. Record the
                # produced packet/workbench and engine state in the paid-audit index.
                self._update_execution(
                    job,
                    status="finalizing",
                    returncode=0,
                    finished_at=utc_now(),
                    pid=None,
                )
                self._finalize_paid_audit_results(job)
                self._update_execution(job, status="succeeded")
            elif workflow.id in AUDIT_RESULT_SOURCES:
                # Do not expose a successful audit until all required exports have
                # been verified and recorded.
                self._update_execution(
                    job,
                    status="finalizing",
                    returncode=0,
                    finished_at=utc_now(),
                    pid=None,
                )
                self._finalize_audit_results(job, workflow)
                self._update_execution(job, status="succeeded")
            else:
                self._update_execution(
                    job,
                    status="succeeded",
                    returncode=0,
                    finished_at=utc_now(),
                    pid=None,
                )
        except Exception as exc:
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"\n[runner] {exc}\n")
            if _job_is_runner_owned(job):
                if spawn_status == "not_started" and job.run_config.get("runner_execution_id"):
                    registry = WorkerRegistry(job_dir)
                    try:
                        execution_id = job.run_config["runner_execution_id"]
                        request = registry.request(execution_id)
                        publish_json(registry.path(execution_id) / "launch-failure.json", {
                            "execution_id": execution_id,
                            "request_sha256": request["request_sha256"],
                            "reason": "worker_launch_failed",
                            "spawn_status": "not_started",
                            "recorded_at": utc_now(),
                        })
                    except (OSError, RegistryError):
                        pass
                # Startup/wait failures are host failures, not an observed
                # subprocess exit. Preserve its known code and PID (if any).
                self._update_execution(job, status="failed", finished_at=utc_now())
            else:
                self._update_execution(job, status="failed", returncode=1, finished_at=utc_now())
        finally:
            with self._lock:
                self._preparing_executions.discard((job.id, job.run_config.get("runner_execution_id")))
                if self._jobs[job.id].run_config.get("runner_execution_id") == job.run_config.get("runner_execution_id"):
                    self._processes.pop(job.id, None)
                    self._stop_requests.discard(job.id)

    def _execution_is_current(self, job: Job) -> bool:
        with self._lock:
            current = self._jobs.get(job.id)
            return current is not None and current.run_config.get("runner_execution_id") == job.run_config.get("runner_execution_id")

    def _update_execution(self, job: Job, **fields):
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.run_config.get("runner_execution_id") != job.run_config.get("runner_execution_id"):
                return
            for key, value in fields.items():
                setattr(current, key, value)
            self._save(current)

    def _monitor_execution(self, job_id: str, execution_id: str) -> None:
        registry = WorkerRegistry(self.run_root / job_id)

        def request_cancel():
            current = self.get(job_id)
            if current and current.run_config.get("runner_execution_id") == execution_id:
                def cancel_verified_execution():
                    latest = self.get(job_id)
                    if latest and latest.run_config.get("runner_execution_id") == execution_id:
                        component = self._measurement_component(latest)
                        component.cancel(job_id, execution_id=execution_id)
                # Cancellation can itself wait on a transport. The monitor
                # must remain responsive to enforce its bounded shutdown.
                threading.Thread(target=cancel_verified_execution, args=(), daemon=True).start()

        def sample_progress():
            current = self.get(job_id)
            if current is None or current.run_config.get("runner_execution_id") != execution_id:
                return {}
            return self._measurement_component(current).ownership(job_id)

        watchdog = WorkerWatchdog(registry, execution_id, request_cancel=request_cancel, sample_progress=sample_progress)
        unknown_delay = 1.0
        while True:
            current = self.get(job_id)
            if current is None or current.run_config.get("runner_execution_id") != execution_id:
                return
            try:
                sample = registry.observe(execution_id)
                worker_state = sample["state"]
                if worker_state == "unknown":
                    # A restarted server has no Popen handle. Registration or
                    # temporarily unreadable evidence may still become valid;
                    # keep observing without cancellation or signals. Bound
                    # retry frequency for permanently damaged/unknown records.
                    time.sleep(unknown_delay)
                    unknown_delay = min(30.0, unknown_delay * 2)
                    continue
                unknown_delay = 1.0
                if worker_state in {"finished", "stopped"}:
                    watchdog.tick()
                    # An exit observed by a different parent has no fabricated
                    # returncode. Completion is independent durable evidence.
                    completion = sample.get("completion")
                    with self._lock:
                        live = self._jobs[job_id]
                        if live.run_config.get("runner_execution_id") != execution_id:
                            return
                        live.run_config["runner_worker_state"] = worker_state
                        live.pid = None
                        observed_returncode = sample.get("observed_returncode")
                        if observed_returncode is not None:
                            live.returncode = observed_returncode
                        if completion and completion.get("error_code") == "cancelled_before_measurement":
                            live.status = "cancelled"
                            live.finished_at = completion.get("completed_at")
                        elif completion and completion.get("result_reference"):
                            result_path = registry.path(execution_id) / "result.json"
                            raw = result_path.read_bytes()
                            if hashlib.sha256(raw).hexdigest() != completion.get("result_sha256"):
                                raise RegistryError("committed_host_result_mismatch")
                            live.status = completion.get("measurement_status", "needs_attention")
                            if live.status == "succeeded" and live.returncode not in {None, 0}:
                                live.status = "failed"
                            live.finished_at = completion.get("completed_at")
                            live.run_config["runner_output_committed"] = True
                            live.run_config["runner_result"] = relative_display(result_path, self.repo_root)
                        elif live.status in {"queued", "running", "finalizing", "stopping"}:
                            live.status = "failed" if live.returncode not in {None, 0} else "needs_attention"
                        self._save(live)
                        settled = copy.deepcopy(live)
                    if not sample.get("result_available") and settled.run_config.get("runner_execution_mode") != "finalize":
                        try:
                            component = self._measurement_component(settled)
                            result = component.inspect(job_id)
                            self._finalize_measurement_if_ready(settled, component, result, sample=sample)
                        except Exception:
                            # Missing/invalid engine output or unavailable
                            # ownership cannot authorize finalization. Leave
                            # the job available for explicit inspection.
                            pass
                    # A failed finalizer remains visible instead of spawning
                    # an unbounded chain of automatic local repair workers.
                    return
                watchdog.tick()
                with self._lock:
                    live = self._jobs[job_id]
                    if live.run_config.get("runner_execution_id") == execution_id:
                        live.run_config["runner_worker_state"] = worker_state
                        live.pid = sample.get("pid")
                        if live.status != "stopping":
                            live.status = "finalizing" if live.run_config.get("runner_execution_mode") == "finalize" else "running"
                        live.finished_at = None
                        self._save(live)
            except Exception:
                # Failed inspection means blocked monitoring, never dead. The
                # durable engine lock is still the execution admission gate.
                pass
            time.sleep(5)

    def _read_saved_jobs(self):
        pending_runner_cancels: list[tuple[str, str]] = []
        monitor_executions: list[tuple[str, str]] = []
        metadata_paths = set(self.run_root.glob("*/job.json"))
        metadata_paths.update(path.parent / "job.json" for path in self.run_root.glob("*/job-origin.json"))
        for meta in sorted(metadata_paths):
            data = read_json_or_none(meta)
            rebuild_projection = data is None
            if data is None:
                try:
                    data = read_record(meta.parent / "job-origin.json")
                except RegistryError:
                    # Corruption remains visible as blocked work even when no
                    # trustworthy metadata remains to reconstruct its scope.
                    if (meta.parent / "executions").exists():
                        blocked = Job(id=meta.parent.name, workflow_id="free_audit_stage1", label="Snapshot metadata requires repair", command=[], status="needs_attention", returncode=None, created_at="", started_at=None, finished_at=None, log_file=str(meta.parent / "stdout.log"), run_config={"runner_owned_snapshot": True, "runner_worker_state": "unknown", "registry_error": "job_metadata_unreadable"})
                        self._jobs[blocked.id] = blocked
                    continue
            try:
                requests = WorkerRegistry(meta.parent).requests()
                if requests:
                    latest = max(requests, key=lambda item: (item.get("ordinal", 0), item["created_at"]))
                    rebuild_projection = rebuild_projection or data.get("run_config", {}).get("runner_execution_id") != latest["execution_id"]
                    if not rebuild_projection:
                        latest = None
                if requests and latest is not None:
                    directory = WorkerRegistry(meta.parent).path(latest["execution_id"])
                    config = data["run_config"]
                    config.update(runner_execution_id=latest["execution_id"], runner_execution_mode=latest["mode"], runner_execution_request=relative_display(directory / "request.json", self.repo_root), runner_execution_result=relative_display(directory / "result.json", self.repo_root), runner_run_preflight=relative_display(directory / "preflight.json", self.repo_root), runner_execution_log=str(directory / "stdout.log"))
                    data["log_file"] = relative_display(directory / "stdout.log", self.repo_root)
                    data["status"] = "finalizing" if latest["mode"] == "finalize" else "running"
                    # A request may survive a crash before its job projection.
                    # Never attribute the previous worker's exit/output/cancel
                    # evidence to this newly reconstructed execution.
                    data.update(returncode=None, pid=None, finished_at=None, stop_requested_at=None)
                    config["runner_output_committed"] = False
                    config["runner_worker_state"] = "unknown"
                    for name in ("runner_cancel_requested", "runner_cancel_execution_id", "runner_cancel_error", "runner_cancel_previous_status", "finalize_available"):
                        config.pop(name, None)
                    data["command"] = _visibility_measurement_command(WORKFLOWS["free_audit_stage1"], config)
            except (RegistryError, KeyError, ValueError):
                data["status"] = "needs_attention"
                data.setdefault("run_config", {})["registry_error"] = "execution_projection_unavailable"
            try:
                data.setdefault("run_config", {})
                data.setdefault("pid", None)
                data.setdefault("stop_requested_at", None)
                job = Job(**data)
            except Exception:
                continue
            was_active = job.status in {"queued", "running", "finalizing", "stopping"}
            if was_active or (_job_is_runner_owned(job) and job.run_config.get("runner_execution_id")):
                if _job_is_runner_owned(job):
                    execution_id = job.run_config.get("runner_execution_id")
                    if execution_id:
                        sample = WorkerRegistry(self.run_root / job.id).observe(execution_id)
                        job.run_config["runner_worker_state"] = sample["state"]
                        if sample["state"] in {"launching", "running", "unresponsive"}:
                            job.pid = sample.get("pid")
                            if job.run_config.get("runner_execution_mode") == "finalize":
                                job.status = "finalizing"
                            elif not was_active:
                                job.status = "running"
                        elif sample["state"] == "unknown":
                            if was_active:
                                job.status = "needs_attention"
                        else:
                            if was_active:
                                job.status = "needs_attention"
                            job.pid = None
                        monitor_executions.append((job.id, execution_id))
                    else:
                        job.status = "needs_attention"
                        job.run_config["runner_worker_state"] = "unknown"
                    if (job.run_config.get("runner_cancel_requested") is True
                        and execution_id
                        and job.run_config.get("runner_cancel_execution_id") == execution_id
                        and job.run_config.get("runner_execution_mode") != "finalize"
                        and sample["state"] not in {"finished", "stopped"}):
                        job.status = "stopping"
                        job.finished_at = None
                        key = (job.id, execution_id)
                        self._runner_cancel_pending.add(key)
                        pending_runner_cancels.append(key)
                else:
                    job.status = "failed"
                    if not _job_is_runner_owned(job):
                        job.returncode = 1
                    # A restarted host cannot recover a subprocess exit code
                    # it never observed. Preserve known runner-worker codes,
                    # including None, rather than inventing one from status.
                    job.finished_at = job.finished_at or utc_now()
                    job.pid = None
            self._jobs[job.id] = job
        for job_id, execution_id in monitor_executions:
            threading.Thread(target=self._monitor_execution, args=(job_id, execution_id), daemon=True).start()
        for job_id, execution_id in pending_runner_cancels:
            thread = threading.Thread(
                target=self._cancel_runner_when_created,
                args=(job_id, execution_id),
                daemon=True,
            )
            thread.start()

    def _save(self, job: Job):
        write_json(self.run_root / job.id / "job.json", asdict(job))

    def _update(self, job_id: str, **fields):
        with self._lock:
            job = self._jobs[job_id]
            for key, value in fields.items():
                setattr(job, key, value)
            self._save(job)

    def _log_path(self, job: Job) -> Path:
        return resolve_repo_relative(job.log_file, base=self.repo_root)

    def _prepare_client_research_job(self, normalized, job_id, job_dir):
        from clinic_geo.integrations.client_research_selection import measurement_from_client_research
        from clinic_geo.integrations.visibility_runner import load_measurement_plan
        from clinic_geo.execution.measurement_inputs import measurement_send_limit
        child = normalized["client_research"]
        measurement = measurement_from_client_research(child["measurement"])
        runtime = copy.deepcopy(child["runtime"])
        rows = selected_snapshot_profile_rows(normalized["selected_profile_ids"], self.repo_root)
        if rows != runtime["profiles"]["selected_profiles"]:
            raise ValueError("Profile bindings changed; preview client research again")
        manifest_path = job_dir / "runner-profiles.json"
        if Path(runtime["profiles"]["manifest_path"]).resolve() != manifest_path.resolve():
            raise ValueError("Client research profile manifest differs from its reviewed job")
        if canonical_sha256(rows) != runtime["profiles"]["manifest_sha256"]:
            raise ValueError("Client research profile bindings changed")
        plan = load_measurement_plan(child["plan"], measurement=measurement, runtime=runtime, run_id=job_id)
        if plan.plan_sha256 != child["plan_sha256"]:
            raise ValueError("Client research reviewed plan changed")
        from clinic_geo.execution.copilot_settings import browser_account_bindings
        if browser_account_bindings(self.repo_root, rows) != child["account_bindings"]:
            raise ValueError("Copilot account bindings changed; review the execution grant again")
        write_json(manifest_path, rows)
        measurement_path, runtime_path = job_dir / "measurement.json", job_dir / "runtime.json"
        write_json(measurement_path, measurement)
        write_json(runtime_path, runtime)
        admission = {"schema_version": "client_research.execution_admission.v1",
            "mode": "scheduled" if child.get("schedule_revision") else "manual",
            **{k: child[k] for k in ("workspace_id", "round_id", "request_id")},
            "directory_revision": measurement["client_research"]["directory_revision"],
            "config_revision": measurement["client_research"]["config_revision"],
            "runner_run_id": job_id, "plan_sha256": plan.plan_sha256,
            "execution_fingerprint": child["execution_fingerprint"],
            "schedule_revision": child.get("schedule_revision"), "slot": child.get("slot"),
            "grant": child.get("grant")}
        admission_path = job_dir / "client-research-admission.json"
        write_json(admission_path, admission)
        preflight_path = job_dir / "plan-preflight.json"
        ceiling = measurement_send_limit(plan)
        write_json(preflight_path, {"schema_version": "visibility-host-preflight-v1", "mode": "plan", "run_id": job_id,
            "measurement_file_sha256": file_sha256(measurement_path), "runtime_file_sha256": file_sha256(runtime_path),
            "plan_sha256": plan.plan_sha256, "authorized_max_external_submissions": ceiling,
            "plan_serialization_version": "visibility_runner.run_plan.v1", "plan": child["plan"]})
        normalized.update({"runner_owned_snapshot": True, "run_id": job_id,
            "runner_measurement": relative_display(measurement_path, self.repo_root),
            "runner_runtime": relative_display(runtime_path, self.repo_root),
            "runner_plan_preflight": relative_display(preflight_path, self.repo_root),
            "runner_frozen_plan_input": relative_display(preflight_path, self.repo_root),
            "runner_run_preflight": relative_display(job_dir / "run-preflight.json", self.repo_root),
            "runner_result": relative_display(job_dir / "measurement-result.json", self.repo_root),
            "runner_plan_sha256": plan.plan_sha256,
            "runner_measurement_sha256": file_sha256(measurement_path), "runner_runtime_sha256": file_sha256(runtime_path),
            "runner_authorized_max_external_submissions": ceiling,
            "runner_client_research_admission_sha256": file_sha256(admission_path)})
        registry = WorkerRegistry(job_dir)
        with registry.launch_lock():
            self._prepare_execution(normalized, job_id, registry, mode="run", request_id=child["request_id"], restart_browser=True)

    def _prepare_visibility_measurement_job(
        self,
        normalized: dict,
        job_id: str,
        job_dir: Path,
        workflow: Workflow,
    ) -> None:
        """Freeze inputs and obtain the worker's pure, hash-bound plan."""

        if workflow.id == "client_research":
            self._prepare_client_research_job(normalized, job_id, job_dir)
            return

        snapshot_path = resolve_repo_relative(
            str(normalized.get("snapshot_config") or ""),
            base=self.repo_root,
        )
        frozen_snapshot = read_json_or_none(snapshot_path)
        if not isinstance(frozen_snapshot, dict):
            raise ValueError("Runner-owned Snapshot selection is unavailable")
        measurement = measurement_from_frozen_snapshot(frozen_snapshot)

        profile_ids = normalized["selected_profile_ids"]
        profile_rows = selected_snapshot_profile_rows(profile_ids, self.repo_root)
        if not profile_rows:
            raise ValueError("Runner-owned Snapshot requires at least one browser profile")
        manifest_path = job_dir / "runner-profiles.json"
        write_json(manifest_path, profile_rows)
        # The runner verifies the parsed JSON value, not presentation bytes.
        # Bind the exact canonical payload so harmless indentation/key-order
        # changes cannot make the pinned manifest appear to be tampered with.
        manifest_sha256 = canonical_sha256(profile_rows)
        tabs_per_provider = max(
            1,
            int(
                normalized.get("tabs_per_profile")
                or DEFAULT_RUN_CONFIG["tabs_per_profile"]
            ),
        )
        # SDK sources have their own account queue. Copilot parsing uses the
        # same Gemini profile/tab capacity as ordinary Gemini observations.
        browser_platforms = set(measurement.get("platforms") or ()) - {"github_copilot"}
        if "github_copilot" in measurement.get("platforms", ()):
            browser_platforms.add("gemini")
        platform_count = max(1, len(browser_platforms))

        runtime = {
            "artifact_root": str(
                self.repo_root
                / "data"
                / "free-audit"
                / "visibility-runner-executions"
            ),
            "profiles": {
                "selected_profile_ids": [row["id"] for row in profile_rows],
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha256,
                "selected_profiles": profile_rows,
                "lease_timeout_seconds": 300,
            },
            "browser": {
                "cdp_url": str(
                    os.environ.get("CDP_URL") or "http://127.0.0.1:9222"
                ).strip(),
                "launch_policy": "attach_only",
                "background_only": True,
                "copilot_verification_policy": "automatic_hold_v1",
            },
            # The runner freezes this exact policy into the hash-bound plan.
            "retry_policy": copy.deepcopy(RUNNER_SNAPSHOT_RETRY_POLICY),
            # Founder decision 2026-09-09: every selected profile runs
            # `tabs_per_provider` owned tabs per platform at the same time,
            # each with its own chat. The runner plans one packet per tab, so
            # max_concurrency admits every packet at once; "Parallel tabs" no
            # longer caps this workflow.
            "max_concurrency": len(profile_rows) * platform_count * tabs_per_provider,
            "tabs_per_provider": tabs_per_provider,
            # Founder decision 2026-09-09, after 63 tabs opening together
            # produced 23 navigation timeouts: a pair's tabs start in waves,
            # one slot per wave, this many seconds apart.
            "packet_start_stagger_seconds": PACKET_START_STAGGER_SECONDS,
            "worker_timeout_seconds": 900,
        }
        if "github_copilot" in measurement.get("platforms", ()):
            from clinic_geo.execution.copilot_settings import runtime_settings
            runtime["github_copilot"] = runtime_settings(
                normalized.get("github_copilot_runtime"), measurement["github_copilot"],
            )
        measurement_path = job_dir / "measurement.json"
        runtime_path = job_dir / "runtime.json"
        write_json(measurement_path, measurement)
        write_json(runtime_path, runtime)

        plan_preflight_path = job_dir / "plan-preflight.json"
        run_preflight_path = job_dir / "run-preflight.json"
        result_path = job_dir / "measurement-result.json"
        plan_command = visibility_measurement_plan_command(
            workflow,
            measurement_path,
            runtime_path,
            job_id,
            plan_preflight_path,
        )
        environment = dict(os.environ)
        existing_pythonpath = environment.get("PYTHONPATH", "")
        local_src = str(self.repo_root / "src")
        environment["PYTHONPATH"] = local_src + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        subprocess.run(
            plan_command,
            cwd=self.repo_root,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        preflight = read_json_or_none(plan_preflight_path)
        if not isinstance(preflight, dict):
            raise ValueError("Visibility measurement planning produced no preflight")
        if preflight.get("mode") != "plan" or preflight.get("run_id") != job_id:
            raise ValueError("Visibility measurement preflight identity is invalid")

        measurement_sha256 = file_sha256(measurement_path)
        runtime_sha256 = file_sha256(runtime_path)
        if preflight.get("measurement_file_sha256") != measurement_sha256:
            raise ValueError("Visibility measurement preflight changed its measurement input")
        if preflight.get("runtime_file_sha256") != runtime_sha256:
            raise ValueError("Visibility measurement preflight changed its runtime input")
        plan_sha256 = str(preflight.get("plan_sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
            raise ValueError("Visibility measurement preflight has no valid plan hash")
        authorized_max = preflight.get("authorized_max_external_submissions")
        if (
            isinstance(authorized_max, bool)
            or not isinstance(authorized_max, int)
            or authorized_max < 0
        ):
            raise ValueError("Visibility measurement preflight has no valid send ceiling")

        normalized.update(
            {
                "runner_owned_snapshot": True,
                "run_id": job_id,
                "runner_measurement": relative_display(
                    measurement_path, self.repo_root
                ),
                "runner_runtime": relative_display(runtime_path, self.repo_root),
                "runner_plan_preflight": relative_display(
                    plan_preflight_path, self.repo_root
                ),
                "runner_run_preflight": relative_display(
                    run_preflight_path, self.repo_root
                ),
                "runner_result": relative_display(result_path, self.repo_root),
                "runner_plan_sha256": plan_sha256,
                "runner_measurement_sha256": measurement_sha256,
                "runner_runtime_sha256": runtime_sha256,
                "runner_authorized_max_external_submissions": authorized_max,
            }
        )
        if preflight.get("plan_serialization_version") == "visibility_runner.run_plan.v1":
            normalized["runner_frozen_plan_input"] = relative_display(
                plan_preflight_path, self.repo_root,
            )
        registry = WorkerRegistry(job_dir)
        with registry.launch_lock():
            self._prepare_execution(normalized, job_id, registry, mode="run", request_id=str(uuid.uuid4()), restart_browser=True)

    def _verified_measurement_inputs(self, job: Job):
        from clinic_geo.visibility_reports.host_job_artifacts import (
            read_frozen_measurement_inputs, snapshot_policy_records, verify_snapshot_policy,
            SnapshotReportPreparationError,
        )
        job_dir = self.run_root / job.id
        if job_dir.is_symlink():
            raise SnapshotReportPreparationError("Frozen job directory must not be a symbolic link")
        for name, key in (("measurement.json", "runner_measurement"), ("runtime.json", "runner_runtime")):
            value = job.run_config.get(key)
            if not isinstance(value, str) or resolve_repo_relative(value, base=self.repo_root).absolute() != (job_dir / name).absolute():
                raise SnapshotReportPreparationError(f"Frozen {name} path does not match this job")
        measurement, runtime = read_frozen_measurement_inputs(job_dir, job.run_config)
        inputs = FrozenExecutionInputs.from_job_config(job.run_config)
        execution_id = job.run_config.get("runner_execution_id")
        request = WorkerRegistry(job_dir).request(execution_id) if execution_id else None
        policy_records = snapshot_policy_records(job_dir, request)
        for record in policy_records[:2]:
            if record:
                try:
                    inputs.verify_record(record, prefix="runner_", label="job records")
                except ValueError as exc:
                    raise SnapshotReportPreparationError(str(exc)) from exc
        if request is not None:
            if request.get("run_id") != job.id:
                raise RegistryError("request_identity_mismatch")
            try:
                inputs.verify_record(request["bindings"])
            except ValueError as exc:
                raise SnapshotReportPreparationError(str(exc)) from exc
        verify_snapshot_policy(job_dir, job.run_config, *policy_records, measurement=measurement)
        if job.workflow_id == "client_research":
            from clinic_geo.execution.client_research_admission import verify_client_research_admission
            verify_client_research_admission(job_dir, measurement, job.run_config, request["bindings"] if request else None)
        return measurement, runtime

    def _measurement_component(self, job: Job):
        _, runtime = self._verified_measurement_inputs(job)
        return self._component_from_runtime(runtime)

    def _component_from_runtime(self, runtime: Mapping):
        factory = self._measurement_component_factory
        if factory is None:
            from clinic_geo.integrations.visibility_runner import VisibilityMeasurementComponent
            factory = VisibilityMeasurementComponent
        return factory(runtime)

    def _apply_measurement_result(self, job_id: str, result: object, *, execution_id: str | None = None) -> Job:
        raw_status = getattr(result, "status", "")
        status = str(getattr(raw_status, "value", raw_status) or "").casefold()
        allowed = {
            "planned",
            "running",
            "succeeded",
            "partial",
            "needs_attention",
            "failed",
            "cancelled",
        }
        if status not in allowed:
            raise ValueError(f"Unsupported runner status: {status or '(blank)'}")
        terminal_statuses = {
            "succeeded",
            "partial",
            "needs_attention",
            "failed",
            "cancelled",
        }
        # Reconcile under the same lock used to record worker exit. An inspect
        # call can overlap proc.wait(), so a pre-exit copy of the job must not
        # erase a newly observed exit code or resurrect a completed worker.
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                raise KeyError(job_id)
            if execution_id is not None and current.run_config.get("runner_execution_id") != execution_id:
                return copy.deepcopy(current)
            self._validate_measurement_result_binding(current, result)
            if status == "succeeded":
                if not current.run_config.get("runner_output_committed"):
                    if current.returncode is not None:
                        status = "failed" if current.returncode != 0 else "needs_attention"
                    elif current.status in {"failed", "needs_attention", "cancelled"}:
                        status = current.status
                    else:
                        status = "finalizing" if current.run_config.get("runner_execution_mode") == "finalize" else "running"
                elif current.returncode is not None and current.returncode != 0:
                    status = "failed"
            elif status in {"planned", "running"}:
                if current.returncode is not None:
                    # A stale nonterminal inspect must not erase terminal
                    # proof already recorded by the worker's exit handler.
                    status = (
                        current.status
                        if current.status in terminal_statuses
                        else "failed"
                    )
                elif current.status == "failed":
                    # Store reload or startup failure left no observed exit;
                    # nonterminal runner metadata cannot prove a live worker.
                    status = "failed"
            if (current.run_config.get("runner_execution_mode") == "finalize"
                and current.returncode is None and current.status == "finalizing"
                and status in {"planned", "running"}):
                status = "finalizing"
            current_execution = current.run_config.get("runner_execution_id")
            if (current_execution and current.returncode is None
                and current.run_config.get("runner_cancel_requested") is True
                and current.run_config.get("runner_cancel_execution_id") == current_execution
                and WorkerRegistry(self.run_root / job_id).observe(current_execution)["state"] not in {"finished", "stopped"}):
                # Engine cancellation records a send barrier before browser
                # cleanup/worker exit. Neither terminal engine state nor an
                # unreadable host identity is proof that stopping completed.
                status = "stopping"
            terminal = status in terminal_statuses
            current.status = status
            current.finished_at = (
                current.finished_at or utc_now() if terminal else None
            )
            # Neither PID ownership nor returncode comes from Runner.inspect.
            # They are set only by the host subprocess lifecycle.
            self._save(current)
            return copy.deepcopy(current)

    @staticmethod
    def _validate_measurement_result_binding(job: Job, result: object) -> None:
        """Require one public result to match this job's frozen runner plan."""

        result_run_id = str(getattr(result, "run_id", "") or "")
        if result_run_id != job.id:
            raise ValueError(
                "Runner result identity mismatch: "
                f"expected {job.id!r}, received {result_run_id or '(blank)'!r}"
            )
        expected_plan_sha256 = str(
            job.run_config.get("runner_plan_sha256") or ""
        ).casefold()
        plan = getattr(result, "plan", None)
        actual_plan_sha256 = str(
            getattr(plan, "plan_sha256", "") or ""
        ).casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256)
            or actual_plan_sha256 != expected_plan_sha256
        ):
            raise ValueError(
                "Runner result plan mismatch: "
                f"expected {expected_plan_sha256 or '(blank)'!r}, "
                f"received {actual_plan_sha256 or '(blank)'!r}"
            )

    def _prepare_paid_audit_job(self, normalized: dict, job_id: str, job_dir: Path) -> None:
        """Freeze the engine scope for a paid audit launch (or reuse it on resume).

        A first launch builds a scope from the UI selection, freezes it into the job
        directory, assigns ``run_id = job_id``, and registers the run in the paid
        audit index. A resume reuses the already-frozen scope + run id so the engine
        re-enters the same run (``citeup-audit resume --run-id ...``).
        """
        existing_run_id = str(normalized.get("run_id") or "").strip()
        existing_scope = str(normalized.get("scope_path") or "").strip()
        existing_engine_scope = str(normalized.get("engine_scope_path") or "").strip()
        if existing_run_id or existing_scope or existing_engine_scope:
            if not existing_run_id or not existing_scope:
                raise ValueError("A paid-audit resume requires its frozen run id and scope")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", existing_run_id):
                raise ValueError("The paid-audit run id is invalid; refusing resume")

            resume_dir = self.run_root.resolve() / existing_run_id
            expected_scope_path = resume_dir / "scope.json"
            frozen_scope_path = resolve_repo_relative(
                existing_scope, base=self.repo_root
            ).resolve()
            if frozen_scope_path != expected_scope_path:
                raise ValueError("The frozen paid-audit scope is outside its run directory")
            if not frozen_scope_path.is_file():
                raise ValueError("The frozen paid-audit scope is unavailable; refusing a new run")

            expected_engine_scope_path = resume_dir / "engine-scope.json"
            runnable_scope = ""
            if existing_engine_scope:
                engine_scope_path = resolve_repo_relative(
                    existing_engine_scope, base=self.repo_root
                ).resolve()
                if engine_scope_path != expected_engine_scope_path:
                    raise ValueError(
                        "The paid-audit engine scope is outside its run directory"
                    )
                if not engine_scope_path.is_file():
                    frozen_scope = read_json_or_none(frozen_scope_path)
                    if not isinstance(frozen_scope, dict):
                        raise ValueError("The frozen paid-audit scope is invalid; cannot resume")
                    write_json(engine_scope_path, build_engine_scope(frozen_scope))
                runnable_scope = relative_display(engine_scope_path, self.repo_root)
            else:
                frozen_scope = read_json_or_none(frozen_scope_path)
                if not isinstance(frozen_scope, dict):
                    raise ValueError("The frozen paid-audit scope is invalid; cannot resume")
                engine_scope_path = expected_engine_scope_path
                write_json(engine_scope_path, build_engine_scope(frozen_scope))
                runnable_scope = relative_display(engine_scope_path, self.repo_root)

            normalized["paid_audit_subcommand"] = "resume"
            normalized["run_id"] = existing_run_id
            normalized["scope_path"] = relative_display(frozen_scope_path, self.repo_root)
            normalized["engine_scope_path"] = runnable_scope
            # Reuse the registry version pinned at the original launch (Stage 5b) so
            # a resume never re-pins to a newer approved version mid-run.
            existing_pin = PaidAuditStore(self.repo_root).pinned_registry_path(existing_run_id)
            if existing_pin.exists():
                normalized["capability_registry_override"] = str(existing_pin)
            return

        try:
            scope = build_scope(normalized.get("paid_audit_selection") or {})
        except ScopeError as exc:
            raise ValueError(str(exc)) from exc

        scope_path = job_dir / "scope.json"
        engine_scope_path = job_dir / "engine-scope.json"
        write_json(scope_path, scope)
        write_json(engine_scope_path, build_engine_scope(scope))
        normalized["paid_audit_subcommand"] = "run"
        normalized["scope_path"] = relative_display(scope_path, self.repo_root)
        normalized["engine_scope_path"] = relative_display(engine_scope_path, self.repo_root)
        normalized["run_id"] = job_id
        normalized["client_id"] = scope["client_id"]
        normalized["engagement_id"] = scope["engagement_id"]
        normalized["clinic_name"] = scope["clinic"]["name"]
        normalized["base_url"] = scope["base_url"]

        # Pin one approved crawler-registry version for this run (Stage 5b). This
        # is NOT best-effort: a tampered approved version or projection failure
        # aborts the launch rather than silently falling back to the engine seed.
        # No approved version yet -> explicit null reason, engine uses its seed.
        paid_store = PaidAuditStore(self.repo_root)
        pin = paid_store.prepare_pinned_registry(job_id)
        if pin["override_path"]:
            normalized["capability_registry_override"] = pin["override_path"]
        normalized["registry_pinned"] = pin["registry_pinned"]
        normalized["registry_pinned_reason"] = pin["reason"]

        # Register the run in the UI index immediately so it appears in history as
        # queued. The index is best effort and must never block a launch.
        try:
            paid_store.record_run(
                {
                    "client_id": scope["client_id"],
                    "engagement_id": scope["engagement_id"],
                    "run_id": job_id,
                    "clinic_name": scope["clinic"]["name"],
                    "base_url": scope["base_url"],
                    "job_id": job_id,
                    "scope_path": normalized["scope_path"],
                    "engine_scope_path": normalized["engine_scope_path"],
                    "state": "queued",
                    "registry_pinned": pin["registry_pinned"],
                    "registry_pinned_reason": pin["reason"],
                }
            )
        except Exception:
            pass

    def _finalize_paid_audit_results(self, job: Job) -> None:
        """Record engine outputs (state, packet files, validation) in the paid index.

        The engine prints its final summary JSON as the last stdout document. We read
        the tail of the job log, extract that trailing object, and update the index.
        Failures here are logged but never fail the job: the engine run itself already
        succeeded and its evidence is intact.
        """
        run_config = job.run_config or {}
        client_id = str(run_config.get("client_id") or "")
        engagement_id = str(run_config.get("engagement_id") or "")
        run_id = str(run_config.get("run_id") or job.id)
        log_path = self._log_path(job)
        try:
            tail = ""
            if log_path.exists():
                data = log_path.read_bytes()
                tail = data[-200_000:].decode("utf-8", errors="replace")
            summary = extract_trailing_json(tail) or {}
            files = {}
            for key, value in (summary.get("files") or {}).items():
                files[key] = relative_display(Path(value), self.repo_root) if value else value
            record = {
                "client_id": client_id,
                "engagement_id": engagement_id,
                "run_id": run_id,
                "job_id": job.id,
                "clinic_name": run_config.get("clinic_name"),
                "base_url": run_config.get("base_url"),
                "state": summary.get("state") or "human_review",
                "validation_passed": summary.get("validation_passed"),
                "engine_files": files,
                "finished_at": job.finished_at,
            }
            PaidAuditStore(self.repo_root).record_run(record)
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write("\n[runner] paid audit indexed\n")
                log.write(f"state -> {record['state']}\n")
                log.write(f"validation_passed -> {record['validation_passed']}\n")
                for kind, path in files.items():
                    log.write(f"{kind} -> {path}\n")
        except Exception as exc:
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"\n[runner] paid audit indexing failed (run is intact): {exc}\n")

    def _finalize_audit_results(self, job: Job, workflow: Workflow) -> None:
        if workflow.id not in AUDIT_RESULT_SOURCES:
            return
        if job.status not in {"finalizing", "succeeded"}:
            return
        log_path = self._log_path(job)
        try:
            exports = export_audit_results(self.repo_root, workflow, job, log_path)
            if exports:
                with log_path.open("a", encoding="utf-8", newline="\n") as log:
                    log.write("\n[runner] audit-results export\n")
                    log.write(f"info -> {exports['info']}\n")
                    log.write(f"log  -> {exports['log']}\n")
                    if exports.get("source"):
                        log.write(f"source -> {exports['source']}\n")
                    else:
                        log.write("source -> missing expected final JSON; wrote run metadata only\n")
                    for kind, path in exports.get("visibility_snapshot", {}).items():
                        log.write(f"visibility snapshot {kind} -> {path}\n")
                shutil.copy2(log_path, self.repo_root / exports["log"])
        except Exception as exc:
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"\n[runner] audit-results export failed: {exc}\n")
            raise


def relative_display(path: Path, root: Path) -> str:
    path = Path(path)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def extract_trailing_json(text: str, *, wanted_keys: Iterable[str] = ("files", "state", "run")):
    """Return the last JSON object in ``text`` that carries one of ``wanted_keys``.

    The engine streams its final summary as the last stdout document, but the job
    log also contains the command header and run-config JSON. Scanning with the
    JSON decoder (which is string-aware) reliably picks out the trailing summary.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    wanted = tuple(wanted_keys)
    index = 0
    length = len(text)
    best = None
    while index < length:
        char = text[index]
        if char == "{" or char == "[":
            try:
                obj, end = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                index += 1
                continue
            if isinstance(obj, dict) and any(key in obj for key in wanted):
                best = obj
            index = max(end, index + 1)
        else:
            index += 1
    return best


def audit_result_stem(clinic_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "-", str(clinic_name or "").strip()).strip("-").lower()
    return clean or "unknown-clinic"


def free_audit_config_path(repo_root_path: Path) -> Path:
    return Path(repo_root_path) / "data" / "free-audit" / "free_audit_config.json"


def free_audit_clinic_name(repo_root_path: Path) -> str:
    config = read_json_or_none(free_audit_config_path(repo_root_path))
    if config is None:
        return ""
    return str(config.get("clinic_name") or "").strip()


def job_started_timestamp(job: Job) -> Optional[float]:
    if not job.started_at:
        return None
    try:
        return datetime.fromisoformat(job.started_at).timestamp()
    except ValueError:
        return None


def first_existing_audit_source(repo_root_path: Path, workflow_id: str, since: Optional[float] = None) -> Optional[Path]:
    spec = AUDIT_RESULT_SOURCES.get(workflow_id)
    if not spec:
        return None
    for rel_path in spec["sources"]:
        candidate = resolve_repo_relative(rel_path, base=Path(repo_root_path))
        if candidate.exists() and (since is None or candidate.stat().st_mtime >= since):
            return candidate
    return None


def read_audit_result_payload(source: Optional[Path]):
    if source is None:
        return None
    payload = read_json_or_none(source)
    if payload is None:
        return source.read_text(encoding="utf-8", errors="replace")
    return payload


def export_audit_results(repo_root_path: Path, workflow: Workflow, job: Job, log_path: Path) -> Optional[dict]:
    spec = AUDIT_RESULT_SOURCES.get(workflow.id)
    if not spec:
        return None

    # Resolved so relative_display() can still relativize the (resolved) source path.
    repo_root_path = Path(repo_root_path).resolve()
    result_dir = repo_root_path / "audit-results"
    result_dir.mkdir(parents=True, exist_ok=True)

    clinic_name = free_audit_clinic_name(repo_root_path) or "unknown clinic"
    stem = audit_result_stem(clinic_name)
    info_path = result_dir / f"{stem}_info.json"
    log_copy_path = result_dir / f"{stem}_log.txt"
    source = first_existing_audit_source(repo_root_path, workflow.id, since=job_started_timestamp(job))

    payload = {
        "clinic_name": clinic_name,
        "stage": spec["stage"],
        "workflow_id": workflow.id,
        "workflow_label": workflow.label,
        "job": {
            "id": job.id,
            "status": "succeeded" if job.status == "finalizing" else job.status,
            "returncode": job.returncode,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        },
        "run_config": job.run_config,
        "result_source": relative_display(source, repo_root_path) if source else None,
        "result": read_audit_result_payload(source),
        "visibility_snapshot": {},
    }
    write_json(info_path, payload)

    return {
        "info": relative_display(info_path, repo_root_path),
        "log": relative_display(log_copy_path, repo_root_path),
        "source": relative_display(source, repo_root_path) if source else "",
        "visibility_snapshot": {},
    }


def jobs_to_dicts(jobs: Iterable[Job]) -> List[dict]:
    return [asdict(job) for job in jobs]


def normalize_run_config(
    run_config: Optional[dict],
    workflow: Workflow,
    repo_root_path: Path | None = None,
) -> dict:
    """Normalize a browser-run config payload while preserving old workflow defaults."""
    provided = run_config if isinstance(run_config, dict) else {}

    if workflow.id == "client_research":
        from clinic_geo.integrations.client_research_selection import measurement_from_client_research
        from clinic_geo.outreach.api import ROUTINE_PROFILE_IDS
        child = copy.deepcopy(provided.get("client_research"))
        if not isinstance(child, dict) or not re.fullmatch(r"cr_[0-9a-f]{32}", str(child.get("runner_run_id", ""))):
            raise ValueError("Client research requires its reviewed frozen child plan")
        measurement = measurement_from_client_research(child.get("measurement"))
        profiles = child["runtime"]["profiles"]["selected_profile_ids"]
        if profiles != list(ROUTINE_PROFILE_IDS) or provided.get("use_all_configured_profiles") or provided.get("retry_missing"):
            raise ValueError("Client research requires the seven explicit routine profiles")
        return {**copy.deepcopy(DEFAULT_RUN_CONFIG), "client_research": child,
            "selected_profile_ids": profiles, "selected_profiles": profiles,
            "profile_count": len(profiles), "use_all_configured_profiles": False,
            "regime": "logged_in", "runs": "single", "project": False,
            "audit_platforms": measurement["platforms"], "tabs_per_profile": 2,
            "snapshot_config": "", "snapshot_selection": {}, "snapshot_id": "",
            "vpn_enabled": False, "retry_missing": False}

    if _is_snapshot_workflow(workflow):
        _reject_detached_classification_options(provided)
    diagnostic = None
    if "visibility_test" in provided:
        if not workflow.runner_owned_snapshot:
            raise ValueError("Visibility tests require the runner-owned workflow")
        raw_test = provided["visibility_test"]
        if not isinstance(raw_test, Mapping) or raw_test.get("scope_kind") != "diagnostic":
            raise ValueError("Visibility tests require a diagnostic prompt scope")
        if provided.get("snapshot_selection") or provided.get("snapshot_config"):
            raise ValueError("Visibility tests cannot also select a Snapshot scope")
        if provided.get("retry_missing"):
            raise ValueError("Visibility test recovery must use the existing job actions")
        diagnostic = measurement_from_frozen_snapshot(raw_test)

    if workflow.id == "paid_audit":
        # The paid audit is driven by an engine scope, not the browser knobs. Carry
        # the launch-form selection plus any already-frozen run identity (the latter
        # is what makes a resume re-enter the same engine run).
        selection = provided.get("paid_audit_selection")
        if not isinstance(selection, dict):
            selection = provided.get("selection") if isinstance(provided.get("selection"), dict) else {}
        return {
            "paid_audit_selection": selection,
            "scope_path": str(provided.get("scope_path") or "").strip(),
            "engine_scope_path": str(provided.get("engine_scope_path") or "").strip(),
            "run_id": str(provided.get("run_id") or "").strip(),
            "client_id": str(provided.get("client_id") or "").strip(),
            "engagement_id": str(provided.get("engagement_id") or "").strip(),
            "clinic_name": str(provided.get("clinic_name") or "").strip(),
            "base_url": str(provided.get("base_url") or "").strip(),
        }

    source = dict(DEFAULT_RUN_CONFIG)
    source.update(provided)

    def as_bool(key: str) -> bool:
        value = source.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def as_int(key: str, default: int, minimum: int = 1, maximum: int = 100) -> int:
        try:
            value = int(source.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    tabs_per_profile = DEFAULT_RUN_CONFIG["tabs_per_profile"]
    if workflow.opens_browser and "tabs_per_profile" in provided:
        raw_tabs = provided["tabs_per_profile"]
        # Do not truncate fractions, coerce booleans or default explicit invalid
        # input: the capacity shown by the UI must be the capacity frozen here.
        if isinstance(raw_tabs, str) and raw_tabs.strip().isascii() and raw_tabs.strip().isdigit():
            raw_tabs = int(raw_tabs.strip())
        if type(raw_tabs) is not int or not 1 <= raw_tabs <= 20:
            raise ValueError("Tabs/profile/platform must be a whole number from 1 to 20.")
        tabs_per_profile = raw_tabs

    regime_source = provided.get("regime") if "regime" in provided else workflow.default_regime
    regime = str(regime_source or "rotating").strip()
    if regime not in {"rotating", "logged_in"}:
        regime = workflow.default_regime if workflow.default_regime in {"rotating", "logged_in"} else "rotating"
    project_enabled = (
        as_bool("project_enabled")
        if "project_enabled" in provided
        else bool(workflow.default_project and workflow.supports_project)
    )
    project_enabled = project_enabled and workflow.supports_project
    if project_enabled:
        regime = "logged_in"
    if _is_snapshot_workflow(workflow):
        # All runner providers must share the same authenticated Chrome identities.
        # The rotating regime clears/changes ChatGPT profile state and would make
        # its observations incomparable with the other platforms.
        regime = "logged_in"
    project_url = str(source.get("project_url") or "").strip()
    default_runs = "full" if _is_snapshot_workflow(workflow) and diagnostic is None else "single"
    selection_mode = provided.get("snapshot_selection") if isinstance(provided.get("snapshot_selection"), Mapping) else {}
    runs = str(source.get("runs") if "runs" in provided else selection_mode.get("runs", default_runs)).strip()
    if diagnostic is not None and runs != "single":
        raise ValueError("Visibility tests run each requested prompt exactly once")
    if _is_snapshot_workflow(workflow):
        from clinic_geo.web.snapshot_production import snapshot_run_mode
        snapshot_run_mode({**{key: provided[key] for key in ("planned_runs", "repetitions", "test_mode") if key in provided}, "runs": runs})
        if "runs" in selection_mode and selection_mode["runs"] != runs:
            raise ValueError("Snapshot run mode conflicts between request fields")
        snapshot_run_mode({**dict(selection_mode), "runs": runs})
    elif runs not in {"single", "full"}:
        runs = "single"
    default_platforms = (
        list(RUNNER_SNAPSHOT_PLATFORMS)
        if _is_snapshot_workflow(workflow)
        else list(FULL_AUDIT_PLATFORMS)
        if workflow.supports_audit_platforms
        else ["chatgpt"]
    )
    raw_platforms = default_platforms
    explicit_platform_sources: list[tuple[str, object]] = []
    if workflow.supports_audit_platforms and "audit_platforms" in provided:
        explicit_platform_sources.append(
            ("audit_platforms", source.get("audit_platforms"))
        )
    if _is_snapshot_workflow(workflow) and "platforms" in provided:
        explicit_platform_sources.append(("platforms", provided.get("platforms")))
    selection_value = provided.get("snapshot_selection")
    if (
        _is_snapshot_workflow(workflow)
        and isinstance(selection_value, Mapping)
        and "platforms" in selection_value
    ):
        explicit_platform_sources.append(
            ("snapshot_selection.platforms", selection_value.get("platforms"))
        )
    if explicit_platform_sources:
        raw_platforms = explicit_platform_sources[0][1]
    if diagnostic is not None:
        explicit_platform_sources.append(("visibility_test.platforms", diagnostic["platforms"]))
        if len(explicit_platform_sources) == 1:
            raw_platforms = diagnostic["platforms"]

    if _is_snapshot_workflow(workflow):
        audit_platforms = _normalize_new_snapshot_platforms(raw_platforms)
        for label, candidate in explicit_platform_sources[1:]:
            normalized_candidate = _normalize_new_snapshot_platforms(candidate)
            if normalized_candidate != audit_platforms:
                raise ValueError(
                    "New Snapshot platform choices conflict between request fields; "
                    f"{label} does not match"
                )
    else:
        try:
            audit_platforms = parse_platforms(raw_platforms, default=default_platforms)
        except ValueError:
            audit_platforms = default_platforms
    # IDs are the API contract. Raw Chrome directories from older clients are
    # still accepted and resolved through the configured registry.
    raw_selection = (
        provided.get("selected_profile_ids")
        if "selected_profile_ids" in provided
        else provided.get("selected_profiles")
        if "selected_profiles" in provided
        else []
    )
    if _is_snapshot_workflow(workflow):
        if provided.get("use_all_configured_profiles"):
            raise ValueError("Snapshot requires explicit routine profiles; all-configured mode is disabled")
        selected_rows = selected_snapshot_profile_rows(
            raw_selection if isinstance(raw_selection, (list, tuple)) else [],
            repo_root_path or REPO_ROOT,
        )
    else:
        if isinstance(raw_selection, str):
            raw_selection = [raw_selection]
        selected_rows = selected_profile_rows(
            raw_selection if isinstance(raw_selection, (list, tuple, set)) else [],
            repo_root_path or REPO_ROOT,
        )
    selected = [row["id"] for row in selected_rows]
    selected_directories = [row["chrome_directory"] for row in selected_rows]

    profile_count = as_int("profile_count", DEFAULT_RUN_CONFIG["profile_count"], 1, 50)
    if _is_snapshot_workflow(workflow):
        profile_count = len(selected)
    elif selected and "profile_count" not in provided:
        profile_count = min(50, len(selected))
    # A normal Free Snapshot should use the complete logged-in account pool. Keep
    # an explicit mode bit instead of inventing a numeric sentinel so resumes
    # preserve the same intent while prepare_job_environment can resolve the
    # current job-local profile snapshot from the configured rows.
    if "use_all_configured_profiles" in provided:
        use_all_configured_profiles = bool(provided.get("use_all_configured_profiles"))
    else:
        use_all_configured_profiles = (
            _is_snapshot_workflow(workflow)
            and not selected
            and "profile_count" not in provided
        )
    if selected or _is_snapshot_workflow(workflow):
        use_all_configured_profiles = False

    vpn_enabled = False if _is_snapshot_workflow(workflow) else as_bool("vpn_enabled")

    snapshot_id = str(source.get("snapshot_id") or "").strip()
    snapshot_config = str(source.get("snapshot_config") or "").strip()
    snapshot_selection = (
        dict(source.get("snapshot_selection"))
        if isinstance(source.get("snapshot_selection"), dict)
        else {}
    )

    copilot_runtime = None
    if _is_snapshot_workflow(workflow) and "github_copilot" in audit_platforms:
        from clinic_geo.execution.copilot_settings import runtime_settings, source_settings
        supplied_source = (
            diagnostic.get("github_copilot") if diagnostic is not None
            else snapshot_selection.get("github_copilot", provided.get("github_copilot"))
        )
        frozen_source = source_settings(supplied_source)
        if diagnostic is not None and "github_copilot" in provided and source_settings(provided["github_copilot"]) != frozen_source:
            raise ValueError("Visibility test Copilot settings conflict with the requested source")
        if snapshot_selection:
            snapshot_selection["github_copilot"] = frozen_source
        copilot_runtime = runtime_settings(provided.get("github_copilot_runtime"), frozen_source)

    return {
        "vpn_enabled": vpn_enabled,
        "relaunch": as_bool("relaunch"),
        "regime": regime,
        "project_enabled": project_enabled,
        "project_url": project_url if project_enabled else "",
        "intelligence_level": str(source.get("intelligence_level") or "").strip(),
        "profile_count": profile_count,
        "selected_profile_ids": selected,
        # Kept for older API consumers; values retain the former raw-directory shape.
        "selected_profiles": selected_directories,
        "use_all_configured_profiles": use_all_configured_profiles,
        "parallel_tabs": as_int("parallel_tabs", DEFAULT_RUN_CONFIG["parallel_tabs"], 1, 50),
        "tabs_per_profile": tabs_per_profile,
        "max_prompts_per_profile": as_int(
            "max_prompts_per_profile",
            DEFAULT_RUN_CONFIG["max_prompts_per_profile"],
            1,
            500,
        ),
        "runs": runs if workflow.supports_runs else DEFAULT_RUN_CONFIG["runs"],
        "audit_platforms": audit_platforms if workflow.supports_audit_platforms else ["chatgpt"],
        "snapshot_id": snapshot_id if _is_snapshot_workflow(workflow) else "",
        "snapshot_config": snapshot_config if _is_snapshot_workflow(workflow) else "",
        "snapshot_selection": snapshot_selection if _is_snapshot_workflow(workflow) else {},
        **({"visibility_test": diagnostic} if diagnostic is not None else {}),
        **({"github_copilot_runtime": copilot_runtime} if copilot_runtime is not None else {}),
        "retry_missing": as_bool("retry_missing") if _is_snapshot_workflow(workflow) else False,
    }


def _paid_audit_command(run_config: dict, repo_root_path: Path = REPO_ROOT) -> List[str]:
    """Build the engine CLI command for a paid audit run or resume.

    ``run``/``resume`` both take the frozen scope, the shared paid-audit data dir,
    and a stable ``--run-id``. The Node binary is resolved at launch time (it is not
    known when the WORKFLOWS registry is built at import).
    """
    node = discover_node_bin() or "node"
    cli = paid_audit_cli_path(repo_root_path)
    data_dir = paid_audit_data_dir(repo_root_path)
    subcommand = str(run_config.get("paid_audit_subcommand") or "run")
    if subcommand not in {"run", "resume"}:
        subcommand = "run"
    scope_rel = str(
        run_config.get("engine_scope_path") or run_config.get("scope_path") or ""
    ).strip()
    scope_path = resolve_repo_relative(scope_rel, base=repo_root_path) if scope_rel else None
    run_id = str(run_config.get("run_id") or "").strip()
    command = [node, str(cli), subcommand]
    if scope_path is not None:
        command += ["--scope", str(scope_path)]
    command += ["--data-dir", str(data_dir)]
    if run_id:
        command += ["--run-id", run_id]
    # Stage 5b: pin the approved crawler registry projected at launch, if any.
    override = str(run_config.get("capability_registry_override") or "").strip()
    if override:
        command += ["--capability-registry", override]
    return command


def _visibility_measurement_command(
    workflow: Workflow,
    run_config: Mapping[str, object],
) -> List[str]:
    """Build the worker's irreversible, hash-bound run invocation."""

    required = {
        "runner_measurement": "measurement input",
        "runner_runtime": "runtime input",
        "run_id": "run ID",
        "runner_run_preflight": "run preflight output",
        "runner_result": "result output",
        "runner_plan_sha256": "plan hash",
        "runner_measurement_sha256": "measurement hash",
        "runner_runtime_sha256": "runtime hash",
    }
    missing = [label for key, label in required.items() if not run_config.get(key)]
    ceiling = run_config.get("runner_authorized_max_external_submissions")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
        missing.append("authorized send ceiling")
    if missing:
        raise ValueError(
            "Runner-owned Snapshot command is not frozen: " + ", ".join(missing)
        )
    command = [
        *workflow.command,
        "--measurement",
        str(run_config["runner_measurement"]),
        "--runtime",
        str(run_config["runner_runtime"]),
        "--run-id",
        str(run_config["run_id"]),
        "--mode",
        str(run_config.get("runner_execution_mode") or "run"),
        "--preflight-output",
        str(run_config["runner_run_preflight"]),
        "--result-output",
        str(run_config.get("runner_execution_result") or run_config["runner_result"]),
        *FrozenExecutionInputs.from_job_config(run_config).command_arguments(),
    ]
    if run_config.get("runner_execution_request"):
        command.extend(["--execution-request", str(run_config["runner_execution_request"])])
    if (run_config.get("runner_execution_mode") or "run") == "run" and run_config.get("runner_frozen_plan_input"):
        command.extend(["--plan-input", str(run_config["runner_frozen_plan_input"])])
    return command


def build_command(workflow: Workflow, run_config: dict) -> List[str]:
    """Build the command line for a workflow from normalized run config."""
    if workflow.id == "paid_audit":
        return _paid_audit_command(run_config)
    if workflow.runner_owned_snapshot:
        return _visibility_measurement_command(workflow, run_config)
    command = list(workflow.command)
    if not workflow.opens_browser:
        return command
    if not workflow.accepts_cli_flags:
        return command
    if not run_config.get("vpn_enabled", True):
        command.append("--no-vpn")
    if run_config.get("relaunch"):
        command.append("--relaunch")
    regime = run_config.get("regime")
    if regime:
        command.extend(["--regime", str(regime)])
    if workflow.supports_project and run_config.get("project_enabled"):
        command.append("--project")
    elif workflow.supports_project and workflow.default_project:
        command.append("--no-project")
    if workflow.supports_runs and run_config.get("runs"):
        command.extend(["--runs", str(run_config["runs"])])
    if workflow.supports_audit_platforms and run_config.get("audit_platforms"):
        command.extend(["--platforms", ",".join(run_config["audit_platforms"])])
    if _is_snapshot_workflow(workflow):
        if run_config.get("snapshot_config"):
            command.extend(["--config", str(run_config["snapshot_config"])])
        if run_config.get("snapshot_id"):
            command.extend(["--snapshot-id", str(run_config["snapshot_id"])])
        if run_config.get("retry_missing"):
            command.append("--retry-missing")
    return command


def effective_profile_ids(run_config: dict, repo_root_path: Path) -> List[str]:
    """Return exact stable IDs, or defaults from the requested profile regime."""
    raw_selected = (
        run_config.get("selected_profile_ids")
        if "selected_profile_ids" in run_config
        else run_config.get("selected_profiles")
    ) or []
    selected = [row["id"] for row in selected_profile_rows(raw_selected, repo_root_path)]
    count = int(run_config.get("profile_count") or len(selected) or 1)
    if selected:
        return selected[:count]
    regime = str(run_config.get("regime") or "rotating")
    if run_config.get("use_all_configured_profiles"):
        # default_profile_ids slices its source list, so a deliberately large
        # bound means "all configured" without coupling this layer to a fixed
        # number of founder accounts.
        return default_profile_ids(10_000, repo_root_path, regime=regime)
    return default_profile_ids(count, repo_root_path, regime=regime)


def effective_profile_names(run_config: dict, repo_root_path: Path) -> List[str]:
    """Compatibility alias returning stable IDs instead of ambiguous names."""
    return effective_profile_ids(run_config, repo_root_path)


def prepare_job_environment(workflow: Workflow, run_config: dict, repo_root_path: Path, job_dir: Path) -> Dict[str, str]:
    """Prepare environment overrides and job-local profile snapshots for a workflow run."""
    if workflow.id == "paid_audit":
        # The engine renders packets with its own disposable Chromium — kept fully
        # separate from the authenticated ChatGPT profiles the free audit uses.
        return dict(paid_audit_engine_env(repo_root_path))
    if workflow.runner_owned_snapshot:
        # Runtime and the canonical profile-manifest payload were frozen before
        # planning.
        # The guarded worker consumes those files directly; legacy browser
        # environment aliases must not override them.
        return {}
    if not workflow.opens_browser:
        return {}
    env: Dict[str, str] = {}
    regime = str(run_config.get("regime") or workflow.default_regime or "rotating")
    profile_config = dict(run_config)
    profile_config["regime"] = regime
    profile_ids = effective_profile_ids(profile_config, repo_root_path)
    profile_count = max(1, len(profile_ids) or int(run_config.get("profile_count") or 1))

    requested_parallel_tabs = max(1, int(run_config.get("parallel_tabs") or 3))
    effective_parallel_tabs = (
        max(requested_parallel_tabs, profile_count)
        if _is_snapshot_workflow(workflow) and regime == "logged_in"
        else requested_parallel_tabs
    )

    env["NUM_PROFILES"] = str(profile_count)
    env["REGIME"] = regime
    env["PARALLEL_TABS"] = str(profile_count)
    env["MAX_PROMPTS_PER_PROFILE"] = str(run_config.get("max_prompts_per_profile") or 3)
    env["ANALYSIS_TABS"] = str(effective_parallel_tabs)
    env["TABS_PER_PROFILE"] = str(run_config.get("tabs_per_profile") or 1)
    if workflow.supports_audit_platforms:
        env["AUDIT_PLATFORMS"] = ",".join(run_config.get("audit_platforms") or ["chatgpt"])
        env["AI_PROVIDER_TABS"] = str(effective_parallel_tabs)

    model = str(run_config.get("intelligence_level") or "").strip()
    if model:
        env["NEW_CHAT_MODEL"] = model
        env["ANALYSIS_MODEL"] = model

    project_url = str(run_config.get("project_url") or "").strip()
    if workflow.supports_project and run_config.get("project_enabled") and project_url:
        env["ANALYSIS_PROJECT_URL"] = project_url
        env["CHATGPT_PROJECT_URL"] = project_url

    if profile_ids:
        logged_rows = selected_profile_rows(profile_ids, repo_root_path)
        if project_url and workflow.supports_project and run_config.get("project_enabled"):
            logged_rows = [{**row, "project_url": project_url} for row in logged_rows]
        logged_path = job_dir / "logged-in-profiles.json"
        write_logged_in_profile_snapshot(logged_rows, logged_path)
        env["LOGGEDIN_PROFILES_PATH"] = str(logged_path)

        chrome_dir = job_dir / "chrome-profiles"
        write_chrome_profile_snapshot(profile_ids, chrome_dir, repo_root_path)
        env["CHROME_PROFILES_DIR"] = str(chrome_dir)

    return env
