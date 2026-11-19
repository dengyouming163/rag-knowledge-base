"""请求/响应模型。

`extra="forbid"` 是安全边界：调用方如果传了 `tenant_id` 或 `kb_id` 字段，
会直接 422，而不是被静默忽略——后者会让人误以为"我传的租户生效了"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Audience = Literal["public", "internal"]
Mode = Literal["hybrid", "vector", "bm25"]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    mode: Mode = "hybrid"
    audience: Audience = "public"


class HitOut(BaseModel):
    chunk_id: str
    doc_id: str
    score: float
    text: str
    metadata: dict


class SearchResponse(BaseModel):
    kb_id: str
    query: str
    mode: Mode
    hits: list[HitOut]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=20)
    audience: Audience = "public"


class UploadDocumentResponse(BaseModel):
    job_id: int
    doc_id: str
