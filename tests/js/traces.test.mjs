import test from "node:test";
import assert from "node:assert/strict";

import { ScopeGuard } from "../../app/static/js/core.mjs";
import {
  buildTimingModel,
  buildTraceListUrl,
  createTraces,
} from "../../app/static/js/views/traces.mjs";


class FakeElement {
  constructor(tagName = "div", ownerDocument = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.scrollTop = 0;
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      remove: (...names) => names.forEach((name) => this.classList.values.delete(name)),
      contains: (name) => this.classList.values.has(name),
      toggle: (name, enabled) => {
        if (enabled) this.classList.values.add(name);
        else this.classList.values.delete(name);
      },
    };
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) {
      const key = name.slice(5).replaceAll(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) => this.appendChild(child));
  }

  replaceChildren(...children) {
    this.children = [];
    this.textContent = "";
    this.innerHTML = "";
    this.append(...children);
  }

  closest(selector) {
    const match = selector.match(/^\[data-([\w-]+)\]$/);
    let element = this;
    while (element) {
      if (match) {
        const key = match[1].replaceAll(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
        if (element.dataset?.[key] !== undefined) return element;
      }
      element = element.parentElement;
    }
    return null;
  }

  focus() {
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
}

class FakeRoot extends FakeElement {
  constructor(ownerDocument) {
    super("section", ownerDocument);
    this.elements = new Map();
    this.listeners = new Map();
  }

  querySelector(selector) {
    if (!this.elements.has(selector)) {
      this.elements.set(selector, new FakeElement("div", this.ownerDocument));
    }
    return this.elements.get(selector);
  }

  querySelectorAll(selector) {
    const values = [...this.elements.values()];
    const visit = (element) => {
      values.push(element);
      element.children.forEach(visit);
    };
    this.children.forEach(visit);
    for (const element of this.elements.values()) element.children.forEach(visit);
    const match = selector.match(/^\[data-([\w-]+)\]$/);
    if (!match) return [];
    const key = match[1].replaceAll(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    return [...new Set(values)].filter((element) => element.dataset?.[key] !== undefined);
  }

  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }

  removeEventListener(type, callback) {
    const callbacks = this.listeners.get(type) ?? [];
    this.listeners.set(type, callbacks.filter((listener) => listener !== callback));
  }

  listenerCount(type) {
    return (this.listeners.get(type) ?? []).length;
  }

  dispatch(type, target = new FakeElement("button", this.ownerDocument), init = {}) {
    const event = {
      ...init,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
      target,
    };
    for (const callback of [...(this.listeners.get(type) ?? [])]) callback(event);
    return event;
  }

  text(selector) {
    return this.querySelector(selector).textContent;
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function traceRow(traceId, overrides = {}) {
  return {
    id: traceId === "trace-a" ? 2 : 1,
    occurred_at: "2026-11-20T10:00:00Z",
    trace_id: traceId,
    operation: "search",
    mode: "vector",
    outcome: "succeeded",
    hits: 2,
    total_ms: 100,
    llm_ms: 40,
    model: "model-a",
    prompt_tokens: 11,
    completion_tokens: 7,
    stage_spans: [{ stage: "retrieve", start_ms: 0, duration_ms: 25, status: "ok" }],
    ...overrides,
  };
}

function detailFixture(traceId = "trace-a", overrides = {}) {
  return {
    metric: {
      ...traceRow(traceId),
      kb_id: "kb_1",
      citation_ok: true,
      rerank_degraded: false,
      total_ms: 100,
      prompt_tokens: 11,
      completion_tokens: 7,
      error_kind: null,
      stage_spans: [
        { stage: "rewrite", start_ms: 0, duration_ms: 10, status: "ok" },
        { stage: "retrieve", start_ms: 10, duration_ms: 20, status: "ok" },
        { stage: "answer", start_ms: 30, duration_ms: 40, status: "ok" },
      ],
    },
    events: [{ trace_id: traceId, query: `<img src=x onerror="alert(1)">` }],
    missing: [],
    ...overrides,
  };
}

function createFixture(t, { api, initialHash = "#/traces", clipboard, ...options } = {}) {
  const document = new EventTarget();
  document.visibilityState = "visible";
  document.activeElement = null;
  document.createElement = (tag) => new FakeElement(tag, document);
  document.createTextNode = (text) => new FakeElement("#text", document);
  const root = new FakeRoot(document);
  for (const [id, value] of Object.entries({
    "trace-window": "24h",
    "trace-outcome": "",
    "trace-operation": "",
    "trace-mode": "",
    "trace-min-ms": "",
    "trace-id": "",
    "trace-limit": "2",
  })) {
    root.querySelector(`#${id}`).value = value;
  }
  for (const tab of ["summary", "timing", "raw"]) {
    const button = root.querySelector(`[data-trace-tab="${tab}"]`);
    button.dataset.traceTab = tab;
    const panel = root.querySelector(`[data-trace-panel="${tab}"]`);
    panel.dataset.tracePanel = tab;
    panel.hidden = tab !== "summary";
  }
  for (const mode of ["list", "detail"]) {
    const button = root.querySelector(`[data-trace-mode="${mode}"]`);
    button.dataset.traceMode = mode;
  }
  const guard = new ScopeGuard();
  const window = new EventTarget();
  window.location = { hash: initialHash };
  window.history = {
    replaceState(_state, _unused, hash) { window.location.hash = hash; },
  };
  const mediaListeners = [];
  window.matchMedia = (query) => ({
    media: query,
    matches: false,
    addEventListener(type, callback) { mediaListeners.push({ type, callback }); },
    removeEventListener(type, callback) {
      const index = mediaListeners.findIndex((item) => item.type === type && item.callback === callback);
      if (index >= 0) mediaListeners.splice(index, 1);
    },
  });
  const scope = { tenant: "tenant_a", kb: "kb_1", user: "user_1", window: "24h" };
  const view = createTraces({
    root,
    guard,
    scope,
    window,
    document,
    api: api ?? (async (path) => path.includes("/detail")
      ? detailFixture(new URL(path, "http://workbuddy.test").pathname.split("/").at(-2))
      : { items: [traceRow("trace-a")], next_cursor: null }),
    clipboard: clipboard ?? { writeText: async () => undefined },
    ...options,
  });
  view.setActive(true);
  t.after(() => view.destroy());
  const context = () => ({ scope: { ...scope }, ...guard.snapshot() });
  return { root, guard, window, document, mediaListeners, view, context };
}


test("trace list URLs preserve every valid filter and the opaque backend cursor", () => {
  const path = buildTraceListUrl(
    { tenant: "tenant_a", kb: "kb with space", window: "24h" },
    {
      window: "7d",
      outcome: "refused",
      operation: "search",
      mode: "vector + hybrid",
      min_ms: 0,
      trace_id: "trace:a.b-c_d",
      limit: 25,
    },
    { before_occurred_at: "2026-11-20T10:00:00+00:00", before_id: 42 },
  );
  const url = new URL(path, "http://workbuddy.test");

  assert.equal(url.pathname, "/v1/ops/trace-list");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    kb_id: "kb with space",
    window: "7d",
    outcome: "refused",
    operation: "search",
    mode: "vector + hybrid",
    min_ms: "0",
    trace_id: "trace:a.b-c_d",
    limit: "25",
    before_occurred_at: "2026-11-20T10:00:00+00:00",
    before_id: "42",
  });
  assert.equal(url.searchParams.has("cursor"), false);
  assert.equal(url.searchParams.has("page"), false);
  assert.equal(url.searchParams.has("tenant"), false);
});

test("trace list URLs omit blank filters and stop without a next cursor", () => {
  const url = new URL(buildTraceListUrl({ kb: "kb_1", window: "1h" }, {}, null), "http://workbuddy.test");
  assert.equal(url.searchParams.get("kb_id"), "kb_1");
  assert.equal(url.searchParams.get("window"), "1h");
  assert.equal(url.searchParams.get("limit"), "20");
  for (const name of ["outcome", "operation", "mode", "min_ms", "trace_id", "before_occurred_at", "before_id"]) {
    assert.equal(url.searchParams.has(name), false, name);
  }
});

test("timing keeps every named unknown span in other and only uses explicit unaccounted time", () => {
  const model = buildTimingModel({
    metric: {
      total_ms: 100,
      timings_unaccounted_ms: 20,
      stage_spans: [
        { stage: "rewrite", start_ms: 0, duration_ms: 10, status: "ok" },
        { stage: "retrieve", start_ms: 10, duration_ms: 20, status: "ok" },
        { stage: "rerank", start_ms: 30, duration_ms: 10, status: "ok" },
        { stage: "answer", start_ms: 40, duration_ms: 30, status: "ok" },
        { stage: "guardrail", start_ms: 70, duration_ms: 10, status: "ok" },
      ],
    },
  });

  assert.deepEqual(model.spans.map((span) => span.stage), [
    "rewrite", "retrieval", "rerank", "llm", "other", "unaccounted",
  ]);
  const other = model.spans.find((span) => span.stage === "other");
  assert.match(other.label, /guardrail/);
  assert.equal(other.durationMs, 10);
  assert.equal(other.width, 10);
  const unaccounted = model.spans.at(-1);
  assert.equal(unaccounted.durationMs, 20);
  assert.equal(unaccounted.source, "timings_unaccounted_ms");
  assert.ok(model.spans.every((span) => Number.isFinite(span.left) && Number.isFinite(span.width)));

  const withoutExplicit = buildTimingModel({ metric: { total_ms: 100, stage_spans: [] } });
  assert.equal(withoutExplicit.spans.some((span) => span.stage === "unaccounted"), false);
});

test("zero and missing totals never generate Infinity or NaN and remain readable", () => {
  for (const totalMs of [0, null, undefined]) {
    const model = buildTimingModel({
      metric: {
        total_ms: totalMs,
        stage_spans: [{ stage: "custom", start_ms: 0, duration_ms: 0, status: "ok" }],
      },
    });
    assert.equal(model.proportional, false);
    assert.match(model.status, totalMs === 0 ? /Zero-duration/ : /not recorded/);
    assert.ok(model.spans.every((span) => span.left === null && span.width === null && span.percentage === null));
    assert.doesNotMatch(JSON.stringify(model), /Infinity|NaN/);
  }
});

test("explicit list loading and cursor pagination retain filters, unique selection, and scroll", async (t) => {
  const calls = [];
  const cursor = { before_occurred_at: "2026-11-20T09:59:59+00:00", before_id: 99 };
  const fixture = createFixture(t, {
    api: async (path, options) => {
      calls.push({ path, options });
      const url = new URL(path, "http://workbuddy.test");
      if (url.pathname.endsWith("/detail")) return detailFixture("trace-a");
      if (url.searchParams.has("before_id")) {
        return { items: [traceRow("trace-a"), traceRow("trace-b")], next_cursor: null };
      }
      return { items: [traceRow("trace-a")], next_cursor: cursor };
    },
  });
  const { root, view, context } = fixture;
  Object.assign(root.querySelector("#trace-window"), { value: "7d" });
  Object.assign(root.querySelector("#trace-outcome"), { value: "succeeded" });
  Object.assign(root.querySelector("#trace-operation"), { value: "search" });
  Object.assign(root.querySelector("#trace-mode"), { value: "vector" });
  Object.assign(root.querySelector("#trace-min-ms"), { value: "0" });
  Object.assign(root.querySelector("#trace-id"), { value: "trace-a" });
  root.querySelector("#trace-limit").value = "2";

  await view.loadTraces(context());
  await view.selectTrace("trace-a", { context: context(), updateHash: true });
  root.querySelector("#trace-table-scroll").scrollTop = 33;
  root.querySelector("#trace-outcome").value = "error";
  await view.loadMore(context());

  const listCalls = calls.filter((call) => call.path.includes("/trace-list"));
  assert.equal(listCalls.length, 2);
  const second = new URL(listCalls[1].path, "http://workbuddy.test");
  assert.equal(second.searchParams.get("before_occurred_at"), cursor.before_occurred_at);
  assert.equal(second.searchParams.get("before_id"), "99");
  for (const [name, value] of Object.entries({
    kb_id: "kb_1", window: "7d", outcome: "succeeded", operation: "search",
    mode: "vector", min_ms: "0", trace_id: "trace-a", limit: "2",
  })) assert.equal(second.searchParams.get(name), value, name);
  assert.equal(root.querySelectorAll("[data-trace-select]").length, 2, "duplicate pages must not duplicate a trace");
  assert.equal(root.querySelectorAll("[data-trace-select]").filter((row) => row.getAttribute("aria-current") === "true").length, 1);
  assert.equal(root.querySelector("#trace-load-more").hidden, true);
  assert.equal(root.querySelector("#trace-table-scroll").scrollTop, 33);
  assert.equal(fixture.window.location.hash, "#/traces/trace-a");

  await view.loadMore(context());
  assert.equal(listCalls.length, 2, "no next_cursor must not issue another request");

  await view.loadTraces(context());
  assert.equal(fixture.window.location.hash, "#/traces/trace-a");
  assert.equal(root.querySelector("#trace-id").value, "trace-a");
  assert.equal(root.querySelector("#trace-table-scroll").scrollTop, 33);
});

test("detail requests are KB scoped and Summary, missing, Raw copy, and keyboard tabs are safe", async (t) => {
  const copied = [];
  let failCopy = false;
  const calls = [];
  const fixture = createFixture(t, {
    clipboard: {
      async writeText(value) {
        if (failCopy) throw new Error("permission denied");
        copied.push(value);
      },
    },
    api: async (path, options) => {
      calls.push({ path, options });
      const traceId = new URL(path, "http://workbuddy.test").pathname.split("/").at(-2);
      if (traceId === "trace-missing") {
        return { metric: null, events: [{ query: `<script>alert("raw")</script>` }], missing: ["metric"] };
      }
      if (traceId === "trace-no-events") return { ...detailFixture(traceId), events: [], missing: ["events"] };
      return detailFixture(traceId);
    },
  });
  const { root, view, context } = fixture;

  await view.selectTrace("trace-missing", { context: context(), updateHash: true });
  const detailCall = calls[0];
  const url = new URL(detailCall.path, "http://workbuddy.test");
  assert.equal(url.pathname, "/v1/ops/traces/trace-missing/detail");
  assert.equal(url.searchParams.get("kb_id"), "kb_1");
  assert.equal([...url.searchParams.keys()].includes("window"), false);
  assert.equal(detailCall.options.scope.tenant, "tenant_a");
  assert.notEqual(detailCall.options.signal, context().signal);
  assert.match(root.text("#trace-missing"), /metric/i);
  assert.match(root.text('[data-trace-summary="operation"]'), /metric.*not recorded|missing/i);
  const raw = root.querySelector("#trace-raw").textContent;
  assert.equal(JSON.parse(raw).events[0].query, `<script>alert("raw")</script>`);
  assert.match(raw, /<script>/);
  assert.equal(root.querySelector("#trace-raw").innerHTML, "");

  root.dispatch("click", root.querySelector('[data-trace-tab="timing"]'));
  assert.equal(root.querySelector('[data-trace-panel="timing"]').hidden, false);
  assert.equal(root.querySelector('[data-trace-tab="timing"]').getAttribute("aria-selected"), "true");
  const keydown = root.dispatch("keydown", root.querySelector('[data-trace-tab="timing"]'), { key: "End" });
  assert.equal(keydown.defaultPrevented, true);
  assert.equal(root.querySelector('[data-trace-tab="raw"]').getAttribute("aria-selected"), "true");
  assert.equal(root.querySelector('[data-trace-tab="raw"]').getAttribute("tabindex"), "0");
  assert.equal(root.querySelector('[data-trace-panel="timing"]').hidden, true);
  assert.equal(root.querySelector('[data-trace-panel="raw"]').hidden, false);
  assert.equal(fixture.document.activeElement, root.querySelector('[data-trace-tab="raw"]'));

  const arrowLeft = root.dispatch("keydown", root.querySelector('[data-trace-tab="raw"]'), { key: "ArrowLeft" });
  assert.equal(arrowLeft.defaultPrevented, true);
  assert.equal(root.querySelector('[data-trace-tab="timing"]').getAttribute("aria-selected"), "true");
  assert.equal(fixture.document.activeElement, root.querySelector('[data-trace-tab="timing"]'));
  const home = root.dispatch("keydown", root.querySelector('[data-trace-tab="timing"]'), { key: "Home" });
  assert.equal(home.defaultPrevented, true);
  assert.equal(root.querySelector('[data-trace-tab="summary"]').getAttribute("aria-selected"), "true");
  assert.equal(root.querySelector('[data-trace-panel="raw"]').hidden, true);
  const wrappedLeft = root.dispatch("keydown", root.querySelector('[data-trace-tab="summary"]'), { key: "ArrowLeft" });
  assert.equal(wrappedLeft.defaultPrevented, true);
  assert.equal(root.querySelector('[data-trace-tab="raw"]').getAttribute("aria-selected"), "true");
  assert.equal(fixture.document.activeElement, root.querySelector('[data-trace-tab="raw"]'));

  await view.copyRaw();
  assert.equal(copied.length, 1);
  assert.match(copied[0], /<script>/);
  assert.match(root.text("#trace-copy-status"), /copied/i);
  failCopy = true;
  await view.copyRaw();
  assert.match(root.text("#trace-copy-status"), /Copy failed.*permission denied/i);

  await view.selectTrace("trace-no-events", { context: context(), updateHash: false });
  assert.match(root.text("#trace-missing"), /events/i);
});

test("hash restoration loads only the selected detail; scopechange never starts business work", async (t) => {
  const calls = [];
  const fixture = createFixture(t, {
    initialHash: "#/traces/trace_hash",
    api: async (path) => {
      calls.push(path);
      return detailFixture("trace_hash");
    },
  });
  await flush();

  assert.equal(calls.length, 1);
  assert.match(calls[0], /trace_hash\/detail\?kb_id=kb_1/);
  assert.equal(fixture.window.location.hash, "#/traces/trace_hash");
  assert.match(fixture.root.querySelector("#trace-raw").textContent, /trace_hash/);

  fixture.guard.invalidate();
  const changed = {
    scope: { tenant: "tenant_b", kb: "kb_2", user: "user_2", window: "7d" },
    ...fixture.guard.snapshot(),
  };
  fixture.window.dispatchEvent(new CustomEvent("workbuddy:scopechange", { detail: changed }));
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(fixture.window.location.hash, "#/traces");
  assert.equal(fixture.root.querySelectorAll("[data-trace-select]").length, 0);
  assert.equal(fixture.root.querySelector("#trace-raw").textContent, "");

  fixture.window.dispatchEvent(new CustomEvent("workbuddy:refresh", {
    detail: { view: "traces", ...changed },
  }));
  await flush();
  assert.equal(calls.length, 2);
  assert.match(calls[1], /\/v1\/ops\/trace-list\?/);
});

test("only active Traces refresh loads data and same-scope refresh retains selection", async (t) => {
  const calls = [];
  const fixture = createFixture(t, {
    api: async (path) => {
      calls.push(path);
      return path.includes("/detail") ? detailFixture("trace-a") : { items: [traceRow("trace-a")], next_cursor: null };
    },
  });
  const { view, window, context } = fixture;
  await view.loadTraces(context());
  await view.selectTrace("trace-a", { context: context(), updateHash: true });
  assert.equal(calls.length, 2);

  window.dispatchEvent(new CustomEvent("workbuddy:refresh", {
    detail: { view: "lab", ...context() },
  }));
  await flush();
  assert.equal(calls.length, 2);

  window.dispatchEvent(new CustomEvent("workbuddy:refresh", {
    detail: { view: "traces", ...context() },
  }));
  await flush();
  assert.equal(calls.length, 4);
  assert.equal(window.location.hash, "#/traces/trace-a");
  assert.equal(calls.filter((path) => path.includes("/detail")).length, 2);
});

test("inactive and destroyed views abort slow work, ignore late responses, and reject reentry", async (t) => {
  const calls = [];
  const fixture = createFixture(t, {
    api: (_path, options) => {
      const pending = deferred();
      calls.push({ ...pending, options });
      const abort = () => pending.reject(new DOMException("Aborted", "AbortError"));
      if (options.signal.aborted) abort();
      else options.signal.addEventListener("abort", abort, { once: true });
      return pending.promise;
    },
  });
  const { view, root, context } = fixture;
  const parentSignal = context().signal;
  const first = view.loadTraces(context());
  assert.equal(calls.length, 1);
  view.setActive(false);
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(parentSignal.aborted, false);
  await first;

  view.setActive(true);
  const second = view.loadTraces(context());
  assert.equal(calls.length, 2);
  view.destroy();
  assert.equal(calls[1].options.signal.aborted, true);
  await second;
  const status = root.text("#trace-list-status");
  const raw = root.text("#trace-raw");

  await Promise.all([
    view.loadTraces(context()),
    view.loadMore(context()),
    view.selectTrace("after-destroy", { context: context() }),
    view.copyRaw(),
  ]);
  view.setActive(true);
  root.dispatch("submit", root.querySelector("#trace-filter-form"));
  root.dispatch("click", root.querySelector('[data-trace-tab="raw"]'));
  assert.equal(calls.length, 2);
  assert.equal(root.text("#trace-list-status"), status);
  assert.equal(root.text("#trace-raw"), raw);
  assert.equal(root.listenerCount("submit"), 0);
  assert.equal(root.listenerCount("click"), 0);
  assert.equal(root.listenerCount("keydown"), 0);
  assert.equal(fixture.mediaListeners.length, 0);
});

test("createTraces without a root is a safe terminal no-op", async () => {
  const view = createTraces({ root: null });
  view.setActive(true);
  view.clear();
  view.destroy();
  assert.equal(await view.loadTraces({}), undefined);
  assert.equal(await view.loadMore({}), undefined);
  assert.equal(await view.selectTrace("trace"), undefined);
  assert.equal(await view.copyRaw(), false);
});
