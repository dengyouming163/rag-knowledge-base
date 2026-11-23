"""检索与 SSE 接口测试。

覆盖四类风险：
1. 请求体不能带 tenant_id（否则多租户隔离形同虚设）；
2. 不存在的库返回 404 而不是 500；
3. 检索走完整链路（向量 + BM25 + RRF）；
4. LLM 失败时必须下发 `event: error`，不能静默断流。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.deps import get_chat_client
from app.main import create_app
from src.db.base import create_db_engine

TENANT = "t_api"
KB = "kb_api"
DIM = 1024


class FakeChat:
    def __init__(self, reply: str = "退款通常 1-3 个工作日到账。[kb_api#1]") -> None:
        self.reply = reply

    def chat(self, messages, temperature: float = 0.0, max_tokens: int = 512, timeout: float = 30.0):
        return SimpleNamespace(text=self.reply)


class FailingChat:
    def chat(self, *args, **kwargs):
        raise RuntimeError("LLM 连接超时")


@pytest.fixture()
def client() -> TestClient:
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过接口集成测试：{exc}")

    _cleanup(db_engine)
    vector = "[" + ",".join(str(0.02 * ((i % 9) + 1)) for i in range(DIM)) + "]"
    with db_engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'api')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'api kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('doc_api', :t, :kb, '接口测试', '', 'doc_hash_api', 'ready', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )
        connection.execute(
            text(
                "INSERT INTO chunks "
                "(chunk_id, tenant_id, kb_id, doc_id, content_hash, text, token_count, "
                " embedding, embed_model_version, meta) "
                "VALUES ('kb_api#1', :t, :kb, 'doc_api', 'hash_api', "
                "        '退款政策：支持七天无理由退款，1-3 个工作日到账。', 20, "
                "        CAST(:vec AS vector), 'test', CAST(:meta AS jsonb))"
            ),
            {
                "t": TENANT,
                "kb": KB,
                "vec": vector,
                "meta": json.dumps(
                    {"audience": "public", "title": "接口测试", "section_path": ["一"]},
                    ensure_ascii=False,
                ),
            },
        )

    yield TestClient(create_app())

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


# ---------------------------------------------------------------- 参数校验


def test_tenant_id_in_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/search",
        json={"query": "退款", "tenant_id": "someone_else"},
    )
    assert response.status_code == 422


def test_top_k_out_of_range(client: TestClient) -> None:
    assert client.post(f"/v1/kbs/{KB}/search", json={"query": "退款", "top_k": 0}).status_code == 422
    assert client.post(f"/v1/kbs/{KB}/search", json={"query": "退款", "top_k": 99}).status_code == 422


def test_unknown_kb_returns_404(client: TestClient) -> None:
    response = client.post("/v1/kbs/kb_not_exist/search", json={"query": "退款"})
    assert response.status_code == 404


# ---------------------------------------------------------------- 检索


def test_search_returns_hits(client: TestClient) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/search", json={"query": "退款 到账", "top_k": 3}, headers={"X-Tenant-Id": TENANT}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kb_id"] == KB
    assert [hit["chunk_id"] for hit in body["hits"]] == ["kb_api#1"]
    assert body["hits"][0]["text"].startswith("退款政策")


def test_search_bm25_only_mode(client: TestClient) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/search",
        json={"query": "退款", "mode": "bm25"},
        headers={"X-Tenant-Id": TENANT},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "bm25"
    assert response.json()["hits"][0]["chunk_id"] == "kb_api#1"


# ---------------------------------------------------------------- SSE


def test_chat_stream_emits_deltas_and_done(client: TestClient, monkeypatch) -> None:
    # 关掉拒答闸门：这条测的是"生成链路能流式下发"，不是置信度判断
    monkeypatch.setenv("REFUSAL_MIN_SIMILARITY", "0")
    client.app.dependency_overrides[get_chat_client] = lambda: FakeChat()

    response = client.post(
        "/v1/chat/stream", json={"kb_id": KB, "question": "退款多久到账"}, headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: hits" in response.text
    assert '"delta"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_chat_stream_reports_llm_failure(client: TestClient, monkeypatch) -> None:
    # 同上：必须先让请求走到生成环节，才能验证 LLM 失败会下发 event: error
    monkeypatch.setenv("REFUSAL_MIN_SIMILARITY", "0")
    client.app.dependency_overrides[get_chat_client] = lambda: FailingChat()

    response = client.post(
        "/v1/chat/stream", json={"kb_id": KB, "question": "退款多久到账"}, headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "LLM 连接超时" in response.text
    # 关键：失败时不能冒充成功
    assert "[DONE]" not in response.text
