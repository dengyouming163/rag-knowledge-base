"""静态工作台托管测试：只验证响应，不测试前端 DOM 或交互。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.parametrize("path", ["/ui", "/ui/"])
def test_ui_serves_index_from_any_working_directory(
    path: str, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html lang=\"zh-CN\">" in response.text
    assert "<title>知识运维工作台</title>" in response.text


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_ui_has_wechat_desktop_shell(client: TestClient) -> None:
    html = client.get("/ui/").text
    for marker in (
        'class="wx-rail"',
        'data-nav="workbench"',
        'data-nav="cockpit"',
        'data-nav="identity"',
        'data-nav="runs"',
        'data-view="workbench"',
        'class="wx-content"',
    ):
        assert marker in html


def test_ui_shell_navigation_markers(client: TestClient) -> None:
    html = client.get("/ui/").text
    assert html.count("data-nav=") == 5
    assert 'data-view="cockpit" hidden' in html
    assert 'id="wx-list-title"' in html


def test_ui_has_cockpit_panels(client: TestClient) -> None:
    html = client.get("/ui/").text
    for marker in (
        'id="ops-health"',
        'id="ops-queue"',
        'id="ops-jobs"',
        'id="ops-job-events"',
        'id="ops-ai"',
        'id="ops-traces"',
        'id="ops-trace-waterfall"',
    ):
        assert marker in html
