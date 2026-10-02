# -*- coding: utf-8 -*-
"""pytest 公共装置。

三条硬约束 —— 这个套件必须能在 CI 上**零成本、稳定、可重复**地跑：

    1. **禁止访问网络**（`_no_network`，autouse）
       任何非回环连接直接失败。测试里不允许出现真实模型调用；
       花钱的验证属于 `scripts/smoke_test.py --full` 与 `scripts/run_eval.py`。
       这条守卫还有个作用：**忘了打桩会立刻报错，而不是悄悄花掉一笔钱。**

    2. **不污染真实状态**
       审批单用临时文件（`approval_store`）、trace 写临时文件（`trace_file`）。
       跑完测试，`logs/` 下必须一行没多。

    3. **不依赖 `data/index/` 是否构建过**
       那份索引是 gitignore 的构建产物，CI 里不存在。
       语料相关测试自己造小语料（见 `tests/test_rag_store.py`）。

【一个容易踩的坑：打桩要打在"消费方"】
本项目各模块用的是 `from app.llm import chat_json` 这种**直接导入**写法，
名字在 import 时就绑定了。所以 `monkeypatch.setattr(app.llm, "chat_json", ...)`
**改不动**已经导入它的模块 —— 必须打在消费方模块上（如
`app.agents.specialists.chat_json`）。`fake_chat` 已经把这些位置都列出来了。
"""

import socket
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 允许回环：CI 里要起本地服务做接口自检；也允许 AF_UNIX（空 host）
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


# ============================================================
# 守卫：禁止真实网络
# ============================================================
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guard(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(
                f"测试禁止访问网络（试图连接 {address}）。"
                "真实模型调用请走 scripts/smoke_test.py --full 或 scripts/run_eval.py；"
                "单元测试请用 fake_chat 打桩。")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guard)
    yield guard


# ============================================================
# 隔离装置
# ============================================================
@pytest.fixture
def approval_store(tmp_path):
    """临时审批单存储 —— 不碰 logs/approvals.jsonl。"""
    from app.sandbox import approvals
    return approvals.ApprovalStore(path=tmp_path / "approvals.jsonl")


@pytest.fixture
def trace_file(tmp_path, monkeypatch):
    """把 tracer 的落盘位置改到临时目录 —— 不碰 logs/traces.jsonl。"""
    from app.observability import tracer
    path = tmp_path / "traces.jsonl"
    monkeypatch.setattr(tracer, "TRACE_PATH", path)
    return path


def _make_record(**over):
    """造一张合法的审批单入参。"""
    base = dict(command="truncate -s 0 /var/log/nginx/error.log",
                fingerprint="fp-deadbeef0000",
                rule="truncate",
                risk="reversible",
                isolation="container",
                reason="清理写满的日志")
    base.update(over)
    return base


@pytest.fixture
def new_approval(approval_store):
    """工厂：在临时 store 里开一张单，返回记录。"""
    def _create(**over):
        return approval_store.create(**_make_record(**over))
    return _create


# ============================================================
# 模型打桩
# ============================================================
# (模块名, 该模块里的属性名, fake 上的方法名)
#   本项目用 `from app.llm import X` 直接导入，所以必须打在**消费方模块**上。
_CHAT_TARGETS = [
    ("app.llm", "chat", "chat"),
    ("app.llm", "chat_step", "chat_step"),
    ("app.llm", "chat_json", "chat_json"),
    ("app.main", "llm_chat", "chat"),
    ("app.main", "chat_json", "chat_json"),
    ("app.rag.pipeline", "chat", "chat"),
    ("app.agents.common", "chat_step", "chat_step"),
    ("app.agents.graph", "chat_step", "chat_step"),
    ("app.agents.react", "chat_step", "chat_step"),
    ("app.agents.specialists", "chat_json", "chat_json"),
    ("app.evaluation.judges", "chat_json", "chat_json"),
]


class FakeChat:
    """可编排的假模型：按顺序弹出预设回复，并记录收到的消息。"""

    def __init__(self):
        self.replies = []
        self.calls = []
        self.patched = []

    # ---- 编排 ----
    def push(self, *replies):
        self.replies.extend(replies)
        return self

    def _next(self):
        if not self.replies:
            raise AssertionError(
                "假模型没有更多预设回复了 —— 测试里请先 fake.push(...)")
        return self.replies.pop(0)

    # ---- 与 app.llm 同形的三个入口 ----
    def chat(self, messages, temperature=0.7, timeout=60):
        self.calls.append(list(messages))
        return self._next()

    def chat_step(self, messages, tools=None, temperature=0, timeout=90):
        self.calls.append(list(messages))
        return self._next()

    def chat_json(self, messages, temperature=0, timeout=60):
        self.calls.append(list(messages))
        return self._next()

    # ---- 断言辅助 ----
    @property
    def call_count(self):
        return len(self.calls)

    def last_user_message(self):
        assert self.calls, "还没有任何模型调用"
        for msg in reversed(self.calls[-1]):
            if msg.get("role") == "user":
                return msg.get("content") or ""
        return ""


@pytest.fixture
def fake_chat(monkeypatch):
    """把模型调用换成假实现。

    只对**已经被导入**的模块打桩（不会为了打桩去 import app.main 这种重模块）。
    所以要用它的测试，请在模块顶部就 import 好目标模块。
    """
    fake = FakeChat()
    for mod_name, attr, method in _CHAT_TARGETS:
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        monkeypatch.setattr(mod, attr, getattr(fake, method))
        fake.patched.append(f"{mod_name}.{attr}")
    return fake
