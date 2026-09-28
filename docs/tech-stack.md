# AgentDesk 技术栈说明

> **这份文件回答四个问题**：我用了什么 / 怎么实现的 / 还能怎么做，区别在哪 / 我怎么用和怎么测。
>
> **能说出"我为什么选它"只是起点，能说出"什么情况下我会换掉它"才是分水岭** ——
> 因为后者说明理解了这个技术的边界，而不是照着教程抄的。
>
> 更新日期：2026-09-26

---

## 第 0 章｜一页速览

**整个项目的直接依赖只有 9 个。** 这是刻意的——依赖越少，出问题的面越小，也越容易讲清楚。

> 但要注意一个数字：`pip list` 里实际有 **64 个包**（实测）。
> 多出来的 55 个全是传递依赖 —— 装 `langgraph` 带进 `langchain-core`、
> `langgraph-checkpoint`、`langsmith`、`orjson`、`zstandard` 等；
> 装 `mcp` 又带进 `sse-starlette`、`httpx-sse`、`jsonschema` 等。
>
> **"我只装了 9 个包"和"环境里有 64 个包"是两件事。**
> 清楚这个区别，说明真的看过环境，
> 而不是只背了 requirements.txt。

| 层 | 用了什么 | 版本 | 在哪个文件 | 一句话职责 |
|---|---|---|---|---|
| 语言 | Python | 3.12.9 | 全部 | |
| HTTP 客户端 | `httpx` | ≥0.27 | `llm.py` `embedder.py` | 发 HTTP 请求调模型 API |
| 配置与密钥 | `python-dotenv` | ≥1.0 | `llm.py` `embedder.py` | 把 `.env` 读进环境变量 |
| Web 框架 | `fastapi` | ≥0.115 | `main.py` | 把代码变成 HTTP 服务 |
| ASGI 服务器 | `uvicorn[standard]` | ≥0.32 | 启动命令 | 真正跑 FastAPI 的那个进程 |
| 数据校验 | `pydantic` | ≥2.9 | `main.py` | 定义接口收什么、返什么，自动校验 |
| 数值计算 | `numpy` | ≥1.26 | `store.py` `embedder.py` | 向量存成矩阵，相似度=一次矩阵乘法 |
| 中文分词 | `jieba` | ≥0.42 | `store.py` | 给 BM25 分词（缺了会退化成字符 bigram） |
| **Agent 编排** | **`langgraph`** | **≥0.2（装的 1.2.12）** | `agents/graph.py` | **状态图编排：检查点、可视化、按节点流式** |
| **工具协议** | **`mcp`** | **≥2.0（装的 2.2.0）** | `mcp_server/server.py` | **把 7 个工具暴露成标准协议，给外部客户端调** |

**没用的东西同样值得记住**（"为什么不用 X"和"为什么用 X"一样重要）：

| 没用 | 为什么 |
|---|---|
| **LangChain 全家桶** | 只引了 `langgraph`。**没有引 `langchain`、chains、agents 那套**——本项目手写协议调用，不需要它。LangGraph 是独立的编排层，可以单独用 |
| **OpenAI SDK** | 用 `httpx` 手写。SDK 是黑盒，出问题时你不知道它到底发了什么请求。手写一遍，`messages` 数组、`tools` 参数、`stream`、`temperature` 你都亲眼见过 |
| **Milvus / Qdrant / FAISS** | 50 个块的规模用不上。向量库解决的是"千万级向量的近似最近邻"，我这儿一次矩阵乘法几毫秒就完了 |
| **requests** | 它不支持流式 + 异步。而流式和高并发是项目的核心需求 |
| **Flask / Django** | 都不是原生异步，做 SSE 流式要绕路；FastAPI 原生 async + pydantic 直接省掉一半参数校验代码 |
| **PydanticAI / AutoGen / CrewAI** | 同类 Agent 框架。LangGraph 胜在"状态图 + 检查点"这套心智模型最贴近"可中断、可恢复、可观测"的生产需求 |

---

## 第 1 章｜逐项展开

每一项都按同样四段写：**怎么实现的 → 替代方案 → 区别与取舍 → 怎么验证**。

### 1. Python 3.12

**怎么实现的**：`python -m venv .venv` 建虚拟环境，所有依赖装在项目自己的 `.venv/` 里，不污染系统。

**为什么是 3.12 而不是最新的 3.13**：AI 生态对最新版 Python 往往慢一拍，很多库的依赖是按 3.10–3.12 编译验证的。**用 3.12 能少踩一堆"装不上"的坑。**

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| Python 3.10 / 3.11 | 都能跑本项目。3.10 起才有 `match`、`X \| None` 这种写法 | 库只支持到 3.10 时 |
| Python 3.13 | 性能更好，但部分库的轮子还没跟上 | 等半年后生态跟上 |
| Node.js / Go / Java | Agent 生态的库（LangGraph、Ragas、MCP）**首先且主要支持 Python**，用别的语言等于放弃生态 | 除非团队强制要求 |
| **conda / uv / poetry** | 不是替代语言，是替代 `venv + pip` 的管理工具。**`uv` 装包快 10–100 倍**，且自带锁文件 | 依赖变多、装包开始变慢时（很推荐） |

**怎么验证**：

```bash
.venv\Scripts\python.exe --version          # 应显示 3.12.x
.venv\Scripts\python.exe -m pip list        # 确认 7 个包都装了
```

---

### 2. httpx —— 手写调模型（而不是用 SDK）

**怎么实现的**（`app/llm.py`）：

```python
resp = httpx.post(
    cfg["url"],
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    json={"model": cfg["model"], "messages": messages, "temperature": temperature},
    timeout=60,
)
answer = resp.json()["choices"][0]["message"]["content"]
```

流式则用 `httpx.stream(...)` + `iter_lines()`，逐行解析 `data: {...}`。

**为什么不用 `openai` SDK**：SDK 把请求包装起来了，你只看到 `client.chat.completions.create(...)`。**出问题时，你不知道它到底发了什么、超时怎么设、重试几次。** 手写一遍之后，`messages` 数组、`temperature`、`stream`、SSE 的每一行长什么样，你都亲眼见过。

**替代方案的区别**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **`openai` SDK** | 少写几十行，自带重试/超时/类型提示。**但底层是同一个 HTTP 协议**（DeepSeek、通义都兼容 OpenAI 格式） | 项目进入生产、要接很多家模型时 |
| `requests` | 最流行，但**只能同步**，且流式支持弱（`iter_lines` 有但不优雅，无异步） | 写一次性脚本时够用 |
| `aiohttp` | 异步，性能好，但 API 比 httpx 繁琐，且没有同步版本 | 极端追求性能时 |
| `urllib`（标准库） | 零依赖，但写法原始，要自己管编码和连接池 | 不想装任何包时 |

> **本项目的一个设计点**：因为两家都是 OpenAI 兼容格式，`PROVIDERS` 字典里只有 `url` 和 `model` 不同，其余代码完全一样。**换服务商是改配置，不是改代码**——这就是"统一入口"的价值。

**怎么验证**：

```bash
# 脚本内直接测（第 2 层）
.venv\Scripts\python.exe scripts\smoke_test.py
```

---

### 3. 模型服务：DeepSeek（OpenAI 兼容协议）

**怎么实现的**：`app/llm.py` 的 `PROVIDERS` 字典，按 `.env` 里配了哪个 Key 自动选择：

| 服务商 | 地址 | 模型 | 环境变量 |
|---|---|---|---|
| DeepSeek | `api.deepseek.com/chat/completions` | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| 阿里云百炼 | `dashscope.aliyuncs.com/compatible-mode/v1/...` | `qwen-plus` | `DASHSCOPE_API_KEY` |

**为什么选 DeepSeek**：便宜到"可以随便试错"（一次问答约 ¥0.00017，10 元能跑 5 万多次）。**在需要反复试错的开发阶段，"不怕花钱"本身就是生产力**——不敢跑实验的人学不会。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| 通义千问 / 智谱 / Kimi | 同样是 OpenAI 兼容，改配置即可。**国内直连**，不用管网络 | 需要更长上下文、或多模态时 |
| OpenAI / Claude | 能力强，但**国内需要网络代理，且贵 10–50 倍** | 面向海外业务时 |
| **本地 Ollama / vLLM** | 数据不出内网（合规场景刚需），零调用成本，但**要显卡**，且小模型效果明显下降 | 处理敏感数据、或要跑微调模型时 |
| 多模型混用 | 意图解析这种简单活派给便宜小模型，复杂推理给大模型。**成本能降一半以上** | 调用量大起来之后（这是本项目 `PROVIDERS` 已经留好的扩展点） |

**怎么验证**：`scripts/smoke_test.py` 的第 2 层会真实发一次请求并打印耗时与回答。

---

### 4. FastAPI + uvicorn —— 从「工具」到「服务」

**怎么实现的**（`app/main.py`）：

```python
app = FastAPI(title="AgentDesk", version="0.2.0")

class ChatRequest(BaseModel):          # 请求长什么样
    question: str
    top_k: int = Field(3, ge=1, le=10)

@app.post("/chat", response_model=ChatResponse)   # 声明式定义接口
async def chat_endpoint(req: ChatRequest):
    ...
```

**FastAPI 带来三件白送的东西**：
1. **参数自动校验** —— 声明了类型，错的请求根本进不来
2. **自动生成交互式文档** —— `/docs` 页面，不用写前端就能点着测
3. **原生异步** —— SSE 流式是它的原生能力

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **Flask** | 最轻最熟，但**原生同步**。做 SSE 要开线程或用 gevent，且没有自动文档、没有 pydantic 校验 | 只写几个内部小接口时 |
| **Django** | 全家桶（ORM、Admin、认证），但**重**。本项目不需要数据库和后台 | 做完整业务系统时 |
| **Litestar** | FastAPI 的现代替代，性能和类型支持更好，但**生态小、资料少** | 愿意接受新框架时 |
| **Gin（Go）/ Spring Boot（Java）** | 性能/生态好，但要写两套语言。**AI 生态在 Python，混用不划算** | 团队已有 Go/Java 服务时 |
| uvicorn → **gunicorn + uvicorn worker** | 生产标准做法：多进程 + 进程管理。**单 uvicorn 进程挂了服务就挂了** | 上线时必换 |

**怎么验证**：

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000/docs —— 可以直接点着测每个接口
```

---

### 5. pydantic v2 —— 参数校验与数据契约

**怎么实现的**：`main.py` 里每个接口的请求/响应都先定义成模型：

```python
class ChatRequest(BaseModel):
    question: str
    top_k: int = Field(3, ge=1, le=10)     # 默认 3，且必须在 1~10 之间
```

**它的真正价值不是"校验"，是"契约"**：模型类读起来就是这份接口的文档——什么必填、什么可选、取值范围多少，一目了然。而且 FastAPI 用它自动生成 `/docs` 里的表单。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| 手动 `if "question" not in body` | 零依赖，但**每个接口都要写一遍校验**，加字段时容易漏 | 只有一两个接口时 |
| `dataclass` | 标准库、轻，但**不做类型转换和校验**——传进来字符串不会变成数字 | 纯内部数据结构，不经 HTTP |
| `marshmallow` | 老牌校验库，功能全，但**要单独写 schema 类**，比 pydantic 啰嗦 | 用 Flask 时常见搭配 |
| `attrs / msgspec` | 更快，但生态和文档不如 pydantic | 追求极致性能时 |

**怎么验证**：故意发一个错请求，看它自己拦下来：

```bash
# top_k 传 999（超出 ge/le 限制）→ 应返回 422，并告诉你哪里错了
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" -d "{\"question\":\"test\",\"top_k\":999}"
```

---

### 6. SSE —— 流式输出

**怎么实现的**：`StreamingResponse` + 异步生成器，每块输出 `data: {...}\n\n`：

```python
async def event_stream():
    async for delta in chat_stream_async(messages):
        yield f"data: {json.dumps({'delta': delta})}\n\n"
    yield "data: [DONE]\n\n"

return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**为什么用 SSE 而不是 WebSocket**：SSE 是**单向**的（服务端→客户端）、跑在普通 HTTP 上。而问答场景恰好就是单向的。WebSocket 是双向全双工，为这个场景引入它等于凭空多一套连接管理、心跳、重连逻辑。

**三个必须知道的坑**：

1. **Nginx 默认缓冲会把流攒够一批才转发** —— 等于流式退化成非流式。解法是响应头加 `X-Accel-Buffering: no`（上线时会用到）
2. **流一旦开始，就再也改不了 HTTP 状态码** —— 所以出错时只能把错误当一条数据推给客户端
3. **别在 `async def` 里用同步客户端** —— 会把事件循环卡死，异步白写

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **WebSocket** | 双向。适合"用户随时打断模型"、多轮实时协作 | 要做可打断的对话时 |
| **长轮询** | 兼容性最好（老浏览器也支持），但**每次都要重发一个请求**，服务端压力大、延迟高 | 环境不支持 SSE 时 |
| 普通一次性返回 | 实现最简单 | 短回答、内部接口。**但长回答时用户要盯着空白等十几秒** |
| 前端轮询任务状态 | 适合超长任务（跑几分钟），前端定时问"好了没" | 批处理类任务 |

**怎么验证**：

```bash
curl -sN -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" --data-binary @_q.json
```

`-N` 关掉 curl 自己的缓冲，**才能看到字是逐块到达的**（不加 `-N` 你会以为流式没生效）。

---

### 7. 向量化（embedding）—— 可插拔后端

**怎么实现的**（`app/rag/embedder.py`）：按可用性自动降级。

| 后端 | 模型 | 维度 | 说明 |
|---|---|---|---|
| `dashscope` | `text-embedding-v3` | 1024 | 阿里云百炼，**真正的语义向量**（推荐） |
| `local` | 哈希词袋 | 512 | 零依赖兜底，**只用来跑通链路**，本质还是词面匹配 |

**为什么做成可插拔**：让整条链路在"没有任何额外 Key"的情况下也能跑通和自测。**但要说清代价**——`local` 没有语义能力，用它的评测数据不能当真实效果。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **BGE-M3 / bge-large-zh** | 中文语义效果最好的开源模型之一。**本地跑，零调用费、数据不出内网**，但要下模型（几百 MB）且占内存 | 有显卡、或数据敏感时（**这是本项目下一步的推荐升级**） |
| OpenAI `text-embedding-3` | 效果好，但需代理且有成本 | 海外业务 |
| `m3e` / `text2vec` | 更小更快，效果略逊 | 资源受限时 |
| **稀疏向量（SPLADE）** | 语义+关键词兼顾，能替代 BM25 那一路 | 想简化架构时 |

> ⚠️ **一个必须记住的工程约束**：**换了 embedding 后端，索引必须重建。**
> 向量空间不同了，旧向量没法查询 —— **而且它不报错，只是"莫名其妙查不准"**。
> 所以索引文件里记录了后端名和维度，载入时校验，不匹配直接报错（把静默错误变成显式错误）。

**怎么验证**：`scripts/smoke_test.py` 会打印当前用的是哪个后端；`GET /rag/stats` 也会返回 `embedder_ready` 字段（`local` 时为 false）。

---

### 8. 检索：numpy 内存索引 + BM25 + RRF

这是项目里"技术含量"最集中的部分。

**怎么实现的**（`app/rag/store.py`）：

```
查询 → ┬→ 向量检索：L2 归一化后 矩阵乘法 得余弦相似度 → 排前 20
       └→ BM25 检索：jieba 分词 → 算 BM25 分数     → 排前 20
                    ↓
              RRF 融合（k=60）→ 取前 top_k
```

**三个设计点**：

1. **为什么只做矩阵乘法**：向量 L2 归一化之后，**点积就等于余弦相似度**。于是"算 50 个块的相似度"退化成一次 `矩阵 @ 向量`，几毫秒算完。
2. **为什么要混合检索**：纯向量**对精确词不敏感**——它擅长"按意思找"，但分不清 `OOMKilled` 和 CPU 高。而运维场景全是精确词（`Exit Code 137`、`EADDRINUSE`）。所以**向量管语义 + BM25 管字面**，互补。
3. **为什么用 RRF 而不是把分数加权平均**：两路**分数量纲完全不同**（余弦在 -1~1，BM25 是 0~∞ 的无界值），硬加权等于拿两把不同刻度的尺子量同一件事。**RRF 只看排名不看分数**，天生不受量纲影响。

**实测效果**（Top-3 召回率，32 个问题）：

| 模式 | 总体 | 词面型 | 语义型 |
|---|---|---|---|
| 纯向量 | 93.8% | 96.2% | 83.3% |
| 纯 BM25 | 96.9% | 100% | 83.3% |
| **混合检索** | **100%** | **100%** | **100%** |

**混合检索比它两个单项都高** —— 说明两路漏掉的是**不同的**问题，融合补上了各自的盲区。这就是 RRF 价值的实证。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **FAISS** | 专业向量检索库，支持十亿级向量，**但只管向量，没有关键词检索** | 向量规模到百万级 |
| **Milvus / Qdrant / Weaviate** | 完整向量数据库：分布式、持久化、增删改、权限。**但部署成本高（要单独起服务）** | 多实例共享索引、要在线增删时 |
| **pgvector（PostgreSQL）** | 向量直接存在关系库里，**和业务数据在同一个事务里**，不用维护两套存储 | 已有 Postgres、且向量规模中等 |
| **Elasticsearch** | 天然支持 BM25 + 向量混合检索，**它的 RRF 是内置的** | 已经有 ES 集群时（最省事） |
| Redis / 内存（本项目） | 最简单，**重启即丢，不支持在线更新** | 原型阶段、语料几万块以内 |
| BM25 实现：`rank_bm25` 库 | 现成的，少写 100 行。**本项手写是为了讲得清**（公式就是几行） | 生产上直接用库更稳妥 |

> **什么时候该换**：语料从 50 块涨到几十万块、或者需要在线上不停机增删文档时。**现在换是过度设计**——
> 我算过规模：50 个块用不上 Milvus，检索只要几毫秒，等规模上来我会换 Qdrant，
> 这比"我用了 Milvus"更站得住脚。

**怎么验证**：

```bash
.venv\Scripts\python.exe -m app.rag.pipeline eval --top-k 3    # 三种模式召回率对比
.venv\Scripts\python.exe -m app.rag.pipeline ask "nginx 报 502 怎么排查"
```

---

### 9. python-dotenv —— 密钥管理

**怎么实现的**：`.env` 放密钥，`.gitignore` 排除它，代码里 `load_dotenv()` 读进来，永远只取环境变量、不硬编码。

**为什么这是安全底线**：**密钥一旦提交到 Git，就等于公开了**（GitHub 上有大量爬虫专扫 API Key）。而且删掉那次提交也没用——历史记录还在。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **`pydantic-settings`** | 类型化的配置管理：缺了必填项、类型写错，**启动时直接报错**，而不是运行到一半才发现 | 配置项变多时（推荐升级） |
| 系统环境变量 | 不落盘，最安全，但**每台机器都要单独配**，不方便协作 | 服务器部署时（配合 CI/CD） |
| Docker secrets / 密钥管理服务 | 生产标准做法（AWS Secrets Manager、Vault） | 生产环境 |
| 直接写在代码里 | —— | **永远不要** |

**怎么验证**：

```bash
git status --short            # .env 不应出现在里面
git check-ignore -v .env      # 应输出「.gitignore:行号:.env」
```

---

### 10. jieba —— 中文分词

**怎么实现的**：BM25 需要"词"而不是"字"，中文没有空格，所以要先分词。

**一个细节**：`jieba` **不是核心依赖**——代码里做了判空，没装就退化成字符 bigram。**缺一个非核心包不该让整条链路跑不起来**，这是可选的依赖该有的处理方式。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **字符 bigram（本项目兜底）** | 零依赖，中文效果居然不太差，但**召回粒度粗、噪音多** | 不想装任何包时 |
| `HanLP` / `LAC`（百度） | 准确率更高，**但要下模型、依赖重** | 分词的准确性成为瓶颈时 |
| `pkuseg` | 支持领域词典（比如加进"运维术语表"） | 有大量专有名词时 |
| 简繁/白空格语言 | 英文等有空格的语言不需要分词，`split()` 就够 | 语料变成英文时 |

> **一个可讲的经验**：分词粒度会直接影响 BM25 的效果。所以专门做了「自定义词典」这个扩展点——
> 把 `OOMKilled`、`CrashLoopBackOff` 这类词加进去，能让它们不被切碎。

**怎么验证**：`scripts/smoke_test.py` 第 3 层的 `bm25` 那一行就是它的效果。

---

### 11. Git —— 版本控制

**怎么实现的**：9 次提交，每完成一个模块一个或几个 commit，**commit message 写清"做了什么 + 为什么"**。

**为什么 commit message 值得认真写**：点开仓库，**第一眼看的就是提交历史**。一条 `fix` 和一条 `fix: requirements.txt 改为纯 ASCII，修复中文 Windows 下 pip 编码错误` —— 后者证明你会排查、会记录。

**一个踩过的坑**：全局邮箱曾经写成 `163,com`（逗号），**GitHub 靠邮箱把提交关联到账号**，不改的话推上去的提交显示不出作者，别人点开仓库看到一片灰色默认头像。

**替代方案**：SVN（集中式，现在少用）、Mercurial（Git 的同期对手，生态已落后）。**Git 在这个领域没有真正的替代品。**

---

### 12. LangGraph —— Agent 编排

**怎么实现的**（`app/agents/graph.py`）：把 ReAct 循环画成状态图。

```python
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]      # reducer：追加并去重
    steps:    Annotated[list, operator.add]      # 轨迹拼接
    usage:    Annotated[dict, _merge_usage]      # ★ token 累加
    stop_reason: str                             # 不写 Annotated → 覆盖

graph.add_edge("tools", "agent")                 # 这条回头边就是「循环」
graph.add_conditional_edges("agent", route, {...})  # 这就是那个 if
```

**`Annotated[..., reducer]` 是它的核心概念**：节点返回的字典怎么合并进状态，由 reducer 决定。
不写 reducer 就是"后写的覆盖前面的"。

**★ 同一个循环项目里有两份实现**，这是刻意的：

| | 手写版 `react.py` | LangGraph 版 `graph.py` |
|---|---|---|
| 循环 | `for` + `break` | `add_edge("tools","agent")` |
| 条件 | `if not tool_calls:` | `add_conditional_edges` |
| 状态 | 局部变量 | `AgentState` + reducer，可落盘 |
| 可视化 | 自己画 | `draw_mermaid()` 白送 |
| 检查点 | 要自己做 | 内建 |
| 依赖 | 0 | +7 个包 |

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **纯手写循环**（本项目也有） | 零依赖、原理透明、报错栈干净。**但没有检查点** —— 做"人工确认"要自己实现状态落盘 | 需求简单、要极致可控时 |
| **PydanticAI** | 类型安全做得最好，写法更 Pythonic，**但生态和示例比 LangGraph 少** | 团队重类型、喜欢 Pydantic 风格 |
| **AutoGen**（微软） | 面向"多 Agent 对话"设计，Agent 之间互相聊天。**对"工具编排"的抽象不如状态图清晰** | 任务本质是多角色对话时 |
| **CrewAI** | 上手最快，"角色 + 任务" 的写法很直观。**但定制性弱** —— 复杂的循环/回退逻辑不好表达 | 快速做 demo |
| **OpenAI Agents SDK** | 官方出品，轻量，**但和 OpenAI 生态绑定较紧** | 主要用 OpenAI 模型时 |
| **自建状态机**（不用框架） | 完全可控，但要自己实现检查点、恢复、可视化 —— **这几样才是框架真正的价值** | 有特殊约束不能引框架 |

**为什么选 LangGraph**：它的心智模型（状态图 + reducer + 检查点）最贴近
Agent 生产化的三个真实需求 —— **可中断（人工确认）、可恢复（崩了续跑）、
可观测（按节点看进度）**。而这三点恰好是本项目后续要实现的核心能力。

**代价**：

| 代价 | 具体表现 |
|---|---|
| 消息格式要转换 | 原生 API 的 `arguments` 是**字符串**，LangChain 的 `args` 是 **dict**。少写一个字段不报错，只在某次工具调用时莫名失败 |
| 抽象层让排查变难 | 报错栈里全是框架内部帧 |
| 依赖变重 | 1 个包 → 实际多装 7 个（`langchain-core`、`langgraph-checkpoint`、`langsmith`、`orjson`、`zstandard` …） |
| 版本变化快 | 0.x → 1.x 期间 API 改过好几轮 |

**怎么验证**：

```bash
# 看状态图（不花钱，几秒）
.venv\Scripts\python.exe -m app.agents.graph --graph

# 跑一轮
.venv\Scripts\python.exe -m app.agents.graph "cache-01 的 Redis 容器一直重启"

# 和手写版对比（约 ¥0.1）
.venv\Scripts\python.exe -m app.agents.compare
```

> 完整的对比数据、两个真实踩过的坑，见 [`react-langgraph.md`](react-langgraph.md)。

---

---

### 13. MCP SDK —— 工具的标准协议出口

**怎么实现的**（`app/mcp_server/server.py`）：用官方 SDK 把 7 个工具注册成 MCP tools。

```python
from mcp.server.mcpserver import MCPServer          # 2.x 改名了（v1 是 FastMCP）
from mcp.types import ToolAnnotations

server = MCPServer(name="agentdesk-ops", version="0.1.0", instructions="...")

@server.tool(name="check_disk", annotations=READ_ONLY,   # 协议级风险提示
             description=TOOLS["check_disk"]["desc"])     # 描述复用工具层，不重写
def check_disk(
    host: Annotated[str, Field(description="主机名，如 web-01")] = "web-01",
) -> dict:
    return _call("check_disk", host=host)                     # 转发，不重新实现
```

**用了三种原语里的两种**：

| 原语 | 本项目 | 判断标准 |
|---|---|---|
| tools | 7 个运维工具 | "做一件事" |
| resources | 工具清单 + 审计记录 | "读一份数据" |
| prompts | 未使用 | "固化一段话术" |

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **`fastmcp`（PrefectHQ，独立包）** | 功能更多（内置 auth、proxy、client、中间件），**但和官方 SDK 是两套代码**，版本各自演进 | 需要它独有的 auth/代理能力时 |
| **自定义 JSON Schema + HTTP 接口** | 完全可控，**但只有自家客户端能用** —— 生态价值归零 | 内部系统、不打算对外开放能力 |
| **OpenAI function calling 清单** | 是模型侧约定，**不是工具侧协议**，每个客户端仍要各接一遍 | —— |
| **不用协议，直接给代码** | 最省事，**但对方要跑 Python、要装依赖** | 只有自己用 |

**为什么选 MCP**：这是目前唯一被多家客户端（Cursor / Claude Desktop /
各类 Agent 平台）共同支持的**工具侧**标准。做完之后，适配成本从 M×N 降到 M+N。

**代价**：

| 代价 | 具体表现 |
|---|---|
| **同一份工具出现两份 schema** | 工具层手写的（给 OpenAI 格式用）+ MCP 从类型注解生成的。**重复一定会漂移，而且漂移了不报错** |
| **版本变化快** | v1→v2：`FastMCP`→`MCPServer`、`inputSchema`→`input_schema`、模块路径也变了 |
| **参数描述只能靠 `Field`** | docstring 的 `Args:` 段不会被解析，忘了写就是空描述 |
| **错误类型有讲究** | 抛 `ToolError` 消息才能传到客户端；抛别的异常只给通用文案，**原因被吃掉** |
| **stdio 模式下 stdout 是协议通道** | 任何调试打印都会破坏协议，且报错完全看不出原因 |
| 依赖变重 | 又带进 `sse-starlette`、`httpx-sse`、`jsonschema` 等一批 |

**怎么验证**：

```bash
# schema 一致性（不启动 server，秒回）
.venv\Scripts\python.exe -m app.mcp_server.server --check

# 协议层自检：官方客户端连自己启动的 server（9 项检查）
.venv\Scripts\python.exe scripts\mcp_check.py

# 或进总自检的第 5 层
.venv\Scripts\python.exe scripts\smoke_test.py --full
```

> 完整的原理、四个坑、客户端配置与排障顺序，见 [`mcp-server.md`](mcp-server.md)。

---

---

### 14. 多 Agent 编排 —— 在循环外面套一层调度

**怎么实现的**（`app/agents/supervisor.py` + `specialists.py`）：
LangGraph 状态图，7 个节点、5 条回边。**Supervisor 只做决策，不干活。**

```python
class MultiAgentState(TypedDict):
    intent: dict          # 意图路由产出
    knowledge: dict       # 知识库检索结果
    diagnosis: dict       # 诊断结论 + 证据
    verdict: dict         # 校验结论
    visited / retries / node_log     # 编排层才需要的三样
    # 注意：没有 messages —— 每个 Agent 的对话历史各自独立
    #       这正是"上下文隔离"的实现方式
```

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **单 Agent（本项目也有）** | 一条对话历史到底，2-3 次模型调用。**但没有"独立校验岗"，也不能单独评测某个部件** | 任务简单、不需要隔离上下文时 |
| **LangGraph 官方 Supervisor 库** | `langgraph-supervisor` 封装好了，**但它的 Supervisor 是让模型决定派给谁** | 步骤确实需要动态规划时 |
| **LLM 决策的 Supervisor** | 灵活，能处理没预想到的路径。**但出错时你不知道它为什么这么选，也不容易测** | 并行派发 / 任务递归分解 / 步骤取决于运行时发现 |
| **AutoGen / CrewAI** | 面向"多 Agent 对话"或"角色+任务"，**上手更快，但对"循环+重试"的抽象弱** | 快速做 demo、任务本质是多角色对话 |
| **规则决策的 Supervisor（本项目）** | 可预测、可复现、零成本、易测。**代价是只能处理预期内的流程** | ← 当前选择 |
| **把校验做成独立服务** | 可以跨项目复用、单独部署 | 有多个 Agent 系统要共用一套校验时 |

**为什么这一步选"规则决策"**：这套流程的步骤是确定的
（意图 → 知识 → 诊断 → 校验 → 汇总），变化只有"要不要查知识库""查现场还是纯推理"，
而这两件事由意图标签驱动。

> **用模型去决定一个已经确定的事情，只会引入不确定性，不会带来能力。**

**代价**：

| 代价 | 具体表现 |
|---|---|
| **多几次模型调用** | 完整流程至少 4 次，实测 5 次左右（单 Agent 是 2-3 次） |
| **链路变长，排障变难** | 出错时不知道是哪个 Agent 的锅 → 所以必须有 `node_log` |
| **Supervisor 的决策逻辑本身是新 bug 源** | 本次踩到两次：悬空 tool_calls 导致 400、"重试"退化成"重来一遍" |
| **子 Agent 没有跨轮次记忆** | 重试时必须显式把"已查过的数据"传过去，否则它会重复查同样的东西 |

**★ 校验 Agent 的设计是最值钱的部分**：

```
规则查（确定、可复现、零成本、无幻觉）：
    数值溯源 / 越权声明 / 有无证据下结论 / 结论是否为空
模型查（只能它查）：
    因果链是否成立 / 是否把"相关"当"因果" / 置信度是否合理

顺序：规则先跑，能判否就直接判否，一次模型调用都不花。
```

**怎么验证**：

```bash
# 多 Agent 编排图（不花钱）
.venv\Scripts\python.exe -m app.agents.supervisor --graph

# 跑一次完整流程
.venv\Scripts\python.exe -m app.agents.supervisor "web-01 上的网站很慢"

# 意图路由 Agent 单独评测（20 条，约 ¥0.02）
.venv\Scripts\python.exe scripts\eval_specialists.py

# 校验器的正反例（在总自检的第 4 层，离线免费）
.venv\Scripts\python.exe scripts\smoke_test.py
```

> 完整的拆分理由、校验规则的三轮误报收窄、五个真实坑，
> 见 [`multi-agent.md`](multi-agent.md)。

---

### 15. 沙箱执行 + 人工确认 —— 让 Agent 敢碰生产系统

**怎么实现的**（`app/sandbox/`）：三个文件各管一件事。

```
policy.py     准入规则：白名单 + 参数级校验 + 三维决策（只判断，不执行）
executor.py   执行：容器通道 / 主机通道 / 仿真通道（fail-closed）
approvals.py  审批单：生命周期、指纹绑定、单次消费、追加日志
```

**三个决策（不是两个）：**

| 决策 | 含义 | 例子 |
|---|---|---|
| `allow` | 只读诊断，直接执行 | `df -h`、`tail -n 20 …` |
| `needs_approval` | 可逆写，必须人工确认 | `systemctl restart nginx` |
| `deny` | 高危，谁确认都不做 | `rm -rf`、`dd`、`chmod` |

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **`subprocess` 直接执行** | 无隔离。**等于是把机器交出去** | 绝不该用于"模型生成的命令" |
| **只做沙箱、不做审批** | 隔离层被绕过时你都不知道；且沙箱不回答"该不该做" | —— |
| **只做审批、不做沙箱** | 人点头后命令在无限制环境跑；审批不回答"最坏能坏到哪" | —— |
| **OS 级沙箱（gVisor / Firecracker）** | 比容器更强的隔离（内核级），但部署复杂得多 | 要跑不受信任的代码时 |
| **SSH + forced command + sudo 白名单** | 主机通道命令（systemctl 类）的"生产级"做法 | 本机演示后，接真实目标机时 |

**为什么写操作走容器、只读命令走主机**：

> 判据不是"读还是写"，是"隔离值不值得付它的成本"。
> `du` 改不了任何东西，挂载方案反而会限制它能看的范围 → 主机通道。
> `truncate` 会改文件，只挂 `/var/log` 就够用且刚好封住范围 → 容器通道。
> **隔离不是越多越好，是"刚刚够用"最好。**

**代价**：

| 代价 | 具体表现 |
|---|---|
| 白名单必须持续维护 | 加了新排查需求（`du -d1`）就要放宽规则，而**放宽就是新一轮的误判风险** |
| 审批是"人肉瓶颈" | 写操作都得等人批，自动化被卡在人的响应时间上 |
| 两条通道的心智负担 | 同一命令库，有的真隔离、有的没有，结果里的 `isolated` 字段必须如实暴露 |
| mock 数据要自洽 | mock 数据矛盾时，Agent 会基于矛盾做出"拒绝动作"这类正确但意外的行为 |

**怎么验证**：

```bash
# 沙箱状态 + 白名单（不花钱）
.venv\Scripts\python.exe -m app.sandbox.server 2>/dev/null || \
curl http://127.0.0.1:8000/sandbox

# 九层自检里的第 5 层（沙箱 5 项 + 审批 3 项，离线免费）
.venv\Scripts\python.exe scripts\smoke_test.py
```

> 完整的三层职责、五个坑、设计问答，见 [`sandbox-hitl.md`](sandbox-hitl.md)。

---

---

### 15. 自研评测判定层 —— 4 项规则 + LLM-as-Judge

**怎么实现的**（`app/evaluation/judges.py` + `scripts/run_eval.py`）：

```
规则判定（确定、可复现、零成本、不会幻觉）
    检索命中   check_retrieval    标准文档有没有进 top-k
    引用可信   check_citations    [n] 编号有没有越界
    数值忠实   check_numbers      答案里的数字在不在资料里
    拒答正确   check_abstention   库外该拒、库内不该拒
模型判定（只能它判）
    相关性 / 忠实度   judge_answer   1-5 分
```

**顺序：规则先跑。** 能判否的直接判否，一次模型调用都不花。

**自校验**：`verify_judge()` 用三个构造样本（好答案 / 跑题答案 / 编造答案）
先验判定器本身，三个都要判对才继续跑正式评测。

**替代方案的差异**：

| 替代 | 差异 | 什么时候换成它 |
|---|---|---|
| **Ragas** | 有成熟指标（faithfulness / answer relevancy / context precision），**但指标是黑盒**，判定提示词和阈值不受你控制，也无法按自己的口径调整 | 需要和业界数字对齐、或指标需求超出自己能写的范围 |
| **只用 LLM-as-Judge** | 写起来最快，**但每一次判定都要花钱，而且不确定** —— "引用编号越界"这种问题机器一眼可判，问模型是浪费 | 规则完全写不出来的场景 |
| **只用规则** | 零成本零波动，**但判不了"这段话说得对不对"** | ← 所以本项目的做法是两者结合 |
| **人工标注** | 最准，**但 40 条 × N 轮迭代的成本不可持续** | 定基线、校准自动化判定器时 |
| **自研（本项目）** | 判定逻辑完全可控、可解释、零额外依赖 | ← 当前选择 |

**为什么自研**：`requirements.txt` 里没有加任何评测依赖。
判定层的每条规则和每个提示词都是可读、可改、可测的，
**这本身就是"可解释"的一部分** —— 每个分数是怎么来的都能逐条说清。

**代价**：

| 代价 | 具体表现 |
|---|---|
| **判定器自己会误报** | 第一轮 40 条里 7 条"拒答"是误报（把回答末尾的边界声明当拒答）；3 条"数字无出处"是误报（context 漏了 Prompt 里的标题） |
| **模型判分有随机性** | `temperature=0` 下仍可能微抖；样本小时结论不稳（语义型只有 5 条，1 条之差就是 20 个百分点） |
| **没有业界可比数** | Ragas 的分数能和公开结果对照，自研的不行 —— 好处是口径完全贴合自己的场景 |
| **评测集标注本身可能错** | 之前就吃过：第一轮 5 个失败里有一半是我自己标错了标签 |

**怎么验证**：

```bash
# 只验尺子（3 次调用）
.venv\Scripts\python.exe -c "from app.evaluation.judges import verify_judge; verify_judge()"

# 小样本试跑（6 条，约 ¥0.08）
.venv\Scripts\python.exe scripts\run_eval.py --limit 6

# 完整评测（40 条 × 2 模式，约 ¥0.234，5 分钟）
.venv\Scripts\python.exe scripts\run_eval.py

# 断点续跑
.venv\Scripts\python.exe scripts\run_eval.py --resume

# 或进总自检的第 7 层（离线部分不花钱）
.venv\Scripts\python.exe scripts\smoke_test.py --full
```

> 完整的六维度设计、判定器自证、第一轮误报排查过程、7 条已知局限，
> 见 [`evaluation.md`](evaluation.md)。

---

## 第 2 章｜总表：什么时候该换

把上面所有"升级触发器"收成一张表。**这张表把"技术选型"的取舍总结成了一条主线**。

| 技术 | 现在够用的理由 | 什么信号出现时该换 | 换成什么 |
|---|---|---|---|
| Python + venv | 直接依赖只有 9 个 | 装包开始变慢、要锁版本 | `uv` 或 `poetry` |
| httpx 手写 | 要看清协议细节 | 要接 5 家以上模型、要统一重试与限流 | `openai` SDK + 网关（LiteLLM） |
| DeepSeek | 便宜到能随便试错 | 要处理敏感数据 / 要私有化 | 本地 Ollama / vLLM |
| FastAPI 单进程 | 本机演示够用 | 上线、要抗并发 | gunicorn + uvicorn worker |
| 内存向量索引 | 50 块，检索几毫秒 | 语料到几十万块 / 要在线增删 | Qdrant 或 pgvector |
| local 哈希向量 | 零 Key 也能跑通 | **要真实语义效果**（现在就该换） | 百炼 `text-embedding-v3` 或本地 BGE-M3 |
| BM25 手写 | 公式只有几行，好讲 | 上生产 | `rank_bm25` 库或 Elasticsearch |
| **LangGraph 内存检查点** | 单进程演示够用 | 要跨进程恢复 / 要多人协作查看历史 | 检查点换 Postgres / Redis 存储 |
| **手写 ReAct** | 原理透明，讲得清 | 复杂分支、要中断续跑 | 全面切 LangGraph（已在用） |
| MCP stdio 传输 | 本地单客户端够用 | 要给远程 / 多客户端共享 | streamable-http + 挂鉴权 |
| MCP 返回 `-> dict` | JSON 文本是所有客户端都能解析的最大公约数 | 客户端程序需要结构化字段 | 返回类型改 TypedDict，开 `structured_output` |
| 沙箱 mock 后端 | 任何机器能跑通演示、无副作用 | 要真隔离、要执行真实写操作 | 装 Docker，设 `SANDBOX_BACKEND=docker` |
| 审批 JSONL 追加日志 | 单机、状态可重建、天然可审计 | 多实例并发 / 要查询统计 | 换 SQLite 或 Postgres，状态机不变 |
| **Supervisor 规则决策** | 流程步骤是确定的，规则可预测可测 | 需要并行派发 / 任务递归分解 / 步骤取决于运行时发现 | 换成 LLM 决策的 Supervisor |
| **自研判定层** | 判定逻辑可控可解释、规则部分零成本零波动 | 需要与业界数字对齐 / 指标超出自己能写的范围 | Ragas |
| **规则 + 模型混合判定** | 能用规则的一律用规则，省钱且确定 | （不存在换成纯模型的理由）—— 纯模型判定每次都花钱且不确定 |
| **单 Agent 循环** | 任务简单时够用，调用次数少 | 需要独立校验岗 / 需要单独评测某部件 / 上下文互相干扰 | 本项目的多 Agent 编排（已实现） |
| **工具 mock 后端** | 任何机器都能复现 | 要查真实主机 | 切 `local`（本机）或 `ssh`（远程）；处置另需 Docker 沙箱 |
| `.env` + dotenv | 单人开发 | 配置项超过 10 个 | `pydantic-settings` |
| 无缓存 | 调用量小 | 调用量上来、开始心疼钱 | Redis 缓存 + 上下文缓存 |

---

## 第 3 章｜后续会引入的技术栈

提前知道"下一步要加什么、以及它们的替代品"，就能讲清"技术演进路线"而不只是"我现在有什么"。

| 要做的 | 主流做法 | 手写替代（本项目风格） | 区别 |
|---|---|---|---|
| 可观测 | **Langfuse**（自托管） | 自己写 JSONL 日志（**本项目已有审计**） | Langfuse 有 Trace 树、Token 成本面板、Prompt 版本管理；自己写只能查文本 |
| 评测 | **Ragas**（LLM-as-Judge） | 自己写召回率 + 双引擎对比（**本项目已有**） | Ragas 有忠实度、答案相关性等成熟指标；自写指标简单但可解释 |
| 监控告警 | **Prometheus + Grafana** | 打印日志 | 有 Zabbix/Grafana 基础的话，这块上手最快 |
| 部署 | **Docker Compose + Nginx** | 直接跑 uvicorn | Nginx 负责 HTTPS、超时、缓冲控制（SSE 那个坑就出在这里） |

> **注意一个顺序问题**：**先手写，再上框架。** 反了的话只会用框架；"LangGraph 的状态图底层怎么跑的"这类问题就答不上来。
> 本项目已经做到了这一点：手写 ReAct 和 LangGraph 版**同时存在**，可以直接对比。

---

## 第 4 章｜怎么使用

### 4.1 起服务（最常用）

```bash
cd "E:\Workbuddy\ai agent\agentdesk"
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

`--reload` 是开发模式：**改代码存盘后服务自动重启**，不用手动重启。上线要去掉。

### 4.2 交互式文档（上手最快）

浏览器打开 **http://127.0.0.1:8000/docs**

这是 FastAPI 白送的页面：每个接口都列出了参数、可以在页面上直接填、点 Execute 就发请求。

**它同时是演示资产**——演示时打开这个页面，比打开代码更有说服力。

### 4.3 curl 命令行

```bash
# 先生成 payload 文件 —— 中文千万别在命令行里直传
.venv\Scripts\python.exe -c "import json,pathlib;pathlib.Path('_q.json').write_text(json.dumps({'question':'nginx 报 502 怎么排查'},ensure_ascii=False),encoding='utf-8')"

curl -s -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" --data-binary @_q.json
```

> ⚠️ **中文 payload 的坑**：Git Bash 下直接写 `-d '{"question":"中文"}'` 会因为编码问题报
> `There was an error parsing the body`。**正确做法是先用 Python 写成 UTF-8 文件，再 `--data-binary @文件`。**

### 4.4 命令行工具（不启动服务也能用）

RAG 模块可以直接当 CLI 跑：

```bash
.venv\Scripts\python.exe -m app.rag.pipeline build          # 构建索引
.venv\Scripts\python.exe -m app.rag.pipeline eval --top-k 3 # 跑召回率评测
.venv\Scripts\python.exe -m app.rag.pipeline ask "nginx 报 502 怎么排查"
```

### 4.5 一键自检（新增，最省事）

```bash
.venv\Scripts\python.exe scripts\smoke_test.py
```

### 4.5 一键自检（新增，最省事）

```bash
.venv\Scripts\python.exe scripts\smoke_test.py          # 快速，不花钱
.venv\Scripts\python.exe scripts\smoke_test.py --full   # 加一次真实 Agent 调用
```

一条命令跑完九层：**运行环境 → 模型连通 → 检索与问答 → Agent（工具+编排）→ 沙箱与人工确认 → 可观测 → 评测 → MCP Server → HTTP 24 个路由**，
最后给出汇总表和失败项的修复提示。**退出码 0 = 已检查项全通**，可以接进自动化。

`--full` 才会跑真实 Agent 调用（约 7k token）—— **默认跳过花钱项，这样你可以随手跑。**

---

## 第 5 章｜怎么测试：分九层

**核心原则：从下往上测。** 下面的层断了，上面的失败都是连带后果——先修下面那个。

| 层 | 测什么 | 怎么测 | 通过标准 |
|---|---|---|---|
| **1 环境** | Python 版本、9 个直接依赖、`.env` 密钥 | `smoke_test.py` 第 1 层 | 4 项全 ✅ |
| **2 模型** | Key 有效、网络通、能拿到回答 | `smoke_test.py` 第 2 层；或 `check_env.py` | 拿到回答 + 打印 token 用量 |
| **3 检索** | 索引能载入、三种模式都能召回、问答带引用 | `smoke_test.py` 第 3 层；或 `pipeline eval` | 召回率表跑出来；引用里有来源文件名 |
| **4 Agent** | 工具注册表能载入、能执行、**能拒绝非法参数**、状态图能编译 | `smoke_test.py` 第 4 层 | 6 项全 ✅ |
| **5 沙箱** | 白名单规则自检（每条 example 命中自己）、攻击面全拒、fail-closed、mock 返回可信数据 | `smoke_test.py` 第 5 层 | 5 项全 ✅ |
| **审批** | 状态机：批准必填 by / 已消费不可重放 / 指纹不符拒绝 | `smoke_test.py` 第 5 层 | 3 项全 ✅ |
| **6 观测** | trace/span 记录、父子关系、嵌套 usage 归并、成本核算、导出 payload | `smoke_test.py` 第 6 层 | 9 项全 ✅ |
| **7 评测** | 评测集标注完整性、4 项规则判定的正反例、判定器自校验（`--full`） | `smoke_test.py` 第 7 层 | 5 项全 ✅（含回归用例） |
| **8 MCP** | schema 无漂移、协议层握手/调用/资源读取、错误路径 | `smoke_test.py` 第 8 层；或 `mcp_check.py` | 2 项全 ✅（协议层 9/9） |
| **9 服务** | 第 9 层实际探测的 12 个接口全通、参数校验生效、告警分级正确 | `smoke_test.py` 第 9 层；或 `/docs` 逐点点 | 12 项接口全 ✅ |

**几个"故意制造错误"的测试**（比"能跑通"更能证明你理解系统）：

| 想验证什么 | 怎么做 | 期望结果 |
|---|---|---|
| 参数校验真的生效 | `top_k` 传 999 | 返回 **422** 并说明字段哪里错 |
| 密钥错时的行为 | `.env` 里填个假 Key | 返回 **502**（上游故障，可重试），不是 500 |
| 高危操作会被拦 | 推 `DiskSpaceCritical` 告警 | `decision: need_human`，**不执行任何操作** |
| 流式真的是流式 | curl 加 `-N` 看输出 | 字逐块到达，不是一次性出现 |
| 索引后端不匹配 | 载入用另一个后端建的索引 | **显式报错**，而不是查出乱七八糟的结果 |
| **防命令注入真的生效** | `execute_tool("check_disk", {"host": "web-01; rm -rf /"})` | 返回「主机名不合法」，**被拒绝** |
| **Agent 不会无限循环** | `--max-steps 1` 跑一个复杂问题 | `stop_reason: max_steps`，且给出了收口答案 |
| **Agent 不会硬找问题** | 问一个一切正常的主机（Q3 负例） | 如实回答"没发现问题"，而不是编一个隐患 |
| **该拒答时拒答** | 问一个知识库外的问题（如「Kafka lag 怎么排查」） | 明确说「知识库中没有相关内容」，**不是硬答** |
| **判定器会判错吗** | 跑 `verify_judge()`（3 个构造样本，含编造答案） | 3/3 通过 —— **尺子不准则所有分数无意义** |
| **该拒答时拒答** | 问一个知识库外的问题（如「Kafka lag 怎么排查」） | 明确说「知识库中没有相关内容」，**不是硬答** |
| **判定器会判错吗** | 跑 `verify_judge()`（3 个构造样本，含编造答案） | 3/3 通过 —— **尺子不准则所有分数无意义** |
| **MCP 错误原因能传到客户端** | 通过 MCP 调 `check_disk` 传非法主机名 | `isError=true` **且消息里带"主机名不合法"**，不是只有通用文案 |
| **schema 漂移会被抓** | 临时给 `ops.py` 加一个 MCP 侧没有的参数 | 一致性校验报 `MCP 缺少参数 [...]`，退出码 1 |

---

## 第 6 章｜技术选型的设计取舍

技术选型的说明逻辑，**四步**：

> **① 我的场景是什么 → ② 我选了什么、为什么它匹配这个场景 → ③ 它的代价是什么 → ④ 什么信号出现时我会换**

**为什么用 FastAPI 而不是 Flask**

> "我的场景需要 SSE 流式和异步，Flask 是原生同步的，做流式要么开线程要么用 gevent，都在绕路。
> FastAPI 原生 async，而且靠 pydantic 直接把参数校验和文档生成都省了。
> 代价是它比 Flask 重一点、概念多（async、依赖注入）。
> 如果我只是写几个内部小接口、不需要并发，Flask 更省事。"

**为什么不用 Milvus**

> "因为我算过规模——语料 6 篇、切成 50 块，检索是一次矩阵乘法，几毫秒就完了。
> Milvus 解决的是千万级向量的近似最近邻，为这个规模引入它，等于多一个要部署、要监控、会挂的服务。
> 我的切换信号很明确：语料上到几十万块、或者需要不停机增删文档，那时候我会换 Qdrant 或者直接用 pgvector，把向量和业务数据放在同一个事务里。"

**为什么用混合检索**

> "因为纯向量对精确词不敏感。运维场景里全是 `Exit Code 137`、`OOMKilled` 这种词，
> 一旦被模型'理解成大概意思'就找不准了。所以我用向量管语义、BM25 管字面，再用 RRF 融合。
> 融合没有用加权平均，因为两路的分数量纲完全不同，硬加权等于拿两把不同刻度的尺子量一件事。
> 实测混合检索的 Top-3 召回率是 100%，比纯向量的 93.8% 和纯 BM25 的 96.9% 都高——
> **因为它两个都高，说明两路漏掉的是不同的题目**。"

**为什么用 LangGraph 而不是自己写**

> "我两个都写了 —— 项目里手写 ReAct 和 LangGraph 版同时存在，因为我想搞清楚框架
> 到底替我做了什么。结论是：手写版就是 `for` 循环加 `break`，主干 30 行；
> LangGraph 里 `add_edge("tools","agent")` 那条回头边就是循环。
>
> 它真正多给我三样：**检查点**（能中断续跑 —— 这是我后面做人工确认功能的技术前提，
> 手写要实现得自己搞状态落盘）、结构可视化（`draw_mermaid()` 直接出架构图）、
> 按节点流式输出进度。
>
> 代价我也清楚：**消息格式要转换**，原生 API 的 `arguments` 是字符串、
> LangChain 的 `args` 是 dict，少写一个字段不报错、只在某次工具调用时莫名失败；
> 还有依赖从 1 个包变成 8 个。"

**这四段的共同点**：**每段都有数字或具体的技术约束**，不是"我觉得它更好"。

> 一句话原则：**说不出代价的技术选型，本质上就是"跟着教程抄的"。**
