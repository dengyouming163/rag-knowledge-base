"""入库执行器：把已落盘的原始文件变成可检索的知识块。

执行顺序固定为：
    解析 → 拼 Markdown → 切分 → 批量嵌入 → 落库 → 重建 BM25 → 更新文档状态

两个刻意的设计：
1. 父块不参与 embedding。父块只在检索命中子块后补上下文，写入 `None` 向量，
   与 PgVectorStore.upsert 的既有契约保持一致。
2. 任何异常都向上抛，由 worker 统一调用 mark_failed。执行器不做兜底，
   否则失败会被伪装成 ready，用户看到"入库成功"却检索不到内容。

提取阶段必须先于建 store / 连库：后缀不支持或内容为空时，不应该产生任何
数据库副作用，也不需要数据库可用（见 tests/test_ingest.py 的 None engine 用例）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .bm25_store import BM25Store
from .chunk import split_document
from .parsers.pdf import PdfParserClient, blocks_to_text
from .store_pg import PgVectorStore

_TEXT_SUFFIXES = {".md", ".txt"}


def _extract_text(file_path: Path, parser: Any) -> str:
    """按后缀提取纯文本/Markdown；不支持的格式直接抛 ValueError。"""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        content = file_path.read_bytes()
        # 只在真正解析 PDF 时才创建客户端：构造本身不联网，
        # 但没必要为 Markdown 上传也持有一个 httpx.Client。
        if parser is None:
            with PdfParserClient() as client:
                blocks = client.parse(content, filename=file_path.name)
        else:
            blocks = parser.parse(content, filename=file_path.name)
        return blocks_to_text(blocks)

    if suffix in _TEXT_SUFFIXES:
        return file_path.read_text(encoding="utf-8")

    raise ValueError(f"不支持的文件类型：{suffix or '(none)'}")


def ingest_document(
    engine: Engine,
    *,
    tenant_id: str,
    kb_id: str,
    doc_id: str,
    path: str | Path,
    embed_client: Any,
    parser: Any = None,
) -> dict:
    """解析并入库一份文档，成功后返回 {"chunks": int, "doc_status": "ready"}。

    `parser` 可注入（测试/自定义解析服务）；为 None 时延迟创建 PdfParserClient。
    """
    file_path = Path(path)
    text_content = _extract_text(file_path, parser)
    if not text_content.strip():
        # 空内容如果继续走流程，最终会把文档标成 ready 却没有任何块，
        # 这类"假成功"比直接失败更难排查。
        raise ValueError(f"文档 {doc_id} 提取结果为空，无法入库")

    chunks = split_document(text_content, doc_id=doc_id)
    texts = [chunk.text for chunk in chunks if not chunk.is_parent]
    vectors = embed_client.embed_texts(texts)
    if len(vectors) != len(texts):
        raise ValueError(
            f"embedding 返回数量与子块数量不一致：期望 {len(texts)}，实际 {len(vectors)}"
        )

    # 按 chunks 原始顺序组装：父块占位 None，子块按 embedding 返回顺序消费。
    remaining = iter(vectors)
    vectors_with_none: list[list[float] | None] = [
        None if chunk.is_parent else next(remaining) for chunk in chunks
    ]

    store = PgVectorStore(engine, tenant_id=tenant_id, kb_id=kb_id)
    # 重复导入同一文档时先清旧块，保证幂等：既避免 chunk_id 主键冲突，
    # 也防止内容变短后留下不再属于本文档的孤儿块。
    store.delete_by_doc(doc_id)
    store.upsert(chunks, vectors_with_none)  # type: ignore[arg-type]

    # 入库完成后立即重建 BM25，避免检索层拿到与新 chunks 不一致的旧索引。
    BM25Store(engine).rebuild(tenant_id, kb_id)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE documents SET status = 'ready', updated_at = now() "
                "WHERE id = :doc_id AND tenant_id = :tenant_id AND kb_id = :kb_id"
            ),
            {"doc_id": doc_id, "tenant_id": tenant_id, "kb_id": kb_id},
        )

    return {"chunks": len(chunks), "doc_status": "ready"}
