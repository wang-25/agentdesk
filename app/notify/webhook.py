# -*- coding: utf-8 -*-
"""
通用 Webhook 渠道：往一个自己控制的 URL POST JSON
=================================================

【为什么先做这个"最笨"的渠道，而不是先做钉钉/飞书】

自建 webhook 是**唯一一个契约由我们定**的出口：对面是谁由用户决定
（可以是自家的中转服务、也可以是 n8n / Bark / Server 酱这类转发器）。
它的 body 结构就是"通知层的数据模型"本身：

    {"title": ..., "text": ..., "source": "agentdesk",
     "ts": "<ISO8601 UTC>", "extra": {...}}

钉钉/飞书那两个渠道是把这份模型**翻译**成各家格式（markdown / text）。
翻译层出错是最常见的故障，而通用 webhook 直连 —— 只要它通了，
就能断定"出站这件事本身是通的"，把问题范围缩到翻译层。
调试顺序因此是固定的：**先自建 webhook，再钉钉，最后飞书。**

【失败判定的唯一标准：HTTP 状态码】

自建对端没有统一的业务错误码（各人写各人的），所以这里只认 2xx。
非 2xx 时错误信息里带**状态码 + 响应前 200 字**，两者缺一不可：
    · 只看状态码，会漏掉"500 页面里写着 JSON 解析失败：少了 text 字段"；
    · 只看 body，遇到 nginx 502 的空响应就完全不知道发生了什么。
对端排查时最常问的就是"你收到的是什么"，把答案预先塞进 error 里。
"""

from app.notify.base import Notifier, NotifyResult, build_body


class WebhookNotifier(Notifier):
    """POST JSON 到任意 URL。没有加签、没有业务码 —— 契约最小。"""

    name = "webhook"

    def __init__(self, url: str, timeout: int = 8, name: str = "webhook"):
        super().__init__(url, timeout=timeout, name=name)

    def send(self, title: str, text: str, *,
             payload: dict | None = None) -> NotifyResult:
        """投递。失败返回 ok=False 的 NotifyResult，**不抛异常**。

        空 URL 直接判失败而不是发一个畸形请求：调用方（dispatcher）
        本来就会跳过没配 URL 的渠道，这里兜的是"手工直接 new 出来用"的场景。
        """
        if not self.url:
            return NotifyResult(channel=self.name, ok=False, status=None,
                                error="未配置 webhook URL", skipped=True)
        return self._post(self.url, build_body(title, text, payload))
