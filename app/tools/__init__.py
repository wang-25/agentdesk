# -*- coding: utf-8 -*-
"""Agent 的工具层 —— Agent 的「手」。

    ops.py   7 个运维工具：5 个查现场 + 1 个查经验（知识库检索）+ 1 个执行白名单命令

【为什么单独一层】
工具是 Agent 和真实世界之间唯一的通道。把它单独放在一层，
是为了让「安全边界」有一个明确的落点：
    - 参数校验（防命令注入）在这里
    - 只读白名单在这里
    - 风险分级在这里
模型无论怎么"想"，最终都得经过这层代码才碰得到系统。

【三后端】
    mock  仿真数据，任何机器都能跑（默认）
    local 在本机执行只读命令（Linux）
    ssh   通过 SSH 到真实远程主机执行只读命令
          —— 逻辑主机名与真实地址解耦，映射表放 OPS_SSH_TARGETS，
             Agent 只能看到逻辑名，无法自己编一个 IP 去连
"""

from app.tools.ops import (        # noqa: F401
    BACKEND,
    KNOWN_HOSTS,
    TOOLS,
    ToolError,
    execute_tool,
    host_list_text,
    tool_catalog,
    tool_result_text,
    tool_schemas,
)
