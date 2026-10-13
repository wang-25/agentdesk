# -*- coding: utf-8 -*-
"""Web 层（UI）—— 与后端逻辑分开，页面代码不再堆在 `main.py` 里。

【为什么单独一层】
`main.py` 曾经有 **49% 的代码是 UI**（1475 / 3039 行），5 个页面各自还带一份
34–53 行的 `<style>` 互相重复 —— 改一处颜色要翻 3000 行、翻 5 个地方，
于是必然漂移（原界面里卡片与提示条的圆角就是不一致的）。

【为什么不用前端框架 / 构建步骤】
直接依赖只有 9 个、CI 有守卫。为了几个读多写少的页面引入 Node 工具链，
换来的复杂度（构建、产物、版本、部署链）远大于收益。这里用的是
**服务端拼字符串 + 一个真正共享的 CSS/JS**，零依赖、零构建、改完刷新即见。

【目录】
    layout.py       统一外壳（`<head>` + 侧栏 + 页头 + 页脚）
    components.py   组件库（卡片 / KPI / 表格 / 条形图 / 时间线 / 空态 / 骨架）
    pages/          各页面（每个模块提供 register(app)）
    static/         app.css（设计系统唯一实现）+ app.js（公共前端工具）
"""

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.web.pages import register_pages

STATIC_DIR = Path(__file__).resolve().parent / "static"

__all__ = ["mount", "STATIC_DIR"]


def mount(app) -> dict:
    """把静态资源与页面挂到 app 上。

    静态资源走 FastAPI 自带的 `StaticFiles`（**零新依赖**）。
    ★ `check_dir=True` 是默认值，这里显式依赖它：CSS/JS 目录缺失时
      应该**启动就报错**，而不是等到某个页面 404 才发现样式没挂上。
    """
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    pages = register_pages(app)
    return {"static": str(STATIC_DIR), "pages": pages}
