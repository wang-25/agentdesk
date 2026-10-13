# -*- coding: utf-8 -*-
"""UI 门禁：把"界面又坏了"变成 CI 能拦住的事。

【为什么要专门做一个 UI 门禁】
这次重做的起点是一个**只有截图能发现**的 bug：

    .hbar .fill { height: 100%; }     /* 行内 <span>，高度不生效 */

容器画出来了、CSS 也没报错、单元测试全绿 —— 但图表上一根数据条都看不见。
`pytest` 永远抓不到这种问题（它不渲染 CSS）。所以这里补三道**能自动跑**的检查。

【三道检查，从便宜到贵】
  ① 静态：样式只有一份（页面里不许有 `<style>`）、不许引外部 CDN、
         条形填充必须有 `display:block`
  ② 组件：服务端渲染的条形图/时间线 HTML 里必须真的有 `.fill` / `.fl` 节点
  ③ **实测布局**：用无头 Chrome 打开一个 harness 页，让页面里的 JS
     量出填充条的真实像素高度并写回 DOM，再用 `--dump-dom` 读回来断言 > 0
     —— 这是唯一能真正复现"行内元素高度不生效"这类问题的手段。

★ ③ 不需要 Pillow、不需要 Playwright：Chrome 自带的 `--dump-dom` 会在跑完 JS 后
  输出 DOM，我们把测量结果写进属性就能读回来。零新依赖。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console      # noqa: F401,E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 允许的外部资源：没有。零依赖零构建意味着页面不许从 CDN 取任何东西。
EXTERNAL_RE = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.I)


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return ""


# ============================================================
# ① 静态检查
# ============================================================
def check_css_fills() -> list:
    """条形填充必须 display:block —— 这正是当初让图表变空的原因。"""
    css = (PROJECT_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    results = []
    for selector in (".bar .fill", ".tl .fl"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        if not block:
            results.append((False, f"CSS 里找不到 {selector} 的规则"))
            continue
        body = block.group(1)
        has_block = "display:block" in body.replace(" ", "")
        results.append((has_block,
                        f"{selector} 有 display:block（行内元素的高度不生效，缺了它图表就是空的）"
                        if has_block else
                        f"{selector} 缺少 display:block —— 图表会渲染成空白"))
    return results


def check_no_page_style() -> list:
    """页面里不许再有页面级 `<style>` —— 样式只能有一份来源。

    ★ 只看**真正会被输出的字符串**：用 AST 剥掉 docstring。
      第一版是全文 find("<style")，于是页面 docstring 里写一句
      "本页不写页面级 <style>" 就把它判红了 —— **门禁自己制造假红，
      比没有门禁更糟**（人一旦习惯忽略它，真问题也就漏过去了）。
    """
    import ast

    offenders = []
    for path in sorted((PROJECT_ROOT / "app" / "web" / "pages").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # 收集所有 docstring 节点的位置，先把它们排除
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))

        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and "<style" in node.value):
                offenders.append(f"{path.name}:{node.lineno}")
    if offenders:
        return [(False, f"这些页面里真的输出了页面级 <style>：{offenders}"
                        f"（样式应全部走 app.css）")]
    return [(True, "页面模块没有输出页面级 <style>（样式单一来源）")]


# ============================================================
# ② 组件渲染检查
# ============================================================
def check_components_render() -> list:
    from app.web import components as C

    results = []
    bars = C.bar_list([("llm", 0.0004), ("tool", 0.0)], fmt=C.money)
    results.append((('class="bars"' in bars and 'class="fill"' in bars
                     and "width:" in bars),
                    "bar_list() 渲染出 .bars/.fill 且带宽度"))
    tl = C.timeline([{"node": "intent", "elapsed_ms": 400, "ok": True},
                     {"node": "diagnose", "elapsed_ms": 5400, "ok": True}])
    results.append((('class="fl"' in tl and "width:" in tl),
                    "timeline() 渲染出 .fl 且带宽度"))
    results.append((C.money(0) == "¥0" and C.money(0.0004) == "¥0.0004"
                    and C.money(12.3456) == "¥12.35",
                    "money() 按量级自适应精度（0 不显示成 ¥0.0000）"))
    results.append((C.esc('<img src=x onerror=1>').startswith("&lt;img"),
                    "esc() 会转义（XSS 防线）"))
    table = C.table(["类型", "次数"], [["llm", "2"]])
    results.append((table.count('class="r"') >= 2,
                    "table() 数字列自动右对齐（numeric_from）"))
    return results


# ============================================================
# ②-B CSS 覆盖对账：页面里用到的类，app.css 里必须都有
# ============================================================
def check_css_coverage() -> list:
    """页面/组件/JS 里用到的 CSS 类，是否都在 `app.css` 里定义过。

    ★ 为什么要这一条：类名打错（`crad`）或**样式被误删**都不会报错，
      只会静默地没有样式 —— 页面照常 200、pytest 全绿、看着就是"有点怪"。
      真实的例子：P4 删掉旧的 `_BASE_CSS` 时，把 `try.js` 仍在用的
      `.steps/.step/.dot/.sum` 一起删了，流式步骤当场失去样式而无人察觉。
    """
    css = (PROJECT_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))

    sources = list((PROJECT_ROOT / "app" / "web" / "pages").glob("*.py"))
    sources += list((PROJECT_ROOT / "app" / "web" / "static").glob("*.js"))
    sources += [PROJECT_ROOT / "app" / "web" / "components.py",
                PROJECT_ROOT / "app" / "web" / "layout.py"]

    used = {}
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'class="([^"]+)"', text):
            raw = m.group(1)
            # ★ 只接受"干净的类名列表"：JS 里拼的 `class="' + cls + '"` 会被
            #   正则匹配成 `' + cls + `，那里面出现的 cls / tone / true 不是类名。
            #   第一版没过滤，于是报出 5 个假缺失（cls/tone/true/false/nameOk）。
            if not re.match(r"^[a-zA-Z][\w-]*(\s+[a-zA-Z][\w-]*)*$", raw.strip()):
                continue
            for cls in raw.split():
                used.setdefault(cls, set()).add(path.name)

    missing = {c: sorted(v) for c, v in used.items() if c not in defined}
    if missing:
        detail = "、".join(f"{c}（{','.join(w)}）" for c, w in sorted(missing.items()))
        return [(False, f"{len(missing)} 个类在页面里用到但 app.css 里没定义：{detail}")]
    return [(True, f"页面用到的 {len(used)} 个类在 app.css 里都有定义")]


# ============================================================
# ②-C 内联 JS 语法：pytest 全绿也可能整页白屏
# ============================================================
def check_inline_js_syntax() -> list:
    """用 node 检查**渲染后**的 JS（页面内联脚本 + `static/*.js`）。

    ★ 这一条来自 P4 的两个真实 bug：内联脚本里写了 Python 风格的
      `card(x, title: "…")` —— 整段脚本 SyntaxError 不执行；
      服务端 200、pytest 全绿、**页面全空**。CSS 门禁抓不到这类问题，
      只有把 JS 当 JS 解析才行。

    ★ 为什么要检查**渲染后**的 HTML 而不是页面源码：
      源码里的内联脚本是 f-string，含 `{json.dumps(...)}` 这类占位符，
      直接抽出来"检查"只会得到假错误（第一版就踩了：把 `{_JS}` 和
      `window.__VIEW__="{json.dumps(flags)}"` 当成语法错）。
      浏览器收到的是渲染结果，那就检查渲染结果。
    """
    node = shutil.which("node")
    if not node:
        return [(True, "未安装 node，跳过 JS 语法检查（这一项在此环境不生效）")]

    def check(js: str) -> tuple:
        tmp = Path(tempfile.mkdtemp(prefix="ui_js_")) / "snippet.js"
        tmp.write_text(js, encoding="utf-8")
        out = subprocess.run([node, "--check", str(tmp)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        return out.returncode == 0, (out.stderr or "").strip()

    results = []

    # ① 静态 JS 文件
    for path in sorted((PROJECT_ROOT / "app" / "web" / "static").glob("*.js")):
        ok, err = check(path.read_text(encoding="utf-8"))
        results.append((ok, f"{path.name} 语法检查"
                        + ("" if ok else f"：{err[:160]}")))

    # ② 渲染后的页面内联脚本
    from fastapi.testclient import TestClient

    import app.main as m

    with TestClient(m.app) as client:
        for page in PAGES:
            html = client.get(page).text
            # ★ 只取**可执行的**脚本：排除 `src=`（外部/静态文件，另有检查）
            #   与 `type="application/json"` 这类**数据块**。
            #   页面把配置与口径定义放在 `<script type="application/json">` 里，
            #   那不是 JS —— 上一版把 `{"needsToken": false}` 当脚本解析，
            #   于是报出"语法错误"。判错的门禁会被学会无视，必须改准。
            for attrs, block in re.findall(r"<script([^>]*)>(.*?)</script>",
                                           html, re.S):
                if "src=" in attrs:
                    continue
                type_m = re.search(r'type\s*=\s*["\']([^"\']+)', attrs)
                if type_m and type_m.group(1).lower() not in (
                        "text/javascript", "application/javascript", "module"):
                    continue                     # 数据块，跳过
                if not block.strip():
                    continue
                ok, err = check(block)
                results.append((ok, f"{page} 内联脚本"
                                + ("" if ok else f"：{err[:160]}")))
    if not results:
        results.append((True, "没有需要检查的 JS"))
    return results


# ============================================================
# ③ 实测布局（无头 Chrome + --dump-dom）
# ============================================================
HARNESS = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="{css}"></head><body>
<div id="host">{body}</div>
<script>
/* 把测量结果写回 DOM —— 这样 chrome --dump-dom 就能读回来。
   这正是在复现"行内元素 height:100% 不生效"那类问题：
   容器在、类在，但填充条的实际高度是 0。 */
function measure(){
  var fill = document.querySelector('.bar .fill');
  var fl = document.querySelector('.tl .fl');
  document.body.setAttribute('data-fill-h', fill ? fill.offsetHeight : -1);
  document.body.setAttribute('data-fill-w', fill ? fill.offsetWidth : -1);
  document.body.setAttribute('data-tl-h', fl ? fl.offsetHeight : -1);
}
window.addEventListener('load', measure);
</script></body></html>"""


def check_layout_measures(chrome: str, keep_dir: Path = None) -> list:
    """用真实浏览器量填充条的像素高度。"""
    if not chrome:
        return [(False, "找不到 Chrome/Edge —— 无法做布局实测（这一步是图表 bug 的唯一守门人）")]

    from app.web import components as C

    body = C.bar_list([("llm", 4.0), ("tool", 1.0)], fmt=C.money) + \
        C.timeline([{"node": "intent", "elapsed_ms": 400, "ok": True},
                    {"node": "diagnose", "elapsed_ms": 5400, "ok": True}])
    css_uri = (PROJECT_ROOT / "app" / "web" / "static" / "app.css").as_uri()
    html = HARNESS.replace("{css}", css_uri).replace("{body}", body)

    tmp = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="ui_check_"))
    page = tmp / "harness.html"
    page.write_text(html, encoding="utf-8")

    out = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=90, encoding="utf-8",
        errors="replace")
    dom = out.stdout or ""

    def attr(name: str) -> int:
        m = re.search(name + r'="(-?\d+)"', dom)
        return int(m.group(1)) if m else -999

    results = []
    fill_h, fill_w = attr("data-fill-h"), attr("data-fill-w")
    tl_h = attr("data-tl-h")
    results.append((fill_h > 0 and fill_w > 0,
                    f"条形图填充实测 {fill_w}×{fill_h}px（>0 才算真的画出来了）"))
    results.append((tl_h > 0, f"时间线条实测高 {tl_h}px"))
    if fill_h <= 0:
        results.append((False, "★ 图表是空的 —— 检查 .bar .fill 是否丢了 display:block"))
    return results


# ============================================================
# ④ 页面冒烟（含"不许引外部 CDN"）
# ============================================================
PAGES = ["/", "/try", "/dashboard", "/settings", "/view/metrics"]


def check_pages() -> list:
    from fastapi.testclient import TestClient

    import app.main as m

    results = []
    with TestClient(m.app) as client:
        for path in PAGES:
            r = client.get(path)
            if r.status_code != 200:
                results.append((False, f"{path} 返回 {r.status_code}"))
                continue
            html = r.text
            if EXTERNAL_RE.search(html):
                results.append((False, f"{path} 引用了外部 CDN 资源（零依赖要求离线可用）"))
            elif "<style" in html:
                results.append((False, f"{path} 里还有页面级 <style>"))
            else:
                results.append((True, f"{path} 200 · 无外部资源 · 无页面级 style "
                                      f"（{len(html)} 字节）"))
    return results


# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="UI 门禁（静态 + 组件 + 实测布局 + 页面冒烟）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-layout", action="store_true", help="跳过浏览器实测布局")
    parser.add_argument("--keep", action="store_true", help="保留 harness 临时文件")
    parser.add_argument("--open", action="store_true",
                        help="把 harness 在浏览器里打开看一眼（排障用）")
    args = parser.parse_args()

    chrome = "" if args.no_layout else find_chrome()
    keep = Path(tempfile.mkdtemp(prefix="ui_check_")) if args.keep else None

    groups = [
        ("静态：样式与零依赖", [
            *check_css_fills(),
            *check_no_page_style(),
        ]),
        ("组件：服务端渲染", check_components_render()),
        ("CSS 覆盖对账", check_css_coverage()),
        ("内联 JS 语法", check_inline_js_syntax()),
        ("实测布局：真实浏览器量像素", check_layout_measures(chrome, keep)),
        ("页面冒烟", check_pages()),
    ]

    all_results = [r for _, rs in groups for r in rs]
    failed = [msg for ok, msg in all_results if not ok]

    if args.json:
        print(json.dumps({"passed": len(all_results) - len(failed),
                          "failed": len(failed),
                          "items": [{"ok": ok, "msg": msg} for ok, msg in all_results]},
                         ensure_ascii=False, indent=2))
        return 1 if failed else 0

    print("=" * 66)
    print("  UI 门禁")
    print("=" * 66)
    for title, results in groups:
        print(f"\n  【{title}】")
        for ok, msg in results:
            print(f"    {'✅' if ok else '❌'} {msg}")

    print("\n" + "=" * 66)
    if failed:
        print(f"  ❌ {len(failed)} 项未通过")
        return 1
    print(f"  ✅ 全部通过（{len(all_results)} 项）")
    if args.open and keep:
        webbrowser.open((keep / "harness.html").as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
