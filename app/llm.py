# -*- coding: utf-8 -*-
"""
模型调用的统一入口
============================================================
全项目所有和大模型打交道的地方，都只经过这一个文件。

【为什么必须统一入口】
如果每个模块各写各的 httpx.post，等你想换模型、加缓存、加重试、
加成本统计的时候，就要改十几个地方 —— 改漏一个就是一个线上 bug。
统一入口之后，这些能力只要在这里加一次，全项目都受益。

这个文件是 `practice/day1/llm_client.py` 的正式版：
多了流式输出、专门的异常类型、以及模型输出的容错解析。
"""

import json
import os
from pathlib import Path
from typing import Iterator

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
# 一、一次性返回（非流式）
# ============================================================
def chat(messages, temperature=0.7, timeout=60) -> str:
    """发一次对话请求，等模型把整段话说完再返回。

    messages 形如 [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
    """
    _, cfg, api_key = _pick()

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
        raise ModelError(f"连不上模型服务：{e}") from e
    except httpx.TimeoutException as e:
        raise ModelError(f"请求超时（{timeout}s）") from e

    if resp.status_code != 200:
        raise ModelError(f"请求失败 HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ============================================================
# 二、流式返回
# ============================================================
def chat_stream(messages, temperature=0.7, timeout=120) -> Iterator[str]:
    """逐块返回模型输出，每 yield 一小段文本。

    【为什么需要流式】
    非流式要等模型说完最后一句话才返回，用户干等十几秒，感觉像卡死了。
    流式让字一个个蹦出来，首字延迟从十几秒降到一秒内 ——
    用户体验的差别是天壤之别，而且实现成本很低。

    【这个函数是怎么做到的】
    请求体里加 "stream": True，服务端就不再一次性返回，
    而是持续推送若干行，每行形如：
        data: {"choices":[{"delta":{"content":"运"}}]}
    直到最后推一行：
        data: [DONE]
    我们要做的就是把每行切出来、解析、取出 content 片段。

    【yield 是什么】
    普通函数用 return 一次性交出结果；带 yield 的函数是"生成器"，
    每次 yield 交出一小块，调用方可以边收边处理。
    这就是"流式"在 Python 里的实现方式。
    """
    _, cfg, api_key = _pick()

    # 这里必须用 httpx.stream(...) 而不是 httpx.post(...)，
    # 因为前者不会等响应体完整下载完，可以边下边读。
    with httpx.stream(
        "POST",
        cfg["url"],
        headers=_headers(api_key),
        json={"model": cfg["model"], "messages": messages,
              "temperature": temperature, "stream": True},
        timeout=timeout,
    ) as resp:

        if resp.status_code != 200:
            # 出错时响应体还没读完，要显式 read() 才能拿到内容
            detail = resp.read().decode("utf-8", errors="replace")[:300]
            raise ModelError(f"请求失败 HTTP {resp.status_code}: {detail}")

        for line in resp.iter_lines():
            # 跳过空行（SSE 协议里用来分隔事件的）和不以 data: 开头的行
            if not line or not line.startswith("data:"):
                continue

            payload = line[5:].strip()   # 去掉 "data:" 这 5 个字符
            if payload == "[DONE]":
                break

            # 偶尔会有半截的 JSON（网络分包），解析不了就跳过这一块，
            # 不要因为一行坏了就把整个流断掉
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
            if delta:
                yield delta


# ============================================================
# 三、容错解析模型返回的 JSON
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
        # 这里翻译一下异常，让上层能明确区分"模型没按格式来"
        raise ModelError(f"模型返回的不是合法 JSON：{raw[:200]}") from e
