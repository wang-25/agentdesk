# M4 实施方案（I-5 自监控与状态外置）

> 对应路线图 [`02-roadmap.md`](02-roadmap.md) 的 M4；解决审计报告 **D5–D9、A6、A7** 与阻断级的 **A2（单进程状态）**。
>
> **状态：待用户确认后开工**（Rules：先评估后动手）。

---

## 0. 这一里程碑要达成的一句话目标

> **让 Agent 自己可被监控** —— 现在它能监控别人（查日志、看磁盘），
> 但**它自己坏了没人知道**：没有 `/metrics`、`/health` 不探依赖、
> 没有结构化日志、JSONL 无限增长且查询全量读文件。

审计里那句话就是这条的由来：`docs/tech-stack.md` 自认"监控告警 = 打印日志"。
一个把可观测性做成本项目卖点的系统，自己却不可观测 —— 这是个自相矛盾。

---

## 1. 本次复查确认的现状（都是刚实测的，不是转述）

| 事实 | 证据 |
|---|---|
| **没有 `/metrics`** | `requirements.txt` 无 `prometheus_client`（实测**未安装**，引入它=第 10 个运行时依赖，会直接推翻 README 的"9 个"与 CI 里那条守卫） |
| **`/health` 不探依赖** | `main.py:2140` 只回 `status/service/version/security` —— 模型挂、索引坏、磁盘满时它照样报 ok |
| **`/audit` 全量读文件** | `main.py:2172` `AUDIT_LOG.read_text()` —— 文件多大就读多大 |
| **`write_audit` 无锁** | `main.py:166`（对比：`tracer` 有 `Lock`、`approvals` 有 `RLock`） |
| **JSONL 无自动轮转** | 全仓无 `RotatingFileHandler`/logrotate；`logs/` 里两份 `.bak`（723KB / 888KB）是**人工复制**的痕迹 |
| **compose 的日志轮转管不到 JSONL** | `docker-compose.yml:67` 把 `./logs` 挂进容器，而 `:89-93` 的 `json-file 10m×3` 只作用于容器 stdout |
| **trace 文件已 918KB** | `read_recent` 只读尾部 2MB，且**任何异常都 `return []`**（`tracer.py:445`）→ 出问题时静默返回"没有数据" |
| **状态全在进程内** | `security.py:101-104`（限流/额度/拒绝计数）、`main.py` 的聚合表与频控、`approvals`/`incident` 的内存折叠 |
| 单进程是硬要求 | `Dockerfile:99` `--workers 1`，注释里写了理由 |

---

## 2. 设计决策（先定方向，再谈实现）

### 2.1 `/metrics`：手写 Prometheus 文本，**不引入 `prometheus_client`**

| | 手写文本格式（建议） | 引入 `prometheus_client` |
|---|---|---|
| 依赖 | **0**（纯 stdlib 拼字符串） | 第 10 个运行时依赖 |
| 能表达 | counter / gauge / histogram（自己算桶） | 同样的东西 + 自带多进程模式 |
| 我们真正需要的 | 计数 + 几个延迟分位 + 少量 gauge | 一样的 |
| 代价 | 要自己保证转义与 `# HELP/# TYPE` 格式正确（约 30 行） | 无 |
| 与项目风格 | 一致（"直接依赖仅 9 个，其余手写"是它的卖点） | 破例 |

**结论：手写。** 并且**用测试保证格式合法**（转义、TYPE 行、`le` 桶单调、
`NaN/Inf` 不出现）——这样"手写"不是偷懒，而是把风险显式管住。

### 2.2 状态外置：**不做 Redis**，而是"可见 + 可拦 + 留缝"

审计把"多 worker 下额度×N"列为阻断级。但真上 Redis 要加依赖、要在用户的 2 核 2G 机器上再跑一个服务 ——
**那是过度设计**（Rules 里明确禁止）。真正该做的是三件事：

1. **可见**：`/health`、`/healthz`、`/metrics` 都如实报出"这些状态是**进程内**的"（`state_scope="process"`），
   并把量级暴露成 gauge。看板上一眼就能看出"这台是单进程在算账"。
2. **可拦**：启动时若检测到"看起来是多 worker 部署"（`WEB_CONCURRENCY`/`UVICORN_WORKERS` > 1，
   或显式 `ALLOW_MULTIWORKER=1` 缺失时检测到多个 worker 各写一份 state 标记文件），
   **拒绝启动并给出解释**（fail-closed：宁可起不来，也不要"以为有额度上限、其实没有"）。
3. **留缝**：把限流/额度/去重的存储收敛到一个明确的模块边界（`app/security.py` 内部集中，
   并在 docstring 写明"换成 Redis 时只需要替换这一处"），**不预先抽象接口**。

### 2.3 `/healthz`：深度探依赖，但**默认不花钱**

| 检查项 | 成本 | 默认 |
|---|---|---|
| JSONL 目录可写（写一个 `.healthz` 探针文件） | 0 | ✅ |
| RAG 索引可加载（进程内缓存，不重建） | 0 | ✅ |
| 审批/事件存储可读（折叠一遍） | 0 | ✅ |
| 通知配置可解析（不发请求） | 0 | ✅ |
| **模型连通性** | 一次调用 ≈ ¥0.001 | ❌ 默认只用"最近一次成功调用距今多久"推断；`HEALTHZ_PROBE_MODEL=1` 才真打 |

`/health` **保持原样**（部署脚本与 compose HEALTHCHECK 都依赖它，改字段＝破坏兼容）；
`/healthz` 是新增的深度版，返回 `200`（健康）/`503`（有依赖不可用）+ 逐项明细。

### 2.4 结构化日志：默认保持文本，`LOG_FORMAT=json` 一键开

当前**没有任何 logging 配置**（全项目只有两个 `getLogger`），所以加配置本身就是"从无到有"。
但把默认输出改成 JSON 会让已有的 `docker logs` 阅读习惯突变，所以：

- `LOG_FORMAT=text`（默认）：与现在观感一致，但补上**等级与请求 ID**
- `LOG_FORMAT=json`：一行一个 JSON，带 `ts/level/logger/msg/request_id/path/status/elapsed_ms`
- **请求 ID** 用 `contextvar` 贯穿（中间件生成 → 日志与 audit 记录都带上），
  这样"一次请求的日志 + 它的审计行"能对上

### 2.5 JSONL 轮转：自写按大小轮转，保留 N 份

**★ 先纠正方案初稿里的一个错误：不是"四个文件都轮转"。**

写方案时我写的是 traces / audit / approvals / incidents 四份都加轮转 ——
**那是错的，而且错得很危险**。理由：

| 文件 | 读取方式 | 能不能轮转 |
|---|---|---|
| `traces.jsonl` | 只从**尾部**读最近 N 条（`read_recent`） | ✅ 可以，且读侧要能**回读上一份** |
| `audit.jsonl` | 只从**尾部**读最近 N 条（`/audit`、MCP 的 recent_audit） | ✅ 同上 |
| `approvals.jsonl` | **启动时整份折叠成状态**（`_load` → `_apply`） | ❌ **绝不能**：把一部分改名挪走 = 静默丢掉那些审批单的状态（已批准的可能变回不存在，重放保护直接失效） |
| `incidents.jsonl` | 同上（**整份折叠**） | ❌ 绝不能，同理 |

所以实际做法是：

- **只有 traces 与 audit 轮转**（它们是纯追加的事件流，读侧只关心尾部）
- approvals / incidents **不轮转**，改为：暴露"文件大小"指标 + 超过阈值时打告警日志 +
  文档写明"要归档请在服务停止时整体移走"（状态日志的归档是运维动作，不是程序自动行为）
- 超过 `LOG_MAX_BYTES`（默认 10MB）改名为 `.1`、`.2`…，保留 `LOG_KEEP`（默认 5）份
- **不删历史**：轮转只是改名，旧文件仍在（审计物不能被"为了省空间"悄悄删掉）
- **读侧要跟上**：`read_recent`/`read_audit` 在"当前文件不够 N 条"时要**回读上一份**，
  否则刚轮转完会看到"最近没有任何记录"（比不轮转更糟）
- **`read_recent` 不再吞异常**：截断/读失败要如实报出来（现在是 `return []`，
  看板静默显示"没有数据"——**静默的错答案比报错危险**）

### 2.6 `/audit` 分页

`read_text()` 全量读 → 改成"从尾部按块读 + 支持 `offset` 游标"，
返回 `{"items", "total", "next_offset"}`。**`total` 语义要说清**：轮转后它是"当前文件内"的总数，
不是全历史（否则又是一个"看起来对其实不对"的数字）。

---

## 3. 指标清单（先定这个，再写代码）

| 指标 | 类型 | 标签 | 回答什么问题 |
|---|---|---|---|
| `agentdesk_http_requests_total` | counter | `path, method, status` | 谁在调、错多少 |
| `agentdesk_http_request_duration_seconds` | histogram | `path` | 哪一步慢（P95） |
| `agentdesk_security_rejections_total` | counter | `reason`（auth/rate/quota） | 是被拦了还是真挂了 |
| `agentdesk_llm_tokens_total` | counter | `kind`（prompt/completion/cache_hit） | token 花在哪 |
| `agentdesk_llm_cost_cny_total` | counter | `model` | 钱花在哪个模型 |
| `agentdesk_llm_calls_total` | counter | `ok` | 模型层抖动 |
| `agentdesk_alerts_received_total` | counter | `decision` | 告警进来后各判定的分布 |
| `agentdesk_incidents` | gauge | `status` | 现在有几个未结故障 |
| `agentdesk_notify_total` | counter | `channel, ok` | 通知通不通 |
| `agentdesk_sandbox_executions_total` | counter | `backend, ok` | 写操作在哪执行、成败 |
| `agentdesk_state_scope` | gauge | `kind` | **如实标出状态是进程内**（值恒为 1） |
| `agentdesk_process_uptime_seconds` | gauge | — | 重启过没有 |

刻意的取舍：**不暴露条数很多的标签**（如 `question`、`trace_id`）—— 那会炸掉基数。

---

## 4. 新增/改动文件

```
app/observability/
├── metrics.py        （新）极简指标注册表 + Prometheus 文本渲染
├── logsetup.py       （新）logging 配置：text/json 两种格式 + 请求 ID contextvar
├── jsonl.py          （新）追加 + 按大小轮转 + **跨文件回读**（traces/audit 共用）
app/selfcheck.py      （新）/healthz 的依赖探针（可单测，不依赖启动服务）
app/observability/tracer.py   轮转 + 不再吞异常 + 回读上一份
app/security.py             拒绝计数 -> 指标；状态作用域可见
app/main.py                 中间件埋点、/metrics、/healthz、/audit 分页、write_audit 加锁
app/llm.py                  token/成本/成功失败埋点（3 行）
app/notify/dispatcher.py    投递结果埋点（2 行）
tests/test_metrics.py       （新）格式合法性 + 埋点正确性 + 基数守卫
tests/test_jsonl_log.py     （新）轮转 + 跨文件回读 + 不删历史 + 状态日志拒轮转
tests/test_healthz.py       （新）依赖探针的失败路径（每项都能报出"哪里坏了"）
tests/test_logsetup.py      （新）两种格式 + 请求 ID 贯穿 + 不改默认观感
```

---

## 5. 验收标准

1. `curl /metrics` 返回**合法的 Prometheus 文本**：有 `# HELP`/`# TYPE`、桶单调、
   无 `NaN/Inf`，且用 `prometheus_client` 的解析器**离线解析一遍**（见 §6 决策 1 的说明）
2. `/healthz` 在**人为弄坏一项依赖**时返回 503 并指明是**哪一项**（磁盘不可写 / 索引坏 / 存储坏）
3. `/health` 的响应字段与改动前**逐字段一致**（兼容性用例）
4. `/audit` 不再全量读：造一个 10MB 的 audit 文件，接口耗时与文件大小**解耦**（有阈值断言）
5. JSONL 轮转：超过阈值后 `traces.jsonl` → `.1`，且**旧文件仍在**；
   轮转后立刻查"最近 N 条"**仍能跨文件回读**（不会出现"最近没有记录"）
6. `write_audit` 并发安全：多线程写 N 条 → 行数正好 N、无交错坏行
7. 日志：`LOG_FORMAT=json` 时每行都是合法 JSON 且带 `request_id`；
   `text`（默认）观感与改动前一致
8. **未配置任何新环境变量时，所有既有接口的响应字段与改动前一致**（回归用例保证）

---

## 6. 需要你拍板的四件事

| # | 问题 | 选项 |
|---|---|---|
| 1 | `/metrics` 怎么实现 | **A（建议）零依赖手写 Prometheus 文本**（用 CI 里的解析器校验格式） · B 引入 `prometheus_client`（第 10 个依赖，但省掉手写风险） |
| 2 | 状态外置做到哪一档 | **A（建议）可见 + 启动可拦 + 文档留缝，不引入 Redis** · B 额外抽一个可插拔 `StateBackend` 接口（多一层抽象，为 Redis 留缝） · C 真做 Redis 后端（加依赖 + 多一个服务） |
| 3 | 日志默认格式 | **A（建议）默认 `text`（观感不变）+ `LOG_FORMAT=json` 一键开** · B 默认 `json`（更适合接 Loki/采集，但改变现有 `docker logs` 观感） |
| 4 | JSONL 轮转阈值与份数 | **A（建议）10MB × 5 份**（约等于当前 trace 文件的 10 倍余量） · B 更保守（5MB × 3） · C 不轮转，只加"文件过大"告警指标 |

> 说明：选项 1 的 A 里，"用 CI 解析器校验"我会用**纯 stdlib 写一个最小解析断言**
> （因为不装 `prometheus_client` 就用不了它的解析器）——即按规范校验
> `HELP/TYPE/标签转义/桶单调`这几条硬要求。这一点在方案里先讲明，免得验收时口径不一致。
