import { ApiError, scopeHeaders } from "../api.mjs";
import { escapeHtml } from "../core.mjs";
import { formatError, t } from "../i18n/index.mjs";

const POLL_INTERVAL_MS = 900;
const TERMINAL_JOB_STATES = new Set(["succeeded", "failed"]);
const LAB_TABS = new Set(["search", "chat", "ingest"]);

function buttonLabels() {
  return {
    search: { idle: t("lab.search.run"), busy: t("lab.search.busy") },
    chat: { idle: t("lab.chat.run"), busy: t("lab.chat.busy") },
    upload: { idle: t("lab.upload.run"), busy: t("lab.upload.busy") },
  };
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
  let body = null;
  let message = response.statusText || t("errors.requestFailed");
  try {
    body = await response.json();
    if (typeof body?.detail === "string") message = body.detail;
    else if (Array.isArray(body?.detail)) message = body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    else if (typeof body?.message === "string") message = body.message;
    else if (typeof body?.error === "string") message = body.error;
  } catch (_error) {
    // Keep the HTTP status for non-JSON error responses.
  }
  return new ApiError(`HTTP ${response.status}: ${message}`, { status: response.status, body });
}

function responseTrace(result) {
  return result?.response?.headers?.get("X-Trace-Id") || "";
}

function renderAnswerHtml(answer, citations) {
  const pattern = /\[([A-Za-z0-9][A-Za-z0-9_.:#-]{0,159})\]/g;
  const citationIds = new Set(citations);
  let cursor = 0;
  let html = "";
  for (const match of String(answer).matchAll(pattern)) {
    html += escapeHtml(answer.slice(cursor, match.index));
    const chunkId = match[1];
    if (citationIds.has(chunkId)) {
      html += `<button type="button" class="wb-lab-inline-citation" data-citation="${escapeHtml(chunkId)}" title="${escapeHtml(t("lab.citation.expand", { id: chunkId }))}">${escapeHtml(match[0])}</button>`;
    } else {
      html += escapeHtml(match[0]);
    }
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
    const labels = buttonLabels()[kind];
    if (!labels) return;
    button.textContent = disabled ? labels.busy : labels.idle;
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
    setStatus("#citation-status", t("lab.noCitations"));
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
    setStatus("#search-status", t("lab.searchAwaiting"));
    setStatus("#chat-status", t("lab.chatAwaiting"));
    setStatus("#upload-status", t("lab.uploadAwaiting"));
    setText("#upload-error", "");
    const uploadError = byId("upload-error");
    if (uploadError) uploadError.hidden = true;
    const progress = byId("upload-progress");
    if (progress) progress.value = 0;
    setText("#upload-progress-value", "0%");
    for (const kind of ["search", "chat", "upload"]) setButton(kind, false);
  }

  function clearState({ preserveContext = false } = {}) {
    cancelAll();
    if (!preserveContext) currentContext = null;
    chatState = null;
    history = [];
    hitCache = new Map();
    citationRequests = new Map();
    clearView();
  }

  function clear({ preserveContext = false } = {}) {
    if (destroyed) return;
    clearState({ preserveContext });
  }

  function cacheHits(hits) {
    for (const hit of Array.isArray(hits) ? hits : []) {
      if (hit?.chunk_id !== undefined && hit?.chunk_id !== null) {
        hitCache.set(String(hit.chunk_id), hit);
      }
    }
  }

  function renderSearchHits(hits, emptyMessage = t("lab.noRetrievalRuns")) {
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
        <div class="wb-lab-hit-head"><code>${escapeHtml(hit?.chunk_id)}</code><span>${escapeHtml(t("lab.search.score", { score: Number.isFinite(score) ? score.toFixed(6) : "—" }))}</span></div>
        <p class="wb-lab-source">${escapeHtml(t("lab.search.document", { docId: hit?.doc_id ?? "" }))}</p>
        <p class="wb-lab-hit-text">${escapeHtml(hit?.text)}</p>
        <pre class="wb-lab-metadata">${escapeHtml(JSON.stringify(hit?.metadata ?? {}, null, 2))}</pre>
      </article></li>`;
    }).join("");
  }

  function validateScope(context, includeUser = true) {
    if (!context?.scope?.tenant || !context?.scope?.kb) {
      throw new Error(t("lab.validation.tenantAndKb"));
    }
    if (includeUser && !context.scope.user) throw new Error(t("lab.validation.user"));
  }

  async function search(context = currentContext, input = {}) {
    if (destroyed) return undefined;
    let query;
    try {
      validateScope(context);
      query = String(input.query ?? byId("search-query")?.value ?? "").trim();
      if (!query) throw new Error(t("lab.validation.query"));
    } catch (error) {
      setStatus("#search-status", error.message, "error");
      return undefined;
    }
    const operation = beginOperation("search", context);
    if (!operation) return undefined;
    const topK = Number(input.topK ?? byId("search-top-k")?.value ?? 5);
    const mode = String(input.mode ?? byId("search-mode")?.value ?? "hybrid");
    const audience = String(input.audience ?? byId("search-audience")?.value ?? "public");
    setButton("search", true);
    renderSearchHits([], t("lab.search.busy"));
    setStatus("#search-status", t("lab.search.searching"));
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
      renderSearchHits(hits, t("lab.search.noMatches"));
      setStatus("#search-status", t("lab.search.complete", { count: hits.length, mode: body.mode || mode }), hits.length ? "success" : "warning");
      setTrace("#search-trace-link", responseTrace(result));
      return hits;
    } catch (error) {
      if (isCurrent(operation) && !isAbort(error)) {
        renderSearchHits([], t("lab.search.failedEmpty"));
        setStatus("#search-status", t("lab.search.failed", { error: formatError(error) }), "error");
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
    answer.innerHTML = renderAnswerHtml(chat.answer, chat.citations);
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
        ? t("lab.citation.available", { count: chat.citations.length })
        : t("lab.citation.missingFrame"),
      chat.citations.length ? "neutral" : "warning",
    );
  }

  function renderStreamMeta(payload) {
    const notes = [];
    if (payload.rewritten) notes.push(t("lab.stream.rewritten", { value: payload.rewritten }));
    const usage = formatObject(payload.usage);
    const timings = formatObject(payload.timings);
    if (usage) notes.push(t("lab.stream.usage", { value: usage }));
    if (timings) notes.push(t("lab.stream.timings", { value: timings }));
    if (payload.rerank_degraded) notes.push(t("lab.stream.rerankDegraded"));
    if (payload.citation_ok === false) notes.push(t("lab.stream.citationFailed"));
    if (payload.trace_id) notes.push(t("lab.stream.trace", { traceId: payload.trace_id }));
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
      throw new Error(t("lab.stream.parseError"));
    }
    if (eventName === "error") throw new Error(payload?.message || t("lab.stream.serverError"));
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
    if (!response.body) throw new Error(t("lab.stream.unavailable"));
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
      if (!receivedDone) throw new Error(t("lab.stream.ended"));
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
    if (destroyed) return { done: false, cancelled: true, citations: [] };
    const options = chatInput(input);
    try {
      validateScope(context);
      if (!options.question) throw new Error(t("lab.validation.question"));
    } catch (error) {
      setStatus("#chat-status", error.message, "error");
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
    setStatus("#chat-status", t("lab.chat.connecting"));
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
      setStatus("#chat-status", t("lab.chat.streaming"));
      await consumeStream(response, operation);
      if (!isCurrent(operation)) return { done: false, cancelled: true, citations: operation.citations };
      operation.done = true;
      history.push(
        { role: "user", content: options.question },
        { role: "assistant", content: operation.answer },
      );
      history = history.slice(-12);
      setStatus("#chat-status", t("lab.chat.complete", { count: operation.citations.length }), "success");
      return { done: true, cancelled: false, citations: [...operation.citations] };
    } catch (error) {
      const cancelled = isAbort(error) || !isCurrent(operation);
      if (!cancelled) setStatus("#chat-status", t("lab.chat.failed", { error: formatError(error) }), "error");
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
    setText("#citation-source", t("lab.citation.source", {
      docId: hit.doc_id,
      score: Number.isFinite(score) ? score.toFixed(6) : "—",
    }));
    setText("#citation-text", String(hit.text ?? ""));
    setText("#citation-metadata", JSON.stringify(hit.metadata ?? {}, null, 2));
    setStatus("#citation-status", t("lab.citation.expanded", { id: hit.chunk_id }), "success");
  }

  async function showCitation(chunkId) {
    if (destroyed) return undefined;
    const chat = chatState;
    if (!chat || !active || document?.visibilityState === "hidden") {
      setStatus("#citation-status", t("lab.citation.chatRequired"), "warning");
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
      setStatus("#citation-status", t("lab.citation.retrieving", { id }));
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
        setStatus("#citation-status", t("lab.citation.missing", { id }), "warning");
        return undefined;
      } catch (error) {
        if (isCurrent(operation) && !isAbort(error)) {
          if (detail) detail.hidden = true;
          setStatus("#citation-status", t("lab.citation.failed", { error: formatError(error) }), "error");
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

  async function upload(context = currentContext, file) {
    if (destroyed) return undefined;
    file = file === undefined ? byId("upload-input")?.files?.[0] : file;
    try {
      validateScope(context, false);
      if (!file) throw new Error(t("lab.validation.document"));
      if (!/\.(pdf|md|txt)$/i.test(file.name)) throw new Error(t("lab.validation.fileType"));
    } catch (error) {
      setStatus("#upload-status", error.message, "error");
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
    setStatus("#upload-status", t("lab.upload.uploading", { name: file.name }));

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
      setStatus("#upload-status", t("lab.upload.created", {
        jobId: created.job_id,
        docId: created.doc_id,
        duplicated: created.duplicated ? ` · ${t("lab.upload.duplicated")}` : "",
      }));

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
          t("lab.upload.status", {
            status: job.status,
            id: job.id,
            docId: job.doc_id,
            source: job.source,
            attempts: job.attempts,
            duplicated: operation.duplicated ? ` · ${t("lab.upload.duplicated")}` : "",
          }),
          job.status === "failed" ? "error" : job.status === "succeeded" ? "success" : "neutral",
        );
        showUploadError(job.error ? t("lab.upload.jobError", { error: job.error }) : "");
        if (TERMINAL_JOB_STATES.has(job.status)) return job;
      }
      return undefined;
    } catch (error) {
      if (isCurrent(operation) && !isAbort(error)) {
        setStatus("#upload-status", t("lab.upload.failed"), "error");
        showUploadError(formatError(error));
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
    if (destroyed) return;
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

  function onRootKeydown(event) {
    if (destroyed) return;
    const tab = event.target?.closest?.("[data-lab-tab]") ?? event.target;
    const selected = tab?.dataset?.labTab;
    if (!LAB_TABS.has(selected)) return;
    const tabs = [...LAB_TABS];
    const index = tabs.indexOf(selected);
    let next;
    if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    else if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (event.key === "Home") next = tabs[0];
    else if (event.key === "End") next = tabs[tabs.length - 1];
    else return;
    event.preventDefault();
    selectTab(next, { updateHash: true });
    root.querySelector(`[data-lab-tab="${next}"]`)?.focus();
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
    if (destroyed) return;
    active = Boolean(value);
    if (!active) clear({ preserveContext: true });
  }

  function onRootSubmit(event) {
    if (destroyed) return;
    const form = event.target?.closest?.("form") ?? event.target;
    if (!["upload-form", "search-form", "chat-form"].includes(form?.id)) return;
    event.preventDefault();
    if (form.id === "upload-form") void upload();
    else if (form.id === "search-form") void search();
    else void startChat();
  }

  root.addEventListener("submit", onRootSubmit);
  root.addEventListener("click", onRootClick);
  root.addEventListener("keydown", onRootKeydown);
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
      clearState();
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      window?.removeEventListener("hashchange", onHashChange);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
      root.removeEventListener?.("submit", onRootSubmit);
      root.removeEventListener?.("click", onRootClick);
      root.removeEventListener?.("keydown", onRootKeydown);
    },
  };
}
