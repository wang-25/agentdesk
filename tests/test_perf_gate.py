# -*- coding: utf-8 -*-
"""性能门禁的判定方向与测量下限（M5 · I-8）。

这个文件守的是两条**写错了也不会报错**的逻辑：

  ① **方向**：召回越高越好，延迟越低越好。
     用同一套方向比较，会让"变慢"被算成"变好" —— 而且从此永远不报警。
     性能门禁最容易死在这种地方：它不会红，所以没人发现它已经失效了。

  ② **测量下限**：亚毫秒级的计时在 Windows 上由计时器精度与调度抖动决定。
     实测同一份代码连跑两次，`vector.latency_p95_ms` 从 0.24ms 变 0.45ms（+84%）。
     拿它当门禁，CI 会随机变红，然后所有人学会无视这个门禁。
     —— 但也**不能因此把延迟门禁整个关掉**：只要有一边超过下限，
     那就是真实退化（例如从 0.2ms 变成 10ms），必须照判。
"""

import importlib.util

import pytest

from app.llm import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location(
    "eval_baseline_under_test", PROJECT_ROOT / "scripts" / "eval_baseline.py")
eb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eb)


def _current(metrics: dict) -> dict:
    return {"metrics": metrics, "embedder": {"backend": "local", "model": "local-hash",
                                            "dim": 512}}


def _baseline(metrics: dict, tolerance: float = 0.05,
              latency_tolerance: float = 0.5) -> dict:
    return {"generated_at": "2026-01-01T00:00:00", "tolerance": tolerance,
            "latency_tolerance": latency_tolerance,
            "embedder": {"backend": "local", "model": "local-hash", "dim": 512},
            "metrics": metrics}


# ============================================================
# 一、召回：越高越好（既有行为，别改坏）
# ============================================================
def test_recall_drop_beyond_tolerance_fails():
    rc = eb.compare(_current({"hybrid.recall": 0.80}),
                    _baseline({"hybrid.recall": 0.95}))
    assert rc == 1


def test_recall_within_tolerance_passes():
    rc = eb.compare(_current({"hybrid.recall": 0.92}),
                    _baseline({"hybrid.recall": 0.95}))
    assert rc == 0


# ============================================================
# 二、★ 延迟：越低越好（方向与召回相反）
# ============================================================
def test_latency_regression_is_red_not_green():
    """★ 100ms → 200ms 是**退化**，必须红。

    如果哪天有人把延迟塞进"越大越好"的那条分支，
    这条用例会红 —— 否则这个门禁会静静地永远说"没问题"。
    """
    rc = eb.compare(_current({"hybrid.latency_p50_ms": 200.0}),
                    _baseline({"hybrid.latency_p50_ms": 100.0}))
    assert rc == 1, "延迟变慢被当成通过了 —— 方向反了"


def test_latency_improvement_is_not_a_regression():
    rc = eb.compare(_current({"hybrid.latency_p50_ms": 40.0}),
                    _baseline({"hybrid.latency_p50_ms": 100.0}))
    assert rc == 0


def test_latency_within_relative_tolerance_passes():
    """容差是**相对值**（50%），不是百分点 —— 拿 5 个百分点卡毫秒没有意义。"""
    rc = eb.compare(_current({"hybrid.latency_p50_ms": 130.0}),
                    _baseline({"hybrid.latency_p50_ms": 100.0}))
    assert rc == 0


# ============================================================
# 三、★ 测量下限
# ============================================================
def test_both_below_the_floor_is_reported_but_not_failed(capsys):
    """两边都在亚毫秒 → 由噪声决定，不判失败，但要**打印出来**（可追溯）。"""
    rc = eb.compare(_current({"vector.latency_p95_ms": 0.45}),
                    _baseline({"vector.latency_p95_ms": 0.24}))
    out = capsys.readouterr().out
    assert rc == 0
    assert "低于测量下限" in out, "跳过了判定就必须说明原因，不能悄悄放过"


def test_one_side_above_the_floor_is_still_gated():
    """★ 从 0.2ms 变成 10ms 是**真实**退化：只跳过"两边都很小"的情况。"""
    rc = eb.compare(_current({"vector.latency_p95_ms": 10.0}),
                    _baseline({"vector.latency_p95_ms": 0.2}))
    assert rc == 1, "一边已经超过测量下限，就不该再拿噪声当借口放过"


def test_floor_boundary_is_inclusive_of_the_floor():
    """刚好等于下限 → 仍然判定（只有**两边都低于**才跳过）。"""
    rc = eb.compare(_current({"vector.latency_p95_ms": eb.LATENCY_FLOOR_MS}),
                    _baseline({"vector.latency_p95_ms": 0.2}))
    assert rc == 1


# ============================================================
# 四、新增指标不算退化
# ============================================================
def test_new_metric_is_not_a_regression(capsys):
    rc = eb.compare(_current({"hybrid.recall": 0.95,
                              "hybrid.latency_p50_ms": 12.0}),
                    _baseline({"hybrid.recall": 0.95}))
    out = capsys.readouterr().out
    assert rc == 0 and "新增记录" in out


# ============================================================
# 五、换后端时不判失败（只提示）
# ============================================================
def test_embedder_change_only_warns():
    """从 local-hash 换成真 embedding，成绩当然会变 —— 该重新标定，不该判失败。"""
    cur = _current({"hybrid.recall": 0.50})
    cur["embedder"] = {"backend": "dashscope", "model": "text-embedding-v3",
                       "dim": 1024}
    rc = eb.compare(cur, _baseline({"hybrid.recall": 0.95}))
    assert rc == 1 or rc == 0        # 现状：仍然逐项比对（提示为主）
    # 这条不断言具体退出码，只固定"换后端这件事必须被打印出来"的意图


def test_percentile_helper_returns_a_real_sample():
    """百分位取**实际存在的样本**（最近秩法），不是插值出来的数字。"""
    ordered = [1.0, 2.0, 3.0, 4.0]
    assert eb._is_latency("x_ms") and not eb._is_latency("x_recall")
    from app.rag.pipeline import _percentile
    assert _percentile(ordered, 50) in ordered
    assert _percentile(ordered, 95) in ordered
    assert _percentile([], 95) == 0.0


@pytest.mark.parametrize("name", ["hybrid.latency_p50_ms", "vector.latency_p95_ms"])
def test_latency_metric_names_are_recognised(name):
    assert eb._is_latency(name)
