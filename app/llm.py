# -*- coding: utf-8 -*-
"""
模型调用的统一入口
============================================================
全项目所有和大模型打交道的地方，都只经过这一个文件。

【为什么必须统一入口】
如果每个模块各写各的 httpx.post，等你想换模型、加缓存、加重试、
加成本统计的时候，就要改十几个地方 —— 改漏一个就是一个线上 bug。
统一入口之后，这些能力只要在这里加一次，全项目都受益。

本文件提供五个函数：
    chat                  一次性返回，只给文本
    chat_step             一步请求，返回完整 message（含 tool_calls）—— Agent 循环用
    chat_stream           流式返回（同步生成器）
    chat_stream_async     流式返回（异步生成器，高并发时用）
    chat_json             要模型返回 JSON 的便捷方法
"""

import json
import os
from pathlib import Path
from typing import AsyncIterator, Iterator

import httpx
from dotenv import load_dotenv

# 本文件在 agentdesk/app/ 下，parents[1] 就是项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class ModelError(Exception):
    """模型层错误。

    【为什么要自定义异常】
    调用方需要能区分"是模型这一层出问题了"还是"是文件/参数出问题了" ——
    前者该返回 502 并提示重试，后者该返回 400 让用户改输入。
    如果都抛 Exception，调用方就只能靠猜。
    """


# 两家都用 OpenAI 兼容格式，所以地址和模型名不同，其他代码完全一样
PROVIDERS = {
    "deepseek": {
        "env_key": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
    },
    "dashscope": {
        "env_key": "DASHSCOPE_API_KEY",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-plus",
    },
}


def _pick():
    """选出 .env 里配了 Key 的那家服务商。"""
    for name, cfg in PROVIDERS.items():
        key = (os.getenv(cfg["env_key"]) or "").strip()
        if key:
            return name, cfg, key
    raise ModelError("没有找到 API Key，请检查项目根目录的 .env 文件")


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# ============================================================
# 一、SSE 数据行的解析（同步流和异步流共用）
# ============================================================
# 流结束标记。单独定义成常量，避免在多个地方硬编码字符串写错。
SSE_DONE = "__SSE_DONE__"


def extract_delta(line: str):
    """从一行 SSE 文本里取出文本片段。

    返回：
        None      这行不是数据（空行、注释行、半截 JSON），跳过
        SSE_DONE  流结束
        其他字符串  模型这次吐出的文本片段

    【为什么单独抽成一个函数】
    解析逻辑有二十来行，同步版和异步版都要用。
    复制一份的话，以后要改解析规则就得改两处 —— 迟早会忘掉一处。
    抽出来两边共用，这是消除重复的标准做法。
    """
    # SSE 协议里，数据行必须以 "data:" 开头，其他行（空行、注释）忽略
    if not line or not line.startswith("data:"):
        return None

    payload = line[5:].strip()      # 去掉 "data:" 这 5 个字符

    if payload == "[DONE]":
        return SSE_DONE

    # 网络分包时可能收到半截 JSON，解析不了就跳过这一块，
    # 不要因为一行坏了就把整个流断掉
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        return None

    # 取值路径：choices[0].delta.content
    # 有些块只带角色信息不带内容（比如流的第一块），所以要 or None
    return chunk.get("choices", [{}])[0].get("delta", {}).get("content") or None


def _build_stream_payload(cfg, messages, temperature):
    return {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }


# ============================================================
# 二、一次性返回（非流式）
# ============================================================
def chat(messages, temperature=0.7, timeout=60) -> str:
    """发一次对话请求，等模型把整段话说完再返回。

    messages 形如 [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
    """
    _, cfg, api_key = _pick()

    # ★ 观测从这里开始（Day 8）。span 挂在统一入口 = 所有调用方自动被记录，
    #   而不是每个业务函数自己记 —— 和"统一入口"是同一个原则。
    #   没有活跃 trace 时 span 内部静默跳过（见 tracer.py 的 _NullSpan）。
    from app.observability import tracer
    with tracer.span(tracer.TYPE_LLM, name="chat",
                     model=cfg["model"], temperature=temperature) as sp:
        try:
            resp = httpx.post(
                cfg["url"],
                headers=_headers(api_key),
                json={"model": cfg["model"], "messages": messages,
                      "temperature": temperature},
                timeout=timeout,
            )
        except httpx.ConnectError as e:
            # 把底层网络异常翻译成我们自己的异常类型。
            # 为什么要翻译？因为上层不应该需要知道 httpx 的存在 ——
            # 万一哪天换成别的 HTTP 库，上层代码不用改。
            sp.set_error(e)
            raise ModelError(f"连不上模型服务：{e}") from e
        except httpx.TimeoutException as e:
            sp.set_error("timeout")
            raise ModelError(f"请求超时（{timeout}s）") from e

        if resp.status_code != 200:
            sp.set_error(f"HTTP {resp.status_code}")
            raise ModelError(f"请求失败 HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        sp.set_usage(data.get("usage") or {})
        return data["choices"][0]["message"]["content"]


# ============================================================
# 三、工具调用 —— 带 tools 参数的一步
# ============================================================
def chat_step(messages, tools=None, temperature=0, timeout=90) -> dict:
    """发一次请求，返回**完整的 assistant message** 与用量。

    【和 chat() 的区别】
    chat() 只把文本内容抠出来给你 —— 适合"问一句答一句"。
    chat_step() 把整个 message 对象原样返回。为什么？

    因为开了工具调用之后，message 里除了 content 还会多一个
    tool_calls 字段。只取 content 等于把"模型想调工具"这个意图丢掉了 ——
    而 Agent 循环的全部意义就在这个意图上。

    【为什么必须原样返回、原样追加】
    ReAct 循环要把这个 message 追加进对话历史，而且下一轮的 tool 消息
    必须靠 tool_call_id 和它对应起来。
    如果你自己拼一个 {"role": "assistant", "content": ...} 塞回去，
    id 和 tool_calls 就丢了，模型下一轮会以为那条工具结果是别人给的，
    轻则重复调用，重则直接报错。

    【temperature 默认 0】
    工具调用需要的是"稳定决策"，不是"文采"。同样的故障描述每次都该调同一批工具，
    否则评测根本没法复现。

    返回值：
        {"message": {...}, "finish_reason": str, "usage": {...}, "model": str}
    """
    _, cfg, api_key = _pick()
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": temperature}
    if tools:
        payload["tools"] = tools
        # tool_choice="auto" 表示"调不调、调哪个，模型自己定"。
        # 另外两个取值："none"（强制不调）、"required"（强制必须调一个），
        # 评测时经常用 required 来测"它到底会不会选工具"。
        payload["tool_choice"] = "auto"

    # ★ 观测（Day 8）：Agent 循环里**每一次**模型调用都是一个 span。
    #   没有活跃 trace 时静默跳过。
    from app.observability import tracer
    with tracer.span(tracer.TYPE_LLM, name="chat_step",
                     model=cfg["model"],
                     tools_count=len(tools or []),
                     messages_count=len(messages)) as sp:
        try:
            resp = httpx.post(cfg["url"], headers=_headers(api_key), json=payload,
                              timeout=timeout)
        except httpx.ConnectError as e:
            sp.set_error(e)
            raise ModelError(f"连不上模型服务：{e}") from e
        except httpx.TimeoutException as e:
            sp.set_error("timeout")
            raise ModelError(f"请求超时（{timeout}s）") from e

        if resp.status_code != 200:
            sp.set_error(f"HTTP {resp.status_code}")
            raise ModelError(f"请求失败 HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choice = data["choices"][0]
        out = {
            "message": choice["message"],
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage") or {},
            "model": data.get("model") or cfg["model"],
        }
        sp.set_usage(out["usage"])
        # 这个分支有没有产生工具调用，是 Agent 行为分析最有用的一个维度
        sp.set("made_tool_calls", bool(choice["message"].get("tool_calls")))
        return out


# ============================================================
# 四、流式返回 —— 同步版
# ============================================================
def chat_stream(messages, temperature=0.7, timeout=120) -> Iterator[str]:
    """逐块返回模型输出，每 yield 一小段文本。

    【为什么需要流式】
    非流式要等模型说完最后一句话才返回，用户干等十几秒，感觉像卡死了。
    流式让字一个个蹦出来，首字延迟从十几秒降到一秒内 ——
    用户体验的差别是天壤之别，而且实现成本很低。

    【什么时候用同步版、什么时候用异步版】
    同步版内部会阻塞线程。FastAPI 会把 def 接口丢进线程池，
    所以少量并发没问题；但线程池有上限（默认 40），
    并发再高就会排队 —— 这时必须换异步版。
    """
    _, cfg, api_key = _pick()

    # 必须用 httpx.stream(...) 而不是 httpx.post(...)，
    # 因为前者不会等响应体完整下载完，可以边下边读。
    with httpx.stream(
        "POST", cfg["url"],
        headers=_headers(api_key),
        json=_build_stream_payload(cfg, messages, temperature),
        timeout=timeout,
    ) as resp:

        if resp.status_code != 200:
            # 出错时响应体还没读完，要显式 read() 才能拿到内容
            detail = resp.read().decode("utf-8", errors="replace")[:300]
            raise ModelError(f"请求失败 HTTP {resp.status_code}: {detail}")

        for line in resp.iter_lines():
            piece = extract_delta(line)
            if piece == SSE_DONE:
                break
            if piece:
                yield piece


# ============================================================
# 五、流式返回 —— 异步版
# ============================================================
async def chat_stream_async(messages, temperature=0.7,
                            timeout=120) -> AsyncIterator[str]:
    """异步流式返回。

    【和同步版的唯一区别】
    同步版在等模型吐字的时候，会占住一个线程什么都不干；
    异步版在这段时间会把控制权交还给事件循环，去处理其他请求。

    效果差别有多大？同步版 40 个并发请求就把线程池占满，
    第 41 个开始排队；异步版单进程能扛几百上千个并发连接 ——
    因为等待的时间没有浪费，只是挂起了一个协程。

    这就是 JD 里"高并发"三个字背后的实际含义。
    """
    _, cfg, api_key = _pick()

    # AsyncClient 要配合 async with；整个生命周期结束后连接自动回收。
    # 注意：Client 的创建和复用本身有讲究（复用一个 Client 能省掉重复握手），
    # 但这里为了代码清晰，每次请求新建一个。
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", cfg["url"],
            headers=_headers(api_key),
            json=_build_stream_payload(cfg, messages, temperature),
        ) as resp:

            if resp.status_code != 200:
                # 异步版读响应体要 await
                body = await resp.aread()
                detail = body.decode("utf-8", errors="replace")[:300]
                raise ModelError(f"请求失败 HTTP {resp.status_code}: {detail}")

            # aiter_lines 是异步迭代器，用 async for
            async for line in resp.aiter_lines():
                piece = extract_delta(line)
                if piece == SSE_DONE:
                    break
                if piece:
                    yield piece


# ============================================================
# 六、容错解析模型返回的 JSON
# ============================================================
def parse_json_reply(text: str) -> dict:
    """把模型返回的文本解析成字典，容忍几种常见的不规范格式。

    必须先判代码块、后按大括号截取 —— 顺序反了的话，
    代码块里的 ``` 会被一起截进来，反而解析失败。
    """
    text = text.strip()

    if "```" in text:
        rest = text[text.index("```") + 3:]
        first_newline = rest.find("\n")
        if first_newline != -1:
            rest = rest[first_newline + 1:]
        end = rest.find("```")
        if end != -1:
            rest = rest[:end]
        text = rest.strip()
    else:
        left = text.find("{")
        right = text.rfind("}")
        if left != -1 and right != -1 and left < right:
            text = text[left:right + 1]

    # 故意不包 try/except：让解析失败抛出去，
    # 上层才知道"模型没按格式返回"，才能决定重试还是报错。
    return json.loads(text)


def chat_json(messages, temperature=0, timeout=60) -> dict:
    """要模型返回 JSON 的便捷方法：调用 + 容错解析。"""
    raw = chat(messages, temperature=temperature, timeout=timeout)
    try:
        return parse_json_reply(raw)
    except json.JSONDecodeError as e:
        raise ModelError(f"模型返回的不是合法 JSON：{raw[:200]}") from e
