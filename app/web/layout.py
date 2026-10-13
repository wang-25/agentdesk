# -*- coding: utf-8 -*-
"""统一外壳：`<head>` + 顶栏（或侧栏）+ 页脚。每个页面只提供正文。

【为什么外壳必须只有一个实现】
原实现是 5 个页面各自一份 `<style>`（34–53 行）+ 各写一遍导航与页头 ——
改一处颜色要翻 3000 行、翻 5 个地方，于是必然漂移（原界面里卡片与提示条的圆角
就是不一致的）。现在**版式只有这里一处**。

【导航与状态】
· 侧栏四个入口：总览 / 提问 / 看板 / 设置
· 底部状态点取 `/healthz`（由 app.js 拉取）—— 把 M4 做的健康检查露到界面上，
  以前要知道"哪一项坏了"得敲命令
· 环境徽标（本地免鉴权 / 公网需令牌）由调用方传入，替代原先塞在提问页正文里的
  `AUTH_ENABLED=0` 说明
"""

from app.web.components import esc

BRAND = "AgentDesk"
VERSION = "1.0.0"

# 节点中文名：时间线、轨迹图共用（原来只在两页的 JS 里各写了一遍）
NODE_LABEL = {
    "supervisor": "调度决策", "intent": "意图路由", "knowledge": "知识检索",
    "diagnose": "工具执行", "reason": "纯推理分析", "verify": "结果校验",
    "remediate": "处置建议", "finalize": "汇总输出",
    "vector": "向量检索", "bm25": "关键词检索", "hybrid": "混合检索",
}

# (路径, 图标, 名称)
NAV = [
    ("/", "▦", "总览"),
    ("/try", "▶", "提问"),
    ("/dashboard", "▤", "看板"),
    ("/settings", "⚙", "设置"),
]


def topbar_nav(active: str) -> str:
    items = []
    for href, icon, label in NAV:
        current = ' aria-current="page"' if href == active else ""
        items.append(f'<a href="{esc(href)}"{current}>'
                     f'<span class="ic">{icon}</span>{esc(label)}</a>')
    return ('<nav>' + "".join(items) + "</nav>")


def shell(body: str, *, title: str, active: str = "", subtitle: str = "",
          actions: str = "", env_badge: str = "本地 · 免鉴权",
          head_extra: str = "", body_class: str = "") -> str:
    """把正文包进统一外壳。

    · `active`   当前页路径（用于导航高亮）
    · `subtitle` 页面头带里的一句话说明
    · `actions`  页面头带右侧的操作按钮
    """
    side = f"""<aside class="side">
  <div class="brand"><span class="mark">A</span>
    <span class="wm">AgentDesk<small>ops agent</small></span></div>
  {topbar_nav(active)}
  <div class="side-foot">
    <div class="row"><span class="dot" id="healthDot"></span>
      <span id="healthText">检查中…</span></div>
    <div class="row"><span>{esc(env_badge)}</span></div>
    <div class="row"><a href="/docs">API 文档</a> ·
      <a href="/metrics">指标</a> · <a href="/healthz">自检</a></div>
  </div>
</aside>"""

    head = f"""<div class="pagehead"><div class="inner">
  <div><p class="t">{esc(title)}</p>
  {f'<p class="d">{esc(subtitle)}</p>' if subtitle else ''}</div>
  {f'<div class="actions">{actions}</div>' if actions else ''}
</div></div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · AgentDesk</title>
<link rel="stylesheet" href="/static/app.css">
{head_extra}</head>
<body class="{esc(body_class)}">
<div class="shell">{side}
  <div class="main">{head}
    <div class="content">{body}</div>
  </div>
</div>
<script src="/static/app.js"></script>
</body></html>"""
