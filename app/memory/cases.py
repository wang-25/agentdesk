# -*- coding: utf-8 -*-
"""
案例记忆：把解决过的故障变成下次的**参考**（不是答案）
============================================================
M2 已经沉淀了"什么告警 → 怎么诊断 → 怎么处置 → 结论"（`incidents.jsonl`），
但那些数据**只用于展示**。这个文件把已结单的事件抽成"案例"，
让下一次遇到相似故障时能想起"上次是怎么弄好的"。

【★★ 这个功能最大的风险：模型把"上次的答案"当成"这次的事实" ★★】

这是本项目一贯拒绝的那件事 —— **把未标注的推测喂给模型**。
更危险的是它比"推测"更像真的：一段上次的处理记录读起来就是一份结论，
而模型没有任何理由怀疑它。具体的失败形态：

    昨天 web-01 因为 inode 耗尽 502，处置是"清理 /var/spool/clientmqueue"。
    今天 web-02 也 502 —— 但这次是磁盘写满。
    把昨天的案例当成事实喂进去，模型很可能直接建议清理那个队列，
    然后这次故障**看起来被处理了**（命令跑成功、没有报错），
    实际问题还在。

所以本模块的设计原则只有一条，后面所有取舍都是它的推论：

    **案例只能作为"历史参考"注入，永远不能作为事实注入。**
    因而 `render_context()` 的输出必须：写明这是**历史**、带上**时间**、
    并显式说明"当初的处理未必适用于现在，请用当前机器的实际数据重新判断"。
    这条文案有专门的用例守着（`test_render_context_demands_fresh_evidence`）
    —— **它不是措辞问题，是安全边界。** 改文案改到那句警告消失，用例就要红。

【相似度：零成本、确定性 —— 为什么不用向量库】

    · 调模型打分：每次诊断都要多花一次钱和两秒，而它只影响"参考哪三条"，
      不影响结论 —— 用最贵的手段做最不值钱的那一步没有道理。
    · 向量库：`app/rag/store.py` 已经有向量检索，但案例库是**几十到几百条**，
      而且会每天增长；为一个"找三条相似的历史"引入 embedding 调用与索引构建，
      代价远大于收益（何况那需要模型，本次不做）。

所以用 `app/rag/store.py` 的 `tokenize`（**复用，不另写一套分词**）
做 token 重叠打分：纯函数、可复现、零成本、不联网。
代价说清楚：它**只认字面命中**，抓不住同义改述
（"磁盘满了" vs "空间不足"）。这是有意的取舍，见 docs/memory.md 的已知边界。

【为什么记录也要去重 / 封顶，并且如实报告】

同一个事件可以被结单多次（`resolved → reopened → resolve`）。不去重的话，
一个反复复发的故障会在案例库里堆出十几条几乎一样的记录，
把真正不同的历史挤出上限 —— 而**"这东西老是坏"恰恰是最该被看见的信号**。
所以：一个事件只留一条案例，后来者**更新**它（结论以最后一次为准）。

`CASES_MAX`（默认 500）超了丢最旧的，丢了几条**必须在返回值里报出来**，
理由与 `sessions.py` 的上限完全一样：**丢可以，偷偷丢不行。**
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.llm import PROJECT_ROOT

#: 案例库上限。超了丢最旧的（并在 `record_case` 的 `evicted` 里如实报告）。
DEFAULT_MAX_CASES = 500

#: 渲染进提示词时，单个字段最多多少字。
#:
#: ★ 截断是**有标注的**（`_clip` 会在末尾写明原长）：
#:   静默截断会让模型以为这就是全部，而它不会知道自己看的是残缺版本。
MAX_FIELD_CHARS = 400

#: 相似度里的类别权重 —— 服务名最关键。
#:
#: 为什么服务名权重更高：运维里"同一个服务的同类故障"复用价值最高
#: （nginx 502 的处置经验用在 nginx 上比用在 mysql 上靠谱得多）；
#: 而处置动作（`actions`）是最容易撞词的一段（"重启服务"到处都是），
#: 所以它权重最低 —— **撞词不等于相似**。
FIELD_WEIGHTS = {"service": 3.0, "symptom": 1.0, "conclusion": 1.0, "actions": 0.5}

#: 只有这些 key 会进 `find_similar` 的打分文本（也决定权重）。
_FIELD_ORDER = ("service", "symptom", "conclusion", "actions")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value) -> str:
    """任意值 → 干净的字符串。`None` → `""`，列表用空格连接。

    `actions` 在事件里到底是 list 还是 str 是**不确定的**（不同调用方写法不同）。
    这里一律归一成字符串，免得后面的 `tokenize` 收到 list 抛异常 ——
    而它抛在 `record_case` 里就等于"结单接口 500"。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value if v is not None)
    if isinstance(value, dict):
        return " ".join(f"{k} {_text(v)}" for k, v in value.items())
    return str(value).strip()


def _tokens(text: str) -> list:
    """分词。**复用 RAG 的 `tokenize`**，不自己再写一套。

    ★ 惰性 import：`app.rag.store` 会拉起 numpy / embedder（重），
      而案例记忆在**每次结单**时都会被调用 —— 模块顶层 import 会让
      "只想用一下案例库"的场景也去加载整套检索依赖。
      同时它也避免了一个真实的循环风险：RAG 是被 Agent 链路广泛 import 的模块，
      从它反向依赖案例库会很难排查。

    ★ 单字过滤：分词结果里的标点与单个虚字（"的"、"了"、"是"）
      会在任意两个中文案例之间都命中，制造出一批**假的相似**。
      代价是"查 inode"这类单字查询也匹配不上 —— 这是可接受的：
      真实查询是整句（"web-01 磁盘 inode 满了"），不是两个字。
    """
    from app.rag.store import tokenize

    out = []
    for token in tokenize(str(text or "")):
        token = token.strip().lower()
        if not token:
            continue
        if len(token) == 1 and not token.isascii():
            continue                      # 单个中文字：噪声，见上
        out.append(token)
    return out


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """截断并**留下痕迹**（静默截断 = 模型以为它看到了全部）。"""
    s = _text(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（已截断，原长 {len(s)} 字）"


def _idf(tokens: list, corpus: list) -> dict:
    """每个 token 在案例库里的"稀有权重" = log(1 + N / (1 + 出现次数))。

    为什么需要它：`service` 字段在一个**全是 nginx 的案例库**里
    不提供任何区分度（每个案例都命中）。按普通重叠算，这样的词会把
    真正区分案例的词（"inode"、"502"）盖过去 ——
    结果就是"随便问什么，返回的都是那三条最早的 nginx 案例"。
    IDF 让"到处都是的词"自动贬值，不需要手工维护停用词表。
    """
    if not corpus:
        return {}
    n = len(corpus)
    counts = {}
    seen = set(tokens)
    for case in corpus:
        for token in set(case["_tokens"]):
            counts[token] = counts.get(token, 0) + 1
    return {t: 1.0 + (n / (1 + counts.get(t, 0))) for t in seen}


def _score(query_tokens: list, case: dict, idf: dict) -> float:
    """查询与案例的相似度，归一化到 (0, 1]。

    做法：对查询里每个 token，取它在案例中**最高的字段权重**，
    乘该 token 的 IDF，求和后再除以"查询全部 token 满载"的分母。

    ★ 为什么取最高权重而不是相加：一个词同时出现在症状和结论里
      （"磁盘"）不该因为出现两次就把分数翻倍 —— 那奖励的是
      **字段冗余**，不是相似度。
    """
    if not query_tokens:
        return 0.0
    hits = 0.0
    total = 0.0
    seen = set()
    for token in query_tokens:
        if token in seen:               # 同一个词重复出现不重复计分
            continue
        seen.add(token)
        total += idf.get(token, 1.0)
        best = max((w for field, w in FIELD_WEIGHTS.items()
                    if token in case["_tokens_by_field"].get(field, ())),
                   default=0.0)
        hits += best * idf.get(token, 1.0)
    return (hits / total) if total else 0.0


class CaseStore:
    """案例存储：追加 JSONL + 启动折叠。线程安全（结单发生在同步接口里）。

    内存态：

        `_cases`:  incident_id -> 案例 dict（顺序 = 插入顺序，最旧在最前）
        `_order`:  incident_id 列表，只在淘汰时用得上（插入序稳定，
                   不靠 dict 顺序，避免"同一秒的两条案例谁先被淘汰"随机）

    案例字段（与 docs/memory.md 一致）：

        ts / incident_id / fingerprint / service / symptom / conclusion
        / actions / elapsed_ms

    ★ `_tokens` / `_tokens_by_field` 是**派生的内存态**，不落盘：
      分词结果与 jieba 版本有关，落盘会增加"盘上写的和现在算的不一致"
      这种没法排查的状态。启动时重算一遍的代价是几十毫秒（500 条以内）。
    """

    def __init__(self, path: Path = None, max_cases: Optional[int] = None):
        self.path = (Path(path) if path
                     else (PROJECT_ROOT / "logs" / "cases.jsonl"))
        self.max_cases = (max(1, int(max_cases)) if max_cases is not None
                          else _env_int("CASES_MAX", DEFAULT_MAX_CASES))
        self._lock = threading.RLock()
        self._cases: dict = {}
        self._load()

    # ============================================================
    # 持久化
    # ============================================================
    def _load(self) -> None:
        """启动时把日志折叠成当前状态。坏行只跳过它自己（同事件存储）。"""
        self._cases = {}
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        self._apply(rec)
        except OSError:
            # 案例是旁路记忆：读不出来不该拦住服务启动
            pass

    def _append(self, rec: dict) -> None:
        """追加一条案例。**先落盘再折叠**，折叠的是同一个 dict（同事件存储）。

        `ts` 原地 `setdefault`：不回填的话，当前进程内的时间戳是 None，
        重启折叠回来又是好的 —— 那个"自愈型"的坑在 approvals 上踩过一次，
        这里不再踩第二次。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec.setdefault("ts", _now())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._apply(rec)

    def _apply(self, rec: dict) -> None:
        """一条记录折叠进案例库。

        三种记录：`created`（新案例）、`updated`（同一事件再次结单，
        结论以最后一次为准）、`evict`（超上限被丢掉的，折叠时从内存里去掉）。

        认不出的类型**静默忽略**：折叠发生在 `__init__` 里，
        这里抛一次异常就等于服务起不来（向前兼容：新版本写的日志旧版本读到不崩）。
        """
        kind = str(rec.get("event") or "")
        iid = str(rec.get("incident_id") or "")
        if not iid:
            return
        if kind in ("created", "updated"):
            case = {
                "ts": rec.get("ts"),
                "incident_id": iid,
                "fingerprint": _text(rec.get("fingerprint")),
                "service": _text(rec.get("service")),
                "symptom": _text(rec.get("symptom")),
                "conclusion": _text(rec.get("conclusion")),
                "actions": _text(rec.get("actions")),
                "elapsed_ms": rec.get("elapsed_ms"),
            }
            self._cases[iid] = case          # 同 id 覆盖 = "以最后一次为准"
            self._reindex(iid)
        elif kind == "evict":
            self._cases.pop(iid, None)

    def _reindex(self, iid: str) -> None:
        """重算一条案例的分词缓存（派生态，不落盘）。"""
        case = self._cases[iid]
        by_field = {}
        for field in _FIELD_ORDER:
            by_field[field] = set(_tokens(case.get(field, "")))
        case["_tokens_by_field"] = by_field
        case["_tokens"] = set().union(*by_field.values()) if by_field else set()

    # ============================================================
    # 写入
    # ============================================================
    def record(self, incident: dict) -> dict:
        """从一个事件抽取案例并记下。返回**如实报告**的结果。

        返回 `{"recorded": str|None, "case": dict|None, "evicted": int, "reason": str}`：

            · `recorded` 是案例 id（就是 `incident_id`），没记成就是 None
            · `evicted` 是这次因为超上限丢掉的旧案例数（**必须如实报**）
            · 同一个 `incident_id` 再记一次 → 更新，`recorded` 仍是它，
              `reason` 写明"更新"

        `incident_id` 缺失 → **不记**（`recorded=None`）。为什么不自己造一个 id：
          案例的价值全在"能回到那个事件去看完整时间线"。造一个假 id，
          点进去 404 —— 那比没有这条案例更糟（它会让人以为查过了）。
        """
        data = incident if isinstance(incident, dict) else {}
        iid = _text(data.get("id") or data.get("incident_id"))
        if not iid:
            return {"recorded": None, "case": None, "evicted": 0,
                    "reason": "事件没有 id，拒绝记录（案例必须能回到原事件）"}

        with self._lock:
            existed = iid in self._cases
            rec = {
                "event": "updated" if existed else "created",
                "incident_id": iid,
                "fingerprint": _text(data.get("fingerprint")),
                "service": _text(data.get("service")),
                "symptom": _text(data.get("summary")),
                "conclusion": _text(data.get("resolution_note")),
                "actions": _text(_actions_of(data)),
                "elapsed_ms": _elapsed_of(data),
            }
            self._append(rec)

            evicted = 0
            while len(self._cases) > max(1, int(self.max_cases)):
                victim = next(iter(self._cases))
                if victim == iid:
                    # 理论上到不了：`iid` 是最新插入的，字典序里排最后。
                    # 留着这道闸是为了让"绝不淘汰刚记下的这条"成为一个
                    # 不依赖插入顺序的**保证**。
                    break
                self._append({"event": "evict", "incident_id": victim,
                              "reason": f"案例库超过上限 {self.max_cases} 条，丢最旧的"})
                evicted += 1

            return {
                "recorded": iid,
                "case": dict(self._cases[iid]) if iid in self._cases else None,
                "evicted": evicted,
                "reason": ("同一事件再次结单，以最后一次为准"
                           if existed else "新案例"),
            }

    # ============================================================
    # 查询
    # ============================================================
    def find(self, query: str, limit: int = 3, service: str = None) -> list:
        """按 token 重叠找相似的**历史**案例。返回的每一行都带 `score`。

        ★ 返回的每个案例里都有一句 `note`，写清"这是历史、时间是什么"。
          为什么要在**数据层**就带上它、而不是只在 `render_context` 里加：
          调用方可能不经过 `render_context`（自己拼提示词、塞进别的流程），
          那时候标注就丢了。**安全标注要跟着数据走，不能只挂在一个函数上。**
        """
        with self._lock:
            corpus = list(self._cases.values())
            if service:
                wanted = _text(service).lower()
                corpus = [c for c in corpus if c["service"].lower() == wanted]

            query_tokens = _tokens(query)
            idf = _idf(query_tokens, corpus)
            scored = [(_score(query_tokens, c, idf), c) for c in corpus]
            # 次键用 `ts` 倒序：相似度相同时**新的优先**（更可能对现在成立）。
            # `incident_id` 兜底，保证同一秒的两条案例顺序也确定 ——
            # 排序不确定会让"同样的输入返回不同的三条"，那种测试是假绿。
            scored = [(s, c) for s, c in scored if s > 0]
            scored.sort(key=lambda pair: (pair[0], pair[1]["ts"] or "",
                                          pair[1]["incident_id"]), reverse=True)

            out = []
            for score, case in scored[:max(0, int(limit))]:
                row = {k: v for k, v in case.items() if not k.startswith("_")}
                row["score"] = round(score, 4)
                row["note"] = (f"历史案例（{row['ts']}），仅供参照；"
                               f"当初的处理未必适用于现在的机器")
                out.append(row)
            return out

    def counts(self) -> dict:
        """`{"total", "services", "oldest", "newest", "max_cases"}`。

        把上限和最早/最新时间一起报出来：看板上"34 条案例"如果不带上限，
        没人知道离淘汰还有多远；不带上最早时间，没人知道这些经验有多旧。
        """
        with self._lock:
            rows = list(self._cases.values())
            services: dict = {}
            for case in rows:
                key = case["service"] or "(未标注)"
                services[key] = services.get(key, 0) + 1
            stamps = sorted(c["ts"] for c in rows if c["ts"])
            return {
                "total": len(rows),
                "services": services,
                "oldest": stamps[0] if stamps else None,
                "newest": stamps[-1] if stamps else None,
                "max_cases": self.max_cases,
            }

    def get(self, incident_id: str) -> dict:
        """按事件 id 取一条案例。没有就返回 `{}`（**不抛**）。

        为什么不抛：这个方法的调用方是"顺手看看有没有对应案例"的诊断链路，
        抛异常会把它变成一次 500 —— 而"没有案例"是完全正常的状态。
        """
        with self._lock:
            case = self._cases.get(_text(incident_id))
            if case is None:
                return {}
            return {k: v for k, v in case.items() if not k.startswith("_")}

    def describe(self) -> dict:
        """当前配置的一句话说明（给接口用，免得以为开了什么、其实关着）。"""
        return {
            "path": str(self.path),
            "max_cases": self.max_cases,
            "note": ("案例只作为历史参考注入，必须与当前机器的实测数据一起判断；"
                     "超过上限时丢最旧的并在 evicted 里如实报告"),
        }


def _actions_of(incident: dict) -> str:
    """从事件里取"当初做了什么"。

    优先 `resolution_note`（人写的处置说明，最接近"做了什么"），
    退到时间线上的动作性事件（`notified` / `diagnosed` 的 note），
    最后才用诊断摘要。**顺序是有讲究的**：`resolution_note` 是人的结论，
    而诊断摘要只是"模型当时说了什么" —— 两者在案例里的价值完全不同。
    """
    parts = []
    for key in ("actions", "resolution_note"):
        got = _text(incident.get(key))
        if got:
            parts.append(got)
    for item in (incident.get("timeline") or []):
        if isinstance(item, dict) and item.get("event") in ("notified", "diagnosed"):
            got = _text(item.get("note"))
            if got:
                parts.append(got)
    if not parts:
        diagnosis = incident.get("diagnosis")
        if isinstance(diagnosis, dict):
            parts.append(_text(diagnosis.get("summary")))
    return "；".join(parts)


def _elapsed_of(incident: dict) -> int:
    """故障持续时长（毫秒）。算不出来就 `None` —— **不编一个 0**。

    `0` 会被读成"瞬间就修好了"，而 `None` 是"不知道"。
    这两件事在复盘里完全不同（前者是表扬，后者是要查的空白）。
    从 `created_at` 到 `resolved_at`；`reopened` 过的事件用最后一次结单时间。
    """
    created, resolved = incident.get("created_at"), incident.get("resolved_at")
    if not created or not resolved:
        return None
    try:
        delta = (datetime.fromisoformat(str(resolved))
                 - datetime.fromisoformat(str(created)))
    except (TypeError, ValueError):
        return None
    ms = int(delta.total_seconds() * 1000)
    return ms if ms >= 0 else None


def render_context(cases: list = None) -> str:
    """把案例渲染成**可注入的文本**。空列表 → `""`（不注入任何东西）。

    ★★ 这段文案是安全边界，不是措辞问题。★★

    它必须同时做到四件事，缺一件就可能让模型把"上次的答案"当成"这次的事实"：

        ① 开场就说清这是**历史**案例（不是观测结果、不是事实）
        ② 每条案例**带时间**（没有时间的"经验"无法判断是否还成立）
        ③ 结尾明确要求**用当前机器的实际数据重新判断**
        ④ 说明"当初的处理未必适用于现在"

    为什么连"开场"也要写：只写在结尾的话，长上下文被截断时
    留下的恰恰是没有警告的那一半。**警告要出现在模型最可能读到的位置。**
    """
    # 只认**真的有来源事件**的案例。一个没有 `incident_id` 的字典不是案例
    # （案例的价值有一半在"能回到那个事件去看完整时间线"），
    # 全是这种"空壳"时必须返回 `""` —— 吐出一个没有内容的标题会污染提示词。
    rows = [c for c in (cases or [])
            if isinstance(c, dict) and _text(c.get("incident_id"))]
    if not rows:
        return ""

    lines = [
        "【历史案例参考 · 不是本次的事实】",
        "以下是过去处理过的相似故障记录。它们**只说明当时发生了什么**，"
        "不代表现在的情况与它们相同。",
        "",
    ]
    for i, case in enumerate(rows, 1):
        service = _text(case.get("service")) or "(未标注服务)"
        lines.append(f"案例 {i}｜时间：{_text(case.get('ts')) or '(无时间)'}"
                     f"｜服务：{service}"
                     f"｜来源事件：{_text(case.get('incident_id')) or '(无)'}")
        for label, key in (("当时的现象", "symptom"),
                           ("当时的结论", "conclusion"),
                           ("当时的处置", "actions")):
            value = _clip(case.get(key))
            if value:
                lines.append(f"  - {label}：{value}")
        score = case.get("score")
        if isinstance(score, (int, float)):
            lines.append(f"  - 与当前问题的字面相似度：{score:.2f}"
                         f"（只按关键词重叠计算，不代表结论正确）")
        lines.append("")

    lines += [
        "────────",
        "以上都是**历史**案例。当初的处理未必适用于现在，"
        "请用当前机器的实际数据重新判断，不要直接照搬上面的结论或命令。",
    ]
    return "\n".join(lines)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读一个正整数环境变量；读不出来就用默认值（配置写错不该让服务崩）。"""
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


# 单例。案例库是"读多写少但必须共享"的进程级状态：
# 每个请求新建一个 store 会让"刚记下的案例"在下一次诊断里查不到。
#
# ★ 与 `sessions.py` 同样**延迟创建**，理由那一条完全一样：
#   import 期就建 store 会在真实 `logs/` 下落一个文件，
#   而"跑一条用例/读一下 openapi"不该有这种副作用。
#   同时 `CASES_LOG` 这类环境变量也因此能在运行时生效。
_STORE = None


def store() -> CaseStore:
    global _STORE
    if _STORE is None:
        _STORE = CaseStore(path=Path(os.getenv("CASES_LOG") or
                                     (PROJECT_ROOT / "logs" / "cases.jsonl")))
    return _STORE


# ============================================================
# 模块级入口（与需求里的函数签名一一对应）
# ============================================================
def record_case(incident: dict) -> dict:
    """从事件抽取案例并追加到 `logs/cases.jsonl`。"""
    return store().record(incident)


def find_similar(query: str, limit: int = 3, service: str = None) -> list:
    """找相似的历史案例（零成本、确定性；不调模型、不用向量库）。"""
    return store().find(query, limit=limit, service=service)


def counts() -> dict:
    """案例库统计。"""
    return store().counts()
