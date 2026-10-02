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
from contextlib import contextmanager
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
MAX_RECORDS = None                 # 已废弃：见下面模块级的说明

# ★ 关于内存上限（这个常量以前是**骗人的**）
#
#   这里原本写着 `MAX_RECORDS = 500`，注释声称"内存里最多保留多少条
#   （防止日志无限增长拖慢启动）"—— 但**全仓库零引用**。
#   也就是说：注释承诺了一个上限，代码里根本没有这个上限，
#   审批单的内存是随着历史单调增长的。
#
#   **一个说了不做的常量比没有这个常量更糟**：读代码的人会据此认为
#   "内存是有界的"，于是不去做轮转、不去看增长。
#   所以现在如实留成 None 并写明事实：
#
#     · 审批单内存 = O(全部历史)。审批是**审计物**，截断会让历史单查不到，
#       所以正确的做法不是"在内存里丢旧的"，而是**轮转 logs/approvals.jsonl**
#       （由运维侧按保留期处理，与 traces/audit 同一套办法）。
#     · 真正需要"有界"的是告警去重表（那是高频写入，
#       见 app/alerting/aggregator.py 的 TTL + 容量上限）。
#   `MAX_RECORDS` 这个名字保留下来只为兼容既有引用，值恒为 None。


class _FileLock:
    """跨进程互斥锁：用 `O_CREAT|O_EXCL` 抢一个锁文件。

    【为什么不用 fcntl.flock 或 msvcrt.locking】
    这个项目**开发在 Windows、部署在 Linux**：
      · `fcntl` 在 Windows 上不存在；
      · `msvcrt.locking` 只在 Windows 上有，而且锁的是字节区间，语义窄。
    `os.open(..., O_CREAT|O_EXCL)` 两边都是**原子**的，用它抢锁是同一个语义，
    而且**不需要引入任何新依赖**（requirements.txt 要保持 9 个）。

    【为什么必须处理"陈旧锁"】
    "抢锁的进程崩了"是必然会发生的事（kill -9、断电、容器被 OOM 杀掉）。
    如果不认陈旧锁，那个锁文件会**永久**挡住后续所有审批 ——
    一个会把系统锁死的保护机制，比没有保护更糟。
    所以锁文件超过 `stale_after` 秒没被动过，就视为陈旧、允许接管。

    【代价说清楚】
    `stale_after` 必须显著大于临界区耗时（这里临界区只有"重读日志 + 追加一行"，
    毫秒级），否则会把一个正在干活的进程的锁抢走 —— 那正好会造成我们
    想避免的双执行。取 60s 是"慢机器也够、又不会真的卡住一小时"的折中。
    """

    def __init__(self, path: Path, stale_after: float = 60.0,
                 timeout: float = 10.0, poll: float = 0.05):
        self.path = Path(path)
        self.stale_after = stale_after
        self.timeout = timeout
        self.poll = poll

    def _is_stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > self.stale_after
        except OSError:
            return False

    def _break(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                finally:
                    os.close(fd)
                return self
            except FileExistsError:
                if self._is_stale():
                    self._break()          # 接管陈旧锁
                    continue
                if time.time() >= deadline:
                    # from None：把 FileExistsError 从因果链里摘掉 ——
                    # 对调用方有意义的是"存储被占用"，而不是"os.open 撞了 EEXIST"。
                    raise ApprovalError(
                        "审批单存储正被另一个进程占用（可能是另一个实例在处理），"
                        "请稍后重试") from None
                time.sleep(self.poll)

    def __exit__(self, *exc):
        self._break()
        return False


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
        # 跨进程锁：与线程锁是两件事，都要有。
        #   线程锁管住"同一进程内的并发请求"（FastAPI 同步接口跑在线程池里）；
        #   文件锁管住"两个进程各持一张 APPROVED 各执行一次"——
        #   **后者是审计里 C2 那条真实缺陷**，光有线程锁挡不住。
        self._file_lock = _FileLock(self.path.with_suffix(self.path.suffix + ".lock"))
        self._records = {}          # id -> record（折叠后的当前状态）
        self._stamp = None          # 上次读到的 (mtime_ns, size)，用于"别的进程改过就重读"
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
            self._stamp = None
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
            self._stamp = self._stat_stamp()
        except OSError:
            pass
        self._expire_locked()

    # ---------- 跨进程一致性 ----------
    def _stat_stamp(self):
        """日志的 (mtime_ns, size)。用来判断"别的进程改过没有"。"""
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload_if_changed(self) -> bool:
        """日志被**别的进程**改过就重新折叠一遍。

        ★ 这是 C2 的另一半。光有文件锁不够 ——
          锁只保证"同一时刻只有一个人写"，但如果我内存里的状态是
          **昨天折叠**的，我今天拿着它做判断，照样会把一张已被别的进程
          消费过的单子再消费一次。**判定必须基于最新的事实。**
        """
        stamp = self._stat_stamp()
        if stamp is None or stamp == self._stamp:
            return False
        with self._lock:
            self._records.clear()
            self._load()
        return True

    @contextmanager
    def _mutating(self):
        """所有会改状态的入口都走这里：**先拿跨进程锁，再重新折叠，再判定+写入**。

        顺序不能变：
          拿锁 → 重读最新事实 → 判定 → 追加 → 放锁
        中间任何一步挪到锁外面，就等于把"检查"和"使用"分开了 ——
        那正是 TOCTOU 的定义。
        """
        with self._file_lock:
            self._reload_if_changed()
            with self._lock:
                yield

    def _append(self, event: dict) -> None:
        """追加一条事件。先落盘再更新内存 —— 顺序反了的话，
        内存说"批准了"而盘上没有，重启就丢了。

        ★ `ts` 必须**回填到调用方那个 dict 上**（setdefault 原地写），
          不能只写进本地的临时 dict。

          这里踩过一个很安静的坑：原先是
              event = {"ts": _now(), **event}     # ← 只改了局部变量
          于是紧跟其后的 `self._apply(ev)` 拿到的 `ev` 里**没有 ts**，
          `created_at` / `approved_at` / `consumed_at` 在**当前进程内**全成了
          None —— 而重启之后从盘上折叠回来又是好的（盘上有 ts）。
          表现是：接口返回的 `created_at: null`、`list()` 按 `created_at or ""`
          排序时同一批单子顺序随机；一重启就"自愈"，所以极难复现。
          **同一份数据有两个来源时，两个来源必须拿到同一个值。**

        ★ `flush + fsync`：写文件的"成功返回"只代表进了内核缓冲，
          不代表落了盘。审批这件事上"以为记下了、其实没有"的代价是
          **重放保护失效**（重启后那条 consumed 不见了），所以这里明确落盘。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event.setdefault("ts", _now())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._stamp = self._stat_stamp()

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
            #
            # ★ 例外：`executed` 不是状态迁移，而是对一条**已消费**记录的
            #   "补充事实"。终态锁住的是**状态**，不是**事实** ——
            #   执行结果只能在执行之后才知道，如果这里把它挡掉，
            #   审批记录就会永远停在那个乐观的 result_ok=True 上（审计里的 C4）。
            if rec["status"] not in (PENDING, APPROVED):
                if not (kind == "executed" and rec["status"] == CONSUMED):
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
                # 这是**乐观占位**（写于执行之前），真正的结果由后面的
                # executed 事件覆盖。字段名不变，语义见 record_result 的说明。
                rec["result_ok"] = ev.get("ok")
                rec["result_provisional"] = True
            elif kind == "executed":
                rec["executed_at"] = ev.get("ts")
                rec["result_ok"] = ev.get("ok")
                rec["result_provisional"] = False
                rec["exit_code"] = ev.get("exit_code")
                rec["elapsed_ms"] = ev.get("elapsed_ms")
                rec["result_error"] = ev.get("error", "")
            elif kind == "expired":
                rec["status"] = EXPIRED
                rec["expired_at"] = ev.get("ts")
                rec["expired_reason"] = ev.get("reason", "")
            else:
                return
            rec["history"].append({"ts": ev.get("ts"), "event": kind,
                                   "by": ev.get("by")})

    # ---------- 创建 ----------
    def create(self, *, command: str, fingerprint: str, rule: str, risk: str,
               isolation: str, reason: str, source: str = "agent",
               question: str = "", tool: str = "run_command",
               ttl: int = DEFAULT_TTL_SECONDS) -> dict:
        with self._mutating():
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
                    self._append({"event": "expired", "id": rec["id"],
                                  "reason": "无人处理，超过有效期自动过期"})
                    n += 1
            except (ValueError, TypeError):
                continue
        return n

    def approve(self, rid: str, by: str, note: str = "") -> dict:
        with self._mutating():
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
        with self._mutating():
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

        ★ 这里做三件防重放 / 防篡改的事：

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

          3. **整个判定到写入都在跨进程锁里，且基于最新折叠的状态**（C2）。
             光有状态机不够：两个进程各持一张 APPROVED 时，
             状态机在各自的进程里都是"合法的"。
             真正的互斥只能由**文件锁 + 重新折叠**提供。
        """
        with self._mutating():
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

            # ★ 有效期校验放在**状态机里**，不只放在接口的预检里。
            #   理由是项目反复出现的那条原则：**约束要放在绕不过去的那一层。**
            #   接口层可以被绕过（写个脚本直接调 store），
            #   状态机绕不过去。check_executable 只是把同一个判定提前、
            #   好给调用方一个更友好的错误，它不是唯一的关口。
            if self._is_expired(rec):
                expired_ev = {
                    "event": "expired", "id": rid,
                    "reason": f"到执行时已超过有效期（{rec.get('expires_at')}）"}
                self._append(expired_ev)
                self._apply(expired_ev)
                raise ApprovalError(
                    f"这张审批单已超过有效期（{rec.get('expires_at')}），拒绝执行。"
                    f"需要执行请重新提交审批 —— 一条三天前批准的命令，"
                    f"今天的环境已经和当时不是同一回事了")

            ev = {"event": "consumed", "id": rid, "by": by, "ok": ok}
            self._append(ev)
            self._apply(ev)
            return dict(self._records[rid])

    # ---------- 执行前的完整校验 / 执行后的结果回写（M3 C3、C4） ----------
    def check_executable(self, rid: str, *, expected_fingerprint: str = None) -> dict:
        """执行前的**全部**校验：存在 / 已批准 / 未过期 / 指纹一致。

        抽成一个方法是为了让"能执行"这件事只有一个定义 ——
        接口层不再自己拼一套判断（那边少判一条就是一个漏洞）。

        过期 → 顺带把单子折叠成 `expired` 并留下理由（C3，用户选定方案 A：
        **过期就拒绝**，需要时重新开票。这正是 TTL 该有的摩擦）。
        """
        with self._mutating():
            rec = self.get(rid)

            if rec["status"] == EXPIRED:
                raise ApprovalError(
                    f"这张审批单已过期（{rec.get('expires_at')}），不能执行。"
                    f"过期原因：{rec.get('expired_reason') or '超过有效期'}")
            if rec["status"] == CONSUMED:
                raise ApprovalError(
                    f"这张审批单已经执行过了（{rec.get('consumed_at')}），"
                    f"不能重复执行")
            if rec["status"] != APPROVED:
                raise ApprovalError(
                    f"审批单状态是 {rec['status']}，必须先批准才能执行")

            if self._is_expired(rec):
                ev = {"event": "expired", "id": rid,
                      "reason": f"到执行时已超过有效期（{rec.get('expires_at')}）"}
                self._append(ev)
                self._apply(ev)
                raise ApprovalError(
                    f"这张审批单已超过有效期（{rec.get('expires_at')}），拒绝执行。"
                    f"需要执行请重新提交审批 —— 一条三天前批准的命令，"
                    f"今天的环境已经和当时不是同一回事了")

            if expected_fingerprint and rec.get("fingerprint") != expected_fingerprint:
                raise ApprovalError(
                    f"命令指纹不匹配，拒绝执行。"
                    f"审批时是 {rec.get('fingerprint')}，"
                    f"现在要执行的是 {expected_fingerprint}。")
            return rec

    @staticmethod
    def _is_expired(rec: dict) -> bool:
        try:
            return datetime.fromisoformat(rec["expires_at"]) < datetime.now()
        except (KeyError, TypeError, ValueError):
            # 时间戳坏了 → **不当作过期**：宁可多执行一次人工已批准的命令，
            # 也不要因为一个坏字段把正常审批卡死（那是可用性事故）。
            return False

    def record_result(self, rid: str, *, ok: bool, exit_code=None,
                      elapsed_ms=None, error: str = "",
                      by: str = "system") -> dict:
        """执行完成后**回写真实结果**（C4）。

        ★ 为什么不在 consume 时写：
          `consume` 是防重放的闸门，**必须发生在执行之前**
          （反过来的话"执行成功但消费失败"会导致重复执行 —— 那个代价更大）。
          所以那条路写下的 `ok` 只是**乐观占位**，它不知道执行结果。

          代价就是审计里那条 C4：`approvals.jsonl` 里的 `result_ok`
          可能写着成功而实际失败。修法不是颠倒顺序（那会打开重放的洞），
          而是执行完再追加一条 `executed` 事件把真值补上，折叠时以最后一条为准。
        """
        with self._mutating():
            self.get(rid)          # 存在性检查：不存在的单子不该被写成"执行过"
            ev = {"event": "executed", "id": rid, "by": by, "ok": bool(ok),
                  "exit_code": exit_code, "elapsed_ms": elapsed_ms,
                  "error": error}
            self._append(ev)
            self._apply(ev)
            return dict(self._records[rid])


# 单例。审批状态必须是进程级的 —— 每次请求新建一个 store，
# 内存里的状态就散了（虽然能从日志重建，但没必要每次都读文件）。
_STORE = ApprovalStore()


def store() -> ApprovalStore:
    return _STORE
