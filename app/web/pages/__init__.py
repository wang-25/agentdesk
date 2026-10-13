# -*- coding: utf-8 -*-
"""页面注册表。

【为什么要显式列出"已实现的页面"】
每个阶段替换一个页面：先做好的页面接管路由，没做的继续用 main.py 里的旧实现。
用显式列表而不是 `glob` + 静默跳过 —— **静默跳过会掩盖真实错误**
（模块里一个 import 打错，表现成"页面还是旧的"，很难查）。

【每阶段的接线顺序】
    P1  ← 现在：只挂 /static，不动任何页面（旧页面继续工作）
    P2  热加载 home / try_
    P3  热加载 dashboard
    P4  热加载 settings / view，并删掉 main.py 里的旧 HTML（主文件减半）
"""

import importlib

# 已由 app/web 接管的页面模块（每阶段追加一个）
IMPLEMENTED = [
    "home",
    "try_",
    # "dashboard",
    # "settings",
    # "view",
]


def register_pages(app) -> list:
    """把已实现的页面注册到 app 上，返回注册成功的模块名列表。

    每个页面模块提供 `register(app)`，内部用 `@app.get(...)` 注册自己的路由。
    """
    done = []
    for name in IMPLEMENTED:
        mod = importlib.import_module(f"app.web.pages.{name}")
        mod.register(app)
        done.append(name)
    return done
