export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

export function formatMetric(value, unit = "", digits = 1) {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  const formatted = numeric.toFixed(digits);
  return unit ? `${formatted} ${unit}` : formatted;
}

export class ScopeGuard {
  #epoch = 0;
  #controller = new AbortController();

  snapshot() {
    return { epoch: this.#epoch, signal: this.#controller.signal };
  }

  invalidate() {
    this.#epoch += 1;
    this.#controller.abort();
    this.#controller = new AbortController();
  }

  isCurrent(snapshot) {
    return snapshot?.epoch === this.#epoch;
  }
}
