"""Prepare a completed measurement's immutable report for human review.

This is local report work under the engine's public execution lock. It never
submits measurement work or grants recovery allowance, approves, or publishes.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

from clinic_geo.execution.worker_registry import RegistryError, WorkerRegistry, publish_json
from clinic_geo.integrations.visibility_runner import MeasurementConfig
from clinic_geo.workflows.free_audit.publication import (
    SnapshotConflictError,
    SnapshotPublicationStore,
)

from .api import FreeSnapshotMeasurementAPI
from .calculate_visibility import analyze_snapshot_observations
from .convert_measurement_results import adapt_run_result
from .create_visibility_report import build_candidate_payloads
from .report_target import bind_report_target, make_report_target_binding, normalize_report_target
from .host_job_artifacts import (
    SnapshotReportPreparationError,
    _completed_execution_directory,
    _completed_job_bindings,
    _directory,
    _job_directory,
    _job_file_bytes,
    _json_object,
    _run_root,
    _validated_job_id,
    _verified_runtime,
    read_frozen_measurement_inputs,
    snapshot_policy_records,
    verify_snapshot_policy,
)


def _public_json(value):
    """Mirror the guarded worker's lossless serialization of public types."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        # Old committed host results predate this additive engine field. Keep
        # their byte-comparison projection unchanged without normalizing any
        # other historical dataclass fields through the engine's plan serializer.
        return {field.name: _public_json(getattr(value, field.name)) for field in fields(value)
                if not (isinstance(value, MeasurementConfig)
                        and field.name == "diagnostic_prompts"
                        and value.scope_kind != "diagnostic"
                        and not value.diagnostic_prompts)}
    if isinstance(value, Mapping):
        return {str(key): _public_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_public_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SnapshotReportPreparationError("unsupported public runner result value")


def _immutable_json(path: Path, payload: Mapping) -> None:
    if path.is_symlink():
        raise SnapshotReportPreparationError("report binding must not be a symbolic link")
    try:
        publish_json(path, payload)
    except FileExistsError:
        existing = _json_object(_job_file_bytes(path.parent, path.name), filename=path.name)
        if existing != payload:
            raise SnapshotReportPreparationError("existing report binding differs from the frozen measurement") from None


def _verified_execution_request(job_dir: Path, output_dir: Path, job_id: str,
                                bindings: Mapping, committed: Mapping) -> tuple[dict, str]:
    encoded = _job_file_bytes(output_dir, "request.json")
    request = _json_object(encoded, filename="request.json")
    try:
        request = WorkerRegistry(job_dir).validate_request(bindings["execution_id"], request)
    except (RegistryError, TypeError, ValueError) as exc:
        raise SnapshotReportPreparationError("execution request identity or digest is invalid") from exc
    if (request.get("run_id") != job_id
            or not isinstance(request.get("mode"), str)
            or request.get("mode") not in {"run", "resume", "refetch", "finalize"}
            or request.get("mode") != committed.get("mode")
            or not isinstance(request.get("request_id"), str) or not request["request_id"]
            or request["request_id"] != committed.get("request_id")):
        raise SnapshotReportPreparationError("execution request does not bind the committed host result")
    inputs = request.get("bindings")
    if not isinstance(inputs, Mapping) or any(inputs.get(field) != bindings[key] for field, key in (
        ("measurement_sha256", "runner_measurement_sha256"),
        ("runtime_sha256", "runner_runtime_sha256"),
        ("plan_sha256", "runner_plan_sha256"),
    )):
        raise SnapshotReportPreparationError("execution request input hashes differ from the frozen job")
    return request, hashlib.sha256(encoded).hexdigest()


def prepare_snapshot_report(
    job_id: str,
    *,
    repo_root: str | Path,
    run_root: str | Path | None = None,
    api_factory=None,
    renderer=None,
    store_factory=None,
    report_target: Mapping | None = None,
) -> dict:
    """Render/register one completed host execution, safely reusing delivery.

    The current engine result must exactly match the host's committed output.
    Its frozen scope is copied unchanged into the existing publication store.
    That store alone determines approval eligibility, including any existing
    limitations of the v3 report's publication policy.
    """
    job_id = _validated_job_id(job_id)
    target = normalize_report_target(report_target) if report_target is not None else None
    repository = _directory(repo_root, label="repository root")
    job_dir = _job_directory(_run_root(repository, run_root), job_id)
    bindings = _completed_job_bindings(job_dir, job_id)
    if "execution_id" not in bindings:
        raise SnapshotReportPreparationError("report preparation requires a committed host execution")
    measurement, runtime = read_frozen_measurement_inputs(job_dir, bindings)
    if measurement.get("scope_kind") == "diagnostic":
        raise SnapshotReportPreparationError("Diagnostic runs cannot produce clinic or area reports")
    api = (api_factory or FreeSnapshotMeasurementAPI)(runtime)

    # This ownership context blocks Refetch and another renderer while holding
    # no browser resources and creating no measurement/recovery authorization.
    with api.execution(job_id, execution_id="report-" + uuid4().hex):
        if _completed_job_bindings(job_dir, job_id) != bindings:
            raise SnapshotReportPreparationError("job execution changed during report preparation")
        output_dir = _completed_execution_directory(job_dir, bindings)
        encoded = _job_file_bytes(output_dir, "result.json")
        committed = _json_object(encoded, filename="result.json")
        request, request_file_sha256 = _verified_execution_request(job_dir, output_dir, job_id, bindings, committed)
        policy = verify_snapshot_policy(job_dir, *snapshot_policy_records(job_dir, request), committed,
                                        measurement=measurement, required=True)
        for field, key in (("measurement_file_sha256", "runner_measurement_sha256"),
                           ("runtime_file_sha256", "runner_runtime_sha256")):
            if committed.get(field) != bindings[key]:
                raise SnapshotReportPreparationError("committed result input hashes differ from the frozen job")
        result = api.inspect(job_id)
        raw = result.runner_result
        if result.run_id != job_id or not result.succeeded or raw.plan.plan_sha256 != bindings["runner_plan_sha256"]:
            raise SnapshotReportPreparationError("inspected result does not match the completed host execution")
        if _public_json(raw) != committed.get("result"):
            raise SnapshotReportPreparationError("current runner result differs from committed host output; finalize the current output first")
        if verify_snapshot_policy(job_dir, *snapshot_policy_records(job_dir, request), committed,
                                  measurement=measurement, plan=raw.plan, required=True) != policy:
            raise SnapshotReportPreparationError("Frozen host publication policy changed during report preparation")
        scope = getattr(raw.plan, "scope_manifest", None)
        if not isinstance(scope, Mapping):
            raise SnapshotReportPreparationError("runner result has no frozen report scope")
        scope = _public_json(scope)
        report_id = _validated_job_id(scope.get("snapshot_id"))
        observations = adapt_run_result(result, frozen_scope=scope, read_artifact=api.read_artifact)
        analysis = analyze_snapshot_observations(observations)
        if analysis.get("candidate_eligible") is not True:
            raise SnapshotReportPreparationError("completed measurement is not eligible for report preparation")
        target_binding = None
        if target is not None:
            target_binding = make_report_target_binding(scope, target, measurement_run_id=job_id)
            analysis = bind_report_target(analysis, target_binding)
            report_id = target_binding["report_id"]
        elif scope.get("scope_kind") == "area_treatments" or not scope.get("clinic"):
            raise SnapshotReportPreparationError("choose a clinic from the verified directory to create its report")
        results_hash = analysis["results_hash"]
        store = (store_factory or SnapshotPublicationStore)(repository)
        run_dir = store.run_dir(report_id)
        if run_dir.is_symlink() or not run_dir.is_relative_to(repository):
            raise SnapshotReportPreparationError("report directory escaped the repository")
        manifest = run_dir / "scope_manifest.json"
        target_path = run_dir / "report_target.json"
        if target_path.exists() or target_path.is_symlink():
            saved_target = _json_object(_job_file_bytes(run_dir, target_path.name), filename=target_path.name)
            if saved_target != target_binding:
                raise SnapshotReportPreparationError("existing report target differs from the requested clinic")
        if manifest.exists() or manifest.is_symlink():
            saved_scope = _json_object(_job_file_bytes(run_dir, manifest.name), filename=manifest.name)
            if saved_scope != scope:
                raise SnapshotReportPreparationError("existing report scope differs from the frozen measurement")
        existing = store.get_snapshot(report_id) if (run_dir / "snapshot_state.json").exists() else None
        if existing and existing.get("test_mode") is not policy["test_mode"]:
            raise SnapshotConflictError("Report publication mode differs from the frozen host policy")
        if existing and existing.get("publication_state") == "prospect_ready" and existing.get("report_revision_id") != results_hash:
            raise SnapshotConflictError("A published Snapshot is immutable; create a new Snapshot")

        candidate = build_candidate_payloads(analysis, run_dir=run_dir, renderer=renderer)
        if candidate is None:
            raise SnapshotReportPreparationError("completed measurement did not produce a candidate report")
        if _completed_job_bindings(job_dir, job_id) != bindings:
            raise SnapshotReportPreparationError("job execution changed during report preparation")
        _verified_runtime(job_dir, bindings)
        checked_request, checked_request_sha256 = _verified_execution_request(job_dir, output_dir, job_id, bindings, committed)
        if checked_request != request or checked_request_sha256 != request_file_sha256:
            raise SnapshotReportPreparationError("execution request changed during report preparation")
        if verify_snapshot_policy(job_dir, *snapshot_policy_records(job_dir, request), committed,
                                  measurement=measurement, plan=raw.plan, required=True) != policy:
            raise SnapshotReportPreparationError("Frozen host publication policy changed during report preparation")
        _immutable_json(manifest, scope)
        if target_binding is not None:
            _immutable_json(target_path, target_binding)
        _immutable_json(run_dir / "report-revisions" / results_hash / "sources" / (bindings["execution_id"] + ".json"), {
            "schema_version": "visibility-report-source-v1", "job_id": job_id,
            "report_id": report_id, "results_hash": results_hash, **bindings,
            "host_result_sha256": hashlib.sha256(encoded).hexdigest(),
            "host_request_sha256": request["request_sha256"],
            "host_request_file_sha256": request_file_sha256,
            "host_snapshot_policy_version": policy["host_snapshot_policy_version"],
            "snapshot_config_sha256": policy["snapshot_config_sha256"],
            "host_test_mode": policy["test_mode"],
            **({"report_target_binding": target_binding} if target_binding is not None else {}),
        })
        # Exact re-delivery after approval must preserve that approval too.
        detail = existing if existing and existing.get("publication_state") == "prospect_ready" else store.register_candidate(
            report_id, analysis=candidate.analysis, presentation=candidate.presentation,
            quality=candidate.quality, candidate_artifacts=candidate.candidate_artifacts,
            test_mode=policy["test_mode"], operational_state="complete",
        )
        return {
            "job_id": job_id, "measurement_run_id": job_id, "report_id": report_id,
            "results_hash": results_hash, "publication_state": detail["publication_state"],
            "blockers": detail.get("blockers", []),
            "candidate_artifacts": detail.get("candidate_artifacts", {}),
            "collection_period": analysis.get("collection_period", {}), "snapshot": detail,
        }
