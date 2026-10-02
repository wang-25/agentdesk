# -*- coding: utf-8 -*-
"""
AgentDesk 服务入口
============================================================
把模型能力做成一个别人能调用的 HTTP 服务 —— 这是「工具」变成「服务」的那一步。

【运行方式】在 agentdesk 目录下执行：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000

【接口一览】共 31 个业务路由，其中 26 个进 OpenAPI 文档。
  为什么是 24 而不是 22：`GET /`（导航页）和 `GET /try`（在线试用页）是给人看的
  页面，标了 `include_in_schema=False`，不进 OpenAPI 的路径表 ——
  它们真实存在、能访问，只是不该混进接口清单里干扰视线。

  健康与审计
    GET  /health                    健康检查，供容器探活与监控使用
    GET  /audit                     读取审计日志（每次操作都留痕）

  基础模型能力
    POST /chat                      问答，一次性返回
    POST /chat/stream               问答，SSE 流式返回（异步版）
    POST /parse                     意图解析，把一句人话转成结构化 JSON

  告警驱动
    POST /webhook/alert             告警驱动的入口 —— 无人值守自动诊断

  知识库（RAG）
    GET  /rag/stats                 索引统计
    POST /rag/index                 重建索引
    POST /rag/search                只检索不生成（排查检索质量用）
    POST /rag/ask                   RAG 问答，带引用溯源

  Agent
    GET  /agent/tools               工具清单（含风险等级）
    GET  /agent/graph               导出状态图的 mermaid 定义
    POST /agent/ask                 ★ Agent 自主诊断（engine 可选三档）
                                      handwritten  手写 ReAct 循环
                                      langgraph    状态图（默认）
                                      supervisor   ★ 多 Agent 编排

  沙箱与人工确认（HITL）
    GET  /sandbox                   沙箱状态 + 命令白名单（准入规则一览）
    GET  /approvals                 待人工确认的审批单列表
    GET  /approvals/{id}            单张审批单详情
    POST /approvals/{id}/approve    批准（必须填审批人）
    POST /approvals/{id}/reject     驳回
    POST /approvals/{id}/execute    执行已批准的命令（一次性）

  可观测
    GET  /metrics/summary           成本与延迟聚合看板
    GET  /traces                    最近运行列表
    GET  /traces/{id}               单次运行的完整轨迹

  页面（不进 OpenAPI；页面只是空壳，数据仍由接口的令牌保护）
    GET  /                          导航页：这是什么、从哪开始试
    GET  /try                       在线试用页：填问题、点按钮
    GET  /dashboard               可观测看板：成本归因 / 对账 / 逐步轨迹 / 待审批
    GET  /view/{name}              7 个 JSON 接口的可视化皮：metrics/traces/
                                    tools/sandbox/approvals/audit/health，
                                    每张表可导出 CSV、整页可导出 JSON
    GET  /settings                 系统设置页：令牌查看/修改、数据源切换
  系统设置（/settings 页的数据接口；开鉴权时须持当前令牌）
    GET  /settings/api             当前令牌（明文，见安全模型注释）与数据源
    POST /settings/token           修改令牌：运行时生效 + 写回 .env
    POST /settings/ops             切换 mock/local/ssh 与 SSH 目标/私钥/清单
    POST /agent/ask/stream        Agent 诊断的 SSE 流式版（实时编排过程）

服务起来后打开 http://127.0.0.1:8000/docs 有自动生成的交互式文档。
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import security, selfcheck
from app.alerting import AlertAggregator, Silence
from app.observability import jsonl as obs_jsonl
from app.observability import logsetup, metrics as obs_metrics
from app.alerting import alert_to_question, normalize_alerts   # noqa: F401  ← 对外 re-export
from app.incident.model import IncidentError
from app.llm import PROJECT_ROOT, ModelError
from app.llm import chat as llm_chat
from app.llm import chat_json, chat_stream_async
from app.notify import events as notify_events
from app.observability import costs as obs_costs
from app.observability import langfuse_export, tracer as obs

# 配了 LANGFUSE_* 环境变量才生效；没配就是本地记录模式，功能不受影响
langfuse_export.install()

# ============================================================
# 应用实例
# ============================================================
app = FastAPI(
    title="AgentDesk",
    description="面向运维场景的多 Agent 智能体系统",
    version="1.0.0",
)

# 公网安全层：token 鉴权 + 限流 + 每日额度。
# AUTH_ENABLED=0（默认）时完全放行，本地开发行为不变。
security.install_security(app)

# ============================================================
# 可观测：日志格式 + 请求 ID + HTTP 指标（M4）
# ============================================================
# ★ 这三件事必须在**任何请求进来之前**配好，所以放在模块导入期。
#   注意它们都不改变默认行为：
#     · LOG_FORMAT 默认 text（观感与改动前一致，只补上请求 ID）
#     · 请求 ID 只是多一个响应头与日志字段
#     · HTTP 指标只做内存计数（基数有上限，见 metrics 的丢弃计数）
_LOG_SETUP = logsetup.setup_logging()
_STARTED_AT = time.time()

# ★ 压掉几个"话很多"的第三方 logger。
#
#   为什么必须显式压：在这之前**根本没有 logging 配置**，root 没有 handler，
#   Python 只用"最后手段"打印 WARNING 及以上 —— 所以 httpx 那些
#   `HTTP Request: GET ...` 的 INFO 行是不出现的。
#   一旦配上 handler 与 INFO 级别，它们就会涌进容器日志，
#   默认观感与改动前不一致（而且真正有用的那几行被淹掉）。
#
#   **策略放在这里，机制留在 logsetup**：logsetup 负责"格式、级别、请求 ID"，
#   "哪些第三方的 INFO 不该进我们的日志"是本文件的判断。
for _noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3",
               "jieba", "asyncio", "multipart"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _metric_path(path: str) -> str:
    """把路径里的 ID 归一成 `{id}`，避免标签基数爆炸。

    ★ 为什么必须做：`/traces/<id>`、`/approvals/<id>`、`/incidents/<id>` 每次请求
      路径都不同 —— 直接拿原始路径当标签，指标序列数会随请求数线性增长，
      最后是**监控端先被自己打挂**（Prometheus 侧基数爆炸）。
      metrics 模块里还有一层"每指标最多 N 个序列"的兜底，但源头归一更干净。

    ★ 判定规则要按**真实的 ID 形态**来，不能只认自己记得的那几个前缀：
      本项目里 ID 是"短前缀 + 十六进制"（`tr-` trace、`sp-` span、
      `ap-` 审批单、`inc-` 事件），所以规则写成"前缀-hex"这一通用形态 ——
      只写 `ap-`/`inc-` 的话 `/traces/tr-a1b2…` 就漏了（我第一版正是这样，
      被用例抓出来的）。纯数字段与特别长的段也一并归一。
    """
    parts = []
    for seg in path.split("/"):
        if not seg:
            continue
        if _ID_SEGMENT_RE.match(seg):
            parts.append("{id}")
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


# 形如 `tr-a1b2c3d4e5f6` / `ap-1a2b` / `42` / 很长的十六进制串
_ID_SEGMENT_RE = re.compile(r"^(?:[a-z]{1,6}-[0-9a-f]{4,}|\d+|[0-9a-f]{16,})$")


@app.middleware("http")
async def _observability_middleware(request, call_next):
    """给每个请求分配 ID，并记一条 HTTP 指标。

    顺序上它在安全中间件**之后**注册 → 实际执行时在安全层**之内**，
    所以被鉴权/限流挡掉的请求不会走到这里。
    那些拒绝由安全层自己计数（`agentdesk_security_rejections_total`），
    两边的口径合起来才是完整的。
    """
    rid = logsetup.new_request_id()
    logsetup.set_request_id(rid)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        elapsed = time.perf_counter() - started
        path = _metric_path(request.url.path)
        obs_metrics.counter("agentdesk_http_requests_total",
                            {"path": path, "method": request.method,
                             "status": str(status)})
        obs_metrics.observe("agentdesk_http_request_duration_seconds",
                            elapsed, {"path": path})
        logsetup.clear_request_id()


# ============================================================
# 让 /docs 上出现 Authorize 按钮
# ============================================================
# 【为什么必须做这一步】
# 鉴权是在中间件里做的，中间件对 FastAPI 是「黑盒」——
# 它拦请求，但框架并不知道「这些接口需要 token」这件事。
# 结果就是 /docs 生成的交互式文档里**没有填 token 的地方**：
# 用户在页面上点 "Try it out"，请求不带任何凭据，一律 401。
# 用户体验上就是「文档骗我，点了就报错」。
#
# 所以要把这个约定**告诉 OpenAPI**：在 schema 里声明一个 apiKey 类型的
# securityScheme。声明之后 Swagger UI 才会渲染那个 Authorize 按钮，
# 用户填一次 token，之后所有请求自动带上。
def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "AgentDeskToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": (
                "部署到公网时开启的访问令牌，值在服务器 .env 的 AGENT_TOKEN。\n\n"
                "点右上角 Authorize 填入后，后续请求会自动带上这个头。\n"
                "本地开发（AUTH_ENABLED=0）时不需要填。"
            ),
        }
    }
    schema["security"] = [{"AgentDeskToken": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

# ============================================================
# 审计日志
# ============================================================
# 【为什么要有审计】
# 通用 AI 助手跑一次，你只有聊天记录 —— 非结构化、没法查、没法追责。
# 而运维这件事的硬要求是：谁、在什么时候、对哪台机器、做了什么、结果如何。
# 每次关键动作都追加一行 JSON，就有了可追溯的结构化记录。
#
# 用 JSONL（每行一个 JSON）而不是一个大 JSON 数组，是因为追加写入更安全：
# 写到一半进程挂了，前面几行仍然是完整的。
AUDIT_LOG = PROJECT_ROOT / "logs" / "audit.jsonl"
# 审计写入的锁。
#
# ★ 为什么别的写入都有锁、唯独这里漏了：
#   `tracer` 有 Lock、`approvals` 有 RLock，而 `write_audit` 一直是裸的 ——
#   因为它的写入是"小的单行追加"，看起来原子。但**审计是并发最狠的那条路径**
#   （每个接口、每次工具调用、每次通知都写），单行追加在文件层是原子的，
#   而 `json.dumps` 与后续可能的轮转不是。M4 给审计加了轮转之后必须补锁 ——
#   轮转是"改名 + 重新打开"，和另一个线程的追加撞在一起就是丢数据。
_audit_lock = threading.Lock()


def write_audit(event: str, detail: dict) -> dict:
    """追加一条审计记录，返回写入的内容。

    ★ 带上 `request_id`（如果当前在某个请求里）。
      没有它，日志与审计是两条平行的、对不上的线：
      "这次请求的日志"和"它写了哪几条审计"只能靠时间去猜。
      请求之外的审计（后台任务、启动期）没有这个字段 —— **不补空串**，
      免得让"没有请求上下文"和"请求 ID 恰好是空"混成一种样子。
    """
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **detail,
    }
    rid = logsetup.get_request_id()
    if rid:
        record["request_id"] = rid
    with _audit_lock:
        # 交给公共写入层：它负责"先看要不要轮转，再追加"。
        # 审计**要 fsync**：它是"谁在何时做了什么"的唯一证据，
        # 而写入频率远低于 trace（每次动作一条，不是每次请求几条）。
        obs_jsonl.append_jsonl(AUDIT_LOG, record, fsync=True)
    return record


# 通知层与工具层的审计出口：
# 这两个包刻意都不 import 本模块（否则循环导入），它们通过钩子拿到写审计的函数。
# **痕迹必须落在审计里** —— 否则"以为在通知/以为拒绝了、其实什么都没记"会静默很久。
notify_events.set_audit_hook(write_audit)


def _install_ops_audit_hook() -> None:
    """把审计出口接给工具层（`policy.denied` / `approval.requested` / 只读 run_command）。

    放在函数里而不是模块顶层：工具层的 import 链比通知层重，
    顶层立刻 import 会让"只想读一下 openapi"这种场景也去加载整套工具。
    """
    from app.tools import ops
    ops.set_audit_hook(write_audit)


_install_ops_audit_hook()


def _display_path(path) -> str:
    """尽量显示成项目内相对路径；不在项目内就退回绝对路径。

    ★ 原先这里是直接 `obs.TRACE_PATH.relative_to(PROJECT_ROOT)`，
      只要 trace 文件被放到项目根之外（挂载卷、自定义部署路径、
      测试用的临时目录），就会抛 ValueError 让 `/metrics/summary` 直接 500。

      **一个只读看板不该因为"文件放在哪儿"而挂掉** —— 路径显示是给人看的，
      取不到相对路径就显示绝对路径，而不是报错。
    """
    try:
        return str(Path(path).relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


# ============================================================
# 请求模型
# ============================================================
class ChatRequest(BaseModel):
    """pydantic 会自动做校验 —— 字段缺失或类型不对会直接返回 422，
    业务代码里不用写任何 if 判断。"""

    question: str = Field(..., min_length=1, max_length=2000,
                          description="用户的提问")


class ChatResponse(BaseModel):
    answer: str


# ============================================================
# 意图解析（含自动重试）
# ============================================================
INTENT_SYSTEM_PROMPT = """你是一个运维意图解析器。把用户的一句话解析成 JSON。

字段定义：
- action      要做的事，只能取这四个值之一：
              diagnose（诊断排查）/ restart（重启服务）/ cleanup（清理资源）/ query（查询信息）
- service     涉及的服务名，如 nginx / mysql / docker。用户没提到就填 null
- host        主机名，如 web-01。用户没提到就填 null
- risk        危险程度，只能取 low / medium / high。
              判断依据：只读、查原因、问原理是 low；
              **重启服务、清理资源、任何"动手改系统"的请求都是 high**；
              认不出来填 medium。
              ★ 注意：这只是一个**判断意见**，不是最终决定。
                真正的放行规则在策略层（policy.ACTION_RISK），
                你判得比它松时以它为准 —— 我只能让你更谨慎，不能让你更冒险。
                （这个值曾经被当成决定依据，结果"重启服务"被标成 medium 就放行了。）
- need_confirm 是否需要人工确认，布尔值。risk 为 high 时必须为 true
- reason      一句话说明你判断的依据

硬性要求：
1. 只输出 JSON 对象本身。不要用 markdown 代码块包裹，不要写任何解释文字
2. 字段名必须和上面完全一致，不要增加也不要减少字段
3. 不确定的内容填 null，不要编造

示例：
输入：查一下 mysql 的进程还在不在
输出：{"action":"query","service":"mysql","host":null,"risk":"low","need_confirm":false,"reason":"只读查询，无副作用"}"""

REQUIRED_FIELDS = ["action", "service", "host", "risk", "need_confirm"]
ALLOWED_RISK = ["low", "medium", "high"]

# 总共问几次：1 次原始 + 2 次自我修正
MAX_PARSE_ATTEMPTS = 3


class IntentParseFailed(Exception):
    """意图解析在重试用尽后仍然不合格。"""

    def __init__(self, message: str, problems=None, data=None):
        super().__init__(message)
        self.problems = problems or []
        self.data = data


def validate_intent(data: dict):
    """检查意图解析结果。返回 (是否通过, 问题列表)。

    ★ 第一句必须挡住"根本不是一个对象"的输入。

      模型输出裸 `null`、`[]`、数字都是**合法 JSON**，`app.llm.parse_json_reply`
      会照原样解析出来并交给这里。而原先这里直接 `if field not in data` ——
      对 None 会抛
          TypeError: argument of type 'NoneType' is not iterable

      这个异常的后果比"解析失败"严重得多：`parse_intent` 的 try 只捕获
      `ModelError`，所以它会穿过 `/parse` 与 `/webhook/alert` 的
      `except IntentParseFailed`，变成 **HTTP 500**，
      并且**既不记 parse_failed 审计、也不触发重试** —— 无人值守链路静默中断
      （凌晨三点那条告警就这么没了，日志里什么都查不到）。

      **校验层的职责是"判定"，不是"把异常抛给调用方"。**
      挡住之后，它退化成一个正常的"不合格 → 重试 → 到上限报错"路径。
    """
    if not isinstance(data, dict):
        return False, [f"意图解析结果必须是 JSON 对象，收到 {type(data).__name__}"]

    problems = []
    for field in REQUIRED_FIELDS:
        if field not in data:
            problems.append(f"缺少字段: {field}")
    if "risk" in data and data["risk"] not in ALLOWED_RISK:
        problems.append(f"risk 取值不合法: {data['risk']!r}")
    return (len(problems) == 0, problems)


def parse_intent(question: str):
    """解析意图，不合格就自我修正重试。返回 (intent, 实际尝试次数)。

    【自动重试为什么有效】
    结构化输出失败的原因通常很具体 —— 少个字段、取值不在范围内、
    多包了一层 markdown。把「你的输出有什么问题」直接告诉模型，
    它第二次基本能改对。实测一次重试能把成功率从七成提到九成以上。
    这是生产环境做结构化输出的标准做法，也是常见的关注点。

    【为什么把失败也做成异常抛出去，而不是返回 None】
    调用方需要区分"解析失败"和"解析成功但字段恰好是空的"。
    返回 None 会让这两种情况混在一起，没法处理。
    """
    messages = [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    last_data = None
    problems = []

    for attempt in range(1, MAX_PARSE_ATTEMPTS + 1):
        try:
            data = chat_json(messages)
        except ModelError as e:
            # 模型层错误（网络抖动、返回不是 JSON）也值得重试
            problems = [str(e)]
            continue

        last_data = data
        ok, problems = validate_intent(data)
        if ok:
            return data, attempt

        # 把不合格的输出和问题一起反馈回去，让它对着改
        messages.append({
            "role": "assistant",
            "content": json.dumps(data, ensure_ascii=False),
        })
        messages.append({
            "role": "user",
            "content": ("你上一次的输出有以下问题：" + "；".join(problems) +
                        "。请只输出修正后的 JSON 对象，不要任何解释文字。"),
        })

    raise IntentParseFailed("意图解析在重试后仍不合格", problems, last_data)


# ============================================================
# 告警链路：配置与状态
# ============================================================
# 归一化（normalize_alerts）与提问拼装（alert_to_question）已经搬去
# app/alerting/normalize.py —— 这样它们能被单独测试，不用 import 整个服务入口。
# 本文件顶部 re-export 了这两个名字，既有调用方与测试一行都不用改。
#
# 聚合与抑制（app/alerting/）解决的是"调模型**之前**"该做的事：
#   聚合：一场风暴里 50 条同源告警是一个故障，不是 50 个 → 只诊断一次
#   抑制：计划内维护窗口里的告警不该叫人起床

# ★ 是否让告警**真的**触发一轮 Agent 诊断。**默认关闭。**
#
#   为什么默认关：开启后每条告警都要跑一次完整的模型链路（实测 6–20 秒、
#   几分钱）。"自动"是有价的 —— 凌晨网络抖一下推 500 条告警，
#   就是 500 次诊断。所以在能力打开的同时，必须带上下面三道闸：
#
#     ① 去重/聚合：同源告警窗口内只处理一次，其余并入同一个事件
#     ② 抑制：维护窗口内的告警直接跳过
#     ③ 频控：每小时最多自动诊断 N 次，超了转人工
#
#   **这三道闸必须挡在调模型之前**，否则被抑制的那部分告警也在悄悄烧钱，
#   那才是真正的"告警风暴"。
ALERT_AUTO_DIAGNOSE = (os.getenv("ALERT_AUTO_DIAGNOSE", "0").strip() == "1")
ALERT_DEDUP_SECONDS = int(os.getenv("ALERT_DEDUP_SECONDS", "600"))
ALERT_MAX_PER_HOUR = int(os.getenv("ALERT_MAX_PER_HOUR", "10"))

# 聚合开关（默认开）：
#   1 = 按「主机 + 服务」聚合：50 条同源告警 → 1 个事件、1 次诊断
#   0 = 退回旧的「告警名 + 主机」精确去重语义，行为与加这个开关之前完全一致
ALERT_AGGREGATE = (os.getenv("ALERT_AGGREGATE", "1").strip() == "1")
ALERT_AGG_MAX_ENTRIES = int(os.getenv("ALERT_AGG_MAX_ENTRIES", "5000"))

# 告警入口是否异步（默认同步，与既有响应形态一致）
ALERT_ASYNC = (os.getenv("ALERT_ASYNC", "0").strip() == "1")

# ★ 去重/聚合表。它替代了原先那个 `_ALERT_LAST_SEEN = {}` ——
#   旧实现只增不减：长期运行内存单调增长，而且不报错、不告警，
#   只是一天天变大（那种"谁也不会注意到"的泄漏）。
#   新实现带 TTL 与容量上限，超了先清过期、再淘汰最旧的。
_ALERT_AGG = AlertAggregator(
    window_seconds=ALERT_DEDUP_SECONDS,
    aggregate=ALERT_AGGREGATE,
    max_entries=ALERT_AGG_MAX_ENTRIES,
)
_ALERT_SILENCE = Silence()
_ALERT_HOUR = {"hour": "", "count": 0}

DIAGNOSE_PLAYBOOK = {
    "nginx": ["systemctl status nginx", "tail -100 /var/log/nginx/error.log",
              "df -h", "ss -lntp | grep :80"],
    "mysql": ["systemctl status mariadb", "df -h", "free -m",
              "tail -100 /var/log/mysql/error.log"],
    "mariadb": ["systemctl status mariadb", "df -h", "free -m",
                "tail -100 /var/log/mysql/error.log"],
    "docker": ["docker ps -a", "docker logs --tail 100 <container>",
               "df -h", "systemctl status docker"],
}
DEFAULT_PLAYBOOK = ["uptime", "df -h", "free -m", "systemctl --failed"]


# `normalize_alerts` / `alert_to_question` 已搬到 app/alerting/normalize.py，
# 本文件顶部 re-export（`m.normalize_alerts` 这类既有引用照常可用）。
# 搬家的理由：它们与告警聚合/抑制是一件事，放一起才能单独测。


# ============================================================
# 接口 0：首页
# ============================================================
# ============================================================
# 三页共享的视觉基建：设计变量（亮/暗）+ 流式/时间线/图表组件样式。
# 经 __BASE_CSS__ 占位注入各页 <style> 的**末尾** —— 级联上后到者胜，
# 暗色覆盖才能压过页面原有的亮色硬编码。
# ============================================================
_BASE_CSS = """
/* ★ 固定明亮模式（用户 2026-09-30 决定）：不跟系统 prefers-color-scheme 走。
   原因：演示与截图场景以亮色为主，暗色双套维护面大于收益。 */
:root {
  --bg: #f6f8fa; --surface: #ffffff; --text: #1f2328; --muted: #59636e;
  --border: #e6e8eb; --accent: #0969da; --ok: #1f883d; --err: #cf222e;
  --warn: #d4a017; --track: #eef0f2;
}
/* ---- 流式步骤 ---- */
.steps { display: flex; flex-direction: column; gap: 6px; }
.step { display: flex; align-items: baseline; gap: 8px; font-size: 13.5px;
        padding: 6px 2px; border-bottom: 1px dashed var(--border); }
.step:last-child { border-bottom: 0; }
.step .dot { width: 8px; height: 8px; border-radius: 50%;
             background: var(--accent); flex: 0 0 auto;
             align-self: center; }
.step.done .dot { background: var(--ok); }
.step .ms { color: var(--muted); font-size: 12px; margin-left: auto; }
.step .sum { color: var(--muted); font-size: 12.5px; }

/* ---- 执行轨迹时间线 ---- */
.tl { display: flex; flex-direction: column; gap: 7px; margin-top: 8px; }
.tl-row { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
.tl-name { width: 92px; flex: 0 0 auto; color: var(--muted);
           text-align: right; }
.tl-track { flex: 1 1 auto; height: 10px; border-radius: 5px;
            background: var(--track); overflow: hidden; }
.tl-fill { height: 100%; border-radius: 5px; background: var(--accent); }
.tl-fill.bad { background: var(--err); }
.tl-ms { width: 64px; flex: 0 0 auto; color: var(--muted);
         font-variant-numeric: tabular-nums; }

/* ---- 横向条形图（手写 SVG 外的行内条）---- */
.hbar { display: flex; align-items: center; gap: 8px;
        font-size: 12.5px; margin: 6px 0; }
.hbar .label { width: 110px; flex: 0 0 auto; color: var(--muted);
               text-align: right; }
.hbar .track { flex: 1 1 auto; height: 12px; border-radius: 6px;
               background: var(--track); overflow: hidden; }
.hbar .fill { height: 100%; background: var(--accent); border-radius: 6px; }
.hbar .val { width: 76px; flex: 0 0 auto; color: var(--text);
             font-variant-numeric: tabular-nums; }

/* ---- 暗色模式：覆盖三页已有的亮色硬编码 ---- */
"""

# ============================================================
# 两页共享的前端工具：节点中文名 / 时间线 / 条形图。纯原生 JS，
# 经 __BASE_JS__ 占位注入 /try 与 /dashboard 的 <script>。
# ============================================================
_BASE_JS = """
var NL = String.fromCharCode(10);
var NODE_LABEL = { supervisor: "调度决策", intent: "意图路由",
  knowledge: "知识检索", diagnose: "工具执行", reason: "纯推理分析",
  verify: "结果校验", remediate: "处置建议", finalize: "汇总输出" };

function timelineHTML(items) {
  /* 执行时间线：条宽按耗时占比。items: [{node|name, elapsed_ms, ok, summary?}] */
  items = items || [];
  var total = 0;
  items.forEach(function (it) { total += (it.elapsed_ms || 0); });
  if (!total) return '<p class="hint">（无计时数据）</p>';
  var rows = "";
  items.forEach(function (it) {
    var ms = it.elapsed_ms || 0;
    var pct = Math.max(1, Math.round(ms * 100 / total));
    var nm = it.node || it.name || "?";
    rows += '<div class="tl-row">' +
      '<span class="tl-name">' + (NODE_LABEL[nm] || nm) + '</span>' +
      '<span class="tl-track"><span class="tl-fill' +
      (it.ok === false ? ' bad' : '') + '" style="width:' + pct +
      '%"></span></span>' +
      '<span class="tl-ms">' + ms + 'ms</span></div>';
  });
  return '<div class="tl">' + rows +
    '<div class="hint">总耗时 ' + (total / 1000).toFixed(1) +
    's · 条宽按各步耗时占比</div></div>';
}

function hbars(m) {
  /* 横向条形图：{名字: {cost_cny, ...}} 按成本降序。纯 DOM/CSS，不引图表库 */
  var keys = Object.keys(m || {}).sort(function (a, b) {
    return ((m[b] || {}).cost_cny || 0) - ((m[a] || {}).cost_cny || 0); });
  var max = 0;
  keys.forEach(function (k) { max = Math.max(max, (m[k] || {}).cost_cny || 0); });
  if (!max) return '<p class="hint">（暂无数据）</p>';
  var h = "";
  keys.forEach(function (k) {
    var v = (m[k] || {}).cost_cny || 0;
    var pct = Math.max(1, Math.round(v * 100 / max));
    h += '<div class="hbar"><span class="label">' + esc(k) + '</span>' +
      '<span class="track"><span class="fill" style="width:' + pct +
      '%"></span></span>' +
      '<span class="val">' + money(v) + '</span></div>';
  });
  return h;
}
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(response: Response):
    """根路径给一张「这是什么 + 怎么试」的导航页。

    【为什么要这个接口】
    服务起来之后，人第一次打开 http://127.0.0.1:8000 看到的是 404 ——
    得先知道 `/docs` 这个约定才知道去哪儿，而**访客或同事不会知道**。

    一个几十行的首页，把"这是什么、从哪开始试、有哪些能力"一次说清，
    是演示成本最低、收益最直接的一步。

    `include_in_schema=False`：它只是导航页，不是业务接口，
    不该混进 /docs 的接口清单里干扰视线。
    """
    # ---- 和 /try 页同一个道理：要不要令牌，服务端定死，别让页面去猜 ----
    # 首页原先无条件写「除几个路径外都要求令牌」，这在本地（AUTH_ENABLED=0）
    # 是**错的**：第一次打开的人会以为必须先搞到一个令牌才能用。
    needs_token = security.AUTH_ENABLED
    if needs_token:
        docs_hint = "交互式文档。要先点右上角 Authorize 填 token，否则一律 401"
        auth_block = (
            '<p style="font-size:13px;color:#59636e">'
            '除 <code>/</code>、<code>/try</code>、<code>/dashboard</code>、'
            '<code>/health</code>、<code>/docs</code> 这几个<b>页面</b>之外，'
            '所有<b>接口</b>都要求请求头带令牌（<code>X-API-Key</code>）。'
            '（页面是空壳，数据仍由接口那一步的令牌保护。）'
            '所以直接点上面表格里的链接会看到 401 &mdash; 那是安全层在正常工作，不是服务坏了。</p>'
        )
        try_step = "填令牌、填问题、点「开始」"
        curl_demo = (
            '<pre>curl -H "X-API-Key: &lt;你的令牌&gt;" \\\n'
            '     -H "Content-Type: application/json" \\\n'
            '     -d \'{"question":"web-01 磁盘快满了怎么处理"}\' \\\n'
            '     https://agent.simosheng.fun/rag/ask   # 或 /agent/ask</pre>\n'
            '<p style="font-size:13px;color:#59636e">令牌在服务器的 '
            '<code>/opt/agentdesk/.env</code> 里，<code>AGENT_TOKEN</code> 那一行。</p>'
        )
        acl_block = (
            "鉴权采用白名单：默认拒绝，只放行上面标注的那几个路径。"
        )
    else:
        docs_hint = "交互式文档。未开鉴权时直接点 Try it out 即可"
        auth_block = (
            '<p style="font-size:13px;color:#59636e">'
            '这个实例<b>没有开启鉴权</b>（<code>AUTH_ENABLED=0</code>），'
            '上面表格里的链接点开就能用，不需要任何令牌。</p>'
        )
        try_step = "填问题、点「开始」"
        curl_demo = (
            '<pre>curl -H "Content-Type: application/json" \\\n'
            '     -d \'{"question":"web-01 磁盘快满了怎么处理"}\' \\\n'
            '     http://127.0.0.1:8000/rag/ask   # 或 /agent/ask</pre>\n'
            '<p style="font-size:13px;color:#59636e">本地实例不需要令牌；'
            '部署到公网时必须打开鉴权并带上 <code>X-API-Key</code>。</p>'
        )
        acl_block = (
            "本地未开鉴权；部署到公网时会打开白名单鉴权，默认拒绝、只放行标注的路径。"
        )

    links = [
        ("/try", "★ 在线试用", "填问题、点按钮，看它自己决定查什么。不用懂 API，从这里开始"),
        ("/dashboard", "★ 可观测看板", "成本归因、对账偏差、逐步轨迹、待审批 —— 不用敲命令就能看"),
        ("/docs", "26 个接口的调试台", docs_hint),
        ("/view/metrics", "成本看板", "按 Agent / 动作两维看成本、缓存命中率、P95 延迟（数据源 /metrics/summary）"),
        ("/view/traces", "链路追踪", "每次运行发生了什么、哪一步最慢最贵（数据源 /traces）"),
        ("/view/tools", "工具清单", "Agent 能调用的 7 个工具，含风险等级（数据源 /agent/tools）"),
        ("/view/sandbox", "沙箱状态", "当前是 mock 还是真容器、白名单概览、fail-closed（数据源 /sandbox）"),
        ("/view/approvals", "审批单", "Agent 想执行但还没执行的写操作（数据源 /approvals）"),
        ("/view/audit", "审计日志", "谁、何时、哪条告警、判成什么风险、做了什么（数据源 /audit）"),
        ("/view/health", "健康检查", "不需要 token，给容器探活和监控用（数据源 /health）"),
        ("/settings", "系统设置", "查看/修改访问令牌；切换工具层数据源（mock / 本机 / ssh）与 SSH 目标"),
    ]
    # 「地址 | 是什么」两列合并成一列：点「是什么」就跳转，地址以小字附在链接里
    rows = "".join(
        f'<tr><td><a href="{u}"><b>{n}</b><br><code>{u}</code></a></td>'
        f'<td>{d}</td></tr>'
        for u, n, d in links
    )
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentDesk</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      max-width:860px;margin:0 auto;padding:40px 24px;color:#1f2328;
      line-height:1.65;background:#fff}}
 h1{{margin:0 0 4px;font-size:26px}}
 .sub{{color:#59636e;margin-bottom:28px}}
 h2{{font-size:16px;margin:30px 0 10px;padding-bottom:6px;
    border-bottom:1px solid #e6e8eb}}
 table{{width:100%;border-collapse:collapse;font-size:14px}}
 td,th{{padding:9px 10px;border-bottom:1px solid #f0f1f3;text-align:left;
       vertical-align:top}}
 th{{color:#59636e;font-weight:600;font-size:13px}}
 code{{background:#f4f5f7;padding:2px 6px;border-radius:4px;
      font-family:ui-monospace,Consolas,monospace;font-size:13px}}
 a{{color:#0969da;text-decoration:none}} a:hover{{text-decoration:underline}}
 pre{{background:#f6f8fa;padding:14px 16px;border-radius:8px;overflow-x:auto;
     font-size:13px;border:1px solid #e6e8eb}}
 .note{{background:#fff8e6;border-left:3px solid #d4a017;padding:12px 16px;
       border-radius:0 6px 6px 0;font-size:14px;margin:16px 0}}
 .k{{display:inline-block;background:#eef4ff;color:#0550ae;border-radius:4px;
    padding:1px 7px;font-size:12px;margin-right:6px}}
__BASE_CSS__
</style></head><body>

<h1>AgentDesk</h1>
<div class="sub">面向运维场景的多 Agent 智能体系统 &middot;
<span class="k">模型调用</span><span class="k">私有知识检索</span>
<span class="k">工具执行</span><span class="k">沙箱 + 人工确认</span></div>

<h2>这是什么</h2>
<p>一个 HTTP 服务：告警或人发起请求 &rarr; 多个专业 Agent 分工诊断 &rarr;
需要动手时提交人工审批 &rarr; 在一次性容器里执行 &rarr; 全过程留痕、可查成本。</p>

<h2>从哪儿开始试</h2>
<table>
<tr><th>是什么（点击打开）</th><th>说明</th></tr>
{rows}
</table>
{auth_block}

<h2>最快的一次体验</h2>
<p>打开 <a href="/try"><b>/try</b></a>：{try_step}。
它会自己决定查什么、查几轮，返回里带完整的执行轨迹和校验结论。</p>
<p>换个知识库里没有的问题问它（比如「Kafka 消费组 lag 怎么排查」），
它会明确告诉你<strong>知识库里没有</strong> &mdash; 这是刻意设计的，不是能力不足。</p>

<h2>用命令行调</h2>
{curl_demo}

<div class="note"><b>安全边界</b>：所有写操作（重启服务、清空日志）
都不会自动执行。它们会变成一张审批单挂在 <a href="/view/approvals"><b>审批单</b></a>，
等人批准；沙箱不可用时宁可拒绝执行，也不降级。</div>

<p style="margin-top:32px;color:#8b949e;font-size:13px">
演示实例曾部署在阿里云一台 2 核 2G 的机器上（完整部署实录见 docs/deployment.md），<b>当前暂停对外开放</b> —— 按 docs/quickstart-own-server.md 可部署到你自己的机器。
{acl_block}
部署过程与安全设计见仓库的 <code>docs/deployment.md</code>。</p>
</body></html>"""

    # HTML 一律不缓存 —— 和 /try 页同一个理由（服务端渲染、内容随配置变）。
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    # f-string 先求值（占位符不含花括号），再注入共享样式
    page = page.replace('__BASE_CSS__', _BASE_CSS)
    return page


# ============================================================
# 在线试用页
# ============================================================
@app.get("/try", response_class=HTMLResponse, include_in_schema=False)
def try_page(response: Response):
    """一个不用懂 API 就能试的页面。

    【为什么要有它】
    /docs 是给开发者的，它要求你懂 HTTP 方法、请求体格式、鉴权头。
    但演示的观众是第一次接触它的人 —— 他们不会为了看你一个项目去学怎么发 curl。
    一个「填问题 → 点按钮 → 看结果」的页面，把试用的门槛降到了零。

    页面逻辑在浏览器里（token 存 localStorage，点击后 fetch /agent/ask），
    但**「要不要令牌」这个决定是服务端做的**：见函数末尾的替换逻辑。
    """
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentDesk · 在线试用</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      max-width:900px;margin:0 auto;padding:32px 20px 80px;color:#1f2328;
      line-height:1.65;background:#fff}
 h1{margin:0 0 4px;font-size:24px}
 .sub{color:#59636e;margin-bottom:24px;font-size:14px}
 h2{font-size:15px;margin:26px 0 10px;padding-bottom:6px;
    border-bottom:1px solid #e6e8eb;color:#24292f}
 label{display:block;font-size:13px;color:#59636e;margin:12px 0 5px}
 input,select,textarea{width:100%;padding:9px 11px;border:1px solid #d0d7de;
      border-radius:6px;font-size:14px;font-family:inherit;background:#fff;color:#1f2328}
 textarea{min-height:74px;resize:vertical;line-height:1.5}
 input:focus,select:focus,textarea:focus{outline:2px solid #0969da;
      outline-offset:-1px;border-color:#0969da}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .row>div{flex:1;min-width:210px}
 button{margin-top:16px;padding:10px 22px;background:#1f883d;color:#fff;
      border:0;border-radius:6px;font-size:15px;font-weight:600;cursor:pointer}
 button:hover{background:#1a7f37} button:disabled{background:#94d3a2;cursor:wait}
 .ghost{background:#f6f8fa;color:#24292f;border:1px solid #d0d7de;
      font-weight:400;padding:6px 12px;font-size:13px;margin:0}
 .ghost:hover{background:#eef1f4}
 .hint{font-size:12.5px;color:#59636e;margin:5px 0 0}
 .card{border:1px solid #e6e8eb;border-radius:10px;padding:18px 20px;
      margin-top:14px;background:#fff}
 .note{background:#fff8e6;border-left:3px solid #d4a017;padding:11px 15px;
      border-radius:0 6px 6px 0;font-size:13.5px;margin:14px 0}
 .err{background:#fff0f0;border-left:3px solid #cf222e;padding:11px 15px;
      border-radius:0 6px 6px 0;font-size:13.5px;margin:14px 0;color:#a40e26}
 .ok{background:#eaf7ee;border-left:3px solid #1f883d;padding:11px 15px;
      border-radius:0 6px 6px 0;font-size:13.5px;margin:14px 0;color:#0a5c26}
 pre{background:#f6f8fa;padding:13px 15px;border-radius:8px;overflow-x:auto;
      font-size:13px;border:1px solid #e6e8eb;
      font-family:ui-monospace,Consolas,monospace;white-space:pre-wrap;
      word-break:break-word}
 code{background:#f4f5f7;padding:2px 5px;border-radius:4px;
      font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
 table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}
 td,th{padding:7px 9px;border-bottom:1px solid #eef0f2;text-align:left;
      vertical-align:top}
 th{color:#59636e;font-weight:600;font-size:12.5px;background:#fafbfc}
 .md{font-size:14.5px} .md h1,.md h2,.md h3{font-size:15px;margin:16px 0 8px;
      border:0;padding:0} .md table{font-size:13px}
 .md pre{font-size:12.5px}
 .spin{display:inline-block;width:13px;height:13px;border:2px solid #94d3a2;
      border-top-color:#1f883d;border-radius:50%;animation:sp .8s linear infinite;
      vertical-align:-2px;margin-right:8px}
 @keyframes sp{to{transform:rotate(360deg)}}
 .meta{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:#59636e;
      padding:11px 0 3px}
 .meta b{color:#1f2328;font-weight:600}
__BASE_CSS__
</style></head><body>

<h1>AgentDesk 在线试用</h1>
<div class="sub">多 Agent 智能体系统 &middot; 面向运维场景 &middot;
填问题、点按钮，看它自己决定查什么</div>

<div class="note" id="authnote">__AUTH_NOTE__</div>

__DATA_SOURCE_NOTE__

<div class="card" id="tokcard"__TOKCARD_STYLE__>
  <label for="token">访问令牌</label>
  <div class="row">
    <div style="flex:3"><input id="token" type="password"
      placeholder="粘贴 AGENT_TOKEN 的值" autocomplete="off"></div>
    <div style="flex:0 0 auto;align-self:flex-end">
      <button class="ghost" id="forget">清除</button></div>
  </div>
  <p class="hint" id="tokstate"></p>
</div>

<div class="card">
  <label for="q">问它一个问题</label>
  <textarea id="q">web-01 上的网站访问很慢，有时报 502，帮我看下原因</textarea>

  <div class="row">
    <div>
      <label for="mode">能力</label>
      <select id="mode">
        <option value="supervisor">多 Agent 编排（推荐，约 10-30 秒）</option>
        <option value="rag">知识库问答 · 带引用溯源（约 4 秒）</option>
        <option value="langgraph">Agent 诊断 · LangGraph 状态图</option>
        <option value="handwritten">Agent 诊断 · 手写 ReAct 循环</option>
      </select>
    </div>
  </div>

  <button id="go">开始</button>
  <p class="hint">多 Agent 编排会调用真实模型，属于计费动作，计入每日额度。</p>
</div>

<div id="out"></div>

<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script>
var $ = function (id) { return document.getElementById(id); };
var KEY = 'agentdesk_token';
var t0 = 0, timer = null;

(function restore() {
  var v = localStorage.getItem(KEY) || '';
  $('token').value = v;
  $('tokstate').textContent = v ? '已保存令牌（本机浏览器），直接点「开始」即可。'
                                : '还没有填写令牌。';
})();

$('forget').onclick = function () {
  localStorage.removeItem(KEY);
  $('token').value = '';
  $('tokstate').textContent = '已清除。';
};

// ---- 要不要令牌：由**服务端渲染时**写进来，前端不探测 ----
// ★ 这里连续踩过两次，值得记住：
//   第一版：页面**无条件**要求令牌 —— 它假设自己一定部署在公网。
//           结果本地关着鉴权（AUTH_ENABLED=0）也照样被前端拦住，而服务端
//           其实压根没检查。**前端不该假设后端的部署形态。**
//   第二版：改成前端 fetch /health 探测。看起来对，但有三个失败面：
//           ① 浏览器缓存旧页面 → 探测代码根本没跑；
//           ② 相对路径 /health 在某些加载方式（预览面板、代理、file://）下取不到；
//           ③ 探测是异步的 —— 手快在它返回前点「开始」，NEEDS_TOKEN 还是初值 true。
//   第三版（当前）：服务端**本来就知道**答案，直接写进页面，前端只读不算。
//           把决定权放在知道答案的那一侧，就不会有这三类问题。
__BASE_JS__
var NEEDS_TOKEN = __NEEDS_TOKEN__;

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;');
}

function md(text) {
  if (typeof marked !== 'undefined' && marked.parse) {
    try { return marked.parse(text); } catch (e) { /* 降级 */ }
  }
  return '<pre>' + esc(text) + '</pre>';
}

function elapsed() { return ((Date.now() - t0) / 1000).toFixed(1); }

function tick() {
  $('out').innerHTML = '<div class="card"><span class="spin"></span>' +
    '正在处理…已等待 <b>' + elapsed() + '</b> 秒' +
    '<p class="hint">多 Agent 编排要跑好几轮工具调用，慢是正常的。</p></div>';
}

$('go').onclick = async function () {
  var token = $('token').value.trim();
  var question = $('q').value.trim();
  var mode = $('mode').value;

  // 只有服务端确实开了鉴权时才要求令牌 —— 见上面 probeAuth 的说明
  if (NEEDS_TOKEN && !token) {
    $('out').innerHTML = '<div class="err">请先填写访问令牌。它在服务器的 ' +
      '<code>/opt/agentdesk/.env</code> 里。</div>';
    return;
  }
  if (!question) {
    $('out').innerHTML = '<div class="err">问题不能为空。</div>';
    return;
  }

  if (token) {
    localStorage.setItem(KEY, token);
    $('tokstate').textContent = '已保存令牌（本机浏览器），直接点「开始」即可。';
  }
  $('go').disabled = true;
  t0 = Date.now();
  tick();
  timer = setInterval(tick, 400);

  var path, body;
  if (mode === 'rag') {
    path = '/rag/ask';
    body = { question: question, top_k: 5, mode: 'hybrid' };
  } else {
    path = '/agent/ask';
    body = { question: question, engine: mode, include_trace: true };
  }

  // 没有令牌就不带这个头 —— 服务端没开鉴权时，多带一个空令牌只会让人困惑
  var headers = { 'Content-Type': 'application/json' };
  if (token) { headers['X-API-Key'] = token; }
  // supervisor 引擎走流式 —— 编排过程本身就是演示的主菜，
  // 让访问者看着「意图路由 → 知识检索 → 正在查磁盘」一步步亮起来，
  // 而不是对着空白页干等 20 秒。其他引擎保持一次性请求。
  if (mode === 'supervisor') {
    clearInterval(timer);   // ★ 流式有自己的进度展示；不定时器会把
                            //   #out 连同 #steps/#final 覆盖掉，
                            //   result 到达时 getElementById('final') 为 null
    try {
      await askStream(question, headers);
    } catch (e) {
      $('out').innerHTML = '<div class="err"><b>流式请求失败：</b>' +
        esc(e.message) + '</div>';
    } finally {
      clearInterval(timer);
      $('go').disabled = false;
    }
    return;
  }

  try {
    var r = await fetch(path, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(body)
    });
    clearInterval(timer);
    var text = await r.text();
    var data = null;
    try { data = JSON.parse(text); } catch (e) { }

    if (r.status === 401) {
      $('out').innerHTML = '<div class="err"><b>令牌不对（HTTP 401）。</b>' +
        '请检查它是否和服务器 <code>.env</code> 里的 <code>AGENT_TOKEN</code> ' +
        '完全一致 —— 注意别多复制了空格或换行。</div>';
      return;
    }
    if (r.status === 429) {
      $('out').innerHTML = '<div class="err"><b>触发限流（HTTP 429）。</b>' +
        (data && data.detail ? esc(data.detail) : '') +
        '<p class="hint">每 IP 每分钟 20 次、全站 60 次、每天 300 次。等一下再试。</p></div>';
      return;
    }
    if (!r.ok) {
      $('out').innerHTML = '<div class="err"><b>HTTP ' + r.status + '</b><pre>' +
        esc(text.slice(0, 800)) + '</pre></div>';
      return;
    }

    $('out').innerHTML = render(data, mode);
  } catch (e) {
    clearInterval(timer);
    $('out').innerHTML = '<div class="err"><b>请求失败：</b>' + esc(e.message) +
      '<p class="hint">如果是网络错误，检查一下服务是否在跑：' +
      '<code>https://agent.simosheng.fun/health</code></p></div>';
  } finally {
    $('go').disabled = false;
  }
};

async function askStream(question, headers) {
  /* 消费 /agent/ask/stream 的 SSE：step 事件实时点亮，
     result 事件与一次性接口同一结构（同一个 _agent_payload），
     所以渲染直接复用 render()。 */
  $('out').innerHTML = '<div class="card"><h2 style="margin-top:0">执行过程</h2>' +
    '<div class="steps" id="steps"></div></div><div id="final"></div>';
  var stepBox = document.getElementById('steps');
  var gotResult = false;

  var r = await fetch('/agent/ask/stream', {
    method: 'POST', headers: headers,
    body: JSON.stringify({ question: question, engine: 'supervisor' })
  });
  if (r.status === 401) {
    $('out').innerHTML = '<div class="err"><b>令牌不对（HTTP 401）。</b>' +
      '请检查它是否和服务器 <code>.env</code> 里的 <code>AGENT_TOKEN</code> 完全一致。</div>';
    return;
  }
  if (r.status === 429) {
    $('out').innerHTML = '<div class="err"><b>触发限流（HTTP 429）。</b>等一下再试。</div>';
    return;
  }
  if (!r.ok || !r.body) {
    throw new Error('流式接口不可用（HTTP ' + r.status + '）');
  }

  var reader = r.body.getReader();
  var dec = new TextDecoder();
  var buf = '';
  var lastStep = null;
  while (true) {
    var c = await reader.read();
    if (c.done) break;
    buf += dec.decode(c.value, { stream: true });
    var i;
    while ((i = buf.indexOf(NL + NL)) >= 0) {
      var frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      var evName = '';
      var dataRaw = '';
      frame.split(NL).forEach(function (ln) {
        if (ln.indexOf('event: ') === 0) { evName = ln.slice(7); }
        else if (ln.indexOf('data: ') === 0) { dataRaw = ln.slice(6); }
      });
      if (!dataRaw) { continue; }
      var d = JSON.parse(dataRaw);
      if (evName === 'step') {
        if (lastStep) { lastStep.className = 'step done'; }
        var row = document.createElement('div');
        row.className = 'step';
        row.innerHTML = '<span class="dot"></span><b>' +
          (NODE_LABEL[d.node] || d.node) + '</b>' +
          (d.summary ? '<span class="sum">' + esc(d.summary) + '</span>' : '') +
          '<span class="ms">' + d.elapsed_ms + 'ms</span>';
        stepBox.appendChild(row);
        lastStep = row;
      } else if (evName === 'result') {
        if (lastStep) { lastStep.className = 'step done'; }
        gotResult = true;
        document.getElementById('final').innerHTML =
          '<h2>结果</h2>' + render(d, 'supervisor') +
          '<h2>执行时间线</h2><div class="card">' +
          timelineHTML(d.node_log) + '</div>';
      } else if (evName === 'error') {
        gotResult = true;
        document.getElementById('final').innerHTML =
          '<div class="err"><b>诊断中断：</b>' + esc(d.error || '未知错误') + '</div>';
      }
    }
  }
  // 流结束但没等到 result —— 被截断了。不能静默：空白和出错是两种状态
  if (!gotResult) {
    throw new Error('流在结果到达前结束（服务端可能中断）');
  }
}

function render(d, mode) {
  var html = '';
  if (d === null) {
    return '<div class="err">返回的不是合法 JSON。</div>';
  }

  var m = d.metrics || {};
  if (mode !== 'rag') {
    html += '<div class="meta">' +
      '<span>引擎 <b>' + esc(d.engine || mode) + '</b></span>' +
      '<span>轮次 <b>' + (m.rounds !== undefined ? m.rounds : '-') + '</b></span>' +
      '<span>工具调用 <b>' + (m.tool_calls !== undefined ? m.tool_calls : '-') + '</b></span>' +
      '<span>tokens <b>' + (m.tokens !== undefined ? m.tokens : '-') + '</b></span>' +
      '<span>耗时 <b>' + (m.elapsed_ms ? (m.elapsed_ms / 1000).toFixed(1) + 's' : elapsed() + 's') + '</b></span>' +
      '</div>';
  }

  html += '<h2>回答</h2><div class="card md">' +
    md(d.answer || '（没有返回 answer 字段）') + '</div>';

  if (d.sources && d.sources.length) {
    html += '<h2>引用来源</h2><div class="card"><table>' +
      '<tr><th>#</th><th>来源</th><th>标题</th><th>片段</th></tr>';
    d.sources.forEach(function (s, i) {
      html += '<tr><td>' + (i + 1) + '</td><td><code>' + esc(s.source || '') +
        '</code></td><td>' + esc(s.title || '') + '</td><td>' +
        esc((s.preview || s.text || '').slice(0, 150)) + '…</td></tr>';
    });
    html += '</table></div>';
  }

  if (d.trace && d.trace.length) {
    html += '<h2>执行轨迹</h2><div class="card"><table>' +
      '<tr><th>步</th><th>它的判断</th><th>调用的工具</th><th>参数</th><th>结果</th></tr>';
    d.trace.forEach(function (s) {
      html += '<tr><td>' + esc(s.step) + '</td><td>' +
        esc((s.thought || '').slice(0, 110)) + '</td><td><code>' +
        esc(s.tool || '') + '</code></td><td><code>' +
        esc(JSON.stringify(s.args || {})) + '</code></td><td>' +
        (s.ok ? '成功' : '失败') +
        (s.risk ? ' · 风险 ' + esc(s.risk) : '') + '</td></tr>';
    });
    html += '</table></div>';
  }

  return html;
}
</script>
</body></html>"""

    # ---- 把「要不要令牌」在服务端定死，再交给浏览器 ----
    # 服务端在启动时就知道 AUTH_ENABLED，没有任何理由让前端去探测（原因见上面 JS 的注释）。
    # 顺带把顶部提示和令牌框的初始状态也一起渲染好 —— 页面一打开就是最终形态，
    # 不再有「先显示令牌框、几百毫秒后消失」的闪动。
    needs_token = security.AUTH_ENABLED
    if needs_token:
        auth_note = (
            "<b>需要访问令牌。</b>这个实例部署在公网，除首页外的接口都要求 token。"
            "令牌在服务器 <code>/opt/agentdesk/.env</code> 的 "
            "<code>AGENT_TOKEN</code> 那一行。填一次即可，浏览器会记住。"
            "<br><b>别在公共电脑上填。</b>令牌存在本机浏览器里，等于一把钥匙。"
        )
        tokcard_style = ""
    else:
        auth_note = (
            "<b>本地实例，不需要令牌。</b>这个实例没有开启鉴权"
            "（<code>AUTH_ENABLED=0</code>），直接提问即可。"
            "<br>部署到公网时必须打开鉴权，见 README 的「公网安全层」。"
        )
        tokcard_style = ' style="display:none"'

    # ★★ 必须**如实告诉用户这些数字是从哪来的**。
    #
    #   起因：公网演示实例跑在 mock 后端（不连任何真实机器），
    #   而页面上没有任何提示。于是「web-01 磁盘还剩多少」会答
    #   「用了 96%」—— 一个**看起来像真的、实际是预置样例值**的答案，
    #   而真机上是 36%。README 里写了，但用户不会先读 README 再提问。
    #
    #   这个项目的卖点就是"数字可信"（对账、溯源、评测）。
    #   一个不标注数据来源的演示页，恰恰在削弱它自己的卖点。
    #   **数据来源不是脚注，是结论的一部分** —— 尤其在运维场景里，
    #   「我查到磁盘 96%」和「我查到样例值里的磁盘 96%」是两件事。
    from app.tools import ops as _ops
    backend = (_ops.BACKEND or "mock").lower()
    if backend == "mock":
        data_source_note = (
            '<div class="err" style="font-size:13.5px">'
            '<b>⚠️ 本实例用的是仿真数据，不连接任何真实机器。</b><br>'
            '工具层跑在 <code>mock</code> 后端，下面查到的磁盘 / 负载 / 容器 '
            '都是<b>预置的样例值</b>，不是你环境里的真实情况。<br>'
            '（真机后端是 <code>ssh</code>，本项目在本地就是这么跑的 —— '
            '两套实例刻意分开：演示服务不该和生产机共用取证通道。）'
            '</div>')
    else:
        # ★ 报"真正连得上的主机"，不是"已知主机名清单"。
        #   KNOWN_HOSTS 里有 db-01 / cache-01（逻辑主机名，用于意图路由），
        #   但它们没有配 SSH 目标、查询会明确报错。
        #   把它们列在"数据来自实时查询"后面，又是一次"看起来像真的"。
        reachable = list(_ops.SSH_TARGETS) if backend == "ssh" else list(_ops.KNOWN_HOSTS)
        hosts = "、".join(reachable)
        data_source_note = (
            '<div class="ok" style="font-size:13.5px">'
            f'✓ 数据来自<b>实时查询</b>（后端 <code>{backend}</code>'
            + (f'，主机 {hosts}' if hosts else "")
            + '）。每条结论都能追到具体命令与输出。</div>')

    html = (
        html.replace("__AUTH_NOTE__", auth_note)
        .replace("__DATA_SOURCE_NOTE__", data_source_note)
        .replace("__TOKCARD_STYLE__", tokcard_style)
        .replace("__BASE_JS__", _BASE_JS)
        .replace("__BASE_CSS__", _BASE_CSS)
        .replace("__NEEDS_TOKEN__", "true" if needs_token else "false")
    )

    # HTML 一律不缓存。
    # ★ 这次就是从「浏览器一直给旧页面」开始的 —— 服务端已经改好了，
    #   用户看到的还是老界面，而且刷新键都不一定管用（普通刷新会走缓存）。
    #   页面本身就是服务端渲染的、内容随时可能变，没有任何缓存的理由。
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return html


# ============================================================
# 可观测看板（页面）
# ============================================================
# ============================================================
# 数据可视化视图：把「一打开就是裸 JSON」的 7 个接口变成人能看的页面。
# ============================================================
# 【为什么是独立 /view/{name} 而不是让原接口返回 HTML】
# 这些 JSON 同时被程序消费（dashboard 的 fetch、MCP、curl 探活）——
# 改默认返回格式等于砸掉自己的 API。HTML 视图单独开路由，
# 数据还是同一个接口出的：**一个事实源，两种皮**。
_DATA_VIEWS = {
    "metrics":   ("/metrics/summary?limit=20", "成本看板",
                  "按 Agent / 动作两维的成本、缓存命中率、P95 延迟"),
    "traces":    ("/traces?limit=20", "链路追踪",
                  "每次运行发生了什么、哪一步最慢最贵"),
    "tools":     ("/agent/tools", "工具清单",
                  "Agent 能调用的工具与风险等级"),
    "sandbox":   ("/sandbox", "沙箱状态",
                  "当前后端（mock / 真容器）、隔离状态与命令白名单"),
    "approvals": ("/approvals", "审批单",
                  "Agent 想执行但还没执行的写操作，等人工确认"),
    "audit":     ("/audit?limit=30", "审计日志",
                  "谁、何时、哪条告警、判成什么风险、做了什么"),
    "health":    ("/health", "健康检查",
                  "服务存活与安全层状态（不需要令牌）"),
}

_VIEW_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentDesk · __TITLE__</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      max-width:1100px;margin:0 auto;padding:32px 20px 80px;color:#1f2328;
      line-height:1.65;background:#fff}
 h1{margin:0 0 4px;font-size:22px}
 h3{margin:18px 0 8px;font-size:14px;color:#59636e}
 .sub{color:#59636e;margin-bottom:16px;font-size:14px}
 .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}
 .bar input{flex:0 1 340px;padding:8px 10px;border:1px solid #d0d7de;
            border-radius:6px;font-size:13px}
 button{padding:8px 14px;background:#1f883d;color:#fff;border:0;
        border-radius:6px;font-size:13px;font-weight:600;cursor:pointer}
 button:hover{background:#1a7f37}
 button.ghost{background:#f6f8fa;color:#24292f;border:1px solid #d0d7de;
              font-weight:400}
 button.ghost:hover{background:#eef1f4}
 .card{border:1px solid #e6e8eb;border-radius:10px;padding:14px 16px;
       margin:10px 0;background:#fff}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:7px 9px;border-bottom:1px solid #f0f1f3;text-align:left;
       vertical-align:top;word-break:break-all}
 th{color:#59636e;font-weight:600;background:#fafbfc;white-space:nowrap}
 code{background:#f4f5f7;padding:1px 5px;border-radius:4px;
      font-family:ui-monospace,Consolas,monospace;font-size:12px}
 .hint{font-size:12.5px;color:#59636e}
 .err{background:#ffebe9;border-left:3px solid #cf222e;padding:10px 14px;
      border-radius:0 6px 6px 0;margin:10px 0;font-size:14px}
 .tblbar{display:flex;justify-content:space-between;align-items:center;
         margin-bottom:8px}
 .tblbar span{font-size:12.5px;color:#59636e}
 .scroll{overflow-x:auto}
__BASE_CSS__
</style></head><body>

<h1>__TITLE__</h1>
<div class="sub">__DESC__</div>

<div class="bar">
  <input id="token" type="password" placeholder="访问令牌（开了鉴权才需要；存本机浏览器）">
  <button class="ghost" onclick="saveTok()">保存令牌</button>
  <button class="ghost" onclick="forgetTok()">清除</button>
  <span style="flex:1"></span>
  <button class="ghost" onclick="load()">刷新</button>
  <button onclick="exportJson()">导出 JSON</button>
</div>
<div class="hint" id="stamp">__TOKEN_HINT__</div>
<div id="content"><p class="hint">载入中…</p></div>

<script>
var NEEDS_TOKEN = __NEEDS_TOKEN__;
var API = '__API_PATH__';
var VNAME = '__VNAME__';
var KEY = 'agentdesk_token';
var RAW = null;

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s)
    .split('&').join('&amp;').split('<').join('&lt;')
    .split('>').join('&gt;').split('"').join('&quot;');
}
function err(msg) { return '<div class="err"><b>' + msg + '</b></div>'; }

function saveTok() {
  localStorage.setItem(KEY, $('token').value.trim());
  load();
}
function forgetTok() {
  localStorage.removeItem(KEY);
  $('token').value = '';
  load();
}

async function load() {
  var token = localStorage.getItem(KEY) || '';
  $('token').value = token;
  $('content').innerHTML = '<p class="hint">载入中…</p>';
  var headers = {};
  if (token) { headers['X-API-Key'] = token; }
  var r;
  try {
    r = await fetch(API, { headers: headers });
  } catch (e) {
    $('content').innerHTML = err('网络错误：' + esc(e.message) +
      '。确认服务已启动（uvicorn app.main:app）。');
    return;
  }
  if (r.status === 401) {
    $('content').innerHTML = err('这个接口需要访问令牌（HTTP 401）。' +
      '在上方填入服务器 .env 里 AGENT_TOKEN 的值，点「保存令牌」。');
    return;
  }
  if (r.status === 429) {
    $('content').innerHTML = err('触发限流（HTTP 429）。等一下再点刷新。');
    return;
  }
  var text = await r.text();
  if (!r.ok) {
    $('content').innerHTML = err('HTTP ' + r.status) +
      '<pre>' + esc(text.slice(0, 600)) + '</pre>';
    return;
  }
  var data;
  try { data = JSON.parse(text); }
  catch (e) {
    $('content').innerHTML = err('返回不是合法 JSON。') +
      '<pre>' + esc(text.slice(0, 600)) + '</pre>';
    return;
  }
  RAW = data;
  $('stamp').textContent = '载入于 ' + new Date().toLocaleTimeString() +
    ' · 数据源 ' + API;
  $('content').innerHTML = renderAny(data, '');
}

/* ---------- 通用渲染：JSON 转表格 / 卡片 ---------- */

function cell(v) {
  if (v === null || v === undefined || v === '') { return ''; }
  if (typeof v === 'object') {
    var s = JSON.stringify(v);
    return '<code>' + esc(s.slice(0, 110)) + (s.length > 110 ? '…' : '') + '</code>';
  }
  return esc(String(v));
}
function isArrOfObj(a) {
  return Array.isArray(a) && a.length > 0 &&
    a.every(function (x) { return x && typeof x === 'object' && !Array.isArray(x); });
}

function renderAny(v, path) {
  if (isArrOfObj(v)) { return tableHTML(v, path); }
  if (Array.isArray(v)) {
    return '<div class="card"><span class="hint">数组 ' + v.length +
      ' 项（内容不是同构对象，逐项展示）</span><pre>' +
      esc(JSON.stringify(v, null, 2)).slice(0, 2500) + '</pre></div>';
  }
  if (v && typeof v === 'object') {
    var scalars = '', h = '';
    Object.keys(v).forEach(function (k) {
      var val = v[k];
      if (val === null || typeof val !== 'object') {
        scalars += '<tr><td style="width:190px;color:#59636e">' + esc(k) +
          '</td><td>' + cell(val) + '</td></tr>';
      }
    });
    if (scalars) {
      h += '<div class="card"><table><tr><th>字段</th><th>值</th></tr>' +
        scalars + '</table></div>';
    }
    Object.keys(v).forEach(function (k) {
      var val = v[k];
      if (val && typeof val === 'object') {
        h += '<h3>' + esc(k) + '</h3>' + renderAny(val, path ? path + '.' + k : k);
      }
    });
    return h || '<p class="hint">（空对象）</p>';
  }
  return '<div class="card">' + cell(v) + '</div>';
}

function tableHTML(arr, path) {
  var cols = [];
  arr.slice(0, 80).forEach(function (o) {
    Object.keys(o).forEach(function (k) {
      if (cols.indexOf(k) < 0) { cols.push(k); }
    });
  });
  var h = '<div class="card"><div class="tblbar"><span>' + arr.length +
    ' 行</span><button class="ghost" onclick="exportCsv(this)" data-path="' +
    esc(path) + '">导出 CSV</button></div><div class="scroll"><table><tr>';
  cols.forEach(function (c) { h += '<th>' + esc(c) + '</th>'; });
  h += '</tr>';
  arr.forEach(function (o) {
    h += '<tr>';
    cols.forEach(function (c) { h += '<td>' + cell(o[c]) + '</td>'; });
    h += '</tr>';
  });
  return h + '</table></div></div>';
}

/* ---------- 导出 ---------- */

function download(name, mime, content) {
  var b = new Blob([content], { type: mime });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(b);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}
function exportJson() {
  if (RAW === null) { return; }
  download(VNAME + '.json', 'application/json', JSON.stringify(RAW, null, 2));
}
function csvOf(arr) {
  var CR = String.fromCharCode(13), LF = String.fromCharCode(10);
  var cols = [];
  arr.slice(0, 80).forEach(function (o) {
    Object.keys(o).forEach(function (k) {
      if (cols.indexOf(k) < 0) { cols.push(k); }
    });
  });
  var lines = [cols.join(',')];
  arr.forEach(function (o) {
    lines.push(cols.map(function (k) {
      var v = o[k];
      if (v === null || v === undefined) { v = ''; }
      if (typeof v === 'object') { v = JSON.stringify(v); }
      return '"' + String(v).split('"').join('""') + '"';
    }).join(','));
  });
  return lines.join(CR + LF);
}
function exportCsv(btn) {
  var path = btn.getAttribute('data-path');
  var arr = path
    ? path.split('.').reduce(function (o, k) { return o ? o[k] : null; }, RAW)
    : RAW;
  if (!arr) { return; }
  // BOM 前缀：让 Excel 识别 UTF-8，中文不乱码
  var bom = String.fromCharCode(0xFEFF);
  var fname = VNAME + (path ? '.' + path.split('.').join('_') : '') + '.csv';
  download(fname, 'text/csv;charset=utf-8', bom + csvOf(arr));
}
</script>
</body></html>"""

@app.get("/view/{name}", response_class=HTMLResponse, include_in_schema=False)
def view_page(name: str, response: Response):
    """JSON 接口的可视化皮。数据仍从原接口出（一个事实源，两种皮）。"""
    if name not in _DATA_VIEWS:
        raise HTTPException(status_code=404, detail="未知的数据视图，可选："
                            + "、".join(sorted(_DATA_VIEWS)))
    api_path, title, desc = _DATA_VIEWS[name]
    needs_token = security.AUTH_ENABLED
    html = _VIEW_HTML
    for k, v in (
        ("__TITLE__", f"AgentDesk · {title}"),
        ("__DESC__", desc),
        ("__API_PATH__", api_path),
        ("__VNAME__", name),
        ("__NEEDS_TOKEN__", "true" if needs_token else "false"),
        ("__TOKEN_HINT__", (
            "这个服务开启了鉴权：先填令牌，点「保存令牌」后数据自动载入。"
            if needs_token else
            "当前未开启鉴权（本地开发模式），令牌可留空。"
            "接口本身是 " + api_path + "，本页只是它的可视化皮。")),
        ("__BASE_CSS__", _BASE_CSS),
    ):
        html = html.replace(k, v)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return HTMLResponse(html)



# ============================================================
# 系统设置：令牌管理 + 工具层数据源切换（页面 + 三个接口）
# ============================================================
# 【为什么要这个页面】
# 改令牌、切数据源原来是「ssh 上服务器改 .env 再重启」的事；
# 演示和联调时这个回路太长。三个接口把两件事做成运行时可操作：
#   · security.AGENT_TOKEN / ops.BACKEND / ops.SSH_TARGETS 都是
#     「调用时读模块级全局」，直接改写立刻生效（有对账测试兜底）；
#   · 同时写回项目根 .env（先备份 .env.bak.settings），重启不丢，
#     容器部署时 compose 的 env_file 读同一份。
# 【安全模型】页面壳公开（和 /try 同理，壳里没有数据）；
# 三个数据接口不豁免 —— 开了鉴权时必须持当前令牌才能读/改，
# 「改钥匙要先出示旧钥匙」。读接口会返回完整令牌：持令牌者
# 本来就能以服务身份行事，不在展示层假装它更保密。

_SETTINGS_ENV_KEYS = ("AGENT_TOKEN", "OPS_BACKEND", "OPS_SSH_TARGETS",
                      "OPS_SSH_KEY", "OPS_HOSTS")


def _mask_token(v: str) -> str:
    if not v:
        return "（未设置）"
    if len(v) <= 10:
        return v[0] + "…" + v[-1]
    return v[:4] + "…" + v[-4:]


def _persist_env(updates: dict) -> str:
    """把键值对写回项目根 .env（存在才写；先备份上一份）。

    返回说明字符串：写到了哪里 / 为什么没写。**只改认识的键**，
    其它行原样保留 —— .env 里还有 DEEPSEEK_API_KEY 这些不能碰的东西。
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return "项目根目录没有 .env，改动只在本次运行内生效（重启后还原）"
    text = env_path.read_text(encoding="utf-8")
    (env_path.parent / ".env.bak.settings").write_text(text, encoding="utf-8")
    lines_out = text.splitlines()
    for k, v in updates.items():
        hit = False
        for i, ln in enumerate(lines_out):
            if ln.startswith(k + "="):
                lines_out[i] = k + "=" + v
                hit = True
                break
        if not hit:
            lines_out.append(k + "=" + v)
    env_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return ("已写回 .env（原文件备份为 .env.bak.settings）—— 重启后仍生效；"
            "容器部署时 compose 的 env_file 读同一份")


def _settings_snapshot() -> dict:
    from app.tools import ops as _ops
    env_path = PROJECT_ROOT / ".env"
    return {
        "auth": {
            "auth_enabled": security.AUTH_ENABLED,
            "token_masked": _mask_token(security.AGENT_TOKEN),
            "token": security.AGENT_TOKEN,
            "token_len": len(security.AGENT_TOKEN),
        },
        "ops": {
            "backend": (_ops.BACKEND or "mock").lower(),
            "targets": {k: v for k, v in _ops.SSH_TARGETS.items()},
            # ★ 序列化成 _parse_ssh_targets 能吃回去的 `名=user@host[:port]`。
            #   第一版直接 f"{k}={v}"，v 是解析后的 dict —— 回填到表单里
            #   就是 `web-01={'user': 'root'...}`，用户一点「应用」就 400。
            #   序列化和解析是同一份数据的两个方向，必须能对账往返。
            "targets_raw": ",".join(
                f"{k}={v['user']}@{v['host']}"
                + (f":{v['port']}" if v.get("port") not in (None, 22) else "")
                for k, v in _ops.SSH_TARGETS.items()),
            "key": _ops.SSH_KEY,
            "hosts_env": (os.getenv("OPS_HOSTS") or "").strip(),
            "known_hosts": list(_ops.KNOWN_HOSTS),
        },
        "env_file": "项目根 .env（可持久化）" if env_path.exists()
                    else "无 .env（改动仅本次运行生效）",
    }


@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(response: Response):
    """设置页壳：数据由 /settings/api 出（开了鉴权时同样要令牌）。"""
    html = _SETTINGS_HTML
    for k, v in (
        ("__NEEDS_TOKEN__", "true" if security.AUTH_ENABLED else "false"),
        ("__TOKEN_MASKED__", _mask_token(security.AGENT_TOKEN)),
        ("__BASE_CSS__", _BASE_CSS),
    ):
        html = html.replace(k, v)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return HTMLResponse(html)


@app.get("/settings/api")
def settings_api():
    """当前令牌（含完整值，见上方安全模型说明）与数据源配置。"""
    return _settings_snapshot()


class SettingsTokenRequest(BaseModel):
    token: str = Field(min_length=16, max_length=200,
                       description="新令牌：>=16 字符、不含空白。建议 32+ 随机字符")


@app.post("/settings/token")
def settings_token(req: SettingsTokenRequest):
    """修改访问令牌：运行时立即生效 + 写回 .env。旧令牌当场作废。"""
    v = req.token.strip()
    if len(v) < 16:
        raise HTTPException(400, detail="令牌太短：至少 16 个字符")
    if any(ch.isspace() for ch in v):
        raise HTTPException(400, detail="令牌不能含空白字符")
    old_masked = _mask_token(security.AGENT_TOKEN)
    security.AGENT_TOKEN = v
    os.environ["AGENT_TOKEN"] = v
    persisted = _persist_env({"AGENT_TOKEN": v})
    return {"ok": True, "old": old_masked, "new_masked": _mask_token(v),
            "persist": persisted}


class SettingsOpsRequest(BaseModel):
    backend: str = Field(description="mock | local | ssh")
    targets: str = Field(default="", description="web-01=root@1.2.3.4:22,db-01=ops@10.0.0.9")
    key: str = Field(default="", description="SSH 私钥路径（ssh 后端用）")
    hosts: str = Field(default="", description="可选：显式主机清单，逗号分隔；留空=自动推导")


@app.post("/settings/ops")
def settings_ops(req: SettingsOpsRequest):
    """切换工具层数据源。ssh 后端要求至少配一个目标，否则 400（fail-closed）。"""
    from app.tools import ops as _ops
    backend = (req.backend or "").strip().lower()
    if backend not in ("mock", "local", "ssh"):
        raise HTTPException(400, detail="backend 只能是 mock / local / ssh")
    targets_raw = (req.targets or "").strip()
    if backend == "ssh" and not targets_raw:
        raise HTTPException(400, detail="ssh 后端至少要配一个目标，"
                            "例如 web-01=root@1.2.3.4 —— 没有目标的 ssh 只会"
                            "在每次查询时报错，不如显式拒绝")
    try:
        targets = _ops._parse_ssh_targets(targets_raw) if targets_raw else {}
    except ValueError as e:
        raise HTTPException(400, detail=f"目标格式不对：{e}")

    _ops.BACKEND = backend
    _ops.SSH_TARGETS = targets
    _ops.SSH_KEY = (req.key or "").strip()
    hosts_env = (req.hosts or "").strip()
    if hosts_env:
        os.environ["OPS_HOSTS"] = hosts_env
    else:
        os.environ.pop("OPS_HOSTS", None)
    # KNOWN_HOSTS 是导入时算好的，换后端必须重推 ——
    # 漏了这步就会出现「配置已是 ssh、提示词还列着旧的机器清单」的矛盾
    _ops.KNOWN_HOSTS = _ops._resolve_hosts()

    os.environ["OPS_BACKEND"] = backend
    os.environ["OPS_SSH_TARGETS"] = targets_raw
    os.environ["OPS_SSH_KEY"] = _ops.SSH_KEY
    persisted = _persist_env({
        "OPS_BACKEND": backend,
        "OPS_SSH_TARGETS": targets_raw,
        "OPS_SSH_KEY": _ops.SSH_KEY,
        **({"OPS_HOSTS": hosts_env} if hosts_env else {"OPS_HOSTS": ""}),
    })
    resp = {"ok": True, "backend": backend,
            "known_hosts": list(_ops.KNOWN_HOSTS), "persist": persisted}
    # ★ 实测踩过的坑（2026-09-30）：key 留空保存后，ssh 每次查询都
    #   Permission denied，而 responses 里只有 ok=true —— 用户要到
    #   前端查不出数据才回头找原因。不阻止（有人的 ssh 靠默认路径/
    #   agent 转发就能通），但必须把后果说明白。
    if backend == "ssh" and not _ops.SSH_KEY:
        resp["warning"] = ("未配置私钥：ssh 将退回 ssh 客户端的默认密钥搜索路径，"
                           "大概率每次查询都 Permission denied。"
                           "如果连不上，回到本页把私钥路径填上。")
    return resp


_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentDesk · 系统设置</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      max-width:900px;margin:0 auto;padding:32px 20px 80px;color:#1f2328;
      line-height:1.65;background:#fff}
 h1{margin:0 0 4px;font-size:22px}
 h2{font-size:15px;margin:28px 0 10px;padding-bottom:6px;
    border-bottom:1px solid #e6e8eb;color:#24292f}
 .sub{color:#59636e;margin-bottom:20px;font-size:14px}
 label{display:block;font-size:13px;color:#59636e;margin:12px 0 5px}
 input,textarea{width:100%;padding:9px 11px;border:1px solid #d0d7de;
      border-radius:6px;font-size:14px;font-family:inherit;background:#fff;color:#1f2328}
 textarea{min-height:64px;resize:vertical;font-family:ui-monospace,Consolas,monospace;
      font-size:13px}
 input:focus,textarea:focus{outline:2px solid #0969da;outline-offset:-1px;
      border-color:#0969da}
 button{padding:9px 18px;background:#1f883d;color:#fff;border:0;
        border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
 button:hover{background:#1a7f37}
 button.ghost{background:#f6f8fa;color:#24292f;border:1px solid #d0d7de;
              font-weight:400;padding:6px 12px;font-size:13px}
 button.ghost:hover{background:#eef1f4}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .row input{flex:1 1 260px;width:auto}
 .card{border:1px solid #e6e8eb;border-radius:10px;padding:16px 18px;margin:12px 0}
 .hint{font-size:12.5px;color:#59636e;margin:6px 0 0}
 .warn{background:#fff8e6;border-left:3px solid #d4a017;padding:10px 14px;
       border-radius:0 6px 6px 0;font-size:13.5px;margin:10px 0}
 .ok-msg{background:#f0fff4;border-left:3px solid #1f883d;padding:10px 14px;
       border-radius:0 6px 6px 0;font-size:13.5px;margin:10px 0}
 .err{background:#ffebe9;border-left:3px solid #cf222e;padding:10px 14px;
      border-radius:0 6px 6px 0;font-size:13.5px;margin:10px 0}
 code{background:#f4f5f7;padding:1px 5px;border-radius:4px;
      font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
 table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
 td,th{padding:7px 9px;border-bottom:1px solid #f0f1f3;text-align:left}
 th{color:#59636e;font-weight:600}
 .radio-row{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0;font-size:14px}
 .radio-row label{display:flex;align-items:center;gap:6px;margin:0;color:#1f2328}
__BASE_CSS__
</style></head><body>

<h1>系统设置</h1>
<div class="sub">访问令牌与工具层数据源 —— 运行时生效，同时写回 .env（自动备份）</div>

<div class="warn"><b>这一页能改钥匙。</b>开了鉴权的实例上，读/改这里的配置
需要出示<b>当前有效</b>令牌；改完令牌后旧令牌立即作废，本页会自动把新令牌
存进本机浏览器。令牌与配置明文只出现在服务端与你的浏览器之间，不进日志。</div>

<div id="msg"></div>

<h2>访问令牌</h2>
<div class="card">
  <div class="row">
    <input id="cur" type="password" value="" readonly
           style="flex:1 1 320px;font-family:ui-monospace,Consolas,monospace">
    <button class="ghost" id="reveal" type="button">显示</button>
    <button class="ghost" id="gen" type="button">生成随机令牌</button>
  </div>
  <label for="newtok">新令牌（≥16 字符，建议 32+；留空 = 不修改）</label>
  <div class="row">
    <input id="newtok" type="text" placeholder="粘贴或用上面按钮生成"
           style="font-family:ui-monospace,Consolas,monospace">
    <button id="savetok" type="button">确认修改</button>
  </div>
  <p class="hint" id="authline"></p>
</div>

<h2>工具层数据源</h2>
<div class="card">
  <div class="radio-row">
    <label><input type="radio" name="backend" value="mock"> mock · 内置仿真数据（默认，最安全）</label>
    <label><input type="radio" name="backend" value="local"> local · 查本机真实状态</label>
    <label><input type="radio" name="backend" value="ssh"> ssh · 查远程主机</label>
  </div>
  <label for="targets">SSH 目标（逻辑名=user@地址[:端口]，逗号或换行分隔；ssh 后端必填）</label>
  <textarea id="targets" placeholder="web-01=root@192.168.1.10,db-01=ops@192.168.1.11:2222"></textarea>
  <label for="key">SSH 私钥路径（可选；例如 C:/Users/you/.ssh/ops_key 或 /opt/agentdesk/.ssh/ops_key）</label>
  <input id="key" type="text" style="font-family:ui-monospace,Consolas,monospace">
  <label for="hosts">主机清单（可选，逗号分隔；留空 = 按目标自动推导。这是「Agent 知道队里有哪几台」，和「能连到哪几台」不是一回事）</label>
  <input id="hosts" type="text" placeholder="web-01,db-01">
  <div class="row" style="margin-top:14px">
    <button id="applyops" type="button">应用数据源</button>
    <span class="hint" id="opshint"></span>
  </div>
  <div id="opsview"></div>
</div>

<p class="hint" id="persist"></p>

<script>
var NEEDS_TOKEN = __NEEDS_TOKEN__;
var KEY = 'agentdesk_token';
var RAW = null;

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s)
    .split('&').join('&amp;').split('<').join('&lt;')
    .split('>').join('&gt;').split('"').join('&quot;');
}
function msg(kind, text) {
  $('msg').innerHTML = '<div class="' + kind + '">' + text + '</div>';
}
function headers() {
  var h = { 'Content-Type': 'application/json' };
  var t = localStorage.getItem(KEY) || '';
  if (t) { h['X-API-Key'] = t; }
  return h;
}
function authed(r, data) {
  if (r.status !== 401) { return true; }
  msg('err', '<b>需要当前有效令牌（HTTP 401）。</b>' +
    '先在右上角令牌框确认本机浏览器存的令牌还有效。');
  return false;
}

async function load() {
  var r = await fetch('/settings/api', { headers: headers() });
  var data = await r.json().catch(function () { return null; });
  if (!authed(r, data)) { return; }
  if (!r.ok) { msg('err', 'HTTP ' + r.status); return; }
  RAW = data;
  $('cur').value = data.auth.token || '';
  $('authline').textContent = '当前令牌 ' + data.auth.token_len +
    ' 字符 · 鉴权' + (data.auth.auth_enabled ? '已开启（接口都要令牌）'
                                             : '未开启（本地开发模式）');
  document.querySelectorAll('input[name=backend]').forEach(function (el) {
    el.checked = (el.value === data.ops.backend);
  });
  $('targets').value = data.ops.targets_raw;
  $('key').value = data.ops.key;
  $('hosts').value = data.ops.hosts_env;
  $('persist').textContent = '持久化位置：' + data.env_file +
    ' · 每次修改前自动把上一份备份为 .env.bak.settings';
  renderOps(data.ops);
}

function renderOps(ops) {
  var h = '<table><tr><th>当前生效</th><th>值</th></tr>' +
    '<tr><td>后端</td><td><code>' + esc(ops.backend) + '</code></td></tr>' +
    '<tr><td>能说出名字的主机</td><td><code>' + esc(ops.known_hosts.join(', ')) +
    '</code></td></tr>' +
    '<tr><td>SSH 目标</td><td>' +
    (Object.keys(ops.targets).length
      ? Object.keys(ops.targets).map(function (k) {
          return '<code>' + esc(k + ' → ' + ops.targets[k]) + '</code>';
        }).join(' ')
      : '<span class="hint">（未配置）</span>') + '</td></tr>' +
    '<tr><td>私钥路径</td><td>' + (ops.key ? '<code>' + esc(ops.key) + '</code>'
      : '<span class="hint">（未配置）</span>') + '</td></tr></table>';
  $('opsview').innerHTML = h;
}

$('reveal').onclick = function () {
  $('cur').type = ($('cur').type === 'password') ? 'text' : 'password';
  $('reveal').textContent = ($('cur').type === 'password') ? '显示' : '隐藏';
};
$('gen').onclick = function () {
  var b = new Uint8Array(32);
  crypto.getRandomValues(b);
  var s = btoa(String.fromCharCode.apply(null, b));
  $('newtok').value = s.split('+').join('-').split('/').join('_')
                       .split('=').join('');
};

$('savetok').onclick = async function () {
  var v = $('newtok').value.trim();
  if (!v) { msg('err', '新令牌为空 —— 留空就是不修改。'); return; }
  var r = await fetch('/settings/token', {
    method: 'POST', headers: headers(), body: JSON.stringify({ token: v })
  });
  var data = await r.json().catch(function () { return null; });
  if (!authed(r, data)) { return; }
  if (!r.ok) { msg('err', '<b>修改失败：</b>' + esc(data && data.detail || ('HTTP ' + r.status))); return; }
  localStorage.setItem(KEY, v);          // 立刻换上新手，免得把自己锁在门外
  $('newtok').value = '';
  msg('ok', '<b>令牌已修改并即时生效。</b>' + esc(data.persist) +
    '。旧令牌 <code>' + esc(data.old) + '</code> 已作废；' +
    '其它页面 / 调用方要换用新令牌。');
  load();
};

$('applyops').onclick = async function () {
  var backend = document.querySelector('input[name=backend]:checked').value;
  var body = {
    backend: backend,
    targets: $('targets').value.trim(),
    key: $('key').value.trim(),
    hosts: $('hosts').value.trim()
  };
  var r = await fetch('/settings/ops', {
    method: 'POST', headers: headers(), body: JSON.stringify(body)
  });
  var data = await r.json().catch(function () { return null; });
  if (!authed(r, data)) { return; }
  if (!r.ok) { msg('err', '<b>应用失败：</b>' + esc(data && data.detail || ('HTTP ' + r.status))); return; }
  msg('ok', '<b>数据源已切换为 <code>' + esc(data.backend) +
    '</code> 并即时生效。</b>' + esc(data.persist) +
    '。到 <a href="/try">/try</a> 问一句机器状态即可验证。' +
    (data.warning ? '<div class="warn" style="margin-top:8px"><b>' +
      esc(data.warning) + '</b></div>' : ''));
  load();
};

load();
</script>
</body></html>"""



@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(response: Response):
    """把 /metrics/summary、/traces、/approvals 渲染成一个可读的页面。

    【为什么要有这一页】
    数据一直都是完整的（本地 JSONL 里躺着上百条 trace），但对第一次看的人，
    一坨 JSON 等于没有 —— 尤其「成本按 Agent 分布」和「对账偏差」这两个最能
    说明工程质量的东西，埋在 JSON 里谁也看不见。

    【这一页刻意不做什么】
    · 不引图表库、不做构建、不依赖 CDN —— 一个文件，部署即用
    · 不做写入 —— 它只是**读**三个已有接口，不新增任何业务状态
    · **不缓存**（见末尾 no-store）：看板显示的是"现在"，
      缓存过的看板给出的是过去的数字，而读者会以为那是当前的

    【"要不要令牌"由服务端决定】
    与 /try 同一条规矩：服务端本来就知道 AUTH_ENABLED，直接写进页面，
    前端只读取、不去探测（探测方案有三个失败面，/try 里已写过一遍）。
    """
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentDesk · 可观测看板</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     max-width:1040px;margin:0 auto;padding:28px 20px 70px;color:#1f2328;
     line-height:1.6;background:#fff}
h1{font-size:22px;margin:0 0 3px}
.sub{color:#59636e;font-size:13px;margin-bottom:20px}
h2{font-size:15px;margin:26px 0 10px;padding-bottom:6px;
    border-bottom:1px solid #e6e8eb}
h3{font-size:13px;margin:16px 0 0;color:#24292f}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
input[type=text]{flex:1;min-width:230px;padding:8px 10px;border:1px solid #d0d7de;
     border-radius:6px;font-size:13px}
button{padding:8px 14px;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa;
     font-size:13px;cursor:pointer;color:#1f2328}
button:hover{background:#eef1f4}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 4px}
.card{flex:1;min-width:128px;border:1px solid #e6e8eb;border-radius:8px;padding:11px 13px}
.card .k{font-size:12px;color:#59636e}
.card .v{font-size:19px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #eef1f4}
th{color:#59636e;font-weight:500;background:#f6f8fa}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:#1a7f37}.warn{color:#9a6700}.err{color:#cf222e}.mut{color:#59636e}
.err-box{border:1px solid #ffcecb;background:#fff5f5;border-radius:8px;
     padding:10px 12px;font-size:13px;color:#82071e;margin:8px 0}
.note{font-size:12px;color:#59636e;margin-top:6px}
.trace{border:1px solid #e6e8eb;border-radius:8px;padding:9px 11px;margin-bottom:7px;
     cursor:pointer}
.trace:hover{background:#f6f8fa}
.spans{margin-top:8px;font-size:12px;color:#424a53;border-top:1px dashed #e6e8eb;
     padding-top:7px}
.empty{color:#59636e;font-size:13px;padding:10px 0}
code{background:#f6f8fa;padding:1px 5px;border-radius:4px;font-size:12px}
__BASE_CSS__
</style></head><body>

<h1>AgentDesk · 可观测看板</h1>
<div class="sub">数据来自 <code>/metrics/summary</code> · <code>/traces</code> ·
  <code>/approvals</code> —— 这一页<b>只读</b>，不做任何写入。</div>

<div class="bar" id="tokbar" style="display:none">
  <input type="text" id="token" placeholder="访问令牌（本实例开启了鉴权）">
  <button onclick="saveToken()">保存令牌</button>
</div>
<div class="bar">
  <button onclick="loadAll()">刷新</button>
  <label style="font-size:13px;color:#59636e;display:flex;align-items:center;gap:5px">
    <input type="checkbox" id="auto" onchange="toggleAuto()"> 每 30 秒自动刷新</label>
  <span class="note" id="stamp"></span>
</div>

<h2>概览</h2>
<div id="summary"></div>

<h2>成本归因</h2>
<div class="note">两个维度是<b>同一笔钱的两种切法</b>（按 Agent / 按动作），
  各自合计应等于总账 —— <b>不能相加</b>（相加等于翻倍）。</div>
<div id="breakdown"></div>

<h2>对账</h2>
<div id="reconcile"></div>

<h2>最近运行（点开看逐步轨迹）</h2>
<div id="traces"></div>

<h2>待人工确认的写操作</h2>
<div id="approvals"></div>

<script>
// ★ 全局错误必须显示出来，不能让页面"静默空白"。
//   第一版就栽在这里：一处 JS 语法错误让整个脚本没执行，
//   页面上五块数据区全空、连一条报错都没有 —— 看起来像"没数据"，
//   实际是"代码根本没跑"。**空白和出错必须是两种可见的状态。**
window.onerror = function (msg, src, line) {
  var d = document.createElement('div');
  d.className = 'err-box';
  d.textContent = '页面脚本出错：' + msg + '（第 ' + line + ' 行）—— 数据区因此是空的';
  document.body.insertBefore(d, document.body.firstChild);
  return false;
};

__BASE_JS__
var NEEDS_TOKEN = __NEEDS_TOKEN__;
var TOKEN = localStorage.getItem('agentdesk_token') || '';
var timer = null;

if (NEEDS_TOKEN) {
  document.getElementById('tokbar').style.display = 'flex';
  document.getElementById('token').value = TOKEN;
}
function saveToken(){
  TOKEN = document.getElementById('token').value.trim();
  localStorage.setItem('agentdesk_token', TOKEN);
  loadAll();
}
function headers(){
  var h = {'Accept':'application/json'};
  if (NEEDS_TOKEN && TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;
  return h;
}
async function getJson(path){
  var r = await fetch(path, {headers: headers(), cache: 'no-store'});
  if (r.status === 401) throw new Error('需要令牌（HTTP 401）—— 请在上方填入');
  if (!r.ok) throw new Error(path + ' → HTTP ' + r.status);
  return r.json();
}
 function esc(s){
   return String(s == null ? '' : s)
     .replace(/&/g, '&amp;').replace(/</g, '&lt;')
     .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
 }
function money(v){ return '¥' + Number(v || 0).toFixed(4); }
function card(k, v, cls){
  return '<div class="card"><div class="k">' + k +
         '</div><div class="v ' + (cls || '') + '">' + v + '</div></div>';
}
function fail(e, w){
  document.getElementById(w).innerHTML =
    '<div class="err-box">' + w + ' 取数失败：' + e.message + '</div>';
}
function tbl(m, title, kh){
  var rows = '';
  for (var k in m) {
    rows += '<tr><td>' + k + '</td><td class="n">' + m[k].calls +
            '</td><td class="n">' + m[k].tokens +
            '</td><td class="n">' + money(m[k].cost_cny) +
            '</td><td class="n">' + m[k].elapsed_ms + '</td></tr>';
  }
  return '<h3>' + title + '</h3><table><tr><th>' + kh +
         '</th><th style="text-align:right">次数</th>' +
         '<th style="text-align:right">token</th>' +
         '<th style="text-align:right">成本</th>' +
         '<th style="text-align:right">耗时ms</th></tr>' + rows + '</table>';
}

async function loadSummary(){
  try {
    var d = await getJson('/metrics/summary?limit=50');
    var e = d.elapsed_ms || {}, tk = (d.tokens || {}).total_tokens || 0;
    var sc = d.scope || {};
    document.getElementById('summary').innerHTML = '<div class="cards">' +
      card('运行次数', d.runs || 0) +
      card('不同问题', d.distinct_questions || 0) +
      card('错误', d.errors || 0, d.errors ? 'err' : 'ok') +
      card('总成本', money(d.cost_cny)) + card('token', tk) +
      card('缓存命中率', ((d.prompt_cache_hit_rate || 0) * 100).toFixed(1) + '%') +
      card('P50 / P95', (e.p50 || 0) + ' / ' + (e.p95 || 0) + ' ms') + '</div>' +
      '<div class="note">延迟只统计单次请求（批处理不计入）· 样本 ' +
      (e.sample || 0) + ' 次</div>' +
      (sc.caveat
        ? '<div class="note" style="background:#fff8e6;border-left:3px solid #d4a017">' +
          '<b>「运行次数」不等于「用户数」。</b> ' + esc(sc.caveat) + '</div>'
        : '') +
      (sc.records_scanned
        ? '<div class="note">记录口径：扫描 ' + sc.records_scanned + ' 条 → 真实运行 ' +
          sc.live_available + ' 条 · 已排除自检 ' + sc.selftest_excluded + ' 条</div>'
        : '');
    document.getElementById('breakdown').innerHTML =
      tbl(d.by_span_name || {}, '按 Agent（钱花在谁身上）', '节点') +
      tbl(d.by_span_type || {}, '按动作（钱花在什么事上）', '类型');
    document.getElementById('breakdown').innerHTML +=
      '<h2>成本分布</h2><div class="card">' + hbars(d.by_span_name || {}) +
      '</div>';

    var rc = d.reconcile || {};
    function gc(g){ return Math.abs(g) <= 0.01 ? 'ok' : 'warn'; }
    var h = '<table><tr><th>口径</th><th style="text-align:right">金额</th>' +
      '<th style="text-align:right">与总账偏差</th></tr>' +
      '<tr><td>总账</td><td class="n">' + money(rc.trace_total_cny) +
      '</td><td class="n mut">基准</td></tr>' +
      '<tr><td>按动作合计</td><td class="n">' + money(rc.by_type_total_cny) +
      '</td><td class="n ' + gc(rc.by_type_gap || 0) + '">' +
      ((rc.by_type_gap || 0) * 100).toFixed(2) + '%</td></tr>' +
      '<tr><td>按 Agent 合计</td><td class="n">' + money(rc.by_name_total_cny) +
      '</td><td class="n ' + gc(rc.by_name_gap || 0) + '">' +
      ((rc.by_name_gap || 0) * 100).toFixed(2) + '%</td></tr></table>';
    if (rc.warning) h += '<div class="err-box">' + rc.warning + '</div>';
    if (d.unpriced_models && d.unpriced_models.length) {
      h += '<div class="err-box">未定价模型：' + d.unpriced_models.join('、') +
           ' —— 这些数字是按兜底价估的</div>';
    }
    h += '<div class="note">' + (rc.note || '') + ' · 无 span 的 trace：' +
         (rc.traces_without_spans || 0) + ' 条（残差的已知来源）</div>';
    document.getElementById('reconcile').innerHTML = h;
  } catch (err) {
    fail(err, 'summary'); fail(err, 'breakdown'); fail(err, 'reconcile');
  }
}

async function loadTraces(){
  try {
    var items = (await getJson('/traces?limit=8')).items || [];
    if (!items.length) {
      document.getElementById('traces').innerHTML =
        '<div class="empty">还没有运行记录。先跑一次 /try 或 /agent/ask。</div>';
      return;
    }
    // ★ 用 DOM 构造 + 事件绑定，不用内联 onclick。
    //   第一版是用字符串拼一个内联 onclick 处理器，里面要嵌单引号 ——
    //   而这段 JS 是放在 Python 的普通三引号字符串里的，
    //   反斜杠转义被 Python 先吃掉一层，发出去的属性变成空引号拼接，
    //   整个 script 直接语法错误、一行都没执行，页面全空。
    //   两个教训：
    //     ① 靠转义引号来生成属性，等于把"能不能跑"押在两层转义的配合上 ——
    //        改用 DOM API + addEventListener，这个问题根本不存在
    //     ② 在 Python 里写 JS，尽量不产生转义序列；
    //        本地读代码是对的、发出去的字节是错的，这种错最难查
    var box = document.getElementById('traces');
    box.innerHTML = '';
    items.forEach(function(t){
      var el = document.createElement('div');
      el.className = 'trace';
      el.dataset.id = t.trace_id;
      el.innerHTML =
        '<div><b>' + t.name + '</b> <span class="mut">' + t.trace_id + '</span> ' +
        '<span class="' + (t.status === 'ok' ? 'ok' : 'err') + '">' + t.status +
        '</span></div><div class="mut" style="font-size:12px">' +
        (t.question || '') + ' · ' + (t.elapsed_ms || 0) + 'ms · ' +
        money(t.cost_cny) + ' · ' + (t.span_count || 0) + ' span</div>' +
        '<div class="spans" style="display:none"></div>';
      el.addEventListener('click', function () { expand(el, el.dataset.id); });
      box.appendChild(el);
    });
  } catch (err) { fail(err, 'traces'); }
}

async function expand(el, id){
  var box = el.querySelector('.spans');
  if (box.style.display !== 'none') { box.style.display = 'none'; return; }
  box.innerHTML = '加载中…'; box.style.display = 'block';
  try {
      var items = [];
      ((await getJson('/traces/' + id)).spans || []).forEach(function (s) {
        items.push({ name: s.name, elapsed_ms: s.elapsed_ms || 0,
                     ok: s.status === 'ok' });
      });
      box.innerHTML = timelineHTML(items) || '（这条 trace 没有 span）';
  } catch (err) { box.innerHTML = '加载失败：' + err.message; }
}

async function loadApprovals(){
  try {
    var items = (await getJson('/approvals?status=pending&limit=10')).items || [];
    if (!items.length) {
      document.getElementById('approvals').innerHTML =
        '<div class="empty">没有待确认的写操作。</div>';
      return;
    }
    var h = '<table><tr><th>编号</th><th>命令</th><th>风险</th><th>到期</th></tr>';
    items.forEach(function(a){
      h += '<tr><td>' + a.id + '</td><td><code>' + a.command + '</code></td><td>' +
        (a.risk || '') + '</td><td class="mut">' + (a.expires_at || '') + '</td></tr>';
    });
    document.getElementById('approvals').innerHTML = h + '</table>';
  } catch (err) { fail(err, 'approvals'); }
}

function toggleAuto(){
  if (document.getElementById('auto').checked) timer = setInterval(loadAll, 30000);
  else { clearInterval(timer); timer = null; }
}
async function loadAll(){
  document.getElementById('stamp').textContent = '载入中…';
  await Promise.all([loadSummary(), loadTraces(), loadApprovals()]);
  document.getElementById('stamp').textContent =
    '更新于 ' + new Date().toLocaleTimeString();
}
loadAll();
</script>
</body></html>"""

    # ★ "要不要令牌"由服务端决定，前端只读取 —— 与 /try 同一条规矩。
    needs_token = "true" if security.AUTH_ENABLED else "false"
    html = html.replace("__BASE_JS__", _BASE_JS)
    html = html.replace("__BASE_CSS__", _BASE_CSS)
    html = html.replace("__NEEDS_TOKEN__", needs_token)

    # 看板显示的是"现在"，没有任何缓存的理由
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return html


# ============================================================
# 接口 1：健康检查
# ============================================================
@app.get("/health")
async def health():
    """部署和监控系统靠这个接口判断服务是否活着。

    别小看它 —— 后面要用 Docker + Nginx 上线，
    容器的健康检查、负载均衡的存活探测全都依赖这个接口。

    这里额外返回安全层状态（鉴权开没开、额度用了多少、拦了多少请求），
    现场排查时一眼就能看出「是服务挂了还是 token 不对」。
    """
    return {
        "status": "ok",
        "service": "agentdesk",
        "version": "1.0.0",
        "security": security.security_status(),
    }


# ============================================================
# 接口 1·B：深度健康检查 与 Prometheus 指标（M4）
# ============================================================
def _refresh_gauges() -> None:
    """把"只有抓取时才算得出来"的东西刷进指标。

    ★ 为什么在抓取时现算，而不是在每个写入点维护：
      gauge 的语义是"当前值"。写在写入点就要求**每一条状态迁移都记得更新它** ——
      漏一处就永远差一个数，而且很难发现（数字看起来是合理的）。
      抓取时现算只有一个事实源。代价是每次抓取读一次小文件（事件表几 KB），
      Prometheus 15–60 秒抓一次，可以接受。
    """
    obs_metrics.gauge("agentdesk_process_uptime_seconds", time.time() - _STARTED_AT)
    # ★ 如实标出"这些状态是进程内的"（审计里的阻断级风险，M4 的处理是**可见 + 可拦**）
    for kind in ("rate_limit", "daily_quota", "alert_dedup"):
        obs_metrics.gauge("agentdesk_state_scope", 1,
                          {"kind": kind, "scope": "process"})
    try:
        counts = _incident_store().counts()
        for status in ("open", "ack", "resolved", "reopened"):
            obs_metrics.gauge("agentdesk_incidents", counts.get(status, 0),
                              {"status": status})
    except Exception:                             # pragma: no cover
        pass                                      # 指标抓取不能因为存储坏了而 500
    obs_metrics.gauge("agentdesk_trace_write_failures", obs.write_failures())
    obs_metrics.gauge("agentdesk_export_failures", obs.export_failures())
    obs_metrics.gauge("agentdesk_alert_dedup_entries", _ALERT_AGG.size())


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus 文本格式指标。

    【为什么手写而不引入 prometheus_client】
      这个项目对外宣称"直接依赖仅 9 个"，CI 里还有一条守卫盯着这个数字。
      我们真正需要的只有 counter / gauge / histogram 三种最基础的类型，
      手写约 30 行；风险（格式不合规）由测试里的规范校验兜住 ——
      **把风险显式管住，而不是用一个依赖换掉它**。

    【为什么不在免鉴权清单里】
      指标里有成本、错误率、主机名与存储路径。公网裸奔等于把运维内部状况送出去。
      抓取端加一个 `X-API-Key` 头即可（Prometheus 的 `authorization` 配置支持）。
      这与"`/health` 公开、`/healthz` 要令牌"是同一条判断：
      **能秒回、零依赖、不泄露内部信息的那个公开；带明细的要令牌。**
    """
    _refresh_gauges()
    return Response(content=obs_metrics.render(),
                    media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/healthz")
def healthz():
    """**深度**健康检查：真的去碰一下依赖，坏了会说清是哪一项。

    `/health` 保持原样（公开、秒回、零依赖）—— 容器编排与负载均衡用它；
    `/healthz` 是给人排障用的：逐项明细（含路径与计数），所以要令牌。
    返回 `200`（能干活）或 `503`（有依赖不可用，`failing` 里点名）。
    """
    result = selfcheck.probe()
    return JSONResponse(content=result,
                        status_code=200 if result["ok"] else 503)


# ============================================================
# 接口 2：审计日志查询
# ============================================================
@app.get("/audit")
def read_audit(limit: int = Query(20, ge=1, le=200, description="返回最近多少条"),
               offset: int = Query(0, ge=0, le=5000,
                                   description="从最新往前的游标：0 = 最新")):
    """读取最近的审计记录。

    【这个接口的价值】
    它是「可追溯」的证据。可以这样描述：
    「每一次告警触发的判断和决策都落库了，我能查出来三小时前那次为什么没自动处理。」
    这句话是通用 AI 助手给不了的 —— 它有聊天记录，但没有结构化审计。

    【M4 的两处改动】

      ① **不再全量读文件**。原先是 `AUDIT_LOG.read_text()` —— 文件多大就读多大；
         审计只增不减，接口会随着时间越来越慢，而且是那种"没人注意到的变慢"。
         现在从尾部按块读，并且**跨轮转文件回读**。

      ② **`total` 的语义要说清**：它是"这次窗口里读到了多少条"，
         不是"全历史总数"。轮转之后全历史散在多个文件里，硬报一个总数
         只会得到一个**看起来对、其实不对**的数字 —— 那比不给更糟。
         所以这里把窗口元信息一并返回（读了哪些文件、有没有坏行、有没有被截断）。
    """
    # 上限保护：offset 不能被用来"变相读全文件"
    want = min(limit + offset, 10_000)
    records, meta = obs_jsonl.read_tail(AUDIT_LOG, want)

    end = len(records) - offset
    start = max(0, end - limit)
    items = records[start:end] if end > 0 else []

    return {
        "total": len(records),          # 窗口内条数（见上面 ② 的说明）
        "items": items,                 # 旧 → 新，与改动前一致
        "offset": offset,
        "window": meta,                 # files / bad_lines / truncated
        "note": ("total 是**窗口内**条数，不是全历史；"
                 "window 里如实标出读了哪些文件、有没有坏行或被截断"),
    }


# ============================================================
# 接口 3：问答（非流式）
# ============================================================
@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    """一次性返回完整回答。实现最简单，但用户要等。"""
    # ★ 一次 HTTP 请求 = 一个 trace。
    #   /chat 也会被记录 —— 因为"成本看板"需要覆盖所有调用方，
    #   只看 Agent 的成本会低估真实开销。
    with obs.trace("chat", question=req.question):
        try:
            answer = llm_chat([
                {"role": "system", "content": "你是一个简洁的运维助手，回答不超过 100 字。"},
                {"role": "user", "content": req.question},
            ])
        except ModelError as e:
            # 模型层出问题 → 502 Bad Gateway（上游服务故障）
            # 用户参数不对 → 422（FastAPI 自动处理）
            # 调用方看状态码就知道该找谁。
            raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(answer=answer)


# ============================================================
# 接口 4：问答（流式 SSE，异步版）
# ============================================================
@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """流式返回，字一个个蹦出来。

    【为什么这里是 async def】
    内部用的是异步生成器（chat_stream_async）。
    如果写成 def，同步阻塞会占满线程池，并发上不去。
    现在这个写法在等模型吐字的间隙会把控制权交还给事件循环，
    可以同时服务远超线程池上限的并发请求 —— 这就是「高并发」的实际含义。

    【SSE 是什么】
    Server-Sent Events：服务端持续往同一个 HTTP 连接里推数据。
    格式很土，每块数据就一行，必须形如：
        data: 你要发送的内容
        （空行表示这一块结束）
    浏览器端的 EventSource 就是按这个格式解析的。
    """
    messages = [
        {"role": "system", "content": "你是一个简洁的运维助手，回答不超过 100 字。"},
        {"role": "user", "content": req.question},
    ]

    async def gen():
        try:
            async for piece in chat_stream_async(messages):
                # ensure_ascii=False 让中文原样输出，
                # 否则会变成 \u4f60\u597d 这种（能用但没法读）
                payload = json.dumps({"delta": piece}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except ModelError as e:
            # 流已经开始了就没法改状态码了，只能把错误当成一条数据推给客户端。
            # 这是流式接口的固有难点。
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"        # 约定俗成的结束标记

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        # X-Accel-Buffering: no 是给 Nginx 看的 ——
        # 不关掉缓冲，Nginx 会把内容攒够一批才转发，流式就退化成非流式了。
        # 这个坑上线必踩，提前埋好。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 接口 5：意图解析
# ============================================================
@app.post("/parse")
def parse_endpoint(req: ChatRequest):
    """把一句人话解析成程序能读的结构化 JSON。

    这个接口是后续「意图路由 Agent」的内核。
    """
    try:
        intent, attempts = parse_intent(req.question)
    except IntentParseFailed as e:
        # 422 Unprocessable Entity：请求本身没问题，是模型给的答案不合格。
        # 把问题列表一起带上，方便定位是 Prompt 的问题还是模型的问题。
        raise HTTPException(status_code=422, detail={
            "message": str(e),
            "problems": e.problems,
            "last_output": e.data,
        })

    return {**intent, "_attempts": attempts}


# ============================================================
# 接口 6：告警驱动的入口 —— 无人值守
# ============================================================
@app.post("/webhook/alert")
def webhook_alert(payload: dict, background: BackgroundTasks = None):
    # ★ 无人值守链路也必须有 trace —— 这恰恰是"凌晨三点谁在干活"
    #   唯一的答案来源。里面每个 Agent 节点的 span 由 supervisor 自动挂。
    """接收告警系统推送的事件，自动完成判断并给出处置方案。

    【这个接口存在的意义】
    通用 AI 助手必须有人在旁边打字才能干活。
    而这个接口是给 Alertmanager 这类告警系统调的 ——
    凌晨三点你在睡觉，告警响了它自己起来判断、自己给出结论、自己留痕。

    「无人值守」是 Agent 服务和通用助手最本质的差别。

    【执行边界（很重要，别被字段名骗了）】
    1. 风险判定：唯一的判定源在 `policy.ACTION_RISK`；
       模型自报的 risk **只能抬高、不能降低**（policy.escalate）。
       凡是"要动手改系统"的告警一律转人工，不管模型说它有多安全。
    2. 写操作永不自动执行 —— 这是本项目的核心边界，宁可少做，不可做错。
    3. 默认只产出**处置预案**（一份写死的命令清单），**不是诊断结论**。
       要让它真的去查机器，需要 `ALERT_AUTO_DIAGNOSE=1`（每条告警会跑一次
       模型，因此默认关闭，并配了去重 + 频控两道闸）。
    """
    alerts = normalize_alerts(payload)
    reports = []

    # ★ 空批次不是告警。
    #   归一化层已经不再把 `{"alerts": []}` 变成一条凭空捏造的 UnknownAlert（D6），
    #   这里再把"确实收到 0 条"如实说清楚 —— 而不是返回一个看不出发生过什么的响应。
    if not alerts:
        write_audit("alert.empty_batch", {"keys": sorted((payload or {}).keys())})
        return {"received": 0, "reports": [],
                "message": "空告警批次（alerts 为空）：未建事件、未触发任何诊断"}

    for alert in alerts:
        question = alert_to_question(alert)
        base = {
            "alertname": alert["alertname"],
            "host": alert.get("host"),
            "severity": alert["severity"],
        }

        # ---- 第 0 步：抑制 / 聚合 / 频控，三道闸都挡在"花钱"之前 ----
        # ★ 顺序是关键：必须在 parse_intent（调模型）**之前**判。
        #   放在之后的话，被抑制的那部分告警也已经为每个 token 付过钱了 ——
        #   抑制的意义在于不花钱，不在于不返回。

        # 闸 1：维护窗口。
        #   计划内变更（磁盘扩容 / 发版 / 主从切换）本来就会触发一堆告警。
        #   不抑制的话，Agent 会认真地为你自己的维护动作做诊断并推通知 ——
        #   几次之后值班的人就开始无视通知了，**那才是真正的事故**。
        silence_hit = _ALERT_SILENCE.match(alert)
        if silence_hit:
            report = {**base, "decision": "silenced",
                      "reason": (f"命中维护窗口：{silence_hit.get('note') or '（无备注）'}"
                                 f"（{silence_hit['_matched_until']} 前生效），"
                                 f"本次不诊断"),
                      "playbook": []}
            obs_metrics.counter("agentdesk_alerts_received_total",
                                {"decision": report["decision"]})
            write_audit("alert.silenced", {
                **report,
                "rule": {k: v for k, v in silence_hit.items()
                         if not k.startswith("_")},
            })
            reports.append(report)
            continue

        # 闸 2：去重 / 聚合。
        #   窗口内同源告警只处理一次，其余作为成员并入**同一个事件**。
        #   **被抑制 ≠ 被丢弃** —— 下面会给它一个 incident_id，
        #   值班的人能看到"这条告警去哪了"，而不是凭空少了一条。
        verdict = _ALERT_AGG.ingest(alert)
        if not verdict.is_new:
            incident_id = verdict.prev_incident_id
            if incident_id:
                _link_alert_quietly(incident_id, alert)
            if ALERT_AGGREGATE:
                decision = ("merged_into_incident" if incident_id
                            else "suppressed_duplicate")
                reason = (f"{ALERT_DEDUP_SECONDS}s 内已处理过同源告警"
                          f"（{verdict.key}），本次并入事件后跳过诊断"
                          f"（不重复花钱）；这是窗口内第 {verdict.members} 条")
            else:
                # 聚合关掉时**保持改动前的字段值与原文案** —— "可以一键回退"
                # 如果连 decision 的取值都变了，回退就不是回退。
                # 事件归并仍然发生，但只体现在新增字段 incident_id 上。
                decision = "suppressed_duplicate"
                reason = (f"{ALERT_DEDUP_SECONDS}s 内已处理过同名同机的告警，"
                          f"本次跳过（不重复花钱）")
            report = {**base,
                      "decision": decision,
                      "reason": reason,
                      "incident_id": incident_id or None,
                      "members": verdict.members,
                      "playbook": []}
            obs_metrics.counter("agentdesk_alerts_received_total",
                                {"decision": report["decision"]})
            write_audit("alert.suppressed", report)
            reports.append(report)
            continue

        # 闸 3：频控
        hour = time.strftime("%Y%m%d%H", time.localtime())
        if _ALERT_HOUR["hour"] != hour:
            _ALERT_HOUR["hour"] = hour
            _ALERT_HOUR["count"] = 0
        if _ALERT_HOUR["count"] >= ALERT_MAX_PER_HOUR:
            report = {**base, "decision": "rate_limited",
                      "reason": (f"本小时自动诊断已达上限 {ALERT_MAX_PER_HOUR} 次，"
                                 f"本次交给人工。调大 ALERT_MAX_PER_HOUR 可放宽")}
            obs_metrics.counter("agentdesk_alerts_received_total",
                                {"decision": report["decision"]})
            write_audit("alert.rate_limited", report)
            reports.append(report)
            continue
        _ALERT_HOUR["count"] += 1

        # ---- 事件：把这条告警挂到一个"故障"上，而不是散着 ----
        #   这一步刻意放在三道闸**之后**：被抑制/被频控的告警不该凭空开一个事件。
        #   建事件失败不阻断诊断（事件是"记账"，不是"诊断的前提"）。
        incident = _open_incident(alert)
        incident_id = incident.get("id", "")
        if incident_id:
            _ALERT_AGG.bind_incident(verdict.key, incident_id)
            _link_alert_quietly(incident_id, alert)

        state = {"alert": alert, "question": question, "base": base,
                 "incident_id": incident_id, "members": verdict.members}

        # ---- 异步分岔（可选，`ALERT_ASYNC=1`）----
        #
        # ★ 为什么需要它：Alertmanager 这类告警系统对 webhook 有超时约束，
        #   而一轮诊断要 6–20 秒。同步跑的话，对端会超时重推，
        #   于是**同一批告警被重复投递**（幂等由聚合表兜住，但请求本身是白跑的）。
        #
        # ★ 为什么默认仍然是同步：异步的代价是"结论不在响应里"，
        #   而且**进程若在后台任务跑完前退出，这次诊断就丢了**
        #   （没有持久队列 —— 单进程 + 不加依赖是本项目的既定取舍）。
        #   所以它是个开关，默认关；开了之后审计里有 alert.accepted 可对账，
        #   事件会停在 open 状态，不会假装诊断过。
        if ALERT_ASYNC and background is not None:
            background.add_task(_run_alert_pipeline, state)
            write_audit("alert.accepted", {
                "alertname": alert["alertname"], "host": alert.get("host"),
                "incident_id": incident_id, "async": True,
                "question": question})
            reports.append({**base, "decision": "accepted_async",
                            "incident_id": incident_id,
                            "members": verdict.members,
                            "reason": ("已受理，诊断在后台执行（ALERT_ASYNC=1）；"
                                       "结论请查 /incidents/" + (incident_id or "")),
                            "playbook": []})
            continue

        reports.append(_run_alert_pipeline(state))

    return {"received": len(alerts),
            "async": bool(ALERT_ASYNC and background is not None),
            "reports": reports}


def _run_alert_pipeline(state: dict) -> dict:
    """告警的**慢路径**：理解 → 定风险 → 诊断/预案 → 写回事件 → 通知。

    同步（默认）与异步（`ALERT_ASYNC=1`）两条路都走这里。
    **逻辑只有一份** —— 否则"异步那条少做了一步"这类错误会长期潜伏，
    而且只在开了开关的机器上出现（本地默认关，测试也测不到）。

    state 由快路径准备好：alert / question / base / incident_id / members。
    """
    alert = state["alert"]
    question = state["question"]
    base = state["base"]
    incident_id = state["incident_id"]
    members = state["members"]

    # ---- 第一步：理解告警 ----
    try:
        intent, attempts = parse_intent(question)
    except IntentParseFailed as e:
        report = {**base, "decision": "parse_failed",
                  "reason": str(e), "problems": e.problems}
        write_audit("alert.parse_failed", report)
        return report

    action = intent.get("action")
    service = (intent.get("service") or "").lower()
    base["intent"] = intent
    base["parse_attempts"] = attempts

    # ---- 第二步：定风险 ----
    #
    # ★ 这里曾经是个**闸门形同虚设**的漏洞：原先只用模型自报的 risk，
    #   而本文件那份提示词把"重启服务"定义成 medium、闸门只拦 high ——
    #   实测"mysql 挂了需要立即重启"一路走到 auto_diagnose。
    #
    #   更根本的问题是，同一个"重启服务"在本项目里有三处各自定义风险：
    #     ① 这份提示词            → medium
    #     ② policy.py 的命令规则  → needs_approval（要人工）
    #     ③ ops.py 的 run_command → high
    #   现在三者收敛到 policy.py 一处（ACTION_RISK），判定的规则是：
    #
    #     **模型自报的 risk 只能抬高、不能降低**（policy.escalate 取更危险者）。
    #     它会判错，而它的错会直接把闸门打开；
    #     反过来，它判得更严时值得尊重 —— 它可能看到了规则表没覆盖的上下文。
    #     **它可以让我们更谨慎，不能让我们更冒险。**
    #
    #   两个值都留在返回里：risk 是实际用于决策的，risk_reported 是模型的原本判断，
    #   留着对账（哪条告警被抬高了、抬到第几档，审计里要看得到）。
    from app.sandbox import policy
    reported = intent.get("risk")
    risk = policy.escalate(reported, policy.risk_of_action(action))
    base["risk"] = risk
    base["risk_reported"] = reported

    if risk == "high":
        report = {**base, "decision": "need_human",
                  "reason": (f"请求要动手改系统（action={action}），未执行任何操作，"
                             f"等待人工确认"),
                  "playbook": []}
        write_audit("alert.gate_blocked", {
            "alertname": alert["alertname"], "action": action,
            "risk": risk, "risk_reported": reported,
            "reason": report["reason"]})
    elif ALERT_AUTO_DIAGNOSE:
        # ---- 第三步 A：真的跑一轮诊断（需显式开启，因为它花钱）----
        try:
            from app.agents.supervisor import run as engine_run
            result = engine_run(question)
            # 指标字段与 /agent/ask 保持一致（同一套口径，两处复用）——
            # 这样告警诊断和手动提问在可观测看板上是**可比的一组数**。
            metrics = {
                "rounds": result.get("rounds"),
                "tool_calls": result.get("tool_calls"),
                "distinct_tools": len(result.get("distinct_tools") or []),
                "tokens": (result.get("usage") or {}).get("total_tokens", 0),
                "elapsed_ms": result.get("elapsed_ms"),
                "stop_reason": result.get("stop_reason"),
            }
            report = {**base, "decision": "auto_diagnosed",
                      "reason": "已自动跑完一轮诊断（只读，未执行任何写操作）",
                      "answer": result.get("answer"),
                      "metrics": metrics,
                      "executed": False}
            write_audit("alert.diagnosed", {
                "alertname": alert["alertname"], "question": question,
                **metrics})
        except Exception as e:
            report = {**base, "decision": "diagnose_failed",
                      "reason": f"自动诊断失败：{e}",
                      "playbook": DIAGNOSE_PLAYBOOK.get(service, DEFAULT_PLAYBOOK)}
    else:
        # ---- 第三步 B：只出预案（默认）——它**不是**诊断结果 ----
        playbook = DIAGNOSE_PLAYBOOK.get(service, DEFAULT_PLAYBOOK)
        report = {**base, "decision": "playbook_only",
                  "reason": "只读诊断，已生成处置预案",
                  "playbook": playbook,
                  "executed": False,
                  "note": ("这是一份**写死的命令清单**，不是诊断结论 —— "
                           "它没查过任何机器。"
                           "要让它真去查，把 ALERT_AUTO_DIAGNOSE=1 打开"
                           "（每条告警会跑一次模型，有去重与频控兜底）")}

    # ---- 归宿：把结论写回事件，并把结论推到"人真正会看的地方" ----
    if incident_id:
        _attach_diagnosis_quietly(incident_id, report)
        report["incident_id"] = incident_id

    obs_metrics.counter("agentdesk_alerts_received_total",
                        {"decision": report["decision"]})
    write_audit("alert.handled", report)

    # ★ 这一步是 M2 存在的理由：告警链路的终点原来只有"HTTP 响应 + audit.jsonl"，
    #   凌晨三点**没有任何人会被叫醒**。诊断做得再对，送不到人手上就等于没做。
    #   通知是旁路：events.send 永不抛异常，推不出去只留一条失败记录。
    _notify_incident(report, incident_id, members)

    return report


# ============================================================
# 事件与通知：把"一次告警处理"落到一个可追踪的故障上
# ============================================================
def _incident_store():
    """拿到事件存储。

    ★ 必须在**请求时**取单例，不能顶层绑定。
      测试用 monkeypatch 替换 `app.incident.store.store` 来把落盘改到临时目录；
      顶层 `from ... import store` 会把函数对象绑死，隔离装置就失效了 ——
      那正是 M1 那次"假 trace 写进真实 logs"的事故形态（成本口径被压低 12 倍）。
    """
    from app.incident import store as incident_store
    return incident_store.store()


def _open_incident(alert: dict) -> dict:
    """为一条告警建立事件。失败只记审计，不阻断诊断。"""
    try:
        return _incident_store().create(
            source="alert",
            host=alert.get("host") or "",
            service=alert.get("service") or "",
            severity=alert.get("severity") or "unknown",
            summary=alert.get("summary") or "",
        )
    except Exception as e:
        write_audit("incident.create_failed", {
            "alertname": alert.get("alertname"),
            "error": f"{type(e).__name__}: {e}"})
        return {}


def _link_alert_quietly(incident_id: str, alert: dict) -> None:
    """把一条告警并入事件的成员列表。失败不阻断（少记一条成员不影响处置）。"""
    try:
        _incident_store().link_alert(incident_id, alert)
    except Exception:
        pass


def _attach_diagnosis_quietly(incident_id: str, report: dict) -> None:
    """把诊断结论写回事件。过不了校验/失败的结论也如实记（ok=False）。"""
    answer = report.get("answer")
    if not answer:
        return
    try:
        _incident_store().attach_diagnosis(
            incident_id, str(answer),
            trace_id=report.get("trace_id"),
            ok=report.get("decision") != "diagnose_failed")
    except Exception:
        pass


def _notify_incident(report: dict, incident_id: str, members: int) -> None:
    """推通知，并把投递结果记回事件（送达与失败都记，**失败不许谎报成功**）。"""
    outcome = notify_events.incident(report, incident_id=incident_id, members=members)
    if outcome is None or not incident_id:
        return
    results = getattr(outcome, "results", []) or []
    ok = bool(getattr(outcome, "ok", False)) and not getattr(outcome, "deduped", False)
    err = "" if ok else "; ".join(
        f"{r.channel}: {r.error}" for r in results if not r.ok) or "已去重"
    try:
        _incident_store().mark_notified(
            incident_id, ",".join(getattr(r, "channel", "?") for r in results) or "none",
            ok, err)
    except Exception:
        pass


# ============================================================
# RAG：检索增强
# ============================================================
class SearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="要检索的问题")
    top_k: int = Field(5, ge=1, le=20, description="返回前几条")
    mode: str = Field("hybrid", pattern="^(vector|bm25|hybrid)$",
                      description="检索模式：vector 纯向量 / bm25 纯关键词 / hybrid 混合")


def _rag_error(e: Exception):
    """把 RAG 层的异常翻译成合适的 HTTP 状态码。"""
    if isinstance(e, FileNotFoundError):
        # 索引还没建 —— 这是「前置条件不满足」，用 503 而不是 500
        return HTTPException(status_code=503, detail=(
            f"{e}。请先调用 POST /rag/index 构建索引，"
            "或执行 python -m app.rag.pipeline build"))
    if isinstance(e, RuntimeError):
        # 索引与当前 embedding 后端不匹配
        return HTTPException(status_code=409, detail=str(e))
    return HTTPException(status_code=502, detail=str(e))


@app.get("/rag/stats")
def rag_stats():
    """索引统计：有多少文档、多少块、用的哪个向量后端。"""
    from app.rag.pipeline import load_store
    try:
        store = load_store()
    except (FileNotFoundError, RuntimeError) as e:
        raise _rag_error(e)

    docs = sorted({c.doc_id for c in store.chunks})
    return {
        "documents": len(docs),
        "chunks": len(store.chunks),
        "doc_ids": docs,
        "embedder": store.embedder.describe(),
        "embedder_ready": store.embedder.backend != "local",
    }


@app.post("/rag/index")
def rag_index():
    """重建索引。

    语料有更新（新增或修改文档）之后必须重建 —— 向量不会自己更新。
    生产上这一步会做成增量索引加定时任务；当前规模直接全量重建，
    几十个块只要一两秒。
    """
    from app.rag.pipeline import build_index, load_store
    try:
        stats = build_index(verbose=False)
        load_store(force=True)      # 清掉进程内缓存，让新索引立即生效
    except (FileNotFoundError, RuntimeError) as e:
        raise _rag_error(e)
    write_audit("rag.index_rebuilt", {"documents": stats["documents"],
                                      "chunks": stats["chunks"]})
    return stats


@app.post("/rag/search")
def rag_search(req: SearchRequest):
    """只检索，不生成。

    这个接口是用来定位问题的：回答不对时，
    先调它看看检索回来的片段对不对 ——
    如果检索就不对，那是语料或切分的问题，改 Prompt 没用。
    """
    from app.rag.pipeline import retrieve
    try:
        hits = retrieve(req.question, top_k=req.top_k, mode=req.mode)
    except (FileNotFoundError, RuntimeError) as e:
        raise _rag_error(e)
    return {"question": req.question, "mode": req.mode,
            "count": len(hits), "hits": hits}


@app.post("/rag/ask")
def rag_ask(req: SearchRequest):
    """RAG 问答：先检索，再让模型基于检索结果回答，并附引用来源。

    回答里带着 [1][2] 这样的编号，用户可以翻回原文核对 ——
    这是 RAG 相对「直接问模型」最大的价值：**可验证**。
    模型有没有编，看一眼引用就能判断。
    """
    from app.rag.pipeline import answer as rag_answer
    try:
        result = rag_answer(req.question, top_k=req.top_k, mode=req.mode)
    except (FileNotFoundError, RuntimeError) as e:
        raise _rag_error(e)
    except ModelError as e:
        raise HTTPException(status_code=502, detail=str(e))

    write_audit("rag.ask", {"question": req.question, "mode": req.mode,
                            "cited": [c["source"] for c in result["citations"]]})

    # hits 里带完整原文，响应会很大，对外只返回引用信息
    return {
        "question": result["question"],
        "answer": result["answer"],
        "mode": result["mode"],
        "citations": result["citations"],
    }


# ============================================================
# Agent：ReAct 循环（自己决定调哪个工具）
# ============================================================
# 这一组接口是项目的分水岭：前面的接口都是"你说一句，它答一句"，
# 从这里开始，服务会**自己决定要做几件事、按什么顺序做**。
class AgentRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="要诊断的问题")
    engine: str = Field("langgraph", pattern="^(handwritten|langgraph|supervisor)$",
                        description="编排引擎：handwritten 手写循环 / "
                                    "langgraph 状态图 / supervisor 多 Agent 编排")
    max_retries: int = Field(1, ge=0, le=3,
                             description="仅 supervisor：校验不通过时最多重查几次")
    max_steps: int = Field(6, ge=1, le=12,
                           description="最多几轮工具调用。这既是成本上限，也是防死循环的护栏")
    include_trace: bool = Field(True, description="是否返回完整执行轨迹")


@app.get("/agent/tools")
def agent_tools():
    """列出 Agent 能调用的工具，以及每个工具的风险等级。

    【为什么要把这个暴露成接口】
    1. 调试时能一眼看到"模型到底有哪些牌可打"
    2. 演示时可以直接展示 —— 工具清单就是能力的边界
    3. 风险等级在这里是公开的：调用方能看到哪些操作需要人工确认
    """
    from app.tools import tool_catalog
    from app.tools.ops import BACKEND
    return {"backend": BACKEND, "count": len(tool_catalog()),
            "tools": tool_catalog()}


@app.get("/agent/graph")
def agent_graph(max_steps: int = Query(6, ge=1, le=12),
                engine: str = Query("react", pattern="^(react|supervisor)$"),
                max_retries: int = Query(1, ge=0, le=3)):
    """导出状态图的 mermaid 定义。

    LangGraph 白送的能力：图的结构不用手画，直接导出。
    把这段文本贴进任何支持 mermaid 的地方（GitHub README、飞书文档、
    VS Code 预览）就会渲染成流程图。

    这张图比任何口头描述都直观：**一眼能看出哪里是循环**。
    """
    from app.agents.graph import mermaid
    if engine == "supervisor":
        from app.agents.supervisor import mermaid as sup_mermaid
        text = sup_mermaid(max_retries)
        return {"engine": engine, "max_retries": max_retries,
                "format": "mermaid", "graph": text}
    text = mermaid(max_steps)
    return {"engine": engine, "max_steps": max_steps,
            "format": "mermaid", "graph": text}


@app.post("/agent/ask")
def agent_ask(req: AgentRequest):
    """让 Agent 自己诊断一个问题。

    【为什么是 def 而不是 async def】
    这个接口内部是同步的（模型调用 + 工具执行都是阻塞的），
    一次要跑好几秒。写成 def，FastAPI 会把它丢进线程池执行；
    写成 async def 反而会卡住事件循环 —— 一个请求就把所有人都堵住。

    这是最容易搞反的一处：**不是所有接口都该写成 async。**
    只有内部真的用了异步 IO（比如 httpx.AsyncClient）时，
    async def 才有意义。
    """
    started_ts = datetime.now().isoformat(timespec="seconds")

    # 三个引擎的返回结构是统一的（question/answer/steps/rounds/...），
    # 所以这里只需要换实现，下面的响应组装代码完全一样。
    # **这就是"统一返回结构"的价值** —— 加第三个引擎没有改动下游任何一行。
    if req.engine == "handwritten":
        from app.agents.react import run as engine_run
        runner = lambda: engine_run(req.question, max_steps=req.max_steps)
    elif req.engine == "supervisor":
        from app.agents.supervisor import run as engine_run
        runner = lambda: engine_run(req.question, max_retries=req.max_retries)
    else:
        from app.agents.graph import run as engine_run
        runner = lambda: engine_run(req.question, max_steps=req.max_steps)

    try:
        result = runner()
    except ModelError as e:
        # 模型层故障 → 502（上游问题，可重试）
        raise HTTPException(status_code=502, detail=str(e))

    return _agent_payload(result, started_ts, req.engine, req.include_trace)


def _agent_payload(result: dict, started_ts: str, engine: str,
                   include_trace: bool = True) -> dict:
    """审计留痕 + 响应组装 —— 一次性接口与流式接口共用。

    ★ 与 supervisor._build_result 同一个理由：流式版的 result 事件
      必须与 /agent/ask 的 JSON 逐字段一致 —— 同一个问题两个入口
      给出不同结构，是新的对账问题。（那里保证引擎层一致，
      这里保证 HTTP 层一致。）
    """
    # 审计留痕：Agent 自己做了决定这件事，必须可追溯。
    # 记的是"它查了什么"，而不是"它答了什么" —— 后者可以从日志里再取，
    # 前者才是排查"它为什么这么判断"的关键。
    write_audit("agent.ask", {
        "question": result["question"],
        "engine": engine,
        "rounds": result["rounds"],
        "tool_calls": result["tool_calls"],
        "tools": result["distinct_tools"],
        "stop_reason": result["stop_reason"],
        "tokens": result["usage"].get("total_tokens", 0),
    })

    payload = {
        "started_at": started_ts,
        "engine": result["engine"],
        "question": result["question"],
        "answer": result["answer"],
        # 这三个数字是 Agent 特有的可观测指标 ——
        # 普通问答接口没有"轮次"和"工具调用"这两个概念
        "metrics": {
            "rounds": result["rounds"],
            "tool_calls": result["tool_calls"],
            "distinct_tools": len(result["distinct_tools"]),
            "tokens": result["usage"].get("total_tokens", 0),
            "elapsed_ms": result["elapsed_ms"],
            "stop_reason": result["stop_reason"],
        },
    }
    # 多 Agent 编排特有的产出：意图标签、校验结论、执行路径、Supervisor 决策。
    # 这些是"单 Agent 模式拿不到的信息" —— 也是拆分之后才能讲出来的东西。
    if engine == "supervisor":
        payload["orchestration"] = {
            "intent": result.get("intent"),
            "intent_status": result.get("intent_status"),
            "knowledge": {
                "query": (result.get("knowledge") or {}).get("query"),
                "count": (result.get("knowledge") or {}).get("count"),
                "sources": [r["source"] for r
                            in (result.get("knowledge") or {}).get("results", [])],
            },
            "verdict": result.get("verdict"),
            # ★ 处置结果。pending_approvals 是响应里最该被看一眼的字段 ——
            #   它代表「Agent 想做但还没做」的全部内容。
            "remediation": result.get("remediation"),
            "pending_approvals": result.get("pending_approvals") or [],
            "executed_commands": result.get("executed_commands") or [],
            "denied_commands": result.get("denied_commands") or [],
            "path": result.get("path"),
            "node_log": result.get("node_log"),
            "supervisor_decisions": result.get("supervisor_decisions"),
            "agents": 5,
        }

    if include_trace:
        # ★ 去掉 observation_text —— 那是给内部校验用的完整工具返回，
        #   对外只需要预览。**数据在源头保留完整，由出口决定裁剪**（见 common.py）。
        payload["trace"] = [
            {k: v for k, v in step.items() if k != "observation_text"}
            for step in result["steps"]
        ]
    return payload


# ============================================================
# 十二、沙箱与人工确认
# ============================================================
# 这一组接口是**给人用的**，不是给 Agent 用的。
#
# 前面所有接口（/chat /rag /agent/ask）的调用方都是"程序"或者"模型"；
# 这四个的调用方是**值班的人**：
#
#     GET  /sandbox                   "现在这套东西到底能执行什么？"
#     GET  /approvals?status=pending  "有什么在等我批？"
#     POST /approvals/{id}/approve    "我同意这一条"
#     POST /approvals/{id}/execute    "执行它"
#
# ★ 为什么"批准"和"执行"要分成两个动作？
#
#   合成一个"批准并执行"看起来更省事，但会丢掉一个关键信息：
#   **批准是一个决定，执行是一个动作。** 拧在一起之后，
#   你没法表达"我同意这么做，但现在先别做"（比如要等到维护窗口）。
#
#   而且分开之后，"谁批准了"和"谁执行的"可以不是同一个人 ——
#   这在有审批流程的团队里是常态，也是审计真正关心的东西。
class ApprovalAction(BaseModel):
    """审批动作。by 必填 —— 见下面 approve 接口的说明。"""

    by: str = Field(..., min_length=1, max_length=64,
                    description="审批人标识（姓名 / 工号 / 邮箱均可）。"
                                "**必填**，它是审计里最关键的一个字段")
    note: str = Field("", max_length=300, description="备注")


# ============================================================
# 接口：流式版 Agent 诊断（SSE）
# ============================================================
@app.post("/agent/ask/stream")
def agent_ask_stream(req: AgentRequest):
    """/agent/ask 的流式版（SSE，仅 supervisor 引擎）。

    【为什么要有它 —— 这是被演示体验逼出来的】
    supervisor 跑一轮要 5-20 秒。一次性接口下，/try 页面点「开始」后
    用户对着空白页干等 —— 而**最值得看的部分（Agent 自己决定查什么、
    一共跑了几步）恰好发生在这段等待里**。SSE 把每个节点完成的事件
    实时推给前端，「多 Agent 编排」从结果里的文字变成看得见的过程。

    【为什么只支持 supervisor】
    流式要展示的是「编排过程」；handwritten / langgraph 是单 Agent，
    没有可展示的节点序列。传其他引擎直接 400，宁可报错不要静默降级。

    【鉴权与降级】
    走统一安全中间件（与其他接口同规则，令牌必填）；
    SSE 中途出错以 error 事件收尾（HTTP 200 已发出，错误必须在事件里）。
    """
    if req.engine != "supervisor":
        raise HTTPException(
            status_code=400,
            detail="流式接口仅支持 supervisor 引擎 —— 要展示的正是编排过程本身")

    def gen():
        started_ts = datetime.now().isoformat(timespec="seconds")
        yield _sse_event("start", {"question": req.question})
        try:
            from app.agents.supervisor import run_stream
            for ev in run_stream(req.question, max_retries=req.max_retries):
                if ev.get("type") == "result":
                    # ★ result 事件 = 与 POST /agent/ask 完全相同的 JSON 结构
                    #   （同一个 _agent_payload 组装）—— 前端两种模式可以
                    #   用同一套渲染代码，不存在"流式的结果少几个字段"。
                    ev.pop("type", None)
                    yield _sse_event("result", _agent_payload(
                        ev, started_ts, "supervisor", req.include_trace))
                else:
                    yield _sse_event("step", ev)
        except Exception as e:                      # noqa: BLE001
            # SSE 头已发出，HTTP 状态码救不了 —— 错误必须作为事件送达
            yield _sse_event("error", {"error": str(e)[:300]})
        except Exception as e:                      # noqa: BLE001
            # SSE 头已发出，HTTP 状态码救不了 —— 错误必须作为事件送达
            yield _sse_event("error", {"error": str(e)[:300]})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store",
                 "X-Accel-Buffering": "no"},   # 防 Nginx 缓冲把流变成一次性到达
    )


def _sse_event(event: str, data: dict) -> str:
    """一条 SSE 帧。

    ★ 换行写成 chr(10) 而不是反斜杠字面量 —— 本文件的改动经常由脚本完成，
      反斜杠转义在 JSON/shell/Python 三层传递里最容易被吃掉一层
      （本项目实测两次：一次 onclick 引号、一次就是这一行）。
      用 chr(10) 之后，这个函数不再含有任何转义序列，脚本怎么传都不会坏。
    """
    nl = chr(10)
    return (f"event: {event}{nl}"
            f"data: {json.dumps(data, ensure_ascii=False)}{nl}{nl}")



@app.get("/sandbox")
def sandbox_status():
    """沙箱状态 + 命令白名单。

    演示时这个接口很有用：它一次回答了"你能执行什么、哪些要人批、
    **哪些真的有隔离**"三个问题。
    """
    from app.sandbox import executor, policy

    info = executor.preflight()
    info["policy"] = policy.describe()
    info["commands"] = policy.catalog()

    # ★ 把"哪些写操作**没有**容器隔离"显式说出来（M3 C7）。
    #
    #   原先文档讲"写操作在一次性容器里执行"，而实际能进容器的只有文件类操作
    #   （`truncate` 这种）；`systemctl restart` / `docker restart` 必须在
    #   **主机上**执行才能生效 —— 它们靠审批 + 审计约束，**没有容器隔离**。
    #
    #   这不是能"修"的缺陷（要重启宿主机上的服务就不可能只给它一个容器），
    #   能修的是"别让文档说出与实际不符的话"。
    #   所以在这里把事实摊开：谁有隔离、谁没有、没有的靠什么约束。
    host_approval = sorted(k for k, r in policy.COMMANDS.items()
                           if r.isolation == policy.CHANNEL_HOST
                           and r.decision == policy.NEEDS_APPROVAL)
    info["isolation_scope"] = {
        "container_channel": policy.describe()["container_channel"],
        "host_channel": policy.describe()["host_channel"],
        "host_channel_needs_approval": host_approval,
        "note": ("写操作里只有 container 通道那批在一次性容器里执行"
                 "（非 root、根只读、无网络、内存封顶）；"
                 "systemctl restart / docker restart 这类必须在主机上执行才有意义，"
                 "**它们没有容器隔离**，靠「审批 + 指纹 + 审计」约束。"
                 "别把「写操作都进容器」当成事实 —— 那是这份文档以前的说法。"),
    }
    return info


@app.get("/approvals")
def list_approvals(status: Optional[str] = Query(
        None, pattern="^(pending|approved|rejected|consumed|expired)$",
        description="按状态过滤，不传返回全部"),
    limit: int = Query(50, ge=1, le=200)):
    """列出审批单。默认按时间倒序。

    ★ pending 是运维最关心的那个视图 ——
      "有什么在等我批"，这句话应该有一个接口能直接回答，
      而不是让人去翻日志。
    """
    from app.sandbox import approvals

    st = approvals.store()
    return {
        "counts": st.counts(),
        "status_filter": status,
        "items": st.list(status=status, limit=limit),
    }


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: str):
    from app.sandbox import approvals

    try:
        return approvals.store().get(approval_id)
    except approvals.ApprovalError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, req: ApprovalAction):
    """批准一张审批单。

    ★ 为什么 by 是必填的？

      一张没有审批人的审批单，等于**没有审批**。

      它在审计上回答问题"谁批准了这次变更"时是空的 ——
      事故复盘时，"系统批准的"不是一个能交差的回答。
      所以这里宁可多要一个字段，也不要事后追责时发现查不到人。

      （这条约束写在 approvals.py 的 store 层，不只是这里。
        接口层可以绕过（比如别的调用方），store 层绕不过。）
    """
    from app.sandbox import approvals

    # ★ 观测：审批动作是一次**独立的人机交互**，单独成一条 trace。
    #   理由和下面的 execute 接口一样：它不在 Agent 那次运行的上下文里。
    with obs.trace("approval.approve", question=approval_id,
                   approval_id=approval_id, by=req.by):
        try:
            rec = approvals.store().approve(approval_id, by=req.by, note=req.note)
        except approvals.ApprovalError as e:
            # 409 = 状态冲突（比如已经批过了）。用 409 不用 400，
            # 因为"你这个请求本身没问题，是对象当前状态不允许"——
            # 调用方看到 409 就知道该刷新一下状态，看到 400 会去改参数。
            raise HTTPException(status_code=409, detail=str(e))

        write_audit("approval.approved", {
            "approval_id": approval_id, "by": req.by, "note": req.note,
            "command": rec.get("command"), "fingerprint": rec.get("fingerprint"),
        })
        return rec


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, req: ApprovalAction):
    """驳回一张审批单。驳回理由会被记录 —— 它是改进 Prompt 的素材。"""
    from app.sandbox import approvals

    # 观测：和 approve 一样，单独成 trace（驳回也是写操作生命周期的一步）
    with obs.trace("approval.reject", question=approval_id,
                   approval_id=approval_id, by=req.by):
        try:
            rec = approvals.store().reject(approval_id, by=req.by, note=req.note)
        except approvals.ApprovalError as e:
            raise HTTPException(status_code=409, detail=str(e))

        write_audit("approval.rejected", {
            "approval_id": approval_id, "by": req.by, "note": req.note,
            "command": rec.get("command"),
        })
        return rec


# ★ 观测：把「执行一次写操作」包成一条独立的 trace。
#
#   为什么必须单独包：这个接口是**独立的 HTTP 请求** ——
#   审批发生在 Agent 那次运行之后（可能过了几分钟，甚至是另一个人点的），
#   所以它天然不在那次 Agent trace 的上下文里。
#   不包的话，「容器真的执行了」这件事在 trace 里完全不存在（只有审计日志记了），
#   而这恰恰是最该被追溯的一步：**真正改动系统的那一下**。
#
#   包上之后，被拒绝、指纹不匹配这类「没执行成功的尝试」也会留痕 ——
#   trace() 会把异常记成 status=error。写操作的生命周期因此是完整的。
#
#   外层只负责开 trace，真正逻辑在 _execute_approved 里（不重新缩进整段代码，
#   免得动到那几十行本来正确的校验逻辑）。
@app.post("/approvals/{approval_id}/execute")
def execute_approval(approval_id: str, req: ApprovalAction):
    """执行一张**已批准**的审批单（外层只负责开 trace）。"""
    with obs.trace("approval.execute", question=approval_id,
                   approval_id=approval_id, by=req.by):
        return _execute_approved(approval_id, req)


def _execute_approved(approval_id: str, req: ApprovalAction):
    """执行一张**已批准**的审批单。

    【这里做三道校验，缺一道都不行】

      ① 状态必须是 approved（approvals.consume 里卡）
         → 挡住"没批就执行"

      ② 只能消费一次（consumed 是终态）
         → 挡住"一次批准执行一百次"。**审批系统的头号漏洞就是重放。**

      ③ 命令指纹必须和审批时一致
         → 挡住 TOCTOU：批准的命令和执行的命令必须是同一条

    这三道都在 store 层实现，**不在这个接口里** ——
    因为接口层是可以被绕过的（换个调用方、写个脚本直接调），
    只有放在状态机里，约束才是真的。

    ★ 这也是本项目反复出现的那个原则的又一次应用：
      **把约束放在"绕不过去"的那一层。**
      提示词可以被忽略，接口可以被绕过，状态机绕不过。
    """
    from app.sandbox import approvals, executor, policy

    st = approvals.store()

    # ★ 执行前的**全部**校验收敛到 store 的 `check_executable` 一处：
    #   存在 / 已批准 / 未过期 / 指纹一致。
    #   原先这些判断有一部分散在这个接口里，接口少判一条就是一个洞；
    #   现在"能不能执行"只有一个定义，接口层只负责把它翻译成 HTTP 状态码。
    #
    #   M3 新增的那一条是**有效期**：一条三天前批准的命令，
    #   今天的环境已经和当时不是同一回事了 —— 过期就拒绝（用户选定方案）。
    try:
        rec = st.check_executable(approval_id)
    except approvals.ApprovalError as e:
        msg = str(e)
        status = 404 if "没有这张审批单" in msg else 409
        if status == 409:
            # 被拦下的尝试也必须留痕（C8 的审计缺口之一）。
            # 命令文本取不到就留空 —— 审计写不进去不能反过来影响这次拒绝。
            try:
                cmd = st.get(approval_id).get("command")
            except approvals.ApprovalError:
                cmd = ""
            write_audit("approval.execute_blocked", {
                "approval_id": approval_id, "by": req.by,
                "command": cmd, "reason": msg,
            })
        raise HTTPException(status_code=status, detail=msg) from e

    # ★ 重新走一遍策略，拿到带 argv 的 Decision。
    #
    #   为什么不把 argv 存进审批单？因为**策略可能已经变了** ——
    #   白名单调整过、撤回了一条规则。这时候正确的行为是**重新判定**，
    #   而不是拿几天前的决定去执行。
    #
    #   「批准时合法」和「执行时合法」是两件事，
    #   两个时刻都要成立才允许执行。
    decision = policy.decide(rec["command"])

    if decision.decision == policy.DENY:
        write_audit("approval.execute_blocked", {
            "approval_id": approval_id, "by": req.by,
            "command": rec["command"], "reason": decision.reason,
        })
        raise HTTPException(
            status_code=409,
            detail=f"这条命令现在已被策略禁止，拒绝执行：{decision.reason}")

    try:
        # 先消费（占位），再执行 —— 顺序很重要。
        #
        # 反过来的话：执行成功但消费失败（比如写日志时崩了），
        # 这张单子会停留在 approved，下次还能再执行一次。
        # **宁可出现"已标记消费但执行失败"，也不要出现"执行成功还能再执行"。**
        # 前者是少做了一次（人能看到错误），后者是重复做（可能造成事故）。
        #
        # （consume 内部也会再校验一次指纹与状态，且整个判定到写入
        #   都在跨进程锁里 —— 见 approvals._mutating。）
        st.consume(approval_id, expected_fingerprint=decision.fingerprint,
                   by=req.by, ok=True)
    except approvals.ApprovalError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        # ★ 观测：真正动手执行的那一下，单独一个 sandbox span。
        #   里面记 backend / isolated / exit_code —— 这三个字段回答
        #   "它到底是在真容器里跑的，还是退回了本机/仿真"。
        #   沙箱不可用时 SandboxUnavailable 会被 span() 自动记成 status=error
        #   然后原样抛出（观测层不吞业务异常），外面的 except 照常处理。
        with obs.span(obs.TYPE_SANDBOX, name="sandbox.execute",
                      command=rec["command"],
                      isolation=decision.isolation) as sp:
            result = executor.run(decision)
            sp.set("backend", result.backend)
            sp.set("isolated", bool(result.isolated))
            sp.set("ok", bool(result.ok))
            sp.set("exit_code", result.exit_code)
            sp.set("elapsed_ms", result.elapsed_ms)
            if not result.ok:
                sp.set_error(result.error or f"退出码 {result.exit_code}")
    except executor.SandboxUnavailable as e:
        # 沙箱不可用 → **不降级执行**。记审计，如实返回失败。
        write_audit("approval.execute_failed", {
            "approval_id": approval_id, "by": req.by,
            "command": rec["command"], "error": str(e),
        })
        # ★ 失败也要回写真实结果（C4）：否则审批记录里那条乐观的
        #   result_ok=True 会一直挂着，而执行其实没发生。
        _record_execution_result(st, approval_id, ok=False, error=str(e),
                                 by=req.by)
        raise HTTPException(status_code=503, detail=str(e)) from e

    payload = result.to_dict()
    # 写操作到底在哪执行、成没成 —— 这是「它有没有真的动手」的唯一量化口径
    obs_metrics.counter("agentdesk_sandbox_executions_total",
                        {"backend": result.backend,
                         "ok": "true" if result.ok else "false"})
    write_audit("approval.executed", {
        "approval_id": approval_id, "by": req.by,
        "command": rec["command"], "ok": result.ok,
        "isolated": result.isolated, "backend": result.backend,
        "exit_code": result.exit_code, "elapsed_ms": result.elapsed_ms,
    })

    # ★ 把**真实结果**回写进审批单（C4 的修法）。
    #   `consume` 写下的 ok 是执行前的乐观占位（防重放要求它必须先写），
    #   所以真值在这里补上；折叠时以最后一条为准。
    #   **"审批记录写着成功、实际失败"这种不一致，比不记还危险** ——
    #   复盘的人会据此认为那次变更生效了。
    _record_execution_result(st, approval_id, ok=result.ok,
                             exit_code=result.exit_code,
                             elapsed_ms=result.elapsed_ms,
                             error=result.error or "", by=req.by)

    # ★ 执行结果推给值班的人。失败的**更要推**：
    #   approvals.jsonl 里那条 result_ok 是在执行**之前**写下的（见 audit 报告 C4），
    #   所以"审批记录说成功、实际失败"这件事只能靠通知把真值送到人手上。
    notify_events.approval_executed(rec, payload)

    return {"approval": st.get(approval_id), "result": payload}


def _record_execution_result(st, approval_id: str, *, ok: bool,
                             exit_code=None, elapsed_ms=None,
                             error: str = "", by: str = "system") -> None:
    """回写执行结果。**失败不影响返回**（结果已经在审计与响应里了）。"""
    try:
        st.record_result(approval_id, ok=ok, exit_code=exit_code,
                         elapsed_ms=elapsed_ms, error=error, by=by)
    except Exception as e:                     # pragma: no cover
        write_audit("approval.result_writeback_failed", {
            "approval_id": approval_id,
            "error": f"{type(e).__name__}: {e}"})


# ============================================================
# 十二·B、事件（Incident）—— 一个故障一个对象
# ============================================================
# 为什么要有这一层：告警是**现象**，事件是**一次故障**。
# 凌晨一次磁盘写满可能打出 50 条告警，它们是同一个故障的不同侧面。
# 没有事件这一层，就会出现三件事：
#     ① 值班的人收到 50 条通知，然后学会无视通知（告警疲劳）
#     ② 模型被调用 50 次，花 50 份钱，得出 50 个互相矛盾的结论
#     ③ 复盘时没有任何地方能回答"这次故障是谁处理的"
class IncidentAction(BaseModel):
    """事件的认领/结单请求。

    `by` 必填 —— 与审批单"批准必须填审批人"同一条原则：
    **每一次状态变化都要有人负责**。事故复盘时"系统自己结的"不是一个能交差的回答。
    """

    by: str = Field(..., min_length=1, max_length=80,
                    description="操作人（必填）")
    note: str = Field("", max_length=500, description="备注")


def _require_incident(incident_id: str) -> dict:
    """取事件；不存在就 404（而不是把 store 的异常直接漏成 500）。"""
    try:
        return _incident_store().get(incident_id)
    except IncidentError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/incidents")
def list_incidents(
    status: Optional[str] = Query(
        None, description="按状态过滤：open / ack / resolved / reopened"),
    limit: int = Query(50, ge=1, le=200, description="返回最近多少条"),
):
    """事件列表。

    `aggregate` 字段如实报出当前是"按主机+服务聚合"还是"旧的精确去重" ——
    免得看的人以为开了聚合、其实配置里关着。
    """
    st = _incident_store()
    return {
        "items": st.list(status=status, limit=limit),
        "counts": st.counts(),
        "aggregate": _ALERT_AGG.describe(),
        "notify": notify_events.describe(),
    }


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    """单个事件详情（含完整时间线与成员告警）。"""
    return _require_incident(incident_id)


@app.post("/incidents/{incident_id}/ack")
def ack_incident(incident_id: str, req: IncidentAction):
    """认领事件：**谁在处理**这件事必须有名字。

    先取一次（404 判定），再迁移（状态不合法 → 400）。
    两步分开是为了让"这个 id 不存在"和"这个操作现在不允许"返回不同的状态码 ——
    调用方能据此决定是刷新列表还是改操作。
    """
    _require_incident(incident_id)
    try:
        rec = _incident_store().ack(incident_id, by=req.by, note=req.note)
    except IncidentError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    write_audit("incident.ack", {"incident_id": incident_id, "by": req.by,
                                 "note": req.note, "status": rec.get("status")})
    return rec


@app.post("/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, req: IncidentAction):
    """结单：谁结的 + 为什么结（note 就是复盘的第一手材料）。"""
    _require_incident(incident_id)
    try:
        rec = _incident_store().resolve(incident_id, by=req.by, note=req.note)
    except IncidentError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    write_audit("incident.resolve", {"incident_id": incident_id, "by": req.by,
                                     "note": req.note, "status": rec.get("status")})
    # 结单也通知一声：处置完成是值班场景里最该被看见的一条状态变化
    notify_events.send(
        f"[AgentDesk] 事件 {incident_id} 已结单",
        f"**已结单**\n\n- 事件：`{incident_id}`\n- 处理人：{req.by}\n"
        f"- 说明：{req.note or '（无）'}",
        key=f"incident-resolved:{incident_id}",
        payload={"incident_id": incident_id, "by": req.by})
    return rec


# ============================================================
# 十三、可观测查询接口
# ============================================================
# 这三个接口回答运维中最值得关注的三类问题：
#     GET /traces           "刚才那次运行到底发生了什么？"（逐步轨迹）
#     GET /traces/{id}      "这一步为什么慢/为什么错？"（单次详情）
#     GET /metrics/summary  "钱花在哪了？哪一步最慢？"（聚合看板）
#
# ★ 它们读的是本地 logs/traces.jsonl —— 不依赖任何外部服务。
#   配了 Langfuse 只是"多了一个可视化的面板"，不是"唯一的数据源"。
class _NoBody(BaseModel):
    pass


@app.get("/traces")
def list_traces(limit: int = Query(20, ge=1, le=100)):
    """最近的 trace 列表（不含 span 明细）。"""
    items = obs.recent_traces(limit=limit)
    # 列表视图不带 spans —— 一条 trace 几十个 span，列表页会被撑爆
    for it in items:
        it.pop("spans", None)
    return {
        "count": len(items),
        "langfuse": langfuse_export.describe(),
        "export_failures": obs.export_failures(),
        "items": items,
    }


@app.get("/traces/{trace_id}")
def get_trace(trace_id: str):
    """单条 trace 的完整详情（含全部 span）。

    排障时的用法：/traces 看到某次运行慢 → 拿 trace_id 来这里 →
    按 elapsed_ms 排序找最慢的 span → 它就是瓶颈。
    """
    detail = obs.trace_detail(trace_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"没有这条 trace：{trace_id}")
    return detail


@app.get("/metrics/summary")
def metrics_summary(limit: int = Query(50, ge=1, le=500,
                                       description="聚合最近多少次 trace")):
    """成本与性能的聚合看板。

    ★ 这个接口是「用数据驱动优化」的落点。它至少能回答：
      - 平均一次运行花多少钱、多少 token
      - 成本按 Agent（intent/knowledge/diagnose/verify/remediate）怎么分布
      - 成本按工具（check_disk / run_command…）怎么分布
      - P95 延迟在哪、错误率多少
      - DeepSeek 缓存命中率（命中率低 = system prompt 每次都在变，白花钱）
    """
    stats = {}
    traces = obs.recent_traces(limit=limit, stats=stats)
    report = obs_costs.aggregate(traces)
    report["langfuse"] = langfuse_export.describe()
    report["export_failures"] = obs.export_failures()
    report["store"] = {
        "path": _display_path(obs.TRACE_PATH),
        "exists": obs.TRACE_PATH.exists(),
    }
    # ★ 如实交代"这份报告统计的是什么"。
    #   起因：这个接口曾经报 errors=8，而真实运行一次错误都没有 ——
    #   8 条全是自检脚本每次都要造的错误 trace。计数类指标被污染到 100%，
    #   金额却看不出问题（自检记录花的钱很少）。**所以光看总成本发现不了。**
    report["scope"] = {
        "window_requested": limit,
        "live_runs": len(traces),
        "records_scanned": stats.get("scanned", 0),
        "selftest_excluded": stats.get("excluded_selftest", 0),
        "live_available": stats.get("live_available", len(traces)),
        "note": ("只统计真实运行（source=live）。自检脚本 scripts/smoke_test.py "
                 "写的记录被排除 —— 它们用来验证「记录机制对不对」，"
                 "不能代表「系统运行状况」。"
                 "records_scanned = live_available + selftest_excluded，"
                 "live_runs 是其中被窗口取用的部分（min(live_available, limit)）。"),
        # ★★ 这一条是用户逼出来的。他看到「最近运行」列表后直接说
        #   「最近运行应该不是真的」—— 他是对的：那 49 条里，
        #   同一句健康检查重复 14 次、同一句验证问句重复 7 次，
        #   全是我调试这个项目时产生的，没有一条是真实用户提问。
        #
        #   而**服务端没有能力区分"真实用户"和"开发者"** ——
        #   同一个接口、同一份凭据、同一种请求格式，HTTP 层面看不出区别。
        #   所以这里不假装能区分，只把已知的口径讲清楚，
        #   并给出能自己说话的旁证（distinct_questions）。
        "caveat": ("「运行次数」= 接口被调用的次数，不等于「被多少真实用户用过」。"
                   "服务端无法区分真实用户与开发者调试（同一接口、同一凭据、"
                   "同一请求格式），所以判断方法是看问题的多样性："
                   "runs 明显大于 distinct_questions，就是在反复测同一件事。"
                   "当前这批记录来自开发调试（含评测批处理与告警测试），"
                   "不代表有人在使用这个服务。"),
    }
    return report
