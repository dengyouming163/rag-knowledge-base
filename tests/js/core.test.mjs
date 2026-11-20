import test from "node:test";
import assert from "node:assert/strict";

import { escapeHtml, formatMetric, ScopeGuard } from "../../app/static/js/core.mjs";
import { ApiError, apiJson, scopeHeaders } from "../../app/static/js/api.mjs";
import { viewFromHash } from "../../app/static/js/app.mjs";


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
  assert.equal(request.options.headers["X-Tenant-Id"], "tenant_a");
  assert.equal(request.options.headers["X-User-Id"], "user_1");
  assert.equal(request.options.headers["Content-Type"], "application/json");
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

  assert.equal(request.options.body, form);
  assert.equal(Object.hasOwn(request.options.headers, "Content-Type"), false);
  assert.equal(request.options.headers["X-Tenant-Id"], "tenant_a");
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
