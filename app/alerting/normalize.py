# -*- coding: utf-8 -*-
"""告警归一化：把不同来源的告警统一成同一种结构。

只要统一了，后面的逻辑就不用关心数据是从 Alertmanager 来的、
从 Zabbix 来的、还是某个脚本直接 POST 上来的。

【本文件从 main.py 搬出来，只改了一处行为（见 `normalize_alerts` 里的 D6 说明）】
"""


def normalize_alerts(payload: dict) -> list:
    """把告警统一成同一种结构。

    兼容三种输入：
      1. Alertmanager 标准格式（顶层有 alerts 数组，元素带 labels / annotations）
      2. 最简单的扁平格式（直接给 alertname / host / summary）
      3. Zabbix 风格（字段直接平铺，没有 labels 嵌套）

    ★ 空批次不是告警（这条以前是错的）。

      原先是 `payload.get("alerts") or [payload]` —— 对 `{"alerts": []}` 来说，
      空列表是假值，于是**回退成"把整个 payload 当成一条告警"**，
      产出一条 `UnknownAlert`。

      后果不轻：`{"alerts": []}` 是告警系统明确表达的"本次没有告警"
      （例如恢复通知批次、或维护窗口内的空推送）。把它当成真实告警，
      在 `ALERT_AUTO_DIAGNOSE=1` 时会白跑一整轮模型 —— 而且**产出的是
      一个凭空捏造的故障结论**，比白花钱更糟。

      所以现在按"**有没有 alerts 这个键**"分支，而不是按"它是不是假值"分支：
      `{"alerts": []}` → 空列表（明确的无告警）；`{}` → 才回退成单条告警。
    """
    if not isinstance(payload, dict):
        # 非 dict 输入不做猜测：猜错的代价是凭空造出一条 UnknownAlert。
        #
        # 这里**保持既有的异常类型** `AttributeError`（原实现是
        # `payload.get("alerts")` 撞出来的），因为这属于已登记的技术债 D7，
        # 收紧成 TypeError 是一次独立的取舍，不该夹在 M2 里偷偷改掉契约。
        # 唯一的变化是把它变成**显式抛出** —— 靠"碰巧撞出一个异常"来表达契约，
        # 下一个人重构时会顺手把它改没。
        raise AttributeError(
            f"告警负载必须是 dict，收到 {type(payload).__name__}。"
            "HTTP 路径不会走到这里（/webhook/alert 的 body 声明为 dict）；"
            "内部调用方传列表请先包成 {'alerts': [...]}。")

    if "alerts" in payload:
        raw_list = payload.get("alerts") or []
    else:
        raw_list = [payload]

    result = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        labels = raw.get("labels") or {}
        annotations = raw.get("annotations") or {}

        # instance 常带端口（web-01:9100），主机名只取冒号前面那段
        instance = (labels.get("instance") or raw.get("host")
                    or raw.get("instance") or "")
        host = instance.split(":")[0] if instance else None

        result.append({
            "alertname": (labels.get("alertname") or raw.get("alertname")
                          or "UnknownAlert"),
            "severity": (labels.get("severity") or raw.get("severity")
                         or "unknown"),
            "instance": instance,
            "host": host,
            "service": labels.get("service") or raw.get("service"),
            "summary": annotations.get("summary") or raw.get("summary") or "",
            "description": (annotations.get("description")
                            or raw.get("description") or ""),
            "status": raw.get("status") or payload.get("status") or "firing",
        })
    return result


def alert_to_question(alert: dict) -> str:
    """把告警拼成一句自然语言，交给意图解析器去理解。

    【为什么不直接按字段规则判断，而要绕一圈用模型？】
    规则判断只能处理你预先想到的情况。而告警名是千奇百怪的
    （NginxHighErrorRate、DiskSpaceLow、ServiceDown……），
    写规则永远补不完。让模型理解语义，是把「穷举」换成「理解」。
    """
    parts = [f"收到告警：{alert['alertname']}"]
    if alert.get("host"):
        parts.append(f"主机：{alert['host']}")
    if alert.get("service"):
        parts.append(f"服务：{alert['service']}")
    if alert.get("severity") and alert["severity"] != "unknown":
        parts.append(f"级别：{alert['severity']}")
    if alert.get("summary"):
        parts.append(f"摘要：{alert['summary']}")
    if alert.get("description"):
        parts.append(f"详情：{alert['description']}")
    parts.append("请判断应该采取什么动作，以及风险等级。")
    return "；".join(parts)
