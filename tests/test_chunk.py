"""src/chunk.py 的单元测试：不依赖网络与外部文件。"""

from __future__ import annotations

import hashlib

import pytest

from src.chunk import estimate_tokens, load_markdown_file, split_document


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t\n"])
def test_empty_document_returns_empty_list(text):
    assert split_document(text, doc_id="empty") == []


def test_estimate_tokens_handles_chinese_and_english():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") >= 4
    assert estimate_tokens("a" * 40) < estimate_tokens("好" * 40)


def test_document_without_headings_is_split_by_paragraph():
    paragraphs = [f"第{i}段：{'客服说明' * 10}" for i in range(12)]
    text = "\n\n".join(paragraphs)

    chunks = split_document(
        text, doc_id="plain", target_tokens=200, max_tokens=400, overlap_tokens=0
    )

    assert 3 <= len(chunks) <= 5
    assert all(chunk.section_path == [] for chunk in chunks)
    assert all(chunk.text.strip() for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks).replace("\n", "") != ""


def test_markdown_headings_build_section_path():
    text = """# 售后政策

退款总则说明。

## 退款流程

申请退款需要提供订单号。

### 异常处理

超时未退款时升级处理。

## 换货政策

换货需在签收后七天内申请。
"""

    chunks = split_document(
        text,
        doc_id="policy",
        title="售后政策",
        target_tokens=300,
        max_tokens=600,
        overlap_tokens=0,
    )

    assert [chunk.section_path for chunk in chunks] == [
        ["售后政策"],
        ["售后政策", "退款流程"],
        ["售后政策", "退款流程", "异常处理"],
        ["售后政策", "换货政策"],
    ]
    assert all(chunk.title == "售后政策" for chunk in chunks)


def test_long_paragraph_is_split_and_respects_max_tokens():
    sentence = "用户咨询退款进度时，客服需要先核对订单状态并记录沟通时间，再告知预计处理周期。"
    text = sentence * 20

    chunks = split_document(
        text, doc_id="long", target_tokens=100, max_tokens=200, overlap_tokens=20
    )

    assert len(chunks) >= 2
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= int(200 * 1.1)


def test_overlap_exists_between_adjacent_chunks():
    text = "\n\n".join(["第一段内容", "第二段内容", "第三段内容", "第四段内容", "第五段内容"])

    chunks = split_document(
        text, doc_id="overlap", target_tokens=12, max_tokens=24, overlap_tokens=8
    )

    assert len(chunks) >= 2
    assert chunks[0].text.splitlines()[-1] in chunks[1].text


def test_overlap_not_less_than_target_raises_value_error():
    with pytest.raises(ValueError):
        split_document("正文内容。", doc_id="bad", target_tokens=100, overlap_tokens=100)


def test_table_rows_are_not_cut_in_the_middle():
    text = (
        "以下是报价明细：\n\n"
        "| 型号 | 价格 |\n"
        "| --- | --- |\n"
        "| A 型 | 199 元 |\n"
        "| B 型 | 299 元 |\n\n"
        "请以门店实际价格为准。"
    )
    rows = {"| 型号 | 价格 |", "| --- | --- |", "| A 型 | 199 元 |", "| B 型 | 299 元 |"}

    chunks = split_document(
        text, doc_id="table", target_tokens=8, max_tokens=20, overlap_tokens=0
    )

    seen = set()
    for chunk in chunks:
        for line in chunk.text.splitlines():
            if line.lstrip().startswith("|"):
                assert line in rows, f"表格行被切断：{line!r}"
                seen.add(line)
    assert rows <= seen


def test_overlap_does_not_split_table_rows():
    rows = [f"| 型号{i} | {'参数说明' * 12} |" for i in range(3)]
    text = "\n".join(rows)

    chunks = split_document(
        text, doc_id="table-overlap", target_tokens=70, max_tokens=120, overlap_tokens=20
    )

    assert len(chunks) >= 2
    for chunk in chunks:
        for line in chunk.text.splitlines():
            if line.lstrip().startswith("|"):
                assert line in rows, f"重叠引入了被切断的表格行：{line!r}"


def test_chunk_id_and_content_hash_are_stable():
    text = "# 标题\n\n第一段内容。\n\n第二段内容。"

    first = split_document(text, doc_id="doc", target_tokens=8, max_tokens=32, overlap_tokens=0)
    second = split_document(text, doc_id="doc", target_tokens=8, max_tokens=32, overlap_tokens=0)

    assert [chunk.model_dump() for chunk in first] == [chunk.model_dump() for chunk in second]
    assert [chunk.chunk_id for chunk in first] == [f"doc#{i}" for i in range(1, len(first) + 1)]
    assert len(first) >= 2
    for chunk in first:
        assert chunk.content_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()[:16]
        assert chunk.audience == "public"


def test_load_markdown_file_reads_utf8_file(tmp_path):
    path = tmp_path / "faq.md"
    path.write_text(
        "# 常见问题\n\n## 退款\n\n退款一般需要 3 个工作日到账。\n", encoding="utf-8"
    )

    chunks = load_markdown_file(path)

    assert chunks
    assert chunks[0].doc_id == "faq"
    assert chunks[0].title == "常见问题"
    assert chunks[0].section_path == ["常见问题", "退款"]
    assert "3 个工作日" in chunks[0].text


def test_load_markdown_file_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_markdown_file(tmp_path / "missing.md")
