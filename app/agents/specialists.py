# -*- coding: utf-8 -*-
"""
四个专业 Agent —— 把一个大 Agent 拆成四个小 Agent
============================================================
这个文件回答一个问题：**拆成多个 Agent，到底多得到了什么？**

不是"听起来更高级"。拆分的真实收益有四条，每条都能验证：

    1. 上下文隔离
       一个 Agent 跑久了，对话历史里会混进日志、命令输出、文档片段……
       全都堆在同一个上下文里，互相干扰，还挤占窗口。
       拆开之后，每个 Agent 的上下文只装**自己那类数据**。

    2. 可单独评测
       "意图路由"和"结果校验"这两个 Agent **不调工具**，输入输出都是结构化 JSON，
       有标准答案 —— 可以单独算准确率。这是最容易量化的两个部件。
       而"一个什么都会的 Agent"，你只能端到端打分，出了错也不知道错在哪一步。

    3. 权限最小化
       工具执行 Agent **拿不到 search_knowledge**，因为它不该干这事。
       工具选择变少 → 模型选错的概率下降（工具越多选错率越高，这是模型特性）。

    4. 可以换不同档次的模型
       意图路由这种简单分类，用便宜的小模型就够；
       诊断推理才需要大模型。拆开才有这个空间。

    反过来说，**拆分的代价**也要讲清楚：

    - 多几次模型调用 → 成本和延迟上升
    - 多一层编排 → 出错时链路更长，排障更难
    - Supervisor 的决策逻辑本身可能成为新的 bug 来源

================================================================
四个 Agent 的分工
================================================================

    意图路由 (intent)       无工具。把「web-01 上的网站很慢」压成结构化标签：
                            task_type / hosts / services / needs_knowledge / symptoms
                            ★ 它管的是「语义」，不是「编排」

    知识检索 (knowledge)    调 search_knowledge。查历史工单、运维手册。

    工具执行 (diagnose)     调 5 个运维工具（**不含 search_knowledge**）。
                            内部是一个受限的 ReAct 子图。

    结果校验 (verify)       无工具。检查诊断结论有没有证据支撑、
                            有没有越权声明。**先跑规则，规则过不了才问模型**

================================================================
★ "意图路由 Agent" 和 "Supervisor" 到底重不重复？
================================================================
这是最容易被追问的地方，因为它看起来像是两件事在干同一件事。

**不重复，它们在不同的层：**

    Supervisor（在 supervisor.py）
        管**编排** —— 派活给谁、什么时候停、失败了要不要重来。
        它看的是"流程状态"：已经做过哪几步了、还差哪几步。

    意图路由 Agent（在本文件）
        管**语义** —— 把一句模糊的自然语言压成可判断的标签。
        它看的是"问题内容"：这是诊断还是咨询？涉及哪几台机器？

为什么值得分成两个？因为**职责混在一起的代价是可测性**：
意图分类可以单独拿出来算准确率（见 scripts/eval_specialists.py），
而如果分类逻辑埋在 Supervisor 的决策里，你只能端到端跑一遍才知道准不准。
"""

import json
import re

from app.agents.common import new_usage
from app.agents.graph import run as _react_run
from app.llm import ModelError, chat_json
from app.tools.ops import KNOWN_HOSTS, host_list_text

# ============================================================
# 一、意图路由 Agent
# ============================================================
# 这是一个**无工具 Agent**：不执行任何操作，只做一次分类，输出 JSON。
#
# 提示词的写法要点（和「结构化输出四要素」一致）：
#     身份 + 任务 + **字段定义（含取值范围与判断依据）** + 示例
#
# ★ 最容易漏的是"判断依据"。只写"task_type 只能是 X/Y/Z"，
#   模型只能猜；写清"什么时候算 diagnose、什么时候算 explain"，
#   同一个问题今天判和明天判才会一致。**这就是"可复现"。**
INTENT_SYSTEM = """你是一个运维请求的意图分类器。你的唯一任务是把用户的一句话压成结构化标签。

字段定义：

- task_type：请求的类型，五选一
    diagnose  用户报告了一个**具体的异常现象**，需要查明原因
              （例：网站报 502、容器一直重启、机器很卡）
    remediate 用户**明确要求执行一个处置动作**（不只是想知道原因）
              （例：帮我把日志清了、重启一下 nginx、把磁盘腾出空间）
              ★ remediate 和 diagnose 的区别：diagnose 是"帮我看看为什么"，
                remediate 是"帮我处理掉"。**用户要动手，才算 remediate。**
                只是"建议怎么处理"仍然是 diagnose。
    query     用户只是想知道**某个状态**，没有报告异常
              （例：db-01 现在负载多少、磁盘还剩多少）
    explain   用户在问**原理或做法**，不需要真实数据
              （例：为什么清理日志要用 truncate、怎么做滚动重启）
    other     以上都不是

- hosts：问题涉及的主机名数组。只能从下方「已知主机」清单里选。
          **没提到具体主机就返回空数组**，不要猜、不要默认填。

- services：涉及的软件或服务名数组（如 nginx、mysql、redis、docker）。
            没提到就返回空数组。

- needs_live_data：是否需要查看真实主机数据才可能回答。
    diagnose / query 一般为 true；explain 一般为 false。

- needs_knowledge：是否需要查历史故障处理经验或操作手册。
    现象描述得比较具体（有报错信息、有明确症状）→ true；
    只是问一个简单状态 → false。

- symptoms：用户提到的症状关键词数组（如 ["502", "响应慢"]）。没有就空数组。

- reason：一句话说明你的判断依据（不超过 40 字）。**必须写**，它是给人看的审计依据。

只输出 JSON 对象，不要任何解释文字。"""

# 已知主机清单从配置里取（见 app/tools/ops.py 的 KNOWN_HOSTS）。
# 写死在 prompt 里的话，换机器时得同时改代码、prompt、文档，漏一处就自相矛盾。
INTENT_SYSTEM += "\n\n已知主机：" + host_list_text() + "。"


# task_type 的合法取值。**定义一次，两处引用。**
#
# 原来是字面量写了两遍（校验一处、归一化一处）。加 remediate 的时候，
# 只改一处就会变成：校验通过 → 归一化时被判回 "other" → 静默走错分支。
# 这正是 _clamp_intent 自己的注释里警告的那类 bug —— 只不过这次的坑
# 在"同一个列表写了两遍"。**重复定义是这类 bug 的固定来源。**
_TASK_TYPES = ("diagnose", "remediate", "query", "explain", "other")


def _clamp_intent(data: dict) -> tuple:
    """校验并归一化意图标签。返回 (cleaned, problems)。

    【为什么必须校验，而且必须在这里校验】
    这个字段的取值会**决定 Supervisor 走哪条分支**。
    如果 task_type 返回了一个没定义的字符串，而代码里是
    `if task_type == "diagnose"` 这种判断，那它会静默走进 else 分支 ——
    **不报错、但行为完全是错的**。

    这类"错误的值导致错误的分支、且不报错"的问题，是 Agent 系统里最难查的
    一类 bug。所以每个取值都要在边界上卡住。
    """
    problems = []

    task_type = data.get("task_type")
    if task_type not in _TASK_TYPES:
        problems.append(f"task_type 取值不合法：{task_type!r}，"
                        f"只允许 {list(_TASK_TYPES)}")

    hosts = data.get("hosts")
    if not isinstance(hosts, list):
        problems.append(f"hosts 必须是数组，收到 {type(hosts).__name__}")
        hosts = []
    unknown = [h for h in hosts if h not in KNOWN_HOSTS]
    if unknown:
        # 不直接判错，而是剔除并记录：模型偶尔会把 "web01" 写成 "web-01" 之外的形式，
        # 剔除后仍可继续，但要让调用方看到发生过什么
        problems.append(f"hosts 里有未知主机：{unknown}（已知 {KNOWN_HOSTS}）")
        hosts = [h for h in hosts if h in KNOWN_HOSTS]

    services = data.get("services")
    if not isinstance(services, list):
        problems.append(f"services 必须是数组，收到 {type(services).__name__}")
        services = []

    symptoms = data.get("symptoms")
    if not isinstance(symptoms, list):
        symptoms = []

    cleaned = {
        "task_type": task_type if task_type in _TASK_TYPES else "other",
        "hosts": hosts,
        "services": [str(s) for s in services],
        "symptoms": [str(s) for s in symptoms],
        # 布尔字段用 bool() 归一化：模型有时返回 "true" 字符串
        "needs_live_data": bool(data.get("needs_live_data")),
        "needs_knowledge": bool(data.get("needs_knowledge")),
        "reason": str(data.get("reason") or "")[:120],
    }
    return cleaned, problems


def route_intent(question: str, max_attempts: int = 3) -> dict:
    """意图路由 Agent：把一句人话压成结构化标签。

    【为什么要自己写重试，而不是直接用 chat_json】
    `chat_json` 只负责"拿到 JSON"，它不知道这些字段**该怎么取值**。
    校验失败时，把**具体哪里不合格**反馈给模型让它自己改 ——
    这个套路在 `/parse` 接口里验证过，成功率能明显提升。

    返回：
        {"intent": {...}, "attempts": n, "problems": [...], "elapsed_ms": ms}
    """
    import time
    started = time.time()
    messages = [{"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": question}]
    last_problems = []

    for attempt in range(1, max_attempts + 1):
        try:
            data = chat_json(messages, temperature=0)
        except ModelError as e:
            return {"intent": _fallback_intent(question), "attempts": attempt,
                    "problems": [f"模型调用失败：{e}"],
                    "elapsed_ms": int((time.time() - started) * 1000)}

        cleaned, problems = _clamp_intent(data)
        if not problems:
            return {"intent": cleaned, "attempts": attempt, "problems": [],
                    "elapsed_ms": int((time.time() - started) * 1000)}

        last_problems = problems
        # ★ 把问题反馈回去，而不是简单地重问一遍。
        #   模型看到自己错在哪，第二次基本能改对。
        messages.append({"role": "assistant",
                         "content": json.dumps(data, ensure_ascii=False)})
        messages.append({"role": "user", "content":
                         "你上一次的输出有以下问题：" + "；".join(problems) +
                         "。请只输出修正后的 JSON 对象，不要任何解释文字。"})

    return {"intent": _fallback_intent(question), "attempts": max_attempts,
            "problems": last_problems,
            "elapsed_ms": int((time.time() - started) * 1000)}


def _fallback_intent(question: str) -> dict:
    """降级方案：模型连续失败时用的保守默认值。

    【为什么要有降级，而不是直接抛异常】
    意图分类只是"路由用的标签"，不是最终结论。
    它失败不该让整个多 Agent 流程崩掉 ——
    给一个**最保守的默认值**（按诊断处理、不限主机），
    后面的 Agent 仍然能干活，只是在轨迹里记下"路由是降级的"。
    这比整个请求 500 要好得多。

    这是"优雅降级"：**非核心组件失败，不应该拖垮核心链路。**
    """
    return {
        "task_type": "diagnose",
        "hosts": [],
        "services": [],
        "symptoms": [],
        "needs_live_data": True,
        "needs_knowledge": True,
        "reason": "意图分类失败，降级为最保守的默认值",
        "_degraded": True,
    }


# ============================================================
# 二、知识检索 Agent
# ============================================================
def retrieve_knowledge(question: str, intent: dict, top_k: int = 3) -> dict:
    """知识检索 Agent：查历史故障经验与操作手册。

    【为什么要单独一个 Agent，而不是让诊断 Agent 顺手查】
    两个理由：
      1. **上下文隔离**：知识库返回的是一大段文档片段。
         如果让诊断 Agent 去查，这些片段会和日志、命令输出混在同一个上下文里，
         既挤窗口又互相干扰。
      2. **检索词不同**：诊断 Agent 的检索词会带上大量现场数据（主机名、具体命令），
         那反而不利于检索"通用经验"。单独的 Agent 用**症状关键词**去查，效果更好。

    【检索词怎么构造，是个真问题】
    直接把用户原话丢进去检索，会带上"帮我看下""怎么处理"这类无意义的词，
    反而稀释了关键词。所以这里用 **症状 + 服务名** 拼检索词 ——
    这是意图路由 Agent 的产出被真正用上的地方。
    """
    from app.rag.pipeline import load_store

    keywords = []
    keywords += intent.get("symptoms") or []
    keywords += intent.get("services") or []
    # 症状和服务名都没有时，退回用原问题（总不能空着查）
    query = " ".join(keywords) if keywords else question
    # 补一个指向性词，让检索偏向"处理办法"而不是"现象描述"
    # remediate 也要查 —— 处置前先看有没有现成的操作手册，
    # 这比模型自己发挥可靠得多。
    if intent.get("task_type") in ("diagnose", "query", "remediate"):
        query = f"{query} 排查 处理".strip()

    try:
        store = load_store()
        hits = store.search(query, top_k=top_k, mode="hybrid")
    except Exception as e:
        # 知识库不可用不该让整个流程失败 —— 诊断 Agent 还能靠现场数据干活
        return {"query": query, "count": 0, "results": [],
                "error": f"{type(e).__name__}: {e}"}

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
# 三、工具执行 Agent（受限 ReAct 子图）
# ============================================================
# ★ 注意这里没有 search_knowledge —— 查知识库是上面那个 Agent 的职责。
#   这就是"权限最小化"的具体落地，不是嘴上说说。
DIAGNOSE_TOOLS = ["check_disk", "check_load", "check_service",
                  "list_containers", "tail_log"]

DIAGNOSE_SYSTEM = """你是一个 Linux 运维诊断执行者。你的职责是**用工具查现场数据**并给出诊断结论。

你的权限：
- 只能调用给你的这几个只读工具
- **不能**查知识库（那由另一个 Agent 负责，相关经验会以「参考经验」形式给你）
- **不能**执行任何修改操作。可以给建议，但不能声称你做过

工作要求：
1. 先想清楚需要哪些信息，再决定调哪个工具。一次可以调多个。
2. 「参考经验」只用来帮你判断**方向**（该查什么），
   但结论**必须**由工具返回的真实数据支撑。经验里提到的问题，你也要用工具去核实。
3. 严禁编造。工具没返回的数据不能出现在结论里。
4. 不要输出"我已经收集到足够信息"这类过程性说明，直接给结论。
5. 最终回答结构：
   - **现象**：一句话概括
   - **依据**：列出关键数据（**必须带具体数值，且这些数值必须来自工具返回**）
   - **根因**：你的判断 + 置信度（确定 / 可能 / 需要更多信息）
   - **建议**：具体处置步骤，按优先级排"""


def diagnose(question: str, intent: dict, knowledge: dict = None,
             max_steps: int = 6, tool_names: list = None,
             previous_problems: list = None,
             previous_evidence: list = None) -> dict:
    """工具执行 Agent：跑一个受限的 ReAct 子图。

    【"受限"具体体现在哪】
    `tool_names` 决定这个子图的工具清单。工具执行模式给 5 个运维工具，
    模型连 search_knowledge 的 schema 都看不到，所以它**不可能**去调。

    这比在提示词里写"请不要查知识库"可靠得多 ——
    **提示词是请求，schema 是约束。** 能用结构约束的，就不要靠模型自觉。

    传空列表 `[]` 就是**纯推理模式**（没有手可以伸出去），
    用于"不需要查现场"的问题 —— 同一个 Agent 实现，两种模式。

    【参考经验怎么给】
    知识库的检索结果被压成"文件名 + 一句话摘要"再传进来。
    完整片段有几百字，塞进来只会挤占上下文 ——
    这是 Supervisor 作为"信息中介"该做的裁剪。

    【重查时为什么要带上一轮的问题】
    校验没通过就重跑一遍同样的问题，等于期待"运气不一样"。
    把上一次的具体问题作为提示带进去，模型才会**换一个角度**查 ——
    比如上次漏了看容器状态，这次就会去看。
    这是"重试"和"瞎重试"的区别。
    """
    if tool_names is None:
        tool_names = DIAGNOSE_TOOLS

    # 组装子问题：原问题 + 已知线索 + 参考经验 + （重查时的）上轮问题
    parts = [f"【用户的问题】{question}"]

    if intent.get("hosts"):
        parts.append(f"【涉及主机】{', '.join(intent['hosts'])}")
    if intent.get("services"):
        parts.append(f"【涉及服务】{', '.join(intent['services'])}")
    if intent.get("symptoms"):
        parts.append(f"【用户描述的症状】{', '.join(intent['symptoms'])}")

    if knowledge and knowledge.get("results"):
        lines = [f"- {r['source']}：{r['preview'][:80]}"
                 for r in knowledge["results"][:3]]
        parts.append("【参考经验（仅供判断方向，结论必须用工具数据核实）】\n"
                     + "\n".join(lines))

    if previous_problems:
        parts.append(
            "【上一次的结论没有通过校验，这些问题必须解决】\n"
            + "\n".join(f"- {p}" for p in previous_problems[:5])
            + "\n请从**不同的角度**补充数据来支撑或修正结论，"
              "不要只是把上一次的话重说一遍。")

    # ★ 把已经查过的数据告诉它 —— 否则重查会把同样的工具再跑一遍。
    #
    #   这不是"省钱"这么简单：子 Agent 每次启动都是一个全新的对话，
    #   **它不知道自己上一轮查过什么**。不告诉它，它就会：
    #       第 1 次：check_disk, check_load, check_service, list_containers
    #       第 2 次：check_disk, check_load, check_service, list_containers  ← 又一遍
    #   然后再次撞上工具调用上限，永远查不到真正缺的那部分数据。
    #   （第一次跑的时候就真实发生了：第二次重查把额度用光，连日志都没读到。）
    #
    #   **跨轮次的记忆缺失，是"重试"退化成"重来一遍"的根本原因。**
    if previous_evidence:
        digest = "\n".join(
            f"- {str(item).replace(chr(10), ' ')[:150]}"
            for item in previous_evidence[:8])
        parts.append(
            "【已经查过的数据（不要重复调用同样的工具）】\n"
            + digest
            + "\n请把有限的工具调用额度用在**还没查过**的数据上。")

    result = _react_run("\n\n".join(parts), max_steps=max_steps,
                        tool_names=tool_names,
                        system_prompt=DIAGNOSE_SYSTEM)

    # 把工具返回的完整文本抽出来 —— 给校验 Agent 做数值溯源用
    evidence = [s["observation_text"] for s in result["steps"]]

    return {
        "answer": result["answer"],
        "steps": result["steps"],
        "evidence": evidence,
        "rounds": result["rounds"],
        "tool_calls": result["tool_calls"],
        "tools": result["distinct_tools"],
        "usage": result["usage"],
        "stop_reason": result["stop_reason"],
        "elapsed_ms": result["elapsed_ms"],
        "mode": "inspect" if tool_names else "reason",
    }


# ============================================================
# 三·五、处置 Agent —— 唯一一个有「写权限」的 Agent
# ============================================================
# ★ 为什么它必须是**独立的一个 Agent**，而不是给诊断 Agent 加个工具？
#
#   因为「诊断」和「处置」是两种性质完全不同的活动：
#
#       诊断   只读。错了的代价 = 结论不准，人一眼能看出来
#       处置   会改系统。错了的代价 = 服务中断、数据丢失
#
#   把这两种权限混在一个 Agent 手里，就等于为了让它可以重启服务，
#   顺带把"随时能重启服务"的能力给了它做诊断的每一步。
#   **权限一旦给出去，就没有"只在这一步有效"这回事。**
#
#   拆开之后：
#       诊断 Agent 的工具集 = 5 个只读工具，**它连 run_command 长什么样都不知道**
#       处置 Agent 的工具集 = 只有 run_command，而且里面全是需要审批的写操作
#
#   这样即使诊断 Agent 被 Prompt 注入攻破，它也做不了任何改动 ——
#   因为它手上根本没有那个工具。**这是结构上的隔离，不是提示词上的约定。**
#
#   （同一招在 MCP 里也用过：schema 里没有某工具 = 客户端根本调不到。
#     在工具清单里也用过：删掉 search_knowledge = 它根本调不到。
#     **能用结构约束的，就不要靠提示词请求。**）
REMEDIATE_TOOLS = ["run_command"]

REMEDIATE_SYSTEM = """你是一个运维处置执行者。你的职责是**把诊断结论落实成一个具体动作**。

你只有一个工具：run_command，它只能执行白名单命令。

【你必须理解的规则】
1. 命令必须在白名单里，格式必须完全正确。不在白名单里的命令会被直接拒绝。
2. **写操作（重启服务、清理日志）不会立刻执行** —— 它会变成一张人工审批单。
   这是设计如此，不是你操作失败。
3. 提交审批单之后，你的回答要**如实说明"等待人工确认"**，
   **绝对不要**说成"已经执行完成"、"已清理"、"已重启"。
   即使你「觉得」它一定会被批准，你也不能那么说 —— 那是事实错误。

【可以做的事】
- 清理日志：用 `truncate -s 0 <日志路径>`，**不要用 rm**
  （rm 之后正在写日志的进程还攥着文件句柄，空间不会释放；
   truncate 把长度截为零，句柄仍有效，空间立刻释放）
- 重启服务：`systemctl restart <服务名>`、`docker restart <容器名>`
- 只读核查：`df -h`、`tail -n <行数> <日志路径>`、`systemctl is-active <服务名>`

【工作要求】
1. 只做诊断结论明确要求的动作，不要顺手多做。
2. 一次只提交一个动作，方便人逐条判断。
3. 最终回答结构：
   - **要做什么**：一句话
   - **命令**：完整命令原文
   - **为什么**：基于哪条诊断依据（带数值）
   - **当前状态**：已提交审批 / 已执行 / 被策略拒绝，如实说
   - **需要人工做什么**：如果提交了审批，说明批准后如何执行"""


def remediate(question: str, intent: dict, diagnosis: dict,
              max_steps: int = 6) -> dict:
    """处置 Agent：跑一个只能用 run_command 的受限 ReAct 子图。

    【为什么把「诊断结论」作为输入传进来，而不是让它自己重新判断】
    因为处置动作必须**有据可依**。如果它自己去查数据、自己下结论、自己动手，
    那"依据"和"动作"之间就没有可追溯的关系了 ——

        诊断 Agent 说「磁盘 96% 满，建议清理 /var/log」
        处置 Agent 执行「truncate -s 0 /var/log/nginx/error.log」

    这两句话之间是有一条线的，审批人看到的正是这条线。
    如果处置 Agent 自己重新判断一遍，它可能得出别的结论，
    于是**审批人批的和实际执行的就对不上了**。

    **让每一步都建立在上一部的产出之上，是可追溯性的前提。**
    """
    parts = [f"【用户的问题】{question}"]

    if intent.get("hosts"):
        parts.append(f"【涉及主机】{', '.join(intent['hosts'])}")
    if intent.get("services"):
        parts.append(f"【涉及服务】{', '.join(intent['services'])}")

    parts.append("【诊断结论（你的动作必须基于这个结论）】\n"
                 + (diagnosis.get("answer") or "（无诊断结论）")[:1500])

    if diagnosis.get("tools"):
        parts.append(f"【诊断已查过的工具】{', '.join(diagnosis['tools'])}")

    result = _react_run("\n\n".join(parts), max_steps=max_steps,
                        tool_names=REMEDIATE_TOOLS,
                        system_prompt=REMEDIATE_SYSTEM)

    # ★ 从工具调用记录里把「审批单」和「执行的命令」抽出来。
    #   这些不是我另外维护的状态，而是**从真实发生过的调用里读出来的** ——
    #   如果 Agent 声称执行了但轨迹里没有对应的工具调用，那就是在编。
    approvals, executed, denied = [], [], []
    for step in result["steps"]:
        if step.get("tool") != "run_command":
            continue
        try:
            payload = json.loads(step.get("observation_text") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        record = payload.get("result") or payload
        decision = record.get("decision")
        entry = {
            "command": record.get("command"),
            "purpose": record.get("purpose"),
            "reason": record.get("reason"),
            "isolation": record.get("isolation"),
        }
        if decision == "needs_approval" and record.get("approval_id"):
            entry["approval_id"] = record["approval_id"]
            entry["expires_at"] = record.get("expires_at")
            approvals.append(entry)
        elif decision == "allow" and record.get("executed"):
            entry["output"] = (record.get("result") or {}).get("stdout", "")[:400]
            executed.append(entry)
        elif decision == "deny":
            entry["error"] = record.get("error")
            denied.append(entry)

    return {
        "answer": result["answer"],
        "steps": result["steps"],
        "rounds": result["rounds"],
        "tool_calls": result["tool_calls"],
        "tools": result["distinct_tools"],
        "usage": result["usage"],
        "stop_reason": result["stop_reason"],
        "elapsed_ms": result["elapsed_ms"],
        "approvals": approvals,     # 待人工确认的
        "executed": executed,       # 已执行的（只读命令）
        "denied": denied,           # 被策略拒绝的
    }


# ============================================================
# 四、结果校验 Agent
# ============================================================
# ★★ 这是四个 Agent 里设计最讲究的一个，也是最值得展开讲的一处。
#
# 【为什么不能只让模型"再检查一遍"】
# 让模型自检有个根本问题：**它检查的是同一批信息，用的是同一个脑子。**
# 它编错了，再问一遍它多半还是觉得没错。这叫"自证清白"，说服力很低。
#
# 【正确做法：能规则化的检查用规则，规则查不了的才用模型】
#
#     规则能查的（确定性强、可复现、零成本）：
#         · 结论里的数字能不能在工具返回里找到出处  ← 数值溯源
#         · 有没有越权声明（"我已经清理了…"）
#         · 有没有证据就下了结论
#
#     只有规则查不了的（逻辑自洽性）才交给模型：
#         · 依据和根因之间的因果链站不站得住
#         · 有没有夸大置信度
#
# **这个顺序很重要**：规则先跑。规则能判否就直接否，一次模型调用都不花。
# 这不是省钱，是**把确定性最强的判断放在最前面** ——
# 规则不会有幻觉，模型会有。

# 越权声明的关键词。这个 Agent 只有只读权限，出现这些说法就是越权。
_OVERREACH_PATTERNS = [
    r"我已(经)?(清理|删除|重启|关闭|修改|执行|启动|停止)",
    r"操作已完成", r"已经帮你(清理|删除|重启)",
    r"(已|成功)(清理|删除|重启)了",
]

# 允许出现在结论里、但不需要出处的数字。
# 1~3 这种小数字通常是列表序号（"1. 清理日志"），追它没有意义。
_MIN_SOURCED_DIGITS = 2


_SECTION_RE = re.compile(r"\*\*(现象|依据|根因|建议)\*\*")


def split_sections(claim: str) -> dict:
    """把结论按 **现象** / **依据** / **根因** / **建议** 切成段。

    【为什么需要它 —— 这是第二版才想明白的事】

    第一版把"结论里的所有数字"都拿去溯源，结果误报了两类：
        1. 用户问题里提到的数字（比如用户说"报 502"）→ 已修：来源池加上问题
        2. **建议里给出的参数值**：`chmod 755`、`chown 999:999`、
           阈值"降到 80% 以下"—— 这些是**建议值，不是声称的数据**，
           当然不可能在工具返回里找到出处

    第 2 类的本质是：**溯源只对"声称的事实"有效，对"给出的建议"无效。**
    一个数字出现在「依据」里，是断言"我查到了这个值"；
    出现在「建议」里，是断言"你应该用这个值"。**两者根本不该用同一个标准检查。**

    所以溯源必须**限定在「现象」和「依据」两段** —— 这两段才是"事实主张"。

    ★ 这是校验类代码的通病：**规则本身是对的，但作用范围划错了，
      就会把正常行为判成违规。** 误报比漏报更消耗信任 ——
      一个总在报假警报的校验器，最后会被所有人忽略。
    """
    marks = list(_SECTION_RE.finditer(claim))
    if not marks:
        return {}
    out = {}
    for i, m in enumerate(marks):
        seg_start = m.end()
        seg_end = marks[i + 1].start() if i + 1 < len(marks) else len(claim)
        out[m.group(1)] = claim[seg_start:seg_end]
    return out


# 允许出现在结论里、但不需要出处的数字。
# 1 位数通常是列表序号或单位（"1. 清理日志"、"2 核"），追它没有意义。
_MIN_SOURCED_DIGITS = 2


def check_numbers(claim: str, evidence: list, question: str = "") -> tuple:
    """数值溯源：结论里声称的数据，必须能在工具返回或用户问题里找到出处。

    返回 (problems, scope_note)：
        problems   发现的问题（空列表 = 通过）
        scope_note 本次检查覆盖了哪些段落（给人看的说明）

    【为什么要检查"依据"这一段】
    模型编造结论时最常见的形态不是整句瞎编，而是**数字对不上**：
    把 96% 记成 98%，或者把"使用率 96%"说成"剩余 96%"。
    整句读起来很合理，但数字在证据里根本找不到。

    **这是人肉审阅最容易被骗过去的地方** —— 因为读者默认数字有出处。
    所以交给机器逐字比对。

    【已知局限，必须说清楚】
    1. 用「子串包含」判断，所以 "96" 会命中 "1960"
    2. **派生值会误报** —— "可用内存 338MB，占总量 9.5%"，
       那个 9.5 是算出来的，工具没直接返回过
    3. 分段依赖模型遵守输出格式；分不出段落时**主动跳过并说明**，
       而不是退回"检查全文"（那就会把建议里的参数值也算进来）

    **先做便宜的近似，等误报多到影响使用，再升级。**
    主动说出局限比被追问出来好。
    """
    sections = split_sections(claim)
    if sections:
        # 只查「现象」和「依据」—— 这两段才是事实主张
        scope = sections.get("现象", "") + "\n" + sections.get("依据", "")
        scope_note = "已覆盖「现象」「依据」两段"
    else:
        # 分不出段落说明模型没按格式输出。这时候**宁可跳过，也不要查全文** ——
        # 查全文会把「建议」里的 chmod 755、阈值 80% 全算成"没出处"。
        return [], "结论未按段落格式输出，数值溯源已跳过（避免把建议值误判为编造）"

    # ★ 来源池 = 工具返回 + 用户的问题
    pool = "\n".join(list(evidence or []) + [question or ""])
    if not pool.strip():
        return [], "没有工具返回数据，数值溯源已跳过"

    # ★ 第三类误报：主机名里的数字
    #   `web-01`、`cache-01`、`/dev/vda1` 里的 01 / 1 会被 \d+ 抓出来，
    #   然后判成"没出处的数字"。但它是**标识符的一部分，不是测量值**。
    #
    #   修法：用否定环视排掉"紧挨着字母/数字/下划线/点/连字符"的数字 ——
    #   也就是只认**独立出现的数字**。
    #
    #   这是同一个坑的第三次：**校验规则每加一层范围限定，
    #   就暴露出一类新的"正常但不该被检查"的东西。**
    #   写校验器时要有预期：第一版一定误报，靠真实数据一轮轮收窄。
    claimed = set()
    for raw in re.findall(r"(?<![A-Za-z0-9_.\-])(\d+(?:\.\d+)?)", scope):
        if len(raw.replace(".", "")) < _MIN_SOURCED_DIGITS:
            continue
        claimed.add(raw)

    unsourced = sorted(n for n in claimed if n not in pool)
    if not unsourced:
        return [], scope_note
    return ([f"「依据」中这些数值在工具返回和用户问题中都找不到出处：{unsourced}"
             f"（可能是模型自己算的或编的，需人工核对）"], scope_note)


def _check_overreach(claim: str) -> list:
    """越权检查：只读权限的 Agent 不能声称自己做了修改操作。

    【为什么这条必须用规则，不能交给模型】
    这是**合规红线**，不是"质量建议"。
    红线的判断必须是确定的、可复现的、可举证的 ——
    交给模型判，它今天说有、明天说没有，你就没法拿它当门禁。
    """
    for pattern in _OVERREACH_PATTERNS:
        hit = re.search(pattern, claim)
        if hit:
            return [f"结论里出现越权声明：{hit.group(0)!r}。"
                    f"本 Agent 只有只读权限，不允许声称执行过任何操作"]
    return []


VERIFY_SYSTEM = """你是一个诊断结论的审核员。你不查数据，只判断别人的结论站不站得住。

给你三样东西：用户的问题、工具返回的原始数据、诊断者给出的结论。

你要检查的只有一件事（数字有没有出处、有没有越权，机器已经检查过了，不用你管）：

**依据能不能推出根因？**
- 每条「依据」是不是真的指向那个「根因」？有没有跳步？
- 置信度标得合不合理？（只有间接证据却标"确定" ← 这是最常见的问题）
- 有没有把"相关"当成"因果"？（比如磁盘满和容器重启同时发生，但日志显示是权限问题）

只输出 JSON：
{
  "pass": true 或 false,
  "problems": ["问题1", "问题2"],       // 没问题就空数组
  "note": "一句话总评（不超过 50 字）"
}

要求：
- 只针对逻辑问题，不要复述数据、不要重写结论
- 拿不准就 pass 为 true，把疑问写进 note —— 不要因为"可能有问题"就否掉
- 不要输出 JSON 之外的任何内容"""


def verify(question: str, diagnosis: dict, intent: dict,
           use_model: bool = True) -> dict:
    """结果校验 Agent：先规则，后模型。

    返回：
        {
          "pass": bool,
          "problems": [...],        # 具体问题，可直接展示给用户
          "source": "rule" | "model" | "skipped",
          "note": str,
          "elapsed_ms": ms,
          "usage": {...},
        }
    """
    import time
    started = time.time()
    claim = diagnosis.get("answer") or ""
    evidence = diagnosis.get("evidence") or []
    problems = []

    # ---------- 第一关：规则 ----------
    if not claim.strip():
        problems.append("诊断 Agent 没有给出任何结论")

    if not evidence and claim.strip():
        # 没有任何工具数据却给了结论 —— 最严重的失败模式
        problems.append("没有任何工具返回数据，却给出了诊断结论")

    num_problems, scope_note = check_numbers(claim, evidence, question)
    problems += num_problems
    problems += _check_overreach(claim)

    # ★ 规则能判否就直接判否，一次模型调用都不花。
    #   规则是确定的，模型是概率的 —— 让确定的东西先说话。
    if problems:
        return {"pass": False, "problems": problems, "source": "rule",
                "note": f"规则检查未通过（{scope_note}），未进入模型审核",
                "elapsed_ms": int((time.time() - started) * 1000),
                "usage": new_usage()}

    if not use_model:
        return {"pass": True, "problems": [], "source": "skipped",
                "note": f"规则全部通过（{scope_note}；模型审核已关闭）",
                "elapsed_ms": int((time.time() - started) * 1000),
                "usage": new_usage()}

    # ---------- 第二关：模型判逻辑 ----------
    payload = (
        f"【用户的问题】\n{question}\n\n"
        f"【工具返回的原始数据】\n" + "\n---\n".join(evidence)[:6000] +
        f"\n\n【诊断者给出的结论】\n{claim}"
    )
    try:
        data = chat_json([{"role": "system", "content": VERIFY_SYSTEM},
                          {"role": "user", "content": payload}],
                         temperature=0)
    except ModelError as e:
        # 审核员自己挂了 —— 不能因此否掉结论，但必须标记出来。
        # 这属于"校验不完整"，不是"校验不通过"。
        return {"pass": True, "problems": [], "source": "model_failed",
                "note": f"模型审核不可用（{e}），仅通过规则检查",
                "elapsed_ms": int((time.time() - started) * 1000),
                "usage": new_usage()}

    model_pass = bool(data.get("pass"))
    model_problems = data.get("problems")
    if not isinstance(model_problems, list):
        model_problems = []
    model_problems = [str(p)[:200] for p in model_problems]

    # ★ 防一手"假通过"：模型说 pass=true 却又列了问题，这是自相矛盾。
    #   遇到矛盾时按**更保守**的一方处理（判不通过），
    #   因为漏放一个错的结论，比多拦一个对的结论代价大得多。
    if model_pass and model_problems:
        model_pass = False
        model_problems.append("（模型同时给了 pass=true 和问题列表，按保守处理判为不通过）")

    # 数值溯源遇到"无法分段"时是主动跳过的，这件事必须让调用方看到 ——
    # **校验器说"我没查"和"我查了没问题"是两回事，不能混在一起。**
    model_note = str(data.get("note") or "")[:120]
    if "跳过" in scope_note:
        model_note = f"[数值溯源跳过：{scope_note}] {model_note}".strip()

    return {
        "pass": model_pass,
        "problems": model_problems,
        "source": "model",
        "note": model_note,
        "elapsed_ms": int((time.time() - started) * 1000),
        "usage": new_usage(),
    }


# ============================================================
# 五、汇总：把四个 Agent 的产出合成最终回答（Supervisor 用）
# ============================================================
def compose_answer(question: str, intent: dict, knowledge: dict,
                   diagnosis: dict, verdict: dict, config: dict,
                   remediation: dict = None) -> str:
    """把各 Agent 的产出拼成最终回答。

    【为什么用模板拼，而不是再让模型"总结一下"】
    再调一次模型有两个坏处：
      1. 它会**改写数字**（这是真实存在的现象，模型在复述时容易说错）
      2. 多一次调用 = 多一次失真机会 + 多花钱

    而结论本身已经是模型生成的，我只需要**加上编排层才知道的信息**：
    走过哪些 Agent、校验结果如何、引用了哪几篇文档。

    **让编排层干"编排"的事，不要越界去重新生成内容。**
    这一条原则在很多系统里都成立：**汇总不等于重写。**
    """
    parts = []

    if config.get("show_routing", True):
        parts.append(
            f"> 路由：{intent.get('task_type')}"
            f"｜主机 {intent.get('hosts') or '未指定'}"
            f"｜服务 {intent.get('services') or '未指定'}"
            f"｜查知识库 {'是' if intent.get('needs_knowledge') else '否'}"
            + (f"\n> 判断依据：{intent['reason']}" if intent.get("reason") else "")
        )

    parts.append(diagnosis.get("answer") or "（诊断 Agent 未给出结论）")

    if knowledge.get("results"):
        refs = "、".join(r["source"] for r in knowledge["results"])
        parts.append(f"**参考文档**：{refs}")

    if config.get("show_verification", True):
        v = verdict
        icon = "通过" if v.get("pass") else "未通过"
        block = [f"**结论校验**：{icon}（{v.get('source')}）"]
        if v.get("note"):
            block.append(f"- {v['note']}")
        for p in v.get("problems") or []:
            block.append(f"- {p}")
        if not v.get("pass"):
            block.append("- **以上问题需要人工核对后再采用本结论**")
        parts.append("\n".join(block))

    # ---- ★ 处置结果 ----
    # 这一段的作用只有一个：**让"想做什么"和"已经做了什么"在回答里分得清清楚楚。**
    #
    # 不这么写会出什么事？—— 处置 Agent 的提示词里我写了"不要说成已执行"，
    # 但提示词是请求。**这里用结构保证：待审批的动作永远出现在
    # 「待人工确认」这个小标题下面，而不是混在诊断结论的正文里。**
    # 人扫一眼就知道现在系统改了没有。
    rem = remediation or {}
    if rem:
        blocks = []

        if rem.get("approvals"):
            lines = ["**待人工确认（尚未执行，需要有人批准）**"]
            for a in rem["approvals"]:
                lines.append(f"- `{a['command']}`")
                lines.append(f"  - 审批单：`{a['approval_id']}`"
                             f"（{a.get('expires_at', '')} 前有效）")
                lines.append(f"  - 原因：{a.get('reason') or '-'}")
                if a.get("purpose"):
                    lines.append(f"  - 说明：{a['purpose']}")
                lines.append(f"  - 通道：{a.get('isolation')}"
                             f"（{'真隔离' if a.get('isolation') == 'container' else '无容器隔离'}）")
            lines.append("")
            lines.append("> 批准：`POST /approvals/{id}/approve`　"
                         "执行：`POST /approvals/{id}/execute`")
            blocks.append("\n".join(lines))

        if rem.get("executed"):
            lines = ["**已执行（只读命令）**"]
            for e in rem["executed"]:
                lines.append(f"- `{e['command']}`")
                if e.get("output"):
                    # 只取前几行，完整输出在接口响应里
                    head = "\n".join(e["output"].splitlines()[:4])
                    lines.append(f"  ```\n  {head}\n  ```")
            blocks.append("\n".join(lines))

        if rem.get("denied"):
            lines = ["**被策略拒绝（未执行）**"]
            for d in rem["denied"]:
                lines.append(f"- `{d['command']}` —— {d.get('error') or d.get('reason')}")
            blocks.append("\n".join(lines))

        if not blocks:
            # 处置 Agent 跑过但什么都没提 —— 这也是信息，要说出来。
            # "没提议处置"和"没跑处置"是两件事，人需要能区分。
            blocks.append(f"**处置**：未提出任何动作\n\n{rem.get('answer', '')[:300]}")

        parts.append("\n\n".join(blocks))

    return "\n\n".join(parts)
