from __future__ import annotations

import pytest

from app import deps


@pytest.fixture(autouse=True)
def isolate_trace_files(tmp_path, monkeypatch):
    """所有测试的 trace 都写到临时目录，避免污染仓库 data/。"""
    monkeypatch.setenv("TRACE_ROOT", str(tmp_path / "traces"))
    get_trace_store = getattr(deps, "get_trace_store", None)
    if get_trace_store is not None:
        get_trace_store.cache_clear()
    yield
    if get_trace_store is not None:
        get_trace_store.cache_clear()
