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

import os
import shutil
import signal
import subprocess
import tempfile
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

# ---- 超时回收（C6）用的常量 ----
TASKKILL_BIN = "taskkill"
# Windows 上 signal 模块**没有 SIGKILL 这个属性**，取不到就退回 9。
# Windows 分支根本不用它（走 taskkill /T /F），留着只是让 POSIX 分支
# 在所有平台上都能被读到（也让它能被单测直接引用）。
SIGKILL = getattr(signal, "SIGKILL", 9)
# 回收动作自己的超时。回收是为了"能停下来"，它自己不能反过来把执行挂住。
RECLAIM_TIMEOUT = 10
# 容器通道的 cidfile 前缀（临时文件，超时回收靠它拿容器 id）
CIDFILE_PREFIX = "agentdesk-cid-"


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
    所以在这里兜住 —— 这跟给工具输出做 limit 是同一个思路：
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
def docker_argv(decision: Decision, image: str = None, cidfile: str = None) -> list:
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

      --cidfile <文件>          让 docker 把容器 id 写进这个文件（只有传了才加）。
                                超时回收要靠它：杀掉 docker CLI **不等于**容器没了，
                                得凭这个 id 去 `docker rm -f`（见下面"三·A"一节）。

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
        # ---- 超时回收：容器 id 落盘，超时后才找得到那个容器 ----
        *(["--cidfile", cidfile] if cidfile else []),
        # ---- 挂载（由策略决定，不由调用方决定）----
        *mounts,
        # ---- 镜像与命令 ----
        image or SANDBOX_IMAGE,
        *decision.argv,
    ]


# ============================================================
# 三·A、超时回收（C6）：把"停下来"这件事**做完**
# ============================================================
# 【为什么原来的超时不算"停下来了"】
# 超时只杀掉**直接子进程**（或 docker CLI）时，真实情况往往是：
#
#     $ sh -c "sleep 600 &"        ← 被超时杀掉的是最外层那一个
#     $ ps -ef | grep sleep        ← 真正占资源的孙进程还活着
#
#     $ docker run ...             ← docker CLI 被杀了
#     $ docker ps                  ← 容器还在跑（CLI 死了它并不跟着死）
#
# 更糟的是**没有任何信号**告诉调用方"还有东西在跑"：ExecResult 上写着
# "已终止"，机器上却留着进程和容器。这跟 fail-open 是同一类错误 ——
# 看起来做了，实际没做，而且看不出来。
#
# ★ 所以"回收"要完整地做两件事：
#     ① 杀**整个进程组**（不是单个进程）—— 前提是子进程自成一组
#        （Popen(start_new_session=True)），否则 getpgid(pid) 拿到的
#        很可能是**我们自己**的进程组，killpg 等于自杀
#     ② **等它真的死掉**（wait()）—— 否则留下僵尸，pid 也回收不了
#   容器通道同理：杀 docker CLI ≠ 容器没了，得凭 cidfile 里的 id
#   `docker rm -f` 把它删掉，而且**顺序是先删容器、再杀 CLI**。
def _on_windows() -> bool:
    """当前是不是 Windows。

    单独抽成函数，是为了让**两条回收路径都能被测到**：否则在 Windows 上
    永远测不到 killpg 那条、在 Linux 上永远测不到 taskkill 那条 ——
    而两条路径出事的代价是一样的（真出事时才发现没测过）。
    """
    return os.name == "nt"


def _terminate(proc) -> None:
    """兜底：杀单个进程（拿不到进程组、或进程组已经消失时用）。"""
    try:
        proc.kill()
    except Exception:
        pass


def _helper_env() -> dict:
    """**回收类辅助命令**（taskkill / docker rm -f）用的环境。

    ★ 它们跟"被沙箱执行的命令"不是一回事：被执行的命令属于**沙箱对象**，
      所以环境必须最小（见 `_run_host`）；而杀进程、删容器是**我们自己的工具**，
      作用对象是宿主机。所以这里在最小环境之上只补一个操作系统必需的键，
      而不是把父进程环境整个合并回来：

        · Windows 的 `SystemRoot` —— 实测少了它 `taskkill` **根本起不来**：
              ERROR: The specified module could not be found.
          回收会静默失败（更糟的是后面 wait() 永远等不到，见 `_reap`）。
          这是操作系统要的变量，不是我们要的。
    """
    env = policy.command_env()
    if _on_windows():
        env["SystemRoot"] = (os.environ.get("SystemRoot")
                             or os.environ.get("windir") or r"C:\Windows")
    return env


def _kill_process_group(proc) -> None:
    """杀掉**整个进程组**，含子孙进程。

    ★ 这个函数的正确性与 `start_new_session=True` 是绑在一起的：
      没建新会话时，子进程的 pgid 通常就是父进程（我们自己）的 pgid，
      `killpg` 会把我们自己也杀掉。调用点都是 Popen(start_new_session=True)。
    """
    pid = getattr(proc, "pid", None)
    if not pid:
        _terminate(proc)
        return

    if _on_windows():
        # Windows 没有"给进程组发信号"这回事：taskkill /T 连子孙一起，
        # /F 强杀。/PID 指到直接子进程，它就是整棵树的根。
        try:
            subprocess.run(
                [TASKKILL_BIN, "/T", "/F", "/PID", str(pid)],
                shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=RECLAIM_TIMEOUT, check=False, env=_helper_env(),
            )
        except Exception:
            _terminate(proc)
        return

    try:
        os.killpg(os.getpgid(pid), SIGKILL)
    except OSError:
        # 进程组已经不在了（自己退出了）或没有权限。
        # ★ 这里**不能**让异常炸出去：超时本身已经是失败路径，
        #   再抛一个异常会把"超时"变成"崩了"，调用方连失败原因都拿不到。
        _terminate(proc)


def _reap(proc) -> bool:
    """等被杀掉的子进程真正结束（wait），避免留下僵尸。返回是否确认收到了尸。

    ★ **必须带超时。** 回收是为了"能停下来"；如果杀不掉还无限 `wait()`，
      就变成了另一种"停不下来" —— 而且比原来更糟：原来只是资源泄漏，
      现在连调用方都一起挂死。
      实测过：Windows 上 `taskkill` 环境不对时它立刻返回失败，
      此时**无超时的 `wait()` 会把执行进程永远挂住**。

      等不到就退回直接 kill 再等一次；还是等不到就如实返回 False，
      由调用方把"没确认退出"写进错误信息（不假装回收成功）。
    """
    try:
        proc.wait(timeout=RECLAIM_TIMEOUT)
        return True
    except subprocess.TimeoutExpired:
        _terminate(proc)
        try:
            proc.wait(timeout=RECLAIM_TIMEOUT)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _close_pipes(proc) -> None:
    """关掉管道。超时路径不再读输出了，留着 fd 会一路泄漏到进程结束。"""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, name, None)
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _new_cidfile_path() -> str:
    """给 `docker run --cidfile` 造一个**尚不存在**的独占路径。

    为什么先建再删：`--cidfile` 指向的文件必须还不存在
    （Docker 见到已存在的 cidfile 会拒绝启动 —— 那是它防止"认错容器"的机制）。
    而 `mkstemp` 保证名字唯一、不撞车；建完删掉，路径依然是独占的。
    """
    fd, path = tempfile.mkstemp(prefix=CIDFILE_PREFIX, suffix=".cid")
    os.close(fd)
    os.unlink(path)
    return path


def _discard_cidfile(path: str) -> None:
    """删掉 cidfile。成功、超时、异常 —— 三条路都要删（放在 finally 里）。"""
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_cidfile(path: str) -> str:
    """读 cidfile 里的容器 id。读不到（容器还没起来）就返回空串。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            cid = fh.read().strip()
    except OSError:
        return ""

    # ★ 校验不是形式主义：这个值会被放到 `docker rm -f <值>` 的**参数位**上。
    #   cidfile 是我们自己造的临时文件，但"内容来自文件"就该拦一道 ——
    #   一个以 `-` 开头的值会被 docker 当成**选项**而不是容器 id
    #   （这就是参数注入的经典形态：问题不是"拼字符串"，是数据被当成了指令）。
    if not cid or len(cid) > 128 or cid.startswith("-") or any(c.isspace() for c in cid):
        return ""
    return cid


def _remove_container(container_id: str) -> bool:
    """超时后强制删掉容器：`docker rm -f <id>`。

    返回是否成功 —— 回收失败必须**说出来**，而不是假装回收了
    （这正是 C6 的教训：看起来做了、实际没做，且看不出来）。
    """
    try:
        done = subprocess.run(
            [DOCKER_BIN, "rm", "-f", container_id],
            shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=RECLAIM_TIMEOUT, check=False, env=_helper_env(),
        )
    except Exception:
        return False
    return getattr(done, "returncode", 0) == 0


def _run_docker(decision: Decision) -> ExecResult:
    """容器通道执行。

    超时的回收顺序是**先删容器、再杀 CLI**：
    反过来的话，CLI 一死就没人再告诉我们容器 id（cidfile 是 docker 在
    容器启动那一刻写的），容器会成为孤儿一直跑下去 —— 那正是 C6 描述的现象。

    ★ 为什么这里**不**把 `decision.argv[0]` 也解析成宿主机的绝对路径（C1）：
      容器里那条命令是由**镜像自己的 PATH** 解析的，宿主机的 PATH 与它无关；
      把宿主机的 `/usr/bin/truncate` 塞进容器，换个镜像就可能根本不存在 ——
      那是把隔离改坏，不是改好。这个通道里**真正由宿主机 PATH 解析**的是
      docker CLI 自己（`DOCKER_BIN`）：它的固定化不在本次范围内，
      因为它会让"开发在 Windows、部署在 Linux"里的 Windows 开发机直接
      失去容器通道（POSIX 固定目录里找不到 docker.exe）。
    """
    started = time.time()
    cid_path = _new_cidfile_path()
    argv = docker_argv(decision, cidfile=cid_path)

    try:
        try:
            proc = subprocess.Popen(
                argv, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env=policy.command_env(),
                # 自成一组，超时才能整组回收（见 _kill_process_group）。
                # 它只影响 docker CLI 自己的会话，不影响容器里的进程
                # （我们没有用 -i / -t，CLI 不需要终端）。
                start_new_session=True,
            )
        except FileNotFoundError:
            return ExecResult(
                ok=False, backend="docker", isolated=True, command=decision.command,
                argv=decision.argv, container_argv=argv,
                elapsed_ms=int((time.time() - started) * 1000),
                error=f"找不到 {DOCKER_BIN} —— 装了 Docker 之后再试",
            )
        except OSError as e:
            return ExecResult(
                ok=False, backend="docker", isolated=True, command=decision.command,
                argv=decision.argv, container_argv=argv,
                elapsed_ms=int((time.time() - started) * 1000),
                error=f"启动 {DOCKER_BIN} 失败：{e}",
            )

        try:
            stdout, stderr = proc.communicate(timeout=decision.timeout)
        except subprocess.TimeoutExpired:
            # ---- 超时回收（C6）----
            container_id = _read_cidfile(cid_path)
            removed = _remove_container(container_id) if container_id else False

            _kill_process_group(proc)
            reaped = _reap(proc)
            _close_pipes(proc)

            if not container_id:
                detail = "cidfile 里还没有容器 id（容器可能没起来），已终止 docker CLI"
            elif removed:
                detail = f"已 `{DOCKER_BIN} rm -f {container_id}` 删掉容器，并终止 docker CLI"
            else:
                detail = (f"`{DOCKER_BIN} rm -f {container_id}` 返回失败，"
                          f"容器可能仍在运行，请人工核实 `{DOCKER_BIN} ps -a`；"
                          f"docker CLI 已终止")
            if not reaped:
                # ★ 不假装回收成功：wait 没等到就如实说，让人去核实
                detail += "；⚠️ 未能在限时内确认 docker CLI 已退出，可能仍有残留"

            return ExecResult(
                ok=False, backend="docker", isolated=True, command=decision.command,
                argv=decision.argv, container_argv=argv,
                elapsed_ms=int((time.time() - started) * 1000),
                error=(f"容器执行超时（timeout={decision.timeout}s），"
                       f"已回收（reclaim）：{detail}；临时 cidfile 已清理。"),
            )

        out, t1 = _clip(stdout)
        err, t2 = _clip(stderr)
        return ExecResult(
            ok=proc.returncode == 0, backend="docker", isolated=True,
            command=decision.command, argv=decision.argv, container_argv=argv,
            exit_code=proc.returncode, stdout=out, stderr=err,
            elapsed_ms=int((time.time() - started) * 1000),
            truncated=t1 or t2,
            error="" if proc.returncode == 0
                  else f"命令以退出码 {proc.returncode} 结束",
        )
    finally:
        # ★ 无论成功、超时、还是中途抛异常，临时 cidfile 都要删掉。
        _discard_cidfile(cid_path)


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

    ★ 第五层兜底（C1）：**要执行哪个程序，由代码里的固定目录决定，不看 PATH。**
      白名单只比对命令名（并显式拒绝带 `/` 的名字），如果执行时靠继承的
      `PATH` 去找程序，那么**谁能控制 PATH，谁就能放一个假的 `systemctl`
      绕过整张白名单表** —— 白名单再严也没用，因为它把"到底执行哪个程序"
      这个决定交给了环境变量。所以：
        ① 先用 `policy.resolve_binary()` 解析成固定目录里的绝对路径；
        ② 解析不到就**拒绝执行**（绝不退回裸名 —— 那等于把刚堵上的洞又打开）；
        ③ 子进程环境用 `policy.command_env()`，**不继承父进程的环境**
           （PATH 固定、LD_PRELOAD / BASH_ENV 这类注入变量一并清掉）。
      `Decision` 里给人看的字段（`command`）一个字都不改 ——
      审批界面显示什么，执行的就必须是什么。
    """
    started = time.time()

    # ---- ① 解析可执行文件：只在固定目录里找 ----
    resolved = policy.resolve_binary(decision.argv[0])
    if resolved is None:
        return ExecResult(
            ok=False, backend="subprocess", isolated=False,
            command=decision.command, argv=decision.argv,
            elapsed_ms=int((time.time() - started) * 1000),
            error=(f"在固定目录里找不到这个可执行文件：{decision.argv[0]!r}"
                   f"（只找 {'、'.join(policy.BINARY_DIRS)}）。拒绝执行 —— "
                   f"不退回裸名：按 PATH 找程序等于把白名单交给环境变量，"
                   f"谁能控制 PATH 谁就能放一个假的 {decision.argv[0]} 绕过整张白名单表。"),
        )
    argv = [resolved, *decision.argv[1:]]

    try:
        proc = subprocess.Popen(
            argv, shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            # ③ 最小环境：**不** os.environ.copy()，也不把父进程环境合并回来
            env=policy.command_env(),
            # 自成一组：超时后 killpg 才能一次性收走它和它的子孙进程。
            # 没有这一句，getpgid(pid) 拿到的是我们自己的组，killpg 会打到我们自己。
            start_new_session=True,
        )
    except FileNotFoundError:
        return ExecResult(
            ok=False, backend="subprocess", isolated=False,
            command=decision.command, argv=decision.argv,
            elapsed_ms=int((time.time() - started) * 1000),
            error=(f"在固定目录里解析到的 {resolved} 在执行前消失了"
                   f"（`{decision.argv[0]}` 未被启动）"),
        )
    except OSError as e:
        return ExecResult(
            ok=False, backend="subprocess", isolated=False,
            command=decision.command, argv=decision.argv,
            elapsed_ms=int((time.time() - started) * 1000),
            error=f"启动 {resolved} 失败：{e}",
        )

    try:
        stdout, _ = proc.communicate(timeout=decision.timeout)
    except subprocess.TimeoutExpired:
        # ---- ② 超时回收（C6）：杀**整个进程组**，再 wait 收尸 ----
        # 只杀直接子进程的话，`sh -c "... &"` 留下的孙进程会继续跑；
        # 而且命令的 stdout 我们已经不打算要了（超时路径本来就没有输出），
        # 所以直接把管道关掉，别为了读残余输出把一个停不下来的进程等下去。
        _kill_process_group(proc)
        reaped = _reap(proc)
        _close_pipes(proc)
        if reaped:
            detail = "杀掉了整个进程组（含子进程与孙进程），并 wait() 回收，不留僵尸"
        else:
            # ★ 不假装回收成功：没等到就是没等到，如实写出来让人去核实
            detail = ("已对整个进程组下杀，但**未能在限时内确认进程退出**"
                      "（可能仍有残留，请人工用 ps / tasklist 核实）")
        return ExecResult(
            ok=False, backend="subprocess", isolated=False,
            command=decision.command, argv=decision.argv,
            elapsed_ms=int((time.time() - started) * 1000),
            error=f"执行超时（timeout={decision.timeout}s），已回收（reclaim）：{detail}。",
        )

    out, t = _clip(stdout)
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
                stdout="active\n（mock 后端：仿真输出）",
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
