"""唯一 WorkBuddy 入口、专业面板与静态资源托管测试。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


NAV = ("overview", "lab", "traces", "ingestion", "evaluation", "access", "audit")
VIEW_MODULES = {
    "overview": "createOverview",
    "lab": "createLab",
    "traces": "createTraces",
    "ingestion": "createIngestion",
    "evaluation": "createEvaluation",
    "access": "createAccess",
    "audit": "createAudit",
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_ui_has_one_seven_module_workbuddy_shell_with_overview_visible(
    client: TestClient,
) -> None:
    html = client.get("/ui/").text

    assert "<title>RAG Operations Console</title>" in html
    assert re.findall(r'data-nav="([^"]+)"', html) == list(NAV)
    assert re.findall(r'data-view="([^"]+)"', html) == list(NAV)
    for name in NAV:
        assert html.count(f'data-nav="{name}"') == 1
        assert html.count(f'data-view="{name}"') == 1

    sections = re.findall(r"<section\b[^>]*\bdata-view=\"[^\"]+\"[^>]*>", html)
    assert len(sections) == len(NAV)
    overview_nav = re.search(r'<button\b[^>]*data-nav="overview"[^>]*>', html).group(0)
    assert 'aria-current="page"' in overview_nav
    for section in sections:
        name = re.search(r'data-view="([^"]+)"', section).group(1)
        if name == "overview":
            assert " hidden" not in section
        else:
            assert " hidden" in section

    for legacy_class in ("wx-rail", "wx-middle", "wx-content"):
        assert not re.search(rf'class="[^"]*\b{legacy_class}\b', html)


def test_ui_loads_every_es_module_and_professional_panel(client: TestClient) -> None:
    html = client.get("/ui/").text
    assert html.count('<script type="module" src="/ui/js/app.mjs"></script>') == 1
    assert "<script>" not in html

    app_module = client.get("/ui/js/app.mjs").text
    assert 'from "./core.mjs"' in app_module
    assert 'from "./permissions.mjs"' in app_module
    for name, factory in VIEW_MODULES.items():
        assert f'from "./views/{name}.mjs"' in app_module
        assert factory in app_module
        response = client.get(f"/ui/js/views/{name}.mjs")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    panels = {
        "overview": (
            'data-overview-summary-status', 'data-overview-trends-status',
            'data-overview-card="requests"', 'data-overview-card="cost"',
            'data-overview-card="queue"', 'data-overview-trend="requests"',
            'data-overview-trend="e2e_p95_ms"',
        ),
        "lab": (
            'id="lab-panel-search"', 'id="lab-panel-chat"',
            'id="lab-panel-ingest"', 'id="search-results"',
            'id="citation-detail"', 'id="upload-progress"',
        ),
        "traces": (
            'id="trace-filter-form"', 'id="trace-list-rows"',
            'id="trace-load-more"', 'id="trace-detail"',
            'id="trace-inspector-panel-summary"',
            'id="trace-inspector-panel-timing"',
            'id="trace-inspector-panel-raw"',
        ),
        "ingestion": (
            'id="ingestion-job-rows"', 'id="ingestion-job-detail"',
            'id="ingestion-job-events"', 'id="ingestion-retry"',
        ),
        "evaluation": (
            'id="evaluation-run-rows"', 'id="evaluation-run-detail"',
            'id="evaluation-run-output"', 'id="run-trigger-eval-mock"',
            'id="run-trigger-loadtest"', 'id="run-trigger-drill-parser"',
            'id="run-trigger-drill-llm"', 'id="run-trigger-drill-queue"',
        ),
        "access": (
            'id="access-identity-status"', 'id="access-kb-permission-status"',
            'data-access-layer="tenant"', 'data-access-layer="kb"',
            'data-access-layer="user"', 'id="access-compare-results"',
        ),
        "audit": ('id="audit-timeline"',),
    }
    for name, markers in panels.items():
        assert f'<section id="view-{name}"' in html, name
        rest = html.split(f'<section id="view-{name}"', 1)[1]
        view = rest.split("\n        </section>", 1)[0]
        assert view != rest, f"{name}: 8-space </section> terminator not found"
        for marker in markers:
            assert marker in view, (name, marker)


def test_ui_preserves_legacy_core_capability_element_ids(client: TestClient) -> None:
    html = client.get("/ui/").text
    for element_id in (
        "upload-form", "upload-input", "upload-button", "upload-progress",
        "upload-progress-value", "upload-status", "upload-error",
        "search-form", "search-query", "search-top-k", "search-mode",
        "search-audience", "search-button", "search-status", "search-results",
        "chat-form", "chat-question", "chat-top-k", "chat-audience",
        "chat-button", "chat-status", "answer", "stream-meta",
        "citation-list", "citation-status", "citation-detail",
        "citation-heading", "citation-source", "citation-text",
        "citation-metadata", "run-trigger-loadtest", "audit-timeline",
    ):
        assert html.count(f'id="{element_id}"') == 1, element_id


def test_ui_redirects_to_canonical_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(), follow_redirects=False)

    response = client.get("/ui")
    assert response.status_code == 307
    assert response.headers["location"].endswith("/ui/")


@pytest.mark.parametrize("path", ["/ui", "/ui/"])
def test_ui_and_all_static_assets_load_from_any_working_directory(
    path: str, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>RAG Operations Console</title>" in response.text
    assert 'href="/ui/css/workbench.css"' in response.text

    assets = [
        "/ui/css/workbench.css",
        "/ui/js/app.mjs",
        "/ui/js/api.mjs",
        "/ui/js/core.mjs",
        "/ui/js/permissions.mjs",
        *(f"/ui/js/views/{name}.mjs" for name in VIEW_MODULES),
    ]
    for asset in assets:
        asset_response = client.get(asset)
        assert asset_response.status_code == 200, asset
        assert asset_response.content, asset
