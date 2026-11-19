"""可选的飞书 docx 文档来源，以文档 revision 做实例内增量同步。

使用飞书 OpenAPI 的以下接口：
- ``POST /open-apis/auth/v3/tenant_access_token/internal`` 获取租户凭证；
- ``GET /open-apis/drive/v1/files`` 分页列出 Drive 根目录可见文件；
- ``GET /open-apis/docx/v1/documents/{document_id}`` 读取 ``revision_id``；
- ``GET /open-apis/docx/v1/documents/{document_id}/raw_content`` 读取纯文本。

Drive 列表不提供文档大小时，``DocumentRef.size`` 统一为 -1（未知）。
``DocumentRef.hash`` 保存外部版本号 revision，而不是伪造内容 SHA-256。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from .base import DocumentRef

logger = logging.getLogger(__name__)

_DOCUMENT_URI_PREFIX = "feishu://docx/"
_TOKEN_REFRESH_MARGIN_SECONDS = 60


class FeishuAPIError(RuntimeError):
    """飞书返回 HTTP 错误或业务错误码。"""


class FeishuSource:
    """同步状态仅保留在当前实例中；目前仅接入 Drive 列表中的 docx 文档。"""

    name = "feishu"

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        *,
        base_url: str = "https://open.feishu.cn",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._app_id = app_id or ""
        self._app_secret = app_secret or ""
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )
        self._tenant_token: str | None = None
        self._tenant_token_expires_at = 0.0
        self._seen_revisions: dict[str, str] = {}

    @classmethod
    def from_env(
        cls,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> FeishuSource | None:
        """凭证齐全时构造来源；否则记录提示并返回 None，供调用方跳过注册。"""
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            missing = [
                key
                for key, value in (
                    ("FEISHU_APP_ID", app_id),
                    ("FEISHU_APP_SECRET", app_secret),
                )
                if not value
            ]
            logger.info("飞书连接器未注册：缺少环境变量 %s", ", ".join(missing))
            return None
        return cls(app_id, app_secret, transport=transport)

    @property
    def available(self) -> bool:
        return bool(self._app_id and self._app_secret)

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError(
                "飞书连接器不可用：请配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"
            )

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip() or "无响应详情"
            raise FeishuAPIError(
                f"飞书 API 返回 HTTP {response.status_code}：{detail}"
            ) from exc

        payload = response.json()
        code = payload.get("code", 0)
        if code != 0:
            message = payload.get("msg") or "未知错误"
            raise FeishuAPIError(f"飞书 API 错误 {code}：{message}")
        return payload

    def _get_tenant_token(self) -> str:
        self._require_available()
        now = time.monotonic()
        if self._tenant_token is not None and now < self._tenant_token_expires_at:
            return self._tenant_token

        response = self._client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        payload = self._decode_response(response)
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuAPIError("飞书认证响应缺少 tenant_access_token")
        self._tenant_token = str(token)
        expires_in = int(payload.get("expire", 7200))
        self._tenant_token_expires_at = time.monotonic() + max(
            expires_in - _TOKEN_REFRESH_MARGIN_SECONDS,
            0,
        )
        return self._tenant_token

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict:
        response = self._client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {self._get_tenant_token()}"},
        )
        return self._decode_response(response)

    def list_documents(self, since: datetime | None) -> Iterable[DocumentRef]:
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {"page_size": 50}
            if page_token is not None:
                params["page_token"] = page_token
            payload = self._get("/open-apis/drive/v1/files", params=params)
            data = payload.get("data") or {}

            for item in data.get("files", []):
                if item.get("type") != "docx":
                    continue
                mtime = datetime.fromtimestamp(
                    int(item["modified_time"]),
                    tz=timezone.utc,
                )
                if since is not None and mtime <= since:
                    continue

                token = str(item["token"])
                metadata = self._get(f"/open-apis/docx/v1/documents/{token}")
                document = (metadata.get("data") or {})["document"]
                yield DocumentRef(
                    uri=f"{_DOCUMENT_URI_PREFIX}{token}",
                    mtime=mtime,
                    size=-1,
                    hash=str(document["revision_id"]),
                )

            if not data.get("has_more"):
                break
            page_token = data["next_page_token"]

    def fetch(self, ref: DocumentRef) -> bytes:
        if not ref.uri.startswith(_DOCUMENT_URI_PREFIX):
            raise ValueError(f"不是有效的飞书 docx 引用：{ref.uri}")
        token = ref.uri[len(_DOCUMENT_URI_PREFIX) :]
        if not token or "/" in token:
            raise ValueError(f"不是有效的飞书 docx 引用：{ref.uri}")

        payload = self._get(
            f"/open-apis/docx/v1/documents/{token}/raw_content"
        )
        content = (payload.get("data") or {})["content"]
        return str(content).encode("utf-8")

    def sync(self) -> list[DocumentRef]:
        self._require_available()
        refs = list(self.list_documents(None))
        changed = [
            ref
            for ref in refs
            if self._seen_revisions.get(ref.uri) != ref.hash
        ]
        self._seen_revisions = {ref.uri: ref.hash for ref in refs}
        return changed
