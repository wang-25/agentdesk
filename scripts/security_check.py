# -*- coding: utf-8 -*-
"""
公网安全层自检
============================================================
验证 app/security.py 真的拦得住，而不是"写了但没生效"。

鉴权这种东西有个特点：**失效了不会报错**。
不带 token 能调通、限流没拦住，接口返回的都是 200，
从日志上看一切正常 —— 直到账单或事故把你叫醒。
所以必须有这样一个"故意去攻击自己"的脚本。

用法：
    python scripts/security_check.py on     开启鉴权模式，验证拦截是否生效
    python scripts/security_check.py off    关闭鉴权模式，验证本地开发不受影响
    python scripts/security_check.py        两种模式都跑一遍

退出码 0 表示全部通过。
"""

import argparse
import os
import sys
from pathlib import Path

# Windows 控制台：输出流 + 代码页都切 UTF-8（否则中文乱码）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 测试用的固定凭据。放在 import app 之前设置，
# 因为 security.py 在模块加载时就把环境变量读成常量了。
TEST_TOKEN = "self-check-token-2f8a1c9e"

_results = []


def check(name: str, got, want):
    ok = got == want
    _results.append(ok)
    print(f"  [{'通过' if ok else '失败'}] {name:<38} 期望 {want}  实得 {got}")
    return ok


def run(mode: str) -> bool:
    if mode == "on":
        os.environ["AUTH_ENABLED"] = "1"
        os.environ["AGENT_TOKEN"] = TEST_TOKEN
        os.environ["DAILY_QUOTA"] = "3"
        os.environ["RATE_LIMIT_PER_MIN"] = "100"
        os.environ["RATE_LIMIT_GLOBAL_PER_MIN"] = "100"

    from starlette.testclient import TestClient

    from app import security
    from app.main import app

    client = TestClient(app)
    token_header = {"X-API-Key": TEST_TOKEN}

    print(f"\n=== 模式：AUTH_ENABLED={'1' if security.AUTH_ENABLED else '0'} ===")

    if mode == "off":
        check("关闭鉴权时业务接口正常放行", client.get("/audit").status_code, 200)
        check("关闭鉴权时写接口不会被拦",
              client.post("/agent/ask", json={"question": ""}).status_code != 401, True)
        return all(_results)

    print("\n-- 公开路径 --")
    check("GET / 首页可匿名访问", client.get("/").status_code, 200)
    check("GET /health 可匿名访问", client.get("/health").status_code, 200)
    check("GET /docs 可匿名访问", client.get("/docs").status_code, 200)

    print("\n-- token 鉴权 --")
    check("无 token 访问 /audit", client.get("/audit").status_code, 401)
    check("错误 token 访问 /audit",
          client.get("/audit", headers={"X-API-Key": "wrong"}).status_code, 401)
    check("正确 token 访问 /audit", client.get("/audit", headers=token_header).status_code, 200)
    check("Authorization: Bearer 也算数",
          client.get("/traces", headers={"Authorization": f"Bearer {TEST_TOKEN}"}).status_code, 200)
    check("无 token 访问 /traces", client.get("/traces").status_code, 401)
    check("无 token 访问 /metrics/summary", client.get("/metrics/summary").status_code, 401)
    check("无 token 访问 /audit 之外 /approvals", client.get("/approvals").status_code, 401)

    print("\n-- 写操作与花钱接口（最关键的一组）--")
    check("无 token 批准审批单",
          client.post("/approvals/x/approve", json={"approver": "attacker"}).status_code, 401)
    check("无 token 执行审批命令",
          client.post("/approvals/x/execute", json={"executor": "attacker"}).status_code, 401)
    check("无 token 触发告警 webhook", client.post("/webhook/alert", json={}).status_code, 401)
    check("无 token 重建索引", client.post("/rag/index").status_code, 401)

    print("\n-- 限流（每 IP 3 次）--")
    security.PER_IP_PER_MIN = 3
    security._ip_hits.clear()
    security._global_hits.clear()
    codes = [client.get("/audit", headers=token_header).status_code for _ in range(5)]
    check("连打 5 次应出现 429", 429 in codes, True)
    check("第 4 次起被拦", codes[3], 429)

    print("\n-- 全局限流（伪造 IP 也绕不过）--")
    security.PER_IP_PER_MIN = 100
    security.GLOBAL_PER_MIN = 2
    security._ip_hits.clear()
    security._global_hits.clear()
    codes = [client.get("/audit", headers={**token_header, "X-Forwarded-For": f"1.2.3.{i}"}).status_code
             for i in range(5)]
    check("每请求换一个假 IP 仍被拦", 429 in codes, True)

    print("\n-- 每日额度（只扣花钱路径）--")
    security.PER_IP_PER_MIN = 100
    security.GLOBAL_PER_MIN = 100
    security._ip_hits.clear()
    security._global_hits.clear()
    security._daily["date"] = ""
    security._daily["count"] = 0
    client.get("/sandbox", headers=token_header)
    check("查状态这类接口不扣额度", security._daily["count"], 0)
    for _ in range(security.DAILY_QUOTA):
        security._check_quota()
    allowed, remain = security._check_quota()
    check("额度用尽后拒绝", allowed, False)
    check("剩余额度归零", remain, 0)

    print("\n-- fail-closed：开了鉴权却没配 token --")
    saved = security.AGENT_TOKEN
    security.AGENT_TOKEN = ""
    try:
        security.install_security(app)
        check("应拒绝启动", False, True)
    except RuntimeError:
        check("应拒绝启动", True, True)
    finally:
        security.AGENT_TOKEN = saved

    return all(_results)


def main():
    parser = argparse.ArgumentParser(description="AgentDesk 安全层自检")
    parser.add_argument("mode", nargs="?", choices=["on", "off", "both"], default="both",
                        help="on=开启鉴权验证拦截；off=关闭鉴权验证本地不受影响")
    args = parser.parse_args()

    # 【为什么要开子进程跑】
    # AUTH_ENABLED 是 security.py 的模块级常量，import 时就定死了。
    # 在同一个进程里先跑 on 再跑 off，第二次拿到的还是 on 的配置，
    # 测出来的是假象。每个模式必须是一个干净的进程。
    if args.mode == "both":
        import subprocess
        rc = 0
        for m in ("on", "off"):
            rc |= subprocess.call([sys.executable, __file__, m],
                                  cwd=str(PROJECT_ROOT))
        sys.exit(rc)

    ok = run(args.mode)
    total = len(_results)
    if ok:
        print(f"\n全部通过：{total} 项")
        sys.exit(0)
    print(f"\n失败：{_results.count(False)} / {total} 项未通过")
    sys.exit(1)


if __name__ == "__main__":
    main()
