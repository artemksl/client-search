"""Reviewed city checks and a durable dispatch outbox for the public runner.

Planning and inspection are local. Only a recorded Start or recurring grant can
reach JobStore.start; the existing guarded worker retains browser ownership,
send fences and operator recovery controls.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import threading
import uuid

from clinic_geo.integrity import canonical_sha256
from clinic_geo.outreach.api import (ConflictError, ROUTINE_PROFILE_IDS,
    build_matching_context, direct_branch_context)
from clinic_geo.integrations.client_research_selection import (
    client_research_runtime, measurement_from_client_research)
from clinic_geo.integrations.visibility_runner import VisibilityMeasurementComponent, load_measurement_plan
from clinic_geo.visibility_reports.api import adapt_client_research_result
from clinic_geo.execution.copilot_settings import browser_account_bindings
from clinic_geo.execution.measurement_inputs import measurement_send_limit
from clinic_geo.web.config import selected_snapshot_profile_rows
from clinic_geo.web.jobs import RUNNER_SNAPSHOT_RETRY_POLICY


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _value(value):
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(k): _value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(v) for v in value]
    return value


class ClientResearchCoordinator:
    def __init__(self, root, service, job_store, *, component_factory=None, profile_loader=None, account_loader=None):
        self.root, self.service, self.jobs = Path(root).resolve(), service, job_store
        self.component_factory = component_factory or VisibilityMeasurementComponent
        self.profile_loader = profile_loader or selected_snapshot_profile_rows
        self.account_loader = account_loader or browser_account_bindings
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(service.db_path), threading.RLock())

    def inspect_availability(self):
        try:
            from visibility_runner import client_research_capabilities
            capabilities = client_research_capabilities()
            required = {"client_research.identification.v1", "client_research.recommendations.v1"}
            if not required.issubset(capabilities):
                raise ValueError("Installed visibility runner does not support both client research checks")
            return {"available": True, "capabilities": list(capabilities), "errors": []}
        except (ImportError, ValueError) as exc:
            return {"available": False, "errors": [str(exc)]}

    def _settings(self, identifier):
        availability = self.inspect_availability()
        if not availability["available"]:
            raise ImportError("; ".join(availability["errors"]))
        workspace = self.service.get_workspace(identifier)
        directory = self.service.directory(identifier)
        config = workspace["configuration"]
        if not workspace["directory_revision"] or not workspace["config_revision"]:
            raise ValueError("Import a directory and save its check configuration first")
        if not config["source_template"] or not config["recommendation_template"]:
            raise ValueError("Review both prompts in the workspace language")
        if not config["cells"] or any(not c["treatment_label"] for c in config["cells"]):
            raise ValueError("Select at least one area/treatment cell and review its treatment label")
        rows = self.profile_loader(list(ROUTINE_PROFILE_IDS), self.root)
        if [r["id"] for r in rows] != list(ROUTINE_PROFILE_IDS):
            raise ValueError("All seven explicit routine profiles must be configured")
        accounts = self.account_loader(self.root, rows)
        execution = {"schema_version": "client_research.execution_grant.v1", "workspace_id": identifier,
            "city": workspace["city"], "country_code": workspace["country_code"], "country_name": workspace["country_name"],
            "language": workspace["language"], "cells": config["cells"],
            "recommendation_template": config["recommendation_template"], "providers": config["providers"],
            "profiles": rows, "account_bindings": accounts, "retry_policy": RUNNER_SNAPSHOT_RETRY_POLICY,
            "browser_policy": {"background_only": True, "copilot_verification_policy": "automatic_hold_v1"},
            "tabs_per_provider": 2, "packet_start_stagger_seconds": 10,
            "cdp_url": str(os.environ.get("CDP_URL") or "http://127.0.0.1:9222").strip(),
            "interval_hours": 72}
        execution["execution_fingerprint"] = canonical_sha256(execution)
        return workspace, directory, config, rows, execution

    def _manifest(self, identifier, mode, branch_ids=None):
        if mode not in {"initial", "identification", "recommendations"}:
            raise ValueError("mode must be initial, identification or recommendations")
        workspace, directory, config, profiles, grant = self._settings(identifier)
        context = build_matching_context(directory)
        available = {b["branch_id"] for b in directory["branches"]}
        if branch_ids is not None and (not isinstance(branch_ids, list) or not branch_ids or any(b not in available for b in branch_ids)):
            raise ValueError("branch_ids must select active supplied branches")
        if mode == "recommendations" and branch_ids:
            raise ValueError("Recommendation checks cover the selected city cells")
        selected = set(branch_ids) if branch_ids else available
        if mode == "initial" and branch_ids is None:
            projected = self.service.list_accounts(identifier, view="all", limit=500)
            # Pagination cannot silently omit branches in large directories.
            page = 0
            fresh = set()
            while True:
                for account in projected["items"]:
                    for branch in account["branches"]:
                        if branch.get("identification", {}).get("state") in {"identified", "could_not_identify"}:
                            fresh.add(branch["branch_id"])
                if len(projected["items"]) < 500:
                    break
                page += 500
                projected = self.service.list_accounts(identifier, view="all", limit=500, offset=page)
            selected -= fresh
        common = {k: workspace[k] for k in ("city", "country_code", "country_name", "language", "directory_revision", "config_revision")}
        common["workspace_id"] = identifier
        children = []
        for kind in (["identification", "recommendations"] if mode == "initial" else [mode]):
            if kind == "identification" and not selected:
                continue
            metadata = deepcopy(common)
            if kind == "identification":
                branches = []
                for branch_id in sorted(selected):
                    branch = direct_branch_context(branch_id, context, {**config, "language": workspace["language"]}, identifier, workspace["city"], workspace["country_name"])
                    fields = {"account_id", "account_name", "branch_id", "branch_name", "area", "address", "website", "identity_fingerprint", "matching_fingerprint", "direct_fingerprint", "ambiguity_context", "account_aliases", "branch_aliases", "source_prompt"}
                    branches.append({k: v for k, v in branch.items() if k in fields})
                metadata.update(branches=branches, source_provider=config["source_provider"], judge_provider=config["judge_provider"], source_template=config["source_template"])
                platforms = [config["source_provider"], config["judge_provider"]]
            else:
                metadata.update(cells=deepcopy(config["cells"]), recommendation_template=config["recommendation_template"])
                platforms = config["providers"]
            measurement = measurement_from_client_research({"scope_kind": "client_research_" + kind,
                "client_research": metadata, "platforms": platforms, "repetitions": 1})
            run_id = "cr_" + uuid.uuid4().hex
            runtime = client_research_runtime(self.root, run_id, profiles, RUNNER_SNAPSHOT_RETRY_POLICY,
                cdp_url=grant["cdp_url"], run_root=self.jobs.run_root)
            from visibility_runner import serialize_run_plan
            plan = self.component_factory(runtime).plan(measurement, run_id=run_id)
            children.append({"kind": kind, "runner_run_id": run_id, "measurement": measurement, "runtime": runtime,
                "plan": serialize_run_plan(plan), "plan_sha256": plan.plan_sha256,
                "initial_external_submissions": plan.submission_budget.total_external_submissions,
                "authorized_max_external_submissions": measurement_send_limit(plan), "observation_count": len(plan.observation_ids),
                "account_bindings": grant["account_bindings"], "execution_fingerprint": grant["execution_fingerprint"]})
        if not children:
            raise ValueError("No branches need identification; select branches to explicitly recheck")
        return {"schema_version": "client_research.round_manifest.v1", "workspace_id": identifier,
            "workspace_version": workspace["version"], "mode": mode, "directory_revision": workspace["directory_revision"],
            "config_revision": workspace["config_revision"], "directory": directory, "configuration": config,
            "execution_fingerprint": grant["execution_fingerprint"], "grant": grant, "children": children}

    def preview_checks(self, identifier, *, mode="initial", branch_ids=None):
        manifest = self._manifest(identifier, mode, branch_ids)
        preview = self.service.save_check_preview(identifier, manifest)
        return {**preview, "mode": mode, "executable": True, "execution_fingerprint": manifest["execution_fingerprint"],
            "branch_count": sum(c["observation_count"] for c in manifest["children"] if c["kind"] == "identification"),
            "cell_count": len(manifest["configuration"]["cells"]) if mode != "identification" else 0,
            "profile_ids": list(ROUTINE_PROFILE_IDS), "providers": manifest["configuration"]["providers"],
            "initial_external_submissions": sum(c["initial_external_submissions"] for c in manifest["children"]),
            "authorized_max_external_submissions": sum(c["authorized_max_external_submissions"] for c in manifest["children"]),
            "children": [{k: c[k] for k in ("kind", "observation_count", "initial_external_submissions", "authorized_max_external_submissions")} for c in manifest["children"]],
            "questions": [{"kind": c["kind"], "provider": o["platform"], "profile_id": o["profile_id"],
                "judge_provider": o.get("judge_provider"), "judge_profile_id": o.get("judge_profile_id"),
                "branch_id": o.get("branch_id"), "cell_id": o.get("cell_id"), "question": o["prompt_text"]}
                for c in manifest["children"] for o in c["plan"]["observation_plan"]["observations"]],
            "startup_notice": "Start closes Chrome and all tabs, then reopens the seven selected routine profiles. Copilot verification may briefly focus Chrome."}

    def start_checks(self, identifier, *, preview_id, expected_fingerprint, expected_workspace_version, request_id):
        preview = self.service.get_preview(identifier, preview_id, "checks", allow_consumed=True)
        with self.lock:
            if not preview["consumed_at"]:
                self._verify_preview_execution(identifier, preview)
            round_ = self.service.reserve_round(identifier, manifest=preview["payload"], request_id=request_id,
                preview_id=preview_id, expected_fingerprint=expected_fingerprint, expected_workspace_version=expected_workspace_version)
            self._reconcile_round(round_)
            return {"round_id": round_["id"], "round": self.service.get_round(round_["id"])}

    def _verify_preview_execution(self, identifier, preview):
        _, _, _, _, grant = self._settings(identifier)
        if grant != preview["payload"]["grant"]:
            raise ConflictError("Execution settings or profile bindings changed; preview checks again")

    def set_schedule(self, identifier, *, enabled, expected_workspace_version, request_id, preview_id=None, expected_fingerprint=None):
        grant = None
        if enabled:
            preview = self.service.get_preview(identifier, preview_id, "checks", allow_consumed=True)
            if preview["payload"]["mode"] != "recommendations":
                raise ValueError("Preview recommendation checks before enabling the schedule")
            if not preview["consumed_at"]:
                self._verify_preview_execution(identifier, preview)
            grant = preview["payload"]["grant"]
        return self.service.set_schedule(identifier, enabled=enabled, grant=grant,
            execution_fingerprint=grant["execution_fingerprint"] if grant else None,
            expected_workspace_version=expected_workspace_version, request_id=request_id,
            preview_id=preview_id, expected_fingerprint=expected_fingerprint)

    def pause_schedule(self, identifier, reason):
        self.service.pause_schedule(identifier, reason)

    def dispatch_due(self, identifier, schedule, slot):
        with self.lock:
            old = self.service.scheduled_round(identifier, schedule["revision"], slot)
            if old:
                return old
            _, _, _, _, current_grant = self._settings(identifier)
            if current_grant != schedule["grant"] or current_grant["execution_fingerprint"] != schedule["execution_fingerprint"]:
                raise ValueError("Schedule execution settings or profile bindings changed; review a new grant")
            for active in self.service.active_rounds():
                if active["workspace_id"] == identifier and active["schedule_revision"] == schedule["revision"] and active["slot"] < slot and all(r["cursor"] == "reserved" for r in active["runs"]):
                    self.service.set_round_state(active["id"], "skipped")
            manifest = self._manifest(identifier, "recommendations")
            for child in manifest["children"]:
                child["grant"] = deepcopy(schedule["grant"])
            round_ = self.service.reserve_round(identifier, manifest=manifest,
                request_id="slot_" + canonical_sha256([identifier, schedule["revision"], slot]), schedule_revision=schedule["revision"], slot=slot)
            self._reconcile_round(round_)
            return self.service.get_round(round_["id"])

    def _busy(self, owned_run_id):
        for job in self.jobs.list(limit=None):
            if job.id == owned_run_id:
                continue
            if job.status in {"queued", "running", "stopping", "finalizing", "needs_attention"}:
                return True
            if job.run_config.get("runner_owned_snapshot"):
                try:
                    host = self.jobs.failure_readout(job.id).get("host", {})
                    if host.get("ownership_state") not in {"available", "released"} or host.get("worker_state") in {"unknown", "running", "unresponsive", "launching"}:
                        return True
                except (ValueError, OSError, KeyError):
                    return True
        return False

    def reconcile(self):
        with self.lock:
            self._sync_explicit_recovery()
            for round_ in self.service.active_rounds():
                self._reconcile_round(round_)

    def _sync_explicit_recovery(self):
        # A terminal parent does not grant another execution. An operator may
        # explicitly Refetch its child in Run history; index only that same
        # job's now-current public result without issuing a runner action.
        for job in self.jobs.list(limit=None):
            if getattr(job, "workflow_id", None) != "client_research":
                continue
            round_ = self.service.round_for_run(job.id)
            if not round_ or round_["state"] in {"queued", "running", "waiting_attention", "skipped"}:
                continue
            link = next(r for r in round_["runs"] if r["runner_run_id"] == job.id)
            execution_id = job.run_config.get("runner_execution_id")
            old = link["result"] or {}
            if not execution_id or old.get("execution_id") == execution_id and old.get("sync_complete"):
                continue
            try:
                component = self.component_factory(link["manifest"]["runtime"])
                result = component.inspect(job.id)
                accepted_ids = self._ingest(round_, link, component, result)
            except Exception as exc:
                self.service.update_run_link(round_["id"], link["kind"], cursor="terminal",
                    result={"status": "needs_attention", "execution_id": execution_id,
                        "accepted_result_ids": [], "sync_complete": False}, error=str(exc))
                raise
            status = _value(result.status)
            complete = (job.status not in {"queued", "running", "finalizing", "stopping", "needs_attention"}
                and status in {"succeeded", "partial", "failed", "cancelled"})
            self.service.update_run_link(round_["id"], link["kind"], cursor="terminal",
                result={"status": status, "execution_id": execution_id,
                    "accepted_result_ids": accepted_ids, "sync_complete": complete},
                error=None if status == "succeeded" else "Check incomplete; review the original run")

    def read_evidence(self, identifier):
        row = self.service.get_evidence(identifier)
        round_ = self.service.get_round(row["round_id"])
        child = next(r for r in round_["runs"] if r["runner_run_id"] == row["runner_run_id"])
        component = self.component_factory(child["manifest"]["runtime"])
        from visibility_runner import ArtifactReference
        for key, field in (("source_artifact", "source_answer"), ("classification_artifact", "classification_answer")):
            reference = ArtifactReference(**row[key])
            text = component.read_artifact(row["runner_run_id"], reference).decode("utf-8")
            if field == "source_answer" and text != row[field]:
                raise ValueError("Indexed answer differs from its hash-verified runner evidence")
            row[field] = text
        return row

    def _reconcile_round(self, round_):
        failures = False
        for link in round_["runs"]:
            if link["cursor"] == "terminal":
                failures |= bool(link["error"])
                continue
            job = self.jobs.get(link["runner_run_id"])
            if job is None:
                if link["manifest"].get("schedule_revision"):
                    current = self.service.get_workspace(round_["workspace_id"])["schedule"]
                    try:
                        _, _, _, _, grant = self._settings(round_["workspace_id"])
                    except (ValueError, ImportError):
                        grant = None
                    if not current or not current["enabled"] or current["revision"] != link["manifest"]["schedule_revision"] or grant != link["manifest"].get("grant"):
                        self.pause_schedule(round_["workspace_id"], "Schedule grant changed before dispatch; review the retained round")
                        if link["cursor"] != "reserved":
                            self.service.set_round_state(round_["id"], "waiting_attention")
                        return
                if self._busy(link["runner_run_id"]):
                    return
                self.service.update_run_link(round_["id"], link["kind"], cursor="dispatch_requested")
                try:
                    job = self.jobs.start("client_research", {"client_research": link["manifest"]}, request_id=link["request_id"])
                except Exception as exc:
                    # Dispatch can have committed before delivery failed. Never
                    # reserve a replacement identity or automatically Refetch.
                    self.service.update_run_link(round_["id"], link["kind"], cursor="dispatch_requested", error=str(exc))
                    self.service.set_round_state(round_["id"], "waiting_attention")
                    return
            self.service.update_run_link(round_["id"], link["kind"], cursor="attached", job_id=job.id)
            if job.id != link["runner_run_id"]:
                raise ConflictError("Worker delivery changed its frozen run identity")
            self.service.set_round_state(round_["id"], "running")
            try:
                component = self.component_factory(link["manifest"]["runtime"])
                result = component.inspect(job.id)
                accepted_ids = self._ingest(round_, link, component, result)
            except Exception as exc:
                if job.status in {"failed", "cancelled", "stopped"}:
                    self.service.update_run_link(round_["id"], link["kind"], cursor="terminal", error=str(exc))
                    failures = True
                    continue
                if job.status not in {"queued", "running"}:
                    self.service.set_round_state(round_["id"], "waiting_attention")
                return
            status = _value(result.status)
            snapshot = {"status": status, "execution_id": job.run_config.get("runner_execution_id"),
                "accepted_result_ids": accepted_ids, "sync_complete": False}
            self.service.update_run_link(round_["id"], link["kind"], cursor="attached", result=snapshot)
            if status in {"needs_attention", "interrupted"} or job.status in {"needs_attention", "stopping"}:
                self.service.set_round_state(round_["id"], "waiting_attention")
                return
            if status in {"planned", "running", "queued"} or job.status in {"queued", "running", "finalizing"}:
                return
            if status not in {"succeeded", "partial", "failed", "cancelled"}:
                self.service.set_round_state(round_["id"], "waiting_attention")
                return
            host = self.jobs.failure_readout(job.id).get("host", {})
            if host.get("ownership_state") not in {"available", "released"} or host.get("worker_state") in {"unknown", "running", "unresponsive", "launching"}:
                self.service.set_round_state(round_["id"], "waiting_attention")
                return
            failures |= status != "succeeded"
            self.service.update_run_link(round_["id"], link["kind"], cursor="terminal",
                result={**snapshot, "sync_complete": True},
                error=None if status == "succeeded" else "Check incomplete; review the original run")
        state = "succeeded" if not failures else "partial" if self.service.round_evidence_count(round_["id"]) else "failed"
        self.service.set_round_state(round_["id"], state)

    def _ingest(self, round_, link, component, result):
        child = link["manifest"]
        plan = load_measurement_plan(child["plan"], measurement=child["measurement"], runtime=child["runtime"], run_id=child["runner_run_id"])
        if result.run_id != child["runner_run_id"] or result.plan is None or result.plan.plan_sha256 != plan.plan_sha256:
            raise ValueError("Runner result differs from its reviewed plan")
        frozen = {o["observation_id"]: o for o in child["plan"]["observation_plan"]["observations"]}
        rows = []
        for observation in result.observations:
            if _value(observation.status) != "succeeded":
                continue
            expected_observation = frozen[observation.observation_id]
            metadata = _value(observation.client_research)
            direct = link["kind"] == "identification"
            common = {k: child["measurement"]["client_research"][k] for k in ("workspace_id", "directory_revision", "config_revision")}
            keys = ("branch_id", "account_id", "source_provider", "judge_provider", "profile_id", "judge_profile_id", "direct_fingerprint", "identity_fingerprint", "matching_fingerprint") if direct else ("cell_id", "area_id", "treatment_id", "query_fingerprint", "profile_id")
            expected_metadata = {**common, **{k: expected_observation[k] for k in keys}}
            expected_metadata["capability"] = expected_observation["client_research_capability"]
            if any(metadata.get(k) != v for k, v in expected_metadata.items()) or _value(observation.platform) != expected_observation["platform"] or observation.profile_id != expected_observation["profile_id"]:
                raise ValueError("Accepted observation correlation differs from its plan")
            prompt_hash = hashlib.sha256(expected_observation["prompt_text"].encode()).hexdigest()
            operations = ("identification_source", "identification_judge") if direct else ("measurement_turn_1", "same_chat_turn_2")
            if (metadata.get("source_prompt_sha256") != prompt_hash
                    or observation.prompt_sha256 not in {None, prompt_hash}
                    or any(not observation.accepted_attempt_ids.get(action) for action in operations)):
                raise ValueError("Accepted observation lacks its source prompt or operation fence bindings")
            source = component.read_artifact(result.run_id, observation.artifacts.turn_1).decode("utf-8")
            if metadata.get("source_answer_sha256") != hashlib.sha256(source.encode()).hexdigest():
                raise ValueError("Accepted source answer lacks its frozen content hash")
            parsed = component.read_artifact(result.run_id, observation.artifacts.turn_2).decode("utf-8")
            event_id = "result_" + canonical_sha256([result.run_id, observation.observation_id,
                _value(observation.accepted_attempt_ids), observation.artifacts.turn_1.sha256, observation.artifacts.turn_2.sha256])
            row = {**metadata, "kind": link["kind"], "status": "succeeded", "result_id": event_id,
                "run_id": result.run_id, "round_id": round_["id"], "provider": _value(observation.platform),
                "source_prompt": expected_observation["prompt_text"], "source_answer": source,
                "collected_at": observation.collection_metadata.get("captured_at"),
                "accepted_attempt_ids": _value(observation.accepted_attempt_ids),
                "source_artifact": asdict(observation.artifacts.turn_1), "classification_artifact": asdict(observation.artifacts.turn_2)}
            if direct:
                refs = [r for r in observation.artifacts.additional if r.sha256 == metadata["judge_request_sha256"]]
                if len(refs) != 1 or hashlib.sha256(parsed.encode()).hexdigest() != metadata["judge_answer_sha256"]:
                    raise ValueError("Independent judge artifacts are incomplete")
                row.update(source_profile_id=observation.profile_id, reference_identity=expected_observation["reference_identity"],
                    ambiguity_context=expected_observation["ambiguity_context"], judgment=parsed,
                    judge_request=component.read_artifact(result.run_id, refs[0]).decode("utf-8"), judge_answer=parsed)
                from visibility_runner.api import identification_judge_prompt
                exact_judge = identification_judge_prompt(expected_observation["prompt_text"], source,
                    expected_observation["reference_identity"], expected_observation["ambiguity_context"])
                if row["judge_request"] != exact_judge:
                    raise ValueError("Judge request differs from the exact frozen source answer and identity")
            else:
                row["extraction"] = parsed
            accepted = adapt_client_research_result(row, expected={**expected_metadata, "run_id": result.run_id,
                "source_prompt": expected_observation["prompt_text"]})
            accepted.update(kind=link["kind"], runner_run_id=result.run_id, runner_result_id=event_id)
            rows.append(accepted)
        self.service.ingest_evidence(round_["id"], rows)
        return [row["runner_result_id"] for row in rows]
