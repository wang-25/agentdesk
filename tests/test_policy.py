# -*- coding: utf-8 -*-
"""命令白名单与路径准入 —— 安全边界的第一道闸。

这一层决定"模型能不能碰生产"，所以测试的重点不是覆盖率，而是两类**对称**的错误：

    ① **放行了不该放的**（越权）—— 攻击面必须全拒
    ② **拦住了本该放行的**（误拒）—— 白名单里每条规则都要有正例

②不是凑数：本项目真的踩过这个坑。`du` 最初只允许 `/var/log`，结果处置 Agent
想定位"根分区被什么占满"时被拒，只能如实回答"需要人工补上这一步"
（见 `policy.py:248-259`）。**一个只会说"不"的白名单，等于没有诊断能力。**
"""

import pytest

from app.sandbox import policy

# ============================================================
# 一、白名单正例：每条规则都必须接受自己的示例
# ============================================================
_RULE_EXAMPLES = [
    pytest.param(key, rule.example, id=key)
    for key, rule in sorted(policy.COMMANDS.items())
    if getattr(rule, "example", "")
]


def test_whitelist_is_not_empty():
    """白名单被测空了的话，下面那批参数化用例会静默变成 0 条 —— 先挡住。"""
    assert _RULE_EXAMPLES, "白名单里没有任何带示例的规则，测试失去意义"


@pytest.mark.parametrize("key,example", _RULE_EXAMPLES)
def test_every_rule_accepts_its_own_example(key, example):
    """每条规则的示例都必须能通过 —— 防"白名单写了但用不了"。"""
    d = policy.decide(example)
    assert d.decision != policy.DENY, f"{key} 的示例 {example!r} 被拒：{d.reason}"


def test_no_whitelisted_binary_is_also_blocked():
    """白名单与高危黑名单**不能有交集**。

    有交集意味着同一条命令既能被放行又能被禁止，判定结果就取决于
    "哪道闸先跑"——这种不确定本身就是漏洞。
    """
    whitelisted = {r.bin for r in policy.COMMANDS.values()}
    assert whitelisted & set(policy.BLOCKED_BINARIES) == set()


# ============================================================
# 二、黑名单：明确高危的二进制一律拒绝
# ============================================================
@pytest.mark.parametrize("binary", sorted(policy.BLOCKED_BINARIES))
def test_blocked_binary_is_denied(binary):
    d = policy.decide(f"{binary} --help")
    assert d.decision == policy.DENY, f"{binary} 被放行了：{d.decision}"
    assert binary in d.reason, "拒绝理由里要点明具体是哪个命令，模型才会换个做法"


# ============================================================
# 三、攻击面：组合、注入、路径逃逸
# ============================================================
ATTACKS = [
    pytest.param("systemctl restart nginx; rm -rf /", id="semicolon"),
    pytest.param("df -h | grep vda", id="pipe"),
    pytest.param("tail -n 20 /var/log/nginx/error.log > /tmp/out", id="redirect"),
    pytest.param("df -h && uptime", id="and"),
    pytest.param("df -h || df -h", id="or"),
    pytest.param("df -h `id`", id="backtick"),
    pytest.param("df -h $(id)", id="dollar-paren"),
    pytest.param("df -h $HOME", id="variable"),
    pytest.param("systemctl restart nginx\nrm -rf /", id="newline"),
    pytest.param("df -h \\n uptime", id="backslash"),
    pytest.param("/tmp/df -h", id="bin-with-path"),
    pytest.param("./df -h", id="bin-relative-path"),
    pytest.param("nmap -sT 10.0.0.1", id="not-in-whitelist"),
    pytest.param("bash -c id", id="shell"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
]


@pytest.mark.parametrize("command", ATTACKS)
def test_attack_surface_is_denied(command):
    assert policy.decide(command).decision == policy.DENY


def test_none_command_is_denied_not_crash():
    """`decide` 永不抛异常 —— 调用方是模型，它需要的是理由而不是 traceback。"""
    d = policy.decide(None)
    assert d.decision == policy.DENY


# ============================================================
# 四、路径准入（本项目最容易出错的一处）
# ============================================================
@pytest.mark.parametrize("command", [
    pytest.param("tail -n 20 /etc/passwd", id="outside-readable-dirs"),
    pytest.param("tail -n 20 /var/log/../../etc/passwd", id="dotdot-escape"),
    pytest.param("tail -n 20 /var/log/./../../etc/shadow", id="dotdot-mixed"),
    pytest.param("tail -n 20 var/log/nginx/error.log", id="relative-path"),
    pytest.param("tail -n 20 /var/logx/nginx.log", id="prefix-confusion"),
])
def test_read_path_escapes_are_denied(command):
    """读路径必须落在登记的目录里，前缀匹配不能被骗。

    `/var/logx/...` 这一条是专门防"前缀比较"写错成 `startswith("/var/log")`
    的 —— 那种写法会把 `/var/logx` 也算进去。
    """
    assert policy.decide(command).decision == policy.DENY


@pytest.mark.parametrize("command", [
    pytest.param("truncate -s 0 /var/log/nginx/error", id="write-not-dot-log"),
    pytest.param("truncate -s 0 /tmp/x.log", id="write-outside-dirs"),
    pytest.param("truncate -s 0 /etc/passwd", id="write-system-file"),
])
def test_write_paths_are_stricter_than_read(command):
    """写操作比读更严：只允许 `/var/log` 下的 `.log`。

    读错了最多是泄露，写错了可能是服务起不来。
    """
    assert policy.decide(command).decision == policy.DENY


def test_du_accepts_inspect_dirs_but_not_root_home():
    """`du` 的目录准入：排查目录放行，敏感目录拒绝。

    历史坑：`_INSPECT_DIRS` 里**混进过 `"/"`**，而校验用的是
    `startswith(d.rstrip("/") + "/")` —— 对 `"/"` 来说前缀退化成 `"/"`，
    于是任何绝对路径都成立，`du -sh /root` 被放行（`policy.py:264-277`）。
    """
    assert policy.decide("du -sh /var/lib/docker").decision != policy.DENY
    for sensitive in ("/root", "/etc", "/boot"):
        assert policy.decide(f"du -sh {sensitive}").decision == policy.DENY, sensitive


# ============================================================
# 五、决策字段的完整性
# ============================================================
def test_needs_approval_carries_enough_for_a_human_to_decide():
    """要人点头的命令，必须把"要执行什么"完整交代清楚。

    审批界面显示 A、实际执行 B —— 这是 HITL 最经典的漏洞，
    所以 `Decision` 里给人看的字段就是执行时用的字段。
    """
    d = policy.decide("truncate -s 0 /var/log/nginx/error.log")
    assert d.decision == policy.NEEDS_APPROVAL
    assert d.argv, "没有 argv 就无法执行，也无法核对"
    assert d.command, "没有规范化命令文本，审批界面就没东西可显示"
    assert d.fingerprint, "没有指纹就无法在执行前比对（防 TOCTOU）"
    assert d.reason, "没有理由，审批人只能盲批"


def test_allow_decision_also_carries_argv():
    d = policy.decide("df -h")
    assert d.decision == policy.ALLOW
    assert d.argv == ["df", "-h"]


def test_every_rule_declares_a_known_decision():
    for key, rule in policy.COMMANDS.items():
        assert rule.decision in (policy.ALLOW, policy.NEEDS_APPROVAL), key


def test_restart_rules_run_on_host_channel_not_container():
    """**记录当前事实**：重启类命令走的是主机通道，没有容器隔离。

    `docs/overview.md` 与 README 讲"写操作在一次性容器里执行"时，
    覆盖的其实只有 `container` 通道那批（如 `truncate`）。
    `systemctl restart` / `docker restart` 必须在主机上执行才能生效，
    所以它们**没有**容器隔离 —— 这一点必须留在测试里，
    免得下次又拿"写操作都进容器"去讲（见 docs/redev/01-audit.md C7）。
    """
    for command in ("systemctl restart nginx", "docker restart wp-app"):
        d = policy.decide(command)
        assert d.decision == policy.NEEDS_APPROVAL, command
        assert d.isolation == policy.CHANNEL_HOST, command


def test_truncate_rule_is_isolated_in_container():
    d = policy.decide("truncate -s 0 /var/log/nginx/error.log")
    assert d.isolation == policy.CHANNEL_CONTAINER


# ============================================================
# 六、风险收敛：模型只能抬高、不能降低
# ============================================================
@pytest.mark.parametrize("reported,floor,expected", [
    pytest.param("low", "high", "high", id="model-says-low-code-says-high"),
    pytest.param("high", "low", "high", id="model-says-high-respect-it"),
    pytest.param("medium", "medium", "medium", id="same"),
    pytest.param("low", "low", "low", id="both-low"),
    pytest.param(None, "high", "high", id="model-missing-arg"),
    pytest.param("high", None, "high", id="floor-missing"),
    pytest.param("low", "bogus", "low", id="unknown-floor-falls-back"),
    pytest.param("bogus", "medium", "medium", id="unknown-reported"),
])
def test_escalate_only_raises(reported, floor, expected):
    """**模型可以让我们更谨慎，不可以让我们更冒险。**

    这条如果写反了，闸门就等于没有 —— 而且错得很安静：
    它只表现为"什么都没发生"。
    """
    assert policy.escalate(reported, floor) == expected


@pytest.mark.parametrize("action,expected", [
    pytest.param("query", "low", id="query"),
    pytest.param("diagnose", "low", id="diagnose"),
    pytest.param("QUERY", "low", id="case-insensitive"),
    pytest.param("  query  ", "low", id="whitespace"),
    pytest.param("restart", "high", id="restart"),
    pytest.param("cleanup", "high", id="cleanup"),
    pytest.param("remediate", "high", id="remediate"),
    pytest.param(None, "medium", id="unknown-none"),
    pytest.param("nonsense", "medium", id="unknown-action"),
])
def test_risk_of_action(action, expected):
    assert policy.risk_of_action(action) == expected


def test_run_command_declared_risk_is_the_worst_case():
    """万能工具 `run_command` 的声明风险 = 它可能做到的最危险的事。"""
    assert policy.MAX_ACTION_RISK == "high"


# ============================================================
# 七、指纹（防 TOCTOU）
# ============================================================
def test_fingerprint_is_stable_and_short():
    args = ["truncate", "-s", "0", "/var/log/nginx/error.log"]
    mounts = [("/var/log/nginx", "/var/log/nginx", "rw")]
    a = policy.fingerprint(args, "container", mounts)
    b = policy.fingerprint(args, "container", mounts)
    assert a == b and len(a) == 16


def test_fingerprint_covers_channel_and_mounts():
    """只哈希命令文本不够：同一条命令，挂载只读还是可写是两件事。"""
    args = ["tail", "-n", "20", "/var/log/nginx/error.log"]
    base = policy.fingerprint(args, "container", [("/var/log", "/var/log", "ro")])
    assert policy.fingerprint(args, "host", [("/var/log", "/var/log", "ro")]) != base
    assert policy.fingerprint(args, "container", [("/var/log", "/var/log", "rw")]) != base


def test_fingerprint_is_order_sensitive_in_argv():
    a = policy.fingerprint(["tail", "-n", "20"], "host", [])
    b = policy.fingerprint(["tail", "20", "-n"], "host", [])
    assert a != b


# ============================================================
# 八、对外清单（模型与审批界面看到的东西）
# ============================================================
def test_catalog_matches_commands_table():
    rows = policy.catalog()
    assert len(rows) == len(policy.COMMANDS)
    for row in rows:
        assert set(row) >= {"key", "bin", "usage", "example", "decision",
                            "requires_approval", "isolation", "isolated"}
        assert row["requires_approval"] == (row["decision"] == policy.NEEDS_APPROVAL)
        assert row["isolated"] == (row["isolation"] == policy.CHANNEL_CONTAINER)


def test_describe_counts_are_self_consistent():
    d = policy.describe()
    assert d["whitelisted"] == len(policy.COMMANDS)
    assert d["blocked_binaries"] == len(policy.BLOCKED_BINARIES)
    assert d["allow"] + d["needs_approval"] == d["whitelisted"]
    assert d["readable_dirs"] and d["writable_dirs"]
    assert set(d["container_channel"]) <= set(policy.COMMANDS)
    assert set(d["host_channel"]) <= set(policy.COMMANDS)
    # 每条规则要么走容器要么走主机，不能两边都不占
    assert len(d["container_channel"]) + len(d["host_channel"]) == len(policy.COMMANDS)
