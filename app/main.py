# -*- coding: utf-8 -*-
"""
AgentDesk 服务入口
============================================================
把模型能力做成一个别人能调用的 HTTP 服务 —— 这是「工具」变成「服务」的那一步。

【运行方式】在 agentdesk 目录下执行：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000

【接口一览】共 24 个业务路由，其中 22 个进 OpenAPI 文档。
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

  页面（不进 OpenAPI）
    GET  /                          导航页：这是什么、从哪开始试
    GET  /try                       在线试用页：填问题、点按钮

服务起来后打开 http://127.0.0.1:8000/docs 有自动生成的交互式文档。
"""

import json
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.llm import PROJECT_ROOT, ModelError
from app.observability import costs as obs_costs
from app.observability import langfuse_export, tracer as obs

# 配了 LANGFUSE_* 环境变量才生效；没配就是本地记录模式，功能不受影响
langfuse_export.install()
from app import security
from app.llm import chat as llm_chat
from app.llm import chat_json, chat_stream_async

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


def write_audit(event: str, detail: dict) -> dict:
    """追加一条审计记录，返回写入的内容。"""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **detail,
    }
    # 用 "a" 模式追加，每条一行
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


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
    """检查意图解析结果。返回 (是否通过, 问题列表)。"""
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
# 告警归一化与处置预案
# ============================================================
# 不同告警系统推过来的格式不一样。统一成同一种结构，
# 后面的逻辑就不用关心数据是从哪来的 —— 这叫「归一化」。

# ★ 是否让告警**真的**触发一轮 Agent 诊断。**默认关闭。**
#
#   为什么默认关：开启后每条告警都要跑一次完整的模型链路（实测 6–20 秒、
#   几分钱）。"自动"是有价的 —— 凌晨网络抖一下推 500 条告警，
#   就是 500 次诊断。所以在能力打开的同时，必须带上下面两道闸：
#
#     ① 去重：同名 + 同机的告警，窗口内只处理一次
#     ② 频控：每小时最多自动诊断 N 次，超了转人工
#
#   **这两道闸必须挡在调模型之前**，否则被抑制的那部分告警也在悄悄烧钱，
#   那才是真正的"告警风暴"。
ALERT_AUTO_DIAGNOSE = (os.getenv("ALERT_AUTO_DIAGNOSE", "0").strip() == "1")
ALERT_DEDUP_SECONDS = int(os.getenv("ALERT_DEDUP_SECONDS", "600"))
ALERT_MAX_PER_HOUR = int(os.getenv("ALERT_MAX_PER_HOUR", "10"))

# 去重与频控的计数（进程内内存态，与限流层同一种取舍：重启即清零）。
# 单进程部署下准确；多实例需要换成 Redis —— 已知边界，不隐藏。
_ALERT_LAST_SEEN = {}                      # (alertname, host) -> 上次处理的时间戳
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


def normalize_alerts(payload: dict) -> list:
    """把告警统一成同一种结构。

    兼容两种输入：
      1. Alertmanager 标准格式（顶层有 alerts 数组，每个元素带 labels / annotations）
      2. 最简单的扁平格式（直接给 alertname / host / summary）
    """
    raw_list = payload.get("alerts") or [payload]
    result = []
    for raw in raw_list:
        labels = raw.get("labels") or {}
        annotations = raw.get("annotations") or {}

        # instance 常带端口（web-01:9100），主机名只取冒号前面那段
        instance = (labels.get("instance") or raw.get("host")
                    or raw.get("instance") or "")
        host = instance.split(":")[0] if instance else None

        result.append({
            "alertname": (labels.get("alertname") or raw.get("alertname")
                          or "UnknownAlert"),
            "severity": (labels.get("severity") or raw.get("severity")
                         or "unknown"),
            "instance": instance,
            "host": host,
            "service": labels.get("service") or raw.get("service"),
            "summary": annotations.get("summary") or raw.get("summary") or "",
            "description": (annotations.get("description")
                            or raw.get("description") or ""),
            "status": raw.get("status") or payload.get("status") or "firing",
        })
    return result


def alert_to_question(alert: dict) -> str:
    """把告警拼成一句自然语言，交给意图解析器去理解。

    【为什么不直接按字段规则判断，而要绕一圈用模型？】
    规则判断只能处理你预先想到的情况。而告警名是千奇百怪的
    （NginxHighErrorRate、DiskSpaceLow、ServiceDown……），
    写规则永远补不完。让模型理解语义，是把「穷举」换成「理解」。
    """
    parts = [f"收到告警：{alert['alertname']}"]
    if alert.get("host"):
        parts.append(f"主机：{alert['host']}")
    if alert.get("service"):
        parts.append(f"服务：{alert['service']}")
    if alert.get("severity") and alert["severity"] != "unknown":
        parts.append(f"级别：{alert['severity']}")
    if alert.get("summary"):
        parts.append(f"摘要：{alert['summary']}")
    if alert.get("description"):
        parts.append(f"详情：{alert['description']}")
    parts.append("请判断应该采取什么动作，以及风险等级。")
    return "；".join(parts)


# ============================================================
# 接口 0：首页
# ============================================================
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
            '除 <code>/</code>、<code>/health</code>、<code>/docs</code>、<code>/try</code> '
            '之外，所有接口都要求请求头带令牌（<code>X-API-Key</code>）。'
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
        ("/docs", "22 个接口的调试台", docs_hint),
        ("/metrics/summary", "成本看板", "按 Agent / 动作两维看成本、缓存命中率、P95 延迟"),
        ("/traces", "链路追踪", "每次运行发生了什么、哪一步最慢最贵"),
        ("/agent/tools", "工具清单", "Agent 能调用的 7 个工具，含风险等级"),
        ("/sandbox", "沙箱状态", "当前是 mock 还是真容器、白名单概览、fail-closed"),
        ("/approvals", "审批单", "Agent 想执行但还没执行的写操作"),
        ("/audit", "审计日志", "谁、何时、哪条告警、判成什么风险、做了什么"),
        ("/health", "健康检查", "不需要 token，给容器探活和监控用"),
    ]
    rows = "".join(
        f'<tr><td><a href="{u}"><code>{u}</code></a></td>'
        f'<td><b>{n}</b></td><td>{d}</td></tr>'
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
<tr><th>地址</th><th>是什么</th><th>说明</th></tr>
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
都不会自动执行。它们会变成一张审批单挂在 <a href="/approvals">/approvals</a>，
等人批准；沙箱不可用时宁可拒绝执行，也不降级。</div>

<p style="margin-top:32px;color:#8b949e;font-size:13px">
这个实例跑在阿里云一台 2 核 2G 的机器上，与 WordPress、Zabbix 共 8 个容器共存，
经 Nginx Proxy Manager 提供 HTTPS。<br>
{acl_block}
部署过程与安全设计见仓库的 <code>docs/deployment.md</code>。</p>
</body></html>"""

    # HTML 一律不缓存 —— 和 /try 页同一个理由（服务端渲染、内容随配置变）。
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
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
</style></head><body>

<h1>AgentDesk 在线试用</h1>
<div class="sub">多 Agent 智能体系统 &middot; 面向运维场景 &middot;
填问题、点按钮，看它自己决定查什么</div>

<div class="note" id="authnote">__AUTH_NOTE__</div>

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

    html = (
        html.replace("__AUTH_NOTE__", auth_note)
        .replace("__TOKCARD_STYLE__", tokcard_style)
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
# 接口 2：审计日志查询
# ============================================================
@app.get("/audit")
def read_audit(limit: int = Query(20, ge=1, le=200, description="返回最近多少条")):
    """读取最近的审计记录。

    【这个接口的价值】
    它是「可追溯」的证据。可以这样描述：
    「每一次告警触发的判断和决策都落库了，我能查出来三小时前那次为什么没自动处理。」
    这句话是通用 AI 助手给不了的 —— 它有聊天记录，但没有结构化审计。
    """
    if not AUDIT_LOG.exists():
        return {"total": 0, "items": []}

    lines = [l for l in AUDIT_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue    # 坏行跳过，不要因为一行坏了整个接口报错
    return {"total": len(lines), "items": items}


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
def webhook_alert(payload: dict):
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

    for alert in alerts:
        question = alert_to_question(alert)
        base = {
            "alertname": alert["alertname"],
            "host": alert.get("host"),
            "severity": alert["severity"],
        }

        # ---- 第 0 步：去重 + 频控，两道闸都挡在"花钱"之前 ----
        # ★ 顺序是关键：必须在 parse_intent（调模型）**之前**判。
        #   放在之后的话，被抑制的那部分告警也已经为每个 token 付过钱了 ——
        #   抑制的意义在于不花钱，不在于不返回。
        key = (alert["alertname"], alert.get("host"))
        now = time.time()
        last = _ALERT_LAST_SEEN.get(key)
        if last is not None and (now - last) < ALERT_DEDUP_SECONDS:
            report = {**base, "decision": "suppressed_duplicate",
                      "reason": (f"{ALERT_DEDUP_SECONDS}s 内已处理过同名同机的告警，"
                                 f"本次跳过（不重复花钱）")}
            write_audit("alert.suppressed", report)
            reports.append(report)
            continue
        _ALERT_LAST_SEEN[key] = now

        hour = time.strftime("%Y%m%d%H", time.localtime(now))
        if _ALERT_HOUR["hour"] != hour:
            _ALERT_HOUR["hour"] = hour
            _ALERT_HOUR["count"] = 0
        if _ALERT_HOUR["count"] >= ALERT_MAX_PER_HOUR:
            report = {**base, "decision": "rate_limited",
                      "reason": (f"本小时自动诊断已达上限 {ALERT_MAX_PER_HOUR} 次，"
                                 f"本次交给人工。调大 ALERT_MAX_PER_HOUR 可放宽")}
            write_audit("alert.rate_limited", report)
            reports.append(report)
            continue
        _ALERT_HOUR["count"] += 1

        # ---- 第一步：理解告警 ----
        try:
            intent, attempts = parse_intent(question)
        except IntentParseFailed as e:
            report = {**base, "decision": "parse_failed",
                      "reason": str(e), "problems": e.problems}
            write_audit("alert.parse_failed", report)
            reports.append(report)
            continue

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

        write_audit("alert.handled", report)
        reports.append(report)

    return {"received": len(alerts), "reports": reports}


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

    # 审计留痕：Agent 自己做了决定这件事，必须可追溯。
    # 记的是"它查了什么"，而不是"它答了什么" —— 后者可以从日志里再取，
    # 前者才是排查"它为什么这么判断"的关键。
    write_audit("agent.ask", {
        "question": req.question,
        "engine": req.engine,
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
    if req.engine == "supervisor":
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

    if req.include_trace:
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

    # 先取出来 —— 需要它的 command 去重新决策（拿到执行用的 argv）
    try:
        rec = st.get(approval_id)
    except approvals.ApprovalError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if rec["status"] != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"审批单状态是 {rec['status']}，必须先批准才能执行"
                   f"（当前状态：{rec['status']}）")
    if rec.get("consumed_at"):
        raise HTTPException(
            status_code=409,
            detail=f"这张审批单已在 {rec['consumed_at']} 执行过，不能重复执行")

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

    # 指纹比对（防 TOCTOU）。用当前决策算出的指纹去对审批时记下的。
    if decision.fingerprint != rec.get("fingerprint"):
        raise HTTPException(
            status_code=409,
            detail=f"命令指纹不匹配，拒绝执行。"
                   f"审批时：{rec.get('fingerprint')}，"
                   f"现在：{decision.fingerprint}")

    try:
        # 先消费（占位），再执行 —— 顺序很重要。
        #
        # 反过来的话：执行成功但消费失败（比如写日志时崩了），
        # 这张单子会停留在 approved，下次还能再执行一次。
        # **宁可出现"已标记消费但执行失败"，也不要出现"执行成功还能再执行"。**
        # 前者是少做了一次（人能看到错误），后者是重复做（可能造成事故）。
        st.consume(approval_id, expected_fingerprint=decision.fingerprint,
                   by=req.by, ok=True)
    except approvals.ApprovalError as e:
        raise HTTPException(status_code=409, detail=str(e))

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
        raise HTTPException(status_code=503, detail=str(e))

    payload = result.to_dict()
    write_audit("approval.executed", {
        "approval_id": approval_id, "by": req.by,
        "command": rec["command"], "ok": result.ok,
        "isolated": result.isolated, "backend": result.backend,
        "exit_code": result.exit_code, "elapsed_ms": result.elapsed_ms,
    })
    return {"approval": st.get(approval_id), "result": payload}


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
    traces = obs.recent_traces(limit=limit)
    report = obs_costs.aggregate(traces)
    report["langfuse"] = langfuse_export.describe()
    report["export_failures"] = obs.export_failures()
    report["store"] = {
        "path": str(obs.TRACE_PATH.relative_to(PROJECT_ROOT)),
        "exists": obs.TRACE_PATH.exists(),
    }
    return report
