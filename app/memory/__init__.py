# -*- coding: utf-8 -*-
"""
记忆（Memory）：会话记忆 + 案例记忆
============================================================
两个模块，解决两件不同的事：

    `sessions.py`  **会话记忆** —— 同一个会话里的追问能接上。
                   人不会每次把前 10 轮重新贴一遍，所以"追问"这个动作
                   在无状态接口上实际上不可用。

    `cases.py`     **案例记忆** —— 解决过的故障变成下次的参考。
                   M2 的事件表里已经沉淀了"什么告警 → 怎么诊断 → 怎么处置"，
                   但这些数据只用于展示，**从未回流到诊断过程**。

两者共享同一套存储范式（追加 JSONL + 启动折叠 + 上限如实报告），
但**风险等级完全不同**，这一点值得说清楚：

    会话记忆错了 → 模型把上下文接错，答非所问。吵，但看得见。
    案例记忆错了 → 模型把"上次的答案"当成"这次的事实"，
                  处置动作看起来成功了，实际问题还在。**安静，且危险。**

所以 `cases.render_context()` 的输出带强制标注（历史 / 时间 / 请用当前
实测数据重新判断），并有专门的用例守着那段文案 ——
见 `tests/test_memory.py::test_render_context_demands_fresh_evidence`。

使用文档：`docs/memory.md`。
"""

from app.memory.cases import (
    CaseStore,
    counts as case_counts,
    find_similar,
    record_case,
    render_context,
)
from app.memory.sessions import SessionStore, require_session_id

__all__ = [
    "SessionStore",
    "require_session_id",
    "CaseStore",
    "record_case",
    "find_similar",
    "render_context",
    "case_counts",
]
