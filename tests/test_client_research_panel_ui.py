"""Standalone panel against the real research API and temporary persistence.

All browser requests are intercepted. Jobs and collected answers are synthetic;
no worker, saved profile, provider request or scheduler thread is started.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import base64
import re

import pytest
from playwright.sync_api import expect

from clinic_geo.outreach.api import ClientResearchService, ROUTINE_PROFILE_IDS
from clinic_geo.integrations.visibility_runner import VisibilityMeasurementComponent
from clinic_geo.web.client_research_api import ClientResearchAPI
from clinic_geo.web.client_research_execution import ClientResearchCoordinator
from test_client_research_execution import Jobs
from test_client_research_execution_review import completed_initial
from _browser import browser  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/clinic_geo/web"
CSV = ("account_id,account_name,branch_id,branch_name,country_code,city,area,address,website,treatment_ids\n"
       "a,Alpha,b,Alpha Central,FR,Paris,Central,1 Rue Test,https://alpha.example,skin\n"
       "a,Alpha,b2,Alpha East,FR,Paris,Central,2 Rue Test,https://alpha.example,skin\n")


class PanelJobs(Jobs):
    def start(self, workflow, config, request_id):
        job = super().start(workflow, config, request_id)
        job.workflow_id = workflow
        job.label = "Client research " + config["client_research"]["kind"]
        job.created_at = "2026-10-11T00:00:00Z"
        job.run_config.update(deepcopy(config))
        return job


@pytest.fixture
def panel(browser, tmp_path):
    service = ClientResearchService(tmp_path, clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))
    jobs = PanelJobs(tmp_path)
    profiles = [{"id": profile, "display_name": profile, "chrome_directory": f"Synthetic {i}"}
                for i, profile in enumerate(ROUTINE_PROFILE_IDS)]

    def no_result(run_id):
        raise LookupError("No synthetic answer has been collected")

    coordinator = ClientResearchCoordinator(tmp_path, service, jobs,
        component_factory=lambda runtime: SimpleNamespace(plan=VisibilityMeasurementComponent(runtime).plan, inspect=no_result),
        profile_loader=lambda ids, root: deepcopy(profiles), account_loader=lambda root, rows: {})
    api = ClientResearchAPI(tmp_path, service_factory=lambda root: service,
                            coordinator_factory=lambda supplied: coordinator)
    context = browser.new_context(viewport={"width": 1383, "height": 900}, service_workers="block")
    page = context.new_page()
    calls, errors, unhandled = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(intercept):
        request = intercept.request
        url = urlsplit(request.url)
        calls.append((url.path, request.method, request.post_data_json if request.post_data else None))
        assert url.netloc == "127.0.0.1:1", "External request blocked: " + request.url
        if url.path == "/visibility-testing":
            intercept.fulfill(content_type="text/html", body=(WEB / "templates/visibility_testing.html").read_text())
        elif url.path.startswith("/static/"):
            asset = WEB / url.path.lstrip("/")
            intercept.fulfill(content_type="text/css" if asset.suffix == ".css" else "application/javascript", body=asset.read_text())
        elif url.path.startswith("/api/client-research/"):
            response = (api.handle_get(url.path, parse_qs(url.query, keep_blank_values=True)) if request.method == "GET"
                        else api.handle_post(url.path, request.post_data_json))
            intercept.fulfill(status=response.status, json=response.payload)
        elif url.path == "/api/jobs":
            assert parse_qs(url.query)["scope"] == ["visibility"]
            intercept.fulfill(json={"jobs": [vars(job) for job in jobs.list()]})
        elif url.path.startswith("/api/jobs/") and url.path.endswith("/log"):
            intercept.fulfill(content_type="text/plain", body="Synthetic research log")
        elif url.path.startswith("/api/jobs/") and url.path.endswith("/failures"):
            intercept.fulfill(json={"readout": {"run_id": url.path.split("/")[3], "host": {"refetch_available": False}}})
        else:
            unhandled.append(request.url)
            intercept.fulfill(status=500, json={"error": "Unmocked request"})

    context.route("**/*", route)
    s = SimpleNamespace(page=page, context=context, service=service, coordinator=coordinator,
                        jobs=jobs, calls=calls, root=tmp_path)
    yield s
    context.close()
    assert not errors, errors
    assert not unhandled, unhandled


def open_research(s):
    s.page.goto("http://127.0.0.1:1/visibility-testing#clientResearch")
    expect(s.page.locator("#workspaceTitle")).to_have_text("Client research")
    expect(s.page.locator("#clientResearchView")).to_be_visible()
    expect(s.page.locator("#testingView")).not_to_be_visible()


def create_and_import(s):
    page = s.page
    open_research(s)
    page.get_by_role("button", name="Create city workspace", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("City", exact=True).fill("Paris")
    dialog.get_by_label("Country", exact=True).select_option("FR")
    dialog.get_by_label("Timezone", exact=True).fill("Europe/Paris")
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(page.locator(".cr-setup")).to_contain_text("Paris, France")
    page.get_by_role("button", name="Import directory", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Directory CSV").set_input_files({"name": "clinics.csv", "mimeType": "text/csv", "buffer": CSV.encode()})
    dialog.get_by_role("button", name="Preview import", exact=True).click()
    expect(dialog.get_by_role("button", name="Import reviewed directory", exact=True)).to_be_enabled()
    assert not s.jobs.starts
    dialog.get_by_role("button", name="Import reviewed directory", exact=True).click()
    expect(page.locator(".cr-setup")).to_contain_text("2 branches in 1 account")
    expect(page.locator(".cr-account-table tbody > tr")).to_have_count(1)
    page.get_by_role("button", name="Configure questions", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("skin", exact=True).fill("skin treatments")
    dialog.get_by_role("group", name="Area and treatment pairs").get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Save configuration", exact=True).click()
    expect(page.get_by_role("button", name="Check now", exact=True)).to_be_enabled()


def test_city_research_prompt_is_specific_downloadable_and_does_not_start_checks(panel):
    s, page = panel, panel.page
    create_and_import(s)
    before = len(s.calls)
    page.get_by_role("button", name="Import directory", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Research prompt", exact=True).click()
    expect(dialog.get_by_label("Treatments to research", exact=True)).to_have_value("skin = skin treatments")
    dialog.get_by_label("Treatments to research", exact=True).fill("laser-hair-removal = Épilation laser\nbotox = Botox treatment")
    dialog.get_by_label("Areas to research (optional)", exact=True).fill("Central\nEast")
    dialog.get_by_role("button", name="Create research prompt", exact=True).click()
    prompt = dialog.get_by_label("AI directory research prompt", exact=True)
    expect(prompt).to_be_visible()
    text = prompt.input_value()
    assert 'City (exact CSV value): "Paris"' in text
    assert 'Country name: "France"' in text
    assert 'Country code (exact CSV value): "FR"' in text
    assert 'Area scope: ["Central","East"]' in text
    assert "laser-hair-removal = Épilation laser" in text
    assert "{{" not in text
    header = (ROOT / "docs/examples/client-search-directory-template.csv").read_text().strip()
    assert header in text
    assert "research-notes.md" in text
    assert "BEGIN CSV DATA CONTRACT" in text
    assert "COLUMN 12: branch_aliases" in text
    assert "BEGIN FICTIONAL CSV EXAMPLE" in text
    assert prompt.evaluate("el => el.readOnly")
    with page.expect_download() as downloaded:
        dialog.get_by_role("link", name="Download research prompt", exact=True).click()
    assert Path(downloaded.value.path()).read_text(encoding="utf-8") == text
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="http://127.0.0.1:1")
    dialog.get_by_role("button", name="Copy research prompt", exact=True).click()
    expect(dialog.get_by_role("status")).to_contain_text("Research prompt copied")
    assert page.evaluate("navigator.clipboard.readText()") == text
    assert all(method == "GET" for _, method, _ in s.calls[before:])
    assert not s.jobs.starts and not s.service.enabled_schedules()


def test_research_prompt_option_keeps_drafts_when_toggled_on_mobile(panel):
    s, page = panel, panel.page
    create_and_import(s)
    page.set_viewport_size({"width": 390, "height": 844})
    before = len(s.calls)
    page.get_by_role("button", name="Import directory", exact=True).click()
    dialog = page.get_by_role("dialog")
    toggle = dialog.get_by_role("button", name="Research prompt", exact=True)
    download = dialog.get_by_role("link", name="Download CSV template", exact=True)
    setup = dialog.locator(f"#{toggle.get_attribute('aria-controls')}")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(setup).to_be_hidden()
    expect(setup).to_have_attribute("aria-label", "Research prompt setup")
    assert download.evaluate("el => el.parentElement.querySelector('button')?.textContent") == "Research prompt"

    toggle.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(setup).to_be_visible()
    areas = setup.get_by_label("Areas to research (optional)", exact=True)
    areas.fill("East")
    setup.get_by_role("button", name="Create research prompt", exact=True).click()
    prompt = setup.get_by_label("AI directory research prompt", exact=True)
    expect(prompt).to_be_visible()
    text = prompt.input_value()
    toggle.press("Space")
    expect(setup).to_be_hidden()
    expect(toggle).to_be_focused()
    toggle.press("Enter")
    expect(areas).to_have_value("East")
    expect(prompt).to_have_value(text)
    expect(setup.get_by_role("link", name="Download research prompt", exact=True)).to_have_attribute("href", re.compile(r"^blob:"))
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert all(method == "GET" for _, method, _ in s.calls[before:])
    assert not s.jobs.starts and not s.service.enabled_schedules()


def test_research_prompt_requires_valid_treatments_and_discards_stale_output(panel):
    s, page = panel, panel.page
    create_and_import(s)
    page.get_by_role("button", name="Import directory", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Research prompt", exact=True).click()
    treatments = dialog.get_by_label("Treatments to research", exact=True)
    generate = dialog.get_by_role("button", name="Create research prompt", exact=True)
    treatments.fill("")
    generate.click()
    expect(dialog.get_by_role("alert")).to_contain_text("Enter at least one treatment")
    for value in ("skin = Skin\nskin = Other", "bad id = Skin", "skin =", "skin"):
        treatments.fill(value)
        generate.click()
        expect(dialog.get_by_role("alert")).to_contain_text("one unique treatment-id")
        expect(dialog.get_by_role("link", name="Download research prompt", exact=True)).not_to_be_visible()
    assert not any(path.endswith("client_research_directory_prompt.txt") for path, _, _ in s.calls)
    treatments.fill("skin = Skin treatments")
    generate.click()
    prompt = dialog.get_by_label("AI directory research prompt", exact=True)
    expect(prompt).to_be_visible()
    assert "All areas inside this city's boundaries." in prompt.input_value()
    dialog.get_by_label("Areas to research (optional)", exact=True).fill("Central")
    expect(prompt).not_to_be_visible()
    generate.click()
    expect(prompt).to_be_visible()
    assert 'Area scope: ["Central"]' in prompt.input_value()
    assert sum(path.endswith("client_research_directory_prompt.txt") for path, _, _ in s.calls) == 1
    page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true})")
    dialog.get_by_role("button", name="Copy research prompt", exact=True).click()
    expect(dialog.get_by_role("status")).to_contain_text("Automatic copying is unavailable")
    assert prompt.evaluate("el => el.selectionStart === 0 && el.selectionEnd === el.value.length")
    assert not s.jobs.starts


def test_visible_copy_control_generates_and_copies_only_the_current_valid_prompt(panel):
    s, page = panel, panel.page
    create_and_import(s)
    before = len(s.calls)
    page.get_by_role("button", name="Import directory", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Research prompt", exact=True).click()
    copy = dialog.get_by_role("button", name="Copy research prompt", exact=True)
    expect(copy).to_be_visible()
    expect(copy).to_have_count(1)
    prompt = dialog.get_by_label("AI directory research prompt", exact=True)
    expect(prompt).not_to_be_visible()
    page.evaluate("""() => {
        window.researchCopiedPrompts = [];
        Object.defineProperty(navigator, 'clipboard', {configurable: true,
            value: {writeText: async text => {window.researchCopiedPrompts.push(text);}}});
    }""")

    treatments = dialog.get_by_label("Treatments to research", exact=True)
    treatments.fill("")
    copy.click()
    expect(dialog.get_by_role("alert")).to_contain_text("Enter at least one treatment")
    assert page.evaluate("window.researchCopiedPrompts") == []
    assert not any(path.endswith("client_research_directory_prompt.txt") for path, _, _ in s.calls)

    treatments.fill("skin = Skin treatments")
    copy.click()
    expect(dialog.get_by_role("status")).to_contain_text("Research prompt copied")
    first = prompt.input_value()
    assert 'City (exact CSV value): "Paris"' in first
    assert "COLUMN 12: branch_aliases" in first
    assert page.evaluate("window.researchCopiedPrompts") == [first]
    expect(dialog.get_by_role("link", name="Download research prompt", exact=True)).to_be_visible()

    dialog.get_by_label("Areas to research (optional)", exact=True).fill("East")
    copy.click()
    expect(dialog.get_by_role("status")).to_contain_text("Research prompt copied")
    second = prompt.input_value()
    assert 'Area scope: ["East"]' in second and second != first
    assert page.evaluate("window.researchCopiedPrompts") == [first, second]

    treatments.fill("skin =")
    copy.click()
    expect(dialog.get_by_role("alert")).to_contain_text("one unique treatment-id")
    expect(prompt).not_to_be_visible()
    assert page.evaluate("window.researchCopiedPrompts") == [first, second]
    assert sum(path.endswith("client_research_directory_prompt.txt") for path, _, _ in s.calls) == 1
    assert all(method == "GET" for _, method, _ in s.calls[before:])
    assert not s.jobs.starts and not s.service.enabled_schedules()


def test_research_template_load_failure_can_be_retried(panel):
    s, page = panel, panel.page
    create_and_import(s)
    page.get_by_role("button", name="Import directory", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Research prompt", exact=True).click()
    asset = "**/static/client_research_directory_prompt.txt"
    page.route(asset, lambda route: route.fulfill(status=503, body="Unavailable"))
    dialog.get_by_role("button", name="Create research prompt", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("could not load")
    expect(dialog.get_by_label("AI directory research prompt", exact=True)).not_to_be_visible()
    page.unroute(asset)
    dialog.get_by_role("button", name="Create research prompt", exact=True).click()
    expect(dialog.get_by_label("AI directory research prompt", exact=True)).to_be_visible()
    expect(dialog.get_by_role("alert")).not_to_be_visible()
    assert not s.jobs.starts


def test_import_setup_schedule_and_exact_research_run_in_standalone_panel(panel):
    s, page = panel, panel.page
    create_and_import(s)
    assert not s.jobs.starts
    page.get_by_role("button", name="Schedule settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("pre")).to_contain_text('"kind": "recommendations"')
    assert not s.service.enabled_schedules()
    dialog.get_by_role("button", name="Enable checks every 72 hours", exact=True).click()
    expect(page.locator(".cr-schedule-info")).to_contain_text("Checks every 72 hours")
    assert len(s.service.enabled_schedules()) == 1
    assert not s.jobs.starts
    page.get_by_role("button", name="Schedule settings", exact=True).click()
    page.get_by_role("button", name="Pause scheduled checks", exact=True).click()
    expect(page.locator(".cr-schedule-info")).to_contain_text("Scheduled checks paused")
    assert not s.service.enabled_schedules()

    page.get_by_role("button", name="Check now", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("pre")).to_contain_text('"judge_provider": "gemini"')
    expect(dialog.locator("pre")).to_contain_text('"kind": "recommendations"')
    assert not s.jobs.starts
    dialog.get_by_role("button", name="Start checks", exact=True).click()
    expect(page.get_by_role("button", name="Open identification run", exact=True)).to_be_visible()
    assert len(s.jobs.starts) == 1
    run_id = s.jobs.starts[0][1]["client_research"]["runner_run_id"]
    page.get_by_role("button", name="Open identification run", exact=True).click()
    expect(page.locator("#selectedRunSummary")).to_contain_text(run_id)
    expect(page.locator("#selectedRunTitle")).to_have_text("Client research identification")
    expect(page.locator("#logView")).to_have_text("Synthetic research log")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator("#testingView")).to_be_visible()
    page.get_by_role("button", name="Client research", exact=True).click()
    expect(page.locator(".cr-setup")).to_contain_text("Paris, France")
    assert len(s.jobs.starts) == 1
    page.reload()
    expect(page.locator(".cr-setup")).to_contain_text("Paris, France")
    assert len(s.jobs.starts) == 1


def test_shared_branch_contacts_notes_and_queue_filters(panel):
    s, page = panel, panel.page
    create_and_import(s)
    page.get_by_role("button", name="Alpha", exact=True).click()
    expect(page.locator(".cr-branch")).to_have_count(2)
    page.get_by_role("button", name="Contacts and history", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Note or reason").fill("Research both branches together")
    dialog.get_by_role("button", name="Save account action", exact=True).click()
    page.get_by_role("button", name="Contacts and history", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Research both branches together")
    dialog.get_by_label("Action", exact=True).select_option("save_contact")
    dialog.get_by_label("Contact value").fill("hello@alpha.example")
    dialog.get_by_role("button", name="Save account action", exact=True).click()
    page.get_by_role("button", name="Contacts and history", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("hello@alpha.example")
    dialog.get_by_label("Action", exact=True).select_option("log_external_contact")
    dialog.get_by_label("Note or reason").fill("Contact made elsewhere")
    dialog.get_by_role("button", name="Save account action", exact=True).click()
    expect(page.get_by_role("button", name="Alpha", exact=True)).to_have_count(0)
    page.get_by_label("Contact queue").select_option("all")
    page.get_by_role("button", name="Apply filters", exact=True).click()
    expect(page.locator(".cr-account-table")).to_contain_text("contacted")
    assert not s.jobs.starts


def test_saved_assessment_evidence_and_review_flag(panel):
    s, page = panel, panel.page
    s.w = s.service.create_workspace(city="Paris", country_code="FR", country_name="France")
    preview = s.service.preview_import(s.w["id"], file_name="clinics.csv", content_base64=base64.b64encode(CSV.encode()).decode())
    s.service.commit_import(s.w["id"], preview_id=preview["preview_id"], expected_workspace_version=s.w["version"], request_id="import")
    s.w = s.service.get_workspace(s.w["id"])
    s.service.configure(s.w["id"], configuration={"treatments": {"skin": "skin treatments"}}, expected_workspace_version=s.w["version"])
    s.w = s.service.get_workspace(s.w["id"])
    completed_initial(s)
    # Reading original answers uses a synthetic artifact port with verified bytes.
    from test_client_research_execution_review import accepted_result
    ports = {}
    for link in s.service.get_workspace(s.w["id"])["rounds"][0]["runs"]:
        _, ports[link["runner_run_id"]] = accepted_result(link, direct=link["kind"] == "identification")
    s.coordinator.component_factory = lambda runtime: SimpleNamespace(read_artifact=lambda run_id, ref: ports[run_id].read_artifact(run_id, ref))
    open_research(s)
    expect(page.locator(".cr-account-table")).to_contain_text("P1 · Could not identify")
    page.get_by_role("button", name="Alpha", exact=True).click()
    expect(page.locator(".cr-branch")).to_have_count(2)
    expect(page.locator(".cr-branch").first).to_contain_text("Coverage: 21/21 samples")
    page.locator(".cr-branch").first.get_by_role("button", name="View evidence", exact=True).first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("I cannot identify that exact clinic branch")
    dialog.get_by_label("Reason for review").fill("Check exact branch identity")
    dialog.get_by_role("button", name="Flag for review", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".cr-branch").first).to_contain_text("needs review")
    assert not s.jobs.starts


def test_navigation_reads_only_and_mobile_layout(panel):
    s, page = panel, panel.page
    page.goto("http://127.0.0.1:1/visibility-testing")
    expect(page.locator("#testingView")).to_be_visible()
    assert not any(path.startswith("/api/client-research/") for path, _, _ in s.calls)
    page.get_by_role("button", name="Client research", exact=True).click()
    expect(page.get_by_role("button", name="Create city workspace", exact=True)).to_be_visible()
    assert not s.jobs.starts
    assert all(method == "GET" for _, method, _ in s.calls)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name="Client research", exact=True)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.go_back()
    expect(page.locator("#testingView")).to_be_visible()
