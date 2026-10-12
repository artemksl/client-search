"""Supplied city directories, immutable research evidence and shared account CRM.

This service has no execution capability. Collection is injected by the web
composition through the public runner; all transactions are short and local.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .client_research_store import ClientResearchStore, canonical, fingerprint, new_id, utc
from .db import ConflictError, NotFoundError
from .models import normalize_destination
from .supplied_directory import parse_supplied_clinic_directory
from .client_research_assessment import DEFAULT_DIRECT_SOURCE_TEMPLATE


ROUTINE_PROFILE_IDS = (
    "ortemksl_personal", "ortemksl_finance", "sokr4r3", "kysil_work",
    "loggedin_1", "loggedin_2", "loggedin_3",
)
SOURCE_TEMPLATE = DEFAULT_DIRECT_SOURCE_TEMPLATE
RECOMMENDATION_TEMPLATE = ('Which clinics in {area}, {city}, {country_name} would you '
    'recommend for {treatment_label}? Please list the clinics and briefly explain your choices.')
ACTIVE_ROUND_STATES = ("queued", "running", "waiting_attention")
TERMINAL_ROUND_STATES = ("succeeded", "partial", "failed", "cancelled", "skipped")


def _text(value, field, *, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be a nonempty string of at most {maximum} characters")
    return value.strip()


def _version(value, name="expected_workspace_version"):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _area_id(area):
    return "area_" + hashlib.sha256(area.encode()).hexdigest()[:20]


def _row(connection, table, key, value):
    # Table/key are fixed internal call-site literals, never HTTP inputs.
    result = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    if result is None:
        raise NotFoundError(f"{table.removeprefix('client_search_')}: {value} not found")
    return dict(result)


def _receipt(connection, scope, scope_id, request_id, payload):
    _text(request_id, "request_id", maximum=128)
    row = connection.execute("SELECT * FROM client_search_request_receipts WHERE scope_kind=? AND scope_id=? AND request_id=?", (scope, scope_id, request_id)).fetchone()
    if row:
        if row["payload_hash"] != fingerprint(payload):
            raise ConflictError("request_id is already bound to different content")
        return json.loads(row["result_json"])
    return None


def _save_receipt(connection, scope, scope_id, request_id, payload, result, now):
    connection.execute("INSERT INTO client_search_request_receipts VALUES(?,?,?,?,?,?)", (scope, scope_id, request_id, fingerprint(payload), canonical(result), now))


class ClientResearchService:
    def __init__(self, root: Path | str, db_path=None, *, clock=None):
        self.root = Path(root).resolve()
        self.store = ClientResearchStore(self.root, db_path)
        self.db_path = self.store.path
        self.clock = clock or utc

    def _now(self):
        return utc(self.clock())

    def create_workspace(self, *, city, country_code, country_name, language="English", timezone="UTC", name=None):
        city = _text(city, "city")
        if not isinstance(country_code, str) or not re.fullmatch(r"[A-Z]{2}", country_code):
            raise ValueError("country_code must contain two uppercase letters")
        country_name = _text(country_name, "country_name")
        language = _text(language, "language", maximum=128)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise ValueError("timezone must be a valid IANA timezone") from None
        identifier, now = new_id("city"), self._now().isoformat()
        with self.store.session(write=True) as c:
            c.execute("INSERT INTO client_search_workspaces(id,name,city,country_code,country_name,language,timezone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (identifier, _text(name or city, "name"), city, country_code, country_name, language, timezone, now, now))
        return self.get_workspace(identifier)

    def list_workspaces(self):
        with self.store.session() as c:
            return [self._workspace(c, row["id"]) for row in c.execute("SELECT id FROM client_search_workspaces ORDER BY name,id")]

    def _directory(self, c, workspace):
        if not workspace["directory_revision"]:
            return {"country_code": workspace["country_code"], "city": workspace["city"], "accounts": [], "branches": []}
        return json.loads(_row(c, "client_search_directory_revisions", "id", workspace["directory_revision"])["snapshot_json"])

    def _configuration(self, c, workspace, directory):
        if workspace["config_revision"]:
            config = json.loads(_row(c, "client_search_config_revisions", "id", workspace["config_revision"])["config_json"])
        else:
            config = {"treatments": {}, "cells": [], "source_provider": "chatgpt", "judge_provider": "gemini", "providers": ["chatgpt", "gemini", "copilot"], "profile_ids": list(ROUTINE_PROFILE_IDS), "source_template": SOURCE_TEMPLATE if workspace["language"].casefold() in {"en", "english"} else "", "recommendation_template": RECOMMENDATION_TEMPLATE if workspace["language"].casefold() in {"en", "english"} else "", "direct_freshness_days": 30, "recommendation_freshness_hours": 144}
        available = {(b["area"], t) for b in directory["branches"] for t in b["treatment_ids"]}
        selected = config["cells"] if workspace["config_revision"] else [{"area": a, "treatment_id": t} for a, t in sorted(available)]
        cells = []
        for cell in selected:
            if (cell["area"], cell["treatment_id"]) not in available:
                continue
            area, treatment = cell["area"], cell["treatment_id"]
            label = config["treatments"].get(treatment, "")
            cell = {"area_id": _area_id(area), "area": area, "treatment_id": treatment, "treatment_label": label}
            cell["cell_id"] = "cell_" + fingerprint([workspace["country_code"], workspace["city"], area, treatment])[:24]
            query = {**cell, "country_code": workspace["country_code"], "city": workspace["city"], "country_name": workspace["country_name"], "language": workspace["language"], "template": config["recommendation_template"], "providers": config["providers"], "profiles": config["profile_ids"], "extraction": "client_search.recommendation_extraction.v1"}
            cell["query_fingerprint"] = fingerprint(query)
            cells.append(cell)
        config["cells"] = cells
        config["required_samples"] = [{"provider": provider, "profile_id": profile} for provider in config["providers"] for profile in config["profile_ids"]]
        if directory["branches"]:
            from .client_research_assessment import build_matching_context, direct_branch_context
            context = build_matching_context(directory)
            config["direct_fingerprints"] = {b["branch_id"]: direct_branch_context(b["branch_id"], context, {**config, "language": workspace["language"]}, workspace["id"], workspace["city"], workspace["country_name"])["direct_fingerprint"] for b in directory["branches"]}
        return config

    def _workspace(self, c, identifier):
        workspace = _row(c, "client_search_workspaces", "id", identifier)
        directory = self._directory(c, workspace)
        workspace["configuration"] = self._configuration(c, workspace, directory)
        workspace["areas"] = sorted({b["area"] for b in directory["branches"]})
        workspace["treatment_ids"] = sorted({t for b in directory["branches"] for t in b["treatment_ids"]})
        workspace["available_cells"] = [{"area_id": _area_id(a), "area": a, "treatment_id": t, "treatment_label": workspace["configuration"]["treatments"].get(t, "")} for a, t in sorted({(b["area"], t) for b in directory["branches"] for t in b["treatment_ids"]})]
        workspace["account_count"], workspace["branch_count"] = len(directory["accounts"]), len(directory["branches"])
        schedule = c.execute("SELECT * FROM client_search_schedules WHERE workspace_id=?", (identifier,)).fetchone()
        workspace["schedule"] = {**dict(schedule), "grant": json.loads(schedule["grant_json"])} if schedule else None
        workspace["rounds"] = [self._round(c, r["id"]) for r in c.execute("SELECT id FROM client_search_rounds WHERE workspace_id=? ORDER BY rowid DESC LIMIT 20", (identifier,))]
        return workspace

    def get_workspace(self, identifier):
        with self.store.session() as c:
            return self._workspace(c, identifier)

    def directory(self, identifier):
        with self.store.session() as c:
            return self._directory(c, _row(c, "client_search_workspaces", "id", identifier))

    def preview_import(self, identifier, *, file_name, content_base64, base_directory_revision=None, account_mappings=None):
        _text(file_name, "file_name", maximum=255)
        if not isinstance(content_base64, str) or len(content_base64) > 14 * 1024 * 1024:
            raise ValueError("CSV upload exceeds 10 MiB")
        try:
            raw = base64.b64decode(content_base64, validate=True)
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError("CSV upload exceeds 10 MiB")
            parsed = parse_supplied_clinic_directory(raw.decode("utf-8-sig"))
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError("Upload must be a base64-encoded UTF-8 CSV") from exc
        snapshot = {"country_code": parsed.country_code, "city": parsed.city, "accounts": [asdict(a) for a in parsed.accounts], "branches": [asdict(b) for b in parsed.branches]}
        mappings = account_mappings or {}
        if not isinstance(mappings, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mappings.items()):
            raise ValueError("account_mappings must map supplied IDs to internal account IDs")
        now = self._now()
        with self.store.session(write=True) as c:
            workspace = _row(c, "client_search_workspaces", "id", identifier)
            if workspace["directory_revision"] != base_directory_revision:
                raise ConflictError("Directory changed; preview the file again")
            if (workspace["city"], workspace["country_code"]) != (parsed.city, parsed.country_code):
                raise ValueError("Directory city and country must match the workspace")
            self._validate_identity(c, identifier, snapshot, mappings)
            old = self._directory(c, workspace)
            changes = {}
            for kind, key, table in (("accounts", "account_id", "client_search_accounts"), ("branches", "branch_id", "client_search_branches")):
                old_by = {r[key]: r for r in old[kind]}
                new_by = {r[key]: r for r in snapshot[kind]}
                historical = {r[0] for r in c.execute(f"SELECT supplied_id FROM {table} WHERE workspace_id=?", (identifier,))}
                changes[kind] = {"added": sorted(set(new_by) - historical), "reactivated": sorted((set(new_by) & historical) - set(old_by)), "retired": sorted(set(old_by) - set(new_by)), "changed": sorted(k for k in old_by.keys() & new_by.keys() if fingerprint(old_by[k]) != fingerprint(new_by[k])), "unchanged": sorted(k for k in old_by.keys() & new_by.keys() if fingerprint(old_by[k]) == fingerprint(new_by[k]))}
            payload = {"file_name": file_name, "content_base64": content_base64, "raw_hash": hashlib.sha256(raw).hexdigest(), "snapshot": snapshot, "normalized_hash": fingerprint(snapshot), "base_directory_revision": base_directory_revision, "account_mappings": mappings, "diff": changes, "duplicate_rows": parsed.duplicate_rows}
            preview = self._save_preview(c, identifier, "import", workspace["version"], payload, now)
            return {**preview, "diff": changes, "duplicate_rows": parsed.duplicate_rows, "account_count": len(parsed.accounts), "branch_count": len(parsed.branches), "account_mappings": mappings, "normalized_hash": payload["normalized_hash"], "workspace_version": workspace["version"]}

    def _validate_identity(self, c, identifier, snapshot, mappings):
        supplied = {a["account_id"] for a in snapshot["accounts"]}
        if set(mappings) - supplied:
            raise ValueError("Mapping contains an account absent from the file")
        used = {}
        for account in snapshot["accounts"]:
            key = account["account_id"]
            old = c.execute("SELECT internal_account_id FROM client_search_accounts WHERE workspace_id=? AND supplied_id=?", (identifier, key)).fetchone()
            internal = mappings.get(key) or (old[0] if old else None)
            if old and internal != old[0]:
                raise ConflictError("An existing supplied account cannot be remapped")
            if internal:
                _row(c, "sales_accounts", "id", internal)
                existing = c.execute("SELECT supplied_id FROM client_search_accounts WHERE workspace_id=? AND internal_account_id=?", (identifier, internal)).fetchone()
                if internal in used or (existing and existing[0] != key):
                    raise ConflictError("Two supplied accounts in one workspace cannot share an internal buyer")
                used[internal] = key
        for branch in snapshot["branches"]:
            old = c.execute("SELECT account_id FROM client_search_branches WHERE workspace_id=? AND supplied_id=?", (identifier, branch["branch_id"])).fetchone()
            if old and old[0] != branch["account_id"]:
                raise ConflictError("A stable branch ID cannot change its buying account")

    def _save_preview(self, c, identifier, kind, version, payload, now):
        preview_id, digest = new_id("preview"), fingerprint(payload)
        expires = (now + timedelta(minutes=30)).isoformat()
        c.execute("INSERT INTO client_search_previews VALUES(?,?,?,?,?,?,?,?,NULL)", (preview_id, identifier, kind, version, canonical(payload), digest, now.isoformat(), expires))
        return {"preview_id": preview_id, "fingerprint": digest, "expires_at": expires, "workspace_version": version}

    def save_check_preview(self, identifier, manifest):
        with self.store.session(write=True) as c:
            workspace = _row(c, "client_search_workspaces", "id", identifier)
            if workspace["version"] != manifest["workspace_version"]:
                raise ConflictError("Workspace changed while planning")
            return self._save_preview(c, identifier, "checks", workspace["version"], manifest, self._now())

    def get_preview(self, identifier, preview_id, kind, *, allow_consumed=False):
        with self.store.session() as c:
            if not allow_consumed:
                return self._preview(c, identifier, preview_id, kind)
            row = _row(c, "client_search_previews", "id", preview_id)
            if row["workspace_id"] != identifier or row["kind"] != kind:
                raise NotFoundError("Preview does not belong to this workspace/action")
            row["payload"] = json.loads(row["payload_json"])
            return row

    def _preview(self, c, identifier, preview_id, kind):
        preview = _row(c, "client_search_previews", "id", preview_id)
        if preview["workspace_id"] != identifier or preview["kind"] != kind:
            raise NotFoundError("Preview does not belong to this workspace/action")
        if preview["consumed_at"] or utc(preview["expires_at"]) <= self._now():
            raise ConflictError("Preview consumed or expired; preview again")
        preview["payload"] = json.loads(preview["payload_json"])
        if fingerprint(preview["payload"]) != preview["fingerprint"]:
            raise ConflictError("Preview integrity check failed")
        return preview

    def commit_import(self, identifier, *, preview_id, expected_workspace_version, request_id, account_mappings=None):
        payload = {"preview_id": preview_id, "expected_workspace_version": expected_workspace_version, "account_mappings": account_mappings or {}}
        now = self._now().isoformat()
        with self.store.session(write=True) as c:
            cached = _receipt(c, "import", identifier, request_id, payload)
            if cached is not None:
                return cached
            workspace = _row(c, "client_search_workspaces", "id", identifier)
            preview = self._preview(c, identifier, preview_id, "import")
            if workspace["version"] != _version(expected_workspace_version) or preview["version"] != workspace["version"]:
                raise ConflictError("Workspace changed; preview again")
            p = preview["payload"]
            if payload["account_mappings"] != p["account_mappings"]:
                raise ConflictError("Mapping choices changed; preview again")
            snapshot = p["snapshot"]
            self._validate_identity(c, identifier, snapshot, p["account_mappings"])
            old_revision = _row(c, "client_search_directory_revisions", "id", workspace["directory_revision"]) if workspace["directory_revision"] else None
            if old_revision and old_revision["normalized_hash"] == p["normalized_hash"]:
                result = {"directory_revision": old_revision["id"], "version": workspace["version"], "unchanged": True, "diff": p["diff"]}
            else:
                from .client_research_assessment import build_matching_context, branch_identity_fingerprint, branch_matching_fingerprint
                revision = new_id("directory")
                c.execute("INSERT INTO client_search_directory_revisions VALUES(?,?,?,?,?,?,?,?)", (revision, identifier, p["file_name"], base64.b64decode(p["content_base64"]), p["raw_hash"], p["normalized_hash"], canonical(snapshot), now))
                context = build_matching_context(snapshot)
                accounts = {a["account_id"]: a for a in snapshot["accounts"]}
                c.execute("UPDATE client_search_accounts SET active=0,updated_at=? WHERE workspace_id=?", (now, identifier))
                c.execute("UPDATE client_search_branches SET active=0,updated_at=? WHERE workspace_id=?", (now, identifier))
                for account in snapshot["accounts"]:
                    existing = c.execute("SELECT internal_account_id FROM client_search_accounts WHERE workspace_id=? AND supplied_id=?", (identifier, account["account_id"])).fetchone()
                    internal = existing[0] if existing else p["account_mappings"].get(account["account_id"])
                    if internal is None:
                        internal = new_id("account")
                        c.execute("INSERT INTO sales_accounts(id,created_at,updated_at) VALUES(?,?,?)", (internal, now, now))
                    c.execute("INSERT INTO client_search_accounts VALUES(?,?,?,?,1,?,?) ON CONFLICT(workspace_id,supplied_id) DO UPDATE SET data_json=excluded.data_json,active=1,updated_at=excluded.updated_at", (identifier, account["account_id"], internal, canonical(account), now, now))
                for branch in snapshot["branches"]:
                    c.execute("INSERT INTO client_search_branches VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(workspace_id,supplied_id) DO UPDATE SET data_json=excluded.data_json,identity_fingerprint=excluded.identity_fingerprint,matching_fingerprint=excluded.matching_fingerprint,active=1,updated_at=excluded.updated_at", (identifier, branch["branch_id"], branch["account_id"], canonical(branch), branch_identity_fingerprint(branch, accounts[branch["account_id"]]), branch_matching_fingerprint(branch, context), now, now))
                old_config = self._configuration(c, workspace, self._directory(c, workspace))
                c.execute("UPDATE client_search_workspaces SET directory_revision=?,version=version+1,updated_at=? WHERE id=?", (revision, now, identifier))
                new_workspace = {**workspace, "directory_revision": revision}
                new_config = self._configuration(c, new_workspace, snapshot)
                if fingerprint(old_config["cells"]) != fingerprint(new_config["cells"]):
                    self._pause_schedule(c, identifier, "Directory changed the selected query scope", now)
                result = {"directory_revision": revision, "version": workspace["version"] + 1, "unchanged": False, "diff": p["diff"]}
            c.execute("UPDATE client_search_previews SET consumed_at=? WHERE id=?", (now, preview_id))
            _save_receipt(c, "import", identifier, request_id, payload, result, now)
        return result

    def configure(self, identifier, *, configuration, expected_workspace_version):
        if not isinstance(configuration, dict):
            raise ValueError("configuration must be an object")
        allowed = {"treatments", "cells", "source_provider", "judge_provider", "providers", "profile_ids", "source_template", "recommendation_template", "direct_freshness_days", "recommendation_freshness_hours"}
        if set(configuration) - allowed:
            raise ValueError("Unknown configuration fields: " + ", ".join(sorted(set(configuration) - allowed)))
        now = self._now().isoformat()
        with self.store.session(write=True) as c:
            w = _row(c, "client_search_workspaces", "id", identifier)
            if w["version"] != _version(expected_workspace_version):
                raise ConflictError("Workspace changed; reload configuration")
            directory = self._directory(c, w)
            config = self._configuration(c, w, directory)
            config.pop("required_samples", None)
            config.pop("direct_fingerprints", None)
            config.update(configuration)
            if config["source_provider"] not in {"chatgpt", "gemini", "copilot"} or config["judge_provider"] not in {"chatgpt", "gemini", "copilot"} or config["source_provider"] == config["judge_provider"]:
                raise ValueError("Source and judge must be different supported providers")
            if config["providers"] != ["chatgpt", "gemini", "copilot"] or config["profile_ids"] != list(ROUTINE_PROFILE_IDS):
                raise ValueError("Use the three recommendation providers and seven explicit routine profiles")
            if not isinstance(config["treatments"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in config["treatments"].items()):
                raise ValueError("treatments must map stable IDs to localised labels")
            available = {(b["area"], t) for b in directory["branches"] for t in b["treatment_ids"]}
            if not isinstance(config["cells"], list):
                raise ValueError("cells must be an array")
            selected = []
            for cell in config["cells"]:
                if not isinstance(cell, dict) or set(cell) - {"cell_id", "area_id", "area", "treatment_id", "treatment_label", "query_fingerprint"}:
                    raise ValueError("Invalid query cell")
                key = (cell.get("area"), cell.get("treatment_id"))
                if key not in available:
                    raise ValueError("Selected cell is absent from the supplied directory")
                _text(config["treatments"].get(key[1]), f"treatment wording for {key[1]}")
                selected.append({"area": key[0], "treatment_id": key[1]})
            config["cells"] = sorted({canonical(cell): cell for cell in selected}.values(), key=lambda cell: (cell["area"], cell["treatment_id"]))
            for key in ("source_template", "recommendation_template"):
                _text(config[key], key, maximum=16000)
            # Formatting is checked here; runner also freezes and validates exact bytes.
            source_allowed = {"branch_name", "account_name", "account_phrase", "area", "city", "country_name"}
            generic_allowed = {"area", "city", "country_name", "treatment_label"}
            import string
            for key, placeholders in (("source_template", source_allowed), ("recommendation_template", generic_allowed)):
                fields = [field for _, field, spec, conversion in string.Formatter().parse(config[key]) if field is not None]
                if any(field not in placeholders for field in fields):
                    raise ValueError(f"{key} contains an unsupported placeholder")
                config[key].format(**{field: "value" for field in placeholders})
            for key, maximum in (("direct_freshness_days", 365), ("recommendation_freshness_hours", 720)):
                if type(config[key]) is not int or not 1 <= config[key] <= maximum:
                    raise ValueError(f"Invalid {key}")
            digest = fingerprint(config)
            old = _row(c, "client_search_config_revisions", "id", w["config_revision"]) if w["config_revision"] else None
            if old and old["fingerprint"] == digest:
                return {"config_revision": old["id"], "version": w["version"], "unchanged": True, "configuration": self._configuration(c, w, directory)}
            revision = new_id("config")
            c.execute("INSERT INTO client_search_config_revisions VALUES(?,?,?,?,?)", (revision, identifier, canonical(config), digest, now))
            c.execute("UPDATE client_search_workspaces SET config_revision=?,version=version+1,updated_at=? WHERE id=?", (revision, now, identifier))
            self._pause_schedule(c, identifier, "Configuration changed; review the schedule grant", now)
        updated = self.get_workspace(identifier)
        return {"config_revision": revision, "version": updated["version"], "configuration": updated["configuration"], "unchanged": False}

    def _pause_schedule(self, c, identifier, reason, now):
        c.execute("UPDATE client_search_schedules SET enabled=0,pause_reason=?,updated_at=? WHERE workspace_id=? AND enabled=1", (reason, now, identifier))
        c.execute("UPDATE client_search_rounds SET state='skipped',updated_at=?,finished_at=? WHERE workspace_id=? AND schedule_revision IS NOT NULL AND state='queued' AND NOT EXISTS(SELECT 1 FROM client_search_run_links l WHERE l.round_id=client_search_rounds.id AND l.cursor!='reserved')", (now, now, identifier))

    def _round(self, c, identifier):
        row = _row(c, "client_search_rounds", "id", identifier)
        row["admission_sequence"] = c.execute(
            "SELECT rowid FROM client_search_rounds WHERE id=?", (identifier,)
        ).fetchone()[0]
        row["manifest"] = json.loads(row.pop("manifest_json"))
        row["runs"] = []
        for link in c.execute("SELECT * FROM client_search_run_links WHERE round_id=? ORDER BY CASE kind WHEN 'identification' THEN 0 ELSE 1 END", (identifier,)):
            item = dict(link)
            item["manifest"] = json.loads(item.pop("manifest_json"))
            if fingerprint(item["manifest"]) != item["manifest_hash"]:
                raise ConflictError("Frozen research run link was changed")
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            row["runs"].append(item)
        row["current_state"] = row["state"]
        if row["state"] in TERMINAL_ROUND_STATES and row["state"] != "skipped":
            results = [r["result"] for r in row["runs"] if r["result"]]
            statuses = {r.get("status") for r in results}
            if statuses & {"planned", "queued", "running"}:
                row["current_state"] = "running"
            elif statuses & {"needs_attention", "interrupted"}:
                row["current_state"] = "waiting_attention"
            elif statuses & {"failed", "partial", "cancelled"}:
                row["current_state"] = "partial" if any(r.get("accepted_result_ids") for r in results) else "failed"
            elif len(results) == len(row["runs"]) and statuses == {"succeeded"}:
                row["current_state"] = "succeeded"
        return row

    def get_round(self, identifier):
        with self.store.session() as c:
            return self._round(c, identifier)

    def reserve_round(self, identifier, *, manifest, request_id, preview_id=None, expected_workspace_version=None, expected_fingerprint=None, schedule_revision=None, slot=None):
        now = self._now().isoformat()
        payload = {"preview_id": preview_id, "expected_workspace_version": expected_workspace_version, "expected_fingerprint": expected_fingerprint, "manifest": manifest, "schedule_revision": schedule_revision, "slot": slot}
        with self.store.session(write=True) as c:
            cached = _receipt(c, "start", identifier, request_id, payload)
            if cached is not None:
                return self._round(c, cached["round_id"])
            w = _row(c, "client_search_workspaces", "id", identifier)
            if w["version"] != manifest["workspace_version"]:
                raise ConflictError("Workspace changed; preview checks again")
            if preview_id:
                preview = self._preview(c, identifier, preview_id, "checks")
                if preview["payload"] != manifest or preview["version"] != _version(expected_workspace_version) or preview["fingerprint"] != expected_fingerprint:
                    raise ConflictError("Check preview/settings do not match")
            if schedule_revision:
                schedule = _row(c, "client_search_schedules", "workspace_id", identifier)
                if not schedule["enabled"] or schedule["revision"] != schedule_revision or schedule["execution_fingerprint"] != manifest["execution_fingerprint"]:
                    raise ConflictError("Schedule grant changed or paused")
            active = c.execute("SELECT id FROM client_search_rounds WHERE workspace_id=? AND state IN ('queued','running','waiting_attention')", (identifier,)).fetchone()
            if active:
                raise ConflictError(f"Workspace already has active round {active[0]}")
            round_id = new_id("round")
            c.execute("INSERT INTO client_search_rounds VALUES(?,?,?,?,?,?,?,?,?,?,NULL)", (round_id, identifier, manifest["mode"], "queued", request_id, schedule_revision, slot, canonical(manifest), now, now))
            for child in manifest["children"]:
                kind = child["kind"]
                runner_run_id = child["runner_run_id"]
                child_request = "cr_" + fingerprint([request_id, kind])[:40]
                frozen = {**child, "runner_run_id": runner_run_id, "request_id": child_request, "round_id": round_id, "workspace_id": identifier, "schedule_revision": schedule_revision, "slot": slot}
                c.execute("INSERT INTO client_search_run_links VALUES(?,?,?,?,?,'reserved',?,?,NULL,NULL)", (round_id, kind, runner_run_id, child_request, None, canonical(frozen), fingerprint(frozen)))
            if preview_id:
                c.execute("UPDATE client_search_previews SET consumed_at=? WHERE id=?", (now, preview_id))
            _save_receipt(c, "start", identifier, request_id, payload, {"round_id": round_id}, now)
            return self._round(c, round_id)

    def update_run_link(self, round_id, kind, *, cursor, job_id=None, result=None, error=None):
        if cursor not in {"reserved", "dispatch_requested", "attached", "terminal"}:
            raise ValueError("Invalid dispatch cursor")
        with self.store.session(write=True) as c:
            old = c.execute("SELECT * FROM client_search_run_links WHERE round_id=? AND kind=?", (round_id, kind)).fetchone()
            if not old:
                raise NotFoundError("Run link not found")
            order = {"reserved": 0, "dispatch_requested": 1, "attached": 2, "terminal": 3}
            if order[cursor] < order[old["cursor"]] or (old["job_id"] and job_id and old["job_id"] != job_id):
                raise ConflictError("Run link cannot be rewound or rebound")
            c.execute("UPDATE client_search_run_links SET cursor=?,job_id=COALESCE(job_id,?),result_json=COALESCE(?,result_json),error=? WHERE round_id=? AND kind=?", (cursor, job_id, canonical(result) if result is not None else None, error, round_id, kind))

    def set_round_state(self, identifier, state):
        if state not in ACTIVE_ROUND_STATES + TERMINAL_ROUND_STATES:
            raise ValueError("Invalid round state")
        now = self._now().isoformat()
        with self.store.session(write=True) as c:
            old = _row(c, "client_search_rounds", "id", identifier)
            if state == "skipped" and c.execute("SELECT 1 FROM client_search_run_links WHERE round_id=? AND cursor!='reserved'", (identifier,)).fetchone():
                raise ConflictError("Admitted work cannot be skipped")
            if old["state"] in TERMINAL_ROUND_STATES and state != old["state"]:
                raise ConflictError("Terminal round cannot change state")
            c.execute("UPDATE client_search_rounds SET state=?,updated_at=?,finished_at=? WHERE id=?", (state, now, now if state in TERMINAL_ROUND_STATES else None, identifier))

    def active_rounds(self):
        with self.store.session() as c:
            return [self._round(c, row[0]) for row in c.execute("SELECT id FROM client_search_rounds WHERE state IN ('queued','running','waiting_attention') ORDER BY created_at,id")]

    def round_for_run(self, run_id):
        with self.store.session() as c:
            row = c.execute("SELECT round_id FROM client_search_run_links WHERE runner_run_id=?", (run_id,)).fetchone()
            return self._round(c, row[0]) if row else None

    def round_evidence_count(self, round_id):
        with self.store.session() as c:
            return c.execute("SELECT COUNT(*) FROM client_search_evidence WHERE round_id=?", (round_id,)).fetchone()[0]

    def ingest_evidence(self, round_id, evidence):
        with self.store.session(write=True) as c:
            round_ = self._round(c, round_id)
            run_links = {r["runner_run_id"]: r for r in round_["runs"]}
            for item in evidence:
                if item["runner_run_id"] not in run_links:
                    raise ConflictError("Evidence belongs to another run")
                link = run_links[item["runner_run_id"]]
                metadata = link["manifest"]["measurement"]["client_research"]
                if (item.get("kind") != link["kind"] or item.get("round_id") != round_id
                        or any(item.get(k) != metadata[k] for k in ("workspace_id", "directory_revision", "config_revision"))):
                    raise ConflictError("Evidence differs from its frozen research scope")
                planned = link["manifest"]["plan"]["observation_plan"]["observations"]
                target = "branch_id" if link["kind"] == "identification" else "cell_id"
                if not any(o.get(target) == item.get(target) and o["platform"] == item["provider"]
                           and o["profile_id"] == item["profile_id"] for o in planned):
                    raise ConflictError("Evidence is not a planned provider/profile/target sample")
                result_id = _text(item["runner_result_id"], "runner_result_id")
                old = c.execute("SELECT data_json FROM client_search_evidence WHERE runner_result_id=?", (result_id,)).fetchone()
                if old:
                    stored = json.loads(old[0]); stored.pop("id", None)
                    if fingerprint(stored) != fingerprint(item):
                        raise ConflictError("Runner result changed on repeated ingestion")
                    continue
                identifier = new_id("evidence")
                stored = {**item, "id": identifier}
                c.execute("INSERT INTO client_search_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, round_["workspace_id"], round_id, item["kind"], result_id, item["runner_run_id"], item.get("branch_id"), item.get("cell_id"), item["provider"], item["profile_id"], item["collected_at"], canonical(stored)))

    def get_evidence(self, identifier):
        with self.store.session() as c:
            row = _row(c, "client_search_evidence", "id", identifier)
            result = json.loads(row["data_json"])
            result["reviews"] = [dict(r) for r in c.execute("SELECT * FROM client_search_review_annotations WHERE evidence_id=? ORDER BY created_at,id", (identifier,))]
            return result

    def review_evidence(self, identifier, *, reason, actor="user"):
        reason = _text(reason, "reason", maximum=4000)
        review_id, now = new_id("review"), self._now().isoformat()
        with self.store.session(write=True) as c:
            _row(c, "client_search_evidence", "id", identifier)
            c.execute("INSERT INTO client_search_review_annotations VALUES(?,?,?,?,?)", (review_id, identifier, reason, _text(actor, "actor"), now))
        return {"id": review_id, "evidence_id": identifier, "reason": reason, "actor": actor, "created_at": now}

    def _projection(self, c, identifier):
        from .client_research_assessment import project_client_research
        w = _row(c, "client_search_workspaces", "id", identifier)
        directory = self._directory(c, w)
        rounds = [self._round(c, r[0]) for r in c.execute("SELECT id FROM client_search_rounds WHERE workspace_id=? ORDER BY rowid", (identifier,))]
        evidence = []
        for row in c.execute("SELECT e.*,EXISTS(SELECT 1 FROM client_search_review_annotations a WHERE a.evidence_id=e.id) AS reviewed FROM client_search_evidence e WHERE e.workspace_id=?", (identifier,)):
            evidence.append({**json.loads(row["data_json"]), "round_id": row["round_id"], "reviewed": bool(row["reviewed"])})
        return project_client_research(directory, self._configuration(c, w, directory), evidence, rounds, self._now())

    def _account(self, c, identifier, supplied_id, projection):
        row = c.execute("SELECT * FROM client_search_accounts WHERE workspace_id=? AND supplied_id=?", (identifier, supplied_id)).fetchone()
        if not row:
            raise NotFoundError("Supplied account not found")
        account = json.loads(row["data_json"])
        sales = _row(c, "sales_accounts", "id", row["internal_account_id"])
        account.update(next((a for a in projection["accounts"] if a["account_id"] == supplied_id), {}))
        account.update({"supplied_id": supplied_id, "internal_account_id": sales["id"], "version": sales["version"], "active": bool(row["active"]), "account_name": sales["display_name_override"] or account["account_name"]})
        raw_branches = {r["supplied_id"]: json.loads(r["data_json"]) for r in c.execute("SELECT * FROM client_search_branches WHERE workspace_id=? AND account_id=? AND active=1", (identifier, supplied_id))}
        account["branches"] = [{**raw_branches[b["branch_id"]], **b} for b in projection["branches"] if b["branch_id"] in raw_branches]
        account["contacts"] = [dict(r) for r in c.execute("SELECT * FROM contact_points WHERE account_id=? ORDER BY created_at,id", (sales["id"],))]
        account["interactions"] = [dict(r) for r in c.execute("SELECT * FROM interactions WHERE account_id=? ORDER BY occurred_at DESC,id DESC", (sales["id"],))]
        suppressed = c.execute("SELECT 1 FROM suppressions WHERE account_id=? AND scope_type='account' AND branch_id IS NULL AND active=1", (sales["id"],)).fetchone()
        contact_events = [r for r in account["interactions"] if r["direction"] in {"outbound", "inbound"}]
        reply = any(r["direction"] == "inbound" for r in contact_events)
        if suppressed:
            status = "suppressed"
        elif sales["status"] in {"won", "lost", "disqualified"} or sales["stage"] in {"won", "lost"}:
            status = "closed"
        elif sales["status"] in {"nurture", "archived"} or sales["stage"] == "nurture":
            status = "held"
        elif reply or sales["stage"] in {"replied", "positive", "meeting", "qualified", "proposal"}:
            status = "replied"
        elif contact_events or sales["stage"] == "contacted":
            status = "contacted"
        else:
            status = "uncontacted"
        account["contact_status"] = status
        account["last_contact_at"] = contact_events[0]["occurred_at"] if contact_events else None
        account["contact_date_label"] = "Contact date not recorded" if status in {"contacted", "replied"} and not contact_events else ""
        return account

    def list_accounts(self, identifier, *, q="", priority="", area="", treatment="", evidence_state="", contact_state="", state="", contact_status="", view="queue", limit=100, offset=0):
        evidence_state = state or evidence_state
        contact_state = contact_status or contact_state
        if view not in {"queue", "all"} or type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid account pagination/view")
        with self.store.session() as c:
            projection = self._projection(c, identifier)
            accounts = [self._account(c, identifier, r[0], projection) for r in c.execute("SELECT supplied_id FROM client_search_accounts WHERE workspace_id=? AND active=1", (identifier,))]
        def matches(a):
            return (not q or q.casefold() in canonical(a).casefold()) and (not priority or a.get("priority") == priority) and (not evidence_state or a.get("state") == evidence_state) and (not contact_state or a["contact_status"] == contact_state) and (view == "all" or a["contact_status"] == "uncontacted") and (not area or any(b["area"] == area for b in a["branches"])) and (not treatment or any(treatment in b["treatment_ids"] for b in a["branches"]))
        accounts = sorted(filter(matches, accounts), key=lambda a: ({"P1": 0, "P2": 1, "P3": 2}.get(a.get("priority"), 3), a["account_name"].casefold(), a["internal_account_id"]))
        return {"items": accounts[offset:offset+limit], "total": len(accounts), "limit": limit, "offset": offset}

    def get_account(self, identifier, supplied_id):
        with self.store.session() as c:
            return self._account(c, identifier, supplied_id, self._projection(c, identifier))

    def account_action(self, identifier, supplied_id, *, action, expected_account_version, request_id, branch_id=None, actor="user", **fields):
        allowed = {"save_contact", "add_note", "log_external_contact", "log_external_reply", "suppress", "unsuppress"}
        if action not in allowed:
            raise ValueError("Unsupported account action")
        allowed_fields = {"save_contact": {"channel", "value", "notes", "contact_id", "expected_contact_version"}, "add_note": {"notes"}, "log_external_contact": {"notes", "channel", "occurred_at"}, "log_external_reply": {"notes", "channel", "occurred_at"}, "suppress": {"reason"}, "unsuppress": {"reason"}}[action]
        if set(fields) - allowed_fields:
            raise ValueError("Unsupported action fields")
        payload = {"workspace_id": identifier, "supplied_id": supplied_id, "action": action, "expected_account_version": expected_account_version, "branch_id": branch_id, "actor": actor, **fields}
        now = self._now().isoformat()
        with self.store.session(write=True) as c:
            row = c.execute("SELECT * FROM client_search_accounts WHERE workspace_id=? AND supplied_id=?", (identifier, supplied_id)).fetchone()
            if not row:
                raise NotFoundError("Supplied account not found")
            internal = row["internal_account_id"]
            cached = _receipt(c, "account", internal, request_id, payload)
            if cached is not None:
                return cached
            sales = _row(c, "sales_accounts", "id", internal)
            if sales["version"] != _version(expected_account_version, "expected_account_version"):
                raise ConflictError("Account changed; reload before recording the action")
            if branch_id:
                branch = c.execute("SELECT account_id FROM client_search_branches WHERE workspace_id=? AND supplied_id=?", (identifier, branch_id)).fetchone()
                if not branch or branch[0] != supplied_id:
                    raise ValueError("Branch does not belong to this supplied account")
            if action == "save_contact":
                channel = fields.get("channel")
                if channel not in {"email", "phone", "sms", "whatsapp", "linkedin", "form", "other"}:
                    raise ValueError("Invalid contact channel")
                value = _text(fields.get("value"), "value", maximum=2000)
                if channel in {"phone", "sms", "whatsapp"}:
                    number = re.sub(r"[ ()\-.]", "", value)
                    if not re.fullmatch(r"\+[1-9][0-9]{1,14}", number):
                        raise ValueError("Phone routes require an explicit +country-code number")
                normalized = normalize_destination(channel, value)
                if channel == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
                    raise ValueError("Invalid email address")
                if not normalized:
                    raise ValueError("Contact route could not be normalized")
                existing = c.execute("SELECT * FROM contact_points WHERE account_id=? AND channel=? AND normalized_value=?", (internal, channel, normalized)).fetchone()
                if fields.get("contact_id"):
                    existing = _row(c, "contact_points", "id", fields["contact_id"])
                    if existing["account_id"] != internal or existing["branch_id"] is not None:
                        raise ValueError("Contact is outside this account scope")
                if existing:
                    if existing["branch_id"] is not None:
                        raise ValueError("Contact is outside this account scope")
                    if existing["version"] != _version(fields.get("expected_contact_version"), "expected_contact_version"):
                        raise ConflictError("Contact changed; reload its version")
                    item_id = existing["id"]
                    c.execute("UPDATE contact_points SET channel=?,value=?,normalized_value=?,verification_status='unverified',verified_at=NULL,expires_at=NULL,notes=?,version=version+1,updated_at=? WHERE id=?", (channel, value, normalized, fields.get("notes"), now, item_id))
                else:
                    item_id = new_id("contact")
                    c.execute("INSERT INTO contact_points(id,account_id,channel,value,normalized_value,route_scope,verification_status,notes,created_at,updated_at) VALUES(?,?,?,?,?,'organization','unverified',?,?,?)", (item_id, internal, channel, value, normalized, fields.get("notes"), now, now))
            elif action in {"suppress", "unsuppress"}:
                reason = _text(fields.get("reason"), "reason", maximum=4000)
                item_id = new_id("suppression")
                if action == "suppress":
                    c.execute("INSERT INTO suppressions(id,account_id,scope_type,reason,source,created_by,created_at) VALUES(?,?,'account',?,'client_research',?,?)", (item_id, internal, reason, actor, now))
                else:
                    c.execute("UPDATE suppressions SET active=0,lifted_by=?,lifted_at=?,lift_reason=? WHERE account_id=? AND scope_type='account' AND active=1 AND branch_id IS NULL", (actor, now, reason, internal))
            else:
                notes = _text(fields.get("notes"), "notes", maximum=10000)
                occurred = utc(fields.get("occurred_at") or now)
                if occurred > self._now():
                    raise ValueError("External activity must already have occurred")
                direction = {"add_note": "internal", "log_external_contact": "outbound", "log_external_reply": "inbound"}[action]
                from .interaction_records import insert_interaction_record
                item_id = new_id("interaction")
                insert_interaction_record(c, {"id": item_id, "account_id": internal, "interaction_type": action, "direction": direction, "channel": fields.get("channel"), "occurred_at": occurred.isoformat(), "notes": notes, "dedupe_key": "client_" + fingerprint([internal, request_id]), "payload_json": canonical({"historical": True, "actor": actor, "client_research": True}), "created_at": now, "outreach_purpose": "evidence_delivery"})
                w = _row(c, "client_search_workspaces", "id", identifier)
                reason = self._account(c, identifier, supplied_id, self._projection(c, identifier)).get("reason")
                c.execute("INSERT INTO client_search_interaction_contexts VALUES(?,?,?,?,?,?)", (item_id, identifier, supplied_id, branch_id, w["directory_revision"], canonical({"reason": reason})))
            c.execute("UPDATE sales_accounts SET version=version+1,updated_at=? WHERE id=?", (now, internal))
            result = {"id": item_id, "action": action, "account_version": sales["version"] + 1}
            _save_receipt(c, "account", internal, request_id, payload, result, now)
        return result

    def set_schedule(self, identifier, *, enabled, grant=None, execution_fingerprint=None, expected_workspace_version, request_id=None, preview_id=None, expected_fingerprint=None):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        now = self._now()
        payload = {"enabled": enabled, "grant": grant, "execution_fingerprint": execution_fingerprint,
                   "expected_workspace_version": expected_workspace_version, "preview_id": preview_id, "expected_fingerprint": expected_fingerprint}
        with self.store.session(write=True) as c:
            if request_id:
                cached = _receipt(c, "schedule", identifier, request_id, payload)
                if cached is not None:
                    return cached
            w = _row(c, "client_search_workspaces", "id", identifier)
            if w["version"] != _version(expected_workspace_version):
                raise ConflictError("Workspace changed; review schedule again")
            if not enabled:
                self._pause_schedule(c, identifier, "Paused by user", now.isoformat())
            else:
                if not grant or not execution_fingerprint:
                    raise ValueError("An execution preview and frozen grant are required")
                if preview_id:
                    preview = self._preview(c, identifier, preview_id, "checks")
                    if preview["fingerprint"] != expected_fingerprint or preview["payload"]["mode"] != "recommendations" or preview["payload"]["execution_fingerprint"] != execution_fingerprint:
                        raise ConflictError("Schedule preview does not match its reviewed grant")
                revision = new_id("schedule")
                c.execute("INSERT INTO client_search_schedules VALUES(?,?,1,?,?,72,?,?,NULL,?) ON CONFLICT(workspace_id) DO UPDATE SET revision=excluded.revision,enabled=1,anchor=excluded.anchor,next_due_at=excluded.next_due_at,grant_json=excluded.grant_json,execution_fingerprint=excluded.execution_fingerprint,pause_reason=NULL,updated_at=excluded.updated_at", (identifier, revision, now.isoformat(), (now+timedelta(hours=72)).isoformat(), canonical(grant), execution_fingerprint, now.isoformat()))
                if preview_id:
                    c.execute("UPDATE client_search_previews SET consumed_at=? WHERE id=?", (now.isoformat(), preview_id))
            row = c.execute("SELECT * FROM client_search_schedules WHERE workspace_id=?", (identifier,)).fetchone()
            result = {**dict(row), "grant": json.loads(row["grant_json"])} if row else None
            if request_id:
                _save_receipt(c, "schedule", identifier, request_id, payload, result, now.isoformat())
            return result

    def pause_schedule(self, identifier, reason):
        with self.store.session(write=True) as c:
            self._pause_schedule(c, identifier, _text(reason, "reason", maximum=4000), self._now().isoformat())

    def scheduled_round(self, identifier, revision, slot):
        with self.store.session() as c:
            row = c.execute("SELECT id FROM client_search_rounds WHERE workspace_id=? AND schedule_revision=? AND slot=?", (identifier, revision, slot)).fetchone()
            return self._round(c, row[0]) if row else None

    def enabled_schedules(self):
        with self.store.session() as c:
            return [{**dict(r), "grant": json.loads(r["grant_json"])} for r in c.execute("SELECT * FROM client_search_schedules WHERE enabled=1 ORDER BY next_due_at,workspace_id")]

    def advance_schedule(self, identifier, revision, next_due_at):
        with self.store.session(write=True) as c:
            c.execute("UPDATE client_search_schedules SET next_due_at=?,updated_at=? WHERE workspace_id=? AND revision=? AND enabled=1", (utc(next_due_at).isoformat(), self._now().isoformat(), identifier, revision))

