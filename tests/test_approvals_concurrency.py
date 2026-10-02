# -*- coding: utf-8 -*-
"""M3：审批单的跨进程安全、有效期、以及执行结果的如实回写。

这个文件里的用例全部冲着**已经在审计里实证过的缺陷**去：

    C2  两个进程各持一张 APPROVED → 同一条写命令执行两次
    C3  三天前批准的单今天仍能执行（审批有 TTL，执行却不看它）
    C4  `approvals.jsonl` 里的 result_ok 写着成功，而实际失败

C2 那组**必须用真进程**：线程锁在同一进程内就能骗过测试，
只有另起一个进程才能复现"两个进程各自折叠出一张 APPROVED"这个前提。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.sandbox import approvals

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 装置
# ============================================================
@pytest.fixture
def store(tmp_path):
    return approvals.ApprovalStore(path=tmp_path / "approvals.jsonl")


def _make_approved(store, ttl=approvals.DEFAULT_TTL_SECONDS, fingerprint="fp-abc123"):
    rec = store.create(command="truncate -s 0 /var/log/nginx/error.log",
                       fingerprint=fingerprint, rule="truncate",
                       risk="reversible", isolation="container",
                       reason="清理写满的日志", ttl=ttl)
    store.approve(rec["id"], by="sre-zhang")
    return store.get(rec["id"])


def _make_approved_then_expired(store, fingerprint="fp-abc123"):
    """造一张"**已经批准、但有效期已过**"的单子。

    ★ 为什么不能直接用 `create(ttl=-10)`：
      那种单子根本**批不了** —— `_expire_locked` 会先把它判成 expired，
      approve 直接报"当前状态是 expired"。这是正确行为（没人会去批一张
      出生就过期的单子），所以只能用"先正常批准、再把有效期改成过去"的办法
      来复现"批准之后放了三天"这个真实场景。
    """
    rec = _make_approved(store, fingerprint=fingerprint)

    # 改写日志里那条 created 事件的 expires_at，然后重新折叠
    events = [json.loads(line) for line
              in store.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for ev in events:
        if ev["event"] == "created" and ev["id"] == rec["id"]:
            ev["expires_at"] = "2020-01-01T00:00:00"
    store.path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")

    fresh = approvals.ApprovalStore(path=store.path)
    got = fresh.get(rec["id"])
    assert got["status"] == approvals.APPROVED, "前提没造出来：它应该是已批准状态"
    return fresh, got


# ============================================================
# 一、C2：跨进程只允许执行一次
# ============================================================
_CHILD = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, r"{root}")
from app.sandbox import approvals

path = Path(r"{log}")
st = approvals.ApprovalStore(path=path)
# ★ 关键前提：先把状态折进**内存**，再等其它进程动这个文件。
#   这正是缺陷的成因 —— 每个进程都拿着一份"看起来还是 approved"的快照。
st.get("{rid}")

go = Path(r"{go}")
deadline = time.time() + 30
while not go.exists() and time.time() < deadline:
    time.sleep(0.005)

try:
    st.consume("{rid}", expected_fingerprint="{fp}", by="child")
    print("CONSUMED")
except approvals.ApprovalError as exc:
    print("REJECTED:" + str(exc)[:80])
except Exception as exc:                      # pragma: no cover
    print("ERROR:" + type(exc).__name__ + ":" + str(exc)[:60])
'''


def test_two_real_processes_cannot_both_consume_the_same_ticket(tmp_path, store):
    """★ C2 的正面回归：两个**真进程**抢同一张单，只能有一个成功。

    用一个 "go 文件" 当发令枪：两个子进程都先折叠好状态、都在等这个文件，
    文件一出现它们几乎同时去 consume。

      修复前：两个进程各自内存里都是 approved → **两个都打印 CONSUMED**
            （一条写命令被执行两次，而审批只批了一次）
      修复后：判定与写入都在跨进程锁里，且判定前重新折叠 →
            第二个进程看到 consumed，打印 REJECTED
    """
    rec = _make_approved(store)
    go = tmp_path / "go"
    code = _CHILD.format(root=str(PROJECT_ROOT), log=str(store.path),
                         rid=rec["id"], fp=rec["fingerprint"], go=str(go))

    procs = [subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, cwd=str(PROJECT_ROOT))
             for _ in range(2)]
    time.sleep(0.6)                    # 给两个子进程时间把状态折进内存
    go.write_text("go", encoding="utf-8")

    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        outs.append((out or "").strip() or f"<no stdout> {err[-200:]}")

    consumed = [o for o in outs if o.startswith("CONSUMED")]
    rejected = [o for o in outs if o.startswith("REJECTED")]
    assert len(consumed) == 1, f"应当恰好一个成功，实际：{outs}"
    assert len(rejected) == 1, f"另一个必须被拒，实际：{outs}"

    # 日志里也只能有一条 consumed 事件（不能只是"有个进程没报成功"）
    events = [json.loads(line) for line
              in store.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len([e for e in events if e["event"] == "consumed"]) == 1
    assert approvals.ApprovalStore(path=store.path).get(rec["id"])["status"] == \
        approvals.CONSUMED


def test_consumed_state_written_by_another_process_is_seen(store):
    """另一个进程改了日志之后，本进程必须能看见（重新折叠）。

    这条是 C2 的"另一半"：光有文件锁不够 ——
    锁只保证同一时刻只有一个人写；如果判定用的是**昨天折出来的状态**，
    照样会把一张已被别人消费过的单子再消费一次。
    """
    rec = _make_approved(store)
    other = approvals.ApprovalStore(path=store.path)
    other.consume(rec["id"], expected_fingerprint=rec["fingerprint"], by="other")
    with pytest.raises(approvals.ApprovalError):
        store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])


# ============================================================
# 二、跨进程锁本身的边界
# ============================================================
def test_fresh_lock_blocks_and_times_out(tmp_path):
    """锁被一个**活着**的进程持有时，别人不能进来（超时后如实报错）。"""
    store = approvals.ApprovalStore(path=tmp_path / "a.jsonl")
    lock_path = store.path.with_suffix(store.path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999 1700000000", encoding="utf-8")   # 新鲜的锁

    fast = approvals._FileLock(lock_path, stale_after=600, timeout=0.3, poll=0.05)
    with pytest.raises(approvals.ApprovalError):
        with fast:
            pass


def test_stale_lock_is_taken_over(tmp_path):
    """★ 抢锁的进程崩了（kill -9 / 断电）时，锁**不能永久卡住**系统。

    不认陈旧锁的保护机制，比没有保护更糟：它会把后续所有审批挡在门外，
    而且没人知道为什么。
    """
    store = approvals.ApprovalStore(path=tmp_path / "a.jsonl")
    lock_path = store.path.with_suffix(store.path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("12345 1700000000", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock_path, (old, old))            # 一小时前留下的锁 → 陈旧

    lock = approvals._FileLock(lock_path, stale_after=60, timeout=1.0)
    with lock:
        assert lock_path.exists()              # 已被本进程接管
    assert not lock_path.exists(), "释放后必须删掉锁文件"


def test_lock_is_released_even_when_the_body_raises(tmp_path):
    """临界区里抛异常也必须放锁 —— 否则一次业务错误会锁死整个审批。"""
    lock_path = tmp_path / "x.lock"
    lock = approvals._FileLock(lock_path, timeout=1.0)
    with pytest.raises(RuntimeError):
        with lock:
            raise RuntimeError("boom")
    assert not lock_path.exists()
    with approvals._FileLock(lock_path, timeout=1.0):     # 还能再拿到
        pass


# ============================================================
# 三、C3：有效期
# ============================================================
def test_expired_approved_ticket_cannot_be_executed(store):
    """一条三天前批准的命令，今天的环境已经和当时不是同一回事了。"""
    fresh, rec = _make_approved_then_expired(store)

    with pytest.raises(approvals.ApprovalError) as ei:
        fresh.check_executable(rec["id"])
    assert "过期" in str(ei.value) or "有效期" in str(ei.value)


def test_expiry_is_folded_into_the_ticket_with_a_reason(store):
    """拒绝之后单据要变成 expired 并留下理由 —— 不能只是"报错但状态没变"。"""
    fresh, rec = _make_approved_then_expired(store)
    with pytest.raises(approvals.ApprovalError):
        fresh.check_executable(rec["id"])

    after = fresh.get(rec["id"])
    assert after["status"] == approvals.EXPIRED
    assert "有效期" in (after.get("expired_reason") or "")

    # 重启折叠之后仍然是 expired（不是只在内存里改了状态）
    reborn = approvals.ApprovalStore(path=store.path)
    assert reborn.get(rec["id"])["status"] == approvals.EXPIRED


def test_expired_ticket_cannot_be_consumed_either(store):
    """`consume` 那条老路也一样挡住 —— 两个入口不能一个严一个松。"""
    fresh, rec = _make_approved_then_expired(store)
    with pytest.raises(approvals.ApprovalError):
        fresh.consume(rec["id"], expected_fingerprint=rec["fingerprint"])


def test_broken_expiry_field_does_not_lock_the_ticket(store):
    """时间戳坏了 → **不当作过期**。

    宁可多执行一次"人工已经批准过"的命令，也不要因为一个坏字段
    把正常审批永久卡死（那是可用性事故，而且很难查）。
    """
    rec = _make_approved(store)
    path = store.path
    lines = path.read_text(encoding="utf-8").splitlines()
    fixed = []
    for line in lines:
        ev = json.loads(line)
        if ev["event"] == "created":
            ev["expires_at"] = "不是一个时间"
        fixed.append(json.dumps(ev, ensure_ascii=False))
    path.write_text("\n".join(fixed) + "\n", encoding="utf-8")

    fresh = approvals.ApprovalStore(path=path)
    assert fresh.check_executable(rec["id"])["status"] == approvals.APPROVED


def test_fresh_ticket_still_passes_check_executable(store):
    """★ 正例不能少：加固之后"没过期的单子照样能执行"。"""
    rec = _make_approved(store)
    got = store.check_executable(rec["id"], expected_fingerprint=rec["fingerprint"])
    assert got["status"] == approvals.APPROVED


def test_fingerprint_mismatch_still_refused_by_check_executable(store):
    rec = _make_approved(store)
    with pytest.raises(approvals.ApprovalError) as ei:
        store.check_executable(rec["id"], expected_fingerprint="fp-other")
    assert "指纹" in str(ei.value)


# ============================================================
# 四、C4：执行结果如实回写
# ============================================================
def test_result_writeback_overrides_the_optimistic_placeholder(store):
    """★ C4 的正面回归：`consume` 写的 ok 是乐观占位，真值由 executed 覆盖。"""
    rec = _make_approved(store)
    store.consume(rec["id"], expected_fingerprint=rec["fingerprint"], ok=True)
    assert store.get(rec["id"])["result_ok"] is True
    assert store.get(rec["id"])["result_provisional"] is True, \
        "consume 写下的值必须被标成占位，否则没人知道它还不是真值"

    done = store.record_result(rec["id"], ok=False, exit_code=1,
                               elapsed_ms=12, error="Permission denied")
    assert done["result_ok"] is False, "真实结果没覆盖掉乐观占位"
    assert done["result_provisional"] is False
    assert done["exit_code"] == 1 and done["result_error"] == "Permission denied"


def test_writeback_survives_restart(store):
    rec = _make_approved(store)
    store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])
    store.record_result(rec["id"], ok=False, exit_code=127,
                        elapsed_ms=5, error="command not found")
    reborn = approvals.ApprovalStore(path=store.path)
    got = reborn.get(rec["id"])
    assert got["result_ok"] is False and got["exit_code"] == 127


def test_result_writeback_does_not_reopen_the_state_machine(store):
    """回写是"补充事实"，**不是状态迁移** —— 终态仍然不可逆。"""
    rec = _make_approved(store)
    store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])
    store.record_result(rec["id"], ok=True, exit_code=0, elapsed_ms=3)
    assert store.get(rec["id"])["status"] == approvals.CONSUMED
    with pytest.raises(approvals.ApprovalError):
        store.approve(rec["id"], by="sre-li")          # 终态不能被批准
    with pytest.raises(approvals.ApprovalError):
        store.consume(rec["id"])                       # 也不能再执行


def test_history_shows_the_whole_story(store):
    """时间线要能讲清"批了、执行了、结果是失败"这整件事。"""
    rec = _make_approved(store)
    store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])
    store.record_result(rec["id"], ok=False, exit_code=1, error="boom")
    events = [h["event"] for h in store.get(rec["id"])["history"]]
    assert events == ["created", "approved", "consumed", "executed"]


# ============================================================
# 五、M2-1：那个骗人的上限常量
# ============================================================
def test_max_records_is_not_a_lying_constant():
    """`MAX_RECORDS` 曾经注释成"内存里最多保留多少条"，但全仓库零引用。

    **一个说了不做的常量比没有这个常量更糟**：读代码的人会据此认为
    内存是有界的，于是不去做轮转、不去看增长。
    现在它如实是 None，并在模块里写清了"内存 = O(全部历史)，靠轮转解决"。
    """
    assert approvals.MAX_RECORDS is None
    source = Path(approvals.__file__).read_text(encoding="utf-8")
    assert "MAX_RECORDS" in source
    assert "零引用" in source or "全仓库" in source, \
        "要把『它其实没生效』这件事写在代码里，而不是只留一个名字"
