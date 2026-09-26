# -*- coding: utf-8 -*-
"""
全链路自检
============================================================
一条命令跑完四层检查，输出 ✅/❌ 清单 + 汇总表。

【为什么需要这个脚本】
技术栈是分层堆起来的：

    Python 环境  →  模型 API  →  检索(RAG)  →  HTTP 服务

出问题时必须自下而上逐层确认。手敲命令一个个试，容易漏、也容易看错。
这个脚本把四层一次性跑完，最后告诉你「哪一层是好的、哪一层断了」。

【运行】在 agentdesk 目录下：
    .venv\\Scripts\\python.exe scripts\\smoke_test.py

【注意】第 4 层需要服务已在运行。没运行会自动跳过，并提示启动命令。
        脚本会真实调用模型 3 次（约 ¥0.001），可忽略。

【退出码】0 = 全通；1 = 有失败项
"""

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
    print("\n[1/4] 运行环境　—— Python 与依赖包")

    v = sys.version_info
    record("环境", f"Python {v.major}.{v.minor}.{v.micro}",
           v >= (3, 10), "要求 >= 3.10，低于则语法会报错")

    # 逐个 import，缺哪个补哪个，而不是笼统说"依赖有问题"
    packages = ["httpx", "dotenv", "fastapi", "pydantic", "numpy", "jieba", "uvicorn"]
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
    print("\n[2/4] 模型连通　—— 真实发一次请求")

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
    print("\n[3/4] 检索与问答　—— RAG 全链路")

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
# 第四层：HTTP 服务（需要服务已在运行）
# ============================================================
def check_http():
    print("\n[4/4] HTTP 服务　—— 10 个接口")

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


# ============================================================
# 汇总
# ============================================================
def summarize():
    print("\n" + "=" * 62)
    print("  自检汇总")
    print("=" * 62)

    layers = ["环境", "模型", "检索", "问答", "服务", "接口"]
    for layer in layers:
        rows = [r for r in results if r[0] == layer]
        if not rows:
            continue
        skipped = sum(1 for r in rows if r[4])
        ok = sum(1 for r in rows if r[2] and not r[4])
        if skipped and ok == 0:
            print(f"  {layer:4s} ⏭ 跳过　{len(rows)} 项")
            continue
        bar = "█" * ok + "░" * (len(rows) - ok - skipped)
        print(f"  {layer:4s} {bar}  {ok}/{len(rows)}")

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
        print(f"  ⏭  有 {len(skipped)} 项被跳过（服务没在跑），启动服务后可复跑。")
    else:
        print("  ✅ 全部通过。四层技术栈都在工作。")
    print("=" * 62)
    return 1 if failed else 0


def main():
    print("=" * 62)
    print("  AgentDesk 全链路自检")
    print(f"  项目目录：{PROJECT_ROOT}")
    print("=" * 62)

    for step in (check_env, check_model, check_rag, check_http):
        try:
            step()
        except Exception as e:
            # 单个步骤意外崩了，不该拖垮整份报告
            record("脚本", step.__name__, False, brief(e))

    sys.exit(summarize())


if __name__ == "__main__":
    main()
