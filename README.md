# AgentDesk

面向运维场景的多 Agent 智能体系统 —— 用一句自然语言完成「查日志 / 看磁盘 / 重启服务 / 定位故障」，  
而不是记几十条命令。

---

> **想快速了解「现在到哪了、怎么用、要不要部署公网」→ 看 [`docs/overview.md`](docs/overview.md)**

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

| 维度   | 通用 AI 助手       | AgentDesk                                   |
| ---- | -------------- | ------------------------------------------- |
| 触发方式 | 人打开界面、打字       | 告警 webhook、其他系统调 API，**无人值守**               |
| 交付形态 | 一个会话窗口         | 一个 HTTP 服务，可被运维平台 / 值班机器人调用                 |
| 权限边界 | 给了凭据就全部放行      | 命令白名单 + 只读挂载 + 一次性容器 + 高危操作人工确认             |
| 审计   | 聊天记录，非结构化      | 每次操作落库：谁触发、跑了什么命令、结果如何                      |
| 知识来源 | 模型自带 + 你贴给它的内容 | 检索历史工单、运维手册、内部拓扑                            |
| 可靠性  | 无法证明           | 任务完成率 / 工具调用准确率 / P95 延迟 / Token 成本，**可量化** |
| 成本   | 按对话计费          | 小模型路由 + 大模型生成 + 语义缓存，可控                     |

**最本质的差别是最后两行。**

通用助手跑一次，你不知道它为什么这么判断，也没法证明它靠谱。  
而生产环境要的不是「它好像挺聪明」，是「我能量出它有 XX% 的任务完成率，并且知道剩下那部分错在哪」。

### 第三层：这两者不是竞争关系

**这一层已经做完了**（Day 5）：6 个运维操作封装成了 **MCP Server**。  
挂上 Cursor / Claude Desktop 之后，通用 AI 助手可以直接调用 AgentDesk 提供的能力。

```bash
# 挂上之后，在 Cursor 里问「web-01 磁盘满了吗」，它会直接调 check_disk 拿真实数据
.venv\Scripts\python.exe -m app.mcp_server.server        # stdio 模式
.venv\Scripts\python.exe scripts\mcp_check.py            # 协议层自检，9 项
```

**做的不是通用助手的替代品，而是补上它缺的那一块。**

---

## 技术栈

**现在实际在用的（Day 0-8）** —— 直接依赖 9 个：

| 层        | 选型                                    | 为什么选它                                       |
| -------- | ------------------------------------- | ------------------------------------------- |
| 语言       | Python 3.12                           | 3.10-3.12 是 AI 生态验证最充分的区间                   |
| 模型调用     | httpx 手写 + DeepSeek（OpenAI 兼容）        | 手写看得清协议细节；两家兼容格式，换服务商只改配置                   |
| 服务       | FastAPI + uvicorn                     | 原生 async（SSE 流式必需）+ pydantic 自动校验与文档        |
| 校验       | pydantic v2                           | 定义接口契约，FastAPI 依赖它生成 `/docs`                |
| 检索       | numpy 内存索引 + BM25(jieba) + RRF 融合     | 50 块规模用不上向量库，一次矩阵乘法几毫秒                      |
| 向量化      | 可插拔：百炼 `text-embedding-v3` / local 兜底 | 没有额外 Key 也能跑通链路自测                           |
| Agent 编排 | LangGraph 1.2                         | 状态图 + 检查点 + 可视化（与手写版并存对比）                   |
| 工具协议     | MCP SDK 2.2                           | 7 个工具暴露给任何 MCP 客户端（Cursor / Claude Desktop） |
| 沙箱执行     | Docker 一次性容器 + 命令白名单                  | 写操作隔离执行（无 Docker 时 fail-closed 仿真，不降级）      |
| 人工确认     | 审批单（HITL）+ 指纹绑定                       | 写操作必须人批准，防重放、防 TOCTOU                       |
| 可观测      | 自研 trace（JSONL）+ 可选 Langfuse 导出       | 本地记录是主路径，面板是可选                              |
| 配置       | python-dotenv                         | 密钥不落代码、不进仓库                                 |

**规划中的（Day 10 起）**：

| 层  | 选型                                      |
| -- | --------------------------------------- |
| 评测 | Ragas + LLM-as-Judge（自写召回率评测已有）         |
| 评测 | Ragas + LLM-as-Judge（自写召回率评测已有）         |
| 部署 | Docker Compose + Nginx + HTTPS（阿里云 ECS） |

> **每一项的替代方案、取舍、升级时机，以及"怎么验证它在工作"，见 [`docs/tech-stack.md`](docs/tech-stack.md)。**

---

## 环境要求

- **Python 3.12**（不要用 3.13，参考项目依赖按 3.10-3.12 验证过）
- Git
- Docker（本地跑沙箱、或按 Day 10 部署到服务器时需要）

> **部署到公网**的完整过程、安全设计和运维手册见 [`docs/deployment.md`](docs/deployment.md)。
> 那台机器上还跑着 WordPress + Zabbix（可用内存仅 359MB、swap=0），
> 所以里面有一节专门讲「怎么证明新服务不会把邻居挤死」。

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

### 4. 全链路自检

```bash
.venv\Scripts\python.exe scripts\smoke_test.py
```

一条命令跑完八层：**运行环境 → 模型连通 → 检索与问答 → Agent → 沙箱/审批 → 观测 → MCP → HTTP 接口**，  
最后给出 ✅/❌ 汇总表。服务没启动时最后一层自动跳过（退出码仍为 0）。  
加 `--full` 会额外跑一次真实 Agent 调用和 MCP 协议层自检。

### 5. 启动服务

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

启动后打开 **<http://127.0.0.1:8000/docs>** —— FastAPI 自动生成的交互式文档，  
不用写前端就能点着测每一个接口。

### 6. 接口一览

| 方法   | 路径               | 说明                                                                   |
| ---- | ---------------- | -------------------------------------------------------------------- |
| GET  | `/health`        | 健康检查，供容器探活和监控使用                                                      |
| GET  | `/audit`         | 读取审计日志（每次关键动作都留痕）                                                    |
| POST | `/chat`          | 问答，一次性返回完整回答                                                         |
| POST | `/chat/stream`   | 问答，SSE 流式返回（字逐个蹦出）                                                   |
| POST | `/parse`         | 意图解析：把一句人话变成结构化 JSON                                                 |
| POST | `/webhook/alert` | 告警驱动入口：无人值守自动诊断                                                      |
| GET  | `/rag/stats`     | 知识库索引统计                                                              |
| POST | `/rag/index`     | 重建索引                                                                 |
| POST | `/rag/search`    | 只检索不生成（排查检索质量用）                                                      |
| POST | `/rag/ask`       | RAG 问答，带引用溯源                                                         |
| GET  | `/agent/tools`   | Agent 能调用的工具清单（含风险等级）                                                |
| GET  | `/agent/graph`   | 导出状态图的 mermaid（`engine=react\|supervisor`）                           |
| POST | `/agent/ask`     | **Agent 自主诊断**（`engine` 三档：handwritten / langgraph / **supervisor**） |

> **公网部署时接口要带 token**：设了 `AUTH_ENABLED=1` 之后，
> 除 `/`、`/health`、`/docs` 之外的所有接口都要求请求头带凭据。
> 本地开发默认关闭，行为与之前完全一致。
>
> ```bash
> curl -H "X-API-Key: <你的 AGENT_TOKEN>" \
>      -H "Content-Type: application/json" \
>      -d '{"question":"web-01 磁盘快满了怎么处理"}' \
>      https://agent.simosheng.fun/rag/ask
> ```
>
> 安全设计（白名单为什么比黑名单可靠、单 IP 限流为什么能被绕过、
> 取 `X-Forwarded-For` 该取第一个还是最后一个）见
> [`docs/deployment.md`](docs/deployment.md)。

RAG 也可以用命令行：

```bash
.venv\Scripts\python.exe -m app.rag.pipeline build     # 构建索引
.venv\Scripts\python.exe -m app.rag.pipeline eval      # 跑召回率评测
.venv\Scripts\python.exe -m app.rag.pipeline ask "nginx 报 502 怎么排查"
```

Agent 也可以用命令行（三个引擎任选）：

```bash
# 手写 ReAct 循环
.venv\Scripts\python.exe -m app.agents.react "web-01 上的网站很慢，帮我查下"

# LangGraph 状态图版本
.venv\Scripts\python.exe -m app.agents.graph "cache-01 的 Redis 容器一直重启"

# 看状态图长什么样（mermaid）
.venv\Scripts\python.exe -m app.agents.graph --graph

# 多 Agent 编排（Supervisor + 4 个专业 Agent）
.venv\Scripts\python.exe -m app.agents.supervisor "web-01 上的网站很慢，帮我查下"
.venv\Scripts\python.exe -m app.agents.supervisor --graph      # 看编排图

# 两个引擎横向对比（会真实调用模型，约 ¥0.1）
.venv\Scripts\python.exe -m app.agents.compare

# 意图路由 Agent 的准确率评测（20 条，约 ¥0.02）
.venv\Scripts\python.exe scripts\eval_specialists.py
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

| 检索模式              | 总体         | 词面型        | 语义型        |
| ----------------- | ---------- | ---------- | ---------- |
| 纯向量检索             | 93.8%      | 96.2%      | 83.3%      |
| 纯关键词 BM25         | 96.9%      | 100.0%     | 83.3%      |
| **混合检索（+RRF 融合）** | **100.0%** | **100.0%** | **100.0%** |

指标是 **Top-3 召回率**：前 3 条结果里是否有片段来自正确的那篇文档。

**混合检索的分数高于两个单项**，这说明两路漏掉的是**不同的**问题，  
融合把它们各自的盲区补上了 —— 这才是 RRF 的价值，不是"取平均"。

> ⚠️ 当前向量后端是本地哈希兜底（没有配 embedding API Key），  
> 它本质上还是词面匹配，所以「语义型」那一列的差距还没体现出来。  
> 配好 `DASHSCOPE_API_KEY` 后重建索引，语义型问题的差距会明显拉开。

---

## Agent 能力（实测）

**6 个工具，都只读**：`check_disk` `check_load` `check_service` `list_containers`  
`tail_log` `search_knowledge`（前 5 个查现场，最后 1 个查知识库）。

工具层做了两件事，都是安全边界：

- **参数白名单校验**：`"web-01; rm -rf /"` 这类输入直接被拒，绝不拼 shell
- **双后端**：默认 `mock`（仿真数据，任何机器都能跑）  
  ／ 可切 `local`（真执行只读命令，Linux 上可用）

**两个引擎实现同一个 ReAct 循环**，用来回答「框架到底替你做了什么」：

|     | 手写版 `app/agents/react.py` | LangGraph 版 `app/agents/graph.py` |
| --- | ------------------------- | --------------------------------- |
| 循环  | `for` + `break`           | `add_conditional_edges`           |
| 状态  | 局部变量                      | `AgentState` + reducer，可落盘        |
| 可视化 | 自己画                       | `draw_mermaid()` 白送               |
| 依赖  | 零                         | +7 个包                             |

两个版本共用 `common.py` 里的同一份 Prompt 与工具执行逻辑 —— **唯一的变量是编排方式**。

---

## 多 Agent 编排（实测）

在单 Agent ReAct 循环外面套一层调度。**4 个 Agent，7 个节点**：

| Agent    | 节点                    | 工具                 | 职责                                               |
| -------- | --------------------- | ------------------ | ------------------------------------------------ |
| **意图路由** | `intent`              | 无                  | 把人话压成结构化标签（task_type / hosts / services / 是否查资料） |
| **知识检索** | `knowledge`           | `search_knowledge` | 查历史故障经验                                          |
| **工具执行** | `diagnose` / `reason` | 5 个运维工具 / **空**    | 查现场 / 纯推理                                        |
| **结果校验** | `verify`              | 无                  | 检查结论站不站得住                                        |

**拆分的四条收益（都能验证）**：上下文隔离、**可单独评测**、  
**权限最小化**（工具执行 Agent 的 schema 里根本没有 `search_knowledge`）、  
可换不同档次的模型。

**意图路由 Agent 准确率实测**（20 条标注用例，逐字段判定）：

| 字段                | 准确率        | 对了/总数 |
| ----------------- | ---------- | ----- |
| `task_type`       | **100.0%** | 20/20 |
| `hosts`           | **100.0%** | 19/19 |
| `services`        | 80.0%      | 4/5   |
| `needs_live_data` | **100.0%** | 20/20 |
| `needs_knowledge` | 83.3%      | 5/6   |

**全字段全对率 90.0%（18/20）**，需要二次修正的用例 0/20。

> ⚠️ 第一轮跑出来 5 个失败，逐条分析后发现**有一半是我自己标错了标签**  
> （比如"nginx 和 apache 有什么区别"漏标了 apache），  
> 还有两条是"多种答案都合理"。修正标签 + 加 `accept` 多解声明之后才是上面的数字。  
> **不能靠改标准答案让指标好看 —— 那叫改测试，不叫改代码。**

**校验 Agent 的设计**（这一块最值得看）：**能规则化的用规则，规则查不了的才用模型**。

```
规则查（确定、可复现、零成本、无幻觉）：数值溯源 / 越权声明 / 无证据下结论
模型查（只能它查）：因果链是否成立 / 置信度是否合理

而且规则先跑 —— 能判否就直接判否，一次模型调用都不花。
```

> 数值溯源规则踩了三轮误报（来源池漏了用户问题 / 把建议里的 `chmod 755` 当声称的数据 /  
> 主机名 `web-01` 里的数字），**收窄作用范围之后正例反例全部验证过** ——  
> 这 6 个正反例现在跑在自检脚本里。  
> **一个"永远返回通过"的校验器比没有更糟。**

完整原理、五个真实踩到的坑、面试问答见 [`docs/multi-agent.md`](docs/multi-agent.md)。

---

---

## MCP 能力（实测）

同一批工具挂了两个出口：自家 Agent 循环调用（`app/agents/`），  
以及 **MCP 标准协议**（`app/mcp_server/`）—— 后者让 Cursor / Claude Desktop  
这类客户端能直接调用。

**6 个 tools + 2 个 resources**，工具全带协议级风险提示：

| 协议字段              | 取值      | 含义        |
| ----------------- | ------- | --------- |
| `readOnlyHint`    | `True`  | 只读，不改任何东西 |
| `destructiveHint` | `False` | 不会造成破坏性变更 |
| `idempotentHint`  | `True`  | 重复调用结果一样  |
| `openWorldHint`   | `True`  | 会跟外部系统交互  |

两个资源（只读数据，不是动作）：

| URI                         | 内容                       |
| --------------------------- | ------------------------ |
| `agentdesk://tools/catalog` | 6 个工具的元数据（名称、风险等级、参数、说明） |
| `agentdesk://audit/recent`  | 最近 20 条审计记录              |

**协议层自检 9/9 通过**（用官方客户端连自己启动的 server）：

```
✅ initialize 握手成功   server = agentdesk-ops v0.1.0
✅ list_tools   收到 6 个工具（含 annotations）
✅ call check_disk(web-01)        最高使用率 96% / 级别 critical
✅ call tail_log(web-01, nginx)   命中模式 ['磁盘空间耗尽', '上游服务响应超时']
✅ call search_knowledge          命中 2 条　来源 ['disk-full.md']
✅ 非法参数被拒绝（防注入）        isError=True　原因已传到客户端=True
✅ 调用不存在的工具会报错          返回 isError（进程没有崩）
✅ read agentdesk://tools/catalog 工具数 6
✅ read agentdesk://audit/recent   8 条审计记录
```

> **一个必须说清的设计取舍**：同一份工具有**两份 schema** ——  
> 工具层手写的（给 OpenAI 格式用）和 MCP 层从类型注解自动生成的。  
> 重复一定会漂移，而漂移了**不会报错**，只会让客户端拿到过时的参数说明。  
> 所以写了 `verify_schema_consistency()` 逐项比对，并且**实测验证过它能抓到  
> 三类漂移**（多参数 / 必填项不一致 / 类型写错）—— 一个"永远通过"的校验器  
> 比没有更糟。

### MCP 能挂到哪些地方

| 客户端            | 配置位置                               |
| -------------- | ---------------------------------- |
| Cursor         | `~/.cursor/mcp.json`               |
| Claude Desktop | 客户端配置文件                            |
| 其他 MCP 客户端     | 统一用 `command` + `args` + `cwd` 三件套 |

> ⚠️ `command` 必须用**绝对路径**指向 `.venv\Scripts\python.exe`，  
> 且必须给 `cwd`（项目根目录）—— 客户端启动子进程时不继承你的虚拟环境激活状态。  
> 完整配置片段和排障顺序见 [`docs/mcp-server.md`](docs/mcp-server.md)。

---

## 端到端评测（实测）

> 完整报告：[`eval/reports/`](eval/reports/)　·　方法说明：[`docs/evaluation.md`](docs/evaluation.md)

**40 条问题 × (RAG / 裸模型对照)**，成本 ¥0.55。其中 **10 条是知识库里根本  
没有的问题** —— 专门测「会不会硬答」。

| 指标                    | RAG        | 裸模型对照    |
| --------------------- | ---------- | -------- |
| 答案相关性均分（1-5，仅库内 30 条） | **4.77**   | 5.0      |
| 答案忠实度均分（1-5，仅库内 30 条） | **4.97**   | — （无法测量） |
| **库外问题拒答率**           | **100.0%** | **0.0%** |
| 库内问题作答率               | 96.7%      | 100.0%   |
| 引用编号越界次数              | **0**      | 不适用      |

**按题型分层**：

| 题型              | 用例数 | 检索命中率      | 相关性  | 拒答率        |
| --------------- | --- | ---------- | ---- | ---------- |
| 词面型（用文档原词提问）    | 25  | **100.0%** | 4.88 | 0.0%       |
| 语义型（换一套词表达同一意思） | 5   | 80.0%      | 4.2  | 20.0%      |
| 库外（知识库里根本没有）    | 10  | —          | —    | **100.0%** |

**分层归因**（答案不好，是检索的锅还是生成的锅）：

| 分层    | 用例数 | 相关性均分   |
| ----- | --- | ------- |
| 检索命中  | 29  | **4.9** |
| 检索未命中 | 1   | **1.0** |

差值 3.9 分**全部来自上游检索** —— 该修的是检索，不是提示词。

### 三条值得单独说的结论

**一、`库外拒答率 100% vs 裸模型 0%` 这个差值，就是 RAG 在「不乱说」上的  
可量化收益。** 检索**永远**会返回 top-k 结果，哪怕全都不相关 ——  
所以「知识库里没有」只能靠「读到的东西答不了这个问题」来判断，  
用"检索分数低于阈值就拒答"是行不通的（实测：库外题最高分 0.030-0.033，  
库内题 0.031-0.033，**几乎无差别**）。

**二、忠实度这一栏对裸模型是「—」，不是低分，是「这个指标不存在」。**  
判断一段话有没有超出资料范围，前提是先有资料。  
**这是 RAG 让「有没有编」第一次变成可测量的。**

**三、判定器必须先自证。** 跑评测前用三个构造样本（好答案 / 跑题答案 /  
编造答案）测判定器，三个都要判对 —— 尤其第三个：它给的建议在现实中  
基本都成立，但资料里没有，判定器必须能识别出「超出资料范围」。  
**尺子不准，量出来的数字全是假的。**

> 第一轮跑完，10 条失败样例里**有 9 条是判定器误报**（把回答末尾的  
> 「参考资料中未涉及」当成拒答、拼 context 时漏了 Prompt 里的标题）。  
> 修完后库内作答率从 73.3% 回到 96.7%，失败样例从 10 条降到 1 条。  
> 完整排查过程见 [`docs/evaluation.md`](docs/evaluation.md) 第 4 章。

---

## 进度

- [x] **Day 0** 环境搭建、密钥管理、第一次模型调用
- [x] **Day 1** Python 基础（5 个练习）+ 容错解析结构化输出
- [x] **Day 2** FastAPI 服务 + SSE 流式 + 意图解析 + 告警 webhook + 审计留痕
- [x] **Day 3** RAG 全链路 + 混合检索 + 召回率评测 + 引用溯源问答
- [x] **Day 4** 工具层（6 个运维工具）+ 手写 ReAct + LangGraph 双版本 + 对比评测
- [x] **Day 5** MCP Server：6 个工具暴露成标准协议（含 schema 漂移校验 + 协议层自检）
- [x] **Day 6** Supervisor + 4 个专业 Agent（意图路由/知识检索/工具执行/结果校验）+ 意图路由准确率评测
- [x] **Day 7** Docker 沙箱执行 + Human-in-the-Loop（命令白名单 + 一次性容器 + 审批单 + 指纹防重放）
- [x] **Day 8** 全链路 Trace + 成本看板（自研记录层 + 可选 Langfuse 自托管导出）
- [x] **Day 9** 端到端评测：40 条评测集（含 10 条库外拒答题） + 4 项规则判定 + 模型判分 + 裸模型对照 + 判定器自证
- [x] **Day 10** 部署到公网：公网安全层（白名单鉴权 + 三层限流 + 每日额度）+ Dockerfile + Docker Compose + 复用已有 Nginx Proxy Manager
- [ ] **Day 11-15** 仓库整理 + 简历 + 面试演练 + 第一批投递

---

## 目录结构

```
agentdesk/
├── app/
│   ├── __init__.py      包说明与目录规划
│   ├── llm.py           模型调用统一入口（含 chat_step：带工具调用的一步）
│   ├── main.py          FastAPI 服务入口（22 个接口）
│   ├── security.py      公网安全层：白名单鉴权 + 三层限流 + 每日额度
│   ├── agents/          Agent 编排层
│   │   ├── common.py    两个引擎共用的 Prompt、消息处理、工具执行
│   │   ├── react.py     手写 ReAct 循环（零依赖，原理在这里）
│   │   ├── graph.py     LangGraph 状态图版本
│   │   ├── specialists.py  5 个专业 Agent（含校验 Agent 规则库 + 处置 Agent）
│   │   ├── supervisor.py   Supervisor 多 Agent 编排
│   │   └── compare.py   两个引擎的对比评测
│   ├── tools/           工具层（Agent 的「手」）
│   │   └── ops.py       7 个运维工具（含 run_command）+ 参数白名单 + 风险分级
│   ├── sandbox/         沙箱执行 + 人工确认（Day 7）
│   │   ├── policy.py    命令白名单 + 参数级校验 + 三维决策
│   ├── observability/   可观测（Day 8）
│   ├── evaluation/      评测判定（Day 9）
│   │   └── judges.py    4 项规则判定 + 模型判分 + 判定器自证
│   │   ├── tracer.py    trace/span 记录（contextvar 栈，落 JSONL）
│   │   ├── costs.py     成本核算（含缓存折扣）+ 按维度聚合
│   │   └── langfuse_export.py  可选导出（零新依赖，手写 ingestion）
│   │   ├── executor.py  容器 / 主机 / 仿真三通道，fail-closed
│   │   └── approvals.py 审批单：指纹绑定 + 单次消费 + 追加日志
│   ├── mcp_server/      MCP 出口（同一批工具的第二个调用方）
│   │   └── server.py    7 tools + 2 resources + schema 一致性校验
│   └── rag/
│       ├── loader.py    文档加载与语义段落切分
│       ├── embedder.py  可插拔向量后端（dashscope / local 兜底）
│       ├── store.py     向量存储 + BM25 + RRF 融合检索
│       └── pipeline.py  全链路编排 + RAG 问答 + 召回率评测 + CLI
├── data/
│   ├── knowledge/       知识库语料（6 篇运维排障文档，进仓库）
│   └── index/           构建出的索引（可重建，不进仓库）
├── docs/
│   ├── overview.md           ★ 项目全景：做了什么 / 怎么用 / 要不要上公网
│   ├── project-map.md        项目框架说明书（每个文件干什么、怎么串起来）
│   ├── tech-stack.md         技术栈说明（用了什么 / 替代方案 / 怎么测）
│   ├── react-langgraph.md    ReAct 原理 + 两个实现的对比与取舍
│   ├── mcp-server.md         MCP 原理 + 四个真实坑 + 怎么配客户端
│   ├── multi-agent.md        多 Agent 拆分理由 + 校验 Agent 设计 + 五个坑
│   ├── sandbox-hitl.md       沙箱 + 人工确认：三层职责、五个坑、面试四问
│   ├── observability.md      可观测：三层架构 + 三个坑 + 面试三问
│   ├── evaluation.md         评测：六维度设计 + 判定器自证 + 误报排查
│   ├── python-reference.md   Python 速查手册（含笔试四件套 + 报错速查表）
│   ├── deployment.md         公网部署：安全层设计 + 内存判断 + 运维手册
│   └── knowledge-points.md   知识点清单（面试复习用）
├── eval/
│   ├── qa_set.json      检索召回率评测集（32 个问题，测检索层）
│   ├── rag_eval_set.json  端到端评测集（40 条 = 库内 30 + 库外 10）
│   ├── intent_set.json  意图路由评测集（22 条）
│   └── reports/         跑出来的评测报告（*.md 进仓库，*.json 不进）
├── practice/
│   └── day1/            Day 1 的 5 个练习 + 公共封装
├── scripts/
│   ├── smoke_test.py    九层自检：环境 → 模型 → 检索 → Agent → 沙箱/审批
│   │                    → 观测 → 评测 → MCP → 22 个接口
│   ├── mcp_check.py     MCP 协议层自检（官方客户端连自己，9 项）
│   ├── eval_specialists.py  意图路由 Agent 逐字段准确率评测
│   ├── security_check.py 公网安全层自检（23 项，含"换假 IP 绕限流"用例）
│   └── run_eval.py      端到端评测 + 报告（RAG vs 裸模型对照）
├── check_env.py        Day 0 验收脚本
├── Dockerfile           生产镜像（非 root + 健康检查 + workers=1）
├── docker-compose.yml   生产编排（内存硬上限 + 端口只绑回环 + 复用 NPM 网络）
├── requirements.txt
├── .env                 本地密钥（不进仓库）
├── .env.example         模板
└── .gitignore
```

> 后续补充 `deploy/`（Day 10）。
