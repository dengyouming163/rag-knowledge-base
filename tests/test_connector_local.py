"""Task 12：Source 协议与本地目录增量发现。"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from src.connectors.local_dir import LocalDirectorySource


def test_first_sync_discovers_supported_files_with_metadata(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    markdown = nested / "POLICY.MD"
    markdown_payload = b"# policy\n"
    markdown.write_bytes(markdown_payload)
    text = tmp_path / "guide.txt"
    text.write_bytes(b"guide")
    pdf = nested / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    (tmp_path / "ignored.docx").write_bytes(b"ignored")
    (tmp_path / "ignored.JSON").write_bytes(b"{}")
    (tmp_path / "directory.pdf").mkdir()

    source = LocalDirectorySource(tmp_path)
    refs = source.sync()

    assert source.name == "local_dir"
    assert {ref.uri for ref in refs} == {
        markdown.resolve().as_uri(),
        text.resolve().as_uri(),
        pdf.resolve().as_uri(),
    }
    assert [ref.uri for ref in refs] == sorted(ref.uri for ref in refs)

    markdown_ref = next(ref for ref in refs if ref.uri == markdown.resolve().as_uri())
    assert markdown_ref.size == len(markdown_payload)
    assert markdown_ref.hash == hashlib.sha256(markdown_payload).hexdigest()
    assert markdown_ref.mtime == datetime.fromtimestamp(
        markdown.stat().st_mtime, tz=timezone.utc
    )
    assert markdown_ref.mtime.tzinfo is timezone.utc


def test_sync_uses_both_mtime_and_content_hash(tmp_path) -> None:
    path = tmp_path / "policy.txt"
    path.write_bytes(b"one")
    source = LocalDirectorySource(tmp_path)

    first = source.sync()
    assert [ref.uri for ref in first] == [path.resolve().as_uri()]
    assert source.sync() == []

    # 同长度、同 mtime 的内容变化只能靠哈希发现。
    original_stat = path.stat()
    path.write_bytes(b"two")
    os.utime(
        path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    content_changed = source.sync()
    assert len(content_changed) == 1
    assert content_changed[0].mtime == first[0].mtime
    assert content_changed[0].hash != first[0].hash
    assert source.sync() == []

    # 内容不变但 mtime 变化也应按协议视为文档发生更新。
    changed_stat = path.stat()
    os.utime(
        path,
        ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns + 1_000_000_000),
    )
    mtime_changed = source.sync()
    assert len(mtime_changed) == 1
    assert mtime_changed[0].hash == content_changed[0].hash
    assert mtime_changed[0].mtime > content_changed[0].mtime


def test_fetch_returns_original_file_bytes(tmp_path) -> None:
    payload = b"\x00\xffbinary\r\n"
    path = tmp_path / "manual.pdf"
    path.write_bytes(payload)
    source = LocalDirectorySource(tmp_path)

    ref = source.sync()[0]

    assert source.fetch(ref) == payload


def test_list_documents_filters_by_mtime(tmp_path) -> None:
    old_path = tmp_path / "old.md"
    old_path.write_bytes(b"old")
    new_path = tmp_path / "new.txt"
    new_path.write_bytes(b"new")
    old_mtime = datetime(2024, 1, 1, tzinfo=timezone.utc)
    new_mtime = old_mtime + timedelta(days=2)
    os.utime(old_path, (old_mtime.timestamp(), old_mtime.timestamp()))
    os.utime(new_path, (new_mtime.timestamp(), new_mtime.timestamp()))
    source = LocalDirectorySource(tmp_path)

    refs = list(source.list_documents(since=old_mtime + timedelta(days=1)))

    assert [ref.uri for ref in refs] == [new_path.resolve().as_uri()]
    assert refs[0].mtime == new_mtime
