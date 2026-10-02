# -*- coding: utf-8 -*-
"""`/healthz` 的依赖探针。

这个文件测的核心不是"能不能返回 200"，而是**坏的时候说不说得清**：
健康检查最没用的形态就是"503，不告诉你哪儿坏了"。
所以每一根探针都要有一条"把它弄坏 → 明细里点名"的用例。

另一条同样重要：**探针不能有副作用、不能花钱**。
默认档读的都是本地状态（索引、存储、磁盘、trace 痕迹），
真调模型的路径必须显式打开，并且要在返回里标注"本次检查会产生费用"。
"""

import json

import pytest

from app import selfcheck


# ============================================================
# 一、逐项检查：正常路径
# ============================================================
def test_logs_writable_ok(tmp_path):
    got = selfcheck.check_logs_writable(tmp_path)
    assert got["ok"] is True
    assert not (tmp_path / ".healthz").exists(), "探针文件必须被清理掉"


def test_logs_writable_reports_failure(tmp_path, monkeypatch):
    """★ 写不进去要**说清是哪个目录**。"""
    logs = tmp_path / "logs"
    logs.mkdir()

    def boom(*a, **kw):
        raise OSError("Disk quota exceeded")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    got = selfcheck.check_logs_writable(logs)
    assert got["ok"] is False
    assert "不可写" in got["detail"] and "quota" in got["detail"].lower()


def test_disk_space_ok(tmp_path):
    got = selfcheck.check_disk_space(tmp_path)
    assert got["ok"] is True and "剩余" in got["detail"]


def test_disk_space_low_is_unhealthy(tmp_path, monkeypatch):
    """磁盘快满 = 审计与 trace 都会丢，必须算不健康。"""
    import shutil as _shutil

    class Usage:
        free = 1024          # 1KB 剩余

    monkeypatch.setattr(_shutil, "disk_usage", lambda p: Usage())
    got = selfcheck.check_disk_space(tmp_path)
    assert got["ok"] is False and "低于" in got["detail"]


def test_notify_unconfigured_is_healthy(monkeypatch):
    """没配通知**不是故障** —— 默认就是不出站，那是有意的选择。"""
    from app.notify import events

    monkeypatch.delenv("NOTIFY_CHANNELS", raising=False)
    events.reset()                      # 通知层会缓存装配结果，改环境必须重置
    try:
        got = selfcheck.check_notify_config()
        assert got["ok"] is True and "默认不出站" in got["detail"]
    finally:
        events.reset()


def test_notify_channel_named_but_url_missing_is_unhealthy(monkeypatch):
    """★ 点了渠道名却没配 URL：这是配置错了，必须报出来。

    这类错误最容易骗人：日志里一切正常，只是**通知永远发不出去**。

    ★ 两条测试卫生要求（第一版都没做，于是本地 .env 一配上飞书就红了）：
      · 清掉**所有**渠道的 URL，让用例不受本机 .env 影响
      · `events.reset()` —— 通知层缓存装配结果，不重置就还在测上一次的配置
    """
    from app.notify import events

    monkeypatch.setenv("NOTIFY_CHANNELS", "dingtalk")
    for var in ("NOTIFY_DINGTALK_URL", "NOTIFY_FEISHU_URL", "NOTIFY_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    events.reset()
    try:
        got = selfcheck.check_notify_config()
        assert got["ok"] is False and "没有" in got["detail"]
    finally:
        events.reset()                  # 别把打桩环境装出来的 dispatcher 留给后面的用例


def test_index_loadable_reports_missing_index(monkeypatch, tmp_path):
    """索引不存在要如实说"需要 build"，而不是笼统的"不可用"。"""
    from app.rag import pipeline

    monkeypatch.setattr(pipeline, "INDEX_DIR", tmp_path / "no-index")
    monkeypatch.setattr(pipeline, "_STORE", None)
    got = selfcheck.check_index_loadable()
    assert got["ok"] is False
    assert "索引" in got["detail"]


def test_model_recent_without_any_trace_is_informational(monkeypatch):
    from app.observability import tracer

    monkeypatch.setattr(tracer, "read_recent", lambda limit=400: [])
    got = selfcheck.check_model_recent()
    assert got["ok"] is True and got["informational"] is True
    assert "还没有" in got["detail"]


def test_model_recent_reads_trace_instead_of_calling_the_model(monkeypatch):
    """★ 默认档**不能**调模型：探针会被监控系统每分钟调一次。"""
    from app.observability import tracer

    monkeypatch.setattr(tracer, "read_recent",
                        lambda limit=400: [{"type": "llm", "ts": "2026-10-02T12:00:00"}])
    called = {"n": 0}

    class Boom:
        @staticmethod
        def chat(*a, **kw):
            called["n"] += 1
            raise AssertionError("默认档不该调模型")

    monkeypatch.setitem(__import__("sys").modules, "app.llm", Boom)
    got = selfcheck.check_model_recent()
    assert got["ok"] is True and called["n"] == 0
    assert "2026-10-02T12:00:00" in got["detail"]


def test_model_recent_tolerates_records_without_timestamps(monkeypatch):
    """★ 这条是实测抓出来的假故障：

    有模型记录但 `ts` 全为空时，原先的 `max(... for s in stamps if s)`
    会在空生成器上抛 `ValueError` —— 于是 `/healthz` 报 503，
    让人以为依赖坏了，其实是**探针自己算错了**。

    "探针自己出错"与"依赖坏了"必须能区分：前者是 bug，后者才是故障。
    """
    from app.observability import tracer

    monkeypatch.setattr(tracer, "read_recent", lambda limit=400: [
        {"type": "llm"}, {"type": "llm", "ts": None},
        {"type": "llm", "ts": ""}, {"name": "chat"},
    ])
    got = selfcheck.check_model_recent()
    assert got["ok"] is True and got["informational"] is True
    assert "还没有" in got["detail"]


def test_model_recent_uses_the_newest_timestamp(monkeypatch):
    from app.observability import tracer

    monkeypatch.setattr(tracer, "read_recent", lambda limit=400: [
        {"type": "llm", "ts": "2026-10-01T10:00:00"},
        {"type": "llm", "ts": "2026-10-02T10:00:00"},
    ])
    got = selfcheck.check_model_recent()
    assert got["last_call_ts"] == "2026-10-02T10:00:00"


# ============================================================
# 二、汇总：失败项要点名
# ============================================================
def _patch_all_ok(monkeypatch):
    monkeypatch.setattr(selfcheck, "check_logs_writable", lambda d=None: {"ok": True, "detail": ""})
    monkeypatch.setattr(selfcheck, "check_disk_space", lambda d=None: {"ok": True, "detail": ""})
    monkeypatch.setattr(selfcheck, "check_index_loadable", lambda: {"ok": True, "detail": ""})
    monkeypatch.setattr(selfcheck, "check_approvals_readable", lambda: {"ok": True, "detail": ""})
    monkeypatch.setattr(selfcheck, "check_incidents_readable", lambda: {"ok": True, "detail": ""})
    monkeypatch.setattr(selfcheck, "check_notify_config", lambda: {"ok": True, "detail": ""})
    monkeypatch.setattr(selfcheck, "check_model_recent",
                        lambda: {"ok": True, "detail": "", "informational": True})


def test_probe_all_good(monkeypatch):
    _patch_all_ok(monkeypatch)
    got = selfcheck.probe(env={})
    assert got["ok"] is True and got["failing"] == []
    assert set(got["checks"]) == {"logs_writable", "disk_space", "index_loadable",
                                  "approvals_readable", "incidents_readable",
                                  "notify_config", "model_recent"}


def test_probe_names_the_failing_dependency(monkeypatch):
    """★ 这是这个接口存在的意义：503 时必须说清是哪一项坏了。"""
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(selfcheck, "check_incidents_readable",
                        lambda: {"ok": False, "detail": "事件日志损坏"})
    got = selfcheck.probe(env={})
    assert got["ok"] is False
    assert got["failing"] == ["incidents_readable"]
    assert "损坏" in got["checks"]["incidents_readable"]["detail"]


def test_informational_failure_does_not_flip_ok(monkeypatch):
    """"最近没人用过模型"不等于"服务有病" —— 只报不判。"""
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(selfcheck, "check_model_recent",
                        lambda: {"ok": False, "detail": "很久没调用",
                                 "informational": True})
    got = selfcheck.probe(env={})
    assert got["ok"] is True and got["failing"] == []


def test_probe_survives_a_check_that_raises(monkeypatch):
    """探针自己不能崩：某一项抛异常 → 记成该项失败，其余照跑。"""
    _patch_all_ok(monkeypatch)

    def boom():
        raise RuntimeError("探针内部炸了")

    monkeypatch.setattr(selfcheck, "check_index_loadable", boom)
    got = selfcheck.probe(env={})
    assert got["ok"] is False
    assert got["failing"] == ["index_loadable"]
    assert "RuntimeError" in got["checks"]["index_loadable"]["detail"]


def test_model_probe_only_runs_when_explicitly_enabled(monkeypatch):
    """真调模型花钱，只有 HEALTHZ_PROBE_MODEL=1 才跑。"""
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(selfcheck, "check_model_probe",
                        lambda: {"ok": True, "detail": "模型可用　⚠️ 本次检查会产生费用"})

    assert "model_probe" not in selfcheck.probe(env={})["checks"]
    enabled = selfcheck.probe(env={"HEALTHZ_PROBE_MODEL": "1"})
    assert "model_probe" in enabled["checks"]
    assert "会产生费用" in enabled["checks"]["model_probe"]["detail"]


def test_probe_reports_state_scope_as_process(monkeypatch):
    """★ M4 的可见性要求：如实标出"这些状态是进程内的"。

    多 worker 会让限流/额度/去重各算各的 —— 那是被审计列为阻断级的风险。
    真上 Redis 属于过度设计，但**必须让人看得见**这个边界。
    """
    _patch_all_ok(monkeypatch)
    got = selfcheck.probe(env={})
    assert got["state_scope"] == "process"
    assert "--workers 1" in got["state_note"]
    assert "Redis" in got["state_note"]


def test_probe_output_is_json_serializable(monkeypatch):
    """健康检查的响应体会被监控系统直接取用 —— 必须能序列化。"""
    _patch_all_ok(monkeypatch)
    assert json.dumps(selfcheck.probe(env={}), ensure_ascii=False)


def test_timed_adds_elapsed_ms_and_catches_exceptions():
    got = selfcheck._timed(lambda: {"ok": True})
    assert got["ok"] is True and isinstance(got["elapsed_ms"], int)

    def boom():
        raise ValueError("nope")

    bad = selfcheck._timed(boom)
    assert bad["ok"] is False and "ValueError" in bad["detail"]
    assert isinstance(bad["elapsed_ms"], int)


@pytest.mark.parametrize("detail_len", [0, 5000])
def test_timed_bounds_the_error_text(detail_len):
    """异常信息要截断：探针可能被公开访问，别把一大段栈信息吐出去。"""
    def boom():
        raise RuntimeError("x" * detail_len)

    got = selfcheck._timed(boom)
    assert len(got["detail"]) <= 200
