# -*- coding: utf-8 -*-
"""
事件存储：追加 JSONL + 启动时折叠（fold）
============================================================
本文件只解决一个问题：**把上一条注释里那些事件动作，变成重启之后依然存在的事实。**

【持久化：为什么还是追加日志，而不是数据库】

这一条完全沿用 `app/sandbox/approvals.py` 的理由，并且在这里更成立：

    - **不会写坏**：追加是原子的。进程被 kill 在写第 3 行中间，前 2 行仍然完整。
      一次故障处理到一半时进程崩了，恰恰是最需要日志完整的时刻。
    - **可审计**：事件时间线**本身就是产品要展示的东西**（`/incidents/{id}` 返回
      `timeline`）。用 UPDATE 存"当前状态"的话，时间线得另建一张表 ——
      而那张表会跟主表不一致。这里时间线就是日志，日志就是时间线。
    - **可重建**：当前状态 = 把日志从头折叠一遍。删掉内存，重启后一模一样。

【一条硬规矩：内存态永远是"盘上事件的函数"】

所有写操作**必须**是 `_append(ev)` → `_apply(ev)` 两步，且 `_apply` 拿到的
必须是**同一个 dict 对象**（`_append` 会往它身上原地回填 `ts`）。

    先落盘再改内存：反过来的话，内存说"已结单"而盘上没有，重启就退回去了。
    同一个对象：反过来的话，内存里少一个 `ts` —— 这正是 approvals 踩过的坑（见下）。

★ 踩过的坑（approvals.py 的 `_append`，此处已经避开）：

    原先写的是  `event = {"ts": _now(), **event}`  —— 只改了局部变量。
    于是紧跟其后的 `self._apply(ev)` 拿到的 `ev` 里**没有 ts**，
    `created_at` / `acked_at` / `resolved_at` 在**当前进程内**全成了 None；
    而重启后从盘上折叠回来又是好的（盘上有 ts）。
    表现：接口返回 `created_at: null`、`list()` 按 `created_at or ""` 排序时
    顺序随机、一重启就"自愈" —— 这是最难复现的一类 bug。

    修法就是 `event.setdefault("ts", _now())`：**原地回填**，
    调用方那个 dict 和写进盘的 JSON 拿到的是同一个值。
    **同一份数据有两个来源（内存 / 盘）时，两个来源必须拿到同一个值。**

【并发：为什么必须加锁】

`/incidents/{id}/ack`、`/resolve` 是同步接口，FastAPI 会把它们丢进线程池 ——
它们**真的会并发执行**。两个值班的人同时点"认领"，或者"认领"和"结单"撞在一起：

    不加锁：两边都读到 status == open → 两边都通过 `can_transition` 检查
            → 两条 ack 事件落盘 → 事件有了两个 owner，时间线上出现
              "先张三认领、后李四认领"，而两个人都以为自己接下了这个故障。

这类 bug 本地几乎复现不出来（要压测才看得到），上线之后才炸。
**加锁的成本是一行，不加的成本是一次线上事故。**

用 RLock 而不是 Lock，因为 `create()` 内部会调 `link_alert()`、
`ack()` 内部会调 `get()` —— 可重入锁让"公开方法互相调用"不必小心翼翼。

【规模：这个文件现在不封顶，是有意的】

`approvals.py` 里有个 `MAX_RECORDS = 500` 的常量（注释写着"防止日志无限增长"）
**但全文没有任何地方用它** —— 内存实际是无界增长的。这里不重复这个错误：
不定义一个骗人的上限。

事件表暂时**不截断**，理由是：事件是审计物，`get()` 必须能查到历史事件；
悄悄丢掉最老的记录，会让某一天突然"查不到上个月的故障"。
真正的有界性要求（M2 的 I-3）是在**去重表与会话缓存**上，不是在这张表上。
代价说清楚：`_load()` 是 O(全部历史)，日志很大时启动会变慢 ——
这一条留给运维侧轮转（`logs/*.jsonl` 都是可轮转的），不在这里做。
"""

import copy
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from app.incident.model import (
    EVENT_ACK,
    EVENT_ALERT_LINKED,
    EVENT_CREATED,
    EVENT_DIAGNOSED,
    EVENT_NOTIFIED,
    EVENT_REOPENED,
    EVENT_RESOLVED,
    STATUS_ACK,
    STATUS_OPEN,
    STATUS_REOPENED,
    STATUS_RESOLVED,
    IncidentError,
    can_transition,
    new_id,
    normalize_severity,
    severity_of,
    severity_rank,
)
from app.llm import PROJECT_ROOT


def _now() -> str:
    """落盘用时间戳。**全项目只有这里生成事件时间。**

    秒级精度是故意的：事件是"给人看的时间线"，不是时序数据库。
    排序的稳定性靠 `list()` 里的插入序号兜（同秒创建的事件不能随机排）。
    """
    return datetime.now().isoformat(timespec="seconds")


def _highest(a: str, b: str) -> str:
    """两档等级取高。事件的 severity **只升不降**（理由见 `link_alert`）。"""
    return a if severity_rank(a) >= severity_rank(b) else b


def alert_fingerprint(alert: dict) -> str:
    """算一条告警的去重键（成员告警按它归并）。

    先认 `fingerprint` 字段（上游算好的权威值）。**没有就得自己造一个** ——
    这是个真实的坑：如果 `fingerprint` 缺失时一律返回 `""`，
    那么这个事件下所有"没带指纹的告警"都会塌成同一条成员记录，
    计数变成一个没有意义的数字，而且**告警条数直接丢了**。

    降级顺序：
        1. `fingerprint`（权威）
        2. `alertname|host|service`（归一化之后必然有的三个字段）
        3. 整条告警的稳定哈希 —— 连名字都没有的裸 payload（归一化失败时
           会出现）也要能"自己跟自己折叠"，而不是跟别的告警混在一起。
    """
    if not isinstance(alert, dict):
        alert = {}
    given = str(alert.get("fingerprint") or "").strip()
    if given:
        return given
    parts = [str(alert.get(key) or "").strip()
             for key in ("alertname", "host", "service")]
    if any(parts):
        return "derived:" + "|".join(parts)
    blob = json.dumps(alert, sort_keys=True, ensure_ascii=False, default=str)
    return "derived:sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _require_person(by: str, action: str) -> str:
    """"谁做的"必须有名字。返回去掉前后空格的 `by`。

    ★ 这条校验被放在**状态校验之前**，是有意的：
      "这一步合不合法"依赖状态机是否正确，而"有没有人"不依赖任何东西。
      把"无人签名一律拒"做成无条件的第一道闸，状态机哪天被改错了，
      这条底线还在。（与审批单"批准必须填审批人"同一条原则 ——
      事故复盘时"系统自己决定关掉的"和"不知道谁关的"是两件事，
      而"没填"属于后者。）
    """
    name = (by or "").strip()
    if not name:
        raise IncidentError(f"{action}必须提供执行人（by 字段）—— 没有名字的动作等于没人负责")
    return name


class IncidentStore:
    """事件存储。线程安全（FastAPI 的同步接口跑在线程池里，必须加锁）。

    记录字段（全部由 `_apply` 折叠得到，任何地方都不许绕过它直接改）：

        id / source / host / service / severity / status / summary / owner
        created_at / updated_at
        acked_at / acked_by
        resolved_at / resolved_by / resolution_note
        reopened_at
        linked_alerts   成员告警（按 fingerprint 去重 + 计数）
        diagnosis       最近一次诊断结论（摘要 + trace_id + ok）
        notifications   出站通知的送达记录（成功与失败都在）
        timeline        完整事件流，**按发生顺序追加**（产品展示的就是它）
        history         只有状态迁移，字段固定 {ts, event, by}（对账用）

    `timeline` 与 `history` 的分工值得记一笔：前者回答"这件事都发生过什么"
    （含关联告警、诊断、通知），后者回答"状态被谁改过几次"。
    审批单只有 history；事件两者都要，因为**诊断与通知本身就是要展示的内容**，
    把它们塞进 history 会让"状态迁移账本"不再是纯迁移，没法直接对账。
    """

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else (PROJECT_ROOT / "logs" / "incidents.jsonl")
        self._lock = threading.RLock()
        self._records = {}          # id -> record（折叠后的当前状态）
        self._load()

    # ============================================================
    # 持久化
    # ============================================================
    def _load(self) -> None:
        """启动时把日志折叠成当前状态。

        只跳过**坏的那一行**，不因为一行坏了丢掉整份历史 ——
        进程被 kill 在写一半的时候，最后一行必然是坏的，
        而它前面的全部历史都是好的（这正是选追加日志而不是单文件 JSON 的原因）。
        """
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict):
                        self._apply(ev)
        except OSError:
            # 读不了（权限/文件被占）时**不让进程起不来**：事件是旁路记账，
            # 它读不出来也不该拦住 FastAPI 启动。此时内存为空，
            # 后续写入照常追加 —— 日志本身没丢。
            pass

    def _append(self, event: dict) -> None:
        """追加一条事件。**原地回填 `ts`**，然后把同一个对象交给 `_apply`。

        这两点都不能省：
            - 顺序反了（先改内存后落盘）→ 内存说"已结单"，盘上没有，重启退回去
            - 不回填（`event = {"ts": ..., **event}`）→ 内存里时间戳全是 None，
              重启后从盘上折叠又好了 —— 见模块开头的"踩过的坑"
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event.setdefault("ts", _now())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _apply(self, ev: dict) -> None:
        """一条事件折叠进状态。

        这里**再判一次** `can_transition`，不是为了防调用方（调用方已经先 raise 过），
        而是为了防**盘上的历史**：日志可能被手工改过、可能是旧版本写的。
        有了这道闸，"不可能的迁移"永远不会被折叠成状态 ——
        折叠结果因此只可能是合法状态序列的产物。

        非法/认不出的事件一律**静默丢弃**，不抛异常：
        折叠发生在 `__init__` 里，这里抛一次异常就等于服务起不来。
        """
        kind = ev.get("event")
        iid = ev.get("id")

        if kind == EVENT_CREATED:
            self._apply_created(ev)
            return

        rec = self._records.get(iid)
        if rec is None:
            # 孤儿事件（日志被截断、或者手工插了一行）：没有 created 就无处可折。
            # 直接丢掉，**不要**凭空造一条记录 —— 那样 counts() 会多出
            # 谁也没建过的"幽灵事件"，而值班的人会去查它。
            return

        ts = ev.get("ts")
        if kind in (EVENT_ACK, EVENT_RESOLVED, EVENT_REOPENED):
            if not can_transition(rec["status"], kind):
                return
            self._apply_transition(rec, ev, ts)
        elif kind == EVENT_ALERT_LINKED:
            self._apply_alert_linked(rec, ev, ts)
        elif kind == EVENT_DIAGNOSED:
            self._apply_diagnosed(rec, ev, ts)
        elif kind == EVENT_NOTIFIED:
            self._apply_notified(rec, ev, ts)
        else:
            # 认不出的类型：忽略。向前兼容 —— 新版本写的日志被旧版本读到时不崩。
            return
        rec["updated_at"] = ts

    def _apply_created(self, ev: dict) -> None:
        iid = ev.get("id")
        if not iid or iid in self._records:
            # 重复的 created 不能覆盖已有状态：否则日志里补一行 created
            # 就能把一个已结单的事件重置回 open，等于凭空抹掉一段历史。
            return
        ts = ev.get("ts")
        by = str(ev.get("by") or ev.get("source") or "unknown")
        self._records[iid] = {
            "id": iid,
            "source": ev.get("source", ""),
            "host": ev.get("host", ""),
            "service": ev.get("service", ""),
            "severity": normalize_severity(ev.get("severity")),
            "status": STATUS_OPEN,
            "summary": ev.get("summary", ""),
            "owner": ev.get("owner", "") or "",
            "created_at": ts,
            "updated_at": ts,
            "acked_at": None,
            "acked_by": "",
            "resolved_at": None,
            "resolved_by": "",
            "resolution_note": "",
            "reopened_at": None,
            "linked_alerts": [],
            "diagnosis": None,
            "notifications": [],
            "timeline": [{"ts": ts, "event": EVENT_CREATED, "by": by, "note": ""}],
            "history": [{"ts": ts, "event": EVENT_CREATED, "by": by}],
        }

    def _apply_transition(self, rec: dict, ev: dict, ts: str) -> None:
        """三个状态迁移事件。到这里的都已经过 `can_transition`。"""
        kind = ev.get("event")
        by = ev.get("by") or ""
        note = ev.get("note", "")
        if kind == EVENT_ACK:
            rec["status"] = STATUS_ACK
            # 顶层的 owner / acked_by 是"**当前**负责人"。
            # 复发后再次认领会覆盖它 —— 上一个负责人不丢，在 timeline / history 里。
            rec["owner"] = by
            rec["acked_at"] = ts
            rec["acked_by"] = by
        elif kind == EVENT_RESOLVED:
            rec["status"] = STATUS_RESOLVED
            rec["resolved_at"] = ts
            rec["resolved_by"] = by
            rec["resolution_note"] = note
        elif kind == EVENT_REOPENED:
            rec["status"] = STATUS_REOPENED
            rec["reopened_at"] = ts
            # 上一次的结单信息从**顶层字段**上清掉：顶层字段代表"当前状态"，
            # 一个 reopened 的事件还挂着 resolved_at 是自相矛盾的
            # （看板会显示"已结单 12:00"却同时显示"处理中"）。
            # 历史没丢 —— timeline / history 里那条 resolved 还在。
            rec["resolved_at"] = None
            rec["resolved_by"] = ""
            rec["resolution_note"] = ""
        rec["timeline"].append({"ts": ts, "event": kind, "by": by, "note": note})
        rec["history"].append({"ts": ts, "event": kind, "by": by})

    def _apply_alert_linked(self, rec: dict, ev: dict, ts: str) -> None:
        """并入一条成员告警。**同一个 fingerprint 只占一行，计数 +1。**

        为什么不去重而是计数：告警系统在故障持续期间会反复推同一条告警
        （每次评估周期一次）。如果每条都存，"50 条告警聚成 1 个事件"会变成
        "1 个事件里躺着 3000 条一模一样的记录"；如果只留最后一次，
        又丢掉了"它反复响了 3000 次"这个信息 —— 而反复响本身是等级的信号。
        所以：**一行 + 计数**。
        """
        raw = ev.get("alert")
        alert = raw if isinstance(raw, dict) else {}
        # 事件里带了 fingerprint（写入时算好的）就用它，保证折叠与写入同一口径；
        # 没有就现算 —— 旧格式日志也能正确折叠。
        fp = str(ev.get("fingerprint") or alert_fingerprint(alert))
        fresh = normalize_severity(alert.get("severity"))
        for item in rec["linked_alerts"]:
            if item["fingerprint"] == fp:
                item["count"] += 1
                item["updated_at"] = ts
                item["severity"] = _highest(item["severity"], fresh)
                break
        else:
            rec["linked_alerts"].append({
                "fingerprint": fp,
                "alertname": alert.get("alertname", ""),
                "severity": fresh,
                "count": 1,
                "first_seen": ts,
                "updated_at": ts,
            })
        # 事件的等级 = 成员告警里最高的一档，**只升不降**。
        rec["severity"] = _highest(rec["severity"], severity_of(rec["linked_alerts"]))
        note = alert.get("alertname") or fp
        rec["timeline"].append({"ts": ts, "event": EVENT_ALERT_LINKED,
                                "by": "alert", "note": note})

    def _apply_diagnosed(self, rec: dict, ev: dict, ts: str) -> None:
        """记下诊断结论（摘要 + trace_id + 是否通过校验）。"""
        ok = bool(ev.get("ok", True))
        rec["diagnosis"] = {
            "summary": ev.get("summary", ""),
            "trace_id": ev.get("trace_id", ""),
            "ok": ok,
            "at": ts,
        }
        note = ev.get("summary", "")
        if not ok:
            # **不许谎报成功**：过不了校验的结论也要能看见，而且要一眼看出
            # 它没通过校验。否则时间线上一个失败的诊断和成功的诊断长得一样，
            # 人会拿一个"模型胡说但没过校验"的结论去处置故障。
            note = f"[未通过校验] {note}"
        rec["timeline"].append({"ts": ts, "event": EVENT_DIAGNOSED,
                                "by": ev.get("by") or "agent", "note": note})

    def _apply_notified(self, rec: dict, ev: dict, ts: str) -> None:
        """记下一条出站通知的送达结果。失败的原因也要留下。"""
        channel = ev.get("channel") or "unknown"
        ok = bool(ev.get("ok"))
        error = ev.get("error", "")
        rec["notifications"].append({"ts": ts, "channel": channel,
                                     "ok": ok, "error": error})
        if ok:
            note = f"{channel}：已送达"
        else:
            note = f"{channel}：发送失败（{error or '未记录原因'}）"
        rec["timeline"].append({"ts": ts, "event": EVENT_NOTIFIED,
                                "by": "notify", "note": note})

    # ============================================================
    # 写入
    # ============================================================
    def create(self, *, source: str, host: str = "", service: str = "",
               severity: str, summary: str = "", alerts: list = None,
               owner: str = "") -> dict:
        """建立一个事件。

        `severity` 是必填的（哪怕传了个认不出的值，也会被压成 warning）：
        事件是给人分诊用的，"这个有多急"必须有一个明确答案，
        不能让调用方漏填之后悄悄变成 None 再在排序时炸掉。

        `alerts` 是本事件**已经收到**的成员告警。它们走的是和 `link_alert`
        完全相同的一条路径（不是特例代码），所以去重、计数、等级提升的
        语义完全一致 —— 只有一份实现，就没有"两条路径行为不一样"的可能。
        """
        with self._lock:
            iid = self._new_id_locked()
            ev = {"event": EVENT_CREATED, "id": iid, "source": source,
                  "host": host, "service": service,
                  "severity": normalize_severity(severity), "summary": summary,
                  "owner": owner, "by": str(source or "unknown")}
            self._append(ev)
            self._apply(ev)
            for alert in (alerts or []):
                self.link_alert(iid, alert)
            return self.get(iid)

    def link_alert(self, iid: str, alert: dict) -> dict:
        """并入一条成员告警（按 `fingerprint` 去重；已存在则计数 +1）。

        ★ **本方法绝不改变 `status`** —— 包括"事件已结单、告警又响了"这种情况。
          结单之后要把事件打回处理中，必须显式调 `reopen()`。理由：
          "记一条关联告警"看起来是个只读操作，如果它偷偷改了状态，
          时间线上就会出现**没人下过指令的状态变化** ——
          而时间线的全部价值就在于"每一行都对应一个明确的动作"。
          代价：调用方（告警聚合路径）多一次显式判断。这个代价我们认。

        ★ 等级**只升不降**：后续并入低等级告警不能把事件降下来。
          一次故障在升级过程中从 warning 升到 critical，是正确的；
          随着恢复期零星进来几条 info 又降回 warning，会让"这事有多严重"
          随最后一条告警随机漂移 —— 复盘时看不出当时的峰值。
        """
        with self._lock:
            rec = self.get(iid)
            if not isinstance(alert, dict) or not alert:
                # 告警入口送进来一条垃圾：**不要抛**。
                # 外部系统不会重放，抛出去这条告警就永远消失了
                # （同一条原则见 tests/test_alert_normalize.py 的头部说明）。
                return rec
            ev = {"event": EVENT_ALERT_LINKED, "id": iid, "alert": alert,
                  "fingerprint": alert_fingerprint(alert)}
            self._append(ev)
            self._apply(ev)
            return self.get(iid)

    def attach_diagnosis(self, iid: str, summary: str, trace_id: str = None,
                         ok: bool = True) -> dict:
        """把诊断结论挂到事件上。

        **不检查状态**，这是有意的：诊断可能是异步跑完才回来的
        （M2 的 `ALERT_ASYNC=1`），回来时事件可能已经被人先 `resolve` 了。
        此时应该做的**不是**丢掉结论（那是花钱换来的、也是复盘要用的），
        而是如实记上 —— `ok=False` 表示"这个结论没过校验"，
        它会以 `[未通过校验]` 出现在时间线上，不会被误当成可信结论。
        """
        with self._lock:
            self.get(iid)
            ev = {"event": EVENT_DIAGNOSED, "id": iid, "summary": summary,
                  "trace_id": trace_id or "", "ok": bool(ok), "by": "agent"}
            self._append(ev)
            self._apply(ev)
            return self.get(iid)

    def ack(self, iid: str, by: str, note: str = "") -> dict:
        """认领：把事件交到某个具体的人手上。**`by` 必填。**

        只有 `open` / `reopened` 可以认领：
            - 已经是 `ack` 再认领一次 → 拒绝。否则两个人都以为自己接下了
              这个故障，而真正的处理人（第一个）会在事后被追责时发现
              "单子上写的是李四"。
            - 已经是 `resolved` 再认领 → 拒绝。请先 `reopen`：
              跳过"复发"这一事实去处理，时间线上就看不出中间发生过什么。
        """
        with self._lock:
            name = _require_person(by, "认领事件")
            rec = self.get(iid)
            if not can_transition(rec["status"], EVENT_ACK):
                raise IncidentError(
                    f"事件 {iid} 当前状态是 {rec['status']}，不能认领"
                    f"（只有 open / reopened 可以认领；已结单的请先 reopen）")
            ev = {"event": EVENT_ACK, "id": iid, "by": name, "note": note}
            self._append(ev)
            self._apply(ev)
            return self.get(iid)

    def resolve(self, iid: str, by: str, note: str = "") -> dict:
        """结单。**`by` 必填**（谁结的单），`note` 记处置说明。

        只有 `ack` 可以结单 —— `open` 直接结单被**有意**禁止：
        那等于"无人认领就被关掉"，而事故复盘里最怕的就是这句
        "这个故障谁处理的？没人"。自愈场景请先 `ack(by="auto")`。
        """
        with self._lock:
            name = _require_person(by, "结单")
            rec = self.get(iid)
            if not can_transition(rec["status"], EVENT_RESOLVED):
                raise IncidentError(
                    f"事件 {iid} 当前状态是 {rec['status']}，不能结单"
                    f"（必须先 ack：没有负责人的结单等于没人处理过）")
            ev = {"event": EVENT_RESOLVED, "id": iid, "by": name, "note": note}
            self._append(ev)
            self._apply(ev)
            return self.get(iid)

    def reopen(self, iid: str, reason: str = "") -> dict:
        """复发：同源告警又响了。

        只有 `resolved` 能 reopen（没有结过的单谈不上"重开"）。
        actor 固定记为 `alert` —— 这个动作是**告警系统**触发的，不是人；
        写成一个假人名（比如 "system"）会让审计看不出"是人重开的还是告警重开的"。
        真正的人工重开属于另一件事：新建一个事件。

        顶层的 `resolved_*` 会被清空（见 `_apply_transition`），
        但时间线上那一次结单不会被抹掉。
        """
        with self._lock:
            rec = self.get(iid)
            if not can_transition(rec["status"], EVENT_REOPENED):
                raise IncidentError(
                    f"事件 {iid} 当前状态是 {rec['status']}，不能重开"
                    f"（只有已结单的事件才可能复发）")
            ev = {"event": EVENT_REOPENED, "id": iid, "by": "alert", "note": reason}
            self._append(ev)
            self._apply(ev)
            return self.get(iid)

    def mark_notified(self, iid: str, channel: str, ok: bool, error: str = "") -> dict:
        """记一条出站通知的结果。成功与失败**都要记**。

        通知是旁路：它失败不该影响诊断。但失败必须留痕，
        否则"半夜没人被叫醒"这件事会变成一个查不出来的悬案 ——
        看起来通知发了，实际上对方 500 了。

        渠道名缺失时落成 `unknown` 而不是抛异常：**这条记录本身不能因为
        渠道名是空的而丢失**（恰恰是配置写错的时候最需要留下"发去哪失败了"）。
        """
        with self._lock:
            self.get(iid)
            ev = {"event": EVENT_NOTIFIED, "id": iid,
                  "channel": (channel or "").strip() or "unknown",
                  "ok": bool(ok), "error": error}
            self._append(ev)
            self._apply(ev)
            return self.get(iid)

    # ============================================================
    # 查询
    # ============================================================
    def get(self, iid: str) -> dict:
        """取一个事件。不存在 → `IncidentError`。

        返回的是**深拷贝**。浅拷贝不够：`timeline` / `linked_alerts` 是列表，
        浅拷贝之后调用方（接口层、看板、测试）随手 `rec["timeline"].append(...)`
        就改到了 store 的内存态，而盘上没变 —— 于是"内存与盘不一致"，
        也就是本模块开头那个坑的另一种形态。**这个 bug 只在"有人不小心改了"
        时才出现，而回滚它的成本远高于一次 deepcopy。**
        """
        with self._lock:
            rec = self._records.get(iid)
            if not rec:
                raise IncidentError(f"没有这个事件：{iid}")
            return copy.deepcopy(rec)

    def list(self, status: str = None, limit: int = 50) -> list:
        """列表，新的在前。

        排序键是 `(created_at, 插入序号)`：事件创建到秒级精度，
        同一秒内建的多个事件 `created_at` 完全相同。只按 `created_at` 排的话，
        它们的相对顺序由字典迭代顺序决定 —— 重启后折叠顺序一致，所以还算稳定，
        但读出 "limit=50" 时**砍掉哪 5 条会变得没道理**。加上插入序号，
        排序就是全序，"最新 50 条"才有确定含义。
        """
        with self._lock:
            rows = list(enumerate(self._records.values()))
            if status:
                rows = [(seq, r) for seq, r in rows if r["status"] == status]
            rows.sort(key=lambda pair: (pair[1].get("created_at") or "", pair[0]),
                      reverse=True)
            return [copy.deepcopy(r) for _, r in rows[:limit]]

    def counts(self) -> dict:
        """{"total": n, <status>: n, ...}。看板顶部的数字就是它。

        `total` 必须等于各状态之和 —— 有一条事件的状态不在 `ALL_STATUSES` 里
        （比如旧版本写的日志），它就会被算进 total 却不在任何一列里，
        看板上的数字加起来对不上。所以状态集合是封闭的、由 model 定义。
        """
        with self._lock:
            out = {"total": len(self._records)}
            for rec in self._records.values():
                out[rec["status"]] = out.get(rec["status"], 0) + 1
            return out

    # ============================================================
    # 内部
    # ============================================================
    def _new_id_locked(self) -> str:
        """生成一个没被用过的 id。

        `secrets.token_hex(4)` 只有 32 bit，撞的概率极低但不是零 ——
        而"两个不同故障共用一个 id"会让磁盘上的历史互相污染，
        且**不可恢复**。多试几次的成本是零，所以顺手做掉。
        """
        for _ in range(8):
            iid = new_id()
            if iid not in self._records:
                return iid
        # 连撞 8 次（概率上只可能是随机源坏了）：退回按已有数量加后缀，
        # 保证**唯一**优先于"好看"。
        return f"{new_id()}-{len(self._records)}"


# 单例。事件状态必须是进程级的 —— 每次请求新建一个 store，内存里的状态就散了
# （虽然能从日志重建，但没必要每次都读文件）。与 approvals.store() 同形。
_STORE = IncidentStore()


def store() -> IncidentStore:
    return _STORE
