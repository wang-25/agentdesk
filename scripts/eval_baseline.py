# -*- coding: utf-8 -*-
"""检索基线门禁 —— 指标退化了就让 CI 变红。

【为什么需要它】
`scripts/run_eval.py` 每跑一次就生成一份带时间戳的新报告（`eval/reports/*.md`），
但**没有任何东西会去比对上一份**。指标掉了 10 个百分点，仓库里也只是多了一个
没人看的文件 —— 那跟"没有评测"差别不大。

这个脚本只做一件事：拿 `eval/baseline.json` 里记下的基线跟**现在**跑出来的数字比，
掉超过容差就 `exit 1`（可以直接当 CI 门禁）。

【基线为什么要跟"向量后端"绑在一起】
召回率高度依赖向量后端：真 embedding 与 `local-hash` 词袋兜底不是一个量级。
所以基线里连 `embedder` 的描述一起记下来；当**当前后端与基线不一致**时，
脚本只警告、不判失败 —— 换了后端本来就该重新标定基线（`--update`）。
拿真 embedding 的成绩去卡兜底后端，只会得到一个永远红的门禁，
而永远红的门禁等于没有门禁。

【零成本】
纯检索，不调用任何模型（只有 `local` 或已配 Key 的 embedding 后端在索引期会用网络；
CI 里是 `local-hash`，全程离线）。

用法：
    .venv\\Scripts\\python.exe scripts\\eval_baseline.py             # 比对（CI 用）
    .venv\\Scripts\\python.exe scripts\\eval_baseline.py --update    # 重新标定基线
    .venv\\Scripts\\python.exe scripts\\eval_baseline.py --top-k 8
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402  （Windows 中文控制台的 UTF-8 切换）

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BASELINE_PATH = PROJECT_ROOT / "eval" / "baseline.json"
DEFAULT_TOLERANCE = 0.05      # 5 个百分点。先宽松，避免噪声造成假红
# 延迟的容差是**相对值**（允许变慢 50%），不是百分点 ——
# 拿"5 个百分点"去卡延迟没有意义：它是毫秒，不是 0~1 的比例。
# 50% 也是刻意宽松的：Windows 上亚毫秒级计时的抖动本来就大，
# 门禁要抓的是"慢了一倍"这种量级的退化，不是 3% 的噪声。
LATENCY_TOLERANCE = 0.5
# ★ 测量下限：**两边都低于这个值时，延迟不判失败，只打印**。
#
#   为什么必须有它：实测同一份代码连跑两次，`vector.latency_p95_ms` 从 0.24ms
#   变成 0.45ms（+84%）—— 亚毫秒级的数字在这台机器上主要由计时器精度与
#   调度抖动决定，而不是由代码决定。拿它当门禁，CI 会随机变红，
#   然后所有人学会无视这个门禁（"那只会让人学会无视门禁"是项目自己写下的原则）。
#
#   但只要**有一边**超过下限（例如从 0.2ms 变成 10ms），那就是真实退化，照判。
#   索引变大以后（几万块 → 几十毫秒）这个门禁自然开始起作用。
LATENCY_FLOOR_MS = 5.0

MODES = ("vector", "bm25", "hybrid")


def _is_latency(name: str) -> bool:
    return name.endswith("_ms")


def collect(top_k: int) -> dict:
    """跑一遍检索评测，返回 {指标名: 数值} 与后端描述。"""
    from app.rag import pipeline

    report = pipeline.evaluate(top_k=top_k, modes=MODES, verbose=True)

    metrics = {}
    for mode, data in report["modes"].items():
        metrics[f"{mode}.recall"] = data["recall"]
        for qtype, t in (data.get("by_type") or {}).items():
            metrics[f"{mode}.recall.{qtype}"] = t["recall"]
        # ★ 延迟也进门禁。没有它，"某次改动让每次检索慢了一倍"只能靠人感觉 ——
        #   而检索是**每次问答都要走**的路径，慢一倍会被放大到所有回答上。
        lat = data.get("latency_ms") or {}
        if lat:
            metrics[f"{mode}.latency_p50_ms"] = lat.get("p50", 0.0)
            metrics[f"{mode}.latency_p95_ms"] = lat.get("p95", 0.0)

    return {
        "top_k": top_k,
        "total_questions": report.get("total"),
        "embedder": report.get("embedder"),
        "metrics": metrics,
    }


def write_baseline(current: dict) -> None:
    payload = {
        "_comment": (
            "检索基线。由 scripts/eval_baseline.py --update 生成。"
            "embedder 变了（例如从 local-hash 换成真 embedding）就该重新标定，"
            "否则门禁会拿旧后端的成绩去卡新后端。"
        ),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tolerance": DEFAULT_TOLERANCE,
        "latency_tolerance": LATENCY_TOLERANCE,
        **current,
    }
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n  已写入基线：{BASELINE_PATH.relative_to(PROJECT_ROOT)}")


def compare(current: dict, baseline: dict) -> int:
    """返回退出码：0 = 通过，1 = 有指标退化。"""
    tolerance = float(baseline.get("tolerance", DEFAULT_TOLERANCE))
    old = baseline.get("metrics") or {}
    new = current["metrics"]

    print("\n" + "=" * 62)
    print("  基线比对")
    print("=" * 62)
    print(f"  基线时间：{baseline.get('generated_at', '未知')}"
          f"　容差：{tolerance * 100:.1f} 个百分点")

    if baseline.get("embedder") != current["embedder"]:
        print("\n  ⚠️  向量后端与基线不一致，**本次不判失败**（只提示）：")
        print(f"      基线：{baseline.get('embedder')}")
        print(f"      当前：{current['embedder']}")
        print("      换了后端请重新标定：--update")

    regressions = []
    improved = []
    latency_tol = float(baseline.get("latency_tolerance", LATENCY_TOLERANCE))
    for name, value in sorted(new.items()):
        if name not in old:
            unit = (f"{value:8.2f}ms" if _is_latency(name)
                    else f"{value * 100:6.1f}%")
            print(f"  ＋ {name:26s} {unit}　（基线里没有，新增记录）")
            continue

        # ★ 延迟与召回**方向相反**：召回越高越好，延迟越低越好。
        #   用同一套比较方向会让"变慢"被算成"变好" ——
        #   这是性能门禁最容易写错、而且错了以后**永远不报**的地方。
        if _is_latency(name):
            before, after = old[name], value
            ratio = (after / before) if before else 1.0
            # ★ 两边都在测量下限之下 → 这个数字由噪声决定，不判失败（见 LATENCY_FLOOR_MS）
            if max(before, after) < LATENCY_FLOOR_MS:
                print(f"    {name:26s} {after:8.2f}ms　(基线 {before:.2f}ms，"
                      f"{ratio * 100 - 100:+.0f}%)　低于测量下限 {LATENCY_FLOOR_MS:g}ms，"
                      f"不判失败")
                continue
            bad = ratio > 1 + latency_tol
            flag = "❌" if bad else "  "
            print(f"  {flag} {name:26s} {after:8.2f}ms　(基线 {before:.2f}ms，"
                  f"{after - before:+.2f}ms / {ratio * 100 - 100:+.0f}%)")
            if bad:
                regressions.append((name, before, after, ratio - 1))
            elif ratio < 1 - latency_tol:
                improved.append((name, -(1 - ratio)))
            continue

        delta = value - old[name]
        flag = "  " if delta >= -tolerance else "❌"
        print(f"  {flag} {name:26s} {value * 100:6.1f}%　(基线 {old[name] * 100:.1f}%，"
              f"{delta * 100:+.1f}pp)")
        if delta < -tolerance:
            regressions.append((name, old[name], value, delta))
        elif delta > tolerance:
            improved.append((name, delta))

    print()
    if regressions:
        print(f"  ❌ {len(regressions)} 项指标退化超过容差：")
        for name, before, after, delta in regressions:
            if _is_latency(name):
                print(f"     · {name}：{before:.2f}ms → {after:.2f}ms"
                      f"（{delta * 100:+.0f}%）")
            else:
                print(f"     · {name}：{before * 100:.1f}% → {after * 100:.1f}%"
                      f"（{delta * 100:+.1f}pp）")
        return 1

    if improved:
        print(f"  ⬆  {len(improved)} 项指标变好（记得在提交说明里写清楚为什么，"
              f"并考虑 --update 抬高基线）：")
        for name, delta in improved:
            unit = f"{delta * 100:+.0f}%（更快）" if _is_latency(name) else f"{delta * 100:+.1f}pp"
            print(f"     · {name} {unit}")
    print("  ✅ 没有指标退化超过容差。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检索基线门禁（零成本，纯检索）")
    parser.add_argument("--top-k", type=int, default=8,
                        help="Top-K 召回率里的 K（默认 8，与 README 的口径一致）")
    parser.add_argument("--update", action="store_true",
                        help="把当前结果写成新基线（换了向量后端就该跑一次）")
    args = parser.parse_args()

    current = collect(args.top_k)

    if args.update:
        write_baseline(current)
        return 0

    if not BASELINE_PATH.exists():
        print(f"  基线文件不存在：{BASELINE_PATH}")
        print("  先跑一次：scripts/eval_baseline.py --update")
        return 1

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if baseline.get("top_k") != args.top_k:
        print(f"  ⚠️  基线是 Top-{baseline.get('top_k')} 的，当前跑的是 Top-{args.top_k}；"
              "两者不可比，跳过比对。")
        return 0

    return compare(current, baseline)


if __name__ == "__main__":
    sys.exit(main())
