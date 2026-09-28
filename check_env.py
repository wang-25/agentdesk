# -*- coding: utf-8 -*-
"""
环境验收脚本
============================================================
目标：确认三件事都通了 —— Python 环境、API Key、到模型的网络。

运行方式（在 agentdesk 目录下打开终端）：
    .venv\\Scripts\\python.exe check_env.py

通过标准：
    屏幕上出现模型的一句回答，以及这次调用消耗了多少 token、花了多少钱。
============================================================

【怎么读这个文件】
每一段代码上面都有注释解释「为什么这么写」，而不只是「这行做了什么」。
排查问题时重点看「为什么」，语法细节可以跳过。
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ============================================================
# 零、Windows 中文环境的一个坑：把输出流强制成 UTF-8
# ============================================================
# 中文 Windows 的终端默认用 GBK 编码。直接打印中文或符号（✅❌ 这类）时，
# 轻则显示成乱码，重则直接抛 UnicodeEncodeError 把程序打断。
# reconfigure 把输出流改成 UTF-8，一次解决。
#
# 为什么包在 try 里？因为个别运行环境（比如没有真实终端的场景）
# 这个流对象不支持 reconfigure。环境问题不该拖垮整个脚本，
# 所以失败就静默跳过 —— 这是写兼容代码的常见手法。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ============================================================
# 一、读取配置
# ============================================================

# __file__ 是当前这个脚本的路径；.resolve() 把它变成完整绝对路径；
# .parent 取它所在的目录。这样写的好处：不管你从哪个目录运行这个脚本，
# 它都能正确找到同目录下的 .env —— 这是最常踩的坑之一。
PROJECT_ROOT = Path(__file__).resolve().parent

# load_dotenv 会把 .env 文件里每一行 "KEY=VALUE" 读进环境变量。
# 为什么要把 Key 放在 .env 而不是直接写在代码里？
#   因为代码要推到 GitHub，Key 一旦泄露，别人就能用你的账号花钱。
# 这是 AI 开发里最重要的一条安全习惯，从一开始就要养成。
load_dotenv(PROJECT_ROOT / ".env")

# ============================================================
# 二、选择用哪家模型
# ============================================================
# 两家都用「OpenAI 兼容」的接口格式，所以除了地址和模型名不同，
# 请求体和返回结构是一样的 —— 这就是为什么换模型不用改多少代码。
PROVIDERS = {
    "deepseek": {
        "env_key": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        # 单价单位：元 / 百万 token
        # ⚠️ 价格会调整！第一次跑之前去官网定价页核对一下，不对就改这两行：
        #    https://api-docs.deepseek.com/quick_start/pricing
        "price_in": 2.0,
        "price_out": 8.0,
    },
    "dashscope": {
        "env_key": "DASHSCOPE_API_KEY",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-plus",
        # 没把握就先留空，脚本会跳过成本计算、只打印 token 数
        "price_in": None,
        "price_out": None,
    },
}


def pick_provider():
    """找出 .env 里填了哪个 Key，就用哪家。都没填就给出手把手指引。"""
    for name, cfg in PROVIDERS.items():
        key = (os.getenv(cfg["env_key"]) or "").strip()
        if key:
            # 只显示头尾，中间打码。养成习惯：密钥永远不要完整打印，
            # 因为终端输出可能被录屏、截图或者写进日志文件。
            masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"
            print(f"✅ 找到 {name} 的 Key：{masked}")
            return name, cfg, key

    print("❌ 没有找到任何可用的 API Key。\n")
    print("请按顺序做：")
    print("  1. 打开 agentdesk 目录下的 .env 文件")
    print("  2. 把 DEEPSEEK_API_KEY= 后面填上你自己的 Key（= 后面不要有空格）")
    print("  3. 保存文件，重新运行这个脚本")
    print()
    print("如果还没有 Key：")
    print("  DeepSeek   → https://platform.deepseek.com    （充值 10-20 元够跑完整个项目）")
    print("  阿里云百炼 → https://bailian.console.aliyun.com （你已有阿里云账号，可能有免费额度）")
    sys.exit(1)


# ============================================================
# 三、组装并发出请求
# ============================================================
def call_model(cfg, api_key):
    """发一次对话请求，返回 (模型说的话, 用量字典)。"""

    # messages 是一个数组，这是所有大模型接口的统一格式。
    # 三种角色要分清，后面做 Agent 全靠它们：
    #   system    —— 给模型的规则和身份，优先级最高，用户看不到
    #   user      —— 用户说的话
    #   assistant —— 模型之前说过的话（多轮对话时要把历史也放进来）
    messages = [
        {
            "role": "system",
            "content": "你是一个简洁的技术助手。回答不超过 30 个字，不要客套话。",
        },
        {
            "role": "user",
            "content": "用一句话解释什么是运维。",
        },
    ]

    payload = {
        "model": cfg["model"],
        "messages": messages,
        # temperature 控制随机性：0 最稳定（同样输入尽量给同样输出），
        # 后面做结构化输出和工具调用时必须设成 0，现在先养成习惯
        "temperature": 0,
    }

    if (os.getenv("DEBUG") or "").strip() == "1":
        print("\n--- 即将发出的请求体（这就是一次模型调用最核心的东西）---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    print("\n⏳ 正在请求模型...")

    # 用 try 包住网络请求：网络类操作随时可能失败，
    # 不处理的话你只会看到一大坨看不懂的报错
    try:
        resp = httpx.post(
            cfg["url"],
            headers={
                # Bearer 是行业标准的鉴权写法，几乎所有 API 都长这样
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            # 一定要设超时。不设的话请求卡住时程序会永远挂着，
            # 你根本不知道是网络慢还是代码错
            timeout=60,
        )
    except httpx.ConnectError:
        print("❌ 连不上服务器。大概率是网络问题，检查一下代理或换网络重试。")
        sys.exit(1)
    except httpx.TimeoutException:
        print("❌ 请求超时（超过 60 秒）。不一定是错的，重跑一次试试。")
        sys.exit(1)

    # 按状态码给出人话版的错误说明。
    # 这一步很值得：以后你在项目里排查问题，靠的就是这些状态码。
    if resp.status_code != 200:
        print(f"❌ 请求失败，HTTP 状态码 {resp.status_code}")
        if resp.status_code == 401:
            print("   → 401 = Key 不对。检查 .env 里有没有多余空格、Key 是否完整。")
        elif resp.status_code == 402:
            print("   → 402 = 余额不足。去控制台充值。")
        elif resp.status_code == 429:
            print("   → 429 = 请求太频繁或额度用尽，等一会儿再试。")
        else:
            print("   → 原始返回内容：")
            print("  ", resp.text[:500])
        sys.exit(1)

    data = resp.json()

    if (os.getenv("DEBUG") or "").strip() == "1":
        print("\n--- 服务器返回的完整结构（第一次一定要看一眼）---")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])

    # 从返回结构里取出内容。记住这个路径，
    # 以后所有模型接口都是这个套路：choices → 第一个 → message → content
    answer = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return answer, usage


# ============================================================
# 四、算钱
# ============================================================
def report(provider_name, cfg, answer, usage):
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

    print("\n" + "=" * 56)
    print("模型回答：")
    print(" ", answer.strip())
    print("=" * 56)

    print(f"\n用量（{provider_name} / {cfg['model']}）")
    print(f"  输入 prompt_tokens     : {prompt_tokens}")
    print(f"  输出 completion_tokens : {completion_tokens}")
    print(f"  合计 total_tokens      : {total_tokens}")

    if cfg["price_in"] is None or cfg["price_out"] is None:
        print("\n  （这家模型的单价没填，跳过成本计算）")
        return

    # 费用 = 输入 token 数 × 输入单价 + 输出 token 数 × 输出单价
    # 单价是「元 / 百万 token」，所以要除以 100 万
    cost = prompt_tokens / 1_000_000 * cfg["price_in"] + completion_tokens / 1_000_000 * cfg["price_out"]
    print(f"\n本次成本 ≈ ¥{cost:.6f}")

    # 这个换算比绝对金额更有用：你真正关心的是「这笔钱能跑多少次」
    if cost > 0:
        print(f"  换算一下：¥10 大约可以跑 {int(10 / cost):,} 次这样的调用")
    print("\n📌 记住这个公式，「怎么控制成本」的答案就是它。")
    print("📌 输入和输出单价不一样，输出通常贵 2-4 倍 —— 所以让它少说废话是真能省钱的。")


# ============================================================
# 五、主流程
# ============================================================
def main():
    print("=" * 56)
    print("  环境验收")
    print("=" * 56)

    # 顺便把环境信息打出来。以后遇到「为什么我这报错他那不报」，
    # 第一步永远是确认环境版本 —— 这是运维人的老本行，用得上
    print(f"Python 版本：{sys.version.split()[0]}")
    print(f"解释器路径：{sys.executable}")
    print()

    provider_name, cfg, api_key = pick_provider()
    answer, usage = call_model(cfg, api_key)
    report(provider_name, cfg, answer, usage)

    print("\n✅ 验收通过。你的环境、Key、网络三件事都通了。")


if __name__ == "__main__":
    # 这行的意思是：只有「直接运行这个文件」时才执行 main()。
    # 如果别的文件 import 它，就不会自动跑 —— 这是 Python 的标准写法。
    main()
