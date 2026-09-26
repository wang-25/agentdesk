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
