"""驾驶舱写动作接口：任务重试与通用运行入队。

规则：写动作必须管理员且 `OPS_ENABLE_ACTIONS=1`；被拒时落一条 denied 审计，
审计写失败不影响动作结果。重试只接受 failed 任务；同 kind 已有活跃运行时
入队返回 409。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src import ingest_jobs, ops_audit
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
def client(engine):
    with TestClient(create_app()) as test_client:
        yield test_client


def _audit(client: TestClient) -> list[dict]:
    response = client.get("/v1/ops/audit", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    return response.json()["audit"]


def _fail_job(engine, job_id: int, *, attempts: int = 3, progress: int = 80) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET status='failed', attempts=:attempts, "
                "progress=:progress, error='boom', finished_at=now() WHERE id=:id"
            ),
            {"id": job_id, "attempts": attempts, "progress": progress},
        )


def _document_status(engine, doc_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT status FROM documents "
                    "WHERE id = :id AND tenant_id = :t"
                ),
                {"id": doc_id, "t": TENANT},
            ).scalar_one()
        )


def test_non_admin_cannot_retry_job(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    job_id = ingest_jobs.enqueue(engine, TENANT, KB, doc_id="doc_retry")
    _fail_job(engine, job_id)

    response = client.post(
        f"/v1/ops/jobs/{job_id}/retry",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "admin required"
    audit = _audit(client)
    denied = [
        item
        for item in audit
        if item["action"] == "job_retry"
        and item["target"] == str(job_id)
        and item["result"] == "denied"
    ]
    assert len(denied) == 1


def test_admin_retry_resets_failed_job(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    doc_id = "doc_retry_ok"
    job_id = ingest_jobs.enqueue(engine, TENANT, KB, doc_id=doc_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:id, :t, :kb, 'retry doc', '', 'hash_retry', 'failed', "
                "'{}'::jsonb)"
            ),
            {"id": doc_id, "t": TENANT, "kb": KB},
        )
    _fail_job(engine, job_id)

    response = client.post(f"/v1/ops/jobs/{job_id}/retry", headers=ADMIN_HEADERS)

    assert response.status_code == 202
    job = client.get(f"/v1/jobs/{job_id}", headers={"X-Tenant-Id": TENANT}).json()
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["progress"] == 0
    assert _document_status(engine, doc_id) == "pending"
    allowed = [
        item
        for item in _audit(client)
        if item["action"] == "job_retry" and item["result"] == "allowed"
    ]
    assert [item["target"] for item in allowed] == [str(job_id)]


def test_retry_missing_job_returns_404(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    with engine.connect() as connection:
        missing_id = int(
            connection.execute(
                text("SELECT COALESCE(MAX(id), 0) + 100000 FROM ingest_jobs")
            ).scalar_one()
        )

    response = client.post(f"/v1/ops/jobs/{missing_id}/retry", headers=ADMIN_HEADERS)

    assert response.status_code == 404


def test_retry_non_failed_job_returns_409(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    job_id = ingest_jobs.enqueue(engine, TENANT, KB, doc_id="doc_running")

    response = client.post(f"/v1/ops/jobs/{job_id}/retry", headers=ADMIN_HEADERS)

    assert response.status_code == 409


def test_admin_can_enqueue_eval_run(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": {"limit": 5, "note": "smoke"}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["run_id"] > 0
    allowed = [
        item
        for item in _audit(client)
        if item["action"] == "run_eval_mock" and item["result"] == "allowed"
    ]
    assert len(allowed) == 1
    detail = allowed[0]["detail"]
    assert detail["params_keys"] == ["limit", "note"]
    assert detail["params_size"] > 0
    assert "params" not in detail


def test_duplicate_active_run_returns_409(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    body = {"kind": "eval_mock", "params": {}}

    first = client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=ADMIN_HEADERS)
    second = client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=ADMIN_HEADERS)

    assert first.status_code == 202
    assert second.status_code == 409
    run_id = first.json()["run_id"]
    denied = [
        item
        for item in _audit(client)
        if item["action"] == "run_eval_mock"
        and item["result"] == "denied"
        and item["reason"] == "already_running"
        and item["target"] == str(run_id)
    ]
    assert len(denied) == 1


def test_invalid_run_kind_returns_422(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "rm_rf", "params": {}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422


def test_run_params_too_many_keys_returns_422(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": {f"k{i}": i for i in range(21)}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422


def test_run_params_too_large_returns_422(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": {"blob": "x" * 5000}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422


def test_run_params_rejects_nested_values(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": {"nested": {"a": 1}}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422


def test_run_params_rejects_non_finite_values(
    client: TestClient, monkeypatch
) -> None:
    """Infinity/NaN 能被 json 解析但不是合法 JSONB，必须在入口拒绝。"""
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    # httpx 的 json= 会拒绝非有限浮点，这里直接发原始文本
    payload = json.dumps(
        {"kind": "eval_mock", "params": {"ratio": float("inf")}}
    )

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        content=payload,
        headers={**ADMIN_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_audit_failure_does_not_break_actions(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    def _boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(ops_audit, "record", _boom)
    body = {"kind": "eval_mock", "params": {}}

    first = client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=ADMIN_HEADERS)
    second = client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=ADMIN_HEADERS)

    assert first.status_code == 202
    assert second.status_code == 409

    job_id = ingest_jobs.enqueue(engine, TENANT, KB, doc_id="doc_audit_boom")
    _fail_job(engine, job_id)
    retried = client.post(f"/v1/ops/jobs/{job_id}/retry", headers=ADMIN_HEADERS)
    assert retried.status_code == 202


def test_params_size_guard_matches_json_encoding(
    client: TestClient, monkeypatch
) -> None:
    """略小于上限的 params 仍可通过，确保 4096 是按 JSON 编码长度判定。"""
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    params = {"blob": "x" * (4096 - len(json.dumps({"blob": ""})))}

    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": params},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 202
