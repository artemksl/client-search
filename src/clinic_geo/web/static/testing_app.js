/* Standalone visibility and client research panel. Opening it only reads saved state. */
const state = {
  testingOnly: true,
  activeView: "testing",
  jobWorkspaceView: "testing",
  jobs: [],
  selectedJob: null,
  selectedJobs: {testing: null},
  savedJobHistory: new Map(),
  historyLoaded: new Set(),
  historyErrors: {},
  historyRequestIds: {testing: 0},
  refreshRequestId: 0,
  logRequestId: 0,
  displayedLogJob: null,
  pendingJobActions: new Set(),
  actionStatusTimer: null,
};

function panelViewFromHash() {
  return location.hash === "#clientResearch" ? "clientResearch" : "testing";
}

function navigateTo(view = "testing", updateHash = true) {
  const next = view === "clientResearch" ? view : "testing";
  const changed = state.activeView !== next;
  state.activeView = next;
  for (const section of document.querySelectorAll(".testing-workspace > .view")) {
    section.classList.toggle("active", section.id === `${next}View`);
  }
  for (const button of document.querySelectorAll("[data-panel-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.panelView === next));
  }
  const research = next === "clientResearch";
  byId("workspaceTitle").textContent = research ? "Client research" : "Visibility engine";
  byId("workspaceDescription").textContent = research
    ? "Import a city directory, assess AI visibility, and find clinics to contact."
    : "Test a platform, review chat outcomes, and retry failed measurements.";
  document.title = research ? "Client research" : "Visibility testing";
  if (updateHash && location.hash !== `#${next}`) location.hash = next;
  if (changed && research) void window.ClientResearchPanel.refresh();
  if (changed && !research) void runUiAction("Load run history", refresh);
}

async function refresh() {
  const requestId = ++state.refreshRequestId;
  await loadRunHistory("testing", requestId);
  if (requestId !== state.refreshRequestId) return;
  state.jobs = Array.from(state.savedJobHistory.values()).filter(isVisibilityMeasurementJob)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  renderJobs();
  refreshSelectedReadoutIfChanged();
  if (state.selectedJob) await refreshLog();
}

async function refreshCurrentView() {
  const button = byId("refresh");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    if (state.activeView === "clientResearch") await window.ClientResearchPanel.refresh();
    else {
      await refresh();
      if (state.selectedJob) await refreshFailureReadout(state.selectedJob);
    }
  } catch (error) {
    reportActionError("Refresh", error);
  } finally {
    button.disabled = false;
    button.textContent = "Refresh";
  }
}

let statusPollPending = false;
async function pollStatus() {
  if (statusPollPending || state.activeView !== "testing") return;
  statusPollPending = true;
  try {
    await refresh();
    const job = state.jobs.find((item) => item.id === state.selectedJob);
    if (job && ["queued", "running", "finalizing", "stopping"].includes(job.status)
      && Date.now() - (state.lastMeasurementPoll || 0) >= 10000) {
      state.lastMeasurementPoll = Date.now();
      await refreshFailureReadout(job.id);
    }
  } catch (error) {
    reportActionError("Refresh run history", error);
  } finally {
    statusPollPending = false;
  }
}

byId("refresh").addEventListener("click", refreshCurrentView);
for (const button of document.querySelectorAll("[data-panel-view]")) {
  button.addEventListener("click", () => navigateTo(button.dataset.panelView));
}
window.addEventListener("hashchange", () => navigateTo(panelViewFromHash(), false));
navigateTo(panelViewFromHash(), false);
if (state.activeView === "testing") void runUiAction("Load run history", refresh);
setInterval(() => {
  if (!document.hidden) void pollStatus();
}, 2500);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void pollStatus();
});
