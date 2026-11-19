"""FastAPI 骨架测试：探针语义 + 异常映射。

重点不是"接口能返回 200"，而是：
- /live 永远 200（不能因为数据库挂了就重启容器）；
- /ready 不可用时返回 503，且有缓存、不会每次探针都打库；
- 代码缺陷（缺租户过滤）返回 500，越权返回 404。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import deps
from app.errors import ScopeNotFoundError
from app.main import create_app
from app.routers import health
from src.store_pg import MissingTenantFilterError


@pytest.fixture()
def client() -> TestClient:
    health.reset_cache()
    return TestClient(create_app())


@pytest.fixture()
def db_available() -> None:
    try:
        if not health.probe_db():
            pytest.skip("数据库不可用，跳过就绪探针用例")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")


def test_live_always_ok(client: TestClient) -> None:
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_ready_ok_when_db_up(client: TestClient, db_available) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": "up"}


def test_ready_returns_503_when_db_down(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(health, "probe_db", lambda: False)
    health.reset_cache()

    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["db"] == "down"


def test_ready_is_cached(client: TestClient, monkeypatch) -> None:
    calls = {"count": 0}

    def counting_probe() -> bool:
        calls["count"] += 1
        return True

    monkeypatch.setattr(health, "probe_db", counting_probe)
    health.reset_cache()

    for _ in range(3):
        assert client.get("/ready").status_code == 200

    # 5 秒缓存内只应该探测一次
    assert calls["count"] == 1


def test_missing_tenant_filter_is_500() -> None:
    app: FastAPI = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise MissingTenantFilterError("缺少 tenant_id")

    response = TestClient(app).get("/boom")
    assert response.status_code == 500
    assert response.json()["detail"] == "internal scope error"


def test_scope_not_found_is_404() -> None:
    app: FastAPI = create_app()

    @app.get("/missing")
    def missing() -> None:
        raise ScopeNotFoundError("不存在")

    response = TestClient(app).get("/missing")
    assert response.status_code == 404


def test_tenant_comes_from_header() -> None:
    """租户只能从请求头取；没有请求头时用环境默认值。"""
    assert deps.current_tenant("t_a") == "t_a"
    assert deps.current_tenant(None) == "demo"
