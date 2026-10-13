# -*- coding: utf-8 -*-
"""可观测看板（P3）—— 排障与对账页。

【这一页只做两件事】
    ① 把 `/metrics/summary`、`/traces`、`/approvals` 三个**已有接口**渲染成人能读的界面
    ② 让每个数字都能**追到定义**（ⓘ 悬停 + 页脚折叠区），并让口径之间的矛盾**在页面上被解释掉**

【为什么值得单独一页】
数据一直是完整的（本地 JSONL 里躺着上百条 trace），但对第一次看的人一坨 JSON 等于没有 ——
尤其「成本按 Agent 分布」和「对账偏差」这两个最能说明工程质量的东西，埋在 JSON 里谁也看不见。

【这一页刻意不做什么】
· 不引图表库、不做构建、不依赖 CDN —— 一个文件，部署即用
· 不做写入 —— 只**读**三个已有接口，不新增任何业务状态
· **不缓存**（见 no-store）：看板显示的是"现在"，缓存过的看板给出的是过去的数字，
  而读者会以为那是当前的

【为什么服务端只渲染骨架，数据在浏览器里填】
指标聚合要扫一遍 traces.jsonl，慢的是接口而不是页面。骨架先出、数据后到，
页面就是秒开的；否则用户对着白屏等聚合。壳里的 `.sk` 就是为这个状态准备的。

【旧页的八个问题，逐条对应到本文件的做法】
    1 图表空白（行内 span 配 height:100% 高度不生效）→ 只用新类名 `AD.bars` / `AD.timeline`
      （它们的 CSS 是 `.bar .fill{display:block}` / `.tl .fl{display:block}`），
      本文件里**一个旧类名都不出现**，也不自己拼条形容器 —— 页面上不可能再踩第二次
    2 口径自相矛盾且无桥接 → 概览脚下一行写明
      `完整台账 N 条 = 扫描 X − 自检 Y`，并明确「窗口内运行次数 r」是 N 里的窗口子集
    3 KPI 卡宽度不齐、P50/P95 挤一张卡 → 统一的 `.kpis` + 一个数一张卡 + `kpi(unit="s")`
    4 表格数字左对齐、货币一律四位小数 → `components.table(numeric_from=1)` + `money()`
    5 100+ 字琥珀散文解释指标 → 全部下沉为 ⓘ 悬停 + 底部 `<details class="acc">`
    6 原生复选框紧挨自定义按钮 → `.switch` 包复选框 + `.btn.ghost.sm` 按钮，同一行高
    7 白底压白底 → 全部走 `.card`（白底 + 阴影 + 边框）
    8 `耗时ms` 缺空格、全角半角括号混用 → 列名一律「耗时」，数字走 `seconds()`/`AD.secs()`
"""

import json

from fastapi.responses import HTMLResponse

from app.web.components import (
    button,
    card,
    esc,
    info_tip,
    link_button,
    note,
    skeleton,
    table,
)
from app.web.layout import shell

# ============================================================
# 口径定义：**唯一来源**，页脚折叠区与每个 ⓘ 悬停都从这里取
# ------------------------------------------------------------
# 旧页用一段 100+ 字的琥珀色散文解释指标口径，占着正文最显眼的位置 ——
# 而"排障时最该先看到的是数字"。定义留在这里、在需要它的地方悬停即见。
# ============================================================
DEFS = {
    "runs": ("运行次数（窗口内）= /metrics/summary 取用的真实运行条数，"
             "等于 min(完整台账条数, 窗口 limit)。只计 source=live 的记录，"
             "自检脚本 scripts/smoke_test.py 造的记录不计入。"),
    "distinct": ("不同问题 = 窗口内去重后还剩几个不同的问题。"
                 "和运行次数一起看才有意义：运行 49 / 不同问题 8 → 在反复测同一件事；"
                 "运行 49 / 不同问题 47 → 才像真实使用。"),
    "bridge": ("台账 ↔ 窗口的桥接：records_scanned = live_available + selftest_excluded，"
               "窗口内的运行次数 = min(live_available, 窗口 limit)。"
               "所以「运行次数」和「完整台账条数」不是同一件事，也不该相同。"),
    "errors": ("错误次数 = 窗口内 status != ok 的 trace 条数。"
               "自检造的 smoke-err 已被排除 —— 它们存在的意义正是验证「错误 trace 能被记录」，"
               "计入会把「0 错误」污染成一个像 16% 错误率的系统。"),
    "cost": ("总成本 = 把窗口内每条 trace 的「叶子 span」按当前单价表重算后求和。"
             "刻意不用落盘时记的 cost_cny：单价表被核对修正过（缓存命中价改过 25 倍），"
             "直接累加会让同一份报告混着两套价格基准。"),
    "tokens": ("token = 窗口内 usage.total_tokens 之和。"
               "只取 usage.total_tokens 这一个字段 —— usage 里同时有 prompt / completion / "
               "cache_hit / cache_miss，全加一遍等于把同一笔 token 算四五遍。"),
    "cache": ("缓存命中率 = prompt_cache_hit_tokens ÷ (命中 + 未命中)。"
              "命中部分在 DeepSeek 上是大幅打折的，所以命中率低 = system prompt 每次都在变、"
              "在白花钱。样本很小的时候这个比例不稳定，别当趋势看。"),
    "p50": ("P50 延迟 = 窗口内「单次请求」耗时的中位数（取整毫秒）。"
            "batch=True 的批处理任务（评测一轮几百秒）不计入 —— "
            "混进来 P50/P95 就变成「哪个批处理任务跑了多久」。"),
    "p95": ("P95 延迟 = 单次请求耗时的 95 分位（取整毫秒）。"
            "延迟只统计单次请求，批处理不计入；样本很少时 P95 约等于最大值，"
            "所以卡片上带了样本数。"),
    "batch": ("批处理 = 窗口内 batch=True 的 trace 条数。"
              "它们的成本计入总账，但不计入延迟（见 P50/P95 口径）。"),
    "window": ("窗口 = /metrics/summary?limit=N，默认 50，最大 500。"
               "它同时决定「聚合最近多少次运行」和「最近运行列表显示多少条」，"
               "所以两处的条数是设计上一致的。"),
    "by_name": ("按 Agent（钱花在谁身上）：每笔花费记在「离叶子 span 最近的 agent 祖先」头上；"
                "没有 agent 祖先的（例如直接调 llm 的 RAG 评测）归入 (无节点)，"
                "这些钱不丢弃 —— 丢掉的钱会让合计小于总账，对账信号就被噪声填满了。"),
    "by_type": ("按动作（钱花在什么事上）：每笔花费按叶子 span 的 type 切分（llm / tool / agent）。"
                "与「按 Agent」是同一笔钱的两种切法，各自独立等于总账。"),
    "reconcile": ("对账 = 两个归因维度的合计各自与总账比。偏离超过 1% 就点警告。"
                  "常见的已知来源是「无 span 的 trace」（流式接口），它们的钱只进总账、"
                  "进不了两个维度。"),
    "approvals": ("待确认的写操作：本页只读展示，不提供批准/驳回按钮。"
                  "处置动作的确认入口在 API（/approvals/{id}/approve）与运维流程里，"
                  "看板不越权代替人做决定。"),
}

# 概览 KPI 的骨架（服务端先渲染加载态，浏览器拿到数再填）
_SK_ROWS = [
    ["sk-label", "sk-strong"],
    ["sk-label", "sk-strong"],
    ["sk-label", "sk-strong"],
    ["sk-label", "sk-strong"],
]


def metric_defs_html() -> str:
    """页脚折叠区：所有口径定义集中一处（正文里只留 ⓘ）。"""
    items = "".join(
        f'<dt>{esc(key)}</dt><dd>{esc(text)}</dd>' for key, text in DEFS.items()
    )
    return (
        '<details class="acc"><summary>指标口径说明（每个数字的定义都在这里）</summary>'
        f'<dl class="kv">{items}</dl>'
        "<p class=\"hint\">页面上的 ⓘ 悬停显示的就是同一段文字 —— "
        "口径只维护一处，不会出现「提示和文档说的不一样」。</p>"
        "</details>"
    )


def register(app) -> None:
    """把自己挂到 app 上（由 app/web/pages/__init__.py 的 IMPLEMENTED 调用）。"""

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_page():
        """看板。**只渲染骨架**，数据在浏览器端从三个已有接口拉。"""
        from app import security

        # ★ "要不要令牌"由服务端决定，前端只读取 —— 与 /try 同一条规矩。
        #   探测方案有三个失败面，这里不重复踩。
        needs_token = "true" if security.AUTH_ENABLED else "false"

        # ---------------- 头带操作区：刷新 / 窗口 / 自动刷新 ----------------
        window_pick = (
            '<label class="switch" title="聚合最近多少次运行">窗口 '
            '<select id="limitSel" aria-label="聚合窗口">'
            + "".join(f'<option value="{n}"{" selected" if n == 50 else ""}>'
                      f"最近 {n} 次</option>" for n in (20, 50, 100, 200))
            + "</select></label>")
        actions = (
            window_pick
            + button("刷新", tone="ghost", size="sm", attrs='id="refreshBtn"')
            + '<label class="switch"><input type="checkbox" id="auto"> 每 30 秒自动刷新'
              "</label>"
            + '<span class="hint" id="stamp" style="margin:0">载入中…</span>'
            + link_button("接口文档", "/docs", size="sm")
        )

        # ---------------- 概览 KPI ----------------
        summary_body = (
            '<div class="kpis" id="summaryCards">'
            + skeleton("100%", "74px") * 9 + "</div>"
            '<div id="summaryEmpty"></div>'
            '<div id="bridge" class="hint" style="margin-top:12px">指标口径载入中…</div>'
        )

        # ---------------- 成本归因（两个维度不可相加） ----------------
        breakdown_body = (
            note('<b>两个维度是同一笔钱的两种切法</b>（按 Agent／按动作），各自独立等于总账 '
                 '—— <b>不能相加</b>（相加等于翻倍）。')
            + '<div class="grid c2" id="breakdownTables" style="margin-top:16px">'
            + card(
                table(["Agent", "次数", "token", "成本", "耗时"],
                      _SK_ROWS, numeric_from=1),
                title="按 Agent（钱花在谁身上）", tight=True,
                actions=f'<i class="info" data-tip="{esc(DEFS["by_name"])}">i</i>')
            + card(
                table(["动作类型", "次数", "token", "成本", "耗时"],
                      _SK_ROWS, numeric_from=1),
                title="按动作（钱花在什么事上）", tight=True,
                actions=f'<i class="info" data-tip="{esc(DEFS["by_type"])}">i</i>')
            + "</div>"
            '<div class="grid c2" style="margin-top:16px">'
            + card('<div id="chartAgent">' + skeleton("100%", "120px") + "</div>",
                   title="成本分布 · 按 Agent", tight=True,
                   actions=f'<i class="info" data-tip="{esc(DEFS["cost"])}">i</i>')
            + card('<div id="chartType">' + skeleton("100%", "120px") + "</div>",
                   title="成本分布 · 按动作", tight=True,
                   actions=f'<i class="info" data-tip="{esc(DEFS["cost"])}">i</i>')
            + "</div>"
        )

        # ---------------- 对账 ----------------
        reconcile_body = (
            '<div id="reconcileBadges">' + skeleton("260px", "22px") + "</div>"
            '<div id="reconcileTable" style="margin-top:12px">'
            + table(["口径", "金额", "与总账偏差", "判定"], _SK_ROWS, numeric_from=1)
            + "</div>"
            '<div id="reconcileNote"></div>'
        )

        # ---------------- 最近运行 + 逐步轨迹 ----------------
        traces_body = (
            '<p class="hint">每一条都是真实运行（source=live，自检记录不在其中）—— '
            "点任意一条展开它的逐步轨迹，条宽是各步耗时占比。"
            "这里最多显示 50 条（与上方窗口取交集）。</p>"
            '<!-- traces -->'
        )

        # ---------------- 待审批 ----------------
        approvals_body = (
            '<div id="approvalsBox">' + skeleton("100%", "90px") + "</div>"
            '<p class="hint">只是展示，批准/驳回要走 API 或运维流程 —— '
            f'{esc(DEFS["approvals"])}</p>'
        )

        # 口径定义交给前端当 ⓘ 的 title 用（唯一来源就是上面的 DEFS）。
        # ★ 只把 "<" 转成 \u003c：这样 </script> 打不断这个块，
        #   而 JSON.parse 仍能还原成原文（用 esc() 会把引号变成 &quot; 反而破坏 JSON）。
        defs_json = json.dumps(DEFS, ensure_ascii=False).replace("<", "\\u003c")

        body = f"""<div class="stack">
  <script type="application/json" id="pageDefs">{defs_json}</script>
  <div id="pageState" data-needs-token="{needs_token}" hidden></div>

  <div id="tokbar" class="btnrow" hidden>
    <div class="btnrow">
      <input type="text" id="token" placeholder="访问令牌（本实例开启了鉴权）"
             aria-label="访问令牌" style="max-width:280px">
      {button("保存令牌", size="sm", attrs='id="tokSave"')}
      <span class="hint" style="margin:0">令牌只存在浏览器 localStorage，
        服务端不回显、不落盘。</span>
    </div>
  </div>

  <div id="err"></div>

  <section class="section">
    <h2><span>概览</span>{info_tip(DEFS["runs"] + " " + DEFS["bridge"])}</h2>
    {card(summary_body)}
  </section>

  <section class="section">
    <h2><span>成本归因</span>{info_tip(DEFS["by_name"] + " " + DEFS["by_type"])}
      <span class="hint" style="margin:0 0 0 8px">两个维度不可相加</span></h2>
    {breakdown_body}
  </section>

  <section class="section">
    <h2><span>对账</span>{info_tip(DEFS["reconcile"])}</h2>
    {card(reconcile_body)}
  </section>

  <section class="section">
    <h2><span>最近运行与逐步轨迹</span></h2>
    {traces_body}
  </section>

  <section class="section">
    <h2><span>待人工确认的写操作</span>{info_tip(DEFS["approvals"])}</h2>
    {approvals_body}
  </section>

  <section class="section">
    {metric_defs_html()}
  </section>
</div>"""

        body = body.replace(
            "<!-- traces -->",
            card('<div id="traces" class="stack">' + skeleton("100%", "150px") + "</div>",
                 title="最近运行", tight=True),
        )

        html = shell(
            body,
            title="可观测看板",
            active="/dashboard",
            subtitle="成本归因 · 对账 · 逐步轨迹 · 待审批 —— 数据来自 /metrics/summary、"
                     "/traces、/approvals",
            actions=actions,
            head_extra='<script src="/static/dashboard.js" defer></script>',
        )

        # 看板显示的是"现在"，缓存过的看板给出的是过去的数字
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
