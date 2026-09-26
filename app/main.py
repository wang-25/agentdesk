# -*- coding: utf-8 -*-
"""
AgentDesk 服务入口
============================================================
把模型能力做成一个别人能调用的 HTTP 服务 —— 这是「工具」变成「服务」的那一步。

【运行方式】在 agentdesk 目录下执行：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000

【四个接口】
    GET  /health        健康检查，确认服务活着
    POST /chat          问答，一次性返回
    POST /chat/stream   问答，流式返回（SSE，字一个个蹦）
    POST /parse         意图解析，把一句人话转成结构化 JSON

【自带交互式文档】服务起来之后打开 http://127.0.0.1:8000/docs
    FastAPI 会自动生成接口文档和"试用"按钮，不用写前端就能点着测。
    这个页面本身就是交付物的一部分，面试演示时可以直接打开。

【下面有三处标了 ★★ 是留给你的练习，不影响服务运行，做完再往下走】
"""

import json
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.llm import ModelError
from app.llm import chat as llm_chat
from app.llm import chat_json, chat_stream, parse_json_reply

# ============================================================
# 应用与数据模型
# ============================================================
app = FastAPI(
    title="AgentDesk",
    description="面向运维场景的多 Agent 智能体系统",
    version="0.1.0",
)


class ChatRequest(BaseModel):
    """请求体。pydantic 会自动做校验 —— 字段缺失或类型不对会直接返回 422，
    我们的业务代码里不用写任何 if 判断。"""

    question: str = Field(..., min_length=1, max_length=2000,
                          description="用户的提问")


class ChatResponse(BaseModel):
    answer: str


# ============================================================
# 意图解析的 Prompt
# ============================================================
# 这段是从 practice/day1/d1_05_llm_json.py 搬过来的，一字未改。
# 搬迁本身就是一次「把实验代码正式化」的过程：
# 在练习里它是写死在函数里的，现在它成了服务的一个组成部分。
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


def validate_intent(data: dict):
    """检查意图解析结果。返回 (是否通过, 问题列表)。"""
    problems = []
    for field in REQUIRED_FIELDS:
        if field not in data:
            problems.append(f"缺少字段: {field}")
    if "risk" in data and data["risk"] not in ALLOWED_RISK:
        problems.append(f"risk 取值不合法: {data['risk']!r}")
    return (len(problems) == 0, problems)


# ============================================================
# 接口 1：健康检查
# ============================================================
@app.get("/health")
async def health():
    """部署和监控系统靠这个接口判断服务是否活着。

    别小看它 —— 你后面要用 Docker + Nginx 上线，
    容器的健康检查、负载均衡的存活探测全都依赖这个接口。
    """
    return {"status": "ok", "service": "agentdesk", "version": "0.1.0"}


# ============================================================
# 接口 2：问答（非流式）
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
        # ★ 这里体现了「异常翻译」的价值：
        # 模型层出问题 → 502 Bad Gateway（上游服务故障）
        # 而如果是用户参数不对 → 422（FastAPI 自动处理）
        # 调用方看状态码就知道该找谁。
        raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(answer=answer)


# ============================================================
# 接口 3：问答（流式 SSE）
# ============================================================
@app.post("/chat/stream")
def chat_stream_endpoint(req: ChatRequest):
    """流式返回，字一个个蹦出来。

    【为什么这个接口用 def 而不是 async def】
    下面的 gen() 是同步生成器（内部用同步 httpx）。
    如果写成 async def，这个同步生成器会在事件循环里跑，
    把整个服务卡住 —— 一个用户提问，其他人都得等。
    写成 def，FastAPI 会把它丢进线程池，不阻塞事件循环。

    ★★ 练习 1：把它改成异步版本
       在 app/llm.py 里加一个 chat_stream_async（用 httpx.AsyncClient），
       然后把这里改成 async def + async 生成器。
       改完用两个终端同时 curl 两个请求，能感觉到差别。

    【SSE 是什么】
    Server-Sent Events：服务端持续往同一个 HTTP 连接里推数据。
    格式很土，每块数据就一行，必须形如：
        data: 你要发送的内容
        （空行表示这一块结束）
    浏览器端的 EventSource 就是按这个格式解析的。
    """

    def gen():
        messages = [
            {"role": "system", "content": "你是一个简洁的运维助手，回答不超过 100 字。"},
            {"role": "user", "content": req.question},
        ]
        try:
            for piece in chat_stream(messages):
                # ensure_ascii=False 让中文原样输出，
                # 否则会变成 \u4f60\u597d 这种（能用但没法读）
                payload = json.dumps({"delta": piece}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except ModelError as e:
            # 流已经开始了就没法改状态码了，只能把错误当成一条数据推给客户端。
            # 这是流式接口的一个固有难点，也是你以后被面试官问到的点。
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        # 约定俗成的结束标记，客户端看到它就知道流结束了
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 接口 4：意图解析
# ============================================================
@app.post("/parse")
def parse_endpoint(req: ChatRequest):
    """把一句人话解析成程序能读的结构化 JSON。

    这个接口是 Day 9 那个「意图路由 Agent」的内核，
    现在先以一个普通接口的形式跑起来。
    """
    try:
        data = chat_json([
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": req.question},
        ])
    except ModelError as e:
        raise HTTPException(status_code=502, detail=str(e))

    ok, problems = validate_intent(data)
    if not ok:
        # 422 Unprocessable Entity：请求本身没问题，是模型给的答案不合格。
        # 把原始返回一起带上，方便排查是 Prompt 的问题还是模型的问题。
        raise HTTPException(status_code=422,
                            detail={"message": "模型返回的字段不合格", "problems": problems})

    # ★★ 练习 2：加一个「自动重试」
    #     校验失败时，把问题拼成一句话追加到 messages 里再问一次，
    #     最多重试 2 次。这是真实项目里提高结构化输出成功率的标准做法。

    return data


# ============================================================
# 接口 5：告警驱动的入口（只留了骨架）
# ============================================================
@app.post("/webhook/alert")
def webhook_alert(payload: dict):
    """接收告警系统推送的事件，触发自动诊断。

    【为什么要有这个接口】
    通用 AI 助手必须有人在旁边打字才能干活。
    而这个接口是给 Alertmanager 这类告警系统调的 —— 凌晨三点你在睡觉，
    告警响了它自己起来诊断、自己给出结论。

    「无人值守」是 Agent 服务和通用助手最本质的差别。

    ★★ 练习 3：把它实现出来
       1. 从 payload 里取出告警名和主机名
       2. 调 /parse 同样的逻辑判断意图
       3. 如果 risk 是 low，直接跑诊断；是 high，返回"需要人工确认"
       4. 返回一份诊断结论

    【现在的状态】先返回 501，表示"接口存在但还没实现"。
    用 501 而不是 404，是为了让调用方知道"不是地址写错了"。
    """
    # 记一笔日志，方便你验证 webhook 有没有被调通
    print(f"[webhook] 收到告警事件: {json.dumps(payload, ensure_ascii=False)[:200]}")
    print(f"[webhook] 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    raise HTTPException(
        status_code=501,
        detail="告警自动诊断尚未实现（这是 Day 3 的练习）",
    )
