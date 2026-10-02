# -*- coding: utf-8 -*-
"""告警聚合：把一场风暴收敛成**一个**故障。

【要解决的具体问题】
凌晨网络抖一下，Alertmanager 推来 500 条告警：`DiskSpaceLow`、`NginxHighErrorRate`、
`NginxUpstreamTimeout`……它们全是**同一个故障**（磁盘写满导致 nginx 出错）的不同侧面。
原先是每条告警各跑一轮诊断 —— 500 次模型调用、500 条互不相关的报告，
而且**没有任何地方能看出它们其实是同一件事**。

【做法：一个键同时干两件事】
    key = (host, service)      ← 聚合键；配 ALERT_AGGREGATE=0 退回旧的 (alertname, host)
    同一张表既用来**去重**（窗口内同键只处理一次），也用来**归并**（同键的告警进同一个事件）。

    一个键做两件事，是为了消除"去重表和聚合表各说各话"的可能 ——
    那类不一致会让"这条告警到底算不算新故障"变成一个说不清的问题，
    而它偏偏又决定了要不要花钱调模型。

【内存必须有界（这是一处真实缺陷的修复）】
原先的去重表是模块级的 `_ALERT_LAST_SEEN = {}`，**只增不减**：
长期运行就是内存单调增长，而且它是那种"谁也不会注意到"的泄漏 ——
不报错、不告警，只是一天天变大，直到某天重启才发现启动变慢。
现在改成带 TTL 的滑动窗口 + 容量上限：超了先清过期条目，再淘汰最旧的。

【为什么不引入 Redis / 数据库】
单进程部署（`--workers 1`）下内存态就是准的，这一点与限流层是同一个取舍。
多实例要换共享存储，这是**已知边界**，写在注释里而不是假装不存在。
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class _Entry:
    """一个聚合键在窗口内的状态。"""

    processed_at: float                 # 上次**真正处理**（走诊断）的时间
    last_seen: float                    # 上次收到同键告警的时间（含被抑制的）
    count: int = 1                      # 窗口内同键告警条数（含被抑制的）
    incident_id: str = ""               # 这批告警归到哪个事件上


@dataclass
class AggVerdict:
    """一次 ingest 的判定结果。"""

    is_new: bool                        # True = 该处理（调模型）；False = 窗口内重复
    key: str
    members: int = 1                    # 窗口内同键告警条数（含本次）
    prev_incident_id: str = ""
    prev_processed_at: float = 0.0
    entry: _Entry = field(default=None, repr=False)


class AlertAggregator:
    """有界的告警去重 / 聚合表。

    clock 与 window 都可注入，所以测试是**确定性**的（不用 sleep、不看墙上时间）。
    """

    def __init__(self, *, window_seconds: int = 600, aggregate: bool = True,
                 max_entries: int = 5000, clock=time.time):
        self.window = max(1, int(window_seconds))
        self.aggregate = bool(aggregate)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self._seen: "OrderedDict[str, _Entry]" = OrderedDict()
        self._prune_every = 128          # 每 N 次 ingest 清理一次，避免每次都全表扫
        self._ingests = 0

    # ---------- 键 ----------
    def key(self, alert: dict) -> str:
        """聚合键。

        aggregate=True  → `host|service`：同机同服务的不同告警名归成一个故障
        aggregate=False → `alertname|host`：完全退回改动前的语义

        主机/服务缺失时用 `-` 占位：**不能让 None 变成"匹配一切"** ——
        那正是白名单里混进 `/` 那类错误的同一个形状。
        """
        if self.aggregate:
            host = (alert.get("host") or "-").strip() or "-"
            service = (alert.get("service") or "-").strip() or "-"
            return f"{host}|{service}"
        name = (alert.get("alertname") or "-").strip() or "-"
        host = (alert.get("host") or "-").strip() or "-"
        return f"{name}|{host}"

    # ---------- 主入口 ----------
    def ingest(self, alert: dict) -> AggVerdict:
        """记一条告警，并告诉调用方"该不该为它花钱"。

        窗口内重复 → `is_new=False`（调用方应当只做归并，不调模型）。
        """
        k = self.key(alert)
        now = self._clock()
        with self._lock:
            self._ingests += 1
            self._maybe_prune(now)

            entry = self._seen.get(k)
            if entry is not None and (now - entry.processed_at) < self.window:
                # 窗口内重复：**不刷新 processed_at**。
                #   刷新的话，只要风暴不停，这个键就永远不会过期 ——
                #   那等于"风暴期间这个故障只诊断一次，直到世界安静下来"，
                #   听起来不错，但会让一条持续 8 小时的抖动把真正的新故障一并吞掉。
                entry.count += 1
                entry.last_seen = now
                self._seen.move_to_end(k)
                return AggVerdict(is_new=False, key=k, members=entry.count,
                                  prev_incident_id=entry.incident_id,
                                  prev_processed_at=entry.processed_at,
                                  entry=entry)

            self._seen[k] = _Entry(processed_at=now, last_seen=now)
            self._seen.move_to_end(k)
            return AggVerdict(is_new=True, key=k, members=1,
                              entry=self._seen[k])

    # ---------- 事件绑定 ----------
    def bind_incident(self, key: str, incident_id: str) -> None:
        """把这个键上的一批告警绑到某个事件上。

        这样后续被抑制的告警就能在响应里说清"我并到哪个事件去了" ——
        **被抑制不等于被丢弃**，值班的人要能看到它去哪了。
        """
        with self._lock:
            entry = self._seen.get(key)
            if entry is not None:
                entry.incident_id = incident_id

    def incident_of(self, key: str) -> str:
        with self._lock:
            entry = self._seen.get(key)
            return entry.incident_id if entry else ""

    # ---------- 有界性 ----------
    def _maybe_prune(self, now: float) -> None:
        if self._ingests % self._prune_every == 0 or len(self._seen) > self.max_entries:
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> int:
        """清掉过期条目；仍然超上限就淘汰最旧的。返回清掉多少条。"""
        removed = 0
        for k in [k for k, e in self._seen.items()
                  if (now - e.processed_at) >= self.window]:
            del self._seen[k]
            removed += 1
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)      # 淘汰最旧的
            removed += 1
        return removed

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked(self._clock())

    def size(self) -> int:
        with self._lock:
            return len(self._seen)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
            self._ingests = 0

    def describe(self) -> dict:
        with self._lock:
            return {
                "mode": "aggregate" if self.aggregate else "legacy-exact",
                "window_seconds": self.window,
                "entries": len(self._seen),
                "max_entries": self.max_entries,
            }
