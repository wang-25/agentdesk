# -*- coding: utf-8 -*-
"""
意图路由 Agent 的准确率评测
============================================================
这是**多 Agent 拆分最直接的收益兑现**：
拆出一个只做分类、不调工具的 Agent 之后，它可以被**单独**打分。

    「一个什么都会的 Agent」
        → 只能端到端看"回答得好不好"，出了错也不知道错在哪一步

    「意图路由 Agent」
        → 有标准答案，逐字段算准确率，错了能一眼看出错在哪个字段

【为什么这件事值钱】
多 Agent 的常见批评是"为了拆而拆，还多花了几次调用"。
要回应这个批评，唯一的办法是**拿出数字**：
拆出来之后，我能说清"意图分类的准确率是 X%，其中 hosts 字段最容易错"——
而集中式的做法说不出这句话。

【逐字段算准确率，而不是只算"全对率"】
"全对"这个指标太粗：一个用例可能 5 个字段里对 4 个，
全对率会把它和"全错"一视同仁。而逐字段看，你才知道
**该优化哪个字段**（比如 hosts 老错 → 该在提示词里补例子）。

用法：
    python -m scripts.eval_specialists                # 全量 20 条
    python -m scripts.eval_specialists --limit 5      # 只跑前 5 条（先试水）
    python -m scripts.eval_specialists --difficulty tricky   # 只看易错的那批
"""

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台：输出流 + 代码页都切 UTF-8（否则中文乱码）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402

from app.agents.specialists import route_intent    # noqa: E402

SET_PATH = PROJECT_ROOT / "eval" / "intent_set.json"
REPORT_DIR = PROJECT_ROOT / "eval" / "reports"

# 参与评分的字段。顺序就是报告里的列顺序。
FIELDS = ["task_type", "hosts", "services", "needs_live_data",
          "needs_knowledge"]


def _fmt_value(v) -> str:
    """把期望值/实际值渲染成报告里好读的样子。

    json.dumps 会把字符串也带上引号（`"query"`），
    在表格里读起来噪音大；这里只在需要消歧时才加结构符号。
    """
    if v is None:
        return "null"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def compare(case: dict, got: dict) -> dict:
    """逐字段比对。返回 {字段: True/False}。

    【数组字段怎么比才公平】
    hosts / services 是数组，用**集合相等**而不是顺序相等 ——
    ["web-01","db-01"] 和 ["db-01","web-01"] 是同一个意思。
    但如果模型多返回了"未提及的服务"，集合相等会判错，这是对的 ——
    **宁可比得严一点，也不要放过"自己加戏"的行为。**

    【多解字段】
    部分用例的某些字段有多种合理答案（写在 case["accept"] 里）。
    这种必须显式声明，不能靠"把标准答案改成模型给的"来让指标好看 ——
    那叫改测试，不叫改代码。
    """
    accept = case.get("accept") or {}
    expect = case["expect"]
    result = {}
    for field in FIELDS:
        if field not in expect:
            continue          # 这条用例没标这个字段，跳过不计分
        want = expect[field]
        have = got.get(field)

        # ★ 有些字段有**多种合理答案**（比如"当前负载高吗"可以是 query 也可以是 diagnose）。
        #   对这种用例，硬钉一个标准答案然后判模型错，是在制造假指标 ——
        #   你测的不是模型准不准，是你自己的判断偏好。
        #   所以显式声明 accept 列表：落在这个范围内的都算对。
        if field in accept:
            result[field] = have in accept[field]
            continue

        if isinstance(want, list):
            result[field] = set(want) == set(have or [])
        else:
            result[field] = want == have
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="意图路由 Agent 准确率评测")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--difficulty", default="",
                        help="只跑某个难度：basic / edge / tricky")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)

    data = json.loads(SET_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.difficulty:
        cases = [c for c in cases if c.get("difficulty") == args.difficulty]
    if args.limit:
        cases = cases[:args.limit]

    print("=" * 62)
    print("  意图路由 Agent 准确率评测")
    print(f"  用例 {len(cases)} 条"
          + (f"（难度筛选：{args.difficulty}）" if args.difficulty else ""))
    print("=" * 62)

    field_hits = {f: 0 for f in FIELDS}
    field_total = {f: 0 for f in FIELDS}
    rows = []
    per_difficulty = {}

    for i, case in enumerate(cases, 1):
        t0 = time.time()
        result = route_intent(case["question"])
        got = result["intent"]
        elapsed = int((time.time() - t0) * 1000)

        marks = compare(case, got)
        for f, ok in marks.items():
            field_total[f] += 1
            field_hits[f] += int(ok)

        all_ok = all(marks.values())
        diff = case.get("difficulty", "?")
        agg = per_difficulty.setdefault(diff, [0, 0])
        agg[0] += int(all_ok)
        agg[1] += 1

        wrong = [f for f, ok in marks.items() if not ok]
        rows.append({
            "id": case["id"], "question": case["question"],
            "difficulty": diff, "all_ok": all_ok, "wrong": wrong,
            "marks": marks, "got": got,
            # ★ 报告要打印"期望"，所以这里必须把期望值一起带上。
            #   原先没带，报告只能用 marks（布尔判定）凑第二列 —— 于是列就错了。
            "expect": case["expect"], "accept": case.get("accept") or {},
            "attempts": result["attempts"], "problems": result["problems"],
            "elapsed_ms": elapsed,
        })

        flag = "✅" if all_ok else "❌"
        tail = "" if all_ok else f"　错在：{', '.join(wrong)}"
        print(f"  {flag} {case['id']}　{case['question'][:34]}{tail}")
        if not all_ok:
            for f in wrong:
                print(f"        {f}: 期望 {case['expect'].get(f)!r} "
                      f"→ 实际 {got.get(f)!r}")
        if result["problems"]:
            print(f"        校验提示：{result['problems']}")

    # ---------------- 汇总 ----------------
    total_all_ok = sum(1 for r in rows if r["all_ok"])
    print("\n" + "=" * 62)
    print("  字段级准确率")
    print("=" * 62)
    for f in FIELDS:
        if not field_total[f]:
            continue
        rate = field_hits[f] / field_total[f]
        bar = "█" * int(rate * 24) + "░" * (24 - int(rate * 24))
        print(f"  {f:18s} {bar} {rate * 100:5.1f}%  "
              f"({field_hits[f]}/{field_total[f]})")

    print(f"\n  全字段全对率：{total_all_ok}/{len(rows)} = "
          f"{total_all_ok / len(rows) * 100:.1f}%")
    if per_difficulty:
        print("\n  分难度：")
        for d in ("basic", "edge", "tricky"):
            if d in per_difficulty:
                ok, n = per_difficulty[d]
                print(f"    {d:8s} {ok}/{n}")

    # 重试次数 —— 直接反映提示词写得清不清楚
    retried = [r for r in rows if r["attempts"] > 1]
    print(f"\n  需要二次修正的用例：{len(retried)}/{len(rows)}"
          + (f"（{'、'.join(r['id'] for r in retried)}）" if retried else ""))
    avg_ms = sum(r["elapsed_ms"] for r in rows) // max(len(rows), 1)
    print(f"  平均耗时：{avg_ms}ms/条")
    print("=" * 62)

    if not args.no_save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"intent_eval_{time.strftime('%Y%m%d_%H%M%S')}.md"
        path.write_text(build_report(rows, field_hits, field_total,
                                     per_difficulty), encoding="utf-8")
        print(f"\n  报告已保存：{path}")

    # 有字段全对率低于 80% 就返回非 0 —— 能接进自动化
    worst = min((field_hits[f] / field_total[f]
                 for f in FIELDS if field_total[f]), default=1.0)
    return 0 if worst >= 0.8 else 1


def build_report(rows, field_hits, field_total, per_difficulty) -> str:
    lines = [
        "# 意图路由 Agent 准确率评测报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例数：{len(rows)}",
        f"- 评测对象：`app/agents/specialists.py` 的 `route_intent()`",
        f"- 模型：DeepSeek，temperature=0",
        "",
        "## 一、字段级准确率",
        "",
        "| 字段 | 准确率 | 对了/总数 |",
        "|---|---|---|",
    ]
    for f in FIELDS:
        if not field_total[f]:
            continue
        lines.append(f"| {f} | {field_hits[f] / field_total[f] * 100:.1f}% | "
                     f"{field_hits[f]}/{field_total[f]} |")
    total_all_ok = sum(1 for r in rows if r["all_ok"])
    lines += [
        "",
        f"**全字段全对率：{total_all_ok}/{len(rows)}**",
        "",
        "## 二、逐条结果",
        "",
        "| ID | 难度 | 问题 | 结果 | 错在 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['id']} | {r['difficulty']} | "
                     f"{r['question'][:30]} | "
                     f"{'✅' if r['all_ok'] else '❌'} | "
                     f"{'、'.join(r['wrong']) or '—'} |")

    lines += ["", "## 三、错例详情", ""]
    lines += ["> 「期望」列里出现 `[a, b]` 表示该字段**声明了多个合理答案**"
              "（用例里的 `accept`），落在这个范围内的都算对 ——"
              "这类用例硬钉单一标准答案会制造假指标。", ""]
    bad = [r for r in rows if not r["all_ok"]]
    if not bad:
        lines.append("全部通过。")
    for r in bad:
        lines.append(f"### {r['id']}｜{r['difficulty']}")
        lines.append("")
        lines.append(f"**问题**：{r['question']}")
        lines.append("")
        lines.append("| 字段 | 期望 | 实际 |")
        lines.append("|---|---|---|")
        for f in r["wrong"]:
            # ★ 两列原先写反了：第一列填的是 got（实际值），第二列填的是
            #   marks[f]（布尔判定）。读者看到的是「期望 False」这种毫无意义的表，
            #   而真正该看的"模型答成了什么"被塞进了标题写着「期望」的那一列。
            #   现在按表头顺序填：期望 = 用例声明的答案，实际 = 模型给出的值。
            if f in r["accept"]:
                exp = r["accept"][f]          # 该字段声明了多个合理答案
            else:
                exp = r["expect"].get(f)
            lines.append(f"| `{f}` | `{_fmt_value(exp)}` | "
                         f"`{_fmt_value(r['got'].get(f))}` |")
        lines.append("")
        lines.append(f"模型给出的实际标签：`{json.dumps(r['got'], ensure_ascii=False)}`")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
