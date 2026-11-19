"""轻量请求 trace：内存环形缓冲 + 按 UTC 日期追加分阶段 JSONL。"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


def new_trace_id() -> str:
    return uuid4().hex[:12]


class TraceStore:
    """记录各请求阶段；写盘失败时保留内存记录，不影响业务请求。"""

    def __init__(self, root: str | Path = "data/traces") -> None:
        self._root = Path(root)
        self._records: deque[dict] = deque(maxlen=1000)
        self._memory_lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._seq = 0

    def record(
        self,
        *,
        trace_id: str,
        tenant: str,
        kb: str,
        stage: str,
        ms: float,
        hits: int,
        refused: bool,
        mode: str,
        query: str,
    ) -> dict:
        now = datetime.now(timezone.utc)
        item = {
            "trace_id": trace_id,
            "tenant": tenant,
            "kb": kb,
            "stage": stage,
            "ms": round(max(0.0, float(ms)), 3),
            "hits": hits,
            "refused": refused,
            "mode": mode,
            "query": query,
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        with self._memory_lock:
            item["seq"] = self._seq
            self._seq += 1
            self._records.append(item)
        try:
            with self._file_lock:
                self._root.mkdir(parents=True, exist_ok=True)
                path = self._root / f"{now.date().isoformat()}.jsonl"
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            logger.exception("trace 写盘失败，已保留内存记录：trace_id=%s", trace_id)
        return dict(item)

    def find(self, trace_id: str, tenant: str) -> list[dict]:
        """先复制内存，再读当天文件补齐已淘汰阶段，去重后按时间和序号排序。"""
        with self._memory_lock:
            memory = [dict(item) for item in self._records if item["trace_id"] == trace_id]
        with self._file_lock:
            persisted = self._read_today(trace_id)

        merged: list[dict] = []
        seen: set[str] = set()
        for item in [*persisted, *memory]:
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                merged.append(item)
        merged.sort(key=lambda item: (item["ts"], item.get("seq", -1)))
        return [item for item in merged if item.get("tenant") == tenant]

    def _read_today(self, trace_id: str) -> list[dict]:
        date = datetime.now(timezone.utc).date().isoformat()
        path = self._root / f"{date}.jsonl"
        records: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("trace_id") == trace_id:
                        records.append(item)
        except FileNotFoundError:
            return []
        except OSError:
            logger.exception("trace 文件读取失败：path=%s", path)
            return []
        return records
