# -*- coding: utf-8 -*-
"""
沙箱执行器
============================================================
策略说"可以跑"，这个文件负责"怎么跑、跑完长什么样"。

【三个后端，但只有一种是"真隔离"】

    docker      一次性容器。文件系统只读、无网络、掉全部 capability、
                非 root、内存 CPU 进程数全部封顶。
                → 真隔离。适合文件/日志类操作。

    subprocess  直接在目标主机执行。**没有隔离。**
                → 必须由人显式开启（SANDBOX_BACKEND=subprocess），
                  启动时打警告。默认永远不会走到这里。

    mock        什么都不执行，返回仿真结果。
                → 让任何机器上都能跑通演示与自检。

【★ 最重要的一条设计：不可用时必须 fail-closed】

auto 模式下如果探测不到 docker，**默认落到 mock，绝不悄悄降级成 subprocess。**

这个选择看着保守，但它是安全设计里最容易搞反的一点：

    fail-open（错的方向）  沙箱不可用 → 那就直接执行吧
                           → **隔离失效的那一刻，恰好是最危险的时候**
                           → 而且全程没有任何信号告诉你隔离没了

    fail-closed（本项目）  沙箱不可用 → 拒绝真执行，返回明确的未执行结果
                           → 安全属性不会因为环境变化而静默丢失

真实事故里，"降级执行"比"拒绝执行"危险得多 —— 因为没人会注意到降级。
所以这里宁可功能不可用，也不静默降级。
"""

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from app.sandbox import policy
from app.sandbox.policy import (
    CHANNEL_CONTAINER, MAX_OUTPUT_BYTES, Decision,
)

# ============================================================
# 一、配置与探测
# ============================================================
# auto     —— 有 docker 用 docker，没有就用 mock（**不会**落到 subprocess）
# docker   —— 强制 docker，探测不到就直接报错
# subprocess —— 显式要求无隔离执行，启动时会打警告
# mock     —— 强制仿真
_BACKEND_ENV = (os.getenv("SANDBOX_BACKEND") or "auto").strip().lower()
SANDBOX_IMAGE = (os.getenv("SANDBOX_IMAGE") or "alpine:3.20").strip()
DOCKER_BIN = (os.getenv("SANDBOX_DOCKER_BIN") or "docker").strip()


def _docker_available() -> bool:
    """探测 docker 是否真的能用。

    只判断"可执行文件在不在"是不够的 —— docker CLI 存在但 daemon 没起来
    也会返回一个看起来正常的路径。所以这里真的跑一次 `docker info`。
    """
    if not shutil.which(DOCKER_BIN):
        return False
    try:
        p = subprocess.run([DOCKER_BIN, "info"], shell=False, timeout=8,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p.returncode == 0
    except Exception:
        return False


_DOCKER_OK = _docker_available()


def active_backend() -> str:
    if _BACKEND_ENV in ("docker", "subprocess", "mock"):
        return _BACKEND_ENV
    return "docker" if _DOCKER_OK else "mock"


BACKEND = active_backend()


def describe() -> dict:
    backend = active_backend()
    return {
        "backend": backend,
        "configured_by": "env SANDBOX_BACKEND" if _BACKEND_ENV != "auto" else "auto",
        "env_value": _BACKEND_ENV,
        "isolated": backend == "docker",
        "docker_available": _DOCKER_OK,
        "docker_bin": DOCKER_BIN if _DOCKER_OK else None,
        "image": SANDBOX_IMAGE if backend == "docker" else None,
        "note": {
            "docker": "一次性容器执行，真实隔离",
            "mock": "仿真执行（未做任何真操作）。装了 Docker 并设 "
                    "SANDBOX_BACKEND=docker 即为真隔离",
            "subprocess": "⚠️ 无隔离，直接在目标主机执行",
        }.get(backend, ""),
        "fail_closed": True,
    }


class SandboxUnavailable(Exception):
    """沙箱不可用，且策略不允许降级执行。"""


# ============================================================
# 二、结果结构
# ============================================================
@dataclass
class ExecResult:
    ok: bool
    backend: str
    isolated: bool          # ← 调用方必须能一眼看到"这次到底有没有隔离"
    command: str
    argv: list = field(default_factory=list)
    exit_code: int = None
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: int = 0
    truncated: bool = False
    error: str = ""
    container_argv: list = field(default_factory=list)   # 实际发给 docker 的参数

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "backend": self.backend, "isolated": self.isolated,
            "command": self.command, "exit_code": self.exit_code,
            "stdout": self.stdout, "stderr": self.stderr,
            "elapsed_ms": self.elapsed_ms, "truncated": self.truncated,
            "error": self.error,
        }


def _clip(text: str) -> tuple:
    """截断输出。返回 (文本, 是否被截断)。

    命令的输出是**不可预知长度**的。一条 `journalctl` 可能吐几百 MB，
    直接塞进模型上下文会爆掉窗口，而且花的全是冤枉钱。
    所以在这里兜住 —— 这跟 Day 4 给工具输出做 limit 是同一个思路：
    **凡是外部来的、长度不可控的东西，都要有上限。**
    """
    raw = (text or "").encode("utf-8", errors="replace")
    if len(raw) <= MAX_OUTPUT_BYTES:
        return text, False
    head = raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return head + f"\n…（输出过长，已截断至 {MAX_OUTPUT_BYTES // 1024}KB）", True


# ============================================================
# 三、容器通道：真隔离
# ============================================================
# 这份模板是**代码里的常量**，跟模型无关。
# 模型能决定的只有最后那一段 argv —— 而那一段已经过白名单校验。
def docker_argv(decision: Decision, image: str = None) -> list:
    """把一条决策翻译成 docker run 参数。

    每一行限制都在挡一类具体的事，不是凑数的：

      --network none            容器不能连网络。挡住"下载脚本再执行"、
                                "把数据外传"、反连 shell —— 顺带也让扫描行为无效
      --read-only               根文件系统只读。挡住装包、写文件、
                                改容器内任何东西（要保持可写的 /tmp 单独给 tmpfs）
      --tmpfs /tmp:...          唯一可写的目录，且 noexec/nosuid ——
                                能写不能执行，挡住"写个脚本再跑"
      --memory / --memory-swap  内存封顶且不给 swap。挡住内存耗尽把宿主机拖垮
      --cpus                     CPU 封顶。挡住跑满所有核影响生产
      --pids-limit               进程数封顶。挡住 fork 炸弹
      --cap-drop ALL             掉掉全部 Linux capability。默认容器还留着
                                NET_RAW 等一堆，全部去掉，只留下最基础的
      --security-opt no-new-privileges  禁止提权（setuid 程序也提不上去）
      --user 65534:65534        用 nobody 跑，不是 root
      --workdir /tmp            进到唯一可写的地方

    ★ 这些参数的存在，就是为了保证**即使命令是恶意的，破坏范围也有上界**。
      沙箱的目标从来不是"挡住所有攻击"，是"让最坏情况可控"。
    """
    mounts = []
    for host_dir, target, mode in decision.mounts:
        mounts += ["-v", f"{host_dir}:{target}:{mode}"]

    return [
        DOCKER_BIN, "run", "--rm",
        # ---- 资源与权限 ----
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--memory", "128m",
        "--memory-swap", "128m",
        "--cpus", "0.5",
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--workdir", "/tmp",
        # ---- 挂载（由策略决定，不由调用方决定）----
        *mounts,
        # ---- 镜像与命令 ----
        image or SANDBOX_IMAGE,
        *decision.argv,
    ]


def _run_docker(decision: Decision) -> ExecResult:
    argv = docker_argv(decision)
    started = time.time()
    try:
        proc = subprocess.run(
            argv, shell=False, timeout=decision.timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ExecResult(
            ok=False, backend="docker", isolated=True, command=decision.command,
            argv=decision.argv, container_argv=argv,
            elapsed_ms=int((time.time() - started) * 1000),
            error=f"容器执行超时（{decision.timeout}s），已强制终止",
        )
    except FileNotFoundError:
        return ExecResult(
            ok=False, backend="docker", isolated=True, command=decision.command,
            argv=decision.argv, container_argv=argv,
            error=f"找不到 {DOCKER_BIN} —— 装了 Docker 之后再试",
        )

    out, t1 = _clip(proc.stdout)
    err, t2 = _clip(proc.stderr)
    return ExecResult(
        ok=proc.returncode == 0, backend="docker", isolated=True,
        command=decision.command, argv=decision.argv, container_argv=argv,
        exit_code=proc.returncode, stdout=out, stderr=err,
        elapsed_ms=int((time.time() - started) * 1000),
        truncated=t1 or t2,
        error="" if proc.returncode == 0
              else f"命令以退出码 {proc.returncode} 结束",
    )


# ============================================================
# 四、主机通道：没有容器隔离，靠别的兜底
# ============================================================
def _run_host(decision: Decision) -> ExecResult:
    """直接在目标主机执行。

    ★ **这里没有隔离，必须说清楚它靠什么兜底** —— 四层：

        ① 白名单     能跑到这里的命令，已通过 policy 的参数级校验。
                     注意是"参数级"：`systemctl restart nginx` 放行，
                     `systemctl disable firewalld` 连门都进不来
        ② 人工确认   所有写操作都是 needs_approval，没人点头不会执行
        ③ 审计留痕   每次执行都写 audit，谁在什么时候跑了什么
        ④ 隔离之外的   用一次性容器跑 `systemctl`？做不到 ——
                     容器里的 systemd 和宿主机的 systemd 是两个世界。
                     真正能提供硬隔离的做法是给目标机装一个受限 agent，
                     或者用 ssh 的 forced command + sudo 白名单。
                     本项目在文档里标了这个升级路径（见 docs/sandbox-hitl.md）。

    **诚实地说清楚"这里没有隔离"，比含糊地暗示"有隔离"重要。**
    运维工具最怕的就是给人虚假的安全感。
    """
    started = time.time()
    try:
        proc = subprocess.run(
            decision.argv, shell=False, timeout=decision.timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ExecResult(
            ok=False, backend="subprocess", isolated=False,
            command=decision.command, argv=decision.argv,
            elapsed_ms=int((time.time() - started) * 1000),
            error=f"执行超时（{decision.timeout}s），已强制终止",
        )
    except FileNotFoundError:
        return ExecResult(
            ok=False, backend="subprocess", isolated=False,
            command=decision.command, argv=decision.argv,
            error=f"目标主机上没有 `{decision.argv[0]}` 这个命令",
        )

    out, t = _clip(proc.stdout)
    return ExecResult(
        ok=proc.returncode == 0, backend="subprocess", isolated=False,
        command=decision.command, argv=decision.argv,
        exit_code=proc.returncode, stdout=out,
        elapsed_ms=int((time.time() - started) * 1000), truncated=t,
        error="" if proc.returncode == 0
              else f"命令以退出码 {proc.returncode} 结束",
    )


# ============================================================
# 五、mock 通道：仿真
# ============================================================
# 仿真数据要"像真的"，否则自检和演示都没有意义。
#
# ★ 注意：这里和 app/tools/ops.py 的 _MOCK 是**两套数据**，
#   但必须自洽 —— 两边对磁盘的描述要一致（都用 /data 做第二块盘），
#   否则诊断 Agent（读 check_disk）和处置 Agent（读 run_command df）会看到矛盾。
_MOCK = {
    "df -h": (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "/dev/vda1        40G   38G  1.2G  96% /\n"
        "/dev/vda2        60G   21G   36G  35% /data\n", 0),
    "free -m": (
        "               total        used        free      shared\n"
        "Mem:            3924        3120         410          12\n"
        "Swap:           2047         512        1535\n", 0),
    "uptime": (" 19:20:31 up 12 days,  3:41,  2 users,  "
               "load average: 8.42, 7.98, 7.10\n", 0),
}

# 磁盘占用的仿真数据。★ 这一块是"被真实使用逼出来的"：
#
#   第一版 mock 对 `du` 只返回一句"仿真执行"占位文案，没有数字。
#   结果处置 Agent 想确认"nginx 日志是不是根分区写满的主因"时，
#   拿到了命令被允许、但没有真实数值的结果 —— 于是它**拒绝提交清理动作**：
#   "无法确认日志是主因，不能提交"。
#
#   那个拒绝本身是对的（拿不到证据就不该动手），但根因是 mock 数据不完整。
#   **mock 数据也要和真实场景一样自洽、一样能给 Agent 足够的信息去决策。**
#   否则你在"测试环境"里验证不了"生产环境"里会发生的流程。
#
#   于是补齐：du 的每个允许目录都返回一个可信的大小，
#   -d1 形态返回子目录分布 —— 这正是定位"磁盘被什么占满"需要的东西。
_MOCK_DU = {
    "du -sh /": "9.8G\t/\n",
    "du -sh /var": "9.8G\t/var\n",
    "du -sh /var/log": "9.8G\t/var/log\n",
    "du -sh /var/log/nginx": "5.2G\t/var/log/nginx\n",
    "du -sh /var/lib": "6.2G\t/var/lib\n",
    "du -sh /var/lib/docker": "4.1G\t/var/lib/docker\n",
    "du -sh /var/lib/mysql": "6.2G\t/var/lib/mysql\n",
    "du -sh /var/cache": "820M\t/var/cache\n",
    "du -sh /tmp": "2.1G\t/tmp\n",
    "du -sh /home": "340M\t/home\n",
    "du -sh /opt": "120M\t/opt\n",
    "du -sh /srv": "64M\t/srv\n",
    "du -sh /usr": "1.1G\t/usr\n",
    "du -sh /data": "21.0G\t/data\n",
    # -d1 形态：子目录分布（两种等价写法都覆盖）
    "du -h -d1 /": "9.8G\t/var\n1.1G\t/usr\n2.1G\t/tmp\n820M\t/var/cache\n340M\t/home\n",
    "du -h -d 1 /": "9.8G\t/var\n1.1G\t/usr\n2.1G\t/tmp\n820M\t/var/cache\n340M\t/home\n",
    "du -h -d1 /var": "9.8G\t/var/log\n6.2G\t/var/lib\n820M\t/var/cache\n",
    "du -h -d 1 /var": "9.8G\t/var/log\n6.2G\t/var/lib\n820M\t/var/cache\n",
    "du -h -d1 /var/log": "5.2G\t/var/log/nginx\n2.8G\t/var/log/mariadb\n1.1G\t/var/log/journal\n900M\t/var/log/apt\n",
    "du -h -d 1 /var/log": "5.2G\t/var/log/nginx\n2.8G\t/var/log/mariadb\n1.1G\t/var/log/journal\n900M\t/var/log/apt\n",
}
_MOCK.update({k: (v, 0) for k, v in _MOCK_DU.items()})


def _run_mock(decision: Decision) -> ExecResult:
    """仿真执行 —— **不碰系统任何东西**，只是把"会执行什么"如实报告出来。

    这里刻意不假装成功：响应里带着 `backend: mock` 和 `isolated: false`，
    还会额外给一句话说明。看的人不会误以为真跑过了。
    """
    cmd = decision.command

    # ★ 精确匹配优先，而不是 startswith ——
    #   "du -sh /var".startswith("du -sh /") 是 True，
    #   用 startswith 遍历 dict 会把 `/var` 错配给 `/` 那条。
    #   （这跟 policy 里"白名单混入一个通配一切的值"是同一类坑）
    if cmd in _MOCK:
        out, code = _MOCK[cmd]
        return ExecResult(
            ok=True, backend="mock", isolated=False, command=cmd,
            argv=decision.argv,
            exit_code=code, stdout=out,
            elapsed_ms=1,
            stderr="（mock 后端：仿真输出，未在真实主机执行）",
        )

    if cmd.startswith("du "):
        # 没命中的 du 目标（理论上不会发生，因为策略已经限制了目录）——
        # 兜底返回一个数字，但**必须注明是兜底值**，不能让 Agent 误以为精确。
        target = decision.argv[-1]
        return ExecResult(
            ok=True, backend="mock", isolated=False, command=cmd,
            argv=decision.argv, exit_code=0,
            stdout=f"1.0G\t{target}   #（mock 兜底值，非真实测量）\n",
        )

    if cmd.startswith("truncate"):
        path = decision.argv[-1]
        return ExecResult(
            ok=True, backend="mock", isolated=False, command=cmd,
            argv=decision.argv, exit_code=0,
            stdout="",
            stderr=(f"（mock 后端：仿真执行，未真的清空 {path}）"),
        )

    if cmd.startswith(("systemctl", "docker", "journalctl")):
        first = decision.argv[0]
        if first == "systemctl" and decision.argv[1] == "is-active":
            return ExecResult(
                ok=True, backend="mock", isolated=False, command=cmd,
                argv=decision.argv, exit_code=0,
                stdout=f"active\n（mock 后端：仿真输出）",
            )
        return ExecResult(
            ok=True, backend="mock", isolated=False, command=cmd,
            argv=decision.argv, exit_code=0,
            stdout=f"（mock 后端：仿真执行 {cmd}）\n",
        )

    return ExecResult(
        ok=True, backend="mock", isolated=False, command=cmd,
        argv=decision.argv, exit_code=0,
        stdout=f"（mock 后端：仿真执行 {cmd}）\n",
    )



# ============================================================
# 六、统一入口
# ============================================================
def run(decision: Decision) -> ExecResult:
    """执行一条已通过策略判定的命令。

    ★ 注意签名：**它只接受 Decision，不接受字符串。**
      这样从类型上就杜绝了"绕过策略直接执行"——
      你想执行什么，就必须先经过 decide() 拿到一个 Decision。
      **让安全的路径是唯一可走的路径，比写完文档要求大家走安全路径可靠得多。**
    """
    if not isinstance(decision, Decision):
        raise TypeError("run() 只接受 policy.decide() 产出的 Decision 对象")
    if decision.decision == policy.DENY:
        raise SandboxUnavailable(f"这条命令已被策略拒绝，不应进入执行：{decision.reason}")

    backend = active_backend()

    if backend == "mock":
        return _run_mock(decision)

    if backend == "docker":
        if not _DOCKER_OK:
            # 显式要求 docker 但没有 → fail-closed，不降级
            raise SandboxUnavailable(
                f"SANDBOX_BACKEND=docker 但探测不到可用的 Docker"
                f"（试过 `{DOCKER_BIN} info`）。"
                f"请启动 Docker Desktop，或在 .env 里改 SANDBOX_BACKEND=mock。"
                f"**不会自动降级成无隔离执行。**")
        if decision.isolation == CHANNEL_CONTAINER:
            return _run_docker(decision)
        # 主机通道的命令（systemctl / docker restart）即使 docker 可用，
        # 也**不能**放进容器 —— 容器里的 systemd 和宿主机是两个世界。
        # 这点必须显式降级成主机通道，并且结果里 isolated 如实写 False。
        res = _run_host(decision)
        res.stderr = ("（该命令属于主机通道：容器内够不着宿主机的 systemd / docker daemon，"
                      "因此未使用容器隔离）\n" + res.stderr)
        return res

    # backend == "subprocess"：人显式开的，执行但要如实标记
    return _run_host(decision)


def preflight() -> dict:
    """启动预检。给 /sandbox 接口和自检脚本用。"""
    info = describe()
    problems = []
    if info["backend"] == "mock":
        problems.append(
            "当前是 mock 后端：命令不会真的执行。装了 Docker 后设 "
            "SANDBOX_BACKEND=docker 即为真隔离")
    if info["backend"] == "subprocess":
        problems.append("⚠️ 当前无隔离：命令直接在目标主机执行")

    if info["backend"] == "docker":
        try:
            p = subprocess.run([DOCKER_BIN, "image", "inspect", SANDBOX_IMAGE],
                               shell=False, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            info["image_present"] = p.returncode == 0
            if p.returncode != 0:
                problems.append(
                    f"镜像 {SANDBOX_IMAGE} 还没拉下来，首次执行会先下载。"
                    f"可先执行：{DOCKER_BIN} pull {SANDBOX_IMAGE}")
        except Exception as e:
            info["image_present"] = None
            problems.append(f"检查镜像失败：{e}")

    info["problems"] = problems
    info["ok"] = not problems or info["backend"] == "docker"
    return info
