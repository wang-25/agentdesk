# -*- coding: utf-8 -*-
"""
可观测层
============================================================
把「每一次 Agent 运行」变成可查询、可算账的数据。

【为什么观测要分三层】

    tracer.py          记录层   trace/span 落 JSONL。本地文件，永远可用
    costs.py           算账层   token → 成本，按 Agent / 工具聚合
    langfuse_export.py 导出层   配了 Key 就推 Langfuse，没配就静默跳过

**本地记录是主路径，Langfuse 是可选导出。**

这个顺序不能反。理由很实际：
    - 观测是"出问题时才被需要"的东西，把它依赖在外部服务上，
      等于让"最需要它的时刻"（外部服务也挂了的时候）变成最不可用的时刻
    - 演示时如果断网，Langfuse 面板打不开，但本地的 traces.jsonl 还在

【三个概念】
    trace   一次完整运行（一次 /agent/ask、一次告警处置）
    span    trace 里的一个环节（一次模型调用 / 一次工具调用 / 一个 Agent 节点）
    usage   token 消耗 —— 挂在 span 上，聚合出成本

文档见 docs/observability.md。
"""
