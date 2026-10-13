# -*- coding: utf-8 -*-
"""SSH 凭据（每台主机一份，私钥或密码）测试。

这个功能碰的是**密钥**，所以除了"能不能用"，还要专门守三件事：
    ① 密码**绝不回显**（响应、页面、日志、审计、命令行参数都不行）
    ② 默认行为不变（不配任何新变量时，与改动前逐字节一致）
    ③ fail-closed（配了密码却没密码、认证方式写错、base64 写坏 —— 都要当场报错）

第 ① 条尤其要盯：历史上这一层出过"取参数名写错导致引用来源从来没显示过"
（`citations` vs `sources`）和"接口被删掉但测试全绿"两次静默故障。
"""

import base64
import sys

import pytest

from app.llm import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))
from app.tools import ops  # noqa: E402

_ENV_KEYS = ("OPS_BACKEND", "OPS_SSH_TARGETS", "OPS_SSH_KEY", "OPS_SSH_USER",
             "OPS_SSH_AUTH", "OPS_SSH_PASSWORD", "OPS_HOSTS")


@pytest.fixture
def ssh_env(monkeypatch):
    """隔离环境：清掉所有 `OPS_*` 变量，测完恢复模块状态。

    ★ 必须恢复 `_CREDENTIALS` / `SSH_TARGETS` / `BACKEND`：
      它们是模块级状态，泄漏到别的用例里会让**别的测试**莫名其妙地失败。

    ★ 快照的是**所有 `OPS_` 开头的变量**，不是一份固定名单 ——
      第一版写死了 `_ENV_KEYS`，结果新加的每主机变量
      （`OPS_SSH_AUTH_DB_01` / `OPS_SSH_PASSWORD_B64_DB_01`）不在名单里，
      于是"上一个用例保存的密码"泄漏到下一个用例，
      表现出来是**另一个测试**失败（"缺密码的警告没出现"，因为密码其实还在）。
      固定名单这种写法，每加一个变量就多一次静默泄漏的机会。
    """
    import os

    saved_env = {k: v for k, v in os.environ.items() if k.startswith("OPS_")}
    saved_state = (ops.BACKEND, ops.SSH_TARGETS, ops.SSH_KEY, ops._CREDENTIALS,
                   ops.SSH_AUTH_DEFAULT, ops.SSH_PASSWORD)
    for k in list(saved_env):
        monkeypatch.delenv(k, raising=False)

    def _reload(targets: str = ""):
        ops.BACKEND = "ssh"
        ops.SSH_TARGETS = ops._parse_ssh_targets(targets)
        ops.SSH_AUTH_DEFAULT = (os.getenv("OPS_SSH_AUTH") or "key").strip().lower()
        ops.SSH_PASSWORD = os.getenv("OPS_SSH_PASSWORD") or ""
        ops.SSH_KEY = (os.getenv("OPS_SSH_KEY") or "").strip()
        ops.KNOWN_HOSTS = ops._resolve_hosts()
        ops.reload_credentials()

    yield _reload

    ops.BACKEND, ops.SSH_TARGETS, ops.SSH_KEY, ops._CREDENTIALS, \
        ops.SSH_AUTH_DEFAULT, ops.SSH_PASSWORD = saved_state
    for k in [k for k in os.environ if k.startswith("OPS_")]:
        os.environ.pop(k, None)
    os.environ.update(saved_env)


# ============================================================
# 一、环境变量后缀：主机名里有 `-`，而变量名不允许
# ============================================================
@pytest.mark.parametrize("host,expect", [
    ("web-01", "WEB_01"), ("db-01", "DB_01"),
    ("cache.internal", "CACHE_INTERNAL"), ("a b", "A_B"), ("", ""),
])
def test_suffix_normalises_host_names(host, expect):
    assert ops._cred_suffix(host) == expect


def test_per_host_credentials_are_read(ssh_env, monkeypatch):
    monkeypatch.setenv("OPS_SSH_KEY", "C:/keys/global")
    monkeypatch.setenv("OPS_SSH_AUTH_WEB_01", "key")
    monkeypatch.setenv("OPS_SSH_KEY_WEB_01", "C:/keys/web01")
    monkeypatch.setenv("OPS_SSH_AUTH_DB_01", "password")
    monkeypatch.setenv("OPS_SSH_PASSWORD_DB_01", "pw-db")
    ssh_env("web-01=root@1.1.1.1:22,db-01=ops@1.1.1.2:22")

    assert ops.credentials_for("web-01") == {
        "auth": "key", "key": "C:/keys/web01", "password": ""}
    assert ops.credentials_for("db-01")["auth"] == "password"
    assert ops.credentials_for("db-01")["password"] == "pw-db"


def test_unconfigured_host_falls_back_to_global(ssh_env, monkeypatch):
    """★ 默认行为不变：不配新变量时，就是改动前的"全局私钥"。"""
    monkeypatch.setenv("OPS_SSH_KEY", "C:/keys/global")
    ssh_env("cache-01=root@1.1.1.3:22")
    cred = ops.credentials_for("cache-01")
    assert cred["auth"] == "key" and cred["key"] == "C:/keys/global"
    assert cred["password"] == ""


def test_unknown_host_falls_back_to_global(ssh_env, monkeypatch):
    monkeypatch.setenv("OPS_SSH_KEY", "C:/keys/global")
    ssh_env("")
    assert ops.credentials_for("never-configured")["key"] == "C:/keys/global"


# ============================================================
# 二、密码的 base64 形式（设置页写回 .env 用）
# ============================================================
def test_password_supports_awkward_characters(ssh_env, monkeypatch):
    """★ 密码里可能有 `#`、空格、引号、单引号 —— 明文写进 .env 会被 dotenv
    静默解析错（在 # 处截断）。base64 让这些字符都能精确往返。"""
    nasty = 'p#ss word"x\'y\\z  '
    monkeypatch.setenv("OPS_SSH_AUTH_DB_01", "password")
    monkeypatch.setenv("OPS_SSH_PASSWORD_B64_DB_01",
                       base64.b64encode(nasty.encode()).decode())
    ssh_env("db-01=ops@1.1.1.2:22")
    assert ops.credentials_for("db-01")["password"] == nasty


def test_plaintext_wins_over_base64(ssh_env, monkeypatch):
    monkeypatch.setenv("OPS_SSH_PASSWORD_DB_01", "plain")
    monkeypatch.setenv("OPS_SSH_PASSWORD_B64_DB_01",
                       base64.b64encode(b"encoded").decode())
    ssh_env("db-01=ops@1.1.1.2:22")
    assert ops.credentials_for("db-01")["password"] == "plain"


def test_broken_base64_fails_loudly(ssh_env, monkeypatch):
    """★ 坏 base64 必须当场报错，而不是"当作没配"。

    "以为配了、其实没配上"会在半夜以『认证失败』的形式出现，
    真正的错因（.env 里那串被截断的 base64）谁也看不出来。
    """
    monkeypatch.setenv("OPS_SSH_PASSWORD_B64_DB_01", "这不是base64!!")
    with pytest.raises(ValueError, match="base64"):
        ssh_env("db-01=ops@1.1.1.2:22")


def test_unknown_auth_method_fails_loudly(ssh_env, monkeypatch):
    monkeypatch.setenv("OPS_SSH_AUTH_WEB_01", "kerberos")
    with pytest.raises(ValueError, match="认证方式"):
        ssh_env("web-01=root@1.1.1.1:22")


def test_host_name_collision_fails_loudly(ssh_env, monkeypatch):
    """`web-01` 与 `web.01` 规范化后都是 `WEB_01` —— 凭据会互相覆盖。"""
    monkeypatch.setenv("OPS_SSH_KEY_WEB_01", "C:/keys/a")
    with pytest.raises(ValueError, match="规范化"):
        ssh_env("web-01=root@1.1.1.1:22,web.01=root@1.1.1.9:22")


def test_collision_without_credentials_is_harmless(ssh_env):
    """没配凭据的名字撞车不该拦人（否则等于禁止同时用这两种命名）。"""
    ssh_env("web-01=root@1.1.1.1:22,web.01=root@1.1.1.9:22")
    assert set(ops._CREDENTIALS) >= {"web-01", "web.01"}


# ============================================================
# 三、执行分派：密码走 paramiko，私钥走命令行（且都能带每主机配置）
# ============================================================
def test_password_mode_without_password_fails_closed(ssh_env, monkeypatch):
    monkeypatch.setenv("OPS_SSH_AUTH_DB_01", "password")
    ssh_env("db-01=ops@1.1.1.2:22")
    with pytest.raises(ops.ToolError, match="没有密码"):
        ops._exec(["uptime"], host="db-01")


def test_key_mode_uses_the_per_host_key(ssh_env, monkeypatch):
    """私钥通道仍然走 `ssh` 命令行，且用**这台主机的**私钥。"""
    monkeypatch.setenv("OPS_SSH_KEY_WEB_01", "C:/keys/web01")
    ssh_env("web-01=root@1.1.1.1:22")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    rc, out = ops._exec(["uptime"], host="web-01")
    assert (rc, out) == (0, "ok")
    argv = seen["argv"]
    assert argv[argv.index("-i") + 1] == "C:/keys/web01"
    assert "BatchMode=yes" in argv, "私钥通道必须保留 BatchMode（否则可能弹提示挂死）"


def test_password_mode_uses_paramiko_and_never_puts_the_password_in_argv(
        ssh_env, monkeypatch):
    """★ 密码只能进 paramiko 的 connect 参数，**绝不能进命令行**。

    进程列表（`ps` / 任务管理器）对同机其它用户可见 ——
    密码一旦作为 argv 传出去，等于贴在公告板上。
    """

    monkeypatch.setenv("OPS_SSH_AUTH_DB_01", "password")
    monkeypatch.setenv("OPS_SSH_PASSWORD_DB_01", "s3cret-pw")
    ssh_env("db-01=ops@10.0.0.9:2222")

    import paramiko

    # 显式标注：这个字典混装 bool / str / dict，
    # 不标注会被推断成 dict[str, bool]（首个赋值是 True），后面全报类型错
    seen: dict = {}

    class FakeChannel:
        def recv_exit_status(self):
            return 0

    class FakeStream:
        def __init__(self, data):
            self._data = data
            self.channel = FakeChannel()

        def read(self):
            return self._data

    class FakeClient:
        def load_system_host_keys(self):
            seen["loaded_host_keys"] = True

        def set_missing_host_key_policy(self, policy):
            seen["policy"] = type(policy).__name__

        def connect(self, **kw):
            seen["connect"] = kw

        def exec_command(self, remote, timeout=None):
            seen["remote"] = remote
            seen["timeout"] = timeout
            return None, FakeStream(b"up 3 days\n"), FakeStream(b"")

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    argv_seen = []
    monkeypatch.setattr(ops.subprocess, "run",
                        lambda argv, **kw: argv_seen.append(argv))

    rc, out = ops._exec(["uptime"], host="db-01")
    assert rc == 0 and "up 3 days" in out
    assert argv_seen == [], "密码通道**不该**调用命令行 ssh"

    kw = seen["connect"]
    assert kw["password"] == "s3cret-pw"
    assert kw["username"] == "ops" and kw["port"] == 2222
    assert kw["allow_agent"] is False and kw["look_for_keys"] is False, \
        "只允许密码：否则会出现『配了密码却用一把旧私钥登录成功』这种难查的情况"
    assert seen["closed"] is True
    # 远端命令与私钥通道用同一套逐参数加引号
    assert seen["remote"] == "'uptime'"


def test_password_mode_quotes_the_remote_command(ssh_env, monkeypatch):
    """过了 SSH 这一跳必须重新画出参数边界（与 CLI 通道同一约定）。"""
    from app.tools import ops as _ops

    monkeypatch.setenv("OPS_SSH_AUTH_WEB_01", "password")
    monkeypatch.setenv("OPS_SSH_PASSWORD_WEB_01", "pw")
    ssh_env("web-01=root@1.1.1.1:22")

    import paramiko

    captured = {}

    class FakeStream:
        channel = type("C", (), {"recv_exit_status": lambda self: 0})()

        def read(self):
            return b""

    class FakeClient:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, p):
            pass

        def connect(self, **kw):
            pass

        def exec_command(self, remote, timeout=None):
            captured["remote"] = remote
            return None, FakeStream(), FakeStream()

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    _ops._exec(["df", "-h", "/var/log"], host="web-01")
    assert captured["remote"] == "'df' '-h' '/var/log'"


def test_auth_failure_message_is_actionable(ssh_env, monkeypatch):
    """认证失败必须说清"下一步做什么"，而不是抛一个裸的 paramiko 异常。"""
    import paramiko

    monkeypatch.setenv("OPS_SSH_AUTH_WEB_01", "password")
    monkeypatch.setenv("OPS_SSH_PASSWORD_WEB_01", "wrong")
    ssh_env("web-01=root@1.1.1.1:22")

    class FakeClient:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, p):
            pass

        def connect(self, **kw):
            raise paramiko.AuthenticationException("bad password")

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    with pytest.raises(ops.ToolError) as ei:
        ops._exec(["uptime"], host="web-01")
    msg = str(ei.value)
    assert "密码认证失败" in msg and "设置页" in msg
    assert "wrong" not in msg, "报错里不能带密码"


# ============================================================
# 四、接口层：保存 / 只写不读 / 留空不修改 / 清除
# ============================================================
@pytest.fixture
def settings_client(tmp_path, monkeypatch, ssh_env):
    """把 `.env` 指到临时目录 —— **绝不能让测试写到真 .env**（里面有真密钥）。"""
    import importlib

    from fastapi.testclient import TestClient

    import app.main as m

    (tmp_path / ".env").write_text("OPS_BACKEND=ssh\n", encoding="utf-8")
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("OPS_SSH_KEY", "C:/keys/global")
    ssh_env("web-01=root@1.1.1.1:22,db-01=ops@1.1.1.2:22")
    with TestClient(m.app) as c:
        yield c, m, tmp_path
    importlib.reload  # noqa: B018 - 明确表示这里不做 reload（避免误用）


def _save(client, creds):
    body = {"backend": "ssh", "targets": "web-01=root@1.1.1.1:22,db-01=ops@1.1.1.2:22",
            "key": "C:/keys/global", "hosts": "web-01,db-01", "credentials": creds}
    return client.post("/settings/ops", json=body)


def test_saving_a_password_never_echoes_it(settings_client):
    """★ 密码只能进、不能出。响应、摘要、页面 HTML 都不许出现它。"""
    client, _m, _tmp = settings_client
    pw = "S3cret!#weird'pw"
    r = _save(client, [{"host": "db-01", "auth": "password", "password": pw}])
    assert r.status_code == 200, r.text
    assert pw not in r.text
    assert "S3cret" not in r.text

    got = client.get("/settings/api")
    assert pw not in got.text
    row = [x for x in got.json()["ops"]["credentials"] if x["host"] == "db-01"][0]
    assert row["auth"] == "password" and row["password_set"] is True
    assert "password" not in row or row.get("password") in (None, ""), \
        "凭据视图里不该有 password 字段的值"


def test_empty_password_keeps_the_saved_one(settings_client):
    """★ 留空 = 不修改。

    "只改私钥路径顺手一保存，密码就没了"是最容易让人放弃这个功能的那种 bug。
    """
    client, _m, _tmp = settings_client
    _save(client, [{"host": "db-01", "auth": "password", "password": "keep-me"}])
    # 第二次保存：不带 password（表单原样提交的情形）
    _save(client, [{"host": "db-01", "auth": "password", "key": "C:/keys/x"}])
    got = client.get("/settings/api").json()
    row = [x for x in got["ops"]["credentials"] if x["host"] == "db-01"][0]
    assert row["password_set"] is True, "留空保存把密码弄丢了"
    # 换私钥这件事本身要生效
    save2 = _save(client, [{"host": "db-01", "auth": "password", "key": "C:/keys/x"}])
    assert save2.status_code == 200


def test_clear_password_explicitly(settings_client):
    """想清空必须显式说要清空。"""
    client, _m, _tmp = settings_client
    _save(client, [{"host": "db-01", "auth": "password", "password": "bye"}])
    _save(client, [{"host": "db-01", "auth": "key", "clear_password": True}])
    got = client.get("/settings/api").json()
    row = [x for x in got["ops"]["credentials"] if x["host"] == "db-01"][0]
    assert row["password_set"] is False and row["auth"] == "key"


def test_password_is_stored_as_base64_in_env(settings_client):
    """写进 .env 的是 base64：明文会被 dotenv 在 `#` 处静默截断。"""
    client, _m, tmp = settings_client
    pw = "p#ss word\"quote'"
    _save(client, [{"host": "db-01", "auth": "password", "password": pw}])
    env = (tmp / ".env").read_text(encoding="utf-8")
    assert pw not in env, "密码明文不该出现在 .env 里"
    line = [ln for ln in env.splitlines() if "PASSWORD_B64_DB_01=" in ln][0]
    decoded = base64.b64decode(line.split("=", 1)[1]).decode("utf-8")
    assert decoded == pw, "base64 必须能精确还原（含 # 空格 引号）"


def test_per_host_settings_are_persisted(settings_client):
    client, _m, tmp = settings_client
    _save(client, [{"host": "web-01", "auth": "key", "key": "C:/keys/web01"}])
    env = (tmp / ".env").read_text(encoding="utf-8")
    assert "OPS_SSH_AUTH_WEB_01=key" in env
    assert "OPS_SSH_KEY_WEB_01=C:/keys/web01" in env


def test_bad_auth_method_is_rejected(settings_client):
    client, _m, _tmp = settings_client
    r = _save(client, [{"host": "db-01", "auth": "kerberos"}])
    assert r.status_code == 400
    assert "key 或 password" in r.text


def test_warning_when_password_mode_has_no_password(settings_client):
    """配了密码登录却没密码 → 当场警告（否则每次查询都认证失败）。"""
    client, _m, _tmp = settings_client
    r = _save(client, [{"host": "db-01", "auth": "password"}])
    assert r.status_code == 200
    assert "warning" in r.json() and "db-01" in r.json()["warning"]
