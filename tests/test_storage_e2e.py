"""存储抽象端到端：上传 → worker 入库 → 检索命中（local 后端）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.bm25_store import BM25Store
from src.db.base import create_db_engine
from src.retrieve import HybridRetriever
from src.storage import LocalStorage
from src.store_pg import PgVectorStore
from src.worker import worker_once

TENANT = "t_storage_e2e"
KB = "kb_storage_e2e"
DIM = 1024

CONTENT = "# 售后政策\n\n## 退款\n\n支持七天无理由退款。".encode()


class FakeEmbed:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1)] * DIM for index, _ in enumerate(texts)]


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'storage')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'storage kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def test_upload_worker_and_search_use_local_storage(engine, tmp_path, monkeypatch) -> None:
    storage = LocalStorage(tmp_path)
    monkeypatch.setattr("app.routers.documents.build_storage", lambda: storage)

    client = TestClient(create_app())
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.md", CONTENT, "text/markdown")},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 202
    doc_id = response.json()["doc_id"]
    assert (tmp_path / TENANT / KB / f"{doc_id}.md").is_file()

    assert worker_once(engine, embed_client=FakeEmbed(), storage=storage) == 1

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM documents WHERE id = :d AND tenant_id = :t"),
            {"d": doc_id, "t": TENANT},
        ).scalar_one()
    assert status == "ready"

    retriever = HybridRetriever(
        PgVectorStore(engine, tenant_id=TENANT, kb_id=KB),
        BM25Store(engine).load(KB),
        FakeEmbed(),
    )
    hits = retriever.retrieve("七天无理由退款", top_k=5, mode="hybrid")

    assert hits
    assert hits[0].doc_id == doc_id
