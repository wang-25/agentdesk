# 处置剧本（Plays）

> M5 · I-9。把告警给出的"处置预案"从**一份写死的命令清单**升级成
> **有判定、有分叉、有回滚声明的流程**。

---

## 1. 它不做什么（先说这条）

**不自动执行，也不自动回滚。**

项目的核心边界是「写操作永不自动执行」，剧本不碰这条边界：
它只把处置流程结构化、在**加载时**校验每一句话是否站得住、
并把"这一步退不回去"摆在执行**之前**。每一步照样走既有的 policy 准入与审批单。

有一条用例专门守着这件事：`tests/test_plays.py::test_plays_module_never_executes_anything`
—— 剧本模块里一旦出现 `subprocess` / `executor` / `app.tools`，用例就红。

---

## 2. 一份剧本长什么样

`plays/disk-full.json`（节选）：

```json
{
  "name": "disk-full",
  "title": "磁盘写满（No space left on device）",
  "applies_to": ["DiskSpaceLow", "NoSpaceLeft"],
  "steps": [
    {
      "id": "check_usage", "kind": "probe", "run": "df -h",
      "expect": "有分区的已用比例 ≥ 90%",
      "on_pass": "find_big_dir", "on_fail": "check_deleted"
    },
    {
      "id": "propose_truncate", "kind": "action",
      "run": "truncate -s 0 /var/log/nginx/error.log",
      "rollback": { "none": true, "reason": "被截断的日志内容无法恢复……" }
    }
  ]
}
```

字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` / `title` | ✅ | 剧本名（小写+连字符）与标题 |
| `description` / `applies_to` | | 说明；以及它适配哪些告警名 |
| `steps[]` | ✅ | 至少一步，且**至少要有一个只读探针** |
| `step.id` | ✅ | 唯一，分叉按它引用 |
| `step.kind` | ✅ | `probe`（只读，可自动跑）/ `action`（写操作，要审批） |
| `step.run` | ✅ | 命令原文（**必须命中 policy 白名单**） |
| `step.expect` | | 判定条件（人读的，例如"使用率降到 80% 以下"） |
| `step.on_pass` / `on_fail` | | 分叉目标；**写了分叉就必须有 `expect`** |
| `step.rollback` | action 必填 | `{"run": "反向命令"}` 或 `{"none": true, "reason": "为什么退不回去"}` |
| `step.note` / `step.timeout` | | 备注；超时秒数 |

### 流程语义（三条）

1. **有分叉就必须有判定**：`on_pass`/`on_fail` 与 `expect` 必须同时出现。
   没有判定的分叉等于"看心情走哪条"。
2. **没有分叉 = 顺序执行下一步**。信息性探针（`systemctl status` 这类
   只给人看细节的命令）就属于这种，不该为了"看起来像流程图"硬编判定。
3. **有 `expect` 但没有分叉也合法**：判定结论会被记下来给人看，流程照常往下走。

---

## 3. 加载时校验哪些东西（这是它最有价值的部分）

| 规则 | 为什么 |
|---|---|
| 命令必须命中 **policy 白名单** | 否则剧本会承诺一件系统自己会拒绝的事 |
| `probe` 必须是 policy 认定的**只读**命令 | 标成 probe 却要走审批 = 剧本与真实行为不一致，比没有剧本更危险 |
| **回滚命令同样过白名单** | 否则"回滚"成了绕过闸门的通道 |
| action 必须声明 `rollback` 或 `none`+`reason` | "退不回去"是合法答案，但**必须执行前就让人看见** |
| 分叉目标必须存在 / **不能成环** | 死循环的剧本意味着值班的人被卡住（A 让你去 B，B 让你回 A） |
| 未知字段直接报错 | `on_passs` 这种拼写错误会让分叉**静默失效**，而剧本看起来完全正常 |
| 至少一个只读探针 | 只有动作的"剧本"其实是脚本 |
| **坏剧本让整个加载失败**（不跳过） | 跳过意味着真出事时"剧本列表里少了一条"，而人不会有时间想为什么少了 |
| 空目录不是错误，目录里有坏剧本才是 | 还没写剧本是合法状态 |

---

## 4. 怎么加一份剧本

1. 在 `plays/` 新建 `<name>.json`
2. 命令**先在白名单里查一遍**：

```powershell
.venv\Scripts\python.exe -c "from app.sandbox import policy; print(policy.decide('df -h').decision)"
```

3. 跑一次校验（坏剧本会当场报错并说清第几步、为什么）：

```powershell
.venv\Scripts\python.exe -c "from app.plays import store; print(store.counts())"
```

4. 看渲染结果（这就是接口会返回给人的东西）：

```powershell
.venv\Scripts\python.exe -c "from app.plays import store, render; print(render(store.get('disk-full')))"
```

---

## 5. 已知边界

- **当前白名单里的写操作都没有真正的反向命令**（没有 `systemctl start`、没有 `docker start`），
  所以现有剧本的回滚都是 `none` + 理由。带 `run` 回滚的那条路是为将来的可逆动作
  （例如"改配置 → 改回去"）留的 —— 有一条用例**记录**这个现状，等白名单里出现
  可逆动作时会变红，提醒回来补一份带真回滚的剧本。
- 剧本用 **JSON 不是 YAML**：PyYAML 是本项目声明依赖之外的东西
  （`uvicorn[standard]` 顺带装的）。依赖"别人的可选依赖"正是这个项目要避免的隐性耦合；
  而项目里运维可编辑的配置本来就是 JSON（`env/alert_silences.json`）。
- 剧本只描述**处置流程**，不负责"什么时候该走哪份剧本"——
  那由告警名（`applies_to`）与人的判断决定。
- **校验是静态的**：它保证命令在允许范围内、结构自洽，
  但保证不了"这份处置在业务上是对的"。后者只能靠人 review —— 所以剧本进 git、可 diff。

---

## 6. 顺便修掉的一个真缺陷

写这份校验时，它立刻抓出了**原有告警预案里的 4 条越界命令**：

| 原预案里的命令 | policy 判定 | 出现在哪 |
|---|---|---|
| `systemctl --failed` | **deny** | `DEFAULT_PLAYBOOK`（默认预案，所有告警都会展示） |
| `ss -lntp \| grep :80` | **deny** | `DIAGNOSE_PLAYBOOK["nginx"]` |
| `tail -100 /var/log/nginx/error.log` | **deny**（只允许 `tail -n 100` 写法） | `DIAGNOSE_PLAYBOOK["nginx"]` / `["mysql"]` |
| `docker logs --tail 100 <container>` | 占位符 `<container>`，不是可执行命令 | `DIAGNOSE_PLAYBOOK["docker"]` |

也就是说：**告警预案里展示给人看的命令，有一部分是系统自己会拒绝的。**
以前没人发现，因为预案只是一段文本、从不经过策略校验。
结构化剧本的第一个作用就是把这个不一致暴露出来。
