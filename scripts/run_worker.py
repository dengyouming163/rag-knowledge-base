#!/usr/bin/env python3
"""持续运行入库 worker；定时同步可选 Source，--once 跑一轮到期同步和入库。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.deps import get_embedder
from src.connectors.base import DocumentRef, Source
from src.connectors.feishu import FeishuSource
from src.connectors.local_dir import LocalDirectorySource
from src.db.base import create_db_engine
from src.ingest_events import record_worker_heartbeat
from src.ingest_jobs import receive_source_document
from src.worker import worker_once

logger = logging.getLogger(__name__)
DEFAULT_SOURCE_INTERVAL = 300.0
MIN_SOURCE_SLEEP = 0.1
RetryRefs = dict[str, dict[str, DocumentRef]]


def load_connector_config() -> tuple[str | None, str | None, list[Source]]:
    """从环境变量注册连接器，并返回共同目标租户、知识库和来源列表。

    CONNECTOR_TENANT_ID / CONNECTOR_KB_ID 是所有来源的共同目标；LOCAL_DIR_PATH
    启用本地目录，FEISHU_APP_ID / FEISHU_APP_SECRET 启用飞书。配置不完整时
    对应来源不注册，不影响普通 upload 任务处理。
    """
    tenant_id = os.environ.get("CONNECTOR_TENANT_ID", "").strip() or None
    kb_id = os.environ.get("CONNECTOR_KB_ID", "").strip() or None
    missing_target = [
        key
        for key, value in (
            ("CONNECTOR_TENANT_ID", tenant_id),
            ("CONNECTOR_KB_ID", kb_id),
        )
        if not value
    ]
    if missing_target:
        logger.info("连接器未注册：缺少共同目标环境变量 %s", ", ".join(missing_target))
        return None, None, []

    sources: list[Source] = []
    local_dir = os.environ.get("LOCAL_DIR_PATH", "").strip()
    if local_dir:
        sources.append(LocalDirectorySource(local_dir))
    else:
        logger.info("local_dir 连接器未注册：缺少环境变量 LOCAL_DIR_PATH")

    feishu = FeishuSource.from_env()
    if feishu is not None:
        sources.append(feishu)
    return tenant_id, kb_id, sources


def sync_sources(
    engine,
    sources: list[Source],
    *,
    tenant_id: str,
    kb_id: str,
    upload_root: str | Path | None = None,
    retry_refs: RetryRefs | None = None,
) -> list[dict]:
    """同步所有来源；单个来源或 ref 失败只记录日志，并保留失败 ref 下轮重试。"""
    retries = retry_refs if retry_refs is not None else {}
    results: list[dict] = []
    for source in sources:
        try:
            refs = source.sync()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Source 同步失败：source=%s error=%s: %s",
                source.name,
                type(exc).__name__,
                exc,
            )
            continue

        pending = dict(retries.get(source.name, {}))
        pending.update({ref.uri: ref for ref in refs})
        retries.pop(source.name, None)
        for uri, ref in pending.items():
            try:
                content = source.fetch(ref)
                result = receive_source_document(
                    engine,
                    tenant_id,
                    kb_id,
                    source_name=source.name,
                    uri=uri,
                    content=content,
                    upload_root=upload_root,
                )
            except Exception as exc:  # noqa: BLE001
                retries.setdefault(source.name, {})[uri] = ref
                logger.error(
                    "Source 文档接收失败：source=%s ref=%s error=%s: %s",
                    source.name,
                    uri,
                    type(exc).__name__,
                    exc,
                )
                continue
            results.append(result)
            logger.info(
                "Source 文档已接收：source=%s ref=%s doc_id=%s job_id=%s duplicated=%s",
                source.name,
                uri,
                result["doc_id"],
                result["job_id"],
                result["duplicated"],
            )
    return results


def run_due_source_sync(
    engine,
    sources: list[Source],
    *,
    tenant_id: str | None,
    kb_id: str | None,
    source_interval: float,
    last_sync_at: float | None,
    now: float | None = None,
    upload_root: str | Path | None = None,
    retry_refs: RetryRefs | None = None,
) -> float:
    """首次或到达 source_interval 时同步；返回本轮采用的最近同步时间。"""
    current = time.monotonic() if now is None else now
    if last_sync_at is not None and current - last_sync_at < source_interval:
        return last_sync_at
    if sources and tenant_id and kb_id:
        sync_sources(
            engine,
            sources,
            tenant_id=tenant_id,
            kb_id=kb_id,
            upload_root=upload_root,
            retry_refs=retry_refs,
        )
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后台文档入库 worker")
    parser.add_argument(
        "--interval", type=float, default=5.0, help="空队列时的轮询间隔（秒）"
    )
    parser.add_argument(
        "--source-interval",
        type=float,
        default=DEFAULT_SOURCE_INTERVAL,
        help="Source 同步间隔（秒），与空队列轮询间隔独立",
    )
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    if args.source_interval <= 0:
        parser.error("--source-interval 必须大于 0")

    engine = create_db_engine()
    embed_client = get_embedder()
    tenant_id, kb_id, sources = load_connector_config()
    retry_refs: RetryRefs = {}
    last_source_sync: float | None = None
    worker_id = os.environ.get("WORKER_ID") or f"worker-{os.getpid()}"
    try:
        while True:
            record_worker_heartbeat(engine, worker_id=worker_id)
            last_source_sync = run_due_source_sync(
                engine,
                sources,
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_interval=args.source_interval,
                last_sync_at=last_source_sync,
                retry_refs=retry_refs,
            )
            processed = worker_once(
                engine, embed_client=embed_client, worker_id=worker_id
            )
            if args.once:
                return 0
            if processed == 0:
                sleep_seconds = args.interval
                if sources and last_source_sync is not None:
                    source_remaining = args.source_interval - (
                        time.monotonic() - last_source_sync
                    )
                    sleep_seconds = min(
                        sleep_seconds, max(MIN_SOURCE_SLEEP, source_remaining)
                    )
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("worker 收到中断信号，已停止。")
        return 0
    finally:
        for source in sources:
            close = getattr(source, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(main())
