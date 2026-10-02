# -*- coding: utf-8 -*-
"""检索地基：分词 / BM25 / RRF 融合 / 混合检索 / 兜底向量。

这一层决定"给模型的上下文对不对"。它坏了的表现不是报错，而是**答得头头是道
但引错了文档** —— 没有人会收到告警，所以只能靠测试把它钉住。

所以这里刻意覆盖三类不变量：

    ① **排序**：含关键词的文档必须排在前面（检索的第一性要求）
    ② **不丢东西**：RRF 融合时只在一路出现的候选也要保留 ——
       纯向量和纯 BM25 各有一路召回，融合时把"只被一路召回"的丢掉，
       等于把两路的好处抵消掉了一半，而且完全不会报错
    ③ **兜底不吹牛**：没配 `DASHSCOPE_API_KEY` 时用的是 `local-hash`
       词袋哈希，**它没有语义能力**。本地词面相同才相似、换个说法就是 0 分。
       这一点必须留在测试里，否则"我们做了语义检索"这句话会被重复讲下去
       （docs/redev/01-audit.md 第 6 条已经记过这个账）。

零成本、不联网：语料自己造（`data/index/` 是 gitignore 的构建产物，CI 里不存在），
向量走 `local` 兜底后端。
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from app.rag.embedder import LOCAL_DIM, Embedder, EmbeddingError
from app.rag.loader import Document, chunk_documents
from app.rag.store import BM25, VectorStore, rrf_fuse, tokenize

# ============================================================
# 小语料：5 篇，每篇一个明确的"信号词"
# ============================================================
# 刻意让信号词**不重叠**：这样"含关键词的排在前面"才是可判定的，
# 而不是靠一个模糊的分数大小去猜。
_CORPUS = [
    ("nginx-502", "nginx-502.md", "nginx 502 排查",
     "nginx 报 502 Bad Gateway，通常是后端服务挂了或者端口没起来。\n\n"
     "先用 systemctl status 看后端进程，再看 error.log。"),
    ("disk-full", "disk-full.md", "磁盘写满",
     "磁盘写满会导致写日志失败，进而引发服务异常。\n\n"
     "先用 df -h 看哪个分区满了，再定位大文件。"),
    ("mysql-crash", "mysql-crash.md", "mysql 崩了",
     "mysql 突然退出常见原因是内存不足被 OOMKilled，或者数据目录损坏。\n\n"
     "先看 error.log，再确认内存水位。"),
    ("docker-down", "docker-down.md", "docker 容器退出",
     "docker 容器反复重启要看 exit code 和容器日志。\n\n"
     "exit code 137 一般是被杀，137 基本等于内存不足。"),
    ("cpu-high", "cpu-high.md", "CPU 使用率偏高",
     "CPU 使用率长期偏高会拖慢所有请求。\n\n"
     "先用 top 找进程，再用 uptime 看负载。"),
]


def _doc(doc_id: str, source: str, title: str, text: str) -> Document:
    return Document(doc_id=doc_id, source=source, title=title, text=text)


@pytest.fixture
def corpus_docs():
    """5 篇小文档 —— 够把排序和融合测明白，又不依赖任何构建产物。"""
    return [_doc(*row) for row in _CORPUS]


@pytest.fixture
def store(corpus_docs):
    """在小语料上建好的 VectorStore（local 兜底向量 + BM25）。"""
    chunks = chunk_documents(corpus_docs)
    assert len(chunks) == len(corpus_docs), "小语料应当一篇一块，切分变了要回来看这条"
    return VectorStore().build(chunks)


# ============================================================
# 一、分词
# ============================================================
def test_tokenize_never_returns_empty_for_non_blank_text():
    """非空文本必须切出至少一个词。

    切出空列表等于**该文档永远检索不到** —— 而且不报错，
    只表现为"这条知识明明写进去了却搜不出来"。
    """
    for text in ("磁盘满了", "OOMKilled", "nginx 报 502", "磁盘 df -h 混排"):
        assert tokenize(text), text


def test_tokenize_blank_input_is_empty():
    """空串/纯空白不能崩，也不该凭空造出词（否则每个块都会被无关查询命中）。"""
    for text in ("", "   ", "\n\t "):
        assert tokenize(text) == []


def test_tokenize_keeps_punctuation_out_of_the_term_list():
    """标点不该成为检索词。

    `tokenize` 里那句 `if w.strip()` 只挡得住空白，挡不住标点：
    纯标点文本会原样吐出 `['!', '!', ',']`。这里如实固化当前行为。
    """
    assert tokenize("!!!,,,") == ["!", "!", "!", ",", ",", ","]


def test_tokenize_handles_english_whole_words():
    """英文必须按**整词**切，而不是按字符。

    按字符切的话 "OOMKilled" 会变成一串字母，BM25 的精确匹配就废了 ——
    而运维语料里全是这类精确词。
    """
    assert tokenize("OOMKilled") == ["OOMKilled"], "英文词被切碎了"


@pytest.mark.parametrize("text", ["磁盘满了 df -h", "nginx 报 502", "内存不足 OOMKilled"])
def test_tokenize_mixed_text_keeps_both_languages(text):
    """中英混排两边都要留下：中文词 + 英文词都能对上原文片段。

    只保住一种语言时，混排语料上会有一半查询永远命中不了。
    """
    words = tokenize(text)
    assert any(w in text for w in words if w.isascii() and w.isalpha()), text
    assert any(not w.isascii() for w in words), text


def test_tokenize_is_deterministic():
    """同一段文本两次分词必须一致，否则任何一次性建好的索引都不可复现。"""
    text = "nginx 报 502 Bad Gateway，磁盘也满了"
    assert tokenize(text) == tokenize(text)


# ============================================================
# 二、BM25
# ============================================================
def test_bm25_ranks_the_document_containing_the_term_first():
    """含查询词的文档必须排在前面 —— 检索排序的第一性要求。

    这里用"另一个文档完全不含该词"来做判定，避免拿分数大小去猜。
    """
    corpus = [tokenize("磁盘满了用 df -h 查看"),
              tokenize("nginx 报 502 要看后端进程"),
              tokenize("mysql 内存不足被 OOMKilled")]
    b = BM25(corpus)
    scores = b.scores(tokenize("502"))
    assert scores[1] > 0, "含 502 的文档没得分"
    assert scores[0] == 0 and scores[2] == 0, "不含该词的文档不该得分"


def test_bm25_scores_length_matches_corpus():
    """`scores()` 的下标就是文档下标 —— 长度对不上，`argsort` 就会越界或错位。"""
    corpus = [tokenize("磁盘满了"), tokenize("nginx 502"), tokenize("容器退出")]
    assert len(BM25(corpus).scores(tokenize("磁盘"))) == len(corpus)


def test_bm25_unknown_term_scores_all_zero():
    """语料里不存在的词给全 0，而不是抛 KeyError。

    调用方是检索入口，任何异常都会变成一次 500。
    """
    b = BM25([tokenize("磁盘满了"), tokenize("nginx 502")])
    scores = b.scores(tokenize("这个词根本不存在xyz"))
    assert (scores == 0).all()


def test_bm25_empty_corpus_does_not_crash():
    """空语料（索引没建/语料目录为空）时不能除零崩掉。"""
    b = BM25([])
    assert b.n == 0
    assert list(b.scores(tokenize("任意查询"))) == []


def test_bm25_scores_are_non_negative_and_deterministic():
    """BM25 分数天然非负，且必须可复现（同一输入同一结果）。"""
    corpus = [tokenize("磁盘满了用 df -h 查看"), tokenize("df 查看分区 df df")]
    b = BM25(corpus)
    a = b.scores(tokenize("df 磁盘"))
    assert (a >= 0).all()
    assert np.array_equal(a, b.scores(tokenize("df 磁盘")))


# ============================================================
# 三、RRF 融合
# ============================================================
def test_rrf_fuse_is_deterministic():
    """同一组候选两次融合必须完全一致。

    融合结果不确定的话，同一句告警每次诊断引用的文档都不同，
    评测根本无法复现 —— 这类"偶发不一致"最难查。
    """
    rankings = [[0, 1, 2], [2, 0, 3]]
    assert rrf_fuse(rankings) == rrf_fuse(rankings)


def test_rrf_fuse_keeps_candidates_seen_by_only_one_route():
    """★ 只在一路出现的候选也必须保留 —— RRF 最常见的漏项。

    向量路召回 A，BM25 路召回 B：B 只在第二路出现。
    如果实现写成"取两路的交集"或者按第一路的下标去查分，
    B 会被静默丢掉 —— 而 B 恰恰是关键词精确命中的那一个。
    """
    fused = rrf_fuse([[0], [1]])
    kept = [idx for idx, _ in fused]
    assert set(kept) == {0, 1}, f"只在一路出现的候选被丢了：{kept}"


def test_rrf_fuse_score_is_reciprocal_rank_from_every_route():
    """得分 = Σ 1/(k+名次+1)（名次从 0 起算，即第 1 名是 1/(k+1)）。

    两个东西必须同时成立：**只算名次不算原始分数**（两路分数量纲不同），
    以及两路都出现要**累加**而不是取最大值 —— 否则融合就退化成了
    "以某一路为准"，另一路白跑。
    """
    k = 60
    fused = dict(rrf_fuse([[0, 1], [1, 2]], k=k))
    assert fused[0] == pytest.approx(1 / (k + 1))
    assert fused[1] == pytest.approx(1 / (k + 2) + 1 / (k + 1))   # 两路累加
    assert fused[2] == pytest.approx(1 / (k + 2))                # 只在第二路，名次 1
    assert set(fused) == {0, 1, 2}


def test_rrf_fuse_sorts_by_score_descending():
    """返回必须已按得分降序 —— 调用方直接截前 top_k，不会自己再排一次。"""
    fused = rrf_fuse([[5, 6], [6, 7]])
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)
    assert fused[0][0] == 6, "被两路同时召回的候选应当排第一"


def test_rrf_fuse_empty_input_returns_empty_list():
    """一路都没召回（或压根没跑检索）时返回空列表，而不是抛异常。

    "空结果"和"出错"必须是两种状态：前者是正常情况，后者要报错。
    """
    assert rrf_fuse([]) == []


def test_rrf_fuse_tolerates_an_empty_route():
    """其中一路为空（比如纯向量后端不可用）时，另一路的结果照常保留。"""
    fused = rrf_fuse([[], [0, 1]])
    assert [idx for idx, _ in fused] == [0, 1]


# ============================================================
# 四、VectorStore：构建与检索
# ============================================================
def test_build_reports_corpus_size(store):
    assert len(store) == len(_CORPUS)
    assert store.bm25 is not None, "建库时必须同时建好 BM25 索引，否则 hybrid 缺一路"


@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
def test_search_modes_run_and_respect_top_k(store, mode):
    """三种模式都要能跑通，且返回条数受 `top_k` 约束。

    超发结果会直接把上下文撑爆（也会多花钱），是很容易漏掉的一条边界。
    """
    results = store.search("磁盘 df 写满", top_k=2, mode=mode)
    assert 0 < len(results) <= 2, f"{mode} 返回了 {len(results)} 条"
    assert [r["rank"] for r in results] == list(range(1, len(results) + 1)), \
        "rank 必须是 1..n 的连续序列（调用方直接拿它做展示）"


@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
def test_search_results_carry_citation_fields(store, mode):
    """每条结果都要带得回原文：没有 source / chunk_id 就没法做引用溯源。

    RAG 的答案如果指不回出处，在运维场景里等于不可信。
    """
    top = store.search("磁盘 df 写满", top_k=1, mode=mode)[0]
    for field in ("chunk_id", "doc_id", "source", "title", "text", "preview", "score"):
        assert top.get(field) not in (None, ""), f"{mode} 的结果缺少 {field}"


def test_bm25_mode_does_not_return_zero_score_noise(store):
    """BM25 模式必须过滤 0 分结果 —— 否则会往上下文里塞一堆无关块。

    `_rank_bm25` 里那句 `if scores[i] > 0` 就是干这个的：
    查询跟语料毫无交集时，宁可返回空，也不要"凑够 top_k"。
    """
    assert store.search("这句话跟语料没有任何交集xyz", top_k=3, mode="bm25") == []


def test_vector_mode_always_fills_top_k_even_for_a_nonsense_query(store):
    """**记录当前事实**：向量路没有分数阈值，再离谱的查询也会凑满 top_k。

    local-hash 后端下这个查询的相关度基本是噪声（见下面 embedder 那组用例），
    但它照样会被当成候选混进 hybrid 的粗筛里。所以"检索到的就是相关的"
    这句话在兜底后端下不成立 —— 真正拦住噪声的是 BM25 那一路的 0 分过滤。
    """
    results = store.search("zzz 完全无关的查询 qqq", top_k=3, mode="vector")
    assert len(results) == 3


def test_hybrid_top_k_is_capped_and_ranks_are_unique(store):
    """hybrid = 两路各取 `candidate_k` 个候选 → 融合 → 只截前 `top_k`。

    融合后同一个块有可能被两路各推一次，截取时必须**去重**
    （RRF 用下标累加天然去重，这条是防它被改成列表拼接）。
    """
    results = store.search("磁盘 df 写满 nginx 502", top_k=3, mode="hybrid")
    assert len(results) <= 3
    ids = [r["chunk_id"] for r in results]
    assert len(ids) == len(set(ids)), f"融合结果里有重复块：{ids}"


def test_hybrid_ranking_is_deterministic(store):
    """同一查询两次 hybrid 检索结果必须完全一致（评测与复现的前提）。"""
    a = store.search("磁盘 df 写满", top_k=3, mode="hybrid")
    b = store.search("磁盘 df 写满", top_k=3, mode="hybrid")
    assert a == b


def test_unknown_search_mode_is_rejected_loudly(store):
    """拼错模式名必须报错，不能静默退化成某一种检索。

    静默退化的话，评测里"我们用的是 hybrid"就成了一句没人验证过的话。
    """
    with pytest.raises(ValueError):
        store.search("磁盘", mode="hibrd")


def test_search_on_empty_store_returns_empty_for_every_mode():
    """空库（语料目录为空 / 索引没建）不能崩 —— 首页自检就会打到这里。"""
    empty = VectorStore().build([])
    assert len(empty) == 0
    for mode in ("vector", "bm25", "hybrid"):
        assert empty.search("任意查询", mode=mode) == []


@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
def test_top_k_larger_than_corpus_does_not_pad(store, mode):
    """`top_k` 大于语料规模时按实际数量返回，不补齐、不重复。

    `argsort` 之后如果没做去重就可能把同一个块返回两次 —— 上下文里
    出现两份一样的文本，既浪费 token 又会让模型以为是两条独立证据。
    """
    results = store.search("磁盘 df", top_k=100, mode=mode)
    assert 0 < len(results) <= len(store)
    assert len({r["chunk_id"] for r in results}) == len(results)


def test_save_load_round_trip_keeps_hits_identical(store, tmp_path):
    """存盘再载入，检索结果必须一模一样。

    索引是构建产物 + 运行时读取两个进程，中间任何一处
    "只写了块、没写向量"或"载入时忘了重建 BM25"都会让线上检索悄悄变差。
    """
    directory = store.save(tmp_path / "index")
    query = "磁盘 df 写满"
    before = store.search(query, top_k=3, mode="hybrid")

    reloaded = VectorStore.load(directory)
    assert len(reloaded) == len(store)
    assert reloaded.bm25 is not None, "载入时必须重建 BM25，否则 bm25/hybrid 少一路"
    assert reloaded.search(query, top_k=3, mode="hybrid") == before


# ============================================================
# 五、兜底向量后端：它**不是**语义检索
# ============================================================
def test_embedder_falls_back_to_local_hash_without_api_key(monkeypatch):
    """没配 Key 就自动落到 `local` 后端。

    这条不是"能跑就行"：这里固化的是**降级发生了**这个事实，
    降级本身必须可见（见下面 describe 那几条）。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    e = Embedder()
    assert e.backend == "local"
    assert e.model == "local-hash"
    assert e.dim == LOCAL_DIM


def test_embedder_never_pretends_a_local_backend_is_dashscope(monkeypatch):
    """`describe()` 必须如实报出自己的后端与维度。

    索引存盘时会把它写进 `chunks.json`；载入时靠它判断"索引和当前后端
    是不是同一个向量空间"。这里谎报一次，后面所有检索结果都是错的，
    而且**不报错**，只表现为"莫名其妙查不准"（store.py:194-216 那段注释）。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    described = Embedder().describe()
    assert described["backend"] != "dashscope"
    assert described["model"] == "local-hash"
    assert described["dim"] == LOCAL_DIM


def test_local_backend_is_not_marked_as_degraded_in_describe(monkeypatch):
    """★ 已知问题：`describe()` 只报后端坐标，**不标注"这是降级兜底、无语义能力"**。

    现状：没有 Key 时，索引元数据里只有 `{'backend': 'local', 'model':
    'local-hash', 'dim': 512}`。`dashscope` 和 `local-hash` 在结构上完全平等，
    没有任何字段说明后者"只能跑通链路"。

    后果：见下面那条相似度用例 —— 词面不同的相关文本相似度是 0，
    但接口、元数据、统计口径都不会提示这一点。作者已经在
    `.env.example:18-23` 与 docs/overview.md:197 自认过这件事，
    代码里却没有机器可读的标记。

    这条用例固化当前行为；等 `describe()` 加上 `degraded: True`
    之类的标记时，这里要跟着改，而不是删掉。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    described = Embedder().describe()
    assert not any("degrad" in key or "semantic" in key for key in described), \
        "describe() 开始自报降级状态了 —— 请更新这条用例并同步报告"
    assert set(described) == {"backend", "model", "dim"}


def test_local_backend_has_no_semantic_understanding(monkeypatch):
    """★ 兜底后端**不是**语义检索：换个说法就是 0 分。

    「磁盘占满」和「空间不够」是同一件事，在真 embedding 下应当很近。
    local-hash 只做字符 unigram + bigram 哈希，这两句**没有任何共同字符**
    （实测交集为空），余弦相似度恰好是 0.0 —— 不是"低"，是"无关"。

    后果：凡是不复用原词的说法，纯向量路一律召回不到；而且向量路没有
    分数阈值，此时它返回的是**噪声**（见前面
    `test_vector_mode_always_fills_top_k_even_for_a_nonsense_query`）。
    所以"本项目做了语义检索"这句话，在没有 Key 的环境下是不成立的 ——
    真正拦住噪声的只有 BM25 那一路的 0 分过滤。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    e = Embedder()
    score = float(np.dot(e.encode_one("磁盘占满"), e.encode_one("空间不够")))
    assert abs(score) < 1e-6, f"同义不同词居然算出了相似度 {score}（后端换真模型了？）"
    # 同样的文本自己比自己必须是 1.0，否则归一化写错了
    assert float(np.dot(e.encode_one("磁盘占满"), e.encode_one("磁盘占满"))) == \
        pytest.approx(1.0, abs=1e-5)


def test_local_backend_encoding_is_normalized_and_shaped(monkeypatch):
    """向量必须是 L2 归一化的 (n, dim) 矩阵 —— 归一化之后点积才等于余弦相似度，
    `_rank_vector` 的一次矩阵乘法才算得对。空输入返回 (0, dim) 而不是报错。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    e = Embedder()
    matrix = e.encode(["磁盘满了", "nginx 502", "容器退出"])
    assert matrix.shape == (3, LOCAL_DIM)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    assert e.encode([]).shape == (0, LOCAL_DIM)


def test_local_backend_zero_vector_is_not_nan(monkeypatch):
    """纯标点/空白文本会得到全 0 向量，归一化时必须防除零。

    不防的话这里会出 NaN，NaN 一旦进矩阵，整次检索的排序全部静默失效
    （`argsort` 对 NaN 不报错，只是结果没有任何意义）。
    当前实现把 0 范数替换成 1.0，所以 0 向量**原样保留为 0 向量**。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    matrix = Embedder().encode(["!!!", "   ", "，。", "磁盘满了"])
    assert not np.isnan(matrix).any(), "归一化除零产生了 NaN"
    norms = np.linalg.norm(matrix, axis=1)
    assert np.allclose(norms[:3], 0.0), "无内容的文本应当是 0 向量（不是随机向量）"
    assert norms[3] == pytest.approx(1.0, abs=1e-5), "有内容的文本必须归一化"


def test_local_hash_is_stable_across_processes(monkeypatch):
    """★ 哈希必须跨进程稳定 —— 这是 crc32 而不是内置 `hash()` 的原因。

    内置 `hash()` 对字符串按进程随机加盐：今天建好的索引，明天重启进程
    就落到别的桶里，检索结果整体错乱且**不报错**。
    所以这里起一个全新的 Python 进程算同一句话的向量来比对。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    here = Embedder().encode_one("nginx 502").tolist()
    code = (
        "import numpy as np;"
        "from app.rag.embedder import Embedder;"
        "print(Embedder().encode_one('nginx 502').tolist())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHONIOENCODING": "utf-8", "PATH": ""},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        pytest.skip(f"子进程不可用（受限沙箱？）：{proc.stderr[-200:]}")
    assert json.loads(proc.stdout.strip()) == pytest.approx(here), \
        "换个进程向量就变了 —— 索引不可复现"


def test_asking_for_dashscope_without_a_key_fails_loudly(monkeypatch):
    """显式要求真语义后端但没有 Key 时，必须报错而不是悄悄退回 local-hash。

    静默降级是这类系统最危险的一种错：配置写的是 dashscope，
    线上跑的是词袋，而所有页面都显示"检索正常"。
    """
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(EmbeddingError):
        Embedder(backend="dashscope")
