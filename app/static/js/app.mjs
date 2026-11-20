import { ScopeGuard } from "./core.mjs";

const VIEW_NAMES = ["overview", "lab", "traces", "ingestion", "evaluation", "access", "audit"];
const scopeGuard = new ScopeGuard();
let activeScope = null;

function normalizeView(name) {
  return VIEW_NAMES.includes(name) ? name : "overview";
}

export function viewFromHash(hash) {
  const requested = String(hash ?? "").replace(/^#\/?/, "").split(/[/?]/)[0];
  return normalizeView(requested);
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
    view.querySelectorAll("[data-view-body]").forEach((body) => body.replaceChildren());
    view.querySelectorAll(".is-selected").forEach((item) => item.classList.remove("is-selected"));
    view.querySelectorAll("[aria-selected]").forEach((item) => item.setAttribute("aria-selected", "false"));
    view.querySelectorAll("[data-selected]").forEach((item) => item.removeAttribute("data-selected"));
  });
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
  const canonicalHash = `#/${view}`;
  if (window.location.hash !== canonicalHash) {
    window.history.replaceState(null, "", canonicalHash);
  }
  showView(view);
  return view;
}

function initializeWorkbench() {
  activeScope = readScope();
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

  for (const id of ["scope-tenant", "scope-kb", "scope-user", "scope-window"]) {
    document.getElementById(id)?.addEventListener("change", applyScope);
  }

  document.getElementById("scope-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (applyScope()) return;
    scopeGuard.invalidate();
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
