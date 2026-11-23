#!/usr/bin/env python3
"""生成 PDF 解析用的测试样本（3 种版式）。

为什么用脚本而不是直接提交二进制：样本要能复现、能改内容，
否则以后没人知道那几份 PDF 是怎么来的。

用法：
    py -3.13 scripts/make_pdf_fixtures.py
输出：
    eval/fixtures/pdf/policy_single.pdf   单栏正文
    eval/fixtures/pdf/policy_table.pdf    带表格
    eval/fixtures/pdf/policy_two_col.pdf  双栏排版
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "fixtures" / "pdf"

FONT_NAME = "SimHei"
FONT_PATH = Path(r"C:\Windows\Fonts\simhei.ttf")

BODY = [
    "本文说明售后政策中的退款与换货规则，适用于全渠道订单。",
    "支持七天无理由退款。退款审核通过后按原支付路径退回，通常 1-3 个工作日到账。",
    "收到货七天内可申请换货。换货需要商品完好、配件齐全，包装不得破损。",
    "以下情况不支持无理由退款：定制类商品、已激活的软件、拆封的耗材。",
]


def _register_font() -> None:
    if not FONT_PATH.exists():
        raise SystemExit(f"缺少中文字体：{FONT_PATH}")
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("body", fontName=FONT_NAME, fontSize=11, leading=18)
    title = ParagraphStyle("title", parent=base, fontSize=18, leading=24, spaceAfter=12)
    heading = ParagraphStyle("heading", parent=base, fontSize=14, leading=20, spaceBefore=10, spaceAfter=6)
    return {"body": base, "title": title, "heading": heading}


def build_single(styles: dict[str, ParagraphStyle]) -> None:
    doc = SimpleDocTemplate(str(OUT / "policy_single.pdf"), pagesize=A4)
    story = [Paragraph("售后政策", styles["title"])]
    story.append(Paragraph("一、退款", styles["heading"]))
    for line in BODY[:2]:
        story.append(Paragraph(line, styles["body"]))
    story.append(Paragraph("二、换货", styles["heading"]))
    for line in BODY[2:]:
        story.append(Paragraph(line, styles["body"]))
    doc.build(story)


def build_table(styles: dict[str, ParagraphStyle]) -> None:
    doc = SimpleDocTemplate(str(OUT / "policy_table.pdf"), pagesize=A4)
    rows = [
        ["服务类型", "时限", "说明"],
        ["无理由退款", "7 天", "按原支付路径退回"],
        ["质量问题退款", "15 天", "需提供照片凭证"],
        ["换货", "7 天", "商品完好、配件齐全"],
    ]
    table = Table(rows, colWidths=[110, 70, 240])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.5, "black"),
                ("BACKGROUND", (0, 0), (-1, 0), "#eeeeee"),
            ]
        )
    )
    doc.build(
        [
            Paragraph("售后时效对照表", styles["title"]),
            Paragraph("下表列出各服务类型的时限。", styles["body"]),
            Spacer(1, 12),
            table,
        ]
    )


def build_two_col(styles: dict[str, ParagraphStyle]) -> None:
    """双栏：验证解析器能按阅读顺序把左右栏接起来，而不是交叉串行。"""
    doc = BaseDocTemplate(str(OUT / "policy_two_col.pdf"), pagesize=A4)
    gap = 24
    width, height = A4
    col_width = (width - 2 * 36 - gap) / 2
    left = Frame(36, 36, col_width, height - 72, id="left")
    right = Frame(36 + col_width + gap, 36, col_width, height - 72, id="right")
    doc.addPageTemplates([PageTemplate(id="two", frames=[left, right])])

    story = [Paragraph("售后政策（双栏版）", styles["title"])]
    for index, line in enumerate(BODY, start=1):
        story.append(Paragraph(f"{index}. {line}", styles["body"]))
        story.append(Spacer(1, 8))
    doc.build(story)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _register_font()
    styles = _styles()
    build_single(styles)
    build_table(styles)
    build_two_col(styles)
    for path in sorted(OUT.glob("*.pdf")):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
