"""Task 2：ingest_jobs 状态机（入队 / 领取 / 完成 / 失败 / 重试）。

核心断言：
1. 同一 (tenant, kb, doc_id) 已有排队/运行中的任务时不重复入队；
2. 领取是原子的：同一条任务不会被两个 worker 同时拿到；
3. 连续失败到 MAX_ATTEMPTS 后进入终态 failed；
4. get_job 必须带 tenant_id，跨租户查不到。
"""

from __future__ import annotations

import threading

import httpx
import pytest
from pypdf import PdfWriter
from sqlalchemy import text

from src.db.base import create_db_engine
from src.ingest_jobs import (
    claim_next,
    enqueue,
    get_job,
    mark_failed,
    mark_succeeded,
)
from src.parsers.pdf import ParserUnavailable, PdfParserClient
from src.worker import worker_once

TENANT = "t_jobs"
KB = "kb_jobs"


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
            text("INSERT INTO tenants (id, name) VALUES (:t, 'jobs')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'jobs kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )


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


def test_claim_is_idempotent_under_concurrency(engine) -> None:
    """一个连接持锁不提交，另一个 engine 的 worker 必须被 SKIP LOCKED 跳过。"""
    job_id = enqueue(engine, TENANT, KB, doc_id="d1", source="upload")
    holder_engine = create_db_engine()
    worker_engine = create_db_engine()
    claimed: list[dict | None] = []
    errors: list[BaseException] = []
    try:
        with holder_engine.connect() as holder:
            transaction = holder.begin()
            locked = holder.execute(
                text(
                    "SELECT id FROM ingest_jobs WHERE status = 'queued' "
                    "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
                )
            ).scalar_one()
            assert locked == job_id

            def work() -> None:
                try:
                    claimed.append(claim_next(worker_engine, worker="w2"))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "claim_next 在持锁期间被阻塞，SKIP LOCKED 未生效"
            assert not errors, errors
            assert claimed == [None]
            transaction.rollback()

        after = claim_next(worker_engine, worker="w1")
        assert after is not None and after["id"] == job_id
    finally:
        holder_engine.dispose()
        worker_engine.dispose()


def test_enqueue_is_concurrency_safe(engine) -> None:
    """同一文档并发入队，咨询锁保证最终只有一条任务。"""
    workers = 4
    barrier = threading.Barrier(workers)
    ids: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def work() -> None:
        try:
            barrier.wait(timeout=5)
            job_id = enqueue(engine, TENANT, KB, doc_id="d6")
            with lock:
                ids.append(job_id)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads), "并发 enqueue 超时"
    assert not errors, errors
    assert len(ids) == workers and len(set(ids)) == 1, ids

    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM ingest_jobs "
                "WHERE tenant_id = :t AND kb_id = :kb AND doc_id = 'd6'"
            ),
            {"t": TENANT, "kb": KB},
        ).scalar_one()
    assert count == 1


def test_failure_after_three_attempts_marks_failed(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d2")
    for _ in range(3):
        job = claim_next(engine, worker="w1")
        mark_failed(engine, job["id"], "boom")
    assert get_job(engine, TENANT, job_id)["status"] == "failed"


def test_enqueue_is_idempotent(engine) -> None:
    first = enqueue(engine, TENANT, KB, doc_id="d3")
    second = enqueue(engine, TENANT, KB, doc_id="d3")

    assert first == second


def test_get_job_rejects_other_tenant(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d4")

    assert get_job(engine, TENANT, job_id) is not None
    assert get_job(engine, "t_jobs_other", job_id) is None


def test_mark_succeeded_sets_finished_at(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d5")
    claim_next(engine, worker="w1")

    mark_succeeded(engine, job_id)

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    assert job["finished_at"] is not None


class FakeEmbed:
    """返回与 chunks.embedding 列维度一致的确定性假向量，不访问网络。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * 1024 for _ in texts]


def _seed_document(engine, doc_id: str, source_url: str, content_hash: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:doc, :t, :kb, :doc, :url, :hash, 'pending', '{}'::jsonb)"
            ),
            {
                "doc": doc_id,
                "t": TENANT,
                "kb": KB,
                "url": source_url,
                "hash": content_hash,
            },
        )


def test_worker_once_processes_one_job(engine, tmp_path, monkeypatch) -> None:
    source_url = f"{TENANT}/{KB}/d1.md"
    _seed_document(engine, "d1", source_url, "worker_doc_hash_1")
    job_id = enqueue(engine, TENANT, KB, doc_id="d1")
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
    path = tmp_path / TENANT / KB / "d1.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 售后政策\n\n## 退款\n\n支持七天无理由退款。", encoding="utf-8")

    assert worker_once(engine, embed_client=FakeEmbed()) == 1

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        doc_status = connection.execute(
            text("SELECT status FROM documents WHERE id = 'd1' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
        child_count = connection.execute(
            text(
                "SELECT count(*) FROM chunks "
                "WHERE tenant_id = :t AND doc_id = 'd1' AND is_parent = false"
            ),
            {"t": TENANT},
        ).scalar_one()
    assert doc_status == "ready"
    assert child_count > 0

    assert worker_once(engine, embed_client=FakeEmbed()) == 0


def test_worker_marks_document_failed_after_max_retries(
    engine, tmp_path, monkeypatch
) -> None:
    source_url = f"{TENANT}/{KB}/missing.md"
    _seed_document(engine, "d2", source_url, "worker_doc_hash_2")
    job_id = enqueue(engine, TENANT, KB, doc_id="d2")
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)

    for _ in range(3):
        assert worker_once(engine, embed_client=FakeEmbed()) == 1

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "failed"
    assert job["finished_at"] is not None
    assert job["error"]
    with engine.begin() as connection:
        doc_status = connection.execute(
            text("SELECT status FROM documents WHERE id = 'd2' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
    assert doc_status == "failed"


def test_worker_fails_scanned_document_without_retry(
    engine, tmp_path, monkeypatch
) -> None:
    class ScannedParser:
        def parse(self, content: bytes, filename: str = ""):
            raise ParserUnavailable("扫描件")

    source_url = f"{TENANT}/{KB}/scanned.pdf"
    _seed_document(engine, "scanned", source_url, "worker_scanned_doc_hash")
    job_id = enqueue(engine, TENANT, KB, doc_id="scanned")
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
    path = tmp_path / TENANT / KB / "scanned.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4 scanned")

    assert worker_once(engine, embed_client=FakeEmbed(), parser=ScannedParser()) == 1

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert "扫描件" in job["error"]
    assert "需要 OCR" in job["error"]
    assert job["finished_at"] is not None
    with engine.begin() as connection:
        doc_status = connection.execute(
            text("SELECT status FROM documents WHERE id = 'scanned' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
    assert doc_status == "failed"

    assert worker_once(engine, embed_client=FakeEmbed(), parser=ScannedParser()) == 0


def test_worker_retries_remote_503_when_local_pdf_has_no_text(
    engine, tmp_path, monkeypatch
) -> None:
    source_url = f"{TENANT}/{KB}/remote-503.pdf"
    _seed_document(engine, "remote-503", source_url, "worker_remote_503_doc_hash")
    job_id = enqueue(engine, TENANT, KB, doc_id="remote-503")
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
    path = tmp_path / TENANT / KB / "remote-503.pdf"
    path.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="parser temporarily unavailable")

    with PdfParserClient(
        "http://parser:8000", transport=httpx.MockTransport(handler)
    ) as parser:
        assert worker_once(engine, embed_client=FakeEmbed(), parser=parser) == 1

        job = get_job(engine, TENANT, job_id)
        assert job["status"] == "queued"
        assert job["attempts"] == 1
        assert job["finished_at"] is None
        assert "扫描件" in job["error"]
        with engine.begin() as connection:
            doc_status = connection.execute(
                text("SELECT status FROM documents WHERE id = 'remote-503' AND tenant_id = :t"),
                {"t": TENANT},
            ).scalar_one()
        assert doc_status == "pending"

        assert worker_once(engine, embed_client=FakeEmbed(), parser=parser) == 1
        assert worker_once(engine, embed_client=FakeEmbed(), parser=parser) == 1
        assert worker_once(engine, embed_client=FakeEmbed(), parser=parser) == 0

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "failed"
    assert job["attempts"] == 3
    assert job["finished_at"] is not None
    with engine.begin() as connection:
        doc_status = connection.execute(
            text("SELECT status FROM documents WHERE id = 'remote-503' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
    assert doc_status == "failed"


def test_worker_retries_ocr_only_parser_unavailable(
    engine, tmp_path, monkeypatch
) -> None:
    class OcrOnlyParser:
        def parse(self, content: bytes, filename: str = ""):
            raise ParserUnavailable("需要 OCR")

    source_url = f"{TENANT}/{KB}/ocr-only.pdf"
    _seed_document(engine, "ocr-only", source_url, "worker_ocr_only_doc_hash")
    job_id = enqueue(engine, TENANT, KB, doc_id="ocr-only")
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
    path = tmp_path / TENANT / KB / "ocr-only.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4 ocr only")

    assert worker_once(engine, embed_client=FakeEmbed(), parser=OcrOnlyParser()) == 1

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "queued"
    assert job["attempts"] == 1
    assert job["finished_at"] is None
    assert job["error"] == "ParserUnavailable: 需要 OCR"
    with engine.begin() as connection:
        doc_status = connection.execute(
            text("SELECT status FROM documents WHERE id = 'ocr-only' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
    assert doc_status == "pending"

    assert worker_once(engine, embed_client=FakeEmbed(), parser=OcrOnlyParser()) == 1
    assert worker_once(engine, embed_client=FakeEmbed(), parser=OcrOnlyParser()) == 1
    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "failed"
    assert job["attempts"] == 3


def test_worker_once_returns_zero_when_idle(engine) -> None:
    assert worker_once(engine, embed_client=FakeEmbed()) == 0
