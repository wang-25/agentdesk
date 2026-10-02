# -*- coding: utf-8 -*-
"""事件（Incident）—— 把 500 条告警记成 1 个故障，并且每一步都有主。

用例围绕三件事（和 `tests/test_approvals.py` 同一个骨架，因为守的是同一条底线）：

    · **状态不能乱走**：`open → ack → resolved`，复发走 `reopened`；
      剩下 22 种组合全部拒绝。判定的正确性靠穷举，不靠"看代码觉得对"。
    · **每一步都有名字**：`ack` / `resolve` 不填 `by` 一律拒，
      和审批单"批准必须填审批人"是同一条原则 ——
      事故复盘时"谁处理的"不能是空字符串。
    · **重启后一模一样**：状态靠折叠追加日志得到，而内存里的时间戳
      必须是**同一个值**（M1 那个"只在内存里丢时间戳、重启自愈"的坑
      在这里有正面回归用例）。

另外还有一条只有事件才有的：**收敛不能丢信息**。
同一 fingerprint 的告警反复推来，成员列表只占一行、计数 +1 ——
既不会让一个事件里躺着三千条重复记录，也不会丢掉"它反复响了三千次"。

零成本：不联网（conftest 的 autouse 守卫）、不调模型、不碰真实 `logs/`
（每个用例用 `tmp_path` 造自己的 `IncidentStore`）。
"""

import json

import pytest

from app.incident import model
from app.incident.store import IncidentStore, alert_fingerprint

# ============================================================
# 装置
# ============================================================
@pytest.fixture
def incidents(tmp_path):
    """临时事件存储 —— 绝不写 logs/incidents.jsonl。"""
    return IncidentStore(path=tmp_path / "incidents.jsonl")


def _new(incidents, **over):
    """造一个合法的事件入参（默认是"一次 nginx 5xx 故障"）。"""
    payload = {"source": "alert", "host": "web-01", "service": "nginx",
               "severity": "warning", "summary": "nginx 5xx 比例超过 5%"}
    payload.update(over)
    return incidents.create(**payload)


def _alert(fingerprint="fp-001", **over):
    """造一条归一化后的告警（字段集与 app.main.normalize_alerts 一致）。"""
    alert = {"fingerprint": fingerprint, "alertname": "NginxHighErrorRate",
             "host": "web-01", "service": "nginx", "severity": "critical"}
    alert.update(over)
    return alert


def _events(rec):
    return [t["event"] for t in rec["timeline"]]


def _disk_rows(incidents):
    """盘上的事件流（一行一个事件）。用来核对"内存与盘一致"。"""
    return [json.loads(line) for line
            in incidents.path.read_text(encoding="utf-8").splitlines() if line]


# ============================================================
# 一、状态机的纯函数部分（穷举）
# ============================================================
@pytest.mark.parametrize("current,event", [
    ("", model.EVENT_CREATED),                            # 还没有记录时创建
    (None, model.EVENT_CREATED),
    (model.STATUS_OPEN, model.EVENT_ACK),
    (model.STATUS_REOPENED, model.EVENT_ACK),             # 复发后重新认领
    (model.STATUS_ACK, model.EVENT_RESOLVED),
    (model.STATUS_RESOLVED, model.EVENT_REOPENED),        # 故障会复发
])
def test_can_transition_allows_only_the_documented_moves(current, event):
    assert model.can_transition(current, event) is True


@pytest.mark.parametrize("current,event", [
    (model.STATUS_OPEN, model.EVENT_CREATED),      # 重复的 created 不能重置历史
    (model.STATUS_OPEN, model.EVENT_RESOLVED),     # ★ 有意禁止：必须先 ack
    (model.STATUS_ACK, model.EVENT_ACK),           # 重复认领 = 两个人抢同一个故障
    (model.STATUS_RESOLVED, model.EVENT_ACK),      # 终态不能再认领，请先 reopen
    (model.STATUS_REOPENED, model.EVENT_RESOLVED),  # 复发后也要先重新认领
    ("something-else", model.EVENT_ACK),           # 认不出的状态：fail-closed
    # "没结过单就想 reopen"在下面 test_reopen_only_from_resolved... 里从 store 侧测
])
def test_can_transition_rejects_everything_else(current, event):
    assert model.can_transition(current, event) is False


# ============================================================
# 二、创建：内存里就必须有值
# ============================================================
def test_create_starts_open_and_stamps_the_time_in_memory(incidents):
    """★ M1 那个坑的正面回归。

    `_append` 曾经写成 `event = {"ts": _now(), **event}`（只改局部变量），
    于是紧跟其后的 `_apply(ev)` 拿不到 `ts`：**当前进程内** `created_at` 是 None，
    而重启后从盘上折叠又好了（"自愈"），极难复现。
    这里的三条断言就是钉住这一点。
    """
    ids = {model.new_id() for _ in range(20)}
    assert len(ids) == 20, "inc- id 撞了"
    assert all(i.startswith("inc-") and len(i) == 12 for i in ids)

    rec = _new(incidents)
    assert rec["status"] == model.STATUS_OPEN
    assert rec["id"].startswith("inc-")
    assert rec["created_at"], "created_at 在内存里丢了（_append 只写盘、没回填 ts）"
    assert rec["updated_at"] == rec["created_at"]
    assert rec["timeline"][0]["event"] == model.EVENT_CREATED
    assert rec["timeline"][0]["ts"] == rec["created_at"]
    assert [h["event"] for h in rec["history"]] == [model.EVENT_CREATED]
    assert rec["owner"] == "" and rec["acked_at"] is None and rec["diagnosis"] is None


def test_in_memory_record_matches_what_was_persisted(incidents):
    """同一份数据有两个来源（内存 / 盘）时，两个来源必须拿到同一个值。"""
    rec = _new(incidents)
    incidents.ack(rec["id"], by="sre-zhang", note="我在看")
    incidents.resolve(rec["id"], by="sre-zhang", note="扩容后恢复")

    rows = _disk_rows(incidents)
    assert len(rows) == 3
    assert rows[0]["event"] == model.EVENT_CREATED, "第一条落盘的必须是 created"
    created, acked, resolved = rows

    got = incidents.get(rec["id"])
    assert got["created_at"] == created["ts"]
    assert got["acked_at"] == acked["ts"]
    assert got["resolved_at"] == resolved["ts"]
    assert got["updated_at"] == resolved["ts"], "updated_at 就是最后一条事件的 ts"


@pytest.mark.parametrize("given", ["high", "unknown", "", None])
def test_an_unknown_severity_is_stored_as_warning(incidents, given):
    """认不出的等级按 warning：既不落到 info（漏报），也不升到 critical（全站拉响）。"""
    rec = _new(incidents, severity=given)
    assert rec["severity"] == "warning"
    assert rec["severity"] in model.SEVERITY_ORDER


# ============================================================
# 三、等级归并（severity_of 是纯函数，独立测）
# ============================================================
def test_severity_of_takes_the_highest_never_the_average():
    members = [_alert(f"fp-{i}", severity=s)
               for i, s in enumerate(("info", "warning", "critical"))]
    assert model.severity_of(members) == "critical"
    assert model.severity_of(list(reversed(members))) == "critical", "与到达顺序无关"
    assert model.severity_of(members[:2]) == "warning"
    assert model.severity_of(members[:1]) == "info"


def test_severity_of_falls_back_to_warning_instead_of_guessing():
    assert model.severity_of([]) == "warning"
    assert model.severity_of(None) == "warning"  # type: ignore[arg-type]
    for unknown in ("high", "unknown", "", None, "CRITICAL!", 3):
        assert model.severity_of([{"severity": unknown}]) == "warning", unknown
    # 认不出的成员不能拖累已知的最高档
    assert model.severity_of([{"severity": "bogus"}, {"severity": "critical"}]) == "critical"
    # 垃圾成员要跳过而不是崩（告警入口一抛，那条告警就永远消失了）
    assert model.severity_of([None, "x", {"severity": "critical"}]) == "critical"  # type: ignore[list-item]
    assert model.severity_of([None, "x"]) == "warning"  # type: ignore[list-item]


# ============================================================
# 四、状态迁移（store 层：非法的一律 IncidentError，且不许改状态）
# ============================================================
def test_open_cannot_be_resolved_directly(incidents):
    """★ 有意的取舍：必须先 `ack` 才能结单。

    代价：自愈类告警也要先 `ack(by="auto")` 一次。
    换来：**每一个被关掉的事件都有名字** —— 复盘时"谁处理的"必须有答案。
    """
    rec = _new(incidents)
    with pytest.raises(model.IncidentError):
        incidents.resolve(rec["id"], by="sre-zhang", note="自己好了")
    got = incidents.get(rec["id"])
    assert got["status"] == model.STATUS_OPEN, "被拒的迁移不能留下任何痕迹"
    assert got["resolved_at"] is None


def test_duplicate_ack_and_ack_after_resolution_are_rejected(incidents):
    rec = _new(incidents)
    incidents.ack(rec["id"], by="sre-zhang")
    with pytest.raises(model.IncidentError):
        incidents.ack(rec["id"], by="sre-li")          # 抢单
    incidents.resolve(rec["id"], by="sre-zhang")
    with pytest.raises(model.IncidentError):
        incidents.ack(rec["id"], by="sre-li")          # 终态不能再认领
    with pytest.raises(model.IncidentError):
        incidents.resolve(rec["id"], by="sre-zhang")   # 也不能重复结单
    got = incidents.get(rec["id"])
    assert got["owner"] == "sre-zhang"
    assert [h["event"] for h in got["history"]] == ["created", "ack", "resolved"]


def test_reopen_only_from_resolved_and_clears_the_last_resolution(incidents):
    rec = _new(incidents)
    with pytest.raises(model.IncidentError):
        incidents.reopen(rec["id"], reason="还没结过单")

    incidents.ack(rec["id"], by="sre-zhang")
    incidents.resolve(rec["id"], by="sre-zhang", note="重启服务后恢复")
    again = incidents.reopen(rec["id"], reason="同源告警 DiskSpaceLow 再次触发")

    assert again["status"] == model.STATUS_REOPENED
    assert again["reopened_at"]
    # 顶层字段代表"当前状态"：还挂着 resolved_at 是自相矛盾的。
    # 但那一次结单不丢 —— 它在 timeline / history 里。
    assert again["resolved_at"] is None and again["resolved_by"] == ""
    assert again["resolution_note"] == ""
    assert [h["event"] for h in again["history"]] == \
        ["created", "ack", "resolved", "reopened"]
    assert [t["note"] for t in again["timeline"] if t["event"] == "resolved"] == \
        ["重启服务后恢复"]


def test_a_reopened_incident_can_be_acked_and_resolved_again(incidents):
    rec = _new(incidents)
    incidents.ack(rec["id"], by="sre-zhang")
    incidents.resolve(rec["id"], by="sre-zhang")
    incidents.reopen(rec["id"], reason="又响了")

    back = incidents.ack(rec["id"], by="sre-li")
    assert back["status"] == model.STATUS_ACK
    assert back["owner"] == "sre-li", "顶层 owner 是【当前】负责人，旧的留在时间线上"
    done = incidents.resolve(rec["id"], by="sre-li", note="换了台机器")
    assert done["status"] == model.STATUS_RESOLVED
    assert done["resolution_note"] == "换了台机器"
    assert done["reopened_at"], "复发过这件事不能被抹掉"
    assert [h["by"] for h in done["history"]] == \
        ["alert", "sre-zhang", "sre-zhang", "alert", "sre-li", "sre-li"]


def test_ack_and_resolve_without_a_person_are_rejected(incidents):
    """没有名字的动作等于没人负责 —— 与审批单"批准必须填审批人"同一条原则。"""
    for blank in ("", "   ", None):
        rec = _new(incidents, summary=f"blank={blank!r}")
        with pytest.raises(model.IncidentError):
            incidents.ack(rec["id"], by=blank)
        assert incidents.get(rec["id"])["status"] == model.STATUS_OPEN

    rec = _new(incidents)
    incidents.ack(rec["id"], by="sre-zhang")
    for blank in ("", "   ", None):
        with pytest.raises(model.IncidentError):
            incidents.resolve(rec["id"], by=blank, note="想偷偷结单")
    got = incidents.get(rec["id"])
    assert got["status"] == model.STATUS_ACK, "被拒的结单不能改动任何状态"
    assert got["resolved_at"] is None


def test_ack_and_resolve_record_who_when_and_what(incidents):
    rec = _new(incidents)
    acked = incidents.ack(rec["id"], by="  sre-zhang  ", note="我在看")
    assert acked["status"] == model.STATUS_ACK
    assert acked["owner"] == "sre-zhang", "前后空格要去掉（否则同一个人有两个 ID）"
    assert acked["acked_by"] == "sre-zhang"
    assert acked["acked_at"]

    done = incidents.resolve(rec["id"], by="sre-zhang", note="扩容后恢复")
    assert done["resolved_by"] == "sre-zhang"
    assert done["resolved_at"]
    assert done["resolution_note"] == "扩容后恢复"


# ============================================================
# 五、成员告警：收敛但不丢信息
# ============================================================
def test_create_links_the_alerts_it_was_given_and_escalates_severity(incidents):
    rec = _new(incidents, severity="info",
               alerts=[_alert("fp-1", severity="critical"),
                       _alert("fp-2", severity="info")])
    assert [a["fingerprint"] for a in rec["linked_alerts"]] == ["fp-1", "fp-2"]
    assert _events(rec) == ["created", "alert_linked", "alert_linked"]
    assert rec["severity"] == "critical"

    rec = incidents.link_alert(rec["id"], _alert("fp-3", severity="info"))
    assert rec["severity"] == "critical", "事件等级只能升不能降"


def test_link_alert_dedupes_by_fingerprint(incidents):
    rec = _new(incidents)
    first = incidents.link_alert(rec["id"], _alert("fp-dup"))
    assert len(first["linked_alerts"]) == 1
    assert first["linked_alerts"][0]["count"] == 1

    second = incidents.link_alert(rec["id"], _alert("fp-dup", severity="warning"))
    members = second["linked_alerts"]
    assert len(members) == 1, "同一个 fingerprint 不能占两行"
    assert members[0]["count"] == 2
    assert members[0]["updated_at"] >= members[0]["first_seen"]
    # 去重的是成员列表，不是事件流：每一次并入都要留痕
    assert _events(second).count("alert_linked") == 2


def test_link_alert_derives_a_key_when_fingerprint_is_missing(incidents):
    """没有 fingerprint 的告警不能全塌成一条 —— 那会直接丢掉告警条数。"""
    bare = {"alertname": "DiskSpaceLow", "host": "web-01", "service": "nginx"}
    assert alert_fingerprint(bare).startswith("derived:")

    rec = _new(incidents)
    incidents.link_alert(rec["id"], dict(bare))
    incidents.link_alert(rec["id"], dict(bare, host="web-02"))
    got = incidents.get(rec["id"])
    assert len(got["linked_alerts"]) == 2, "不同主机的告警不能被合成一条"

    incidents.link_alert(rec["id"], dict(bare))
    got = incidents.get(rec["id"])
    assert len(got["linked_alerts"]) == 2
    assert sorted(a["count"] for a in got["linked_alerts"]) == [1, 2]
    assert all(a["fingerprint"].startswith("derived:") for a in got["linked_alerts"])


def test_linking_an_alert_never_changes_the_status(incidents):
    """★ 有意的取舍：告警再次进来**不会**自动 `reopen`。

    "记一条关联告警"看起来是只读操作；如果它偷偷改了状态，时间线上就会出现
    **没人下过指令的状态变化**。要重开请显式 `reopen()`。
    """
    rec = _new(incidents)
    incidents.ack(rec["id"], by="sre-zhang")
    incidents.resolve(rec["id"], by="sre-zhang", note="重启服务")

    same_source = incidents.link_alert(rec["id"], _alert("fp-again"))
    assert same_source["status"] == model.STATUS_RESOLVED
    assert len(same_source["linked_alerts"]) == 1
    assert same_source["resolved_at"], "悄悄重开不该顺带清掉结单信息"

    # 诊断同理：异步诊断回来时事件可能已经被人先结单了，结论不该丢
    late = incidents.attach_diagnosis(rec["id"], summary="inode 耗尽", trace_id="tr-late")
    assert late["status"] == model.STATUS_RESOLVED
    assert late["diagnosis"]["summary"] == "inode 耗尽"


# ============================================================
# 六、诊断与通知：失败也要如实记
# ============================================================
def test_attach_diagnosis_records_the_conclusion_and_marks_failures(incidents):
    rec = _new(incidents)
    ok = incidents.attach_diagnosis(rec["id"], summary="扩容 /var 后恢复",
                                    trace_id="tr-ok")
    assert ok["diagnosis"]["summary"] == "扩容 /var 后恢复"
    assert ok["diagnosis"]["trace_id"] == "tr-ok"
    assert ok["diagnosis"]["ok"] is True
    assert ok["diagnosis"]["at"] == \
        [t["ts"] for t in ok["timeline"] if t["event"] == "diagnosed"][-1]

    bad = incidents.attach_diagnosis(rec["id"], summary="模型说可以删库",
                                     trace_id="tr-bad", ok=False)
    assert bad["diagnosis"]["ok"] is False
    note = [t["note"] for t in bad["timeline"] if t["event"] == "diagnosed"][-1]
    assert note.startswith("[未通过校验]"), "过了校验和没过校验的结论不能在时间线上长得一样"

    assert incidents.attach_diagnosis(rec["id"], "没有 trace")["diagnosis"]["trace_id"] == ""


def test_mark_notified_records_success_failure_and_a_missing_channel(incidents):
    rec = _new(incidents)
    incidents.mark_notified(rec["id"], "dingtalk", ok=True)
    failed = incidents.mark_notified(rec["id"], "feishu", ok=False, error="HTTP 500")

    notes = [t["note"] for t in failed["timeline"] if t["event"] == "notified"]
    assert notes[0].endswith("已送达")
    assert "失败" in notes[1] and "HTTP 500" in notes[1], "推失败不许谎报成功"
    assert [(n["channel"], n["ok"]) for n in failed["notifications"]] == \
        [("dingtalk", True), ("feishu", False)]

    # 渠道名缺失也要留下记录 —— 恰恰是配置写错的时候最需要知道"发去哪失败了"
    unknown = incidents.mark_notified(rec["id"], "", ok=False, error="没有配置 URL")
    assert unknown["notifications"][-1]["channel"] == "unknown"
    assert len(unknown["notifications"]) == 3


def test_timeline_records_the_whole_story_in_order(incidents):
    """时间线按发生顺序追加，且产品要看的那些事件一个都不能少。"""
    rec = _new(incidents)
    incidents.link_alert(rec["id"], _alert("fp-tl"))
    incidents.attach_diagnosis(rec["id"], summary="结论", trace_id="tr-tl")
    incidents.ack(rec["id"], by="sre-zhang", note="我来看")
    done = incidents.resolve(rec["id"], by="sre-zhang", note="已恢复")

    assert _events(done) == ["created", "alert_linked", "diagnosed", "ack", "resolved"]
    stamps = [t["ts"] for t in done["timeline"]]
    assert stamps == sorted(stamps), "时间线必须是有序的"
    assert done["timeline"][3]["by"] == "sre-zhang"
    assert [h["event"] for h in done["history"]] == ["created", "ack", "resolved"], \
        "history 只有状态迁移（资料性事件不进对账账本）"


# ============================================================
# 七、重启折叠 + 坏日志容忍
# ============================================================
def test_state_is_rebuilt_from_the_log_after_restart(incidents, tmp_path):
    """进程重启后状态必须逐字段一模一样 —— 这是"追加日志而不是 UPDATE"的意义。"""
    rec = _new(incidents)
    incidents.link_alert(rec["id"], _alert("fp-r"))
    incidents.attach_diagnosis(rec["id"], summary="结论", trace_id="tr-r")
    incidents.mark_notified(rec["id"], "dingtalk", ok=True)
    incidents.ack(rec["id"], by="sre-zhang")
    incidents.resolve(rec["id"], by="sre-zhang", note="修好了")

    reborn = IncidentStore(path=tmp_path / "incidents.jsonl")
    got = reborn.get(rec["id"])
    assert got == incidents.get(rec["id"]), "重启后必须逐字段一致"
    assert got["owner"] == "sre-zhang"
    assert got["created_at"] == rec["created_at"]
    assert reborn.list()[0]["id"] == rec["id"]
    # 重启后状态机依然生效
    with pytest.raises(model.IncidentError):
        reborn.resolve(rec["id"], by="sre-zhang")


def test_a_corrupt_line_does_not_lose_the_rest(incidents, tmp_path):
    """一行坏数据不该丢掉整份历史；没有 created 的孤儿事件也不能变成幽灵记录。"""
    rec = _new(incidents)
    incidents.ack(rec["id"], by="sre-zhang")
    with open(incidents.path, "a", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON\n")
        f.write("\n")
        f.write('{"event": "ack", "id": "inc-ghost", "ts": "2024-01-01T00:00:00"}\n')

    reborn = IncidentStore(path=tmp_path / "incidents.jsonl")
    got = reborn.get(rec["id"])
    assert got["status"] == model.STATUS_ACK
    assert got["acked_by"] == "sre-zhang"
    assert reborn.counts()["total"] == 1, "孤儿事件不能凭空造出一条记录"
    with pytest.raises(model.IncidentError):
        reborn.get("inc-ghost")


def test_missing_file_is_not_an_error(tmp_path):
    store = IncidentStore(path=tmp_path / "nope.jsonl")
    assert store.list() == []
    assert store.counts()["total"] == 0


# ============================================================
# 八、查询
# ============================================================
def test_unknown_ids_are_rejected_everywhere(incidents):
    calls = [
        lambda: incidents.get("inc-nope"),
        lambda: incidents.get(""),
        lambda: incidents.link_alert("inc-nope", _alert()),
        lambda: incidents.attach_diagnosis("inc-nope", summary="x"),
        lambda: incidents.ack("inc-nope", by="sre-zhang"),
        lambda: incidents.resolve("inc-nope", by="sre-zhang"),
        lambda: incidents.reopen("inc-nope"),
        lambda: incidents.mark_notified("inc-nope", "dingtalk", ok=True),
    ]
    for call in calls:
        with pytest.raises(model.IncidentError):
            call()


def test_list_filters_by_status_is_newest_first_and_honours_limit(incidents):
    a = _new(incidents, summary="a")
    b = _new(incidents, summary="b")
    c = _new(incidents, summary="c")
    incidents.ack(a["id"], by="sre-zhang")
    incidents.resolve(a["id"], by="sre-zhang")
    incidents.ack(b["id"], by="sre-li")

    assert [r["id"] for r in incidents.list(status=model.STATUS_OPEN)] == [c["id"]]
    assert [r["id"] for r in incidents.list(status=model.STATUS_ACK)] == [b["id"]]
    assert [r["id"] for r in incidents.list(status=model.STATUS_RESOLVED)] == [a["id"]]
    assert incidents.list(status="nope") == []

    assert len(incidents.list(limit=2)) == 2
    assert len(incidents.list()) == 3
    # 新的在前。三个事件是同一秒建的，created_at 完全相同 ——
    # 所以这条同时在测"排序必须是全序"（只按 created_at 排会随机）
    assert [r["id"] for r in incidents.list()] == [c["id"], b["id"], a["id"]]

    counts = incidents.counts()
    assert counts == {"total": 3, model.STATUS_OPEN: 1,
                      model.STATUS_ACK: 1, model.STATUS_RESOLVED: 1}
    assert counts["total"] == sum(v for k, v in counts.items() if k != "total")


def test_a_returned_record_is_a_copy_not_the_store_state(incidents):
    """浅拷贝不够：`timeline` / `linked_alerts` 是列表，调用方一改就改到了 store 的内存态，
    而盘上没变 —— 那就是"内存与盘不一致"的另一种形态。"""
    rec = _new(incidents)
    got = incidents.get(rec["id"])
    got["timeline"].append({"ts": "x", "event": "hacked"})
    got["linked_alerts"].append({"fingerprint": "fp-ghost"})
    got["status"] = model.STATUS_RESOLVED

    again = incidents.get(rec["id"])
    assert _events(again) == ["created"]
    assert again["linked_alerts"] == []
    assert again["status"] == model.STATUS_OPEN
    assert len(incidents.list()[0]["timeline"]) == 1


def test_default_path_and_singleton_follow_the_approvals_convention():
    """默认落点和 `approvals` 一致（`PROJECT_ROOT/logs/`），单例是进程级的。

    为什么要有这条：运维只会去一个地方找日志。两个模块各写一个目录，
    排查时就会"审批单找得到、事件找不到"。**这里只读路径常量，不碰真实 logs/**。
    """
    from app.incident import store as store_module
    from app.llm import PROJECT_ROOT

    assert store_module.IncidentStore().path == PROJECT_ROOT / "logs" / "incidents.jsonl"
    assert store_module.store() is store_module.store(), "必须是模块级单例"
    assert isinstance(store_module.store(), store_module.IncidentStore)

