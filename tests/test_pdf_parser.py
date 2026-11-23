"""Task 3.6：PDF 解析容器与客户端的测试。

不解容器、不联网：
- 容器侧只测 `to_blocks` 的映射规则（unstructured 未安装也能导入）；
- 客户端用 httpx.MockTransport 模拟解析服务；
- 降级路径断言 `ParserUnavailable`，而不是让异常冒到上层。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.parsers.pdf import ParserUnavailable, PdfBlock, PdfParserClient, blocks_to_text

PARSER_MAIN = Path(__file__).resolve().parents[1] / "parser" / "main.py"


def _load_parser_main():
    """按文件路径加载 parser/main.py，避免和 app.main 同名冲突。"""
    spec = importlib.util.spec_from_file_location("kb_parser_main", PARSER_MAIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_to_blocks_maps_types_and_tracks_section() -> None:
    module = _load_parser_main()
    elements = [
        SimpleNamespace(text="售后政策", category="Title", metadata=SimpleNamespace(page_number=1)),
        SimpleNamespace(text="   ", category="Text", metadata=SimpleNamespace(page_number=1)),
        SimpleNamespace(text="支持七天无理由退款。", category="NarrativeText", metadata=SimpleNamespace(page_number=1)),
        SimpleNamespace(text="换货规则", category="Title", metadata=SimpleNamespace(page_number=2)),
        SimpleNamespace(text="收到货七天内可申请换货。", category="ListItem", metadata=SimpleNamespace(page_number=2)),
    ]

    blocks = module.to_blocks(elements)

    # 空白元素被丢弃，只剩 4 条
    assert [block.type for block in blocks] == ["title", "text", "title", "list"]
    # 标题会切换 section，正文继承最近一次标题
    assert [block.section for block in blocks] == ["售后政策", "售后政策", "换货规则", "换货规则"]
    assert blocks[3].page == 2


def test_client_parses_service_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/parse"
        return httpx.Response(
            200,
            json={
                "blocks": [
                    {"type": "title", "text": "售后政策", "page": 1, "section": "售后政策"},
                    {"type": "text", "text": "支持七天无理由退款。", "page": 1, "section": "售后政策"},
                    {"type": "text", "text": "   ", "page": 1, "section": "售后政策"},
                ]
            },
        )

    with PdfParserClient("http://parser:8000", transport=httpx.MockTransport(handler)) as client:
        blocks = client.parse(b"%PDF-1.4 fake", filename="policy.pdf")

    assert [block.text for block in blocks] == ["售后政策", "支持七天无理由退款。"]
    assert blocks[1].section == "售后政策"


def test_client_degrades_when_service_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="boom")

    with PdfParserClient("http://parser:8000", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ParserUnavailable):
            # 降级到本地 pypdf；这段字节不是合法 PDF，两条路都失败 → 明确报错
            client.parse(b"not a real pdf")


def test_empty_content_is_rejected() -> None:
    with PdfParserClient("http://parser:8000") as client:
        with pytest.raises(ValueError):
            client.parse(b"")


def test_blocks_to_text_keeps_section_headers() -> None:
    blocks = [
        PdfBlock(type="title", text="售后政策", page=1, section="售后政策"),
        PdfBlock(type="text", text="支持七天无理由退款。", page=1, section="售后政策"),
        PdfBlock(type="text", text="换货需商品完好。", page=2, section="换货"),
    ]

    text = blocks_to_text(blocks)

    assert text.startswith("## 售后政策")
    assert "## 换货" in text
    assert "支持七天无理由退款。" in text
    # 页码不进正文，避免污染 BM25 词频
    assert "page" not in text and "第2页" not in text
