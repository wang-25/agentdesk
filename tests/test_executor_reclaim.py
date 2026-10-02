# -*- coding: utf-8 -*-
"""超时回收（C6）与执行面收口（C1）—— 只测"调用了什么"，不真起容器、不真等超时。

【两条硬约束，这个文件是围着它们写的】

  1. **不真起容器、不真 sleep 去等超时**
     超时不是"等出来的"，是假进程 `communicate()` 里直接抛 `TimeoutExpired` 造出来的；
     `Popen` / `subprocess.run` 全部换成记录器。所以这组用例在任何机器上都是
     毫秒级、零依赖（不需要 docker、不需要真的能跑那 60 秒命令）。

  2. **断言的是"调用序列"，不是"最终状态"**
     "容器被删了""进程被杀干净了"在打桩环境里没有真实状态可看，
     能看的是**顺序**：
         docker rm -f <id>  →  杀 CLI  →  wait() 收尸  →  删 cidfile
     顺序错了就等于没修：先杀 CLI 的话，cidfile 里那个容器 id 可能还没写下来，
     容器就变成没人知道的孤儿 —— 那正是 C6 描述的现象。

★ 另外两条同样重要的用例方向：
  · **成功路径什么都不该杀** —— 过度回收会把别人的进程带走，
    这跟"该杀不杀"是同一类错误的两面；
  · **假 PATH 不生效** —— Linux 上跑真 `df`、Windows 上拒绝执行，两种情况都要过，
    因为两种情况证明的是同一件事：PATH 里的假货没有被调用。
"""

import os
import subprocess

import pytest

from app.sandbox import executor, policy

# ============================================================
# 装置：假进程 / 假 subprocess / 调用序列记录
# ============================================================


class FakeStream:
    """假的管道对象，只用来记录"关没关"。"""

    def __init__(self, log, name):
        self.log = log
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True
        self.log.append((f"close:{self.name}", None))

    def read(self):
        return ""


class FakeProc:
    """假子进程 —— 能记录被怎么处理，也能按需抛 TimeoutExpired。

    `timeout=True` 时 `communicate()` 抛 `subprocess.TimeoutExpired`，
    这就是"超时"的全部来源：**没有真的等待，也没有真的 sleep**。
    """

    def __init__(self, log, argv, *, pid=4321, returncode=0, stdout="", stderr="",
                 timeout=False, stuck=False, on_communicate=None,
                 stdout_stream=None, stderr_stream=None):
        self.log = log
        self.argv = list(argv)
        self.pid = pid
        self.returncode = returncode
        self.stdin = None
        self.stdout = stdout_stream
        self.stderr = stderr_stream
        self._out = stdout
        self._err = stderr
        self._timeout = timeout
        self._stuck = stuck
        self._on_communicate = on_communicate
        self.communicate_calls = []
        self.wait_calls = 0
        self.kill_calls = 0

    # ---- executor 会用到的那几个接口 ----
    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        self.log.append(("communicate", timeout))
        if self._on_communicate is not None:
            # 模拟"容器启动时 docker 把 id 写进 cidfile"这类副作用
            self._on_communicate(self)
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        return self._out, self._err

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.log.append(("wait", timeout))
        if self._stuck:
            # "杀不掉"的进程：wait 永远等不到它结束
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        return self.returncode

    def poll(self):
        self.log.append(("poll", None))
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.log.append(("proc.kill", self.pid))
        self.returncode = -9


class Spy:
    """subprocess 层面的假实现 + 调用序列记录。

    `log` 里每一项都是 `(kind, payload)`，按**发生顺序**追加 ——
    这就是"调用序列"断言的唯一依据。
    """

    def __init__(self):
        self.log = []
        self.popens = []      # [(argv, kwargs)]
        self.runs = []        # [(argv, kwargs)]
        self.procs = []       # [FakeProc]
        self.next_proc = {}   # 下一个假进程的构造参数
        self.popen_error = None

    # ---- 替代 subprocess.Popen / subprocess.run ----
    def popen(self, argv, **kwargs):
        argv = list(argv)
        self.log.append(("popen", argv))
        self.popens.append((argv, kwargs))
        if self.popen_error is not None:
            raise self.popen_error
        proc = FakeProc(self.log, argv, **self.next_proc)
        self.next_proc = {}
        self.procs.append(proc)
        return proc

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.log.append(("run", argv))
        self.runs.append((argv, kwargs))
        # Windows 的 taskkill 与 docker rm -f 都走这里；返回码 0 = 成功
        return subprocess.CompletedProcess(argv, 0)

    # ---- 断言辅助 ----
    def index(self, kind, contains=None):
        """第一次出现某个调用的下标；没有返回 -1。"""
        for i, (k, payload) in enumerate(self.log):
            if k != kind:
                continue
            if contains is None:
                return i
            text = payload if isinstance(payload, str) else " ".join(map(str, payload or []))
            if contains in text:
                return i
        return -1

    def run_argv(self, contains):
        for kind, payload in self.log:
            if kind == "run" and contains in " ".join(map(str, payload)):
                return payload
        return None

    def run_kwargs(self, contains):
        for argv, kwargs in self.runs:
            if contains in " ".join(map(str, argv)):
                return kwargs
        return None

    def kill_index(self):
        """"杀掉进程/进程组"这个动作第一次出现的下标（三条路径都算）。"""
        for i, (kind, payload) in enumerate(self.log):
            if kind in ("proc.kill", "killpg"):
                return i
            if kind == "run" and payload and payload[0] == executor.TASKKILL_BIN:
                return i
        return -1

    def kill_desc(self):
        for kind, payload in self.log:
            if kind == "killpg":
                return f"killpg{tuple(payload)}"
            if kind == "proc.kill":
                return "proc.kill"
            if kind == "run" and payload and payload[0] == executor.TASKKILL_BIN:
                return " ".join(map(str, payload))
        return "(没杀)"


@pytest.fixture
def spy(monkeypatch):
    """把 subprocess.Popen / subprocess.run 换成记录器。

    ★ executor 里写的是 `subprocess.Popen(...)`，所以打桩要打在 **subprocess 模块**上
    （跟 conftest 里"打桩要打在消费方"是同一个道理）。
    """
    s = Spy()
    monkeypatch.setattr(subprocess, "Popen", s.popen)
    monkeypatch.setattr(subprocess, "run", s.run)
    return s


@pytest.fixture
def host_backend(monkeypatch):
    """强制走主机通道（这台开发机上默认是 mock，不真执行）。"""
    monkeypatch.setattr(executor, "active_backend", lambda: "subprocess")


@pytest.fixture
def docker_backend(monkeypatch):
    """强制走容器通道，并假装 docker 可用。"""
    monkeypatch.setattr(executor, "active_backend", lambda: "docker")
    monkeypatch.setattr(executor, "_DOCKER_OK", True)


@pytest.fixture
def resolvable(monkeypatch):
    """把"在固定目录里解析"打桩成必定成功。

    解析本身由下面 `test_fake_binary_in_path_is_never_executed` 与
    `tests/test_policy.py` 覆盖；这里只关心"拿到绝对路径之后的行为"，
    否则在 Windows 上这些用例会全部停在"解析不到"那一步，测不到回收。
    """
    monkeypatch.setattr(policy, "resolve_binary", lambda name: f"/usr/bin/{name}")


def _install_posix_kill(spy, monkeypatch):
    """装上 POSIX 的 killpg/getpgid 并记录调用（Windows 上这两个属性本来不存在）。

    ★ 单独抽出来是为了让 **taskkill 与 killpg 两条路径都能被测到**：
    只测当前平台那一条的话，另一条永远是"没跑过的代码"。
    """
    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        spy.log.append(("killpg", [pgid, sig]))

    monkeypatch.setattr(executor, "_on_windows", lambda: False)
    monkeypatch.setattr(os, "getpgid", fake_getpgid, raising=False)
    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)


def _host_decision(command="df -h", timeout=3):
    d = policy.decide(command)
    assert d.decision == policy.ALLOW, d.reason
    assert d.isolation == policy.CHANNEL_HOST
    d.timeout = timeout
    return d


def _container_decision(timeout=5):
    d = policy.decide("truncate -s 0 /var/log/nginx/error.log")
    assert d.decision == policy.NEEDS_APPROVAL, d.reason
    assert d.isolation == policy.CHANNEL_CONTAINER
    d.timeout = timeout
    return d


def _cidfile_of(argv):
    if "--cidfile" in argv:
        return argv[argv.index("--cidfile") + 1]
    return None


@pytest.fixture(params=[False, True], ids=["windows-taskkill", "posix-killpg"])
def platform_kill(request, spy, monkeypatch):
    """两条回收路径都跑一遍（当前平台 + 另一条）。"""
    if request.param:
        _install_posix_kill(spy, monkeypatch)
    else:
        monkeypatch.setattr(executor, "_on_windows", lambda: True)
    return request.param


# ============================================================
# 一、主机通道：超时后杀整个进程组并收尸
# ============================================================
def test_host_timeout_reclaims_whole_process_group(spy, host_backend, resolvable,
                                                   platform_kill, monkeypatch):
    """超时必须把**整个进程组**收走，再 wait() 收尸。

    ★ 两个断言都是必需的，少一个这修复就不算成立：
      · 有"杀" —— 否则孙进程继续跑（C6 的原症状）
      · 杀之后有 wait() —— 否则留下僵尸，pid 也回收不了
    """
    spy.next_proc = dict(pid=4242, timeout=True,
                         stdout_stream=FakeStream(spy.log, "stdout"))
    d = _host_decision(timeout=3)

    r = executor.run(d)

    # ---- 失败态与语义：ok=False，没有退出码，超时/reclaim 都写在错误里 ----
    assert r.ok is False
    assert r.exit_code is None                     # 超时路径的既有语义：没有退出码
    assert isinstance(r.elapsed_ms, int) and r.elapsed_ms >= 0
    assert r.backend == "subprocess" and r.isolated is False
    assert "timeout" in r.error and "reclaim" in r.error
    assert "timeout=3s" in r.error, "要写清超时是多少秒"

    # ---- 执行的是解析出的绝对路径，并且自成进程组 ----
    argv, kwargs = spy.popens[0]
    assert argv == ["/usr/bin/df", "-h"]
    assert kwargs["start_new_session"] is True, "不成组就杀不了整组（killpg 会打到自己）"
    assert kwargs["shell"] is False

    # ---- 调用序列：杀 → wait ----
    kill_i = spy.kill_index()
    wait_i = spy.index("wait")
    assert kill_i != -1, "超时后什么都没杀 —— 孙进程会继续跑"
    assert wait_i != -1, "杀完没有 wait() 回收 —— 会留下僵尸进程"
    assert kill_i < wait_i, f"顺序不对：{spy.log}"
    assert spy.index("close:stdout") != -1, "超时路径的管道没有关掉（fd 泄漏）"

    if platform_kill:
        killed = [p for k, p in spy.log if k == "killpg"]
        assert killed == [[4242, executor.SIGKILL]], "POSIX 必须是 killpg(getpgid(pid), SIGKILL)"
    else:
        taskkill = spy.run_argv(executor.TASKKILL_BIN)
        assert taskkill == [executor.TASKKILL_BIN, "/T", "/F", "/PID", "4242"], taskkill
        # 辅助命令的环境：最小集（Windows 上再补一个操作系统必需的 SystemRoot）
        assert spy.run_kwargs(executor.TASKKILL_BIN)["env"] == executor._helper_env()


def test_host_success_kills_nothing(spy, host_backend, resolvable, platform_kill):
    """★ 成功路径**什么都不该杀**。

    过度回收（比如"每次都顺手 rm -f 一下""反正 kill 一下也没坏处"）会把别的进程
    带走 —— 这跟"该杀不杀"是同一类错误的两面，所以它必须有一条用例守着。
    """
    spy.next_proc = dict(pid=99, returncode=0, stdout="Filesystem  Size  Used\n")
    r = executor.run(_host_decision())

    assert r.ok is True
    assert r.exit_code == 0
    assert r.stdout.startswith("Filesystem")
    assert r.error == ""
    assert spy.kill_index() == -1, f"成功路径竟然杀了东西：{spy.kill_desc()}"
    assert spy.index("wait") == -1
    assert spy.index("run") == -1, "成功路径不该调用任何回收命令"


def test_reap_never_waits_forever_when_the_process_will_not_die(spy, host_backend,
                                                               resolvable, platform_kill):
    """★ 回收本身也不能"停不下来"。

    这一条是**实测踩出来的**：Windows 上 taskkill 的环境不对时它立刻返回失败
    （缺 `SystemRoot` → "The specified module could not be found."），
    而当时回收里写的是无超时的 `wait()` —— 整个执行进程就**永远**挂在那里。
    那比原来的"资源泄漏"更糟：原来只是东西没清干净，现在是调用方也死了。

    所以：wait 必须带超时；等不到就退回直接 kill；还是等不到就**如实**写进错误信息，
    绝不假装回收成功。
    """
    spy.next_proc = dict(pid=555, timeout=True, stuck=True)

    r = executor.run(_host_decision(timeout=2))

    assert r.ok is False
    assert "timeout" in r.error and "reclaim" in r.error
    assert "未能在限时内确认" in r.error, "杀不掉的时候不能假装已经回收成功"
    proc = spy.procs[0]
    assert proc.wait_calls >= 2, "wait 没有带超时地重试"
    assert proc.kill_calls >= 1, "等不到之后应退回直接 kill 兜底"


def test_helper_env_is_minimal_but_keeps_what_the_os_needs():
    """回收类辅助命令（taskkill / docker rm -f）的环境。

    ★ 守的是一个**实测 bug**：Windows 上少了 `SystemRoot`，`taskkill` 连启动都
      启动不了（"ERROR: The specified module could not be found."），回收静默失败。

    它是操作系统自己需要的变量，不是"把父进程环境合并回来"：
    被沙箱执行的命令那边依旧只有最小环境（见 `test_child_process_gets_minimal_env`）。
    """
    env = executor._helper_env()

    assert env["PATH"] == ":".join(policy.BINARY_DIRS)
    if os.name == "nt":
        assert env.get("SystemRoot"), "Windows 上少了 SystemRoot，taskkill 会起不来"
        assert set(env) == {"PATH", "LANG", "LC_ALL", "SystemRoot"}
    else:
        assert env == policy.command_env()


# ============================================================
# 二、执行面（C1）：解析不到就不执行，绝不退回裸名
# ============================================================
def test_unresolvable_binary_is_refused_and_never_run_bare(spy, host_backend, monkeypatch):
    """固定目录里找不到 → 拒绝执行。

    如果这里"退回裸名"去执行，就等于把刚堵上的洞又打开：按 PATH 找程序
    等于把"到底执行哪个程序"这个决定交回给环境变量。
    """
    monkeypatch.setattr(policy, "resolve_binary", lambda name: None)

    d = _host_decision()
    r = executor.run(d)

    assert r.ok is False
    assert "固定目录" in r.error and "找不到" in r.error
    assert spy.popens == [], "解析不到就绝不能启动任何进程（更不能用裸名去启动）"
    assert spy.log == []
    # 给人看的字段一个字都不改，审批界面显示的就是要执行的
    assert r.command == "df -h"
    assert r.argv == ["df", "-h"]


def test_fake_binary_in_path_is_never_executed(monkeypatch, tmp_path):
    """★ C1 的验收：把 PATH 指向一个含假 `df` 的目录，假的那个**绝不能被执行**。

    Linux 上会执行真的 `/usr/bin/df`；Windows 上因为在固定目录里找不到而拒绝。
    **两种情况都算通过** —— 它们证明的是同一件事：假程序没有被调用。
    假程序被调用的判据是它写下的标记文件（见下：先自证它能写）。
    """
    evil = tmp_path / "evil"
    evil.mkdir()
    marker = tmp_path / "pwned.txt"
    fake = evil / "df"
    fake.write_text(f'#!/bin/sh\ntouch "{marker}"\necho pwned\n', encoding="utf-8")
    fake.chmod(0o755)

    monkeypatch.setenv("PATH", str(evil))
    monkeypatch.setattr(executor, "active_backend", lambda: "subprocess")

    if os.name == "posix":
        # 自证：这个假程序本身确实会写标记。
        # 否则"标记文件不存在"可能只是**空过**（判据本身失效，用例假装通过）。
        subprocess.run([str(fake)], check=False)
        assert marker.exists(), "假程序没有写出标记 —— 这条用例的判据不成立"
        marker.unlink()

    d = policy.decide("df -h")
    assert d.decision == policy.ALLOW
    resolved = policy.resolve_binary("df")

    r = executor.run(d)

    assert not marker.exists(), "PATH 里的假 df 被执行了 —— PATH 劫持面没有堵上"
    if resolved is None:
        # Windows：固定目录里没有 df → fail-closed 拒绝，而不是去 PATH 里找
        assert r.ok is False
        assert "固定目录" in r.error
    else:
        # POSIX：执行的是解析出的绝对路径（真 df），与 PATH 无关
        assert r.exit_code is not None, r.error


# ============================================================
# 三、子进程环境：最小集
# ============================================================
def test_child_process_gets_minimal_env(spy, host_backend, resolvable, monkeypatch):
    """子进程拿到的是**最小环境**：没有 LD_PRELOAD 之类可注入变量，PATH 也是固定的。

    用假 Popen 捕获 env 参数来断言 —— 这是"执行时用了什么环境"唯一可信的来源。
    """
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/evil")
    monkeypatch.setenv("BASH_ENV", "/tmp/evil.sh")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    spy.next_proc = dict(pid=7, returncode=0, stdout="ok\n")

    r = executor.run(_host_decision())
    assert r.ok is True

    argv, kwargs = spy.popens[0]
    env = kwargs["env"]
    assert env == policy.command_env()
    for leaked in ("LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "PYTHONPATH"):
        assert leaked not in env, f"{leaked} 泄漏进了子进程环境"
    assert env["PATH"] == ":".join(policy.BINARY_DIRS)
    # 是"最小环境"，不是"最小环境 + 父环境"：一个多余的键都不该有
    assert set(env) == {"PATH", "LANG", "LC_ALL"}
    assert kwargs["shell"] is False
    assert argv[0] == "/usr/bin/df"


# ============================================================
# 四、容器通道：先删容器，再杀 CLI，cidfile 必删
# ============================================================
def test_container_timeout_removes_container_before_killing_cli(
        spy, docker_backend, platform_kill):
    """容器超时的回收序列：`docker rm -f <id>` → 杀 CLI → wait → 删 cidfile。

    ★ 顺序是这条用例的重点：反过来的话，CLI 一死，cidfile 里可能还没有容器 id，
      容器就成了没人知道的孤儿 —— 一直跑下去，且没有任何信号。
    """
    cid = "e" * 64
    cidfiles = []

    def docker_writes_cidfile(proc):
        path = _cidfile_of(proc.argv)
        assert path, "docker run 没有带 --cidfile，超时就找不到容器"
        cidfiles.append(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cid + "\n")          # 模拟 docker 在容器启动时写入 id

    spy.next_proc = dict(pid=777, timeout=True, on_communicate=docker_writes_cidfile,
                         stdout_stream=FakeStream(spy.log, "stdout"),
                         stderr_stream=FakeStream(spy.log, "stderr"))
    d = _container_decision(timeout=5)

    r = executor.run(d)

    assert r.ok is False
    assert r.backend == "docker" and r.isolated is True
    assert r.exit_code is None
    assert "timeout" in r.error and "reclaim" in r.error
    assert cid in r.error, "错误信息里要写清删掉了哪个容器"

    # docker run 确实带了 --cidfile（临时文件）
    argv, kwargs = spy.popens[0]
    assert argv[:2] == [executor.DOCKER_BIN, "run"]
    assert _cidfile_of(argv) and _cidfile_of(argv).endswith(".cid")
    assert os.path.basename(_cidfile_of(argv)).startswith(executor.CIDFILE_PREFIX)
    assert kwargs["env"] == policy.command_env()

    # ★ 调用序列：rm -f → 杀 CLI → wait
    rm_i = spy.index("run", contains="rm -f")
    kill_i = spy.kill_index()
    wait_i = spy.index("wait")
    assert rm_i != -1, "超时后没有 docker rm -f —— 容器会变成孤儿继续跑"
    assert kill_i != -1, "超时后没有终止 docker CLI"
    assert wait_i != -1
    assert rm_i < kill_i, f"必须先删容器再杀 CLI，实际：{spy.log}"
    assert kill_i < wait_i
    assert spy.run_argv("rm -f") == [executor.DOCKER_BIN, "rm", "-f", cid]
    # docker rm -f 的环境：与 docker run 同源（同一台 daemon 的视角），
    # 但辅助命令要带操作系统必需的那个键（Windows 的 SystemRoot）
    assert spy.run_kwargs("rm -f")["env"] == executor._helper_env()

    # ★ cidfile 无论成败都要清掉（这里走的是超时路径）
    assert cidfiles, "没拿到 cidfile 路径"
    assert not os.path.exists(cidfiles[0]), "超时后 cidfile 没有被清理"


def test_container_success_removes_nothing_and_cleans_cidfile(spy, docker_backend):
    """容器成功返回：不 rm、不 kill，但 cidfile 照样要删。"""
    paths = []

    def docker_writes_cidfile(proc):
        path = _cidfile_of(proc.argv)
        paths.append(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("f" * 64)

    spy.next_proc = dict(pid=1, returncode=0, stdout="ok\n", stderr="",
                         on_communicate=docker_writes_cidfile,
                         stdout_stream=FakeStream(spy.log, "stdout"),
                         stderr_stream=FakeStream(spy.log, "stderr"))

    r = executor.run(_container_decision())

    assert r.ok is True and r.exit_code == 0
    assert r.stdout == "ok\n"
    assert r.isolated is True
    assert spy.kill_index() == -1, "成功路径竟然杀了东西"
    assert spy.index("run") == -1, "成功路径不该调用 docker rm"
    assert paths and not os.path.exists(paths[0]), "成功后 cidfile 也必须清理"


def test_cidfile_is_cleaned_even_if_the_docker_cli_is_missing(spy, docker_backend):
    """`Popen` 直接失败（docker 没装）时，cidfile 也必须被清掉。

    这一条守的是 `finally`：临时文件是最容易"只在成功路径上删"的东西，
    而失败路径才是它真正会堆积的地方。
    """
    spy.popen_error = FileNotFoundError(2, "No such file or directory: 'docker'")

    r = executor.run(_container_decision())

    assert r.ok is False and "找不到" in r.error
    argv, _ = spy.popens[0]
    path = _cidfile_of(argv)
    assert path and not os.path.exists(path), "启动失败时 cidfile 泄漏了"


def test_docker_argv_without_cidfile_keeps_the_previous_shape():
    """不传 cidfile 时 `docker_argv()` 的形状不变（文档里手工起容器还在用它）。"""
    d = _container_decision()
    argv = executor.docker_argv(d)

    assert "--cidfile" not in argv
    assert argv[:2] == [executor.DOCKER_BIN, "run"]
    assert argv[-len(d.argv):] == d.argv          # 命令原样跟在镜像后面
    assert executor.SANDBOX_IMAGE in argv
