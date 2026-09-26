# AgentDesk

面向运维场景的多 Agent 智能体系统 —— 用一句自然语言完成「查日志 / 看磁盘 / 重启服务 / 定位故障」，
而不是记几十条命令。

> **状态：Day 0（环境搭建）**
> 这个 README 会随项目推进持续更新。面试官一定会点开看，所以从第一天就按最终标准写。

---

## 要解决什么问题

现在排查一个 nginx 502，实际动作是：

```bash
systemctl status nginx              # 服务活着吗
tail -100 /var/log/nginx/error.log  # 日志说什么
df -h                               # 是不是磁盘满了
docker ps                           # 上游容器在不在
```

敲五到八条命令、跨三个工具，然后在脑子里把结果拼成判断。

AgentDesk 想做到的是：输入「nginx 报 502 了，帮我看下」，由 Agent 自己决定查什么、
看结果、再决定下一步，最后给出结论和建议。

**差别不是少敲几条命令，而是从「我操作工具」变成「我表达意图」。**

---

## 为什么不直接用现成的通用 AI 助手

这是必须回答的问题，因为它就是面试必问题：「现在通用 AI 助手都能直连服务器，为什么还要自己做一个？」

诚实的回答分两层。

### 第一层：先承认通用助手更划算的那部分

**如果只是「我随口问一句、它帮我看服务器」，那通用助手确实更划算。**
单机、单人、有人在场的场景，不值得自建。这一点要先说清楚——硬说自己的东西更强，只会显得不懂行。

### 第二层：通用助手是「工具」，本项目是「服务」，不在同一层

| 维度 | 通用 AI 助手 | AgentDesk |
|---|---|---|
| 触发方式 | 人打开界面、打字 | 告警 webhook、其他系统调 API，**无人值守** |
| 交付形态 | 一个会话窗口 | 一个 HTTP 服务，可被运维平台 / 值班机器人调用 |
| 权限边界 | 给了凭据就全部放行 | 命令白名单 + 只读挂载 + 一次性容器 + 高危操作人工确认 |
| 审计 | 聊天记录，非结构化 | 每次操作落库：谁触发、跑了什么命令、结果如何 |
| 知识来源 | 模型自带 + 你贴给它的内容 | 检索历史工单、运维手册、内部拓扑 |
| 可靠性 | 无法证明 | 任务完成率 / 工具调用准确率 / P95 延迟 / Token 成本，**可量化** |
| 成本 | 按对话计费 | 小模型路由 + 大模型生成 + 语义缓存，可控 |

**最本质的差别是最后两行。**

通用助手跑一次，你不知道它为什么这么判断，也没法证明它靠谱。
而生产环境要的不是「它好像挺聪明」，是「我能量出它有 XX% 的任务完成率，并且知道剩下那部分错在哪」。

### 第三层：这两者不是竞争关系

本项目的目标之一，是把 5 个运维操作封装成 **MCP Server**。
做出来之后，通用 AI 助手反而可以调用 AgentDesk 提供的能力。

**做的不是通用助手的替代品，而是补上它缺的那一块。**


---

## 技术栈（规划）

| 层 | 选型 |
|---|---|
| 编排 | LangGraph（Supervisor 多 Agent） |
| 服务 | FastAPI + SSE 流式输出 |
| 检索 | Milvus / Qdrant + BGE-M3 embedding + 混合检索 + 重排 |
| 工具 | FastMCP（MCP Server） |
| 安全 | Docker 沙箱执行 + 命令白名单 + Human-in-the-Loop |
| 可观测 | 自托管 Langfuse + OpenTelemetry + Prometheus / Grafana |
| 评测 | Ragas + LLM-as-Judge |
| 部署 | Docker Compose + Nginx + HTTPS（阿里云 ECS） |

---

## 环境要求

- **Python 3.12**（不要用 3.13，参考项目依赖按 3.10-3.12 验证过）
- Git
- Docker Desktop（Day 10 才需要，现在不用装）

---

## 怎么跑起来

### 1. 配置密钥

把 `.env.example` 复制成 `.env`（仓库里已经帮你建好了），填入你自己的 API Key：

```env
DEEPSEEK_API_KEY=sk-你的key
```

> `.env` 已被 `.gitignore` 忽略，不会被提交到 GitHub。

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 运行 Day 0 验收

```bash
.venv\Scripts\python.exe check_env.py
```

**通过标准**：屏幕出现模型的一句回答，并打印出本次调用的 token 用量与成本估算。

---

## 进度

- [x] **Day 0** 环境搭建、密钥管理、第一次模型调用
- [ ] **Day 1-3** Python 子集 + Prompt 工程 + 结构化输出（产出：CLI 问答脚本）
- [ ] **Day 4** FastAPI + SSE 流式接口
- [ ] **Day 5-6** RAG 全链路（解析 → 切分 → 向量化 → 检索 → 重排）
- [ ] **Day 7** 手写 ReAct + LangGraph 双版本
- [ ] **Day 8** 5 个运维工具 → MCP Server
- [ ] **Day 9** 拆成 Supervisor + 4 个专业 Agent
- [ ] **Day 10** Docker 沙箱 + Human-in-the-Loop
- [ ] **Day 11** 自托管 Langfuse + 全链路 Trace
- [ ] **Day 12** 40 条评测集 + 评测报告
- [ ] **Day 13** 部署到公网
- [ ] **Day 14-15** 仓库整理 + 简历 + 面试演练

---

## 目录结构

```
agentdesk/
├── check_env.py      # Day 0 验收脚本：验证环境、Key、网络
├── requirements.txt
├── .env               # 本地密钥（不进仓库）
├── .env.example       # 模板
└── .gitignore
```

> Day 4 开始补充 `app/`（编排、Agent、工具、RAG、沙箱、可观测）与 `eval/`、`deploy/`。
