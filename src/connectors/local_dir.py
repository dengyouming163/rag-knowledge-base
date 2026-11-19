"""本地目录来源：递归发现白名单文档，并在实例内按 mtime 与内容哈希增量同步。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .base import DocumentRef

_SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt"}


class LocalDirectorySource:
    """同步状态仅保留在当前实例中，不承诺跨进程重启后的增量恢复。"""

    def __init__(self, root: str | Path, *, name: str = "local_dir") -> None:
        self.root = Path(root)
        self.name = name
        self._seen: dict[str, tuple[datetime, str]] = {}

    def list_documents(self, since: datetime | None) -> Iterable[DocumentRef]:
        paths = sorted(
            (
                path
                for path in self.root.rglob("*")
                if path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES
            ),
            key=lambda path: path.resolve().as_uri(),
        )
        for path in paths:
            content = path.read_bytes()
            stat = path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if since is not None and mtime <= since:
                continue
            yield DocumentRef(
                uri=path.resolve().as_uri(),
                mtime=mtime,
                size=len(content),
                hash=hashlib.sha256(content).hexdigest(),
            )

    def fetch(self, ref: DocumentRef) -> bytes:
        return Path(url2pathname(urlsplit(ref.uri).path)).read_bytes()

    def sync(self) -> list[DocumentRef]:
        changed = []
        for ref in self.list_documents(since=None):
            previous = self._seen.get(ref.uri)
            current = (ref.mtime, ref.hash)
            if previous != current:
                changed.append(ref)
            self._seen[ref.uri] = current
        return changed
