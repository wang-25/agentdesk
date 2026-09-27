"""RAG 评测的判定层。

【为什么判定要分规则和模型两层】

规则判定（确定、可复现、零成本、不会幻觉）：
    检索有没有命中 / 引用编号有没有越界 / 数字能不能在资料里找到 / 该不该拒答

模型判定（只能它判）：
    这段回答跑不跑题 / 有没有超出资料范围编造

顺序上**规则先跑** —— 一个"引用了第 9 条资料但只检索到 5 条"的问题，
一眼就能看出来，没必要花一次模型调用去问。这和 `app/agents/specialists.py`
里校验 Agent 的设计是同一条原则，因为评测判定层本质上也是"校验器"。

【★ 最重要的一条纪律】

**判定器本身必须被验证。**

一个总是返回"通过"的判定器，比没有判定器更糟 —— 它会给出一份看起来
很漂亮、实际上毫无意义的评测报告。所以本模块提供 `verify_judge()`：
用构造好的「好答案 / 跑题答案 / 编造答案」去喂判定器，
三个都要判对才算这个尺子是准的。

跑完整评测之前必须先过这一关。理由很简单：
**尺子不准，量出来的数字全是假的。**
"""

from __future__ import annotations

import re

from app.llm import ModelError, chat_json

# ============================================================
# 一、规则判定
# ============================================================

# 引用编号：[1] [12]。限定 1-2 位数字，避免把「[137] 退出码」这类内容误当引用。
_CITATION_RE = re.compile(r"\[(\d{1,2})\]")

# 数值：独立出现的、至少两位的数字。
# 用否定环视排掉紧挨着字母/数字/下划线/点/连字符的情况 ——
# 否则 `web-01` 里的 01、`v1.2` 里的 2 都会被抓出来，
# 然后被判成"资料里找不到的数字"。这个坑在 Day 6 踩过一次了。
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.\-])(\d{2,}(?:\.\d+)?)")

# ★ 拒答特征分两档 —— 这个分层是第一轮评测踩出来的，值得说清楚。
#
# 【第一版：一个列表装所有"否定"句式，结果大面积误报】
#
# 第一版有 30 多个特征词，包括「参考资料中未」「未提及」「没有提供」。
# 跑完 40 条，失败列表里 7 条写着「明明有资料却拒答」——
# 但翻开原文一看，那些回答都是**完整的排查步骤**，只是末尾多了一段
# 「补充说明」或「参考资料中未涉及的部分」：
#
#     参考资料中未出现相互冲突的内容          ← 这是在确认「没有冲突」，
#                                              而且系统提示词本来就要求它
#                                              「有冲突要指出」，它是在合规
#     ### 参考资料中未涉及的部分
#     参考资料中没有提供通过 kubectl top pod 查询的说明…
#
# **这两句话和"我答不了这个问题"完全是两回事。**
# 前者是"我答完了，顺便说一句边界"，后者是"我不打算答"。
#
# 【修法：强句式 / 弱句式分开】
#
#   强句式 = 完整的拒答主张（「知识库中没有相关内容」「无法回答该问题」），
#            正常回答不会这么写 → 命中即判拒答
#   弱句式 = 正常回答的补充说明里也会出现 → 只有在**整段回答几乎没有
#            实质内容**时才算拒答（有实质内容 = 够长 + 有多个引用）
#
# ★ 通用教训和第 6 天数值溯源的三轮误报是同一个：
#   **校验规则的作用范围划错，就会把正常行为判成违规。**
#   而误报比漏报更消耗信任 —— 一个总在报假警报的判定器，
#   最后会被所有人忽略，连带真实问题一起被忽略。
_ABSTAIN_STRONG = (
    "知识库中没有相关内容", "知识库中没有相关", "知识库中无相关",
    "知识库里没有相关", "知识库中未收录", "知识库中没有收录",
    "没有相关的内容", "没有相关内容", "无相关内容", "没有相关文档",
    "无法回答该问题", "无法回答这个问题", "无法回答上述", "无法解答该问题",
    "不能回答该问题", "无法给出答案", "无法提供答案",
    "不在知识库范围内", "超出知识库范围", "超出我的知识库",
    "现有参考资料无法", "参考资料无法回答", "依据现有资料无法",
    "以上资料无法", "资料无法回答",
)

_ABSTAIN_WEAK = (
    "参考资料中未", "资料中没有", "资料里没有", "文档中没有", "文档里没有",
    "未提及", "没有提及", "未包含", "没有包含", "未提供", "没有提供",
    "未给出", "没有给出", "信息不足", "资料不足",
)

# 判定"有没有实质内容"的门槛。
#
# ★ 主判据是**引用数**，不是长度。
#
#   第一版用的是"长度 ≥ 300 字"，结果在回归测试里当场翻车：
#   一段 190 字、结构完整的短回答（结论 + 处理方向 + 末尾补充说明），
#   因为不够 300 字被判成"没有实质内容 → 拒答"。
#   长度是个脆弱的代理指标 —— 答得简洁不代表没答。
#
#   而「有没有认真回引编号」稳定得多：**真的拒答不会去引用资料。**
#   所以主判据改成引用数 ≥ 1，长度只作为一条很松的下限。
_SUBSTANTIVE_CITES = 1
_SUBSTANTIVE_CHARS = 150


def check_retrieval(expect_doc, hits) -> dict:
    """检索命中：期望文档是否出现在检索结果里。

    ★ 这个指标是**上游指标**。它没命中，下游答案基本不可能对 ——
      所以报告必须把"检索未命中"的用例单独拎出来看，
      否则会把"检索的锅"算到"生成的锅"头上。
    """
    if not expect_doc:
        # 知识库外的题不考检索命中 —— 它本来就该检索不到有用的东西
        return {"applicable": False, "hit": None, "got": []}
    got = sorted({h.get("source", "").replace(".md", "") for h in hits})
    return {"applicable": True, "hit": expect_doc in got, "got": got}


def check_citations(answer: str, n_hits: int) -> dict:
    """引用可信：答案里的 [n] 编号必须落在检索结果范围内。

    【为什么这条最值得测】
    "带引用溯源"是 RAG 相对裸模型最大的卖点 —— 用户看到 [2] 就能翻回去核对。
    但如果模型引用了不存在的编号（检索了 5 条却引用 [7]），
    这个卖点立刻变成负资产：**看着可验证，实际不可验证。**

    一条规则就能守住它，而且它绝不会误报。
    """
    cited = sorted({int(m) for m in _CITATION_RE.findall(answer or "")})
    invalid = [n for n in cited if not (1 <= n <= n_hits)]
    return {
        "cited": cited,
        "invalid": invalid,
        "has_citation": bool(cited),
        # 有引用、且没有越界编号 —— 才算通过。
        # 一个引用都不给也算不通过：那等于把"可核对"这个能力丢掉了。
        "ok": bool(cited) and not invalid,
    }


def check_numbers(answer: str, context: str) -> dict:
    """数值忠实：答案里出现的数字，应该在检索片段里找得到。

    【为什么盯数字】
    模型编造内容时，最常见的形态不是整句瞎编，而是**数字对不上**：
    把 60 秒写成 30 秒，把退出码 137 说成 139。
    句子读起来毫无破绽，但数字在资料里根本不存在。

    这是人肉审阅最容易被骗过去的地方 —— 因为读者默认数字有出处。

    【已知局限，必须主动说】
    1. 用子串包含判断，所以 "96" 会命中 "196"
    2. **派生值会误报**：资料写"负载 8.4 / 4 核"，答案写"每核 2.1"，
       那个 2.1 是算出来的，资料里没有
    3. 它只查数字，查不了"把 A 的结论安到 B 身上"这类错误

    所以这个指标在报告里按「无出处数字为 0」的比例呈现，
    并把未命中的数字列出来给人看 —— **不假装它是个精确指标。**
    """
    claimed = sorted({m for m in _NUM_RE.findall(answer or "")})
    ctx = context or ""
    unsourced = [n for n in claimed if n not in ctx]
    return {
        "claimed": claimed,
        "unsourced": unsourced,
        "clean": not unsourced,     # 无出处数字为 0
    }


def check_abstention(answer: str) -> dict:
    """拒答判定：回答是不是在说「知识库里没有相关内容」。

    【这个维度为什么最重要】

    检索**永远会返回 top_k 条结果** —— 哪怕全都不相关。
    所以「知识库里没有」这件事，模型没法靠「检索结果为空」得知，
    只能靠「读到的东西答不了这个问题」来判断。

    绝大多数人做的 RAG 从不测这一项。结果就是：
    用户问一个知识库外的问题，系统照样一本正经地答，
    而且答得还挺像那么回事 —— 因为它用的是模型自己脑子里的知识，
    **不受你的知识库约束，也无法审计。**

    这正是 RAG 相对裸模型的核心价值所在，所以必须能被量化。

    返回里的 `basis` 说明是靠哪一档判定的，方便人工复核：
        strong             命中强拒答句式 → 直接判拒答
        weak+substantive   命中弱句式，但回答有实质内容 → 判为**没有**拒答
        weak               命中弱句式且回答几乎没有实质内容 → 判拒答
        none               没命中任何特征
    """
    text = answer or ""

    strong = [m for m in _ABSTAIN_STRONG if m in text]
    if strong:
        return {"abstained": True, "matched": strong[:3], "basis": "strong"}

    weak = [m for m in _ABSTAIN_WEAK if m in text]
    if not weak:
        return {"abstained": False, "matched": [], "basis": "none"}

    # 弱句式：只有在回答几乎没有实质内容时才算拒答。
    # 有实质内容 → 那只是回答末尾的「补充说明 / 边界声明」，不是拒答。
    cites = len(_CITATION_RE.findall(text))
    substantive = (len(text) >= _SUBSTANTIVE_CHARS
                   and cites >= _SUBSTANTIVE_CITES)
    return {"abstained": not substantive, "matched": weak[:3],
            "basis": "weak+substantive" if substantive else "weak"}


# ============================================================
# 二、模型判定（LLM-as-Judge）
# ============================================================
JUDGE_SYSTEM = """你是 RAG 系统的质量评审员。给你一个提问、一批检索到的参考资料、以及系统的回答。
请从两个维度打分，都是 1-5 的整数。

【维度一 relevance（答案相关性）】
  5 = 直接、完整地回答了提问
  4 = 回答了主要问题，但缺少一些必要细节
  3 = 沾边，但答非所问，或只答了一部分
  2 = 基本没有回答问题
  1 = 完全无关
  额外规则：如果提问问的是「怎么排查 / 怎么办」，而回答只罗列了现象、没有给出方法，最高给 3 分。

【维度二 faithfulness（忠实度）】
  5 = 全部内容都能在参考资料里找到依据，没有一处是资料之外补充的
  4 = 绝大部分有依据，个别措辞是概括，不算编造
  3 = 有 1-2 处明显超出资料范围的内容
  2 = 有多处超出资料范围的内容
  1 = 大量编造，或与参考资料矛盾
  ★ 注意：参考资料里没有的内容，即使你知道正确答案，也算不忠实。
    这个维度衡量的是「有没有守住资料的边界」，不是「说得对不对」。
    这是本维度的核心，不要用常识去替它辩护。

只输出 JSON，不要任何解释文字：
{"relevance": <整数>, "faithfulness": <整数>, "reason": "<30 字以内的理由>"}"""

# ★ 无资料时用的另一套提示词：只判相关性。
#
# 【为什么必须分开 —— 这是第一版跑出来的真问题】
#
# 裸模型对照组没有参考资料。第一版图省事，照样把空 context 传进去,
# 结果 faithfulness 全部判成 1 分 —— 因为判定器的规则是
# "资料里没有的内容就算不忠实"，而没有资料时**所有内容都在资料之外**。
#
# 于是报告表格里写着「对照组忠实度 1.0」，而报告正文写着「不适用」——
# **数字和文字自相矛盾**。读的人只会更糊涂：到底哪个是真的？
#
# 更本质的问题是：**忠实度这个指标在无资料时不存在，不是"很低"。**
#   有资料 → 可以问"有没有超出资料范围"
#   无资料 → 这个问题本身无法成立，答什么都一样
#
# 所以正确做法不是"给个低分"，而是**不给分**，
# 让报告里真的显示"—"，并在正文说明原因。
JUDGE_SYSTEM_RELEVANCE_ONLY = """你是问答系统的质量评审员。给你一个提问和系统的回答。
本次没有提供参考资料，因此**只评估相关性，不评估忠实度**。
请只输出 relevance 一项，1-5 的整数：

  5 = 直接、完整地回答了提问
  4 = 回答了主要问题，但缺少一些必要的细节
  3 = 沾边，但答非所问，或只答了一部分
  2 = 基本没有回答问题
  1 = 完全无关
  额外规则：如果提问问的是「怎么排查 / 怎么办」，而回答只罗列了现象、没有给出方法，最高给 3 分。

只输出 JSON，不要任何解释文字：
{"relevance": <整数>, "reason": "<30 字以内的理由>"}"""


def judge_answer(question: str, context: str, answer: str) -> dict:
    """调模型给回答打分。失败时返回带 error 的结果，不抛异常。

    【为什么不抛异常】
    评测跑 40 条，中间一次网络抖动不该让整份报告作废。
    记下这一条失败、继续跑完，比中断有用得多 ——
    报告里会写明有几条没判成，读者自己知道该信到什么程度。

    【两种模式】
        context 非空 → 判相关性 + 忠实度
        context 为空 → **只判相关性**，faithfulness 返回 None
                       （None 在报告里显示为「—」，而不是 0 分或 1 分）
    """
    has_context = bool((context or "").strip())
    if has_context:
        user = (f"【提问】\n{question}\n\n"
                f"【检索到的参考资料】\n{context}\n\n"
                f"【系统的回答】\n{answer or '（空）'}\n\n"
                f"请按上面的标准打分。")
        system = JUDGE_SYSTEM
    else:
        user = (f"【提问】\n{question}\n\n"
                f"【系统的回答】\n{answer or '（空）'}\n\n"
                f"请只评估相关性。")
        system = JUDGE_SYSTEM_RELEVANCE_ONLY

    try:
        data = chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}], temperature=0)
    except (ModelError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}", "relevance": None,
                "faithfulness": None, "reason": "", "no_context": not has_context}

    def _score(key):
        v = data.get(key)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if 1 <= v <= 5 else None

    rel = _score("relevance")
    faith = _score("faithfulness") if has_context else None
    if rel is None or (has_context and faith is None):
        return {"error": f"打分取值不合法：{data}", "relevance": rel,
                "faithfulness": faith, "reason": "",
                "no_context": not has_context}
    return {"error": None, "relevance": rel, "faithfulness": faith,
            "reason": str(data.get("reason") or "")[:80],
            "no_context": not has_context}


# ============================================================
# 三、判定器自校验 —— 跑评测之前先验尺子
# ============================================================
# 三个构造样本，覆盖三种必须能被区分开的情况。
_VERIFY_CASES = [
    {
        "name": "好答案（有依据 + 回答了问题）",
        "expect": {"relevance": (4, 5), "faithfulness": (4, 5)},
        "question": "容器退出码 137 是什么意思？",
        "context": ("[1] 来源：docker-exit-code.md — Docker 容器退出码含义与排查\n"
                    "| `137` | **被 SIGKILL 杀掉（128+9）** | 被 OOM Killer 杀，"
                    "或 `docker stop` 超时后被强杀 |\n"
                    "## 137 的三种成因\n"
                    "1. 容器内存超过 limit，被 OOM Killer 杀掉\n"
                    "2. `docker stop` 超时（默认 10 秒）后被 SIGKILL\n"
                    "3. 容器内进程被宿主机的 OOM Killer 选中"),
        "answer": "退出码 137 表示进程被 SIGKILL 杀掉，也就是 128 + 9。"
                  "常见成因有三种：一是容器内存超过 limit 被 OOM Killer 杀掉；"
                  "二是 `docker stop` 超时后被强杀；"
                  "三是容器内进程被宿主机的 OOM Killer 选中。[1]",
    },
    {
        "name": "跑题答案（资料对，但没回答提问）",
        "expect": {"relevance": (1, 2)},
        "question": "容器退出码 137 是什么意思？",
        "context": ("[1] 来源：docker-exit-code.md — Docker 容器退出码含义与排查\n"
                    "| `137` | 被 SIGKILL 杀掉（128+9） | 被 OOM Killer 杀 |"),
        "answer": "Docker 是一个容器化平台，它把应用和依赖打包成镜像，"
                  "让应用可以在任何支持 Docker 的环境里一致地运行。"
                  "使用 Docker 可以提升部署效率、隔离运行环境、方便横向扩容。",
    },
    {
        "name": "编造答案（资料里没有，但听起来很对）",
        "expect": {"faithfulness": (1, 2)},
        "question": "容器退出码 137 是什么意思？",
        "context": ("[1] 来源：docker-exit-code.md — Docker 容器退出码含义与排查\n"
                    "| `137` | 被 SIGKILL 杀掉（128+9） | 被 OOM Killer 杀 |"),
        "answer": "退出码 137 表示容器被 SIGKILL 杀掉（128+9），通常是 OOM。"
                  "建议按下面的顺序处理：\n"
                  "1. 执行 `docker update --memory-swap -1 wp-app` 解除 swap 限制\n"
                  "2. 在 docker-compose.yml 里把 `deploy.resources.limits.memory` 设成 512M\n"
                  "3. 开启 `--oom-kill-disable` 保护关键容器\n"
                  "4. 用 `docker stats --no-stream` 观察 5 分钟，峰值不应超过 limit 的 80%\n"
                  "5. 如果仍然被杀，检查宿主机 `/proc/sys/vm/overcommit_memory` 是否为 1\n"
                  "以上步骤可以彻底解决 OOM 问题。[1]",
    },
]


def verify_judge(verbose: bool = True) -> dict:
    """★ 用构造样本验证判定器本身是准的。

    【为什么这一步不能省】

    评测报告的可信度上限 = 判定器的可信度。
    如果判定器分不出"好答案"和"编得一本正经的答案"，
    那么报告里那个"忠实度 4.6 分"就只是一个数字，不代表任何东西。

    所以跑正式评测之前，先用三个已知答案探一遍：
        · 好答案   → relevance 和 faithfulness 都该高
        · 跑题答案 → relevance 必须低（即使它说的内容是对的）
        · 编造答案 → faithfulness 必须低（即使它说的做法是合理的）

    ★ 第三个样本是最关键的。它给出的建议**在真实世界里基本都成立** ——
      问题不在于它错，而在于**资料里没有这些内容**。
      一个合格的 faithfulness 判定器必须能认出来。
      如果判定器被"听起来很专业"骗过去，这个维度就白设了。
    """
    results = []
    for case in _VERIFY_CASES:
        got = judge_answer(case["question"], case["context"], case["answer"])
        ok = got.get("error") is None
        detail = []
        for dim, (lo, hi) in case["expect"].items():
            v = got.get(dim)
            good = isinstance(v, int) and lo <= v <= hi
            ok = ok and good
            detail.append(f"{dim}={v}(期望 {lo}-{hi}){'✓' if good else '✗'}")
        results.append({"name": case["name"], "ok": ok,
                        "detail": "  ".join(detail),
                        "reason": got.get("reason") or got.get("error") or ""})
        if verbose:
            mark = "✅" if ok else "❌"
            print(f"  {mark} {case['name']}")
            print(f"     {results[-1]['detail']}")
            if results[-1]["reason"]:
                print(f"     判定理由：{results[-1]['reason']}")

    passed = sum(1 for r in results if r["ok"])
    return {"ok": passed == len(results), "passed": passed,
            "total": len(results), "cases": results}
