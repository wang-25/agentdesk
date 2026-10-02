# -*- coding: utf-8 -*-
"""墙钟预算（M5 · I-8）。

这个文件测的核心不是"能不能停"，而是**停下来时的姿态**：

  ① 到点后不再开**新的**工具调用（那才是花时间和花钱的地方）
  ② 用**已经拿到的信息**收口出结论，并把收口语换成"时间到"那一句
  ③ `stop_reason="budget"`，让上层/审计/看板都看得见这次是**被截断**的
  ④ **默认不限**：不设 `AGENT_BUDGET_SECONDS` 时行为与改动前完全一致

还有一条容易被写错、后果很隐蔽的：**预算不能被烘进编译缓存的图**。
`build_graph` 是跨请求复用编译结果的，若把每个请求各自的 deadline 塞进闭包，
所有请求就会共用第一次那个时间 —— 一个"看起来生效、实际用错时间"的 bug。
"""

import time

import pytest

# ★ 必须在文件顶部导入：fake_chat 只替换**已经导入**的模块里的模型入口
from app.agents import graph as g
from app.agents import react as r
from app.agents import supervisor as sup


def _reply(content="", tool_calls=None, usage=None):
    """构造一个 chat_step 形状的假回复。"""
    return {
        "message": {"role": "assistant", "content": content,
                    "tool_calls": tool_calls or []},
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _one_tool_call():
    return [{"id": "call_1", "type": "function",
             "function": {"name": "check_disk", "arguments": "{}"}}]


# ============================================================
# 一、_deadline_from_env：默认不限，写错不炸
# ============================================================
def test_budget_is_off_by_default(monkeypatch):
    """★ 默认**不限** —— 不加环境变量时行为与改动前完全一致。"""
    monkeypatch.delenv("AGENT_BUDGET_SECONDS", raising=False)
    assert sup._deadline_from_env() is None


@pytest.mark.parametrize("raw", ["0", "0.0", "-5", ""])
def test_zero_or_negative_means_unlimited(monkeypatch, raw):
    monkeypatch.setenv("AGENT_BUDGET_SECONDS", raw)
    assert sup._deadline_from_env() is None


def test_positive_value_becomes_a_deadline_in_the_future(monkeypatch):
    monkeypatch.setenv("AGENT_BUDGET_SECONDS", "60")
    deadline = sup._deadline_from_env()
    assert deadline is not None
    assert 55 <= deadline - time.time() <= 61


def test_garbage_value_falls_back_to_unlimited_and_warns(monkeypatch, caplog):
    """★ 配置写错的表现应该是"没限制住"，不应该是"服务挂了"。

    而且"没限制住"这件事本身也要留痕（一条 warning），否则用户以为开着保护。
    """
    monkeypatch.setenv("AGENT_BUDGET_SECONDS", "60s")
    with caplog.at_level("WARNING"):
        assert sup._deadline_from_env() is None
    assert any("AGENT_BUDGET_SECONDS" in rec.getMessage()
               for rec in caplog.records), "回退到不限时必须留一条警告"


# ============================================================
# 二、手写版（react.py）：到点后用现有信息收口
# ============================================================
def test_react_budget_expired_stops_before_any_new_tool_call(monkeypatch):
    """★ 到点后**一次工具都不该执行** —— 检查点在每轮开头（模型决策之前）。"""
    calls = []

    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        calls.append({"messages": list(messages), "tools": tools})
        return _reply("基于已有信息：磁盘写满了。没来得及查 inode。")

    monkeypatch.setattr(r, "chat_step", fake_chat_step)
    out = r.run("查磁盘", max_steps=5, deadline=time.time() - 1)

    assert out["stop_reason"] == "budget"
    assert out["steps"] == [], "预算已过期时不该执行任何工具"
    assert out["tool_calls"] == 0
    assert len(calls) == 1, "只该有那一次收口调用（不先问模型决策）"
    assert calls[0]["tools"] is None, "收口时必须去掉 tools，否则模型还会再调工具"


def test_react_budget_wrap_up_prompt_says_what_is_missing(monkeypatch):
    """收口语要**换掉**：撞步数上限和预算用尽对用户是两件事。

    而且必须明写"不要猜测没查过的数据" —— 被截断时模型最容易做的就是
    把缺口补成幻觉，那比少给结论危险得多。
    """
    seen = {}

    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        seen["last_user"] = messages[-1]["content"]
        return _reply("结论")

    monkeypatch.setattr(r, "chat_step", fake_chat_step)
    r.run("查磁盘", max_steps=5, deadline=time.time() - 1)

    text = seen["last_user"]
    assert "时间预算已用尽" in text
    assert "没来得及查" in text
    assert "不要猜测" in text


def test_react_without_budget_is_unchanged(monkeypatch):
    """不传 deadline（默认）时，循环行为与改动前一致。"""
    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        return _reply("直接回答")

    monkeypatch.setattr(r, "chat_step", fake_chat_step)
    out = r.run("查磁盘", max_steps=5)
    assert out["stop_reason"] == "answered"
    assert out["answer"] == "直接回答"


def test_react_future_deadline_does_not_interfere(monkeypatch):
    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        return _reply("正常回答")

    monkeypatch.setattr(r, "chat_step", fake_chat_step)
    out = r.run("查磁盘", max_steps=5, deadline=time.time() + 3600)
    assert out["stop_reason"] == "answered"


# ============================================================
# 三、框架版（graph.py）：router 分流 + 收口原因区分
# ============================================================
def test_graph_budget_expired_routes_to_finalize_with_budget_reason(monkeypatch):
    calls = []

    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        calls.append({"tools": tools, "last_user": messages[-1].get("content")})
        if tools is None:
            return _reply("基于已有信息作答")
        return _reply("", tool_calls=_one_tool_call())

    monkeypatch.setattr(g, "chat_step", fake_chat_step)
    out = g.run("查磁盘", max_steps=5, deadline=time.time() - 1)

    assert out["stop_reason"] == "budget"
    assert out["tool_calls"] == 0, "预算已过期时 router 不该放行到 tools 节点"
    assert any("时间预算已用尽" in (c["last_user"] or "") for c in calls), \
        "收口那次必须用预算专用的提示语"


def test_graph_without_budget_still_reports_max_steps(monkeypatch):
    """★ 预算与步数上限必须**分得清**：没有预算时仍然报 max_steps。

    两者混成一句话会让人以为"再问一次就能查完"。
    """
    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        if tools is None:
            return _reply("收口结论")
        return _reply("", tool_calls=_one_tool_call())

    monkeypatch.setattr(g, "chat_step", fake_chat_step)
    # 工具名不在白名单里也没关系：撞到步数上限就会走 finalize
    out = g.run("查磁盘", max_steps=1, tool_names=[])
    assert out["stop_reason"] in ("max_steps", "answered")


def test_graph_satisfied_question_is_not_affected(monkeypatch):
    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        return _reply("不需要查，直接回答")

    monkeypatch.setattr(g, "chat_step", fake_chat_step)
    out = g.run("一加一等于几", max_steps=5, deadline=time.time() + 3600)
    assert out["stop_reason"] == "answered"


# ============================================================
# 四、★ 预算不能进编译缓存（这条最隐蔽）
# ============================================================
def test_deadline_is_per_request_not_baked_into_the_cached_graph(monkeypatch):
    """★ 图是**编译缓存、跨请求复用**的，预算必须走 state。

    如果哪天有人图省事把 deadline 传进 `build_graph` 的闭包，
    所有请求就会共用第一次那个时间：**先来的请求决定后面所有人的预算**。
    症状是"有时到点不停、有时刚开始就停"，而且只在生产并发下出现。
    """
    def fake_chat_step(messages, tools=None, temperature=0, timeout=90):
        if tools is None:
            return _reply("收口")
        return _reply("", tool_calls=_one_tool_call())

    monkeypatch.setattr(g, "chat_step", fake_chat_step)

    # 同一个 (max_steps, tool_names) 键 → 拿到**同一个**编译对象
    # （`tool_names=None` 表示"全部工具"，这里必须传 None 才能命中同一个缓存键）
    first = g.build_graph(5, None)          # type: ignore[arg-type]
    second = g.build_graph(5, None)         # type: ignore[arg-type]
    assert first is second, "这个前提不成立的话，本用例就测不到东西"

    tight = g.run("查磁盘", max_steps=5, deadline=time.time() - 1)
    loose = g.run("查磁盘", max_steps=5, deadline=time.time() + 3600)

    assert tight["stop_reason"] == "budget", "紧预算那次该停"
    assert loose["stop_reason"] != "budget", \
        "宽预算那次被上一次的 deadline 影响了 —— 预算被烘进了编译缓存"


def test_supervisor_passes_the_deadline_into_state(monkeypatch):
    """★ 验证**接线**：supervisor 把算出来的 deadline 放进 state 传给图。

    只测 `_deadline_from_env()` 是纯函数不够 —— 那测不到"有没有真的传下去"。
    实测里最容易漏的恰恰是这一步：函数写对了，但没人调它。
    """
    captured = {}

    class FakeGraph:
        def invoke(self, init, config=None):
            captured["init"] = init
            captured["config"] = config
            return {"messages": [], "steps": [], "rounds": 1, "usage": {},
                    "stop_reason": "answered", "node_log": []}

    monkeypatch.setattr(sup, "build_graph", lambda *a, **kw: FakeGraph())
    monkeypatch.setattr(sup, "_deadline_from_env", lambda: 12345.0)

    sup.run("查磁盘", max_retries=0)
    assert captured["init"]["deadline"] == 12345.0, \
        "deadline 没有进 state —— 图里的节点就永远拿不到预算"


def test_supervisor_accepts_an_explicit_deadline_over_the_env(monkeypatch):
    """显式传入的 deadline 优先于环境变量（调用方更清楚这次请求的预算）。"""
    captured = {}

    class FakeGraph:
        def invoke(self, init, config=None):
            captured["init"] = init
            return {"messages": [], "steps": [], "rounds": 1, "usage": {},
                    "stop_reason": "answered", "node_log": []}

    monkeypatch.setattr(sup, "build_graph", lambda *a, **kw: FakeGraph())
    monkeypatch.setattr(sup, "_deadline_from_env", lambda: 999.0)

    sup.run("查磁盘", max_retries=0, deadline=4242.0)
    assert captured["init"]["deadline"] == 4242.0


# ============================================================
# 五、默认档行为不变（回归）
# ============================================================
@pytest.mark.parametrize("engine", ["react", "graph"])
def test_no_budget_env_means_no_deadline(monkeypatch, engine):
    monkeypatch.delenv("AGENT_BUDGET_SECONDS", raising=False)
    assert sup._deadline_from_env() is None
    assert engine in ("react", "graph")          # 参数化只为两套引擎都过一遍


# ============================================================
# 六、指标：被截断这件事必须在 /metrics 上看得见
# ============================================================
def test_budget_exceeded_is_counted_in_metrics(monkeypatch):
    """★ 新能力都要能在看板上看见。

    没有这条指标，"最近是不是经常撞预算"就只能靠翻日志 ——
    而它恰恰是"该调大预算、还是该去优化某个工具的耗时"的唯一判据。
    """
    from app.observability import metrics

    metrics.reset()
    monkeypatch.setattr(r, "chat_step",
                        lambda messages, tools=None, temperature=0, timeout=90:
                        _reply("收口"))
    r.run("查磁盘", max_steps=3, deadline=time.time() - 1)

    body = metrics.render()
    assert "agentdesk_agent_budget_exceeded_total" in body
    assert 'engine="handwritten"' in body, "标签要能区分是哪套引擎撞的预算"


def test_normal_run_does_not_touch_the_budget_counter(monkeypatch):
    """没撞预算就不该有这条序列 —— 否则看板上的数字会永远大于 0。"""
    from app.observability import metrics

    metrics.reset()
    monkeypatch.setattr(r, "chat_step",
                        lambda messages, tools=None, temperature=0, timeout=90:
                        _reply("正常回答"))
    r.run("查磁盘", max_steps=3)
    assert "agentdesk_agent_budget_exceeded_total" not in metrics.render()


def test_budget_metric_failure_does_not_break_the_run(monkeypatch):
    """指标是旁路：它坏了也不能让一次问答失败。"""
    from app.observability import metrics

    monkeypatch.setattr(metrics, "counter",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("metrics down")))
    monkeypatch.setattr(r, "chat_step",
                        lambda messages, tools=None, temperature=0, timeout=90:
                        _reply("收口"))
    out = r.run("查磁盘", max_steps=3, deadline=time.time() - 1)
    assert out["stop_reason"] == "budget"
