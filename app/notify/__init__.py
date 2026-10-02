# -*- coding: utf-8 -*-
"""
AgentDesk 出站通知层（Notify）
================================

一句话：**把诊断结论推出到运维人真正会看的地方，并且推不出去也不能影响诊断。**

    NotifyResult      一次投递的结果（唯一的对外输出形态）
    Notifier          渠道抽象基类
    Dispatcher        多渠道路由 + 去重 + 重试退避
    build_dispatcher  按环境变量装配；没配就返回 None（零出站）
    mask              出站脱敏（凭据不落到第三方 IM 的聊天记录里）

【怎么用（调用方视角，app/main.py 是唯一调用方）】

    from app.notify import build_dispatcher, mask, mask_enabled

    dispatcher = build_dispatcher()          # 启动时读一次
    ...
    if dispatcher:                            # None = 完全不出站，不用管渠道细节
        text = mask(answer) if mask_enabled() else answer
        outcome = dispatcher.notify("凌晨诊断完成", text, key=alert_key,
                                    payload={"host": host, "exit_code": 1})
        write_audit("notify.sent", outcome.to_dict())   # ★ 审计由调用方写

【为什么审计不在这里做（这个分工是刻意的，不是偷懒）】

  1. 本包**不能 import app.main**：main 是调用方，反过来 import 就是循环导入。
     而审计写入函数 write_audit 就在 main 里。
  2. 更重要的理由：审计是主链路的痕迹，通知是出口。
     出口一旦有了写主链路状态的权力，"通知挂掉"和"审计挂了"就会互相牵连。
  3. 通知的结果必须留痕（否则"以为在通知、其实一直失败"会静默很久），
     但留痕的**位置和格式**属于主链路 —— 所以由调用方拿着 NotifyOutcome 去写。

【默认行为：什么都不发】
`NOTIFY_CHANNELS` 缺省或为空 → `build_dispatcher()` 返回 None。
不配置就一行网络代码都不会执行（这也让测试天然安全）。
"""

from app.notify.base import Notifier, NotifyResult, mask, mask_enabled
from app.notify.dingtalk import DingTalkNotifier, sign_dingtalk
from app.notify.dispatcher import Dispatcher, NotifyOutcome, build_dispatcher
from app.notify.feishu import FeishuNotifier, sign_feishu
from app.notify.webhook import WebhookNotifier

__all__ = [
    # 题目要求的四个对外名字
    "NotifyResult",
    "Dispatcher",
    "build_dispatcher",
    "mask",
    # 调用方还会用到的
    "NotifyOutcome",
    "Notifier",
    "mask_enabled",
    "WebhookNotifier",
    "DingTalkNotifier",
    "FeishuNotifier",
    "sign_dingtalk",
    "sign_feishu",
]
