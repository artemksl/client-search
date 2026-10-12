"""Hash-bound host request provenance for manual and recurring research work."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from clinic_geo.integrity import canonical_sha256


def verify_client_research_admission(job_dir: Path, measurement: dict, *records):
    research = measurement.get("client_research")
    if measurement.get("scope_kind") not in {"client_research_identification", "client_research_recommendations"}:
        return None
    keys = []
    for record in records:
        record = record or {}
        digest = record.get("runner_client_research_admission_sha256", record.get("client_research_admission_sha256"))
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Client research requires an immutable execution admission")
        keys.append(digest)
    if not keys or len(set(keys)) != 1:
        raise ValueError("Client research admission bindings disagree")
    path = Path(job_dir) / "client-research-admission.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Client research admission file is unavailable")
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != keys[0]:
        raise ValueError("Client research admission was changed")
    admission = json.loads(encoded)
    if admission.get("schema_version") != "client_research.execution_admission.v1" or admission.get("mode") not in {"manual", "scheduled"}:
        raise ValueError("Client research admission is unsupported")
    if not isinstance(research, dict) or any(admission.get(k) != research.get(k) for k in ("workspace_id", "directory_revision", "config_revision")):
        raise ValueError("Client research admission identity differs from measurement")
    if not admission.get("request_id") or not admission.get("round_id"):
        raise ValueError("Client research admission lacks its delivery identity")
    if admission.get("runner_run_id") != Path(job_dir).name:
        raise ValueError("Client research admission belongs to another job")
    for record in records:
        plan_hash = (record or {}).get("runner_plan_sha256", (record or {}).get("plan_sha256"))
        if plan_hash and plan_hash != admission.get("plan_sha256"):
            raise ValueError("Client research admission differs from its frozen plan")
    if admission["mode"] == "scheduled":
        grant = admission.get("grant")
        if measurement["scope_kind"] != "client_research_recommendations" or not isinstance(grant, dict) or not admission.get("schedule_revision") or type(admission.get("slot")) is not int:
            raise ValueError("Recurring admission permits recommendation checks only")
        if (admission["slot"] < 1 or grant.get("execution_fingerprint") != admission.get("execution_fingerprint")
                or canonical_sha256({k: v for k, v in grant.items() if k != "execution_fingerprint"}) != grant.get("execution_fingerprint")):
            raise ValueError("Recurring admission differs from its reviewed execution grant")
        if (any(grant.get(k) != research.get(k) for k in ("workspace_id", "city", "country_code", "country_name", "language", "cells", "recommendation_template"))
                or grant.get("providers") != measurement.get("platforms")):
            raise ValueError("Recurring measurement exceeds its reviewed grant")
    return admission
