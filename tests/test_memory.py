# -*- coding: utf-8 -*-
"""M5b 记忆：会话记忆 + 案例记忆。

这个文件围绕四件事（前两件是"能不能用"，后两件是"会不会害人"）：

    1. **会话记得住、也忘得掉**：追加 / 取回（旧→新）/ 清除 / 统计 / 过期，
       重启后还在（追加 JSONL + 折叠，同 incident/approvals）。
    2. **上限必须如实报告**：每会话 10 轮、全局 200 个会话。
       触发时 `append()` 的 `evicted` 里要有数、盘上要有 `trim`/`evict` 事件。
       **丢可以，偷偷丢不行** —— 静默截断会变成"它怎么忘了我前面说的话"，
       而这种 bug 没有现场、没有日志。
    3. ★ **案例只能当参考、不能当事实**：
       `render_context()` 必须写明这是**历史**、带**时间**、
       并要求"用当前机器的实际数据重新判断"。
       这条是本功能最大的风险 —— 模型把"上次的答案"当成"这次的事实"，
       处置动作跑成功了而实际问题还在（本项目一贯拒绝把未标注的推测喂给模型）。
    4. ★ **默认档行为不变**：`/chat` 不传 `session_id` 时，
       提示词、响应、落盘**与加 M5b 之前逐字段一致**。
       这条靠"消息里只有 system+user 两条、结果目录里一个文件都没多"来钉住。

零成本：不联网（conftest 的 autouse 守卫）、不调模型（`chat_stub` 打桩）、
**绝不写真实 `logs/`**（两个 store 都指向 `tmp_path` ——
本项目吃过这个亏：假 trace 写进真文件，把对外成本口径压低了 12 倍）。
"""

import json
from pathlib import Path
from typing import Iterator, Optional, cast

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.memory import cases as case_memory
from app.memory import sessions as session_memory
from app.memory.cases import CaseStore, find_similar, render_context
from app.memory.sessions import SessionStore, require_session_id
from app.observability import tracer
from app.sandbox import approvals

# ============================================================
# 装置
# ============================================================
@pytest.fixture
def session_path(tmp_path) -> Path:
    return tmp_path / "sessions.jsonl"


@pytest.fixture
def case_path(tmp_path) -> Path:
    return tmp_path / "cases.jsonl"


@pytest.fixture
def sessions(session_path, monkeypatch) -> SessionStore:
    """临时会话存储，同时**替换单例** —— 绝不写 logs/sessions.jsonl。"""
    fresh = SessionStore(path=session_path)
    monkeypatch.setattr(session_memory, "_STORE", fresh)
    monkeypatch.setattr(session_memory, "store", lambda: fresh)
    return fresh


@pytest.fixture
def cases(case_path, monkeypatch) -> CaseStore:
    """临时案例库，同时替换单例 —— 绝不写 logs/cases.jsonl。"""
    fresh = CaseStore(path=case_path)
    monkeypatch.setattr(case_memory, "_STORE", fresh)
    monkeypatch.setattr(case_memory, "store", lambda: fresh)
    return fresh


@pytest.fixture
def clock(monkeypatch) -> Iterator[dict]:
    """可控时钟（改会话轮次的落盘时间戳）。

    ★ 为什么需要：落盘时间戳是**秒级**的（与事件存储同一口径），
      所以一个用例里连续写的几条记录 `ts` 完全相同 ——
      "哪一轮更旧"靠 `ts` 区分不出来。
      靠 `time.sleep(1)` 让它们可区分，会让测试变慢且不稳；
      所以把"现在几点"换成一个每次调用 +1 秒的假时钟。

    ★ 但 `sweep` 的过期判定用的是**真实时钟**（`_now_epoch`）：
      过期是"距离现在多久"，而"现在"是外部事实 ——
      把两者都换掉，测的就是假时钟与假时钟的关系了（自我安慰）。
      所以过期用例走 `_backdate()`：直接改盘上的时间戳，让记录**真的**变旧。
    """
    state = {"t": 1_700_000_000.0}

    def now() -> str:
        state["t"] += 1.0
        from datetime import datetime
        return datetime.fromtimestamp(state["t"]).isoformat(timespec="seconds")

    monkeypatch.setattr(session_memory, "_now", now)
    yield state


def _backdate(store, seconds: int) -> None:
    """把盘上所有记录的时间戳往前推 `seconds` 秒，然后重新折叠。

    ★ 改的是**盘**而不是内存：这样折叠出来的 `last_seen` 与真实运行
      （重启后从日志折叠）走的是同一条路。改内存测的是另一个对象。
    """
    _shift(store, seconds)


def _backdate_one(store, session_id: str, seconds: int) -> None:
    """只把某一个会话的记录往前推（用来造"最久未使用"）。"""
    _shift(store, seconds, only=session_id)


def _shift(store, seconds: int, only: Optional[str] = None) -> None:
    from datetime import datetime

    rows = [json.loads(ln) for ln in
            store.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for row in rows:
        if only is not None and row.get("session_id") != only:
            continue
        try:
            shifted = (datetime.fromisoformat(row["ts"]).timestamp() - seconds)
        except (KeyError, TypeError, ValueError):
            continue
        row["ts"] = datetime.fromtimestamp(shifted).isoformat(timespec="seconds")
    store.path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    store._load()


def _turn(sessions, sid, n, question=None, answer=None) -> dict:
    """往会话里塞一轮，内容一眼能看出是第几轮。"""
    return sessions.append(sid, question or f"问题{n}", answer or f"答案{n}")


def _incident(iid="inc-0001", **over) -> dict:
    """造一个"已结单事件"的摘要（字段集与 `/incidents/{id}` 的返回一致）。

    ★ 持续时长刻意用 12 分 30 秒 = 750000 ms：不等于任何一条上限的秒数，
      所以"算错了"一定会被断言抓到（`elapsed_ms` 算成 0 或算成秒都会露馅）。
    """
    rec = {
        "id": iid,
        "fingerprint": "fp-nginx-1",
        "service": "nginx",
        "summary": "nginx 5xx 比例超过 5%",
        "host": "web-01",
        "status": "resolved",
        "created_at": "2026-10-01T10:00:00",
        "resolved_at": "2026-10-01T10:12:30",
        "resolution_note": "清理 /var/spool/clientmqueue 后恢复",
    }
    rec.update(over)
    return rec


@pytest.fixture
def chat_stub(monkeypatch):
    """把模型换成假实现（`/chat` 只走 `m.llm_chat` 这一个入口）。"""
    seen: list = []

    def fake(messages, temperature=0.7, timeout=60):
        seen.append(list(messages))
        return f"答复-{len(seen)}"

    monkeypatch.setattr(m, "llm_chat", fake)
    return seen


@pytest.fixture
def client(sessions, cases, chat_stub, tmp_path, monkeypatch) -> Iterator[TestClient]:
    """进程内客户端，所有会落盘的状态都改到 tmp_path。

    ★ 事件存储**也必须隔离**：`test_resolving_an_incident_records_a_case`
      会真的走一遍 `create → ack → resolve`，而 `/incidents/{id}/resolve`
      用的是**进程级单例** —— 漏掉这一行，用例就会往真实的
      `logs/incidents.jsonl` 里写演示数据。

      这不是假想的风险：本项目为同类事故付过代价（M1 期间一条漏加隔离的用例
      把假 trace 写进真实 logs/traces.jsonl，把对外成本口径压低了 12 倍）。
      **新加会落盘的功能时，隔离装置必须同步加上。**
      （这个用例的现场就是这么发现的：只跑本文件，`incidents.jsonl` 也变大了。）
    """
    from app.incident import store as incident_store_mod

    monkeypatch.setattr(m, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(tracer, "TRACE_PATH", tmp_path / "traces.jsonl")
    monkeypatch.setattr(approvals, "_STORE",
                        approvals.ApprovalStore(path=tmp_path / "approvals.jsonl"))
    fresh_incidents = incident_store_mod.IncidentStore(
        path=tmp_path / "incidents.jsonl")
    monkeypatch.setattr(incident_store_mod, "_STORE", fresh_incidents,
                        raising=False)
    monkeypatch.setattr(incident_store_mod, "store", lambda: fresh_incidents)
    from app import security
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    with TestClient(m.app) as c:
        yield c


def _audit_events(path: Path) -> list:
    """读审计里的 event 名（写不进去就是空列表，不影响断言的成功与否）。"""
    if not path.exists():
        return []
    return [json.loads(ln)["event"] for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _disk(sessions) -> list:
    """盘上的会话日志（一行一条记录）。"""
    return [json.loads(ln) for ln in
            sessions.path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ============================================================
# 一、会话记忆：记住 / 取回 / 顺序
# ============================================================
def test_append_then_recent_returns_what_was_said(sessions):
    """最基本的往返：问了什么、答了什么，一条不少。"""
    got = sessions.append("s-1", "web-01 磁盘满了", "先看 df -h")
    assert got["session_id"] == "s-1"
    assert got["turns"] == 1
    assert got["trimmed"] is False
    assert got["evicted"] == {"turns": 0, "sessions": 0, "reason": []}

    items = sessions.recent("s-1")
    assert len(items) == 1
    assert items[0]["question"] == "web-01 磁盘满了"
    assert items[0]["answer"] == "先看 df -h"
    assert items[0]["ts"], "会话轮次必须带时间（没有时间的上下文无法判断新旧）"


def test_recent_is_oldest_first(sessions):
    """★ 顺序是**旧→新**，而且这个约定要一直传到提示词里。

    倒序喂给模型，它**不会报错**，只会答得莫名其妙 ——
    这类问题没有断言基本发现不了（模型永远会给你一个看起来合理的回答）。
    """
    for i in range(1, 4):
        _turn(sessions, "s-1", i)
    assert [t["question"] for t in sessions.recent("s-1")] == \
        ["问题1", "问题2", "问题3"]


def test_recent_limit_takes_the_most_recent_end(sessions):
    """`limit` 取的是**最近的** N 轮，不是最早的 N 轮。"""
    for i in range(1, 6):
        _turn(sessions, "s-1", i)
    assert [t["question"] for t in sessions.recent("s-1", limit=2)] == \
        ["问题4", "问题5"]
    assert sessions.recent("s-1", limit=0) == []
    assert len(sessions.recent("s-1", limit=99)) == 5


def test_unknown_session_has_no_history_and_is_not_an_error(sessions):
    """没有历史的会话是**正常状态**，不是错误（`/chat` 的第一次请求就是这样）。"""
    assert sessions.recent("s-never") == []
    assert sessions.counts() == {"sessions": 0, "turns": 0,
                                 "max_turns": 10, "max_sessions": 200}


def test_sessions_are_isolated_from_each_other(sessions):
    """"同一个 session_id"才共享上下文 —— 串台比没有记忆更危险。"""
    _turn(sessions, "s-1", 1)
    _turn(sessions, "s-2", 2)
    assert [t["question"] for t in sessions.recent("s-1")] == ["问题1"]
    assert [t["question"] for t in sessions.recent("s-2")] == ["问题2"]
    assert sessions.counts()["sessions"] == 2


def test_recent_returns_copies_not_the_store_state(sessions):
    """返回的必须是副本：调用方随手改一下不能改到 store 的内存态（而盘上没变）。"""
    _turn(sessions, "s-1", 1)
    got = sessions.recent("s-1")
    got[0]["answer"] = "被我改了"
    got[0]["ts"] = "1999-01-01T00:00:00"
    assert sessions.recent("s-1")[0]["answer"] == "答案1"
    assert sessions.recent("s-1")[0]["ts"] != "1999-01-01T00:00:00"


# ============================================================
# 二、session_id 校验：防注入 / 防日志污染
# ============================================================
@pytest.mark.parametrize("good", [
    "s-1", "abc", "ABC_123", "a" * 64, "-_-", "0",
])
def test_valid_session_ids_are_accepted(sessions, good):
    assert require_session_id(good) == good
    assert sessions.append(good, "q", "a")["session_id"] == good


@pytest.mark.parametrize("bad", [
    "",            # 空
    "   ",         # 只有空格（strip 之后是空）
    "a b",         # 空格
    "s/1",         # 路径分隔符
    "s\\1",        # 反斜杠
    "../etc/passwd",
    "s.1",         # 点（jsonl 的扩展名就是这么来的）
    "会话一",       # 非 ASCII（不在白名单里）
    "s#1",
    "a\nb",        # ★ 换行：会**把一行日志变成两行**
    "a\r\nb",
    "a" * 65,      # 超长
])
def test_invalid_session_ids_are_rejected(sessions, bad):
    """★ 非法 id 一律 `ValueError`（接口层翻成 400）。

    最要命的是换行符：放行它，一行日志就变成两行 ——
    折叠时多出一条谁也不认识的记录，**盘上的行数与内存里的轮数从此对不上**。
    """
    with pytest.raises(ValueError):
        require_session_id(bad)
    with pytest.raises(ValueError):
        sessions.append(bad, "q", "a")
    with pytest.raises(ValueError):
        sessions.recent(bad)
    with pytest.raises(ValueError):
        sessions.clear(bad)
    assert sessions.counts()["turns"] == 0, "被拒的写入不能留下任何痕迹"


def test_a_rejected_id_never_reaches_the_log(sessions):
    """被拒的 id 连同内容一起不许落盘（校验必须在写之前）。"""
    with pytest.raises(ValueError):
        sessions.append("bad id!", "问题", "答案")
    assert not sessions.path.exists() or sessions.path.read_text() == ""


# ============================================================
# 三、清除与统计
# ============================================================
def test_clear_returns_how_many_turns_were_dropped(sessions):
    """★ 返回**条数**而不是布尔值：

    "清了一个不存在的会话"和"清了一个有 3 轮的会话"在响应里长得一样的话，
    运维没法判断"是不是我 id 打错了"。这两种情况要做的事完全不同。
    """
    for i in range(1, 4):
        _turn(sessions, "s-1", i)
    assert sessions.clear("s-1") == 3
    assert sessions.recent("s-1") == []
    assert sessions.counts()["turns"] == 0
    assert sessions.clear("s-1") == 0, "清一个已经空了的会话是 0 轮，不是报错"
    assert sessions.clear("s-never") == 0


def test_clear_only_touches_the_named_session(sessions):
    _turn(sessions, "s-1", 1)
    _turn(sessions, "s-2", 2)
    assert sessions.clear("s-1") == 1
    assert sessions.recent("s-2") != [], "清一个会话不能顺手动到别的会话"


def test_counts_adds_up_across_sessions(sessions):
    _turn(sessions, "s-1", 1)
    _turn(sessions, "s-1", 2)
    _turn(sessions, "s-2", 3)
    counts = sessions.counts()
    assert counts["sessions"] == 2 and counts["turns"] == 3
    # 上限一起报出来：否则看板上"3 轮"没人知道离淘汰还有多远
    assert counts["max_turns"] == 10 and counts["max_sessions"] == 200


def test_list_is_most_recently_used_first_without_the_body(sessions):
    _turn(sessions, "s-old", 1)
    _turn(sessions, "s-new", 2)
    _turn(sessions, "s-old", 3)          # 再用一次 → 它变成最近用过的
    rows = sessions.list()
    assert [r["session_id"] for r in rows] == ["s-old", "s-new"]
    assert rows[0]["turns"] == 2
    assert "items" not in rows[0], "列表页不带对话正文（会被撑爆）"


# ============================================================
# 四、上限：丢可以，偷偷丢不行
# ============================================================
def test_per_session_limit_keeps_the_newest_and_reports_the_loss(session_path):
    """★ 每会话上限触发时：留最近的 N 轮，**并且如实报告丢了几轮**。

    ★ 断言的是**累计**丢了多少，不是最后一次的返回值：
      上限是"每来一轮超了就丢一轮"，所以每次只丢 1 轮。
      只盯最后一次的 `evicted` 会读成"整个过程只丢了 1 轮" ——
      而用例要证明的恰恰是"总共丢了 2 轮，一笔都不少"。
    """
    st = SessionStore(path=session_path, max_turns=3)
    lost = 0
    for i in range(1, 6):
        got = st.append("s-1", f"问题{i}", f"答案{i}")
        lost += got["evicted"]["turns"]
        assert got["turns"] <= 3

    assert lost == 2, "总共丢了两轮，必须一笔不少地报出来"
    assert st._sessions["s-1"]["turns"], "内存里必须还有轮次"
    assert [t["question"] for t in st.recent("s-1")] == ["问题3", "问题4", "问题5"]
    assert st.counts()["turns"] == 3


def test_a_trim_is_written_to_the_log_not_silently_dropped(session_path):
    """★ 丢这件事必须**落盘**（`trim` 事件）。

    只在返回值里报一次是不够的：调用方可能把它丢了。
    盘上有 `trim` 才意味着"重启折叠之后内存与盘上仍然一致"
    （不会出现盘上 5 行、内存里 3 轮这种两个来源对不上的状态）。
    """
    st = SessionStore(path=session_path, max_turns=2)
    for i in range(1, 5):
        st.append("s-1", f"问题{i}", f"答案{i}")

    rows = _disk(st)
    trims = [r for r in rows if r["event"] == "trim"]
    assert [r["count"] for r in trims] == [1, 1]
    assert len([r for r in rows if r["event"] == "turn"]) == 4, "每一轮都要留痕"
    # 重启折叠后与关掉前**逐字段一致**（内存永远是盘上日志的函数）
    reborn = SessionStore(path=session_path, max_turns=2)
    assert reborn.recent("s-1") == st.recent("s-1")
    assert reborn.counts()["turns"] == 2


def test_global_session_limit_evicts_the_least_recently_used(monkeypatch, session_path):
    """★ 全局上限按**最久未使用**淘汰（不是最旧的会话）。

    上限走环境变量而不是构造参数：需求把 `SessionStore.__init__` 的签名钉成
    `(path, max_turns=None)`，所以"全局会话数"只能从 `SESSION_MAX_SESSIONS` 配
    —— 这也正是运维实际会用的那条路。
    """
    monkeypatch.setenv("SESSION_MAX_SESSIONS", "2")
    st = SessionStore(path=session_path, max_turns=10)
    st.append("s-a", "问题a", "答案a")
    st.append("s-b", "问题b", "答案b")
    st.append("s-a", "问题a2", "答案a2")        # s-a 又被用过一次

    # ★ 把 s-b 的时间戳往前拨：同秒内的先后靠 `ts` 是分不出来的，
    #   而"最久未使用"这个判定必须有可区分的事实（与 sweep 用同一条路）。
    _backdate_one(st, "s-b", 3600)

    got = st.append("s-c", "问题c", "答案c")
    assert got["evicted"]["sessions"] == 1
    assert got["evicted"]["turns"] == 0, "丢的是整个会话，不是某几轮"
    assert "s-b" in got["evicted"]["reason"][0], "要指明淘汰的是谁"
    assert {r["session_id"] for r in st.list()} == {"s-a", "s-c"}, "s-a 不该被赶走"
    assert st.counts()["sessions"] == 2


def test_the_session_just_written_is_never_evicted_by_itself(monkeypatch, session_path):
    """刚写进去的那一轮不能立刻被自己淘汰（`keep` 那道闸）。

    同秒的时间戳在字典序里可能让"最新的"排在最前 ——
    没有这道闸，现象是"我刚问的话它怎么不记得"，而日志上看不出任何异常。
    """
    monkeypatch.setenv("SESSION_MAX_SESSIONS", "1")
    st = SessionStore(path=session_path)
    got = st.append("s-1", "问题1", "答案1")
    assert got["turns"] == 1
    assert st.recent("s-1")[0]["question"] == "问题1"


def test_an_eviction_is_written_to_the_log(monkeypatch, session_path):
    monkeypatch.setenv("SESSION_MAX_SESSIONS", "1")
    st = SessionStore(path=session_path)
    st.append("s-a", "问题a", "答案a")
    st.append("s-b", "问题b", "答案b")
    evicts = [r for r in _disk(st) if r["event"] == "evict"]
    assert len(evicts) == 1 and evicts[0]["session_id"] == "s-a"
    assert evicts[0]["lost_turns"] == 1, "丢了几轮也要留在日志里"


def test_session_limits_are_read_from_the_environment(monkeypatch, session_path):
    """环境变量是可配的（默认档 = 10 轮 / 200 会话）。"""
    monkeypatch.setenv("SESSION_MAX_TURNS", "2")
    monkeypatch.setenv("SESSION_MAX_SESSIONS", "1")
    st = SessionStore(path=session_path)
    assert st.max_turns == 2 and st.max_sessions == 1
    for i in range(1, 4):
        got = st.append("s-1", f"q{i}", f"a{i}")
    assert got["turns"] == 2
    st.append("s-2", "q", "a")
    assert st.counts()["sessions"] == 1


@pytest.mark.parametrize("bad_value", ["abc", "", "-3", "0", "3.5"])
def test_a_broken_limit_falls_back_to_the_default(monkeypatch, session_path, bad_value):
    """配置写错 → 退回默认档，**绝不让每次问答都 500**（同 jsonl 的 _env_int）。"""
    monkeypatch.setenv("SESSION_MAX_TURNS", bad_value)
    st = SessionStore(path=session_path)
    assert st.max_turns == session_memory.DEFAULT_MAX_TURNS


def test_zero_max_turns_is_clamped_to_one(session_path):
    """`max_turns=0` 不是"关掉记忆"的开关，而是一个自相矛盾的状态
    （记一轮又立刻删掉，`turns` 永远是 0）。要关掉记忆的正确做法是不传 session_id。"""
    st = SessionStore(path=session_path, max_turns=0)
    assert st.max_turns == 1
    assert st.append("s-1", "q", "a")["turns"] == 1


# ============================================================
# 五、持久化：重启后一模一样 + 坏日志容忍
# ============================================================
def test_the_log_is_rebuilt_after_restart(session_path):
    """追加日志 + 折叠的全部意义：重启后逐字段一致。"""
    st = SessionStore(path=session_path)
    st.append("s-1", "问题1", "答案1")
    st.append("s-2", "问题2", "答案2")
    st.append("s-1", "问题3", "答案3")

    reborn = SessionStore(path=session_path)
    assert reborn.recent("s-1") == st.recent("s-1")
    assert reborn.recent("s-2") == st.recent("s-2")
    assert reborn.counts() == st.counts()


def test_a_corrupt_line_does_not_lose_the_rest(session_path):
    """一行坏数据只丢它自己（进程被 kill 在写一半时，坏的一定是最后一行）。

    这里同时验两种"坏"：
      · 不是合法 JSON（写一半）
      · `session_id` 非法（手工改过）→ **不能凭空造出一个会话**
      · 合法 id 但缺字段 → 折叠成空正文，而不是崩掉
    """
    st = SessionStore(path=session_path)
    st.append("s-1", "问题1", "答案1")
    with open(st.path, "a", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON\n")
        f.write("\n")
        f.write('{"event": "turn", "session_id": "bad id!", "question": "x"}\n')
        f.write('{"event": "turn", "session_id": "s-empty"}\n')

    reborn = SessionStore(path=session_path)
    assert reborn.recent("s-1")[0]["question"] == "问题1"
    assert reborn.recent("s-empty")[0]["question"] == "", "缺字段折叠成空串，不崩"
    assert {r["session_id"] for r in reborn.list()} == {"s-1", "s-empty"}, \
        "非法 id（带空格）不能凭空造出一个会话"


def test_the_log_survives_a_cleared_session(session_path):
    """清空之后重启，不会"从日志里又长回来"。"""
    st = SessionStore(path=session_path)
    st.append("s-1", "问题1", "答案1")
    assert st.clear("s-1") == 1
    reborn = SessionStore(path=session_path)
    assert reborn.recent("s-1") == []
    assert reborn.counts()["sessions"] == 0


def test_missing_file_is_not_an_error(session_path):
    st = SessionStore(path=session_path / "nope" / "none.jsonl")
    assert st.recent("s-1") == []
    assert st.counts()["sessions"] == 0


def test_another_process_writing_is_picked_up(session_path):
    """★ 别的进程写过就要重读（判定必须基于最新事实）。

    不重读的后果很具体：我拿着**昨天折叠的**内存态去做 LRU 淘汰，
    就会把"其实一直在用"的会话当成最久未使用的那个丢掉。
    """
    a = SessionStore(path=session_path)
    b = SessionStore(path=session_path)
    b.append("s-b", "别人写的", "答案")
    assert a.recent("s-b") == [], "还没重读之前，a 不知道这件事"
    a.append("s-a", "我写的", "答案")
    assert [t["question"] for t in a.recent("s-b")] == ["别人写的"]
    assert a.counts()["sessions"] == 2


# ============================================================
# 六、并发：同步接口跑在线程池里
# ============================================================
def test_concurrent_appends_do_not_lose_turns(sessions):
    """并发追加不能丢轮次。

    `/chat` 是 async 接口，但读写的是进程级共享的 store，
    而 `/sessions/{id}` 走线程池 —— 它们真的会并发。
    不加锁时"读改写"会互相覆盖，现象是"明明问了 24 次，只记下 19 轮"。
    """
    import threading

    def worker(n: int) -> None:
        for i in range(8):
            sessions.append("s-conc", f"问题{n}-{i}", "答案")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 24 轮 > 默认上限 10 → 被 trim 掉一部分，但**丢了多少必须能对上账**
    rows = _disk(sessions)
    turns = [r for r in rows if r["event"] == "turn"]
    trims = [r for r in rows if r["event"] == "trim"]
    assert len(turns) == 24, "每一次写入都必须落盘（一行都不能少）"
    assert sum(r["count"] for r in trims) == 24 - sessions.counts()["turns"]
    assert sessions.counts()["turns"] == 10


def test_a_turn_is_written_to_disk_before_the_memory_changes(sessions):
    """先落盘再改内存：反过来的话，内存说记下了而盘上没有，重启就退回去。"""
    sessions.append("s-1", "问题1", "答案1")
    rows = _disk(sessions)
    assert len(rows) == 1 and rows[0]["event"] == "turn"
    assert rows[0]["question"] == "问题1"
    assert rows[0]["ts"] == sessions.recent("s-1")[0]["ts"], \
        "内存与盘上必须是**同一个**时间戳（回填 ts，不是另写一个）"


# ============================================================
# 七、sweep：过期会话的清理
# ============================================================
def test_sweep_removes_sessions_that_went_quiet(sessions, session_path):
    """太久没有新的一轮 → 清掉，返回**删掉的轮数**。"""
    for i in range(1, 4):
        _turn(sessions, "s-old", i)
    for i in range(1, 3):
        _turn(sessions, "s-fresh", i)
    # 让 s-old 的三轮"变旧"：先写盘再折叠（走的就是重启折叠那条路）
    rows = _disk(sessions)
    for row in rows[:-2]:
        row["ts"] = "2020-01-01T00:00:00"
    sessions.path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    sessions._load()

    removed = sessions.sweep(max_age_seconds=600)
    assert removed == 3, "返回的是删掉的**轮数**，不是一个布尔值"
    assert sessions.recent("s-old") == []
    assert sessions.counts()["sessions"] == 1
    assert sessions.recent("s-fresh")[0]["question"] == "问题1"


def test_sweep_never_touches_a_session_that_keeps_talking(sessions):
    """★ 过期判定用**最后一轮**的时间，不是创建时间。

    一个早上建的会话如果一直在追问，它不该在下午被当成垃圾清掉 ——
    否则就成了"越活跃越早被赶走"。
    """
    _turn(sessions, "s-1", 1)
    _backdate(sessions, 5 * 86400)       # 创建时间变成五天前
    _turn(sessions, "s-1", 2)            # 但刚刚又追问了一轮
    assert sessions.sweep(max_age_seconds=600) == 0
    assert [t["question"] for t in sessions.recent("s-1")] == ["问题1", "问题2"]


def test_sweep_with_a_non_positive_age_is_off(sessions):
    """`<= 0` = 显式关掉自动清理（比"传一个巨大的数"更不容易被误解）。"""
    _turn(sessions, "s-1", 1)
    _backdate(sessions, 10 ** 6)
    assert sessions.sweep(max_age_seconds=0) == 0
    assert sessions.sweep(max_age_seconds=-1) == 0
    assert sessions.sweep(max_age_seconds=None) == 0
    assert sessions.counts()["sessions"] == 1


def test_sweep_removes_are_recorded_in_the_log(sessions):
    """清理也要留痕：否则"我的对话怎么没了"会变成一个查不出来的问题。"""
    _turn(sessions, "s-1", 1)
    _backdate(sessions, 10 ** 6)
    assert sessions.sweep(max_age_seconds=60) == 1
    evicts = [r for r in _disk(sessions) if r["event"] == "evict"]
    assert len(evicts) == 1
    assert "自动过期" in evicts[0]["reason"]


def test_a_broken_timestamp_does_not_break_sweep(sessions, session_path):
    """盘上有一行坏时间戳时，`sweep` 不能整个挂掉。

    ★ 这里用**改盘上日志**的方式造坏数据，而不是去改内存里的私有字段：
      要验的正是"从盘上折叠出来的坏时间戳"这条路 ——
      改内存测的是另一个对象，那不叫回归用例。

    坏时间戳的会话按"很久以前"处理（最坏结果是被清一次，而它本来就已经坏了）；
    反过来的方向是"永远逃过清理"—— 那是内存泄漏。
    """
    sessions.append("s-ok", "问题1", "答案1")
    sessions.append("s-bad", "问题2", "答案2")
    rows = _disk(sessions)
    for row in rows:
        row["ts"] = "不是时间"
    sessions.path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")

    reborn = SessionStore(path=session_path)
    assert reborn.counts()["sessions"] == 2, "坏时间戳不该让记录本身消失"
    assert reborn.sweep(max_age_seconds=60) == 2
    assert reborn.counts()["sessions"] == 0


# ============================================================
# 八、案例记忆：抽取与记录
# ============================================================
def test_record_case_extracts_the_documented_fields(cases):
    """案例字段就是这 8 个，一个不多一个不少（接口/文档都按它对账）。

    `case` 里**只有**这 8 个：分词缓存是内存里的派生态（`_` 开头），
    落盘和对外都不带它 —— 否则 `json.dumps` 会直接报"set 不是 JSON 类型"。
    """
    got = cases.record(_incident())
    assert got["recorded"] == "inc-0001"
    case = got["case"]
    assert {k for k in case if not k.startswith("_")} == {
        "ts", "incident_id", "fingerprint", "service", "symptom",
        "conclusion", "actions", "elapsed_ms"}
    assert case["service"] == "nginx"
    assert case["symptom"] == "nginx 5xx 比例超过 5%"
    assert case["conclusion"] == "清理 /var/spool/clientmqueue 后恢复"
    assert case["fingerprint"] == "fp-nginx-1"
    assert case["elapsed_ms"] == 750000, "持续时长是可算的（12 分 30 秒 = 750000ms）"
    assert case["ts"], "案例必须带时间（没有时间的经验无法判断是否还成立）"
    assert "_tokens" not in _disk(cases)[0], "派生缓存不该落盘"


def test_record_case_writes_one_line_per_case(cases):
    cases.record(_incident())
    rows = _disk(cases)
    assert len(rows) == 1
    assert rows[0]["event"] == "created" and rows[0]["incident_id"] == "inc-0001"


def test_the_same_incident_is_updated_not_duplicated(cases):
    """★ 同一个事件只留一条案例。

    故障会复发（`resolved → reopened → resolved`）。不去重的话，一个反复
    出问题的服务会堆出十几条几乎一样的记录，把真正不同的历史挤出上限 ——
    而"这东西老是坏"恰恰是最该被看见的信号。
    """
    cases.record(_incident())
    got = cases.record(_incident(resolution_note="换成异步写盘后恢复",
                                 resolved_at="2026-10-01T11:00:00"))
    assert got["reason"] == "同一事件再次结单，以最后一次为准"
    assert got["recorded"] == "inc-0001"
    assert cases.counts()["total"] == 1
    assert cases.get("inc-0001")["elapsed_ms"] == 60 * 60 * 1000, \
        "更新之后持续时长也要跟着新的结单时间走"
    assert cases.get("inc-0001")["conclusion"] == "换成异步写盘后恢复"


def test_a_case_without_an_incident_id_is_refused(cases):
    """★ 没有 id 的事件不记 —— 案例的价值全在"能回到那个事件看完整时间线"。

    造一个假 id 点进去 404，比没有这条案例更糟：它会让人以为查过了。
    """
    for payload in ({}, {"id": ""}, {"id": "   "}, None, "不是 dict"):
        got = cases.record(payload)
        assert got["recorded"] is None
        assert got["evicted"] == 0
        assert "id" in got["reason"]
    assert cases.counts()["total"] == 0
    assert not cases.path.exists() or cases.path.read_text() == ""


def test_elapsed_ms_is_none_when_it_cannot_be_computed(cases):
    """算不出来就 `None` —— **不编一个 0**（0 会被读成"瞬间就修好了"）。"""
    assert cases.record(_incident(resolved_at=None))["case"]["elapsed_ms"] is None
    assert cases.record(
        _incident(iid="inc-2", created_at="坏时间"))["case"]["elapsed_ms"] is None


def test_actions_prefer_the_human_resolution_note(cases):
    """`actions` 的取值**有优先级**：人写的结单说明 > 时间线上的动作 > 诊断摘要。

    为什么顺序不能反：结单说明是人对着现场写下的结论；诊断摘要是
    "模型当时说了什么"（它可能根本没解决问题）。案例的价值主要在人的那一句上。
    """
    # ① 有结单说明时，诊断摘要不抢位（它只是模型当时的说法）
    case = cases.record(_incident(diagnosis={"summary": "模型当时的猜测"}))["case"]
    assert "clientmqueue" in case["actions"]
    assert "模型当时的猜测" not in case["actions"], \
        "有人的结论时不该把模型的猜测混进去"

    # ② 没有结单说明时，退到时间线上的动作（只取动作性事件，不取 ack 备注）
    case2 = cases.record(_incident(
        iid="inc-3", resolution_note="",
        timeline=[{"event": "notified", "note": "已通知值班"},
                  {"event": "ack", "note": "这条不该进 actions"}]))["case"]
    assert "已通知值班" in case2["actions"]
    assert "这条不该进 actions" not in case2["actions"]

    # ③ 什么都没有时，才用诊断摘要（总比空着强）
    case3 = cases.record(_incident(
        iid="inc-4", resolution_note="", timeline=[],
        diagnosis={"summary": "只剩这一条材料"}))["case"]
    assert case3["actions"] == "只剩这一条材料"


def test_case_limit_drops_the_oldest_and_reports_it(case_path):
    """★ 案例库上限：丢最旧的，**丢了几个必须报出来**。"""
    st = CaseStore(path=case_path, max_cases=2)
    for i in range(1, 4):
        got = st.record(_incident(f"inc-{i}"))
    assert got["evicted"] == 1
    assert st.counts()["total"] == 2
    assert st.get("inc-1") == {}, "最旧的被丢掉了"
    assert st.get("inc-3") != {}
    evicts = [r for r in _disk(st) if r["event"] == "evict"]
    assert evicts and evicts[0]["incident_id"] == "inc-1"


def test_case_counts_and_config_are_honest(cases, monkeypatch):
    """统计要带上服务分布、时间跨度与上限 —— 免得让人以为案例库是无限的。"""
    monkeypatch.setenv("CASES_MAX", "7")
    fresh = CaseStore(path=cases.path, max_cases=None)
    assert fresh.max_cases == 7
    fresh.record(_incident("inc-1"))
    fresh.record(_incident("inc-2", service="mysql", summary="连接数打满"))
    counts = fresh.counts()
    assert counts["total"] == 2
    assert counts["services"] == {"nginx": 1, "mysql": 1}
    assert counts["oldest"] and counts["newest"]
    assert counts["max_cases"] == 7


def test_cases_are_rebuilt_from_the_log_after_restart(cases, case_path):
    cases.record(_incident())
    reborn = CaseStore(path=case_path)
    assert reborn.get("inc-0001") == cases.get("inc-0001")
    assert reborn.counts()["total"] == 1


def test_a_corrupt_case_line_does_not_lose_the_rest(cases, case_path):
    cases.record(_incident())
    with open(case_path, "a", encoding="utf-8") as f:
        f.write("{ 坏行\n")
        f.write('{"event": "created", "incident_id": ""}\n')
    reborn = CaseStore(path=case_path)
    assert reborn.counts()["total"] == 1


def test_case_get_returns_a_copy_and_an_empty_dict_when_missing(cases):
    """"没有案例"是正常状态（**不抛**）：抛了会把一次顺手查询变成 500。"""
    assert cases.get("inc-nope") == {}
    cases.record(_incident())
    got = cases.get("inc-0001")
    got["conclusion"] = "被我改了"
    assert cases.get("inc-0001")["conclusion"] != "被我改了"
    assert "_tokens" not in cases.get("inc-0001"), "派生的分词缓存不该泄出去"


# ============================================================
# 九、相似度：零成本、确定性
# ============================================================
def _seed_disk_cases(cases) -> None:
    cases.record(_incident("inc-1", service="nginx",
                           summary="web-01 磁盘 inode 耗尽，nginx 报 502",
                           resolution_note="清理 /var/spool/clientmqueue 里的积压邮件"))
    cases.record(_incident("inc-2", service="mysql",
                           summary="mysql 连接数打满，应用报 too many connections",
                           resolution_note="调大 max_connections 并重启"))
    cases.record(_incident("inc-3", service="nginx",
                           summary="nginx 配置改了之后没 reload，仍旧是旧配置",
                           resolution_note="nginx -s reload"))


def test_find_similar_ranks_by_keyword_overlap(cases):
    """字面重叠多、稀有的词命中 → 排前面。纯函数、不调模型。"""
    _seed_disk_cases(cases)
    rows = cases.find("web-01 磁盘 inode 又满了，nginx 502", limit=3)
    assert rows, "应该能找到相似的案例"
    assert rows[0]["incident_id"] == "inc-1"
    assert rows[0]["score"] > 0
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True), "必须按相似度降序"
    assert rows[0]["incident_id"] != "inc-2" or len(rows) == 1


def test_find_similar_is_deterministic(cases):
    """同样的问题必须返回同样的三条 —— 否则测试是假绿、行为不可复现。"""
    _seed_disk_cases(cases)
    first = cases.find("nginx 502 磁盘", limit=3)
    for _ in range(5):
        again = cases.find("nginx 502 磁盘", limit=3)
        assert [(r["incident_id"], r["score"]) for r in again] == \
            [(r["incident_id"], r["score"]) for r in first]


def test_find_similar_can_be_filtered_by_service(cases):
    """服务过滤先于打分：过滤掉之后没有命中就返回空，**不硬凑**。"""
    _seed_disk_cases(cases)
    assert [r["incident_id"] for r in cases.find("连接数", limit=3, service="mysql")], \
        "mysql 的那条在服务过滤之后仍应命中"
    assert cases.find("连接数", limit=3, service="redis") == []
    assert all(r["service"] == "nginx"
               for r in cases.find("nginx 磁盘 inode", limit=3, service="nginx"))


@pytest.mark.parametrize("query", ["", "   ", "完全无关的词汇 zzzqqq"])
def test_find_similar_returns_nothing_instead_of_guessing(cases, query):
    """★ 没有把握就返回空 —— **不许硬凑三条**。

    硬凑的后果比"没找到"严重得多：模型会拿到三条毫不相干的历史，
    却因为它们被摆在"相似案例"的位置上而当成参考。**没有比错的强。**
    """
    _seed_disk_cases(cases)
    assert cases.find(query, limit=3) == []


def test_find_similar_honours_limit_and_empty_library(cases):
    assert cases.find("nginx", limit=3) == [], "空库不报错"
    _seed_disk_cases(cases)
    assert len(cases.find("nginx", limit=1)) == 1
    assert cases.find("nginx", limit=0) == []


def test_find_similar_marks_every_row_as_history(cases):
    """★ 安全标注要**跟着数据走**，不能只挂在 `render_context` 上。

    调用方可能不经过 `render_context`（自己拼提示词、塞进别的流程）——
    那时候标注就丢了。所以每一行都自带 `note`（历史 + 时间）。
    """
    _seed_disk_cases(cases)
    for row in cases.find("nginx 磁盘", limit=3):
        assert "历史案例" in row["note"]
        assert row["ts"] in row["note"]
        assert "未必适用" in row["note"]


def test_find_similar_uses_the_shared_tokenizer(cases, monkeypatch):
    """复用 `app/rag/store.py` 的 `tokenize`，不另写一套分词。

    断言的是"调用发生了"：换掉 RAG 的分词器，案例检索的结果必须跟着变 ——
    否则说明这里其实用了自己的一套（那就会出现"知识库能查到、案例查不到"
    这种两套分词标准打架的现象）。
    """
    _seed_disk_cases(cases)
    calls = []

    def spy(text):
        calls.append(text)
        return ["nginx", "磁盘"]

    monkeypatch.setattr("app.rag.store.tokenize", spy)
    rows = cases.find("随便什么", limit=3)
    assert calls, "没有走 app.rag.store.tokenize"
    assert rows, "stub 出来的 token 应当能命中 nginx 的案例"


# ============================================================
# 十、★★ 案例只作参考、不作事实（本功能最大的风险）
# ============================================================
REQUIRED_WARNINGS = (
    "历史案例参考",
    "不是本次的事实",
    "当初的处理未必适用于现在",
    "请用当前机器的实际数据重新判断",
    "不要直接照搬",
)


def test_render_context_demands_fresh_evidence(cases):
    """★★ 守这段警示文案的用例 —— **它守的是安全边界，不是措辞**。

    失败形态（真实会发生）：昨天的 inode 耗尽用"清理邮件队列"修好了，
    今天另一台机器 502（这次是磁盘写满）。如果注入的文本读起来像一份结论，
    模型很可能直接建议清理那个队列 —— 命令跑成功、没有报错，
    而**实际问题还在**。

    所以这段输出必须同时做到：① 说是**历史**；② 每条带**时间**；
    ③ 结尾要求"用当前机器的实际数据重新判断"；④ 说明"当初的处置未必适用"。
    少一件，模型就可能把"上次的答案"当成"这次的事实"。
    """
    cases.record(_incident())
    text = render_context(cases.find("nginx 5xx", limit=3))

    for needle in REQUIRED_WARNINGS:
        assert needle in text, f"警示文案丢了：{needle!r}\n---\n{text}"
    assert "2026-" in text, "案例必须带时间（没有时间的经验无法判断是否还成立）"
    assert "inc-0001" in text, "要能回到原事件（来源事件 id）"
    assert "clientmqueue" in text, "案例内容本身也要在（否则这段提示词没有价值）"


def test_render_context_without_cases_injects_nothing(cases):
    """没有案例就**什么都不注入** —— 一段空标题会让模型以为"有案例但没内容"。"""
    assert render_context([]) == ""
    # `None` 走的是同一个分支（"没有案例"），用 cast 明确表达这一意图 ——
    # 签名上的 `list` 是给调用方看的窄类型，实现里本来就容忍 None。
    assert render_context(cast(list, None)) == ""
    assert render_context([{"不是案例": 1}, "垃圾"]) == "", \
        "全是垃圾时也不能吐出一个空壳标题"
    assert render_context(cases.find("没有任何命中的问题", limit=3)) == ""


def test_render_context_labels_every_case_as_history(cases):
    """每一条都要单独标注：只写在开头的话，长上下文被截断时
    留下的恰恰是没有警告的那一半。"""
    _seed_disk_cases(cases)
    text = render_context(cases.find("nginx 磁盘 inode", limit=3))
    assert text.count("案例 ") == len(cases.find("nginx 磁盘 inode", limit=3))
    for line in text.splitlines():
        if line.startswith("案例 "):
            assert "时间：" in line and "来源事件：" in line


def test_render_context_truncates_fields_with_a_marker(cases):
    """长字段截断必须**留痕**（静默截断会让模型以为这就是全部）。"""
    cases.record(_incident("inc-long", resolution_note="处置说明" * 500))
    text = render_context(cases.find("nginx 5xx", limit=1))
    assert "已截断" in text
    assert "原长" in text


# ============================================================
# 十一、/chat 接线：默认档行为不变（★ 回归）
# ============================================================
def test_chat_without_session_id_keeps_the_old_behaviour(client, chat_stub,
                                                        sessions, cases):
    """★ 不传 `session_id` 时，与加 M5b 之前**逐字段一致**。

    调用方（脚本、集成方）不应该因为"加了记忆"而改变行为 ——
    要记忆能力就显式传 `session_id`。**显式开关比"悄悄变了"好。**
    """
    r = client.post("/chat", json={"question": "磁盘满了怎么处理"})
    assert r.status_code == 200
    assert r.json() == {"answer": "答复-1"}, "响应形态不能变"

    assert len(chat_stub) == 1
    messages = chat_stub[0]
    assert [msg["role"] for msg in messages] == ["system", "user"], \
        "不传 session_id 时不允许注入任何额外上下文"
    assert messages[0]["content"] == "你是一个简洁的运维助手，回答不超过 100 字。"
    assert messages[1]["content"] == "磁盘满了怎么处理"

    # 一个字节都不落：不写会话、不记案例、不读历史
    assert sessions.counts()["turns"] == 0
    assert not sessions.path.exists() or sessions.path.read_text() == ""
    assert cases.counts()["total"] == 0


def test_chat_with_session_id_injects_marked_history_and_appends(client, chat_stub,
                                                                 sessions):
    """传了 `session_id`：第二轮必须带上第一轮，而且**带标注**（历史 + 时间）。"""
    client.post("/chat", json={"question": "第一问", "session_id": "s-abc"})
    assert len(chat_stub) == 1
    assert [msg["role"] for msg in chat_stub[0]] == ["system", "user"], \
        "第一轮没有历史可注入"

    client.post("/chat", json={"question": "第二问", "session_id": "s-abc"})
    second = chat_stub[1]
    assert [msg["role"] for msg in second] == ["system", "system", "user"]
    history = second[1]["content"]
    assert "第一问" in history and "答复-1" in history
    assert "历史对话" in history and "仅供参考" in history
    assert "2026-" in history or "20" in history, "历史对话也要带时间"
    assert "不要把上面助手的说法当成事实" in history

    # 回答之后才追加：这次请求的第三个参数就是刚落下的那一轮
    assert sessions.counts()["turns"] == 2
    assert [t["question"] for t in sessions.recent("s-abc")] == ["第一问", "第二问"]


def test_chat_does_not_append_a_half_turn_when_the_model_fails(client, sessions,
                                                               monkeypatch):
    """★ 模型失败时**不追加半轮**：

    否则历史里会出现"用户问了、助手没答"，下一次注入时模型会努力去补一个
    不存在的回答（而且它不知道自己缺了什么）。
    **会话记的是发生过的事，不是尝试过的事。**
    """
    from app.llm import ModelError

    def boom(messages, temperature=0.7, timeout=60):
        raise ModelError("模型挂了")

    monkeypatch.setattr(m, "llm_chat", boom)
    r = client.post("/chat", json={"question": "问一句", "session_id": "s-1"})
    assert r.status_code == 502
    assert sessions.counts()["turns"] == 0
    assert sessions.recent("s-1") == []


def test_chat_rejects_an_illegal_session_id_before_calling_the_model(client,
                                                                    chat_stub):
    """非法 id → 400，**而且一次模型都不调**（那笔钱不该花）。"""
    r = client.post("/chat", json={"question": "问一句", "session_id": "bad id!"})
    assert r.status_code == 400
    assert "session_id" in r.text
    assert chat_stub == [], "参数不合法不该先花钱调模型"


def test_chat_overlong_session_id_is_a_422_from_pydantic(client, chat_stub):
    """超过 64 字符在**进入业务代码之前**就被 pydantic 挡掉。"""
    r = client.post("/chat", json={"question": "q", "session_id": "a" * 65})
    assert r.status_code == 422
    assert chat_stub == []


def test_chat_injects_the_marked_case_context_when_cases_exist(client, chat_stub,
                                                               cases):
    """结过单的相似故障会被注入 —— **带全套警示**，且排在历史对话之前。"""
    cases.record(_incident("inc-1", summary="nginx 5xx 比例超过 5%",
                           resolution_note="清理邮件队列后恢复"))
    client.post("/chat", json={"question": "nginx 又开始 502 了",
                               "session_id": "s-1"})
    messages = chat_stub[0]
    assert [msg["role"] for msg in messages] == ["system", "system", "user"]
    case_block = messages[1]["content"]
    for needle in REQUIRED_WARNINGS:
        assert needle in case_block, f"注入给模型的案例丢了警示：{needle!r}"


def test_a_failing_case_lookup_never_breaks_the_chat(client, chat_stub, monkeypatch):
    """★ 案例是**旁路**：查不出来只留一条审计，问答照常完成。"""
    def boom(*a, **k):
        raise RuntimeError("案例库炸了")

    monkeypatch.setattr(case_memory, "find_similar", boom)
    r = client.post("/chat", json={"question": "问一句", "session_id": "s-1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": "答复-1"}
    assert "memory.cases_lookup_failed" in _audit_events(m.AUDIT_LOG)


# ============================================================
# 十二、/sessions 接口 + 审计
# ============================================================
def test_session_endpoints_round_trip(client, sessions):
    """看 / 列 / 清三个接口走一遍，而且**都留审计**。"""
    client.post("/chat", json={"question": "第一问", "session_id": "s-abc"})

    listed = client.get("/sessions").json()
    assert [r["session_id"] for r in listed["items"]] == ["s-abc"]
    assert listed["counts"]["turns"] == 1
    assert listed["config"]["max_turns"] == 10, "上限要报出来（否则没人知道离淘汰多远）"

    one = client.get("/sessions/s-abc").json()
    assert one["turns"] == 1
    assert "items" not in one, "默认不带对话正文"
    with_body = client.get("/sessions/s-abc", params={"include_turns": True}).json()
    assert [t["question"] for t in with_body["items"]] == ["第一问"]

    removed = client.delete("/sessions/s-abc").json()
    assert removed["cleared_turns"] == 1
    assert client.get("/sessions").json()["counts"]["turns"] == 0
    assert client.delete("/sessions/s-abc").json()["cleared_turns"] == 0

    events = _audit_events(m.AUDIT_LOG)
    for expected in ("session.turn", "session.view", "session.clear"):
        assert expected in events, f"新接口必须过 write_audit：缺 {expected}"


def test_unknown_session_reads_as_empty_not_404(client):
    """"没有这个会话"是正常状态（第一次请求之前它本来就不存在）。"""
    body = client.get("/sessions/s-never").json()
    assert body["turns"] == 0 and "items" not in body
    assert client.get("/sessions/s-never", params={"include_turns": True}
                      ).json()["items"] == []


@pytest.mark.parametrize("bad", ["bad id!", "会话"])
def test_session_endpoints_reject_illegal_ids_with_400(client, bad):
    """非法 id 是**参数问题**（400），不是"没有这个会话"（404）。

    ★ 这里不含带 `/` 的 id（如 `a/b`）：那种串在**路由层**就被拆成两段路径、
    直接 404 了，根本走不到本项目的校验 —— 那是框架行为，不是我们的判定。
    带 `/`/换行的 id 在 `require_session_id` 那一层仍然一律 `ValueError`
    （见上面 `test_invalid_session_ids_are_rejected`），
    而它真正会进来的地方是 `/chat` 的请求体：那条路返回 400。
    """
    assert client.get(f"/sessions/{bad}").status_code == 400
    assert client.request("DELETE", f"/sessions/{bad}").status_code == 400
    assert "session_id" in client.get(f"/sessions/{bad}").text


# ============================================================
# 十三、结单 → 记案例（接线）
# ============================================================
def test_resolving_an_incident_records_a_case(client, cases):
    """★ 事件 `resolved` 时自动沉淀成案例 —— 接线的端到端证明。

    接在 `/incidents/{id}/resolve` 这一处（全项目唯一能把事件推到 resolved
    的地方），存储层一行都没动：**存储不认识记忆**。
    """
    from app.incident import store as incident_store

    inc = incident_store.store().create(source="alert", host="web-01",
                                        service="nginx", severity="warning",
                                        summary="nginx 5xx 比例超过 5%")
    incident_store.store().ack(inc["id"], by="sre-zhang")
    r = client.post(f"/incidents/{inc['id']}/resolve",
                    json={"by": "sre-zhang", "note": "清理邮件队列后恢复"})
    assert r.status_code == 200, r.text

    assert cases.counts()["total"] == 1, "结单必须沉淀出一条案例"
    case = cases.get(inc["id"])
    assert case["service"] == "nginx"
    assert case["symptom"] == "nginx 5xx 比例超过 5%"
    assert case["conclusion"] == "清理邮件队列后恢复"
    assert case["ts"], "案例必须带时间"
    assert cases.find("nginx 5xx", limit=3), "刚记下的案例要能被马上检索到"
    assert "memory.case_recorded" in _audit_events(m.AUDIT_LOG)


def test_a_failing_case_store_never_breaks_the_resolution(client, monkeypatch):
    """★ 案例是旁路：记不进去只留审计，**结单必须成功**（同通知层的纪律）。"""
    from app.incident import store as incident_store

    def boom(*a, **k):
        raise RuntimeError("案例库炸了")

    monkeypatch.setattr(case_memory, "record_case", boom)
    inc = incident_store.store().create(source="alert", host="web-01",
                                        service="nginx", severity="warning",
                                        summary="测试")
    incident_store.store().ack(inc["id"], by="sre-zhang")
    r = client.post(f"/incidents/{inc['id']}/resolve",
                    json={"by": "sre-zhang", "note": "已恢复"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"
    assert "memory.case_record_failed" in _audit_events(m.AUDIT_LOG)


# ============================================================
# 十四、默认路径与单例
# ============================================================
def test_default_paths_follow_the_approvals_convention(monkeypatch):
    """默认落点与 `approvals` / `incident` 一致（`PROJECT_ROOT/logs/`）。

    运维只会去一个地方找日志。**这里只读路径常量，不碰真实 logs/**。
    """
    from app.llm import PROJECT_ROOT

    monkeypatch.setattr(session_memory, "_STORE", None)
    monkeypatch.delenv("SESSIONS_LOG", raising=False)
    monkeypatch.setattr(case_memory, "_STORE", None)
    monkeypatch.delenv("CASES_LOG", raising=False)
    assert session_memory.store() is session_memory.store(), "必须是进程级单例"
    assert session_memory.store().path == PROJECT_ROOT / "logs" / "sessions.jsonl"
    assert case_memory.store() is case_memory.store()
    assert case_memory.store().path == PROJECT_ROOT / "logs" / "cases.jsonl"
    assert not session_memory.store().path.exists(), \
        "只是取一下单例不该在真实 logs/ 下建文件（延迟创建）"
    assert not case_memory.store().path.exists()


def test_log_paths_can_be_redirected_by_environment(monkeypatch, tmp_path):
    """`SESSIONS_LOG` / `CASES_LOG` 让落点可配（部署到只读目录时用得上）。"""
    monkeypatch.setattr(session_memory, "_STORE", None)
    monkeypatch.setenv("SESSIONS_LOG", str(tmp_path / "elsewhere" / "s.jsonl"))
    monkeypatch.setattr(case_memory, "_STORE", None)
    monkeypatch.setenv("CASES_LOG", str(tmp_path / "elsewhere" / "c.jsonl"))
    assert session_memory.store().path == tmp_path / "elsewhere" / "s.jsonl"
    assert case_memory.store().path == tmp_path / "elsewhere" / "c.jsonl"
    session_memory.store().append("s-1", "q", "a")
    assert (tmp_path / "elsewhere" / "s.jsonl").exists(), "配了就该写到配的地方去"


def test_describe_says_what_is_actually_on(sessions, cases):
    """`describe()` 必须如实报出当前配置（免得以为开了什么、其实关着）。"""
    s = sessions.describe()
    assert s["max_turns"] == 10 and "SESSIONS" not in str(s)
    assert "evicted" in s["note"]
    c = cases.describe()
    assert c["max_cases"] == case_memory.DEFAULT_MAX_CASES
    assert "参考" in c["note"]


def test_module_level_entry_points_use_the_singleton(cases, monkeypatch):
    """`record_case` / `find_similar` / `counts` 三个模块级入口就是单例的转发。"""
    assert find_similar("nginx") == []
    assert case_memory.counts()["total"] == 0
    assert case_memory.record_case(_incident())["recorded"] == "inc-0001"
    assert case_memory.counts()["total"] == 1
    assert find_similar("nginx 5xx", limit=1)[0]["incident_id"] == "inc-0001"
