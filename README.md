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

### 4. 启动服务

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

启动后打开 **http://127.0.0.1:8000/docs** —— FastAPI 自动生成的交互式文档，
不用写前端就能点着测每一个接口。

### 5. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，供容器探活和监控使用 |
| GET | `/audit` | 读取审计日志（每次关键动作都留痕） |
| POST | `/chat` | 问答，一次性返回完整回答 |
| POST | `/chat/stream` | 问答，SSE 流式返回（字逐个蹦出） |
| POST | `/parse` | 意图解析：把一句人话变成结构化 JSON |
| POST | `/webhook/alert` | 告警驱动入口：无人值守自动诊断 |
| GET | `/rag/stats` | 知识库索引统计 |
| POST | `/rag/index` | 重建索引 |
| POST | `/rag/search` | 只检索不生成（排查检索质量用） |
| POST | `/rag/ask` | RAG 问答，带引用溯源 |

RAG 也可以用命令行：

```bash
.venv\Scripts\python.exe -m app.rag.pipeline build     # 构建索引
.venv\Scripts\python.exe -m app.rag.pipeline eval      # 跑召回率评测
.venv\Scripts\python.exe -m app.rag.pipeline ask "nginx 报 502 怎么排查"
```

调用示例（在 Git Bash 下，中文请用文件传参，命令行直传会被编码搞坏）：

```bash
# 先生成 payload 文件，避免中文在命令行里被破坏
python -c "import json,pathlib;pathlib.Path('_q.json').write_text(json.dumps({'question':'帮我看看 web-01 上 nginx 的日志'},ensure_ascii=False),encoding='utf-8')"

# 非流式
curl -s -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" --data-binary @_q.json

# 流式（-N 关闭缓冲，才能看到逐块输出）
curl -sN -X POST http://127.0.0.1:8000/chat/stream -H "Content-Type: application/json" --data-binary @_q.json
```

---

## RAG 检索效果（实测）

语料：`data/knowledge/` 下 6 篇运维排障文档，切成 50 个块。
评测集：`eval/qa_set.json`，32 个问题，分**词面型**（用文档里的原词提问）
和**语义型**（刻意换成完全不同的词表达同一意思）两类。

| 检索模式 | 总体 | 词面型 | 语义型 |
|---|---|---|---|
| 纯向量检索 | 93.8% | 96.2% | 83.3% |
| 纯关键词 BM25 | 96.9% | 100.0% | 83.3% |
| **混合检索（+RRF 融合）** | **100.0%** | **100.0%** | **100.0%** |

指标是 **Top-3 召回率**：前 3 条结果里是否有片段来自正确的那篇文档。

**混合检索的分数高于两个单项**，这说明两路漏掉的是**不同的**问题，
融合把它们各自的盲区补上了 —— 这才是 RRF 的价值，不是"取平均"。

> ⚠️ 当前向量后端是本地哈希兜底（没有配 embedding API Key），
> 它本质上还是词面匹配，所以「语义型」那一列的差距还没体现出来。
> 配好 `DASHSCOPE_API_KEY` 后重建索引，语义型问题的差距会明显拉开。

---

## 进度

- [x] **Day 0** 环境搭建、密钥管理、第一次模型调用
- [x] **Day 1** Python 基础（5 个练习）+ 容错解析结构化输出
- [x] **Day 2** FastAPI 服务 + SSE 流式 + 意图解析 + 告警 webhook + 审计留痕
- [x] **Day 3** RAG 全链路 + 混合检索 + 召回率评测 + 引用溯源问答
- [ ] **Day 4** 手写 ReAct + LangGraph 双版本
- [ ] **Day 5** 5 个运维工具 → MCP Server
- [ ] **Day 6** 拆成 Supervisor + 4 个专业 Agent
- [ ] **Day 7** Docker 沙箱执行 + Human-in-the-Loop
- [ ] **Day 8** 自托管 Langfuse + 全链路 Trace
- [ ] **Day 9** 40 条评测集 + 评测报告
- [ ] **Day 10** 部署到公网（Docker Compose + Nginx + HTTPS）
- [ ] **Day 11-15** 仓库整理 + 简历 + 面试演练 + 第一批投递

---

## 目录结构

```
agentdesk/
├── app/
│   ├── __init__.py      包说明与目录规划
│   ├── llm.py           模型调用统一入口（chat / chat_stream / chat_stream_async / chat_json）
│   ├── main.py          FastAPI 服务入口（10 个接口）
│   └── rag/
│       ├── loader.py    文档加载与语义段落切分
│       ├── embedder.py  可插拔向量后端（dashscope / local 兜底）
│       ├── store.py     向量存储 + BM25 + RRF 融合检索
│       └── pipeline.py  全链路编排 + RAG 问答 + 召回率评测 + CLI
├── data/
│   ├── knowledge/       知识库语料（6 篇运维排障文档，进仓库）
│   └── index/           构建出的索引（可重建，不进仓库）
├── docs/
│   ├── python-reference.md   Python 速查手册（含笔试四件套 + 报错速查表）
│   └── knowledge-points.md   知识点清单（面试复习用）
├── eval/
│   └── qa_set.json      召回率评测集（32 个问题）
├── practice/
│   └── day1/            Day 1 的 5 个练习 + 公共封装
├── check_env.py        Day 0 验收脚本
├── requirements.txt
├── .env                 本地密钥（不进仓库）
├── .env.example         模板
└── .gitignore
```

> 后续补充 `app/agents/`、`app/tools/`、`app/mcp_server/`、`app/rag/`、
> `app/sandbox/`、`app/observability/`，以及 `eval/` 与 `deploy/`。

