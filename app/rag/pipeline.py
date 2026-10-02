# -*- coding: utf-8 -*-
"""
RAG 全链路编排与评测
============================================================
把前面几步串起来，并提供三个能力：

    构建索引   build_index()
    检索提问   retrieve() / answer()
    召回评测   evaluate()

【也可以当命令行用】在 agentdesk 目录下：

    .venv\\Scripts\\python.exe -m app.rag.pipeline build
    .venv\\Scripts\\python.exe -m app.rag.pipeline eval
    .venv\\Scripts\\python.exe -m app.rag.pipeline ask "nginx 报 502 怎么排查"
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from app.llm import ModelError, PROJECT_ROOT, chat
from app.rag.embedder import Embedder
from app.rag.loader import chunk_documents, load_documents
from app.rag.store import VectorStore

KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"
INDEX_DIR = PROJECT_ROOT / "data" / "index"
QA_SET_PATH = PROJECT_ROOT / "eval" / "qa_set.json"


# ============================================================
# 一、构建索引
# ============================================================
def build_index(chunk_size: int = 400, overlap: int = 80,
                knowledge_dir=None, index_dir=None, verbose: bool = True) -> dict:
    """加载文档 → 切分 → 向量化 → 存盘。"""
    knowledge_dir = Path(knowledge_dir or KNOWLEDGE_DIR)
    index_dir = Path(index_dir or INDEX_DIR)

    docs = load_documents(knowledge_dir)
    if not docs:
        raise RuntimeError(f"知识库目录里没有可用的 .md/.txt 文件：{knowledge_dir}")

    chunks = chunk_documents(docs, chunk_size=chunk_size, overlap=overlap)

    embedder = Embedder()
    store = VectorStore(embedder).build(chunks)
    store.save(index_dir)

    stats = {
        "documents": len(docs),
        "chunks": len(chunks),
        "chunk_size": chunk_size,
        "overlap": overlap,
        "embedder": embedder.describe(),
        "index_dir": str(index_dir),
    }

    if verbose:
        print("=" * 62)
        print("索引构建完成")
        print("=" * 62)
        print(f"  文档数    : {stats['documents']}")
        print(f"  切块数    : {stats['chunks']}")
        print(f"  块大小    : {chunk_size} 字（重叠 {overlap} 字）")
        print(f"  向量后端  : {embedder.model}（{embedder.dim} 维）")
        print(f"  索引位置  : {index_dir}")
        if embedder.backend == "local":
            print()
            print("  ⚠️ 当前用的是本地兜底后端（哈希词袋），检索质量有限。")
            print("     在 .env 里配上 DASHSCOPE_API_KEY 后重建索引，效果会明显提升。")
        print()
        print("  各文档切块数：")
        per_doc = {}
        for c in chunks:
            per_doc[c.doc_id] = per_doc.get(c.doc_id, 0) + 1
        for doc_id, n in per_doc.items():
            print(f"    {doc_id:36s} {n} 块")

    return stats


# ============================================================
# 二、载入与检索
# ============================================================
_STORE = None


def load_store(index_dir=None, force: bool = False) -> VectorStore:
    """载入索引（进程内缓存，避免每次请求都读盘）。"""
    global _STORE
    if _STORE is not None and not force:
        return _STORE
    _STORE = VectorStore.load(Path(index_dir or INDEX_DIR))
    return _STORE


def retrieve(question: str, top_k: int = 5, mode: str = "hybrid") -> list:
    """检索相关片段。"""
    return load_store().search(question, top_k=top_k, mode=mode)


# ============================================================
# 三、RAG 问答（带引用溯源）
# ============================================================
RAG_SYSTEM_PROMPT = """你是一个运维知识助手。请严格依据【参考资料】回答问题。

规则：
1. 只使用参考资料里的内容，不要用你自己的知识补充，更不要编造
2. 如果参考资料里没有能回答问题的内容，直接说「知识库中没有相关内容」，不要硬答
3. 回答末尾用 [编号] 标注引用了哪几段资料，例如：[1][3]
4. 如果参考资料之间有冲突，指出冲突并说明
5. 回答用中文，结构清晰，可以直接给出操作步骤"""


def build_rag_messages(question: str, hits: list):
    """把检索结果拼成 RAG 的 messages。

    【为什么要标注编号并要求模型回引】
    这是「可验证」的关键。用户看到答案里写着 [2]，就能翻到第 2 条资料去核对 ——
    模型有没有编，一眼就能看出来。
    没有引用的 RAG 是个黑盒，出了问题没法定位是检索错了还是模型瞎说。
    """
    if not hits:
        context = "（没有检索到任何相关资料）"
    else:
        blocks = []
        for h in hits:
            blocks.append(f"[{h['rank']}] 来源：{h['source']} — {h['title']}\n{h['text']}")
        context = "\n\n".join(blocks)

    user = f"""【参考资料】
{context}

【问题】
{question}

请依据上面的参考资料回答。"""
    return [
        {"role": "system", "content": RAG_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def answer(question: str, top_k: int = 5, mode: str = "hybrid") -> dict:
    """完整 RAG 问答：检索 → 拼 Prompt → 生成 → 附引用。"""
    hits = retrieve(question, top_k=top_k, mode=mode)
    messages = build_rag_messages(question, hits)

    try:
        text = chat(messages, temperature=0)
    except ModelError as e:
        raise ModelError(f"生成失败：{e}") from e

    return {
        "question": question,
        "answer": text,
        "mode": mode,
        "citations": [
            {"no": h["rank"], "source": h["source"],
             "title": h["title"], "chunk_id": h["chunk_id"]}
            for h in hits
        ],
        "hits": hits,
    }


# ============================================================
# 四、召回率评测
# ============================================================
def load_qa_set(path=None) -> list:
    path = Path(path or QA_SET_PATH)
    if not path.exists():
        raise FileNotFoundError(f"评测集不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _percentile(ordered: list, pct: float) -> float:
    """已排序列表的百分位（最近秩法）。

    ★ 为什么不用 numpy.percentile：这里的样本只有几十条，
      而我们关心的是"这次改动有没有让 P95 明显变差"，
      最近秩法给出的就是**实际存在的那次耗时**，比插值出来的数字更好解释 ——
      "P95 = 12.3ms" 应该对应一次真实发生过的检索，而不是两个样本的平均。
      也省掉一次 numpy 依赖方向的纠结（它是运行时依赖，但检索层本可以更薄）。
    """
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return float(ordered[idx])


def evaluate(top_k: int = 3, modes=("vector", "bm25", "hybrid"),
             qa_set_path=None, verbose: bool = True, store=None) -> dict:
    """计算 Top-K 召回率，并对比三种检索模式 × 两类问题。

    参数 `store`：传了就评测**这一个** store，不传就用进程内缓存的那个。
    ★ 为什么要留这个口子：M5d 要对比"本地哈希后端"与"真语义后端"，
      而进程内的 store 是**单例缓存**（`load_store` 只认一个）。
      与其在对比脚本里把评测逻辑再抄一遍（两处实现必然漂移），
      不如让同一份实现接受一个 store —— 对比与门禁跑的**完全同一套代码**。

    【什么叫 Top-K 召回率】
    每个测试问题都标注了「标准答案出自哪篇文档」。
    如果检索返回的前 K 条里，至少有一条来自那篇文档，就算命中。
    命中数 / 总问题数 = 召回率。

    【为什么要分「词面型」和「语义型」两类问题】
    这是这个评测最关键的设计。

    词面型问题用了文档里的原词（比如直接问「502」「OOMKilled」），
    BM25 这类关键词检索就能做对 —— 因为词对上了。

    语义型问题刻意用**完全不同的词**表达同一个意思
    （比如问「前面的代理层返回了网关错误」，而不提 502 和 nginx）。
    这时关键词检索会失效，只有真正理解语义的向量模型才能命中。

    两类分开统计，才能看出「向量检索到底有没有在干活」——
    混在一起算平均，会被词面型问题掩盖掉全部差距。

    ★ 这张对比表回答了「RAG 效果怎么测」这个问题。
    """
    store = store if store is not None else load_store()
    qa_set = load_qa_set(qa_set_path)

    report = {"top_k": top_k, "total": len(qa_set),
              "embedder": store.embedder.describe(), "modes": {}}

    for mode in modes:
        hits = 0
        by_type = defaultdict(lambda: {"total": 0, "hits": 0})
        misses = []
        latencies = []

        for item in qa_set:
            qtype = item.get("type", "lexical")
            by_type[qtype]["total"] += 1

            # ★ 逐条计时：只测"检索本身"，不含后面的答案生成。
            #   这是为了让延迟门禁能定位到**检索层**的退化 ——
            #   混进模型调用的耗时里，就再也分不清"检索变慢了"还是"模型变慢了"。
            _t0 = time.perf_counter()
            results = store.search(item["question"], top_k=top_k, mode=mode)
            latencies.append((time.perf_counter() - _t0) * 1000)

            got_docs = {r["doc_id"] for r in results}

            if got_docs & set(item["expect_docs"]):
                hits += 1
                by_type[qtype]["hits"] += 1
            else:
                misses.append({
                    "question": item["question"],
                    "type": qtype,
                    "expected": item["expect_docs"],
                    "got": sorted(got_docs),
                })

        ordered = sorted(latencies)
        report["modes"][mode] = {
            "hits": hits,
            "recall": round(hits / len(qa_set), 4) if qa_set else 0.0,
            "latency_ms": {
                "p50": round(_percentile(ordered, 50), 3),
                "p95": round(_percentile(ordered, 95), 3),
                "mean": round(sum(ordered) / len(ordered), 3) if ordered else 0.0,
                "max": round(ordered[-1], 3) if ordered else 0.0,
            },
            "by_type": {
                t: {"total": v["total"], "hits": v["hits"],
                    "recall": round(v["hits"] / v["total"], 4) if v["total"] else 0.0}
                for t, v in by_type.items()
            },
            "misses": misses,
        }

    if verbose:
        embedder = report["embedder"]
        print("=" * 62)
        print(f"召回率评测（Top-{top_k}，共 {len(qa_set)} 个问题）")
        print("=" * 62)
        print(f"  向量后端：{embedder['model']}（{embedder['dim']} 维）")
        print()

        # 表头
        types = ["lexical", "semantic"]
        type_label = {"lexical": "词面型", "semantic": "语义型"}
        header = f"  {'检索模式':<26}{'总体':>8}"
        for t in types:
            header += f"{type_label.get(t, t):>9}"
        print(header)
        print("  " + "-" * 58)

        labels = {"vector": "纯向量检索", "bm25": "纯关键词(BM25)",
                  "hybrid": "混合检索(向量+BM25+RRF)"}
        for mode, data in report["modes"].items():
            row = f"  {labels.get(mode, mode):<26}{data['recall'] * 100:>7.1f}%"
            for t in types:
                bt = data["by_type"].get(t)
                row += f"{(bt['recall'] * 100):>8.1f}%" if bt else f"{'—':>9}"
            print(row)

        # 混合检索相比纯向量的提升 —— 这个结论可以直接写进项目说明
        if "vector" in report["modes"] and "hybrid" in report["modes"]:
            vec = report["modes"]["vector"]
            hyb = report["modes"]["hybrid"]
            print()
            delta = (hyb["recall"] - vec["recall"]) * 100
            if delta > 0:
                print(f"  → 混合检索比纯向量高 {delta:.1f} 个百分点")
            else:
                print("  → 本语料下混合检索与纯向量持平")

        # 语义型问题的差距最能说明问题
        v_sem = report["modes"].get("vector", {}).get("by_type", {}).get("semantic")
        b_sem = report["modes"].get("bm25", {}).get("by_type", {}).get("semantic")
        if v_sem and b_sem:
            if v_sem["recall"] > b_sem["recall"]:
                print(f"  → 语义型问题上，向量检索比关键词检索高 "
                      f"{(v_sem['recall'] - b_sem['recall']) * 100:.1f} 个百分点"
                      f"（这才是向量检索的价值所在）")
            else:
                print(f"  → 语义型问题上两者接近（当前向量后端能力有限，"
                      f"配真 embedding 后差距会拉开）")

        if embedder["backend"] == "local":
            print()
            print("  ⚠️ 当前向量后端是本地哈希词袋，不具备真正的语义能力，")
            print("     所以「语义型」这一列的差距还没体现出来。")
            print("     在 .env 配好 DASHSCOPE_API_KEY 后重建索引再看这张表。")

        misses = report["modes"].get("hybrid", {}).get("misses", [])
        if misses:
            print(f"\n  混合检索未命中的 {len(misses)} 个问题：")
            for m in misses:
                print(f"    ✗ [{m['type']}] {m['question'][:40]}")
                print(f"      期望 {m['expected']}，实际 {m['got']}")

    return report


# ============================================================
# 五、命令行入口
# ============================================================
def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m app.rag.pipeline",
        description="AgentDesk RAG 全链路工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="构建索引")
    p_build.add_argument("--chunk-size", type=int, default=400)
    p_build.add_argument("--overlap", type=int, default=80)

    p_eval = sub.add_parser("eval", help="跑召回率评测")
    p_eval.add_argument("--top-k", type=int, default=3)

    p_ask = sub.add_parser("ask", help="提问（带引用来源）")
    p_ask.add_argument("question")
    p_ask.add_argument("--top-k", type=int, default=5)
    p_ask.add_argument("--mode", default="hybrid",
                       choices=["vector", "bm25", "hybrid"])

    p_search = sub.add_parser("search", help="只检索，不生成")
    p_search.add_argument("question")
    p_search.add_argument("--top-k", type=int, default=5)
    p_search.add_argument("--mode", default="hybrid",
                          choices=["vector", "bm25", "hybrid"])

    args = parser.parse_args(argv)

    if args.command == "build":
        build_index(chunk_size=args.chunk_size, overlap=args.overlap)

    elif args.command == "eval":
        evaluate(top_k=args.top_k)

    elif args.command == "search":
        print("=" * 62)
        print(f"检索：{args.question}")
        print(f"模式：{args.mode}")
        print("=" * 62)
        for h in retrieve(args.question, top_k=args.top_k, mode=args.mode):
            print(f"\n[{h['rank']}] {h['source']} — {h['title']}")
            print(f"    得分 {h['score']}")
            print(f"    {h['preview']}")

    elif args.command == "ask":
        result = answer(args.question, top_k=args.top_k, mode=args.mode)
        print("=" * 62)
        print(f"问题：{result['question']}")
        print("=" * 62)
        print(result["answer"])
        print()
        print("-" * 62)
        print("引用来源：")
        for c in result["citations"]:
            print(f"  [{c['no']}] {c['source']} — {c['title']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    _main()
