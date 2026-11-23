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


NAV = ("overview", "lab", "traces", "ingestion", "evaluation", "access", "audit", "storage")
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
            yield page, f"http://127.0.0.1:{server.server_port}/ui/?lang=en"
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_workbench_shell(client: TestClient) -> None:
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert '<title data-i18n="app.title">RAG Operations Console</title>' in html
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
    html = client.get("/ui/").text
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
    html = client.get("/ui/").text
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


def test_trace_id_pattern_is_valid_in_chromium(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.goto(f"{workbench_url}#/traces")
    trace_id = page.locator("#trace-id")
    trace_id.wait_for(state="visible")

    trace_id.fill("trace_a:b.c-d")
    assert trace_id.evaluate("input => input.checkValidity()")
    trace_id.fill("trace/invalid")
    assert not trace_id.evaluate("input => input.checkValidity()")


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
        chat_button = page.locator("#chat-button")
        chat_button.scroll_into_view_if_needed()
        visible = chat_button.bounding_box()
        assert visible is not None, (width, geometry)
        assert 0 <= visible["y"], (width, visible)
        assert visible["y"] + visible["height"] <= 901, (width, visible)
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
    html = client.get("/ui/").text
    rest = html.split('<section id="view-traces"', 1)[1]
    traces = rest.split("\n        </section>", 1)[0]
    assert traces != rest, "traces: 8-space </section> terminator not found"

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

    headings = re.findall(r'<th scope="col"(?: data-i18n="[^"]+")?>([^<]+)</th>', traces)
    assert headings == ["Time", "Trace", "Operation", "Outcome", "Stages", "Total", "Hits", "Token"]
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

    header = page.locator(".wb-trace-table thead th").all_inner_texts()
    assert len(header) == 8
    assert header[-1] == "Token"
    trace_a_cells = page.locator('tr[data-trace-id="trace-a"] td')
    assert trace_a_cells.last.inner_text() == "18"

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
    assert "Not found" in page.locator("#trace-timing-status").inner_text()
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
    html = client.get("/ui/").text
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
    page.locator("#ingestion-action-status").filter(has_text="A job is already running").wait_for()
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


def test_review_evaluation_trigger_accepts_only_http_202(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(5000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    page.route("**/v1/ops/runs?*", lambda route: _fulfill_json(route, {"runs": []}))
    page.route(
        "**/v1/ops/kbs/*/runs",
        lambda route: _fulfill_json(route, {"run_id": 88, "status": "queued"}, status=200),
    )
    detail_calls = []
    page.route("**/v1/ops/runs/*", lambda route: detail_calls.append(route.request.url))
    page.goto(f"{workbench_url}#/evaluation")
    page.locator("#evaluation-load").click()
    page.locator("#run-trigger-eval-mock").click()

    page.locator("#evaluation-action-status").filter(has_text="Expected HTTP 202").wait_for()
    assert "HTTP 200" in page.locator("#evaluation-action-status").inner_text()
    assert page.locator("#run-trigger-eval-mock").is_enabled()
    page.wait_for_timeout(700)
    assert detail_calls == []


def test_review_ingestion_retry_accepts_only_http_202_without_reloading(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(5000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    calls = {"jobs": 0, "events": 0}

    def jobs_route(route) -> None:
        calls["jobs"] += 1
        _fulfill_json(route, {"jobs": [_task14_job(11)], "counts": {"failed": 1}})

    def events_route(route) -> None:
        calls["events"] += 1
        _fulfill_json(route, {"job_id": 11, "events": [{"message": "failed"}], "counts": {}})

    page.route("**/v1/ops/jobs?*", jobs_route)
    page.route("**/v1/ops/jobs/11/events", events_route)
    page.route(
        "**/v1/ops/jobs/11/retry",
        lambda route: _fulfill_json(route, {"job_id": 11, "status": "queued"}, status=200),
    )
    page.goto(f"{workbench_url}#/ingestion")
    page.locator("#ingestion-load").click()
    page.locator('[data-job-select="11"]').click()
    page.locator("#ingestion-job-events li").wait_for()
    page.locator("#ingestion-retry").click()

    page.locator("#ingestion-action-status").filter(has_text="Expected HTTP 202").wait_for()
    assert "HTTP 200" in page.locator("#ingestion-action-status").inner_text()
    assert page.locator("#ingestion-retry").is_enabled()
    page.wait_for_timeout(700)
    assert calls == {"jobs": 1, "events": 1}


def test_review_evaluation_rejects_non_scalar_ids_and_metric_values(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(5000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    invalid_metrics = {
        "recall_at_k": True,
        "mrr": [],
        "hit_rate": [0.8],
        "refusal_accuracy": {"value": 0.8},
        "answer_coverage": "Infinity",
        "citation_accuracy": "0.8",
    }
    current_run = _task14_run(30)
    current_run["summary"]["metrics"] = invalid_metrics
    history = [
        {**_task14_run(31), "id": True},
        {**_task14_run(32), "id": [32]},
        {**_task14_run(33), "id": {"value": 33}},
        {**_task14_run(34), "id": None},
        current_run,
        _task14_run(27, base=0.6),
    ]
    page.route("**/v1/ops/runs?*", lambda route: _fulfill_json(route, {"runs": history}))
    detail_calls = []
    trigger_ids = iter([True, [], {"value": 46}, None, "45"])

    def trigger_route(route) -> None:
        _fulfill_json(route, {"run_id": next(trigger_ids), "status": "queued"}, status=202)

    def detail_route(route) -> None:
        detail_calls.append(route.request.url)
        run_id = int(route.request.url.rsplit("/", 1)[-1])
        detail = _task14_run(run_id)
        detail["summary"]["metrics"] = invalid_metrics
        _fulfill_json(route, detail)

    page.route("**/v1/ops/kbs/*/runs", trigger_route)
    page.route("**/v1/ops/runs/*", detail_route)
    page.goto(f"{workbench_url}#/evaluation")
    page.locator("#evaluation-load").click()
    page.locator('[data-run-select="30"]').wait_for()
    assert page.locator("[data-run-select]").count() == 2
    page.locator('[data-run-select="30"]').click()
    page.locator('[data-run-field="status"]').filter(has_text="succeeded").wait_for()

    for metric in ["recall_at_k", "mrr", "hit_rate", "refusal_accuracy", "answer_coverage"]:
        assert page.locator(f'[data-run-metric="{metric}"]').inner_text() == "—"
        assert page.locator(f'[data-run-delta="{metric}"]').inner_text() == "—"
    assert page.locator('[data-run-metric="citation_accuracy"]').inner_text() == "0.8000"
    assert page.locator('[data-run-delta="citation_accuracy"]').inner_text() == "+0.2500"
    detail_text = page.locator("#evaluation-run-detail").inner_text()
    assert "NaN" not in detail_text
    assert "Infinity" not in detail_text
    detail_count_before_triggers = len(detail_calls)

    for invalid_id in [True, [], {}, None]:
        page.locator("#run-trigger-eval-mock").click()
        page.locator("#evaluation-action-status").filter(has_text="valid run id").wait_for()
        assert page.locator("#run-trigger-eval-mock").is_enabled()
        assert len(detail_calls) == detail_count_before_triggers, invalid_id
    page.locator("#run-trigger-eval-mock").click()
    page.locator("#evaluation-action-status").filter(has_text="terminal").wait_for()
    assert len(detail_calls) == detail_count_before_triggers + 1
    assert detail_calls[-1].endswith("/v1/ops/runs/45")


def test_review_ingestion_rejects_non_scalar_job_ids(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(5000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    jobs = [
        {**_task14_job(12), "id": True},
        {**_task14_job(13), "id": [13]},
        {**_task14_job(14), "id": {"value": 14}},
        {**_task14_job(15), "id": None},
        _task14_job(11),
    ]
    page.route("**/v1/ops/jobs?*", lambda route: _fulfill_json(route, {"jobs": jobs, "counts": {}}))
    page.route("**/v1/ops/jobs/*/events", lambda route: _fulfill_json(route, {"events": []}))
    page.goto(f"{workbench_url}#/ingestion")
    page.locator("#ingestion-load").click()
    page.locator('[data-job-select="11"]').wait_for()

    assert page.locator("[data-job-select]").count() == 1
    assert page.locator("[data-job-select='1']").count() == 0
    assert page.locator("#ingestion-job-rows").inner_text().find("NaN") == -1


def test_review_late_trigger_and_retry_finalizers_cannot_overwrite_new_scope(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.goto(f"{workbench_url}#/overview")
    result = page.evaluate("""async () => {
        const { ScopeGuard } = await import('/ui/js/core.mjs');
        const { createEvaluation } = await import('/ui/js/views/evaluation.mjs');
        const { createIngestion } = await import('/ui/js/views/ingestion.mjs');
        const permissions = {
            canActions: () => true,
            canRetry: () => true,
            actionReason: () => 'STALE FINALIZER OVERWRITE',
            retryReason: () => 'STALE FINALIZER OVERWRITE',
        };
        const permissionRoot = () => {
            const root = document.createElement('section');
            const status = document.createElement('p');
            status.id = 'evaluation-permission-status';
            root.appendChild(status);
            return { root, status };
        };
        const changeScope = (guard, kb) => {
            guard.invalidate();
            const scope = { tenant: 'tenant_a', kb, user: 'new_scope_user', window: '24h' };
            window.dispatchEvent(new CustomEvent('workbuddy:scopechange', {
                detail: { scope, ...guard.snapshot() },
            }));
        };

        const evaluationFixture = permissionRoot();
        const evaluationGuard = new ScopeGuard();
        let resolveTrigger;
        const triggerPending = new Promise(resolve => { resolveTrigger = resolve; });
        const evaluation = createEvaluation({
            root: evaluationFixture.root,
            guard: evaluationGuard,
            scope: { tenant: 'tenant_a', kb: 'kb_after_sale', user: 'demo_user', window: '24h' },
            permissions,
            api: () => triggerPending,
        });
        evaluation.setActive(true);
        const triggering = evaluation.triggerRun('eval_mock');
        changeScope(evaluationGuard, 'kb_new_evaluation');
        const evaluationExpected = evaluationFixture.status.textContent;
        resolveTrigger({ run_id: 101, status: 'queued' });
        await triggering;
        await Promise.resolve();
        const evaluationSafe = evaluationExpected === 'Permissions are checked when runs load.'
            && evaluationFixture.status.textContent === evaluationExpected;
        evaluation.destroy();

        const ingestionRoot = document.createElement('section');
        const ingestionStatus = document.createElement('p');
        ingestionStatus.id = 'ingestion-permission-status';
        ingestionRoot.appendChild(ingestionStatus);
        const ingestionGuard = new ScopeGuard();
        const ingestionScope = { tenant: 'tenant_a', kb: 'kb_after_sale', user: 'demo_user', window: '24h' };
        const ingestionContext = { scope: ingestionScope, ...ingestionGuard.snapshot() };
        let resolveRetry;
        const retryPending = new Promise(resolve => { resolveRetry = resolve; });
        const ingestion = createIngestion({
            root: ingestionRoot,
            guard: ingestionGuard,
            scope: ingestionScope,
            permissions,
            api: path => {
                if (path.includes('/retry')) return retryPending;
                if (path.includes('/events')) return Promise.resolve({ job_id: 11, events: [] });
                return Promise.resolve({ jobs: [{ id: 11, status: 'failed', progress: 1 }], counts: {} });
            },
        });
        ingestion.setActive(true);
        await ingestion.loadJobs(ingestionContext);
        await ingestion.selectJob(11, ingestionContext);
        const retrying = ingestion.retrySelectedJob(ingestionContext);
        changeScope(ingestionGuard, 'kb_new_ingestion');
        const ingestionExpected = ingestionStatus.textContent;
        resolveRetry({ job_id: 11, status: 'queued' });
        await retrying;
        await Promise.resolve();
        const ingestionSafe = ingestionExpected === 'Permissions are checked when jobs load.'
            && ingestionStatus.textContent === ingestionExpected;
        ingestion.destroy();
        return { evaluationSafe, ingestionSafe };
    }""")
    assert result == {"evaluationSafe": True, "ingestionSafe": True}


def test_review_evaluation_exit_aborts_pending_identity_and_permissions(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(5000)
    page.add_init_script("""
        window.__reviewPermissionSignals = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = (input, init = {}) => {
            const path = String(input);
            if (path.includes('/v1/identity') || path.includes('/v1/ops/permissions')) {
                window.__reviewPermissionSignals.push(init.signal);
            }
            return originalFetch(input, init);
        };
    """)
    pending = []
    page.route("**/v1/identity", lambda route: pending.append(route))
    page.route("**/v1/ops/permissions?*", lambda route: pending.append(route))
    page.route("**/v1/ops/runs?*", lambda route: _fulfill_json(route, {"runs": []}))
    page.goto(f"{workbench_url}#/evaluation")
    page.locator("#evaluation-load").click()
    page.wait_for_function("window.__reviewPermissionSignals.length === 2")

    page.evaluate("window.location.hash = '#/overview'")
    page.locator("#view-overview").wait_for(state="visible")
    page.wait_for_function(
        "window.__reviewPermissionSignals.length === 2 && "
        "window.__reviewPermissionSignals.every(signal => signal?.aborted)"
    )


def test_review_shared_permission_lease_is_not_cancelled_while_another_view_is_active(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.goto(f"{workbench_url}#/overview")
    result = page.evaluate("""async () => {
        const { ScopeGuard } = await import('/ui/js/core.mjs');
        const { createPermissions } = await import('/ui/js/permissions.mjs');
        const { createEvaluation } = await import('/ui/js/views/evaluation.mjs');
        const { createIngestion } = await import('/ui/js/views/ingestion.mjs');
        const guard = new ScopeGuard();
        const scope = { tenant: 'tenant_a', kb: 'kb_after_sale', user: 'demo_user', window: '24h' };
        const context = { scope, ...guard.snapshot() };
        const signals = [];
        let resolveRequests;
        const pending = new Promise(resolve => { resolveRequests = resolve; });
        const permissions = createPermissions({
            guard,
            scope,
            api: (_path, options) => {
                signals.push(options.signal);
                return pending;
            },
        });
        const evaluation = createEvaluation({
            root: document.createElement('section'), guard, scope, permissions,
        });
        const ingestion = createIngestion({
            root: document.createElement('section'), guard, scope, permissions,
        });
        evaluation.setActive(true);
        ingestion.setActive(true);
        const loading = permissions.load(context);
        await Promise.resolve();
        await Promise.resolve();
        evaluation.setActive(false);
        const afterFirstExit = signals.length === 2 && signals.every(signal => !signal.aborted);
        ingestion.destroy();
        const afterLastDestroy = signals.length === 2 && signals.every(signal => signal.aborted);
        resolveRequests({ role: 'admin', is_admin: true, ops: { actions_enabled: true, can_trigger: true } });
        await loading;
        evaluation.destroy();
        permissions.destroy();
        return { afterFirstExit, afterLastDestroy };
    }""")
    assert result == {"afterFirstExit": True, "afterLastDestroy": True}


def test_review_active_evaluation_polling_stops_on_real_scope_change(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    page.route("**/v1/ops/runs?*", lambda route: _fulfill_json(route, {"runs": []}))
    page.route("**/v1/ops/kbs/*/runs", lambda route: _fulfill_json(route, {"run_id": 101, "status": "queued"}, status=202))
    detail_calls = {"count": 0}

    def detail_route(route) -> None:
        detail_calls["count"] += 1
        _fulfill_json(route, _task14_run(101, status="running"))

    page.route("**/v1/ops/runs/*", detail_route)
    page.goto(f"{workbench_url}#/evaluation")
    page.locator("#evaluation-load").click()
    page.locator("#run-trigger-eval-mock").click()
    page.locator('[data-run-field="status"]').filter(has_text="running").wait_for()
    page.wait_for_timeout(550)
    assert detail_calls["count"] >= 2

    page.locator("#scope-kb").fill("kb_scope_changed")
    page.locator("#scope-kb").press("Tab")
    assert page.locator("#evaluation-runs-status").inner_text() == "Load runs or refresh to inspect evaluation history."
    assert page.locator("#evaluation-action-status").inner_text() == ""
    calls_after_scope_change = detail_calls["count"]
    page.wait_for_timeout(1200)
    assert detail_calls["count"] == calls_after_scope_change

    page.locator("#scope-refresh").click()
    page.locator("#evaluation-runs-status").filter(has_text="0 runs").wait_for()
    page.wait_for_timeout(700)
    assert detail_calls["count"] == calls_after_scope_change


def test_review_same_scope_refresh_settles_write_controls_for_late_outcomes(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.goto(f"{workbench_url}#/overview")
    result = page.evaluate("""async () => {
        const { ScopeGuard } = await import('/ui/js/core.mjs');
        const { createEvaluation } = await import('/ui/js/views/evaluation.mjs');
        const { createIngestion } = await import('/ui/js/views/ingestion.mjs');
        const scope = { tenant: 'demo', kb: 'kb_after_sale', user: 'demo_user', window: '24h' };
        const permissions = {
            canActions: () => true,
            canRetry: () => true,
            actionReason: () => 'allowed',
            retryReason: () => 'allowed',
        };
        const refresh = (guard, view) => {
            guard.invalidate();
            window.dispatchEvent(new CustomEvent('workbuddy:refresh', {
                detail: { scope, view, ...guard.snapshot() },
            }));
        };
        const fixture = (view) => {
            const root = document.createElement('section');
            const button = document.createElement('button');
            if (view === 'evaluation') button.dataset.kind = 'eval_mock';
            else button.id = 'ingestion-retry';
            const action = document.createElement('p');
            action.id = `${view}-action-status`;
            const permission = document.createElement('p');
            permission.id = `${view}-permission-status`;
            root.append(button, action, permission);
            document.body.appendChild(root);
            return { root, button, action };
        };
        const settle = (pending, outcome, value) => {
            if (outcome === 'resolve') pending.resolve(value);
            else pending.reject(value);
        };

        async function evaluationCase(outcome) {
            const guard = new ScopeGuard();
            const context = { scope, ...guard.snapshot() };
            const { root, button, action } = fixture('evaluation');
            let pending;
            const request = new Promise((resolve, reject) => { pending = { resolve, reject }; });
            const evaluation = createEvaluation({
                root, guard, scope, permissions,
                api: (_path, options) => options.method === 'POST'
                    ? request
                    : Promise.resolve({ runs: [] }),
            });
            evaluation.setActive(true);
            const writing = evaluation.triggerRun('eval_mock', context);
            const disabledBefore = button.disabled;
            refresh(guard, 'evaluation');
            const enabledAfterRefresh = !button.disabled;
            const statusAfterRefresh = action.textContent;
            settle(pending, outcome, outcome === 'resolve'
                ? { data: { run_id: 101 }, status: 202 }
                : new Error('late trigger failure'));
            await writing;
            await Promise.resolve();
            const result = {
                disabledBefore,
                enabledAfterRefresh,
                enabledAfterLate: !button.disabled,
                statusAfterRefresh,
                statusAfterLate: action.textContent,
            };
            evaluation.destroy();
            root.remove();
            return result;
        }

        async function ingestionCase(outcome) {
            const guard = new ScopeGuard();
            const context = { scope, ...guard.snapshot() };
            const { root, button, action } = fixture('ingestion');
            let pending;
            const request = new Promise((resolve, reject) => { pending = { resolve, reject }; });
            const ingestion = createIngestion({
                root, guard, scope, permissions,
                api: (path, options) => {
                    if (options.method === 'POST') return request;
                    if (path.includes('/events')) return Promise.resolve({ job_id: 11, events: [] });
                    return Promise.resolve({ jobs: [{ id: 11, status: 'failed', progress: 80 }], counts: {} });
                },
            });
            ingestion.setActive(true);
            await ingestion.loadJobs(context);
            await ingestion.selectJob(11, context);
            const writing = ingestion.retrySelectedJob(context);
            const disabledBefore = button.disabled;
            refresh(guard, 'ingestion');
            const enabledAfterRefresh = !button.disabled;
            const statusAfterRefresh = action.textContent;
            settle(pending, outcome, outcome === 'resolve'
                ? { status: 202 }
                : Object.assign(new Error('late retry conflict'), { status: 409 }));
            await writing;
            await Promise.resolve();
            const result = {
                disabledBefore,
                enabledAfterRefresh,
                enabledAfterLate: !button.disabled,
                statusAfterRefresh,
                statusAfterLate: action.textContent,
            };
            ingestion.destroy();
            root.remove();
            return result;
        }

        return {
            evaluationResolve: await evaluationCase('resolve'),
            evaluationReject: await evaluationCase('reject'),
            ingestionResolve: await ingestionCase('resolve'),
            ingestionReject: await ingestionCase('reject'),
        };
    }""")
    expected_evaluation = {
        "disabledBefore": True,
        "enabledAfterRefresh": True,
        "enabledAfterLate": True,
        "statusAfterRefresh": "Run trigger cancelled; no outcome is assumed.",
        "statusAfterLate": "Run trigger cancelled; no outcome is assumed.",
    }
    expected_ingestion = {
        "disabledBefore": True,
        "enabledAfterRefresh": True,
        "enabledAfterLate": True,
        "statusAfterRefresh": "Retry request cancelled; reload the job to verify its actual state.",
        "statusAfterLate": "Retry request cancelled; reload the job to verify its actual state.",
    }
    assert result == {
        "evaluationResolve": expected_evaluation,
        "evaluationReject": expected_evaluation,
        "ingestionResolve": expected_ingestion,
        "ingestionReject": expected_ingestion,
    }


def test_review_retry_stays_busy_until_jobs_and_events_reload(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(5000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    retry_requests = []
    job_requests = []
    event_requests = []
    pending_jobs = []
    pending_events = []

    def jobs_route(route) -> None:
        job_requests.append(route.request.url)
        if len(job_requests) == 1:
            _fulfill_json(route, {"jobs": [_task14_job(11)], "counts": {"failed": 1}})
        else:
            pending_jobs.append(route)

    def events_route(route) -> None:
        event_requests.append(route.request.url)
        if len(event_requests) == 1:
            _fulfill_json(route, {"job_id": 11, "events": [{"stage": "parsing", "status": "failed"}]})
        else:
            pending_events.append(route)

    def retry_route(route) -> None:
        retry_requests.append(route.request.url)
        _fulfill_json(route, {"job_id": 11, "status": "queued"}, status=202)

    page.route("**/v1/ops/jobs?*", jobs_route)
    page.route("**/v1/ops/jobs/11/events", events_route)
    page.route("**/v1/ops/jobs/11/retry", retry_route)
    page.goto(f"{workbench_url}#/ingestion")
    page.locator("#ingestion-load").click()
    page.locator('[data-job-select="11"]').click()
    page.locator("#ingestion-retry").wait_for(state="visible")
    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="reloading actual state").wait_for()

    disabled_during_reload = page.locator("#ingestion-retry").is_disabled()
    page.locator("#ingestion-retry").evaluate("button => { button.click(); button.click(); }")
    page.wait_for_timeout(200)
    assert disabled_during_reload is True
    assert page.locator("#ingestion-retry").is_disabled()
    assert len(retry_requests) == 1
    assert len(job_requests) == 2
    assert len(event_requests) == 2

    _fulfill_json(pending_jobs.pop(0), {"jobs": [_task14_job(11, "queued")], "counts": {"queued": 1}})
    _fulfill_json(pending_events.pop(0), {"job_id": 11, "events": [{"stage": "queued", "status": "queued"}]})
    page.locator("#ingestion-action-status").filter(has_text="reloaded job #11").wait_for()
    assert page.locator("#ingestion-retry").is_hidden()
    assert "could not be fully reloaded" not in page.locator("#ingestion-action-status").inner_text()


@pytest.mark.parametrize("view", ["evaluation", "ingestion"])
def test_review_repeated_arrow_navigation_retains_selection_and_focus(workbench_page, view: str) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(4000)
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    if view == "evaluation":
        _install_task14_runs(page, {"history": "ok", "detail_errors": {}}, {})
        selector = "data-run-select"
        dataset_key = "runSelect"
        steps = [("ArrowDown", "29"), ("ArrowDown", "28"), ("ArrowUp", "29"), ("ArrowUp", "30")]
        first = "30"
    else:
        page.route(
            "**/v1/ops/jobs?*",
            lambda route: _fulfill_json(route, {
                "jobs": [_task14_job(13), _task14_job(12), _task14_job(11)], "counts": {"failed": 3},
            }),
        )
        page.route("**/v1/ops/jobs/*/events", lambda route: _fulfill_json(route, {"events": []}))
        selector = "data-job-select"
        dataset_key = "jobSelect"
        steps = [("ArrowDown", "12"), ("ArrowDown", "11"), ("ArrowUp", "12"), ("ArrowUp", "13")]
        first = "13"

    page.goto(f"{workbench_url}#/{view}")
    page.locator(f"#{view}-load").click()
    page.locator(f'[{selector}="{first}"]').wait_for()
    page.locator(f'[{selector}="{first}"]').focus()

    def wait_for_selection(run_or_job_id: str) -> None:
        page.wait_for_function(
            """({ view, selector, datasetKey, id }) => {
                const root = document.querySelector(`#view-${view}`);
                const selected = root?.querySelector(`[${selector}][aria-current="true"]`);
                return document.activeElement?.dataset?.[datasetKey] === id
                    && selected?.dataset?.[datasetKey] === id;
            }""",
            arg={"view": view, "selector": selector, "datasetKey": dataset_key, "id": run_or_job_id},
        )

    for key, expected_id in steps:
        page.keyboard.press(key)
        wait_for_selection(expected_id)
    page.wait_for_timeout(150)
    wait_for_selection(steps[-1][1])


def test_workbench_access_audit_modules_and_unified_action_reason_markers(client: TestClient) -> None:
    html = client.get("/ui/").text
    access = html.split('<section id="view-access"', 1)[1].split("</section>", 1)[0]
    audit = html.split('<section id="view-audit"', 1)[1].split("</section>", 1)[0]

    for element_id in (
        "access-load", "access-permission-status", "access-identity-status",
        "access-kb-permission-status", "access-permission-reason",
        "access-compare-form", "access-compare-query", "access-compare-user-ids",
        "access-compare-top-k", "access-compare-submit", "access-compare-error",
        "access-compare-status", "access-compare-results",
    ):
        assert f'id="{element_id}"' in access
        assert html.count(f'id="{element_id}"') == 1
    for layer in ("tenant", "kb", "user"):
        assert f'data-access-layer="{layer}"' in access
    for field in ("tenant", "user", "role", "tags", "kbs"):
        assert f'data-access-field="{field}"' in access
    assert 'for="access-compare-query"' in access
    assert 'for="access-compare-user-ids"' in access
    assert 'for="access-compare-top-k"' in access
    assert 'minlength="1"' in access
    assert 'maxlength="500"' in access
    assert 'min="1"' in access
    assert 'max="10"' in access

    for element_id in ("audit-load", "audit-status", "audit-timeline"):
        assert f'id="{element_id}"' in audit
        assert html.count(f'id="{element_id}"') == 1
    assert "<ol" in audit

    for status_id in (
        "ingestion-permission-status", "evaluation-permission-status", "access-permission-status",
    ):
        status = html.split(f'id="{status_id}"', 1)[1].split(">", 1)[0]
        assert "data-action-disabled-reason" in status

    app_module = client.get("/ui/js/app.mjs").text
    assert 'from "./views/access.mjs"' in app_module
    assert 'from "./views/audit.mjs"' in app_module
    access_module = client.get("/ui/js/views/access.mjs")
    audit_module = client.get("/ui/js/views/audit.mjs")
    assert access_module.status_code == 200
    assert audit_module.status_code == 200
    assert "canCompare" in access_module.text
    assert "compareReason" in access_module.text
    assert "/v1/ops/audit" in audit_module.text


def test_access_identity_isolation_compare_xss_mobile_and_keyboard(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    identity_calls: list[str] = []
    permission_calls: list[str] = []
    compare_calls: list[dict] = []
    xss = '<img src=x onerror="window.__task15Xss=1">'

    def identity_route(route) -> None:
        identity_calls.append(route.request.url)
        payload = _task14_identity()
        payload.update({
            "tags": ["dept:cs", xss],
            "kbs": ["kb_after_sale", "kb_internal"],
        })
        _fulfill_json(route, payload)

    def permissions_route(route) -> None:
        permission_calls.append(route.request.url)
        payload = _task14_permissions()
        payload["reason"] = f"tenant, ACL and RLS explanation {xss}"
        payload["rls"].update({"scoped_count": 7, "bare_count": 0, "enabled": True})
        payload["acl"]["user_tags"] = ["dept:cs", xss]
        _fulfill_json(route, payload)

    def compare_route(route) -> None:
        compare_calls.append({
            "body": route.request.post_data_json,
            "headers": route.request.headers,
            "url": route.request.url,
        })
        _fulfill_json(route, {
            "query": route.request.post_data_json["query"],
            "users": [
                {
                    "user": "guest", "role": "guest", "tags": [], "hits": 2,
                    "top_score": 0.75, "latency_ms": 4.2,
                    "visible_chunk_ids": ["chunk-public", xss],
                },
                {
                    "user": "member", "role": "member", "tags": ["dept:cs"], "hits": 0,
                    "top_score": None, "latency_ms": 3.1, "visible_chunk_ids": [],
                },
            ],
        })

    page.route("**/v1/identity", identity_route)
    page.route("**/v1/ops/permissions?*", permissions_route)
    page.route("**/v1/ops/kbs/*/identity-compare", compare_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/access")
    assert identity_calls == []
    assert permission_calls == []
    assert compare_calls == []

    page.locator("#access-load").click()
    page.locator('[data-access-field="tenant"]').filter(has_text="demo").wait_for()
    assert len(identity_calls) == 1
    assert len(permission_calls) == 1
    assert parse_qs(urlparse(permission_calls[0]).query) == {"kb_id": ["kb_after_sale"]}
    assert page.locator('[data-access-field="user"]').inner_text() == "demo_user"
    assert page.locator('[data-access-field="role"]').inner_text() == "admin"
    assert "dept:cs" in page.locator('[data-access-field="tags"]').inner_text()
    assert "kb_internal" in page.locator('[data-access-field="kbs"]').inner_text()
    assert "7" in page.locator('[data-access-kb-field="scoped_count"]').inner_text()
    assert "0" in page.locator('[data-access-kb-field="bare_count"]').inner_text()
    assert "tenant, ACL and RLS explanation" in page.locator("#access-permission-reason").inner_text()

    tenant_layer = page.locator('[data-access-layer="tenant"]').inner_text()
    kb_layer = page.locator('[data-access-layer="kb"]').inner_text()
    user_layer = page.locator('[data-access-layer="user"]').inner_text()
    assert "Tenant" in tenant_layer and "demo" in tenant_layer
    assert "KB" in kb_layer and "kb_after_sale" in kb_layer and "7" in kb_layer
    assert "User" in user_layer and "demo_user" in user_layer and "admin_bypass" in user_layer

    page.locator("#access-compare-query").fill("refund")
    page.locator("#access-compare-user-ids").fill("guest, member")
    page.locator("#access-compare-top-k").fill("1")
    page.locator("#access-compare-top-k").press("Enter")
    page.locator('[data-compare-user="guest"]').wait_for(state="visible")
    assert compare_calls == [{
        "body": {"query": "refund", "user_ids": ["guest", "member"], "top_k": 1},
        "headers": compare_calls[0]["headers"],
        "url": compare_calls[0]["url"],
    }]
    assert compare_calls[0]["headers"]["x-tenant-id"] == "demo"
    assert compare_calls[0]["headers"]["x-user-id"] == "demo_user"
    assert compare_calls[0]["url"].endswith("/v1/ops/kbs/kb_after_sale/identity-compare")
    guest = page.locator('[data-compare-user="guest"]')
    assert guest.locator("[data-compare-hits]").inner_text() == "2"
    assert guest.locator("[data-compare-chunk-id]").count() == 2
    assert "chunk-public" in guest.inner_text()
    assert "No visible chunks" in page.locator('[data-compare-user="member"]').inner_text()
    assert page.locator("#view-access img").count() == 0
    assert page.evaluate("window.__task15Xss") is None
    assert xss in page.locator("#view-access").inner_text()

    page.locator("#access-compare-query").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "access-compare-user-ids"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "access-compare-top-k"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "access-compare-submit"

    page.set_viewport_size({"width": 390, "height": 1000})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    columns = page.locator("#access-compare-form").evaluate(
        "form => getComputedStyle(form).gridTemplateColumns"
    )
    assert len(columns.split()) == 1
    geometry = page.locator("#access-compare-form").evaluate(
        """form => {
            const outer = form.getBoundingClientRect();
            return [...form.querySelectorAll('textarea, input, button')].map(element => {
                const rect = element.getBoundingClientRect();
                return { id: element.id, left: rect.left, right: rect.right,
                    outerLeft: outer.left, outerRight: outer.right };
            });
        }"""
    )
    assert all(
        item["left"] >= item["outerLeft"] - 1 and item["right"] <= item["outerRight"] + 1
        for item in geometry
    )

    calls_before_scope_change = len(identity_calls) + len(permission_calls)
    page.locator("#scope-kb").fill("kb_changed")
    page.locator("#scope-kb").press("Tab")
    assert len(identity_calls) + len(permission_calls) == calls_before_scope_change
    assert page.locator("#access-compare-results").inner_text() == ""
    assert "Refresh" in page.locator("#access-identity-status").inner_text()


def test_access_compare_boundaries_empty_error_and_dual_permission_reasons(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    permission_state = {"identity": "admin", "permissions": "admin"}
    compare_state = {"mode": "ok"}
    posted: list[dict] = []
    _install_task14_permissions(page, permission_state)

    def compare_route(route) -> None:
        posted.append(route.request.post_data_json)
        if compare_state["mode"] == "error":
            _fulfill_json(route, {"detail": "compare exploded"}, status=500)
        elif compare_state["mode"] == "empty":
            _fulfill_json(route, {"query": route.request.post_data_json["query"], "users": []})
        else:
            _fulfill_json(route, {"query": route.request.post_data_json["query"], "users": [
                {"user": "guest", "role": "guest", "tags": [], "hits": 0, "visible_chunk_ids": []},
            ]})

    page.route("**/v1/ops/kbs/*/identity-compare", compare_route)
    page.goto(f"{workbench_url}#/access")
    page.locator("#access-load").click()
    page.locator("#access-compare-submit").wait_for(state="visible")
    page.wait_for_function("!document.querySelector('#access-compare-submit').disabled")

    invalid_cases = (
        ("", "guest", "5", "access-compare-query"),
        ("x" * 501, "guest", "5", "access-compare-query"),
        ("valid", "", "5", "access-compare-user-ids"),
        ("valid", "a,b,c,d", "5", "access-compare-user-ids"),
        ("valid", "guest", "0", "access-compare-top-k"),
        ("valid", "guest", "11", "access-compare-top-k"),
        ("valid", "guest", "1.5", "access-compare-top-k"),
    )
    for query, user_ids, top_k, invalid_id in invalid_cases:
        page.locator("#access-compare-query").evaluate(
            "(element, value) => { element.value = value; }", query
        )
        page.locator("#access-compare-user-ids").fill(user_ids)
        page.locator("#access-compare-top-k").fill(top_k)
        page.locator("#access-compare-submit").click()
        page.locator("#access-compare-error").wait_for(state="visible")
        assert posted == []
        assert page.locator(f"#{invalid_id}").get_attribute("aria-invalid") == "true"
        assert page.evaluate("document.activeElement.id") == invalid_id

    page.locator("#access-compare-query").evaluate(
        "(element) => { element.value = 'x'.repeat(500); }"
    )
    page.locator("#access-compare-user-ids").fill("guest,member,admin")
    page.locator("#access-compare-top-k").fill("10")
    page.locator("#access-compare-submit").click()
    page.wait_for_function("document.querySelectorAll('[data-compare-user]').length === 1")
    assert posted == [{"query": "x" * 500, "user_ids": ["guest", "member", "admin"], "top_k": 10}]
    assert page.locator("#access-compare-error").is_hidden()

    compare_state["mode"] = "empty"
    page.locator("#access-compare-submit").click()
    page.locator("#access-compare-status").filter(has_text="No comparison rows returned").wait_for()
    assert page.locator("#access-compare-results").inner_text() == ""
    compare_state["mode"] = "error"
    page.locator("#access-compare-submit").click()
    page.locator("#access-compare-status").filter(has_text="Comparison unavailable").wait_for()
    assert "compare exploded" in page.locator("#access-compare-status").inner_text()
    assert "No comparison rows returned" not in page.locator("#access-compare-status").inner_text()

    permission_state.update(identity="member", permissions="member")
    page.locator("#scope-user").fill("member_user")
    page.locator("#scope-user").press("Tab")
    page.locator("#scope-refresh").click()
    page.locator("#access-permission-status").filter(has_text="Administrator role required").wait_for()
    member_reason = page.locator("#access-permission-status").inner_text()
    assert "OPS_ENABLE_ACTIONS" not in member_reason
    assert page.locator("#access-compare-submit").is_disabled()
    assert page.locator("#access-compare-submit").get_attribute("aria-describedby") == "access-permission-status"

    permission_state.update(identity="disabled", permissions="admin")
    page.locator("#scope-user").fill("demo_user")
    page.locator("#scope-user").press("Tab")
    page.locator("#scope-refresh").click()
    page.locator("#access-permission-status").filter(has_text="OPS_ENABLE_ACTIONS").wait_for()
    disabled_reason = page.locator("#access-permission-status").inner_text()
    assert "Administrator role required" not in disabled_reason
    assert page.locator("#access-compare-submit").is_disabled()


def test_access_identity_failure_preserves_kb_and_permission_request(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    page.add_init_script("""
        window.__task15PermissionSignals = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = (input, init = {}) => {
            const path = String(input);
            if (path.includes('/v1/identity') || path.includes('/v1/ops/permissions')) {
                window.__task15PermissionSignals.push({ path, signal: init.signal });
            }
            return originalFetch(input, init);
        };
    """)
    pending_permissions = []
    page.route("**/v1/identity", lambda route: _fulfill_json(
        route, {"detail": "identity exploded"}, status=500
    ))
    page.route("**/v1/ops/permissions?*", lambda route: pending_permissions.append(route))
    page.goto(f"{workbench_url}#/access")
    page.locator("#access-load").click()
    page.locator("#access-identity-status").filter(has_text="Identity unavailable").wait_for()
    assert "identity exploded" in page.locator("#access-identity-status").inner_text()
    assert page.locator("#scope-kb").input_value() == "kb_after_sale"
    page.wait_for_function("window.__task15PermissionSignals.length === 2")
    assert page.evaluate(
        "window.__task15PermissionSignals.every(entry => !entry.signal.aborted)"
    )
    assert "identity exploded" not in page.locator("#access-kb-permission-status").inner_text()

    payload = _task14_permissions()
    payload["reason"] = "independent KB decision"
    payload["rls"]["scoped_count"] = 9
    _fulfill_json(pending_permissions.pop(0), payload)
    page.locator("#access-permission-reason").filter(has_text="independent KB decision").wait_for()
    assert page.locator('[data-access-kb-field="scoped_count"]').inner_text() == "9"
    assert "Identity unavailable" in page.locator("#access-identity-status").inner_text()
    assert "identity exploded" not in page.locator("#access-kb-permission-status").inner_text()
    assert page.locator("#scope-kb").input_value() == "kb_after_sale"


def test_audit_timeline_order_fields_states_xss_mobile_and_keyboard(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    audit_state = {"mode": "delayed"}
    audit_calls: list[str] = []
    pending = []
    xss = '<img src=x onerror="window.__task15AuditXss=1">'
    records = [
        {
            "id": 2, "created_at": "2026-11-20T10:00:00Z", "actor": "new_actor",
            "action": "identity_compare", "target": "kb_after_sale", "result": "allowed",
            "reason": f"new reason {xss}",
        },
        {
            "id": 1, "created_at": "2026-11-20T09:00:00Z", "actor": "old_actor",
            "action": "job_retry", "target": "job:11", "result": "denied", "reason": "admin required",
        },
    ]

    def audit_route(route) -> None:
        audit_calls.append(route.request.url)
        if audit_state["mode"] == "delayed":
            pending.append(route)
        elif audit_state["mode"] == "error":
            _fulfill_json(route, {"detail": "audit exploded"}, status=500)
        elif audit_state["mode"] == "empty":
            _fulfill_json(route, {"audit": []})
        else:
            _fulfill_json(route, {"audit": records})

    page.route("**/v1/ops/audit?*", audit_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{workbench_url}#/audit")
    assert audit_calls == []
    page.locator("#audit-load").click()
    page.locator("#audit-status").filter(has_text="Loading audit records").wait_for()
    assert parse_qs(urlparse(audit_calls[0]).query) == {"limit": ["50"]}
    _fulfill_json(pending.pop(0), {"audit": records})

    items = page.locator("[data-audit-item]")
    items.first.wait_for(state="visible")
    assert items.count() == 2
    assert items.nth(0).locator('[data-audit-field="actor"]').inner_text() == "old_actor"
    assert items.nth(1).locator('[data-audit-field="actor"]').inner_text() == "new_actor"
    assert items.nth(0).locator("time").get_attribute("datetime") == "2026-11-20T09:00:00Z"
    for index, record in enumerate(reversed(records)):
        item = items.nth(index)
        for field in ("actor", "action", "target", "result", "reason"):
            assert str(record[field]) in item.locator(f'[data-audit-field="{field}"]').inner_text()
    assert "oldest first" in page.locator("#audit-status").inner_text().lower()
    assert page.locator("#view-audit img").count() == 0
    assert page.evaluate("window.__task15AuditXss") is None
    assert xss in items.nth(1).inner_text()

    items.nth(0).focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement?.dataset?.auditItem") == "2"
    page.keyboard.press("ArrowUp")
    assert page.evaluate("document.activeElement?.dataset?.auditItem") == "1"

    audit_state["mode"] = "empty"
    page.locator("#audit-load").click()
    page.locator("#audit-status").filter(has_text="No audit records").wait_for()
    assert items.count() == 0
    audit_state["mode"] = "error"
    page.locator("#audit-load").click()
    page.locator("#audit-status").filter(has_text="Audit unavailable").wait_for()
    assert "audit exploded" in page.locator("#audit-status").inner_text()
    assert "No audit records" not in page.locator("#audit-status").inner_text()

    page.set_viewport_size({"width": 390, "height": 1000})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_access_audit_and_permission_stale_responses_and_destroy_are_isolated(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.goto(f"{workbench_url}#/overview")
    result = page.evaluate("""async () => {
        const { ScopeGuard } = await import('/ui/js/core.mjs');
        const { createPermissions } = await import('/ui/js/permissions.mjs');
        const { createAccess } = await import('/ui/js/views/access.mjs');
        const { createAudit } = await import('/ui/js/views/audit.mjs');
        const deferred = () => {
            let resolve;
            const promise = new Promise(next => { resolve = next; });
            return { promise, resolve };
        };
        const scope = kb => ({ tenant: 'demo', kb, user: kb === 'kb_a' ? 'old_user' : 'new_user', window: '24h' });
        const ignoreWindow = { addEventListener() {}, removeEventListener() {} };
        const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };

        const permissionGuard = new ScopeGuard();
        const permissionRequests = [];
        const permissions = createPermissions({
            guard: permissionGuard,
            scope: scope('kb_a'),
            api: (path, options) => {
                const pending = deferred();
                permissionRequests.push({ path, options, pending });
                return pending.promise;
            },
        });
        const contextA = { scope: scope('kb_a'), ...permissionGuard.snapshot() };
        const loadingA = permissions.load(contextA);
        await tick();
        permissionGuard.invalidate();
        permissions.clear();
        const contextB = { scope: scope('kb_b'), ...permissionGuard.snapshot() };
        const loadingB = permissions.load(contextB);
        await tick();
        for (const request of permissionRequests.filter(item => item.options.scope.kb === 'kb_b')) {
            request.pending.resolve(request.path.includes('/identity')
                ? { tenant: 'demo', user: 'new_user', role: 'admin', is_admin: true,
                    ops: { actions_enabled: true, can_trigger: true } }
                : { user: 'new_user', is_admin: true, reason: 'new permission', rls: {} });
        }
        await loadingB;
        for (const request of permissionRequests.filter(item => item.options.scope.kb === 'kb_a')) {
            request.pending.resolve(request.path.includes('/identity')
                ? { tenant: 'demo', user: 'old_user', role: 'admin', is_admin: true,
                    ops: { actions_enabled: true, can_trigger: true } }
                : { user: 'old_user', is_admin: true, reason: 'stale permission', rls: {} });
        }
        await loadingA;
        await tick();
        const permissionState = permissions.getState();
        const permissionSafe = permissionState.identity?.user === 'new_user'
            && permissionState.permissions?.user === 'new_user'
            && permissionState.permissions?.reason === 'new permission'
            && permissionRequests.filter(item => item.options.scope.kb === 'kb_a')
                .every(item => item.options.signal.aborted);
        permissions.destroy();

        const accessGuard = new ScopeGuard();
        const accessRoot = document.getElementById('view-access').cloneNode(true);
        accessRoot.removeAttribute('id');
        const accessRequests = [];
        const accessState = {
            identity: { user: 'new_user', role: 'admin', is_admin: true, tags: [], kbs: [],
                ops: { actions_enabled: true, can_trigger: true } },
            permissions: { user: 'new_user', is_admin: true, rls: {}, kb: {}, acl: {} },
            identityError: '', permissionError: '', identityLoading: false, permissionLoading: false,
            loading: false, canCompare: true, reason: 'allowed',
        };
        const accessPermissions = {
            load: async () => accessState,
            getState: () => accessState,
            subscribe: () => () => {},
            canCompare: () => true,
            compareReason: () => 'allowed',
            actionReason: () => 'allowed',
            retain() {}, release() {},
        };
        const access = createAccess({
            root: accessRoot, guard: accessGuard, scope: scope('kb_a'),
            permissions: accessPermissions, window: ignoreWindow,
            api: (_path, options) => {
                const pending = deferred();
                accessRequests.push({ options, pending });
                return pending.promise;
            },
        });
        access.setActive(true);
        const accessA = { scope: scope('kb_a'), ...accessGuard.snapshot() };
        accessRoot.querySelector('#access-compare-query').value = 'old query';
        accessRoot.querySelector('#access-compare-user-ids').value = 'old_user';
        accessRoot.querySelector('#access-compare-top-k').value = '5';
        const oldCompare = access.compareIdentities(accessA);
        await tick();
        accessGuard.invalidate();
        access.clear();
        const accessB = { scope: scope('kb_b'), ...accessGuard.snapshot() };
        await access.loadWorkspace(accessB);
        accessRoot.querySelector('#access-compare-query').value = 'new query';
        accessRoot.querySelector('#access-compare-user-ids').value = 'new_user';
        accessRoot.querySelector('#access-compare-top-k').value = '5';
        const newCompare = access.compareIdentities(accessB);
        await tick();
        accessRequests.find(item => item.options.scope.kb === 'kb_b').pending.resolve({
            users: [{ user: 'new_user', hits: 1, visible_chunk_ids: ['new_chunk'] }],
        });
        await newCompare;
        accessRequests.find(item => item.options.scope.kb === 'kb_a').pending.resolve({
            users: [{ user: 'old_user', hits: 1, visible_chunk_ids: ['stale_chunk'] }],
        });
        await oldCompare;
        await tick();
        const accessSafe = accessRoot.querySelector('#access-compare-results').textContent.includes('new_chunk')
            && !accessRoot.querySelector('#access-compare-results').textContent.includes('stale_chunk')
            && accessRequests.find(item => item.options.scope.kb === 'kb_a').options.signal.aborted;

        const destroyingCompare = access.compareIdentities(accessB);
        await tick();
        const destroyingRequest = accessRequests.at(-1);
        const beforeDestroy = accessRoot.querySelector('#access-compare-status').textContent;
        access.destroy();
        const destroyAborted = destroyingRequest.options.signal.aborted;
        destroyingRequest.pending.resolve({ users: [{ user: 'late', hits: 1, visible_chunk_ids: ['late_chunk'] }] });
        await destroyingCompare;
        await tick();
        const accessDestroySafe = destroyAborted
            && accessRoot.querySelector('#access-compare-status').textContent === beforeDestroy
            && !accessRoot.querySelector('#access-compare-results').textContent.includes('late_chunk');

        const auditGuard = new ScopeGuard();
        const auditRoot = document.getElementById('view-audit').cloneNode(true);
        auditRoot.removeAttribute('id');
        const auditRequests = [];
        const audit = createAudit({
            root: auditRoot, guard: auditGuard, scope: scope('kb_a'), window: ignoreWindow,
            api: (_path, options) => {
                const pending = deferred();
                auditRequests.push({ options, pending });
                return pending.promise;
            },
        });
        audit.setActive(true);
        const auditA = { scope: scope('kb_a'), ...auditGuard.snapshot() };
        const oldAudit = audit.loadAudit(auditA);
        await tick();
        auditGuard.invalidate();
        audit.clear();
        const auditB = { scope: scope('kb_b'), ...auditGuard.snapshot() };
        const newAudit = audit.loadAudit(auditB);
        await tick();
        auditRequests.find(item => item.options.scope.kb === 'kb_b').pending.resolve({
            audit: [{ id: 2, created_at: '2026-11-20T10:00:00Z', actor: 'new_actor',
                action: 'new', target: 'new', result: 'allowed', reason: 'new' }],
        });
        await newAudit;
        auditRequests.find(item => item.options.scope.kb === 'kb_a').pending.resolve({
            audit: [{ id: 1, created_at: '2026-11-20T09:00:00Z', actor: 'stale_actor',
                action: 'stale', target: 'stale', result: 'denied', reason: 'stale' }],
        });
        await oldAudit;
        await tick();
        const auditSafe = auditRoot.querySelector('#audit-timeline').textContent.includes('new_actor')
            && !auditRoot.querySelector('#audit-timeline').textContent.includes('stale_actor')
            && auditRequests.find(item => item.options.scope.kb === 'kb_a').options.signal.aborted;

        const destroyingAudit = audit.loadAudit(auditB);
        await tick();
        const destroyingAuditRequest = auditRequests.at(-1);
        const auditBeforeDestroy = auditRoot.querySelector('#audit-status').textContent;
        audit.destroy();
        const auditDestroyAborted = destroyingAuditRequest.options.signal.aborted;
        destroyingAuditRequest.pending.resolve({ audit: [{ id: 3, actor: 'late_actor' }] });
        await destroyingAudit;
        await tick();
        const auditDestroySafe = auditDestroyAborted
            && auditRoot.querySelector('#audit-status').textContent === auditBeforeDestroy
            && !auditRoot.querySelector('#audit-timeline').textContent.includes('late_actor');

        return { permissionSafe, accessSafe, accessDestroySafe, auditSafe, auditDestroySafe };
    }""")
    assert result == {
        "permissionSafe": True,
        "accessSafe": True,
        "accessDestroySafe": True,
        "auditSafe": True,
        "auditDestroySafe": True,
    }


def test_access_audit_bfcache_restore_and_subsequent_pagehide_cleanup(workbench_page) -> None:
    page, workbench_url = workbench_page
    page.set_default_timeout(7000)
    page.add_init_script("""
        window.__task15BfcachePermissionSignals = [];
        window.__task15BfcacheAuditSignals = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = (input, init = {}) => {
            const path = String(input);
            const entry = { path, signal: init.signal };
            if (path.includes('/v1/identity') || path.includes('/v1/ops/permissions')) {
                window.__task15BfcachePermissionSignals.push(entry);
            }
            if (path.includes('/v1/ops/audit')) {
                window.__task15BfcacheAuditSignals.push(entry);
            }
            return originalFetch(input, init);
        };
    """)
    identity_calls: list[str] = []
    permission_calls: list[str] = []
    pending_audit = []

    def identity_route(route) -> None:
        identity_calls.append(route.request.url)
        _fulfill_json(route, _task14_identity())

    def permission_route(route) -> None:
        permission_calls.append(route.request.url)
        kb_id = parse_qs(urlparse(route.request.url).query)["kb_id"][0]
        payload = _task14_permissions()
        payload["kb"]["id"] = kb_id
        payload["reason"] = f"permission for {kb_id}"
        _fulfill_json(route, payload)

    page.route("**/v1/identity", identity_route)
    page.route("**/v1/ops/permissions?*", permission_route)
    page.route("**/v1/ops/audit?*", lambda route: pending_audit.append(route))
    page.goto(f"{workbench_url}#/access")
    page.locator("#access-load").click()
    page.locator("#access-permission-reason").filter(
        has_text="permission for kb_after_sale"
    ).wait_for()
    assert len(identity_calls) == 1
    assert len(permission_calls) == 1

    page.evaluate("""
        window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
        window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    """)
    assert len(identity_calls) == 1
    assert len(permission_calls) == 1

    page.locator("#scope-kb").fill("kb_restored")
    page.locator("#scope-kb").press("Tab")
    assert len(identity_calls) == 1
    assert len(permission_calls) == 1
    page.locator("#access-load").click()
    page.locator("#access-permission-reason").filter(
        has_text="permission for kb_restored"
    ).wait_for()
    assert len(identity_calls) == 2
    assert len(permission_calls) == 2
    assert parse_qs(urlparse(permission_calls[-1]).query)["kb_id"] == ["kb_restored"]

    page.locator('[data-nav="audit"]').click()
    page.locator("#view-audit").wait_for(state="visible")
    page.locator("#audit-load").click()
    page.wait_for_function("window.__task15BfcacheAuditSignals.length === 1")
    page.evaluate(
        "window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: false }))"
    )
    page.wait_for_function("window.__task15BfcacheAuditSignals[0].signal.aborted")
    page.locator("#audit-load").click()
    assert page.evaluate("window.__task15BfcacheAuditSignals.length") == 1
    _fulfill_json(pending_audit.pop(0), {"audit": []})


def test_workbench_defaults_to_chinese_and_switches_to_english(workbench_page) -> None:
    page, url = workbench_page
    base = url.split("?")[0]
    page.goto(base)
    page.wait_for_function("document.documentElement.lang === 'zh-CN'")

    chinese_labels = ("概览", "查询实验室", "调用追踪", "入库任务", "评测与演练", "访问控制", "审计日志")
    for name, label in zip(NAV, chinese_labels):
        assert page.locator(f'[data-nav="{name}"] span').last.inner_text() == label
    assert page.evaluate("document.documentElement.lang") == "zh-CN"

    page.select_option("#locale-select", "en")
    page.wait_for_function("document.documentElement.lang === 'en'")

    english_labels = ("Overview", "Query Lab", "Traces", "Ingestion", "Evaluation", "Access Control", "Audit Log")
    for name, label in zip(NAV, english_labels):
        assert page.locator(f'[data-nav="{name}"] span').last.inner_text() == label
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

    with page.expect_response(lambda response: "/v1/ops/summary" in response.url):
        page.select_option("#locale-select", "en")


def test_workbench_overview_refresh_uses_chinese_dynamic_copy(workbench_page) -> None:
    page, url = workbench_page
    page.set_default_timeout(7000)
    page.route(
        "**/v1/ops/summary*",
        lambda route: _fulfill_json(route, {
            "observability": {"window": "24h"},
            "queue": {"queued": 3},
            "workers": [{"status": "online"}, {"status": "offline"}],
        }),
    )
    page.route(
        "**/v1/ops/metrics/timeseries*",
        lambda route: _fulfill_json(route, {"series": []}),
    )
    page.goto(url)
    page.locator("#scope-refresh").click()
    page.locator('[data-overview-summary-status][data-state="success"]').wait_for()
    assert page.locator('[data-overview-unit="queue"]').inner_text() == "queued"
    assert page.locator('[data-overview-unit="workers"]').inner_text() == "workers"

    page.select_option("#locale-select", "zh-CN")
    page.locator('[data-overview-unit="queue"]').filter(has_text="排队中").wait_for()

    assert page.locator('[data-overview-unit="workers"]').inner_text() == "个工作进程"
    assert page.locator('[data-overview-window="queue"]').inner_text() == "快照"
    assert page.locator('[data-overview-window="workers"]').inner_text() == "快照"
    assert page.locator('[data-overview-summary-status]').inner_text().startswith("更新于 ")
    assert page.locator('[data-overview-trends-status]').inner_text().startswith("更新于 ")


def test_workbench_lab_clear_after_locale_switch_uses_chinese_dynamic_copy(workbench_page) -> None:
    page, url = workbench_page
    page.goto(f"{url}#/lab/search")
    page.locator("#scope-refresh").click()
    assert page.locator("#search-button").inner_text() == "Run search"
    assert page.locator("#search-status").inner_text() == "Awaiting a retrieval query."

    page.select_option("#locale-select", "zh-CN")

    assert page.locator("#search-button").inner_text() == "执行检索"
    assert page.locator("#chat-button").text_content() == "开始流式问答"
    assert page.locator("#upload-button").text_content() == "上传并跟踪任务"
    assert page.locator("#search-status").inner_text() == "等待检索查询。"
    assert page.locator("#chat-status").text_content() == "等待提问。"
    assert page.locator("#upload-status").text_content() == "等待选择文档。"
    assert page.locator("#citation-status").text_content() == "暂无可展开的引用。"


def test_workbench_locale_switch_reloads_access_identity_and_permissions(workbench_page) -> None:
    page, url = workbench_page
    page.set_default_timeout(7000)
    page.route("**/v1/identity", lambda route: _fulfill_json(route, _task14_identity()))
    page.route(
        "**/v1/ops/permissions?*",
        lambda route: _fulfill_json(route, _task14_permissions()),
    )
    page.goto(f"{url}#/access")
    page.locator("#access-load").click()
    page.locator('[data-access-field="tenant"]').filter(has_text="demo").wait_for()
    page.locator("#access-permission-reason").filter(has_text="allowed").wait_for()

    with page.expect_response(lambda response: response.url.endswith("/v1/identity")):
        with page.expect_response(lambda response: "/v1/ops/permissions?" in response.url):
            page.select_option("#locale-select", "zh-CN")


def test_workbench_chinese_copy_covers_shell_and_overview(workbench_page) -> None:
    page, url = workbench_page
    page.goto(url.split("?")[0])
    page.wait_for_function("document.documentElement.lang === 'zh-CN'")

    assert page.locator(".wb-brand strong").inner_text() == "WorkRAG"
    assert page.locator(".wb-brand-copy span").inner_text() == "RAG 运维"
    assert page.locator('[data-nav="lab"] span').last.inner_text() == "查询实验室"
    assert page.locator("#view-overview h1").inner_text() == "概览"
    assert page.locator('[data-overview-card="requests"] h2').inner_text() == "请求数"
    assert page.locator("#scope-refresh").inner_text() == "刷新"
    assert page.locator('[data-lab-tab="search"]').inner_text() == "检索"
    assert page.locator("#search-button").inner_text() == "执行检索"
    assert page.locator("#view-traces h1").inner_text() == "调用追踪"
    assert page.locator(".wb-trace-table thead th").first.inner_text() == "时间"
    assert page.locator("#view-ingestion h1").inner_text() == "入库任务"
    assert page.locator("#ingestion-retry").text_content() == "重试失败任务"
    assert page.locator("#view-evaluation h1").inner_text() == "评测与演练"
    assert page.locator("#run-trigger-loadtest").inner_text() == "启动压测"
    assert page.locator("#view-access h1").inner_text() == "访问控制"
    assert page.locator("#view-audit h1").inner_text() == "审计日志"


def test_workbench_ingestion_uses_chinese_dynamic_copy(workbench_page) -> None:
    page, url = workbench_page
    page.set_default_timeout(7000)
    base = url.split("?")[0]
    retry_state = {"status": 202}
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})
    page.route(
        "**/v1/ops/jobs?*",
        lambda route: _fulfill_json(route, {
            "jobs": [_task14_job(11)],
            "counts": {"queued": 0, "running": 0, "succeeded": 0, "failed": 1},
        }),
    )
    page.route(
        "**/v1/ops/jobs/*/events",
        lambda route: _fulfill_json(route, {"job_id": 11, "events": [{
            "id": 1, "job_id": 11, "stage": "parsing", "status": "failed",
            "progress": 80, "created_at": "2026-11-20T08:00:03Z",
        }]}),
    )

    def retry_route(route) -> None:
        status = retry_state["status"]
        _fulfill_json(
            route,
            {"job_id": 11, "status": "queued"} if status == 202 else {"detail": "job is not failed"},
            status=status,
        )

    page.route("**/v1/ops/jobs/*/retry", retry_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base}#/ingestion")

    assert page.locator("#ingestion-jobs-status").inner_text() == "加载任务或刷新以查看入库情况。"
    assert page.locator("#ingestion-retry").inner_text() == "重试失败任务"

    page.locator("#ingestion-load").click()
    page.locator('[data-job-select="11"]').wait_for(state="visible")
    assert page.locator("#ingestion-jobs-status").inner_text() == "1 个任务 · 排队 0 · 运行中 0 · 失败 1"

    page.locator('[data-job-select="11"]').click()
    page.locator("#ingestion-job-events-status").filter(has_text="共 1 条事件").wait_for()
    assert page.locator("#ingestion-job-events-status").inner_text() == "任务 #11 共 1 条事件。"
    assert page.locator("#ingestion-retry").get_attribute("title") == "重试失败的入库任务 #11"

    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="重试已接受").wait_for()
    assert "任务 #11" in page.locator("#ingestion-action-status").inner_text()

    retry_state["status"] = 409
    page.locator("#ingestion-retry").click()
    page.locator("#ingestion-action-status").filter(has_text="已有运行中的任务").wait_for()
    conflict = page.locator("#ingestion-action-status").inner_text()
    assert conflict.startswith("已有运行中的任务；请刷新任务状态。")
    assert "A job is already running" not in conflict


def test_workbench_evaluation_uses_chinese_dynamic_copy(workbench_page) -> None:
    page, url = workbench_page
    page.set_default_timeout(7000)
    base = url.split("?")[0]
    run_state = {"history": "ok", "empty": False}
    _install_task14_permissions(page, {"identity": "admin", "permissions": "admin"})

    def runs_route(route) -> None:
        if run_state["history"] == "error":
            _fulfill_json(route, {"detail": "runs exploded"}, status=500)
            return
        runs = [] if run_state["empty"] else [_task14_run(30, base=0.8), _task14_run(27, base=0.6)]
        _fulfill_json(route, {"runs": runs})

    def detail_route(route) -> None:
        run_id = int(urlparse(route.request.url).path.rsplit("/", 1)[-1])
        _fulfill_json(route, _task14_run(run_id, base=0.8))

    page.route("**/v1/ops/runs?*", runs_route)
    page.route("**/v1/ops/runs/*", detail_route)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base}#/evaluation")

    assert page.locator("#evaluation-runs-status").inner_text() == "加载运行记录或刷新以查看评测历史。"
    assert page.locator("#evaluation-detail-status").inner_text() == "选择一条运行记录以独立查看详情。"
    assert page.locator("#run-trigger-loadtest").inner_text() == "启动压测"

    run_state["empty"] = True
    page.locator("#evaluation-load").click()
    page.locator("#evaluation-run-rows .wb-ops-empty").filter(
        has_text="当前作用域没有评测运行记录。"
    ).wait_for()
    assert page.locator("#evaluation-delta-context").inner_text() == "没有可用于差值对比的更早同类成功运行。"

    run_state["empty"] = False
    page.locator("#evaluation-load").click()
    page.locator('[data-run-select="30"]').wait_for(state="visible")
    assert page.locator("#evaluation-runs-status").inner_text() == "2 次运行 · 最新历史在前。"

    page.locator('[data-run-select="30"]').click()
    page.locator("#evaluation-detail-status").filter(has_text="已加载运行").wait_for()
    assert page.locator("#evaluation-detail-status").inner_text() == "已加载运行 #30 · succeeded。"
    delta_context = page.locator("#evaluation-delta-context").inner_text()
    assert delta_context.startswith("差值 = 当前值")
    assert "最近一次同类成功运行 eval_mock #27" in delta_context

    run_state["history"] = "error"
    page.locator("#evaluation-load").click()
    page.locator("#evaluation-runs-status").filter(has_text="运行历史不可用").wait_for()
    assert "Run history unavailable" not in page.locator("#evaluation-runs-status").inner_text()


def test_workbench_access_uses_chinese_dynamic_copy(workbench_page) -> None:
    page, url = workbench_page
    page.set_default_timeout(7000)
    base = url.split("?")[0]
    page.route("**/v1/identity", lambda route: _fulfill_json(route, _task14_identity()))
    page.route(
        "**/v1/ops/permissions?*",
        lambda route: _fulfill_json(route, _task14_permissions()),
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(base)
    page.locator('[data-nav="access"]').click()
    page.wait_for_function("window.location.hash === '#/access'")

    assert page.locator("#access-identity-status").inner_text() == "加载访问状态或刷新以查看身份信息。"
    assert page.locator("#access-kb-permission-status").inner_text() == "知识库权限与身份信息分开加载。"
    assert page.locator("#access-permission-status").inner_text() == "身份与知识库权限尚未加载。"
    assert page.locator("#access-compare-status").inner_text() == "对比身份前请先加载权限。"

    page.locator("#access-load").click()
    page.locator('[data-access-field="tenant"]').filter(has_text="demo").wait_for()
    assert page.locator("#access-identity-status").inner_text() == "已独立加载身份信息。"
    assert page.locator("#access-kb-permission-status").inner_text() == "已独立加载知识库权限。"
    assert page.locator("#access-permission-status").inner_text() == "当前租户、用户与知识库已启用管理员操作。"
    assert page.locator('[data-access-field="role"]').inner_text() == "admin"
    assert "租户 demo" in page.locator('[data-access-layer="tenant"]').inner_text()
    assert "知识库 kb_after_sale" in page.locator('[data-access-layer="kb"]').inner_text()
    assert "用户 demo_user" in page.locator('[data-access-layer="user"]').inner_text()
    assert "Identity and knowledge-base permissions" not in page.locator("#view-access").inner_text()


def test_workbench_audit_uses_chinese_dynamic_copy(workbench_page) -> None:
    page, url = workbench_page
    page.set_default_timeout(7000)
    base = url.split("?")[0]
    audit_state = {"records": [
        {
            "id": 2, "created_at": "2026-11-20T10:00:00Z", "actor": "new_actor",
            "action": "identity_compare", "target": "kb_after_sale", "result": "allowed",
            "reason": "new reason",
        },
        {
            "id": 1, "created_at": "2026-11-20T09:00:00Z", "actor": "old_actor",
            "action": "job_retry", "target": "job:11", "result": "denied",
            "reason": "admin required",
        },
    ]}

    page.route(
        "**/v1/ops/audit?*",
        lambda route: _fulfill_json(route, {"audit": audit_state["records"]}),
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base}#/audit")

    assert page.locator("#view-audit h1").inner_text() == "审计日志"
    assert page.locator("#audit-load").inner_text() == "加载审计记录"
    assert page.locator("#audit-status").inner_text() == "加载审计记录或刷新以查看时间线。"
    assert (
        page.locator("#audit-timeline").get_attribute("aria-label")
        == "运维审计时间线，最早的事件在前"
    )

    page.locator("#audit-load").click()
    items = page.locator("[data-audit-item]")
    items.first.wait_for(state="visible")
    assert items.count() == 2
    assert page.locator("#audit-status").inner_text() == "2 条审计记录 · 最早记录在前。"
    first = items.nth(0)
    assert first.locator("dt").all_inner_texts() == ["操作人", "操作", "目标", "结果", "原因"]
    assert first.locator('[data-audit-field="actor"]').inner_text() == "old_actor"
    assert first.locator('[data-audit-field="result"]').inner_text() == "denied"
    assert "audit records" not in page.locator("#audit-status").inner_text()

    audit_state["records"] = []
    page.locator("#audit-load").click()
    page.locator("#audit-status").filter(has_text="当前作用域没有审计记录").wait_for()
    assert items.count() == 0
