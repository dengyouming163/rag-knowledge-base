import test from "node:test";
import assert from "node:assert/strict";

import zhCN from "../../app/static/js/i18n/zh-CN.mjs";
import en from "../../app/static/js/i18n/en.mjs";
import {
  applyTranslations,
  formatError,
  getLocale,
  normalizeLocale,
  resolveLocale,
  setLocale,
  t,
} from "../../app/static/js/i18n/index.mjs";

test("normalizeLocale maps regional variants", () => {
  assert.equal(normalizeLocale("zh"), "zh-CN");
  assert.equal(normalizeLocale("zh-Hans-CN"), "zh-CN");
  assert.equal(normalizeLocale("en-US"), "en");
  assert.equal(normalizeLocale("EN"), "en");
  assert.equal(normalizeLocale("fr"), null);
  assert.equal(normalizeLocale(""), null);
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

test("t interpolates named parameters and keeps missing ones", () => {
  setLocale("zh-CN", { persist: false });
  assert.equal(t("lab.search.complete", { count: 2, mode: "hybrid" }), "检索完成 · 2 条命中 · hybrid");
  assert.equal(t("lab.search.complete", { count: 2 }), "检索完成 · 2 条命中 · {mode}");
});

test("dictionaries expose the same key set", () => {
  assert.deepEqual(Object.keys(zhCN).sort(), Object.keys(en).sort());
});

test("applyTranslations updates text and attributes idempotently", () => {
  setLocale("en", { persist: false });
  const map = {
    "[data-i18n]": [{ getAttribute: () => "nav.overview", textContent: "" }],
    "[data-i18n-aria-label]": [{
      getAttribute: () => "topbar.language",
      setAttribute(name, value) { this[name] = value; },
    }],
    "[data-i18n-placeholder]": [],
    "[data-i18n-title]": [],
  };
  const root = {
    querySelectorAll(selector) {
      return map[selector] ?? [];
    },
  };
  applyTranslations(root);
  assert.equal(root.querySelectorAll("[data-i18n]")[0].textContent, "Overview");
  assert.equal(root.querySelectorAll("[data-i18n-aria-label]")[0]["aria-label"], "Language");
  applyTranslations(root);
  assert.equal(root.querySelectorAll("[data-i18n]")[0].textContent, "Overview");
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
