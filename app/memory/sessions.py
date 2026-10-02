# -*- coding: utf-8 -*-
"""
会话记忆：让同一个会话里的追问能被接上
============================================================
本文件只解决一个问题：**`/chat` 现在每次都从零开始。**

    用户："web-01 的磁盘满了怎么处理"
    模型：（答）
    用户："那 inode 呢"          ← 模型不知道"那"指的是 web-01 的磁盘

【为什么需要它，以及它和最省事的做法差在哪】

最省事的做法是让调用方每次把整段对话自己带上来（无状态 HTTP 的标准答案）。
这对**程序**调用方是对的：调用方本来就持有上下文。
但对**人**（值班台上打字、手机上看告警后追问）不成立 ——
人不会每次把前 10 轮重新贴一遍，于是"追问"这个动作实际上不可用。

代价说清楚：会话记忆把服务从"无状态"变成"有状态"。有状态就带来
持久化、上限、淘汰、并发这几件事 —— 这个文件就是来付这个代价的。
**不传 `session_id` 时它一行都不会被走到**（见 `app/main.py` 的 `chat_endpoint`），
默认档的语义一个字都不变。

【持久化：继续用「追加 JSONL + 折叠」】

与 `app/incident/store.py`、`app/sandbox/approvals.py` 同一套路，理由也一样：

    · **不会写坏**：追加是原子的，进程被 kill 在写第 3 行中间，前 2 行仍然完整
    · **可重建**：当前状态 = 把日志从头折叠一遍，重启不丢
    · **不引入新依赖**：`requirements.txt` 保持 9 个（CI 有守卫）

★ 但这里比事件/审批单多一条硬要求：**上限必须如实报告，不许静默丢历史。**

    事件表可以"不封顶"（它是审计物，见 incident/store.py 的说明）；
    会话表不行 —— 会话是高频写入（每问一句就 +1 轮），不封顶就是内存泄漏。
    所以它有**两道**上限（每会话轮数、全局会话数），
    而这两道上限**一定会丢东西**（旧的轮次、最久没用的会话）。

    **丢可以，偷偷丢不行。** 于是分三处落实：

        ① `append()` 的返回值里带 `evicted`（丢了哪几轮、丢了哪个会话）
        ② 丢这件事本身**写进日志**（`trim` / `evict` 事件），
           重启折叠之后内存与盘上一致 —— 不会出现"盘上有 12 轮、
           内存里只有 10 轮"这种两个来源对不上的状态
        ③ `sweep()` 返回删掉的轮数（不是 True/False）

    为什么这条这么重要：一个"最多保留 10 轮"的常量如果只是**静默**截断，
    排查时看到的现象是"它好像忘了我前面说的话"——
    而这种 bug 没有现场、没有日志、复现不了。本项目在 approvals
    的 `MAX_RECORDS` 上已经吃过一次"说了不做的常量"的亏（见那里的长注释）。

【并发：为什么必须加锁】

`/chat` 是 async 接口，但它读写的这个 store 是**进程级共享**的，
而且同步接口（`/sessions/{id}`）会跑在线程池里 —— 它们真的会并发。
两个请求同时给同一个会话追加一轮，或者"追加"撞上"淘汰"：
不加锁就会读改写出错（后写的把先写的那轮的 `trim` 判定覆盖掉）。
与事件存储同一条结论：**加锁的成本是一行，不加的成本是一次线上事故。**

【跨进程：为什么不做跨进程锁（与 approvals 的取舍不同）】

`approvals.py` 有一把 `O_CREAT|O_EXCL` 的文件锁，因为**重复执行一条已批准的命令
是安全事故**。会话不是：两个人用同一个 `session_id` 追问，
最坏结果是对话历史顺序有点乱 —— 不值得为它引入跨进程锁的复杂度
（锁文件、陈旧锁接管、超时）。
但"另一个进程写过"这件事还是要跟上：`_reload_if_changed()` 按
`(mtime_ns, size)` 判断，写之前先重新折叠一遍，
**判定必须基于最新的事实**（否则会拿昨天的内存态去淘汰今天的会话）。
"""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from app.llm import PROJECT_ROOT

#: 合法 `session_id`。**只允许这三个字符类，长度 1~64。**
#:
#: ★ 为什么必须校验（两道不同的理由，都很具体）：
#:
#:   ① **防日志污染**：`session_id` 会原样进 JSONL 与审计。
#:      如果放行换行符，一行日志就变成了两行 —— 折叠时多出一条谁也不认识的记录，
#:      而且**盘上的行数与内存里的轮数从此对不上**（审计最喜欢这种）。
#:      放行路径分隔符更糟：那是把"一个字段"当成"一个路径"用。
#:
#:   ② **防"用别人的 id 看别人的对话"**：纯数字/纯十六进制的 id
#:      是可以被猜的（`session-1`、`session-2`…）。本文件**不解决**这件事
#:      （会话没有鉴权归属），但把它写在文档的"已知边界"里 ——
#:      校验只保证 id 形态合法，不保证 id 属于你。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 单个会话保留最近多少轮（一轮 = 一问一答）。
DEFAULT_MAX_TURNS = 10

#: 全局最多多少个会话。超了按**最久未使用**淘汰。
#:
#: 为什么是 200 而不是 10000：这是"一个人的值班台 + 几个程序的会话"的量级。
#: 调大只会让内存撑得更久一点，不会让功能变好；真需要长期留存的，
#: 应该把日志留档（`logs/sessions.jsonl` 可轮转），而不是指望内存里那点。
DEFAULT_MAX_SESSIONS = 200

#: 单条回答落盘时的长度上限。**只截断会话历史里的副本，不截断给用户的回答。**
#:
#: 会话说到底是"给模型看的提示词材料"，不是审计物 ——
#: 一条 8KB 的诊断结论重复 10 轮，光提示词就把上下文窗口吃掉了。
#: 截断这件事同样是**如实的**：截断了就在答案末尾留下标记（见 `_clip`）。
MAX_ANSWER_CHARS = 4000
MAX_QUESTION_CHARS = 2000

#: 折叠动作在日志里的事件名。`trim` / `evict` 这两类事件的存在，
#: 就是为了让"丢过东西"这件事在盘上看得见（见模块开头 ②）。
EVENT_TURN = "turn"
EVENT_TRIM = "trim"
EVENT_EVICT = "evict"
EVENT_CLEAR = "clear"


def _now() -> str:
    """会话时间戳。秒级精度（与事件存储同一口径，避免两套时间格式）。"""
    return datetime.now().isoformat(timespec="seconds")


def _now_epoch() -> float:
    """给 `sweep` 用的 epoch 秒。

    ★ 为什么不复用 `datetime.now()`：`sweep` 要跟配置的秒数比大小，
    用 epoch 少一次 `fromisoformat` 解析 —— 而**解析失败**是个真实的坑：
    日志被手工改坏一行时间戳，`fromisoformat` 会抛 ValueError，
    整个 sweep 就挂了。epoch 比较里坏时间戳退化成 `inf`（永不过期），
    见 `_epoch_of`。
    """
    return datetime.now().timestamp()


def to_epoch(ts: str) -> float:
    """把落盘的时间戳换成 epoch 秒。**解析不出来就当"很久以前"。**

    方向是刻意选的：一个时间戳坏掉的会话，如果按"刚刚活跃"处理，
    它会永远逃过 `sweep`；按"很久以前"处理，最坏结果是它被清掉一次
    （而它已经坏了）。**坏的字段不该让整个清理机制失效。**
    """
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _clip(text, limit: int) -> str:
    """截断并**留下痕迹**。

    静默截断的后果是"模型看到的和实际发生的不一样"，而且查不出来。
    所以截了就在末尾写明截了多少字 —— 与项目里"不许谎报成功"同一条原则。
    """
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（会话记录已截断，原长 {len(s)} 字）"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读一个正整数环境变量。**读不出来就用默认值，绝不因此起不来。**

    与 `app/observability/jsonl.py` 的 `_env_int` 同一口径：
    配置写错（`SESSION_MAX_TURNS=abc`）时的正确行为是退回默认档，
    而不是让每次问答都 500。
    """
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


class SessionStore:
    """会话存储。线程安全（同步接口跑在线程池里，Web 层也可能并发）。

    内存态（**永远是盘上日志的函数**，任何写操作都是 `_append` → `_apply`）：

        `_sessions`: session_id -> {"session_id", "created_at", "last_seen", "turns"}
                     `turns` 是**旧→新**的 `{ts, question, answer}` 列表
        `_order`:    最近使用时间缓存，用于 LRU 淘汰与 `sweep`（派生值，可随时重建）

    `turns` 的顺序约定（旧→新）不是随手定的：`recent()` 要按"旧→新"喂给模型，
    倒序会让模型看到一段倒着放的对话，而它**不会报错，只会答得奇怪**。
    """

    def __init__(self, path: Path = None, max_turns: int = None):
        self.path = (Path(path) if path
                     else (PROJECT_ROOT / "logs" / "sessions.jsonl"))
        # ★ `max(1, ...)`：0 或负数不是一个"关掉历史"的开关，而是一个
        #   自相矛盾的状态（记了一轮又立刻删掉，`append` 的 `turns` 永远是 0）。
        #   要"完全没有会话记忆"的正确做法是**不传 session_id**（默认档）。
        self.max_turns = (max(1, int(max_turns)) if max_turns is not None
                          else _env_int("SESSION_MAX_TURNS", DEFAULT_MAX_TURNS))
        self.max_sessions = _env_int("SESSION_MAX_SESSIONS", DEFAULT_MAX_SESSIONS)
        self._lock = threading.RLock()
        self._sessions: dict = {}
        self._stamp = None
        self._load()

    # ============================================================
    # 持久化
    # ============================================================
    def _stat_stamp(self):
        """日志的 `(mtime_ns, size)`。用来判断"别的进程改过没有"。"""
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        """启动时把日志折叠成当前状态。

        与事件存储同一条容错原则：**只跳过坏的那一行**，不因为一行坏了
        丢掉整份历史（进程被 kill 在写一半时，最后一行必然是坏的）。
        """
        self._sessions = {}
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
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        self._apply(rec)
            self._stamp = self._stat_stamp()
        except OSError:
            # 读不了（权限/文件被占）时不让进程起不来：会话是旁路记忆，
            # 它读不出来也不该拦住 FastAPI 启动。此时内存为空，后续写入照常追加。
            pass

    def _reload_if_changed(self) -> bool:
        """日志被别的进程改过就重新折叠一遍（判定必须基于最新事实）。"""
        stamp = self._stat_stamp()
        if stamp is None or stamp == self._stamp:
            return False
        with self._lock:
            self._load()
        return True

    def _append(self, rec: dict) -> None:
        """追加一条记录。**先落盘，再折叠**，而且折叠的是同一个 dict。

        ★ 顺序与"同一个对象"这两点都不能省，理由与 `incident/store.py`
          开头那条"踩过的坑"完全一样：先改内存后落盘 → 内存说记下了、盘上没有，
          重启就退回去；不回填 ts → **当前进程内**时间戳是 None，
          重启折叠回来又是好的（"自愈"），属于最难复现的一类 bug。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec.setdefault("ts", _now())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._apply(rec)
        self._stamp = self._stat_stamp()

    # ============================================================
    # 折叠
    # ============================================================
    def _apply(self, rec: dict) -> None:
        """一条日志记录折叠进内存态。

        认不出的 `event` 一律**静默忽略**（向前兼容：新版本写的日志被旧版本
        读到时不崩），而折叠发生在 `__init__` 里，这里抛异常就等于服务起不来。
        """
        kind = rec.get("event")
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
            # 盘上的 id 不合法（手工改过、旧版本写的）：丢掉这一条。
            # **不许凭空建一个键**——那会让 `counts()` 多出谁也建不出来的会话。
            return
        ts = rec.get("ts") or _now()

        if kind == EVENT_TURN:
            entry = self._sessions.get(sid)
            if entry is None:
                entry = self._sessions[sid] = {
                    "session_id": sid,
                    "created_at": ts,
                    "last_seen": ts,
                    "turns": [],
                }
            entry["turns"].append({
                "ts": ts,
                "question": _clip(rec.get("question"), MAX_QUESTION_CHARS),
                "answer": _clip(rec.get("answer"), MAX_ANSWER_CHARS),
            })
            entry["last_seen"] = ts
            return

        entry = self._sessions.get(sid)
        if entry is None:
            # 孤儿记录（日志被截断、或手工插了一行）：没有 turn 就无处可折。
            # 直接丢掉，不要造记录 —— 否则会凭空多出一个"零轮会话"。
            return
        if kind == EVENT_TRIM:
            # ★ 折叠时**按盘上写的条数**删，而不是按当前的 `max_turns` 重算：
            #   配置可能改过（10 → 3），按当前配置重算会让"内存与盘上一致"
            #   这个不变量失效。**盘上写了什么就折什么。**
            self._drop_turns(entry, int(rec.get("count") or 0))
        elif kind == EVENT_EVICT:
            self._sessions.pop(sid, None)
        elif kind == EVENT_CLEAR:
            self._sessions.pop(sid, None)
        # 其余未知类型：忽略（向前兼容）

    def _drop_turns(self, entry: dict, count: int) -> None:
        """从**最旧的一端**删掉 `count` 轮。顺带维护 `last_seen`。"""
        turns = entry["turns"]
        if count <= 0 or not turns:
            return
        del turns[:count]
        if not turns:
            self._sessions.pop(entry["session_id"], None)
        else:
            entry["last_seen"] = turns[-1]["ts"]

    # ============================================================
    # 写入
    # ============================================================
    def append(self, session_id: str, question: str, answer: str) -> dict:
        """追加一轮（一问一答），返回**如实报告**的结果。

        返回：

            {"session_id", "turn", "turns", "last_seen",
             "evicted": {"turns": n, "sessions": m, "reason": [...]},
             "trimmed": bool}

        ★ `evicted` 是本方法最容易被写坏的地方。上限一定会触发，
          而"偷偷丢"会变成一个查不出来的 bug：用户觉得"它怎么忘了我说过的话"，
          日志里却什么都看不到。所以：

            · 每会话超出 `max_turns` → 丢最旧的，`evicted["turns"] += n`，
              并写一条 `trim` 事件（**盘上也能看到丢了几轮**）
            · 全局超出 `max_sessions` → 淘汰**最久未使用**的那个会话，
              `evicted["sessions"] += 1`，并写一条 `evict` 事件

        `turns` 是**提交之后**该会话的轮数：调大 `max_turns` 不会让历史回来，
        调用方（运维/看板）要能一眼看出"我现在只剩这么多"。
        """
        sid = require_session_id(session_id)
        with self._lock:
            self._reload_if_changed()
            ts = _now()
            evicted_turns = 0
            evicted_sessions = 0
            reasons = []

            self._append({"event": EVENT_TURN, "session_id": sid, "ts": ts,
                          "question": _clip(question, MAX_QUESTION_CHARS),
                          "answer": _clip(answer, MAX_ANSWER_CHARS)})

            entry = self._sessions[sid]
            entry["last_seen"] = ts
            overflow = len(entry["turns"]) - max(1, int(self.max_turns))
            if overflow > 0:
                # 先写 trim 再删：盘上的条数就是内存里要删的条数，
                # 折叠顺序与写入顺序完全一致（重启后一模一样）。
                self._append({"event": EVENT_TRIM, "session_id": sid, "count": overflow,
                              "reason": f"超过每会话上限 {self.max_turns} 轮"})
                evicted_turns += overflow
                reasons.append(f"每会话上限 {self.max_turns} 轮，丢了最旧的 {overflow} 轮")

            evicted_sessions += self._evict_lru_locked(keep=sid, reasons=reasons)

            return {
                "session_id": sid,
                "turn": {"ts": ts, "question": question, "answer": answer},
                "turns": len(self._sessions[sid]["turns"]) if sid in self._sessions else 0,
                "last_seen": self._sessions[sid]["last_seen"] if sid in self._sessions else ts,
                "evicted": {"turns": evicted_turns, "sessions": evicted_sessions,
                            "reason": reasons},
                "trimmed": bool(evicted_turns),
            }

    def _evict_lru_locked(self, keep: str = "", reasons: list = None) -> int:
        """会话数超上限时淘汰最久未使用的，返回淘汰了几个。

        `keep` 是**刚被写过的**那个会话：它一定是最新的，不该被自己淘汰掉。
        （这个参数不是多余的安全带：如果 `last_seen` 恰好同秒，
          "最旧的"在字典序里可能正好是刚写进来的那个 —— 那就会出现
          "刚记下的一轮立刻被丢掉"，而返回的 `evicted` 只有会话数、
          看不出丢的是刚写的那一个。）
        """
        removed = 0
        limit = max(1, int(self.max_sessions))
        while len(self._sessions) > limit:
            candidates = [s for s in self._sessions if s != keep]
            if not candidates:
                break
            victim = min(candidates, key=lambda s: (self._sessions[s]["last_seen"], s))
            lost = len(self._sessions[victim]["turns"])
            self._append({"event": EVENT_EVICT, "session_id": victim,
                          "reason": f"全局会话数超过上限 {limit}，按最久未使用淘汰",
                          "lost_turns": lost})
            removed += 1
            if reasons is not None:
                reasons.append(f"全局会话上限 {limit}，淘汰最久未使用的 "
                               f"{victim}（{lost} 轮）")
        return removed

    def clear(self, session_id: str) -> int:
        """清空一个会话，返回**删掉了几轮**（不是 True/False）。

        ★ 为什么返回条数：调用方（`DELETE /sessions/{id}` 的响应）要能回答
          "刚才到底清掉了什么"。返回布尔值的话，"清了一个不存在的会话"
          和"清了一个有 10 轮的会话"在响应里长得一样 ——
          而这两种情况运维要做的事完全不同。

        清空同样**留痕**（`clear` 事件）：会话日志是可轮转的旁路记录，
        但"谁在什么时候把哪个会话清了"这件事必须能查 ——
        否则"我的对话怎么没了"会变成一个查不出来的问题。
        """
        sid = require_session_id(session_id)
        with self._lock:
            self._reload_if_changed()
            entry = self._sessions.get(sid)
            if entry is None:
                return 0
            count = len(entry["turns"])
            self._append({"event": EVENT_CLEAR, "session_id": sid, "count": count,
                          "reason": "显式清除"})
            return count

    def sweep(self, max_age_seconds: int) -> int:
        """清掉**太久没动过**的会话，返回删掉的轮数。

        为什么需要它：`clear` 是用户主动清的，而"用户再也不回来了"是常态。
        没有这一步，200 个会话的名额会被上个月的一次性会话占满，
        真正在用的会话反而被 LRU 淘汰 —— 那就成了"越活跃越早被赶走"。

        ★ 过期判定用**最后一轮的时间**（`last_seen`），不是创建时间：
          一个早上建的会话如果一直在追问，它不该在下午被当成垃圾清掉。

        `max_age_seconds <= 0` → **什么都不做**（返回 0）。
        这是"关掉自动清理"的显式开关，比"传一个巨大的数"更不容易被误解。
        """
        try:
            age = int(max_age_seconds)
        except (TypeError, ValueError):
            return 0
        if age <= 0:
            return 0

        deadline = _now_epoch() - age
        with self._lock:
            self._reload_if_changed()
            victims = [s for s, e in self._sessions.items()
                       if to_epoch(e["last_seen"]) < deadline]
            removed = 0
            for sid in victims:
                entry = self._sessions.get(sid)
                if entry is None:
                    continue
                lost = len(entry["turns"])
                self._append({"event": EVENT_EVICT, "session_id": sid,
                              "reason": f"超过 {age} 秒没有新的一轮（自动过期）",
                              "lost_turns": lost})
                removed += lost
            return removed

    # ============================================================
    # 查询
    # ============================================================
    def recent(self, session_id: str, limit: int = None) -> list:
        """最近 `limit` 轮，**旧 → 新**。

        ★ 顺序是旧→新，而且这个约定要一直传到提示词里：
          模型看到"倒着的对话"不会报错，只会答得莫名其妙 ——
          这类问题在没有回归用例时几乎不可能发现（见 `tests/test_memory.py`
          里那条"顺序"的断言）。

        返回**深拷贝**（`dict(t)` 逐条复制）：浅拷贝之后调用方随手
        `turns[0]["answer"] = "..."` 就改到了 store 的内存态、而盘上没变 ——
        那正是"内存与盘不一致"的另一种形态（同 `incident/store.py` 的 `get`）。
        """
        sid = require_session_id(session_id)
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                return []
            turns = entry["turns"]
            if limit is not None:
                try:
                    n = max(0, int(limit))
                except (TypeError, ValueError):
                    n = len(turns)
                turns = turns[len(turns) - n:] if n else []
            return [dict(t) for t in turns]

    def counts(self) -> dict:
        """`{"sessions": n, "turns": m, "max_turns", "max_sessions"}`。

        把上限一起报出来是有意的：看板上"12 个会话 / 34 轮"
        如果不带上限，没人知道离淘汰还有多远 ——
        而 `evicted` 一旦开始非零，排查的第一步就是看这个比值。
        """
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "turns": sum(len(e["turns"]) for e in self._sessions.values()),
                "max_turns": self.max_turns,
                "max_sessions": self.max_sessions,
            }

    def list(self, limit: int = 50) -> list:
        """会话列表，**最近用过的在前**。不含 `turns` 正文（列表页会被撑爆）。"""
        with self._lock:
            rows = sorted(self._sessions.values(),
                          key=lambda e: (e["last_seen"], e["session_id"]),
                          reverse=True)
            out = []
            for entry in rows[:max(0, int(limit))]:
                out.append({
                    "session_id": entry["session_id"],
                    "created_at": entry["created_at"],
                    "last_seen": entry["last_seen"],
                    "turns": len(entry["turns"]),
                })
            return out

    def describe(self) -> dict:
        """当前配置的一句话说明（给 `/sessions` 用）。

        与 `/incidents` 里的 `aggregate` / `notify` 同一个理由：
        **免得看的人以为开了什么、其实配置里关着。**
        """
        return {
            "path": str(self.path),
            "max_turns": self.max_turns,
            "max_sessions": self.max_sessions,
            "note": ("超过上限时会按 trim/evict 事件写进日志，并在 append 的 "
                     "evicted 字段里如实报告；不会静默丢弃"),
        }


def require_session_id(session_id: str) -> str:
    """校验 `session_id`，非法就抛 `ValueError`。

    ★ 为什么是 `ValueError` 而不是自定义异常：这是个**参数校验**，
      调用方（FastAPI 层）要把它翻成 400。自定义异常会让"参数不对"
      和"存储出问题"在异常处理里混在一起，而这两件事的状态码不同。

    ★ 为什么校验放在**这一层**而不是接口层：本项目反复出现的那条原则 ——
      **约束要放在绕不过去的那一层**。接口层可以被绕过（脚本直接调 store），
      store 绕不过去。接口层再校验一次只是为了给出更友好的 400 文案。
    """
    sid = str(session_id or "").strip()
    if not _SESSION_ID_RE.match(sid):
        raise ValueError(
            "session_id 不合法：只允许 A-Za-z0-9_- ，长度 1~64"
            f"（收到 {session_id!r}）")
    return sid


# 单例。会话状态必须是进程级的 —— 每次请求新建一个 store，
# 内存里的会话就散了（虽然能从日志重建，但没必要每次都读文件）。
# 与 approvals.store() / incident.store() 同形。
#
# ★ 但这里**延迟创建**（第一次 `store()` 才建），刻意与 approvals/incident 的
#   "import 期就 `_STORE = Store()`"不同。理由有两条，都是具体的：
#
#     ① **import 不该有副作用**。`_STORE = SessionStore()` 会在 import 期
#        `mkdir(logs/)` 并创建 `logs/sessions.jsonl` —— 也就是说
#        "只想读一下 `/openapi.json`"或跑一条用例，都会在真实 logs/ 下留一个文件。
#        本项目为"测试污染真实 logs"付过代价（假 trace 写进真文件，
#        把对外成本口径压低了 12 倍），空文件是同一个方向上的小口子。
#     ② **环境变量要在"用的时候"读**。`SESSIONS_LOG` 是运维在 .env 里配的，
#        延迟创建让"配了就能生效"，而不是"必须在 import 之前配好"。
#        （`app/llm.py` 里 load_dotenv 会改 os.environ，这一条很实际。）
#
#   测试要能 monkeypatch：`_STORE` 仍然是模块级名字，
#   `store()` 拿到非 None 就直接返回它（see tests/test_memory.py 的装置）。
_STORE = None


def store() -> SessionStore:
    global _STORE
    if _STORE is None:
        _STORE = SessionStore(path=Path(os.getenv("SESSIONS_LOG") or
                                        (PROJECT_ROOT / "logs" / "sessions.jsonl")))
    return _STORE
