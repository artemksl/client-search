"""Area-only rendering primitives for new Market Evidence demand.

Market query geography has exactly three levels: local area, greater area, and
city.  Frozen Snapshot v4 templates can be consumed as source material, but
their historical station/postcode placeholders are deterministically rewritten
to the exact approved local-area binding.  Historical Snapshot reconstruction
lives in ``workflows.free_audit.prompt_rendering_v4`` and is incompatible with
this renderer contract.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from string import Formatter
from typing import Any, Mapping

from clinic_geo.integrity import canonical_sha256


class PromptRenderingError(ValueError):
    """A frozen slot cannot be rendered from the approved area-only bindings."""


MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION = "prompt-rendering.market-area.v1"
_GEOGRAPHY_LEVELS = frozenset({"local", "greater_area", "city"})
_FORBIDDEN_QUERY_FIELDS = frozenset(
    {
        "station",
        "nearest_station",
        "postcode",
        "postcode_district",
        "landmark",
        "variants",
    }
)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptRenderingError(f"{field_name} must be non-empty text")
    return value


def _area_only_template(source_template: str) -> str:
    template = source_template.replace("{station_or_area}", "{local_area}")
    template = template.replace("{postcode_or_area}", "{local_area}")
    forbidden = {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name in _FORBIDDEN_QUERY_FIELDS
    }
    if forbidden:
        raise PromptRenderingError(
            f"market template contains forbidden query-geography bindings: {sorted(forbidden)}"
        )
    return template


def render_market_area_slot(
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
) -> dict[str, Any]:
    """Render one market slot against exactly one of the three geography levels."""

    rendering_map = slot.get("renderings")
    if not isinstance(rendering_map, Mapping) or platform not in rendering_map:
        raise PromptRenderingError(f"slot has no frozen rendering for {platform}")
    rendering = rendering_map[platform]
    if not isinstance(rendering, Mapping):
        raise PromptRenderingError(f"invalid frozen rendering for {platform}")

    declared_subject_kind = str(slot.get("subject_kind") or "")
    if subject_kind not in {"treatment", "clinic_archetype"}:
        raise PromptRenderingError("subject_kind is not a supported market subject kind")
    if declared_subject_kind != subject_kind:
        raise PromptRenderingError("slot subject_kind does not match the supplied subject")
    _require_text(subject.get("id"), "subject.id")
    if subject_kind == "treatment":
        _require_text(treatment_name, "treatment_name")
    elif treatment_name not in {None, ""}:
        raise PromptRenderingError(
            "clinic-archetype slots cannot carry a treatment_name"
        )

    geography_level = str(slot.get("geography_level") or "")
    if geography_level not in _GEOGRAPHY_LEVELS:
        raise PromptRenderingError(
            "slot geography_level must be local, greater_area, or city"
        )
    target = deepcopy(dict(target_geography))
    forbidden_target_fields = sorted(_FORBIDDEN_QUERY_FIELDS.intersection(target))
    if forbidden_target_fields:
        raise PromptRenderingError(
            "market target geography cannot carry station, postcode, landmark, "
            f"or variant data: {forbidden_target_fields}"
        )
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
        }[geography_level],
        geography_level,
    )
    if target_name != expected_target_name:
        raise PromptRenderingError(
            "target geography name does not match the exact approved geography"
        )

    source_template = _require_text(rendering.get("text"), "rendering.text")
    template = _area_only_template(source_template)
    bindings = {
        "treatment": treatment_name or "",
        "local_area": local_area,
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
        raise PromptRenderingError("market template has an unknown binding") from exc
    for name, value in exact_bindings.items():
        _require_text(value, f"rendering binding {name}")
    try:
        text = template.format(**bindings)
    except (KeyError, ValueError) as exc:
        raise PromptRenderingError("market template could not be rendered") from exc

    text_hash = _text_sha256(text)
    semantic_payload = {
        "contract": "market_evidence.semantic_prompt.area_only.v1",
        "renderer_contract_version": MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION,
        "semantic_slot_id": slot["id"],
        "subject_kind": subject_kind,
        "subject_id": subject["id"],
        "target_geography": target,
    }
    semantic_prompt_id = (
        f"market-semantic-{str(slot['id']).casefold()}-"
        f"{canonical_sha256(semantic_payload)[:20]}"
    )
    prompt_identity = {
        **semantic_payload,
        "platform": platform,
        "prompt_text_sha256": text_hash,
    }
    prompt_id = (
        f"market-prompt-{platform}-{str(slot['id']).casefold()}-"
        f"{canonical_sha256(prompt_identity)[:20]}"
    )
    collection_payload = {
        "contract": "market_evidence.collection_scope.area_only.v1",
        "renderer_contract_version": MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION,
        "basket_id": slot["basket_id"],
        "semantic_slot_id": slot["id"],
        "platform": platform,
        "subject_kind": subject_kind,
        "subject_id": subject["id"],
        "target_geography": target,
        "prompt_text_sha256": text_hash,
    }
    collection_scope_id = (
        f"market-collection-scope-{canonical_sha256(collection_payload)[:24]}"
    )

    return {
        "schema_version": MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION,
        "platform": platform,
        "semantic_slot_id": slot["id"],
        "geography_level": geography_level,
        "source_template": source_template,
        "source_template_sha256": _text_sha256(source_template),
        "template": template,
        "template_sha256": _text_sha256(template),
        "rendering_bindings": exact_bindings,
        "rendering_bindings_sha256": canonical_sha256(exact_bindings),
        "text": text,
        "prompt_text_sha256": text_hash,
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
    "MARKET_AREA_SLOT_RENDERER_CONTRACT_VERSION",
    "PromptRenderingError",
    "render_market_area_slot",
]
