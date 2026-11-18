"""src/embed.py 的单元测试：全部 mock httpx，不发起真实网络请求。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.embed import EmbeddingClient, EmbeddingError


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeHTTPClient:
    """按顺序返回预置响应或抛出预置异常，并记录调用参数。"""

    def __init__(self, items: list[object]) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


def make_payload(vectors: list[list[float]], *, total_tokens: int | None = None) -> dict:
    payload: dict = {
        "model": "BAAI/bge-m3",
        "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
    }
    if total_tokens is not None:
        payload["usage"] = {"prompt_tokens": total_tokens, "total_tokens": total_tokens}
    return payload


@pytest.fixture
def build_client(monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EMBED_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("EMBED_MODEL", "BAAI/bge-m3")

    def _build(items: list[object]) -> tuple[EmbeddingClient, FakeHTTPClient]:
        fake_http = FakeHTTPClient(items)
        monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: fake_http)
        client = EmbeddingClient(sleep=lambda seconds: None)
        return client, fake_http

    return _build


def test_embed_texts_keeps_input_order(build_client):
    payload = make_payload([[0.1, 0.2], [0.3, 0.4]], total_tokens=9)
    payload["data"].reverse()  # 模拟服务端乱序返回
    client, fake_http = build_client([FakeResponse(200, payload)])

    vectors = client.embed_texts(["第一段", "第二段"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert len(fake_http.calls) == 1
    url, kwargs = fake_http.calls[0]
    assert url == "https://example.invalid/v1/embeddings"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert kwargs["json"]["model"] == "BAAI/bge-m3"
    assert kwargs["json"]["input"] == ["第一段", "第二段"]

    stats = client.stats()
    assert stats == {"calls": 1, "total_tokens": 9, "total_texts": 2}


def test_embed_texts_batches_requests(build_client):
    texts = [f"文本{i}" for i in range(5)]
    client, fake_http = build_client(
        [
            FakeResponse(200, make_payload([[1.0, 0.0], [0.0, 1.0]], total_tokens=10)),
            FakeResponse(200, make_payload([[0.5, 0.5], [1.0, 1.0]], total_tokens=10)),
            FakeResponse(200, make_payload([[2.0, 2.0]], total_tokens=10)),
        ]
    )

    vectors = client.embed_texts(texts, batch_size=2)

    assert len(fake_http.calls) == 3
    assert [len(kwargs["json"]["input"]) for _, kwargs in fake_http.calls] == [2, 2, 1]
    assert vectors == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5], [1.0, 1.0], [2.0, 2.0]]
    assert client.stats() == {"calls": 3, "total_tokens": 30, "total_texts": 5}


def test_retries_on_429_then_succeeds(build_client):
    client, fake_http = build_client(
        [
            FakeResponse(429, {"error": "rate limited"}),
            FakeResponse(200, make_payload([[0.2, 0.3]], total_tokens=4)),
        ]
    )

    vectors = client.embed_texts(["限流后成功"])

    assert vectors == [[0.2, 0.3]]
    assert len(fake_http.calls) == 2
    assert client.stats()["calls"] == 1


def test_401_is_not_retried(build_client):
    client, fake_http = build_client([FakeResponse(401, {"error": "invalid api key"})])

    with pytest.raises(EmbeddingError) as exc_info:
        client.embed_texts(["hi"])

    assert exc_info.value.status_code == 401
    assert "invalid api key" in exc_info.value.message
    assert len(fake_http.calls) == 1


def test_inconsistent_dimension_raises(build_client):
    client, _ = build_client(
        [FakeResponse(200, make_payload([[0.1, 0.2], [0.3, 0.4, 0.5]]))]
    )

    with pytest.raises(EmbeddingError) as exc_info:
        client.embed_texts(["a", "b"])

    assert "维度" in exc_info.value.message


def test_missing_env_var_raises_clear_error(monkeypatch):
    monkeypatch.delenv("EMBED_API_KEY", raising=False)

    with pytest.raises(EmbeddingError) as exc_info:
        EmbeddingClient()

    assert "EMBED_API_KEY" in exc_info.value.message
