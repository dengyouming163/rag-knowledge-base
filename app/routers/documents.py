"""文档上传接口：`POST /v1/kbs/{kb_id}/documents`。

上传只负责"接收 + 建任务 + 落盘"，解析/切分/向量化交给异步 worker，
所以立即返回 202 和 job_id，客户端靠任务接口轮询进度。

写入顺序刻意是「先提交数据库、后落盘」：
1. 同一份文件重复上传按 content_hash 去重，复用已有 doc_id，不再撞唯一约束；
2. 落盘失败时把 documents 置为 failed，不会留下永远 pending 的行；
3. 反过来，数据库失败时文件还没写，不会产生孤儿文件。

租户只从依赖注入取（请求头 → Phase 3 换成 token），请求体/查询参数里
出现 tenant_id 不会被接受；tenant_id / kb_id 还会做字符白名单校验，
防止被拼进路径时发生目录穿越。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import text

from app.deps import EngineDep, TenantId
from app.schemas import UploadDocumentResponse
from app.services import ensure_kb
from src import ingest_jobs
from src.ingest_events import record_job_event

router = APIRouter(prefix="/v1", tags=["documents"])

ALLOWED_SUFFIX = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}
UPLOAD_ROOT = Path("data/uploads")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_id(name: str, value: str) -> None:
    if not _SAFE_ID.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"invalid {name}")


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
    # 这两个值会拼进落盘路径，先卡字符白名单，杜绝 ../ 之类的穿越
    _validate_id("tenant_id", tenant_id)
    _validate_id("kb_id", kb_id)

    # 归属校验：不存在和不属于你都返回 404，不泄露"这个库存在"
    ensure_kb(engine, tenant_id, kb_id)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise HTTPException(status_code=422, detail=f"unsupported file type: {suffix or '(none)'}")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="empty file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    content_hash = hashlib.sha256(content).hexdigest()[:16]
    duplicated = False

    with engine.begin() as connection:
        existing = connection.execute(
            text(
                "SELECT id FROM documents "
                "WHERE tenant_id = :t AND kb_id = :kb AND content_hash = :hash"
            ),
            {"t": tenant_id, "kb": kb_id, "hash": content_hash},
        ).scalar_one_or_none()

        if existing is not None:
            # 同一份内容已经收过：复用原文档，避免唯一约束报 500
            doc_id = existing
            duplicated = True
        else:
            doc_id = f"{kb_id}-{uuid4().hex[:8]}"
            inserted = connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                    "VALUES (:id, :t, :kb, :title, :url, :hash, 'pending', '{}'::jsonb) "
                    "ON CONFLICT (tenant_id, kb_id, content_hash) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "id": doc_id,
                    "t": tenant_id,
                    "kb": kb_id,
                    # 原始文件名不参与命名，title 先用服务端生成的 doc_id 占位
                    "title": doc_id,
                    "url": (Path(tenant_id) / kb_id / f"{doc_id}{suffix}").as_posix(),
                    "hash": content_hash,
                },
            ).scalar_one_or_none()
            if inserted is None:
                # 并发上传同一份内容：另一请求先插入了，复用它的 doc_id
                doc_id = connection.execute(
                    text(
                        "SELECT id FROM documents "
                        "WHERE tenant_id = :t AND kb_id = :kb AND content_hash = :hash"
                    ),
                    {"t": tenant_id, "kb": kb_id, "hash": content_hash},
                ).scalar_one()
                duplicated = True

        job_id = ingest_jobs.enqueue(engine, tenant_id, kb_id, doc_id, source="upload")

    # 任务已入队：写一条 queued 事件，驾驶舱据此展示排队起点（best-effort）
    record_job_event(
        engine,
        job_id=job_id,
        tenant_id=tenant_id,
        stage="queued",
        status="started",
        progress=0,
        message=f"source=upload doc_id={doc_id}",
    )

    # 数据库已提交，再落盘：这里的失败只会留下 failed 行，不会留下 pending 行
    relative_path = Path(tenant_id) / kb_id / f"{doc_id}{suffix}"
    destination = UPLOAD_ROOT / relative_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    except OSError as exc:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE documents SET status = 'failed', updated_at = now() "
                    "WHERE id = :id AND tenant_id = :t"
                ),
                {"id": doc_id, "t": tenant_id},
            )
        raise HTTPException(status_code=500, detail="failed to persist upload") from exc

    return UploadDocumentResponse(job_id=job_id, doc_id=doc_id, duplicated=duplicated)
