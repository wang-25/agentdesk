# -*- coding: utf-8 -*-
"""Day 8 补丁 2：把新踩的坑与真实 Docker 验证结果补进文档。"""
from pathlib import Path

hits = []


def rep(p, old, new, label):
    s = p.read_text(encoding="utf-8").replace("\r", "")
    assert old in s, f"[{p.name}] 锚点找不到：{label}"
    assert s.count(old) == 1, f"[{p.name}] 锚点不唯一：{label}"
    p.write_text(s.replace(old, new, 1), encoding="utf-8", newline="")
    hits.append(label)


# ================= observability.md：补三类新坑 =================
p = Path("docs/observability.md")
rep(p, "**二、断言绑定了后端实现细节**",
    """**二、成本归因连踩三个版本（每个版本都是一次对账教训）**

「成本按 Agent 分布」看着简单，实际做了三版才对：

| 版本 | 写法 | 后果 |
|---|---|---|
| v1 | `set_usage(out["usage"])` | **覆盖语义**，把子 span 归并上来的用量清零 → 看板显示"这个 Agent 不花钱" |
| v2 | `add_usage(out["usage"])` | 累加，但 `out["usage"]` 是**引擎自报的整轮汇总**，和子 span 归并值重复 → 成本翻倍 |
| v3 | 什么都不加，只靠子 span 归并 | 叶子 span 是唯一事实源，**节点合计 = 动作合计 = 总成本** ✓ |

> **同一个量只能有一个事实源。** v2 的错误很典型：两个来源（传播 + 引擎自报）
> 都是"对的"，加起来就是错的。而对账（三者是否一致）是唯一能发现它的手段 ——
> 不报错、不越界，只是数字大了一倍。

**二·补、`int + dict`：同一个坑，本项目第二次踩**

归并 v3 之后仍有问题：节点用量里**有 prompt/completion/total，却没有缓存命中字段**。
根因是手写累加 `parent.usage[k] = parent.usage.get(k, 0) + v` 遇到
DeepSeek usage 里的 `prompt_tokens_details`（嵌套 dict）→ `0 + dict` 抛 TypeError
→ 被 `except Exception: pass` 吞掉 → **归并半途中断**。

dict 键顺序救了前三个字段、坑了后面两个，所以表现是"数据看起来是有的"——
节点成本按"全部未命中"算，**数字虚高（实测 43%）**。

> ① **累加前必须过滤非数值字段** —— 外部返回的 usage 里混嵌套结构是常态
> ② **吞异常的代码会吞掉"半成品状态"** —— 丢一半比丢整条更难发现
> ③ 这个坑在 Day 4 的 `_merge_usage` 里已经踩过一次并写进了注释，
>    **说明"写下来"不等于"不会再犯"，要有断言守着**（现已固化为自检项）

**三、断言绑定了后端实现细节**""", "观测补三类坑")

rep(p, "**二、（真实收获）Permission denied 恰好是安全设计的证明**",
    "**四、（真实收获）Permission denied 恰好是安全设计的证明**", "坑编号")

# ================= sandbox-hitl.md：补真实 Docker 验证 =================
p = Path("docs/sandbox-hitl.md")
rep(p, "**七层自检**：沙箱 5/5、审批 3/3，其余层全绿。",
    """**★ 装上 Docker 之后的真实验证（这是最终结论）**

沙箱隔离参数逐项实测：

| 隔离项 | 验证方式 | 结果 |
|---|---|---|
| 非 root | `id -u` | `65534` ✓ |
| 根文件系统只读 | `touch /etc/x` | `Read-only file system` ✓ |
| tmpfs 可写 | `touch /tmp/x` | 成功 ✓ |
| 无网络 | `ping 1.1.1.1` | `Network unreachable` ✓ |
| 内存封顶 | 读 cgroup `memory.max` | `134217728`（128MB）✓ |

完整 HITL 端到端（真实容器）：

```
造真实日志文件（2 行）
  → Agent 提交 run_command truncate -s 0 /var/log/nginx/access.log
  → 决策 needs_approval，生成审批单，**不执行**
  → 人工批准（by=ops-drill）
  → 指纹比对一致 → 一次性容器执行（isolated=True, backend=docker）
  → 验证文件：0 字节 ✓
```

**过程中被拦下一次 Permission denied**（root 属主文件 + 容器以 nobody 运行）——
这不是故障，是"非 root 运行"在做它该做的事，也顺手演示了真实生产里
最常见的权限问题。修好属主后同一条链路完整跑通。

**八层自检**：沙箱 5/5、审批 3/3、观测 9/9，其余层全绿。""", "沙箱补真实验证")

print(f"✅ {len(hits)} 处锚点命中")
for h in hits:
    print("   ·", h)
