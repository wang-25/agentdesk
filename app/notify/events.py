# -*- coding: utf-8 -*-
"""通知策略：**唯一**决定"什么时候发、发什么内容"的地方。

【为什么要有这个文件（而不是在各处直接调 dispatcher）】
`build_dispatcher()` 回答"往哪发"，这个文件回答"**该不该发、发什么**"。
如果把这个判断散在 main.py、ops.py 里，那"哪些事件会通知人"就变成一件
要靠 grep 才能回答的事 —— 而它恰恰是值班体验的核心：
**少发一条会漏故障，多发一条会让人开始无视通知。**

所以三个使用场景都收在这里，各自一个函数：

    approval_created(rec)          有一条写操作等你批准
    approval_executed(rec, result) 你批的那条执行完了（成功/失败都发）
    incident(report, ...)          告警来了、以及它的诊断结论

【三条纪律】
1. **永不抛异常。** 通知是旁路：推不出去只能变成一条失败记录，
   绝不能让"推不出去"变成"诊断结果丢了"。
2. **未配置就什么都不做。** `build_dispatcher()` 返回 None 时全部是空操作，
   所以"没配通知"与"加了这一层之前"的行为逐字段一致。
3. **审计由主链路写。** 本模块通过 `set_audit_hook()` 拿到写审计的函数
   （main.py 在 import 时注入），自己不 import main（那会循环导入），
   也不自己决定审计格式。**没有 hook 时不写审计，但照样发通知** ——
   通知的可达性不该依赖审计层是否就绪。
"""

import threading

from app.notify.base import mask, mask_enabled
from app.notify.dispatcher import build_dispatcher

# 审计钩子：main.py import 时注入 write_audit
_audit_hook = None
_dispatcher = None
_dispatcher_built = False
_lock = threading.RLock()


def set_audit_hook(fn) -> None:
    """注入写审计的函数（签名 `(event: str, detail: dict) -> dict`）。"""
    global _audit_hook
    _audit_hook = fn


def _audit(event: str, detail: dict) -> None:
    if _audit_hook is None:
        return
    try:
        _audit_hook(event, detail)
    except Exception:                      # pragma: no cover
        # 审计写不进去也不能影响通知本身，更不能影响主链路
        pass


def dispatcher(env: dict | None = None):
    """拿到装配好的 dispatcher（进程内缓存一次）。

    ★ 缓存是刻意的：`build_dispatcher()` 每次都会重新解析环境变量、
      重新构造渠道对象。放在请求路径上，等于每条告警都重建一遍。
      测试要换配置时调 `reset()`。
    """
    global _dispatcher, _dispatcher_built
    with _lock:
        if not _dispatcher_built:
            _dispatcher = build_dispatcher(env)
            _dispatcher_built = True
        return _dispatcher


def reset() -> None:
    """丢掉缓存的 dispatcher（测试用；也用于运行时改完 .env 后重新装配）。"""
    global _dispatcher, _dispatcher_built
    with _lock:
        _dispatcher = None
        _dispatcher_built = False


def _safe(text) -> str:
    """出站前过一遍脱敏（`NOTIFY_MASK=1`，默认开）。"""
    if text is None:
        return ""
    text = str(text)
    return mask(text) if mask_enabled() else text


def send(title: str, text: str, *, key: str, payload: dict | None = None,
         event: str = "notify.sent"):
    """发一条通知并留痕。**永不抛异常**；未配置时返回 None。"""
    d = dispatcher()
    if d is None:
        return None
    try:
        outcome = d.notify(title, _safe(text), key=key, payload=payload)
    except Exception as exc:               # pragma: no cover - dispatcher 已兜底
        _audit("notify.failed", {"key": key, "error": f"{type(exc).__name__}: {exc}"})
        return None

    detail = {"key": key, "title": title, **outcome.to_dict()}
    if outcome.deduped:
        _audit("notify.deduped", detail)
    else:
        _audit(event if outcome.ok else "notify.failed", detail)
    return outcome


# ============================================================
# 一、审批链路
# ============================================================
def approval_created(rec: dict):
    """有一条写操作等人批准 —— 这条必须发出去。

    理由：审批单是**卡在人身上**的。没人知道它存在，它就会一直躺着，
    30 分钟后过期（`DEFAULT_TTL_SECONDS`），而告警那边还在等处置。
    "有一张单子在等你"是这套系统里最值得打扰人的一件事。
    """
    rid = rec.get("id", "?")
    host_channel = (rec.get("isolation") or "") == "host"
    text = (
        f"**需要人工确认**\n\n"
        f"- 审批单：`{rid}`\n"
        f"- 命令：`{rec.get('command', '')}`\n"
        f"- 风险：{rec.get('risk', '?')}（隔离通道 {rec.get('isolation', '?')}）\n"
        f"- 理由：{rec.get('reason', '')}\n"
        f"- 过期：{rec.get('expires_at', '?')}\n\n"
        f"批准后才会执行：`POST /approvals/{rid}/approve`"
    )
    if host_channel:
        # ★ 审批人有权知道"这条命令没有容器隔离"（M3 C7）。
        #   把隔离承诺说得比实际大，是审批界面最不该犯的错 ——
        #   人是在这个信息上做风险判断的。
        text += ("\n\n> ⚠️ 这条命令走**主机通道**：它在目标主机上直接执行，"
                 "**没有容器隔离**（重启类命令必须如此）。约束来自审批 + 指纹 + 审计。")
    return send(f"[AgentDesk] 待审批：{rec.get('command', '')[:60]}", text,
                key=f"approval:{rid}",
                payload={"approval_id": rid, "risk": rec.get("risk"),
                         "isolation": rec.get("isolation"),
                         "host": rec.get("host")})


def approval_executed(rec: dict, result: dict):
    """你批的那条执行完了 —— 成功和失败都要发。

    ★ 失败的更要发：原来 `approvals.jsonl` 里那条 `result_ok` 是在执行**之前**
      写下的（见 audit 报告 C4），所以"审批记录说成功、实际失败"这件事
      只能靠通知把真值送到人手上。
    """
    rid = rec.get("id", "?")
    ok = bool(result.get("ok"))
    head = "已执行完成" if ok else "**执行失败**"
    text = (
        f"**{head}**\n\n"
        f"- 审批单：`{rid}`（批准人 {rec.get('approved_by', '?')}）\n"
        f"- 命令：`{rec.get('command', '')}`\n"
        f"- 退出码：{result.get('exit_code')}　后端：{result.get('backend')}\n"
        f"- 隔离：{result.get('isolated')}　耗时：{result.get('elapsed_ms')}ms\n"
    )
    if not ok:
        text += f"- 错误：{result.get('error') or '（无错误信息）'}\n"
    return send(f"[AgentDesk] 审批 {rid} {'成功' if ok else '失败'}", text,
                key=f"approval-exec:{rid}",
                payload={"approval_id": rid, "ok": ok,
                         "exit_code": result.get("exit_code")},
                event="notify.sent")


# ============================================================
# 二、告警 / 事件链路
# ============================================================
def incident(report: dict, incident_id: str = "", members: int = 1, *,
             kind: str = "alert"):
    """告警处理结果（含诊断结论）推给值班的人。

    ★ 这是整个 M2 存在的理由：告警链路的终点原来只有"HTTP 响应 + audit.jsonl"，
      凌晨三点**没有任何人会被叫醒**。诊断做得再对，送不到人手上就等于没做。

    kind:
        alert    告警进来后的判定（可能是预案、可能转人工、可能正在诊断）
        diagnose 自动诊断跑完了（带结论）
    """
    decision = report.get("decision", "?")
    head = {
        "auto_diagnosed": "自动诊断完成",
        "need_human": "**需要人工介入**",
        "playbook_only": "已生成处置预案（未诊断）",
        "parse_failed": "意图解析失败",
        "diagnose_failed": "自动诊断失败",
        "rate_limited": "已触发频控（转人工）",
    }.get(decision, decision)

    lines = [
        f"**{head}**",
        "",
        f"- 告警：`{report.get('alertname', '?')}`",
        f"- 主机：{report.get('host') or '（未指定）'}"
        f"　级别：{report.get('severity', '?')}",
    ]
    if incident_id:
        # ★ 措辞刻意写成"本次为第 N 条"而不是"同源告警 N 条"：
        #   通知是在**第一条**告警处理完就发出去的，此刻后面那些同源告警还没进来。
        #   写"同源告警 1 条"虽然当时为真，但读的人会以为整个事件只有一条告警 ——
        #   而实际上它通常会继续并入几十条。**数字要说清楚它是哪个时刻的数字。**
        lines.append(f"- 事件：`{incident_id}`（同源告警并入本事件，本次为第 {members} 条）")
    if report.get("reason"):
        lines.append(f"- 判定：{report['reason']}")
    if report.get("answer"):
        lines += ["", "**诊断结论**", "", str(report["answer"])]
    if report.get("playbook"):
        lines += ["", "**处置预案（需人工执行）**", ""]
        lines += [f"{i}. `{cmd}`" for i, cmd in enumerate(report["playbook"], 1)]
    if decision == "need_human":
        lines += ["", "写操作永不自动执行 —— 请到 `/approvals` 处理。"]

    key = f"incident:{incident_id}" if incident_id else \
        f"alert:{report.get('alertname')}:{report.get('host')}"
    return send(f"[AgentDesk] {report.get('alertname', '告警')} @ "
                f"{report.get('host') or 'unknown'}", "\n".join(lines),
                key=key,
                payload={"decision": decision, "host": report.get("host"),
                         "severity": report.get("severity"),
                         "incident_id": incident_id})


def describe() -> dict:
    """给 /health 或设置页看的一句话：通知到底通没通、发去哪。"""
    d = dispatcher()
    return {
        "enabled": d is not None,
        "channels": (d.channels if d else []),
        "mask": mask_enabled(),
        "known_channels": list(_known()),
        "hint": ("未配置通知（完全不出站）。要打开见 .env.example 的「出站通知」一节"
                 if d is None else ""),
    }


def _known():
    from app.notify.dispatcher import KNOWN_CHANNELS
    return KNOWN_CHANNELS
