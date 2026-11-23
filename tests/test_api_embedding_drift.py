"""漂移接口：按 KB 返回版本分布与漂移块数，跨租户不可见、kb 非法 400。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine

T, OTHER, KB, KB2 = "t_drift", "t_drift_other", "kb_drift", "kb_drift_2"
CONF = "BAAI/bge-m3"
VEC = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    _cleanup(db_engine)
    with db_engine.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'drift')"), {"t": T})
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'other')"), {"t": OTHER})
        for kb in (KB, KB2):
            c.execute(
                text(
                    "INSERT INTO knowledge_bases (id,tenant_id,name,description,status) "
                    "VALUES (:kb,:t,:kb,'','active')"
                ),
                {"kb": kb, "t": T},
            )
        c.execute(
            text(
                "INSERT INTO knowledge_bases (id,tenant_id,name,description,status) "
                "VALUES ('kb_x',:t,'kb_x','','active')"
            ),
            {"t": OTHER},
        )
    for tenant, kb, cid, ver in (
        (T, KB, "c1", CONF),
        (T, KB, "c2", CONF),
        (T, KB, "c3", "mock-hash"),
        (T, KB2, "c4", CONF),
        (OTHER, "kb_x", "c5", "mock-hash"),
    ):
        _seed(db_engine, tenant, kb, cid, ver)
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine):
    with engine.begin() as c:
        for tenant in (T, OTHER):
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            for table in ("chunks", "documents", "knowledge_bases"):
                c.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
        c.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": T, "b": OTHER})


def _seed(engine, tenant, kb, cid, version):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        c.execute(
            text(
                "INSERT INTO documents (id,tenant_id,kb_id,title,source_url,content_hash,status,meta) "
                "VALUES (:d,:t,:kb,:d,'',:d,'ready','{}'::jsonb) ON CONFLICT (id) DO NOTHING"
            ),
            {"d": f"doc-{cid}", "t": tenant, "kb": kb},
        )
        c.execute(
            text(
                "INSERT INTO chunks (chunk_id,tenant_id,kb_id,doc_id,content_hash,text,token_count,"
                "embedding,embed_model_version,meta) VALUES (:id,:t,:kb,:d,:id,'x',1,"
                "CAST(:v AS vector),:m,'{}'::jsonb)"
            ),
            {"id": cid, "t": tenant, "kb": kb, "d": f"doc-{cid}", "v": VEC, "m": version},
        )


def _get(client, monkeypatch, **params):
    monkeypatch.setenv("EMBED_MODEL", CONF)
    return client.get(
        "/v1/ops/storage/embedding-drift", params=params, headers={"X-Tenant-Id": T}
    )


def test_drift_shape(engine, monkeypatch):
    client = TestClient(create_app())
    body = _get(client, monkeypatch, kb_id=KB).json()
    assert body["configured_version"] == CONF
    assert body["total_chunks"] == 3 and body["drift_chunks"] == 1
    assert body["kbs"] == [
        {"kb_id": KB, "total": 3, "drift": 1, "versions": {CONF: 2, "mock-hash": 1}}
    ]


def test_drift_isolation_and_invalid_kb(engine, monkeypatch):
    client = TestClient(create_app())
    monkeypatch.setenv("EMBED_MODEL", CONF)
    body = client.get(
        "/v1/ops/storage/embedding-drift", headers={"X-Tenant-Id": OTHER}
    ).json()
    assert body["total_chunks"] == 1 and body["drift_chunks"] == 1
    assert _get(client, monkeypatch, kb_id="../evil").status_code == 400
