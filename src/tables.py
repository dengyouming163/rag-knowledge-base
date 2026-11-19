"""把解析结果中的表格转成 Markdown，避免切块检索后丢失列语义。"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_NEWLINES_RE = re.compile(r"[\r\n]+")


def _markdown_cell(cell: str) -> str:
    # Markdown 表格以竖线和换行作为结构符，单元格内必须转义或折叠。
    return _NEWLINES_RE.sub(" ", cell).replace("|", r"\|")


def rows_to_markdown(rows: list[list[str]]) -> str:
    """二维数组 → Markdown 表格；首行当表头，缺列补空单元格。"""
    if not rows:
        return ""

    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""

    def format_row(row: list[str]) -> str:
        cells = [_markdown_cell(cell) for cell in row]
        cells.extend([""] * (width - len(cells)))
        return f"| {' | '.join(cells)} |"

    lines = [format_row(rows[0]), f"| {' | '.join(['---'] * width)} |"]
    lines.extend(format_row(row) for row in rows[1:])
    return "\n".join(lines)


def split_markdown_table(rows: list[list[str]], *, max_rows: int = 20) -> list[str]:
    """大表按数据行切块（表头不计入行数），每块都重复表头。"""
    if max_rows < 1:
        raise ValueError("max_rows 必须大于等于 1")
    if len(rows) < 2:
        return []

    header = rows[0]
    return [
        rows_to_markdown([header, *rows[start : start + max_rows]])
        for start in range(1, len(rows), max_rows)
    ]


class _FirstTableParser(HTMLParser):
    """只读取首个 table；单元格空白折叠可避免 HTML 缩进污染正文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._in_table = False
        self._finished = False
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._finished:
            return
        if tag == "table":
            if not self._in_table:
                self._in_table = True
            return
        if not self._in_table:
            return
        if tag == "tr" and self._row is None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table or self._finished:
            return
        if tag in {"th", "td"} and self._cell_parts is not None and self._row is not None:
            text = " ".join("".join(self._cell_parts).split())
            self._row.append(text)
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_table = False
            self._finished = True


def html_table_to_rows(html: str) -> list[list[str]]:
    """HTML <table> → 二维数组；用标准库 html.parser，不引入 bs4。"""
    parser = _FirstTableParser()
    parser.feed(html)
    parser.close()
    return parser.rows
