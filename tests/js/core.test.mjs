import test from "node:test";
import assert from "node:assert/strict";

import { escapeHtml, formatMetric, ScopeGuard } from "../../app/static/js/core.mjs";
import { ApiError, apiJson, scopeHeaders } from "../../app/static/js/api.mjs";
import { viewFromHash } from "../../app/static/js/app.mjs";
import { setLocale } from "../../app/static/js/i18n/index.mjs";

setLocale("en", { persist: false });

test("escapeHtml escapes markup, quotes, ampersands, and apostrophes", () => {
  assert.equal(
    escapeHtml(`<img src=x onerror="x">`),
    "&lt;img src=x onerror=&quot;x&quot;&gt;",
  );
  assert.equal(escapeHtml("A&B 'ready'"), "A&amp;B &#39;ready&#39;");
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(0), "0");
});

test("formatMetric formats finite numbers with units and precision", () => {
  assert.equal(formatMetric(12), "12.0");
  assert.equal(formatMetric(12.345, "ms"), "12.3 ms");
  assert.equal(formatMetric(12.345, "ms", 2), "12.35 ms");
  assert.equal(formatMetric("8.25", "", 0), "8");
  assert.equal(formatMetric(0, "ms"), "0.0 ms");
  assert.equal(formatMetric(-2.35, "s", 1), "-2.4 s");
});

test("formatMetric applies the same explicit unit semantics to percentages", () => {
  assert.equal(formatMetric(12.34, "%"), "12.3 %");
  assert.equal(formatMetric(0, "%", 0), "0 %");
  assert.equal(formatMetric(99.99, "%", 1), "100.0 %");
});

test("formatMetric renders missing and non-finite values as an em dash", () => {
  for (const value of [null, undefined, Number.NaN, Infinity, -Infinity, "not-a-number"]) {
    assert.equal(formatMetric(value, "ms"), "—");
  }
  assert.equal(formatMetric(null), "—");
  assert.equal(formatMetric(undefined, "%", 0), "—");
});

test("formatMetric rejects blank strings and booleans without rejecting numeric strings", () => {
  for (const value of ["", "   ", "\t\n", true, false]) {
    assert.equal(formatMetric(value, "ms"), "—");
  }
  assert.equal(formatMetric(" 8.25 ", "ms"), "8.3 ms");
});

test("ScopeGuard snapshots share the current epoch and signal", () => {
  const guard = new ScopeGuard();
  const first = guard.snapshot();
  const second = guard.snapshot();

  assert.equal(first.epoch, 0);
  assert.equal(second.epoch, 0);
  assert.equal(first.signal, second.signal);
  assert.equal(first.signal.aborted, false);
  assert.equal(guard.isCurrent(first), true);
});

test("ScopeGuard invalidation aborts and expires previous snapshots", () => {
  const guard = new ScopeGuard();
  const stale = guard.snapshot();
  guard.invalidate();
  const current = guard.snapshot();

  assert.equal(stale.signal.aborted, true);
  assert.equal(current.signal.aborted, false);
  assert.notEqual(current.signal, stale.signal);
  assert.equal(current.epoch, 1);
  assert.equal(guard.isCurrent(stale), false);
  assert.equal(guard.isCurrent(current), true);

  guard.invalidate();
  assert.equal(current.signal.aborted, true);
  assert.equal(guard.isCurrent(current), false);
  assert.equal(guard.snapshot().epoch, 2);
});

test("ScopeGuard exposes a coherent active snapshot during synchronous abort reentry", () => {
  const guard = new ScopeGuard();
  const stale = guard.snapshot();
  let reentrant;
  stale.signal.addEventListener("abort", () => {
    reentrant = guard.snapshot();
  }, { once: true });

  guard.invalidate();

  assert.equal(reentrant.epoch, 1);
  assert.equal(reentrant.signal.aborted, false);
  assert.notEqual(reentrant.signal, stale.signal);
  assert.equal(reentrant.signal, guard.snapshot().signal);
});

test("scopeHeaders keeps tenant identity in headers and supports JSON", () => {
  const scope = { tenant: "tenant_a", kb: "kb_1", user: "user_1", window: "24h" };
  assert.deepEqual(scopeHeaders(scope), {
    "X-Tenant-Id": "tenant_a",
    "X-User-Id": "user_1",
  });
  assert.deepEqual(scopeHeaders(scope, { json: true }), {
    "X-Tenant-Id": "tenant_a",
    "X-User-Id": "user_1",
    "Content-Type": "application/json",
  });
  assert.deepEqual(scopeHeaders({ tenant: "tenant_a" }), { "X-Tenant-Id": "tenant_a" });
});

test("apiJson stringifies JSON bodies without injecting tenant data", async (t) => {
  let request;
  const signal = new AbortController().signal;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });

  const result = await apiJson("/v1/search", {
    scope: { tenant: "tenant_a", kb: "kb_1", user: "user_1" },
    signal,
    method: "POST",
    body: { query: "refund" },
  });

  assert.deepEqual(result, { ok: true });
  assert.equal(request.path, "/v1/search");
  assert.equal(request.options.signal, signal);
  assert.equal(request.options.body, JSON.stringify({ query: "refund" }));
  assert.deepEqual(JSON.parse(request.options.body), { query: "refund" });
  const requestHeaders = new Headers(request.options.headers);
  assert.equal(requestHeaders.get("x-tenant-id"), "tenant_a");
  assert.equal(requestHeaders.get("x-user-id"), "user_1");
  assert.equal(requestHeaders.get("content-type"), "application/json");
});

test("apiJson preserves FormData and lets the browser set its content type", async (t) => {
  const form = new FormData();
  form.append("file", new Blob(["hello"]), "hello.txt");
  let request;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ job_id: "job_1" }), {
      headers: { "Content-Type": "application/json" },
    });
  });

  await apiJson("/v1/documents", {
    scope: { tenant: "tenant_a" },
    method: "POST",
    body: form,
  });

  const requestHeaders = new Headers(request.options.headers);
  assert.equal(request.options.body, form);
  assert.equal(requestHeaders.has("content-type"), false);
  assert.equal(requestHeaders.get("x-tenant-id"), "tenant_a");
});

test("apiJson preserves Headers instances and case-insensitive explicit overrides", async (t) => {
  let request;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ ok: true }));
  });
  const suppliedHeaders = new Headers({
    "x-tenant-id": "tenant_override",
    "X-User-Id": "user_override",
    "CONTENT-TYPE": "application/vnd.ops+json",
    "X-Custom-Header": "preserved",
  });

  await apiJson("/v1/search", {
    scope: { tenant: "tenant_a", user: "user_1" },
    method: "POST",
    body: { query: "refund" },
    headers: suppliedHeaders,
  });

  const normalized = new Headers(request.options.headers);
  assert.equal(normalized.get("x-tenant-id"), "tenant_override");
  assert.equal(normalized.get("x-user-id"), "user_override");
  assert.equal(normalized.get("content-type"), "application/vnd.ops+json");
  assert.equal(normalized.get("x-custom-header"), "preserved");
  assert.ok(request.options.headers instanceof Headers);
});

test("apiJson deduplicates mixed-case plain object header variants", async (t) => {
  let request;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ ok: true }));
  });

  await apiJson("/v1/search", {
    scope: { tenant: "tenant_a" },
    method: "POST",
    body: { query: "refund" },
    headers: {
      "x-tenant-id": "tenant_override",
      "content-type": "application/problem+json",
      "X-CUSTOM-HEADER": "preserved",
    },
  });

  const normalized = request.options.headers;
  assert.ok(normalized instanceof Headers);
  assert.equal(normalized.get("x-tenant-id"), "tenant_override");
  assert.equal(normalized.get("content-type"), "application/problem+json");
  assert.equal(normalized.get("x-custom-header"), "preserved");
  assert.equal([...normalized.keys()].filter((key) => key === "x-tenant-id").length, 1);
  assert.equal([...normalized.keys()].filter((key) => key === "content-type").length, 1);
});

test("apiJson deletes FormData content type after normalizing supplied Headers", async (t) => {
  const form = new FormData();
  form.append("file", new Blob(["hello"]), "hello.txt");
  let request;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ job_id: "job_1" }));
  });

  await apiJson("/v1/documents", {
    scope: { tenant: "tenant_a" },
    method: "POST",
    body: form,
    headers: new Headers({
      "Content-Type": "text/plain",
      "x-tenant-id": "tenant_override",
    }),
  });

  const normalized = request.options.headers;
  assert.ok(normalized instanceof Headers);
  assert.equal(normalized.has("content-type"), false);
  assert.equal(normalized.get("x-tenant-id"), "tenant_override");
  assert.equal([...normalized.keys()].filter((key) => key === "x-tenant-id").length, 1);
});

test("apiError preserves HTTP status and structured error details", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(
    JSON.stringify({ detail: "scope is required" }),
    { status: 422, headers: { "Content-Type": "application/json" } },
  ));

  await assert.rejects(apiJson("/v1/search"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 422);
    assert.deepEqual(error.body, { detail: "scope is required" });
    assert.match(error.message, /422/);
    assert.match(error.message, /scope is required/);
    return true;
  });
});

test("apiJson reports malformed successful JSON with response context", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("not-json", { status: 200 }));

  await assert.rejects(apiJson("/v1/search"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 200);
    assert.equal(error.body, "not-json");
    assert.match(error.message, /JSON/);
    return true;
  });
});

test("apiJson returns null for empty successful responses", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  assert.equal(await apiJson("/v1/resource"), null);
});

test("apiJson propagates abort errors without wrapping them", async (t) => {
  const abortError = new DOMException("The operation was aborted", "AbortError");
  t.mock.method(globalThis, "fetch", async () => { throw abortError; });

  await assert.rejects(apiJson("/v1/search"), (error) => {
    assert.equal(error, abortError);
    assert.equal(error instanceof ApiError, false);
    return true;
  });
});

test("viewFromHash restores known modules and falls back to overview", () => {
  assert.equal(viewFromHash("#/traces"), "traces");
  assert.equal(viewFromHash("#lab"), "lab");
  assert.equal(viewFromHash("#/not-a-module"), "overview");
  assert.equal(viewFromHash(""), "overview");
  assert.equal(viewFromHash(undefined), "overview");
});

let workbenchImportSequence = 0;

async function loadWorkbench(t) {
  const controls = Object.fromEntries(
    ["scope-tenant", "scope-kb", "scope-user", "scope-window"].map((id, index) => {
      const control = new EventTarget();
      control.value = ["tenant_a", "kb_1", "user_1", "24h"][index];
      return [id, control];
    }),
  );
  const form = new EventTarget();
  const status = { textContent: "Ready" };
  const elements = new Map([
    ...Object.entries(controls),
    ["scope-form", form],
    ["app-status", status],
  ]);
  const fakeDocument = {
    readyState: "complete",
    getElementById(id) { return elements.get(id) ?? null; },
    querySelectorAll() { return []; },
  };
  const fakeWindow = new EventTarget();
  fakeWindow.location = { hash: "" };
  fakeWindow.history = {
    replaceState(_state, _unused, hash) { fakeWindow.location.hash = hash; },
  };

  globalThis.document = fakeDocument;
  globalThis.window = fakeWindow;
  t.after(() => {
    delete globalThis.document;
    delete globalThis.window;
  });

  workbenchImportSequence += 1;
  await import(`../../app/static/js/app.mjs?workbench-test=${workbenchImportSequence}`);
  return { controls, form, window: fakeWindow };
}

function submit(form) {
  form.dispatchEvent(new Event("submit", { cancelable: true }));
}

test("manual refresh invalidates the previous epoch without emitting scopechange", async (t) => {
  const { form, window } = await loadWorkbench(t);
  const refreshes = [];
  const scopeChanges = [];
  window.addEventListener("workbuddy:refresh", (event) => refreshes.push(event.detail));
  window.addEventListener("workbuddy:scopechange", (event) => scopeChanges.push(event.detail));

  submit(form);
  submit(form);

  assert.equal(refreshes.length, 2);
  assert.equal(scopeChanges.length, 0);
  assert.equal(refreshes[1].epoch, refreshes[0].epoch + 1);
  assert.notEqual(refreshes[1].signal, refreshes[0].signal);
  assert.equal(refreshes[0].signal.aborted, true);
  assert.equal(refreshes[1].signal.aborted, false);
});

test("changed submit emits one scopechange and one refresh with the same new snapshot", async (t) => {
  const { controls, form, window } = await loadWorkbench(t);
  const refreshes = [];
  const scopeChanges = [];
  window.addEventListener("workbuddy:refresh", (event) => refreshes.push(event.detail));
  window.addEventListener("workbuddy:scopechange", (event) => scopeChanges.push(event.detail));

  controls["scope-tenant"].value = "tenant_b";
  submit(form);

  assert.equal(scopeChanges.length, 1);
  assert.equal(refreshes.length, 1);
  assert.equal(scopeChanges[0].scope.tenant, "tenant_b");
  assert.equal(refreshes[0].scope.tenant, "tenant_b");
  assert.equal(refreshes[0].epoch, scopeChanges[0].epoch);
  assert.equal(refreshes[0].signal, scopeChanges[0].signal);
  assert.equal(refreshes[0].signal.aborted, false);
});

test("change followed by submit emits exactly one business load through refresh", async (t) => {
  const { controls, form, window } = await loadWorkbench(t);
  const businessLoads = [];
  const scopeChanges = [];
  window.addEventListener("workbuddy:refresh", (event) => businessLoads.push(event.detail));
  window.addEventListener("workbuddy:scopechange", (event) => scopeChanges.push(event.detail));

  controls["scope-tenant"].value = "tenant_b";
  controls["scope-tenant"].dispatchEvent(new Event("change"));
  submit(form);

  assert.equal(scopeChanges.length, 1);
  assert.equal(businessLoads.length, 1);
  assert.equal(scopeChanges[0].scope.tenant, "tenant_b");
  assert.equal(businessLoads[0].scope.tenant, "tenant_b");
  assert.equal(businessLoads[0].epoch, scopeChanges[0].epoch + 1);
  assert.notEqual(businessLoads[0].signal, scopeChanges[0].signal);
  assert.equal(scopeChanges[0].signal.aborted, true);
  assert.equal(businessLoads[0].signal.aborted, false);
});
