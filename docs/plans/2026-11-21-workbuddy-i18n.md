# WorkBuddy 控制台多语言（中文 / 英文）实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 `/ui` 控制台提供默认中文、可切英文的双语界面，并保持零构建、可扩展第三语种。

**Architecture:** 新增 `app/static/js/i18n/` 模块，包含语言注册表、`t()` 取词、`applyTranslations()` 静态 DOM 替换与 `formatError()` 错误映射；`index.html` 文本节点加 `data-i18n` 标注；视图模块的动态文案改走 `t()`；顶栏下拉切换语言并触发既有 `workbuddy:refresh` 重拉当前视图。

**Tech Stack:** 原生 ES modules、Playwright（Python）、Node 内置 `node:test`、pytest。

**Spec:** `docs/superpowers/specs/2026-11-21-workbuddy-i18n-design.md`

**执行约定：**

- 在 `projects/rag-knowledge-base` 当前 `main` 分支工作，不新建 worktree。
- 严格 TDD：先写失败测试，再写实现。
- 每个 Task 结束后跑相关测试并单独提交；commit message 用英文。
- 只 `git add` 本 Task 涉及的文件，不改不暂存受保护文件（`src/`、`deploy/`、`eval/`、`scripts/calibrate_refusal.py`、`scripts/run_eval.py`、`scripts/make_pdf_fixtures.py`、`tests/test_api_search.py`、`tests/test_pipeline.py`、`tests/test_eval_generate.py`、`tests/test_pdf_parser.py`、`docs/plans/2026-11-20-workbuddy-observability.md`）。
- 不 amend、不 push。

---

### Task 1: i18n 核心模块与字典骨架

**Files:**
- Create: `app/static/js/i18n/zh-CN.mjs`
- Create: `app/static/js/i18n/en.mjs`
- Create: `app/static/js/i18n/index.mjs`
- Create: `tests/js/i18n.test.mjs`
- Modify: `tests/js/*.test.mjs`（每个现有测试文件顶部加一行 `setLocale("en", { persist: false })`）

**Step 1: 写失败测试**

创建 `tests/js/i18n.test.mjs`：

```js
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
  const root = {
    querySelectorAll(selector) {
      const map = {
        "[data-i18n]": [{ getAttribute: () => "nav.overview", textContent: "" }],
        "[data-i18n-aria-label]": [{
          getAttribute: () => "topbar.language",
          setAttribute(name, value) { this[name] = value; },
        }],
        "[data-i18n-placeholder]": [],
        "[data-i18n-title]": [],
      };
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
```

**Step 2: 运行测试确认失败**

Run: `node --test tests/js/i18n.test.mjs`
Expected: FAIL，`Cannot find module .../i18n/index.mjs`

**Step 3: 创建字典骨架**

`app/static/js/i18n/zh-CN.mjs`：

```js
export default {
  "app.title": "RAG 运维控制台",
  "nav.overview": "概览",
  "topbar.language": "界面语言",
  "lab.search.complete": "检索完成 · {count} 条命中 · {mode}",
  "errors.http.404": "资源不存在",
  "errors.http.500": "服务内部错误",
  "errors.detail.knowledgeBaseNotFound": "知识库不存在",
  "errors.unexpected": "请求失败，请稍后重试。",
};
```

`app/static/js/i18n/en.mjs`：

```js
export default {
  "app.title": "RAG Operations Console",
  "nav.overview": "Overview",
  "topbar.language": "Language",
  "lab.search.complete": "Search complete · {count} hits · {mode}",
  "errors.http.404": "Not found",
  "errors.http.500": "Internal server error",
  "errors.detail.knowledgeBaseNotFound": "Knowledge base not found",
  "errors.unexpected": "Request failed. Please try again.",
};
```

**Step 4: 实现 `app/static/js/i18n/index.mjs`**

```js
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
```

**Step 5: 现有 Node 测试固定为英文**

在每个 `tests/js/*.test.mjs` 文件顶部 import 段之后加：

```js
import { setLocale } from "../../app/static/js/i18n/index.mjs";
setLocale("en", { persist: false });
```

（路径相对 `tests/js/`，因此是 `../../app/...`。）

**Step 6: 运行测试**

Run: `node --test tests/js/`
Expected: 全绿，包括新增 `i18n.test.mjs` 与原有 107 个用例。

**Step 7: 提交**

```bash
git add app/static/js/i18n/zh-CN.mjs app/static/js/i18n/en.mjs app/static/js/i18n/index.mjs tests/js/i18n.test.mjs tests/js/*.test.mjs
git commit -m "feat(ui): add i18n core with zh-CN and en dictionaries"
```

---

### Task 2: 顶栏语言切换器与启动接线

**Files:**
- Modify: `app/static/index.html`（topbar 与 `<title>`）
- Modify: `app/static/js/app.mjs`
- Modify: `app/static/css/workbench.css`（仅在窄屏溢出时补充）
- Modify: `tests/test_api_ui.py`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试（静态入口）**

在 `tests/test_api_ui.py` 追加：

```python
def test_ui_ships_locale_assets_and_switcher(client: TestClient) -> None:
    html = client.get("/ui/").text
    assert 'id="locale-select"' in html
    assert 'data-i18n="app.title"' in html
    for asset in (
        "/ui/js/i18n/index.mjs",
        "/ui/js/i18n/zh-CN.mjs",
        "/ui/js/i18n/en.mjs",
    ):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert "javascript" in response.headers["content-type"]
```

**Step 2: 写失败测试（浏览器行为）**

在 `tests/test_workbench_ui.py` 追加：

```python
def test_workbench_defaults_to_chinese_and_switches_to_english(workbench_page) -> None:
    page, url = workbench_page
    base = url.split("?")[0]
    page.goto(base)

    assert page.locator('[data-nav="overview"] span').last.inner_text() == "概览"
    assert page.evaluate("document.documentElement.lang") == "zh-CN"

    page.select_option("#locale-select", "en")
    page.wait_for_function("document.documentElement.lang === 'en'")

    assert page.locator('[data-nav="overview"] span').last.inner_text() == "Overview"
    assert page.evaluate("localStorage.getItem('workbuddy.locale')") == "en"
    assert "lang=en" in page.url


def test_workbench_url_language_overrides_stored_preference(workbench_page) -> None:
    page, url = workbench_page
    base = url.split("?")[0]
    page.goto(base)
    page.evaluate("localStorage.setItem('workbuddy.locale', 'zh-CN')")

    page.goto(url)
    assert page.locator('[data-nav="overview"] span').last.inner_text() == "Overview"
    assert page.evaluate("localStorage.getItem('workbuddy.locale')") == "zh-CN"


def test_workbench_locale_switch_reloads_active_view(workbench_page) -> None:
    page, url = workbench_page
    page.route(
        "**/v1/ops/summary*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"observability": {"window": "24h"}}),
        ),
    )
    page.route(
        "**/v1/ops/metrics/timeseries*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"series": []})
        ),
    )
    page.goto(url.split("?")[0])
    page.wait_for_timeout(300)

    with page.expect_response(lambda response: "/v1/ops/summary" in response.url):
        page.select_option("#locale-select", "en")
```

同时把 fixture 的返回值改为带 `?lang=en`：

```python
            yield page, f"http://127.0.0.1:{server.server_port}/ui/?lang=en"
```

**Step 3: 运行测试确认失败**

Run: `pytest tests/test_api_ui.py::test_ui_ships_locale_assets_and_switcher tests/test_workbench_ui.py::test_workbench_defaults_to_chinese_and_switches_to_english -v`
Expected: FAIL，找不到 `locale-select` / `data-i18n`。

**Step 4: 修改 `index.html`**

`<title>` 改为：

```html
  <title data-i18n="app.title">RAG Operations Console</title>
```

在 `#app-status` 之后插入（保持状态输出靠右、语言下拉最右）：

```html
        <output id="app-status" class="wb-app-status" role="status" aria-live="polite">Ready</output>
        <label class="wb-locale" for="locale-select">
          <span data-i18n="topbar.language">Language</span>
          <select id="locale-select" name="locale" autocomplete="off" data-i18n-aria-label="topbar.language">
            <option value="zh-CN">中文</option>
            <option value="en">English</option>
          </select>
        </label>
```

`workbench.css` 在 `.wb-app-status` 规则之后追加：

```css
.wb-locale {
  display: inline-flex;
  align-items: center;
  align-self: center;
  gap: 6px;
  color: var(--wb-text-secondary);
  font-size: 10px;
  white-space: nowrap;
}

.wb-locale select {
  height: 32px;
  padding: 0 8px;
  border: 1px solid var(--wb-border-strong);
  border-radius: 4px;
  background: var(--wb-surface);
  color: var(--wb-text);
  font-family: var(--wb-font-sans);
  font-size: 11px;
}

.wb-locale select:focus {
  border-color: var(--wb-accent);
}
```

导航项内层文字先加标注（本 Task 只做导航与顶栏，其余静态文案在 Task 3–8）：

```html
<span data-i18n="nav.overview">Overview</span>
```

## 后续依次为 `nav.lab`、`nav.traces`、`nav.ingestion`、`nav.evaluation`、`nav.access`、`nav.audit`。

**Step 5: 修改 `app.mjs`**

顶部 import 与启动顺序：

```js
import { applyTranslations, getLocale, initLocale, LOCALES, setLocale, t } from "./i18n/index.mjs";
```

`initializeWorkbench()` 开头改为：

```js
function initializeWorkbench() {
  initLocale();
  applyTranslations(document);
  syncLocaleSelect();
  activeScope = readScope();
  // ...原有控制器创建保持不变
```

新增函数：

```js
function syncLocaleSelect() {
  const select = document.getElementById("locale-select");
  if (select) select.value = getLocale();
}

function appendLangToUrl(code) {
  const url = new URL(window.location.href);
  url.searchParams.set("lang", code);
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function onLocaleChange(event) {
  const next = setLocale(event.target.value);
  appendLangToUrl(next);
  applyTranslations(document);
  syncLocaleSelect();
  const view = viewFromHash(window.location.hash);
  const snapshot = scopeGuard.snapshot();
  setStatus(t("topbar.languageChanged", { language: LOCALES.find((item) => item.code === next)?.label ?? next }));
  window.dispatchEvent(new CustomEvent("workbuddy:refresh", {
    detail: { scope: activeScope, view, ...snapshot },
  }));
}
```

在 `initializeWorkbench()` 的事件绑定区加：

```js
  document.getElementById("locale-select")?.addEventListener("change", onLocaleChange);
```

同时把 `setStatus("Scope changed; previous view data cleared.")` 与 `setStatus(\`Refresh requested for ${view}.\`)` 改为 `t("app.scopeChanged")` 与 `t("app.refreshRequested", { view })`，并在两份字典补：

```js
  "app.scopeChanged": "作用域已变更，旧视图数据已清除。",
  "app.refreshRequested": "已请求刷新 {view}。",
  "topbar.languageChanged": "界面语言已切换为 {language}。",
```

英文对应：

```js
  "app.scopeChanged": "Scope changed; previous view data cleared.",
  "app.refreshRequested": "Refresh requested for {view}.",
  "topbar.languageChanged": "Interface language set to {language}.",
```

**Step 6: 运行测试**

Run: `pytest tests/test_api_ui.py::test_ui_ships_locale_assets_and_switcher tests/test_workbench_ui.py -v`
Expected: 新用例 PASS；原有 Playwright 用例仍 PASS（fixture 已带 `?lang=en`）。

Run: `node --test tests/js/`
Expected: PASS。

**Step 7: 提交**

```bash
git add app/static/index.html app/static/js/app.mjs app/static/css/workbench.css app/static/js/i18n/zh-CN.mjs app/static/js/i18n/en.mjs tests/test_api_ui.py tests/test_workbench_ui.py
git commit -m "feat(ui): add language switcher and locale bootstrap"
```

---

### 附录 A：中文术语表（测试断言依赖）

以下取值在 Task 3–8 的字典与断言中必须一致：

| key | zh-CN | en |
| --- | --- | --- |
| `shell.brandSubtitle` | RAG 运维 | RAG Operations |
| `nav.lab` | 查询实验室 | Query Lab |
| `nav.traces` | 调用追踪 | Traces |
| `nav.ingestion` | 入库任务 | Ingestion |
| `nav.evaluation` | 评测与演练 | Evaluation |
| `nav.access` | 访问控制 | Access Control |
| `nav.audit` | 审计日志 | Audit Log |
| `scope.refresh` | 刷新 | Refresh |
| `overview.card.requests` | 请求数 | Requests |
| `lab.tab.search` | 检索 | Search |
| `lab.search.run` | 执行检索 | Run search |
| `traces.table.time` | 时间 | Time |
| `ingestion.retry` | 重试失败任务 | Retry failed job |
| `evaluation.trigger.loadtest` | 启动压测 | Load test |

---

### Task 3: 应用壳与 Overview 静态文案

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/js/i18n/zh-CN.mjs`
- Modify: `app/static/js/i18n/en.mjs`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试**

在 `tests/test_workbench_ui.py` 追加（后续 Task 4–8 会在同一函数末尾继续追加断言）：

```python
def test_workbench_chinese_copy_covers_shell_and_overview(workbench_page) -> None:
    page, url = workbench_page
    page.goto(url.split("?")[0])

    assert page.locator(".wb-brand strong").inner_text() == "WorkBuddy"
    assert page.locator(".wb-brand-copy span").inner_text() == "RAG 运维"
    assert page.locator('[data-nav="lab"] span').last.inner_text() == "查询实验室"
    assert page.locator("#view-overview h1").inner_text() == "概览"
    assert page.locator('[data-overview-card="requests"] h2').inner_text() == "请求数"
    assert page.locator("#scope-refresh").inner_text() == "刷新"
```

Run: `pytest tests/test_workbench_ui.py::test_workbench_chinese_copy_covers_shell_and_overview -v`
Expected: FAIL（当前显示英文）。

**Step 2: 标注并翻译**

覆盖范围：

- brand：`aria-label`、`RAG Operations`。
- 侧边栏：`WORKSPACE`、`Operations workspace`、`Scoped to the active tenant`。
- 顶栏：`GLOBAL SCOPE`、`Applied to every module`、`Global request scope`（aria-label）、`Tenant ID`、`Knowledge base`、`User ID`、`Time window`、四个 `<option>`、`Refresh`、`Ready`。
- Overview：模块 kicker、标题、副标题、4 条说明、13 张卡片标题、`Window`、`Samples`、全部 `data-overview-*` 初始占位文本。

key 命名：`shell.*`、`scope.*`、`overview.*`。示例：

```html
<span class="wb-eyebrow" data-i18n="scope.eyebrow">GLOBAL SCOPE</span>
<label for="scope-tenant" data-i18n="scope.tenant">Tenant ID</label>
<option value="1h" data-i18n="scope.window.1h">Last hour</option>
```

**Step 3: 补字典**

每个 `data-i18n` key 同时补两份字典；英文值必须与改造前的 HTML 原文逐字一致，避免现有英文断言变化。

**Step 4: 验证**

Run: `pytest tests/test_api_ui.py tests/test_workbench_ui.py -v`
Expected: 新中文用例 PASS；英文路径用例仍 PASS。

**Step 5: 提交**

```bash
git add app/static/index.html app/static/js/i18n/zh-CN.mjs app/static/js/i18n/en.mjs tests/test_workbench_ui.py
git commit -m "feat(ui): localize shell and overview static copy"
```

---

### Task 4: Lab 静态文案

**Files:** 同 Task 3 三个文件 + `tests/test_workbench_ui.py`。

覆盖：三个 Tab 标签、搜索/问答/入库三个面板的全部 `<label>`、`aria-label`、按钮文字、空态与提示文本、`option` 文本。

key 前缀：`lab.*`。英文值保持原文。

在 `test_workbench_chinese_copy_covers_shell_and_overview` 末尾追加：

```python
    assert page.locator('[data-lab-tab="search"]').inner_text() == "检索"
    assert page.locator("#search-button").inner_text() == "执行检索"
```

Run: `pytest tests/test_workbench_ui.py -k chinese -v`
Expected: PASS。

提交：`feat(ui): localize retrieval lab static copy`。

---

### Task 5: Traces 静态文案

**Files:** 同 Task 4。

覆盖：筛选表单标签、按钮、表头（含 Token 列）、Inspector 三个面板标题、复制/展开等控件、空态与初始状态文本。

key 前缀：`traces.*`。英文值保持原文。

在中文用例末尾追加：

```python
    assert page.locator("#view-traces h1").inner_text() == "调用追踪"
    assert page.locator(".wb-trace-table thead th").first.inner_text() == "时间"
```

Run: `pytest tests/test_workbench_ui.py -k chinese -v`
Expected: PASS。

提交：`feat(ui): localize traces static copy`。

---

### Task 6: Ingestion 静态文案

**Files:** 同 Task 4。

覆盖：任务列表/详情/事件三个区块标题、表头、重试按钮、状态与空态文本。

key 前缀：`ingestion.*`。

在中文用例末尾追加：

```python
    assert page.locator("#view-ingestion h1").inner_text() == "入库任务"
    assert page.locator("#ingestion-retry").text_content() == "重试失败任务"
```

Run: `pytest tests/test_workbench_ui.py -k chinese -v`
Expected: PASS。

提交：`feat(ui): localize ingestion static copy`。

---

### Task 7: Evaluation 静态文案

**Files:** 同 Task 4。

覆盖：评测运行列表、详情、输出区、四个触发按钮与确认提示文本。

key 前缀：`evaluation.*`。

在中文用例末尾追加：

```python
    assert page.locator("#view-evaluation h1").inner_text() == "评测与演练"
    assert page.locator("#run-trigger-loadtest").inner_text() == "启动压测"
```

Run: `pytest tests/test_workbench_ui.py -k chinese -v`
Expected: PASS。

提交：`feat(ui): localize evaluation static copy`。

---

### Task 8: Access 与 Audit 静态文案

**Files:** 同 Task 4。

覆盖：身份状态、KB 权限、三层权限对比、审计时间线与筛选控件。

key 前缀：`access.*`、`audit.*`。

在中文用例末尾追加：

```python
    assert page.locator("#view-access h1").inner_text() == "访问控制"
    assert page.locator("#view-audit h1").inner_text() == "审计日志"
```

Run: `pytest tests/test_workbench_ui.py -k chinese -v`
Expected: PASS。

提交：`feat(ui): localize access and audit static copy`。

---

### Task 9: Overview 与 app/core 动态文案

**Files:**
- Modify: `app/static/js/views/overview.mjs`
- Modify: `app/static/js/app.mjs`（剩余 `setStatus` 字面量）
- Modify: `app/static/js/core.mjs`（如仍需 `formatError` 透出）
- Modify: `app/static/js/i18n/zh-CN.mjs`
- Modify: `app/static/js/i18n/en.mjs`
- Test: `tests/js/overview.test.mjs`

**重要规则：模块顶层常量里不能存文案**，否则切换语言后不会更新。把：

```js
const TRENDS = [{ key: "requests", label: "Requests", unit: "req" }, ...];
```

改为函数：

```js
function trendDefinitions() {
  return [
    { key: "requests", label: t("overview.trend.requests"), unit: "req" },
    { key: "e2e_p95_ms", label: t("overview.trend.e2e"), unit: "ms" },
    { key: "retrieval_p95_ms", label: t("overview.trend.retrieval"), unit: "ms" },
    { key: "llm_p95_ms", label: t("overview.trend.llm"), unit: "ms" },
  ];
}
```

调用处 `for (const trend of trendDefinitions())`。

替换的文案清单：

- `costStatus()`：`Not configured`、`Complete estimate`、`Partial estimate`。
- `renderTrend()`：`No trend data for {label}.`、aria-label `{label} trend`、desc 模板。
- `renderSummary()`：`Snapshot`、`Low sample · interpret carefully`。
- 面板状态：`Awaiting refresh.`、`Loading summary…`、`Refreshing summary…`、`Updated {time}`、`stale-at {time} · Last success {time} · {error}`、`Summary unavailable: {error}`，trends 同理。
- `errorMessage()` 改为使用 `formatError` 并删除本地实现。
- `app.mjs` 中 `Scope changed...` / `Refresh requested...` 若 Task 2 未完全替换，在此补齐。

**Step 1–5:** 先改 `tests/js/overview.test.mjs` 的断言保持英文（已由 `setLocale("en")` 保证），新增一条中文断言：

```js
test("overview status uses Chinese after locale switch", async () => {
  setLocale("zh-CN", { persist: false });
  const fixture = createFixture();
  await fixture.controller.loadOverview(fixture.context);
  assert.match(fixture.root.text("[data-overview-summary-status]"), /更新于|更新/);
  setLocale("en", { persist: false });
});
```

Run: `node --test tests/js/overview.test.mjs`
Expected: 新增用例先 FAIL，实现后 PASS。

**Step 6: 提交**

```bash
git add app/static/js/views/overview.mjs app/static/js/app.mjs app/static/js/core.mjs app/static/js/i18n/zh-CN.mjs app/static/js/i18n/en.mjs tests/js/overview.test.mjs
git commit -m "feat(ui): localize overview dynamic copy"
```

---

### Task 10: Lab 动态文案

**Files:**
- Modify: `app/static/js/views/lab.mjs`
- Modify: `app/static/js/i18n/zh-CN.mjs`、`en.mjs`
- Test: `tests/js/lab.test.mjs`

要点：

- `BUTTON_LABELS` 改为 `buttonLabels()` 函数（英文值：`Run search`、`Start streaming chat`、`Upload and track job`；忙碌态：`Searching…`、`Answering…`、`Processing…`）。
- `errorMessage()` 改用 `formatError`。
- `responseError()` 改为抛出带 `status` 与 `body` 的 `ApiError`（从 `../api.mjs` 导入），保证错误映射生效。
- 文案：搜索完成/空态/失败、问答状态、引用提示、上传进度与任务状态。

运行 `node --test tests/js/lab.test.mjs`，英文断言保持通过；补一条中文状态断言后提交：

```bash
git add app/static/js/views/lab.mjs app/static/js/i18n/zh-CN.mjs app/static/js/i18n/en.mjs tests/js/lab.test.mjs
git commit -m "feat(ui): localize retrieval lab dynamic copy"
```

---

### Task 11: Traces 动态文案

**Files:**
- Modify: `app/static/js/views/traces.mjs`
- Modify: `app/static/js/i18n/zh-CN.mjs`、`en.mjs`
- Test: `tests/js/traces.test.mjs`

覆盖：列表状态、`Loading traces…`、`Apply filters or refresh to load traces.`、`Cancelled`、`Copied`、`Copy failed`、`not recorded`、`not supported`、`Zero-duration`、`proportion unavailable`、Inspector 各阶段名称与缺失事件提示、`—` 以外的所有说明文字。

`formatError` 替换本地 `errorMessage()`。

Run: `node --test tests/js/traces.test.mjs`
Expected: PASS（英文词典值必须与现有断言逐字一致）。

提交：`feat(ui): localize traces dynamic copy`。

---

### Task 12: Ingestion 动态文案

**Files:**
- Modify: `app/static/js/views/ingestion.mjs`
- Modify: `app/static/js/i18n/zh-CN.mjs`、`en.mjs`
- Test: `tests/js/ingestion.test.mjs`

覆盖：任务状态、阶段事件、`reloaded`、`no longer visible`、`admin required`、`已有运行中的任务` 等运行时文案（注意该中文来自后端 detail，前端只翻译自身生成的文案）、进度与空的 `—` 处理。

Run: `node --test tests/js/ingestion.test.mjs`

提交：`feat(ui): localize ingestion dynamic copy`。

---

### Task 13: Evaluation 动态文案

**Files:**
- Modify: `app/static/js/views/evaluation.mjs`
- Modify: `app/static/js/i18n/zh-CN.mjs`、`en.mjs`
- Test: `tests/js/evaluation.test.mjs`

覆盖：运行状态、触发动作结果、`already active`、`valid run id`、`terminal`、轮询状态与错误、输出区空态。

Run: `node --test tests/js/evaluation.test.mjs`

提交：`feat(ui): localize evaluation dynamic copy`。

---

### Task 14: Access、Audit、permissions 动态文案

**Files:**
- Modify: `app/static/js/views/access.mjs`
- Modify: `app/static/js/views/audit.mjs`
- Modify: `app/static/js/permissions.mjs`
- Modify: `app/static/js/i18n/zh-CN.mjs`、`en.mjs`
- Test: `tests/js/access.test.mjs`、`tests/js/audit.test.mjs`、`tests/js/permissions.test.mjs`（按实际文件名调整）

覆盖：权限身份状态、角色名额、对比结果、审计时间线空态与错误提示、`Administrator role required` 等权限文案。

Run: `node --test tests/js/`

提交：`feat(ui): localize access audit and permissions copy`。

---

### Task 15: 全量验证与浏览器走查

**Files:** 无新增，仅修复走查发现的问题。

**Step 1: 回归测试**

```powershell
docker pause rag-kb-worker
pytest
node --test tests/js/
docker unpause rag-kb-worker
```

Expected: 全绿；若 `rag-kb-worker` 不存在则跳过 pause/unpause 并在结果中说明。

**Step 2: 浏览器走查**

启动静态服务或使用现有 `http://127.0.0.1:8000/ui/`，分别检查：

| 检查项 | 预期 |
| --- | --- |
| 无 `?lang` 首次进入 | 中文导航、中文卡片标题、中文状态 |
| 切换 English | 不刷新页面，静态文案与状态立即变英文，当前视图重新加载 |
| 刷新页面 | 保持上次选择 |
| `?lang=en` + 已存中文 | 以英文显示，存储不被改写 |
| 四个宽度 1440 / 1280 / 900 / 390 | 顶栏语言下拉不溢出、不遮挡状态输出 |
| 键盘 | Tab 可聚焦下拉，方向键改变语言 |
| 技术字段 | Trace ID、Chunk ID、时间戳、`ms`/`%`/`tokens`、HTTP 码保持原样 |

**Step 3: 文档勾选**

在本计划文件末尾追加一节 `## 执行记录`，记录每个 Task 的 commit hash、测试命令与结果、走查截图路径（若已保存）。

**Step 4: 提交**

```bash
git add docs/plans/2026-11-21-workbuddy-i18n.md
git commit -m "docs(ui): record i18n implementation verification"
```
