# -*- coding: utf-8 -*-
"""Agent 编排层 —— Agent 的「大脑」

同一个 ReAct 循环，两个实现，用来讲清"框架到底替你做了什么"：

    react.py     手写循环（while + 工具调用）。原理在这里，约 250 行
    graph.py     LangGraph StateGraph 版本。同样的行为，用状态图表达

    compare.py   跑同一个问题在两个引擎上，并排对比轨迹

【为什么要写两遍】
不是炫技，而是为了把两者的差异讲清楚：
「你用了 LangGraph，那它底层在做什么？」
手写过一遍，你才知道框架替你接管的是：状态怎么存、循环怎么走、
每一步怎么落盘（检查点）、什么条件下中断等人确认。这些概念手写版里都有，
只是没有名字。反过来，如果只学过框架，你会答不出来。
"""

from app.agents.react import run as run_react     # noqa: F401
