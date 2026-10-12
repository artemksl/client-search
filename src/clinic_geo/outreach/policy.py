"""Load and fingerprint the active founder-outreach operating policy."""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from functools import lru_cache
import re


CURRENT_OUTREACH_POLICY_VERSION = "founder-outreach-policy-v1"
POLICY_RESOURCE_NAME = f"{CURRENT_OUTREACH_POLICY_VERSION}.md"


@lru_cache(maxsize=1)
def current_outreach_policy_text() -> str:
    return (
        resources.files("clinic_geo.outreach.policies")
        .joinpath(POLICY_RESOURCE_NAME)
        .read_text(encoding="utf-8")
    )


@lru_cache(maxsize=1)
def current_outreach_policy_sha256() -> str:
    return hashlib.sha256(current_outreach_policy_text().encode("utf-8")).hexdigest()


ROUTING_MESSAGE_POLICY_VERSION = "buyer-identification-only-v1"


def routing_message_errors(body: str, *, subject: str | None = None) -> list[str]:
    """Return violations for the deliberately narrow routing-message allowlist."""
    text = " ".join(str(body or "").split())
    lowered = text.lower()
    errors: list[str] = []
    neutral_subjects = {
        "",
        "marketing contact",
        "who handles marketing?",
        "contact for marketing",
        "quick question for reception",
    }
    if str(subject or "").strip().lower() not in neutral_subjects:
        errors.append("routing subject must be empty or use an approved neutral template")
    if len(text) > 600:
        errors.append("routing messages must be 600 characters or fewer")
    if re.search(r"https?://|www\.", lowered):
        errors.append("routing messages must not contain links")
    canonical = re.sub(r"[?.!]+$", "", lowered).strip()
    allowed = {
        "who is responsible for the clinic's marketing decisions",
        "who is responsible for marketing at the clinic",
        "who handles marketing for the clinic",
        "could you tell me who is responsible for marketing at the clinic",
        "could you put me in touch with the person responsible for marketing",
        "could you connect me with the person responsible for marketing",
        "could you connect me with the person responsible for online visibility and marketing",
        "please put me in touch with the person responsible for marketing",
        "could you connect me with the right person for marketing",
    }
    if canonical not in allowed:
        errors.append("routing messages must use an approved buyer-identification-only template")
    return errors


def message_content_sha256(
    *,
    account_id: str,
    person_id: str | None,
    contact_point_id: str | None,
    visibility_check_id: str | None,
    subject: str | None,
    body: str,
    channel: str,
    outreach_purpose: str,
    branch_id: str | None = None,
    treatment_slug: str | None = None,
) -> str:
    canonical_payload = {
        "account_id": str(account_id),
        "person_id": str(person_id or ""),
        "contact_point_id": str(contact_point_id or ""),
        "visibility_check_id": str(visibility_check_id or ""),
        "subject": str(subject or ""),
        "body": str(body),
        "channel": str(channel),
        "outreach_purpose": str(outreach_purpose),
    }
    if branch_id:
        canonical_payload["branch_id"] = str(branch_id)
        canonical_payload["treatment_slug"] = str(treatment_slug or "")
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
