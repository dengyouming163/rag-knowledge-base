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
        if trace_id == "trace-b":
            _fulfill_json(route, {"detail": "trace not found"}, status=404)
            return
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

    page.locator("#trace-outcome").select_option("error")
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
    assert second_query["outcome"] == ["succeeded"]
    assert page.locator("#trace-outcome").input_value() == "error"

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

    page.evaluate("window.location.hash = '#/traces/trace-b'")
    page.locator("#trace-detail-status").filter(has_text="unavailable").wait_for()
    assert page.locator("#trace-raw").text_content() == ""
    assert page.locator('[data-trace-summary="trace_id"]').text_content() == "trace-b"
    assert page.locator('[data-trace-summary="kb_id"]').text_content() == "kb_after_sale"
    for field in (
        "occurred_at", "operation", "mode", "model", "outcome", "hits", "citation",
        "rerank", "prompt_tokens", "completion_tokens", "total_tokens", "errors",
    ):
        assert page.locator(f'[data-trace-summary="{field}"]').text_content() == "—"
    assert page.locator("#trace-missing").is_hidden()
    assert page.locator("#trace-missing").text_content() == ""
    assert parse_qs(urlparse(detail_requests[-1]).query) == {"kb_id": ["kb_after_sale"]}

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
        assert page.locator("#trace-inspector-tab-summary").get_attribute("aria-selected") == "true"
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "trace-inspector-tab-summary"
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "trace-drawer-close"
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("document.activeElement.id") == "trace-inspector-tab-summary"
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("document.activeElement.id") == "trace-drawer-close"
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "trace-inspector-tab-summary"
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

    page.evaluate("window.location.hash = '#/overview'")
    page.locator("#view-overview").wait_for(state="visible")
    page.evaluate("window.location.hash = '#/traces'")
    page.locator("#view-traces").wait_for(state="visible")
    assert page.locator("#trace-mode-list").get_attribute("aria-selected") == "true"
    assert page.locator("#trace-list-panel").is_visible()
    assert page.locator("#trace-detail").is_hidden()


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


def test_trace_cancelled_requests_settle_and_ignore_abort_insensitive_responses(
    workbench_page,
) -> None:
    page, workbench_url = workbench_page
    page.add_init_script(
        """
            window.__tracePending = [];
            window.__traceSignals = [];
            window.__deferNextList = false;
            const originalFetch = window.fetch.bind(window);
            window.fetch = (input, init = {}) => {
                const url = String(input);
                if (url.includes('/v1/ops/trace-list')) {
                    window.__traceSignals.push(init.signal);
                    if (window.__deferNextList) {
                        window.__deferNextList = false;
                        return new Promise(resolve => {
                            window.__tracePending.push({ url, resolve });
                        });
                    }
                }
                return originalFetch(input, init);
            };
            window.__respondToStaleList = body => {
                const pending = window.__tracePending.shift();
                pending.resolve(new Response(JSON.stringify(body), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }));
            };
        """
    )
    cursor = {"before_occurred_at": "2026-11-20T09:59:59+00:00", "before_id": 99}
    page.route(
        "**/v1/ops/trace-list**",
        lambda route: _fulfill_json(
            route,
            {"items": [_trace_row("trace-a")], "next_cursor": cursor},
        ),
    )

    def detail_route(route) -> None:
        if "/trace-404/" in route.request.url:
            _fulfill_json(route, {"detail": "trace not found"}, status=404)
        elif "/trace-network/" in route.request.url:
            route.abort("failed")
        else:
            _fulfill_json(route, _trace_detail("trace-a"))

    page.route("**/v1/ops/traces/*/detail**", detail_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/traces")

    page.evaluate("window.__deferNextList = true")
    page.locator("#trace-apply").click()
    page.wait_for_function("window.__tracePending.length === 1")
    assert page.locator("#trace-list-status").inner_text() == "Loading traces…"
    page.evaluate("window.location.hash = '#/overview'")
    page.locator("#view-overview").wait_for(state="visible")
    page.wait_for_function("window.__traceSignals[0].aborted")
    assert "cancelled" in page.locator("#trace-list-status").inner_text()
    assert page.locator("#trace-load-more").is_enabled()
    page.evaluate("window.location.hash = '#/traces'")
    page.locator("#view-traces").wait_for(state="visible")
    cancelled_status = page.locator("#trace-list-status").inner_text()
    assert "Loading traces" not in cancelled_status
    page.evaluate(
        """async () => {
            window.__respondToStaleList({
                items: [{ trace_id: 'late-list', occurred_at: '2026-11-20T10:00:00Z' }],
                next_cursor: null,
            });
            await Promise.resolve();
            await Promise.resolve();
            await Promise.resolve();
        }"""
    )
    assert page.locator("#trace-list-status").inner_text() == cancelled_status
    assert page.locator('tr[data-trace-id="late-list"]').count() == 0

    page.locator("#trace-apply").click()
    page.locator('[data-trace-select="trace-a"]').wait_for(state="visible")
    page.evaluate("window.__deferNextList = true")
    page.locator("#trace-load-more").click()
    page.wait_for_function("window.__tracePending.length === 1")
    assert page.locator("#trace-load-more").is_disabled()
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', {
                configurable: true,
                value: 'hidden',
            });
            document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )
    page.wait_for_function("window.__traceSignals[2].aborted")
    cancelled_status = page.locator("#trace-list-status").inner_text()
    assert "cancelled" in cancelled_status
    assert "Loading older traces" not in cancelled_status
    assert page.locator("#trace-load-more").is_enabled()
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', {
                configurable: true,
                value: 'visible',
            });
            document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )
    page.evaluate(
        """async () => {
            window.__respondToStaleList({
                items: [{ trace_id: 'late-page', occurred_at: '2026-11-20T09:00:00Z' }],
                next_cursor: null,
            });
            await Promise.resolve();
            await Promise.resolve();
            await Promise.resolve();
        }"""
    )
    assert page.locator("#trace-list-status").inner_text() == cancelled_status
    assert page.locator('tr[data-trace-id="late-page"]').count() == 0
    assert page.locator('tr[data-trace-id="trace-a"]').count() == 1
    assert page.locator("#trace-load-more").is_enabled()

    page.evaluate("window.location.hash = '#/traces/trace-404'")
    page.locator("#trace-detail-status").filter(has_text="unavailable").wait_for()
    assert "404" in page.locator("#trace-timing-status").inner_text()
    assert "Loading timing" not in page.locator("#trace-timing-status").inner_text()
    page.evaluate("window.location.hash = '#/traces/trace-network'")
    page.locator("#trace-detail-status").filter(has_text="unavailable").wait_for()
    assert "unavailable" in page.locator("#trace-timing-status").inner_text()
    assert "Loading timing" not in page.locator("#trace-timing-status").inner_text()


def test_trace_delayed_clipboard_cannot_overwrite_trace_or_scope_changes(
    workbench_page,
) -> None:
    page, workbench_url = workbench_page
    page.route(
        "**/v1/ops/traces/*/detail**",
        lambda route: _fulfill_json(
            route,
            _trace_detail(urlparse(route.request.url).path.rsplit("/", 2)[-2]),
        ),
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/traces/trace-a")
    page.locator("#trace-raw").filter(has_text="trace-a").wait_for(state="attached")
    page.locator("#trace-inspector-tab-raw").click()
    page.evaluate(
        """
            window.__clipboardWrites = [];
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: {
                    writeText(value) {
                        let resolve;
                        let reject;
                        const promise = new Promise((resolvePromise, rejectPromise) => {
                            resolve = resolvePromise;
                            reject = rejectPromise;
                        });
                        const write = { value, resolve, reject, settled: false };
                        window.__clipboardWrites.push(write);
                        return promise.finally(() => { write.settled = true; });
                    },
                },
            });
        """
    )

    page.locator("#trace-copy").click()
    page.wait_for_function("window.__clipboardWrites.length === 1")
    page.evaluate("window.location.hash = '#/traces/trace-b'")
    page.locator("#trace-raw").filter(has_text="trace-b").wait_for()
    selected_status = page.locator("#trace-copy-status").inner_text()
    page.evaluate("window.__clipboardWrites[0].resolve()")
    page.wait_for_function("window.__clipboardWrites[0].settled")
    assert page.locator("#trace-copy-status").inner_text() == selected_status
    assert "Copied" not in selected_status

    page.locator("#trace-copy").click()
    page.wait_for_function("window.__clipboardWrites.length === 2")
    page.locator("#scope-kb").fill("kb_changed")
    page.locator("#scope-kb").press("Tab")
    page.locator("#trace-list-status").filter(has_text="Apply filters").wait_for()
    cleared_status = page.locator("#trace-copy-status").inner_text()
    page.evaluate("window.__clipboardWrites[1].reject(new Error('permission denied'))")
    page.wait_for_function("window.__clipboardWrites[1].settled")
    assert page.locator("#trace-copy-status").inner_text() == cleared_status
    assert "Copy failed" not in cleared_status


def test_workbench_ingestion_evaluation_and_permission_markers(client: TestClient) -> None:
    html = client.get("/ui/workbench.html").text
    ingestion = html.split('<section id="view-ingestion"', 1)[1].split("</section>", 1)[0]
    evaluation = html.split('<section id="view-evaluation"', 1)[1].split("</section>", 1)[0]

    for element_id in (
        "ingestion-load", "ingestion-window", "ingestion-jobs-status",
        "ingestion-job-rows", "ingestion-job-detail", "ingestion-job-events-status",
        "ingestion-job-events", "ingestion-retry", "ingestion-permission-status",
    ):
        assert f'id="{element_id}"' in ingestion
        assert html.count(f'id="{element_id}"') == 1
    for heading in ("Job", "Document", "Status", "Wait", "Duration", "Progress", "Error"):
        assert heading in ingestion
    assert "disabled" in ingestion.split('id="ingestion-window"', 1)[1].split(">", 1)[0]
    assert "not supported" in ingestion

    for element_id in (
        "evaluation-load", "evaluation-window", "evaluation-runs-status",
        "evaluation-run-rows", "evaluation-run-detail", "evaluation-run-output",
        "evaluation-action-status", "evaluation-permission-status",
    ):
        assert f'id="{element_id}"' in evaluation
        assert html.count(f'id="{element_id}"') == 1
    triggers = {
        "run-trigger-eval-mock": "eval_mock",
        "run-trigger-loadtest": "loadtest",
        "run-trigger-drill-parser": "drill_parser",
        "run-trigger-drill-llm": "drill_llm",
        "run-trigger-drill-queue": "drill_queue",
    }
    for element_id, kind in triggers.items():
        assert f'id="{element_id}"' in evaluation
        assert f'data-kind="{kind}"' in evaluation
    for key in (
        "recall_at_k", "mrr", "hit_rate", "refusal_accuracy",
        "answer_coverage", "citation_accuracy",
    ):
        assert f'data-run-metric="{key}"' in evaluation
        assert f'data-run-delta="{key}"' in evaluation
    for field in (
        "kind", "status", "requested_at", "started_at", "finished_at",
        "exit_code", "artifact_path", "error",
    ):
        assert f'data-run-field="{field}"' in evaluation
    assert "disabled" in evaluation.split('id="evaluation-window"', 1)[1].split(">", 1)[0]
    assert "not supported" in evaluation

    app_module = client.get("/ui/js/app.mjs").text
    assert 'from "./permissions.mjs"' in app_module
    assert 'from "./views/ingestion.mjs"' in app_module
    assert 'from "./views/evaluation.mjs"' in app_module
    permissions = client.get("/ui/js/permissions.mjs")
    ingestion_module = client.get("/ui/js/views/ingestion.mjs")
    evaluation_module = client.get("/ui/js/views/evaluation.mjs")
    assert permissions.status_code == 200
    assert ingestion_module.status_code == 200
    assert evaluation_module.status_code == 200
    for marker in ("canActions", "canRetry", "canCompare", "/v1/identity", "/v1/ops/permissions"):
        assert marker in permissions.text
    for kind in triggers.values():
        assert f'triggerRun("{kind}")' in evaluation_module.text


def _task14_identity(*, admin: bool = True, actions: bool = True) -> dict:
    role = "admin" if admin else "member"
    return {
        "tenant": "demo", "user": "demo_user", "role": role,
        "tags": [], "is_admin": admin, "kbs": ["kb_after_sale"],
        "isolation": {"tenant_scope": True, "acl": "admin_bypass", "rls": {}},
        "ops": {"actions_enabled": actions, "can_trigger": admin and actions},
    }


def _task14_permissions(*, admin: bool = True) -> dict:
    return {
        "tenant": "demo", "user": "demo_user", "role": "admin" if admin else "member",
        "tags": [], "is_admin": admin,
        "kb": {"id": "kb_after_sale", "exists": True, "tenant_match": True},
        "acl": {"mode": "admin_bypass", "user_tags": []},
        "rls": {"enabled": True, "scoped_count": 1, "bare_count": 0},
        "decision": "allow", "reason": "allowed",
    }


def _install_task14_permissions(page, state: dict) -> None:
    def identity_route(route) -> None:
        mode = state.get("identity", "admin")
        if mode == "error":
            _fulfill_json(route, {"detail": "identity exploded"}, status=500)
        else:
            _fulfill_json(route, _task14_identity(admin=mode != "member", actions=mode != "disabled"))

    def permissions_route(route) -> None:
        mode = state.get("permissions", "admin")
        if mode == "error":
            _fulfill_json(route, {"detail": "permission exploded"}, status=500)
        else:
            _fulfill_json(route, _task14_permissions(admin=mode != "member"))

    page.route("**/v1/identity", identity_route)
    page.route("**/v1/ops/permissions**", permissions_route)


def _task14_job(job_id: int, status: str = "failed") -> dict:
    return {
        "id": job_id, "tenant_id": "demo", "kb_id": "kb_after_sale",
        "doc_id": f"doc_{job_id}", "status": status,
        "error": "parser failed" if status == "failed" else "",
        "source": "upload", "attempts": 2, "progress": 80 if status == "failed" else 25,
        "created_at": "2026-11-20T08:00:00Z", "started_at": "2026-11-20T08:00:01Z",
        "finished_at": "2026-11-20T08:00:03Z" if status == "failed" else None,
        "wait_seconds": 1.25, "duration_seconds": 2.5 if status == "failed" else None,
    }


def test_ingestion_jobs_events_retry_permissions_and_partial_failures(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    permission_state = {"identity": "admin", "permissions": "admin"}
    job_state = {"jobs": "ok", "events": "ok", "retry": 202}
    job_requests: list[str] = []
    event_requests: list[str] = []
    retry_requests: list[str] = []
    _install_task14_permissions(page, permission_state)

    def jobs_route(route) -> None:
        job_requests.append(route.request.url)
        if job_state["jobs"] == "error":
            _fulfill_json(route, {"detail": "jobs exploded"}, status=500)
            return
        malformed = _task14_job(13, "running")
        malformed.update({"wait_seconds": -4, "duration_seconds": "bad", "progress": None})
        _fulfill_json(route, {
            "jobs": [_task14_job(11), _task14_job(12, "running"), malformed],
            "counts": {"queued": 0, "running": 2, "succeeded": 0, "failed": 1},
        })

    def events_route(route) -> None:
        event_requests.append(route.request.url)
        if job_state["events"] == "error":
            _fulfill_json(route, {"detail": "events exploded"}, status=500)
            return
        if job_state["events"] == "empty":
            _fulfill_json(route, {"job_id": 11, "events": []})
            return
        _fulfill_json(route, {"job_id": 11, "events": [{
            "id": 1, "job_id": 11, "stage": "parsing", "status": "failed",
            "progress": 80, "message": '<img src=x onerror="window.__task14Xss=1">',
            "created_at": "2026-11-20T08:00:03Z",
        }]})

    def retry_route(route) -> None:
        retry_requests.append(route.request.url)
        status = job_state["retry"]
        detail = {409: "job is not failed", 403: "admin required", 404: "job not found"}.get(status, "")
        _fulfill_json(route, {"job_id": 11, "status": "queued"} if status == 202 else {"detail": detail}, status=status)

    page.route("**/v1/ops/jobs?*", jobs_route)
    page.route("**/v1/ops/jobs/*/events", events_route)
    page.route("**/v1/ops/jobs/*/retry", retry_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/ingestion")
    assert job_requests == []
    page.locator("#ingestion-load").click()
    page.locator('[data-job-select="11"]').wait_for(state="visible")
    assert parse_qs(urlparse(job_requests[0]).query) == {"kb_id": ["kb_after_sale"], "limit": ["50"]}
    assert "window" not in job_requests[0]
    assert page.locator("#ingestion-window").is_disabled()
    assert "not supported" in page.locator("#ingestion-window-note").inner_text()
    assert page.locator('[data-job-field="wait"][data-job-id="11"]').inner_text() == "1.25 s"
    assert page.locator('[data-job-field="duration"][data-job-id="11"]').inner_text() == "2.50 s"
    assert page.locator('[data-job-field="progress"][data-job-id="13"]').inner_text() == "—"
    assert "NaN" not in page.locator("#view-ingestion").inner_text()
    assert "Infinity" not in page.locator("#view-ingestion").inner_text()

    page.locator('[data-job-select="11"]').focus()
    page.keyboard.press("Enter")
    page.locator("#ingestion-job-events").filter(has_text="parsing").wait_for()
    assert event_requests[-1].endswith("/v1/ops/jobs/11/events")
    assert page.locator("#ingestion-job-events img").count() == 0
    assert page.evaluate("window.__task14Xss") is None
    assert page.locator("#ingestion-retry").is_enabled()
    assert page.locator("#ingestion-retry").get_attribute("title") == "Retry failed job #11"
    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="reloaded").wait_for()
    assert len(retry_requests) == 1
    assert len(job_requests) == 2
    assert len(event_requests) == 2

    job_state["retry"] = 409
    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="已有运行中的任务").wait_for()
    assert page.locator("#ingestion-retry").is_enabled()
    job_state["retry"] = 403
    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="admin required").wait_for()
    assert page.locator("#ingestion-retry").is_enabled()

    job_state["events"] = "error"
    page.locator('[data-job-select="12"]').click()
    page.locator("#ingestion-job-events-status").filter(has_text="events exploded").wait_for()
    assert page.locator('[data-job-select="11"]').is_visible()
    job_state["events"] = "ok"
    job_state["jobs"] = "error"
    page.locator("#ingestion-load").click()
    page.locator("#ingestion-jobs-status").filter(has_text="jobs exploded").wait_for()
    assert "Select a job" in page.locator("#ingestion-job-events-status").inner_text() or "events" in page.locator("#ingestion-job-events-status").inner_text()
    assert "succeeded" not in page.locator("#ingestion-jobs-status").inner_text()

    job_state["jobs"] = "ok"
    job_state["retry"] = 404
    page.locator("#ingestion-load").click()
    page.locator('[data-job-select="11"]').click()
    page.locator("#ingestion-job-events").filter(has_text="parsing").wait_for()
    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="no longer visible").wait_for()
    assert page.locator('[data-job-select="11"]').count() == 0
    assert page.locator("#ingestion-retry").is_hidden()

    permission_state.update(identity="member", permissions="member")
    page.locator("#scope-user").fill("member_user")
    page.locator("#scope-user").press("Tab")
    page.locator("#scope-refresh").click()
    page.locator('[data-job-select="11"]').click()
    assert page.locator("#ingestion-retry").is_disabled()
    assert "Administrator role required" in page.locator("#ingestion-permission-status").inner_text()
    permission_state.update(identity="disabled", permissions="admin")
    page.locator("#scope-user").fill("demo_user")
    page.locator("#scope-user").press("Tab")
    page.locator("#scope-refresh").click()
    page.locator('[data-job-select="11"]').click()
    assert "OPS_ENABLE_ACTIONS" in page.locator("#ingestion-permission-status").inner_text()


def _task14_metrics(base: float) -> dict:
    return {
        "recall_at_k": base,
        "mrr": base - 0.1,
        "hit_rate": base + 0.1,
        "refusal_accuracy": base - 0.2,
        "answer_coverage": base + 0.05,
        "citation_accuracy": base - 0.05,
    }


def _task14_run(run_id: int, kind: str = "eval_mock", status: str = "succeeded", base: float = 0.8) -> dict:
    return {
        "id": run_id, "tenant_id": "demo", "kb_id": "kb_after_sale",
        "kind": kind, "status": status, "params": {}, "requested_by": "demo_user",
        "requested_at": "2026-11-20T08:00:00Z", "started_at": "2026-11-20T08:00:01Z",
        "finished_at": "2026-11-20T08:00:03Z" if status in ("succeeded", "completed") else None,
        "exit_code": 0 if status in ("succeeded", "completed") else None,
        "summary": {"metrics": _task14_metrics(base), "counts": {"cases": 10}},
        "artifact_path": f"data/ops/eval/{run_id}.json", "attempts": 1,
        "output_tail": '<img src=x onerror="window.__task14Xss=1">',
        "error": "",
    }


def _install_task14_runs(page, state: dict, requests: dict) -> None:
    def runs_route(route) -> None:
        requests.setdefault("history", []).append(route.request.url)
        if state.get("history") == "error":
            _fulfill_json(route, {"detail": "runs exploded"}, status=500)
            return
        _fulfill_json(route, {"runs": [
            _task14_run(30, base=0.8),
            _task14_run(29, status="failed", base=0.99),
            _task14_run(28, kind="loadtest", base=0.7),
            _task14_run(27, base=0.6),
            _task14_run(26, status="completed", base=0.4),
        ]})

    def detail_route(route) -> None:
        run_id = int(urlparse(route.request.url).path.rsplit("/", 1)[-1])
        requests.setdefault("details", []).append(run_id)
        if run_id in state.get("detail_errors", {}):
            _fulfill_json(route, {"detail": "detail exploded"}, status=state["detail_errors"][run_id])
            return
        base = {30: 0.8, 29: 0.99, 28: 0.7, 27: 0.6, 26: 0.4}.get(run_id, 0.5)
        kind = "loadtest" if run_id == 28 else "eval_mock"
        status = "failed" if run_id == 29 else "succeeded"
        _fulfill_json(route, _task14_run(run_id, kind=kind, status=status, base=base))

    page.route("**/v1/ops/runs?*", runs_route)
    page.route("**/v1/ops/runs/*", detail_route)


def test_evaluation_history_detail_metrics_same_kind_delta_and_local_errors(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    permission_state = {"identity": "admin", "permissions": "admin"}
    run_state = {"history": "ok", "detail_errors": {}}
    requests: dict = {}
    _install_task14_permissions(page, permission_state)
    _install_task14_runs(page, run_state, requests)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/evaluation")
    assert requests == {}
    page.locator("#evaluation-load").click()
    page.locator('[data-run-select="30"]').wait_for(state="visible")
    assert parse_qs(urlparse(requests["history"][0]).query) == {"kb_id": ["kb_after_sale"], "limit": ["20"]}
    page.locator('[data-run-select="30"]').focus()
    page.keyboard.press("Enter")
    page.locator('[data-run-field="kind"]').filter(has_text="eval_mock").wait_for()
    assert requests["details"] == [30]
    for field in ("status", "requested_at", "started_at", "finished_at", "exit_code", "artifact_path", "error"):
        assert page.locator(f'[data-run-field="{field}"]').inner_text() != ""
    expected_deltas = {
        "recall_at_k": "+0.2000", "mrr": "+0.2000", "hit_rate": "+0.2000",
        "refusal_accuracy": "+0.2000", "answer_coverage": "+0.2000", "citation_accuracy": "+0.2000",
    }
    for key, delta in expected_deltas.items():
        assert page.locator(f'[data-run-metric="{key}"]').inner_text() != "—"
        assert page.locator(f'[data-run-delta="{key}"]').inner_text() == delta
    assert "29" not in page.locator("#evaluation-delta-context").inner_text()
    assert "27" in page.locator("#evaluation-delta-context").inner_text()
    assert page.locator("#evaluation-run-output img").count() == 0
    assert page.evaluate("window.__task14Xss") is None

    run_state["history"] = "error"
    page.locator("#evaluation-load").click()
    page.locator("#evaluation-runs-status").filter(has_text="runs exploded").wait_for()
    assert page.locator('[data-run-field="kind"]').inner_text() == "eval_mock"
    assert "NaN" not in page.locator("#view-evaluation").inner_text()
    run_state["history"] = "ok"
    page.locator("#evaluation-load").click()
    run_state["detail_errors"] = {27: 500}
    page.locator('[data-run-select="27"]').click()
    page.locator("#evaluation-detail-status").filter(has_text="detail exploded").wait_for()
    assert page.locator('[data-run-select="30"]').is_visible()
    run_state["detail_errors"] = {27: 404}
    page.locator('[data-run-select="27"]').click()
    page.locator("#evaluation-detail-status").filter(has_text="no longer visible").wait_for()
    assert page.locator('[data-run-select="27"]').count() == 0

    permission_state["identity"] = "error"
    page.locator("#scope-user").fill("unknown_user")
    page.locator("#scope-user").press("Tab")
    page.locator("#scope-refresh").click()
    page.locator("#evaluation-permission-status").filter(has_text="identity exploded").wait_for()
    status = page.locator("#evaluation-permission-status").inner_text()
    assert "identity exploded" in status
    assert "Identity unavailable" in status
    assert "Administrator role required" not in status
    assert page.locator("#scope-kb").input_value() == "kb_after_sale"
    permission_state.update(identity="admin", permissions="error")
    page.locator("#scope-user").fill("demo_user")
    page.locator("#scope-user").press("Tab")
    page.locator("#scope-refresh").click()
    page.locator("#evaluation-permission-status").filter(has_text="permission exploded").wait_for()
    status = page.locator("#evaluation-permission-status").inner_text()
    assert "permission exploded" in status
    assert "Knowledge-base permissions unavailable" in status


def test_evaluation_explicit_triggers_202_409_and_mobile_desktop_geometry(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    permission_state = {"identity": "admin", "permissions": "admin"}
    trigger_state = {"mode": "accepted"}
    posted: list[dict] = []
    requested_details: list[int] = []
    _install_task14_permissions(page, permission_state)
    page.route("**/v1/ops/runs?*", lambda route: _fulfill_json(route, {"runs": []}))
    kind_ids = {"eval_mock": 101, "loadtest": 102, "drill_parser": 103, "drill_llm": 104, "drill_queue": 105}

    def trigger_route(route) -> None:
        body = route.request.post_data_json
        posted.append({"body": body, "headers": route.request.headers, "url": route.request.url})
        if trigger_state["mode"] == "conflict":
            _fulfill_json(route, {"detail": {"message": "run already active", "run_id": 88}}, status=409)
        elif trigger_state["mode"] == "missing":
            _fulfill_json(route, {"status": "queued"}, status=202)
        else:
            _fulfill_json(route, {"run_id": kind_ids[body["kind"]], "status": "queued"}, status=202)

    def detail_route(route) -> None:
        run_id = int(urlparse(route.request.url).path.rsplit("/", 1)[-1])
        requested_details.append(run_id)
        kind = next(key for key, value in kind_ids.items() if value == run_id)
        _fulfill_json(route, _task14_run(run_id, kind=kind, base=0.5))

    page.route("**/v1/ops/kbs/*/runs", trigger_route)
    page.route("**/v1/ops/runs/*", detail_route)
    triggers = {
        "run-trigger-eval-mock": "eval_mock", "run-trigger-loadtest": "loadtest",
        "run-trigger-drill-parser": "drill_parser", "run-trigger-drill-llm": "drill_llm",
        "run-trigger-drill-queue": "drill_queue",
    }
    for width in (1440, 390):
        posted.clear()
        requested_details.clear()
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(f"{workbench_url}#/evaluation")
        page.locator("#evaluation-load").click()
        for button_id, kind in triggers.items():
            button = page.locator(f"#{button_id}")
            assert button.get_attribute("data-kind") == kind
            button.scroll_into_view_if_needed()
            button.click()
            page.locator("#evaluation-action-status").filter(has_text="terminal").wait_for()
            assert posted[-1]["body"] == {"kind": kind, "params": {}}
            assert "tenant" not in posted[-1]["body"]
            assert posted[-1]["headers"]["x-tenant-id"] == "demo"
            assert posted[-1]["url"].endswith("/v1/ops/kbs/kb_after_sale/runs")
            assert requested_details[-1] == kind_ids[kind]
            assert page.locator('[data-run-field="kind"]').inner_text() == kind
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

        trigger_state["mode"] = "conflict"
        page.locator("#run-trigger-eval-mock").click()
        page.locator("#evaluation-action-status").filter(has_text="already active").wait_for()
        for button_id in triggers:
            assert page.locator(f"#{button_id}").is_enabled()
        trigger_state["mode"] = "missing"
        page.locator("#run-trigger-loadtest").click()
        page.locator("#evaluation-action-status").filter(has_text="valid run id").wait_for()
        assert all(run_id is not None for run_id in requested_details)
        assert not page.evaluate("window.location.href.includes('undefined')")
        trigger_state["mode"] = "accepted"


def test_evaluation_poll_stops_on_exit_scope_hidden_and_destroy_ignores_late_responses(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    page.route("**/v1/ops/runs?*", lambda route: _fulfill_json(route, {"runs": []}))
    page.route("**/v1/ops/kbs/*/runs", lambda route: _fulfill_json(route, {"run_id": 101, "status": "queued"}, status=202))
    detail_calls = {"count": 0}
    page.add_init_script("""
        window.__task14Signals = [];
        window.__task14Aborts = 0;
        const originalFetch = window.fetch.bind(window);
        window.fetch = (input, init = {}) => {
            if (String(input).includes('/v1/ops/')) {
                window.__task14Signals.push(init.signal);
                init.signal?.addEventListener('abort', () => { window.__task14Aborts += 1; }, { once: true });
            }
            return originalFetch(input, init);
        };
    """)

    def detail_route(route) -> None:
        detail_calls["count"] += 1
        status = "succeeded" if detail_calls["count"] >= 3 else "running"
        _fulfill_json(route, _task14_run(101, status=status))

    page.route("**/v1/ops/runs/*", detail_route)
    page.goto(f"{workbench_url}#/evaluation")
    page.locator("#evaluation-load").click()
    page.locator("#run-trigger-eval-mock").click()
    page.locator("#evaluation-action-status").filter(has_text="terminal").wait_for(timeout=10000)
    assert detail_calls["count"] == 3
    polls_after_terminal = detail_calls["count"]
    page.wait_for_timeout(1200)
    assert detail_calls["count"] == polls_after_terminal

    detail_calls["count"] = 0
    page.locator("#run-trigger-eval-mock").click()
    page.locator('[data-run-field="status"]').filter(has_text="running").wait_for()
    page.evaluate("window.location.hash = '#/overview'")
    page.locator("#view-overview").wait_for(state="visible")
    page.wait_for_function("window.__task14Aborts >= 1")
    calls_after_exit = detail_calls["count"]
    page.wait_for_timeout(1200)
    assert detail_calls["count"] == calls_after_exit

    page.evaluate("window.location.hash = '#/evaluation'")
    page.locator("#scope-refresh").click()
    detail_calls["count"] = 0
    page.locator("#run-trigger-eval-mock").click()
    page.locator('[data-run-field="status"]').filter(has_text="running").wait_for()
    page.evaluate("""
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
        document.dispatchEvent(new Event('visibilitychange'));
    """)
    calls_after_hide = detail_calls["count"]
    page.wait_for_timeout(1200)
    assert detail_calls["count"] == calls_after_hide
    page.evaluate("""
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
        document.dispatchEvent(new Event('visibilitychange'));
        window.location.hash = '#/overview';
    """)

    # Public destroy must abort even an abort-insensitive API and ignore its late 202.
    result = page.evaluate("""async () => {
        const { ScopeGuard } = await import('/ui/js/core.mjs');
        const { createEvaluation } = await import('/ui/js/views/evaluation.mjs');
        const guard = new ScopeGuard();
        const calls = [];
        let resolveTrigger;
        const pending = new Promise(resolve => { resolveTrigger = resolve; });
        const controller = createEvaluation({
            root: document.createElement('section'), guard,
            scope: { tenant: 'demo', kb: 'kb_after_sale', user: 'demo_user', window: '24h' },
            api: (path, options) => {
                calls.push({ path, signal: options.signal });
                if (options.method === 'POST') return pending;
                return Promise.resolve({ id: 101, kind: 'eval_mock', status: 'succeeded', summary: {} });
            },
        });
        controller.setActive(true);
        const triggering = controller.triggerRun('eval_mock');
        controller.destroy();
        const aborted = calls.every(call => call.signal?.aborted);
        resolveTrigger({ run_id: 101, status: 'queued' });
        await triggering;
        await Promise.resolve();
        return { calls: calls.map(call => call.path), aborted };
    }""")
    assert result["aborted"] is True
    assert result["calls"] == ["/v1/ops/kbs/kb_after_sale/runs"]


def test_ingestion_late_jobs_response_cannot_cross_scope(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    pending = []
    page.route("**/v1/ops/jobs?*", lambda route: pending.append(route))
    page.goto(f"{workbench_url}#/ingestion")
    page.locator("#ingestion-load").click()
    page.wait_for_function("document.querySelector('#ingestion-jobs-status').textContent.includes('Loading')")
    page.locator("#scope-kb").fill("kb_changed")
    page.locator("#scope-kb").press("Tab")
    assert page.locator("#ingestion-jobs-status").inner_text() == "Load jobs or refresh to inspect ingestion."
    assert len(pending) == 1
    try:
        _fulfill_json(pending.pop(0), {"jobs": [_task14_job(999)], "counts": {}})
    except Exception:
        pass
    assert page.locator('[data-job-select="999"]').count() == 0
    assert page.locator("#ingestion-job-events li").count() == 0
