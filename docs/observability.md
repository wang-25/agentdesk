# 可观测：全链路 Trace + 成本看板（Day 8）

## 一句话

把「每一次 Agent 运行」变成可查询、可算账的数据——
**我不只知道花了多少钱，我知道每个环节花了多少、哪一步最慢、哪一步在白花钱。**

## 三层架构

```
app/observability/
├── tracer.py          记录层   trace/span 落 JSONL。本地文件，永远可用
├── costs.py           算账层   token → 成本（含缓存折扣），按 Agent/工具聚合
└── langfuse_export.py 导出层   配了 Key 就推 Langfuse，没配就静默跳过
```

**本地记录是主路径，Langfuse 是可选导出。**

这个顺序不能反：

- 观测是"出问题时才被需要"的东西。把它依赖在外部服务上，等于让
  最需要它的时刻（外部服务也挂了的时候）变成最不可用的时刻
- 面试现场演示如果断网，Langfuse 面板打不开，但 `logs/traces.jsonl` 还在

## 概念与实现

| 概念 | 是什么 | 例子 |
|---|---|---|
| **trace** | 一次完整运行 | 一次 `/agent/ask`、一次告警处置 |
| **span** | trace 里的一个环节 | 一次模型调用 / 一次工具调用 / 一个 Agent 节点 |
| **usage** | token 消耗 | 挂在 span 上，聚合出成本 |

三个关键实现决定：

**一、contextvar 而不是层层传参。** span 要知道"我属于哪个 trace、父是谁"。
把 trace_id 当参数层层传，`llm → agents → tools` 每个签名都要改，漏一处断链。
用 contextvar 栈，**加观测不用改任何函数签名**——和 llm.py 统一入口是同一原则：
横切能力长在它该在的地方。

**二、"结束时写完整记录"而不是"事件日志+折叠"。** approvals.py 用折叠是因为它有
状态机要重放；trace 每条 span 结束时一次性写完整（耗时/usage/状态），查询直接读。
代价是进程崩溃时未结束的 span 会丢——**观测数据允许丢，业务数据不允许丢**。

**三、观测代码绝不能让业务挂掉。** tracer 所有对外函数吞自己的异常；
但 span 里**业务**的异常只记录、不吞（原样抛出）。自检里专门有一条验证这个。

## 接入点（全部是"一行接入"）

| 位置 | 记什么 |
|---|---|
| `llm.chat` / `llm.chat_step` | 每次模型调用：模型名、usage、是否产生工具调用 |
| `tools.execute_tool` | 每次工具调用：参数摘要、ok/error、风险等级 |
| `supervisor` 每个节点 | 每个 Agent 节点：该节点消耗的 token |
| 三个引擎 `run()` | 整次运行包成一个 trace（`@tracer.traced` 装饰器） |
| `/chat`、`/webhook/alert` | 一次 HTTP 请求 = 一个 trace |

## 查询接口

```
GET /traces             最近的运行列表（新→旧，含成本与耗时）
GET /traces/{id}        单次完整轨迹：每个 span 的耗时/状态/参数摘要
GET /metrics/summary    聚合看板（默认最近 50 次运行）
```

`/metrics/summary` 能回答的问题：

- 平均一次运行花多少钱、多少 token；P50/P95 延迟
- 成本按 **Agent** 分布（intent / knowledge / diagnose / verify / remediate）
- 成本按**工具**分布（check_disk / run_command / …）
- DeepSeek **缓存命中率**——命中率低 = system prompt 每次都在变，白花钱
- 错误率

## 成本核算的一个细节

DeepSeek 的 usage 里有 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，
**命中部分单价是 0.5/M，未命中是 2/M**（上次核对 2026-09-26，会变，改 costs.py 顶部的表）。

> 大多数人算成本就是 prompt × 单价。把缓存折扣算进去才是真的会算账——
> 而且缓存命中率本身就是一个可优化指标。

## Langfuse 自托管（可选）

`deploy/langfuse-compose.yml`：Langfuse v2 单容器 + Postgres（v3 要 ClickHouse/MinIO/
Redis 全家桶，学习项目没必要）。数据全在本机 Docker 卷，不外发。

启用步骤见 compose 文件头部注释。没配 Key 时 `/traces` 会显示
`langfuse.enabled: false`，一切照常。

**为什么手写导出而不用 langfuse SDK**：ingestion 协议就是一个 POST + Basic Auth
+ 约定的 event 结构。手写一遍，`trace-create / span-create / generation-create`
怎么映射亲眼见过，**这个文件零新依赖**。整条 trace 结束时一次批量推、
放后台线程——导出是旁路，不能拖慢主链路，更不能在 Langfuse 挂掉时拖垮用户请求。

---

## 踩到的坑（本天最有价值的两个）

**一、嵌套 span 让整条 trace 静默丢失**

`parent_id = stack[-1]` ——塞进去的是 **_Span 对象本身**而不是它的 id。
顶层 span 的 parent_id 是 None（可序列化），单层 trace 一切正常；
一嵌套，`json.dumps` 失败，而观测层按铁律吞掉了自己的异常——
**结果不是报错，是数据静默丢失**。两条教训：

1. 「栈顶元素」和「栈顶元素的 id」差一个属性访问，类型系统救不了你
2. **静默吞异常的代码必须配自检**——smoke_test 里那几条 trace 记录断言
   就是为这类问题存在的，否则丢失无信号

**二、成本归因连踩三个版本（每个版本都是一次对账教训）**

「成本按 Agent 分布」看着简单，实际做了三版才对：

| 版本 | 写法 | 后果 |
|---|---|---|
| v1 | `set_usage(out["usage"])` | **覆盖语义**，把子 span 归并上来的用量清零 → 看板显示"这个 Agent 不花钱" |
| v2 | `add_usage(out["usage"])` | 累加，但 `out["usage"]` 是**引擎自报的整轮汇总**，和子 span 归并值重复 → 成本翻倍 |
| v3 | 什么都不加，只靠子 span 归并 | 叶子 span 是唯一事实源，**节点合计 = 动作合计 = 总成本** ✓ |

> **同一个量只能有一个事实源。** v2 的错误很典型：两个来源（传播 + 引擎自报）
> 都是"对的"，加起来就是错的。而对账（三者是否一致）是唯一能发现它的手段 ——
> 不报错、不越界，只是数字大了一倍。

**二·补、`int + dict`：同一个坑，本项目第二次踩**

归并 v3 之后仍有问题：节点用量里**有 prompt/completion/total，却没有缓存命中字段**。
根因是手写累加 `parent.usage[k] = parent.usage.get(k, 0) + v` 遇到
DeepSeek usage 里的 `prompt_tokens_details`（嵌套 dict）→ `0 + dict` 抛 TypeError
→ 被 `except Exception: pass` 吞掉 → **归并半途中断**。

dict 键顺序救了前三个字段、坑了后面两个，所以表现是"数据看起来是有的"——
节点成本按"全部未命中"算，**数字虚高（实测 43%）**。

> ① **累加前必须过滤非数值字段** —— 外部返回的 usage 里混嵌套结构是常态
> ② **吞异常的代码会吞掉"半成品状态"** —— 丢一半比丢整条更难发现
> ③ 这个坑在 Day 4 的 `_merge_usage` 里已经踩过一次并写进了注释，
>    **说明"写下来"不等于"不会再犯"，要有断言守着**（现已固化为自检项）

**三、断言绑定了后端实现细节**

沙箱自检写死了"mock 后端必须返回仿真数据"。用户装上 Docker 后 `auto` 切到
真执行，`du` 在 Windows 上找不到 `/var/log`，断言就错了。
**断言不该绑定后端实现细节**，改成"无论哪个后端，执行器都给出确定性结果"。

**四、（真实收获）Permission denied 恰好是安全设计的证明**

Docker 真跑起来后，容器以 uid 65534（nobody）执行，root 建的日志文件
`truncate` 直接 Permission denied。**这正是"非 root 运行"在做它该做的事**——
模拟了真实生产里最常见的权限问题。把文件属主修对（chmod 666）后，
同一条链路完整跑通。

## 面试三问

**Q：你们怎么做 Agent 的可观测？**
答三层：本地 JSONL trace 是主路径（不依赖外部服务），Langfuse 是可选导出，
成本按 Agent/工具维度聚合。然后直接给出数字："意图路由占 12%，诊断占 71%，
其中 XX 工具调用占了大头"——**有维度的成本数据才能指导优化**。

**Q：为什么不直接用 Langfuse SDK？**
ingestion 是一个 HTTP 协议，手写 60 行换零依赖 + 出问题知道查哪。
和手写模型 API 调用是同一个决策。

**Q：trace 数据丢了怎么办？**
观测数据允许丢（进程崩溃时未结束的 span），业务数据不允许丢。
这是两类日志的根本区别——审批单就必须用"追加事件+折叠"保证不丢。

## 怎么用

```bash
# 产生数据
.venv\Scripts\python.exe -m app.agents.supervisor "web-01 上的网站很慢"

# 看列表 / 详情 / 看板（起服务后）
curl http://127.0.0.1:8000/traces
curl http://127.0.0.1:8000/metrics/summary

# 第八层自检（离线、免费）
.venv\Scripts\python.exe scripts\smoke_test.py
```
