# Handoff to the next agent

## First run

1. Read `AGENTS.md` and `README.md`.
2. Use Python 3.12+ and run `./setup.sh` in this folder.
3. Run `./run.sh --port 8766`; open
   `http://127.0.0.1:8766/visibility-testing#clientResearch`.
4. For local inspection, create a workspace, open Import directory and use the
   CSV or Research prompt option. No profile configuration is needed for this.
5. Run the offline tests before changes. Use a temporary database in tests and
   the fictional example, not the user's actual directory.
6. For real checks, ask the user to supply the already signed-in profiles and
   follow `config/README.md`. Review the check preview together. Do not initiate
   checks or schedules merely to demonstrate that the application starts.

## Where to change behavior

| Concern | Files |
| --- | --- |
| Standalone server and route exposure | `src/client_search/app.py` |
| UI, CSV/prompt controls, country selection | `src/clinic_geo/web/static/client_research.js`, `client_research.css` |
| Panel shell and shared run inspector | `web/templates/visibility_testing.html`, `web/static/testing_app.js`, `run_workspace.js` |
| Strict AI directory specification | `web/static/client_research_directory_prompt.txt` |
| CSV parsing and branch/account records | `outreach/supplied_directory.py` |
| Workspaces, imports, contacts, configuration | `outreach/client_research_service.py`, `client_research_store.py` |
| Matching, coverage and priority rules | `outreach/client_research_assessment.py` |
| HTTP research endpoints | `web/client_research_api.py` |
| Reviewed preview, admission, child jobs, reconciliation | `web/client_research_execution.py` |
| Recurring due slots | `web/client_research_scheduler.py` |
| Runner measurement/runtime selections | `integrations/client_research_selection.py` |
| Grounded runner result validation | `visibility_reports/client_research_evidence.py` |
| Guarded worker and browser ownership | `scripts/run_visibility_measurement.py`, `execution/`, `web/jobs.py` |
| Schema | `outreach/migrations/013_client_search_workspace.sql` plus earlier required migrations |

Paths without an explicit prefix in the table are under `src/clinic_geo/`.
The inherited host facades import supporting outreach, snapshot, reporting and
other modules. They were copied to keep the contracts working. Those other
application lanes are not exposed by the standalone HTTP composition.

## What is included and what must be supplied

- The exported source, migrations, templates and tests are included, with source
  hashes in `SOURCE_MANIFEST.json`.
- Runner, automation-core and macOS AX Bridge wheels are included and pinned. They contain
  executable package code, not collected clinic answers or browser sessions.
- Unchanged static prompt and geography catalogs are included under `resources/`
  because the inherited snapshot contracts load them during import.
- Ordinary third-party Python dependencies are installed from a package index.
- Real CSVs, verified sources, profile bindings, Chrome login sessions and
  optional Copilot email bindings must be supplied by the user locally.
- No original host Git history or runtime database is included.

## Expected operation

The source chatbot answers a question about the named physical branch. A
different chatbot judges whether that complete answer identifies the branch or
explicitly cannot identify it. Independent generic recommendation observations
are shared across clinics in the selected city area/treatment cells. A matching
name must be grounded in the saved answer; shared brands are not proof of an
individual branch.

The contact queue aggregates physical branches under the supplied buying
account. P1 and P2 need complete, fresh, accepted negative recommendation
coverage. A current positive recommendation is P3. Missing or failed checks are
not negatives. Review flags exclude evidence from eligibility without rewriting
the original record.

The scheduler is started by the local server but only dispatches explicitly
enabled persisted grants. It checks due slots every 30 seconds and reserves one
current 72-hour slot after downtime. Scope changes pause a grant. Keep execution
receipts, immutable revisions, ownership fences and ambiguous-send protection.

## Verification and limits

The exported checkout passed all 288 tests on 2026-10-12, including the isolated
UI suite. Its independent setup, dependency consistency, fictional CSV validator
and standalone HTTP startup were also checked. No live answers were collected.

Use the commands in `README.md`. The source tests cover CSV validation,
replacement imports, identities, service transactions, matching, grounded
evidence, API validation, reviewed admission, recovery, schedules and isolated
desktop/mobile UI. Packaging tests exercise real loopback HTTP and persistence.

Offline tests establish local behavior. They do not prove chatbot sign-in,
provider availability, CAPTCHA handling or live result quality on another
machine. Verify those only through a separately authorized measurement run.

If the runner capability check fails, install the supplied wheels with the
repository's setup command; do not remove the capability guard. If profile
selection fails, fix the local catalogs explicitly instead of broadening the
profile allowlist or guessing a fallback.
