import { apiJson } from "../api.mjs";

const TRACE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const INSPECTOR_TABS = ["summary", "timing", "raw"];
const MOBILE_MODES = ["list", "detail"];
const STAGE_NAMES = {
  rewrite: "rewrite",
  retrieve: "retrieval",
  retrieval: "retrieval",
  rerank: "rerank",
  answer: "llm",
  llm: "llm",
};
const SUMMARY_FIELDS = [
  "trace_id", "kb_id", "occurred_at", "operation", "mode", "model", "outcome",
  "hits", "citation", "rerank", "prompt_tokens", "completion_tokens", "total_tokens", "errors",
];

function finiteNonNegative(value) {
  if (value === null || value === undefined || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function errorMessage(error) {
  return error instanceof Error && error.message ? error.message : "Request failed";
}

function isAbort(error) {
  return error?.name === "AbortError";
}

function formatMs(value) {
  const numeric = finiteNonNegative(value);
  return numeric === null ? "Not recorded" : `${numeric.toFixed(1)} ms`;
}

function formatTokens(item) {
  const prompt = finiteNonNegative(item?.prompt_tokens);
  const completion = finiteNonNegative(item?.completion_tokens);
  return prompt === null || completion === null ? "—" : String(prompt + completion);
}

export function buildTraceListUrl(scope = {}, filters = {}, cursor = null) {
  const query = new URLSearchParams({
    kb_id: String(scope.kb ?? ""),
    window: String(filters.window || scope.window || "24h"),
  });
  for (const name of ["outcome", "operation", "mode", "trace_id"]) {
    const value = String(filters[name] ?? "").trim();
    if (value) query.set(name, value);
  }
  if (filters.min_ms !== "" && filters.min_ms !== null && filters.min_ms !== undefined) {
    query.set("min_ms", String(filters.min_ms));
  }
  query.set("limit", String(filters.limit || 20));
  if (cursor && typeof cursor === "object") {
    if (cursor.before_occurred_at !== undefined && cursor.before_id !== undefined) {
      query.set("before_occurred_at", String(cursor.before_occurred_at));
      query.set("before_id", String(cursor.before_id));
    }
  }
  return `/v1/ops/trace-list?${query.toString()}`;
}

export function buildTimingModel(detail = {}) {
  const metric = detail?.metric && typeof detail.metric === "object" ? detail.metric : null;
  const totalMs = finiteNonNegative(metric?.total_ms);
  let proportional = totalMs !== null && totalMs > 0;
  let proportionOverflow = false;
  const proportion = (value) => {
    if (!proportional || value === null) return null;
    const ratio = value / totalMs * 100;
    if (!Number.isFinite(ratio)) {
      proportionOverflow = true;
      return null;
    }
    return ratio;
  };
  const sourceSpans = Array.isArray(metric?.stage_spans) ? metric.stage_spans : [];
  const spans = sourceSpans.map((span, index) => {
    const sourceStage = String(span?.stage || `unnamed-${index + 1}`);
    const stage = Object.hasOwn(STAGE_NAMES, sourceStage) ? STAGE_NAMES[sourceStage] : "other";
    const startMs = finiteNonNegative(span?.start_ms);
    const durationMs = finiteNonNegative(span?.duration_ms);
    return {
      stage,
      sourceStage,
      label: stage === "other" ? `other · ${sourceStage}` : stage,
      status: String(span?.status || "Not recorded"),
      startMs,
      durationMs,
      left: proportion(startMs),
      width: proportion(durationMs),
      percentage: proportion(durationMs),
      source: "stage_spans",
    };
  });

  const explicitUnaccounted = finiteNonNegative(
    metric?.timings_unaccounted_ms ?? detail?.timings_unaccounted_ms,
  );
  const endValues = spans
    .filter((span) => span.startMs !== null && span.durationMs !== null)
    .map((span) => span.startMs + span.durationMs);
  const invalidEnd = endValues.some((end) => !Number.isFinite(end));
  const ends = endValues.filter((end) => Number.isFinite(end));
  const maxEnd = invalidEnd ? null : ends.length ? Math.max(...ends) : 0;
  const pushUnaccounted = (startMs, durationMs, source) => {
    spans.push({
      stage: "unaccounted",
      sourceStage: "unaccounted",
      label: "unaccounted",
      status: "Not a named stage",
      startMs,
      durationMs,
      left: proportion(startMs),
      width: proportion(durationMs),
      percentage: proportion(durationMs),
      source,
    });
  };
  if (explicitUnaccounted !== null) {
    if (explicitUnaccounted > 0) {
      if (invalidEnd) proportionOverflow = true;
      pushUnaccounted(maxEnd, explicitUnaccounted, "timings_unaccounted_ms");
    }
  } else if (maxEnd !== null && totalMs !== null && totalMs > 0) {
    const gap = Math.max(totalMs - maxEnd, 0);
    if (gap > 0) pushUnaccounted(maxEnd, gap, "derived_unaccounted");
  }

  if (proportionOverflow) {
    proportional = false;
    for (const span of spans) {
      span.left = null;
      span.width = null;
      span.percentage = null;
    }
  }

  let status;
  if (!metric) status = "Timing unavailable: metric summary was not recorded.";
  else if (totalMs === null) status = "Total duration was not recorded; proportional timing is unavailable.";
  else if (totalMs === 0) status = "Zero-duration trace · 0.0 ms; proportional timing is unavailable.";
  else if (proportionOverflow) status = `Total ${formatMs(totalMs)} · proportion unavailable because a timing sum or ratio exceeded the finite range.`;
  else if (!spans.length) status = `Total ${formatMs(totalMs)} · no stage spans were recorded.`;
  else status = `Total ${formatMs(totalMs)} · bars are normalized to total_ms.`;
  return { totalMs, proportional, zeroDuration: totalMs === 0, spans, status };
}

export function createTraces({
  root,
  guard,
  scope = null,
  window = globalThis.window,
  document = globalThis.document,
  api = apiJson,
  clipboard = null,
} = {}) {
  if (!root) {
    return {
      async loadTraces() {},
      async loadMore() {},
      async selectTrace() {},
      async copyRaw() { return false; },
      selectInspectorTab() {},
      setActive() {},
      clear() {},
      destroy() {},
    };
  }

  let active = false;
  let destroyed = false;
  let sequence = 0;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let currentInspectorTab = "summary";
  let mobileMode = "list";
  let layout = "wide";
  let drawerOpen = false;
  let drawerTrigger = null;
  let selectedTraceId = "";
  let selectedDetail = null;
  let items = new Map();
  let nextCursor = null;
  let appliedFilters = null;
  let listLoaded = false;
  const operations = { list: null, detail: null };
  const mediaQueries = [];

  const byId = (id) => root.querySelector(`#${id}`);
  const setText = (selector, value) => {
    const element = root.querySelector(selector);
    if (element) element.textContent = String(value ?? "");
  };

  function makeElement(tag, className = "", text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function readFilters() {
    const value = (id) => String(byId(id)?.value ?? "").trim();
    const filters = {
      window: value("trace-window") || currentContext?.scope?.window || "24h",
      outcome: value("trace-outcome"),
      operation: value("trace-operation"),
      mode: value("trace-mode"),
      min_ms: value("trace-min-ms"),
      trace_id: value("trace-id"),
      limit: value("trace-limit") || "20",
    };
    if (filters.min_ms !== "" && finiteNonNegative(filters.min_ms) === null) {
      throw new Error("Minimum total must be a finite, non-negative number.");
    }
    const limit = Number(filters.limit);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      throw new Error("Page size must be an integer from 1 to 100.");
    }
    if (filters.trace_id && !TRACE_ID_PATTERN.test(filters.trace_id)) {
      throw new Error("Trace ID contains unsupported characters.");
    }
    filters.limit = limit;
    return filters;
  }

  function operationIsCurrent(operation) {
    return !destroyed
      && active
      && operations[operation.kind] === operation
      && !operation.controller.signal.aborted
      && !operation.context.signal?.aborted
      && (!guard || guard.isCurrent(operation.context));
  }

  function cancelOperation(kind) {
    const operation = operations[kind];
    if (!operation) return;
    operations[kind] = null;
    operation.unlink();
    operation.controller.abort();
    if (destroyed) return;
    if (kind === "list") {
      setText("#trace-list-status", "Trace list request cancelled. Refresh to retry.");
      const loadMore = byId("trace-load-more");
      if (loadMore) loadMore.disabled = false;
    } else {
      setText("#trace-detail-status", "Trace detail loading cancelled. Select the trace to retry.");
      setText("#trace-timing-status", "Timing loading cancelled. Select the trace to retry.");
    }
  }

  function cancelAll() {
    for (const kind of Object.keys(operations)) cancelOperation(kind);
  }

  function beginOperation(kind, context) {
    if (destroyed || !active || document?.visibilityState === "hidden") return null;
    if (!context?.scope?.kb || context.signal?.aborted || (guard && !guard.isCurrent(context))) return null;
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

  function stageSummary(row) {
    if (!Array.isArray(row?.stage_spans) || !row.stage_spans.length) return "Not recorded";
    return row.stage_spans.map((span, index) => String(span?.stage || `other-${index + 1}`)).join(" · ");
  }

  function syncSelectedRows() {
    root.querySelectorAll("[data-trace-select]").forEach((button) => {
      const selected = button.dataset.traceSelect === selectedTraceId;
      button.classList.toggle("is-selected", selected);
      if (selected) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
      const row = button.closest("tr");
      row?.classList.toggle("is-selected", selected);
    });
  }

  function renderRows(scrollTop = byId("trace-table-scroll")?.scrollTop ?? 0) {
    const body = byId("trace-list-rows");
    if (!body) return;
    const rows = [];
    if (!items.size) {
      const row = makeElement("tr");
      const cell = makeElement(
        "td",
        "wb-trace-empty",
        listLoaded ? "No traces match these filters." : "No traces loaded.",
      );
      cell.setAttribute("colspan", "8");
      row.appendChild(cell);
      rows.push(row);
    } else {
      for (const [traceId, item] of items) {
        const row = makeElement("tr", "wb-trace-row");
        row.dataset.traceId = traceId;
        if (traceId === selectedTraceId) row.classList.add("is-selected");
        row.appendChild(makeElement("td", "wb-timestamp", item?.occurred_at || "Not recorded"));
        const traceCell = makeElement("td");
        const button = makeElement("button", "wb-trace-id", traceId);
        button.type = "button";
        button.dataset.traceSelect = traceId;
        button.setAttribute("aria-label", `Inspect trace ${traceId}`);
        if (traceId === selectedTraceId) {
          button.setAttribute("aria-current", "true");
          button.classList.add("is-selected");
          drawerTrigger = button;
        }
        traceCell.appendChild(button);
        row.appendChild(traceCell);
        row.appendChild(makeElement("td", "", item?.operation || "Not recorded"));
        row.appendChild(makeElement("td", `wb-trace-outcome wb-trace-outcome-${item?.outcome || "missing"}`, item?.outcome || "Not recorded"));
        row.appendChild(makeElement("td", "wb-trace-stages", stageSummary(item)));
        row.appendChild(makeElement("td", "wb-numeric", formatMs(item?.total_ms)));
        row.appendChild(makeElement("td", "wb-numeric", item?.hits ?? "Not recorded"));
        row.appendChild(makeElement("td", "wb-numeric", formatTokens(item)));
        rows.push(row);
      }
    }
    body.replaceChildren(...rows);
    const scroll = byId("trace-table-scroll");
    if (scroll) scroll.scrollTop = scrollTop;
  }

  function clearDetail() {
    selectedDetail = null;
    for (const field of SUMMARY_FIELDS) setText(`[data-trace-summary="${field}"]`, "—");
    setText('[data-trace-summary="trace_id"]', selectedTraceId || "—");
    setText('[data-trace-summary="kb_id"]', currentContext?.scope?.kb || "—");
    setText("#trace-detail-status", selectedTraceId
      ? `Loading trace ${selectedTraceId}…`
      : "Select a trace to inspect diagnostic evidence.");
    const missing = byId("trace-missing");
    if (missing) {
      missing.hidden = true;
      missing.textContent = "";
    }
    setText("#trace-timing-status", selectedTraceId
      ? "Loading timing evidence…"
      : "Timing appears after a trace is selected.");
    byId("trace-timing-spans")?.replaceChildren();
    setText("#trace-raw", "");
    setText("#trace-copy-status", "Copy uses your browser clipboard permission.");
  }

  function missingText(detail) {
    const missing = Array.isArray(detail?.missing) ? detail.missing : [];
    const messages = [];
    if (missing.includes("metric")) {
      messages.push("Missing metric: the Postgres summary was not recorded; summary and timing fields are unavailable.");
    }
    if (missing.includes("events")) {
      messages.push("Missing events: no scoped JSONL events were recorded; Raw contains only available evidence.");
    }
    for (const name of missing.filter((name) => !["metric", "events"].includes(name))) {
      messages.push(`Missing ${name}: this evidence source was not recorded.`);
    }
    return messages.join(" ");
  }

  function summaryValue(value, metricPresent) {
    if (value !== null && value !== undefined && value !== "") {
      if (typeof value === "boolean") return value ? "Yes" : "No";
      return String(value);
    }
    return metricPresent ? "Not recorded" : "Missing: metric summary was not recorded.";
  }

  function renderSummary(detail) {
    const metric = detail?.metric && typeof detail.metric === "object" ? detail.metric : null;
    const present = Boolean(metric);
    const values = {
      trace_id: selectedTraceId || metric?.trace_id,
      kb_id: metric?.kb_id || currentContext?.scope?.kb,
      occurred_at: summaryValue(metric?.occurred_at, present),
      operation: summaryValue(metric?.operation, present),
      mode: summaryValue(metric?.mode, present),
      model: summaryValue(metric?.model, present),
      outcome: summaryValue(metric?.outcome, present),
      hits: summaryValue(metric?.hits, present),
      citation: summaryValue(metric?.citation_ok, present),
      rerank: summaryValue(metric?.rerank_degraded, present),
      prompt_tokens: summaryValue(metric?.prompt_tokens, present),
      completion_tokens: summaryValue(metric?.completion_tokens, present),
    };
    const prompt = finiteNonNegative(metric?.prompt_tokens);
    const completion = finiteNonNegative(metric?.completion_tokens);
    values.total_tokens = prompt === null && completion === null
      ? summaryValue(null, present)
      : String((prompt ?? 0) + (completion ?? 0));
    const eventErrors = (Array.isArray(detail?.events) ? detail.events : [])
      .map((event) => event?.error_kind || event?.error)
      .filter(Boolean)
      .map(String);
    const errors = [metric?.error_kind, ...eventErrors].filter(Boolean).map(String);
    values.errors = errors.length ? [...new Set(errors)].join("; ") : summaryValue(null, present);
    for (const field of SUMMARY_FIELDS) {
      setText(`[data-trace-summary="${field}"]`, values[field] ?? "—");
    }
  }

  function renderTiming(detail) {
    const model = buildTimingModel(detail);
    setText("#trace-timing-status", model.status);
    const rows = model.spans.map((span) => {
      const row = makeElement("div", "wb-trace-span");
      row.dataset.traceStage = span.stage;
      const heading = makeElement("div", "wb-trace-span-heading");
      heading.appendChild(makeElement("span", "wb-trace-span-label", span.label));
      const percentage = span.percentage === null ? "proportion unavailable" : `${span.percentage.toFixed(1)}%`;
      heading.appendChild(makeElement("span", "wb-numeric", `${formatMs(span.durationMs)} · ${percentage}`));
      row.appendChild(heading);
      const track = makeElement("div", "wb-trace-span-track");
      track.setAttribute("aria-hidden", "true");
      const bar = makeElement("div", `wb-trace-span-bar wb-trace-span-bar-${span.stage}`);
      if (span.left !== null && span.width !== null) {
        bar.style.left = `${span.left}%`;
        bar.style.width = `${span.width}%`;
      } else {
        bar.classList.add("is-non-proportional");
      }
      track.appendChild(bar);
      row.appendChild(track);
      row.appendChild(makeElement(
        "p",
        "wb-trace-span-meta",
        `start ${formatMs(span.startMs)} · ${span.status} · source ${span.sourceStage}`,
      ));
      return row;
    });
    byId("trace-timing-spans")?.replaceChildren(...rows);
  }

  function renderDetail(detail) {
    selectedDetail = detail;
    renderSummary(detail);
    renderTiming(detail);
    setText("#trace-raw", JSON.stringify(detail, null, 2));
    const reason = missingText(detail);
    const missing = byId("trace-missing");
    if (missing) {
      missing.textContent = reason;
      missing.hidden = !reason;
    }
    setText("#trace-detail-status", reason
      ? `Trace ${selectedTraceId} loaded with missing evidence.`
      : `Trace ${selectedTraceId} loaded.`);
  }

  async function loadList(context = currentContext, append = false) {
    if (destroyed || !active) return undefined;
    if (append && !nextCursor) return undefined;
    let filters;
    try {
      filters = append ? appliedFilters : readFilters();
      if (!context?.scope?.kb) throw new Error("Knowledge base is required.");
    } catch (error) {
      setText("#trace-list-status", errorMessage(error));
      return undefined;
    }
    const operation = beginOperation("list", context);
    if (!operation) return undefined;
    const cursor = append ? nextCursor : null;
    const scrollTop = byId("trace-table-scroll")?.scrollTop ?? 0;
    const loadMore = byId("trace-load-more");
    if (loadMore) loadMore.disabled = true;
    setText("#trace-list-status", append ? "Loading older traces…" : "Loading traces…");

    try {
      const body = await api(buildTraceListUrl(context.scope, filters, cursor), {
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation)) return undefined;
      if (!append) {
        items = new Map();
        appliedFilters = { ...filters };
      }
      for (const item of Array.isArray(body?.items) ? body.items : []) {
        const id = String(item?.trace_id ?? "").trim();
        if (TRACE_ID_PATTERN.test(id)) items.set(id, item);
      }
      nextCursor = body?.next_cursor ?? null;
      listLoaded = true;
      renderRows(scrollTop);
      setText("#trace-list-status", items.size
        ? `${items.size} trace${items.size === 1 ? "" : "s"} loaded${append ? " including older results" : ""}.`
        : "No traces match these filters.");
      if (loadMore) loadMore.hidden = !nextCursor;
      return body;
    } catch (error) {
      if (operationIsCurrent(operation) && !isAbort(error)) {
        if (append) nextCursor = cursor;
        else nextCursor = null;
        if (loadMore) loadMore.hidden = !nextCursor;
        setText("#trace-list-status", `Trace list unavailable: ${errorMessage(error)}`);
      }
      return undefined;
    } finally {
      const current = finishOperation(operation);
      if (current && !destroyed && loadMore) loadMore.disabled = false;
    }
  }

  async function loadTraces(context = currentContext) {
    return loadList(context, false);
  }

  async function loadMore(context = currentContext) {
    return loadList(context, true);
  }

  function routeTraceId() {
    const match = String(window?.location?.hash ?? "").match(/^#\/traces\/([^/?#]+)$/);
    if (!match) return "";
    try {
      const id = decodeURIComponent(match[1]);
      return TRACE_ID_PATTERN.test(id) ? id : "";
    } catch (_error) {
      return "";
    }
  }

  function viewIsTraces() {
    return /^#\/traces(?:\/|$)/.test(String(window?.location?.hash ?? ""));
  }

  function syncLayout({ focusDrawer = false } = {}) {
    if (destroyed) return;
    root.setAttribute("data-trace-layout", layout);
    const detail = byId("trace-detail");
    const list = byId("trace-list-panel");
    const backdrop = byId("trace-drawer-backdrop");
    if (!detail || !list) return;

    if (layout === "mobile") {
      detail.setAttribute("role", "tabpanel");
      detail.setAttribute("aria-labelledby", "trace-mode-detail");
      detail.removeAttribute("aria-modal");
      list.setAttribute("role", "tabpanel");
      list.setAttribute("aria-labelledby", "trace-mode-list");
      list.hidden = mobileMode !== "list";
      detail.hidden = mobileMode !== "detail";
      if (backdrop) backdrop.hidden = true;
    } else if (layout === "drawer") {
      detail.setAttribute("role", "dialog");
      detail.setAttribute("aria-modal", "true");
      detail.setAttribute("aria-labelledby", "trace-detail-title");
      list.removeAttribute("role");
      list.setAttribute("aria-labelledby", "trace-list-title");
      list.hidden = false;
      detail.hidden = !drawerOpen;
      if (backdrop) backdrop.hidden = !drawerOpen;
      if (focusDrawer && drawerOpen) byId("trace-drawer-close")?.focus();
    } else {
      detail.removeAttribute("role");
      detail.removeAttribute("aria-modal");
      detail.setAttribute("aria-labelledby", "trace-detail-title");
      detail.hidden = false;
      list.removeAttribute("role");
      list.setAttribute("aria-labelledby", "trace-list-title");
      list.hidden = false;
      if (backdrop) backdrop.hidden = true;
    }
  }

  function openDetail(trigger = null) {
    if (trigger) drawerTrigger = trigger;
    const shouldFocus = layout === "drawer" && !drawerOpen;
    drawerOpen = true;
    mobileMode = "detail";
    selectMobileMode("detail");
    syncLayout({ focusDrawer: shouldFocus });
  }

  function closeDrawer({ restoreFocus = true } = {}) {
    if (destroyed) return;
    drawerOpen = false;
    syncLayout();
    if (restoreFocus && layout === "drawer") drawerTrigger?.focus?.();
  }

  async function selectTrace(traceId, {
    context = currentContext,
    updateHash = true,
    trigger = null,
    force = false,
  } = {}) {
    if (destroyed || !active) return undefined;
    const id = String(traceId ?? "").trim();
    if (!TRACE_ID_PATTERN.test(id) || !context?.scope?.kb) return undefined;
    const sameLoadedTrace = id === selectedTraceId && selectedDetail && !force;
    if (sameLoadedTrace) {
      openDetail(trigger);
      if (updateHash) window.location.hash = `#/traces/${encodeURIComponent(id)}`;
      return selectedDetail;
    }
    const operation = beginOperation("detail", context);
    if (!operation) return undefined;
    selectedTraceId = id;
    currentContext = context;
    clearDetail();
    syncSelectedRows();
    openDetail(trigger);
    if (updateHash) window.location.hash = `#/traces/${encodeURIComponent(id)}`;

    try {
      const query = new URLSearchParams({ kb_id: context.scope.kb });
      const detail = await api(`/v1/ops/traces/${encodeURIComponent(id)}/detail?${query.toString()}`, {
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation) || selectedTraceId !== id) return undefined;
      renderDetail(detail);
      return detail;
    } catch (error) {
      if (operationIsCurrent(operation) && !isAbort(error)) {
        setText("#trace-detail-status", `Trace detail unavailable: ${errorMessage(error)}`);
        setText("#trace-timing-status", `Timing unavailable: ${errorMessage(error)}`);
      }
      return undefined;
    } finally {
      finishOperation(operation);
    }
  }

  function selectInspectorTab(tab, { focus = false } = {}) {
    if (destroyed) return;
    const selected = INSPECTOR_TABS.includes(tab) ? tab : "summary";
    currentInspectorTab = selected;
    for (const name of INSPECTOR_TABS) {
      const button = root.querySelector(`[data-trace-tab="${name}"]`);
      const panel = root.querySelector(`[data-trace-panel="${name}"]`);
      const isSelected = name === selected;
      if (button) {
        button.setAttribute("aria-selected", String(isSelected));
        button.setAttribute("tabindex", isSelected ? "0" : "-1");
      }
      if (panel) panel.hidden = !isSelected;
    }
    if (focus) root.querySelector(`[data-trace-tab="${selected}"]`)?.focus();
  }

  function selectMobileMode(mode, { focus = false } = {}) {
    if (destroyed) return;
    mobileMode = MOBILE_MODES.includes(mode) ? mode : "list";
    for (const name of MOBILE_MODES) {
      const button = root.querySelector(`[data-trace-mode="${name}"]`);
      const selected = name === mobileMode;
      if (button) {
        button.setAttribute("aria-selected", String(selected));
        button.setAttribute("tabindex", selected ? "0" : "-1");
      }
    }
    if (layout === "mobile") syncLayout();
    if (focus) root.querySelector(`[data-trace-mode="${mobileMode}"]`)?.focus();
  }

  async function copyRaw() {
    if (destroyed) return false;
    const raw = byId("trace-raw")?.textContent ?? "";
    if (!raw || !selectedDetail) {
      setText("#trace-copy-status", "No Raw JSON is available to copy.");
      return false;
    }
    const copyContext = currentContext;
    const copyTraceId = selectedTraceId;
    const copyDetail = selectedDetail;
    const copyIsCurrent = () => !destroyed
      && active
      && currentContext === copyContext
      && selectedTraceId === copyTraceId
      && selectedDetail === copyDetail
      && !copyContext?.signal?.aborted
      && (!guard || guard.isCurrent(copyContext));
    try {
      const targetClipboard = clipboard ?? globalThis.navigator?.clipboard;
      if (!targetClipboard?.writeText) throw new Error("Clipboard API is unavailable");
      await targetClipboard.writeText(raw);
      if (!copyIsCurrent()) return false;
      setText("#trace-copy-status", "Copied Raw JSON to the clipboard.");
      return true;
    } catch (error) {
      if (copyIsCurrent()) {
        setText("#trace-copy-status", `Copy failed: ${errorMessage(error)}. Select and copy the Raw JSON manually.`);
      }
      return false;
    }
  }

  function nextTab(names, current, key) {
    const index = names.indexOf(current);
    if (key === "ArrowRight") return names[(index + 1) % names.length];
    if (key === "ArrowLeft") return names[(index - 1 + names.length) % names.length];
    if (key === "Home") return names[0];
    if (key === "End") return names[names.length - 1];
    return null;
  }

  function trapDrawerFocus(event) {
    const detail = byId("trace-detail");
    if (!detail) return;
    const focusable = [...detail.querySelectorAll(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]',
    )].filter((element) => element.tabIndex >= 0 && element.getClientRects().length > 0);
    if (!focusable.length) {
      event.preventDefault();
      detail.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const current = document.activeElement;
    if (!focusable.includes(current)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && current === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && current === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function onRootKeydown(event) {
    if (destroyed) return;
    if (event.key === "Escape" && layout === "drawer" && drawerOpen) {
      event.preventDefault();
      closeDrawer();
      return;
    }
    if (event.key === "Tab" && layout === "drawer" && drawerOpen) {
      trapDrawerFocus(event);
      return;
    }
    const inspector = event.target?.closest?.("[data-trace-tab]");
    if (inspector?.dataset?.traceTab) {
      const next = nextTab(INSPECTOR_TABS, inspector.dataset.traceTab, event.key);
      if (!next) return;
      event.preventDefault();
      selectInspectorTab(next, { focus: true });
      return;
    }
    const mobile = event.target?.closest?.("[data-trace-mode]");
    if (mobile?.dataset?.traceMode && layout === "mobile") {
      const next = nextTab(MOBILE_MODES, mobile.dataset.traceMode, event.key);
      if (!next) return;
      event.preventDefault();
      selectMobileMode(next, { focus: true });
    }
  }

  function onRootClick(event) {
    if (destroyed) return;
    const trace = event.target?.closest?.("[data-trace-select]");
    if (trace?.dataset?.traceSelect) {
      void selectTrace(trace.dataset.traceSelect, { trigger: trace });
      return;
    }
    const tab = event.target?.closest?.("[data-trace-tab]");
    if (tab?.dataset?.traceTab) {
      selectInspectorTab(tab.dataset.traceTab);
      return;
    }
    const mode = event.target?.closest?.("[data-trace-mode]");
    if (mode?.dataset?.traceMode && layout === "mobile") {
      selectMobileMode(mode.dataset.traceMode);
      return;
    }
    if (event.target?.closest?.("[data-trace-drawer-close]")) {
      closeDrawer();
      return;
    }
    if (event.target?.closest?.("#trace-load-more")) {
      void loadMore();
      return;
    }
    if (event.target?.closest?.("#trace-copy")) void copyRaw();
  }

  function onRootSubmit(event) {
    if (destroyed) return;
    const form = event.target?.closest?.("form") ?? event.target;
    if (form?.id !== "trace-filter-form") return;
    event.preventDefault();
    void loadTraces();
  }

  function restoreHashSelection() {
    if (destroyed || !active || !viewIsTraces()) return;
    const id = routeTraceId();
    if (!id) return;
    if (id === selectedTraceId && (selectedDetail || operations.detail)) {
      openDetail();
      return;
    }
    void selectTrace(id, { updateHash: false });
  }

  function onHashChange() {
    if (destroyed || !active || !viewIsTraces()) return;
    if (routeTraceId()) {
      restoreHashSelection();
    } else {
      selectMobileMode("list");
      if (layout === "drawer") closeDrawer({ restoreFocus: false });
    }
  }

  function onScopeChange(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    clear({ preserveContext: true });
  }

  function onRefresh(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    if (event.detail?.view !== "traces" || !active || document?.visibilityState === "hidden") return;
    void loadTraces(currentContext);
    if (selectedTraceId) {
      void selectTrace(selectedTraceId, {
        context: currentContext,
        updateHash: false,
        force: true,
      });
    }
  }

  function onVisibilityChange() {
    if (!destroyed && document.visibilityState === "hidden") cancelAll();
  }

  function updateLayout() {
    if (destroyed) return;
    if (mediaQueries[0]?.matches) layout = "mobile";
    else if (mediaQueries[1]?.matches) layout = "drawer";
    else layout = "wide";
    syncLayout();
  }

  function setActive(value) {
    if (destroyed) return;
    const next = Boolean(value);
    active = next;
    if (!active) {
      cancelAll();
      drawerOpen = false;
      syncLayout();
      return;
    }
    syncLayout();
    onHashChange();
  }

  function clear({ preserveContext = false } = {}) {
    if (destroyed) return;
    cancelAll();
    if (!preserveContext) currentContext = null;
    selectedTraceId = "";
    selectedDetail = null;
    items = new Map();
    nextCursor = null;
    appliedFilters = null;
    listLoaded = false;
    drawerOpen = false;
    drawerTrigger = null;
    if (currentContext?.scope?.window && byId("trace-window")) {
      byId("trace-window").value = currentContext.scope.window;
    }
    if (viewIsTraces()) window?.history?.replaceState(null, "", "#/traces");
    clearDetail();
    renderRows(0);
    setText("#trace-list-status", "Apply filters or refresh to load traces.");
    const loadMoreButton = byId("trace-load-more");
    if (loadMoreButton) {
      loadMoreButton.hidden = true;
      loadMoreButton.disabled = false;
    }
    selectMobileMode("list");
    syncLayout();
  }

  root.addEventListener("submit", onRootSubmit);
  root.addEventListener("click", onRootClick);
  root.addEventListener("keydown", onRootKeydown);
  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  window?.addEventListener("hashchange", onHashChange);
  document?.addEventListener("visibilitychange", onVisibilityChange);
  if (window?.matchMedia) {
    for (const query of [
      "(max-width: 899px)",
      "(min-width: 900px) and (max-width: 1439px)",
    ]) {
      const media = window.matchMedia(query);
      mediaQueries.push(media);
      media.addEventListener?.("change", updateLayout);
    }
  }
  selectInspectorTab(currentInspectorTab);
  selectMobileMode(mobileMode);
  updateLayout();

  return {
    loadTraces,
    loadMore,
    selectTrace,
    copyRaw,
    selectInspectorTab,
    setActive,
    clear,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      active = false;
      cancelAll();
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      window?.removeEventListener("hashchange", onHashChange);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
      for (const media of mediaQueries) media.removeEventListener?.("change", updateLayout);
      root.removeEventListener?.("submit", onRootSubmit);
      root.removeEventListener?.("click", onRootClick);
      root.removeEventListener?.("keydown", onRootKeydown);
    },
  };
}
