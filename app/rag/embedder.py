# -*- coding: utf-8 -*-
"""
文本向量化（embedding）
============================================================
把文字变成一串数字，让「语义相近」变成「向量距离近」。
这是 RAG 里唯一一处「魔法」，其他部分都是普通工程。

【为什么要支持多个后端】
embedding 决定了检索质量的上限。生产上应该用真正的语义模型，
但为了让整条链路在「没有任何额外 Key」的情况下也能跑通和自测，
这里做了可插拔的设计，并按可用性自动降级：

    1. dashscope —— 阿里云百炼 text-embedding-v3，真正的语义向量（推荐）
    2. local     —— 本地哈希词袋，零依赖兜底，仅用于跑通链路

⚠️ 两者的检索质量差距很大。用 local 只是为了验证代码链路通畅，
   真要用来回答问题、跑评测数据，请配好 DASHSCOPE_API_KEY。
"""

import os
import zlib
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# 百炼的一批最多 10 条文本，稳妥起见按 10 切批
DASHSCOPE_BATCH = 10
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
DASHSCOPE_MODEL = "text-embedding-v3"
DASHSCOPE_DIM = 1024

# 本地兜底后端的维度。512 是个够用又不会太慢的值。
LOCAL_DIM = 512


class EmbeddingError(Exception):
    """向量化失败。"""


# ============================================================
# 后端 1：阿里云百炼（真正的语义向量）
# ============================================================
def _encode_dashscope(texts, api_key, timeout=60):
    vectors = []
    for i in range(0, len(texts), DASHSCOPE_BATCH):
        batch = texts[i:i + DASHSCOPE_BATCH]
        try:
            resp = httpx.post(
                DASHSCOPE_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": DASHSCOPE_MODEL, "input": batch,
                      "dimensions": DASHSCOPE_DIM, "encoding_format": "float"},
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise EmbeddingError(f"连不上百炼 embedding 服务：{e}") from e

        if resp.status_code != 200:
            raise EmbeddingError(
                f"百炼 embedding 失败 HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        # 按 index 排序，保证返回顺序和输入顺序一致 ——
        # 顺序错了会导致「向量和文本对不上」，是最隐蔽的一类 bug
        items = sorted(data["data"], key=lambda x: x["index"])
        vectors.extend(item["embedding"] for item in items)

    return np.asarray(vectors, dtype=np.float32)


# ============================================================
# 后端 2：本地哈希词袋（零依赖兜底）
# ============================================================
def _tokens(text: str) -> list:
    """把文本切成 字符 unigram + bigram。

    中文没有空格分词，但用字符级 n-gram 已经能捕捉相当多的信息，
    而且完全不需要分词器、不需要下载模型。
    """
    # 只保留中文字、字母和数字，丢掉标点和空白
    chars = [c for c in text if c.isalnum()]
    grams = list(chars)
    grams += [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    return grams


def _hash_bucket(token: str, dim: int):
    """把 token 稳定地映射到某个维度。

    ★ 这里必须用 crc32 而不是 Python 内置的 hash()。
    内置 hash() 对字符串是**每个进程随机加盐**的（PYTHONHASHSEED），
    同一个词这次跑和下次跑会落到不同的桶里 —— 索引就废了。
    这是个很隐蔽的坑，写哈希特征时一定要留神。
    """
    h = zlib.crc32(token.encode("utf-8"))
    # 用一位来定符号，减少哈希碰撞带来的系统性偏差
    sign = 1.0 if (h >> 31) & 1 else -1.0
    return h % dim, sign


def _encode_local(texts, dim=LOCAL_DIM):
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for row, text in enumerate(texts):
        for token in _tokens(text):
            bucket, sign = _hash_bucket(token, dim)
            out[row, bucket] += sign
    return out


def _l2_normalize(matrix):
    """按行做 L2 归一化。

    归一化之后，两个向量的点积就等于余弦相似度 ——
    于是「算相似度」退化成了「一次矩阵乘法」，快很多。
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0            # 防止除零
    return matrix / norms


# ============================================================
# 统一入口
# ============================================================
class Embedder:
    """统一的向量化接口。自动选择可用后端。"""

    def __init__(self, backend: str = "auto"):
        self.api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()

        if backend == "auto":
            backend = "dashscope" if self.api_key else "local"
        if backend == "dashscope" and not self.api_key:
            raise EmbeddingError(
                "指定了 dashscope 后端，但 .env 里没有 DASHSCOPE_API_KEY")

        self.backend = backend
        self.model = DASHSCOPE_MODEL if backend == "dashscope" else "local-hash"
        self.dim = DASHSCOPE_DIM if backend == "dashscope" else LOCAL_DIM

    # ---- 对外只暴露这一个方法 ----
    def encode(self, texts) -> np.ndarray:
        """把文本列表转成 (n, dim) 的归一化向量矩阵。"""
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        if self.backend == "dashscope":
            matrix = _encode_dashscope(texts, self.api_key)
        else:
            matrix = _encode_local(texts, self.dim)

        return _l2_normalize(matrix.astype(np.float32))

    def encode_one(self, text: str) -> np.ndarray:
        """单条文本的向量，形状 (dim,)。"""
        return self.encode([text])[0]

    def describe(self) -> dict:
        """写入索引文件，用来判断索引和当前后端是否匹配。"""
        return {"backend": self.backend, "model": self.model, "dim": self.dim}
