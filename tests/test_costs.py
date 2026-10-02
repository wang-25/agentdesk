# -*- coding: utf-8 -*-
"""成本计算与聚合 —— "钱花在哪了"这句话能不能信的底线。

这一层的特点：**算错了看不出来**。token 数看着正常、接口返回 200、
数字精确到小数点后四位，读者会自动相信它 —— 而它可能：
按错的模型名查价（请求 chat，实际由 flash 服务）、
忽略缓存命中的折扣（Flash 上命中价是未命中的 1/50）、
或者把同一笔 token 算了两遍（usage 里字段互相包含）。

所以这里的用例密度集中在三件事上：

    · **口径**：峰值/空闲、命中/未命中、别名 → 正式模型名，各是一个价
    · **对账**：by_name / by_type 各自必须等于总账（偏差在 1% 容差内）
    · **不丢钱**：没有 Agent 祖先的花费必须进 "(无节点)" 而不是被丢掉

★ 凡涉及时间的断言一律传**固定时刻**（`at=`），不读当前时间 ——
  否则这些用例会在每天下午 18:00 之后或周末悄悄变红/变绿。
"""

from datetime import datetime, timezone

import pytest

from app.observability import costs

BEIJING = costs.BEIJING
FLASH = costs.PRICING["deepseek-flash"]

# 固定的高峰/空闲时刻。周一至周五 9:00-12:00、14:00-18:00 为高峰。
PEAK_AT = datetime(2026, 9, 30, 10, 0, tzinfo=BEIJING)      # 周三上午
OFF_AT = datetime(2026, 9, 30, 12, 30, tzinfo=BEIJING)      # 周三午休
WEEKEND_AT = datetime(2026, 10, 3, 10, 0, tzinfo=BEIJING)   # 周六上午


def _fresh_unpriced():
    """清掉进程级的"未知模型"记录。

    `_UNPRICED` 是模块级集合、跨用例累积 —— 不清就会让后面的用例
    "白捡"前面留下的条目，断言变成永远为真。
    """
    costs._UNPRICED.clear()


# ============================================================
# 一、单价表本身的形状
# ============================================================
def test_price_table_is_tiered_per_million_tokens():
    """每个模型都必须同时给出"峰值/空闲"两档、且空闲价严格更低。

    平台是峰谷定价（空闲是高峰的一半）—— 如果哪天有人把两档填成一样，
    峰谷逻辑就等于静默失效，成本会系统性偏高，而报表上看不出异常。
    """
    assert costs.PRICING, "单价表空了"
    for model, price in costs.PRICING.items():
        for field in ("cache_hit", "input", "output"):
            peak, off = price[f"{field}_peak"], price[f"{field}_off"]
            assert off < peak, f"{model}.{field} 的空闲价没有低于高峰价"
        # 缓存命中必须比未命中便宜得多（Flash 上是 1/50），否则"缓存折扣"是假的
        assert price["cache_hit_off"] < price["input_off"], model


# ============================================================
# 二、模型名归一：按"实际服务的模型"计价
# ============================================================
def test_legacy_aliases_resolve_to_flash():
    """老名字（deepseek-chat 等）归一成 `deepseek-flash`。

    实测：请求 `deepseek-chat`，服务端返回 `model: "deepseek-flash"`。
    不归一的话这些调用会走兜底价，而"请求名"和"实际服务的模型"可能不是一回事 ——
    成本数字看起来正常，其实没有任何一条是按真实单价算的。
    """
    for alias in ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash",
                  "deepseek-v4-flash-vision-exp"):
        assert costs.resolve_model(alias) == costs.DEFAULT_MODEL, alias


def test_resolve_model_is_case_and_space_insensitive():
    """名字来自配置/请求体，大小写和空格不该改变计价结果。"""
    assert costs.resolve_model("  DeepSeek-Chat  ") == costs.DEFAULT_MODEL
    assert costs.resolve_model("DEEPSEEK-FLASH") == costs.DEFAULT_MODEL


def test_unknown_model_is_recorded_and_priced_by_fallback():
    """未知模型要**可见**：走兜底价，同时出现在 unpriced_models() 里。

    "用一个不知道对不对的价格默默算出一堆数字"比算不出来更糟：
    读者会以为那是真实成本。所以未知模型必须能被 /metrics/summary 点出来。
    """
    _fresh_unpriced()
    assert costs.resolve_model("some-future-model") == "some-future-model"
    assert costs.unpriced_models() == ["some-future-model"]

    u = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert costs.cost_of(u, "some-future-model", at=OFF_AT) == pytest.approx(
        FLASH["input_off"] + FLASH["output_off"])


def test_empty_model_name_falls_back_to_default():
    """没填模型名不是错误路径 —— 按默认模型算，而且**不要**记成未知模型。"""
    _fresh_unpriced()
    for blank in (None, "", "   "):
        assert costs.resolve_model(blank) == costs.DEFAULT_MODEL  # type: ignore[arg-type]
    assert costs.unpriced_models() == []


def test_model_of_span_prefers_what_the_server_served():
    """从 span 取模型时 `model_served` 优先于请求时的 `model`。

    这就是"按返回的模型计费"那条修正：请求名与实际服务名可能不是同一个模型，
    而两者的单价不同。取错顺序 → 成本按错的价目表算。
    """
    assert costs.model_of_span(
        {"attrs": {"model": "deepseek-chat", "model_served": "deepseek-flash"}}
    ) == "deepseek-flash"
    # 只有请求名时退回请求名；两者都没有时退回默认模型
    assert costs.model_of_span({"attrs": {"model": "deepseek-v4-pro"}}) == "deepseek-v4-pro"
    assert costs.model_of_span({}) == costs.DEFAULT_MODEL
    assert costs.model_of_span(None) == costs.DEFAULT_MODEL  # type: ignore[arg-type]


# ============================================================
# 三、峰谷定价：一律用固定时刻验证
# ============================================================
def test_is_peak_boundaries():
    """峰谷判定的**边界**：9:00/12:00/14:00/18:00 各是含还是不含。

    边界错一分钟，就会有一批调用按错的价格入账 —— 而且只在特定时刻发生，
    开发时几乎撞不到。周末全天算空闲，拿"工作日"当唯一条件就会多算一倍钱。
    """
    # 先自检锚点日期：防止"某天顺手改了日期"让这些用例静默失去意义
    assert PEAK_AT.weekday() == 2 and OFF_AT.weekday() == 2 and WEEKEND_AT.weekday() == 5
    cases = [
        (datetime(2026, 9, 30, 9, 0, tzinfo=BEIJING), True, "9:00 是高峰起点"),
        (datetime(2026, 9, 30, 11, 59, tzinfo=BEIJING), True, "12:00 之前仍是高峰"),
        (datetime(2026, 9, 30, 12, 0, tzinfo=BEIJING), False, "12:00 整已离开高峰"),
        (datetime(2026, 9, 30, 14, 0, tzinfo=BEIJING), True, "14:00 是下午高峰起点"),
        (datetime(2026, 9, 30, 18, 0, tzinfo=BEIJING), False, "18:00 整已离开高峰"),
        (datetime(2026, 9, 30, 8, 59, tzinfo=BEIJING), False, "9:00 之前不算高峰"),
        (WEEKEND_AT, False, "周末全天算空闲"),
    ]
    for at, peak, why in cases:
        assert costs.is_peak(at) is peak, why


def test_naive_and_utc_datetimes_are_normalised_to_beijing():
    """计费口径在北京时间，与跑在哪个时区无关；不带时区的按北京时间解释。

    同一时刻用 UTC 表示（02:00 = 北京 10:00）必须是**高峰** ——
    按本机时区判断的话，这个数在 UTC 机器上会算成空闲价（半价）。
    naive datetime（历史数据里常见）也必须被当成北京时间，而不是拒算或当成 UTC。
    """
    assert costs.is_peak(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)) is True
    assert costs.is_peak(datetime(2026, 9, 30, 10, 0)) is True
    assert costs.is_peak(datetime(2026, 9, 30, 12, 30)) is False


def test_peak_costs_exactly_twice_the_off_peak_price_and_the_weekend_is_off():
    """同一笔用量，高峰价必须是空闲价的 2 倍；周末按空闲价算。

    这是账单口径，不是近似 —— 平台明说空闲时段是高峰的一半。
    算成别的倍数（或拿"工作日"当唯一条件而在周末多算一倍钱）
    说明有人改了表却没改逻辑，而报表上看不出异常。
    """
    u = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    off = costs.cost_of(u, costs.DEFAULT_MODEL, at=OFF_AT)
    peak = costs.cost_of(u, costs.DEFAULT_MODEL, at=PEAK_AT)
    assert peak == pytest.approx(off * 2)
    assert costs.cost_of(u, costs.DEFAULT_MODEL, at=WEEKEND_AT) == pytest.approx(off)


# ============================================================
# 四、缓存命中 vs 未命中
# ============================================================
def test_cache_hit_and_miss_are_priced_differently():
    """同样多的输入 token，"全部命中"必须比"全部未命中"便宜得多。

    旧价格表把命中价记成 ¥0.5（真值 ¥0.02，差 25 倍），
    而本项目缓存命中率约 50% —— 那不是"略有偏差"，是量级错误。
    """
    hit = costs.cost_of({"prompt_tokens": 1_000_000,
                         "prompt_cache_hit_tokens": 1_000_000}, at=OFF_AT)
    miss = costs.cost_of({"prompt_tokens": 1_000_000,
                          "prompt_cache_miss_tokens": 1_000_000}, at=OFF_AT)
    assert hit < miss
    assert hit == pytest.approx(FLASH["cache_hit_off"])
    assert miss == pytest.approx(FLASH["input_off"])


def test_partial_cache_hit_splits_the_input_by_its_own_price():
    """命中与未命中各按自己的单价算，剩下的部分不能都按未命中价算。"""
    u = {"prompt_cache_hit_tokens": 500_000, "prompt_cache_miss_tokens": 500_000}
    got = costs.cost_of(u, costs.DEFAULT_MODEL, at=OFF_AT)
    assert got == pytest.approx((500_000 * FLASH["cache_hit_off"]
                                 + 500_000 * FLASH["input_off"]) / 1_000_000)
    # 全按未命中算会高得多 —— 这条断言就是"缓存折扣真的生效了"
    all_miss = costs.cost_of({"prompt_tokens": 1_000_000}, at=OFF_AT)
    assert got < all_miss


def test_old_usage_format_without_miss_field_is_supported():
    """老格式（只有 prompt_tokens 和命中数，没有 miss 字段）必须能算。

    历史 trace 里没有 `prompt_cache_miss_tokens`。若代码只认新字段，
    这些记录会被算成"输入为 0"—— 复算历史成本时会凭空少一大块钱。
    这里的口径应为 miss = prompt_tokens - hit。
    """
    u = {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 400_000,
         "completion_tokens": 0}
    assert costs.cost_of(u, costs.DEFAULT_MODEL, at=OFF_AT) == pytest.approx(
        (400_000 * FLASH["cache_hit_off"] + 600_000 * FLASH["input_off"]) / 1_000_000)


def test_miss_never_goes_negative_when_hit_exceeds_prompt_tokens():
    """命中数比总输入还大（脏数据）时，未命中部分按 0 计，不能变成负数。

    负的 token 会**倒扣钱** —— 报表上表现为成本莫名其妙偏低，
    这类"负值"比报错难查得多。
    """
    u = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 500,
         "completion_tokens": 10_000}
    got = costs.cost_of(u, costs.DEFAULT_MODEL, at=OFF_AT)
    expected = (500 * FLASH["cache_hit_off"] + 10_000 * FLASH["output_off"]) / 1_000_000
    assert got == pytest.approx(expected)
    assert got > 0


def test_non_dict_usage_costs_nothing_instead_of_raising():
    """usage 不是 dict（模型返回异常、字段缺失）时算 0，不抛异常。

    这个函数在 trace 收尾和聚合里被调用 —— 它抛异常会连带丢掉整条 trace
    的成本统计，远比少算一次调用严重。空 usage 同理：算 0 元是**合法**结果。
    """
    _fresh_unpriced()
    bad_usage: list[object] = [None, [], "100 tokens", 7, {}]
    for bad in bad_usage:
        assert costs.cost_of(bad, costs.DEFAULT_MODEL, at=OFF_AT) == 0.0  # type: ignore[arg-type]
    assert costs.unpriced_models() == []


def test_unknown_model_falls_back_to_the_flash_price_of_the_same_tier():
    """未知模型的兜底价 == Flash 在**同一时段**的价（不是无条件"高峰价"）。

    ★ 记录当前真实行为，别被注释骗了：`_FALLBACK = PRICING["deepseek-flash"]`
      是**同一张分档表**，所以「宁可高估」只对"比 Flash 便宜/等价"的模型成立；
      对一个比 Flash 更贵的未知模型（例如 ¥50 的模型），兜底价是**低估**的。
      这里如实固化了这一点：未知模型 = 按 Flash 计，没有额外的保守系数。
      如果哪天兜底表真的被换成一份更贵的独立表，这条会红 ——
      提醒改动者：未知模型的成本口径变了，报表解读也要跟着改。
    """
    assert costs._FALLBACK == costs.PRICING[costs.DEFAULT_MODEL]
    u = {"prompt_cache_miss_tokens": 1_000_000}
    for at, tier in ((OFF_AT, "off"), (PEAK_AT, "peak")):
        assert costs.cost_of(u, "model-not-in-table", at=at) == pytest.approx(
            FLASH[f"input_{tier}"]), tier


def test_usage_with_nested_details_is_ignored_not_swallowed():
    """usage 里混入嵌套 dict（`prompt_tokens_details`）时：不抛异常、也不丢字段。

    DeepSeek 的 usage 同时带 prompt_tokens_details 这类嵌套结构。
    逐字段做算术的代码会在这里 `int + dict` 崩掉 —— 而崩在 trace 收尾的
    `except: pass` 里，表现是"成本数字看起来正常但少了一半字段"。
    """
    _fresh_unpriced()
    u = {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 200,
         "completion_tokens": 300,
         "prompt_tokens_details": {"cached_tokens": 100},
         "completion_tokens_details": {"reasoning_tokens": 50}}
    got = costs.cost_of(u, costs.DEFAULT_MODEL, at=OFF_AT)
    assert got == pytest.approx((100 * FLASH["cache_hit_off"]
                                 + 200 * FLASH["input_off"]
                                 + 300 * FLASH["output_off"]) / 1_000_000)
    assert costs.unpriced_models() == []


# ============================================================
# 五、aggregate：按 trace 结构造数据
# ============================================================
def _leaf(span_id, parent_id, name, stype, usage, attrs=None):
    """造一条**结构合法**的 span 记录。

    字段照 `tracer._Span._finish()` 与 `tracer.trace()` 真实写出的形状来，
    不自己发明字段名 —— 否则测的是"我以为的结构"，而不是真实落盘的结构。
    attrs 用 `{"model": ...}` / `{"model_served": ...}` 的形式传，形如真实落盘。
    """
    return {"kind": "span", "trace_id": "tr-x", "span_id": span_id,
            "parent_id": parent_id, "type": stype, "name": name,
            "started_at": "2026-09-30T10:00:00.000", "elapsed_ms": 100,
            "status": "ok", "usage": usage, "attrs": dict(attrs or {})}


def _trace(name="run", spans=(), usage=None, question="q", cost_cny=None,
           elapsed_ms=1000, status="ok", source="live", **over):
    """造一条结构合法的 trace 汇总记录（kind=trace）。"""
    rec = {"kind": "trace", "trace_id": "tr-" + name, "name": name,
           "batch": False, "source": source, "question": question,
           "started_at": "2026-09-30T10:00:00.000", "elapsed_ms": elapsed_ms,
           "status": status, "usage": usage or {},
           "cost_cny": cost_cny, "span_count": len(spans),
           "spans": list(spans)}
    rec.update(over)
    return rec


def _llm(span_id, parent_id, name="chat", hit=0, miss=0, out=0, model=None):
    u = {"prompt_tokens": hit + miss, "completion_tokens": out,
         "total_tokens": hit + miss + out,
         "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}
    attrs = {"model": model, "model_served": model} if model else {}
    return _leaf(span_id, parent_id, name, "llm", u, attrs=attrs)


def _agent(span_id, parent_id, name):
    """造一个 agent 节点 span（本身不直接产生用量，靠子 span 归并）。"""
    return _leaf(span_id, parent_id, name, "agent", {})


def test_empty_input_reports_zero_runs_without_raising():
    """"没有数据"必须是一种明确状态（runs=0），而不是 KeyError 或空字典。

    聚合接口返回空结果和"没有数据"看起来一样，正是本项目踩过的坑
    （维度永远为空、接口 200）。
    """
    assert costs.aggregate([])["runs"] == 0


def test_selftest_traces_are_excluded_from_the_report():
    """自检造的 trace（source="selftest"）不进对外口径。

    实测过：窗口里 50 条 trace 有 24 条是自检造的，errors 显示 8 而真实运行
    一次错误都没有 —— 计数类指标被污染成"错误率 16%"的系统。
    """
    live = _trace("live-1", spans=[_llm("s1", None, hit=1_000_000)],
                  usage={"prompt_cache_hit_tokens": 1_000_000})
    fake = _trace("self-1", spans=[_llm("s1", None, hit=1_000_000)],
                  source="selftest", status="error")
    report = costs.aggregate([live, fake])
    assert report["runs"] == 1
    assert report["errors"] == 0
    assert report["tokens"]["prompt_cache_hit_tokens"] == 1_000_000


def test_cost_is_recomputed_from_leaf_spans_not_from_the_stored_number():
    """成本按当前单价表从**叶子 span** 重算，不累加落盘的 cost_cny。

    落盘值是用"当时那张（可能错的）价格表"算的。直接累加会让报告里
    混着两套价格基准，而读者无从知道哪条 trace 是哪个价算的。
    """
    spans = [_agent("agent-1", None, "diagnose"),
             _llm("llm-1", "agent-1", hit=1_000_000)]
    t = _trace("recompute", spans=spans, cost_cny=999.0)
    report = costs.aggregate([t])
    expected = costs.cost_of(spans[1]["usage"], "deepseek-flash", at=None)  # type: ignore[arg-type]
    assert report["cost_cny"] == pytest.approx(expected, abs=1e-4)
    assert report["cost_cny"] < 999.0, "用了落盘值 —— 重算没有生效"


def test_cost_uses_each_leafs_own_model():
    """每个叶子按**自己用的模型**计价，而不是全表按默认模型。

    pro 与 flash 的单价差好几倍。整表按默认模型算，会让一次混用模型的
    trace 成本偏差到离谱，而报表上看不出是哪个 span 的问题。

    期望值按**当前时段**取档：`aggregate()` 没有 `at` 参数（它按"现在"计价），
    所以这里用 `is_peak()` 读出档位再算期望，否则这条用例会在
    每天 12:00-14:00 或 18:00 之后自己变红。
    """
    flash = _llm("s-flash", None, miss=1_000_000_000, model="deepseek-flash")
    pro = _llm("s-pro", None, miss=1_000_000_000, model="deepseek-v4-pro")
    report = costs.aggregate([_trace("mixed", spans=[flash, pro])])

    tier = "peak" if costs.is_peak() else "off"
    # 1e9 token = 1000 个"百万 token"，所以单价要乘 1000
    expect = 1000 * (costs.PRICING["deepseek-flash"][f"input_{tier}"]
                     + costs.PRICING["deepseek-v4-pro"][f"input_{tier}"])
    assert report["cost_cny"] == pytest.approx(expect, rel=1e-3)
    # 整表按 flash 算会少算 pro 那部分 —— 这条是"按各自模型计价"的证据
    assert report["cost_cny"] > 1000 * 2 * costs.PRICING["deepseek-flash"][f"input_{tier}"]


def test_traces_without_spans_fall_back_to_the_stored_cost():
    """没有 span 的 trace（例如流式接口）退回落盘的 cost_cny。

    退回落盘值比算成 0 诚实 —— 它至少是一笔真实花掉的钱；
    而这类 trace 数是"两个维度合计小于总账"的**已知来源**，必须被点出来。
    """
    t = _trace("stream", spans=[], cost_cny=0.5,
               usage={"prompt_tokens": 100, "completion_tokens": 50})
    report = costs.aggregate([t])
    assert report["cost_cny"] == pytest.approx(0.5)
    assert report["reconcile"]["traces_without_spans"] == 1
    # 钱进了总账，进不了两个维度 —— 这是已知且被报告的偏差来源
    assert report["reconcile"]["by_type_gap"] == pytest.approx(-1.0)


def test_both_dimensions_reconcile_with_the_total_within_one_percent():
    """两个维度各自合计 ≈ 总账（1% 容差），且偏差字段自己会说话。

    by_type（钱花在什么事上）与 by_name（钱花在谁身上）是同一笔钱的
    **两个正交切法**：各自独立等于总账，相加等于翻倍。
    对账偏差一旦被场景差异填满，这个信号就废了 —— 所以必须有这条断言。
    """
    spans = [_leaf("a1", None, "diagnose", "agent", {}),
             _leaf("a2", "a1", "verify", "agent", {}),
             _llm("l1", "a2", hit=1_000_000),
             _llm("l2", "a1", miss=200_000, out=50_000),
             _leaf("t1", "a1", "check_disk", "tool", {"total_tokens": 10})]
    report = costs.aggregate([_trace("recon", spans=spans)])
    rec = report["reconcile"]
    assert rec["by_type_total_cny"] == pytest.approx(rec["trace_total_cny"], rel=0.01)
    assert rec["by_name_total_cny"] == pytest.approx(rec["trace_total_cny"], rel=0.01)
    assert abs(rec["by_type_gap"]) <= 0.01
    assert abs(rec["by_name_gap"]) <= 0.01
    assert "warning" not in rec, "两个维度都应当对得上，不该报警"
    # 口径说明必须随数字一起返回：两个维度是同一笔钱的两种切法，相加等于翻倍
    assert "翻倍" in rec["note"] and "叶子" in rec["note"]


def test_by_type_counts_every_leaf_and_nothing_else():
    """维度也只按**叶子**计：中间层 agent span 的用量是从子 span 归并来的，
    再算一次就是重复计数（这正是"两类相加正好翻倍"的成因）。"""
    spans = [_leaf("a1", None, "diagnose", "agent", {"total_tokens": 9999}),
             _llm("l1", "a1", hit=1_000_000)]
    report = costs.aggregate([_trace("leaves", spans=spans)])
    assert set(report["by_span_type"]) == {"llm"}
    assert report["by_span_type"]["llm"]["calls"] == 1
    assert report["by_span_type"]["llm"]["tokens"] == 1_000_000


def test_by_name_attributes_the_money_to_the_nearest_agent_ancestor():
    """叶子往上找**最近的** agent 祖先，而不是最外层那个。

    找错层会把"哪个 Agent 在白花钱"这个结论整个弄错。
    这条同时覆盖多层嵌套（a1 → a2 → 叶子）。
    """
    spans = [_leaf("a1", None, "supervisor", "agent", {}),
             _leaf("a2", "a1", "diagnose", "agent", {}),
             _llm("l1", "a2", hit=1_000_000),
             _leaf("l2", None, "chat", "llm", {"total_tokens": 5})]
    report = costs.aggregate([_trace("owner", spans=spans)])
    assert set(report["by_span_name"]) == {"diagnose", "(无节点)"}
    assert report["by_span_name"]["diagnose"]["calls"] == 1
    # 顶层没有 agent 祖先的那次调用必须进 "(无节点)"
    assert report["by_span_name"]["(无节点)"]["calls"] == 1


def test_spans_without_an_agent_ancestor_are_bucketed_not_dropped():
    """**丢了钱会让对账失真** —— 找不到 agent 祖先就归 "(无节点)"。

    评测类 trace 直接调 llm，压根没有 Agent 编排。上一版把这部分钱只进
    by_type、不进 by_name，实测 by_name 偏差 -76%，对账直接变成噪声告警。
    """
    spans = [_llm("l1", None, miss=1_000_000),
             _llm("l2", "tool-1", miss=1_000_000)]          # 父 span 不在本 trace 里
    report = costs.aggregate([_trace("orphan", spans=spans)])
    assert set(report["by_span_name"]) == {"(无节点)"}
    assert report["by_span_name"]["(无节点)"]["calls"] == 2
    assert report["reconcile"]["by_name_gap"] == pytest.approx(0.0, abs=1e-4)


def test_a_cycle_in_parent_links_does_not_hang():
    """父链成环（拼接坏的数据）时不能死循环 —— _owner_name 有 seen 保护。

    ★ 已知问题（见下一条）：不挂死，但**钱会消失**。这条只锁"不挂死"。
    """
    spans = [_leaf("x", "y", "chat", "llm", {"prompt_tokens": 1_000_000_000}),
             _leaf("y", "x", "tool", "tool", {})]
    report = costs.aggregate([_trace("cycle", spans=spans)])
    assert report["runs"] == 1


def test_a_cycle_silently_hides_the_money_it_should_still_be_counted():
    """**已知问题（如实固化，不要为了让用例变绿而删掉）**：

    父链成环时，环上每个 span 的 span_id 都出现在某个 parent_id 里，
    于是 `[s for s in spans if s.get("span_id") not in parents]`
    （`app/observability/costs.py:275`）筛不出**任何**叶子 ——
    这批 span 的钱既没进总账、也没进两个维度：

        真实花费：1.0 元     聚合报告：{"trace_total_cny": 0.0}

    后果：对账**看起来是完美的**（差值 0.0，小于 1% 容差，不报警），
    而账面上凭空少了一整笔钱。"对账差 0"本来是"数据可信"的信号，
    在这里变成了"数据被吞掉"的掩护。

    注：正常路径下 trace 记录由 `tracer.trace()` 生成，不该出现环；
    这条防的是坏数据/人工拼接的 trace，属于聚合层的健壮性。
    修法：叶子的定义应兜底为"没有任何 span 把它当父节点，**且至少存在一个叶子**"，
    否则退回按顶层 span（parent_id is None）算。
    """
    spans = [_leaf("x", "y", "chat", "llm", {"prompt_tokens": 1_000_000_000}),
             _leaf("y", "x", "tool", "tool", {})]
    report = costs.aggregate([_trace("cycle", spans=spans)])
    assert report["reconcile"]["trace_total_cny"] == 0.0
    assert report["reconcile"]["by_name_gap"] == 0.0
    assert "warning" not in report["reconcile"], "丢了钱却没有任何提示 —— 这就是问题本身"


def test_top_level_usage_tokens_are_summed_but_not_double_counted():
    """总 token 取 trace 的顶层 usage（已归并），并且只累加数值字段。"""
    hit = 1_000_000
    t = _trace("tok", spans=[_llm("l1", None, hit=hit, out=5)],
               usage={"prompt_cache_hit_tokens": hit, "completion_tokens": 5,
                      "total_tokens": hit + 5, "prompt_tokens_details": {"cached": hit}})
    report = costs.aggregate([t])
    assert report["tokens"]["total_tokens"] == hit + 5
    assert "prompt_tokens_details" not in report["tokens"], "嵌套 dict 不该被并进 token 合计"


def test_trace_usage_with_nested_details_does_not_break_aggregation():
    """trace 级 usage 里混嵌套 dict 时聚合不能抛异常 —— 一条脏数据不该挂掉整个报表。"""
    t = _trace("nested", spans=[_llm("l1", None, hit=1)],
               usage={"total_tokens": 10, "prompt_tokens_details": {"cached": 5}})
    report = costs.aggregate([t])
    assert report["tokens"]["total_tokens"] == 10


def test_elapsed_and_batch_runs_are_counted_in_separate_buckets():
    """批处理任务的钱要算，延迟**不能**算。

    实测过一次 P95 显示 315 秒 —— 那是评测跑一轮的真实耗时。
    把批处理和用户请求混在一个延迟分布里，P95 就变成"哪个批处理跑了多久"。
    """
    fast = _trace("fast", spans=[_llm("l1", None, hit=1)], elapsed_ms=100)
    slow = _trace("slow", spans=[_llm("l1", None, hit=1)], elapsed_ms=315_675, batch=True)
    report = costs.aggregate([fast, slow])
    assert report["elapsed_ms"]["sample"] == 1
    assert report["elapsed_ms"]["p95"] == 100
    assert report["batch_runs"] == 1
    assert report["runs"] == 2, "批处理的成本仍要计入运行次数与总账"


def test_cache_hit_rate_is_computed_over_both_token_kinds():
    """命中率的分母是"命中 + 未命中"，不是"总输入"或其他近似。

    这个数字是"system prompt 是不是每次都在变"的唯一信号，
    分母选错会让它看起来永远偏高（缓存效果好得不像话）。
    """
    t = _trace("rate", spans=[_llm("l1", None)],
               usage={"prompt_cache_hit_tokens": 750, "prompt_cache_miss_tokens": 250})
    assert costs.aggregate([t])["prompt_cache_hit_rate"] == pytest.approx(0.75)


def test_distinct_questions_exposes_repeated_debugging():
    """`runs` 会和"不同问题数"一起给出 —— 用来识别反复测同一件事。

    49 次运行 / 8 个不同问题 = 开发调试；49 次 / 47 个 = 才像真实使用。
    服务端区分不了"真实用户"和"开发者"，只能给这个旁证。
    """
    traces = [_trace("a", spans=[_llm("l1", None, hit=1)], question="磁盘满了吗"),
              _trace("b", spans=[_llm("l1", None, hit=1)], question="磁盘满了吗"),
              _trace("c", spans=[_llm("l1", None, hit=1)], question="  ")]
    report = costs.aggregate(traces)
    assert report["runs"] == 3
    assert report["distinct_questions"] == 1


def test_unpriced_models_are_surfaced_in_the_report():
    """用了单价表里没有的模型，聚合结果必须自己点出来。

    否则这些数字是"兜底价估的"，而读者会当成真实成本。
    """
    _fresh_unpriced()
    t = _trace("unpriced", spans=[_llm("l1", None, miss=1_000_000,
                                       model="ghost-model")])
    report = costs.aggregate([t])
    assert "ghost-model" in report["unpriced_models"]
    tier = "peak" if costs.is_peak() else "off"
    assert report["cost_cny"] == pytest.approx(costs._FALLBACK[f"input_{tier}"],
                                              rel=1e-3)


def test_by_span_dimensions_are_sorted_by_cost_descending():
    """排序不是装饰：看报告的人第一眼要看到"哪一步最贵"。

    两个维度都刻意造出**两个桶**、且便宜的那个先出现 ——
    否则"降序"这条断言会因为只有一个桶而永远为真（等于没测）。
    """
    # (无节点) 的孤儿调用（1000M token）比 diagnose 名下的那次贵得多
    spans = [_agent("a1", None, "diagnose"),
             _llm("cheap", "a1", hit=100),
             _llm("pricey", None, miss=1_000_000_000)]
    report = costs.aggregate([_trace("sort", spans=spans)])
    assert set(report["by_span_name"]) == {"diagnose", "(无节点)"}
    assert list(report["by_span_name"])[0] == "(无节点)", "贵的桶必须排在前面"
    assert list(report["by_span_type"]) == ["llm"]
