# -*- coding: utf-8 -*-
"""指标层（手写 Prometheus 文本格式）
============================================================
tracer.py 记的是**一次运行的细节**（每条 trace 一条 JSONL），
本文件记的是**系统整体此刻的状态**：被调了多少次、错误多少、
延迟分布长什么样。两者回答的不是同一个问题：

    tracer   "这次运行发生了什么"      → 事后排查，逐条可读
    metrics  "系统现在健康吗"          → 实时拉取，聚合计数

【为什么不用 prometheus_client】

项目对外宣称「直接依赖仅 9 个，其余全部手写」，CI 有一条守卫卡
requirements.txt 的有效依赖行数。而 Prometheus 的**文本暴露格式**
（text exposition format 0.0.4）本身很简单：

    # HELP <name> <说明>
    # TYPE <name> counter|gauge|histogram
    <name>{<label>="<value>",...} <数值>

为了这一个格式引入一个依赖（还会带进来一堆传递依赖），
不如按规范手写（本文件约 500 行，含解释；纯逻辑不到 200 行）。
代价是**没有官方库帮我们兜格式的错**，
所以本文件的写法与 tracer 一致：宁可少收集，也不能产出坏文本。

【铁律：观测层不许把业务弄挂 —— 但校验错误必须炸】

这里和 tracer 的取舍**故意不一样**，值得写清楚：

    · 数值层面的问题（NaN / Inf / 指标名写错）→ 静默丢弃 + 计数
      NaN 混进文本里，会让 Prometheus 端**整个抓取失败** ——
      一个业务代码的小 bug 能把整台机器的监控打瞎。
      两害相权，丢一个样本、把丢弃数报出来，是安全的那一边。

    · 编程层面的问题（指标名 / 标签名不合法、labels 不是 dict）
      → **立刻 ValueError**。这类错误不会只在生产出现，它在测试里
      就会复发；悄悄"清洗"掉非法名字，只会让人以为指标在正常上报，
      实际名字已经被改成了一个谁都搜不到的东西。

一句话：**值得丢的是数据，不值得丢的是错误。**

【基数守卫（本文件最重要的一条）】

每个指标最多保留 MAX_SERIES_PER_METRIC 个标签组合，超出的直接丢弃，
并在 `agentdesk_metrics_dropped_total{reason="cardinality"}` 里累加。

为什么不"多留一点"：标签基数爆炸的后果不是"数据多一点"，
而是 Prometheus 端内存先炸（每个标签组合是一个独立时间序列，
时间序列数 × 抓取次数 才是真正的写入量）。**在源头丢弃 + 如实报告
丢弃数**，比"看起来收集了很多、实际把监控打挂"安全得多。
被丢弃的样本必须能被看见 —— 这条和 costs.py 里"未知模型要可见"
是同一个道理：静默地少给数据，比给不出数据更坏。

【直方图为什么是累积桶】

Prometheus 的 `_bucket{le="x"}` 是 **le（小于等于）的累积计数**：
`le="0.1"` 的数已经包含 `le="0.005"` 的那些。所以 observe 时必须
**把当前桶和它右边所有桶一起 +1**，否则桶会不单调 —— 而 Prometheus
的直方图函数（histogram_quantile）在非单调桶上会算出**看起来合理
但完全错误**的分位数。这里没有"近似"的余地。

【刻意不做的两件事】
    · summary 分位数：需要在客户端维护滑动窗口并算分位数，
      而**客户端算出来的分位数不可聚合**（多个实例的 P95 不能求平均）。
      直方图把桶交出去、由服务端聚合，才是分布式下正确的形态。
    · exemplar（样本附 trace_id）：规范上属于 OpenMetrics，
      文本格式 0.0.4 不要求；本项目 trace 已经在 traces.jsonl 里，
      要做关联应该由查询层按时间窗口 join，而不是塞进指标文本。
"""

import math
import re
import threading

# ============================================================
# 常量
# ============================================================
TYPE_COUNTER = "counter"
TYPE_GAUGE = "gauge"
TYPE_HISTOGRAM = "histogram"

# 默认桶。选这套（Prometheus 官方默认）而不是自己拍脑袋：
# 它们覆盖了"毫秒级工具调用 → 几十秒的模型调用"这一个量级跨度，
# 且边界是固定的 —— 桶边界一变，历史数据的直方图就不可比了。
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
                   0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# 基数守卫：单个指标最多保留多少种标签组合（见模块 docstring）
MAX_SERIES_PER_METRIC = 200

# 丢弃原因。目前只有一种，但保留成字典：将来若按"标签值过长"之类
# 再丢样本，直接加一个 key 就行，不用改暴露格式。
REASON_CARDINALITY = "cardinality"
REASON_NONFINITE = "nonfinite"

# 自身指标：监控组件自己也要被监控，否则"指标怎么没了"无从查起
SELF_SERIES = "agentdesk_metrics_series"
SELF_DROPPED = "agentdesk_metrics_dropped_total"
SELF_HELP_SERIES = "Number of series currently held, per metric name."
SELF_HELP_DROPPED = "Samples discarded before exposition, by reason."

# 名字校验：规范来自 exposition format 0.0.4。
# 指标名允许 ':'（record 规则产生），标签名不允许 —— 这是规范原文的差别，
# 不是笔误。不合法一律 ValueError，见模块 docstring 的铁律。
_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_LOCK = threading.Lock()

# name -> {"type": str, "help": str, "series": dict, "seen": int}
#   counter/gauge: series[key] = float
#   histogram:     series[key] = {"buckets": [int...], "sum": float, "count": int}
# key 是 ((标签名, 标签值), ...) 且**已按标签名排序** —— 排序在这里做完，
# 渲染时就不用再排，也保证同一组标签永远映射到同一个 key。
_REGISTRY: dict = {}

# name -> 已注册的 help 文案
_HELP: dict = {}

# (reason, ) -> 丢弃数。非有限值和基数超限分别计数，绝不混成一个数：
# "丢了多少" 和 "为什么丢" 必须能分开看，否则守卫本身没法调参。
_DROPPED: dict = {REASON_CARDINALITY: 0, REASON_NONFINITE: 0}


# ============================================================
# 内部工具
# ============================================================
def _check_name(name) -> str:
    """校验指标名。非法就抛 —— 见模块 docstring 的铁律。"""
    if not isinstance(name, str) or not _METRIC_NAME_RE.match(name):
        raise ValueError(
            f"非法指标名 {name!r}：必须匹配 ^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    return name


def _check_label(key, value) -> tuple:
    """校验一对标签。标签值非字符串直接拒绝（不 str() 兜底）。

    为什么不悄悄 str(value)：`counter("x", {"code": 500})` 和
    `{"code": "500"}` 在文本里长得一样，但后者是调用方的本意、
    前者多半是漏了引号。让它在测试里炸掉，比让它在看板上
    变成一个"永远只有一种取值"的标签强。
    """
    if not isinstance(key, str) or not _LABEL_NAME_RE.match(key):
        raise ValueError(
            f"非法标签名 {key!r}：必须匹配 ^[a-zA-Z_][a-zA-Z0-9_]*$")
    if not isinstance(value, str):
        raise ValueError(
            f"标签值必须是字符串（{key}={value!r}）：数字/布尔请显式 str()")
    return key, value


def _norm_labels(labels) -> tuple:
    """把 labels 规范化成已排序的元组 key，顺带完成校验。

    `labels=None` 与 `labels={}` 得到同一个 key（空元组）——
    两种写法都是"没有标签"，不该变成两个不同的序列。
    """
    if labels is None:
        return ()
    if not isinstance(labels, dict):
        raise ValueError(f"labels 必须是 dict，收到 {type(labels).__name__}")
    return tuple(sorted(_check_label(k, v) for k, v in labels.items()))


def _check_number(value) -> float:
    """把数值转成 float。允许 int/float/bool，其余一律拒绝。

    拒绝 Decimal / 字符串 / None：它们在 `float()` 上要么抛
    TypeError、要么给出**静默错误**的结果（`float("nan")` 不抛）。
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"指标数值必须是 int/float，收到 {type(value).__name__}")


def _bump_dropped(reason: str) -> None:
    """丢弃计数 +1。是**纯记账**，不再走基数守卫（守卫自己不能被守卫拦下）。"""
    _DROPPED[reason] = _DROPPED.get(reason, 0) + 1


def _fmt(value) -> str:
    """浮点格式：整数就写整数，别产出 `5.0` 这种尾零。

    为什么在意：`5` 和 `5.0` 在 Prometheus 里等价，但人读 /metrics
    输出（和文档、diff）时，一堆 `_total 12.0` 会让人怀疑计数被算成了浮点。
    计数类指标本来就应该是整数形态。
    """
    f = float(value)
    if not math.isfinite(f):
        return "0"                     # 理论上不可达：非有限值在入口就丢了
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _escape(value: str) -> str:
    """标签值转义（规范 0.0.4）：反斜杠 → `\\\\`、双引号 → `\\"`、换行 → `\\n`。

    ★ 顺序不能反：必须**先转义反斜杠**。反过来的话，
    原值 `\\n`（反斜杠 + 字母 n）里那个新转义出来的反斜杠会被再转一次，
    变成 `\\\\n`，读回来是 `\\n` 而不是 `\n` —— 一个只在
    标签值真含反斜杠时出现、且**看起来只是"多了一个斜杠"**的错误。

    转义后的文本必须能被逐字还原（tests/test_metrics.py 里有往返用例）：
    解析器和生成器对转义规则的理解只要差一点，标签值就会静默变形，
    而标签值是聚合的维度 —— 变形后就成了另一个序列。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _series_text(key: tuple) -> str:
    """渲染 `{a="1",b="2"}`。无标签返回空串 —— **不写空花括号**。

    `metric{}` 是合法的，但多余：它让"这个指标有没有标签"这个
    看一眼就能回答的问题，变成要读完才知道。
    """
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + inner + "}"


# ============================================================
# 对外接口
# ============================================================
def help_text(name: str, text: str) -> None:
    """注册 HELP 文案。没注册过的指标不输出 `# HELP` 行。

    为什么不给默认文案：`# HELP x ` 这种空文案在抓取端是合法但无意义的，
    它会让"忘了写说明"和"写了个空的"看起来一样。宁可没有这一行。
    """
    _check_name(name)
    if not isinstance(text, str):
        raise ValueError("HELP 文案必须是字符串")
    with _LOCK:
        _HELP[name] = text


def counter(name: str, labels: dict | None = None, value: float = 1.0) -> None:
    """计数器：单调累加。同名同标签累加，不同标签各算一条序列。

    ★ 同名指标**第一次出现时确定类型**，之后调用类型不符就抛：
    counter 和 gauge 混用同一个名字，会让文本里的 `# TYPE` 与实际
    语义打架（Prometheus 端会直接报错，且报的是"类型冲突"，
    离真正的原因很远）。类型冲突是编程错误，必须在测试里暴露。
    """
    _check_name(name)
    key = _norm_labels(labels)
    amount = _check_number(value)
    if not math.isfinite(amount):
        with _LOCK:
            _bump_dropped(REASON_NONFINITE)
        return

    with _LOCK:
        m = _REGISTRY.get(name)
        if m is None:
            m = _REGISTRY[name] = {"type": TYPE_COUNTER, "series": {}}
        elif m["type"] != TYPE_COUNTER:
            raise ValueError(
                f"指标 {name} 已注册为 {m['type']}，不能当 counter 用")
        series = m["series"]
        if key not in series:
            if len(series) >= MAX_SERIES_PER_METRIC:
                # 基数超限：丢弃并如实记账（见模块 docstring）
                _bump_dropped(REASON_CARDINALITY)
                return
            series[key] = 0.0
        series[key] += amount


def gauge(name: str, value: float, labels: dict | None = None) -> None:
    """仪表：**覆盖式设置**（不是累加）。

    ★ 这个签名和 counter 不同（value 在前），是刻意的 ——
    两种语义最容易写错的地方就是"把 set 写成 add"，参数位置一致
    反而更容易看错。覆盖语义要的是"当前值"，累加语义要的是"增量"。

    负数允许：gauge 表达的是瞬时值（队列长度、内存占用），
    某些"计算出来的差值"天然可以是负的。counter 则不允许为负 ——
    但那由调用方保证，这里不做符号检查（一个负的增量往往是
    "我传错了"，让它出现在输出里比悄悄取绝对值更容易被发现）。
    """
    _check_name(name)
    key = _norm_labels(labels)
    amount = _check_number(value)
    if not math.isfinite(amount):
        with _LOCK:
            _bump_dropped(REASON_NONFINITE)
        return

    with _LOCK:
        m = _REGISTRY.get(name)
        if m is None:
            m = _REGISTRY[name] = {"type": TYPE_GAUGE, "series": {}}
        elif m["type"] != TYPE_GAUGE:
            raise ValueError(
                f"指标 {name} 已注册为 {m['type']}，不能当 gauge 用")
        series = m["series"]
        if key not in series:
            if len(series) >= MAX_SERIES_PER_METRIC:
                _bump_dropped(REASON_CARDINALITY)
                return
            series[key] = 0.0
        series[key] = amount          # 覆盖，不累加


def observe(name: str, seconds: float, labels: dict | None = None) -> None:
    """观测一次耗时（秒），进直方图。

    ★ 累积桶：找到第一个 `value <= 边界` 的桶，从它开始
    **往右全部 +1**（见模块 docstring）。这是 le 语义决定的，
    不是实现细节 —— 桶一旦不单调，histogram_quantile 会给出
    看起来合理但完全错误的分位数。
    """
    _check_name(name)
    key = _norm_labels(labels)
    value = _check_number(seconds)
    if not math.isfinite(value):
        # NaN 的比较永远是 False，混进桶里会让**每个**桶的判定都失败；
        # Inf 则会把 +Inf 桶和数据总量对不上。两者都会让抓取端算出鬼数字。
        with _LOCK:
            _bump_dropped(REASON_NONFINITE)
        return

    nbuckets = len(DEFAULT_BUCKETS)
    with _LOCK:
        m = _REGISTRY.get(name)
        if m is None:
            m = _REGISTRY[name] = {"type": TYPE_HISTOGRAM, "series": {}}
        elif m["type"] != TYPE_HISTOGRAM:
            raise ValueError(
                f"指标 {name} 已注册为 {m['type']}，不能当 histogram 用")
        series = m["series"]
        obs = series.get(key)
        if obs is None:
            if len(series) >= MAX_SERIES_PER_METRIC:
                _bump_dropped(REASON_CARDINALITY)
                return
            obs = series[key] = {
                "buckets": [0] * (nbuckets + 1),   # 多一格给 +Inf
                "sum": 0.0, "count": 0,
            }
        idx = 0
        for i, bound in enumerate(DEFAULT_BUCKETS):
            if value <= bound:
                idx = i
                break
        else:
            idx = nbuckets                          # 比最后一个边界还大 → +Inf
        buckets = obs["buckets"]
        for i in range(idx, nbuckets + 1):
            buckets[i] += 1
        obs["sum"] += value
        obs["count"] += 1


def render() -> str:
    """按 Prometheus text exposition format 0.0.4 渲染全部指标。

    输出顺序**确定**（指标名排序、桶按边界升序、标签名排序）：
    不确定的顺序会让 /metrics 的每次抓取都产生 diff，
    而"输出变了"本来是一个信号 —— 顺序抖动会把这个信号淹掉。

    结束时**必定有换行**：抓取端的最后一行如果没有换行，
    某些实现会把它丢掉（少一个样本，且不报错）。
    """
    # 取一份快照再拼字符串：**不在持锁状态下做字符串处理**。
    # 指标导出会被高频抓取（每 15 秒一次），按住锁拼文本会让所有
    # 业务线程在计数时排队 —— 观测的开销不该被别人抓取的频率决定。
    #
    # 自身指标不进 _REGISTRY，而是**临时加到这份快照上**：
    # 注册表是"被记录的指标"，守卫数字是"算出来的"——
    # 混在一起会让基数守卫去数自己，甚至把自己的序列丢掉。
    #
    # ★ 即使一个用户指标都没有（注册表为空），`agentdesk_metrics_dropped_total`
    #   仍然输出：如果"全空"和"层没工作"在输出上长得一样，就没法回答
    #   "进程活着吗、指标层加载了吗"—— 而 0 字节的 /metrics 恰恰最容易被
    #   当成"抓取正常，只是没有数据"。
    #   但 `agentdesk_metrics_series` 只在真有指标时才输出：它的语义是
    #   "每个指标当前有多少条序列"，一个指标都没有时它**没有样本可言**，
    #   输出一个光有 `# TYPE` 没有样本的族反而是坏文本。
    with _LOCK:
        model = {n: (_REGISTRY[n]["type"], dict(_REGISTRY[n]["series"]))
                 for n in _REGISTRY}
        help_map = dict(_HELP)
        if model:
            model[SELF_SERIES] = (
                TYPE_GAUGE,
                {((("metric", n),)): float(len(s)) for n, (_, s) in model.items()},
            )
        model[SELF_DROPPED] = (
            TYPE_COUNTER,
            {((("reason", r),)): float(c) for r, c in _DROPPED.items()},
        )

    lines = []
    for name in sorted(model):
        mtype, series = model[name]
        help_line = help_map.get(name)
        if help_line is None and name == SELF_SERIES:
            help_line = SELF_HELP_SERIES
        elif help_line is None and name == SELF_DROPPED:
            help_line = SELF_HELP_DROPPED
        # HELP 只在**注册过**（或自身指标）时输出，见 help_text 的说明
        if help_line is not None:
            lines.append(f"# HELP {name} {help_line}")
        lines.append(f"# TYPE {name} {mtype}")

        if mtype == TYPE_HISTOGRAM:
            for key in sorted(series):
                obs = series[key]
                for i, bound in enumerate(DEFAULT_BUCKETS):
                    lines.append(
                        f'{name}_bucket{_with_le(key, _fmt(bound))} '
                        f'{_fmt(obs["buckets"][i])}')
                # +Inf 桶 = 观测总数。它必须等于 _count ——
                # 对不上就说明有样本被漏计/重计，抓取端会直接报错。
                lines.append(
                    f'{name}_bucket{_with_le(key, "+Inf")} {_fmt(obs["buckets"][-1])}')
                lines.append(f'{name}_sum{_series_text(key)} {_fmt(obs["sum"])}')
                lines.append(f'{name}_count{_series_text(key)} {_fmt(obs["count"])}')
        else:
            for key in sorted(series):
                lines.append(f"{name}{_series_text(key)} {_fmt(series[key])}")

    text = "\n".join(lines)
    return text + "\n" if text else ""


def _with_le(key: tuple, le: str) -> str:
    """直方图桶的标签：调用方标签 + `le`。

    ★ le 放在最后。规范不强制顺序，但"用户标签在前、le 在后"
    是 Prometheus 官方 client 的习惯 —— 也让 grep `le=` 更容易。
    注意 le 的**值**是数字字面量（`0.005`、`+Inf`），要按标签值转义渲染。
    """
    parts = [f'{k}="{_escape(v)}"' for k, v in key]
    parts.append(f'le="{le}"')
    return "{" + ",".join(parts) + "}"


def reset() -> None:
    """清空全部指标（测试用）。

    丢弃计数也一起清零：留着它会让"这条用例的丢弃数"混进上一条用例
    留下的数字，断言就变成了"大于等于"而不是"等于" —— 那种断言
    永远不会因为回归而变红，等于没测。
    """
    with _LOCK:
        _REGISTRY.clear()
        _HELP.clear()
        for reason in list(_DROPPED):
            _DROPPED[reason] = 0


def snapshot() -> dict:
    """结构化快照（测试 / 调试用）。

    形状：`{指标名: {"type", "help", "series": [{"labels", ...值}]}}`。
    这是**给人看的调试视图**（`/debug/metrics` 之类），不是抓取格式 ——
    抓取格式永远只有 `render()` 一条路径，多一个"差不多的"输出格式
    就多一处会和规范漂移的地方。

    自身指标（`agentdesk_metrics_series` 等）不出现在快照里：
    它们是 render() 时按需计算的派生物，放进快照会让人以为
    它们也是"被记录"的数据。
    """
    with _LOCK:
        out = {}
        for name in sorted(_REGISTRY):
            if name in (SELF_SERIES, SELF_DROPPED):
                continue
            m = _REGISTRY[name]
            rows = []
            for key in sorted(m["series"]):
                item = {"labels": {k: v for k, v in key}}
                data = m["series"][key]
                if m["type"] == TYPE_HISTOGRAM:
                    item["buckets"] = [
                        {"le": _fmt(b), "count": data["buckets"][i]}
                        for i, b in enumerate(DEFAULT_BUCKETS)
                    ]
                    item["buckets"].append(
                        {"le": "+Inf", "count": data["buckets"][-1]})
                    item["sum"] = data["sum"]
                    item["count"] = data["count"]
                else:
                    item["value"] = data
                rows.append(item)
            out[name] = {"type": m["type"], "help": _HELP.get(name), "series": rows}
        return out


def dropped(reason: str | None = None) -> int:
    """读丢弃计数。`reason=None` 返回**全部原因**的合计。

    为什么要有这个函数（而不是只暴露成指标）：测试与自检脚本需要
    一个不依赖文本解析的读数；而"合计"这个视图只有在这里才方便给。
    """
    with _LOCK:
        if reason is None:
            return sum(_DROPPED.values())
        return _DROPPED.get(reason, 0)
