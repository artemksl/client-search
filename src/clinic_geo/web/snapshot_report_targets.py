"""Resolve report targets from the current verified directory, after measurement."""

from clinic_geo.outreach.api import verify_current_release
from clinic_geo.visibility_reports.api import normalize_report_target
from clinic_geo.web.snapshot_production import SnapshotReleaseConflict


def snapshot_report_targets(root):
    release = verify_current_release(root)
    targets = []
    for account in release.accounts:
        account_id = account.get("account_id") or account.get("stable_account_key")
        name = account.get("clinic_name")
        if not account_id or not name:
            continue
        target = normalize_report_target({
            "account_id": account_id,
            "name": name,
            "verified_aliases": list(dict.fromkeys(
                alias for alias in account.get("member_names") or []
                if isinstance(alias, str) and alias.strip() and alias != name
            )),
            "research_release_id": release.release_id,
            "research_manifest_sha256": release.manifest_sha256,
        })
        targets.append(target)
    targets.sort(key=lambda row: (row["name"].casefold(), row["account_id"]))
    return {
        "research_release_id": release.release_id,
        "research_manifest_sha256": release.manifest_sha256,
        "targets": targets,
    }


def resolve_snapshot_report_target(root, selection):
    if not isinstance(selection, dict):
        raise ValueError("Choose a clinic for the report")
    # Names and aliases always come from the verified release, never the request.
    unsupported = set(selection) - {"account_id", "research_release_id", "research_manifest_sha256"}
    if unsupported:
        raise ValueError("Report target accepts only a directory account and its release identity")
    catalog = snapshot_report_targets(root)
    for key in ("research_release_id", "research_manifest_sha256"):
        if selection.get(key) != catalog[key]:
            raise SnapshotReleaseConflict("The clinic directory changed; reload the report choices")
    target = next((row for row in catalog["targets"] if row["account_id"] == selection.get("account_id")), None)
    if target is None:
        raise ValueError("Choose a clinic from the current directory")
    return target
