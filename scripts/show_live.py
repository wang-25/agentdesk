# -*- coding: utf-8 -*-
"""看真机的原始数据 —— 不经过模型，直接调工具层。

【为什么需要单独一个脚本】
平时问 Agent「磁盘还剩多少」，你看到的是**模型转述之后**的答案。
要确认"它到底从机器上读到了什么"，必须把模型摘掉 ——
否则永远分不清哪部分是真实数据、哪部分是模型的表述。

这个脚本就干这件事：直接调 app/tools/ops.py 里的工具函数，
把原始返回原样打出来。不花 token，也没有任何转述。

【它和别的脚本的区别】
    scripts/demo.py        走 HTTP 接口 + 真实调用模型 —— 演示"整条链路通了"
    scripts/smoke_test.py  九层自检 —— 回答"有没有坏"
    本脚本                 直接调工具函数 —— 回答"真机数据长什么样"

用法（在 agentdesk 目录下）：
    .venv\\Scripts\\python.exe scripts\\show_live.py
    .venv\\Scripts\\python.exe scripts\\show_live.py --host web-01
    .venv\\Scripts\\python.exe scripts\\show_live.py --only check_disk,list_containers
    .venv\\Scripts\\python.exe scripts\\show_live.py --service nginx --lines 50
"""

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def hr(char="-", n=66):
    print(char * n)


def main():
    ap = argparse.ArgumentParser(
        description="不经模型，直接看工具层从真机读到的原始数据")
    ap.add_argument("--host", default=None,
                    help="逻辑主机名；不填就用清单里的第一台")
    ap.add_argument("--service", default="docker",
                    help="check_service / tail_log 要查的服务名（默认 docker）")
    ap.add_argument("--lines", type=int, default=15,
                    help="tail_log 读多少行（默认 15）")
    ap.add_argument("--only", default=None,
                    help="只跑指定的工具，逗号分隔，例如 check_disk,list_containers")
    args = ap.parse_args()

    from app.tools.ops import (BACKEND, KNOWN_HOSTS, SSH_TARGETS, TOOLS,
                               execute_tool)

    host = args.host or (KNOWN_HOSTS[0] if KNOWN_HOSTS else "web-01")

    # ---- 先把"这次查的是哪儿"讲清楚 ----
    # ★ 这一步不能省。工具层接的是仿真数据还是真机，
    #   决定了下面每一个数字的性质 —— 不写清楚，看的人会默认"它连的是真机器"。
    hr("=")
    print(f"  工具层后端：{BACKEND}")
    if BACKEND == "ssh":
        t = SSH_TARGETS.get(host) or {}
        print(f"  目标机器  ：{host} → {t.get('user')}@{t.get('host')}:{t.get('port')}")
    elif BACKEND == "local":
        print("  目标机器  ：本机（执行只读命令）")
    else:
        print("  目标机器  ：内置仿真数据 —— 不连接任何真实机器")
    print(f"  主机清单  ：{KNOWN_HOSTS}")
    hr("=")

    plan = [
        ("check_disk", {"host": host}),
        ("check_load", {"host": host}),
        ("list_containers", {"host": host}),
        ("check_service", {"host": host, "service": args.service}),
        ("tail_log", {"host": host, "service": args.service, "lines": args.lines}),
    ]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - set(TOOLS)
        if unknown:
            print(f"  ⚠ 不存在的工具：{sorted(unknown)}；可用：{list(TOOLS)}")
        plan = [p for p in plan if p[0] in wanted]

    ok_count = 0
    for name, tool_args in plan:
        hr()
        print(f"▶ {name}({', '.join(f'{k}={v!r}' for k, v in tool_args.items())})")
        started = time.time()
        out = execute_tool(name, tool_args)
        elapsed = int((time.time() - started) * 1000)

        if out.get("ok"):
            ok_count += 1
            # 原样打印，不做任何加工 —— 这个脚本存在的意义就是"不给转述"
            print(json.dumps(out.get("result"), ensure_ascii=False, indent=2))
        else:
            print(f"  ✗ {out.get('error')}")
        print(f"  （{elapsed} ms，风险等级 {out.get('risk')}）")

    hr("=")
    print(f"  完成 {ok_count}/{len(plan)} 个只读工具")
    if BACKEND == "mock":
        print("  注意：以上全部是仿真数据，不代表任何真实机器。")
        print("  要看真机，把 .env 里的 OPS_BACKEND 改成 ssh 或 local。")
    print("  注：这里只调了只读工具。写操作（run_command）走审批流程，不在本脚本范围内。")
    hr("=")


if __name__ == "__main__":
    main()
