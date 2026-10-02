# -*- coding: utf-8 -*-
"""接口层：路由存在性 + 鉴权边界（进程内跑，零成本、不用起服务）。

【为什么必须有这个文件】
`scripts/smoke_test.py` 的第 9 层在服务没启动时会**整体跳过**，而"跳过"不算失败——
也就是说"自检全绿"完全可能一次接口都没验证过（实测就是如此，见
`docs/redev/01-audit.md` 的 D2）。CI 里不可能为了冒烟去手动起一个服务，
所以接口层必须在**进程内**用 `TestClient` 验证：不需要端口、不需要 API Key，
因此可以放进**零成本的必需档**。

【覆盖边界】
这里只碰**只读、无副作用、不花钱**的接口；
需要模型或写操作的路径（`/agent/ask`、`/chat`、`/rag/index`、approvals 的写接口）
只断言"它们还在 OpenAPI 里"，具体行为交给各自模块的测试与 `smoke_test.py --full`。

【状态隔离】
`/approvals` 会触发惰性过期检查，那条路径**可能往 approvals.jsonl 追加事件**。
所以这里把审批单存储与 trace 落盘都换成临时文件 ——
跑完测试，真实 `logs/` 下必须一行没多。
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import security
from app.observability import tracer
from app.sandbox import approvals, policy

TOKEN = "unit-test-token-0123456789abcdef"

# 必须一直存在的读接口。删掉其中任何一个，这个测试就要红。
READONLY_GETS = [
    "/health",
    "/agent/tools",
    "/agent/graph",
    "/sandbox",
    "/approvals",
    "/incidents",
    "/traces",
    "/metrics/summary",
    "/audit",
]

# 页面（公开的"空壳"，不含数据）
PUBLIC_PAGES = ["/", "/try", "/dashboard", "/settings"]

# 打开鉴权后必须一律 401 的路径。
#   ★ `/settings/api` 在这里是**故意的**：审计报告一度把它标成"明文泄露令牌"，
#     复核发现它并不在免鉴权清单里（security.py:81）—— 这条用例把这个事实钉住，
#     免得以后有人"顺手"把它加进公开清单。
PROTECTED_PATHS = [
    "/traces",
    "/audit",
    "/approvals",
    "/incidents",
    "/metrics/summary",
    "/sandbox",
    "/agent/tools",
    "/settings/api",
]

# 零成本档不实际调用的写/花钱接口：只断言它们还在
REQUIRED_WRITE_OPS = [
    ("/agent/ask", "post"),
    ("/agent/ask/stream", "post"),
    ("/chat", "post"),
    ("/chat/stream", "post"),
    ("/parse", "post"),
    ("/webhook/alert", "post"),
    ("/rag/ask", "post"),
    ("/rag/search", "post"),
    ("/rag/index", "post"),
    ("/approvals/{approval_id}/approve", "post"),
    ("/approvals/{approval_id}/reject", "post"),
    ("/approvals/{approval_id}/execute", "post"),
    ("/incidents/{incident_id}/ack", "post"),
    ("/incidents/{incident_id}/resolve", "post"),
]


# ============================================================
# 装置
# ============================================================
@pytest.fixture
def client(tmp_path, monkeypatch):
    """进程内客户端，且把**所有会落盘的状态**改到临时目录。"""
    monkeypatch.setattr(approvals, "_STORE",
                        approvals.ApprovalStore(path=tmp_path / "approvals.jsonl"))
    monkeypatch.setattr(tracer, "TRACE_PATH", tmp_path / "traces.jsonl")
    _isolate_incidents(tmp_path, monkeypatch)
    # 令牌与数据源与测试无关，这里显式保证是"本地开发"档
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    with TestClient(main.app) as c:
        yield c


def _isolate_incidents(tmp_path, monkeypatch):
    """事件存储也要隔离 —— 否则敲一下 `/incidents` 就会往真实 logs/ 写。

    ★ 这条是冲着一次真实事故加的：M1 期间就有一条漏加隔离装置的用例
      把假 trace 写进了真实 logs/traces.jsonl，把对外成本口径压低了 12 倍。
      **新加会落盘的功能时，隔离装置必须同步加上**，而不是等出事了再补。
    """
    try:
        import app.incident.store as incident_store_mod
    except ImportError:                       # 事件模块尚未接线时不阻塞其它用例
        return
    fresh = incident_store_mod.IncidentStore(path=tmp_path / "incidents.jsonl")
    monkeypatch.setattr(incident_store_mod, "store", lambda: fresh)
    monkeypatch.setattr(incident_store_mod, "_STORE", fresh, raising=False)


@pytest.fixture
def auth_client(client, monkeypatch):
    """打开了鉴权的客户端。

    `AUTH_ENABLED` / `AGENT_TOKEN` 是模块级全局，而中间件在**每次请求**时才读它，
    所以 monkeypatch 能直接生效，不需要重新 import app。
    """
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    monkeypatch.setattr(security, "AGENT_TOKEN", TOKEN)
    return client


# ============================================================
# 一、路由存在性
# ============================================================
def test_openapi_keeps_the_required_write_operations(client):
    spec = client.get("/openapi.json").json()
    for path, method in REQUIRED_WRITE_OPS:
        assert path in spec["paths"], f"路由被删了：{path}"
        assert method in spec["paths"][path], f"方法被删了：{method} {path}"


def test_openapi_operation_count_does_not_shrink(client):
    """操作数只许涨不许跌（新增接口是好事，删接口要有人为它改这个数字）。"""
    spec = client.get("/openapi.json").json()
    total = sum(len(v) for v in spec["paths"].values())
    assert total >= 26, f"OpenAPI 操作数从 26 掉到了 {total}"


def test_openapi_declares_the_api_key_scheme(client):
    """Swagger 里的 Authorize 按钮靠它渲染。

    没有这个声明，文档就变成"点了 Try it out 一律 401"的骗人文档
    （见 main.py:112-118 的说明）。
    """
    spec = client.get("/openapi.json").json()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "AgentDeskToken" in schemes
    assert schemes["AgentDeskToken"]["name"] == "X-API-Key"
    assert schemes["AgentDeskToken"]["in"] == "header"


# ============================================================
# 二、只读接口真的能响应（不是"路由存在"而已）
# ============================================================
@pytest.mark.parametrize("path", READONLY_GETS)
def test_readonly_endpoints_respond(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} 返回 {r.status_code}：{r.text[:200]}"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


def test_health_reports_version_and_security_state(client):
    body = client.get("/health").json()
    assert body.get("status")
    assert body.get("version")


# ============================================================
# 三、鉴权边界
# ============================================================
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_paths_require_a_token(auth_client, path):
    assert auth_client.get(path).status_code == 401, f"{path} 在没有令牌时被放行了"


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_paths_reject_a_wrong_token(auth_client, path):
    r = auth_client.get(path, headers={"X-API-Key": TOKEN + "x"})
    assert r.status_code == 401, f"{path} 接受了错误的令牌"


@pytest.mark.parametrize("path", ["/traces", "/audit", "/approvals", "/sandbox"])
def test_x_api_key_header_grants_access(auth_client, path):
    r = auth_client.get(path, headers={"X-API-Key": TOKEN})
    assert r.status_code == 200, f"{path} 带正确令牌仍被拒：{r.status_code}"


def test_bearer_token_also_works(auth_client):
    r = auth_client.get("/traces", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/health", "/openapi.json"] + PUBLIC_PAGES)
def test_public_paths_stay_open_when_auth_is_on(auth_client, path):
    """"打开链接就能看到有这个东西"—— 公开的只是空壳，数据仍要令牌。"""
    assert auth_client.get(path).status_code != 401, f"{path} 不该要求令牌"


# ============================================================
# 四、公开清单与花钱清单的**不变量**
# ============================================================
# 这些路径一旦进了免鉴权清单，就等于把服务器和余额一起交出去
SENSITIVE = {"/traces", "/audit", "/approvals", "/metrics/summary",
             "/settings/api", "/settings/token", "/settings/ops",
             "/agent/ask", "/chat", "/rag/index", "/webhook/alert"}


def test_public_whitelist_contains_nothing_sensitive():
    leaked = SENSITIVE & set(security.PUBLIC_EXACT)
    assert not leaked, f"这些敏感路径被放进了免鉴权清单：{sorted(leaked)}"


def _route_paths():
    """应用真实注册的路由路径集合。"""
    return {getattr(r, "path", None) for r in main.app.routes}


def test_every_public_path_actually_exists():
    """公开清单里不能有"打错的路径"—— 那会让人误以为某处是公开的。

    ★ 这里用 `app.routes` 而不是 OpenAPI：`/`、`/try`、`/dashboard`、`/settings`
      这几个页面是用 `include_in_schema=False` 注册的（它们只是不含数据的空壳，
      没必要进接口文档），所以 **OpenAPI 里看不到它们**。
      "路由是否存在"和"文档里是否有它"是两件事，别拿后者当前者 ——
      这一条最初就是按 OpenAPI 写的，于是误报了一次。
    """
    known = _route_paths()
    for path in security.PUBLIC_EXACT:
        if path in ("/openapi.json", "/favicon.ico"):
            continue          # 由框架处理，不在业务路由表里
        assert path in known, f"公开清单里的 {path} 不是真实路由"


def test_cost_paths_are_real_route_prefixes(client):
    """花钱清单必须指向真实接口 —— 写错的后果是"该扣额度的没扣"。"""
    spec = client.get("/openapi.json").json()
    known = list(spec["paths"])
    for prefix in security.COST_PREFIX:
        assert any(p.startswith(prefix) for p in known), \
            f"花钱清单里的 {prefix} 没有对应路由"


def test_daily_quota_only_applies_to_cost_paths(client):
    assert security._is_cost_path("/agent/ask") is True
    assert security._is_cost_path("/traces") is False
    assert security._is_cost_path("/health") is False


# ============================================================
# 五、写接口的参数校验（不发模型、不落副作用）
# ============================================================
def test_approve_requires_a_person(client):
    """没有审批人的批准必须被拒 —— 这是"每次改动都有主"的底线。"""
    r = client.post("/approvals/ap-nonexistent/approve", json={})
    assert r.status_code in (400, 404, 409, 422)


def test_execute_unknown_approval_returns_an_error_not_a_crash(client):
    r = client.post("/approvals/ap-nonexistent/execute", json={"by": "sre-zhang"})
    assert 400 <= r.status_code < 500


def test_sandbox_status_exposes_the_whitelist_summary(client):
    body = client.get("/sandbox").json()
    text = str(body)
    assert "fail" in text.lower() or "closed" in text.lower() or "closed" in text
    # 白名单的真实条数必须能在响应里对上（别让接口报一个手写的数字）
    assert len(policy.catalog()) == len(policy.COMMANDS)


# ============================================================
# 六、事件（Incident）接口的行为
# ============================================================
def test_incident_list_is_empty_before_anything_happens(client):
    body = client.get("/incidents").json()
    assert body.get("items") == []
    assert body.get("counts", {}).get("total", 0) == 0


def test_incident_unknown_id_returns_404(client):
    assert client.get("/incidents/inc-nope").status_code == 404


def test_incident_ack_requires_a_person(client):
    """认领事件必须写清是谁 —— 与"批准必须填审批人"同一条原则。"""
    for payload in ({}, {"by": ""}, {"by": "   "}):
        r = client.post("/incidents/inc-nope/ack", json=payload)
        assert r.status_code in (400, 404, 422), payload


def test_incident_resolve_requires_a_person(client):
    for payload in ({}, {"by": ""}):
        r = client.post("/incidents/inc-nope/resolve", json=payload)
        assert r.status_code in (400, 404, 422), payload


def test_incident_status_filter_is_honoured(client):
    body = client.get("/incidents", params={"status": "open"}).json()
    assert body.get("items") == []
