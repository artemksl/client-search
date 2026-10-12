"""Strict source-grounding and public plan correlation without model calls."""
from copy import deepcopy
import hashlib

import pytest

from clinic_geo.visibility_reports.client_research_evidence import (
    ClientResearchEvidenceError, adapt_client_research_result,
    validate_identification_judgment, validate_recommendation_extraction,
)


def judgment(decision="identified"):
    return {"schema_version": "client_search.identification_judgment.v1", "decision": decision,
        "entity_scope": "branch" if decision == "identified" else "none",
        "reason_code": "distinguishing_identity_matches" if decision == "identified" else "explicit_non_identification",
        "evidence": [{"quote": "https://belle.fr", "supports": "website"}] if decision == "identified" else
                    [{"quote": "I cannot identify that branch.", "supports": "explicit_non_identification"}],
        "explanation": "Grounded classification"}


def extraction():
    return {"schema_version": "client_search.recommendation_extraction.v1", "response_status": "answered",
            "recommendations": [{"name": "Belle Café", "quote": "I recommend Belle Café."}],
            "other_mentions": [], "inconclusive_evidence": []}


def envelope():
    return {"result_id": "result", "run_id": "run", "workspace_id": "work",
        "directory_revision": "dir", "config_revision": "cfg", "kind": "identification",
        "status": "accepted", "source_prompt": "Can you identify Belle Montmartre in Paris?",
        "source_answer": "The official website is https://belle.fr", "captured_at": "2026-10-11T10:00:00Z",
        "branch_id": "b", "identity_fingerprint": "identity", "matching_fingerprint": "matching",
        "direct_fingerprint": "direct", "source_provider": "chatgpt", "judge_provider": "gemini",
        "source_profile_id": "one", "judge_profile_id": "two",
        "reference_identity": {"website": "https://belle.fr", "address": "12 Rue Lepic, Paris"},
        "ambiguity_context": {"host_index": {"belle.fr": ["b"]}}, "judgment": judgment()}


def test_grounded_direct_result_keeps_original_judgment_and_full_unicode_answer():
    item = envelope()
    item["source_answer"] += " — Clinique Café à Paris."
    result = adapt_client_research_result(item, {"run_id": "run", "branch_id": "b"})
    assert result["decision"] == "identified"
    assert result["source_answer"] == item["source_answer"]
    assert result["branch_identity_fingerprint"] == "identity"
    assert result["judgment"] == item["judgment"]


@pytest.mark.parametrize("mutation", ["same_chatbot", "bad_hash", "wrong_plan", "no_offset", "empty_answer", "failed"])
def test_execution_and_provenance_failures_are_rejected(mutation):
    item = envelope()
    expected = None
    if mutation == "same_chatbot":
        item["judge_provider"] = "chatgpt"
    elif mutation == "bad_hash":
        item["source_answer_sha256"] = "wrong"
    elif mutation == "wrong_plan":
        expected = {"workspace_id": "different"}
    elif mutation == "no_offset":
        item["captured_at"] = "2026-10-11T10:00:00"
    elif mutation == "empty_answer":
        item["source_answer"] = ""
    else:
        item["status"] = "failed"
    with pytest.raises(ClientResearchEvidenceError):
        adapt_client_research_result(item, expected)


def test_grounded_explicit_denial_is_not_timeout_or_missing_capture():
    item = envelope()
    item["source_answer"] = "I cannot identify that branch."
    item["judgment"] = judgment("could_not_identify")
    assert adapt_client_research_result(item)["decision"] == "could_not_identify"


def test_shared_host_alone_becomes_unclear():
    item = envelope()
    item["ambiguity_context"]["host_index"]["belle.fr"].append("sibling")
    result = adapt_client_research_result(item)
    assert result["decision"] == "unclear"
    assert result["acceptance_reason"] == "shared_host_only"
    assert result["judgment"]["decision"] == "identified"


def test_full_matching_address_distinguishes_branch_on_shared_host():
    item = envelope()
    item["ambiguity_context"]["shared_hosts"] = ["belle.fr"]
    item["source_answer"] += " at 12 Rue Lepic, Paris"
    item["judgment"]["evidence"] = [{"quote": "12 Rue Lepic, Paris", "supports": "address"}]
    assert adapt_client_research_result(item)["decision"] == "identified"


def test_website_host_suffix_attack_is_not_matching_anchor():
    item = envelope()
    item["source_answer"] = "The site is https://belle.fr.evil.invalid"
    item["judgment"]["evidence"][0]["quote"] = item["source_answer"]
    assert adapt_client_research_result(item)["decision"] == "unclear"


def test_prompt_echo_and_denial_plus_identification_are_unclear():
    item = envelope()
    item["source_prompt"] += " supplied website https://belle.fr"
    assert adapt_client_research_result(item)["decision"] == "unclear"
    item = envelope()
    item["source_answer"] += " I cannot identify that branch."
    item["judgment"] = judgment("could_not_identify")
    assert adapt_client_research_result(item)["decision"] == "unclear"


@pytest.mark.parametrize("mutation", ["unknown_field", "invented_quote", "wrong_scope", "bad_supports", "empty_evidence", "duplicate_json"])
def test_direct_judgment_is_closed_and_source_grounded(mutation):
    value = judgment()
    if mutation == "unknown_field":
        value["another"] = "field"
    elif mutation == "invented_quote":
        value["evidence"][0]["quote"] = "Not in answer"
    elif mutation == "wrong_scope":
        value["entity_scope"] = "account"
    elif mutation == "bad_supports":
        value["evidence"][0]["supports"] = "guess"
    elif mutation == "empty_evidence":
        value["evidence"] = []
    else:
        value = '{"decision":"identified","decision":"could_not_identify"}'
    with pytest.raises(ClientResearchEvidenceError):
        validate_identification_judgment(value, "https://belle.fr")


def test_recommendation_lists_are_exact_grounded_entries():
    result = validate_recommendation_extraction(extraction(), "I recommend Belle Café.")
    assert result["recommendations"][0]["name"] == "Belle Café"


@pytest.mark.parametrize("mutation", ["invented_name", "invented_quote", "cross_entry_name", "extra_field", "false_inconclusive", "empty_inconclusive"])
def test_generic_invalid_or_inconclusive_extraction_cannot_be_empty_coverage(mutation):
    value = extraction()
    answer = "I recommend Belle Café. Avoid Other Clinic."
    if mutation == "invented_name":
        value["recommendations"][0]["name"] = "Invented"
    elif mutation == "invented_quote":
        value["recommendations"][0]["quote"] = "A different quote"
    elif mutation == "cross_entry_name":
        value["recommendations"][0]["quote"] = "Avoid Other Clinic."
    elif mutation == "extra_field":
        value["recommendations"][0]["score"] = 10
    elif mutation == "false_inconclusive":
        value["inconclusive_evidence"] = [{"quote": "Avoid Other Clinic.", "reason_code": "capability_limited"}]
    else:
        value["response_status"] = "inconclusive"
        value["recommendations"] = []
    with pytest.raises(ClientResearchEvidenceError):
        validate_recommendation_extraction(value, answer)


def test_valid_inability_is_inconclusive_and_never_a_recommendation():
    value = extraction()
    value.update({"response_status": "inconclusive", "recommendations": [],
        "inconclusive_evidence": [{"quote": "I cannot answer this request.", "reason_code": "capability_limited"}]})
    assert validate_recommendation_extraction(value, "I cannot answer this request.")["response_status"] == "inconclusive"


def test_generic_adapter_checks_frozen_sample_and_source_hash():
    item = envelope()
    item.update({"kind": "recommendation", "cell_id": "cell", "provider": "copilot", "profile_id": "one",
        "query_fingerprint": "query", "source_answer": "I recommend Belle Café.", "extraction": extraction()})
    item["source_answer_sha256"] = hashlib.sha256(item["source_answer"].encode()).hexdigest()
    result = adapt_client_research_result(item, {"provider": "copilot", "profile_id": "one"})
    assert result["extraction"] == extraction()
    assert result["kind"] == "recommendation"


def test_adapter_does_not_edit_immutable_input():
    item = envelope()
    original = deepcopy(item)
    adapt_client_research_result(item)
    assert item == original
