# -*- coding: utf-8 -*-
"""告警聚合与抑制 —— "一场风暴 = 一个故障"这条判断的守卫。

这一层决定了**要不要为一条告警花钱调模型**，所以它错的两个方向都要测：

    · 该聚的没聚 → 500 条告警 = 500 次诊断（账单炸了）
    · 不该聚的聚了 → 三个不同故障被并成一个，值班的人只看到一部分（漏故障）

后者的代价更高，所以"不同服务/不同主机必须分开"和"同源必须合并"在下面同等重要。
时间全部用**注入的假时钟**推进，测试不 sleep、不看墙上时间。
"""

import json
import os
import threading

import pytest

from app.alerting import normalize as N
from app.alerting.aggregator import AlertAggregator
from app.alerting.silence import Silence


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def agg(clock):
    return AlertAggregator(window_seconds=600, clock=clock)


def _alert(name="DiskSpaceLow", host="web-01", service="nginx"):
    return {"alertname": name, "host": host, "service": service,
            "severity": "warning", "summary": "", "description": ""}


# ============================================================
# 一、聚合键
# ============================================================
def test_key_groups_by_host_and_service_when_aggregating(agg):
    """同机同服务的不同告警名 → **同一个键**（这正是"一个故障多个侧面"）。"""
    a = agg.key(_alert("DiskSpaceLow"))
    b = agg.key(_alert("NginxHighErrorRate"))
    assert a == b == "web-01|nginx"


def test_different_host_or_service_is_a_different_failure(agg):
    """不同主机或不同服务**必须分开** —— 并错了等于漏故障。"""
    keys = {
        agg.key(_alert(host="web-01", service="nginx")),
        agg.key(_alert(host="web-02", service="nginx")),
        agg.key(_alert(host="web-01", service="mysql")),
    }
    assert len(keys) == 3


def test_legacy_mode_keys_by_alertname_and_host(clock):
    """ALERT_AGGREGATE=0：完全退回改动前的 (alertname, host) 语义。"""
    legacy = AlertAggregator(window_seconds=600, aggregate=False, clock=clock)
    assert legacy.key(_alert("DiskSpaceLow")) == "DiskSpaceLow|web-01"
    assert legacy.key(_alert("NginxHighErrorRate")) != legacy.key(_alert("DiskSpaceLow"))


def test_missing_host_or_service_does_not_become_a_wildcard(agg):
    """字段缺失用 `-` 占位，**不能让 None 变成"匹配一切"**。

    这是白名单里混进 `/` 那类错误的同一个形状：一个空值悄悄让整张表失效。
    两台都没报主机名的机器不该因为"都没有主机名"而被并成一个故障。
    """
    k1 = agg.key({"alertname": "A", "host": None, "service": None})
    k2 = agg.key({"alertname": "A", "host": "", "service": ""})
    k3 = agg.key({"alertname": "A", "host": "web-01", "service": None})
    assert k1 == k2 == "-|-"
    assert k3 != k1
    assert None not in (k1, k3)


# ============================================================
# 二、窗口内重复与过期
# ============================================================
def test_first_alert_is_new(agg):
    v = agg.ingest(_alert())
    assert v.is_new is True
    assert v.members == 1


def test_repeat_inside_window_is_suppressed_and_counted(agg, clock):
    agg.ingest(_alert("DiskSpaceLow"))
    clock.now += 10
    v2 = agg.ingest(_alert("NginxHighErrorRate"))
    assert v2.is_new is False
    assert v2.members == 2, "被抑制的告警也要计数，否则看不出风暴规模"
    assert v2.prev_incident_id == ""


def test_repeat_after_window_is_new_again(agg, clock):
    agg.ingest(_alert())
    clock.now += 601
    assert agg.ingest(_alert()).is_new is True


def test_suppressed_alerts_do_not_extend_the_window(agg, clock):
    """★ 被抑制的告警**不刷新**处理时间。

    刷新的话，只要风暴不停这个键就永远不过期 —— 一条持续 8 小时的抖动
    会把之后真正的新故障一并吞掉。宁可让它按首次时间过期。
    """
    agg.ingest(_alert())
    for _ in range(5):
        clock.now += 100
        agg.ingest(_alert())            # 一直被抑制
    clock.now += 601 - 500              # 距**首次**处理已超过窗口
    assert agg.ingest(_alert()).is_new is True


def test_legacy_mode_suppresses_only_the_same_alertname(clock):
    legacy = AlertAggregator(window_seconds=600, aggregate=False, clock=clock)
    legacy.ingest(_alert("DiskSpaceLow"))
    assert legacy.ingest(_alert("DiskSpaceLow")).is_new is False
    assert legacy.ingest(_alert("NginxHighErrorRate")).is_new is True


# ============================================================
# 三、与事件的绑定（被抑制 ≠ 被丢弃）
# ============================================================
def test_suppressed_alerts_report_which_incident_they_joined(agg, clock):
    v1 = agg.ingest(_alert())
    agg.bind_incident(v1.key, "inc-abcd1234")
    clock.now += 5
    v2 = agg.ingest(_alert("NginxUpstreamTimeout"))
    assert v2.is_new is False
    assert v2.prev_incident_id == "inc-abcd1234", \
        "被抑制的告警必须能说清自己并到哪个事件去了"


def test_incident_of_unknown_key_is_empty(agg):
    assert agg.incident_of("nope|nope") == ""


# ============================================================
# 四、内存有界（这是对一处真实缺陷的回归）
# ============================================================
def test_table_is_bounded_under_a_long_storm(clock):
    """★ 长期运行内存必须有界。

    原先的 `_ALERT_LAST_SEEN` 只增不减：不报错、不告警，只是一天天变大。
    这里推 2000 条**互不相同**的告警（模拟各种抖动），表大小必须被上限压住。
    """
    a = AlertAggregator(window_seconds=600, max_entries=100, clock=clock)
    for i in range(2000):
        clock.now += 1
        a.ingest(_alert(name=f"A{i}", host=f"h{i % 50}", service=f"s{i % 20}"))
    assert a.size() <= 100, f"去重表涨到了 {a.size()}，上限是 100"


def test_expired_entries_are_pruned(clock):
    a = AlertAggregator(window_seconds=60, clock=clock)
    for i in range(10):
        a.ingest(_alert(name=f"A{i}", host=f"h{i}"))    # 不同主机 → 不同键
    assert a.size() == 10
    clock.now += 61
    assert a.prune() == 10
    assert a.size() == 0


def test_prune_keeps_entries_still_inside_the_window(clock):
    a = AlertAggregator(window_seconds=600, clock=clock)
    a.ingest(_alert(name="old", host="h-old"))
    clock.now += 500
    a.ingest(_alert(name="fresh", host="h-fresh"))
    clock.now += 200                     # old 过期了，fresh 还没
    a.prune()
    assert a.size() == 1
    assert a.ingest(_alert(name="fresh", host="h-fresh")).is_new is False


def test_same_key_different_alertnames_share_one_entry(clock):
    """★ 聚合成效的正面证据：同机同服务的不同告警名只占**一条**表项。

    （这条用例的由来：我最初写上面两条测试时用了同名默认主机，
     结果 10 条告警只留下 1 条表项 —— 那是聚合在正常工作，不是 bug。）
    """
    a = AlertAggregator(window_seconds=600, clock=clock)
    for name in ("DiskSpaceLow", "NginxHighErrorRate", "NginxUpstreamTimeout"):
        a.ingest(_alert(name=name, host="web-01", service="nginx"))
    assert a.size() == 1


def test_concurrent_ingest_does_not_corrupt_the_table(clock):
    """FastAPI 的同步接口跑在线程池里 —— 并发是真的会发生的。"""
    a = AlertAggregator(window_seconds=600, max_entries=50, clock=lambda: 1_700_000_000.0)
    errors = []

    def worker(seed):
        try:
            for i in range(200):
                a.ingest(_alert(name=f"A{seed}-{i}", host=f"h{i % 5}", service="s"))
        except Exception as exc:                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert a.size() <= 50


def test_clear_resets_the_table(agg):
    agg.ingest(_alert())
    agg.clear()
    assert agg.size() == 0
    assert agg.ingest(_alert()).is_new is True


def test_describe_reports_mode_and_bounds(agg):
    d = agg.describe()
    assert d["mode"] == "aggregate"
    assert d["window_seconds"] == 600
    assert d["entries"] == 0
    assert d["max_entries"] == 5000
    assert AlertAggregator(aggregate=False).describe()["mode"] == "legacy-exact"


# ============================================================
# 五、压制窗口（Silence）
# ============================================================
def _write_silences(path, rules):
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def silence_path(tmp_path):
    return tmp_path / "alert_silences.json"


def test_missing_silence_file_means_no_suppression(silence_path):
    s = Silence(path=silence_path)
    assert s.load() == []
    assert s.match(_alert()) is None


def test_future_until_suppresses(silence_path, clock):
    _write_silences(silence_path, [{
        "alertname_prefix": "DiskSpace", "host_prefix": "db-01",
        "until": "2033-01-01T00:00:00", "note": "磁盘扩容（CHG-2026-101）"}])
    hit = Silence(path=silence_path, clock=clock).match(_alert("DiskSpaceLow", "db-01"))
    assert hit and hit["note"].startswith("磁盘扩容")


def test_past_until_does_not_suppress(silence_path, clock):
    _write_silences(silence_path, [{
        "alertname_prefix": "DiskSpace", "until": "2020-01-01T00:00:00"}])
    assert Silence(path=silence_path, clock=clock).match(_alert("DiskSpaceLow")) is None


def test_unparseable_until_fails_open(silence_path, clock):
    """★ 解析不出截止时间就不抑制（fail-open）。

    抑制本身是"少做一件事"，这条路出错时更吵一点没关系；
    反过来 fail-closed 会把真告警藏起来，而"被藏起来的告警"是最难查的一类事故。
    """
    _write_silences(silence_path, [
        {"alertname_prefix": "DiskSpace", "until": "下周三"},
        {"alertname_prefix": "DiskSpace", "until": "not-a-date"},
        {"alertname_prefix": "DiskSpace"},                     # 缺 until
    ])
    assert Silence(path=silence_path, clock=clock).match(_alert("DiskSpaceLow")) is None


def test_prefix_does_not_match_everything_by_accident(silence_path, clock):
    _write_silences(silence_path, [{
        "alertname_prefix": "DiskSpace", "host_prefix": "db-",
        "until": "2033-01-01T00:00:00"}])
    s = Silence(path=silence_path, clock=clock)
    assert s.match(_alert("DiskSpaceLow", host="db-01")) is not None
    assert s.match(_alert("DiskSpaceLow", host="web-01")) is None, "主机不该被匹配"
    assert s.match(_alert("NginxHighErrorRate", host="db-01")) is None, "告警名不该被匹配"


def test_missing_dimension_means_no_restriction(silence_path, clock):
    """字段缺失 = 该维度不限制；显式空串 "" 才是匹配一切。两者语义不同。"""
    _write_silences(silence_path, [{
        "alertname_prefix": "DiskSpace", "until": "2033-01-01T00:00:00"}])
    s = Silence(path=silence_path, clock=clock)
    assert s.match(_alert("DiskSpaceLow", host="any-host")) is not None

    _write_silences(silence_path, [{
        "alertname_prefix": "DiskSpace", "host_prefix": "",
        "until": "2033-01-01T00:00:00"}])
    assert Silence(path=silence_path, clock=clock, ).match(
        _alert("DiskSpaceLow", host="any-host")) is not None


def test_broken_json_does_not_suppress_anything(silence_path, clock):
    silence_path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    s = Silence(path=silence_path, clock=clock)
    assert s.load() == []
    assert s.match(_alert()) is None


def test_silences_wrapper_is_tolerated(silence_path, clock):
    _write_silences(silence_path, {"silences": [{
        "alertname_prefix": "DiskSpace", "until": "2033-01-01T00:00:00"}]})
    assert Silence(path=silence_path, clock=clock).match(_alert("DiskSpaceLow"))


def test_file_change_is_picked_up_without_restart(silence_path, clock):
    """改了抑制文件必须立刻生效，否则每次变更都要重启服务。

    注意这里用 `os.utime` **显式推进 mtime**：文件系统的时间戳粒度有限，
    测试里两次写入发生在同一刻度内时，只靠 mtime 会看不出变化 ——
    那是测试环境的性质，不是被测代码的缺陷（缓存键是 mtime_ns + size，
    已经比单独的 mtime 稳得多）。
    """
    _write_silences(silence_path, [])
    s = Silence(path=silence_path, clock=clock)
    assert s.match(_alert("DiskSpaceLow")) is None

    _write_silences(silence_path, [{
        "alertname_prefix": "DiskSpace", "until": "2033-01-01T00:00:00"}])
    st = silence_path.stat()
    os.utime(silence_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert s.match(_alert("DiskSpaceLow")) is not None, "改了文件还不生效，等于每次都要重启"


# ============================================================
# 六、归一化：空批次不是告警（D6 修复）
# ============================================================
def test_empty_alerts_array_is_not_a_phantom_alert():
    """★ `{"alerts": []}` 是"本次没有告警"，不是一条 UnknownAlert。

    原先是 `payload.get("alerts") or [payload]`：空列表是假值 → 回退成把整个
    payload 当告警 → 凭空造出一条 UnknownAlert。开自动诊断时会白跑一轮模型，
    **而且产出的是一个捏造出来的故障结论**，比白花钱更糟。
    """
    assert N.normalize_alerts({"alerts": []}) == []


def test_missing_alerts_key_still_falls_back_to_single_alert():
    """没有 alerts 键时才回退成"把 payload 当一条告警" —— 旧行为保留。"""
    got = N.normalize_alerts({"alertname": "DiskSpaceLow", "host": "web-01"})
    assert len(got) == 1
    assert got[0]["alertname"] == "DiskSpaceLow"


def test_alertmanager_batch_is_normalized_and_ordered():
    payload = {"alerts": [
        {"labels": {"alertname": "A", "instance": "web-01:9100"},
         "annotations": {"summary": "s1"}},
        {"labels": {"alertname": "B", "instance": "web-02:9100"}},
    ]}
    got = N.normalize_alerts(payload)
    assert [a["alertname"] for a in got] == ["A", "B"], "顺序必须保持（排障要看先后）"
    assert got[0]["host"] == "web-01", "端口不该进主机名"
    assert got[0]["summary"] == "s1"


def test_normalized_field_set_is_constant():
    """字段集合恒定 —— 少了字段，下游就是 KeyError。"""
    expected = {"alertname", "severity", "instance", "host", "service",
                "summary", "description", "status"}
    got = N.normalize_alerts({"alerts": [{"labels": {"alertname": "A"}}, {}]})
    assert len(got) == 2
    for alert in got:
        assert set(alert) == expected


def test_non_dict_payload_still_raises_attribute_error():
    """登记在案的技术债 D7：保持既有异常类型，只是改成显式抛出。"""
    with pytest.raises(AttributeError):
        N.normalize_alerts([])  # type: ignore[arg-type]
