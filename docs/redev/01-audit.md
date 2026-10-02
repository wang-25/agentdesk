# AgentDesk 二次创作 · 尽调报告（Workflow 步骤 1）

> 目的：在做任何改动之前，先把「它到底是什么状态、缺什么、哪些不能动」用**可复现的证据**钉死。
>
> 方法：**运行时实证**（跑起来看）+ **静态审计**（读代码要行号）+ **交叉验证**（对审计结论逐条复核，纠正误报）。
>
> 所有结论都附 `文件:行号`；未经验证的推测一律标注「未确认」。

---

## 0. 结论速览

**它不是"半成品"，而是「单机演示级完成度很高 + 生产级运维闭环缺失」。**
把两件事混为一谈会导致错误的改造策略：推翻重来会把已经做对的东西一起砸掉，
而"补一个聊天框"又完全没碰到真正的缺口。

### 0.1 运行时实证：能跑，而且跑得不错

| 取证项 | 命令 | 结果 |
|---|---|---|
| 九层自检 | `.venv\Scripts\python.exe scripts\smoke_test.py` | **exit 0，全部通过**（13 组 47 项，4 项按设计跳过） |
| HTTP 服务 | `uvicorn app.main:app --port 8011` | 启动成功，26 个 OpenAPI 操作 |
| 接口存活 | 8 个只读接口探针 | `/health` `/` `/sandbox` `/traces` `/metrics/summary` `/audit` `/approvals` `/agent/tools` **全部 200** |
| 历史使用 | `logs/traces.jsonl` | **1291 条 trace**、145 条审计、22 张审批单 → 不是"没跑过"的骨架 |
| 模型连通 | 自检第 2 层 | 真实调用 0.8s 成功 |
| 真机连通 | 自检第 1 层 | `OPS_BACKEND=ssh`，3 个目标中 1 个真机连通 |
| 版本管理 | `git log` | 51 次提交，工作区仅 `README.md` 有未提交的排版改动 |

### 0.2 真正的八处短板（按「不补就上不了生产」排序）

| # | 短板 | 一句话后果 |
|---|---|---|
| 1 | **告警闭环断在"通知"** | 凌晨三点的诊断结论只回到 HTTP 响应和 `audit.jsonl`，**没有任何出站通知**，没人看得到 |
| 2 | **无事件（Incident）实体** | 告警、诊断、审批、处置各自为政，没有"一个故障"这个对象：无状态、无时间线、无归属 |
| 3 | **告警风暴无聚合/关联** | 去重键是 `(alertname, host)` 精确匹配 + 固定 600s（`main.py:2287`），500 条告警 = 500 份孤立报告，看不出"同一个根因" |
| 4 | **零测试、零 CI** | 无 `tests/`、无 `.github/`、无 `pyproject.toml`/`pytest.ini`；1.3 万行代码只有自检脚本，改坏无人知道 |
| 5 | **"九层自检全通"含水分** | HTTP 层在服务未启动时**整体跳过且不计失败**（`smoke_test.py:819-825,1115-1116`）——我实测那次 exit 0 就是跳过的 |
| 6 | **语义检索实际未启用** | `DASHSCOPE_API_KEY` 为空 → 索引是 `local-hash` 词袋（`data/index/chunks.json` 元数据实证），**无语义能力**，作者已在 `.env.example:18-23` 自认 |
| 7 | **执行面有 4 条真实缺陷** | PATH 可劫持绕过整表白名单（`policy.py:792-799`）、审批单可跨进程双执行（`approvals.py:109-145`）、批准后无执行时限、读路径与注释矛盾（`policy.py:235-240`） |
| 8 | **状态全在进程内** | 限流/额度/告警去重是内存态（`security.py:101-104`、`main.py:318-319`），多 worker 即失效；代码注释自己承认（`security.py:96-99`），`Dockerfile` 只能 `--workers 1` |

### 0.3 二次创作的策略判断

- **不能动**：分层解耦结构、fail-closed 安全边界、审批状态机、成本归因链路、九层自检——这些是本项目最值钱的资产。
- **该补的**：把「诊断能力」接进「运维闭环」，并补上工程可信度。这两件事都不需要推翻任何现有设计。
- **不该做的**：为了显得高级而引入向量库/微服务/消息队列——91 块语料和单机规模用不上，属于过度设计。

---

## 1. 取证方法与可复现性

```bash
# ① 运行时实证（零成本，先说结论：全部通过）
.venv\Scripts\python.exe scripts\smoke_test.py            # exit 0

# ② 服务层实证
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8011
curl http://127.0.0.1:8011/openapi.json                   # 26 operations
curl http://127.0.0.1:8011/health                         # 200

# ③ 工程化现状（结论：三者皆无）
git rev-list --count HEAD                                 # 51
Test-Path tests, .github, pyproject.toml                   # False

# ④ 静态审计：四路并行（服务/安全 · 工具/沙箱 · 编排/检索 · 可观测/工程化）
```

**方法学声明**：本次审计**对每条关键结论做了复核**，并纠正了下述误报——
误报如果照抄进报告，会直接导致错误的改造优先级。

### 已纠正的审计误报

| 误报 | 复核结论 | 证据 |
|---|---|---|
| `verify` 节点返回 `usage: new_usage()` → **校验 Agent 的 token 未被计入成本** | **误报，实为按设计。** 节点返回值**故意不作为**成本事实源；真正的账在叶子 LLM span 上，并通过父子归并汇总 | `llm.py:138,163`（LLM span 带 usage）→ `tracer.py:258`（子 span 归并进父）→ `supervisor.py:414-424`（v1/v2/v3 三版教训，明确"叶子 span 是唯一事实源"） |
| `/settings/api` 明文返回完整 `AGENT_TOKEN`，判为**阻断级** | **降级为"硬化项"。** 该接口**不在**免鉴权清单里，开鉴权时必须持有效令牌才能读 | `security.py:81`（`PUBLIC_EXACT` 不含它）、`security.py:83-85`（作者已显式声明该设计）；真正风险是 `AUTH_ENABLED=0` 时整个服务本就敞开 |

---

## 2. 项目现状（架构与功能清单）

### 2.1 分层架构（据代码实证，非文档转述）

```
调用方：值班工程师 / 告警系统(Alertmanager·Zabbix) / MCP 客户端(Cursor…)
                         │
        ┌────────────────▼─────────────────┐
        │ app/main.py  3111 行 · 31 个路由  │  ← 大门（含 4 个内嵌 HTML 页面，约 1800 行）
        │  鉴权 → 限流 → 额度 → 校验 → 留痕  │
        └──┬────────┬────────┬─────────────┘
           │        │        │
   ┌───────▼──┐  ┌──▼─────┐  ┌▼──────────────┐
   │ agents/  │  │ rag/   │  │ mcp_server/   │  7 tools + 2 resources
   │ 3 套引擎  │  │ 混合检索│  └───────────────┘
   │ 5 专业Agent│ │ 91 块  │
   └───┬──────┘  └────────┘
       │
   ┌───▼──────────────────────────────────────┐
   │ tools/  7 个工具（6 只读 + run_command）    │  OPS_BACKEND = mock | local | ssh
   └───┬──────────────────────────────────────┘
       │ 写操作
   ┌───▼──────────────────────────────────────┐
   │ sandbox/  policy(13 条白名单) → approvals  │  fail-closed；ssh 后端下执行通道关闭
   │           → executor(一次性容器)           │
   └──────────────────────────────────────────┘

横切：observability/（trace+成本+审计，落 logs/*.jsonl） · evaluation/（4 规则+模型判分）
```

**三套编排引擎并存且都是真实实现**（这是本项目少见的地方——不是"备选方案写在文档里"，而是三份可跑的代码）：

| 引擎 | 实现 | 节点/回边 | 完成度 |
|---|---|---|---|
| `agents/react.py` | 手写 ReAct 循环 | `for step in range(1, max_steps+1)` | 完整，无 checkpoint |
| `agents/graph.py` | LangGraph 状态图 | 3 节点 + tools→agent 回边 | 完整，含悬空 tool_call 协议补丁 |
| `agents/supervisor.py` | **多 Agent 编排** | **8 节点**：supervisor/intent/knowledge/diagnose/reason/verify/remediate/finalize，6 条回边 | 最完整 |

### 2.2 功能清单（现状）

| 能力域 | 已实现 | 关键证据 |
|---|---|---|
| 服务化 | 31 路由、SSE 流式、OpenAPI 自动文档、`/try` 试用页、`/dashboard` 看板、`/settings` 运行时改配置 | `main.py`；OpenAPI 26 ops 实测 |
| 鉴权 | 单一静态令牌 + 中间件白名单 + `compare_digest` 常数时间比较；`AUTH_ENABLED` 默认 0 | `security.py:81,196,224` |
| 限流/成本闸 | 三层：每 IP 20/min、全局 60/min、每日额度 300（**仅花钱路径扣**） | `security.py:88-153` |
| RAG | 10 篇语料 / 91 块；chunk 400/overlap 80；BM25(k1=1.5,b=0.75) + 向量 + RRF(k=60)；引用溯源；库外拒答 | `loader.py:33`、`store.py:70,120,260` |
| 工具层 | 7 工具、参数正则白名单、三后端（mock/local/ssh）；逻辑名与真实地址解耦 | `tools/ops.py`；自检第 4 层 |
| 沙箱 + 人工确认 | 13 条命令白名单、43 项高危二进制拦截、审批单状态机、指纹防重放、fail-closed | `sandbox/policy.py`、`approvals.py`、`executor.py` |
| 可观测 | trace/span 树、成本按 Agent+动作双维归因、审计日志、Langfuse 导出（可选） | `observability/*`；1291 条历史 trace |
| 评测 | 40 条端到端 + 32 条检索 + 22 条意图；判定器自证（防"判定器自欺"） | `eval/*`、`app/evaluation/judges.py` |
| MCP 出口 | 同一批工具以标准 MCP 暴露，schema 漂移校验 | `mcp_server/server.py`；自检 9/9 |
| 部署 | 非 root 容器、内存硬上限 400M、端口只绑回环、健康检查、`logging` 轮转 | `Dockerfile`、`docker-compose.yml:60-101` |

### 2.3 已经做对、二次创作必须保留的部分

1. **分层不反向依赖**：换模型只动 `llm.py`，换工具实现只动 `tools/`，换编排只动 `agents/`。
2. **安全边界是结构而不是提示词**：诊断 Agent 的工具清单里**根本没有** `run_command`（schema 级隔离）；沙箱不可用时**拒绝执行**而不是降级。
3. **风险收敛到一处**：`policy.ACTION_RISK` 是唯一判定源，模型自报风险**只能抬高不能降低**（`policy.escalate`）。
4. **唯一事实源原则**：成本只在叶子 span 上记一次，父子归并、总账不重复计。
5. **"知道自己不知道"**：库外拒答 100%（裸模型 0%），且有判定器自证防止评测自欺。

---

## 3. 缺口清单（按层）

### A. 服务与安全层

| # | 缺口 | 证据 | 为什么在生产是问题 |
|---|---|---|---|
| A1 | **无任何会话/多轮上下文** | `ChatRequest` 只有 `question`（`main.py:178`）；全仓 grep `session_id\|conversation\|history` **零命中** | 运维排查天然是多轮（"再查下磁盘"、"按你说的做"），现在每句失忆 |
| A2 | **状态存储分裂**（内存 + JSONL 混用） | `security.py:101-104`、`main.py:318-319` 内存；审批/轨迹 `approvals.jsonl`/`traces.jsonl` 追加写 | `--workers 1` 成硬依赖；多实例 = 额度×N、去重失效、审批缓存不一致 |
| A3 | **模型层零重试零退避** | `llm.py:141,218` 单次 `httpx.post`，仅翻译 `ConnectError`/`Timeout`；出口一律 502（`main.py:2170,2601`） | 上游 429/抖动直接 502 给值班人；唯一重试在解析层（`main.py:268`） |
| A4 | **无后台任务/调度** | 全文件无 `lifespan`/`BackgroundTask`/`create_task` | 审批过期只能被读时惰性触发（`approvals.py:244`）；无巡检、无清理 |
| A5 | **改配置不传播** | `/settings/token` 直接改变量并写回 `.env`（`main.py:1517-1519`） | 改令牌在其它 worker/实例不生效，且无 schema 校验与回滚 |
| A6 | **无 `/metrics`** | `requirements.txt` 无 `prometheus_client`；`/metrics/summary` 是 JSON 聚合，且**全量读文件**（`tracer.py:425-446`、`main.py:3059`） | 接不进 Prometheus/Grafana → **Agent 自己不可被监控** |
| A7 | **日志无结构化、无统一入口** | 全项目仅 `security.py:53` 一个 logger，无 `logging.basicConfig` | 出问题只能翻 `.jsonl`，无法按级别/请求 ID 检索 |
| A8 | **3111 行单文件** | `main.py`，其中约 1800 行是内嵌前端字符串 | 改一个按钮要动服务入口；该文件无法单元测试 |

### B. 编排与检索层

| # | 缺口 | 证据 | 后果 |
|---|---|---|---|
| B1 | **无记忆/无 case memory** | grep `case_memory/long_term` 零命中；`verify_node` 结论不落库（`supervisor.py:324-340`） | 同一故障第 5 次发生仍从零推理；复盘结论无法回流知识库 |
| B2 | **无任务规划** | 唯一"规划"是二值路由 `_plan_for()`（`supervisor.py:137-156`） | 多步故障（磁盘满→日志暴涨→上游超时）被压成一次 ReAct，步骤不可见不可跟踪 |
| B3 | **无 checkpoint / 中断恢复** | `compile()` 未传 checkpointer（`supervisor.py:457`、`graph.py:442`）；`approvals.py:29-47` 自认未用 interrupt | **审批通过后不会自动续跑原流程**；进程重启轨迹全丢 |
| B4 | **RCA 深度不足** | 无拓扑/依赖/时序/多告警关联（grep 零命中）；`normalize_alerts` 逐条独立转问题（`main.py:333,367`） | 告警风暴看不到"同一个根因" |
| B5 | **无上下文预算控制** | `messages` 只增不减（`react.py:112`）；仅两处硬截断（`specialists.py:841,559`） | 大输出持续挤占窗口，成本与质量双双失控 |
| B6 | **无端到端墙钟预算** | 单次超时 60s/90s，但**整图无 deadline** | 一次诊断最坏可挂 5–6 分钟，前端只看到转圈 |
| B7 | **supervisor 全链路零评测** | `eval_specialists.py` 只测 `route_intent()`；`run_eval.py:123` 只调 `pipeline.answer()`；`compare.py` 仅 3 用例（报告自认"不构成结论"） | **五 Agent 链路（校验/处置/重试回边）改坏无人知道** |
| B8 | **知识库规模与增量** | 10 篇 / 91 块；`build_index` 全量重建（`pipeline.py:37-51`）；`_STORE` 进程级缓存不失效 | 语料更新必须手动调接口；覆盖面窄（无 Redis/ES/Kafka/Prometheus 语料） |
| B9 | **无 rerank** | `store.py:260-282` 仅 RRF | 91 块下收益小，扩到千级后必需 |
| B10 | **工具调用串行** | `common.py:142` `for call in tool_calls` | 5 个互不依赖的只读查询延迟叠加 |

### C. 工具与沙箱层

先说结论：**这一层的设计思想是对的（白名单 + 审批 + 一次性容器 + fail-closed），但实现上存在 4 条真实缺陷**——
它们的共同特征是「看起来有防护，实际在某条路径上不成立」。

| # | 缺口 | 证据 | 后果 |
|---|---|---|---|
| C1 | **PATH 劫持面**：白名单只比对裸命令名（显式拒绝带 `/` 的名字），执行时依赖继承的 `PATH` | `policy.py:792-799`、`executor.py:270` | 谁能控制 `PATH`，就能用一个假 `systemctl` 绕过**整张**白名单表 |
| C2 | **审批单可跨进程双执行**：状态只在进程内存、启动时折叠一次，无 `flock`/CAS | `approvals.py:109-145,330-336` | 两个进程各持一张 APPROVED → 同一条写命令**执行两次** |
| C3 | **批准后无执行时限**：过期只处理 `pending`，`consume`/`execute` 不校验 `expires_at` | `approvals.py:244-257,306-327`、`main.py:2933-2968` | 三天前批的单今天仍能执行，而策略与环境可能早已变化 |
| C4 | **审批记录会错报成功**：`consume(ok=True)` 在**执行之前**写入，`approvals.jsonl` 的 `result_ok` 不反映真实结果 | `main.py:2977-2978`；真实结果只落 audit 与 span（`main.py:3008-3013`） | 复核审批单的人看到"成功"，而真值在 `audit.jsonl` 的 `ok=false` 里 |
| C5 | **读路径与注释自相矛盾**：注释写"只允许读 `.log` 结尾"，代码只校验目录前缀 | 注释 `policy.py:235-239` vs 代码 `policy.py:240,315-318` | `/var/log/secure`、`wtmp`、`btmp`、`audit/*` 都在可读范围内 |
| C6 | **超时不回收**：`TimeoutExpired` 只杀 docker CLI 或直接子进程 | `executor.py:214-225,270-281` | 容器与孙进程继续存活，资源泄漏且"停不下来" |
| C7 | **写操作走 host 通道、零隔离**：`systemctl restart` / `docker restart` 以 API 进程权限直接执行 | `policy.py:669-680`、`executor.py:249-274` | "容器隔离"的承诺只覆盖 `truncate` 一类，重启类命令并无隔离 |
| C8 | **审计链缺口**：policy DENY、开票、只读 `run_command`、指纹不匹配的尝试都不写 `audit.jsonl` | `ops.py:901-911,915-952`、`main.py:2933-2968` | "谁试图做什么被拒"查不到，审计不完整 |
| C9 | **工具面窄**：无指标/时序查询、无进程/端口/连通性、无 k8s/DB 连接数/证书/inode/IO | 白名单仅 13 条（`policy.py:568-690`） | 诊断停在"看一眼瞬时点值"，拿不出趋势与容量证据 |
| C10 | **默认 host 是 `web-01`** | `ops.py:659,694,734` | 漏传主机参数时会拿到**另一台机器**的证据，而结论看起来照样可信 |
| C11 | **两条旁路未经统一收口**：`ops.py` 的 `journalctl` / `docker logs` 不走 policy/executor | `ops.py:546-575` | 只读通道存在绕过统一准入的路径（只读，风险有限） |

**被复核修正的一条**：C4 原始表述为"执行失败时审批记录仍报成功"——
核实后**部分成立**：`audit.jsonl`（`:3008-3013`）与 sandbox span（`:2994-2998`）都记录了真实 `ok`，
错的是 `approvals.jsonl` 里那份记录。且"先消费再执行"是**有意为之**的取舍
（`main.py:2971-2976` 明确论证：宁可"标记消费但执行失败"，也不要"执行成功还能再执行"）。
→ 正确的修法是**保持顺序不变，执行后回写真实结果**，而不是颠倒顺序。

### D. 可观测 / 评测 / 工程化

这一层的**设计**（trace 树、成本双维归因、判定器自证）质量很高；
问题集中在**工程化外壳**：没有测试、没有 CI、没有自监控。

| # | 缺口 | 证据 | 后果 |
|---|---|---|---|
| D1 | **零自动化测试**：无 `tests/`、无 pytest；自检脚本是自写计数而非断言式测试框架 | 目录缺失；`smoke_test.py:55-58,1115-1129` | 1.3 万行代码没有回归保护 |
| D2 | **自检的 HTTP 层可整体跳过，而仍然 `exit 0`** | `smoke_test.py:819-825`（服务未启动即 `skipped=True` 返回）、`:1115-1116`（skipped 不计失败） | **我实测那次 exit 0，第 9 层就是跳过的**——"全绿"不等于接口被验证过。这是自检最需要修的一处语义 |
| D3 | **无 CI/CD、无 lint、无类型检查** | 无 `.github/`、`pyproject.toml`、`pytest.ini`、`ruff.toml`、`Makefile`、`pre-commit` | README 写"退出码 0 可接进 CI"，但仓库里没有 CI |
| D4 | **评测无基线门禁** | 每次生成新时间戳报告（`run_eval.py:791`），无跨版本 diff、无阈值 | 指标退化无人知晓 |
| D5 | **审计写入无锁** | `main.py:158-172`（对比：tracer 有 `Lock`、approvals 有 `RLock`） | 并发写可能交错损坏行 |
| D6 | **JSONL 无界增长 + 全量读** | `/audit` 与 MCP `recent_audit` 全量 `read_text`（`main.py:2141`、`mcp_server/server.py:303`）；无轮转保留策略 | 文件越大越慢，最终拖垮查询接口 |
| D7 | **`spans[-80:]` 静默截断** | `tracer.py:355` | 超过 80 个 span 的运行，明细与对账被悄悄丢弃 |
| D8 | **无自身监控** | `requirements.txt` 无 `prometheus_client`；`docs/tech-stack.md:769` 自认"监控告警=打印日志" | **Agent 自己坏了没人知道** |
| D9 | **`/health` 不探依赖** | `main.py:2108-2123` 只回 status/version/security | 模型挂、索引坏、磁盘满时仍报 healthy |
| D10 | **无脱敏**：审计原样落 question、command、日志内容、webhook payload | `main.py:2294`；全 `app/` 仅 `_mask_token`（`:1419`，仅用于设置页展示） | 日志里的凭据/PII 会进审计文件且无保留期 |
| D11 | **无职责分离**：单一令牌，审批人只是自由文本 `by` | `security.py:59,81`、`main.py:2825` | **同一令牌可自己开票、自己批准、自己执行**——"人工确认"的制衡在单令牌下并不成立 |
| D12 | **审批无到期提醒** | `approvals.py:244-257` | 待审批单静静过期，没人被提醒 |

> D2 + D3 合起来是一个**认知风险**：项目对外宣称"九层自检全通，可接进 CI"，
> 而实际上「接口层可以跳过」「CI 并不存在」。
> 这不是造假，是**把"自检"当成了"测试"**——二次创作里必须先把这条缝补上，
> 否则后面所有改动的"已验证"都不可信。

### E. AIOps 场景闭环（本报告独立验证的核心发现）

这一节回答「它离"运维 Agent"还差什么」——**缺口不在诊断能力，而在闭环**。

```
现在：  告警 ──► 风险闸门 ──► 诊断 ──► ⛔ 结果只进 HTTP 响应 + audit.jsonl
                                          （没人被通知，没有工单，没有下一步）

应有：  告警 ──► 聚合/关联 ──► 事件(Incident) ──► 诊断 ──► 通知值班人
                    │                │                      │
                    │                ├─ 时间线/状态机        └─► 人审批 ──► 沙箱执行 ──► 回写事件
                    │                └─ 归属/SLA
                    └─► 复盘结论回流知识库 ──► 下次同类故障直接命中
```

| # | 缺口 | 证据 |
|---|---|---|
| E1 | **无出站通知**：全项目只有 3 处 `httpx.post`，分别是模型、embedding、Langfuse — **没有任何 IM/工单出口** | grep `dingtalk\|feishu\|slack\|wecom\|smtp` **零命中** |
| E2 | **无事件实体**：告警处理完即结束，无 incident id / status / owner / timeline | `main.py:2277-2401` 返回 `reports[]` 后即结束 |
| E3 | **去重是精确键 + 固定窗口**，且 `_ALERT_LAST_SEEN` **无上限、无清理**（长期运行内存单调增长） | `main.py:318,2287-2297` |
| E4 | **告警入口是同步阻塞的**：`def webhook_alert`（线程池内执行），`ALERT_AUTO_DIAGNOSE=1` 时每条告警在请求内跑完整诊断（6–20s+） | `main.py:2251`；`.env.example:43-48` |
| E5 | **无自动修复剧本与回滚**：`DIAGNOSE_PLAYBOOK` 是**写死的命令数组**，不是可执行剧本；无回滚/幂等 | `main.py:320-330,2390-2398` |
| E6 | **无复盘回流**：处置结果与结论不写回 `data/knowledge/` | 同 B1 |

> E1+E2 是本项目"无人值守"最讽刺的地方：它确实能在凌晨三点自己起来诊断，
> **但诊断完没有任何人会被叫醒**。补上这两条，闭环才成立。

---

## 4. 技术债清单（分级）

| 级别 | 项 | 证据 |
|---|---|---|
| **阻断** | 多 worker/多实例即让限流与每日额度失效（成本闸门形同虚设） | `security.py:96-99`（作者自认）、`Dockerfile` workers=1 |
| **阻断** | 零测试零 CI：1.3 万行代码改坏无保护 | 无 `tests/`、`.github/`、`pyproject.toml` |
| **高** | 语义检索实际未启用（词袋哈希兜底） | `data/index/chunks.json` 元数据、`.env.example:18-23` |
| **高** | 无会话记忆 → 多轮能力需从零建 | `main.py:178` |
| **高** | supervisor 五 Agent 链路无任何评测 | 见 B7 |
| **高** | 模型层无重试/退避/熔断，抖动直接 502 | `llm.py:141-160` |
| **高** | 审批后不能续跑（无 checkpoint） | `supervisor.py:457` |
| **中** | JSONL 无自动轮转；`read_recent` 只读尾部 2MB 且**吞掉所有异常返回 `[]`** | `tracer.py:425-446`；`logs/traces.jsonl` 已 851KB（存在人工 `.bak` 轮转痕迹） |
| **中** | 无 `/metrics`，Agent 自身不可监控 | 见 A6 |
| **中** | 3111 行单文件、前端字符串占约六成 | `main.py` |
| **高** | PATH 可被劫持，绕过**整张**命令白名单 | `policy.py:792-799`、`executor.py:270` |
| **高** | 审批单可跨进程双执行（状态仅在进程内，无 `flock`/CAS） | `approvals.py:109-145,330-336` |
| **高** | "九层自检全通"不等于接口被验证过：HTTP 层可跳过且不计失败 | `smoke_test.py:819-825,1115-1116` |
| **高** | 单一令牌可自批自执，"人工确认"的制衡不成立（无职责分离） | `security.py:59,81`、`main.py:2825` |
| **中** | 无脱敏：审计原样落命令/日志/告警原文，且无保留期 | `main.py:2294` |
| **中** | 批准后无执行时限；审批记录 `result_ok` 与真实结果不符 | `approvals.py:306-327`、`main.py:2977-2978` |
| **中** | 读路径可为 `/var/log` 下任意文件（注释声明只允许 `.log`） | `policy.py:235-240,315-318` |
| **低** | `_MIN_SOURCED_DIGITS` 重复定义（`specialists.py:646`、`:686`） | 静态检查 |
| **低** | `check_numbers` 子串匹配会误报（"96" 命中 "1960"），作者已自述 | `specialists.py:705` |

---

## 5. 下一步

范围已确认：**M1（I-0 自检语义 + I-1 测试与 CI 门禁）+ M2（I-2 事件与通知 + I-3 告警聚合）**。
方案见 [`03-design-m1.md`](03-design-m1.md) 与 [`02-roadmap.md`](02-roadmap.md)。

---

## 6. M1 实施记录：写测试时抓出来的新缺陷

这一节是"补测试"的直接产出——**下面每一条都是先有了测试才暴露的**，
它们此前都不在任何人的清单上（包括本报告的第一版）。

| # | 缺陷 | 证据 | 后果 | 处理 |
|---|---|---|---|---|
| N1 | **审批单时间戳只写盘、不回填给调用方** | `approvals.py` 的 `_append` 里 `event = {"ts": _now(), **event}` 改的是**局部变量**，紧跟其后的 `self._apply(ev)` 拿到的 `ev` 里没有 `ts` | 同一进程内新建/批准/执行的审批单，`created_at` / `approved_at` / `consumed_at` 全是 `null`；`list()` 按 `created_at or ""` 排序时同批单子顺序随机。**一重启就"自愈"**（盘上有 ts），所以极难复现 | 已修（`_append` 改 `setdefault` 原地回填），并加用例：`test_in_memory_record_matches_what_was_persisted` 把"内存与盘必须一致"钉住 |
| N2 | **`/metrics/summary` 会因 trace 文件放在项目外而 500** | `main.py` 里 `obs.TRACE_PATH.relative_to(PROJECT_ROOT)` 直接抛 `ValueError` | 挂载卷 / 自定义部署路径下，**只读成本看板直接挂掉**；也让"把落盘位置改到临时目录"的测试根本没法写 | 已修（新增 `_display_path`：取不到相对路径就退回绝对路径） |
| N3 | **lint 债：`app/` + `scripts/` 共 66 处** | `ruff check app scripts --statistics`：B904 18 / F541 16 / F841 10 / F401 8 / B905 6 / E731 3 / B007 3 / E741 1 / B025 1 | 一次性大扫除会把真实改动淹没在 diff 里 | M1 **只对 `tests/` 强制 lint**；M2 新增的三个子包（`app/alerting`、`app/incident`、`app/notify`）**零新增债**，且顺手清掉了 `main.py` 里 7 处历史 E402（app+scripts 从 69 降到 66）。这条债在此显式登记，按里程碑逐目录纳入（**登记了才不许它悄悄消失**） |
| N4 | **`requirements-dev.txt` 首版带中文注释 → `pip` 直接崩** | `UnicodeDecodeError: 'gbk' codec can't decode byte 0x89` | pip 按系统区域编码（中文 Windows 上是 GBK）读 requirements 文件 | 已改为纯 ASCII。**这正是 `requirements.txt:7-9` 早就写明的规矩** —— 说明"写在注释里的约定"挡不住人，得靠 CI 兜 |

**方法学教训（也记下来）**：测试"公开清单里的路径都是真实路由"时，一开始用 OpenAPI 的
`paths` 判断路由是否存在 —— 结果误报，因为 `/`、`/try`、`/dashboard`、`/settings`
是用 `include_in_schema=False` 注册的（它们只是不含数据的空壳），**OpenAPI 里看不到它们**。
"路由存在"和"文档里有它"是两件事。改用 `app.routes` 后正确。
**误报和漏报一样有害：一条会假红的测试，很快就会被所有人忽略。**

### 6.1 门禁自证：证明这些绿灯真的拦得住东西

新加了一堆测试之后，最该回答的问题是：**它拦得住吗？**
于是做了注入实验 —— 把项目**历史上真实出现过**的缺陷注回去，看测试是否变红：

| 轮次 | 注入内容 | 结果 | 结论 |
|---|---|---|---|
| 第 1 轮 | 只把 `"/"` 加回 `_INSPECT_DIRS` | ❌ **测试没红** | **我的测试不够强**：当前实现是 `target == d or target.startswith(d + "/")`，`"/"` 拼出来是 `"//"`，对 `/root` 不成立 —— 当年的修复是**结构性的**，不是"删掉那一行" |
| 第 2 轮 | `"/"` 回表 **且** 前缀比较退回 `d.rstrip("/") + "/"` | ✅ 变红，诊断精准：`assert 'allow' == 'deny'`（`du -sh /root`） | 门禁**确实守住了这条不变量**，并且守的是结构而不是字面值 |

还原后用 `git checkout` 复位并复跑：绿。

> 第 1 轮的失败比第 2 轮的成功更有价值：它说明**"加测试"和"加了能拦住的测试"是两件事**。
> 如果只跑一次"注入 → 变红"就收工，得到的结论会是错的（因为第一次注入的点根本不是缺陷所在）。
> **门禁自证必须注入"真实的失效机制"，而不是"看起来像失效的东西"。**

### 6.2 由新测试抓出的其余缺陷（已登记，按里程碑处理）

| 编号 | 缺陷 | 证据 | 后果 | 处理 |
|---|---|---|---|---|
| **D1** | `validate_intent` 没挡住"不是对象"的输入 | `main.py` 的 `validate_intent` 第一句 `if field not in data` | 模型返回裸 `null`（合法 JSON）→ `TypeError` → 穿过 `parse_intent` / `/parse` / `/webhook/alert` 的 `except IntentParseFailed` → **HTTP 500，且不写 `parse_failed` 审计、不重试** —— 凌晨那条告警静默消失，日志里什么都查不到 | **已在 M1 修复**；用例从"固化崩溃"翻转成"重试到上限后报 `IntentParseFailed`"，并补了"下一轮改对则恢复成功" |
| D2 | 整轮都是模型层错误时 `last_output` 恒为 `null` | `parse_intent` | `/parse` 返回 `last_output: null`，排障时看不到任何模型输出线索 | 登记，M4（可观测）一并处理 |
| D3 | `validate_intent` 不校验 `action` 取值域 | 同上 | `action="delete_everything"` 判合格、不重试（被 `policy.risk_of_action` 的"认不出一律 medium"兜住，不会直接放行） | 登记，M3 |
| D4 | `validate_intent` 不校验 `host` 的事实合法性 | 同上 | `host="生产数据库"`、`12345` 都判合格；真正拦住它的是 SSH 目标白名单。**这是"结构合法 vs 事实合法"的边界，收紧前要先算清误拒代价** | 登记（有意的宽松，需专门设计） |
| D5 | `embedder.describe()` 没有机器可读的"降级"标记 | `app/rag/embedder.py` | `local-hash` 与真语义后端在结构上**完全平等**：索引元数据与 `/rag/stats` 都不会提示"当前不是语义检索"。实证：`local-hash` 下「磁盘占满」vs「空间不够」余弦相似度恰为 **0.0**，而向量路又没有分数阈值（必凑满 top_k） | 登记，M5（I-7 真语义检索） |
| **D6** | `{"alerts": []}` 与 `{}` 产出同一条 `UnknownAlert` | `main.py` 的 `normalize_alerts` | 一个**明确表示"本次无告警"的空批次**会被当成真实告警，开了 `ALERT_AUTO_DIAGNOSE` 就白跑一轮模型 | 登记，**M2 必须一起修**（正是告警聚合要碰的地方） |
| D7 | `normalize_alerts` 不做类型检查 | 同上 | 传列表 → `AttributeError`；HTTP 路径进不来（body 声明为 `dict`），内部调用会踩 | 登记，低优先 |
| **C-①** | **父链成环时整笔钱静默消失，对账却显示"完美"** | `costs.py` 叶子集合筛选，配合 `_gap` 在 `total<=0` 时返回 `0.0` | 两条 span 互为父子、其上 1e9 未命中 token（真实约 ¥1000）→ 报告 `cost_cny=0.0`、两个维度皆空、`gap=0.0`、**无 warning**；同样这笔钱放在合法结构上是 2000.0。**"对账差 0"这个本该代表"数据可信"的信号，在这里变成了"钱被吞掉"的掩护** | 登记，M4。建议修法：叶子集合为空但有 span 时，退回按顶层 span 归因 |
| C-② | 兜底价"宁可高估"的说法不成立 | `costs.py` 的 `_FALLBACK` 与 Flash 表是同一对象 | 空闲时段未知模型报 1.0 而实际 4.5（高峰 2.0 vs 9.0）—— 对**更贵**的未知模型是**低估**，与注释相反；低估会误导优化决策 | 登记，M4 |

### 6.3 一次真实事故：新测试污染了对外指标（已非破坏性清理）

一条漏加隔离装置的用例把测试记录写进了**真实**的 `logs/traces.jsonl`。
后果不是"日志多了几行"，而是**对外成本指标被压低了一个数量级**：

| | `/metrics/summary`（limit=50） |
|---|---|
| 清理前 | `runs=50`、`cost_cny=`**0.0774** |
| 清理后 | `runs=50`、`cost_cny=`**0.9253** |

原因：那些假 trace 全是 `source=live`、成本≈0，把"最近 50 条真实运行"的窗口整个挤掉了。

清理方式**非破坏性**：按项目自己的机制，把 126 条测试遗留记录标记为 `source=selftest`
（`/metrics/summary` 会排除、默认的 live 列表视图也不再显示），
**文件行数不变（1374 行）**，备份留在 `logs/traces.jsonl.bak.retag.*`。

> **教训**：这个项目"每次运行都留 trace"的设计本身很好，
> 但它意味着"测试写脏数据"的代价不只是噪声 ——
> **它会把对外宣称的可观测性数字变成假的**，而且假得很安静（数字看起来照样合理）。
> 这也解释了为什么 conftest 里的隔离装置和禁网守卫是必需品，而不是讲究。

---

## 7. M2 实施记录：事件与通知落地后，闭环补上了哪几环

### 7.1 缺口清单里被关掉的部分

| 原编号 | 内容 | 处理 |
|---|---|---|
| **E1** | 无任何出站通知（"凌晨三点没人被叫醒"） | **已实现**：通用 webhook + 钉钉 + 飞书三个渠道，含加签、去重、重试退避、失败留痕 |
| **E2** | 无事件（Incident）实体 | **已实现**：`app/incident/`（追加日志 + 折叠），状态机 `open→ack→resolved`、`resolved→reopened`，4 个接口 |
| **E3** | 去重是精确键 + 固定窗口，且 `_ALERT_LAST_SEEN` 无界增长 | **已实现**：`app/alerting/aggregator.py` 按「主机+服务」聚合，带 TTL 与容量上限（2000 条互异告警压测后表大小 ≤ 上限） |
| **E4** | 告警入口同步阻塞 | **已留开关**：`ALERT_ASYNC`（默认 0 = 保持现有同步形态，兼容优先；置 1 走后台） |
| **D6** | `{"alerts": []}` 被当成一条凭空捏造的 `UnknownAlert` | **已修**（归一化层按"有没有 alerts 键"分支；入口返回 `received: 0`，不建事件、不调模型） |
| **D12** | 审批无到期提醒 | **已实现**：审批单创建即推通知（含过期时间与批准入口） |
| 部分 **D10** | 无脱敏 | **已实现**：出站正文默认过 `mask()`（`NOTIFY_MASK` 默认 **1**），凭据/`password=`/长哈希被折叠，主机名与路径原样保留 |

### 7.2 验收证据（全部可复跑、零成本）

| 门禁 | 结果 |
|---|---|
| `pytest` | **509 条全绿**（M1 为 359 条；M2 新增 150 条） |
| `ruff check tests` | All checks passed |
| `mypy` | Success: no issues found in 13 source files |
| `scripts/security_check.py` | 21 + 2 项全通 |
| `scripts/mcp_check.py` | 9/9 |
| `scripts/eval_baseline.py` | 无退化 |
| **50 条同源告警 → 1 个事件 + 1 次诊断 + 1 条通知** | `tests/test_m2_wiring.py` 端到端断言（诊断引擎打桩计数） |
| **没有配置通知时零出站** | 同一文件；未配置时行为与加这一层之前一致 |
| 外发内容脱敏 | 固定密钥/固定时间戳的签名确定性用例 + `mask()` 正反例 |

### 7.3 M2 期间发现的新问题（已登记）

| 编号 | 问题 | 证据 | 处理 |
|---|---|---|---|
| M2-1 | **`MAX_RECORDS = 500` 是死常量**：注释声称"内存里最多保留多少条（防止日志无限增长拖慢启动）"，全仓零引用 → 审批单内存实际**无界增长** | `app/sandbox/approvals.py:81` | 登记。incident 侧没有复制这个写法（并明确写出"真正的有界性要求在去重表上"） |
| M2-2 | 审批记录 `result_ok` 在执行**之前**写入（原 C4） | `main.py` 的 `execute_approval` | 未改顺序（有意取舍），但**通知把真值送到人手上**：执行结果通知里带 `ok`/退出码/错误 |
| M2-3 | 通知层初版把 `NOTIFY_MASK` 默认设为关 | `app/notify/base.py` | **复核时翻成默认开**：出站内容离开信任边界，两个方向的代价不对称（少一段文字 vs 凭据进第三方聊天记录），用例同步翻转并写明理由 |
| M2-4 | 通知 dispatcher 初版**持锁发送**（一条卡死的 webhook 会堵死所有渠道） | `app/notify/dispatcher.py` | 实现方自查后改为"锁只保护去重状态"，代价是同毫秒两条同源可能都发 —— 宁可多发一条，不可整层被堵死 |

### 7.4 M2 之后的闭环

```
告警 ──► 抑制(维护窗口) ──► 聚合(主机+服务) ──► 频控
                                  │
                                  ├─► 事件(incident) ──► 通知值班人（钉钉/飞书/自建 webhook）
                                  │        │
                                  │        └─► 时间线：谁认领、谁结单、诊断结论、通知成败
                                  └─► 诊断（可选，默认关）──► 结论写回事件 ──► 再通知
                                        └─► 写操作 ──► 审批单 ──► 通知 ──► 人批准 ──► 沙箱执行 ──► 结果通知
```

**仍然没做的**（按路线图留给后续里程碑）：处置剧本与自动回滚（I-9）、复盘知识回流（I-6）、
工单系统集成、真机钉钉/飞书机器人实测（需要用户提供机器人地址）、`ALERT_ASYNC=1` 的后台任务实现（开关已留，同步路径已验收）。
