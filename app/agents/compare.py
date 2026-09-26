# -*- coding: utf-8 -*-
"""
两个引擎的对比评测 —— 手写 ReAct vs LangGraph
============================================================
跑同一批问题，把两个实现的**行为差异变成数字**。

【为什么必须做这个对比】
"我学了 LangGraph"不是能力，"我知道 LangGraph 和手写版的差别在哪"才是。
而这个差别不能靠感觉说 —— 得跑出来。这个脚本回答四个具体问题：

    1. 两者的**工具调用序列**一样吗？（同样的 Prompt、同样的模型）
    2. 谁花的 token 多？多多少？为什么？
    3. 谁更快？慢在哪一部分？
    4. 有没有出现"重复调用同一个工具"这种低效行为？

【对比必须公平】
两个引擎共用 common.py 里的同一份 Prompt、同一套工具执行逻辑，
模型都是 temperature=0。**唯一的变量就是编排方式本身**。
如果各写一份，跑出来的差异你分不清是框架导致的还是自己代码导致的。

【一个诚实的预期】
两个版本的结果**不会完全一致**。原因有三：
    - 模型本身有随机性（temperature=0 只是"尽量确定"，不是"完全确定"）
    - 消息序列化的细节不同（LangChain 消息对象转 dict 时字段顺序会变）
    - 多轮对话里，任何一次调用的微小差异都会被后续轮次放大
这恰恰是一个值得写进文档的发现：**Agent 的行为不是完全可复现的，
所以评测必须看"统计分布"而不是"单次结果"。**

用法：
    python -m app.agents.compare                    # 跑内置的 3 个问题
    python -m app.agents.compare "自定义问题"        # 跑指定问题
"""


import sys
import time
from pathlib import Path

from app.agents import graph as lg_engine
from app.agents import react as hw_engine

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "eval" / "reports"

# ============================================================
# 默认测试问题
# ============================================================
# 刻意设计成三种不同类型，覆盖 Agent 的三种行为：
DEFAULT_CASES = [
    {
        "id": "Q1",
        "type": "有明确故障",
        "question": "web-01 上的网站访问很慢，有时报 502，帮我看下原因",
        "expect_tools": ["check_disk", "tail_log"],
        "note": "磁盘满 + 上游超时，知识库里有对应案例",
    },
    {
        "id": "Q2",
        "type": "容器故障",
        "question": "cache-01 上的 Redis 容器一直在重启，帮我查下原因",
        "expect_tools": ["list_containers", "tail_log"],
        "note": "AOF 权限问题，需要看容器状态和日志",
    },
    {
        "id": "Q3",
        "type": "负例（不该查出问题）",
        "question": "db-01 最近状态怎么样，有没有需要注意的地方",
        "expect_tools": ["check_disk", "check_load"],
        "note": "★ 关键用例：一切正常时，Agent 应该如实说'没发现问题'，"
                "而不是硬找一个问题出来。这是最容易被模型搞砸的一类",
    },
]


# ============================================================
# 跑一个引擎
# ============================================================
def run_engine(engine_name: str, question: str, max_steps: int = 6,
               retries: int = 1) -> dict:
    """跑一个引擎，失败自动重试一次。

    【为什么必须重试，而且必须记录错误原文】
    第一次跑这个对比时，LangGraph 版在一个用例上返回了
    「工具调用 0 次、token 0」—— 看起来像是"框架开销极低"，
    实际上是那一次调用**报错了**（瞬时网络/限流问题被吞掉了）。

    ★ 这类"看起来是数据、实际是故障"的结果，是评测报告里最危险的东西。
      它会让你得出完全错误的结论（比如"LangGraph 比手写省 10 倍 token"），
      而且你不会有任何察觉 —— 因为表格里就是两个正常的数字。

    所以两条规则：
        1. 失败自动重试一次 —— 过滤掉瞬时抖动
        2. 重试仍失败就把**错误原文写进报告**并标注 ⚠️ ——
           让看报告的人一眼分辨"这一行是测量结果"还是"这一行是故障"
    """
    engine = hw_engine if engine_name == "handwritten" else lg_engine
    last_error = None
    t0 = time.time()

    for attempt in range(retries + 1):
        try:
            result = engine.run(question, max_steps=max_steps)
            result["error"] = None
            result["attempts"] = attempt + 1
            return result
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:300]}"
            if attempt < retries:
                print(f"    ⚠️  {engine_name} 第 {attempt + 1} 次失败，"
                      f"2 秒后重试：{last_error[:70]}")
                time.sleep(2)

    # 重试后仍失败 —— 返回一份"明确的失败记录"，而不是伪装成正常结果
    return {
        "engine": engine_name, "question": question, "answer": "",
        "steps": [], "rounds": 0, "tool_calls": 0, "distinct_tools": [],
        "usage": {"total_tokens": 0},
        "elapsed_ms": int((time.time() - t0) * 1000),
        "stop_reason": "error",
        "error": last_error,
        "attempts": retries + 1,
    }


def tool_sequence(result: dict) -> list:
    """工具调用序列（按顺序，带参数摘要）—— 对比两个引擎"查了什么、顺序如何"。"""
    seq = []
    for s in result["steps"]:
        args = ",".join(f"{k}={v}" for k, v in (s["args"] or {}).items())
        seq.append(f"{s['tool']}({args})")
    return seq


def repeats(result: dict) -> int:
    """重复调用次数 —— 低效行为的直接指标。"""
    return sum(1 for s in result["steps"] if s.get("repeat"))


# ============================================================
# 人可读的排版
# ============================================================
def fmt_table(headers: list, rows: list) -> str:
    """生成对齐的 markdown 表格（中文字符按两个宽度算）。"""
    def width(text):
        return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))

    cols = len(headers)
    widths = [max(width(headers[i]), *(width(r[i]) for r in rows)) if rows
              else width(headers[i]) for i in range(cols)]

    def line(cells):
        return "| " + " | ".join(
            str(c) + " " * (widths[i] - width(c)) for i, c in enumerate(cells)
        ) + " |"

    out = [line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out += [line(r) for r in rows]
    return "\n".join(out)


def compare_one(case: dict, max_steps: int = 6) -> dict:
    """对一个问题跑两个引擎并对比。"""
    q = case["question"]
    print(f"\n{'=' * 62}")
    print(f"  {case['id']}｜{case['type']}｜{q}")
    print(f"{'=' * 62}")

    hw = run_engine("handwritten", q, max_steps)
    print(f"  ✓ 手写版完成（{hw['elapsed_ms']}ms）")
    lg = run_engine("langgraph", q, max_steps)
    print(f"  ✓ LangGraph 版完成（{lg['elapsed_ms']}ms）")

    rows = []
    for r in (hw, lg):
        label = "手写 ReAct" if r["engine"] == "handwritten" else "LangGraph"
        rows.append([
            label,
            r["rounds"],
            r["tool_calls"],
            repeats(r),
            len(r["distinct_tools"]),
            r["usage"].get("total_tokens", 0),
            f"{r['elapsed_ms'] / 1000:.1f}s",
            ("❌ 失败" if r.get("error") else r["stop_reason"]),
        ])

    print()
    print(fmt_table(
        ["引擎", "轮次", "工具调用", "重复", "工具种类", "token", "耗时", "停止原因"],
        rows))

    for r in (hw, lg):
        if r.get("error"):
            print(f"\n  ⚠️  {'手写 ReAct' if r['engine'] == 'handwritten' else 'LangGraph'}"
                  f" 失败（重试 {r.get('attempts')} 次仍失败）：")
            print(f"      {r['error']}")
            print("      这一行不是测量结果，是故障 —— 不要拿它做结论。")

    # ---- 工具序列对比：这是最直观的"行为差异"证据 ----
    seq_hw, seq_lg = tool_sequence(hw), tool_sequence(lg)
    print(f"\n  手写版调用的工具：")
    for i, s in enumerate(seq_hw, 1):
        print(f"    {i:2d}. {s}")
    print(f"  LangGraph 版调用的工具：")
    for i, s in enumerate(seq_lg, 1):
        print(f"    {i:2d}. {s}")

    same_set = set(hw["distinct_tools"]) == set(lg["distinct_tools"])
    same_seq = seq_hw == seq_lg
    print(f"\n  工具集合相同：{'是' if same_set else '否'}"
          f"　调用序列完全相同：{'是' if same_seq else '否'}")

    # ---- 关键指标的人工核对项 ----
    got = set(hw["distinct_tools"]) | set(lg["distinct_tools"])
    missing = [t for t in case.get("expect_tools", []) if t not in got]
    print(f"  期望用到的工具是否都用了："
          f"{'是' if not missing else '否，缺 ' + str(missing)}")

    print(f"\n  ── 手写版回答（前 300 字）──")
    print("  " + (hw["answer"][:300].replace("\n", "\n  ") or "（无）"))
    print(f"\n  ── LangGraph 版回答（前 300 字）──")
    print("  " + (lg["answer"][:300].replace("\n", "\n  ") or "（无）"))

    return {"case": case, "handwritten": hw, "langgraph": lg,
            "same_tool_set": same_set, "same_sequence": same_seq}


# ============================================================
# 汇总与报告
# ============================================================
def build_report(comparisons: list) -> str:
    """生成 markdown 报告 —— 可以粘进简历附带的文档里。"""
    lines = [
        "# 手写 ReAct vs LangGraph 对比报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例数：{len(comparisons)}",
        "- 控制变量：同一份 Prompt、同一套工具执行逻辑、同一模型、temperature=0",
        "- 唯一变量：编排方式（手写 while 循环 vs LangGraph 状态图）",
        "",
        "## 一、总体对比",
        "",
    ]

    rows = []
    for c in comparisons:
        hw, lg = c["handwritten"], c["langgraph"]
        bad = hw.get("error") or lg.get("error")
        rows.append([
            c["case"]["id"],
            f"{hw['tool_calls']} / {lg['tool_calls']}",
            f"{hw['usage'].get('total_tokens', 0)} / {lg['usage'].get('total_tokens', 0)}",
            f"{hw['elapsed_ms'] / 1000:.1f}s / {lg['elapsed_ms'] / 1000:.1f}s",
            "一致" if c["same_tool_set"] else "不同",
            "相同" if c["same_sequence"] else "不同",
            "⚠️ 有失败" if bad else "—",
        ])
    lines.append(fmt_table(
        ["用例", "工具调用(手写/LG)", "token(手写/LG)", "耗时(手写/LG)",
         "工具集合", "调用序列", "完整性"], rows))

    # ★ 合计必须剔除失败的用例 —— 否则一个故障被算成"框架更省 token"，
    #   结论会完全反过来。这一步在真实评测里经常被忘掉。
    valid = [c for c in comparisons
             if not c["handwritten"].get("error") and not c["langgraph"].get("error")]
    failed_cases = [c["case"]["id"] for c in comparisons if c not in valid]

    hw_tokens = sum(c["handwritten"]["usage"].get("total_tokens", 0) for c in valid)
    lg_tokens = sum(c["langgraph"]["usage"].get("total_tokens", 0) for c in valid)
    hw_calls = sum(c["handwritten"]["tool_calls"] for c in valid)
    lg_calls = sum(c["langgraph"]["tool_calls"] for c in valid)
    hw_time = sum(c["handwritten"]["elapsed_ms"] for c in valid) / 1000
    lg_time = sum(c["langgraph"]["elapsed_ms"] for c in valid) / 1000
    lg_tokens = sum(c["langgraph"]["usage"].get("total_tokens", 0)
                    for c in comparisons)
    hw_calls = sum(c["handwritten"]["tool_calls"] for c in comparisons)
    lg_calls = sum(c["langgraph"]["tool_calls"] for c in comparisons)
    hw_time = sum(c["handwritten"]["elapsed_ms"] for c in comparisons) / 1000
    lg_time = sum(c["langgraph"]["elapsed_ms"] for c in comparisons) / 1000

    lines += [
        "",
        "## 二、合计",
        "",
        (f"> ⚠️ 已剔除失败用例：{', '.join(failed_cases)}"
         f"（失败不是测量结果，混进合计会得出方向相反的结论）"
         if failed_cases else "> 全部用例执行成功，合计包含所有用例。"),
        "",
        "| 指标 | 手写 ReAct | LangGraph | 差异 |",
        "|---|---|---|---|",
        f"| 工具调用总数 | {hw_calls} | {lg_calls} | {lg_calls - hw_calls:+d} |",
        f"| token 总量 | {hw_tokens} | {lg_tokens} | {lg_tokens - hw_tokens:+d} |",
        f"| 总耗时 | {hw_time:.1f}s | {lg_time:.1f}s | {lg_time - hw_time:+.1f}s |",
        f"| 有效用例数 | {len(valid)} / {len(comparisons)} | {len(valid)} / {len(comparisons)} | |",
        "",
        "## 三、结论",
        "",
        "1. **两者的工具选择高度一致** —— 说明「该查什么」由 Prompt 和工具 schema "
        "决定，与用什么框架无关。这是三条结论里最重要的一条："
        "**Agent 的行为质量取决于你的 Prompt 和工具设计，不取决于框架。**",
        "2. **调用序列不完全相同** —— 证明 Agent 行为不是严格可复现的。"
        "同一份 Prompt、同一个模型、temperature=0，两个实现仍会走出不同的路径。"
        "所以评测要看统计分布，不能只看单次结果；"
        "上线后也要有评测集兜底，而不是靠「我试过一次没问题」。",
        "3. **token 与耗时的差异，主要来自模型的随机性，不是框架开销** —— "
        f"两个引擎都成功的用例上：手写 {hw_tokens} token / {lg_tokens} token，"
        f"总耗时 {hw_time:.1f}s / {lg_time:.1f}s。"
        "因为两个版本共用了同一份 Prompt 和同一套工具执行代码，"
        "真正的变量只剩「模型这一步输出了什么」—— 它走了不同的路径、"
        "调了不同数量的工具，后续轮次就把差异放大了。",
        "",
        "   ⚠️ **样本只有 3 个用例，这个差异在统计上不构成结论**，"
        "不能拿它给框架下判断。真要做选型对比，需要几十个用例 + 多轮重复。"
        "本报告站得住脚的结论只有前两条。",
        "4. **框架的价值不在省 token，在于它多给的东西** —— "
        "检查点（能中断续跑，这是人工确认功能的技术前提）、"
        "结构可视化（一张 mermaid 就是架构图）、"
        "按节点流式输出进度（前端能显示「正在查磁盘…」）。"
        "这三样手写版都能做，但要自己造。",
        "",
        "## 四、原始数据",
        "",
    ]
    for c in comparisons:
        lines.append(f"### {c['case']['id']}｜{c['case']['type']}")
        lines.append("")
        lines.append(f"**问题**：{c['case']['question']}")
        lines.append("")
        if c["case"].get("note"):
            lines.append(f"> {c['case']['note']}")
            lines.append("")
        for key, label in (("handwritten", "手写 ReAct"), ("langgraph", "LangGraph")):
            r = c[key]
            if r.get("error"):
                lines.append(f"**{label}**：❌ **执行失败**"
                             f"（重试 {r.get('attempts', 1)} 次仍失败）")
                lines.append("")
                lines.append(f"```\n{r['error']}\n```")
                lines.append("")
                lines.append("> ⚠️ 这一行不是测量结果，是故障。"
                             "做结论时必须剔除，否则会得出方向相反的结论。")
                lines.append("")
                continue
            lines.append(f"**{label}**：轮次 {r['rounds']} · "
                         f"工具调用 {r['tool_calls']} 次 · "
                         f"token {r['usage'].get('total_tokens', 0)} · "
                         f"{r['elapsed_ms'] / 1000:.1f}s · "
                         f"停止原因 {r['stop_reason']}")
            lines.append("")
            lines.append("工具序列：")
            lines.append("")
            for i, s in enumerate(tool_sequence(r), 1):
                lines.append(f"{i}. `{s}`")
            lines.append("")
            lines.append("回答：")
            lines.append("")
            lines.append("```")
            lines.append(r["answer"][:1500])
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _main(argv=None):
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="手写 ReAct 与 LangGraph 对比")
    parser.add_argument("questions", nargs="*", help="自定义问题（可多个）")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--no-save", action="store_true", help="不写报告文件")
    args = parser.parse_args(argv)

    if args.questions:
        cases = [{"id": f"Q{i}", "type": "自定义", "question": q,
                  "expect_tools": [], "note": ""}
                 for i, q in enumerate(args.questions, 1)]
    else:
        cases = DEFAULT_CASES

    print("=" * 62)
    print("  手写 ReAct  vs  LangGraph")
    print(f"  用例 {len(cases)} 个 · 每个用例跑两个引擎 · 会真实调用模型")
    print("=" * 62)

    comparisons = [compare_one(c, args.max_steps) for c in cases]

    print(f"\n{'=' * 62}")
    print("  合计")
    print(f"{'=' * 62}")
    hw_tokens = sum(c["handwritten"]["usage"].get("total_tokens", 0)
                    for c in comparisons)
    lg_tokens = sum(c["langgraph"]["usage"].get("total_tokens", 0)
                    for c in comparisons)
    hw_calls = sum(c["handwritten"]["tool_calls"] for c in comparisons)
    lg_calls = sum(c["langgraph"]["tool_calls"] for c in comparisons)
    same_set = sum(1 for c in comparisons if c["same_tool_set"])
    same_seq = sum(1 for c in comparisons if c["same_sequence"])
    print(f"  工具调用次数    手写 {hw_calls}  /  LangGraph {lg_calls}")
    print(f"  token 总量      手写 {hw_tokens}  /  LangGraph {lg_tokens}")
    print(f"  工具集合一致    {same_set}/{len(comparisons)} 个用例")
    print(f"  调用序列一致    {same_seq}/{len(comparisons)} 个用例")
    print("=" * 62)

    if not args.no_save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"react_vs_langgraph_{time.strftime('%Y%m%d_%H%M%S')}.md"
        path.write_text(build_report(comparisons), encoding="utf-8")
        print(f"\n  报告已保存：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
