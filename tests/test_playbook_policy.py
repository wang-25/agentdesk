# -*- coding: utf-8 -*-
"""告警预案里的命令**必须**是 policy 允许的。

这个文件是 M5 的一个副产品，但它守的问题比它看起来重要：

原来 `DIAGNOSE_PLAYBOOK` / `DEFAULT_PLAYBOOK` 里有 4 条命令是系统自己会拒绝的 ——
包括**默认预案里的 `systemctl --failed`**，而默认预案是"所有告警都会展示给值班人"
的那一份。以前没人发现，因为这些清单**只是一段展示给人看的文本，
从来不经过任何策略校验**。

于是就有了一个荒谬的处境：系统对值班的人说"你去执行这个"，
而它自己在同一套策略下会拒绝这个命令。这类不一致最坏的地方在于
**它不会报错** —— 只会让人在故障中多绕一圈。

修法有两步（M5 都做了）：
  ① 把清单改成白名单内的命令；
  ② 加这个文件，逐条喂给 policy —— 再出现越界命令当场变红。
"""

import pytest

import app.main as m
from app.sandbox import policy


def _all_playbook_commands():
    for service, cmds in m.DIAGNOSE_PLAYBOOK.items():
        for cmd in cmds:
            yield f"DIAGNOSE_PLAYBOOK[{service!r}]", cmd
    for cmd in m.DEFAULT_PLAYBOOK:
        yield "DEFAULT_PLAYBOOK", cmd


def test_playbook_is_not_empty():
    """清单本身不能是空的 —— 空清单会让"已生成处置预案"变成一句空话。"""
    assert m.DEFAULT_PLAYBOOK
    assert m.DIAGNOSE_PLAYBOOK
    for service, cmds in m.DIAGNOSE_PLAYBOOK.items():
        assert cmds, f"{service} 的预案是空的"


@pytest.mark.parametrize("where,cmd", list(_all_playbook_commands()))
def test_playbook_command_is_not_denied_by_policy(where, cmd):
    """★ 逐条检查：**系统不能一边让人执行、一边自己拒绝**。"""
    decision = policy.decide(cmd)
    assert decision.decision != "deny", (
        f"{where} 里的 {cmd!r} 会被策略拒绝：{decision.reason}。"
        f"预案展示给人看的命令必须是白名单内的")


@pytest.mark.parametrize("where,cmd", list(_all_playbook_commands()))
def test_playbook_command_has_no_placeholder(where, cmd):
    """★ 预案是给人**照着执行**的，所以不能含未替换的占位符。

    原 `docker` 预案里的 `docker logs --tail 100 <container>` 就是这种：
    它展示出来以后，值班的人还得自己想"container 该填什么" ——
    而那一刻最不该让人动脑子的地方就是命令本身。
    """
    assert "<" not in cmd and ">" not in cmd, \
        f"{where} 里的 {cmd!r} 含占位符，展示出来没法直接执行"


def test_playbook_commands_are_readonly_by_design():
    """预案是**只读**排查清单。写操作只能出现在结构化剧本里并带审批与回滚声明。

    这条守的是"预案"这个词的含义：它是让你先看清楚，不是让你直接动手。
    """
    for where, cmd in _all_playbook_commands():
        decision = policy.decide(cmd)
        assert decision.decision == "allow", (
            f"{where} 里的 {cmd!r} 不是只读命令（policy 判定 {decision.decision}）—— "
            f"写操作必须走剧本 + 审批，不能混进只读预案")
        assert decision.risk == "readonly", \
            f"{where} 里的 {cmd!r} 风险等级是 {decision.risk}，不是只读"


def test_the_regression_that_motivated_this_file():
    """★ 把当年那 4 条越界命令固定成反例。

    如果哪天有人"顺手"把 `systemctl --failed` 加回默认预案，
    上面那条参数化用例会红；这条用例则负责说明**为什么**它不能加回来。
    """
    for cmd in ("systemctl --failed", "ss -lntp", "tail -100 /var/log/nginx/error.log"):
        assert policy.decide(cmd).decision == "deny", \
            f"{cmd!r} 现在居然是允许的 —— 策略放宽了，请重新评估这几条预案"
