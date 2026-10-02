# -*- coding: utf-8 -*-
"""
事件模型：状态机 + 等级归并 + 标识
============================================================
这个文件里全是**纯函数和常量**：不读磁盘、不取当前时间、不持有状态。
理由不是"好看"，是三条具体的工程需要：

    1. **判定只有一份**。接口层（`/incidents/{id}/ack` 能不能点）、
       存储层（这条迁移合不合法）、看板（这个事件该显示成什么颜色）
       用的是同一个 `can_transition` / `severity_of`。
       判定散成三份，就会出现"前端允许、后端报错"这种低级错位。

    2. **零成本可验证**。全状态机是 4 个状态 × 7 个事件 = 28 种组合，
       一条参数化用例就能穷举完，不需要起服务、不需要模型、不需要磁盘。

    3. **时间戳只在一个地方生成**（`store._now()`）。
       模型不碰时钟，就不会出现"同一个动作记下两个不同时间"的问题。

【状态机：为什么是这四个状态】

    open ──ack──> ack ──resolved──> resolved ──reopened──> reopened
                    ↑                                          │
                    └────────────── ack ───────────────────────┘

**为什么 `open` 不能直接 `resolved`（有意的取舍）**

从运维现实看,"自愈类告警"确实存在：告警自己恢复了，没人碰过它。
允许 `open → resolved` 会让"无人认领就被关掉"成为一条**合法路径** ——
而事故复盘里最怕的那句话恰恰是"这个故障谁处理的？没人"。
`owner` 是 `ack` 时写下的，所以"先认领再结单"这个顺序保证了
**每一个被关掉的事件都有名字**。

代价是自愈场景要多一次 `ack(by="auto")`。这一步摩擦是故意留下的：
自动化可以把 `by` 填成 `auto`，但那个字段必须存在且有人填 ——
"系统自己关的"和"不知道谁关的"是两件完全不同的事。

**为什么 `resolved` 不是绝对终态（与审批单故意不同）**

审批单的 `consumed` 不可逆：一次批准被执行两遍是安全漏洞。
事件不是授权，事件是**对现实故障的记账**；故障会复发，复发必须记在
同一个事件上（否则"这东西老是坏"这个最该被看见的信号会被拆成
十个互不相干的单次故障）。所以 `resolved → reopened` 是一条合法的边。

但 `resolved → ack` 仍然非法：那等于**跳过"复发"这一事实**去处理，
时间线上会看不出中间发生过什么。要重新处理，先 `reopen`。

**"同源"这个条件不在这个函数里**

`can_transition("resolved", "reopened")` 只看状态，不看告警是不是同源 ——
纯函数拿不到告警。同源性由调用方保证：聚合路径是按 `host + service`
把告警路由到事件上的，能走到 `reopen(iid)` 就说明它本来就是同一个源。
把"路由"和"判定"分开，是为了让判定能被穷举测试。
"""

import secrets

# ============================================================
# 状态
# ============================================================
STATUS_OPEN = "open"            # 事件已建立，还没有人认领
STATUS_ACK = "ack"              # 已认领，有人在处理（owner 此时必须有值）
STATUS_RESOLVED = "resolved"    # 已结单
STATUS_REOPENED = "reopened"    # 结单后同源告警又响了 —— 故障复发，重新打开

# ============================================================
# 等级
# ============================================================
# 只认三档。**认不出的取值一律按 warning 处理**（见 severity_of）：
# 既不能落到 info（未知 ≠ 不重要，一条乱写级别的告警被静默降级，
# 就是一次漏报），也不能升到 critical（否则拼错一个单词「critcal」
# 就能把全站拉响，而人对误报的忍耐是有限的）。
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

_UNKNOWN_RANK = SEVERITY_ORDER["warning"]
_NAME_BY_RANK = {rank: name for name, rank in SEVERITY_ORDER.items()}
DEFAULT_SEVERITY = "warning"

# ============================================================
# 时间线上的事件名
# ============================================================
# 前三个是**状态迁移**（会进 history），后面四个是**资料性事件**（只进 timeline）。
EVENT_CREATED = "created"
EVENT_ACK = "ack"
EVENT_RESOLVED = "resolved"
EVENT_REOPENED = "reopened"
EVENT_ALERT_LINKED = "alert_linked"
EVENT_DIAGNOSED = "diagnosed"
EVENT_NOTIFIED = "notified"

#: 会改变 `status` 的事件。其余事件只往时间线上追加"发生过什么"。
TRANSITION_EVENTS = (EVENT_ACK, EVENT_RESOLVED, EVENT_REOPENED)


def new_id() -> str:
    """`inc-` + 8 位十六进制。

    前缀必须和审批单的 `ap-` 区分开：值班台上、日志里、审计里
    两种 id 混在一起时，人一眼要能看出"这是一张审批单还是一个事件"。
    8 位（32 bit）足够：这是**给人看的短 id**，不是安全标识，
    撞了也有 `IncidentStore._new_id_locked` 兜着。
    """
    return "inc-" + secrets.token_hex(4)


def severity_rank(severity: str) -> int:
    """等级的排序值。认不出的一律按 warning 的档位算。

    对外的比较入口 —— store 的"等级只升不降"就是用它实现的。
    """
    return SEVERITY_ORDER.get(str(severity or "").strip().lower(), _UNKNOWN_RANK)


def severity_of(alerts: list[dict]) -> str:
    """取成员告警里**最高**的一档。

    ★ 为什么是"最高"而不是"平均"、也不是"第一条"：

      平均：一条 critical 混在 20 条 info 里，平均下来是 info。
            这不是降噪，这是**漏报**，而且是静默的那种。
      第一条：告警的到达顺序是网络决定的，跟严重程度无关；
            取第一条等于"谁先到谁说了算"。
      最高：代价是可能被一条误报拉高等级。但这个代价是**可见的**
            （事件显示成 critical，人会去看，然后发现是误报），
            而漏报是不可见的（关掉了，没人知道曾经着过火）。
            运维里"宁可见到误报"是共识。

    空列表（或全是垃圾成员）→ `warning`：既不是 info（"没有告警"不等于
    "不重要"，这个事件已经被人建出来了），也不是 critical（没证据）。
    """
    if not alerts:
        return DEFAULT_SEVERITY
    ranks = [severity_rank(a.get("severity")) for a in alerts if isinstance(a, dict)]
    if not ranks:
        return DEFAULT_SEVERITY
    return _NAME_BY_RANK[max(ranks)]


def normalize_severity(severity: str) -> str:
    """把任意输入压成 `info` / `warning` / `critical` 三者之一。

    存进记录里的 `severity` 必须是可枚举的，否则看板按等级分色、
    按等级排序时都得再判一次"这个值认不认识"。归一化一次，下游全省。
    """
    return _NAME_BY_RANK[severity_rank(severity)]


#: `event -> 可以从哪些当前状态发起它`。
#: `created` 是特例（只允许"还没有记录"时），单独在 `can_transition` 里判。
_ALLOWED_FROM = {
    EVENT_ACK: (STATUS_OPEN, STATUS_REOPENED),
    EVENT_RESOLVED: (STATUS_ACK,),
    EVENT_REOPENED: (STATUS_RESOLVED,),
}


def can_transition(current: str, event: str) -> bool:
    """这次迁移合不合法。**状态机的唯一判定入口。**

    合法集合（少一条都是有意的，逐条理由见模块开头的长注释）：

        ""/None + created      → 只允许在"这个 id 还没有记录"时创建。
                                否则一个重复的 created 就能把已结单的事件
                                重置回 open —— 等于凭空抹掉一段历史
        open + ack             → 认领
        reopened + ack         → 复发后重新认领
        ack + resolved         → 结单
        resolved + reopened    → 同源告警又响了（"同源"由调用方保证）

    ★ `open + resolved` 是**故意**不允许的：见模块开头"为什么 open 不能直接
      resolved"。自愈场景请先 `ack(by="auto")`。

    `current` 传 `""` / `None` 表示"还没有这条记录"。任何认不出的状态值
    （比如日志被手工改过）都返回 False —— 判定出错时**默认拒绝**，
    和 sandbox 的 fail-closed 是同一条原则。
    """
    cur = str(current or "").strip()
    if event == EVENT_CREATED:
        return not cur
    return cur in _ALLOWED_FROM.get(event, ())


class IncidentError(Exception):
    """事件流程错误。消息会直接返回给调用方（HTTP 400 / 404）。"""


# 供测试与调用方使用的状态全集。`counts()` 的自洽性检查、
# 看板的固定列都基于它 —— 漏加一个状态就会让某个事件在界面上凭空消失。
ALL_STATUSES = (STATUS_OPEN, STATUS_ACK, STATUS_RESOLVED, STATUS_REOPENED)
