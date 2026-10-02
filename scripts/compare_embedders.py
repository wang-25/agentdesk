# -*- coding: utf-8 -*-
"""对比"本地哈希后端"与"真语义后端"的检索质量（M5d 的执行工具）。

【它回答的问题】
`app/rag/embedder.py` 早就支持两个后端，`auto` 模式按有没有
`DASHSCOPE_API_KEY` 自动切换。但"真语义到底好多少"一直**没有证据** ——
而这个项目的规矩是：**听起来更好的改动不算理由，要有数据**。

【为什么要单独建一份索引】
真语义后端是 1024 维、本地哈希是 512 维，两者的向量**不在同一个空间**里，
一份索引没法给两个后端共用（`VectorStore.load` 会直接拒绝，这是对的）。
所以这里把语义索引建到**另一个目录**（默认 `data/index-semantic`），
**绝不动**你正在用的 `data/index` —— 否则一次对比就把可用状态弄没了。

【怎么用】
    # 1. 先看现状（不需要 Key，零成本）
    .venv\\Scripts\\python.exe scripts/compare_embedders.py --local-only

    # 2. 在 .env 里配好 DASHSCOPE_API_KEY 之后，建语义索引并对比
    .venv\\Scripts\\python.exe scripts/compare_embedders.py --build

    # 3. 只想重建语义索引、不跑对比
    .venv\\Scripts\\python.exe scripts/compare_embedders.py --build --no-eval

注意：`--build` 会对 46 条之外的**全部知识库文档**做 embedding，
      会产生一次性的 API 费用（本仓库 10 篇文档 / 91 块，量很小）。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402  （Windows 中文控制台切 UTF-8）

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ★ 自己加载 .env：本项目加载它的是 app/llm.py、security.py 等（都在 import 时执行），
#   而这个脚本刻意只 import 检索层。不载的话会出现最误导人的那种输出：
#   "我明明配了 Key，它却说没配"。（notify_check.py 踩过同一个坑。）
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:                                   # pragma: no cover
    pass

LOCAL_INDEX = PROJECT_ROOT / "data" / "index"
SEMANTIC_INDEX = PROJECT_ROOT / "data" / "index-semantic"
REPORT_DIR = PROJECT_ROOT / "eval" / "reports"
MODES = ("vector", "bm25", "hybrid")


def has_key() -> bool:
    import os
    return bool((os.getenv("DASHSCOPE_API_KEY") or "").strip())


def _store_for(directory: Path, backend: str):
    """按**指定后端**载入某个索引目录。

    ★ 必须显式指定后端：`Embedder()` 的 auto 模式看到 Key 就会选 dashscope，
      于是"载入本地索引来对比"这件事会直接撞上维度不匹配。
      对比场景下"用哪个后端"是**调用方的意图**，不该让它去猜。
    """
    from app.rag.embedder import Embedder
    from app.rag.store import VectorStore

    return VectorStore.load(directory, embedder=Embedder(backend=backend))


def build_semantic_index(verbose: bool = True) -> dict:
    """把语义索引建到**独立目录**，不动现有索引。"""
    from app.rag import pipeline

    if not has_key():
        raise SystemExit(
            "没有 DASHSCOPE_API_KEY，建不了语义索引。\n"
            "  在 .env 里填上 DASHSCOPE_API_KEY=sk-… 再跑一次；\n"
            "  只想验证这套对比流程能跑通，用 --local-only（零成本、不需要 Key）。")
    if verbose:
        print(f"  正在构建语义索引 → {SEMANTIC_INDEX.relative_to(PROJECT_ROOT)}")
        print("  （这一步会调用百炼 embedding，产生一次性费用；"
              "现有 data/index 完全不受影响）")
    stats = pipeline.build_index(index_dir=SEMANTIC_INDEX, verbose=verbose)
    return stats


def _force_local_env() -> None:
    """把当前进程的 Key 拿掉，让 `Embedder()` 选中 local 后端。

    只在评测本地索引时用：否则 auto 模式会选 dashscope，载入本地索引时报维度不匹配。

    ★ 必须**成对使用**（见 `_with_local_env`）：早先这里只摘不还，
      结果同一个进程里随后评测语义后端时反而报"指定了 dashscope 但没有 Key" ——
      刚配好的 Key 被自己摘掉了。这类 bug 的形态是"配置明明是对的，工具说不对"。
    """
    import os
    os.environ.pop("DASHSCOPE_API_KEY", None)


def _with_local_env(fn):
    """临时摘掉 Key 跑 `fn`，跑完**恢复**（不留副作用）。"""
    import os

    saved = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        return fn()
    finally:
        if saved is not None:
            os.environ["DASHSCOPE_API_KEY"] = saved


def evaluate_backend(name: str, directory: Path, backend: str) -> dict:
    """跑一遍 46 条评测，返回 report（与门禁脚本用的是**同一份实现**）。"""
    from app.rag import pipeline

    store = _store_for(directory, backend)
    report = pipeline.evaluate(top_k=8, modes=MODES, verbose=False, store=store)
    report["backend_name"] = name
    report["index_dir"] = str(directory.relative_to(PROJECT_ROOT))
    return report


def _row(label: str, mode_data: dict) -> str:
    by_type = mode_data.get("by_type") or {}
    lat = mode_data.get("latency_ms") or {}
    return (f"  {label:22s} {mode_data['recall'] * 100:6.1f}%"
            f"{mode_data.get('accuracy_at_1', 0) * 100:8.1f}%"
            f"{mode_data.get('accuracy_at_3', 0) * 100:8.1f}%"
            f"{mode_data.get('mrr', 0):7.3f}"
            f"{by_type.get('lexical', {}).get('recall', 0) * 100:9.1f}%"
            f"{by_type.get('semantic', {}).get('recall', 0) * 100:9.1f}%"
            f"{by_type.get('semantic', {}).get('accuracy_at_1', 0) * 100:10.1f}%"
            f"{lat.get('p50', 0):9.2f}ms")


def print_report(reports: list) -> None:
    print("=" * 78)
    print("  检索后端对比（Top-8 召回率）")
    print("=" * 78)
    for report in reports:
        emb = report.get("embedder") or {}
        print(f"\n  【{report['backend_name']}】{emb.get('model')}"
              f"（{emb.get('dim')} 维）　索引：{report['index_dir']}")
        print(f"  {'检索模式':22s} {'recall@8':>8s}{'acc@1':>8s}{'acc@3':>8s}"
              f"{'MRR':>7s}{'词面型':>10s}{'语义型':>10s}{'语义acc@1':>11s}{'P50':>12s}")
        print("  " + "-" * 96)
        for mode, label in (("vector", "纯向量"), ("bm25", "纯关键词(BM25)"),
                            ("hybrid", "混合(向量+BM25+RRF)")):
            print(_row(label, report["modes"][mode]))

    if len(reports) < 2:
        print("\n  只有一份后端的数据 —— 对比需要两份（见文件头部的用法）。")
        return

    print("\n  " + "=" * 74)
    print("  结论")
    print("  " + "=" * 74)
    base, sem = reports[0], reports[1]
    print("  ★ 主要看 acc@1 与『语义列的 acc@1』：recall@8 在这份 10 篇文档的语料上"
          "几乎没有分辨力（随机猜也有五成以上）。")
    for mode, label in (("vector", "纯向量"), ("hybrid", "混合检索")):
        before_r = base["modes"][mode]["recall"] * 100
        after_r = sem["modes"][mode]["recall"] * 100
        before_1 = base["modes"][mode].get("accuracy_at_1", 0) * 100
        after_1 = sem["modes"][mode].get("accuracy_at_1", 0) * 100
        before_s = (base["modes"][mode].get("by_type", {})
                    .get("semantic", {}).get("accuracy_at_1", 0) * 100)
        after_s = (sem["modes"][mode].get("by_type", {})
                   .get("semantic", {}).get("accuracy_at_1", 0) * 100)
        print(f"  {label:10s} recall@8 {before_r:5.1f}% → {after_r:5.1f}%"
              f"（{after_r - before_r:+.1f}pp）")
        print(f"  {'':10s} acc@1    {before_1:5.1f}% → {after_1:5.1f}%"
              f"（{after_1 - before_1:+.1f}pp）")
        print(f"  {'':10s} 语义列 acc@1 {before_s:5.1f}% → {after_s:5.1f}%"
              f"（{after_s - before_s:+.1f}pp）　← 真语义该赢的就是这一行")
    print("\n  ★ 判断口径：真语义应当**明显赢在语义型那一列**。"
          "如果两列都没赢，那就如实写『没赢、不引入』——"
          "换后端是要花钱的，数据说了不算才引入。")


def write_report(reports: list) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"embedder-compare-{datetime.now():%Y%m%d_%H%M%S}.md"
    lines = ["# 检索后端对比（本地哈希 vs 真语义）", "",
             f"生成时间：{datetime.now().isoformat(timespec='seconds')}", "",
             "| 后端 | 模型 | 维度 | 索引 | 模式 | 总体 | 词面型 | 语义型 | 陷阱型 | P50(ms) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for report in reports:
        emb = report.get("embedder") or {}
        for mode in MODES:
            data = report["modes"][mode]
            by = data.get("by_type") or {}
            lines.append(
                f"| {report['backend_name']} | {emb.get('model')} | {emb.get('dim')} "
                f"| `{report['index_dir']}` | {mode} "
                f"| {data['recall'] * 100:.1f}% "
                f"| {by.get('lexical', {}).get('recall', 0) * 100:.1f}% "
                f"| {by.get('semantic', {}).get('recall', 0) * 100:.1f}% "
                f"| {by.get('trap', {}).get('recall', 0) * 100:.1f}% "
                f"| {(data.get('latency_ms') or {}).get('p50', 0):.2f} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="检索后端对比（本地哈希 vs 真语义）")
    parser.add_argument("--build", action="store_true",
                        help="先构建语义索引（需要 DASHSCOPE_API_KEY，会产生费用）")
    parser.add_argument("--no-eval", action="store_true", help="只构建，不评测")
    parser.add_argument("--local-only", action="store_true",
                        help="只评测本地后端（不需要 Key，用于验证流程）")
    args = parser.parse_args()

    reports = []

    if args.build:
        build_semantic_index()
        if args.no_eval:
            print("\n  已构建语义索引（未评测）。")
            return 0

    # ---- 本地后端（临时摘 Key → 跑 → 恢复；不能留副作用）----
    if LOCAL_INDEX.is_dir():
        reports.append(_with_local_env(
            lambda: evaluate_backend("本地哈希", LOCAL_INDEX, "local")))
    else:
        print(f"  ⚠️ 本地索引不存在：{LOCAL_INDEX}")

    if args.local_only:
        print_report(reports)
        if reports:
            print(f"\n  报告已写入：{write_report(reports).relative_to(PROJECT_ROOT)}")
        return 0

    # ---- 语义后端 ----
    if not has_key():
        print("\n  ⚠️ 没有 DASHSCOPE_API_KEY，只能给出本地后端的数据。")
        print("     配上 Key 后跑 --build 即可得到两份对比。")
        print_report(reports)
        return 0

    if not SEMANTIC_INDEX.is_dir():
        print(f"\n  ⚠️ 语义索引不存在：{SEMANTIC_INDEX} —— 先跑 --build 构建它。")
        print_report(reports)
        return 1

    reports.append(evaluate_backend("真语义", SEMANTIC_INDEX, "dashscope"))
    print_report(reports)
    print(f"\n  报告已写入：{write_report(reports).relative_to(PROJECT_ROOT)}")
    # 顺带把"混合检索未命中"的问题列出来 —— 那是下一步优化的输入
    for report in reports:
        misses = report["modes"]["hybrid"].get("misses") or []
        if misses:
            print(f"\n  【{report['backend_name']}】混合检索未命中 {len(misses)} 条：")
            for item in misses[:5]:
                print(f"    ✗ [{item['type']}] {item['question'][:34]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
