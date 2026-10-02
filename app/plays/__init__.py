# -*- coding: utf-8 -*-
"""结构化处置剧本（M5 · I-9）。

**不做自动执行、不做自动回滚** —— 项目边界是"写操作永不自动执行"。
剧本只做三件事：把处置流程结构化、在**加载时**校验每一句话是否站得住、
把"这一步退不回去"在执行**之前**摆到人眼前。
"""

from app.plays.model import (  # noqa: F401
    Play,
    PlayError,
    Step,
    load_file,
    render,
    validate,
)
from app.plays.store import (  # noqa: F401
    counts,
    dir_path,
    get,
    load_all,
    reset,
)

__all__ = [
    "Play", "Step", "PlayError", "validate", "load_file", "render",
    "load_all", "get", "counts", "reset", "dir_path",
]
