# -*- coding: utf-8 -*-
"""
命令策略 —— 沙箱的「准入规则」
============================================================
这个文件回答一个问题：**一条命令，允不允许跑？**

【为什么策略和执行要分开】
因为"允不允许"和"怎么执行"是两个完全不同的问题：

    策略（policy.py）    —— 白名单、参数校验、风险分级 → 产出决策
    执行（executor.py）  —— 容器 / 主机两个通道 → 产出结果

混在一起写的话，"想加一条新命令"就得动执行逻辑，
"想换个隔离方式"又得动白名单 —— 两件事互相干扰，改一处怕碰坏另一处。

分开之后：加命令只在 COMMANDS 表里加一条；换隔离方式只动 executor.py。
**这也是为什么这份文件里没有一行 subprocess —— 它只管判断，不管执行。**

【白名单而不是黑名单】
`rm -rf /` 这类东西是列不完的：`rm -rf /`、`rm -fr /`、`find / -delete`、
`dd if=/dev/zero of=/dev/sda`、`> /dev/sda`、`mkfs.ext4 /dev/sda1`……
黑名单永远漏。

所以规则是**正向的**：只有明确登记在 COMMANDS 里的命令才能跑。
不在表里 = 不许跑。

    ❌ 提示词说"请不要执行危险命令"     → 只是请求，模型可以不理会
    ✅ 不在白名单里就根本进不来          → 是约束，它连这条命令长什么样都不知道

同一个原则在多 Agent 编排里用过（把工具从 schema 里删掉而不是提示词里禁止），
在 MCP 里也用过（用协议自带的 hint 而不是自定义字段）。
**能用结构约束的，就不要靠自觉。**
"""

import hashlib
import re
import shlex
from dataclasses import dataclass, field

# ============================================================
# 一、常量：三个决策 + 两个通道
# ============================================================
# 决策三态。注意「拒绝」和「需审批」是两件不同的事：
#   DENY            —— 这件事本身就不该做（rm -rf、dd、改权限）
#                     不管谁来确认都不做，策略层面封死
#   NEEDS_APPROVAL  —— 这件事可以做，但必须有人点过头（重启服务、清日志）
ALLOW = "allow"
NEEDS_APPROVAL = "needs_approval"
DENY = "deny"

# 两个执行通道。**隔离强度不一样，而且不能混为一谈。**
#
#   container —— 一次性容器：文件系统只读、无网络、掉全部 capability、
#                非 root 用户、内存与 CPU 封顶、进程数封顶。
#                适合「对文件做的事」：读日志、清日志。
#                代价是：它看见的是容器内视角，不是宿主机的真实视角。
#
#   host —— 直接在目标主机执行。**没有容器隔离。**
#                适合「对服务做的事」：systemctl / docker 这类必须碰到
#                宿主机 init 和 docker daemon 的操作 —— 容器内根本够不着它们。
#
# ★ 这里有个必须讲清楚的判断：
#   很多人以为"把命令放进 docker run 就安全了"。对 `df -h` 这类命令，
#   放进容器之后你得到的是**容器的磁盘视图**，不是宿主机的 —— 隔离性和
#   观测准确性是两个方向的需求，硬混在一起会两头不讨好。
#
#   所以本项目的做法是：
#       写文件的操作  → 容器通道（真隔离，真的好用）
#       管服务的操作  → 主机通道（没隔离，靠白名单 + 审批 + 审计兜底）
#   并且每次执行结果里都带 `isolated: true/false`，
#   **让调用方自己看得见这次有没有真隔离，而不是让它"以为"有。**
CHANNEL_CONTAINER = "container"
CHANNEL_HOST = "host"

# 输出上限。命令可能刷出几百 MB 日志，直接塞进模型上下文会爆。
MAX_OUTPUT_BYTES = 16 * 1024


# ============================================================
# 请求级风险：**唯一的定义源**
# ============================================================
# ★ 这一节是为了修一个真实的安全漏洞而加的。
#
#   原先这个项目有**三处各自定义"重启服务有多危险"**，结论互相矛盾：
#
#     app/main.py 的 INTENT_SYSTEM_PROMPT   →  medium（于是闸门放行）
#     本文件的命令规则（systemctl restart）  →  needs_approval（要人工）
#     app/tools/ops.py 的 run_command       →  risk=high
#
#   而**危害最大的那条路径（无人值守告警）偏偏用了最松的那一份**：
#   实测"mysql 挂了需要立即重启"被判成 medium → 一路走到 auto_diagnose。
#
#   根因不是某处写错了，是"同一个概念允许被定义三次"。
#   **一个概念只能有一个定义源**，否则迟早有一处会被读错 ——
#   而且总是被最危险的那条路径读到。
#
# ★ 两个层级要分清，它们不是一回事：
#     · 请求级风险（本节）—— 一句话想干的事有多危险。用于**放不放行的决策**。
#     · 命令级风险（Decision.risk）—— 一条具体命令是只读还是可逆。用于展示与审计。
#
#   决策一律以本表 + decide() 为准；**模型自报的 risk 只能抬高、不能降低**
#   （见 escalate）。因为它会判错，而它的错会直接把闸门打开。
ACTION_RISK = {
    "query":     "low",      # 只看看状态
    "diagnose":  "low",      # 查原因，只读
    "explain":   "low",      # 问原理/做法
    "restart":   "high",     # 会中断服务
    "cleanup":   "high",     # 会删/清数据
    "remediate": "high",     # 任何"动手改系统"的请求
    "other":     "medium",   # 认不出来 → 不确定，不自动执行
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# run_command 这类万能工具的声明风险 = 它可能做到的最危险的事
MAX_ACTION_RISK = max(ACTION_RISK.values(), key=lambda r: RISK_ORDER.get(r, 0))


def risk_of_action(action: str) -> str:
    """由 action 得出请求级风险。认不出来按 other 处理（medium，不自动）。"""
    return ACTION_RISK.get((action or "").strip().lower(), ACTION_RISK["other"])


def escalate(reported: str, floor: str) -> str:
    """取两者中更危险的那个 —— 这是"安全底线"的标准写法。

    ★ 为什么要这样设计，而不是"以模型为准"或"以代码为准"：

      模型判得**更严**时必须尊重它（它可能看到了代码规则没覆盖的上下文，
      比如用户说"这是生产库"，那查询也该谨慎对待）；

      模型判得**更松**时不能听它的（它判错一次，闸门就开一次，
      而这类错误不会被任何测试抓住 —— 它只表现为"什么都没发生"）。

      所以规则是：**模型可以让我更谨慎，不可以让我更冒险。**
      这与"模型提议、代码决定"是同一条原则的两个面。
    """
    a = RISK_ORDER.get((reported or "").strip().lower(), -1)
    b = RISK_ORDER.get((floor or "").strip().lower(), -1)
    if a < 0:
        return floor if b >= 0 else ACTION_RISK["other"]
    if b < 0:
        return reported
    return reported if a >= b else floor


class PolicyError(Exception):
    """策略层拒绝。消息是给模型看的 —— 它会据此换一个做法。"""


# ============================================================
# 二、第一道闸：shell 元字符一律拒绝
# ============================================================
# 技术上，我们用 subprocess 的列表传参 + shell=False，分号管道本来就
# 不会被解释。那为什么还要在这一层拦掉？
#
# 两个原因：
#   1. **不要给"看起来能拼命令"留任何入口。** 今天是 shell=False，
#      哪天有人图省事改成 shell=True，这层拦截就是最后一道墙。
#   2. 这条命令会以文本形式**进入模型的上下文和人的审批界面**。
#      一条带 `;` 的命令混在里面，人眼很难一眼看出它是组合命令。
#      **审批界面里显示的东西，必须是它真正要执行的东西。**
#
# 审批 UI 上显示 A、实际执行 B —— 这是 HITL 最经典的安全漏洞。
_SHELL_META = set(';|&$`><\n\r\\')

# 明确高危的可执行文件。放进这里是为了给模型一个**具体理由**，
# 而不是笼统的"不在白名单" —— 后者它可能会换个写法再试一次。
BLOCKED_BINARIES = {
    "rm": "删除操作，不可恢复。清理日志请用 truncate -s 0",
    "shred": "不可恢复地覆写文件",
    "dd": "可覆写磁盘设备",
    "mkfs": "格式化文件系统",
    "mkfs.ext4": "格式化文件系统",
    "fdisk": "改分区表",
    "parted": "改分区表",
    "shutdown": "关机",
    "reboot": "重启主机",
    "halt": "关机",
    "poweroff": "关机",
    "kill": "杀进程，可能影响生产服务。请改用 systemctl restart",
    "pkill": "按名杀进程，影响范围不可控",
    "killall": "按名杀进程，影响范围不可控",
    "chmod": "改权限，可能造成服务无法启动",
    "chown": "改属主，可能造成服务无法读取文件",
    "chattr": "改文件属性",
    "passwd": "改密码",
    "useradd": "改用户",
    "userdel": "删用户",
    "usermod": "改用户",
    "visudo": "改 sudo 配置",
    "iptables": "改防火墙规则，可能导致失联",
    "nft": "改防火墙规则",
    "curl": "可下载并执行任意内容",
    "wget": "可下载并执行任意内容",
    "nc": "网络工具，可反连",
    "ncat": "网络工具，可反连",
    "socat": "网络工具，可反连",
    "bash": "可执行任意脚本，绕过一切白名单",
    "sh": "可执行任意脚本，绕过一切白名单",
    "zsh": "可执行任意脚本，绕过一切白名单",
    "python": "可执行任意代码",
    "python3": "可执行任意代码",
    "perl": "可执行任意代码",
    "ruby": "可执行任意代码",
    "node": "可执行任意代码",
    "eval": "动态求值",
    "exec": "替换进程",
    "mount": "挂载文件系统",
    "umount": "卸载文件系统",
    "crontab": "改定时任务",
}

# ★ 注意这里**故意没有 systemctl**：它既是最常用的，也是最危险的。
#   把 `systemctl` 整体放行，等于连 `systemctl disable firewalld`、
#   `systemctl mask sshd` 一起放行了；整体禁止又什么都做不了。
#
#   所以它必须**按子命令逐条放行** —— 见下面 COMMANDS 里的
#   `systemctl.is-active`（只读，直接放行）/ `systemctl.status`（只读）/
#   `systemctl.restart`（写，需审批）三条，共用一个 bin、三个不同决策。
#
#   **这就是白名单要按"子命令"而不是按"可执行文件"来管的原因。**


# ============================================================
# 三、第二道闸：路径白名单
# ============================================================
# 参数里最容易出问题的是**路径**。如果路径由调用方随便给：
#     cat /etc/shadow          读密码文件
#     tail -n 5 ../../etc/passwd
#     truncate -s 0 /etc/passwd    清空密码文件
#
# 所以路径不是"校验一下格式"，而是**必须落在登记过的目录里**。
#
# 而且只允许读 .log 结尾的文件 —— 日志目录里也可能有别的，
# 没必要给。**能少给就少给。**
# ---- 读：日志目录 ----
# 只允许读 /var/log 下的文件。日志目录里也可能有别的（wtmp、btmp、审计日志），
# 没必要给。**能少给就少给。**
_READABLE_DIRS = ("/var/log",)
# ---- 写：只允许日志目录，而且必须是 .log 文件 ----
# 比读更严，因为读错了最多是泄露，写错了可能是服务起不来。
_WRITABLE_DIRS = ("/var/log",)
_LOG_FILE_RE = re.compile(r"^/var/log/(?:[\w.-]+/)*[\w.-]+\.log(?:\.\d+)?$")

# ---- 排查用目录（只给 du 这类"看目录大小"的命令）----
#
# ★ 这条白名单是**被真实使用逼出来的**。
#
#   第一版 du 只允许 /var/log。结果处置 Agent 想定位"根分区被什么占满"时，
#   `du -sh /var/lib/docker` 直接被拒 —— 而它没法去查这件事。
#   最后它只能如实说"根分区真实占用尚未定位，需要人工补上这一步"。
#
#   拒绝对不对？对，白名单里没写就该拒。
#   但**这份白名单本身设计得太窄了**：诊断类只读命令给得太少，
#   Agent 会卡在"查不到"上，而不是卡在"不能做"上。
#
#   加目录的原则是：**看目录大小这件事泄露的是"有哪些目录、各多大"，
#   不是内容。** 风险很低，而它对定位磁盘问题几乎是必需的。
#   与之相对的 `cat` 就不行 —— 那是读内容。
#
#   ★ 所以"放开权限"不是一刀切的：**要按"这个操作能泄露/破坏什么"来分级，
#     而不是按"它是读还是写"。**
# ★ 这里**不能放 "/"**。
#
#   第一版放了，结果白名单整个失效：校验时用
#       clean.startswith(d.rstrip("/") + "/")
#   对 d="/" 来说，d.rstrip("/") 是**空串**，条件退化成
#       clean.startswith("/")
#   —— 任何绝对路径都成立。于是 `du -sh /root` 被放行了。
#
#   ★ 这是白名单设计里非常典型的一类错误：**表里混进了一个"匹配一切"的值，
#     它不会报错、不会告警，只是让整张表形同虚设。**
#     从代码上看，那一行还特别像"允许根目录"这样一个合理的配置。
#
#   根目录本身是允许的（`du -h -d1 /` 是定位磁盘问题的起点），
#   但它必须在代码里**单独判断**，不能混在这张表里 —— 见下面的 _du_allows。
_INSPECT_DIRS = (
    "/var", "/var/log", "/var/lib", "/var/lib/docker",
    "/var/lib/mysql", "/var/cache", "/tmp", "/home", "/opt",
    "/srv", "/usr", "/data",
)


def _du_allows(target: str) -> bool:
    """du 的目录准入判断。"""

    # 根目录本身允许：`du -h -d1 /` 是定位磁盘问题的标准起点。
    #
    # 为什么它可以，而 `/root` 不行？——**因为契约是"排查目标目录"**。
    # 根目录是一个明确的整体视角；`/root`、`/etc` 这些是具体的敏感目录，
    # 它们不在排查清单里，就不该被单独探测。
    #
    # 注意不能只允许 `/` 就直接放行所有路径 —— 那正是上一版的漏洞。
    if target == "/":
        return True

    for d in _INSPECT_DIRS:
        if target == d or target.startswith(d + "/"):
            return True
    return False


def _check_path(path: str, *, writable: bool = False) -> str:
    """路径校验。返回规范化后的路径，不合法就抛。"""
    if not isinstance(path, str) or not path:
        raise PolicyError("路径不能为空")
    if not path.startswith("/"):
        raise PolicyError(f"必须是绝对路径：{path!r}")
    if ".." in path.split("/"):
        raise PolicyError(f"路径不允许包含 ..：{path!r}")
    if _SHELL_META & set(path):
        raise PolicyError(f"路径含非法字符：{path!r}")

    allowed = _WRITABLE_DIRS if writable else _READABLE_DIRS
    if not any(path.startswith(d + "/") for d in allowed):
        raise PolicyError(
            f"路径不在允许范围内：{path!r}。只允许 {'、'.join(allowed)} 下的文件")

    if writable and not _LOG_FILE_RE.match(path):
        # 写操作**只允许 .log 结尾**。这条比读更严 ——
        # 因为读错了最多是泄露，写错了可能是服务起不来。
        raise PolicyError(
            f"只允许对 .log 文件做写操作，收到：{path!r}。"
            f"日志轮转文件（.log.1）也可以")
    return path


# ============================================================
# 四、命令规则表
# ============================================================
@dataclass
class CommandRule:
    """一条放行规则。

    bin        可执行文件名（不是完整路径 —— 避免 /tmp/mydf 这种伪造）
    decision   决策：allow / needs_approval
    isolation  走哪个通道：container / host
    timeout    超时秒数
    validate   参数校验函数，签名 (args: list[str]) -> list[str]
               返回规范化后的参数（或抛 PolicyError）
               ★ 约定：args 是**去掉可执行文件名之后**的全部参数。
                 单用途命令（tail）args 就是参数；
                 多子命令命令（systemctl / docker）args[0] 是子命令，
                 用 _sub() 剥离（别手写，这个约定被踩过一次）
    usage      给模型和审批人看的标准用法（**含占位符，不能直接执行**）
    example    一条具体、可直接执行的示例（换掉占位符的真实参数）

    ★ 为什么 usage 和 example 要分开：

      usage 是给人看的语法说明：`tail -n <行数> /var/log/.../xxx.log`
      example 是给模型看的可执行样例：`tail -n 20 /var/log/nginx/error.log`

    第一版只有 usage。自检时发现：**拿 usage 当命令去执行，7 条规则全部被拒** ——
    因为 `<服务名>` 里的尖括号正好是 shell 元字符，`[-a]` 也不是合法参数。

    这对人不是问题（人看得懂 `<X>` 是占位符），但**模型很可能会照抄**。
    一个「文档里写了但一执行就报错」的示例，比没有示例更糟 ——
    它会让模型在同一个地方反复撞墙，还以为是自己写错了。

    所以：**凡是会被模型读到的示例，都必须是能直接跑通的那种。**
    """

    bin: str
    decision: str
    isolation: str
    timeout: int
    validate: object
    usage: str
    example: str = ""
    note: str = ""


def _exact(*wanted):
    """参数必须是这几个固定组合之一。"""
    def _v(args):
        got = list(args)
        if got not in [list(w) for w in wanted]:
            raise PolicyError(
                f"参数只允许 {' 或 '.join(' '.join(w) or '(无)' for w in wanted)}，"
                f"收到 {' '.join(got) or '(无)'}")
        return got
    return _v


def _sub(args, name, rest):
    """多子命令的 bin 用它：校验并剥离子命令。

    ★ 这个助手是一次真实 bug 的产物，值得说清楚。

    【背景：validate 收到的 args 到底是什么】

      单用途命令（df / free / tail / truncate）：
          `tail -n 20 /var/log/x.log`  →  args = ["-n", "20", "/var/log/x.log"]
          参数就是参数。

      多子命令命令（systemctl / docker）：
          `systemctl is-active nginx`  →  args = ["is-active", "nginx"]
          **第一个元素是子命令，不是参数。**

    【踩到的坑】
    第一版把 systemctl 的三个 validator 按"纯参数"写：
        def _v_is_active(args):
            return ["is-active"] + _service_name(args)   # 以为 args 是 [svc]
    实际传进来的是 ["is-active", "nginx"]，于是 _service_name 抱怨
    "需要一个服务名，收到 2 个参数" → 校验失败 → **这条规则永远命中不了**。

    危险的地方在于它**不报错**：规则表看起来好好的，
    文档里也写着支持 `systemctl restart`，但实际执行时永远是"参数形式不被允许"。
    自检时拿每条规则自己的 example 去跑，才发现 12 条里有 5 条从来没生效过。

    ★ 教训：**"存在但从不被触发"的代码，比不存在的代码危险。**
      不存在的代码你会去补；永远不触发的代码，你以为它已经在保护你了。

      （同类的东西在设计上也要防：多 Agent 的校验器、MCP 的 schema 一致性校验，
        都用了同一个办法 —— **拿真实样例喂给它，确认它真的会响**。）
    """
    if not args or args[0] != name:
        raise PolicyError(f"只支持 `{name}` 子命令")
    return [name] + rest(args[1:])


def _service_name(args, *, count=1):
    """服务名：只允许小写字母数字点下划线连字符和 @。"""
    if len(args) != count:
        raise PolicyError(f"需要一个服务名，收到 {len(args)} 个参数")
    name = args[0]
    if not re.match(r"^[a-zA-Z0-9_.@-]{1,32}$", name):
        raise PolicyError(f"服务名不合法：{name!r}")
    return [name]


def _v_is_active(args):
    return _sub(args, "is-active", _service_name)


def _v_status(args):
    def _rest(rest):
        # 允许 `-l` 和 `--no-pager`，但顺序固定，不做任意组合 ——
        # 支持任意组合就等于支持任意参数，白名单的意义就没了
        if rest and rest[0] in ("-l", "--no-pager"):
            return [rest[0]] + _service_name(rest[1:])
        return _service_name(rest)
    return _sub(args, "status", _rest)


def _v_restart(args):
    return _sub(args, "restart", _service_name)


def _v_journalctl(args):
    """journalctl -u <svc> -n <N>"""
    if len(args) != 4 or args[0] != "-u" or args[2] != "-n":
        raise PolicyError("用法：journalctl -u <服务名> -n <行数>")
    svc = _service_name([args[1]])[0]
    lines = _check_lines([args[3]])[0]
    return ["-u", svc, "-n", lines]


def _check_lines(args):
    if len(args) != 1 or not args[0].isdigit():
        raise PolicyError("行数必须是数字")
    n = int(args[0])
    if not 1 <= n <= 200:
        raise PolicyError(f"行数必须在 1-200 之间，收到 {n}")
    return [str(n)]


def _v_tail(args):
    """tail -n <N> <日志文件>"""
    if len(args) != 3 or args[0] != "-n":
        raise PolicyError("用法：tail -n <行数> <日志文件绝对路径>")
    lines = _check_lines([args[1]])[0]
    path = _check_path(args[2])
    return ["-n", lines, path]


def _v_truncate(args):
    """truncate -s 0 <日志文件>  —— 唯一允许的写操作

    【为什么是 truncate 而不是 rm】
    这不是我拍的，是知识库文档 data/knowledge/disk-full.md 里写的：

        rm 之后文件没了，但**正在写它的进程还攥着那个文件句柄**，
        空间不会释放 —— 你以为清了 5G，df 一看一点没变。
        truncate -s 0 是把文件长度截为零，句柄仍然有效，
        进程继续往同一个位置写，空间立刻释放。

    ★ 这里有个值得说的闭环：**这条策略不是凭空定的，是把知识库里的
      运维经验直接编码成了准入规则。** RAG 让 Agent"知道"该这么做，
      策略让它"只能"这么做。
      —— 知道和做到之间那道缝，就是这类系统最常见的翻车点。
    """
    if len(args) != 3 or args[0] != "-s" or args[1] != "0":
        raise PolicyError("用法：truncate -s 0 <日志文件绝对路径>")
    path = _check_path(args[2], writable=True)
    return ["-s", "0", path]


def _container_name(args):
    if len(args) != 1 or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", args[0]):
        raise PolicyError(f"容器名不合法：{args[0] if args else '(空)'}")
    return [args[0]]


def _v_docker_ps(args):
    return _sub(args, "ps", _exact([], ["-a"]))


def _v_docker_restart(args):
    return _sub(args, "restart", _container_name)


def _v_du(args):
    """du —— 查看目录占用。只支持两种形态：

        du -sh <目录>           看一个目录的总大小
        du -h -d1 <目录>        看一个目录下**逐个子目录**的大小

    【为什么要支持第二种】
    因为定位"根分区被什么占满"这件事，本质是**逐层下钻**：
        du -sh /  太大了 →  du -h -d1 /  看到 /var 最大
        → du -h -d1 /var → 看到 /var/lib 最大 → …
    只给 `-sh` 的话，Agent 每下钻一层都要多花一轮，而且它得自己猜子目录名。
    一个 `-d1` 就能看到全部子目录 —— **这正是排查时真正需要的那个形态。**

    ★ 注意这里只放行了 `-d1`，没有放行 `-d 2` / `--exclude` 之类。
      因为 `-d2` 开始输出量就不可控了（一个 /usr 下面几万个子目录）。
      **"只把常用的那一个形态精确放进白名单"，比"放行整个命令再想办法限制"可靠。**
    """
    forms = {
        ("-sh",): 1,          # du -sh <dir>
        ("-h", "-d1"): 2,     # du -h -d1 <dir>
        ("-h", "-d", "1"): 2,  # du -h -d 1 <dir>（等价写法）
    }
    if len(args) < 2:
        raise PolicyError("用法：du -sh <目录> 或 du -h -d1 <目录>")

    # 找到匹配的形态：按前缀最长的优先，避免 "-h" 抢先匹配 "-sh" 之类
    matched = None
    for form, n_extra in sorted(forms.items(), key=lambda x: -len(x[0])):
        if tuple(args[:len(form)]) == form:
            matched = (form, n_extra)
            break
    if not matched:
        raise PolicyError("du 只支持 `-sh` 或 `-h -d1` 两种参数形式")

    form, n_extra = matched
    rest = args[len(form):]
    if len(rest) != 1:
        raise PolicyError("只允许指定一个目录")

    target = rest[0]
    if not target.startswith("/"):
        raise PolicyError(f"必须是绝对路径：{target!r}")
    if ".." in target.split("/") or (_SHELL_META & set(target)):
        raise PolicyError(f"目录路径不合法：{target!r}")

    clean = target.rstrip("/") or "/"
    if not _du_allows(clean):
        raise PolicyError(
            f"目录不在排查白名单内：{target!r}。"
            f"允许：/（只看顶层）或 {'、'.join(_INSPECT_DIRS)} 之下")
    return list(form) + [clean]


# ---- 表本身 ----
COMMANDS = {
    # 只读诊断：直接放行。这些都是"看一眼"的操作，不会改变任何状态。
    "df": CommandRule(
        bin="df", decision=ALLOW, isolation=CHANNEL_HOST, timeout=10,
        validate=_exact(["-h"], []),
        example="df -h",
        usage="df -h",
        note="只读，查看磁盘使用率"),
    "free": CommandRule(
        bin="free", decision=ALLOW, isolation=CHANNEL_HOST, timeout=10,
        validate=_exact(["-m"], []),
        example="free -m",
        usage="free -m",
        note="只读，查看内存占用"),
    "uptime": CommandRule(
        bin="uptime", decision=ALLOW, isolation=CHANNEL_HOST, timeout=10,
        validate=_exact([]),
        example="uptime",
        usage="uptime",
        note="只读，查看负载"),
    "systemctl.is-active": CommandRule(
        bin="systemctl", decision=ALLOW, isolation=CHANNEL_HOST, timeout=10,
        validate=_v_is_active,
        example="systemctl is-active nginx",
        usage="systemctl is-active <服务名>",
        note="只读，查询服务状态"),
    # systemctl status 单独一条：它和 restart 共用 bin，但决策完全不同。
    # **这就是为什么白名单要按"子命令"而不是按"可执行文件"来管** ——
    # 把 systemctl 整体放行，等于连 `systemctl disable firewalld` 一起放行了。
    "systemctl.status": CommandRule(
        bin="systemctl", decision=ALLOW, isolation=CHANNEL_HOST, timeout=10,
        validate=_v_status,
        example="systemctl status nginx",
        usage="systemctl status [--no-pager] <服务名>",
        note="只读，查看服务详情"),
    "journalctl": CommandRule(
        bin="journalctl", decision=ALLOW, isolation=CHANNEL_HOST, timeout=15,
        validate=_v_journalctl,
        example="journalctl -u nginx -n 20",
        usage="journalctl -u <服务名> -n <行数>",
        note="只读，查看服务日志"),
    "docker.ps": CommandRule(
        bin="docker", decision=ALLOW, isolation=CHANNEL_HOST, timeout=15,
        validate=_v_docker_ps,
        example="docker ps -a",
        usage="docker ps [-a]",
        note="只读，列出容器"),

    # ★ lsof 只放行 `+L1`（列出"已删除但仍被进程持有"的文件）。
    #
    #   这不是随便加的 —— 诊断 Agent 在回答里点名要查这个。
    #   而它确实是磁盘"莫名满"最常见的原因之一：
    #       日志被 rm 掉了，但写它的进程还攥着文件句柄，
    #       空间不释放，du 也看不到。
    #
    #   ★ 这个例子很好地说明了白名单该按什么粒度管：
    #       `lsof` 单独用可以列出所有进程打开的所有文件 —— 信息量极大，
    #       `lsof +L1` 只列"已删除但被持有"的文件 —— 精确、够用。
    #     整体放行太大，整体禁止又做不了这件事，
    #     **那就精确到那一个参数形态。**
    "lsof": CommandRule(
        bin="lsof", decision=ALLOW, isolation=CHANNEL_HOST, timeout=20,
        validate=_exact(["+L1"]),
        usage="lsof +L1",
        example="lsof +L1",
        note="只读，列出已删除但仍被进程持有的文件"),

    # 文件类：容器通道。这两个是**真正享受到容器隔离**的命令。
    "tail": CommandRule(
        bin="tail", decision=ALLOW, isolation=CHANNEL_CONTAINER, timeout=15,
        validate=_v_tail,
        example="tail -n 20 /var/log/nginx/error.log",
        usage="tail -n <行数> /var/log/.../xxx.log",
        note="只读，读取日志尾部；走容器通道，挂载日志目录为只读"),
    # ★ du 从容器通道改到主机通道，这个改动值得说清楚。
    #
    #   【改动的判据：隔离值不值得付它的成本】
    #
    #   一开始 du 放容器通道，理由是"它只读，挂 ro 进去更安全"。
    #   但真去用的时候发现：要支持 `du -h -d1 /`，就得把宿主机根目录
    #   整个挂进容器（`-v /:/:ro`）。那反而更糟 ——
    #   为了一个只读命令，把整个文件系统暴露给容器。
    #
    #   反过来想：**隔离的价值在于约束"能造成改动的操作"。**
    #   `du` 改不了任何东西，隔离对它没有额外收益，
    #   却实实在在限制了它能看的范围（挂 / 太重、只挂 /var/log 又不够用）。
    #
    #   ★ 所以通道的划分判据不是"读还是写"，而是：
    #       **这个操作有没有可能造成破坏 / 泄露？容器隔离能不能真的帮上忙？**
    #     du 的答案：不会破坏；挂载方案反而扩大暴露面。→ 主机通道。
    #     truncate 的答案：会改文件；只挂 /var/log 就够用且刚好封住了范围。→ 容器通道。
    #
    #   **隔离不是越多越好，是"刚刚够用"最好。**
    "du": CommandRule(
        bin="du", decision=ALLOW, isolation=CHANNEL_HOST, timeout=20,
        validate=_v_du,
        usage="du -sh <目录> 或 du -h -d1 <目录>",
        example="du -h -d1 /var/lib",
        note="只读，统计目录占用"),

    # ---- 需要人工确认：可逆，但会改变运行状态 ----
    "systemctl.restart": CommandRule(
        bin="systemctl", decision=NEEDS_APPROVAL, isolation=CHANNEL_HOST,
        timeout=60, validate=_v_restart,
        example="systemctl restart nginx",
        usage="systemctl restart <服务名>",
        note="会中断服务约数秒到数十秒，需人工确认"),
    "docker.restart": CommandRule(
        bin="docker", decision=NEEDS_APPROVAL, isolation=CHANNEL_HOST,
        timeout=60, validate=_v_docker_restart,
        example="docker restart wp-app",
        usage="docker restart <容器名>",
        note="容器会短暂不可用，需人工确认"),
    "truncate": CommandRule(
        bin="truncate", decision=NEEDS_APPROVAL, isolation=CHANNEL_CONTAINER,
        timeout=20, validate=_v_truncate,
        example="truncate -s 0 /var/log/nginx/error.log",
        usage="truncate -s 0 /var/log/.../xxx.log",
        note="清空日志内容（保留文件句柄），需人工确认"),

    # 注意：这里**故意没有** rm / dd / chmod / bash / curl。
    # 它们不在表里 = 永远进不来。BLOCKED_BINARIES 只是用来给出**更好的理由**。
}


# ============================================================
# 五、决策
# ============================================================
@dataclass
class Decision:
    """一条命令的判定结果。

    这个对象会被序列化进 API 响应和审批记录 —— 也就是说，
    **审批人看到的每一个字段，都是执行时会用到的字段。**
    不允许出现"UI 上显示一套、执行时用另一套"。
    """

    decision: str          # allow / needs_approval / deny
    command: str           # 规范化后的命令（空格连接的 argv，用于展示与审批）
    argv: list             # 真正要执行的参数列表
    rule_key: str = ""     # 命中的规则，如 systemctl.restart
    risk: str = ""         # readonly / reversible
    isolation: str = ""    # container / host
    timeout: int = 15
    mounts: list = field(default_factory=list)   # 容器通道的挂载：[(host, target, mode)]
    reason: str = ""       # 给模型/审批人看的一句话
    fingerprint: str = ""  # ← 见下面的说明

    def to_dict(self) -> dict:
        return {
            "decision": self.decision, "command": self.command,
            "rule": self.rule_key, "risk": self.risk,
            "isolation": self.isolation, "timeout": self.timeout,
            "reason": self.reason, "fingerprint": self.fingerprint,
        }


def fingerprint(argv: list, isolation: str, mounts: list) -> str:
    """命令指纹：把「要执行什么」压成一个短哈希。

    ★ 它是防 TOCTOU 的。

    TOCTOU = Time-Of-Check to Time-Of-Use，检查时和用时不是同一个东西。
    在 HITL 里它长得是这样：

        10:00  模型想执行 `systemctl restart nginx`  → 生成审批单，人看到的是这条
        10:05  人点了"同意"
        10:05  执行 —— 但执行的是 `truncate -s 0 /var/log/nginx/error.log`？

    上面这个错位在三种情况下会真实发生：
        ① 审批单里的命令字段被别处改过
        ② 审批和执行之间隔了持久化，重新读回来时被篡改
        ③ 同一个审批 ID 被重放，但命令换了

    所以：**审批时记下指纹，执行时重新算一遍比对。**
    对不上就拒绝执行，并且记审计。

    指纹覆盖 argv + 通道 + 挂载 —— 这三样加起来才是"这条命令"的完整定义。
    只哈希命令文本不够：同一个 `tail -n 20 /var/log/x.log`
    挂载只读和挂载可写，是两个完全不同的事。
    """
    payload = "\x1f".join([
        isolation,
        " ".join(argv),
        ";".join(f"{h}>{t}:{m}" for h, t, m in sorted(mounts)),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_HINT = ("命令必须是白名单里的（见 /sandbox/commands）。"
         "只读诊断通常已有专用工具，优先用它们。")


def decide(command: str) -> Decision:
    """判定一条命令。永不抛异常 —— 拒绝也是一种决策，要能被返回出去。

    【为什么这里不抛异常】
    调用方是模型。它需要的是"这条不行，因为 X，你可以试试 Y"，
    而不是一个 traceback。**拒绝理由本身就是给模型的提示。**
    理由写得越具体，它第二次改得越准。
    """
    raw = "" if command is None else str(command)
    text = raw.strip()

    if not text:
        return _deny("", "空命令", _HINT)

    # ---- 闸 1：shell 元字符 ----
    hit = _SHELL_META & set(text)
    if hit:
        return _deny(text,
                     f"命令里含 shell 特殊字符 {''.join(sorted(hit))!r}，"
                     f"不允许组合命令、管道、重定向或变量展开。"
                     f"一次只执行一条简单命令",
                     "如需多步操作，请分多次调用")

    # ---- 解析 ----
    try:
        argv = shlex.split(text)
    except ValueError as e:
        return _deny(text, f"命令无法解析：{e}", _HINT)
    if not argv:
        return _deny(text, "空命令", _HINT)

    bin_name = argv[0]
    args = argv[1:]

    # 拒绝带路径的可执行文件：`/tmp/df`、`./df` 都挡掉，
    # 否则"程序名白名单"就没意义了。
    if "/" in bin_name:
        return _deny(text, f"可执行文件只能用名字，不能用路径：{bin_name!r}",
                     "只允许白名单里的标准命令")

    # ---- 闸 2：明确高危 ----
    if bin_name in BLOCKED_BINARIES:
        return _deny(text, f"`{bin_name}` 被策略禁止：{BLOCKED_BINARIES[bin_name]}",
                     "请改用白名单里的等价做法")

    # ---- 闸 3：查白名单 ----
    # 一个 bin 可能对应多条规则（systemctl 就有三条），所以先筛候选，
    # 再用各自的 validate 试 —— 哪个通过算哪个。
    candidates = [r for r in COMMANDS.values() if r.bin == bin_name]
    if not candidates:
        return _deny(text, f"`{bin_name}` 不在白名单里",
                     f"当前允许的命令：{'、'.join(sorted(COMMANDS))}")

    for rule in candidates:
        try:
            normalized = rule.validate(args)
        except PolicyError:
            continue
        return _build(rule, bin_name, normalized)

    # 全部候选都不接受这些参数 —— 把各自的用法列出来给模型
    # 给**可直接执行的示例**，而不是含占位符的 usage ——
    # 后者模型可能照抄，照抄就必然报错（尖括号是 shell 元字符）。
    examples = "　".join(f"`{r.example}`" for r in candidates if r.example)
    return _deny(text, f"`{bin_name}` 的参数形式不被允许",
                 f"可用示例：{examples}" if examples else _HINT)


def _build(rule: CommandRule, bin_name: str, args: list) -> Decision:
    """命中了规则 —— 组装完整决策，含挂载与指纹。"""
    argv = [bin_name] + args
    mounts = _mounts_for(rule, args)
    key = next((k for k, r in COMMANDS.items() if r is rule), "")
    risk = "readonly" if rule.decision == ALLOW else "reversible"

    return Decision(
        decision=rule.decision,
        command=" ".join(argv),
        argv=argv,
        rule_key=key,
        risk=risk,
        isolation=rule.isolation,
        timeout=rule.timeout,
        mounts=mounts,
        reason=rule.note,
        fingerprint=fingerprint(argv, rule.isolation, mounts),
    )


def _mounts_for(rule: CommandRule, args: list) -> list:
    """容器通道的挂载。**由规则决定，不由调用方决定。**

    ★ 这一点很关键：如果挂载能由调用方指定，那"只读挂载"就是假的 ——
      它完全可以要求挂载成可写。**挂载是权限的一部分，跟命令一样必须来自白名单。**

    挂载路径取命令里出现的那个路径的所在目录，并以相同路径挂进容器，
    这样 argv 里的绝对路径在容器内依然有效。
    """
    if rule.isolation != CHANNEL_CONTAINER:
        return []
    paths = [a for a in args if a.startswith("/")]
    if not paths:
        return []
    target = paths[0]
    directory = target.rsplit("/", 1)[0] or "/"
    mode = "rw" if rule.decision == NEEDS_APPROVAL else "ro"
    # 目录挂进去，而不是挂单个文件 —— 挂文件的话 truncate 会失败
    # （文件被截断时它可能被替换，挂载点会失效）。
    return [(directory, directory, mode)]


def _deny(command: str, reason: str, hint: str = "") -> Decision:
    return Decision(
        decision=DENY, command=command, argv=[],
        reason=reason + (f"。{hint}" if hint else ""),
    )


def catalog() -> list:
    """给模型和审批界面看的命令清单。"""
    out = []
    for key, r in sorted(COMMANDS.items()):
        out.append({
            "key": key, "bin": r.bin, "usage": r.usage,
            "example": r.example,
            "decision": r.decision,
            "requires_approval": r.decision == NEEDS_APPROVAL,
            "isolation": r.isolation,
            "isolated": r.isolation == CHANNEL_CONTAINER,
            "note": r.note,
        })
    return out


def describe() -> dict:
    return {
        "whitelisted": len(COMMANDS),
        "allow": sum(1 for r in COMMANDS.values() if r.decision == ALLOW),
        "needs_approval": sum(1 for r in COMMANDS.values()
                              if r.decision == NEEDS_APPROVAL),
        "blocked_binaries": len(BLOCKED_BINARIES),
        "readable_dirs": list(_READABLE_DIRS),
        "writable_dirs": list(_WRITABLE_DIRS),
        # 走容器（真隔离）的：**写操作** —— 隔离的价值就在于约束能造成改动的操作
        "container_channel": sorted(k for k, r in COMMANDS.items()
                                    if r.isolation == CHANNEL_CONTAINER),
        # 走主机（无容器隔离）的：只读排查类 + 必须碰到 systemd/docker 的
        "host_channel_note": "只读排查类命令走主机通道：隔离对它们没有额外收益，"
                              "而挂载方案反而会限制可见范围或扩大暴露面",
        "host_channel": sorted(k for k, r in COMMANDS.items()
                               if r.isolation == CHANNEL_HOST),
    }
