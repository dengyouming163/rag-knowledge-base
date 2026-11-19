"""PDF 解析客户端：优先走解析容器，容器不可用时本地降级。

设计约束（与 CLAUDE.md 一致）：
1. 主镜像**不装** unstructured / pdfminer 等重依赖，只依赖 httpx；
2. 解析容器是外部依赖，**必须能降级**——容器挂了不应该让上传直接 500；
3. 降级路径用 pypdf（轻量、纯 Python），没装就抛 `ParserUnavailable`，
   由调用方决定是重试还是提示用户；
4. HTML 表格转成 Markdown，大表切块时重复表头，避免召回单块后丢失列语义。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

import httpx

from src.tables import html_table_to_rows, rows_to_markdown, split_markdown_table


class ParserUnavailable(RuntimeError):
    """解析服务与本地降级都不可用。"""


@dataclass(frozen=True)
class PdfBlock:
    type: str
    text: str
    page: int
    section: str = ""
    meta: dict = field(default_factory=dict, compare=False, repr=False)


class PdfParserClient:
    """POST {base_url}/parse，返回结构化块。

    没配 `PARSER_BASE_URL` 时直接用本地 pypdf 抽取——
    这样开发机和 CI 不需要起解析容器也能跑通。
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved = base_url if base_url is not None else os.environ.get("PARSER_BASE_URL", "")
        self.base_url = resolved.rstrip("/")
        # transport 可注入：单元测试用 httpx.MockTransport，不发真实请求
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PdfParserClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def parse(self, content: bytes, filename: str = "document.pdf") -> list[PdfBlock]:
        if not content:
            raise ValueError("PDF 内容为空")
        if not self.base_url:
            return self._local(content, reason="未配置 PARSER_BASE_URL")

        try:
            response = self._client.post(
                f"{self.base_url}/parse",
                files={"file": (filename, content, "application/pdf")},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # 容器挂了/超时/返回非 2xx：降级到本地，不让上传整体失败
            return self._local(content, reason=f"解析服务不可用：{exc}")

        return self._from_payload(response.json())

    @staticmethod
    def _from_payload(payload: dict) -> list[PdfBlock]:
        blocks = []
        for item in payload.get("blocks", []):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            blocks.append(
                PdfBlock(
                    type=str(item.get("type") or "text"),
                    text=text,
                    page=int(item.get("page") or 1),
                    section=str(item.get("section") or ""),
                )
            )
        return blocks

    @staticmethod
    def _local(content: bytes, *, reason: str = "") -> list[PdfBlock]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ParserUnavailable(
                f"{reason}；本地降级需要 pypdf（pip install pypdf）"
            ) from exc

        try:
            reader = PdfReader(io.BytesIO(content))
            blocks = []
            for index, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append(PdfBlock(type="text", text=text, page=index))
        except Exception as exc:  # noqa: BLE001 - 损坏的 PDF 要给出可读原因
            raise ParserUnavailable(f"{reason}；本地解析失败：{exc}") from exc

        if not blocks:
            raise ParserUnavailable(f"{reason}；未解析出文本（可能是扫描件，需要 OCR）")
        return blocks


def blocks_to_text(blocks: list[PdfBlock]) -> str:
    """把块拼成带章节标题的 Markdown，供 chunk.split_document 复用。

    章节变化时插入 `## 标题`，这样分块器能识别标题层级、
    子块也能继承 `section_path`；页码不写进正文，避免污染 BM25 词频。
    表格块转成 Markdown 并重复大表表头，让独立召回的表格块仍保留列含义。
    """
    lines: list[str] = []
    current = None
    for block in blocks:
        if block.section and block.section != current:
            lines.append(f"## {block.section}")
            current = block.section
        if block.type == "table":
            rows = html_table_to_rows(block.text)
            if rows:
                # max_rows=20 是控制块大小的经验值；极宽/极长表格仍可能被
                # split_document 二次切分，导致后续块无表头，属于已知限制。
                tables = split_markdown_table(rows)
                if not tables:
                    tables = [rows_to_markdown(rows)]
                for table in tables:
                    lines.append(table)
                    lines.append("")
                continue
        lines.append(block.text)
        lines.append("")
    return "\n".join(lines).strip()
