# -*- coding: utf-8 -*-
"""Trace 记录层 —— 观测数据是"钱花在哪了"的唯一证据链。

这一层的失败模式很特别：**它不会报错**。tracer 的所有对外函数都吞掉自己的
异常（铁律：观测的可用性永远低于业务的可用性），所以出问题时的表现是
"数据静默丢失"或"数字看起来正常但少了一半字段"。

因此这里锁的不是"函数返回什么"，而是四条不变量：

    ① **落盘位置可控**：测试绝不写真实 logs/traces.jsonl（用 trace_file）
    ② **归并语义**：父 span 的 usage = 自己 + 所有子 span（add_usage 累加，
       不是覆盖 —— 覆盖会零点子 span 归并上来的量）
    ③ **唯一事实源**：节点级 agent span 不叠加引擎自报的用量
       （supervisor.py:414-424 的 v1/v2/v3 教训：同一个量只能有一个来源）
    ④ **观测不吞业务异常**：span 内抛出的异常照常抛出，只额外记 status=error

用法上的公共约定：`trace(...)` 结束时写 kind=trace 的汇总记录，
散装 span 记录只在崩溃/异常路径下出现（见 span() 末尾的兜底写入）。
"""

import pytest

from app.agents.common import add_usage, new_usage
from app.observability import tracer


# ============================================================
# 造数据与读数据的小工具
# ============================================================
def _records(path):
    """读回落盘的记录（每行一条 JSON）。"""
    import json
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _trace_records(path):
    return [r for r in _records(path) if r.get("kind") == "trace"]


def _spans_of(path, trace_id):
    rec = next(r for r in _trace_records(path) if r["trace_id"] == trace_id)
    return {s["name"]: s for s in rec["spans"]}


def _close_span_before_trace():
    """在一个 trace **之前**写一条散装 span 记录。

    没有活跃 trace 时 span() 是 no-op，所以散装记录只能这么造。
    这里同时清掉模块级的 `_collected`：它可能是上一个用例留下的列表，
    不清的话这条前置记录会被顺手收进别人的 trace 里。
    """
    tracer._collected.set(None)
    tracer._write({"kind": "span", "trace_id": "tr-earlier", "span_id": "sp-earlier",
                   "parent_id": None, "type": "tool", "name": "stray",
                   "elapsed_ms": 1, "status": "ok", "usage": {}, "attrs": {}})


# ============================================================
# 一、落盘隔离
# ============================================================
def test_a_trace_is_written_to_the_isolated_file(trace_file):
    """一次 trace 结束时落一条 kind=trace 的汇总记录，位置可被测试接管。

    ★ 这条用例同时是"隔离装置有效"的证明：漏了 trace_file，
      它就会往真实 logs/traces.jsonl 里写字。
    """
    with tracer.trace("unit-test", question="磁盘满了吗") as tid:
        with tracer.span(tracer.TYPE_LLM, name="chat") as sp:
            sp.set_usage({"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15})

    assert trace_file.exists()
    rec = next(r for r in _trace_records(trace_file) if r["trace_id"] == tid)
    assert rec["name"] == "unit-test"
    assert rec["question"] == "磁盘满了吗"
    assert rec["status"] == "ok"
    assert rec["span_count"] == 1
    assert rec["source"] == "live" and rec["batch"] is False
    assert rec["usage"]["total_tokens"] == 15
    assert rec["elapsed_ms"] >= 0
    assert [s["name"] for s in rec["spans"]] == ["chat"]


def test_write_appends_ndjson_and_never_opens_the_real_log(trace_file):
    """`_write` 是追加式 NDJSON：每行一条、可逐行解析（坏一行不牵连别行）。"""
    tracer._write({"kind": "trace", "trace_id": "tr-a"})
    tracer._write({"kind": "trace", "trace_id": "tr-b"})
    assert [r["trace_id"] for r in _records(trace_file)] == ["tr-a", "tr-b"]


# ============================================================
# 二、嵌套与归并
# ============================================================
def test_child_usage_is_merged_into_the_parent_span(trace_file):
    """父 span 的 usage = 自身 + 所有子 span —— 标准 trace 语义。

    没有这一步，「成本按 Agent 分布」就是错的：模型调用的 token 记在 llm span 上，
    节点自己的 span 却是 0，看板会显示"诊断不花钱"。
    """
    with tracer.trace("merge") as tid:
        with tracer.span(tracer.TYPE_AGENT, name="diagnose"):
            with tracer.span(tracer.TYPE_LLM, name="chat") as llm:
                llm.set_usage({"prompt_tokens": 100, "completion_tokens": 20,
                               "total_tokens": 120})

    spans = _spans_of(trace_file, tid)
    assert spans["chat"]["usage"]["total_tokens"] == 120
    assert spans["diagnose"]["parent_id"] is None
    assert spans["diagnose"]["usage"] == {"prompt_tokens": 100,
                                          "completion_tokens": 20,
                                          "total_tokens": 120}


def test_merging_climbs_through_every_level(trace_file):
    """三层嵌套：用量层层归并到顶，**归并只做加法**（不覆盖已有的量）。

    每个 span 的记录是在自己 `finally` 里写的，而那一刻它已经吸收了
    所有子 span 的用量 —— 所以中间层（tool）看到的是"自己的 + 下面全部的"。
    这正是"父节点用量 = 自己 + 所有子 span"的字面含义。
    """
    with tracer.trace("deep") as tid:
        with tracer.span(tracer.TYPE_AGENT, name="supervisor"):
            with tracer.span(tracer.TYPE_TOOL, name="check_disk") as tool:
                tool.set_usage({"elapsed_ms_field": 1, "total_tokens": 2})
                with tracer.span(tracer.TYPE_LLM, name="chat") as llm:
                    llm.set_usage({"prompt_tokens": 1000, "completion_tokens": 100,
                                   "total_tokens": 1100})

    spans = _spans_of(trace_file, tid)
    assert spans["chat"]["usage"]["total_tokens"] == 1100
    # 中间层 = 自己的 2 + 子 span 的 1100（既有量没被覆盖）
    assert spans["check_disk"]["usage"] == {"elapsed_ms_field": 1, "total_tokens": 1102,
                                            "prompt_tokens": 1000,
                                            "completion_tokens": 100}
    assert spans["supervisor"]["usage"]["total_tokens"] == 1102
    assert spans["supervisor"]["usage"]["prompt_tokens"] == 1000


def test_nested_spans_are_linked_by_parent_id_not_by_object(trace_file):
    """`parent_id` 必须是**子 span 的字符串 id**，不能是 span 对象。

    本项目踩过：`parent_id = stack[-1]` 塞进去的是 _Span 对象本身。
    顶层 span 因为 parent_id=None 而侥幸正常，一旦嵌套就 json 序列化失败，
    而观测层吞掉了自己的异常 —— 表现不是报错，是**数据静默丢失**。
    """
    with tracer.trace("links") as tid:
        with tracer.span(tracer.TYPE_AGENT, name="intent") as outer:
            with tracer.span(tracer.TYPE_LLM, name="chat") as inner:
                inner.set_usage({"total_tokens": 1})

    spans = _spans_of(trace_file, tid)
    assert isinstance(spans["chat"]["parent_id"], str)
    assert spans["chat"]["parent_id"] == outer.id
    assert spans["intent"]["parent_id"] is None
    assert isinstance(outer.id, str) and outer.id.startswith("sp-")


def test_trace_total_usage_counts_only_top_level_spans(trace_file):
    """trace 的总量只按**顶层 span** 求：归并和汇总必须配套，漏一半就重复计数。

    这里一个 agent 下面挂两次模型调用。如果按全部 span 求和，
    总 token 会变成 (100+50) 归并值 + 100 + 50 = 300，直接翻倍。
    """
    with tracer.trace("total") as tid:
        with tracer.span(tracer.TYPE_AGENT, name="diagnose"):
            with tracer.span(tracer.TYPE_LLM, name="chat") as a:
                a.set_usage({"prompt_tokens": 100, "completion_tokens": 0,
                             "total_tokens": 100})
            with tracer.span(tracer.TYPE_LLM, name="chat") as b:
                b.set_usage({"prompt_tokens": 50, "completion_tokens": 0,
                             "total_tokens": 50})

    rec = next(r for r in _trace_records(trace_file) if r["trace_id"] == tid)
    assert rec["usage"]["total_tokens"] == 150
    assert rec["span_count"] == 3


def test_set_usage_on_a_parent_wipes_out_the_merged_child_usage(trace_file):
    """**记录当前行为（v1 那个坑的现场）**：子 span 归并完之后，
    父 span 再 `set_usage(...)` 会把归并上来的量**整片覆盖掉**。

    这就是 supervisor 的 v1：`set_usage(out.usage)` 让 chat 的 449 token 归零，
    节点成本凭空消失。修法是**节点层根本不要再调 set_usage/add_usage**
    （v3：叶子 span 是唯一事实源），而不是指望调用方记得区别两个方法。

    这条用例的价值：它把"覆盖语义有多危险"钉在文件里 ——
    如果哪天有人把 set_usage 改成"累加"，这条会红，提醒他两个方法的语义被合并了。
    """
    with tracer.trace("overwrite") as tid:
        with tracer.span(tracer.TYPE_AGENT, name="diagnose") as node:
            with tracer.span(tracer.TYPE_LLM, name="chat") as llm:
                llm.set_usage({"total_tokens": 449})
            assert node.usage["total_tokens"] == 449      # 归并已完成
            node.set_usage({"total_tokens": 0})           # v1 的写法

    spans = _spans_of(trace_file, tid)
    assert spans["diagnose"]["usage"]["total_tokens"] == 0, "覆盖语义变了 —— 见 docstring"
    assert spans["chat"]["usage"]["total_tokens"] == 449, "叶子 span 自己没被改坏"


# ============================================================
# 三、唯一事实源（supervisor.py:414-424 的 v1/v2/v3 教训）
# ============================================================
def test_agent_span_does_not_add_the_engines_self_reported_usage(fake_chat, trace_file):
    """节点级 agent span 的用量**只来自子 span 归并**，不叠加引擎自报的整轮汇总。

    这条覆盖 supervisor 的 v1/v2/v3：
        v1 set_usage(out.usage) → 覆盖，把子 span 归并上来的清零
        v2 add_usage(out.usage) → 累加，但引擎自报值与归并值重复 → 成本翻倍
        v3 什么都不加          → 叶子 span 是唯一事实源

    造法与生产一致：节点里调用模型（engine_usage 是引擎自报的整轮汇总，
    比叶子求和更"大"），节点 wrapper **不把它加进 span**。
    若有人加回去，agent span 就会变成 999+120 —— 这条用例会红。
    """
    fake_chat.push("ok")                                  # 确保不误触真实模型
    with tracer.trace("source-of-truth") as tid:
        with tracer.span(tracer.TYPE_AGENT, name="diagnose"):
            engine_usage = fake_chat.chat([{"role": "user", "content": "查磁盘"}])
            with tracer.span(tracer.TYPE_LLM, name="chat") as llm:
                llm.set_usage({"prompt_tokens": 100, "completion_tokens": 20,
                               "total_tokens": 120})
            # ★ 这里刻意没有 add_usage(engine_usage)：引擎自报的用量
            #   仍然保留在返回值里给人看，但不进 trace。
            assert engine_usage is not None

    rec = next(r for r in _trace_records(trace_file) if r["trace_id"] == tid)
    agent_span = next(s for s in rec["spans"] if s["name"] == "diagnose")
    assert agent_span["usage"]["total_tokens"] == 120, "引擎自报值被叠加进来了（v2 回归）"
    assert rec["usage"]["total_tokens"] == 120


# ============================================================
# 四、异常：记下来，然后原样抛出去
# ============================================================
def test_a_failing_span_is_recorded_as_error_and_the_exception_still_raises(trace_file):
    """观测层最恶劣的错误是**吞掉业务异常** —— 它会把故障变成静默的。

    所以：状态记 error + 错误摘要留痕（截断，免得一条坏 span 撑爆存储），
    但异常必须原样抛给业务。
    """
    class Boom(RuntimeError):
        pass

    long_message = "磁盘命令挂了 " + "x" * 1000
    with pytest.raises(Boom):
        with tracer.trace("failing") as tid:
            with tracer.span(tracer.TYPE_TOOL, name="check_disk"):
                raise Boom(long_message)

    rec = next(r for r in _trace_records(trace_file) if r["trace_id"] == tid)
    span = rec["spans"][0]
    assert span["status"] == "error"
    assert "磁盘命令挂了" in span["error"]
    assert rec["status"] == "error"
    assert "磁盘命令挂了" in rec["error"]
    assert len(span["error"]) <= 300 and len(rec["error"]) <= 300


def test_span_error_does_not_leak_into_later_spans(trace_file):
    """异常路径结束后上下文栈必须干净 —— 否则后续 span 会挂到僵尸父节点上。"""
    with pytest.raises(RuntimeError):
        with tracer.trace("t1"):
            with tracer.span(tracer.TYPE_TOOL, name="boom"):
                raise RuntimeError("x")
            # 这行不会执行；整条 trace 记录仍在 finally 里落盘
    with tracer.trace("t2") as tid2:
        with tracer.span(tracer.TYPE_TOOL, name="ok"):
            pass

    spans2 = _spans_of(trace_file, tid2)
    assert set(spans2) == {"ok"}
    assert spans2["ok"]["parent_id"] is None, "上一个用例的 span 栈漏过来了"


# ============================================================
# 五、没有 trace 上下文时的静默路径
# ============================================================
def test_span_without_a_trace_is_a_silent_noop(trace_file):
    """没包 trace 的调用点（如 /rag/ask）拿不到上下文：静默跳过，不抛错、不落盘。

    这不是错误路径，是正常路径 —— 加观测不该要求每个调用点都改签名。
    """
    with tracer.span(tracer.TYPE_TOOL, name="orphan") as sp:
        sp.set_usage({"total_tokens": 1})
        sp.set("host", "web-01")
        sp.set_error(RuntimeError("忽略"))
        assert sp.__class__.__name__ == "_NullSpan"
    assert not trace_file.exists(), "没有活跃 trace 却写了文件"


def test_records_written_before_a_trace_do_not_join_that_trace(trace_file):
    """散装 span（trace 之外写的）不能被算进下一条 trace 的 span 列表。

    真实场景：进程崩溃时未结束的 span 走兜底写入，它们是**另一条**记录。
    混进来的话，trace 的 span 列表里会出现不存在的节点（对账就再也说不清）。
    """
    _close_span_before_trace()
    with tracer.trace("clean") as tid:
        with tracer.span(tracer.TYPE_TOOL, name="mine"):
            pass

    rec = next(r for r in _trace_records(trace_file) if r["trace_id"] == tid)
    assert [s["name"] for s in rec["spans"]] == ["mine"]


# ============================================================
# 六、sum_usage / add_usage：累加前必须过滤非数值字段
# ============================================================
def test_sum_usage_filters_nested_and_non_numeric_fields():
    """`prompt_tokens_details` 是 dict，手写 `total + v` 会 TypeError。

    这是本项目**第二次踩同一个坑**（第一次在 add_usage）：
    dict 的键顺序恰好让前三个数值字段先归并成功、后面的缓存字段丢失 ——
    表现是"数据看起来是有的"，比整条丢失更难发现。
    """
    got = tracer.sum_usage([
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4},
         "completion_tokens": 2, "note": "abc", "flag": True, "score": 1.5},
        {"prompt_tokens": 5, "completion_tokens": None, "items": [1, 2]},
    ])
    assert got == {"prompt_tokens": 15, "completion_tokens": 2, "score": 1.5}


def test_sum_usage_keeps_cache_hit_tokens_that_a_naive_sum_would_drop():
    """真实的 DeepSeek usage 形状：缓存字段必须活到累加之后。

    顺序是 prompt / completion / total / prompt_tokens_details /
    prompt_cache_hit_tokens / prompt_cache_miss_tokens —— 一旦在 details 上崩掉，
    后两个（决定成本的那个折扣）就永远丢了 → 成本按"全部未命中"算 → 虚高。
    落盘数据还可能不是 dict（只读的是历史文件），必须跳过而不是抛异常。
    """
    u = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
         "prompt_tokens_details": {"cached_tokens": 800},
         "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}
    got = tracer.sum_usage([u, u])
    assert got["prompt_cache_hit_tokens"] == 1600
    assert got["prompt_cache_miss_tokens"] == 400
    assert tracer.sum_usage([None, "x", 3, {"a": 1}]) == {"a": 1}


def test_set_usage_overwrites_while_add_usage_accumulates(trace_file):
    """★ 两个方法的语义必须分开，不能靠调用方自觉。

    踩过的坑：chat 的 449 token 先归并进 intent 的 span，随后节点返回时
    `set_usage({})` 又把它覆盖成空 —— 同一个字段两种语义，必错。
    add_usage 还要顺手过滤非数值字段（`int + dict` 会崩在 span() 的
    `except: pass` 里，静默丢掉一半字段）。

    ★ 这里必须带 `trace_file`：`tracer.trace()` 一进一出就会写一条落盘记录，
      漏了隔离装置就会往真实 logs/traces.jsonl 里写字（本文件第一次写这组
      用例时真的漏了，等于用测试污染了生产可观测性数据）。
    """
    with tracer.trace("two-semantics"):
        with tracer.span(tracer.TYPE_AGENT, name="node") as sp:
            sp.add_usage({"total_tokens": 449})
            sp.set_usage({"total_tokens": 7})            # 覆盖：449 没了（如实固化）
            assert sp.usage == {"total_tokens": 7}
            sp.add_usage({"total_tokens": 3})            # 累加：7 + 3
            assert sp.usage == {"total_tokens": 10}
            sp.add_usage({"prompt_tokens_details": {"cached": 1}, "flag": True})
            assert sp.usage == {"total_tokens": 10}, "非数值字段混进来了"
            sp.set_usage("not-a-dict")                   # 脏输入忽略，不把 usage 弄坏
            assert sp.usage == {"total_tokens": 10}
    assert trace_file.exists(), "这段用例离开了隔离装置就写进真实 logs/ 了"


# ============================================================
# 七、agents.common 的用量累计器（trace 里的 usage 就是它产出的）
# ============================================================
def test_new_usage_shape_and_add_usage_tolerate_missing_fields_or_none():
    """累加器的初始形状 = "每次调用都会有的三个字段"，且累加要能容错。

    多轮循环的 token 是**累加**的 —— 这就是 Agent 比单轮问答贵的原因，
    所以这个形状本身是成本口径的一部分。
    缺字段按 0 补、usage 为 None 不炸：模型提供商少返字段是常态，
    为了一个 None 让整次诊断挂掉是本末倒置。
    """
    assert new_usage() == {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0}
    total = new_usage()
    add_usage(total, {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
    add_usage(total, {"prompt_tokens": 5, "total_tokens": 5})      # 少了 completion
    add_usage(total, None)  # type: ignore[arg-type]  # 故意传 None：容错是行为要求
    assert total == {"prompt_tokens": 15, "completion_tokens": 2, "total_tokens": 17}


def test_add_usage_does_not_carry_nested_details_into_the_accumulator():
    """只累计声明过的三个字段，嵌套的 details 不会混进 trace 的 usage。

    记录当前口径：token 级明细（缓存命中数等）**不进**这个累加器，
    所以 trace 的 usage 里看不到缓存字段 —— 缓存折扣只在叶子 span 上体现。
    这是已知的口径缺口，不是本文件的 bug，如实留档。
    """
    total = new_usage()
    add_usage(total, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                      "prompt_cache_hit_tokens": 999,
                      "prompt_tokens_details": {"cached_tokens": 999}})
    assert "prompt_cache_hit_tokens" not in total
    assert "prompt_tokens_details" not in total


# ============================================================
# 八、read_recent：只读尾部、跳过坏行
# ============================================================
def test_read_recent_returns_empty_when_the_file_is_missing(tmp_path, monkeypatch):
    """文件不存在不是错误（CI 里首跑就是这种状态），返回空列表。"""
    monkeypatch.setattr(tracer, "TRACE_PATH", tmp_path / "nope.jsonl")
    assert tracer.read_recent() == []


def test_read_recent_skips_corrupt_lines_instead_of_failing(tmp_path, monkeypatch):
    """一行坏数据不该弄挂查询接口 —— 跳过它，其余照读。"""
    path = tmp_path / "traces.jsonl"
    path.write_text('{"trace_id": "tr-1"}\n'
                    '{ 这不是合法 JSON\n'
                    '\n'
                    '{"trace_id": "tr-2"}\n', encoding="utf-8")
    monkeypatch.setattr(tracer, "TRACE_PATH", path)
    assert [r["trace_id"] for r in tracer.read_recent()] == ["tr-1", "tr-2"]


def test_read_recent_takes_the_tail_and_honours_limit(tmp_path, monkeypatch):
    """从尾部取 N 条 —— 查"最近发生了什么"时，老的记录不该挤掉新的。"""
    path = tmp_path / "traces.jsonl"
    path.write_text("".join(f'{{"n": {i}}}\n' for i in range(10)), encoding="utf-8")
    monkeypatch.setattr(tracer, "TRACE_PATH", path)
    assert [r["n"] for r in tracer.read_recent(limit=3)] == [7, 8, 9]
    assert len(tracer.read_recent(limit=50)) == 10
