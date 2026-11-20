"""ops-runner：kind 白名单、成功落状态与产物摘要、超时/异常落 failed。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from scripts.run_ops import build_command, run_once
from src.db.base import create_db_engine
from src.ops_runs import enqueue, get_run

TENANT = "t_ops_runner"
KB = "kb_ops_runner"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过 ops runner 测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops runner')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'runner kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_audit WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture(autouse=True)
def clean_ops(engine):
    # claim_next 全局领取 queued 记录，逐例清理本租户残留，避免串味
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_audit WHERE tenant_id = :t"), {"t": TENANT}
        )
    yield


@pytest.fixture()
def artifacts():
    """记录用例写出的产物路径，结束后删除，避免污染仓库 data/。"""
    written: list[str] = []
    yield written
    for path in written:
        Path(path).unlink(missing_ok=True)


def test_build_command_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        build_command("rm -rf /", {"id": 1, "kb_id": "kb", "tenant_id": "t"})


def test_build_command_allowlists_all_kinds() -> None:
    run = {"id": 7, "kb_id": "kb", "tenant_id": "t"}
    for kind in ("eval_mock", "loadtest", "drill_parser", "drill_llm", "drill_queue"):
        args, _artifact = build_command(kind, run)
        assert args and all(isinstance(arg, str) for arg in args)


def test_run_once_marks_eval_succeeded(engine, artifacts) -> None:
    run_id = enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")

    def fake_runner(args, **kwargs):
        out = Path(args[args.index("--out") + 1])
        artifacts.append(str(out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"metrics": {"recall_at_k": 1.0}, "counts": {"cases": 50}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, "ok", "")

    assert run_once(engine, runner=fake_runner, kind="eval_mock") is True
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "succeeded"
    assert run["summary"]["metrics"]["recall_at_k"] == 1.0
    assert run["artifact_path"].startswith("data/ops/eval/")


def test_runner_timeout_marks_failed(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "loadtest", requested_by="u_admin")

    def timeout_runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    assert run_once(engine, runner=timeout_runner, kind="loadtest") is True
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "timeout"


def test_runner_exception_marks_failed(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "drill_llm", requested_by="u_admin")

    def boom_runner(args, **kwargs):
        raise RuntimeError("spawn failed")

    assert run_once(engine, runner=boom_runner, kind="drill_llm") is True
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "spawn failed"


def test_run_once_returns_false_when_queue_empty(engine) -> None:
    assert run_once(engine, kind="drill_parser") is False
