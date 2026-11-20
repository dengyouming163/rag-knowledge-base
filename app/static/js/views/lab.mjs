import { scopeHeaders } from "../api.mjs";
import { escapeHtml } from "../core.mjs";

const POLL_INTERVAL_MS = 900;
const TERMINAL_JOB_STATES = new Set(["succeeded", "failed"]);
const LAB_TABS = new Set(["search", "chat", "ingest"]);
const BUTTON_LABELS = {
  search: "Run search",
  chat: "Start streaming chat",
  upload: "Upload and track job",
};

function errorMessage(error) {
  return error instanceof Error && error.message ? error.message : "Request failed";
}

function isAbort(error) {
  return error?.name === "AbortError";
}

function unwrap(result) {
  return result && Object.hasOwn(result, "data") && Object.hasOwn(result, "response")
    ? result.data
    : result;
}

async function responseError(response) {
  let message = response.statusText || "Request failed";
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") message = body.detail;
    else if (Array.isArray(body?.detail)) message = body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    else if (typeof body?.message === "string") message = body.message;
    else if (typeof body?.error === "string") message = body.error;
  } catch (_error) {
    // Keep the HTTP status for non-JSON error responses.
  }
  return new Error(`HTTP ${response.status}: ${message}`);
}

function responseTrace(result) {
  return result?.response?.headers?.get("X-Trace-Id") || "";
}

function renderAnswerHtml(answer) {
  const pattern = /\[([A-Za-z0-9][A-Za-z0-9_.:#-]{0,159})\]/g;
  let cursor = 0;
  let html = "";
  for (const match of String(answer).matchAll(pattern)) {
    html += escapeHtml(answer.slice(cursor, match.index));
    const chunkId = match[1];
    html += `<button type="button" class="wb-lab-inline-citation" data-citation="${escapeHtml(chunkId)}" title="Expand citation ${escapeHtml(chunkId)}">${escapeHtml(match[0])}</button>`;
    cursor = match.index + match[0].length;
  }
  return html + escapeHtml(answer.slice(cursor));
}

function formatObject(value) {
  if (!value || typeof value !== "object") return "";
  return Object.entries(value).map(([key, item]) => `${key}=${typeof item === "object" ? JSON.stringify(item) : item}`).join(", ");
}

export function createLab({
  root,
  guard,
  scope = null,
  window = globalThis.window,
  document = globalThis.document,
  api,
  fetchImpl = globalThis.fetch?.bind(globalThis),
  FormDataImpl = globalThis.FormData,
  setTimeout = globalThis.setTimeout,
  clearTimeout = globalThis.clearTimeout,
  pollInterval = POLL_INTERVAL_MS,
} = {}) {
  if (!root) {
    return {
      async search() {},
      async startChat() { return { done: false, cancelled: true, citations: [] }; },
      async upload() {},
      async showCitation() {},
      setActive() {},
      clear() {},
      destroy() {},
      selectTab() {},
    };
  }

  let active = false;
  let destroyed = false;
  let sequence = 0;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let currentTab = "search";
  let history = [];
  let chatState = null;
  let hitCache = new Map();
  let citationRequests = new Map();
  const operations = { search: null, chat: null, upload: null, citation: null };

  const byId = (id) => root.querySelector(`#${id}`);
  const setText = (selector, value) => {
    const element = root.querySelector(selector);
    if (element) element.textContent = value;
  };
  const setStatus = (selector, message, tone = "neutral") => {
    const element = root.querySelector(selector);
    if (!element) return;
    element.textContent = message;
    element.dataset.tone = tone;
  };
  const setButton = (kind, disabled) => {
    const button = byId(`${kind}-button`);
    if (!button) return;
    button.disabled = disabled;
    button.textContent = disabled
      ? { search: "Searching…", chat: "Answering…", upload: "Processing…" }[kind]
      : BUTTON_LABELS[kind];
  };

  async function defaultApi(path, { scope: requestScope = {}, body, signal, ...options } = {}) {
    const isForm = typeof FormData !== "undefined" && body instanceof FormData;
    const hasJson = body !== undefined && body !== null && !isForm;
    const response = await fetchImpl(path, {
      ...options,
      headers: scopeHeaders(requestScope, { json: hasJson }),
      body: hasJson ? JSON.stringify(body) : body,
      signal,
    });
    if (!response.ok) throw await responseError(response);
    const data = response.status === 204 ? null : await response.json();
    return { data, response };
  }

  const request = api ?? defaultApi;

  function isCurrent(operation) {
    return !destroyed
      && active
      && document?.visibilityState !== "hidden"
      && operations[operation.kind] === operation
      && !operation.controller.signal.aborted
      && (!guard || guard.isCurrent(operation.context));
  }

  function clearTimer(operation) {
    if (!operation?.timer) return;
    clearTimeout(operation.timer.id);
    const resolve = operation.timer.resolve;
    operation.timer = null;
    resolve();
  }

  function cancelOperation(kind) {
    const operation = operations[kind];
    if (!operation) return;
    operations[kind] = null;
    clearTimer(operation);
    operation.unlink();
    operation.controller.abort();
    setButton(kind, false);
  }

  function cancelAll() {
    for (const kind of Object.keys(operations)) cancelOperation(kind);
  }

  function beginOperation(kind, context) {
    cancelOperation(kind);
    if (destroyed || !active || document?.visibilityState === "hidden") return null;
    if (!context?.scope || context.signal?.aborted || (guard && !guard.isCurrent(context))) return null;
    const controller = new AbortController();
    const parent = context.signal;
    const abort = () => controller.abort();
    if (parent) parent.addEventListener("abort", abort, { once: true });
    const operation = {
      id: ++sequence,
      kind,
      context,
      controller,
      timer: null,
      unlink() { parent?.removeEventListener("abort", abort); },
    };
    operations[kind] = operation;
    return operation;
  }

  function waitForPoll(operation) {
    return new Promise((resolve) => {
      operation.timer = {
        id: setTimeout(() => {
          operation.timer = null;
          resolve();
        }, pollInterval),
        resolve,
      };
    });
  }

  function setTrace(selector, traceId) {
    const link = root.querySelector(selector);
    if (!link) return;
    const id = String(traceId || "").trim();
    link.hidden = !id;
    if (id) link.setAttribute("href", `#/traces/${encodeURIComponent(id)}`);
    else link.removeAttribute("href");
  }

  function clearCitation() {
    setText("#citation-list", "");
    const list = byId("citation-list");
    if (list) list.innerHTML = "";
    const detail = byId("citation-detail");
    if (detail) detail.hidden = true;
    for (const id of ["citation-heading", "citation-source", "citation-text", "citation-metadata"]) {
      setText(`#${id}`, "");
    }
    setStatus("#citation-status", "No citations to expand.");
  }

  function clearView() {
    for (const selector of ["#answer", "#stream-meta", "#search-results"]) {
      const element = root.querySelector(selector);
      if (!element) continue;
      element.textContent = "";
      element.innerHTML = "";
    }
    clearCitation();
    setTrace("#search-trace-link", "");
    setTrace("#chat-trace-link", "");
    setStatus("#search-status", "Awaiting a retrieval query.");
    setStatus("#chat-status", "Awaiting a question.");
    setStatus("#upload-status", "Awaiting a document.");
    setText("#upload-error", "");
    const uploadError = byId("upload-error");
    if (uploadError) uploadError.hidden = true;
    const progress = byId("upload-progress");
    if (progress) progress.value = 0;
    setText("#upload-progress-value", "0%");
    for (const kind of ["search", "chat", "upload"]) setButton(kind, false);
  }

  function clear({ preserveContext = false } = {}) {
    cancelAll();
    if (!preserveContext) currentContext = null;
    chatState = null;
    history = [];
    hitCache = new Map();
    citationRequests = new Map();
    clearView();
  }

  function cacheHits(hits) {
    for (const hit of Array.isArray(hits) ? hits : []) {
      if (hit?.chunk_id !== undefined && hit?.chunk_id !== null) {
        hitCache.set(String(hit.chunk_id), hit);
      }
    }
  }

  function renderSearchHits(hits, emptyMessage = "No retrieval runs yet.") {
    const container = byId("search-results");
    if (!container) return;
    const items = Array.isArray(hits) ? hits : [];
    if (!items.length) {
      container.innerHTML = `<li class="wb-lab-empty">${escapeHtml(emptyMessage)}</li>`;
      return;
    }
    container.innerHTML = items.map((hit) => {
      const score = Number(hit?.score);
      return `<li><article class="wb-lab-hit">
        <div class="wb-lab-hit-head"><code>${escapeHtml(hit?.chunk_id)}</code><span>score ${Number.isFinite(score) ? score.toFixed(6) : "—"}</span></div>
        <p class="wb-lab-source">doc_id: ${escapeHtml(hit?.doc_id)}</p>
        <p class="wb-lab-hit-text">${escapeHtml(hit?.text)}</p>
        <pre class="wb-lab-metadata">${escapeHtml(JSON.stringify(hit?.metadata ?? {}, null, 2))}</pre>
      </article></li>`;
    }).join("");
  }

  function validateScope(context, includeUser = true) {
    if (!context?.scope?.tenant || !context?.scope?.kb) {
      throw new Error("Tenant ID and knowledge base are required.");
    }
    if (includeUser && !context.scope.user) throw new Error("User ID is required.");
  }

  async function search(context = currentContext, input = {}) {
    let query;
    try {
      validateScope(context);
      query = String(input.query ?? byId("search-query")?.value ?? "").trim();
      if (!query) throw new Error("Enter a retrieval query.");
    } catch (error) {
      setStatus("#search-status", errorMessage(error), "error");
      return undefined;
    }
    const operation = beginOperation("search", context);
    if (!operation) return undefined;
    const topK = Number(input.topK ?? byId("search-top-k")?.value ?? 5);
    const mode = String(input.mode ?? byId("search-mode")?.value ?? "hybrid");
    const audience = String(input.audience ?? byId("search-audience")?.value ?? "public");
    setButton("search", true);
    renderSearchHits([], "Searching…");
    setStatus("#search-status", "Searching the active scope…");
    setTrace("#search-trace-link", "");

    try {
      const result = await request(`/v1/kbs/${encodeURIComponent(context.scope.kb)}/search`, {
        method: "POST",
        scope: context.scope,
        body: { query, top_k: topK, mode, audience },
        signal: operation.controller.signal,
      });
      if (!isCurrent(operation)) return undefined;
      const body = unwrap(result) ?? {};
      const hits = Array.isArray(body.hits) ? body.hits : [];
      cacheHits(hits);
      renderSearchHits(hits, "No matching chunks. Adjust the query, mode, or audience.");
      setStatus("#search-status", `Search complete · ${hits.length} hits · ${body.mode || mode}`, hits.length ? "success" : "warning");
      setTrace("#search-trace-link", responseTrace(result));
      return hits;
    } catch (error) {
      if (isCurrent(operation) && !isAbort(error)) {
        renderSearchHits([], "Search failed; no results are available.");
        setStatus("#search-status", `Search failed: ${errorMessage(error)}`, "error");
      }
      return undefined;
    } finally {
      if (operations.search === operation) {
        operations.search = null;
        operation.unlink();
        setButton("search", false);
      }
    }
  }

  function renderAnswer(chat) {
    const answer = byId("answer");
    if (!answer) return;
    answer.textContent = chat.answer;
    answer.innerHTML = renderAnswerHtml(chat.answer);
    answer.scrollTop = answer.scrollHeight;
  }

  function renderCitationList(chat) {
    const list = byId("citation-list");
    if (list) {
      list.innerHTML = chat.citations.map((chunkId) => (
        `<li><button type="button" class="wb-lab-citation-chip" data-citation="${escapeHtml(chunkId)}">[${escapeHtml(chunkId)}]</button></li>`
      )).join("");
    }
    const detail = byId("citation-detail");
    if (detail) detail.hidden = true;
    setStatus(
      "#citation-status",
      chat.citations.length
        ? `${chat.citations.length} citations available; select one to inspect its source.`
        : "The hits frame did not provide citations.",
      chat.citations.length ? "neutral" : "warning",
    );
  }

  function renderStreamMeta(payload) {
    const notes = [];
    if (payload.rewritten) notes.push(`Rewritten: ${payload.rewritten}`);
    const usage = formatObject(payload.usage);
    const timings = formatObject(payload.timings);
    if (usage) notes.push(`Usage: ${usage}`);
    if (timings) notes.push(`Timings: ${timings}`);
    if (payload.rerank_degraded) notes.push("Rerank degraded");
    if (payload.citation_ok === false) notes.push("Citation validation failed; verify the source");
    if (payload.trace_id) notes.push(`Trace: ${payload.trace_id}`);
    setText("#stream-meta", notes.join(" · "));
  }

  function consumeFrame(chat, rawFrame) {
    let eventName = "message";
    const dataLines = [];
    for (const line of rawFrame.split(/\r\n|\n|\r/)) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) eventName = line.slice(6).trimStart();
      else if (line.startsWith("data:")) {
        let value = line.slice(5);
        if (value.startsWith(" ")) value = value.slice(1);
        dataLines.push(value);
      }
    }
    if (!dataLines.length) return false;
    const rawData = dataLines.join("\n");
    if (rawData === "[DONE]") return true;
    let payload;
    try {
      payload = JSON.parse(rawData);
    } catch (_error) {
      throw new Error("Unable to parse the server SSE data.");
    }
    if (eventName === "error") throw new Error(payload?.message || "The server returned a streaming error.");
    if (!isCurrent(chat)) return false;
    if (eventName === "hits") {
      chat.citations = [...new Set((Array.isArray(payload.chunks) ? payload.chunks : []).map(String).filter(Boolean))];
      chat.rewritten = typeof payload.rewritten === "string" ? payload.rewritten : "";
      renderStreamMeta(payload);
      renderCitationList(chat);
      setTrace("#chat-trace-link", payload.trace_id);
      return false;
    }
    if (typeof payload?.delta === "string") {
      chat.answer += payload.delta;
      renderAnswer(chat);
    }
    return false;
  }

  async function consumeStream(response, chat) {
    if (!response.body) throw new Error("The browser did not receive a readable response stream.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let receivedDone = false;

    function drainFrames() {
      let separator = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
      while (separator && !receivedDone) {
        const frame = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        receivedDone = consumeFrame(chat, frame);
        separator = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
      }
    }

    try {
      while (!receivedDone) {
        const result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, { stream: true });
        drainFrames();
      }
      buffer += decoder.decode();
      if (!receivedDone) drainFrames();
      if (!receivedDone && buffer.trim()) receivedDone = consumeFrame(chat, buffer);
      if (!receivedDone) throw new Error("The stream ended before [DONE].");
      await reader.cancel().catch(() => undefined);
    } catch (error) {
      await reader.cancel().catch(() => undefined);
      throw error;
    } finally {
      reader.releaseLock();
    }
  }

  function chatInput(input) {
    return {
      question: String(input.question ?? byId("chat-question")?.value ?? "").trim(),
      topK: Number(input.topK ?? byId("chat-top-k")?.value ?? 5),
      audience: String(input.audience ?? byId("chat-audience")?.value ?? "public"),
    };
  }

  async function startChat(context = currentContext, input = {}, { fetch: fetchOverride } = {}) {
    const options = chatInput(input);
    try {
      validateScope(context);
      if (!options.question) throw new Error("Enter a question.");
    } catch (error) {
      setStatus("#chat-status", errorMessage(error), "error");
      return { done: false, cancelled: false, citations: [] };
    }
    cancelOperation("citation");
    const operation = beginOperation("chat", context);
    if (!operation) return { done: false, cancelled: true, citations: [] };
    Object.assign(operation, {
      ...options,
      answer: "",
      citations: [],
      rewritten: "",
      done: false,
    });
    chatState = operation;
    citationRequests = new Map();
    setButton("chat", true);
    setText("#stream-meta", "");
    setTrace("#chat-trace-link", "");
    clearCitation();
    renderAnswer(operation);
    setStatus("#chat-status", "Connecting to the streaming chat API…");
    const streamFetch = fetchOverride ?? fetchImpl;

    try {
      const response = await streamFetch("/v1/chat/stream", {
        method: "POST",
        headers: {
          ...scopeHeaders(context.scope, { json: true }),
          Accept: "text/event-stream",
        },
        body: JSON.stringify({
          kb_id: context.scope.kb,
          question: options.question,
          history: history.slice(-12).map((turn) => ({ ...turn })),
          top_k: options.topK,
          audience: options.audience,
        }),
        signal: operation.controller.signal,
      });
      if (!response.ok) throw await responseError(response);
      if (!isCurrent(operation)) return { done: false, cancelled: true, citations: operation.citations };
      setTrace("#chat-trace-link", response.headers?.get("X-Trace-Id"));
      setStatus("#chat-status", "Streaming response · receiving deltas…");
      await consumeStream(response, operation);
      if (!isCurrent(operation)) return { done: false, cancelled: true, citations: operation.citations };
      operation.done = true;
      history.push(
        { role: "user", content: options.question },
        { role: "assistant", content: operation.answer },
      );
      history = history.slice(-12);
      setStatus("#chat-status", `Answer complete · [DONE] received · ${operation.citations.length} citations`, "success");
      return { done: true, cancelled: false, citations: [...operation.citations] };
    } catch (error) {
      const cancelled = isAbort(error) || !isCurrent(operation);
      if (!cancelled) setStatus("#chat-status", `Streaming chat failed: ${errorMessage(error)}; try again.`, "error");
      return { done: false, cancelled, citations: [...operation.citations] };
    } finally {
      if (operations.chat === operation) {
        operations.chat = null;
        operation.unlink();
        setButton("chat", false);
      }
    }
  }

  function renderCitation(hit) {
    const detail = byId("citation-detail");
    if (detail) detail.hidden = false;
    setText("#citation-heading", `[${hit.chunk_id}]`);
    const score = Number(hit.score);
    setText("#citation-source", `doc_id: ${hit.doc_id} · score ${Number.isFinite(score) ? score.toFixed(6) : "—"}`);
    setText("#citation-text", String(hit.text ?? ""));
    setText("#citation-metadata", JSON.stringify(hit.metadata ?? {}, null, 2));
    setStatus("#citation-status", `Expanded ${hit.chunk_id} with retrieval text and metadata.`, "success");
  }

  async function showCitation(chunkId) {
    const chat = chatState;
    if (!chat || destroyed || !active || document?.visibilityState === "hidden") {
      setStatus("#citation-status", "Source unavailable: complete a chat in the active scope first.", "warning");
      return undefined;
    }
    const id = String(chunkId);
    const cached = hitCache.get(id);
    if (citationRequests.has(id)) return citationRequests.get(id);
    cancelOperation("citation");
    if (cached) {
      renderCitation(cached);
      return cached;
    }

    const promise = (async () => {
      const operation = beginOperation("citation", chat.context);
      if (!operation) return undefined;
      const detail = byId("citation-detail");
      if (detail) detail.hidden = true;
      setStatus("#citation-status", `Retrieving source text for ${id}…`);
      try {
        const result = await request(`/v1/kbs/${encodeURIComponent(chat.context.scope.kb)}/search`, {
          method: "POST",
          scope: chat.context.scope,
          body: {
            query: chat.rewritten || chat.question,
            top_k: chat.topK,
            mode: "hybrid",
            audience: chat.audience,
          },
          signal: operation.controller.signal,
        });
        if (!isCurrent(operation) || chatState !== chat) return undefined;
        cacheHits(unwrap(result)?.hits);
        const hit = hitCache.get(id);
        if (hit) {
          renderCitation(hit);
          return hit;
        }
        if (detail) detail.hidden = true;
        setStatus("#citation-status", `Source unavailable: retrieval did not return ${id}.`, "warning");
        return undefined;
      } catch (error) {
        if (isCurrent(operation) && !isAbort(error)) {
          if (detail) detail.hidden = true;
          setStatus("#citation-status", `Citation retrieval failed: ${errorMessage(error)}`, "error");
        }
        return undefined;
      } finally {
        if (operations.citation === operation) {
          operations.citation = null;
          operation.unlink();
        }
      }
    })();
    citationRequests.set(id, promise);
    try {
      return await promise;
    } finally {
      if (citationRequests.get(id) === promise) citationRequests.delete(id);
    }
  }

  function showUploadError(message) {
    const element = byId("upload-error");
    if (!element) return;
    element.textContent = message;
    element.hidden = !message;
  }

  function renderUploadProgress(value) {
    const numeric = Number(value);
    const progress = Number.isFinite(numeric) ? Math.min(100, Math.max(0, numeric)) : 0;
    const element = byId("upload-progress");
    if (element) element.value = progress;
    setText("#upload-progress-value", `${Math.round(progress)}%`);
  }

  async function upload(context = currentContext, file = byId("upload-input")?.files?.[0]) {
    try {
      validateScope(context, false);
      if (!file) throw new Error("Select a document to upload.");
      if (!/\.(pdf|md|txt)$/i.test(file.name)) throw new Error("Only .pdf, .md, and .txt files are supported.");
    } catch (error) {
      setStatus("#upload-status", errorMessage(error), "error");
      return undefined;
    }
    const operation = beginOperation("upload", context);
    if (!operation) return undefined;
    operation.duplicated = false;
    const form = new FormDataImpl();
    form.append("file", file);
    showUploadError("");
    renderUploadProgress(0);
    setButton("upload", true);
    setStatus("#upload-status", `Uploading ${file.name}…`);

    try {
      const created = unwrap(await request(`/v1/kbs/${encodeURIComponent(context.scope.kb)}/documents`, {
        method: "POST",
        scope: { ...context.scope, user: "" },
        body: form,
        signal: operation.controller.signal,
      }));
      if (!isCurrent(operation)) return undefined;
      operation.jobId = created.job_id;
      operation.duplicated = Boolean(created.duplicated);
      setStatus("#upload-status", `Job #${created.job_id} created · document ${created.doc_id}${created.duplicated ? " · duplicated content reused" : ""}`);

      while (isCurrent(operation)) {
        await waitForPoll(operation);
        if (!isCurrent(operation)) return undefined;
        const job = unwrap(await request(`/v1/jobs/${encodeURIComponent(operation.jobId)}`, {
          scope: { ...context.scope, user: "" },
          signal: operation.controller.signal,
        }));
        if (!isCurrent(operation)) return undefined;
        renderUploadProgress(job.progress);
        setStatus(
          "#upload-status",
          `Status ${job.status} · job #${job.id} · document ${job.doc_id} · source ${job.source} · attempts ${job.attempts}${operation.duplicated ? " · duplicated content reused" : ""}`,
          job.status === "failed" ? "error" : job.status === "succeeded" ? "success" : "neutral",
        );
        showUploadError(job.error ? `Job error: ${job.error}` : "");
        if (TERMINAL_JOB_STATES.has(job.status)) return job;
      }
      return undefined;
    } catch (error) {
      if (isCurrent(operation) && !isAbort(error)) {
        setStatus("#upload-status", "Upload or job polling failed; retry when ready.", "error");
        showUploadError(errorMessage(error));
      }
      return undefined;
    } finally {
      if (operations.upload === operation) {
        clearTimer(operation);
        operations.upload = null;
        operation.unlink();
        setButton("upload", false);
      }
    }
  }

  function selectTab(tab, { updateHash = false } = {}) {
    const selected = LAB_TABS.has(tab) ? tab : "search";
    currentTab = selected;
    for (const name of LAB_TABS) {
      const button = root.querySelector(`[data-lab-tab="${name}"]`);
      const panel = root.querySelector(`[data-lab-panel="${name}"]`);
      const isActive = name === selected;
      if (button) {
        button.setAttribute("aria-selected", String(isActive));
        button.setAttribute("tabindex", isActive ? "0" : "-1");
      }
      if (panel) panel.hidden = !isActive;
    }
    if (updateHash && window?.location) window.location.hash = `#/lab/${selected}`;
  }

  function onRootClick(event) {
    const target = event.target;
    const citation = target?.closest?.("[data-citation]") ?? target;
    if (citation?.dataset?.citation) {
      void showCitation(citation.dataset.citation);
      return;
    }
    const tab = target?.closest?.("[data-lab-tab]") ?? target;
    if (tab?.dataset?.labTab) selectTab(tab.dataset.labTab, { updateHash: true });
  }

  function onScopeChange(event) {
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onRefresh(event) {
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onVisibilityChange() {
    if (document.visibilityState === "hidden") clear({ preserveContext: true });
  }

  function onHashChange() {
    const tab = window.location.hash.match(/^#\/lab\/(search|chat|ingest)$/)?.[1];
    if (viewIsLab()) selectTab(tab || "search");
  }

  function viewIsLab() {
    return !window?.location?.hash || /^#\/lab(?:\/|$)/.test(window.location.hash);
  }

  function setActive(value) {
    active = Boolean(value);
    if (!active) clear({ preserveContext: true });
  }

  root.addEventListener("submit", (event) => {
    const form = event.target?.closest?.("form") ?? event.target;
    if (!["upload-form", "search-form", "chat-form"].includes(form?.id)) return;
    event.preventDefault();
    if (form.id === "upload-form") void upload();
    else if (form.id === "search-form") void search();
    else void startChat();
  });
  root.addEventListener("click", onRootClick);
  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  window?.addEventListener("hashchange", onHashChange);
  document?.addEventListener("visibilitychange", onVisibilityChange);
  const initialTab = window?.location?.hash?.match(/^#\/lab\/(search|chat|ingest)$/)?.[1] || currentTab;
  selectTab(initialTab);

  return {
    search,
    startChat,
    upload,
    showCitation,
    selectTab,
    setActive,
    clear,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      clear();
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      window?.removeEventListener("hashchange", onHashChange);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
      root.removeEventListener?.("click", onRootClick);
    },
  };
}
