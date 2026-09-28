# -*- coding: utf-8 -*-
"""
运维工具集 —— Agent 的「手」
============================================================
只做问答的 Agent 只会「说」。RAG 让它「知道」，但输出的仍然只是文字。
这一层给它手：能去查真实的磁盘、日志、服务状态、容器。

【三个可切换的后端】
    mock  —— 仿真数据。任何机器上 clone 下来都能跑通整条链路（默认）
    local —— 在本机执行只读命令（Linux）
    ssh   —— 通过 SSH 到真实远程主机执行只读命令

为什么必须做 mock：
    工具层如果一开始就依赖"真的连上一台机器"，那这个项目在别人电脑上
    就完全跑不起来 —— 别人 clone 下来什么都看不到。
    而 mock 数据让整条链路可复现：同样的输入，永远得到同样的输出。

为什么 local 和 ssh 共用同一套解析器：
    远端命令的输出格式和本机一样（都是 `df -h` / `docker ps` / `systemctl`）。
    所以「命令在哪执行」和「输出怎么解析」必须拆开：
    只有运输层不同，解析层共用一份代码 ——
    否则两种后端各写一套解析，迟早跑出不一致的结论，而且很难发现。

【安全三原则】—— 这是最容易被追问的地方
    1. 全部只读：没有任何写操作。不重启、不删除、不修改配置。
       诊断和处置必须分开 —— 处置是另一件事，需要人工确认。
    2. 参数白名单校验：绝不把用户/模型的字符串拼进 shell 命令。
       每个参数都过正则，不匹配直接拒绝。这是防命令注入的第一道墙。
    3. 风险分级：每个工具标 risk（low/medium/high）。
       调用方（Agent 循环）据此决定是否需要人工确认（已接入 HITL）。

【一句要记住的话】
    模型永远不执行任何东西。它只是"提出请求"。
    真正决定执不执行、要不要拦下来的，是这个文件里的代码。
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

# 自己加载 .env —— 不依赖别的模块"恰好先加载过"。
#
# ★ 这里踩过一次，值得记下来：
#   下面那些配置（BACKEND / SSH_TARGETS）都是**模块导入时**读一次，
#   而 .env 原先只由 llm.py / security.py / embedder.py 加载。
#   于是任何"先 import app.tools"的入口 —— 自检脚本、直接跑工具、
#   单独写的小测试 —— 都会读到空环境，静默退回 mock。
#   表现出来是「明明配了 OPS_BACKEND=ssh，却还在看仿真数据」，
#   而且**一个错都不报**，因为 mock 本来就是合法默认值。
#
#   教训：**配置的读取必须和使用它的代码绑在一起，不能靠 import 顺序。**
#   顺序这种东西没有任何东西在保证它。
#
# load_dotenv 默认不覆盖已存在的环境变量（override=False），
# 所以命令行上临时指定 `OPS_BACKEND=mock python ...` 仍然能盖过 .env ——
# 这正是回归测试需要的。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# ============================================================
# 零、后端选择
# ============================================================
# 默认 mock：保证在任何机器上都能跑通。
# 要查真机就把 OPS_BACKEND 设成 local（本机）或 ssh（远程）。
BACKEND = (os.getenv("OPS_BACKEND") or "mock").strip().lower()

# ---- ssh 后端配置 ----
# 设计要点：**逻辑主机名与真实地址解耦**。
# Agent 的"资产清单"里叫 web-01，它并不该知道 web-01 到底在哪 ——
# 那张映射表放在环境变量里，Agent 只能看到逻辑名。
# 这样做的两个好处：
#   1. 换机器只改配置，prompt / 评测集 / 文档一行都不用动
#   2. 模型无法"自己编一个 IP 去连"，它能选的只有清单里的名字
SSH_KEY = (os.getenv("OPS_SSH_KEY") or "").strip()
SSH_DEFAULT_USER = (os.getenv("OPS_SSH_USER") or "root").strip()
# 单条命令的执行超时 / 建连超时。分开设：建连慢和命令慢是两回事，
# 混在一起会让"连不上"和"命令卡死"报出同一个错，根本没法排查。
SSH_TIMEOUT = int(os.getenv("OPS_SSH_TIMEOUT") or "15")
SSH_CONNECT_TIMEOUT = int(os.getenv("OPS_SSH_CONNECT_TIMEOUT") or "8")


def _parse_ssh_targets(raw: str) -> dict:
    """解析 `名称=user@host[:port]`，逗号分隔。

    例：`OPS_SSH_TARGETS=web-01=root@1.2.3.4:22,db-01=ops@10.0.0.9`
    写成解析函数而不是直接 eval，是因为这份配置来自环境变量 ——
    和工具参数一样属于"外部输入"，不该有任何被解释执行的机会。
    """
    out = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, spec = item.split("=", 1)
        name, spec = name.strip(), spec.strip()
        user = SSH_DEFAULT_USER
        if "@" in spec:
            user, spec = spec.split("@", 1)
        host, _, port = spec.partition(":")
        if name and host:
            try:
                out[name] = {"user": user.strip() or SSH_DEFAULT_USER,
                             "host": host.strip(),
                             "port": int(port or 22)}
            except ValueError:
                # 这里抛 ValueError 而不是 ToolError：ToolError 定义在后面，
                # 而且配置写错属于"启动就该失败"，不该等到某次工具调用才暴露。
                raise ValueError(f"OPS_SSH_TARGETS 里的端口不是数字：{item!r}")
    return out


SSH_TARGETS = _parse_ssh_targets(os.getenv("OPS_SSH_TARGETS"))


# ============================================================
# 一、参数校验（防命令注入的第一道墙）
# ============================================================
# 只允许小写字母、数字、点、下划线、连字符。长度限死。
# 这样 "nginx; rm -rf /" 这种输入根本进不来。
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_.@-]{1,32}$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.-]{1,64}$")

# 已知主机清单。只允许查这几台 —— 模型不能自己编一个主机名去连。
_DEFAULT_HOSTS = ["web-01", "db-01", "cache-01"]


def _resolve_hosts() -> list:
    """决定"已知主机"是哪几台。

    优先级：
      1. OPS_HOSTS —— 显式清单（逗号分隔）
      2. ssh 后端下已经配了 SSH 目标的名字 —— 配置即清单，不用两处维护，
         而且能天然避免"配了却没生效"这种错
      3. 默认三台 —— mock 用的仿真主机，也是没配任何东西时的兜底

    默认仍然是原来那三台：**不配任何环境变量时，行为与之前完全一致。**
    """
    env = (os.getenv("OPS_HOSTS") or "").strip()
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    if BACKEND == "ssh" and SSH_TARGETS:
        return list(SSH_TARGETS)
    return list(_DEFAULT_HOSTS)


KNOWN_HOSTS = _resolve_hosts()

# 主机角色说明。只是给模型的语义提示，让它能把「网站访问慢」对上某台主机。
# 是可选的：清单里换成别的名字照样工作，只是少一点语义线索。
_HOST_ROLES = {"web-01": "Web 服务器", "db-01": "数据库", "cache-01": "缓存"}


def host_list_text() -> str:
    """把已知主机清单渲染成给模型看的一句话。

    ★ 从 KNOWN_HOSTS 生成，而不是写死在 prompt 里。
      写死的话，换机器时得同时改代码、prompt、MCP 工具描述、文档四处 ——
      漏掉任意一处，就会出现「prompt 说只有三台、配置里却是别的」这种矛盾，
      而模型会照着 prompt 里的错清单去猜主机名。
    """
    return "、".join(
        f"{h}（{_HOST_ROLES[h]}）" if h in _HOST_ROLES else h
        for h in KNOWN_HOSTS
    )


class ToolError(Exception):
    """工具层错误。

    单独定义一个异常类型，是为了让上层能区分：
    「工具参数不合法」（该让模型改参数重试）
    「工具执行失败」（该换一个工具或转人工）
    """


def _check_host(host: str) -> str:
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise ToolError(f"主机名不合法：{host!r}")
    if host not in KNOWN_HOSTS:
        raise ToolError(f"未知主机：{host}。已知主机：{KNOWN_HOSTS}")
    return host


def _check_service(service: str) -> str:
    if not isinstance(service, str) or not _SERVICE_RE.match(service):
        raise ToolError(f"服务名不合法：{service!r}")
    return service


# ============================================================
# 二、mock 后端：仿真数据
# ============================================================
# 设计成「一眼能看出问题」的场景，这样 Agent 的诊断过程才有意义：
#   web-01 —— 磁盘 96% 满 + nginx 报 upstream timed out（就是知识库里的案例）
#   db-01  —— 一切正常（用来验证 Agent 不会"没问题也硬找出问题"）
#   cache-01 —— 容器反复重启（对应知识库里的 CrashLoop 案例）
_MOCK = {
    "web-01": {
        "hostname": "web-01",
        "ip": "10.0.1.11",
        "disk": [
            {"filesystem": "/dev/vda1", "mount": "/", "size": "40G",
             "used": "38.4G", "use_percent": 96},
            # ★ 这一条原来写的是 `/var`，是错的 —— 被 Agent 逮住了。
            #
            #   原来的数据：`/` 96% 满，`/var` 35%。而 nginx 日志在
            #   /var/log/nginx，属于 /var 分区 —— 也就是说
            #   **"清理 nginx 日志"这个动作解决不了根分区满的问题**，
            #   日志报的 no space left on device 是受害者而不是原因。
            #
            #   处置 Agent 从诊断依据里读出了这个矛盾，明确拒绝了清理动作，
            #   还写了「不要批准清理 /var/log/nginx —— 它解决不了根分区满的问题」。
            #
            #   **这说明处置环节的"必须基于诊断依据"这条约束是有效的**：
            #   如果它只是照着用户那句"帮我把日志清了"去做，
            #   就会执行一个没用的动作，而且看起来还挺合理。
            #
            #   数据本身是错的（跟知识库的 disk-full 案例也对不上），所以改掉：
            #   把第二块盘改成 /data，让 /var/log 落在根分区上，
            #   这样"根分区满 → nginx 写不了日志 → 502"这条因果链才成立。
            #
            #   ★ 顺便记一条经验：**mock 数据也要过一致性检查。**
            #     数据里凡是"能被推理出来的关系"，都要和真实场景自洽，
            #     否则要么误导 Agent，要么暴露设计漏洞。
            {"filesystem": "/dev/vda2", "mount": "/data", "size": "60G",
             "used": "21.0G", "use_percent": 35},
        ],
        "services": {
            "nginx": {"active": "active", "since": "3 days ago",
                      "restarts": 0, "pid": 1183},
            "docker": {"active": "active", "since": "12 days ago",
                       "restarts": 0, "pid": 892},
        },
        "containers": [
            {"name": "wp-app", "image": "wordpress:6.4", "status": "Up 3 days",
             "ports": "0.0.0.0:8080->80/tcp", "restarts": 0},
            {"name": "wp-db", "image": "mariadb:10.11", "status": "Up 3 days",
             "ports": "3306/tcp", "restarts": 0},
        ],
        "logs": {
            "nginx": [
                "2026/09/26 09:12:03 [error] 1183#1183: *8821 upstream timed out "
                "(110: Connection timed out) while reading response header from upstream, "
                "client: 10.0.1.50, server: www.example.com, "
                "request: \"GET /wp-admin/ HTTP/1.1\", upstream: \"http://127.0.0.1:8080/wp-admin/\"",
                "2026/09/26 09:12:31 [error] 1183#1183: *8823 upstream timed out "
                "(110: Connection timed out) while reading response header from upstream, "
                "client: 10.0.1.51, upstream: \"http://127.0.0.1:8080/\"",
                "2026/09/26 09:13:02 [error] 1183#1183: *8827 no space left on device "
                "while writing to /var/log/nginx/access.log",
                "2026/09/26 09:13:02 [alert] 1183#1183: could not open error log file",
            ],
            "docker": [
                "time=\"2026-09-26T09:13:05Z\" level=warning msg=\"container wp-db "
                "failed to write log: no space left on device\""
            ],
        },
        "load": {"load1": 8.42, "load5": 6.90, "load15": 3.10,
                 "cpu_cores": 2,
                 "mem_total_mb": 3548, "mem_used_mb": 3210, "mem_avail_mb": 338,
                 "top_cpu": [{"name": "mariadbd", "cpu": 62.4, "mem": 41.2},
                             {"name": "nginx", "cpu": 8.1, "mem": 1.4}]},
    },
    "db-01": {
        "hostname": "db-01",
        "ip": "10.0.1.21",
        "disk": [
            {"filesystem": "/dev/vdb1", "mount": "/", "size": "80G",
             "used": "31.2G", "use_percent": 41},
            {"filesystem": "/dev/vdb2", "mount": "/var/lib/mysql", "size": "200G",
             "used": "88.0G", "use_percent": 44},
        ],
        "services": {
            "mysql": {"active": "active", "since": "15 days ago",
                      "restarts": 0, "pid": 2210},
        },
        "containers": [
            {"name": "zabbix-mysql", "image": "mysql:8.0", "status": "Up 15 days",
             "ports": "3306/tcp", "restarts": 0},
        ],
        "logs": {
            "mysql": ["2026-09-26T09:00:00.123456Z 0 [Note] InnoDB: "
                      "Buffer pool(s) load completed at 260926  9:00:00"],
        },
        "load": {"load1": 0.42, "load5": 0.51, "load15": 0.48,
                 "cpu_cores": 4,
                 "mem_total_mb": 7900, "mem_used_mb": 3120, "mem_avail_mb": 4780,
                 "top_cpu": [{"name": "mysqld", "cpu": 12.0, "mem": 28.5}]},
    },
    "cache-01": {
        "hostname": "cache-01",
        "ip": "10.0.1.31",
        "disk": [
            {"filesystem": "/dev/vdc1", "mount": "/", "size": "40G",
             "used": "12.1G", "use_percent": 32},
        ],
        "services": {
            "docker": {"active": "active", "since": "1 day ago",
                       "restarts": 0, "pid": 640},
        },
        "containers": [
            {"name": "redis-cache", "image": "redis:7-alpine",
             "status": "Restarting (1) 8 seconds ago",
             "ports": "6379/tcp", "restarts": 47},
        ],
        "logs": {
            "docker": [
                "2026-09-26T09:10:11Z ERROR: redis-cache exited with code 1",
                "2026-09-26T09:10:20Z WARNING: Container redis-cache is restarting, "
                "restart count=47",
            ],
            "redis": ["1:M 26 Sep 2026 09:10:11.123 # Fatal error, "
                      "can't open the append-only file: Permission denied"],
        },
        "load": {"load1": 1.10, "load5": 0.95, "load15": 0.80,
                 "cpu_cores": 2,
                 "mem_total_mb": 1774, "mem_used_mb": 610, "mem_avail_mb": 1164,
                 "top_cpu": [{"name": "redis-server", "cpu": 45.3, "mem": 2.1}]},
    },
}


def _mock(host: str) -> dict:
    return _MOCK[host]


# ============================================================
# 三、真机执行通道：local（本机）/ ssh（远程）
# ============================================================
# 只允许固定的命令模板。注意没有一处是把参数拼进 shell 字符串 ——
# subprocess 传的是"列表"，等于告诉内核"这是参数列表，不是一段命令"，
# shell 根本没机会解释其中的分号、管道、反引号。
#
# shell=False 是关键：如果写成 shell=True，白名单就形同虚设。
#
# ★ SSH 有一个细节必须写清楚，否则容易误判安全性：
#   `ssh 主机 命令 参数` 里的"命令 + 参数"最终会被**远端的 shell** 解释。
#   也就是说远端那一层 shell 是客观存在的，约束不在本地。
#   两道防线，缺一不可：
#     1. 参数来源受限：能进这里的东西只允许来自固定模板或已过正则校验
#        （`service` 过 _SERVICE_RE、`lines` 转 int、日志路径从固定候选表里挑）
#     2. 参数逐项加引号（见 _remote_quote）—— 把参数边界重新画出来
def _remote_quote(arg) -> str:
    """把一个参数包成"远端 shell 不会动它"的形式。

    ★ 为什么必须做这一步（实测踩过，不是理论担忧）：
      subprocess 传列表时，本地没有 shell，参数边界天然安全。
      但一旦跨过 SSH，**远端那个 shell 又回来了**：
        `ssh 主机 docker ps --format {{.Names}}\\t{{.Status}}`
      远端 shell 会把未加引号的 `\\t` 解释掉，只剩一个字母 `t` ——
      docker 拿到的格式串就废了，实测输出变成
      `agentdesktagentdesk:1.0.0tUp 4 hours`（分隔符只剩 t）。
      所以过了 SSH 这一跳，必须**重新把参数边界画出来**。
      顺带也堵住了分号、管道、通配符被远端解释的可能。
    """
    return "'" + str(arg).replace("'", "'\\''") + "'"


def _remote_command(cmd: list) -> str:
    """把参数列表拼成一条"逐参数已加引号"的远端命令。"""
    return " ".join(_remote_quote(a) for a in cmd)


def _exec(cmd: list, host: str = None, timeout: int = None) -> tuple:
    """把一条命令送到"它该去的地方"执行。返回 (rc, 输出文本)。

    这是唯一的运输层出口。上面的解析器只调用它，
    不关心命令究竟跑在本机还是远端 —— 这就是两种后端能共用解析器的原因。
    """
    if BACKEND == "ssh":
        target = SSH_TARGETS.get(host or "")
        if not target:
            raise ToolError(
                f"主机 {host!r} 没有配置 SSH 目标。请在 OPS_SSH_TARGETS 里补上"
                f"（当前已配置：{list(SSH_TARGETS) or '空'}）"
            )
        argv = [
            "ssh",
            # BatchMode=yes：绝不弹密码提示。自动化里一旦弹提示就是永久挂起，
            # 比直接报错难查得多 —— 宁可立刻失败。
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            # 首次连接自动记录指纹，之后严格校验。
            # 这样既有首次的便利，又保留了"主机指纹变了必须报警"的能力。
            "-o", "StrictHostKeyChecking=accept-new",
            # ssh 客户端自己的提示（例如服务端不支持抗量子密钥交换的告警）
            # 对"这台机器怎么了"这个判断毫无价值，全是噪音。压到 ERROR。
            "-o", "LogLevel=ERROR",
            "-p", str(target["port"]),
        ]
        if SSH_KEY:
            argv += ["-i", SSH_KEY]
        argv.append(f"{target['user']}@{target['host']}")
        # ★ 远端命令作为**单个参数**传入，且已逐参数加引号 —— 见 _remote_quote
        argv.append(_remote_command(cmd))
        limit = timeout or SSH_TIMEOUT
    else:
        argv = cmd
        limit = timeout or 10

    try:
        proc = subprocess.run(
            argv, shell=False, timeout=limit,
            stdout=subprocess.PIPE,
            # ★ stderr 绝不与 stdout 合并。
            #   ssh 客户端自己的诊断也走 stderr，一旦合并它就成了"输出的一部分"：
            #   实测 `df` 的解析器把告警句里的 "may be vulnerable"
            #   当成了 Use% 列，直接崩在 `int('be')`；
            #   `nproc` 则是 int() 失败后静默降级成 1 核。
            #   这两个错误的共同点是：**看起来像数据异常，其实是运输层污染了数据。**
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise ToolError("找不到 ssh 命令 —— 请确认已安装 OpenSSH 客户端")
    except subprocess.TimeoutExpired:
        raise ToolError(f"命令超时（{limit}s）：{' '.join(cmd)}")

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and err:
        # 命令失败时才把 stderr 附上 —— 这时它多半是真正的错误信息
        # （command not found / permission denied），对排查有用。
        # 成功时丢弃：成功了还吐 stderr 的，基本只剩客户端告警。
        out = (out + "\n" + err).strip() if out else err
    return proc.returncode, out


def _exists(host: str, path: str) -> bool:
    """判断某个文件是否存在。

    ★ 这是切换到远端时最容易漏掉的一类操作：
      local 用 os.path.exists 一次系统调用就够，
      ssh 必须真的去远端问一次 `test -f`。
      如果这里偷懒还调本地的 os.path.exists，
      远端日志会永久报"找不到文件"，而且看起来毫无问题 ——
      因为本机确实没有那个路径。
    """
    if BACKEND == "ssh":
        rc, _ = _exec(["test", "-f", path], host=host)
        return rc == 0
    return os.path.exists(path)


def _run_df(host: str) -> list:
    # -P 是 POSIX 输出格式：**保证一行一个文件系统**。
    # 不加 -P 时，超长的挂载点会被折到下一行，解析器把续行当成新记录，
    # 列就整体串位了 —— 这类问题只在挂载点特别长的机器上才出现，
    # 本地测永远碰不到。
    _, out = _exec(["df", "-hP"], host=host)
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        # 只认「第 5 列长得像百分比」的行。
        # 防御性检查：不同发行版、不同挂载命名都可能让列数变化，
        # 与其让 int() 抛异常把整个工具打挂，不如跳过这一行 ——
        # **一个工具不该因为一条解析不了的行就整个失效。**
        if len(parts) >= 6 and re.fullmatch(r"\d+%", parts[4]):
            rows.append({
                "filesystem": parts[0], "size": parts[1], "used": parts[2],
                "avail": parts[3],
                "use_percent": int(parts[4].rstrip("%") or 0),
                "mount": parts[5],
            })
    return rows


# 伪文件系统：不是真实磁盘。
# 实测在一台跑 Docker 的机器上，`df -h` 有 12 行，其中 9 行是容器的
# overlay 挂载和 tmpfs —— 它们全部指向同一块物理盘。
# 把它们的"使用率"算进结论，只会让判断落到与用户无关的分区上。
_PSEUDO_FS = ("tmpfs", "devtmpfs", "overlay", "squashfs", "ramfs", "nsfs", "shm")


def _is_pseudo(fs) -> bool:
    name = str(fs or "").strip()
    if not name or name == "none":
        return True
    return name.startswith(_PSEUDO_FS)


def _run_service(host: str, service: str) -> dict:
    _, active = _exec(["systemctl", "is-active", service], host=host)
    _, detail = _exec(["systemctl", "status", "--no-pager", "-l", service],
                      host=host)
    return {"active": active, "detail": detail[:2000]}


def _run_containers(host: str) -> list:
    _, out = _exec(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"],
        host=host,
    )
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"name": parts[0], "image": parts[1], "status": parts[2]})
    return rows


# 日志来源，按这个顺序试（顺序有理由，不是随便排的）：
#   1. systemd 日志 —— 现代 Linux 上最通用：服务日志不一定落盘到文件，
#      但 systemd 一定有记录，而且不用事先知道路径
#   2. 常见日志文件 —— 覆盖不接 journal 的服务和传统部署方式
#   3. 容器日志 —— 这台机器上真正干活的服务器全跑在容器里，
#      journalctl 里只有 docker daemon 自己的日志，
#      看不到容器内部发生了什么
#
# 每条来源都把 `source` 一起返回：同一批日志可能来自三个不同的地方，
# 不写清来源，读的人（和模型）就没法判断这条证据的覆盖范围有多大。
_LOG_FILE_CANDIDATES = [
    "/var/log/{service}/error.log",
    "/var/log/{service}.log",
    "/var/log/{service}/current",
]


def _run_logs(host: str, service: str, lines: int) -> dict:
    """取某个服务的日志。返回 {"source": 来源描述, "lines": [...]}。"""
    # ---- 1. systemd ----
    # `--no-pager` 必须加：不加会去调分页器，
    # 在非交互的 SSH 会话里分页器读不到终端，表现是"命令卡住"。
    rc, out = _exec(
        ["journalctl", "-u", service, "-n", str(lines), "--no-pager"], host=host)
    if rc == 0 and out and "No entries" not in out:
        # journalctl 会在开头插一行 `-- Logs begin at ...`，
        # 那是它自己的表头，不是日志内容，去掉。
        kept = [ln for ln in out.splitlines() if not ln.startswith("-- ")]
        if kept:
            return {"source": f"journalctl -u {service}", "lines": kept}

    # ---- 2. 日志文件 ----
    # 路径不交给调用方指定 —— 只在这张表里查。
    # 让外部传路径，就等于给了它读任意文件的权限。
    for tpl in _LOG_FILE_CANDIDATES:
        path = tpl.format(service=service)
        if _exists(host, path):
            _, out = _exec(["tail", "-n", str(lines), path], host=host)
            return {"source": path, "lines": out.splitlines()}

    # ---- 3. 容器日志 ----
    # ★ 这里用 `sh -c` 是有原因的：`docker logs` 把容器的 stderr
    #   写到自己的 stderr 上，而我们的运输层**刻意不合并 stderr**
    #   （原因见 _exec 的注释）。所以必须显式 `2>&1` 把两股流合起来，
    #   否则容器里真正有用的报错会全部丢掉。
    #   参数用的是 _exec 里的同一套引号规则，没有引入新的注入面。
    rc, out = _exec(
        ["sh", "-c",
         "docker logs --tail {} {} 2>&1".format(int(lines), _remote_quote(service))],
        host=host)
    if rc == 0 and out:
        return {"source": f"docker logs {service}", "lines": out.splitlines()}

    raise ToolError(
        f"取不到 {service} 的日志。已试过：systemd 单元、"
        f"常见日志文件（{[t.format(service=service) for t in _LOG_FILE_CANDIDATES]}）、"
        f"同名容器。若它是容器，请直接用容器名（例如 wp-app、zabbix-server）。")


# ---- 负载与内存：远端拿回来的是原文，必须在这里解析 ----
# ★ 之前 local 分支只把 `uptime` / `free` 的原文原样塞回给模型让它自己读。
#   那是偷懒：解析是确定性工作，交给模型做只会多一个出错点，
#   而且 `load_per_core` 算不出来，告警等级会永远是 unknown ——
#   等于"接了真机却给不出结论"。
_UPTIME_RE = re.compile(r"load average:\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)")


def _parse_uptime(text: str) -> dict:
    """从 `uptime` 输出里取 1/5/15 分钟负载。"""
    m = _UPTIME_RE.search(text or "")
    if not m:
        return {}
    return {"load1": float(m.group(1)), "load5": float(m.group(2)),
            "load15": float(m.group(3))}


def _parse_free(text: str) -> dict:
    """解析 `free -m` 的 Mem 行，单位 MB。

    ★ available 取第 7 列（内核估算的"真正可用"），而不是第 3 列 free。
      两者差得很远：free 不含可回收的 buff/cache，
      用它判断内存够不够会得出"内存快没了"的错误结论。
    """
    for line in (text or "").splitlines():
        if line.strip().startswith("Mem:"):
            nums = re.findall(r"\d+", line)
            if len(nums) >= 3:
                total, used = int(nums[0]), int(nums[1])
                avail = int(nums[6]) if len(nums) >= 7 else int(nums[2])
                return {"mem_total_mb": total, "mem_used_mb": used,
                        "mem_avail_mb": avail}
    return {}


def _run_load(host: str) -> dict:
    _, cores = _exec(["nproc"], host=host)
    _, uptime_out = _exec(["uptime"], host=host)
    _, free_out = _exec(["free", "-m"], host=host)
    try:
        cores_n = int(str(cores).strip() or 1)
    except ValueError:
        cores_n = 1
    out = {"cpu_cores": cores_n or 1}
    out.update(_parse_uptime(uptime_out))
    out.update(_parse_free(free_out))
    # 原文一起返回：解析可能失败（不同发行版格式有差异），
    # 留着原文至少能让人看出"是格式没匹配上"，而不是当成"机器没有负载"。
    out["raw_uptime"] = uptime_out
    out["raw_free"] = free_out
    return out


# ============================================================
# 四、六个工具
# ============================================================
def check_disk(host: str = "web-01") -> dict:
    """查看磁盘使用率。磁盘满是运维故障里出现频率最高的一类。"""
    host = _check_host(host)
    if BACKEND in ("local", "ssh"):
        partitions = _run_df(host)
    else:
        partitions = _mock(host)["disk"]

    # 只对「真实磁盘」下结论：tmpfs / overlay / devtmpfs 不是磁盘，
    # 而且在一台跑 Docker 的机器上它们会占掉大半行数 ——
    # 交给模型既浪费 token，也容易把它的注意力带偏。
    # 过滤掉多少条如实报出来，不藏着（**过滤要透明，不能让调用方以为那就是全部**）。
    real = [p for p in partitions if not _is_pseudo(p["filesystem"])]
    # 兜底：万一全是伪文件系统（比如容器内跑 local 后端），
    # 不能给出"没有磁盘"这种结论，退回全量。
    judged = real or partitions

    # 顺手算出最高使用率 —— 让模型不用自己在脑子里比大小。
    # 这是"工具该做的事"：把原始数据加工成结论，减少模型的推理负担和出错机会。
    worst = max(judged, key=lambda p: p["use_percent"]) if judged else None
    return {
        "host": host,
        "backend": BACKEND,
        "partitions": judged,
        "partitions_filtered": len(partitions) - len(judged),
        "max_use_percent": worst["use_percent"] if worst else None,
        "max_mount": worst["mount"] if worst else None,
        # 阈值判断放在工具里而不是 Prompt 里：阈值是运维标准（行业知识），
        # 不是模型的常识。放在代码里才能改、才能测、才能被 review。
        "level": ("critical" if worst and worst["use_percent"] >= 90
                  else "warning" if worst and worst["use_percent"] >= 80
                  else "ok"),
    }


def check_load(host: str = "web-01") -> dict:
    """查看负载、CPU 核数、内存占用。用来判断"是不是被压垮了"。"""
    host = _check_host(host)
    if BACKEND in ("local", "ssh"):
        load = _run_load(host)
    else:
        load = dict(_mock(host)["load"])

    # 负载高不高的判断标准是「每个核上跑了多少任务」，不是负载绝对值。
    # 4 核上 8.0 是过载，16 核上 8.0 很轻松 —— 模型经常算错这个，
    # 所以由工具直接给出结论。
    cores = load.get("cpu_cores") or 1
    load1 = load.get("load1")
    if load1 is None:
        per_core = None
        level = "unknown"
    else:
        per_core = round(load1 / cores, 2)
        level = ("critical" if per_core >= 2 else
                 "warning" if per_core >= 1 else "ok")
    return {"host": host, "backend": BACKEND, "load_per_core": per_core,
            "level": level, **load}


def check_service(host: str, service: str) -> dict:
    """查看 systemd 服务是否在运行。"""
    host = _check_host(host)
    service = _check_service(service)
    if BACKEND in ("local", "ssh"):
        return {"host": host, "service": service, "backend": BACKEND,
                **_run_service(host, service)}

    svc = _mock(host)["services"].get(service)
    if svc is None:
        return {"host": host, "service": service, "backend": BACKEND,
                "active": "not-found",
                "hint": f"该主机上没有名为 {service} 的服务"}
    return {"host": host, "service": service, "backend": BACKEND, **svc}


def list_containers(host: str = "web-01") -> dict:
    """列出容器及其状态。能看出反复重启（Restarting + 重启次数高）。"""
    host = _check_host(host)
    if BACKEND in ("local", "ssh"):
        items = _run_containers(host)
    else:
        items = _mock(host)["containers"]

    for c in items:
        status = str(c.get("status", ""))
        c["restarting"] = status.startswith("Restarting") or c.get("restarts", 0) > 5
    return {"host": host, "backend": BACKEND, "containers": items,
            "restarting_count": sum(1 for c in items if c["restarting"])}


def tail_log(host: str, service: str, lines: int = 20) -> dict:
    """读取服务日志的尾部。这是定位故障原因最关键的一步。"""
    host = _check_host(host)
    service = _check_service(service)
    # lines 必须转成 int 再限范围。如果直接用它拼命令，传个 "20; rm -rf /" 就出事。
    lines = max(1, min(int(lines), 200))

    source = "mock 仿真数据"
    if BACKEND in ("local", "ssh"):
        got = _run_logs(host, service, lines)
        entries, source = got["lines"], got["source"]
    else:
        entries = _mock(host)["logs"].get(service)
        if entries is None:
            return {"host": host, "service": service, "backend": BACKEND,
                    "lines": [], "count": 0,
                    "hint": f"{host} 上没有 {service} 的日志"}

    entries = entries[-lines:]

    # 关键词扫描：把"日志里有哪些已知错误模式"直接标出来。
    # 这是把运维经验编码进工具 —— 比让模型通读日志找线索可靠得多。
    patterns = {
        "no space left on device": "磁盘空间耗尽",
        "upstream timed out": "上游服务响应超时",
        "Connection refused": "连接被拒绝（服务没监听）",
        "OOMKilled": "被内存溢出杀掉",
        "Permission denied": "权限不足",
        "Too many connections": "连接数超限",
        "exit code 137": "进程被 SIGKILL（多半是 OOM 或人工 kill）",
        "exited with code 1": "进程异常退出",
    }
    text = "\n".join(entries)
    found = [{"pattern": k, "meaning": v} for k, v in patterns.items()
             if k.lower() in text.lower()]
    return {"host": host, "service": service, "backend": BACKEND,
            # 来源必须写清楚：同一批日志可能来自 journalctl、某个文件、
            # 或容器日志，覆盖范围完全不同。
            "source": source,
            "count": len(entries), "lines": entries,
            "matched_patterns": found,
            "hint": ("日志中命中已知错误模式：" +
                     "、".join(f["meaning"] for f in found)) if found else None}


def search_knowledge(query: str, top_k: int = 3) -> dict:
    """检索运维知识库（RAG）。

    ★ 这是一个特殊的工具：它不是"去看现场"，而是"去查经验"。

    把 RAG 做成 Agent 的一个工具，而不是把检索结果一股脑塞进 Prompt，
    是「Agent 化 RAG」和「传统 RAG 问答」的分水岭：
      - 传统 RAG：不管什么问题都先检索一遍，检索结果全塞给模型
      - Agent 化：模型自己判断"这题我需不需要查资料、该用哪个词查"
    后者更省 token，也更适合"既要查现场、又要查经验"的混合任务。
    """
    from app.rag.pipeline import load_store    # 延迟导入，避免循环依赖

    top_k = max(1, min(int(top_k), 10))
    store = load_store()
    hits = store.search(query, top_k=top_k, mode="hybrid")
    return {
        "query": query,
        "count": len(hits),
        "results": [
            {"source": h["source"], "preview": h["preview"],
             "score": round(float(h.get("score", 0)), 4)}
            for h in hits
        ],
    }


# ============================================================
# 四·五、第七个工具：run_command —— Agent 第一次有了「能做改动」的手
# ============================================================
# ★ 前面六个工具全是只读的。这一个不一样，所以它走的路也不一样：
#
#     机制                 作用
#     ────────────────────────────────────────────────────────
#     policy.decide()      白名单 + 参数级校验 + 三维决策
#                          不在白名单 → 直接拒；写操作 → 要求审批
#     approvals            生成审批单，等人点头
#     executor.run()       一次性容器 / 主机通道执行
#     审计                 审批人、时间、命令、结果全部留痕
#
# 没有这四层，这个工具就不该存在 —— 一个没有约束的"执行任意命令"工具，
# 等于把服务器交出去。
def run_command(command: str, purpose: str = "") -> dict:
    """在沙箱里执行一条白名单命令。

    只允许白名单里的命令；**写操作会生成审批单，需要人工确认后才执行**。
    只读诊断请优先用 check_disk / tail_log 这类专用工具 ——
    它们返回的是结构化数据，比你自己解析命令输出更可靠。

    【这个函数的每一段都在做同一件事：把"模型想干什么"和"实际能干什么"分开】

        模型说：我要执行 `truncate -s 0 /var/log/nginx/error.log`
          ↓ 策略说：允许这个**动作形态**（truncate 白名单里，路径在 /var/log 下，是 .log）
                 但它是写操作 → 需要人确认
          ↓ 审批说：现在还没人确认 → 生成单子，不执行
          ↓ 结果：模型拿到"已提交审批"，而不是"执行成功"

    即使模型在 purpose 里写"这是紧急情况不用确认"，也**不会改变决策** ——
    purpose 只是给人看的说明，不参与任何判定。
    这一点很重要：**如果模型的输入能影响安全决策，那安全决策就等于没有。**
    """
    from app.sandbox import approvals, executor, policy

    decision = policy.decide(command)

    base = {
        "command": decision.command or str(command or "").strip(),
        "purpose": str(purpose or "")[:200],
        "decision": decision.decision,
        "rule": decision.rule_key,
        "isolation": decision.isolation,
        "risk": decision.risk,
        "reason": decision.reason,
        "executed": False,
    }

    # ---- 闸门零：ssh 后端下，执行通道整体关闭（fail-closed）----
    # 为什么必须关掉，而不是"顺手让它在本机执行"：
    #   诊断走的是远端（OPS_BACKEND=ssh），但沙箱的执行通道只会在**本机**落地
    #   （见 sandbox/executor.py 的 host 通道，基于本地 subprocess）。
    #   于是就成了：模型对着远端机器下结论，命令却打在本机上 ——
    #   打错的机器和打对的机器只差一个环境变量，这种错误不该靠"记得改"来避免。
    #
    #   **宁可让处置能力不可用，也不要执行到错误的机器上。**
    #   这是和沙箱"探测不到 docker 不降级"同一条原则：
    #   安全属性的缺失要立刻暴露，不能悄悄降级。
    if BACKEND == "ssh":
        base["error"] = "ssh 后端下不提供执行能力"
        base["hint"] = (
            "当前 OPS_BACKEND=ssh，工具层查的是远端主机；"
            "而沙箱的执行通道目前只在本机落地。"
            "「诊断在远端、执行在本地」的错配比「不能执行」危险得多，"
            "所以这里直接关闭。"
            "需要真实处置时，请在目标主机上以 OPS_BACKEND=local 运行，"
            "或改用 check_disk / tail_log 等只读工具先拿到现场数据。"
        )
        return base

    # ---- 情况一：策略拒绝 ----
    if decision.decision == policy.DENY:
        # 「拒绝」也是一种结果，要原样返回给模型 —— 它需要知道为什么、
        # 以及可以改用什么。抛异常就丢掉这些信息了。
        base["error"] = decision.reason
        base["hint"] = "这条命令不允许执行。请改用白名单里的等价做法，或优先使用专用工具。"
        return base

    # ---- 情况二：需要人工确认 ----
    if decision.decision == policy.NEEDS_APPROVAL:
        rec = approvals.store().create(
            command=decision.command,
            fingerprint=decision.fingerprint,
            rule=decision.rule_key,
            risk=decision.risk,
            isolation=decision.isolation,
            reason=decision.reason,
            tool="run_command",
        )
        base["approval_id"] = rec["id"]
        base["expires_at"] = rec["expires_at"]
        base["hint"] = ("已提交人工审批，尚未执行。请在结果里告诉用户："
                        f"需有人确认后才会执行（审批单 {rec['id']}）。"
                        "不要假装已经执行完成。")
        return base

    # ---- 情况三：只读命令，直接执行 ----
    result = executor.run(decision)
    base["executed"] = True
    base["result"] = result.to_dict()
    if not result.ok:
        base["error"] = result.error
    return base


# ============================================================
# 五、工具注册表
# ============================================================
# 一张表把「函数 / 给模型看的 schema / 风险等级」绑在一起。
# 加新工具只要在这里加一条，Agent 循环、HTTP 接口、文档都不用改。
#
# risk 的作用：Agent 循环遇到非 low 的工具会记录下来，
# 上线时接 HITL（人工确认）就靠这个字段。
TOOLS = {
    "check_disk": {
        "func": check_disk,
        "risk": "low",
        "desc": "查看主机各分区磁盘使用率，并给出是否告警的判断",
        "params": {
            "host": {"type": "string", "desc": "主机名，如 web-01",
                     "required": False},
        },
    },
    "check_load": {
        "func": check_load,
        "risk": "low",
        "desc": "查看主机负载、CPU 核数与内存占用，判断是否过载",
        "params": {
            "host": {"type": "string", "desc": "主机名，如 web-01",
                     "required": False},
        },
    },
    "check_service": {
        "func": check_service,
        "risk": "low",
        "desc": "查看指定 systemd 服务是否在运行",
        "params": {
            "host": {"type": "string", "desc": "主机名", "required": True},
            "service": {"type": "string", "desc": "服务名，如 nginx、mysql",
                        "required": True},
        },
    },
    "list_containers": {
        "func": list_containers,
        "risk": "low",
        "desc": "列出主机上的容器及其状态，可发现反复重启的容器",
        "params": {
            "host": {"type": "string", "desc": "主机名", "required": False},
        },
    },
    "tail_log": {
        "func": tail_log,
        "risk": "low",
        "desc": "读取服务日志尾部，并自动标注命中的已知错误模式",
        "params": {
            "host": {"type": "string", "desc": "主机名", "required": True},
            "service": {"type": "string", "desc": "服务名", "required": True},
            "lines": {"type": "integer", "desc": "读取行数，默认 20，最大 200",
                      "required": False},
        },
    },
    "search_knowledge": {
        "func": search_knowledge,
        "risk": "low",
        "desc": "检索运维知识库（历史故障处理经验、操作手册）",
        "params": {
            "query": {"type": "string", "desc": "检索关键词或问题",
                      "required": True},
            "top_k": {"type": "integer", "desc": "返回条数，默认 3",
                      "required": False},
        },
    },
    # ★ 唯一一个非只读的工具，所以它也是唯一一个 risk 为 high 的。
    #
    #   注意 risk 的语义在这里变了：
    #       前六个工具的 risk 描述的是"这个工具本身有没有副作用"
    #       这一个的 risk=high 描述的是"**这个工具的最高可能风险**"
    #       —— 它既能跑只读命令（无副作用），也能提交写操作（有副作用），
    #          所以从"最坏情况"来看它是 high。
    #
    #   **风险标注要按最坏情况标，不能按典型情况标。**
    #   一个"平时都很安全"的通道，出事的恰恰是那 1% 的情况。
    "run_command": {
        "func": run_command,
        "risk": "high",
        "desc": ("在沙箱里执行一条白名单命令。写操作（如重启服务、清理日志）"
                 "会先提交人工审批，批准后才执行。"
                 "只读诊断请优先用专用工具（check_disk / tail_log 等），"
                 "它们返回结构化数据，更可靠"),
        "params": {
            "command": {"type": "string",
                        "desc": "要执行的命令，必须是白名单里的形式。"
                                "例如 `systemctl restart nginx`、"
                                "`truncate -s 0 /var/log/nginx/error.log`、"
                                "`tail -n 50 /var/log/nginx/error.log`",
                        "required": True},
            "purpose": {"type": "string",
                        "desc": "为什么要执行它（一句话，会展示给审批人看）",
                        "required": False},
        },
    },
}


def tool_schemas() -> list:
    """转成 OpenAI 兼容的 tools 参数 —— 这就是模型看到的「工具清单」。

    注意：模型看到的只有这里的内容。它不知道函数体怎么实现、
    也不会因为参数写得难看就报错 —— schema 写得好不好，直接决定它用得对不对。
    """
    schemas = []
    for name, meta in TOOLS.items():
        props, required = {}, []
        for pname, pmeta in meta["params"].items():
            props[pname] = {"type": pmeta["type"], "description": pmeta["desc"]}
            if pmeta.get("required"):
                required.append(pname)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": meta["desc"],
                "parameters": {"type": "object", "properties": props,
                               "required": required},
            },
        })
    return schemas


def tool_catalog() -> list:
    """给 HTTP 接口 / 前端看的工具清单（带风险等级）。"""
    return [{"name": name, "risk": meta["risk"], "desc": meta["desc"],
             "params": list(meta["params"].keys())}
            for name, meta in TOOLS.items()]


def execute_tool(name: str, args: dict) -> dict:
    """执行一个工具。返回值永远是可 JSON 序列化的 dict。

    ★ 观测包装：每次工具调用都记一个 span，含参数摘要与结果状态。
      用包装而不是改函数体 —— 这个函数里有六处早退分支，
      每处都补 set_error 的话迟早漏一处。
      **一层包装统一处理，比在六处分别处理可靠。**

    【为什么这里必须 try 住所有异常】
    工具是模型"点名"调用的，参数由模型生成 —— 也就是说，
    **输入是不可信的**。参数写错是常态，不是异常情况。
    如果把异常抛出去，整个 Agent 循环就会崩；正确做法是把错误
    当成一条「观察结果」返回给模型，让它看到自己写错了然后改。
    这是工具层和普通函数最大的区别。
    """
    from app.observability import tracer

    # 参数进 span 前先做短摘要：轨迹既要给人看也要落盘，
    # 完整参数（可能含大段日志内容）不该进观测存储。
    safe_args = {}
    if isinstance(args, dict):
        for k, v in list(args.items())[:8]:
            safe_args[k] = v if isinstance(v, (int, float, bool)) else str(v)[:120]

    with tracer.span(tracer.TYPE_TOOL, name=name, **safe_args) as sp:
        out = _execute_tool_inner(name, args)
        sp.set("ok", bool(out.get("ok")))
        sp.set("risk", out.get("risk"))
        if not out.get("ok"):
            sp.set_error(out.get("error"))
        return out


def _execute_tool_inner(name: str, args: dict) -> dict:
    """原 execute_tool 的实现体（由上面的观测包装调用）。"""
    started = time.time()
    if name not in TOOLS:
        return {"ok": False, "tool": name,
                "error": f"没有名为 {name} 的工具。可用工具：{list(TOOLS)}",
                "elapsed_ms": 0}

    meta = TOOLS[name]
    if not isinstance(args, dict):
        return {"ok": False, "tool": name,
                "error": f"参数必须是对象，收到 {type(args).__name__}",
                "elapsed_ms": 0}

    try:
        result = meta["func"](**args)
        ok, error = True, None
    except ToolError as e:
        # 参数不合法 —— 模型能自己修，把原话告诉它
        ok, result, error = False, None, str(e)
    except TypeError as e:
        # 参数名写错或必填项没给。补一句提示，模型第二次基本能改对
        ok, result = False, None
        error = (f"参数错误：{e}。本工具需要的参数："
                 f"{list(meta['params'])}")
    except Exception as e:
        ok, result, error = False, None, f"{type(e).__name__}: {e}"

    elapsed = int((time.time() - started) * 1000)
    out = {"ok": ok, "tool": name, "elapsed_ms": elapsed,
           "risk": meta["risk"]}
    if ok:
        out["result"] = result
    else:
        out["error"] = error
    return out


def tool_result_text(name: str, args: dict, out: dict) -> str:
    """把工具返回值转成给模型看的文本。

    统一在这里做序列化，避免每个调用方各写一份 json.dumps ——
    那次 requirements.txt 的编码事故就是"同一件事写两遍"的教训。
    """
    if out.get("ok"):
        return json.dumps(out["result"], ensure_ascii=False)
    return json.dumps({"error": out.get("error"),
                       "tool": name, "args": args}, ensure_ascii=False)
