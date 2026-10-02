# -*- coding: utf-8 -*-
"""意图解析 —— 模型说的"人话"变成程序能读的结构化 JSON 的那一步。

这一层的输入**完全不可信**：它来自模型，而模型会少字段、会改字段名、
会把 JSON 包进 markdown 代码块、会自作主张换个取值。所以用例的重心是
"坏输入会怎样"，而不是"好输入能通过"：

    · 校验（`validate_intent`）是**纯函数**，必须永不抛异常 ——
      它是解析循环里唯一的下游防线，它一抛，重试机制就形同不存在
    · 重试必须**真的重试**，且次数有上限（`MAX_PARSE_ATTEMPTS`）。
      少了上限就是无限烧钱；上限写错了就没有第二次机会
    · 不合格必须抛 `IntentParseFailed`（带 problems），
      而不是返回 None —— 调用方要能区分"解析失败"和"解析成功但字段为空"

【打桩为什么必须在模块顶部 import app.main】
conftest 的 `fake_chat` 只对**已经被导入**的模块打桩（`sys.modules.get`），
本项目又是 `from app.llm import chat_json` 这种直接导入写法 ——
名字在 import 时就绑定到消费方模块上了，所以"打桩要打在消费方"。
下面这一行不能挪进用例里。
"""

import json

# ★ 必须在文件顶部导入：fake_chat 只会替换已导入模块里的模型入口。
import app.main as m
import pytest
from app.llm import ModelError

# ============================================================
# 一、validate_intent：纯函数，永不抛异常
# ============================================================
def _valid(**over):
    """一份合法的意图解析结果。"""
    base = {"action": "diagnose", "service": "nginx", "host": "web-01",
            "risk": "low", "need_confirm": False, "reason": "只读排查"}
    base.update(over)
    return base


def test_valid_intent_passes_with_no_problems():
    ok, problems = m.validate_intent(_valid())
    assert ok is True
    assert problems == []


def test_validate_intent_returns_a_bool_and_a_list():
    """返回值形状固定为 `(bool, list)` —— 调用方写的是 `ok, problems = ...`。

    "通过"时如果返回 `(True, None)`，那边立刻就是 TypeError，
    而且只在**成功路径**上崩，测试很容易漏。
    """
    ok, problems = m.validate_intent({"action": "query"})
    assert isinstance(ok, bool)
    assert isinstance(problems, list)


@pytest.mark.parametrize("field", m.REQUIRED_FIELDS)
def test_each_required_field_is_actually_required(field):
    """逐个字段验证"缺了就必须报"。

    参数化而不是写一个 `{}` 就完事：只有逐个去掉，才能发现
    某个字段被写进了必填清单却**其实没被检查**。
    """
    data = _valid()
    del data[field]
    ok, problems = m.validate_intent(data)
    assert ok is False
    assert any(field in p for p in problems), f"缺少 {field} 没被指出来：{problems}"


def test_missing_fields_are_all_reported_at_once():
    """一次把所有问题列全，而不是只报第一个。

    重试时这些 problems 会被拼进给模型的反馈里 ——
    一次只报一个，等于让模型多跑两轮（多花两份钱）。
    """
    ok, problems = m.validate_intent({})
    assert ok is False
    assert len(problems) == len(m.REQUIRED_FIELDS)
    for field in m.REQUIRED_FIELDS:
        assert any(field in p for p in problems), field


@pytest.mark.parametrize("risk", m.ALLOWED_RISK)
def test_allowed_risk_values_pass(risk):
    """`low/medium/high` 三个档位都是合法值 —— 提示词里就是这么写的。"""
    ok, problems = m.validate_intent(_valid(risk=risk))
    assert ok is True, f"{risk} 被拒了：{problems}"


@pytest.mark.parametrize("risk", [
    pytest.param("HIGH", id="uppercase"),
    pytest.param("  high  ", id="whitespace"),
    pytest.param("critical", id="unknown-word"),
    pytest.param("", id="empty-string"),
    pytest.param(None, id="null"),
    pytest.param(3, id="number"),
    pytest.param(["high"], id="list"),
])
def test_risk_outside_the_allowed_domain_is_rejected(risk):
    """risk 只有三个合法取值，其余一律报问题。

    为什么这条盯得紧：这个字段曾经被当成**决定依据**，
    模型把"重启服务"标成 medium 就放行了（见 `INTENT_SYSTEM_PROMPT` 里那段注释）。
    现在它只是"模型意见"，最终由 policy.escalate 抬高 ——
    但意见本身写错值仍然必须被拦下来重问一次。
    """
    ok, problems = m.validate_intent(_valid(risk=risk))
    assert ok is False
    assert any("risk" in p for p in problems), problems


def test_unknown_action_is_not_caught_here():
    """★ 已知问题：`action` 的取值域**没有**被校验。

    提示词说 action 只能取 diagnose/restart/cleanup/query 四个值
    （见 `INTENT_SYSTEM_PROMPT`），而 `validate_intent` 只检查"字段在不在"
    （`for field in REQUIRED_FIELDS: if field not in data`），没有任何取值域判断。
    于是模型返回 `{"action": "delete_everything", ...}` 会被判**通过**，
    不会触发重试。

    后果被下游接住了：`policy.risk_of_action` 对认不出来的 action
    一律给 medium，再由 `policy.escalate` 取更危险者，
    所以不会因此放行危险操作。但"字段取值域"这一层是缺的 ——
    模型编出来的 action 会一路带着上下文往下走，
    直到某处匹配不上任何分支（`/webhook/alert` 里的 `intent.get("action")`）。
    这里固化当前行为；补上取值域校验时这条要跟着改。
    """
    for bogus in ("delete_everything", "RESTART", "", None, 42):
        ok, problems = m.validate_intent(_valid(action=bogus))
        assert ok is True, f"action={bogus!r} 居然被拦了：{problems}"


def test_unknown_host_is_not_caught_here():
    """★ 已知问题：`host` 只检查"字段在不在"，不检查值。

    提示词要求"不确定就填 null，不要编造"，但校验层对
    `host="web-99.example.com"`（清单里不存在的主机）完全无感。
    后果：诊断会被指向一台不存在/不该碰的机器 ——
    真正拦住它的是 SSH 目标白名单（ops.py），不是这里。
    这里固化"校验层只做结构合法、不做事实合法"这一现状。
    """
    for host in ("web-99.example.com", "生产数据库", 12345):
        ok, problems = m.validate_intent(_valid(host=host))
        assert ok is True, f"host={host!r} 被拦了：{problems}"


def test_extra_fields_are_tolerated():
    """多出来的字段不判失败（提示词说"不要增加字段"，但校验层不做减法）。

    记录这个宽松是有用的：将来收紧成"多余字段也算问题"，
    会直接影响重试率与成本 —— 那是一次有意的取舍，不是 bug。
    """
    ok, problems = m.validate_intent(_valid(extra="模型自己加的", confidence=0.9))
    assert ok is True and problems == []


def test_none_is_reported_not_crashed():
    """模型直接返回 `null`（合法 JSON，但不是一个对象）时必须报问题，
    而不是把 TypeError 抛给调用方。

    这是 M1 修掉的一个真实缺陷：原先 `validate_intent` 第一句就是
    `if field not in data`，data 为 None 时抛
    `TypeError: argument of type 'NoneType' is not iterable`。
    那个异常不被 parse_intent / `/parse` / `/webhook/alert` 的任何 except 接住，
    会变成 HTTP 500 且**不写 parse_failed 审计** —— 无人值守链路静默中断。
    """
    ok, problems = m.validate_intent(None)  # type: ignore[arg-type]
    assert ok is False
    assert problems, "判了不合格却没给理由，调用方（模型）没法据此改正"


@pytest.mark.parametrize("data", [
    pytest.param([], id="empty-list"),
    pytest.param([{"action": "query"}], id="list-of-one"),
    pytest.param("just a string", id="string"),
])
def test_non_dict_container_input_does_not_pass_silently(data):
    """非 dict 的**容器**输入不能"静默通过" —— 通过就意味着后面拿它当 dict 用。

    当前实现是靠 `field not in data` 在容器上做成员判断，
    所以列表/字符串碰巧被判成"缺字段"。这条把这个巧合固定下来，
    它是"非 dict 一律不合格"这个期望在当前实现下最接近的形态。
    """
    ok, _ = m.validate_intent(data)
    assert ok is False, f"非 dict 输入被判成合格：{data!r}"


@pytest.mark.parametrize("data", [
    pytest.param(None, id="none"),
    pytest.param(42, id="number"),
    pytest.param(3.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param(ModelError("请求超时"), id="model-error-object"),
])
def test_non_iterable_input_is_reported_not_crashed(data):
    """★ 不可迭代的输入（None / 数字 / 异常对象）必须被判定为不合格，
    **而不是抛 TypeError**。

    修复前的一行复现：`m.validate_intent(None)` →
    `TypeError: argument of type 'NoneType' is not iterable`。

    为什么这不是"理论上"的问题：模型输出裸 `null` 是合法 JSON，
    `app.llm.parse_json_reply` 会把它解析成 Python 的 None 并返回，
    所以这条路径在真实运行里随时会被走到（M1 已修，见 validate_intent 开头）。

    注意 `ModelError` 这个参数：它是异常**对象**被当成模型返回值传了进来。
    修复前它会在成员判断上炸；修复后它被如实判成"不是 JSON 对象" ——
    这正是我们要的：**校验层不分辨输入从哪来，只判定它是不是合格的对象。**
    """
    ok, problems = m.validate_intent(data)
    assert ok is False, f"非对象输入被判成合格：{data!r}"
    assert problems, "判了不合格却没给理由"


# ============================================================
# 二、parse_intent：正常路径
# ============================================================
def test_valid_reply_is_returned_with_attempt_count_one(fake_chat):
    """一次就对：返回 (intent, 1)，且只调了一次模型（成本口径靠它）。

    `_attempts` 会出现在 `/parse` 的返回里，也是告警链路的审计字段 ——
    它错了就没人知道重试到底吃了多少 token。
    """
    fake_chat.push(_valid())
    intent, attempts = m.parse_intent("查一下 nginx 状态")
    assert intent == _valid()
    assert attempts == 1
    assert fake_chat.call_count == 1


def test_first_messages_carry_prompt_and_question(fake_chat):
    """第一轮必须是 [system 提示词, user 原问题]。

    少一个都不行：没有系统提示词模型不会输出约定字段；
    把问题放进 system 会丢掉"这是用户输入"的边界。
    """
    fake_chat.push(_valid())
    m.parse_intent("mysql 为什么会崩")
    roles = [msg["role"] for msg in fake_chat.calls[0]]
    assert roles == ["system", "user"]
    assert fake_chat.calls[0][0]["content"] == m.INTENT_SYSTEM_PROMPT
    assert fake_chat.last_user_message() == "mysql 为什么会崩"


@pytest.mark.parametrize("shape", [
    pytest.param("已解析好的 dict", id="dict-from-chat-json"),
    pytest.param("模型吐的是带 ```json 围栏的文本", id="fenced-json-text"),
    pytest.param("模型在 JSON 前后加了解释文字", id="json-with-prose"),
])
def test_reply_is_consumed_as_a_dict_in_one_attempt(fake_chat, shape):
    """★ 契约：`chat_json` 的对外承诺是"给我一个 dict"，形状差异在那一层抹平。

    模型实际会吐出三种形状：干净 JSON、``` 围栏包裹、前后带解释文字。
    抹平它们的是 `app.llm.parse_json_reply`（先剥代码块、再按大括号截取），
    所以 `parse_intent` 拿到手时**已经是 dict**，
    它自己不做反序列化，也不该因为形状问题浪费一次重试。

    这里刻意打桩在 `app.main.chat_json` 上（消费方），
    所以三种形状在测试里都表现为"已经解析好的 dict"——
    这条用例固定的是契约本身：parse_intent 只认 dict，
    一旦解析层的承诺变了（比如它开始返回原始文本），这里必须先红。
    至于那个承诺本身成立不成立，由下面
    `test_real_parse_json_reply_strips_fences_and_prose` 用真解析层来证。
    """
    fake_chat.push(_valid())
    intent, attempts = m.parse_intent("重启 nginx")
    assert intent == _valid()
    assert attempts == 1, f"{shape} 被当成失败重试了"
    assert fake_chat.call_count == 1


def test_real_parse_json_reply_strips_fences_and_prose():
    """★ 用**真** `app.llm.parse_json_reply` 验证：围栏与解释文字确实被抹平。

    上面那条用例打桩打掉了这一层，所以"chat_json 会给 dict"这个承诺
    必须由这条来兑现。这里直接测 `parse_json_reply` ——
    它就是 `chat_json` 内部包着的那层解析，同一个模块，测它等于测实际代码。

    更关键的是这条把**两层真函数串起来跑**：把模型真实会吐的三种文本
    先过 `parse_json_reply`，再过 `validate_intent`，必须一次通过、
    不产生任何 problems。少了这条，围栏 JSON 一旦解析不了，
    表现就是"每次解析都白重试 3 次"——花钱、变慢，而且不会报错。
    """
    from app.llm import parse_json_reply

    for reply in (
            '```json\n' + json.dumps(_valid(), ensure_ascii=False) + '\n```',
            '好的，解析如下：\n' + json.dumps(_valid(), ensure_ascii=False) + '\n以上。',
            json.dumps(_valid(), ensure_ascii=False),
    ):
        data = parse_json_reply(reply)
        assert data == _valid(), f"形状没被抹平：{reply[:20]!r}"
        ok, problems = m.validate_intent(data)
        assert ok is True, f"解析层的输出过不了校验：{problems}"


# ============================================================
# 三、parse_intent：自我修正重试
# ============================================================
def test_retries_once_and_reports_two_attempts(fake_chat):
    """第一次少字段、第二次改对：返回 attempts=2，模型被调了两次。"""
    fake_chat.push({"action": "restart", "service": "nginx"}, _valid())
    intent, attempts = m.parse_intent("重启 nginx")
    assert intent == _valid()
    assert attempts == 2
    assert fake_chat.call_count == 2


def _make_queued_errors_raise(fake_chat, monkeypatch):
    """让假模型把队列里的异常对象**抛出来**，而不是当成返回值。

    为什么需要这一层：`FakeChat.chat_json` 只负责弹出预设值并返回它。
    直接把 `ModelError` 塞进队列的话，它会被当成"模型返回的数据"
    送进校验器，然后被判成"不是 JSON 对象"（validate_intent 已能挡住，
    见 test_non_iterable_input_is_reported_not_crashed）——
    那测的就不是"模型层错误该重试"，而是普通的不合格重试了。
    真实链路里 `chat_json` 是 raise（它把 `JSONDecodeError` 翻成
    `ModelError`），这里补上这一层。

    做法是替换 `app.main.chat_json` 本身，仍然复用 conftest 的
    队列与调用记录（`replies` / `calls`），保证断言口径不变。
    """
    def chat_json(messages, **kwargs):
        fake_chat.calls.append(list(messages))
        reply = fake_chat.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(m, "chat_json", chat_json)
    return fake_chat


def test_retry_feeds_the_problem_back_to_the_model(fake_chat):
    """★ 重试必须带上"上一次哪里错了"，否则重试只是重问一遍。

    这是"自我修正"能work的全部原因：把校验出的具体问题指出来，
    模型第二次基本能改对（见 `parse_intent` 里那段"自动重试为什么有效"）。
    少了这条反馈，重试的成功率和第一次一样，钱却照样花。
    """
    fake_chat.push(_valid(risk="bogus"), _valid(risk="high", action="restart"))
    m.parse_intent("重启 nginx")
    second = fake_chat.calls[1]
    assert [msg["role"] for msg in second[:2]] == ["system", "user"]
    # 追加了 上一轮 assistant 输出 + 一条 user 反馈
    assert second[2]["role"] == "assistant"
    assert "bogus" in second[2]["content"], "上一轮输出必须原样回灌"
    assert second[3]["role"] == "user"
    assert "risk" in second[3]["content"], "反馈里必须点明是哪个字段出的问题"


def test_retry_keeps_the_original_system_prompt(fake_chat):
    """重试时系统提示词不能丢 —— 丢了模型就不知道字段约定，
    第二次只会错得更离谱。
    """
    fake_chat.push(_valid(risk="bogus"), _valid())
    m.parse_intent("重启 nginx")
    for call in fake_chat.calls:
        assert call[0]["content"] == m.INTENT_SYSTEM_PROMPT


def test_failure_after_max_attempts_raises_intent_parse_failed(fake_chat):
    """重试用尽仍然不合格 → 抛 `IntentParseFailed`，不带病返回。

    返回 None 会让"解析失败"和"解析成功但字段为空"混成一种情况，
    调用方没法区分该重试还是该报错（见 `parse_intent` 的 docstring）。
    """
    bad = {"action": "restart"}
    fake_chat.push(bad, bad, bad)
    with pytest.raises(m.IntentParseFailed) as e:
        m.parse_intent("重启 nginx")
    assert e.value.problems, "异常里必须带上问题清单，否则没法定位"
    assert e.value.data == bad, "最后一次的原始输出要留着对账"


def test_exactly_max_parse_attempts_model_calls(fake_chat):
    """★ 重试次数必须**恰好**等于 `MAX_PARSE_ATTEMPTS`。

    这条是成本闸门：每次重试都是一次真实计费调用。
    少了 → 自我修正没生效；多了 → 每次解析失败都在多烧钱，
    而"多试一次"这种改动看起来人畜无害，最容易被随手加上去。
    """
    fake_chat.push(*[{}] * (m.MAX_PARSE_ATTEMPTS + 5))
    with pytest.raises(m.IntentParseFailed):
        m.parse_intent("重启 nginx")
    assert fake_chat.call_count == m.MAX_PARSE_ATTEMPTS
    assert fake_chat.replies, "多推的回复不该被消费掉"


def test_model_layer_error_also_counts_as_an_attempt_and_is_retried(fake_chat,
                                                                   monkeypatch):
    """模型层错误（网络抖动 / 返回不是 JSON）同样值得重试。

    这类失败占线上解析失败的一大半，只重试"字段不合格"是不够的
    （`except ModelError` 那个分支就是为它准备的）。
    """
    _make_queued_errors_raise(fake_chat, monkeypatch)
    fake_chat.push(ModelError("连不上模型服务"), _valid())
    intent, attempts = m.parse_intent("重启 nginx")
    assert intent == _valid()
    assert attempts == 2
    assert fake_chat.call_count == 2


def test_all_attempts_failing_at_model_layer_reports_the_error_text(fake_chat,
                                                                   monkeypatch):
    """连续模型层错误也要走到上限后抛 `IntentParseFailed`，并带上错误原因。"""
    _make_queued_errors_raise(fake_chat, monkeypatch)
    fake_chat.push(*[ModelError("请求超时")] * m.MAX_PARSE_ATTEMPTS)
    with pytest.raises(m.IntentParseFailed) as e:
        m.parse_intent("重启 nginx")
    assert fake_chat.call_count == m.MAX_PARSE_ATTEMPTS
    assert any("超时" in p for p in e.value.problems)


def test_model_layer_failure_leaves_last_output_empty(fake_chat, monkeypatch):
    """★ 已知问题：整轮都是模型层错误时，异常里的 `data` 是 None。

    复现：`chat_json` 连续抛 `ModelError` → `parse_intent` 走 `continue`
    分支，只更新了 `problems`，**没有记录任何"上一次输出"**，
    最终 `raise IntentParseFailed(..., last_data)` 带出去的是 None。

    后果：`/parse` 会把 `last_output: null` 返回给调用方，
    排障的人看到的是"解析失败 + 没有问题输出"，
    而真正的原因（超时/网络）在 problems 里 —— 前端只渲染 last_output 时
    就完全看不到线索。
    """
    _make_queued_errors_raise(fake_chat, monkeypatch)
    fake_chat.push(*[ModelError("请求超时")] * m.MAX_PARSE_ATTEMPTS)
    with pytest.raises(m.IntentParseFailed) as e:
        m.parse_intent("重启 nginx")
    assert e.value.data is None, "行为变了 —— 请更新这条用例并同步报告"


def test_null_reply_is_retried_then_reported(fake_chat):
    """模型返回裸 `null` 时，应当是**重试 + 最终报 IntentParseFailed**，
    而不是抛 TypeError 变成 HTTP 500。

    修复前的链路：模型输出裸 `null`（`app.llm.parse_json_reply` 会把它解析成
    Python 的 None 并通过 `chat_json` 返回）→ `validate_intent(None)` 在
    `if field not in data` 上抛
    `TypeError: argument of type 'NoneType' is not iterable` →
    `parse_intent` 的 try 只捕获 `ModelError`，异常穿过 `/parse` 与
    `/webhook/alert` 的 `except IntentParseFailed` → **HTTP 500**，
    那条告警不会被记成 `parse_failed`，也没有任何审计行，整次无人值守静默中断。

    修好之后它退化成一条正常路径：不合格 → 重试 → 到上限报错，
    而且**每一次尝试都留下了记录**（`_attempts` 出现在返回值与审计里）。
    """
    fake_chat.push(None, None, None)          # 每轮都回 null
    with pytest.raises(m.IntentParseFailed):
        m.parse_intent("重启 nginx")
    assert fake_chat.call_count == m.MAX_PARSE_ATTEMPTS, \
        "应当一直重试到上限，而不是第一轮就崩"


def test_null_reply_then_a_good_reply_recovers(fake_chat):
    """裸 `null` 之后模型改对了，整次解析必须成功 —— 这就是重试的价值。"""
    fake_chat.push(None, _valid())
    intent, attempts = m.parse_intent("重启 nginx")
    assert intent == _valid()
    assert attempts == 2


@pytest.mark.parametrize("bad_reply", [
    pytest.param({"action": "diagnose"}, id="missing-fields"),
    pytest.param(_valid(risk="critical"), id="bad-risk"),
])
def test_every_attempt_is_revalidated_not_just_the_first(fake_chat, bad_reply):
    """每一次返回都要重新校验 —— 不能因为"已经第二轮了"就放行。

    改动这一处（比如把校验挪到循环外）的后果是：
    模型随便回一个东西都会被当成解析成功，
    而这个产物会直接进入风险判定与动作选择。
    """
    fake_chat.push(bad_reply, bad_reply, bad_reply)
    with pytest.raises(m.IntentParseFailed):
        m.parse_intent("重启 nginx")
    assert fake_chat.call_count == m.MAX_PARSE_ATTEMPTS


def test_parse_intent_never_touches_the_network(fake_chat):
    """解析链路里除了模型入口，不该有第二个外部依赖（conftest 的禁网守卫兜住）。

    这条同时是"忘了打桩会立刻报错"的自证：本用例全程只走假模型。
    """
    fake_chat.push(_valid())
    m.parse_intent("查一下磁盘")
    assert fake_chat.patched, "conftest 没有替换到任何模型入口，测试实际会走真网络"
    assert "app.main.chat_json" in fake_chat.patched, \
        "app.main.chat_json 没被打桩 —— fake_chat 的替换目标清单可能变了"
