# -*- coding: utf-8 -*-
"""现场演示：证明服务真的能用（真实调用模型）。"""
import sys
import time
from pathlib import Path

import httpx

# Windows 控制台：输出流 + 代码页都切 UTF-8（否则中文乱码）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402

B = "http://127.0.0.1:8000"


def line(t=""):
    print(t, flush=True)


line("=" * 66)
line("  AgentDesk 现场演示")
line("=" * 66)

# ---- 1. 版本一致性 ----
h = httpx.get(f"{B}/health", timeout=10).json()
o = httpx.get(f"{B}/openapi.json", timeout=15).json()
line(f"\n[1] 服务在线　版本 {h['version']}（openapi 报 {o['info']['version']}）"
     f"　接口 {len(o['paths'])} 个")

# ---- 2. 沙箱与工具 ----
sb = httpx.get(f"{B}/sandbox", timeout=15).json()
p = sb["policy"]
line(f"[2] 沙箱后端 {sb['backend']}（真隔离={sb['isolated']}，fail-closed={sb['fail_closed']}）")
line(f"    白名单 {p['whitelisted']} 条（直通 {p['allow']} / 需审批 {p['needs_approval']}）"
     f"　高危拦截 {p['blocked_binaries']} 个二进制")

tl = httpx.get(f"{B}/agent/tools", timeout=15).json()
line(f"[3] Agent 可调用工具 {tl['count']} 个："
     + "、".join(t["name"] for t in tl["tools"]))

# ---- 3. 真实诊断（多 Agent 编排）----
line("\n" + "-" * 66)
line("[4] 真实诊断：让它自己决定查什么")
line("-" * 66)
q = "web-01 上的网站访问很慢，有时报 502，帮我看下原因"
line(f"    提问：{q}")
t0 = time.time()
r = httpx.post(f"{B}/agent/ask",
               json={"question": q, "engine": "supervisor", "max_retries": 1},
               timeout=300).json()
el = time.time() - t0
orc = r.get("orchestration") or {}
m = r.get("metrics") or {}
line(f"\n    路径：{' → '.join(orc.get('path') or [])}")
line(f"    指标：{m.get('rounds')} 轮 · {m.get('tool_calls')} 次工具调用 · "
     f"{m.get('tokens')} token · {el:.1f}s")
line(f"    意图：{orc.get('intent', {}).get('task_type')} · "
     f"主机 {orc.get('intent', {}).get('hosts')}")
v = orc.get("verdict") or {}
line(f"    校验：{'通过' if v.get('pass') else '未通过'}（{v.get('source')}）")
tr = r.get("trace") or []
line(f"    实际调用的工具：{[(s.get('step'), s.get('tool')) for s in tr]}")
line("\n    --- 回答前 300 字 ---")
line("    " + (r.get("answer") or "")[:300].replace("\n", "\n    "))

# ---- 4. 知识边界（库外问题该拒答）----
line("\n" + "-" * 66)
line("[5] 知识边界：问一个知识库里根本没有的问题")
line("-" * 66)
q2 = "Kafka 消费组 lag 一直涨，怎么排查？"
line(f"    提问：{q2}")
r2 = httpx.post(f"{B}/agent/ask",
                json={"question": q2, "engine": "supervisor", "max_retries": 0},
                timeout=300).json()
line("\n    --- 回答 ---")
line("    " + (r2.get("answer") or "")[:260].replace("\n", "\n    "))

# ---- 5. 成本看板 ----
line("\n" + "-" * 66)
line("[6] 成本看板（刚才这两次调用留下了数据）")
line("-" * 66)
c = httpx.get(f"{B}/metrics/summary", timeout=20).json()
line(f"    运行 {c['runs']} 次 · 总成本 ¥{c['cost_cny']} · "
     f"缓存命中率 {c['prompt_cache_hit_rate']:.0%} · P95 {c['elapsed_ms']['p95']}ms")
for name, b in list(c["by_span_name"].items())[:5]:
    line(f"      {name:12s} {b['calls']:>2d} 次  {b['tokens']:>6d} tok  ¥{b['cost_cny']:.6f}")

line("\n" + "=" * 66)
line("  ✅ 演示结束 —— 以上全部是本机真实运行结果")
line("=" * 66)
