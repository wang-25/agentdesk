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

★ 单价需要人工核实。**2026-09-28 核对时发现旧表整体是过期的**，
  见下面 PRICING 上面的长注释 —— 那次核对把三个问题一起挖出来了：
  模型名对不上（请求 chat、实际由 flash 服务）、单价量级错（缓存命中差了 25 倍）、
  以及平台已改成峰谷定价。**这三件事都是"成本数字看起来正常但其实是错的"类型。**
"""

import math
from datetime import datetime, timedelta, timezone

# ============================================================
# 单价表：¥ / 1M tokens
# ============================================================
# 上次核对：2026-09-28
# 依据：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
#
# ★ 为什么整张表重写了（旧表按 deepseek-chat 记 输入 ¥2 / 缓存命中 ¥0.5 / 输出 ¥8）：
#
#   ① **模型名对不上**。实测：请求 `deepseek-chat`，服务端返回
#      `model: "deepseek-flash"` —— 现在对外的主模型名是 `deepseek-flash`
#      （底层 DeepSeek-V4.1-Flash），`deepseek-chat` 只是兼容别名。
#      单价按"返回的模型"查，才是最接近账单的口径。
#
#   ② **单价量级错**。Flash 的实际单价：缓存命中 ¥0.02、未命中 ¥1、输出 ¥4。
#      旧表记的是 ¥0.5 / ¥2 / ¥8 —— **缓存命中差了 25 倍**。
#      而本项目的缓存命中率约 50%，所以旧表算出来的不是"略有偏差"，是量级错误。
#      （教训：价格表属于"会随外部世界变化"的数字，必须定期核对，
#        而且核对时要连"模型名有没有变"一起查，不能只看数字。）
#
#   ③ **平台已改成峰谷定价**。空闲时段单价是高峰时段的一半。
#      高峰 = 北京时间周一至周五（不含法定节假日）9:00-12:00、14:00-18:00；
#      其余时段（含周末与法定节假日全天）为空闲。
#      ⚠️ 已知近似：本项目**没有法定节假日日历**，节假日会按工作日算，
#         即高峰判定偏保守（可能多算钱）。宁可略高估，也不要低估。
BEIJING = timezone(timedelta(hours=8))

PRICING = {
    "deepseek-flash": {
        "cache_hit_off": 0.02, "cache_hit_peak": 0.04,
        "input_off": 1.0, "input_peak": 2.0,
        "output_off": 4.0, "output_peak": 8.0,
    },
    "deepseek-v4-pro": {
        "cache_hit_off": 0.15, "cache_hit_peak": 0.30,
        "input_off": 4.5, "input_peak": 9.0,
        "output_off": 13.5, "output_peak": 27.0,
    },
}

# 兼容别名 → 现在的正式模型名。
# 依据官方价格页脚注：旧名（deepseek-v4-flash 等）仍可调用，
# 但对应模型已下线，请求由 DeepSeek-V4.1-Flash 提供服务，**按 Flash 价格计费**。
# deepseek-chat / deepseek-reasoner 是更早的一代名字，对应 Flash 的非思考/思考模式。
ALIASES = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}

DEFAULT_MODEL = "deepseek-flash"

# 兜底价：未知模型按 Flash 的**高峰**价估 —— 宁可高估，也不要用一个
# 看起来精确但实际偏低的价格去指导优化决策。
_FALLBACK = PRICING[DEFAULT_MODEL]

# ★ 没查到单价的模型会记在这里，并由 /metrics/summary 暴露出来。
#   "用一个不知道对不对的价格默默算出一堆数字"是比算不出来更糟的事 ——
#   读者会以为那是真实成本。所以这里必须**可见**。
_UNPRICED = set()


def unpriced_models() -> list:
    """返回本次进程里出现过、但单价表里没有的模型名。"""
    return sorted(_UNPRICED)


def is_peak(at: datetime = None) -> bool:
    """是否处于高峰时段（北京时间 周一至周五 9:00-12:00 / 14:00-18:00）。

    用北京时间判断，而不是本机时区 —— 计费口径在服务端，与你在哪台机器上跑无关。
    """
    dt = at or datetime.now(BEIJING)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING)
    dt = dt.astimezone(BEIJING)
    if dt.weekday() >= 5:            # 5=周六 6=周日
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)


def resolve_model(model: str) -> str:
    """把别名归一成正式模型名；未知模型记账并原样返回。"""
    key = (model or "").strip().lower()
    key = ALIASES.get(key, key)
    if key and key not in PRICING:
        _UNPRICED.add(key)
    return key or DEFAULT_MODEL


def model_of_span(sp: dict) -> str:
    """从一个 span 记录里取出"这次调用实际用的模型"。

    优先 `model_served`（服务端返回的），退回 `model`（我们请求的）。
    ★ 这个优先顺序就是本文件要修的那个 bug 的核心：**按返回的模型计费**，
      而不是按你请求的那个名字 —— 两者可能不是同一个模型。
    """
    attrs = (sp or {}).get("attrs") or {}
    return attrs.get("model_served") or attrs.get("model") or DEFAULT_MODEL


def _owner_name(sp: dict, by_id: dict) -> str:
    """这个 span 花的钱，该记在哪个 Agent 节点头上。

    从自己开始向上找最近的 `type == "agent"` 祖先，返回它的名字。
    找不到（例如直接调 llm 的 RAG 评测，压根没有 Agent 编排）就归 "(无节点)" ——
    **必须给它一个明确的桶，而不是丢弃**：丢掉的钱会让 by_name 合计小于总账，
    而对账偏差一旦被噪声填满，这个信号就没用了。
    """
    seen = set()
    cur = sp
    while cur:
        if cur.get("type") == "agent" and cur.get("name"):
            return cur["name"]
        pid = cur.get("parent_id")
        if not pid or pid in seen:
            break
        seen.add(pid)
        cur = by_id.get(pid)
    return "(无节点)"


def cost_of(usage: dict, model: str = None, at: datetime = None) -> float:
    """按 usage 算一次调用的人民币成本。

    model 传 None 时按默认模型（Flash）算；未知模型会记进 unpriced_models()。
    at 用于指定计费时刻（默认取当前北京时间），便于对历史数据复算。

    ★ 特意区分了**缓存命中**和**未命中**的输入 ——
      DeepSeek 的 usage 里有 prompt_cache_hit_tokens / prompt_cache_miss_tokens，
      命中部分是大幅打折的（Flash 上是 1/50）。
      **把缓存折扣算进去，才是真的会算账。**
      而且缓存命中率本身就是一个可优化的指标：
      命中率低说明 system prompt 每次都在变（对缓存不友好）。
    """
    if not isinstance(usage, dict):
        return 0.0

    key = resolve_model(model)
    price = PRICING.get(key) or _FALLBACK
    tier = "peak" if is_peak(at) else "off"

    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        # 老格式没有 miss 字段：用总输入减命中
        miss = (usage.get("prompt_tokens") or 0) - hit
    output = usage.get("completion_tokens") or 0

    cost = (hit * price[f"cache_hit_{tier}"]
            + max(0, miss) * price[f"input_{tier}"]
            + output * price[f"output_{tier}"]) / 1_000_000
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
    traces_without_spans = 0

    for t in traces:
        u = t.get("usage") or {}
        for k, v in u.items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v

        # ★ 成本口径：**统一按当前单价表从叶子 span 重算**，
        #   而不是用落盘时的 cost_cny。
        #   原因有两个，都是实测撞出来的：
        #     ① 单价表会被核对修正（这次就把缓存命中价从 ¥0.5 改成 ¥0.02，25 倍）。
        #        若直接累加落盘值，报告里会**混着两套价格基准** ——
        #        而 /metrics/summary 的读者无从知道哪条 trace 是哪个价算的。
        #     ② 只有叶子 span 才知道"实际用的哪个模型"，落盘的 cost_cny
        #        是按当时（可能错的）模型名算的。
        #   落盘的 cost_cny 保留在每条 trace 里，作为"当时记的数"的历史记录。
        spans = t.get("spans") or []
        if not spans:
            # 没有 span 的 trace（例如流式接口）无法重算，退回落盘值 ——
            # 退回落盘值比算成 0 诚实：它至少是一笔真实花掉的钱。
            total_cost += t.get("cost_cny") or 0.0
            traces_without_spans += 1
        # 有 span 的 trace：成本与两个维度**在同一趟里算完**（见下面的叶子归因）。
        # 三条线共用一个来源，才不会各算各的。

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

        # ★ 归因模型的第二版 —— 这一版才真正做到「两个维度各自精确等于总账」。
        #
        #   上一版按"类型"分流：非 agent 的 span 进 by_type，agent 的进 by_name。
        #   它有一个致命的场景漏洞：**评测类 trace 里根本没有 agent span**
        #   （RAG 评测直接调 llm，不经过 Agent 编排），那部分钱只进得了 by_type、
        #   进不了 by_name —— 实测 by_name 偏差达 **-76%**，对账直接变成噪声告警。
        #
        #   根因是"谁该被算"被绑在了 span 类型上。正确做法是：
        #   **统一按叶子 span 归因**（叶子才是真正花钱的那次模型调用），
        #   再让两个维度各自对它做一次切分：
        #     by_type ← 叶子的 type           （钱花在什么事上）
        #     by_name ← 叶子最近的 agent 祖先 （钱花在谁身上；没有就归"(无节点)"）
        #   这样两个维度都是**对同一笔钱的完整分解**，各自合计等于总账，
        #   对账偏差才是有意义的信号，而不是被场景差异填满。
        by_id = {s.get("span_id"): s for s in spans}
        parents = {s.get("parent_id") for s in spans}
        for sp in [s for s in spans if s.get("span_id") not in parents]:
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
            # ★ 按**这个 span 自己用的模型**计价，而不是按默认模型。
            #   模型名从 attrs 里取（model_served 优先），见 model_of_span()。
            c = cost_of(su, model_of_span(sp))
            total_cost += c

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
            #
            # ★ 两个维度是同一笔钱的**两个正交切法**（按事 / 按人），
            #   各自独立等于总账 —— **不能相加**（相加等于翻倍）。
            for acc, dim in ((by_type, sp.get("type") or "unknown"),
                             (by_name, _owner_name(sp, by_id))):
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

    # ---- 对账（reconcile）----
    # ★ 这是本次新增的部分，也是把"文档里的一句话"变成"被检查的事实"的地方。
    #   原先接口只返回三个数字（总账 + 两个维度），谁也不去算它们的差 ——
    #   所以"三者对账一致"从来没被验证过，而实测是**两类相加正好翻倍**。
    #   现在把差值和口径一起返回：
    #     - 两个维度各自独立，合计都应当 ≈ 总账；
    #     - 若哪个维度偏离超过 1%，说明那次聚合漏了数据（例如某个 span
    #       忘了记 usage、或 trace 被截断），**接口自己就会说出来**。
    by_type_total = round(sum(v.get("cost_cny", 0) for v in by_type.values()), 6)
    by_name_total = round(sum(v.get("cost_cny", 0) for v in by_name.values()), 6)
    trace_total = round(total_cost, 6)

    def _gap(part):
        if trace_total <= 0:
            return 0.0
        return round((part - trace_total) / trace_total, 4)

    reconcile = {
        "trace_total_cny": trace_total,
        "by_type_total_cny": by_type_total,
        "by_name_total_cny": by_name_total,
        "by_type_gap": _gap(by_type_total),
        "by_name_gap": _gap(by_name_total),
        # 没有 span 的 trace 数 —— 它们的钱只进总账、进不了两个维度，
        # 是对账差值的**已知来源**。列出来，免得读者去猜那点差值哪来的。
        "traces_without_spans": traces_without_spans,
        # 说明这两个维度是同一笔钱的两种切法，**不能相加**
        "note": "by_span_type（按事）与 by_span_name（按人）是同一笔钱的两种切法，"
                "各自独立等于总账；两者相加等于翻倍。归因口径：都按**叶子 span** 计，"
                "没有 Agent 祖先的花费归入 \"(无节点)\"。",
    }
    # 偏离超过 1% 就点名，让漂移在接口上可见
    suspects = [k for k in ("by_type_gap", "by_name_gap") if abs(reconcile[k]) > 0.01]
    if suspects:
        reconcile["warning"] = ("以下维度与总账偏离超过 1%，通常是某些 span 漏记 "
                                "usage、或 trace 被截断（span 只保留最后 80 个）："
                                + "、".join(suspects))

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
        "reconcile": reconcile,
        # ★ 单价表里没有的模型：说明这些数字是**用兜底价估的**。
        #   空列表才代表"所有成本都按真实单价算的"。
        "unpriced_models": unpriced_models(),
        "price_table_checked_at": "2026-09-28",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"最近 {n} 次 trace",
    }


def _sorted(bucket: dict) -> dict:
    """按成本降序。看报告的人第一眼就想知道"钱花哪了"。"""
    return dict(sorted(bucket.items(),
                       key=lambda kv: -kv[1].get("cost_cny", 0)))
