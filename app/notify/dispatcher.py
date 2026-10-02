# -*- coding: utf-8 -*-
"""
分发器：多个渠道的并发投递 + 去重 + 重试退避 + 配置装配
=========================================================

【这个文件是整个通知层的"总闸"，因此它的第一职责不是发送，而是兜底】

调用方（app/main.py）会在诊断**已经成功之后**调 `notify()`。那一刻，
"诊断结果"已经在内存里了，只差写审计和返回给用户。如果通知在这一步抛出
异常，调用方要么整个请求 500、要么走进 except 分支把结果丢了 ——
**为了一个旁路功能，牺牲了主链路唯一的产出**，这是最不能接受的事故形态。

所以这里有三层兜底，一层比一层笨，但合起来保证"异常不可能逃出去"：

    · 第一层：`_send_one` 把单个渠道的任何异常变成 NotifyResult(ok=False)
      （包括渠道自己没写好、或者打桩打坏了的情况）；
    · 第二层：`_send_with_retry` 连"重试/退避/计时本身"也兜住；
    · 第三层：`notify()` 整体再包一次 —— 就算上面的兜底代码自己有 bug，
      也只损失一次通知，不会波及主链路。

【去重（防通知轰炸）为什么必须有】

告警风暴是这个场景的常态：一台机器挂了，10 分钟内可能推 200 条同源告警。
每一条都触发一次"诊断 + 通知"，群里就会被同一个故障刷屏 ——
然后所有人开始屏蔽这个机器人，**通知渠道就死了**（比不通知更糟）。
按 `key`（调用方给的业务键，例如 alertname+host）在 min_interval 内只发第一条。

【去重状态是进程内内存，这是有意的】
和 security.py 的限流计数器同一个道理：部署固定 `--workers 1`，内存态就是准的。
换成 Redis 会让"通知层"凭空多一个外部依赖 —— 而通知层本身挂掉不能影响诊断，
不该由它来引入新的故障面。多 worker 时去重会变松（每个 worker 各发一次），
这个退化的后果是"多收几条消息"，可以接受。

【★ 重试边界：max_attempts 是"总次数"，不是"重试次数"】

`max_attempts=3` 的含义是**最多发 3 次**（首次 + 2 次重试），
两次退避分别是 backoff[0]=0.5s、backoff[1]=1.0s。所以：

    for attempt in 1..max_attempts:
        发
        if 成功: 返回
        if attempt < max_attempts: sleep(backoff[attempt-1])

两个边界都要想清楚，否则线上总耗时会对不上账：

    · 成功于第 3 次 → attempts=3，退了 **2** 次（0.5 + 1.0），
      **最后一次失败之后绝不再 sleep**。多睡一次纯粹是白等 ——
      "已经放弃了还要再等 2 秒"，在小红书式的排障里就是"通知怎么这么慢"。
    · 一直失败 → attempts == max_attempts，退避序列是前 max_attempts-1 项，
      末尾那个 2.0 用不上。它的存在是给"把 max_attempts 调大"留的余量，
      而不是被浪费了。

总耗时上界 = max_attempts × timeout + sum(backoff[:max_attempts-1])。
默认配置是 3×8 + 1.5 = 25.5s —— 这是收尾阶段能接受的上限，
再往上调之前先想想"用户要等多久才看到回答"。

【为什么退避要能被注入】
`Dispatcher(sleep=..., clock=...)` 两个参数存在的唯一理由是**测试不许真的睡**。
默认值就是真的 `time.sleep` / `time.time`，生产上不需要传。
退避序列本身也用参数给（而不是写死 2**n）：运维现场调参的唯一入口就是 .env。
"""

import logging
import os
import threading
import time
from dataclasses import dataclass

from app.notify.base import NotifyResult, env_int, mask_enabled
from app.notify.dingtalk import DingTalkNotifier
from app.notify.feishu import FeishuNotifier
from app.notify.webhook import WebhookNotifier

log = logging.getLogger("agentdesk.notify")

# 认得的渠道名。写错名字（NOTIFY_CHANNELS=dingding）必须明确警告，
# 不能静静地什么都不做 —— 那正是"以为在通知、其实一直失败"的经典成因。
KNOWN_CHANNELS = ("webhook", "dingtalk", "feishu")


@dataclass
class NotifyOutcome:
    """一次 `notify()` 的整体结果。

    deduped=True 表示**被去重拦下、一个请求都没发**，此时 results 为空列表。
    【为什么结果用列表而不是字典】渠道可能重名（用户手工塞了两个 webhook），
    列表不会互相覆盖，且顺序与 notifiers 一致，好对账。
    """

    results: list
    deduped: bool = False

    @property
    def ok(self) -> bool:
        """是否"没有失败"。注意 deduped 时也算 ok —— 去重是设计行为，不是故障。"""
        return all(r.ok or r.skipped for r in self.results)

    def to_dict(self) -> dict:
        return {
            "deduped": self.deduped,
            "ok": self.ok,
            "results": [r.to_dict() for r in self.results],
        }


class Dispatcher:
    """把一次通知投给所有渠道。线程安全。

    【锁的边界】进程内一把锁，只保护去重状态（读+写时间戳），
    **不跨越任何网络调用** —— 理由见 `_is_duplicate` 的 docstring：
    持锁发送会让一条卡住的 webhook 堵死所有渠道。

    【去重状态的内存占用】`_last_sent` 是一个 key→时间戳 的字典，只增不减。
    这是**有意的**：key 由调用方给（业务实体，如 alertname+host），
    取值空间是有限的、和"有多少种告警"同阶，不是"有多少条告警"。
    真到了 key 无限增长的场景（比如拿 UUID 当 key），那是调用方用错了参数，
    而这个字典一条也才几十字节 —— 不值得为它引入定时清理的复杂度。
    """

    def __init__(self, notifiers: list, max_attempts: int = 3,
                 min_interval: int = 60, backoff: tuple = (0.5, 1.0, 2.0),
                 sleep=time.sleep, clock=time.time):
        self.notifiers = list(notifiers or [])
        # 至少 1 次：0 会导致"一个渠道都没试过"却返回结果，语义混乱
        self.max_attempts = max(1, int(max_attempts))
        self.min_interval = max(0, int(min_interval))
        self.backoff = tuple(backoff or ())
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_sent: dict = {}

    # ---------- 只读信息 ----------
    @property
    def channels(self) -> list:
        """渠道名列表（与 notifiers 同序）。给日志与 /health 用。"""
        return [n.name for n in self.notifiers]

    # ---------- 主入口 ----------
    def notify(self, title: str, text: str, *, key: str,
               payload: dict | None = None) -> NotifyOutcome:
        """投递一次。**绝不抛异常**，所有失败都变成 ok=False 的结果。

        key: 去重键（业务含义，如 "cpu_high@web-01"）。空 key 表示不做去重
             —— 调用方明确说"这条每次都要发"时用它。
        """
        try:
            if key and self._is_duplicate(key):
                # 去重命中：**连一个 HTTP 请求都不发**。
                # 这是与调用方约定好的语义：deduped=True 且 results=[]，
                # 调用方据此写一条 "notify.deduped" 审计，而不是"发送失败"。
                return NotifyOutcome(results=[], deduped=True)

            results = []
            for notifier in self.notifiers:
                results.append(self._send_with_retry(notifier, title, text, payload))
            return NotifyOutcome(results=results)
        except Exception as e:  # pragma: no cover - 兜底，正常路径不会到这里
            # 走到这里说明兜底代码自己坏了。宁可少一次通知，也不能让异常
            # 逃到"已经诊断完成"的主链路上去。
            log.exception("通知分发出现未预期异常（已被吞掉，不影响主链路）")
            return NotifyOutcome(results=[NotifyResult(
                channel="dispatcher", ok=False,
                error=f"{type(e).__name__}: {e}")])

    # ---------- 去重 ----------
    def _is_duplicate(self, key: str) -> bool:
        """同 key 在 min_interval 秒内是否已经发过（命中则返回 True）。

        【为什么"记时间"要在发送**之前**做】
        这里是**先占坑再发送**。反过来（发成功了才记时间）会有一个漏洞：
        慢渠道 + 高并发时，两个同源告警几乎同时进来，都通过去重判断，
        于是两条都发了 —— 而去重存在的理由恰恰就是应对"同时来一堆"。
        代价是"发送全失败也照样占用一次窗口"，但那个后果是良性的：
        失败在 NotifyResult 里看得见，窗口过后下一轮告警照样会发。

        【为什么锁只包住"读时间+写时间"，不包住发送】
        一开始想的是"持锁发送"来做到严格单飞，但那会把 HTTP I/O（最长
        8 秒、重试后 25 秒）关在锁里：一个 webhook 卡住，**所有**其它告警的
        通知都排在它后面 —— 包括本来能发出去的渠道。这是把一个渠道的故障
        放大成全渠道的故障，正是这个模块最该避免的事。
        所以锁内只做状态判断，锁外发送。代价是"同一毫秒到达的两条同源告警
        可能都发出去"（严格说去重会漏一次），收益是故障不会横向扩散 ——
        对通知层来说，多发一条消息远比"整层被一条慢请求堵死"轻。
        """
        now = self._clock()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self.min_interval:
                return True
            self._last_sent[key] = now
            return False

    # ---------- 单渠道 + 重试 ----------
    def _send_with_retry(self, notifier, title: str,
                         text: str, payload: dict | None) -> NotifyResult:
        """一个渠道的完整重试过程。**这个函数本身也不允许抛。**"""
        started = self._clock()
        name = getattr(notifier, "name", "unknown")
        last_error = ""
        last_status = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                res = notifier.send(title, text, payload=payload)
            except Exception as e:
                # 渠道自己没兜住异常（自研渠道最容易犯）。这里补上：
                # 异常不逃出去，但**要记下来**，否则这个渠道静默变哑巴。
                res = NotifyResult(
                    channel=name, ok=False,
                    error=f"{type(e).__name__}: {e}")

            if res.ok:
                # 成功也要校正两个字段：
                #   · attempts —— 渠道内部可能有自己的重试/降级，
                #     而这里要回答的是"分发器视角下发了几次"；
                #   · elapsed_ms —— 必须**含退避等待**。不覆盖的话，成功
                #     路径上的耗时是"最后一次 HTTP 的耗时"，睡掉的 1.5 秒
                #     凭空消失，对账时对不上"为什么通知花了 2 秒"。
                #     （失败路径下面自己造 NotifyResult，天然是全程耗时。）
                res.attempts = attempt
                res.elapsed_ms = int((self._clock() - started) * 1000)
                return res

            last_error = res.error or "未知失败"
            last_status = res.status
            if attempt < self.max_attempts:
                self._backoff_sleep(attempt)

        return NotifyResult(
            channel=name, ok=False, status=last_status, error=last_error,
            attempts=self.max_attempts,
            elapsed_ms=int((self._clock() - started) * 1000))

    def _backoff_sleep(self, attempt: int) -> None:
        """退避等待。序列用尽就取最后一项（而不是不睡）——
        【为什么取最后一项】backoff=(0.5,1.0,2.0) 配 max_attempts=5 时，
        第 4 次退避没有对应项。此时"不睡"会让重试变成密集轰击对端
        （对端正在故障中，密集重试只会让它更起不来）；取最后一项是
        "封顶"的语义，符合指数退避的本意。
        """
        if not self.backoff:
            return
        idx = min(attempt - 1, len(self.backoff) - 1)
        delay = float(self.backoff[idx])
        if delay > 0:
            self._sleep(delay)


# ============================================================
# 配置装配
# ============================================================
def _env(env: dict | None) -> dict:
    return os.environ if env is None else env


def build_dispatcher(env: dict | None = None) -> Dispatcher | None:
    """按环境变量装配分发器。**返回 None 表示完全不出站**。

    配置项（全部可留空）：
        NOTIFY_CHANNELS=webhook,dingtalk,feishu   ← 只有这里点到的渠道才会被构造
        NOTIFY_WEBHOOK_URL / NOTIFY_DINGTALK_URL / NOTIFY_DINGTALK_SECRET
        NOTIFY_FEISHU_URL / NOTIFY_FEISHU_SECRET
        NOTIFY_TIMEOUT=8  NOTIFY_MAX_ATTEMPTS=3  NOTIFY_MIN_INTERVAL=60
        NOTIFY_MASK=1      ← 只是给调用方读的开关，本模块不自动用它（见 mask_enabled）

    【env 参数为什么存在】
    默认读 `os.environ`（与 security.py 一致：`.env` 由 app.llm 在 import 时
    用 load_dotenv 灌进 os.environ，本项目所有配置模块都靠这一条链路）。
    显式传 dict 只有两个场景：测试注入确定值，以及将来要做"运行时可改配置"
    时由调用方把一份快照递进来。**不要**为了读 .env 在这里自己 load_dotenv ——
    那会让同一个进程里出现两份配置来源。

    【为什么用"白名单 + 返回 None"而不是"有 URL 就发"】
    与 security.py 的 PUBLIC_EXACT 白名单是同一个思路，而且理由更强：
    通知是**出站**的，一旦某个渠道的 URL 被误填（比如复用了别人的 webhook），
    泄露的就是整段诊断内容 —— 那是不可撤回的。
    所以只在 `NOTIFY_CHANNELS` 里被明确点名的渠道才会被构造。
    换个角度说：**默认状态下这个模块一行网络代码都不会执行**，
    用户不配就不会有任何出站流量，这是最安全的默认值。

    【某个渠道被点名但没配 URL → 跳过 + 明确警告，不是静默】
    静默跳过会造成"我明明配了 dingtalk 啊"的排查地狱。
    跳过是为了不因为半配置而让整个通知层失效（其它渠道照发），
    警告是为了让人看得见。这两件事不冲突。

    【所有点名渠道都没配 URL → None】
    与"完全没配"等价。返回 None 让调用方可以用 `if dispatcher:` 一行判断，
    不需要理解渠道细节。
    """
    e = _env(env)

    raw = str(e.get("NOTIFY_CHANNELS", "") or "").strip()
    if not raw:
        # 没点名任何渠道 = 通知功能关闭。这不是错误，是默认值。
        return None

    names = [c.strip().lower() for c in raw.split(",") if c.strip()]
    if not names:
        return None

    timeout = env_int(e, "NOTIFY_TIMEOUT", 8, log.warning)
    max_attempts = env_int(e, "NOTIFY_MAX_ATTEMPTS", 3, log.warning)
    min_interval = env_int(e, "NOTIFY_MIN_INTERVAL", 60, log.warning)

    notifiers = []
    for name in names:
        if name not in KNOWN_CHANNELS:
            # 拼错渠道名是高频错误（dingding / lark / wechat）。
            # 打警告而不是抛异常：一个名字打错不该让服务起不来。
            log.warning(
                "NOTIFY_CHANNELS 里的 %r 不是已知渠道（支持：%s），已跳过",
                name, "/".join(KNOWN_CHANNELS))
            continue

        url = str(e.get(f"NOTIFY_{name.upper()}_URL", "") or "").strip()
        if not url:
            log.warning(
                "NOTIFY_CHANNELS 点名了 %s，但没有配 NOTIFY_%s_URL，"
                "该渠道已跳过（其它渠道不受影响）",
                name, name.upper())
            continue

        if name == "webhook":
            notifiers.append(WebhookNotifier(url, timeout=timeout))
        elif name == "dingtalk":
            secret = str(e.get("NOTIFY_DINGTALK_SECRET", "") or "").strip()
            notifiers.append(DingTalkNotifier(url, secret=secret, timeout=timeout))
        else:
            secret = str(e.get("NOTIFY_FEISHU_SECRET", "") or "").strip()
            notifiers.append(FeishuNotifier(url, secret=secret, timeout=timeout))

    if not notifiers:
        log.warning(
            "NOTIFY_CHANNELS=%r 指定的渠道全部没有可用 URL，通知功能未启用"
            "（零出站）", raw)
        return None

    log.info("出站通知已启用：channels=%s mask=%s",
             ",".join(n.name for n in notifiers), mask_enabled(e))
    return Dispatcher(notifiers, max_attempts=max_attempts,
                      min_interval=min_interval)


__all__ = [
    "Dispatcher",
    "NotifyOutcome",
    "build_dispatcher",
    "mask_enabled",
    "KNOWN_CHANNELS",
]
