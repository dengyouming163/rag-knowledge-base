"""Task 12：Source 协议与本地目录增量发现。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.connectors.base import DocumentRef
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
    (tmp_path / "ignored.doc").write_bytes(b"ignored")
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
        markdown.stat().st_mtime, tz=UTC
    )
    assert markdown_ref.mtime.tzinfo is UTC


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
    old_mtime = datetime(2024, 1, 1, tzinfo=UTC)
    new_mtime = old_mtime + timedelta(days=2)
    os.utime(old_path, (old_mtime.timestamp(), old_mtime.timestamp()))
    os.utime(new_path, (new_mtime.timestamp(), new_mtime.timestamp()))
    source = LocalDirectorySource(tmp_path)

    refs = list(source.list_documents(since=old_mtime + timedelta(days=1)))

    assert [ref.uri for ref in refs] == [new_path.resolve().as_uri()]
    assert refs[0].mtime == new_mtime


def test_sync_retries_all_changes_when_a_later_file_fails(
    tmp_path, monkeypatch
) -> None:
    first_path = tmp_path / "a.txt"
    first_path.write_bytes(b"first")
    failing_path = tmp_path / "z.txt"
    failing_path.write_bytes(b"last")
    source = LocalDirectorySource(tmp_path)
    original_open = Path.open
    failed = False

    def fail_last_file_once(path: Path, mode: str = "r", *args, **kwargs):
        nonlocal failed
        if path == failing_path and mode == "rb" and not failed:
            failed = True
            raise OSError("temporary read failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_last_file_once)

    with pytest.raises(OSError, match="temporary read failure"):
        source.sync()

    retry_refs = source.sync()
    assert [ref.uri for ref in retry_refs] == [
        first_path.resolve().as_uri(),
        failing_path.resolve().as_uri(),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC 路径转换仅在 Windows 验证")
def test_fetch_preserves_windows_unc_authority(tmp_path, monkeypatch) -> None:
    payload = b"unc payload"
    ref = DocumentRef(
        uri="file://server/share/docs/a%20b.txt",
        mtime=datetime.now(UTC),
        size=len(payload),
        hash=hashlib.sha256(payload).hexdigest(),
    )
    read_paths = []

    def fake_read_bytes(path: Path) -> bytes:
        read_paths.append(path)
        return payload

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    source = LocalDirectorySource(tmp_path)

    assert source.fetch(ref) == payload
    assert read_paths == [Path(r"\\server\share\docs\a b.txt")]


def test_sync_rejects_file_changed_during_read(tmp_path, monkeypatch) -> None:
    path = tmp_path / "policy.txt"
    path.write_bytes(b"old")
    source = LocalDirectorySource(tmp_path)
    original_open = Path.open
    mutated = False

    class MutatingReader:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def fileno(self) -> int:
            return self._stream.fileno()

        def read(self, *args) -> bytes:
            nonlocal mutated
            content = self._stream.read(*args)
            if not mutated:
                mutated = True
                path.write_bytes(b"new content")
            return content

    def open_mutating_reader(path_arg: Path, mode: str = "r", *args, **kwargs):
        stream = original_open(path_arg, mode, *args, **kwargs)
        if path_arg == path and mode == "rb" and not mutated:
            return MutatingReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_mutating_reader)

    with pytest.raises(OSError, match="读取期间"):
        source.sync()


def test_fetch_rejects_content_changed_after_sync(tmp_path) -> None:
    path = tmp_path / "policy.txt"
    path.write_bytes(b"before")
    source = LocalDirectorySource(tmp_path)
    ref = source.sync()[0]
    path.write_bytes(b"after!")

    with pytest.raises(OSError, match="快照"):
        source.fetch(ref)


def test_list_documents_does_not_read_files_older_than_since(
    tmp_path, monkeypatch
) -> None:
    old_path = tmp_path / "old.txt"
    old_path.write_bytes(b"old")
    new_path = tmp_path / "new.txt"
    new_path.write_bytes(b"new")
    old_mtime = datetime(2024, 1, 1, tzinfo=UTC)
    new_mtime = old_mtime + timedelta(days=2)
    os.utime(old_path, (old_mtime.timestamp(), old_mtime.timestamp()))
    os.utime(new_path, (new_mtime.timestamp(), new_mtime.timestamp()))
    original_open = Path.open
    old_file_reads = []

    class ReadTracker:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def fileno(self) -> int:
            return self._stream.fileno()

        def read(self, *args) -> bytes:
            old_file_reads.append(True)
            return self._stream.read(*args)

    def track_old_file_reads(path: Path, mode: str = "r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path == old_path and mode == "rb":
            return ReadTracker(stream)
        return stream

    monkeypatch.setattr(Path, "open", track_old_file_reads)
    source = LocalDirectorySource(tmp_path)

    refs = list(source.list_documents(since=old_mtime + timedelta(days=1)))

    assert [ref.uri for ref in refs] == [new_path.resolve().as_uri()]
    assert old_file_reads == []


def test_sync_deduplicates_canonical_uris_before_comparing(
    tmp_path, monkeypatch
) -> None:
    uri = "file:///docs/policy.txt"
    mtime = datetime.now(UTC)
    first = DocumentRef(
        uri=uri,
        mtime=mtime,
        size=5,
        hash=hashlib.sha256(b"first").hexdigest(),
    )
    last = DocumentRef(
        uri=uri,
        mtime=mtime,
        size=4,
        hash=hashlib.sha256(b"last").hexdigest(),
    )
    source = LocalDirectorySource(tmp_path)
    monkeypatch.setattr(
        source,
        "list_documents",
        lambda since: iter([first, last]),
    )

    assert source.sync() == [last]
    assert source.sync() == []
