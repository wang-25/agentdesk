# -*- coding: utf-8 -*-
"""
公网安全层：token 鉴权 + 三层限流 + 每日额度
============================================================
本地开发**默认关闭**（`AUTH_ENABLED=0`），部署到公网时打开。

开启方式（.env）：
    AUTH_ENABLED=1
    AGENT_TOKEN=<一串随机字符>
    RATE_LIMIT_PER_MIN=20          每 IP 每分钟
    RATE_LIMIT_GLOBAL_PER_MIN=60   全站每分钟（防伪造 IP 绕过）
    DAILY_QUOTA=300                每天最多多少次「花钱」调用

【为什么是白名单，不是黑名单】
黑名单（只保护 /agent/ask、/approvals/*）看着够用，但有个致命缺陷：
**以后新加的接口默认是公开的**。哪天加了个 /admin/reset 忘了登记，
就是一次静默越权。白名单反过来 —— 默认全部拒绝，只放行明确要公开的，
新接口天然是安全的。这是 fail-closed 的思路。

【为什么限流要做三层】
只按 IP 限流有个漏洞：X-Forwarded-For 是普通 HTTP 头，客户端可以随便写。
伪造不同 IP 就能绕开单 IP 限制。所以必须再加两层与客户端身份无关的保险：
    · 全局限流 —— 不管谁来的，全站每分钟就这么多
    · 每日额度 —— 不管什么时间窗口，一天就这么多
这样即使前两层都被绕过，账单也烧不穿。

【为什么 token 比较要用 secrets.compare_digest】
普通 == 比较会在第一个不同字符处短路返回，耗时随「匹配了多长的前缀」变化。
攻击者测量响应时间就能逐字节猜出 token。compare_digest 是常数时间比较，
堵住这个侧信道。

【为什么取 X-Forwarded-For 的最后一个值】
Nginx 用 $proxy_add_x_forwarded_for 追加，格式是「客户端自带的值, 真实IP」。
客户端伪造的部分会留在**左边**。取第一个等于把限流 key 交给攻击者控制；
取最后一个才是我们唯一信任的那跳代理（本机的 NPM）填的真实 IP。
"""

import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.llm import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

log = logging.getLogger("agentdesk.security")

# ============================================================
# 配置
# ============================================================
AUTH_ENABLED = (os.getenv("AUTH_ENABLED", "0").strip() == "1")
AGENT_TOKEN = (os.getenv("AGENT_TOKEN") or "").strip()

PER_IP_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
GLOBAL_PER_MIN = int(os.getenv("RATE_LIMIT_GLOBAL_PER_MIN", "60"))
DAILY_QUOTA = int(os.getenv("DAILY_QUOTA", "300"))

# 白名单：只有这些路径不需要 token。其余一律要求鉴权。
PUBLIC_EXACT = {"/", "/health", "/openapi.json", "/favicon.ico"}
PUBLIC_PREFIX = ("/docs", "/redoc")

# 「花钱」的路径：会调用模型或 embedding API。只有这些扣每日额度。
COST_PREFIX = (
    "/chat", "/parse", "/rag/ask", "/rag/search", "/rag/index",
    "/agent/ask", "/agent/graph", "/webhook/alert",
)

# ============================================================
# 计数器（进程内）
# ============================================================
# 【为什么可以用进程内内存计数】
# 部署时用 --workers 1 单进程，所有请求都在同一个进程里，
# 内存计数就是准的。如果哪天开了多 worker，这套要换成 Redis ——
# 否则每个 worker 各算各的，实际额度会翻倍。
_lock = threading.Lock()
_ip_hits = defaultdict(deque)
_global_hits = deque()
_daily = {"date": "", "count": 0}
_rejected = {"auth": 0, "rate": 0, "quota": 0}


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。详见模块开头关于 XFF 取值的说明。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


def _check_rate(ip: str):
    """滑动窗口限流。返回 None 表示通过，否则返回超限原因。"""
    now = time.time()

    with _lock:
        # --- 全局限流 ---
        while _global_hits and _global_hits[0] < now - 60:
            _global_hits.popleft()
        if len(_global_hits) >= GLOBAL_PER_MIN:
            return "global"

        # --- 每 IP 限流 ---
        q = _ip_hits[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= PER_IP_PER_MIN:
            return "ip"

        _global_hits.append(now)
        q.append(now)
        return None


def _check_quota():
    """扣一次每日额度。返回 (是否放行, 剩余次数)。"""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock:
        if _daily["date"] != today:
            _daily["date"] = today
            _daily["count"] = 0
        if DAILY_QUOTA > 0 and _daily["count"] >= DAILY_QUOTA:
            return False, 0
        _daily["count"] += 1
        return True, max(DAILY_QUOTA - _daily["count"], 0)


def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)


def _is_cost_path(path: str) -> bool:
    return path.startswith(COST_PREFIX)


def _extract_token(request: Request) -> str:
    """支持两种传法：X-API-Key 头，或 Authorization: Bearer。"""
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def security_status() -> dict:
    """给 /health 用：暴露安全层的当前状态，方便线上排查。"""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock:
        used = _daily["count"] if _daily["date"] == today else 0
        window = len(_global_hits)
    return {
        "auth_enabled": AUTH_ENABLED,
        "token_configured": bool(AGENT_TOKEN),
        "rate_limit_per_ip_per_min": PER_IP_PER_MIN,
        "rate_limit_global_per_min": GLOBAL_PER_MIN,
        "daily_quota": DAILY_QUOTA,
        "daily_used": used,
        "requests_last_minute": window,
        "rejected": dict(_rejected),
    }


# ============================================================
# 中间件
# ============================================================
def install_security(app: FastAPI) -> None:
    """把安全中间件挂到 app。在 main.py 里调用一次即可。"""

    if AUTH_ENABLED and not AGENT_TOKEN:
        # 【为什么要硬失败】
        # 打开了鉴权却忘了配 token，如果只是打个警告就继续跑，
        # 结果就是「以为有保护、其实裸奔」—— 比明知道没保护更危险。
        raise RuntimeError(
            "AUTH_ENABLED=1 但 AGENT_TOKEN 为空。请先设置一个足够长的随机 token，"
            "或把 AUTH_ENABLED 改回 0。\n"
            "生成方式：python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )

    @app.middleware("http")
    async def _security_middleware(request: Request, call_next):
        path = request.url.path

        # 未开启鉴权（本地开发）直接放行，行为与以前完全一致
        if not AUTH_ENABLED:
            return await call_next(request)

        if _is_public(path):
            return await call_next(request)

        ip = _client_ip(request)

        # ---------- 第 1 层：token 鉴权 ----------
        supplied = _extract_token(request)
        if not supplied or not secrets.compare_digest(supplied, AGENT_TOKEN):
            with _lock:
                _rejected["auth"] += 1
            log.warning("鉴权失败 path=%s ip=%s", path, ip)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "需要有效的 API token。请在 X-API-Key 头中提供。",
                    "hint": "本地调试可用：curl -H 'X-API-Key: <token>' ...",
                },
            )

        # ---------- 第 2 层：限流 ----------
        reason = _check_rate(ip)
        if reason:
            with _lock:
                _rejected["rate"] += 1
            log.warning("限流拦截 reason=%s path=%s ip=%s", reason, path, ip)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "请求过于频繁，请稍后再试。",
                    "scope": "全站" if reason == "global" else "当前 IP",
                    "limit_per_minute": (GLOBAL_PER_MIN if reason == "global"
                                         else PER_IP_PER_MIN),
                },
                headers={"Retry-After": "60"},
            )

        # ---------- 第 3 层：每日额度（只对花钱接口扣） ----------
        if _is_cost_path(path):
            ok, remain = _check_quota()
            if not ok:
                with _lock:
                    _rejected["quota"] += 1
                log.warning("每日额度用尽 path=%s ip=%s", path, ip)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "今日调用额度已用尽，请明天再试。",
                        "daily_quota": DAILY_QUOTA,
                    },
                )
            response = await call_next(request)
            response.headers["X-DailyQuota-Remaining"] = str(remain)
            return response

        response = await call_next(request)
        remaining = max(GLOBAL_PER_MIN - len(_global_hits), 0)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
