# 监控与运维（M4）

> 这篇讲**怎么监控 AgentDesk 自己**。它前面几个里程碑学会了监控别人（查日志、看磁盘、读容器），
> 而它自己长期以来是"坏了没人知道"：没有指标端点、健康检查不探依赖、
> JSONL 无限增长且查询全量读文件。M4 把这块补上了。

---

## 1. 三个接口，各管一件事

| 接口 | 鉴权 | 用途 | 特点 |
|---|---|---|---|
| `GET /health` | **公开** | 容器编排 / 负载均衡的存活探测 | 秒回、零依赖、不碰磁盘 —— 只回答"进程还活着吗" |
| `GET /healthz` | 要令牌 | 人排障 | 真去碰依赖，逐项明细；坏哪一项**点名**；`200` / `503` |
| `GET /metrics` | 要令牌 | Prometheus 抓取 | 手写 Prometheus 文本格式，**零新增依赖** |

**为什么分开**：`/health` 会被每分钟调用很多次，它必须便宜且不泄露内部信息；
`/healthz` 会读磁盘、折叠一遍存储，明细里有路径与计数 —— 那是给运维看的，不是给公网看的。

### 1.1 `/healthz` 检查了什么

| 检查项 | 成本 | 不通过时意味着 |
|---|---|---|
| `logs_writable` | 0 | 审计 / trace / 审批**全都在丢** |
| `disk_space` | 0 | 剩余 < 50MB，马上要写不进去了 |
| `index_loadable` | 0 | 检索不可用（会明确说"索引不存在，需要 build"） |
| `approvals_readable` | 0 | 审批状态读不出来（顺带触发一次过期折叠） |
| `incidents_readable` | 0 | 事件存储读不出来 |
| `notify_config` | 0 | 点名了渠道却没配 URL（**没配通知不算故障**） |
| `model_recent` | 0 | 只报不判：最近一次成功的模型调用距今多久（读 trace 痕迹） |
| `model_probe` | **花钱** | 只有 `HEALTHZ_PROBE_MODEL=1` 才真调一次模型 |

**"只报不判"是一个刻意设计**：长时间没有模型调用可能只是没人用，
不等于服务有病。把它写进明细能省排障时间，但不该让健康检查报 503。

**默认不真调模型**：探针会被监控系统每分钟调一次，真调模型就成了"监控本身在烧钱"。
所以默认改为看痕迹（最近一次成功调用距今多久，从 trace 里读，零成本）。

### 1.2 `/metrics` 指标清单

| 指标 | 类型 | 标签 | 回答什么问题 |
|---|---|---|---|
| `agentdesk_http_requests_total` | counter | `path, method, status` | 谁在调、错多少 |
| `agentdesk_http_request_duration_seconds` | histogram | `path` | 哪一步慢（P95） |
| `agentdesk_security_rejections_total` | counter | `reason` | 是被拦了还是真挂了 |
| `agentdesk_llm_calls_total` | counter | `ok` | 模型层抖不抖 |
| `agentdesk_llm_tokens_total` | counter | `kind`（prompt/completion/cache_hit） | token 花在哪 |
| `agentdesk_llm_cost_cny_total` | counter | `model` | 钱花在**哪个模型**上 |
| `agentdesk_alerts_received_total` | counter | `decision` | 告警进来后各判定的分布（降噪有没有生效） |
| `agentdesk_incidents` | gauge | `status` | 现在有几个未结故障 |
| `agentdesk_notify_total` | counter | `channel, ok` | 通知通不通 |
| `agentdesk_sandbox_executions_total` | counter | `backend, ok` | 写操作在哪执行、成没成 |
| `agentdesk_state_scope` | gauge | `kind, scope` | **如实标出"限流/额度/去重是进程内状态"** |
| `agentdesk_alert_dedup_entries` | gauge | — | 告警去重表现在多大 |
| `agentdesk_trace_write_failures` | gauge | — | 观测**写入**失败次数（以前是静默吞掉的） |
| `agentdesk_process_uptime_seconds` | gauge | — | 重启过没有 |
| `agentdesk_metrics_series` / `_dropped_total` | gauge / counter | `reason` | 指标层自己的基数与丢弃 |

### 1.3 接 Grafana / Prometheus

```yaml
# prometheus.yml
scrape_configs:
  - job_name: agentdesk
    metrics_path: /metrics
    authorization:
      credentials: <你的 AGENT_TOKEN>
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

值得直接抄的三条告警规则：

```yaml
- alert: AgentDeskNotifyFailing      # 通知发不出去（最容易被忽略的故障）
  expr: rate(agentdesk_notify_total{ok="false"}[10m]) > 0
- alert: AgentDeskTraceWriteFailing  # 观测在丢数据
  expr: increase(agentdesk_trace_write_failures[10m]) > 0
- alert: AgentDeskIncidentsOpen       # 有未结故障堆积
  expr: agentdesk_incidents{status="open"} > 5
```

---

## 2. 日志

### 2.1 两种格式

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_FORMAT` | `text` | `text` = 人类可读（**默认，观感与改动前一致**）；`json` = 一行一个 JSON |
| `LOG_LEVEL` | `INFO` | 写错值会回退 INFO 并打一条警告，不会让服务起不来 |
| `LOG_JSON_EXTRA` | `1` | json 模式下是否带额外字段 |

`json` 模式每行的键：`ts`（带时区偏移）、`level`、`logger`、`msg`、`request_id`（有则带）、
`exc_info`（异常时）。**中文不转义**（`ensure_ascii=False`），日志是给人看的。

**为什么默认 `text`**：改默认输出格式会让所有已有的 `docker logs` 阅读习惯突变。
需要接 Loki / 采集器时，一个环境变量切换即可。

### 2.2 请求 ID

每个请求分配一个 8 位短 ID：
- 响应头 `X-Request-ID`
- 该请求写下的**审计行**里带 `request_id` 字段
- 该请求期间的日志行带 `[req=xxxxxxxx]`

于是"这次请求的日志"和"它写了哪几条审计"能直接对上 —— 没有它，两条线只能靠时间猜。
请求之外的审计（后台任务、启动期）**不带这个字段**：不补空串，
免得"没有请求上下文"和"请求 ID 恰好是空"混成一种样子。

### 2.3 JSONL 轮转

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_MAX_BYTES` | `10485760`（10MB） | 超过就轮转 |
| `LOG_KEEP` | `5` | 保留几份 |

**只有 `traces.jsonl` 与 `audit.jsonl` 轮转。** 这不是漏了，是**不能**：

| 文件 | 读取方式 | 能否轮转 |
|---|---|---|
| `traces.jsonl` | 只从尾部读最近 N 条 | ✅ 可以（读侧会回读上一份） |
| `audit.jsonl` | 只从尾部读最近 N 条 | ✅ 同上 |
| `approvals.jsonl` | 启动时**整份折叠成状态** | ❌ **绝不能** |
| `incidents.jsonl` | 启动时**整份折叠成状态** | ❌ 绝不能 |

把审批单日志的一部分改名挪走，等于**静默丢掉那些单据的状态** ——
一张已批准的单子会变回"不存在"，重放保护直接失效。
这两个文件的归档是**运维动作**（停服务、整体移走），不是程序自动行为。
程序能做的是把大小报出来（`/metrics`）、超阈值时告警。

**保留份数满了怎么办**：最旧的一份被**移进 `logs/archive/`**，不是删掉。
"保留 5 份"躲不开"第 6 份怎么办"，而删掉最旧的 = 程序替人销毁审计数据 ——
这个项目不做这种事。磁盘仍会涨，但**涨得看得见**，且没有任何东西被悄悄删掉。

**轮转之后读得到吗**：读侧会回读 `.1`、`.2`…
不会出现"刚轮转完，看板显示最近没有任何记录"（那比不轮转更糟）。

---

## 3. 关于"状态是进程内的"这件事

限流计数、每日额度、告警去重表**都在进程内存里**。单进程（`--workers 1`）下完全准确；
**多 worker 会让每一个额度各算各的**（审计把它列为阻断级风险）。

M4 的处理是 **可见 + 可拦 + 留缝**，而不是引入 Redis（那属于过度设计：要加依赖、
要在 2 核机器上再跑一个服务）。具体：

- **可见**：`/healthz` 与 `/metrics` 都如实报出 `state_scope=process`
- **留缝**：所有进程内状态集中在 `app/security.py` 与 `app/main.py` 的几处，
  docstring 里写明"换成 Redis 时只需要替换这一处"
- **不预先抽象接口**：当前只有一个实现，抽接口只会多一层没人用的间接

---

## 4. 自检与排障

```powershell
# 指标端点能不能出数据
curl -H "X-API-Key: $env:AGENT_TOKEN" http://127.0.0.1:8000/metrics

# 依赖深度检查（坏哪一项会点名）
curl -H "X-API-Key: $env:AGENT_TOKEN" http://127.0.0.1:8000/healthz

# 通知到底发得出去吗（把业务错误码打出来）
.venv\Scripts\python.exe scripts\notify_check.py
```

**"HTTP 200 不等于发送成功"** —— 钉钉会在 200 的响应体里回 `errcode: 310000`（关键词不匹配），
飞书回 `code: 19021`（签名校验失败）。`notify_check.py` 专治这种假象，
`agentdesk_notify_total{ok="false"}` 让它在看板上也看得见。

---

## 5. 已知边界

- 容器回收、`killpg` 分支的验证情况见 [`redev/01-audit.md`](redev/01-audit.md) §8.4
- 指标**不支持** summary 与 exemplar（前者客户端分位数不可聚合，后者属 OpenMetrics；
  文本 0.0.4 不要求）。trace 关联请在查询层按时间窗 join `traces.jsonl`
- 没有真实 Prometheus 服务端做端到端抓取验证（环境禁网），
  格式合规性由 `tests/test_metrics.py` 里一个**独立实现**的规范校验器保证
  （它自己也有一组反例用例，确保它真的会红）
- 指标基数上限为每指标 200 个标签组合，超出会丢弃并计入
  `agentdesk_metrics_dropped_total{reason="cardinality"}`
