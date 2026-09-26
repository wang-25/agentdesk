# -*- coding: utf-8 -*-
"""
向量存储与检索
============================================================
包含三件事：
  1. VectorStore —— 存向量、存块、做相似度检索
  2. BM25        —— 关键词检索
  3. RRF         —— 把两路结果融合成一路

【为什么需要「混合检索」而不是只用向量】
纯向量检索有个明显短板：**对精确词不敏感**。
它擅长「按意思找」，但分不清 OOMKilled 和 CPU 高 ——
因为这两个词的向量可能离得很近。

而运维场景里全是精确词：OOMKilled、EADDRINUSE、Exit Code 137、
具体的命令名和参数名。这类词一旦被模型「理解成大概意思」，就找不准了。

所以生产上的标准做法是：**向量检索 + 关键词检索（BM25）各跑一遍，再融合**。
向量负责语义，BM25 负责精确匹配，两者互补。

【为什么用 RRF 融合，而不是把分数加权平均】
因为两路的分数量纲完全不同：余弦相似度在 -1~1 之间，
BM25 分数可能是 0~30 的无界值。直接加权平均需要先归一化，
而归一化方式的选择本身就会引入主观偏差。

RRF（Reciprocal Rank Fusion）只用**排名**不用分数：
每个文档得分 = Σ 1/(k + 排名)。k 一般取 60。
好处是完全不需要调权重、对分数量纲不敏感，工业界用了很多年。
"""

import json
import logging
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from app.rag.embedder import Embedder
from app.rag.loader import Chunk

# jieba 第一次调用会打印 "Building prefix dict"，影响输出可读性。
# 它自己的日志走 logging，关掉就好。
try:
    import jieba
    jieba.setLogLevel(logging.WARNING)
except ImportError:      # pragma: no cover
    jieba = None


INDEX_VECTORS = "index.npz"
INDEX_CHUNKS = "chunks.json"


# ============================================================
# BM25
# ============================================================
class BM25:
    """经典的关键词检索算法。

    直觉版解释：一个词在当前文档里出现得越多、在整个语料里出现得越少，
    它对这个文档的代表性就越强。

    k1 控制「词频饱和」：一个词出现 5 次和 50 次，相关性不该差 10 倍，
       所以用 k1 让词频的增长逐渐饱和。常用 1.2~2.0。
    b  控制「长度惩罚」：长文档天然更容易命中词，所以要惩罚一下。
       b=0 完全不惩罚，b=1 完全按长度归一化。常用 0.75。
    """

    def __init__(self, tokenized_corpus, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = tokenized_corpus
        self.n = len(tokenized_corpus)
        self.doc_len = [len(d) for d in tokenized_corpus]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 1.0

        self.freqs = [Counter(d) for d in tokenized_corpus]

        df = Counter()
        for counter in self.freqs:
            df.update(counter.keys())

        # idf：在多少篇文档里出现过。加 0.5 平滑，避免极端值
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def scores(self, query_tokens) -> np.ndarray:
        """返回每个文档的 BM25 分数。"""
        result = np.zeros(self.n, dtype=np.float32)
        for term in query_tokens:
            if term not in self.idf:
                continue
            idf = self.idf[term]
            for i, freq in enumerate(self.freqs):
                tf = freq.get(term, 0)
                if tf == 0:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                result[i] += idf * tf * (self.k1 + 1) / denom
        return result


def tokenize(text: str) -> list:
    """中文分词。装了 jieba 就用 jieba，否则退化成字符 bigram。

    为什么要给退化路径：jieba 不是核心依赖，缺了也不该让整条链路跑不起来。
    """
    if jieba is not None:
        return [w for w in jieba.lcut(text) if w.strip()]
    chars = [c for c in text if c.isalnum()]
    return chars + [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


# ============================================================
# RRF 融合
# ============================================================
def rrf_fuse(rankings, k: int = 60) -> list:
    """把多路检索结果按排名融合。

    参数 rankings 是一个列表，每个元素是「按相关性从高到低排好的下标列表」。
    返回 [(下标, 融合得分), ...]，已按得分降序。
    """
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


# ============================================================
# 向量存储
# ============================================================
class VectorStore:
    """内存里的向量库。支持存盘、载入、三种检索模式。

    【关于「为什么不用 Milvus」】
    Milvus / Qdrant 这类专业向量库解决的是「千万级向量下的近似最近邻搜索」——
    它们用 HNSW、IVF 这类索引结构，牺牲一点精度换极大的速度。

    本项目的语料规模是几百个块，用 numpy 做**精确的**暴力检索
    （一次矩阵乘法）只要几毫秒，反而比引入一个服务更简单可靠。

    接口是刻意设计成一样的（build / search），
    等数据量真的上来了，换实现不用改调用方 —— 这就是分层的好处。
    """

    def __init__(self, embedder: Embedder = None):
        self.embedder = embedder or Embedder()
        self.chunks = []
        self.matrix = None          # (n, dim) 归一化向量
        self.bm25 = None
        self.tokenized = []

    # ---------- 构建 ----------
    def build(self, chunks):
        """向量化所有块，同时建好 BM25 索引。"""
        self.chunks = list(chunks)
        if not self.chunks:
            self.matrix = None
            self.bm25 = None
            return self

        self.matrix = self.embedder.encode([c.text for c in self.chunks])
        self.tokenized = [tokenize(c.text) for c in self.chunks]
        self.bm25 = BM25(self.tokenized)
        return self

    def __len__(self):
        return len(self.chunks)

    # ---------- 存盘 ----------
    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        if self.matrix is not None:
            np.savez_compressed(directory / INDEX_VECTORS, vectors=self.matrix)

        payload = {
            "embedder": self.embedder.describe(),
            "chunks": [c.to_dict() for c in self.chunks],
        }
        (directory / INDEX_CHUNKS).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return directory

    @classmethod
    def load(cls, directory, embedder: Embedder = None):
        """从磁盘载入索引。

        ★ 这里做了一个重要的校验：索引用的 embedding 后端/维度，
        必须和当前的后端一致。否则查出来的向量根本不在同一个空间里，
        检索结果会完全乱掉 —— 而且不报错，只是"莫名其妙查不准"。
        """
        directory = Path(directory)
        chunks_file = directory / INDEX_CHUNKS
        if not chunks_file.exists():
            raise FileNotFoundError(
                f"索引不存在：{chunks_file}，请先执行 build_index()")

        payload = json.loads(chunks_file.read_text(encoding="utf-8"))
        saved = payload.get("embedder", {})

        store = cls(embedder)
        now = store.embedder.describe()
        if saved and (saved.get("dim") != now["dim"]
                      or saved.get("backend") != now["backend"]):
            raise RuntimeError(
                "索引与当前 embedding 后端不匹配，必须重建索引。\n"
                f"  索引里是：{saved}\n"
                f"  当前是：  {now}\n"
                "原因：换了 embedding 后端，向量空间就不同了，"
                "旧的向量没法用来查询。")

        store.chunks = [Chunk.from_dict(d) for d in payload["chunks"]]
        store.tokenized = [tokenize(c.text) for c in store.chunks]
        store.bm25 = BM25(store.tokenized) if store.chunks else None

        vectors_file = directory / INDEX_VECTORS
        if vectors_file.exists():
            store.matrix = np.load(vectors_file)["vectors"]
        else:
            store.matrix = None

        return store

    # ---------- 检索 ----------
    def _rank_vector(self, query: str, limit: int):
        """向量检索，返回按相似度降序的下标列表。

        因为向量都做了 L2 归一化，点积就是余弦相似度，
        所以「n 个块的相似度」可以一次矩阵乘法算完。
        """
        if self.matrix is None or len(self.chunks) == 0:
            return []
        q = self.embedder.encode_one(query)
        sims = self.matrix @ q                      # (n,)
        order = np.argsort(-sims)[:limit]
        return [int(i) for i in order], sims

    def _rank_bm25(self, query: str, limit: int):
        if self.bm25 is None:
            return [], None
        scores = self.bm25.scores(tokenize(query))
        order = np.argsort(-scores)[:limit]
        # 只保留真正有分的，避免一堆 0 分的噪声块混进来
        return [int(i) for i in order if scores[i] > 0], scores

    def search(self, query: str, top_k: int = 5, mode: str = "hybrid",
               candidate_k: int = 20) -> list:
        """检索。mode 可选 vector / bm25 / hybrid。

        candidate_k 是「召回候选数」：先从每路取回 20 个候选，
        融合之后再截取前 top_k。这就是「粗筛 → 精排」的粗筛那一步。
        """
        if len(self.chunks) == 0:
            return []

        if mode == "vector":
            idxs, sims = self._rank_vector(query, top_k)
            picked = [(i, float(sims[i])) for i in idxs]
        elif mode == "bm25":
            idxs, scores = self._rank_bm25(query, top_k)
            picked = [(i, float(scores[i])) for i in idxs]
        elif mode == "hybrid":
            vec_idx, vec_sims = self._rank_vector(query, candidate_k)
            bm_idx, bm_scores = self._rank_bm25(query, candidate_k)
            fused = rrf_fuse([vec_idx, bm_idx])[:top_k]
            picked = [(i, score) for i, score in fused]
        else:
            raise ValueError(f"未知检索模式：{mode}")

        results = []
        for rank, (i, score) in enumerate(picked, 1):
            chunk = self.chunks[i]
            results.append({
                "rank": rank,
                "score": round(score, 6),
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "source": chunk.source,
                "title": chunk.title,
                "text": chunk.text,
                "preview": chunk.preview(70),
            })
        return results
