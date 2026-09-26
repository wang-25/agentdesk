# -*- coding: utf-8 -*-
"""
文档加载与切分
============================================================
RAG 的第一步：把「一堆文件」变成「一堆可检索的小块」。

【为什么必须切分】
模型一次能看到的文本量有限（上下文窗口），而且就算放得下，
把整篇文档塞进去也是浪费 —— 检索的意义就是「只把相关的那几段给它」。
所以要把文档切成小块（chunk），每块单独向量化，检索时按块召回。

【切分策略的两个关键参数】
  chunk_size（块大小）
      - 太小：语义不完整，检索到了也答不上来
      - 太大：一堆无关内容混进来，稀释了相关性
      实践值：中文 300~500 字
  overlap（重叠）
      相邻块之间保留一段重叠内容。为什么需要？
      因为关键信息很可能刚好被切在边界上 —— 前半句在上一块、后半句在下一块，
      两块单独看都不完整。留一段重叠能缓解这个问题。
      实践值：块大小的 10%~20%

【本实现的做法】
不做「按固定字数硬切」，而是**先按空行和标题切成语义段落，再合并到目标长度**。
这样切出来的块基本都能保持语义完整，比硬切效果好得多。
"""

import re
from pathlib import Path
from typing import Iterator

# 默认参数：中文文档的经验值
DEFAULT_CHUNK_SIZE = 400
DEFAULT_OVERLAP = 80


class Document:
    """一篇原始文档。"""

    __slots__ = ("doc_id", "source", "title", "text")

    def __init__(self, doc_id: str, source: str, title: str, text: str):
        self.doc_id = doc_id        # 唯一标识，用文件名（不含扩展名）
        self.source = source        # 相对路径，用于引用溯源
        self.title = title          # 文档标题，用于引用溯源
        self.text = text


class Chunk:
    """切分后的一个块。"""

    __slots__ = ("chunk_id", "doc_id", "source", "title", "index", "text")

    def __init__(self, chunk_id, doc_id, source, title, index, text):
        self.chunk_id = chunk_id    # doc_id#序号
        self.doc_id = doc_id
        self.source = source
        self.title = title
        self.index = index
        self.text = text

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "index": self.index,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(d["chunk_id"], d["doc_id"], d["source"],
                   d["title"], d["index"], d["text"])

    def preview(self, n: int = 60) -> str:
        """一行摘要，给日志和调试用。"""
        one_line = re.sub(r"\s+", " ", self.text).strip()
        return one_line[:n] + ("..." if len(one_line) > n else "")


# ============================================================
# 加载
# ============================================================
def _extract_title(text: str, fallback: str) -> str:
    """从 Markdown 的第一个 # 标题里取文档标题。取不到就用文件名。"""
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def load_documents(directory) -> list:
    """读取目录下所有 .md / .txt 文件。

    encoding="utf-8" 是必须的 —— 中文 Windows 默认用 GBK 读文件，
    中文语料会直接报 UnicodeDecodeError。
    """
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"知识库目录不存在：{directory}")

    docs = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".md", ".txt"):
            continue
        text = path.read_text(encoding="utf-8")
        doc_id = path.stem
        docs.append(Document(
            doc_id=doc_id,
            source=str(path.relative_to(directory)).replace("\\", "/"),
            title=_extract_title(text, doc_id),
            text=text,
        ))
    return docs


# ============================================================
# 切分
# ============================================================
def _split_blocks(text: str) -> list:
    """把文档切成语义段落。

    优先在空行处断开 —— Markdown 里空行就是段落边界。
    代码块和表格内部也会有空行，但对我们的语料来说影响不大，
    真要严谨可以用 Markdown 解析器，但没必要为这个引入依赖。
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text)]
    return [b for b in blocks if b]


def _split_long_block(block: str, chunk_size: int) -> list:
    """单个段落就超长时，按行继续切（表格、长列表常见）。

    不按字符硬切 —— 硬切会把一行中的句子劈成两半，读起来莫名其妙。
    按行切至少保证每行是完整的。
    """
    pieces, current = [], ""
    for line in block.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if len(current) + len(line) + 1 <= chunk_size:
            current = f"{current}\n{line}" if current else line
        else:
            if current:
                pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def _tail(text: str, n: int) -> str:
    """取文本末尾 n 个字符，作为下一块的重叠部分。

    尽量从句号处开始，避免把一句话切成两半。
    """
    if len(text) <= n:
        return text
    tail = text[-n:]
    # 找第一个句子结束符，从它之后开始 —— 这样重叠部分是以完整句子起头的
    match = re.search(r"[。！？；\n]", tail)
    if match and match.end() < len(tail):
        return tail[match.end():]
    return tail


def chunk_document(doc: Document, chunk_size: int = DEFAULT_CHUNK_SIZE,
                   overlap: int = DEFAULT_OVERLAP) -> list:
    """把一篇文档切成若干块。"""
    chunks = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            index = len(chunks)
            chunks.append(Chunk(
                chunk_id=f"{doc.doc_id}#{index}",
                doc_id=doc.doc_id,
                source=doc.source,
                title=doc.title,
                index=index,
                text=current.strip(),
            ))
        current = ""

    for block in _split_blocks(doc.text):
        # 单个段落就超长 → 先按行拆成若干段
        for piece in (_split_long_block(block, chunk_size)
                      if len(block) > chunk_size else [block]):

            # 加进去会不会超？会就先落盘，然后带上重叠重新开一块
            if current and len(current) + len(piece) + 2 > chunk_size:
                flush()
                if overlap > 0:
                    current = _tail(chunks[-1].text, overlap) + "\n\n"

            current = f"{current}\n\n{piece}" if current else piece

    flush()
    return chunks


def chunk_documents(docs: list, chunk_size: int = DEFAULT_CHUNK_SIZE,
                    overlap: int = DEFAULT_OVERLAP) -> list:
    """批量切分。"""
    chunks = []
    for doc in docs:
        chunks.extend(chunk_document(doc, chunk_size, overlap))
    return chunks
