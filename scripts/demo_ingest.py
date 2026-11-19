#!/usr/bin/env python3
"""P1 端到端冒烟：上传 PDF → 异步入库 → 混合检索。

前置条件：数据库已迁移，parser 已启动，并重建启动 api 与 worker：
    docker compose -f deploy/docker-compose.yml --profile parser up -d --build api worker parser

运行（可用 API_BASE_URL 覆盖默认的 http://127.0.0.1:8000）：
    py -3.13 scripts/demo_ingest.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.base import create_db_engine

TENANT = "t_smoke"
KB = "kb_smoke"
FIXTURE = ROOT / "eval" / "fixtures" / "pdf" / "policy_table.pdf"
POLL_INTERVAL_SECONDS = 2
JOB_TIMEOUT_SECONDS = 90
STARTUP_HINT = (
    "请先启动服务：docker compose -f deploy/docker-compose.yml "
    "--profile parser up -d --build api worker parser"
)


def ensure_smoke_kb() -> None:
    """用独立租户幂等准备冒烟知识库，不清理或修改其他演示数据。"""
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tenants (id, name) VALUES (:tenant, 'ingest smoke') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"tenant": TENANT},
            )
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases "
                    "(id, tenant_id, name, description, status) "
                    "VALUES (:kb, :tenant, 'ingest smoke', 'P1 e2e smoke', 'active') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"kb": KB, "tenant": TENANT},
            )
    finally:
        engine.dispose()


def wait_for_job(client: httpx.Client, job_id: int) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while True:
        response = client.get(f"/v1/jobs/{job_id}")
        if response.status_code != 200:
            raise RuntimeError(
                f"查询任务失败：HTTP {response.status_code} {response.text[:300]}"
            )
        job = response.json()
        status = job.get("status")
        progress = job.get("progress")
        print(f"任务状态：status={status} progress={progress}")

        if status == "succeeded":
            return
        if status == "failed":
            raise RuntimeError(f"入库任务失败：{job.get('error') or '未提供错误原因'}")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"等待入库超时（{JOB_TIMEOUT_SECONDS}s）："
                f"status={status} progress={progress} error={job.get('error') or ''}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> int:
    api_base_url = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {"X-Tenant-Id": TENANT}
    print(f"== P1 入库冒烟（{api_base_url}，tenant={TENANT}，kb={KB}）==")

    try:
        with httpx.Client(
            base_url=api_base_url, headers=headers, timeout=10.0
        ) as client:
            try:
                live = client.get("/live")
            except httpx.HTTPError as exc:
                print(f"API 不可达：{exc}", file=sys.stderr)
                print(STARTUP_HINT, file=sys.stderr)
                return 1
            if live.status_code != 200:
                print(
                    f"API 预检失败：HTTP {live.status_code} {live.text[:300]}",
                    file=sys.stderr,
                )
                print(STARTUP_HINT, file=sys.stderr)
                return 1
            print("API 预检通过。")

            ensure_smoke_kb()
            print(f"已确认租户与知识库存在：{TENANT}/{KB}")

            if not FIXTURE.is_file():
                raise RuntimeError(f"测试文件不存在：{FIXTURE}")
            upload = client.post(
                f"/v1/kbs/{KB}/documents",
                files={
                    "file": (FIXTURE.name, FIXTURE.read_bytes(), "application/pdf")
                },
            )
            if upload.status_code != 202:
                raise RuntimeError(
                    f"上传失败：HTTP {upload.status_code} {upload.text[:300]}"
                )
            uploaded = upload.json()
            job_id = uploaded.get("job_id")
            doc_id = uploaded.get("doc_id")
            if not job_id or not doc_id:
                raise RuntimeError(f"上传响应缺少 job_id/doc_id：{uploaded}")
            print(
                f"上传成功：job_id={job_id} doc_id={doc_id} "
                f"duplicated={uploaded.get('duplicated')}"
            )

            wait_for_job(client, job_id)

            search = client.post(
                f"/v1/kbs/{KB}/search",
                json={
                    "query": "无理由退款多久",
                    "mode": "hybrid",
                    "top_k": 5,
                    "audience": "public",
                },
            )
            if search.status_code != 200:
                raise RuntimeError(
                    f"检索失败：HTTP {search.status_code} {search.text[:300]}"
                )
            hits = search.json().get("hits") or []
            if not hits:
                raise RuntimeError("检索断言失败：hits 为空")

            print(f"检索命中 {len(hits)} 条：")
            for hit in hits:
                snippet = " ".join((hit.get("text") or "").split())[:180]
                print(f"  - {hit.get('chunk_id')}: {snippet}")
            matched = [
                hit
                for hit in hits
                if "无理由退款" in (hit.get("text") or "")
                and "7 天" in (hit.get("text") or "")
            ]
            if not matched:
                raise RuntimeError(
                    "检索断言失败：没有 hit 的文本同时包含‘无理由退款’和‘7 天’"
                )
    except Exception as exc:  # noqa: BLE001 - CLI 需要把所有失败统一映射为退出码 1
        print(f"FAIL：{exc}", file=sys.stderr)
        return 1

    print("PASS upload→ingest→search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
