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
# 零、Windows 中文环境：输出流 + 控制台代码页，一起切 UTF-8
# ============================================================
# 中文 Windows 终端默认 GBK。**只 reconfigure 输出流是治不了本的** ——
# 控制台自己的代码页仍是 936，会把 UTF-8 字节按 GBK 解释，中文照样乱码。
# 两层都要切，所以这件事统一交给 scripts/_console.py（含代码页切换）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402

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
    print("\n[1/9] 运行环境　—— Python 与依赖包")

    v = sys.version_info
    record("环境", f"Python {v.major}.{v.minor}.{v.micro}",
           v >= (3, 10), "要求 >= 3.10，低于则语法会报错")

    # 逐个 import，缺哪个补哪个，而不是笼统说"依赖有问题"
    packages = ["httpx", "dotenv", "fastapi", "pydantic", "numpy", "jieba",
                "uvicorn", "langgraph", "mcp"]
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

    # ---- 工具层数据源 ----
    # ★ 这一项必须报出来。工具层接的是仿真数据还是真机，
    #   决定了后面所有诊断结论的性质 —— 不写清楚，
    #   读报告的人会默认"它查的是真机器"。
    report_tool_backend()


def report_tool_backend():
    """报出工具层后端；ssh 后端下**顺手验一次连通性**。

    ★ 为什么要真连一次：配置写错（密钥路径不对、IP 写错、目标机器没开）
      是个很容易发生、又不会在启动时报错的错误 ——
      它会一直潜伏到某次工具调用才暴露，而那时你正在演示。
      在自检里花一次只读查询把它揪出来，成本几乎为零。
    """
    try:
        from app.tools.ops import BACKEND, KNOWN_HOSTS, SSH_TARGETS, execute_tool
    except Exception as e:
        record("环境", "工具层后端", False,
               str(e).replace("\n", " ")[:90])
        return

    if BACKEND != "ssh":
        record("环境", f"工具层后端 {BACKEND}", True,
               "内置仿真数据，不连任何真实机器" if BACKEND == "mock"
               else "在本机执行只读命令")
        return

    if not SSH_TARGETS:
        record("环境", "工具层后端 ssh", False,
               "OPS_SSH_TARGETS 是空的 —— ssh 后端必须配映射表")
        return

    desc = ", ".join(f"{k}={v['user']}@{v['host']}:{v['port']}"
                     for k, v in SSH_TARGETS.items())
    failures = []
    for name in SSH_TARGETS:
        # 用 check_load 探活：它是最轻的只读查询之一，
        # 而且会顺带证明"整条链路（ssh → 解析 → 返回）通"，
        # 不只是"端口能连上"。
        out = execute_tool("check_load", {"host": name})
        if not out.get("ok"):
            failures.append(f"{name}: {str(out.get('error'))[:70]}")
    record("环境", f"工具层后端 ssh（{KNOWN_HOSTS}）", not failures,
           f"{desc}　{len(SSH_TARGETS)} 个目标连通" if not failures
           else "连不上 → " + "；".join(failures))


# ============================================================
# 第二层：模型连通
# ============================================================
def check_model():
    print("\n[2/9] 模型连通　—— 真实发一次请求")

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
    print("\n[3/9] 检索与问答　—— RAG 全链路")

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
    print("\n[4/9] Agent 层　—— 工具注册表与编排引擎")

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
        record("编排", "5 个专业 Agent 可导入", True,
               f"工具执行 Agent 的受限工具集：{len(DIAGNOSE_TOOLS)} 个（不含 search_knowledge / run_command）")
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
# 第五层：沙箱与人工确认（离线，不花钱，不需要服务在跑）
# ============================================================
def check_sandbox(full: bool = False):
    print("\n[5/9] 沙箱与人工确认　—— 准入策略 + 审批状态机")

    import tempfile

    from app.sandbox import executor, policy
    from app.sandbox import approvals as ap

    # ---- 1. 后端与 fail-closed ----
    info = executor.describe()
    note = {"mock": "未装 Docker，仿真执行；装了 Docker 设 "
                    "SANDBOX_BACKEND=docker 即真隔离",
            "docker": f"真隔离，镜像 {info.get('image')}",
            "subprocess": "⚠️ 无隔离，直接在目标主机执行"}.get(
        info["backend"], "")
    record("沙箱", f"执行后端 {info['backend']}", True, note)
    if info.get("fail_closed") is True:
        record("沙箱", "fail-closed（不可用即拒绝，不降级）", True,
               "这是安全设计里最容易搞反的一点")

    # ---- 2. 规则表自检：每条 example 必须命中自己 ----
    #     ★ 这一条是沙箱那部分踩出来的真实教训的固化：
    #       第一版 12 条规则里有 5 条（systemctl / docker 的）因为
    #       子命令约定不一致，**从来没生效过**，而且不报错。
    bad = 0
    for r in policy.catalog():
        d = policy.decide(r["example"])
        want = "needs_approval" if r["requires_approval"] else "allow"
        if d.decision != want:
            bad += 1
    record("沙箱", f"白名单 {len(policy.catalog())} 条规则自检", bad == 0,
           "全部命中自己的示例" if bad == 0 else f"{bad} 条失效")

    # ---- 3. 攻击面必须全拒 ----
    attacks = ["rm -rf /", "truncate -s 0 /etc/passwd",
               "df -h; rm -rf /", "bash -c id",
               "tail -n 5 ../../etc/passwd", "cat /etc/shadow",
               "du -sh /root", "lsof -i", "systemctl disable firewalld"]
    leaked = [a for a in attacks if policy.decide(a).decision != "deny"]
    record("沙箱", f"攻击面 {len(attacks)} 条全拒", not leaked,
           "全部拒绝" if not leaked else f"漏了：{leaked}")

    # ---- 4. 执行器真的在干活（后端感知的断言）----
    #   ★ 这条断言吃过一次亏：第一版写死了"mock 后端必须返回仿真数据"。
    #     用户装上 Docker 后 auto 切到真执行，du 走主机通道在 Windows 上
    #     找不到 /var/log，断言就错了 —— **断言不该绑定后端实现细节。**
    #     改成：无论哪个后端，执行器都要给出"确定性的结果"（成功或明确的失败），
    #     并如实暴露 isolated 标记。
    d = policy.decide("du -h -d1 /var/log")
    r = executor.run(d)
    info = executor.describe()
    if info["backend"] == "mock":
        record("沙箱", "mock 执行 du 返回仿真数据",
               r.ok and "nginx" in r.stdout, r.stdout.strip()[:40])
    else:
        record("沙箱", f"真实执行（后端 {info['backend']}）",
               isinstance(r.ok, bool) and (r.ok or r.error),
               f"isolated={r.isolated}　exit={r.exit_code}　"
               + (r.error or r.stdout.strip()[:30]))

    # ---- 5. 审批状态机（临时 store，不污染 logs/approvals.jsonl）----
    st = ap.ApprovalStore(path=Path(tempfile.mkdtemp()) / "approvals.jsonl")

    rec = st.create(command="truncate -s 0 /var/log/nginx/error.log",
                    fingerprint="fp1", rule="truncate", risk="reversible",
                    isolation="container", reason="测试")
    aid = rec["id"]

    # 批准必须填审批人
    ok_noby = False
    try:
        st.approve(aid, by="   ")
    except ap.ApprovalError:
        ok_noby = True
    record("审批", "批准必须填审批人", ok_noby, "缺 by 被拒")

    # 批准 → 消费 → 重放
    st.approve(aid, by="test")
    st.consume(aid, expected_fingerprint="fp1")
    ok_replay = False
    try:
        st.consume(aid, expected_fingerprint="fp1")
    except ap.ApprovalError:
        ok_replay = True
    record("审批", "已消费不可重放", ok_replay, "第二次 consume 被拒")

    # 指纹不匹配（TOCTOU 防护）
    rec2 = st.create(command="x", fingerprint="fpA", rule="t", risk="r",
                     isolation="c", reason="t")
    st.approve(rec2["id"], by="t")
    ok_fp = False
    try:
        st.consume(rec2["id"], expected_fingerprint="fpB")
    except ap.ApprovalError:
        ok_fp = True
    record("审批", "指纹不符拒绝执行", ok_fp, "TOCTOU 防护生效")


# ============================================================
# 第六层：可观测（离线，不花钱）
# ============================================================
def check_observability(full: bool = False):
    print("\n[6/9] 可观测　—— trace 记录 + 成本核算 + 导出 payload")

    from app.observability import costs, langfuse_export, tracer

    # ---- 1. trace + 嵌套 span 的记录与聚合 ----
    started_count = 0
    try:
        # ★ 结构必须和真实运行一致：**usage 落在叶子 span 上**。
        #   原先这里把 usage 直接挂在"有子节点的 agent span"上（intent），
        #   而真实运行里 token 永远产生在最内层那次模型调用（llm span）上，
        #   再向上归并。成本核算现在按叶子 span 归因（只有叶子知道用了哪个模型），
        #   所以旧写法的成本会算成 0 —— 是**测试用例不真实**，不是代码错了。
        with tracer.trace("smoke", question="自检用例", source="selftest") as tid:
            with tracer.span(tracer.TYPE_AGENT, name="intent") as sp:
                with tracer.span(tracer.TYPE_LLM, name="chat_step") as lsp:
                    lsp.set_usage({"prompt_tokens": 100, "completion_tokens": 20,
                                   "prompt_cache_hit_tokens": 60,
                                   "prompt_cache_miss_tokens": 40})
                with tracer.span(tracer.TYPE_TOOL, name="check_disk") as tsp:
                    tsp.set("host", "web-01")
        traces = tracer.recent_traces(limit=5, source="selftest")
        t = next((x for x in traces if x["trace_id"] == tid), None)
        ok_trace = t is not None
        spans_ok = ok_trace and t.get("span_count") == 3 and len(t.get("spans") or []) == 3
        # ★ 按 name 找，不按位置 —— spans 是按「结束顺序」收集的，
        #   内层 span 先结束先入列，位置断言会随嵌套方向翻反
        by_name = {sp.get("name"): sp for sp in (t.get("spans") or [])} if t else {}
        intent_id = by_name.get("intent", {}).get("span_id")
        parent_ok = (spans_ok and intent_id
                     and all(by_name.get(n, {}).get("parent_id") == intent_id
                             for n in ("chat_step", "check_disk")))
        # 叶子 llm span 的 usage 应当被归并到顶层（trace 总量）
        usage_ok = (ok_trace and t.get("usage", {}).get("prompt_tokens") == 100)
        cost_ok = ok_trace and (t.get("cost_cny") or 0) > 0
        record("观测", "trace + 嵌套 span 记录", ok_trace and spans_ok,
               f"span {t.get('span_count') if t else '?'} 个")
        record("观测", "父子关系正确（嵌套 span 挂对父节点）", parent_ok, "")
        record("观测", "usage 聚合 + 成本核算", usage_ok and cost_ok,
               f"cost=¥{t.get('cost_cny') if t else '?'}")
    except Exception as e:

        record("观测", "trace 记录", False, brief(e))

    # ---- 1.5 ★ 嵌套 usage 字段不能破坏归并（踩过的坑，固化成断言）----
    #   DeepSeek 的 usage 里有 `prompt_tokens_details`（嵌套 dict）。
    #   第一版归并写的是 `parent.usage[k] = parent.usage.get(k, 0) + v`，
    #   遍历到嵌套字段时 `0 + dict` 抛 TypeError，被吞掉 → **归并半途中断**：
    #   前三个字段（prompt/completion/total）正常，后面的缓存字段全丢，
    #   于是节点成本按"全部未命中"算，看板数字虚高。
    try:
        with tracer.trace("smoke-nested", source="selftest") as tid2:
            with tracer.span(tracer.TYPE_AGENT, name="node"):
                with tracer.span(tracer.TYPE_LLM, name="chat_step") as lsp:
                    lsp.set_usage({
                        "prompt_tokens": 1004, "completion_tokens": 231,
                        "total_tokens": 1235,
                        "prompt_tokens_details": {"cached_tokens": 768},   # ← 嵌套 dict
                        "prompt_cache_hit_tokens": 768,
                        "prompt_cache_miss_tokens": 236,
                    })
        t2 = next((x for x in tracer.recent_traces(limit=5, source="selftest")
                   if x["trace_id"] == tid2), None)
        node = next((s for s in (t2 or {}).get("spans", [])
                     if s.get("name") == "node"), None)
        nu = (node or {}).get("usage") or {}
        # ★ 关键断言：缓存字段必须一起归并上来（正是被吞掉的那部分）
        nested_ok = (nu.get("prompt_cache_hit_tokens") == 768
                     and nu.get("prompt_cache_miss_tokens") == 236
                     and nu.get("completion_tokens") == 231)
        record("观测", "嵌套 usage 字段不破坏归并", nested_ok,
               f"缓存字段归并成功（hit={nu.get('prompt_cache_hit_tokens')}）"
               if nested_ok else f"缓存字段丢失：{nu}")
    except Exception as e:
        record("观测", "嵌套 usage 字段归并", False, brief(e))

    # ---- 2. 异常会被记下来且原样抛出（观测绝不吞业务异常） ----
    raised, recorded = False, False
    try:
        with tracer.trace("smoke-err", question="", source="selftest"):
            with tracer.span(tracer.TYPE_AGENT, name="boom"):
                raise ValueError("故意抛出")
    except ValueError:
        raised = True
    if raised:
        bad = [t for t in tracer.recent_traces(limit=5, source="selftest")
               if t["name"] == "smoke-err"]
        recorded = bool(bad) and bad[0].get("status") == "error"
    record("观测", "异常：记录状态且原样抛出", raised and recorded,
           "业务异常不被吞掉" if raised else "")

    # ---- 3. 无 trace 上下文时静默跳过（不崩、不报错） ----
    try:
        with tracer.span(tracer.TYPE_TOOL, name="orphan"):
            pass
        record("观测", "无 trace 时 span 静默跳过", True, "_NullSpan")
    except Exception as e:
        record("观测", "无 trace 时的 span", False, brief(e))

    # ---- 4. 成本：缓存命中折扣要算进去 ----
    # ★ 这里不再硬编码总价。原先断言 `abs(c - 0.0048) < 1e-9` ——
    #   而 0.0048 是按**当时的价表**手算出来的，价格一核对（本次就把缓存命中价
    #   从 ¥0.5 改成 ¥0.02，差 25 倍）这条自检就红，而它想验的其实是
    #   「折扣有没有生效」，不是「总价等于多少」。
    #   **断言要盯住"性质"，不要盯住"当时算出来的那个数"。**
    import datetime as _dt
    at = _dt.datetime(2026, 9, 28, 3, 0, tzinfo=costs.BEIJING)   # 固定为空闲时段
    u = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
         "prompt_cache_miss_tokens": 200, "completion_tokens": 500}
    c = costs.cost_of(u, "deepseek-flash", at)
    c_flat = costs.cost_of({**u, "prompt_cache_hit_tokens": 0,
                            "prompt_cache_miss_tokens": 1000},
                           "deepseek-flash", at)
    saved = (1 - c / c_flat) if c_flat else 0.0
    record("观测", "成本核算（缓存命中折扣生效）", c > 0 and saved > 0.2,
           f"¥{c:.6f}，比全部按未命中算省 {saved:.0%}")

    # 别名归一：请求名与实际服务的模型必须落到同一笔账上
    alias_ok = abs(costs.cost_of(u, "deepseek-chat", at) - c) < 1e-12
    record("观测", "成本按返回的模型计价（别名归一）", alias_ok,
           "deepseek-chat → deepseek-flash，同一笔账")

    # ---- 5. Langfuse 导出 payload 形状（离线构造，不联网） ----
    fake = {"trace_id": "tr-x", "name": "t", "question": "q",
            "started_at": "2026-09-26T20:00:00", "elapsed_ms": 100,
            "status": "ok", "cost_cny": 0.001,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "span_count": 1,
            "spans": [{"span_id": "sp-1", "parent_id": None, "type": "llm",
                       "name": "chat_step", "started_at": "2026-09-26T20:00:00",
                       "elapsed_ms": 90, "status": "ok",
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "attrs": {"model": "deepseek-chat"}}]}
    try:
        events = langfuse_export.build_ingestion_events(fake)
        types = [e["type"] for e in events]
        gen = next(e for e in events if e["type"] == "generation-create")
        shape_ok = ("trace-create" in types
                    and gen["body"]["traceId"] == "tr-x"
                    and gen["body"]["usage"]["total"] == 15)
        record("观测", "Langfuse payload 构造（离线）", shape_ok,
               f"事件 {len(events)} 条：{','.join(types)}")
    except Exception as e:
        record("观测", "Langfuse payload", False, brief(e))

    info = langfuse_export.describe()
    record("观测", f"导出器 {'启用' if info['enabled'] else '未启用（本地记录模式）'}",
           True, info["note"][:60])


# ============================================================
# 第七层：评测（离线自检评测资产本身，不花钱）
# ============================================================
def check_evaluation(full: bool = False):
    print("\n[7/9] 评测　—— 评测集 + 判定器（离线部分不花钱）")

    # ---- 1. 评测集能载入，且标注完整 ----
    try:
        import json
        from collections import Counter

        data = json.loads((PROJECT_ROOT / "eval" / "rag_eval_set.json")
                          .read_text(encoding="utf-8"))
        cases = data["cases"]
        scopes = Counter(c["scope"] for c in cases)
        cats = Counter(c["category"] for c in cases)

        # ★ 标注完整性：库内题必须有标准文档，库外题必须没有。
        #   标错了会直接污染指标 —— 而这类错误**不会报错**，
        #   只会让分数悄悄变得不可信。所以用断言守住。
        bad = []
        for c in cases:
            if c["scope"] == "in" and not c.get("doc"):
                bad.append(f"{c['id']} 库内却无标准文档")
            if c["scope"] == "out" and c.get("doc"):
                bad.append(f"{c['id']} 库外却有标准文档")

        record("评测", f"评测集载入（{len(cases)} 条）", not bad,
               f"库内 {scopes['in']} / 库外 {scopes['out']}"
               f"　词面 {cats['word']} · 语义 {cats['semantic']} · 库外 {cats['out']}"
               + ("" if not bad else f"　标注问题：{bad[:3]}"))
    except Exception as e:
        record("评测", "评测集载入", False, brief(e))

    # ---- 2. 规则判定器：正例反例都要对 ----
    # ★ 判定器是"尺子"。尺子不准，量出来的数字全是假的。
    #   所以这里用**已知答案**的正反例去测它，两边都要判对。
    try:
        from app.evaluation import judges as J

        ctx = ("[1] 来源：docker-exit-code.md\n"
               "| 137 | 被 SIGKILL 杀掉（128+9）| 被 OOM Killer 杀 |\n"
               "退出码 143 表示收到 SIGTERM（128+15）")

        cite_cases = [
            ("引用 [1][2] 且范围内 → 应通过", "[1][2]", 5, True),
            ("引用 [7] 超出范围 → 应发现", "[7]", 5, False),
            ("一个引用都不给 → 应发现", "无", 5, False),
            ("正文里的 [137] 不是引用 → 应通过", "[1][137]", 5, True),
        ]
        bad_c = 0
        for label, tail, n_hits, want in cite_cases:
            got = J.check_citations(f"结论是 OOM。{tail}", n_hits)["ok"]
            if got != want:
                bad_c += 1
                print(f"      ✗ {label}：期望 {want}，实际 {got}")
        record("评测", f"引用判定 {len(cite_cases)} 个正反例", bad_c == 0,
               "正反例全对" if bad_c == 0 else f"{bad_c} 个不符预期")

        num_cases = [
            ("资料里有的数字 → 应通过", "退出码 137 表示被 SIGKILL", True),
            ("资料里没有的数字 → 应发现", "退出码 139 表示段错误", False),
            ("主机名里的数字不算 → 应通过", "web-01 上的容器", True),
        ]
        bad_n = sum(1 for _, a, w in num_cases
                    if J.check_numbers(a, ctx)["clean"] != w)
        for label, a, w in num_cases:
            got = J.check_numbers(a, ctx)["clean"]
            if got != w:
                print(f"      ✗ {label}：期望 {w}，实际 {got}")
        record("评测", f"数值判定 {len(num_cases)} 个正反例", bad_n == 0,
               "正反例全对" if bad_n == 0 else f"{bad_n} 个不符预期")

        # 这两条长文本是**第一轮评测的真实误报原文**，固化成断言守着。
        # 当时 40 条里有 7 条被判成"明明有资料却拒答"，翻开一看全是
        # 正常回答末尾的「补充说明」—— 判定规则的作用范围划错了。
        long_answer = (
            "# Nginx 502 排查\n\n## 结论\n`upstream timed out` 表示上游处理超时，"
            "超过 `proxy_read_timeout`（默认 60 秒）。[1][2]\n\n"
            "## 处理方向\n- 查上游慢在哪里；必要时调大 `proxy_read_timeout`。[1]\n"
            "- 根治思路：上游慢 → 加缓存、拆接口、加机器。[2]\n\n"
            "## 补充说明\n\n参考资料中未出现相互冲突的内容。"
            "需要注意的是，`upstream timed out` 也可能是上游内存不足导致的。[3]"
        )
        abs_cases = [
            ("明确拒答 → 应识别",
             "知识库中没有相关内容，无法回答该问题。", True),
            ("正常作答 → 不应误判",
             "先执行 df -h 看哪个分区满了。", False),
            ("普通的「没有」→ 不应误判",
             "如果进程没有起来，先看日志。", False),
            # ★ 回归：正常回答**末尾**的补充说明里出现「参考资料中未」，不算拒答
            ("有实质内容 + 末尾边界声明 → 不应误判为拒答", long_answer, False),
            # ★ 回归：短回答里出现弱句式，没有实质内容 → 算拒答
            ("短回答 + 资料中未提及 → 应识别为拒答",
             "资料中没有提及这个内容，无法回答。", True),
        ]
        bad_a = sum(1 for _, a, w in abs_cases
                    if J.check_abstention(a)["abstained"] != w)
        for label, a, w in abs_cases:
            got = J.check_abstention(a)["abstained"]
            if got != w:
                print(f"      ✗ {label}：期望 {w}，实际 {got}")
        record("评测", f"拒答判定 {len(abs_cases)} 个正反例", bad_a == 0,
               "正反例全对（含「末尾补充说明不算拒答」的回归用例）"
               if bad_a == 0 else f"{bad_a} 个不符预期")
    except Exception as e:
        record("评测", "规则判定器", False, brief(e))

    # ---- 3. 模型判定器：花钱项，只在 --full 下跑 ----
    if not full:
        record("评测", "判定器自校验（3 次模型调用）", False,
               "加 --full 才会跑：python scripts/smoke_test.py --full",
               skipped=True)
        return

    try:
        from app.evaluation import judges as J
        v = J.verify_judge(verbose=False)
        record("评测", f"★ 判定器自校验 {v['passed']}/{v['total']}", v["ok"],
               "好答案 / 跑题 / 编造 三类都能判对" if v["ok"]
               else f"判定器不可信：{[c['name'] for c in v['cases'] if not c['ok']]}")
    except Exception as e:
        record("评测", "判定器自校验", False, brief(e))


# ============================================================
# 第八层：MCP Server（把工具暴露成标准协议）
# ============================================================
def check_mcp(full: bool = False):
    print("\n[8/9] MCP Server　—— 工具的标准协议出口")

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
# 第九层：HTTP 服务（需要服务已在运行）
# ============================================================
def check_http(full: bool = False):
    # ★ 标题里不再写死接口数量。
    #   原先写「22 个接口」，实际只探了 12 个 —— 典型的"标题比内容好看"，
    #   而且会让人以为 22 个都验证过了。真实条数在末尾按实际记录算出来。
    print("\n[9/9] HTTP 服务　—— 22 个接口里抽测（末尾给出实际条数）")

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

    # ---- 下面六个都是只读的观测/状态接口，免费，没理由不测 ----
    # ★ 补这一段的起因：本层原先标题写着「22 个接口」，实际只探了 12 个，
    #   而 /sandbox、/approvals、/traces、/metrics/summary 这些**一个都没测**。
    #   它们恰恰是最容易写坏又最不容易被发现的（聚合口径、状态字段）。
    #   **自检覆盖不到的地方，就是下次出问题的地方。**

    # GET /sandbox —— 顺便验 fail-closed 三件事都如实报出来
    try:
        r = httpx.get(f"{BASE_URL}/sandbox", timeout=15)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        record("接口", "GET  /sandbox（沙箱状态）", ok,
               f"后端 {d.get('backend')}／隔离 {d.get('isolated')}"
               f"／docker 可用 {d.get('docker_available')}" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "GET  /sandbox", False, brief(e))

    # GET /metrics/summary —— 顺便验成本对账（这两个维度不能相加）
    try:
        r = httpx.get(f"{BASE_URL}/metrics/summary", params={"limit": 20},
                      timeout=30)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        rec = d.get("reconcile") or {}
        gap = max(abs(rec.get("by_type_gap") or 0),
                  abs(rec.get("by_name_gap") or 0))
        note = (f"{d.get('runs')} 次运行 · ¥{d.get('cost_cny')}"
                f" · 对账偏差 {gap:.1%}")
        if d.get("unpriced_models"):
            note += f" · ⚠ 未定价模型 {d['unpriced_models']}"
        record("接口", "GET  /metrics/summary（成本看板）", ok, note)
    except Exception as e:
        record("接口", "GET  /metrics/summary", False, brief(e))

    # GET /traces + GET /traces/{id}
    trace_id = None
    try:
        r = httpx.get(f"{BASE_URL}/traces", params={"limit": 3}, timeout=20)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        items = d.get("items") or []
        trace_id = items[0].get("trace_id") if items else None
        # 列表视图不该带 spans（几十个 span 会把列表页撑爆）
        leaked = any("spans" in it for it in items)
        record("接口", "GET  /traces（运行列表）", ok and not leaked,
               f"{d.get('count')} 条，列表不含 spans" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "GET  /traces", False, brief(e))

    if trace_id:
        try:
            r = httpx.get(f"{BASE_URL}/traces/{trace_id}", timeout=20)
            ok = r.status_code == 200
            d = r.json() if ok else {}
            record("接口", "GET  /traces/{id}（单次详情）", ok,
                   f"{d.get('span_count')} 个 span · 用时 {d.get('elapsed_ms')}ms"
                   if ok else f"HTTP {r.status_code}")
        except Exception as e:
            record("接口", "GET  /traces/{id}", False, brief(e))
    else:
        record("接口", "GET  /traces/{id}（无 trace 可查）", False,
               "还没有任何 trace，先跑一次 /chat 或 /agent/ask",
               skipped=True)

    # GET /approvals + GET /approvals/{id}
    approval_id = None
    try:
        r = httpx.get(f"{BASE_URL}/approvals", params={"limit": 3}, timeout=20)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        items = d.get("items") or []
        approval_id = items[0].get("id") if items else None
        counts = d.get("counts") or {}
        record("接口", "GET  /approvals（审批单）", ok,
               f"返回 {len(items)} 张 · 状态分布 {counts}" if ok
               else f"HTTP {r.status_code}")
    except Exception as e:
        record("接口", "GET  /approvals", False, brief(e))

    if approval_id:
        try:
            r = httpx.get(f"{BASE_URL}/approvals/{approval_id}", timeout=20)
            ok = r.status_code == 200
            d = r.json() if ok else {}
            record("接口", "GET  /approvals/{id}（单张详情）", ok,
                   f"状态 {d.get('status')} · 风险 {d.get('risk')}" if ok
                   else f"HTTP {r.status_code}")
        except Exception as e:
            record("接口", "GET  /approvals/{id}", False, brief(e))
    else:
        record("接口", "GET  /approvals/{id}（无审批单可查）", False,
               "还没有审批单，先让 Agent 提一次写操作", skipped=True)

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

    # ---- 如实报出本层实际探测了多少 ----
    # 这段是有意加的：标题曾写「22 个接口」而实际只探 12 个，
    # 于是"自检全绿"给人一种"22 个接口都验证过了"的错觉。
    # **让脚本自己数、自己说**，就不会再出现标题和事实对不上。
    probed = [r for r in results if r[0] == "接口"]
    ok_n = len([r for r in probed if r[2] and not r[4]])
    skip_n = len([r for r in probed if r[4]])
    print(f"      → 本层实际探测 {len(probed)} 个：通过 {ok_n}，跳过 {skip_n}")
    print("        OpenAPI 共 22 个，未覆盖的是**需要副作用的写操作**"
          "（/rag/index 与 approvals 的 approve / reject / execute）——")
    print("        它们在 security_check.py 的 23 项用例里单独验证，不在本层重复跑。")


# ============================================================
# 汇总
# ============================================================
def summarize():
    print("\n" + "=" * 62)
    print("  自检汇总")
    print("=" * 62)

    layers = ["环境", "模型", "检索", "问答", "工具", "编排", "校验", "沙箱", "审批", "观测", "评测", "MCP", "服务", "接口"]
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
        print("  ✅ 全部通过。九层技术栈都在工作。")
    print("=" * 62)
    return 1 if failed else 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="AgentDesk 全链路自检（九层：环境/模型/检索/Agent/沙箱/观测/评测/MCP/接口）")
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
             lambda: check_sandbox(args.full),
             lambda: check_observability(args.full),
             lambda: check_evaluation(args.full),
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
