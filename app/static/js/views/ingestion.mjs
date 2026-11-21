import { apiJson } from "../api.mjs";

function scopeKey(scope = {}) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user, scope.window]);
}

function errorMessage(error) {
  const detail = error?.body?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") return detail.message;
  return error instanceof Error && error.message ? error.message : "Request failed";
}

function finiteNonNegative(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function seconds(value, digits = 2) {
  const numeric = finiteNonNegative(value);
  return numeric === null ? "—" : `${numeric.toFixed(digits)} s`;
}

function progress(value) {
  const numeric = finiteNonNegative(value);
  return numeric === null || numeric > 100 ? "—" : `${numeric}%`;
}

function validJobId(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !/^[1-9]\d*$/.test(value)) return null;
  const numeric = Number(value);
  return Number.isSafeInteger(numeric) && numeric > 0 ? String(numeric) : null;
}

function datasetJobId(value) {
  return typeof value === "string" ? validJobId(value) : null;
}

export function createIngestion({
  root,
  guard,
  scope = null,
  permissions = null,
  api = apiJson,
  window = globalThis.window,
  document = globalThis.document,
} = {}) {
  if (!root) {
    return {
      async loadJobs() { return false; },
      async selectJob() { return false; },
      async retrySelectedJob() { return false; },
      setActive() {}, clear() {}, destroy() {},
    };
  }

  let active = false;
  let destroyed = false;
  let sequence = 0;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let jobs = [];
  let selectedJobId = null;
  const operations = { jobs: null, events: null, retry: null };
  const permissionLease = {};

  const byId = (id) => root.querySelector(`#${id}`);
  const setText = (selector, value) => {
    const element = root.querySelector(selector);
    if (element) element.textContent = String(value ?? "");
  };
  const makeElement = (tag, className = "", text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const selectedJob = () => jobs.find((job) => validJobId(job.id) === selectedJobId) ?? null;
  const contextIsCurrent = (context) => !destroyed
    && active
    && document?.visibilityState !== "hidden"
    && currentContext
    && scopeKey(context?.scope) === scopeKey(currentContext.scope)
    && (!guard || guard.isCurrent(context));

  function syncPermissions() {
    if (destroyed) return;
    const reason = permissions?.actionReason() ?? "Action availability is verified by the server.";
    setText("#ingestion-permission-status", reason);
    const retry = byId("ingestion-retry");
    const job = selectedJob();
    if (!retry) return;
    retry.hidden = !job || job.status !== "failed";
    retry.disabled = Boolean(operations.retry) || (permissions ? !permissions.canRetry(job) : job?.status !== "failed");
    retry.title = retry.disabled && !operations.retry && permissions
      ? permissions.retryReason(job)
      : `Retry failed job #${selectedJobId ?? "—"}`;
  }

  function renderJobs() {
    const body = byId("ingestion-job-rows");
    if (!body || destroyed) return;
    const rows = [];
    if (!jobs.length) {
      const row = makeElement("tr");
      const cell = makeElement("td", "wb-ops-empty", "No ingestion jobs in this scope.");
      cell.colSpan = 7;
      row.appendChild(cell);
      rows.push(row);
    }
    for (const job of jobs) {
      const id = validJobId(job.id);
      const row = makeElement("tr", "wb-ops-row");
      row.dataset.jobId = id;
      if (id === selectedJobId) row.classList.add("is-selected");
      const jobCell = makeElement("td");
      const select = makeElement("button", "wb-ops-select", `#${id}`);
      select.type = "button";
      select.dataset.jobSelect = id;
      select.setAttribute("aria-label", `Inspect ingestion job ${id}`);
      if (id === selectedJobId) select.setAttribute("aria-current", "true");
      jobCell.appendChild(select);
      row.appendChild(jobCell);
      row.appendChild(makeElement("td", "", job.doc_id || "—"));
      row.appendChild(makeElement("td", `wb-ops-status wb-ops-status-${job.status || "unknown"}`, job.status || "—"));
      const values = {
        wait: seconds(job.wait_seconds),
        duration: seconds(job.duration_seconds),
        progress: progress(job.progress),
        error: job.error || "—",
      };
      for (const [field, value] of Object.entries(values)) {
        const cell = makeElement("td", field === "error" ? "wb-ops-error-text" : "wb-numeric", value);
        cell.dataset.jobField = field;
        cell.dataset.jobId = id;
        row.appendChild(cell);
      }
      rows.push(row);
    }
    const focusedId = datasetJobId(document?.activeElement?.dataset?.jobSelect);
    body.replaceChildren(...rows);
    if (focusedId) body.querySelector(`[data-job-select="${focusedId}"]`)?.focus();
  }

  function renderJobDetail() {
    const job = selectedJob();
    const values = {
      id: selectedJobId ? `#${selectedJobId}` : "—",
      status: job?.status || "—",
      wait: seconds(job?.wait_seconds),
      duration: seconds(job?.duration_seconds),
      progress: progress(job?.progress),
      error: job?.error || "—",
    };
    for (const [field, value] of Object.entries(values)) setText(`[data-job-detail="${field}"]`, value);
    syncPermissions();
  }

  function clearEvents(message = "Select a job to load its events independently.") {
    byId("ingestion-job-events")?.replaceChildren();
    setText("#ingestion-job-events-status", message);
  }

  function clearSelection(message = "Select a job to load its events independently.") {
    selectedJobId = null;
    renderJobs();
    renderJobDetail();
    clearEvents(message);
  }

  function operationIsCurrent(operation) {
    return contextIsCurrent(operation.context)
      && operations[operation.kind] === operation
      && !operation.controller.signal.aborted;
  }

  function cancelOperation(kind) {
    const operation = operations[kind];
    if (!operation) return;
    operations[kind] = null;
    operation.unlink();
    operation.controller.abort();
    if (destroyed) return;
    if (kind === "jobs") setText("#ingestion-jobs-status", "Jobs request cancelled. Load jobs to retry.");
    if (kind === "events") setText("#ingestion-job-events-status", "Event request cancelled. Select the job to retry.");
    if (kind === "retry") setText("#ingestion-action-status", "Retry request cancelled; reload the job to verify its actual state.");
    syncPermissions();
  }

  function cancelAll() {
    for (const kind of Object.keys(operations)) cancelOperation(kind);
  }

  function beginOperation(kind, context) {
    if (!contextIsCurrent(context)) return null;
    cancelOperation(kind);
    const controller = new AbortController();
    const parent = context.signal;
    const abort = () => controller.abort();
    if (parent) parent.addEventListener("abort", abort, { once: true });
    const operation = {
      id: ++sequence,
      kind,
      context,
      controller,
      unlink() { parent?.removeEventListener("abort", abort); },
    };
    operations[kind] = operation;
    return operation;
  }

  function finishOperation(operation) {
    if (operations[operation.kind] !== operation) return false;
    operations[operation.kind] = null;
    operation.unlink();
    return true;
  }

  async function loadJobs(context = currentContext) {
    const operation = beginOperation("jobs", context);
    if (!operation) return false;
    setText("#ingestion-jobs-status", "Loading ingestion jobs…");
    const query = new URLSearchParams({ kb_id: context.scope.kb, limit: "50" });
    try {
      const data = await api(`/v1/ops/jobs?${query.toString()}`, {
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation)) return false;
      jobs = (Array.isArray(data?.jobs) ? data.jobs : []).filter((job) => validJobId(job?.id));
      if (selectedJobId && !selectedJob()) selectedJobId = null;
      renderJobs();
      renderJobDetail();
      if (!selectedJobId) clearEvents();
      const counts = data?.counts ?? {};
      setText(
        "#ingestion-jobs-status",
        `${jobs.length} jobs · queued ${counts.queued ?? "—"} · running ${counts.running ?? "—"} · failed ${counts.failed ?? "—"}`,
      );
      return true;
    } catch (error) {
      if (!operationIsCurrent(operation)) return false;
      setText("#ingestion-jobs-status", `Jobs unavailable: ${errorMessage(error)}`);
      return false;
    } finally {
      if (finishOperation(operation) && contextIsCurrent(operation.context)) syncPermissions();
    }
  }

  async function loadEvents(jobId, context = currentContext) {
    const id = validJobId(jobId);
    if (!id) return false;
    const operation = beginOperation("events", context);
    if (!operation) return false;
    byId("ingestion-job-events")?.replaceChildren();
    setText("#ingestion-job-events-status", `Loading events for job #${id}…`);
    try {
      const data = await api(`/v1/ops/jobs/${encodeURIComponent(id)}/events`, {
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation) || selectedJobId !== id) return false;
      const events = Array.isArray(data?.events) ? data.events : [];
      const items = events.map((event) => {
        const item = makeElement("li", "wb-ops-event");
        const heading = makeElement("div", "wb-ops-event-heading");
        heading.appendChild(makeElement("strong", "", event.stage || "—"));
        heading.appendChild(makeElement("span", "", event.status || "—"));
        item.appendChild(heading);
        item.appendChild(makeElement(
          "p",
          "wb-ops-event-meta",
          `${progress(event.progress)} · ${event.created_at || "—"}`,
        ));
        if (event.message) item.appendChild(makeElement("p", "wb-ops-event-message", event.message));
        return item;
      });
      if (!items.length) {
        const empty = makeElement("li", "wb-ops-empty", "No events recorded for this job.");
        byId("ingestion-job-events")?.replaceChildren(empty);
      } else {
        byId("ingestion-job-events")?.replaceChildren(...items);
      }
      setText("#ingestion-job-events-status", `${events.length} events for job #${id}.`);
      return true;
    } catch (error) {
      if (!operationIsCurrent(operation) || selectedJobId !== id) return false;
      if (error?.status === 404) {
        jobs = jobs.filter((job) => validJobId(job.id) !== id);
        clearSelection("The selected job is no longer visible.");
        setText("#ingestion-action-status", `Job #${id} is no longer visible.`);
      } else {
        setText("#ingestion-job-events-status", `Events unavailable: ${errorMessage(error)}`);
      }
      return false;
    } finally {
      if (finishOperation(operation) && contextIsCurrent(operation.context)) syncPermissions();
    }
  }

  async function selectJob(jobId, context = currentContext) {
    if (destroyed || !contextIsCurrent(context)) return false;
    const id = validJobId(jobId);
    if (!id || !jobs.some((job) => validJobId(job.id) === id)) return false;
    cancelOperation("events");
    selectedJobId = id;
    setText("#ingestion-action-status", "");
    renderJobs();
    renderJobDetail();
    return loadEvents(id, context);
  }

  async function retrySelectedJob(context = currentContext) {
    if (operations.retry) return false;
    const job = selectedJob();
    if (!job || (permissions ? !permissions.canRetry(job) : job.status !== "failed")) {
      syncPermissions();
      return false;
    }
    const id = validJobId(job.id);
    const operation = beginOperation("retry", context);
    if (!operation) return false;
    syncPermissions();
    setText("#ingestion-action-status", `Retrying job #${id}…`);
    try {
      const { status } = await api(`/v1/ops/jobs/${encodeURIComponent(id)}/retry`, {
        method: "POST",
        scope: context.scope,
        signal: operation.controller.signal,
        withMeta: true,
      });
      if (!operationIsCurrent(operation)) return false;
      if (status !== 202) {
        setText("#ingestion-action-status", `Retry failed: Expected HTTP 202, received HTTP ${status}.`);
        return false;
      }
      setText("#ingestion-action-status", `Retry accepted for job #${id}; reloading actual state…`);
      const results = await Promise.all([loadJobs(context), loadEvents(id, context)]);
      if (!contextIsCurrent(context) || selectedJobId !== id) return results.every(Boolean);
      setText(
        "#ingestion-action-status",
        results.every(Boolean)
          ? `Retry accepted; reloaded job #${id} and events from the server.`
          : `Retry accepted, but job #${id} could not be fully reloaded. Inspect the local errors above.`,
      );
      return results.every(Boolean);
    } catch (error) {
      if (!operationIsCurrent(operation)) return false;
      if (error?.status === 409) {
        setText("#ingestion-action-status", `已有运行中的任务；请刷新任务状态。${errorMessage(error)}`);
      } else if (error?.status === 404) {
        jobs = jobs.filter((item) => validJobId(item.id) !== id);
        clearSelection("The selected job is no longer visible.");
        setText("#ingestion-action-status", `Job #${id} is no longer visible.`);
      } else {
        setText("#ingestion-action-status", `Retry failed: ${errorMessage(error)}`);
      }
      return false;
    } finally {
      if (finishOperation(operation) && contextIsCurrent(operation.context)) syncPermissions();
    }
  }

  async function loadWorkspace(context = currentContext) {
    if (!contextIsCurrent(context)) return false;
    const permissionRequest = permissions?.load(context) ?? Promise.resolve(null);
    void permissionRequest.then(() => {
      if (contextIsCurrent(context)) syncPermissions();
    });
    const jobsResult = await loadJobs(context);
    if (contextIsCurrent(context)) syncPermissions();
    return jobsResult;
  }

  function onRootClick(event) {
    if (destroyed) return;
    const select = event.target?.closest?.("[data-job-select]");
    const id = datasetJobId(select?.dataset.jobSelect);
    if (id) {
      void selectJob(id);
      return;
    }
    if (event.target?.closest?.("#ingestion-load")) void loadWorkspace();
    if (event.target?.closest?.("#ingestion-retry")) void retrySelectedJob();
  }

  function onRootKeydown(event) {
    if (destroyed || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const select = event.target?.closest?.("[data-job-select]");
    if (!select) return;
    const buttons = [...root.querySelectorAll("[data-job-select]")];
    const index = buttons.indexOf(select);
    const next = buttons[(index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length];
    if (!next) return;
    event.preventDefault();
    next.focus();
    const id = datasetJobId(next.dataset.jobSelect);
    if (id) void selectJob(id);
  }

  function onScopeChange(event) {
    if (destroyed) return;
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onRefresh(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    if (event.detail?.view !== "ingestion" || !contextIsCurrent(currentContext)) return;
    cancelOperation("retry");
    syncPermissions();
    void loadWorkspace(currentContext);
    if (selectedJobId) void loadEvents(selectedJobId, currentContext);
  }

  function onVisibilityChange() {
    if (!destroyed && document.visibilityState === "hidden") cancelAll();
  }

  function setActive(value) {
    if (destroyed) return;
    active = Boolean(value);
    if (active) permissions?.retain?.(permissionLease);
    else {
      cancelAll();
      permissions?.release?.(permissionLease);
    }
    syncPermissions();
  }

  function clear() {
    if (destroyed) return;
    cancelAll();
    currentContext = null;
    jobs = [];
    selectedJobId = null;
    renderJobs();
    renderJobDetail();
    clearEvents();
    setText("#ingestion-jobs-status", "Load jobs or refresh to inspect ingestion.");
    setText("#ingestion-action-status", "");
    setText("#ingestion-permission-status", "Permissions are checked when jobs load.");
  }

  root.addEventListener("click", onRootClick);
  root.addEventListener("keydown", onRootKeydown);
  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  document?.addEventListener("visibilitychange", onVisibilityChange);
  renderJobs();
  renderJobDetail();

  return {
    loadJobs,
    selectJob,
    retrySelectedJob,
    setActive,
    clear,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      active = false;
      for (const kind of Object.keys(operations)) {
        const operation = operations[kind];
        if (!operation) continue;
        operations[kind] = null;
        operation.unlink();
        operation.controller.abort();
      }
      permissions?.release?.(permissionLease);
      root.removeEventListener("click", onRootClick);
      root.removeEventListener("keydown", onRootKeydown);
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
    },
  };
}
