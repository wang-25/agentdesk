# 会话记忆与案例记忆（M5b）

> 这份文档回答三件事：**跨会话记住什么**、**解决过的故障怎么回流到诊断里**，
> 以及**边界在哪**（尤其是"案例绝不能当成事实"这条）。

---

## 1. 为什么要有这一层

`/chat` 原来是**完全无状态**的：`ChatRequest` 只有 `question` 一个字段，
跨请求什么都不记得。

```
用户：web-01 的磁盘满了怎么处理
模型：（答）
用户：那 inode 呢              ← 模型不知道"那"指的是 web-01 的磁盘
```

同时 M2 已经建立了"事件"体系，里面沉淀了"什么告警 → 怎么诊断 → 怎么处置 → 结论"，
但这些数据**只用于展示，从未回流到诊断过程**。

M5b 补上这两块：

| 模块 | 解决什么 | 风险 |
|---|---|---|
| `app/memory/sessions.py` | 同一个会话里的追问能接上 | 错了 → 答非所问。**吵，但看得见** |
| `app/memory/cases.py` | 解决过的故障变成下次的参考 | 错了 → 把"上次的答案"当"这次的事实"。**安静，且危险** |

两者的存储范式相同（追加 JSONL + 启动折叠 + 上限如实报告），
但**风险等级完全不同**，文档后面会分别说。

```
告警 ──► 事件 ──结单──► 案例库（logs/cases.jsonl）
                          │
                          │  只作为【带标注的历史参考】
                          ▼
下一次问答 ──► POST /chat（带 session_id）──► 提示词 ──► 模型
                    ▲
会话历史 ───────────┘（logs/sessions.jsonl，同样带标注）
```

---

## 2. 会话记忆

### 2.1 怎么用

`POST /chat` 多了一个**可选**字段 `session_id`：

```bash
# 第一次：没有历史可注入
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"web-01 磁盘满了怎么处理","session_id":"shift-2026-1002-a"}'
# {"answer":"先 df -h 看是数据分区还是 inode 用满……"}

# 第二次：会自动带上上一轮（带时间、带"仅供参考"标注）
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"那 inode 呢","session_id":"shift-2026-1002-a"}'
```

★ **不传 `session_id` 时行为与改动前逐字段一致**：不读会话、不写会话、
响应仍然只有 `answer` 一个字段。有回归用例守着
（`test_chat_without_session_id_keeps_the_old_behaviour`）。

### 2.2 `session_id` 的规则

只允许 `[A-Za-z0-9_-]`，长度 1–64。**非法一律 400**（`/chat` 的请求体、`/sessions/{id}` 都是）。

为什么必须校验，两条不同的理由：

1. **防日志污染**：`session_id` 会原样进 JSONL 与审计。放行换行符，
   一行日志就变成两行 —— 折叠时多出一条谁也不认识的记录，
   而且**盘上的行数与内存里的轮数从此对不上**。
2. **防路径穿越**：`/`、`\`、`.`、`..` 一律拒绝（`sessions.jsonl` 是文件，不是目录）。

**它不解决的问题**：会话没有"归属者"概念 —— 猜到一个 id 就能读到那个会话的历史。
单机本地部署（`AUTH_ENABLED=0`）下这不是新增的风险面；
**部署到公网前应把 `session_id` 与外部身份绑定**（见第 6 节）。

### 2.3 注入给模型的上下文长什么样

```
【同一会话的历史对话 · 仅供参考】以下是会话 shift-2026-1002-a 最近 2 轮的记录（旧 → 新）。
它们只说明**当时**问了什么、答了什么，不代表现在的机器仍是那个状态。

[1] 时间：2026-10-02T03:14:09
    用户：web-01 磁盘满了怎么处理
    助手：先 df -h 看是数据分区还是 inode 用满……
[2] 时间：2026-10-02T03:15:41
    用户：那 inode 呢
    助手：……

以上是历史对话，仅供参考。回答当前问题前，请用当前机器的实际数据重新判断，
不要把上面助手的说法当成事实。
```

历史对话**也是要标注的**：它和案例在机制上是同一类东西 ——
都是"上一次的说法"，都不能被当成"这一次的事实"。

### 2.4 上限与淘汰

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `SESSIONS_LOG` | `logs/sessions.jsonl` | 落盘位置 |
| `SESSION_MAX_TURNS` | `10` | **每会话**保留最近多少轮 |
| `SESSION_MAX_SESSIONS` | `200` | **全局**最多多少会话，超了按**最久未使用**淘汰 |

★★ **上限被触发时一定如实报告**，不许静默丢历史。分三处落实：

1. `append()` 的返回值里有 `evicted`：

```json
{"session_id":"s-1","turns":10,"trimmed":true,
 "evicted":{"turns":2,"sessions":1,
            "reason":["每会话上限 10 轮，丢了最旧的 2 轮",
                      "全局会话上限 200，淘汰最久未使用的 s-old（4 轮）"]}}
```

2. 丢这件事**写进日志**（`trim` / `evict` 事件）——
   重启折叠之后内存与盘上一致，不会出现"盘上 12 行、内存里 10 轮"这种对不上的状态。
3. `sweep(max_age_seconds)` 返回**删掉的轮数**（不是 `True`/`False`）。

为什么这条这么重要：一个"最多保留 10 轮"的常量如果只是**静默**截断，
现象是"它好像忘了我前面说的话"—— 没有现场、没有日志、复现不了。
本项目在 `approvals.MAX_RECORDS` 上已经吃过一次"说了不做的常量"的亏。

### 2.5 接口

| 接口 | 作用 | 审计事件 |
|---|---|---|
| `GET /sessions` | 列表（最近用过的在前）+ 计数 + **当前上限** | —— |
| `GET /sessions/{id}` | 看一个会话记住了什么（**默认不带正文**，`include_turns=true` 才给） | `session.view` |
| `DELETE /sessions/{id}` | 清空，返回**删掉了几轮** | `session.clear` |
| `POST /chat`（带 `session_id`） | 注入历史 + 追加一轮 | `session.turn` |

```bash
curl -s http://127.0.0.1:8000/sessions | python -m json.tool
curl -s "http://127.0.0.1:8000/sessions/shift-2026-1002-a?include_turns=true"
curl -s -X DELETE http://127.0.0.1:8000/sessions/shift-2026-1002-a
```

`GET /sessions` 的 `counts` 里带上限是**故意**的：看板上"12 个会话 / 34 轮"
如果不带上限，没人知道离淘汰还有多远。

---

## 3. 案例记忆

### 3.1 案例是怎么来的

事件被 **`POST /incidents/{id}/resolve`** 结单时自动沉淀成一条案例。

★ **为什么接在这一处**（三选一，理由写在 `app/main.py` 的注释里）：

- 接在 `IncidentStore.resolve()` 里 → 存储层反向依赖记忆层。事件存储是核心审计物，
  它多一个依赖，每个 import 事件的场景都跟着多一份代价；而且存储层调记忆层失败会
  **影响结单本身**。分层上"存储不认识记忆"更干净。
- 接在告警链路里 → 那条链路根本不 resolve，等于没接。
- **接在接口这一处** → 这是全项目**唯一**能把事件从 `ack` 推到 `resolved` 的地方，
  所以接一处就覆盖了全部结单路径，存储层一行都不用改。

案例字段（8 个）：

```json
{"ts":"2026-10-02T03:20:11",
 "incident_id":"inc-34509ba4",
 "fingerprint":"",
 "service":"nginx",
 "symptom":"nginx 5xx 比例超过 5%",
 "conclusion":"清理 /var/spool/clientmqueue 后恢复",
 "actions":"清理 /var/spool/clientmqueue 后恢复；已通知值班",
 "elapsed_ms":750000}
```

| 字段 | 来自 | 说明 |
|---|---|---|
| `symptom` | 事件的 `summary` | 当时的现象 |
| `conclusion` | 结单说明 `note` | **人**写的结论 |
| `actions` | 结单说明 → 时间线上 `notified`/`diagnosed` → 诊断摘要 | 优先级就是"人的结论 > 动作记录 > 模型的猜测" |
| `elapsed_ms` | `resolved_at - created_at` | 算不出来就是 `null` —— **不编一个 0**（0 会被读成"瞬间修好了"） |

同一个事件**只留一条**案例（复发再结单是**更新**，以最后一次为准）：
不去重的话，一个反复出问题的服务会堆出十几条几乎一样的记录，
把真正不同的历史挤出上限 —— 而"这东西老是坏"恰恰是最该被看见的信号。

### 3.2 相似度：零成本、确定性

用 **`app/rag/store.py` 的 `tokenize`**（复用，不另写一套分词）分词后算 token 重叠：

- **字段权重**：服务名 3.0 > 症状/结论 1.0 > 处置动作 0.5。
  服务名最关键（同服务的经验复用价值最高）；处置动作权重最低 ——
  "重启服务"到处都是，**撞词不等于相似**。
- **IDF**：`log(1 + N/(1+出现次数))`。一个"全是 nginx"的案例库里，
  `nginx` 不提供区分度；没有 IDF 的话，随便问什么都返回那三条最早的 nginx 案例。
- **归一化**到 `(0, 1]`，相同时**新的优先**（更可能对现在成立），
  再按 `incident_id` 兜底，保证**同样的输入永远返回同样的三条**。

**没有把握就返回空**，绝不硬凑三条 —— 硬凑的后果比"没找到"严重得多：
模型会拿到三条毫不相干的历史，却因为它们被摆在"相似案例"的位置上而当成参考。

### 3.3 注入给模型的文本（★ 安全边界）

`render_context()` 的输出**必须**同时做到四件事：

① 开场就说清这是**历史**案例；② 每条**带时间**；③ 结尾要求**用当前机器的实际数据重新判断**；
④ 说明**当初的处理未必适用于现在**。

```
【历史案例参考 · 不是本次的事实】
以下是过去处理过的相似故障记录。它们**只说明当时发生了什么**，不代表现在的情况与它们相同。

案例 1｜时间：2026-10-02T03:20:11｜服务：nginx｜来源事件：inc-34509ba4
  - 当时的现象：nginx 5xx 比例超过 5%
  - 当时的结论：清理 /var/spool/clientmqueue 后恢复
  - 当时的处置：清理 /var/spool/clientmqueue 后恢复；已通知值班
  - 与当前问题的字面相似度：0.61（只按关键词重叠计算，不代表结论正确）

────────
以上都是**历史**案例。当初的处理未必适用于现在，请用当前机器的实际数据重新判断，
不要直接照搬上面的结论或命令。
```

★ 这段文案**不是措辞问题，是安全边界**，有专门的用例守着它：
`tests/test_memory.py::test_render_context_demands_fresh_evidence`
（断言 `历史案例参考` / `不是本次的事实` / `当初的处理未必适用于现在` /
`请用当前机器的实际数据重新判断` / `不要直接照搬` 全部存在）。
**改文案改到那句警告消失，用例就要红。**

#### 三道防线（为什么只写在一处不够）

| 位置 | 做法 | 防的是什么 |
|---|---|---|
| 数据层 | `find_similar()` 返回的**每一行**都带 `note`（"历史案例（时间），仅供参照；当初的处理未必适用于现在的机器"） | 调用方绕过 `render_context` 自己拼提示词时，标注还在 |
| 注入层 | `render_context()` 的**开头与结尾各有一段警告** | 长上下文被截断时，留下的不会是没有警告的那一半 |
| 来源层 | 案例必须带 `incident_id`，**没有 id 的事件拒绝记录** | 案例永远能回到原事件看完整时间线 —— "上次到底是不是这么修好的"有据可查 |

### 3.4 上限与去重

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `CASES_LOG` | `logs/cases.jsonl` | 落盘位置 |
| `CASES_MAX` | `500` | 案例条数上限，超了丢最旧的 |

`record_case()` 的返回值如实报告：

```json
{"recorded":"inc-34509ba4","case":{...},"evicted":1,
 "reason":"新案例"}
```

`reason` 是 `新案例` 或 `同一事件再次结单，以最后一次为准`。
`evicted > 0` 表示这次顺手丢了最旧的一条（并在日志里留 `evict` 事件）。

---

## 4. 环境变量一览

```env
# 会话记忆
#SESSIONS_LOG=logs/sessions.jsonl      # 落盘位置
#SESSION_MAX_TURNS=10                  # 每会话保留最近多少轮
#SESSION_MAX_SESSIONS=200              # 全局最多多少会话（LRU 淘汰）

# 案例记忆
#CASES_LOG=logs/cases.jsonl
#CASES_MAX=500
```

配置写错（`SESSION_MAX_TURNS=abc`）→ **退回默认档**，不让每次问答都 500。
`SESSION_MAX_TURNS=0` 会被夹到 1：要关掉会话记忆，正确做法是**不传 `session_id`**。

---

## 5. 排障

| 现象 | 先看哪 |
|---|---|
| "它怎么忘了我前面说的话" | `GET /sessions/{id}` 看还有几轮；`logs/sessions.jsonl` 里的 `trim` 事件告诉你丢了几轮、为什么 |
| "我的会话怎么没了" | `GET /sessions` 的 `counts`（sessions vs max_sessions）；日志里的 `evict` 事件写明淘汰了谁、丢了几轮 |
| "模型怎么照着上次的答案答" | `logs/audit.jsonl` 的 `session.turn`；重放一次 `/chat` 看注入的 `system` 消息里有没有那两段警告 |
| "案例库怎么一直不涨" | `logs/audit.jsonl` 的 `memory.case_recorded` / `memory.case_record_failed`；`GET /incidents` 看有没有事件被 resolve 过 |
| "案例明明记了却检索不到" | 相似度**只认字面命中**（见下一节）。换个说法（同义改述）可能就命中不了 |

---

## 6. 已知边界（不藏）

| 边界 | 说明与代价 |
|---|---|
| **相似度抓不住同义改述** | "磁盘满了" vs "空间不足"命中不了。这是为"零成本、确定性、不调模型"付的代价。要更强就得引入 embedding（那就每次多花钱、多一个索引要维护）——本里程碑明确不做 |
| **会话没有归属者** | 知道 `session_id` 就能读那个会话。单机 / 内网部署下可接受；**公网部署前必须把 `session_id` 与身份绑定**（或由网关改写），否则它会变成一个"可猜测的对话读取口子" |
| **记忆不参与权限判定** | 会话与案例只影响**提示词文本**，不改变任何策略、审批或工具放行判定。模型看到历史 ≠ 它被授权做历史里做过的事 |
| **多进程下不共享内存态** | 与限流层同一个取舍（单进程 `--workers 1` 下准确）。写之前会按 `(mtime_ns, size)` 重新折叠，所以不会拿旧内存态去做淘汰判定；但两个进程同时写同一个会话时，历史顺序可能交错 |
| **日志只增不减** | `clear` / `trim` / `evict` 都是**追加事件**（不是重写文件），所以文件会一直长。留给运维侧轮转（与 `logs/*.jsonl` 同一套办法） |
| **`elapsed_ms` 用结单时间算** | 复发过的事件，`resolved_at` 是最后一次结单时间，所以"持续时长"会偏大。宁可偏大也不要编一个精确的假数 |
| **相似度分数没有绝对含义** | `0.61` 是"相对这个案例库的归一化重叠"，不是"61% 可能就是这个故障"。它只用来**排序** |
| **案例检索在每次带 session 的 `/chat` 都会跑一次** | 500 条以内是毫秒级（纯字符串比较，不调模型）；库很大时应先看 `counts` 再考虑调小 `CASES_MAX` |

---

## 7. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -o addopts="" -q
```

94 条用例，分四类：

| 类别 | 代表用例 |
|---|---|
| 记住 / 忘掉 / 重启后一致 | `test_the_log_is_rebuilt_after_restart`、`test_clear_returns_how_many_turns_were_dropped` |
| 上限**如实报告** | `test_per_session_limit_keeps_the_newest_and_reports_the_loss`、`test_global_session_limit_evicts_the_least_recently_used`、`test_a_trim_is_written_to_the_log_not_silently_dropped` |
| ★ 案例只作参考 | `test_render_context_demands_fresh_evidence`、`test_find_similar_marks_every_row_as_history`、`test_find_similar_returns_nothing_instead_of_guessing` |
| ★ 默认档行为不变 | `test_chat_without_session_id_keeps_the_old_behaviour`、`test_chat_rejects_an_illegal_session_id_before_calling_the_model` |

全部零成本：不联网（`conftest` 的 autouse 守卫）、不调模型（`chat_stub`）、
**绝不写真实 `logs/`**（两个 store 都指向 `tmp_path`）。
