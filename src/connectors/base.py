"""连接器共用的文档引用与来源协议。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class DocumentRef:
    """来源文档的一次内容快照。

    mtime 必须带时区；hash 是来源版本标识/摘要：local_dir 使用内容 SHA-256，
    feishu 使用文档 revision_id。
    """

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
