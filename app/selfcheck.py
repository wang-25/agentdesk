# -*- coding: utf-8 -*-
"""`/healthz`：深度探依赖，**默认一分钱不花**。

【为什么 `/health` 不够】
`/health` 只回 `status: ok` —— 模型挂了、索引坏了、磁盘满了，它照样报 ok。
容器健康检查与负载均衡的存活探测靠它没问题（那是"进程还活着吗"），
但**"服务能不能干活"是另一个问题**，需要一个真的去碰一下依赖的接口。

两者**都保留**：`/health` 是给探针用的（秒回、零依赖），
`/healthz` 是给人排障用的（逐项明细，坏了会说清是哪一项）。

【为什么不默认真调一次模型】
那是**花钱的检查**，而探针会被监控系统每分钟调一次 ——
真调模型就成了"监控本身在烧钱"。所以默认改成看**痕迹**：
最近一次成功的模型调用距今多久（从 trace 里读，零成本）。
要真打就 `HEALTHZ_PROBE_MODEL=1`（那条路会明确标注它花钱）。

【每一项都要能单独失败】
排障最怕的是"健康检查说 503，但不说是哪儿坏了"。
所以下面每一项都独立返回 `{ok, detail}`，失败项会在 `failing` 列表里点名。
"""

import os
import shutil
import time
from pathlib import Path

from app.llm import PROJECT_ROOT

# 磁盘剩余低于这个值就认为"不健康"（日志与索引都写不下去了）
MIN_FREE_BYTES = 50 * 1024 * 1024
# 模型"多久没成功调用过"算异常。默认 24 小时 —— 静默期不一定有病，
# 所以这一项**只报不判**（不进 ok 的判定），但会出现在明细里。
MODEL_STALE_SECONDS = 24 * 3600


def _timed(fn):
    """跑一项检查并记录耗时。任何异常都变成 `{ok: False, detail}`，不让探针自己崩。"""
    started = time.time()
    try:
        result = fn()
        if not isinstance(result, dict):
            result = {"ok": bool(result), "detail": ""}
    except Exception as exc:                     # noqa: BLE001 —— 探针必须兜住一切
        result = {"ok": False,
                  "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
    result.setdefault("ok", False)
    result.setdefault("detail", "")
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


# ============================================================
# 各项检查
# ============================================================
def check_logs_writable(logs_dir: Path = None) -> dict:
    """日志目录能不能写 —— 写不进去就意味着审计、trace、审批全都在丢。"""
    logs_dir = Path(logs_dir) if logs_dir else PROJECT_ROOT / "logs"
    probe = logs_dir / ".healthz"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text(str(time.time()), encoding="utf-8")
        probe.unlink()
        return {"ok": True, "detail": f"{logs_dir} 可写"}
    except OSError as exc:
        return {"ok": False, "detail": f"{logs_dir} 不可写：{exc}"}


def check_index_loadable() -> dict:
    """RAG 索引能不能载入（**不重建** —— 探针不该有副作用，也不该花时间）。"""
    from app.rag import pipeline

    try:
        store = pipeline.load_store()
    except FileNotFoundError as exc:
        return {"ok": False, "detail": f"索引不存在（需要 build）：{exc}"}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "detail": f"索引不可用：{exc}"}
    return {"ok": True, "detail": f"{len(store.chunks)} 块 / "
                                  f"后端 {(store.embedder.describe() or {}).get('model', '?')}"}


def check_approvals_readable() -> dict:
    """审批单存储能不能读（顺带会触发一次过期折叠）。"""
    from app.sandbox import approvals

    counts = approvals.store().counts()
    return {"ok": True, "detail": f"审批单 {counts.get('total', 0)} 张"}


def check_incidents_readable() -> dict:
    """事件存储能不能读。"""
    from app.incident import store as incident_store

    counts = incident_store.store().counts()
    return {"ok": True, "detail": f"事件 {counts.get('total', 0)} 个"}


def check_notify_config() -> dict:
    """通知配置能不能解析。**不发请求** —— 那是 notify_check.py 的事。

    注意：**没配通知不算不健康**（默认就是不出站，那是有意选择，
    不是故障）。这里只在"点名了渠道却没配上 URL"时才算问题。
    """
    from app.notify import events

    raw = (os.getenv("NOTIFY_CHANNELS") or "").strip()
    info = events.describe()
    if not raw:
        return {"ok": True, "detail": "未配置通知（默认不出站）"}
    if info["enabled"]:
        return {"ok": True,
                "detail": f"渠道 {'、'.join(info['channels'])}"
                          f"　脱敏 {'开' if info['mask'] else '关'}"}
    return {"ok": False,
            "detail": f"NOTIFY_CHANNELS={raw!r} 点名了渠道，但没有一个配了 URL"}


def check_model_recent() -> dict:
    """最近一次成功的模型调用距今多久（**读 trace，不调模型**）。

    ★ 这一项**只报不判**：长时间没有模型调用可能只是没人用，
      不等于服务有病。把它写进明细是为了排障时一眼看到"模型层最后什么时候动过"。
    """
    from app.observability import tracer

    records = tracer.read_recent(limit=400)
    # ★ 过滤要**同时**看"是不是模型记录"和"有没有可用的时间戳"。
    #   踩过一次：写成 `max(str(s) for s in stamps if s)` ——
    #   当记录存在但 ts 全为空时，生成器是空的，`max()` 抛 ValueError，
    #   于是 /healthz 报出一个**假故障**（探针没崩，但说了假话）。
    #   "探针自己出错"与"依赖坏了"必须能区分开，这条同样是测试的职责。
    stamps = [str(r.get("ts")) for r in records
              if (r.get("type") == "llm" or r.get("name") == "chat") and r.get("ts")]
    if not stamps:
        return {"ok": True, "detail": "trace 里还没有模型调用记录",
                "informational": True, "last_call_ts": ""}
    last = max(stamps)
    age = _age_seconds(last)
    detail = f"最近一次模型调用：{last}"
    if age is not None:
        detail += f"（{int(age)} 秒前）"
        if age > MODEL_STALE_SECONDS:
            detail += "　⚠️ 已超过 24 小时"
    return {"ok": True, "detail": detail, "informational": True,
            "last_call_ts": last, "age_seconds": age}


def _age_seconds(iso: str):
    from datetime import datetime
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def check_disk_space(logs_dir: Path = None) -> dict:
    """日志所在卷的剩余空间。写满了会连带把审计/trace 全丢掉。"""
    target = Path(logs_dir) if logs_dir else PROJECT_ROOT / "logs"
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return {"ok": False, "detail": f"取不到磁盘用量：{exc}"}
    free_mb = usage.free / 1024 / 1024
    ok = usage.free >= MIN_FREE_BYTES
    return {"ok": ok, "informational": False,
            "detail": f"剩余 {free_mb:.0f} MB"
                      + ("" if ok else f"（低于 {MIN_FREE_BYTES // 1024 // 1024} MB）"),
            "free_bytes": usage.free}


def check_model_probe() -> dict:
    """**真的**调一次模型（花钱）。只有 `HEALTHZ_PROBE_MODEL=1` 时才会走到这里。"""
    from app import llm

    started = time.time()
    try:
        answer = llm.chat([{"role": "user", "content": "ping"}], timeout=15)
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "detail": f"模型不可用：{type(exc).__name__}: {exc}"}
    return {"ok": True, "detail": f"模型可用（{(time.time() - started) * 1000:.0f}ms）"
                                  f"　⚠️ 本次检查会产生费用"}


# ============================================================
# 汇总
# ============================================================
def probe(env: dict = None, logs_dir: Path = None) -> dict:
    """跑一遍全部检查，返回可直接作为 `/healthz` 响应体的字典。

    `ok` 只由**依赖不可用**决定；`informational` 的项（模型静默期）
    出现在明细里但不影响判定 —— **能干活**与**最近没人用过**是两件事。
    """
    env = os.environ if env is None else env
    checks = {
        "logs_writable": _timed(lambda: check_logs_writable(logs_dir)),
        "disk_space": _timed(lambda: check_disk_space(logs_dir)),
        "index_loadable": _timed(check_index_loadable),
        "approvals_readable": _timed(check_approvals_readable),
        "incidents_readable": _timed(check_incidents_readable),
        "notify_config": _timed(check_notify_config),
        "model_recent": _timed(check_model_recent),
    }
    if str(env.get("HEALTHZ_PROBE_MODEL", "0")).strip() == "1":
        checks["model_probe"] = _timed(check_model_probe)

    failing = [name for name, c in checks.items()
               if not c.get("ok") and not c.get("informational")]
    return {
        "ok": not failing,
        "failing": failing,
        "checks": checks,
        # ★ M4 的可见性要求：如实标出这些状态是**进程内**的
        "state_scope": "process",
        "state_note": ("限流 / 每日额度 / 告警去重都是**进程内**状态："
                       "单进程（--workers 1）下准确，多 worker 会各算各的。"
                       "换 Redis 时只需要替换 app/security.py 里的这几处计数。"),
    }
