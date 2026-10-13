# -*- coding: utf-8 -*-
"""系统设置（P4）—— 「看配置、不泄密」的一页。

【这一页回答什么问题】
打开它的人想知道的是**「现在到底什么生效」**，而不是读一份配置手册。所以：

    ① 顶部一组现状摘要（KPI）：鉴权 / 额度 / 通知 / 向量后端 / 剧本 / 沙箱 / 模型
    ② 主体按能力分组（模型 · 检索 · 告警与通知 · 沙箱与审批 · 安全与限额 · 日志与可观测），
       每组里"现在是什么值 + 徽标 + ⓘ 口径"
    ③ 说明性长文案下沉 `<details class="acc">`，正文里不写文档
    ④ 只有**两处**真的能改：访问令牌、工具层数据源（与旧页能力一致）；
       别的配置如实显示"怎么改"（改 .env 重启），不做假的输入框

【★ 密钥永不回显 —— 这是安全要求，不是审美】
旧页做了一件当时看着方便、实际是缺陷的事：它 `fetch('/settings/api')` 之后
把**完整令牌**写进 `<input id="cur">`（还带一个「显示」按钮），
并把 SSH 私钥路径写进输入框。那意味着：

    · 令牌进了 DOM（浏览器扩展、录屏、截图、"打印/另存为"都会带上它）
    · 令牌进了浏览器历史之外的地方（页面 HTML 一旦被缓存/代抓，#cur 的 value 就在里面）

本文件的纪律（写死在这里，改动必须显式违反它）：

    1. **密钥类字段只渲染状态，不渲染值**，连掩码后的前缀都不要
       （`已设置 / 未设置` + 长度，长度不构成泄密，前缀会）
    2. **服务端渲染时绝不把密钥插进 HTML**：`_model_info()` 只返回布尔与标签，
       绝不返回 key 本身 —— 所以"页面 HTML 里没有密钥"是**结构上成立**的，
       而不是靠"记得转义"或"记得别拼错变量"
    3. **前端白名单取值**：`/settings/api` 确实会返回完整令牌（读接口的语义没变，
       它有鉴权 + 有对账测试兜底），但本页的 JS **只读** `auth_enabled` / `token_len`
       与 `ops` 的 `backend/targets/targets_raw/hosts_env/known_hosts/credentials` ——
       不读 `auth.token`、不读 `auth.token_masked`。
       没有"记得删掉那一行"的步骤，只有"不许取"这一条
    4. 令牌的**新值**只在用户主动输入时存在；保存成功后只存进本机 localStorage
       （与旧页一致），服务端的响应里只回掩码给提示语用 —— 页面不渲染它
    5. **主机的密码只写不读**（「SSH 登录」卡）：`ops.credentials[].key` 是私钥
       **路径**（文件系统上的位置，不是密钥内容），所以它可以回填、可以编辑、
       而且必须**原样回传** —— POST /settings/ops 的语义是"没给的字段 = 空值"，
       不回传就会把用户配好的目标/全局私钥冲掉。
       而**密码在 `/settings/api` 的响应里根本不存在**，只有 `password_set: bool`。
       于是密码只可能来自"用户刚敲进去的那一次"：它只进 `input.value` 与请求体，
       提交后立刻 `input.value = ""`，绝不过 innerHTML / console / localStorage / URL。
       失败回执（400/422）也只取 `loc` + `msg`，**绝不复述 `input`** ——
       FastAPI 的 422 会把出错的那个值原样放在 `detail[].input` 里，那正是密码

【数据从哪来（全部是已有接口，零后端改动）】
    /settings/api   访问令牌状态 + 工具层数据源（要令牌；本地免鉴权时公开）
    /health         安全层状态：鉴权开关 / 限流 / 每日额度与已用（公开）
    /incidents      通知装配结果（渠道 / 脱敏开关）+ 告警聚合
    /rag/stats      索引文档数、块数、向量后端
    /sandbox        沙箱后端、隔离实情、命令白名单

取不到的每一项都显示 `—` + 一句原因（尤其"开了鉴权、本机浏览器没有令牌"），
**不让整页报错** —— 设置页在最需要它的时候（配置出问题、服务半残）也必须能打开。
"""

import os
from pathlib import Path

from fastapi.responses import HTMLResponse

from app.web.components import (
    badge,
    button,
    card,
    esc,
    info_tip,
    link_button,
    note,
    skeleton,
)
from app.web.layout import NODE_LABEL, shell  # noqa: F401  (NODE_LABEL 供后续复用/一致性)

# ============================================================
# 一、现状摘要的取值口径（唯一来源：页脚折叠区与每个 ⓘ 都从这里取）
# ============================================================
DEFS = {
    "summary": ("现状摘要的每一项都来自已有接口的当前值，不是配置文件里的声明。"
                "「已开启 / 未配置」看的是运行时装配结果 —— 声明与生效不一致时，"
                "这里显示的是生效的那个。"),
    "auth": ("鉴权 = `AUTH_ENABLED`。开启后除首页、本页、`/health` 等少数路径外，"
             "所有接口都要求 `X-API-Key`。页面是空壳，数据仍由接口那一步保护。"),
    "token": ("令牌状态只显示「已设置 / 未设置」，永不回显令牌本身 —— "
              "连掩码后的前缀都不显示。前缀对判断强度没有帮助，但会泄密。"),
    "creds": ("每台主机一份 SSH 凭据：认证方式（私钥 / 密码）+ 该主机自己的私钥路径。"
              "统计里的「N 台用密码」指这些主机的认证方式是密码 —— "
              "密码本身永不出现在这里，页面只知道「有没有设置」。"),
    "quota": ("每日额度 = `DAILY_QUOTA`，进程内计数：单进程（`--workers 1`）下准确，"
              "多 worker 会各算各的。它统计的是花钱的调用，不是所有请求。"),
    "notify": ("通知渠道由 `NOTIFY_CHANNELS` 点名决定 —— 只有被点名的渠道才会被构造，"
               "避免某个 URL 被误填后把运维内部信息发出去。"
               "「未配置」是默认状态，不是错误。"),
    "retrieval": ("向量后端 auto 选择：配了 `DASHSCOPE_API_KEY` 就是真语义后端，"
                  "否则退回 `local-hash` —— 它只有词面匹配能力，没有任何语义理解。"
                  "别把兜底当成真的语义检索（这是本项目最容易高估自己的一项）。"),
    "plays": ("剧本 = `plays/*.json`：一份写死的处置流程（触发条件 → 步骤 → 需要的审批）。"
              "数量是启动期校验过的，坏文件会让服务起不来而不是静默跳过。"),
    "sandbox": ("沙箱后端 `docker` = 一次性容器里执行（真隔离）；`mock` = 仿真，"
                "不碰任何真实机器；`subprocess` = ⚠️ 无隔离，直接在目标主机执行。"),
    "model": ("模型供应商按 .env 里配了哪个 Key 自动选择（DeepSeek 优先）。"
              "这里只显示「是否配置」与模型名，密钥值不出现。"),
    "logs": ("日志格式 / 级别在启动时读一次；trace 与审计按大小轮转"
             "（`LOG_MAX_BYTES` / `LOG_KEEP`），审批单与事件是折叠式状态日志，不轮转。"),
}


def _tip(text: str) -> str:
    """ⓘ 悬停提示（`components.info_tip` 只接受一个参数，这里省得每处都写 esc）。"""
    return info_tip(text)


# ============================================================
# 二、服务端取到的值（只返回**非密钥**的标签与布尔）
# ============================================================
# 单价表的"最近核对日期"。**刻意只报日期、不报价格**：
# 价格会变（缓存命中价实测改过 25 倍），在页面上抄一份价格表，
# 迟早出现"页面说的和实际算的不一样"——那正是这个项目最不能出的错。
PRICE_TABLE_CHECKED_AT = "2026-09-28"


def _env_flag(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().lower()


def _env_text(name: str, default: str = "—") -> str:
    v = (os.getenv(name) or "").strip()
    return v or default


def _model_info() -> dict:
    """当前模型与密钥**是否配置**。

    ★ 返回值里**没有任何密钥字符串** —— 只有 `configured` 布尔。
      页面里"没有密钥"因此是结构上的事实，而不是"记得别拼错变量"。
      这也让这条纪律可以被测试直接断言：整个 HTML 里搜不到密钥形态的串。
    """
    from app.llm import PROVIDERS

    for name, cfg in PROVIDERS.items():
        present = bool((os.getenv(cfg["env_key"]) or "").strip())
        if present:
            return {"provider": name, "configured": True,
                    "model": cfg["model"], "env_key": cfg["env_key"]}
    return {"provider": (os.getenv("DEFAULT_MODEL") or "deepseek").strip() or "deepseek",
            "configured": False, "model": os.getenv("DEFAULT_MODEL") or "—",
            "env_key": "DEEPSEEK_API_KEY"}


def _play_count() -> int:
    """`plays/*.json` 的份数（启动期校验过的剧本）。取不到就是 0，不抛。"""
    try:
        from app.llm import PROJECT_ROOT
        return len(list((Path(PROJECT_ROOT) / "plays").glob("*.json")))
    except Exception:                             # noqa: BLE001 —— 设置页不许因此报错
        return 0


def _log_files() -> list:
    """日志文件与大小（只看名字与字节数，不读内容）。"""
    out = []
    try:
        from app.llm import PROJECT_ROOT
        for name in ("traces.jsonl", "audit.jsonl", "approvals.jsonl",
                     "incidents.jsonl"):
            p = Path(PROJECT_ROOT) / "logs" / name
            if p.exists():
                out.append({"name": name, "bytes": p.stat().st_size})
    except Exception:                             # noqa: BLE001
        return []
    return out


def _kpi_shell(key: str, label: str, tip: str, value: str = "—") -> str:
    """摘要卡：服务端先渲染出值，浏览器拿到接口数据后再覆盖。

    ★ 标题与 ⓘ 挂在**卡片的 `.k` 里**（`kpi()` 的结构），值单独一个 `<span>`；
      前端只改那个 span 的文本，不重建卡片 —— 结构由服务端定死，
      前端没有机会把 HTML 拼错（也不需要 innerHTML）。
    ★ `value` 默认 `—`：**接口取不到时就是一根破折号**，而不是空白或报错。
      少数几项（剧本份数、模型名）在服务端就能定，直接渲染出来 ——
      没有理由让它们也走一次网络请求。
    """
    return (f'<div class="kpi"><div class="k">{esc(label)}{_tip(tip)}</div>'
            f'<div class="v" id="k-{esc(key)}">{esc(value)}</div></div>')


SUMMARY_TILES = (
    ("auth", "鉴权", DEFS["auth"]),
    ("quota", "每日额度", DEFS["quota"]),
    ("notify", "告警通知", DEFS["notify"]),
    ("retrieval", "检索后端", DEFS["retrieval"]),
    ("plays", "处置剧本", DEFS["plays"]),
    ("sandbox", "沙箱隔离", DEFS["sandbox"]),
    ("model", "模型", DEFS["model"]),
    # ↓ 可选的第八格：一眼看出"凭据配了几台、有几台靠密码"（浏览器拿到接口后填）
    ("creds", "SSH 凭据", DEFS["creds"]),
)


# ============================================================
# 三、分组正文（服务端渲染的部分）
# ============================================================
def _row(label: str, value_html: str, tip: str = "") -> str:
    return (f'<div class="field"><label class="f">{esc(label)}'
            f'{_tip(tip) if tip else ""}</label><div>{value_html}</div></div>')


def _state(ok: bool, yes: str = "已设置", no: str = "未设置") -> str:
    """★ 密钥类字段的统一形态：**只有状态徽标，没有值**。"""
    return badge(yes if ok else no, "ok" if ok else "neutral")


def _model_section(model: dict) -> str:
    key_badge = _state(model["configured"], "已设置", "未设置")
    body = (
        _row("当前供应商", badge(model["provider"], "brand"), "按 .env 里配了哪个 Key 自动选")
        + _row("当前模型", f'<code>{esc(model["model"])}</code>',
               "对外的主模型名；实测服务端返回的就是它")
        + _row(f'API 密钥（{model["env_key"]}）', key_badge, DEFS["token"])
        + _row("在 .env 中的默认模型名",
               f'<code>{esc(_env_text("DEFAULT_MODEL", "（未设置）"))}</code>',
               "只是默认值；当前用的是「当前模型」那一行")
    )
    head = (note("<b>密钥永不回显</b>：这里只有「已设置 / 未设置」，"
                 "页面 HTML 里不存在密钥值（有测试守着）。")
            + ("" if model["configured"] else
               note("没有配任何模型 Key —— 提问与 Agent 诊断会直接报错。"
                    "在 .env 里填 <code>DEEPSEEK_API_KEY</code> 后重启。", "warn")))
    return card(head + body, title="模型与大模型调用")


def _retrieval_section() -> str:
    body = (
        '<div id="ragRows">'
        + _row("向量后端 / 模型", skeleton("180px", "18px"), DEFS["retrieval"])
        + _row("索引规模", skeleton("120px", "18px"))
        + "</div>"
        + '<div id="ragNote" class="hint">载入中…</div>'
    )
    return card(body, title="检索（RAG）",
                desc="数据源 /rag/stats",
                actions=link_button("检索统计接口", "/docs", size="sm"))


def _notify_section() -> str:
    body = (
        '<div id="notifyRows">'
        + _row("出站渠道", skeleton("160px", "18px"), DEFS["notify"])
        + _row("出站脱敏", skeleton("100px", "18px"),
               "NOTIFY_MASK 默认 1：出站前把疑似 token / key 打码")
        + "</div>"
        + '<div id="notifyNote" class="hint">载入中…</div>'
        + note("通知是<b>出站</b>的：一旦某个 URL 被误填，运维内部信息就发出去了。"
               "所以只有 `NOTIFY_CHANNELS` 里点名的渠道才会被构造。")
        + _row("改这些要动哪儿", "<code>.env</code> 的「出站通知」一节（重启生效）")
    )
    return card(body, title="出站通知渠道", desc="数据源 /incidents 的 notify 字段")


def _alert_card() -> str:
    """告警聚合与自动诊断：**抑制发生在调模型之前**，所以这一栏是成本控制的一部分。"""
    agg_on = _env_flag("ALERT_AGGREGATE", "1") == "1"
    body = (
        _row("聚合开关", badge("按主机 + 服务聚合" if agg_on else "精确去重（旧语义）",
                              "ok" if agg_on else "neutral"),
             "同机 + 同服务的多条告警聚成一个事件：1 条告警 = 1 次诊断，"
             "而不是 50 条告警 = 50 次诊断")
        + '<div id="aggRows">'
        + _row("运转状态", skeleton("180px", "18px"))
        + _row("去重表上限", skeleton("120px", "18px"),
               "防止长期运行内存单调增长；超限先清过期、再淘汰最旧")
        + "</div>"
        + _row("自动诊断开关",
               badge("已开启" if _env_flag("ALERT_AUTO_DIAGNOSE") == "1" else "关闭",
                     "warn" if _env_flag("ALERT_AUTO_DIAGNOSE") == "1" else "ok"),
               "ALERT_AUTO_DIAGNOSE=1 时每条告警都会跑一轮诊断（实测 6–20 秒、几分钱）")
        + _row("抑制窗口 / 每小时上限",
               f'<code>{esc(_env_text("ALERT_DEDUP_SECONDS", "600"))}</code> 秒 · '
               f'每小时 <code>{esc(_env_text("ALERT_MAX_PER_HOUR", "10"))}</code> 次')
        + '<div id="aggNote" class="hint">载入中…</div>'
    )
    return card(body, title="告警聚合与自动诊断",
                desc="数据源 /incidents 的 aggregate 字段 · 环境变量")


def _sandbox_section() -> str:
    body = (
        '<div id="sandboxRows">'
        + _row("沙箱后端", skeleton("140px", "18px"), DEFS["sandbox"])
        + _row("命令白名单", skeleton("140px", "18px"),
               "只有清单里的命令能被执行；清单外的直接拒绝（fail-closed）")
        + _row("需要人工审批", skeleton("140px", "18px"),
               "写操作必须由人确认；审批单带指纹，参数变了要重新批")
        + "</div>"
        + '<div id="sandboxNote" class="hint">载入中…</div>'
    )
    return card(body, title="沙箱与审批", desc="数据源 /sandbox")


def _devprefix_card() -> str:
    """允许的**命令前缀**（静态信息，服务端渲染）：fail-closed 的具体形态。"""
    prefixes = ['uptime', 'df', 'free', 'du', 'systemctl status',
                'journalctl', 'docker ps', 'docker logs', 'tail', 'lsof']
    chips = "".join(f'<span class="chip">{esc(p)}</span>' for p in prefixes)
    body = (
        '<div class="chips">' + chips + "</div>"
        + note("清单外的命令一律拒绝，而且这个判断发生在执行之前 —— "
               "<b>fail-closed</b>：拿不准就不做，而不是「试一下」。")
        + _row("写操作怎么走",
               "转人工审批（审批单绑定命令指纹，参数变了要重新批）",
               "`systemctl restart` / `docker restart` / `truncate` 这类")
    )
    return card(body, title="允许的命令前缀", desc="与执行器里的策略同一来源")


def _security_section() -> str:
    body = (
        '<div id="securityRows">'
        + _row("鉴权", skeleton("140px", "18px"), DEFS["auth"])
        + _row("访问令牌", skeleton("140px", "18px"), DEFS["token"])
        + _row("限流（每 IP / 全站 · 每分钟）", skeleton("140px", "18px"))
        + _row("每日额度", skeleton("140px", "18px"), DEFS["quota"])
        + _row("今日已用（进程内）", skeleton("140px", "18px"))
        + "</div>"
    )
    form = (
        '<div class="section">'
        + _row("新令牌（≥ 16 字符，留空 = 不修改）",
               '<input type="text" id="newtok" autocomplete="new-password" '
               'placeholder="粘贴，或点右边的按钮生成" '
               'aria-label="新令牌" class="mono">')
        + '<div class="btnrow">'
        + button("生成随机令牌", tone="ghost", size="sm", attrs='id="gen"')
        + button("确认修改", size="sm", attrs='id="savetok"')
        + link_button("接口文档", "/docs", size="sm")
        + "</div>"
        + '<div id="tokmsg"></div>'
        + '<p class="hint">取当前令牌需要出示<b>当前有效</b>的令牌（改了钥匙要先有旧钥匙）；'
          "改完旧令牌立即作废，新令牌自动存进本机浏览器，其它调用方要跟着换。</p>"
        + "</div>"
    )
    return card(body + form, title="安全与限额", desc="数据源 /health · /settings/api")


def _logging_section() -> str:
    body = (
        _row("日志格式", badge(_env_text("LOG_FORMAT", "text"), "neutral"), DEFS["logs"])
        + _row("日志级别", badge(_env_text("LOG_LEVEL", "INFO"), "neutral"))
        + _row("轮转阈值 / 保留份数",
               f'<code>{esc(_env_text("LOG_MAX_BYTES", "10485760"))}</code> 字节 · '
               f'保留 <code>{esc(_env_text("LOG_KEEP", "5"))}</code> 份')
        + _row("自检是否真调模型",
               badge("会花钱" if _env_flag("HEALTHZ_PROBE_MODEL") == "1" else "否",
                     "warn" if _env_flag("HEALTHZ_PROBE_MODEL") == "1" else "ok"),
               "HEALTHZ_PROBE_MODEL=1 时 /healthz 会真的调一次模型")
        + _row("告警自动诊断",
               badge("已开启" if _env_flag("ALERT_AUTO_DIAGNOSE") == "1" else "关闭",
                     "warn" if _env_flag("ALERT_AUTO_DIAGNOSE") == "1" else "ok"),
               "开启后每条告警都会跑一轮 Agent 诊断（6–20 秒、几分钱）")
        + _row("抑制窗口 / 每小时上限",
               f'<code>{esc(_env_text("ALERT_DEDUP_SECONDS", "600"))}</code> 秒 · '
               f'每小时 <code>{esc(_env_text("ALERT_MAX_PER_HOUR", "10"))}</code> 次',
               "两道闸都挡在调模型之前：被抑制的告警不会偷偷烧钱")
    )
    return card(body, title="日志与自检",
                desc="数据源 环境变量（启动时读一次）")


def _log_files_card() -> str:
    """日志文件大小（不读内容，只看大小 —— 设置页不该顺手把日志读一遍）。"""
    files = _log_files()
    rows = "".join(
        f'<div class="field"><label class="f">{esc(f["name"])}</label>'
        f'<div><code>{f["bytes"] / 1024:.0f} KB</code></div></div>'
        for f in files
    ) or '<p class="hint">logs/ 下还没有日志文件（服务还没写过盘）。</p>'
    return card(rows, title="日志文件", desc="logs/ 下的大小")


def _cost_card() -> str:
    """成本与单价：只报**单价表的权威来源**，不复述价格（价格会过期）。"""
    body = (
        _row("单价表生效日期", f'<code>{esc(PRICE_TABLE_CHECKED_AT)}</code>',
             "价格会被上游改（缓存命中价实测改过 25 倍），所以金额一律**按当前单价表重算**，"
             "不用落盘时记的那个数")
        + _row("单价的唯一来源", "<code>app/observability/costs.py</code>",
               "对账、看板、本页都从这里取 —— 不在这里再抄一份价格表")
        + _row("用量与成本去哪儿看",
               '<a href="/dashboard">看板</a> · <a href="/metrics">指标</a> · '
               '<a href="/view/metrics">成本看板</a>')
    )
    return card(body, title="成本与单价")


def _limits_card() -> str:
    """闸门参数：抑制发生在调模型之前，所以它们也是**成本控制**的一部分。"""
    body = (
        _row("检索候选数 / RRF 常数",
               f'<code>{esc(_env_text("RETRIEVAL_CANDIDATE_K", "30"))}</code> · '
               f'<code>{esc(_env_text("RETRIEVAL_RRF_K", "60"))}</code>',
               "候选数与融合常数；改它们要先跑评测基线，别凭感觉调")
        + _row("BM25 参数 k1 / b",
               f'<code>{esc(_env_text("RETRIEVAL_BM25_K1", "1.5"))}</code> · '
               f'<code>{esc(_env_text("RETRIEVAL_BM25_B", "0.75"))}</code>')
        + _row("Agent 墙钟预算",
               f'<code>{esc(_env_text("AGENT_BUDGET_SECONDS", "0（不限）"))}</code>',
               "0 = 不限（与加这个开关之前的行为一致）")
        + note("这两处是**花钱与否**的闸门：检索参数影响召回，预算与抑制参数决定"
               "一次诊断能跑多久、一条告警会不会真的去调模型。")
    )
    return card(body, title="检索与预算参数")


def _settings_form() -> str:
    """工具层数据源（唯一一处"真的能改"的表单，与旧页能力一致）。

    ★ 这里的私钥路径是**全局兜底**（每台主机自己的那把在「SSH 登录」卡里编辑），
      输入框**永远留空**：旧页把当前路径回填进来（`$('key').value = ops.key`），
      本页不回填、也不把当前值渲染出来 —— 需要保留就留空（服务端语义：留空 = 不修改）。
      想知道"现在配的有没有"，看左边的徽标。
      ★ 而"回车保存时原样回传"是另一回事：凭据卡的请求体必须带上当前的全局
      `key`（否则一次保存凭据就会把它冲成空值）——它从接口快照里取，不从这个输入框取。
    """
    body = (
        '<div class="btnrow">'
        + "".join(
            f'<label class="switch"><input type="radio" name="backend" value="{v}">'
            f" {esc(t)}</label>"
            for v, t in (("mock", "mock · 内置仿真（最安全）"),
                         ("local", "local · 查本机真实状态"),
                         ("ssh", "ssh · 查远程主机")))
        + "</div>"
        + _row("SSH 目标（逻辑名=user@地址[:端口]，逗号分隔；ssh 后端必填）",
               '<textarea id="targets" placeholder="web-01=root@192.168.1.10,'
               'db-01=ops@192.168.1.11:2222" aria-label="SSH 目标"></textarea>')
        + _row("SSH 私钥路径（留空 = 不修改）",
               '<input type="text" id="key" class="mono" '
               'placeholder="留空即保持当前值（可能为空）" aria-label="SSH 私钥路径">')
        + _row("主机清单（可选，逗号分隔；留空 = 按目标自动推导）",
               '<input type="text" id="hosts" placeholder="web-01,db-01" '
               'aria-label="主机清单">')
        + '<div class="btnrow">'
        + button("应用数据源", size="sm", attrs='id="applyops"')
        + "</div>"
        + '<div id="opsmsg"></div>'
        + note("「Agent 知道队里有哪几台」（主机清单）与「能连到哪几台」（SSH 目标）"
               "不是一回事：前者影响它怎么描述环境，后者决定它能不能真查。")
        + '<div id="opsview" class="section">' + skeleton("100%", "60px") + "</div>"
        + '<p class="hint" id="persist">持久化位置载入中…</p>'
    )
    return card(body, title="工具层数据源", desc="改完运行时立即生效，并写回 .env（自动备份）")


def _credentials_card() -> str:
    """「SSH 登录（每台主机一份凭据）」—— 本页唯一会碰到密码的地方。

    【为什么服务端只渲染一具空壳】
    凭据清单的唯一来源是 `/settings/api` 的 `ops.credentials`，所以这里
    **一个主机名、一条路径都不写**：行的内容全部由前端按接口返回渲染。
    服务端渲染时因此结构上碰不到凭据表（也就碰不到密码）——
    "页面 HTML 里没有密码"是**结构上成立**的，而不是"记得别拼错变量"。

    【密码的三条界面规则（与服务端同一套语义，缺一不可）】
    · **只写不读**：密码输入框永远不回显，只有 placeholder + 徽标「已设置 / 未设置」
    · **留空 = 不修改**：只改私钥路径时密码不会被抹掉
      （"保存一次就丢密码"是最容易让人放弃这个功能的那种 bug）
    · **要清空必须显式说**：勾「清除密码」→ `clear_password: true`

    【没有目标的行】
    `target` 为 null = 这台主机在清单里但没有 SSH 目标（连不上）。
    那一行的控件整行停用、并打「尚未配置目标」徽标：它不参与保存
    （提交时该行留空 = 服务端语义的"不改这项"，不会把它已有的密码弄丢）。
    它同时**也不能测**（没有地址就没得连）—— 所以那一行的「测试登录」按钮是灰的，
    并把原因写在 title 里，而不是"点了没反应"。

    【两件让"提交成功 ≠ 登得上"这件事可见的东西】
    · 每行一个「测试登录」：用**这一行当前输入的值**试一次真登录（`POST /settings/ssh/test`），
      结果就地写在该行（`<span id="credTest-<i>">`），不用 alert 弹窗。
      留空的字段 = 用这台主机已保存的那一项，所以"存了但不确定"可以直接测。
    · 保存前先出「将要改动」确认条（`#credConfirm`），确认后才发 `POST /settings/ops`；
      保存成功后**串行**把所有有目标的主机各测一次，逐行填结果 + 回执区一句汇总。

    【这两处新增的代码为什么**碰不到密码**】
    · 确认清单说的是「密码 → 已设置」这种**状态**，从不写用户敲的值（见 js 的 credChanges）
    · 测试结果只来自「`reason` / 远端主机名 / 延迟」三个字段 —— 回执里本来就没有密码
    · 测试成功后立刻清空该行密码框（它已经用掉了），失败保留让用户改完重试；
      测试与保存两处收尾都调 clearPwdInputs()
    """
    body = (
        '<div id="credSummary" class="hint">载入中…</div>'
        + '<div id="credRows">' + skeleton("100%", "72px") + "</div>"
        + '<div class="btnrow">'
        + button("保存全部凭据", size="sm", attrs='id="saveCreds"')
        + "</div>"
        + '<div id="credConfirm" class="card tight" hidden></div>'
        + '<div id="credMsg"></div>'
        + note("三件事写在这里，因为弄错任何一件都会让人再也不敢用这个功能："
               "<b>密码只写不读</b>（页面只能显示「已设置 / 未设置」，回显不了值）；"
               "<b>留空 = 不修改</b>（只改私钥路径不会把已保存的密码弄丢）；"
               "<b>清空要显式勾</b>「清除密码」。编辑框里的私钥是<b>路径</b>"
               "（文件在哪儿），不是密钥内容。")
        + note("这一栏的按钮把<b>所有行</b>一起提交，但没动过的行保持原值；"
               "带「尚未配置目标」徽标的主机连不上，也不参与保存 —— "
               "要让它能连，先去「工具层数据源」里给它配 "
               "<code>名=user@地址[:端口]</code>。", "warn")
        + note("每行的<b>「测试登录」</b>用你<b>当前填的值</b>试一次真登录"
               "（只读一条 <code>hostname</code>，不改任何配置、不落盘）："
               "填完先测、通了再保存，就不必等到某天认证失败才发现配错了。"
               "框里<b>留空 = 用这台主机已保存的那一项</b>，所以「存了但不确定」"
               "也可以直接测。<b>保存会先列一份「将要改动」让你确认</b>，"
               "确认后保存，并把所有能连的主机各测一遍。")
    )
    return card(body, title="SSH 登录（每台主机一份凭据）",
                desc="数据源 /settings/api 的 ops.credentials")


def _footer_details() -> str:
    items = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in DEFS.items())
    return (
        '<details class="acc"><summary>口径与安全说明（这一页每个数字的定义）</summary>'
        f'<dl class="kv">{items}</dl>'
        '<p class="hint">页面上每个 ⓘ 悬停显示的就是同一段文字 —— 口径只维护一处，'
        "不会出现「提示和文档说的不一样」。</p></details>"
    )


def _body() -> str:
    model = _model_info()
    # 服务端就能定下来的两格：剧本份数（数文件）与模型（读 PROVIDERS）。
    # 其余五格要接口才知道，浏览器填入 —— 取不到就保持破折号。
    plays = _play_count()
    srv = {
        "plays": f"{plays} 份" if plays else "—",
        "model": model["model"] if model["configured"] else "未配置",
    }
    hero = (
        '<div class="hero">'
        "<h1>现在什么生效</h1>"
        "<p>这一页看的是<b>运行时装配结果</b>，不是配置文件里的声明 —— "
        "声明与生效不一致时，这里显示生效的那个。密钥只显示状态，永不回显。</p>"
        '<div class="btnrow">'
        + link_button("看板与对账", "/dashboard", tone="ghost")
        + link_button("API 文档", "/docs", tone="ghost")
        + "</div>"
        '<div class="tags"><span>改动即时生效</span><span>写回 .env 并自动备份</span>'
        "<span>密钥永不回显</span></div>"
        "</div>"
    )
    return f"""<div class="stack">
  <div id="err"></div>

  {hero}

  <div class="kpis">{"".join(_kpi_shell(k, lbl, tip, srv.get(k, "—"))
                             for k, lbl, tip in SUMMARY_TILES)}</div>

  <div id="tokbar" class="card tight" hidden>
    <div class="btnrow">
      <input type="text" id="token" class="mono"
             placeholder="访问令牌（本实例开启了鉴权）" aria-label="访问令牌">
      {button("保存令牌", size="sm", attrs='id="tokSave"')}
      <span class="hint">令牌只存在本机浏览器，服务端不回显、页面不渲染。</span>
    </div>
  </div>

  <section class="section">
    <h2>模型</h2>
    <div class="grid c2">{_model_section(model)}{_cost_card()}</div>
  </section>

  <section class="section">
    <h2>检索</h2>
    <div class="grid c2">{_retrieval_section()}{_limits_card()}</div>
  </section>

  <section class="section">
    <h2>告警与通知</h2>
    <div class="grid c2">{_notify_section()}{_alert_card()}</div>
  </section>

  <section class="section">
    <h2>沙箱与审批</h2>
    <div class="grid c2">{_sandbox_section()}{_devprefix_card()}</div>
  </section>

  <section class="section">
    <h2>安全与限额</h2>
    <div class="grid c2">{_security_section()}
      {_settings_form()}</div>
  </section>

  <section class="section">
    <h2>SSH 登录</h2>
    {_credentials_card()}
  </section>

  <section class="section">
    <h2>日志与可观测</h2>
    <div class="grid c2">{_logging_section()}{_log_files_card()}</div>
  </section>

  <section class="section">{_footer_details()}</section>
</div>"""


# ============================================================
# 四、前端：取数（白名单）与填值
# ============================================================
_JS = r"""
/* ============================================================
   设置页：只填值，不重建结构。
   ★ 纪律：从 /settings/api 只取下面点名的那几个字段 ——
     不取 auth.token、不取 auth.token_masked、不取 ops.key。
     没有值被写进 DOM，也就不存在"忘了删掉那一行"。
   ============================================================ */
document.addEventListener("DOMContentLoaded", function () {
  "use strict";
  var KEY = "agentdesk_token";
  var $ = function (id) { return document.getElementById(id); };
  var DASH = "\u2014";

  /* 从 URL hash 收一次令牌（方便在被鉴权挡住的实例上一次性进入；存本机浏览器） */
  (function () {
    var m = /(?:^|[#&])token=([^&]+)/.exec(location.hash || "");
    if (m) {
      try { localStorage.setItem(KEY, decodeURIComponent(m[1])); } catch (e) {}
      history.replaceState(null, "", location.pathname + location.search);
    }
  })();

  function tok() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }
  function headers(json) {
    var h = {};
    var t = tok();
    if (t) h["X-API-Key"] = t;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }
  function fetchJson(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        try { data = JSON.parse(txt); } catch (e) { data = null; }
        return { status: r.status, ok: r.ok, data: data };
      });
    });
  }
  /* 每项失败都带**一句原因**，而不是把整页搞崩 */
  function fail(r, what) {
    if (r.status === 401) return "需要令牌（HTTP 401）";
    if (r.status === 403) return "被拒绝（HTTP 403）";
    if (r.status === 404) return what + "不存在（HTTP 404）";
    if (r.status === 503) return "依赖不健康（HTTP 503）";
    return what + "取不到（HTTP " + r.status + "）";
  }

  function setText(id, text) {
    var el = $(id);
    if (el) el.textContent = text;
  }
  /* 填一行 `.field` 的值：label 在服务端渲染，这里按顺序换掉骨架 */
  function rowsOf(sel, pairs) {
    var rows = document.querySelectorAll(sel + " .field > div");
    for (var i = 0; i < rows.length; i++) {
      rows[i].innerHTML = (pairs[i] === undefined ? DASH : pairs[i]);
    }
  }
  function note(id, text, tone) {
    var el = $(id);
    if (el) el.innerHTML = tone === "warn"
      ? '<div class="note warn"><span class="ic">\u26a0</span><span>' + text + "</span></div>"
      : '<p class="hint">' + text + "</p>";
  }
  function badgeHtml(text, tone) {
    return '<span class="badge ' + tone + '">' + AD.esc(text) + "</span>";
  }
  function code(v) { return "<code>" + AD.esc(v) + "</code>"; }

  /* ---------- 现状摘要 ---------- */
  function tileAuth(h) {
    var s = (h && h.security) || null;
    if (!s) { setText("k-auth", DASH); return; }
    setText("k-auth", s.auth_enabled ? "已开启" : "关闭");
    $("k-auth").title = s.auth_enabled ? "接口都要令牌" : "本地开发模式：不需要令牌";
  }
  function tileQuota(h) {
    var s = (h && h.security) || null;
    if (!s) { setText("k-quota", DASH); return; }
    setText("k-quota", (s.daily_quota > 0 ? s.daily_quota : "不限") +
      (s.daily_quota > 0 ? " · 用 " + (s.daily_used || 0) : ""));
  }
  function tileNotify(n) {
    if (!n || typeof n.enabled !== "boolean") { setText("k-notify", DASH); return; }
    setText("k-notify", n.enabled ? (n.channels || []).join("、") : "未启用");
  }
  function tileRetrieval(r) {
    if (!r || !r.embedder) { setText("k-retrieval", DASH); return; }
    setText("k-retrieval", r.embedder.model || r.embedder.backend || DASH);
  }
  function tileSandbox(s) {
    if (!s || !s.backend) { setText("k-sandbox", DASH); return; }
    setText("k-sandbox", s.backend === "docker" ? "容器隔离"
      : (s.backend === "mock" ? "仿真（无真执行）" : "无隔离"));
  }

  /* ---------- 分组 ---------- */
  function renderRag(r) {
    if (!r || !r.embedder) {
      rowsOf("#ragRows", [DASH, DASH]);
      note("ragNote", "取不到索引统计：服务端还没有构建索引（跑一次检索或 /rag/index 重建）。");
      return;
    }
    var e = r.embedder;
    rowsOf("#ragRows", [
      code(e.model || e.backend) + " " + badgeHtml(
        e.backend === "local" ? "词袋哈希兜底" : "语义检索",
        e.backend === "local" ? "warn" : "ok"),
      code(r.documents + " 篇 / " + r.chunks + " 块") + " " + badgeHtml(
        r.embedder_ready ? "语义后端就绪" : "未启用语义后端",
        r.embedder_ready ? "ok" : "neutral")
    ]);
    note("ragNote", r.embedder_ready
      ? "当前向量后端是真语义的 —— 换个说法问同一件事也能召回。"
      : "当前是 local-hash 兜底：<b>只有词面匹配，没有语义理解</b>。"
        + "填好 DASHSCOPE_API_KEY 并重建索引后才算真的语义检索。",
      r.embedder_ready ? "" : "warn");
  }

  function renderNotify(d) {
    if (!d) {
      rowsOf("#notifyRows", [DASH, DASH]);
      note("notifyNote", "取不到通知装配结果。");
      return;
    }
    var n = d.notify || {};
    rowsOf("#notifyRows", [
      (n.enabled ? code((n.channels || []).join("、"))
                 : badgeHtml("未启用（默认不出站）", "neutral"))
        + (n.hint ? ' <span class="hint">' + AD.esc(n.hint) + "</span>" : ""),
      badgeHtml(n.mask ? "开（默认）" : "关", n.mask ? "ok" : "warn")
    ]);
    note("notifyNote", n.enabled
      ? "启用中：只有上面这些渠道会被构造，其它一律不发出站请求。"
      : "未配置通知（完全不出站）—— 这是默认状态，不是故障。要打开见 .env.example 的「出站通知」。");
  }

  /* 聚合器的运转状态（entries / max_entries / window）—— 与通知渠道分开一栏 */
  function renderAgg(d) {
    if (!d || !d.aggregate) {
      rowsOf("#aggRows", [DASH, DASH]);
      note("aggNote", "取不到聚合器状态。");
      return;
    }
    var a = d.aggregate;
    rowsOf("#aggRows", [
      code(a.mode || DASH) + " " + badgeHtml("去重表 " + (a.entries || 0) + " 条", "neutral")
        + ' <span class="hint">窗口 ' + (a.window_seconds || 0) + " 秒</span>",
      code(String(a.max_entries || 0)) + ' <span class="hint">超出先清过期、再淘汰最旧</span>'
    ]);
    note("aggNote", a.mode === "aggregate"
      ? "当前按「主机 + 服务」聚合：一次风暴里的 50 条同源告警 = 1 次诊断。"
      : "当前是旧的精确去重语义（每条告警各自成一个事件）—— 与加聚合之前的行为一致，"
        + "用于对比与回退。");
  }

  function renderSandbox(s) {
    if (!s || !s.backend) {
      rowsOf("#sandboxRows", [DASH, DASH, DASH]);
      note("sandboxNote", "取不到沙箱状态。");
      return;
    }
    var p = s.policy || {};
    rowsOf("#sandboxRows", [
      code(s.backend) + " " + badgeHtml(s.isolated ? "真隔离" : "无容器隔离",
        s.isolated ? "ok" : (s.backend === "mock" ? "neutral" : "warn")),
      code((p.whitelisted || 0) + " 条") + " " +
        badgeHtml("允许 " + (p.allow || 0), "ok") + " " +
        badgeHtml("需审批 " + (p.needs_approval || 0), "warn"),
      code((p.needs_approval || 0) + " 个命令") + " " +
        badgeHtml("指纹绑定参数", "info")
    ]);
    var problems = s.problems || [];
    note("sandboxNote", (s.note || "") +
      (problems.length ? " " + problems.join(" ") : ""),
      problems.length ? "warn" : "");
  }

  function renderHealth(h, secOk) {
    if (!h || !h.security) {
      rowsOf("#securityRows", [DASH, DASH, DASH, DASH, DASH]);
      return;
    }
    var s = h.security;
    rowsOf("#securityRows", [
      badgeHtml(s.auth_enabled ? "已开启" : "关闭", s.auth_enabled ? "ok" : "neutral")
        + ' <span class="hint">AUTH_ENABLED=' + (s.auth_enabled ? "1" : "0") + "</span>",
      (secOk ? badgeHtml(s.token_configured ? "已设置" : "未设置",
                         s.token_configured ? "ok" : "neutral")
             : badgeHtml("未设置", "neutral"))
        + ' <span class="hint">' + (secOk ? "不显示长度以外的任何信息" : "需要令牌才能读") + "</span>",
      code((s.rate_limit_per_ip_per_min || 0) + " / " +
           (s.rate_limit_global_per_min || 0)) + " 次",
      code(s.daily_quota > 0 ? s.daily_quota : "不限") +
        (s.daily_quota > 0 ? "" : " " + badgeHtml("不限量", "warn")),
      code(s.daily_used || 0) + ' <span class="hint">进程内计数</span>'
    ]);
  }

  function renderOps(ops) {
    if (!ops) { $("opsview").innerHTML = AD.empty("取不到数据源配置", "需要令牌或被拒绝"); return; }
    var pairs = [];
    var targets = ops.targets || {};
    var keys = Object.keys(targets);
    pairs.push(["当前后端", code(ops.backend || DASH)]);
    pairs.push(["能说出名字的主机",
      ops.known_hosts && ops.known_hosts.length
        ? code(ops.known_hosts.join("、")) : '<span class="hint">（无）</span>']);
    pairs.push(["SSH 目标（已解析）",
      keys.length ? code(keys.map(function (k) {
        return k + " \u2192 " + targets[k];
      }).join("  ")) : '<span class="hint">（未配置）</span>']);
    pairs.push(["私钥路径", ops.key_configured
      ? badgeHtml("已设置", "ok") : badgeHtml("未设置", "neutral")]);
    $("opsview").innerHTML = '<dl class="kv">' + pairs.map(function (p) {
      return "<dt>" + AD.esc(p[0]) + "</dt><dd>" + p[1] + "</dd>";
    }).join("") + "</dl>";
  }

  /* ---------- 主流程：每一项独立取数，互不拖垮 ---------- */
  // ★ 真实 AGENT_TOKEN / DEEPSEEK_API_KEY 的值**永远不取**：
  //    不取 auth.token、不取 auth.token_masked。
  //    ops.key 是私钥**路径**（不是密钥内容），凭据卡要原样回传它 ——
  //    见上方「密码只写不读」那一段。
  function load() {
    var h = headers();
    var secOk = false;

    fetchJson("/settings/api", { headers: h }).then(function (r) {
      if (!r.ok) {
        if (r.status === 401) {
          $("tokbar").hidden = false;
          note("err", "本实例开启了鉴权：读配置需要<b>当前有效的令牌</b>。"
            + "填一次（或用 URL 上的 #token=…）后本页自动载入。", "warn");
        } else {
          note("err", "取配置失败：" + fail(r, "设置接口"), "warn");
        }
        renderOps(null);
        renderCreds(null);
        return;
      }
      secOk = true;
      var ops = r.data.ops || {};
      /* ★ 凭据卡的请求体要**原样**带上这四个字段：POST /settings/ops 的语义是
         "没给的字段 = 空值"，少带一个就会把用户配好的目标 / 全局私钥 / 清单冲掉。
         所以这里存一份快照 —— 它只用于回传，界面不显示、也不允许在这里编辑。 */
      OPS = { backend: ops.backend, targets_raw: ops.targets_raw,
              key: ops.key, hosts_env: ops.hosts_env };
      renderCreds(ops.credentials);
      renderOps({
        backend: ops.backend,
        targets: ops.targets,
        known_hosts: ops.known_hosts,
        /* ★ 只把"有没有配"这一个布尔交出去 —— 全局私钥的路径不进这一栏 */
        key_configured: !!(ops.key && String(ops.key).length),
        raw_targets: ops.targets_raw
      });
      if ($("targets") && ops.targets_raw) $("targets").value = ops.targets_raw;
      if ($("hosts") && ops.hosts_env) $("hosts").value = ops.hosts_env;
      var radios = document.querySelectorAll('input[name=backend]');
      for (var i = 0; i < radios.length; i++) {
        radios[i].checked = (radios[i].value === ops.backend);
      }
      setText("persist", "持久化位置：" + (r.data.env_file || DASH) +
        " · 每次修改前把上一份备份为 .env.bak.settings");
    }).catch(function () {
      note("err", "取配置失败：请求本身出错（服务没起来或网络断了）。", "warn");
      renderOps(null);
      renderCreds(null);
    });

    fetchJson("/health", { headers: h }).then(function (r) {
      if (!r.ok) { tileAuth(null); tileQuota(null); renderHealth(null, secOk); return; }
      tileAuth(r.data); tileQuota(r.data); renderHealth(r.data, secOk);
    }).catch(function () { tileAuth(null); tileQuota(null); renderHealth(null, secOk); });

    fetchJson("/incidents?limit=1", { headers: h }).then(function (r) {
      if (!r.ok) { tileNotify(null); renderNotify(null); renderAgg(null); return; }
      tileNotify(r.data.notify);
      renderNotify(r.data);
      renderAgg(r.data);
    }).catch(function () { tileNotify(null); renderNotify(null); renderAgg(null); });

    fetchJson("/rag/stats", { headers: h }).then(function (r) {
      renderRag(r.ok ? r.data : null);
      tileRetrieval(r.ok ? r.data : null);
    }).catch(function () { renderRag(null); tileRetrieval(null); });

    fetchJson("/sandbox", { headers: h }).then(function (r) {
      renderSandbox(r.ok ? r.data : null);
      tileSandbox(r.ok ? r.data : null);
    }).catch(function () { renderSandbox(null); tileSandbox(null); });
  }

  /* ---------- 令牌 ---------- */
  if ($("tokSave")) {
    $("tokSave").onclick = function () {
      var v = ($("token").value || "").trim();
      if (!v) { note("err", "令牌为空 —— 留空就是不修改。", "warn"); return; }
      try { localStorage.setItem(KEY, v); } catch (e) {}
      $("token").value = "";
      location.reload();
    };
  }
  if ($("gen")) {
    $("gen").onclick = function () {
      var b = new Uint8Array(32);
      crypto.getRandomValues(b);
      var s = btoa(String.fromCharCode.apply(null, b));
      $("newtok").value = s.split("+").join("-").split("/").join("_")
        .split("=").join("");
    };
  }
  if ($("savetok")) {
    $("savetok").onclick = function () {
      var v = ($("newtok").value || "").trim();
      if (!v) { note("tokmsg", "新令牌为空 —— 留空就是不修改。", "warn"); return; }
      fetchJson("/settings/token", {
        method: "POST", headers: headers(true), body: JSON.stringify({ token: v })
      }).then(function (r) {
        if (!r.ok) {
          var d = (r.data && r.data.detail) || ("HTTP " + r.status);
          note("tokmsg", "修改失败：" + AD.esc(d), "warn");
          return;
        }
        /* 立刻换上新手，免得把自己锁在门外（与旧页一致，只存本机浏览器） */
        try { localStorage.setItem(KEY, v); } catch (e) {}
        $("newtok").value = "";
        note("tokmsg", "令牌已修改并即时生效。" + AD.esc(r.data.persist || "") +
          " —— 旧令牌已作废，其它页面与调用方要换用新令牌。");
        load();
      }).catch(function () {
        note("tokmsg", "修改失败：请求本身出错。", "warn");
      });
    };
  }

  /* ---------- 数据源 ---------- */
  if ($("applyops")) {
    $("applyops").onclick = function () {
      var picked = document.querySelector('input[name=backend]:checked');
      if (!picked) { note("opsmsg", "请先选一个后端。", "warn"); return; }
      fetchJson("/settings/ops", {
        method: "POST", headers: headers(true),
        body: JSON.stringify({
          backend: picked.value,
          targets: ($("targets").value || "").trim(),
          key: ($("key").value || "").trim(),
          hosts: ($("hosts").value || "").trim()
        })
      }).then(function (r) {
        if (!r.ok) {
          var d = (r.data && r.data.detail) || ("HTTP " + r.status);
          note("opsmsg", "应用失败：" + AD.esc(d), "warn");
          return;
        }
        note("opsmsg", "数据源已切换为 " + code(r.data.backend) +
          " 并即时生效。" + AD.esc(r.data.persist || "") +
          ' 到 <a href="/try">提问页</a> 问一句机器状态即可验证。' +
          (r.data.warning ? '<div class="note warn"><span class="ic">\u26a0</span>'
            + "<span>" + AD.esc(r.data.warning) + "</span></div>" : ""));
        $("key").value = "";
        load();
      }).catch(function () {
        note("opsmsg", "应用失败：请求本身出错。", "warn");
      });
    };
  }

  /* ============================================================
     SSH 凭据（每台主机一份）：私钥路径 / 密码 / 测试登录
     ------------------------------------------------------------
     ★ 密码只写不读 —— 五条通道全堵上：
        ① 接口不回显（只有 password_set），所以密码**无从**被渲染
        ② 密码只进 input.value 与请求体；innerHTML 里只出现静态文字
        ③ 提交后（成功或失败）立刻 input.value = ""，只留 placeholder
        ④ 失败回执只取 loc/msg，**不复述 detail[].input**（422 会带密码！）
        ⑤ 测试登录的**结果文字只来自 reason / remote / latency_ms**，
           请求体本身从不进 DOM；测完清空密码框，测不了也不把它的值打到页面上
     ★ 私钥是**路径**：回填、编辑、原样回传都是对的（不给 = 被清空）。
     ============================================================ */
  var CREDS = [];        /* 接口给的凭据视图（永远不含密码字段） */
  var OPS = null;        /* 原样回传用的数据源快照（见 load() 里的说明） */
  /* 测试登录的并发闸：一行一个在跑，就不许再点（同一行点两次会发两次连接） */
  var TESTING = {};
  /* 保存流程的两个状态：确认条显示时不能改行；保存+连测期间一切按钮停用 */
  var PENDING = null;    /* 已确认、等待（或正在）提交的凭据数组 */
  var SAVING = false;
  /* 只读一条 hostname；0 = 服务端默认 8 秒（接口上限 60） */
  var TEST_TIMEOUT = 0;
  /* 确认条的比对基准：**每一次渲染或成功保存之后**刷新成"服务端现在是什么"。
     声明放在这里（在 renderCreds 之前）是有意的 —— 它必须在使用之前就初始化，
     免得将来有人不小心让 renderCreds 在赋值语句之前跑起来。 */
  var PENDING_BASE = [];

  function credNoteHtml(text, tone) {
    var ic = tone === "warn" ? "\u26a0" : (tone === "ok" ? "\u2713" : "\u25b8");
    var cls = "note" + (tone ? " " + tone : "");
    return '<div class="' + cls + '"><span class="ic">' + ic + "</span><span>"
      + text + "</span></div>";
  }
  function credMsg(parts) {
    var el = $("credMsg");
    if (el) el.innerHTML = parts.join("");
  }
  function credPwdBadge(set) {
    return badgeHtml(set ? "已设置" : "未设置", set ? "ok" : "neutral");
  }
  /* 目标 → `user@host:port`；没有目标返回 null（这一行连不上、也不参与保存） */
  function targetText(t) {
    if (!t) return null;
    return String(t.user || "") + "@" + String(t.host || "")
      + (t.port ? ":" + t.port : "");
  }

  /* 一行的**结构**（服务端渲染不出来：行的内容只有接口知道）
     版式：主机名 + 目标 / 认证方式 | 私钥路径 / 密码（两列，窄屏自动堆叠） */
  function credRowHtml(c, i) {
    var tt = targetText(c.target);
    return '<div class="field" id="credRow-' + i + '">'
      + '<label class="f">' + AD.esc(c.host) + " "
      + (tt === null ? badgeHtml("尚未配置目标", "warn")
                     : '<span class="hint">' + AD.esc(tt) + "</span>")
      + "</label>"
      + '<div class="grid c2">'
      + '<div><label class="f" for="credAuth-' + i + '">认证方式</label>'
      + '<select id="credAuth-' + i + '" aria-label="认证方式">'
      + '<option value="key">私钥</option>'
      + '<option value="password">密码</option></select></div>'
      + '<div><label class="f" for="credKey-' + i + '">私钥路径</label>'
      + '<input type="text" id="credKey-' + i + '" class="mono" '
      + 'placeholder="选「私钥」时生效；留空 = 不修改"></div>'
      + '<div><label class="f" for="credPwd-' + i + '">密码 '
      + credPwdBadge(!!c.password_set) + "</label>"
      + '<div class="btnrow">'
      /* ★ 密码框：没有 value、没有回填，autocomplete 关掉（免得浏览器把管理员
         自己的密码自动填进来，一保存就把它写到 .env 里去了） */
      + '<input type="password" id="credPwd-' + i + '" class="mono" '
      + 'autocomplete="new-password" aria-label="密码" placeholder="'
      + (c.password_set ? "已设置（留空表示不修改）" : "未填写") + '">'
      + (c.password_set
         ? '<label class="switch"><input type="checkbox" id="credClear-' + i
           + '"> 清除密码</label>'
         : "")
      /* 「测试登录」：用**这一行当前填的值**试一次真登录；没有目标的行连不上，
         按钮照样渲染（停用 + title 说明原因），比"这个功能有时不存在"清楚 */
      + '<button type="button" class="btn ghost sm" id="credTest-' + i
      + '">测试登录</button>'
      + "</div>"
      /* 测试结果就地写在这一行（不用 alert）：内容只有 ✅/❌ + 中文原因 */
      + '<div class="hint" id="credTestStatus-' + i + '"></div>'
      + "</div></div></div>";
  }

  /* 认证方式切换：选「密码」时私钥框禁用但**保留值**（切回来还在，别清空） */
  function toggleCredAuth(i) {
    var row = $("credRow-" + i), sel = $("credAuth-" + i), keyEl = $("credKey-" + i);
    if (!row || !sel || !keyEl) return;
    if (row.getAttribute("data-notarget") === "1") return;   /* 整行已停用 */
    keyEl.disabled = (sel.value !== "key");
  }

  /* 逐行**填值**：值只走 .value（密钥类字段绝不进 innerHTML） */
  function fillCredRow(c, i) {
    var row = $("credRow-" + i), sel = $("credAuth-" + i);
    var keyEl = $("credKey-" + i), pwdEl = $("credPwd-" + i);
    var clr = $("credClear-" + i);
    var noTarget = !c.target;
    if (row) row.setAttribute("data-notarget", noTarget ? "1" : "0");
    if (sel) sel.value = (c.auth === "password") ? "password" : "key";
    if (keyEl) keyEl.value = c.key || "";   /* 路径：可以显示 */
    if (pwdEl) pwdEl.value = "";            /* ★ 密码：永远是空的 */
    if (clr) clr.checked = false;           /* 清除是"这一次的动作"，不是状态 */
    if (noTarget) {
      var ctrls = row ? row.querySelectorAll("select,input") : [];
      for (var k = 0; k < ctrls.length; k++) ctrls[k].disabled = true;
      /* 没有目标就没得连：测试按钮也停用，并把**原因**写在 title 上
         （灰按钮 + 有理由，胜过"点了没反应"） */
      var tb = $("credTest-" + i);
      if (tb) {
        tb.disabled = true;
        tb.title = "这台主机还没有 SSH 目标，没有地址可连 —— "
          + "先去「工具层数据源」配 名=user@地址[:端口]";
      }
      setRowTest(i, "\u25b8 没有 SSH 目标 —— 不能测（不参与保存）", "");
      return;
    }
    toggleCredAuth(i);
  }

  /* ============================================================
     测试登录：立刻回答"我填的私钥/密码到底通不通"
     ------------------------------------------------------------
     · 请求体 = **这一行当前的值**（auth / key / password）；框里留空 =
       用这台主机已保存的那一项 —— 所以"存了但不确定"直接点就行
     · 结果只来自回执的 reason / remote / latency_ms；**请求体本身不进 DOM**
     · 400 的 detail 是字符串（原样显示，已经是对人友好的中文）；
       422 只取 loc + msg（绝不复述 input，那里可能是密码）
     · 测试**不改服务端任何状态**（接口不落盘），也不改本行任何控件的值
     ============================================================ */
  function setRowTest(i, text, tone) {
    var el = $("credTestStatus-" + i);
    if (!el) return;
    el.textContent = text;
    /* 颜色走 CSS 类（`.hint.ok` / `.hint.warn`），不写内联样式 ——
       内联样式在这个项目里是禁止的（样式只有 app.css 一处来源） */
    el.className = "hint" + (tone ? " " + tone : "");
  }
  function testNote(i, text, tone, pwdCleared) {
    var extra = (tone === "warn" && pwdCleared)
      ? "\uff08你刚敲的密码已从输入框清掉，不留在页面上\uff09" : "";
    setRowTest(i, text + extra, tone);
  }

  function runTests(ids, done) {
    var at = 0, ok = 0, miss = 0;
    function step() {
      if (at >= ids.length) { done(ok, miss); return; }
      var i = ids[at++];
      /* 串行：一台测完再测下一台（别并发打一堆 SSH 连接出去） */
      testRow(i, function (good) {
        if (good === null) miss++; else if (good) ok++;
        step();
      });
    }
    step();
  }

  function testRow(i, cb) {
    var c = CREDS[i], tb = $("credTest-" + i);
    if (!c || !c.target || TESTING[i] || SAVING) return;
    TESTING[i] = true;
    var sel = $("credAuth-" + i), keyEl = $("credKey-" + i);
    var pwdEl = $("credPwd-" + i);
    var auth = sel ? sel.value : "key";
    var key = (auth === "key" && keyEl) ? (keyEl.value || "").trim() : "";
    /* ★ 密码原样送出、不 trim（密码里可以有空格）；留空 = 用已保存的那个 */
    var pwd = pwdEl ? (pwdEl.value || "") : "";
    if (tb) { tb.disabled = true; tb.innerHTML = '<span class="spin"></span>'; }
    setRowTest(i, "测试中\u2026 登录一次，最多 8 秒", "");
    fetchJson("/settings/ssh/test", {
      method: "POST", headers: headers(true),
      body: JSON.stringify({ host: c.host, auth: auth, key: key,
                             password: pwd, timeout: TEST_TIMEOUT })
    }).then(function (r) {
      TESTING[i] = false;
      if (tb) tb.disabled = false;
      if (pwdEl && pwd) pwdEl.value = "";     /* ★ 用掉就清掉，别留在 DOM 里 */
      var d = r.data || {};
      if (!r.ok) {
        /* 400：detail 是字符串，原样显示；422：只取 loc + msg（里面的 input 是密码） */
        testNote(i, "\u274c " + credErrText(d && d.detail, r.status), "warn", !!pwd);
        if (cb) cb(null);
        return;
      }
      if (d.ok) {
        /* 成功 = 真的落到目标机器上了：报远端主机名而不只是"连上了" */
        setRowTest(i, "\u2705 通 \u00b7 " + (d.latency_ms || 0) + " ms \u00b7 远端主机名 "
          + (d.remote || "(未返回)") + "\uff08此行已确认\uff09", "ok");
        if (cb) cb(true);
        return;
      }
      testNote(i, "\u274c " + (d.reason || "登录失败（回执里没有原因）"), "warn", !!pwd);
      if (cb) cb(false);
    }).catch(function () {
      TESTING[i] = false;
      if (tb) tb.disabled = false;
      if (pwdEl && pwd) pwdEl.value = "";
      setRowTest(i,
        "\u274c 测试没发出去：请求本身出错（服务没起来或网络断了）", "warn");
      if (cb) cb(null);
    });
  }

  function renderCreds(list) {
    var box = $("credRows");
    if (!box) return;
    if (!list) {                                  /* 取不到 ≠ 没有主机 */
      CREDS = [];
      box.innerHTML = AD.empty("取不到凭据清单", "需要令牌或被拒绝");
      setText("credSummary", "取不到凭据清单 —— 先解决取数，再谈保存。");
      setText("k-creds", DASH);
      if ($("saveCreds")) $("saveCreds").disabled = true;
      return;
    }
    CREDS = list;
    PENDING_BASE = list.slice();   /* 确认条的比对基准：就是这一次渲染用的视图 */
    var pwdN = 0, noT = 0, i;
    for (i = 0; i < CREDS.length; i++) {
      if (CREDS[i].password_set) pwdN++;
      if (!CREDS[i].target) noT++;
    }
    setText("k-creds", CREDS.length
      ? CREDS.length + " 台 · " + pwdN + " 台用密码" : "没有主机");
    setText("credSummary", "共 " + CREDS.length + " 台主机 · 密码已设置 " + pwdN + " 台"
      + (noT ? " · " + noT + " 台尚未配置目标（不参与保存）" : ""));
    if (!CREDS.length) {
      box.innerHTML = AD.empty("没有主机",
        "先在「工具层数据源」里配 SSH 目标或主机清单，再回来配凭据");
      if ($("saveCreds")) $("saveCreds").disabled = true;
      return;
    }
    var html = "";
    for (i = 0; i < CREDS.length; i++) html += credRowHtml(CREDS[i], i);
    box.innerHTML = html;
    for (i = 0; i < CREDS.length; i++) {
      fillCredRow(CREDS[i], i);
      bindCredRow(i);
    }
    /* 一行都连不上的时候，这个按钮什么也做不了 */
    if ($("saveCreds")) $("saveCreds").disabled = (noT === CREDS.length);
  }
  function bindCredRow(i) {
    var sel = $("credAuth-" + i);
    if (sel) sel.onchange = function () { toggleCredAuth(i); };
    var tb = $("credTest-" + i);
    if (tb) {
      tb.onclick = function () { testRow(i, null); };
      /* 确认条显示时、或保存+连测期间：不许再点（点两次会打两次 SSH 连接） */
      if (PENDING || SAVING) tb.disabled = true;
    }
  }

  /* 收集请求体里的 credentials：**只读 DOM 的值**，不做任何猜测 */
  function collectCreds() {
    var out = [], i;
    for (i = 0; i < CREDS.length; i++) {
      var c = CREDS[i];
      if (!c.target) {
        /* 没有目标的行整行留空 = 服务端语义的"不改这项"
           （auth/key 留空不改，password 留空不改，clear_password=false）——
           它已有的密码因此不会被这次保存顺手抹掉。 */
        out.push({ host: c.host, auth: "", key: "", password: "",
                   clear_password: false });
        continue;
      }
      var sel = $("credAuth-" + i), keyEl = $("credKey-" + i);
      var pwdEl = $("credPwd-" + i), clr = $("credClear-" + i);
      var auth = sel ? sel.value : "key";
      out.push({
        host: c.host,
        auth: auth,
        /* 私钥路径只在「私钥」方式下提交（选密码时那句 key 留空 = 不改） */
        key: (auth === "key" && keyEl) ? (keyEl.value || "").trim() : "",
        /* ★ 密码原样提交、**不 trim**（密码里可以有空格）；留空 = 不修改 */
        password: pwdEl ? (pwdEl.value || "") : "",
        clear_password: !!(clr && clr.checked)
      });
    }
    return out;
  }

  /* 提交后立刻清空所有密码框：只在 placeholder 里留状态 */
  function clearPwdInputs() {
    for (var i = 0; i < CREDS.length; i++) {
      var el = $("credPwd-" + i);
      if (el) el.value = "";
    }
  }

  /* 失败信息：400 是 detail 字符串；422 的 detail 是列表，**只取 loc + msg**。
     ★ FastAPI 的 422 里每个错误项都有一个 `input`，那正是用户填的值 ——
       密码校验失败时它就是密码。所以这里一个字段一个字段地挑，绝不整块塞进页面。 */
  function credErrText(detail, status) {
    if (typeof detail === "string" && detail) return detail;
    if (detail && detail.length) {
      var parts = [], i;
      for (i = 0; i < detail.length && i < 4; i++) {
        var e = detail[i] || {};
        parts.push((e.loc || []).join(".") + "：" + (e.msg || "校验不通过"));
      }
      if (parts.length) return parts.join("；");
    }
    return "HTTP " + status;
  }

  /* ============================================================
     保存前确认：**先把"将要改动什么"列出来**，确认之后才真的提交
     ------------------------------------------------------------
     · 比对基准 = `PENDING_BASE`（上一次渲染这一卡时接口给的视图）。
       比对发生在**收集到的请求体**与它之间，所以"变了没有"与"提交什么"
       是同一份数据算出来的，不会出现"确认条说的"和"实际提交的"不一致。
     · ★ 这里只说**状态**不说值：「密码 → 已设置」而不是把用户敲的密码写出来。
     · 不动任何服务端状态：真正的请求在 doSave() 里，只有「确认保存」才走到。
     ============================================================ */
  function prevCreds(host) {
    for (var i = 0; i < PENDING_BASE.length; i++) {
      if (PENDING_BASE[i].host === host) return PENDING_BASE[i];
    }
    return null;
  }
  function clip(v, n) {
    v = String(v === undefined || v === null ? "" : v);
    return v.length > n ? v.slice(0, n) + "\u2026" : v;
  }
  /* 一条改动 = 描述**哪个字段**从什么变成什么；密码只说"已设置/清除"，不说值 */
  function credChangeItems(newC, oldC) {
    var items = [], oldAuth = oldC.auth === "password" ? "password" : "key";
    var newAuth = newC.auth === "password" ? "password" : "key";
    /* 私钥**路径**可以显示（它是文件在哪儿，不是密钥内容） */
    if (newAuth !== oldAuth) {
      items.push("认证方式 → " + (newAuth === "password" ? "密码" : "私钥"));
    }
    if (newAuth === "key" && newC.key && newC.key !== (oldC.key || "")) {
      items.push("私钥路径 → " + clip(newC.key, 120));
    }
    if (newC.clear_password) items.push("清除密码");
    else if (newC.password.length) {
      items.push("密码 → 已设置（" + newC.password.length + " 字符，不回显）");
    }
    return items;
  }
  function configChanges(creds) {
    var out = [], i;
    for (i = 0; i < creds.length; i++) {
      var e = creds[i];
      if (!e.host) continue;
      var oldC = prevCreds(e.host);
      if (!oldC) continue;                       /* 接口没给过的行：无可比对 */
      if (!oldC.target) {                        /* 没有目标的行 = "不改这项" */
        out.push({ host: e.host, items: ["没有 SSH 目标：这一行不参与保存"] });
        continue;
      }
      var items = credChangeItems(e, oldC);
      if (items.length) out.push({ host: e.host, items: items });
    }
    return out;
  }
  /* 保存成功之后把基准推进一步（**不再取一次接口**）：否则下一次确认条会拿
     "上一次进页面时"的旧值比对，把已经保存过的改动又列一遍 ——
     而用户会以为自己重复改了什么。行没动过的话这一步是恒等变换。 */
  function applySavedToBase(creds) {
    var i, j;
    for (i = 0; i < creds.length; i++) {
      var e = creds[i];
      for (j = 0; j < PENDING_BASE.length; j++) {
        var b = PENDING_BASE[j];
        if (b.host !== e.host || !b.target) continue;
        if (e.auth) b.auth = e.auth;                 /* 留空 = 不改这项 */
        if (e.auth === "key" && e.key) b.key = e.key;
        if (e.clear_password) b.password_set = false;
        else if (e.password.length) b.password_set = true;
      }
    }
  }

  function enableTestButtons(on) {
    for (var i = 0; i < CREDS.length; i++) {
      var tb = $("credTest-" + i);
      if (tb && !tb.title) tb.disabled = !on;    /* 没有目标的行保持停用 */
    }
  }
  function hideConfirm() {
    var box = $("credConfirm");
    if (box) { box.hidden = true; box.innerHTML = ""; }
    PENDING = null;
    enableTestButtons(true);
  }

  function showConfirm(creds) {
    var box = $("credConfirm");
    if (!box) return;
    var changes = configChanges(creds), body = "", i, j;
    if (!changes.length) {
      body = credNoteHtml("没有改动 —— 各行的值与已保存的一致，"
        + "这次保存不会改变任何东西。", "");
    } else {
      var lines = "";
      for (i = 0; i < changes.length; i++) {
        for (j = 0; j < changes[i].items.length; j++) {
          lines += '<div class="hint">' + AD.esc(changes[i].host) + "："
            + AD.esc(changes[i].items[j]) + "</div>";
        }
      }
      body = credNoteHtml("将要把 <b>" + changes.length + " 台主机</b>的改动写进 .env"
        + "（原文件先备份为 .env.bak.settings）：<br>" + lines, "warn");
    }
    box.innerHTML = body
      + '<div class="btnrow">'
      + '<button type="button" class="btn sm" id="credConfirmGo">确认保存</button>'
      + '<button type="button" class="btn ghost sm" id="credConfirmNo">取消</button>'
      + "</div>";
    box.hidden = false;
    PENDING = creds;                             /* 确认之后要提交的那一份载荷 */
    enableTestButtons(false);
    var go = $("credConfirmGo"), no = $("credConfirmNo");
    if (go) go.onclick = function () {
      var payload = PENDING;
      hideConfirm();
      doSave(payload);
    };
    if (no) no.onclick = function () {           /* ★ 取消：一个保存请求都不发 */
      hideConfirm();
      credMsg([credNoteHtml("已取消 —— 什么都没有提交。", "")]);
    };
  }

  /* 收集 + 本地校验（超长密码先在本地拦掉），然后进确认条 */
  function requestSave() {
    if (!OPS) {
      credMsg([credNoteHtml("还没取到配置（需要令牌，或服务没起来）—— "
        + "先解决取数，再保存凭据。", "warn")]);
      return;
    }
    if (SAVING || PENDING) return;
    var creds = collectCreds(), over = [], i;
    for (i = 0; i < creds.length; i++) {
      /* 服务端的硬上限是 500 字符（超了会 422）。在这里先拦一次：
         既不把密码送去撞一个"会把 input 回显出来"的错误，也不白丢一次保存。 */
      if (creds[i].password.length > 500) over.push(creds[i].host);
    }
    if (over.length) {
      credMsg([credNoteHtml("密码超过 500 字符（" + AD.esc(over.join("、"))
        + "）—— 已中止，什么都没有提交。", "warn")]);
      return;
    }
    showConfirm(creds);
  }

  /* 保存成功的回执。★ warning 必须显示：例如"配了密码登录但还没填密码"
     —— 不显示的话用户要到每次查询认证失败时才知道 */
  function afterSave(data, creds, prev) {
    /* 回执里不会说"哪台改了"，那就自己比：认证方式 / 私钥路径 / 新密码 / 清除 */
    var changed = [], k;
    for (k = 0; k < creds.length; k++) {
      var e = creds[k], old = null, j;
      for (j = 0; j < prev.length; j++) if (prev[j].host === e.host) old = prev[j];
      if (!old || !old.target) continue;
      var newAuth = e.auth || old.auth;
      if (e.password.length || e.clear_password || newAuth !== old.auth
          || (e.key && e.key !== (old.key || ""))) changed.push(e.host);
    }
    applySavedToBase(creds);         /* ★ 基准前移：下次确认条只列"还没保存的" */
    credMsg([
      credNoteHtml("已保存" + (changed.length
        ? "：" + AD.esc(changed.join("、")) : "（各行的值都没有变化）")
        + "。" + AD.esc(data.persist || ""), "ok"),
      data.warning ? credNoteHtml(AD.esc(data.warning), "warn") : ""
    ]);
    if (data.credentials && data.credentials.length) renderCreds(data.credentials);
    else load();
    testAllAfterSave();
  }

  /* 保存成功之后：对所有**有目标**的主机各测一次登录 —— 这才是"到底通不通"的答案 */
  function testAllAfterSave() {
    var ids = [], i;
    for (i = 0; i < CREDS.length; i++) {
      if (CREDS[i].target) ids.push(i);
    }
    if (!ids.length) return;
    credMsg([credNoteHtml("已保存。正在<b>逐台测试登录</b>（串行，每台最多 8 秒）…", "")]);
    runTests(ids, function (ok, miss) {
      var bad = ids.length - ok - miss;
      credMsg([
        credNoteHtml("已保存：逐台测过 " + ids.length + " 台，连通 <b>" + ok + "/"
          + ids.length + "</b>"
          + (bad ? " · 不通 " + bad + " 台（原因见各行）" : "")
          + (miss ? " · " + miss + " 台没能测（请求没发出去）" : ""),
          ok === ids.length ? "ok" : "warn")
      ]);
    });
  }

  function doSave(creds) {
    var btn = $("saveCreds");
    if (!creds) return;
    var label = btn ? btn.getAttribute("data-label") : "";
    SAVING = true;
    enableTestButtons(false);
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 保存中…'; }
    var prev = PENDING_BASE.slice();
    fetchJson("/settings/ops", {
      method: "POST", headers: headers(true),
      body: JSON.stringify({
        /* ★ 这四项**原样回传**（来自 /settings/api 的快照）：它们不属于这张卡，
           改一个字就会把用户配好的 SSH 目标 / 全局私钥 / 主机清单冲成空值 */
        backend: OPS.backend,
        targets: OPS.targets_raw,
        key: OPS.key,
        hosts: OPS.hosts_env,
        credentials: creds
      })
    }).then(function (r) {
      SAVING = false;
      clearPwdInputs();                  /* ★ 成败都清：密码不留在一个可被读走的地方 */
      if (btn) { btn.disabled = false; btn.innerHTML = AD.esc(label); }
      if (!r.ok) {
        credMsg([credNoteHtml("保存失败："
          + AD.esc(credErrText(r.data && r.data.detail, r.status)), "warn")]);
        return;                          /* ★ 保存失败不测试：先把配置改对 */
      }
      afterSave(r.data || {}, creds, prev);
    }).catch(function () {
      SAVING = false;
      clearPwdInputs();
      if (btn) { btn.disabled = false; btn.innerHTML = AD.esc(label); }
      credMsg([credNoteHtml("保存失败：请求本身出错（服务没起来或网络断了）。", "warn")]);
    });
  }

  /* 保存按钮只是"请求确认"：真正的提交在确认条的「确认保存」上 */
  if ($("saveCreds")) {
    $("saveCreds").setAttribute("data-label", $("saveCreds").innerHTML);
    $("saveCreds").onclick = requestSave;
  }
  /* 这里**不预先渲染**：服务端已经放好了骨架 + "载入中…"，
     取数结果一到（load() 里）再决定是"每台主机一行"还是"取不到"。 */

  load();
});
"""


def register(app) -> None:
    """把自己挂到 app 上（由 `app/web/pages/__init__.py` 的 IMPLEMENTED 调用）。"""

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page():
        """设置页。**现状摘要在服务端定标题、值在浏览器里填**。

        ★ 服务端渲染时**不接触任何密钥值**（见 `_model_info`）：所以
          "HTML 里没有密钥"是结构上的事实。前端取数也走白名单（见文件头纪律）。
        """
        from app import security

        env_badge = "公网 · 需令牌" if security.AUTH_ENABLED else "本地 · 免鉴权"
        actions = (link_button("刷新", "/settings", size="sm")
                   + link_button("接口文档", "/docs", size="sm"))
        html = shell(
            _body(),
            title="系统设置",
            active="/settings",
            subtitle="现在什么生效 · 密钥只显示状态，永不回显 · 改动即时生效并写回 .env",
            actions=actions,
            env_badge=env_badge,
            head_extra=f"<script>{_JS}</script>",
        )
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
