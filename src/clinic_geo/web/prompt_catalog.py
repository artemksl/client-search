"""Prompt-source catalog and customization helpers for the local web UI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from clinic_geo.parsing.prompt_sets import load_prompts
from clinic_geo.paths import resolve_repo_relative, root_or_default
from clinic_geo.web.jobs import WORKFLOWS


MAX_RESOURCE_BYTES = 50_000_000
BUILD_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class PromptResource:
    id: str
    label: str
    path: str
    kind: str
    role: str
    editable: bool
    notes: str = ""
    required_placeholder: Optional[str] = None


@dataclass(frozen=True)
class PromptWorkflow:
    """The prompt-specific delta for a workflow.

    `label` and `section` are NOT redeclared here: they are owned by
    clinic_geo.web.jobs.WORKFLOWS and read back out in prompt_catalog().
    """

    id: str
    source_type: str
    prompt_source: str
    run_behavior: str
    customization: list[str]
    generated_prompt_path: Optional[str] = None
    build_command: Optional[list[str]] = None
    resources: list[PromptResource] = field(default_factory=list)


PROMPT_WORKFLOWS: dict[str, PromptWorkflow] = {
    "smoke": PromptWorkflow(
        id="smoke",
        source_type="No live ChatGPT prompts",
        prompt_source="Uses the local smoke fixture under tests/fixtures/smoke.",
        run_behavior="Runs a deterministic local fixture and does not open ChatGPT.",
        customization=[
            "Customize the smoke fixture files if you need a different local test case.",
            "No prompt rebuild is needed because this workflow does not call the prompt runner.",
        ],
    ),
    "free_audit_stage1": PromptWorkflow(
        id="free_audit_stage1",
        source_type="Production-selected branch-treatment prompt pack",
        prompt_source="The dashboard freezes prompts for the selected area-treatment scope. ChatGPT and Gemini use the selected browser run count. Optional GitHub Copilot (SDK) uses its own account and repetition count, with Gemini parsing the saved answer.",
        run_behavior="The engine collects immutable observations and extracts ordered clinic names. After measurement completes, Prepare report analyzes the frozen scope and creates an internal review candidate.",
        customization=[
            "Select and validate the clinic, branches and branch-local treatments in Run setup; the job writes a run-local immutable config.",
            "Build creates a non-collectable preview only; starting the workflow freezes a new hash-bound pack in its own run directory.",
            "Changing scope after collection requires a new Snapshot rather than rebuilding an existing run.",
            "The broad audit_prompts.json basket remains read-only internal benchmarking material and is never report-eligible.",
        ],
        generated_prompt_path="data/free-audit/snapshot_prompts_v2_preview.json",
        build_command=[sys.executable, "-m", "clinic_geo.workflows.free_audit.build_prompt_preview_v2"],
        resources=[
            PromptResource("free_audit_config", "Snapshot v2 scope", "data/free-audit/free_audit_config.json", "json", "Frozen-scope source", True),
            PromptResource("free_audit_snapshot_preview", "Example prompt preview (not current selection)", "data/free-audit/snapshot_prompts_v2_preview.json", "json", "Non-collectable example generated from the checked-in config", False),
            PromptResource("free_audit_basket", "Internal benchmark basket", "data/free-audit/audit_prompts.json", "json", "Legacy internal benchmark; report_eligible=false", False),
            PromptResource("free_audit_guide", "Snapshot v2 guide", "data/free-audit/README.md", "text", "Workflow and evidence contract", False),
        ],
    ),
    "paid_audit": PromptWorkflow(
        id="paid_audit",
        source_type="Separate paid Diagnostic Audit",
        prompt_source="The paid Diagnostic Audit engine owns its versioned prompt contract independently from the frozen Free Visibility Snapshot pack.",
        run_behavior="Freezes a separate paid-audit scope and runs or resumes that engagement without generating, scanning, exporting, or modifying a Free Snapshot release.",
        customization=[
            "Configure the client, market, treatments, competitors, and audit options in the paid Diagnostic Audit setup.",
            "Do not use the Free Snapshot scope, review state or release pointer as mutable Diagnostic Audit inputs.",
            "The old brand/head-to-head prompts are retained only as read-only historical benchmark material.",
        ],
        resources=[
            PromptResource("diagnostic_legacy_basket", "Historical brand benchmark", "data/free-audit/audit_prompts_built.json", "json", "Legacy internal benchmark; report_eligible=false", False),
            PromptResource("diagnostic_guide", "Snapshot boundary guide", "data/free-audit/README.md", "text", "Explains separation from the free report", False),
        ],
    ),
    "database_complete_build": PromptWorkflow(
        id="database_complete_build",
        source_type="Audited multi-source local build",
        prompt_source="Reconciles the existing 56-treatment ChatGPT discovery evidence with the current CQC directory, current clinic websites, and optional OpenStreetMap discovery. It sends no new ChatGPT prompts.",
        run_behavior="Builds and validates a frozen release, retains every source candidate with a decision, publishes only evidence-qualified London clinics, refreshes territories and prospect scores, and advances the current-release pointer only after publication succeeds.",
        customization=[
            "Review completion_config.json before changing market scope, source requirements, service aliases, or exclusion patterns.",
            "Treat coverage as source-bounded: it proves every returned source record was accounted for, not that no unlisted clinic exists.",
            "Work the release review queue before outreach; missing consent, suppression, identity, location, or visibility evidence never passes automatically.",
            "Use the immutable release manifest and validation report to reproduce or audit a build.",
        ],
        resources=[
            PromptResource("database_completion_config", "Completion rules", "data/database/completion_config.json", "json", "Versioned market, source, and evidence rules", True),
            PromptResource("database_current_release", "Current release pointer", "data/database/current-release.json", "json", "Published release and manifest reference", False),
            PromptResource("database_complete_evidence", "Clinic evidence", "data/database/clinic-evidence.jsonl", "text", "Current canonical source evidence", False),
            PromptResource("database_complete_territories", "Clinic territories", "data/database/clinic-territories.jsonl", "text", "Current territory-enriched evidence", False),
            PromptResource("database_complete_ranking", "Prospect accounts", "data/database/prospect-scoring/prospect-accounts.jsonl", "text", "Current deduplicated ranking", False),
        ],
    ),
    "database_search_clinics": PromptWorkflow(
        id="database_search_clinics",
        source_type="Generated from procedure list",
        prompt_source="make_prompts reads data/database/1_finding_cliniques/procedures.json and formats each row with prompt_template.txt.",
        run_behavior="The generated 2_prompts.json is sent through the shared browser runner; extraction maps procedure ids to named clinics.",
        customization=[
            "Edit procedures.json to control which procedures become prompts.",
            "Edit prompt_template.txt to change the sentence around each procedure; keep the {name} placeholder.",
            "The current generated prompt set is view-only because each run rebuilds it from the source list/template.",
        ],
        generated_prompt_path="data/database/1_finding_cliniques/2_prompts.json",
        build_command=[sys.executable, "-m", "clinic_geo.workflows.database.finding_clinics.make_prompts"],
        resources=[
            PromptResource("database_search_procedures", "Procedure list", "data/database/1_finding_cliniques/procedures.json", "json", "Input list", True, "Expected schema: [{\"id\":\"slug\", \"name\":\"Treatment name\"}]."),
            PromptResource("database_search_template", "Prompt template", "data/database/1_finding_cliniques/prompt_template.txt", "text", "Generator template", True, "Must contain {name}.", "{name}"),
            PromptResource("database_search_built", "Generated procedure prompts", "data/database/1_finding_cliniques/2_prompts.json", "json", "Generated output", False),
        ],
    ),
    "database_matching": PromptWorkflow(
        id="database_matching",
        source_type="No live ChatGPT prompts",
        prompt_source="build_missing_list runs the conservative local matcher against extracted clinic names and the current database.",
        run_behavior="Runs locally: refreshes name_matches.json unless --skip-match is supplied, then writes missing-clinic and crosscheck files.",
        customization=[
            "Edit clinic_aliases.json to add vetted name overrides before matching.",
            "Refresh clinic-evidence.jsonl when the reference database changes.",
            "No prompt rebuild is needed because this workflow does not call the prompt runner.",
        ],
        resources=[
            PromptResource("database_matching_aliases", "Clinic aliases", "data/database/2_matching_found_with_existing_db/clinic_aliases.json", "json", "Curated matcher aliases", True),
            PromptResource("database_matching_missing", "Missing-clinic output", "data/database/2_matching_found_with_existing_db/3_missing_from_db.json", "json", "Generated output", False),
            PromptResource("database_matching_crosscheck", "Crosscheck output", "data/database/2_matching_found_with_existing_db/4_crosscheck.jsonl", "text", "Generated output", False),
        ],
    ),
    "database_enriching": PromptWorkflow(
        id="database_enriching",
        source_type="Generated from missing-clinic list",
        prompt_source="make_enrich_prompts reads 3_missing_from_db.json and formats each clinic with enrichment_prompt_template.txt.",
        run_behavior="The generated research prompts ask for strict JSON, run through the shared browser runner, and are parsed into enriched database rows.",
        customization=[
            "Edit enrichment_prompt_template.txt to change research instructions and scoring fields; keep the {name} placeholder.",
            "The missing-clinic list is produced upstream by the matching workflow; edit it only if you need to manually remove or add clinics.",
            "Do not change the JSON output keys unless parse_enriched.py is updated too.",
        ],
        generated_prompt_path="data/database/3_enriching_mismatched_data/2_prompts.json",
        build_command=[sys.executable, "-m", "clinic_geo.workflows.database.enriching.make_enrich_prompts"],
        resources=[
            PromptResource("database_enrich_template", "Enrichment prompt template", "data/database/3_enriching_mismatched_data/enrichment_prompt_template.txt", "text", "Generator template", True, "Must contain {name}; parsed output expects the listed JSON keys.", "{name}"),
            PromptResource("database_enrich_missing", "Missing-clinic list", "data/database/2_matching_found_with_existing_db/3_missing_from_db.json", "json", "Generated upstream input", True),
            PromptResource("database_enrich_built", "Generated enrichment prompts", "data/database/3_enriching_mismatched_data/2_prompts.json", "json", "Generated output", False),
        ],
    ),
    "database_merge_new_clinics": PromptWorkflow(
        id="database_merge_new_clinics",
        source_type="No live ChatGPT prompts",
        prompt_source="merge_old_and_new_data.py appends parsed enriched clinics into clinic-evidence.jsonl and snapshots the previous evidence database.",
        run_behavior="Runs locally and does not open ChatGPT.",
        customization=[
            "Review 4_enriched_clinics.jsonl before merging.",
            "Use the workflow command output to confirm how many new rows will be added.",
            "No prompt rebuild is needed because this workflow does not call the prompt runner.",
        ],
        resources=[
            PromptResource("database_merge_enriched", "Parsed enriched clinics", "data/database/3_enriching_mismatched_data/4_enriched_clinics.jsonl", "text", "Generated upstream input", False),
            PromptResource("database_merge_current", "Clinic evidence", "data/database/clinic-evidence.jsonl", "text", "Evidence database output", False),
            PromptResource("database_merge_last", "Previous evidence snapshot", "data/database/backups/previous-clinic-evidence.jsonl", "text", "Database backup", False),
        ],
    ),
    "database_tagging": PromptWorkflow(
        id="database_tagging",
        source_type="Deterministic territory mapping with postcode research fallback",
        prompt_source="Supplied full postcodes are validated locally. build_area_prompts asks ChatGPT only to research a missing or invalid full postcode; the model never chooses a territory.",
        run_behavior="The tagging runner validates postcode geography, maps it through the versioned 44-territory market taxonomy, and writes provenance, coordinates, review status, and a territory summary.",
        customization=[
            "Edit territory_taxonomy.json only for a deliberate, versioned territory-boundary change; each unit has a three-active-client cap.",
            "Set AREA_BATCH_SIZE or AREA_LIMIT to change missing-postcode research batching.",
            "Missing, multi-location, invalid, and outside-London records remain in the review queue instead of being forced into Central London.",
        ],
        generated_prompt_path="data/area-tagging/1_prompts.json",
        build_command=[sys.executable, "-m", "clinic_geo.workflows.tagging.build_area_prompts"],
        resources=[
            PromptResource("database_tagging_taxonomy", "Territory taxonomy", "data/area-tagging/territory_taxonomy.json", "json", "Versioned cap and boundary rules", True),
            PromptResource("database_tagging_built", "Missing-postcode prompts", "data/area-tagging/1_prompts.json", "json", "Generated output", False),
            PromptResource("database_tagging_summary", "Territory summary", "data/area-tagging/area_summary.json", "json", "Generated output", False),
            PromptResource("database_tagging_unresolved", "Territory review queue", "data/area-tagging/unresolved_areas.json", "json", "Generated output", False),
        ],
    ),
    "database_prospect_scoring": PromptWorkflow(
        id="database_prospect_scoring",
        source_type="Deterministic local calculation",
        prompt_source="Uses the current database, the shared appearance snapshot, procedure crosscheck, and optional contact enrichment; it sends no new prompts.",
        run_behavior="Runs locally, deduplicates clinic accounts, applies the versioned prospect rubric, and writes an account ranking plus a source-preserving scored clinic snapshot.",
        customization=[
            "The scoring weights and quality gates are versioned in clinic_geo.stats.prospect_scoring.",
            "Account identity overrides require sourced UID groups; base UID exclusions require an explicit documented decision.",
            "Refresh the appearance and database workflows before scoring when newer evidence is available.",
            "Review quality-summary.json warnings and REVIEW records before using the outreach queue.",
        ],
        resources=[
            PromptResource("prospect_database", "Clinic evidence", "data/database/clinic-evidence.jsonl", "text", "Primary evidence input; does not contain the prospect ranking", False),
            PromptResource("prospect_appearance", "Appearance extraction", "data/appearance/extracted_names.json", "json", "Observed GEO input", False),
            PromptResource("prospect_procedures", "Procedure crosscheck", "data/database/2_matching_found_with_existing_db/4_crosscheck.jsonl", "text", "Capped supporting input", False),
            PromptResource("prospect_account_overrides", "Account identity overrides", "data/database/account_entity_overrides.json", "json", "Sourced account-only unions", True),
            PromptResource("prospect_base_exclusions", "Base UID exclusions", "data/database/base_uid_exclusions.json", "json", "Explicit lineage allowlist", True),
            PromptResource("prospect_ranking", "Prospect accounts", "data/database/prospect-scoring/prospect-accounts.jsonl", "text", "Generated ranked account output", False),
            PromptResource("prospect_clinics", "Scored clinic records", "data/database/prospect-scoring/scored-clinics.jsonl", "text", "Generated UID output", False),
            PromptResource("prospect_summary", "Scoring quality summary", "data/database/prospect-scoring/quality-summary.json", "json", "Generated methodology and QA output", False),
        ],
    ),
    "clinic_analysis": PromptWorkflow(
        id="clinic_analysis",
        source_type="Generated from master prompt, verified methods, and website crawl",
        prompt_source="The workflow builds a one-shot verified-methods prompt from check_prompt.txt, then builds one large analysis prompt per clinic from master_prompt.txt, verified_methods.txt, and the website crawl cache.",
        run_behavior="Runs in a pinned logged-in Project profile, first refreshing verified methods, then sending per-clinic prompts through the shared browser runner.",
        customization=[
            "Edit master_prompt.txt for the per-clinic analysis rubric and output contract.",
            "Edit check_prompt.txt for the verified-methods research prompt.",
            "verified_methods.txt and generated 2_prompts.json are run outputs; rebuild after changing source prompts.",
        ],
        generated_prompt_path="data/clinic-analysis/2_prompts.json",
        build_command=[
            sys.executable,
            "-c",
            "from clinic_geo.workflows.clinic_analysis.build_check_prompt import main as c; "
            "from clinic_geo.workflows.clinic_analysis.build_prompts import main as b; c(); b()",
        ],
        resources=[
            PromptResource("clinic_analysis_master", "Master analysis prompt", "config/master_prompt.txt", "text", "Source instructions", True),
            PromptResource("clinic_analysis_check", "Verified-methods check prompt", "config/check_prompt.txt", "text", "Source instructions", True),
            PromptResource("clinic_analysis_verified_methods", "Verified methods output", "data/clinic-analysis/verified_methods.txt", "text", "Generated upstream input", False),
            PromptResource("clinic_analysis_check_built", "Generated check prompt", "data/clinic-analysis/check/2_prompts.json", "json", "Generated output", False),
            PromptResource("clinic_analysis_built", "Generated clinic prompts", "data/clinic-analysis/2_prompts.json", "json", "Generated output", False),
        ],
    ),
}


# Client research owns reviewed prompts in its city configuration and is an
# internal worker workflow, rather than a prompt-catalog launch form.
if set(PROMPT_WORKFLOWS) != set(WORKFLOWS) - {"client_research"}:
    raise RuntimeError(
        "prompt catalog and workflow registry drifted apart: "
        f"{sorted(set(PROMPT_WORKFLOWS) ^ (set(WORKFLOWS) - {'client_research'}))}"
    )


def _resource_stat(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "size": 0, "modified": None}
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()
    return {"exists": True, "size": path.stat().st_size, "modified": modified}


def _prompt_preview(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False, "count": 0, "items": []}
    try:
        prompts = load_prompts(path)
    except Exception as exc:
        return {"path": str(path), "exists": True, "count": 0, "items": [], "error": str(exc)}
    items = []
    for prompt_id, text, metadata in prompts:
        items.append({"id": prompt_id, "text": text, "metadata": metadata})
    return {"path": str(path), "exists": True, "count": len(prompts), "items": items}


def _resource_payload(resource: PromptResource, base: Path) -> dict:
    stat = _resource_stat(resolve_repo_relative(resource.path, base=base))
    return {
        "id": resource.id,
        "label": resource.label,
        "path": resource.path,
        "role": resource.role,
        "editable": resource.editable,
        "notes": resource.notes,
        **stat,
    }


def prompt_catalog(root: Path) -> dict:
    workflows = []
    base = root_or_default(root).resolve()
    for item in PROMPT_WORKFLOWS.values():
        source = WORKFLOWS[item.id]
        generated_path = (
            resolve_repo_relative(item.generated_prompt_path, base=base) if item.generated_prompt_path else None
        )
        workflow = {
            "id": item.id,
            "label": source.label,
            "section": source.section,
            "source_type": item.source_type,
            "prompt_source": item.prompt_source,
            "run_behavior": item.run_behavior,
            "customization": item.customization,
            "generated_prompt_path": item.generated_prompt_path,
            "build_supported": bool(item.build_command),
            "resources": [_resource_payload(resource, base) for resource in item.resources],
            "preview": _prompt_preview(generated_path) if generated_path else {"exists": False, "count": 0, "items": []},
        }
        workflows.append(workflow)
    return {"workflows": workflows}


def find_resource(workflow_id: str, resource_id: str) -> tuple[PromptWorkflow, PromptResource]:
    workflow = PROMPT_WORKFLOWS.get(workflow_id)
    if workflow is None:
        raise KeyError(f"unknown prompt workflow: {workflow_id}")
    for resource in workflow.resources:
        if resource.id == resource_id:
            return workflow, resource
    raise KeyError(f"unknown prompt resource: {resource_id}")


def read_resource(workflow_id: str, resource_id: str, root: Path) -> dict:
    _, resource = find_resource(workflow_id, resource_id)
    base = root_or_default(root).resolve()
    path = resolve_repo_relative(resource.path, base=base)
    if not path.exists():
        content = ""
    else:
        if path.stat().st_size > MAX_RESOURCE_BYTES:
            raise ValueError("resource is too large to edit in the browser")
        content = path.read_text(encoding="utf-8")
    return {"resource": _resource_payload(resource, base), "content": content}


def save_resource(workflow_id: str, resource_id: str, content: str, root: Path) -> dict:
    _, resource = find_resource(workflow_id, resource_id)
    if not resource.editable:
        raise ValueError("resource is read-only")
    if len(content.encode("utf-8")) > MAX_RESOURCE_BYTES:
        raise ValueError("resource is too large to save in the browser")
    if resource.required_placeholder and resource.required_placeholder not in content:
        raise ValueError(f"resource must contain {resource.required_placeholder}")
    if resource.kind == "json":
        if not content.strip():
            raise ValueError("JSON resource cannot be empty")
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    path = resolve_repo_relative(resource.path, base=root_or_default(root).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return read_resource(workflow_id, resource_id, root)


def build_generated_prompts(workflow_id: str, root: Path) -> dict:
    workflow = PROMPT_WORKFLOWS.get(workflow_id)
    if workflow is None:
        raise KeyError(f"unknown prompt workflow: {workflow_id}")
    if not workflow.build_command:
        raise ValueError("this workflow has no prompt build step")
    base = root_or_default(root).resolve()
    env = {"PYTHONPATH": str(base / "src")}
    try:
        proc = subprocess.run(
            workflow.build_command,
            cwd=base,
            env={**os.environ, **env},
            text=True,
            capture_output=True,
            check=False,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        # Runs inside the HTTP handler thread: fail loudly instead of hanging the request.
        raise TimeoutError(
            f"prompt build for {workflow_id} timed out after {BUILD_TIMEOUT_SECONDS}s"
        ) from exc
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "catalog": prompt_catalog(root),
    }
