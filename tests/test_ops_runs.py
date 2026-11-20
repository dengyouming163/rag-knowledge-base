"""ops_runs 状态机：同 kind 活跃互斥、独占领取、成功/失败落状态、租户隔离。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine
from src.ops_runs import (
    AlreadyRunning,
    claim_next,
    enqueue,
    get_run,
    list_runs,
    mark_failed,
    mark_succeeded,
)

TENANT = "t_ops_runs"
KB = "kb_ops_runs"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过运行记录测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops runs')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'runs kb', '', 'active')"
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
    # 每个用例开始前清掉运行/审计残留，避免 claim_next 领到别的用例的 queued 记录
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_audit WHERE tenant_id = :t"), {"t": TENANT}
        )
    yield


def test_enqueue_rejects_same_active_kind(engine) -> None:
    run_id = enqueue(
        engine, TENANT, KB, "eval_mock", requested_by="u_admin", params={}
    )
    with pytest.raises(AlreadyRunning):
        enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin", params={})
    assert get_run(engine, TENANT, run_id)["status"] == "queued"


def test_claim_is_exclusive_and_finishes(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")
    claimed = claim_next(engine, kind="eval_mock")
    assert claimed["id"] == run_id
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["started_at"] is not None
    assert claim_next(engine, kind="eval_mock") is None
    mark_succeeded(
        engine, run_id, exit_code=0,
        summary={"metrics": {"recall_at_5": 1.0}},
        artifact_path="data/ops/eval/1.json", output_tail="ok",
    )
    assert get_run(engine, TENANT, run_id)["status"] == "succeeded"
    # 终态不再占用活跃唯一索引，同 kind 可再次入队
    assert enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin") > run_id


def test_mark_succeeded_ignores_non_running(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")

    mark_succeeded(
        engine, run_id, exit_code=0, summary={}, artifact_path=None,
        output_tail="",
    )

    assert get_run(engine, TENANT, run_id)["status"] == "queued"


def test_failed_run_records_error(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "loadtest", requested_by="u_admin")
    claim_next(engine, kind="loadtest")
    mark_failed(engine, run_id, error="timeout", exit_code=None)
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "timeout"


def test_list_runs_is_tenant_scoped(engine) -> None:
    enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")
    assert list_runs(engine, "other", KB) == []
