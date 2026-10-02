# -*- coding: utf-8 -*-
"""JSONL 轮转与跨文件回读。

这一层的核心不是"把文件切小"，而是两条**很容易做错**的语义：

    ① **轮转之后，"最近 N 条"仍然要能跨文件读到**
       否则刚轮转完，看板上就变成"最近没有任何记录" —— 比不轮转更糟。

    ② **状态日志绝不能轮转**
       `approvals.jsonl` / `incidents.jsonl` 是启动时**整份折叠**成状态的，
       把一部分改名挪走 = 静默丢掉那些单据的状态
       （已批准的单子会变回"不存在"，重放保护直接失效）。
       这条在本文件里是用例守着的 —— 它是一条**设计约束**，不是实现细节。

还有一条：**不删历史**。到保留上限的一份被移进 `archive/`，而不是被删掉。
磁盘仍会涨，但涨得看得见，而且没有东西被悄悄销毁。
"""

import json

import pytest

from app.observability import jsonl


def _write(path, n, start=0, pad=0):
    for i in range(start, start + n):
        jsonl.append_jsonl(path, {"i": i, "pad": "x" * pad}, limit_bytes=10 ** 9)


# ============================================================
# 一、基本写入
# ============================================================
def test_append_creates_parent_dir_and_one_line_per_record(tmp_path):
    path = tmp_path / "deep" / "a.jsonl"
    jsonl.append_jsonl(path, {"a": 1})
    jsonl.append_jsonl(path, {"a": 2})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}


def test_append_keeps_chinese_readable(tmp_path):
    path = tmp_path / "a.jsonl"
    jsonl.append_jsonl(path, {"msg": "磁盘写满"})
    assert "磁盘写满" in path.read_text(encoding="utf-8")


# ============================================================
# 二、轮转
# ============================================================
def test_rotate_renames_instead_of_deleting(tmp_path):
    """★ 轮转是**改名**，不是删除。"""
    path = tmp_path / "t.jsonl"
    _write(path, 5)
    assert jsonl.maybe_rotate(path, limit_bytes=1, keep=5) is True
    assert not path.exists() or path.stat().st_size == 0
    first = tmp_path / "t.jsonl.1"
    assert first.exists(), "旧内容应当被改名成 .1"
    assert len(first.read_text(encoding="utf-8").splitlines()) == 5
    assert jsonl.size_of(first) > 0


def test_no_rotation_below_threshold(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, 3)
    assert jsonl.maybe_rotate(path, limit_bytes=10 ** 9, keep=5) is False
    assert not (tmp_path / "t.jsonl.1").exists()


def test_rotation_shifts_older_files_along(tmp_path):
    """`.1` → `.2`、`.2` → `.3` …… 顺序不能搞反（搞反就是覆盖，会丢数据）。

    每轮写**内容不同**的记录，否则"逐格后移"和"直接覆盖"在文件内容上看不出区别 ——
    那样这条用例就白写了。
    """
    path = tmp_path / "t.jsonl"
    for round_no in range(3):
        _write(path, 2, start=round_no * 100)
        jsonl.maybe_rotate(path, limit_bytes=1, keep=5)

    assert [json.loads((tmp_path / f"t.jsonl.{i}").read_text(encoding="utf-8").splitlines()[0])["i"]
            for i in (1, 2, 3)] == [200, 100, 0], \
        "最旧的应当在 .3、最新的在 .1（顺序反了就是覆盖，会丢数据）"


def test_overflow_is_archived_not_deleted(tmp_path):
    """★ 保留份数满了之后：最旧的一份进 `archive/`，**绝不删除**。

    "保留 5 份"这句话躲不开"第 6 份怎么办"。删掉最旧的 = 程序替人销毁审计数据 ——
    这个项目不做这种事。所以移到 archive/，并且写一条警告（磁盘仍会涨，但看得见）。
    """
    path = tmp_path / "t.jsonl"
    for round_no in range(4):
        _write(path, 2, start=round_no * 100)
        jsonl.maybe_rotate(path, limit_bytes=1, keep=2)

    archive = tmp_path / "archive"
    assert archive.is_dir(), "超出份数的文件应当进 archive/"
    archived = list(archive.iterdir())
    assert archived, "archive 里应当有被移走的那一份"
    assert all(f.stat().st_size > 0 for f in archived)
    # 保留份数仍然是 keep
    assert (tmp_path / "t.jsonl.1").exists() and (tmp_path / "t.jsonl.2").exists()
    assert not (tmp_path / "t.jsonl.3").exists(), "第 3 份应当已被移走而不是留在原地"


# ============================================================
# 三、跨文件回读（这条是轮转能不能上线的关键）
# ============================================================
def test_read_tail_reads_from_current_file(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, 10)
    got, meta = jsonl.read_tail(path, 3)
    assert [r["i"] for r in got] == [7, 8, 9]
    assert meta["bad_lines"] == 0 and meta["files"] == ["t.jsonl"]


def test_read_tail_reaches_into_rotated_files(tmp_path):
    """★ 刚轮转完必须还能读到"最近 N 条"，否则看板会突然变成"没有数据"。"""
    path = tmp_path / "t.jsonl"
    _write(path, 3, start=0)                     # 0,1,2
    jsonl.maybe_rotate(path, limit_bytes=1, keep=5)
    _write(path, 2, start=100)                   # 100,101（轮转后写进新文件）

    got, meta = jsonl.read_tail(path, 4)         # 最新 4 条 = 1,2,100,101
    assert [r["i"] for r in got] == [1, 2, 100, 101]
    assert "t.jsonl" in meta["files"] and "t.jsonl.1" in meta["files"]
    assert meta["bad_lines"] == 0


def test_read_tail_orders_oldest_to_newest_across_files(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, 2, start=0)
    jsonl.maybe_rotate(path, limit_bytes=1, keep=5)
    _write(path, 2, start=50)
    got, _ = jsonl.read_tail(path, 10)
    assert [r["i"] for r in got] == [0, 1, 50, 51]


def test_read_tail_with_more_files_than_records_is_fine(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, 1)
    got, meta = jsonl.read_tail(path, 100)
    assert [r["i"] for r in got] == [0]
    assert meta["truncated"] is True, "凑不够条数要如实标出来"


def test_read_tail_reports_bad_lines_instead_of_hiding_them(tmp_path):
    """★ 坏行要**报出来**，不能只跳过。

    原先是"读不出来就 return []"，于是看板静默显示"最近没有数据" ——
    而"没有数据"和"读失败"是两件事。**静默的错答案比报错危险。**
    """
    path = tmp_path / "t.jsonl"
    jsonl.append_jsonl(path, {"i": 0})
    with open(path, "a", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON\n")
    jsonl.append_jsonl(path, {"i": 1})

    got, meta = jsonl.read_tail(path, 10)
    assert [r["i"] for r in got] == [0, 1]
    assert meta["bad_lines"] == 1, "坏行数必须如实报出来"


def test_read_tail_missing_file_is_empty_not_an_error(tmp_path):
    got, meta = jsonl.read_tail(tmp_path / "nope.jsonl", 5)
    assert got == [] and meta["files"] == [] and meta["bad_lines"] == 0


def test_read_tail_zero_limit(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, 3)
    assert jsonl.read_tail(path, 0) == ([], {"scanned": 0, "bad_lines": 0,
                                            "truncated": False, "files": []})


def test_read_tail_survives_a_torn_line_at_the_byte_cap(tmp_path):
    """从中间截断时，第一条可能是半截 JSON —— 必须丢掉它，而不是算成坏行。"""
    path = tmp_path / "t.jsonl"
    _write(path, 40, pad=500)                    # 每行约 520 字节
    got, meta = jsonl.read_tail(path, 3, tail_bytes=1800)   # ≈3.4 行 → 丢半截后刚好 3 行
    assert len(got) == 3
    assert meta["truncated"] is True
    assert meta["bad_lines"] == 0, "半截行是**读的时候截断**造成的，不该记成坏数据"
    assert [r["i"] for r in got] == [37, 38, 39]


# ============================================================
# 四、状态日志不能轮转（设计约束，不是实现细节）
# ============================================================
def test_state_logs_must_not_use_this_writer():
    """★ 这条用例守的是一条**设计约束**：

    `approvals.jsonl` 与 `incidents.jsonl` 是启动时整份折叠成状态的。
    它们一旦轮转，`_load` 就只能看到一部分历史 ——
    那些"只存在于被挪走的那一段里"的事件会消失：
    一张已批准的单子可能变回不存在（**重放保护失效**），
    一个已结单的事件可能变回 open。

    所以本模块的轮转只给"读侧只关心尾部"的事件流用。
    这里用源码级断言把这条约束固定下来：那两个 store 不许 import 本模块的写路径。
    """
    from pathlib import Path

    import app.incident.store as incident_store
    import app.sandbox.approvals as approvals

    for mod in (approvals, incident_store):
        src = Path(mod.__file__ or "").read_text(encoding="utf-8")
        assert "jsonl.append_jsonl" not in src, \
            f"{mod.__name__} 是折叠式状态日志，绝不能走轮转写路径"
        assert "maybe_rotate" not in src, \
            f"{mod.__name__} 不能触发轮转（会把状态一起挪走）"


# ============================================================
# 五、配置读取要容错
# ============================================================
def test_env_parsing_defaults_and_fallback(monkeypatch):
    assert jsonl.max_bytes({}) == jsonl.DEFAULT_MAX_BYTES
    assert jsonl.keep_count({}) == jsonl.DEFAULT_KEEP
    assert jsonl.max_bytes({"LOG_MAX_BYTES": "2048"}) == 2048
    assert jsonl.keep_count({"LOG_KEEP": "2"}) == 2
    # 非法值：回退默认，而不是让服务起不来
    assert jsonl.max_bytes({"LOG_MAX_BYTES": "ten mb"}) == jsonl.DEFAULT_MAX_BYTES
    assert jsonl.keep_count({"LOG_KEEP": "-3"}) == 1
    assert jsonl.max_bytes({"LOG_MAX_BYTES": "10"}) == 1024, "下限要兜住"


def test_rotated_paths_are_newest_first(tmp_path):
    path = tmp_path / "t.jsonl"
    got = jsonl.rotated_paths(path, keep=3)
    assert [p.name for p in got] == ["t.jsonl.1", "t.jsonl.2", "t.jsonl.3"]


@pytest.mark.parametrize("keep", [1, 2, 5])
def test_keep_is_respected(tmp_path, keep):
    path = tmp_path / "t.jsonl"
    _write(path, 1)
    jsonl.maybe_rotate(path, limit_bytes=1, keep=keep)
    assert (tmp_path / "t.jsonl.1").exists()
    assert not (tmp_path / f"t.jsonl.{keep + 1}").exists()
