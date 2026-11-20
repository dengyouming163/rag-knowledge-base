"""驾驶舱写动作接口：任务重试与通用运行入队。

规则：写动作必须管理员且 `OPS_ENABLE_ACTIONS=1`；被拒时落一条 denied 审计。
重试只接受 failed 任务；同 kind 已有活跃运行时入队返回 409。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src import ingest_jobs
from src.db.base import create_db_engine

TENANT = "t_ops_actions"
KB = "kb_ops_actions"

ADMIN_HEADERS = {"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"}


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过写动作接口测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops actions')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'actions kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                "VALUES ('u_admin', :t, 'Admin', ARRAY['dept:cs'], 'admin')"
            ),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                "VALUES ('u_member', :t, 'Member', ARRAY['dept:legal'], 'member')"
            ),
            {"t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingest_job_events WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_audit WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def test_non_admin_cannot_retry_job(
    client: TestClient, engine, monkeypatch
) -> None:
    job_id = ingest_jobs.enqueue(engine, TENANT, KB, doc_id="doc_retry")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE ingest_jobs SET status='failed', error='boom' WHERE id=:id"),
            {"id": job_id},
        )

    response = client.post(
        f"/v1/ops/jobs/{job_id}/retry",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )

    assert response.status_code == 403
    # 读审计前打开开关（审计读接口同样要求管理员 + 开关）
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    audit = client.get("/v1/ops/audit", headers=ADMIN_HEADERS).json()["audit"]
    assert any(item["result"] == "denied" for item in audit)


def test_admin_retry_resets_failed_job(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    job_id = ingest_jobs.enqueue(engine, TENANT, KB, doc_id="doc_retry_ok")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET status='failed', attempts=3, progress=80, "
                "error='boom', finished_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )

    response = client.post(
        f"/v1/ops/jobs/{job_id}/retry", headers=ADMIN_HEADERS
    )

    assert response.status_code == 202
    job = client.get(f"/v1/jobs/{job_id}", headers={"X-Tenant-Id": TENANT}).json()
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["progress"] == 0


def test_admin_can_enqueue_eval_run(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": {}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["run_id"] > 0


def test_duplicate_active_run_returns_409(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    body = {"kind": "eval_mock", "params": {}}

    first = client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=ADMIN_HEADERS)
    second = client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=ADMIN_HEADERS)

    assert first.status_code == 202
    assert second.status_code == 409
