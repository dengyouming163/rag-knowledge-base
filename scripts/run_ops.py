#!/usr/bin/env python3
"""独立 ops-runner：白名单执行评测/压测/安全演练。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.base import create_db_engine
from src.ops_runs import claim_next, mark_failed, mark_succeeded

logger = logging.getLogger(__name__)
MAX_OUTPUT = 8192
TIMEOUTS = {
    "eval_mock": 300,
    "loadtest": 300,
    "drill_parser": 120,
    "drill_llm": 120,
    "drill_queue": 120,
}
DRILL_COMMANDS = {
    "drill_parser": [
        sys.executable, "-m", "pytest",
        "tests/test_ocr_branch.py", "tests/test_pdf_parser.py", "-q",
    ],
    "drill_llm": [sys.executable, "-m", "pytest", "tests/test_llm.py", "-q"],
    "drill_queue": [
        sys.executable, "-m", "pytest", "tests/test_ingest_jobs.py", "-q",
    ],
}


def build_command(kind: str, run: dict) -> tuple[list[str], str | None]:
    if kind == "eval_mock":
        artifact = f"data/ops/eval/{run['id']}.json"
        return [
            sys.executable, "scripts/run_eval.py", "--mock", "--out", artifact,
        ], artifact
    if kind == "loadtest":
        artifact = f"data/ops/loadtest/{run['id']}.json"
        return [
            sys.executable, "scripts/loadtest.py",
            "--concurrency", "20", "--requests", "200",
            "--kb", run["kb_id"], "--tenant", run["tenant_id"],
            "--out", artifact,
        ], artifact
    if kind in DRILL_COMMANDS:
        return list(DRILL_COMMANDS[kind]), None
    raise ValueError(f"unsupported ops kind: {kind}")


def _text(chunk: str | bytes | None) -> str:
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return chunk or ""


def _tail(*chunks: str | bytes | None) -> str:
    return "".join(_text(chunk) for chunk in chunks)[-MAX_OUTPUT:]


def _summary(kind: str, artifact: str | None, returncode: int) -> dict:
    if artifact and (ROOT / artifact).exists():
        try:
            data = json.loads((ROOT / artifact).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 产物读不出来不改变 returncode 语义，只标记后交给上层按成功落状态
            logger.warning("运行产物不可读：artifact=%s error=%s", artifact, exc)
            return {"passed": returncode == 0, "artifact_unreadable": True}
        if not isinstance(data, dict):
            logger.warning("运行产物顶层不是对象：artifact=%s", artifact)
            return {"passed": returncode == 0, "artifact_unreadable": True}
        if kind == "eval_mock":
            return {"metrics": data.get("metrics", {}), "counts": data.get("counts", {})}
        if kind == "loadtest":
            return data.get("summary", data)
    return {"passed": returncode == 0}


def run_once(engine, *, runner=subprocess.run, kind: str | None = None) -> bool:
    run = claim_next(engine, kind=kind)
    if run is None:
        return False

    run_id = run["id"]
    kind = run["kind"]
    started = time.perf_counter()
    logger.info(
        "已领取运行：id=%s kind=%s tenant=%s kb=%s",
        run_id, kind, run["tenant_id"], run["kb_id"],
    )
    try:
        args, artifact = build_command(kind, run)
        completed = runner(
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUTS[kind],
            shell=False,
        )
        tail = _tail(completed.stdout, completed.stderr)
        summary = _summary(kind, artifact, completed.returncode)
        elapsed = time.perf_counter() - started
        if completed.returncode == 0:
            mark_succeeded(
                engine, run_id, exit_code=completed.returncode,
                summary=summary, artifact_path=artifact, output_tail=tail,
            )
            logger.info(
                "运行成功：id=%s kind=%s exit_code=0 elapsed=%.2fs",
                run_id, kind, elapsed,
            )
        else:
            mark_failed(
                engine, run_id, error=f"exit_code={completed.returncode}",
                exit_code=completed.returncode, summary=summary,
                artifact_path=artifact, output_tail=tail,
            )
            logger.warning(
                "运行失败：id=%s kind=%s exit_code=%s elapsed=%.2fs",
                run_id, kind, completed.returncode, elapsed,
            )
    except subprocess.TimeoutExpired as exc:
        tail = _tail(exc.stdout, exc.stderr) or str(exc)[:MAX_OUTPUT]
        mark_failed(
            engine, run_id, error="timeout", exit_code=None,
            output_tail=tail,
        )
        logger.warning("运行超时：id=%s kind=%s", run_id, kind)
    except Exception as exc:  # noqa: BLE001 - 执行异常也必须落 failed，不能卡在 running
        reason = str(exc)[:2000] or type(exc).__name__
        mark_failed(
            engine, run_id, error=reason, exit_code=None,
            output_tail="",
        )
        logger.warning("运行异常：id=%s kind=%s error=%s", run_id, kind, reason)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="独立 ops-runner：白名单执行评测/压测/安全演练"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("OPS_RUNNER_INTERVAL", "5")),
        help="空队列时的轮询间隔（秒），也可用 OPS_RUNNER_INTERVAL 指定",
    )
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")

    engine = create_db_engine()
    try:
        while True:
            try:
                if not run_once(engine):
                    time.sleep(args.interval)
            except Exception:  # noqa: BLE001 - DB 瞬时错误不能让常驻进程退出
                logger.exception("ops-runner 循环异常，%ss 后重试", args.interval)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("ops-runner 收到中断信号，已停止。")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
