# -*- coding: utf-8 -*-
"""指标层 —— "这个文本能不能被 Prometheus 正确抓走"的底线。

这一层的特点和 tracer/costs 一样：**错了不会有人告诉你**。

    · 一个没转义的换行 → 文本从那一行断开，抓取端少一个样本，**不报错**
    · 直方图桶不单调   → histogram_quantile 给出看起来很合理的错分位数
    · 混进一个 NaN     → Prometheus 端**整次抓取失败**，一个业务小 bug
                          能把整台机器的监控打瞎
    · 标签基数失控     → 源头没事，监控端先 OOM

所以这里的用例密度集中在三件事上：

    ① **规范校验器**（本文件顶部那个纯 stdlib 解析器）
       不是"看一眼输出里有没有某段字符串"，而是把整份 `render()` 的
       输出**当规范文本解析一遍**：每个样本行能不能解析、值是不是小数字面量、
       HELP/TYPE 是否成对、桶是否单调、`_count` 是否等于 `+Inf` 桶。
       字符串断言只能证明"我想看到的那段在"，解析器能证明"没有别的东西是坏的"。

    ② **转义的可往返性**
       生成器和解析器对转义规则的理解只要差一点，标签值就会静默变形 ——
       而标签值是聚合维度，变形后就成了**另一个序列**。
       所以断言不是"看起来转义了"，而是"转义后能逐字还原"。

    ③ **失败必须可见**
       丢弃的样本要能被数出来（`dropped()` / `agentdesk_metrics_dropped_total`）。
       静默地少给数据，比给不出数据更坏 —— 这是本项目反复出现的同一条教训。

每个用例都从 `reset()` 开始（下面的 autouse 装置），
所以断言的是**准确值**（`== 1`）而不是"至少为 1" ——
后者永远不会因为回归而变红，等于没测。
"""

import re
import threading

import pytest

from app.observability import metrics

# ============================================================
# 规范校验器（纯 stdlib：逐行正则 + 状态机）
# ============================================================
# 样本行：`name{labels} value` / `name value` / `name{} value`
#   标签值的正则 `(?:[^"\\]|\\.)*` 允许转义序列，但不允许裸引号 ——
#   一个没转义的 `"` 会在这里被抓住，而不是被当成合法标签。
_SAMPLE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?:\{(?P<labels>.*)\})?'
    r' (?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$')
_LABEL_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"$')
_TYPE_RE = re.compile(r'^# TYPE ([a-zA-Z_:][a-zA-Z0-9_:]*) '
                      r'(counter|gauge|histogram)$')
_HELP_RE = re.compile(r'^# HELP ([a-zA-Z_:][a-zA-Z0-9_:]*) (.*)$')

# `# HELP name ` 后面**必须**有内容（空文案等于没写，却被当成写了）
_BARE_TYPE = re.compile(r'^# TYPE ')
_BARE_HELP = re.compile(r'^# HELP ')
_KNOWN_TYPE = {"counter", "gauge", "histogram"}


def _split_labels(body):
    """按逗号拆标签（转义序列里的逗号不在引号外，不会被误拆）。"""
    pairs: list = []
    if body == "":
        return pairs
    for part in body.split(","):
        m = _LABEL_RE.match(part)
        assert m, f"标签片段不合法：{part!r}"
        pairs.append((m.group(1), m.group(2)))
    return pairs


def parse_exposition(text):
    """把 render() 的输出解析成结构化数据，任何不合规都直接 assert 失败。

    返回 `{指标名: {"type", "help", "samples"}}`，
    samples 里每条是 `(name, labels元组, 数值, 行号)`。

    ★ 这个解析器是**独立实现**的：它不调用 metrics 里的任何渲染辅助函数
      （`_escape` / `_fmt` / `_series_text` 一个都不用）。
      否则生成器和"校验器"共享同一个错误理解 —— 转义规则写错了，
      两边一起错，用例还是绿的。
    """
    assert text, "render() 返回了空字符串"
    assert text.endswith("\n"), "输出必须以换行结尾（末行会被抓取端丢掉）"
    assert "\r" not in text, "输出里不允许出现 CR"

    found: dict = {}
    help_map = {}
    type_map = {}

    lines = text.split("\n")
    assert lines[-1] == "", "以换行结尾时 split 的最后一项必须是空串"
    for lineno, line in enumerate(lines[:-1], start=1):
        assert line != "", f"第 {lineno} 行是空行：指标块之间不该有空行"

        if _BARE_HELP.match(line):
            m = _HELP_RE.match(line)
            assert m, f"第 {lineno} 行 HELP 不合法：{line!r}"
            name = m.group(1)
            assert m.group(2).strip(), f"第 {lineno} 行 HELP 文案为空"
            assert name not in help_map, f"{name} 的 HELP 出现了两次"
            help_map[name] = m.group(2)
            continue

        if _BARE_TYPE.match(line):
            m = _TYPE_RE.match(line)
            assert m, f"第 {lineno} 行 TYPE 不合法：{line!r}"
            name, mtype = m.group(1), m.group(2)
            assert mtype in _KNOWN_TYPE, mtype
            assert name not in type_map, f"{name} 的 TYPE 出现了两次"
            type_map[name] = mtype
            continue

        assert not line.startswith("#"), f"第 {lineno} 行有未识别的注释：{line!r}"

        m = _SAMPLE_RE.match(line)
        assert m, f"第 {lineno} 行不是合法的样本行：{line!r}"
        name, body, raw_value = m.group("name"), m.group("labels"), m.group("value")
        labels = tuple(_split_labels(body or ""))
        # 标签名唯一（同一行里重复的标签是坏文本）
        keys = [k for k, _ in labels]
        assert len(keys) == len(set(keys)), f"第 {lineno} 行标签名重复：{line!r}"
        value = float(raw_value)
        assert value == value and value not in (float("inf"), float("-inf")), \
            f"第 {lineno} 行出现了非有限值：{line!r}"

        found.setdefault(name, []).append((tuple(sorted(labels)), value, lineno))

    # ---- 状态机校验 ----
    # 直方图在文本里是**三个名字族**（`h_bucket` / `h_sum` / `h_count`），
    # 但只有基名 `h` 有 `# TYPE h histogram`。所以先把"带后缀的名字"
    # 归到基名上，再做"每个样本名都必须有 TYPE"的检查 ——
    # 这正是抓取端解析指标的规则，校验器必须照它来。
    for name in found:
        if name in type_map:
            continue
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                assert type_map.get(base) == "histogram", \
                    f"{name} 看起来是直方图的 {suffix}，但没有基名 {base} 的 histogram TYPE"
                break
        else:
            raise AssertionError(f"{name} 有样本行，却不是已知的指标名或直方图后缀名")

    for name, samples in found.items():
        mtype = _family_type(name, type_map)
        if mtype == "histogram":
            continue
        # counter / gauge：同标签组合**只能出现一次** ——
        # 重复暴露同一序列，抓取端会直接报错。
        seen_keys = [labels for labels, _, _ in samples]
        assert len(seen_keys) == len(set(seen_keys)), \
            f"{name} 暴露了重复的标签组合：{seen_keys}"

    # 每个直方图的**基名**本身没有样本行（样本在 _bucket/_sum/_count 里），
    # 所以"有 TYPE 就必有样本"这条要按族判断；非直方图才要求同名样本。
    for name, mtype in type_map.items():
        if mtype == "histogram":
            _check_histogram(name, found)      # 内部已断言三族都在
        else:
            assert name in found, f"{name} 声明了 # TYPE {mtype} 却一个样本都没有"

    return {n: {"type": _family_type(n, type_map), "help": help_map.get(n),
                "samples": s} for n, s in found.items()}


def _family_type(name, type_map):
    """样本名 → 它属于哪个指标。直方图的后缀名归到基名。"""
    if name in type_map:
        return type_map[name]
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix):
            return type_map.get(name[: -len(suffix)])
    return None


def _check_histogram(name, found):
    """直方图专属校验：桶单调、`+Inf` 桶 == `_count`、`_sum`/`_count` 齐全。

    ★ 这里的三个断言就是"Prometheus 能不能正确算分位数"的全部前提。
      任何一条不成立，输出在语法上仍然是合法文本 —— 抓取端不会报错，
      只会给出一个**看起来合理的错数字**。
    """
    bucket_name, sum_name, count_name = name + "_bucket", name + "_sum", name + "_count"
    assert bucket_name in found, f"{name} 是直方图却没有 _bucket 行"
    assert sum_name in found, f"{name} 是直方图却没有 _sum 行"
    assert count_name in found, f"{name} 是直方图却没有 _count 行"

    # 桶：按"去掉 le 的标签组合"分组，逐组检查单调性与 +Inf 收尾
    buckets: dict = {}
    for labels, value, lineno in found[bucket_name]:
        pair = dict(labels)
        assert "le" in pair, f"{bucket_name} 的桶行必须有 le 标签：{labels}"
        key = tuple(p for p in labels if p[0] != "le")
        buckets.setdefault(key, []).append((pair["le"], value, lineno))

    for key, rows in buckets.items():
        les = [le for le, _, _ in rows]
        assert les[-1] == "+Inf", f"{name} 的最后一个桶必须是 +Inf：{les[-1]!r}"
        finite = [float(le) for le in les[:-1]]
        assert len(finite) == len(set(finite)), "桶边界重复"
        values = [v for _, v, _ in rows]
        assert values == sorted(values), f"{name} 的桶计数不单调不减：{values}"
        assert finite == sorted(finite), f"{name} 的 le 边界没有升序：{finite}"

        # `_count` 必须存在（同一组标签），且**严格等于** +Inf 桶
        count_rows = [v for lbs, v, _ in found[count_name]
                      if tuple(p for p in lbs if p[0] != "le") == key]
        assert len(count_rows) == 1, f"{name} 的 _count 行数不是 1：{count_rows}"
        assert count_rows[0] == values[-1], \
            f"{name} 的 _count({count_rows[0]}) != +Inf 桶({values[-1]})"

        # `_sum` 也必须存在（同一组标签），且不能是负数以外的异常值
        sum_rows = [v for lbs, v, _ in found[sum_name]
                    if tuple(p for p in lbs if p[0] != "le") == key]
        assert len(sum_rows) == 1, f"{name} 的 _sum 行数不是 1：{sum_rows}"

    # `_sum` / `_count` 不允许出现在没有任何桶的标签组合上
    bucket_keys = set(buckets)
    for family in (sum_name, count_name):
        for labels, _, lineno in found[family]:
            key = tuple(p for p in labels if p[0] != "le")
            assert key in bucket_keys, \
                f"第 {lineno} 行 {family}{labels} 没有对应的 _bucket 行"


def samples_of(parsed, name):
    return parsed[name]["samples"]


def value_of(parsed, name, **labels):
    """按名字 + 标签取唯一一个样本的数值。

    ★ 必须先排序再比：解析器里的标签是**按标签名排序**的元组，
      而关键字参数保持调用顺序 —— `value_of(x, le="1", op="a")`
      拿到的是 `(("le",..),("op",..))`，不排序就一条都匹配不上，
      表现为"样本凭空消失"。这正是标签要排序带来的连带要求。
    """
    want = tuple(sorted(labels.items()))
    hits = [v for lbs, v, _ in parsed[name]["samples"] if lbs == want]
    assert len(hits) == 1, f"{name}{labels} 匹配到 {len(hits)} 条样本"
    return hits[0]


def all_values(parsed, name):
    return [v for _, v, _ in parsed[name]["samples"]]


def _unescape(text):
    """转义的逆运算 —— 用来断言"转义后的形态能被还原"。"""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        out.append("\n" if nxt == "n" else nxt)
        i += 2
    return "".join(out)


@pytest.fixture(autouse=True)
def _clean_metrics():
    """每条用例从空注册表开始 —— 否则断言会跨用例累积，变成永远为真。"""
    metrics.reset()
    yield
    metrics.reset()


# ============================================================
# 一、文本格式（规范 0.0.4）
# ============================================================
def test_render_is_valid_prometheus_exposition_text():
    """三种类型混在一起时，整份输出必须整体合规（不是"某几行像"）。"""
    metrics.help_text("req_total", "Total requests.")
    metrics.counter("req_total", {"path": "/health"}, 3)
    metrics.gauge("queue_depth", 7.0, {"queue": "default"})
    metrics.observe("latency_seconds", 0.42, {"endpoint": "/agent/ask"})

    parsed = parse_exposition(metrics.render())

    assert parsed["req_total"]["type"] == "counter"
    assert parsed["queue_depth"]["type"] == "gauge"
    # 直方图在文本里是三个名字族，基名本身没有样本行
    assert parsed["latency_seconds_bucket"]["type"] == "histogram"


def test_help_and_type_are_emitted_in_order_and_only_once():
    """HELP 必须在 TYPE 之前，两者各出现一次。

    ★ 也锁住"没注册就不输出 HELP"：`# HELP x ` 这种空文案在抓取端合法
    但无意义，它会让"忘了写说明"和"写了个空的"看起来一样。
    """
    metrics.help_text("req_total", "Total requests.")
    metrics.counter("req_total")
    metrics.counter("no_help_total")          # 刻意不注册 HELP

    text = metrics.render()
    parsed = parse_exposition(text)           # 顺带跑一遍整体校验

    assert text.index("# HELP req_total Total requests.") < \
        text.index("# TYPE req_total counter")
    assert text.count("# TYPE req_total counter") == 1
    assert "# HELP no_help_total" not in text, "没注册的指标不该凭空长出 HELP"
    assert parsed["no_help_total"]["help"] is None


def test_no_labels_means_no_braces():
    """无标签时不写空花括号 —— `metric{}` 合法但多余，人读时更费劲。"""
    metrics.counter("plain_total", None, 5)
    metrics.gauge("plain_gauge", 2)
    metrics.observe("plain_hist", 0.02)

    text = metrics.render()
    parse_exposition(text)

    assert "plain_total 5" in text
    assert "{}" not in text, f"输出了空花括号：\n{text}"
    for line in text.splitlines():
        if "#" not in line and "plain_hist_bucket" in line:
            assert "le=" in line, line


def test_rendered_output_ends_with_exactly_one_newline():
    """结尾必须有换行，且**只有**一个 —— 缺了会丢末行，多了会出现空行。"""
    metrics.counter("a_total")
    metrics.gauge("b_gauge", 1)

    text = metrics.render()
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert metrics.render() == text, "render() 必须是确定性的"


def test_output_order_is_deterministic_and_sorted():
    """顺序确定（指标名升序、标签名升序、桶升序）。

    不确定的顺序会让每次抓取都产生 diff，而"输出变了"本来是一个信号。
    """
    metrics.counter("z_total", {"b": "2", "a": "1"})
    metrics.counter("a_total", {"y": "2", "x": "1"})
    metrics.observe("m_seconds", 0.3, {"z": "1", "a": "2"})

    # 先用另一份注册顺序造出同样的指标，确认输出与注册顺序无关
    first = metrics.render()
    metrics.reset()
    metrics.counter("a_total", {"x": "1", "y": "2"})
    metrics.observe("m_seconds", 0.3, {"a": "2", "z": "1"})
    metrics.counter("z_total", {"a": "1", "b": "2"})
    second = metrics.render()

    assert first == second, "输出随注册顺序变了"
    names = [line.split("{")[0].split(" ")[0]
             for line in first.splitlines() if not line.startswith("#")]
    # 指标名必须成组出现，且组间按名字升序
    groups = [n.split("_bucket")[0].split("_sum")[0].split("_count")[0]
              for n in names]
    assert groups == sorted(groups, key=lambda g: (g, names.index(next(
        x for x in names if x.startswith(g))))), groups
    # 同一指标内部的标签必须有序
    assert 'z_total{a="1",b="2"}' in first


def test_histogram_buckets_are_cumulative_and_boundaries_ascend():
    """桶是**累积**的（le = 小于等于），且 `_count` == `+Inf` 桶。

    Prometheus 的 histogram_quantile 在非单调桶上会算出**看起来合理
    但完全错误**的分位数 —— 所以这条不是"格式好看"，是正确性。
    """
    for seconds in (0.003, 0.007, 0.02, 3.0):
        metrics.observe("lat_seconds", seconds)

    parsed = parse_exposition(metrics.render())      # 校验器里已断言单调 + 对账
    rows = samples_of(parsed, "lat_seconds_bucket")
    counts = [v for _, v, _ in rows]

    # 0.003→桶0.005、0.007→0.01、0.02→0.025、3.0→桶5（不是 2.5！）
    # 每个观测只从自己所在的桶开始向右累加：前三个都在 0.025 之前落定，
    # 第四个落在 5 这一桶，所以 0.05..2.5 是 3、5..+Inf 才是 4。
    assert counts == [1, 2, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4], counts
    assert value_of(parsed, "lat_seconds_count") == 4
    assert value_of(parsed, "lat_seconds_sum") == pytest.approx(3.03)


def test_histogram_respects_its_own_labels():
    """不同标签各自一条直方图，不能互相污染。"""
    metrics.observe("lat_seconds", 0.003, {"op": "fast"})
    metrics.observe("lat_seconds", 9.0, {"op": "slow"})
    metrics.observe("lat_seconds", 9.0, {"op": "slow"})

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, "lat_seconds_count", op="fast") == 1
    assert value_of(parsed, "lat_seconds_count", op="slow") == 2
    # fast 的值只落在第一个桶里
    assert value_of(parsed, "lat_seconds_bucket", op="fast", le="0.005") == 1
    assert value_of(parsed, "lat_seconds_bucket", op="fast", le="+Inf") == 1
    # slow 的两次观测在 5 秒以下的桶里一个都不该有（9.0 落在 le="10" 这一桶）
    assert value_of(parsed, "lat_seconds_bucket", op="slow", le="5") == 0
    assert value_of(parsed, "lat_seconds_bucket", op="slow", le="10") == 2
    assert value_of(parsed, "lat_seconds_bucket", op="slow", le="+Inf") == 2


def test_a_single_observation_lands_in_every_bucket_up_to_its_own():
    """单次观测：它所在的桶**及右侧全部**都要 +1（le 语义的直接体现）。"""
    metrics.observe("one_seconds", 0.03)

    parsed = parse_exposition(metrics.render())
    values = {le: v for (labels, v, _) in samples_of(parsed, "one_seconds_bucket")
              for k, le in labels if k == "le"}
    assert values["0.005"] == 0
    assert values["0.01"] == 0
    assert values["0.025"] == 0
    assert values["0.05"] == 1        # 0.03 <= 0.05，落在这里
    assert values["30"] == 1          # 右侧全是累积
    assert values["+Inf"] == 1


def test_observe_zero_and_boundary_values_are_counted():
    """0 和"正好等于桶边界"的值都要进桶（le 是**小于等于**）。

    边界写成 `<` 而不是 `<=` 的话，恰好等于典型耗时的那些观测会
    整批掉进下一个桶 —— 分位数偏大，而且只在整数值上出现。
    """
    metrics.observe("edge_seconds", 0.0)
    metrics.observe("edge_seconds", 0.005)     # 正好等于第一个边界

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, "edge_seconds_bucket", le="0.005") == 2
    assert value_of(parsed, "edge_seconds_count") == 2
    assert value_of(parsed, "edge_seconds_sum") == pytest.approx(0.005)


# ============================================================
# 二、转义（生成器与解析器必须对同一套规则）
# ============================================================
def test_label_values_are_escaped_and_exactly_reversible():
    """反斜杠 / 双引号 / 换行按规范转义，且**逐字可还原**。

    只断言"看起来转义了"是不够的：转义顺序错（先转引号后转反斜杠）
    会让原值里的 `\\n` 变成 `\\\\n`，读回来多一个斜杠 ——
    这是"看起来只是多了一个字符"的那种错误。
    """
    original = 'a"b\\c\nd'          # 双引号 + 反斜杠 + 换行
    metrics.counter("esc_total", {"v": original})
    text = metrics.render()
    parsed = parse_exposition(text)

    assert 'v="a\\"b\\\\c\\nd"' in text, "转义后的形态不符合规范"
    # 快照里存的必须是**原始值**：转义只发生在渲染那一刻。
    # 若在入口就转义，内部计数、快照、将来的导出会层层叠加转义 ——
    # 而且 `\n` 会在内存里变成一个真的反斜杠加 n。
    assert metrics.snapshot()["esc_total"]["series"][0]["labels"]["v"] == original
    got = dict(samples_of(parsed, "esc_total")[0][0])["v"]
    assert _unescape(got) == original, f"往返失败：{got!r}"


def test_help_text_is_escaped_and_keeps_the_output_parseable():
    """HELP 文案里混进换行/反斜杠，不能把整份输出弄成坏文本。"""
    metrics.help_text("h_total", "行一\\n行二")
    metrics.counter("h_total")

    text = metrics.render()
    parse_exposition(text)
    assert '\\n' in text, "HELP 文案里的换行没有被转义"


def test_unicode_label_values_survive_the_round_trip():
    """中文 / emoji 标签值原样输出（Prometheus 文本格式是 UTF-8）。"""
    metrics.counter("zh_total", {"host": "数据库-01", "note": "磁盘🈵"})
    parsed = parse_exposition(metrics.render())
    labels = dict(samples_of(parsed, "zh_total")[0][0])
    assert labels["host"] == "数据库-01"
    assert labels["note"] == "磁盘🈵"


def test_empty_label_value_is_allowed_and_rendered_as_empty_string():
    """空标签值是合法的（`v=""`），不能被当成"没有标签"。"""
    metrics.counter("empty_total", {"reason": ""})
    text = metrics.render()
    parsed = parse_exposition(text)
    assert 'empty_total{reason=""} 1' in text
    assert dict(samples_of(parsed, "empty_total")[0][0])["reason"] == ""


def test_labels_none_and_empty_dict_are_the_same_series():
    """`labels=None` 与 `{}` 都是"没有标签"，必须归到同一条序列。"""
    metrics.counter("same_total", None, 2)
    metrics.counter("same_total", {}, 3)
    assert value_of(parse_exposition(metrics.render()), "same_total") == 5


# ============================================================
# 三、名字校验（编程错误必须立刻炸）
# ============================================================
@pytest.mark.parametrize("bad", [
    "1abc",              # 数字开头
    "a-b",               # 连字符
    "a b",               # 空格
    "",                  # 空
    "指标",              # 非 ASCII
    "a.b",               # 点号（Prometheus 里不是合法字符）
])
def test_invalid_metric_names_raise_value_error(bad):
    """非法指标名 → ValueError，而不是"清洗"成一个别的名字。

    静默改名最坏：调用方以为指标在上报，实际名字已经变成谁都搜不到的东西。
    """
    for call in (
        lambda: metrics.counter(bad),
        lambda: metrics.gauge(bad, 1),
        lambda: metrics.observe(bad, 0.1),
        lambda: metrics.help_text(bad, "x"),
    ):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("bad", [
    "1label", "a-b", "a b", "", "标签", "a:b",       # 标签名不允许 ':'
])
def test_invalid_label_names_raise_value_error(bad):
    """非法标签名 → ValueError。注意标签名**不允许** ':'（与指标名不同）。"""
    with pytest.raises(ValueError):
        metrics.counter("ok_total", {bad: "v"})


def test_non_string_label_value_is_rejected_not_coerced():
    """标签值不是字符串 → ValueError。

    悄悄 `str(500)` 的话，`{"code": 500}`（多半是漏了引号）和
    `{"code": "500"}` 会长得一模一样，测试里再也发现不了。
    """
    for bad in (500, 5.0, True, None, b"x"):
        with pytest.raises(ValueError):
            metrics.counter("ok_total", {"code": bad})


def test_labels_must_be_a_dict():
    with pytest.raises(ValueError):
        metrics.counter("ok_total", [("a", "b")])     # type: ignore[arg-type]


def test_mismatched_metric_type_raises_instead_of_writing_bad_text():
    """同一个名字先当 counter 再当 gauge → ValueError。

    混用会让文本里的 `# TYPE` 与实际语义打架，抓取端报的是
    "类型冲突"，离真正的原因很远。
    """
    metrics.counter("t_total")
    with pytest.raises(ValueError):
        metrics.gauge("t_total", 1)
    with pytest.raises(ValueError):
        metrics.observe("t_total", 0.1)

    metrics.reset()
    metrics.observe("t_seconds", 0.1)
    with pytest.raises(ValueError):
        metrics.counter("t_seconds")


def test_colons_are_allowed_in_metric_names_but_not_label_names():
    """指标名允许 ':'（record 规则产生），标签名不允许 —— 规范原文的差别。"""
    metrics.counter("job:rate:total", {"x": "1"})
    assert "job:rate:total" in metrics.render()
    with pytest.raises(ValueError):
        metrics.counter("ok_total", {"job:rate": "1"})


def test_non_numeric_values_are_rejected():
    """数值必须是 int/float（bool 也接受，按 1/0 计）—— 字符串一律拒绝。"""
    with pytest.raises(ValueError):
        metrics.counter("n_total", None, "5")        # type: ignore[arg-type]
    with pytest.raises(ValueError):
        metrics.gauge("n_gauge", None)               # type: ignore[arg-type]
    metrics.gauge("bool_gauge", True)
    assert value_of(parse_exposition(metrics.render()), "bool_gauge") == 1


# ============================================================
# 四、非有限值：丢弃 + 计数（NaN 会让抓取端整次失败）
# ============================================================
def test_non_finite_values_are_dropped_and_counted():
    """NaN / +Inf / -Inf 一律不进输出，丢弃计数各自 +1。

    一个 NaN 混进文本，Prometheus 端是**整次抓取失败** ——
    一个业务小 bug 能把整台机器的监控打瞎。丢样本 + 报数是对的那一边。
    """
    metrics.counter("bad_total", None, float("nan"))
    metrics.gauge("bad_gauge", float("inf"))
    metrics.observe("bad_seconds", float("-inf"))
    metrics.observe("bad_seconds", float("nan"))

    text = metrics.render()
    parsed = parse_exposition(text)          # 校验器会拒绝任何非有限值

    for name in ("bad_total", "bad_gauge", "bad_seconds"):
        assert name not in parsed, f"{name} 不该出现在输出里"
    # 输出里除了直方图的 `+Inf` 桶之外，不允许出现 nan/inf 字样
    assert "nan" not in text.lower(), text
    assert "inf" not in text.lower().replace("+inf", ""), text

    assert metrics.dropped(metrics.REASON_NONFINITE) == 4
    assert value_of(parsed, metrics.SELF_DROPPED, reason="nonfinite") == 4
    # 正常观测仍然要进去，丢弃不能连累合法数据
    metrics.observe("good_seconds", 0.1)
    assert value_of(parse_exposition(metrics.render()), "good_seconds_count") == 1


def test_a_dropped_non_finite_value_does_not_create_the_metric_at_all():
    """只被 NaN 碰过的指标不该出现在输出里（连 `# TYPE` 都不该有）。"""
    metrics.counter("never_total", None, float("inf"))
    text = metrics.render()
    assert "never_total" not in text
    assert metrics.snapshot() == {} or "never_total" not in metrics.snapshot()


def test_a_valid_observation_after_a_nan_still_works():
    """先丢一个 NaN、再记一个合法值：合法值必须正常进桶。"""
    metrics.observe("seq_seconds", float("nan"))
    metrics.observe("seq_seconds", 0.03)

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, "seq_seconds_count") == 1
    assert value_of(parsed, "seq_seconds_bucket", le="+Inf") == 1
    assert metrics.dropped(metrics.REASON_NONFINITE) == 1


# ============================================================
# 五、基数守卫（源头丢弃 + 如实报告）
# ============================================================
def test_series_beyond_the_limit_are_dropped_and_reported():
    """300 个不同标签值打同一个指标 → 只留 200 条，丢弃 100。

    标签基数爆炸的后果不是"数据多一点"，是 Prometheus 端先崩。
    在源头丢弃 + 报告丢弃数，比"看起来收集了很多、实际把监控打挂"安全。
    """
    for i in range(300):
        metrics.counter("card_total", {"i": str(i)})
    assert metrics.dropped(metrics.REASON_CARDINALITY) == 100

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, metrics.SELF_SERIES, metric="card_total") == 200
    assert value_of(parsed, metrics.SELF_DROPPED,
                    reason="cardinality") == 100
    assert len(samples_of(parsed, "card_total")) == 200


def test_series_count_gauge_tracks_every_metric_separately():
    """`agentdesk_metrics_series` 是**每个指标一行**的 gauge，不是总数。"""
    for i in range(5):
        metrics.counter("m_a_total", {"i": str(i)})
    metrics.gauge("m_b_gauge", 1, {"only": "one"})

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, metrics.SELF_SERIES, metric="m_a_total") == 5
    assert value_of(parsed, metrics.SELF_SERIES, metric="m_b_gauge") == 1


def test_exactly_at_the_limit_is_accepted_and_one_more_is_dropped():
    """边界（正好等于上限）必须被接受 —— 差一错误会让上限变成 199。"""
    for i in range(metrics.MAX_SERIES_PER_METRIC):
        metrics.gauge("bound_gauge", 1, {"i": str(i)})
    assert metrics.dropped(metrics.REASON_CARDINALITY) == 0

    metrics.gauge("bound_gauge", 1, {"i": "overflow"})
    assert metrics.dropped(metrics.REASON_CARDINALITY) == 1
    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, metrics.SELF_SERIES, metric="bound_gauge") == \
        metrics.MAX_SERIES_PER_METRIC


def test_existing_series_keep_updating_after_the_limit_is_reached():
    """到达上限后**已有**序列仍要正常工作，且不再产生新的丢弃。

    这条最容易写错：守卫写成"到上限就整体拒绝"，会让已经在看板上的
    序列突然停止更新 —— 而且没有任何错误提示。
    """
    for i in range(metrics.MAX_SERIES_PER_METRIC):
        metrics.counter("full_total", {"i": str(i)})
    dropped_before = metrics.dropped(metrics.REASON_CARDINALITY)

    for _ in range(10):
        metrics.counter("full_total", {"i": "0"})

    parsed = parse_exposition(metrics.render())
    # 首次写入建序列时为 1，再加 10 次 = 11 —— 关键是"到上限后仍能继续累加"
    assert value_of(parsed, "full_total", i="0") == 11
    assert metrics.dropped(metrics.REASON_CARDINALITY) == dropped_before


def test_the_guard_is_per_metric_not_global():
    """上限是**每个指标**各自的 —— 一个指标打满不该把别的指标挤掉。"""
    for i in range(metrics.MAX_SERIES_PER_METRIC + 50):
        metrics.counter("hog_total", {"i": str(i)})
    for i in range(3):
        metrics.counter("other_total", {"i": str(i)})

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, metrics.SELF_SERIES, metric="hog_total") == 200
    assert value_of(parsed, metrics.SELF_SERIES, metric="other_total") == 3


def test_non_finite_drops_and_cardinality_drops_are_counted_separately():
    """两类丢弃分别计数，绝不混成一个数 —— 否则守卫本身没法调参。"""
    metrics.counter("mix_total", None, float("nan"))
    metrics.counter("mix_total", {"i": "0"})     # 这一条要活下来
    for i in range(metrics.MAX_SERIES_PER_METRIC + 1):
        metrics.counter("mix_total", {"i": str(i)})

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, metrics.SELF_DROPPED, reason="nonfinite") == 1
    assert value_of(parsed, metrics.SELF_DROPPED, reason="cardinality") == 1
    assert metrics.dropped() == 2, "无参调用必须给出全部原因的合计"


# ============================================================
# 六、counter / gauge 语义
# ============================================================
def test_counter_accumulates_same_name_and_labels():
    metrics.counter("acc_total")
    metrics.counter("acc_total")
    metrics.counter("acc_total", None, 3.0)
    assert value_of(parse_exposition(metrics.render()), "acc_total") == 5


def test_counter_keeps_different_labels_separate():
    """不同标签是不同序列 —— 混成一个数会让所有维度分析失效。"""
    metrics.counter("by_total", {"path": "/a"})
    metrics.counter("by_total", {"path": "/b"}, 2)
    metrics.counter("by_total", {"path": "/a"})

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, "by_total", path="/a") == 2
    assert value_of(parsed, "by_total", path="/b") == 2


def test_gauge_overwrites_instead_of_accumulating():
    """gauge 是**覆盖式设置**。写成累加会让"当前队列长度"变成历史总和 ——
    这种错在第一次看板上表现为"数字一直涨"，但那时已经信了它很久。
    """
    metrics.gauge("depth_gauge", 7, {"q": "a"})
    metrics.gauge("depth_gauge", 3, {"q": "a"})
    metrics.gauge("depth_gauge", 0, {"q": "a"})

    assert value_of(parse_exposition(metrics.render()), "depth_gauge", q="a") == 0


def test_counter_and_gauge_with_different_labels_are_independent():
    metrics.gauge("g2_gauge", 1, {"q": "a"})
    metrics.gauge("g2_gauge", 9, {"q": "b"})
    assert value_of(parse_exposition(metrics.render()), "g2_gauge", q="a") == 1


def test_gauge_accepts_negative_values():
    """gauge 允许负数（差值类指标天然可以为负），要能正常渲染。"""
    metrics.gauge("delta_gauge", -2.5)
    assert value_of(parse_exposition(metrics.render()), "delta_gauge") == -2.5


def test_float_formatting_drops_meaningless_trailing_zeros():
    """整数写整数（`5` 而不是 `5.0`）—— 计数类指标不该看起来是浮点。"""
    metrics.counter("fmt_total", None, 5.0)
    metrics.gauge("fmt_gauge", 1e3)
    metrics.observe("fmt_seconds", 0.5)

    text = metrics.render()
    assert "fmt_total 5\n" in text, text
    assert "fmt_total 5.0" not in text
    assert "fmt_gauge 1000\n" in text, text
    assert "fmt_seconds_count 1\n" in text, text
    # 真小数必须保留（不能被格式化成整数）
    assert "fmt_seconds_sum 0.5\n" in text, text


# ============================================================
# 七、snapshot / reset（调试视图）
# ============================================================
def test_snapshot_is_structured_and_sorted_and_excludes_self_metrics():
    """快照是给人看的调试视图：结构完整、顺序确定、不含自身指标。

    自身指标是 render() 时算出来的派生物，放进快照会让人以为
    它们也是"被记录"的数据。
    """
    metrics.counter("s_total", {"b": "2"})
    metrics.counter("s_total", {"a": "1"})
    metrics.observe("s_seconds", 0.02, {"op": "x"})
    metrics.help_text("s_total", "help me")

    snap = metrics.snapshot()
    assert set(snap) == {"s_total", "s_seconds"}
    assert snap["s_total"]["type"] == "counter"
    assert snap["s_total"]["help"] == "help me"
    # 快照里的序列按**标签值**排序（"1" < "2"），顺序确定
    assert [row["labels"] for row in snap["s_total"]["series"]] == \
        [{"a": "1"}, {"b": "2"}]
    hist = snap["s_seconds"]["series"][0]
    assert hist["labels"] == {"op": "x"}
    assert hist["count"] == 1
    assert hist["sum"] == pytest.approx(0.02)
    assert hist["buckets"][-1] == {"le": "+Inf", "count": 1}
    assert metrics.SELF_SERIES not in snap


def test_reset_clears_everything_including_drop_counters():
    """reset() 之后快照为空、丢弃计数归零。

    丢弃计数不清零的话，"这条用例丢了几个"会混进上一条用例留下的数字，
    断言就退化成"大于等于" —— 那种断言永远不会因为回归变红。
    """
    metrics.counter("r_total", {"i": "1"})
    metrics.counter("r_other", None, float("nan"))
    metrics.help_text("r_total", "x")
    assert metrics.snapshot()

    metrics.reset()

    assert metrics.snapshot() == {}
    assert metrics.dropped() == 0
    # 自身指标仍然在（见 render() 的说明：空输出 = 无法区分"没数据"和"层没工作"），
    # 但用户指标一个都不该剩；series 这个族在没有指标时没有样本可言，不输出
    parsed = parse_exposition(metrics.render())
    assert set(parsed) == {metrics.SELF_DROPPED}
    assert value_of(parsed, metrics.SELF_DROPPED, reason="cardinality") == 0
    assert value_of(parsed, metrics.SELF_DROPPED, reason="nonfinite") == 0


def test_snapshot_returns_a_copy_not_a_live_view():
    """快照必须是**拷贝**：改它不能反过来改注册表（否则调试动作会污染数据）。"""
    metrics.counter("copy_total", {"a": "1"}, 2)
    snap = metrics.snapshot()
    snap["copy_total"]["series"][0]["value"] = 999
    snap["copy_total"]["series"][0]["labels"]["a"] = "mutated"

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, "copy_total", a="1") == 2


# ============================================================
# 八、线程安全（FastAPI 的同步接口真的跑在线程池里）
# ============================================================
def test_concurrent_counters_lose_nothing():
    """8 线程 × 200 次 → 总数必须是 1600（一个都不能丢）。"""
    threads = [threading.Thread(target=lambda: [metrics.counter("c_total")  # type: ignore[func-returns-value]
                                                for _ in range(200)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    parsed = parse_exposition(metrics.render())
    assert value_of(parsed, "c_total") == 1600


def test_concurrent_writers_on_many_label_sets_are_consistent():
    """8 线程各写自己的一组标签：每组数值正确，且没有多余序列。"""
    def work(worker):
        for i in range(200):
            metrics.counter("multi_total", {"worker": str(worker)})
            metrics.gauge("multi_gauge", i, {"worker": str(worker)})
            metrics.observe("multi_seconds", 0.001 * i, {"worker": str(worker)})

    threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    parsed = parse_exposition(metrics.render())
    assert len(samples_of(parsed, "multi_total")) == 8
    for w in range(8):
        assert value_of(parsed, "multi_total", worker=str(w)) == 200
        assert value_of(parsed, "multi_gauge", worker=str(w)) == 199
        assert value_of(parsed, "multi_seconds_count", worker=str(w)) == 200


def test_threads_racing_on_the_same_new_series_agree_on_its_value():
    """多线程同时首次创建**同一条**序列：只能有一条，数值是总和。

    "先查再插"没有锁保护的话，会创建出多条同标签序列，
    文本里就出现重复序列 —— 抓取端直接报错。
    """
    def work():
        for _ in range(100):
            metrics.counter("race_total", {"k": "v"})

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    parsed = parse_exposition(metrics.render())
    assert len(samples_of(parsed, "race_total")) == 1
    assert value_of(parsed, "race_total", k="v") == 800


def test_render_and_snapshot_can_run_while_writers_are_active():
    """写入与导出并发时不能抛异常，导出的文本仍必须整体合规。

    这一条防的是"读到写了一半的结构"：注册表用普通 dict 而不是
    defaultdict / 计数器对象，就是为了让"部分更新"在锁外不可能被看见。
    """
    stop = threading.Event()
    errors = []

    def writer():
        try:
            n = 0
            while not stop.is_set():
                n += 1
                metrics.counter("rw_total", {"i": str(n % 50)})
                metrics.observe("rw_seconds", 0.01 * (n % 20))
        except Exception as e:            # pragma: no cover - 失败时才走到
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for _ in range(20):
            parse_exposition(metrics.render())
            metrics.snapshot()
    finally:
        stop.set()
        for t in threads:
            t.join()

    assert not errors, errors


# ============================================================
# 九、校验器自身的可信度
# ============================================================
@pytest.mark.parametrize("bad_text", [
    "no_type_total 1\n",                                   # 缺 # TYPE
    "# TYPE x_total counter\n",                            # 有 TYPE 无样本
    '# TYPE x_total counter\nx_total{a="1" 1\n',           # 花括号没闭合
    '# TYPE x_total counter\nx_total{a=1} 1\n',            # 标签值没有引号
    '# TYPE x_total counter\nx_total 1.0.0\n',             # 值不是小数字面量
    '# TYPE x_total counter\nx_total 1',                   # 结尾缺换行
    '# TYPE x_total counter\nx_total{a="1",a="2"} 1\n',    # 标签名重复
    '# TYPE h_seconds histogram\nh_seconds_count 1\n',     # 直方图没有桶
])
def test_the_validator_rejects_broken_text(bad_text):
    """校验器必须真的会红 —— 一个永远通过的校验器等于没有校验。

    每一条都是**真实可能写出来**的坏文本（少换行、没转义的引号、
    重复标签…）。如果哪天 render() 退化成产出这些形态，
    上面的用例会红，而不是被一个宽松的校验器放过。
    """
    with pytest.raises(AssertionError):
        parse_exposition(bad_text)


@pytest.mark.parametrize("bad_text", [
    # 桶递减（累积语义被写反了）
    '# TYPE h_seconds histogram\n'
    'h_seconds_bucket{le="1"} 3\n'
    'h_seconds_bucket{le="+Inf"} 2\n'
    'h_seconds_count 2\n',
    # _count 与 +Inf 桶对不上
    '# TYPE h_seconds histogram\n'
    'h_seconds_bucket{le="1"} 1\n'
    'h_seconds_bucket{le="+Inf"} 1\n'
    'h_seconds_count 5\n',
    # 最后一个桶不是 +Inf
    '# TYPE h_seconds histogram\n'
    'h_seconds_bucket{le="1"} 1\n'
    'h_seconds_bucket{le="2"} 1\n'
    'h_seconds_count 1\n',
])
def test_the_validator_rejects_broken_histograms(bad_text):
    """直方图的三条硬要求（单调、+Inf 收尾、count 对账）各有一条反例。"""
    with pytest.raises(AssertionError):
        parse_exposition(bad_text)
