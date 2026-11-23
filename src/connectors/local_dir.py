"""本地目录来源：递归发现白名单文档，并在实例内按 mtime 与内容哈希增量同步。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .base import DocumentRef

_SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}


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
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                mtime = datetime.fromtimestamp(before.st_mtime, tz=timezone.utc)
                if since is not None and mtime <= since:
                    continue
                content = stream.read()
                after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError(f"文件在读取期间发生变化：{path}")
            yield DocumentRef(
                uri=path.resolve().as_uri(),
                mtime=mtime,
                size=len(content),
                hash=hashlib.sha256(content).hexdigest(),
            )

    def fetch(self, ref: DocumentRef) -> bytes:
        parts = urlsplit(ref.uri)
        url_path = f"//{parts.netloc}{parts.path}" if parts.netloc else parts.path
        content = Path(url2pathname(url_path)).read_bytes()
        if len(content) != ref.size or hashlib.sha256(content).hexdigest() != ref.hash:
            raise OSError(f"文档内容与快照不一致：{ref.uri}")
        return content

    def sync(self) -> list[DocumentRef]:
        refs = list(
            {ref.uri: ref for ref in self.list_documents(since=None)}.values()
        )
        changed = [
            ref
            for ref in refs
            if self._seen.get(ref.uri) != (ref.mtime, ref.hash)
        ]
        self._seen = {ref.uri: (ref.mtime, ref.hash) for ref in refs}
        return changed
