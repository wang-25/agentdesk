# ReAct 与 LangGraph 双版本实现

> **这一步做了什么**：给项目补上「**能执行**」那条腿。
>
> 在此之前 Agent 只会"说"——RAG 让它"知道"，但输出仍然只是文字。
> 有了这一层，它会自己决定：**要查什么、按什么顺序查、什么时候信息够了停手。**
>
> 更新日期：2026-09-26 ｜ 对应进度：Agent 执行层完成

---

## 第 0 章｜一句话说清 ReAct

**ReAct = Reasoning（推理）+ Acting（行动）。**

```
普通问答：  question ─────────────────────────→ answer
                   （一问一答，中间没有动作）

ReAct：     question → 想 → 做 → 看 → 想 → 做 → 看 → … → answer
                        ↑__________________________|
                          中间那些「做」和「看」，
                          就是 Agent 和聊天机器人的分水岭
```

举个具体的例子。用户说：

> web-01 上的网站访问很慢，有时报 502，帮我看下原因

Agent 实际做的事：

| 轮 | 它在想什么 | 它做了什么 | 它看到了什么 |
|---|---|---|---|
| 1 | 先看主机整体状态 | `check_disk` `check_load` `check_service` `list_containers` | 磁盘 96%、负载每核 4.21、nginx 正常、容器都在 |
| 2 | 磁盘满了，那日志里有什么？顺便查查经验 | `tail_log` `search_knowledge` | `no space left on device`、`upstream timed out` |
| 3 | 信息够了，出结论 | （不再调工具） | —— |

**这三步没有一步是预先写死的。** 「第 1 轮该调哪几个工具」是模型自己判断的。
这就是 Agent 和「按固定流程跑的脚本」的区别。

---

## 第 1 章｜手写版：核心其实只有 30 行

`app/agents/react.py`。剥掉注释和防护代码，主干长这样：

```python
messages = [system_prompt, question]

for step_no in range(1, max_steps + 1):
    out = chat_step(messages, tools=tool_schemas(), temperature=0)   # 1. 让模型决策
    messages.append(assistant_message(out["message"]))
    tool_calls = out["message"].get("tool_calls") or []

    if not tool_calls:            # 2. 它不打算调工具了 = 它要回答了
        answer = out["message"]["content"]
        break

    tool_messages, steps = run_tool_calls(tool_calls, ...)           # 3. 执行工具
    messages.extend(tool_messages)                                   #    把结果塞回对话
```

**就这么多。** 剩下的全是护栏和可观测性。

### 三个关键设计点

**一、为什么必须把 assistant message 原样追加回去**

开了工具调用之后，模型返回的 message 里除了 `content` 还有一个 `tool_calls`，
每个 call 带一个 `id`。下一轮的 `role: "tool"` 消息必须用 `tool_call_id` 指回它。

如果你自己拼一个 `{"role":"assistant","content": ...}` 塞回去，`id` 和 `tool_calls` 就丢了——
模型下一轮会认为那条工具结果是别人给的，**轻则重复调用，重则直接报错**。

**二、为什么工具报错不能抛异常**

工具的参数是**模型生成的**，也就是说**输入不可信，写错是常态**。
把异常抛出去，整个循环就崩了。

正确做法是把错误当成一条「观察结果」返回给它：

```
tool → {"error": "主机名不合法：'web-01; rm -rf /'"}
```

模型看到自己写错了，第二轮基本能改对。**这是工具层和普通函数最大的区别。**

**三、为什么必须有 `max_steps`**

模型可能陷入"反复查同一个东西"的死循环。没有上限意味着：
请求永远不返回 + 无限花钱。所以：

- `max_steps` 是硬上限
- 同一个工具 + 同样的参数查第二次 → 检测到，直接告诉它「你查过了」
- 撞上限时**把 `tools` 参数去掉**再问一次，让它只能输出文字
  （还带着 tools 的话，它很可能又调一次，然后再撞上限）

---

## 第 2 章｜LangGraph 版：把循环画成图

同一个行为，用状态图表达（`app/agents/graph.py`）。

### 2.1 图长什么样

这是 `python -m app.agents.graph --graph` 的真实输出：

```
       ┌─────────────┐
 START │    agent    │  调模型，决定"回答"还是"调工具"
       └──────┬──────┘
              │ 有 tool_calls 吗？
      ┌───────┼────────┐
      │ 有          没有│
      ↓                ↓
 ┌─────────┐      ┌─────────┐
 │  tools  │      │finalize │  超上限时强制收口（不带工具）
 └────┬────┘      └────┬────┘
      │                │
      └──→ agent       └──→ END
           ↑
      这条回头边就是「循环」
```

mermaid 原文（可以直接贴到 GitHub README 或飞书文档里渲染）：

```
graph TD;
	__start__ --> agent;
	agent -.-> __end__;
	agent -.-> finalize;
	agent -.-> tools;
	tools --> agent;
	finalize --> __end__;
```

**这张图的形状，就是手写版那个 `for` 循环的等价物：**

| 手写版 | LangGraph 版 |
|---|---|
| `for step_no in range(...)` | `add_edge("tools", "agent")` —— 回头边就是循环 |
| `if not tool_calls: break` | `add_conditional_edges` 返回 `END` |
| `if step_no >= max_steps:` | 路由函数返回 `"finalize"` |

### 2.2 状态与 reducer —— 框架真正多给的东西

手写版里那些变量（`messages` `steps` `usage`），在图里变成显式声明的状态：

```python
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]      # 追加消息，按 id 去重
    steps:    Annotated[list, operator.add]      # 轨迹拼接
    rounds:   Annotated[int, operator.add]       # 轮次累加
    usage:    Annotated[dict, _merge_usage]      # ★ token 用量累加
    stop_reason: str                             # 不写 Annotated → 覆盖
```

**`Annotated[..., reducer]` 是 LangGraph 的核心概念**：节点返回的字典怎么合并进状态，
由 reducer 决定。不写 reducer 就是"后写的覆盖前面的"。

`usage` 必须写 reducer —— 每次模型调用都消耗 token，节点只知道自己那一次的量。
**如果写成默认覆盖，最后只剩最后一次的用量。而 Agent 恰恰是"多轮累加"才贵，
统计错了会让你完全估不准成本。**

> ★ **这里踩过一个真实的坑，值得单独记下来**：
> DeepSeek 的 `usage` 不是扁平的，它带嵌套字段：
> ```json
> {"prompt_tokens": 29, "completion_tokens": 14, "total_tokens": 43,
>  "prompt_tokens_details": {"cached_tokens": 0}}   ← 这是个 dict
> ```
> 无脑写 `out[key] = old + val` 就会抛
> `TypeError: unsupported operand type(s) for +: 'int' and 'dict'`。
>
> **「累加一个统计字典」这种看着最不可能出错的代码，恰恰因为第三方返回结构的意外而挂掉。**

### 2.3 ★ 消息格式转换 —— 用框架的代价就体现在这里

LangChain 有自己的消息类，而我们直接调 API 要的是 dict。中间要转一层：

| LangChain | 原生 API |
|---|---|
| `SystemMessage` | `{"role":"system", "content": str}` |
| `HumanMessage` | `{"role":"user", "content": str}` |
| `AIMessage` | `{"role":"assistant", "content": str, "tool_calls":[{"id","type","function":{"name","arguments"}}]}` |
| `ToolMessage` | `{"role":"tool", "tool_call_id": str, "content": str}` |

**最坑的一处**：原生格式里 `arguments` 是**字符串**，LangChain 里 `args` 是**已解析的 dict**。

于是就有了这样一条往返路径：

```
模型输出的原始字符串 →「LangChain 标准化的 dict」→ 要发回给 API 的字符串
```

**一来一回，如果中间某次解析失败（模型输出了非法 JSON），原始字符串就丢了** ——
模型看不到自己写错在哪，只会反复犯同样的错。

所以代码里用 `additional_kwargs` 把**原始字符串**一起存下来，发回给 API 时优先用它。
多存一个字段，换来"零信息损失"。

这一段是整份代码里最容易出 bug 的地方：**两种消息格式的字段名不一样，
少写一个字段不会报错，只会在某次工具调用时莫名其妙地失败。**

---

## 第 3 章｜实测对比

`python -m app.agents.compare` 跑出来的真实数据。

**控制变量**：同一份 Prompt、同一套工具执行逻辑（共用 `common.py`）、
同一模型、`temperature=0`。
**唯一变量**：编排方式。

### 3.1 三个用例

| 用例 | 类型 | 问题 |
|---|---|---|
| Q1 | 有明确故障 | web-01 上的网站访问很慢，有时报 502 |
| Q2 | 容器故障 | cache-01 上的 Redis 容器一直在重启 |
| Q3 | **负例**（不该查出问题） | db-01 最近状态怎么样，有没有需要注意的地方 |

> ★ Q3 是最关键的一个用例：一切正常时，Agent 应该如实说"没发现问题"，
> 而不是硬找一个问题出来。**这是最容易被模型搞砸的一类**——
> 它为了显得有用，很容易把正常波动描述成隐患。

### 3.2 结果

| 用例 | 工具调用(手写/LG) | token(手写/LG) | 耗时(手写/LG) | 工具集合 | 调用序列 |
|---|---|---|---|---|---|
| Q1 | 10 / 7 | 9717 / 6214 | 9.3s / 6.4s | 一致 | 不同 |
| Q2 | 6 / 6 | 7124 / 7176 | 6.2s / 7.2s | 一致 | 不同 |
| Q3 | 5 / 5 | 4880 / 4894 | 4.0s / 3.9s | 一致 | 相同 |
| **合计** | **21 / 18** | **21721 / 18284** | **19.5s / 17.5s** | **3/3 一致** | **1/3 相同** |

### 3.3 四条结论

**一、工具选择高度一致（3/3）—— 这是最重要的一条。**

同样的 Prompt 和工具 schema 下，两个引擎**选了同一批工具**。
说明「该查什么」由 **Prompt + 工具设计** 决定，与用什么框架**无关**。

> **Agent 的行为质量取决于你的 Prompt 和工具设计，不取决于框架。**

**二、调用序列只有 1/3 相同 —— Agent 行为不是严格可复现的。**

同一份 Prompt、同一个模型、`temperature=0`，两个实现仍会走出不同的路径。

这条的实践含义：**评测必须看统计分布，不能只看单次结果。**
上线后必须要有评测集兜底，而不是靠"我试过一次没问题"。

**三、token 差异（21721 vs 18284）主要来自模型随机性，不是框架开销。**

因为两个版本共用了同一份 Prompt 和同一套工具执行代码，真正的变量只剩
"模型这一步输出了什么"——它走了不同的路径、调了不同数量的工具，后续轮次把差异放大了。

> ⚠️ **样本只有 3 个用例，这个差异在统计上不构成结论。**
> 真要做选型对比，需要几十个用例 + 多轮重复。这里能站住的只有前两条。

**四、评测里最危险的东西：看起来是数据、实际是故障。**

第一次跑这个对比时，LangGraph 版在 Q1 上返回「工具调用 0 次、token 0」——
**看起来像是"框架开销极低"，实际是那次调用报错了**，瞬时网络问题被吞掉。

如果不加处理，你会得出"LangGraph 比手写省 10 倍 token"这种完全错误的结论，
而且**没有任何察觉**，因为表格里就是两个正常的数字。

所以 `compare.py` 里加了两条规则，这两条在真实评测里经常被忘掉：

1. **失败自动重试一次** —— 过滤瞬时抖动
2. **重试仍失败就把错误原文写进报告并标 ⚠️** —— 让人一眼分辨"测量结果"还是"故障"
3. **合计必须剔除失败用例** —— 否则一个故障被算成优势

---

## 第 4 章｜框架替你做了什么、代价是什么

**这是「什么时候该用框架」的判断依据。**

### 框架多给的

| | 手写版 | 框架版 | 差异的份量 |
|---|---|---|---|
| **检查点**（能中断续跑） | 要自己做 | 内建 | ★★★ **这是「人工确认」的技术前提**：流程走到一半停下来等人点"同意"，人点了再从那一页继续。手写要做这个得自己实现状态落盘 |
| **结构可视化** | 自己画 | `draw_mermaid()` | ★★ 一张 mermaid 就是架构图，写文档直接用 |
| **按节点流式输出进度** | 要自己做 | `stream(stream_mode="updates")` | ★★ 前端能显示"正在查磁盘…"，这是手写要自己造的 |
| **状态声明式管理** | 局部变量 | `AgentState` + reducer | ★ 改动时不容易漏 |
| **递归上限兜底** | 自己写 | `recursion_limit` | ★ 第二道保险 |

### 代价

| 代价 | 具体表现 |
|---|---|
| **多一层抽象** | 出错时报错栈里全是框架内部帧，比手写难查（本项目就踩过一次，见 2.2 那个 `TypeError`） |
| **消息格式要转换** | 就是 2.3 那一整节。**这是最容易出 bug 的地方** |
| **依赖变重** | 装 `langgraph` 顺带引入 `langchain-core`、`langgraph-checkpoint`、`langsmith` 等 7 个包 |
| **版本变化快** | 0.x → 1.x 期间 API 改过好几轮（本项目装的是 1.2.12） |

### 判断标准

> **手写版理解原理，框架版上生产。顺序不能反。**
>
> 反了的话你只会用框架，却说不清它替你做了什么。

具体到本项目：
- **当前阶段**：两个都保留。手写版证明理解原理，框架版证明会用主流工具
- **做人工确认时**：会自然偏向框架版，因为检查点正是它的强项
- **为什么两个都保留**：先说"我两个都写了，因为想搞清楚框架到底做了什么"，
  再展开到 1.2 和 1.3 那两个坑 —— **这比"我用了 LangGraph"更有说服力**

---

## 第 5 章｜怎么用、怎么测

### 5.1 命令行

```bash
# 手写版
.venv\Scripts\python.exe -m app.agents.react "web-01 上的网站很慢，帮我查下"

# LangGraph 版
.venv\Scripts\python.exe -m app.agents.graph "cache-01 的 Redis 容器一直重启"

# 只看状态图（不花钱）
.venv\Scripts\python.exe -m app.agents.graph --graph

# 两个引擎对比（约 ¥0.1，跑 40 秒）
.venv\Scripts\python.exe -m app.agents.compare
```

加 `--quiet` 只看最终答案；`--max-steps N` 改轮次上限。

### 5.2 HTTP 接口

```bash
# 看它有哪些工具可打
curl -s http://127.0.0.1:8000/agent/tools

# 看状态图
curl -s http://127.0.0.1:8000/agent/graph

# 让它自己诊断（agent/ask 响应里带完整 trace）
# 中文 payload 先用文件生成，见 README「调用示例」
curl -s -X POST http://127.0.0.1:8000/agent/ask \
  -H "Content-Type: application/json" --data-binary @_agent.json
```

响应里的 `metrics` 是 Agent 特有的可观测指标：

```json
{"rounds": 3, "tool_calls": 9, "distinct_tools": 6,
 "tokens": 9208, "elapsed_ms": 8271, "stop_reason": "answered"}
```

> **`rounds` 和 `tool_calls` 这两个数字，是普通问答接口没有的。**
> 它们才是 Agent 的"性能指标"——就像 HTTP 服务的 QPS 和 P99 延迟。

### 5.3 自检

```bash
.venv\Scripts\python.exe scripts\smoke_test.py          # 九层，不花钱
.venv\Scripts\python.exe scripts\smoke_test.py --full   # 加一次真实 Agent 调用
```

Agent 层会检查四件事：

```
✅ 注册表载入 6 个工具   后端 mock
✅ schema 转换 6 条      模型看到的就是这些 schema
✅ 执行 check_disk(web-01)   最高使用率 96% / level=critical
✅ 拒绝非法参数（防注入）   主机名不合法：'web-01; rm -rf /'
✅ 手写 ReAct 引擎可导入
✅ LangGraph 状态图编译   mermaid 409 字符　循环边 存在
```

**注意第 4 项**：这不是"功能测试"，是**安全边界测试**。
参数校验是防命令注入的那道墙，墙必须被主动验证过才算存在。

---

## 第 6 章｜设计问答

### ReAct 的实现要点

> ReAct 就是"推理 + 行动"。实现上它是一个循环：把问题给模型，
> 模型决定是直接回答还是调工具；要调工具就执行、把结果塞回对话、再问一遍。
> 我的实现里就 30 行主干，剩下全是护栏：
> max_steps 上限、重复调用检测、工具报错当观察结果返回。

### LangGraph 底层在做什么

> 我把同一个循环手写了一遍，所以能说得清。手写版是 `for` 循环加 `break`，
> LangGraph 版是状态图：`add_edge("tools","agent")` 那条回头边就是循环，
> `add_conditional_edges` 就是那个 if，`Annotated[list, add_messages]`
> 这种 reducer 决定状态怎么合并。
>
> 它真正多给我的是三样：**检查点**（能中断续跑，这是我后面做人工确认的前提）、
> 结构可视化（`draw_mermaid()` 直接出架构图）、按节点流式输出进度。

### 框架相比手写有什么代价

> 最实在的代价是**消息格式要转换**。LangChain 有自己的消息类，
> 我直接调 API 要的是 dict。原生格式里 `arguments` 是字符串，
> LangChain 里 `args` 是已经解析好的 dict。这层转换里少写一个字段不会报错，
> 会等到某次工具调用时莫名其妙地失败。
>
> 我还踩过一个更隐蔽的：DeepSeek 的 usage 带嵌套字段
> `prompt_tokens_details`，我那个累加 reducer 只假设了整数，直接抛
> `TypeError: int + dict`。**这种"看着最不可能出错的代码"最容易挂。**

### 如何避免乱调工具与无限循环

> 四道护栏，从外到内：
> 1. **工具层**：参数白名单校验，绝不拼 shell —— `web-01; rm -rf /` 直接拒
> 2. **循环层**：`max_steps` 硬上限；同一个工具同样的参数查第二次会被检测出来并提示
> 3. **收口**：撞上限时把 `tools` 参数去掉再问一次，让它只能输出文字
> 4. **框架层**：`recursion_limit` 兜底
>
> 另外工具全部只读 —— **诊断和处置是两件事**。
> 处置要人工确认，这是我做沙箱和 HITL 的原因。

### 两个诚实的回答

**对比结论是什么**

> 三条。**第一，工具选择 3/3 一致** —— 说明该查什么由 Prompt 和工具设计决定，
> 跟框架无关。**第二，调用序列只有 1/3 相同** —— 说明 Agent 行为不是严格可复现的，
> 所以评测必须看统计分布，不能只看单次结果。**第三，token 的差异我不敢下结论** ——
> 样本只有 3 个用例，这个量级在统计上没有意义。

**评测里踩过什么坑**

> 最危险的一个：有一次 LangGraph 版跑出「工具调用 0 次、token 0」，
> 表格里看着像"框架开销极低"，实际是那次调用报错了、错误被吞掉了。
> 如果没发现，我会得出一个方向完全相反的结论，而且毫无察觉。
> 所以后来加了两条规则：失败重试一次；重试仍失败就把错误原文写进报告并标注，
> 而且**合计必须剔除失败用例**。
>
> **评测报告里最危险的不是没有数据，是"看起来是数据、实际是故障"的那一行。**

---

## 附：文件对照

| 文件 | 作用 |
|---|---|
| `app/tools/ops.py` | 6 个运维工具 + 参数白名单 + 风险分级 + mock/local 双后端 |
| `app/agents/common.py` | 两个引擎共用的 Prompt、消息处理、工具执行（保证对比公平） |
| `app/agents/react.py` | 手写 ReAct 循环（零依赖） |
| `app/agents/graph.py` | LangGraph 状态图版本 |
| `app/agents/compare.py` | 对比评测，输出 markdown 报告到 `eval/reports/` |
| `app/llm.py` → `chat_step()` | 带 `tools` 参数的一步请求（返回完整 message，不只取 content） |
| `app/main.py` → `/agent/*` | 3 个接口：工具清单、状态图、自主诊断 |
