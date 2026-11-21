import { ScopeGuard } from "./core.mjs";
import { createOverview } from "./views/overview.mjs";
import { createLab } from "./views/lab.mjs";
import { createTraces } from "./views/traces.mjs";
import { createPermissions } from "./permissions.mjs";
import { createIngestion } from "./views/ingestion.mjs";
import { createEvaluation } from "./views/evaluation.mjs";
import { createAccess } from "./views/access.mjs";
import { createAudit } from "./views/audit.mjs";

const VIEW_NAMES = ["overview", "lab", "traces", "ingestion", "evaluation", "access", "audit"];
const scopeGuard = new ScopeGuard();
let activeScope = null;
let overviewController = null;
let labController = null;
let tracesController = null;
let permissionsController = null;
let ingestionController = null;
let evaluationController = null;
let accessController = null;
let auditController = null;

function normalizeView(name) {
  return VIEW_NAMES.includes(name) ? name : "overview";
}

export function viewFromHash(hash) {
  const requested = String(hash ?? "").replace(/^#\/?/, "").split(/[/?]/)[0];
  return normalizeView(requested);
}

function canonicalTraceHash(hash) {
  const match = String(hash ?? "").match(/^#\/traces\/([^/?#]+)$/);
  if (!match) return null;
  try {
    const traceId = decodeURIComponent(match[1]);
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(traceId)) return null;
    return `#/traces/${encodeURIComponent(traceId)}`;
  } catch (_error) {
    return null;
  }
}

export function showView(name, root = globalThis.document) {
  const activeView = normalizeView(name);
  root.querySelectorAll("[data-view]").forEach((view) => {
    const isActive = view.dataset.view === activeView;
    view.hidden = !isActive;
    if (isActive) {
      view.removeAttribute("aria-hidden");
    } else {
      view.setAttribute("aria-hidden", "true");
    }
  });
  root.querySelectorAll("[data-nav]").forEach((button) => {
    const isActive = button.dataset.nav === activeView;
    button.classList.toggle("is-active", isActive);
    if (isActive) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  overviewController?.setActive(activeView === "overview");
  labController?.setActive(activeView === "lab");
  tracesController?.setActive(activeView === "traces");
  ingestionController?.setActive(activeView === "ingestion");
  evaluationController?.setActive(activeView === "evaluation");
  accessController?.setActive(activeView === "access");
  auditController?.setActive(activeView === "audit");
  return activeView;
}

function readScope() {
  const value = (id) => document.getElementById(id)?.value.trim() ?? "";
  return {
    tenant: value("scope-tenant"),
    kb: value("scope-kb"),
    user: value("scope-user"),
    window: value("scope-window"),
  };
}

function clearViewState() {
  document.querySelectorAll("[data-view]").forEach((view) => {
    if (["overview", "lab", "traces", "ingestion", "evaluation", "access", "audit"].includes(view.dataset.view)) return;
    view.querySelectorAll("[data-view-body]").forEach((body) => body.replaceChildren());
    view.querySelectorAll(".is-selected").forEach((item) => item.classList.remove("is-selected"));
    view.querySelectorAll("[aria-selected]").forEach((item) => item.setAttribute("aria-selected", "false"));
    view.querySelectorAll("[data-selected]").forEach((item) => item.removeAttribute("data-selected"));
  });
  overviewController?.clear();
  labController?.clear();
  tracesController?.clear();
  ingestionController?.clear();
  evaluationController?.clear();
  accessController?.clear();
  auditController?.clear();
  permissionsController?.clear();
}

function setStatus(message) {
  const status = document.getElementById("app-status");
  if (status) status.textContent = message;
}

function applyScope() {
  const scope = readScope();
  if (activeScope && JSON.stringify(scope) === JSON.stringify(activeScope)) return false;

  const previousScope = activeScope;
  scopeGuard.invalidate();
  clearViewState();
  activeScope = scope;
  const snapshot = scopeGuard.snapshot();
  setStatus("Scope changed; previous view data cleared.");
  window.dispatchEvent(new CustomEvent("workbuddy:scopechange", {
    detail: { scope, previousScope, ...snapshot },
  }));
  return true;
}

function restoreRoute() {
  const view = viewFromHash(window.location.hash);
  const labTab = view === "lab"
    ? window.location.hash.match(/^#\/lab\/(search|chat|ingest)$/)?.[1]
    : null;
  const traceHash = view === "traces" ? canonicalTraceHash(window.location.hash) : null;
  const canonicalHash = labTab ? `#/lab/${labTab}` : traceHash || `#/${view}`;
  if (window.location.hash !== canonicalHash) {
    window.history.replaceState(null, "", canonicalHash);
  }
  showView(view);
  return view;
}

function destroyWorkbench() {
  scopeGuard.invalidate();
  overviewController?.destroy?.();
  labController?.destroy?.();
  tracesController?.destroy?.();
  ingestionController?.destroy?.();
  evaluationController?.destroy?.();
  accessController?.destroy();
  auditController?.destroy();
  permissionsController?.destroy();
}

function initializeWorkbench() {
  activeScope = readScope();
  overviewController = createOverview({
    root: document.getElementById("view-overview"),
    guard: scopeGuard,
  });
  labController = createLab({
    root: document.getElementById("view-lab"),
    guard: scopeGuard,
    scope: activeScope,
  });
  tracesController = createTraces({
    root: document.getElementById("view-traces"),
    guard: scopeGuard,
    scope: activeScope,
  });
  permissionsController = createPermissions({
    guard: scopeGuard,
    scope: activeScope,
  });
  ingestionController = createIngestion({
    root: document.getElementById("view-ingestion"),
    guard: scopeGuard,
    scope: activeScope,
    permissions: permissionsController,
  });
  evaluationController = createEvaluation({
    root: document.getElementById("view-evaluation"),
    guard: scopeGuard,
    scope: activeScope,
    permissions: permissionsController,
  });
  accessController = createAccess({
    root: document.getElementById("view-access"),
    guard: scopeGuard,
    scope: activeScope,
    permissions: permissionsController,
  });
  auditController = createAudit({
    root: document.getElementById("view-audit"),
    guard: scopeGuard,
    scope: activeScope,
  });
  restoreRoute();

  document.querySelectorAll("[data-nav]").forEach((button) => {
    button.addEventListener("click", () => {
      const view = normalizeView(button.dataset.nav);
      if (window.location.hash === `#/${view}`) {
        showView(view);
      } else {
        window.location.hash = `/${view}`;
      }
    });
  });

  window.addEventListener("hashchange", restoreRoute);
  window.addEventListener("pagehide", destroyWorkbench, { once: true });

  for (const id of ["scope-tenant", "scope-kb", "scope-user", "scope-window"]) {
    document.getElementById(id)?.addEventListener("change", applyScope);
  }

  // scopechange is notification only; refresh is the sole business data-load trigger.
  document.getElementById("scope-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const scopeChanged = applyScope();
    if (!scopeChanged) scopeGuard.invalidate();
    const view = viewFromHash(window.location.hash);
    const snapshot = scopeGuard.snapshot();
    setStatus(`Refresh requested for ${view}.`);
    window.dispatchEvent(new CustomEvent("workbuddy:refresh", {
      detail: { scope: activeScope, view, ...snapshot },
    }));
  });
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeWorkbench, { once: true });
  } else {
    initializeWorkbench();
  }
}
