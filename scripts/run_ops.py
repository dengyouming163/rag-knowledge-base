#!/usr/bin/env python3
"""独立 ops-runner：白名单执行评测/压测/安全演练。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from src.db.base import create_db_engine
from src.ops_runs import claim_next, mark_failed, mark_succeeded

ROOT = Path(__file__).resolve().parents[1]
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


def _summary(kind: str, artifact: str | None, returncode: int) -> dict:
    if artifact and (ROOT / artifact).exists():
        data = json.loads((ROOT / artifact).read_text(encoding="utf-8"))
        if kind == "eval_mock":
            return {"metrics": data.get("metrics", {}), "counts": data.get("counts", {})}
        if kind == "loadtest":
            return data.get("summary", data)
    return {"passed": returncode == 0}


def run_once(engine, *, runner=subprocess.run, kind: str | None = None) -> bool:
    run = claim_next(engine, kind=kind)
    if run is None:
        return False

    kind = run["kind"]
    args, artifact = build_command(kind, run)
    try:
        completed = runner(
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=TIMEOUTS[kind],
            shell=False,
        )
        tail = (completed.stdout or "")[-MAX_OUTPUT:]
        summary = _summary(kind, artifact, completed.returncode)
        if completed.returncode == 0:
            mark_succeeded(
                engine, run["id"], exit_code=completed.returncode,
                summary=summary, artifact_path=artifact, output_tail=tail,
            )
        else:
            mark_failed(
                engine, run["id"], error=f"exit_code={completed.returncode}",
                exit_code=completed.returncode, summary=summary,
                artifact_path=artifact, output_tail=tail,
            )
    except subprocess.TimeoutExpired as exc:
        mark_failed(
            engine, run["id"], error="timeout", exit_code=None,
            output_tail=str(exc)[:MAX_OUTPUT],
        )
    except Exception as exc:  # noqa: BLE001 - 执行异常也必须落 failed，不能卡在 running
        mark_failed(
            engine, run["id"], error=str(exc)[:2000], exit_code=None,
            output_tail="",
        )
    return True


def main() -> None:
    engine = create_db_engine()
    interval = float(os.environ.get("OPS_RUNNER_INTERVAL", "5"))
    while True:
        ran = run_once(engine)
        if not ran:
            time.sleep(interval)


if __name__ == "__main__":
    main()
