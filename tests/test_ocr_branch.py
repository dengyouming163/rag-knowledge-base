"""扫描件 OCR 回退测试：伪造 OCR 引擎，不下载或加载模型。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pdfplumber
from fastapi.testclient import TestClient
from PIL import Image

PARSER_MAIN = Path(__file__).resolve().parents[1] / "parser" / "main.py"


def _load_parser_main():
    spec = importlib.util.spec_from_file_location("kb_parser_ocr_test", PARSER_MAIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_empty_parse_result_calls_injected_ocr(monkeypatch) -> None:
    module = _load_parser_main()
    received: list[bytes] = []

    monkeypatch.setattr(module, "_parse_pdf", lambda content: [])

    def fake_ocr(content: bytes) -> list[object]:
        received.append(content)
        return [module.Block(type="text", text="扫描件正文", page=1, section="")]

    monkeypatch.setattr(module, "_ocr_pdf", fake_ocr, raising=False)

    response = TestClient(module.app).post(
        "/parse", files={"file": ("scan.pdf", b"%PDF-fake", "application/pdf")}
    )

    assert response.status_code == 200
    assert response.json()["blocks"] == [
        {"type": "text", "text": "扫描件正文", "page": 1, "section": ""}
    ]
    assert received == [b"%PDF-fake"]


def test_non_empty_parse_result_does_not_call_ocr(monkeypatch) -> None:
    module = _load_parser_main()
    block = module.Block(type="text", text="文字层正文", page=1, section="")
    monkeypatch.setattr(module, "_parse_pdf", lambda content: [block])

    def unexpected_ocr(content: bytes) -> list[object]:
        raise AssertionError("正常文本 PDF 不应触发 OCR")

    monkeypatch.setattr(module, "_ocr_pdf", unexpected_ocr, raising=False)

    response = TestClient(module.app).post(
        "/parse", files={"file": ("text.pdf", b"%PDF-fake", "application/pdf")}
    )

    assert response.status_code == 200
    assert response.json()["blocks"][0]["text"] == "文字层正文"


def test_ocr_failure_returns_readable_422(monkeypatch) -> None:
    module = _load_parser_main()
    monkeypatch.setattr(module, "_parse_pdf", lambda content: [])

    def unavailable_ocr(content: bytes) -> list[object]:
        raise RuntimeError("RapidOCR 模型不可用")

    monkeypatch.setattr(module, "_ocr_pdf", unavailable_ocr, raising=False)

    response = TestClient(module.app).post(
        "/parse", files={"file": ("scan.pdf", b"%PDF-fake", "application/pdf")}
    )

    assert response.status_code == 422
    assert "扫描件 OCR 失败" in response.json()["detail"]
    assert "RapidOCR 模型不可用" in response.json()["detail"]


def test_ocr_without_text_returns_readable_422(monkeypatch) -> None:
    module = _load_parser_main()
    monkeypatch.setattr(module, "_parse_pdf", lambda content: [])
    monkeypatch.setattr(module, "_ocr_pdf", lambda content: [], raising=False)

    response = TestClient(module.app).post(
        "/parse", files={"file": ("scan.pdf", b"%PDF-fake", "application/pdf")}
    )

    assert response.status_code == 422
    assert "扫描件 OCR 未识别出文本" in response.json()["detail"]


def _patch_pdf_pages(monkeypatch, pages: list[object]) -> None:
    class FakePdf:
        def __init__(self) -> None:
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(pdfplumber, "open", lambda stream: FakePdf())


def test_default_ocr_engine_is_initialized_once_and_reused(monkeypatch) -> None:
    module = _load_parser_main()
    image = Image.new("RGB", (2, 2), "white")
    page = SimpleNamespace(to_image=lambda resolution: SimpleNamespace(original=image))
    _patch_pdf_pages(monkeypatch, [page])
    monkeypatch.setattr(module, "_OCR_ENGINE", None, raising=False)
    initializations = 0
    inferences = 0

    def fake_ocr(image_array):
        nonlocal inferences
        inferences += 1
        return [[[0, 0], "扫描件正文", 0.99]], 0.01

    def fake_engine_factory():
        nonlocal initializations
        initializations += 1
        return fake_ocr

    fake_module = ModuleType("rapidocr_onnxruntime")
    fake_module.RapidOCR = fake_engine_factory
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake_module)

    module._ocr_pdf(b"%PDF-first")
    module._ocr_pdf(b"%PDF-second")

    assert inferences == 2
    assert initializations == 1


def test_ocr_pdf_converts_fake_engine_results_to_blocks(monkeypatch) -> None:
    module = _load_parser_main()
    image = Image.new("RGB", (2, 2), "white")
    resolutions: list[int] = []

    def fake_page():
        def to_image(resolution: int):
            resolutions.append(resolution)
            return SimpleNamespace(original=image)

        return SimpleNamespace(to_image=to_image)

    _patch_pdf_pages(monkeypatch, [fake_page(), fake_page()])
    calls: list[tuple[int, ...]] = []

    def fake_ocr(image_array):
        calls.append(image_array.shape)
        if len(calls) == 1:
            return [[[0, 0], "第一页标题", 0.99], [[0, 1], "第一页正文", 0.98]], 0.01
        return [[[0, 0], "  ", 0.99], [[0, 1], "第二页正文", 0.97]], 0.01

    blocks = module._ocr_pdf(b"%PDF-fake", ocr=fake_ocr)

    assert [block.model_dump() for block in blocks] == [
        {"type": "text", "text": "第一页标题\n第一页正文", "page": 1, "section": ""},
        {"type": "text", "text": "第二页正文", "page": 2, "section": ""},
    ]
    assert calls == [(2, 2, 3), (2, 2, 3)]
    assert resolutions == [200, 200]
