"""Historical Snapshot v4 prompt rendering compatibility.

Snapshot v4 allowed local station/postcode substitutions.  Stored v4 artifacts
pin that behaviour, so it remains available only in the Snapshot package for
reconstruction and verification.  New market-evidence planning must not import
this module or reinterpret its observations as area-only evidence.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from string import Formatter
from typing import Any, Mapping

from clinic_geo.integrity import canonical_sha256


class PromptRenderingError(ValueError):
    """A frozen Snapshot v4 slot cannot be rendered from its exact bindings."""


V4_SLOT_RENDERER_CONTRACT_VERSION = "prompt-rendering.v4-slot.v1"


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptRenderingError(f"{field_name} must be non-empty text")
    return value


def render_v4_slot(
    *,
    platform: str,
    slot: Mapping[str, Any],
    subject_kind: str,
    subject: Mapping[str, Any],
    target_geography: Mapping[str, Any],
    expected_target_geography_id: str,
    treatment_name: str | None,
    local_area: str,
    greater_area: str,
    city: str,
    archetype_singular: str,
    archetype_plural: str,
    variants: Mapping[str, str],
) -> dict[str, Any]:
    """Reproduce one frozen Snapshot v4 semantic slot exactly."""

    rendering_map = slot.get("renderings")
    if not isinstance(rendering_map, Mapping) or platform not in rendering_map:
        raise PromptRenderingError(f"slot has no frozen rendering for {platform}")
    rendering = rendering_map[platform]
    if not isinstance(rendering, Mapping):
        raise PromptRenderingError(f"invalid frozen rendering for {platform}")

    declared_subject_kind = str(slot.get("subject_kind") or "")
    if subject_kind not in {"treatment", "clinic_archetype"}:
        raise PromptRenderingError("subject_kind is not a frozen v4 subject kind")
    if declared_subject_kind != subject_kind:
        raise PromptRenderingError("slot subject_kind does not match the supplied subject")
    _require_text(subject.get("id"), "subject.id")
    if subject_kind == "treatment":
        _require_text(treatment_name, "treatment_name")
    elif treatment_name not in {None, ""}:
        raise PromptRenderingError(
            "clinic-archetype slots cannot carry a treatment_name"
        )

    geography_tier = str(slot["geography_level"])
    if geography_tier not in {"local", "greater_area", "city"}:
        raise PromptRenderingError("slot geography_level is not a frozen v4 level")
    target = deepcopy(dict(target_geography))
    target_id = _require_text(target.get("id"), "target_geography.id")
    if target_id != _require_text(
        expected_target_geography_id, "expected_target_geography_id"
    ):
        raise PromptRenderingError(
            "target geography ID does not match the expected query geography"
        )
    target_name = _require_text(target.get("name"), "target_geography.name")
    expected_target_name = _require_text(
        {
            "local": local_area,
            "greater_area": greater_area,
            "city": city,
        }[geography_tier],
        geography_tier,
    )
    if geography_tier != "local" and target_name != expected_target_name:
        raise PromptRenderingError(
            "target geography name does not match the slot geography binding"
        )
    template = str(rendering["text"])
    requested_variant = geography_tier
    actual_variant_type = geography_tier
    actual_variant_value = str(target["name"])
    used_fallback = False
    if "{station_or_area}" in template:
        requested_variant = "station"
        actual_variant_type = "station" if variants.get("station") else "area"
        actual_variant_value = variants.get("station") or local_area
        used_fallback = actual_variant_type != "station"
    elif "{postcode_or_area}" in template:
        requested_variant = "postcode"
        actual_variant_type = "postcode" if variants.get("postcode") else "area"
        actual_variant_value = variants.get("postcode") or local_area
        used_fallback = actual_variant_type != "postcode"

    bindings = {
        "treatment": treatment_name or "",
        "local_area": local_area,
        "station_or_area": variants.get("station") or local_area,
        "postcode_or_area": variants.get("postcode") or local_area,
        "greater_area": greater_area,
        "city": city,
        "clinic_archetype_singular": archetype_singular,
        "clinic_archetype_plural": archetype_plural,
    }
    binding_names = tuple(
        dict.fromkeys(
            field_name
            for _, field_name, _, _ in Formatter().parse(template)
            if field_name
        )
    )
    try:
        exact_bindings = {name: bindings[name] for name in binding_names}
    except KeyError as exc:
        raise PromptRenderingError("frozen v4 template has an unknown binding") from exc
    for name, value in exact_bindings.items():
        _require_text(value, f"rendering binding {name}")
    try:
        text = template.format(**bindings)
    except (KeyError, ValueError) as exc:
        raise PromptRenderingError("frozen v4 template could not be rendered") from exc

    text_hash = _text_sha256(text)
    semantic_payload = {
        "contract": "free_snapshot.semantic_prompt.v4",
        "semantic_slot_id": slot["id"],
        "subject_kind": subject_kind,
        "subject_id": subject["id"],
        "target_geography": target,
    }
    semantic_prompt_id = (
        f"semantic-{str(slot['id']).casefold()}-"
        f"{canonical_sha256(semantic_payload)[:20]}"
    )
    prompt_identity = {
        **semantic_payload,
        "platform": platform,
        "prompt_text_sha256": text_hash,
    }
    prompt_id = (
        f"prompt-v4-{platform}-{str(slot['id']).casefold()}-"
        f"{canonical_sha256(prompt_identity)[:20]}"
    )
    collection_payload = {
        "contract": "free_snapshot.collection_scope.v4",
        "basket_id": slot["basket_id"],
        "platform": platform,
        "subject_kind": subject_kind,
        "subject_id": subject["id"],
        "target_geography": target,
    }
    collection_scope_id = (
        f"collection-scope-{canonical_sha256(collection_payload)[:24]}"
    )

    return {
        "schema_version": V4_SLOT_RENDERER_CONTRACT_VERSION,
        "platform": platform,
        "semantic_slot_id": slot["id"],
        "geography_tier": geography_tier,
        "template": template,
        "template_sha256": _text_sha256(template),
        "rendering_bindings": exact_bindings,
        "rendering_bindings_sha256": canonical_sha256(exact_bindings),
        "text": text,
        "prompt_text_sha256": text_hash,
        "requested_variant_type": requested_variant,
        "actual_variant": {
            "type": actual_variant_type,
            "value": actual_variant_value,
            "used_fallback": used_fallback,
        },
        "semantic_payload": semantic_payload,
        "semantic_prompt_id": semantic_prompt_id,
        "prompt_identity": prompt_identity,
        "prompt_id": prompt_id,
        "collection_payload": collection_payload,
        "collection_scope_id": collection_scope_id,
        "naturalness_score": int(rendering["naturalness_score"]),
        "slot_naturalness_score": int(slot["naturalness_score"]),
    }


__all__ = [
    "PromptRenderingError",
    "V4_SLOT_RENDERER_CONTRACT_VERSION",
    "render_v4_slot",
]
