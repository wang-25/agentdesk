# -*- coding: utf-8 -*-
"""
成本计算与聚合
============================================================
token → 钱。以及"钱都花在哪了"的聚合口径。

【为什么成本要单独一个文件】

因为它**一定会变**：模型单价会调、会换模型、会加缓存折扣价。
把价格埋在调用点里，改一次价格要全项目找一遍；
集中在一个字典里，改一处。**凡是"会随外部世界变化"的数字，
都应该有一个单独的、一眼能找到的家。**

★ 单价需要人工核实。写代码时的依据是 DeepSeek 公开价格
  （deepseek-chat：输入 ¥2/M、缓存命中 ¥0.5/M、输出 ¥8/M），
  但模型在变（曾见过请求 chat、返回 flash 的情况）。
  **所以这里的每个数字都标了"上次核对时间"，换模型时先来改这里。**
"""

import math
from datetime import datetime

# ============================================================
# 单价表：¥ / 1M tokens
# ============================================================
# 上次核对：2026-09-26（依据 api-docs.deepseek.com/quick_start/pricing）
# ★ 换模型 / 涨价时改这里，别去改调用代码。
PRICING = {
    "deepseek-chat": {
        "input": 2.0,             # 缓存未命中的输入
        "input_cache_hit": 0.5,   # 缓存命中的输入（DeepSeek 特有，打 2.5 折）
        "output": 8.0,
    },
    # 兜底：未知模型按这个价估，宁可粗也不要算不出钱
    "_default": {"input": 2.0, "input_cache_hit": 0.5, "output": 8.0},
}


def cost_of(usage: dict, model: str = "deepseek-chat") -> float:
    """按 usage 算一次调用的人民币成本。

    ★ 这里特意区分了**缓存命中**和**未命中**的输入 ——
      早就发现 DeepSeek 的 usage 里有 prompt_cache_hit_tokens /
      prompt_cache_miss_tokens 两个字段，命中部分是打折的。

      大多数人算成本只会 prompt_tokens × 单价，
      **把缓存折扣算进去，才是真的会算账。**
      而且缓存命中率本身就是一个可优化的指标：
      命中率低说明你的 system prompt 每次都在变（对缓存不友好）。

    「模型返回的是 flash、单价可能对不上」怎么办？
      保留：按请求的模型名查表，估个量级足够指导优化决策；
      精确到分需要按账单对账 —— 那是财务口径，不是工程口径。
    """
    if not isinstance(usage, dict):
        return 0.0
    price = PRICING.get(model) or PRICING["_default"]

    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        # 老格式没有 miss 字段：用总输入减命中
        miss = (usage.get("prompt_tokens") or 0) - hit
    output = usage.get("completion_tokens") or 0

    cost = (hit * price["input_cache_hit"]
            + max(0, miss) * price["input"]
            + output * price["output"]) / 1_000_000
    return cost


def aggregate(traces: list) -> dict:
    """把一组 trace 聚合成"这份钱花在哪了"的报告。

    ★ 这里的维度选择是刻意设计的。讲可观测时最有力的是这句：
      「我不只知道花了多少钱，我知道**每个环节**花了多少、
       哪一步最慢、哪一步在白花钱」—— 所以按 span 拆维度。
    """
    n = len(traces)
    if not n:
        return {"runs": 0}

    total_usage = {}
    total_cost = 0.0
    elapsed = []
    by_type = {}          # span 类型（llm/tool/agent）→ tokens/cost/次数
    by_name = {}          # 具体名字（check_disk / intent / diagnose）→ 同上
    errors = 0
    batch_runs = 0
    cache_hit = 0
    cache_miss = 0

    for t in traces:
        u = t.get("usage") or {}
        for k, v in u.items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v
        total_cost += t.get("cost_cny") or 0.0
        if t.get("elapsed_ms") is not None:
            # ★ 只把"单次请求"类 trace 计入延迟统计。
            #   批处理任务（评测跑一轮 315 秒）计入成本，但不计入延迟 ——
            #   混在一起 P95 会变成"哪个批处理任务跑了多久"。
            if t.get("batch"):
                batch_runs += 1
            else:
                elapsed.append(t["elapsed_ms"])
        if t.get("status") != "ok":
            errors += 1
        cache_hit += u.get("prompt_cache_hit_tokens") or 0
        cache_miss += u.get("prompt_cache_miss_tokens") or 0

        for sp in t.get("spans") or []:
            su = sp.get("usage") or {}
            # ★ token 只取 total_tokens 这一个字段。
            #   第一版把 usage 里所有数值字段全加了一遍 ——
            #   而 usage 里同时有 prompt_tokens / completion_tokens /
            #   total_tokens / prompt_cache_hit_tokens / prompt_cache_miss_tokens，
            #   求和等于把同一笔 token 算了四五遍，数字直接翻倍还多。
            #   **"求和"之前先问：这些字段是并列的，还是互相包含的？**
            tok = su.get("total_tokens")
            if tok is None:
                tok = (su.get("prompt_tokens") or 0) + (su.get("completion_tokens") or 0)
            c = cost_of(su)
            # ★ 这里有个调试了半天的静默 bug，值得记下来：
            #   第一版把循环变量命名为 (key, bucket)，守卫写成 `if not key` ——
            #   而 key 是累加字典（初始为空 → falsy），于是**每个 span
            #   都在第一行被 continue 掉**：不报错、接口 200、维度永远为空。
            #
            #   两个教训：
            #     ① 守卫要检查的是「这一维的值存不存在」（sp.get("type")），
            #        不是「累加器在不在」—— 变量名起错，条件就检查错了东西
            #     ② **聚合接口返回"空结果"和"没有数据"必须是两种状态**，
            #        否则这类 bug 的表现就是"看起来正常但查不出东西"
            for acc, dim in ((by_type, sp.get("type") if sp.get("type") != "agent" else None),
                             (by_name, sp.get("name") if sp.get("type") == "agent" else None)):
                # ★ 两个维度各管一半，加起来才等于总账：
                #   by_type  = 叶子 span（llm / tool）—— 真正"花钱的动作"
                #   by_name  = agent 节点 —— 谁花的
                #   agent 节点的 usage 是从子 span 归并来的，两边都算
                #   就是重复计费。叶子明细去 /traces/{id} 看。
                if not dim:
                    continue
                b = acc.setdefault(dim, {"calls": 0, "tokens": 0,
                                         "cost_cny": 0.0, "elapsed_ms": 0})
                b["calls"] += 1
                b["tokens"] += tok
                b["cost_cny"] = round(b["cost_cny"] + c, 6)
                b["elapsed_ms"] += sp.get("elapsed_ms") or 0

    def _pctl(values, p):
        if not values:
            return 0
        s = sorted(values)
        idx = min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1)
        return s[max(0, idx)]

    hit_rate = (cache_hit / (cache_hit + cache_miss)) if (cache_hit + cache_miss) else 0.0

    return {
        "runs": n,
        "errors": errors,
        "tokens": total_usage,
        "cost_cny": round(total_cost, 4),
        # 延迟口径：**只统计单次请求**（不含批处理任务），
        # 并带上样本数 —— 样本少的时候 P95 约等于最大值，读者自己该知道。
        "elapsed_ms": {
            "avg": int(sum(elapsed) / len(elapsed)) if elapsed else 0,
            "p50": _pctl(elapsed, 50),
            "p95": _pctl(elapsed, 95),
            "sample": len(elapsed),
        },
        "batch_runs": batch_runs,
        "prompt_cache_hit_rate": round(hit_rate, 4),
        "by_span_type": _sorted(by_type),
        "by_span_name": _sorted(by_name),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"最近 {n} 次 trace",
    }


def _sorted(bucket: dict) -> dict:
    """按成本降序。看报告的人第一眼就想知道"钱花哪了"。"""
    return dict(sorted(bucket.items(),
                       key=lambda kv: -kv[1].get("cost_cny", 0)))
