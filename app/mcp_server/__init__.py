# -*- coding: utf-8 -*-
"""MCP Server —— 把 Agent 的工具暴露成标准协议

    server.py   用官方 mcp SDK 的 FastMCP 暴露 6 个工具 + 1 个资源

【为什么要有这一层】
在这之前，6 个工具只有本项目自己能调用。MCP 是一层标准协议，
做好之后**任何支持 MCP 的客户端**（Cursor、Claude Desktop、其他 Agent）
都能直接调用它们 —— 项目从"自用工具"变成"生态里的一个能力提供方"。

【一句话讲清 MCP 是什么】
MCP（Model Context Protocol）= **AI 应用和外部能力之间的 USB-C 接口**。

    没有 MCP 之前：每个客户端要为自己的每个工具写一遍适配代码
                  M 个客户端 × N 个工具 = M×N 份胶水代码

    有了 MCP 之后：工具方按协议暴露一次，客户端按协议接一次
                  M + N 份

这就是为什么它是标准，也是为什么值得单独做一层。
"""
