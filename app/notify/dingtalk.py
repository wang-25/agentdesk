# -*- coding: utf-8 -*-
"""
钉钉自定义机器人渠道（markdown 消息 + 加签）
=============================================

【官方加签算法（照抄，别自己发挥）】

    timestamp = 毫秒级时间戳（字符串）
    string_to_sign = f"{timestamp}\\n{secret}"
    sign = urlencode(base64(hmac_sha256(secret, string_to_sign)))

注意三个细节，每一个写错都表现为"服务端返回 310000 签名不匹配"，
而这个报错不会告诉你错在哪：

  1. **密钥在两个位置出现，且位置不同**：
     HMAC 的 key 是 secret，待签串是 `timestamp\\nsecret`。
     写成 `hmac(secret, timestamp)` 是最常见的错法。
  2. **两个时间戳不是一回事**：query 里的 `timestamp` 是**毫秒**
     （13 位），而进待签串的也是这个毫秒值。写成本地时间字符串、
     或者秒级时间戳，都会因为"与服务端时间对不上/对不上算法"失败。
  3. **sign 必须 URL 编码**：base64 里会出现 `+` `/` `=`，
     而 `+` 在 query string 里会被解析成空格。不编码 = 随机失败
     （取决于 base64 里恰好有没有 `+`，所以本地测十次可能九次是通的）。

【时间戳只能由调用方给（sign_dingtalk 是纯函数）】
理由与 base.py 里 `sign_hmac_sha256_b64` 的说明相同：把不确定性从被测量里
赶出去。测试用固定密钥 + 固定时间戳断言**那一串具体字符**，
将来谁改算法（或改编码方式）都会立刻红，而不是"看起来还在工作"。

【★ 最重要的一条：HTTP 200 也可能是失败】
钉钉的约定是：**响应体里的 `errcode != 0` 即失败**，HTTP 状态码经常还是 200。
典型场景：
    {"errcode": 310000, "errmsg": "keywords not in content"}
—— 机器人开了"自定义关键词"安全设置，而推送的正文里没有关键词。
这种情况下如果只看 status_code，我们会把"消息根本没发出去"记成通知成功，
于是**没有任何人会去查**：审计里一片绿，群里一条消息都没有。
所以 `_business_error` 必须判 `errcode`。
"""

import time

from app.notify.base import Notifier, NotifyResult, append_query, sign_hmac_sha256_b64

DEFAULT_NAME = "dingtalk"


def sign_dingtalk(secret: str, timestamp: int) -> str:
    """钉钉加签。**纯函数**：同样的 (secret, timestamp) 永远得到同样的串。

        string_to_sign = f"{timestamp}\\n{secret}"
        sign = urlencode(base64(hmac_sha256(key=secret, msg=string_to_sign)))

    timestamp 用**毫秒**（调用方负责乘 1000）。
    URL 编码是算法的一部分，不是可选的美化 —— 见模块头的第 3 点。
    """
    return sign_hmac_sha256_b64(
        secret, f"{timestamp}\n{secret}", urlencode=True)


class DingTalkNotifier(Notifier):
    """钉钉群机器人。有 secret 就加签，没有就裸调（机器人安全设置选 IP 白名单时）。"""

    name = DEFAULT_NAME

    def __init__(self, url: str, secret: str = "", timeout: int = 8):
        super().__init__(url, timeout=timeout, name=DEFAULT_NAME)
        self.secret = (secret or "").strip()

    def build_url(self, timestamp: int) -> str:
        """带签名的完整 URL。没有 secret 时原样返回。

        单独抽成方法是为了**能被断言**：测试里固定时间戳调它，
        就能验证 timestamp 与 sign 都在、都没被吃掉。
        """
        if not self.secret:
            return self.url
        return append_query(self.url, {
            "timestamp": timestamp,
            "sign": sign_dingtalk(self.secret, timestamp),
        })

    def send(self, title: str, text: str, *,
             payload: dict | None = None) -> NotifyResult:
        """发 markdown 消息。失败返回 ok=False，**不抛异常**。"""
        if not self.url:
            return NotifyResult(channel=self.name, ok=False,
                                error="未配置钉钉 webhook URL", skipped=True)

        # 毫秒时间戳：钉钉要求，且必须与签名用的是同一个值。
        # 先算一次存下来，绝不第二次调 time.time() —— 两次取值跨过毫秒边界，
        # 签名和 query 就会用不同时间戳，表现为偶发签名失败。
        ts_ms = int(time.time() * 1000)

        body = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": self._render(title, text, payload),
            },
        }
        return self._post(self.build_url(ts_ms), body)

    @staticmethod
    def _render(title: str, text: str, payload: dict | None) -> str:
        """拼 markdown 正文。

        【为什么 text 里可能带 payload】钉钉自定义机器人开了"自定义关键词"
        之后，**正文里必须包含关键词**才发得出去（这正是 errcode 310000 的
        成因）。把 payload 里的事实（主机、告警名、退出码）一并带上，
        既是给运维看的信息，也顺带更容易命中关键词。
        """
        lines = [f"### {title}", "", text or ""]
        if payload:
            lines += ["", "---", ""]
            lines += [f"- **{k}**: {v}" for k, v in payload.items()]
        return "\n".join(lines)

    def _business_error(self, status: int, data: dict, raw: str) -> str:
        """errcode != 0 → 失败。**字段缺失按成功处理**（见下）。

        【为什么 `errcode` 缺失时不判失败】自定义机器人的成功响应是
        `{"errcode":0,"errmsg":"ok"}`。但如果用户填的其实是个中转服务
        （自己写的转发器），它可能只回 200 空体。那种情况按"成功"处理
        更合理：我们唯一确知的事实是"对端收下了"。
        反过来把缺失当失败，会让所有自建转发场景被误报成失败。
        """
        if not data:
            return ""
        code = data.get("errcode")
        if code in (None, 0, "0"):
            return ""
        msg = data.get("errmsg") or data.get("sub_errmsg") or raw
        return f"errcode={code} errmsg={msg}"
