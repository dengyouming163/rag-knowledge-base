import { apiJson } from "../api.mjs";
import { escapeHtml, formatMetric } from "../core.mjs";
import { formatError, t } from "../i18n/index.mjs";

const REFRESH_INTERVAL_MS = 15000;
const LOW_SAMPLE_THRESHOLD = 30;

function trendDefinitions() {
  return [
    { key: "requests", label: t("overview.trend.requests"), unit: "req" },
    { key: "e2e_p95_ms", label: t("overview.trend.e2e"), unit: "ms" },
    { key: "retrieval_p95_ms", label: t("overview.trend.retrieval"), unit: "ms" },
    { key: "llm_p95_ms", label: t("overview.trend.llm"), unit: "ms" },
  ];
}
const METRIC_NAMES = [
  "requests", "avg_qpm", "client_errors", "server_errors", "e2e_p95",
  "retrieval_p95", "llm_p95", "zero_hit", "refusal", "citation",
  "tokens", "cost", "queue", "workers",
];

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function percentage(value) {
  const numeric = finiteNumber(value);
  return numeric === null ? null : numeric * 100;
}

export function costStatus(tokens = {}) {
  if (!tokens.pricing_configured) return t("overview.cost.notConfigured");
  return t(tokens.pricing_complete ? "overview.cost.complete" : "overview.cost.partial");
}

export function renderTrend(series, { key = "requests", label = t("overview.trend.requests"), unit = "" } = {}) {
  const points = (Array.isArray(series) ? series : [])
    .map((point) => ({ ts: String(point?.ts ?? ""), value: finiteNumber(point?.[key]) }));
  const validPoints = points.filter((point) => point.value !== null);

  if (!validPoints.length) {
    return `<div class="wb-overview-empty" data-trend-empty>${escapeHtml(t("overview.trend.empty", { label }))}</div>`;
  }

  const width = 640;
  const height = 180;
  const left = 38;
  const right = 16;
  const top = 18;
  const bottom = 34;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const values = validPoints.map((point) => point.value);
  const dataMinimum = Math.min(...values);
  const dataMaximum = Math.max(...values);
  const scale = Math.max(...values.map((value) => Math.abs(value))) || 1;
  const normalizedValues = points.map((point) => point.value === null ? null : point.value / scale);
  let minimum = Math.min(...values.map((value) => value / scale));
  let maximum = Math.max(...values.map((value) => value / scale));
  if (minimum === maximum) {
    const padding = Math.max(1, Math.abs(maximum) * 0.1);
    minimum -= padding;
    maximum += padding;
  }
  const coordinate = (value) => Number(value.toFixed(2));
  const coordinates = points.map((point, index) => ({
    x: coordinate(points.length === 1 ? left + plotWidth / 2 : left + index * plotWidth / (points.length - 1)),
    y: point.value === null ? null : coordinate(top + (maximum - normalizedValues[index]) * plotHeight / (maximum - minimum)),
  }));
  if (!coordinates.every((point) => Number.isFinite(point.x) && (point.y === null || Number.isFinite(point.y)))) {
    return `<div class="wb-overview-empty" data-trend-empty>${escapeHtml(t("overview.trend.empty", { label }))}</div>`;
  }
  const path = coordinates.flatMap((point, index) => point.y === null ? [] : [
    `${index === 0 || coordinates[index - 1].y === null ? "M" : "L"} ${point.x} ${point.y}`,
  ]).join(" ");
  const circles = coordinates.filter((point) => point.y !== null).map((point) => (
    `<circle cx="${point.x}" cy="${point.y}" r="2.5"></circle>`
  )).join("");
  const firstTimestamp = escapeHtml(points[0].ts);
  const lastTimestamp = escapeHtml(points.at(-1).ts);
  const description = t("overview.trend.description", {
    label,
    start: points[0].ts,
    end: points.at(-1).ts,
    valid: validPoints.length,
    total: points.length,
    minimum: formatMetric(dataMinimum, unit, 1),
    maximum: formatMetric(dataMaximum, unit, 1),
  });

  return `<svg class="wb-overview-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(t("overview.trend.ariaLabel", { label }))}" tabindex="0">
    <title>${escapeHtml(label)}</title>
    <desc>${escapeHtml(description)}</desc>
    <line class="wb-overview-axis" x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}"></line>
    <text class="wb-overview-axis-label" x="4" y="${top + 4}">${escapeHtml(formatMetric(dataMaximum, "", 1))}</text>
    <text class="wb-overview-axis-label" x="4" y="${height - bottom}">${escapeHtml(formatMetric(dataMinimum, "", 1))}</text>
    <text class="wb-overview-unit" x="${width - right}" y="12" text-anchor="end">${escapeHtml(unit)}</text>
    <path d="${path}" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke"></path>
    ${circles}
    <text class="wb-overview-axis-label" x="${left}" y="${height - 10}">${firstTimestamp}</text>
    <text class="wb-overview-axis-label" x="${width - right}" y="${height - 10}" text-anchor="end">${lastTimestamp}</text>
  </svg>`;
}

function metricDefinitions(summary) {
  const observability = summary?.observability ?? {};
  const http = observability.http ?? {};
  const rag = observability.rag ?? {};
  const latency = rag.latency_ms ?? {};
  const tokens = observability.tokens ?? {};
  const queue = summary?.queue ?? {};
  const workers = Array.isArray(summary?.workers) ? summary.workers.length : null;
  const quantile = (name) => ({
    value: latency[name]?.p95,
    unit: "ms",
    digits: 1,
    samples: latency[name]?.samples,
  });
  const rate = (name) => ({
    value: percentage(rag[name]?.rate),
    unit: "%",
    digits: 1,
    samples: rag[name]?.denominator,
  });

  return {
    requests: { value: http.requests, unit: "req", digits: 0, samples: http.requests },
    avg_qpm: { value: http.avg_qpm, unit: "req/min", digits: 2, samples: http.requests },
    client_errors: { value: percentage(http["4xx_rate"]), unit: "%", digits: 1, samples: http.requests },
    server_errors: { value: percentage(http["5xx_rate"]), unit: "%", digits: 1, samples: http.requests },
    e2e_p95: quantile("e2e"),
    retrieval_p95: quantile("retrieve"),
    llm_p95: quantile("llm"),
    zero_hit: rate("zero_hit"),
    refusal: rate("refusal"),
    citation: rate("citation"),
    tokens: { value: tokens.total, unit: "tokens", digits: 0, samples: rag.operations },
    cost: {
      value: tokens.estimated_cost_usd,
      unit: "USD",
      digits: 6,
      samples: rag.operations,
      note: summary?.observability?.tokens ? costStatus(tokens) : "",
    },
    queue: { value: queue.queued, unit: t("overview.queueUnit"), digits: 0, snapshot: true },
    workers: { value: workers, unit: t("overview.workersUnit"), digits: 0, snapshot: true },
  };
}

function scopeKey(scope) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user, scope.window]);
}

function endpoint(path, scope) {
  const query = new URLSearchParams({ kb_id: scope.kb ?? "", window: scope.window || "24h" });
  return `${path}?${query.toString()}`;
}

export function createOverview({
  root,
  guard,
  api = apiJson,
  document = globalThis.document,
  window = globalThis.window,
  setInterval = globalThis.setInterval,
  clearInterval = globalThis.clearInterval,
  now = () => new Date(),
} = {}) {
  if (!root) {
    return {
      async loadOverview() {},
      setActive() {},
      clear() {},
      destroy() {},
    };
  }

  let active = false;
  let destroyed = false;
  let timer = null;
  let requestVersion = 0;
  let currentContext = null;
  let currentScopeKey = null;
  let summaryCache = null;
  let trendsCache = null;
  const flights = { summary: null, trends: null };

  function setText(selector, value) {
    const element = root.querySelector(selector);
    if (element) element.textContent = value;
  }

  function setPanelStatus(panel, message, state, timestamps = {}) {
    const status = root.querySelector(`[data-overview-${panel}-status]`);
    if (!status) return;
    status.textContent = message;
    status.setAttribute("data-state", state);
    status.removeAttribute("data-stale-at");
    status.removeAttribute("data-last-success-at");
    for (const [name, value] of Object.entries(timestamps)) {
      status.setAttribute(`data-${name}`, value);
    }
  }

  function renderSummary(summary, fallbackWindow = "—") {
    const definitions = metricDefinitions(summary);
    const window = summary?.observability?.window || fallbackWindow || "—";
    for (const name of METRIC_NAMES) {
      const definition = definitions[name];
      const samples = finiteNumber(definition.samples);
      const lowSample = !definition.snapshot && samples !== null && samples >= 0 && samples < LOW_SAMPLE_THRESHOLD;
      const notes = [definition.note, lowSample ? t("overview.lowSample") : ""].filter(Boolean);
      setText(`[data-overview-value="${name}"]`, formatMetric(definition.value, "", definition.digits));
      setText(`[data-overview-unit="${name}"]`, definition.unit);
      setText(`[data-overview-window="${name}"]`, definition.snapshot ? t("overview.snapshot") : window);
      setText(`[data-overview-samples="${name}"]`, definition.snapshot ? "—" : formatMetric(definition.samples, "", 0));
      setText(`[data-overview-note="${name}"]`, notes.join(" · "));
      const note = root.querySelector(`[data-overview-note="${name}"]`);
      if (note) note.hidden = notes.length === 0;
    }
  }

  function renderTrends(data) {
    const series = Array.isArray(data?.series) ? data.series : [];
    for (const trend of trendDefinitions()) {
      const container = root.querySelector(`[data-overview-trend-svg="${trend.key}"]`);
      if (container) container.innerHTML = renderTrend(series, trend);
    }
  }

  function resetPanels() {
    cancelPanels();
    summaryCache = null;
    trendsCache = null;
    renderSummary(null);
    renderTrends(null);
    setPanelStatus("summary", t("overview.awaitingRefresh"), "idle");
    setPanelStatus("trends", t("overview.awaitingRefresh"), "idle");
  }

  function clear() {
    requestVersion += 1;
    currentContext = null;
    currentScopeKey = null;
    resetPanels();
  }

  function canRender(context, version) {
    return !destroyed
      && active
      && document.visibilityState === "visible"
      && version === requestVersion
      && scopeKey(context.scope) === currentScopeKey
      && guard.isCurrent(context);
  }

  function cancelPanel(panel, pause = false) {
    const flight = flights[panel];
    if (!flight) return;
    flights[panel] = null;
    flight.dispose();
    flight.controller.abort();
    if (pause) setPanelStatus(panel, t("overview.refreshPaused"), "idle");
  }

  function cancelPanels({ pause = false } = {}) {
    for (const panel of Object.keys(flights)) cancelPanel(panel, pause);
  }

  function pausePanels() {
    requestVersion += 1;
    cancelPanels({ pause: true });
  }

  function startPanel(panel, context, version, skipPending, loader) {
    const existing = flights[panel];
    if (existing && skipPending && guard.isCurrent(existing.context)) return existing.promise;
    if (existing) cancelPanel(panel);

    const controller = new AbortController();
    const parentSignal = context.signal;
    const onAbort = () => controller.abort(parentSignal.reason);
    if (parentSignal.aborted) onAbort();
    else parentSignal.addEventListener("abort", onAbort, { once: true });
    const flight = {
      context,
      controller,
      promise: null,
      dispose() {
        parentSignal.removeEventListener("abort", onAbort);
      },
    };
    flights[panel] = flight;
    flight.promise = loader(context, version, controller.signal).finally(() => {
      flight.dispose();
      if (flights[panel] === flight) flights[panel] = null;
    });
    return flight.promise;
  }

  async function loadSummary(context, version, signal) {
    const cached = summaryCache?.key === currentScopeKey ? summaryCache : null;
    if (cached) renderSummary(cached.data, context.scope.window);
    setPanelStatus("summary", t(cached ? "overview.refreshingSummary" : "overview.loadingSummary"), "loading");
    try {
      const data = await api(endpoint("/v1/ops/summary", context.scope), {
        scope: context.scope,
        signal,
      });
      if (!canRender(context, version)) return;
      const updatedAt = now().toISOString();
      summaryCache = { key: currentScopeKey, data, updatedAt };
      renderSummary(data, context.scope.window);
      setPanelStatus("summary", t("overview.updated", { time: updatedAt }), "success");
    } catch (error) {
      if (!canRender(context, version)) return;
      const staleAt = now().toISOString();
      if (cached) {
        renderSummary(cached.data, context.scope.window);
        setPanelStatus(
          "summary",
          t("overview.stale", { time: staleAt, lastSuccess: cached.updatedAt, error: formatError(error) }),
          "stale",
          { "stale-at": staleAt, "last-success-at": cached.updatedAt },
        );
      } else {
        renderSummary(null, context.scope.window);
        setPanelStatus("summary", t("overview.summaryUnavailable", { error: formatError(error) }), "error");
      }
    }
  }

  async function loadTrends(context, version, signal) {
    const cached = trendsCache?.key === currentScopeKey ? trendsCache : null;
    if (cached) renderTrends(cached.data);
    setPanelStatus("trends", t(cached ? "overview.refreshingTrends" : "overview.loadingTrends"), "loading");
    try {
      const data = await api(endpoint("/v1/ops/metrics/timeseries", context.scope), {
        scope: context.scope,
        signal,
      });
      if (!canRender(context, version)) return;
      const updatedAt = now().toISOString();
      trendsCache = { key: currentScopeKey, data, updatedAt };
      renderTrends(data);
      setPanelStatus("trends", t("overview.updated", { time: updatedAt }), "success");
    } catch (error) {
      if (!canRender(context, version)) return;
      const staleAt = now().toISOString();
      if (cached) {
        renderTrends(cached.data);
        setPanelStatus(
          "trends",
          t("overview.stale", { time: staleAt, lastSuccess: cached.updatedAt, error: formatError(error) }),
          "stale",
          { "stale-at": staleAt, "last-success-at": cached.updatedAt },
        );
      } else {
        renderTrends(null);
        setPanelStatus("trends", t("overview.trendsUnavailable", { error: formatError(error) }), "error");
      }
    }
  }

  async function loadOverview(scopeSnapshot, { skipPending = false } = {}) {
    if (destroyed || !scopeSnapshot?.scope || !guard.isCurrent(scopeSnapshot)) return;
    const key = scopeKey(scopeSnapshot.scope);
    if (key !== currentScopeKey) {
      requestVersion += 1;
      currentScopeKey = key;
      resetPanels();
    }
    currentContext = scopeSnapshot;
    const version = skipPending ? requestVersion : ++requestVersion;
    const summaryRequest = startPanel("summary", scopeSnapshot, version, skipPending, loadSummary);
    const trendsRequest = startPanel("trends", scopeSnapshot, version, skipPending, loadTrends);
    await Promise.allSettled([summaryRequest, trendsRequest]);
  }

  function clearTimer() {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  function syncTimer() {
    clearTimer();
    if (!destroyed && active && document.visibilityState === "visible") {
      timer = setInterval(onTimer, REFRESH_INTERVAL_MS);
    }
  }

  function onTimer() {
    if (
      destroyed
      || !active
      || document.visibilityState !== "visible"
      || !currentContext
      || !guard.isCurrent(currentContext)
    ) return undefined;
    return loadOverview(currentContext, { skipPending: true });
  }

  function setActive(value) {
    const next = Boolean(value);
    if (active === next) return;
    active = next;
    if (!active) pausePanels();
    syncTimer();
  }

  function onRefresh(event) {
    const detail = event.detail;
    if (
      currentContext
      && detail?.scope
      && scopeKey(detail.scope) === currentScopeKey
      && guard.isCurrent(detail)
    ) {
      currentContext = detail;
    }
    if (detail?.view !== "overview" || !active || document.visibilityState !== "visible") return;
    void loadOverview(detail);
  }

  function onVisibilityChange() {
    if (document.visibilityState !== "visible") pausePanels();
    syncTimer();
  }

  function destroy() {
    destroyed = true;
    requestVersion += 1;
    cancelPanels();
    clearTimer();
    window.removeEventListener("workbuddy:refresh", onRefresh);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  }

  window.addEventListener("workbuddy:refresh", onRefresh);
  document.addEventListener("visibilitychange", onVisibilityChange);
  resetPanels();

  return { loadOverview, setActive, clear, destroy };
}
