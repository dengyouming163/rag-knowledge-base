import { apiJson } from "../api.mjs";
import { formatError, t } from "../i18n/index.mjs";

function scopeKey(scope = {}) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user, scope.window]);
}

function display(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export function createAudit({
  root,
  guard,
  scope = null,
  api = apiJson,
  window = globalThis.window,
  document = globalThis.document,
} = {}) {
  if (!root) {
    return { async loadAudit() { return false; }, setActive() {}, clear() {}, destroy() {} };
  }

  let active = false;
  let destroyed = false;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let operation = null;
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

  function renderRecords(records) {
    const items = records.map((record, index) => {
      const item = makeElement("li", "wb-audit-item");
      item.dataset.auditItem = display(record?.id ?? index + 1);
      item.tabIndex = 0;
      const heading = makeElement("div", "wb-audit-item-heading");
      const time = makeElement("time", "", display(record?.created_at));
      if (record?.created_at !== null && record?.created_at !== undefined && record?.created_at !== "") {
        time.dateTime = String(record.created_at);
      }
      heading.appendChild(time);
      heading.appendChild(makeElement(
        "span",
        `wb-audit-result${record?.result === "denied" ? " is-denied" : ""}`,
        display(record?.result),
      ));
      item.appendChild(heading);

      const fields = makeElement("dl", "wb-audit-fields");
      for (const field of ["actor", "action", "target", "result", "reason"]) {
        const wrapper = makeElement("div");
        wrapper.appendChild(makeElement("dt", "", t(`audit.field.${field}`)));
        const value = makeElement("dd", "", display(record?.[field]));
        value.dataset.auditField = field;
        wrapper.appendChild(value);
        fields.appendChild(wrapper);
      }
      item.appendChild(fields);
      return item;
    });
    byId("audit-timeline")?.replaceChildren(...items);
  }

  function operationIsCurrent(candidate) {
    return operation === candidate
      && contextIsCurrent(candidate.context)
      && !candidate.controller.signal.aborted;
  }

  function cancelAudit() {
    if (!operation) return;
    const previous = operation;
    operation = null;
    previous.unlink();
    previous.controller.abort();
    if (!destroyed) setText("#audit-status", t("audit.cancelled"));
  }

  function beginAudit(context) {
    if (!contextIsCurrent(context)) return null;
    cancelAudit();
    const controller = new AbortController();
    const parent = context.signal;
    const abort = () => controller.abort();
    if (parent) parent.addEventListener("abort", abort, { once: true });
    operation = {
      context,
      controller,
      unlink() { parent?.removeEventListener("abort", abort); },
    };
    return operation;
  }

  function finishAudit(candidate) {
    if (operation !== candidate) return false;
    operation = null;
    candidate.unlink();
    return true;
  }

  async function loadAudit(context = currentContext) {
    if (destroyed || !context?.scope) return false;
    currentContext = context;
    const candidate = beginAudit(context);
    if (!candidate) return false;
    byId("audit-timeline")?.replaceChildren();
    setText("#audit-status", t("audit.loading"));
    try {
      const data = await api("/v1/ops/audit?limit=50", {
        scope: context.scope,
        signal: candidate.controller.signal,
      });
      if (!operationIsCurrent(candidate)) return false;
      const records = (Array.isArray(data?.audit) ? data.audit : [])
        .map((record, index) => ({ record, index }))
        .sort((left, right) => {
          const leftTime = Date.parse(left.record?.created_at);
          const rightTime = Date.parse(right.record?.created_at);
          const leftValid = Number.isFinite(leftTime);
          const rightValid = Number.isFinite(rightTime);
          if (leftValid && rightValid && leftTime !== rightTime) return leftTime - rightTime;
          if (leftValid !== rightValid) return leftValid ? -1 : 1;
          return left.index - right.index;
        })
        .map(({ record }) => record);
      renderRecords(records);
      setText(
        "#audit-status",
        records.length ? t("audit.summary", { count: records.length }) : t("audit.empty"),
      );
      return true;
    } catch (error) {
      if (!operationIsCurrent(candidate)) return false;
      byId("audit-timeline")?.replaceChildren();
      setText("#audit-status", t("audit.unavailable", { error: formatError(error) }));
      return false;
    } finally {
      finishAudit(candidate);
    }
  }

  function onRootClick(event) {
    if (!destroyed && event.target?.closest?.("#audit-load")) void loadAudit();
  }

  function onRootKeydown(event) {
    if (destroyed || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const item = event.target?.closest?.("[data-audit-item]");
    if (!item) return;
    const items = [...root.querySelectorAll("[data-audit-item]")];
    const index = items.indexOf(item);
    const next = items[(index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
    if (!next) return;
    event.preventDefault();
    next.focus();
  }

  function onScopeChange(event) {
    if (destroyed) return;
    clear();
    if (event.detail?.scope) currentContext = event.detail;
  }

  function onRefresh(event) {
    if (destroyed) return;
    if (event.detail?.scope) currentContext = event.detail;
    if (event.detail?.view !== "audit" || !contextIsCurrent(currentContext)) return;
    void loadAudit(currentContext);
  }

  function onVisibilityChange() {
    if (!destroyed && document.visibilityState === "hidden") cancelAudit();
  }

  function setActive(value) {
    if (destroyed) return;
    active = Boolean(value);
    if (!active) cancelAudit();
  }

  function clear() {
    if (destroyed) return;
    cancelAudit();
    currentContext = null;
    byId("audit-timeline")?.replaceChildren();
    setText("#audit-status", t("audit.status"));
  }

  root.addEventListener("click", onRootClick);
  root.addEventListener("keydown", onRootKeydown);
  window?.addEventListener("workbuddy:scopechange", onScopeChange);
  window?.addEventListener("workbuddy:refresh", onRefresh);
  document?.addEventListener("visibilitychange", onVisibilityChange);

  return {
    loadAudit,
    setActive,
    clear,
    destroy() {
      if (destroyed) return;
      const previous = operation;
      operation = null;
      destroyed = true;
      active = false;
      if (previous) {
        previous.unlink();
        previous.controller.abort();
      }
      root.removeEventListener("click", onRootClick);
      root.removeEventListener("keydown", onRootKeydown);
      window?.removeEventListener("workbuddy:scopechange", onScopeChange);
      window?.removeEventListener("workbuddy:refresh", onRefresh);
      document?.removeEventListener("visibilitychange", onVisibilityChange);
    },
  };
}
