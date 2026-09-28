# MCP Server —— 把工具暴露成标准协议

> **这一步做了什么**：把 `app/tools/` 里那 6 个运维工具，从"本项目自己能用"
> 变成"**任何支持 MCP 的客户端都能调用**"。
>
> 更新日期：2026-09-26

---

## 第 0 章｜为什么值得单独做这一层

这一章回答的就是你之前问过的那个问题的下半句：

> 「我直接用通用 AI 助手不就行了，为什么要自己做？」

第 4 章我们已经答过前半句：通用助手是**工具**，你的项目是**服务** ——
触发方式、权限边界、审计留痕、可量化程度都不一样。

而 MCP 解决的是**后半句**：

**通用助手确实比自建 Agent 强，但它缺"你们这台机器"的能力。**
你做的不是替代品，而是补上它缺的那块 ——
**而 MCP 就是"补上去"的那个接口。**

做完之后会发生什么：

```
之前：Cursor 只能用通用能力，问它"web-01 磁盘满了吗"它只能猜
之后：Cursor 挂上你的 MCP Server，直接调用 check_disk 拿到真实数据
```

**这就是从"我的项目"到"生态里的一个能力提供方"的转变。**

---

## 第 1 章｜MCP 是什么

**一句话：MCP（Model Context Protocol）= AI 应用和外部能力之间的 USB-C 接口。**

```
没有 MCP 之前：
    客户端 A ──适配代码──┐
    客户端 B ──适配代码──┼──→ 工具 1、工具 2、工具 3……（N 个工具）
    客户端 C ──适配代码──┘
    M 个客户端 × N 个工具 = M×N 份胶水代码

有了 MCP 之后：
    客户端 A ─┐
    客户端 B ─┼─ 说同一种协议 ─┬─→ 工具方按协议暴露一次
    客户端 C ─┘                └─→ 客户端按协议接一次
    M + N 份
```

**这就是为什么它是标准，也是为什么值得单独做一层。**

### 1.1 三种原语

| 原语 | 是什么 | 判断标准 | 本项目 |
|---|---|---|---|
| **tools** | 可调用的**动作**（会消耗资源或改变状态） | "做一件事" | 6 个 |
| **resources** | 可读取的**数据**（只读，像"文件"） | "读一份数据" | 2 个 |
| **prompts** | 可复用的**提示词模板** | "固化一段话术" | 暂时没用 |

**★ 这个判断标准是最容易被搞错的地方。**

把只读数据硬做成 tool 是常见误用。比如"工具清单"：

```
做成 tool：模型每次要用工具前，都得先花一次推理去"读清单"
           —— 既慢又费 token，而且这个决定本来就不该它做
做成 resource：客户端直接把数据挂进上下文，零推理成本
```

所以本项目的两个资源是：

| URI | 内容 |
|---|---|
| `agentdesk://tools/catalog` | 6 个工具的元数据（名称、风险等级、参数、说明） |
| `agentdesk://audit/recent` | 最近 20 条审计记录（谁在什么时候做了什么） |

### 1.2 ★ 风险提示：用协议自带的字段，不要自己发明

MCP 协议在工具定义里内建了 `annotations`，专门声明"这个动作危不危险"：

| 字段 | 含义 | 本项目取值 |
|---|---|---|
| `readOnlyHint` | 只读，不改任何东西 | `True` |
| `destructiveHint` | 可能造成破坏性变更 | `False` |
| `idempotentHint` | 重复调用结果一样 | `True` |
| `openWorldHint` | 会跟外部系统交互（不是纯本地计算） | `True` |

**重点不是这四个值，而是"用协议规定的字段表达风险"这件事。**

客户端（Cursor 等）**认识这些字段**，所以它们能据此在 UI 上提示用户、
或者在自动模式下决定要不要弹确认框。

如果你自己发明一个 `risk_level` 塞进 `meta`，客户端不认识它 —— 等于白写。
**这就是标准协议的价值：大家约好用同一套词。**

这也正好和 `app/tools/ops.py` 里那份 `risk` 元数据对上：
工具层的风险分级，到这里变成了协议层认得的字段。

### 1.3 两种传输方式

| | stdio | streamable-http |
|---|---|---|
| 怎么跑 | 客户端把 server 当**子进程**启动，走标准输入输出 | 独立进程，走 HTTP |
| 安全 | **不开端口**，进程生死由客户端管 | 要自己管鉴权、限流 |
| 适合 | 本地使用（Cursor / Claude Desktop 默认） | 远程、多客户端共享、部署到服务器 |
| 启动 | `python -m app.mcp_server.server` | 加 `--transport streamable-http --port 8765` |

本项目两种都支持，命令行切一下就行：

```bash
# stdio（默认）
.venv\Scripts\python.exe -m app.mcp_server.server

# 远程 HTTP
.venv\Scripts\python.exe -m app.mcp_server.server --transport streamable-http --port 8765
```

> ⚠️ **stdio 模式下有一个必须记住的规矩：stdout 是协议通道。**
> 往 stdout 打印任何调试信息，都会破坏协议 —— 客户端会收到一堆解析不了的垃圾，
> 报错却完全看不出原因。
>
> 所以本文件里所有提示都走 `stderr`（`print(..., file=sys.stderr)`）。
> **这是写 stdio 类程序最容易踩的坑，也是"能跑"和"稳定"之间的差别。**

---

## 第 2 章｜一个必须正视的设计取舍

### 2.1 两份 schema

同一个工具，现在有**两份参数声明**：

| 位置 | 给谁用 | 怎么来的 |
|---|---|---|
| `app/tools/ops.py` 的 `TOOLS` | OpenAI 格式的 Agent 循环 | 手写 |
| `app/mcp_server/server.py` 的类型注解 | MCP 客户端 | `Annotated[str, Field(...)]` 自动生成 |

**这是重复。而重复的东西一定会漂移。**

更糟的是**漂移了不会报错**：你在 `ops.py` 加了个参数、忘了在 MCP 这边加，
客户端拿到的就是一份过时的参数说明，然后一直用错参数 —— 没有任何异常提示你。

### 2.2 解法：把它变成显式校验

`server.py` 里的 `verify_schema_consistency()` 会把两边的 schema 拉出来逐项比对：

```python
mcp_tools = {t.name: t for t in await server.list_tools()}
# 逐个工具比：参数集合、必填项、类型、描述是否为空、有没有多余/遗漏的工具
```

**这就是「把静默不一致变成显式错误」** —— 和 RAG 那边
"索引记录 embedding 后端名、载入时校验"是同一个思路。
在 AI 工程里这一条格外重要，因为 **AI 的错误往往就是"不报错但结果不对"**。

### 2.3 ★ 校验器必须自己先被验证

**一个"永远返回通过"的校验器比没有更糟** —— 它会给你虚假的安全感。

所以我注入三类漂移，实测确认它真的能抓到：

| 注入的漂移 | 校验器的输出 |
|---|---|
| ops.py 多了一个参数，MCP 忘了加 | `check_disk：MCP 缺少参数 ['threshold']` |
| 必填项不一致 | `check_service：必填项不一致　声明 ['service'] vs MCP ['host', 'service']` |
| 类型写错 | `tail_log.lines：类型不一致　声明 string vs MCP integer` |

**这一步不能省。** 写完校验逻辑就跑一次"正常状态通过"是不够的 ——
你得证明它在异常状态下会失败。**否则你根本不知道这个校验有没有在工作。**

---

## 第 3 章｜四个真实踩到的坑

这些都是"凭记忆写代码就会挂"的地方，实测出来的。

### 坑一：mcp 2.x 把 `FastMCP` 改名成 `MCPServer`

```python
# ❌ v1 的写法，在 2.x 上直接 ModuleNotFoundError
from mcp.server.fastmcp import FastMCP

# ✅ 2.x
from mcp.server.mcpserver import MCPServer
```

安装的版本是 **mcp 2.2.0**。SDK 自己的报错信息很贴心，直接告诉你去哪看迁移指南 ——
**但如果你不看报错、凭记忆写 import，就会在这一步卡很久。**

> 顺带一个数字：v1 → v2 期间，同一个东西至少有三处改名：类名（FastMCP→MCPServer）、
> 字段名（`inputSchema`→`input_schema`）、模块路径。**这就是"框架版本变化快"的具体代价。**

### 坑二：参数描述只能靠 `Field`，docstring 的 `Args:` 段不解析

我一开始这样写，**参数描述是空的**：

```python
@server.tool(name="check_disk")
def check_disk(host: str = "web-01") -> dict:
    """查看主机磁盘使用率。

    Args:
        host: 主机名，如 web-01        ← 这段不会被解析！
    """
```

正确写法：

```python
def check_disk(
    host: Annotated[str, Field(description="主机名，如 web-01")] = "web-01",
) -> dict:
```

**实测结果**：用 docstring 写法，`input_schema.properties.host.description` 是 `None`；
用 `Field` 写法才有值。

**描述为空的后果很实际**：客户端（和模型）只能靠猜参数含义。
所以一致性校验里专门加了一条"描述为空也算问题"。

### 坑三：★ 错误类型决定了消息能不能传到客户端

这是最隐蔽的一个。第一版我抛的是 `ValueError`：

```
客户端收到：Error executing tool check_disk      ← 原因（"主机名不合法"）没了
```

模型看到这句等于没看到 —— 它不知道自己是参数写错了，只会原地重试同一个错参数。

看 SDK 源码才明白规则（`mcpserver/server.py`）：

```
抛 ToolError        → 客户端收到 "Error executing tool X: 主机名不合法"   ✅ 原因保留
抛其他任何异常       → 客户端只收到 "Error executing tool X"              ❌ 原因被吃
```

而且**错误类型还决定服务端日志级别**：

| 抛什么 | 服务端日志 |
|---|---|
| `ToolError` | `info` 级：`Tool X failed: 参数不合法` |
| 其他异常 | `exception` 级 + **完整 traceback** |

这个差别很重要：模型传错参数是**常态，不是崩溃**。
如果每次都刷一段 traceback，真正的故障就会被淹在噪音里 ——
**排障时最怕的不是没日志，是日志里全是废话。**

修复后实测：

```
✅ 非法参数被拒绝（防注入）
   isError=True　原因已传到客户端=True
   消息：Error executing tool check_disk: check_disk: 主机名不合法：'web-01; rm ...
```

> **顺带一个命名冲突**：`app.tools.ops.ToolError` 是我们自己定义的（参数不合法），
> MCP SDK 也有个 `ToolError`，两者含义不同但名字一样。
> 所以导入时起了别名 `MCPToolError` —— 不处理的话，以后读代码的人
> 会在"这到底是哪个 ToolError"上浪费很多时间。

### 坑四：`structured_output=True` 不接受 `dict` 返回类型

```python
@server.tool(name="x", structured_output=True)
def x() -> dict:        # ❌ InvalidSignature:
    ...                 #    return type <class 'dict'> is not serializable
```

想要 MCP 的 `structuredContent` 字段，返回类型必须是 **TypedDict 或 pydantic 模型**。

**本项目的选择：不启用，保持 `-> dict`。**

为什么？用 `-> dict` 时，工具结果以 **JSON 文本**形式放在 `content` 里 ——
这是**所有客户端都能解析**的最大公约数。启用结构化输出确实更"现代"，
但它要求每个工具定义一个返回模型，而且客户端不一定都支持。

**这是个可以讲出来的取舍：兼容性优先，等某个具体客户端真的需要往结构化字段再改。**

---

## 第 4 章｜怎么用、怎么测

### 4.1 三种用法

```bash
# ① 只看工具清单 + 跑 schema 一致性校验（不启动 server，适合自检/CI）
.venv\Scripts\python.exe -m app.mcp_server.server --check

# ② stdio 模式（给 Cursor / Claude Desktop 用）
.venv\Scripts\python.exe -m app.mcp_server.server

# ③ HTTP 模式（远程、多客户端共享）
.venv\Scripts\python.exe -m app.mcp_server.server --transport streamable-http --port 8765
```

`--check` 的真实输出：

```
==============================================================
  AgentDesk MCP Server
==============================================================
  名称     agentdesk-ops
  版本     0.1.0
  工具后端 mock（mock = 仿真数据 / local = 真机只读命令）
  工具数   6
  资源数   2（agentdesk://tools/catalog / agentdesk://audit/recent）

  · check_disk         risk=low    host
  · check_load         risk=low    host
  · check_service      risk=low    host*  service*
  · list_containers    risk=low    host
  · tail_log           risk=low    host*  service*  lines
  · search_knowledge   risk=low    query*  top_k
  （参数名后带 * 表示必填）

  ✅ MCP schema 与 ops.py 定义一致，无漂移
```

### 4.2 ★ 怎么测：用官方客户端连自己

```bash
.venv\Scripts\python.exe scripts\mcp_check.py
```

这个脚本**把 MCP Server 当子进程启动，然后用官方 MCP 客户端连上去**，
走一遍完整协议流程。实测 9/9 通过：

```
[1/4] 启动子进程并握手
  ✅ initialize 握手成功   server = agentdesk-ops v0.1.0

[2/4] 列出工具
  ✅ list_tools   收到 6 个工具
      · check_disk         参数 ['host']　必填 []　只读提示 True
      · tail_log           参数 ['host', 'lines', 'service']　必填 ['host', 'service']　只读提示 True
      · search_knowledge   参数 ['query', 'top_k']　必填 ['query']　只读提示 True
      （其余略）

[3/4] 调用工具
  ✅ call check_disk(web-01)   最高使用率 96% / 级别 critical
  ✅ call tail_log(web-01, nginx, 5)   返回 4 行，命中模式 ['磁盘空间耗尽', '上游服务响应超时']
  ✅ call search_knowledge   命中 2 条　来源 ['disk-full.md', 'disk-full.md']
  ✅ 非法参数被拒绝（防注入）   isError=True　原因已传到客户端=True
  ✅ 调用不存在的工具会报错   返回 isError（进程没有崩）

[4/4] 读取资源
  ✅ read agentdesk://tools/catalog   工具数 6　后端 mock
  ✅ read agentdesk://audit/recent   8 条审计记录
```

**为什么这个脚本很值**：它用的是**真正的 MCP 客户端**，和 Cursor / Claude Desktop
用的是同一套代码。所以它跑通 ≈ 那些客户端也能连上。

> **写 MCP Server 最容易卡在"配了客户端但连不上，也不知道是哪一步错了"。**
> 先跑通这个脚本，再去配客户端，问题范围立刻缩小一半。
> 这就是「协议层自检」存在的意义 —— `--check` 只证明了"模块能导入"，
> 传输层、握手、序列化任何一处错了它都测不出来。

### 4.3 HTTP 模式的实测证据

用 curl 手写 JSON-RPC 走一遍（这一步能看清协议长什么样）：

```
POST /mcp  initialize
  → 200 OK
    mcp-session-id: cb21474b0af5433c9a1dc84293769940
    x-accel-buffering: no          ← SDK 已替我们处理了 SSE 被 Nginx 缓冲那个坑
    content-type: text/event-stream
    data: {"result":{"serverInfo":{"name":"agentdesk-ops","version":"0.1.0"},...}}

POST /mcp  tools/list
  → check_disk  {'readOnlyHint': True, 'destructiveHint': False,
                 'idempotentHint': True, 'openWorldHint': True}
    （6 个工具都是这组值）

POST /mcp  tools/call  {"name":"check_disk","arguments":{"host":"web-01"}}
  → {"content":[{"type":"text","text":"{\"host\":\"web-01\",\"max_use_percent\":96,
                                          \"level\":\"critical\",...}"}],"isError":false}
```

**注意两点：**
1. 远程模式要**带 `mcp-session-id`** —— 握手那一步返回的，后续请求都要带上
2. 工具结果是 JSON **字符串**在 `content` 里，不是 `structuredContent`（见坑四）

### 4.4 配到客户端

**Cursor**：`~/.cursor/mcp.json`（Windows 是 `C:\Users\<你>\.cursor\mcp.json`）

```json
{
  "mcpServers": {
    "agentdesk-ops": {
      "command": "E:\\Workbuddy\\ai agent\\agentdesk\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server.server"],
      "cwd": "E:\\Workbuddy\\ai agent\\agentdesk"
    }
  }
}
```

**Claude Desktop**：配置文件里用同样的结构。

**两个必须注意的点**：

| 点 | 说明 |
|---|---|
| `command` 要用**绝对路径**指向 venv 里的 python | 客户端启动子进程时**不会**继承你的激活状态。用系统 python 会报 `No module named mcp` |
| 一定要给 `cwd` | 不然 `import app` 会失败（server 需要从项目根目录运行） |

> **排障顺序**（卡住时按这个顺序查，能省很多时间）：
> 1. 先跑 `scripts\mcp_check.py` —— 它通过就说明 server 没问题，是客户端配置的问题
> 2. 再检查 `command` 是不是绝对路径、`cwd` 有没有写
> 3. 最后看客户端的 MCP 日志（Cursor 里能看到 server 的 stderr 输出）

### 4.5 进自检

MCP 已经是 `scripts\smoke_test.py` 的**第 5 层**（加 `--full` 会跑协议层自检）：

```
[5/6] MCP Server　—— 工具的标准协议出口
  ✅ 服务端载入：agentdesk-ops   工具 6 个 · 资源 2 个
  ✅ schema 与 ops.py 一致（无漂移）   6 个工具全部对齐
  ✅ 协议层自检（真客户端连真 server）   9/9 项通过
```

---

## 第 5 章｜设计问答

### 5.1 为什么需要 MCP

> MCP 是 AI 应用和外部能力之间的标准接口，作用是把我这套工具的适配成本
> 从 M×N 降到 M+N —— 客户端不用为我写适配，我也不用为每个客户端写一遍。
>
> 我做它的动机很具体：我前面已经论证过，通用 AI 助手比自建 Agent 强，
> 但它缺"你们这台机器"的能力。**所以我不做替代品，我做它能接进去的那一块。**
> MCP 就是这个接口。

### 5.2 用到了哪些 MCP 原语

> 三种原语用了两种：**6 个 tools + 2 个 resources**。
>
> prompts 没用 —— 我暂时没有需要固化的提示词模板。
>
> 这里有个判断标准我觉得挺重要：**"做一件事"用 tool，"读一份数据"用 resource。**
> 比如"工具清单"，如果把它做成 tool，模型每次用工具前都得先花一次推理去读它 ——
> 既慢又费 token，而且这个决定本来就不该它做。做成 resource，
> 客户端直接挂进上下文就行。

### 5.3 工具的权限与风险如何表达

> 用协议**自带的** `annotations` 字段，不自己发明。
> MCP 定义了四个 hint：`readOnlyHint`、`destructiveHint`、
> `idempotentHint`、`openWorldHint`。我那 6 个工具全是只读诊断，
> 所以是 `readOnly=True / destructive=False / idempotent=True / openWorld=True`。
>
> **重点不是我填了什么值，而是客户端认识这些字段** ——
> Cursor 能据此在 UI 上提示用户，或者在自动模式下决定要不要弹确认框。
> 如果我自己发明一个 `risk_level` 塞进 meta，客户端不认识，等于白写。
> **标准协议的价值就是大家约好用同一套词。**

### 5.4 实现 MCP Server 的踩坑记录

> 四个，都是实测出来的：
>
> 1. **SDK 2.x 把 `FastMCP` 改名成了 `MCPServer`**，导入路径也变了 ——
>    凭记忆写 import 会直接 ModuleNotFoundError
> 2. **参数描述必须写 `Field(description=...)`**，
>    docstring 的 `Args:` 段不会被解析，参数描述会是空的
> 3. **★ 错误类型决定了消息能不能传到客户端**。我第一版抛 `ValueError`，
>    客户端只收到"Error executing tool check_disk"，**原因被吃掉了** ——
>    模型看到这句等于没看到，只会原地重试同一个错参数。
>    必须抛 SDK 认得的 `ToolError`。而且它还决定服务端日志级别：
>    抛 `ToolError` 记 info 级，抛别的异常记带 traceback 的 exception 级。
>    **模型传错参数是常态不是崩溃，让每次都刷 traceback，真故障就会被淹掉。**
> 4. **`structured_output=True` 不接受 `dict` 返回类型**，要用 TypedDict。
>    我选择不开结构化输出，保持 JSON 文本 —— 那是所有客户端都能解析的最大公约数。

### 5.5 如何测试 MCP Server

> 分两层。
>
> **第一层是 schema 一致性**：这层最有价值，因为同一份工具有两份参数声明
> （工具层手写的给 OpenAI 格式用，MCP 层从类型注解自动生成），
> 而**重复一定会漂移，漂移了还不报错**。所以我写了个校验器逐项比对。
>
> **但校验器本身必须先被验证** —— 一个"永远返回通过"的校验器比没有更糟，
> 它会给你虚假的安全感。所以我注入了三类漂移（多参数、必填项不一致、类型写错），
> 实测确认它真的能抓到。
>
> **第二层是协议层自检**：用官方 MCP 客户端连自己启动的 server，
> 走一遍 initialize → list_tools → call_tool → read_resource，
> 还专门测了错误路径（非法参数、不存在的工具）。
>
> 写 MCP Server 最容易卡在"配了客户端但连不上，也不知道是哪一步错了"。
> 先跑通协议自检，再去配客户端，问题范围立刻缩小一半。

---

## 附：文件对照

| 文件 | 作用 |
|---|---|
| `app/mcp_server/server.py` | MCP Server：6 tools + 2 resources + schema 一致性校验 |
| `scripts/mcp_check.py` | 协议层自检：官方客户端连自己，9 项检查 |
| `scripts/smoke_test.py` | 第 5 层是 MCP（`--full` 会跑协议层自检） |
| `app/tools/ops.py` | 工具实现（MCP 层只是转发，**没有第二份实现**） |

> **最后一个设计点**：MCP Server **没有重新实现任何工具**，
> 全部转发给 `app/tools/ops.py` 的 `execute_tool()`。
>
> 这意味着：改工具实现只改一个地方，安全边界（参数白名单、只读、风险分级）
> 自动对所有出口生效 —— 不管是自家 Agent 调，还是 Cursor 调。
> **这才是"分层"真正的好处：新增一个出口，不用复制一份逻辑。**
