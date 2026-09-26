# -*- coding: utf-8 -*-
"""
全链路自检
============================================================
一条命令跑完六层检查，输出 ✅/❌ 清单 + 汇总表。

【为什么需要这个脚本】
技术栈是分层堆起来的：

    运行环境 → 模型 API → 检索(RAG) → Agent(工具+编排+多Agent) → MCP Server → HTTP 服务

出问题时必须自下而上逐层确认。手敲命令一个个试，容易漏、也容易看错。
这个脚本把六层一次性跑完，最后告诉你「哪一层是好的、哪一层断了」。

【运行】在 agentdesk 目录下：
    .venv\\Scripts\\python.exe scripts\\smoke_test.py           # 快速（推荐，不花钱）
    .venv\\Scripts\\python.exe scripts\\smoke_test.py --full    # 完整（含真实 Agent 调用 + MCP 协议自检）

【注意】
    - 第 6 层需要服务已在运行。没运行会自动跳过，并提示启动命令。
    - 默认会真实调用模型 3 次（约 ¥0.001）；--full 再加一次 Agent 调用（约 ¥0.02）。
    - 「跳过」和「失败」是两回事：服务没启动只会标 ⏭，退出码仍是 0。

【退出码】0 = 已检查项全通；1 = 有真失败项
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# 零、Windows 中文环境：把输出流强制成 UTF-8
# ============================================================
# 中文 Windows 终端默认 GBK，打印 ✅❌ 这类符号可能直接抛
# UnicodeEncodeError 把脚本打断。reconfigure 一次解决。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 让脚本不管从哪个目录运行，都能 import 到 app 包
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BASE_URL = "http://127.0.0.1:8000"

# 每一项检查结果：(层, 项目, 是否通过, 说明, 是否跳过)
# 「跳过」和「失败」必须区分：服务没启动就不该让脚本以失败退出，
# 否则这个脚本没法放进自动化流程（一看退出码 1 就以为代码坏了）。
results = []


def record(layer: str, item: str, ok: bool, note: str = "",
           skipped: bool = False):
    """记一条检查结果，并立刻打印。"""
    results.append((layer, item, ok, note, skipped))
    mark = "⏭ " if skipped else ("✅" if ok else "❌")
    tail = f"   {note}" if note else ""
    print(f"  {mark} {item}{tail}")


def brief(exc: Exception) -> str:
    """错误信息只取一行，避免刷屏。"""
    text = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:90]}"


# ============================================================
# 第一层：运行环境
# ============================================================
def check_env():
    print("\n[1/6] 运行环境　—— Python 与依赖包")

    v = sys.version_info
    record("环境", f"Python {v.major}.{v.minor}.{v.micro}",
           v >= (3, 10), "要求 >= 3.10，低于则语法会报错")

    # 逐个 import，缺哪个补哪个，而不是笼统说"依赖有问题"
    packages = ["httpx", "dotenv", "fastapi", "pydantic", "numpy", "jieba",
                "uvicorn", "langgraph"]
    missing = []
    for name in packages:
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    record("环境", f"依赖包 {len(packages)} 个", not missing,
           "全部就位" if not missing else "缺失: " + ", ".join(missing)
           + "　→ 运行 pip install -r requirements.txt")

    # 看 .env 配了哪家的 Key（只显示前 6 位，不泄露完整密钥）
    from dotenv import load_dotenv
    import os
    load_dotenv(PROJECT_ROOT / ".env")
    found = []
    for var in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        val = (os.getenv(var) or "").strip()
        if val:
            found.append(f"{var}={val[:6]}...")
    record("环境", ".env 中的 API Key", bool(found),
           " · ".join(found) if found else "一个都没配 —— 第 2 层会失败")
    record("环境", "向量后端", True,
           "dashscope（真语义）" if os.getenv("DASHSCOPE_API_KEY")
           else "local 兜底（词面匹配，能跑通但不是真语义）")


# ============================================================
# 第二层：模型连通
# ============================================================
def check_model():
    print("\n[2/6] 模型连通　—— 真实发一次请求")

    try:
        from app.llm import chat
    except Exception as e:
        record("模型", "导入 app.llm", False, brief(e))
        return

    try:
        t0 = time.time()
        answer = chat(
            [
                {"role": "system", "content": "回答不超过 20 个字，不要客套话。"},
                {"role": "user", "content": "用一句话说明什么是负载均衡"},
            ],
            temperature=0,
        )
        elapsed = time.time() - t0
        record("模型", "调用成功", bool(answer.strip()),
               f"{elapsed:.1f}s　回答：{answer.strip()[:32]}")
    except Exception as e:
        record("模型", "调用失败", False,
               brief(e) + "　→ 检查 Key 是否有效、余额是否充足、网络是否通")


# ============================================================
# 第三层：检索与问答（RAG）
# ============================================================
def check_rag():
    print("\n[3/6] 检索与问答　—— RAG 全链路")

    try:
        from app.rag.pipeline import load_store, answer
    except Exception as e:
        record("检索", "导入 app.rag.pipeline", False, brief(e))
        return

    try:
        store = load_store()
        desc = store.embedder.describe()
        record("检索", f"载入索引：{len(store.chunks)} 块", True,
               f"后端 {desc['model']}／{desc['dim']} 维")
    except Exception as e:
        record("检索", "载入索引失败", False,
               brief(e) + "　→ 先构建：python -m app.rag.pipeline build")
        return

    # 三种检索模式各跑一次，顺便验证它们都能返回结果
    question = "nginx 报 502 怎么排查"
    for mode, label in (("vector", "向量（按语义找）"),
                        ("bm25", "关键词 BM25（按字面找）"),
                        ("hybrid", "混合检索（RRF 融合）")):
        try:
            hits = store.search(question, top_k=3, mode=mode)
            top = hits[0]["source"] if hits else "-"
            record("检索", f"{mode:7s} 命中 {len(hits)} 条", len(hits) > 0,
                   f"{label}　Top1 = {top}")
        except Exception as e:
            record("检索", f"{mode} 模式", False, brief(e))

    # 端到端问答（检索 + 模型生成 + 引用）
    try:
        t0 = time.time()
        result = answer("nginx 报 502 且 error.log 显示 upstream timed out，怎么处理",
                        top_k=3)
        elapsed = time.time() - t0
        sources = [c["source"] for c in result.get("citations", [])]
        record("问答", "RAG 问答 + 引用溯源", bool(result.get("answer")),
               f"{elapsed:.1f}s　引用 {sources}")
    except Exception as e:
        record("问答", "RAG 问答", False, brief(e))


# ============================================================
# 第四层：Agent（工具 + 编排）
# ============================================================
def check_agent(full: bool = False):
    print("\n[4/6] Agent 层　—— 工具注册表与编排引擎")

    # ---- 工具注册表 ----
    try:
        from app.tools import tool_catalog, tool_schemas
        from app.tools.ops import BACKEND, execute_tool
        catalog = tool_catalog()
        schemas = tool_schemas()
        record("工具", f"注册表载入 {len(catalog)} 个工具", len(catalog) > 0,
               f"后端 {BACKEND}　" + " · ".join(t["name"] for t in catalog))
        record("工具", f"schema 转换 {len(schemas)} 条", len(schemas) == len(catalog),
               "模型看到的就是这些 schema")
    except Exception as e:
        record("工具", "载入工具注册表", False, brief(e))
        return

    # ---- 真的执行一个工具（mocked 后端，不碰系统） ----
    try:
        out = execute_tool("check_disk", {"host": "web-01"})
        ok = out.get("ok")
        record("工具", "执行 check_disk(web-01)", bool(ok),
               f"最高使用率 {out.get('result', {}).get('max_use_percent')}%"
               f" / level={out.get('result', {}).get('level')}" if ok
               else out.get("error"))
    except Exception as e:
        record("工具", "执行 check_disk", False, brief(e))

    # ---- 参数校验是否真的在拦（这是安全边界，必须验） ----
    try:
        bad = execute_tool("check_disk", {"host": "web-01; rm -rf /"})
        ok = (not bad.get("ok")) and "不合法" in (bad.get("error") or "")
        record("工具", "拒绝非法参数（防注入）", ok,
               bad.get("error", "")[:60])
    except Exception as e:
        record("工具", "参数校验", False, brief(e))

    # ---- 手写引擎：模块可导入 + 图能编译 ----
    try:
        from app.agents.react import run as hw_run            # noqa: F401
        record("编排", "手写 ReAct 引擎可导入", True, "app/agents/react.py")
    except Exception as e:
        record("编排", "手写 ReAct 引擎", False, brief(e))

    try:
        from app.agents.graph import build_graph, mermaid
        build_graph(6)
        mm = mermaid(6)
        has_loop = "tools --> agent" in mm
        record("编排", "LangGraph 状态图编译", True,
               f"mermaid {len(mm)} 字符　循环边 {'存在' if has_loop else '缺失!'}")
    except Exception as e:
        record("编排", "LangGraph 状态图", False, brief(e))

    # ---- 多 Agent 编排：图能编译，且 4 个 Agent 都在 ----
    try:
        from app.agents.supervisor import build_graph as build_multi, mermaid as multi_mermaid
        build_multi(1)
        mm = multi_mermaid(1)
        # 每个专业节点干完都要回到 supervisor —— 这就是多 Agent 的"循环"
        back_edges = sum(1 for n in ("intent", "knowledge", "diagnose",
                                     "reason", "verify")
                         if f"{n} --> supervisor" in mm)
        record("编排", "多 Agent 状态图编译", True,
               f"mermaid {len(mm)} 字符　回边 {back_edges}/5")
    except Exception as e:
        record("编排", "多 Agent 状态图", False, brief(e))

    try:
        from app.agents.specialists import DIAGNOSE_TOOLS, diagnose
        record("编排", "4 个专业 Agent 可导入", True,
               f"工具执行 Agent 的受限工具集：{len(DIAGNOSE_TOOLS)} 个（不含 search_knowledge）")
    except Exception as e:
        record("编排", "专业 Agent 导入", False, brief(e))

    # ---- ★ 校验器自己也要被验证 ----
    # 一个"永远返回通过"的校验器比没有更糟 —— 它会给你虚假的安全感。
    # 所以这里用已知的**正例和反例**去测它，两边都要对。
    try:
        from app.agents.specialists import check_numbers, _check_overreach

        evidence = ['{"use_percent":96,"size":"40G","used":"38.4G","host":"web-01"}']
        cases = [
            # (说明, 结论片段, 期望是否发现问题)
            ("数字有出处 → 应通过",
             "**依据**\n- /dev/vda1 使用率 96%，共 40G，主机 web-01", False),
            ("数字来自用户问题 → 应通过",
             "**依据**\n- 用户提到的 502 由上游超时引起", False),
            ("数字凭空出现 → 应发现",
             "**依据**\n- 使用率 98%，剩余 2G", True),
            ("建议里的参数值 → 应通过（不是事实主张）",
             "**建议**\n1. chmod 755 /var/log\n2. chown 999:999 /data", False),
        ]
        bad_nums = 0
        for label, claim, want_problem in cases:
            probs, _note = check_numbers(claim, evidence,
                                         "nginx 报 502 了" if "用户问题" in label else "")
            found = bool(probs)
            if found != want_problem:
                bad_nums += 1
                print(f"      ✗ {label}：期望{'发现问题' if want_problem else '通过'}，"
                      f"实际 {'发现' if found else '通过'}")
        record("校验", f"数值溯源 {len(cases)} 个正反例", bad_nums == 0,
               "正例反例全对" if bad_nums == 0 else f"{bad_nums} 个不符预期")

        over_cases = [
            ("只读建议 → 应通过", "建议清理 /var/log 下的旧日志", False),
            ("越权声明 → 应发现", "我已经清理了 /var/log 下的旧日志", True),
        ]
        bad_over = 0
        for label, claim, want in over_cases:
            if bool(_check_overreach(claim)) != want:
                bad_over += 1
        record("校验", f"越权检测 {len(over_cases)} 个正反例", bad_over == 0,
               "正例反例全对" if bad_over == 0 else f"{bad_over} 个不符预期")
    except Exception as e:
        record("校验", "校验器自测", False, brief(e))

    # ---- 可选：真跑一轮（花钱，默认不跑） ----
    if full:
        try:
            from app.agents.graph import run as lg_run
            t0 = time.time()
            res = lg_run("web-01 上的磁盘用满了吗", max_steps=3)
            record("编排", "LangGraph 真跑一轮", bool(res["answer"]),
                   f"{time.time() - t0:.1f}s · {res['rounds']} 轮 · "
                   f"{res['tool_calls']} 次工具调用 · "
                   f"token {res['usage'].get('total_tokens', 0)}")
        except Exception as e:
            record("编排", "LangGraph 真跑一轮", False, brief(e))
        # 多 Agent 比单 Agent 贵（4 次以上模型调用，约 ¥0.02），
        # 所以也放在 --full 后面 —— 但它验证的是完全不同的东西：
        # 单 Agent 只证明 ReAct 循环能跑，多 Agent 还证明"调度 + 校验 + 汇总"这条链路通。
        try:
            from app.agents.supervisor import run as multi_run
            t0 = time.time()
            res = multi_run("web-01 上的磁盘用满了吗", max_retries=0)
            record("编排", "多 Agent 真跑一轮", bool(res["answer"]),
                   f"{time.time() - t0:.1f}s · "
                   f"路径 {'→'.join(res['path'])} · "
                   f"token {res['usage'].get('total_tokens', 0)} · "
                   f"校验 {'通过' if res['verdict'].get('pass') else '未通过'}")
        except Exception as e:
            record("编排", "多 Agent 真跑一轮", False, brief(e))
    else:
        record("编排", "真实一轮 Agent 调用（默认跳过）", False,
               "加 --full 参数才会跑：python scripts/smoke_test.py --full",
               skipped=True)


# ============================================================
# 第五层：MCP Server（把工具暴露成标准协议）
# ============================================================
def check_mcp(full: bool = False):
    print("\n[5/6] MCP Server　—— 工具的标准协议出口")

    # ---- 1. 服务端能导入、工具注册正确 ----
    try:
        from app.mcp_server.server import server, verify_schema_consistency
        from app.tools import TOOLS as TOOL_REGISTRY
        record("MCP", f"服务端载入：{server.name}", True,
               f"工具 {len(TOOL_REGISTRY)} 个 · 资源 2 个")
    except Exception as e:
        record("MCP", "载入 app.mcp_server.server", False, brief(e))
        return

    # ---- 2. ★ schema 一致性：MCP 自动生成的 vs ops.py 手写的 ----
    # 两份声明一定会漂移，而且漂移了不报错 —— 只会让客户端拿到过时的参数说明。
    # 所以这项检查是这一层的核心，不是附带的。
    try:
        result = asyncio.run(verify_schema_consistency())
        record("MCP", "schema 与 ops.py 一致（无漂移）", result["ok"],
               f"{result['tools']} 个工具全部对齐" if result["ok"]
               else "；".join(result["problems"]))
    except Exception as e:
        record("MCP", "schema 一致性校验", False, brief(e))

    # ---- 3. 协议层自检（可选的完整版）----
    if full:
        try:
            proc = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "mcp_check.py")],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180)
            tail = [ln.strip() for ln in (proc.stdout or "").splitlines()
                    if "项通过" in ln]
            record("MCP", "协议层自检（真客户端连真 server）",
                   proc.returncode == 0,
                   tail[0] if tail else f"退出码 {proc.returncode}")
        except Exception as e:
            record("MCP", "协议层自检", False, brief(e))
    else:
        record("MCP", "协议层自检（默认跳过）", False,
               "加 --full 才会跑：scripts\\mcp_check.py", skipped=True)


# ============================================================
# 第六层：HTTP 服务（需要服务已在运行）
# ============================================================
def check_http(full: bool = False):
    print("\n[6/6] HTTP 服务　—— 13 个接口")

    import httpx

    # 先探活。连不上就提示启动命令，直接返回，不要报一堆连接错误刷屏
    try:
        httpx.get(f"{BASE_URL}/health", timeout=5)
    except Exception:
        record("服务", "服务未启动（本层跳过）", False,
               "先执行：.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000",
               skipped=True)
        return

    record("服务", "服务在线", True, BASE_URL)

    def post(path, payload, timeout=90):
        return httpx.post(f"{BASE_URL}{path}", json=payload, timeout=timeout)

    # GET /health
    try:
        r = httpx.get(f"{BASE_URL}/health", timeout=10)
        record("接口", "GET  /health", r.status_code == 200,
               r.json().get("status", ""))
    except Exception as e:
        record("接口", "GET  /health", False, brief(e))

    # GET /audit
    try:
        r = httpx.get(f"{BASE_URL}/audit", params={"limit": 3}, timeout=10)
        ok = r.status_code == 200
        record("接口", "GET  /audit（审计留痕）", ok,
               f"累计 {r.json().get('total')} 条" if ok else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "GET  /audit", False, brief(e))

    # GET /rag/stats
    try:
        r = httpx.get(f"{BASE_URL}/rag/stats", timeout=30)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        record("接口", "GET  /rag/stats", ok,
               f"{d.get('documents')} 篇 / {d.get('chunks')} 块" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "GET  /rag/stats", False, brief(e))

    # POST /chat
    try:
        r = post("/chat", {"question": "一句话说明什么是容器"}, timeout=60)
        ok = r.status_code == 200 and r.json().get("answer")
        record("接口", "POST /chat（非流式）", bool(ok),
               (r.json().get("answer", "")[:28] if r.status_code == 200
                else f"HTTP {r.status_code}"))
    except Exception as e:
        record("接口", "POST /chat", False, brief(e))

    # POST /chat/stream —— 流式要逐块收，不能一次性读
    try:
        chunks = 0
        first = ""
        with httpx.stream("POST", f"{BASE_URL}/chat/stream",
                          json={"question": "用两句话说明什么是负载均衡"},
                          timeout=60) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    chunks += 1
                    if chunks == 1:
                        first = line[:36]
        record("接口", "POST /chat/stream（SSE 流式）", chunks > 1,
               f"收到 {chunks} 块　首块 {first}")
    except Exception as e:
        record("接口", "POST /chat/stream", False, brief(e))

    # POST /parse
    try:
        r = post("/parse", {"question": "web-01 上 nginx 日志太大了，清理一下 /var/log"})
        ok = r.status_code == 200
        d = r.json() if ok else {}
        record("接口", "POST /parse（意图解析）", ok,
               f"risk={d.get('risk')} need_confirm={d.get('need_confirm')}"
               f" 问了{d.get('_attempts')}次" if ok else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "POST /parse", False, brief(e))

    # POST /rag/search
    try:
        r = post("/rag/search", {"question": "容器退出码 137", "top_k": 3},
                 timeout=30)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        record("接口", "POST /rag/search（只检索）", ok,
               f"{d.get('mode')} 模式命中 {d.get('count')} 条" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "POST /rag/search", False, brief(e))

    # POST /rag/ask
    try:
        r = post("/rag/ask", {"question": "磁盘满了怎么处理", "top_k": 3})
        ok = r.status_code == 200 and r.json().get("answer")
        d = r.json() if r.status_code == 200 else {}
        record("接口", "POST /rag/ask（RAG 问答）", bool(ok),
               f"引用 {[c['source'] for c in d.get('citations', [])]}" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "POST /rag/ask", False, brief(e))

    # POST /webhook/alert —— 高危告警应转人工，这是核心行为
    try:
        alert = {
            "status": "firing",
            "alerts": [{
                "labels": {"alertname": "DiskSpaceCritical", "severity": "critical",
                           "instance": "web-01:9100", "service": "nginx"},
                "annotations": {"summary": "根分区使用率 96%",
                                "description": "请清理 /var/log 下的旧日志"},
            }],
        }
        r = post("/webhook/alert", alert)
        ok = r.status_code == 200
        rep = r.json()["reports"][0] if ok else {}
        record("接口", "POST /webhook/alert（无人值守）", ok,
               f"决策 {rep.get('decision')} / risk={rep.get('intent', {}).get('risk')}"
               if ok else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "POST /webhook/alert", False, brief(e))

    # GET /agent/tools
    try:
        r = httpx.get(f"{BASE_URL}/agent/tools", timeout=20)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        record("接口", "GET  /agent/tools（工具清单）", ok,
               f"{d.get('count')} 个工具 · 后端 {d.get('backend')}" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "GET  /agent/tools", False, brief(e))

    # GET /agent/graph —— 状态图里必须有那条循环边，没有就说明图建错了
    try:
        r = httpx.get(f"{BASE_URL}/agent/graph", timeout=20)
        ok = r.status_code == 200 and "tools --> agent" in r.text
        record("接口", "GET  /agent/graph（状态图）", ok,
               "循环边存在" if ok else "未找到 tools --> agent 边")
    except Exception as e:
        record("接口", "GET  /agent/graph", False, brief(e))

    # POST /agent/ask —— 真跑一轮 Agent，比较贵，只在 --full 时跑
    if full:
        try:
            t0 = time.time()
            r = post("/agent/ask", {"question": "web-01 上的磁盘用满了吗",
                                    "engine": "langgraph", "max_steps": 3})
            ok = r.status_code == 200 and r.json().get("answer")
            m = (r.json().get("metrics") or {}) if r.status_code == 200 else {}
            record("接口", "POST /agent/ask（自主诊断）", bool(ok),
                   f"{time.time() - t0:.1f}s · {m.get('rounds')} 轮 · "
                   f"{m.get('tool_calls')} 次工具调用 · "
                   f"token {m.get('tokens')}" if ok else f"HTTP {r.status_code}")
        except Exception as e:
            record("接口", "POST /agent/ask", False, brief(e))
    else:
        record("接口", "POST /agent/ask（默认跳过）", False,
               "加 --full 参数才会跑（一次约 7k token）", skipped=True)


# ============================================================
# 汇总
# ============================================================
def summarize():
    print("\n" + "=" * 62)
    print("  自检汇总")
    print("=" * 62)

    layers = ["环境", "模型", "检索", "问答", "工具", "编排", "校验", "MCP", "服务", "接口"]
    for layer in layers:
        rows = [r for r in results if r[0] == layer]
        if not rows:
            continue
        skipped = sum(1 for r in rows if r[4])
        ok = sum(1 for r in rows if r[2] and not r[4])
        checked = len(rows) - skipped
        if skipped and checked == 0:
            print(f"  {layer:4s} ⏭ 跳过　{len(rows)} 项")
            continue
        bar = "█" * ok + "░" * (checked - ok)
        tail = f"　(另跳过 {skipped} 项)" if skipped else ""
        print(f"  {layer:4s} {bar}  {ok}/{checked}{tail}")

    failed = [r for r in results if not r[2] and not r[4]]
    skipped = [r for r in results if r[4]]
    print()
    if failed:
        print(f"  ❌ {len(failed)} 项未通过：")
        for layer, item, _, note, _ in failed:
            print(f"     · [{layer}] {item}　{note}")
        print("\n  → 从最下面的失败层往上修，上面那层通常是它的连带后果。")
    elif skipped:
        print("  ✅ 已检查的项目全部通过。")
        print(f"  ⏭  有 {len(skipped)} 项被跳过（服务没在跑 / 未加 --full），可复跑。")
    else:
        print("  ✅ 全部通过。六层技术栈都在工作。")
    print("=" * 62)
    return 1 if failed else 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="AgentDesk 全链路自检（五层：环境/模型/检索/Agent/接口）")
    parser.add_argument("--full", action="store_true",
                        help="额外跑一次真实 Agent 调用（约 7k token，默认跳过）")
    args = parser.parse_args()

    print("=" * 62)
    print("  AgentDesk 全链路自检")
    print(f"  项目目录：{PROJECT_ROOT}")
    print(f"  模式：{'完整（含真实 Agent 调用）' if args.full else '快速（跳过花钱项）'}")
    print("=" * 62)

    steps = [lambda: check_env(),
             lambda: check_model(),
             lambda: check_rag(),
             lambda: check_agent(args.full),
             lambda: check_mcp(args.full),
             lambda: check_http(args.full)]
    for step in steps:
        try:
            step()
        except Exception as e:
            # 单个步骤意外崩了，不该拖垮整份报告
            record("脚本", "某一步", False, brief(e))

    sys.exit(summarize())


if __name__ == "__main__":
    main()
