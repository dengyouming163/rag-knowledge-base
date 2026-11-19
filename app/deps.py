"""依赖注入：数据库引擎与当前租户。

当前租户先用请求头占位（X-Tenant-Id，缺省 demo），Phase 3 接入真实鉴权后
只需替换 `current_tenant` 的实现，路由代码一行都不用改。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy.engine import Engine

from src.db.base import create_db_engine

DEFAULT_TENANT = "demo"


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_db_engine()


def get_engine() -> Engine:
    return _engine()


def current_tenant(
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> str:
    """返回当前请求的租户。

    注意：这里只从请求头取，**绝不从请求体取**——请求体是用户可控的，
    一旦允许传入 tenant_id，多租户隔离就形同虚设。
    """
    return (x_tenant_id or os.environ.get("DEFAULT_TENANT", DEFAULT_TENANT)).strip()


TenantId = Annotated[str, Depends(current_tenant)]
EngineDep = Annotated[Engine, Depends(get_engine)]


@lru_cache(maxsize=1)
def get_embedder():
    """没有真实 Key 时退回 1024 维哈希向量。

    维度必须是 1024：数据库列是 vector(1024)，64 维会直接被 Postgres 拒绝。
    哈希向量没有语义，只保证"链路可用"，检索质量仍以 eval 基线为准。
    """
    mode = os.environ.get("EMBED_MODE", "").lower()
    if mode == "mock" or not os.environ.get("EMBED_API_KEY"):
        from scripts.compare_routes import HashEmbeddingClient

        return HashEmbeddingClient(dim=1024)

    from src.embed import EmbeddingClient

    return EmbeddingClient()


@lru_cache(maxsize=1)
def get_chat_client():
    from fastapi import HTTPException

    from src.llm import ChatClient, LLMError

    try:
        return ChatClient()
    except LLMError as exc:
        # 配置缺失属于"服务不可用"，不是客户端错误
        raise HTTPException(status_code=503, detail=f"LLM 未配置：{exc}") from exc


# 用 Depends 而不是直接调用：测试可以 dependency_overrides 注入假实现
EmbedderDep = Annotated[Any, Depends(get_embedder)]
ChatClientDep = Annotated[Any, Depends(get_chat_client)]


@lru_cache(maxsize=1)
def get_reranker():
    from src.rerank import build_reranker_from_env

    return build_reranker_from_env()


RerankerDep = Annotated[Any, Depends(get_reranker)]
