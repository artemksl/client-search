"""Independent acceptance of installed public plans and neutral evidence ingestion.

Only the public pure planner and synthetic immutable result/artifact ports are
used. These tests never start the guarded worker or call a browser/model.
"""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
import visibility_runner as runner
from visibility_runner.api import identification_judge_prompt

from clinic_geo.integrations.visibility_runner import load_measurement_plan
from clinic_geo.visibility_reports.api import ClientResearchEvidenceError
from test_client_research_execution import setup, start_args


STAMP = "2026-10-11T00:00:00+00:00"


class VerifiedArtifacts:
    def __init__(self):
        self.content = {}
        self.reads = []

    def reference(self, label, text):
        raw = text.encode("utf-8")
        reference = runner.ArtifactReference(label, hashlib.sha256(raw).hexdigest(), len(raw), "text/plain")
        self.content[label] = raw
        return reference

    def read_artifact(self, run_id, reference):
        self.reads.append((run_id, reference.relative_path))
        raw = self.content[reference.relative_path]
        if len(raw) != reference.byte_length or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError("Synthetic artifact integrity failed")
        return raw


def reserve(s, mode="initial"):
    preview = s.coordinator.preview_checks(s.w["id"], mode=mode)
    manifest = s.service.get_preview(s.w["id"], preview["preview_id"], "checks")["payload"]
    return s.service.reserve_round(s.w["id"], manifest=manifest, **start_args(preview))


def accepted_result(link, *, direct=False, inconclusive=False, name=None, mismatch=None,
                    omitted=(), prompt_hash=None, accepted_fences=None, judge_answer=None,
                    captured_at=STAMP):
    child = link["manifest"]
    plan = load_measurement_plan(child["plan"], measurement=child["measurement"],
                                 runtime=child["runtime"], run_id=child["runner_run_id"])
    artifacts = VerifiedArtifacts()
    observations = []
    for frozen in child["plan"]["observation_plan"]["observations"]:
        common = child["measurement"]["client_research"]
        metadata = {key: common[key] for key in ("workspace_id", "directory_revision", "config_revision")}
        keys = ("branch_id", "account_id", "source_provider", "judge_provider", "profile_id", "judge_profile_id",
                "direct_fingerprint", "identity_fingerprint", "matching_fingerprint") if direct else (
                "cell_id", "area_id", "treatment_id", "query_fingerprint", "profile_id")
        metadata.update({key: frozen[key] for key in keys})
        if direct:
            answer = "I cannot identify that exact clinic branch. — réponse complète."
            parsed = {"schema_version": "client_search.identification_judgment.v1", "decision": "could_not_identify",
                "entity_scope": "none", "reason_code": "explicit_non_identification",
                "evidence": [{"quote": "I cannot identify that exact clinic branch.", "supports": "explicit_non_identification"}],
                "explanation": "The answer explicitly denies identification of this branch."}
            request = identification_judge_prompt(frozen["prompt_text"], judge_answer or answer,
                frozen["reference_identity"], frozen["ambiguity_context"])
            request_ref = artifacts.reference(frozen["observation_id"] + "-request", request)
            metadata.update(judge_request_sha256=request_ref.sha256)
            extra = (request_ref,)
        else:
            answer = "I cannot answer this clinic recommendation request." if inconclusive else (
                f"I recommend {name}." if name else "No clinic is included in the recommendation list.")
            parsed = {"schema_version": "client_search.recommendation_extraction.v1",
                "response_status": "inconclusive" if inconclusive else "answered",
                "recommendations": [{"name": name, "quote": answer}] if name else [], "other_mentions": [],
                "inconclusive_evidence": [{"quote": answer, "reason_code": "capability_limited"}] if inconclusive else []}
            extra = ()
        source_ref = artifacts.reference(frozen["observation_id"] + "-source", answer)
        parsed_text = json.dumps(parsed, ensure_ascii=False)
        parsed_ref = artifacts.reference(frozen["observation_id"] + "-parsed", parsed_text)
        metadata.update(source_prompt_sha256=hashlib.sha256(frozen["prompt_text"].encode()).hexdigest(),
                        source_answer_sha256=source_ref.sha256)
        metadata["capability"] = "client_research.identification.v1" if direct else "client_research.recommendations.v1"
        if direct:
            metadata["judge_answer_sha256"] = parsed_ref.sha256
        if mismatch:
            metadata.update(mismatch)
        for key in omitted:
            metadata.pop(key, None)
        packet = next(packet for packet in plan.packets if frozen["observation_id"] in packet.observation_ids)
        observations.append(runner.ObservationResult(
            observation_id=frozen["observation_id"], packet_id=packet.packet_id,
            platform=runner.Platform(frozen["platform"]), profile_id=frozen["profile_id"],
            status=runner.ObservationStatus.SUCCEEDED, extraction_status=runner.ExtractionStatus.SUCCEEDED,
            prompt_sha256=prompt_hash or frozen["prompt_text_sha256"],
            artifacts=runner.ArtifactReferences(turn_1=source_ref, turn_2=parsed_ref, additional=extra),
            attempt_ids=tuple(accepted_fences.values()) if accepted_fences is not None else ("source-attempt", "parser-attempt"),
            accepted_attempt_ids=accepted_fences if accepted_fences is not None else {
                "identification_source" if direct else "measurement_turn_1": "source-attempt",
                "identification_judge" if direct else "same_chat_turn_2": "parser-attempt"},
            collection_metadata={"captured_at": captured_at}, client_research=metadata))
    packets = tuple(runner.PacketResult(packet.packet_id, runner.PacketStatus.SUCCEEDED,
        tuple(item for item in observations if item.packet_id == packet.packet_id)) for packet in plan.packets)
    return runner.RunResult(run_id=plan.run_id, status=runner.RunStatus.SUCCEEDED, packets=packets,
        submission_budget=plan.submission_budget, plan=plan), artifacts


def completed_initial(s, *, recommendation_name=None):
    round_ = reserve(s)
    for link in round_["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification",
            name=recommendation_name if link["kind"] == "recommendations" else None)
        s.coordinator._ingest(round_, link, artifacts, result)
        s.service.update_run_link(round_["id"], link["kind"], cursor="terminal",
            result={"status": "succeeded", "execution_id": "initial-execution"})
    s.service.set_round_state(round_["id"], "succeeded")
    return round_


def refetch_port(s, link, result, artifacts, *, job_status="failed"):
    artifacts.inspect = lambda run_id: result
    s.coordinator.component_factory = lambda runtime: artifacts
    s.jobs.saved[link["runner_run_id"]] = SimpleNamespace(id=link["runner_run_id"], workflow_id="client_research",
        status=job_status, run_config={"runner_owned_snapshot": True, "runner_execution_id": "refetch-execution"})


def failed_observations(result, *, retained_branch=None):
    return replace(result, status=runner.RunStatus.PARTIAL if retained_branch else runner.RunStatus.FAILED,
        packets=tuple(replace(packet, status=runner.PacketStatus.PARTIAL if retained_branch else runner.PacketStatus.FAILED,
            observations=tuple(observation if observation.client_research.get("branch_id") == retained_branch and retained_branch
                else replace(observation, status=runner.ObservationStatus.FAILED,
                    extraction_status=runner.ExtractionStatus.FAILED, client_research={},
                    accepted_attempt_ids={}, artifacts=runner.ArtifactReferences())
                for observation in packet.observations)) for packet in result.packets))


def test_public_installed_plan_full_coverage_denials_produce_one_account_with_two_p1_branches(setup):
    s = setup
    round_ = reserve(s)
    for link in round_["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification")
        s.coordinator._ingest(round_, link, artifacts, result)
    s.service.set_round_state(round_["id"], "succeeded")
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] == "P1"
    assert len(account["branches"]) == 2
    assert all(branch["priority"] == "P1" for branch in account["branches"])
    assert all(branch["recommendation"]["accepted_count"] == 21 for branch in account["branches"])
    assert s.jobs.starts == []


def test_valid_inconclusive_public_observations_leave_absence_uncovered(setup):
    s = setup
    round_ = reserve(s)
    for link in round_["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification", inconclusive=link["kind"] == "recommendations")
        s.coordinator._ingest(round_, link, artifacts, result)
    s.service.set_round_state(round_["id"], "partial")
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "needs_review" for branch in account["branches"])
    assert all(branch["recommendation"]["accepted_count"] == 0 for branch in account["branches"])


@pytest.mark.parametrize("mismatch", [{"profile_id": "different"}, {"directory_revision": "other"},
                                      {"query_fingerprint": "other"}, {"workspace_id": "other"}])
def test_accepted_metadata_must_match_exact_public_plan_before_any_artifact_read(setup, mismatch):
    s = setup
    round_ = reserve(s, "recommendations")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, mismatch=mismatch)
    with pytest.raises(ValueError, match="correlation"):
        s.coordinator._ingest(round_, link, artifacts, result)
    assert artifacts.reads == []
    with s.service.store.session() as c:
        assert c.execute("SELECT COUNT(*) FROM client_search_evidence").fetchone()[0] == 0


def test_direct_judge_artifacts_and_source_hashes_are_verified(setup):
    s = setup
    round_ = reserve(s, "identification")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, direct=True)
    source = result.observations[0].artifacts.turn_1
    artifacts.content[source.relative_path] = b"Changed source answer"
    with pytest.raises(ValueError, match="integrity"):
        s.coordinator._ingest(round_, link, artifacts, result)
    with s.service.store.session() as c:
        assert c.execute("SELECT COUNT(*) FROM client_search_evidence").fetchone()[0] == 0


@pytest.mark.parametrize("omitted", [("source_prompt_sha256",), ("source_answer_sha256",), ("capability",)])
def test_successful_public_result_requires_source_provenance_metadata(setup, omitted):
    s = setup
    round_ = reserve(s, "recommendations")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, omitted=omitted)
    with pytest.raises(ValueError):
        s.coordinator._ingest(round_, link, artifacts, result)
    assert s.service.round_evidence_count(round_["id"]) == 0


@pytest.mark.parametrize("direct", [False, True])
def test_successful_public_result_requires_both_accepted_operation_fences(setup, direct):
    s = setup
    round_ = reserve(s, "identification" if direct else "recommendations")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, direct=direct, accepted_fences={})
    with pytest.raises(ValueError):
        s.coordinator._ingest(round_, link, artifacts, result)
    assert s.service.round_evidence_count(round_["id"]) == 0


def test_successful_public_result_prompt_hash_must_match_exact_plan(setup):
    s = setup
    round_ = reserve(s, "recommendations")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, prompt_hash="f" * 64)
    with pytest.raises(ValueError):
        s.coordinator._ingest(round_, link, artifacts, result)
    assert artifacts.reads == []


def test_hash_valid_independent_judge_request_must_include_this_full_source_answer(setup):
    s = setup
    round_ = reserve(s, "identification")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, direct=True, judge_answer="I identified a different clinic.")
    with pytest.raises(ValueError):
        s.coordinator._ingest(round_, link, artifacts, result)
    assert s.service.round_evidence_count(round_["id"]) == 0


def test_ingestion_is_idempotent_and_full_unicode_answers_remain_viewable(setup):
    s = setup
    round_ = reserve(s, "identification")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, direct=True)
    s.coordinator._ingest(round_, link, artifacts, result)
    s.coordinator._ingest(round_, link, artifacts, result)
    with s.service.store.session() as c:
        rows = c.execute("SELECT id FROM client_search_evidence").fetchall()
    assert len(rows) == 2
    evidence = s.service.get_evidence(rows[0][0])
    assert evidence["source_answer"].endswith("réponse complète.")
    assert evidence["classification_artifact"]["sha256"]


def test_new_failed_direct_refresh_cannot_leave_old_p1_in_contact_queue(setup):
    s = setup
    initial = reserve(s)
    for link in initial["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification")
        s.coordinator._ingest(initial, link, artifacts, result)
    s.service.set_round_state(initial["id"], "succeeded")
    assert s.service.get_account(s.w["id"], "a")["priority"] == "P1"
    from datetime import timedelta
    s.now[0] += timedelta(hours=1)
    p = s.coordinator.preview_checks(s.w["id"], mode="identification")
    manifest = s.service.get_preview(s.w["id"], p["preview_id"], "checks")["payload"]
    newer = s.service.reserve_round(s.w["id"], manifest=manifest, **{**start_args(p), "request_id": "refresh"})
    s.service.set_round_state(newer["id"], "failed")
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "needs_review" for branch in account["branches"])


def test_new_direct_failure_preserves_fresh_recommendation_date(setup):
    s = setup
    initial = reserve(s)
    link = next(link for link in initial["runs"] if link["kind"] == "recommendations")
    result, artifacts = accepted_result(link, name="Alpha Central")
    s.coordinator._ingest(initial, link, artifacts, result)
    s.service.set_round_state(initial["id"], "partial")
    from datetime import timedelta
    s.now[0] += timedelta(hours=1)
    p = s.coordinator.preview_checks(s.w["id"], mode="identification", branch_ids=["b"])
    manifest = s.service.get_preview(s.w["id"], p["preview_id"], "checks")["payload"]
    newer = s.service.reserve_round(s.w["id"], manifest=manifest, **{**start_args(p), "request_id": "refresh"})
    s.service.set_round_state(newer["id"], "failed")
    branch = next(branch for branch in s.service.get_account(s.w["id"], "a")["branches"] if branch["branch_id"] == "b")
    assert branch["priority"] == "P3"
    assert branch["recommendation"]["collected_at"] == STAMP


def test_failed_public_generic_observations_are_not_ingested_or_negative_coverage(setup):
    s = setup
    round_ = reserve(s)
    direct = next(link for link in round_["runs"] if link["kind"] == "identification")
    result, artifacts = accepted_result(direct, direct=True)
    s.coordinator._ingest(round_, direct, artifacts, result)
    generic = next(link for link in round_["runs"] if link["kind"] == "recommendations")
    result, artifacts = accepted_result(generic)
    packets = tuple(replace(packet, status=runner.PacketStatus.FAILED, observations=tuple(
        replace(observation, status=runner.ObservationStatus.FAILED,
                extraction_status=runner.ExtractionStatus.FAILED, client_research={},
                accepted_attempt_ids={}, artifacts=runner.ArtifactReferences())
        for observation in packet.observations)) for packet in result.packets)
    result = replace(result, status=runner.RunStatus.FAILED, packets=packets)
    s.coordinator._ingest(round_, generic, artifacts, result)
    assert artifacts.reads == []
    s.service.set_round_state(round_["id"], "partial")
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["recommendation"]["accepted_count"] == 0 for branch in account["branches"])
    assert all(branch["state"] == "needs_review" for branch in account["branches"])


def test_old_direct_capture_cannot_be_renewed_by_ingestion_at_a_later_date(setup):
    from datetime import timedelta
    s = setup
    s.now[0] += timedelta(days=31)
    round_ = reserve(s)
    for link in round_["runs"]:
        direct = link["kind"] == "identification"
        result, artifacts = accepted_result(link, direct=direct,
            captured_at=STAMP if direct else s.now[0].isoformat())
        s.coordinator._ingest(round_, link, artifacts, result)
    s.service.set_round_state(round_["id"], "succeeded")
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "needs_refresh" for branch in account["branches"])
    assert all(branch["identification"]["collected_at"] == STAMP for branch in account["branches"])


def test_pending_explicit_direct_refresh_shows_checking_and_keeps_previous_capture_date(setup):
    from datetime import timedelta
    s = setup
    initial = reserve(s)
    for link in initial["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification")
        s.coordinator._ingest(initial, link, artifacts, result)
    s.service.set_round_state(initial["id"], "succeeded")
    s.now[0] += timedelta(hours=1)
    preview = s.coordinator.preview_checks(s.w["id"], mode="identification")
    manifest = s.service.get_preview(s.w["id"], preview["preview_id"], "checks")["payload"]
    s.service.reserve_round(s.w["id"], manifest=manifest, **{**start_args(preview), "request_id": "refresh"})
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "checking_identification" for branch in account["branches"])
    assert all(branch["identification"]["previous_assessment"]["collected_at"] == STAMP
               for branch in account["branches"])
    assert s.jobs.starts == []


def test_newer_failed_direct_round_wins_when_admission_timestamps_are_identical(setup, monkeypatch):
    import clinic_geo.outreach.client_research_service as service_module
    s = setup
    original_id = service_module.new_id
    round_ids = iter(("round_z_older", "round_a_newer"))
    monkeypatch.setattr(service_module, "new_id",
        lambda prefix: next(round_ids) if prefix == "round" else original_id(prefix))
    initial = reserve(s)
    for link in initial["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification")
        s.coordinator._ingest(initial, link, artifacts, result)
    s.service.set_round_state(initial["id"], "succeeded")
    assert s.service.get_account(s.w["id"], "a")["priority"] == "P1"
    preview = s.coordinator.preview_checks(s.w["id"], mode="identification")
    manifest = s.service.get_preview(s.w["id"], preview["preview_id"], "checks")["payload"]
    newer = s.service.reserve_round(s.w["id"], manifest=manifest,
        **{**start_args(preview), "request_id": "refresh"})
    assert newer["created_at"] == initial["created_at"]
    s.service.set_round_state(newer["id"], "failed")
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "needs_review" for branch in account["branches"])


def test_explicit_refetch_indexes_original_run_once_without_issuing_an_execution(setup):
    from datetime import timedelta
    s = setup
    round_ = reserve(s, "identification")
    link = round_["runs"][0]
    result, initial_artifacts = accepted_result(link, direct=True)
    s.coordinator._ingest(round_, link, initial_artifacts, result)
    s.service.update_run_link(round_["id"], link["kind"], cursor="terminal",
        result={"status": "succeeded", "execution_id": "initial-execution"})
    s.service.set_round_state(round_["id"], "succeeded")
    s.now[0] += timedelta(hours=2)
    result, refetched_artifacts = accepted_result(link, direct=True, captured_at=s.now[0].isoformat(),
        accepted_fences={"identification_source": "refetch-source", "identification_judge": "refetch-judge"})
    refetched_artifacts.inspect = lambda run_id: result
    s.coordinator.component_factory = lambda runtime: refetched_artifacts
    s.jobs.saved[link["runner_run_id"]] = SimpleNamespace(id=link["runner_run_id"], workflow_id="client_research",
        status="succeeded", run_config={"runner_owned_snapshot": True, "runner_execution_id": "refetch-execution"})
    s.coordinator.reconcile()
    assert s.service.round_evidence_count(round_["id"]) == 4
    refreshed = s.service.get_round(round_["id"])
    assert refreshed["runs"][0]["result"]["execution_id"] == "refetch-execution"
    assert refreshed["runs"][0]["runner_run_id"] == link["runner_run_id"]
    reads = len(refetched_artifacts.reads)
    s.coordinator.reconcile()
    assert len(refetched_artifacts.reads) == reads
    assert s.service.round_evidence_count(round_["id"]) == 4
    assert s.jobs.starts == []


def test_evidence_drawer_reads_full_answers_through_original_hash_verified_artifact_port(setup):
    s = setup
    round_ = reserve(s, "identification")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, direct=True)
    s.coordinator._ingest(round_, link, artifacts, result)
    with s.service.store.session() as c:
        identifier = c.execute("SELECT id FROM client_search_evidence LIMIT 1").fetchone()[0]
    s.coordinator.component_factory = lambda runtime: artifacts
    row = s.coordinator.read_evidence(identifier)
    assert row["source_answer"].endswith("réponse complète.")
    assert json.loads(row["classification_answer"])["decision"] == "could_not_identify"
    assert all(run_id == link["runner_run_id"] for run_id, path in artifacts.reads)
    assert s.jobs.starts == []


@pytest.mark.parametrize("field", ["source_artifact", "classification_artifact"])
def test_evidence_drawer_rejects_changed_runner_artifacts(setup, field):
    s = setup
    round_ = reserve(s, "identification")
    link = round_["runs"][0]
    result, artifacts = accepted_result(link, direct=True)
    s.coordinator._ingest(round_, link, artifacts, result)
    with s.service.store.session() as c:
        identifier = c.execute("SELECT id FROM client_search_evidence LIMIT 1").fetchone()[0]
    row = s.service.get_evidence(identifier)
    artifacts.content[row[field]["relative_path"]] = b"Changed evidence bytes"
    s.coordinator.component_factory = lambda runtime: artifacts
    with pytest.raises(ValueError, match="integrity"):
        s.coordinator.read_evidence(identifier)
    assert s.jobs.starts == []


def test_new_failed_explicit_direct_refetch_does_not_preserve_old_p1(setup):
    from datetime import timedelta
    s = setup
    round_ = reserve(s)
    for link in round_["runs"]:
        result, artifacts = accepted_result(link, direct=link["kind"] == "identification")
        s.coordinator._ingest(round_, link, artifacts, result)
        s.service.update_run_link(round_["id"], link["kind"], cursor="terminal",
            result={"status": "succeeded", "execution_id": "initial-execution"})
    s.service.set_round_state(round_["id"], "succeeded")
    assert s.service.get_account(s.w["id"], "a")["priority"] == "P1"
    s.now[0] += timedelta(hours=1)
    link = next(link for link in round_["runs"] if link["kind"] == "identification")
    result, artifacts = accepted_result(link, direct=True)
    result = replace(result, status=runner.RunStatus.FAILED, packets=tuple(
        replace(packet, status=runner.PacketStatus.FAILED, observations=tuple(
            replace(observation, status=runner.ObservationStatus.FAILED,
                extraction_status=runner.ExtractionStatus.FAILED, client_research={},
                accepted_attempt_ids={}, artifacts=runner.ArtifactReferences())
            for observation in packet.observations)) for packet in result.packets))
    artifacts.inspect = lambda run_id: result
    s.coordinator.component_factory = lambda runtime: artifacts
    s.jobs.saved[link["runner_run_id"]] = SimpleNamespace(id=link["runner_run_id"], workflow_id="client_research",
        status="failed", run_config={"runner_owned_snapshot": True, "runner_execution_id": "refetch-execution"})
    s.coordinator.reconcile()
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "needs_review" for branch in account["branches"])
    assert s.jobs.starts == []


@pytest.mark.parametrize("recommended", [False, True])
def test_new_failed_generic_refetch_removes_old_absence_and_preserves_fresh_affirmative(setup, recommended):
    from datetime import timedelta
    s = setup
    round_ = completed_initial(s, recommendation_name="Alpha Central" if recommended else None)
    s.now[0] += timedelta(hours=1)
    link = next(link for link in round_["runs"] if link["kind"] == "recommendations")
    result, artifacts = accepted_result(link)
    refetch_port(s, link, failed_observations(result), artifacts)
    s.coordinator.reconcile()
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] == ("P3" if recommended else None)
    assert all(branch["recommendation"]["accepted_count"] == 0 for branch in account["branches"])
    if recommended:
        branch = next(branch for branch in account["branches"] if branch["branch_id"] == "b")
        assert branch["recommendation"]["collected_at"] == STAMP
    else:
        assert all(branch["state"] == "needs_review" for branch in account["branches"])
    assert s.jobs.starts == []


def test_pending_direct_refetch_shows_checking_without_replacing_original_round(setup):
    from datetime import timedelta
    s = setup
    round_ = completed_initial(s)
    s.now[0] += timedelta(hours=1)
    link = next(link for link in round_["runs"] if link["kind"] == "identification")
    result, artifacts = accepted_result(link, direct=True)
    refetch_port(s, link, replace(result, status=runner.RunStatus.RUNNING, packets=()), artifacts, job_status="running")
    s.coordinator.reconcile()
    account = s.service.get_account(s.w["id"], "a")
    assert account["priority"] is None
    assert all(branch["state"] == "checking_identification" for branch in account["branches"])
    assert all(branch["identification"]["previous_assessment"]["collected_at"] == STAMP for branch in account["branches"])
    assert s.service.get_round(round_["id"])["state"] == "succeeded"
    assert s.jobs.starts == []


def test_partial_direct_refetch_preserves_unchanged_accepted_branch_in_current_result(setup):
    s = setup
    round_ = completed_initial(s)
    link = next(link for link in round_["runs"] if link["kind"] == "identification")
    result, artifacts = accepted_result(link, direct=True)
    refetch_port(s, link, failed_observations(result, retained_branch="b"), artifacts)
    s.coordinator.reconcile()
    account = s.service.get_account(s.w["id"], "a")
    branches = {branch["branch_id"]: branch for branch in account["branches"]}
    assert branches["b"]["priority"] == "P1"
    assert branches["b"]["identification"]["collected_at"] == STAMP
    assert branches["b2"]["priority"] is None
    assert branches["b2"]["state"] == "needs_review"
    assert s.jobs.starts == []
