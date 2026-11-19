"""表格转 Markdown 的单元测试：不联网，也不依赖数据库。"""

from __future__ import annotations

import pytest

from src.parsers.pdf import PdfBlock, blocks_to_text


def test_rows_to_markdown_builds_header_and_data_rows() -> None:
    from src.tables import rows_to_markdown

    md = rows_to_markdown([["服务类型", "时限"], ["无理由退款", "7 天"], ["换货", "7 天"]])

    assert md.count("| 服务类型 | 时限 |") == 1
    assert "| 无理由退款 | 7 天 |" in md
    assert md == (
        "| 服务类型 | 时限 |\n"
        "| --- | --- |\n"
        "| 无理由退款 | 7 天 |\n"
        "| 换货 | 7 天 |"
    )


def test_rows_to_markdown_pads_missing_cells_and_accepts_empty_rows() -> None:
    from src.tables import rows_to_markdown

    md = rows_to_markdown([["a", "b"], ["1"]])

    assert md.splitlines()[2] == "| 1 |  |"
    assert rows_to_markdown([]) == ""


def test_rows_to_markdown_escapes_pipes_and_flattens_newlines() -> None:
    from src.tables import rows_to_markdown

    md = rows_to_markdown([["列|名", "说明"], ["第一行\n第二行", "x|y"]])

    assert "| 列\\|名 | 说明 |" in md
    assert "| 第一行 第二行 | x\\|y |" in md


def test_split_markdown_table_repeats_header_for_sixty_rows() -> None:
    from src.tables import split_markdown_table

    rows = [["列名", "值"]] + [[f"k{i}", str(i)] for i in range(60)]

    chunks = split_markdown_table(rows, max_rows=20)

    assert len(chunks) == 3
    assert all(chunk.startswith("| 列名 | 值 |\n| --- | --- |\n") for chunk in chunks)
    assert "| k0 | 0 |" in chunks[0]
    assert "| k19 | 19 |" in chunks[0]
    assert "| k20 | 20 |" in chunks[1]
    assert "| k59 | 59 |" in chunks[-1]


def test_split_markdown_table_handles_remainder_and_missing_data() -> None:
    from src.tables import split_markdown_table

    rows = [["列名", "值"]] + [[f"k{i}", str(i)] for i in range(61)]

    chunks = split_markdown_table(rows, max_rows=20)

    assert len(chunks) == 4
    assert chunks[-1].splitlines() == ["| 列名 | 值 |", "| --- | --- |", "| k60 | 60 |"]
    assert split_markdown_table([["列名", "值"]], max_rows=20) == []
    assert split_markdown_table([], max_rows=20) == []


@pytest.mark.parametrize("max_rows", [0, -1])
def test_split_markdown_table_rejects_invalid_max_rows(max_rows: int) -> None:
    from src.tables import split_markdown_table

    with pytest.raises(ValueError):
        split_markdown_table([["列名", "值"], ["k", "1"]], max_rows=max_rows)


def test_html_table_to_rows_reads_attributes_headers_and_cells() -> None:
    from src.tables import html_table_to_rows

    html = (
        '<table border="1"><tr><th>服务类型</th><th>时限</th></tr>'
        "<tr><td>无理由退款</td><td>7 天</td></tr></table>"
    )

    assert html_table_to_rows(html) == [["服务类型", "时限"], ["无理由退款", "7 天"]]


def test_html_table_to_rows_collapses_whitespace_and_uses_first_table() -> None:
    from src.tables import html_table_to_rows

    html = """
    <table><tr><th> 服务   类型 </th></tr><tr><td>无理由\n   退款</td></tr></table>
    <table><tr><td>不应读取</td></tr></table>
    """

    assert html_table_to_rows(html) == [["服务 类型"], ["无理由 退款"]]


def test_html_table_to_rows_returns_empty_without_table_or_rows() -> None:
    from src.tables import html_table_to_rows

    assert html_table_to_rows("服务类型 时限\n无理由退款 7 天") == []
    assert html_table_to_rows("<table><tbody></tbody></table>") == []


def test_blocks_to_text_converts_html_table_to_markdown() -> None:
    blocks = [
        PdfBlock(type="text", text="售后政策", page=1, section="政策"),
        PdfBlock(
            type="table",
            text=(
                "<table><tr><th>服务类型</th><th>时限</th></tr>"
                "<tr><td>无理由退款</td><td>7 天</td></tr></table>"
            ),
            page=1,
        ),
    ]

    text = blocks_to_text(blocks)

    assert "## 政策" in text
    assert "| 服务类型 | 时限 |" in text
    assert "| --- | --- |" in text
    assert "| 无理由退款 | 7 天 |" in text
    assert "<table>" not in text


def test_blocks_to_text_repeats_header_for_each_large_table_chunk() -> None:
    rows = "".join(f"<tr><td>k{i}</td><td>{i}</td></tr>" for i in range(21))
    blocks = [
        PdfBlock(
            type="table",
            text=f"<table><tr><th>列名</th><th>值</th></tr>{rows}</table>",
            page=1,
        )
    ]

    text = blocks_to_text(blocks)

    assert text.count("| 列名 | 值 |") == 2
    assert text.count("| --- | --- |") == 2
    assert "| k0 | 0 |" in text
    assert "| k20 | 20 |" in text


def test_blocks_to_text_keeps_plain_text_table_unchanged() -> None:
    plain_table = "服务类型 时限\n无理由退款 7 天"

    text = blocks_to_text([PdfBlock(type="table", text=plain_table, page=1)])

    assert text == plain_table


def test_html_table_to_rows_flushes_implicit_cell_and_row_boundaries() -> None:
    from src.tables import html_table_to_rows

    html = "<table><tr><td>a<td>b<tr><td>c<td>d</table>"

    assert html_table_to_rows(html) == [["a", "b"], ["c", "d"]]
    # 没有 </table> 时，parser.close() 也必须保留最后一个单元格与行。
    assert html_table_to_rows("<table><tr><td>a<td>b") == [["a", "b"]]


def test_split_markdown_table_pads_all_chunks_to_global_width() -> None:
    from src.tables import split_markdown_table

    chunks = split_markdown_table([["a", "b"], ["1", "2"], ["3", "4", "5"]], max_rows=1)

    assert len(chunks) == 2
    assert [chunk.splitlines()[0] for chunk in chunks] == ["| a | b |  |"] * 2
    assert chunks[0].splitlines()[2] == "| 1 | 2 |  |"
    assert chunks[1].splitlines()[2] == "| 3 | 4 | 5 |"


def test_blocks_to_text_preserves_header_only_html_table() -> None:
    block = PdfBlock(
        type="table",
        text="<table><tr><th>服务类型</th><th>时限</th></tr></table>",
        page=1,
    )

    text = blocks_to_text([block])

    assert text == "| 服务类型 | 时限 |\n| --- | --- |"
