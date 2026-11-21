"""WorkBuddy 应用壳的静态托管与模块边界测试。"""

from __future__ import annotations

import json
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


NAV = ("overview", "lab", "traces", "ingestion", "evaluation", "access", "audit")
STATIC_ROOT = Path(__file__).parents[1] / "app" / "static"


class _WorkbenchStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        if path.startswith("/ui/"):
            path = path[3:]
        return super().translate_path(path)

    def log_message(self, _format: str, *args: object) -> None:
        pass


@pytest.fixture()
def workbench_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error:
            chrome_candidates = (
                Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            )
            executable = next((path for path in chrome_candidates if path.is_file()), None)
            if executable is None:
                pytest.skip("No Playwright Chromium or system Chrome executable is available")
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)

        handler = partial(_WorkbenchStaticHandler, directory=str(STATIC_ROOT))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page = browser.new_page()
            yield page, f"http://127.0.0.1:{server.server_port}/ui/workbench.html"
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_workbench_shell(client: TestClient) -> None:
    response = client.get("/ui/workbench.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<title>RAG Operations Console</title>" in html
    assert 'class="wb-sidebar"' in html
    assert 'class="wb-topbar"' in html
    assert 'class="wx-rail"' not in html
    assert '<script type="module" src="/ui/js/app.mjs"></script>' in html
    assert html.count("data-nav=") == len(NAV)
    assert html.count("data-view=") == len(NAV)
    for name in NAV:
        assert f'data-nav="{name}"' in html
        assert f'data-view="{name}"' in html
    for element_id in (
        "scope-tenant",
        "scope-kb",
        "scope-user",
        "scope-window",
        "scope-refresh",
        "app-status",
    ):
        assert f'id="{element_id}"' in html
    assert '<section id="view-overview" data-view="overview"' in html
    for name in NAV[1:]:
        assert f'data-view="{name}" hidden' in html


def test_workbench_overview_contains_operational_metrics_and_trends(
    client: TestClient,
) -> None:
    html = client.get("/ui/workbench.html").text
    overview = html.split('<section id="view-overview"', 1)[1].split("</section>", 1)[0]

    for label in (
        "Requests",
        "Avg QPM",
        "4xx",
        "5xx",
        "E2E p95",
        "Retrieve p95",
        "LLM p95",
        "Zero-hit",
        "Refusal",
        "Citation",
        "Token",
        "Cost",
        "Queue",
        "Workers",
    ):
        assert label in overview

    metric_names = (
        "requests", "avg_qpm", "client_errors", "server_errors", "e2e_p95",
        "retrieval_p95", "llm_p95", "zero_hit", "refusal", "citation",
        "tokens", "cost", "queue", "workers",
    )
    assert overview.count("data-overview-card=") == len(metric_names)
    for name in metric_names:
        assert f'data-overview-card="{name}"' in overview
    for field in ("value", "unit", "window", "samples"):
        assert overview.count(f"data-overview-{field}=") == len(metric_names)
    assert "Low sample" in overview

    for name in ("requests", "e2e_p95_ms", "retrieval_p95_ms", "llm_p95_ms"):
        assert f'data-overview-trend="{name}"' in overview
        assert f'data-overview-trend-svg="{name}"' in overview

    assert 'from "./views/overview.mjs"' in client.get("/ui/js/app.mjs").text
    assert client.get("/ui/js/views/overview.mjs").status_code == 200


def test_workbench_lab_contains_retrieval_chat_and_ingestion_controls(
    client: TestClient,
) -> None:
    html = client.get("/ui/workbench.html").text
    lab = html.split('<section id="view-lab"', 1)[1].split("</section>", 1)[0]

    for tab in ("search", "chat", "ingest"):
        assert f'data-lab-tab="{tab}"' in lab
        assert f'data-lab-panel="{tab}"' in lab
    element_ids = (
        "upload-form",
        "upload-input",
        "upload-progress",
        "search-form",
        "search-results",
        "search-trace-link",
        "chat-form",
        "answer",
        "citation-list",
        "stream-meta",
        "chat-trace-link",
    )
    for element_id in element_ids:
        assert f'id="{element_id}"' in lab
        assert html.count(f'id="{element_id}"') == 1

    assert 'from "./views/lab.mjs"' in client.get("/ui/js/app.mjs").text
    assert client.get("/ui/js/views/lab.mjs").status_code == 200


def test_workbench_core_modules(client: TestClient) -> None:
    app_response = client.get("/ui/js/app.mjs")
    css_response = client.get("/ui/css/workbench.css")

    assert app_response.status_code == 200
    assert "javascript" in app_response.headers["content-type"]
    assert css_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]
    for marker in (
        "hashchange",
        "replaceState",
        "workbuddy:scopechange",
        "workbuddy:refresh",
        "ScopeGuard",
    ):
        assert marker in app_response.text


def _relative_luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter = max(_relative_luminance(foreground), _relative_luminance(background))
    darker = min(_relative_luminance(foreground), _relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def test_workbench_muted_token_meets_aa_on_shell_backgrounds(client: TestClient) -> None:
    css = client.get("/ui/css/workbench.css").text
    tokens = dict(re.findall(r"(--wb-[\w-]+):\s*(#[0-9a-fA-F]{6});", css))

    for background_token in ("--wb-surface", "--wb-bg"):
        ratio = _contrast_ratio(tokens["--wb-muted"], tokens[background_token])
        assert ratio >= 4.5, f"--wb-muted on {background_token} has contrast {ratio:.2f}:1"


def test_trace_deep_links_survive_lab_clicks_and_direct_loads(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.route(
        "**/v1/kbs/kb_after_sale/search",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            headers={"X-Trace-Id": "trace_search"},
            body=json.dumps({"kb_id": "kb_after_sale", "mode": "hybrid", "hits": []}),
        ),
    )
    page.goto(f"{workbench_url}#/lab/search")
    page.locator("#search-query").fill("return policy")
    page.locator("#search-button").click()
    trace_link = page.locator("#search-trace-link")
    trace_link.wait_for(state="visible")
    assert trace_link.get_attribute("href") == "#/traces/trace_search"

    trace_link.click()
    page.locator("#view-traces").wait_for(state="visible")
    assert page.evaluate("window.location.hash") == "#/traces/trace_search"

    page.goto("about:blank")
    page.goto(f"{workbench_url}#/traces/trace%5Fdirect")
    page.locator("#view-traces").wait_for(state="visible")
    assert page.evaluate("window.location.hash") == "#/traces/trace_direct"

    for unsafe_hash in (
        "#/traces/%2Funsafe",
        "#/traces/%3Cscript%3E",
        "#/traces/trace/extra",
        "#/traces/%E0%A4%A",
    ):
        page.goto(f"{workbench_url}{unsafe_hash}")
        page.locator("#view-traces").wait_for(state="visible")
        assert page.evaluate("window.location.hash") == "#/traces"


def test_lab_form_geometry_fits_medium_and_mobile_widths(workbench_page) -> None:
    page, workbench_url = workbench_page
    for width in (821, 900, 390):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{workbench_url}#/lab/search")
        page.locator("#search-form").wait_for(state="visible")
        geometry = page.locator("#search-form").evaluate(
            """form => {
                const rect = form.getBoundingClientRect();
                return {
                    left: rect.left,
                    right: rect.right,
                    clientWidth: form.clientWidth,
                    scrollWidth: form.scrollWidth,
                    columns: getComputedStyle(form).gridTemplateColumns,
                    children: [...form.children].map((element) => {
                        const child = element.getBoundingClientRect();
                        return { id: element.id, left: child.left, right: child.right };
                    }),
                };
            }"""
        )

        assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, (width, geometry)
        for child in geometry["children"]:
            assert child["left"] >= geometry["left"] - 1, (width, child, geometry)
            assert child["right"] <= geometry["right"] + 1, (width, child, geometry)
        assert any(child["id"] == "search-button" for child in geometry["children"])
        if width == 390:
            assert len(geometry["columns"].split()) == 1, geometry

    for width in (821, 900, 390):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{workbench_url}#/lab/chat")
        page.locator("#chat-form").wait_for(state="visible")
        geometry = page.locator("#chat-form").evaluate(
            """form => {
                const rect = form.getBoundingClientRect();
                const container = form.closest(".wb-lab").getBoundingClientRect();
                const button = form.querySelector("#chat-button");
                const buttonRect = button.getBoundingClientRect();
                const centerX = (buttonRect.left + buttonRect.right) / 2;
                const centerY = (buttonRect.top + buttonRect.bottom) / 2;
                const centerHit = document.elementFromPoint(centerX, centerY);
                return {
                    left: rect.left,
                    right: rect.right,
                    clientWidth: form.clientWidth,
                    scrollWidth: form.scrollWidth,
                    columns: getComputedStyle(form).gridTemplateColumns,
                    containerLeft: container.left,
                    containerRight: container.right,
                    button: {
                        left: buttonRect.left,
                        right: buttonRect.right,
                        top: buttonRect.top,
                        bottom: buttonRect.bottom,
                        centerX,
                        centerY,
                        centerHit: centerHit?.id || centerHit?.className || centerHit?.tagName,
                        centerHitsButton: centerHit === button || button.contains(centerHit),
                    },
                    children: [...form.children].map((element) => {
                        const child = element.getBoundingClientRect();
                        return { id: element.id, left: child.left, right: child.right };
                    }),
                };
            }"""
        )

        assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, (width, geometry)
        for child in geometry["children"]:
            assert child["left"] >= geometry["left"] - 1, (width, child, geometry)
            assert child["right"] <= geometry["right"] + 1, (width, child, geometry)
        button = geometry["button"]
        assert button["left"] >= geometry["containerLeft"] - 1, (width, geometry)
        assert button["right"] <= geometry["containerRight"] + 1, (width, geometry)
        assert button["top"] >= 0, (width, geometry)
        assert button["bottom"] <= 901, (width, geometry)
        if width == 390:
            assert len(geometry["columns"].split()) == 1, geometry
        if width == 821:
            assert button["centerHitsButton"], (width, geometry)
            page.locator("#chat-question").fill("Geometry click")
            page.locator("#chat-form").evaluate(
                """form => {
                    window.__chatSubmitCount = 0;
                    form.addEventListener("submit", event => {
                        event.preventDefault();
                        event.stopPropagation();
                        window.__chatSubmitCount += 1;
                    }, { once: true });
                }"""
            )
            page.locator("#chat-button").click()
            assert page.evaluate("window.__chatSubmitCount") == 1


def test_workbench_traces_contains_filters_table_inspector_and_responsive_controls(
    client: TestClient,
) -> None:
    html = client.get("/ui/workbench.html").text
    traces = html.split('<section id="view-traces"', 1)[1].split("\n        </section>", 1)[0]

    for element_id in (
        "trace-filter-form",
        "trace-window",
        "trace-outcome",
        "trace-operation",
        "trace-mode",
        "trace-min-ms",
        "trace-id",
        "trace-limit",
        "trace-apply",
        "trace-list-status",
        "trace-list-rows",
        "trace-load-more",
        "trace-detail",
        "trace-missing",
        "trace-raw",
        "trace-copy",
        "trace-copy-status",
        "trace-drawer-close",
        "trace-drawer-backdrop",
    ):
        assert f'id="{element_id}"' in traces
        assert html.count(f'id="{element_id}"') == 1

    headings = re.findall(r'<th scope="col">([^<]+)</th>', traces)
    assert headings == ["Time", "Trace", "Operation", "Outcome", "Stages", "Total", "Hits"]
    for tab in ("summary", "timing", "raw"):
        assert f'data-trace-tab="{tab}"' in traces
        assert f'data-trace-panel="{tab}"' in traces
    for mode in ("list", "detail"):
        assert f'data-trace-mode="{mode}"' in traces
    assert 'role="dialog"' not in traces, "dialog semantics must be synchronized to the drawer breakpoint"
    assert 'data-trace-detail' in traces

    app_module = client.get("/ui/js/app.mjs").text
    assert 'from "./views/traces.mjs"' in app_module
    assert "createTraces" in app_module
    assert client.get("/ui/js/views/traces.mjs").status_code == 200
    css = client.get("/ui/css/workbench.css").text
    for marker in (
        "@media (min-width: 1440px)",
        "@media (min-width: 900px) and (max-width: 1439px)",
        "@media (max-width: 899px)",
        "prefers-reduced-motion",
        "focus-visible",
    ):
        assert marker in css


def _trace_row(trace_id: str, **overrides) -> dict:
    row = {
        "id": 2 if trace_id == "trace-a" else 1,
        "occurred_at": "2026-11-20T10:00:00Z",
        "trace_id": trace_id,
        "operation": "search",
        "mode": "vector",
        "outcome": "succeeded",
        "hits": 2,
        "total_ms": 100.0,
        "llm_ms": 40.0,
        "model": "model-a",
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "stage_spans": [
            {"stage": "retrieve", "start_ms": 10.0, "duration_ms": 20.0, "status": "ok"}
        ],
    }
    row.update(overrides)
    return row


def _trace_detail(trace_id: str) -> dict:
    return {
        "metric": {
            **_trace_row(trace_id),
            "kb_id": "kb_after_sale",
            "citation_ok": True,
            "rerank_degraded": False,
            "error_kind": None,
            "timings_unaccounted_ms": 10.0,
            "stage_spans": [
                {"stage": "rewrite", "start_ms": 0.0, "duration_ms": 10.0, "status": "ok"},
                {"stage": "retrieve", "start_ms": 10.0, "duration_ms": 20.0, "status": "ok"},
                {"stage": "answer", "start_ms": 30.0, "duration_ms": 40.0, "status": "ok"},
                {"stage": "guardrail", "start_ms": 70.0, "duration_ms": 20.0, "status": "ok"},
            ],
        },
        "events": [{"trace_id": trace_id, "query": '<img src=x onerror="window.__xss=1">'}],
        "missing": [],
    }


def _fulfill_json(route, body: dict, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(body),
    )


def test_trace_inspector_filters_cursor_refresh_tabs_raw_copy_and_missing_semantics(
    workbench_page,
) -> None:
    page, workbench_url = workbench_page
    list_requests: list[str] = []
    detail_requests: list[str] = []
    cursor = {"before_occurred_at": "2026-11-20T09:59:59+00:00", "before_id": 99}

    def list_route(route) -> None:
        list_requests.append(route.request.url)
        query = parse_qs(urlparse(route.request.url).query)
        if "before_id" in query:
            _fulfill_json(
                route,
                {"items": [_trace_row("trace-a"), _trace_row("trace-b")], "next_cursor": None},
            )
        else:
            _fulfill_json(route, {"items": [_trace_row("trace-a")], "next_cursor": cursor})

    def detail_route(route) -> None:
        detail_requests.append(route.request.url)
        trace_id = urlparse(route.request.url).path.rsplit("/", 2)[-2]
        if trace_id == "trace-zero":
            body = _trace_detail(trace_id)
            body["metric"]["total_ms"] = 0.0
            body["metric"]["timings_unaccounted_ms"] = None
            body["metric"]["stage_spans"] = [
                {"stage": "custom", "start_ms": 0.0, "duration_ms": 0.0, "status": "ok"}
            ]
        elif trace_id == "trace-missing":
            body = {"metric": None, "events": [], "missing": ["metric", "events"]}
        else:
            body = _trace_detail(trace_id)
        _fulfill_json(route, body)

    page.route("**/v1/ops/trace-list**", list_route)
    page.route("**/v1/ops/traces/*/detail**", detail_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/traces")
    page.locator("#trace-filter-form").wait_for(state="visible")
    assert list_requests == []
    assert detail_requests == []

    page.locator("#trace-window").select_option("7d")
    page.locator("#trace-outcome").select_option("succeeded")
    page.locator("#trace-operation").select_option("search")
    page.locator("#trace-mode").fill("vector")
    page.locator("#trace-min-ms").fill("0")
    page.locator("#trace-id").fill("trace-a")
    page.locator("#trace-limit").fill("2")
    page.locator("#trace-apply").click()
    page.locator('[data-trace-select="trace-a"]').wait_for(state="visible")

    first_query = parse_qs(urlparse(list_requests[0]).query)
    assert first_query == {
        "kb_id": ["kb_after_sale"],
        "window": ["7d"],
        "outcome": ["succeeded"],
        "operation": ["search"],
        "mode": ["vector"],
        "min_ms": ["0"],
        "trace_id": ["trace-a"],
        "limit": ["2"],
    }

    page.locator('[data-trace-select="trace-a"]').click()
    page.locator('[data-trace-summary="trace_id"]').filter(has_text="trace-a").wait_for()
    detail_query = parse_qs(urlparse(detail_requests[0]).query)
    assert detail_query == {"kb_id": ["kb_after_sale"]}

    page.locator("#trace-load-more").click()
    page.locator('[data-trace-select="trace-b"]').wait_for(state="visible")
    assert page.locator('[data-trace-select="trace-a"]').count() == 1
    assert page.locator('tr[data-trace-id="trace-a"].is-selected').count() == 1
    assert page.locator("#trace-load-more").is_hidden()
    second_query = parse_qs(urlparse(list_requests[1]).query)
    assert second_query["before_occurred_at"] == [cursor["before_occurred_at"]]
    assert second_query["before_id"] == ["99"]
    assert second_query["trace_id"] == ["trace-a"]
    assert second_query["window"] == ["7d"]

    page.add_style_tag(content=".wb-trace-row { height: 120px; }")
    page.locator("#trace-table-scroll").evaluate(
        "element => { element.style.maxHeight = '90px'; element.scrollTop = 30; }"
    )
    with (
        page.expect_response(lambda response: "/v1/ops/trace-list" in response.url),
        page.expect_response(lambda response: "/v1/ops/traces/trace-a/detail" in response.url),
    ):
        page.locator("#scope-refresh").click()
    page.wait_for_function(
        "document.querySelector('#trace-list-status').textContent.includes('1 trace')"
    )
    assert page.evaluate("window.location.hash") == "#/traces/trace-a"
    assert page.locator("#trace-id").input_value() == "trace-a"
    assert page.locator('tr[data-trace-id="trace-a"].is-selected').count() == 1
    page.wait_for_function("document.querySelector('#trace-table-scroll').scrollTop === 30")

    page.locator("#trace-inspector-tab-summary").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#trace-inspector-tab-timing").get_attribute("aria-selected") == "true"
    page.keyboard.press("End")
    assert page.locator("#trace-inspector-tab-raw").get_attribute("aria-selected") == "true"
    assert page.locator("#trace-inspector-panel-summary").is_hidden()
    assert page.locator("#trace-inspector-panel-raw").is_visible()
    raw_text = page.locator("#trace-raw").text_content()
    assert json.loads(raw_text)["events"][0]["query"] == '<img src=x onerror="window.__xss=1">'
    assert "<img src=x" in raw_text
    assert page.locator("#trace-raw img").count() == 0
    assert page.evaluate("window.__xss") is None

    page.evaluate(
        """() => {
            window.__copiedRaw = '';
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: { writeText: async value => { window.__copiedRaw = value; } },
            });
        }"""
    )
    page.locator("#trace-copy").click()
    page.locator("#trace-copy-status").filter(has_text="Copied").wait_for()
    assert page.evaluate("window.__copiedRaw") == raw_text

    page.evaluate("window.location.hash = '#/traces/trace-zero'")
    page.locator('[data-trace-summary="trace_id"]').filter(has_text="trace-zero").wait_for(state="attached")
    page.locator("#trace-inspector-tab-timing").click()
    timing_text = page.locator("#trace-inspector-panel-timing").inner_text()
    assert "Zero-duration" in timing_text
    assert "Infinity" not in timing_text
    assert "NaN" not in timing_text

    page.evaluate("window.location.hash = '#/traces/trace-missing'")
    page.locator("#trace-inspector-tab-summary").click()
    page.locator("#trace-missing").filter(has_text="metric").wait_for()
    assert "events" in page.locator("#trace-missing").inner_text()
    assert "not recorded" in page.locator('[data-trace-summary="operation"]').inner_text()


def test_trace_responsive_wide_drawer_focus_and_mobile_internal_tabs(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.route(
        "**/v1/ops/trace-list**",
        lambda route: _fulfill_json(route, {"items": [_trace_row("trace-a")], "next_cursor": None}),
    )
    page.route(
        "**/v1/ops/traces/trace-a/detail**",
        lambda route: _fulfill_json(route, _trace_detail("trace-a")),
    )

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/traces")
    page.locator("#trace-apply").click()
    page.locator('[data-trace-select="trace-a"]').click()
    page.locator("#trace-detail").wait_for(state="visible")
    assert page.locator("#view-traces").get_attribute("data-trace-layout") == "wide"
    assert len(
        page.locator(".wb-traces-layout").evaluate(
            "element => getComputedStyle(element).gridTemplateColumns.split(' ')"
        )
    ) == 3
    assert page.locator("#trace-drawer-backdrop").is_hidden()

    for width in (1280, 900):
        page.set_viewport_size({"width": width, "height": 1000})
        page.wait_for_function(
            "document.querySelector('#view-traces').dataset.traceLayout === 'drawer'"
        )
        if page.locator("#trace-detail").is_visible():
            page.keyboard.press("Escape")
            page.locator("#trace-detail").wait_for(state="hidden")
        page.locator('[data-trace-select="trace-a"]').click()
        page.locator("#trace-detail").wait_for(state="visible")
        assert page.locator("#view-traces").get_attribute("data-trace-layout") == "drawer"
        assert page.locator("#trace-detail").get_attribute("role") == "dialog"
        assert page.locator("#trace-detail").get_attribute("aria-modal") == "true"
        assert page.locator("#trace-drawer-backdrop").is_visible()
        assert page.evaluate("document.activeElement.id") == "trace-drawer-close"
        page.locator("#trace-inspector-tab-raw").focus()
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "trace-drawer-close"
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("document.activeElement.id") == "trace-inspector-tab-raw"
        page.keyboard.press("Escape")
        assert page.locator("#trace-detail").is_hidden()
        assert page.evaluate("document.activeElement.dataset.traceSelect") == "trace-a"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(f"{workbench_url}#/traces")
    page.wait_for_function(
        "document.querySelector('#view-traces').dataset.traceLayout === 'mobile'"
    )
    page.locator("#trace-list-panel").wait_for(state="visible")
    page.locator("#trace-apply").click()
    page.locator('[data-trace-select="trace-a"]').click()
    page.locator("#trace-detail").wait_for(state="visible")
    assert page.locator("#view-traces").get_attribute("data-trace-layout") == "mobile"
    assert page.locator("#trace-mode-detail").get_attribute("aria-selected") == "true"
    assert page.locator("#trace-detail").get_attribute("role") == "tabpanel"
    assert page.locator("#trace-detail").get_attribute("aria-modal") is None
    assert page.locator("#trace-drawer-backdrop").is_hidden()
    page.locator("#trace-mode-list").click()
    assert page.locator("#trace-list-panel").is_visible()
    assert page.locator("#trace-detail").is_hidden()
    page.locator("#trace-mode-detail").click()
    assert page.locator("#trace-detail").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_trace_slow_requests_abort_on_exit_and_scope_change_without_late_render(
    workbench_page,
) -> None:
    page, workbench_url = workbench_page
    sync_api = pytest.importorskip("playwright.sync_api")
    pending_routes = []
    page.add_init_script(
        """
            window.__traceSignals = [];
            window.__traceAbortCount = 0;
            const originalFetch = window.fetch.bind(window);
            window.fetch = (input, init = {}) => {
                if (String(input).includes('/v1/ops/')) {
                    window.__traceSignals.push(init.signal);
                    init.signal?.addEventListener('abort', () => {
                        window.__traceAbortCount += 1;
                    }, { once: true });
                }
                return originalFetch(input, init);
            };
        """
    )
    page.route("**/v1/ops/trace-list**", lambda route: pending_routes.append(route))
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/traces")

    page.locator("#trace-apply").click()
    page.wait_for_function("window.__traceSignals.length === 1")
    assert len(pending_routes) == 1
    page.evaluate("window.location.hash = '#/overview'")
    page.wait_for_function("window.__traceAbortCount === 1")
    try:
        _fulfill_json(pending_routes.pop(0), {"items": [_trace_row("late-exit")], "next_cursor": None})
    except sync_api.Error:
        pass
    assert page.locator('#trace-list-rows tr[data-trace-id="late-exit"]').count() == 0

    page.evaluate("window.location.hash = '#/traces'")
    page.locator("#trace-apply").click()
    page.wait_for_function("window.__traceSignals.length === 2")
    assert len(pending_routes) == 1
    page.locator("#scope-kb").fill("kb_changed")
    page.locator("#scope-kb").press("Tab")
    page.wait_for_function("window.__traceAbortCount === 2")
    assert page.evaluate("window.location.hash") == "#/traces"
    assert page.locator("#trace-list-status").inner_text() == "Apply filters or refresh to load traces."
    try:
        _fulfill_json(pending_routes.pop(0), {"items": [_trace_row("late-scope")], "next_cursor": None})
    except sync_api.Error:
        pass
    assert page.locator("#trace-list-rows tr[data-trace-id]").count() == 0
    assert page.locator("#trace-raw").text_content() == ""
    assert page.evaluate("window.__traceSignals.length") == 2
