"""HTTP-facing adapter for the founder outreach workspace.

The adapter intentionally contains no sending capability.  It validates the
small public request surface used by the local UI and delegates all business
rules (including the final readiness re-check) to :class:`OutreachService`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from http import HTTPStatus
import inspect
from pathlib import Path
from typing import Any, Callable, Mapping


KNOWN_ACCOUNT_VIEWS = frozenset({"all", "prospects", "worklist", "founder", "pipeline", "today"})
KNOWN_ACTIONS = frozenset(
    {
        "add_to_worklist",
        "approve_draft",
        "confirm_branch",
        "log_interaction",
        "merge_branches",
        "remove_from_worklist",
        "resolve_import_conflict",
        "save_buyer",
        "save_channel_review",
        "save_contact",
        "save_draft",
        "save_rationale",
        "save_visibility_check",
        "set_branch_treatment",
        "set_priority_override",
        "set_stage",
        "suppress",
        "unsuppress",
    }
)
ACCOUNT_QUERY_FIELDS = frozenset(
    {
        "view",
        "q",
        "tier",
        "stage",
        "readiness",
        "worklist",
        "worklist_id",
        "limit",
        "offset",
        "sort",
        "direction",
    }
)
ACCOUNT_SORT_FIELDS = frozenset(
    {
        "rank",
        "score",
        "provisional_score",
        "commercial_capacity",
        "geo_opportunity",
        "digital_readiness",
        "service_fit",
        "evidence_access",
        "geo_visibility",
        "name",
        "area",
        "tier",
        "research_status",
        "visibility_status",
        "readiness",
        "buyer",
        "stage",
        "updated_at",
    }
)
ACCOUNT_DIRECTIONS = frozenset({"asc", "desc"})
POST_FIELDS = {
    "/api/outreach/sync": frozenset({"release", "dry_run", "actor"}),
    "/api/outreach/worklist": frozenset(
        {"name", "description", "limit", "tiers", "actor"}
    ),
    "/api/outreach/export": frozenset(
        {
            "worklist",
            "worklist_id",
            "worklist_name",
            "format",
            "ready_only",
            "outreach_purpose",
            "branch_id",
            "treatment_slug",
            "dry_run",
            "actor",
        }
    ),
}


@dataclass(frozen=True)
class ApiResponse:
    payload: dict[str, Any]
    status: HTTPStatus = HTTPStatus.OK


def _default_service_factory(root: Path):
    # Kept lazy so the established research UI remains importable even if the
    # optional outreach workspace has not yet been initialized on disk.
    from clinic_geo.outreach.api import OutreachService

    return OutreachService(root)


def _jsonable(value: Any) -> Any:
    """Convert service results to values accepted by ``json.dumps``."""
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    # sqlite3.Row behaves like a keyed sequence but is not a Mapping.
    keys = getattr(value, "keys", None)
    if callable(keys):
        try:
            return {str(key): _jsonable(value[key]) for key in keys()}
        except (KeyError, IndexError, TypeError):
            pass
    return value


def _single(query: Mapping[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name, [])
    if len(values) > 1:
        raise ValueError(f"{name} may only be supplied once")
    return str(values[0]).strip() if values else default


def _integer(value: Any, name: str, default: int, *, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _boolean(value: Any, name: str, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "on"}:
        return True
    if normalized in {"false", "no", "0", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _actor(payload: Mapping[str, Any]) -> str:
    actor = str(payload.get("actor") or "founder").strip()
    if not actor:
        return "founder"
    if len(actor) > 100:
        raise ValueError("actor is too long")
    return actor


def _reject_unknown(values: Mapping[str, Any], allowed: frozenset[str], context: str) -> None:
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"Unknown {context} field(s): {', '.join(unknown)}")


def _call(method: Callable[..., Any], **values: Any) -> Any:
    """Call a service method while tolerating intentionally optional API fields.

    The service owns the business contract.  This only avoids coupling the HTTP
    layer to optional presentation filters that a smaller service build may not
    expose yet; required arguments are never fabricated.
    """
    signature = inspect.signature(method)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return method(**values)
    supported = {name: value for name, value in values.items() if name in signature.parameters}
    return method(**supported)


def _error_response(exc: Exception) -> ApiResponse:
    name = type(exc).__name__
    family = {base.__name__ for base in type(exc).__mro__}
    message = str(exc).strip()
    if name == "NotFoundError":
        return ApiResponse(
            {"error": message or "The requested outreach record was not found.", "code": "not_found"},
            HTTPStatus.NOT_FOUND,
        )
    if name in {"ConflictError", "IntegrityError"}:
        return ApiResponse(
            {
                "error": message or "The outreach request conflicts with current records.",
                "code": "conflict",
            },
            HTTPStatus.CONFLICT,
        )
    if isinstance(exc, ValueError) or "OutreachError" in family:
        return ApiResponse(
            {"error": message or "Check the outreach request and try again.", "code": "invalid_request"},
            HTTPStatus.BAD_REQUEST,
        )
    return ApiResponse(
        {
            "error": "The outreach workspace could not complete this request.",
            "code": "internal_error",
        },
        HTTPStatus.INTERNAL_SERVER_ERROR,
    )


class OutreachAPI:
    """Validated, JSON-only API facade around ``OutreachService``."""

    def __init__(self, root: Path | str, service_factory: Callable[..., Any] | None = None):
        self.root = Path(root).resolve()
        self._service_factory = service_factory or _default_service_factory

    def _service(self):
        return self._service_factory(self.root)

    def handle_get(self, path: str, query: Mapping[str, list[str]]) -> ApiResponse:
        try:
            if path not in {
                "/api/outreach/dashboard",
                "/api/outreach/accounts",
                "/api/outreach/account",
            }:
                return ApiResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
            service = self._service()
            if path == "/api/outreach/dashboard":
                if query:
                    raise ValueError("The dashboard does not accept query fields")
                result = service.dashboard()
            elif path == "/api/outreach/accounts":
                result = self._list_accounts(service, query)
            elif path == "/api/outreach/account":
                _reject_unknown(
                    query,
                    frozenset({"id", "branch_id", "treatment_slug"}),
                    "account query",
                )
                account_id = _single(query, "id")
                if not account_id:
                    raise ValueError("Choose an outreach account")
                result = _call(
                    service.get_account,
                    account_id=account_id,
                    branch_id=_single(query, "branch_id") or None,
                    treatment_slug=_single(query, "treatment_slug") or None,
                )
            converted = _jsonable(result)
            return ApiResponse(converted if isinstance(converted, dict) else {"result": converted})
        except Exception as exc:  # Business errors are translated without leaking traces.
            return _error_response(exc)

    def _list_accounts(self, service: Any, query: Mapping[str, list[str]]) -> Any:
        _reject_unknown(query, ACCOUNT_QUERY_FIELDS, "account query")
        view = _single(query, "view", "prospects").lower()
        if view not in KNOWN_ACCOUNT_VIEWS:
            raise ValueError(f"Unknown account view: {view}")
        direction = _single(query, "direction", "asc").lower()
        if direction not in ACCOUNT_DIRECTIONS:
            raise ValueError("direction must be asc or desc")
        sort = _single(query, "sort")
        if sort and sort not in ACCOUNT_SORT_FIELDS:
            raise ValueError(f"Unknown account sort field: {sort}")
        limit = _integer(_single(query, "limit"), "limit", 100, minimum=1, maximum=1000)
        offset = _integer(_single(query, "offset"), "offset", 0, minimum=0, maximum=1_000_000)
        worklist = _single(query, "worklist") or _single(query, "worklist_id")
        return _call(
            service.list_accounts,
            view=view,
            q=_single(query, "q"),
            query=_single(query, "q"),
            tier=_single(query, "tier").upper(),
            stage=_single(query, "stage").lower(),
            readiness=_single(query, "readiness").lower(),
            worklist=worklist,
            worklist_id=worklist,
            limit=limit,
            offset=offset,
            sort=sort,
            direction=direction,
        )

    def handle_post(self, path: str, payload: Mapping[str, Any]) -> ApiResponse:
        try:
            if not isinstance(payload, Mapping):
                raise ValueError("The request body must be a JSON object")
            if path not in {
                "/api/outreach/sync",
                "/api/outreach/worklist",
                "/api/outreach/action",
                "/api/outreach/export",
            }:
                return ApiResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
            actor = _actor(payload)
            if path == "/api/outreach/sync":
                _reject_unknown(payload, POST_FIELDS[path], "sync")
                release = str(payload.get("release") or "current").strip().lower()
                if release != "current":
                    raise ValueError("Only the current verified research release can be connected")
                service = self._service()
                result = _call(
                    service.sync_current_release,
                    release=release,
                    dry_run=_boolean(payload.get("dry_run"), "dry_run", False),
                    actor=actor,
                )
                status = HTTPStatus.OK
            elif path == "/api/outreach/worklist":
                _reject_unknown(payload, POST_FIELDS[path], "worklist")
                name = str(payload.get("name") or "founder-25").strip()
                if not name:
                    raise ValueError("Give the founder list a name")
                tiers = payload.get("tiers", ["A", "B"])
                if not isinstance(tiers, list) or not tiers or any(str(tier).upper() not in {"A", "B", "C"} for tier in tiers):
                    raise ValueError("tiers must be a list containing A, B, or C")
                service = self._service()
                result = _call(
                    service.create_worklist,
                    name=name,
                    description=str(payload.get("description") or "").strip(),
                    limit=_integer(payload.get("limit"), "limit", 25, minimum=1, maximum=25),
                    tiers=[str(tier).upper() for tier in tiers],
                    actor=actor,
                )
                status = HTTPStatus.CREATED
            elif path == "/api/outreach/action":
                action = str(payload.get("action") or "").strip()
                if not action:
                    raise ValueError("Choose an outreach action")
                if action not in KNOWN_ACTIONS:
                    raise ValueError(f"Unknown outreach action: {action}")
                nested = payload.get("payload")
                if nested is not None and not isinstance(nested, Mapping):
                    raise ValueError("payload must be a JSON object")
                action_payload = dict(nested or {})
                # The local UI uses a convenient flat shape.  Nested values win
                # if both forms are present, and actor/action are never forwarded.
                action_payload = {
                    **{key: value for key, value in payload.items() if key not in {"action", "actor", "payload"}},
                    **action_payload,
                }
                service = self._service()
                result = _call(
                    service.perform_action,
                    action=action,
                    payload=action_payload,
                    actor=actor,
                )
                status = HTTPStatus.OK
            elif path == "/api/outreach/export":
                _reject_unknown(payload, POST_FIELDS[path], "export")
                worklist = str(
                    payload.get("worklist")
                    or payload.get("worklist_id")
                    or payload.get("worklist_name")
                    or "founder-25"
                ).strip()
                export_format = str(payload.get("format") or "csv").strip().lower()
                if export_format != "csv":
                    raise ValueError("Only CSV outreach exports are supported")
                ready_only = _boolean(payload.get("ready_only"), "ready_only", True)
                if not ready_only:
                    raise ValueError("Outreach exports must remain readiness-gated")
                service = self._service()
                outreach_purpose = str(payload.get("outreach_purpose") or "evidence_delivery").strip().lower()
                if outreach_purpose not in {"routing", "evidence_delivery"}:
                    raise ValueError("outreach_purpose must be routing or evidence_delivery")
                result = _call(
                    service.export_ready,
                    worklist=worklist,
                    worklist_id=worklist,
                    worklist_name=worklist,
                    format=export_format,
                    ready_only=ready_only,
                    outreach_purpose=outreach_purpose,
                    branch_id=str(payload.get("branch_id") or "").strip() or None,
                    treatment_slug=str(payload.get("treatment_slug") or "").strip() or None,
                    dry_run=_boolean(payload.get("dry_run"), "dry_run", False),
                    actor=actor,
                )
                status = HTTPStatus.OK
            converted = _jsonable(result)
            return ApiResponse(
                converted if isinstance(converted, dict) else {"result": converted},
                status,
            )
        except Exception as exc:  # Business errors are translated without leaking traces.
            return _error_response(exc)
