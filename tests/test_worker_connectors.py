"""Task 14：worker 定时接收 Source 文档并复用异步入库链路。"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

import src.ingest_jobs as ingest_jobs
from scripts import run_worker
from src.connectors.base import DocumentRef
from src.db.base import create_db_engine
from src.worker import worker_once

TENANT = "t_worker_connectors"
KB = "kb_worker_connectors"


class FakeEmbed:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * 1024 for _ in texts]


class FakeSource:
    def __init__(
        self,
        refs: list[DocumentRef] | None = None,
        contents: dict[str, bytes] | None = None,
        *,
        name: str = "local_dir",
        sync_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.refs = refs or []
        self.contents = contents or {}
        self.sync_error = sync_error
        self.fetch_errors: set[str] = set()
        self.sync_calls = 0

    def sync(self) -> list[DocumentRef]:
        self.sync_calls += 1
        if self.sync_error is not None:
            raise self.sync_error
        return self.refs

    def fetch(self, ref: DocumentRef) -> bytes:
        if ref.uri in self.fetch_errors:
            raise OSError("injected fetch failure")
        return self.contents[ref.uri]


class ClosableSource(FakeSource):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _ref(uri: str, content: bytes) -> DocumentRef:
    return DocumentRef(
        uri=uri,
        mtime=datetime.now(timezone.utc),
        size=len(content),
        hash=hashlib.sha256(content).hexdigest(),
    )


def _receive(engine, **kwargs):
    assert hasattr(ingest_jobs, "receive_source_document"), "缺少来源文档收件函数"
    return ingest_jobs.receive_source_document(engine, TENANT, KB, **kwargs)


def _sync(engine, sources, **kwargs):
    assert hasattr(run_worker, "sync_sources"), "缺少 Source 同步函数"
    return run_worker.sync_sources(
        engine,
        sources,
        tenant_id=TENANT,
        kb_id=KB,
        **kwargs,
    )


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'connectors')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'connectors kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def _document_and_job(engine, doc_id: str) -> dict:
    with engine.begin() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT d.source_url, d.content_hash, d.status AS doc_status, "
                    "j.id AS job_id, j.status AS job_status, j.source "
                    "FROM documents d JOIN ingest_jobs j ON j.doc_id = d.id "
                    "WHERE d.id = :doc_id AND d.tenant_id = :t"
                ),
                {"doc_id": doc_id, "t": TENANT},
            ).mappings().one()
        )


def test_received_document_uses_upload_layout_and_existing_worker(
    engine, tmp_path, monkeypatch
) -> None:
    def enqueue_running(engine, tenant_id, kb_id, doc_id, *, source):
        with engine.begin() as connection:
            return int(
                connection.execute(
                    text(
                        "INSERT INTO ingest_jobs "
                        "(tenant_id, kb_id, doc_id, status, error, source, attempts, progress) "
                        "VALUES (:t, :kb, :d, 'running', '', :source, 1, 10) RETURNING id"
                    ),
                    {"t": tenant_id, "kb": kb_id, "d": doc_id, "source": source},
                ).scalar_one()
            )

    monkeypatch.setattr(ingest_jobs, "enqueue", enqueue_running)
    content = b"# connector policy\n\nsupport refunds within seven days."
    uri = "file:///source/policy.md"

    received = _receive(
        engine,
        source_name="local_dir",
        uri=uri,
        content=content,
        upload_root=tmp_path,
    )

    assert received["duplicated"] is False
    assert received["doc_id"].startswith(f"{KB}-")
    assert received["job_id"] > 0
    row = _document_and_job(engine, received["doc_id"])
    assert row["content_hash"] == hashlib.sha256(content).hexdigest()[:16]
    assert row["doc_status"] == "pending"
    assert row["job_status"] == "running"
    assert row["source"] == "local_dir"
    assert (tmp_path / row["source_url"]).read_bytes() == content

    running_job = ingest_jobs.get_job(engine, TENANT, received["job_id"])
    assert running_job is not None and running_job["status"] == "running"
    claims = iter([running_job, None])
    monkeypatch.setattr("src.worker.claim_next", lambda *args, **kwargs: next(claims))
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
    assert worker_once(engine, embed_client=FakeEmbed()) == 1
    row = _document_and_job(engine, received["doc_id"])
    assert row["doc_status"] == "ready"
    assert row["job_status"] == "succeeded"
    assert worker_once(engine, embed_client=FakeEmbed()) == 0

    duplicate = _receive(
        engine,
        source_name="local_dir",
        uri=uri,
        content=content,
        upload_root=tmp_path,
    )
    assert duplicate == {
        "doc_id": received["doc_id"],
        "job_id": None,
        "duplicated": True,
    }
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 1


def test_duplicate_while_queued_reuses_job_and_feishu_is_markdown(
    engine, tmp_path
) -> None:
    content = "飞书正文".encode()
    kwargs = {
        "source_name": "feishu",
        "uri": "feishu://docx/doc-1",
        "content": content,
        "upload_root": tmp_path,
    }

    first = _receive(engine, **kwargs)
    second = _receive(engine, **kwargs)

    assert second["duplicated"] is True
    assert second["job_id"] == first["job_id"]
    row = _document_and_job(engine, first["doc_id"])
    assert row["source"] == "feishu"
    assert row["source_url"].endswith(".md")
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 1


def test_active_duplicate_reuses_job_without_rewriting_file(
    engine, tmp_path, monkeypatch
) -> None:
    kwargs = {
        "source_name": "local_dir",
        "uri": "file:///source/active.txt",
        "content": b"complete content",
        "upload_root": tmp_path,
    }
    first = _receive(engine, **kwargs)
    original_write_bytes = Path.write_bytes
    writes = []

    def track_write(path, data):
        writes.append(path)
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", track_write)
    duplicate = _receive(engine, **kwargs)

    assert duplicate["job_id"] == first["job_id"]
    assert duplicate["duplicated"] is True
    assert writes == []


def test_interleaved_reader_never_observes_partial_destination(
    engine, tmp_path, monkeypatch
) -> None:
    content = b"complete atomic content"
    observed = []

    def interleaved_write(path, data):
        with engine.begin() as connection:
            source_url = connection.execute(
                text("SELECT source_url FROM documents WHERE tenant_id = :t"),
                {"t": TENANT},
            ).scalar_one()
        destination = tmp_path / source_url
        with path.open("wb") as stream:
            stream.write(data[:3])
            stream.flush()
            observed.append(destination.read_bytes() if destination.exists() else None)
            stream.write(data[3:])
        return len(data)

    monkeypatch.setattr(Path, "write_bytes", interleaved_write)
    received = _receive(
        engine,
        source_name="local_dir",
        uri="file:///source/atomic.txt",
        content=content,
        upload_root=tmp_path,
    )

    row = _document_and_job(engine, received["doc_id"])
    destination = tmp_path / row["source_url"]
    assert observed == [None]
    assert destination.read_bytes() == content
    assert list(destination.parent.iterdir()) == [destination]


def test_atomic_replace_failure_cleans_temporary_file(
    engine, tmp_path, monkeypatch
) -> None:
    def fail_replace(path, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        _receive(
            engine,
            source_name="local_dir",
            uri="file:///source/replace-failure.txt",
            content=b"content",
            upload_root=tmp_path,
        )

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one()
    assert status == "failed"
    assert list((tmp_path / TENANT / KB).iterdir()) == []


def test_changed_content_creates_a_new_document_and_job(engine, tmp_path) -> None:
    uri = "file:///source/policy.txt"
    first = _receive(
        engine,
        source_name="local_dir",
        uri=uri,
        content=b"before",
        upload_root=tmp_path,
    )
    changed = _receive(
        engine,
        source_name="local_dir",
        uri=uri,
        content=b"after",
        upload_root=tmp_path,
    )

    assert changed["duplicated"] is False
    assert changed["doc_id"] != first["doc_id"]
    assert changed["job_id"] != first["job_id"]
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 2


@pytest.mark.parametrize(
    ("tenant_id", "kb_id", "uri", "content"),
    [
        ("../escape", KB, "file:///source/a.txt", b"content"),
        (TENANT, "../escape", "file:///source/a.txt", b"content"),
        (TENANT, KB, "file:///source/a.doc", b"content"),
        (TENANT, KB, "file:///source/no-suffix", b"content"),
        (TENANT, KB, "file:///source/empty.txt", b""),
    ],
)
def test_receive_rejects_unsafe_or_unsupported_input_without_db_rows(
    engine, tmp_path, tenant_id, kb_id, uri, content
) -> None:
    assert hasattr(ingest_jobs, "receive_source_document"), "缺少来源文档收件函数"
    with pytest.raises(ValueError):
        ingest_jobs.receive_source_document(
            engine,
            tenant_id,
            kb_id,
            source_name="local_dir",
            uri=uri,
            content=content,
            upload_root=tmp_path,
        )

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 0


@pytest.mark.parametrize("concurrent_state", ["active", "ready"])
def test_enqueue_failure_preserves_concurrent_document_state(
    engine, tmp_path, monkeypatch, concurrent_state
) -> None:
    original_enqueue = ingest_jobs.enqueue

    def enqueue_then_fail(*args, **kwargs):
        job_id = original_enqueue(*args, **kwargs)
        if concurrent_state == "ready":
            ingest_jobs.mark_succeeded(engine, job_id)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE documents SET status = 'ready', updated_at = now() "
                        "WHERE tenant_id = :t"
                    ),
                    {"t": TENANT},
                )
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(ingest_jobs, "enqueue", enqueue_then_fail)
    content = b"complete content"
    with pytest.raises(RuntimeError, match="injected enqueue failure"):
        _receive(
            engine,
            source_name="local_dir",
            uri="file:///source/concurrent.txt",
            content=content,
            upload_root=tmp_path,
        )

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT d.status AS doc_status, d.source_url, j.status AS job_status "
                "FROM documents d JOIN ingest_jobs j ON j.doc_id = d.id "
                "WHERE d.tenant_id = :t"
            ),
            {"t": TENANT},
        ).mappings().one()
    expected = {"active": ("pending", "queued"), "ready": ("ready", "succeeded")}
    assert (row["doc_status"], row["job_status"]) == expected[concurrent_state]
    assert (tmp_path / row["source_url"]).read_bytes() == content


def test_failure_update_and_enqueue_are_serialized_across_threads(
    engine, tmp_path, monkeypatch
) -> None:
    original_enqueue = ingest_jobs.enqueue
    thread_state = threading.local()
    success_at_enqueue = threading.Event()
    failure_finished = threading.Event()
    outcomes = {}

    def controlled_enqueue(*args, **kwargs):
        if thread_state.role == "success":
            success_at_enqueue.set()
            if not failure_finished.wait(10):
                raise TimeoutError("failure thread did not finish")
            return original_enqueue(*args, **kwargs)
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(ingest_jobs, "enqueue", controlled_enqueue)
    kwargs = {
        "source_name": "local_dir",
        "uri": "file:///source/thread-race.txt",
        "content": b"same complete content",
        "upload_root": tmp_path,
    }

    def succeed() -> None:
        thread_state.role = "success"
        try:
            outcomes["success"] = _receive(engine, **kwargs)
        except Exception as exc:  # noqa: BLE001
            outcomes["success_error"] = exc

    def fail() -> None:
        thread_state.role = "failure"
        try:
            _receive(engine, **kwargs)
        except Exception as exc:  # noqa: BLE001
            outcomes["failure_error"] = str(exc)
        finally:
            failure_finished.set()

    success_thread = threading.Thread(target=succeed)
    failure_thread = threading.Thread(target=fail)
    success_thread.start()
    assert success_at_enqueue.wait(10)
    failure_thread.start()
    failure_thread.join(10)
    success_thread.join(10)

    assert not failure_thread.is_alive()
    assert not success_thread.is_alive()
    assert outcomes.get("failure_error") == "injected enqueue failure"
    assert outcomes.get("success_error") is None
    assert outcomes["success"]["job_id"] > 0
    row = _document_and_job(engine, outcomes["success"]["doc_id"])
    assert (row["doc_status"], row["job_status"]) == ("pending", "queued")


def test_file_write_failure_marks_document_failed_without_a_job(
    engine, tmp_path
) -> None:
    upload_root = tmp_path / "not-a-directory"
    upload_root.write_text("file", encoding="utf-8")

    with pytest.raises(OSError):
        _receive(
            engine,
            source_name="local_dir",
            uri="file:///source/policy.txt",
            content=b"content",
            upload_root=upload_root,
        )

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT status FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).mappings().one()
        assert row["status"] == "failed"
        assert connection.execute(
            text("SELECT count(*) FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 0


def test_source_and_ref_failures_are_isolated_and_failed_ref_is_retried(
    engine, tmp_path, caplog
) -> None:
    bad_uri = "file:///source/bad.txt"
    good_uri = "file:///source/good.txt"
    bad_ref = _ref(bad_uri, b"bad")
    good_ref = _ref(good_uri, b"good")
    mixed = FakeSource([bad_ref, good_ref], {bad_uri: b"bad", good_uri: b"good"})
    mixed.fetch_errors.add(bad_uri)
    broken = FakeSource(name="broken", sync_error=RuntimeError("sync unavailable"))
    retries = {}

    with caplog.at_level(logging.ERROR):
        results = _sync(
            engine,
            [broken, mixed],
            upload_root=tmp_path,
            retry_refs=retries,
        )

    assert len(results) == 1
    assert results[0]["duplicated"] is False
    assert "broken" in caplog.text
    assert bad_uri in caplog.text
    assert bad_uri in retries["local_dir"]
    assert good_uri not in retries["local_dir"]

    mixed.refs = []
    mixed.fetch_errors.clear()
    retried = _sync(
        engine,
        [mixed],
        upload_root=tmp_path,
        retry_refs=retries,
    )
    assert len(retried) == 1
    assert retried[0]["duplicated"] is False
    assert retries == {}
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one() == 2


def test_connector_config_requires_common_target(monkeypatch, tmp_path, caplog) -> None:
    assert hasattr(run_worker, "load_connector_config"), "缺少连接器配置函数"
    monkeypatch.delenv("CONNECTOR_TENANT_ID", raising=False)
    monkeypatch.delenv("CONNECTOR_KB_ID", raising=False)
    monkeypatch.setenv("LOCAL_DIR_PATH", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    with caplog.at_level(logging.INFO):
        tenant_id, kb_id, sources = run_worker.load_connector_config()

    assert (tenant_id, kb_id, sources) == (None, None, [])
    assert "CONNECTOR_TENANT_ID" in caplog.text
    assert "CONNECTOR_KB_ID" in caplog.text
    assert "app-secret" not in caplog.text


def test_connector_config_skips_missing_directory_and_feishu_credentials(
    monkeypatch, caplog
) -> None:
    assert hasattr(run_worker, "load_connector_config"), "缺少连接器配置函数"
    monkeypatch.setenv("CONNECTOR_TENANT_ID", TENANT)
    monkeypatch.setenv("CONNECTOR_KB_ID", KB)
    monkeypatch.delenv("LOCAL_DIR_PATH", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    with caplog.at_level(logging.INFO):
        tenant_id, kb_id, sources = run_worker.load_connector_config()

    assert tenant_id == TENANT
    assert kb_id == KB
    assert sources == []
    assert "LOCAL_DIR_PATH" in caplog.text
    assert "飞书连接器未注册" in caplog.text


def test_connector_config_registers_local_directory_without_feishu(
    monkeypatch, tmp_path
) -> None:
    assert hasattr(run_worker, "load_connector_config"), "缺少连接器配置函数"
    monkeypatch.setenv("CONNECTOR_TENANT_ID", TENANT)
    monkeypatch.setenv("CONNECTOR_KB_ID", KB)
    monkeypatch.setenv("LOCAL_DIR_PATH", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    tenant_id, kb_id, sources = run_worker.load_connector_config()

    assert tenant_id == TENANT
    assert kb_id == KB
    assert len(sources) == 1
    assert sources[0].name == "local_dir"
    assert sources[0].root == tmp_path


def test_source_sync_interval_is_separate_and_immediately_due(engine) -> None:
    assert hasattr(run_worker, "run_due_source_sync"), "缺少到期同步调度函数"
    source = FakeSource()
    retries = {}

    last_sync = run_worker.run_due_source_sync(
        engine,
        [source],
        tenant_id=TENANT,
        kb_id=KB,
        source_interval=30,
        last_sync_at=None,
        now=10.0,
        retry_refs=retries,
    )
    assert last_sync == 10.0
    assert source.sync_calls == 1

    unchanged = run_worker.run_due_source_sync(
        engine,
        [source],
        tenant_id=TENANT,
        kb_id=KB,
        source_interval=30,
        last_sync_at=last_sync,
        now=39.9,
        retry_refs=retries,
    )
    assert unchanged == 10.0
    assert source.sync_calls == 1

    due = run_worker.run_due_source_sync(
        engine,
        [source],
        tenant_id=TENANT,
        kb_id=KB,
        source_interval=30,
        last_sync_at=last_sync,
        now=40.0,
        retry_refs=retries,
    )
    assert due == 40.0
    assert source.sync_calls == 2


@pytest.mark.parametrize("option", ["--interval", "--source-interval"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_worker_rejects_nonpositive_intervals_before_startup(
    monkeypatch, option, value
) -> None:
    engine_calls = []
    monkeypatch.setattr(
        run_worker,
        "create_db_engine",
        lambda: engine_calls.append("engine") or object(),
    )
    monkeypatch.setattr(run_worker, "get_embedder", lambda: object())
    monkeypatch.setattr(
        run_worker,
        "load_connector_config",
        lambda: (None, None, []),
    )
    monkeypatch.setattr(run_worker, "worker_once", lambda *args, **kwargs: 0)

    with pytest.raises(SystemExit) as error:
        run_worker.main(["--once", option, value])
    assert error.value.code == 2
    assert engine_calls == []


@pytest.mark.parametrize(("now", "expected_sleep"), [(101.5, 0.5), (103.0, 0.1)])
def test_idle_sleep_uses_time_until_source_due(
    monkeypatch, now, expected_sleep
) -> None:
    source = FakeSource()
    monkeypatch.setattr(run_worker, "create_db_engine", lambda: object())
    monkeypatch.setattr(run_worker, "get_embedder", lambda: object())
    monkeypatch.setattr(
        run_worker,
        "load_connector_config",
        lambda: (TENANT, KB, [source]),
    )
    monkeypatch.setattr(
        run_worker,
        "run_due_source_sync",
        lambda *args, **kwargs: 100.0,
    )
    monkeypatch.setattr(run_worker, "worker_once", lambda *args, **kwargs: 0)
    monkeypatch.setattr(run_worker.time, "monotonic", lambda: now)
    sleeps = []

    def stop_after_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(run_worker.time, "sleep", stop_after_sleep)

    assert run_worker.main(["--interval", "10", "--source-interval", "2"]) == 0
    assert sleeps == [expected_sleep]


def test_once_runs_due_source_sync_before_worker(
    monkeypatch, tmp_path
) -> None:
    assert hasattr(run_worker, "sync_sources"), "缺少 Source 同步函数"
    monkeypatch.setenv("CONNECTOR_TENANT_ID", TENANT)
    monkeypatch.setenv("CONNECTOR_KB_ID", KB)
    monkeypatch.setenv("LOCAL_DIR_PATH", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    events = []
    monkeypatch.setattr(run_worker, "create_db_engine", lambda: object())
    monkeypatch.setattr(run_worker, "get_embedder", lambda: object())
    monkeypatch.setattr(
        run_worker,
        "sync_sources",
        lambda *args, **kwargs: events.append("sync") or [],
    )
    monkeypatch.setattr(
        run_worker,
        "worker_once",
        lambda *args, **kwargs: events.append("worker") or 0,
    )

    assert run_worker.main(["--once", "--source-interval", "12"]) == 0
    assert events == ["sync", "worker"]


@pytest.mark.parametrize(
    ("argv", "interrupt"),
    [(["--once"], False), ([], True)],
    ids=["once", "keyboard-interrupt"],
)
def test_main_closes_sources_on_exit(monkeypatch, argv, interrupt) -> None:
    source = ClosableSource()
    monkeypatch.setattr(run_worker, "create_db_engine", lambda: object())
    monkeypatch.setattr(run_worker, "get_embedder", lambda: object())
    monkeypatch.setattr(
        run_worker,
        "load_connector_config",
        lambda: (TENANT, KB, [source]),
    )
    monkeypatch.setattr(
        run_worker,
        "run_due_source_sync",
        lambda *args, **kwargs: 100.0,
    )

    def run_job(*args, **kwargs):
        if interrupt:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(run_worker, "worker_once", run_job)

    assert run_worker.main(argv) == 0
    assert source.close_calls == 1
