"""WorkBuddy 应用壳的静态托管与模块边界测试。"""

from __future__ import annotations

import json
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

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
