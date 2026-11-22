import { apiJson } from "../api.mjs";
import { formatError, t } from "../i18n/index.mjs";

function scopeKey(scope = {}) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user, scope.window]);
}

function display(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function listDisplay(value) {
  return Array.isArray(value) && value.length ? value.map((item) => String(item)).join(", ") : "—";
}

export function createAccess({
  root,
  guard,
  scope = null,
  permissions = null,
  api = apiJson,
  window = globalThis.window,
  document = globalThis.document,
} = {}) {
  if (!root) {
    return {
      async loadWorkspace() { return false; },
      async compareIdentities() { return false; },
      setActive() {}, clear() {}, destroy() {},
    };
  }

  let active = false;
  let destroyed = false;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let compareOperation = null;
  let permissionRetained = false;
  const permissionLease = {};
  const byId = (id) => root.querySelector(`#${id}`);
  const setText = (selector, value) => {
    const element = root.querySelector(selector);
    if (element) element.textContent = String(value ?? "");
  };
  const makeElement = (tag, className = "", text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const contextIsCurrent = (context) => !destroyed
    && active
    && document?.visibilityState !== "hidden"
    && currentContext
    && scopeKey(context?.scope) === scopeKey(currentContext.scope)
    && (!guard || guard.isCurrent(context));

  function retainPermissions() {
    if (destroyed || permissionRetained || !active || document?.visibilityState === "hidden") return;
    permissions?.retain?.(permissionLease);
    permissionRetained = true;
  }

  function releasePermissions() {
    if (!permissionRetained) return;
    permissionRetained = false;
    permissions?.release?.(permissionLease);
  }

  function renderComparison(rows) {
    const cards = rows.map((row) => {
      const card = makeElement("article", "wb-compare-user");
      card.dataset.compareUser = display(row?.user);
      const heading = makeElement("div", "wb-compare-user-heading");
      heading.appendChild(makeElement("h3", "", display(row?.user)));
      heading.appendChild(makeElement("span", "", display(row?.role)));
      card.appendChild(heading);
      card.appendChild(makeElement(
        "p",
        "wb-compare-user-meta",
        t("access.compare.tags", { tags: listDisplay(row?.tags) }),
      ));
      const hits = makeElement("p", "wb-compare-hits-line", t("access.compare.hits"));
      hits.appendChild(makeElement("strong", "", display(row?.hits)));
      hits.querySelector("strong").dataset.compareHits = "";
      card.appendChild(hits);
      const evidence = makeElement(
        "p",
        "wb-compare-user-meta",
        t("access.compare.evidence", {
          score: display(row?.top_score),
          latency: display(row?.latency_ms),
        }),
      );
      card.appendChild(evidence);
      const chunkIds = Array.isArray(row?.visible_chunk_ids) ? row.visible_chunk_ids : [];
      if (!chunkIds.length) {
        card.appendChild(makeElement("p", "wb-compare-empty", t("access.compare.noChunks")));
      } else {
        const list = makeElement("ul", "wb-compare-chunks");
        list.setAttribute(
          "aria-label",
          t("access.compare.chunkListAria", { user: display(row?.user) }),
        );
        for (const chunkId of chunkIds) {
          const item = makeElement("li");
          const code = makeElement("code", "", chunkId);
          code.dataset.compareChunkId = "";
          item.appendChild(code);
          list.appendChild(item);
        }
        card.appendChild(list);
      }
      return card;
    });
    byId("access-compare-results")?.replaceChildren(...cards);
  }

  function renderPermissionState() {
    if (destroyed) return;
    const state = permissions?.getState?.() ?? {
      identity: null,
      permissions: null,
      identityError: "",
      permissionError: "",
      identityLoading: false,
      permissionLoading: false,
    };
    const identity = state.identity;
    const kbPermissions = state.permissions;
    if (state.identityError) {
      setText("#access-identity-status", t("access.identityUnavailable", { error: state.identityError }));
    } else if (identity) {
      setText("#access-identity-status", t("access.identityLoaded"));
    } else if (state.identityLoading) {
      setText("#access-identity-status", t("access.identityLoading"));
    } else {
      setText("#access-identity-status", t("access.identityStatus"));
    }
    const identityFields = {
      tenant: display(identity?.tenant),
      user: display(identity?.user),
      role: display(identity?.role),
      tags: listDisplay(identity?.tags),
      kbs: listDisplay(identity?.kbs),
    };
    for (const [field, value] of Object.entries(identityFields)) {
      setText(`[data-access-field="${field}"]`, value);
    }

    if (state.permissionError) {
      setText(
        "#access-kb-permission-status",
        t("access.kbPermissionUnavailable", { error: state.permissionError }),
      );
    } else if (kbPermissions) {
      setText("#access-kb-permission-status", t("access.kbPermissionLoaded"));
    } else if (state.permissionLoading) {
      setText("#access-kb-permission-status", t("access.kbPermissionLoading"));
    } else {
      setText("#access-kb-permission-status", t("access.kbPermissionStatus"));
    }
    const kbFields = {
      kb: display(kbPermissions?.kb?.id),
      decision: display(kbPermissions?.decision),
      scoped_count: display(kbPermissions?.rls?.scoped_count),
      bare_count: display(kbPermissions?.rls?.bare_count),
    };
    for (const [field, value] of Object.entries(kbFields)) {
      setText(`[data-access-kb-field="${field}"]`, value);
    }
    setText("#access-permission-reason", display(kbPermissions?.reason));

    const tenantDetail = t("access.layer.tenantDetail", {
      tenant: display(identity?.tenant ?? kbPermissions?.tenant),
      scope: display(identity?.isolation?.tenant_scope),
      match: display(kbPermissions?.kb?.tenant_match),
    });
    const kbDetail = t("access.layer.kbDetail", {
      kb: display(kbPermissions?.kb?.id),
      decision: display(kbPermissions?.decision),
      scoped: display(kbPermissions?.rls?.scoped_count),
      bare: display(kbPermissions?.rls?.bare_count),
      enabled: display(kbPermissions?.rls?.enabled ?? identity?.isolation?.rls?.enabled),
    });
    const userDetail = t("access.layer.userDetail", {
      user: display(identity?.user ?? kbPermissions?.user),
      role: display(identity?.role ?? kbPermissions?.role),
      acl: display(kbPermissions?.acl?.mode ?? identity?.isolation?.acl),
      tags: listDisplay(kbPermissions?.acl?.user_tags ?? identity?.tags),
    });
    setText('[data-access-layer="tenant"] [data-access-layer-detail]', tenantDetail);
    setText('[data-access-layer="kb"] [data-access-layer-detail]', kbDetail);
    setText('[data-access-layer="user"] [data-access-layer-detail]', userDetail);

    const reason = permissions?.compareReason?.() ?? t("access.actionAvailability");
    setText("#access-permission-status", reason);
    const submit = byId("access-compare-submit");
    if (submit) {
      submit.disabled = Boolean(compareOperation) || (permissions ? !permissions.canCompare() : false);
      submit.title = compareOperation
        ? t("access.compare.inProgress")
        : submit.disabled ? reason : t("access.compare.submitTitle");
    }
  }

  function operationIsCurrent(operation) {
    return compareOperation === operation
      && contextIsCurrent(operation.context)
      && !operation.controller.signal.aborted;
  }

  function cancelComparison() {
    const operation = compareOperation;
    if (!operation) return;
    compareOperation = null;
    operation.unlink();
    operation.controller.abort();
    if (!destroyed) {
      setText("#access-compare-status", t("access.compare.cancelled"));
      renderPermissionState();
    }
  }

  function beginComparison(context) {
    if (!contextIsCurrent(context)) return null;
    cancelComparison();
    const controller = new AbortController();
    const parent = context.signal;
    const abort = () => controller.abort();
    if (parent) parent.addEventListener("abort", abort, { once: true });
    compareOperation = {
      context,
      controller,
      unlink() { parent?.removeEventListener("abort", abort); },
    };
    return compareOperation;
  }

  function finishComparison(operation) {
    if (compareOperation !== operation) return false;
    compareOperation = null;
    operation.unlink();
    return true;
  }

  function clearValidation() {
    for (const id of ["access-compare-query", "access-compare-user-ids", "access-compare-top-k"]) {
      byId(id)?.removeAttribute("aria-invalid");
    }
    const error = byId("access-compare-error");
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
  }

  function showValidationError(id, message) {
    clearValidation();
    const input = byId(id);
    input?.setAttribute("aria-invalid", "true");
    const error = byId("access-compare-error");
    if (error) {
      error.hidden = false;
      error.textContent = message;
    }
    input?.focus();
    return null;
  }

  function validComparisonInput() {
    const query = byId("access-compare-query")?.value.trim() ?? "";
    if (query.length < 1 || query.length > 500) {
      return showValidationError("access-compare-query", t("access.validation.query"));
    }
    const userIds = (byId("access-compare-user-ids")?.value ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    if (userIds.length < 1 || userIds.length > 3) {
      return showValidationError("access-compare-user-ids", t("access.validation.userIds"));
    }
    const rawTopK = byId("access-compare-top-k")?.value ?? "";
    const topK = Number(rawTopK);
    if (!rawTopK || !Number.isInteger(topK) || topK < 1 || topK > 10) {
      return showValidationError("access-compare-top-k", t("access.validation.topK"));
    }
    clearValidation();
    return { query, user_ids: userIds, top_k: topK };
  }

  async function compareIdentities(context = currentContext) {
    if (destroyed) return false;
    const body = validComparisonInput();
    if (!body) return false;
    if (permissions && !permissions.canCompare()) {
      setText("#access-compare-status", permissions.compareReason());
      renderPermissionState();
      return false;
    }
    const operation = beginComparison(context);
    if (!operation) return false;
    byId("access-compare-results")?.replaceChildren();
    setText("#access-compare-status", t("access.compare.comparing"));
    renderPermissionState();
    try {
      const data = await api(`/v1/ops/kbs/${encodeURIComponent(context.scope.kb)}/identity-compare`, {
        method: "POST",
        body,
        scope: context.scope,
        signal: operation.controller.signal,
      });
      if (!operationIsCurrent(operation)) return false;
      const rows = Array.isArray(data?.users) ? data.users : [];
      renderComparison(rows);
      setText(
        "#access-compare-status",
        rows.length
          ? t(rows.length === 1 ? "access.compare.summaryOne" : "access.compare.summary", {
            count: rows.length,
          })
          : t("access.compare.empty"),
      );
      return true;
    } catch (error) {
      if (!operationIsCurrent(operation)) return false;
      byId("access-compare-results")?.replaceChildren();
      setText(
        "#access-compare-status",
        t("access.compare.unavailable", { error: formatError(error) }),
      );
      return false;
    } finally {
      if (finishComparison(operation) && contextIsCurrent(operation.context)) renderPermissionState();
    }
  }

  async function loadWorkspace(context = currentContext) {
    if (destroyed || !context?.scope) return false;
    currentContext = context;
    if (!contextIsCurrent(context)) return false;
    renderPermissionState();
    const request = permissions?.load(context) ?? Promise.resolve(null);
    const result = await request;
    if (contextIsCurrent(context)) renderPermissionState();
    return Boolean(result ?? permissions);
  }

  function onRootClick(event) {
    if (!destroyed && event.target?.closest?.("#access-load")) void loadWorkspace();
  }

  function onRootSubmit(event) {
    if (destroyed || !event.target?.matches?.("#access-compare-form")) return;
    event.preventDefault();
    void compareIdentities();
  }

  function onScopeChange(event) {
    if (destroyed) return;
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onRefresh(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    if (event.detail?.view !== "access" || !contextIsCurrent(currentContext)) return;
    cancelComparison();
    void loadWorkspace(currentContext);
  }

  function onVisibilityChange() {
    if (destroyed) return;
    if (document.visibilityState === "hidden") {
      cancelComparison();
      releasePermissions();
    } else {
      retainPermissions();
    }
  }

  function setActive(value) {
    if (destroyed) return;
    active = Boolean(value);
    if (active) retainPermissions();
    else {
      cancelComparison();
      releasePermissions();
    }
    renderPermissionState();
  }

  function clear() {
    if (destroyed) return;
    cancelComparison();
    currentContext = null;
    byId("access-compare-form")?.reset();
    clearValidation();
    byId("access-compare-results")?.replaceChildren();
    setText("#access-compare-status", t("access.compareStatus"));
    renderPermissionState();
    setText("#access-identity-status", t("access.identityScopeChanged"));
    setText("#access-kb-permission-status", t("access.kbScopeChanged"));
    setText("#access-permission-status", t("access.permissionStatus"));
  }

  root.addEventListener("click", onRootClick);
  root.addEventListener("submit", onRootSubmit);
  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  document?.addEventListener("visibilitychange", onVisibilityChange);
  const unsubscribe = permissions?.subscribe?.(() => {
    if (active) renderPermissionState();
  }) ?? (() => {});
  renderPermissionState();

  return {
    loadWorkspace,
    compareIdentities,
    setActive,
    clear,
    destroy() {
      if (destroyed) return;
      const operation = compareOperation;
      compareOperation = null;
      destroyed = true;
      active = false;
      if (operation) {
        operation.unlink();
        operation.controller.abort();
      }
      releasePermissions();
      unsubscribe();
      root.removeEventListener("click", onRootClick);
      root.removeEventListener("submit", onRootSubmit);
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
    },
  };
}
