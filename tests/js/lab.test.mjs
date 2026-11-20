import test from "node:test";
import assert from "node:assert/strict";

import { ScopeGuard } from "../../app/static/js/core.mjs";
import { createLab } from "../../app/static/js/views/lab.mjs";


class FakeElement extends EventTarget {
  constructor() {
    super();
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.attributes = {};
    this.classList = {
      toggle(name, enabled) {
        if (enabled) this[name] = true;
        else delete this[name];
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
}

class FakeRoot {
  elements = new Map();
  listeners = new Map();

  querySelector(selector) {
    if (!this.elements.has(selector)) this.elements.set(selector, new FakeElement());
    return this.elements.get(selector);
  }

  querySelectorAll(selector) {
    return this.listeners.get(selector) ?? [];
  }

  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }

  dispatch(type) {
    for (const callback of this.listeners.get(type) ?? []) {
      callback({ preventDefault() {}, target: { dataset: { labTab: "chat" } } });
    }
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

function abortError() {
  return new DOMException("Aborted", "AbortError");
}

function linkedSignal(parent) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (parent.aborted) abort();
  else parent.addEventListener("abort", abort, { once: true });
  return controller.signal;
}

function response(body, { traceId = "", stream = false } = {}) {
  return {
    ok: true,
    status: 200,
    headers: { get: (name) => name.toLowerCase() === "x-trace-id" ? traceId : null },
    json: async () => body,
    body: stream ? body : undefined,
  };
}

function streamResponse(chunks, calls) {
  let index = 0;
  const reader = {
    read() {
      if (index < chunks.length) return Promise.resolve({ done: false, value: chunks[index++] });
      const pending = deferred();
      calls.push({ type: "read", ...pending });
      return pending.promise;
    },
    cancel: async () => undefined,
    releaseLock() {},
  };
  return response({ getReader: () => reader }, { stream: true });
}

function sseStream(frames, { traceId = "" } = {}) {
  const bytes = frames.map((frame) => new TextEncoder().encode(frame));
  return response({
    getReader() {
      let index = 0;
      return {
        read: async () => index < bytes.length
          ? { done: false, value: bytes[index++] }
          : { done: true, value: undefined },
        cancel: async () => undefined,
        releaseLock() {},
      };
    },
  }, { traceId, stream: true });
}

function createFixture(t, { api, fetchImpl, initialTab = "search", ...options } = {}) {
  const root = new FakeRoot();
  for (const tab of ["search", "chat", "ingest"]) {
    const button = root.querySelector(`[data-lab-tab="${tab}"]`);
    button.dataset.labTab = tab;
    const panel = root.querySelector(`[data-lab-panel="${tab}"]`);
    panel.dataset.labPanel = tab;
    panel.hidden = tab !== initialTab;
  }
  const guard = new ScopeGuard();
  const window = new EventTarget();
  const document = new EventTarget();
  document.visibilityState = "visible";
  const timers = {
    nextId: 0,
    callbacks: new Map(),
    setTimeout(callback, milliseconds) {
      const id = ++this.nextId;
      this.callbacks.set(id, { callback, milliseconds });
      return id;
    },
    clearTimeout(id) {
      this.callbacks.delete(id);
    },
    async tick() {
      const entry = this.callbacks.entries().next().value;
      assert.ok(entry, "expected a pending poll timer");
      const [id, { callback }] = entry;
      this.callbacks.delete(id);
      await callback();
    },
  };
  const lab = createLab({
    root,
    guard,
    window,
    document,
    api: api ?? (async () => ({})),
    fetchImpl: fetchImpl ?? (async () => { throw new Error("unexpected fetch"); }),
    setTimeout: timers.setTimeout.bind(timers),
    clearTimeout: timers.clearTimeout.bind(timers),
    ...options,
  });
  lab.setActive(true);
  t.after(() => lab.destroy());

  const scope = { tenant: "tenant_a", kb: "kb_1", user: "user_1", window: "24h" };
  const context = () => ({ scope: { ...scope }, ...guard.snapshot() });
  return { root, guard, window, document, timers, lab, context };
}

function searchHit(overrides = {}) {
  return {
    chunk_id: "chunk-1",
    doc_id: "doc-1",
    score: 0.875,
    text: "Grounded source text",
    metadata: { page: 2 },
    ...overrides,
  };
}

function chatOptions(root) {
  return {
    question: root.querySelector("#chat-question").value || "How do returns work?",
    topK: Number(root.querySelector("#chat-top-k").value || 5),
    audience: root.querySelector("#chat-audience").value || "internal",
  };
}


test("Search sends only the retrieval contract, caches hits, escapes content, and links its trace", async (t) => {
  const calls = [];
  const hit = searchHit({
    chunk_id: `chunk"><img src=x>`,
    doc_id: `doc<script>alert(1)</script>`,
    text: `<img src=x onerror="alert(1)">`,
    metadata: { note: `<script>alert(2)</script>` },
  });
  const fixture = createFixture(t, {
    api: async (path, options) => {
      calls.push({ path, options });
      return { data: { kb_id: "kb_1", mode: "hybrid", hits: [hit] }, response: { headers: new Headers({ "X-Trace-Id": "trace_search" }) } };
    },
  });
  const { root, lab, context } = fixture;
  const snapshot = context();
  const query = "  return policy  ";
  root.querySelector("#search-query").value = query;
  root.querySelector("#search-top-k").value = "7";
  root.querySelector("#search-mode").value = "hybrid";
  root.querySelector("#search-audience").value = "internal";

  await lab.search(snapshot);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/v1/kbs/kb_1/search");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(calls[0].options.body, {
    query: "return policy",
    top_k: 7,
    mode: "hybrid",
    audience: "internal",
  });
  assert.equal(calls[0].options.scope.tenant, "tenant_a");
  assert.notEqual(calls[0].options.signal, snapshot.signal);
  assert.equal(calls[0].options.signal.aborted, false);
  snapshot.signal.addEventListener("abort", () => {}, { once: true });
  const output = root.querySelector("#search-results").innerHTML;
  assert.match(output, /0\.875000/);
  assert.match(output, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(output, /&lt;script&gt;alert\(2\)&lt;\/script&gt;/);
  assert.doesNotMatch(output, /<img|<script/);
  const trace = root.querySelector("#search-trace-link");
  assert.equal(trace.hidden, false);
  assert.equal(trace.getAttribute("href"), "#/traces/trace_search");

  root.querySelector("#chat-question").value = "return policy";
  const chat = await lab.startChat(snapshot, chatOptions(root), {
    fetch: async () => sseStream([
      `event: hits\ndata: ${JSON.stringify({ chunks: [hit.chunk_id] })}\n\n`,
      `data: ${JSON.stringify({ delta: `Answer [${hit.chunk_id}]` })}\n\ndata: [DONE]\n\n`,
    ]),
  });
  const searchCallsBeforeCitation = calls.length;
  await lab.showCitation(hit.chunk_id);

  assert.equal(chat.done, true);
  assert.equal(calls.length, searchCallsBeforeCitation, "cached Search hit must avoid another request");
  assert.equal(root.querySelector("#citation-detail").hidden, false);
  assert.equal(root.querySelector("#citation-text").textContent, hit.text);
  assert.match(root.querySelector("#citation-metadata").textContent, /alert\(2\)/);
});

test("Chat sends scope headers without tenant in the body and preserves CRLF frames split across chunks", async (t) => {
  const requests = [];
  const chunks = [
    "event: hits\r\n",
    `data: ${JSON.stringify({ chunks: ["chunk-a", "chunk-a"], rewritten: "rewritten question", rerank_degraded: true, citation_ok: false, usage: { prompt_tokens: 11, completion_tokens: 7 }, timings: { retrieve: 12.5 }, trace_id: "trace_chat" })}\r`,
    "\n\r\nda",
    `ta: ${JSON.stringify({ delta: "First " })}\r\n\r\n`,
    `data: ${JSON.stringify({ delta: "second [chunk-a]" })}\n\n`,
    "data: [DO",
    "NE]\n\n",
  ];
  const fixture = createFixture(t, {
    fetchImpl: async (path, options) => {
      requests.push({ path, options });
      return sseStream(chunks);
    },
  });
  const { root, lab, context } = fixture;
  root.querySelector("#chat-question").value = "  original question  ";
  root.querySelector("#chat-top-k").value = "6";
  root.querySelector("#chat-audience").value = "internal";

  const result = await lab.startChat(context(), chatOptions(root));

  assert.equal(result.done, true);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/v1/chat/stream");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    kb_id: "kb_1",
    question: "original question",
    history: [],
    top_k: 6,
    audience: "internal",
  });
  assert.equal(requests[0].options.headers["X-Tenant-Id"], "tenant_a");
  assert.equal(requests[0].options.headers["X-User-Id"], "user_1");
  assert.equal(requests[0].options.headers.Accept, "text/event-stream");
  assert.equal(root.querySelector("#answer").textContent, "First second [chunk-a]");
  assert.match(root.querySelector("#answer").innerHTML, /data-citation="chunk-a"/);
  assert.deepEqual(result.citations, ["chunk-a"]);
  const meta = root.querySelector("#stream-meta").textContent;
  assert.match(meta, /rewritten question/);
  assert.match(meta, /11/);
  assert.match(meta, /7/);
  assert.match(meta, /12\.5/);
  assert.match(meta, /rerank/i);
  assert.match(meta, /citation/i);
  assert.match(meta, /trace_chat/);
  assert.equal(root.querySelector("#chat-trace-link").hidden, false);
  assert.equal(root.querySelector("#chat-trace-link").getAttribute("href"), "#/traces/trace_chat");
  assert.equal(root.querySelector("#chat-button").disabled, false);

  const followup = deferred();
  fixture.lab.startChat(context(), { ...chatOptions(root), question: "follow up" }, {
    fetch: (path, options) => {
      requests.push({ path, options });
      return followup.promise;
    },
  });
  await flush();
  assert.deepEqual(JSON.parse(requests[1].options.body).history, [
    { role: "user", content: "original question" },
    { role: "assistant", content: "First second [chunk-a]" },
  ]);
  followup.reject(abortError());
  await flush();
});

test("Chat surfaces escaped server errors, restores controls, and does not commit incomplete history", async (t) => {
  const fixture = createFixture(t, {
    fetchImpl: async () => sseStream([
      `data: ${JSON.stringify({ delta: "partial" })}\n\n`,
      `event: error\ndata: ${JSON.stringify({ message: `<script>alert("bad")</script>` })}\n\n`,
    ]),
  });
  const { root, lab, context } = fixture;
  root.querySelector("#chat-question").value = "question";

  const result = await lab.startChat(context(), chatOptions(root));

  assert.equal(result.done, false);
  assert.match(root.querySelector("#chat-status").textContent, /alert\(&quot;bad&quot;\)|alert\("bad"\)/);
  assert.doesNotMatch(root.querySelector("#chat-status").innerHTML, /<script/);
  assert.equal(root.querySelector("#chat-button").disabled, false);
  let nextRequest;
  const pending = deferred();
  const second = lab.startChat(context(), chatOptions(root), {
    fetch: (_path, options) => {
      nextRequest = options;
      return pending.promise;
    },
  });
  await flush();
  assert.deepEqual(JSON.parse(nextRequest.body).history, []);
  fixture.document.visibilityState = "hidden";
  fixture.document.dispatchEvent(new Event("visibilitychange"));
  pending.reject(abortError());
  await second;
});

test("Chat rejects a stream that ends before DONE and restores the submit button", async (t) => {
  const fixture = createFixture(t, {
    fetchImpl: async () => sseStream([`data: ${JSON.stringify({ delta: "unfinished" })}\n\n`]),
  });
  const { root, lab, context } = fixture;

  const result = await lab.startChat(context(), chatOptions(root));

  assert.equal(result.done, false);
  assert.match(root.querySelector("#chat-status").textContent, /\[DONE\]/);
  assert.equal(root.querySelector("#chat-button").disabled, false);
});

test("a citation cache miss searches once with the rewritten question and reports a remaining miss", async (t) => {
  const requests = [];
  const fixture = createFixture(t, {
    api: async (path, options) => {
      requests.push({ path, options });
      return { data: { hits: [] }, response: { headers: new Headers() } };
    },
    fetchImpl: async () => sseStream([
      `event: hits\ndata: ${JSON.stringify({ chunks: ["missing-chunk"], rewritten: "better query" })}\n\n`,
      `data: ${JSON.stringify({ delta: "answer" })}\n\ndata: [DONE]\n\n`,
    ]),
  });
  const { root, lab, context } = fixture;
  root.querySelector("#chat-question").value = "original";
  root.querySelector("#chat-top-k").value = "8";
  root.querySelector("#chat-audience").value = "public";
  await lab.startChat(context(), chatOptions(root));

  await Promise.all([
    lab.showCitation("missing-chunk"),
    lab.showCitation("missing-chunk"),
  ]);

  assert.equal(requests.length, 1, "an in-flight miss should be shared");
  assert.deepEqual(requests[0].options.body, {
    query: "better query",
    top_k: 8,
    mode: "hybrid",
    audience: "public",
  });
  assert.equal(root.querySelector("#citation-detail").hidden, true);
  assert.match(root.querySelector("#citation-status").textContent, /did not return missing-chunk/);
});

test("a completed citation miss can be retried", async (t) => {
  const requests = [];
  const fixture = createFixture(t, {
    api: async (_path, options) => {
      requests.push(options);
      return { hits: [] };
    },
    fetchImpl: async () => sseStream([
      `event: hits\ndata: ${JSON.stringify({ chunks: ["missing-chunk"] })}\n\n`,
      `data: ${JSON.stringify({ delta: "answer" })}\n\ndata: [DONE]\n\n`,
    ]),
  });
  const { lab, context } = fixture;
  await lab.startChat(context(), { question: "question", topK: 5, audience: "public" });

  await lab.showCitation("missing-chunk");
  await lab.showCitation("missing-chunk");

  assert.equal(requests.length, 2);
});

test("Upload sends FormData and polls real job progress until success", async (t) => {
  const calls = [];
  let pollCount = 0;
  const fixture = createFixture(t, {
    api: async (path, options = {}) => {
      calls.push({ path, options });
      if (options.method === "POST") return { job_id: 42, doc_id: "doc-42", duplicated: true };
      pollCount += 1;
      return pollCount === 1
        ? { id: 42, doc_id: "doc-42", status: "running", progress: 45, attempts: 1, source: "upload", error: "" }
        : { id: 42, doc_id: "doc-42", status: "succeeded", progress: 100, attempts: 1, source: "upload", error: "" };
    },
  });
  const { root, lab, timers, context } = fixture;
  const file = new File(["contents"], "returns.md", { type: "text/markdown" });

  const upload = lab.upload(context(), file);
  await flush();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/v1/kbs/kb_1/documents");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.body.get("file"), file);
  assert.equal(calls[0].options.scope.tenant, "tenant_a");
  assert.equal(root.querySelector("#upload-button").disabled, true);
  await timers.tick();
  assert.equal(root.querySelector("#upload-progress").value, 45);
  assert.match(root.querySelector("#upload-progress-value").textContent, /45%/);
  await timers.tick();
  await upload;

  assert.equal(pollCount, 2);
  assert.equal(calls[1].path, "/v1/jobs/42");
  assert.equal(calls[2].path, "/v1/jobs/42");
  assert.equal(root.querySelector("#upload-progress").value, 100);
  assert.match(root.querySelector("#upload-status").textContent, /succeeded/);
  assert.match(root.querySelector("#upload-status").textContent, /duplicated|重复/i);
  assert.equal(root.querySelector("#upload-button").disabled, false);
  assert.equal(timers.callbacks.size, 0);
});

test("failed upload jobs and request failures escape errors and restore controls", async (t) => {
  const scenarios = [
    {
      name: "terminal job failure",
      poll: true,
      api: async (_path, options = {}) => options.method === "POST"
        ? { job_id: 9, doc_id: "doc-9", duplicated: false }
        : { id: 9, doc_id: "doc-9", status: "failed", progress: 60, attempts: 2, source: "upload", error: `<img src=x onerror="bad()">` },
    },
    {
      name: "request failure",
      api: async () => { throw new Error(`<script>alert("upload")</script>`); },
    },
  ];

  for (const scenario of scenarios) {
    await t.test(scenario.name, async (t) => {
      const fixture = createFixture(t, { api: scenario.api });
      const { root, lab, context } = fixture;
      const pending = lab.upload(context(), new File(["x"], "bad.txt"));
      if (scenario.poll) {
        await flush();
        await fixture.timers.tick();
      }
      await pending;
      assert.equal(root.querySelector("#upload-button").disabled, false);
      assert.equal(root.querySelector("#upload-error").hidden, false);
      assert.match(root.querySelector("#upload-error").textContent, /bad\(\)|alert/);
      assert.doesNotMatch(root.querySelector("#upload-error").innerHTML, /<img|<script/);
    });
  }
});

test("scope change, hiding, inactivity, and destroy cancel work, clear answers and caches, and restore buttons", async (t) => {
  const scenarios = [
    ["scopechange", (fixture) => {
      fixture.guard.invalidate();
      fixture.window.dispatchEvent(new CustomEvent("workbuddy:scopechange", {
        detail: { scope: { tenant: "tenant_b", kb: "kb_2", user: "user_2" }, ...fixture.guard.snapshot() },
      }));
    }],
    ["hidden", (fixture) => {
      fixture.document.visibilityState = "hidden";
      fixture.document.dispatchEvent(new Event("visibilitychange"));
    }],
    ["inactive", (fixture) => fixture.lab.setActive(false)],
    ["destroy", (fixture) => fixture.lab.destroy()],
  ];

  for (const [name, action] of scenarios) {
    await t.test(name, async (t) => {
      const calls = [];
      const chatRead = deferred();
      let uploads = 0;
      const fixture = createFixture(t, {
        api: (path, options = {}) => {
          const request = deferred();
          const signal = options.signal ?? linkedSignal(fixture?.context?.().signal ?? new AbortController().signal);
          calls.push({ path, options, signal, ...request });
          const abort = () => request.reject(abortError());
          if (signal.aborted) abort();
          else signal.addEventListener("abort", abort, { once: true });
          return request.promise;
        },
        fetchImpl: async (_path, options) => {
          const signal = options.signal;
          calls.push({ path: "/v1/chat/stream", options, signal });
          return response({
            getReader: () => ({
              read: () => chatRead.promise,
              cancel: async () => undefined,
              releaseLock() {},
            }),
          }, { stream: true });
        },
      });
      const { root, lab, context } = fixture;
      root.querySelector("#search-query").value = "search question";
      root.querySelector("#chat-question").value = "question";
      const snapshot = context();
      const searching = lab.search(snapshot);
      const chatting = lab.startChat(snapshot, chatOptions(root));
      const uploading = lab.upload(snapshot, new File(["x"], "scope.md"));
      uploads += 1;
      await flush();
      assert.equal(uploads, 1);
      assert.equal(root.querySelector("#search-button").disabled, true, JSON.stringify({
        calls: calls.map((call) => ({ path: call.path, aborted: call.signal.aborted })),
        status: root.text("#search-status"),
      }));
      assert.equal(root.querySelector("#chat-button").disabled, true);
      assert.equal(root.querySelector("#upload-button").disabled, true);

      action(fixture);

      assert.ok(calls.length >= 3);
      for (const call of calls) assert.equal(call.signal.aborted, true, call.path);
      assert.equal(snapshot.signal.aborted, name === "scopechange");
      chatRead.reject(abortError());
      await Promise.allSettled([searching, chatting, uploading]);
      assert.equal(root.querySelector("#search-button").disabled, false);
      assert.equal(root.querySelector("#chat-button").disabled, false);
      assert.equal(root.querySelector("#upload-button").disabled, false);
      assert.equal(root.querySelector("#answer").textContent, "");
      assert.equal(root.querySelector("#stream-meta").textContent, "");
      assert.equal(root.querySelector("#citation-list").innerHTML, "");
      assert.equal(root.querySelector("#search-results").innerHTML, "");
      assert.equal(root.querySelector("#search-trace-link").hidden, true);
      assert.equal(root.querySelector("#chat-trace-link").hidden, true);
      assert.equal(fixture.timers.callbacks.size, 0);
    });
  }
});

test("leaving and returning preserves the scope for direct actions without auto-loading", async (t) => {
  const calls = [];
  const scope = { tenant: "tenant_a", kb: "kb_1", user: "user_1", window: "24h" };
  const fixture = createFixture(t, {
    scope,
    api: async (path, options) => {
      calls.push({ path, options });
      return { hits: [] };
    },
  });
  const { root, lab } = fixture;
  root.querySelector("#search-query").value = "manual query";

  lab.setActive(false);
  lab.setActive(true);
  assert.equal(calls.length, 0);
  await lab.search();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.scope.tenant, "tenant_a");
  assert.equal(calls[0].options.body.query, "manual query");
});

test("Lab tabs update accessible selection and the hash without triggering business requests", (t) => {
  const window = new EventTarget();
  window.location = { hash: "#/lab/search" };
  const root = new FakeRoot();
  for (const tab of ["search", "chat", "ingest"]) {
    const button = root.querySelector(`[data-lab-tab="${tab}"]`);
    button.dataset.labTab = tab;
    const panel = root.querySelector(`[data-lab-panel="${tab}"]`);
    panel.hidden = tab !== "search";
  }
  const lab = createLab({ root, window, document: new EventTarget() });
  t.after(() => lab.destroy());

  root.dispatch("click");

  assert.equal(root.querySelector('[data-lab-panel="chat"]').hidden, false);
  assert.equal(root.querySelector('[data-lab-panel="search"]').hidden, true);
  assert.equal(root.querySelector('[data-lab-tab="chat"]').getAttribute("aria-selected"), "true");
  assert.equal(root.querySelector('[data-lab-tab="search"]').getAttribute("aria-selected"), "false");
  assert.equal(window.location.hash, "#/lab/chat");
});

test("createLab without a root is a safe no-op for lightweight shell tests", async () => {
  const lab = createLab({ root: null });
  lab.setActive(true);
  lab.clear();
  lab.destroy();
  assert.equal(await lab.search({}), undefined);
  assert.equal(await lab.upload({}, null), undefined);
  assert.equal(await lab.showCitation("chunk"), undefined);
  assert.deepEqual(await lab.startChat({}, {}), { done: false, cancelled: true, citations: [] });
});
