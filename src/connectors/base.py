"""连接器共用的文档引用与来源协议。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class DocumentRef:
    """来源文档的一次内容快照；mtime 必须带时区，hash 为内容 SHA-256。"""

    uri: str
    mtime: datetime
    size: int
    hash: str


class Source(Protocol):
    """供本地目录与其他远程来源共用的精简协议。"""

    name: str

    def list_documents(self, since: datetime | None) -> Iterable[DocumentRef]: ...

    def fetch(self, ref: DocumentRef) -> bytes: ...

    def sync(self) -> list[DocumentRef]: ...
