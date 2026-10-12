"""Build the normalized data payload consumed by visibility snapshot renderers.

The module deliberately stops at JSON-compatible data.  HTML, PDF, assets, CLI
handling, and workflow integration live in the other reporting modules.  Keeping
the calculations here makes the report auditable and lets every renderer use the
same denominators and performance thresholds.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from clinic_geo.platforms import (
    CHATGPT,
    FULL_AUDIT_PLATFORMS,
    GOOGLE_AI_OVERVIEW,
    PLATFORM_LABELS,
    platform_data_dir,
)
from clinic_geo.parsing.clinic_names import normalize_clinic_name
from clinic_geo.parsing.treatments import (
    configured_treatments,
    prompt_treatment_tags,
    treatment_key,
)
from clinic_geo.stats.share_of_voice import aggressive_match_key


SCHEMA_VERSION = "1.0"
MARKET_STRATA = (
    "recommendation",
    "informational",
    "qualified_persona",
    "control_exploratory",
)
STRATUM_LABELS = {
    "recommendation": "Recommendation",
    "informational": "Informational",
    "qualified_persona": "Qualified persona",
    "control_exploratory": "Control / exploratory",
}
RATING_LABELS = {
    "good": "Good",
    "mediocre": "Mediocre",
    "poor": "Poor",
    "not_measured": "Not measured",
}
VALID_PROVIDER_STATUSES = {"ok"}
VALID_GOOGLE_STATUSES = VALID_PROVIDER_STATUSES | {"not_shown"}
VALID_CHATGPT_STATUSES = {"collected", "ok"}
TREATMENT_VISIBILITY_STRATA = {"recommendation", "qualified_persona"}


class ReportDataError(ValueError):
    """Raised when source data cannot safely identify or measure this client."""


def rate_competitive_visibility(
    client_rate: Optional[float],
    leader_rate: Optional[float],
    valid_outcomes: int,
) -> str:
    """Return the requested relative Good/Mediocre/Poor rating key.

    ``not_measured`` covers an absent denominator and the deliberately neutral
    case where neither the client nor any selected competitor appeared.
    """
    if valid_outcomes <= 0 or client_rate is None or leader_rate is None:
        return "not_measured"
    if client_rate == 0 and leader_rate == 0:
        return "not_measured"
    if leader_rate == 0:
        return "good"
    index = client_rate / leader_rate
    if index >= 0.80:
        return "good"
    if index >= 0.40:
        return "mediocre"
    return "poor"


def rate_head_to_head(
    client_first_rate: Optional[float],
    rival_first_rate: Optional[float],
    valid_outcomes: int,
) -> str:
    """Rate head-to-head results by first mention of either tested clinic."""
    if valid_outcomes <= 0 or client_first_rate is None or rival_first_rate is None:
        return "not_measured"
    if client_first_rate > rival_first_rate:
        return "good"
    if client_first_rate < rival_first_rate:
        return "poor"
    return "mediocre"


def select_top_competitors(
    appearance_counts: Mapping[str, int],
    *,
    excluded: Iterable[str] = (),
    limit: int = 3,
) -> list[str]:
    """Select rivals by pooled appearances, breaking ties alphabetically."""
    excluded_keys = {normalize_clinic_name(name) for name in excluded}
    candidates = [
        (str(name), int(count))
        for name, count in appearance_counts.items()
        if int(count) > 0 and normalize_clinic_name(name) not in excluded_keys
    ]
    candidates.sort(key=lambda item: (-item[1], item[0].casefold(), item[0]))
    return [name for name, _ in candidates[: max(0, int(limit))]]


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_report_id(inputs) -> str:
    """Return a compact report ID backed by a SHA-256 canonical-input hash."""
    digest = hashlib.sha256(_canonical_json(inputs).encode("utf-8")).hexdigest()
    return f"VS-{digest[:12].upper()}"


def _stable_sha256(inputs) -> str:
    return hashlib.sha256(_canonical_json(inputs).encode("utf-8")).hexdigest()


def _round_rate(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)


def _read_required_dict(path: Path, label: str) -> dict:
    if not path.is_file():
        raise ReportDataError(f"Missing required {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReportDataError(f"Malformed {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportDataError(f"Malformed {label}: expected a JSON object in {path}")
    return value


def _read_optional_dict(path: Path, label: str) -> Optional[dict]:
    if not path.is_file():
        return None
    return _read_required_dict(path, label)


def _clean_name(value) -> str:
    return str(value or "").strip()


def _base_prompt_id(prompt_id: str) -> str:
    return str(prompt_id).split("#", 1)[0]


def _warning(
    code: str,
    message: str,
    *,
    stage: Optional[str] = None,
    platform: Optional[str] = None,
    count: Optional[int] = None,
    examples: Optional[Sequence[str]] = None,
) -> dict:
    row = {"code": code, "message": message}
    if stage is not None:
        row["stage"] = stage
    if platform is not None:
        row["platform"] = platform
    if count is not None:
        row["count"] = count
    if examples:
        row["examples"] = list(examples)
    return row


class _DisjointNames:
    """Union matcher-provided raw/canonical spellings across all platforms."""

    def __init__(self):
        self.parent: dict[str, str] = {}
        self.canonical_frequency: Counter[str] = Counter()
        self.database_names: set[str] = set()

    def _add(self, name: str) -> None:
        name = _clean_name(name)
        if name and name not in self.parent:
            self.parent[name] = name

    def _find(self, name: str) -> str:
        parent = self.parent[name]
        if parent != name:
            self.parent[name] = self._find(parent)
        return self.parent[name]

    def _union(self, left: str, right: str) -> None:
        self._add(left)
        self._add(right)
        if not left or not right:
            return
        a, b = self._find(left), self._find(right)
        if a != b:
            # The root itself is not the display choice; a lexical root keeps the
            # graph deterministic regardless of input/platform ordering.
            if (a.casefold(), a) <= (b.casefold(), b):
                self.parent[b] = a
            else:
                self.parent[a] = b

    def ingest(self, matches: Optional[dict]) -> None:
        if not matches:
            return
        canonical = matches.get("canonical") or {}
        if not isinstance(canonical, dict):
            raise ReportDataError("Malformed name_matches.json: 'canonical' must be an object")
        for raw, info in canonical.items():
            if not isinstance(info, dict):
                raise ReportDataError("Malformed name_matches.json: canonical rows must be objects")
            raw_name = _clean_name(raw)
            canonical_name = _clean_name(info.get("canonical")) or raw_name
            self._union(raw_name, canonical_name)
            self.canonical_frequency[canonical_name] += 1
            db_name = _clean_name(info.get("db_name"))
            if info.get("in_database") and db_name:
                self._union(canonical_name, db_name)
                self.database_names.add(db_name)

    def _members(self, root: str) -> list[str]:
        return [name for name in self.parent if self._find(name) == root]

    def canonical(self, raw: str) -> str:
        raw = _clean_name(raw)
        if not raw or raw not in self.parent:
            return raw
        root = self._find(raw)
        members = self._members(root)
        db_candidates = [name for name in members if name in self.database_names]
        candidates = db_candidates or members
        candidates.sort(
            key=lambda name: (
                -self.canonical_frequency.get(name, 0),
                len(name),
                name.casefold(),
                name,
            )
        )
        return candidates[0]


@dataclass
class _PlatformFiles:
    stage: str
    platform: str
    root: Path
    extracted: Optional[dict]
    matches: Optional[dict]
    manifest: Optional[dict]


@dataclass(frozen=True)
class _Outcome:
    stage: str
    platform: str
    prompt_id: str
    base_prompt_id: str
    stratum: str
    sub_stratum: str
    scoring: str
    treatments: tuple[str, ...]
    names: tuple[str, ...]
    source_status: str


def _self_matcher(config: dict, canonicalizer: _DisjointNames):
    values = [_clean_name(config.get("clinic_name"))]
    aliases = config.get("clinic_aliases") or []
    if not isinstance(aliases, list):
        raise ReportDataError("Malformed free_audit_config.json: clinic_aliases must be a list")
    values.extend(_clean_name(alias) for alias in aliases)
    values.extend(canonicalizer.canonical(value) for value in list(values))
    aggressive = {aggressive_match_key(value) for value in values if value}
    aggressive.discard("")
    conservative = {normalize_clinic_name(value) for value in values if value}
    conservative.discard("")

    def is_self(name: str) -> bool:
        loose = aggressive_match_key(name)
        return (bool(loose) and loose in aggressive) or normalize_clinic_name(name) in conservative

    return is_self


def _prompt_rows(pack: dict, expected_stage: str, label: str) -> list[dict]:
    if pack.get("stage") != expected_stage:
        raise ReportDataError(f"Stale {label}: expected stage={expected_stage!r}")
    rows = pack.get("prompts")
    if not isinstance(rows, list):
        raise ReportDataError(f"Malformed {label}: 'prompts' must be a list")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not _clean_name(row.get("id")):
            raise ReportDataError(f"Malformed {label}: every prompt needs an id")
        pid = str(row["id"])
        if pid in seen:
            raise ReportDataError(f"Malformed {label}: duplicate prompt id {pid!r}")
        seen.add(pid)
    return rows


def _load_platform_files(
    stage: str,
    stage_root: Path,
    platforms: Sequence[str],
    warnings: list[dict],
    hash_inputs: dict,
) -> list[_PlatformFiles]:
    files = []
    hash_inputs[stage] = {}
    for platform in platforms:
        root = platform_data_dir(stage_root, platform)
        extracted = _read_optional_dict(root / "extracted_names.json", "extracted names")
        if extracted is None:
            extracted = _read_optional_dict(root / "extracted_clinics.json", "extracted clinics")
        matches = _read_optional_dict(root / "name_matches.json", "name matches")
        manifest = _read_optional_dict(root / "platform_run.json", "platform manifest")
        hash_manifest = None
        if manifest is not None:
            results = manifest.get("results")
            hash_manifest = {
                "platform": manifest.get("platform"),
                "results": sorted(
                    [
                        {
                            "prompt_id": row.get("prompt_id"),
                            "status": row.get("status"),
                        }
                        for row in results
                        if isinstance(row, dict)
                    ],
                    key=lambda row: str(row.get("prompt_id") or ""),
                ) if isinstance(results, list) else results,
            }
        hash_inputs[stage][platform] = {
            "extracted": extracted,
            "matches": matches,
            # Output paths, wall-clock durations, and citation UI details do not
            # affect report metrics and would make an otherwise identical report
            # change after relocation or rerendering.
            "manifest": hash_manifest,
        }
        if extracted is None:
            warnings.append(_warning(
                "missing_extracted_names",
                f"No extracted clinic names are available for {PLATFORM_LABELS[platform]} {stage} results.",
                stage=stage,
                platform=platform,
            ))
        if matches is None and extracted:
            warnings.append(_warning(
                "missing_name_matches",
                f"No canonical name matches are available for {PLATFORM_LABELS[platform]} {stage} results.",
                stage=stage,
                platform=platform,
            ))
        files.append(_PlatformFiles(stage, platform, root, extracted, matches, manifest))
    return files


def _manifest_statuses(manifest: Optional[dict]) -> tuple[dict[str, str], Counter[str]]:
    statuses: dict[str, str] = {}
    counts: Counter[str] = Counter()
    if not manifest:
        return statuses, counts
    results = manifest.get("results")
    if not isinstance(results, list):
        raise ReportDataError("Malformed platform_run.json: 'results' must be a list")
    for result in results:
        if not isinstance(result, dict) or not _clean_name(result.get("prompt_id")):
            raise ReportDataError("Malformed platform_run.json: every result needs a prompt_id")
        prompt_id = str(result["prompt_id"])
        status = _clean_name(result.get("status")) or "unknown"
        statuses[prompt_id] = status
        counts[status] += 1
    return statuses, counts


def _is_valid_status(platform: str, status: str) -> bool:
    if platform == CHATGPT:
        return status in VALID_CHATGPT_STATUSES
    if platform == GOOGLE_AI_OVERVIEW:
        return status in VALID_GOOGLE_STATUSES
    return status in VALID_PROVIDER_STATUSES


def _build_outcomes(
    platform_files: _PlatformFiles,
    prompt_rows: Sequence[dict],
    prompt_meta: Mapping[str, dict],
    canonicalizer: _DisjointNames,
    is_self,
    client_name: str,
    warnings: list[dict],
) -> tuple[list[_Outcome], dict]:
    expected_ids = [str(row["id"]) for row in prompt_rows]
    expected_set = set(expected_ids)
    extracted = dict(platform_files.extracted or {})
    extracted = {str(k): v for k, v in extracted.items() if not str(k).startswith("_")}
    for pid, names in extracted.items():
        if not isinstance(names, list):
            raise ReportDataError(
                f"Malformed extracted names for {platform_files.platform}: {pid!r} must map to a list"
            )
    extra_ids = sorted(set(extracted) - expected_set)
    if extra_ids:
        warnings.append(_warning(
            "unexpected_prompt_ids",
            f"Ignored {len(extra_ids)} result(s) not present in the built {platform_files.stage} prompt pack.",
            stage=platform_files.stage,
            platform=platform_files.platform,
            count=len(extra_ids),
            examples=extra_ids[:5],
        ))

    statuses, manifest_counts = _manifest_statuses(platform_files.manifest)
    if platform_files.manifest is None and platform_files.platform != CHATGPT and extracted:
        warnings.append(_warning(
            "missing_platform_manifest",
            f"Collection status was inferred from extracted results for {PLATFORM_LABELS[platform_files.platform]}.",
            stage=platform_files.stage,
            platform=platform_files.platform,
        ))

    unknown_names = set()
    outcomes: list[_Outcome] = []
    status_counts: Counter[str] = Counter()
    for prompt_id in expected_ids:
        if prompt_id in statuses:
            status = statuses[prompt_id]
        elif prompt_id in extracted:
            status = "collected" if platform_files.platform == CHATGPT else "ok"
        else:
            status = "missing"
        status_counts[status] += 1
        valid_status = _is_valid_status(platform_files.platform, status)
        # A provider answer marked ok still needs a completed extraction; otherwise
        # an unavailable parse would be indistinguishable from a genuine empty list.
        has_names = prompt_id in extracted
        if not valid_status or (not has_names and status != "not_shown"):
            if valid_status and not has_names:
                warnings.append(_warning(
                    "missing_valid_extraction",
                    f"A valid {status} outcome has no completed name extraction and was excluded.",
                    stage=platform_files.stage,
                    platform=platform_files.platform,
                    examples=[prompt_id],
                ))
            continue
        ordered = []
        seen = set()
        for raw in extracted.get(prompt_id, []):
            raw_name = _clean_name(raw)
            if not raw_name:
                continue
            if platform_files.matches is not None and raw_name not in (platform_files.matches.get("canonical") or {}):
                unknown_names.add(raw_name)
            name = canonicalizer.canonical(raw_name)
            if is_self(raw_name) or is_self(name):
                name = client_name
            key = normalize_clinic_name(name)
            if key and key not in seen:
                seen.add(key)
                ordered.append(name)
        base_id = _base_prompt_id(prompt_id)
        meta = prompt_meta.get(base_id, {})
        outcomes.append(_Outcome(
            platform_files.stage,
            platform_files.platform,
            prompt_id,
            base_id,
            _clean_name(meta.get("stratum")),
            _clean_name(meta.get("sub_stratum")),
            _clean_name(meta.get("scoring")),
            tuple(str(value) for value in (meta.get("treatments") or []) if _clean_name(value)),
            tuple(ordered),
            status,
        ))

    ambiguous = (platform_files.matches or {}).get("ambiguous") or {}
    if not isinstance(ambiguous, dict):
        raise ReportDataError("Malformed name_matches.json: 'ambiguous' must be an object")
    if ambiguous:
        warnings.append(_warning(
            "ambiguous_name_matches",
            f"{len(ambiguous)} ambiguous clinic name match(es) require review.",
            stage=platform_files.stage,
            platform=platform_files.platform,
            count=len(ambiguous),
            examples=sorted(str(name) for name in ambiguous)[:5],
        ))
    if unknown_names:
        warnings.append(_warning(
            "unmatched_extracted_names",
            f"{len(unknown_names)} extracted clinic name(s) were absent from the canonical matcher output.",
            stage=platform_files.stage,
            platform=platform_files.platform,
            count=len(unknown_names),
            examples=sorted(unknown_names, key=str.casefold)[:5],
        ))

    valid = len(outcomes)
    expected = len(expected_ids)
    unavailable = Counter(status_counts)
    for outcome in outcomes:
        unavailable[outcome.source_status] -= 1
    unavailable = Counter({key: value for key, value in unavailable.items() if value > 0})
    collection = {
        "expected_outcomes": expected,
        "valid_outcomes": valid,
        "valid_rate": _round_rate(valid / expected) if expected else None,
        "status": "measured" if expected and valid == expected else ("partial" if valid else "not_measured"),
        "status_counts": dict(sorted(status_counts.items())),
        "manifest_status_counts": dict(sorted(manifest_counts.items())),
        "unavailable_reasons": [
            {"status": status, "count": count}
            for status, count in sorted(unavailable.items())
        ],
    }
    return outcomes, collection


def _entity_stats(outcomes: Sequence[_Outcome], entity: str) -> dict:
    appearances = 0
    first = 0
    positions = []
    entity_key = normalize_clinic_name(entity)
    for outcome in outcomes:
        keys = [normalize_clinic_name(name) for name in outcome.names]
        if entity_key in keys:
            appearances += 1
            position = keys.index(entity_key) + 1
            positions.append(position)
            if position == 1:
                first += 1
    valid = len(outcomes)
    return {
        "valid_outcomes": valid,
        "appearances": appearances,
        "appearance_rate": _round_rate(appearances / valid) if valid else None,
        "first_named": first,
        "first_named_rate": _round_rate(first / valid) if valid else None,
        "mean_position": round(sum(positions) / len(positions), 2) if positions else None,
    }


def _all_entity_counts(outcomes: Sequence[_Outcome]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for outcome in outcomes:
        counts.update(outcome.names)
    return counts


def _rank_map(counts: Mapping[str, int]) -> dict[str, int]:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold(), item[0]))
    return {name: index for index, (name, _) in enumerate(ordered, start=1)}


def _comparison(outcomes: Sequence[_Outcome], client_name: str, competitors: Sequence[str]) -> dict:
    valid = len(outcomes)
    client = _entity_stats(outcomes, client_name)
    competitor_stats = [(name, _entity_stats(outcomes, name)) for name in competitors]
    competitor_stats.sort(
        key=lambda item: (-item[1]["appearances"], item[0].casefold(), item[0])
    )
    leader_name = competitor_stats[0][0] if competitor_stats else None
    leader_appearances = competitor_stats[0][1]["appearances"] if competitor_stats else 0
    client_rate = client["appearance_rate"]
    leader_rate = _round_rate(leader_appearances / valid) if valid else None
    rating = rate_competitive_visibility(client_rate, leader_rate, valid)
    index = None
    if client_rate is not None and leader_rate:
        index = round(client_rate / leader_rate, 4)
    gap = None
    if client_rate is not None and leader_rate is not None:
        gap = round((client_rate - leader_rate) * 100, 1)
    return {
        "valid_outcomes": valid,
        "client_appearances": client["appearances"],
        "client_rate": client_rate,
        "leader_name": leader_name,
        "leader_appearances": leader_appearances,
        "leader_rate": leader_rate,
        "gap_pp": gap,
        "competitive_index": index,
        "rating": rating,
        "rating_label": RATING_LABELS[rating],
    }


def _treatment_breakdown(
    outcomes: Sequence[_Outcome],
    client_name: str,
    treatments: Sequence[str],
    platforms: Sequence[str],
) -> dict:
    """Build client visibility and competitor analysis for each treatment.

    Only recommendation/persona outcomes whose prompt is scored for appearance
    SOV and explicitly tagged with the treatment are eligible.  A comparison
    prompt tagged with two treatments can appear in both rows; rates are never
    summed across treatment rows.
    """

    def eligible(outcome: _Outcome, key: str) -> bool:
        return (
            outcome.scoring == "appearance_sov"
            and outcome.stratum in TREATMENT_VISIBILITY_STRATA
            and key in {treatment_key(value) for value in outcome.treatments}
        )

    def treatment_view(rows: Sequence[_Outcome]) -> dict:
        counts = _all_entity_counts(rows)
        rivals = select_top_competitors(counts, excluded=[client_name], limit=3)
        ranks = _rank_map(counts)
        client = _entity_stats(rows, client_name)
        client["market_rank"] = ranks.get(client_name)
        competitor_rows = []
        for rival in rivals:
            stats = _entity_stats(rows, rival)
            competitor_rows.append({
                "name": rival,
                **stats,
                "market_rank": ranks.get(rival),
                "gap_pp": round(
                    (client["appearance_rate"] - stats["appearance_rate"]) * 100,
                    1,
                )
                if client["appearance_rate"] is not None
                and stats["appearance_rate"] is not None
                else None,
            })
        comparison = _comparison(rows, client_name, rivals)
        return {
            "client": client,
            "comparison": comparison,
            # The compact report reads these headline fields directly while
            # the full comparison object remains available to other consumers.
            "leader_name": comparison["leader_name"],
            "leader_rate": comparison["leader_rate"],
            "gap_pp": comparison["gap_pp"],
            "rating": comparison["rating"],
            "rating_label": comparison["rating_label"],
            "competitors": competitor_rows,
        }

    rows = []
    for treatment in treatments:
        key = treatment_key(treatment)
        relevant = [outcome for outcome in outcomes if eligible(outcome, key)]
        view = treatment_view(relevant)
        platform_rows = []
        for platform in platforms:
            platform_outcomes = [row for row in relevant if row.platform == platform]
            platform_view = treatment_view(platform_outcomes)
            platform_rows.append({
                "platform": platform,
                "platform_label": PLATFORM_LABELS[platform],
                "valid_outcomes": len(platform_outcomes),
                **platform_view,
            })
        rows.append({
            "treatment": treatment,
            "label": treatment,
            "key": key,
            "status": "measured" if relevant else "not_measured",
            "valid_outcomes": len(relevant),
            "prompt_count": len({row.base_prompt_id for row in relevant}),
            "prompt_ids": sorted({row.base_prompt_id for row in relevant}),
            **view,
            "by_platform": platform_rows,
        })
    return {
        "methodology": {
            "scoring": "appearance_sov",
            "strata": sorted(TREATMENT_VISIBILITY_STRATA),
            "denominator": (
                "Valid recommendation outcomes explicitly tagged with this treatment; "
                "unrelated, informational, brand and control prompts are excluded."
            ),
        },
        "rows": rows,
    }


def _collection_total(collections: Sequence[dict]) -> dict:
    expected = sum(row["expected_outcomes"] for row in collections)
    valid = sum(row["valid_outcomes"] for row in collections)
    return {
        "expected_outcomes": expected,
        "valid_outcomes": valid,
        "valid_rate": _round_rate(valid / expected) if expected else None,
    }


def _brand_target(base_prompt_id: str, basket_meta: Mapping[str, dict], config: dict) -> Optional[str]:
    meta = basket_meta.get(base_prompt_id) or {}
    attach = meta.get("attach") or []
    if not isinstance(attach, list):
        return None
    for placeholder in attach:
        if str(placeholder).startswith("COMPETITOR_"):
            config_key = str(placeholder).lower()
            target = _clean_name(config.get(config_key))
            return target or None
    return None


def _recognition(outcomes: Sequence[_Outcome], entity: str) -> dict:
    return _entity_stats(outcomes, entity)


def _brand_comparison(own: dict, rivals: Sequence[dict]) -> dict:
    measurable = [row for row in rivals if row["valid_outcomes"] > 0]
    measurable.sort(key=lambda row: (-(row["appearance_rate"] or 0), row["name"].casefold(), row["name"]))
    leader = measurable[0] if measurable else None
    own_rate = own["appearance_rate"]
    leader_rate = leader["appearance_rate"] if leader else None
    comparable = own["valid_outcomes"] > 0 and leader is not None
    rating = rate_competitive_visibility(own_rate, leader_rate, 1 if comparable else 0)
    index = round(own_rate / leader_rate, 4) if own_rate is not None and leader_rate else None
    gap = round((own_rate - leader_rate) * 100, 1) if own_rate is not None and leader_rate is not None else None
    return {
        "client_rate": own_rate,
        "client_valid_outcomes": own["valid_outcomes"],
        "leader_name": leader["name"] if leader else None,
        "leader_rate": leader_rate,
        "leader_valid_outcomes": leader["valid_outcomes"] if leader else 0,
        "gap_pp": gap,
        "competitive_index": index,
        "rating": rating,
        "rating_label": RATING_LABELS[rating],
    }


def _head_to_head_row(
    outcomes: Sequence[_Outcome],
    client_name: str,
    competitor: str,
    *,
    prompt_id: Optional[str] = None,
) -> dict:
    wins = ties = losses = client_named = rival_named = client_first = rival_first = 0
    client_key = normalize_clinic_name(client_name)
    rival_key = normalize_clinic_name(competitor)
    for outcome in outcomes:
        keys = [normalize_clinic_name(name) for name in outcome.names]
        client_position = keys.index(client_key) if client_key in keys else None
        rival_position = keys.index(rival_key) if rival_key in keys else None
        client_named += client_position is not None
        rival_named += rival_position is not None
        if client_position is None and rival_position is None:
            ties += 1
        elif rival_position is None or (
            client_position is not None and client_position < rival_position
        ):
            wins += 1
            client_first += 1
        elif client_position is None or rival_position < client_position:
            losses += 1
            rival_first += 1
        else:
            ties += 1
    valid = len(outcomes)
    client_first_rate = _round_rate(client_first / valid) if valid else None
    rival_first_rate = _round_rate(rival_first / valid) if valid else None
    rating = rate_head_to_head(client_first_rate, rival_first_rate, valid)
    row = {
        "competitor": competitor,
        "valid_outcomes": valid,
        "client_named": client_named,
        "client_named_rate": _round_rate(client_named / valid) if valid else None,
        "rival_named": rival_named,
        "rival_named_rate": _round_rate(rival_named / valid) if valid else None,
        "client_first": client_first,
        "client_first_rate": client_first_rate,
        "rival_first": rival_first,
        "rival_first_rate": rival_first_rate,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "rating": rating,
        "rating_label": RATING_LABELS[rating],
    }
    if prompt_id is not None:
        row["prompt_id"] = prompt_id
    return row


def _head_to_head(
    brand_outcomes: Sequence[_Outcome],
    client_name: str,
    basket_meta: Mapping[str, dict],
    config: dict,
    canonicalizer: _DisjointNames,
) -> dict:
    head = [outcome for outcome in brand_outcomes if outcome.sub_stratum == "head_to_head"]
    by_prompt_groups: dict[tuple[str, str], list[_Outcome]] = defaultdict(list)
    by_competitor_groups: dict[str, list[_Outcome]] = defaultdict(list)
    for outcome in head:
        raw_target = _brand_target(outcome.base_prompt_id, basket_meta, config)
        if not raw_target:
            continue
        competitor = canonicalizer.canonical(raw_target)
        by_prompt_groups[(outcome.base_prompt_id, competitor)].append(outcome)
        by_competitor_groups[competitor].append(outcome)
    by_prompt = [
        _head_to_head_row(rows, client_name, competitor, prompt_id=prompt_id)
        for (prompt_id, competitor), rows in sorted(by_prompt_groups.items())
    ]
    by_competitor = [
        _head_to_head_row(rows, client_name, competitor)
        for competitor, rows in sorted(by_competitor_groups.items(), key=lambda item: item[0].casefold())
    ]
    return {"by_prompt": by_prompt, "by_competitor": by_competitor}


def build_visibility_snapshot_payload(
    source: Path | str,
    *,
    generated_at: str,
    platforms: Sequence[str] = FULL_AUDIT_PLATFORMS,
    strict: bool = False,
) -> dict:
    """Load workflow outputs and return the complete renderer-ready payload.

    ``source`` is the free-audit root containing the config, basket, built
    prompt packs, ``data/`` Stage 1 results, and ``audit-data/`` Stage 2 results.
    The generated timestamp is display metadata and intentionally does not alter
    the stable report/input hash.
    """
    source = Path(source).expanduser().resolve()
    generated_at = _clean_name(generated_at)
    if not generated_at:
        raise ReportDataError("generated_at must be a non-empty ISO timestamp")
    platforms = tuple(dict.fromkeys(str(platform) for platform in platforms))
    unknown_platforms = [platform for platform in platforms if platform not in PLATFORM_LABELS]
    if unknown_platforms:
        raise ReportDataError(f"Unknown report platform(s): {', '.join(unknown_platforms)}")
    if not platforms:
        raise ReportDataError("At least one report platform is required")

    config = _read_required_dict(source / "free_audit_config.json", "free-audit config")
    basket = _read_required_dict(source / "audit_prompts.json", "audit prompt basket")
    procedures = _read_optional_dict(source / "procedures.json", "treatment procedures") or {}
    snapshot_pack = _read_required_dict(source / "snapshot_prompts.json", "snapshot prompt pack")
    audit_pack = _read_optional_dict(source / "audit_prompts_built.json", "audit prompt pack")
    client_name = _clean_name(config.get("clinic_name"))
    if not client_name:
        raise ReportDataError("free_audit_config.json has no clinic_name")
    if _clean_name(snapshot_pack.get("clinic_name")) != client_name:
        raise ReportDataError("Stale snapshot prompt pack: clinic_name does not match the current config")
    if audit_pack is not None and _clean_name(audit_pack.get("clinic_name")) != client_name:
        raise ReportDataError("Stale audit prompt pack: clinic_name does not match the current config")
    basket_version = _clean_name(basket.get("basket_version"))
    if snapshot_pack.get("basket_version") != basket.get("basket_version"):
        raise ReportDataError("Stale snapshot prompt pack: basket_version does not match the basket")
    if audit_pack is not None and audit_pack.get("basket_version") != basket.get("basket_version"):
        raise ReportDataError("Stale audit prompt pack: basket_version does not match the basket")

    treatment_labels = configured_treatments(config, procedures)
    for label, pack in (("snapshot", snapshot_pack), ("audit", audit_pack)):
        if pack is None or "treatments" not in pack:
            continue
        packed_treatments = pack.get("treatments")
        if not isinstance(packed_treatments, list):
            raise ReportDataError(f"Malformed {label} prompt pack: treatments must be a list")
        if [treatment_key(value) for value in packed_treatments] != [
            treatment_key(value) for value in treatment_labels
        ]:
            raise ReportDataError(
                f"Stale {label} prompt pack: configured treatments have changed"
            )

    snapshot_rows = _prompt_rows(snapshot_pack, "snapshot", "snapshot prompt pack")
    warnings: list[dict] = []
    if audit_pack is None:
        audit_rows: list[dict] = []
        warnings.append(_warning(
            "missing_audit_prompt_pack",
            "The Stage 2 brand prompt pack is missing; brand results are not measured.",
            stage="brand",
        ))
    else:
        audit_rows = _prompt_rows(audit_pack, "audit", "audit prompt pack")

    basket_rows = basket.get("prompts")
    if not isinstance(basket_rows, list):
        raise ReportDataError("Malformed audit prompt basket: 'prompts' must be a list")
    basket_meta = {
        str(row.get("id")): row
        for row in basket_rows
        if isinstance(row, dict) and row.get("id") is not None
    }
    snapshot_meta = {}
    inferred_treatment_rows = 0
    for row in snapshot_rows:
        metadata = dict(row)
        if "treatments" not in metadata:
            inferred_treatment_rows += 1
        metadata["treatments"] = prompt_treatment_tags(metadata, treatment_labels)
        snapshot_meta[_base_prompt_id(row["id"])] = metadata
    audit_meta = {_base_prompt_id(row["id"]): row for row in audit_rows}
    if treatment_labels and inferred_treatment_rows:
        warnings.append(_warning(
            "inferred_treatment_tags",
            (
                f"Treatment attribution was inferred from prompt text for "
                f"{inferred_treatment_rows} legacy prompt row(s); rebuild the prompt pack "
                "to persist explicit tags."
            ),
            stage="market",
            count=inferred_treatment_rows,
        ))

    hash_inputs = {
        "config": {
            "clinic_name": client_name,
            "clinic_aliases": config.get("clinic_aliases") or [],
            "area": config.get("area"),
            "competitor_1": config.get("competitor_1"),
            "competitor_2": config.get("competitor_2"),
            "competitor_3": config.get("competitor_3"),
            "treatments": treatment_labels,
        },
        "basket_version": basket_version,
        "snapshot_prompts": snapshot_pack,
        "audit_prompts": audit_pack,
    }
    market_files = _load_platform_files(
        "market", source / "data", platforms, warnings, hash_inputs
    )
    brand_files = _load_platform_files(
        "brand", source / "audit-data", platforms, warnings, hash_inputs
    )
    canonicalizer = _DisjointNames()
    for platform_file in market_files + brand_files:
        canonicalizer.ingest(platform_file.matches)
    is_self = _self_matcher(config, canonicalizer)

    market_outcomes: list[_Outcome] = []
    brand_outcomes: list[_Outcome] = []
    market_collections: dict[str, dict] = {}
    brand_collections: dict[str, dict] = {}
    for platform_file in market_files:
        outcomes, collection = _build_outcomes(
            platform_file, snapshot_rows, snapshot_meta, canonicalizer,
            is_self, client_name, warnings,
        )
        market_outcomes.extend(outcomes)
        market_collections[platform_file.platform] = collection
    for platform_file in brand_files:
        outcomes, collection = _build_outcomes(
            platform_file, audit_rows, audit_meta, canonicalizer,
            is_self, client_name, warnings,
        )
        brand_outcomes.extend(outcomes)
        brand_collections[platform_file.platform] = collection

    if not market_outcomes:
        warnings.append(_warning(
            "no_valid_market_outcomes",
            "No valid Stage 1 market outcomes are available.",
            stage="market",
        ))
    if not brand_outcomes:
        warnings.append(_warning(
            "no_valid_brand_outcomes",
            "No valid Stage 2 brand outcomes are available.",
            stage="brand",
        ))

    market_counts = _all_entity_counts(market_outcomes)
    competitors = select_top_competitors(market_counts, excluded=[client_name], limit=3)
    overall_comparison = _comparison(market_outcomes, client_name, competitors)
    overall_ranks = _rank_map(market_counts)
    client_market = _entity_stats(market_outcomes, client_name)
    client_market["market_rank"] = overall_ranks.get(client_name)

    platform_rows = []
    by_platform = []
    for platform in platforms:
        platform_market = [row for row in market_outcomes if row.platform == platform]
        platform_brand = [row for row in brand_outcomes if row.platform == platform]
        comparison = _comparison(platform_market, client_name, competitors)
        comparison.update({"platform": platform, "platform_label": PLATFORM_LABELS[platform]})
        by_platform.append(comparison)
        client_platform_market = _entity_stats(platform_market, client_name)
        client_platform_market["market_rank"] = _rank_map(_all_entity_counts(platform_market)).get(client_name)
        own_platform = [row for row in platform_brand if row.sub_stratum == "own_brand"]
        platform_rows.append({
            "key": platform,
            "label": PLATFORM_LABELS[platform],
            "collection": {
                "market": market_collections[platform],
                "brand": brand_collections[platform],
            },
            "market": client_platform_market,
            "brand": {
                "own_brand": _recognition(own_platform, client_name),
            },
            "competition": comparison,
        })

    by_stratum = []
    for stratum in MARKET_STRATA:
        rows = [outcome for outcome in market_outcomes if outcome.stratum == stratum]
        comparison = _comparison(rows, client_name, competitors)
        comparison.update({"stratum": stratum, "label": STRATUM_LABELS[stratum]})
        by_stratum.append(comparison)

    comparison_set = [client_name] + competitors
    platform_leaders: dict[str, set[str]] = defaultdict(set)
    for platform in platforms:
        rows = [outcome for outcome in market_outcomes if outcome.platform == platform]
        counts = {name: _entity_stats(rows, name)["appearances"] for name in comparison_set}
        max_count = max(counts.values(), default=0)
        if max_count:
            for name, count in counts.items():
                if count == max_count:
                    platform_leaders[name].add(platform)

    leaderboard = []
    for name in comparison_set:
        stats = _entity_stats(market_outcomes, name)
        stats.update({
            "name": name,
            "is_client": name == client_name,
            "market_rank": overall_ranks.get(name),
            "platforms_led": sorted(platform_leaders.get(name, set()), key=platforms.index),
        })
        leaderboard.append(stats)
    leaderboard.sort(key=lambda row: (-row["appearances"], row["name"].casefold(), row["name"]))

    competitor_profiles = []
    for competitor in competitors:
        stats = _entity_stats(market_outcomes, competitor)
        strongest_platform = None
        platform_candidates = []
        for platform in platforms:
            rows = [outcome for outcome in market_outcomes if outcome.platform == platform]
            candidate = _entity_stats(rows, competitor)
            platform_candidates.append((candidate["appearance_rate"] or 0, PLATFORM_LABELS[platform], platform))
        if platform_candidates and stats["appearances"]:
            strongest_platform = max(platform_candidates, key=lambda item: (item[0], -platforms.index(item[2])))[1]
        stratum_candidates = []
        for stratum in MARKET_STRATA:
            rows = [outcome for outcome in market_outcomes if outcome.stratum == stratum]
            candidate = _entity_stats(rows, competitor)
            stratum_candidates.append((candidate["appearance_rate"] or 0, STRATUM_LABELS[stratum], stratum))
        strongest_stratum = None
        if stratum_candidates and stats["appearances"]:
            strongest_stratum = max(
                stratum_candidates,
                key=lambda item: (item[0], -MARKET_STRATA.index(item[2])),
            )[1]
        competitor_profiles.append({
            "name": competitor,
            **stats,
            "strongest_platform": strongest_platform,
            "strongest_stratum": strongest_stratum,
            "gap_pp": round((client_market["appearance_rate"] - stats["appearance_rate"]) * 100, 1)
            if client_market["appearance_rate"] is not None and stats["appearance_rate"] is not None else None,
        })

    own_brand_outcomes = [row for row in brand_outcomes if row.sub_stratum == "own_brand"]
    competitor_brand_outcomes = [row for row in brand_outcomes if row.sub_stratum == "competitor_brand"]
    own_brand = _recognition(own_brand_outcomes, client_name)
    brand_rival_groups: dict[str, list[_Outcome]] = defaultdict(list)
    for outcome in competitor_brand_outcomes:
        target = _brand_target(outcome.base_prompt_id, basket_meta, config)
        if target:
            brand_rival_groups[canonicalizer.canonical(target)].append(outcome)
    rival_recognition = []
    for rival, rows in sorted(brand_rival_groups.items(), key=lambda item: item[0].casefold()):
        rival_recognition.append({"name": rival, **_recognition(rows, rival)})
    brand_comparison = _brand_comparison(own_brand, rival_recognition)

    brand_by_platform = []
    for platform in platforms:
        own_rows = [row for row in own_brand_outcomes if row.platform == platform]
        own = _recognition(own_rows, client_name)
        rivals = []
        for rival, rows in sorted(brand_rival_groups.items(), key=lambda item: item[0].casefold()):
            platform_rival_rows = [row for row in rows if row.platform == platform]
            rivals.append({"name": rival, **_recognition(platform_rival_rows, rival)})
        brand_by_platform.append({
            "platform": platform,
            "platform_label": PLATFORM_LABELS[platform],
            "own_brand": own,
            "competitors": rivals,
            "comparison": _brand_comparison(own, rivals),
        })

    head_to_head = _head_to_head(
        brand_outcomes, client_name, basket_meta, config, canonicalizer
    )
    treatment_analysis = _treatment_breakdown(
        market_outcomes, client_name, treatment_labels, platforms
    )
    market_coverage = _collection_total(list(market_collections.values()))
    brand_coverage = _collection_total(list(brand_collections.values()))
    total_expected = market_coverage["expected_outcomes"] + brand_coverage["expected_outcomes"]
    total_valid = market_coverage["valid_outcomes"] + brand_coverage["valid_outcomes"]
    input_sha256 = _stable_sha256(hash_inputs)

    aliases = config.get("clinic_aliases") or []
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_id": f"VS-{input_sha256[:12].upper()}",
        "input_sha256": input_sha256,
        "generated_at": generated_at,
        "client": {
            "name": client_name,
            "aliases": [_clean_name(alias) for alias in aliases if _clean_name(alias)],
            "area": _clean_name(config.get("area")) or None,
            "treatments": treatment_labels,
        },
        "basket": {
            "version": basket_version or None,
            "run_mode": snapshot_pack.get("run_mode") or (audit_pack or {}).get("run_mode"),
            "market_prompts": len(snapshot_rows),
            "brand_prompts": len(audit_rows),
        },
        "coverage": {
            "expected_outcomes": total_expected,
            "valid_outcomes": total_valid,
            "valid_rate": _round_rate(total_valid / total_expected) if total_expected else None,
            "market": market_coverage,
            "brand": brand_coverage,
        },
        "market": {
            "valid_outcomes": len(market_outcomes),
            "client": client_market,
        },
        "platforms": platform_rows,
        "competition": {
            "competitors": competitor_profiles,
            "leaderboard": leaderboard,
            "overall": overall_comparison,
            "by_platform": by_platform,
            "by_stratum": by_stratum,
            "by_treatment": treatment_analysis["rows"],
            "head_to_head": head_to_head,
        },
        "treatments": treatment_analysis,
        "brand": {
            "own_brand": own_brand,
            "competitor_brand": {"competitors": rival_recognition},
            "comparison": brand_comparison,
            "by_platform": brand_by_platform,
            "head_to_head": head_to_head,
        },
        "warnings": warnings,
    }
    if strict and warnings:
        codes = ", ".join(dict.fromkeys(row["code"] for row in warnings))
        raise ReportDataError(f"Strict validation failed: {codes}")
    return payload
