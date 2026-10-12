"""Read-only outreach projection for paid-audit launch choices.

This read model belongs to outreach because it interprets outreach-owned
tables.  It never migrates or writes the database; unavailable, partial, or
busy storage degrades to the manual-entry contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


REQUIRED_SCOPE_OPTION_TABLES = {
    "sales_accounts",
    "clinic_branches",
    "clinic_branch_snapshots",
    "treatments",
}


def _manual_scope_options(
    reason: str,
    *,
    missing_tables: list[str] | None = None,
) -> dict:
    return {
        "manual_entry": True,
        "outreach": {
            "schema_ready": False,
            "prefill_available": False,
            "reason": reason,
            "missing_tables": list(missing_tables or []),
        },
        "accounts": [],
        "treatments": [],
    }


def load_paid_audit_scope_options(outreach_db_path: Path | str) -> dict:
    """Return the branch/treatment read model used to start a paid audit."""

    database = Path(outreach_db_path)
    if not database.is_file():
        return _manual_scope_options("database_unavailable")

    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.5) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(REQUIRED_SCOPE_OPTION_TABLES - tables)
            if missing:
                return _manual_scope_options(
                    "schema_incomplete",
                    missing_tables=missing,
                )

            treatments = [
                {
                    "slug": str(row["slug"]),
                    "canonical_name": str(row["canonical_name"]),
                    "category": str(row["category"] or ""),
                    "taxonomy_version": str(row["taxonomy_version"] or ""),
                }
                for row in connection.execute(
                    """
                    SELECT slug,canonical_name,category,taxonomy_version
                    FROM treatments
                    WHERE active=1
                    ORDER BY canonical_name COLLATE NOCASE,slug
                    """
                )
            ]
            active_treatments = {item["slug"] for item in treatments}

            accounts: dict[str, dict] = {
                str(row["id"]): {
                    "sales_account_id": str(row["id"]),
                    "clinic_name": str(row["display_name_override"] or ""),
                    "area": "",
                    "base_url": "",
                    "branches": [],
                    "priority_treatments": [],
                }
                for row in connection.execute(
                    "SELECT id,display_name_override FROM sales_accounts ORDER BY id"
                )
            }

            if "research_account_snapshots" in tables:
                for row in connection.execute(
                    """
                    WITH ranked AS (
                      SELECT sales_account_id,clinic_name,area,owned_website,
                             ROW_NUMBER() OVER (
                               PARTITION BY sales_account_id
                               ORDER BY imported_at DESC,release_id DESC,research_account_id DESC
                             ) AS row_number
                      FROM research_account_snapshots
                    )
                    SELECT sales_account_id,clinic_name,area,owned_website
                    FROM ranked WHERE row_number=1
                    """
                ):
                    account = accounts.get(str(row["sales_account_id"]))
                    if not account:
                        continue
                    account["clinic_name"] = account["clinic_name"] or str(
                        row["clinic_name"] or ""
                    )
                    account["area"] = str(row["area"] or "")
                    account["base_url"] = str(row["owned_website"] or "")

            has_confirmations = "branch_confirmations" in tables
            confirmation_columns = (
                ",bc.status AS confirmation_status,bc.display_name AS confirmed_name"
                if has_confirmations
                else ",NULL AS confirmation_status,NULL AS confirmed_name"
            )
            confirmation_join = (
                "LEFT JOIN branch_confirmations bc ON bc.branch_id=cb.id"
                if has_confirmations
                else ""
            )
            branch_query = f"""
                WITH ranked AS (
                  SELECT cbs.*,
                         ROW_NUMBER() OVER (
                           PARTITION BY cbs.branch_id
                           ORDER BY cbs.created_at DESC,cbs.release_id DESC
                         ) AS row_number
                  FROM clinic_branch_snapshots cbs
                )
                SELECT cb.id AS branch_id,cb.sales_account_id,
                       ranked.derived_parent_name,ranked.derived_branch_name,
                       ranked.area,ranked.area_name,ranked.postcode,
                       ranked.derivation_status
                       {confirmation_columns}
                FROM clinic_branches cb
                JOIN ranked ON ranked.branch_id=cb.id AND ranked.row_number=1
                {confirmation_join}
                ORDER BY ranked.derived_parent_name COLLATE NOCASE,
                         ranked.derived_branch_name COLLATE NOCASE,cb.id
            """
            for row in connection.execute(branch_query):
                status = str(row["confirmation_status"] or "unconfirmed")
                if status in {"rejected", "merged"}:
                    continue
                account = accounts.get(str(row["sales_account_id"]))
                if not account:
                    continue
                parent_name = str(
                    account["clinic_name"]
                    or row["derived_parent_name"]
                    or account["sales_account_id"]
                )
                branch_name = str(
                    row["confirmed_name"]
                    or row["derived_branch_name"]
                    or row["area_name"]
                    or row["postcode"]
                    or "Main"
                )
                account["clinic_name"] = parent_name
                account["area"] = account["area"] or str(
                    row["area_name"] or row["area"] or ""
                )
                account["branches"].append(
                    {
                        "branch_id": str(row["branch_id"]),
                        "name": branch_name,
                        "area": str(row["area_name"] or row["area"] or ""),
                        "postcode": str(row["postcode"] or ""),
                        "seed_urls": [],
                        "entity_id": "",
                        "location_id": "",
                        "display_name": f"{parent_name} — {branch_name}",
                        "confirmation_status": status,
                        "prefill": status == "confirmed",
                    }
                )

            if "account_treatment_observations" in tables:
                observed: dict[str, set[str]] = {}
                for row in connection.execute(
                    """
                    SELECT DISTINCT sales_account_id,treatment_slug
                    FROM account_treatment_observations
                    ORDER BY sales_account_id,treatment_slug
                    """
                ):
                    slug = str(row["treatment_slug"])
                    if slug in active_treatments:
                        observed.setdefault(
                            str(row["sales_account_id"]),
                            set(),
                        ).add(slug)
                for account_id, slugs in observed.items():
                    if account_id in accounts:
                        accounts[account_id]["priority_treatments"] = sorted(slugs)[:20]

            account_rows = sorted(
                (
                    account
                    for account in accounts.values()
                    if account["clinic_name"]
                ),
                key=lambda account: (
                    account["clinic_name"].casefold(),
                    account["sales_account_id"],
                ),
            )
            prefill_available = bool(treatments) and any(
                branch.get("prefill")
                for account in account_rows
                for branch in account["branches"]
            )
            return {
                "manual_entry": True,
                "outreach": {
                    "schema_ready": True,
                    "prefill_available": prefill_available,
                    "reason": "ready" if prefill_available else "catalog_empty",
                    "missing_tables": [],
                },
                "accounts": account_rows,
                "treatments": treatments,
            }
    except (OSError, sqlite3.Error):
        return _manual_scope_options("database_unavailable")


__all__ = ["load_paid_audit_scope_options"]
