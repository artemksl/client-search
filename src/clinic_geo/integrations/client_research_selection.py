"""Freeze client research into the runner's public portable scope contracts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from clinic_geo.integrity import canonical_sha256


def measurement_from_client_research(value):
    from clinic_geo.integrations.visibility_runner import coerce_measurement_config
    if not isinstance(value, dict) or value.get("scope_kind") not in {"client_research_identification", "client_research_recommendations"}:
        raise ValueError("A public client research scope is required")
    return coerce_measurement_config(deepcopy(value)).to_mapping()


def client_research_runtime(root, run_id, profile_rows, retry_policy, *, cdp_url="http://127.0.0.1:9222", run_root=None):
    """Construct the same owned browser policy used by standard measurement."""
    root = Path(root)
    return {
        "artifact_root": str(root / "data/free-audit/visibility-runner-executions"),
        "profiles": {"selected_profile_ids": [r["id"] for r in profile_rows], "manifest_path": str((Path(run_root) if run_root else root / "data/ui-runs") / run_id / "runner-profiles.json"), "manifest_sha256": canonical_sha256(profile_rows), "selected_profiles": deepcopy(profile_rows), "lease_timeout_seconds": 300},
        "browser": {"cdp_url": cdp_url, "launch_policy": "attach_only", "background_only": True, "copilot_verification_policy": "automatic_hold_v1"},
        "retry_policy": deepcopy(retry_policy), "max_concurrency": len(profile_rows) * 3 * 2,
        "tabs_per_provider": 2, "packet_start_stagger_seconds": 10, "worker_timeout_seconds": 900,
    }
