"""PDF 解析微服务（独立容器）。

为什么单独一个容器：
PDF 解析依赖只活在这个容器里，主服务用 HTTP 调它（见 src/parsers/pdf.py），
主镜像体积不变；早期 unstructured 方案还会连带装入 pdfminer / layoutparser / torch，
让镜像从 400MB 涨到 2GB+。

切换原因：unstructured fast 不做表格结构识别，真实 PDF 验收不达标，
因此改用无需模型、镜像更小的 pdfplumber，并显式输出去重后的文本与表格块。

接口：
    GET  /health   → {"status": "ok"}
    POST /parse    → {"blocks": [{type, text, page, section}]}
"""

from __future__ import annotations

import html
import io
import threading
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

app = FastAPI(title="kb-parser", version="0.1.0")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 13pt 为启发式，对正文大字号文档可能误判标题，影响仅限 section 噪声，后续可按文档主字号自适应。
TITLE_FONT_SIZE_MIN = 13.0
_OCR_ENGINE: Any | None = None
_OCR_LOCK = threading.RLock()

# 旧元素类型 → 我们的块类型。保留该纯函数以兼容现有调用方和单元测试；
# HTTP 解析流程已不再依赖 unstructured。
TYPE_MAP = {
    "Title": "title",
    "Header": "header",
    "Footer": "footer",
    "NarrativeText": "text",
    "Text": "text",
    "ListItem": "list",
    "Table": "table",
    "FigureCaption": "text",
}


class Block(BaseModel):
    type: str
    text: str
    page: int
    section: str


class ParseResponse(BaseModel):
    blocks: list[Block]


def to_blocks(elements: list[Any]) -> list[Block]:
    """把旧式元素列表转成稳定的块结构，并继承最近一个标题。"""
    blocks: list[Block] = []
    section = ""
    for element in elements:
        text = (getattr(element, "text", "") or "").strip()
        if not text:
            continue

        raw = getattr(element, "category", None) or getattr(element, "type", None) or "Text"
        block_type = TYPE_MAP.get(str(raw), "text")
        if block_type == "title":
            section = text

        metadata = getattr(element, "metadata", None)
        page = int(getattr(metadata, "page_number", 1) or 1)
        blocks.append(Block(type=block_type, text=text, page=page, section=section))
    return blocks


def _table_to_html(table: Any) -> str:
    rows = [
        ["" if cell is None else str(cell).strip() for cell in row]
        for row in (table.extract() or [])
    ]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""

    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table>{body}</table>"


def _inside_any_table(word: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    x = (float(word["x0"]) + float(word["x1"])) / 2
    y = (float(word["top"]) + float(word["bottom"])) / 2
    return any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)


# 按 top 聚行 + x0 排序对复杂双栏/上下标版面可能错序，属已知限制，当前样本验收以内容流顺序为准。
def _group_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        top = float(word["top"])
        if not lines or abs(top - lines[-1]["top"]) > 3.0:
            lines.append({"top": top, "words": [word]})
        else:
            lines[-1]["words"].append(word)

    result = []
    for line in lines:
        line_words = sorted(line["words"], key=lambda item: float(item["x0"]))
        text = " ".join(str(word["text"]) for word in line_words).strip()
        if not text:
            continue
        result.append(
            {
                "kind": "line",
                "top": line["top"],
                "x0": min(float(word["x0"]) for word in line_words),
                "text": text,
                "size": sum(float(word.get("size", 0)) for word in line_words) / len(line_words),
            }
        )
    return result


def _parse_pdf(content: bytes) -> list[Block]:
    import pdfplumber

    blocks: list[Block] = []
    section = ""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            tables = list(page.find_tables())
            boxes = [tuple(table.bbox) for table in tables]
            events: list[dict[str, Any]] = []
            for table in tables:
                table_html = _table_to_html(table)
                if table_html:
                    events.append(
                        {
                            "kind": "table",
                            "top": float(table.bbox[1]),
                            "x0": float(table.bbox[0]),
                            "text": table_html,
                        }
                    )

            words = page.extract_words(extra_attrs=["size"])
            remaining_words = [word for word in words if not _inside_any_table(word, boxes)]
            events.extend(_group_lines(remaining_words))
            events.sort(key=lambda event: (event["top"], event["x0"]))

            text_lines: list[str] = []

            def flush_text(page_number=page_number, section=section) -> None:
                nonlocal text_lines
                if text_lines:
                    blocks.append(
                        Block(
                            type="text",
                            text="\n".join(text_lines),
                            page=page_number,
                            section=section,
                        )
                    )
                    text_lines = []

            for event in events:
                if event["kind"] == "table":
                    flush_text()
                    blocks.append(
                        Block(
                            type="table",
                            text=event["text"],
                            page=page_number,
                            section=section,
                        )
                    )
                elif event["size"] >= TITLE_FONT_SIZE_MIN:
                    flush_text()
                    section = event["text"]
                    blocks.append(
                        Block(type="title", text=section, page=page_number, section=section)
                    )
                else:
                    text_lines.append(event["text"])
            flush_text()
    return blocks


def _ocr_pdf(content: bytes, ocr: Any | None = None) -> list[Block]:
    """把无文字层 PDF 逐页渲染后交给 RapidOCR，并复用统一块结构。"""
    global _OCR_ENGINE
    if ocr is None:
        # 默认引擎只初始化一次；持锁递归复用下方流程，同时串行化 ONNX 推理。
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _OCR_ENGINE = RapidOCR()
            return _ocr_pdf(content, ocr=_OCR_ENGINE)

    import numpy as np
    import pdfplumber

    blocks: list[Block] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            image = page.to_image(resolution=200).original
            result, _ = ocr(np.asarray(image))
            lines = []
            for item in result or []:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
            if lines:
                blocks.append(
                    Block(type="text", text="\n".join(lines), page=page_number, section="")
                )
    return blocks


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/parse", response_model=ParseResponse)
async def parse(file: UploadFile = File(...)) -> ParseResponse:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 20MB 大小限制")
    if not content:
        raise HTTPException(status_code=400, detail="空文件")

    try:
        blocks = await run_in_threadpool(_parse_pdf, content)
    except Exception as exc:  # noqa: BLE001 - 解析失败要给出可读原因
        raise HTTPException(status_code=422, detail=f"PDF 解析失败：{exc}") from exc

    if not blocks:
        try:
            blocks = await run_in_threadpool(_ocr_pdf, content)
        except Exception as exc:  # noqa: BLE001 - 缺少 OCR 依赖或模型时快速给出可读原因
            raise HTTPException(
                status_code=422,
                detail=f"扫描件 OCR 失败：{exc}；请确认 parser 镜像已预置 RapidOCR 模型",
            ) from exc
        if not blocks:
            raise HTTPException(status_code=422, detail="扫描件 OCR 未识别出文本，请检查扫描件清晰度")
    return ParseResponse(blocks=blocks)
