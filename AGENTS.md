# Agent instructions

Read `README.md` and `docs/AGENT_HANDOFF.md` first. This repository is a scoped,
standalone export of the client-search feature, not the full source application.

## Operate

- Run `./setup.sh`, then `./run.sh --port 8766`. Change the port if occupied;
  never stop another application to claim its port.
- Opening the panel, importing CSVs and generating research prompts are local
  operations. A new live measurement or recurring schedule requires an explicit
  human Start/Refetch or schedule enablement. Packaging, setup and testing alone
  do not authorize collection. Do not press Start or enable a schedule as a test.
- Real profiles and account bindings must be supplied locally by the user. See
  `config/README.md`. Use only the seven explicit routine IDs; no empty/default,
  all-profile or outside-allowlist fallback.
- Live Start/Refetch restarts Chrome under ownership locks. Existing Copilot
  automatic verification may use foreground input. Preserve exact tab/profile,
  episode, execution and request correlation; never dismiss unknown blockers or
  enter passwords/MFA to force progress.

## Preserve the product contract

- One city and country per workspace. Branch/account identity and grouping come
  from the supplied CSV. Do not infer shared buyers from a name or website.
- Imports preview a complete replacement. Missing branches become inactive;
  account contacts and historical evidence remain available.
- Identification and recommendations are different checks. The identification
  judge is a different provider and judges the complete original answer.
- P1/P2 require complete fresh recommendation coverage. Absence, errors, stale
  evidence, review flags and ambiguous names must remain distinct states.
- Recommendation schedules run every 72 hours only after explicit enablement.
  Scope changes pause the grant. Identification refresh is a separate manual
  action. Do not construct or replay missed measurements.
- Contact actions record activity; they never send messages to clinics.

## Develop

- `src/client_search/app.py` owns the standalone composition and exposed routes.
  Reuse the imported job and runner controls rather than creating another browser
  measurement implementation.
- Import outreach through `clinic_geo.outreach.api`, analysis through
  `clinic_geo.visibility_reports.api`, and runner contracts through its public
  API. Outreach must not import web or workflow layers; non-web code must not
  import the web control plane. Preserve the copied package boundaries.
- Keep source logic in its existing package. The support modules included by
  the export are load-bearing dependencies; do not delete them based on names.
- Read large modules by symbol or line range. Use `rg` for searches.
- Use the supplied offline tests for changed behavior. No live browser sends,
  external collection, model calls or saved user profiles in tests.
- Treat runtime evidence as immutable. Never fabricate, edit, replay or
  reclassify past measurement answers to make a priority or test pass.
- Never commit `.env`, local configuration, credentials, Chrome data, clinic
  databases or run artifacts. Commit only scoped source changes.
- `SOURCE_MANIFEST.json` records export provenance. `PACKAGE_MANIFEST.json`
  records shipped file hashes. After intentional source changes, refresh package
  hashes with `python tools/verify_package.py --refresh`; do not rewrite source
  provenance as if the exported files were unchanged.

This checkout has fresh history. It does not contain or authorize publishing the
original host repository's history. Work and publish only inside this repository
when the human requests it.
