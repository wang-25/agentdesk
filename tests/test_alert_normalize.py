# -*- coding: utf-8 -*-
"""告警归一化 —— AIOps 的入口，也是"无人值守"这条链路的第一道窄门。

不同的告警系统推来的格式完全不同：Alertmanager 是顶层 `alerts[]` + labels/
annotations，Zabbix 更像是一个平铺的 dict。归一化就是把这些统一成同一种结构，
后面的意图解析与风险判定就不用关心数据是从哪来的。

这一层的特点是**输入来自外部系统，字段没有任何保证**：可能少 `labels`、
可能少 `annotations`、可能是裸的一条、可能整包是空的。所以用例的重心是
"缺东西时会发生什么"：

    · 不能抛异常 —— 告警入口一抛，那条告警就永远消失了（外部系统不会重放）
    · 缺字段要落到可读的默认值（`UnknownAlert` / `""`），而不是 `None` 漏下去
    · 拼出来的问题文本必须带**主机与服务** —— 少了这两个，
      诊断会去查一台不知道哪台机器上的某个不知道什么服务，
      而模型会一本正经地给出一堆看起来合理的建议

零成本、不联网：这两个函数都是纯函数，不碰模型（`fake_chat` 用不上，
所以本文件不需要在顶部 import app.main 也能跑）。
"""


import pytest

from app import main as m

# ============================================================
# 一、三种输入格式
# ============================================================
def test_alertmanager_standard_payload():
    """Alertmanager 标准格式：顶层 `alerts[]`，元素带 `labels` / `annotations`。

    这是主路径，所有字段都要落到正确的位置上：
    `instance` 保留原样（带端口）、`host` 取冒号前那段、
    `summary`/`description` 来自 annotations 而不是 labels。
    """
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "NginxHighErrorRate",
                "severity": "critical",
                "instance": "web-01:9100",
                "service": "nginx",
            },
            "annotations": {
                "summary": "nginx 5xx 比例超过 5%",
                "description": "持续 10 分钟，影响面较大",
            },
        }],
    }
    alerts = m.normalize_alerts(payload)
    assert len(alerts) == 1
    got = alerts[0]
    assert got["alertname"] == "NginxHighErrorRate"
    assert got["severity"] == "critical"
    assert got["instance"] == "web-01:9100", "instance 要保留端口原样，便于回跳监控"
    assert got["host"] == "web-01", "host 必须剥掉端口 —— SSH 目标是 web-01"
    assert got["service"] == "nginx"
    assert got["summary"] == "nginx 5xx 比例超过 5%"
    assert got["description"] == "持续 10 分钟，影响面较大"
    assert got["status"] == "firing"


def test_alertmanager_multi_alert_payload_keeps_order_and_count():
    """一个 payload 里可能有多条告警（成组推送），必须**逐条**归一化。

    漏掉成组里的第一条/只取第一条，都会让"一次故障多条告警"场景下
    大部分告警被静默丢弃 —— 而这恰恰是真实故障的常见形态。
    """
    payload = {"alerts": [
        {"labels": {"alertname": "DiskSpaceLow", "instance": "db-01:9100"}},
        {"labels": {"alertname": "DiskSpaceLow", "instance": "web-02:9100"}},
        {"labels": {"alertname": "ServiceDown", "instance": "web-03"}},
    ]}
    alerts = m.normalize_alerts(payload)
    assert [a["alertname"] for a in alerts] == \
        ["DiskSpaceLow", "DiskSpaceLow", "ServiceDown"]
    assert [a["host"] for a in alerts] == ["db-01", "web-02", "web-03"]


def test_zabbix_style_payload():
    """Zabbix 风格：字段平铺在顶层，没有 labels/annotations 包装。

    这也是"为什么必须看真实 API 而不是凭记忆猜"的例子 ——
    它不是 Alertmanager 那种嵌套结构，所以 `labels` 缺失路径必须走得通。
    """
    alerts = m.normalize_alerts({
        "alertname": "Zabbix agent unreachable",
        "host": "web-01",
        "severity": "high",
        "service": "nginx",
        "summary": "agent 探活失败",
        "status": "firing",
    })
    assert len(alerts) == 1
    got = alerts[0]
    assert got["alertname"] == "Zabbix agent unreachable"
    assert got["host"] == "web-01"
    assert got["severity"] == "high"
    assert got["service"] == "nginx"
    assert got["summary"] == "agent 探活失败"
    assert got["status"] == "firing"


def test_bare_flat_dict_with_only_host():
    """最简的裸 dict（只有主机名）也要能变成一条告警。

    手工调试、脚本自检都会用这种最小 payload 打 `/webhook/alert`；
    它必须给出可读的默认名，而不是 None 或者报错。
    """
    alerts = m.normalize_alerts({"host": "web-01"})
    assert len(alerts) == 1
    assert alerts[0]["host"] == "web-01"
    assert alerts[0]["alertname"] == "UnknownAlert"
    assert alerts[0]["severity"] == "unknown"


def test_flat_field_can_also_come_from_instance_key():
    """平铺格式里用 `instance` 也认（不只是 `host`）—— 真实系统两种写法都有。"""
    alerts = m.normalize_alerts({"alertname": "CPUHigh", "instance": "db-01:9100"})
    assert alerts[0]["host"] == "db-01"
    assert alerts[0]["instance"] == "db-01:9100"


# ============================================================
# 二、字段缺失 / 空 payload：容错而不是抛异常
# ============================================================
def test_empty_payload_does_not_crash_and_yields_one_placeholder():
    """★ 空 payload 不能抛异常 —— 它是纯函数里最容易被忽略的输入。

    当前行为：`payload.get("alerts") or [payload]` 会把空 dict 当成
    "一条没有任何字段的告警"，于是产出一条 `UnknownAlert`。
    记录这个行为本身很重要：调用方（webhook）会拿它去拼问题、调模型 ——
    哪怕这条告警毫无信息量，链路也必须能走完并留下审计。
    """
    alerts = m.normalize_alerts({})
    assert len(alerts) == 1
    got = alerts[0]
    assert got["alertname"] == "UnknownAlert"
    assert got["severity"] == "unknown"
    assert got["host"] is None, "没有主机信息时必须是 None，不能是空串或 'None'"
    assert got["service"] is None
    assert got["summary"] == ""
    assert got["description"] == ""
    assert got["status"] == "firing"


def test_empty_alerts_array_falls_back_to_the_payload_itself():
    """★ 注意 `alerts: []` 的语义：`or` 把它当成"没有数组"，于是回退成整包一条。

    这是**已知的语义含混**：`[]`（明确说了"这次没有告警"）与
    `{}`（没给任何信息）在当前实现里产出完全一样的一条 `UnknownAlert`。
    后果：Alertmanager 推送一次空批次，也会触发一轮"UnknownAlert 诊断"
    （若开启了自动诊断，就是一次白花的模型调用）。
    这里固化当前行为；若改成"空数组返回空列表"，这条要跟着改。
    """
    assert m.normalize_alerts({"alerts": []}) == m.normalize_alerts({})
    assert [a["alertname"] for a in m.normalize_alerts({"alerts": []})] == \
        ["UnknownAlert"]


@pytest.mark.parametrize("raw", [
    pytest.param({}, id="totally-empty"),
    pytest.param({"labels": {}}, id="empty-labels"),
    pytest.param({"labels": None}, id="null-labels"),
    pytest.param({"labels": {}, "annotations": None}, id="null-annotations"),
    pytest.param({"labels": {"instance": ""}}, id="empty-instance"),
    pytest.param({"host": ""}, id="empty-host"),
])
def test_missing_or_null_subfields_are_tolerated(raw):
    """`labels` / `annotations` 缺失、为 None、为空——都不能崩。

    这条盯的是 `raw.get("labels") or {}` 里的 `or`：
    去掉它就会在 `None.get(...)` 上直接 AttributeError，
    而告警入口一抛异常，这条告警就永远消失了（外部系统不会重放）。
    """
    alerts = m.normalize_alerts({"alerts": [raw]})
    assert len(alerts) == 1
    assert alerts[0]["alertname"] == "UnknownAlert"
    assert alerts[0]["host"] is None


def test_labels_win_over_flat_fields_when_both_present():
    """两种格式的字段同时存在时，**labels 优先**（它来自标准格式，更权威）。

    反过来取的话，一个带上层 `host` 的转发包装会覆盖掉告警自己的主机名 ——
    诊断就会跑到错误的机器上。
    """
    alerts = m.normalize_alerts({
        "alertname": "from-top-level",
        "host": "top-level-host",
        "alerts": [{
            "alertname": "from-flat-element",
            "host": "element-host",
            "labels": {"alertname": "from-labels", "instance": "labels-host:9100"},
        }],
    })
    got = alerts[0]
    assert got["alertname"] == "from-labels"
    assert got["host"] == "labels-host"


def test_alert_level_status_wins_over_payload_status():
    """单条告警自己的 status 优先于整包的 status。

    成组推送里各条状态可以不同（有的 resolved、有的 firing），
    一律用整包状态会让"已恢复"的告警被当成还在着火。
    """
    payload = {"status": "firing",
               "alerts": [{"labels": {"alertname": "A"}, "status": "resolved"}]}
    assert m.normalize_alerts(payload)[0]["status"] == "resolved"


def test_unknown_severity_is_labelled_unknown_not_dropped():
    """认不出的级别落到 `unknown`，而不是留空或者丢掉这条告警。

    留空会让 `alert_to_question` 静默跳过级别那一段；
    "unknown" 至少是显式的，人一眼能看出"这条告警没带级别"。
    """
    assert m.normalize_alerts({"labels": {"alertname": "A"}})[0]["severity"] == "unknown"


def test_output_always_has_the_full_schema():
    """无论输入多残缺，输出结构的字段集合必须**固定**。

    下游是按 key 取值的（`alert_to_question`、审计、看板）。
    少一个 key 就是 KeyError，而它在"某类告警恰好缺某个字段"时才出现 ——
    是最难复现的一类线上事故。
    """
    expected = {"alertname", "severity", "instance", "host", "service",
                "summary", "description", "status"}
    payloads: list[dict] = [{}, {"host": "web-01"}, {"alerts": [{}]},
                            {"alerts": [{"labels": {"alertname": "A"}}]}]
    for payload in payloads:
        for alert in m.normalize_alerts(payload):
            assert set(alert) == expected, payload


def test_non_dict_payload_raises_instead_of_guessing():
    """★ 已知问题：`payload` 不是 dict 时直接 AttributeError。

    复现：`normalize_alerts([])` → `[] .get("alerts")` →
    `AttributeError: 'list' object has no attribute 'get'`
    （`normalize_alerts` 开头那句 `payload.get("alerts")`）。

    为什么值得记一笔：FastAPI 把 `/webhook/alert` 的 body 声明成 `dict`，
    所以正常走 HTTP 时进不来；
    但内部调用（脚本、自检、后续的告警聚合器）可能直接传列表。
    当前实现不做类型检查，这里固化这个现状。
    """
    with pytest.raises(AttributeError):
        m.normalize_alerts([])  # type: ignore[arg-type]


# ============================================================
# 三、alert_to_question：主机与服务必须在文本里
# ============================================================
def _normalized(raw: dict) -> dict:
    return m.normalize_alerts(raw)[0]


def test_question_mentions_alert_name():
    """问题文本必须从告警名开头发起 —— 它是模型判断动作的主要依据。"""
    question = m.alert_to_question(_normalized({"alertname": "DiskSpaceLow"}))
    assert question.startswith("收到告警：DiskSpaceLow")


def test_question_contains_host_and_service():
    """★ 主机与服务必须出现在问题文本里。

    这是整条链路里最容易"静默跑偏"的一处：少了主机，模型不知道查哪台；
    少了服务，它只能给通用建议。两者都不会报错 ——
    只会让诊断看起来很有道理但答非所问。
    """
    alert = _normalized({"alerts": [{
        "labels": {"alertname": "NginxHighErrorRate", "instance": "web-01:9100",
                   "service": "nginx"},
        "annotations": {"summary": "5xx 比例过高"},
    }]})
    question = m.alert_to_question(alert)
    assert "web-01" in question, "问题文本里没有主机，诊断会跑偏"
    assert "nginx" in question, "问题文本里没有服务，只能给通用建议"
    assert "NginxHighErrorRate" in question
    assert "5xx 比例过高" in question


def test_question_uses_the_stripped_host_not_the_instance_with_port():
    """要写进问题文本的是**剥掉端口的主机名**。

    `web-01:9100` 是监控侧的 scrape 地址，不是 SSH 目标；
    把它当主机名交给模型/工具，连上去就是 connection refused。
    """
    question = m.alert_to_question(_normalized({"alertname": "A", "host": "web-01:9100"}))
    assert "web-01" in question
    assert "9100" not in question, "端口不该出现在写给模型的问题文本里"


def test_question_skips_missing_host_and_service_without_leaving_dangling_text():
    """★ `labels` 缺失时也要可读：不能出现 `主机：None` 这种内容。

    把 None 拼进提示词比不拼更糟 —— 模型会真的去"查"一台叫 None 的机器，
    或者把这个字符串当成主机名回填进后续的结构化输出。
    """
    question = m.alert_to_question(_normalized({}))
    assert "None" not in question
    assert "主机" not in question
    assert "服务" not in question
    assert "UnknownAlert" in question
    assert question.endswith("请判断应该采取什么动作，以及风险等级。")


def test_question_omits_unknown_severity():
    """级别认不出来时不该把 `unknown` 写进提示词。

    "级别：unknown"对模型没有任何信息量，只会占 token、拉长提示词，
    还可能被它理解成"有一个叫 unknown 的级别"。
    """
    question = m.alert_to_question(_normalized({"alertname": "A"}))
    assert "级别：unknown" not in question
    assert "级别" not in question


def test_question_includes_severity_when_known():
    """级别明确时必须带上 —— 它是"这事有多急"的唯一线索。"""
    question = m.alert_to_question(
        _normalized({"alertname": "A", "severity": "critical"}))
    assert "级别：critical" in question


def test_question_includes_summary_and_description():
    """摘要与详情都要带上：摘要给判断方向，详情给具体数值/时间。"""
    alert = _normalized({"alerts": [{
        "labels": {"alertname": "DiskSpaceLow"},
        "annotations": {"summary": "磁盘剩余 5%", "description": "/var 已用 95%"},
    }]})
    question = m.alert_to_question(alert)
    assert "摘要：磁盘剩余 5%" in question
    assert "详情：/var 已用 95%" in question


def test_question_ends_with_the_action_request():
    """最后一句必须是明确的动作请求。

    少了它，模型只会描述现象而不会给出"该做什么"——
    这条链路的产物是**处置预案**，不是故障报告。
    """
    question = m.alert_to_question(_normalized({"alertname": "A"}))
    assert "采取什么动作" in question
    assert "风险等级" in question


def test_question_is_a_single_readable_line():
    """输出必须是一行、无换行、无多余分隔符。

    它是 `parse_intent` 的 user 消息内容；换行与空段会让"一次告警一次解析"
    的日志/审计难以对齐，也不利于人肉复用这条文本去手工重放。
    """
    question = m.alert_to_question(_normalized({
        "alertname": "A", "host": "web-01", "service": "nginx",
        "severity": "high", "summary": "s", "description": "d",
    }))
    assert "\n" not in question
    assert "；；" not in question
    assert not question.endswith("；")


def test_question_reflects_every_normalized_field():
    """逐字段自证：归一化出来的每个有值字段都要在问题文本里出现一次。

    这条是防"归一化了但没用上"的：新增字段忘了拼进提示词时，
    归一化再准也传不到模型这边。
    """
    alert = _normalized({"alerts": [{
        "labels": {"alertname": "NginxHighErrorRate", "instance": "web-01:9100",
                   "service": "nginx", "severity": "critical"},
        "annotations": {"summary": "5xx 比例过高", "description": "持续 10 分钟"},
    }]})
    for value in (alert["alertname"], alert["host"], alert["service"],
                  alert["severity"], alert["summary"], alert["description"]):
        assert value in m.alert_to_question(alert), f"{value!r} 没进问题文本"
