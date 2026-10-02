# -*- coding: utf-8 -*-
"""
Trace 记录层
============================================================
一次 Agent 运行 = 一个 trace；里面的每次模型调用、每次工具调用、
每个 Agent 节点 = 一个 span。全部追加写入 logs/traces.jsonl。

【核心机制：contextvar，而不是层层传参】

记录 span 时需要知道"我属于哪个 trace、我的父 span 是谁"。
两种做法：

    ① 把 trace_id / span_id 作为参数层层传递
       llm.py → agents → tools → …… 每个函数签名都要加参数，
       改一处带一串，漏一处就断链。

    ② contextvar（本文件的做法）
       trace/span 用 with 语句打开，当前活跃的 span 压进一个
       上下文栈；任何深处的代码想挂 span，自动拿到栈顶当父节点。
       **调用方完全无感知。**

② 的代价是要理解 contextvar，但收益是"加观测不用改函数签名"——
这跟 llm.py 统一入口是同一个思路：**让横切能力长在它该在的地方，
而不是散进每一个业务函数。**

【为什么是"结束时写完整记录"而不是"事件日志 + 折叠"】

approvals.py 用"追加事件、状态靠折叠"，因为它有状态机要重放；
trace 这里每条 span 在**结束时一次性写完整**（含耗时、usage、状态），
查询时直接读，不需要折叠。代价是：进程崩掉时未结束的 span 会丢——
**观测数据允许丢，业务数据不允许丢**，两类日志的取舍本来就该不同。

【一条铁律：观测代码绝不能让业务挂掉】

本文件所有对外函数都吞掉自己的异常。tracer 抛异常导致一次诊断失败，
是本末倒置——观测的可用性永远低于业务的可用性。
"""

import contextvars
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.observability import jsonl

# 项目根目录。注意这里**不复用** app.llm.PROJECT_ROOT ——
# llm.py 会 import 本文件，反过来 import 就是循环依赖。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACE_PATH = PROJECT_ROOT / "logs" / "traces.jsonl"

# span 类型。给聚合用的维度，别随意加。
TYPE_LLM = "llm"          # 一次模型调用
TYPE_TOOL = "tool"        # 一次工具调用
TYPE_AGENT = "agent"      # 一个 Agent 节点跑完（意图/诊断/校验/处置…）
TYPE_FLOW = "flow"        # 一段流程（如告警归一化）

# ★ 下面两个是为「写操作生命周期」补的（原先这一段在 trace 里完全不可见）。
#   背景：审计日志回答了"谁在什么时候做了什么"，但它回答不了
#   "这次运行里 Agent 想干什么、卡在哪一步"。而写操作恰恰是最需要
#   被观察的一环 —— 之前 by_span_type 里只有 llm / tool，
#   审批与容器执行一个 span 都没有。
TYPE_APPROVAL = "approval"  # 审批单的生命周期（被拒 / 已提交待批）
TYPE_SANDBOX = "sandbox"    # 一次沙箱执行（容器 / 本机 subprocess）

_lock = threading.Lock()

# ---- 上下文 ----
_trace_id: contextvars.ContextVar = contextvars.ContextVar("trace_id", default=None)
_span_stack: contextvars.ContextVar = contextvars.ContextVar("span_stack", default=None)
_collected: contextvars.ContextVar = contextvars.ContextVar("collected", default=None)

# 导出器由 langfuse_export 在配置了 Key 时挂进来；tracer 只管在 trace 结束时通知。
_export_hook = None          # callable(trace_record) 或 None
_export_failures = 0
# 观测**写入**失败的累计次数。写入失败不能中断业务（见模块铁律），
# 但也不能装作没发生 —— 这个计数由 /metrics 与 /healthz 暴露出去。
_write_failures = 0


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _write(record: dict) -> None:
    """追加一条记录。**任何失败都吞掉** —— 见模块 docstring 的铁律。

    ★ 但"吞掉"不等于"装作没发生"：失败次数记在 `_write_failures` 里，
      由 `write_failures()` 暴露给 /metrics 与 /healthz。
      观测数据写不进去，业务不该中断；但**看板上必须看得出来**，
      否则就成了"以为在记录、其实一直在丢"——那正是本项目反复警惕的静默失败。
    """
    global _write_failures
    try:
        jsonl.append_jsonl(TRACE_PATH, record,
                           limit_bytes=jsonl.max_bytes(),
                           keep=jsonl.keep_count())
    except Exception:
        with _lock:
            _write_failures += 1


def write_failures() -> int:
    """观测写入失败的累计次数（进程内计数）。"""
    return _write_failures


def set_export_hook(hook) -> None:
    global _export_hook
    _export_hook = hook


def note_export_failure() -> None:
    global _export_failures
    _export_failures += 1


def export_failures() -> int:
    return _export_failures


class _NullSpan:
    """没有活跃 trace 时的空句柄 —— 一切调用都是 no-op。

    这不是错误路径，是正常路径：没包 trace 的调用点（如 /rag/ask）
    就是拿不到上下文，静默跳过比抛错合适。
    """

    def set_usage(self, usage): pass
    def set(self, key, value): pass
    def set_error(self, err): pass


class _Span:
    """with 语句用的 span 句柄。

    用法：
        with tracer.span("tool", name="check_disk", host="web-01") as sp:
            out = execute_tool("check_disk", {"host": "web-01"})
            sp.set_usage(out.get("usage"))
    """

    def __init__(self, trace_id: str, span_id: str, parent_id, stype: str,
                 name: str, attrs: dict):
        self.trace_id = trace_id
        self.id = span_id
        self.parent_id = parent_id
        self.type = stype
        self.name = name
        self.attrs = dict(attrs or {})
        self.started = time.time()
        self.started_at = _now()
        self.usage = {}
        self.status = "ok"
        self.error = None

    # ---- 调用方在 with 块里补充的信息 ----
    def set_usage(self, usage: dict) -> None:
        """覆盖语义。只用于"这个 span 本身直接产生"的用量（叶子 span）。"""
        if isinstance(usage, dict):
            self.usage = usage

    def add_usage(self, usage: dict) -> None:
        """累加语义。用于「把别人的用量并进来」（父 span 汇总子 span）。

        ★ set_usage 和 add_usage 必须分清 —— 归因踩过的坑就是
          用覆盖把子 span 归并上来的用量清零了：
          chat 的 449 token 先归并进 intent 的 span，
          随后节点返回时 set_usage({}) 又把它覆盖成空。
          **同一个字段两种语义，必须拆成两个方法，靠调用方自觉必错。**
        """
        if isinstance(usage, dict):
            for k, v in usage.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.usage[k] = self.usage.get(k, 0) + v

    def set(self, key: str, value) -> None:
        self.attrs[key] = value

    def set_error(self, err) -> None:
        self.status = "error"
        self.error = str(err)[:300]

    # ---- 内部 ----
    def _finish(self) -> dict:
        elapsed_ms = int((time.time() - self.started) * 1000)
        # 属性截断 + 限量：轨迹可能很大，单条 span 不允许撑爆存储
        attrs = {k: (v if isinstance(v, (int, float, bool)) else str(v)[:300])
                 for k, v in list(self.attrs.items())[:12]}
        record = {
            "kind": "span", "trace_id": self.trace_id, "span_id": self.id,
            "parent_id": self.parent_id, "type": self.type, "name": self.name,
            "started_at": self.started_at, "elapsed_ms": elapsed_ms,
            "status": self.status, "usage": self.usage, "attrs": attrs,
        }
        if self.error:
            record["error"] = self.error
        return record


@contextmanager
def span(stype: str, name: str, **attrs):
    """打开一个 span。自动挂到当前 trace；嵌套时自动成为上一层的子 span。"""
    tid = _trace_id.get()
    if tid is None:
        yield _NullSpan()
        return

    stack = list(_span_stack.get() or [])
    # ★ 这里是调试最久的一行 bug，值得写下来：
    #   第一版写的是 `parent_id = stack[-1]` —— 塞进去的是 **_Span 对象本身**。
    #   顶层 span 的 parent_id 是 None（能序列化），所以单层 trace 一切正常；
    #   一旦嵌套，parent_id 变成不可序列化的对象，trace 记录 json.dumps 失败，
    #   而观测层按铁律吞掉了自己的异常 —— **结果不是报错，是数据静默丢失**。
    #   教训有两条：
    #     ① 「栈顶元素」和「栈顶元素的 id」差一个属性访问，类型系统救不了你
    #     ② 静默吞异常的代码**必须配上自检**（smoke_test 那几条
    #        trace 记录断言就是为这类问题存在的），否则丢失无信号
    parent_id = stack[-1].id if stack else None
    sp = _Span(tid, "sp-" + secrets.token_hex(4), parent_id, stype, name, attrs)

    stack.append(sp)
    _span_stack.set(stack)
    try:
        yield sp
    except Exception as e:
        # 业务代码崩了：记下来，再**原样抛出去**。
        # ★ 吞掉业务异常是观测层最恶劣的错误 —— 它会把故障变成静默的。
        sp.set_error(e)
        raise
    finally:
        stack.pop()
        _span_stack.set(stack)
        try:
            record = sp._finish()
            collected = _collected.get()
            if collected is not None:
                collected.append(record)
            # ★ 把自己的 usage 归并进父 span —— 标准 trace 语义。
            #
            #   没有这一步，「成本按 Agent 分布」就是错的：
            #   diagnose 节点里那几次模型调用的 token 记在 llm span 上，
            #   节点自己的 span 却是 0，看板会显示"诊断不花钱"。
            #   归并后：父节点的 usage = 自己的 + 所有子 span 的，
            #   而 trace 的总量只按**顶层 span**算（见 trace()），
            #   不会因为层层归并而重复计数。
            parent = stack[-1] if stack else None
            if parent is not None:
                # ★ 用 add_usage（内部会过滤非数值字段）而不是手写累加。
                #
                #   手写那版的教训 —— **和 _merge_usage 是同一个坑，
                #   本项目第二次踩**：
                #
                #       parent.usage[k] = parent.usage.get(k, 0) + v
                #
                #   DeepSeek 的 usage 里有 `prompt_tokens_details`（嵌套 dict），
                #   遍历到它时 `0 + dict` → TypeError。而这个 TypeError
                #   发生在下面的 `except Exception: pass` 里 —— **被吞掉了**。
                #
                #   后果极其隐蔽：dict 的键顺序是
                #       prompt_tokens, completion_tokens, total_tokens,
                #       prompt_tokens_details, prompt_cache_hit_tokens, ...
                #   所以前三个字段正常归并、后面两个（缓存命中/未命中）
                #   永远丢失 → 节点成本按"全部未命中"算 → 看板数字虚高，
                #   而且层级越高越离谱（每层都少算折扣）。
                #
                #   两条通用教训：
                #     ① **累加前必须过滤非数值字段** —— 外部返回的 usage
                #        里混嵌套结构是常态，不是意外
                #     ② **吞异常的代码会吞掉"半成品状态"** ——
                #        这次丢的不是整条数据，是一半字段，
                #        比整条丢失更难发现（数据看起来是有的）
                parent.add_usage(record.get("usage") or {})
            _write(record)
        except Exception:
            pass


@contextmanager
def trace(name: str, question: str = "", batch: bool = False,
          source: str = "live", **attrs):
    """打开一个 trace（一次完整运行）。

    结束时写一条 kind=trace 的汇总记录：总 token / 总成本 / 完整 spans
    都并进这一条 —— 查询单次详情只需要读一行。

    ★ source 标记这条 trace 是**怎么产生的**：

      · "live"     —— 真实运行（HTTP 请求、Agent 诊断、评测批处理…）
      · "selftest" —— 自检脚本写的，**不是真实流量**

      为什么必须分开（这是被一个假数字逼出来的）：

        某天看 /metrics/summary，显示 **errors=8**。真实运行里一次错误都没有 ——
        那 8 条全是 `scripts/smoke_test.py` 每次跑都要造的 `smoke-err`
        （它专门用来验证"错误 trace 能被正确记录"）。
        同一窗口里 50 条 trace 有 24 条是自检造的。

        成本上这些假记录占比很小（不到 2%），所以金额看不出问题；
        但**计数类指标被直接污染** —— "50 次运行、8 次错误"读起来
        像一个错误率 16% 的系统，而真相是 0%。

        **自检数据用于验证"记录机制对不对"，不能拿来当"系统运行状况"。**
        它必须能被一眼认出、且默认不计入对外口径。


    ★ batch=True 表示**批处理任务**（比如跑一次评测：160 次模型调用、
      几分钟），不是"一次用户请求"。

      【为什么必须区分 —— 这是被一个很唬人的数字逼出来的】

      某次看 /metrics/summary，P95 显示 **315675ms（315 秒）**。
      第一反应是"有脏数据"，查下去发现：那是一个**真实**的 trace ——
      就是那次评测运行本身，它确实跑了 315 秒。

      数据没错，**是口径错了**：把一次批处理任务和一次用户请求
      混在同一个延迟分布里，P95 就变成了"哪个批处理任务跑了多久"，
      完全不能反映用户感受到的延迟。

      **成本要合并算（钱真的花了），但延迟必须分开口径。**
      这和"相关性只能统计库内题"是同一类错误：
      **分母选错，指标就失去意义。**
    """
    tid = "tr-" + secrets.token_hex(6)
    started = time.time()

    token = _trace_id.set(tid)
    _span_stack.set([])
    _collected.set([])
    status, error = "ok", None
    try:
        yield tid
    except Exception as e:
        status, error = "error", str(e)[:300]
        raise
    finally:
        _trace_id.reset(token)
        _span_stack.set(None)
        spans = _collected.get() or []
        _collected.set(None)
        try:
            # ★ 只按顶层 span（parent_id 为空）求总量。
            #   子 span 的 usage 已经归并进父 span（见 span() 的 finally），
            #   全部求和会重复计数 —— "归并"和"汇总"必须配套改，漏一半就错。
            usage = sum_usage([s.get("usage") for s in spans
                               if s.get("parent_id") is None])
            # ★ 但**算钱不能按顶层 span 算**：只有叶子 span（真正发起模型调用那层）
            #   的 attrs 里才有"这次到底用了哪个模型"。顶层 agent span 的 usage 是从
            #   子 span 归并上来的，模型信息已经丢了 —— 拿它查单价只能退回默认模型，
            #   于是"请求 deepseek-chat、服务端实际用 deepseek-flash" 这类偏差
            #   永远修不掉（实测确实如此，两个模型单价不同）。
            #   叶子 = 没有任何 span 的 parent_id 指向它。
            from app.observability.costs import cost_of, model_of_span
            parent_ids = {s.get("parent_id") for s in spans}
            leaves = [s for s in spans if s.get("span_id") not in parent_ids]
            cost = sum(cost_of(s.get("usage") or {}, model_of_span(s))
                       for s in leaves)
            record = {
                "kind": "trace", "trace_id": tid, "name": name,
                # batch=True 的 trace 会计入成本，但不计入延迟统计（见上方说明）
                "batch": bool(batch),
                # ★ live = 真实运行 / selftest = 自检脚本造的。
                #   聚合时默认把 selftest 排除在外，否则"运行次数/错误数"
                #   这类计数指标会被自检数据污染（实测污染过 100%）。
                "source": source,
                "question": str(question or "")[:200],
                "started_at": _now(), "elapsed_ms": int((time.time() - started) * 1000),
                "status": status, "usage": usage,
                "cost_cny": round(cost, 6), "span_count": len(spans),
                "spans": spans[-80:],          # 兜底：极端情况下也别写爆一行
            }
            if attrs:
                record["attrs"] = {k: str(v)[:200] for k, v in list(attrs.items())[:8]}
            if error:
                record["error"] = error
            _write(record)
            if _export_hook:
                try:
                    _export_hook(record)       # 整条 trace 一次导出，不逐 span 推
                except Exception:
                    note_export_failure()
        except Exception:
            pass


def traced(engine: str):
    """装饰器：把一次引擎 run(question, ...) 包成一个 trace。

    用装饰器而不是改 run() 的函数体，和 execute_tool 用包装是同一个理由：
    **横切能力不改业务代码。** 三个引擎（react / graph / supervisor）
    各加一行装饰器就全部接入，函数体零改动。

    ★ 引擎会嵌套：supervisor 的诊断节点内部调 graph.run（诊断子图）。
      如果不做判断，内层 run 会再开一个 trace —— 灾难性的：

        ① 外层的 trace_id 被覆盖，内层结束后虽然 reset 回来，
           但内层的 _span_stack.set(None) 把外层的 span 栈清空了
           → 外层后续所有 span 断链（实测：diagnose 的 span 从此消失）
        ② 数据被劈成两条 trace，看板上"诊断不花钱"

      正确语义：**有活跃 trace 就不再开新的** —— 子引擎的 span
      自然挂到外层 trace 的当前节点上，这才是"同一次运行"。
      （这也是分布式追踪的标准做法：span 跟着上下文走，不跟着函数走。）
    """

    def deco(fn):
        def wrapper(question, *args, **kwargs):
            if _trace_id.get() is not None:
                # 已在一次运行里 —— 只是被外层引擎调用，不开新 trace
                return fn(question, *args, **kwargs)
            with trace(engine, question=question, engine=engine):
                return fn(question, *args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "run")
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return deco


# ============================================================
# 聚合与查询
# ============================================================
def sum_usage(usages) -> dict:
    """把一组 usage 字典累加。

    ★ 只累加**确定是数字**的字段。DeepSeek 的 usage 带嵌套字段
      （prompt_tokens_details），无脑累加会撞上前面那个
      `int + dict` 的 TypeError —— 看着最不可能出错的代码最容易挂。
    """
    out = {}
    for u in usages:
        if not isinstance(u, dict):
            continue
        for k, v in u.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    return out


def read_recent(limit: int = 800, stats: dict = None) -> list:
    """读最近 N 条原始记录（**跨轮转文件回读**）。

    ★ 两处刻意的改动（M4）：

      ① 轮转之后仍要能读到"最近 N 条"：当前文件不够就回读 `.1`、`.2`…
         否则刚轮转完，看板会突然变成"最近没有任何记录" —— 比不轮转更糟。

      ② **不再"出任何错就 return []"**：读失败与"确实没有数据"是两件事，
         而原实现把前者伪装成后者（看板静默显示空）。
         失败/截断/坏行都写进 `stats["read"]`（嵌套键，避免与 recent_traces
         自己那个 `scanned`（含义是"扫过多少条 trace"）撞名 —— 同名不同义
         正是这个项目最容易出错的坑之一）。
    """
    records, meta = jsonl.read_tail(TRACE_PATH, limit)
    if stats is not None:
        stats["read"] = meta
    return records


def recent_traces(limit: int = 20, source: str = "live", stats: dict = None) -> list:
    """最近的 trace 汇总（不含 spans，列表视图用）。新的在前。

    stats：可选。传一个 dict 进来，会把"扫过多少条、滤掉多少条"写回去 ——
           调用方（/metrics/summary）需要如实报告"排除了多少自检记录"。
           **静默过滤和不过滤一样是撒谎**：读者看到 runs=48 时应该能知道
           窗口里其实还有 75 条自检记录被挡在外面。


    ★ source 默认 "live" —— **列表视图默认不显示自检记录**。

      两个理由：
        ① 自检数据要用来验证"记录机制对不对"，混进"最近运行"列表里
           只会让人以为系统真跑过这些请求（`smoke` / `smoke-err` 尤其误导）
        ② 过滤放在"读"这一层（而不是聚合层），才能保证
           **要 50 条就给 50 条**：否则读 50 条、再在聚合里剔掉 24 条，
           接口返回 26 条却说自己查的是 50 次运行 —— 又一次静默少给。

      传 source=None 可拿到全部（自检脚本自己要用，见 smoke_test.py）。

    ★ 不能固定按 limit×N 读原始记录再筛 —— 这一点踩过：

      文件里同时有 `kind=trace` 和 `kind=span` 两种行（散装 span 是崩溃兜底，
      见 span() 末尾的写入）。两者比例**随观测粒度变化**：每加一层观测，
      每条 trace 的 span 就更多，trace 在文件里占比就更低。

      原先写死 `limit * 6`。实测 ratio 约 5:1 时勉强够；一旦 span 变多，
      就可能出现「接口说最近 50 次 trace，实际只返回 33 次」——
      而且**不报错、不提示**，纯粹静默少给。

      改成逐步扩读：不够就换个更大的倍数再读，直到凑够或读满上限。
      代价是极端情况下多读一两次文件（尾部 2MB，很便宜），
      换来的是"接口承诺多少就给多少"。
    """
    excluded, raw, out = 0, [], []
    for factor in (6, 20, 60):
        raw = [r for r in read_recent(limit=limit * factor)
               if r.get("kind") == "trace"]
        # ★ 每轮重新计数：换更大的 factor 会**重扫一遍范围更大的尾部**，
        #   累加会把同一批记录数多次（实测报出 163 条，而扫过的一共只有 123 条 ——
        #   **排除数比扫描数还大**，一眼假）。
        excluded = 0
        traces = []
        for r in raw:
            # 老记录没有 source 字段 → 视作 live（不能因为加字段
            # 就把历史数据全弄丢）
            src = r.get("source") or "live"
            if source is not None and src != source:
                excluded += 1
                continue
            traces.append(r)
        # 扫到的真实运行**总数**（可能多于 limit —— 那部分是被窗口截掉的，
        # 不是被排除的，两者不能混为一谈）
        live_total = len(traces)
        seen, out = set(), []
        for t in reversed(traces):
            tid = t.get("trace_id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            out.append(t)
            if len(out) >= limit:
                break
        if stats is not None:
            # ★ 三个数必须能对上：scanned = selftest_excluded + live_available
            #   （报过一次 selftest_excluded=163 而 scanned 只有 123 —— 排除数
            #    比扫描数还大，一眼假。根因是扩读时重复累加。）
            stats["excluded_selftest"] = excluded
            stats["scanned"] = len(raw)
            stats["live_available"] = live_total
        if len(out) >= limit:
            return out
    return out


def trace_detail(trace_id: str):
    """一条 trace 的完整详情。kind=trace 的记录里已并入 spans，直接返回。"""
    records = [r for r in read_recent(limit=3000) if r.get("trace_id") == trace_id]
    if not records:
        return None
    trace_rec = next((r for r in records if r.get("kind") == "trace"), None)
    if trace_rec:
        return trace_rec
    # 兜底：只有散装 span（老数据或写了一半），现场拼一份
    spans = [r for r in records if r.get("kind") == "span"]
    if not spans:
        return None
    return {
        "kind": "trace", "trace_id": trace_id, "name": "(reconstructed)",
        "started_at": spans[0].get("started_at"),
        "elapsed_ms": max(s.get("elapsed_ms", 0) for s in spans),
        "usage": sum_usage([s.get("usage") for s in spans]),
        "spans": spans,
    }
