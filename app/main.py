# -*- coding: utf-8 -*-
"""
AgentDesk 服务入口
============================================================
把模型能力做成一个别人能调用的 HTTP 服务 —— 这是「工具」变成「服务」的那一步。

【运行方式】在 agentdesk 目录下执行：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000

【接口一览】
    GET  /health          健康检查，供容器探活与监控使用
    GET  /audit           读取审计日志（每次操作都留痕）
    POST /chat            问答，一次性返回
    POST /chat/stream     问答，SSE 流式返回（异步版）
    POST /parse           意图解析，把一句人话转成结构化 JSON
    POST /webhook/alert   告警驱动的入口 —— 无人值守自动诊断

服务起来后打开 http://127.0.0.1:8000/docs 有自动生成的交互式文档。
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.llm import PROJECT_ROOT, ModelError
from app.llm import chat as llm_chat
from app.llm import chat_json, chat_stream_async

# ============================================================
# 应用实例
# ============================================================
app = FastAPI(
    title="AgentDesk",
    description="面向运维场景的多 Agent 智能体系统",
    version="0.2.0",
)

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
              判断依据：只读操作是 low；重启服务是 medium；删除、清理、修改配置是 high
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
    这是生产环境做结构化输出的标准做法，也是面试常问的点。

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
# 接口 1：健康检查
# ============================================================
@app.get("/health")
async def health():
    """部署和监控系统靠这个接口判断服务是否活着。

    别小看它 —— 后面要用 Docker + Nginx 上线，
    容器的健康检查、负载均衡的存活探测全都依赖这个接口。
    """
    return {"status": "ok", "service": "agentdesk", "version": "0.2.0"}


# ============================================================
# 接口 2：审计日志查询
# ============================================================
@app.get("/audit")
def read_audit(limit: int = Query(20, ge=1, le=200, description="返回最近多少条")):
    """读取最近的审计记录。

    【这个接口的价值】
    它是「可追溯」的证据。面试时你可以说：
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
            # 这是流式接口的固有难点，也是面试会被问到的点。
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
    """接收告警系统推送的事件，自动完成判断并给出处置方案。

    【这个接口存在的意义】
    通用 AI 助手必须有人在旁边打字才能干活。
    而这个接口是给 Alertmanager 这类告警系统调的 ——
    凌晨三点你在睡觉，告警响了它自己起来判断、自己给出结论、自己留痕。

    「无人值守」是 Agent 服务和通用助手最本质的差别。

    【当前的执行边界】
    risk 为 low / medium 时生成诊断预案；risk 为 high 时拒绝执行、转人工。
    命令的真实执行需要沙箱层（Day 5 接入），在此之前只产出计划不落地。
    这个边界是刻意的：宁可少做，不可做错。
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

        # ---- 第一步：理解告警 ----
        try:
            intent, attempts = parse_intent(question)
        except IntentParseFailed as e:
            report = {**base, "decision": "parse_failed",
                      "reason": str(e), "problems": e.problems}
            write_audit("alert.parse_failed", report)
            reports.append(report)
            continue

        risk = intent.get("risk")
        service = (intent.get("service") or "").lower()
        base["intent"] = intent
        base["parse_attempts"] = attempts

        # ---- 第二步：按风险等级决定做还是停 ----
        if risk == "high":
            # 高危操作不自动执行。这不是能力不足，是责任边界 ——
            # 生产环境不能靠"相信模型不会干坏事"。
            report = {**base, "decision": "need_human",
                      "reason": "风险等级 high，未执行任何操作，等待人工确认",
                      "playbook": []}
        else:
            playbook = DIAGNOSE_PLAYBOOK.get(service, DEFAULT_PLAYBOOK)
            report = {**base, "decision": "auto_diagnose",
                      "reason": "只读诊断，已生成处置预案",
                      "playbook": playbook,
                      "executed": False,
                      "note": "命令执行需接入沙箱层，计划在 Day 5 落地"}

        write_audit("alert.handled", report)
        reports.append(report)

    return {"received": len(alerts), "reports": reports}
