# -*- coding: utf-8 -*-
"""总览页（`/`）—— "这是什么 + 现在什么状态 + 我该点哪"。

【为什么不再是 README 风格】
旧首页是"把 README 渲染成 HTML"：单列、居中、markdown 味的表格、无导航。
访客打开一个**运维 Agent** 期待的是控制台，不是文档。
而它最该回答的三个问题（我该点哪 / 现在健康吗 / 它能干什么）里，
"现在健康吗"旧首页**根本回答不了** —— `/healthz` 已经能逐项报依赖，
但界面里看不到，得自己去敲命令。这是本次新增的主要价值。

【数据在浏览器端拉，服务端只给骨架】
首屏秒开靠这个：`/healthz`、`/metrics/summary`、`/incidents`、`/plays`
都由页内脚本用 `AD.get()` 拉。拉不到（例如公网实例开了鉴权）时
**如实说明原因**，不留空白 ——"取不到"和"一切正常"是两种状态，
界面上必须能区分（旧页恰恰分不清）。

【为什么页内脚本在 home.py 里而不另开一个 home.js】
本阶段的改动范围被限定在三个文件（home.py / try_.py / static/try.js）。
脚本放在 shell 的 `</body>` 之后，零构建、可离线，功能不受影响；
若后续要拆出 `static/home.js`，只要把 `_SCRIPT` 整段搬过去即可。
"""

from fastapi import Response
from fastapi.responses import HTMLResponse

from app.security import AUTH_ENABLED
from app.web.components import (
    badge,
    card,
    esc,
    kpi,
    link_button,
    note,
    skeleton,
)
from app.web.layout import VERSION, shell

# 一句话定位。首屏第一眼要读到的东西，不允许出现开发者术语。
PITCH = ("告警或人提出一个问题 → 多个专业 Agent 分工诊断 → 需要动手时提交人工审批 "
         "→ 在一次性容器里执行 → 全过程留痕、可查成本。")

# 能力矩阵。每一项都是"真实现了的"，不是路线图 —— 卡片本身就是承诺。
CAPABILITIES = [
    ("模型调用", "问答 / 意图解析 / 纯推理，可换 provider 与模型",
     "成本与 token 计数", []),
    ("私有知识检索", "自建索引 + 混合检索（向量 + 关键词）+ 引用溯源",
     "知识库里没有就直说没有", [("/view/metrics", "索引与检索")]),
    ("工具执行", "磁盘 / 负载 / 进程 / 服务 / 容器 / 日志，7 个只读工具",
     "风险等级逐个标注", [("/view/tools", "工具清单")]),
    ("沙箱与隔离", "写操作在一次性容器里执行，不可用则拒绝执行（fail-closed）",
     "不降级", [("/view/sandbox", "沙箱状态")]),
    ("人工确认", "写操作永不自动执行，先变成审批单，等人批准再执行",
     "批准与执行是两个动作", [("/view/approvals", "待审批")]),
    ("可观测与对账", "每次运行的轨迹、成本归因、缓存命中、P95 延迟、审计留痕",
     "口径可追溯", [("/dashboard", "看板"), ("/view/audit", "审计")]),
]

# 首页能力标签（hero 里的 pill）
HERO_TAGS = ["多 Agent 编排", "引用溯源", "工具执行", "沙箱隔离", "人工审批", "成本可查"]

# 深入视图：旧首页把它们列在表格里，新版收成一行链接 ——
# 页面少了不等于功能少了，`/view/{name}` 五个视图仍然可达。
DATA_VIEWS = [
    ("/view/metrics", "成本看板"), ("/view/traces", "链路追踪"),
    ("/view/tools", "工具清单"), ("/view/sandbox", "沙箱状态"),
    ("/view/approvals", "审批单"), ("/view/audit", "审计日志"),
]

# (图标, 链接, 标题, 说明, 按钮文案, 该行动卡的动态数据锚点 id)
ACTION_CARDS = [
    ("▶", "/try", "去提问",
     "填一句话，看它自己决定查什么、查几轮，带完整执行轨迹与校验结论。",
     "开始提问", ""),
    ("▤", "/dashboard", "看看板",
     "成本归因、对账偏差、逐步轨迹、待审批 —— 不用敲命令就能看。",
     "打开看板", ""),
    ("⌘", "/plays", "处置剧本",
     "有判定、有分叉、有回滚声明的结构化流程，每条命令都与策略对过账。",
     "看剧本", "playCount"),
]


def _backend_note() -> str:
    """数据来源不是脚注 —— 它是结论的一部分。

    起因：公网演示实例跑在 mock 后端（不连真实机器），页面却不标注。
    于是「web-01 磁盘还剩多少」会答「用了 96%」—— 一个**看起来像真的、
    实际是预置样例值**的答案，而真机上是 36%。这个项目的卖点就是"数字可信"，
    一个不标注数据来源的页面恰恰在削弱它。

    放在折叠区里而不是首屏横幅：首屏留给"这是什么、我该点哪"。
    """
    try:
        from app.tools import ops as _ops
    except Exception:                                 # pragma: no cover
        return note("数据来源：未知（工具层信息不可读）", "warn")
    backend = (_ops.BACKEND or "mock").lower()
    if backend == "mock":
        return note(
            "<b>本实例用的是仿真数据，不连接任何真实机器。</b>"
            "工具层跑在 <code>mock</code> 后端，查到的磁盘 / 负载 / 容器"
            "都是<b>预置样例值</b>，不是你环境里的真实情况。"
            "真机后端是 <code>ssh</code>（本项目在本地就是这么跑的）。", "warn")
    hosts = "、".join(_ops.SSH_TARGETS if backend == "ssh" else _ops.KNOWN_HOSTS)
    tail = f"主机 {esc(hosts)}。" if hosts else ""
    return note(f"数据来自<b>实时查询</b>（后端 <code>{esc(backend)}</code>，{tail}）"
                "每条结论都能追到具体命令与输出。", "ok")


def _dev_details() -> str:
    """口径与已知边界 —— 折叠，不摊在首屏。"""
    if AUTH_ENABLED:
        code = ('curl -H "X-API-Key: <你的令牌>" -H "Content-Type: application/json" \\\n'
                "     -d '{\"question\":\"web-01 磁盘快满了怎么处理\"}' \\\n"
                "     http://127.0.0.1:8000/agent/ask")
    else:
        code = ('curl -H "Content-Type: application/json" \\\n'
                "     -d '{\"question\":\"web-01 磁盘快满了怎么处理\"}' \\\n"
                "     http://127.0.0.1:8000/agent/ask")
    return f"""<details class="acc"><summary>口径与已知边界</summary>
<div class="stack">
{_backend_note()}
{note("<b>写操作永不自动执行。</b>重启服务、清空日志这类动作会变成一张审批单，"
      "等人批准；沙箱不可用时宁可拒绝执行，也不降级成直接执行。")}
{note("限流 / 每日额度 / 告警去重是<b>进程内</b>状态：单进程（--workers 1）下准确，"
      "多 worker 会各算各的。")}
<p class="hint" style="margin:0">用命令行调（与页面同一套接口）：</p>
<pre class="mono">{esc(code)}</pre>
<p class="hint" style="margin:0">逐项自检明细看 <a href="/healthz">/healthz</a>；
接口调试看 <a href="/docs">/docs</a>；指标抓取看 <a href="/metrics">/metrics</a>。
本页的"系统状态"每 60 秒自动刷新。</p>
</div></details>"""


def _script() -> str:
    """页内脚本：把 /healthz 等接口的结果填进已经存在的骨架。

    只用 `window.AD`（app.js 提供的 esc / get / money / secs / empty / timeline），
    **不引任何外部资源** —— 断网可跑是硬约束。
    """
    return """<script>
(function () {
  "use strict";
  var AD = window.AD;
  function $(id) { return document.getElementById(id); }
  function badge(text, tone) {
    return '<span class="badge ' + AD.esc(tone) + '">' + AD.esc(text) + '</span>';
  }
  /* 单项检查 → 徽标色调。informational（例如"最近没调用过模型"）
     不算失败，用 warn 让它看起来"值得看一眼但不影响判定"。 */
  function toneOf(c) {
    if (c.ok === true) { return 'ok'; }
    return (c.informational === true && !(c.failing && c.failing.length))
      ? 'warn' : 'err';
  }
  function wordOf(c) {
    if (c.ok === true) { return '通过'; }
    return c.informational === true ? '未使用' : '未通过';
  }
  function setKpi(anchor, value, note, tone) {
    var box = $(anchor);
    if (!box) { return; }
    var k = box.querySelector('.kpi');
    if (k) { k.className = 'kpi' + (tone ? ' ' + tone : ''); }
    var v = box.querySelector('.v');
    if (v) {
      var unit = v.querySelector('small');
      v.innerHTML = AD.esc(value) + (unit ? unit.outerHTML : '');
    }
    if (note) {
      var x = box.querySelector('.x');
      if (x) { x.textContent = note; }
    }
  }
  /* 整机状态 → 侧栏状态点的类名。"" = 正常，warn = 有项未使用，err = 有依赖坏了 */
  function healthTone(ok, failing, informational) {
    if (ok) { return ''; }
    return (failing && failing.length) ? 'err' : (informational ? 'warn' : 'err');
  }

  function health(d) {
    var checks = (d && d.checks) || {};
    var names = Object.keys(checks);
    var grid = $('healthGrid');
    if (!names.length) {
      grid.innerHTML = AD.empty('取不到自检明细', '/healthz 需要访问令牌，或服务未就绪');
      $('statusNote').textContent = '没有可显示的检查项。';
      return;
    }
    grid.innerHTML = names.map(function (k) {
      var c = checks[k] || {};
      return '<div class="card tight' + (c.ok === true ? '' : ' flat') + '">' +
        '<div class="card-head" style="margin-bottom:var(--s2)">' +
        '<span class="t">' + AD.esc(k) + '</span>' +
        '<span class="actions">' + badge(wordOf(c), toneOf(c)) + '</span></div>' +
        '<div class="hint" style="margin:0">' + AD.esc(c.detail || '（没有明细）') +
        (c.elapsed_ms !== undefined ? ' · ' + AD.esc(AD.secs(c.elapsed_ms)) : '') +
        '</div></div>';
    }).join('');
    var bad = (d && d.failing) || [];
    var informational = names.filter(function (k) {
      return checks[k].informational === true && checks[k].ok !== true;
    }).length;
    var tone = toneOf({ ok: d && d.ok,
                        informational: informational > 0,
                        failing: bad });
    setKpi('kpiHealth', names.length,
      informational ? informational + ' 项为"未使用"（不计入判定）' : '逐项明细见下',
      tone === 'err' ? 'err' : 'brand');
    $('statusNote').innerHTML =
      badge(d && d.ok ? '全部通过' : '有依赖未通过', tone) +
      (bad.length ? ' 未通过：' + AD.esc(bad.join('、')) : '') +
      (informational ? ' · ' + informational + ' 项为"未使用"（不计入判定）' : '') +
      ' · 检查于 ' + AD.esc(new Date().toLocaleTimeString('zh-CN'));
    var dot = document.querySelector('.side .side-foot .dot');
    if (dot) { dot.className = 'dot ' + healthTone(d && d.ok, bad, informational); }
    var txt = $('healthText');
    if (txt) {
      txt.textContent = (d && d.ok) ? '正常'
        : (bad.length ? '异常：' + bad.join('、') : '有项未使用');
    }
  }

  function healthFailed(msg) {
    $('healthGrid').innerHTML = AD.empty('取不到自检明细',
      msg + ' —— 开了鉴权时 /healthz 需要令牌');
    $('statusNote').textContent = msg;
  }

  function refresh() {
    AD.get('/healthz').then(health, function (e) {
      healthFailed(String((e && e.message) || e));
    });
    AD.get('/metrics/summary?limit=50').then(function (d) {
      var s = (d && d.scope) || {};
      var live = (s.live_runs !== undefined) ? s.live_runs : d.runs;
      var scanned = (s.records_scanned === undefined) ? '—' : s.records_scanned;
      setKpi('kpiRuns', (live === undefined ? '—' : live),
        '扫描 ' + scanned + ' 条 · 窗口成本 ' + AD.money(d.cost_cny || 0));
    }, function (e) {
      setKpi('kpiRuns', '—', '取不到 /metrics/summary：' + String((e && e.message) || e));
    });
    AD.get('/incidents?limit=50').then(function (d) {
      var c = (d && d.counts) || {};
      var open = (c.open || 0) + (c.ack || 0);
      setKpi('kpiIncidents', open,
        'open ' + (c.open || 0) + ' · ack ' + (c.ack || 0) +
        ' · 已结 ' + (c.resolved || 0),
        open ? 'err' : 'ok');
    }, function (e) {
      setKpi('kpiIncidents', '—', '取不到 /incidents：' + String((e && e.message) || e));
    });
    AD.get('/plays').then(function (d) {
      var el = $('playCount');
      if (el) {
        el.textContent = (d && d.total !== undefined)
          ? '共 ' + d.total + ' 份 · 打开看流程' : '打开看流程';
      }
    }, function () {
      var el = $('playCount');
      if (el) { el.textContent = '剧本数 —（接口不可达）· 打开看流程'; }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  function start() {
    refresh();
    var b = $('refreshStatus');
    if (b) { b.addEventListener('click', refresh); }
    setInterval(refresh, 60000);
  }
})();
</script>"""


def body() -> str:
    """页面正文（不含外壳）。单独一个函数是为了能直接断言渲染结果。"""
    hero = f"""<section class="hero">
<h1>让 Agent 自己去查，把过程留下来</h1>
<p>{esc(PITCH)}</p>
<div class="btnrow">
{link_button("▶ 去提问", "/try", tone="primary")}
{link_button("看看板", "/dashboard", tone="ghost")}
</div>
<div class="tags">{"".join(f"<span>{esc(t)}</span>" for t in HERO_TAGS)}</div>
</section>"""

    actions = "".join(
        f'<a class="card hover action" href="{esc(href)}">'
        f'<span class="ic">{icon}</span>'
        f'<span class="t">{esc(title)}</span>'
        f'<span class="d">{esc(desc)}</span>'
        + (f'<span class="hint" id="{anchor}"></span>' if anchor else
           f'<span class="hint">{esc(cta)}</span>')
        + "</a>"
        for icon, href, title, desc, cta, anchor in ACTION_CARDS)

    # ---- 系统状态：KPI 三张 + 逐项自检小卡 ----
    # ★ 骨架在**页面生成时**就写死在这里：先有容器再填数据，
    #   不是"点开之后才长出来"。
    def stat(anchor: str, kpi_html: str) -> str:
        return f'<div id="{anchor}">{kpi_html}</div>'

    health_grid = skeleton("100%", "58px") * 3
    status = card(
        '<div class="kpis" style="margin-bottom:var(--s4)">'
        + stat("kpiHealth", kpi("逐项自检", "—", unit="项", tone="brand",
                                note="正在读取 /healthz"))
        + stat("kpiRuns", kpi("窗口内真实运行", "—",
                              note="正在读取 /metrics/summary"))
        + stat("kpiIncidents", kpi("未结事件", "—", note="正在读取 /incidents"))
        + "</div>"
        + f'<div class="grid c3" id="healthGrid">{health_grid}</div>'
        + '<p class="hint" id="statusNote">正在读取 /healthz…</p>',
        title="系统状态",
        desc="来自 /healthz 的逐项依赖自检（以前要知道哪一项坏了得敲命令）",
        actions='<button type="button" class="btn ghost sm" id="refreshStatus">刷新</button>',
    )

    caps = "".join(
        card(f'<p class="hint" style="margin-top:0">{esc(desc)}</p>'
             f'<div class="btnrow">{badge(tag, "info")}'
             + "".join(link_button(label, href, tone="ghost", size="sm")
                       for href, label in links)
             + "</div>", title=name)
        for name, desc, tag, links in CAPABILITIES)

    return f"""{hero}
<div class="grid c3" style="margin-top:var(--s6)">{actions}</div>
<h2>系统状态</h2>
{status}
<h2>能力矩阵</h2>
<div class="grid c3">{caps}</div>
<p class="hint">深入视图（同一批数据，换成能看的页面）：
{" · ".join(f'<a href="{esc(href)}">{esc(label)}</a>' for href, label in DATA_VIEWS)}</p>
<h2>口径与已知边界</h2>
{_dev_details()}
<p class="hint">AgentDesk v{esc(VERSION)} · 页面本身是空壳，数据全部由接口实时提供。</p>
{_script()}"""


def page() -> str:
    """完整 HTML（含外壳）—— 测试与临时脚本直接调它。"""
    return shell(
        body(),
        title="总览",
        active="/",
        subtitle="多 Agent 运维诊断台 · 先看状态，再动手",
        actions=link_button("▶ 去提问", "/try", tone="primary", size="sm"),
        env_badge="本地 · 免鉴权" if not AUTH_ENABLED else "公网 · 需令牌",
    )


def register(app) -> None:
    """注册 `/`。与旧实现同一路径；同样 no-store（页面随配置变，不该被缓存）。"""

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(response: Response) -> str:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return page()


__all__ = ["register", "page", "body", "PITCH", "CAPABILITIES", "ACTION_CARDS"]
