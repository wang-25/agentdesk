# -*- coding: utf-8 -*-
"""结构化日志层 —— 保证"补上观测能力"没有偷偷改变现有观感。

用例密度集中在四件事上：

    · **默认档不变观感**：`setup_logging({})` 必须是 text + INFO。
      补日志配置最容易出的问题不是"没日志"，而是"日志变了样子、
      半夜排查的人发现 docker logs 读不出来了"。
    · **request_id 不串号**：它是排查的绳子。串号的 request_id 比没有
      更糟 —— 它会把排查引向错误的请求，而且看不出来。
    · **幂等**：setup 会被 main.py 调一次、被测试调很多次。
      重复挂 handler 的症状是"每条日志打两遍"，很容易被当成业务 bug。
    · **坏事不许逃出去**：写错环境变量、extra 里塞了不可序列化的对象、
      参数插值写错了 —— 一律降级，不许抛。

★ 断言不看真实 stdout/stderr：格式用 `Formatter.format(record)` 直接验，
  集成路径用挂在 root 上的 `_Sink` 收记录（caplog 同类做法）。
  测试进程的 stderr 只允许"被 pytest 捕获"，不允许被断言。

★ 日志层是**进程级全局状态**。`_isolate_logging` 在每个用例前后把
  root logger 恢复原状，否则后一个用例会继承前一个的 handler/level，
  "默认档"这类断言就变成了看执行顺序碰运气。
"""

import json
import logging
import re
import sys
import threading
from datetime import datetime

import pytest

from app.observability import logsetup
from app.observability.logsetup import (
    JsonFormatter,
    RequestIdFilter,
    TextFormatter,
    clear_request_id,
    get_request_id,
    new_request_id,
    set_request_id,
    setup_logging,
)

SUMMARY_KEYS = {"format", "level", "handlers", "request_id", "note"}


# ============================================================
# 装置
# ============================================================
class _Sink(logging.Handler):
    """把记录收进列表的 handler —— 不往真实 stdout/stderr 写。

    直接 `addHandler` 到 root，可以看到"业务 logger 打的记录真的走到
    root 了吗、级别闸门放行了吗"，而且不依赖 caplog 的实现细节。
    """

    def __init__(self):
        super().__init__()
        self.addFilter(RequestIdFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def sink():
    """挂在 root 上的记录收集器。"""
    handler = _Sink()
    logging.getLogger().addHandler(handler)
    try:
        yield handler
    finally:
        logging.getLogger().removeHandler(handler)


@pytest.fixture(autouse=True)
def _isolate_logging():
    """用例前后复原 root logger 与请求上下文（见模块 docstring 的说明）。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    clear_request_id()
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if getattr(handler, logsetup.HANDLER_MARK, False):
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        # 模块级的登记表也要清：否则下一个用例里 `_installed` 还记着
        # 已经不在 root 上的 handler，摘要里的 handlers 数就不真实了。
        logsetup._installed[:] = []
        clear_request_id()


def _our_handlers() -> list[logging.Handler]:
    """root 上属于本模块的 handler（靠身份标记区分，不靠位置猜）。"""
    return [h for h in logging.getLogger().handlers
            if getattr(h, logsetup.HANDLER_MARK, False)]


def _make_record(msg="消息", level=logging.INFO, name="agentdesk.demo",
                 args=None, exc_info=None, extra=None, rid=None):
    """造一条 record。`request_id` 平时由 RequestIdFilter 盖，这里手动盖以便单测 formatter。"""
    record = logging.LogRecord(name, level, __file__, 42, msg, args or (), exc_info,
                               func="unit_test")
    record.request_id = rid or ""
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _raise_and_capture(exc: Exception):
    """跑出一个异常并返回 record 用的 `exc_info`（必须在 except 块里取）。"""
    try:
        raise exc
    except Exception:
        return sys.exc_info()


def _read_id_in_helper() -> str:
    """模拟"深处的业务函数"：不接收任何参数，直接读上下文。"""
    return get_request_id()


class _ExplodingEnv(dict):
    """一个"取值就炸"的环境表：模拟配置来源本身坏掉。"""

    def get(self, key, default=None):
        raise RuntimeError("环境表坏了")


# ============================================================
# 一、默认档与摘要
# ============================================================
def test_default_summary_and_handler_are_text_info():
    """`setup_logging({})` 必须是 text + INFO —— 默认档不变观感。"""
    summary = setup_logging({})
    assert set(summary) == SUMMARY_KEYS
    assert summary["format"] == "text"
    assert summary["level"] == "INFO"
    assert summary["handlers"] == 1
    assert summary["request_id"] is True
    assert summary["note"]

    handlers = _our_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, TextFormatter)
    assert logging.getLogger().level == logging.INFO

    # 多一个不认识的环境变量（名字拼错）不该改变任何东西，也不该报错
    assert setup_logging({"LOG_LEVL": "DEBUG"})["level"] == "INFO"
    assert len(_our_handlers()) == 1


def test_injected_env_wins_over_process_environ_and_is_normalized(monkeypatch):
    """注入 dict 时必须**只用这个 dict** —— 半读注入、半读进程环境 = 用例看天吃饭。"""
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert setup_logging({})["format"] == "text"
    assert setup_logging({})["level"] == "INFO"

    summary = setup_logging({"LOG_FORMAT": " JSON ", "LOG_LEVEL": " debug "})
    assert (summary["format"], summary["level"]) == ("json", "DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_env_none_reads_process_environ(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    summary = setup_logging()
    assert (summary["format"], summary["level"]) == ("json", "ERROR")


def test_json_env_switches_handler_formatter_and_extra_flag():
    setup_logging({"LOG_FORMAT": "json"})
    formatter = _our_handlers()[0].formatter
    assert isinstance(formatter, JsonFormatter)
    # ensure_ascii=False 是中文日志能读的前提（见 json 用例）
    assert formatter.ensure_ascii is False and formatter.include_extra is True

    summary = setup_logging({"LOG_FORMAT": "json", "LOG_JSON_EXTRA": "0"})
    fmt = _our_handlers()[0].formatter
    assert getattr(fmt, "include_extra", None) is False
    assert "LOG_JSON_EXTRA=0" in summary["note"]
    assert len(_our_handlers()) == 1


# ============================================================
# 二、text 格式（默认档）
# ============================================================
def test_text_format_is_time_level_logger_message():
    line = TextFormatter().format(_make_record("磁盘写满"))
    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO agentdesk\.demo 磁盘写满$", line
    ), line
    assert "[req=" not in line          # 没有请求上下文就不该出现空壳标记

    stamped = TextFormatter().format(_make_record("磁盘写满", rid="3f9a1c02"))
    assert stamped.endswith("磁盘写满 [req=3f9a1c02]")


def test_text_format_keeps_traceback_for_exceptions():
    record = _make_record("处置失败", level=logging.ERROR,
                          exc_info=_raise_and_capture(ValueError("boom-text")))
    line = TextFormatter().format(record)
    assert "Traceback (most recent call last)" in line
    assert "ValueError: boom-text" in line
    # 首行仍是标准格式（traceback 在后续行）
    assert line.splitlines()[0].endswith("处置失败")


def test_text_formatter_does_not_leave_temp_field_on_record():
    """临时字段必须清干净 —— 否则同一 record 走到 json formatter 时会多出一个假业务字段。"""
    record = _make_record("x", rid="aaaaaaaa")
    TextFormatter().format(record)
    assert not hasattr(record, "reqpart")
    assert "reqpart" not in json.loads(JsonFormatter().format(record))


def test_real_handler_prints_request_id_in_text_line(sink):
    """端到端：中间件 set 的 id，要真的出现在我们那个 handler 的输出行里。"""
    setup_logging({})
    set_request_id("cafe1234")
    logging.getLogger("agentdesk.demo").warning("磁盘写满")
    line = _our_handlers()[0].format(sink.records[-1])
    assert line.endswith("WARNING agentdesk.demo 磁盘写满 [req=cafe1234]"), line


def test_level_gate_actually_filters_records(sink):
    setup_logging({"LOG_LEVEL": "WARNING"})
    log = logging.getLogger("agentdesk.demo")
    log.info("不该出现")
    log.warning("该出现")
    assert [r.getMessage() for r in sink.records] == ["该出现"]


# ============================================================
# 三、json 格式
# ============================================================
def test_json_line_is_valid_json_with_full_keys():
    line = JsonFormatter(include_extra=False).format(_make_record("磁盘写满", rid="beef0001"))
    assert "\n" not in line
    payload = json.loads(line)
    assert set(payload) == {"ts", "level", "logger", "msg", "request_id"}
    assert payload["level"] == "INFO" and payload["logger"] == "agentdesk.demo"
    assert payload["msg"] == "磁盘写满" and payload["request_id"] == "beef0001"


def test_json_omits_request_id_key_when_absent():
    """没有请求上下文时**不写这个键**，而不是写成空串。"""
    payload = json.loads(JsonFormatter().format(_make_record("启动完成")))
    assert "request_id" not in payload


def test_json_ts_is_iso8601_with_timezone_and_msg_interpolates_args():
    payload = json.loads(JsonFormatter().format(_make_record("剩余 %s 次", args=("3",))))
    parsed = datetime.fromisoformat(payload["ts"])
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    assert payload["msg"] == "剩余 3 次"


def test_json_keeps_chinese_readable():
    """ensure_ascii=False 的验收点：中文不许变成 \\uXXXX。"""
    line = JsonFormatter().format(_make_record("磁盘写满，清理日志"))
    assert "磁盘写满，清理日志" in line
    assert "\\u" not in line
    assert json.loads(line)["msg"] == "磁盘写满，清理日志"


def test_json_exception_field_is_a_string_with_type_and_message():
    record = _make_record("处置失败", level=logging.ERROR,
                          exc_info=_raise_and_capture(KeyError("boom-json")))
    payload = json.loads(JsonFormatter().format(record))
    assert isinstance(payload["exc_info"], str)
    assert "Traceback (most recent call last)" in payload["exc_info"]
    assert "KeyError: 'boom-json'" in payload["exc_info"]
    assert len(payload["exc_info"].splitlines()) > 1


def test_json_extra_can_be_toggled():
    rich = json.loads(JsonFormatter(include_extra=True).format(
        _make_record("x", extra={"trace_id": "tr-abc"})))
    assert rich["trace_id"] == "tr-abc"             # extra={...} 带进来的业务字段
    assert rich["thread"] and rich["func"] == "unit_test" and rich["line"] == 42

    lean = json.loads(JsonFormatter(include_extra=False).format(
        _make_record("x", extra={"trace_id": "tr-abc"})))
    assert set(lean) == {"ts", "level", "logger", "msg"}
    assert "trace_id" not in lean and "thread" not in lean


def test_json_survives_hostile_extra():
    """三种"编不出来"的情况都不许丢记录 —— 它们往往正是出错那条。"""
    # ① extra 里塞了对象 → 降级成字符串
    payload = json.loads(JsonFormatter().format(
        _make_record("x", extra={"obj": object(), "trace_id": "tr-1"})))
    assert isinstance(payload["obj"], str) and payload["trace_id"] == "tr-1"

    # ② 循环引用 → 回落到核心字段 + log_error
    loop: dict = {}
    loop["self"] = loop
    payload = json.loads(JsonFormatter().format(_make_record("x", extra={"loop": loop})))
    assert payload["msg"] == "x" and payload["level"] == "INFO" and "log_error" in payload

    # ③ 插值参数写错（%s 少给了）→ 只降级成"原始消息 + 原因"
    payload = json.loads(JsonFormatter().format(_make_record("失败: %s %s", args=("只有一个",))))
    assert "参数插值失败" in payload["msg"]


def test_real_json_handler_emits_one_json_line_per_record(sink):
    """端到端：json 档下真实 handler 的输出必须逐行可 json.loads。"""
    setup_logging({"LOG_FORMAT": "json", "LOG_JSON_EXTRA": "0"})
    set_request_id("beef0001")
    logging.getLogger("agentdesk.demo").error("磁盘写满 msg=%s", "x")
    line = _our_handlers()[0].format(sink.records[-1])
    assert len(line.splitlines()) == 1
    payload = json.loads(line)
    assert payload["msg"] == "磁盘写满 msg=x" and payload["request_id"] == "beef0001"
    assert set(payload) == {"ts", "level", "logger", "msg", "request_id"}


# ============================================================
# 四、request_id：contextvar 与线程隔离
# ============================================================
def test_new_request_id_is_short_hex_and_unique():
    ids = {new_request_id() for _ in range(200)}
    assert all(re.fullmatch(r"[0-9a-f]{8}", rid) for rid in ids)
    assert len(ids) > 190           # 碰撞概率 ~5e-6，留一点余量避免假红


def test_request_id_set_get_clear_roundtrip_across_calls():
    clear_request_id()
    assert get_request_id() == ""
    set_request_id("aaaa1111")
    assert get_request_id() == "aaaa1111"
    assert _read_id_in_helper() == "aaaa1111"       # 跨函数、不用传参
    clear_request_id()
    assert get_request_id() == "" and _read_id_in_helper() == ""


def test_request_id_filter_stamps_record_and_always_passes():
    filter_ = RequestIdFilter()
    record = _make_record()
    set_request_id("abcd1234")
    assert filter_.filter(record) is True            # 只盖章，不丢记录
    assert record.request_id == "abcd1234"
    clear_request_id()
    assert filter_.filter(record) is True
    assert record.request_id == ""


def test_request_id_does_not_leak_between_threads():
    """两个线程各 set 各的：互相读不到对方的，也不污染主线程。"""
    set_request_id("main0001")
    barrier = threading.Barrier(2, timeout=5)
    seen: dict = {}

    def worker():
        # 新线程从**空上下文**开始 —— 这正是"不继承上游请求 id"的保证
        seen["fresh"] = get_request_id()
        set_request_id("thr00001")
        barrier.wait()
        seen["inside"] = get_request_id()
        stamped = _make_record()
        RequestIdFilter().filter(stamped)
        seen["stamped"] = stamped.request_id
        barrier.wait()
        seen["after_main_changed"] = get_request_id()

    thread = threading.Thread(target=worker, name="req-id-test")
    thread.start()
    barrier.wait()
    seen["main_while_worker_active"] = get_request_id()
    set_request_id("main0002")          # 主线程改自己的，不许影响子线程
    barrier.wait()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert seen["fresh"] == ""
    assert seen["inside"] == "thr00001"
    assert seen["stamped"] == "thr00001"
    assert seen["main_while_worker_active"] == "main0001"
    assert seen["after_main_changed"] == "thr00001"   # 子线程不受主线程改动影响
    assert get_request_id() == "main0002"             # 子线程的 set 也没泄漏回来


# ============================================================
# 五、幂等：不许重复挂 handler
# ============================================================
def test_repeated_setup_is_idempotent_and_switches_format():
    root = logging.getLogger()
    first = setup_logging({})
    count, handler = len(root.handlers), _our_handlers()[0]

    second = setup_logging({})
    assert first["handlers"] == second["handlers"] == 1
    assert len(root.handlers) == count                  # 总数没涨
    assert len(_our_handlers()) == 1
    assert _our_handlers()[0] is not handler            # 换新的，不是又挂一个
    assert len({id(h) for h in root.handlers}) == len(root.handlers)   # 没有重复项

    setup_logging({"LOG_FORMAT": "json"})
    switched = setup_logging({"LOG_FORMAT": "text"})
    assert len(_our_handlers()) == 1 and switched["handlers"] == 1
    assert isinstance(_our_handlers()[0].formatter, TextFormatter)


def test_setup_never_removes_foreign_root_handlers():
    """只回收自己挂的 —— pytest/caplog、别人加的文件 handler 一律不碰。"""
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        setup_logging({})
        assert foreign in root.handlers
        setup_logging({"LOG_FORMAT": "json"})
        assert foreign in root.handlers
        assert len(_our_handlers()) == 1
    finally:
        root.removeHandler(foreign)


# ============================================================
# 六、非法配置与异常兜底
# ============================================================
def test_invalid_level_falls_back_to_info_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=logsetup.LOGGER_NAME):
        summary = setup_logging({"LOG_LEVEL": "VERBOSE"})
    assert summary["level"] == "INFO"
    assert logging.getLogger().level == logging.INFO      # 服务照常起来
    assert "VERBOSE" in summary["note"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("LOG_LEVEL" in r.getMessage() for r in warnings)

    # 能用的写法（别名 / 数字）要正常归一化，别一起回退了
    assert setup_logging({"LOG_LEVEL": "warn"})["level"] == "WARNING"
    assert setup_logging({"LOG_LEVEL": "20"})["level"] == "INFO"


def test_invalid_format_falls_back_to_text_and_notes_reason(caplog):
    with caplog.at_level(logging.WARNING, logger=logsetup.LOGGER_NAME):
        summary = setup_logging({"LOG_FORMAT": "xml"})
    assert summary["format"] == "text"
    assert "xml" in summary["note"]
    assert isinstance(_our_handlers()[0].formatter, TextFormatter)
    assert any("LOG_FORMAT" in r.getMessage() for r in caplog.records)


def test_setup_never_raises_on_broken_env():
    summary = setup_logging(_ExplodingEnv())
    assert set(summary) == SUMMARY_KEYS
    assert "失败" in summary["note"]
    assert summary["handlers"] == 0

    # 传了个不是映射的东西（例如 list）也必须只是警告，不许把服务带崩
    assert set(setup_logging([])) == SUMMARY_KEYS        # type: ignore[arg-type]


def test_failed_setup_keeps_previous_handler_alive():
    """配置失败时不许把已经装好的 handler 拆掉 —— 那会让日志凭空消失。"""
    setup_logging({})
    before = _our_handlers()
    assert before
    setup_logging(_ExplodingEnv())
    assert _our_handlers() == before


# ============================================================
# 七、不许碰 uvicorn 的 logger
# ============================================================
def test_uvicorn_loggers_are_never_touched():
    """uvicorn 的 access/error logger 是它自己的，我们只配 root 与 agentdesk.*。"""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    before = {}
    for name in names:
        logger = logging.getLogger(name)
        before[name] = (list(logger.handlers), logger.level, logger.propagate,
                        list(logger.filters))
    setup_logging({"LOG_FORMAT": "json", "LOG_LEVEL": "DEBUG"})
    for name in names:
        logger = logging.getLogger(name)
        handlers, level, propagate, filters = before[name]
        assert list(logger.handlers) == handlers, name
        assert logger.level == level, name
        assert logger.propagate == propagate, name
        assert list(logger.filters) == filters, name
        assert not any(getattr(h, logsetup.HANDLER_MARK, False) for h in logger.handlers)
