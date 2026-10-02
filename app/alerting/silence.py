# -*- coding: utf-8 -*-
"""告警抑制：计划内维护窗口里的告警不该叫人起床。

【为什么需要它】
"凌晨三点被叫醒"里有一大半不是故障，是**计划内变更**：
磁盘扩容、发版、数据库主从切换 —— 这些操作本身就会触发一堆告警。
没有抑制窗口的话，Agent 会认真地为你自己的维护动作做诊断、推通知，
几次之后值班的人就开始无视通知了 —— **那才是真正的事故**。

【为什么是"最小实现"而不是规则引擎】
完整的抑制系统有标签匹配、正则、嵌套、按时间段复用、静默审批……那是另一个产品。
这里只要一个 JSON 文件，够挡住"我自己在维护"这类最常见的场景：

    data/alert_silences.json
    [
      {"alertname_prefix": "DiskSpace", "host_prefix": "db-01",
       "until": "2026-10-03T02:00:00", "note": "磁盘扩容中（变更单 CHG-2026-101）"}
    ]

【两条有意的取舍（都写进这里，避免下一个人猜）】
1. **解析不出 `until` 就不抑制**（fail-open）。
   抑制本来就是"少做一件事"，这条路出错时更吵一点没关系；
   反过来 fail-closed（解析不出来就一律静默）会把真告警藏起来，
   而"藏起来的告警"是所有运维事故里最难查的一种。
2. **空前缀 = 匹配一切**，但只在显式给出 `""` 时才这样；
   字段缺失（`None`）表示"这条不限制该维度"，两者是不同的语义，别混。
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from app.llm import PROJECT_ROOT

DEFAULT_PATH = PROJECT_ROOT / "data" / "alert_silences.json"


class Silence:
    """按文件内容抑制告警；文件改了不必重启（按 mtime 懒加载）。"""

    def __init__(self, path: Path = None, clock=time.time):
        self.path = Path(path) if path else DEFAULT_PATH
        self._clock = clock
        self._lock = threading.RLock()
        self._rules: list = []
        self._stamp = None

    # ---------- 加载 ----------
    def load(self, force: bool = False) -> list:
        """读规则文件。文件不存在 / 内容坏了 → 返回空列表（不是抛异常）。"""
        with self._lock:
            try:
                st = self.path.stat()
            except OSError:
                self._rules, self._stamp = [], None
                return []

            # ★ 缓存键用 (mtime_ns, size) 而不是单独的 mtime。
            #   文件系统的时间戳粒度有限（Windows 上曾出现"同一刻度内两次写入
            #   mtime 一模一样"），只比 mtime 会让"刚改完文件却不生效"变成
            #   一个要靠重启才能绕过的怪现象。加上 size 能挡住绝大多数快速改写。
            stamp = (st.st_mtime_ns, st.st_size)

            if not force and self._stamp == stamp:
                return list(self._rules)

            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # 文件读坏时 **不抑制**：宁可多叫一次，也不要因为一个坏文件
                # 把整段告警静默掉，而且没人知道是它干的。
                self._rules, self._stamp = [], stamp
                return []

            if isinstance(raw, dict):            # 容忍 {"silences": [...]} 这种包法
                raw = raw.get("silences") or []
            self._rules = [r for r in raw if isinstance(r, dict)]
            self._stamp = stamp
            return list(self._rules)

    # ---------- 匹配 ----------
    @staticmethod
    def _prefix_ok(rule_value, actual: str) -> bool:
        """前缀匹配。字段缺失（None）= 该维度不限制；给空串 "" 才等于匹配一切。"""
        if rule_value is None:
            return True
        return str(actual or "").startswith(str(rule_value))

    def match(self, alert: dict, now: datetime = None) -> dict:
        """命中的抑制规则（含 `_matched_until`），没命中返回 None。"""
        rules = self.load()
        if not rules:
            return None

        now = now or datetime.fromtimestamp(self._clock())
        for rule in rules:
            if not self._prefix_ok(rule.get("alertname_prefix"),
                                   alert.get("alertname") or ""):
                continue
            if not self._prefix_ok(rule.get("host_prefix"), alert.get("host") or ""):
                continue
            if not self._prefix_ok(rule.get("service_prefix"),
                                   alert.get("service") or ""):
                continue

            until_raw = rule.get("until")
            if until_raw is None:
                continue                      # 没写截止时间的规则不生效（避免永久静默）
            try:
                until = datetime.fromisoformat(str(until_raw))
            except (TypeError, ValueError):
                continue                      # 解析不了 → 不抑制（fail-open，见文件头）
            if until <= now:
                continue                      # 已过期

            hit = dict(rule)
            hit["_matched_until"] = until.isoformat(timespec="seconds")
            return hit
        return None

    def describe(self) -> dict:
        rules = self.load()
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "rules": len(rules),
            "active": len([r for r in rules if self.match({
                "alertname": r.get("alertname_prefix") or "X",
                "host": r.get("host_prefix") or "x",
                "service": r.get("service_prefix")})]),
        }
