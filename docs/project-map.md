# AgentDesk 项目框架说明书

> **这份文件回答一个问题：这个仓库里每个东西是干什么的？**
>
> 代码可以一行行读，但先看懂"骨架"更重要 —— 知道每个文件在整体里承担什么角色，
> 读代码时就不会迷路 —— 先建立这张整体图。
>
> 更新日期：2026-09-26

---

## 第 0 章｜一句话说清这个项目

**AgentDesk 是一个 HTTP 服务，它把「大模型 + 你的私有运维知识 + 能真正执行的操作」打包成别人可以调用的能力。**

三个关键词，缺一不可：

| 关键词 | 靠什么实现 | 没有它会怎样 |
|---|---|---|
| **大模型** | `app/llm.py` 统一调用 DeepSeek | 没有"理解人话"的能力 |
| **私有知识** | `app/rag/` 检索你自己的排障文档 | 只能答通用问题，答不了"你们这台机器" |
| **能执行** | `app/tools/` 7 个运维工具 | 只能"说"，不能"查"，结论无法验证 |

而"服务"这两个字的意思是：**它不是一个聊天框，是一个可以被别的系统调用的地址。**

---

## 第 1 章｜全局地图

```
      ┌──────────────────────────────┐    ┌──────────────────────────────┐
      │  调用它的人 / 别的系统        │    │  外部 MCP 客户端              │
      │  值班工程师 / 告警系统 / 平台  │    │  Cursor / Claude Desktop     │
      └──────────────┬───────────────┘    └──────────────┬───────────────┘
                     │ HTTP                              │ MCP 协议
                     │                                   │ (stdio / HTTP)
      ┌──────────────▼───────────────┐    ┌──────────────▼───────────────┐
      │        app/main.py           │    │    app/mcp_server/server.py  │
      │  27 个路由 + 校验 + 审计留痕   │    │  6 tools + 2 resources       │
      └───┬──────────┬──────────┬────┘    └──────────────┬───────────────┘
          │          │          │                        │
 ┌────────▼──────┐ ┌─▼────────┐ ┌▼──────────────┐       │
 │ app/agents/   │ │app/rag/  │ │ app/llm.py    │       │
 │ Agent 编排层   │ │检索增强   │ │ 模型调用唯一入口│       │
 │ react/graph   │ │loader →  │ │ chat / stream │       │
 │ 想→做→看→想…  │ │embedder →│ │ chat_step ★   │       │
 │               │ │store →   │ │ chat_json     │       │
 │               │ │pipeline  │ └───────┬───────┘       │
 └───────┬───────┘ └────┬─────┘         │               │
         │ 调用工具      │ 检索           │               │
 ┌───────▼───────────────▼───────────────▼───────────────▼───────┐
 │                      app/tools/  （唯一的实现）                 │
 │  check_disk · check_load · check_service ·                     │
 │  list_containers · tail_log · search_knowledge ────────────────┘
 │  参数白名单 + 只读 + 风险分级 + mock/local/ssh                   │
 └────────────────────────────┬───────────────────────────────────┘
                              │ 查真实系统（mock 仿真 / local 本机 / ssh 远程）
                 ┌────────────▼────────────────┐
                 │        DeepSeek API         │  ← 外部
                 └─────────────────────────────┘

          数据侧：data/knowledge/*.md （语料）
                  data/index/*.npz    （向量索引，可重建）
                  logs/audit.jsonl    （审计日志）
          验证侧：eval/qa_set.json    （32 个检索问题 · 测检索层）
                  eval/reports/       （跑出来的报告，可重建）

          部署侧：deploy/langfuse-compose.yml（可选：Langfuse 观测后端）
```

**★ 这张图里最重要的一处是 `app/tools/` 那个框：它同时被两个箭头指向。**

```
app/tools/ 是唯一的实现
    ├── agents/      自家 Agent 循环调用 —— 错误当"观察结果"返回给模型
    └── mcp_server/  外部 MCP 客户端调用 —— 错误抛异常，走协议 isError
```

**同一份工具、同一套安全边界，两个出口、两种错误契约。**
新增一个出口不用复制逻辑 —— 这才是分层真正的好处。
改一次工具实现，所有出口自动生效。

**读这张图的方法**：从上往下是"请求怎么进来的"，从下往上是"数据从哪来的"。

**注意 `search_knowledge` 那条虚线连接** —— 它很特殊：**把 RAG 变成了 Agent 的一只手**。
传统 RAG 是"不管问什么先检索一遍、结果全塞进 Prompt"；这里改成模型自己判断
"这题要不要查资料、用什么词查"。这是 Agent 化 RAG 和传统 RAG 的分水岭。

**分层依赖是单向的**：`main` → `agents` → `tools`。所以换模型只动 `llm.py`、
换工具实现只动 `tools/`、换编排引擎只动 `agents/` —— 上层不用改。

---

## 第 2 章｜逐文件说明

### 根目录

| 文件 | 干什么的 | 为什么需要它 |
|---|---|---|
| `check_env.py` | 环境验收脚本：验证 Python 环境、API Key、网络，并打印 token 用量与成本 | 项目最早产出的脚本，注释里讲的是"为什么这么写"，适合作为读代码的起点 |
| `requirements.txt` | 依赖清单 | **刻意保持纯 ASCII**（英文注释）。中文 Windows 下 pip 用 GBK 读它，有中文就崩 |
| `.env` | 放 API Key | **不进仓库**（`.gitignore` 里）。密钥泄露了别人能花你的钱 |
| `.env.example` | 模板 | 给别人（clone 下来后）看需要配哪些变量，不带真实密钥 |
| `.gitignore` | 决定哪些不进仓库 | 三类别提交：密钥、可重建的产物（索引）、运行数据（日志） |
| `README.md` | 项目门面 | **访客点开仓库第一眼看的就是它**。含问题背景、架构、接口、实测数据、进度清单 |

### `app/` —— 主体代码

| 文件 | 行数 | 干什么的 |
|---|---|---|
| `app/__init__.py` | 13 | 包说明 + 目录规划。相当于一张"这里以后会有什么"的施工图 |
| `app/llm.py` | 277 | **模型调用的唯一入口** |
| `app/main.py` | 562 | **FastAPI 服务入口**，27 个路由（23 个进 OpenAPI） |

**`app/llm.py` —— 为什么它是"唯一入口"**

全项目所有和大模型打交道的地方，都只经过这一个文件。里面提供 4 个函数：

| 函数 | 用途 | 什么时候用 |
|---|---|---|
| `chat(messages)` | 一次性返回完整回答 | 普通问答 |
| `chat_stream(messages)` | 流式返回（同步生成器） | 命令行脚本里 |
| `chat_stream_async(messages)` | 流式返回（异步生成器） | **HTTP 接口里用这个**，高并发不占线程 |
| `chat_json(messages)` | 要模型返回 JSON | 意图解析、结构化输出 |

还有两个"零件"值得记住：

- `extract_delta(line)` —— 解析 SSE 流里的单行，切掉 `data:` 前缀、跳过 `[DONE]`、取 `choices[0].delta.content`。同步版和异步版**共用**它（不复制两份，改规则时不会漏改一处）
- `parse_json_reply(text)` —— 容错解析模型的输出。处理四种情况：纯 JSON、` ```json ` 包裹、` ``` ` 包裹、前后带解释文字

> **为什么必须统一入口**：如果每个模块各写各的 `httpx.post`，等你想换模型、加缓存、加重试、加成本统计时，就要改十几个地方 —— **改漏一个就是一个线上 bug**。

**`app/main.py` —— 大门与 27 个路由**

这个文件的职责是：**接请求 → 校验参数 → 调能力 → 返回结果 → 留痕**。它自己不做任何"智能"的事。

27 个路由分九组（其中 23 个进 OpenAPI）：

| 分组 | 接口 | 说明 |
|---|---|---|
| **基础** | `GET /health` | 健康检查，给容器探活和监控用 |
| | `GET /audit` | 读审计日志：几点、哪条告警、判成什么风险、做了什么 |
| **问答** | `POST /chat` | 一次性返回完整回答 |
| | `POST /chat/stream` | SSE 流式返回（字逐个蹦出） |
| **意图** | `POST /parse` | 把一句人话变成结构化 JSON，失败会自动重试修正 |
| **告警** | `POST /webhook/alert` | **告警驱动的入口 —— 无人值守自动诊断** |
| **RAG** | `GET /rag/stats` | 索引统计（多少篇文档、切了多少块） |
| | `POST /rag/index` | 重建索引 |
| | `POST /rag/search` | 只检索不生成（排查检索质量时用） |
| | `POST /rag/ask` | RAG 问答，**带引用溯源** |
| **Agent** | `GET /agent/tools` | Agent 能调用的工具清单（含风险等级） |
| | `GET /agent/graph` | 导出状态图的 mermaid 定义 |
| | `POST /agent/ask` | ★ **Agent 自主诊断**：它自己决定调哪些工具、跑几轮 |
| **沙箱** | `GET /sandbox` | 当前后端（mock/docker）、白名单概览、fail-closed 状态 |
| **审批** | `GET /approvals` | 审批单列表（按状态过滤） |
| | `GET /approvals/{id}` | 单张审批单详情 + 事件时间线 |
| | `POST /approvals/{id}/approve` | 人工批准（必填 `by`） |
| | `POST /approvals/{id}/reject` | 人工驳回 |
| | `POST /approvals/{id}/execute` | 执行已批准的命令（三道校验：状态 / 只消费一次 / 指纹一致） |
| **观测** | `GET /traces` | trace 列表（耗时、成本、span 数） |
| | `GET /traces/{id}` | 单条 trace 的完整 span 树 |
| | `GET /metrics/summary` | ★ 成本看板：按 Agent / 动作两维聚合 + 缓存命中率 + P95 |

> **`/chat` 和 `/agent/ask` 的区别，就是「聊天机器人」和「Agent」的区别。**
> 前者一问一答；后者会自己决定要查什么、查几轮、什么时候停手，
> 响应里多出 `rounds` / `tool_calls` / `distinct_tools` 这三个普通问答没有的指标。

文件里还有几个关键零件：

- `ChatRequest` / `ChatResponse` / `SearchRequest` / `AgentRequest` —— pydantic 模型，**定义接口收什么、返什么**。FastAPI 靠它们自动校验参数、自动生成文档
- `validate_intent(data)` —— 校验模型返回的 JSON 合不合格（字段齐不齐、risk 取值合不合法）。**不合格就反馈给模型重试**
- `normalize_alerts(payload)` —— 把不同格式的告警（Alertmanager 标准格式、扁平格式）统一成一种结构
- `alert_to_question(alert)` —— 把告警翻译成人话，交给模型去理解
- `write_audit(event, detail)` —— 审计留痕，按 JSONL 追加写

> **一个刻意的设计**：`/webhook/alert` 遇到 `risk=high` 时**不执行任何操作**，返回 `decision: need_human`。
> 这不是能力不足，是**责任边界** —— 生产环境不能靠"相信模型不会删错东西"。
>
> **另一个**：`/agent/ask` 写成 `def` 而不是 `async def`。它内部是同步阻塞的（模型调用 +
> 工具执行要跑好几秒），写成 `def` FastAPI 会丢进线程池；写成 `async def`
> 反而会卡住事件循环 —— **一个请求就把所有人都堵住**。

### `app/tools/` —— 工具层

```
Agent 的「手」：能去查真实的磁盘、日志、服务状态、容器
```

| 文件 | 干什么的 |
|---|---|
| `ops.py` | 7 个工具 + 参数白名单 + 风险分级 + 三后端（mock/local/ssh）+ 注册表 |

六个工具，**全部只读**：

| 工具 | 查什么 | 在哪个场景下是关键证据 |
|---|---|---|
| `check_disk` | 各分区使用率，**顺带给出告警级别** | 磁盘满（最高频故障） |
| `check_load` | 负载 / CPU 核数 / 内存，**顺带算每核负载** | 判断"是不是被压垮了" |
| `check_service` | systemd 服务是否在跑 | 服务挂了 |
| `list_containers` | 容器状态，**标出反复重启的** | CrashLoop 类问题 |
| `tail_log` | 日志尾部，**自动标注命中的已知错误模式** | 定位具体原因 |
| `search_knowledge` | 检索私有知识库 | ★ 见下方说明 |

**三个设计点**：

**1. 阈值判断放在工具里，不放在 Prompt 里。** `check_disk` 直接返回 `level: critical/warning/ok`，
`check_load` 直接算好"每个核上跑了多少任务"。为什么？**阈值是运维标准（行业知识），
不是模型的常识** —— 放在代码里才能改、才能测、才能被 review。
而且模型经常算错"4 核上负载 8.0 高不高"这种事，让它去算就是给它挖坑。

**2. `search_knowledge` 这个工具是把 RAG 做成 Agent 的一只手。**
它和"把检索结果一股脑塞进 Prompt"是两种路子：

| | 传统 RAG 问答 | Agent 化 RAG |
|---|---|---|
| 检索时机 | 不管什么问题都先检索一遍 | 模型自己判断"这题要不要查资料" |
| 检索词 | 用原问题 | 模型自己组织关键词 |
| token | 检索结果全塞进 Prompt | 只在需要时取，取几条也能自己定 |

**3. `local` 后端的关键是 `shell=False` + 列表传参。**
如果写成 `shell=True`，白名单就形同虚设 —— 因为 shell 会解释分号、管道、反引号。
传"列表"等于告诉内核"这是参数列表，不是一段命令"，**shell 根本没机会参与**。
这是防命令注入的根本手段，不是靠黑名单过滤。

### `app/agents/` —— 编排层

```
common.py       两个引擎共用的 Prompt、消息处理、工具执行
react.py        手写 ReAct 循环（零依赖，原理在这里）
graph.py        LangGraph 状态图版本
specialists.py  4 个专业 Agent（多 Agent 拆分）
supervisor.py   Supervisor 编排：调度 + 条件边 + 汇总
compare.py      两个引擎的对比评测 → 输出报告
```

**后来又加了一层：在单 Agent 循环外面套了一层调度。** 三层的关系：

```
supervisor.py        管编排：谁来做、按什么顺序、失败了要不要重来
  └─ specialists.py  4 个专业 Agent（工具执行 Agent 内部又跑一个受限的 ReAct 子图）
       └─ graph.py   ReAct 循环 —— "想 → 做 → 看"
```

**加这一层的时候，`tools/`、`rag/`、`common.py` 一行都没改** ——
改动全在新加的两个文件里。**这就是分层攒下来的复利。**

**为什么同一个循环写两遍**：为了回答「LangGraph 底层在做什么」。
手写版是 `for` + `break`；图里 `add_edge("tools","agent")` 那条回头边就是循环。
**先手写、后框架，顺序不能反** —— 反了只会用框架，答不出原理。

| 手写版 | LangGraph 版 |
|---|---|
| `for step_no in range(1, max_steps+1)` | `add_edge("tools", "agent")` |
| `if not tool_calls: break` | `add_conditional_edges` 返回 `END` |
| 局部变量 `messages/steps/usage` | `AgentState` + reducer，可落盘 |
| 自己画图 | `draw_mermaid()` 白送 |

> **共用 `common.py` 不是偷懒，是对比实验的基本要求**：如果两个版本的工具执行逻辑不一样，
> 跑出来的差异你就分不清是"框架的差异"还是"你自己代码的差异"。**变量只留一个。**

实测对比数据、框架的取舍、踩过的两个坑（消息格式转换 / usage 嵌套字段），
见 [`react-langgraph.md`](react-langgraph.md)。

### `app/mcp_server/` —— MCP 出口

```
server.py   6 tools + 2 resources + schema 一致性校验
```

**它不实现任何工具，只做转发** —— 全部指向 `app/tools/ops.py` 的 `execute_tool()`。

为什么要单独一层：**让同一批工具能被外部客户端调用**。
挂到 Cursor / Claude Desktop 之后，通用助手可以调你的能力 ——
这是 README 第 3 章那个"补上它缺的那块"的技术落点。

| 用了 MCP 的哪部分 | 内容 |
|---|---|
| **tools** | 7 个运维工具（"做一件事"） |
| **resources** | `agentdesk://tools/catalog`（工具清单）、`agentdesk://audit/recent`（审计记录） |
| **annotations** | 协议级的风险提示：`readOnlyHint=True` / `destructiveHint=False` / `idempotentHint=True` / `openWorldHint=True` |
| **prompts** | 没用（暂时没有要固化的提示词模板） |

**三个值得记住的点**：

**一、"做一件事"用 tool，"读一份数据"用 resource。**

"工具清单"如果做成 tool，模型每次用工具前都得先花一次推理去读它 ——
既慢又费 token，而且这个决定本来就不该它做。做成 resource，
客户端直接挂进上下文，零推理成本。

**二、风险提示用协议自带的字段，不自己发明。**

MCP 定义了那四个 hint，**客户端认识它们**，所以能据此在 UI 上提示用户、
或在自动模式下决定要不要弹确认框。自己发明一个 `risk_level` 塞进 `meta`，
客户端不认识，等于白写。**标准协议的价值就是大家约好用同一套词。**

**三、同一份工具，两种错误契约。**

| 调用方 | 错误怎么处理 | 为什么 |
|---|---|---|
| `agents/`（自家 Agent） | 当"观察结果"返回给模型 | 模型要看到自己参数写错了才能改 |
| `mcp_server/`（外部客户端） | 抛异常，走协议 `isError` | 客户端是程序，需要明确区分成功/失败 |

**这不是不一致，是因为面向的调用方不同，契约也就不同。**

**一个必须正视的取舍**：同一个工具有**两份 schema**（工具层手写的 / MCP 从类型注解生成的）。
重复一定会漂移，而漂移了**不会报错**。所以写了 `verify_schema_consistency()`
逐项比对，并且**实测验证过它能抓到三类漂移**。

> **一个"永远返回通过"的校验器比没有更糟** —— 它会带来虚假的安全感。

完整原理、四个真实踩到的坑（SDK 2.x 改名 / 参数描述只能靠 Field /
**错误类型决定消息能否传到客户端** / structured_output 拒绝 dict）、
客户端配置与排障顺序，见 [`mcp-server.md`](mcp-server.md)。

### `app/rag/` —— 检索增强

```
loader.py     文档 → 小块      （读进来、切开）
embedder.py   小块 → 向量      （把文字变成数字）
store.py      存起来 + 检索    （向量检索 + 关键词检索 + 融合）
pipeline.py   串起来 + 评测    （编排 + 问答 + 算分）
```

| 文件 | 行数 | 干什么的 |
|---|---|---|
| `loader.py` | 211 | 加载 Markdown + **语义段落切分**。先按空行切成语义段落，再合并到目标长度（400 字），块间留 80 字重叠。产出 `Document` 和 `Chunk` 两个数据结构 |
| `embedder.py` | 166 | 文本向量化，**可插拔后端**。有 Key 就用阿里云百炼（真语义），没 Key 就降级到本地哈希词袋（只用于跑通链路） |
| `store.py` | 289 | 存放向量 + 两路检索 + 融合。含 `VectorStore`、`BM25`、`rrf_fuse`、`tokenize`（中文分词） |
| `pipeline.py` | 371 | 编排全链路 + RAG 问答（带引用）+ **召回率评测** + 命令行入口 |

**四个必须理解的设计决定**：

**1. 为什么要切分？** 模型一次能看的文本有限（上下文窗口），而且"只把相关的那几段给它"才是检索的意义。切太小语义不完整，切太大无关内容稀释相关性 → 中文实践值 300~500 字。

**2. 为什么要重叠？** 关键信息很可能刚好被切在边界上 —— 前半句在上一块、后半句在下一块，两块单独看都不完整。留 10%~20% 重叠能缓解。

**3. 为什么不能只用向量检索？** 纯向量**对精确词不敏感**。它擅长"按意思找"，但分不清 `OOMKilled` 和 CPU 高 —— 这两个词的向量可能离得很近。而运维场景里全是精确词（`Exit Code 137`、`EADDRINUSE`）。所以要 **向量管语义 + BM25 管精确**，两路跑完再融合。

**4. 为什么用 RRF 融合，而不是把分数加权平均？** 两路的分数量纲完全不同：余弦相似度在 -1~1，BM25 是 0~∞ 的无界值。硬加权等于拿两把不同刻度的尺子量同一件事。**RRF 只看排名不看分数**，天生不受量纲影响。

### `data/` —— 数据

| 路径 | 内容 | 进仓库吗 |
|---|---|---|
| `data/knowledge/` | 6 篇真实运维排障文档：nginx 502、磁盘满、Docker 退出码、MySQL 连接数、Linux 负载高、K8s CrashLoop | ✅ **进**。语料是项目内容，别人 clone 下来才能复现 |
| `data/index/` | 向量索引文件（`.npz`） | ❌ 不进。它是可重建的产物，而且和 embedding 后端绑定 |

> **一个坑**：换了 embedding 后端，索引必须重建。**不重建不报错，只是"莫名其妙查不准"**。
> 所以索引文件里记录了后端名和维度，载入时校验，不匹配直接报错 —— **把静默错误变成显式错误**。

### `eval/` —— 验证（两套评测，测的是两层）

| 文件 | 内容 | 测哪一层 |
|---|---|---|
| `qa_set.json` | 32 个检索问题，分「词面型 / 语义型」两类 | **检索层**：标准文档有没有进 top-k |
| `rag_eval_set.json` | 40 个端到端问题（库内 30 / **库外 10**） | **端到端**：最终那段回答靠不靠谱 |
| `intent_set.json` | 22 条意图标注用例 | 意图路由 Agent 的逐字段准确率 |
| `reports/*.md` | 跑出来的报告，**进仓库**（访客点开就该看到真实数字） | |

**为什么要分层测**：「检索 100% 命中」和「答案是对的」是两件事。
检索漏了 → 答案必错；检索对了 → 答案仍可能跑题、编造，
或者**对着知识库里根本没有的问题一本正经地硬答**。

**★ 库外那 10 条是整个评测集里最重要的部分**：检索**永远会返回 top-k 条结果**，
哪怕全都不相关 —— 所以「知识库里没有」这件事，模型没法靠「检索为空」得知，
只能靠「读到的东西答不了这个问题」来判断。
绝大多数人做的 RAG 从不测这一项，上线后就对着库外问题硬答，而且**无法审计**。

**为什么要分「词面型 / 语义型」**：语义型问题**刻意用完全不同的词**表达同一个意思
（问"应用起来马上就退出、来回循环"而全篇不提 Pod、CrashLoopBackOff、kubectl）。
混在一起算平均，词面型会把差距全部掩盖掉 —— 分开统计才看得出"向量检索到底有没有在干活"。

### `docs/` —— 文档

| 文件 | 定位 | 怎么用 |
|---|---|---|
| `project-map.md` | **本文件**。项目框架说明 | 迷路时回来看 |
| `tech-stack.md` | 技术栈说明：用了什么 / 替代方案 / 区别 / 怎么用与测 | 想了解选型理由时看 |
| `deployment.md` | 从本地到公网的部署过程、安全层设计、运维手册 | 想了解怎么上线时看 |

### `scripts/` —— 工具脚本

| 文件 | 干什么的 |
|---|---|
| `smoke_test.py` | **全链路自检**。一条命令跑九层：环境 → 模型 → 检索 → Agent → 沙箱 → 观测 → 评测 → MCP → HTTP 接口，最后给 ✅/❌ 汇总表。服务没启动时最后一层自动跳过（退出码仍为 0），可以接进自动化。`--full` 才跑花钱项 |
| `show_live.py` | **看真机原始数据**。不经过模型，直接调工具层把原始 JSON 打出来 —— 用来确认"它到底从机器上读到了什么"。支持 `--host` / `--service` / `--only` |
| `demo.py` | 现场演示：走 HTTP 接口 + 真实调用模型，证明整条链路通了 |
| `run_eval.py` | **RAG 端到端评测**。40 条 × (RAG / 裸模型对照)，产出带数字的 markdown 报告。支持 `--limit` 试跑、`--resume` 断点续跑 |
| `eval_specialists.py` | 意图路由 Agent 的逐字段准确率评测 |
| `mcp_check.py` | MCP 协议层自检（官方客户端连自己，9 项） |
| `security_check.py` | 公网安全层自检（鉴权 / 限流 / 配额，23 项） |

> **`smoke_test.py` 和 `show_live.py` 的分工**：前者回答"有没有坏"（逐层断言、给退出码），
> 后者回答"真机数据长什么样"（不做任何判断，原样打印）。
> 而 Agent 的回答是**模型转述之后**的结果 —— 想分清哪部分是真实数据、
> 哪部分是模型的表述，就得用 `show_live.py` 把模型摘掉。

> **和 `check_env.py` 的区别**：`check_env.py` 只验最初那四件事（Python、Key、网络、成本公式），
> 是"最小环境验收"；`smoke_test.py` 是**完整的全链路自检**，每次改完代码都能跑一遍确认没弄坏别的东西。

---

## 第 3 章｜一次请求怎么走（七条主线）

### 主线 A：普通问答（`POST /chat`）

```
用户提问
  → main.py 校验请求参数（pydantic）
  → llm.py 组装 messages 并请求 DeepSeek
  → 拿到回答
  → 返回 JSON
```

### 主线 B：RAG 问答（`POST /rag/ask`）★ 目前最完整的一条

```
用户提问「nginx 报 502 怎么排查」
  → ① 问题向量化           embedder.py
  → ② 两路检索并行          store.py
       向量检索（按语义找）+ BM25（按关键词找）
  → ③ RRF 融合成一路        store.py
  → ④ 取 Top-K 块
  → ⑤ 把「块内容 + 原始问题」拼成 Prompt   pipeline.py
  → ⑥ 调模型生成回答        llm.py
  → ⑦ 返回回答 + 引用来源   ← 用户能看到"这个结论出自哪篇文档"
```

**第 ⑤ 步是 RAG 的灵魂**：不是让模型"回忆"，而是把证据摆在它面前，让它**基于给定材料回答**。
**第 ⑦ 步是可信度的来源**：没有引用的 RAG，用户没法判断它是不是在编。

### 主线 C：告警驱动的无人值守（`POST /webhook/alert`）★ 差异化最强的一条

```
告警系统推送（凌晨三点，你不在场）
  → ① 告警归一化            normalize_alerts()
  → ② 翻译成人话            alert_to_question()
  → ③ 意图解析（JSON）      parse_intent()  ← 失败会自动重试修正
  → ④ 按风险分级决策
       risk=high → decision: need_human   （停手，等人确认）
       其他      → decision: auto_diagnose（给出诊断预案）
  → ⑤ 审计留痕              write_audit()
```

**这条链路是「通用 AI 助手做不到」的技术落点**：通用助手要你打开界面打字；这个接口是给告警系统调的 ——
**你不在场，它自己起来干活。**

### 主线 D：Agent 自主诊断（`POST /agent/ask`）★ 最新的一条

**它和前面三条有本质区别：前面三条的流程是写死的，这一条的流程是模型自己决定的。**

```
用户提问「web-01 上的网站很慢，有时报 502」
  → ① 组装 messages（system prompt + 问题）
  → 循环开始（最多 max_steps 轮）：
      ② 调模型，带上 7 个工具的 schema      chat_step(messages, tools=...)
      ③ 模型返回 tool_calls？── 没有 ──→ 它要回答了，跳出循环
      ④ 有 → 逐个执行工具                    execute_tool()
              参数校验 → 执行 → 结果转成文本
      ⑤ 把 tool 消息追加回对话历史            role:"tool" + tool_call_id
      ⑥ 回到 ②，让模型看结果继续决策
  → ⑦ 撞上限则去掉 tools 强制收口
  → ⑧ 返回 answer + trace + metrics，写审计
```

**实测一次（Q1）走了 3 轮、10 次工具调用**：

| 轮 | 模型决定了什么 |
|---|---|
| 1 | 先看主机整体：`check_disk` `check_load` `check_service` `list_containers` |
| 2 | 磁盘满了，查日志 + 查经验：`tail_log` `search_knowledge` |
| 3 | 信息够了，出结论（不再调工具） |

**这三步没有一步是预先写死的。** 这就是 ReAct。

**四道护栏**（生产环境的必要条件）：

| 层 | 护栏 |
|---|---|
| 工具层 | 参数白名单正则 + `shell=False` 列表传参 —— 绝不拼 shell |
| 循环层 | `max_steps` 硬上限 + 重复调用检测（同工具同参数查第二次就提示它） |
| 收口 | 撞上限时**去掉 tools** 再问一次，让它只能输出文字 |
| 框架层 | `recursion_limit` 第二道保险 |

> 完整的循环实现、两个引擎的对比数据、踩过的坑，见 [`react-langgraph.md`](react-langgraph.md)。

### 主线 E：外部客户端调用工具（MCP 协议）★ 唯一一条"出口"方向的主线

**前四条主线都是"请求进来"，这一条是"能力出去"。**

```
外部 MCP 客户端（Cursor / Claude Desktop）
  → ① 客户端把 server 当子进程启动          command + args + cwd
  → ② initialize 握手                       拿到 serverInfo 与会话 ID
  → ③ tools/list                            拉到 7 个工具的 schema + 风险提示
  → ④ 用户在 Cursor 里问「web-01 磁盘满了吗」
       Cursor 自己的模型决定调用 check_disk
  → ⑤ tools/call {"name":"check_disk","arguments":{"host":"web-01"}}
       → mcp_server 转发给 tools/ops.execute_tool()
       → 参数白名单校验 → 执行 → 返回
  → ⑥ 结果以 JSON 文本放在 content 里返回，isError=false
  → ⑦ Cursor 拿着真实数据回答用户
```

**这条链路的意义**：你不用打开 AgentDesk，也不用大模型知道 AgentDesk 存在 ——
**它的能力已经被挂进了你日常用的工具里。**

**两种传输方式**：

| | stdio | streamable-http |
|---|---|---|
| 怎么跑 | 客户端把 server 当子进程 | 独立进程，走 HTTP |
| 安全 | 不开端口 | 要自己管鉴权 |
| 适合 | 本地（Cursor 默认） | 远程、多客户端共享 |

> ⚠️ **stdio 模式有个必须记住的规矩：stdout 是协议通道。**
> 往 stdout 打印任何调试信息都会破坏协议，而且报错完全看不出原因。
> 所以 `server.py` 里所有提示都走 `stderr`。

完整的原理、四个真实踩到的坑、客户端配置与排障顺序，
见 [`mcp-server.md`](mcp-server.md)。

### 主线 F：多 Agent 编排（`POST /agent/ask` engine=supervisor）★ 最新的一条

**和主线 D 的区别**：主线 D 是一个 Agent 自己从头跑到尾；
这条是**四个 Agent 分工协作，中间还有一道校验岗**。

```
用户提问「web-01 上的网站很慢，有时报 502」
  → 循环开始（每一步干完都回到 supervisor）：
      ① supervisor 决策：先做意图分类
         ② intent Agent（无工具）→ {task_type: diagnose, hosts: [web-01],
                                   symptoms: [访问慢, 502], needs_knowledge: true}
      ③ supervisor 决策：意图说需要经验 → 去查知识库
         ④ knowledge Agent → 检索「访问慢 502 排查 处理」，命中 3 篇
      ⑤ supervisor 决策：意图说需要现场数据 → 派给工具执行 Agent
         ⑥ diagnose Agent → 内部跑一个**受限 ReAct 子图**
                            （只有 5 个运维工具，没有 search_knowledge）
      ⑦ supervisor 决策：结论出来了 → 先校验
         ⑧ verify Agent → **先跑规则**（数值溯源 / 越权声明 / 有无证据）
                          规则过了才**再问模型**判因果链
      ⑨ 校验没过且有额度 → 回到 ⑥ 重查（带上上次的问题 + 已查过的数据）
      ⑩ supervisor 决策：收工 → finalize
  → 汇总输出：路由标签 + 诊断结论 + 参考文档 + 校验结论
```

**实测顺利路径**：`intent → knowledge → diagnose → verify → finalize`，
5 次工具调用、token 4286、8.8 秒。

**实测带重试路径**：`intent → knowledge → diagnose → verify → diagnose → finalize`，
12 步。

**★ 这条链路最大的价值在 ⑧ 那一步。**
前面说过"模型永远不执行任何东西"；多 Agent 这里补上另一半：
**它也不该是唯一判断自己对不对的人。**

```
让模型自己"再检查一遍" → 同一批信息、同一个脑子，自证清白
本项目的做法           → 能规则化的用规则（确定、可复现、零成本、无幻觉），
                        规则查不了的才用模型，而且规则先跑
```

完整的拆分理由、校验规则的三轮误报收窄、五个真实坑，
见 [`multi-agent.md`](multi-agent.md)。

---

---

### 主线 G：沙箱执行 + 人工确认

**这是唯一一条"会改系统"的链路**，所以它比前面所有主线都多了一道门。

```
模型（处置 Agent）想执行 truncate -s 0 /var/log/nginx/error.log
  → policy.decide()
      白名单命中 truncate → 决策 needs_approval
      路径在 /var/log 下、是 .log → 通过
      算指纹（argv + 通道 + 挂载）
  → approvals.create()
      生成 ap-xxxxxxxx，状态 pending，追加写入 logs/approvals.jsonl
  → 工具把审批单作为观察结果返回
      模型据此回答「已提交审批，等待人工确认」（而不是「已清理」）
  → 人：GET /approvals?status=pending 看到它
        POST /approvals/{id}/approve {by:"张三"}   ← by 必填
        POST /approvals/{id}/execute
  → 执行时三道校验（都在 store 状态机里，接口层绕不过）：
      ① 状态必须 approved（挡住没批就执行）
      ② 只能消费一次，consumed 是终态（挡住重放）
      ③ 命令指纹必须与审批时一致（挡住 TOCTOU）
  → executor.run() → 一次性容器：无网络、根只读、掉全部 capability
  → 结果写审计；审批单 → consumed
```

**★ 这条链路有两条安全规则，是全项目最重要的两句话：**

```
1. 结论没过校验，就不许据此动手。
   —— Supervisor 只在 verdict.pass 为真时才派发处置 Agent。

2. 沙箱不可用就 fail-closed，不降级。
   —— 探测不到 Docker 时落到 mock 仿真，绝不悄悄变成无隔离执行。
```

完整的策略设计、三个坑、关键设计决策，见 [`sandbox-hitl.md`](sandbox-hitl.md)。

---

## 第 4 章｜还没做的是什么

`app/__init__.py` 里写着的目录规划，目前这些还是空的：

| 目录 | 计划做什么 | 对应能力 |
|---|---|---|
| `deploy/` | docker-compose + Nginx + HTTPS | 部署与稳定性 |

（自托管 Langfuse 的 compose 已经写好放在 `deploy/langfuse-compose.yml`，
本机 Docker 引擎启动后 `docker compose up -d` 即可，属可选增强项。）

**已经做完的**：

| 目录 | 状态 | 对应能力 |
|---|---|---|
| `app/rag/` | ✅ 已完成 | RAG、向量检索、混合检索 |
| `app/tools/` | ✅ 已完成 | 工具调用 / Function Calling |
| `app/agents/` | ✅ 单 Agent<br>✅ 多 Agent<br>✅ 处置 Agent | Agent 框架、ReAct、编排、多 Agent 协同 |
| `app/sandbox/` | ✅ 已完成 | 沙箱执行、权限控制、人工确认（HITL） |
| `app/mcp_server/` | ✅ 已完成 | MCP、工具生态、协议对接 |
| `app/observability/` | ✅ 已完成 | AgentOps、可观测性、Token 成本 |
| `app/evaluation/` | ✅ 已完成 | **评测体系**、LLM-as-Judge、拒答能力 |

**这张表就是"下一步路线图"。** 照着它看，
能清楚知道一个完整的 Agent 系统还该有哪些部件。

---

## 第 5 章｜怎么用这份地图

**自己查阅时**：
- 想不起来某个文件干什么 → 回来看第 2 章
- 新加文件时 → 先想清楚它属于哪一层（大门？能力？数据？验证？），再决定放哪个目录

**向别人介绍时**：
- 被问"介绍一下这个项目" → 先讲第 0 章那句一句话总结，再讲第 1 章那张图
- 被问"架构是什么样的" → 讲五个方框：`main.py`（大门）、`agents/`（大脑）、
  `tools/`（手）、`rag/`（知识）、`mcp_server/`（对外出口）
- **被问"这跟直接用 Cursor 有什么区别"** → 讲第 1 章那句
  "`tools/` 被两个箭头指向" —— 本项目不做替代品，做的是它能接进去的那一块
- 被问"项目里有什么难点" → 挑第 2 章里那几个"设计决定"讲（为什么切分、为什么混合检索、
  为什么 RRF、为什么工具里做阈值判断、为什么两个引擎都写、为什么 MCP 要校验 schema 漂移）
- **被问"多 Agent 是不是过度设计"** → 答「拆分的四条收益 + 三条代价」，
  再补一句"**拆分的门槛是有没有需要被单独评测或隔离上下文的部件**"（第 2 章的 agents 段）
- **被问"结论怎么保证是对的"** → 讲校验 Agent：**规则先跑、模型兜底**，
  以及"让模型自检是自证清白"这个理由
- 被问"敢让 Agent 碰生产系统吗" → 讲 sandbox + HITL：
  **结论没过校验不许动手 / 写操作必须人批 / 沙箱不可用就 fail-closed**
- 被问"还没做什么" → 讲第 4 章那张表

**一句话记住整个项目**：

> **一个 HTTP 服务：门口 27 个路由，里面靠"模型调用 + 私有知识检索 + 工具执行"
> 三条腿走路，高危操作停下来等人确认，每一步都留痕；
> 同一批工具还通过 MCP 协议挂给外部 AI 客户端用。**
