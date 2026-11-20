"""WorkBuddy 应用壳的静态托管与模块边界测试。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


NAV = ("overview", "lab", "traces", "ingestion", "evaluation", "access", "audit")


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
