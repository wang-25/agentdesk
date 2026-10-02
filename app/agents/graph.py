# -*- coding: utf-8 -*-
"""
LangGraph 版 ReAct —— 同样的行为，用状态图表达
============================================================
这个文件做的是和 react.py **一模一样**的事，只是换成用 LangGraph 写。
写它的目的不是"框架更高端"，而是用两个版本对照，看清框架到底替你做了什么。

【LangGraph 的核心思想：把循环画成图】
手写版里，控制流藏在 `for` 循环和 `break` 里 —— 你得读代码才知道怎么走。
LangGraph 把它显式化成节点和边：

        ┌─────────┐
   START│  agent  │  ← 调模型，决定"回答"还是"调工具"
        └────┬────┘
             │  有 tool_calls 且未超上限？
        ┌────┴─────┐
        │          │ 是              否
        ↓          ↓
    ┌───────┐   ┌──────────┐
    │ tools │   │ finalize │ ← 超上限时强制收口（不带工具）
    └───┬───┘   └────┬─────┘
        │            │
        └──→ agent   └──→ END

**这张图的形状，就是手写版那个 while 循环的等价物。**
`add_conditional_edges` 就是那个 `if`；`add_edge("tools", "agent")` 就是 `continue`。

【框架相比手写版多给了什么】—— 这才是"什么时候该用框架"的答案
    1. 状态管理：哪一步产出什么、怎么合并，声明式写清楚（reducers）
    2. 检查点（checkpointing）：每一步都能落盘，崩了能从中断处续跑
       ★ 这个是手写版最难自己做的 —— 也是「人工确认」功能的技术前提：
         流程走到一半停下来等人点"同意"，人点了再从那一页继续
    3. 可视化：`draw_mermaid()` 直接把图导出成流程图，写文档不用手画
    4. 流式事件：`stream(stream_mode="updates")` 能按节点吐进度
       —— 前端可以显示"正在查磁盘…正在读日志…"，这是手写要自己造的

【代价是什么】—— 这部分更值得关注
    1. 多一层抽象：出错时的报错栈里全是框架内部帧，比手写难查
    2. 消息对象要转换：`_payload_messages` 那一段就是代价本身 ——
       LangChain 有自己的消息类，而我们直接调 API 要的是 dict，
       中间这层转换是**最容易出 bug 的地方**（字段丢一个就静默出错）
    3. 依赖变重：装 langgraph 顺带引入 langchain-core 等 7 个包
    4. 版本变化快：0.x→1.x 期间 API 改过好几轮

【一句话总结】
    手写版理解原理，框架版上生产。
    **顺序不能反** —— 反了的话你只会用框架，答不出它替你做了什么。
"""

import json
import operator
import time
from typing import Annotated, TypedDict

from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.observability import tracer
from app.agents.common import (
    DEFAULT_MAX_STEPS,
    SYSTEM_PROMPT,
    add_usage,
    new_usage,
    parse_arguments,
    run_tool_calls,
    summarize,
    tool_payload,
)
from app.llm import chat_step
from app.tools import execute_tool, tool_result_text  # noqa: F401  (供扩展用)

# 上限提示语，和手写版保持一致（放在这里是为了两个版本真的可比）
LIMIT_NOTICE = ("已达到工具调用上限。请立即停止调用工具，"
                "基于你目前已经获得的信息给出结论。"
                "如果信息不足以确定根因，就明确说明还缺什么。")

# 预算用尽的收口语。与 LIMIT_NOTICE 分开，因为对用户是两件事：
# "问得太多被截断"和"时间到了被截断"，后者还必须说清"还差什么没查"。
# ★ 明写"不要猜测没查过的数据"：被截断时模型最容易做的恰恰是把缺口补成幻觉。
BUDGET_NOTICE = ("时间预算已用尽。请立即停止调用工具，"
                 "基于你目前已经获得的信息给出结论，"
                 "并明确说明：哪些已经查到、还有哪些没来得及查。"
                 "不要猜测没查过的数据。")


# ============================================================
# 一、状态定义
# ============================================================
def _merge_usage(left: dict, right: dict) -> dict:
    """usage 的归并规则：数值累加，嵌套字典递归，其他覆盖。

    【为什么需要 reducer】
    LangGraph 里，节点返回的字典会"合并"进状态。合并方式由 reducer 决定：
        - 不写 Annotated → 默认覆盖（后写入的赢）
        - 写了 Annotated[dict, _merge_usage] → 用你的函数合并

    usage 必须累加：每次模型调用都消耗 token，节点只知道自己那一次的用量。
    如果写成默认覆盖，最终只会剩下最后一次的用量 —— 而 Agent 恰恰是
    "多轮累加"才贵，统计错了会让你完全估不准成本。

    ★ 这里踩过一个真实的坑，值得记下来：
      DeepSeek 的 usage 不是扁平的，它带嵌套字段：
          {"prompt_tokens": 29, "completion_tokens": 14, "total_tokens": 43,
           "prompt_tokens_details": {"cached_tokens": 0}}   ← 这是个 dict
      如果无脑写 `out[key] = old + val`，遇到这个字段就会抛
          TypeError: unsupported operand type(s) for +: 'int' and 'dict'
      所以必须判断类型。**「累加一个统计字典」这种看着最不可能出错的代码，
      恰恰因为第三方返回结构的意外而挂掉** —— 这也说明为什么每一步都要真跑。
    """
    out = dict(left or {})
    for key, val in (right or {}).items():
        old = out.get(key)
        if isinstance(val, dict):
            # 嵌套字典（如 prompt_tokens_details）递归合并
            out[key] = _merge_usage(old if isinstance(old, dict) else {}, val)
        elif isinstance(val, (int, float)) and isinstance(old, (int, float)):
            out[key] = old + val
        else:
            # 非数值（字符串、None 等）直接覆盖，不做加法
            out[key] = val
    return out


class AgentState(TypedDict):
    """图的状态。每个节点读它、返回要更新的部分。

    【和手写版的对应关系】
    手写版里这些是普通局部变量（messages、steps、usage、seen_calls）。
    框架版把它们变成"显式声明、由框架托管"的状态 —— 好处是每一步都能落盘、
    能回放、能从中间恢复。
    """
    # add_messages 是内置 reducer：追加消息，并按 id 去重
    messages: Annotated[list, add_messages]
    # 轨迹：每轮产出若干条，用 operator.add 拼起来
    steps: Annotated[list, operator.add]
    # 轮次计数：agent 节点每次 +1
    rounds: Annotated[int, operator.add]
    usage: Annotated[dict, _merge_usage]
    # 停止原因：不加 Annotated → 后写的覆盖前面的，正是我们要的
    stop_reason: str
    # 墙钟预算的截止时刻（time.time() 语义）。None = 不限。
    #
    # ★ 它必须在**状态**里，不能塞进 build_graph 的闭包：
    #   这个图是**编译后缓存、跨请求复用**的（见 build_graph 的缓存），
    #   把每个请求各自的 deadline 烘进闭包，等于让所有请求共用第一次那个值 ——
    #   一个"看起来生效、实际用错时间"的 bug，而且极难发现。
    #   凡"每请求不同"的东西，一律走 state。
    deadline: float


# ============================================================
# 二、★ 消息格式转换（框架的代价就体现在这里）
# ============================================================
def _text(content) -> str:
    """把消息内容统一成字符串。

    LangChain 的 content 可能是 str，也可能是 list（多模态：文本 + 图片）。
    我们只处理文本，所以非字符串就强转，避免后面 json.dumps 时炸掉。
    """
    return content if isinstance(content, str) else str(content or "")


def ai_message_from(raw: dict) -> AIMessage:
    """OpenAI 格式的 message → LangChain 的 AIMessage。

    【这里有个真实的坑，值得单独讲】
    LangChain 的 tool_calls 里，参数是**已经解析好的 dict**（args）。
    但模型返回给我们的是**字符串**（arguments）。也就是说：
        "模型输出的原始字符串" →「LangChain 标准化的 dict」→ "要发回给 API 的字符串"
    一来一回，如果中间某次解析失败（模型输出了非法 JSON），
    原始字符串就丢了 —— 模型看不到自己写错在哪，只会反复犯同样的错。

    所以这里用 `additional_kwargs` 把**原始字符串**一起存下来，
    发回给 API 时优先用它。多存一个字段，换来"零信息损失"。
    """
    raw_args, calls = {}, []
    for idx, call in enumerate(raw.get("tool_calls") or []):
        fn = call.get("function") or {}
        call_id = call.get("id") or f"call_{idx}"
        raw_args[call_id] = fn.get("arguments")
        parsed, _err = parse_arguments(fn.get("arguments"))
        calls.append({
            "name": fn.get("name") or "unknown",
            "args": parsed,
            "id": call_id,
            "type": "tool_call",
        })
    return AIMessage(
        content=raw.get("content") or "",
        tool_calls=calls,
        additional_kwargs={"raw_arguments": raw_args},
    )


def payload_messages(messages: list) -> list:
    """LangChain 消息对象 → 我们自己调 API 用的 dict。

    ★ 这是整份代码里最容易出错的一段，也是"用框架"必须付的代价：
      两种消息格式的字段名、嵌套结构都不一样，少写一个字段不会报错，
      只会在某次工具调用时莫名其妙地失败。

      对照表（值得记住，覆盖了"LangChain 消息和原生 API 怎么对应"这个问题）：
        SystemMessage  → {"role":"system",  "content": str}
        HumanMessage   → {"role":"user",    "content": str}
        AIMessage      → {"role":"assistant","content": str,
                          "tool_calls":[{"id","type","function":{"name","arguments"}}]}
        ToolMessage    → {"role":"tool",    "tool_call_id": str, "content": str}

      注意原生格式里 arguments 是**字符串**，LangChain 里 args 是**dict** ——
      这是最容易搞混的一处。
    """
    out = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": _text(m.content)})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": _text(m.content)})
        elif isinstance(m, AIMessage):
            item = {"role": "assistant", "content": _text(m.content)}
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                raw_args = (m.additional_kwargs or {}).get("raw_arguments") or {}
                item["tool_calls"] = [{
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        # 优先用原始字符串，没有才自己序列化 —— 保证零信息损失
                        "arguments": raw_args.get(c["id"])
                        or json.dumps(c.get("args") or {}, ensure_ascii=False),
                    },
                } for c in calls]
            out.append(item)
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                        "content": _text(m.content)})
        elif isinstance(m, dict):
            # 兜底：允许直接塞原生 dict（初始状态就用了这个能力）
            out.append(m)
    return out


# ============================================================
# 三、节点
# ============================================================
def make_agent_node(tool_names: list = None):
    """节点工厂：返回一个"只带指定工具"的 agent 节点。

    【为什么要工厂，而不是直接写个 agent_node】
    多 Agent 拆分之后，同一个 ReAct 图标会被复用多次，但**每次要用不同的工具子集**：
        工具执行 Agent   只给 5 个运维工具（不给 search_knowledge）
        单 Agent 模式    给全部 7 个

    LangGraph 的节点是普通可调用对象，所以"用闭包带上配置"是最直接的做法。
    另一种做法是把配置塞进 state —— 但那是"运行时数据"，
    而工具清单是"图的结构"，混在一起会让状态变得难懂。

    对应手写版循环体的第 1、2 步。差别在于：手写版是"读变量 → 判断 → break"，
    这里是"读 state → 返回更新 → 由边决定下一步走哪"。
    """
    tools = tool_payload(tool_names)

    def agent_node(state: AgentState) -> dict:
        out = chat_step(payload_messages(state["messages"]),
                        tools=tools, temperature=0)
        return {
            "messages": [ai_message_from(out["message"])],
            "usage": out["usage"] or {},
            "rounds": 1,
        }

    return agent_node


def tools_node(state: AgentState) -> dict:
    """节点二：执行工具。

    ★ 注意这里复用了 common.run_tool_calls —— 和手写版**同一份代码**。
      这不是偷懒，是对比实验的基本要求：
      如果两个版本的工具体现逻辑不一样，跑出来的差异你就分不清
      是"框架的差异"还是"你自己代码的差异"。
    """
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []
    raw_args = (last.additional_kwargs or {}).get("raw_arguments") or {}

    # 转回 OpenAI 格式，喂给共用的执行函数
    oai_calls = [{
        "id": c["id"],
        "type": "function",
        "function": {
            "name": c["name"],
            "arguments": raw_args.get(c["id"])
            or json.dumps(c.get("args") or {}, ensure_ascii=False),
        },
    } for c in calls]

    # 查重表从已累积的轨迹里重建 —— 不引入可变共享状态，
    # 这样图是可重放的（同样的输入状态，永远得到同样的结果）
    seen = {
        f"{s['tool']}:{json.dumps(s['args'], sort_keys=True, ensure_ascii=False)}": 1
        for s in (state.get("steps") or [])
    }

    tool_messages, steps = run_tool_calls(
        oai_calls, seen, state.get("rounds") or 0, _text(last.content))

    return {
        "messages": [ToolMessage(content=tm["content"],
                                 tool_call_id=tm["tool_call_id"])
                     for tm in tool_messages],
        "steps": steps,
    }


def close_dangling_tool_calls(messages: list) -> list:
    """给「没有执行」的 tool_calls 补上占位 tool 消息。

    ★ 这是一个真实踩出来的 bug，值得完整说清楚。

    【现象】
    撞上 max_steps 上限时，收口阶段直接报 400：

        An assistant message with 'tool_calls' must be followed by tool
        messages responding to each 'tool_call_id'
        (insufficient tool messages following tool_calls message)

    【根因】
    协议要求 assistant 的 `tool_calls` 和 role=tool 的应答消息**成对出现**。
    而路由是**在 agent 节点产出新 tool_calls 之后**才判断"步数用完了"：

        agent 产出 3 个 tool_calls
          → 路由：steps 已 >= max_steps → 直接去 finalize
          → 那 3 个 tool_calls 永远没有人应答
          → finalize 把它们连同历史一起发给模型 → 服务端拒绝

    也就是说：**循环被掐断在了"半路"—— 正好掐在发出请求、还没收到结果的中间。**

    【为什么手写版没这个问题】
    手写版的结构是"先执行完这一轮所有工具调用，再进入下一轮"，
    所以它停下来的时候，最后一条消息一定是 tool 消息，天然成对。

    同一个逻辑，两种结构 —— **一个天然避开，一个没有。**
    这不是"框架不好"，而是提醒你：**换实现方式时，协议约束要重新过一遍。**

    【修法为什么是"补占位"而不是"删掉"】
    删掉那条 assistant 消息也能绕过报错，但会**丢掉"模型想查什么"这条信息**。
    补一条说明「因为达到上限，本次调用未执行」的 tool 消息：
      - 协议上成对，合法
      - 语义上真实：这些调用确实没执行
      - 模型知道"我想查但没查成"，收口结论会更诚实（会说明还缺什么）

    **修 bug 时优先保住信息，而不是把报错消掉。**
    """
    answered = {m.tool_call_id for m in messages
                if isinstance(m, ToolMessage)}
    pending = []
    for m in messages:
        if isinstance(m, AIMessage):
            for call in (m.tool_calls or []):
                if call.get("id") not in answered:
                    pending.append(call)
    if not pending:
        return messages

    placeholders = [
        ToolMessage(
            content=json.dumps({
                "skipped": True,
                "tool": c.get("name"),
                "reason": "已达工具调用上限，本次调用未执行",
            }, ensure_ascii=False),
            tool_call_id=c["id"],
        )
        for c in pending
    ]
    return list(messages) + placeholders


def finalize_node(state: AgentState) -> dict:
    """节点三：超上限 / 预算用尽 时强制收口。

    同样是把 tools 参数去掉，让它只能输出文字。

    ★ 两种收口原因要**分开说**：撞步数上限是"问得太多"，
      预算用尽是"时间到了" —— 对用户是两件事，后者还应该告诉他"还差什么没查"。
      把两者混成一句话（"已达到上限"）会让人以为"再问一次就能查完"。
    """
    deadline = state.get("deadline")
    over_budget = bool(deadline and time.time() >= deadline)

    # ★ 先补齐未应答的 tool_calls，否则发出去会被服务端 400 拒绝
    msgs = payload_messages(close_dangling_tool_calls(state["messages"]))
    if over_budget:
        msgs.append({"role": "user", "content": BUDGET_NOTICE})
    else:
        msgs.append({"role": "user", "content": LIMIT_NOTICE})
    out = chat_step(msgs, tools=None, temperature=0)
    return {
        "messages": [AIMessage(content=out["message"].get("content") or "")],
        "usage": out["usage"] or {},
        "stop_reason": "budget" if over_budget else "max_steps",
    }


# ============================================================
# 四、路由（这就是手写版里的 if / break）
# ============================================================
def make_router(max_steps: int):
    """闭包生成路由函数 —— 因为路由逻辑需要知道 max_steps。

    对应手写版里的：
        if not tool_calls: break
        if step_no >= max_steps: 收口
    """

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        if not (getattr(last, "tool_calls", None) or []):
            return END                       # 模型自己决定回答了 → 结束
        # ★ 预算检查放在步数检查**之前**：两者都要收口，但原因不同，
        #   而 finalize 要靠"现在是不是已经过点"来区分该说哪句话。
        #   先判预算，就不会出现"明明超时了却报成撞步数上限"。
        deadline = state.get("deadline")
        if deadline and time.time() >= deadline:
            return "finalize"                # 时间到了 → 强制收口（不硬砍，见 finalize）
        if len(state.get("steps") or []) >= max_steps:
            return "finalize"                # 撞上限 → 强制收口
        return "tools"                       # 否则继续查

    return route


# ============================================================
# 五、编译成图
# ============================================================
_GRAPH_CACHE = {}


def build_graph(max_steps: int = DEFAULT_MAX_STEPS, tool_names: list = None):
    """编译并缓存图。

    【为什么缓存】
    建图有开销，而每次请求都重建一遍纯属浪费。
    生产上的做法是**服务启动时编译一次**，之后所有请求复用同一个对象 ——
    它是无状态的（状态都在 invoke 传进去），可以安全并发使用。

    这也是手写版没有的收益：手写版每次都"从零开始循环"，
    而编译好的图可以当成一个常驻的"运行时"。
    """
    # 缓存键必须带上工具子集 —— 否则"给 5 个工具"的图和"给 6 个工具"的图
    # 会互相覆盖，而且是**静默**的：第二次拿到的是第一次编译的对象，
    # 工具清单是错的却不报错。缓存键漏字段是这类 bug 的经典来源。
    key = (max_steps, tuple(sorted(tool_names)) if tool_names else None)
    if key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]

    graph = StateGraph(AgentState)

    graph.add_node("agent", make_agent_node(tool_names))   # 想
    graph.add_node("tools", tools_node)          # 做 + 看
    graph.add_node("finalize", finalize_node)    # 收口

    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", make_router(max_steps),
        {"tools": "tools", "finalize": "finalize", END: END},
    )
    graph.add_edge("tools", "agent")             # ★ 这条边就是"循环"
    graph.add_edge("finalize", END)

    compiled = graph.compile()
    _GRAPH_CACHE[key] = compiled
    return compiled


def mermaid(max_steps: int = DEFAULT_MAX_STEPS) -> str:
    """把图导出成 mermaid 文本（LangGraph 白送的能力）。

    这就是框架版多出来的东西之一：**结构可视化不用手画**。
    需要画架构图时，直接把这个贴出去。
    """
    return build_graph(max_steps).get_graph().draw_mermaid()


# ============================================================
# 六、入口
# ============================================================
@tracer.traced("langgraph")            # ★ 一次运行 = 一个 trace
def run(question: str, max_steps: int = DEFAULT_MAX_STEPS,
        verbose: bool = False, tool_names: list = None,
        system_prompt: str = None, deadline: float = None) -> dict:
    """跑一次。返回结构和手写版完全一致（这样对比脚本才不用写两套）。

    tool_names   限制可用工具（多 Agent 拆分时用）
    system_prompt 换一套系统提示（子 Agent 用另一种身份时用）
    deadline     墙钟预算的截止时刻（`time.time()` 语义）。None = 不限（默认）。

    ★ deadline 只进 state（**不进 build_graph 的缓存键**）：图是跨请求复用的，
      而预算是每请求各不相同的。把它烘进编译结果就是"所有请求共用第一次那个时间"。
    """
    started = time.time()
    graph = build_graph(max_steps, tool_names)

    init = {
        "messages": [SystemMessage(content=system_prompt or SYSTEM_PROMPT),
                     HumanMessage(content=question)],
        "steps": [],
        "rounds": 0,
        "usage": new_usage(),
        "stop_reason": "answered",
        "deadline": deadline,
    }

    # recursion_limit 兜底：万一图里出现意料外的环，框架层会直接中断，
    # 而不是无限跑下去把额度烧光。这是框架给的"第二道保险"。
    final = graph.invoke(init, config={"recursion_limit": max_steps * 4 + 10})

    steps = final.get("steps") or []
    answer = _text(final["messages"][-1].content)
    if verbose:
        print(f"\n──── 状态图执行完毕（共 {final.get('rounds')} 轮）────")
        for s in steps:
            mark = "↺" if s["repeat"] else ("✓" if s["ok"] else "✗")
            args = ", ".join(f"{k}={v}" for k, v in (s["args"] or {}).items())
            print(f"  {mark} [第{s['step']}轮] {s['tool']}({args})"
                  f" → {s['elapsed_ms']}ms, {s['observation_chars']} 字符")

    return summarize("langgraph", question, answer, steps,
                     final.get("stop_reason") or "answered",
                     final.get("usage") or new_usage(),
                     int((time.time() - started) * 1000))


def _main(argv=None):
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="LangGraph 版 ReAct")
    parser.add_argument("question", nargs="?", help="要诊断的问题")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--graph", action="store_true",
                        help="只打印状态图的 mermaid 定义")
    args = parser.parse_args(argv)

    if args.graph:
        print(mermaid(args.max_steps))
        return 0

    if not args.question:
        parser.error("需要提供 question（或用 --graph 看图）")

    result = run(args.question, max_steps=args.max_steps, verbose=True)
    print("\n" + "=" * 62)
    print("  最终回答")
    print("=" * 62)
    print(result["answer"])
    print("\n" + "=" * 62)
    print(f"  引擎 langgraph · 轮次 {result['rounds']}"
          f" · 工具调用 {result['tool_calls']} 次")
    print(f"  停止原因 {result['stop_reason']}"
          f" · 耗时 {result['elapsed_ms']}ms"
          f" · token {result['usage']['total_tokens']}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
