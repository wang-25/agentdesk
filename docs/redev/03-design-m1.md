# M1 实施方案（I-0 自检语义 + I-1 测试与 CI 门禁）

> 对应路线图 [`02-roadmap.md`](02-roadmap.md) 的 M1；解决审计报告中的 **D1/D2/D3/D4**（阻断级）与 **0.2-5**。
>
> **状态：待用户确认后开工**（Rules：先评估后动手）。

---

## 0. 本里程碑要达成的一句话目标

> **让"改坏了"这件事在 5 分钟内被自动发现，而不是靠人记得去跑一遍脚本。**

现状的两个具体漏洞（都已实测确认）：

1. `smoke_test.py` 在服务未启动时把整层标为跳过，**仍然 `exit 0`**（`smoke_test.py:819-825,1115-1129`）——
   我实测那次"九层全通"里，HTTP 层根本没跑。
2. 仓库里**没有 CI**，README 却写着"退出码 0 = 全通，可接进 CI"（`README.md:308`）。

---

## 1. I-0 · 自检语义修正（0.5 人日 · 零风险）

### 1.1 改动点

| 文件 | 改动 |
|---|---|
| `scripts/smoke_test.py` | ① `record()` 增加 `kind` 参数：`"opt"`（未加 `--full` 的可选项）/ `"env"`（环境未就绪，如服务没起来）；② `summarize()` 分别统计两类跳过，文案写明"**这些层没有被验证过**"；③ `main()` 增加 `--strict`；④ 更新模块 docstring 的退出码说明（现为 `:24`） |
| `README.md` | 自检一节的表述与实测口径对齐（不再暗示"跑完就代表九层都验证过"） |
| `docs/overview.md` | §5 同一处口径同步 |

### 1.2 行为表（默认行为完全不变，只新增严格档）

| 情况 | 默认 | `--strict` |
|---|---|---|
| 有真失败项 | 退出码 1 | 退出码 1 |
| 有**环境型**跳过（服务没跑） | 退出码 0，汇总明确标注「未验证」 | **退出码 2** |
| 只有**可选型**跳过（未加 `--full`） | 退出码 0 | 退出码 0（这是主动选择，不算未验证） |
| 无跳过、无失败 | 退出码 0 | 退出码 0 |

> 为什么把两种跳过分开：`--full` 是**用户主动选择不跑**（要花钱），
> 而"服务没起来"是**该验证却没验证**。混为一谈正是当前 exit 0 会误导人的原因。

### 1.3 验收

```bash
# ① 服务未启动 → 必须非 0，且明确指出哪层未验证
.venv\Scripts\python.exe scripts\smoke_test.py --strict   # 期望退出码 2

# ② 默认档向后兼容
.venv\Scripts\python.exe scripts\smoke_test.py            # 期望退出码 0 + 明确标注未验证层数

# ③ 服务起来后 → 严格档必须 0
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000   # 另一窗口
.venv\Scripts\python.exe scripts\smoke_test.py --strict   # 期望退出码 0
```

---

## 2. I-1 · 测试与 CI 门禁（5–8 人日 · 低风险）

### 2.1 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| **接口层怎么在 CI 里被验证** | 用 FastAPI `TestClient` 写 **pytest 测试**，而不是靠 `smoke_test.py` | TestClient **不需要真起服务、不需要密钥**，能在 CI 里稳定跑；这比修自检脚本更彻底 |
| **测试能不能调模型** | **一律禁止**。所有模型调用用 `monkeypatch` 打桩 | CI 必须零成本、密封、确定性 |
| **运行时依赖** | `requirements.txt` **仍是 9 个，一个不加** | 那是项目对外宣称的卖点，不能被工具链污染 |
| **新依赖放哪** | 新增 `requirements-dev.txt`（`pytest` / `ruff` / `mypy`） | 开发依赖与运行时依赖分离 |
| **工具配置放哪** | 新增 `pyproject.toml`，**只放 `[tool.*]` 段，不写 `[project]`** | 避免让人误以为项目变成了可安装包；只作工具配置载体 |
| **mypy 范围** | 只覆盖**新增模块**起步，不一次性修 1.3 万行 | 渐进式；避免一个巨型"格式化提交"淹没真实改动 |
| **新功能进不进 `main.py`** | 不进。本里程碑只新增 `tests/`、配置与 CI | 遵循路线图原则：`main.py` 只做接线，不整体重写 |

### 2.2 新增文件清单

```
agentdesk/
├── tests/
│   ├── conftest.py               # 公共 fixture：临时 logs 目录、禁网守卫、ApprovalStore 工厂
│   ├── test_policy.py            # 命令白名单与路径准入（安全边界第一道门）
│   ├── test_approvals.py         # 审批状态机（写操作的唯一闸门）
│   ├── test_intent.py            # 意图解析与校验（防注入）
│   ├── test_costs.py             # 成本口径与双维对账（"会算账"的证据）
│   ├── test_rag_store.py         # RRF / BM25 / 向量兜底（检索质量的地基）
│   ├── test_tracer.py            # span 父子归并与唯一事实源（成本正确性的前提）
│   ├── test_alert_normalize.py   # 告警归一化（AIOps 入口）
│   └── test_api_smoke.py         # 接口层：26 个 OpenAPI 操作的存在性 + 鉴权边界
├── requirements-dev.txt
├── pyproject.toml                # 仅 [tool.pytest] / [tool.ruff] / [tool.mypy]
├── eval/baseline.json            # 检索指标基线（阈值门禁用）
└── .github/workflows/ci.yml
```

### 2.3 测试矩阵（用例要点）

| 测试文件 | 覆盖对象（真实 API） | 关键用例 |
|---|---|---|
| `test_policy.py` | `policy.decide` / `escalate` / `risk_of_action` / `fingerprint` / `catalog` | 13 条白名单规则**各 1 正例**（证明"合法的没被误拦"）；9 类攻击面全拒（复用自检的攻击面清单）；路径：`..`、相对路径、shell 元字符、`/var/log` 下 `.log` 与非 `.log`；`du` 的 `_INSPECT_DIRS` 与 `"/"` 的单独判断（`policy.py:264-282` 那个"匹配一切"的坑）；`escalate`：模型自报**只能抬高不能降低**；`fingerprint` 稳定性（同 argv 同值、改挂载即变） |
| `test_approvals.py` | `ApprovalStore(path=tmp)` | 状态机全迁移 `pending→approved→consumed`、`→rejected`、`→expired`；批准必须填 `by`；指纹不符拒绝；**重复 consume 拒绝**；`create/get/list/counts` 一致性；**重启折叠**（新建 store 指向同一文件，状态须一致）；坏行容忍（写一行垃圾，其余仍可读） |
| `test_intent.py` | `validate_intent`（纯函数）+ `parse_intent`（打桩模型） | 字段缺失/类型错误/未知 host/非法 action；模型返回带 ```` ``` ```` 包裹、带解释文字、非法 JSON → `IntentParseFailed`；重试上限 3 次后失败 |
| `test_costs.py` | `cost_of` / `aggregate` / `resolve_model` / `is_peak` | 缓存命中 vs 未命中的价差；峰谷价差（固定 `at` 时刻）；未知模型走兜底价且出现在 `unpriced_models()`；别名归一（`deepseek-chat`→`flash`）；`aggregate` 的双维对账偏差在容忍内；**"空结果"与"没有数据"是两种状态** |
| `test_rag_store.py` | `tokenize` / `BM25` / `rrf_fuse` / `VectorStore` | 中文/英文/混合分词；BM25 打分序；RRF 融合确定性 + 只在一路出现的项仍被保留；真索引上 `hybrid` 检索 top1 = `nginx-502.md`；兜底 embedder 的 `describe()` 如实标注 `local-hash` |
| `test_tracer.py` | `span` / `trace` / `sum_usage` / `read_recent` | 父 span usage = 子 span 之和；**节点 span 不叠加引擎自报 usage**（唯一事实源，`supervisor.py:414-424`）；异常时 `status=error` 且**异常照常抛出**；无 trace 时 `_NullSpan` 静默；`sum_usage` 过滤嵌套字段（`prompt_tokens_details` 不炸） |
| `test_alert_normalize.py` | `normalize_alerts` / `alert_to_question` | Alertmanager 标准格式（`alerts[]`+labels/annotations）、Zabbix 风格、裸 dict；缺字段容错；生成的问题文本可读且含主机与服务 |
| `test_api_smoke.py` | FastAPI `TestClient` | 26 个 OpenAPI 操作**逐一存在**（防路由被误删）；`/health` `/` `/sandbox` `/traces` `/metrics/summary` `/audit` `/approvals` `/agent/tools` 返回 200；`AUTH_ENABLED=1` 时**无令牌一律 401、带令牌放行**（鉴权边界，零成本）；`/settings/api` 未授权时不可读 |

**目标规模**：8 个文件、约 70–90 条用例；全部**零成本、无网络、可重复**。

### 2.4 CI 设计（`.github/workflows/ci.yml`）

**分两档——这是刻意的：必需档零成本且无需密钥，可选档才花钱。**

| Job | 触发 | 内容 | 成本 |
|---|---|---|---|
| `lint-test`（必需） | push / PR | `ruff check` → `ruff format --check` → `mypy`（限定范围）→ `pytest -q` | 0 |
| `selfcheck`（必需） | push / PR | 起 `uvicorn`（`mock` 后端、无密钥）→ `smoke_test.py --strict`（HTTP 层真跑）→ `security_check.py` → `mcp_check.py` → `app.rag.pipeline eval` 与 `eval/baseline.json` 比对 | 0 |
| `eval-live`（可选） | 手动 / 有 secret 时 | `smoke_test.py --full` + `scripts/run_eval.py` 端到端 + 基线 diff，报告作为 artifact 上传 | 约 ¥0.55 |

> **为什么必需档不跑 `--full` 和端到端评测**：那需要 API Key（会花钱、且 fork 的 PR 拿不到 secret）。
> 零成本档已经能覆盖"代码被改坏"的绝大多数情况；花钱的验证放在可选档。

**基线门禁**：`eval/baseline.json` 记录 `hybrid` 检索 top-k 召回等零成本指标，
CI 中下降超过 **5 个百分点**即失败（阈值先宽松，避免噪声导致假红）。

### 2.5 门禁自证（这一步不能省）

新增一条**验证门禁本身有效**的测试：
故意破坏一处白名单规则（例如把 `rm` 加进表），CI **必须变红**。
做完后恢复。否则我们只是"加了一堆绿灯"，并不能证明它拦得住东西。

### 2.6 提交计划（每个 commit 独立可回滚）

| # | commit | 内容 |
|---|---|---|
| 1 | `chore: 处理工作区遗留的 README 排版改动` | 先让工作区干净（见 §4 待确认项） |
| 2 | `fix(smoke): 区分「环境未就绪」与「未加 --full」两类跳过，新增 --strict` | I-0 |
| 3 | `test: 引入 pytest 骨架与 6 个核心模块测试` | I-1 主体 |
| 4 | `test: 接口层 TestClient 冒烟 + 鉴权边界` | `test_api_smoke.py` |
| 5 | `chore(ci): 新增 ruff/mypy 配置与 GitHub Actions 两档流水线` | 工具链 + CI |
| 6 | `docs: 同步自检口径与测试说明（README / overview / 新增 tests 说明）` | 文档 |

### 2.7 验收标准（DoD）

1. `pytest -q` 全绿，约 70–90 条用例，覆盖上表 8 个文件
2. `scripts/smoke_test.py --strict`：服务未启动 → 退出码 2；服务在跑 → 退出码 0
3. `security_check.py` 23 项全通（**不改动它的断言**，确保没有为了变绿而放松检查）
4. **门禁自证**：故意改坏一条白名单规则 → CI 变红（截图/日志留证）
5. `README.md` 的自检表述与实测一致；新增 `tests/README` 或 README 小节说明怎么跑测试
6. **兼容性**：`requirements.txt` 仍是 9 个依赖；既有 31 路由契约不变；`smoke_test.py` 默认行为不变

---

## 3. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 测试写多了拖慢节奏 | 只覆盖"坏了会**静默**出错"的 6 个模块 + 接口边界，不做无脑覆盖率 |
| 测试依赖真实索引文件（`data/index/`） | `data/index/` 被 gitignore；测试用 `skipif` 或现场重建（`chunk_documents` 是纯函数，可零成本构造） |
| `mypy` 在无类型标注的老代码上噪声大 | 只对新模块与 `tests/` 开检查，老代码进 `ignore` 列表，逐里程碑扩大 |
| CI 里起 `uvicorn` 不稳定 | 只起本地回环 + 健康检查轮询重试；失败时打印服务日志作为 artifact |
| 门禁变红影响你现在的开发习惯 | 先在 `push` 上启用，`PR` 必检可选；阈值宽松起步 |

---

## 4. 开工前需要你拍板的一件事

工作区当前有**未提交的 `README.md` 改动**（`git status`：`M README.md`，186 insertions / 201 deletions）。
从 diff 看是把徽章链接改成了纯图片、并在 NOTE 块后插入空行——**像是被某个格式化工具改过**，
其中"徽章不再可点击"可能并非你的本意。

三个选项：

| 选项 | 说明 |
|---|---|
| **A（建议）** | 先原样提交为 `chore:` 一笔（保留现状，随时可回退），后续改动与它分离 |
| B | `git checkout -- README.md` 丢弃该改动，回到上次提交的门面 |
| C | 暂不处理，我后续所有提交都绕开 `README.md`（会在文档同步时冲突，不推荐） |

---

## 5. 本里程碑不做什么

- 不做 I-2/I-3（事件、通知、聚合）——那是 M2，等 M1 验收后再出方案
- 不修执行面缺陷（PATH 劫持、跨进程双执行等）——那是 M3
- 不重构 `main.py`、不新增运行时依赖、不改任何既有接口契约
