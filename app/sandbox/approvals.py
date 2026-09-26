# -*- coding: utf-8 -*-
"""
Human-in-the-Loop —— 人工确认的审批单
============================================================
策略说"这条命令要人点头"，这个文件负责"这张审批单的生命周期"。

【为什么需要它 —— 从一个具体场景说起】

凌晨 3 点，告警响了：web-01 的 /var/log 写满，nginx 开始 502。

Agent 诊断完，它想执行 `truncate -s 0 /var/log/nginx/error.log`
—— 清理旧日志。这是**正确**的操作。

但它是**写操作**。

两条路：
    ① 让它直接执行       —— 快。但如果它判断错了呢？
                            如果是数据库目录而不是日志呢？
                            如果这个日志正被审计要求保留呢？
    ② 停下来等人确认     —— 慢几步。但**责任边界清楚了**。

生产环境选 ②。不是因为不信任模型，是因为**"谁批准了这次变更"
这个问题必须有一个答案**。事故复盘时，"模型自己决定的"不是一个能交差的回答。

所以 HITL 要解决的不是"让 Agent 更小心"，是**让每一次改动都有主**。

【为什么不直接用 LangGraph 的 interrupt】

LangGraph 有 `interrupt()` + checkpointer，能在图中间暂停、等下回带着
同一个 thread_id 恢复 —— 那是最"框架原生"的做法。本项目没用它，理由是：

    1. **审批可能来自另一个客户端、延迟很久。**
       interrupt 是进程内的：图被中断后挂在内存里（或 checkpointer 里），
       等下一次调用恢复。而真实的审批是"值班工程师十分钟后打开手机点同意"——
       这中间 Web 进程可能重启过、可能换了实例。
       要让它撑住，就得引入 Postgres/SQLite checkpointer，那是新的一份持久化。

    2. **"待办审批"需要是一个可查询的列表。**
       值班台要能问"现在有几条等我批" —— 这是产品形态问题，不只是技术问题。
       图里的中断状态散在各条 thread 里，汇总不出来。
       本项目的审批单是一张**独立的、可查询、可审计的表**。

    3. **审批和执行应该解耦。**
       批准 ≠ 立刻执行。批完之后可能是另一个进程、另一个时间点执行的。
       这个解耦让"审批"这个动作本身变得可被记录和追责。

如果要做"审批后自动从断点继续跑完整个 Agent 流程"，那 interrupt + checkpointer
更合适。本项目的定位是**审批作为独立的运维动作**，所以选了外部记录这条路。
两种都成立，取决于你要的是"续跑"还是"可追溯的审批流"。

【持久化：追加事件日志，状态靠折叠（fold）得到】

不用数据库，用一行一个 JSON 的追加日志。理由是它天然满足三个要求：

    - **不会写坏**：追加是原子性的，写到一半断电，前面几行仍然完整
    - **可审计**：每一次状态变化都留了痕，不是只留最终结果
    - **可重建**：当前状态 = 把日志从头折叠一遍。删掉内存，重启后一模一样

这跟 logs/audit.jsonl 是同一个模式。**这类"状态变化需要留痕"的场景，
追加日志比 UPDATE 语句更合适** ——因为 UPDATE 会把"过程"抹掉。
"""

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.llm import PROJECT_ROOT

# 审批单状态
PENDING = "pending"        # 等人处理
APPROVED = "approved"      # 已批准，待执行
REJECTED = "rejected"      # 已驳回
CONSUMED = "consumed"      # 已执行完毕（**一次性，不能重放**）
EXPIRED = "expired"        # 超时未处理

DEFAULT_TTL_SECONDS = 30 * 60      # 30 分钟。够一个人看到，又不至于挂一整天
MAX_RECORDS = 500                  # 内存里最多保留多少条（防止日志无限增长拖慢启动）


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ApprovalError(Exception):
    """审批流程错误。消息会直接返回给调用方。"""


class ApprovalStore:
    """审批单存储。线程安全（FastAPI 的同步接口跑在线程池里，必须加锁）。

    【为什么要加锁】
    `/approvals/{id}/approve` 和 `/approvals/{id}/execute` 是同步接口，
    FastAPI 会把它们丢进线程池 —— 也就是说**它们可能真的并发执行**。
    两个请求同时点"同意"，或者"同意"和"执行"撞在一起，
    不加锁就可能把同一张单子消费两次。

    这类并发 bug 在本地开发时几乎复现不出来（要压测才看得到），
    上线之后才炸。**加锁的成本是一行，不加的成本是一次线上事故。**
    """

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else (PROJECT_ROOT / "logs" / "approvals.jsonl")
        self._lock = threading.RLock()
        self._records = {}          # id -> record（折叠后的当前状态）
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        """启动时把日志折叠成当前状态。

        ★ 注意这里处理了"日志写了但没人处理"的情况：
          载入完之后直接扫一遍过期。因为进程可能停了一整天，
          期间没有任何请求进来触发过期检查。
          **过期检查不能只在"有人访问"时才做** —— 那样表里会长期
          挂着一堆早就该死的单子，值班的人看到的是脏数据。
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
                        # 只跳过坏的那一行，不要因为一行坏了丢掉整份历史
                        continue
                    self._apply(ev)
        except OSError:
            pass
        self._expire_locked()

    def _append(self, event: dict) -> None:
        """追加一条事件。先落盘再更新内存 —— 顺序反了的话，
        内存说"批准了"而盘上没有，重启就丢了。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": _now(), **event}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _apply(self, ev: dict) -> None:
        """一条事件 folding 进状态。"""
        kind = ev.get("event")
        rid = ev.get("id")
        if kind == "created":
            self._records[rid] = {
                "id": rid,
                "created_at": ev.get("ts"),
                "expires_at": ev.get("expires_at"),
                "source": ev.get("source", ""),
                "question": ev.get("question", ""),
                "tool": ev.get("tool", ""),
                "command": ev.get("command", ""),
                "fingerprint": ev.get("fingerprint", ""),
                "rule": ev.get("rule", ""),
                "risk": ev.get("risk", ""),
                "isolation": ev.get("isolation", ""),
                "reason": ev.get("reason", ""),
                "status": PENDING,
                "history": [{"ts": ev.get("ts"), "event": "created"}],
            }
        elif rid in self._records:
            rec = self._records[rid]
            # 只有 pending / approved 能被后续事件改变 ——
            # consumed / rejected / expired 是终态。
            # **终态必须不可逆**，否则"已执行的单子又被驳回"这种人话说不通的状态
            # 就会出现，而且审计上是灾难。
            if rec["status"] not in (PENDING, APPROVED):
                return
            if kind == "approved":
                rec["status"] = APPROVED
                rec["approved_by"] = ev.get("by")
                rec["approved_at"] = ev.get("ts")
                rec["approve_note"] = ev.get("note", "")
            elif kind == "rejected":
                rec["status"] = REJECTED
                rec["rejected_by"] = ev.get("by")
                rec["rejected_at"] = ev.get("ts")
                rec["reject_note"] = ev.get("note", "")
            elif kind == "consumed":
                rec["status"] = CONSUMED
                rec["consumed_at"] = ev.get("ts")
                rec["consumed_by"] = ev.get("by")
                rec["result_ok"] = ev.get("ok")
            elif kind == "expired":
                rec["status"] = EXPIRED
            else:
                return
            rec["history"].append({"ts": ev.get("ts"), "event": kind,
                                   "by": ev.get("by")})

    # ---------- 创建 ----------
    def create(self, *, command: str, fingerprint: str, rule: str, risk: str,
               isolation: str, reason: str, source: str = "agent",
               question: str = "", tool: str = "run_command",
               ttl: int = DEFAULT_TTL_SECONDS) -> dict:
        with self._lock:
            rid = "ap-" + secrets.token_hex(4)
            expires_at = (datetime.now() + timedelta(seconds=ttl)
                          ).isoformat(timespec="seconds")
            ev = {"event": "created", "id": rid, "expires_at": expires_at,
                  "source": source, "question": question, "tool": tool,
                  "command": command, "fingerprint": fingerprint,
                  "rule": rule, "risk": risk, "isolation": isolation,
                  "reason": reason}
            self._append(ev)
            self._apply(ev)
            return dict(self._records[rid])

    # ---------- 查询 ----------
    def get(self, rid: str) -> dict:
        with self._lock:
            self._expire_locked()
            rec = self._records.get(rid)
            if not rec:
                raise ApprovalError(f"没有这张审批单：{rid}")
            return dict(rec)

    def list(self, status: str = None, limit: int = 50) -> list:
        with self._lock:
            self._expire_locked()
            rows = list(self._records.values())
            if status:
                rows = [r for r in rows if r["status"] == status]
            # 新的在前
            rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
            return [dict(r) for r in rows[:limit]]

    def counts(self) -> dict:
        with self._lock:
            self._expire_locked()
            out = {"total": len(self._records)}
            for r in self._records.values():
                out[r["status"]] = out.get(r["status"], 0) + 1
            return out

    # ---------- 状态迁移 ----------
    def _expire_locked(self) -> int:
        now = datetime.now()
        n = 0
        for rec in self._records.values():
            if rec["status"] != PENDING:
                continue
            try:
                if datetime.fromisoformat(rec["expires_at"]) < now:
                    rec["status"] = EXPIRED
                    self._append({"event": "expired", "id": rec["id"]})
                    n += 1
            except (ValueError, TypeError):
                continue
        return n

    def approve(self, rid: str, by: str, note: str = "") -> dict:
        with self._lock:
            rec = self.get(rid)
            if rec["status"] != PENDING:
                raise ApprovalError(
                    f"这张审批单当前状态是 {rec['status']}，不能再批准"
                    f"（只有 pending 可以批准）")
            if not (by or "").strip():
                # 强制记录"谁批的"。没有审批人的审批单等于没有审批。
                raise ApprovalError("必须提供审批人（by 字段）")
            ev = {"event": "approved", "id": rid, "by": by.strip(), "note": note}
            self._append(ev)
            self._apply(ev)
            return dict(self._records[rid])

    def reject(self, rid: str, by: str, note: str = "") -> dict:
        with self._lock:
            rec = self.get(rid)
            if rec["status"] != PENDING:
                raise ApprovalError(
                    f"这张审批单当前状态是 {rec['status']}，不能再驳回")
            ev = {"event": "rejected", "id": rid, "by": (by or "anonymous").strip(),
                  "note": note}
            self._append(ev)
            self._apply(ev)
            return dict(self._records[rid])

    def consume(self, rid: str, *, expected_fingerprint: str = None,
                by: str = "system", ok: bool = True) -> dict:
        """消费（执行）一张已批准的审批单。**单次使用。**

        ★ 这里做两件防重放 / 防篡改的事：

          1. **指纹比对**。执行前重新算一遍命令指纹，跟审批时记下的比对。
             不一致 → 拒绝执行 + 报错。这是防 TOCTOU ——
             批准的和执行的必须是同一条命令。

             （实际上本项目里指纹是从同一个 Decision 对象来的，
               不存在被改的可能；但这道校验的意义在于**它不依赖
               "上游没被改过"这个假设**。安全性不该建立在
               "别的地方不会出错"之上。）

          2. **状态机只允许 APPROVED → CONSUMED 走一次**。
             第二条 consume 会撞在 "当前状态是 consumed" 上。
             **"已执行的审批单被重放"是这类系统最典型的漏洞** ——
             一次批准执行一百次，审批就形同虚设了。
        """
        with self._lock:
            rec = self.get(rid)

            if expected_fingerprint and rec.get("fingerprint") != expected_fingerprint:
                raise ApprovalError(
                    f"命令指纹不匹配，拒绝执行。"
                    f"审批时是 {rec.get('fingerprint')}，"
                    f"现在要执行的是 {expected_fingerprint}。"
                    f"（批准的和执行的不是同一条命令 —— 已记审计）")

            if rec["status"] == CONSUMED:
                raise ApprovalError(
                    f"这张审批单已经执行过了（{rec.get('consumed_at')}），"
                    f"不能重复执行。需要再跑请重新提交审批")
            if rec["status"] != APPROVED:
                raise ApprovalError(
                    f"审批单状态是 {rec['status']}，必须先批准才能执行")

            ev = {"event": "consumed", "id": rid, "by": by, "ok": ok}
            self._append(ev)
            self._apply(ev)
            return dict(self._records[rid])


# 单例。审批状态必须是进程级的 —— 每次请求新建一个 store，
# 内存里的状态就散了（虽然能从日志重建，但没必要每次都读文件）。
_STORE = ApprovalStore()


def store() -> ApprovalStore:
    return _STORE
