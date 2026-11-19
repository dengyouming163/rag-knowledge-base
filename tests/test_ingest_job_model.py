"""ingest_jobs 进度字段：不连库，只校验模型定义。"""

from __future__ import annotations

from src.db.models import IngestJob


def test_ingest_job_has_progress_fields() -> None:
    columns = set(IngestJob.__table__.c.keys())
    assert {"source", "attempts", "progress", "finished_at"} <= columns
    assert IngestJob.__table__.c.attempts.default.arg == 0
    assert IngestJob.__table__.c.finished_at.nullable is True
    assert "upload" in str(IngestJob.__table__.c.source.server_default.arg)
