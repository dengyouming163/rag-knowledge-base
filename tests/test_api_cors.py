"""CORS：默认关闭；白名单内放行预检。"""

from fastapi.testclient import TestClient

from app.main import create_app

ORIGIN = "https://console.example.com"


def test_cors_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    resp = TestClient(create_app()).get("/live", headers={"Origin": ORIGIN})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_allows_listed_origin(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", f"{ORIGIN}, https://other.example.com")
    resp = TestClient(create_app()).options(
        "/v1/storage",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN
