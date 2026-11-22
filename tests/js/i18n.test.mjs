import test from "node:test";
import assert from "node:assert/strict";

import zhCN from "../../app/static/js/i18n/zh-CN.mjs";
import en from "../../app/static/js/i18n/en.mjs";
import {
  applyTranslations,
  formatError,
  getLocale,
  LOCALES,
  normalizeLocale,
  resolveLocale,
  setLocale,
  t,
} from "../../app/static/js/i18n/index.mjs";

function importFreshI18n(name) {
  const url = new URL("../../app/static/js/i18n/index.mjs", import.meta.url);
  url.searchParams.set("test", name);
  return import(url.href);
}

test("storage read failures do not block resolution or initialization", async () => {
  const fresh = await importFreshI18n("storage-read");
  const documentElement = { lang: "" };
  globalThis.localStorage = {
    getItem() { throw new Error("storage read blocked"); },
  };
  globalThis.location = { search: "" };
  globalThis.document = { documentElement };
  try {
    assert.equal(resolveLocale({ query: "en" }), "en");
    assert.equal(fresh.initLocale(), "zh-CN");
    assert.equal(documentElement.lang, "zh-CN");
  } finally {
    delete globalThis.localStorage;
    delete globalThis.location;
    delete globalThis.document;
    setLocale("zh-CN", { persist: false });
  }
});

test("storage write failures do not block locale or document updates", () => {
  const documentElement = { lang: "zh-CN" };
  globalThis.localStorage = {
    setItem() { throw new Error("storage write blocked"); },
  };
  globalThis.document = { documentElement };
  try {
    setLocale("zh-CN", { persist: false });
    assert.doesNotThrow(() => setLocale("en"));
    assert.equal(getLocale(), "en");
    assert.equal(documentElement.lang, "en");
  } finally {
    delete globalThis.localStorage;
    delete globalThis.document;
    setLocale("zh-CN", { persist: false });
  }
});

test("normalizeLocale maps regional variants", () => {
  assert.equal(normalizeLocale("zh"), "zh-CN");
  assert.equal(normalizeLocale("zh-Hans-CN"), "zh-CN");
  assert.equal(normalizeLocale("en-US"), "en");
  assert.equal(normalizeLocale("EN"), "en");
  assert.equal(normalizeLocale("fr"), null);
  assert.equal(normalizeLocale(""), null);
});

test("normalizeLocale derives aliases from registered locales", () => {
  LOCALES.push({ code: "fr-CA", label: "Français", dictionary: {} });
  try {
    assert.equal(normalizeLocale("fr"), "fr-CA");
    assert.equal(normalizeLocale("fr-FR"), "fr-CA");
    assert.equal(normalizeLocale("FR-ca"), "fr-CA");
  } finally {
    LOCALES.pop();
  }
});

test("resolveLocale prefers query, then storage, then default", () => {
  assert.equal(resolveLocale({ query: "en", stored: "zh-CN" }), "en");
  assert.equal(resolveLocale({ query: null, stored: "en" }), "en");
  assert.equal(resolveLocale({ query: "", stored: "" }), "zh-CN");
  assert.equal(resolveLocale({ query: "fr", stored: "en" }), "en");
});

test("setLocale persists only when asked", () => {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (key) => store.get(key) ?? null,
    setItem: (key, value) => store.set(key, String(value)),
  };
  try {
    setLocale("en", { persist: false });
    assert.equal(store.has("workbuddy.locale"), false);
    setLocale("en");
    assert.equal(store.get("workbuddy.locale"), "en");
    assert.equal(getLocale(), "en");
  } finally {
    delete globalThis.localStorage;
    setLocale("zh-CN", { persist: false });
  }
});

test("t falls back from missing zh key to en and then to the key", () => {
  setLocale("zh-CN", { persist: false });
  assert.equal(t("nav.overview"), "概览");
  assert.equal(t("errors.http.404"), "资源不存在");
  assert.equal(t("missing.key"), "missing.key");
  setLocale("en", { persist: false });
  assert.equal(t("missing.key"), "missing.key");
});

test("t falls back to en when the active zh-CN dictionary lacks a key", () => {
  const original = zhCN["nav.overview"];
  delete zhCN["nav.overview"];
  setLocale("zh-CN", { persist: false });
  try {
    assert.equal(t("nav.overview"), "Overview");
  } finally {
    zhCN["nav.overview"] = original;
  }
});

test("t ignores inherited dictionary properties", () => {
  setLocale("zh-CN", { persist: false });
  assert.equal(t("constructor"), "constructor");
  assert.equal(t("__proto__"), "__proto__");
});

test("t interpolates named parameters and keeps missing ones", () => {
  setLocale("zh-CN", { persist: false });
  assert.equal(t("lab.search.complete", { count: 2, mode: "hybrid" }), "检索完成 · 2 条命中 · hybrid");
  assert.equal(t("lab.search.complete", { count: 2 }), "检索完成 · 2 条命中 · {mode}");
});

test("t keeps inherited parameter names as missing placeholders", () => {
  const original = zhCN["lab.search.complete"];
  zhCN["lab.search.complete"] = "{constructor}";
  setLocale("zh-CN", { persist: false });
  try {
    assert.equal(t("lab.search.complete", {}), "{constructor}");
  } finally {
    zhCN["lab.search.complete"] = original;
  }
});

test("t does not reprocess placeholders inside replacement values", () => {
  setLocale("zh-CN", { persist: false });
  assert.equal(
    t("lab.search.complete", { count: "{mode}", mode: "hybrid" }),
    "检索完成 · {mode} 条命中 · hybrid",
  );
});

test("dictionaries expose the same key set", () => {
  assert.deepEqual(Object.keys(zhCN).sort(), Object.keys(en).sort());
});

test("dictionaries use matching placeholder sets for every key", () => {
  const placeholders = (value) => [...new Set(
    [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]),
  )].sort();
  for (const key of Object.keys(en)) {
    assert.deepEqual(placeholders(zhCN[key]), placeholders(en[key]), key);
  }
});

test("applyTranslations updates text and attributes idempotently", () => {
  setLocale("en", { persist: false });
  const map = {
    "[data-i18n]": [{ getAttribute: () => "nav.overview", textContent: "" }],
    "[data-i18n-aria-label]": [{
      getAttribute: () => "topbar.language",
      setAttribute(name, value) { this[name] = value; },
    }],
    "[data-i18n-placeholder]": [{
      getAttribute: () => "topbar.language",
      setAttribute(name, value) { this[name] = value; },
    }],
    "[data-i18n-title]": [{
      getAttribute: () => "app.title",
      setAttribute(name, value) { this[name] = value; },
    }],
  };
  const root = {
    querySelectorAll(selector) {
      return map[selector] ?? [];
    },
  };
  applyTranslations(root);
  assert.equal(root.querySelectorAll("[data-i18n]")[0].textContent, "Overview");
  assert.equal(root.querySelectorAll("[data-i18n-aria-label]")[0]["aria-label"], "Language");
  assert.equal(root.querySelectorAll("[data-i18n-placeholder]")[0].placeholder, "Language");
  assert.equal(root.querySelectorAll("[data-i18n-title]")[0].title, "RAG Operations Console");
  applyTranslations(root);
  assert.equal(root.querySelectorAll("[data-i18n]")[0].textContent, "Overview");
});

test("initLocale resolves the first locale and updates document language", async () => {
  const fresh = await importFreshI18n("init-locale");
  const documentElement = { lang: "zh-CN" };
  globalThis.location = { search: "?lang=en" };
  globalThis.localStorage = { getItem: () => "zh-CN" };
  globalThis.document = { documentElement };
  try {
    assert.equal(fresh.initLocale(), "en");
    assert.equal(fresh.getLocale(), "en");
    assert.equal(documentElement.lang, "en");
  } finally {
    delete globalThis.localStorage;
    delete globalThis.location;
    delete globalThis.document;
    setLocale("zh-CN", { persist: false });
  }
});

test("formatError maps status and detail with passthrough fallback", () => {
  setLocale("en", { persist: false });
  const apiError = (status, detail) => ({
    name: "ApiError",
    status,
    message: `HTTP ${status}: ${detail}`,
    body: { detail },
  });
  assert.equal(formatError(apiError(404, "Knowledge base not found")), "Knowledge base not found");
  assert.equal(formatError(apiError(404, "something odd")), "Not found · something odd");
  assert.equal(formatError(apiError(418, "teapot")), "HTTP 418: teapot");
  assert.equal(formatError(new Error("boom")), "Request failed. Please try again.");
});
