"""Pure, conservative projections of supplied clinic research evidence.

The frozen directory is the only name resolver. No clinic discovery, fuzzy
matching, browser work, database reads or reconstruction of missing samples is
performed here. Time is injected so freshness never depends on a test's clock.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unicodedata
from urllib.parse import urlsplit
from typing import Any, Mapping, Sequence


RESOLVER_VERSION = "client_search.literal_resolver.v1"
ASSESSMENT_POLICY_VERSION = "client_search.assessment.v1"
_ACTIVE = {"queued", "running", "waiting_attention"}
_TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def normalize_identity(value: str) -> str:
    """NFC, case folding and whitespace only; never strip meaningful accents."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def canonical_fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _mapping(value: object) -> dict:
    return asdict(value) if is_dataclass(value) else dict(value)  # type: ignore[arg-type]


def _records(directory: Mapping | object, key: str) -> list[dict]:
    value = _mapping(directory).get(key, [])
    if isinstance(value, Mapping):
        value = value.values()
    return [_mapping(item) for item in value if _mapping(item).get("active", True)]


def _names(item: Mapping, prefix: str) -> set[str]:
    return {normalize_identity(str(name)) for name in
            [item.get(f"{prefix}_name", ""), *item.get(f"{prefix}_aliases", [])] if name}


def branch_identity_fingerprint(branch: Mapping, account: Mapping) -> str:
    """Treatment selection changes scope, not the supplied identity."""
    return canonical_fingerprint({
        "branch_id": branch["branch_id"], "account_id": branch["account_id"],
        "branch_names": sorted(_names(branch, "branch")),
        "account_names": sorted(_names(account, "account")),
        "country_code": branch.get("country_code", ""),
        **{key: normalize_identity(str(branch.get(key, "")))
           for key in ("city", "area", "address")},
        "website": str(branch.get("website", "")),
    })


def build_matching_context(directory: Mapping | object) -> dict:
    accounts = {item["account_id"]: item for item in _records(directory, "accounts")}
    branches = {item["branch_id"]: item for item in _records(directory, "branches")}
    membership = {account_id: sorted(branch_id for branch_id, branch in branches.items()
                  if branch["account_id"] == account_id) for account_id in accounts}
    index: dict[str, dict[str, set[str]]] = {}
    for kind, records, prefix, key in (("accounts", accounts, "account", "account_id"),
                                        ("branches", branches, "branch", "branch_id")):
        for item in records.values():
            for name in _names(item, prefix):
                index.setdefault(name, {"accounts": set(), "branches": set()})[kind].add(item[key])
    hosts: dict[str, list[str]] = {}
    for branch_id, branch in branches.items():
        host = (urlsplit(str(branch.get("website", ""))).hostname or "").casefold().removeprefix("www.")
        if host:
            hosts.setdefault(host, []).append(branch_id)
    return {"resolver_version": RESOLVER_VERSION, "accounts": accounts, "branches": branches,
            "membership": membership,
            "name_index": {name: {key: sorted(ids) for key, ids in entry.items()}
                           for name, entry in sorted(index.items())},
            "host_index": {host: sorted(ids) for host, ids in sorted(hosts.items())}}


def branch_matching_fingerprint(branch: Mapping | str, context: Mapping) -> str:
    branch = context["branches"][branch] if isinstance(branch, str) else branch
    account = context["accounts"][branch["account_id"]]
    relevant_names = _names(branch, "branch") | _names(account, "account")
    return canonical_fingerprint({
        "identity": branch_identity_fingerprint(branch, account), "resolver": RESOLVER_VERSION,
        "collisions": {name: context["name_index"][name] for name in sorted(relevant_names)},
        "members": context["membership"][branch["account_id"]],
    })



DEFAULT_DIRECT_SOURCE_TEMPLATE = (
    'Can you identify the clinic branch named {branch_name}{account_phrase}\n'
    'in {area}, {city}, {country_name}?\n\n'
    'Describe which clinic/location it is. If you know its official website or\n'
    'physical address, include it and give supporting sources when available.\n'
    'If you cannot identify this exact branch, say so explicitly.\n'
    'Do not guess or substitute another branch with a similar name.'
)


def direct_branch_context(branch_id: str, context: Mapping, configuration: Mapping,
                          workspace_id: str, city: str | None = None,
                          country_name: str | None = None) -> dict:
    """Freeze role assignments and direct policy without opening a conversation.

    Refreshing a subset uses the full-directory context, so shared hosts and
    sibling membership cannot disappear from its acceptance policy.
    """
    branch = context["branches"][branch_id]
    account = context["accounts"][branch["account_id"]]
    raw_profiles = configuration.get("profile_ids", configuration.get("profiles", []))
    profiles = [str(item.get("profile_id", item.get("id"))) if isinstance(item, Mapping) else str(item)
                for item in raw_profiles]
    if not profiles or len(profiles) != len(set(profiles)) or any(not value or value == "None" for value in profiles):
        raise ValueError("Direct checks require an explicit unique ordered profile list")
    source_provider = configuration.get("source_provider", configuration.get("direct_source_provider", "chatgpt"))
    judge_provider = configuration.get("judge_provider", configuration.get("direct_judge_provider", "gemini"))
    if not source_provider or not judge_provider or source_provider == judge_provider:
        raise ValueError("Direct checks require different source and judge providers")
    def role_profile(role: str) -> str:
        digest = canonical_fingerprint([workspace_id, branch_id, role])
        return profiles[int(digest, 16) % len(profiles)]
    source_profile, judge_profile = role_profile("source"), role_profile("judge")
    # Keep quoted names as JSON data literals, matching the runner's renderer.
    quoted_branch = json.dumps(str(branch["branch_name"]), ensure_ascii=False)
    quoted_account = json.dumps(str(account["account_name"]), ensure_ascii=False)
    phrase = '' if normalize_identity(branch["branch_name"]) == normalize_identity(account["account_name"]) else ' of ' + quoted_account
    template = configuration.get("source_template", configuration.get("source_prompt_template",
        configuration.get("direct_source_template", DEFAULT_DIRECT_SOURCE_TEMPLATE)))
    source_prompt = template.format(branch_name=quoted_branch, account_name=quoted_account,
        account_phrase=phrase, area=branch["area"], city=city or branch["city"],
        language=configuration.get("language", "en"),
        country_name=country_name or configuration.get("country_name", branch["country_code"]))
    names = _names(branch, "branch") | _names(account, "account")
    host = (urlsplit(str(branch.get("website", ""))).hostname or "").casefold().removeprefix("www.")
    host_candidates = context["host_index"].get(host, [])
    ambiguity = {"target_branch_id": branch_id,
        "host_index": {host: host_candidates} if host else {},
        "shared_hosts": [host] if len(host_candidates) > 1 else [],
        "website_host_shared": len(host_candidates) > 1,
        "account_branch_ids": context["membership"][branch["account_id"]],
        "membership": {branch["account_id"]: context["membership"][branch["account_id"]]},
        "name_index": {name: context["name_index"][name] for name in sorted(names)}}
    identity = branch_identity_fingerprint(branch, account)
    matching = branch_matching_fingerprint(branch, context)
    fingerprint = canonical_fingerprint({"identity": identity, "matching": matching,
        "ambiguity_context": ambiguity, "source_prompt": source_prompt,
        "source_provider": source_provider, "judge_provider": judge_provider,
        "source_profile_id": source_profile, "judge_profile_id": judge_profile,
        "judge_template": configuration.get("judge_prompt_template", configuration.get("judge_template", "client_search.identification_judge.v1")),
        "acceptance_policy": configuration.get("identification_policy_version", "client_search.identification_acceptance.v1")})
    return {**dict(branch), "account_name": account["account_name"],
        "identity_fingerprint": identity, "matching_fingerprint": matching,
        "direct_fingerprint": fingerprint, "source_prompt": source_prompt,
        "source_provider": source_provider, "judge_provider": judge_provider,
        "source_profile_id": source_profile, "judge_profile_id": judge_profile,
        "ambiguity_context": ambiguity}

def resolve_recommendation_name(name: str, context: Mapping) -> dict:
    """A shared brand cannot be attributed using the question's area."""
    entry = context["name_index"].get(normalize_identity(name))
    if not entry:
        return {"state": "unmatched", "branch_ids": [], "account_ids": []}
    branch_ids = set(entry["branches"])
    account_ids = set(entry["accounts"])
    account_ids.update(context["branches"][key]["account_id"] for key in branch_ids)
    relevant = branch_ids | {key for account_id in entry["accounts"]
                              for key in context["membership"][account_id]}
    # Account words on a multi-branch account are brand-level even when a
    # branch's supplied name happens to equal those same words.
    shared_account = any(len(context["membership"][key]) != 1 for key in entry["accounts"])
    if len(account_ids) > 1 or len(branch_ids) > 1:
        state = "ambiguous"
    elif shared_account:
        state = "brand_only"
    elif len(relevant) == 1:
        state = "matched"
    else:
        state = "ambiguous"
    return {"state": state, "branch_ids": sorted(relevant), "account_ids": sorted(account_ids)}


def _time(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Research timestamps must include a UTC offset")
    return result.astimezone(timezone.utc)


def _event_time(item: Mapping) -> datetime:
    value = item.get("collected_at") or item.get("captured_at") or item.get("completed_at") or item.get("created_at")
    return _time(value) if value else datetime.min.replace(tzinfo=timezone.utc)


def _round_order(item: Mapping) -> tuple:
    # Persisted admission order remains stable if timestamps tie or the wall
    # clock changes. Neutral callers without it keep deterministic time order.
    sequence = item.get("admission_sequence", 0)
    return (sequence if type(sequence) is int else 0,
            _event_time({"created_at": item.get("admitted_at") or item.get("created_at")}),
            str(item["id"]))


def _accepted(item: Mapping) -> bool:
    return (item.get("accepted", item.get("status") in {"accepted", "succeeded"}) is True
            and not item.get("reviewed") and not item.get("flagged"))


def _result_id(item: Mapping):
    return item.get("runner_result_id", item.get("result_id", item.get("id")))


def _snapshot(round_: Mapping, kind: str) -> Mapping | None:
    for link in round_.get("runs", []):
        if link.get("kind", "").removesuffix("s") == kind.removesuffix("s"):
            result = link.get("result")
            if isinstance(result, Mapping) and "accepted_result_ids" in result:
                return result
    return None


def _snapshot_state(snapshot: Mapping | None, fallback: str | None) -> str | None:
    state = snapshot.get("status") if snapshot else fallback
    return {"planned": "queued", "needs_attention": "waiting_attention",
            "interrupted": "waiting_attention", "stopping": "running",
            "finalizing": "running"}.get(state, state)


def _cells(branch: Mapping, configuration: Mapping) -> list[dict]:
    treatments = set(branch.get("treatment_ids", []))
    return [dict(cell) for cell in configuration.get("cells", [])
            if cell.get("selected", True) and cell.get("treatment_id") in treatments
            and cell.get("area", cell.get("area_id")) == branch.get("area")]


def _samples(configuration: Mapping) -> list[tuple[str, str]]:
    return list(dict.fromkeys((str(item["provider"]), str(item["profile_id"]))
                             for item in configuration.get("required_samples", [])))


def _identification(branch: Mapping, context: Mapping, configuration: Mapping,
                    evidence: Sequence[Mapping], rounds: Sequence[Mapping], now: datetime) -> dict:
    identity = branch_identity_fingerprint(branch, context["accounts"][branch["account_id"]])
    matching = branch_matching_fingerprint(branch, context)
    direct = configuration.get("direct_fingerprints", {}).get(branch["branch_id"], branch.get("direct_fingerprint"))
    candidates = [item for item in evidence if item.get("kind") == "identification"
                  and item.get("branch_id") == branch["branch_id"]
                  and item.get("branch_identity_fingerprint", item.get("identity_fingerprint")) == identity
                  and item.get("branch_matching_fingerprint", item.get("matching_fingerprint")) == matching
                  and (not direct or item.get("direct_fingerprint") == direct)]
    # A new explicit refresh is observable even before it produces accepted
    # evidence. Failed or pending refreshes must not leave a previous denial in
    # the confirmed P1 queue; retain its date as history instead.
    direct_rounds = []
    for round_ in rounds:
        if round_.get("kind") not in {"initial", "identification"} or round_.get("state") == "skipped":
            continue
        for child in round_.get("manifest", {}).get("children", []):
            if child.get("kind") != "identification":
                continue
            targets = child.get("measurement", {}).get("client_research", {}).get("branches", [])
            if any(target.get("branch_id") == branch["branch_id"]
                   and (not direct or target.get("direct_fingerprint") == direct) for target in targets):
                direct_rounds.append(round_)
                break
    newest_round = max(direct_rounds, key=_round_order) if direct_rounds else None
    latest = max(candidates, key=lambda item: (_event_time(item), str(item.get("id", "")))) if candidates else None
    snapshot = _snapshot(newest_round, "identification") if newest_round else None
    current = [item for item in candidates if newest_round and item.get("round_id") == newest_round["id"]
               and (snapshot is None or _result_id(item) in snapshot["accepted_result_ids"])]
    if newest_round and not current:
        previous = {"evidence_ids": [latest.get("id", latest.get("result_id"))],
                    "collected_at": latest.get("collected_at", latest.get("captured_at")),
                    "state": latest.get("decision", latest.get("judgment", {}).get("decision"))} if latest else None
        active = _snapshot_state(snapshot, newest_round.get("state")) in _ACTIVE
        return {"state": "checking_identification" if active else "needs_review", "checking_now": active,
                "evidence_ids": [], "round_id": newest_round["id"], "previous_assessment": previous}
    if current:
        latest = max(current, key=lambda item: (_event_time(item), str(item.get("id", ""))))
    if latest is None:
        old = any(item.get("kind") == "identification" and item.get("branch_id") == branch["branch_id"] for item in evidence)
        return {"state": "needs_refresh" if old else "awaiting_identification", "evidence_ids": []}
    base = {"evidence_ids": [latest.get("id", latest.get("result_id"))],
            "collected_at": latest.get("collected_at", latest.get("captured_at"))}
    if not _accepted(latest):
        return {**base, "state": "needs_review"}
    if not timedelta(0) <= now - _event_time(latest) <= timedelta(days=configuration.get("direct_freshness_days", 30)):
        return {**base, "state": "needs_refresh"}
    decision = latest.get("decision", latest.get("judgment", {}).get("decision"))
    return {**base, "state": decision if decision in {"identified", "could_not_identify"} else "needs_review"}


def _round_view(round_: Mapping, branch: Mapping, context: Mapping, configuration: Mapping,
                evidence: Sequence[Mapping], cells: Sequence[Mapping], now: datetime) -> dict:
    manifest = round_.get("manifest", {})
    frozen_directory = manifest.get("directory")
    frozen_config = manifest.get("configuration", {})
    snapshot = _snapshot(round_, "recommendations")
    required = {(cell["cell_id"], provider, profile) for cell in cells for provider, profile in _samples(configuration)}
    view = {"round_id": round_["id"], "state": _snapshot_state(snapshot, round_.get("state")), "required_count": len(required),
            "accepted_count": 0, "provider_counts": {}, "positive": [], "ambiguous": False,
            "complete": False, "current": False, "evidence_ids": [], "collected_at": None,
            "sample_signature": None, "fresh": False, "has_recommendation": False, "scope_current": False}
    if not frozen_directory:
        return view
    frozen = build_matching_context(frozen_directory)
    branch_id = branch["branch_id"]
    if branch_id not in frozen["branches"]:
        return view
    if branch_matching_fingerprint(branch, context) != branch_matching_fingerprint(branch_id, frozen):
        return view
    # A same-name branch with newly selected treatments cannot inherit an old
    # cell's absence: compare required cell/query/sample scope as well.
    frozen_cells = {cell["cell_id"]: cell for cell in _cells(frozen["branches"][branch_id], frozen_config)}
    eligible_cells = {cell["cell_id"] for cell in cells if cell["cell_id"] in frozen_cells
                      and cell.get("query_fingerprint") == frozen_cells[cell["cell_id"]].get("query_fingerprint")}
    if _samples(configuration) != _samples(frozen_config):
        return view
    view["current"] = True
    view["scope_current"] = len(eligible_cells) == len(cells)
    view["sample_signature"] = canonical_fingerprint({"samples": sorted(required),
        "queries": {cell["cell_id"]: cell.get("query_fingerprint") for cell in cells},
        "matching": branch_matching_fingerprint(branch, context)})
    current_cells = {cell["cell_id"]: cell for cell in cells}
    observations: dict[tuple[str, str, str], Mapping] = {}
    for item in evidence:
        if item.get("kind") not in {"recommendation", "recommendations"} or item.get("round_id") != round_["id"]:
            continue
        key = (item.get("cell_id"), item.get("provider"), item.get("profile_id"))
        if key not in required or key[0] not in eligible_cells or item.get("query_fingerprint") != current_cells[key[0]].get("query_fingerprint"):
            continue
        # Affirmatives remain evidence with their original capture dates even
        # when an explicit Refetch replaces or fails this sample. Current
        # negative coverage comes exclusively from the current result snapshot.
        if _accepted(item) and item.get("extraction", {}).get("response_status") == "answered":
            age = now - _event_time(item)
            if timedelta(0) <= age <= timedelta(hours=configuration.get("recommendation_freshness_hours", 144)):
                for entry in item["extraction"].get("recommendations", []):
                    resolution = resolve_recommendation_name(entry["name"], frozen)
                    if resolution["state"] == "matched" and branch_id in resolution["branch_ids"]:
                        view["positive"].append({"evidence_id": item.get("id", item.get("result_id")),
                            "cell_id": key[0], "name": entry["name"], "quote": entry["quote"],
                            "collected_at": item.get("collected_at", item.get("captured_at"))})
        if snapshot is not None and _result_id(item) not in snapshot["accepted_result_ids"]:
            continue
        previous = observations.get(key)
        if previous is None or (_event_time(item), str(item.get("id", ""))) > (_event_time(previous), str(previous.get("id", ""))):
            observations[key] = item
    accepted_times = []
    for (cell_id, provider, _profile), item in observations.items():
        if not _accepted(item):
            continue
        extraction = item.get("extraction", {})
        if extraction.get("response_status") != "answered":
            continue
        view["accepted_count"] += 1
        view["provider_counts"][provider] = view["provider_counts"].get(provider, 0) + 1
        item_id = item.get("id", item.get("result_id"))
        view["evidence_ids"].append(item_id)
        accepted_times.append(_event_time(item))
        for entry in extraction.get("recommendations", []):
            resolution = resolve_recommendation_name(entry["name"], frozen)
            if branch_id not in resolution["branch_ids"]:
                continue
            if resolution["state"] == "matched":
                view["has_recommendation"] = True
            else:
                view["ambiguous"] = True
        for entry in extraction.get("other_mentions", []):
            resolution = resolve_recommendation_name(entry["name"], frozen)
            if branch_id in resolution["branch_ids"] and (entry.get("kind") == "ambiguous" or resolution["state"] != "matched"):
                view["ambiguous"] = True
    view["complete"] = bool(required) and view["accepted_count"] == len(required) and not view["ambiguous"]
    if accepted_times:
        # All samples must still be fresh; the earliest capture is the safe
        # coverage timestamp, while each positive retains its own capture date.
        earliest = min(accepted_times)
        view["collected_at"] = earliest.isoformat()
        limit = timedelta(hours=configuration.get("recommendation_freshness_hours", 144))
        view["fresh"] = all(timedelta(0) <= now - capture <= limit for capture in accepted_times)
    return view


def _recommendation(branch: Mapping, context: Mapping, configuration: Mapping,
                    evidence: Sequence[Mapping], rounds: Sequence[Mapping], now: datetime) -> dict:
    cells = _cells(branch, configuration)
    base = {"state": "awaiting_checks", "accepted_count": 0,
            "required_count": len(cells) * len(_samples(configuration)), "provider_counts": {},
            "evidence_ids": [], "checking_now": False, "latest_check_incomplete": False,
            "repeated_gap": False, "cell_ids": [cell["cell_id"] for cell in cells]}
    if not cells:
        return {**base, "state": "outside_selected_scope"}
    relevant = [item for item in rounds if item.get("kind") in {"initial", "recommendations", "recommendation"}
                and item.get("state") != "skipped"
                and branch["branch_id"] in {entry["branch_id"] for entry in
                    _records(item.get("manifest", {}).get("directory", {}), "branches")}]
    relevant.sort(key=_round_order, reverse=True)
    if not relevant:
        return base
    views = [_round_view(item, branch, context, configuration, evidence, cells, now) for item in relevant]
    latest = views[0]
    checking = latest["state"] in _ACTIVE
    base.update({key: latest[key] for key in ("accepted_count", "required_count", "provider_counts", "evidence_ids")})
    base["checking_now"] = checking
    base["latest_check_incomplete"] = latest["state"] in _TERMINAL and not latest["complete"]
    positives = [entry for view in views if view["current"] for entry in view["positive"]]
    if positives:
        chosen = max(positives, key=_event_time)
        return {**base, "state": "recommended", "positive": chosen,
                "collected_at": chosen["collected_at"], "evidence_ids": list(dict.fromkeys(
                    [entry["evidence_id"] for entry in positives]))}
    selected = latest
    if checking:
        previous = next((view for view in views[1:] if view["state"] in _TERMINAL), None)
        if previous and previous["current"] and previous["complete"] and previous["fresh"]:
            selected = previous
            base.update({key: selected[key] for key in ("accepted_count", "required_count", "provider_counts", "evidence_ids")})
        else:
            return base
    if not selected["current"] or not selected["scope_current"]:
        return {**base, "state": "needs_refresh"}
    if not selected["complete"]:
        return {**base, "state": "needs_review" if selected["state"] in _TERMINAL or selected["ambiguous"] else "awaiting_checks"}
    if not selected["fresh"]:
        return {**base, "state": "needs_refresh", "collected_at": selected["collected_at"]}
    index = views.index(selected)
    previous = views[index + 1] if index + 1 < len(views) else None
    repeated = bool(previous and previous["state"] in _TERMINAL and previous["complete"]
                    and previous["sample_signature"] == selected["sample_signature"] and not previous["has_recommendation"])
    return {**base, "state": "not_recommended", "collected_at": selected["collected_at"], "repeated_gap": repeated}


def project_client_research(directory: Mapping | object, configuration: Mapping,
                            evidence: Sequence[Mapping], rounds: Sequence[Mapping],
                            now: datetime | str) -> dict[str, Any]:
    """Project branch signals and account priority without mutating evidence."""
    now = _time(now)
    context = build_matching_context(directory)
    projected = []
    for branch in context["branches"].values():
        direct = _identification(branch, context, configuration, evidence, rounds, now)
        recommendation = _recommendation(branch, context, configuration, evidence, rounds, now)
        priority = None
        state = recommendation["state"]
        if state == "recommended":
            priority, state = "P3", "recommended"
        elif state == "not_recommended":
            if direct["state"] == "could_not_identify":
                priority, state = "P1", "could_not_identify"
            elif direct["state"] == "identified":
                priority, state = "P2", "identified_not_recommended"
            else:
                state = direct["state"]
        reasons = {"P1": "AI could not identify this branch; complete checks contain no recommendation.",
                   "P2": "AI identified this branch; complete checks contain no recommendation.",
                   "P3": "This branch appears in a current affirmative recommendation."}
        projected.append({"branch_id": branch["branch_id"], "account_id": branch["account_id"],
            "priority": priority, "state": state, "reason": reasons.get(priority, state.replace("_", " ")),
            "identification": direct, "recommendation": recommendation,
            "potential_p1": direct["state"] == "could_not_identify" and state == "awaiting_checks",
            "repeated_gap": recommendation["repeated_gap"]})
    account_views = []
    priorities = {"P1": 0, "P2": 1, "P3": 2}
    state_order = {"needs_review": 0, "needs_refresh": 1, "awaiting_identification": 2,
                   "checking_identification": 3, "awaiting_checks": 4, "outside_selected_scope": 5}
    for account_id, account in context["accounts"].items():
        children = [item for item in projected if item["account_id"] == account_id]
        confirmed = [item for item in children if item["priority"]]
        chosen = min(confirmed, key=lambda item: (priorities[item["priority"]], item["branch_id"])) if confirmed else (
            min(children, key=lambda item: (state_order.get(item["state"], 99), item["branch_id"])) if children else None)
        account_views.append({"account_id": account_id, "account_name": account["account_name"],
            "priority": chosen["priority"] if chosen else None,
            "state": chosen["state"] if chosen else "outside_selected_scope",
            "reason": chosen["reason"] if chosen else "No active branches",
            "reason_branch_id": chosen["branch_id"] if chosen else None,
            "mixed_visibility": any(item["priority"] == "P3" for item in children)
                                and any(item["priority"] in {"P1", "P2"} for item in children),
            "branch_count": len(children)})
    account_views.sort(key=lambda item: (priorities.get(item["priority"], 3),
        normalize_identity(item["account_name"]), item["account_id"]))
    return {"policy_version": ASSESSMENT_POLICY_VERSION, "evaluated_at": now.isoformat(),
            "branches": projected, "accounts": account_views}
