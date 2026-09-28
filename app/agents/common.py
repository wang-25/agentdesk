# -*- coding: utf-8 -*-
"""
ReAct 两个实现的公共部分
============================================================
手写版和 LangGraph 版要做的是同一件事，所以下面这些必须**完全一致**：

    SYSTEM_PROMPT          给模型的系统提示
    assistant_message()    把模型返回的 message 收拾成能发回去的形状
    parse_arguments()      容错解析模型生成的参数
    run_tool_calls()       执行一批工具调用，产出观察结果与轨迹

【为什么要抽出来】
如果不抽，两个文件各写一份。看起来只是"重复了 100 行"，实际后果是：
    - 改 Prompt 时改了一个忘了另一个 → 对比结果不可比
    - 修工具执行的 bug 只修一处 → 另一个版本带病运行
    - 当被问"为什么两个版本结果不一样"时 → 你分不清是框架的差异
      还是你自己代码的差异

抽出来之后，"两个引擎的唯一差别"就只剩编排方式本身 ——
这才是对比实验该有的样子。变量只留一个。
"""

import json

from app.llm import chat_step
from app.tools import execute_tool, tool_result_text, tool_schemas

# ============================================================
# 系统提示
# ============================================================
# 这段提示词的几个刻意写法：
#
# 1. 写清"什么时候**不**调工具" —— 只写"要调工具"的话，模型会为了显得勤快
#    而无脑调一堆，既慢又费钱。明确告诉它"信息够了就停"。
#
# 2. 写清"不许编造" —— 这是最危险的失败模式：模型假装自己查过了，
#    给你一个听起来非常合理的诊断。用户完全看不出来。
#
# 3. 写清"只诊断不处置" —— 边界。这个 Agent 只有只读权限。
#
# 4. 写清"不要输出过程性说明" —— 否则回答开头会出现
#    "我已经收集到足够信息" 这种废话，污染结果、也干扰评测打分。
SYSTEM_PROMPT = """你是一个 Linux 运维诊断助手。你可以调用工具去查看真实的主机和容器状态。

工作要求：
1. 先想清楚需要哪些信息，再决定调哪个工具。一次可以调多个。
2. 拿到工具结果后，判断信息是否足够下结论：
   - 不够 → 继续调工具（换一个角度查，不要重复查同样的东西）
   - 够了 → 停止调用工具，直接给出结论
3. **严禁编造**。你只能使用工具真实返回的数据。如果工具报错或没有数据，
   就如实说明"无法获取"，绝不允许凭经验假设一个结果。
4. 你只有只读权限。可以给出处置建议，但不要声称你已经执行了任何操作。
5. 不要输出"我已经收集到足够信息""让我开始分析"这类过程性说明，
   直接给结论。
6. 最终回答用中文，结构为：
   - **现象**：一句话概括
   - **依据**：列出关键数据（要带具体的数值，不要含糊）
   - **根因**：你的判断，并说明置信度（确定 / 可能 / 需要更多信息）
   - **建议**：具体的处置步骤，按优先级排

已知主机：web-01（Web 服务器）、db-01（数据库）、cache-01（缓存）"""

DEFAULT_MAX_STEPS = 6


# ============================================================
# 消息处理
# ============================================================
def assistant_message(msg: dict) -> dict:
    """把模型返回的 message 收拾成"能原样发回去"的形状。

    【为什么不能直接把原始 message 塞回 messages】
    模型返回的 message 里可能带 `reasoning_content`（思维链）之类的字段。
    第一次返回没问题，但原样发回去时，有些服务端会认为
    "多出来的字段不合法"而报错。

    这就是"能跑"和"稳定"之间的距离 —— 大量 AI 应用的线上故障，
    都出在这种不起眼的字段处理上。
    """
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    return out


def parse_arguments(raw) -> tuple:
    """解析工具参数。返回 (args_dict, error_or_None)。

    ★ 必须容错：arguments 是模型生成的**字符串**，不是对象。
      它会输出 '{"host": "web-01"}'，偶尔也会输出
      '{"host": "web-01",}' 这种带尾逗号的非法 JSON。

    故意把错误返回出去而不是抛异常：调用方需要知道
    "模型给了一个坏参数"，然后把这个错误当成观察结果告诉它。
    """
    if isinstance(raw, dict):
        return raw, None
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, f"参数不是合法 JSON（{e}）。你输出的是：{str(raw)[:200]}"
    if not isinstance(parsed, dict):
        return {}, f"参数必须是 JSON 对象，收到 {type(parsed).__name__}"
    return parsed, None


# ============================================================
# 工具执行
# ============================================================
def run_tool_calls(tool_calls: list, seen_calls: dict, round_no: int,
                   thought: str = "") -> tuple:
    """执行一批工具调用。

    参数：
        tool_calls  模型给出的 tool_calls 列表（OpenAI 格式）
        seen_calls  查重表 {(工具名, 参数json): 次数}，跨轮次复用同一个 dict
        round_no    当前是第几轮，写进轨迹
        thought     模型这一轮的说明文字，写进轨迹

    返回 (tool_messages, steps)：
        tool_messages  准备追加进对话历史的 role=tool 消息
        steps          这一轮的轨迹

    【为什么一轮要支持多个工具】
    模型经常同时需要"查磁盘"和"看日志"。这两件事互不依赖，
    一次要齐比来回问两轮省一次模型调用 —— 也就是省一次钱。
    而且第二轮还多了往返延迟。

    【为什么要查重】
    同一个工具同样的参数，结果必然一样。模型陷入循环时
    最典型的表现就是反复查同一个东西。检测到就提示它，
    而不是白花一次调用 —— 这既是省钱，也是防死循环的第一道护栏。
    """
    tool_messages, steps = [], []

    for call in tool_calls:
        fn = call.get("function") or {}
        name = fn.get("name") or "unknown"
        args, arg_err = parse_arguments(fn.get("arguments"))

        signature = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        repeat = signature in seen_calls
        seen_calls[signature] = seen_calls.get(signature, 0) + 1

        if arg_err:
            # 连参数都解析不了，执行都不用执行
            observation = json.dumps({"error": arg_err}, ensure_ascii=False)
            result = {"ok": False, "elapsed_ms": 0,
                      "risk": "unknown", "error": arg_err}
        else:
            result = execute_tool(name, args)
            observation = tool_result_text(name, args, result)

        if repeat:
            # 不改数据，只加一句提示 —— 让模型自己意识到"这条路走过了"
            observation += (
                f"\n[系统提示] 你已经用相同参数调用过 {name}，结果是一样的。"
                f"请换一个工具或换一组参数，或者基于已有信息直接给出结论。"
            )
            result["repeat"] = True

        tool_messages.append({
            "role": "tool",
            "tool_call_id": call.get("id"),
            "content": observation,
        })
        steps.append({
            "step": round_no,
            "thought": (thought or "")[:300],
            "tool": name,
            "args": args,
            "ok": bool(result.get("ok")),
            "risk": result.get("risk"),
            "repeat": repeat,
            "elapsed_ms": result.get("elapsed_ms", 0),
            "observation_chars": len(observation),
            # 只留预览：完整结果可能有几千字，轨迹给人看时不需要全存
            "observation_preview": observation[:400],
            # ★ 但完整文本要保留 —— 为什么？
            #   多 Agent 拆分后，「结果校验 Agent」要做**数值溯源**：
            #   检查结论里出现的每个数字能不能在工具返回里找到出处。
            #   拿被截断的预览去比对，会把"有出处"的数字误判成"没出处" ——
            #   校验器自己产出假警报，比不校验还糟。
            #
            #   所以：数据在源头不要销毁，由**出口**决定要不要裁剪。
            #   API 返回轨迹时会把 observation_text 去掉（见 main.py），
            #   内部数据保持完整。这个原则叫「不要把信息损失埋在数据转换里」。
            "observation_text": observation,
            "error": result.get("error"),
        })

    return tool_messages, steps


def new_usage() -> dict:
    """新建一个空的用量累计器。

    prompt_tokens 每次都算、completion_tokens 每次都算 ——
    所以多轮循环的 token 消耗是**累加**的，这也是 Agent 比单轮问答贵的原因。
    "Agent 的成本为什么高"？答案就在这里：轮次越多，输入重复喂的次数越多。
    """
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def add_usage(total: dict, usage: dict) -> dict:
    """把一次调用的用量累加进去（原地修改并返回）。"""
    for key in total:
        total[key] += (usage or {}).get(key) or 0
    return total


def tool_payload(only: list = None) -> list:
    """模型看到的工具清单（转成 OpenAI 兼容格式）。

    【only 参数为什么存在】
    多 Agent 拆分之后，不同的专业 Agent 应该只拿到**自己该用的那部分工具**。
    比如"工具执行 Agent"不该看见 search_knowledge —— 查知识库是"知识检索 Agent"
    的职责。

    这不只是"分工好看"，而是有实际效果：
        1. 上下文隔离：每个 Agent 的上下文里只装自己那类数据
        2. 选择变少 → 模型选错的概率变低（工具越多，选错率越高）
        3. 权限最小化：Agent 只能碰它该碰的东西

    传 None 表示不限制（单 Agent 模式用）。
    """
    schemas = tool_schemas()
    if only is None:
        return schemas
    allowed = set(only)
    picked = [s for s in schemas if s["function"]["name"] in allowed]
    # 静默过滤是危险的：名字写错一个，模型就永远拿不到那个工具，
    # 而且没有任何提示。所以这里主动报错。
    missing = allowed - {s["function"]["name"] for s in picked}
    if missing:
        raise ValueError(f"tool_payload 收到了不存在的工具名：{sorted(missing)}")
    return picked


def summarize(engine: str, question: str, answer: str, steps: list,
              stop_reason: str, usage: dict, elapsed_ms: int) -> dict:
    """统一两个引擎的返回结构 —— 不然对比脚本得写两套解析。"""
    return {
        "engine": engine,
        "question": question,
        "answer": answer,
        "steps": steps,
        "stop_reason": stop_reason,
        "rounds": max((s["step"] for s in steps), default=0),
        "tool_calls": len(steps),
        "distinct_tools": sorted({s["tool"] for s in steps}),
        "usage": usage,
        "elapsed_ms": elapsed_ms,
    }
