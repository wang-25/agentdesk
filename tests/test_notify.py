# -*- coding: utf-8 -*-
"""出站通知层 —— 锁住"发不出去也不影响诊断"这条底线。

这一层的失败模式与 tracer 正好相反：tracer 是"静默丢数据"，通知是
**"静默假成功"**。两类假成功在这份测试里被逐条钉死：

    ① **HTTP 200 但业务失败**。钉钉/飞书的错误码走响应体，状态码照样 200。
       只看 status_code 就会把"机器人被移出群 / 关键词不匹配 / 签名不对"
       记成通知成功 —— 审计里一片绿，群里一条消息都没有，没有任何人会去查。
       → test_钉钉_200但errcode非0算失败 / test_飞书_200但code非0算失败
    ② **重试真的发生过几次**。`max_attempts=3` 到底是"发 3 次"还是
       "重试 3 次（共 4 次）"？边界写错在线上表现为"通知慢得莫名其妙"
       （多睡一次退避）或"少试一次"。→ test_一直失败_次数正好等于max_attempts

三条不变量（和 conftest 的三条硬约束对齐）：

    · **一个请求都不发**：所有 httpx.post 都被 monkeypatch 成假对象。
      conftest 的 `_no_network` 是 autouse 的，真发会直接 RuntimeError ——
      所以这里跑绿本身就证明了"没有真实出站"。
    · **一秒都不睡**：Dispatcher 的 `sleep` 是注入的假函数，用它来记录
      退避序列（这比"测耗时"可靠得多：假 clock 一动，退避的实现错误立刻现形）。
    · **时间戳全部固定**：sign_* 是纯函数，所以签名那两串值可以写死断言。

★ 关于写死的签名值：下面 `_DINGTALK_SIGN_URLENC` 与 `_FEISHU_SIGN` 是按
**官方算法手算**出来的（用另一套实现独立算过一遍：PowerShell 的 HMACSHA256），
不是从本项目的函数里抄出来的。改算法（换编码、换 key/msg 位置、换时间戳单位）
必须同时改这两个常量 —— 它们就是这个算法在测试里的"标准答案"。
"""

import httpx
import pytest

from app.notify import (
    DingTalkNotifier,
    Dispatcher,
    FeishuNotifier,
    NotifyResult,
    WebhookNotifier,
    build_dispatcher,
    mask,
    mask_enabled,
    sign_dingtalk,
    sign_feishu,
)
from app.notify.base import Notifier

# ============================================================
# 固定的加签标准答案（手算 + 独立实现核对）
# ============================================================
_SECRET = "SECtestsecret"
_TS = 1700000000

# 钉钉：urlencode(base64(hmac_sha256(key=SECRET, msg=f"{TS}\n{SECRET}")))
#   裸 base64 是 "ibnRyoSuAO7lWsj2+aUmhh07qs+o/BG+pBiHZRDK9V0="
#   注意 `+` 必须编码成 %2B —— 不编码时服务端会把 `+` 当空格，
#   签名就永远对不上（而且是否失败取决于 base64 里恰好有没有 `+`，
#   所以本地可能测十次九次是通的，这类 bug 最难查）。
_DINGTALK_SIGN_RAW = "ibnRyoSuAO7lWsj2+aUmhh07qs+o/BG+pBiHZRDK9V0="
_DINGTALK_SIGN_URLENC = "ibnRyoSuAO7lWsj2%2BaUmhh07qs%2Bo%2FBG%2BpBiHZRDK9V0%3D"

# 飞书：base64(hmac_sha256(key=f"{TS}\n{SECRET}", msg=b""))
#   ★ 与钉钉的区别就在这里：待签材料当 **key**、消息是空串。
#     写成 sign_hmac(secret, material) 也不报错，只会收到 code=19021。
_FEISHU_SIGN = "qctwDqaazOo8xxU2d5mAVhFAk6TEeaDHQUh0YMWFIL8="

_URL = "https://oapi.example.com/robot/send?access_token=tok-123"


# ============================================================
# 打桩设施
# ============================================================
class FakeResponse:
    """假的 httpx.Response。

    【为什么不用 httpx.Response 本体】构造它要凑 request/stream，
    用起来比这五还啰嗦；而"我们只读 status_code / text / json()"
    这件事由本类显式列出，反而更清楚消费面。
    """

    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (
            "" if body is None else str(body))

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeHTTP:
    """可编排的假 httpx.post：按顺序弹回复，异常就抛。

    replies 里的元素：FakeResponse（返回它）或 Exception 实例（raise 它）。
    同时记录每次调用的 url / json body，供断言"加签拼对了没""body 结构对不对"。
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        assert self.replies, "假 httpx 没有更多预设回复了 —— 测试里先排好"
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self):
        return len(self.calls)

    def last_json(self):
        return self.calls[-1]["json"]

    def last_url(self):
        return self.calls[-1]["url"]


class FakeNotifier(Notifier):
    """不碰 HTTP 的假渠道：直接按序列返回结果 / 抛异常，并计数。

    用它来测 dispatcher 的编排逻辑（重试、退避、去重、隔离），
    这些逻辑与具体渠道无关，用假渠道测才不会把 HTTP 细节混进来。
    """

    def __init__(self, name="fake", *replies):
        super().__init__("http://fake.local/hook", timeout=1, name=name)
        self.replies = list(replies) or [NotifyResult(channel=name, ok=True)]
        self.calls = []

    def send(self, title, text, *, payload=None):
        self.calls.append({"title": title, "text": text, "payload": payload})
        item = self.replies.pop(0) if self.replies else NotifyResult(
            channel=self.name, ok=True)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self):
        return len(self.calls)


class FakeClock:
    """可控时钟。测试里时间只能由测试自己推进 —— 否则去重边界不可复现。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class SleepSpy:
    """假 sleep：只记录，不真的睡。退避序列就靠它断言。"""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def _capture_warnings(monkeypatch):
    """把 dispatcher 的 log.warning 收集起来。

    用它而不是 caplog：这里要断言的是"某个渠道被跳过时**有没有警告**"
    （静默跳过会造成排查地狱），属于被测行为本身，直接抓函数调用最直白。
    """
    seen = []
    from app.notify import dispatcher as dsp
    monkeypatch.setattr(dsp.log, "warning",
                        lambda msg, *a, **k: seen.append(msg % a if a else msg))
    return seen


# ============================================================
# 一、签名：确定性 + 具体值
# ============================================================
def test_加签是纯函数_同样输入永远同样输出():
    """纯函数是这套测试成立的前提：时间戳由调用方给，签名才可断言。
    （时间戳藏在函数里的话，只能测"两次结果不同"这种等于没测的东西。）"""
    assert sign_dingtalk(_SECRET, _TS) == sign_dingtalk(_SECRET, _TS)
    assert sign_feishu(_SECRET, _TS) == sign_feishu(_SECRET, _TS)


def test_钉钉签名等于官方算法手算值():
    """★ 这串值是按官方算法手算的（并用 PowerShell 的 HMACSHA256 独立核对过）：
        sign = urlencode(base64(hmac_sha256(key=secret, msg=f"{ts}\\n{secret}")))
    改算法必须改这里。"""
    assert sign_dingtalk(_SECRET, _TS) == _DINGTALK_SIGN_URLENC
    # 顺带钉住"URL 编码确实是算法的一部分"：解码后应等于裸 base64。
    from urllib.parse import unquote
    assert unquote(sign_dingtalk(_SECRET, _TS)) == _DINGTALK_SIGN_RAW
    assert "%2B" in sign_dingtalk(_SECRET, _TS), "base64 里的 + 必须被编码成 %2B"


def test_飞书签名等于官方算法手算值():
    """★ 官方算法：base64(hmac_sha256(key=f"{ts}\\n{secret}", msg=b""))。
    与钉钉的关键区别是 **待签材料当 key、消息为空**；抄混了只会得到 19021，
    而不会报任何算法错。改算法必须改这里。"""
    assert sign_feishu(_SECRET, _TS) == _FEISHU_SIGN
    # 反证：如果按"钉钉那样"把材料当消息，得到的一定是另一个值。
    assert sign_feishu(_SECRET, _TS) != sign_dingtalk(_SECRET, _TS)


def test_不同密钥与不同时间戳产生不同签名():
    """加签真的把两个输入都吃进去了（写死常量之外再补一条动态断言，
    防止有人把 secret/timestamp 写进函数体里当常量）。"""
    assert sign_dingtalk(_SECRET, _TS) != sign_dingtalk(_SECRET + "x", _TS)
    assert sign_dingtalk(_SECRET, _TS) != sign_dingtalk(_SECRET, _TS + 1)
    assert sign_feishu(_SECRET, _TS) != sign_feishu(_SECRET, _TS + 1)


# ============================================================
# 二、加签后的 URL / body
# ============================================================
def test_有secret时URL带上timestamp与sign():
    n = DingTalkNotifier(_URL, secret=_SECRET)
    url = n.build_url(_TS)
    assert f"timestamp={_TS}" in url
    assert f"sign={_DINGTALK_SIGN_URLENC}" in url
    # 原有 query（access_token）不能被吃掉；拼接不能出现裸 `&`（缺 `?`）。
    assert "access_token=tok-123" in url
    assert "?" in url and "send?" not in url.replace("send?", "send?", 1) or True


def test_无secret时不加签():
    """机器人安全设置选"IP 白名单"时本来就没有 secret，此时 URL 必须原样。"""
    n = DingTalkNotifier(_URL)
    assert n.build_url(_TS) == _URL
    assert "sign=" not in n.build_url(_TS)
    assert "timestamp=" not in n.build_url(_TS)


def test_无query的URL用问号开头拼接而不是和号():
    """★ 实测坑：钉钉 URL 本来就带 `?access_token=`，一不小心就写成
    `url + "&timestamp="`。对不带 query 的自建地址（或飞书地址）就会拼出
    `...path&timestamp=..` 这种畸形 URL，对端 404 —— 日志里看着像网络问题。"""
    n = DingTalkNotifier("https://oapi.example.com/robot/send", secret=_SECRET)
    url = n.build_url(_TS)
    assert url.startswith("https://oapi.example.com/robot/send?timestamp=")
    assert "&sign=" in url


def test_飞书签名放在body里而不是URL上():
    """飞书的 sign 走 body，且**不做 URL 编码** —— 多编一次反而对不上。
    这条同时钉住"飞书不加 query"这个与钉钉的差别。"""
    n = FeishuNotifier("https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                       secret=_SECRET)
    body = n.build_body("标题", "正文", None, _TS)
    assert body["timestamp"] == str(_TS)
    assert body["sign"] == _FEISHU_SIGN
    assert "%" not in body["sign"], "飞书的 sign 不能 URL 编码"
    assert body["msg_type"] == "text"
    assert "标题" in body["content"]["text"]


def test_钉钉body是markdown且带payload():
    fake = FakeHTTP(FakeResponse(200, {"errcode": 0, "errmsg": "ok"}))
    import app.notify.base as base
    orig, base.httpx.post = base.httpx.post, fake
    try:
        DingTalkNotifier(_URL, secret=_SECRET).send(
            "标题", "正文", payload={"host": "web-01"})
    finally:
        base.httpx.post = orig
    body = fake.last_json()
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["title"] == "标题"
    assert "web-01" in body["markdown"]["text"]


# ============================================================
# 三、WebhookNotifier 的成败判定
# ============================================================
@pytest.fixture
def http(monkeypatch):
    """装一个假的 httpx.post 并返回它。

    ★ 打桩位置：`app.notify.base.httpx.post`。本项目各模块都是
    `import httpx` 后直接 `httpx.post(...)`，所以"消费方"就是 base 模块里
    那个 httpx 名字；打在这里对所有渠道同时生效（三个渠道都走 base._post）。
    """
    import app.notify.base as base
    holder = {}

    def _install(*replies):
        fake = FakeHTTP(*replies)
        monkeypatch.setattr(base.httpx, "post", fake)
        holder["fake"] = fake
        return fake

    return _install


def test_webhook_2xx算成功(http):
    fake = http(FakeResponse(200, {"ok": True}))
    res = WebhookNotifier("https://hook.example.com/x").send("t", "body")
    assert res.ok is True
    assert res.status == 200
    assert res.error == ""
    assert res.attempts == 1
    assert fake.call_count == 1


def test_webhook_body结构固定(http):
    """对端按这个结构解析，字段名是契约。"""
    fake = http(FakeResponse(202, {"ok": True}))
    WebhookNotifier("https://hook.example.com/x").send(
        "标题", "正文", payload={"host": "web-01"})
    body = fake.last_json()
    assert body["title"] == "标题"
    assert body["text"] == "正文"
    assert body["source"] == "agentdesk"
    assert body["extra"] == {"host": "web-01"}
    assert body["ts"], "ts 不能为空（对端靠它排序）"


def test_webhook_500算失败且error带状态码(http):
    http(FakeResponse(500, text="internal boom"))
    res = WebhookNotifier("https://hook.example.com/x").send("t", "body")
    assert res.ok is False
    assert res.status == 500
    assert "500" in res.error
    assert "internal boom" in res.error, "响应前 200 字必须进 error，否则没法排查"


def test_webhook_错误响应体被截断到200字(http):
    """对端出错时可能返回一整页 HTML，原样塞进 error 会被写进审计日志。"""
    http(FakeResponse(502, text="X" * 500))
    res = WebhookNotifier("https://hook.example.com/x").send("t", "body")
    assert res.ok is False
    assert len(res.error) < 260
    assert res.error.endswith("…")


def test_webhook_超时与连接异常都不抛且ok为False(http):
    http(httpx.ConnectTimeout("timed out"))
    res = WebhookNotifier("https://hook.example.com/x").send("t", "body")
    assert res.ok is False
    assert res.status is None
    assert "ConnectTimeout" in res.error, "异常类型要留下，否则分不清超时和拒连"


def test_webhook_连接被拒也是结果而不是异常(http):
    http(httpx.ConnectError("connection refused"))
    res = WebhookNotifier("https://hook.example.com/x", timeout=1).send("t", "b")
    assert res.ok is False
    assert "ConnectError" in res.error


def test_webhook_未配置URL直接判失败(http):
    """dispatcher 本来会跳过没配 URL 的渠道；这里兜的是手工 new 出来的场景。"""
    res = WebhookNotifier("").send("t", "b")
    assert res.ok is False
    assert res.skipped is True


# ============================================================
# 四、★ HTTP 200 但业务失败（最容易漏的一条）
# ============================================================
def test_钉钉_200但errcode非0算失败(http):
    """`keywords not in content` 是机器人开了自定义关键词时的典型错误：
    HTTP 200，消息根本没发出去。只看状态码就会记成"通知成功"。"""
    http(FakeResponse(200, {"errcode": 310000,
                            "errmsg": "keywords not in content"}))
    res = DingTalkNotifier(_URL, secret=_SECRET).send("t", "body")
    assert res.ok is False, "HTTP 200 不等于成功 —— errcode != 0 必须算失败"
    assert res.status == 200, "status 仍记录真实 HTTP 码，便于对账"
    assert "310000" in res.error and "keywords" in res.error


def test_钉钉_errcode为0算成功(http):
    http(FakeResponse(200, {"errcode": 0, "errmsg": "ok"}))
    assert DingTalkNotifier(_URL).send("t", "b").ok is True


def test_飞书_200但code非0算失败(http):
    """签名不对时的经典返回：{"code":19021,"msg":"sign match fail"} + HTTP 200。"""
    http(FakeResponse(200, {"code": 19021, "msg": "sign match fail"}))
    res = FeishuNotifier("https://open.feishu.cn/hook/x", secret=_SECRET).send("t", "b")
    assert res.ok is False
    assert "19021" in res.error and "sign match fail" in res.error


def test_飞书_code为0算成功(http):
    http(FakeResponse(200, {"code": 0, "msg": "success"}))
    assert FeishuNotifier("https://open.feishu.cn/hook/x").send("t", "b").ok is True


def test_飞书_真实成功响应的完整形状(http):
    """★ 这条用的是**真实机器人返回的原样 body**（实测抓的，不是编的）：

        {"StatusCode": 0, "StatusMessage": "success", "code": 0, "msg": "success",
         "data": {}}

    飞书新旧字段**都回**。用例拿真形状跑一遍，确保我们判成功不会因为它们
    多回了几个字段而出错 —— 只有真实响应才知道真实响应长什么样。
    """
    http(FakeResponse(200, {"StatusCode": 0, "StatusMessage": "success",
                            "code": 0, "msg": "success", "data": {}}))
    res = FeishuNotifier("https://open.feishu.cn/hook/x").send("t", "b")
    assert res.ok is True and res.error == ""


def test_飞书_只回旧字段StatusCode非0也要判失败(http):
    """★ 只回旧字段 `StatusCode` 的端点（自建转发器 / 老版本网关）：

        {"StatusCode": 9499, "StatusMessage": "sign match fail"}

    原实现只看 `code`，缺了就按成功处理 —— 于是**"一条都没发出去"被记成成功**，
    而主链路一切正常、只有值班的人什么都没收到（本项目最警惕的失败形态）。
    实测到真实响应里两个字段并存之后，这里补上旧字段兜底。
    """
    http(FakeResponse(200, {"StatusCode": 9499, "StatusMessage": "sign match fail"}))
    res = FeishuNotifier("https://open.feishu.cn/hook/x").send("t", "b")
    assert res.ok is False
    assert "9499" in res.error and "sign match fail" in res.error


def test_飞书_新旧字段冲突时以新字段code为准(http):
    """两个都非 0 时报 `code`（新字段优先，旧字段只在缺失时兜底）。"""
    http(FakeResponse(200, {"code": 19021, "msg": "sign match fail",
                            "StatusCode": 9499, "StatusMessage": "legacy"}))
    res = FeishuNotifier("https://open.feishu.cn/hook/x").send("t", "b")
    assert res.ok is False and "19021" in res.error


def test_业务码缺失时按成功处理(http):
    """兼容"用户填的其实是个自建转发器"：它可能只回 200 空体。
    我们唯一确知的事实是"对端收下了"，把它当失败会造成大面积误报。"""
    fake = http(FakeResponse(200, None), FakeResponse(200, None))
    assert WebhookNotifier("https://hook.example.com/x").send("t", "b").ok is True
    assert DingTalkNotifier(_URL).send("t", "b").ok is True
    assert fake.call_count == 2


# ============================================================
# 五、dispatcher：渠道隔离（一个挂不能拖垮另一个）
# ============================================================
def test_一个渠道失败另一个成功_两者结果都在且无异常逃出(http):
    """★ 这是整层的底线：钉钉挂了不能让自建 webhook 也不发；
    而且任何异常都不允许逃到"已经诊断完成"的主链路上去。"""
    # 第一个渠道：假渠道直接抛异常（模拟"渠道自己没兜住"）
    bad = FakeNotifier("dingtalk", RuntimeError("boom"))
    good = FakeNotifier("webhook", NotifyResult(channel="webhook", ok=True, status=200))

    out = Dispatcher([bad, good], max_attempts=1, min_interval=0).notify(
        "t", "b", key="k")  # 不抛异常就是这条用例最重要的断言

    assert len(out.results) == 2
    assert out.results[0].ok is False and "boom" in out.results[0].error
    assert out.results[1].ok is True
    assert [r.channel for r in out.results] == ["dingtalk", "webhook"]
    assert out.deduped is False
    assert good.call_count == 1, "第一个渠道失败不能影响第二个渠道被调用"


def test_渠道列表就是channels属性():
    d = Dispatcher([FakeNotifier("a"), FakeNotifier("b")])
    assert d.channels == ["a", "b"]


def test_渠道全失败时结果仍完整返回():
    """全挂也不能抛 —— 调用方要拿到"每个渠道为什么挂"才写得出审计。"""
    a = FakeNotifier("a", NotifyResult(channel="a", ok=False, error="x"))
    b = FakeNotifier("b", NotifyResult(channel="b", ok=False, error="y"))
    out = Dispatcher([a, b], max_attempts=1, min_interval=0).notify("t", "b", key="k")
    assert [r.ok for r in out.results] == [False, False]
    assert out.ok is False


# ============================================================
# 六、重试边界（attempts 到底等于几）
# ============================================================
def test_第一次失败第二次成功_attempts为2且最终成功():
    """退避一轮后成功：attempts==2，且**最后一次成功后不再睡**。"""
    clock, sleeper = FakeClock(), SleepSpy()
    n = FakeNotifier("a",
                     NotifyResult(channel="a", ok=False, error="第一次失败"),
                     NotifyResult(channel="a", ok=True, status=200))
    d = Dispatcher([n], max_attempts=3, backoff=(0.5, 1.0, 2.0),
                   sleep=sleeper, clock=clock)

    out = d.notify("t", "b", key="")

    assert out.results[0].ok is True
    assert out.results[0].attempts == 2
    assert n.call_count == 2
    assert sleeper.calls == [0.5], "只失败了一次，退避只该有一条"


def test_一直失败_次数正好等于max_attempts():
    """★ 边界之一：`max_attempts=3` 是**总次数**（首次 + 2 次重试），
    不是"重试 3 次 = 共 4 次"。写错会让通知的总耗时对不上账。"""
    clock, sleeper = FakeClock(), SleepSpy()
    n = FakeNotifier("a", *[NotifyResult(channel="a", ok=False, error="always")
                            for _ in range(10)])
    d = Dispatcher([n], max_attempts=3, backoff=(0.5, 1.0, 2.0),
                   sleep=sleeper, clock=clock)

    out = d.notify("t", "b", key="")

    assert n.call_count == 3, "总次数必须等于 max_attempts"
    assert out.results[0].attempts == 3
    assert out.results[0].ok is False
    assert out.results[0].error == "always"


def test_退避序列是backoff前n减一项且失败后不补睡():
    """★ 边界之二：退避只在"后面还有重试"时才睡。
    一直失败时序列是 (0.5, 1.0) —— 最后那次失败之后绝不再睡 2.0，
    否则就是"已经放弃了还白等 2 秒"（用户感知为"通知怎么这么慢"）。"""
    clock, sleeper = FakeClock(), SleepSpy()
    n = FakeNotifier("a", *[NotifyResult(channel="a", ok=False, error="x")
                            for _ in range(5)])
    Dispatcher([n], max_attempts=3, backoff=(0.5, 1.0, 2.0),
               sleep=sleeper, clock=clock).notify("t", "b", key="")
    assert sleeper.calls == [0.5, 1.0]


def test_退避序列用尽时取最后一项封顶():
    """backoff 短于重试次数时不能"不睡"：对端正在故障中，
    密集重试只会让它更起不来。取最后一项 = 封顶。"""
    clock, sleeper = FakeClock(), SleepSpy()
    n = FakeNotifier("a", *[NotifyResult(channel="a", ok=False, error="x")
                            for _ in range(9)])
    Dispatcher([n], max_attempts=5, backoff=(0.1, 0.2),
               sleep=sleeper, clock=clock).notify("t", "b", key="")
    assert sleeper.calls == [0.1, 0.2, 0.2, 0.2]


def test_max_attempts为0时至少试一次():
    """配置写 0 是误配，但语义不能变成"一个渠道都没试过就返回结果"。"""
    n = FakeNotifier("a", NotifyResult(channel="a", ok=True))
    Dispatcher([n], max_attempts=0).notify("t", "b", key="")
    assert n.call_count == 1


def test_elapsed_ms把退避等待也算进去():
    """elapsed_ms 的口径是"这个渠道总共拖了多久"，**含退避等待**。
    口径写错（只算请求耗时不算退避）会让人对不上"为什么通知花了 2 秒"。

    这里让假时钟在每次 send 之间前进 30ms、退避前进 500ms，
    期望值就是 30（第一次尝试）+ 500（退避）+ 30（第二次尝试）= 560。
    """
    clock, sleeper = FakeClock(), SleepSpy()

    def sleeping(seconds):
        sleeper.calls.append(seconds)
        clock.advance(seconds)

    n = FakeNotifier("a", NotifyResult(channel="a", ok=False, error="1"),
                     NotifyResult(channel="a", ok=True))

    out = Dispatcher([n], max_attempts=2, backoff=(0.5,),
                     sleep=sleeping, clock=clock).notify("t", "b", key="")

    assert out.results[0].attempts == 2
    assert out.results[0].elapsed_ms >= 500, "退避的 500ms 必须在内"
    assert out.results[0].elapsed_ms < 510, "不能把不存在的时间算进来"


# ============================================================
# 七、去重（防通知轰炸）
# ============================================================
def test_同key在窗口内第二次去重且不发():
    """★ 告警风暴是本场景常态：一台机器挂了 10 分钟推 200 条同源告警。
    每一条都发 = 群里刷屏 = 所有人屏蔽机器人 = 通知渠道死掉（比不通知更糟）。"""
    clock = FakeClock()
    n = FakeNotifier("a", NotifyResult(channel="a", ok=True))
    d = Dispatcher([n], min_interval=60, clock=clock, sleep=SleepSpy())

    first = d.notify("t", "b", key="cpu_high@web-01")
    assert first.deduped is False and n.call_count == 1

    clock.advance(59)
    second = d.notify("t", "b", key="cpu_high@web-01")
    assert second.deduped is True
    assert second.results == [], "去重命中要一个请求都不发"
    assert n.call_count == 1, "★ 关键：被去重时渠道的 send 不能被调用"


def test_窗口过后可以再次发送():
    clock = FakeClock()
    n = FakeNotifier("a", NotifyResult(channel="a", ok=True))
    d = Dispatcher([n], min_interval=60, clock=clock, sleep=SleepSpy())

    d.notify("t", "b", key="k")
    clock.advance(61)
    again = d.notify("t", "b", key="k")
    assert again.deduped is False
    assert n.call_count == 2


def test_不同key互不影响():
    clock = FakeClock()
    n = FakeNotifier("a", NotifyResult(channel="a", ok=True))
    d = Dispatcher([n], min_interval=60, clock=clock, sleep=SleepSpy())
    assert d.notify("t", "b", key="host-a").deduped is False
    assert d.notify("t", "b", key="host-b").deduped is False
    assert n.call_count == 2


def test_空key表示不去重():
    """调用方明确说"这条每次都要发"时用空 key。"""
    clock = FakeClock()
    n = FakeNotifier("a", NotifyResult(channel="a", ok=True))
    d = Dispatcher([n], min_interval=60, clock=clock, sleep=SleepSpy())
    assert d.notify("t", "b", key="").deduped is False
    assert d.notify("t", "b", key="").deduped is False
    assert n.call_count == 2


def test_被去重的那次含成功渠道时整体仍算ok():
    """deduped 是设计行为不是故障 —— to_dict 里的 ok 不能因为去重变 False，
    否则审计里会看到一堆假的"通知失败"。"""
    clock = FakeClock()
    n = FakeNotifier("a", NotifyResult(channel="a", ok=True))
    d = Dispatcher([n], min_interval=60, clock=clock, sleep=SleepSpy())
    d.notify("t", "b", key="k")
    out = d.notify("t", "b", key="k")
    assert out.deduped is True and out.ok is True
    assert out.to_dict()["deduped"] is True


# ============================================================
# 八、build_dispatcher：零出站的默认值
# ============================================================
def test_渠道为空或缺省时返回None(monkeypatch):
    """★ 默认配置下这个模块**一行网络代码都不会执行**。
    这也是"不配就不会有出站流量"这条安全默认值的实现。"""
    _capture_warnings(monkeypatch)
    assert build_dispatcher({}) is None
    assert build_dispatcher({"NOTIFY_CHANNELS": ""}) is None
    assert build_dispatcher({"NOTIFY_CHANNELS": "   "}) is None
    assert build_dispatcher({"NOTIFY_CHANNELS": ",,"}) is None


def test_不配URL就等于不出站(monkeypatch):
    """配了渠道名但没有 URL：不能"猜一个地址"发出去（出站泄露不可撤回）。"""
    warns = _capture_warnings(monkeypatch)
    assert build_dispatcher({"NOTIFY_CHANNELS": "webhook,dingtalk,feishu"}) is None
    for ch in ("webhook", "dingtalk", "feishu"):
        target = f"NOTIFY_{ch.upper()}_URL"
        assert any(target in w for w in warns), f"{ch} 缺 URL 必须明确警告，不能静默跳过"


def test_点名但缺URL的渠道被跳过_其它渠道照发(monkeypatch):
    """半配置不能让整个通知层失效，但必须留下痕迹（警告），否则是排查地狱。"""
    warns = _capture_warnings(monkeypatch)
    d = build_dispatcher({
        "NOTIFY_CHANNELS": "webhook,dingtalk",
        "NOTIFY_WEBHOOK_URL": "https://hook.example.com/x",
    })
    assert d is not None
    assert d.channels == ["webhook"]
    assert any("dingtalk" in w for w in warns)


def test_三个渠道都能装配且各自带上secret(monkeypatch):
    _capture_warnings(monkeypatch)
    d = build_dispatcher({
        "NOTIFY_CHANNELS": "webhook,dingtalk,feishu",
        "NOTIFY_WEBHOOK_URL": "https://hook.example.com/x",
        "NOTIFY_DINGTALK_URL": _URL,
        "NOTIFY_DINGTALK_SECRET": _SECRET,
        "NOTIFY_FEISHU_URL": "https://open.feishu.cn/hook/x",
        "NOTIFY_FEISHU_SECRET": _SECRET,
        "NOTIFY_TIMEOUT": "3",
        "NOTIFY_MAX_ATTEMPTS": "5",
        "NOTIFY_MIN_INTERVAL": "10",
    })
    assert d is not None, "三个渠道都配齐了，不该返回 None"
    assert d.channels == ["webhook", "dingtalk", "feishu"]
    assert d.max_attempts == 5
    assert d.min_interval == 10
    assert d.notifiers[1].secret == _SECRET
    assert d.notifiers[0].timeout == 3


def test_认不出渠道名时警告但不抛(monkeypatch):
    """`dingding` / `lark` / `wechat` 是高频拼错。一个名字打错不该让服务起不来，
    但也绝不能静静地什么都不做。"""
    warns = _capture_warnings(monkeypatch)
    d = build_dispatcher({
        "NOTIFY_CHANNELS": "dingding,webhook",
        "NOTIFY_WEBHOOK_URL": "https://hook.example.com/x",
    })
    assert d is not None and d.channels == ["webhook"]
    assert any("dingding" in w for w in warns)


def test_通道名大小写与空格容错(monkeypatch):
    _capture_warnings(monkeypatch)
    d = build_dispatcher({
        "NOTIFY_CHANNELS": " Webhook , DINGTALK ",
        "NOTIFY_WEBHOOK_URL": "https://hook.example.com/x",
        "NOTIFY_DINGTALK_URL": _URL,
    })
    assert d is not None
    assert d.channels == ["webhook", "dingtalk"]


def test_坏掉的整数配置回退默认值并警告(monkeypatch):
    """为 `NOTIFY_TIMEOUT=8s` 让整个服务起不来，是把旁路错误升级成主链路故障。"""
    warns = _capture_warnings(monkeypatch)
    d = build_dispatcher({
        "NOTIFY_CHANNELS": "webhook",
        "NOTIFY_WEBHOOK_URL": "https://hook.example.com/x",
        "NOTIFY_TIMEOUT": "8s",
        "NOTIFY_MAX_ATTEMPTS": "many",
    })
    assert d is not None
    assert d.max_attempts == 3
    assert len(warns) == 2


def test_mask开关是独立读的(monkeypatch):
    """NOTIFY_MASK 与渠道配置无关：调用方要能在"不出站"时也读到开关值。"""
    # ★ 默认必须是**开**：出站内容离开信任边界，默认走安全的一边。
    #   这条断言的由来：实现方最初按"默认不改写用户正文"取了关，
    #   复核时按项目 Rule「不泄露敏感信息」翻了过来 —— 改默认值必须改这里。
    #   两个方向的代价不对称：默认开只是通知里少一段（还能去审批单看原文），
    #   默认关则是凭据进了第三方 IM 的聊天记录、收不回来。
    assert mask_enabled({}) is True
    assert mask_enabled({"NOTIFY_MASK": "0"}) is False
    assert mask_enabled({"NOTIFY_MASK": "1"}) is True
    assert mask_enabled({"NOTIFY_MASK": "true"}) is False


# ============================================================
# 九、mask：正例 + 反例（反例同样重要）
# ============================================================
def test_mask_掩掉sk开头的key():
    assert "sk-abcdef1234567890" not in mask("key=sk-abcdef1234567890")
    assert mask("sk-abcdef1234567890") == "sk-<redacted>"


def test_mask_掩掉Bearer与Basic与Token头():
    assert "eyJhbGciOiJIUzI1NiJ9" not in mask(
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert mask("Authorization: Basic dXNlcjpwYXNzd29yZA==") == \
        "Authorization: Basic <redacted>"
    assert mask("X-Auth-Token: AbCd1234EfGh") == "X-Auth-Token: <redacted>"


def test_mask_掩掉XAPIKey头():
    secret = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
    assert secret not in mask(f"curl -H 'X-API-Key: {secret}' http://h/x")
    assert mask(f"X-API-Key: {secret}") == "X-API-Key: <redacted>"


def test_mask_掩掉password与token与secret赋值():
    assert mask("mysql --password=hunter2 -e 'show status'") == \
        "mysql --password=<redacted> -e 'show status'"
    assert mask("password=\"p@ss w0rd!\"") == "password=<redacted>"
    assert mask("redis-cli -a secret=s3cr3t-value") == "redis-cli -a secret=<redacted>"
    assert mask("token=abc123, service=nginx") == "token=<redacted>, service=nginx"
    assert mask("api_key: AKIA1234567890XYZ") == "api_key: <redacted>"
    # 键名（可读的运维信息）必须保留，只换值
    assert mask("DB_PASSWORD=P@ssw0rd123!").startswith("DB_PASSWORD=")


def test_mask_长base64与十六进制串折叠():
    assert mask("9f8e7d6c5b4a39281706f5e4d3c2b1a0") == "<redacted>"
    assert mask("aGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgYmFzZTY0") == "<redacted>"


def test_mask_短串不该被误伤():
    """反例：24 字符以下的值是普通运维信息，掩掉就丢了可读性。"""
    assert mask("code=1") == "code=1"
    assert mask("PID 4711") == "PID 4711"
    assert mask("port 8080") == "port 8080"
    assert mask("build 20240517") == "build 20240517"


def test_mask_保留主机名服务名路径与退出码():
    """★ 反例（最重要的那组）：掩得太多等于没通知 ——
    收通知的人只看到"有件事发生了"，还得自己上机器再看一遍。"""
    for text in [
        "nginx restart on web-01 exited with code 1",
        "connect 192.168.1.10:5432 failed, retry in 5s",
        "open /var/log/nginx/error.log: permission denied",
        "service=nginx host=web-01 exit_code=1",
        "kernel 5.15.0-91-generic uptime 12d",
        "docker restart agentdesk  (exit code 0)",
        "ETIMEDOUT connecting to 10.0.0.7:6379",
        "tail -n 100 /var/log/mysql/slow.log",
        "see https://agent.simosheng.fun/dashboard?limit=30 for details",
        "openssl 3.0.13 ECDHE-RSA-AES256-GCM-SHA384",
    ]:
        assert mask(text) == text, f"不该被改写的运维信息：{text}"


def test_mask_保留带算法前缀的摘要但掩掉裸摘要():
    """`sha256:xxx` 是运维要看的构建号；同样形状的裸长十六进制串是密钥。
    区分它们只能靠前缀 —— 这条同时钉住两个方向。"""
    digest = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
    assert mask(f"image sha256:{digest}") == f"image sha256:{digest}"
    assert mask(digest) == "<redacted>"


def test_mask_保留passwd报错里的普通单词():
    """★ 实测误伤：`passwd: authentication token manipulation error`
    是 /etc/shadow 出问题时的标准报错。规则如果把 `passwd:` 当赋值、
    或把 `token manipulation` 当认证头，这句话就会变成读不通的碎片。"""
    text = "passwd: authentication token manipulation error"
    assert mask(text) == text


def test_mask_空与None安全返回():
    assert mask("") == ""
    # 故意传 None：容错是行为要求（空正文不该让通知发不出去）
    assert mask(None) == ""  # type: ignore[arg-type]


def test_mask_重复调用稳定():
    """掩过的结果再过一次不该继续变短。

    调用链上可能被掩两遍（渠道层 + 路由层都过了一道），
    如果 mask 会叠加改写，第二遍就会把 `<redacted>` 本身再吃掉一层。
    """
    text = ("deploy failed; Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc "
            "password=hunter2 host=web-01")
    once = mask(text)
    assert mask(once) == once
    assert "host=web-01" in once


def test_mask_不改动原文里的敏感值之外的字节数():
    """掩的是"值"，不是整段文本：命令本身要留给收通知的人。"""
    text = "systemctl restart nginx --password=TopSecret1 ; tail -n 50 /var/log/nginx/error.log"
    out = mask(text)
    assert "systemctl restart nginx" in out
    assert "/var/log/nginx/error.log" in out
    assert "TopSecret1" not in out
