# -*- coding: utf-8 -*-
"""M2 接线测试：告警 → 聚合 → 事件 → 通知。

这一层是 M2 真正交付的东西，所以它必须用**端到端的方式**验证，
而不是各自测模块：模块都对，接错了照样不工作。

三个"最该被证明"的行为：

    1. 50 条同源告警 → **1 个事件、1 次诊断**（不然账单和告警疲劳都躲不掉）
    2. 诊断结论**真的发出去了**（M2 存在的全部理由）
    3. 没有配置通知时**一个请求都不发**（与加这一层之前逐字段一致）

全部零成本：诊断引擎用打桩，模型入口用 fake_chat，落盘全部改到 tmp_path。
"""

import pytest

from app.alerting.aggregator import AlertAggregator
from app.alerting.silence import Silence
from app.incident.store import IncidentStore
from app.notify import events as notify_events
from app.notify.base import Notifier, NotifyResult
from app.notify.dispatcher import Dispatcher

import app.main as m
import app.incident.store as incident_store_mod


# ============================================================
# 装置
# ============================================================
class RecordingNotifier(Notifier):
    """假渠道：把发出去的东西记下来，一个请求都不发。"""

    name = "recording"

    def __init__(self):
        self.sent = []

    def send(self, title, text, *, payload=None):
        self.sent.append({"title": title, "text": text, "payload": payload})
        return NotifyResult(channel=self.name, ok=True, status=200)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把告警链路的所有外部依赖都换掉：事件落盘、审计、聚合表、诊断引擎。"""
    # 落盘隔离
    monkeypatch.setattr(m, "AUDIT_LOG", tmp_path / "audit.jsonl")
    store = IncidentStore(path=tmp_path / "incidents.jsonl")
    monkeypatch.setattr(incident_store_mod, "store", lambda: store)
    monkeypatch.setattr(incident_store_mod, "_STORE", store)

    # 聚合表与频控计数：每个用例都从干净状态开始，且时钟可控
    clock = {"now": 1_700_000_000.0}
    agg = AlertAggregator(window_seconds=m.ALERT_DEDUP_SECONDS, aggregate=True,
                          clock=lambda: clock["now"])
    monkeypatch.setattr(m, "_ALERT_AGG", agg)
    monkeypatch.setattr(m, "_ALERT_HOUR", {"hour": "", "count": 0})
    monkeypatch.setattr(m, "_ALERT_SILENCE", Silence(path=tmp_path / "none.json"))

    # 诊断引擎打桩：证明"跑了几次"，且不花一分钱
    calls = []
    from app.agents import supervisor

    def fake_run(question):
        calls.append(question)
        return {"answer": f"诊断结论：{question[:20]}",
                "rounds": 2, "tool_calls": 3, "distinct_tools": ["check_disk"],
                "usage": {"total_tokens": 1234}, "elapsed_ms": 42,
                "stop_reason": "finalize"}

    monkeypatch.setattr(supervisor, "run", fake_run)
    monkeypatch.setattr(m, "ALERT_AUTO_DIAGNOSE", True)

    # 通知：注入一个记录型渠道
    rec = RecordingNotifier()
    monkeypatch.setattr(notify_events, "dispatcher",
                        lambda env=None: Dispatcher([rec], max_attempts=1,
                                                    min_interval=0))

    return {"store": store, "agg": agg, "clock": clock, "diag": calls, "rec": rec}


def _alerts_payload(n=50, host="web-01", service="nginx"):
    """Alertmanager 标准格式的一批同源告警（不同告警名，同一个 host+service）。"""
    return {"alerts": [
        {"labels": {"alertname": f"Alert{i}", "service": service,
                    "instance": f"{host}:9100", "severity": "warning"},
         "annotations": {"summary": f"第 {i} 条"}}
        for i in range(n)
    ]}


def _one_alert(name="DiskSpaceLow", host="web-01", service="nginx"):
    return {"alerts": [{"labels": {"alertname": name, "service": service,
                                   "instance": f"{host}:9100",
                                   "severity": "warning"}}]}


# ============================================================
# 一、50 条同源告警 → 1 个事件、1 次诊断
# ============================================================
def test_fifty_同源告警_聚成一个事件且只诊断一次(env, fake_chat):
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "只读排查"})
    out = m.webhook_alert(_alerts_payload(50))

    assert out["received"] == 50
    assert len(env["diag"]) == 1, f"诊断跑了 {len(env['diag'])} 次，应当只有 1 次"
    assert fake_chat.call_count == 1, "意图解析也只该花一次钱"
    assert env["store"].counts()["total"] == 1, "50 条告警应当只产生 1 个事件"

    decisions = [r["decision"] for r in out["reports"]]
    assert decisions[0] == "auto_diagnosed"
    assert decisions[1:] == ["merged_into_incident"] * 49


def test_被抑制的告警说得出自己并到哪个事件去了(env, fake_chat):
    """★ 被抑制 ≠ 被丢弃：值班的人要能看到"这条告警去哪了"。"""
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "只读排查"})
    out = m.webhook_alert(_alerts_payload(5))

    first, merged = out["reports"][0], out["reports"][1]
    assert first["incident_id"]
    assert merged["incident_id"] == first["incident_id"]
    assert merged["members"] == 2, "窗口内第几条要如实报出来（风暴规模）"
    assert "并入事件" in merged["reason"]


def test_异源告警不会被并到一起(env, fake_chat):
    """并错了等于漏故障 —— 不同主机必须各自成事件。"""
    fake_chat.push(
        {"action": "diagnose", "service": "nginx", "host": "web-01",
         "risk": "low", "need_confirm": False, "reason": "r"},
        {"action": "diagnose", "service": "mysql", "host": "db-01",
         "risk": "low", "need_confirm": False, "reason": "r"},
    )
    out = m.webhook_alert({"alerts": [
        {"labels": {"alertname": "NginxDown", "service": "nginx",
                    "instance": "web-01:9100"}},
        {"labels": {"alertname": "MysqlDown", "service": "mysql",
                    "instance": "db-01:3306"}},
    ]})
    assert len(env["diag"]) == 2
    assert env["store"].counts()["total"] == 2
    ids = {r["incident_id"] for r in out["reports"]}
    assert len(ids) == 2


def test_聚合关掉时退回旧的精确去重语义(env, fake_chat, monkeypatch):
    """ALERT_AGGREGATE=0：同机不同告警名**不再**合并，文案也与改动前一致。"""
    monkeypatch.setattr(m, "ALERT_AGGREGATE", False)
    legacy = AlertAggregator(window_seconds=m.ALERT_DEDUP_SECONDS, aggregate=False)
    monkeypatch.setattr(m, "_ALERT_AGG", legacy)

    fake_chat.push(
        {"action": "diagnose", "service": "nginx", "host": "web-01",
         "risk": "low", "need_confirm": False, "reason": "r"},
        {"action": "diagnose", "service": "nginx", "host": "web-01",
         "risk": "low", "need_confirm": False, "reason": "r"},
    )
    out = m.webhook_alert({"alerts": [
        {"labels": {"alertname": "A", "service": "nginx", "instance": "web-01"}},
        {"labels": {"alertname": "B", "service": "nginx", "instance": "web-01"}},
    ]})
    assert len(env["diag"]) == 2, "旧语义是同名同机才去重，不同名各跑一次"
    assert [r["decision"] for r in out["reports"]] == ["auto_diagnosed"] * 2, \
        "旧语义下两条都该各跑一轮"


def test_旧文案在被抑制时保持一致(env, fake_chat, monkeypatch):
    monkeypatch.setattr(m, "ALERT_AGGREGATE", False)
    legacy = AlertAggregator(window_seconds=m.ALERT_DEDUP_SECONDS, aggregate=False)
    monkeypatch.setattr(m, "_ALERT_AGG", legacy)
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    out = m.webhook_alert({"alerts": [
        {"labels": {"alertname": "A", "service": "nginx", "instance": "web-01"}},
        {"labels": {"alertname": "A", "service": "nginx", "instance": "web-01"}},
    ]})
    assert out["reports"][1]["decision"] == "suppressed_duplicate"
    assert "已处理过同名同机的告警" in out["reports"][1]["reason"]


# ============================================================
# 一·B、异步入口（ALERT_ASYNC=1）
# ============================================================
def _bg():
    from starlette.background import BackgroundTasks
    return BackgroundTasks()


def _run_bg(bg):
    import asyncio
    asyncio.run(bg())


def test_sync_mode_says_so_in_the_response(env, fake_chat):
    """响应里如实标出这次是同步还是异步 —— 否则调用方不知道结论该去哪找。"""
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    out = m.webhook_alert(_one_alert())
    assert out["async"] is False
    assert out["reports"][0]["decision"] == "playbook_only" or \
        out["reports"][0]["decision"] == "auto_diagnosed"


def test_async_mode_returns_immediately_without_diagnosing(env, fake_chat,
                                                          monkeypatch):
    """★ 异步的意义：Alertmanager 有超时约束，不能在请求里等 6–20 秒的诊断。

    所以响应必须**立刻**返回，并且如实说明"结论不在这里、去 /incidents 查"。
    """
    monkeypatch.setattr(m, "ALERT_ASYNC", True)
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    bg = _bg()
    out = m.webhook_alert(_one_alert(), bg)

    assert out["async"] is True
    assert out["reports"][0]["decision"] == "accepted_async"
    assert "在后台执行" in out["reports"][0]["reason"]
    assert env["diag"] == [], "响应返回时不该已经跑过诊断（否则异步没意义）"
    assert fake_chat.call_count == 0


def test_async_mode_actually_runs_the_pipeline_in_the_background(env, fake_chat,
                                                                 monkeypatch):
    """★ 反过来也要证明：后台**真的会跑**，而且结论写回了事件、通知也发了。

    只测"立刻返回"是不够的 —— 那样一个"接了不干"的实现也能过测试。
    """
    monkeypatch.setattr(m, "ALERT_ASYNC", True)
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    bg = _bg()
    out = m.webhook_alert(_one_alert(), bg)
    incident_id = out["reports"][0]["incident_id"]

    _run_bg(bg)                      # 执行排队的后台任务

    assert env["diag"], "后台任务没有真的跑诊断"
    rec = env["store"].get(incident_id)
    assert rec["diagnosis"], "结论应当写回事件"
    assert rec["notifications"], "通知应当已经发出（异步不等于不通知）"
    audit = (m.AUDIT_LOG).read_text(encoding="utf-8")
    assert "alert.accepted" in audit, "受理也要留痕（否则丢任务时无从对账）"


def test_async_mode_still_merges_duplicates(env, fake_chat, monkeypatch):
    """异步不影响幂等：Alertmanager 超时重推时，聚合表照样把它并进同一个事件。"""
    monkeypatch.setattr(m, "ALERT_ASYNC", True)
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    bg1 = _bg()
    first = m.webhook_alert(_one_alert(), bg1)
    bg2 = _bg()
    second = m.webhook_alert(_one_alert(), bg2)      # 同源重推

    assert first["reports"][0]["decision"] == "accepted_async"
    assert second["reports"][0]["decision"] == "merged_into_incident"
    _run_bg(bg1)
    _run_bg(bg2)
    assert len(env["diag"]) == 1, "重推不该再花一次钱"


# ============================================================
# 二、空批次与维护窗口
# ============================================================
def test_空告警批次不建事件不调模型(env, fake_chat):
    """D6：`{"alerts": []}` 是"本次没有告警"，不该白跑一轮模型。"""
    out = m.webhook_alert({"alerts": []})
    assert out["received"] == 0 and out["reports"] == []
    assert "空告警批次" in out["message"]
    assert env["diag"] == [] and fake_chat.call_count == 0
    assert env["store"].counts().get("total", 0) == 0
    assert env["rec"].sent == [], "空批次不该推通知"


def test_维护窗口内的告警被抑制且留审计(env, fake_chat, tmp_path, monkeypatch):
    import json
    silence_file = tmp_path / "alert_silences.json"
    silence_file.write_text(json.dumps([{
        "alertname_prefix": "DiskSpace", "host_prefix": "web-",
        "until": "2033-01-01T00:00:00", "note": "磁盘扩容（CHG-101）"}]),
        encoding="utf-8")
    monkeypatch.setattr(m, "_ALERT_SILENCE", Silence(path=silence_file))

    out = m.webhook_alert(_one_alert("DiskSpaceLow"))
    assert out["reports"][0]["decision"] == "silenced"
    assert "CHG-101" in out["reports"][0]["reason"]
    assert env["diag"] == [], "维护窗口内不该花钱"
    assert env["store"].counts().get("total", 0) == 0

    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "alert.silenced" in audit, "抑制也必须留痕（否则没人知道告警去哪了）"


# ============================================================
# 三、通知（M2 存在的理由）
# ============================================================
def test_诊断结论真的被推出去了(env, fake_chat):
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "只读排查"})
    m.webhook_alert(_one_alert())

    assert len(env["rec"].sent) == 1, "诊断完没有任何人被通知 = 这个功能没做"
    sent = env["rec"].sent[0]
    assert "web-01" in sent["title"]
    assert "诊断结论" in sent["text"]
    assert sent["payload"]["incident_id"]


def test_通知结果记回了事件(env, fake_chat):
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    out = m.webhook_alert(_one_alert())
    rec = env["store"].get(out["reports"][0]["incident_id"])
    assert rec["notifications"], "送达与否必须记在事件上（失败不许谎报成功）"
    assert rec["notifications"][-1]["ok"] is True


def test_没有配置通知时一个请求都不发(env, fake_chat, monkeypatch):
    """★ 兼容性底线：没配通知 → 行为与加这一层之前一致。"""
    monkeypatch.setattr(notify_events, "dispatcher", lambda env=None: None)
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    out = m.webhook_alert(_one_alert())
    assert env["rec"].sent == []
    # 诊断本身照常完成（通知是旁路，缺了不影响主链路）
    assert out["reports"][0]["decision"] == "auto_diagnosed"


def test_通知抛异常也不影响诊断结果(env, fake_chat, monkeypatch):
    """推不出去只能变成一条失败记录，绝不能变成"诊断结果丢了"。"""
    def boom(*a, **kw):
        raise RuntimeError("渠道炸了")

    monkeypatch.setattr(env["rec"], "send", boom)
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    out = m.webhook_alert(_one_alert())
    assert out["reports"][0]["decision"] == "auto_diagnosed"
    assert out["reports"][0]["answer"]


def test_审批单产生时会推提醒(tmp_path, monkeypatch):
    """审批单卡在人身上：没人知道它存在，它就会一直躺着直到过期。"""
    from app.sandbox import approvals as ap

    monkeypatch.setattr(ap, "_STORE", ap.ApprovalStore(path=tmp_path / "a.jsonl"))
    rec = RecordingNotifier()
    monkeypatch.setattr(notify_events, "dispatcher",
                        lambda env=None: Dispatcher([rec], max_attempts=1,
                                                    min_interval=0))
    notify_events.approval_created({
        "id": "ap-test0001", "command": "truncate -s 0 /var/log/nginx/error.log",
        "risk": "reversible", "isolation": "container", "reason": "清理写满的日志",
        "expires_at": "2033-01-01T00:00:00"})

    assert len(rec.sent) == 1
    assert "待审批" in rec.sent[0]["title"]
    assert "ap-test0001" in rec.sent[0]["text"]


# ============================================================
# 四、事件接口的状态机（与接口层互补：这里测语义，那里测边界）
# ============================================================
def test_事件的认领与结单都要有人(env):
    from app.incident.model import IncidentError

    inc = env["store"].create(source="alert", host="web-01", service="nginx",
                              severity="critical", summary="磁盘满")
    with pytest.raises(IncidentError):
        env["store"].ack(inc["id"], by="  ")
    acked = env["store"].ack(inc["id"], by="sre-zhang", note="在看")
    assert acked["status"] == "ack" and acked["owner"] == "sre-zhang"
    done = env["store"].resolve(inc["id"], by="sre-zhang", note="扩容完成")
    assert done["status"] == "resolved"


def test_告警等级取最高而不是第一条(env):
    inc = env["store"].create(source="alert", host="web-01", service="nginx",
                              severity="info", summary="")
    from app.incident.model import severity_of
    assert severity_of([{"severity": "info"}, {"severity": "critical"}]) == "critical"
    assert inc["severity"] == "info"      # 创建时以传入值为准
