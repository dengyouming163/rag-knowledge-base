"""飞书连接器的单元测试：所有 HTTP 请求均由 MockTransport 响应。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.connectors.base import DocumentRef


def _feishu_types():
    from src.connectors.feishu import FeishuAPIError, FeishuSource

    return FeishuAPIError, FeishuSource


def _source(handler) -> object:
    _, FeishuSource = _feishu_types()
    return FeishuSource(
        "app-id",
        "app-secret",
        base_url="https://feishu.test",
        transport=httpx.MockTransport(handler),
    )


def _token_response(expire: int = 7200) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "tenant-token",
            "expire": expire,
        },
    )


def _file(token: str, modified_time: int, *, file_type: str = "docx") -> dict:
    return {
        "token": token,
        "name": f"文档 {token}",
        "type": file_type,
        "modified_time": str(modified_time),
    }


def _document_response(token: str, revision: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "msg": "ok",
            "data": {
                "document": {
                    "document_id": token,
                    "revision_id": revision,
                    "title": f"文档 {token}",
                }
            },
        },
    )


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"FEISHU_APP_ID": "app-id"},
        {"FEISHU_APP_SECRET": "app-secret"},
    ],
)
def test_from_env_returns_none_without_complete_credentials(
    monkeypatch, caplog, credentials
) -> None:
    _, FeishuSource = _feishu_types()
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    for key, value in credentials.items():
        monkeypatch.setenv(key, value)

    with caplog.at_level(logging.INFO):
        assert FeishuSource.from_env() is None

    assert "飞书连接器未注册" in caplog.text
    assert "app-secret" not in caplog.text


def test_from_env_builds_available_source_with_credentials(monkeypatch) -> None:
    _, FeishuSource = _feishu_types()
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    source = FeishuSource.from_env(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )

    assert source is not None
    assert source.available is True
    assert source.name == "feishu"


def test_unavailable_source_rejects_all_operations_without_http() -> None:
    _, FeishuSource = _feishu_types()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    source = FeishuSource(transport=httpx.MockTransport(handler))
    ref = DocumentRef(
        uri="feishu://docx/doc-1",
        mtime=datetime.now(timezone.utc),
        size=-1,
        hash="1",
    )

    assert source.available is False
    with pytest.raises(RuntimeError, match="FEISHU_APP_ID"):
        list(source.list_documents(None))
    with pytest.raises(RuntimeError, match="FEISHU_APP_ID"):
        source.fetch(ref)
    with pytest.raises(RuntimeError, match="FEISHU_APP_ID"):
        source.sync()
    assert requests == []


def test_list_documents_authenticates_and_follows_pagination() -> None:
    token_requests = []
    list_requests = []
    metadata_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            token_requests.append(request)
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "app_id": "app-id",
                "app_secret": "app-secret",
            }
            return _token_response()

        assert request.headers["Authorization"] == "Bearer tenant-token"
        if request.url.path == "/open-apis/drive/v1/files":
            list_requests.append(request)
            assert request.url.params["page_size"] == "50"
            if "page_token" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "ok",
                        "data": {
                            "has_more": True,
                            "next_page_token": "page-2",
                            "files": [
                                _file("doc-1", 1704067200),
                                _file("upload-1", 1704067200, file_type="file"),
                            ],
                        },
                    },
                )
            assert request.url.params["page_token"] == "page-2"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "has_more": False,
                        "files": [_file("doc-2", 1704153600)],
                    },
                },
            )

        if request.url.path.startswith("/open-apis/docx/v1/documents/"):
            metadata_requests.append(request)
            token = request.url.path.rsplit("/", 1)[-1]
            return _document_response(token, 1 if token == "doc-1" else 2)

        return httpx.Response(404, text="unexpected request")

    refs = list(_source(handler).list_documents(None))

    assert [ref.uri for ref in refs] == [
        "feishu://docx/doc-1",
        "feishu://docx/doc-2",
    ]
    assert [ref.hash for ref in refs] == ["1", "2"]
    assert [ref.size for ref in refs] == [-1, -1]
    assert refs[0].mtime == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert refs[0].mtime.tzinfo is timezone.utc
    assert len(token_requests) == 1
    assert len(list_requests) == 2
    assert [request.url.path for request in metadata_requests] == [
        "/open-apis/docx/v1/documents/doc-1",
        "/open-apis/docx/v1/documents/doc-2",
    ]


def test_list_documents_filters_by_mtime_before_reading_revision() -> None:
    metadata_tokens = []
    cutoff = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        if request.url.path == "/open-apis/drive/v1/files":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "files": [
                            _file("old-doc", int((cutoff - timedelta(days=1)).timestamp())),
                            _file("new-doc", int((cutoff + timedelta(days=1)).timestamp())),
                        ],
                    },
                },
            )
        token = request.url.path.rsplit("/", 1)[-1]
        metadata_tokens.append(token)
        return _document_response(token, 7)

    refs = list(_source(handler).list_documents(cutoff))

    assert [ref.uri for ref in refs] == ["feishu://docx/new-doc"]
    assert refs[0].mtime == cutoff + timedelta(days=1)
    assert metadata_tokens == ["new-doc"]


def test_sync_uses_revision_as_incremental_identity_and_reuses_token() -> None:
    revision = 1
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            token_requests += 1
            return _token_response()
        if request.url.path == "/open-apis/drive/v1/files":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "files": [_file("doc-1", 1704067200)],
                    },
                },
            )
        return _document_response("doc-1", revision)

    source = _source(handler)
    first = source.sync()

    assert len(first) == 1
    assert first[0].hash == "1"
    assert source.sync() == []

    # 标题和 modified_time 都不变，仅 revision 变化也必须重新返回。
    revision = 2
    changed = source.sync()
    assert len(changed) == 1
    assert changed[0].uri == first[0].uri
    assert changed[0].mtime == first[0].mtime
    assert changed[0].hash == "2"
    assert source.sync() == []
    assert token_requests == 1


def test_fetch_returns_raw_document_content_as_utf8_bytes() -> None:
    raw_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        assert request.headers["Authorization"] == "Bearer tenant-token"
        raw_requests.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "msg": "ok", "data": {"content": "标题\n正文 😀"}},
        )

    ref = DocumentRef(
        uri="feishu://docx/doc-1",
        mtime=datetime.now(timezone.utc),
        size=-1,
        hash="3",
    )

    assert _source(handler).fetch(ref) == "标题\n正文 😀".encode("utf-8")
    assert [request.url.path for request in raw_requests] == [
        "/open-apis/docx/v1/documents/doc-1/raw_content"
    ]


def test_feishu_error_code_is_propagated_readably() -> None:
    FeishuAPIError, _ = _feishu_types()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 99991663, "msg": "app access token invalid"},
        )

    with pytest.raises(FeishuAPIError, match="99991663.*app access token invalid"):
        list(_source(handler).list_documents(None))


def test_http_error_is_propagated_readably() -> None:
    FeishuAPIError, _ = _feishu_types()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        return httpx.Response(403, text="forbidden by scope policy")

    with pytest.raises(FeishuAPIError, match="403.*forbidden by scope policy"):
        list(_source(handler).list_documents(None))
