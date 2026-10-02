# M2 实施方案（I-2 事件与出站通知 + I-3 告警聚合与降噪）

> 对应路线图 [`02-roadmap.md`](02-roadmap.md) 的 M2；解决审计报告的 **E1/E2/E3/E4/E5（部分）/D6/D10/D12**。
>
> **状态：待用户确认后开工**（Rules：先评估后动手）。

---

## 0. 这一里程碑要达成的一句话目标

> **让凌晨三点的诊断结论真的能叫醒人，并且让 500 条告警收敛成 1 个故障。**

现状（已在审计中实证）：`/webhook/alert` 诊断完只把结果放进 HTTP 响应和 `audit.jsonl`
（`main.py` 的 `webhook_alert` 末尾 `return` + `write_audit`），**全项目没有任何出站通知出口**
（`dingtalk|feishu|slack|wecom|smtp` 在仓库里零命中）。所以"无人值守"目前只做到一半：
它会自己起来诊断，**但诊断完没有任何人会被叫醒**。

---

## 1. 设计主线（四条，先立规矩）

| # | 原则 | 具体含义 |
|---|---|---|
| 1 | **不改既有契约** | `/webhook/alert` 的请求体与响应字段保持兼容；新增字段只增不改；通知未配置时行为与现在**完全一致** |
| 2 | **不引入新运行时依赖** | 事件与通知全部用标准库 + httpx（已依赖）。存储沿用项目自己的**追加日志 + 折叠**模式（与 `approvals.jsonl`/`audit.jsonl` 一致），**不引入数据库** |
| 3 | **通知失败不许拖垮诊断** | 通知是**旁路**：入队 → 异步重试 → 失败记 audit 与 span。**"推不出去"绝不能变成"诊断结果丢了"** |
| 4 | **默认关闭，可一键回退** | `ALERT_AGGREGATE=0` 回到现有的精确去重；`NOTIFY_CHANNELS` 为空则完全不出站 |

---

## 2. I-2 · 事件（Incident）与出站通知（3–5 人日）

### 2.1 新增模块与文件

```
app/incident/
├── __init__.py
├── model.py        事件模型：状态机 + 字段定义 + 状态迁移合法性
└── store.py        存储：追加日志 + 折叠（沿用 approvals.py 的模式与锁策略）
app/notify/
├── __init__.py
├── base.py         适配器接口 + NotifyResult + 脱敏钩子
├── webhook.py      通用 webhook（POST JSON）
├── dingtalk.py     钉钉自定义机器人（支持加签）
├── feishu.py       飞书自定义机器人（支持签名）
└── dispatcher.py   选路 / 重试退避 / 失败记录 / 限速（同一事件不重复轰炸）
```

### 2.2 事件模型

| 字段 | 说明 |
|---|---|
| `id` | `inc-xxxxxxxx`（与 `ap-` 前缀区分） |
| `status` | `open` → `ack` → `resolved`，允许 `reopened`（`resolved` 后同源告警再进来） |
| `severity` | 取**成员告警中最高**的一档（不是平均，也不是第一条） |
| `host` / `service` | 聚合键的可见部分 |
| `source` | `alert` / `manual` / `agent` |
| `linked_alerts` | 成员告警列表（去重后的指纹 + 首次/末次时间 + 条数） |
| `diagnosis` | 通过校验的诊断结论摘要（引用与数字原样保留） |
| `timeline` | 追加式事件流：`created` / `alert_linked` / `diagnosed` / `ack` / `resolved` / `reopened` / `notified` |
| `owner` | `ack` 时必填（与审批单"批准必须填 `by`"同一条原则：**每次状态变化都要有人负责**） |

### 2.3 新增接口（4 个，全部进 OpenAPI）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/incidents` | 列表（`status`/`limit` 过滤，新的在前） |
| GET | `/incidents/{id}` | 单个事件详情（含完整时间线） |
| POST | `/incidents/{id}/ack` | 认领（必须填 `by`） |
| POST | `/incidents/{id}/resolve` | 结单（必须填 `by`，可选 `note`） |

### 2.4 接线点（三处，都在现有代码的边界上）

| 触发点 | 行为 |
|---|---|
| `POST /webhook/alert` 诊断完成 | 按聚合键建立/并入事件 → 写入 `diagnosed` 与结论 → **推通知** |
| 审批单创建（`policy` 判 `needs_approval`） | 推通知："有一条写操作等你批准"，带审批链接与命令原文（经脱敏） |
| 审批单执行完成 | 更新事件时间线 + 推通知（含 `ok` 与退出码，**不谎报成功**） |

### 2.5 配置面（全部有默认值，默认=行为不变）

```env
# 通知：留空 = 不出站（与现在完全一致）
NOTIFY_CHANNELS=                    # webhook,dingtalk,feishu 任选，可多选
NOTIFY_WEBHOOK_URL=
NOTIFY_DINGTALK_URL=
NOTIFY_DINGTALK_SECRET=             # 加签用，可留空
NOTIFY_FEISHU_URL=
NOTIFY_FEISHU_SECRET=
NOTIFY_TIMEOUT=8
NOTIFY_MAX_ATTEMPTS=3
NOTIFY_MIN_INTERVAL=60              # 同一事件 60s 内不重复推（防轰炸）
NOTIFY_MASK=1                       # 出站内容脱敏（默认开）
```

### 2.6 验收标准

1. 本地起一个 mock 通知接收端，模拟告警后**确实收到**诊断结论（含事件 id、等级、主机、结论摘要）
2. 待审批单产生时**确实收到**提醒；批准并执行后**确实收到**结果（`ok=false` 时也必须如实推）
3. `NOTIFY_CHANNELS` 留空时：`/webhook/alert` 与 `/approvals/*` 的响应字段与改动前**逐字段一致**（回归用例保证）
4. 通知端返回 500 / 超时：主流程**不受影响**，重试 3 次后记 `notify.failed` 审计 + span，接口仍返回诊断结果
5. 事件时间线完整可查；`ack`/`resolve` 不填 `by` 一律 400
6. 出站内容不含未脱敏的命令原文与日志片段（`NOTIFY_MASK=1` 时）

---

## 3. I-3 · 告警聚合与降噪（3–4 人日）

### 3.1 现状与问题（都已在审计中定位）

- 去重键是 `(alertname, host)` **精确匹配** + 固定 600s 窗口（`main.py` 的 `_ALERT_LAST_SEEN`）
- `_ALERT_LAST_SEEN` 是**无上限 dict、无清理** → 长期运行内存单调增长
- `webhook_alert` 是**同步阻塞**的：开 `ALERT_AUTO_DIAGNOSE=1` 时，每条告警在请求内跑完整诊断（6–20s），
  Alertmanager 侧很容易超时重推
- `{"alerts": []}` 与 `{}` 产出同一条 `UnknownAlert`（**D6**）→ 一个"本次无告警"的空批次也会白跑一轮模型

### 3.2 做法

| 项 | 做法 |
|---|---|
| 聚合键 | 指纹 = `alertname + host + service + 关键 labels`（`labels` 里的 `job`/`instance`/`severity` 参与），窗口内归并到**同一个 incident** |
| 去重存储 | 滑动窗口 + **定期清理**（消掉无界增长）；保留 `ALERT_DEDUP_SECONDS` 语义 |
| 空批次（D6） | `alerts` 键存在但为空 → `{"received": 0, "message": "空告警批次，未触发任何诊断"}`，**不建事件、不调模型** |
| 异步入口 | `ALERT_ASYNC=1` 时改用后台任务执行诊断，接口**立即**返回 `{"accepted": N}`（含幂等键，防 Alertmanager 重推重复计费） |
| 维护窗口 / 抑制 | **最小实现**：一个 `data/alert_silences.json`（`alertname` 前缀 + `host` 前缀 + `until`），命中则记 `alert.silenced` 审计并跳过。**不做规则引擎** |
| 一键回退 | `ALERT_AGGREGATE=0` → 完全回到现有的精确去重实现 |

### 3.3 验收标准

1. 一次推 50 条同源告警（同一 host+service，不同 alertname）→ **聚成 1 个事件**，且
   `ALERT_AUTO_DIAGNOSE=1` 时**只跑 1 次诊断**（用假模型计数证明，零成本可复跑）
2. 连续推 10 分钟（脚本模拟）→ 去重表与事件表**内存不增长**（有界）
3. `ALERT_ASYNC=1` 时 webhook 响应 < 200ms；重复推同一幂等键**不重复计费**
4. 维护窗口内的告警被抑制且留审计；窗口过期后自动恢复
5. `ALERT_AGGREGATE=0` 时行为与改动前**逐字段一致**（回归用例）
6. `{"alerts": []}` 不产生事件、不调用模型（D6 修复用例）

---

## 4. 测试计划（沿用 M1 的装置，全部零成本）

| 文件 | 覆盖 |
|---|---|
| `tests/test_incident.py` | 状态机全迁移、`ack/resolve` 必须填 `by`、终态与 `reopened`、severity 取最高、时间线完整性、重启折叠一致、坏行容忍 |
| `tests/test_notify.py` | 适配器签名/HMAC 正确性（**固定密钥 + 固定时间戳**算期望值）、重试与退避、失败不阻塞、脱敏生效、`NOTIFY_CHANNELS` 为空时零出站 |
| `tests/test_alert_aggregate.py` | 指纹聚合、窗口边界、内存有界、幂等键、空批次（D6）、维护窗口、`ALERT_AGGREGATE=0` 回退 |
| `tests/test_api_smoke.py`（扩展） | 4 个新接口的存在性与鉴权边界（纳入现有的参数化清单） |

**mock 通知接收端**：用 `TestClient` 起一个最小的本地 HTTP 接收器（回环，符合禁网守卫），
或直接对 `dispatcher` 打桩。两种都要有：前者验证"真的发出了 HTTP"，后者验证重试逻辑。

---

## 5. 提交计划

| # | commit | 内容 |
|---|---|---|
| 1 | `feat(incident): 事件模型与追加日志存储 + 4 个查询/状态接口` | I-2 地基 |
| 2 | `feat(notify): 通知适配器（通用 webhook / 钉钉 / 飞书）+ 重试与脱敏` | I-2 出口 |
| 3 | `feat(alert): 告警诊断完成即建/并事件并推通知（含审批提醒）` | I-2 接线 |
| 4 | `fix(alert): 空告警批次不再被当成真实告警（D6）` | 顺带修 |
| 5 | `feat(alert): 指纹聚合 + 有界去重表 + 异步入口 + 维护窗口` | I-3 |
| 6 | `docs: M2 使用说明（通知怎么配、事件怎么看）+ 尽调报告同步` | 文档 |

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 出站通知把内部信息发到外部 IM | 默认关闭；`NOTIFY_MASK=1` 默认脱敏；通知内容**只含结论摘要与事件 id**，日志原文默认不出站 |
| 聚合改错会把不同故障并成一个 | 指纹只在 `host + service` 相同时才聚合；`ALERT_AGGREGATE=0` 一键回退；用 50 条同源 + 20 条异源混合用例守住"不该合的没合" |
| 异步化后丢了"响应里带结论"的便利 | 异步是**可选开关**（默认同步）；响应里返回事件 id，结论仍可在 `/incidents/{id}` 查到 |
| 钉钉/飞书加签算错（时间戳/密钥拼接顺序） | 用固定密钥 + 固定时间戳的**确定性单测**，并且真机验证一次（需要你提供一个机器人地址，或我留成可选步骤） |
| 新增 55+ 条用例拖慢节奏 | 只覆盖"静默出错"的地方：状态机、签名、重试、聚合边界、回退兼容 |

---

## 7. 本里程碑不做什么

- **不做**处置剧本与自动回滚（I-9，风险最高，按路线图放在最后）
- **不做**复盘知识回流与长期记忆（I-6）
- **不做**工单系统集成（需要外部系统，属项目外依赖）
- **不做**告警关联的拓扑推断（需要资产/依赖数据源，本项目没有；只用可用的 labels 做聚类）
- **不做**规则引擎、不做多租户、不引入数据库

---

## 8. 已确认的三件事（2026-10-02）

| # | 问题 | 确认结果 |
|---|---|---|
| 1 | 通知渠道做到哪一档 | **通用 webhook + 钉钉 + 飞书** |
| 2 | 异步入口默认值 | **默认同步**（`ALERT_ASYNC=0`，兼容优先；置 1 的后台任务实现属后续增量） |
| 3 | 维护窗口 / 抑制是否纳入 M2 | **纳入**（最小实现：`alert_silences.json`，不做规则引擎） |

实施结果与验收证据见 [`01-audit.md`](01-audit.md) §7；使用说明见 [`../incident-notify.md`](../incident-notify.md)。
