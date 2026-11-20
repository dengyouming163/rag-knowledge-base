import test from "node:test";
import assert from "node:assert/strict";

import { ScopeGuard } from "../../app/static/js/core.mjs";
import {
  costStatus,
  createOverview,
  renderTrend,
} from "../../app/static/js/views/overview.mjs";


class FakeElement {
  constructor() {
    this.textContent = "";
    this.innerHTML = "";
    this.attributes = {};
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }
}

class FakeRoot {
  elements = new Map();

  querySelector(selector) {
    if (!this.elements.has(selector)) this.elements.set(selector, new FakeElement());
    return this.elements.get(selector);
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

function summaryFixture() {
  return {
    observability: {
      window: "24h",
      http: {
        requests: 120,
        avg_qpm: 2,
        "4xx_rate": 0.01,
        "5xx_rate": 0.005,
      },
      rag: {
        operations: 60,
        zero_hit: { rate: 0.1, denominator: 50 },
        refusal: { rate: 0.05, denominator: 40 },
        citation: { rate: 0.95, denominator: 40 },
        latency_ms: {
          e2e: { p95: 900, samples: 12 },
          retrieve: { p95: 180, samples: 45 },
          llm: { p95: 650, samples: 40 },
        },
      },
      tokens: {
        total: 12345,
        estimated_cost_usd: 0.25,
        pricing_configured: true,
        pricing_complete: false,
      },
    },
    queue: { queued: 3, running: 2, failed: 1, total: 6 },
    workers: [{ status: "online" }, { status: "offline" }],
  };
}

function timeseriesFixture() {
  return {
    window: "24h",
    series: [
      {
        ts: "2026-11-20T00:00:00+00:00",
        requests: 10,
        e2e_p95_ms: 800,
        retrieval_p95_ms: 150,
        llm_p95_ms: 500,
      },
      {
        ts: "2026-11-20T01:00:00+00:00",
        requests: 20,
        e2e_p95_ms: 900,
        retrieval_p95_ms: 180,
        llm_p95_ms: 650,
      },
    ],
  };
}

function createFixture(t, overrides = {}) {
  const root = new FakeRoot();
  const guard = new ScopeGuard();
  const document = new EventTarget();
  document.visibilityState = "visible";
  const window = new EventTarget();
  const timers = {
    nextId: 0,
    callbacks: new Map(),
    intervals: [],
    setInterval(callback, milliseconds) {
      const id = ++this.nextId;
      this.callbacks.set(id, callback);
      this.intervals.push(milliseconds);
      return id;
    },
    clearInterval(id) {
      this.callbacks.delete(id);
    },
  };
  const api = overrides.api ?? (async (path) => (
    path.includes("/timeseries") ? timeseriesFixture() : summaryFixture()
  ));
  const view = createOverview({
    root,
    guard,
    document,
    window,
    api,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    now: () => new Date("2026-11-20T12:00:00.000Z"),
  });
  view.setActive(true);
  t.after(() => view.destroy());

  const scope = { tenant: "tenant_a", kb: "kb_1", user: "user_1", window: "24h" };
  const snapshot = () => ({ scope: { ...scope }, ...guard.snapshot() });
  return { root, guard, document, window, timers, view, snapshot };
}

function metric(root, name, field = "value") {
  return root.text(`[data-overview-${field}="${name}"]`);
}


test("renderTrend shows an empty state instead of inventing a line", () => {
  for (const series of [[], [{ ts: "now", requests: null }], [{ ts: "now", requests: "bad" }]]) {
    const output = renderTrend(series, { key: "requests" });
    assert.match(output, /No trend data/);
    assert.doesNotMatch(output, /<svg|<path|<circle/);
  }
});

test("renderTrend emits numeric-only geometry and escapes all source text", () => {
  const hostileTimestamp = `2026-11-20"><img src=x onerror="alert(1)">`;
  const output = renderTrend([
    { ts: hostileTimestamp, requests: 10 },
    { ts: "2026-11-21T00:00:00Z", requests: 20 },
  ], { key: "requests", label: `Requests <script>alert(1)</script>`, unit: "req/min" });

  assert.match(output, /<svg/);
  assert.doesNotMatch(output, /<script|<img/);
  assert.equal(output.includes(hostileTimestamp), false);
  assert.match(output, /&lt;script&gt;/);
  assert.match(output, /&lt;img/);
  const path = output.match(/<path[^>]*\bd="([^"]+)"/)?.[1];
  assert.ok(path);
  assert.match(path, /^M [\d.]+ [\d.]+ L [\d.]+ [\d.]+$/);
  assert.doesNotMatch(path, /NaN|Infinity|undefined|null/);
});

test("costStatus distinguishes unconfigured, partial, and complete estimates", () => {
  assert.equal(costStatus({ pricing_configured: false, pricing_complete: false }), "Not configured");
  assert.equal(costStatus({ pricing_configured: true, pricing_complete: false }), "Partial estimate");
  assert.equal(costStatus({ pricing_configured: true, pricing_complete: true }), "Complete estimate");
});

test("loadOverview starts summary and trends in parallel and isolates panel failure", async (t) => {
  const summary = deferred();
  const trends = deferred();
  const calls = [];
  const fixture = createFixture(t, {
    api: (path, options) => {
      calls.push({ path, options });
      return path.includes("/timeseries") ? trends.promise : summary.promise;
    },
  });
  const context = fixture.snapshot();

  const loading = fixture.view.loadOverview(context);
  assert.equal(calls.length, 2);
  assert.ok(calls[0].path.includes("/v1/ops/summary?"));
  assert.ok(calls[1].path.includes("/v1/ops/metrics/timeseries?"));
  for (const call of calls) {
    const url = new URL(call.path, "http://workbuddy.test");
    assert.equal(url.searchParams.get("kb_id"), "kb_1");
    assert.equal(url.searchParams.get("window"), "24h");
    assert.equal(url.searchParams.has("tenant"), false);
    assert.equal(url.searchParams.has("tenant_id"), false);
    assert.equal(call.options.scope.tenant, "tenant_a");
    assert.equal(call.options.signal, context.signal);
  }

  summary.reject(new Error("summary down"));
  trends.resolve(timeseriesFixture());
  await loading;

  assert.match(fixture.root.text("[data-overview-summary-status]"), /summary down/);
  assert.equal(metric(fixture.root, "requests"), "—");
  assert.match(fixture.root.text("[data-overview-trends-status]"), /Updated/);
  assert.match(fixture.root.querySelector('[data-overview-trend-svg="requests"]').innerHTML, /<svg/);
});

test("summary cards show values, units, window, samples, and low-sample guidance", async (t) => {
  const { root, view, snapshot } = createFixture(t);

  await view.loadOverview(snapshot());

  assert.equal(metric(root, "requests"), "120");
  assert.equal(metric(root, "requests", "unit"), "req");
  assert.equal(metric(root, "avg_qpm"), "2.00");
  assert.equal(metric(root, "client_errors"), "1.0");
  assert.equal(metric(root, "client_errors", "unit"), "%");
  assert.equal(metric(root, "e2e_p95"), "900.0");
  assert.equal(metric(root, "retrieval_p95"), "180.0");
  assert.equal(metric(root, "llm_p95"), "650.0");
  assert.equal(metric(root, "zero_hit"), "10.0");
  assert.equal(metric(root, "refusal"), "5.0");
  assert.equal(metric(root, "citation"), "95.0");
  assert.equal(metric(root, "tokens"), "12345");
  assert.equal(metric(root, "cost"), "0.250000");
  assert.equal(metric(root, "queue"), "3");
  assert.equal(metric(root, "workers"), "2");
  for (const name of [
    "requests", "avg_qpm", "client_errors", "server_errors", "e2e_p95",
    "retrieval_p95", "llm_p95", "zero_hit", "refusal", "citation",
    "tokens", "cost", "queue", "workers",
  ]) {
    assert.equal(metric(root, name, "window"), "24h", name);
    assert.notEqual(metric(root, name, "samples"), "", name);
  }
  assert.equal(metric(root, "e2e_p95", "samples"), "12");
  assert.match(metric(root, "e2e_p95", "note"), /Low sample/);
  assert.match(metric(root, "cost", "note"), /Partial estimate/);
});

test("failed refresh retains same-scope data with stale-at but never crosses scope", async (t) => {
  let summaryCalls = 0;
  const fixture = createFixture(t, {
    api: async (path) => {
      if (path.includes("/timeseries")) return timeseriesFixture();
      summaryCalls += 1;
      if (summaryCalls > 1) throw new Error("summary unavailable");
      return summaryFixture();
    },
  });
  const first = fixture.snapshot();

  await fixture.view.loadOverview(first);
  await fixture.view.loadOverview(first);

  const status = fixture.root.querySelector("[data-overview-summary-status]");
  assert.equal(metric(fixture.root, "requests"), "120");
  assert.match(status.textContent, /stale-at/i);
  assert.match(status.textContent, /Last success/);
  assert.equal(status.attributes["data-last-success-at"], "2026-11-20T12:00:00.000Z");
  assert.equal(status.attributes["data-stale-at"], "2026-11-20T12:00:00.000Z");

  fixture.view.clear();
  fixture.guard.invalidate();
  const changed = fixture.snapshot();
  changed.scope = { ...changed.scope, tenant: "tenant_b" };
  await fixture.view.loadOverview(changed);

  assert.equal(metric(fixture.root, "requests"), "—");
  assert.doesNotMatch(status.textContent, /stale-at|Last success/);
  assert.equal(status.attributes["data-last-success-at"], undefined);
});

test("expired, inactive, and hidden responses are discarded", async (t) => {
  const scenarios = [
    ["expired", (fixture) => fixture.guard.invalidate()],
    ["inactive", (fixture) => fixture.view.setActive(false)],
    ["hidden", (fixture) => { fixture.document.visibilityState = "hidden"; }],
  ];

  for (const [name, invalidate] of scenarios) {
    await t.test(name, async (t) => {
      const summary = deferred();
      const trends = deferred();
      const fixture = createFixture(t, {
        api: (path) => path.includes("/timeseries") ? trends.promise : summary.promise,
      });
      const loading = fixture.view.loadOverview(fixture.snapshot());
      invalidate(fixture);
      summary.resolve(summaryFixture());
      trends.resolve(timeseriesFixture());
      await loading;

      assert.equal(metric(fixture.root, "requests"), "—");
      assert.doesNotMatch(fixture.root.text("[data-overview-summary-status]"), /Updated|stale-at/);
      assert.doesNotMatch(
        fixture.root.querySelector('[data-overview-trend-svg="requests"]').innerHTML,
        /<svg/,
      );
    });
  }
});

test("the 15-second timer is singular, visibility-aware, and cleaned up on exit", async (t) => {
  const fixture = createFixture(t);
  assert.equal(fixture.timers.callbacks.size, 1);
  assert.deepEqual(fixture.timers.intervals, [15000]);

  fixture.view.setActive(true);
  assert.equal(fixture.timers.callbacks.size, 1);
  await fixture.view.loadOverview(fixture.snapshot());
  const callback = [...fixture.timers.callbacks.values()][0];
  await callback();

  fixture.document.visibilityState = "hidden";
  fixture.document.dispatchEvent(new Event("visibilitychange"));
  assert.equal(fixture.timers.callbacks.size, 0);
  await callback();
  fixture.document.visibilityState = "visible";
  fixture.document.dispatchEvent(new Event("visibilitychange"));
  assert.equal(fixture.timers.callbacks.size, 1);

  fixture.view.setActive(false);
  assert.equal(fixture.timers.callbacks.size, 0);
  fixture.view.setActive(true);
  fixture.view.setActive(true);
  assert.equal(fixture.timers.callbacks.size, 1);
  fixture.view.destroy();
  assert.equal(fixture.timers.callbacks.size, 0);
});

test("only refresh for Overview starts business loads; scopechange is notification-only", async (t) => {
  let calls = 0;
  const fixture = createFixture(t, {
    api: async (path) => {
      calls += 1;
      return path.includes("/timeseries") ? timeseriesFixture() : summaryFixture();
    },
  });
  const detail = { view: "overview", ...fixture.snapshot() };

  fixture.window.dispatchEvent(new CustomEvent("workbuddy:scopechange", { detail }));
  fixture.window.dispatchEvent(new CustomEvent("workbuddy:refresh", { detail: { ...detail, view: "lab" } }));
  assert.equal(calls, 0);
  fixture.window.dispatchEvent(new CustomEvent("workbuddy:refresh", { detail }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 2);
});
