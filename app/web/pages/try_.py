# -*- coding: utf-8 -*-
"""提问台（`/try`）—— 主交互面。

【为什么旧页面要重做】
旧 `/try` 最大的毛病不是不好看，是**交互闭环缺失**：
整页没有任何输出容器，点「开始」之后结果才"长出来" ——
提交前后页面高度不同、四类结果（轨迹/结论/证据/成本）没有固定位置，
读者不知道会等到什么、也不知道结果会出现在哪。

所以这里的规则是：
    ★ 结果区在**提交前就存在**（四个分区 + 空态/骨架），提交后**原地填充**。
      页面的结构在任何时刻都是完整可预期的。

其他改动：
· textarea 用真 `placeholder`（旧页把示例问题写在标签里，看起来像已填好的值，
  用户容易把示例原样提交）；示例改成可点击的 chips，点一下才填进去。
· 页面级 `<style>` 一律没有 —— 样式只有 `static/app.css` 一处。
· **不引任何 CDN**（旧页从 jsdelivr 加载 marked）：markdown 渲染见
  `static/try.js` 里的 `AD.md`，先转义再套标签、白名单式，可离线/内网运行。
· `AUTH_ENABLED=0` / "见 README 的公网安全层"这类开发者向文案不进首屏，
  环境信息由 `shell(env_badge=...)` 承担。
"""

import json

from fastapi import Response
from fastapi.responses import HTMLResponse

from app.security import AUTH_ENABLED
from app.web.components import card, empty, esc, link_button, note, skeleton
from app.web.layout import shell

# 能力档位：与旧页**逐条一致**（value 就是接口的 engine 参数）。
# `stream=True` 的那一档走 SSE —— 编排过程本身就是演示的主菜。
ENGINES = [
    ("supervisor", "多 Agent 编排（推荐，约 10-30 秒）", True),
    ("rag", "知识库问答 · 带引用溯源（约 4 秒）", False),
    ("langgraph", "Agent 诊断 · LangGraph 状态图", False),
    ("handwritten", "Agent 诊断 · 手写 ReAct 循环", False),
]

# 示例问题 chips。点一下填入输入框（旧页是"预填值"，两者体验差别很大）。
EXAMPLES = [
    "web-01 上的网站访问很慢，有时报 502，帮我看下原因",
    "磁盘快满了怎么处理",
    "查一下容器的状态",
    "Kafka 消费组 lag 怎么排查",
]

# 环境徽标：由服务端决定，页面不去猜（旧页踩过"前端假设部署形态"的坑）。
# ★ 每次渲染时现算，不缓存成模块常量 —— 否则它与同一页里的令牌框
#   （按 AUTH_ENABLED 条件渲染）可能不一致，那就是"页面自相矛盾"。
def env_badge() -> str:
    return "公网 · 需令牌" if AUTH_ENABLED else "本地 · 免鉴权"


def _input_card() -> str:
    options = "".join(
        f'<option value="{esc(v)}">{esc(label)}</option>'
        for v, label, _ in ENGINES)
    chips = "".join(
        f'<button type="button" class="chip" data-q="{esc(q)}">{esc(q)}</button>'
        for q in EXAMPLES)
    token_box = ""
    if AUTH_ENABLED:
        token_box = (
            '<div class="field" id="tokenField">'
            '<label class="f" for="token">访问令牌（存本机浏览器，别在公共电脑上填）</label>'
            '<input id="token" type="password" autocomplete="off" '
            'placeholder="粘贴 AGENT_TOKEN 的值">'
            '<p class="hint" id="tokstate"></p>'
            '<p class="hint">这个实例开启了鉴权，接口要令牌；'
            '页面本身是空壳，不含任何数据。</p></div>')
    return card(
        f'{token_box}'
        f'<div class="field">'
        f'<label class="f" for="q">问它一个问题</label>'
        f'<textarea id="q" rows="5" spellcheck="false" autocomplete="off" '
        f'placeholder="例如：web-01 上的网站访问很慢，有时报 502，帮我看下原因">'
        f'</textarea>'
        f'<p class="hint">示例（点一下填进输入框）：</p>'
        f'<div class="chips">{chips}</div></div>'
        f'<div class="field"><label class="f" for="mode">能力</label>'
        f'<select id="mode">{options}</select></div>'
        f'<div class="btnrow" style="margin-top:var(--s4)">'
        f'<button type="button" class="btn primary" id="go">开始</button>'
        f'<button type="button" class="btn ghost" id="clear">清空</button>'
        f'<button type="button" class="btn ghost" id="stop" disabled>停止</button>'
        f'</div>'
        f'<p class="hint">多 Agent 编排会调用真实模型，属于<b>计费动作</b>，'
        f'计入每日额度。写操作永远不会自动执行 —— 它们会变成一张审批单等人批准。</p>',
        title="提问", desc="填一句话，剩下的它自己决定")


def _result_card(card_id: str, inner: str, *, title: str, desc: str = "",
                 actions: str = "") -> str:
    """带固定 id 的结果卡。

    ★ 为什么不用 `components.card()` 再补 id：那样得对渲染结果做字符串替换，
      而"替换失败"是静默的（页面上看不出少了 id，只是前端永远填不进去）。
      这里 id 是**拼进去**的，缺了它这一行代码就不成立。
    """
    head = ('<div class="card-head">'
            f'<span class="t">{esc(title)}</span>'
            + (f'<span class="d">{esc(desc)}</span>' if desc else "")
            + (f'<span class="actions">{actions}</span>' if actions else "")
            + "</div>")
    return f'<div class="card" id="{esc(card_id)}">{head}{inner}</div>'


def _result_column() -> str:
    """四个分区。★ 它们**现在**就在页面里，不是提交后才出现。

    每个分区一个固定 id，作为前端填充的锚点：**只换内容，不换结构**。
    初始态分别是骨架（执行轨迹）与空态（其余三个），
    所以"还没提交"和"正在跑"和"跑完了"在版面上占据同一块地方。
    """
    trace = _result_card(
        "traceBox", skeleton("100%", "12px") * 4,
        title="执行轨迹", desc="它自己决定查什么、查几轮",
        actions='<span class="badge neutral" id="traceCount">等待提问</span>')
    conclusion = _result_card(
        "conclusionBox",
        empty("还没有结论", "提交一个问题后，这里是它给出的答案与校验结论", icon="◌"),
        title="结论")
    evidence = _result_card(
        "evidenceBox",
        empty("还没有证据", "工具输出、知识库引用与逐步轨迹都会落在这里", icon="◌"),
        title="证据")
    cost = _result_card(
        "costBox",
        empty("还没有成本", "本轮调用的轮次、工具次数、tokens 与耗时", icon="◌"),
        title="成本")
    return f'<div class="stack">{trace}{conclusion}{evidence}{cost}</div>'


def body() -> str:
    return f"""<div class="split">
  <div class="stack">
{_input_card()}
{note("提问会调用真实模型并计费；工具查询是只读的。"
      "想先看系统是否健康，去 <a href='/'>总览</a>。")}
{link_button("← 回总览看系统状态", "/", tone="ghost", size="sm")}
  </div>
  <div>
{_result_column()}
  </div>
</div>
<script id="tryConfig" type="application/json">{_config_json()}</script>
<script src="/static/try.js" defer></script>"""


def _config_json() -> str:
    """给前端的两件事：要不要令牌、流式那一档的 engine 名。

    ★ 用 JSON 而不是"把 Python 变量插进 JS 字面量"：值里不会出现引号/换行，
      也不需要在模板里手写 `true/false`。
    """
    return json.dumps({"needsToken": bool(AUTH_ENABLED),
                       "streamEngine": "supervisor"}, ensure_ascii=False)


def page() -> str:
    return shell(
        body(),
        title="提问台",
        active="/try",
        subtitle="填一句话，看它自己决定查什么 —— 过程与结论都在右边",
        actions=link_button("看看板", "/dashboard", tone="ghost", size="sm"),
        env_badge=env_badge(),
    )


def register(app) -> None:
    @app.get("/try", response_class=HTMLResponse, include_in_schema=False)
    def try_page(response: Response) -> str:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return page()


__all__ = ["register", "page", "body", "ENGINES", "EXAMPLES", "env_badge"]
