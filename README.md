# Client search

A local web panel for researching clinic prospects, one city and country per
workspace. Import a directory supplied by the user, check whether AI can identify
each branch, and check whether AI recommends it for the selected areas and
treatments. Contact history belongs to the buying account shared by its branches.

This folder contains the panel, backend, database migrations, guarded worker,
matching and assessment logic, tests, CSV templates, research prompt and pinned
visibility runner. It is a fresh source export with no original Git history,
clinic records, browser sessions or credentials. Supporting `clinic_geo` modules
are included because the feature imports their public facades. The standalone
server exposes only client research and its visibility/job controls.

## Start

Use Python 3.12 or later. The supplied browser launcher and desktop verification
support macOS with Google Chrome. The local panel and non-browser tests also run
on Linux. Windows is not supported by the current filesystem locking code.

```bash
./setup.sh
./run.sh
```

Open <http://127.0.0.1:8766/visibility-testing#clientResearch>. If another app uses
that port, run `./run.sh --port 8767` and use the same URL with port 8767. Stop the
server with Ctrl+C. Opening the panel does not start measurements.

`setup.sh` installs an editable checkout and finds the private runner packages in
`vendor/`; it downloads ordinary Python dependencies from the configured package
index. Select a particular interpreter with
`CLIENT_SEARCH_PYTHON=python3.12 ./setup.sh`. Keep this checkout intact: the worker,
CSV examples and local configuration are resolved relative to it.

## Use the panel

1. Create a city workspace and select its country from the dropdown.
2. Open **Import directory**. **Download CSV template** provides the required
   header. The adjacent **Research prompt** option creates a detailed instruction
   for an AI chatbot to research that city; choose treatments and optional areas,
   then copy or download the prompt. Review the chatbot's sources and CSV yourself.
3. Preview and import the complete directory. Each row is one physical branch;
   the user supplies stable branch and account IDs. Repeat an account ID for
   branches sharing a buying/contact unit. The application does not guess groups.
4. Configure questions, local treatment labels and area/treatment pairs. Set up
   the browser profiles below before previewing live checks.
5. Review **Check now**, then explicitly choose **Start checks** when ready.
   Identification asks one chatbot about the named clinic and uses a different
   chatbot to judge its saved answer. Recommendation checks ask unbranded area
   and treatment questions across the configured providers and profiles.
6. Review accounts and branch evidence. P1 means explicit inability to identify
   and complete current coverage without recommendations. P2 means identified
   with the same complete absence. P3 means a current affirmative recommendation.
   Failed, ambiguous, incomplete or stale checks do not prove absence.
7. Enable **Schedule settings** separately for recommendation checks every
   72 hours. Leave the computer and server running. After downtime, one current
   due check is reserved instead of replaying missed checks. Identification is
   refreshed manually and expires after 30 days by default.

Replacing a directory stops checks for omitted branches, preserving their old
results and account history. The importer previews the changes before saving.
Account contact actions record contact performed elsewhere; they do not send
email, SMS or other messages.

See [the complete panel guide](docs/client-research-panel.md),
[the CSV contract](docs/client-search-directory-format.md) and
[the agent handoff](docs/AGENT_HANDOFF.md). The CSV example is fictional.

## Configure real measurements

Local import, research-prompt creation and contact management need no chatbot
login. Live measurements need seven explicitly selected, already signed-in
Chrome profiles. Follow [configuration instructions](config/README.md). No real
profile bindings are included. The seven stable IDs are an inherited application
contract, not permission to use someone else's sessions.

The exact runner dependencies are `visibility-runner==0.2.54` and
`workspace-automation-core==0.1.2`. Their wheels and the macOS desktop dependency
`axbridge==0.2.3` are included under `vendor/`.
Measurement defaults are ChatGPT, Gemini and browser Copilot. Actual provider
availability and sign-in must be checked on the target computer.

An authorized new Start or Refetch closes Chrome and reopens the selected
profiles under the existing ownership guard. Copilot's configured automatic
verification can briefly focus Chrome and use the mouse. Resume and finalization
retain their existing execution policy and do not restart Chrome. Keep the
existing send, recovery and ownership fences intact.

## Test without collecting live answers

```bash
.venv/bin/python -m pytest -q --ignore=tests/test_client_research_panel_ui.py
# For the isolated UI suite:
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q tests/test_client_research_panel_ui.py
# Validate a supplied CSV locally:
.venv/bin/python scripts/validate_client_search_directory.py docs/examples/client-search-directory-example.csv
# Check export file integrity:
.venv/bin/python tools/verify_package.py
```

Tests use temporary databases and synthetic evidence. UI tests intercept all
HTTP and open a separate headless Chromium, without saved profiles or provider
calls. `./setup.sh --browser-tests` also installs that isolated test browser.

Runtime state is created in `data/outreach/`, `data/ui-runs/` and the runner's
artifact directories. These paths, `.venv/`, local JSON configuration and `.env`
are ignored by Git. Back up local data separately. Never add credentials,
sessions or private client records to the repository.
