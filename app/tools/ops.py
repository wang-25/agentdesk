# -*- coding: utf-8 -*-
"""
运维工具集 —— Agent 的「手」
============================================================
前三天的 Agent 只会「说」。RAG 让它「知道」，但输出的仍然只是文字。
这一层给它手：能去查真实的磁盘、日志、服务状态、容器。

【两个可切换的后端】
    mock  —— 仿真数据。Windows 上也能跑，演示和评测用（默认）
    local —— 真的执行只读命令。Linux 上可用（Day 10 会换成 Docker 沙箱）

为什么必须做双后端：
    工具层如果一开始就依赖"真的连上一台机器"，那这个项目在别人电脑上
    就完全跑不起来 —— 面试官 clone 下来什么都看不到。
    而 mock 数据让整条链路可复现：同样的输入，永远得到同样的输出。

【安全三原则】—— 这是面试官最会追问的地方
    1. 全部只读：没有任何写操作。不重启、不删除、不修改配置。
       诊断和处置必须分开 —— 处置是另一件事，需要人工确认。
    2. 参数白名单校验：绝不把用户/模型的字符串拼进 shell 命令。
       每个参数都过正则，不匹配直接拒绝。这是防命令注入的第一道墙。
    3. 风险分级：每个工具标 risk（low/medium/high）。
       调用方（Agent 循环）据此决定是否需要人工确认（Day 10 接 HITL）。

【一句要记住的话】
    模型永远不执行任何东西。它只是"提出请求"。
    真正决定执不执行、要不要拦下来的，是这个文件里的代码。
"""

import json
import os
import re
import subprocess
import time

# ============================================================
# 零、后端选择
# ============================================================
# 默认 mock：保证在任何机器上都能跑通。
# 要跑真机就把环境变量 OPS_BACKEND 设成 local。
BACKEND = (os.getenv("OPS_BACKEND") or "mock").strip().lower()


# ============================================================
# 一、参数校验（防命令注入的第一道墙）
# ============================================================
# 只允许小写字母、数字、点、下划线、连字符。长度限死。
# 这样 "nginx; rm -rf /" 这种输入根本进不来。
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_.@-]{1,32}$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.-]{1,64}$")

# 已知主机清单。只允许查这几台 —— 模型不能自己编一个主机名去连。
KNOWN_HOSTS = ["web-01", "db-01", "cache-01"]


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
            {"filesystem": "/dev/vda2", "mount": "/var", "size": "60G",
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
# 三、local 后端：真的执行只读命令
# ============================================================
# 只允许这几个固定的命令模板。注意没有一处是把参数拼进 shell 字符串 ——
# subprocess 传的是"列表"，等于告诉内核"这是参数列表，不是一段命令"，
# shell 根本没机会解释其中的分号、管道、反引号。
#
# shell=False 是关键：如果写成 shell=True，白名单就形同虚设。
def _run(cmd: list, timeout: int = 10) -> str:
    proc = subprocess.run(
        cmd, shell=False, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout.strip()


def _local_disk(host: str) -> list:
    out = _run(["df", "-h"])
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6:
            rows.append({
                "filesystem": parts[0], "size": parts[1], "used": parts[2],
                "avail": parts[3],
                "use_percent": int(parts[4].rstrip("%") or 0),
                "mount": parts[5],
            })
    return rows


def _local_service(host: str, service: str) -> dict:
    active = _run(["systemctl", "is-active", service])
    detail = _run(["systemctl", "status", "--no-pager", "-l", service])
    return {"active": active, "detail": detail[:2000]}


def _local_containers(host: str) -> list:
    out = _run(["docker", "ps", "-a",
                "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"])
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"name": parts[0], "image": parts[1], "status": parts[2]})
    return rows


def _local_logs(host: str, service: str, lines: int) -> list:
    # 日志路径不交给调用方指定 —— 只在这张表里查。
    # 让外部传路径，就等于给了它读任意文件的权限。
    candidates = [
        f"/var/log/{service}/error.log",
        f"/var/log/{service}.log",
        f"/var/log/{service}/current",
    ]
    for path in candidates:
        if os.path.exists(path):
            out = _run(["tail", "-n", str(lines), path])
            return out.splitlines()
    raise ToolError(f"找不到 {service} 的日志文件（已尝试 {candidates}）")


# ============================================================
# 四、六个工具
# ============================================================
def check_disk(host: str = "web-01") -> dict:
    """查看磁盘使用率。磁盘满是运维故障里出现频率最高的一类。"""
    host = _check_host(host)
    if BACKEND == "local":
        partitions = _local_disk(host)
    else:
        partitions = _mock(host)["disk"]

    # 顺手算出最高使用率 —— 让模型不用自己在脑子里比大小。
    # 这是"工具该做的事"：把原始数据加工成结论，减少模型的推理负担和出错机会。
    worst = max(partitions, key=lambda p: p["use_percent"]) if partitions else None
    return {
        "host": host,
        "backend": BACKEND,
        "partitions": partitions,
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
    if BACKEND == "local":
        cores = int(_run(["nproc"]) or 1)
        uptime_out = _run(["uptime"])
        free_out = _run(["free", "-m"])
        load = {"cpu_cores": cores, "raw_uptime": uptime_out, "raw_free": free_out}
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
    if BACKEND == "local":
        return {"host": host, "service": service, "backend": BACKEND,
                **_local_service(host, service)}

    svc = _mock(host)["services"].get(service)
    if svc is None:
        return {"host": host, "service": service, "backend": BACKEND,
                "active": "not-found",
                "hint": f"该主机上没有名为 {service} 的服务"}
    return {"host": host, "service": service, "backend": BACKEND, **svc}


def list_containers(host: str = "web-01") -> dict:
    """列出容器及其状态。能看出反复重启（Restarting + 重启次数高）。"""
    host = _check_host(host)
    if BACKEND == "local":
        items = _local_containers(host)
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

    if BACKEND == "local":
        entries = _local_logs(host, service, lines)
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
            "count": len(entries), "lines": entries,
            "matched_patterns": found,
            "hint": ("日志中命中已知错误模式：" +
                     "、".join(f["meaning"] for f in found)) if found else None}


def search_knowledge(query: str, top_k: int = 3) -> dict:
    """检索运维知识库（Day 3 做的 RAG）。

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

    【为什么这里必须 try 住所有异常】
    工具是模型"点名"调用的，参数由模型生成 —— 也就是说，
    **输入是不可信的**。参数写错是常态，不是异常情况。
    如果把异常抛出去，整个 Agent 循环就会崩；正确做法是把错误
    当成一条「观察结果」返回给模型，让它看到自己写错了然后改。
    这是工具层和普通函数最大的区别。
    """
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
