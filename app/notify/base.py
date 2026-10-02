# -*- coding: utf-8 -*-
"""
通知层的地基：结果类型 + 抽象渠道 + 共享的 HTTP 发送 + 脱敏
============================================================

【这个包解决什么问题】

整套系统原来的最后一步是"写审计日志"。审计日志的问题不是它不好，而是
**它不会主动找人**：凌晨三点诊断完，结论安安静静躺在 logs/audit.jsonl 里，
没有人被叫醒。这一层补的就是这个出口：把结论推出到运维人真正会看的地方
（企业微信/钉钉/飞书/自建 webhook）。

【为什么通知必须是"旁路"，而且旁路要旁路到骨子里】

一句话：**推不出去，绝不能变成"诊断结果丢了"。**

这条约束落到代码上是四个具体决定，每一个都不是随手写的：

  1. **对外只返回 NotifyResult，绝不抛异常**。
     HTTP 超时、连接被拒、对端返回垃圾、证书过期、DNS 挂了 —— 全都变成
     `NotifyResult(ok=False, error="...")`。调用方拿到的是一个结构体，
     不可能因为通知挂了而走进 except 分支、把已经诊断出来的结论丢掉。
  2. **本模块不写审计、不 import app.main**。
     `app.main` 反过来要 import 本模块（它才是调用方），这里再回头 import
     就是循环导入。更重要的是职责：审计是"主链路的痕迹"，通知是"出口"，
     出口不该有写主链路状态的权力。调用方拿到 NotifyResult 后自己写审计 ——
     **通知成功与否必须留下痕迹**，否则"以为在通知、其实一直失败"会静默很久。
  3. **一个渠道失败不影响其它渠道**。每个渠道各自 try、各自记结果。
     钉钉挂了不应该让自建 webhook 也不发。
  4. **不发比发错好**。没配 URL 的渠道直接不构造（见 dispatcher），
     一个渠道都没配就返回 None —— 此时关于通知的全部代码都不会被执行。

【为什么超时给 8 秒，而不是像 langfuse 导出那样 5 秒】

导出是后台线程，慢一点没人等它。通知是在诊断结束的收尾阶段同步调的，
对端（钉钉/飞书的服务器）偶尔会慢到 2~3 秒，8 秒是一个"正常情况下永远
用不到、真出问题时不至于把请求拖到用户以为卡死"的值。别调太小，
超时重试三次的总耗时 = 3×timeout + 退避，20 秒已经是收尾阶段能忍受的上限。

【为什么把 HTTP 发送和"业务错误判定"拆开】

钉钉和飞书都有个反直觉的行为：**HTTP 200 也可能是业务失败**。
钉钉把"机器人被移出群"这种错误放在 `{"errcode": 310000}` 里返回 200，
飞书放在 `{"code": 19021}` 里。只看 status_code 的话，这类失败会一律
记成"通知成功" —— 而它恰恰是最需要被发现的一类：配置已经坏了。

所以基类固定三次判定：① 能不能发出去（异常）② status 是否 2xx
③ 子类解析业务错误码。子类只需要实现第 ③ 步（`_business_error`），
前两步是共享的，不可能漏掉。

【为什么有个 mask()，而且默认不自动生效】

公网暴露之后，诊断片段里可能夹着被诊断服务的凭据（`curl -H "Authorization:
Bearer xxx"`、`.env` 里的 `password=`、日志里漏出来的 `sk-` 开头 key）。
通知是**出站**的：一旦发出去就落在第三方 IM 的服务器与聊天记录里，
比本地日志难收回得多。

它默认**自动生效**（`NOTIFY_MASK` 默认 1）。理由：通知是**出站**的，发出去就落在
第三方 IM 的服务器与聊天记录里，比本地日志难收回得多 ——
而出站内容里出现凭据，是"一旦发生就没法补救"的那类错误。
所以默认走安全的一边，需要原样发出时显式 `NOTIFY_MASK=0`
（**你确认过要发的内容里没有凭据**再关）。`mask()` 的规则刻意保守：
主机名、服务名、路径、退出码这些运维真正要用的信息**必须原样保留**，
只有像凭据的片段才被折叠（见 `mask()` 的正反例用例）。
"""

import abc
import base64
import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

log = logging.getLogger("agentdesk.notify")

# 出站请求的来源标记。对端（自建 webhook 的另一头）靠它区分
# "AgentDesk 推的"和别的系统推的。
SOURCE = "agentdesk"

# 响应体截断长度：错误信息里只留前 200 字。
# 【为什么必须截断】对端出错时可能返回一整页 HTML（nginx 502 页面几 KB），
# 原样塞进 NotifyResult.error 会被原样写进审计日志与 HTTP 响应。
BODY_LIMIT = 200


# ============================================================
# 结果类型
# ============================================================
@dataclass
class NotifyResult:
    """一次通知投递的结果。**这是本模块唯一的对外输出形态。**

    字段说明（顺序与名字是对外接口，别改）：

        channel     渠道名，与 Notifier.name 一致，用于在审计里追责"是谁没发出去"
        ok          这次投递是否成功（HTTP 2xx **且** 业务错误码为 0）
        status      HTTP 状态码；连请求都没发出去（超时/连接失败）时为 None
        error       失败原因，人类可读；成功时为空串
        attempts    实际尝试次数（含第一次）。1 表示一次就成，与 max_attempts 对比看退避有没有白跑
        elapsed_ms  这一渠道的总耗时（含重试与退避等待）
        skipped     是否**主动**没发（渠道未配置，或被 dispatcher 去重拦下）。
                    skipped=True 时 ok 一定是 False —— 把"没发"和"发了但失败"
                    分开记，是因为两者的处置完全不同：前者查配置，后者查网络。
    """

    channel: str
    ok: bool
    status: int | None = None
    error: str = ""
    attempts: int = 1
    elapsed_ms: int = 0
    skipped: bool = False

    def to_dict(self) -> dict:
        """给审计日志/接口响应用的扁平结构（dataclass 直接 json 化也行，
        但显式一份能让字段顺序稳定，日志好读）。"""
        return {
            "channel": self.channel,
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "skipped": self.skipped,
        }


# ============================================================
# 脱敏
# ============================================================
# 设计原则（对着 docs/deployment.md「公网暴露意味着什么」那一节写的）：
# 通知要发到第三方 IM 的服务器上，而诊断片段里经常夹着**别人的**凭据。
# 所以出站前必须能过一道筛子。但筛子有两个方向都会出错：
#   · 掩得太少 —— 凭据泄露到聊天记录里，撤不回来；
#   · 掩得太多 —— 把"服务 nginx 退出码 1、路径 /var/log/nginx/error.log"
#     也抹掉，那收通知的人等于收到一条"有件事发生了"，通知就失去意义。
# 这里的取舍是：**只掩"值的形状"明确是凭据的东西，其余一律原样保留。**

_REDACTED = "<redacted>"

# ① 名字里带 key/secret/token/password 的赋值。
#    两类引号分开写：引号内的值允许有空格，非引号的值在空白/引号处停。
#
#    ★ 拆成两条、而不是一条正则里塞 `[:=]`，是为了处理一个实测踩到的误伤：
#          passwd: authentication token manipulation error
#      这是 /etc/shadow 出问题时的标准报错，必须原样发出去。让 `passwd`
#      接受 `:` 分隔符，这一整条运维信息会被吃掉一半（通知就废了）。
#      而 `password=xxx` / `--password=xxx`（真实泄露的形态）一条都不少。
#      代价：`passwd: <明文密码>` 这种罕见写法会漏掩一次。
#      用"每天都要看的报错被吃掉"换"罕见形态漏一次"，这个取舍是划算的。
_KEY_VALUE_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|auth[_-]?token|password|passwd|pwd|token|secret)\b"
    r"(\s*=\s*)"
    r"(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s,;\"']+))"
)
# `:` 分隔的那条**不含 passwd**（它最多是个错误提示里的词，不是赋值）。
_KEY_VALUE_COLON_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|auth[_-]?token|password|pwd|token|secret)\b"
    r"(\s*:\s*)"
    r"(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s,;\"']+))"
)
_KEY_VALUE_SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{6,}")

def _looks_like_credential(value: str) -> bool:
    """`token <值>` 里的值是不是**真的像个凭据**。

    【为什么需要这个判断 —— 实测的第三个误伤】
        passwd: authentication token manipulation error
    这是 /etc/shadow 出问题时的标准报错。规则 ② 认 scheme 关键词 + 后面跟的
    长串，于是把 `manipulation` 当成 token 值抹掉了 —— 通知里就成了一
    句读不通的话。

    凭据的形状特征：**几乎必然带数字、或大写、或 base64 的 +/= **。
    纯小写字母的长单词（manipulation / authentication / administrator）
    不是凭据。所以这里要求至少含一个非小写字母的字符。

    这是"宁可漏掩"方向的取舍：真有个纯小写字母组成的 token（现实中几乎不存在，
    因为随机串里全小写的概率极低），我们漏掩它，
    换来的是报错信息不被吃掉。
    """
    return any((not c.islower()) for c in value)


# ② 认证头：Bearer / Basic / Token <值>，以及 X-API-Key: <值>
#    为什么单独写一条：`Authorization: Bearer eyJ...` 里的 `Authorization`
#    不在 ① 的关键字列表里（列表里是 token/secret 这些），不写这条就漏。
#    值那一侧加了凭据形状判断（见上），否则 `token manipulation` 会被误伤。
_AUTH_HEADER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._\-+/=]{8,})")
_HEADER_KEY_RE = re.compile(r"(?i)(\bx-api-key\b)(\s*[:=]\s*)([^\s,;\"']+)")


def _mask_auth_header(m) -> str:
    """② 的回调：值不像凭据就原样留着（见 `_looks_like_credential`）。"""
    if not _looks_like_credential(m.group(2)):
        return m.group(0)
    return f"{m.group(1)} {_REDACTED}"



# ③ 长 base64/十六进制串。**这条最危险**：它没有"凭据"这个名字做锚点，
#    只能靠形状。所以配了 `_looks_like_opaque_secret` 三道防线
#    （见那里的注释），宁可漏掩也不能把路径/URL/SHA 抹掉。
_LONG_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")

# 唯一一处"看起来像长十六进制串、但必须保留"的地方：**带算法前缀的摘要**。
#   sha256:9f8e7d6c...（docker inspect / cosign / 校验和输出）
# 它和"裸的十六进制密钥"形状完全一样，只能靠前面的 `算法名:` 区分。
# 判定：合法前缀要求 `image:`/`digest:`/`shaXXX:` 后面直接跟 24 位以上 hex，
# 且**前缀必须是独立词**（前面是行首或非字母数字），否则 `artifacts:` 这种
# 尾巴里含 `facts:` 的会被误判。
_DIGEST_PREFIX_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])(?:sha256|sha1|sha512|md5|image|digest|checksum)\s*[:=]\s*)"
    r"[0-9a-fA-F]{24,}"
)

# 明显不该被 ③ 误伤的"运维可读信息"的形状：
#   IP、文件路径、URL、版本号、以 / 开头的路径
_SAFE_HINT_RE = re.compile(
    r"(?i)^(?:\d{1,3}\.){3}\d{1,3}$"                       # 192.168.1.10
    r"|^[/~.]"                                             # /var/log/nginx/access.log
    r"|^[a-z][a-z0-9+.\-]*://"                             # https://host/path
    r"|^\d+\.\d+[\d.]*"                                    # 1.24.0
)
# 带文件扩展名的，通常是文件名/包名（nginx.tar.gz、python3.12），不掩
_SAFE_EXT_RE = re.compile(
    r"(?i)\.(?:log|json|jsonl|txt|md|yaml|yml|conf|cfg|ini|toml|sh|py|js|ts|go|rb|"
    r"jar|war|zip|gz|tgz|xz|bz2|tar|deb|rpm|pem|crt|key|db|sqlite|sock|pid|lock|html)$"
)


def _looks_like_opaque_secret(token: str) -> bool:
    """判断一个长串"值不值得掩"。**注意：调用方已经剥掉了摘要前缀**。

    ③ 这条规则的目标是 `9f8e7d6c5b4a39281706f5e4d3c2b1a0` 这种裸的凭据/密钥，
    但它用的正则同样能匹配上一些**运维必须看到**的东西，所以逐个排除：

        · 加密套件名   ECDHE-RSA-AES256-GCM-SHA384  → 带 '-'，根本不进这条正则
        · 内核版本串   5.15.0-91-generic           → 带 '.'，被切成短片段
        · 域名片段     agent.simosheng.fun         → 带 '.'，不进
        · 镜像摘要     sha256:9f8e…（40/64 位）    → 在 mask() 里被前缀规则拦走
        · 裸十六进制   a3f1c9d2e8b74c60…           → **会**匹配，也确实该掩
                                                      （裸密钥最典型就是这个形状）

    剩下的判定：
        1. 长度 ≥ 24
        2. 一眼看着像路径 / URL / IP / 版本号的 → 放行
        3. 带文件扩展名的（文件名/包名）→ 放行
        4. 其余长串 → 掩。**"其余"这个口子开得比较小是有意的：
           base64/十六进制长串在运维输出里，是密钥的概率远大于是别的。**
    """
    if _SAFE_HINT_RE.match(token) or _SAFE_EXT_RE.search(token):
        return False
    if len(token) < 24:
        return False
    return True


def mask(text: str) -> str:
    """出站前给命令/日志片段脱敏。**纯函数，不做任何 I/O。**

    规则（`NOTIFY_MASK=1` 时由调用方在发送前调用，见 `mask_enabled`）：

        1. `sk-xxxxxx` 形状的 key                        → sk-<redacted>
        2. `Bearer xxx` / `Basic xxx` / `Token xxx`      → 保留 scheme，值换掉
        3. `X-API-Key: xxx`                              → X-API-Key: <redacted>
        4. `password=` / `passwd=` / `token=` / `secret=`
           （含 api_key / access_key / auth_token 等变体，值带不带引号都行；
             也认 `password: xxx`，但 **`passwd:` 不当赋值处理** ——
             见 _KEY_VALUE_ASSIGN_RE 上方的注释）
                                                        → 保留键名，值换掉
        5. 长度 ≥ 24 的裸 base64/十六进制串              → <redacted>
           （但路径 / URL / IP / 版本号 / 带扩展名的文件名，以及
             `sha256:xxxx` 这类带算法前缀的摘要，一律保留）

    **保留可读信息是硬要求**：主机名、服务名、路径、退出码统统原样留着。
    把整段文本清空或全替换掉，通知就变成了"有件事发生了"，
    收通知的人还得回服务器自己看 —— 那还不如不发。

    空串 / None 输入安全返回空串（调用方常常直接拿可选字段来喂它）。
    """
    if not text:
        return ""
    out = str(text)

    # 顺序有讲究，两条都要注意：
    #   a) 先处理"带名字的"（锚点可靠），再处理"靠形状的"。反过来先跑 ⑤
    #      的话，`password=9f8e...` 的值会先被换成 <redacted> —— 看着结果
    #      一样，但 ④ 的"键名保留"就永远验证不到了。
    #   b) 摘要前缀（sha256:xxxx）必须在 ⑤ **之前**暂存、跑完再还原 ——
    #      否则那串十六进制会被当成裸密钥抹掉，而它是运维要看的构建号。
    #      用占位符而不是"让正则跳过"，是因为 sub 的回调没法告诉外层"别动这段"。
    digests = []

    def _stash_digest(m):
        digests.append(m.group(0))
        return f"\x00DIGEST{len(digests) - 1}\x00"

    out = _DIGEST_PREFIX_RE.sub(_stash_digest, out)
    out = _KEY_VALUE_ASSIGN_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _KEY_VALUE_COLON_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _KEY_VALUE_SK_RE.sub("sk-" + _REDACTED, out)
    out = _AUTH_HEADER_RE.sub(_mask_auth_header, out)
    out = _HEADER_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _LONG_OPAQUE_RE.sub(
        lambda m: _REDACTED if _looks_like_opaque_secret(m.group(0)) else m.group(0),
        out)
    for i, original in enumerate(digests):
        out = out.replace(f"\x00DIGEST{i}\x00", original)
    return out


def mask_enabled(env: dict | None = None) -> bool:
    """读 `NOTIFY_MASK` 开关。**默认 1（开）**。

    【为什么默认开】mask() 会改写要发出去的正文，看起来"默认关更不打扰用户"。
    但两个方向的代价不对称：

        默认开 → 通知里命令少了一段（人看得出来有 `<redacted>`，还能去审批单看原文）
        默认关 → 凭据进了第三方 IM 的聊天记录，**收不回来**

    前者是可读性问题，后者是安全事故。所以默认开；
    确实需要原样发送时显式设 `NOTIFY_MASK=0`。
    """
    e = os.environ if env is None else env
    return str(e.get("NOTIFY_MASK", "1")).strip() == "1"


# ============================================================
# 抽象渠道
# ============================================================
class Notifier(abc.ABC):
    """一个出站渠道（webhook / 钉钉 / 飞书 / 企业微信……）。

    子类只需要实现两件事：
        · `name`            —— 渠道名，会进 NotifyResult.channel 与审计
        · `_business_error` —— 从响应体里判"HTTP 200 但业务失败"

    `send()` 本类不实现：它的**签名是对外契约**（title/text/payload），
    而"要不要加签、body 长什么样"每个渠道都不同。子类实现 send() 时
    调 `self._post(...)` 即可拿到已经过完 ①②③ 三次判定的 NotifyResult。
    """

    name: str = "notifier"

    def __init__(self, url: str, timeout: int = 8, name: str | None = None):
        self.url = (url or "").strip()
        self.timeout = int(timeout)
        if name:
            self.name = name

    # ---------- 子类实现 ----------
    @abc.abstractmethod
    def send(self, title: str, text: str, *,
             payload: dict | None = None) -> NotifyResult:
        """投递一次通知。**不允许抛异常**（子类内部要自己兜住）。"""
        raise NotImplementedError

    def _business_error(self, status: int, data: dict, raw: str) -> str:
        """返回业务错误描述；返回空串表示业务上也成功。

        默认实现："HTTP 2xx 就算成功"。**但钉钉/飞书不能这么想当然**：
        它们恰恰是 HTTP 200 + 业务错误码。谁不覆盖这个方法，
        谁就继承了"200 即成功"这个错误假设。
        """
        return ""

    # ---------- 共享：HTTP 发送 + 三次判定 ----------
    def _post(self, url: str, body: dict) -> NotifyResult:
        """发一个 JSON POST，把"发不出去 / 非 2xx / 业务错误"都变成 NotifyResult。

        之所以把这段放在基类：**"HTTP 200 也是失败"这件事必须有唯一落点**。
        让每个子类各写一遍，迟早有一个渠道忘了判 `errcode`。
        """
        started = time.time()

        try:
            resp = httpx.post(
                url,
                json=body,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:
            # 超时、连接被拒、DNS 失败、证书错误、代理抽风……全在这里。
            # 【为什么 except Exception 而不是只要 httpx.HTTPError】
            # 打桩（测试）与中间件（企业代理、monkeypatch 过的 socket）
            # 都可能抛非 httpx 的异常。通知是旁路，任何异常都不该逃出去 ——
            # 逃出去就意味着"诊断结果丢了"。
            return NotifyResult(
                channel=self.name, ok=False, status=None,
                error=self._err_text(f"{type(e).__name__}: {e}"),
                elapsed_ms=int((time.time() - started) * 1000))

        raw = self._resp_text(resp)
        status = int(getattr(resp, "status_code", 0) or 0)

        if status < 200 or status >= 300:
            return NotifyResult(
                channel=self.name, ok=False, status=status,
                error=self._err_text(f"HTTP {status}: {raw}"),
                elapsed_ms=int((time.time() - started) * 1000))

        data = self._json_or_empty(resp)
        biz = self._business_error(status, data, raw)
        if biz:
            return NotifyResult(
                channel=self.name, ok=False, status=status,
                error=self._err_text(f"HTTP {status} 但业务失败: {biz}"),
                elapsed_ms=int((time.time() - started) * 1000))

        return NotifyResult(channel=self.name, ok=True, status=status,
                            elapsed_ms=int((time.time() - started) * 1000))

    # ---------- 小工具 ----------
    @staticmethod
    def _resp_text(resp) -> str:
        """读响应文本。取不到就返回空串 —— 读 body 本身也可能抛
        （编码错、连接已断、假对象没实现 text）。"""
        try:
            return str(resp.text or "")
        except Exception:
            return ""

    @staticmethod
    def _json_or_empty(resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _err_text(text: str, limit: int = BODY_LIMIT) -> str:
        return text if len(text) <= limit else text[:limit] + "…"


# ============================================================
# 共享：给 webhook 用的出站 body
# ============================================================
def build_body(title: str, text: str, payload: dict | None = None) -> dict:
    """自建 webhook 的固定契约（对面按这个结构解析，别随手改字段名）。

        {"title": ..., "text": ..., "source": "agentdesk",
         "ts": "<ISO8601 UTC>", "extra": {...}}

    多带 title 与 source 是有意的：对端那条 webhook 可能同时接了好几个
    系统的推送，`source` 让它在群里能说清"这条是 AgentDesk 推的"。
    """
    return {
        "title": title,
        "text": text,
        "source": SOURCE,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "extra": payload or {},
    }


def sign_hmac_sha256_b64(secret: str, message: str, *, urlencode: bool) -> str:
    """HMAC-SHA256 → base64。**纯函数**，时间戳由调用方给。

    【为什么必须纯函数、为什么时间戳不进这里】
    `time.time()` 藏在签名函数里，测试就只能断言"两次调用结果不同"或
    "长度对得上"—— 等于没测。时间戳由调用方传入后，固定密钥 + 固定时间戳
    就能断言**那一串具体字符**，算法改错立刻红（见 tests/test_notify.py）。
    这是把"不确定性"从被测量里赶出去的标准做法。

    urlencode=True 是钉钉的额外要求：它的 sign 要 URL 编码后拼进 query，
    而 base64 里的 `+` `/` `=` 不编码就会被服务端解析成别的东西
    （`+` 在 query 里等于空格 —— 这是最经典的加签失败原因）。
    """
    digest = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    return quote(sign, safe="") if urlencode else sign


def sign_key_hmac_sha256_b64(key_material: str) -> str:
    """HMAC-SHA256(key=key_material, msg=b"") → base64。**纯函数**。

    【为什么不能拿 sign_hmac_sha256_b64(secret, "") 凑】
    参数位置根本不同，凑不出来：
        钉钉/通用:  HMAC(key=secret,      msg=f"{ts}\\n{secret}")
        飞书:       HMAC(key=f"{ts}\\n{secret}", msg=b"")
    飞书把"待签材料"当**密钥**、消息是空串 —— 这是个反直觉的设计，
    也正是网上大量教程把两家抄混的地方。单独给一个名字明确的函数，
    比在调用处拼参数安全得多（拼错了没有任何报错，只有 19021 签名失败）。
    """
    digest = hmac.new(
        key_material.encode("utf-8"), b"", hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def append_query(url: str, params: dict) -> str:
    """把参数拼到 URL 上（已有 query 时用 & 续）。

    【为什么要判已有 query】钉钉机器人的 webhook 本来就带
    `?access_token=xxx`，直接 `url + "&timestamp=..."` 碰上不带 token 的
    自定义地址（或飞书那种）就会拼出 `...path&timestamp=..` 这种畸形 URL，
    对端直接 404 —— 而这种错在日志里看着像"网络问题"，很难查。
    """
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    if not qs:
        return url
    return f"{url}&{qs}" if "?" in url else f"{url}?{qs}"


# 供 dispatcher 复用的"配置解析"工具
def env_int(env: dict, key: str, default: int, log_warn=None) -> int:
    """读一个整数配置，坏了就用默认值 + 警告，**绝不抛**。

    【为什么不抛】build_dispatcher 是在服务启动时调的。为了一个
    `NOTIFY_TIMEOUT=8s`（多打了个 s）让整个服务起不来，是把旁路的错误
    升级成了主链路的故障 —— 这个方向搞反了。
    （对比 security.py 的做法：那边 `AUTH_ENABLED=1` 但没 token 必须硬失败，
      因为那是「以为有保护其实裸奔」。而通知配错只会导致"少一个通知渠道"，
      后果量级完全不同 —— 所以处置也不同，这不是双标。）
    """
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        (log_warn or log.warning)(
            "%s=%r 不是整数，已回退为默认值 %s", key, raw, default)
        return default
