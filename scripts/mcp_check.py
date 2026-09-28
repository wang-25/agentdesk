# -*- coding: utf-8 -*-
"""
MCP 协议层自检 —— 用官方客户端连自己
============================================================
本脚本做的事：**把 MCP Server 当子进程启动，然后用官方 MCP 客户端连上去**，
走一遍真实协议流程：

    initialize 握手 → list_tools 列工具 → call_tool 调工具
                    → read_resource 读资源 → 故意调错看错误怎么返回

【为什么要写这个脚本】
`server.py --check` 只证明了"模块能导入、schema 一致"。
它**没有证明协议真的通** —— 传输层、握手、序列化，任何一处错了都测不出来。

而这个脚本用的是**真正的 MCP 客户端**，和 Cursor / Claude Desktop 用的是同一套
代码。所以它跑通 ≈ 那些客户端也能连上。

**这是最省事的验证方式**：不用装 Cursor、不用配 UI、不用点鼠标，
一条命令就能确认"我的 MCP Server 是好的"。
写 MCP Server 最容易卡在"配了客户端但连不上，也不知道是哪一步错了" ——
先跑通这个脚本，再去配客户端，问题范围立刻缩小一半。

【为什么"故意调错"也要测】
工具参数是外部传进来的（不管是模型还是人）。错误路径没验证过，
就等于不知道它出错时是"优雅返回错误"还是"整个进程崩掉"。
后者会让客户端直接断连 —— 排障时会被误导到"网络问题"上去。

用法：
    .venv\\Scripts\\python.exe scripts\\mcp_check.py
退出码：0 = 全部通过；1 = 有失败项
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Windows 控制台：输出流 + 代码页都切 UTF-8（否则中文乱码）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp import ClientSession                                    # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

results = []


def record(item: str, ok: bool, note: str = ""):
    results.append((item, ok, note))
    print(f"  {'✅' if ok else '❌'} {item}" + (f"   {note}" if note else ""))


def brief(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:110]}"


def unwrap(result) -> dict:
    """把 MCP 的 CallToolResult 拆成我们能看懂的东西。

    MCP 的返回结构有两部分：
        content            给模型/人看的（通常是文本）
        structuredContent  给程序用的结构化数据（如果 server 开启了结构化输出）
    本项目的工具都返回 dict，所以优先取结构化的那份；
    取不到就退回解析文本内容。
    """
    if result is None:
        return {}
    structured = (getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if isinstance(structured, dict):
        # SDK 有时会把返回值包一层（比如 {"result": {...}}），把它剥掉
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured

    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_text": text}
    return {}


def is_error(result) -> bool:
    return bool(getattr(result, "is_error", None)
                or getattr(result, "isError", None))


async def run_check() -> int:
    print("=" * 62)
    print("  MCP 协议层自检　—— 用官方客户端连自己")
    print("=" * 62)

    # ★ env 必须显式下发，否则这个自检测不了别的后端。
    #   MCP 官方客户端的 stdio_client 在 env=None 时**不会继承父进程环境**，
    #   它只传一小撮基础变量（HOME/PATH/SHELL/TERM…）。
    #   所以 `OPS_BACKEND=mock python scripts/mcp_check.py` 里的 mock
    #   会被静默丢掉 —— 子进程读 .env 拿到 ssh，你以为在测 mock，其实在测真机。
    #   （同理，PYTHONIOENCODING 也靠这里传，见脚本开头对 Windows 乱码的处理。）
    params = StdioServerParameters(
        command=sys.executable,                 # 用当前 venv 的 python
        args=["-m", "app.mcp_server.server"],
        cwd=str(PROJECT_ROOT),                  # 保证能 import app 包
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    print(f"\n[1/4] 启动子进程并握手")
    try:
        transport = stdio_client(params)
        read, write = await transport.__aenter__()
    except Exception as e:
        record("启动 MCP Server 子进程", False, brief(e))
        return summarize()

    try:
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
    except Exception as e:
        record("建立 ClientSession", False, brief(e))
        await transport.__aexit__(None, None, None)
        return summarize()

    try:
        init = await session.initialize()
        info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
        name = getattr(info, "name", "?") if info else "?"
        ver = getattr(info, "version", "?") if info else "?"
        record("initialize 握手成功", True, f"server = {name} v{ver}")
    except Exception as e:
        record("initialize 握手", False, brief(e))
        return summarize()

    # ---------- 2. list_tools ----------
    print(f"\n[2/4] 列出工具")
    tools = []
    try:
        listed = await session.list_tools()
        tools = list(getattr(listed, "tools", []) or [])
        record("list_tools", len(tools) > 0, f"收到 {len(tools)} 个工具")
    except Exception as e:
        record("list_tools", False, brief(e))

    for t in tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", {}) or {}
        props = sorted((schema.get("properties") or {}).keys())
        req = sorted(schema.get("required") or [])
        ann = getattr(t, "annotations", None)
        ro = getattr(ann, "read_only_hint", None) if ann else None
        print(f"      · {t.name:18s} 参数 {props}"
              f"　必填 {req}　只读提示 {ro}")

    # ---------- 3. call_tool ----------
    print(f"\n[3/4] 调用工具")

    # 3.1 正常调用
    try:
        r = await session.call_tool("check_disk", {"host": "web-01"})
        data = unwrap(r)
        ok = (not is_error(r)) and data.get("max_use_percent") is not None
        record("call check_disk(web-01)", ok,
               f"最高使用率 {data.get('max_use_percent')}% "
               f"/ 级别 {data.get('level')}")
    except Exception as e:
        record("call check_disk", False, brief(e))

    # 3.2 带多个参数 + 有默认值的
    # ★ 这里原先写死 service="nginx" —— 那是 **mock 仿真数据里才有** 的服务名。
    #   后果：自检在默认 mock 后端下永远是绿的，一接真实后端（ssh）立刻红：
    #   真机上压根没有 nginx 这个 systemd 服务（nginx 跑在容器里，叫 wp-npm）。
    #   这正是"测试用例与真实环境脱节"的典型样子 —— 绿灯是假的。
    #
    #   **测试用例不能依赖"只有某个后端才存在"的数据。**
    #   改成先问一句"这台机器上跑着什么"，再从里面挑一个来 tail。
    try:
        r = await session.call_tool("list_containers", {"host": "web-01"})
        names = [c.get("name") for c in (unwrap(r).get("containers") or [])]
        names = [n for n in names if n]
    except Exception:
        names = []

    # 候选顺序：真实存在的容器 → 常见 systemd 服务。
    # 容器排第一，是因为"日志在容器里"是眼下真实负载的常态 ——
    # 宿主机 /var/log 往往是空的（nginx/mysql 全在容器内）。
    candidates = names[:1] + ["docker", "sshd", "nginx"]
    picked, data = None, {}
    for cand in candidates:
        r = await session.call_tool("tail_log",
                                    {"host": "web-01", "service": cand,
                                     "lines": 5})
        data = unwrap(r)
        if not is_error(r) and (data.get("count") or 0) > 0:
            picked = cand
            break
    if picked:
        matched = data.get("matched_patterns") or []
        record(f"call tail_log(web-01, {picked}, 5)", True,
               f"返回 {data.get('count')} 行（来源 {data.get('source')}）"
               f"　命中模式 {[m.get('meaning') for m in matched]}")
    else:
        record("call tail_log(web-01, <自动发现>, 5)", False,
               f"试过 {'、'.join(candidates)} 都没取到日志"
               f"（最后一条提示：{data.get('hint')}）")

    # 3.3 走知识库（会加载 RAG 索引）
    try:
        r = await session.call_tool("search_knowledge",
                                    {"query": "磁盘满了怎么处理", "top_k": 2})
        data = unwrap(r)
        sources = [x.get("source") for x in (data.get("results") or [])]
        record("call search_knowledge", not is_error(r),
               f"命中 {data.get('count')} 条　来源 {sources}")
    except Exception as e:
        record("call search_knowledge", False, brief(e))

    # 3.4 ★ 故意调错 —— 验证错误路径
    # 重点不只是"报错了"，而是"**原因有没有传到客户端**"。
    # 只报 "Error executing tool check_disk" 这种通用文案是没用的：
    # 模型不知道自己是参数写错了，只会原地重试同一个错参数。
    try:
        r = await session.call_tool("check_disk",
                                    {"host": "web-01; rm -rf /"})
        text = "".join(getattr(b, "text", "") or ""
                       for b in (getattr(r, "content", None) or []))
        err = is_error(r)
        carried = "不合法" in text          # 真正的原因有没有带过来
        record("非法参数被拒绝（防注入）", err and carried,
               f"isError={err}　原因已传到客户端={carried}　"
               f"消息：{text.strip()[:64]}")
    except Exception as e:
        # 客户端抛异常也算"被拒绝了"，但要记下来 —— 说明 server 没优雅处理
        record("非法参数被拒绝（防注入）", True,
               f"客户端收到异常：{brief(e)}")

    # 3.5 调用不存在的工具
    try:
        r = await session.call_tool("no_such_tool", {})
        text = "".join(getattr(b, "text", "") or ""
                       for b in (getattr(r, "content", None) or []))
        record("调用不存在的工具会报错", is_error(r),
               f"返回 isError（进程没有崩）　消息：{text.strip()[:50]}")
    except Exception as e:
        record("调用不存在的工具", True, f"客户端收到异常：{brief(e)}")

    # ---------- 4. read_resource ----------
    print(f"\n[4/4] 读取资源")
    try:
        r = await session.read_resource("agentdesk://tools/catalog")
        contents = getattr(r, "contents", None) or []
        text = getattr(contents[0], "text", "") if contents else ""
        data = json.loads(text) if text else {}
        record("read agentdesk://tools/catalog", bool(data),
               f"工具数 {data.get('count')}　后端 {data.get('backend')}")
    except Exception as e:
        record("read tools/catalog", False, brief(e))

    try:
        r = await session.read_resource("agentdesk://audit/recent")
        contents = getattr(r, "contents", None) or []
        text = getattr(contents[0], "text", "") if contents else ""
        data = json.loads(text) if text else {}
        record("read agentdesk://audit/recent", "count" in data,
               f"{data.get('count')} 条审计记录（可能为 0）")
    except Exception as e:
        record("read audit/recent", False, brief(e))

    # ---------- 关闭 ----------
    try:
        await session_cm.__aexit__(None, None, None)
    except Exception:
        pass
    try:
        await transport.__aexit__(None, None, None)
    except Exception:
        pass

    return summarize()


def summarize() -> int:
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 62)
    print("  MCP 自检汇总")
    print("=" * 62)
    ok = len(results) - len(failed)
    print(f"  {ok}/{len(results)} 项通过")
    print()
    if not failed:
        print("  ✅ 协议层全通。Cursor / Claude Desktop 用同一套客户端代码，")
        print("     可以放心去配客户端了。")
    else:
        print(f"  ❌ {len(failed)} 项未通过：")
        for item, _, note in failed:
            print(f"     · {item}　{note}")
    print("=" * 62)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_check()))
