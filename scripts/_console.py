# -*- coding: utf-8 -*-
"""
Windows 控制台 UTF-8 修复（内部工具，给 scripts/ 下的脚本共用）
============================================================
【问题】
脚本源码是 UTF-8，print 出去的中文在 Windows 控制台里却是乱码。

原因不是"编码写错了"，而是**有两层，各错一半**：

  1. **Python 侧**：输出流按控制台默认编码（简体中文 Windows 上是 GBK / cp936）
     来编码。遇到 ✅ ❌ █ 这类字符会直接抛 UnicodeEncodeError，
     所以很多脚本加了 `sys.stdout.reconfigure(encoding="utf-8")`。
  2. **控制台侧**：代码页仍然是 936。就算 Python 吐出的是正确的 UTF-8 字节，
     控制台也会按 GBK 去解释它 —— **中文照样乱码**。

只做第 1 步是治不了本的，这正是"加了 reconfigure 还是乱码"的原因。
必须**同时**把控制台代码页切成 65001（UTF-8）。

【用法】
在脚本最上方（其它 import 之前）加两行：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _console      # noqa: F401  —— 导入即生效

【注意】
- 输出被重定向到文件或管道时**不动代码页**（那时没有控制台，改代码页没意义，
  而且部分 API 会失败）。此时只靠 reconfigure，文件里得到的仍是干净的 UTF-8。
- 这个修复只影响"脚本在终端里的可读性"，**不影响服务本身** ——
  服务返回的一直是正常 UTF-8（curl 出来的中文都是对的）。
"""

import sys


def enable_utf8() -> None:
    """把 Python 输出流 + 控制台代码页都切到 UTF-8。失败一律静默。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if sys.platform != "win32":
        return
    try:
        # 重定向/管道场景（isatty 为假）跳过：那里没有控制台代码页可切
        if not sys.stdout.isatty():
            return
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)   # 输出代码页 → UTF-8
        kernel32.SetConsoleCP(65001)         # 输入代码页 → UTF-8
    except Exception:
        pass


enable_utf8()
