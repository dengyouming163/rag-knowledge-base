"""src/llm.py 的单元测试：全部 mock httpx，不发起真实网络请求。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm import ChatClient, LLMError


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeHTTPClient:
    """按顺序返回预置响应或抛出预置异常，并记录调用次数。"""

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


def make_payload(text: str = "你好，我是客服助手。") -> dict:
    return {
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }


@pytest.fixture
def build_client(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")

    def _build(items: list[object]) -> tuple[ChatClient, FakeHTTPClient]:
        fake_http = FakeHTTPClient(items)
        monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: fake_http)
        client = ChatClient(sleep=lambda seconds: None)
        return client, fake_http

    return _build


def test_chat_returns_text_and_token_usage(build_client):
    client, fake_http = build_client([FakeResponse(200, make_payload("你好，我是客服助手。"))])

    result = client.chat([{"role": "user", "content": "你好"}])

    assert result.text == "你好，我是客服助手。"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 7
    assert result.model == "deepseek-chat"
    assert result.latency_ms >= 0
    assert len(fake_http.calls) == 1
    url, kwargs = fake_http.calls[0]
    assert url == "https://example.invalid/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key-not-real"


def test_retries_on_5xx_then_succeeds(build_client):
    client, fake_http = build_client(
        [FakeResponse(503, {"error": "unavailable"}), FakeResponse(200, make_payload("重试成功"))]
    )

    result = client.chat([{"role": "user", "content": "hi"}])

    assert result.text == "重试成功"
    assert len(fake_http.calls) == 2


def test_retries_on_429(build_client):
    client, fake_http = build_client(
        [FakeResponse(429, {"error": "rate limited"}), FakeResponse(200, make_payload("限流后成功"))]
    )

    result = client.chat([{"role": "user", "content": "hi"}])

    assert result.text == "限流后成功"
    assert len(fake_http.calls) == 2


def test_401_is_not_retried(build_client):
    client, fake_http = build_client([FakeResponse(401, {"error": "invalid api key"})])

    with pytest.raises(LLMError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])

    assert exc_info.value.status_code == 401
    assert "invalid api key" in exc_info.value.message
    assert len(fake_http.calls) == 1


def test_timeout_retries_then_raises_llm_error(build_client):
    timeout = httpx.TimeoutException("request timed out")
    client, fake_http = build_client([timeout, timeout, timeout])

    with pytest.raises(LLMError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])

    assert exc_info.value.status_code is None
    assert len(fake_http.calls) == 3


def test_missing_env_var_raises_clear_error(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(LLMError) as exc_info:
        ChatClient()

    assert "LLM_API_KEY" in exc_info.value.message
