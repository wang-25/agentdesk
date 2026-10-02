# -*- coding: utf-8 -*-
"""出站通知自检 —— 验证"通知到底发得出去吗"。

【为什么需要这个脚本】
通知是个**只会在失败时静默**的东西：配错了 URL、签名算错、机器人被限流、
关键词不匹配 —— 这些全都不会让主流程报错，只会让值班的人"什么都没收到"，
而系统里看起来一切正常。所以必须有一个人能主动去戳一下的工具。

它还专门回答一个最容易骗过人的问题：**HTTP 200 不等于发送成功**。
钉钉会在 200 的响应体里回 `errcode: 310000`（关键词不匹配），
飞书回 `code: 19021`（签名校验失败）—— 这个脚本把那些业务错误码直接打出来。

用法：
    python scripts/notify_check.py              # 按当前配置发一条测试通知
    python scripts/notify_check.py --dry-run    # 只看配置解析结果，一个请求都不发

退出码：0 = 所有渠道都发成功；1 = 有渠道失败或没配通知。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402  （Windows 中文控制台切 UTF-8）

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="出站通知自检")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析配置，不发任何请求")
    parser.add_argument("--text", default="这是一条来自 notify_check 的测试通知。"
                                         "收到即说明渠道配置正确。")
    args = parser.parse_args()

    from app.notify import events
    from app.notify.dispatcher import KNOWN_CHANNELS

    print("=" * 62)
    print("  出站通知自检")
    print("=" * 62)

    d = events.dispatcher()
    info = events.describe()
    print(f"  通知开关：{'已启用' if info['enabled'] else '未启用（完全不出站）'}")
    print(f"  已装配渠道：{info['channels'] or '（无）'}")
    print(f"  出站脱敏：{'开' if info['mask'] else '**关**（内容原样发出）'}")
    print(f"  支持的渠道名：{'、'.join(KNOWN_CHANNELS)}")

    if d is None:
        print()
        print("  没有配置任何渠道 —— 这是**默认行为**，不是故障。")
        print("  要打开，在 .env 里填（至少一个 URL）：")
        print("      NOTIFY_CHANNELS=webhook        # 或 dingtalk / feishu，可多选")
        print("      NOTIFY_WEBHOOK_URL=https://…")
        print("      NOTIFY_DINGTALK_URL=https://oapi.dingtalk.com/robot/send?access_token=…")
        print("      NOTIFY_FEISHU_URL=https://open.feishu.cn/open-apis/bot/v2/hook/…")
        print("  另见 .env.example 的「出站通知」一节与 docs/incident-notify.md。")
        return 1

    if args.dry_run:
        print("\n  --dry-run：配置解析正常，未发送任何请求。")
        return 0

    print("\n  正在发送测试通知……")
    outcome = d.notify("[AgentDesk] 通知自检", args.text,
                       key="notify-check", payload={"source": "notify_check"})

    failed = 0
    for r in outcome.results:
        mark = "✅" if r.ok else "❌"
        line = f"  {mark} {r.channel}：HTTP {r.status}　尝试 {r.attempts} 次"
        if r.error:
            line += f"\n      错误：{r.error}"
        print(line)
        if not r.ok:
            failed += 1

    print()
    print("  参考：通知层的设计取舍见 docs/incident-notify.md")
    print("=" * 62)

    if failed:
        print("  ❌ 有渠道发送失败。常见原因：")
        print("     · 钉钉/飞书机器人开了「加签」但 SECRET 没填（或填反）")
        print("     · 机器人设了自定义关键词，而消息标题里没有它")
        print("     · 机器人被限流（每分钟 20 条）")
        print("     · 服务器出不去外网")
        return 1
    print("  ✅ 所有渠道发送成功。")
    print("  提示：这里成功只代表**这一次**能发出去；")
    print("        长期是否还能发，看 logs/audit.jsonl 里的 notify.failed。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
