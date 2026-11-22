import en from "./en.mjs";
import zhCN from "./zh-CN.mjs";

const DEFAULT_LOCALE = "zh-CN";
const STORAGE_KEY = "workbuddy.locale";
const KNOWN_STATUSES = new Set([400, 401, 403, 404, 409, 422, 429, 500, 503]);
const DETAIL_SLUGS = {
  "Knowledge base not found": "knowledgeBaseNotFound",
};
const ATTRIBUTE_TARGETS = {
  "data-i18n": "textContent",
  "data-i18n-aria-label": "aria-label",
  "data-i18n-placeholder": "placeholder",
  "data-i18n-title": "title",
};

export const LOCALES = [
  { code: "zh-CN", label: "中文", dictionary: zhCN },
  { code: "en", label: "English", dictionary: en },
];

const DICTIONARIES = new Map(LOCALES.map(({ code, dictionary }) => [code, dictionary]));

let currentLocale = DEFAULT_LOCALE;
let initialized = false;
let warned = false;

function storage() {
  try {
    return globalThis.localStorage ?? null;
  } catch (_error) {
    return null;
  }
}

function warnUnsupported(value) {
  if (warned) return;
  warned = true;
  console.warn(`[i18n] Unsupported locale "${value}"; falling back to ${DEFAULT_LOCALE}.`);
}

export function normalizeLocale(value) {
  const raw = String(value ?? "").trim().toLowerCase();
  if (!raw) return null;
  if (raw === "zh" || raw.startsWith("zh-")) return "zh-CN";
  if (raw === "en" || raw.startsWith("en-")) return "en";
  return null;
}

export function resolveLocale({
  query = new URLSearchParams(globalThis.location?.search ?? "").get("lang"),
  stored = storage()?.getItem(STORAGE_KEY),
  fallback = DEFAULT_LOCALE,
} = {}) {
  for (const candidate of [query, stored]) {
    const normalized = normalizeLocale(candidate);
    if (normalized) return normalized;
  }
  if (String(query ?? stored ?? "").trim()) warnUnsupported(query ?? stored);
  return fallback;
}

export function getLocale() {
  return currentLocale;
}

export function setLocale(code, { persist = true, updateDocument = true } = {}) {
  const normalized = normalizeLocale(code) ?? DEFAULT_LOCALE;
  currentLocale = normalized;
  if (persist) storage()?.setItem(STORAGE_KEY, normalized);
  if (updateDocument && globalThis.document?.documentElement) {
    globalThis.document.documentElement.lang = normalized;
  }
  return normalized;
}

export function initLocale() {
  setLocale(initialized ? currentLocale : resolveLocale(), { persist: false });
  initialized = true;
  return currentLocale;
}

export function t(key, params = {}) {
  const dictionary = DICTIONARIES.get(currentLocale) ?? DICTIONARIES.get(DEFAULT_LOCALE);
  let value = dictionary?.[key] ?? en[key] ?? key;
  for (const [name, replacement] of Object.entries(params)) {
    value = value.split(`{${name}}`).join(String(replacement));
  }
  return value;
}

export const translate = t;

export function applyTranslations(root = globalThis.document) {
  if (!root?.querySelectorAll) return;
  for (const [marker, target] of Object.entries(ATTRIBUTE_TARGETS)) {
    for (const element of root.querySelectorAll(`[${marker}]`)) {
      const key = element.getAttribute(marker);
      if (!key) continue;
      const value = t(key);
      if (target === "textContent") element.textContent = value;
      else element.setAttribute(target, value);
    }
  }
}

export function formatError(error, fallbackKey = "errors.unexpected") {
  if (!error) return t(fallbackKey);
  const isApiError = error.name === "ApiError";
  const status = Number(error.status);
  const detail = typeof error.body?.detail === "string" ? error.body.detail : "";

  if (isApiError) {
    const slug = DETAIL_SLUGS[detail];
    if (slug) {
      const localized = t(`errors.detail.${slug}`);
      if (localized !== `errors.detail.${slug}`) return localized;
    }
    if (Number.isInteger(status) && KNOWN_STATUSES.has(status)) {
      const localized = t(`errors.http.${status}`);
      if (localized !== `errors.http.${status}`) {
        return detail ? `${localized} · ${detail}` : localized;
      }
    }
    return error.message || t(fallbackKey);
  }

  console.error("[i18n] unexpected error", error);
  return t(fallbackKey);
}
