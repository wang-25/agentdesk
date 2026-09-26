# -*- coding: utf-8 -*-
"""Agent 的工具层 —— Agent 的「手」。

    ops.py   6 个运维工具：5 个查现场 + 1 个查经验（知识库检索）

【为什么单独一层】
工具是 Agent 和真实世界之间唯一的通道。把它单独放在一层，
是为了让「安全边界」有一个明确的落点：
    - 参数校验（防命令注入）在这里
    - 只读白名单在这里
    - 风险分级在这里
模型无论怎么"想"，最终都得经过这层代码才碰得到系统。

【双后端】
    mock  仿真数据，任何机器都能跑（默认）
    local 真执行只读命令，Linux 上可用（Day 10 换成 Docker 沙箱）
"""

from app.tools.ops import (        # noqa: F401
    TOOLS,
    ToolError,
    execute_tool,
    tool_catalog,
    tool_result_text,
    tool_schemas,
)
