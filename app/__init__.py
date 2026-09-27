# -*- coding: utf-8 -*-
"""AgentDesk 应用包。

目录规划（随进度逐步补齐）：
    app/llm.py          模型调用的统一入口
    app/main.py         FastAPI 服务入口
    app/rag/            （Day 3 ✓）检索增强
    app/tools/          （Day 4 ✓）7 个运维工具 + 安全边界
    app/agents/         （Day 4 ✓）ReAct 编排：手写 + LangGraph 双版本
                        （Day 6 ✓）Supervisor + 4 个专业 Agent
                        （Day 7 ✓）处置 Agent（唯一有写权限的 Agent）
    app/mcp_server/     （Day 5 ✓）把工具暴露成 MCP Server
    app/sandbox/        （Day 7 ✓）Docker 沙箱 + 人工确认（HITL）
    app/observability/  （Day 8 ✓）全链路 Trace + 成本归因
    app/evaluation/     （Day 9 ✓）端到端评测：4 项规则判定 + 模型判分
                        ★ 判定器先自证是准的，再拿来跑分

【一条设计主线】
每一层只依赖它下面那层，不反向依赖：
    main.py  →  agents/  →  tools/  →  （系统 / RAG）
    main.py  →  rag/
    mcp_server/  →  tools/
    sandbox/     →  policy（准入）→ executor（执行）+ approvals（授权）

所以「换模型」只动 llm.py，「换工具实现」只动 tools/，
「换编排引擎」只动 agents/ —— 上层不用改。

【出口与实现的分离】
`tools/` 是唯一的实现，上面挂了两个出口：
    agents/      自家 Agent 循环调用（错误当"观察结果"返回给模型）
    mcp_server/  外部 MCP 客户端调用（错误抛异常，走协议 isError）
同一份工具、同一套安全边界，两种错误契约 —— 因为面向的调用方不同。

【安全的主线】
写操作（run_command）走的不是普通工具路径，而是：
    白名单策略 → 人工审批 → 沙箱执行 → 审计留痕
这条链路的每一环都在 sandbox/ 里，且「结论没过校验不许动手」「沙箱
不可用就 fail-closed」两条规则是硬约束，不在任何提示词里。
"""
