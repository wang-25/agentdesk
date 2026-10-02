# -*- coding: utf-8 -*-
"""追加式 JSONL 的公共读写：按大小轮转 + **跨文件回读**。

【为什么只有一部分日志能轮转 —— 这条是方案初稿写错、后来自己纠正的】

    · 事件流（`traces.jsonl` / `audit.jsonl`）：读侧只关心**尾部**（"最近 N 条"），
      轮转是安全的，只要读侧能回读上一份。
    · 状态日志（`approvals.jsonl` / `incidents.jsonl`）：启动时**整份折叠成状态**
      （`_load` → `_apply`）。把其中一部分改名挪走，等于**静默丢掉那些单据的状态** ——
      一张已批准的单子会变回"不存在"，重放保护直接失效。

    所以状态日志**不走这个模块的写路径**。它们的归档是运维动作
    （停服务、整体移走），不是程序自动行为 —— 程序能做的是把大小报出来、
    超阈值时告警，而不是替人决定"哪些历史可以不要了"。

【轮转满了怎么办：不删，移进 archive/】
"保留 N 份"这句话有个躲不开的问题：第 N+1 份怎么办？
删掉最旧的 —— 那是**程序在替人销毁审计数据**，这个项目不做这种事。
所以到容量上限后，最旧的一份被**移进 `archive/`**（带时间戳），
并写一条警告日志。磁盘仍然会涨，但**涨得看得见**，而且没有任何东西被悄悄删掉。
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("agentdesk.jsonl")

DEFAULT_MAX_BYTES = 10 * 1024 * 1024      # 10MB
DEFAULT_KEEP = 5                          # 轮转保留份数（不含当前文件）
# 读尾部时的字节上限：只读这么多，避免一次把几十 MB 读进内存。
# 与 tracer 原先的 2MB 一致（那是它的既有口径，不改）。
TAIL_BYTES = 2 * 1024 * 1024

_lock = threading.Lock()


# ============================================================
# 配置读取
# ============================================================
def max_bytes(env: dict = None) -> int:
    return _env_int(env, "LOG_MAX_BYTES", DEFAULT_MAX_BYTES, minimum=1024)


def keep_count(env: dict = None) -> int:
    return _env_int(env, "LOG_KEEP", DEFAULT_KEEP, minimum=1)


def _env_int(env: dict, name: str, default: int, minimum: int) -> int:
    raw = (os.environ if env is None else env).get(name)
    if raw in (None, ""):
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        log.warning("%s=%r 不是整数，回退默认值 %s", name, raw, default)
        return default


# ============================================================
# 路径
# ============================================================
def rotated_paths(path: Path, keep: int = None) -> list:
    """轮转文件的路径列表，**新的在前**（`.1` 比 `.2` 新）。"""
    path = Path(path)
    keep = keep if keep is not None else keep_count()
    return [path.with_name(f"{path.name}.{i}") for i in range(1, keep + 1)]


def size_of(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


# ============================================================
# 写入与轮转
# ============================================================
def maybe_rotate(path: Path, *, limit_bytes: int = None, keep: int = None) -> bool:
    """超过阈值就轮转。返回是否真的轮转了。

    轮转 = 改名（`.1` → `.2` → …），**不删**；到上限的一份移进 `archive/`。
    """
    path = Path(path)
    limit = limit_bytes if limit_bytes is not None else max_bytes()
    keep = keep if keep is not None else keep_count()

    if size_of(path) < limit:
        return False

    with _lock:
        # 双检：拿到锁之后可能已经被别的线程轮过了
        if size_of(path) < limit:
            return False

        files = rotated_paths(path, keep)          # [.1 … .keep]
        # ★ 顺序很关键：**先把最旧的一格腾走，再往后挪**。
        #   反过来的话（先挪再判断），keep=1 时刚生成的那份 `.1` 会立刻被判断成
        #   "最旧的"并被移进 archive/ —— 结果是"轮转了但读不到"。
        #   这个 bug 是被本模块的用例抓出来的：test_keep_is_respected[1]。
        if files and files[-1].exists():
            _archive_one(path, files[-1], keep)
        # 从最旧到次旧逐个往后挪，避免覆盖。
        # ★ `strict=False` 是**故意的**：两侧长度本来就差一
        #   （`files` 有 keep 个，`files[:-1]` 只有 keep-1 个），
        #   要的正是那 keep-1 组"后一格 → 前一格"的对应关系。
        #   写成 strict=True 会直接抛异常。
        for older, newer in zip(reversed(files), reversed(files[:-1]),
                                strict=False):
            if newer.exists():
                _replace(newer, older)
        if files and path.exists():
            _replace(path, files[0])

    log.info("日志已轮转：%s（阈值 %s 字节，保留 %s 份）", path.name, limit, keep)
    return True


def _archive_one(path: Path, oldest: Path, keep: int) -> None:
    """把超出保留份数的那一份移进 archive/（**不是删除**）。"""
    archive_dir = path.parent / "archive"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = archive_dir / f"{oldest.name}.{stamp}"
        oldest.replace(target)
        log.warning(
            "轮转份数已达上限（%s 份），最旧的一份被移进 archive/ 而不是删除：%s。"
            "磁盘仍会增长 —— 归档属于运维动作，程序不替人决定删哪些历史。",
            keep, target.name)
    except OSError as exc:                     # pragma: no cover
        log.warning("归档最旧轮转文件失败（%s）：%s", oldest.name, exc)


def _replace(src: Path, dst: Path) -> None:
    try:
        src.replace(dst)          # POSIX 与 Windows 都是原子改名（同盘）
    except OSError:
        # Windows 上目标存在时 replace 会失败：删掉目标再试一次
        try:
            dst.unlink()
            src.replace(dst)
        except OSError as exc:                 # pragma: no cover
            log.warning("轮转改名失败 %s → %s：%s", src.name, dst.name, exc)


def append_jsonl(path: Path, record: dict, *, limit_bytes: int = None,
                 keep: int = None, fsync: bool = False) -> None:
    """追加一条 JSON。写之前先看要不要轮转。

    `fsync` 默认关：trace 是**高频写入**，每行 fsync 会让它明显变慢，
    而丢几行 trace 的代价远小于丢掉一条审批状态
    （审批单那条路径自己在 `approvals._append` 里 fsync，这是两处**故意不同**的取舍）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    maybe_rotate(path, limit_bytes=limit_bytes, keep=keep)
    line = json.dumps(record, ensure_ascii=False)
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        if fsync:
            f.flush()
            os.fsync(f.fileno())


# ============================================================
# 读取（跨文件回读）
# ============================================================
def read_tail(path: Path, limit: int, *, keep: int = None,
              tail_bytes: int = TAIL_BYTES) -> tuple:
    """从**尾部**读最近 `limit` 条；当前文件不够就回读 `.1`、`.2`…

    返回 `(records, meta)`，records 按"旧 → 新"排列。

    ★ `meta` 是这次改动的一个重点：原先是"读不出来就 `return []`"，
      于是看板静默显示"最近没有数据"—— 而"没有数据"和"读失败"是两件事。
      meta 里如实带上：
        scanned    扫过多少行
        bad_lines  解析失败的行数（跳过了，但要让人知道）
        truncated  是否因为字节上限而没能凑够 limit 条
        files      实际读了哪些文件
    """
    path = Path(path)
    keep = keep if keep is not None else keep_count()
    meta = {"scanned": 0, "bad_lines": 0, "truncated": False, "files": []}

    if limit <= 0:
        return [], meta

    collected = []                      # 每条是 (seq, record)，seq 越大越新
    # 序号从大到小：当前文件最新，然后 .1、.2 …
    sources = [path] + rotated_paths(path, keep)
    seq = 0
    for src in sources:
        if len(collected) >= limit:
            break
        chunk = _read_file_tail(src, limit - len(collected), tail_bytes)
        if chunk is None:
            continue
        lines, hit_byte_cap = chunk
        meta["files"].append(src.name)
        if hit_byte_cap:
            meta["truncated"] = True
        for line in reversed(lines):     # 从新到旧编号
            seq += 1
            collected.append((seq, line))

    out = []
    for _, line in reversed(collected[:limit]):     # 还原成旧→新
        meta["scanned"] += 1
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            meta["bad_lines"] += 1
            continue
    if len(out) < limit:
        # 说明所有文件加起来都不够（可能刚轮转过、也可能确实没那么多数据）
        meta["truncated"] = meta["truncated"] or bool(meta["files"])
    return out, meta


def _read_file_tail(path: Path, want: int, tail_bytes: int):
    """读单个文件的尾部若干行。返回 (lines 旧→新, 是否撞到字节上限)；文件不存在返回 None。"""
    if not path.exists() or want <= 0:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - tail_bytes)
            f.seek(start)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError as exc:
        log.warning("读日志失败 %s：%s", path.name, exc)
        return None

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if start > 0:
        # 从中间截断的第一行可能只有半条 JSON：丢掉它（不然必然解析失败）
        lines = lines[1:]
        return lines[-want:], True
    return lines[-want:], len(lines) > want
