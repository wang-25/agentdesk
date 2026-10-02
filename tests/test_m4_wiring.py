# -*- coding: utf-8 -*-
"""M4 接线测试：`/metrics`、`/healthz`、`/audit` 分页、请求 ID、轮转后的读。

模块级测试（test_metrics / test_healthz / test_jsonl_log）证明的是"零件对不对"，
这个文件证明的是**装到一起之后还能不能工作** —— 零件全对但接错了，
是这类改动最常见的失败形态（M3 那条真机回退就是这么来的）。
"""

import json

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app import security
from app.observability import jsonl, tracer
from app.sandbox import approvals


@pytest.fixture
def client(tmp_path, monkeypatch):
    """进程内客户端：审计、trace、审批单、事件**全部**改到临时目录。

    ★ 事件存储也必须隔离 —— 漏掉它就会往真实的 logs/incidents.jsonl 里写演示数据
      （M1 期间就发生过一次同类污染：假 trace 写进真文件，把对外成本口径压低了 12 倍）。
    """
    from app.incident import store as incident_store_mod

    monkeypatch.setattr(m, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(tracer, "TRACE_PATH", tmp_path / "traces.jsonl")
    monkeypatch.setattr(approvals, "_STORE",
                        approvals.ApprovalStore(path=tmp_path / "approvals.jsonl"))
    monkeypatch.setattr(incident_store_mod, "_STORE",
                        incident_store_mod.IncidentStore(path=tmp_path / "incidents.jsonl"),
                        raising=False)
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    from app.observability import metrics
    metrics.reset()
    with TestClient(m.app) as c:
        yield c
    metrics.reset()


# ============================================================
# 一、/metrics
# ============================================================
def test_metrics_endpoint_serves_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in r.headers["content-type"]
    body = r.text
    assert body.endswith("\n")
    assert "# TYPE" in body and "# HELP" in body


def test_metrics_counts_requests_it_just_served(client):
    """抓一次指标，就应当能在下一次抓取里看到这次请求。

    ★ 断言写法注意：Prometheus 文本里的标签是**按名字排序**输出的，
      所以实际是 `{method="GET",path="/health",status="200"}`，
      不能写成 `{path="/health"` 开头（我第一版就是这么写错的）。
    """
    client.get("/health")
    client.get("/audit")
    body = client.get("/metrics").text
    assert "agentdesk_http_requests_total{" in body
    assert 'path="/health"' in body and 'path="/audit"' in body
    assert 'status="200"' in body


def test_metrics_normalises_ids_to_keep_cardinality_low(client):
    """★ `/traces/{id}` 这类路径必须归一，否则序列数会随请求数线性增长。

    这里用的是**项目里真实的 ID 形态**（`tr-` + 12 位十六进制等），
    而不是我随手编的 `t-one` —— 第一版就是编的，于是漏掉了 `tr-` 前缀这条路径，
    规则只认 `ap-`/`inc-`。**测试数据必须像真实数据**，否则用例是自我安慰。
    """
    assert m._metric_path("/traces/tr-a1b2c3d4e5f6") == "/traces/{id}"
    assert m._metric_path("/approvals/ap-1a2b3c4d") == "/approvals/{id}"
    assert m._metric_path("/incidents/inc-1a2b/ack") == "/incidents/{id}/ack"
    assert m._metric_path("/traces/42") == "/traces/{id}"
    assert m._metric_path("/traces/") == "/traces"
    # 普通路由名不能被误伤
    for real in ("/health", "/metrics", "/healthz", "/rag/index",
                 "/webhook/alert", "/sandbox", "/incidents"):
        assert m._metric_path(real) == real

    # 实际跑一遍：不同 id 不该产生不同标签
    client.get("/traces/tr-aaaaaaaaaaaa")
    client.get("/traces/tr-bbbbbbbbbbbb")
    body = client.get("/metrics").text
    assert 'path="/traces/tr-aaaaaaaaaaaa"' not in body
    assert 'path="/traces/tr-bbbbbbbbbbbb"' not in body
    assert 'path="/traces/{id}"' in body


def test_metrics_exposes_state_scope_and_uptime(client):
    body = client.get("/metrics").text
    assert "agentdesk_state_scope" in body
    assert 'scope="process"' in body
    assert "agentdesk_process_uptime_seconds" in body


def test_metrics_requires_a_token_when_auth_is_on(client, monkeypatch):
    """★ 指标里有成本、错误率、路径 —— 公网裸奔等于把运维内部状况送出去。"""
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    monkeypatch.setattr(security, "AGENT_TOKEN", "t" * 32)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics",
                      headers={"X-API-Key": "t" * 32}).status_code == 200


# ============================================================
# 二、/healthz
# ============================================================
def test_healthz_ok_and_health_stays_compatible(client):
    hz = client.get("/healthz")
    assert hz.status_code == 200 and hz.json()["ok"] is True
    # ★ /health 的字段一个都不能变（compose 的 HEALTHCHECK 依赖它）
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["service"] == "agentdesk"
    assert "version" in h and "security" in h


def test_healthz_returns_503_and_names_the_broken_dependency(client, monkeypatch):
    """★ 503 必须说清是哪一项坏了 —— 这是这个接口存在的全部意义。"""
    monkeypatch.setattr(m.selfcheck, "check_index_loadable",
                        lambda: {"ok": False, "detail": "索引损坏：无法解析"})
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["failing"] == ["index_loadable"]
    assert "无法解析" in body["checks"]["index_loadable"]["detail"]


def test_healthz_requires_a_token_when_auth_is_on(client, monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    monkeypatch.setattr(security, "AGENT_TOKEN", "t" * 32)
    assert client.get("/healthz").status_code == 401
    assert client.get("/health",
                      headers={"X-API-Key": "t" * 32}).status_code == 200, \
        "公开的是 /health，不是 /healthz"


def test_healthz_is_json_serializable(client):
    assert json.dumps(client.get("/healthz").json(), ensure_ascii=False)


# ============================================================
# 三、/audit 分页（不再全量读）
# ============================================================
def test_audit_pagination_walks_backwards(client):
    for i in range(10):
        m.write_audit("test.event", {"i": i})
    r = client.get("/audit", params={"limit": 3}).json()
    assert [it["i"] for it in r["items"]] == [7, 8, 9], "默认取最新 3 条，旧→新排序"
    r2 = client.get("/audit", params={"limit": 3, "offset": 3}).json()
    assert [it["i"] for it in r2["items"]] == [4, 5, 6]
    assert r2["offset"] == 3


def test_audit_reports_window_metadata_and_says_what_total_means(client):
    m.write_audit("test.event", {"i": 1})
    body = client.get("/audit", params={"limit": 5}).json()
    assert body["window"]["files"] == ["audit.jsonl"]
    assert body["window"]["bad_lines"] == 0
    assert "窗口内" in body["note"], \
        "total 的语义（窗口内 vs 全历史）必须写在响应里，否则又是一个看起来对的数字"


def test_audit_reads_across_rotated_files(client, monkeypatch):
    """★ 轮转之后 /audit 仍然要能读到最近记录 —— 否则看板会突然变空。"""
    for i in range(6):
        m.write_audit("test.event", {"i": i})
    assert jsonl.maybe_rotate(m.AUDIT_LOG, limit_bytes=1, keep=5) is True
    m.write_audit("test.event", {"i": 99})

    body = client.get("/audit", params={"limit": 3}).json()
    assert [it["i"] for it in body["items"]] == [4, 5, 99], \
        "应当跨轮转文件读到（4、5 在被轮转的那份里）"
    assert len(body["window"]["files"]) == 2


def test_audit_bad_lines_are_reported_not_hidden(client):
    m.write_audit("test.event", {"i": 1})
    with open(m.AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write("{ 坏行\n")
    body = client.get("/audit", params={"limit": 10}).json()
    assert body["window"]["bad_lines"] == 1


def test_audit_leaks_no_pii_beyond_the_records(client):
    """坏行被跳过，但**不能把整份文件读进内存**（这是这次改动的动机）。"""
    for i in range(50):
        m.write_audit("test.event", {"i": i, "pad": "x" * 1000})
    body = client.get("/audit", params={"limit": 5}).json()
    assert len(body["items"]) == 5
    assert body["total"] <= 50


# ============================================================
# 四、write_audit：请求 ID + 并发安全
# ============================================================
def test_audit_rows_carry_the_request_id_inside_a_request(client):
    """一次请求写下的审计行要能跟它的日志对上 —— 靠 request_id。

    ★ 必须由**接口自己**去写审计，不能在测试线程里直接调 write_audit：
      那样根本没有请求上下文，测的就不是这套机制了。
      `POST /webhook/alert` 传空批次正好：它会写一条 `alert.empty_batch` 审计，
      而且**零成本**（不调模型、不建事件）。
    """
    r = client.post("/webhook/alert", json={"alerts": []})
    rid = r.headers.get("X-Request-ID")
    assert rid, "响应里必须带请求 ID"

    rows = [json.loads(ln) for ln in
            m.AUDIT_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    inside = [row for row in rows if row["event"] == "alert.empty_batch"]
    assert inside, "接口应当写了审计"
    assert inside[-1].get("request_id") == rid, \
        "请求内写的审计行必须带上这次的请求 ID"


def test_audit_rows_outside_a_request_have_no_request_id(client):
    """请求之外的审计（后台任务/启动期）不带这个字段 —— 不补空串。

    "没有请求上下文"和"请求 ID 恰好是空"是两件事，混成一种样子就查不出问题。
    """
    from app.observability import logsetup
    logsetup.clear_request_id()
    m.write_audit("test.outside", {})
    rows = [json.loads(ln) for ln in
            m.AUDIT_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert "request_id" not in rows[-1]


def test_concurrent_write_audit_produces_exactly_n_lines(client):
    """★ 审计写入以前**没有锁** —— 而它是最并发的写入路径。

    这条用例同时守着"加锁"与"写入路径改成公共层（可能触发轮转）"这两件事。
    """
    import threading

    n_threads, per_thread = 8, 25
    errors = []

    def worker(seed):
        try:
            for i in range(per_thread):
                m.write_audit("test.concurrent", {"seed": seed, "i": i})
        except Exception as exc:                  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    lines = [ln for ln in m.AUDIT_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == n_threads * per_thread, "并发写丢行或交错坏行"
    for ln in lines:
        json.loads(ln)                     # 每一行都必须是完整 JSON


# ============================================================
# 五、轮转之后既有接口仍然正常（端到端）
# ============================================================
def test_traces_endpoint_still_works_after_rotation(client, monkeypatch):
    """★ 轮转 + 读侧回读的端到端验证。

    只测 `read_tail` 不够：真正会出问题的是"trace 写进新文件、接口读不到旧文件"，
    那会让 `/traces` 在轮转后突然只显示最近几条，而且**不报错**。
    """
    # 让轮转很容易触发：阈值压到 900 字节
    monkeypatch.setattr(jsonl, "max_bytes", lambda env=None: 900)
    monkeypatch.setattr(jsonl, "keep_count", lambda env=None: 5)

    for i in range(6):
        with tracer.trace(f"run-{i}", question=f"q{i}"):
            with tracer.span(tracer.TYPE_LLM, name="chat", model="m"):
                pass

    assert (tracer.TRACE_PATH.parent / "traces.jsonl.1").exists(), "应当已经轮转过"

    body = client.get("/traces", params={"limit": 20}).json()
    items = body.get("items") or body
    assert len(items) >= 4, f"轮转后 /traces 明显少给了数据：{len(items)}"


def test_metrics_report_trace_write_failures(client, monkeypatch):
    """观测写不进去必须**看得出来**（写失败次数进指标）。"""
    monkeypatch.setattr(jsonl, "append_jsonl",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    tracer._write({"kind": "trace", "trace_id": "x"})
    body = client.get("/metrics").text
    assert "agentdesk_trace_write_failures" in body
    assert "agentdesk_trace_write_failures 1" in body


# ============================================================
# 六、埋点：安全拒绝 / 告警判定 / 沙箱执行 / 模型
# ============================================================
def test_security_rejections_are_counted_by_reason(client, monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    monkeypatch.setattr(security, "AGENT_TOKEN", "t" * 32)
    client.get("/traces")                       # 无令牌 → 401
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    body = client.get("/metrics").text
    assert 'agentdesk_security_rejections_total{reason="auth"}' in body


def test_alert_decisions_are_counted(client, fake_chat):
    """告警进来后的**判定分布**要能看见 —— 这是"降噪有没有生效"的唯一量化口径。"""
    fake_chat.push({"action": "diagnose", "service": "nginx", "host": "web-01",
                    "risk": "low", "need_confirm": False, "reason": "r"})
    r = client.post("/webhook/alert", json={"alerts": [
        {"labels": {"alertname": "A", "service": "nginx", "instance": "web-01:9100"}}]})
    assert r.status_code == 200
    body = client.get("/metrics").text
    assert "agentdesk_alerts_received_total{" in body
    assert 'decision="playbook_only"' in body


def test_alert_dedup_gauge_is_exposed(client, fake_chat):
    """去重表大小要能看见 —— 它曾经是无界增长的（M2 修掉了，但得能监控）。"""
    body = client.get("/metrics").text
    assert "agentdesk_alert_dedup_entries" in body


def test_llm_metrics_are_recorded_without_calling_the_model():
    """埋点函数本身可单测（不调模型）：token 按 kind 分开、成本按模型归集。"""
    from app.llm import _note_model_metrics
    from app.observability import metrics

    metrics.reset()
    _note_model_metrics({"prompt_tokens": 100, "completion_tokens": 20,
                         "prompt_cache_hit_tokens": 30}, "deepseek-flash")
    body = metrics.render()
    assert 'agentdesk_llm_tokens_total{kind="prompt"} 100' in body
    assert 'agentdesk_llm_tokens_total{kind="completion"} 20' in body
    assert 'agentdesk_llm_tokens_total{kind="cache_hit"} 30' in body
    assert "agentdesk_llm_cost_cny_total{" in body
    assert 'agentdesk_llm_calls_total{ok="true"} 1' in body

    metrics.reset()
    _note_model_metrics({}, None, ok=False)
    assert 'agentdesk_llm_calls_total{ok="false"} 1' in metrics.render()


def test_notify_metrics_are_recorded_per_channel():
    from app.observability import metrics
    from app.notify.base import NotifyResult
    from app.notify.dispatcher import _note_notify_metrics

    metrics.reset()
    _note_notify_metrics([NotifyResult(channel="webhook", ok=True),
                          NotifyResult(channel="dingtalk", ok=False, error="x")])
    body = metrics.render()
    assert 'agentdesk_notify_total{channel="webhook",ok="true"} 1' in body
    assert 'agentdesk_notify_total{channel="dingtalk",ok="false"} 1' in body
