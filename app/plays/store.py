# -*- coding: utf-8 -*-
"""剧本库：从目录加载全部剧本，**坏剧本会让加载失败**。

【为什么坏剧本要"整个库都加载不出来"，而不是"跳过那一份"】
挑一个最坏的情形：磁盘满的剧本里有一条打错字的回滚命令。
如果选择"跳过这份剧本"，那么在真出事的时候，值班的人会看到
**剧本列表里少了磁盘满那一条** —— 而他不会有时间去想为什么少了。
相比之下，"服务启动时直接报错、说清第几行错了"是**当时就能修好**的故障。

一个例外要讲清：**空目录不算错误**（还没写剧本是合法状态），
但目录里有坏剧本就是错误。
"""

import logging
from pathlib import Path

from app.plays.model import Play, PlayError, load_file, render, validate  # noqa: F401

log = logging.getLogger("agentdesk.plays")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAYS_DIR = PROJECT_ROOT / "plays"

_cache = None


def dir_path() -> Path:
    """剧本目录。`PLAYS_DIR` 可覆盖（测试与多环境部署用）。"""
    import os
    raw = (os.getenv("PLAYS_DIR") or "").strip()
    return Path(raw) if raw else PLAYS_DIR


def load_all(directory: Path = None, *, force: bool = False) -> dict:
    """加载目录下所有 `*.json` 剧本，返回 {name: Play}。

    结果缓存（剧本是启动期资产，读一次即可）；`force=True` 或 `reset()` 强制重读。
    """
    global _cache
    if _cache is not None and not force and directory is None:
        return _cache

    root = Path(directory) if directory is not None else dir_path()
    plays = {}
    errors = []

    if not root.is_dir():
        log.info("剧本目录不存在（%s），按『没有剧本』处理", root)
        result = {}
        if directory is None:
            _cache = result
        return result

    for path in sorted(root.glob("*.json")):
        try:
            play = load_file(path)
        except PlayError as e:
            errors.append(f"{path.name}: {e}")
            continue
        if play.name in plays:
            errors.append(f"{path.name}: 剧本名 {play.name!r} 与 "
                          f"{plays[play.name].source} 重复")
            continue
        plays[play.name] = play

    if errors:
        # 见模块 docstring：坏剧本必须让加载失败，而不是被静默跳过
        raise PlayError("剧本加载失败：\n  - " + "\n  - ".join(errors))

    if directory is None:
        _cache = plays
    return plays


def get(name: str) -> Play:
    """取一份剧本。不存在就抛 PlayError（调用方翻成 404）。"""
    plays = load_all()
    if name not in plays:
        raise PlayError(f"没有名为 {name!r} 的剧本。现有：{sorted(plays)}")
    return plays[name]


def counts() -> dict:
    plays = load_all()
    return {
        "plays": len(plays),
        "steps": sum(len(p.steps) for p in plays.values()),
        "actions": sum(len(p.action_steps()) for p in plays.values()),
        "irreversible": sum(
            1 for p in plays.values() for s in p.action_steps()
            if s.rollback.get("none")),
    }


def reset() -> None:
    """丢掉缓存（测试用；也用于运行时改完剧本后重新加载）。"""
    global _cache
    _cache = None
