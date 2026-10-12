"""Exercise real pure runner contracts with local persistence and fake jobs."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import base64
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from clinic_geo.outreach.api import ClientResearchService, ConflictError, ROUTINE_PROFILE_IDS
from clinic_geo.web.client_research_execution import ClientResearchCoordinator
from clinic_geo.web.jobs import JobStore, normalize_run_config, WORKFLOWS
from clinic_geo.execution.client_research_admission import verify_client_research_admission
from clinic_geo.integrity import canonical_sha256
from clinic_geo.persistence.json_store import read_json_or_none


class Jobs:
    def __init__(self, root):
        self.run_root = root / "data/ui-runs"
        self.saved = {}
        self.starts = []
        self.raise_after_save = False

    def list(self, limit=None):
        return list(self.saved.values())

    def get(self, id):
        return self.saved.get(id)

    def start(self, workflow, config, request_id):
        child = config["client_research"]
        self.starts.append((workflow, deepcopy(config), request_id))
        job = SimpleNamespace(id=child["runner_run_id"], status="queued", run_config={"runner_owned_snapshot": True})
        self.saved[job.id] = job
        if self.raise_after_save:
            self.raise_after_save = False
            raise RuntimeError("delivery interrupted after commit")
        return job

    def failure_readout(self, id):
        return {"host": {"ownership_state": "available", "worker_state": "finished"}}


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2026, 10, 11, tzinfo=timezone.utc)]
    service = ClientResearchService(tmp_path, clock=lambda: now[0])
    w = service.create_workspace(city="Paris", country_code="FR", country_name="France")
    csv = ('account_id,account_name,branch_id,branch_name,country_code,city,area,address,website,treatment_ids\n'
        'a,Alpha,b,Alpha Central,FR,Paris,Central,1 Rue Test,https://alpha.example,skin\n'
        'a,Alpha,b2,Alpha East,FR,Paris,Central,2 Rue Test,https://alpha.example,skin\n')
    p = service.preview_import(w["id"], file_name="directory.csv", content_base64=base64.b64encode(csv.encode()).decode())
    service.commit_import(w["id"], preview_id=p["preview_id"], expected_workspace_version=w["version"], request_id="import")
    w = service.get_workspace(w["id"])
    service.configure(w["id"], configuration={"treatments": {"skin": "skin treatments"}}, expected_workspace_version=w["version"])
    profiles = [{"id": id, "display_name": id, "chrome_directory": "Profile " + str(i + 1)} for i, id in enumerate(ROUTINE_PROFILE_IDS)]
    jobs = Jobs(tmp_path)
    coordinator = ClientResearchCoordinator(tmp_path, service, jobs,
        profile_loader=lambda ids, root: deepcopy(profiles), account_loader=lambda root, rows: {})
    return SimpleNamespace(service=service, w=service.get_workspace(w["id"]), coordinator=coordinator,
        jobs=jobs, profiles=profiles, now=now, root=tmp_path)


def start_args(preview):
    return {"preview_id": preview["preview_id"], "expected_fingerprint": preview["fingerprint"],
        "expected_workspace_version": preview["workspace_version"], "request_id": "start"}


def test_public_plan_counts_deduped_cells_independent_roles_and_no_reference_leak(setup):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"])
    assert p["branch_count"] == 2
    assert p["cell_count"] == 1
    assert p["initial_external_submissions"] == 4 + 42
    assert p["authorized_max_external_submissions"] == 92
    assert len(p["questions"]) == 2 + 21
    assert s.jobs.starts == []
    direct = [q for q in p["questions"] if q["kind"] == "identification"]
    assert all(q["provider"] == "chatgpt" and q["judge_provider"] == "gemini" for q in direct)
    assert all("https://" not in q["question"] and "Rue Test" not in q["question"] for q in direct)
    assert all("Alpha" not in q["question"] for q in p["questions"] if q["kind"] == "recommendations")


def test_duplicate_start_reattaches_original_child_even_after_lost_delivery(setup):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"])
    s.jobs.raise_after_save = True
    first = s.coordinator.start_checks(s.w["id"], **start_args(p))
    assert first["round"]["state"] == "waiting_attention"
    second = s.coordinator.start_checks(s.w["id"], **start_args(p))
    assert second["round_id"] == first["round_id"]
    assert len(s.jobs.starts) == 1
    assert second["round"]["runs"][0]["job_id"] == s.jobs.starts[0][1]["client_research"]["runner_run_id"]
    assert second["round"]["runs"][1]["cursor"] == "reserved"


def test_stale_preview_rejected_before_dispatch_and_payload_receipt_checked(setup):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"])
    args = start_args(p)
    s.service.configure(s.w["id"], configuration={"recommendation_freshness_hours": 140}, expected_workspace_version=s.w["version"])
    with pytest.raises(ConflictError):
        s.coordinator.start_checks(s.w["id"], **args)
    assert not s.jobs.starts


def test_schedule_grant_delivery_is_idempotent_and_changed_bindings_pause_admission(setup):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="recommendations")
    args = {**start_args(p), "enabled": True, "request_id": "schedule"}
    first = s.coordinator.set_schedule(s.w["id"], **args)
    s.now[0] += timedelta(hours=4)
    assert s.coordinator.set_schedule(s.w["id"], **args) == first
    assert s.jobs.starts == []
    s.profiles[0]["chrome_directory"] = "Profile changed"
    with pytest.raises(ValueError, match="changed"):
        s.coordinator.dispatch_due(s.w["id"], first, 1)
    assert s.jobs.starts == []


@pytest.mark.parametrize("action", ["start", "schedule"])
def test_changed_profile_binding_rejects_preview_before_round_or_grant_admission(setup, action):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="recommendations")
    s.profiles[0]["chrome_directory"] = "Profile changed after preview"
    with pytest.raises(ConflictError, match="bindings changed"):
        if action == "start":
            s.coordinator.start_checks(s.w["id"], **start_args(p))
        else:
            s.coordinator.set_schedule(s.w["id"], enabled=True, **start_args(p))
    assert s.jobs.starts == []
    assert not s.service.active_rounds()
    assert not s.service.enabled_schedules()


def test_latest_schedule_slot_is_only_reserved_once(setup):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="recommendations")
    grant = s.coordinator.set_schedule(s.w["id"], enabled=True, **{**start_args(p), "request_id": "enable"})
    s.now[0] += timedelta(hours=720)
    first = s.coordinator.dispatch_due(s.w["id"], grant, 10)
    assert s.coordinator.dispatch_due(s.w["id"], grant, 10)["id"] == first["id"]
    assert len(s.jobs.starts) == 1
    assert first["slot"] == 10
    assert first["runs"][0]["kind"] == "recommendations"
    assert first["runs"][0]["manifest"]["grant"] == grant["grant"]


def test_guarded_job_preparation_consumes_reviewed_plan_and_binds_admission(setup, monkeypatch):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="identification")
    preview = s.service.get_preview(s.w["id"], p["preview_id"], "checks")
    round_ = s.service.reserve_round(s.w["id"], manifest=preview["payload"], **start_args(p))
    child = round_["runs"][0]["manifest"]
    store = JobStore(s.root)
    normalized = normalize_run_config({"client_research": child}, WORKFLOWS["client_research"], s.root)
    monkeypatch.setattr("clinic_geo.web.jobs.selected_snapshot_profile_rows", lambda ids, root: s.profiles)
    monkeypatch.setattr("clinic_geo.execution.copilot_settings.browser_account_bindings", lambda root, rows: {})
    job_dir = store.run_root / child["runner_run_id"]
    job_dir.mkdir()
    store._prepare_client_research_job(normalized, child["runner_run_id"], job_dir)
    saved = read_json_or_none(job_dir / "plan-preflight.json")
    assert saved["plan"] == child["plan"]
    assert saved["plan_sha256"] == child["plan_sha256"]
    assert normalized["runner_authorized_max_external_submissions"] == 8
    admission = verify_client_research_admission(job_dir, child["measurement"], normalized)
    assert admission["round_id"] == round_["id"]
    assert admission["mode"] == "manual"
    (job_dir / "client-research-admission.json").write_text('{}')
    with pytest.raises(ValueError, match="changed"):
        verify_client_research_admission(job_dir, child["measurement"], normalized)


def test_full_host_start_receipt_and_saved_job_admission_without_worker(setup, monkeypatch):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="identification")
    preview = s.service.get_preview(s.w["id"], p["preview_id"], "checks")
    round_ = s.service.reserve_round(s.w["id"], manifest=preview["payload"], **start_args(p))
    child = round_["runs"][0]["manifest"]
    monkeypatch.setattr("clinic_geo.web.jobs.selected_snapshot_profile_rows", lambda ids, root: s.profiles)
    monkeypatch.setattr("clinic_geo.execution.copilot_settings.browser_account_bindings", lambda root, rows: {})
    monkeypatch.setattr("clinic_geo.web.jobs.threading.Thread.start", lambda self: None)
    store = JobStore(s.root)
    job = store.start("client_research", {"client_research": child}, request_id=child["request_id"])
    assert job.id == child["runner_run_id"]
    measurement, runtime = store._verified_measurement_inputs(job)
    assert measurement == child["measurement"]
    assert runtime == child["runtime"]
    restarted = JobStore(s.root)
    assert restarted.start("client_research", {"client_research": child}, request_id=child["request_id"]).id == job.id
    assert "--plan-input" in job.command
    assert "client_research" not in [w["id"] for w in store.workflows()]


def loaded_worker(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/run_visibility_measurement.py"
    spec = importlib.util.spec_from_file_location("_client_research_worker", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_worker_cli_pure_research_plan_never_reaches_execution(setup, monkeypatch):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="identification")
    child = s.service.get_preview(s.w["id"], p["preview_id"], "checks")["payload"]["children"][0]
    measurement_path, runtime_path = s.root / "measurement.json", s.root / "runtime.json"
    measurement_path.write_text(json.dumps(child["measurement"]))
    runtime_path.write_text(json.dumps(child["runtime"]))
    output = s.root / "preflight.json"
    worker = loaded_worker(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Pure planning reached an execution or browser port")

    monkeypatch.setattr(worker.VisibilityMeasurementComponent, "execution", forbidden)
    monkeypatch.setattr(worker.VisibilityMeasurementComponent, "run", forbidden)
    monkeypatch.setattr(worker, "measurement_browser_session", forbidden)
    monkeypatch.setattr(sys, "argv", [str(worker.__file__), "--measurement", str(measurement_path),
        "--runtime", str(runtime_path), "--run-id", child["runner_run_id"], "--mode", "plan",
        "--preflight-output", str(output)])
    assert worker.main() == 0
    assert read_json_or_none(output)["plan_sha256"] == child["plan_sha256"]
    assert not Path(child["runtime"]["artifact_root"]).exists()


def test_worker_cli_rejects_missing_research_admission_before_component(setup, monkeypatch):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="identification")
    child = s.service.get_preview(s.w["id"], p["preview_id"], "checks")["payload"]["children"][0]
    measurement_path, runtime_path = s.root / "measurement.json", s.root / "runtime.json"
    measurement_path.write_text(json.dumps(child["measurement"]))
    runtime_path.write_text(json.dumps(child["runtime"]))
    worker = loaded_worker(monkeypatch)
    monkeypatch.setattr(worker, "VisibilityMeasurementComponent", lambda *a, **k: pytest.fail("Missing admission reached runner"))
    monkeypatch.setattr(sys, "argv", [str(worker.__file__), "--measurement", str(measurement_path),
        "--runtime", str(runtime_path), "--run-id", child["runner_run_id"], "--mode", "run",
        "--preflight-output", str(s.root / "preflight.json")])
    with pytest.raises(ValueError, match="reviewed host admission"):
        worker.main()


def test_worker_cli_rejects_altered_research_admission_before_component(setup, monkeypatch):
    s = setup
    p = s.coordinator.preview_checks(s.w["id"], mode="identification")
    preview = s.service.get_preview(s.w["id"], p["preview_id"], "checks")
    round_ = s.service.reserve_round(s.w["id"], manifest=preview["payload"], **start_args(p))
    child = round_["runs"][0]["manifest"]
    monkeypatch.setattr("clinic_geo.web.jobs.selected_snapshot_profile_rows", lambda ids, root: s.profiles)
    monkeypatch.setattr("clinic_geo.execution.copilot_settings.browser_account_bindings", lambda root, rows: {})
    monkeypatch.setattr("clinic_geo.web.jobs.threading.Thread.start", lambda self: None)
    job = JobStore(s.root).start("client_research", {"client_research": child}, request_id=child["request_id"])
    (s.jobs.run_root / job.id / "client-research-admission.json").write_text('{}')
    worker = loaded_worker(monkeypatch)
    monkeypatch.setattr(worker, "VisibilityMeasurementComponent", lambda *a, **k: pytest.fail("Altered admission reached runner"))
    monkeypatch.chdir(s.root)
    monkeypatch.setattr(sys, "argv", job.command[1:])
    with pytest.raises(ValueError, match="admission was changed"):
        worker.main()
