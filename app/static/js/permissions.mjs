import { apiJson } from "./api.mjs";

function scopeKey(scope = {}) {
  return JSON.stringify([scope.tenant, scope.kb, scope.user]);
}

function errorMessage(error) {
  const detail = error?.body?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") return detail.message;
  return error instanceof Error && error.message ? error.message : "Request failed";
}

function roleOf(snapshot) {
  return String(snapshot?.role || (snapshot?.is_admin ? "admin" : "guest"));
}

export function permissionReason({ identity, permissions, identityError, permissionError, loading } = {}) {
  if (loading) return "Checking identity and knowledge-base permissions…";
  if (identityError) return `Identity unavailable: ${identityError}. Actions remain disabled until identity can be verified.`;
  if (permissionError) return `Knowledge-base permissions unavailable: ${permissionError}. Actions remain disabled for this knowledge base.`;
  if (!identity || !permissions) return "Identity and knowledge-base permissions have not been loaded.";
  if (identity.is_admin !== true || permissions.is_admin !== true) {
    return `Administrator role required (current role: ${roleOf(identity)}).`;
  }
  if (identity.ops?.actions_enabled !== true || identity.ops?.can_trigger !== true) {
    return "Operations actions are disabled by OPS_ENABLE_ACTIONS.";
  }
  return "Administrator actions are enabled for the current tenant, user, and knowledge base.";
}

export function createPermissions({
  guard,
  scope = null,
  api = apiJson,
} = {}) {
  let destroyed = false;
  let currentContext = scope ? { scope, ...guard?.snapshot() } : null;
  let cache = null;
  let operation = null;
  const activeLeases = new Set();
  const listeners = new Set();

  function notify() {
    if (destroyed) return;
    const snapshot = state();
    for (const listener of listeners) listener(snapshot);
  }

  function emptyCache(context) {
    return {
      key: scopeKey(context.scope),
      epoch: context.epoch,
      identity: null,
      permissions: null,
      identityError: "",
      permissionError: "",
      identitySettled: false,
      permissionSettled: false,
    };
  }

  function state() {
    const snapshot = cache;
    const loading = Boolean(operation) || Boolean(snapshot && (!snapshot.identitySettled || !snapshot.permissionSettled));
    const reason = permissionReason({
      identity: snapshot?.identity,
      permissions: snapshot?.permissions,
      identityError: snapshot?.identityError,
      permissionError: snapshot?.permissionError,
      loading,
    });
    const actions = Boolean(
      !loading
      && snapshot?.identity
      && snapshot?.permissions
      && !snapshot.identityError
      && !snapshot.permissionError
      && snapshot.identity.is_admin === true
      && snapshot.permissions.is_admin === true
      && snapshot.identity.ops?.actions_enabled === true
      && snapshot.identity.ops?.can_trigger === true
    );
    return {
      identity: snapshot?.identity ?? null,
      permissions: snapshot?.permissions ?? null,
      identityError: snapshot?.identityError ?? "",
      permissionError: snapshot?.permissionError ?? "",
      identityLoading: Boolean(operation && snapshot && !snapshot.identitySettled),
      permissionLoading: Boolean(operation && snapshot && !snapshot.permissionSettled),
      loading,
      canActions: actions,
      canCompare: actions,
      reason,
    };
  }

  function current(operationSnapshot) {
    return !destroyed
      && operation === operationSnapshot
      && !operationSnapshot.controller.signal.aborted
      && (!guard || guard.isCurrent(operationSnapshot.context));
  }

  function cancel() {
    if (!operation) return;
    const previous = operation;
    operation = null;
    previous.unlink();
    previous.controller.abort();
  }

  function retain(lease) {
    if (destroyed || !lease) return;
    activeLeases.add(lease);
    if (operation) {
      operation.managed = true;
      operation.leases.add(lease);
    }
  }

  function release(lease) {
    if (destroyed || !lease) return;
    activeLeases.delete(lease);
    if (!operation) return;
    operation.leases.delete(lease);
    if (operation.managed && operation.leases.size === 0) cancel();
  }

  function clear() {
    if (destroyed) return;
    cancel();
    cache = null;
    currentContext = null;
    notify();
  }

  async function load(context = currentContext) {
    if (destroyed || !context?.scope?.tenant || !context.scope?.kb || !context.scope?.user) return state();
    if (context.signal?.aborted || (guard && !guard.isCurrent(context))) return state();
    const key = scopeKey(context.scope);
    if (cache && (cache.key !== key || cache.epoch !== context.epoch)) {
      cancel();
      cache = null;
    }
    if (operation && operation.context.epoch === context.epoch && scopeKey(operation.context.scope) === key) {
      return operation.promise;
    }
    if (cache?.key === key && cache.epoch === context.epoch && cache.identitySettled && cache.permissionSettled) {
      return state();
    }

    cancel();
    currentContext = context;
    cache = emptyCache(context);
    const controller = new AbortController();
    const parent = context.signal;
    const abort = () => controller.abort();
    if (parent) parent.addEventListener("abort", abort, { once: true });
    const operationSnapshot = {
      context,
      controller,
      promise: null,
      leases: new Set(activeLeases),
      managed: activeLeases.size > 0,
      unlink() { parent?.removeEventListener("abort", abort); },
    };
    operation = operationSnapshot;
    notify();
    const cacheSnapshot = cache;
    const options = { scope: context.scope, signal: controller.signal };
    const query = new URLSearchParams({ kb_id: context.scope.kb });

    const identityRequest = Promise.resolve()
      .then(() => api("/v1/identity", options))
      .then((identity) => {
        if (!current(operationSnapshot) || cache !== cacheSnapshot) return;
        cacheSnapshot.identity = identity;
      })
      .catch((error) => {
        if (!current(operationSnapshot) || cache !== cacheSnapshot) return;
        cacheSnapshot.identityError = errorMessage(error);
      })
      .finally(() => {
        if (current(operationSnapshot) && cache === cacheSnapshot) {
          cacheSnapshot.identitySettled = true;
          notify();
        }
      });
    const permissionRequest = Promise.resolve()
      .then(() => api(`/v1/ops/permissions?${query.toString()}`, options))
      .then((permissions) => {
        if (!current(operationSnapshot) || cache !== cacheSnapshot) return;
        cacheSnapshot.permissions = permissions;
      })
      .catch((error) => {
        if (!current(operationSnapshot) || cache !== cacheSnapshot) return;
        cacheSnapshot.permissionError = errorMessage(error);
      })
      .finally(() => {
        if (current(operationSnapshot) && cache === cacheSnapshot) {
          cacheSnapshot.permissionSettled = true;
          notify();
        }
      });

    operationSnapshot.promise = Promise.all([identityRequest, permissionRequest]).then(() => {
      if (current(operationSnapshot)) {
        operationSnapshot.unlink();
        operation = null;
        notify();
      }
      return state();
    });
    return operationSnapshot.promise;
  }

  return {
    load,
    retain,
    release,
    clear,
    getState: state,
    subscribe(listener) {
      if (destroyed || typeof listener !== "function") return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    canActions() { return state().canActions; },
    canRetry(job) { return state().canActions && job?.status === "failed"; },
    canCompare() { return state().canCompare; },
    actionReason() { return state().reason; },
    retryReason(job) {
      if (job?.status !== "failed") return "Only failed jobs can be retried.";
      return state().reason;
    },
    compareReason() { return state().reason; },
    destroy() {
      if (destroyed) return;
      clear();
      destroyed = true;
      activeLeases.clear();
      listeners.clear();
    },
  };
}
