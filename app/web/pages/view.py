# -*- coding: utf-8 -*-
"""数据视图与流程可视化（P4）—— `/view/{name}` 的统一重做。

【旧页原本靠什么渲染】
`main.py` 里的 `/view/{name}` 是**一张通用 JSON 皮**：它把 `/metrics/summary`、
`/traces`、`/agent/tools`、`/sandbox`、`/approvals`、`/audit`、`/health`
七个接口的返回递归地摊成"字段表 + 数组表"，配一个令牌框与导出按钮。
它**没有**用 mermaid、没有引任何 CDN —— 也就是说，旧页的可视化能力就是
「结构化表格 + 原始 JSON」，本文件照这个能力做，**没有凭空发明新东西**。

★ 但编排（`/agent/graph` 与 supervisor 的 mermaid）这一层旧页**完全没展示**：
  它是一段贴到别处才能看的 mermaid 文本。而"多 Agent 怎么编排"恰恰是这个项目
  最该被看见的东西。所以本文件补的是**同源数据的另一种皮**，不是新能力：

      · `/agent/graph?engine=react`      → 内联 SVG 结构图（想 → 做 → 收口，含回边）
      · `/agent/graph?engine=supervisor` → 内联 SVG 结构图（调度决策扇出 + 回边）
      · 两张图都用 `layout.NODE_LABEL` 的中文节点名
      · 图是**服务端渲染的静态 SVG**（节点与边是确定的），mermaid 原文仍然拉下来
        放在折叠区里 —— 想贴到 GitHub / 飞书的人拿得到原文

【为什么是内联 SVG 而不是 mermaid.js】
零依赖是硬约束，而且离线/内网环境加载不到 CDN 会**白屏**（比"图不好看"严重得多）。
手绘一份 SVG 的成本是几十行布局代码，换来的是一张确定能显示、能打印、
能随主题走（`fill:currentColor` + CSS 变量）的图。代价是布局不像 mermaid 那样自动
—— 所以节点坐标由 `_layout()` 从**边表**推导，加节点不需要手改坐标。

【保留的旧页能力（一条都没丢）】
    · 七个视图名与各自的数据源接口不变
    · 未知 name → 404（与旧行为一致，在 `register()` 里）
    · 令牌框（开了鉴权才显示）+ localStorage 记忆
    · 刷新 / 导出 JSON / 导出 CSV（数组视图）
    · 无数据 / 出错 / 加载中都有明确形态，不白屏
"""

import json
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from app.web.components import (
    button,
    card,
    esc,
    link_button,
    note,
    skeleton,
    table,
)
from app.web.layout import NODE_LABEL, shell

# ============================================================
# 一、七个数据视图（与旧页逐条对齐：名字、接口、说明）
# ============================================================
VIEWS: dict = {
    "metrics": ("/metrics/summary?limit=20", "成本看板",
                "按 Agent / 动作两维的成本、缓存命中率、P95 延迟"),
    "traces": ("/traces?limit=20", "链路追踪",
               "每次运行发生了什么、哪一步最慢最贵"),
    "tools": ("/agent/tools", "工具清单",
              "Agent 能调用的工具与风险等级"),
    "sandbox": ("/sandbox", "沙箱状态",
                "当前后端（mock / 真容器）、隔离状态与命令白名单"),
    "approvals": ("/approvals", "审批单",
                  "Agent 想执行但还没执行的写操作，等人工确认"),
    "audit": ("/audit?limit=30", "审计日志",
              "谁、何时、哪条告警、判成什么风险、做了什么"),
    "health": ("/health", "健康检查",
               "服务存活与安全层状态（不需要令牌）"),
}

# 三个"能力/流程"视图：不走 JSON 皮，走内联结构图 + 数据表
FLOWS: dict = {
    "supervisor": {
        "engine": "supervisor",
        "title": "多 Agent 编排",
        "desc": "一个调度节点决定「下一步派给谁」，每个专业 Agent 干完回到调度节点 —— "
                "这就是「循环」在真实系统里的样子",
        "head": "调度决策（supervisor）",
        "loop": "专业 Agent 干完 → 回到调度决策",
    },
    "react": {
        "engine": "react",
        "title": "单 Agent 循环",
        "desc": "想（模型决定查什么）→ 做（执行工具）→ 看（观察结果）→ 再想 —— "
                "直到模型自己决定回答，或撞上步数 / 预算上限收口",
        "head": "模型决策（agent）",
        "loop": "工具执行完 → 回到模型决策",
    },
}
ALL_VIEWS = {**{k: (v[1], v[2]) for k, v in VIEWS.items()},
             **{k: (v["title"], v["desc"]) for k, v in FLOWS.items()}}

# ============================================================
# 二、结构图：节点与边（与 `app/agents/*.py` 的 build_graph 逐条对应）
# ------------------------------------------------------------
# 【为什么把边表写在这里而不是运行时调 mermaid 解析】
#   ① 这两张图的结构是**确定的**，边界就是代码里的 add_edge —— 谁改了图，
#      这里的两行也要改，而"两处不一致"会被 `tests` 之外的东西抓住：
#      每个节点都必须能在 `NODE_LABEL` 里找到中文名（见 `_assert_labels`）。
#   ② 调 `build_graph()` 会 import langgraph 并编译图（几十毫秒 + 副作用），
#      而页面只需要"有哪些节点、谁连谁"。**渲染页面不该有这种耦合**。
# ============================================================
KIND_LABEL = {"term": "起止", "decision": "调度/决策", "agent": "专业 Agent",
              "tool": "工具执行", "out": "收口输出"}

_GRAPH_SPEC: dict = {
    "supervisor": {
        "nodes": [
            ("START", "开始", "", "term"),
            ("supervisor", "调度决策", "看流程状态，决定下一步派给谁", "decision"),
            ("intent", "意图路由", "这是诊断还是咨询、涉及哪台机器", "agent"),
            ("knowledge", "知识检索", "查私有语料，带引用", "agent"),
            ("diagnose", "工具执行", "调工具查现场（只读优先）", "agent"),
            ("reason", "纯推理分析", "没有工具可用时的分析路径", "agent"),
            ("verify", "结果校验", "结论站不住就要求重查", "agent"),
            ("remediate", "处置建议", "写操作转人工审批，不直接动手", "agent"),
            ("finalize", "汇总输出", "把结论、证据、成本一次交代清", "out"),
            ("END", "结束", "", "term"),
        ],
        # (起点, 终点, 是否回边, 边上的说明)
        "edges": [
            ("START", "supervisor", False, ""),
            ("supervisor", "intent", False, ""),
            ("supervisor", "knowledge", False, ""),
            ("supervisor", "diagnose", False, ""),
            ("supervisor", "reason", False, ""),
            ("supervisor", "verify", False, ""),
            ("supervisor", "remediate", False, ""),
            ("supervisor", "finalize", False, ""),
            ("intent", "supervisor", True, ""),
            ("knowledge", "supervisor", True, ""),
            ("diagnose", "supervisor", True, ""),
            ("reason", "supervisor", True, ""),
            ("verify", "supervisor", True, ""),
            ("remediate", "supervisor", True, ""),
            ("finalize", "END", False, ""),
        ],
    },
    "react": {
        "nodes": [
            ("START", "开始", "", "term"),
            ("agent", "模型决策", "想：要不要查、查什么", "decision"),
            ("tools", "工具执行", "做 + 看：执行并读回结果", "tool"),
            ("finalize", "收口输出", "回答 / 撞步数上限 / 预算到点", "out"),
            ("END", "结束", "", "term"),
        ],
        "edges": [
            ("START", "agent", False, ""),
            ("agent", "tools", False, "要继续查"),
            ("tools", "agent", True, "观察结果"),
            ("agent", "finalize", False, "要收口 / 撞上限"),
            ("finalize", "END", False, ""),
        ],
    },
}


def _assert_labels(spec: dict) -> None:
    """每个节点都要有**中文名** —— 缺一个就抛，而不是让页面上出现英文裸名。

    中文名的来源有两处，按顺序取：
        ① `layout.NODE_LABEL`（时间线、轨迹、结构图共用一份译名）
        ② 本文件 `_GRAPH_SPEC` 里的节点标签（START / END / agent / tools /
           finalize 这几个在 `NODE_LABEL` 里没有 —— 它们只在结构图里出现）
    只要两者都拿不到，就是有人加了一个节点却忘了写中文名。
    """
    for name, label, _sub, _kind in spec["nodes"]:
        if name not in NODE_LABEL and not label:
            raise KeyError(f"结构图节点 {name!r} 没有中文名"
                           f"（既不在 NODE_LABEL 里，也没有自带标签）")


# ---------- SVG 基础件 ----------
_W, _H = 112, 48            # 节点宽高
_GAP = 156                  # 两个分支节点中心距
_MAR = 26                   # 上下留白
_FS = 13                    # 节点主文案字号
_FS2 = 11                   # 节点副文案字号


def _defs() -> str:
    return (
        '<defs>'
        '<marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="var(--brand-1)"/></marker>'
        '<marker id="aht" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="var(--text-3)"/></marker>'
        "</defs>"
    )


def _node(x: float, y: float, label: str, sub: str = "", width: float = _W) -> str:
    """一个圆角矩形节点（居中锚点）。宽度可覆盖（START / END 用小胶囊）。"""
    top = y - _H / 2
    ty = y + (0 if not sub else -6)
    out = (f'<rect x="{x - width / 2:.1f}" y="{top:.1f}" width="{width:.1f}" '
           f'height="{_H}" rx="10" fill="var(--surface)" stroke="var(--brand-1)" '
           f'stroke-width="1.5"/>'
           f'<text x="{x:.1f}" y="{ty:.1f}" text-anchor="middle" '
           f'dominant-baseline="middle" font-size="{_FS}" font-weight="600" '
           f'fill="var(--text)">{esc(label)}</text>')
    if sub:
        out += (f'<text x="{x:.1f}" y="{y + 13:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="{_FS2}" '
                f'fill="var(--text-3)">{esc(sub)}</text>')
    return out


def _line(x1: float, y1: float, x2: float, y2: float, *, back: bool = False,
          label: str = "", anchor: str = "middle") -> str:
    dash = ' stroke-dasharray="5 4"' if back else ""
    color = "var(--text-3)" if back else "var(--brand-1)"
    mark = "aht" if back else "ah"
    out = (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
           f'stroke="{color}" stroke-width="1.6"{dash} marker-end="url(#{mark})"/>')
    if label:
        out += (f'<text x="{x1:.1f}" y="{(y1 + y2) / 2:.1f}" text-anchor="{anchor}" '
                f'dominant-baseline="middle" font-size="{_FS2}" '
                f'fill="var(--text-3)">{esc(label)}</text>')
    return out


def _rows(edges: list, vertices: list, hub: str) -> dict:
    """把节点分到四行（中间多一行 2.5）—— 行由**到中心的距离**决定，不靠手写坐标。

        row0    中心的上游（START）
        row1    中心 + 它的直接分支（supervisor 的七个 Agent / agent 的想与做）
        row2    分支的收口节点（supervisor 的 finalize）
        row2.5  分支之后的收口（react 的 finalize —— 它挂在"做"后面而不是中心后面）
        row3    收口之后的终点（END）

    这样加一个节点不用改坐标：只要它挂在边表里，就会被摆到正确的下一行。
    """
    def children_of(n: str) -> list:
        return [b for a, b, back, _l in edges
                if a == n and not back and b in vertices]

    row0 = [a for a, b, back, _l in edges if b == hub and not back]
    row1 = [hub] + children_of(hub)
    seen = set(row0) | set(row1)
    row2, row_mid, row3 = [], [], []
    for c in children_of(hub):          # 分支的收口（挂在中心后面）
        if c in seen:
            continue
        seen.add(c)
        row2.append(c)
    for c in row2:                      # 收口之后的终点（挂了 END 的那种）
        for d in children_of(c):
            if d not in seen:
                seen.add(d)
                row3.append(d)
    for c in children_of(hub):          # 分支后面的收口（react 的 finalize）
        for d in children_of(c):
            if d not in seen:
                seen.add(d)
                row_mid.append(d)
    for c in row_mid:                   # 再往下一层，兜底
        for d in children_of(c):
            if d not in seen:
                seen.add(d)
                row3.append(d)
    return {0: row0, 1: row1, 2: row2, 2.5: row_mid, 3: row3}


def _svg(spec: dict) -> str:
    """把边表布局成一张内联 SVG。

    布局规则（不需要手写坐标）：
        · 行号由 `_rows()` 从边表推出来（上游 / 中心与分支 / 收口 / 终点）
        · **同一列的下游节点跟上游对齐**（一条竖线），不同列冲突时才另起一列；
          放不下的行整体居中 —— 于是"想 → 做 → 收口 → 结束"是一条竖线，
          而 supervisor 的七个分支是一排
        · 回边（子 → 父）走**右侧竖干**：短横线接竖干，竖干升回父节点右缘
          —— 走左侧会和扇出线束交叉，走中间会穿过节点
    """
    names = [n[0] for n in spec["nodes"]]
    info = {n[0]: {"label": n[1], "sub": n[2], "kind": n[3]} for n in spec["nodes"]}
    edges = list(spec["edges"])

    outdeg = {n: 0 for n in names}
    for a, _b, back, _lab in edges:
        if not back and a in outdeg:
            outdeg[a] += 1
    hub = max(outdeg, key=lambda k: outdeg[k])
    rows = _rows(edges, names, hub)
    n_col = max(len(r) for r in rows.values()) or 1
    width = n_col * _GAP
    row_y = {0: _MAR + _H / 2, 1: _MAR + _H / 2 + 122,
             2: _MAR + _H / 2 + 122 + 96, 2.5: _MAR + _H / 2 + 122 + 96 + 88,
             3: _MAR + _H / 2 + 122 + 96 + 88 + 84}

    def spread(items: list) -> list:
        return [width / 2 + (i - (len(items) - 1) / 2) * _GAP
                for i in range(len(items))]

    # 先排中心那一行，再让其余各行**尽量跟自己的上游同列**（竖直往下走），
    # 只有同一行里撞了才另起一列 —— 于是"想 → 做 → 收口 → 结束"是一条竖线，
    # 而 supervisor 的七个分支排成一排。
    xs: dict = dict(zip(rows[1], spread(rows[1]), strict=True))
    for r in (0, 2, 2.5, 3):
        free = spread(rows[r])
        for n in rows[r]:
            owners = [xs[a] for a, b, _bk, _l in edges if b == n and a in xs]
            want = owners[0] if owners else None
            same_row = [xs[m] for m in rows[r] if m != n and m in xs]
            if want is None or any(abs(want - t) < _W + 6 for t in same_row):
                want = next((x for x in free
                             if all(abs(x - t) >= _W + 6 for t in same_row)), width / 2)
            xs[n] = want
    row_of = {n: r for r, items in rows.items() for n in items}

    def where(n: str) -> float:
        return row_y[row_of[n]]

    branches = [n for n in rows[1] if n != hub]
    hub_bottom = where(hub) + _H / 2
    # 扇出横杆要落在**分支行方框的上沿之上** —— 否则横杆会从方框里穿过去，
    # 而且要给边上的说明文字留出位置（标签画在横杆上方 4px）。
    rail_y = min(hub_bottom + 42, where(rows[1][0]) - _H / 2 - 32)

    body = [_defs()]
    # ---- 中心竖干 + 分支竖干（先线后节点，线不会压住节点）----
    if branches:
        body.append(f'<line x1="{xs[hub]:.1f}" y1="{hub_bottom:.1f}" '
                    f'x2="{xs[hub]:.1f}" y2="{rail_y:.1f}" '
                    f'stroke="var(--brand-1)" stroke-width="1.6"/>')
        for c in branches:
            lab = next((lb for a, b, _bk, lb in edges
                        if a == hub and b == c and lb), "")
            body.append(_line(xs[c], rail_y, xs[c], where(c) - _H / 2 - 2))
            if lab:
                body.append(f'<text x="{xs[c]:.1f}" y="{rail_y - 4:.1f}" '
                            f'text-anchor="middle" font-size="{_FS2}" '
                            f'fill="var(--text-3)">{esc(lab)}</text>')
    # ---- 回边：短横线 → 右侧竖干 → 回到父节点右缘 ----
    trunk_x = width + 34
    for a, b, back, _lab in edges:
        if not back or a not in xs or b not in xs:
            continue
        body.append(
            f'<path d="M {xs[a] + _W / 2:.1f} {where(a):.1f} H {trunk_x:.1f} '
            f'V {where(b):.1f} H {xs[b] + _W / 2 + 12:.1f}" fill="none" '
            f'stroke="var(--text-3)" stroke-width="1.6" stroke-dasharray="5 4" '
            f'marker-end="url(#aht)"/>')
    # ---- 其余普通边（START→中心、收口→END …；扇出边已经画过）----
    for a, b, back, lab in edges:
        if back or (a == hub and b in branches):
            continue
        if a not in xs or b not in xs:
            continue
        body.append(_line(xs[a], where(a) + _H / 2, xs[b], where(b) - _H / 2,
                          label=lab))
    # ---- 节点（最后画，压住线头）----
    for name in names:
        w = 74 if info[name]["kind"] == "term" else _W
        body.append(_node(xs[name], where(name), info[name]["label"],
                          info[name]["sub"], w))

    bottom = max(row_y.values()) + _H / 2 + _MAR
    right = max(xs.values()) + _W / 2 + 18
    view_w = max(right, trunk_x + 12)
    # ★ 明确给 width/height 属性，**不用** style="width:100%"：
    #   后者会把一张窄图（react 只有 514 单位宽）按比例放大到容器宽度 ——
    #   节点被放大成巨框，纵向撑破卡片。
    #   这里给显式尺寸、并靠外层 `.tablewrap`（`overflow-x:auto`）横向滚动：
    #   文字保持 13px 原大小，比"缩到看得清但小"更适合读流程图。
    return (f'<svg class="flow" viewBox="0 0 {view_w:.0f} {bottom:.0f}" '
            f'width="{view_w:.0f}" height="{bottom:.0f}" role="img" '
            f'aria-label="{esc(_aria(spec))}">' + "".join(body) + "</svg>")


def _aria(spec: dict) -> str:
    names = "、".join(NODE_LABEL.get(n[0], n[1]) for n in spec["nodes"])
    return "流程结构图：" + names


def _legend() -> str:
    return ('<div class="legend">'
            '<span><b style="color:var(--brand-1)">→</b> 执行方向</span>'
            '<span><b style="color:var(--text-3)">⇢</b> 回到上游（循环）</span>'
            '<span>节点名用 `NODE_LABEL` 的中文名，与时间线、轨迹里的一致</span>'
            "</div>")


def _node_table(spec: dict) -> str:
    """节点明细表。

    ★ 只放**图上看不清**的信息（中文名 / 类型 / 职责）—— "下一步是谁""谁回到谁"
      已经在图上画出来了，再列两列只会把四列挤成十列、每格一两个字符。
      `numeric_from=9` 让所有列左对齐：这里一列数字都没有。
    """
    rows = []
    for name, label, sub, kind in spec["nodes"]:
        rows.append([f"<code>{esc(name)}</code>", esc(label),
                     esc(KIND_LABEL.get(kind, kind)), esc(sub or "—")])
    return table(["节点", "中文名", "类型", "职责"], rows, numeric_from=9)


def _flow_card(name: str) -> str:
    spec = FLOWS[name]
    _assert_labels(_GRAPH_SPEC[spec["engine"]])
    graph = _GRAPH_SPEC[spec["engine"]]
    body = (
        _legend()
        + '<div class="tablewrap">'
        + _svg({**graph, "nodes": graph["nodes"]})
        + "</div>"
        + '<p class="hint">这张图是<b>服务端渲染的静态 SVG</b>：没有 CDN、没有前端图库，'
          "离线也能显示，打印与导出都正常。</p>"
        + '<div class="section">' + _node_table(graph) + "</div>"
        + '<details class="acc"><summary>mermaid 原文（想贴到 GitHub / 飞书就用这段）'
          "</summary>"
          f'<pre id="mermaidText" class="mono">'
          f"{esc('载入中… /agent/graph?engine=' + spec['engine'])}</pre></details>"
    )
    return card(body, title=f'{spec["title"]} · 结构图',
                desc="与 app/agents/*.py 的 build_graph 逐条对应")


# ============================================================
# 三、页面骨架
# ============================================================
def _view_picker(active: str) -> str:
    """视图切换：**顶部一行按钮**，不占侧栏。

    【为什么不沿用旧页那种"侧栏 + 两栏布局"】
    结构图是一张宽图（supervisor 有 8 列）。放进 `.split` 的右栏只有 ~700px，
    图会被压得看不清；而 `.grid.c2` 在窄栏里会把卡片压到 300px 以下 ——
    文字开始逐字换行。所以这一页用**满宽纵向堆叠**：切换在上、图在中、数据在下，
    这也是"图要看得清"优先于"栏多显得丰满"的一处取舍。
    """
    rows = []
    for key, (title, _desc) in ALL_VIEWS.items():
        cls = "btn primary sm" if key == active else "btn ghost sm"
        cur = ' aria-current="page"' if key == active else ""
        rows.append(f'<a class="{cls}" href="/view/{esc(key)}"{cur}>{esc(title)}</a>')
    return card('<div class="btnrow">' + "".join(rows) + "</div>",
                title="切换视图",
                desc="Agent 编排（结构图）· 数据视图（同一份 JSON 接口的可视化皮）")


def _data_card(name: str) -> str:
    api_path, title, desc = VIEWS[name]
    body = (
        '<div class="btnrow">'
        + button("刷新", tone="ghost", size="sm", attrs='id="refreshBtn"')
        + button("导出 JSON", tone="ghost", size="sm", attrs='id="jsonBtn"')
        + button("导出 CSV", tone="ghost", size="sm", attrs='id="csvBtn"')
        + '<span class="hint" id="stamp"></span>'
        + "</div>"
        + '<div id="err"></div>'
        + '<div id="content" class="stack">' + skeleton("100%", "160px") + "</div>"
        + '<details class="acc" style="margin-top:16px">'
          "<summary>原始 JSON（页面上看到的每一个值都来自这里）</summary>"
          '<pre id="rawJson" class="mono">载入中…</pre></details>'
        + '</div>'
    )
    return card(body, title=title, desc=f"{desc} · 数据源 {api_path}")


_JS = r"""
/* ============================================================
   视图页：拉数据 → 按形状渲染。
   形状 → 画法（旧页的能力，一条不少）：
     数组（同构对象）  → 表格（列 = 并集，嵌套值截断显示）
     对象（含嵌套）    → 标量走 .kv，嵌套对象/数组各自继续
     字符串数组        → chips
     其它              → 原样 JSON（折叠区里永远有完整原文）
   ★ 所有外部文本过 AD.esc —— 数组里的值可能来自用户提问与命令输出。
   ============================================================ */
document.addEventListener("DOMContentLoaded", function () {
  "use strict";
  var D = window.__VIEW__ || {};
  var KEY = "agentdesk_token";
  var RAW = null;
  var $ = function (id) { return document.getElementById(id); };

  function tok() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }
  /* 从 URL hash 收一次令牌（被鉴权挡住的实例上方便一次性进入；存本机浏览器） */
  (function () {
    var m = /(?:^|[#&])token=([^&]+)/.exec(location.hash || "");
    if (m) {
      try { localStorage.setItem(KEY, decodeURIComponent(m[1])); } catch (e) {}
      history.replaceState(null, "", location.pathname + location.search);
    }
  })();
  function headers() {
    var h = { Accept: "application/json" };
    var t = tok();
    if (t) h["X-API-Key"] = t;
    return h;
  }
  function setErr(html) { $("err").innerHTML = html; }
  function warn(text) {
    return '<div class="note warn"><span class="ic">\u26a0</span><span>' +
      AD.esc(text) + "</span></div>";
  }
  function info(text) {
    return '<div class="note"><span class="ic">\u25b8</span><span>' + text + "</span></div>";
  }
  function codeOf(v) { return "<code>" + AD.esc(v) + "</code>"; }
  function badgeOf(text, tone) {
    return '<span class="badge ' + tone + '">' + AD.esc(text) + "</span>";
  }
  /* JS 侧的卡片。`o` 是可选对象：{title, desc, tight}。
     ★ 服务端的 `components.card()` 是 Python 的**关键字参数**，
       而这是 JS —— 只有对象字面量一种合法写法。 */
  function card(body, o) {
    o = o || {};
    var cls = "card" + (o.tight ? " tight" : "");
    var head = "";
    if (o.title || o.desc) {
      head = '<div class="card-head"><span class="t">' + AD.esc(o.title || "") +
        "</span>" + (o.desc ? '<span class="d">' + AD.esc(o.desc) + "</span>" : "") +
        "</div>";
    }
    return '<div class="' + cls + '">' + head + body + "</div>";
  }
  function kpiCard(label, value, note) {
    return '<div class="kpi"><div class="k">' + AD.esc(label) + "</div>" +
      '<div class="v">' + AD.esc(value === undefined ? "\u2014" : value) + "</div>" +
      (note ? '<div class="x">' + AD.esc(note) + "</div>" : "") + "</div>";
  }
  function pick(o, keys, dflt) {
    for (var i = 0; i < keys.length; i++) {
      var v = o[keys[i]];
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return dflt === undefined ? "\u2014" : dflt;
  }
  function cell(v) {
    if (v === null || v === undefined || v === "") return "";
    if (typeof v === "object") {
      var s = JSON.stringify(v);
      return codeOf(s.length > 120 ? s.slice(0, 120) + "\u2026" : s);
    }
    if (typeof v === "boolean") return badgeOf(v ? "是" : "否", v ? "ok" : "neutral");
    return AD.esc(String(v));
  }

  function isArrOfObj(a) {
    return Array.isArray(a) && a.length > 0 && a.every(function (x) {
      return x && typeof x === "object" && !Array.isArray(x);
    });
  }
  function tableOf(arr, cols) {
    if (!arr.length) return AD.empty("这份数据是空数组", "接口通了，但没有内容");
    var keys = cols ? cols.map(function (c) { return c[0]; }) : [];
    if (!keys.length) {
      arr.slice(0, 80).forEach(function (o) {
        Object.keys(o).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
      });
    }
    var head = "<tr>" + keys.map(function (k) {
      var c = (cols || []).filter(function (x) { return x[0] === k; })[0];
      return "<th>" + AD.esc(c ? c[1] : k) + "</th>";
    }).join("") + "</tr>";
    var body = arr.slice(0, 80).map(function (o) {
      return "<tr>" + keys.map(function (k) {
        var c = (cols || []).filter(function (x) { return x[0] === k; })[0];
        var v = pick(o, [k], "");
        return "<td>" + (c && c[2] ? c[2](v) : cell(v)) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return '<div class="tablewrap"><table><thead>' + head +
      "</thead><tbody>" + body + "</tbody></table></div>" +
      (arr.length > 80 ? '<p class="hint">只显示前 80 行（共 ' + arr.length +
        " 行）—— 导出 CSV 拿全量。</p>" : "");
  }
  function kvOf(o) {
    var pairs = [];
    Object.keys(o).forEach(function (k) {
      var v = o[k];
      if (v === null || typeof v !== "object") {
        var show = (typeof v === "boolean") ? badgeOf(v ? "是" : "否", v ? "ok" : "neutral")
          : codeOf(pick({ v: v }, ["v"], "\u2014"));
        pairs.push(["<dt>" + AD.esc(k) + "</dt>", "<dd>" + show + "</dd>"]);
      }
    });
    if (!pairs.length) return "";
    return '<dl class="kv">' + pairs.map(function (p) {
      return p[0] + p[1];
    }).join("") + "</dl>";
  }
  function chipsOf(a) {
    if (!a.length) return AD.empty("空列表");
    return '<div class="chips">' + a.map(function (x) {
      return '<span class="chip">' + AD.esc(x) + "</span>";
    }).join("") + "</div>";
  }

  /* ---------- 通用树：按形状选画法 ---------- */
  function tree(v, depth) {
    depth = depth || 0;
    if (v === null || v === undefined) return AD.empty("（空）");
    if (isArrOfObj(v)) return card(tableOf(v), { title: v.length + " 行", tight: true });
    if (Array.isArray(v)) {
      if (v.every(function (x) { return typeof x !== "object"; })) return chipsOf(v);
      return card('<pre class="mono">' + AD.esc(JSON.stringify(v, null, 2).slice(0, 2500)) +
        "</pre>", { tight: true });
    }
    if (typeof v === "object") {
      var out = "";
      var scalars = kvOf(v);
      if (scalars) out += card(scalars, { tight: true });
      Object.keys(v).forEach(function (k) {
        var sub = v[k];
        if (sub && typeof sub === "object" && depth < 3) {
          out += '<div class="section"><p class="hint">' + AD.esc(k) + "</p>" +
            tree(sub, depth + 1) + "</div>";
        }
      });
      return out || AD.empty("（空对象）");
    }
    return card(codeOf(v), { tight: true });
  }

  /* ---------- 已知视图：列与格式化（值缺失时自动退回通用树） ---------- */
  function colsFor(name) {
    if (name === "traces") return [
      ["started_at", "开始时间"],
      ["name", "名称"],
      ["question", "问题"],
      ["elapsed_ms", "耗时", function (v) { return AD.secs(v); }],
      ["cost_cny", "成本", function (v) { return AD.money(v); }],
      ["status", "状态", function (v) {
        return badgeOf(pick({ v: v }, ["v"], "?"), v === "ok" ? "ok" : "warn");
      }]
    ];
    if (name === "tools") return [
      ["name", "工具", codeOf], ["risk", "风险等级", function (v) {
        return badgeOf(v, v === "readonly" ? "ok" : "warn");
      }], ["desc", "说明"]
    ];
    if (name === "approvals") return [
      ["created_at", "创建时间"], ["command", "命令", codeOf],
      ["risk", "风险"], ["isolation", "隔离"], ["status", "状态", function (v) {
        return badgeOf(v, v === "pending" ? "warn" : (v === "consumed" ? "ok" : "neutral"));
      }], ["reason", "原因"]
    ];
    if (name === "audit") return [
      ["ts", "时间"], ["event", "事件", codeOf],
      ["detail", "明细"]
    ];
    return null;
  }

  function renderKnown(name, data) {
    if (name === "metrics") {
      var out = "";
      if (data.runs !== undefined) {
        out += '<div class="kpis">' +
          kpiCard("运行次数", data.runs, data.distinct_questions) +
          kpiCard("错误次数", data.errors) +
          kpiCard("总成本", AD.money(data.cost_cny)) +
          kpiCard("批处理", data.batch_runs) +
          kpiCard("缓存命中率", (Number(data.prompt_cache_hit_rate || 0) * 100).toFixed(1) + "%") +
          kpiCard("P50 / P95", AD.secs((data.elapsed_ms || {}).p50) + " / " +
            AD.secs((data.elapsed_ms || {}).p95)) + "</div>";
        out += info("窗口：" + AD.esc(data.window || "\u2014") +
          "　判定口径的解释在" + '<a href="/dashboard">看板</a>的 ⓘ 与页脚折叠区里。');
      }
      if (data.by_span_name && Object.keys(data.by_span_name).length) {
        out += card(AD.bars(data.by_span_name, "cost_cny", AD.money),
                    { title: "成本分布 · 按 Agent", tight: true });
      }
      if (data.by_span_type && Object.keys(data.by_span_type).length) {
        out += card(AD.bars(data.by_span_type, "cost_cny", AD.money),
                    { title: "成本分布 · 按动作", tight: true });
      }
      return out + tree(otherKeys(data, ["runs", "distinct_questions", "errors",
        "cost_cny", "batch_runs", "prompt_cache_hit_rate", "elapsed_ms",
        "by_span_name", "by_span_type"]));
    }
    if (name === "sandbox") {
      var p = data.policy || {};
      var out2 = '<div class="kpis">' +
        kpiCard("沙箱后端", pick(data, ["backend"])) +
        kpiCard("隔离", data.isolated ? "真容器" : "无隔离",
                data.isolated ? "" : "写操作靠审批约束") +
        kpiCard("白名单命令", (p.whitelisted || 0) + " 条") +
        kpiCard("需人工审批", (p.needs_approval || 0) + " 条") +
        "</div>";
      if (data.note) out2 += info(AD.esc(data.note));
      (data.problems || []).forEach(function (x) { out2 += warn(x); });
      out2 += card(tableOf(pick(data, ["commands"], [])),
                   { title: "命令白名单（含是否需要审批、走哪个通道）", tight: true });
      out2 += tree(otherKeys(data, ["policy", "commands"]));
      return out2;
    }
    if (name === "health") {
      var s = data.security || {};
      var out3 = '<div class="kpis">' +
        kpiCard("服务", pick(data, ["status"])) +
        kpiCard("鉴权", s.auth_enabled ? "已开启" : "关闭") +
        kpiCard("今日额度", s.daily_quota > 0 ? s.daily_quota : "不限", "已用 " +
                (s.daily_used || 0)) +
        kpiCard("限流", (s.rate_limit_per_ip_per_min || 0) + " / " +
                (s.rate_limit_global_per_min || 0), "每 IP / 全站 · 每分钟") +
        "</div>";
      out3 += card(kvOf(s), { title: "安全层状态", tight: true });
      return out3 + tree(otherKeys(data, ["security"]));
    }
    var arr = Array.isArray(data) ? data : pick(data, ["items", "tools", "spans"], null);
    if (Array.isArray(arr)) {
      var rest = Array.isArray(data) ? {} : otherKeys(data, ["items", "tools", "spans"]);
      return card(tableOf(arr, colsFor(name)),
                  { title: name === "approvals" ? "审批单" : "记录", tight: true })
        + tree(rest);
    }
    return tree(data);
  }

  function otherKeys(data, skip) {
    var o = {};
    if (!data || typeof data !== "object" || Array.isArray(data)) return o;
    Object.keys(data).forEach(function (k) {
      if (skip.indexOf(k) < 0) o[k] = data[k];
    });
    return o;
  }

  /* ---------- 编排视图：下面那栏拉**实际跑出来的轨迹** ----------
     ★ 与数据视图共用同一个 fetch 封装与错误形态：这里也是一条 `/traces` 请求，
       失败就写一句原因，不把整页搞崩。 */
  function loadTraces() {
    var box = $("content");
    if (!box) return;
    fetch("/traces?limit=20", { headers: headers() }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (d) {
      if ($("rawJson")) $("rawJson").textContent = JSON.stringify(d, null, 2);
      var items = (d && d.items) || [];
      if (!items.length) {
        box.innerHTML = AD.empty("最近还没有运行记录", "去提问页问一句就有了");
        return;
      }
      box.innerHTML = items.slice(0, 12).map(function (t) {
        return '<div class="field"><label class="f">' + AD.esc(t.started_at || "") +
          "　" + badgeOf(t.status || "?", t.status === "ok" ? "ok" : "warn") +
          "　" + AD.esc(AD.secs(t.elapsed_ms)) + "　" + AD.esc(AD.money(t.cost_cny)) +
          '</label><div><code>' + AD.esc(t.name || "") + "</code> " +
          AD.esc(t.question || "") + "</div></div>";
      }).join("");
    }).catch(function (e) {
      box.innerHTML = AD.empty("取不到轨迹", String((e && e.message) || e));
    });
  }

  /* ---------- 取数 ---------- */
  function load() {
    if (D.flow) { loadTraces(); return; }
    setErr("");
    $("content").innerHTML = '<div class="sk" style="height:160px"></div>';
    fetch(D.api, { headers: headers() }).then(function (r) {
      return r.text().then(function (txt) { return { status: r.status, ok: r.ok, txt: txt }; });
    }).then(function (res) {
      if (res.status === 401) {
        $("tokbar").hidden = false;
        setErr(warn("这个接口需要访问令牌（HTTP 401）。填一次即可" +
          "（令牌存本机浏览器，页面不渲染它）；或直接用 /view/" + D.name +
          "#token=<令牌> 一次性进入。"));
        $("content").innerHTML = AD.empty("没有令牌，取不到数据", "接口本身是 " + D.api);
        return;
      }
      if (res.status === 429) {
        setErr(warn("触发限流（HTTP 429）—— 等一下再点刷新。"));
        $("content").innerHTML = AD.empty("被限流拦住了", "这是安全层在正常工作");
        return;
      }
      if (!res.ok) {
        setErr(warn("HTTP " + res.status + " —— " + res.txt.slice(0, 300)));
        $("content").innerHTML = AD.empty("接口返回了错误", D.api);
        return;
      }
      var data = null;
      try { data = JSON.parse(res.txt); }
      catch (e) {
        setErr(warn("返回不是合法 JSON —— 可能是代理或网关插了一页 HTML。"));
        $("content").innerHTML = '<pre class="mono">' +
          AD.esc(res.txt.slice(0, 1500)) + "</pre>";
        return;
      }
      RAW = data;
      $("stamp").textContent = "载入于 " + new Date().toLocaleTimeString() +
        " · 数据源 " + D.api;
      $("rawJson").textContent = JSON.stringify(data, null, 2);
      $("content").innerHTML = (D.name === "traces" || D.name === "audit")
        ? tree(data) : renderKnown(D.name, data);
    }).catch(function (e) {
      setErr(warn("网络错误：" + (e && e.message ? e.message : e) +
        " —— 确认服务还在跑。"));
      $("content").innerHTML = AD.empty("取不到数据", D.api);
    });
  }

  /* ---------- 导出（旧页的能力） ---------- */
  function download(name, mime, content) {
    var b = new Blob([content], { type: mime });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(b);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }
  function csvOf(arr) {
    var CR = String.fromCharCode(13), LF = String.fromCharCode(10);
    var cols = [];
    arr.slice(0, 500).forEach(function (o) {
      Object.keys(o).forEach(function (k) { if (cols.indexOf(k) < 0) cols.push(k); });
    });
    var lines = [cols.join(",")];
    arr.forEach(function (o) {
      lines.push(cols.map(function (k) {
        var v = o[k];
        if (v === null || v === undefined) v = "";
        if (typeof v === "object") v = JSON.stringify(v);
        return '"' + String(v).split('"').join('""') + '"';
      }).join(","));
    });
    return lines.join(CR + LF);
  }
  function arrayOf(data) {
    if (Array.isArray(data)) return data;
    if (data && typeof data === "object") {
      return data.items || data.tools || data.spans || data.commands || null;
    }
    return null;
  }
  if ($("jsonBtn")) {
    $("jsonBtn").onclick = function () {
      if (RAW === null) return;
      download(D.name + ".json", "application/json", JSON.stringify(RAW, null, 2));
    };
  }
  if ($("csvBtn")) {
    $("csvBtn").onclick = function () {
      var arr = arrayOf(RAW);
      if (!arr) { setErr(warn("这份数据里没有可导出的数组（JSON 原文在折叠区里）。")); return; }
      var bom = String.fromCharCode(0xFEFF);
      download(D.name + ".csv", "text/csv;charset=utf-8", bom + csvOf(arr));
    };
  }
  if ($("refreshBtn")) $("refreshBtn").onclick = load;
  if ($("tokSave")) {
    $("tokSave").onclick = function () {
      var v = ($("token").value || "").trim();
      if (!v) return;
      try { localStorage.setItem(KEY, v); } catch (e) {}
      $("token").value = "";
      $("tokbar").hidden = true;
      load();
    };
  }
  load();
})();
"""


def _mermaid_js(engine: str, api: str) -> str:
    """拉一次 mermaid 原文填进折叠区（图是服务端画的，这一步只是"顺手给原文"）。"""
    return ("(function(){var el=document.getElementById('mermaidText');if(!el)return;"
            "fetch(" + json.dumps(api) + ",{headers:{Accept:'application/json'}})"
            ".then(function(r){return r.ok?r.json():null;})"
            ".then(function(d){el.textContent=(d&&d.graph)||'取不到 mermaid 原文';})"
            ".catch(function(){el.textContent='取不到 mermaid 原文';});})();")


def _flow_body(name: str) -> str:
    """编排视图：满宽纵向堆叠（结构图在上，实际轨迹在下）。

    ★ 用 `.stack` 而不是 `.split`/`.grid.c2`：结构图是一张宽图，
      放进窄栏会被压到看不清（见 `_view_picker` 的说明）。
    """
    body = (
        '<div id="err"></div>'
        + '<div id="tokbar" class="card tight" hidden>'
        + '<div class="btnrow">'
        + '<input type="text" id="token" class="mono" placeholder="访问令牌（本实例开启了鉴权）" '
          'aria-label="访问令牌">'
        + button("保存令牌", size="sm", attrs='id="tokSave"')
        + '<span class="hint">令牌只存在本机浏览器，页面不渲染它。</span>'
        + "</div></div>"
        + card(_flow_card(name))
        + card(
            '<div id="content" class="stack">' + skeleton("100%", "140px") + "</div>"
            + '<details class="acc" style="margin-top:16px">'
              "<summary>原始 JSON（/agent/graph 的完整返回）</summary>"
              '<pre id="rawJson" class="mono">载入中…</pre></details>',
            title="实际运行（最近的 Agent 轨迹）", desc="数据源 /traces?limit=20")
        + note("结构图是静态的（节点与边来自 `build_graph`，不会变）；"
               "下面那栏是<b>实际跑出来的轨迹</b> —— 结构对不对，要看这两者是否一致。")
    )
    return body


def _data_body(name: str) -> str:
    return (
        '<div id="tokbar" class="card tight" hidden>'
        + '<div class="btnrow">'
        + '<input type="text" id="token" class="mono" placeholder="访问令牌（本实例开启了鉴权）" '
          'aria-label="访问令牌">'
        + button("保存令牌", size="sm", attrs='id="tokSave"')
        + '<span class="hint">令牌只存在本机浏览器，页面不渲染它。</span>'
        + "</div></div>"
        + _data_card(name)
    )


def _render(name: str) -> str:
    if name in FLOWS:
        title, desc = FLOWS[name]["title"], FLOWS[name]["desc"]
        body = _flow_body(name)
        head_extra = f"<script>{_JS}</script><script>{_mermaid_js(FLOWS[name]['engine'], '/agent/graph?engine=' + FLOWS[name]['engine'])}</script>"
    else:
        title, desc = VIEWS[name][1], VIEWS[name][2]
        body = _data_body(name)
        head_extra = f"<script>{_JS}</script>"
    body = ('<div class="stack">' + _view_picker(name)
            + '<div id="pageBody">' + body + "</div></div>")
    flags = {"name": name, "api": VIEWS[name][0] if name in VIEWS else "",
             "flow": name in FLOWS}
    head_extra = ("<script>window.__VIEW__="
                  + json.dumps(flags, ensure_ascii=False) + ";</script>" + head_extra)
    return shell(
        body,
        title=title,
        active="/view",
        subtitle=desc,
        actions=(link_button("刷新", f"/view/{quote(name)}", size="sm")
                 + link_button("看板", "/dashboard", size="sm")),
        head_extra=head_extra,
    )


def register(app) -> None:
    """把自己挂到 app 上（由 `app/web/pages/__init__.py` 的 IMPLEMENTED 调用）。"""

    @app.get("/view/{name}", response_class=HTMLResponse, include_in_schema=False)
    def view_page(name: str):
        """JSON 接口的可视化皮 + 编排结构图。数据仍从原接口出（一个事实源，两种皮）。

        未知 name → 404，与旧行为一致（列表在错误信息里给出来，省得去翻代码）。
        """
        if name not in ALL_VIEWS:
            raise HTTPException(status_code=404, detail="未知的数据视图，可选："
                                + "、".join(sorted(ALL_VIEWS)))
        return HTMLResponse(
            _render(name),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
