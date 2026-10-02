# M3 实施方案（I-4 安全执行面收口）

> 对应路线图 [`02-roadmap.md`](02-roadmap.md) 的 M3；修复审计报告 **C1–C8、C11** 与 M2 期间登记的 **M2-1**。
>
> **状态：待用户确认后开工**（Rules：先评估后动手）。

---

## 0. 这一里程碑要达成的一句话目标

> **让"看起来有防护"变成"真的有防护"，并且把该留的痕留全。**

M1/M2 把"能不能用"补齐了；M3 处理的是**安全边界上那几条真实缺陷**。
它们的共同特征是同一句话：**代码看起来拦住了，但在某条路径上其实没拦住**。

审计已确认的清单（每条都有 `文件:行号` 证据）：

| 编号 | 缺陷 | 当前的"看起来"与"实际" |
|---|---|---|
| C1 | **PATH 劫持面** | 看起来：白名单逐条比命令名。实际：执行时继承 `PATH`，谁能控制 `PATH` 就能用一个假 `systemctl` 绕过**整张表** |
| C2 | **审批单可跨进程双执行** | 看起来：状态机只允许 `APPROVED → CONSUMED` 走一次。实际：状态只在**进程内存**里、启动时折叠一次，无文件锁/CAS → 两个进程各持一张 APPROVED，同一条写命令执行两次 |
| C3 | **批准后无执行时限** | 看起来：审批单有 30 分钟 TTL。实际：过期只处理 `pending`，`execute` 不校验 `expires_at` → 三天前批的单今天仍能执行 |
| C4 | **审批记录错报成功** | 看起来：`approvals.jsonl` 有 `result_ok`。实际：它是在**执行之前**写下的（有意取舍：防重放），真值只在 `audit.jsonl` 与 span 里 |
| C5 | **读路径与注释矛盾** | 注释：只允许读 `.log` 结尾。代码：只校验目录前缀 → `/var/log/secure`、`wtmp`、`btmp`、`audit/*` 都在可读范围内 |
| C6 | **超时不回收** | 看起来：命令有超时。实际：`TimeoutExpired` 只杀 docker CLI 或直接子进程，**容器与孙进程继续跑** |
| C7 | **写操作走 host 通道、零隔离** | 文档讲"写操作在一次性容器里执行"，实际覆盖的只有 `truncate` 一类；`systemctl restart`/`docker restart` 以 API 进程权限直接执行 |
| C8 | **审计链缺口** | policy DENY、开票、失败尝试、只读 `run_command` 都不写 `audit.jsonl`（只在 trace 里）→ "谁试图做什么被拒"查不到 |
| C11 | **两条旁路未经统一收口** | `ops.py` 的 `journalctl` / `docker logs` 不走 `policy` / `executor` |
| M2-1 | **`MAX_RECORDS = 500` 是死常量** | 注释声称"内存里最多保留多少条（防止日志无限增长拖慢启动）"，全仓零引用 → 审批单内存实际无界增长 |

---

## 1. 设计原则（先立规矩）

| # | 原则 | 含义 |
|---|---|---|
| 1 | **不放宽任何白名单** | 本里程碑只收紧、不改宽。凡是要放宽的（如 C5 的日志文件名）单独列出让用户拍板 |
| 2 | **每条新拒绝规则配一条正例** | 项目已有教训：白名单太窄会把 Agent 卡在"查不到"上（`policy.py:248-259`）。**加固不能变成"什么都不能做"** |
| 3 | **同一份判定只有一处** | 拒绝理由与判定来源继续收敛在 `policy.py`；执行面只负责"照办"与"如实记录" |
| 4 | **不动 fail-closed 哲学** | 沙箱不可用仍然拒绝执行，不降级 |
| 5 | **跨平台** | 开发在 Windows、部署在 Linux。文件锁不能用 `fcntl`（Windows 没有），也不引入新依赖（`requirements.txt` 必须保持 9 个） |

---

## 2. 逐条修法

### C1 · PATH 劫持（`policy.py` 的 `bin` 解析 + `executor.py`）

**问题**：白名单只比裸名、显式拒绝带 `/` 的名字，执行时靠继承的 `PATH` 找程序。

**修法**：
1. 新增 `resolve_binary(bin_name) -> str`：在**固定的候选目录**里找（`/usr/bin`、`/bin`、`/usr/sbin`、`/sbin`、`/usr/local/bin`），**不接受 `PATH` 里的其它目录**；
2. 执行时用解析出的**绝对路径**，并把子进程环境的最小化：`PATH` 固定为上面的候选目录拼接（去掉继承值）、清掉 `LD_PRELOAD`/`LD_LIBRARY_PATH`/`BASH_ENV` 等可注入变量；
3. 找不到绝对路径 → `DENY`（理由写清"这台机器上没有这个命令"），**不退回裸名**。

**验收**：把 `PATH` 指向一个含假 `df` 的目录，`decide + executor` 仍必须执行真 `df`（或在找不到时拒绝），且**绝不执行假的那个**。

### C2 · 跨进程双执行（`approvals.py`）

**问题**：状态只在进程内存；两个进程各自折叠出同一张 APPROVED，各执行一次。

**修法**（不引入新依赖）：
1. **跨平台文件锁**：`_lock_file()` 用 `os.open(path + ".lock", O_CREAT|O_EXCL|O_WRONLY)` 自旋获取（带超时与**陈旧锁清理**：锁文件里写 pid+时间戳，超时未释放则视为陈旧并接管）。
   *为什么不直接 flock：Windows 没有 `fcntl`；`msvcrt.locking` 语义窄且只在 Windows 有。`O_EXCL` 两边都成立。*
2. **每次迁移前重新折叠**：`consume`/`approve` 在持锁期间**重读日志尾部**确认状态（而不是只信启动时折出来的内存），拿到锁再判状态 —— 这是 CAS 语义的关键：判定与写入必须在同一个锁里。
3. **原子写 + fsync**：`_append` 写完 `flush()` + `os.fsync()`，避免"返回成功但没落盘"。

**验收**：用两个**真进程**（`subprocess` 起两个 Python，各自 import 同一个 store 并抢同一张单）验证只有一方成功消费，另一方拿到"已执行过"的错误；坏行/截断日志不会让锁泄漏。

### C3 · 批准后的执行时限（`main.py` 的 execute + `approvals.py`）

**修法**：`execute` 前除了指纹比对，再校验 `expires_at`：过期 → 拒绝执行（409），并把该单折叠成 `expired`（留审计 `approval.execute_blocked`，理由是"过期"）。

**取舍**：这是**收紧**现有行为（原先过期的 approved 单仍可执行），所以要单独写进文档与用例。
**验收**：造一张 `expires_at` 已过期的 approved 单 → execute 返回 409 且状态变 `expired`、命令**未被执行**。

### C4 · 审批记录如实回写（`approvals.py` + `main.py`）

**修法**：**保持"先消费再执行"的顺序**（那是防重放的正确取舍），执行完成后追加一条 `executed` 事件，把真实的 `ok`/`exit_code`/`elapsed_ms`/`error` 回写进记录；折叠时以最后一条为准。

**验收**：执行失败（用一个必失败的命令，如 `truncate` 一个不存在的目录）→ `approvals.jsonl` 折出来的 `result_ok=False`，且与 `audit.jsonl` 的 `approval.executed.ok` **一致**（这正是 M1 报告里 C4 指出的不一致）。

### C5 · 读路径：**需要你拍板**（`policy.py`）

现状：注释说"只允许读 `.log` 结尾"，代码只校验目录前缀。

三种选法：

| 选项 | 行为 | 代价 |
|---|---|---|
| **A（建议）** | 收紧到 `.log`，**并显式放行常用无扩展名日志**：`syslog`、`messages`、`kern.log`、`daemon.log`、`auth.log`、`nginx/access.log` 等；`secure`/`wtmp`/`btmp`/`audit/*` **不给** | 与注释一致，权限最小；但将来要读新文件名得改白名单 |
| B | 保持现状（`/var/log` 下任意文件可读），把注释改成实话 | 零改动风险；但 `secure`/`wtmp` 这类认证与审计日志也在可读范围 |
| C | 严格 `.log` 结尾 | 最严；但 `tail /var/log/syslog` 会被拒——**这是最常用的排障命令之一**，会明显卡住诊断 |

### C6 · 超时回收（`executor.py`）

**修法**：
1. 主机通道：`subprocess.Popen(..., start_new_session=True)`，超时后 `os.killpg(os.getpgid(pid), SIGKILL)` 杀**整个进程组**（Windows 用 `taskkill /T /F`）；
2. 容器通道：`docker run --cidfile <tmp>`，超时后先 `docker rm -f $(cat cidfile)` 再杀 CLI；
3. 无论成败都清理临时 cidfile。

**验收**：跑一条"起子进程再睡 60s"的命令并把超时设成 2s → 断言超时后**没有任何残留进程/容器**（用 `ps`/`docker ps` 佐证）。

### C7 · 主机通道的隔离现状（文档 + 显式可见）

`systemctl restart` / `docker restart` **必须在主机上执行**才能生效，这不是能"修"的缺陷 ——
能修的是**别让文档说出与实际不符的话**：

1. `describe()` 里已有的 `container_channel` / `host_channel` 如实呈现（已有）；
2. `/sandbox` 响应里明确标出"这条命令走主机通道，**没有容器隔离**"；
3. README 与 docs 里"写操作在一次性容器里执行"改成准确表述：**能进容器的进容器（文件类操作），必须在主机上执行的（服务重启类）走主机通道 + 审批 + 审计**；
4. 对走主机通道的命令，审批通知里显式带上"无容器隔离"。

### C8/C11 · 统一收口与审计补齐

1. `ops.py` 的 `journalctl` / `docker logs` 改为经 `policy.decide` + `executor` 执行（它们当前是直接 `subprocess`/`ssh`）；
2. 审计补齐以下事件（现在只有 trace）：`policy.denied`（谁试图跑什么被拒、理由）、`approval.created`（开票）、`approval.execute_blocked`（过期/指纹不符/未批准）、只读 `run_command` 的调用。

### M2-1 · `MAX_RECORDS` 死常量

**修法**：二选一并在注释里写清为什么 ——
（建议）**删掉常量，改成如实的注释**：审批单内存是 O(全部历史)，真正的有界性在告警去重表上；
运维侧靠轮转 `logs/approvals.jsonl`（`logs/` 已有 compose 层 10m×3 的日志轮转，但 JSONL 不归它管，需要补一条说明或脚本）。
**不做**：给审批单加"只保留最近 N 条"的内存上限 —— 审批是审计物，截断会让历史单查不到。

---

## 3. 测试计划（沿用 M1/M2 的装置）

| 文件 | 覆盖 |
|---|---|
| `tests/test_policy_hardening.py` | 二进制解析固定到候选目录、`PATH` 注入无效、环境变量清洗；每条新拒绝规则的正例（**能用的仍然能用**）；读路径白名单（按选定方案） |
| `tests/test_approvals_concurrency.py` | 两个**真进程**抢同一张单；锁超时与陈旧锁接管；原子写 + fsync；执行后真实结果回写 |
| `tests/test_executor_reclaim.py` | 超时回收进程组/容器（用打桩的 `Popen`/`docker` 调用序列断言，不真起容器） |
| `tests/test_api_smoke.py`（扩展） | execute 对过期单返回 409；`/sandbox` 标出主机通道无隔离 |

**对抗性用例（每条都要"被拦 + 留痕"）**：PATH 劫持、过期单执行、跨进程双执行、重复执行、越权读、`LD_PRELOAD` 注入、超时残留。

---

## 4. 提交计划

| # | commit | 内容 |
|---|---|---|
| 1 | `fix(policy): 命令解析固定到候选目录 + 执行环境清洗（消 PATH 劫持面）` | C1 |
| 2 | `fix(approvals): 跨进程文件锁 + 每次迁移前重折叠 + fsync（消双执行）` | C2 |
| 3 | `fix(approvals): 执行前校验过期 + 执行后回写真实结果` | C3+C4 |
| 4 | `fix(sandbox): 超时回收进程组与容器 + 只读命令统一收口 + 审计补齐` | C6/C8/C11 |
| 5 | `docs: 主机通道无容器隔离这件事说清楚（含 /sandbox 与通知）` | C7 |
| 6 | `docs+chore: 读路径按方案收紧 / MAX_RECORDS 死常量清理 + 报告同步` | C5/M2-1 |

---

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 加固导致误拒，Agent 卡在"查不到" | 每条新拒绝规则配正例；先用**当前真实用到的命令**跑一遍回归，确认没有被误伤 |
| 文件锁在异常退出时泄漏 | 锁文件写 pid + 时间戳，超时视为陈旧并接管；用例覆盖"持有者被杀" |
| `start_new_session` 影响容器内行为 | 只在主机通道使用；容器通道用 cidfile + `docker rm -f` |
| 收紧执行时限影响现有手工流程 | 明确写进文档与 CHANGELOG 段落；`expires_at` 到期前来不及批就重新开票（这正是它该有的摩擦） |
| 跨平台差异 | 锁用 `O_EXCL`；杀进程组分平台（POSIX `killpg` / Windows `taskkill /T`），两条路径都有用例 |

---

## 6. 本里程碑不做什么

- **不做**远程（ssh 后端）下的写操作执行通道 —— 保持 fail-closed（"诊断在远端、执行在本地"的错配比不能执行更危险）
- **不做**命令白名单的扩容（那是 I-9 处置剧本的事，且要单独评估）
- **不做** RBAC / 多租户（审计里已判定为"先补单令牌的自批自执漏洞，而不是先堆权限系统"）
- **不改**审批单的终态语义（`consumed` 绝对不可逆）

---

## 7. 需要你拍板的两件事

| # | 问题 | 选项 |
|---|---|---|
| 1 | 读路径（C5）怎么收 | **A（建议）** 收紧到 `.log` + 显式放行 `syslog`/`messages`/`kern.log`/`auth.log` 等常用无扩展名日志；`secure`/`wtmp`/`btmp`/`audit/*` 不给 · B 保持现状只改注释 · C 严格 `.log` 结尾（会拒 `tail /var/log/syslog`） |
| 2 | 过期审批单能否执行（C3） | **A（建议）** 拒绝执行并标记 `expired`，需要时重新开票 · B 允许执行但强制写一条"超期执行"审计 |
