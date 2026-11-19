#!/usr/bin/env python3
"""持续运行入库 worker；--once 可用于冒烟验证或一次性处理。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.deps import get_embedder
from src.db.base import create_db_engine
from src.worker import worker_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后台文档入库 worker")
    parser.add_argument(
        "--interval", type=float, default=5.0, help="空队列时的轮询间隔（秒）"
    )
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = parser.parse_args(argv)

    engine = create_db_engine()
    embed_client = get_embedder()
    try:
        while True:
            processed = worker_once(engine, embed_client=embed_client)
            if args.once:
                return 0
            if processed == 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("worker 收到中断信号，已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
