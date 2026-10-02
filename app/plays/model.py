# -*- coding: utf-8 -*-
"""处置剧本：把"一份写死的命令清单"升级成**有判定与分叉的流程**。

【它解决什么问题】
原来告警给出的"处置预案"是 `DIAGNOSE_PLAYBOOK = {服务名: [命令名, ...]}` ——
一堆只读命令的名字。它没有判定、没有分叉、不知道"做到哪一步算好"，
**更没有回滚**：真出事的时候，"要不要回退、怎么回退"只能现场想。

【它**不做**什么（这条比上面那条重要）】
**不做自动执行，也不做自动回滚。** 项目的核心边界是"写操作永不自动执行"，
剧本不碰这条边界：它只是把"该怎么处置"结构化、可 review、可审计，
每一步**照样走既有的 policy 准入与审批单**。

【为什么回滚必须**事前声明**】
执行到一半才发现"这一步退不回去"，是最坏的时机。
所以校验规则是硬性的：每个 action 步骤必须写清 `rollback`，
**写不出回滚就必须显式声明 `none` 并给出理由** ——
"不可回滚"本身是合法答案，但**必须被执行前的人看见**，
而不是留到事后现场发挥。

【为什么剧本用 JSON 而不是 YAML】
PyYAML **不是**本项目的声明依赖（它是 `uvicorn[standard]` 顺带装进来的）。
依赖一个"别人的可选依赖"，正是这个项目一直避免的隐性耦合：
哪天上游把 extra 拆了，剧本就在这里静默坏掉。
JSON 是 stdlib，而且项目里运维可编辑的配置本来就都是 JSON
（`env/alert_silences.json`）—— 一致性也在这里。

【流程语义（三条，写清楚免得各自理解）】
    ① 有 `on_pass`/`on_fail` 就必须有 `expect` —— **分叉必须有判定**。
       没有判定的分叉等于"看心情走哪条"，那种剧本不如顺序清单。
    ② 没有分叉 = **顺序执行下一步**。信息性探针（`systemctl status` 这类
       只给人看细节的命令）就属于这种，不该为了"看起来像流程图"硬编一个判定。
    ③ 有 `expect` 但没有分叉也是合法的：判定结论会被记录下来给人看，
       流程照常往下走（很多检查是"记下来供人判断"，不是自动开关）。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

TOP_KEYS = {"name", "title", "description", "applies_to", "steps"}
STEP_KEYS = {"id", "kind", "run", "expect", "on_pass", "on_fail",
             "rollback", "note", "timeout"}
ROLLBACK_KEYS = {"run", "none", "reason"}


class PlayError(ValueError):
    """剧本不合法。**加载时就抛** —— 详见 validate 里的说明。"""


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Step:
    id: str
    kind: str                       # "probe"（只读自动跑）| "action"（要审批）
    run: str
    expect: str = ""
    on_pass: str = ""
    on_fail: str = ""
    rollback: dict = field(default_factory=dict)
    note: str = ""
    timeout: int = 0
    # 加载时由 policy 判定的结论，缓存下来给接口/渲染用（不参与相等性）
    verdict: str = ""               # allow / needs_approval
    rule_key: str = ""
    rollback_verdict: str = ""

    @property
    def is_action(self) -> bool:
        return self.kind == "action"


@dataclass
class Play:
    name: str
    title: str
    steps: list
    description: str = ""
    applies_to: list = field(default_factory=list)
    source: str = ""

    def probe_steps(self) -> list:
        return [s for s in self.steps if not s.is_action]

    def action_steps(self) -> list:
        return [s for s in self.steps if s.is_action]

    def step(self, step_id: str):
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


# ============================================================
# 校验
# ============================================================
def validate(raw: dict, *, source: str = "") -> Play:
    """把一份原始 dict 校验成 Play。**任何问题都抛 PlayError。**

    ★ 为什么是"加载即失败"而不是"运行到那一步再失败"：
      剧本是给**半夜被叫起来的人**用的。如果一条命令不在白名单里，
      我们要他在凌晨三点、故障正发生的时候才发现，还是现在（写剧本的时候）就发现？
      所以坏剧本**根本不该被加载** —— 它宁可在启动时报错，
      也不要在一半步骤执行完之后卡住。
    """
    from app.sandbox import policy

    if not isinstance(raw, dict):
        raise PlayError(f"剧本必须是 JSON 对象，收到 {type(raw).__name__}")

    # V9：未知字段直接报错。剧本里的拼写错误是**危险**的 ——
    #      `on_passs` 写错会让一个分叉**静默失效**，而剧本看起来完全正常。
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise PlayError(f"剧本里有未知字段：{sorted(unknown)}"
                        f"（拼错的分叉名会静默失效，所以这里必须报错）")

    name = str(raw.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise PlayError(f"剧本 name 不合法：{name!r}（应为小写字母/数字/连字符，≤32）")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise PlayError(f"剧本 {name} 缺少 title")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlayError(f"剧本 {name} 的 steps 必须是非空数组")

    steps = []
    seen = set()
    for i, item in enumerate(steps_raw):
        step = _validate_step(name, i, item, policy, seen)
        steps.append(step)

    # V8：至少要有一个只读探针 —— 只有动作的"剧本"其实是脚本，不是处置流程
    if not any(not s.is_action for s in steps):
        raise PlayError(f"剧本 {name} 没有任何只读探针（probe）："
                        f"只有动作的剧本不是处置流程，是脚本")

    _validate_links(name, steps)          # V6 + V10
    _validate_acyclic(name, steps)        # V7

    return Play(name=name, title=title, steps=steps,
                description=str(raw.get("description") or ""),
                applies_to=list(raw.get("applies_to") or []),
                source=source)


def _validate_step(play_name: str, index: int, item, policy, seen: set) -> Step:
    where = f"剧本 {play_name} 的第 {index + 1} 个步骤"
    if not isinstance(item, dict):
        raise PlayError(f"{where} 必须是 JSON 对象")

    unknown = set(item) - STEP_KEYS
    if unknown:
        raise PlayError(f"{where} 有未知字段：{sorted(unknown)}")

    step_id = str(item.get("id") or "").strip()
    if not STEP_ID_RE.match(step_id):
        raise PlayError(f"{where} 的 id 不合法：{step_id!r}")
    if step_id in seen:
        raise PlayError(f"{where} 的 id 重复：{step_id!r}（分叉会指错步骤）")
    seen.add(step_id)

    kind = str(item.get("kind") or "").strip()
    if kind not in ("probe", "action"):
        raise PlayError(f"{where}（{step_id}）的 kind 必须是 probe 或 action，"
                        f"收到 {kind!r}")

    run = str(item.get("run") or "").strip()
    if not run:
        raise PlayError(f"{where}（{step_id}）缺少 run")

    decision = policy.decide(run)
    if decision.decision == "deny":
        raise PlayError(
            f"{where}（{step_id}）的命令不在允许范围内：{run!r} —— {decision.reason}。"
            f"剧本只能使用 policy 白名单里的命令")

    if kind == "probe" and decision.decision != "allow":
        raise PlayError(
            f"{where}（{step_id}）标成 probe 但它不是只读的：{run!r}"
            f"（policy 判定 {decision.decision}）。"
            f"probe 必须能自动执行，所以只能是 policy 允许的只读命令")

    rollback = item.get("rollback") or {}
    if kind == "action":
        rollback = _validate_rollback(play_name, step_id, rollback, policy)
    elif rollback:
        raise PlayError(f"{where}（{step_id}）是只读探针，不该有 rollback")

    expect = str(item.get("expect") or "").strip()
    on_pass = str(item.get("on_pass") or "").strip()
    on_fail = str(item.get("on_fail") or "").strip()
    # V10：有分叉就必须有判定 —— 没有判定的分叉是"看心情走哪条"
    if (on_pass or on_fail) and not expect:
        raise PlayError(f"{where}（{step_id}）有分叉却没有 expect："
                        f"分叉必须写明『满足什么条件算通过』")

    return Step(id=step_id, kind=kind, run=run, expect=expect,
                on_pass=on_pass, on_fail=on_fail, rollback=rollback,
                note=str(item.get("note") or ""),
                timeout=int(item.get("timeout") or 0),
                verdict=decision.decision, rule_key=decision.rule_key,
                rollback_verdict=(policy.decide(rollback["run"]).decision
                                  if rollback.get("run") else ""))


def _validate_rollback(play_name: str, step_id: str, rollback, policy) -> dict:
    where = f"剧本 {play_name} 的步骤 {step_id}"
    if not isinstance(rollback, dict) or not rollback:
        raise PlayError(
            f"{where} 是写操作但没有声明 rollback。"
            f"要么写清反向步骤，要么显式写 "
            f'{{"none": true, "reason": "为什么退不回去"}} —— '
            f'"不可回滚"是合法答案，但必须**执行前**就让人看见')

    unknown = set(rollback) - ROLLBACK_KEYS
    if unknown:
        raise PlayError(f"{where} 的 rollback 有未知字段：{sorted(unknown)}")

    if rollback.get("none"):
        reason = str(rollback.get("reason") or "").strip()
        if not reason:
            raise PlayError(f"{where} 声明了不可回滚却没写 reason"
                            f"（这条理由正是要给人看的）")
        if rollback.get("run"):
            raise PlayError(f"{where} 的 rollback 不能同时写 none 和 run")
        return {"none": True, "reason": reason}

    run = str(rollback.get("run") or "").strip()
    if not run:
        raise PlayError(f"{where} 的 rollback 缺少 run（或改为 {{\"none\": true}}）")
    decision = policy.decide(run)
    if decision.decision == "deny":
        raise PlayError(f"{where} 的回滚命令不在允许范围内：{run!r} —— "
                        f"{decision.reason}")
    return {"run": run, "reason": str(rollback.get("reason") or "")}


def _validate_links(play_name: str, steps: list) -> None:
    ids = {s.id for s in steps}
    for step in steps:
        for attr in ("on_pass", "on_fail"):
            target = getattr(step, attr)
            if target and target not in ids:
                raise PlayError(f"剧本 {play_name} 的步骤 {step.id} 的 {attr} "
                                f"指向不存在的步骤：{target!r}")


def _validate_acyclic(play_name: str, steps: list) -> None:
    """V7：分叉不能成环。

    死循环的处置剧本意味着**值班的人被卡住**（走到 A 让你去 B，到 B 又让你回 A）。
    宁可加载时就拒绝，也不要让它在凌晨三点转圈。
    """
    graph = {s.id: [t for t in (s.on_pass, s.on_fail) if t] for s in steps}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in graph}

    def visit(node, path):
        color[node] = GREY
        for nxt in graph.get(node, []):
            if color[nxt] == GREY:
                raise PlayError(f"剧本 {play_name} 的分叉成环："
                                f"{' → '.join(path + [node, nxt])}")
            if color[nxt] == WHITE:
                visit(nxt, path + [node])
        color[node] = BLACK

    for sid in list(graph):
        if color[sid] == WHITE:
            visit(sid, [])


# ============================================================
# 渲染（给 /plays/{name} 与文档用）
# ============================================================
def render(play: Play) -> str:
    """渲染成人类可读的处置流程。**只描述，不执行。**"""
    lines = [f"# {play.title}（{play.name}）"]
    if play.description:
        lines.append(play.description)
    lines.append("")
    for i, step in enumerate(play.steps, 1):
        tag = "只读" if not step.is_action else "写操作（需审批）"
        lines.append(f"{i}. [{tag}] {step.id}")
        lines.append(f"   $ {step.run}")
        if step.expect:
            lines.append(f"   判定：{step.expect}")
        if step.on_pass:
            lines.append(f"   通过 → {step.on_pass}")
        if step.on_fail:
            lines.append(f"   不通过 → {step.on_fail}")
        if step.is_action:
            if step.rollback.get("none"):
                lines.append(f"   ⚠️ 不可回滚：{step.rollback['reason']}")
            else:
                lines.append(f"   回滚：{step.rollback['run']}")
        if step.note:
            lines.append(f"   备注：{step.note}")
    return "\n".join(lines)


def load_file(path: Path) -> Play:
    """从 JSON 文件加载并校验。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlayError(f"{path.name} 不是合法 JSON：{e}") from e
    except OSError as e:                              # pragma: no cover
        raise PlayError(f"读不到 {path.name}：{e}") from e
    return validate(raw, source=path.name)
