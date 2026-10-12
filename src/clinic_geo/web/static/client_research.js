/* City research stays separate from the legacy directory and snapshot UI. */
(function () {
  "use strict";

  const API = "/api/client-research";
  const MAX_CSV_BYTES = 10 * 1024 * 1024;
  // ISO 3166-1 alpha-2 countries and territories; browser names keep this list offline.
  const COUNTRY_NAMES = new Intl.DisplayNames(["en"], { type: "region" });
  const COUNTRIES = (`AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA
    RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW`)
    .split(/\s+/).map(code => ({ code, name: COUNTRY_NAMES.of(code) }))
    .sort((a, b) => a.name.localeCompare(b.name, "en"));
  const ROUTINE_PROFILES = ["ortemksl_personal", "ortemksl_finance", "sokr4r3", "kysil_work", "loggedin_1", "loggedin_2", "loggedin_3"];
  const state = { workspaces: [], workspace: null, accounts: [], total: 0, offset: 0, limit: 50, filter: { view: "queue" }, loading: false, error: "", request: 0, expanded: new Set(), details: new Map(), poll: null };
  let dialogSequence = 0;
  let savedWorkspace = "";
  try { savedWorkspace = localStorage.getItem("clientResearch.workspace") || ""; } catch (_) { /* Storage is optional. */ }

  function root() { return document.getElementById("client-research-root"); }
  function active() { return document.getElementById("clientResearchView")?.classList.contains("active"); }
  function node(tag, text, attrs = {}) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    Object.entries(attrs).forEach(([key, value]) => {
      if (key === "class") element.className = value;
      else if (key === "hidden") element.hidden = Boolean(value);
      else if (key === "disabled") element.disabled = Boolean(value);
      else element.setAttribute(key, String(value));
    });
    return element;
  }
  function append(parent, ...children) { children.forEach(child => { if (child) parent.appendChild(child); }); return parent; }
  function button(text, action, secondary = true, disabled = false) {
    const element = node("button", text, { type: "button", class: secondary ? "secondary" : "", disabled });
    element.addEventListener("click", action);
    return element;
  }
  function id() { return window.crypto?.randomUUID?.() || `research-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  function base(workspace = state.workspace) { return `${API}/workspaces/${encodeURIComponent(workspace.id)}`; }
  function date(value) {
    if (!value) return "Not recorded";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return parsed.toLocaleString(undefined, { timeZone: state.workspace?.timezone || undefined });
  }
  function readable(value) { return String(value || "Awaiting checks").replace(/_/g, " "); }
  function priority(value) { return { P1: "P1 · Could not identify", P2: "P2 · Identified, not recommended", P3: "P3 · Recommended" }[value] || readable(value); }
  function jsonDetails(value, title = "Details") {
    const details = node("details", null, { class: "cr-details" });
    append(details, node("summary", title), node("pre", JSON.stringify(value, null, 2)));
    return details;
  }
  async function request(url, payload) {
    const options = { headers: { Accept: "application/json" }, cache: "no-store" };
    if (payload !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(payload); }
    const response = await fetch(url, options);
    let body;
    try { body = await response.json(); } catch (_) { throw new Error("The server returned an unreadable response. Refresh and try again."); }
    if (!response.ok) {
      const diagnostics = Array.isArray(body.issues) ? body.issues.map(issue => `${issue.line_number ? `Line ${issue.line_number}: ` : ""}${issue.field}: ${issue.message}`).join("\n") : "";
      const error = new Error([body.error || `Request failed (${response.status})`, diagnostics].filter(Boolean).join("\n"));
      error.status = response.status; error.code = body.code; throw error;
    }
    return body;
  }
  function showError(error) { state.error = error.message || String(error); render(); }
  function field(parent, text, value = "", type = "text", required = false) {
    const label = node("label", text);
    const input = node(type === "textarea" ? "textarea" : "input", null, type === "textarea" ? {} : { type });
    input.value = value || ""; input.required = required;
    if (type === "textarea") input.rows = 4;
    append(label, input); parent.appendChild(label); return input;
  }
  function select(parent, text, options, selected = "") {
    const label = node("label", text), input = node("select", null, { "aria-label": text });
    options.forEach(([value, name]) => { const option = node("option", name, { value }); option.selected = value === selected; input.appendChild(option); });
    append(label, input); parent.appendChild(label); return input;
  }
  function modal(title, description = "") {
    const returnFocus = document.activeElement;
    const returnText = returnFocus?.textContent;
    const dialog = node("dialog", null, { class: "cr-dialog", "aria-labelledby": `cr-dialog-${++dialogSequence}` });
    const header = node("header", null, { class: "cr-dialog-head" });
    const heading = node("h2", title, { id: `cr-dialog-${dialogSequence}` });
    const close = button("Close", () => dialog.close());
    append(header, heading, close);
    const content = node("div", null, { class: "cr-dialog-content" });
    const error = node("p", "", { class: "cr-error", role: "alert", hidden: true });
    append(dialog, header, description ? node("p", description, { class: "cr-help" }) : null, content, error);
    document.body.appendChild(dialog);
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (returnFocus?.isConnected) returnFocus.focus();
      else if (returnText) Array.from(root()?.querySelectorAll("button") || []).find(item => item.textContent === returnText)?.focus();
    });
    dialog.showModal();
    return { dialog, content, error, fail(exc) { error.textContent = exc.message || String(exc); error.hidden = false; }, clear() { error.hidden = true; } };
  }
  function form(modal, submitText, handler) {
    const element = node("form", null, { class: "cr-form" });
    const fields = node("div", null, { class: "cr-form-fields" });
    const actions = node("div", null, { class: "cr-actions" });
    const submit = node("button", submitText, { type: "submit" });
    append(actions, submit); append(element, fields, actions); modal.content.appendChild(element);
    element.addEventListener("submit", async event => {
      event.preventDefault(); if (submit.disabled) return;
      submit.disabled = true; modal.clear();
      try { await handler(); } catch (error) { modal.fail(error); } finally { submit.disabled = false; }
    });
    return { element, fields, actions, submit };
  }

  async function refresh(force = false) {
    if (state.loading && force !== true) return;
    const requestNumber = ++state.request;
    state.loading = true;
    if (!state.workspaces.length) render();
    try {
      const body = await request(`${API}/workspaces`);
      if (requestNumber !== state.request) return;
      state.workspaces = Array.isArray(body.workspaces) ? body.workspaces : body.workspaces?.items || [];
      const selected = state.workspace?.id || savedWorkspace;
      const summary = state.workspaces.find(workspace => workspace.id === selected) || state.workspaces[0];
      if (summary) {
        const workspace = (await request(`${API}/workspaces/${encodeURIComponent(summary.id)}`)).workspace;
        const accounts = (await request(`${base(workspace)}/accounts?${new URLSearchParams({ ...state.filter, limit: state.limit, offset: state.offset })}`)).accounts;
        if (requestNumber !== state.request) return;
        state.workspace = workspace;
        state.accounts = Array.isArray(accounts) ? accounts : accounts.items || [];
        state.total = Array.isArray(accounts) ? accounts.length : accounts.total || 0;
        state.details.clear();
        await Promise.all(Array.from(state.expanded).map(async suppliedId => {
          try { const detail = (await request(`${base(workspace)}/accounts/${encodeURIComponent(suppliedId)}`)).account; if (requestNumber === state.request) state.details.set(suppliedId, detail); }
          catch (_) { if (requestNumber === state.request) state.expanded.delete(suppliedId); }
        }));
        if (requestNumber !== state.request) return;
        savedWorkspace = workspace.id;
        try { localStorage.setItem("clientResearch.workspace", savedWorkspace); } catch (_) { /* Storage is optional. */ }
      } else { state.workspace = null; state.accounts = []; state.total = 0; }
      state.error = "";
    } catch (error) { if (requestNumber === state.request) state.error = error.message; }
    finally { if (requestNumber === state.request) { state.loading = false; render(); } }
  }
  async function switchWorkspace(value) {
    ++state.request;
    state.loading = false;
    state.workspace = { id: value }; savedWorkspace = value;
    state.filter = { view: "queue" };
    state.offset = 0; state.expanded.clear(); state.details.clear();
    await refresh();
  }
  function createWorkspace() {
    const m = modal("Create a city workspace", "Use one city and country per directory. You can switch workspaces to research another city.");
    let city, country, language, timezone, name;
    const f = form(m, "Create workspace", async () => {
      const selectedCountry = COUNTRIES.find(item => item.code === country.value);
      if (!selectedCountry) throw new Error("Choose a country.");
      const payload = { city: city.value.trim(), country_code: selectedCountry.code, country_name: selectedCountry.name, language: language.value.trim(), timezone: timezone.value.trim(), name: name.value.trim() };
      const result = await request(`${API}/workspaces`, payload);
      state.workspace = result.workspace; state.offset = 0; state.expanded.clear(); await refresh(true); m.dialog.close();
    });
    name = field(f.fields, "Workspace name", "");
    city = field(f.fields, "City", "", "text", true);
    country = select(f.fields, "Country", [["", "Choose a country"], ...COUNTRIES.map(item => [item.code, `${item.name} (${item.code})`])]);
    country.required = true;
    language = field(f.fields, "Prompt language", "en", "text", true);
    timezone = field(f.fields, "Timezone", Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", "text", true);
  }
  function directoryResearchTemplate(parent, workspace, m) {
    const details = node("section", null, { class: "cr-research-template", id: `${m.dialog.getAttribute("aria-labelledby")}-research-prompt`, "aria-label": "Research prompt setup", hidden: true });
    const toggle = button("Research prompt", () => {
      details.hidden = !details.hidden;
      toggle.setAttribute("aria-expanded", String(!details.hidden));
    });
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", details.id);
    append(details, node("p", `Create a research prompt for ${workspace.city}, ${workspace.country_name || workspace.country_code}. It includes exact column-by-column formats, required values, allowed characters and CSV examples. Choose the treatments first, then copy or download it for a chatbot with web research. The chatbot will produce the clinic CSV and a separate file of sources to review.`, { class: "cr-help" }));
    const savedTreatments = Object.entries(workspace.configuration?.treatments || {}).filter(([, label]) => label.trim()).map(([key, label]) => `${key} = ${label}`).join("\n");
    const treatments = field(details, "Treatments to research", savedTreatments, "textarea");
    treatments.rows = 3;
    treatments.placeholder = "laser-hair-removal = Laser hair removal\nbotox = Botox treatment";
    append(details, node("p", "One treatment-id = treatment name per line. These IDs go in the CSV and are used for the later checks.", { class: "cr-help" }));
    const areas = field(details, "Areas to research (optional)", "", "textarea");
    areas.rows = 2;
    areas.placeholder = "Leave blank for all areas in this city, or list one area per line.";
    const notice = node("p", "", { class: "cr-error", role: "alert", hidden: true });
    const output = node("div", null, { class: "cr-form-fields", hidden: true });
    const prompt = field(output, "AI directory research prompt", "", "textarea");
    prompt.readOnly = true; prompt.rows = 12;
    let downloadUrl = null, template = null;
    const status = node("p", "", { class: "cr-help", role: "status" });
    const download = node("a", "Download research prompt", { download: "clinic-directory-research-prompt.txt" });
    const copy = button("Copy research prompt", async () => {
      if (!prompt.value && !(await createPrompt())) return;
      copy.disabled = true;
      try { await navigator.clipboard.writeText(prompt.value); status.textContent = "Research prompt copied. Paste it into your AI chatbot."; }
      catch (_) { prompt.focus(); prompt.select(); status.textContent = "Automatic copying is unavailable. The prompt is selected; copy it and paste it into your AI chatbot."; }
      finally { copy.disabled = false; }
    });
    append(output, append(node("div", null, { class: "cr-actions" }), download), status);
    function invalidate() {
      output.hidden = true; prompt.value = ""; notice.hidden = true; status.textContent = "";
      if (downloadUrl) { URL.revokeObjectURL(downloadUrl); downloadUrl = null; }
      download.removeAttribute("href");
    }
    async function createPrompt() {
      invalidate();
      const treatmentInput = treatments.value, areaInput = areas.value;
      const lines = treatmentInput.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      const ids = new Set();
      for (const line of lines) {
        const separator = line.indexOf("="), key = line.slice(0, separator).trim(), label = line.slice(separator + 1).trim();
        if (separator < 1 || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key) || !label || ids.has(key)) {
          notice.textContent = "Use one unique treatment-id = treatment name per line. IDs can contain letters, numbers, dots, underscores, colons and hyphens (up to 128 characters).";
          notice.hidden = false; return false;
        }
        ids.add(key);
      }
      if (!lines.length) { notice.textContent = "Enter at least one treatment to research."; notice.hidden = false; treatments.focus(); return false; }
      generate.disabled = true; copy.disabled = true;
      try {
        if (!template) {
          const response = await fetch("/static/client_research_directory_prompt.txt", { cache: "no-store" });
          if (!response.ok) throw new Error("The research template could not load. Try again.");
          template = await response.text();
        }
        if (!m.dialog.isConnected || treatments.value !== treatmentInput || areas.value !== areaInput) return false;
        const values = { CITY: JSON.stringify(workspace.city), COUNTRY_NAME: JSON.stringify(workspace.country_name || workspace.country_code), COUNTRY_CODE: JSON.stringify(workspace.country_code), LANGUAGE: JSON.stringify(workspace.language), AREAS: areaInput.trim() ? JSON.stringify(areaInput.split(/\r?\n/).map(area => area.trim()).filter(Boolean)) : "All areas inside this city's boundaries.", TREATMENTS: lines.join("\n") };
        prompt.value = template.replace(/\{\{([A-Z_]+)\}\}/g, (_, key) => {
          if (!(key in values)) throw new Error("The research template has an unsupported field. Refresh and try again.");
          return values[key];
        });
        downloadUrl = URL.createObjectURL(new Blob([prompt.value], { type: "text/plain;charset=utf-8" }));
        download.href = downloadUrl; output.hidden = false;
        return true;
      } catch (error) { notice.textContent = error.message; notice.hidden = false; return false; }
      finally { generate.disabled = false; copy.disabled = false; }
    }
    const generate = button("Create research prompt", createPrompt);
    treatments.addEventListener("input", invalidate); areas.addEventListener("input", invalidate);
    m.dialog.addEventListener("close", invalidate);
    append(details, append(node("div", null, { class: "cr-actions" }), generate, copy), notice, output);
    parent.appendChild(details);
    return toggle;
  }
  function importDirectory() {
    const workspace = state.workspace;
    const m = modal("Import clinic directory", "Upload the full clinic list for this city. Each upload replaces the previous list.");
    const explanation = node("div", null, { class: "cr-import-explanation" });
    append(explanation, node("p", "Include every branch you still want to check. Branches missing from the new file will stop receiving checks. Their old results, contacts and history stay saved."), node("p", "You'll see what will change before you save."));
    m.content.appendChild(explanation);
    const template = node("a", "Download CSV template", { href: `${API}/directory-template`, download: "client-search-directory-template.csv" });
    const tools = node("div", null, { class: "cr-actions" });
    m.content.appendChild(tools);
    append(tools, template, directoryResearchTemplate(m.content, workspace, m));
    let selectedFile, uploadPayload, preview, mappingInput;
    const requestId = id();
    const f = form(m, "Preview import", async () => {
      if (!selectedFile) throw new Error("Choose the CSV file first.");
      if (selectedFile.size > MAX_CSV_BYTES) throw new Error("Choose a CSV of 10 MiB or less.");
      const bytes = new Uint8Array(await selectedFile.arrayBuffer());
      let binary = "";
      for (let start = 0; start < bytes.length; start += 8192) binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
      let accountMappings = {};
      if (mappingInput.value.trim()) {
        try { accountMappings = JSON.parse(mappingInput.value); } catch (_) { throw new Error("Account links must be a JSON object of supplied IDs to existing account IDs."); }
      }
      uploadPayload = { file_name: selectedFile.name, content_base64: btoa(binary), base_directory_revision: workspace.directory_revision || null, account_mappings: accountMappings };
      preview = (await request(`${base(workspace)}/imports/preview`, uploadPayload)).preview;
      result.replaceChildren();
      append(result, node("h3", "Review the import"), node("p", `${preview.account_count || 0} accounts and ${preview.branch_count || 0} branches. ${preview.duplicate_rows || 0} exact duplicate rows collapsed.`));
      Object.entries(preview.diff || {}).forEach(([kind, changes]) => {
        const section = node("section", null, { class: "cr-import-changes" });
        section.appendChild(node("h3", kind === "accounts" ? "Buying accounts" : "Physical branches"));
        for (const category of ["added", "changed", "retired", "reactivated", "unchanged"]) {
          const values = changes[category] || [];
          const label = { retired: "No longer in this file", reactivated: "Returning to the list" }[category] || readable(category);
          append(section, node("p", `${label}: ${values.length}${values.length ? ` (${values.join(", ")})` : ""}`));
        }
        result.appendChild(section);
      });
      append(result, node("p", "Check the branches leaving the list and any account links before saving. AI checks start only when you choose to run them.", { class: "cr-help" }), jsonDetails(preview, "File receipt and account links"));
      const commit = button("Import reviewed directory", async () => {
        commit.disabled = true; m.clear();
        try {
          await request(`${base(workspace)}/imports/commit`, { preview_id: preview.preview_id || preview.id, expected_workspace_version: preview.expected_workspace_version ?? preview.workspace_version ?? workspace.version, request_id: requestId, account_mappings: uploadPayload.account_mappings });
          state.offset = 0;
          await refresh(true); m.dialog.close();
        } catch (error) { m.fail(error); commit.disabled = false; }
      }, false, Boolean(preview.conflicts?.length || preview.valid === false));
      result.appendChild(commit);
    });
    const file = field(f.fields, "Directory CSV", "", "file", true); file.accept = ".csv,text/csv";
    file.addEventListener("change", () => { selectedFile = file.files[0]; preview = null; result.replaceChildren(); });
    const mappings = node("details"); mappings.appendChild(node("summary", "Link accounts already in this workspace database (optional)"));
    mappingInput = field(mappings, "Supplied account ID to existing internal account ID", "", "textarea");
    mappingInput.placeholder = '{"supplied-account-id":"internal-account-id"}';
    mappingInput.addEventListener("input", () => { preview = null; result.replaceChildren(); });
    f.fields.appendChild(mappings);
    const result = node("div", null, { class: "cr-import-preview" }); m.content.appendChild(result);
  }
  function configure() {
    const workspace = state.workspace, existing = workspace.configuration || {};
    const m = modal("Configure research questions", "Confirm treatment wording and selected area/treatment pairs. Each pair gets one shared question; clinic names stay out of recommendation questions.");
    const treatmentInputs = new Map(), cellInputs = [], profileInputs = [];
    let source, judge, recommendationTemplate, sourceTemplate, directDays, recommendationHours;
    const f = form(m, "Save configuration", async () => {
      const treatments = Object.fromEntries(Array.from(treatmentInputs, ([treatmentId, input]) => [treatmentId, input.value.trim()]));
      const cells = cellInputs.filter(item => item.input.checked).map(item => ({ ...item.cell, treatment_label: treatments[item.cell.treatment_id] }));
      const configuration = { treatments, cells, source_provider: source.value, judge_provider: judge.value, providers: ["chatgpt", "gemini", "copilot"], profile_ids: profileInputs.filter(item => item.input.checked).map(item => item.profile), source_template: sourceTemplate.value, recommendation_template: recommendationTemplate.value, direct_freshness_days: Number(directDays.value), recommendation_freshness_hours: Number(recommendationHours.value) };
      if (source.value === judge.value) throw new Error("Choose different chatbots for the source and judge.");
      if (!configuration.profile_ids.length) throw new Error("Select at least one approved browser profile.");
      await request(`${base(workspace)}/configuration`, { configuration, expected_workspace_version: workspace.version });
      await refresh(true); m.dialog.close();
    });
    const treatments = node("fieldset"); treatments.appendChild(node("legend", "Treatment wording"));
    (workspace.treatment_ids || Object.keys(existing.treatments || {})).forEach(treatmentId => {
      treatmentInputs.set(treatmentId, field(treatments, treatmentId, existing.treatments?.[treatmentId] || "", "text", true));
    });
    f.fields.appendChild(treatments);
    const cells = workspace.available_cells || existing.cells || [];
    const selected = new Set((existing.cells || []).map(cell => `${cell.area_id || cell.area}\u0000${cell.treatment_id}`));
    const scope = node("fieldset"); scope.appendChild(node("legend", "Area and treatment pairs"));
    cells.forEach(cell => {
      const label = node("label", null, { class: "cr-checkbox" }), input = node("input", null, { type: "checkbox" });
      input.checked = selected.has(`${cell.area_id || cell.area}\u0000${cell.treatment_id}`);
      append(label, input, node("span", `${cell.area} — ${cell.treatment_id}`)); scope.appendChild(label); cellInputs.push({ input, cell });
    });
    if (!cells.length) scope.appendChild(node("p", "Import a directory before selecting research pairs."));
    f.fields.appendChild(scope);
    const providerChoices = [["chatgpt", "ChatGPT"], ["gemini", "Gemini"], ["copilot", "Copilot"]];
    source = select(f.fields, "Identification source", providerChoices, existing.source_provider || "chatgpt");
    judge = select(f.fields, "Identification judge", providerChoices, existing.judge_provider || "gemini");
    const profiles = node("fieldset"); profiles.appendChild(node("legend", "Approved browser profiles"));
    const profileIds = workspace.allowed_profile_ids || ROUTINE_PROFILES;
    profileIds.forEach(profile => {
      const label = node("label", null, { class: "cr-checkbox" }), input = node("input", null, { type: "checkbox" });
      input.checked = (existing.profile_ids || ROUTINE_PROFILES).includes(profile);
      input.disabled = true;
      append(label, input, node("span", profile)); profiles.appendChild(label); profileInputs.push({ input, profile });
    });
    f.fields.appendChild(profiles);
    const wording = node("details"); wording.appendChild(node("summary", "Question wording and freshness"));
    wording.appendChild(node("p", "Edit these saved templates for the workspace language. Keep the placeholders; each preview shows the exact rendered questions.", { class: "cr-help" }));
    sourceTemplate = field(wording, "Named identification question", existing.source_template || "", "textarea", true);
    recommendationTemplate = field(wording, "Unbranded recommendation question", existing.recommendation_template || "", "textarea", true);
    directDays = field(wording, "Identification freshness (days)", String(existing.direct_freshness_days || 30), "number", true); directDays.min = "1";
    recommendationHours = field(wording, "Recommendation freshness (hours)", String(existing.recommendation_freshness_hours || 144), "number", true); recommendationHours.min = "1";
    f.fields.appendChild(wording);
  }
  function previewChecks(mode = "initial", schedule = false) {
    const workspace = state.workspace;
    const m = modal(schedule ? "Enable three-day recommendation checks" : "Preview checks", schedule ? "Every 72 hours, the local server checks the reviewed areas and treatments. Your computer and the local server must be running. Identification refresh stays manual." : "Review the exact questions, profiles and message counts before starting. Starting a check uses the installed visibility runner.");
    const status = node("p", "Preparing preview…", { role: "status" }); m.content.appendChild(status);
    const requestId = id();
    request(`${base(workspace)}/checks/preview`, { mode: schedule ? "recommendations" : mode }).then(body => {
      const preview = body.preview;
      status.textContent = "Review the planned work";
      append(m.content, jsonDetails(preview, "Questions, profile assignments and counts"));
      m.content.querySelector("details").open = true;
      const start = button(schedule ? "Enable checks every 72 hours" : "Start checks", async () => {
        start.disabled = true; m.clear();
        try {
          const payload = { preview_id: preview.preview_id || preview.id, expected_fingerprint: preview.fingerprint, expected_workspace_version: preview.expected_workspace_version ?? preview.workspace_version ?? workspace.version, request_id: requestId };
          if (schedule) payload.enabled = true;
          const response = await request(`${base(workspace)}/${schedule ? "schedule" : "checks/start"}`, payload);
          await refresh(true); m.dialog.close();
          if (!schedule && response.start?.round_id) showRound(response.start.round_id);
        } catch (error) { m.fail(error); start.disabled = false; }
      }, false, preview.executable === false || Boolean(preview.errors?.length));
      m.content.appendChild(start);
    }).catch(error => { status.textContent = "Preview unavailable"; m.fail(error); });
  }
  function scheduleSettings() {
    const workspace = state.workspace, schedule = workspace.schedule || {};
    if (schedule.enabled || schedule.status === "enabled" || schedule.status === "active") {
      const m = modal("Schedule settings", "Pausing stops future checks. Work already admitted keeps its existing controls.");
      append(m.content, node("p", `Next check: ${date(schedule.next_due_at || schedule.next_due)}`));
      const requestId = id();
      const pause = button("Pause scheduled checks", async () => {
        pause.disabled = true;
        try { await request(`${base(workspace)}/schedule`, { enabled: false, expected_workspace_version: workspace.version, request_id: requestId }); await refresh(true); m.dialog.close(); }
        catch (error) { m.fail(error); pause.disabled = false; }
      }, false);
      m.content.appendChild(pause);
    } else previewChecks("recommendations", true);
  }
  async function expandAccount(account) {
    const workspace = state.workspace;
    const suppliedId = account.supplied_id || account.account_id;
    if (state.expanded.has(suppliedId)) { state.expanded.delete(suppliedId); render(); return; }
    try {
      const detail = (await request(`${base(workspace)}/accounts/${encodeURIComponent(suppliedId)}`)).account;
      if (state.workspace?.id !== workspace.id) return;
      state.expanded.add(suppliedId); state.details.set(suppliedId, detail); render();
    } catch (error) { showError(error); }
  }
  function accountActions(account) {
    const workspace = state.workspace, suppliedId = account.supplied_id || account.account_id;
    const m = modal(account.account_name || "Account", "Contacts and history are shared across this account’s branches. Record contact already performed elsewhere.");
    let action, branch, channel, value, notes, occurred, contactChoice;
    const requestId = id();
    const f = form(m, "Save account action", async () => {
      const payload = { action: action.value, expected_account_version: account.version, request_id: requestId };
      if (branch.value) payload.branch_id = branch.value;
      if (["save_contact", "log_external_contact", "log_external_reply"].includes(action.value)) payload.channel = channel.value;
      if (action.value === "save_contact") {
        payload.value = value.value.trim();
        let existingContact = (account.contacts || []).find(contact => contact.id === contactChoice.value);
        if (!existingContact) {
          const normalized = channel.value === "email" ? value.value.trim().toLowerCase() : ["phone", "sms", "whatsapp"].includes(channel.value) ? value.value.replace(/[ ()\-.]/g, "") : value.value.trim();
          existingContact = (account.contacts || []).find(contact => contact.channel === channel.value && contact.normalized_value === normalized);
        }
        if (existingContact) { payload.contact_id = existingContact.id; payload.expected_contact_version = existingContact.version; }
      }
      if (["suppress", "unsuppress"].includes(action.value)) payload.reason = notes.value.trim();
      else if (notes.value.trim()) payload.notes = notes.value.trim();
      if (["log_external_contact", "log_external_reply"].includes(action.value) && occurred.value) payload.occurred_at = new Date(occurred.value).toISOString();
      await request(`${base(workspace)}/accounts/${encodeURIComponent(suppliedId)}/actions`, payload);
      await refresh(true); m.dialog.close();
    });
    action = select(f.fields, "Action", [["add_note", "Add note"], ["save_contact", "Save contact"], ["log_external_contact", "Log contact performed elsewhere"], ["log_external_reply", "Log received reply"], ["suppress", "Suppress account"], ["unsuppress", "Remove suppression"]]);
    branch = select(f.fields, "Relevant branch (optional)", [["", "Whole account"], ...(account.branches || []).map(item => [item.branch_id || item.supplied_id, item.branch_name])]);
    contactChoice = select(f.fields, "Saved contact", [["", "New contact"], ...(account.contacts || []).filter(contact => !contact.branch_id).map(contact => [contact.id, `${contact.channel}: ${contact.value}`])]);
    channel = select(f.fields, "Contact channel", [["email", "Email"], ["phone", "Phone"], ["sms", "SMS"], ["whatsapp", "WhatsApp"], ["linkedin", "LinkedIn"], ["form", "Contact form"], ["other", "Other"]]);
    value = field(f.fields, "Contact value", "");
    notes = field(f.fields, "Note or reason", "", "textarea");
    occurred = field(f.fields, "Contact or reply date (computer timezone)", "", "datetime-local");
    const hint = node("p", "Save phone, SMS and WhatsApp numbers with an explicit +country code. New contacts remain unverified.", { class: "cr-help" }); f.fields.appendChild(hint);
    function updateFields() {
      const save = action.value === "save_contact", external = ["log_external_contact", "log_external_reply"].includes(action.value);
      channel.parentElement.hidden = !save && !external; value.parentElement.hidden = !save; value.required = save;
      contactChoice.parentElement.hidden = !save;
      occurred.parentElement.hidden = !external; notes.required = action.value !== "save_contact";
    }
    contactChoice.addEventListener("change", () => { const contact = (account.contacts || []).find(item => item.id === contactChoice.value); if (contact) { channel.value = contact.channel; value.value = contact.value; notes.value = contact.notes || ""; } });
    action.addEventListener("change", updateFields); updateFields();
    if (account.contacts?.length) append(m.content, jsonDetails(account.contacts, "Saved contacts"));
    if (account.interactions?.length) append(m.content, jsonDetails(account.interactions, "Contact history and notes"));
  }
  async function showEvidence(evidenceId) {
    const m = modal("Research evidence", "Answers and judgments are retained as collected. Flagging an item excludes it from priority decisions; it does not edit the answer.");
    const loading = node("p", "Loading evidence…"); m.content.appendChild(loading);
    try {
      const evidence = (await request(`${API}/evidence/${encodeURIComponent(evidenceId)}`)).evidence;
      loading.remove();
      const metadata = { ...evidence }; ["source_prompt", "source_answer", "judge_request", "judge_prompt", "judge_answer", "classification_answer", "prompt", "answer", "raw_answer"].forEach(key => delete metadata[key]);
      append(m.content, jsonDetails(metadata, "Provider, dates and frozen identity"));
      for (const [key, title] of [["source_prompt", "Source question"], ["source_answer", "Source answer"], ["judge_request", "Judge question"], ["judge_prompt", "Judge question"], ["classification_answer", "Saved classification answer"], ["prompt", "Recommendation question"], ["answer", "Recommendation answer"], ["raw_answer", "Full saved answer"]]) {
        if (evidence[key]) append(m.content, node("h3", title), node("pre", evidence[key], { class: "cr-evidence-text" }));
      }
      const reason = field(m.content, "Reason for review", "", "textarea");
      const flag = button("Flag for review", async () => {
        if (!reason.value.trim()) { m.fail(new Error("Explain why this evidence needs review.")); return; }
        flag.disabled = true;
        try { await request(`${API}/evidence/${encodeURIComponent(evidenceId)}/review`, { reason: reason.value.trim(), actor: "user" }); await refresh(true); m.dialog.close(); }
        catch (error) { m.fail(error); flag.disabled = false; }
      }); m.content.appendChild(flag);
    } catch (error) { loading.textContent = "Evidence unavailable"; m.fail(error); }
  }
  async function showRound(roundId) {
    const m = modal("Check progress", "Failed or unclear observations remain visible and do not count as an absence. Use the existing run controls for recovery or verification.");
    const content = node("div"); m.content.appendChild(content);
    let timer;
    async function update() {
      try {
        const round = (await request(`${API}/rounds/${encodeURIComponent(roundId)}`)).round;
        if (!m.dialog.isConnected) return;
        content.replaceChildren();
        const currentState = round.current_state || round.status || round.state;
        append(content, node("h3", readable(currentState)), jsonDetails(round, "Coverage, child runs and errors"));
        const children = round.runs || round.children || round.run_links || [];
        children.forEach(child => {
          const jobId = child.job_id || child.host_job_id;
          if (jobId) {
            append(content, node("p", `${readable(child.kind || child.capability)}: ${readable(child.result?.status || child.status || child.state || child.cursor)} (${jobId})`), button(`Open ${readable(child.kind || child.capability)} run`, () => {
              m.dialog.close();
              document.dispatchEvent(new CustomEvent("client-research:open-run", { detail: { jobId } }));
            }));
          }
        });
        const history = node("a", "Open run history for Resume, Refetch, Cancel and verification", { href: "#testing" });
        history.addEventListener("click", () => m.dialog.close());
        content.appendChild(history);
        if (["queued", "running", "waiting_attention"].includes(currentState)) timer = setTimeout(update, 5000);
      } catch (error) { m.fail(error); }
    }
    m.dialog.addEventListener("close", () => clearTimeout(timer)); await update();
  }

  function setupStrip(container, workspace) {
    const setup = node("section", null, { class: "cr-setup", "aria-label": "Research setup" });
    const facts = node("div", null, { class: "cr-setup-facts" });
    append(facts, node("h2", `${workspace.city}, ${workspace.country_name || workspace.country_code}`), node("p", `${workspace.branch_count || 0} ${workspace.branch_count === 1 ? "branch" : "branches"} in ${workspace.account_count || 0} ${workspace.account_count === 1 ? "account" : "accounts"}`), node("p", `Language: ${workspace.language} · Timezone: ${workspace.timezone}`), node("p", `Directory: ${workspace.directory_revision ? "Imported" : "Not imported"}${workspace.imported_at ? ` ${date(workspace.imported_at)}` : ""}`));
    const schedule = workspace.schedule || {};
    const enabled = schedule.enabled || ["enabled", "active"].includes(schedule.status);
    const scheduleInfo = node("div", null, { class: "cr-schedule-info" });
    append(scheduleInfo, node("strong", enabled ? "Checks every 72 hours" : "Scheduled checks paused"), node("p", enabled ? `Next due: ${date(schedule.next_due_at || schedule.next_due)}` : "Enable after reviewing recommendation scope."));
    if (schedule.overdue) scheduleInfo.appendChild(node("p", "Overdue: start the local server to continue.", { class: "cr-warning" }));
    if (schedule.pause_reason) scheduleInfo.appendChild(node("p", readable(schedule.pause_reason), { class: "cr-warning" }));
    const actions = node("div", null, { class: "cr-actions" });
    append(actions, button("Import directory", importDirectory), button("Configure questions", configure, true, !workspace.directory_revision), button("Check now", () => previewChecks("initial"), false, !workspace.config_revision), button("Refresh identification", () => previewChecks("identification"), true, !workspace.config_revision), button("Schedule settings", scheduleSettings, true, !workspace.config_revision));
    append(setup, facts, scheduleInfo, actions);
    if (workspace.capabilities?.errors?.length || workspace.runner_error) setup.appendChild(node("p", workspace.runner_error || workspace.capabilities.errors.join("\n"), { class: "cr-warning" }));
    const rounds = workspace.rounds || [];
    if (rounds.length) {
      const latest = rounds[0];
      append(setup, node("p", `Latest check: ${readable(latest.current_state || latest.status || latest.state)} · ${date(latest.created_at)}`), button("View latest check", () => showRound(latest.id || latest.round_id)));
    }
    container.appendChild(setup);
  }
  function filters(container) {
    const formElement = node("form", null, { class: "cr-filters", "aria-label": "Filter accounts" });
    const q = field(formElement, "Search accounts", state.filter.q || "", "search");
    const view = select(formElement, "Contact queue", [["queue", "Uncontacted accounts"], ["all", "All accounts"]], state.filter.view || "queue");
    const p = select(formElement, "Priority", [["", "All priorities"], ["P1", "P1 · Could not identify"], ["P2", "P2 · Identified, not recommended"], ["P3", "P3 · Recommended"]], state.filter.priority || "");
    const area = select(formElement, "Area", [["", "All areas"], ...(state.workspace.areas || []).map(value => [value, value])], state.filter.area || "");
    const treatment = select(formElement, "Treatment", [["", "All treatments"], ...(state.workspace.treatment_ids || []).map(value => [value, state.workspace.configuration?.treatments?.[value] || value])], state.filter.treatment || "");
    const evidenceState = select(formElement, "Evidence", [["", "All evidence states"], ["awaiting_checks", "Awaiting recommendation checks"], ["awaiting_identification", "Awaiting identification"], ["needs_review", "Needs review"], ["needs_refresh", "Needs refresh"], ["outside_selected_scope", "Outside selected scope"]], state.filter.state || "");
    const contact = select(formElement, "Contact status", [["", "All contact states"], ["uncontacted", "Uncontacted"], ["contacted", "Contacted"], ["replied", "Replied"], ["suppressed", "Suppressed"], ["closed", "Closed"], ["held", "Held"]], state.filter.contact_status || "");
    formElement.appendChild(node("button", "Apply filters", { type: "submit", class: "secondary" }));
    formElement.addEventListener("submit", event => { event.preventDefault(); state.filter = { q: q.value.trim(), view: view.value, priority: p.value, area: area.value, treatment: treatment.value, state: evidenceState.value, contact_status: contact.value }; state.offset = 0; refresh(); });
    container.appendChild(formElement);
  }
  function evidenceButtons(parent, branch) {
    const ids = new Set();
    for (const key of ["evidence_ids", "identification_evidence_ids", "recommendation_evidence_ids"]) (branch[key] || []).forEach(value => ids.add(typeof value === "string" ? value : value.id));
    for (const key of ["identification", "recommendation", "direct"]) {
      if (branch[key]?.evidence_id) ids.add(branch[key].evidence_id);
      (branch[key]?.evidence_ids || []).forEach(value => ids.add(value));
    }
    (branch.evidence || []).forEach(value => ids.add(typeof value === "string" ? value : value.id || value.evidence_id));
    ids.forEach(value => { if (value) parent.appendChild(button("View evidence", () => showEvidence(value))); });
  }
  function branchRows(tableBody, account) {
    const suppliedId = account.supplied_id || account.account_id;
    const detail = state.details.get(suppliedId);
    if (!detail) return;
    const row = node("tr", null, { class: "cr-branch-row" }), cell = node("td", null, { colspan: "6" });
    const branches = node("div", null, { class: "cr-branches" });
    (detail.branches || []).forEach(branch => {
      const branchElement = node("section", null, { class: "cr-branch" });
      append(branchElement, node("h3", branch.branch_name), node("p", `${branch.area || ""} · ${(branch.treatment_ids || []).map(treatmentId => state.workspace.configuration?.treatments?.[treatmentId] || treatmentId).join(", ")}`), node("strong", priority(branch.priority || branch.state)));
      if (branch.priority) branchElement.appendChild(node("p", branch.reason || "Checks have not established a priority."));
      const signals = node("div", null, { class: "cr-signals" });
      const identification = branch.identification || branch.direct || {};
      const recommendation = branch.recommendation || branch.recommendations || {};
      append(signals, node("p", `Identification: ${readable(identification.decision || identification.state || branch.identification_status)}`), node("p", `Recommendation: ${readable(recommendation.state || recommendation.status || branch.recommendation_status)}`));
      if (recommendation.required_count !== undefined) signals.appendChild(node("p", `Coverage: ${recommendation.accepted_count || 0}/${recommendation.required_count} samples`));
      if (identification.collected_at) signals.appendChild(node("p", `Identification checked: ${date(identification.collected_at)}`));
      if (recommendation.collected_at) signals.appendChild(node("p", `Recommendations checked: ${date(recommendation.collected_at)}`));
      if (branch.potential_p1) signals.appendChild(node("p", "Potential P1; recommendation coverage is still pending.", { class: "cr-warning" }));
      if (branch.repeated_gap) signals.appendChild(node("p", "No recommendation in two comparable complete rounds."));
      if (recommendation.latest_check_incomplete) signals.appendChild(node("p", "The latest check has incomplete coverage.", { class: "cr-warning" }));
      append(branchElement, signals); evidenceButtons(branchElement, branch); branches.appendChild(branchElement);
    });
    append(cell, branches, button("Contacts and history", () => accountActions(detail))); row.appendChild(cell); tableBody.appendChild(row);
  }
  function accountTable(container) {
    const panel = node("section", null, { class: "cr-account-panel" });
    append(panel, node("h2", "Accounts to contact"), node("p", "Assess individual branches and contact their buying account. P1 and P2 require complete recommendation coverage.", { class: "cr-help" }));
    if (!state.accounts.length) {
      append(panel, node("p", state.workspace.directory_revision ? "No accounts match these filters. Choose All accounts to include contacted and suppressed accounts." : "Import your clinic directory to begin. Use one row per physical branch and repeat the account ID for its branches.", { class: "cr-empty" }));
      container.appendChild(panel); return;
    }
    const wrapper = node("div", null, { class: "cr-table-scroll" }), table = node("table", null, { class: "cr-account-table" });
    const head = node("thead"), header = node("tr");
    ["Account / branches", "Research priority", "Identification", "Recommendation", "Contact", "Evidence date"].forEach(text => header.appendChild(node("th", text, { scope: "col" })));
    head.appendChild(header); const body = node("tbody"); append(table, head, body);
    state.accounts.forEach(account => {
      const row = node("tr");
      const identity = node("td"), suppliedId = account.supplied_id || account.account_id;
      const expand = button(account.account_name, () => expandAccount(account));
      expand.setAttribute("aria-expanded", String(state.expanded.has(suppliedId)));
      const branchCount = account.branch_count ?? account.branches?.length ?? 0;
      append(identity, expand, node("small", `${branchCount} ${branchCount === 1 ? "branch" : "branches"}`));
      const rank = node("td"), branches = account.branches || [];
      const reasonBranch = branches.find(branch => branch.branch_id === account.reason_branch_id) || branches[0];
      append(rank, node("strong", priority(account.priority || account.state), { class: `cr-priority ${["P1", "P2", "P3"].includes(account.priority) ? `cr-${account.priority.toLowerCase()}` : ""}` }), node("small", `${reasonBranch?.branch_name ? `${reasonBranch.branch_name}: ` : ""}${account.reason || ""}`));
      if (account.mixed_visibility) rank.appendChild(node("small", "Mixed visibility", { class: "cr-warning" }));
      const direct = reasonBranch?.identification || account.identification || account.direct || {};
      const recommendation = reasonBranch?.recommendation || account.recommendation || account.recommendations || {};
      const directCounts = new Map(); branches.forEach(branch => { const result = branch.identification?.state || "awaiting_identification"; directCounts.set(result, (directCounts.get(result) || 0) + 1); });
      const directSummary = Array.from(directCounts, ([result, count]) => `${count} ${readable(result)}`).join(" · ") || readable(direct.state);
      const recommended = recommendation.required_count !== undefined ? `${readable(recommendation.state || recommendation.status)} · ${recommendation.accepted_count || 0}/${recommendation.required_count}` : readable(account.recommendation_status || recommendation.state || recommendation.status);
      const contact = node("td"); append(contact, node("span", readable(account.contact_status)), node("small", account.last_contact_at ? date(account.last_contact_at) : account.contact_status !== "uncontacted" ? "Contact date not recorded" : ""));
      const evidenceDates = node("td");
      append(evidenceDates, node("small", direct.collected_at ? `Identification: ${date(direct.collected_at)}` : "Identification: not checked"), node("small", recommendation.collected_at ? `Recommendations: ${date(recommendation.collected_at)}` : "Recommendations: not checked"));
      append(row, identity, rank, node("td", directSummary), node("td", recommended), contact, evidenceDates);
      body.appendChild(row); if (state.expanded.has(suppliedId)) branchRows(body, account);
    });
    wrapper.appendChild(table); panel.appendChild(wrapper);
    const pagination = node("div", null, { class: "cr-pagination" });
    append(pagination, node("span", `${state.offset + 1}–${state.offset + state.accounts.length} of ${state.total}`), button("Previous", () => { state.offset = Math.max(0, state.offset - state.limit); refresh(); }, true, state.offset === 0), button("Next", () => { state.offset += state.limit; refresh(); }, true, state.offset + state.limit >= state.total));
    panel.appendChild(pagination); container.appendChild(panel);
  }
  function render() {
    const container = root(); if (!container) return;
    container.replaceChildren();
    const toolbar = node("div", null, { class: "cr-toolbar" });
    const workspace = select(toolbar, "City workspace", [["", "Choose workspace"], ...state.workspaces.map(item => [item.id, item.name || `${item.city}, ${item.country_name || item.country_code}`])], state.workspace?.id || "");
    workspace.addEventListener("change", () => { if (workspace.value) switchWorkspace(workspace.value); });
    append(toolbar, button("New city", createWorkspace), button("Refresh research", () => refresh(), true, state.loading)); container.appendChild(toolbar);
    if (state.error) container.appendChild(node("p", state.error, { class: "cr-error", role: "alert" }));
    if (state.loading && !state.workspace) container.appendChild(node("p", "Loading workspaces…", { role: "status" }));
    if (!state.workspace) {
      const empty = node("section", null, { class: "cr-empty cr-empty-start" });
      const actions = node("div", null, { class: "cr-actions" });
      append(actions, button("Create city workspace", createWorkspace, false), node("a", "Download CSV template", { href: `${API}/directory-template`, download: "client-search-directory-template.csv" }));
      append(empty, node("h2", "Start with one city"), node("p", "Choose a city and upload your clinic list. Then check which clinics AI can identify and which it recommends for local treatments."), actions);
      container.appendChild(empty); return;
    }
    setupStrip(container, state.workspace); filters(container); accountTable(container);
  }
  function poll() {
    clearTimeout(state.poll);
    state.poll = setTimeout(async () => {
      if (active() && !document.querySelector(".cr-dialog[open]")) await refresh();
      poll();
    }, 15000);
  }
  window.ClientResearchPanel = { refresh };
  document.addEventListener("DOMContentLoaded", () => { render(); if (active()) refresh(); poll(); });
  window.addEventListener("hashchange", () => { if (active()) refresh(); });
}());
