import test from "node:test";
import assert from "node:assert/strict";

import { setLocale } from "../../app/static/js/i18n/index.mjs";
import {
  formatBytes,
  statusLabel,
  statusClass,
  renderRows,
} from "../../app/static/js/views/storage.mjs";

test.beforeEach(() => {
  setLocale("en", { persist: false });
});

test("formatBytes handles bytes, kb, mb and missing", () => {
  assert.equal(formatBytes(0), "—");
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.0 MB");
});

test("statusLabel maps known statuses and passes unknown through", () => {
  setLocale("en", { persist: false });
  assert.equal(statusLabel("ready"), "Ready");
  assert.equal(statusLabel("pending"), "Processing");
  assert.equal(statusLabel("failed"), "Failed");
  assert.equal(statusLabel("weird"), "weird");
});

test("statusClass maps ready/pending/failed, empty otherwise", () => {
  assert.equal(statusClass("ready"), "ok");
  assert.equal(statusClass("pending"), "busy");
  assert.equal(statusClass("failed"), "bad");
  assert.equal(statusClass("x"), "");
});

test("renderRows builds table rows with filename, size, status", () => {
  const html = renderRows([
    { id: "d1", kb_id: "kb1", filename: "政策.docx", size: 82000, status: "ready" },
    { id: "d2", kb_id: "kb1", filename: "坏文件.pdf", size: null, status: "failed" },
  ]);
  assert.match(html, /政策\.docx/);
  assert.match(html, /Ready/);
  assert.match(html, /Failed/);
  assert.match(html, /—/);
  assert.doesNotMatch(html, /weird/);
});

test("renderRows empty list returns empty string", () => {
  assert.equal(renderRows([]), "");
});
