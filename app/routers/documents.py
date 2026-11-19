"""文档上传接口：`POST /v1/kbs/{kb_id}/documents`。

上传只负责"接收 + 落盘 + 建任务"，解析/切分/向量化交给异步 worker，
所以立即返回 202 和 job_id，客户端靠任务接口轮询进度。

租户只从依赖注入取（请求头 → Phase 3 换成 token），请求体/查询参数里
出现 tenant_id 不会被接受。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import text

from app.deps import EngineDep, TenantId
from app.schemas import UploadDocumentResponse
from app.services import ensure_kb
from src import ingest_jobs

router = APIRouter(prefix="/v1", tags=["documents"])

ALLOWED_SUFFIX = {".pdf", ".md", ".txt"}
UPLOAD_ROOT = Path("data/uploads")


@router.post(
    "/kbs/{kb_id}/documents",
    status_code=202,
    response_model=UploadDocumentResponse,
)
async def upload_document(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    file: Annotated[UploadFile, File()],
) -> UploadDocumentResponse:
    # 先校验归属：不存在和不属于你都返回 404，不泄露"这个库存在"
    ensure_kb(engine, tenant_id, kb_id)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise HTTPException(status_code=422, detail=f"unsupported file type: {suffix or '(none)'}")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="empty file")

    doc_id = f"{kb_id}-{uuid4().hex[:8]}"
    # 文件名不参与路径拼接：避免 `../` 之类的路径穿越，只用受控的 doc_id
    relative_path = Path(tenant_id) / kb_id / f"{doc_id}{suffix}"
    destination = UPLOAD_ROOT / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:id, :t, :kb, :title, :url, :hash, 'pending', '{}'::jsonb)"
            ),
            {
                "id": doc_id,
                "t": tenant_id,
                "kb": kb_id,
                "title": Path(file.filename or doc_id).name[:256],
                "url": relative_path.as_posix(),
                "hash": hashlib.sha256(content).hexdigest()[:16],
            },
        )

    job_id = ingest_jobs.enqueue(engine, tenant_id, kb_id, doc_id, source="upload")
    return UploadDocumentResponse(job_id=job_id, doc_id=doc_id)
