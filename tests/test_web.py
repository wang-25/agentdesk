# -*- coding: utf-8 -*-
"""Web 层（P1 地基 + 门禁）测试。

这里守两类东西：

① **组件的正确性** —— 尤其是"数字右对齐""货币精度""转义"这些
   上一版界面明确做错的地方（它们不是审美偏好，是可用性与安全问题）。

② **门禁本身可用** —— `scripts/ui_check.py` 的静态检查也在这里再跑一遍。
   理由：门禁是个脚本，容易被绕过（"这次先不看它"）；
   而放进 pytest 就意味着 **`pytest` 绿 = 界面基本规矩没破**。
"""

import importlib.util
import sys

import pytest

from app.llm import PROJECT_ROOT
from app.web import components as C
from app.web import layout as L

# 门禁脚本：导入它自己会把 scripts/ 加进 sys.path
_spec = importlib.util.spec_from_file_location(
    "ui_check_under_test", PROJECT_ROOT / "scripts" / "ui_check.py")
assert _spec is not None and _spec.loader is not None
ui = importlib.util.module_from_spec(_spec)
sys.modules["ui_check_under_test"] = ui
_spec.loader.exec_module(ui)


@pytest.fixture
def app_client():
    """进程内客户端：用于静态资源与页面的 HTTP 冒烟。"""
    from fastapi.testclient import TestClient

    import app.main as m

    with TestClient(m.app) as c:
        yield c


# ============================================================
# 一、转义：少转一处就是一个 XSS
# ============================================================
@pytest.mark.parametrize("raw", [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "<style>body{display:none}</style>",
])
def test_esc_escapes_dangerous_input(raw):
    out = C.esc(raw)
    assert "<script" not in out and "<img" not in out and "<style" not in out
    assert "&lt;" in out or "&quot;" in out


def test_esc_does_not_pretend_to_sanitise_urls():
    """★ 转义只解决 **HTML 文本上下文**里的注入。

    `javascript:alert(1)` 里没有 `<` 或 `"`，所以 `esc()` 原样返回 —— 这是**正确的**：
    它的职责不是"判断一个字符串危不危险"，而是"让它作为文本安全地显示出来"。
    URL 上下文（`<a href=...>`）需要单独的校验（只允许 http/https 白名单）。
    这条用例把边界写清楚，免得以后有人以为 esc 能包办一切。
    """
    assert C.esc("javascript:alert(1)") == "javascript:alert(1)"
    # 但它必须挡住"用引号逃出属性"的那类
    assert "&quot;" in C.esc('" onmouseover="alert(1)')


def test_esc_handles_none():
    assert C.esc(None) == ""


def test_components_escape_their_inputs():
    """组件的文本参数都必须过转义 —— 页面里渲染的是用户提问与命令输出。"""
    assert "<script" not in C.kpi("<script>x</script>", 1)
    assert "<script" not in C.badge("<script>x</script>")
    assert "<script" not in C.card("", title="<script>x</script>")
    assert "<img" not in C.empty("<img src=x>")


# ============================================================
# 二、货币与耗时：上一版明确做错的两处
# ============================================================
@pytest.mark.parametrize("value,expect", [
    (0, "¥0"),                    # ★ 不再显示成 ¥0.0000（整列看起来全是零）
    (0.0004, "¥0.0004"),
    (0.05, "¥0.05"),
    (0.009, "¥0.0090"),           # 小于 1 分才用四位，避免把 0.05 写成 0.0500
    (12.3456, "¥12.35"),          # 常规量级两位小数
    (12345.6, "¥12,346"),         # 大额千分位
    (None, "¥0"),
    ("bad", "—"),
])
def test_money_adapts_precision(value, expect):
    assert C.money(value) == expect


@pytest.mark.parametrize("ms,expect", [
    (0, "0 ms"), (999, "999 ms"), (1000, "1.00 s"), (1393, "1.39 s"), (None, "0 ms"),
])
def test_seconds_has_a_space_between_number_and_unit(ms, expect):
    """上一版写成 `耗时ms`（数字与单位粘在一起）。"""
    assert C.seconds(ms) == expect
    assert "ms" not in C.seconds(1500)


# ============================================================
# 三、表格与图表
# ============================================================
def test_table_right_aligns_numeric_columns():
    """★ 数字左对齐时一列数字没法竖着扫读（上一版看板就是左对齐）。

    3 列表头（1 个文本 + 2 个数字）+ 1 行数据 → 右对齐单元格 = 2（表头）+ 2（数据）。
    """
    html = C.table(["类型", "次数", "成本"], [["llm", "2", "¥0.0004"]])
    assert html.count('class="r"') == 4
    assert html.index("<th>类型</th>") < html.index('<th class="r">次数</th>')
    assert "tablewrap" in html               # 窄屏可横向滚动


def test_table_supports_a_total_row():
    html = C.table(["类型", "次数"], [["llm", "2"]], foot=["合计", "25"])
    assert "<tfoot>" in html and "合计" in html


def test_bar_list_renders_fills_with_width():
    """条形图的填充节点必须存在且带宽度 —— 缺了就是一张空白图。"""
    html = C.bar_list([("llm", 4.0), ("tool", 1.0)], fmt=C.money)
    assert 'class="bars"' in html
    assert html.count('class="fill"') == 2
    assert "width:100.0%" in html and "width:25.0%" in html
    assert "display:block" not in html, "display:block 应该在 CSS 里，不是行内样式"


def test_bar_list_sorts_descending_and_handles_empty():
    html = C.bar_list([("a", 1), ("b", 9)])
    assert html.index(">b<") < html.index(">a<")
    assert "暂无数据" in C.bar_list([])


def test_timeline_uses_chinese_node_labels_and_percentages():
    html = C.timeline([
        {"node": "intent", "elapsed_ms": 400, "ok": True},
        {"node": "diagnose", "elapsed_ms": 1600, "ok": True},
    ])
    assert "意图路由" in html and "工具执行" in html
    assert "width:20.0%" in html and "width:80.0%" in html
    # 失败节点要有区分样式
    assert "bad" in C.timeline([{"node": "verify", "elapsed_ms": 5, "ok": False}])
    assert "暂无计时数据" in C.timeline([])


def test_info_tip_is_a_hover_hint_not_body_text():
    """指标口径走 ⓘ，不占正文（上一版看板被 100+ 字散文占领）。"""
    html = C.info_tip("运行次数 = 接口调用次数，不等于用户数")
    assert 'class="info"' in html and "title=" in html


# ============================================================
# 四、外壳：导航高亮 / 静态资源 / 状态点
# ============================================================
def test_shell_contains_nav_and_marks_the_active_page():
    html = L.shell("正文", title="看板", active="/dashboard", subtitle="说明")
    assert html.startswith("<!DOCTYPE html>")
    for href in ("/", "/try", "/dashboard", "/settings"):
        assert f'href="{href}"' in html
    assert 'href="/dashboard" aria-current="page"' in html
    assert "/static/app.css" in html and "/static/app.js" in html
    assert "看板" in html and "说明" in html


def test_shell_has_no_external_resource():
    """零依赖、可离线：外壳不许引外部 CDN。"""
    assert not ui.EXTERNAL_RE.search(L.shell("x", title="t"))


def test_shell_carries_the_health_dot_and_env_badge():
    html = L.shell("x", title="t", env_badge="公网 · 需令牌")
    assert 'id="healthDot"' in html and "公网 · 需令牌" in html


def test_every_nav_target_is_a_real_page():
    """导航里的每一项都必须真的存在（改了路由别忘了改导航）。"""
    import app.main as m

    paths = {getattr(r, "path", None) for r in m.app.routes}
    for href, _icon, label in L.NAV:
        assert href in paths, f"导航项 {label}（{href}）没有对应路由"


# ============================================================
# 五、mount：静态资源与页面注册
# ============================================================
def test_static_files_are_served(app_client):
    for path in ("/static/app.css", "/static/app.js"):
        r = app_client.get(path)
        assert r.status_code == 200
        assert len(r.content) > 500
    assert "text/css" in app_client.get("/static/app.css").headers["content-type"]


def test_css_keeps_the_fill_rule_that_prevents_blank_charts():
    """★ 这条是"图表变空白"那次事故的守门用例（字体级别的回归保护）。

    `.bar .fill` / `.tl .fl` 是行内 `<span>`，**行内元素的高度不生效** ——
    丢了 `display:block` 图表就一个数据条都不显示，而 pytest 不会渲染 CSS。
    """
    css = (PROJECT_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    for selector in (r"\.bar \.fill", r"\.tl \.fl"):
        import re
        block = re.search(selector + r"\s*\{([^}]*)\}", css)
        assert block, f"app.css 里找不到 {selector}"
        assert "display:block" in block.group(1).replace(" ", ""), \
            f"{selector} 丢了 display:block —— 图表会渲染成空白"


def test_design_tokens_are_defined_in_one_place():
    """设计变量只应出现在 app.css 里（页面里不许有硬编码的品牌色）。"""
    css = (PROJECT_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    for token in ("--brand-1", "--brand-grad", "--ok", "--err", "--surface"):
        assert token in css, f"设计变量 {token} 没定义"
    for page in (PROJECT_ROOT / "app" / "web" / "pages").glob("*.py"):
        text = page.read_text(encoding="utf-8")
        assert "#4f46e5" not in text and "#0ea5e9" not in text, \
            f"{page.name} 里硬编码了品牌色（应走 app.css 的变量）"


# ============================================================
# 六、把门禁的静态检查纳入 pytest
# ============================================================
def test_gate_static_checks_pass():
    """`pytest` 绿 = 界面基本规矩没破（不必额外记得跑门禁脚本）。"""
    results = [*ui.check_css_fills(), *ui.check_no_page_style(),
               *ui.check_components_render()]
    failed = [msg for ok, msg in results if not ok]
    assert not failed, "UI 门禁的静态检查未通过：\n  - " + "\n  - ".join(failed)


def test_gate_page_list_matches_the_nav():
    """门禁要检查的页面 = 导航里的页面 + 一个数据视图页。"""
    for href, _icon, _label in L.NAV:
        assert href in ui.PAGES
    assert "/view/metrics" in ui.PAGES
