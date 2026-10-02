# -*- coding: utf-8 -*-
"""审计链缺口（C8）与两条绕过准入的旁路（C11）。

这个文件守的是两类**事后才会发现**的问题，所以每条用例都要能回答
"如果它坏了，谁会先注意到"：

  C8 审计缺口 —— 三类事件原来只进 trace、没进 audit.jsonl。
     它们共同点很刺眼：**都不会产生任何副作用**。
     策略拒绝了什么、有人在等哪张审批单、某次只读执行以什么退出码结束 ——
     坏了不会有异常、不会有报错页，只会让"事后追责"这个能力
     在某一天突然失效，而且没人知道是哪天失效的。

  C11 旁路 —— `_run_logs` 自己拼 `journalctl` / `docker logs` 直接执行。
     这类问题的危害不在"多执行了一条命令"（那两条本来就是只读的），
     而在**白名单与实际能力对不上**：`policy.catalog()` 说没有 `docker logs`
     这条规则，而代码里它一直在跑。规则表于是成了一份不完整的说明书 ——
     改策略的人会以为自己收紧了，其实没有。

★ 全部零成本：不联网（conftest 有守卫）、不调模型、不起 docker、
  不真读日志。涉及执行的地方一律打桩。
"""

import pytest

from app.sandbox import executor, policy
from app.sandbox.approvals import ApprovalStore
from app.tools import ops

DOCKER_LOGS = "docker logs --tail 100 wp-app"


# ============================================================
# 装置
# ============================================================
class ExecRecorder:
    """记录 `executor.run` 收到的 Decision，并返回可编排的结果。

    刻意**不真的执行任何东西** —— 这一层不该在单元测试里碰系统。
    """

    def __init__(self):
        self.decisions = []
        self.results = []

    def push(self, result):
        self.results.append(result)
        return self

    def __call__(self, decision):
        self.decisions.append(decision)
        assert self.results, "执行器被调用了，但测试没有编排返回值"
        return self.results.pop(0)

    @property
    def last(self):
        assert self.decisions, "executor.run 从未被调用"
        return self.decisions[-1]


@pytest.fixture
def local_backend(monkeypatch):
    """把工具层切到本机后端。

    ★ 必须显式切：`.env` 里当前配的是 `OPS_BACKEND=ssh`，
      而 ssh 后端下 `run_command` 会在**任何策略判定之前**直接返回
      "不提供执行能力"（fail-closed，见 ops.py 的闸门零）。
      不切的话这个文件里所有 run_command 用例都会拿到那条早退分支 ——
      它们会"通过"，但什么也没验证到。
    """
    monkeypatch.setattr(ops, "BACKEND", "local")


@pytest.fixture
def sandbox(local_backend, monkeypatch):
    """打桩：本机日志后端 + 沙箱执行器。

    `active_backend` 必须一起打桩：本机装没装 Docker 会改变它的返回值，
    而测试要验证的是"命令经不经 executor 出去"，**不该依赖 CI 上有没有 docker**。
    """
    rec = ExecRecorder()
    monkeypatch.setattr(executor, "active_backend", lambda: "subprocess")
    monkeypatch.setattr(executor, "run", rec)
    return rec


def make_result(*, ok=True, exit_code=0, stdout="", stderr="", error=""):
    """造一个 `ExecResult`（真类型，但没人真的执行它）。"""
    return executor.ExecResult(
        ok=ok, backend="subprocess", isolated=False, command="x",
        exit_code=exit_code, stdout=stdout, stderr=stderr, error=error,
    )


# ============================================================
# 一、C8：三类事件必须进审计
# ============================================================
@pytest.fixture
def audits(monkeypatch):
    """注入一个记录型审计 hook，并把模块状态恢复干净。

    ★ 必须显式恢复：`set_audit_hook` 改的是模块级全局，漏恢复的话
      会污染同进程里后面的用例（而"审计多写了一条"这种错**不会报错**）。
    """
    events = []

    def recorder(event, detail):
        events.append((event, dict(detail)))

    monkeypatch.setattr(ops, "_audit_hook", recorder)
    return events


def _only(events, name):
    got = [d for e, d in events if e == name]
    assert len(got) == 1, f"期望恰好一条 {name}，实际 {[e for e, _ in events]}"
    return got[0]


def test_policy_deny_is_audited(audits, local_backend, trace_file):
    """被策略拒绝的命令必须留痕 —— 它**不产生任何副作用**。

    换句话说：不写审计，"模型半夜反复试探 `rm -rf /`"这件事
    在任何地方都查不到。这是审计链上最危险的一个洞。
    """
    out = ops.run_command("rm -rf /")

    assert out["decision"] == policy.DENY
    detail = _only(audits, "policy.denied")
    assert detail["command"] == "rm -rf /"
    assert detail["risk"] == "" or detail["risk"]      # deny 决策没有风险档
    assert detail["reason"], "拒绝理由必须写进审计，否则事后只看到'被拒了'"
    assert "rm" in detail["reason"]
    assert "rule" in detail                           # 字段在，便于按规则聚合


def test_approval_request_is_audited(audits, local_backend, monkeypatch,
                                     trace_file):
    """开票要留痕：审批单是'有人被卡住等确认'的那条记录。"""
    store = ApprovalStore(path=trace_file.parent / "approvals.jsonl")
    monkeypatch.setattr("app.sandbox.approvals.store", lambda: store)
    # 通知是另一条链路，这里不该真的出去（events 内部会兜底，但别依赖它）
    monkeypatch.setattr("app.notify.events.approval_created", lambda rec: None)

    out = ops.run_command("systemctl restart nginx")

    assert out["decision"] == policy.NEEDS_APPROVAL
    assert out["executed"] is False
    detail = _only(audits, "approval.requested")
    assert detail["approval_id"] == out["approval_id"]
    assert detail["command"] == "systemctl restart nginx"
    assert detail["rule"] == "systemctl.restart"
    assert detail["risk"] == "reversible"
    assert detail["isolation"] == policy.CHANNEL_HOST
    # expires_at 必须写：单子会静默超时失效，事后要能看出"当时还有效吗"
    assert detail["expires_at"] == out["expires_at"]


def test_readonly_execution_is_audited(audits, sandbox):
    """只读执行也要留痕 —— 它是真实碰到过生产机的动作。

    "只读"不等于"不用记账"：诊断结论必须能追回到"哪次命令、什么退出码"。
    """
    sandbox.push(make_result(stdout="Filesystem Use%\n/dev/vda1 96% /\n"))

    out = ops.run_command("df -h")

    assert out["executed"] is True
    assert out["result"]["ok"] is True
    detail = _only(audits, "run_command.readonly")
    assert detail["command"] == "df -h"
    assert detail["rule"] == "df"
    assert detail["exit_code"] == 0
    assert detail["ok"] is True


def test_readonly_audit_records_failure_too(audits, sandbox):
    """失败也要留痕：`ok=False` 与退出码正是排查时要看的。"""
    sandbox.push(make_result(ok=False, exit_code=1, error="命令以退出码 1 结束"))

    out = ops.run_command("df -h")

    assert out["executed"] is True
    detail = _only(audits, "run_command.readonly")
    assert detail["ok"] is False
    assert detail["exit_code"] == 1


def test_deny_still_returns_the_same_error_semantics(audits, local_backend,
                                                    trace_file):
    """加审计**不能改动返回语义** —— 拒绝仍要原样把理由交给模型。"""
    out = ops.run_command("rm -rf /")
    assert out["executed"] is False
    assert out["error"] and "rm" in out["error"]
    assert out["hint"]
    assert "result" not in out


# ---- 没有 hook / hook 自己炸了：绝不能影响工具调用 ----
def test_no_audit_hook_is_silent(monkeypatch, sandbox):
    """没有 hook 时静默跳过：单独跑工具 / 自检脚本时就是这个场景。"""
    monkeypatch.setattr(ops, "_audit_hook", None)
    sandbox.push(make_result(stdout="ok"))

    assert ops.run_command("df -h")["executed"] is True
    assert ops.run_command("rm -rf /")["decision"] == policy.DENY


def test_audit_hook_exception_does_not_break_the_tool(monkeypatch, sandbox):
    """★ 审计写不进去，不能影响工具调用结果。

    审计是旁路：磁盘满、权限不对都可能让写审计失败，
    而那一刻命令**已经执行完了**。让"记账失败"把一次成功的执行
    变成异常返回，等于把旁路故障升级成主链路故障。
    """
    def boom(event, detail):
        raise OSError("审计盘写不进去了")

    monkeypatch.setattr(ops, "_audit_hook", boom)
    sandbox.push(make_result(stdout="Filesystem Use%\n/dev/vda1 96% /\n"))

    out = ops.run_command("df -h")
    assert out["executed"] is True and out["result"]["ok"] is True

    denied = ops.run_command("rm -rf /")
    assert denied["decision"] == policy.DENY and denied["error"]


# ============================================================
# 二、C11：journalctl / docker logs 必须经统一准入执行
# ============================================================
def test_journalctl_goes_through_policy_and_executor(sandbox, monkeypatch):
    """systemd 那条来源原来直接 `_exec` 自己拼命令 —— 现在必须过准入。"""
    monkeypatch.setattr(ops, "_exists", lambda host, path: False)
    sandbox.push(make_result(
        stdout="-- Logs begin at Mon 2026-09-01\n"
               "Sep 26 09:13:02 web-01 nginx[1183]: no space left on device\n"))

    got = ops.tail_log("web-01", "nginx", 20)

    assert got["source"] == "journalctl -u nginx"
    assert got["count"] == 1, "`-- Logs begin at` 是 journalctl 的表头，不是日志内容"
    # 命中的规则是白名单里那条 journalctl，argv 由 policy 规范化而来
    assert sandbox.last.rule_key == "journalctl"
    assert sandbox.last.argv == ["journalctl", "-u", "nginx", "-n", "20"]
    assert sandbox.last.decision == policy.ALLOW


def test_docker_logs_goes_through_policy_and_executor(sandbox, monkeypatch):
    """容器那条来源原来拼 `sh -c ... 2>&1` 直接执行 —— 现在必须过准入。

    ★ `sh` 恰恰是 BLOCKED_BINARIES 里明令禁止的可执行文件，
      而它就躺在这条"合法"的读取路径上。
    """
    # systemd 与日志文件两条来源都不存在 → 必然落到第三条（容器日志）
    monkeypatch.setattr(ops, "_exists", lambda host, path: False)
    sandbox.push(make_result(ok=False, exit_code=1, error="No entries"))
    sandbox.push(make_result(stdout="容器正常输出行", stderr="容器 stderr 里的报错"))

    got = ops.tail_log("web-01", "wp-app", 20)

    assert got["source"] == "docker logs wp-app"
    assert sandbox.last.rule_key == "docker.logs"
    assert sandbox.last.argv == ["docker", "logs", "--tail", "20", "wp-app"]
    assert sandbox.last.decision == policy.ALLOW
    # 两股流都要在：容器里有用的报错走的是 docker 自己的 stderr
    # （原来靠 shell 的 `2>&1` 合，现在由工具层合 —— 能力不能丢）
    assert any("正常输出" in ln for ln in got["lines"])
    assert any("stderr" in ln for ln in got["lines"])


def test_log_read_falls_back_from_journald_to_container(sandbox, monkeypatch):
    """三条来源的**回退链**必须完整保留 —— 收口不能把后两条吃掉。"""
    monkeypatch.setattr(ops, "_exists", lambda host, path: False)
    sandbox.push(make_result(ok=False, exit_code=1, error="No entries"))
    sandbox.push(make_result(stdout="container log line\n"))

    got = ops.tail_log("web-01", "wp-app", 20)

    assert [d.rule_key for d in sandbox.decisions] == ["journalctl", "docker.logs"], \
        "顺序必须是 systemd → 日志文件 → 容器，且都要过准入"
    assert got["source"] == "docker logs wp-app"


def test_denied_log_command_never_reaches_executor(sandbox, monkeypatch):
    """准入的意义在于"拒绝就是拒绝"——被拒的命令不能有任何执行动作。"""
    monkeypatch.setattr(ops, "_exists", lambda host, path: False)
    bad = {"n": 0}

    def spy(command, host):
        bad["n"] += 1
        return 0, ""

    monkeypatch.setattr(ops, "_exec", spy)
    with pytest.raises(ops.ToolError):
        ops._exec_through_policy("docker logs -f wp-app", "web-01")
    assert bad["n"] == 0, "被策略拒绝的命令仍然被执行了"


def test_service_name_length_matches_the_container_limit(monkeypatch):
    """★ 服务名上限跟下游对齐（64，与 docker 容器名一致），不是 32。

    `tail_log` 会用服务名去找**同名容器**，也会把它当 systemd 单元名 ——
    而 systemd 单元名常常超过 32 个字符
    （`systemd-networkd-wait-online.service` 正好 35）。
    原先 33–64 的名字在工具层就被拒了，表现成"这个服务查不到"，
    真正的原因（名字太长）谁也看不出来。

    字符集（防注入的那道墙）一个字没动，所以注入面没变。
    """
    long_unit = "systemd-networkd-wait-online.service"      # 35 字符
    assert len(long_unit) > 32
    assert ops._check_service(long_unit) == long_unit

    # 边界两侧
    assert ops._check_service("a" * 64) == "a" * 64
    with pytest.raises(ops.ToolError):
        ops._check_service("a" * 65)

    # 字符集仍然拦得住注入（这才是安全边界）
    for bad in ("nginx; rm -rf /", "nginx && id", "nginx`id`", "nginx|x", "a b"):
        with pytest.raises(ops.ToolError):
            ops._check_service(bad)


def test_tail_log_return_shape_is_unchanged(sandbox, monkeypatch):
    """收口不能改动 `tail_log` 的返回结构（下游 Agent / MCP 都按它解析）。"""
    monkeypatch.setattr(ops, "_exists", lambda host, path: False)
    sandbox.push(make_result(stdout="line one\nline two\nline three\n"))

    got = ops.tail_log("web-01", "wp-app", 2)

    assert set(got) == {"host", "service", "backend", "source", "count",
                        "lines", "matched_patterns", "hint"}
    assert got["host"] == "web-01" and got["service"] == "wp-app"
    assert got["count"] == len(got["lines"]) == 2, "lines 上限仍然生效"


# ============================================================
# 二·B、ssh 路径：`--no-pager` 只属于 journalctl
# ============================================================
def test_no_pager_is_only_added_for_journalctl(monkeypatch):
    """★ 这条是**真机上抓到的回退**的回归用例。

    `--no-pager` 是 journalctl 的参数（不加它会去调分页器，非交互 SSH 里
    表现为"命令卡住"）。但 docker **不认识**它 —— 一旦加在共用的 ssh 分支上，
    `docker logs --tail 15 wp-app --no-pager` 会直接报错，
    而 `tail_log` 把"非零退出"当成"这条来源没有日志"，
    于是表现成"容器明明在跑却取不到日志"。

    实测现场：web-01 上的 `wp-app` 容器（wordpress）取不到日志，
    而同一台机器上 `docker ps` 明明列着它。
    两个命令共用一个分支，所以参数必须按**命中的规则**分别拼。

    注意这条用例本身跟 ssh 无关 —— 它断言的是"拼出来的 argv"，
    所以打桩 `_exec` 就够，不需要真机。
    """
    captured = {}

    def spy(argv, host):
        captured["argv"] = list(argv)
        return 0, "ok"

    monkeypatch.setattr(ops, "BACKEND", "ssh")
    monkeypatch.setattr(ops, "_exec", spy)

    ops._exec_through_policy("docker logs --tail 20 wp-app", host="web-01")
    assert "--no-pager" not in captured["argv"], \
        f"docker logs 不该带 --no-pager（docker 不认识它）：{captured['argv']}"
    assert captured["argv"][:2] == ["docker", "logs"]

    ops._exec_through_policy("journalctl -u nginx -n 20", host="web-01")
    assert "--no-pager" in captured["argv"], \
        "journalctl 必须带 --no-pager，否则在非交互 SSH 会话里会卡在分页器上"


# ============================================================
# 三、`docker logs` 新规则的边界
# ============================================================
def test_docker_logs_allow_case_is_host_channel():
    """正例：命中且为 allow / host 通道。

    host 是必然的：一次性容器里没有宿主机的 docker daemon，
    `docker logs` 放进容器根本够不着目标容器。
    """
    d = policy.decide(DOCKER_LOGS)
    assert d.decision == policy.ALLOW
    assert d.rule_key == "docker.logs"
    assert d.isolation == policy.CHANNEL_HOST
    assert d.argv == ["docker", "logs", "--tail", "100", "wp-app"]
    assert d.risk == "readonly"
    assert d.fingerprint, "allow 的 Decision 也要带指纹（执行时比对用）"


@pytest.mark.parametrize("command", [
    pytest.param("docker logs --tail 100000 wp-app", id="tail-out-of-range"),
    pytest.param("docker logs --tail 0 wp-app", id="tail-zero"),
    pytest.param("docker logs --tail -1 wp-app", id="tail-negative"),
    pytest.param("docker logs -f wp-app", id="follow-never-returns"),
    pytest.param("docker logs --since 1h wp-app", id="free-form-time-window"),
    pytest.param("docker logs wp-app; rm -rf /", id="command-chaining"),
    pytest.param("docker logs wp-app | grep err", id="pipe"),
    pytest.param("docker logs --tail 100 wp@app", id="illegal-container-char"),
    # 容器名规则（复用 _container_name）：[A-Za-z0-9][A-Za-z0-9_.-]{0,63} → 最长 64
    pytest.param("docker logs --tail 100 " + "a" * 65, id="container-name-too-long"),
    pytest.param("docker logs --tail 100 -f wp-app", id="extra-flag"),
    pytest.param("docker logs", id="no-container"),
])
def test_docker_logs_denied_forms(command):
    """反例全部 DENY。

    ★ `-f` 必须拒的理由不是"它危险"，是**它永不返回**：
      follow 会一直占着执行通道直到超时 —— 在自动化里等于把这次调用挂死。
    ★ `--since 1h` 必须拒的理由是它引入"自由参数"：
      这条规则的全部价值就在参数形态固定、可枚举。
    """
    assert policy.decide(command).decision == policy.DENY, command


@pytest.mark.parametrize("command,expected", [
    pytest.param("docker logs wp-app", ["docker", "logs", "wp-app"], id="no-tail"),
    pytest.param("docker logs -n 5 wp-app",
                 ["docker", "logs", "-n", "5", "wp-app"], id="short-flag"),
    pytest.param("docker logs --tail 1 wp-app",
                 ["docker", "logs", "--tail", "1", "wp-app"], id="tail-min"),
    pytest.param("docker logs --tail 500 wp-app",
                 ["docker", "logs", "--tail", "500", "wp-app"], id="tail-max"),
])
def test_docker_logs_accepted_forms_are_normalized(command, expected):
    """边界内的形态都要能用，且 argv 必须是规范化后的（执行的就是校验过的）。"""
    d = policy.decide(command)
    assert d.decision == policy.ALLOW, d.reason
    assert d.argv == expected


def test_new_rule_does_not_shadow_existing_docker_rules():
    """★ 边界不能误伤：新旧 docker 规则各管各的子命令。

    这三条共用一个 bin（docker），靠各自的 validate 分流。
    新规则如果写得太松（比如"第一个参数不是 flag 就放行"），
    `docker ps -a` 就可能被它抢走，`docker restart` 的"需审批"
    也会被降级成"直接放行" —— **那才是真的放宽了能力。**
    """
    ps = policy.decide("docker ps -a")
    assert ps.decision == policy.ALLOW and ps.rule_key == "docker.ps"

    restart = policy.decide("docker restart wp-app")
    assert restart.decision == policy.NEEDS_APPROVAL, \
        "docker restart 必须仍然需要人工确认"
    assert restart.rule_key == "docker.restart"

    keys = {row["key"] for row in policy.catalog()}
    assert {"docker.ps", "docker.restart", "docker.logs"} <= keys, \
        "catalog() 里三条规则都要在（模型和审批界面看的就是它）"


def test_new_rule_is_documented_with_a_runnable_example():
    """example 必须**可直接执行**，不能含 `<占位符>`。

    本项目踩过这个坑：第一版规则只有 usage，自检时拿 usage 当命令去执行，
    7 条规则全被拒 —— 尖括号是 shell 元字符。**照抄示例就报错的文档，
    比没有文档更糟。**
    """
    rule = policy.COMMANDS["docker.logs"]
    assert rule.usage and "<" in rule.usage            # usage 给人看，含占位符
    assert rule.example and "<" not in rule.example    # example 给模型抄，必须可跑
    assert policy.decide(rule.example).decision == policy.ALLOW
    assert rule.note
