# -*- coding: utf-8 -*-
"""
Langfuse 导出层（可选）
============================================================
配置了 Key 就把 trace 推给 Langfuse 面板；没配就什么都不发生。

【为什么用原生 HTTP 而不是 langfuse SDK】

和手写模型调用是同一个理由：SDK 是黑盒。
Langfuse 的 ingestion 协议就是一个 POST + Basic Auth + 一组
约定好的 event 结构 —— 手写一遍，trace 怎么映射成 Langfuse 的
trace/span/generation 你就亲眼见过，出问题时知道查哪里。
代价是多写 60 行，收益是**这个文件没有任何新依赖**。

【为什么只在 trace 结束时整批推，而不是逐 span 推】

一次诊断会产生十几个 span。逐个推 = 十几次 HTTP 请求，
其中大半是白花的（客户端批量攒一批是同样的道理）。
**导出是旁路，不该影响主链路的请求数。**

【为什么放后台线程】

导出失败/超时不能拖慢用户的响应。同步推的话，
Langfuse 挂了 = 每次 Agent 调用都多等一个超时 —— 那是事故。
"""

import base64
import json
import os
import threading
import time
from datetime import datetime, timezone

import httpx

from app.observability import tracer

HOST = (os.getenv("LANGFUSE_HOST") or "").strip().rstrip("/")
PUBLIC_KEY = (os.getenv("LANGFUSE_PUBLIC_KEY") or "").strip()
SECRET_KEY = (os.getenv("LANGFUSE_SECRET_KEY") or "").strip()
TIMEOUT = 5.0


def enabled() -> bool:
    """三个都配了才算启用。少一个就静默关闭 ——
    半配置状态比关闭更糟（你会以为在导出，其实一直失败）。"""
    return bool(HOST and PUBLIC_KEY and SECRET_KEY)


def describe() -> dict:
    return {
        "enabled": enabled(),
        "host": HOST or None,
        "public_key": (PUBLIC_KEY[:8] + "…") if PUBLIC_KEY else None,
        "note": "" if enabled()
        else "未配置。在 .env 填 LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / "
             "LANGFUSE_SECRET_KEY 后重启服务即启用（本地记录不受影响）",
    }


# ============================================================
# payload 构造（可以离线测试的部分）
# ============================================================
def _ts(iso: str) -> str:
    """Langfuse 要 ISO8601 UTC。本地记录是本地时间，转一下。"""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_ingestion_events(record: dict) -> list:
    """把本项目的 trace 记录翻译成 Langfuse ingestion 事件。

    ★ 这份映射就是"我们的 trace 模型"到"Langfuse 模型"的翻译层：

        本项目              Langfuse
        ───────────────     ─────────────────
        trace               trace-create
        span(type=llm)      generation-create（带 model + usage）
        span(其他)           span-create
        parent_id           parentObservationId

    概念对不上的时候，宁可丢掉也不要硬塞 ——
    比如「处置等待人工审批」这种跨度几分钟的时段，硬塞进 span
    会在面板上画出一条极长的假耗时。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    tid = record["trace_id"]
    usage = record.get("usage") or {}
    attrs = record.get("attrs") or {}

    events = [{
        "id": tid,
        "type": "trace-create",
        "timestamp": _ts(record.get("started_at")),
        "body": {
            "id": tid,
            "name": record.get("name") or "agentdesk.run",
            "timestamp": _ts(record.get("started_at")),
            "input": record.get("question") or "",
            "metadata": {
                "engine": attrs.get("engine"),
                "status": record.get("status"),
                "cost_cny": record.get("cost_cny"),
                "span_count": record.get("span_count"),
                "elapsed_ms": record.get("elapsed_ms"),
            },
        },
    }]

    for sp in record.get("spans") or []:
        common = {
            "id": sp["span_id"],
            "traceId": tid,
            "parentObservationId": sp.get("parent_id"),
            "name": sp.get("name"),
            "startTime": _ts(sp.get("started_at")),
            "endTime": _ts(sp.get("started_at")),   # 结束时间用耗时推（见下）
            "level": "ERROR" if sp.get("status") != "ok" else "DEFAULT",
            "metadata": {k: v for k, v in (sp.get("attrs") or {}).items()},
        }
        # 结束时间：本地只记了 elapsed_ms，用它推算 endTime。
        # 有毫秒级精度损失，但换来记录层不用存两个时间戳 —— 划算。
        try:
            start = datetime.fromisoformat(sp["started_at"]).astimezone(timezone.utc)
            end_ts = (start.timestamp() + (sp.get("elapsed_ms") or 0) / 1000)
            common["endTime"] = datetime.fromtimestamp(
                end_ts, tz=timezone.utc).isoformat(timespec="milliseconds")
        except Exception:
            pass

        if sp.get("type") == "llm":
            su = sp.get("usage") or {}
            # total 缺失时自己算 —— 别信上游一定给全字段
            total = su.get("total_tokens")
            if total is None:
                total = (su.get("prompt_tokens") or 0) + (su.get("completion_tokens") or 0)
            common["type"] = "generation-create"
            common["body"] = {
                **common,
                "model": sp.get("attrs", {}).get("model") or "deepseek-chat",
                "usage": {
                    "input": su.get("prompt_tokens", 0),
                    "output": su.get("completion_tokens", 0),
                    "total": total,
                    "inputCached": su.get("prompt_cache_hit_tokens", 0),
                },
            }
        else:
            common["type"] = "span-create"
            common["body"] = common

        events.append(common)

    return events


# ============================================================
# 推送
# ============================================================
def _auth_header() -> str:
    raw = f"{PUBLIC_KEY}:{SECRET_KEY}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def export_trace(record: dict) -> None:
    """在后台线程推一条 trace。失败只计数，不抛出、不重试。"""
    if not enabled():
        return

    def _run():
        try:
            events = build_ingestion_events(record)
            resp = httpx.post(
                f"{HOST}/api/public/ingestion",
                content=json.dumps(events, ensure_ascii=False).encode(),
                headers={
                    "Authorization": _auth_header(),
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
            )
            if resp.status_code >= 300:
                tracer.note_export_failure()
        except Exception:
            tracer.note_export_failure()

    threading.Thread(target=_run, daemon=True, name="langfuse-export").start()


def install() -> bool:
    """在服务启动时调用。返回是否启用。"""
    if not enabled():
        return False
    tracer.set_export_hook(export_trace)
    return True
