# -*- coding: utf-8 -*-
"""
AgentDesk MCP Server
============================================================
把 `app/tools/` 里那 6 个运维工具暴露成 **MCP 标准协议**。

【为什么值得单独做这一层】
在这之前，6 个工具只有本项目自己能调用。做完 MCP 之后，
**任何支持 MCP 的客户端**（Cursor / Claude Desktop / 其他 Agent 平台）
都能直接调用它们。

这正是第 4 章回答过的那个问题的落点：
    通用 AI 助手（Cursor 之类）确实比自建 Agent 强，但它缺"你们这台机器"的能力。
    **你做的不是替代品，而是补上它缺的那块。**
    而 MCP 就是"补上去"的那个接口。

【一句话讲清 MCP 是什么】
MCP（Model Context Protocol）= **AI 应用和外部能力之间的 USB-C 接口**。

    没有 MCP：每个客户端要为自己的每个工具写一遍适配代码
               M 个客户端 × N 个工具 = M×N 份胶水代码
    有了 MCP：工具方按协议暴露一次，客户端按协议接一次
               M + N 份

【MCP 的三种原语 —— 面试会问"你用了哪些"】
    tools      可调用的动作（会改变状态或消耗资源）—— 本项目 6 个
    resources  可读取的数据（只读，像"文件"）        —— 本项目 2 个
    prompts    可复用的提示词模板                     —— 本项目暂时没用

    判断标准：**"做一件事"用 tool，"读一份数据"用 resource。**
    把只读数据硬做成 tool 是最常见的误用 —— 那会让模型多花一次推理
    去"决定要不要读"，而 resource 是客户端可以直接挂上去的上下文。

【两种传输方式】
    stdio             客户端把 server 当子进程启动，走标准输入输出。
                      本地用，最安全（不开端口），Cursor / Claude Desktop 用这个。
    streamable-http   走 HTTP，可以远程、可以多客户端共享、能挂鉴权。
                      部署到服务器、或要给别人用的时候用它。

【一个设计取舍，必须说清楚】
MCP 的 schema 由 Python 类型注解 + Field 自动生成；
而 `app/tools/ops.py` 里另有一份手写的参数定义（给 OpenAI 格式用）。
**同一个工具因此有两份 schema，这是重复。**
两份会漂移，而且漂移了不会报错 —— 只会让模型看到错的参数说明。

所以本文件末尾写了 `verify_schema_consistency()`：
启动时把两边的 schema 拉出来逐项比对，不一致直接抛错。
**把静默不一致变成显式错误**（和 RAG 索引校验后端名是同一个思路）。

【v2 的两个坑（凭记忆写就会挂）】
    1. mcp 2.x 把 `FastMCP` 改名为 `MCPServer`，导入路径变成
       `from mcp.server.mcpserver import MCPServer`
    2. 工具的 schema 字段是 `input_schema` 而不是 v1 的 `inputSchema`
    3. docstring 的 `Args:` 段**不会**被解析成参数描述 ——
       参数描述必须写 `Annotated[str, Field(description="...")]`
"""

import asyncio
import json
import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.llm import PROJECT_ROOT
from app.tools import TOOLS, execute_tool
from app.tools.ops import BACKEND

# ============================================================
# 一、风险提示 —— 用协议自带的字段，而不是自己发明
# ============================================================
# MCP 协议在工具定义里内建了 annotations，专门用来声明"这个动作危不危险"：
#     read_only_hint   只读，不改任何东西
#     destructive_hint 可能造成破坏性变更
#     idempotent_hint  重复调用结果一样
#     open_world_hint  会跟外部系统交互（而不是纯本地计算）
#
# 本项目的 6 个工具全都是只读诊断，所以四个值固定。
# **重点不是这四个值，而是"用协议规定的字段表达风险"这件事** ——
# 客户端（Cursor 等）能读懂这些字段，进而在 UI 上提示用户、
# 或者在自动模式下决定要不要弹确认框。
#
# 如果你自己发明一个 `risk_level` 字段塞进 meta，客户端不认识它，
# 等于白写。这就是标准协议的价值：**大家约好用同一套词。**
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# 工具清单与审计日志两个资源的地址。用 URI 形式是 MCP 的约定。
CATALOG_URI = "agentdesk://tools/catalog"
AUDIT_URI = "agentdesk://audit/recent"
AUDIT_PATH = PROJECT_ROOT / "logs" / "audit.jsonl"

# ============================================================
# 二、Server 实例
# ============================================================
server = MCPServer(
    name="agentdesk-ops",
    title="AgentDesk 运维工具",
    version="0.1.0",
    instructions=(
        "提供 Linux 主机与容器的只读诊断工具。"
        "所有工具都不会修改系统状态 —— 可以放心调用。"
        "典型用法：先用 check_disk / check_load / list_containers 看整体，"
        "再用 tail_log 定位具体原因，需要经验时用 search_knowledge 查知识库。"
        "已知主机：web-01（Web）、db-01（数据库）、cache-01（缓存）。"
    ),
)


def _call(name: str, **kwargs):
    """统一调用入口：执行工具，失败就抛 MCP 的 ToolError。

    这里有三个坑，都是真跑出来才发现的，值得记下来：

    【坑一：错误类型决定了消息能不能传到客户端】
    MCP SDK 对不同异常的处理不一样（见 mcpserver/server.py）：

        抛 ToolError         → 客户端收到 "Error executing tool X: 主机名不合法"
        抛其他任何异常        → 客户端只收到 "Error executing tool X"

    第一版我抛的是 `ValueError`，客户端拿到的消息里**原因被吃掉了**。
    对模型来说这等于没报错 —— 它不知道自己是参数写错了，
    只会原地重试同一个错参数。所以必须抛 SDK 认得的 `ToolError`。

    【坑二：命名冲突】
    `app.tools.ops.ToolError` 是我们自己定义的（参数不合法），
    MCP SDK 也有个 `ToolError`，两者含义不同但名字一样。
    所以导入时起别名 `MCPToolError` —— 这种冲突不处理的话，
    以后读代码的人会在"这到底是哪个 ToolError"上浪费很多时间。

    【坑三：错误类型还决定日志级别】
    抛 ToolError，服务端记的是 info 级："Tool X failed: 参数不合法"。
    抛别的异常，服务端记的是**带完整 traceback 的 exception 级**。

    这个差别很重要：模型传错参数是**常态，不是崩溃**。
    如果每次都刷一段 traceback，真正的故障就会被淹在噪音里 ——
    排障时最怕的不是没日志，是日志里全是废话。

    【另一个刻意的选择：这里抛错，而 Agent 循环里返回错误对象】
    在 `app/agents/` 里，工具报错要作为**观察结果**返回给模型，
    让它看到自己写错了然后改 —— 那里是"错误即数据"。

    在 MCP 这一层语义不同：调用方是客户端程序，它需要明确区分
    "调用成功、返回了这个结果" 和 "这次调用失败了"。
    MCP 协议为此专门有 isError 标记，抛异常正好触发它。

    **同一份工具实现，两种错误语义 —— 这不是不一致，
    是因为面向的调用方不同，契约也就不同。**
    """
    out = execute_tool(name, kwargs)
    if not out.get("ok"):
        raise MCPToolError(f"{name}: {out.get('error')}")
    return out["result"]


# ============================================================
# 三、6 个工具
# ============================================================
# 参数描述必须用 Annotated + Field —— docstring 里的 Args 段不会被解析。
# 类型注解决定 schema 里的 type，有没有默认值决定它是否进 required。
@server.tool(name="check_disk", title="查看磁盘使用率", annotations=READ_ONLY,
             description=TOOLS["check_disk"]["desc"])
def check_disk(
    host: Annotated[str, Field(description="主机名，如 web-01")] = "web-01",
) -> dict:
    return _call("check_disk", host=host)


@server.tool(name="check_load", title="查看负载与内存", annotations=READ_ONLY,
             description=TOOLS["check_load"]["desc"])
def check_load(
    host: Annotated[str, Field(description="主机名，如 web-01")] = "web-01",
) -> dict:
    return _call("check_load", host=host)


@server.tool(name="check_service", title="查看服务状态", annotations=READ_ONLY,
             description=TOOLS["check_service"]["desc"])
def check_service(
    host: Annotated[str, Field(description="主机名")],
    service: Annotated[str, Field(description="服务名，如 nginx、mysql")],
) -> dict:
    return _call("check_service", host=host, service=service)


@server.tool(name="list_containers", title="列出容器", annotations=READ_ONLY,
             description=TOOLS["list_containers"]["desc"])
def list_containers(
    host: Annotated[str, Field(description="主机名")] = "web-01",
) -> dict:
    return _call("list_containers", host=host)


@server.tool(name="tail_log", title="读取日志尾部", annotations=READ_ONLY,
             description=TOOLS["tail_log"]["desc"])
def tail_log(
    host: Annotated[str, Field(description="主机名")],
    service: Annotated[str, Field(description="服务名，如 nginx、mysql")],
    lines: Annotated[int, Field(description="读取行数，默认 20，最大 200")] = 20,
) -> dict:
    return _call("tail_log", host=host, service=service, lines=lines)


@server.tool(name="search_knowledge", title="检索运维知识库",
             annotations=READ_ONLY,
             description=TOOLS["search_knowledge"]["desc"])
def search_knowledge(
    query: Annotated[str, Field(description="检索关键词或问题")],
    top_k: Annotated[int, Field(description="返回条数，默认 3")] = 3,
) -> dict:
    return _call("search_knowledge", query=query, top_k=top_k)


# ============================================================
# 四、2 个资源（只读数据，不是动作）
# ============================================================
@server.resource(CATALOG_URI, name="tools_catalog",
                 title="工具清单",
                 description="所有工具的元数据：名称、风险等级、参数、说明",
                 mime_type="application/json")
def tools_catalog() -> str:
    """工具清单。

    ★ 注意它和 `check_disk` 这类工具的区别：
      这里没有"调用"这个动作，客户端是把这份数据**挂进上下文**。
      如果把它做成 tool，模型每次要用工具前都得先花一次推理去"读清单"——
      既慢又浪费 token，而且它本来就不该需要做这个决定。
    """
    from app.tools import tool_catalog
    return json.dumps({
        "backend": BACKEND,
        "count": len(TOOLS),
        "tools": tool_catalog(),
    }, ensure_ascii=False, indent=2)


@server.resource(AUDIT_URI, name="recent_audit",
                 title="最近审计记录",
                 description="AgentDesk 最近处理过的请求（告警、Agent 诊断等）",
                 mime_type="application/json")
def recent_audit() -> str:
    """最近 20 条审计记录。

    审计日志在 `logs/audit.jsonl`，一行一条 JSON。
    读不到文件不算错 —— 可能还没产生任何记录，返回空列表即可。
    （"文件不存在"和"文件存在但为空"对调用方是同一件事：没有记录。）
    """
    if not AUDIT_PATH.exists():
        return json.dumps({"count": 0, "items": []}, ensure_ascii=False)
    lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-20:]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            # 追加写入可能留下半行（进程被 kill），跳过而不是整份报错
            continue
    return json.dumps({"count": len(items), "items": items},
                      ensure_ascii=False, indent=2)


# ============================================================
# 五、★ schema 一致性校验
# ============================================================
# JSON Schema 类型名 → Python 类型。用于比对两边声明的参数类型是否一致。
_JSON_TO_PY = {"string": str, "integer": int, "number": float, "boolean": bool}


async def verify_schema_consistency() -> dict:
    """比对「MCP 自动生成的 schema」与「ops.py 手写的定义」是否一致。

    【为什么要做这件事】
    同一个工具现在有两份参数声明：
        app/tools/ops.py     手写的（给 OpenAI 格式的 Agent 循环用）
        本文件的类型注解       自动生成的（给 MCP 客户端用）

    这是重复，而**重复的东西一定会漂移**。更糟的是漂移了不会报错：
    你在 ops.py 加了个参数、忘了在 MCP 这边加，模型或客户端看到的
    就是一份过时的说明，然后一直用错参数 —— 没有任何异常提示你。

    所以把它做成一个显式校验，并且**在启动和自检时都跑一遍**。
    这类"把静默不一致变成显式错误"的做法，在 AI 工程里格外重要 ——
    因为 AI 的错误往往是"不报错但结果不对"。

    返回：{"ok": bool, "problems": [...], "tools": n}
    """
    mcp_tools = {t.name: t for t in await server.list_tools()}
    problems = []

    for name, meta in TOOLS.items():
        if name not in mcp_tools:
            problems.append(f"{name}：ops.py 里有，但没注册到 MCP")
            continue
        tool = mcp_tools[name]
        schema = tool.input_schema or {}
        mcp_props = schema.get("properties") or {}
        declared = meta["params"]

        missing = sorted(set(declared) - set(mcp_props))
        extra = sorted(set(mcp_props) - set(declared))
        if missing:
            problems.append(f"{name}：MCP 缺少参数 {missing}")
        if extra:
            problems.append(f"{name}：MCP 多出参数 {extra}")

        # 必填项必须一致 —— 这个最容易漏，而且漏了模型就是"少传一个必填参数"
        want_required = {p for p, m in declared.items() if m.get("required")}
        got_required = set(schema.get("required") or [])
        if want_required != got_required:
            problems.append(
                f"{name}：必填项不一致　声明 {sorted(want_required)} "
                f"vs MCP {sorted(got_required)}")

        # 类型也要对得上（JSON Schema 的 type 是字符串）
        for pname, pmeta in declared.items():
            if pname not in mcp_props:
                continue
            mcp_type = mcp_props[pname].get("type")
            if mcp_type != pmeta["type"]:
                problems.append(
                    f"{name}.{pname}：类型不一致　"
                    f"声明 {pmeta['type']} vs MCP {mcp_type}")

        # 描述为空也是个隐患：客户端看不到参数含义，只能靠猜
        for pname, prop in mcp_props.items():
            if not prop.get("description"):
                problems.append(f"{name}.{pname}：MCP 侧缺少参数描述")

    orphan = sorted(set(mcp_tools) - set(TOOLS))
    if orphan:
        problems.append(f"MCP 注册了 ops.py 里不存在的工具：{orphan}")

    return {"ok": not problems, "problems": problems, "tools": len(mcp_tools)}


# ============================================================
# 六、命令行入口
# ============================================================
def _print_tools_report() -> int:
    """打印工具清单 + 跑一致性校验，返回退出码。

    这是一个**非交互**入口 —— 自检脚本和 CI 用它，
    不需要真的启动 server、也不需要 MCP 客户端。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    result = asyncio.run(verify_schema_consistency())
    print("=" * 62)
    print("  AgentDesk MCP Server")
    print("=" * 62)
    print(f"  名称     {server.name}")
    print(f"  版本     0.1.0")
    print(f"  工具后端 {BACKEND}（mock = 仿真数据 / local = 真机只读命令）")
    print(f"  工具数   {result['tools']}")
    print(f"  资源数   2（{CATALOG_URI} / {AUDIT_URI}）")
    print()

    for name, meta in TOOLS.items():
        params = "  ".join(
            f"{p}{'*' if m.get('required') else ''}" for p, m in meta["params"].items())
        print(f"  · {name:18s} risk={meta['risk']:6s} {params}")
    print("  （参数名后带 * 表示必填）")
    print()

    if result["ok"]:
        print("  ✅ MCP schema 与 ops.py 定义一致，无漂移")
    else:
        print(f"  ❌ schema 不一致，发现 {len(result['problems'])} 处问题：")
        for p in result["problems"]:
            print(f"     · {p}")
        print("\n  → 两份声明必须同步。检查 ops.py 的 TOOLS 和本文件的类型注解。")
    print("=" * 62)
    return 0 if result["ok"] else 1


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="AgentDesk MCP Server —— 把 6 个运维工具暴露成 MCP 协议")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"],
                        default="stdio",
                        help="传输方式：stdio（本地，给 Cursor 用）/"
                             "streamable-http（远程）")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 模式监听地址")
    parser.add_argument("--port", type=int, default=8765, help="HTTP 模式端口")
    parser.add_argument("--check", action="store_true",
                        help="只打印工具清单并校验 schema，不启动 server")
    args = parser.parse_args(argv)

    if args.check:
        return _print_tools_report()

    if args.transport == "stdio":
        # stdio 是最安全的本地方式：不开端口，客户端直接管进程生死。
        # 注意 —— stdout 是协议通道，**任何调试打印都会破坏协议**。
        # 这就是为什么本文件所有输出都走 stderr（或干脆不打印）。
        print("[agentdesk-mcp] 以 stdio 方式启动，等待客户端连接……",
              file=sys.stderr)
        server.run(transport="stdio")
    else:
        print(f"[agentdesk-mcp] {args.transport} 方式启动于 "
              f"http://{args.host}:{args.port}/mcp", file=sys.stderr)
        server.run(transport=args.transport,
                   host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
