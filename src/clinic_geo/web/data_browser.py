"""Read-only data catalog and preview helpers for the local control UI."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

from clinic_geo.paths import root_or_default


TEXT_EXTENSIONS = {".txt", ".md", ".log", ".json", ".jsonl", ".csv", ".html", ".htm"}
DOWNLOAD_ONLY_EXTENSIONS = {".pdf", ".xlsx", ".xls"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | DOWNLOAD_ONLY_EXTENSIONS
MAX_TEXT_BYTES = 250_000
MAX_TABLE_ROWS = 200

TABLE_EXTENSIONS = {".json", ".jsonl", ".csv"}
MAX_TABLE_FILE_BYTES = 20_000_000
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

_NUMERIC_TEXT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_SCORE_HINT = re.compile(r"score|rating|_0_\d+$|_1_\d+$", re.IGNORECASE)
_MISSING = object()

_TABLE_CACHE: dict[tuple, dict] = {}
_TABLE_CACHE_LOCK = threading.Lock()
_TABLE_CACHE_MAX = 3


@dataclass(frozen=True)
class DataSource:
    id: str
    label: str
    path: str


@dataclass(frozen=True)
class CuratedDataset:
    id: str
    label: str
    description: str
    path: str
    group: str
    badge: str
    unit: str
    default_sort: str = ""
    default_direction: str = "asc"
    preferred_columns: tuple[str, ...] = ()
    create_action: str = ""


DATA_SOURCES = [
    DataSource("appearance", "Appearance", "data/appearance"),
    DataSource("free_audit", "Free Audit", "data/free-audit"),
    DataSource("audit_results", "Audit Results", "audit-results"),
    DataSource("database", "Database Formation", "data/database"),
    DataSource("clinic_analysis", "Clinic Analysis", "data/clinic-analysis"),
    DataSource("area_tagging", "Area Tagging", "data/area-tagging"),
    DataSource("site_describer", "Site Describer", "data/clinic-analysis/website_cache"),
    DataSource("ui_runs", "UI Runs", "data/ui-runs"),
]


CURATED_DATASETS = [
    CuratedDataset(
        "prospect_accounts",
        "Prospect accounts",
        "One row per deduplicated business, ranked for outreach. Start here.",
        "data/database/prospect-scoring/prospect-accounts.jsonl",
        "Use for outreach",
        "Recommended",
        "accounts",
        "prospect_rank",
        "asc",
        (
            "clinic_name", "prospect_rank", "prospect_score_0_100", "prospect_tier",
            "prospect_status", "commercial_capacity_score", "geo_opportunity_score",
            "digital_readiness_score", "service_fit_score", "evidence_access_score",
            "area", "outreach_route", "owned_website", "risk_flags",
        ),
        "Run Score Prospects to create this dataset.",
    ),
    CuratedDataset(
        "scored_clinics",
        "Scored clinic records",
        "Every clinic and location record with its prospect score, tier, status, and risk flags.",
        "data/database/prospect-scoring/scored-clinics.jsonl",
        "Use for outreach",
        "All clinic rows",
        "clinic records",
        "prospect_rank",
        "asc",
        (
            "clinic_name", "uid", "account_id", "is_primary_record", "prospect_rank",
            "prospect_score_0_100", "prospect_tier", "prospect_status",
            "commercial_capacity_score", "geo_opportunity_score", "digital_readiness_score",
            "service_fit_score", "evidence_access_score", "risk_flags",
        ),
        "Run Score Prospects to create this dataset.",
    ),
    CuratedDataset(
        "scoring_quality",
        "Scoring quality summary",
        "Coverage, tier counts, source hashes, warnings, and the scoring method.",
        "data/database/prospect-scoring/quality-summary.json",
        "Quality checks",
        "QA",
        "report",
        create_action="Run Score Prospects to create this report.",
    ),
    CuratedDataset(
        "database_release",
        "Complete database release",
        "Active immutable release, with links to its coverage, validation, review, and outreach files.",
        "data/database/current-release.json",
        "Quality checks",
        "Release",
        "report",
        create_action="Run Build Complete Database to publish this report.",
    ),
    CuratedDataset(
        "clinic_evidence",
        "Clinic evidence (source)",
        "Research evidence and commercial fields only. This input does not contain prospect scores.",
        "data/database/clinic-evidence.jsonl",
        "Supporting data",
        "Source",
        "clinic records",
        "clinic_name",
        "asc",
        (
            "clinic_name", "uid", "category", "input_address", "official_website_in_input",
            "reviews_reputation_0_30", "website_booking_maturity_0_15",
            "social_brand_reach_0_20", "credibility_press_accreditation_0_15",
            "footprint_commercial_maturity_0_20", "success_score_1_100", "confidence",
            "last_researched",
        ),
    ),
    CuratedDataset(
        "clinic_territories",
        "Clinic territories",
        "Clinic records with London territory, postcode provenance, and review status.",
        "data/database/clinic-territories.jsonl",
        "Supporting data",
        "Areas",
        "clinic records",
        "area_name",
        "asc",
        (
            "clinic_name", "uid", "postcode", "area_name", "area_unit", "area_status",
            "area_confidence", "area_source", "input_address",
        ),
        "Run Assign Territories to create this dataset.",
    ),
    CuratedDataset(
        "ai_visibility",
        "AI visibility results",
        "Per-clinic prompt appearances and positions used by prospect scoring.",
        "data/appearance/analysis_result.json",
        "Supporting data",
        "Visibility",
        "clinics",
        "total",
        "desc",
        ("name", "in_database", "db_name", "total", "result"),
        "Run Appearance Analysis to create this dataset.",
    ),
    CuratedDataset(
        "territory_summary",
        "Territory coverage summary",
        "Territory assignment counts, coverage, capacity rules, and provenance.",
        "data/area-tagging/area_summary.json",
        "Quality checks",
        "Coverage",
        "report",
        create_action="Run Assign Territories to create this report.",
    ),
    CuratedDataset(
        "territory_review",
        "Territory review queue",
        "Clinics needing a postcode, location, or out-of-scope review.",
        "data/area-tagging/unresolved_areas.json",
        "Quality checks",
        "Review",
        "items",
        create_action="Run Assign Territories to create this queue.",
    ),
]


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()


def _safe_relative(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _iter_source_files(source: DataSource, root: Path) -> Iterable[Path]:
    source_root = root / source.path
    if not source_root.exists():
        return []
    return (
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in ALLOWED_EXTENSIONS
        and "__pycache__" not in path.parts
        and ".git" not in path.parts
    )


def _json_record_count(path: Path) -> int | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        list_values = [item for item in value.values() if isinstance(item, list)]
        if len(list_values) == 1:
            return len(list_values[0])
        population = value.get("population")
        if isinstance(population, dict) and isinstance(population.get("unique_accounts"), int):
            return population["unique_accounts"]
        if isinstance(value.get("rows"), int):
            return value["rows"]
    return None


def list_curated_datasets(root: Path | None = None) -> list[dict]:
    """Return the small, human-facing catalog without exposing workflow internals."""
    base = root_or_default(root)
    datasets = []
    for dataset in CURATED_DATASETS:
        path = base / dataset.path
        exists = path.is_file()
        payload = {
            "id": dataset.id,
            "label": dataset.label,
            "description": dataset.description,
            "path": dataset.path,
            "group": dataset.group,
            "badge": dataset.badge,
            "unit": dataset.unit,
            "default_sort": dataset.default_sort,
            "default_direction": dataset.default_direction,
            "preferred_columns": list(dataset.preferred_columns),
            "create_action": dataset.create_action,
            "exists": exists,
            "extension": Path(dataset.path).suffix.lower(),
            "size": 0,
            "modified": None,
            "record_count": None,
        }
        if exists:
            stat = path.stat()
            payload["size"] = stat.st_size
            payload["modified"] = _iso_mtime(path)
            payload["record_count"] = (
                _count_nonempty_lines(path)
                if path.suffix.lower() == ".jsonl"
                else None
                if dataset.unit == "report"
                else _json_record_count(path)
            )
        datasets.append(payload)
    return datasets


def list_data_files(
    *,
    root: Path | None = None,
    process: str = "",
    extension: str = "",
    query: str = "",
    limit: int = 2000,
    include_files: bool = True,
) -> dict:
    """List data/output files grouped by known workflow process roots."""
    base = root_or_default(root)
    process = process.strip()
    extension = extension.strip().lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    query = query.strip().lower()

    sources = DATA_SOURCES if not process else [src for src in DATA_SOURCES if src.id == process]
    scan_sources = sources if process else sorted(
        sources,
        key=lambda source: len(Path(source.path).parts),
        reverse=True,
    )
    files: List[dict] = []
    seen_paths: set[str] = set()
    for source in (scan_sources if include_files else []):
        for path in _iter_source_files(source, base):
            rel = _safe_relative(path, base)
            if rel in seen_paths:
                continue
            seen_paths.add(rel)
            if extension and path.suffix.lower() != extension:
                continue
            if query and query not in rel.lower():
                continue
            stat = path.stat()
            files.append(
                {
                    "process": source.id,
                    "process_label": source.label,
                    "name": path.name,
                    "path": rel,
                    "extension": path.suffix.lower(),
                    "size": stat.st_size,
                    "modified": _iso_mtime(path),
                }
            )

    files.sort(key=lambda item: (item["modified"], item["path"]), reverse=True)
    datasets = list_curated_datasets(base)
    return {
        "sources": [source.__dict__ for source in DATA_SOURCES],
        "datasets": datasets,
        "default_dataset": next(
            (dataset["id"] for dataset in datasets if dataset["exists"]),
            None,
        ),
        "files": files[:limit],
        "total": len(files),
    }


def resolve_data_path(relative_path: str, root: Path | None = None) -> Path:
    """Resolve a data-browser path and reject anything outside known data roots."""
    base = root_or_default(root).resolve()
    candidate = (base / relative_path).resolve()
    candidate.relative_to(base)
    allowed_roots = [(base / source.path).resolve() for source in DATA_SOURCES]
    if not any(candidate == allowed or allowed in candidate.parents for allowed in allowed_roots):
        raise ValueError("path is outside data sources")
    if not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise FileNotFoundError(relative_path)
    return candidate


def _read_limited_text(path: Path) -> tuple[str, bool]:
    data = path.read_bytes()
    truncated = len(data) > MAX_TEXT_BYTES
    if truncated:
        data = data[:MAX_TEXT_BYTES]
    return data.decode("utf-8", errors="replace"), truncated


def _count_nonempty_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def _table_from_json(value) -> Optional[dict]:
    rows = value if isinstance(value, list) else None
    if isinstance(value, dict):
        list_values = [v for v in value.values() if isinstance(v, list)]
        if len(list_values) == 1:
            rows = list_values[0]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows[:MAX_TABLE_ROWS]):
        return None
    keys: List[str] = []
    for row in rows[:MAX_TABLE_ROWS]:
        for key in row:
            if key not in keys:
                keys.append(key)
    return {"columns": keys, "rows": rows[:MAX_TABLE_ROWS], "total_rows": len(rows)}


def preview_data_file(relative_path: str, root: Path | None = None) -> dict:
    """Return a bounded preview for a known data file."""
    path = resolve_data_path(relative_path, root)
    text, truncated = ("", False)
    kind = "binary"
    table = None
    parsed = None

    if path.suffix.lower() in TEXT_EXTENSIONS:
        text, truncated = _read_limited_text(path)
        kind = "text"
        if path.suffix.lower() == ".json":
            try:
                parsed = json.loads(text)
                table = _table_from_json(parsed)
                kind = "json"
            except json.JSONDecodeError:
                parsed = None
        elif path.suffix.lower() == ".jsonl":
            rows = []
            lines = text.splitlines()
            if truncated and text and not text.endswith(("\n", "\r")):
                lines = lines[:-1]
            for line in lines[:MAX_TABLE_ROWS]:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"line": line})
            table = _table_from_json(rows)
            if table is not None:
                table["total_rows"] = _count_nonempty_lines(path)
            kind = "jsonl"
        elif path.suffix.lower() == ".csv":
            rows = list(csv.DictReader(text.splitlines()))
            table = _table_from_json(rows)
            if table is not None and truncated:
                # text stops at MAX_TEXT_BYTES, so len(rows) would undercount a large CSV. Recount
                # the real file, as the .jsonl branch does.
                table["total_rows"] = max(0, _count_nonempty_lines(path) - 1)
            kind = "csv"

    stat = path.stat()
    return {
        "path": _safe_relative(path, root_or_default(root)),
        "name": path.name,
        "extension": path.suffix.lower(),
        "size": stat.st_size,
        "modified": _iso_mtime(path),
        "kind": kind,
        "text": text,
        "truncated": truncated,
        "table": table,
        "download_only": path.suffix.lower() in DOWNLOAD_ONLY_EXTENSIONS,
    }


def _rows_from_jsonl(path: Path) -> tuple[List[dict], int]:
    rows: List[dict] = []
    skipped = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(value, dict):
                rows.append(value)
            else:
                skipped += 1
    return rows, skipped


def _rows_from_json(path: Path) -> tuple[List[dict], int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"file is not valid JSON: {exc.msg}") from exc
    candidate = value if isinstance(value, list) else None
    if isinstance(value, dict):
        list_values = [item for item in value.values() if isinstance(item, list)]
        if len(list_values) == 1:
            candidate = list_values[0]
    if not isinstance(candidate, list):
        raise ValueError("this file has no tabular rows")
    rows = [row for row in candidate if isinstance(row, dict)]
    return rows, len(candidate) - len(rows)


def _rows_from_csv(path: Path) -> tuple[List[dict], int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        rows = []
        for raw in csv.DictReader(io.StringIO(text), restval=""):
            raw.pop(None, None)
            rows.append(dict(raw))
    except csv.Error as exc:
        raise ValueError(f"could not parse CSV: {exc}") from exc
    return rows, 0


def _columns_of(rows: Iterable[dict]) -> List[str]:
    keys: dict[str, None] = {}
    for row in rows:
        for key in row:
            keys.setdefault(str(key), None)
    return list(keys)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _column_types(rows: List[dict], columns: List[str], *, coerce_text: bool) -> dict[str, str]:
    """Type each column from every row in the file.

    `coerce_text` is only true for CSV, where csv.DictReader stringifies every value and a column
    like run_number would otherwise sort "1", "10", "2". JSON keeps its own types, so a genuine
    string such as basket_version "1.0" stays a string.
    """
    types: dict[str, str] = {}
    for column in columns:
        values = [row[column] for row in rows if column in row and not _is_blank(row[column])]
        if not values:
            types[column] = "empty"
        elif any(isinstance(value, (list, dict)) for value in values):
            types[column] = "complex"
        elif all(isinstance(value, bool) for value in values):
            types[column] = "boolean"
        elif all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            types[column] = "number"
        elif coerce_text and all(isinstance(value, str) and _NUMERIC_TEXT.match(value.strip()) for value in values):
            types[column] = "number"
        else:
            types[column] = "string"
    return types


def _load_table(path: Path) -> dict:
    """Parse a whole data file into rows, memoized on (path, mtime, size)."""
    stat = path.stat()
    if stat.st_size > MAX_TABLE_FILE_BYTES:
        raise ValueError(
            f"file is too large for the table view "
            f"({stat.st_size / 1_000_000:.1f} MB > {MAX_TABLE_FILE_BYTES / 1_000_000:.1f} MB) - use Download instead"
        )
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _TABLE_CACHE_LOCK:
        hit = _TABLE_CACHE.get(key)
    if hit is not None:
        return hit

    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows, skipped = _rows_from_jsonl(path)
    elif suffix == ".json":
        rows, skipped = _rows_from_json(path)
    elif suffix == ".csv":
        rows, skipped = _rows_from_csv(path)
    else:
        raise ValueError("this file type has no table view")

    # A file whose only rows are scalars (a JSON dict holding one list of strings, a JSONL of bare
    # values) is not a table. Say so, so the caller falls back to showing the raw contents rather
    # than an empty grid that claims the file has 0 rows.
    if not rows and skipped:
        raise ValueError("this file has no tabular rows")

    columns = _columns_of(rows)
    types = _column_types(rows, columns, coerce_text=(suffix == ".csv"))
    numeric = [column for column in columns if types[column] == "number"]
    loaded = {
        "kind": suffix.lstrip("."),
        "rows": rows,
        "skipped": skipped,
        "columns": columns,
        "types": types,
        "numeric": numeric,
        "score_columns": [column for column in numeric if _SCORE_HINT.search(column)],
        "sortable": [column for column in columns if types[column] != "complex"],
    }
    with _TABLE_CACHE_LOCK:
        _TABLE_CACHE[key] = loaded
        while len(_TABLE_CACHE) > _TABLE_CACHE_MAX:
            _TABLE_CACHE.pop(next(iter(_TABLE_CACHE)))
    return loaded


def _row_haystack(row: dict) -> str:
    # Values only: serializing the whole dict would also match column NAMES, so searching "area" or
    # "score" (both suggested in the filter placeholder) would match every row in the file.
    return json.dumps(list(row.values()), ensure_ascii=False, default=str).lower()


def _sort_key(value: Any, column_type: str):
    if column_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            # An int too large for a float would otherwise escape as an uncaught OverflowError.
            return float("inf")
    if column_type == "boolean":
        return int(bool(value))
    return str(value).casefold()


def _sort_rows(rows: List[dict], column: str, direction: str, column_type: str) -> List[dict]:
    """Sort by one column, always leaving missing/blank values last in both directions."""
    present: List[dict] = []
    absent: List[dict] = []
    for row in rows:
        value = row.get(column, _MISSING)
        if value is _MISSING or _is_blank(value):
            absent.append(row)
            continue
        if column_type == "number" and isinstance(value, str) and not _NUMERIC_TEXT.match(value.strip()):
            absent.append(row)
            continue
        present.append(row)
    present.sort(key=lambda row: _sort_key(row[column], column_type), reverse=(direction == "desc"))
    return present + absent


def _json_safe(value: Any):
    if isinstance(value, float):
        return None if (value != value or value in (float("inf"), float("-inf"))) else value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def table_view(
    relative_path: str,
    *,
    root: Path | None = None,
    sort: str = "",
    direction: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    query: str = "",
) -> dict:
    """Sortable, filterable, paginated table over the *whole* file.

    preview_data_file only ever sees the first 250KB, so a sort built on its rows would rank a
    truncated head of the file and label it with the full row count. Everything here reads every row.
    """
    path = resolve_data_path(relative_path, root)
    if path.suffix.lower() not in TABLE_EXTENSIONS:
        raise ValueError("this file type has no table view")

    loaded = _load_table(path)

    direction = (direction or "asc").strip().lower() or "asc"
    if direction not in ("asc", "desc"):
        raise ValueError("direction must be 'asc' or 'desc'")
    sort = (sort or "").strip()
    if sort:
        if sort not in loaded["columns"]:
            raise ValueError(f"unknown sort column: {sort}")
        if sort not in loaded["sortable"]:
            raise ValueError(f"column is not sortable: {sort}")

    rows = loaded["rows"]
    normalized_query = (query or "").strip().lower()
    if normalized_query:
        rows = [row for row in rows if normalized_query in _row_haystack(row)]
    filtered = len(rows)
    if sort:
        rows = _sort_rows(rows, sort, direction, loaded["types"][sort])

    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    pages = max(1, math.ceil(filtered / page_size))
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * page_size

    return {
        "path": _safe_relative(path, root_or_default(root)),
        "name": path.name,
        "extension": path.suffix.lower(),
        "kind": loaded["kind"],
        "columns": loaded["columns"],
        "column_types": loaded["types"],
        "sortable_columns": loaded["sortable"],
        "numeric_columns": loaded["numeric"],
        "score_columns": loaded["score_columns"],
        "rows": [_json_safe(row) for row in rows[start : start + page_size]],
        "total_rows": len(loaded["rows"]),
        "filtered_rows": filtered,
        "skipped_rows": loaded["skipped"],
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "sort": sort,
        "direction": direction,
        "q": normalized_query,
    }
