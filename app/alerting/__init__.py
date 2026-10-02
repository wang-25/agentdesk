# -*- coding: utf-8 -*-
"""告警入口层：归一化 / 聚合 / 抑制。

这一层回答"告警进来之后、调模型之前"要做的三件事：

    1. **归一化**（normalize）—— 不同告警系统的格式不一样，先统一结构
    2. **聚合**（aggregate）—— 一次风暴里 50 条同源告警是一个故障，不是 50 个
    3. **抑制**（silence）—— 计划内维护窗口里的告警不该叫人起床

【为什么把它们从 main.py 里搬出来】
原先 `normalize_alerts` / `alert_to_question` 定义在 `main.py`（那个文件已经 3000+ 行，
还内嵌了四个页面的 HTML）。搬出来的直接好处是**可以单独测**：
不用 import 整个服务入口，就能验证"空批次不该被当成告警"这类边界。
`main.py` 仍然 re-export 这两个函数，所以既有调用方与测试一行都不用改。
"""

from app.alerting.aggregator import AlertAggregator
from app.alerting.normalize import alert_to_question, normalize_alerts
from app.alerting.silence import Silence

__all__ = ["AlertAggregator", "Silence", "alert_to_question", "normalize_alerts"]
