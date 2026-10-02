# -*- coding: utf-8 -*-
"""
Supervisor 多 Agent 编排
============================================================
在单 Agent ReAct 循环外面再套一层调度：**谁来做、按什么顺序做、做完要不要重来。**

```
                      START
                        │
                ┌───────▼────────┐
                │   supervisor   │  看"流程状态"，决定下一步派给谁
                │  （只做决策）   │  每次决策都记下理由，可审计
                └───────┬────────┘
        ┌───────┬───────┼────────┬────────┬────────┐
        ▼       ▼       ▼        ▼        ▼        ▼
     intent  knowledge diagnose  reason  verify  finalize
        │       │       │        │        │        │
        └───────┴───────┴────────┴────────┴────────┘
                        │  每个专业 Agent 干完都回到 supervisor
                        ▼
                       END
```

================================================================
★ 三个必须想清楚的设计问题
================================================================

【问题一：Supervisor 用规则决策，还是让模型决策？】

两种都能做，我选了**规则**。理由：

    规则决策                          模型决策
    ─────────────────────────────    ─────────────────────────────
    可预测、可复现、零额外成本         灵活，能处理没预想到的路径
    出错时能一眼看出是哪条规则错了      出错时你不知道它"为什么"这么选
    容易测（给状态，断言下一个节点）    不容易测（要造状态还得看运气）

关键判断：**这套流程的步骤是确定的。**
意图 → 知识 → 诊断 → 校验 → 汇总，这个骨架不会因为问题内容而变。
变化的只是"要不要查知识库""是查现场还是纯推理"——这些由**意图路由的结果**驱动。

**用模型去决定一个已经确定的事情，只会引入不确定性，不会带来能力。**
等出现真正需要动态规划的场景（比如并行派给多个 Agent、或者任务可以递归分解），
再换成模型决策。

Supervisor 依然是个**节点**而不是一条普通的条件边 —— 因为这样它的
每一次决策都能写进状态（决策 + 理由），轨迹里看得到、审计日志里留得下。
**"为什么它决定走这条分支"必须是可回答的。**

【问题二：Supervisor 和「意图路由 Agent」重复吗？】

不重复，它们在不同的层：

    Supervisor        管编排：已经做过哪几步、还差哪几步、失败了要不要重来
                      看的是**流程状态**
    意图路由 Agent     管语义：这是诊断还是咨询、涉及哪台机器、要不要查资料
                      看的是**问题内容**

分开的理由是**可测性**：意图分类可以单独算准确率（见 eval/specialists_set.json），
而如果分类逻辑埋在 Supervisor 的决策里，你只能端到端跑一遍才知道准不准。

【问题三：校验不通过怎么办？】

回到"工具执行 Agent"重来，并把**上一次的问题**告诉它 ——
不是简单重复一遍，而是让它换个角度查。
重来次数有上限（max_retries），到顶了就带着"未通过"的标记出结论。

**这是"循环"在真实系统里的样子**：不是为了炫技，是因为
"结论站不住就得重查"是诊断这件事的固有需求。
"""

import logging
import operator
import os
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.common import new_usage
from app.agents.specialists import (
    DIAGNOSE_TOOLS,
    compose_answer,
    diagnose,
    remediate,
    retrieve_knowledge,
    route_intent,
    verify,
)
from app.agents.graph import _merge_usage
from app.observability import tracer

# ============================================================
# 一、状态
# ============================================================
class MultiAgentState(TypedDict):
    """多 Agent 流程的状态。

    【和单 Agent 的 AgentState 有什么不同】
    单 Agent 的状态核心是 `messages`（一条对话历史）。
    多 Agent 的状态核心是**各 Agent 的产出**：
        intent     意图标签
        knowledge  知识库检索结果
        diagnosis  诊断结论 + 证据
        verdict    校验结论
    messages 反而退居其次 —— 因为每个 Agent 的对话历史是**各自独立**的
    （这正是"上下文隔离"的实现方式）。

    另外多了三样编排才需要的东西：
        visited    访问过哪些节点（Supervisor 靠它判断"还差哪几步"）
        retries    重来了几次（上限控制）
        node_log   每一步的耗时与摘要（可观测性的最小实现）
    """
    question: str
    intent: dict
    knowledge: dict
    diagnosis: dict
    verdict: dict
    remediation: dict      # ★ 处置结果（提交了哪些审批 / 执行了什么）
    next_step: str
    supervisor_reason: str
    visited: Annotated[list, operator.add]
    retries: Annotated[int, operator.add]
    node_log: Annotated[list, operator.add]
    usage: Annotated[dict, _merge_usage]
    answer: str
    stop_reason: str


def _log(node: str, ok: bool, elapsed_ms: int, summary: str) -> list:
    """统一的节点日志格式 —— 汇总、打印、审计都用同一份。"""
    return [{"node": node, "ok": bool(ok), "elapsed_ms": int(elapsed_ms),
             "summary": summary[:200]}]


# ============================================================
# 二、Supervisor 节点（只做决策，不干活）
# ============================================================
def _plan_for(intent: dict) -> str:
    """按意图决定"现场诊断"该走哪个模式。

    ★ 这是一个值得单独说的设计点：
      「工具执行 Agent」有**两种模式**，共用同一份实现：

        diagnose  查现场 —— 带 5 个运维工具
        reason    纯推理 —— 工具清单为空

      为什么需要"纯推理"模式？
      「explain」类问题（比如"为什么清理日志要用 truncate"）根本不需要查机器。
      硬要它去查一轮，既浪费钱又可能查出一堆无关数据干扰回答。

      空工具清单不是"禁用工具"，而是让这个 Agent **退化成纯推理** ——
      它仍然拿到知识库的经验，只是没有手可以伸出去。

      **能用结构约束的，就不要靠提示词请求。** 给它 tools=[] 比在提示词里
      写"这个问题请不要调工具"可靠得多。
    """
    return "diagnose" if intent.get("needs_live_data") else "reason"


def make_supervisor_node(max_retries: int):
    """Supervisor 的决策逻辑（规则版）。"""

    def supervisor_node(state: MultiAgentState) -> dict:
        intent = state.get("intent") or {}
        verdict = state.get("verdict") or {}
        visited = list(state.get("visited") or [])
        counts = {n: visited.count(n) for n in set(visited)}
        target_mode = _plan_for(intent)

        def decide(node: str, reason: str) -> dict:
            # 每条决策都带理由 —— 这条理由会进轨迹、进审计日志。
            # 「它为什么走这条分支」永远是可回答的。
            return {"next_step": node, "supervisor_reason": reason,
                    "retries": 0, "visited": ["supervisor"],
                    "node_log": _log("supervisor", True, 0,
                                     f"→ {node}｜{reason}")}

        # 1. 还没做意图分类
        if not counts.get("intent"):
            return decide("intent", "先做意图分类，才能决定后续要不要查资料/查现场")

        # 2. 需要经验且还没查
        if intent.get("needs_knowledge") and not counts.get("knowledge"):
            return decide("knowledge",
                          f"意图标注 needs_knowledge=true（依据：{intent.get('reason')}）")

        # 3. 还没跑诊断（按模式走 diagnose 或 reason）
        if not counts.get(target_mode):
            why = ("需要现场数据" if target_mode == "diagnose"
                   else "不需要现场数据，走纯推理模式")
            return decide(target_mode, why)

        # 4. 还没校验
        if not counts.get("verify"):
            return decide("verify", "结论出来了，先校验再交给用户")

        # 5. 校验没过，且还有重试额度 → 回到诊断重来
        attempts = counts.get(target_mode, 0)
        if not verdict.get("pass") and (attempts - 1) < max_retries:
            problems = "；".join(verdict.get("problems") or [])[:120]
            # ★ 注意 retries 返回 1 —— 诊断节点靠它判断"这是重来"，
            #   并把上一次的问题作为提示带进去。
            out = decide(target_mode,
                         f"校验未通过（第 {attempts} 次），重查：{problems}")
            out["retries"] = 1
            return out

        # 6. ★ 需要处置，且校验已通过 → 派给处置 Agent
        #
        #    ★★ 这里有一道安全规则，是本项目刻意加的：
        #
        #       **结论没过校验，就不许据此动手。**
        #
        #       推理很简单：处置动作是「基于诊断结论」的。
        #       如果校验 Agent 说「这个结论的数值找不到出处 / 因果链不成立」，
        #       那它给出的建议同样可疑 —— 这时候去执行它，等于把
        #       「没被验证的结论」直接变成了「对生产系统的改动」。
        #
        #       代价是：有些本来能自动修的问题，会因为结论表述不严谨而不动手。
        #       这个代价我认为值得付 —— **宁可少修一次，不能修错一次。**
        #       而人看到「结论未通过校验」这条提示，可以自己判断要不要动手。
        needs_remediate = intent.get("task_type") == "remediate"
        if needs_remediate and verdict.get("pass") and not counts.get("remediate"):
            return decide(
                "remediate",
                "意图为 remediate 且诊断结论已通过校验"
                f"（依据：{intent.get('reason') or '-'}）")

        # 7. 全部走完（或重试用尽）→ 汇总
        if not verdict.get("pass"):
            # 若意图要求处置但结论没过校验，必须明确说明「没有动手」
            tail = ("；且因结论未通过校验，**未执行任何处置动作**"
                    if needs_remediate and not counts.get("remediate") else "")
            return decide("finalize",
                          f"校验仍未通过且重试额度已用尽（{attempts - 1}/{max_retries}），"
                          f"带标记出结论{tail}")
        if needs_remediate and not counts.get("remediate"):
            return decide("finalize", "意图要求处置但未满足处置条件，只出结论")
        return decide("finalize", "校验通过，汇总输出")

    return supervisor_node


# ============================================================
# 三、四个专业 Agent 对应的节点
# ============================================================
def intent_node(state: MultiAgentState) -> dict:
    t0 = time.time()
    result = route_intent(state["question"])
    intent = result["intent"]
    elapsed = int((time.time() - t0) * 1000)

    degraded = intent.get("_degraded")
    summary = (f"{intent['task_type']}｜主机 {intent['hosts'] or '-'}"
               f"｜服务 {intent['services'] or '-'}"
               f"｜知识库 {'要' if intent['needs_knowledge'] else '不要'}"
               f"｜尝试 {result['attempts']} 次"
               + ("｜已降级" if degraded else ""))
    if result["problems"]:
        summary += f"｜问题：{'；'.join(result['problems'])[:80]}"

    return {
        "intent": intent,
        "visited": ["intent"],
        "node_log": _log("intent", not degraded, elapsed, summary),
    }


def knowledge_node(state: MultiAgentState) -> dict:
    t0 = time.time()
    knowledge = retrieve_knowledge(state["question"], state.get("intent") or {})
    elapsed = int((time.time() - t0) * 1000)
    ok = not knowledge.get("error")
    summary = (f"检索词「{knowledge['query']}」命中 {knowledge['count']} 条"
               f"｜{'、'.join(r['source'] for r in knowledge['results'])}")
    if knowledge.get("error"):
        summary = f"知识库不可用（已降级）：{knowledge['error']}"
    return {
        "knowledge": knowledge,
        "visited": ["knowledge"],
        "node_log": _log("knowledge", ok, elapsed, summary),
    }


def make_diagnose_node(tool_names: list, node_name: str):
    """「工具执行 Agent」节点工厂 —— 两种模式共用一份实现。"""

    def node(state: MultiAgentState) -> dict:
        t0 = time.time()
        verdict = state.get("verdict") or {}
        previous = state.get("diagnosis") or {}
        is_retry = bool(state.get("retries"))

        # 重试时要带上两样东西，缺一个"重试"就退化成"重来一遍"：
        #   1. 上一次的具体问题 → 知道要改什么
        #   2. 上一次已经查到的数据 → 知道不用再查什么
        result = diagnose(
            state["question"],
            state.get("intent") or {},
            state.get("knowledge") or {},
            max_steps=6 if tool_names else 1,
            tool_names=tool_names,
            previous_problems=(verdict.get("problems") or []) if is_retry else None,
            previous_evidence=previous.get("evidence") if is_retry else None,
            deadline=state.get("deadline"),
        )
        elapsed = int((time.time() - t0) * 1000)

        mode = "查现场" if tool_names else "纯推理"
        summary = (f"{mode}｜{result['rounds']} 轮 · {result['tool_calls']} 次工具调用"
                   f"｜{result['tools'] or '未用工具'}"
                   f"｜{len(result['answer'])} 字")
        if is_retry:
            summary += f"｜第 {state.get('retries')} 次重查（已带上上轮数据）"

        return {
            "diagnosis": result,
            "visited": [node_name],
            "usage": result.get("usage") or {},
            "node_log": _log(node_name, bool(result["answer"]), elapsed, summary),
        }

    return node


def verify_node(state: MultiAgentState) -> dict:
    t0 = time.time()
    verdict = verify(state["question"], state.get("diagnosis") or {},
                     state.get("intent") or {})
    elapsed = int((time.time() - t0) * 1000)
    mark = "通过" if verdict["pass"] else "未通过"
    summary = f"{mark}（{verdict['source']}）"
    if verdict.get("problems"):
        summary += f"｜{'；'.join(verdict['problems'])[:120]}"
    elif verdict.get("note"):
        summary += f"｜{verdict['note']}"
    return {
        "verdict": verdict,
        "visited": ["verify"],
        "usage": verdict.get("usage") or {},
        "node_log": _log("verify", verdict["pass"], elapsed, summary),
    }


def make_remediate_node():
    """处置 Agent 节点。

    ★ 它只在校验通过之后才会被调到 —— 这条约束写在 Supervisor 的决策里，
      不在这里。**约束写在一处，比写两处可靠**：
      如果这里也写一遍 `if not verdict.pass: return`，那两处就有不一致的可能，
      而"同一条件判断两遍"正是最容易出现分歧的地方。
    """

    def node(state: MultiAgentState) -> dict:
        t0 = time.time()
        result = remediate(state["question"], state.get("intent") or {},
                           state.get("diagnosis") or {})
        elapsed = int((time.time() - t0) * 1000)

        n_ap = len(result["approvals"])
        n_ex = len(result["executed"])
        n_dn = len(result["denied"])
        summary = (f"提交审批 {n_ap} 条 · 已执行 {n_ex} 条 · 被拒 {n_dn} 条"
                   f"｜{result['tool_calls']} 次工具调用")
        if result["approvals"]:
            summary += ("｜待批：" + "、".join(
                a["approval_id"] for a in result["approvals"]))

        return {
            "remediation": result,
            "visited": ["remediate"],
            "usage": result.get("usage") or {},
            "node_log": _log("remediate", bool(result["answer"]), elapsed, summary),
        }

    return node


def make_finalize_node(config: dict):
    def finalize_node(state: MultiAgentState) -> dict:
        t0 = time.time()
        answer = compose_answer(
            state["question"], state.get("intent") or {},
            state.get("knowledge") or {}, state.get("diagnosis") or {},
            state.get("verdict") or {}, config,
            state.get("remediation") or {})
        elapsed = int((time.time() - t0) * 1000)
        return {
            "answer": answer,
            "visited": ["finalize"],
            "node_log": _log("finalize", True, elapsed, f"{len(answer)} 字"),
        }

    return finalize_node


# ============================================================
# 四、编译成图
# ============================================================
_GRAPH_CACHE = {}


def build_graph(max_retries: int = 1, config: dict = None):
    config = config or {}
    key = (max_retries, tuple(sorted(config.items())))
    if key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]

    graph = StateGraph(MultiAgentState)

    # ★ 每个 Agent 节点记一个 span。
    #   这一步让「token 都花在哪个 Agent 上了」变成可回答的问题 ——
    #   /metrics/summary 按 span name 聚合，直接给出各 Agent 的成本占比。
    def _add(node_name: str, fn):
        def wrapped(state):
            # ★ 这里**不再**把 out["usage"] 加进 span —— 只靠子 span 归并。
            #
            #   这行经历过三个版本，每个版本都是一次真实的对账教训：
            #     v1 set_usage(out.usage)   → 覆盖，把子 span 归并上来的清零
            #     v2 add_usage(out.usage)   → 累加，但 out.usage 是引擎自报的
            #                                  整轮汇总，和归并值重复 → 成本翻倍
            #     v3 什么都不加             → 叶子 span（模型调用）是唯一事实源，
            #                                  节点用量 = 子 span 归并，和对账完全一致
            #
            #   **同一个量只能有一个事实源。** 引擎自报的 usage 仍然保留在
            #   返回值 / metrics 里给人看，但进 trace 的只有归并链路。
            with tracer.span(tracer.TYPE_AGENT, name=node_name) as sp:
                return fn(state)
        return wrapped


    graph.add_node("supervisor", _add("supervisor", make_supervisor_node(max_retries)))
    graph.add_node("intent", _add("intent", intent_node))
    graph.add_node("knowledge", _add("knowledge", knowledge_node))
    graph.add_node("diagnose", _add("diagnose", make_diagnose_node(DIAGNOSE_TOOLS, "diagnose")))
    graph.add_node("reason", _add("reason", make_diagnose_node([], "reason")))
    graph.add_node("verify", _add("verify", verify_node))
    graph.add_node("remediate", _add("remediate", make_remediate_node()))  # ★ 处置节点
    graph.add_node("finalize", _add("finalize", make_finalize_node(config)))

    graph.add_edge(START, "supervisor")

    # ★ Supervisor 的每条出边都是一个分支。决策写进 state.next_step，
    #   条件边只负责"照办" —— 决策逻辑和路由管道分开，各自可测。
    graph.add_conditional_edges(
        "supervisor", lambda s: s["next_step"],
        {"intent": "intent", "knowledge": "knowledge",
         "diagnose": "diagnose", "reason": "reason",
         "verify": "verify", "remediate": "remediate",
         "finalize": "finalize"},
    )

    # 每个专业 Agent 干完都回到 Supervisor —— 这就是多 Agent 的"循环"
    for node in ("intent", "knowledge", "diagnose", "reason", "verify",
                 "remediate"):
        graph.add_edge(node, "supervisor")
    graph.add_edge("finalize", END)

    compiled = graph.compile()
    _GRAPH_CACHE[key] = compiled
    return compiled


def mermaid(max_retries: int = 1) -> str:
    return build_graph(max_retries).get_graph().draw_mermaid()


# ============================================================
# 五、入口
# ============================================================
log = logging.getLogger("agentdesk.supervisor")


def _deadline_from_env() -> float:
    """从 `AGENT_BUDGET_SECONDS` 算墙钟预算的截止时刻。返回 None = 不限。

    ★ 默认就是"不限"（不设这个变量 / 设 0）：**行为与改动前完全一致**。
      给整条 Agent 链路默认加上时限是行为变更，不该悄悄发生 ——
      谁需要保护，谁显式打开。

    ★ 非法值（`"60s"`、负数）按"不限"处理并打一条警告，而不是让服务起不来：
      配置写错的表现应该是"没限制住"，不应该是"服务挂了"。
      没限制住这件事本身也看得见 —— `stop_reason` 不会是 budget，
      而 `/metrics` 上有 `agentdesk_agent_budget_exceeded_total`。
    """
    raw = (os.getenv("AGENT_BUDGET_SECONDS") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        log.warning("AGENT_BUDGET_SECONDS=%r 不是数字，按不限处理", raw)
        return None
    if seconds <= 0:
        return None
    return time.time() + seconds


@tracer.traced("supervisor")           # ★ 一次运行 = 一个 trace
def run(question: str, max_retries: int = 1, verbose: bool = False,
        config: dict = None, deadline: float = None) -> dict:
    """跑一次多 Agent 流程。

    返回结构兼容单 Agent 版（engine/question/answer/metrics/），
    另外多出 intent / knowledge / verdict / node_log —— 那些是编排层的产物。
    """
    config = config or {}
    started = time.time()
    # 预算只在这里算一次，然后随 state 传下去 ——
    # 让各节点自己读环境变量的话，一次运行里可能跨过"配置被改"的边界，
    # 出现"前面节点不限、后面节点限 60 秒"这种没人查得出来的不一致。
    if deadline is None:
        deadline = _deadline_from_env()
    graph = build_graph(max_retries, config)

    init = {
        "question": question,
        "intent": {}, "knowledge": {}, "diagnosis": {}, "verdict": {},
        "remediation": {},
        "next_step": "", "supervisor_reason": "",
        "visited": [], "retries": 0, "node_log": [],
        "usage": new_usage(), "answer": "",
        "stop_reason": "answered",
        # ★ 墙钟预算的截止时刻；None = 不限（默认，行为与改动前一致）
        "deadline": deadline,
    }

    # recursion_limit 兜底：图上可能出现意料外的环，
    # 框架层直接中断，总比无限跑下去把额度烧光好。
    final = graph.invoke(
        init, config={"recursion_limit": 4 * (max_retries + 1) * 3 + 12})

    if verbose:
        log = final.get("node_log") or []
        print(f"\n──── 多 Agent 执行轨迹（{len(log)} 步）────")
        for item in log:
            mark = "✓" if item["ok"] else "✗"
            print(f"  {mark} {item['node']:12s} {item['elapsed_ms']:>5d}ms  "
                  f"{item['summary']}")

    diagnosis = final.get("diagnosis") or {}
    verdict = final.get("verdict") or {}
    remediation = final.get("remediation") or {}
    usage = final.get("usage") or new_usage()

    return _build_result(final, question, started)


def _build_result(final: dict, question: str, started: float) -> dict:
    """从图的终态构造返回结构 —— run() 与 run_stream() 共用。

    ★ 单独抽出来只有一个理由：流式版的最后一个事件必须与一次性版
      逐字段一致。同一个问题从两个入口进来给出不同结构，
      那是比少一个功能严重得多的对账问题。
    """
    log = final.get("node_log") or []
    diagnosis = final.get("diagnosis") or {}
    verdict = final.get("verdict") or {}
    remediation = final.get("remediation") or {}
    usage = final.get("usage") or new_usage()

    return {
        "engine": "supervisor",
        "question": question,
        "answer": final.get("answer") or "",
        # ---- 编排层产物 ----
        "intent": final.get("intent") or {},
        "intent_status": "degraded" if (final.get("intent") or {}).get("_degraded")
        else "ok",
        "knowledge": final.get("knowledge") or {},
        "verdict": verdict,
        "remediation": remediation,
        # ★ 待人工确认的审批单。这张列表就是「Agent 想做什么但还没做」的
        #   全部内容 —— 它也是 /agent/ask 响应里最该被人看一眼的字段。
        "pending_approvals": remediation.get("approvals") or [],
        "executed_commands": remediation.get("executed") or [],
        "denied_commands": remediation.get("denied") or [],
        "node_log": log,
        "path": [item["node"] for item in log if item["node"] != "supervisor"],
        "supervisor_decisions": [
            item["summary"] for item in log if item["node"] == "supervisor"],
        # ---- 兼容单 Agent 版的字段（对比脚本/接口可直接复用）----
        "steps": diagnosis.get("steps") or [],
        "rounds": diagnosis.get("rounds", 0),
        "tool_calls": diagnosis.get("tool_calls", 0),
        "distinct_tools": diagnosis.get("tools") or [],
        "usage": usage,
        "elapsed_ms": int((time.time() - started) * 1000),
        "stop_reason": ("awaiting_approval" if remediation.get("approvals")
                        else "verification_failed" if not verdict.get("pass")
                        else "answered"),
        "agent_count": 5,
        "node_count": len([n for n in (final.get("visited") or [])
                           if n != "supervisor"]),
    }




def run_stream(question: str, max_retries: int = 1, config: dict = None):
    """run() 的生成器版：每完成一个节点产出一个事件，最后一个事件是完整结果。

    事件两种：
      {"type": "node",   "node": "intent", "elapsed_ms": 812}
      {"type": "result", ...与 run() 返回逐字段一致...}

    ★ 实现要点：stream_mode 用 "values" 而不是 "updates" ——
      本 state 的 visited / node_log / usage 都带 reducer，updates 给的是
      增量，自己合并等于复刻框架的合并语义（第一版实测 usage 被覆盖成 0）。
      values 模式每步给全量 state，最后一块天然与 invoke 终态一致；
      节点事件从 visited 的增长推出。细节见函数体内的注释。
    """
    config = config or {}
    started = time.time()
    graph = build_graph(max_retries, config)

    init = {
        "question": question,
        "intent": {}, "knowledge": {}, "diagnosis": {}, "verdict": {},
        "remediation": {},
        "next_step": "", "supervisor_reason": "",
        "visited": [], "retries": 0, "node_log": [],
        "usage": new_usage(), "answer": "",
        "stop_reason": "answered",
    }

    # ★ 用 values 模式而不是 updates：
    #   本 state 的 visited / node_log / usage 都带 reducer（operator.add /
    #   _merge_usage），updates 模式给的是「增量」，自己合并就得把 reducer
    #   语义重新实现一遍 —— 实测我第一版用 dict.update，usage 被最后一次
    #   增量覆盖成 0，与 invoke 的结果对不上。**与其复刻框架的合并语义，
    #   不如让它自己给完整状态**：values 模式每步产出全量 state，
    #   最后一块天然与 invoke 的终态逐字段一致（下方有对账测试）。
    #   节点事件从 visited 的增长推出 —— 每个节点跑完都会把自己名字加进去。
    final: dict = {}
    prev_visited = 0
    step_started = time.time()
    for chunk in graph.stream(
            init,
            config={"recursion_limit": 4 * (max_retries + 1) * 3 + 12},
            stream_mode="values"):
        final = chunk
        visited = chunk.get("visited") or []
        if len(visited) > prev_visited:
            now = time.time()
            nlog = chunk.get("node_log") or []
            last = nlog[-1] if nlog else {}
            summary = last.get("summary", "") if last.get("node") == visited[-1] else ""
            yield {"type": "node", "node": visited[-1],
                   "elapsed_ms": int((now - step_started) * 1000),
                   "summary": summary}
            step_started = now
        prev_visited = len(visited)

    if final:
        yield {"type": "result", **_build_result(final, question, started)}
def _main(argv=None):
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Supervisor 多 Agent 流程（意图路由 / 知识检索 / 工具执行 / 结果校验）")
    parser.add_argument("question", nargs="?", help="要诊断的问题")
    parser.add_argument("--max-retries", type=int, default=1,
                        help="校验不通过时最多重查几次，默认 1")
    parser.add_argument("--graph", action="store_true",
                        help="只打印编排图的 mermaid 定义")
    args = parser.parse_args(argv)

    if args.graph:
        print(mermaid(args.max_retries))
        return 0

    if not args.question:
        parser.error("需要提供 question（或用 --graph 看图）")

    result = run(args.question, max_retries=args.max_retries, verbose=True)

    print("\n" + "=" * 62)
    print("  最终回答")
    print("=" * 62)
    print(result["answer"])
    print("\n" + "=" * 62)
    print(f"  路径：{' → '.join(result['path'])}")
    if result["pending_approvals"]:
        print("\n  待人工确认（批准后才会执行）：")
        for a in result["pending_approvals"]:
            print(f"    · {a['approval_id']}  {a['command']}")
            print(f"       原因：{a['reason']}")
        print("    → 查看：GET  /approvals?status=pending")
        print("    → 批准：POST /approvals/{id}/approve")
        print("    → 执行：POST /approvals/{id}/execute")
    print(f"  工具调用 {result['tool_calls']} 次 · "
          f"token {result['usage'].get('total_tokens', 0)} · "
          f"耗时 {result['elapsed_ms']}ms")
    print(f"  校验：{'通过' if result['verdict'].get('pass') else '未通过'}"
          f"（{result['verdict'].get('source')}）"
          f"　停止原因 {result['stop_reason']}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
