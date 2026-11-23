"""重嵌脚本：只动目标版本、dry-run 不写、二次运行幂等。"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reembed_chunks.py"
T, KB = "t_reembed", "kb_reembed"
VEC = "[" + ",".join(["0"] * 1024) + "]"


def _module():
    spec = importlib.util.spec_from_file_location("reembed_chunks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEmbed:
    model = "bge-m3-test"

    def embed_texts(self, texts):
        return [[0.001] * 1024 for _ in texts]


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    _cleanup(db_engine)
    with db_engine.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'reembed')"), {"t": T})
        c.execute(
            text(
                "INSERT INTO knowledge_bases (id,tenant_id,name,description,status) "
                "VALUES (:kb,:t,:kb,'','active')"
            ),
            {"kb": KB, "t": T},
        )
    for cid, ver in (("re-1", "mock-hash"), ("re-2", "unknown"), ("re-3", "bulk-random")):
        _seed(db_engine, cid, ver)
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": T})
        for table in ("chunks", "documents", "knowledge_bases"):
            c.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": T})
        c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": T})


def _seed(engine, cid, version):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": T})
        c.execute(
            text(
                "INSERT INTO documents (id,tenant_id,kb_id,title,source_url,content_hash,status,meta) "
                "VALUES (:d,:t,:kb,:d,'',:d,'ready','{}'::jsonb) ON CONFLICT (id) DO NOTHING"
            ),
            {"d": f"doc-{cid}", "t": T, "kb": KB},
        )
        c.execute(
            text(
                "INSERT INTO chunks (chunk_id,tenant_id,kb_id,doc_id,content_hash,text,token_count,"
                "embedding,embed_model_version,meta) VALUES (:id,:t,:kb,:d,:id,:txt,1,"
                "CAST(:v AS vector),:m,'{}'::jsonb)"
            ),
            {
                "id": cid,
                "t": T,
                "kb": KB,
                "d": f"doc-{cid}",
                "txt": f"t-{cid}",
                "v": VEC,
                "m": version,
            },
        )


def _versions(engine):
    with engine.connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": T})
        return {
            row["chunk_id"]: row["embed_model_version"]
            for row in c.execute(
                text("SELECT chunk_id, embed_model_version FROM chunks WHERE tenant_id = :t"),
                {"t": T},
            ).mappings()
        }


def test_reembed_only_mock_hash_and_idempotent(engine):
    module = _module()
    first = module.reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="mock-hash")
    assert first["updated"] == 2
    assert _versions(engine)["re-3"] == "bulk-random"
    again = module.reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="all")
    assert again["updated"] == 1  # 只剩 re-3
    assert module.reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="all")["updated"] == 0


def test_dry_run_does_not_write(engine):
    result = _module().reembed(
        engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="mock-hash", dry_run=True
    )
    assert result["dry_run"] is True and result["updated"] == 0
    assert result["scanned"] == 2
    assert _versions(engine)["re-1"] == "mock-hash"
