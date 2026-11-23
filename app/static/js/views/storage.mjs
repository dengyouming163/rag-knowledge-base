import { apiJson } from "../api.mjs";
import { escapeHtml } from "../core.mjs";
import { formatError, t } from "../i18n/index.mjs";

function scopeKey(scope = {}) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user, scope.window]);
}

export function formatBytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let size = n;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i += 1;
  }
  return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function statusLabel(status) {
  switch (status) {
    case "ready": return t("storage.status.ready");
    case "pending": return t("storage.status.pending");
    case "failed": return t("storage.status.failed");
    default: return status;
  }
}

export function statusClass(status) {
  if (status === "ready") return "ok";
  if (status === "pending") return "busy";
  if (status === "failed") return "bad";
  return "";
}

function formatDateTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function renderRows(documents) {
  if (!documents || documents.length === 0) return "";
  return documents
    .map((d) => {
      const cls = statusClass(d.status);
      const pill = cls
        ? `<span class="pill ${cls}">${escapeHtml(statusLabel(d.status))}</span>`
        : escapeHtml(statusLabel(d.status));
      return [
        "<tr>",
        `<td>${escapeHtml(d.filename || d.id)}</td>`,
        `<td>${escapeHtml(d.kb_id)}</td>`,
        `<td>${formatBytes(d.size)}</td>`,
        `<td>${pill}</td>`,
        `<td>${formatDateTime(d.created_at)}</td>`,
        "</tr>",
      ].join("");
    })
    .join("");
}

export function createStorage({
  root,
  guard,
  scope = null,
  api = apiJson,
  window = globalThis.window,
  document = globalThis.document,
} = {}) {
  if (!root) {
    return { async load() { return false; }, setActive() {}, clear() {}, destroy() {} };
  }

  let active = false;
  let destroyed = false;
  let currentContext = scope ? { scope, ...guard?.snapshot?.() } : null;

  const statsEl = root.querySelector("[data-storage-stats]");
  const bodyEl = root.querySelector("[data-storage-body]");
  const errorEl = root.querySelector("[data-storage-error]");
  const filterKb = root.querySelector("[data-storage-filter-kb]");
  const filterStatus = root.querySelector("[data-storage-filter-status]");

  const contextIsCurrent = (context) => !destroyed
    && active
    && document?.visibilityState !== "hidden"
    && currentContext
    && scopeKey(context?.scope) === scopeKey(currentContext.scope)
    && (!guard || guard.isCurrent(context));

  function card(label, value) {
    return `<div class="wb-stat-card"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`;
  }

  function renderStats(payload) {
    if (!statsEl) return;
    const s = payload.stats || {};
    statsEl.innerHTML = [
      card(t("storage.backend"), String(payload.backend ?? "").toUpperCase()),
      card(t("storage.bucket"), payload.bucket || "—"),
      card(t("storage.documents"), String(s.total ?? 0)),
      card(t("storage.totalSize"), formatBytes(s.bytes)),
      card(t("storage.ready"), String(s.by_status?.ready ?? 0)),
    ].join("");
  }

  function renderKbOptions(documents, preserve) {
    if (!filterKb) return;
    const current = preserve ? filterKb.value : "";
    const kbs = [...new Set((documents || []).map((d) => d.kb_id))].sort();
    filterKb.innerHTML =
      `<option value="">${t("storage.filterAll")}</option>` +
      kbs.map((kb) => `<option value="${escapeHtml(kb)}">${escapeHtml(kb)}</option>`).join("");
    if (preserve && [...filterKb.options].some((o) => o.value === current)) {
      filterKb.value = current;
    }
  }

  function resetFilters() {
    if (filterKb) filterKb.innerHTML = `<option value="">${t("storage.filterAll")}</option>`;
    if (filterStatus) {
      filterStatus.innerHTML = [
        `<option value="">${t("storage.filterAll")}</option>`,
        `<option value="ready">${t("storage.status.ready")}</option>`,
        `<option value="pending">${t("storage.status.pending")}</option>`,
        `<option value="failed">${t("storage.status.failed")}</option>`,
      ].join("");
    }
  }

  async function load() {
    const context = currentContext;
    if (!contextIsCurrent(context) || !bodyEl) return false;
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
    bodyEl.innerHTML = `<tr><td colspan="5">${escapeHtml(t("storage.loading"))}</td></tr>`;

    const params = new URLSearchParams();
    if (filterKb?.value) params.set("kb_id", filterKb.value);
    if (filterStatus?.value) params.set("status", filterStatus.value);

    try {
      const payload = await api(`/v1/storage?${params.toString()}`, { scope: context.scope });
      if (!contextIsCurrent(context)) return false;
      renderStats(payload);
      renderKbOptions(payload.documents, true);
      bodyEl.innerHTML = renderRows(payload.documents)
        || `<tr><td colspan="5">${escapeHtml(t("storage.empty"))}</td></tr>`;
      return true;
    } catch (error) {
      if (!contextIsCurrent(context)) return false;
      if (statsEl) statsEl.innerHTML = "";
      bodyEl.innerHTML = `<tr><td colspan="5">${escapeHtml(t("storage.empty"))}</td></tr>`;
      if (errorEl) { errorEl.hidden = false; errorEl.textContent = formatError(error); }
      return false;
    }
  }

  function onScopeChange(event) {
    if (destroyed) return;
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onRefresh(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    if (event.detail?.view !== "storage" || !contextIsCurrent(currentContext)) return;
    void load();
  }

  function onFilterChange() {
    if (active) void load();
  }

  function setActive(value) {
    if (destroyed) return;
    active = Boolean(value);
  }

  function clear() {
    if (destroyed) return;
    if (statsEl) statsEl.innerHTML = "";
    if (bodyEl) bodyEl.innerHTML = "";
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
    resetFilters();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    active = false;
    window?.removeEventListener("workbuddy:scopechange", onScopeChange);
    window?.removeEventListener("workbuddy:refresh", onRefresh);
    filterKb?.removeEventListener("change", onFilterChange);
    filterStatus?.removeEventListener("change", onFilterChange);
  }

  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  filterKb?.addEventListener("change", onFilterChange);
  filterStatus?.addEventListener("change", onFilterChange);
  resetFilters();

  return { load, setActive, clear, destroy };
}
