# -*- coding: utf-8 -*-
"""结构化处置剧本（M5 · I-9）。

这个文件守的核心只有一句话：
**剧本只能包含 policy 允许的命令，而且坏剧本必须在"加载时"就失败。**

为什么这条最重要：剧本是给**半夜被叫起来的人**用的。
如果一条不在白名单里的命令要等到运行到第 4 步才发现，
那时故障正在发生、前面三步已经执行、人已经在紧张状态 ——
最坏的结果是有人"手工绕过"它。所以校验必须前移到加载期。

还有一条同样重要、但容易被忽略：
**"这一步退不回去"必须被执行前的人看见**，而不是留到事后现场发挥。
"""

import json

import pytest

from app.plays import model as m
from app.plays import store as st


def _play(**over):
    """一份最小的合法剧本（一个只读探针）。"""
    raw = {
        "name": "t",
        "title": "测试剧本",
        "steps": [{"id": "p1", "kind": "probe", "run": "df -h"}],
    }
    raw.update(over)
    return raw


def _step(**over):
    base = {"id": "s1", "kind": "probe", "run": "df -h"}
    base.update(over)
    return base


# ============================================================
# 一、最小合法剧本与内置剧本
# ============================================================
def test_minimal_play_loads():
    play = m.validate(_play())
    assert play.name == "t" and len(play.steps) == 1
    assert play.steps[0].verdict == "allow"
    assert play.steps[0].rule_key == "df"


def test_builtin_plays_all_load():
    """仓库里自带的剧本必须全都是合法的 —— 它们就是这份校验的第一个用户。"""
    plays = st.load_all(force=True)
    assert set(plays) >= {"disk-full", "service-down", "high-load"}
    for play in plays.values():
        assert play.steps, f"{play.name} 没有步骤"
        assert play.probe_steps(), f"{play.name} 没有只读探针"


def test_builtin_counts_are_reported():
    got = st.counts()
    assert got["plays"] >= 3 and got["steps"] > 0
    assert got["actions"] >= 2, "至少要有两个写操作步骤（重启 / 清理）"
    assert got["irreversible"] >= 1, "磁盘清理那条是不可回滚的，应当被统计到"


def test_every_action_step_declares_rollback_or_none():
    """★ 全局不变量：任何写操作都不能"没想过回滚"。"""
    for play in st.load_all(force=True).values():
        for step in play.action_steps():
            assert step.rollback, \
                f"{play.name}.{step.id} 是写操作却没有 rollback 声明"


def test_irreversible_steps_must_say_why():
    """声明"不可回滚"时，理由必须写出来 —— 那就是给人看的那句话。"""
    for play in st.load_all(force=True).values():
        for step in play.action_steps():
            if step.rollback.get("none"):
                assert step.rollback["reason"].strip(), \
                    f"{play.name}.{step.id} 说了不可回滚却没给理由"


# ============================================================
# 二、★ 白名单：加载即失败
# ============================================================
def test_denied_command_fails_at_load_time():
    """★ 这是这个模块存在的意义：坏命令**现在**就报，不是凌晨三点。

    `rm` 被 policy 明确禁止（删除不可恢复）。剧本里出现它 → 加载就该失败。
    """
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(run="rm -rf /var/log/nginx")]))
    assert "不在允许范围内" in str(e.value)
    assert "rm" in str(e.value)


def test_probe_must_be_genuinely_readonly():
    """标成 probe 的命令必须是 policy 认为"可以直接跑"的只读命令。

    否则剧本会承诺"这一步会自动执行"，而实际执行时被审批拦住 ——
    剧本与真实行为不一致，比没有剧本更危险。
    """
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(kind="probe", run="systemctl restart nginx")]))
    assert "不是只读的" in str(e.value)


def test_action_may_need_approval_but_not_be_denied():
    play = m.validate(_play(steps=[
        _step(id="p1", run="df -h"),
        _step(id="a1", kind="action", run="systemctl restart nginx",
              rollback={"none": True, "reason": "重启无法回退会话"}),
    ]))
    action = play.step("a1")
    assert action.verdict == "needs_approval" and action.is_action


def test_rollback_command_is_also_checked_against_policy():
    """回滚命令同样要过白名单 —— 否则"回滚"本身成了绕过闸门的通道。"""
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[
            _step(id="p1"),
            _step(id="a1", kind="action", run="systemctl restart nginx",
                  rollback={"run": "rm -rf /"}),
        ]))
    assert "回滚命令不在允许范围内" in str(e.value)


# ============================================================
# 三、回滚声明
# ============================================================
def test_action_without_rollback_is_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[
            _step(id="p1"),
            _step(id="a1", kind="action", run="systemctl restart nginx"),
        ]))
    assert "没有声明 rollback" in str(e.value)


def test_none_rollback_requires_a_reason():
    """★ "不可回滚"是合法答案，但**必须给理由**。

    理由不是走形式：它就是执行前要摆在操作人眼前的那句话。
    """
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[
            _step(id="p1"),
            _step(id="a1", kind="action", run="truncate -s 0 /var/log/nginx/error.log",
                  rollback={"none": True}),
        ]))
    assert "没写 reason" in str(e.value)


def test_none_and_run_cannot_be_combined():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[
            _step(id="p1"),
            _step(id="a1", kind="action", run="systemctl restart nginx",
                  rollback={"none": True, "reason": "r", "run": "systemctl start nginx"}),
        ]))
    assert "不能同时写" in str(e.value)


def test_probe_with_rollback_is_rejected():
    """只读步骤不该有回滚 —— 那是概念混淆，早报早好。"""
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(rollback={"run": "df -h"})]))
    assert "不该有 rollback" in str(e.value)


# ============================================================
# 四、结构校验
# ============================================================
@pytest.mark.parametrize("bad_name", ["", "T", "有中文", "a" * 33, "-lead", "has space"])
def test_invalid_play_names(bad_name):
    with pytest.raises(m.PlayError):
        m.validate(_play(name=bad_name))


def test_title_is_required():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(title=""))
    assert "title" in str(e.value)


def test_steps_must_be_a_non_empty_list():
    with pytest.raises(m.PlayError):
        m.validate(_play(steps=[]))
    with pytest.raises(m.PlayError):
        m.validate(_play(steps={"a": 1}))


def test_duplicate_step_ids_are_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(), _step()]))
    assert "重复" in str(e.value)


def test_unknown_top_level_field_is_rejected():
    """★ 未知字段必须报错：`on_passs` 这种拼写错误会让分叉**静默失效**，
    而剧本看起来完全正常 —— 那是最难查的一类问题。"""
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(stepz=[]))
    assert "未知字段" in str(e.value)


def test_unknown_step_field_is_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(on_passs="x")]))
    assert "未知字段" in str(e.value)


def test_invalid_kind_is_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(kind="maybe")]))
    assert "kind" in str(e.value)


def test_missing_run_is_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[{"id": "p1", "kind": "probe"}]))
    assert "缺少 run" in str(e.value)


def test_dangling_branch_target_is_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(expect="x", on_pass="nope")]))
    assert "指向不存在" in str(e.value)


def test_branch_without_expect_is_rejected():
    """没有判定的分叉 = "看心情走哪条"。"""
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(on_pass=None, on_fail=None, expect="")] if False
                         else [_step(on_pass="x2"), _step(id="x2")]))
    assert "expect" in str(e.value)


def test_play_without_probe_is_rejected():
    """只有动作的"剧本"其实是脚本，不是处置流程。"""
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[
            _step(kind="action", run="systemctl restart nginx",
                  rollback={"none": True, "reason": "r"}),
        ]))
    assert "只读探针" in str(e.value)


# ============================================================
# 五、分叉成环
# ============================================================
def test_cycle_is_rejected():
    """★ 死循环的处置剧本意味着**值班的人被卡住**（A 让你去 B，B 让你回 A）。

    宁可加载时拒绝，也不要让它在凌晨三点转圈。
    """
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[
            _step(id="a", expect="x", on_pass="b"),
            _step(id="b", expect="y", on_pass="a"),
        ]))
    assert "成环" in str(e.value)


def test_self_loop_is_rejected():
    with pytest.raises(m.PlayError) as e:
        m.validate(_play(steps=[_step(id="a", expect="x", on_pass="a")]))
    assert "成环" in str(e.value)


def test_diamond_shape_is_allowed():
    """菱形（两条路汇到同一个验证步）是正常结构，不能被环检测误伤。"""
    play = m.validate(_play(steps=[
        _step(id="a", expect="x", on_pass="b", on_fail="c"),
        _step(id="b", expect="y", on_pass="d"),
        _step(id="c", expect="z", on_pass="d"),
        _step(id="d"),
    ]))
    assert len(play.steps) == 4


# ============================================================
# 六、加载目录：坏剧本必须让整个加载失败
# ============================================================
def test_load_all_reads_valid_files(tmp_path):
    (tmp_path / "ok.json").write_text(json.dumps(_play(), ensure_ascii=False),
                                      encoding="utf-8")
    plays = st.load_all(tmp_path, force=True)
    assert list(plays) == ["t"]


def test_a_broken_play_fails_the_whole_load(tmp_path):
    """★ 不"跳过坏的那一份"。

    挑最坏的情形：磁盘满的剧本里有一条打错字的回滚命令。
    跳过它意味着真出事时**剧本列表里少了那一条**，而人不会有时间想为什么少了。
    启动时报错是当时就能修好的故障。
    """
    (tmp_path / "ok.json").write_text(json.dumps(_play(), ensure_ascii=False),
                                      encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps(_play(name="bad", steps=[
        _step(run="rm -rf /")]), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(m.PlayError) as e:
        st.load_all(tmp_path, force=True)
    assert "bad.json" in str(e.value) and "不在允许范围内" in str(e.value)


def test_duplicate_play_names_across_files_are_rejected(tmp_path):
    for fn in ("a.json", "b.json"):
        (tmp_path / fn).write_text(json.dumps(_play(), ensure_ascii=False),
                                   encoding="utf-8")
    with pytest.raises(m.PlayError) as e:
        st.load_all(tmp_path, force=True)
    assert "重复" in str(e.value)


def test_invalid_json_is_reported_with_the_file_name(tmp_path):
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(m.PlayError) as e:
        st.load_all(tmp_path, force=True)
    assert "broken.json" in str(e.value)


def test_missing_directory_is_not_an_error(tmp_path):
    """还没写剧本是合法状态 —— 但**目录里有坏剧本**不是。这个区别要守住。"""
    assert st.load_all(tmp_path / "nope", force=True) == {}


def test_empty_directory_loads_nothing(tmp_path):
    assert st.load_all(tmp_path, force=True) == {}


def test_get_unknown_play_raises_with_the_available_names(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYS_DIR", str(tmp_path))
    st.reset()
    with pytest.raises(m.PlayError) as e:
        st.get("nope")
    assert "没有名为" in str(e.value)
    st.reset()


# ============================================================
# 七、渲染：只描述，不执行
# ============================================================
def test_render_marks_readonly_and_action_steps():
    play = m.validate(_play(name="demo", title="演示", steps=[
        _step(id="p1", run="df -h", expect="有分区 ≥90%", on_pass="a1"),
        _step(id="a1", kind="action", run="systemctl restart nginx",
              rollback={"none": True, "reason": "重启不可回退"}),
    ]))
    text = m.render(play)
    assert "只读" in text and "写操作（需审批）" in text
    assert "判定：有分区 ≥90%" in text
    assert "通过 → a1" in text
    assert "不可回滚：重启不可回退" in text


def test_render_shows_rollback_command():
    """渲染器只负责"把剧本画出来"，所以这里直接构造对象、不走校验。

    ★ 为什么不拿一份真剧本测：当前 policy 白名单里的写操作（restart / truncate）
      **都没有真正的反向命令**（没有 systemctl start、没有 docker start），
      所以现有剧本全都声明 `none` + 理由。带 `run` 回滚的那条路是为将来的
      可逆动作（例如"改配置 → 改回来"）留的，用构造的对象测它最干净。
    """
    play = m.Play(name="demo", title="演示", steps=[
        m.Step(id="p1", kind="probe", run="df -h"),
        m.Step(id="a1", kind="action", run="apply-new-config",
               rollback={"run": "restore-old-config", "reason": "改回去"}),
    ])
    text = m.render(play)
    assert "回滚：restore-old-config" in text
    assert "不可回滚" not in text


def test_none_rollback_is_rendered_as_a_warning():
    play = m.Play(name="demo", title="演示", steps=[
        m.Step(id="a1", kind="action", run="truncate -s 0 /x",
               rollback={"none": True, "reason": "内容不可恢复"}),
    ])
    assert "⚠️ 不可回滚：内容不可恢复" in m.render(play)


def test_render_of_builtin_play_is_readable():
    text = m.render(st.get("disk-full"))
    assert "磁盘写满" in text and "df -h" in text and "不可回滚" in text


# ============================================================
# 八、★ 边界：剧本不会自己执行任何东西
# ============================================================
def test_plays_module_never_executes_anything():
    """★ 这条用例守的是**项目的核心边界**（写操作永不自动执行）。

    剧本只做三件事：结构化、加载时校验、把"退不回去"摆到人眼前。
    它**不许** import executor / subprocess / tools ——
    一旦有人图省事让剧本"自动跑一下第一步"，这条用例会红。
    """
    from pathlib import Path

    import app.plays.model as model_mod
    import app.plays.store as store_mod

    forbidden = ("subprocess", "executor", "app.tools", "os.system", "popen")
    for mod in (model_mod, store_mod):
        src = Path(mod.__file__ or "").read_text(encoding="utf-8")
        for bad in forbidden:
            assert bad not in src, \
                f"{mod.__name__} 里出现了 {bad!r} —— 剧本不该能执行任何东西"


def test_play_verdicts_are_recorded_for_audit():
    """每一步的 policy 判定结论要留下来，接口与审计才能如实展示。"""
    play = st.get("service-down")
    action = play.step("approve_restart")
    assert action.verdict == "needs_approval"
    assert action.rule_key == "systemctl.restart"
    # 现状：白名单里的写操作都没有反向命令，所以是 none + 理由
    assert action.rollback.get("none") is True
    assert action.rollback_verdict == "", "没有 run 的回滚不该有判定结论"


def test_rollback_verdict_is_recorded_when_a_rollback_command_exists():
    """带 `run` 的回滚也要记下 policy 判定 —— 接口得能告诉人
    "回滚这一步同样需要审批"，而不是让人以为回滚是免费的。"""
    play = m.validate(_play(steps=[
        _step(id="p1"),
        _step(id="a1", kind="action", run="systemctl restart nginx",
              rollback={"run": "truncate -s 0 /var/log/nginx/error.log"}),
    ]))
    assert play.step("a1").rollback_verdict == "needs_approval"


# ============================================================
# 九、接口接线（GET /plays、GET /plays/{name}）
# ============================================================
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import app.main as main_mod

    with TestClient(main_mod.app) as c:
        yield c


def test_list_plays_endpoint(client):
    body = client.get("/plays").json()
    assert body["total"] >= 3
    names = {i["name"] for i in body["items"]}
    assert {"disk-full", "service-down", "high-load"} <= names
    # ★ 不可回滚的步骤数要单独列出来 —— 这是决策时最该看到的一个数
    for item in body["items"]:
        assert "irreversible" in item and item["irreversible"] <= item["actions"]


def test_read_play_endpoint_exposes_policy_verdicts(client):
    body = client.get("/plays/service-down").json()
    steps = {s["id"]: s for s in body["steps"]}
    assert steps["check_active"]["auto_run"] is True
    assert steps["check_active"]["policy"]["verdict"] == "allow"
    assert steps["approve_restart"]["auto_run"] is False
    assert steps["approve_restart"]["policy"]["verdict"] == "needs_approval"
    assert steps["approve_restart"]["rollback"]["none"] is True
    assert "plan" in body and "只读" in body["plan"]


def test_read_play_endpoint_404s_with_the_available_names(client):
    r = client.get("/plays/does-not-exist")
    assert r.status_code == 404
    assert "没有名为" in r.json()["detail"]


def test_plays_endpoints_say_they_do_not_execute(client):
    """★ 接口自己要把边界说清楚：这是描述，不是执行入口。"""
    for path in ("/plays", "/plays/disk-full"):
        assert "不自动执行" in client.get(path).json()["note"]


def test_broken_play_refuses_to_start_the_service(tmp_path, monkeypatch):
    """★ 坏剧本让服务**拒绝启动**（而不是带着它跑起来）。

    这是设计选择：剧本的错误形态是静默的 —— 一条越界命令要等执行到第 4 步
    才发现，一个拼错的分叉名会让某条分支永远走不到。
    "启动时报错、说清第几步为什么"是当时就能修好的故障。
    """
    (tmp_path / "bad.json").write_text(json.dumps(
        {"name": "bad", "title": "坏的",
         "steps": [{"id": "s1", "kind": "probe", "run": "rm -rf /"}]},
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PLAYS_DIR", str(tmp_path))
    st.reset()
    with pytest.raises(m.PlayError):
        st.load_all()
    st.reset()


def test_current_whitelist_has_no_true_inverse_for_its_actions():
    """★ 一条**记录现状**的用例，而不是断言某个行为。

    现在 policy 允许的写操作只有 restart / truncate 两类，
    它们的"反向命令"（systemctl start、docker start）不在白名单里 ——
    所以所有剧本的回滚都只能是 `none` + 理由。
    等哪天加了可逆动作（改配置 → 改回来），这条用例会红，
    提醒我们回来补一个"真回滚"的剧本和用例。
    """
    verdicts = set()
    for play in st.load_all(force=True).values():
        for step in play.action_steps():
            verdicts.add("run" if step.rollback.get("run") else "none")
    assert verdicts == {"none"}, (
        f"白名单里似乎有了可逆动作（回滚形态：{verdicts}）—— "
        f"请补一份带真回滚的剧本，并把这条用例改成断言那份剧本存在")
