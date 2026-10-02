# -*- coding: utf-8 -*-
"""
结构化日志层（M4 · 审计缺口 A7）
============================================================
补这一层之前，全项目**没有任何 logging 配置**：`app/security.py` 与
`app/notify/*.py` 各取了一个 logger，但没有人 `basicConfig`，也没有
`dictConfig`。后果不是"日志格式不好看"，而是：

    · `log.info(...)` / `log.debug(...)` **全部掉进黑洞** —— root 上没有
      handler，只有 WARNING 以上才会被 `logging.lastResort` 用一句
      光秃秃的消息打到 stderr（没有时间、没有级别、没有 logger 名）
    · 线上按级别调日志是做不到的：没有任何一个旋钮能改级别
    · 一次请求打了十几条日志，**串不起来** —— 没有 request_id 这种
      "这条日志属于哪次请求"的字段，只能靠时间戳肉眼对齐

本模块只解决这三件事：**可配置、可结构化、能按请求串起来**。

【默认档为什么是 text，而不是 json】

json 日志对机器友好、对 `docker logs` 的人不友好。这个项目部署方式是
`docker compose` + `docker logs -f`，看日志的是人。所以默认必须是
text —— **补上一层观测能力，不能让现有排查方式变难读**。
需要给采集器（Loki / ES）喂数据时，`LOG_FORMAT=json` 一行切过去。

【为什么 request_id 用 contextvar，而不是层层传参】

和 tracer.py 里 trace_id 是同一个理由：请求 id 要出现在**任意深度**的
日志里，把它做成函数参数意味着每个签名都要加一个参数、漏一处就断链。
contextvar 的代价是要理解上下文语义，收益是调用方完全无感知。
（module 里那些 `log.info("限流拦截 ...")` 一行都不用改。）

★ 线程隔离是 contextvar 自带的：每个线程有自己的当前上下文，新线程
  从**空上下文**开始。所以两个线程各自 `set_request_id` 互不影响 ——
  这正是"并发处理多个请求时日志不串号"的基础。相对地，如果用
  `threading.local` 就做不到 asyncio 协程级隔离（一个线程里跑多个
  协程会互相覆盖），用模块级全局变量则连线程隔离都没有。
  contextvar 同时覆盖这两种并发模型，这是它被选中的唯一理由。

【幂等：为什么靠"标记 + 只回收自己的"】

`setup_logging()` 会被 main.py 在 import 时调一次，测试里还会被调很多次。
如果每次都无脑 `addHandler`，第二次调用的结果就是**每条日志打两遍**。
所以：

    · 装到 root 上的 handler 都打上 `HANDLER_MARK` 标记
    · 每次先挂新的、再回收**带这个标记**的旧的（顺序不能反，见代码注释）
    · 别人装的 handler（pytest 的 caplog、将来别人加的文件 handler）
      **一律不碰**

【为什么摘要里的 handlers 只数我们装的】

"生效摘要"是给启动日志和 /health 看的，必须**确定**：它在干净进程里是 1，
在 pytest 里也必须是 1。如果数 root 上的全部 handler，同一个配置在
pytest（caplog 会往 root 挂一个）和线上会报出不同的数字，这个字段就
没法用来断言"没有重复挂载"。所以它数的是"我们挂了几个 sink"。

【决不碰 uvicorn 的 logger】

uvicorn 自己配置 `uvicorn` / `uvicorn.error` / `uvicorn.access`
（自带的 handler，且 `propagate=False`），它才是 access log 的主人。
我们只配置 root，`agentdesk.*` 靠**继承** root 的 handler 输出 ——
这就是摘要里 handlers 恒为 1 的原因：全项目只挂一个 sink。
（已确认 uvicorn 默认配置里那两个 logger 的 `propagate=False`，
 所以给 root 挂 handler 不会让 uvicorn 的日志变成双份。）

【一条铁律：配置日志不许把服务搞挂】

写错一个环境变量（`LOG_LEVEL=verbose`）不能让服务起不来 —— 这跟
"打开鉴权却忘配 token 就硬失败"（security.py）是**相反**的取舍：
配置错了只是日志降级（回退 INFO 并在摘要/警告里说清楚），
而安全配置错了是"以为有保护其实裸奔"，那个必须炸。全函数吞掉
自己的异常，任何情况下都返回一份可用的摘要。
"""

import contextvars
import json
import logging
import os
import secrets
import sys
import threading
from datetime import datetime, timezone

# ============================================================
# 常量（测试与 main.py 可以直接引用，不要写字面量）
# ============================================================
LOGGER_NAME = "agentdesk.logsetup"

#: 装在 root 上的 handler 会被打上这个属性。幂等靠它区分"我们的"和"别人的"。
HANDLER_MARK = "_agentdesk_logsetup"

#: 默认档。text = 保持 `docker logs` 现有观感（见模块 docstring）。
DEFAULT_FORMAT = "text"
DEFAULT_LEVEL = "INFO"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_VALID_FORMATS = ("text", "json")
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

_lock = threading.Lock()
#: 当前装在 root 上的、属于我们的 handler（幂等回收用）。
_installed: list[logging.Handler] = []

#: ★ 请求上下文。默认空串 = "不在任何请求里"，因此 `get_request_id()`
#:   在没有请求上下文时（启动期、后台线程、脚本）不会抛异常。
_request_id: contextvars.ContextVar = contextvars.ContextVar("request_id", default="")


# ============================================================
# 请求上下文
# ============================================================
def new_request_id() -> str:
    """生成一个新的 request id：8 位十六进制。

    为什么是 8 位十六进制而不是 uuid4：这个 id 是**打给人看的** ——
    它出现在每一行日志里、出现在 `grep` 命令里、出现在跟人对话的
    "把 req=3f9a1c02 那几条日志发我"里。8 位十六进制（约 43 亿种）
    在单进程几个小时的窗口里碰撞概率可以忽略，而长度只有 uuid 的
    1/4，肉眼抄写不会错。
    """
    return secrets.token_hex(4)


def get_request_id() -> str:
    """取当前请求 id。没有就返回空串（**不返回 None** —— 调用方少一层判空）。"""
    return _request_id.get()


def set_request_id(rid: str) -> None:
    """设置当前请求 id。只影响**当前上下文**（当前线程 / 当前协程）。"""
    _request_id.set("" if rid is None else str(rid))


def clear_request_id() -> None:
    """清空当前请求 id。

    ★ 中间件必须在 `finally` 里调它。否则在"一个线程服务多个请求"的
      模型下（同步 endpoint 会跑在线程池里），下一个请求会**继承**
      上一个请求的 id —— 日志看着串起来了，其实串错了请求，
      这比没有 request_id 更糟：它会把排查引向错误的方向。
    """
    _request_id.set("")


class RequestIdFilter(logging.Filter):
    """给每条 LogRecord 补一个 `request_id` 字段。

    为什么放在 filter 里，而不是让 formatter 自己去读 contextvar：
    formatter 应该只负责"排版"，而 request_id 是这条记录的**属性**。
    盖在 record 上之后，所有消费者（我们的 Text/Json formatter、
    pytest 的 caplog、将来可能接的 Sentry）拿到的都是同一个值，
    不需要各自 import 本模块、也不会各读各的时序。

    ★ 永远返回 True：filter 的另一个用途是"丢弃记录"，这里只盖章。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


# ============================================================
# 格式化
# ============================================================
def _iso_ts(created: float) -> str:
    """ISO8601 时间戳，**带本地时区偏移**。

    带偏移而不是裸本地时间：日志出问题时要跟别的系统（容器、数据库、
    云厂商控制台）对齐，裸时间在多时区部署下会产生"谁快了一小时"
    这种查不下去的悬案。
    """
    return datetime.fromtimestamp(created, tz=timezone.utc).astimezone().isoformat(
        timespec="milliseconds")


def _safe_message(record: logging.LogRecord) -> str:
    """取插值后的消息，**插值失败也不抛**。

    `record.getMessage()` 里是 `msg % args`。调用方写错了参数个数
    （`log.info("失败: %s %s", err)`）时它会抛 TypeError，而日志层
    抛异常的结果是整条日志丢掉 + stderr 上一段 "--- Logging error ---"。
    宁可打一条丑的原始消息，也不能丢这条记录 —— 它往往正是出错那条。
    """
    try:
        return record.getMessage()
    except Exception as e:
        return f"{record.msg!r} (参数插值失败: {type(e).__name__}: {e}) args={record.args!r}"


class TextFormatter(logging.Formatter):
    """默认档：`时间 级别 logger 消息`，有请求上下文时追加 ` [req=xxxxxxxx]`。

    尽量贴近现状：uvicorn 自己的日志格式（`INFO:     ...`）我们不动；
    业务日志以前是"消息 + 一个换行"，现在加上时间/级别/logger 前缀，
    是为了让 `docker logs` 里的每一行都能回答"什么时候、多严重、
    谁打的"。异常照旧带完整 traceback（`exc_info`）。
    """

    def __init__(self, datefmt: str = DEFAULT_DATE_FORMAT):
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s%(reqpart)s",
            datefmt=datefmt,
        )

    def format(self, record: logging.LogRecord) -> str:
        # ---- 临时字段：为什么必须用完就删 ----
        # record 是**多个 handler 共享的同一个对象**。这里往它身上塞的
        # `reqpart`，如果留着，JsonFormatter 的"额外字段"收集（它靠
        # `record.__dict__` 减去标准字段名）就会把 `reqpart` 当成
        # 调用方 extra 传进来的业务字段，输出一行 `"reqpart": " [req=...]"`。
        # 这个 bug 只在"text 和 json 两个 handler 同时挂着"时出现，
        # 平时完全看不见 —— 所以这里不是"顺手清理"，是必须清理。
        rid = getattr(record, "request_id", "") or ""
        record.reqpart = f" [req={rid}]" if rid else ""
        try:
            return super().format(record)
        finally:
            try:
                del record.reqpart
            except AttributeError:
                pass


#: 标准 LogRecord 自带的字段名 —— 收集"额外字段"时要减掉它们。
#: 用真实构造一个 LogRecord 来取，而不是手写列表：Python 版本升级时
#: 加了新字段（3.12 就多了 `taskName`），手写列表会跟不上，
#: 结果是每条 json 里都多出一个没人要的标准字段。
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    # Formatter.format() 会把插值结果回填到 record 上；两个 formatter
    # 先后处理同一条记录时，它就会出现在 __dict__ 里，必须显式排除。
    "message",
    "asctime",
    # 本模块自己盖的字段 / TextFormatter 的临时字段（双保险）
    "request_id",
    "reqpart",
}


class JsonFormatter(logging.Formatter):
    """一行一个 JSON。给采集器（Loki / ES / jq）吃。

    固定字段：`ts`(ISO8601) / `level` / `logger` / `msg`；
    有请求上下文时加 `request_id`；有异常时加 `exc_info`（字符串）。

    【为什么 ensure_ascii=False 是硬要求】
    中文日志被转义成 `"\\u78c1\\u76d8\\u5199\\u6ee1"` 之后：
      · `docker logs | grep 磁盘` 再也 grep 不到
      · 人眼看日志要先去某个网站解转义
    json 规范允许非 ASCII 字符直接出现，采集器也都按 UTF-8 读。
    转义唯一的用途是"输出到纯 ASCII 通道"，那是极少数场景，
    不该让默认档为它牺牲可读性。

    【为什么额外字段可以关掉】
    `LOG_JSON_EXTRA=0` 时只留核心四个字段。用途：日志要进第三方
    系统、对每行体积有硬要求（或不想把线程名/函数名/业务 extra
    暴露出去）时，一个开关就能瘦身。默认 1 是多给上下文，
    因为"出问题时才发现没记"是这一层最常见的遗憾。
    """

    def __init__(self, include_extra: bool = True, ensure_ascii: bool = False):
        super().__init__()
        self.include_extra = include_extra
        self.ensure_ascii = ensure_ascii

    def _payload(self, record: logging.LogRecord) -> dict:
        payload = {
            "ts": _iso_ts(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", "") or ""
        if rid:
            # 没有请求上下文时**不写这个键**，而不是写成 "" ——
            # 采集器端 `request_id:""` 会把启动期/后台任务的日志
            # 跟"某次请求"混进同一个分组。
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        if self.include_extra:
            payload.update(self._extras(record))
        return payload

    def _extras(self, record: logging.LogRecord) -> dict:
        out = {
            "thread": record.threadName,
            "process": record.processName,
            "func": record.funcName,
            "line": record.lineno,
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in out:
                continue
            out[key] = value          # `log.info(..., extra={"trace_id": ...})`
        return out

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(self._payload(record), ensure_ascii=self.ensure_ascii,
                              default=str)
        except Exception as e:
            # 兜底：`extra` 里塞进了循环引用之类 json 编不出来的东西时，
            # **宁可少几个字段，也不能整条记录丢掉**（观测层不许拖垮业务，
            # 和 tracer.py 的铁律同源）。核心四字段是自己拼的，编得出来。
            return json.dumps(
                {
                    "ts": _iso_ts(record.created),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": _safe_message(record),
                    "log_error": f"{type(e).__name__}: {e}"[:200],
                },
                ensure_ascii=False,
            )


# ============================================================
# 配置解析
# ============================================================
def _resolve_format(raw) -> tuple[str, str | None]:
    """`LOG_FORMAT` → (生效值, 警告语)。非法值回退 text。"""
    value = str(raw if raw is not None else DEFAULT_FORMAT).strip().lower()
    if value in _VALID_FORMATS:
        return value, None
    return DEFAULT_FORMAT, f"LOG_FORMAT={raw!r} 不是合法格式，已回退 {DEFAULT_FORMAT}"


def _resolve_level(raw) -> tuple[int, str, str | None]:
    """`LOG_LEVEL` → (级别数字, 规范名, 警告语)。非法值回退 INFO。

    ★ 非法值只回退 + 警告，**不抛异常**：写错一个环境变量不该让服务
      起不来（对比 security.py 的 AUTH_ENABLED=1 却没 token 是硬失败 ——
      那个错误会让"以为有保护其实裸奔"，性质完全不同）。
    """
    text = str(raw if raw is not None else DEFAULT_LEVEL).strip().upper()
    if not text:
        return logging.INFO, "INFO", None
    if text.isdigit():
        # 允许 LOG_LEVEL=20 这种写法（老运维习惯）
        return int(text), logging.getLevelName(int(text)), None
    level = logging.getLevelNamesMapping().get(text)
    if isinstance(level, int) and not isinstance(level, bool):
        # 用 getLevelName 归一化：LOG_LEVEL=WARN 的规范名是 WARNING
        return level, logging.getLevelName(level), None
    return logging.INFO, "INFO", f"LOG_LEVEL={raw!r} 不是合法级别，已回退 INFO"


def _resolve_bool(raw, default: bool) -> bool:
    """`1/true/yes/on` → True；`0/false/no/off` → False；空值 → 默认。"""
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    return text in _TRUTHY


# ============================================================
# 装配
# ============================================================
def _build_handler(formatter: logging.Formatter) -> logging.StreamHandler:
    """造一个属于我们的 handler：stderr + 请求 id 盖章 + 身份标记。

    为什么是 stderr 不是 stdout：和 uvicorn 的默认 handler 一致
    （错误/运行日志走 stderr，access log 走 stdout），这样
    `docker logs 1>/dev/null` 这类分离两个流的老习惯不会被打破。
    """
    handler = logging.StreamHandler()          # 默认 sys.stderr
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())
    # ★ handler 上不设 level：级别闸门只放 root 一处。
    #   两处判级（root 放行、handler 丢弃）是"日志时有时无"这类
    #   灵异问题的经典来源。
    setattr(handler, HANDLER_MARK, True)
    return handler


def _close_quietly(handler: logging.Handler) -> None:
    """关掉被换下来的旧 handler。关不掉也不能让 setup 失败 —— 它已经不在 root 上了。"""
    try:
        handler.close()
    except Exception:
        pass


def _warn(message: str) -> None:
    """尽力喊一声。**连"喊"本身都不许抛异常** —— 走到这条路径时日志本来就是坏的。"""
    try:
        logging.getLogger(LOGGER_NAME).warning(message)
    except Exception:
        try:
            print(f"[{LOGGER_NAME}] {message}", file=sys.stderr)
        except Exception:
            pass


def setup_logging(env: dict | None = None) -> dict:
    """配置 root logger，返回**生效摘要**。幂等，且绝不抛异常。

    `env` 传 None 时读 `os.environ`；传 dict 时**只用这个 dict**
    （不再回落到 os.environ）—— 测试要的是确定性，
    "注入了一半、另一半偷偷读进程环境"会让用例随 CI 环境变红。

    返回：`{"format": "text"|"json", "level": "INFO", "handlers": n,
             "request_id": True|False, "note": "..."}`
    `handlers` 数的是**我们挂在 root 上的 sink 个数**（见模块 docstring）。
    `request_id` 表示"每条记录都会盖上请求 id"这套机制是否生效。
    """
    summary = {
        "format": DEFAULT_FORMAT,
        "level": DEFAULT_LEVEL,
        "handlers": 0,
        "request_id": False,
        "note": "",
    }
    try:
        source = os.environ if env is None else env
        notes: list[str] = []
        problems: list[str] = []

        fmt, problem = _resolve_format(source.get("LOG_FORMAT", DEFAULT_FORMAT))
        if problem:
            problems.append(problem)

        level_no, level_name, problem = _resolve_level(
            source.get("LOG_LEVEL", DEFAULT_LEVEL))
        if problem:
            problems.append(problem)

        include_extra = _resolve_bool(source.get("LOG_JSON_EXTRA", "1"), True)

        if fmt == "json":
            formatter: logging.Formatter = JsonFormatter(include_extra=include_extra)
            notes.append("json 结构化输出（一行一条，ensure_ascii=False）")
            if not include_extra:
                notes.append("LOG_JSON_EXTRA=0：不带额外字段")
        else:
            formatter = TextFormatter()
            notes.append("text 人类可读输出（保持 docker logs 现有观感）")
        notes.append(f"级别 {level_name}")

        handler = _build_handler(formatter)
        root = logging.getLogger()

        with _lock:
            # ★ 顺序不能反：**先挂新的，再拆旧的**。
            #   反过来的话，两步之间有一个"root 上没有 handler"的窗口；
            #   这期间别的线程打的日志会静默丢掉（而且没人会发现，
            #   因为日志层自己不会报"我丢了一条"）。
            root.addHandler(handler)

            # ★ 清理必须**按标记扫 root**，不能只信 `_installed` 这张模块级列表。
            #   踩过的场景（全量套件里才复现）：`app.main` 在 import 时先调了一次
            #   setup_logging，挂上一个带标记的 handler；随后测试为了隔离把
            #   `_installed` 清空/重建，那个 handler 就变成**孤儿**留在 root 上，
            #   谁也不会再摘掉它 —— 结果同一个标记的 handler 挂了两个，
            #   每条日志打印两遍。
            #   标记（HANDLER_MARK）存在的意义就是"跨调用、跨模块识别自己人"，
            #   所以清理也应当以它为准；`_installed` 只用于摘要计数。
            for old in list(root.handlers):
                if old is handler:
                    continue
                if old in _installed or getattr(old, HANDLER_MARK, False):
                    root.removeHandler(old)
                    _close_quietly(old)
            _installed[:] = [handler]
            root.setLevel(level_no)
            installed_count = len([h for h in root.handlers
                                   if getattr(h, HANDLER_MARK, False)])

        summary.update(
            format=fmt,
            level=level_name,
            handlers=installed_count,
            request_id=any(isinstance(f, RequestIdFilter) for f in handler.filters),
        )

        if problems:
            notes.extend(problems)
            # 配置有问题时"摘要里说清楚"还不够 —— 启动日志里必须能看见。
            # （若用户把级别设成 ERROR 以上，这条 warning 会被级别闸门挡掉，
            #   但 note 里始终留着，工单里不会丢证据。）
            for problem_msg in problems:
                _warn(problem_msg)

        summary["note"] = "；".join(notes)
    except Exception as e:
        # ★ 兜底：任何一步炸了都不许逃出去。日志配置失败的正确结果
        #   是"日志少一点"，不是"服务起不来"。
        summary["note"] = f"日志配置失败（已忽略，服务继续可用）：{type(e).__name__}: {e}"
        _warn(summary["note"])
    return summary
