"""Validate public client-research evidence without browser or model access.

Callers obtain full source text through the runner's hash-verifying artifact
reader, then supply this neutral envelope with its frozen plan correlation.
Model JSON is data: closed schemas, exact source quotes and supplied reference
anchors are checked before results enter the account priority projection.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit


IDENTIFICATION_SCHEMA = "client_search.identification_judgment.v1"
RECOMMENDATION_SCHEMA = "client_search.recommendation_extraction.v1"
_IDENTIFICATION_REASONS = {
    "distinguishing_identity_matches", "explicit_non_identification", "prompt_echo_only",
    "account_only", "shared_host_only", "conflicting_identity", "capability_limited", "insufficient_evidence",
}
_SUPPORTS = {"website", "address", "explicit_non_identification", "contradiction", "insufficient_identity"}


class ClientResearchEvidenceError(ValueError):
    """Unusable, ungrounded or mismatched evidence; never a clinic absence."""


def _closed(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ClientResearchEvidenceError(f"{label} must contain exactly {', '.join(sorted(fields))}")
    return dict(value)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ClientResearchEvidenceError(f"{label} must be a nonempty string")
    return value


def _choice(value: object, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ClientResearchEvidenceError(f"Unsupported {label}")
    return value


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ClientResearchEvidenceError(f"{label} must be an array")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ClientResearchEvidenceError(f"Repeated JSON field: {key}")
        value[key] = item
    return value


def _object(value: Mapping | str) -> Mapping:
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_no_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ClientResearchEvidenceError("Response is not one JSON object") from exc
    if not isinstance(value, Mapping):
        raise ClientResearchEvidenceError("Response must be one JSON object")
    return value


def _quote(value: object, answer: str, label: str) -> str:
    quote = _text(value, label)
    if quote not in answer:
        raise ClientResearchEvidenceError(f"{label} is not an exact source-answer quote")
    return quote


def validate_identification_judgment(value: Mapping | str, source_answer: str) -> dict:
    source_answer = _text(source_answer, "source_answer")
    result = _closed(_object(value), {"schema_version", "decision", "entity_scope", "reason_code", "evidence", "explanation"}, "judgment")
    if result["schema_version"] != IDENTIFICATION_SCHEMA:
        raise ClientResearchEvidenceError("Unsupported identification schema")
    decision = _choice(result["decision"], {"identified", "could_not_identify", "unclear"}, "decision")
    scope = _choice(result["entity_scope"], {"branch", "account", "none", "unclear"}, "entity_scope")
    reason = _choice(result["reason_code"], _IDENTIFICATION_REASONS, "reason_code")
    _text(result["explanation"], "explanation", empty=True)
    evidence = _list(result["evidence"], "evidence")
    if not evidence:
        raise ClientResearchEvidenceError("A judgment requires source-grounded evidence")
    for item in evidence:
        entry = _closed(item, {"quote", "supports"}, "judgment evidence")
        _quote(entry["quote"], source_answer, "evidence quote")
        _choice(entry["supports"], _SUPPORTS, "evidence supports")
    if decision == "identified" and (scope != "branch" or reason != "distinguishing_identity_matches"
            or not any(item["supports"] in {"website", "address"} for item in evidence)):
        raise ClientResearchEvidenceError("Identification requires branch-specific identity evidence")
    if decision == "could_not_identify" and (scope not in {"none", "account"}
            or reason != "explicit_non_identification"
            or not any(item["supports"] == "explicit_non_identification" for item in evidence)):
        raise ClientResearchEvidenceError("Non-identification requires an explicit branch denial")
    if decision == "unclear" and reason in {"distinguishing_identity_matches", "explicit_non_identification"}:
        raise ClientResearchEvidenceError("Unclear judgment requires an uncertainty reason")
    return result


def validate_recommendation_extraction(value: Mapping | str, source_answer: str) -> dict:
    source_answer = _text(source_answer, "source_answer")
    result = _closed(_object(value), {"schema_version", "response_status", "recommendations", "other_mentions", "inconclusive_evidence"}, "extraction")
    if result["schema_version"] != RECOMMENDATION_SCHEMA:
        raise ClientResearchEvidenceError("Unsupported recommendation schema")
    status = _choice(result["response_status"], {"answered", "inconclusive"}, "response_status")
    for field in ("recommendations", "other_mentions", "inconclusive_evidence"):
        _list(result[field], field)
    for field in ("recommendations", "other_mentions"):
        for item in result[field]:
            entry = _closed(item, {"name", "quote"} | ({"kind"} if field == "other_mentions" else set()), field)
            name = _text(entry["name"], "clinic name")
            quote = _quote(entry["quote"], source_answer, "entry quote")
            if name not in quote:
                raise ClientResearchEvidenceError("The clinic name must appear in its grounded entry")
            if field == "other_mentions":
                _choice(entry["kind"], {"not_recommended", "ambiguous"}, "mention kind")
    for item in result["inconclusive_evidence"]:
        entry = _closed(item, {"quote", "reason_code"}, "inconclusive evidence")
        _quote(entry["quote"], source_answer, "inconclusive quote")
        _choice(entry["reason_code"], {"capability_limited", "question_not_answered", "insufficient_response"}, "inconclusive reason")
    if status == "answered" and result["inconclusive_evidence"]:
        raise ClientResearchEvidenceError("An answered response cannot contain inconclusive evidence")
    if status == "inconclusive" and (not result["inconclusive_evidence"] or result["recommendations"]):
        raise ClientResearchEvidenceError("Inconclusive responses require grounded reasons and no recommendations")
    return result


def _normal(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _host(value: str) -> str:
    return (urlsplit(value).hostname or "").casefold().removeprefix("www.")


def _has_host(text: str, host: str) -> bool:
    return bool(host and re.search(r"(?<![\w.-])(?:www\.)?" + re.escape(host) + r"(?![\w.-])", text.casefold()))


def _shared_host(host: str, context: Mapping, reference: Mapping) -> bool:
    if context.get("website_host_shared") is True:
        return True
    if host in {_host(str(item)) if "://" in str(item) else str(item).casefold().removeprefix("www.")
                for item in context.get("shared_hosts", [])}:
        return True
    for key in ("host_index", "shared_website_hosts", "website_host_candidates"):
        index = context.get(key, {})
        if isinstance(index, Mapping):
            if any(str(candidate).casefold().removeprefix("www.") == host and len(members) > 1
                   for candidate, members in index.items()):
                return True
    # Complete frozen directory context is accepted without inferring ownership.
    branches = context.get("branches", {})
    if isinstance(branches, Mapping):
        branches = branches.values()
    return sum(_host(str(item.get("website", ""))) == host for item in branches) > 1


def _direct_decision(judgment: Mapping, answer: str, prompt: str,
                     reference: Mapping, context: Mapping) -> tuple[str, str | None]:
    """Keep the model judgment immutable; record a separate acceptance decision."""
    address = _normal(str(reference.get("address", "")))
    answer_normal, prompt_normal = _normal(answer), _normal(prompt)
    address_match = bool(address and re.search(r"(?<!\w)" + re.escape(address) + r"(?!\w)", answer_normal)
                         and address not in prompt_normal)
    host = _host(str(reference.get("website", "")))
    host_match = _has_host(answer, host) and not _has_host(prompt, host)
    branch_anchor = address_match or (host_match and not _shared_host(host, context, reference))
    if judgment["decision"] == "identified":
        grounded_anchor = any(
            (item["supports"] == "address" and address and address in _normal(item["quote"]) and address_match)
            or (item["supports"] == "website" and _has_host(item["quote"], host)
                and host_match and not _shared_host(host, context, reference))
            for item in judgment["evidence"])
        if not grounded_anchor:
            return "unclear", "shared_host_only" if host_match else "insufficient_evidence"
    if judgment["decision"] == "could_not_identify" and branch_anchor:
        return "unclear", "conflicting_identity"
    return str(judgment["decision"]), None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def adapt_client_research_result(result: Mapping, expected: Mapping | None = None) -> dict:
    """Validate one hash-verified neutral runner result against frozen inputs.

    Required common fields: result_id, run_id, workspace_id, directory_revision,
    config_revision, kind, source_prompt, source_answer and captured_at (or
    collected_at). Direct adds branch_id, identity/matching/direct fingerprints,
    source/judge provider/profile, reference_identity, ambiguity_context and
    judgment. Generic adds cell_id, query fingerprint, provider/profile and
    extraction. Optional source/request SHA-256 fields are checked when present.
    ``expected`` contains exact public-plan correlation fields supplied by the
    host; mismatch is an error, never a corrected or inferred value.
    """
    if not isinstance(result, Mapping):
        raise ClientResearchEvidenceError("Result must be a mapping")
    result = dict(result)
    for key in ("result_id", "run_id", "workspace_id", "directory_revision", "config_revision"):
        _text(result.get(key), key)
    kind = _choice(result.get("kind"), {"identification", "recommendation", "recommendations"}, "result kind")
    if result.get("status") not in {"accepted", "succeeded"} and result.get("accepted") is not True:
        raise ClientResearchEvidenceError("Only accepted runner observations can be adapted")
    prompt = _text(result.get("source_prompt"), "source_prompt")
    answer = _text(result.get("source_answer"), "source_answer")
    timestamp = _text(result.get("collected_at", result.get("captured_at")), "collected_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Missing UTC offset")
    except ValueError as exc:
        raise ClientResearchEvidenceError("collected_at must be an offset-aware timestamp") from exc
    for key, text in (("source_prompt_sha256", prompt), ("source_answer_sha256", answer)):
        if key in result and result[key] != _hash(text):
            raise ClientResearchEvidenceError(f"Mismatched {key}")
    if "judge_request_sha256" in result:
        request = _text(result.get("judge_request"), "judge_request")
        if result["judge_request_sha256"] != _hash(request):
            raise ClientResearchEvidenceError("Mismatched judge_request_sha256")
    for key, value in (expected or {}).items():
        if result.get(key) != value:
            raise ClientResearchEvidenceError(f"Result does not match frozen {key}")
    if kind == "identification":
        for key in ("branch_id", "source_provider", "judge_provider", "source_profile_id", "judge_profile_id",
                    "direct_fingerprint"):
            _text(result.get(key), key)
        for canonical, alternate in (("branch_identity_fingerprint", "identity_fingerprint"),
                                     ("branch_matching_fingerprint", "matching_fingerprint")):
            result[canonical] = _text(result.get(canonical, result.get(alternate)), canonical)
        if result["source_provider"] == result["judge_provider"]:
            raise ClientResearchEvidenceError("Source and judgment require different chatbots")
        reference = result.get("reference_identity")
        context = result.get("ambiguity_context")
        if not isinstance(reference, Mapping) or not isinstance(context, Mapping):
            raise ClientResearchEvidenceError("Direct evidence requires frozen identity and ambiguity context")
        judgment = validate_identification_judgment(result.get("judgment"), answer)
        decision, issue = _direct_decision(judgment, answer, prompt, reference, context)
        result.update({"judgment": judgment, "decision": decision, "acceptance_reason": issue})
    else:
        for key in ("cell_id", "query_fingerprint", "provider", "profile_id"):
            _text(result.get(key), key)
        result["extraction"] = validate_recommendation_extraction(result.get("extraction"), answer)
        result["kind"] = "recommendation"
    result.update({"accepted": True, "status": "accepted", "collected_at": timestamp})
    return result
