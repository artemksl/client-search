"""Offline acceptance for conservative client research priority projection."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from clinic_geo.outreach.client_research_assessment import (
    branch_identity_fingerprint, branch_matching_fingerprint, build_matching_context,
    project_client_research, resolve_recommendation_name,
)

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
STAMP = "2026-10-11T10:00:00+00:00"


def directory():
    return {"country_code": "FR", "city": "Paris", "accounts": [
        {"account_id": "a", "account_name": "Belle Clinique", "account_aliases": ["Belle"]}],
        "branches": [{"branch_id": "b", "account_id": "a", "branch_name": "Belle Montmartre",
            "branch_aliases": ["Clinique Café"], "country_code": "FR", "city": "Paris",
            "area": "Montmartre", "address": "12 Rue Lepic, Paris", "website": "https://belle.fr",
            "treatment_ids": ["laser"]}]}


def config():
    return {"cells": [{"cell_id": "c", "area_id": "montmartre", "area": "Montmartre",
        "treatment_id": "laser", "query_fingerprint": "q"}],
        "required_samples": [{"provider": "chatgpt", "profile_id": "one"},
                             {"provider": "gemini", "profile_id": "one"}],
        "direct_freshness_days": 30, "recommendation_freshness_hours": 144}


def round_(directory_=None, config_=None, **changes):
    return {"id": "r", "state": "succeeded", "kind": "recommendations", "created_at": STAMP,
            "manifest": {"directory": directory_ or directory(), "configuration": config_ or config()}, **changes}


def direct(directory_=None, decision="could_not_identify", **changes):
    data = directory_ or directory()
    context = build_matching_context(data)
    branch = data["branches"][0]
    return {"id": "direct", "kind": "identification", "branch_id": branch["branch_id"],
        "branch_identity_fingerprint": branch_identity_fingerprint(branch, data["accounts"][0]),
        "branch_matching_fingerprint": branch_matching_fingerprint(branch, context),
        "status": "accepted", "collected_at": STAMP, "decision": decision, **changes}


def generic(provider="chatgpt", names=(), **changes):
    return {"id": provider, "kind": "recommendation", "round_id": "r", "cell_id": "c",
        "query_fingerprint": "q", "provider": provider, "profile_id": "one", "status": "accepted",
        "collected_at": STAMP, "extraction": {"response_status": "answered",
            "recommendations": [{"name": name, "quote": f"I recommend {name}."} for name in names],
            "other_mentions": [], "inconclusive_evidence": []}, **changes}


def projection(evidence, rounds=None, directory_=None, config_=None, now=NOW):
    return project_client_research(directory_ or directory(), config_ or config(), evidence,
                                   rounds if rounds is not None else [round_()], now)


@pytest.mark.parametrize("decision,priority", [("could_not_identify", "P1"), ("identified", "P2"), ("unclear", None)])
def test_complete_negative_requires_current_direct_decision(decision, priority):
    result = projection([direct(decision=decision), generic(), generic("gemini")])
    assert result["branches"][0]["priority"] == priority


def test_fresh_positive_overrides_direct_denial_and_incomplete_coverage():
    result = projection([direct(), generic(names=["Belle Montmartre"])])
    branch = result["branches"][0]
    assert branch["priority"] == "P3"
    assert branch["recommendation"]["accepted_count"] == 1
    assert branch["recommendation"]["required_count"] == 2


@pytest.mark.parametrize("unusable", ["failed", "inconclusive", "reviewed"])
def test_missing_failed_inconclusive_reviewed_samples_never_form_absence(unusable):
    item = generic("gemini")
    if unusable == "failed":
        item["status"] = "failed"
    elif unusable == "inconclusive":
        item["extraction"]["response_status"] = "inconclusive"
    else:
        item["reviewed"] = True
    result = projection([direct(), generic(), item])
    assert result["branches"][0]["priority"] is None
    assert result["branches"][0]["state"] == "needs_review"


def test_valid_warning_is_not_recommendation():
    item = generic()
    item["extraction"]["other_mentions"] = [{"name": "Belle Montmartre", "quote": "Avoid Belle Montmartre", "kind": "not_recommended"}]
    assert projection([direct(), item, generic("gemini")])["branches"][0]["priority"] == "P1"


def test_related_ambiguous_mention_blocks_negative_coverage():
    item = generic()
    item["extraction"]["other_mentions"] = [{"name": "Belle Montmartre", "quote": "Maybe Belle Montmartre", "kind": "ambiguous"}]
    result = projection([direct(), item, generic("gemini")])
    assert result["branches"][0]["state"] == "needs_review"


def test_brand_mention_does_not_recommend_all_branches_or_establish_absence():
    data = directory()
    data["branches"].append({**data["branches"][0], "branch_id": "b2", "branch_name": "Belle Batignolles", "branch_aliases": []})
    result = projection([direct(data), generic(names=["Belle Clinique"]), generic("gemini")],
                        rounds=[round_(data)], directory_=data)
    assert all(branch["priority"] is None for branch in result["branches"])
    assert all(branch["state"] == "needs_review" for branch in result["branches"])


def test_unique_single_location_account_alias_resolves():
    context = build_matching_context(directory())
    assert resolve_recommendation_name(" BELLE ", context) == {"state": "matched", "branch_ids": ["b"], "account_ids": ["a"]}


def test_nfc_case_and_whitespace_normalization_without_fuzzy_matching():
    context = build_matching_context(directory())
    assert resolve_recommendation_name("  CLINIQUE Cafe\u0301 ", context)["branch_ids"] == ["b"]
    assert resolve_recommendation_name("Clinique Cafe", context)["state"] == "unmatched"


def test_shared_alias_across_supplied_accounts_is_ambiguous():
    data = directory()
    data["accounts"].append({"account_id": "a2", "account_name": "Other", "account_aliases": ["Belle"]})
    data["branches"].append({**data["branches"][0], "branch_id": "b2", "account_id": "a2", "branch_name": "Other Branch", "branch_aliases": []})
    assert resolve_recommendation_name("Belle", build_matching_context(data))["state"] == "ambiguous"


def test_new_alias_collision_invalidates_historical_attribution():
    old = directory()
    changed = deepcopy(old)
    changed["accounts"].append({"account_id": "a2", "account_name": "Other", "account_aliases": []})
    changed["branches"].append({**changed["branches"][0], "branch_id": "b2", "account_id": "a2", "branch_name": "Other", "branch_aliases": ["Belle Montmartre"]})
    result = projection([direct(), generic(names=["Belle Montmartre"])], directory_=changed)
    assert all(branch["priority"] is None for branch in result["branches"])
    assert result["branches"][0]["state"] == "needs_refresh"


def test_added_branch_cannot_inherit_preimport_absence():
    changed = directory()
    changed["branches"].append({**changed["branches"][0], "branch_id": "new", "branch_name": "New", "branch_aliases": []})
    result = projection([direct(), generic(), generic("gemini")], directory_=changed)
    branch = next(branch for branch in result["branches"] if branch["branch_id"] == "new")
    assert branch["state"] == "awaiting_checks"
    assert branch["priority"] is None


def test_new_terminal_failure_removes_prior_negative_without_stitching():
    newer = round_(id="new", state="failed", created_at="2026-10-11T11:00:00Z")
    result = projection([direct(), generic(), generic("gemini")], [round_(), newer])
    assert result["branches"][0]["state"] == "needs_review"
    assert result["branches"][0]["priority"] is None


def test_new_round_cannot_fill_old_missing_samples():
    newer = round_(id="new", created_at="2026-10-11T11:00:00Z")
    result = projection([direct(), generic(), generic("gemini", round_id="new")], [round_(), newer])
    assert result["branches"][0]["priority"] is None
    assert result["branches"][0]["recommendation"]["accepted_count"] == 1


@pytest.mark.parametrize("state", ["failed", "succeeded"])
def test_fresh_older_positive_survives_new_failed_or_nohit_round(state):
    newer = round_(id="new", state=state, created_at="2026-10-11T11:00:00Z")
    evidence = [direct(), generic(names=["Belle Montmartre"])]
    if state == "succeeded":
        evidence += [generic(round_id="new"), generic("gemini", round_id="new")]
    branch = projection(evidence, [round_(), newer])["branches"][0]
    assert branch["priority"] == "P3"
    assert branch["recommendation"]["collected_at"] == STAMP
    assert branch["recommendation"]["latest_check_incomplete"] == (state == "failed")


def test_checking_retains_previous_fresh_absence_until_terminal():
    newer = round_(id="new", state="running", created_at="2026-10-11T11:00:00Z")
    branch = projection([direct(), generic(), generic("gemini")], [round_(), newer])["branches"][0]
    assert branch["priority"] == "P1"
    assert branch["recommendation"]["checking_now"] is True


@pytest.mark.parametrize("which", ["direct", "generic"])
def test_expired_evidence_requires_refresh(which):
    evidence = [direct(), generic(), generic("gemini")]
    stale = "2026-08-01T10:00:00Z"
    for item in evidence:
        if (item["kind"] == "identification") == (which == "direct"):
            item["collected_at"] = stale
    result = projection(evidence)
    assert result["branches"][0]["state"] == "needs_refresh"
    assert result["branches"][0]["priority"] is None


def test_no_selected_cell_is_outside_scope_and_no_round_is_pending():
    config_ = config()
    config_["cells"] = []
    assert projection([direct()], config_=config_)["branches"][0]["state"] == "outside_selected_scope"
    pending = projection([direct()], [])['branches'][0]
    assert pending["state"] == "awaiting_checks"
    assert pending["potential_p1"] is True


def test_duplicate_attempt_does_not_inflate_denominator_or_coverage():
    evidence = [direct(), generic(), generic(id="duplicate"), generic("gemini")]
    branch = projection(evidence)["branches"][0]
    assert branch["recommendation"]["accepted_count"] == 2
    assert branch["recommendation"]["required_count"] == 2


def test_two_comparable_complete_negative_rounds_add_repeated_gap():
    newer = round_(id="new", created_at="2026-10-11T11:00:00Z")
    evidence = [direct(), generic(), generic("gemini"), generic(round_id="new"), generic("gemini", round_id="new")]
    assert projection(evidence, [round_(), newer])["branches"][0]["repeated_gap"] is True
    middle = round_(id="middle", state="failed", created_at="2026-10-11T10:30:00Z")
    assert projection(evidence, [round_(), middle, newer])["branches"][0]["repeated_gap"] is False


def test_account_chooses_best_confirmed_branch_and_marks_mixed_visibility():
    data = directory()
    data["branches"].append({**data["branches"][0], "branch_id": "b2", "branch_name": "Belle Batignolles", "branch_aliases": []})
    evidence = [direct(data), generic(names=["Belle Batignolles"]), generic("gemini")]
    result = projection(evidence, rounds=[round_(data)], directory_=data)
    assert result["accounts"][0]["priority"] == "P1"
    assert result["accounts"][0]["reason_branch_id"] == "b"
    assert result["accounts"][0]["mixed_visibility"] is True


def test_identity_fingerprint_ignores_treatments_but_matching_tracks_membership():
    data = directory()
    branch = data["branches"][0]
    identity = branch_identity_fingerprint(branch, data["accounts"][0])
    matching = branch_matching_fingerprint(branch, build_matching_context(data))
    branch["treatment_ids"] = ["other"]
    assert branch_identity_fingerprint(branch, data["accounts"][0]) == identity
    data["branches"].append({**branch, "branch_id": "new", "branch_name": "Second", "branch_aliases": []})
    assert branch_matching_fingerprint(branch, build_matching_context(data)) != matching


def test_projection_does_not_mutate_frozen_inputs():
    data, config_, evidence, rounds = directory(), config(), [direct(), generic(), generic("gemini")], [round_()]
    original = deepcopy((data, config_, evidence, rounds))
    project_client_research(data, config_, evidence, rounds, NOW)
    assert (data, config_, evidence, rounds) == original


def test_positive_in_unchanged_cell_survives_additional_unchecked_cell():
    data = directory()
    data["branches"][0]["treatment_ids"].append("second")
    newconfig = config()
    newconfig["cells"].append({"cell_id": "new-cell", "area": "Montmartre", "area_id": "montmartre",
        "treatment_id": "second", "query_fingerprint": "new-q"})
    result = projection([generic(names=["Belle Montmartre"])], directory_=data, config_=newconfig)
    assert result["branches"][0]["priority"] == "P3"
    assert result["branches"][0]["recommendation"]["required_count"] == 4


def test_direct_context_assignments_survive_subset_and_include_full_shared_host():
    from clinic_geo.outreach.client_research_assessment import direct_branch_context
    data = directory()
    data["branches"].append({**data["branches"][0], "branch_id": "b2", "branch_name": "Belle Batignolles", "branch_aliases": []})
    context = build_matching_context(data)
    configuration = {**config(), "profile_ids": ["first", "second", "third"], "country_name": "France"}
    result = direct_branch_context("b", context, configuration, "workspace")
    assert result["ambiguity_context"]["shared_hosts"] == ["belle.fr"]
    assert result["ambiguity_context"]["account_branch_ids"] == ["b", "b2"]
    assert data["branches"][0]["address"] not in result["source_prompt"]
    assert data["branches"][0]["website"] not in result["source_prompt"]
    assert "France" in result["source_prompt"]
    assert direct_branch_context("b", context, configuration, "workspace") == result
    changed = {**configuration, "judge_provider": "copilot"}
    assert direct_branch_context("b", context, changed, "workspace")["direct_fingerprint"] != result["direct_fingerprint"]


def test_direct_fingerprint_rejects_old_provider_policy_evidence():
    result = projection([direct(direct_fingerprint="old"), generic(), generic("gemini")],
        config_={**config(), "direct_fingerprints": {"b": "new"}})
    assert result["branches"][0]["state"] == "needs_refresh"
    assert result["branches"][0]["priority"] is None


def test_future_capture_cannot_establish_completed_negative():
    item = generic("gemini", collected_at="2026-10-12T10:00:00Z")
    assert projection([direct(), generic(), item])["branches"][0]["priority"] is None


def test_source_template_uses_runner_json_names_and_exact_data():
    from clinic_geo.outreach.client_research_assessment import direct_branch_context
    data = directory()
    data["branches"][0]["branch_name"] = 'Belle "Café"'
    item = direct_branch_context("b", build_matching_context(data), {"profiles": ["one"],
        "source_template": "Identify {branch_name} of {account_name} in {area}, {city}, {country_name}"},
        "workspace", country_name="France")
    assert item["source_prompt"] == 'Identify "Belle \\"Café\\"" of "Belle Clinique" in Montmartre, Paris, France'


def test_shared_host_context_includes_www_spelling_variant_outside_refresh_subset():
    from clinic_geo.outreach.client_research_assessment import direct_branch_context
    data = directory()
    data["branches"].append({**data["branches"][0], "branch_id": "sibling", "branch_name": "Belle Second",
                             "branch_aliases": [], "website": "https://www.belle.fr/second"})
    result = direct_branch_context("b", build_matching_context(data), {"profiles": ["one"]}, "workspace")
    assert result["ambiguity_context"]["website_host_shared"] is True
    assert result["ambiguity_context"]["host_index"] == {"belle.fr": ["b", "sibling"]}


def test_failed_explicit_direct_refresh_invalidates_prior_denial_priority():
    newest = {"id": "direct-refresh", "kind": "identification", "state": "failed",
        "created_at": "2026-10-11T11:00:00Z", "manifest": {"children": [{"kind": "identification",
            "measurement": {"client_research": {"branches": [{"branch_id": "b"}]}}}]}}
    branch = projection([direct(round_id="earlier"), generic(), generic("gemini")], [round_(), newest])["branches"][0]
    assert branch["priority"] is None
    assert branch["state"] == "needs_review"
    assert branch["identification"]["previous_assessment"]["collected_at"] == STAMP


def test_active_explicit_direct_refresh_shows_checking_and_retains_prior_date():
    newest = {"id": "direct-refresh", "kind": "identification", "state": "running",
        "created_at": "2026-10-11T11:00:00Z", "manifest": {"children": [{"kind": "identification",
            "measurement": {"client_research": {"branches": [{"branch_id": "b"}]}}}]}}
    branch = projection([direct(round_id="earlier"), generic(), generic("gemini")], [round_(), newest])["branches"][0]
    assert branch["priority"] is None
    assert branch["state"] == "checking_identification"
    assert branch["identification"]["checking_now"] is True


def test_direct_failure_never_removes_a_fresh_generic_positive():
    newest = {"id": "direct-refresh", "kind": "identification", "state": "failed",
        "created_at": "2026-10-11T11:00:00Z", "manifest": {"children": [{"kind": "identification",
            "measurement": {"client_research": {"branches": [{"branch_id": "b"}]}}}]}}
    branch = projection([direct(round_id="earlier"), generic(names=["Belle Montmartre"])], [round_(), newest])["branches"][0]
    assert branch["priority"] == "P3"
    assert branch["recommendation"]["collected_at"] == STAMP
