# -*- coding: utf-8 -*-
"""
沙箱执行 + 人工确认（Human-in-the-Loop）
============================================================
让 Agent 能对系统做**改动**，同时保证"最坏情况可控"。

【为什么这两件事必须一起做】

最初的工具全是只读的。当时说过一句话：

    "模型永远不执行任何东西，它只是提出请求，真正决定执不执行的是你的代码。"

后来补上另一半：**当"执行"真的发生之后，谁来兜底。**

只做沙箱不做审批 → 一个被绕过的隔离层，你连它被绕过都不知道
只做审批不做沙箱 → 人点同意之后，命令在一个没有任何限制的环境里跑
两个都做          → 隔离限定"最坏能坏到哪"，审批限定"谁批准的"

【目录结构与职责】

    policy.py     准入规则：白名单 + 参数校验 + 三维决策。**只管判断，不管执行**
    executor.py   执行：容器通道 / 主机通道 / 仿真通道
    approvals.py  审批单：生命周期、指纹绑定、单次消费、追加日志

【一次写操作的完整链路】

    模型想执行 `truncate -s 0 /var/log/nginx/error.log`
      ↓ policy.decide()
        命中 truncate 规则 → decision=needs_approval
        校验路径在 /var/log 下且以 .log 结尾 → 通过
        计算指纹 → 挂载 /var/log 为 rw（容器通道）
      ↓ approvals.create()
        生成 ap-xxxxxxxx，状态 pending，写入 logs/approvals.jsonl
      ↓ 工具把这张单子作为「观察结果」返回给模型
        模型据此回答："已提交审批，等待人工确认"
      ↓ 人：GET /approvals 看到它 → POST /approvals/{id}/approve
        必须带上审批人（by）—— 没有审批人的审批单等于没有审批
      ↓ POST /approvals/{id}/execute
        重新算指纹比对（防 TOCTOU）→ 状态必须是 approved（防重放）
        → executor.run() 放进一次性容器：无网络、根只读、掉全部 capability
      ↓ 结果写审计；审批单状态 → consumed（**不能再执行第二次**）

【一句话总结】

    沙箱是"边界"，审批是"授权"，审计是"证据"。三个缺一个，Agent 就不该被
    允许碰生产环境。

文档见 docs/sandbox-hitl.md。
"""
