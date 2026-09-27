# -*- coding: utf-8 -*-
"""
RAG 端到端评测 + 报告生成
============================================================
把「这个 RAG 到底做得怎么样」变成一份可复现、可对账的报告。

【和第 3 天的召回率评测是什么关系】

    第 3 天测的是**检索层**：标准文档有没有进 top_k（上游指标）
    本脚本测的是**端到端**：最终那段回答到底靠不靠谱

两层都要有。因为「检索 100% 命中」和「答案是对的」是两件事：
检索漏了 → 答案必错；检索对了 → 答案仍可能跑题、编造，
或者**对着知识库里根本没有的问题一本正经地硬答**。

【四个规则维度 + 一个模型维度】

    检索命中   上游指标，用来做「分层归因」
    引用可信   引用编号有没有越界（一条规则就能守住 RAG 最大的卖点）
    数值忠实   答案里的数字在不在资料里
    拒答正确   ★ 库外问题该不该拒答 —— 这是 RAG 相对裸模型的核心价值
    相关性 / 忠实度   模型判分（1-5）

【★ 裸模型对照是这份报告的灵魂】

同样 40 条问题，再跑一遍「不给任何参考资料」的裸模型。
对照组才能回答那个真正的问题：**加了 RAG 到底多得到了什么？**

预期是：
    · 库外问题：裸模型几乎 0 拒答，RAG 应该接近全拒答
      → 这是"知识边界"的价值
    · 库内问题：裸模型答得可能也不差，但它**没有引用**，
      而且**忠实度根本没法测**（没有资料可对照）
      → 这是"可审计"的价值

用法：
    python -m scripts.run_eval                 # 全量 40 条 × 2 模式
    python -m scripts.run_eval --limit 5       # 只跑前 5 条（先试水，很便宜）
    python -m scripts.run_eval --no-baseline   # 跳过对照组
    python -m scripts.run_eval --skip-verify   # 跳过尺子校验（不推荐）
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation import judges                      # noqa: E402
from app.llm import ModelError, chat                    # noqa: E402
from app.observability import tracer                    # noqa: E402
from app.rag import pipeline                            # noqa: E402

SET_PATH = PROJECT_ROOT / "eval" / "rag_eval_set.json"
REPORT_DIR = PROJECT_ROOT / "eval" / "reports"

# 检查点文件。跑一次全量评测要 160 次模型调用、十几分钟 ——
# 中途网络抖一下、进程被杀一次，全部结果就没了，得从头再来一遍。
# 所以每跑完一条就把结果落盘，`--resume` 可以接着跑。
#
# ★ 这和 Day 7 处置 Agent 的教训是同一条：
#   **重试要带上上一轮的状态，否则不叫重试，叫重来一遍。**
CRASH_LOG = REPORT_DIR / ".partial.json"
# 上下文片段很大且只在跑的那一瞬间有用，落盘时丢掉
_KEEP = ("error", "answer", "hits", "elapsed_ms", "retrieval",
         "abstain", "citations", "numbers", "judge")

# 裸模型对照用的提问方式。
# 刻意保持中立：不加"请凭你的知识回答"这类引导，
# 因为我们要测的是**默认行为** —— 不给资料时它会不会自己划边界。
BASELINE_SYSTEM = "你是一个运维助手。请回答用户的问题。"


def _slim(rec: dict) -> dict:
    """检查点存精简体 —— 去掉内容最大的 context 字段。"""
    return {k: rec.get(k) for k in _KEEP}


def _load_partial() -> dict:
    if not CRASH_LOG.exists():
        return {}
    try:
        return json.loads(CRASH_LOG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_partial(store: dict) -> None:
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        CRASH_LOG.write_text(json.dumps(store, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        pass          # 检查点写失败不该影响评测本身


def _progress(msg: str) -> None:
    # flush=True 很重要：输出被重定向到文件或管道时，
    # Python 会改成块缓冲 —— 跑 20 分钟一条都看不见，
    # 而且进程一挂，缓冲区里的内容全部丢失。
    print(msg, flush=True)


# ============================================================
# 一、跑一条用例
# ============================================================
def run_rag(case: dict) -> dict:
    """RAG 模式：检索 → 生成 → 判定。"""
    t0 = time.time()
    try:
        out = pipeline.answer(case["question"], top_k=5, mode="hybrid")
    except ModelError as e:
        return {"error": f"{type(e).__name__}: {e}", "answer": "",
                "context": "", "hits": [], "elapsed_ms": 0}

    hits = out["hits"]
    # ★ context 必须**逐字复刻** Prompt 里给模型看的内容 —— 包括来源和标题。
    #
    #   第一版只拼了片段正文，于是文档标题里的「502」被判成
    #   "答案里凭空出现的数字"（r03 / r22 / r30 三条都是这么误报的）。
    #   模型明明在 Prompt 里看到了「[1] 来源：nginx-502.md — Nginx 502 ...」，
    #   判定层却认为它没看到。
    #
    #   **判定层的「资料」必须等于模型眼里的「资料」，差一个字都会造成误报。**
    #   所以这里直接照抄 `pipeline.build_rag_messages` 的拼装格式 ——
    #   而不是"另写一份差不多"的，那种迟早会不一致。
    context = "\n\n".join(
        f"[{h['rank']}] 来源：{h['source']} — {h.get('title') or ''}\n"
        f"{h.get('text') or ''}"
        for h in hits)

    rec = {
        "error": None,
        "answer": out["answer"],
        "context": context,
        "hits": [{"rank": h["rank"], "source": h["source"]} for h in hits],
        "elapsed_ms": int((time.time() - t0) * 1000),
        "retrieval": judges.check_retrieval(case.get("doc"), hits),
        "abstain": judges.check_abstention(out["answer"]),
    }
    # 引用检查只对"确实作答了"的回答有意义 ——
    # 拒答的回答本来就不该有引用，不该因此被判失败。
    rec["citations"] = (judges.check_citations(out["answer"], len(hits))
                        if not rec["abstain"]["abstained"] else
                        {"cited": [], "invalid": [], "has_citation": False,
                         "ok": None})
    rec["numbers"] = judges.check_numbers(out["answer"], context)
    rec["judge"] = judges.judge_answer(case["question"], context, out["answer"])
    return rec


def run_baseline(case: dict) -> dict:
    """对照组：不给任何参考资料，直接问模型。"""
    t0 = time.time()
    messages = [{"role": "system", "content": BASELINE_SYSTEM},
                {"role": "user", "content": case["question"]}]
    try:
        text = chat(messages, temperature=0)
    except ModelError as e:
        return {"error": f"{type(e).__name__}: {e}", "answer": "",
                "elapsed_ms": 0}

    rec = {
        "error": None,
        "answer": text,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "abstain": judges.check_abstention(text),
        # 对照组没有资料，忠实度**在原理上就无法测量** ——
        # 这不是"没跑到"，是"没有参照物"。
        # 这件事本身就是 RAG 的价值之一，报告里会写明。
        "judge": judges.judge_answer(case["question"], "", text),
    }
    return rec


# ============================================================
# 二、聚合
# ============================================================
def _mean(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.fmean(vals), 2) if vals else None


def _rate(flags):
    flags = [f for f in flags if f is not None]
    return round(sum(1 for f in flags if f) / len(flags), 4) if flags else None


def _pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def aggregate(cases: list, results: dict) -> dict:
    """把逐条结果汇总成指标。

    【★ 分母怎么取，决定了指标是"有意义的"还是"反向的"】

    第一版把所有题都塞进 relevance 均值，跑出来 3.67 分 —— 看起来偏低的。
    但拆开看：库内题全是 5 分，**是库外题的拒答回答把均分拉低的**。
    而"拒答"的 relevance 天然就低（它确实没回答问题），
    恰恰是**正确行为**。

    也就是说那个口径下：**拒答做得越好，相关性分数越低。指标方向是反的。**

    所以分母必须拆开：
        relevance / faithfulness  只统计**知识库内的题**
                                  → 库内却拒答会被自然惩罚，库外拒答不被惩罚
        拒答率                     只在**知识库外的题**上看
    """
    modes = [m for m in ("rag", "baseline") if m in results]
    agg = {"modes": {}, "total": len(cases)}

    for mode in modes:
        rows = results[mode]
        ok = [(c, r) for c, r in zip(cases, rows) if not r.get("error")]
        in_ok = [(c, r) for c, r in ok if c["scope"] == "in"]
        out_ok = [(c, r) for c, r in ok if c["scope"] == "out"]
        in_scope = [(c, r) for c, r in zip(cases, rows) if c["scope"] == "in"]
        out_scope = [(c, r) for c, r in zip(cases, rows) if c["scope"] == "out"]

        m = {
            "runs": len(rows),
            "errors": sum(1 for r in rows if r.get("error")),
            # ★ 只在库内题上算分 —— 理由见函数开头
            "relevance": _mean([r["judge"].get("relevance") for _, r in in_ok]),
            # 对照组没有资料，judge 返回 faithfulness=None，
            # _mean 会自动过滤掉 → 结果是 None → 报告里显示「—」
            "faithfulness": _mean([r["judge"].get("faithfulness")
                                   for _, r in in_ok]),
            "judged": sum(1 for _, r in in_ok
                          if r["judge"].get("relevance") is not None),
            "in_scope_answer_rate": _rate([not r["abstain"]["abstained"]
                                           for _, r in in_scope
                                           if not r.get("error")]),
            "out_scope_abstain_rate": _rate([r["abstain"]["abstained"]
                                             for _, r in out_scope
                                             if not r.get("error")]),
            "elapsed_ms_avg": _mean([r.get("elapsed_ms") for _, r in ok]),
        }
        if mode == "rag":
            m["retrieval_hit_rate"] = _rate(
                [r["retrieval"]["hit"] for _, r in in_ok
                 if r["retrieval"]["applicable"]])
            m["citation_ok_rate"] = _rate([r["citations"]["ok"]
                                           for _, r in ok
                                           if r["citations"].get("ok") is not None])
            m["citation_invalid_count"] = sum(
                len(r["citations"].get("invalid") or []) for _, r in ok)
            m["numbers_clean_rate"] = _rate([r["numbers"]["clean"]
                                             for _, r in ok])
            m["numbers_unsourced_total"] = sum(
                len(r["numbers"]["unsourced"]) for _, r in in_ok)
        agg["modes"][mode] = m

    # ---- 按题型分层：词面型 / 语义型 / 库外 ----
    #
    # 题型混在一起算平均，会把差距抹平：词面型问题好答，语义型问题难答，
    # 只看总数你不知道该优化哪里。分开看才能回答
    # 「检索到底在语义上够不够用」这种真问题。
    rag_rows = results.get("rag") or []
    by_cat = {}
    for cat in ("word", "semantic", "out"):
        rows_c = [(c, r) for c, r in zip(cases, rag_rows)
                  if c["category"] == cat and not r.get("error")]
        if not rows_c:
            continue
        is_out = cat == "out"
        by_cat[cat] = {
            "n": len(rows_c),
            # 库外题不答，所以不算相关性和忠实度（理由同总表）
            "relevance": None if is_out else _mean(
                [r["judge"].get("relevance") for _, r in rows_c]),
            "faithfulness": None if is_out else _mean(
                [r["judge"].get("faithfulness") for _, r in rows_c]),
            "retrieval_hit_rate": _rate(
                [r["retrieval"]["hit"] for _, r in rows_c
                 if r["retrieval"]["applicable"]]),
            "abstain_rate": _rate([r["abstain"]["abstained"] for _, r in rows_c]),
        }
    agg["by_category"] = by_cat

    # ---- 分层归因：检索命中 vs 未命中，答案质量差多少 ----
    rag_rows = results.get("rag")
    if rag_rows:
        hit = [(c, r) for c, r in zip(cases, rag_rows)
               if c["scope"] == "in" and not r.get("error")
               and r["retrieval"].get("hit") is True]
        miss = [(c, r) for c, r in zip(cases, rag_rows)
                if c["scope"] == "in" and not r.get("error")
                and r["retrieval"].get("hit") is False]
        agg["layered"] = {
            "hit": {"n": len(hit),
                    "relevance": _mean([r["judge"].get("relevance")
                                        for _, r in hit]),
                    "faithfulness": _mean([r["judge"].get("faithfulness")
                                           for _, r in hit])},
            "miss": {"n": len(miss),
                     "relevance": _mean([r["judge"].get("relevance")
                                         for _, r in miss]),
                     "faithfulness": _mean([r["judge"].get("faithfulness")
                                            for _, r in miss])},
        }
    return agg


# ============================================================
# 三、报告
# ============================================================
def _table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _clip(text, n=200) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def build_report(cases, results, agg, meta) -> str:
    rag = agg["modes"].get("rag") or {}
    base = agg["modes"].get("baseline") or {}
    L = []

    L.append("# RAG 端到端评测报告")
    L.append("")
    L.append(f"- 生成时间：{meta['ts']}")
    L.append(f"- 评测集：`eval/rag_eval_set.json`　共 **{agg['total']} 条**"
             f"（知识库内 {meta['n_in']} 条 / 知识库外 {meta['n_out']} 条）")
    L.append(f"- 模式：RAG（混合检索 top-5 + 生成）"
             + ("　vs　裸模型对照（不给任何资料）" if base else "　（未跑对照组）"))
    L.append(f"- 耗时：{meta['elapsed_s']:.1f}s　|　"
             f"本次评测成本：¥{meta['cost_cny']:.4f}")
    L.append(f"- 命令：`python -m scripts.run_eval"
             + (f" --limit {meta['limit']}" if meta.get("limit") else "") + "`")
    L.append("")

    # ---------------- 〇、尺子校验 ----------------
    L.append("## 〇、先验尺子：判定器自己通过测试了吗")
    L.append("")
    L.append("**评测报告的可信度上限 = 判定器的可信度。** "
             "如果判定器分不出「好答案」和「编得一本正经的答案」，"
             "那么下面所有分数都只是数字，不代表任何东西。")
    L.append("")
    v = meta["judge_verify"]
    if v is None:
        L.append("> ⚠️ 本次跳过了尺子校验（`--skip-verify`），"
                 "下面的判定结果请谨慎采用。")
    else:
        L.append(_table(["构造样本", "期望", "实际判定", "结论"],
                        [[c["name"], "见下", c["detail"],
                          "✅ 判对" if c["ok"] else "❌ 判错"]
                         for c in v["cases"]]))
        L.append("")
        L.append(f"**{v['passed']}/{v['total']} 通过。** "
                 + ("判定器可用，下面的数字有意义。" if v["ok"]
                    else "⚠️ **判定器未能通过全部测试，本报告不可信。**"))
    L.append("")

    # ---------------- 一、总览 ----------------
    L.append("## 一、总览：RAG 与裸模型对照")
    L.append("")
    headers = ["指标", "RAG", "裸模型对照", "说明"]
    if not base:
        headers = ["指标", "RAG", "说明"]
    rows = []

    def add(label, rv, bv, note):
        rv = "—" if rv is None else rv
        if base:
            rows.append([label, rv, "—" if bv is None else bv, note])
        else:
            rows.append([label, rv, note])

    add("答案相关性均分（1-5）", rag.get("relevance"), base.get("relevance"),
        "★ 只统计库内题：库外拒答分低是正常的")
    add("答案忠实度均分（1-5）", rag.get("faithfulness"), base.get("faithfulness"),
        "★ 只统计库内题；对照组无资料 → 无法测量")
    add("**库外问题拒答率**", _pct(rag.get("out_scope_abstain_rate")),
        _pct(base.get("out_scope_abstain_rate")),
        "★ 知识库里没有的问题，会不会硬答")
    add("库内问题作答率", _pct(rag.get("in_scope_answer_rate")),
        _pct(base.get("in_scope_answer_rate")),
        "有资料的问题，有没有被误拒答")
    add("引用编号越界次数", rag.get("citation_invalid_count"), None,
        "答案引用了不存在的资料编号")
    if base:
        rows[-1] = [rows[-1][0], rows[-1][1], "不适用", rows[-1][3]]

    L.append(_table(headers, rows))
    L.append("")
    L.append("> **口径说明（很重要，不然会误读上面的表）**")
    L.append(">")
    L.append("> - **相关性 / 忠实度只统计知识库内的题。** "
             "库外题的正确行为是「拒答」，而拒答的回答在相关性上天然是低分 —— "
             "把它算进均分，就会得出「拒答做得越好、相关性分数越低」这种反向结论。")
    L.append("> - **忠实度不由规则算，而由模型判。** "
             "它衡量的是「有没有守住资料边界」，不是「说得对不对」。")
    L.append("> - 每条用例的原始回答与逐项判定，都在同目录的 `.json` 明细文件里。")
    L.append("")

    if base:
        L.append("> **忠实度为什么对照组是「—」？** "
                 "因为没有给资料，就没有可对照的参照物 —— "
                 "**判断一段话有没有超出资料范围，前提是先有资料。** "
                 "所以对照组不是「忠实度很低」，而是**这个指标不存在**，"
                 "报告里如实显示为不分。"
                 "这正是 RAG 相对裸模型的一项核心价值："
                 "它让「有没有编」这件事第一次变成可测量的。")
        L.append("")

    # ---------------- 二、按题型分层 ----------------
    cat = agg.get("by_category") or {}
    if cat:
        L.append("## 二、按题型分层：难在哪一类问题上")
        L.append("")
        label = {"word": "词面型（用文档原词提问）",
                 "semantic": "语义型（换一套词表达同一意思）",
                 "out": "库外（知识库里根本没有）"}
        rows_c = []
        for k in ("word", "semantic", "out"):
            e = cat.get(k)
            if not e:
                continue
            rows_c.append([
                label[k], e["n"],
                _pct(e["retrieval_hit_rate"]),
                "—" if e["relevance"] is None else e["relevance"],
                "—" if e["faithfulness"] is None else e["faithfulness"],
                _pct(e["abstain_rate"]),
            ])
        L.append(_table(["题型", "用例数", "检索命中率", "相关性", "忠实度",
                         "拒答率"], rows_c))
        L.append("")
        w, sm = cat.get("word") or {}, cat.get("semantic") or {}
        if w.get("retrieval_hit_rate") is not None and \
                sm.get("retrieval_hit_rate") is not None:
            gap = w["retrieval_hit_rate"] - sm["retrieval_hit_rate"]
            if gap > 0.01:
                L.append(f"> **词面型命中 {_pct(w['retrieval_hit_rate'])}、"
                         f"语义型 {_pct(sm['retrieval_hit_rate'])}，差 "
                         f"{gap * 100:.1f} 个百分点。** "
                         "这个差值就是「检索器有没有真正的语义能力」的量化体现 —— "
                         "当前后端是本地哈希词袋兜底，本质上还是词面匹配，"
                         "所以语义型问题明显更弱。"
                         "换成真正的 embedding 模型（百炼 text-embedding-v3 或本地 BGE-M3）"
                         "后重跑，这个差值应该收窄。**这是本项目目前最大的技术债。**")
            else:
                L.append(f"> 词面型与语义型命中率持平（都是 "
                         f"{_pct(w['retrieval_hit_rate'])}）。"
                         "注意本次语义型样本只有 "
                         f"{sm.get('n', 0)} 条，样本偏小，"
                         "结论只能当参考。")
            L.append("")

    # ---------------- 二、分层归因 ----------------
    lay = agg.get("layered")
    if lay and lay.get("hit", {}).get("n"):
        L.append("## 三、检索分层归因：答案不好，是检索的锅还是生成的锅")
        L.append("")
        L.append(_table(
            ["分层", "用例数", "相关性均分", "忠实度均分"],
            [["检索命中", lay["hit"]["n"], lay["hit"]["relevance"],
              lay["hit"]["faithfulness"]],
             ["检索未命中", lay["miss"]["n"], lay["miss"]["relevance"],
              lay["miss"]["faithfulness"]]]))
        L.append("")
        if lay["miss"]["n"] == 0:
            L.append(f"检索命中率 **{_pct(rag.get('retrieval_hit_rate'))}**"
                     f"（库内 {meta['n_in']} 条全部命中），"
                     "所以分层归因暂时看不出差距 —— 这是个好结果，"
                     "它意味着至少没有「检索拖后腿」的情况。")
        else:
            L.append("两层的差值就是**上游检索对最终答案的影响**。"
                     "差值越大，说明越该先去优化检索而不是调提示词。")
        L.append("")

    # ---------------- 三、拒答能力 ----------------
    L.append("## 四、拒答能力：本报告最该看的一节")
    L.append("")
    L.append(f"知识库外的问题 **{meta['n_out']} 条**，"
             "知识库里完全没有相关内容。正确的行为是明确告诉用户「没有」。")
    L.append("")
    L.append("**为什么这一项最重要**：检索永远会返回 top-5 结果 —— 哪怕全都不相关。"
             "所以「知识库里没有」这件事，模型没法靠「检索结果为空」得知，"
             "只能靠「读到的东西答不了这个问题」来判断。"
             "绝大多数人做的 RAG 从不测这一项，"
             "结果上线后对着库外问题一本正经地编，"
             "而且因为不受知识库约束，**无法审计**。")
    L.append("")

    out_rows = []
    rag_rows = results.get("rag") or []
    base_rows = results.get("baseline") or []
    for i, c in enumerate(cases):
        if c["scope"] != "out":
            continue
        r = rag_rows[i] if i < len(rag_rows) else {}
        b = base_rows[i] if i < len(base_rows) else {}
        ra = r.get("abstain", {}).get("abstained")
        ba = b.get("abstain", {}).get("abstained")
        out_rows.append([
            c["id"],
            _clip(c["question"], 34),
            "✅ 拒答" if ra else "❌ 硬答",
            "—" if not base else ("✅ 拒答" if ba else "❌ 硬答"),
        ])
    headers3 = ["编号", "问题", "RAG", "裸模型"] if base else ["编号", "问题", "RAG"]
    L.append(_table(headers3, out_rows))
    L.append("")

    rag_missed = [c["id"] for i, c in enumerate(cases)
                  if c["scope"] == "out" and i < len(rag_rows)
                  and not rag_rows[i].get("abstain", {}).get("abstained")]
    if rag_missed:
        L.append(f"RAG 未拒答的 {len(rag_missed)} 条：`{'`、`'.join(rag_missed)}` —— "
                 "这些是**真实的知识边界漏洞**，值得逐条看原文"
                 "（见第 4 节失败样例）。")
    else:
        L.append("**RAG 全部拒答。** 知识边界守住了 —— "
                 "这意味着系统不会用知识库之外的内容回答用户。")
    L.append("")

    # ---------------- 四、失败样例 ----------------
    L.append("## 五、失败样例（带原文，用于定位问题）")
    L.append("")
    failures = []
    for i, c in enumerate(cases):
        r = rag_rows[i] if i < len(rag_rows) else {}
        if r.get("error"):
            failures.append((c, r, f"运行失败：{r['error']}"))
            continue
        reasons = []
        if c["scope"] == "in":
            if r["retrieval"].get("hit") is False:
                reasons.append(f"检索未命中（期望 {c['doc']}，"
                               f"实际命中 {r['retrieval']['got']}）")
            if r["abstain"]["abstained"]:
                reasons.append("明明有资料却拒答")
            if r["citations"].get("ok") is False:
                if r["citations"]["invalid"]:
                    reasons.append(f"引用了不存在的编号 {r['citations']['invalid']}")
                elif not r["citations"]["has_citation"]:
                    reasons.append("答案里一个引用编号都没有")
            if r["numbers"]["unsourced"]:
                reasons.append(f"数字无出处：{r['numbers']['unsourced']}")
            rel = r["judge"].get("relevance")
            if isinstance(rel, int) and rel <= 2:
                reasons.append(f"相关性仅 {rel} 分：{r['judge'].get('reason')}")
            fa = r["judge"].get("faithfulness")
            if isinstance(fa, int) and fa <= 2:
                reasons.append(f"忠实度仅 {fa} 分：{r['judge'].get('reason')}")
        else:
            if not r["abstain"]["abstained"]:
                reasons.append("★ 知识库外的问题没有拒答（硬答）")
        if reasons:
            failures.append((c, r, "；".join(reasons)))

    if not failures:
        L.append("**本次没有任何失败样例。**")
    else:
        L.append(f"共 {len(failures)} 条存在问题：")
        L.append("")
        for c, r, why in failures[:12]:
            L.append(f"### {c['id']}　{_clip(c['question'], 60)}")
            L.append("")
            L.append(f"- **问题**：{why}")
            L.append(f"- **预期**：{c['note']}")
            if c["scope"] == "in" and r.get("hits"):
                L.append(f"- **检索到**：{', '.join(h['source'] for h in r['hits'])}")
            L.append(f"- **回答摘录**：{_clip(r.get('answer'), 260)}")
            L.append("")
        if len(failures) > 12:
            L.append(f"（另有 {len(failures) - 12} 条未列出，"
                     "完整数据见同目录的 `.json` 明细文件）")
            L.append("")

    # ---------------- 五、结论 ----------------
    L.append("## 六、结论")
    L.append("")
    L.append(_conclusions(agg, meta))
    L.append("")

    return "\n".join(L)


def _conclusions(agg, meta) -> str:
    rag = agg["modes"].get("rag") or {}
    base = agg["modes"].get("baseline") or {}
    lines = []

    hit = rag.get("retrieval_hit_rate")
    if hit is not None:
        lines.append(f"1. **检索层**：库内问题 Top-5 召回率 **{_pct(hit)}**"
                     f"（{meta['n_in']} 条）。"
                     "检索是上游，它决定了答案的天花板。")

    rel, fa = rag.get("relevance"), rag.get("faithfulness")
    if rel is not None:
        lines.append(f"2. **生成层**（仅统计知识库内的 {meta['n_in']} 条题）："
                     f"答案相关性均分 **{rel}**、忠实度均分 **{fa}**（满分 5）。"
                     "忠实度衡量的是「有没有守住资料边界」，"
                     "不是「说得对不对」—— 这是 RAG 评测和普通问答评测的根本区别。")

    orr = rag.get("out_scope_abstain_rate")
    borr = base.get("out_scope_abstain_rate")
    if orr is not None:
        txt = (f"3. **知识边界**：库外问题拒答率 **{_pct(orr)}**"
               f"（{meta['n_out']} 条）。")
        if borr is not None:
            txt += (f" 同样的题交给裸模型，拒答率只有 **{_pct(borr)}** —— "
                    "**这个差值就是 RAG 在「不乱说」这件事上的可量化收益。**")
        lines.append(txt)

    if rag.get("citation_invalid_count") == 0 and rag.get("citation_ok_rate") is not None:
        lines.append(f"4. **可审计性**：引用编号越界 **0 次**，"
                     f"带有效引用的回答占 **{_pct(rag['citation_ok_rate'])}**"
                     "（仅统计确实作答的题）。"
                     "引用是 RAG 相对裸模型最大的卖点，它必须一条都不越界。")

    clean = rag.get("numbers_clean_rate")
    if clean is not None:
        lines.append(f"5. **数值忠实**：无出处数字为 0 的回答占 **{_pct(clean)}**，"
                     f"全部回答中未命中的数字共 {rag.get('numbers_unsourced_total', 0)} 个。"
                     "⚠️ 这条是**近似规则**：它认不出派生值（资料写「负载 8 / 4 核」，"
                     "答案写「每核 2」），子串匹配也会误判。"
                     "看趋势可以，别当精确指标用。")

    lines.append("")
    lines.append("**一句话总结**："
                 f"检索 {_pct(hit) if hit is not None else '—'}、"
                 f"相关性 {rel if rel is not None else '—'} 分、"
                 f"忠实度 {fa if fa is not None else '—'} 分、"
                 f"库外拒答 {_pct(orr) if orr is not None else '—'}。"
                 "**其中最能说明工程价值的是最后一项** —— 它证明这套系统"
                 "知道自己不知道什么，而这正是不做评测就看不见的东西。")
    return "\n".join(lines)


# ============================================================
# 四、主流程
# ============================================================
def load_cases(limit: int = None) -> list:
    data = json.loads(SET_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    if limit:
        # 抽样要保证库内库外都覆盖到，否则指标会失真
        head = [c for c in cases if c["scope"] == "in"][: max(1, limit - 2)]
        tail = [c for c in cases if c["scope"] == "out"][: min(2, limit)]
        cases = head + tail
    return cases


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RAG 端到端评测")
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑前 N 条（含 2 条库外题），用于试跑")
    ap.add_argument("--no-baseline", action="store_true",
                    help="跳过裸模型对照组（省钱，但报告少一半说服力）")
    ap.add_argument("--skip-verify", action="store_true",
                    help="跳过判定器自校验（不推荐）")
    ap.add_argument("--resume", action="store_true",
                    help="从检查点续跑（上次跑到一半断了时用，不会重复调用模型）")
    args = ap.parse_args(argv)

    cases = load_cases(args.limit)
    n_in = sum(1 for c in cases if c["scope"] == "in")
    n_out = len(cases) - n_in

    _progress("=" * 62)
    _progress("  RAG 端到端评测")
    _progress(f"  用例 {len(cases)} 条（库内 {n_in} / 库外 {n_out}）"
              f"　模式：RAG{' + 裸模型对照' if not args.no_baseline else ''}")
    _progress("=" * 62)

    verify = None
    if not args.skip_verify:
        _progress("")
        _progress("[0] 先验尺子 —— 判定器必须在正反例上都判对")
        verify = judges.verify_judge(verbose=False)
        for c in verify["cases"]:
            _progress(f"  {'OK ' if c['ok'] else 'FAIL'} {c['name']}")
            _progress(f"       {c['detail']}")
        _progress(f"  → {verify['passed']}/{verify['total']} 通过")
        if not verify["ok"]:
            _progress("  ⚠️ 判定器未通过全部测试，跑出来的分数不可信，建议先修判定器。")


    results = {"rag": []}
    if not args.no_baseline:
        results["baseline"] = []

    partial = _load_partial() if args.resume else {}
    if partial:
        done = sum(len(v) for v in partial.values())
        if done:
            _progress(f"\n  从检查点恢复：已有 {done} 条结果，跳过它们")

    started = time.time()
    total = len(cases) * len(results)
    done_n = 0
    with tracer.trace("eval-rag", question=f"{len(cases)} 条") as tid:
        for mode in results:
            store = partial.setdefault(mode, {})
            label = "RAG" if mode == "rag" else "裸模型（对照）"
            _progress(f"\n[{label}] 共 {len(cases)} 条")
            runner = run_rag if mode == "rag" else run_baseline
            for i, c in enumerate(cases, 1):
                cached = store.get(c["id"])
                if cached is not None:
                    # 检查点命中：不重复调用模型。context 没存，补个空串。
                    rec = dict(cached)
                    rec.setdefault("context", "")
                    results[mode].append(rec)
                    done_n += 1
                    continue

                # ★ 单条用例的异常必须被隔离。
                #   评测要跑 160 次模型调用、十几分钟，中间任何一条
                #   出意外（网络异常、模型返回怪东西、检索索引被删）
                #   都不该让整轮作废 —— 记下这一条失败，继续跑完其余 39 条。
                #   报告里会写明几条失败，读者自己知道该信到什么程度。
                try:
                    rec = runner(c)
                except Exception as e:          # noqa: BLE001 —— 故意的
                    rec = {"error": f"{type(e).__name__}: {e}", "answer": "",
                           "hits": [], "elapsed_ms": 0,
                           "retrieval": {"applicable": False, "hit": None,
                                         "got": []},
                           "abstain": {"abstained": False, "matched": []},
                           "citations": {"cited": [], "invalid": [],
                                         "has_citation": False, "ok": None},
                           "numbers": {"claimed": [], "unsourced": [],
                                       "clean": True},
                           "judge": {"error": None, "relevance": None,
                                     "faithfulness": None, "reason": ""}}
                results[mode].append(rec)
                store[c["id"]] = _slim(rec)
                _save_partial(partial)
                done_n += 1

                flag = "✗" if rec.get("error") else "·"
                jud = rec.get("judge") or {}
                if c["scope"] == "out":
                    tail = ("拒答" if rec.get("abstain", {}).get("abstained")
                            else "★硬答")
                elif jud.get("relevance") is not None:
                    tail = f"rel {jud['relevance']} / fa {jud['faithfulness']}"
                else:
                    tail = rec.get("error", "")[:40]
                _progress(f"  {flag} [{done_n:3d}/{total}] {c['id']} "
                          f"{_clip(c['question'], 28)}　{tail}")

        # ★ trace 的汇总记录是在 with 块**退出时**才写的。
        #   第一版把读成本这段写在了 with 块里面，
        #   于是永远读到空记录 → 报告上"本次评测成本 ¥0.0000"。
        #   数字不报错、不异常，就是不对 —— 和 Day 8 那批静默 bug 同类。
    elapsed = time.time() - started
    detail = tracer.trace_detail(tid) or {}
    cost = float(detail.get("cost_cny") or 0.0)
    tokens = (detail.get("usage") or {}).get("total_tokens", 0)

    agg = aggregate(cases, results)
    meta = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_in": n_in, "n_out": n_out, "limit": args.limit,
        "elapsed_s": elapsed, "cost_cny": cost, "tokens": tokens,
        "judge_verify": verify,
    }
    report = build_report(cases, results, agg, meta)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = REPORT_DIR / f"rag_eval_{stamp}.md"
    json_path = REPORT_DIR / f"rag_eval_{stamp}.json"
    md_path.write_text(report, encoding="utf-8")
    json_path.write_text(json.dumps(
        {"meta": {k: v for k, v in meta.items() if k != "judge_verify"},
         "metrics": agg,
         "cases": [{"id": c["id"], "scope": c["scope"], "question": c["question"],
                    "rag_answer": (results["rag"][i].get("answer") or "")[:4000],
                    "rag_abstained": results["rag"][i].get("abstain", {}).get("abstained"),
                    "rag_judge": results["rag"][i].get("judge"),
                    "baseline_answer": ((results.get("baseline") or [{}] * len(cases))[i].get("answer") or "")[:4000],
                    "baseline_abstained": ((results.get("baseline") or [{}] * len(cases))[i].get("abstain") or {}).get("abstained"),
                    }
                   for i, c in enumerate(cases)]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    # 终端摘要
    _progress("\n" + "=" * 62)
    rag = agg["modes"]["rag"]
    base = agg["modes"].get("baseline") or {}
    _progress(f"  检索命中率      {_pct(rag.get('retrieval_hit_rate'))}")
    _progress(f"  答案相关性均分  {rag.get('relevance')} / 5")
    _progress(f"  答案忠实度均分  {rag.get('faithfulness')} / 5")
    _progress(f"  库外拒答率      {_pct(rag.get('out_scope_abstain_rate'))}"
              + (f"　（裸模型 {_pct(base.get('out_scope_abstain_rate'))}）"
                 if base else ""))
    _progress(f"  引用越界次数    {rag.get('citation_invalid_count')}")
    _progress(f"  运行失败        {rag.get('errors')} 条")
    _progress(f"  耗时 {elapsed:.1f}s　成本 ¥{cost:.4f}　token {tokens}")
    _progress("=" * 62)
    _progress(f"\n报告已写入：\n  {md_path}\n  {json_path}")

    # 跑完整了才删检查点 —— 中途失败时留着，下次 --resume 能接着跑
    if CRASH_LOG.exists():
        CRASH_LOG.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
