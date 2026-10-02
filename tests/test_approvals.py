# -*- coding: utf-8 -*-
"""审批状态机 —— 写操作的唯一闸门。

测试全部围绕三件事：

    · **不能重复做**：一张单只能消费一次（防重放）
    · **不能做没批过的**：批准的和执行的必须是同一条命令（防 TOCTOU）
    · **状态要能重建**：靠折叠追加日志得到，进程重启后必须一模一样

这一层如果坏了，后果不是"功能不可用"，而是"审批形同虚设"——
一次批准执行一百次，或者批 A 执行 B。所以这里的用例密度刻意高于别处。
"""

import json

import pytest

from app.sandbox import approvals

# ============================================================
# 一、创建
# ============================================================
def test_create_starts_pending(new_approval):
    rec = new_approval()
    assert rec["status"] == approvals.PENDING
    assert rec["id"].startswith("ap-")
    # 时间戳在**当前进程内**就必须有值 —— 见 _append 的说明：
    # 曾经只写盘不回填，导致这里全是 None，而重启后又"自愈"。
    assert rec["created_at"], "created_at 在内存里丢了（只写盘没回填）"
    assert rec["expires_at"]
    assert rec["history"][0]["ts"], "history 里的时间戳同样不能丢"


def test_in_memory_record_matches_what_was_persisted(new_approval, approval_store):
    """同一份数据有两个来源（内存 / 盘）时，两个来源必须一致。

    这条是冲着上面那个"重启就自愈"的坑去的：只要两边对不上，就必须红。
    """
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    on_disk = [json.loads(line) for line
               in approval_store.path.read_text(encoding="utf-8").splitlines() if line]
    created = next(e for e in on_disk if e["event"] == "created")
    approved = next(e for e in on_disk if e["event"] == "approved")
    got = approval_store.get(rec["id"])
    assert got["created_at"] == created["ts"]
    assert got["approved_at"] == approved["ts"]


def test_create_records_what_will_actually_be_executed(new_approval):
    """审批界面显示的东西，必须是执行时会用到的东西。"""
    rec = new_approval()
    for field in ("command", "fingerprint", "rule", "risk", "isolation", "reason"):
        assert rec.get(field), f"审批单缺少 {field}"


def test_created_approval_is_written_to_the_log_file(new_approval, approval_store):
    """先落盘再更新内存 —— 反过来的话，内存说"批准了"而盘上没有，重启就丢。"""
    new_approval()
    lines = approval_store.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "created"


# ============================================================
# 二、批准：必须有人
# ============================================================
def test_approve_without_a_person_is_rejected(approval_store, new_approval):
    """没有审批人的审批单等于没有审批 —— 事故复盘时"谁批的"必须有答案。"""
    rec = new_approval()
    for blank in ("", "   ", None):
        with pytest.raises(approvals.ApprovalError):
            approval_store.approve(rec["id"], by=blank)
    assert approval_store.get(rec["id"])["status"] == approvals.PENDING


def test_approve_records_who_and_when(approval_store, new_approval):
    rec = new_approval()
    approval_store.approve(rec["id"], by=" sre-zhang ", note="确认过日志已归档")
    got = approval_store.get(rec["id"])
    assert got["status"] == approvals.APPROVED
    assert got["approved_by"] == "sre-zhang"      # 前后空格要去掉
    assert got["approved_at"] and got["approve_note"]


def test_cannot_approve_twice(approval_store, new_approval):
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    with pytest.raises(approvals.ApprovalError):
        approval_store.approve(rec["id"], by="sre-li")


# ============================================================
# 三、消费：一次性 + 指纹比对
# ============================================================
def test_consume_requires_prior_approval(approval_store, new_approval):
    rec = new_approval()
    with pytest.raises(approvals.ApprovalError):
        approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])


def test_consume_once_then_replay_is_rejected(approval_store, new_approval):
    """★ 「已执行的审批单被重放」是这类系统最典型的漏洞。"""
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"],
                           by="sre-zhang")
    with pytest.raises(approvals.ApprovalError):
        approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"],
                              by="sre-zhang")


def test_consume_with_wrong_fingerprint_is_rejected_and_changes_nothing(
        approval_store, new_approval):
    """批 A 执行 B 必须被拦下，**且不能把单子浪费掉**（状态保持 approved）。"""
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    with pytest.raises(approvals.ApprovalError):
        approval_store.consume(rec["id"], expected_fingerprint="fp-someone-else")
    assert approval_store.get(rec["id"])["status"] == approvals.APPROVED
    # 指纹对了仍然可以正常执行
    approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])
    assert approval_store.get(rec["id"])["status"] == approvals.CONSUMED


def test_consume_without_fingerprint_is_allowed_when_not_supplied(
        approval_store, new_approval):
    """`expected_fingerprint` 是可选的（内部调用方可以省略）。

    这里记录的是当前语义：**不传就不比对**。所以凡是走 HTTP 的执行路径
    都必须传 —— 那条路径在 main.py 里传了（见 audit 报告的 C2/C4 记录）。
    """
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    assert approval_store.consume(rec["id"])["status"] == approvals.CONSUMED


def test_consume_records_result_as_passed_in(approval_store, new_approval):
    """记录执行结果。

    ★ 已知问题（见 docs/redev/01-audit.md C4）：调用方 `main.py` 是在
    **执行之前**就 `consume(ok=True)` 的，所以这里记下的 `result_ok`
    不反映真实执行结果（真值在 audit.jsonl 与 sandbox span 里）。
    这条用例固化当前行为，M3 会改成"执行后回写真实结果"——
    到那时这条用例要跟着改，而不是被删掉。
    """
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    done = approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"],
                                  by="sre-zhang", ok=True)
    assert done["result_ok"] is True
    assert done["consumed_by"] == "sre-zhang"


# ============================================================
# 四、驳回与终态不可逆
# ============================================================
def test_reject_is_terminal(approval_store, new_approval):
    rec = new_approval()
    approval_store.reject(rec["id"], by="sre-li", note="这个日志还有用")
    assert approval_store.get(rec["id"])["status"] == approvals.REJECTED
    with pytest.raises(approvals.ApprovalError):
        approval_store.approve(rec["id"], by="sre-zhang")
    with pytest.raises(approvals.ApprovalError):
        approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])


def test_rejected_cannot_be_rejected_again(approval_store, new_approval):
    rec = new_approval()
    approval_store.reject(rec["id"], by="sre-li")
    with pytest.raises(approvals.ApprovalError):
        approval_store.reject(rec["id"], by="sre-li")


def test_consumed_is_terminal_even_against_later_events(approval_store, new_approval):
    """终态必须不可逆：否则会出现"已执行的单子又被驳回"这种说不通的状态。"""
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])
    with pytest.raises(approvals.ApprovalError):
        approval_store.reject(rec["id"], by="sre-li")


# ============================================================
# 五、过期
# ============================================================
def test_expired_cannot_be_approved(approval_store, new_approval):
    rec = new_approval(ttl=-5)          # 一开出来就已经过期
    assert approval_store.get(rec["id"])["status"] == approvals.EXPIRED
    with pytest.raises(approvals.ApprovalError):
        approval_store.approve(rec["id"], by="sre-zhang")


def test_expiry_is_evaluated_on_access(new_approval, approval_store):
    """过期检查不能只在"有人访问"时才做 —— 值班的人不该看到早就该死的单子。"""
    new_approval(ttl=0)
    counts = approval_store.counts()
    assert counts.get(approvals.EXPIRED, 0) >= 1


# ============================================================
# 六、状态重建（折叠日志）
# ============================================================
def test_state_is_rebuilt_from_the_log_after_restart(approval_store, new_approval):
    """进程重启后状态必须一模一样 —— 这是"用追加日志而不是 UPDATE"的意义。"""
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")

    reborn = approvals.ApprovalStore(path=approval_store.path)
    got = reborn.get(rec["id"])
    assert got["status"] == approvals.APPROVED
    assert got["approved_by"] == "sre-zhang"
    assert got["command"] == rec["command"]
    # 重启后依然能正常执行，且依然只能执行一次
    reborn.consume(rec["id"], expected_fingerprint=rec["fingerprint"])
    with pytest.raises(approvals.ApprovalError):
        reborn.consume(rec["id"], expected_fingerprint=rec["fingerprint"])


def test_replay_protection_survives_restart(approval_store, new_approval):
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"])

    reborn = approvals.ApprovalStore(path=approval_store.path)
    assert reborn.get(rec["id"])["status"] == approvals.CONSUMED
    with pytest.raises(approvals.ApprovalError):
        reborn.consume(rec["id"])


def test_a_corrupt_line_does_not_lose_the_rest(approval_store, new_approval):
    """一行坏数据不该丢掉整份历史。"""
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    with open(approval_store.path, "a", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON\n")
        f.write("\n")

    reborn = approvals.ApprovalStore(path=approval_store.path)
    assert reborn.get(rec["id"])["status"] == approvals.APPROVED


def test_missing_file_is_not_an_error(tmp_path):
    store = approvals.ApprovalStore(path=tmp_path / "nope.jsonl")
    assert store.list() == []
    assert store.counts()["total"] == 0


# ============================================================
# 七、查询
# ============================================================
def test_get_unknown_id_raises(approval_store):
    with pytest.raises(approvals.ApprovalError):
        approval_store.get("ap-nope")


def test_list_filters_by_status_and_honours_limit(approval_store, new_approval):
    a = new_approval()
    b = new_approval()
    c = new_approval()
    approval_store.approve(a["id"], by="sre-zhang")
    approval_store.reject(b["id"], by="sre-li")

    pending = approval_store.list(status=approvals.PENDING)
    assert [r["id"] for r in pending] == [c["id"]]

    approved = approval_store.list(status=approvals.APPROVED)
    assert [r["id"] for r in approved] == [a["id"]]

    assert len(approval_store.list(limit=2)) == 2
    assert len(approval_store.list()) == 3


def test_counts_are_consistent(approval_store, new_approval):
    a, b, c = new_approval(), new_approval(), new_approval()
    approval_store.approve(a["id"], by="sre-zhang")
    approval_store.reject(b["id"], by="sre-li")

    counts = approval_store.counts()
    assert counts["total"] == 3
    assert counts.get(approvals.PENDING) == 1
    assert counts.get(approvals.APPROVED) == 1
    assert counts.get(approvals.REJECTED) == 1
    assert c["id"]  # 第三张单仍是 pending，未被动过


def test_history_records_every_transition(approval_store, new_approval):
    """可审计 = 每一次状态变化都留痕，而不是只留最终结果。"""
    rec = new_approval()
    approval_store.approve(rec["id"], by="sre-zhang")
    approval_store.consume(rec["id"], expected_fingerprint=rec["fingerprint"],
                           by="sre-zhang")
    events = [h["event"] for h in approval_store.get(rec["id"])["history"]]
    assert events == ["created", "approved", "consumed"]
