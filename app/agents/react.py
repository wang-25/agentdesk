# -*- coding: utf-8 -*-
"""
手写 ReAct 循环 —— 不依赖任何 Agent 框架
============================================================
这个文件要做的事只有一件：**让模型能自己决定下一步做什么**。

【ReAct 是什么】
ReAct = Reasoning + Acting。名字听着玄，实现起来就是一个 while 循环：

    把问题给模型
    while 没结束:
        模型决定：是直接回答，还是调某个工具？
        要调工具 → 执行 → 把结果塞回对话历史 → 再问一遍
        直接回答 → 结束

就这么简单。**Agent 框架帮你做的就是把这个循环包起来**，
再加上状态管理、检查点、可视化。循环本身只有几十行。

【为什么先手写一遍】
因为"LangGraph 底层在做什么"是个绕不开的问题。如果只会用框架，
这个问题就答不上来 —— 你不知道它替你做了哪些决策。
手写一遍之后你才知道：它管的是状态怎么存、循环怎么走、
失败怎么重试、人在哪一步介入。这些概念手写版里都有，只是没有名字。

【和普通问答的区别】
    普通问答：question → answer                （一问一答，中间没有动作）
    ReAct：   question → 想 → 做 → 看 → 想 → 做 → 看 → … → answer
             中间那些"做"和"看"，就是 Agent 和聊天机器人的分水岭。

【三条工程护栏（缺一个都不敢上生产）】
    1. max_steps —— 必须有上限。模型可能陷入"反复查同一个东西"的死循环，
       没有上限就是无限花钱 + 请求永远不返回。
    2. 重复调用检测 —— 同一个工具同样的参数查第二次，结果必然一样。
       检测到就提示模型，而不是白花一次调用。
    3. 工具异常不当异常处理 —— 参数是模型生成的，写错是常态。
       要把错误当成"观察结果"返回给它，让它自己看到、自己改。

【代码结构】
    本文件只负责"编排"（循环怎么走、什么时候停）。
    Prompt、消息处理、工具执行都在 common.py 里 ——
    因为 LangGraph 版要用同一套，否则两个版本的对比就不公平了。
"""

import time

from app.observability import tracer
from app.agents.common import (
    DEFAULT_MAX_STEPS,
    SYSTEM_PROMPT,
    add_usage,
    assistant_message,
    new_usage,
    run_tool_calls,
    summarize,
    tool_payload,
)
from app.llm import ModelError, chat_step


@tracer.traced("handwritten")          # ★ 一次运行 = 一个 trace
def run(question: str, max_steps: int = DEFAULT_MAX_STEPS,
        verbose: bool = False) -> dict:
    """跑一次完整的 ReAct 循环。

    参数：
        question   用户的问题，比如「web-01 上的网站访问很慢，帮我查下」
        max_steps  最多允许几轮"思考 + 调工具"。这既是成本上限，也是防死循环的护栏。
        verbose    打印每一步的轨迹（调试时用）

    返回：见 common.summarize()
    """
    started = time.time()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question}]
    schemas = tool_payload()
    steps = []
    usage = new_usage()
    seen_calls = {}          # (工具名, 参数) → 调用次数，跨轮次共享，用于查重
    stop_reason = "answered"
    answer = ""

    for step_no in range(1, max_steps + 1):
        if verbose:
            print(f"\n──── 第 {step_no} 轮：模型决策 ────")

        # ---------- 1. 让模型决策 ----------
        # temperature=0：工具调用要的是"稳定决策"，不是文采。
        # 同样的故障描述每次都该调同一批工具，否则评测没法复现。
        out = chat_step(messages, tools=schemas, temperature=0)
        add_usage(usage, out["usage"])

        msg = out["message"]
        messages.append(assistant_message(msg))
        tool_calls = msg.get("tool_calls") or []
        thought = (msg.get("content") or "").strip()

        # ---------- 2. 没有工具调用 = 它准备回答了 ----------
        if not tool_calls:
            answer = thought
            if verbose:
                print("  模型选择直接回答（不再调工具）")
            break

        if verbose:
            print(f"  模型决定调用 {len(tool_calls)} 个工具")
            if thought:
                print(f"  它的说明：{thought[:100]}")

        # ---------- 3. 执行工具，把结果塞回对话历史 ----------
        tool_messages, new_steps = run_tool_calls(
            tool_calls, seen_calls, step_no, thought)
        messages.extend(tool_messages)
        steps.extend(new_steps)

        if verbose:
            for s in new_steps:
                mark = "↺" if s["repeat"] else ("✓" if s["ok"] else "✗")
                print(f"  {mark} {s['tool']}"
                      f"({_brief_args(s['args'])})"
                      f" → {s['elapsed_ms']}ms, {s['observation_chars']} 字符")
    else:
        # for 循环自然跑完 = 用光了步数还没给答案。
        # 注意 for...else 的语义：只有循环没被 break 才走到这里。
        stop_reason = "max_steps"

    # ---------- 4. 步数耗尽时强制收口 ----------
    # 关键点：**把 tools 参数去掉**，让它没有工具可调，只能输出文字。
    # 如果还带着 tools，模型很可能又调一次工具，然后再次撞上限 —— 死循环。
    if stop_reason == "max_steps":
        messages.append({
            "role": "user",
            "content": ("已达到工具调用上限。请立即停止调用工具，"
                        "基于你目前已经获得的信息给出结论。"
                        "如果信息不足以确定根因，就明确说明还缺什么。"),
        })
        try:
            out = chat_step(messages, tools=None, temperature=0)
            add_usage(usage, out["usage"])
            answer = (out["message"].get("content") or "").strip()
        except ModelError as e:
            answer = f"（收口阶段调用失败：{e}）"
            stop_reason = "error"

    return summarize("handwritten", question, answer, steps, stop_reason,
                     usage, int((time.time() - started) * 1000))


def _brief_args(args: dict) -> str:
    """把参数压成一行短的，方便终端打印。"""
    if not args:
        return ""
    return ", ".join(f"{k}={v}" for k, v in args.items())


# ============================================================
# 命令行入口
# ============================================================
def _main(argv=None):
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="手写 ReAct 循环 —— 让模型自己决定调哪个工具")
    parser.add_argument("question", help="要诊断的问题")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help=f"最大轮次，默认 {DEFAULT_MAX_STEPS}")
    parser.add_argument("--quiet", action="store_true", help="只打印最终答案")
    args = parser.parse_args(argv)

    result = run(args.question, max_steps=args.max_steps,
                 verbose=not args.quiet)

    print("\n" + "=" * 62)
    print("  最终回答")
    print("=" * 62)
    print(result["answer"])
    print("\n" + "=" * 62)
    print(f"  轮次 {result['rounds']}"
          f" · 工具调用 {result['tool_calls']} 次"
          f" · 用到的工具 {result['distinct_tools']}")
    print(f"  停止原因 {result['stop_reason']}"
          f" · 耗时 {result['elapsed_ms']}ms"
          f" · token {result['usage']['total_tokens']}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
