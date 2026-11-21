import { apiJson } from "../api.mjs";

const METRICS = [
  "recall_at_k",
  "mrr",
  "hit_rate",
  "refusal_accuracy",
  "answer_coverage",
  "citation_accuracy",
];
const TERMINAL_STATUSES = new Set(["succeeded", "completed", "failed"]);

function scopeKey(scope = {}) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user, scope.window]);
}

function errorMessage(error) {
  const detail = error?.body?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") return detail.message;
  return error instanceof Error && error.message ? error.message : "Request failed";
}

function validRunId(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !/^[1-9]\d*$/.test(value)) return null;
  const numeric = Number(value);
  return Number.isSafeInteger(numeric) && numeric > 0 ? String(numeric) : null;
}

function datasetRunId(value) {
  return typeof value === "string" ? validRunId(value) : null;
}

function finiteNumber(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function displayValue(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function outputText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return "—";
  }
}

export function createEvaluation({
  root,
  guard,
  scope = null,
  permissions = null,
  api = apiJson,
  window = globalThis.window,
  document = globalThis.document,
  setTimeout = globalThis.setTimeout?.bind(globalThis),
  clearTimeout = globalThis.clearTimeout?.bind(globalThis),
} = {}) {
  if (!root) {
    return {
      async loadRuns() { return false; },
      async selectRun() { return false; },
      async triggerRun() { return false; },
      setActive() {}, clear() {}, destroy() {},
    };
  }

  let active = false;
  let destroyed = false;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let runs = [];
  let selectedRunId = null;
  let selectedRun = null;
  let poll = null;
  let pollTimer = null;
  const operations = { history: null, detail: null, trigger: null };
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
  const contextIsCurrent = (context) => !destroyed
    && active
    && document?.visibilityState !== "hidden"
    && currentContext
    && scopeKey(context?.scope) === scopeKey(currentContext.scope)
    && (!guard || guard.isCurrent(context));

  function syncPermissions() {
    if (destroyed) return;
    const reason = permissions?.actionReason() ?? "Action availability is verified by the server.";
    setText("#evaluation-permission-status", reason);
    const busy = Boolean(operations.trigger || poll);
    root.querySelectorAll("[data-kind]").forEach((button) => {
      button.disabled = busy || (permissions ? !permissions.canActions() : false);
      button.title = button.disabled && !busy && permissions
        ? reason
        : `Start ${button.dataset.kind} run`;
    });
  }

  function previousComparableRun(run) {
    if (!run) return null;
    const id = validRunId(run.id);
    const index = runs.findIndex((item) => validRunId(item?.id) === id);
    const candidates = index >= 0
      ? runs.slice(index + 1)
      : runs.filter((item) => {
        const candidateId = finiteNumber(item?.id);
        const currentId = finiteNumber(id);
        return candidateId !== null && currentId !== null && candidateId < currentId;
      });
    return candidates.find((item) => item?.kind === run.kind
      && (item?.status === "succeeded" || item?.status === "completed")) ?? null;
  }

  function renderRuns() {
    const body = byId("evaluation-run-rows");
    if (!body || destroyed) return;
    const rows = [];
    if (!runs.length) {
      const row = makeElement("tr");
      const cell = makeElement("td", "wb-ops-empty", "No evaluation runs in this scope.");
      cell.colSpan = 7;
      row.appendChild(cell);
      rows.push(row);
    }
    for (const run of runs) {
      const id = validRunId(run.id);
      const row = makeElement("tr", "wb-ops-row");
      row.dataset.runId = id;
      if (id === selectedRunId) row.classList.add("is-selected");
      const selectCell = makeElement("td");
      const select = makeElement("button", "wb-ops-select", `#${id}`);
      select.type = "button";
      select.dataset.runSelect = id;
      select.setAttribute("aria-label", `Inspect ${run.kind || "evaluation"} run ${id}`);
      if (id === selectedRunId) select.setAttribute("aria-current", "true");
      selectCell.appendChild(select);
      row.appendChild(selectCell);
      row.appendChild(makeElement("td", "", run.kind || "—"));
      row.appendChild(makeElement("td", `wb-ops-status wb-ops-status-${run.status || "unknown"}`, run.status || "—"));
      row.appendChild(makeElement("td", "", displayValue(run.requested_at)));
      row.appendChild(makeElement("td", "", displayValue(run.started_at)));
      row.appendChild(makeElement("td", "", displayValue(run.finished_at)));
      row.appendChild(makeElement("td", "wb-numeric", displayValue(run.exit_code)));
      rows.push(row);
    }
    const focusedId = datasetRunId(document?.activeElement?.dataset?.runSelect);
    body.replaceChildren(...rows);
    if (focusedId) body.querySelector(`[data-run-select="${focusedId}"]`)?.focus();
  }

  function renderDetail() {
    if (destroyed) return;
    const fields = [
      "kind", "status", "requested_at", "started_at", "finished_at",
      "exit_code", "artifact_path", "error",
    ];
    for (const field of fields) {
      setText(`[data-run-field="${field}"]`, displayValue(selectedRun?.[field]));
    }
    setText("#evaluation-run-output", outputText(selectedRun?.output_tail));

    const metrics = selectedRun?.summary?.metrics ?? {};
    const previous = previousComparableRun(selectedRun);
    const previousMetrics = previous?.summary?.metrics ?? {};
    for (const key of METRICS) {
      const currentValue = finiteNumber(metrics?.[key]);
      const previousValue = finiteNumber(previousMetrics?.[key]);
      setText(`[data-run-metric="${key}"]`, currentValue === null ? "—" : currentValue.toFixed(4));
      let delta = "—";
      if (currentValue !== null && previousValue !== null) {
        const difference = currentValue - previousValue;
        if (Number.isFinite(difference)) delta = `${difference >= 0 ? "+" : ""}${difference.toFixed(4)}`;
      }
      setText(`[data-run-delta="${key}"]`, delta);
    }
    setText(
      "#evaluation-delta-context",
      previous
        ? `Delta = current − nearest earlier successful ${selectedRun.kind} run #${validRunId(previous.id)}.`
        : "No nearest earlier successful run of the same kind is available for delta comparison.",
    );
    syncPermissions();
  }

  function clearDetail(message = "Select a run to inspect its detail independently.") {
    selectedRunId = null;
    selectedRun = null;
    renderRuns();
    renderDetail();
    setText("#evaluation-detail-status", message);
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
    if (kind === "history") setText("#evaluation-runs-status", "Run history request cancelled. Load runs to retry.");
    if (kind === "detail") setText("#evaluation-detail-status", "Run detail request cancelled. Select the run to retry.");
    if (kind === "trigger") setText("#evaluation-action-status", "Run trigger cancelled; no outcome is assumed.");
    syncPermissions();
  }

  function stopPolling({ abort = true } = {}) {
    const previous = poll;
    poll = null;
    if (pollTimer !== null) {
      clearTimeout?.(pollTimer);
      pollTimer = null;
    }
    if (abort) previous?.lastController?.abort();
  }

  function cancelAll() {
    stopPolling();
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

  function upsertRun(run) {
    const id = validRunId(run?.id);
    if (!id) return;
    const index = runs.findIndex((item) => validRunId(item?.id) === id);
    if (index >= 0) runs[index] = { ...runs[index], ...run };
    else runs = [run, ...runs];
  }

  async function loadRuns(context = currentContext) {
    const operation = beginOperation("history", context);
    if (!operation) return false;
    setText("#evaluation-runs-status", "Loading evaluation history…");
    const query = new URLSearchParams({ kb_id: context.scope.kb, limit: "20" });
    try {
      const data = await api(`/v1/ops/runs?${query.toString()}`, {
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation)) return false;
      runs = (Array.isArray(data?.runs) ? data.runs : []).filter((run) => validRunId(run?.id));
      if (selectedRunId && !runs.some((run) => validRunId(run.id) === selectedRunId) && !selectedRun) {
        selectedRunId = null;
      }
      renderRuns();
      renderDetail();
      setText("#evaluation-runs-status", `${runs.length} runs · newest history first.`);
      return true;
    } catch (error) {
      if (!operationIsCurrent(operation)) return false;
      setText("#evaluation-runs-status", `Run history unavailable: ${errorMessage(error)}`);
      return false;
    } finally {
      if (finishOperation(operation) && contextIsCurrent(operation.context)) syncPermissions();
    }
  }

  function schedulePoll() {
    if (!poll || pollTimer !== null || operations.detail || !contextIsCurrent(poll.context)) return;
    const scheduled = poll;
    pollTimer = setTimeout?.(() => {
      pollTimer = null;
      if (poll !== scheduled || !contextIsCurrent(scheduled.context)) return;
      void loadDetail(scheduled.runId, scheduled.context, { triggered: true });
    }, 450) ?? null;
  }

  async function loadDetail(runId, context = currentContext, { triggered = false } = {}) {
    const id = validRunId(runId);
    if (!id) return false;
    if (!triggered) stopPolling();
    if (triggered && (poll?.runId !== id || scopeKey(poll.context?.scope) !== scopeKey(context?.scope))) return false;
    const operation = beginOperation("detail", context);
    if (!operation) return false;
    setText("#evaluation-detail-status", `Loading run #${id} detail…`);
    try {
      const data = await api(`/v1/ops/runs/${encodeURIComponent(id)}`, {
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation) || selectedRunId !== id) return false;
      selectedRun = { ...data, id };
      upsertRun(selectedRun);
      renderRuns();
      renderDetail();
      const status = selectedRun.status || "unknown";
      setText("#evaluation-detail-status", `Loaded run #${id} · ${status}.`);
      if (triggered && poll?.runId === id) {
        if (TERMINAL_STATUSES.has(status)) {
          stopPolling({ abort: false });
          setText("#evaluation-action-status", `Run #${id} reached terminal status: ${status}.`);
        } else {
          setText("#evaluation-action-status", `Run #${id} is ${status}; polling actual run detail…`);
        }
      }
      return true;
    } catch (error) {
      if (!operationIsCurrent(operation) || selectedRunId !== id) return false;
      if (triggered) stopPolling();
      if (error?.status === 404) {
        runs = runs.filter((run) => validRunId(run.id) !== id);
        clearDetail(`Run #${id} is no longer visible.`);
        if (triggered) setText("#evaluation-action-status", `Run #${id} is no longer visible.`);
      } else {
        setText("#evaluation-detail-status", `Run detail unavailable: ${errorMessage(error)}`);
        if (triggered) setText("#evaluation-action-status", `Run #${id} detail failed: ${errorMessage(error)}`);
      }
      return false;
    } finally {
      if (finishOperation(operation) && contextIsCurrent(operation.context)) {
        syncPermissions();
        if (triggered && poll?.runId === id) {
          poll.lastController = operation.controller;
          schedulePoll();
        }
      }
    }
  }

  async function selectRun(runId, context = currentContext) {
    if (destroyed || !contextIsCurrent(context)) return false;
    const id = validRunId(runId);
    if (!id || !runs.some((run) => validRunId(run.id) === id)) return false;
    cancelOperation("detail");
    selectedRunId = id;
    selectedRun = runs.find((run) => validRunId(run.id) === id) ?? null;
    setText("#evaluation-action-status", "");
    renderRuns();
    renderDetail();
    return loadDetail(id, context);
  }

  async function triggerRun(kind, context = currentContext) {
    if (destroyed || !contextIsCurrent(context)) return false;
    if (permissions && !permissions.canActions()) {
      setText("#evaluation-action-status", permissions.actionReason());
      syncPermissions();
      return false;
    }
    stopPolling();
    cancelOperation("detail");
    const operation = beginOperation("trigger", context);
    if (!operation) return false;
    syncPermissions();
    setText("#evaluation-action-status", `Starting ${kind} run…`);
    try {
      const { data, status } = await api(`/v1/ops/kbs/${encodeURIComponent(context.scope.kb)}/runs`, {
        method: "POST",
        body: { kind, params: {} },
        scope: context.scope,
        signal: operation.controller.signal,
        withMeta: true,
      });
      if (!operationIsCurrent(operation)) return false;
      if (status !== 202) {
        setText("#evaluation-action-status", `Run trigger failed: Expected HTTP 202, received HTTP ${status}.`);
        return false;
      }
      const id = validRunId(data?.run_id);
      if (!id) {
        setText("#evaluation-action-status", "The trigger response did not include a valid run id; no run detail was requested.");
        return false;
      }
      selectedRunId = id;
      selectedRun = null;
      renderRuns();
      renderDetail();
      finishOperation(operation);
      poll = { runId: id, context };
      setText("#evaluation-action-status", `Run #${id} accepted; loading actual run detail…`);
      syncPermissions();
      return await loadDetail(id, context, { triggered: true });
    } catch (error) {
      if (!operationIsCurrent(operation)) return false;
      if (error?.status === 409) {
        setText("#evaluation-action-status", `Run conflict: ${errorMessage(error)}`);
      } else {
        setText("#evaluation-action-status", `Run trigger failed: ${errorMessage(error)}`);
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
    const historyResult = await loadRuns(context);
    if (contextIsCurrent(context)) syncPermissions();
    return historyResult;
  }

  function onRootClick(event) {
    if (destroyed) return;
    const select = event.target?.closest?.("[data-run-select]");
    const id = datasetRunId(select?.dataset.runSelect);
    if (id) {
      void selectRun(id);
      return;
    }
    if (event.target?.closest?.("#evaluation-load")) void loadWorkspace();
  }

  function onTriggerClick(event) {
    if (destroyed) return;
    void triggerRun("eval_mock");
  }

  function onLoadtestClick(event) {
    if (destroyed) return;
    void triggerRun("loadtest");
  }

  function onDrillParserClick(event) {
    if (destroyed) return;
    void triggerRun("drill_parser");
  }

  function onDrillLlmClick(event) {
    if (destroyed) return;
    void triggerRun("drill_llm");
  }

  function onDrillQueueClick(event) {
    if (destroyed) return;
    void triggerRun("drill_queue");
  }

  function onRootKeydown(event) {
    if (destroyed || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const select = event.target?.closest?.("[data-run-select]");
    if (!select) return;
    const buttons = [...root.querySelectorAll("[data-run-select]")];
    const index = buttons.indexOf(select);
    const next = buttons[(index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length];
    if (!next) return;
    event.preventDefault();
    next.focus();
    const id = datasetRunId(next.dataset.runSelect);
    if (id) void selectRun(id);
  }

  function onScopeChange(event) {
    if (destroyed) return;
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onRefresh(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    if (event.detail?.view !== "evaluation" || !contextIsCurrent(currentContext)) return;
    stopPolling();
    cancelOperation("trigger");
    cancelOperation("detail");
    syncPermissions();
    void loadWorkspace(currentContext);
    if (selectedRunId) void loadDetail(selectedRunId, currentContext);
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
    runs = [];
    selectedRunId = null;
    selectedRun = null;
    renderRuns();
    renderDetail();
    setText("#evaluation-runs-status", "Load runs or refresh to inspect evaluation history.");
    setText("#evaluation-detail-status", "Select a run to inspect its detail independently.");
    setText("#evaluation-action-status", "");
    setText("#evaluation-permission-status", "Permissions are checked when runs load.");
  }

  root.addEventListener("click", onRootClick);
  root.addEventListener("keydown", onRootKeydown);
  const triggerListeners = [
    ["run-trigger-eval-mock", onTriggerClick],
    ["run-trigger-loadtest", onLoadtestClick],
    ["run-trigger-drill-parser", onDrillParserClick],
    ["run-trigger-drill-llm", onDrillLlmClick],
    ["run-trigger-drill-queue", onDrillQueueClick],
  ];
  for (const [id, listener] of triggerListeners) byId(id)?.addEventListener("click", listener);
  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  document?.addEventListener("visibilitychange", onVisibilityChange);
  renderRuns();
  renderDetail();

  return {
    loadRuns,
    selectRun,
    triggerRun,
    setActive,
    clear,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      active = false;
      stopPolling();
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
      for (const [id, listener] of triggerListeners) byId(id)?.removeEventListener("click", listener);
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
    },
  };
}
