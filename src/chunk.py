"""文档解析与结构化切分：Markdown 标题层级、段落与超长段落二次切分。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Audience = Literal["public", "internal"]

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_DIGIT_RE = re.compile(r"\d")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]*(?:[。！？!?]+|\.(?=\s|$)|\n|$)")


def estimate_tokens(text: str) -> int:
    """轻量估算 token 数：中文按 1 字≈1 token，英文按 4 字符≈1 token，数字按 0.7 token/字符。

    该值只用于切分控制与选块预算，不追求与真实分词器一致。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    digits = len(_DIGIT_RE.findall(text))
    others = max(0, len(text) - cjk - digits)
    return max(1, round(cjk + digits * 0.7 + others / 4))


def _char_cost(char: str) -> float:
    if _CJK_RE.match(char):
        return 1.0
    if _DIGIT_RE.match(char):
        return 0.7
    return 0.25


class Chunk(BaseModel):
    """一个可检索的知识块，携带文档与章节来源信息。"""

    chunk_id: str
    doc_id: str
    text: str
    title: str = ""
    section_path: list[str] = Field(default_factory=list)
    source_url: str = ""
    content_hash: str
    audience: Audience = "public"
    token_count: int


def _split_sections(text: str) -> list[tuple[list[str], str]]:
    """按 Markdown 标题层级切出章节，返回 (section_path, 正文) 列表。"""
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []
    current_path: list[str] = []
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((list(current_path), body))

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            body_lines = []
            level = len(match.group(1))
            name = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            current_path = [item for _, item in stack]
        else:
            body_lines.append(line)

    flush()
    return sections


def _atomic_units(block: str) -> list[str]:
    """把段落拆成不可再分的单元：连续表格行各自成单元，其余文本按行聚合。"""
    units: list[str] = []
    buffer: list[str] = []

    for line in block.splitlines():
        if line.lstrip().startswith("|"):
            if buffer:
                units.append("\n".join(buffer))
                buffer = []
            units.append(line)
        else:
            buffer.append(line)

    if buffer:
        units.append("\n".join(buffer))
    return [unit.strip() for unit in units if unit.strip()]


def _split_sentences(text: str) -> list[str]:
    """按中英文句号、问号、感叹号与换行切句。"""
    parts = [part.strip() for part in _SENTENCE_RE.findall(text)]
    return [part for part in parts if part] or [text.strip()]


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """按估算 token 硬切，仅在句子仍超长时兜底使用。"""
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start
        cost = 0.0
        while end < len(text) and cost < max_tokens:
            cost += _char_cost(text[end])
            end += 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    return pieces


def _subdivide(block: str, max_tokens: int) -> list[str]:
    """把段落切成若干不超过 max_tokens 的片段，优先保留表格行完整。"""
    pieces: list[str] = []
    for unit in _atomic_units(block):
        if estimate_tokens(unit) <= max_tokens:
            pieces.append(unit)
            continue
        if unit.lstrip().startswith("|"):
            # 超长表格行整体保留：宁可超限，也不在行内切断
            pieces.append(unit)
            continue
        for sentence in _split_sentences(unit):
            if estimate_tokens(sentence) <= max_tokens:
                pieces.append(sentence)
            else:
                pieces.extend(_hard_split(sentence, max_tokens))
    return pieces


def _tail_by_tokens(text: str, tokens: int) -> str:
    """从末尾按估算 token 截取重叠文本，尽量对齐行首以免切断表格行。"""
    total = 0.0
    index = len(text)
    while index > 0 and total < tokens:
        index -= 1
        total += _char_cost(text[index])

    tail = text[index:]
    if index > 0 and text[index - 1] != "\n":
        line_start = text.rfind("\n", 0, index) + 1
        if text[line_start : index + 1].lstrip().startswith("|"):
            # 切点落在表格行内部，放弃本次重叠以保证行完整
            return ""
        newline = tail.find("\n")
        if newline != -1:
            tail = tail[newline + 1 :]
    return tail.strip()


def split_document(
    text: str,
    *,
    doc_id: str,
    title: str = "",
    source_url: str = "",
    audience: Audience = "public",
    target_tokens: int = 500,
    max_tokens: int = 800,
    overlap_tokens: int = 50,
) -> list[Chunk]:
    """把文档切分为带稳定 chunk_id 与重叠上下文的知识块。

    优先按 Markdown 标题层级组织，无标题时退化为按段落切分；单段超长时按句子
    边界二次切分，仍超长再硬切。相邻块之间按 overlap_tokens 做文本级重叠。
    """
    if target_tokens <= 0:
        raise ValueError("target_tokens 必须为正整数")
    if max_tokens <= 0:
        raise ValueError("max_tokens 必须为正整数")
    if not 0 <= overlap_tokens < target_tokens:
        raise ValueError("overlap_tokens 必须非负且小于 target_tokens")
    if audience not in ("public", "internal"):
        raise ValueError("audience 只能是 public 或 internal")
    if not text or not text.strip():
        return []

    pack_limit = min(target_tokens, max_tokens)
    sections = _split_sections(text) or [([], text)]

    drafts: list[tuple[list[str], str]] = []
    for section_path, body in sections:
        pieces: list[str] = []
        for paragraph in _PARAGRAPH_RE.split(body):
            if paragraph.strip():
                pieces.extend(_subdivide(paragraph, max_tokens))
        if not pieces:
            continue

        budget = pack_limit if not drafts else max(1, pack_limit - overlap_tokens)
        buffer: list[str] = []
        used = 0
        for piece in pieces:
            size = estimate_tokens(piece)
            if buffer and used + size > budget:
                drafts.append((section_path, "\n\n".join(buffer)))
                buffer = []
                used = 0
                budget = max(1, pack_limit - overlap_tokens)
            buffer.append(piece)
            used += size
        if buffer:
            drafts.append((section_path, "\n\n".join(buffer)))

    chunks: list[Chunk] = []
    previous_body = ""
    for index, (section_path, body) in enumerate(drafts, start=1):
        chunk_text = body
        if previous_body and overlap_tokens > 0:
            tail = _tail_by_tokens(previous_body, overlap_tokens)
            if tail:
                chunk_text = f"{tail}\n{chunk_text}"
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}#{index}",
                doc_id=doc_id,
                text=chunk_text,
                title=title,
                section_path=section_path,
                source_url=source_url,
                content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:16],
                audience=audience,
                token_count=estimate_tokens(chunk_text),
            )
        )
        previous_body = body

    return chunks


def load_markdown_file(path: Path, **kwargs: object) -> list[Chunk]:
    """读取 UTF-8 Markdown 文件并切分；doc_id 默认取文件名，title 默认取第一个一级标题。"""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在：{file_path}")

    text = file_path.read_text(encoding="utf-8")
    kwargs.setdefault("doc_id", file_path.stem)
    if not kwargs.get("title"):
        kwargs["title"] = _first_h1(text)
    return split_document(text, **kwargs)  # type: ignore[arg-type]


def _first_h1(text: str) -> str:
    for line in text.splitlines():
        match = _H1_RE.match(line)
        if match:
            return match.group(1).strip()
    return ""
