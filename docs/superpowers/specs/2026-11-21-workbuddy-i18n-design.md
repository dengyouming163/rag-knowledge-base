# WorkBuddy 控制台多语言（中文 / 英文）设计

- 日期：2026-11-21
- 状态：已确认，待书面评审
- 仓库：`projects/rag-knowledge-base`
- 入口：`/ui`
- 范围：仅前端（`app/static/`），不改后端接口

## 1. 目标

1. 为 WorkBuddy 控制台提供中文（`zh-CN`）与英文（`en`）两套界面语言。
2. 首次访问默认中文；用户可在顶栏切换为 English，选择被记住。
3. 语言可通过 `?lang=zh-CN` / `?lang=en` 显式指定，便于分享链接与自动化测试。
4. 新增第三种语言时，只需新增一个字典文件并在注册表登记一行，不改视图与 HTML 结构。
5. 语言切换不破坏现有键盘操作、ARIA、响应式布局与 XSS 防护。

## 2. 非目标

- 不做后端 `detail` 的全链路 i18n，不发送 `Accept-Language`，不改任何 API 契约。
- 不做时间戳、日期、数字的本地化格式；时间与数值仍按原样显示。
- 不做 RTL（阿拉伯语、希伯来语）排版。
- 本期不交付第三种语言。
- 不自动跟随浏览器语言；未显式选择时固定中文。

## 3. 已确认决策

| 主题 | 决策 |
| --- | --- |
| 默认语言 | 默认中文，可切英文 |
| 翻译边界 | 前端文案 + 常见后端错误映射 |
| 语种范围 | 本期仅 `zh-CN` 与 `en`，结构支持扩展 |
| 切换入口 | 顶栏下拉 + URL `?lang=` 覆盖 |
| 切换后行为 | 即时重译静态文案，并重拉当前视图数据 |
| 实现方案 | 方案 A：字典 + `data-i18n` 标注 + `t()` |

## 4. 架构

### 4.1 新增文件

```
app/static/js/i18n/index.mjs    # 注册表、语言解析、t()、applyTranslations()、错误映射
app/static/js/i18n/zh-CN.mjs    # 默认语言字典
app/static/js/i18n/en.mjs       # 英文字典
```

### 4.2 模块职责

| 模块 | 职责 | 依赖 |
| --- | --- | --- |
| `zh-CN.mjs` / `en.mjs` | 导出扁平 `key → 文案` 对象 | 无 |
| `i18n/index.mjs` | 语言注册表、解析与持久化、取词与插值、DOM 标注替换、错误映射 | 两份字典 |
| `app.mjs` | 启动时先翻译静态骨架，再创建视图；绑定切换器 | `i18n/index.mjs` |
| 视图模块 | 所有用户可见字面量改走 `t()`；错误展示统一走 `formatError()` | `i18n/index.mjs` |

### 4.3 字典格式

扁平点分 key，避免嵌套结构带来的合并与回退复杂度：

```js
export default {
  "app.title": "RAG 运维控制台",
  "nav.overview": "概览",
  "nav.lab": "查询实验室",
  "topbar.language": "界面语言",
  "overview.status.awaiting": "等待刷新。",
  "lab.search.complete": "检索完成 · {count} 条命中 · {mode}",
  "errors.http.404": "资源不存在",
  "errors.detail.knowledgeBaseNotFound": "知识库不存在",
};
```

约定：

- key 使用小写驼峰或短横线，稳定后不复用旧 key 表达新语义。
- 插值占位符统一 `{name}`，参数缺失时保留占位符原文，便于发现漏传。
- 产品名 `WorkBuddy`、单位符号（`ms`、`req`、`req/min`、`%`、`USD`、`tokens`）、HTTP 状态码、`[DONE]`、模块编号（`01`–`07`）不翻译。
- 中文文案使用中文标点；英文文案保持现有措辞，避免无意义改写导致既有测试大面积变更。

## 5. 语言解析与回退

优先级从高到低：

1. URL 查询参数 `?lang=`；
2. `localStorage["workbuddy.locale"]`；
3. 默认 `zh-CN`。

规范化规则：

| 输入 | 结果 |
| --- | --- |
| `zh`、`zh-CN`、`zh-Hans`、`zh-Hans-CN` | `zh-CN` |
| `en`、`en-US`、`en-GB` | `en` |
| 其他或空 | `zh-CN`（`console.warn` 一次） |

取词回退链：当前语言字典 → `en` 字典 → key 本身。缺失 key 不抛异常，界面继续可用。

`?lang=` 只对本次加载生效，不写入 `localStorage`；只有用户主动操作下拉框时才持久化选择。这样分享链接不会覆盖接收者的既有偏好。

运行时将 `document.documentElement.lang` 设为当前 locale。静态 HTML 保留 `lang="en"` 作为脚本未执行时的兜底。

## 6. 静态 DOM 接入

`index.html` 的纯文本节点按下表加标注，由 `applyTranslations(root = document)` 一次性替换：

| 属性 | 替换目标 |
| --- | --- |
| `data-i18n` | `textContent` |
| `data-i18n-aria-label` | `aria-label` |
| `data-i18n-placeholder` | `placeholder` |
| `data-i18n-title` | `title` |

约束：

- 只给纯文本节点加标注；含子元素的容器不加，改标注到内层文本节点（如导航项第二个 `<span>`）。
- 替换只改文本/属性值，不替换或重建节点，保证焦点、事件监听、`hidden`、`aria-current` 等状态不受影响。
- 不翻译 `id`、`class`、`data-*` 值、表单 `name` 与 `value`。
- `applyTranslations()` 必须幂等，可重复调用。
- 动态插入的 DOM 若含标注，也由同一函数在插入后调用一次。

## 7. 动态文案与错误映射

### 7.1 取词

- 7 个视图、`permissions.mjs`、`app.mjs` 中所有用户可见字符串改走 `t(key, params)`。
- 按钮忙碌态（如 `Searching…`）与常规态共用一组 key，由同一渲染函数按语言取值，避免切换后残留旧语言。
- 长度受控的展示格式（如得分、时长）继续由 `formatMetric()` 生成，单位不翻译。

### 7.2 错误映射

新增 `formatError(error)` 并从 `i18n/index.mjs` 导出，替换各视图内重复的 `errorMessage()`：

1. `error.name === "ApiError"` 且 `error.status` 命中已知状态码：取 `errors.http.<status>`；
2. 若 `error.body.detail` 是字符串且命中 `errors.detail.<slug>` 映射，则以该文案覆盖状态码文案；
3. 未命中任何映射：保留原始 `error.message` 透传，不吞掉后端信息；
4. 非 `ApiError` 异常：返回 `errors.unexpected` 文案，并把原始信息保留在 `console.error`。

覆盖的常见状态码：`400`、`401`、`403`、`404`、`409`、`422`、`429`、`500`、`503`。常见 detail 映射先覆盖现有测试中出现的 `Knowledge base not found` 等少量固定文案，后续按需追加。

## 8. 切换器与切换行为

### 8.1 位置与控件

- 顶栏 `app-status` 旁边新增 `<select id="locale-select">`，选项为「中文 / English」。
- 控件自身 `aria-label` 走 `data-i18n-aria-label="topbar.language"`，可被 Tab 聚焦并键盘切换。
- 不引入新的浮层组件，保持与现有顶栏布局一致；窄屏下允许换行，不溢出。

### 8.2 切换流程

用户改变选择后依次执行：

1. `setLocale(code)` 写 `localStorage["workbuddy.locale"]`；
2. 用 `history.replaceState` 更新当前 URL 的 `lang` 参数（保留其他查询参数，hash 不变）；
3. `applyTranslations(document)` 重译静态骨架；
4. 同步 `<html lang>` 与下拉框当前值；
5. 派发既有 `workbuddy:refresh`，`view` 取当前 hash，触发当前视图重新请求并按新语言渲染。

已知代价：

- Lab 中已展示的检索结果会被清空，与切换 scope 的现有行为一致；不保留上一语言的渲染结果。
- 其他视图会重新拉取当前数据，已选中的 Trace / 评测运行若仍存在则按现有逻辑重新加载。

### 8.3 启动顺序

1. 解析语言（URL → 存储 → 默认）；
2. `applyTranslations(document)` 翻译静态骨架；
3. 同步 `<html lang>` 与下拉框值；
4. 创建各视图控制器并 `restoreRoute()`；
5. 绑定下拉框 `change` 事件。

这样视图首次渲染即使用正确语言，不会出现英文骨架闪变。

## 9. 测试策略

### 9.1 Node 单元测试

- 每个现有 `tests/js/*.test.mjs` 顶部调用 `setLocale("en", { persist: false })`，保持现有英文断言不变。
- 新增 `tests/js/i18n.test.mjs` 覆盖：
  - 解析优先级：`?lang=` 覆盖 localStorage，localStorage 覆盖默认值；
  - 语言规范化与未知值回退；
  - 缺 key 回退到 `en` 再回退到 key；
  - `{name}` 插值与缺参保留；
  - `applyTranslations()` 对四种属性的替换与幂等性；
  - `formatError()` 的状态码映射、detail 覆盖、未知错误透传。

### 9.2 Playwright 浏览器测试

- fixture 的 `workbench_url` 改为 `http://127.0.0.1:<port>/ui/?lang=en`，现有 35 处导航与英文文案断言零改动。
- 新增用例：
  - 无 `?lang` 首次加载为中文：导航显示「概览」、`documentElement.lang === "zh-CN"`；
  - 下拉切换 English 后导航与状态变英文，`localStorage` 记录 `en`，URL 出现 `lang=en`；
  - 中文页面加载 `?lang=en` 时以英文显示（URL 覆盖存储）；
  - 切换语言后当前视图重新发起请求（用路由计数断言）；
  - 切换器可 Tab 聚焦、用键盘改变选项。

### 9.3 静态入口测试

`tests/test_api_ui.py` 新增断言：

- `/ui/js/i18n/index.mjs`、`/ui/js/i18n/zh-CN.mjs`、`/ui/js/i18n/en.mjs` 均返回 200 且为 JavaScript；
- `index.html` 含 `data-i18n` 标注与 `id="locale-select"`；
- 现有 `lang` 与标题断言保持兼容。

### 9.4 回归

- 完成后运行全量 `pytest` 与 `node --test tests/js/`。
- 受保护文件（`src/`、`deploy/`、`eval/` 等）不修改、不暂存。

## 10. 验收标准

1. 首次访问 `/ui/` 显示中文界面，导航、表单标签、按钮、空态、加载与错误状态均为中文。
2. 顶栏切换到 English 后无需刷新，静态文案立即变为英文，当前视图重新加载并以英文渲染。
3. 刷新页面后保持上次选择；分享带 `?lang=` 的链接可指定语言。
4. 中文下缩略单位、Trace ID、Chunk ID、时间戳、HTTP 状态码保持原样，可读性不下降。
5. 语言切换后 Tab 顺序、焦点、`aria-current`、响应式布局（1440/1280/900/390）与 XSS 防护均无回归。
6. 全量 `pytest` 与 `node --test tests/js/` 通过。

## 11. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 文案量大（HTML 约 314 处文本节点，JS 数百处）易漏 | 按视图分批改造，用 `data-i18n` 覆盖率检查与人工走查双保险 |
| 切换语言清空 Lab 结果引起误解 | 切换后状态区提示当前视图已刷新；与 scope 变更行为一致 |
| 错误映射掩盖真实后端信息 | 未匹配时原文透传；映射仅覆盖固定常见文案 |
| 中文文案变长导致布局溢出 | 复用现有响应式断点，在 390px 宽度下走查关键视图 |
| 字典 key 拼写错误导致显示 key | 回退链 + Node 测试校验两份字典 key 集合一致 |
