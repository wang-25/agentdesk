# -*- coding: utf-8 -*-
"""
飞书自定义机器人渠道（text 消息 + 加签）
=========================================

【官方加签算法 —— 注意它与钉钉的差别，这是最容易串味的地方】

    飞书: sign = base64(hmac_sha256(key = f"{timestamp}\\n{secret}", msg = b""))
    钉钉: sign = urlencode(base64(hmac_sha256(key = secret, msg = f"{timestamp}\\n{secret}")))

两个算法的"待签材料"是一样的（都是 `timestamp\\nsecret`），但**放在
HMAC 的哪一侧完全不同**：

    · 钉钉：材料当**消息**（msg），secret 当**密钥**（key）
    · 飞书：材料当**密钥**（key），消息是**空字节串**（msg = b""）

写反了不会报错，只会收到 `{"code":19021,"msg":"sign match fail"}`。
更坑的是网上大量中文教程把两者抄混，所以这里把两个式子并排写在 docstring 里，
改的时候一眼能看出该动哪一侧。

另外两点：
  1. 飞书的时间戳进的是**秒**（`int(time.time())`），钉钉是毫秒。
     传成毫秒不会立刻失败 —— 飞书只校验"与服务器时间的偏差在 1 小时内"，
     毫秒值会偏出这个窗口（换算下来是几万年），于是报签名失败，
     而你会以为是算法写错了。
  2. 飞书的 sign **放在 body 里**（`{"timestamp": ..., "sign": ...}`），
     不像钉钉拼在 URL query 上。所以它不需要 URL 编码 —— 多编一次反而失败。

【★ 同样地：HTTP 200 也可能是失败】
飞书的约定是响应体里的 `code != 0` 即失败。经典场景是机器人开了
"签名校验"而我们对不上签 —— HTTP 依然是 200。只看状态码就会把
"一条都没发出去"记成成功。见 `_business_error`。
"""

import time

from app.notify.base import (
    Notifier,
    NotifyResult,
    sign_key_hmac_sha256_b64,
)

DEFAULT_NAME = "feishu"


def sign_feishu(secret: str, timestamp: int) -> str:
    """飞书加签。**纯函数**：同样的 (secret, timestamp) 永远得到同样的串。

        sign = base64(hmac_sha256(key=f"{timestamp}\\n{secret}", msg=b""))

    timestamp 用**秒**。这里不做 URL 编码 —— 飞书的 sign 走 body。
    """
    return sign_key_hmac_sha256_b64(f"{timestamp}\n{secret}")


class FeishuNotifier(Notifier):
    """飞书群机器人。有 secret 就把 timestamp/sign 放进 body，没有就裸调。"""

    name = DEFAULT_NAME

    def __init__(self, url: str, secret: str = "", timeout: int = 8):
        super().__init__(url, timeout=timeout, name=DEFAULT_NAME)
        self.secret = (secret or "").strip()

    def build_body(self, title: str, text: str, payload: dict | None,
                   timestamp: int) -> dict:
        """构造请求体。单独抽出来是为了**能被断言**：
        测试固定时间戳调它，验证 timestamp/sign 都在 body 里、且没被 URL 编码。"""
        body = {
            "msg_type": "text",
            # text 消息不能像 markdown 那样排版，所以把标题拼进第一行。
            # 飞书 text 消息里的换行要用 \n（不是 <br>）—— 写错会在群里
            # 看到字面量 <br>，属于纯观感问题，但一眼就能看出没测过。
            "content": {"text": self._render(title, text, payload)},
        }
        if self.secret:
            body["timestamp"] = str(timestamp)
            body["sign"] = sign_feishu(self.secret, timestamp)
        return body

    def send(self, title: str, text: str, *,
             payload: dict | None = None) -> NotifyResult:
        """发 text 消息。失败返回 ok=False，**不抛异常**。"""
        if not self.url:
            return NotifyResult(channel=self.name, ok=False,
                                error="未配置飞书 webhook URL", skipped=True)
        # 秒级时间戳。取一次用到底，避免签名与 body 里的 timestamp 跨秒。
        ts = int(time.time())
        return self._post(self.url, self.build_body(title, text, payload, ts))

    @staticmethod
    def _render(title: str, text: str, payload: dict | None) -> str:
        lines = [f"【{title}】", text or ""]
        if payload:
            lines.append("")
            lines += [f"{k}: {v}" for k, v in payload.items()]
        return "\n".join(lines)

    def _business_error(self, status: int, data: dict, raw: str) -> str:
        """`code != 0` → 失败；**没有 `code` 时退回看 `StatusCode`**。

        ★ 这两个字段的关系来自一次真实响应（拿真机器人实测时抓到的原始 body）：

            {"StatusCode": 0, "StatusMessage": "success",
             "code": 0, "msg": "success", "data": {}}

          `code`/`msg` 是现在的字段，`StatusCode`/`StatusMessage` 是旧字段，
          飞书**两个都回**。原实现只看 `code`，缺了就当成功 ——
          对只回旧字段的端点（自建转发器、老版本网关）来说，
          那会把"一条都没发出去"记成成功，而这正是本项目最警惕的失败形态：
          **主链路一切正常，只是没人收到通知。**

          顺序仍然是"先看 `code`"：新字段优先，旧字段只在它缺失时兜底，
          这样两种端点都能判对。
        """
        if not data:
            return ""
        code = data.get("code")
        if code is None:
            code = data.get("StatusCode")
        if code in (None, 0, "0"):
            return ""
        msg = (data.get("msg") or data.get("message")
               or data.get("StatusMessage") or raw)
        return f"code={code} msg={msg}"
