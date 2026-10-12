"""Crawler/control-token registry schema v2 (ADR-002 §6).

Fixes the seed's known weakness (``audit-automation/config/capability-registry.json``):
real fetching **crawlers** and robots-policy **control tokens** are mixed in one
``agents`` list there. Registry v2 keeps them in two SEPARATE collections that can
never be confused:

* ``crawlers`` — real fetching user agents (Googlebot, GPTBot, OAI-SearchBot,
  ChatGPT-User, CCBot …). They fetch bytes; robots.txt applies to them; they may
  publish an official identity-verification method and a published fetch-size
  limit.
* ``policy_control_tokens`` — robots-policy tokens that are NOT fetching agents
  (Google-Extended …). They control a *policy*, not a fetch.

Every fact carries its literal evidence ``{source_url, quote, retrieved_at}`` so
downstream verification can re-fetch the cited source and confirm the exact
``quote`` appears in it. Nothing here trusts a value without that evidence.

Scope is deliberately ONLY the facts consumed by audit gates (ADR-002 §6 / the
A04–A09 and A21 gate treatments): crawler and control-token names; provider and
purpose; active/deprecated status; robots.txt applicability; the
crawler-versus-policy-control distinction (structural — which collection); the
official identity-verification methods; and the published fetch-size limits
(nullable — a threshold is NEVER invented). No other fields are accepted.

This module is stdlib-only and imports nothing from the audit lanes, so schema
validation and hashing can be exercised in isolation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable


REGISTRY_SCHEMA_ID = "crawler_registry_v2"

# Closed field sets — anything else is an unknown field and is rejected.
TOP_LEVEL_FIELDS = frozenset({"registry_version", "crawlers", "policy_control_tokens"})
FACT_FIELDS = frozenset({"value", "source_url", "quote", "retrieved_at"})
CRAWLER_FIELDS = frozenset(
    {
        "name",
        "provider",
        "purpose",
        "status",
        "robots_txt_applicability",
        "identity_verification_methods",
        "fetch_size_limit",
    }
)
POLICY_TOKEN_FIELDS = frozenset({"name", "provider", "controls_what", "status"})
STATUS_VALUES = frozenset({"active", "deprecated"})


class SchemaError(ValueError):
    """A proposed registry violates the v2 schema. Verification treats this as a
    hard failure (``isChange`` = ``null``), never as "no change"."""


# --- fact + field validators ------------------------------------------------
def _require_mapping(obj: object, where: str) -> dict:
    if not isinstance(obj, dict):
        raise SchemaError(f"{where} must be a JSON object, got {type(obj).__name__}")
    return obj


def _nonblank_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{where} must be a non-blank string")
    return value


def _validate_fact(
    obj: object,
    where: str,
    *,
    value_validator: Callable[[object, str], Any],
) -> dict:
    """Validate one evidenced fact ``{value, source_url, quote, retrieved_at}``.

    The evidence triple must be present and non-blank; the ``quote`` is the
    literal string a verifier will require to appear in the fetched source.
    """

    fact = _require_mapping(obj, where)
    extra = set(fact) - FACT_FIELDS
    if extra:
        raise SchemaError(f"{where} has unknown field(s): {sorted(extra)}")
    missing = FACT_FIELDS - set(fact)
    if missing:
        raise SchemaError(f"{where} is missing field(s): {sorted(missing)}")
    value_validator(fact["value"], f"{where}.value")
    _nonblank_str(fact["source_url"], f"{where}.source_url")
    _nonblank_str(fact["quote"], f"{where}.quote")
    _nonblank_str(fact["retrieved_at"], f"{where}.retrieved_at")
    return fact


def _str_value(value: object, where: str) -> str:
    return _nonblank_str(value, where)


def _status_value(value: object, where: str) -> str:
    text = _nonblank_str(value, where)
    if text not in STATUS_VALUES:
        raise SchemaError(f"{where} must be one of {sorted(STATUS_VALUES)}, got {text!r}")
    return text


def _bool_value(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{where} must be a boolean")
    return value


def _positive_int_value(value: object, where: str) -> int:
    # bool is a subclass of int — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchemaError(f"{where} must be an integer >= 1 (bytes)")
    return value


# --- record validators ------------------------------------------------------
def _validate_crawler(obj: object, index: int) -> dict:
    where = f"crawlers[{index}]"
    record = _require_mapping(obj, where)
    extra = set(record) - CRAWLER_FIELDS
    if extra:
        # A crawler carrying a policy-token-only field (e.g. controls_what) lands
        # here: a mixed record is rejected outright.
        raise SchemaError(f"{where} has unknown/mixed field(s): {sorted(extra)}")
    missing = CRAWLER_FIELDS - set(record)
    if missing:
        raise SchemaError(f"{where} is missing field(s): {sorted(missing)}")

    _validate_fact(record["name"], f"{where}.name", value_validator=_str_value)
    _validate_fact(record["provider"], f"{where}.provider", value_validator=_str_value)
    _validate_fact(record["purpose"], f"{where}.purpose", value_validator=_str_value)
    _validate_fact(record["status"], f"{where}.status", value_validator=_status_value)
    _validate_fact(
        record["robots_txt_applicability"],
        f"{where}.robots_txt_applicability",
        value_validator=_bool_value,
    )

    methods = record["identity_verification_methods"]
    if not isinstance(methods, list):
        raise SchemaError(f"{where}.identity_verification_methods must be a list")
    for i, method in enumerate(methods):
        _validate_fact(
            method,
            f"{where}.identity_verification_methods[{i}]",
            value_validator=_str_value,
        )

    # Published fetch-size limit is nullable — a provider that publishes none has
    # ``null`` here. A NON-null limit MUST be a fully-evidenced fact; a numeric
    # limit without a cited source is an invented threshold and is rejected.
    limit = record["fetch_size_limit"]
    if limit is not None:
        _validate_fact(
            limit,
            f"{where}.fetch_size_limit",
            value_validator=_positive_int_value,
        )
    return record


def _validate_policy_token(obj: object, index: int) -> dict:
    where = f"policy_control_tokens[{index}]"
    record = _require_mapping(obj, where)
    extra = set(record) - POLICY_TOKEN_FIELDS
    if extra:
        # A control token carrying a crawler-only field (robots_txt_applicability,
        # fetch_size_limit, identity_verification_methods) is a mixed record.
        raise SchemaError(f"{where} has unknown/mixed field(s): {sorted(extra)}")
    missing = POLICY_TOKEN_FIELDS - set(record)
    if missing:
        raise SchemaError(f"{where} is missing field(s): {sorted(missing)}")
    _validate_fact(record["name"], f"{where}.name", value_validator=_str_value)
    _validate_fact(record["provider"], f"{where}.provider", value_validator=_str_value)
    _validate_fact(
        record["controls_what"], f"{where}.controls_what", value_validator=_str_value
    )
    _validate_fact(record["status"], f"{where}.status", value_validator=_status_value)
    return record


def validate_registry(obj: object) -> dict:
    """Validate a proposed registry against schema v2, returning it unchanged.

    Raises :class:`SchemaError` on any deviation: unknown top-level collection or
    field, a crawler/token record carrying the other collection's fields, a
    missing evidence triple, a bad status, or an invented (source-less) fetch
    limit.
    """

    registry = _require_mapping(obj, "registry")
    extra = set(registry) - TOP_LEVEL_FIELDS
    if extra:
        raise SchemaError(f"registry has unknown collection/field(s): {sorted(extra)}")
    missing = TOP_LEVEL_FIELDS - set(registry)
    if missing:
        raise SchemaError(f"registry is missing field(s): {sorted(missing)}")

    _nonblank_str(registry["registry_version"], "registry.registry_version")

    crawlers = registry["crawlers"]
    if not isinstance(crawlers, list):
        raise SchemaError("registry.crawlers must be a list")
    for i, crawler in enumerate(crawlers):
        _validate_crawler(crawler, i)

    tokens = registry["policy_control_tokens"]
    if not isinstance(tokens, list):
        raise SchemaError("registry.policy_control_tokens must be a list")
    for i, token in enumerate(tokens):
        _validate_policy_token(token, i)

    return registry


# --- canonical serialization + content hash ---------------------------------
def canonical_json(registry: object) -> str:
    """Deterministic canonical JSON: sorted keys, no insignificant whitespace.

    Two registries with identical content (including evidence) serialise
    identically regardless of key order, so the content hash is stable.
    """

    return json.dumps(
        registry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash(registry: object) -> str:
    """SHA-256 (hex) of the canonical serialization. This is the content address
    an approved version is stored under and the value each audit pins."""

    return hashlib.sha256(canonical_json(registry).encode("utf-8")).hexdigest()


__all__ = [
    "REGISTRY_SCHEMA_ID",
    "TOP_LEVEL_FIELDS",
    "FACT_FIELDS",
    "CRAWLER_FIELDS",
    "POLICY_TOKEN_FIELDS",
    "STATUS_VALUES",
    "SchemaError",
    "validate_registry",
    "canonical_json",
    "content_hash",
]
