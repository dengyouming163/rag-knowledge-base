"""办公文档解析单测：夹具用解析库现场生成，不提交二进制文件。"""

from __future__ import annotations

import io

import pytest
from docx import Document
from pptx import Presentation
from pptx.util import Inches

from src.parsers.office import (
    OfficeParseError,
    docx_to_markdown,
    pptx_to_markdown,
)


def _docx_bytes() -> bytes:
    document = Document()
    document.add_heading("售后政策", level=1)
    document.add_paragraph("本政策适用于全部渠道。")
    document.add_heading("退款", level=2)
    document.add_heading("时限", level=3)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "退款"
    table.cell(1, 1).text = "七天无理由"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_headings_paragraphs_and_table() -> None:
    markdown = docx_to_markdown(_docx_bytes())

    assert markdown.startswith("# 售后政策")
    assert "## 退款" in markdown
    assert "### 时限" in markdown
    assert "| 项目 | 说明 |" in markdown
    assert "| --- | --- |" in markdown
    assert "| 退款 | 七天无理由 |" in markdown
    # 段落与表格必须保持文档中的先后顺序
    assert markdown.index("本政策适用于全部渠道。") < markdown.index("## 退款")
    assert markdown.index("## 退款") < markdown.index("| 项目 | 说明 |")


def test_docx_empty_document_returns_empty() -> None:
    buffer = io.BytesIO()
    Document().save(buffer)

    assert docx_to_markdown(buffer.getvalue()) == ""


def test_docx_corrupt_file_raises_readable_error() -> None:
    with pytest.raises(OfficeParseError) as exc_info:
        docx_to_markdown(b"not a docx")

    assert "加密" in str(exc_info.value)
    assert "损坏" in str(exc_info.value)


def _pptx_bytes() -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "季度目标"

    body = slide.placeholders[1].text_frame
    body.text = "营收增长 20%"
    body.add_paragraph().text = "费用下降 5%"

    table = slide.shapes.add_table(
        2, 2, Inches(1), Inches(4), Inches(4), Inches(1)
    ).table
    table.cell(0, 0).text = "指标"
    table.cell(0, 1).text = "目标"
    table.cell(1, 0).text = "营收"
    table.cell(1, 1).text = "20%"

    slide.notes_slide.notes_text_frame.text = "备注：口径为同比。"

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def test_pptx_slide_title_text_table_and_notes() -> None:
    markdown = pptx_to_markdown(_pptx_bytes())

    assert "## 第 1 页 · 季度目标" in markdown
    assert markdown.count("季度目标") == 1  # 标题占位符不能被正文重复输出
    assert "营收增长 20%" in markdown
    assert "费用下降 5%" in markdown
    assert "| 指标 | 目标 |" in markdown
    assert "| 营收 | 20% |" in markdown
    assert "### 备注" in markdown
    assert "备注：口径为同比。" in markdown


def test_pptx_empty_presentation_returns_empty() -> None:
    presentation = Presentation()
    buffer = io.BytesIO()
    presentation.save(buffer)

    assert pptx_to_markdown(buffer.getvalue()) == ""


def test_pptx_corrupt_file_raises_readable_error() -> None:
    with pytest.raises(OfficeParseError) as exc_info:
        pptx_to_markdown(b"not a pptx")

    assert "加密" in str(exc_info.value)
    assert "损坏" in str(exc_info.value)
