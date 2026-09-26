# -*- coding: utf-8 -*-
"""AgentDesk 应用包。

目录规划（随进度逐步补齐）：
    app/llm.py          模型调用的统一入口
    app/main.py         FastAPI 服务入口
    app/rag/            （Day 3 ✓）检索增强
    app/tools/          （Day 4 ✓）6 个运维工具 + 安全边界
    app/agents/         （Day 4 ✓）ReAct 编排：手写 + LangGraph 双版本
    app/mcp_server/     （Day 5）把工具暴露成 MCP Server
    app/sandbox/        （Day 7）Docker 沙箱执行
    app/observability/  （Day 8）Langfuse 接入

【一条设计主线】
每一层只依赖它下面那层，不反向依赖：
    main.py  →  agents/  →  tools/  →  （系统 / RAG）
    main.py  →  rag/
所以「换模型」只动 llm.py，「换工具实现」只动 tools/，
「换编排引擎」只动 agents/ —— 上层不用改。
"""
