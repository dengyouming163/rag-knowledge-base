"""轻量请求 trace：内存环形缓冲 + 按 UTC 日期追加分阶段 JSONL。"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
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
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        model: str | None = None,
        llm_ms: float | None = None,
        rerank_degraded: bool | None = None,
        citation_ok: bool | None = None,
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
        # 可选指标只在有值时写入，兼容旧记录不含这些字段的读取
        optional = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": model,
            "llm_ms": round(float(llm_ms), 3) if llm_ms is not None else None,
            "rerank_degraded": rerank_degraded,
            "citation_ok": citation_ok,
        }
        item.update({key: value for key, value in optional.items() if value is not None})
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
        """合并内存与全部保留日期文件，去重后按时间和序号排序。"""
        with self._memory_lock:
            memory = [dict(item) for item in self._records if item["trace_id"] == trace_id]
        with self._file_lock:
            persisted = self._read_all(trace_id)

        merged = self._merge_records(persisted, memory)
        return [item for item in merged if item.get("tenant") == tenant]

    def list_records(self, tenant: str, kb: str) -> list[dict]:
        """合并全部保留的日期文件和内存记录，供租户/知识库报表使用。"""
        with self._memory_lock:
            memory = [dict(item) for item in self._records]
        with self._file_lock:
            persisted = self._read_all()

        merged = self._merge_records(persisted, memory)
        return [
            item
            for item in merged
            if item.get("tenant") == tenant and item.get("kb") == kb
        ]

    def list_recent(
        self, tenant: str, kb: str, *, limit: int = 50, days: int = 7
    ) -> list[dict]:
        """按时间倒序返回最近的记录，只保留最近 `days` 天的数据。"""
        records = self.list_records(tenant, kb)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        def parsed_ts(item: dict) -> datetime:
            return datetime.fromisoformat(item["ts"].replace("Z", "+00:00"))

        fresh = [item for item in records if parsed_ts(item) >= cutoff]
        return list(reversed(fresh[-limit:]))

    @staticmethod
    def _merge_records(persisted: list[dict], memory: list[dict]) -> list[dict]:
        merged: list[dict] = []
        seen: set[str] = set()
        for item in [*persisted, *memory]:
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                merged.append(item)
        merged.sort(key=lambda item: (item["ts"], item.get("seq", -1)))
        return merged

    def _read_all(self, trace_id: str | None = None) -> list[dict]:
        records: list[dict] = []
        for path in sorted(self._root.glob("????-??-??.jsonl")):
            records.extend(self._read_file(path, trace_id))
        return records

    def _read_file(self, path: Path, trace_id: str | None = None) -> list[dict]:
        records: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and (
                        trace_id is None or item.get("trace_id") == trace_id
                    ):
                        records.append(item)
        except FileNotFoundError:
            return []
        except OSError:
            logger.exception("trace 文件读取失败：path=%s", path)
            return []
        return records
