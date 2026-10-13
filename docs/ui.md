# AgentDesk 界面使用与改造指南

> 这篇分两半：**上半是"怎么用"**（打开哪个地址、点哪里、典型流程），
> **下半是"怎么改"**（设计系统在哪、加一个页面要动哪几个文件、门禁怎么跑）。

---

## 第一部分：怎么用

### 1. 启动

```powershell
cd "E:\Workbuddy\ai agent\agentdesk"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

然后浏览器打开 **<http://127.0.0.1:8000/>**。

本地默认 `AUTH_ENABLED=0`（不需要令牌）。部署到公网时才要开鉴权 ——
开了之后每个请求带 `X-API-Key` 头，界面右上角的徽标会显示"公网 · 需令牌"。

> 第一次跑之前如果检索用不了，先建索引：
> `.venv\Scripts\python.exe -m app.rag.pipeline build`
> 打开任一页面时，右上角的**状态点**会告诉你依赖有没有问题（它就是 `/healthz`）。

### 2. 五个页面各干什么

| 页面 | 地址 | 什么时候用它 |
|---|---|---|
| **总览** | `/` | 第一次打开：看清它能做什么、系统现在什么状态、从哪儿进去 |
| **提问** | `/try` | **主入口**：填问题 → 选能力 → 看它自己决定查什么 |
| **看板** | `/dashboard` | 看它花了多少钱、每步多久、有没有待审批的写操作 |
| **设置** | `/settings` | 查当前生效的配置（密钥只显示"已设置/未设置"，不回显） |
| **数据视图** | `/view/metrics` | 把 JSON 接口套上可视化皮（指标 / 链路 / 工具 / 沙箱 / 审批 / 审计 / 健康） |

界面之外还有三个入口，排障时比页面更有用：

| 地址 | 用途 |
|---|---|
| `/docs` | **自动生成的 API 文档**，45 个接口可直接在页面上试 |
| `/metrics` | Prometheus 指标（接 Grafana 就抓这里） |
| `/healthz` | 依赖深度自检：坏哪一项会**点名**（点侧栏的状态点也能看到） |

### 3. 第一次用：三步

1. 打开 **`/try`**，点输入框下面的**示例问题胶囊**（比如"网站变慢 / 502"），它会填进输入框
2. 能力选 **「多 Agent 编排」**（默认），点 **开始**
   - 右侧的**结果区在点之前就存在**（先显示骨架），出现结论时页面不会跳版
   - 轨迹区会逐步显示它调了哪些工具、每步多久
   - 这一步会调真实模型，**计入每日额度**（页面上有明确提示）
3. 想让它真去查一台机器：问题里带上主机名（例如 `web-01`）。
   只读诊断会自动跑；**任何写操作都会变成一张审批单**，去 `/dashboard` 的"待审批"里确认

### 4. 典型的三个用法

**① 排查一个具体故障**
`/try` 里描述现象（"web-01 上的网站访问很慢，有时报 502"）→ 看轨迹里它查了什么 →
去 `/view/audit` 看这次的完整审计（谁、何时、判成什么风险、做了什么）。

**② 回答"这套东西到底花了多少钱、值不值"**
`/dashboard`：成本按 **Agent** 与 **动作** 两个维度切开（**两个维度不可相加**，页面有标注），
下方"对账"会告诉你叶子 span 的合计与总账偏差多少（超过 1% 会变红）。

**③ 接入告警（无人值守）**
在 `.env` 里配好通知渠道（`NOTIFY_CHANNELS` + 对应 URL），
把 Alertmanager 的 webhook 指向 `/webhook/alert`。
告警来了它会：去重聚合 → 判断风险 → 只读诊断 → 把结论推到通知渠道 → 沉淀成"事件"。
写操作仍然只开审批单。
配完先跑一次自检：`.venv\Scripts\python.exe scripts\notify_check.py`

### 5. 什么都不打开，只用命令行

```powershell
.venv\Scripts\python.exe scripts\smoke_test.py            # 九层自检
.venv\Scripts\python.exe scripts\show_live.py --only tail_log --service wp-app
.venv\Scripts\python.exe scripts\notify_check.py          # 通知能不能发出去
.venv\Scripts\python.exe scripts\ui_check.py              # 界面门禁
```

MCP 客户端（Claude Desktop 等）见 `docs/mcp-server.md`。

---

## 第二部分：怎么改界面

### 6. 目录与职责

```
app/web/
├── __init__.py        mount(app)：挂 /static + 注册已接管的页面
├── layout.py          shell()：统一外壳（侧栏 + 页头 + 页脚），版式只在这一处
├── components.py      组件库，每个组件一个纯函数（返回 HTML 片段）
├── pages/
│   ├── __init__.py    ★ IMPLEMENTED：列出"已由新 UI 接管"的页面
│   ├── home.py        register(app) → GET /
│   ├── try_.py        register(app) → GET /try   （try 是关键字，所以叫 try_）
│   ├── dashboard.py   register(app) → GET /dashboard
│   ├── settings.py    register(app) → GET /settings
│   └── view.py        register(app) → GET /view/{name}
└── static/
    ├── app.css        ★ 设计系统唯一实现（tokens + 组件样式）
    └── app.js         window.AD：esc / money / secs / get / watchHealth / timeline / bars
```

**三条硬规矩**

1. **样式只能写在 `app.css`**。页面里出现 `<style>` 会被 `scripts/ui_check.py` 判红 ——
   这正是上一版界面的病根（5 个页面各带一份 34–53 行的重复样式，改一处要翻 5 个地方）。
2. **不许引外部 CDN**。零依赖、可离线是项目的硬约束；
   上一版 `/try` 从 jsDelivr 加载 markdown 库，断网就是坏的。
3. **所有插进 DOM 的外部文本都要转义**（服务端 `esc()` / 前端 `AD.esc()`）。
   页面里渲染的是用户提问、命令输出、容器名 —— 少转一处就是一个 XSS。

### 7. 加一个新页面（或接管一个旧页面）

1. 在 `app/web/pages/` 新建 `<name>.py`：

```python
from fastapi.responses import HTMLResponse
from app.web.layout import shell
from app.web.components import card, kpi, empty

def render(...) -> str:                      # 纯函数，便于单测
    body = card("…", title="…")
    return shell(body, title="页面标题", active="/<name>", subtitle="一句话说明")

def register(app):
    @app.get("/<name>", response_class=HTMLResponse, include_in_schema=False)
    def _page():
        return HTMLResponse(render())
```

2. 把 `<name>` 加进 `app/web/pages/__init__.py` 的 `IMPLEMENTED`
3. **如果它替换了 `main.py` 里的旧路由，必须同时删掉旧的那个** ——
   FastAPI 允许重复路径，**先注册的赢**，所以留着旧路由 = 新页面根本不生效
4. 跑门禁：`python scripts/ui_check.py`，再跑 `pytest`

### 8. 设计系统速查

| 想做的事 | 用什么 |
|---|---|
| 页面骨架 | `shell(body, title, active, subtitle, actions)` |
| 卡片 / KPI 数字 / 徽标 / 提示条 | `card` `kpi` `badge` `note` |
| 表格（数字自动右对齐） | `table(headers, rows, foot=…, numeric_from=1)` |
| 条形图 / 时间线（**别自己拼**） | `bar_list` / `timeline` |
| 空态 / 骨架 | `empty` / `skeleton`（**空态是组件，不是"什么都不显示"**） |
| 货币 / 耗时 | `money()` `seconds()`（前端对应 `AD.money` / `AD.secs`） |
| 指标口径说明 | `info_tip("…")` —— ⓘ 悬停，**不要写进正文** |

**四条排版约定**（写在 `app.css` 顶部，也是评审时的检查点）

1. 数字一律右对齐 + `tabular-nums`
2. 货币按量级自适应精度：`¥0` / `¥0.0004` / `¥12.34` —— 不把 0 写成 `¥0.0000`
3. 指标定义走 ⓘ + 页脚折叠，不占正文（上一版看板被 100+ 字散文占领）
4. 条形图的填充**必须** `display:block`：
   ```css
   .bar .fill { display:block; height:100%; }   /* ← 行内元素的高度不生效！ */
   ```
   上一版用行内 `<span>` + `height:100%`，于是**图表画出来是空的**，
   而 `pytest` 永远抓不到（它不渲染 CSS）。

### 9. 界面门禁（`scripts/ui_check.py`）

它做六组检查，都是"只有渲染出来才会暴露"的问题：

| 检查 | 抓什么 |
|---|---|
| 静态 | 页面里不许有 `<style>`；条形填充必须有 `display:block` |
| 组件 | 服务端渲染的条形图/时间线真的产出了 `.fill` / `.fl` 节点 |
| **CSS 覆盖对账** | 页面里用到的每个类，`app.css` 里都有定义 —— 类名打错或**样式被误删**都不会报错，只会静默地没有样式（真实例子：删旧 CSS 时把 `try.js` 还在用的 `.steps/.step` 一起删了，流式步骤当场失去样式而无人察觉） |
| **JS 语法** | `static/*.js` + **渲染后**的页面内联脚本过一遍 `node --check` —— 内联脚本写错一个字符就是"服务端 200、pytest 全绿、页面全空"。★ 检查渲染结果而不是页面源码：源码里的内联脚本是 f-string（含 `{json.dumps(...)}`），直接查会得到假错误 |
| **实测布局** | 用无头 Chrome 打开 harness 页，让 JS **量出填充条的真实像素高度**并写回 DOM，再读回来断言 > 0 —— 这是"空图表"那类 bug 的唯一守门人（不需要 Pillow / Playwright） |
| 页面冒烟 | 5 个页面 200 · 不含外部 CDN · 不含页面级 `<style>` |

```powershell
.venv\Scripts\python.exe scripts\ui_check.py            # 退出码 0 = 通过（当前 23 项）
.venv\Scripts\python.exe scripts\ui_check.py --open     # 顺便打开 harness 看一眼
.venv\Scripts\python.exe scripts\ui_check.py --json     # 给 CI 用
```

★ **门禁自己必须是准的**：写这个脚本时先后误报过三次（把 docstring 里的
"`<style>`" 当页面样式、把 JS 拼接的 `'class="' + cls + '"'` 当类名、
把 `<script type="application/json">` 数据块当 JS）。**一个会乱报的门禁
最终会被所有人学会无视** —— 所以每次误报都当场改准，而不是"反正不影响"。`check_no_page_style` / `check_css_coverage` 都改用 AST 或严格白名单匹配，
`check_inline_js_syntax` 只认真正的脚本标签。

### 10. 已知边界（不藏）

- **亮色主题**：暗色 token 已预留（变量层），但没做切换开关 ——
  双套配色在这个体量上维护面大于收益（沿用 2026-09-30 的决定）
- **服务端渲染 + 少量原生 JS**：没有组件框架。交互复杂到一定程度就该换方案，
  但现在这几个页面（读多写少）不值当引入构建链
- **`/view/*` 是"JSON 接口的可视化皮"**：数据仍从原接口出，界面只负责呈现
- 界面里的数字最终以 `/metrics/summary` 与 `audit.jsonl` 为准；
  页面只读，不做任何写入（避免"看板改数据"这类反向风险）
