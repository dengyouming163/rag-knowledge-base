"""Task 5：入库执行器测试（解析 → 切分 → 嵌入 → 落库 → BM25 → 文档状态）。

所有 embedding 与 PDF 解析都使用假组件，不联网；数据库用例沿用
tests/test_ingest_jobs.py 的策略：连不通就直接 skip，避免 CI 因缺少 Postgres 失败。
"""

from __future__ import annotations

import httpx
import pytest
from pypdf import PdfWriter
from sqlalchemy import text

from src.bm25_store import BM25Store
from src.chunk import split_document
from src.db.base import create_db_engine
from src.ingest import NonRetryableIngestError, ingest_document
from src.parsers.pdf import ParserUnavailable, PdfBlock, PdfParserClient

TENANT = "t_ingest"
KB = "kb_ingest"
DOC = "doc_ingest"
DIM = 1024

MARKDOWN = "# 售后政策\n\n## 退款\n\n支持七天无理由退款。"

# 多章节文档：父块与子块都不止一个，才能覆盖父子交错时的向量顺序
MULTI_SECTION_MARKDOWN = (
    "# 售后政策\n\n## 退款\n\n支持七天无理由退款。\n\n"
    "## 换货\n\n签收后七天内可申请换货。\n\n"
    "## 发票\n\n支持开具电子发票。"
)


class FakeEmbed:
    """记录每次收到的文本，返回可区分的 1024 维向量（与 chunks.embedding 维度一致）。

    每个向量按输入序号取不同值，落库后可用 `embedding::text` 去重来验证
    每个子块拿到的是自己的向量，而不是整批共用同一个值。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(index + 1)] * DIM for index, _ in enumerate(texts)]


class FailingEmbed:
    """模拟 embedding 服务故障：异常必须冒泡，不能被执行器吞掉。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embed down")


class FakeParser:
    """替代 PdfParserClient：不解析真实 PDF，只验证执行器确实调用了它。"""

    def __init__(self) -> None:
        self.called = False

    def parse(self, content: bytes, filename: str = "") -> list[PdfBlock]:
        self.called = True
        return [PdfBlock(type="text", text="# 政策\n\n七天无理由退款。", page=1)]


def _cleanup(engine) -> None:
    """按外键依赖顺序清理；chunks 开了 RLS，删除前必须声明租户。"""
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def _seed(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ingest')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'ingest kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:doc, :t, :kb, '售后政策', '', 'ingest_doc_hash', 'pending', "
                "'{}'::jsonb)"
            ),
            {"doc": DOC, "t": TENANT, "kb": KB},
        )


def _scoped_rows(engine, sql: str, params: dict) -> list[dict]:
    """在带 app.tenant_id 的事务里查询 chunks，绕过 RLS 的 fail-closed。"""
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _doc_status(engine) -> str:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT status FROM documents WHERE id = :doc AND tenant_id = :t"),
            {"doc": DOC, "t": TENANT},
        ).scalar_one()


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")

    _cleanup(db_engine)
    _seed(db_engine)
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def test_ingest_markdown_end_to_end(engine, tmp_path) -> None:
    path = tmp_path / "policy.md"
    path.write_text(MARKDOWN, encoding="utf-8")

    result = ingest_document(
        engine,
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=DOC,
        path=path,
        embed_client=FakeEmbed(),
    )

    assert result["chunks"] >= 1
    assert result["doc_status"] == "ready"

    rows = _scoped_rows(
        engine,
        "SELECT is_parent, embedding IS NULL AS no_vector FROM chunks "
        "WHERE tenant_id = :t AND doc_id = :doc",
        {"t": TENANT, "doc": DOC},
    )
    assert len(rows) == result["chunks"]
    parents = [row for row in rows if row["is_parent"]]
    children = [row for row in rows if not row["is_parent"]]
    # 父块只用于补上下文，绝不写入向量；子块必须全部有向量
    assert parents and all(row["no_vector"] for row in parents)
    assert children and all(not row["no_vector"] for row in children)

    assert _doc_status(engine) == "ready"
    assert BM25Store(engine).version(KB) is not None


def test_ingest_pdf_uses_parser_client(engine, tmp_path) -> None:
    path = tmp_path / "policy.pdf"
    path.write_bytes(b"%PDF-1.4 fake bytes")
    fake_parser = FakeParser()

    result = ingest_document(
        engine,
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=DOC,
        path=path,
        embed_client=FakeEmbed(),
        parser=fake_parser,
    )

    assert fake_parser.called is True
    assert result["chunks"] >= 1
    assert _doc_status(engine) == "ready"


def test_scanned_pdf_raises_non_retryable_ingest_error(tmp_path) -> None:
    class ScannedParser:
        def parse(self, content: bytes, filename: str = "") -> list[PdfBlock]:
            raise ParserUnavailable("扫描件")

    path = tmp_path / "scanned.pdf"
    path.write_bytes(b"%PDF-1.4 scanned")

    with pytest.raises(NonRetryableIngestError) as exc_info:
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
            parser=ScannedParser(),
        )

    assert "扫描件" in str(exc_info.value)
    assert "需要 OCR" in str(exc_info.value)


def test_non_retryable_parser_error_without_ocr_context_keeps_message(tmp_path) -> None:
    message = "请求格式错误；本地降级需要 pypdf"

    class InvalidParser:
        def parse(self, content: bytes, filename: str = "") -> list[PdfBlock]:
            raise ParserUnavailable(message, retryable=False)

    path = tmp_path / "invalid.pdf"
    path.write_bytes(b"%PDF-1.4 invalid")

    with pytest.raises(NonRetryableIngestError) as exc_info:
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
            parser=InvalidParser(),
        )

    assert str(exc_info.value) == message


@pytest.mark.parametrize("message", ["解析服务不可用；本地解析失败", "需要 OCR"])
def test_other_parser_unavailable_is_not_non_retryable(tmp_path, message) -> None:
    class UnavailableParser:
        def parse(self, content: bytes, filename: str = "") -> list[PdfBlock]:
            raise ParserUnavailable(message)

    path = tmp_path / "unavailable.pdf"
    path.write_bytes(b"%PDF-1.4 unavailable")

    with pytest.raises(ParserUnavailable) as exc_info:
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
            parser=UnavailableParser(),
        )

    assert type(exc_info.value) is ParserUnavailable


def _write_blank_pdf(path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)


def test_remote_503_with_empty_local_fallback_remains_retryable(tmp_path) -> None:
    path = tmp_path / "remote-503.pdf"
    _write_blank_pdf(path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="parser temporarily unavailable")

    with PdfParserClient(
        "http://parser:8000", transport=httpx.MockTransport(handler)
    ) as parser:
        with pytest.raises(ParserUnavailable) as exc_info:
            ingest_document(
                None,  # type: ignore[arg-type]
                tenant_id=TENANT,
                kb_id=KB,
                doc_id=DOC,
                path=path,
                embed_client=FakeEmbed(),
                parser=parser,
            )

    assert exc_info.value.retryable is True
    assert "扫描件" in str(exc_info.value)
    assert "需要 OCR" in str(exc_info.value)


def test_remote_ocr_422_with_empty_local_fallback_is_non_retryable(tmp_path) -> None:
    path = tmp_path / "remote-422.pdf"
    _write_blank_pdf(path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "扫描件 OCR 未识别出文本"})

    with PdfParserClient(
        "http://parser:8000", transport=httpx.MockTransport(handler)
    ) as parser:
        with pytest.raises(NonRetryableIngestError) as exc_info:
            ingest_document(
                None,  # type: ignore[arg-type]
                tenant_id=TENANT,
                kb_id=KB,
                doc_id=DOC,
                path=path,
                embed_client=FakeEmbed(),
                parser=parser,
            )

    assert "扫描件" in str(exc_info.value)
    assert "需要 OCR" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ParserUnavailable)
    assert exc_info.value.__cause__.retryable is False


def test_reingest_same_doc_does_not_duplicate_chunks(engine, tmp_path) -> None:
    path = tmp_path / "policy.md"
    path.write_text(MARKDOWN, encoding="utf-8")

    first = ingest_document(
        engine,
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=DOC,
        path=path,
        embed_client=FakeEmbed(),
    )
    before = _scoped_rows(
        engine,
        "SELECT chunk_id FROM chunks WHERE tenant_id = :t AND doc_id = :doc",
        {"t": TENANT, "doc": DOC},
    )
    second = ingest_document(
        engine,
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=DOC,
        path=path,
        embed_client=FakeEmbed(),
    )
    after = _scoped_rows(
        engine,
        "SELECT chunk_id FROM chunks WHERE tenant_id = :t AND doc_id = :doc",
        {"t": TENANT, "doc": DOC},
    )

    assert second["chunks"] == first["chunks"]
    assert len(after) == len(before)
    assert {row["chunk_id"] for row in after} == {row["chunk_id"] for row in before}


def test_embed_receives_only_child_chunks(engine, tmp_path) -> None:
    """多章节文档：父块不参与 embedding，且每个子块拿到各自顺序的向量。"""
    path = tmp_path / "policy.md"
    path.write_text(MULTI_SECTION_MARKDOWN, encoding="utf-8")
    fake_embed = FakeEmbed()

    ingest_document(
        engine,
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=DOC,
        path=path,
        embed_client=fake_embed,
    )

    chunks = split_document(MULTI_SECTION_MARKDOWN, doc_id=DOC)
    child_texts = [chunk.text for chunk in chunks if not chunk.is_parent]
    parent_count = sum(1 for chunk in chunks if chunk.is_parent)

    # 多章节才有多个父块与子块，父子交错时最容易把向量错位
    assert parent_count >= 2
    assert len(child_texts) >= 2
    # 只调用一次，且收到的文本序列与子块逐条一致——父块不会混进来
    assert fake_embed.calls == [child_texts]
    assert len(fake_embed.calls[0]) == len(chunks) - parent_count

    rows = _scoped_rows(
        engine,
        "SELECT embedding::text AS embedding_text FROM chunks "
        "WHERE tenant_id = :t AND doc_id = :doc AND is_parent = false",
        {"t": TENANT, "doc": DOC},
    )
    # 子块各拿各的向量：落库后的向量文本去重数量必须等于子块数量。
    # 若执行器把同一批向量整体错位/复用，这里会塌缩成更少的唯一值。
    assert len(rows) == len(child_texts)
    assert len({row["embedding_text"] for row in rows}) == len(child_texts)


def test_embedding_failure_keeps_document_pending(engine, tmp_path) -> None:
    """embedding 失败必须上抛，文档保持 pending，且不会清掉旧块（首次导入时块数为 0）。"""
    path = tmp_path / "policy.md"
    path.write_text(MARKDOWN, encoding="utf-8")

    with pytest.raises(RuntimeError, match="embed down"):
        ingest_document(
            engine,
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FailingEmbed(),
        )

    # 不能假成功：文档仍是 pending，等待 worker 重试或标记 failed
    assert _doc_status(engine) == "pending"
    # embedding 在 delete/upsert 之前发生，所以首次导入不会留下任何块
    rows = _scoped_rows(
        engine,
        "SELECT chunk_id FROM chunks WHERE tenant_id = :t AND doc_id = :doc",
        {"t": TENANT, "doc": DOC},
    )
    assert rows == []


def test_unsupported_extension_raises_before_touching_database(tmp_path) -> None:
    """engine 传 None：后缀校验必须在建 store / 连库之前失败。"""
    path = tmp_path / "policy.docx"
    path.write_bytes(b"whatever")

    with pytest.raises(ValueError, match="不支持"):
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
        )


def test_empty_text_raises(tmp_path) -> None:
    """空内容不能静默成功，否则文档会被标为 ready 却没有任何块。"""
    path = tmp_path / "empty.md"
    path.write_text("   \n\n", encoding="utf-8")

    with pytest.raises(ValueError):
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
        )
