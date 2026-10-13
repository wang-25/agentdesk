# -*- coding: utf-8 -*-
"""组件库：每个组件一个纯函数，返回 HTML 片段。

【为什么是"函数返回字符串"而不是模板引擎】
零依赖是这个项目的硬约束（`requirements.txt` 只有 9 个直接依赖，CI 有守卫）。
而这些组件本质就是"拼一段 HTML"，用 f-string 足够；
真正的模板引擎带来的收益（继承、宏）在这里用**一个 `shell()` 外壳函数**就解决了。

【约定】
· 每个组件的参数都是**普通 Python 值**，不接收 ORM 对象 / 请求对象 —— 可单测
· 所有插值都过 `esc()`（除了显式标了 `safe_` 前缀的参数）
· 组件不写 `<style>`：样式全在 `static/app.css` 里，一处维护
"""

from html import escape


def esc(value) -> str:
    """转义一切插进 HTML 的值。

    ★ 默认转义而不是"信任调用方"：页面里会渲染用户提问、命令输出、容器名 ——
      这些都是**外部输入**。少转一处就是一个 XSS。
    """
    return escape(str(value if value is not None else ""), quote=True)


# ============================================================
# 基础块
# ============================================================
def card(body: str, *, title: str = "", desc: str = "",
         actions: str = "", tight: bool = False, hover: bool = False,
         cls: str = "") -> str:
    classes = "card" + (" tight" if tight else "") + (" hover" if hover else "")
    if cls:
        classes += " " + cls
    head = ""
    if title or desc or actions:
        head = ('<div class="card-head">'
                f'<span class="t">{esc(title)}</span>'
                + (f'<span class="d">{esc(desc)}</span>' if desc else "")
                + (f'<span class="actions">{actions}</span>' if actions else "")
                + "</div>")
    return f'<div class="{classes}">{head}{body}</div>'


def kpi(label: str, value, *, unit: str = "", note: str = "",
        tone: str = "", info: str = "") -> str:
    """KPI 数字卡。`tone`: "" | "ok" | "err" | "brand"（brand = 渐变字）。"""
    badge = info_tip(info) if info else ""
    unit_html = f"<small>{esc(unit)}</small>" if unit else ""
    note_html = f'<div class="x">{esc(note)}</div>' if note else ""
    cls = "kpi" + (f" {tone}" if tone else "")
    return (f'<div class="{cls}"><div class="k">{esc(label)}{badge}</div>'
            f'<div class="v">{esc(value)}{unit_html}</div>{note_html}</div>')


def info_tip(text: str) -> str:
    """ⓘ 口径提示。**指标定义一律走这里，不写进正文**（看板上曾被 100 字散文占领）。"""
    return f'<i class="info" title="{esc(text)}">i</i>'


def badge(text: str, tone: str = "neutral") -> str:
    """tone: neutral | ok | warn | err | info | brand"""
    return f'<span class="badge {esc(tone)}">{esc(text)}</span>'


def note(text: str, tone: str = "") -> str:
    """提示条。`tone`: "" | warn | ok | err"""
    icon = {"warn": "⚠", "ok": "✓", "err": "✕"}.get(tone, "▸")
    cls = "note" + (f" {tone}" if tone else "")
    return f'<div class="{cls}"><span class="ic">{icon}</span><span>{text}</span></div>'


def empty(text: str, hint: str = "", icon: str = "◌") -> str:
    """空态。**空态是组件**，不是"什么都不显示"（原 /try 就没有结果区）。"""
    return (f'<div class="empty"><div class="big">{esc(icon)}</div>'
            f'<div>{esc(text)}</div>'
            + (f'<p class="hint">{esc(hint)}</p>' if hint else "")
            + "</div>")


def skeleton(width: str = "100%", height: str = "12px") -> str:
    return f'<div class="sk" style="width:{esc(width)};height:{esc(height)}"></div>'


def button(label: str, *, tone: str = "primary", size: str = "",
           attrs: str = "", kind: str = "button") -> str:
    cls = "btn" + (f" {tone}" if tone else "") + (f" {size}" if size else "")
    return f'<button type="{esc(kind)}" class="{cls}" {attrs}>{esc(label)}</button>'


def link_button(label: str, href: str, *, tone: str = "ghost",
                size: str = "") -> str:
    cls = "btn" + (f" {tone}" if tone else "") + (f" {size}" if size else "")
    return f'<a class="{cls}" href="{esc(href)}">{esc(label)}</a>'


# ============================================================
# 数据展示
# ============================================================
def table(headers: list, rows: list, *, foot: list = None,
          numeric_from: int = 1) -> str:
    """表格。`numeric_from` 之后的列自动右对齐 + 等宽数字。

    ★ 数字右对齐不是审美偏好：左对齐时个位/十位/百位错开，
      一列数字没法竖着扫读（原看板就是左对齐）。
    """
    def cells(items, tag):
        out = []
        for i, cell in enumerate(items):
            cls = ' class="r"' if i >= numeric_from else ""
            out.append(f"<{tag}{cls}>{cell}</{tag}>")
        return "".join(out)

    head = "<tr>" + cells([esc(h) for h in headers], "th") + "</tr>"
    body = "".join("<tr>" + cells(list(r), "td") + "</tr>" for r in rows)
    foot_html = ""
    if foot:
        foot_html = "<tfoot><tr>" + cells(list(foot), "td") + "</tr></tfoot>"
    return (f'<div class="tablewrap"><table><thead>{head}</thead>'
            f"<tbody>{body}</tbody>{foot_html}</table></div>")


def bar_list(items: list, *, fmt=None) -> str:
    """横向条形图。`items`: [(标签, 数值), ...]，按数值降序。

    这里**必须** `.fill{display:block}`（见 app.css 的注释）：原实现用行内 span
    配 height:100%，高度不生效，整张图是空的。
    """
    fmt = fmt or (lambda v: f"{v:g}")
    items = [(str(k), float(v or 0)) for k, v in items]
    if not items:
        return empty("暂无数据")
    items.sort(key=lambda kv: kv[1], reverse=True)
    top = max(v for _, v in items) or 1.0
    rows = []
    for label, value in items:
        pct = max(1.0, round(value * 100 / top, 2))
        rows.append(
            f'<div class="bar"><span class="lbl" title="{esc(label)}">{esc(label)}</span>'
            f'<span class="track"><span class="fill" style="width:{pct}%"></span></span>'
            f'<span class="val">{esc(fmt(value))}</span></div>')
    return '<div class="bars">' + "".join(rows) + "</div>"


def timeline(items: list) -> str:
    """执行时间线。`items`: [{node|name, elapsed_ms, ok}]，条宽按耗时占比。"""
    items = list(items or [])
    total = sum(float(it.get("elapsed_ms") or 0) for it in items)
    if not items or not total:
        return empty("暂无计时数据")
    from app.web.layout import NODE_LABEL

    rows = []
    for it in items:
        ms = float(it.get("elapsed_ms") or 0)
        pct = max(1.0, round(ms * 100 / total, 2))
        name = it.get("node") or it.get("name") or "?"
        bad = " bad" if it.get("ok") is False else ""
        rows.append(
            f'<div class="row"><span class="nm">{esc(NODE_LABEL.get(name, name))}</span>'
            f'<span class="tr"><span class="fl{bad}" style="width:{pct}%"></span></span>'
            f'<span class="ms">{ms:.0f}ms</span></div>')
    return ('<div class="tl">' + "".join(rows)
            + f'<p class="hint">总耗时 {total / 1000:.1f}s · 条宽按各步耗时占比</p></div>')


def kv(pairs: list) -> str:
    body = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs)
    return f'<dl class="kv">{body}</dl>'


def money(value) -> str:
    """货币：按量级自适应精度。

    ★ 原看板一律四位小数，于是 ¥0 显示成 `¥0.0000`，整列看起来全是零。
      这里的规则：0 → `¥0`；极小 → 四位；常规 → 两位；大额 → 千分位。
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if v == 0:
        return "¥0"
    if abs(v) < 0.01:
        return f"¥{v:.4f}"
    if abs(v) < 1000:
        return f"¥{v:.2f}"
    return f"¥{v:,.0f}"


def seconds(ms) -> str:
    """毫秒 → 人类可读。数字与单位之间**留空格**（原界面写成 `耗时ms`）。"""
    try:
        ms = float(ms or 0)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s"
